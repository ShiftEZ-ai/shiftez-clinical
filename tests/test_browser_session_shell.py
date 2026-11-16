"""Shell behaviour across every page: what a person sees when their session ends, and where sign-in returns them."""
import re
from datetime import timedelta
import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, expect
from sqlalchemy import update
from apps.api.models import Session
from packages.domain.time import now
from .conftest import TEST_PASSWORD
from .test_browser import route_fetch, browser, live_server, sign_in, navigate, expect_loaded, wait_until  # noqa: F401

pytestmark = pytest.mark.browser


def advance_until(page, done, step=11_000, tries=12):
    """Moves the page's clock one periodic check at a time until `done()`, letting each check's request answer before
    the clock moves on: the page skips a check while the previous one is still waiting, so racing ahead loses checks."""
    for _ in range(tries):
        if done():
            return True
        try:
            with page.expect_response('**/api/pulse', timeout=3_000):
                page.clock.run_for(step)
        except PlaywrightTimeoutError:
            pass  # that step's check was skipped (the previous one was still answering): move on again
    return done()


def test_idle_expiry_says_why_on_the_sign_in_form_and_returns_to_the_same_page(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        # The session ran past its idle limit on the server (no request moved it on).
        with h.app.state.engine.begin() as db:
            db.execute(update(Session).values(expires_at=now() - timedelta(minutes=1)))
        page.evaluate("location.hash='rules'")
        form = page.locator('.login-form')
        expect(form.get_by_role('status')).to_contain_text('signed out after a period of inactivity')
        expect(page.get_by_label('Username or email', exact=True)).to_be_visible()
        page.get_by_label('Username or email', exact=True).fill('coordinator')
        page.get_by_role('button', name='Continue', exact=True).click()
        page.get_by_label('Password', exact=True).fill(TEST_PASSWORD)
        page.get_by_role('button', name='Sign in', exact=True).click()
        # Back where they were going, not on Home.
        expect_loaded(page, 'rules')
        # A deliberate sign-out says nothing about inactivity.
        page.get_by_role('button', name='Account menu, ', exact=False).click()
        page.get_by_role('menuitem', name='Sign out').click()
        # Signed in through the form, so the device now offers that account.
        expect(page.locator('.login-account')).to_contain_text('coordinator')
        expect(page.locator('.login-form').get_by_role('status')).to_have_count(0)
    finally:
        context.close()


def test_sign_in_page_offers_continue_as_the_last_account_on_this_device(browser, live_server):
    """After a sign-in, the device remembers who (never a password): the next sign-in is Continue, then the password,
    with "Use another account" and a remove button for shared computers."""
    from .test_browser import sign_in_with_form
    context, page = sign_in_with_form(browser, live_server, 'coordinator')
    try:
        stored = page.evaluate("localStorage.getItem('rota.account')")
        assert 'coordinator' in stored and TEST_PASSWORD not in stored
        page.get_by_role('button', name='Account menu, ', exact=False).click()
        page.get_by_role('menuitem', name='Sign out').click()
        chip = page.locator('.login-account')
        expect(chip).to_contain_text('coordinator')
        expect(page.get_by_label('Username or email', exact=True)).to_be_hidden()
        expect(page.get_by_role('button', name='Continue', exact=True)).to_be_focused()
        page.get_by_role('button', name='Continue', exact=True).click()
        expect(page.get_by_label('Password', exact=True)).to_be_focused()
        expect(page.get_by_role('button', name='Not you? Change')).to_be_hidden()  # the chip says who
        page.get_by_label('Password', exact=True).fill(TEST_PASSWORD)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect_loaded(page, 'home')
        page.get_by_role('button', name='Account menu, ', exact=False).click()
        page.get_by_role('menuitem', name='Sign out').click()
        page.get_by_role('button', name='Use another account', exact=True).click()
        expect(chip).to_have_count(0)
        expect(page.get_by_label('Username or email', exact=True)).to_be_focused()
        expect(page.get_by_label('Username or email', exact=True)).to_have_value('')
        expect(page.get_by_label('Remember me', exact=True)).not_to_be_checked()
        expect(page.get_by_label('Password', exact=True)).to_be_hidden()
        page.reload()
        page.get_by_role('button', name=re.compile('^Remove .* from this device$')).click()
        expect(chip).to_have_count(0)
        expect(page.get_by_label('Username or email', exact=True)).to_be_visible()
        assert page.evaluate("localStorage.getItem('rota.account')") is None
    finally:
        context.close()


def test_institutional_only_sign_in_offers_continue_as_and_use_another_account(browser, live_server):
    """Without password sign-in, the remembered account is the institutional button's "Continue as <name>";
    "Use another account" turns it back into the plain institutional sign-in."""
    from .test_browser import new_context, open_app

    def institutional_only(route):
        reply = route_fetch(route)
        route.fulfill(response=reply, json={**reply.json(), 'password_login': False, 'development_login': False, 'institutional_login': True})
    context = new_context(browser)
    try:
        context.add_init_script("localStorage.setItem('rota.account', JSON.stringify({identity: 'x', name: 'Quartz Example', username: ''}))")
        context.route('**/auth/config', institutional_only)
        page = open_app(context, live_server)
        form = page.locator('.login-form')
        expect(form.get_by_role('link', name='Continue as Quartz Example', exact=True)).to_have_attribute('href', re.compile('^/auth/start'))
        expect(form.get_by_label('Password', exact=True)).to_have_count(0)
        form.get_by_role('button', name='Use another account', exact=True).click()
        expect(page.locator('.login-account')).to_have_count(0)
        expect(form.get_by_role('link', name='Continue with institutional sign-in', exact=True)).to_be_focused()
        expect(form.get_by_role('button', name='Use another account', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_an_open_tab_learns_of_a_new_version_and_offers_a_reload(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff')
    seen = []

    def deployed(route):
        # The first check records the running build; every later one reports a newer bundle (a deploy happened).
        reply = route_fetch(route)
        body = reply.json()
        seen.append(body['build'])
        if len(seen) > 1:
            body['build'] += '-next'
        route.fulfill(response=reply, json=body)
    try:
        page.route('**/api/pulse', deployed)
        bar = page.locator('.update-bar')
        # The periodic check, on the page's own clock: the first records the running build, a later one sees the deploy.
        assert advance_until(page, lambda: len(seen) >= 2), seen
        expect(bar).to_be_visible()
        expect(bar).to_have_attribute('role', 'status')
        # One plain notice (N-18): no build numbers, no warning about entries when nothing is unsaved.
        expect(bar).to_have_text(re.compile(r'^A new version of Rota is ready\.\s*Reload\s*Later$'))
        expect(bar.get_by_role('button', name='Reload', exact=True)).to_be_visible()
        # Later dismisses it and nothing is reloaded; it is not offered again for the same version.
        bar.get_by_role('button', name='Later').click()
        expect(bar).to_have_count(0)
        checks = len(seen)
        # Two more checks report the same new version.
        assert advance_until(page, lambda: len(seen) >= checks + 2), seen
        expect(bar).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 900}, {"width": 390, "height": 844}], ids=["desktop", "phone"])
@pytest.mark.parametrize("dirty", [False, True], ids=["saved", "unsaved-settings"])
def test_sign_out_navigates_to_provider_logout_after_revoking_app_session(browser, live_server, viewport, dirty):
    """Real sign-out request plus a synthetic provider URL: the browser must perform the cross-origin navigation."""
    context, page = sign_in(browser, live_server, 'admin' if dirty else 'staff', viewport)
    provider = "https://idp.test/logout?state=synthetic-browser-logout"

    def institutional_logout(route):
        response = route_fetch(route)
        assert response.status == 200 and response.json()["signed_out"] is True
        # A background auth failure while the logout reply is pending must not discard the provider redirect.
        page.evaluate("async () => {const api=await window.importBundle('api.js');await api.get('/api/me').catch(()=>{});"
                      "window.dispatchEvent(new CustomEvent('session-expired'));}")
        route.fulfill(response=response, json={**response.json(), "end_session_url": provider})

    context.route('**/auth/logout', institutional_logout)
    context.route(provider, lambda route: route.fulfill(status=200, content_type='text/html', body='<title>Provider logout</title>'))
    dialogs = []
    page.on('dialog', lambda dialog: (dialogs.append(dialog.type), dialog.dismiss()))
    try:
        if dirty:
            page.goto(live_server + '/#configuration/department')
            page.get_by_label('Department name', exact=True).fill('Unsaved synthetic name')
            expect(page.get_by_role('button', name='Save settings', exact=True)).to_be_enabled()
        page.get_by_role('button', name='Account menu, ', exact=False).click()
        page.get_by_role('menuitem', name='Sign out', exact=True).click()
        expect(page).to_have_url(provider)
        expect(page).to_have_title('Provider logout')
        assert not dialogs
        assert context.request.get(live_server + '/api/me').status == 401
        assert not any(cookie['name'] in ('rota_session', 'rota_csrf') for cookie in context.cookies(live_server))
    finally:
        context.close()


def test_a_page_that_fails_in_an_outdated_tab_reloads_once(browser, live_server):
    """QA-073: the server serves a newer bundle than this tab runs (the pulse's build) and a page fails to draw. The tab
    reloads once onto the new bundle. Here the reload lands on the same old bundle and the page fails again, so the
    guard (keyed by that build) stops a second reload and the page says why, with Retry."""
    context, page = sign_in(browser, live_server, 'coordinator')
    loads, newer = [], []

    def deployed(route):
        reply = route_fetch(route)
        body = reply.json()
        body['build'] += '-next'
        newer[:] = [body['build']]
        route.fulfill(response=reply, json=body)
    try:
        navigate(page, 'home')
        page.route('**/api/events*', lambda route: route.abort())  # pulses only from /api/pulse (the faked deploy)
        page.route('**/api/pulse', deployed)
        page.route(re.compile(r'/api/notifications$'), lambda route: route.fulfill(status=500, json={'detail': 'Synthetic old-bundle failure'}))
        page.reload()
        expect_loaded(page, 'home')
        assert newer, 'the start-up pulse was not read'
        page.on('load', lambda _: loads.append(page.url))
        page.evaluate("location.hash = 'inbox'")  # in-app navigation to a page whose read fails
        wait_until(page, lambda: len(loads) == 1, lambda: f'the outdated tab reloaded {len(loads)} times, not once')
        failed = page.locator('#main').get_by_text('Couldn’t load Inbox.', exact=False)
        expect(failed).to_be_visible()  # after the one reload, the same failure is said, not reloaded again
        expect(page.locator('#main').get_by_role('button', name='Retry', exact=True)).to_be_visible()
        assert page.evaluate("sessionStorage.getItem('fmuc.stale-reload')") == newer[0]
        expect(page.get_by_text('Updated to the latest version.', exact=True)).to_have_count(0)  # it is still the old bundle
        assert len(loads) == 1
    finally:
        context.close()


def test_an_error_in_a_click_handler_of_an_outdated_tab_reloads_once(browser, live_server):
    """QA-073 follow-up: a click handler of an older bundle than the server's throws (uncaught). The tab reloads once onto
    the new bundle, by the same step and guard as a failed page; nothing was unsaved."""
    context, page = sign_in(browser, live_server, 'coordinator')
    loads, newer = [], []

    def deployed(route):
        reply = route_fetch(route)
        body = reply.json()
        body['build'] += '-next'
        newer[:] = [body['build']]
        route.fulfill(response=reply, json=body)
    try:
        navigate(page, 'home')
        page.route('**/api/events*', lambda route: route.abort())  # pulses only from /api/pulse (the faked deploy)
        page.route('**/api/pulse', deployed)
        page.reload()
        expect_loaded(page, 'home')
        assert newer, 'the start-up pulse was not read'
        page.on('load', lambda _: loads.append(page.url))
        page.evaluate("document.body.addEventListener('click',()=>{throw new TypeError('Synthetic old-bundle bug');},{once:true});"
                      "document.body.click();")
        wait_until(page, lambda: len(loads) == 1, lambda: f'the outdated tab reloaded {len(loads)} times, not once')
        expect_loaded(page, 'home')
        assert page.evaluate("sessionStorage.getItem('fmuc.stale-reload')") == newer[0]  # the once-per-bundle guard
        expect(page.locator('.update-bar')).to_have_count(0)
    finally:
        context.close()

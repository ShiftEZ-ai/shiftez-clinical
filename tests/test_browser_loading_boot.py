"""Start-up and reconnect waits (DESIGN.md › Loading): the start-up screen centres the configured emblem and the app's
name on both axes on every viewport and theme, breathes calmly (still under reduced motion), and a module that never
answers shows only its moving line, then one quiet "Still loading · 0:15" under it after 15 s (no Retry while it may still
answer), then "Didn’t load in 1:00. Retry" (F-L1, DL-045). A live update that changes roles
never closes an open form or any other dialog, and the restart after it says why (F-L2)."""
import json
import re
from urllib.parse import urlsplit
import pytest
from playwright.sync_api import expect
from .test_browser import (  # noqa: F401
    browser, live_server, sign_in, navigate, bundle_file, route_fetch, reconnect, holding, wait_held,
)

pytestmark = pytest.mark.browser

# Centre of the emblem, of the name and of the whole column against the viewport's centre.
GEOMETRY = """() => {const r=n=>n.getBoundingClientRect(),main=document.querySelector('#main.boot');
  const e=r(main.querySelector('.boot-emblem')),h=r(main.querySelector('h1')),l=r(main.querySelector('.sk-boot'));
  return {emblem:e.left+e.width/2-innerWidth/2,name:h.left+h.width/2-innerWidth/2,line:l.left+l.width/2-innerWidth/2,
    column:(e.top+l.bottom)/2-innerHeight/2,emblemWidth:main.querySelector('.boot-emblem').offsetWidth,scrolls:document.documentElement.scrollWidth>innerWidth};}"""

NAME_TOP = "() => document.querySelector('#main.boot h1').getBoundingClientRect().top"


@pytest.mark.parametrize('width,height,scheme', [(1440, 900, 'light'), (1440, 900, 'dark'), (390, 844, 'light'), (390, 844, 'dark')])
def test_start_up_screen_is_centred_and_recovers_a_module_that_never_answers(browser, live_server, width, height, scheme):
    context = browser.new_context(viewport={'width': width, 'height': height}, color_scheme=scheme)
    page = context.new_page()
    held, hold = [], [True]
    # Held, not aborted (an abort is a failed load, the other screen). Retry's reload drops the held request; unrouting
    # instead would release it (Chromium continues paused requests) and the app would start under the click.
    page.route(bundle_file('app.js'), lambda route: held.append(route) if hold[0] else route.continue_())
    try:
        page.clock.install()
        page.goto(live_server, wait_until='commit')
        boot = page.locator('#main.boot')
        expect(boot.get_by_role('heading', level=1)).to_have_text('Rota')
        # It fades in after 300 ms (CSS, real time): wait for it, then measure.
        page.wait_for_function("() => getComputedStyle(document.querySelector('#main.boot')).opacity === '1'")
        g = page.evaluate(GEOMETRY)
        assert all(abs(g[k]) <= 1 for k in ('emblem', 'name', 'line', 'column')), g
        assert g['emblemWidth'] == 72 and not g['scrolls'], g
        # The emblem breathes while it waits.
        assert page.evaluate("getComputedStyle(document.querySelector('.boot-emblem')).animationName") == 'boot-breathe'
        name_top = page.evaluate(NAME_TOP)
        page.clock.run_for(3000)
        expect(boot.locator('.sk-slow')).to_have_count(0)  # the moving line alone (owner, 8 Nov 2026)
        page.clock.run_for(12000)
        expect(boot.locator('.sk-slow')).to_have_text(re.compile(r'^Still loading · 0:1\d$'))  # its elapsed time
        expect(boot.get_by_role('button', name='Retry', exact=True)).to_have_count(0)  # the module may still answer
        assert abs(page.evaluate(NAME_TOP) - name_top) <= 1  # the line under the bar moves nothing
        page.clock.run_for(45000)
        expect(boot.locator('.sk-slow')).to_have_text('Didn’t load in 1:00. Retry')  # the read's limit, formatted
        assert abs(page.evaluate(NAME_TOP) - name_top) <= 1
        retry = boot.get_by_role('button', name='Retry', exact=True)
        expect(retry).to_be_visible()
        # Retry reloads the page; the module answers this time and the app starts (the sign-in, signed out).
        hold[0] = False
        retry.click()
        expect(page.get_by_label('Username or email', exact=True)).to_be_visible()
    finally:
        context.close()


def test_reduced_motion_keeps_the_start_up_screen_still(browser, live_server):
    context = browser.new_context(viewport={'width': 390, 'height': 844}, reduced_motion='reduce')
    page = context.new_page()
    page.route(bundle_file('app.js'), lambda route: None)  # never answers
    try:
        page.goto(live_server, wait_until='commit')
        emblem = page.locator('#main.boot .boot-emblem')
        expect(emblem).to_be_visible()
        assert page.evaluate("getComputedStyle(document.querySelector('.boot-emblem')).animationName") == 'none'
        # The line stands still as a full accent bar: the wait is still shown, without movement.
        bar = page.evaluate("(() => {const s=getComputedStyle(document.querySelector('.sk-boot .sk'),'::after');"
                            "return [s.animationName, s.left, s.width];})()")
        assert bar[0] == 'none' and bar[1] == '0px' and bar[2] != '0px', bar
    finally:
        context.close()


def test_a_role_change_waits_for_an_open_form(browser, live_server):
    """F-L2: a server restart or upgrade can report other roles; the open, unsaved New request stays open."""
    context, page = sign_in(browser, live_server, 'staff')
    try:
        page.route('**/api/events', lambda route: route.abort())  # no live stream: the 10 s check reads the pulse

        def other_roles(route):
            response = route_fetch(route)
            pulse = response.json()
            pulse['roles'] = sorted({*pulse['roles'], 'auditor'})
            route.fulfill(response=response, body=json.dumps(pulse))
        page.clock.install()
        page.reload()
        navigate(page, 'requests')
        page.get_by_role('button', name='New request', exact=True).click()
        dialog = page.get_by_role('dialog', name='New request')
        dialog.get_by_role('radio', name='Other request').check()
        page.route('**/api/pulse*', other_roles)
        # The next periodic check reads the pulse naming other roles (the first check may have run before the route).
        with page.expect_response('**/api/pulse*'):
            page.clock.run_for(11000)
            page.clock.run_for(11000)
        page.evaluate('() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))')
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role('radio', name='Other request')).to_be_checked()
    finally:
        context.close()


def test_a_role_change_waits_for_any_dialog_then_says_why(browser, live_server, h):
    """F-L2: a real role change never removes a dialog, even one without changes (the browser can close one for a task
    before it opens again); the restart waits until no dialog is left, then says why the page started again."""
    context, page = sign_in(browser, live_server, 'staff')
    try:
        page.route('**/api/events', lambda route: route.abort())
        navigate(page, 'requests')
        page.evaluate('() => document.activeElement?.blur()')
        page.keyboard.press('?')
        dialog = page.get_by_role('dialog', name='Keyboard shortcuts')
        expect(dialog).to_be_visible()
        reads = []
        page.on('request', lambda r: reads.append(r.url) if r.method == 'GET' and urlsplit(r.url).path == '/api/me' else None)
        # Grant the role before the pulse reaches the server so the restart's protected reads use the same access.
        pulse_url = '**/api/pulse*'
        with holding(page) as held:
            held.hold(pulse_url)
            reconnect(page)
            wait_held(page, held[pulse_url])
            request = held[pulse_url][0].request
            h.command('admin', 'role.grant', {
                'identity_id': h.seed['identities']['staff'], 'role': 'credentialer',
                'starts_on': '2020-01-01', 'reason': 'Fictional cover from another session',
            })
            with page.expect_response(lambda response: response.request == request) as pulse:
                held.release(pulse_url)
        assert pulse.value.ok and 'credentialer' in pulse.value.json()['roles']
        expect(page.locator('#connection')).to_have_attribute('aria-label', 'Connected')
        expect(dialog).to_be_visible()
        assert reads == []  # held: no restart under the dialog
        # The held pulse starts the restart before any new pulse is requested.
        with page.expect_request(
            lambda r: r.method == 'GET' and urlsplit(r.url).path in ('/api/me', '/api/pulse')
        ) as replay:
            dialog.get_by_role('button', name='Close dialog').click()
            page.clock.fast_forward(10_000)
        assert urlsplit(replay.value.url).path == '/api/me'
        expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible()
        expect(page.get_by_role('status').filter(has_text='Your access changed.')).to_be_visible()
        expect(dialog).to_have_count(0)
        assert len(reads) == 1, reads
    finally:
        context.close()

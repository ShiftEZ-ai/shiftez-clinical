"""maintenance, truthful sign-out: Sign out blanks the page at once ("Signing out…", nothing of the account in the DOM) and
shows sign-in only once the server has ended the session; a refused sign-out says "Couldn’t sign you out" with Try
again, never the sign-in form; Back after signing out shows no app page. A tab restored from the back/forward cache
(it missed the fmuc-session message) asks the server once and shows sign-in when the session ended or another tab of
the device signed out since it loaded; the same session keeps the page. The restore is the page's own pageshow event
with persisted=true (the browser's cache itself is not under test)."""
import re
from contextlib import nullcontext

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, expect_loaded, navigate  # noqa: F401

pytestmark = pytest.mark.browser
ACCOUNT = re.compile(r'^Account menu, ')


def start_sign_out(page):
    page.get_by_role('button', name=ACCOUNT).click()
    page.get_by_role('menuitem', name='Sign out', exact=True).click()


def nothing_of_the_account(page, name):
    expect(page.get_by_role('button', name=ACCOUNT)).to_have_count(0)
    expect(page.locator('dialog, nav, .asl-result')).to_have_count(0)
    assert name not in page.evaluate('document.body.innerText')


def test_sign_out_blanks_at_once_and_signs_in_only_when_the_server_answers(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        expect_loaded(page, 'home')
        name = page.evaluate("fetch('/api/me').then(r=>r.json()).then(m=>m.display_name)")
        navigate(page, 'people')
        held = []
        page.route('**/auth/logout', lambda route: held.append(route))
        start_sign_out(page)
        expect(page.get_by_role('heading', name='Signing out…', exact=True)).to_be_visible()
        nothing_of_the_account(page, name)
        expect(page.get_by_role('heading', name='Sign in', exact=True)).to_have_count(0)
        assert len(held) == 1
        held[0].continue_()
        expect(page.get_by_role('heading', name='Sign in', exact=True)).to_be_visible()
        assert len(held) == 1
        assert page.request.get(live_server + '/api/me').status == 401
        page.go_back()
        expect(page.get_by_role('heading', name='Sign in', exact=True)).to_be_visible()
        nothing_of_the_account(page, name)
    finally:
        context.close()


def test_a_refused_sign_out_says_so_and_try_again_sends_it_again(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        expect_loaded(page, 'home')
        name = page.evaluate("fetch('/api/me').then(r=>r.json()).then(m=>m.display_name)")
        page.route('**/auth/logout', lambda route: route.fulfill(
            status=503, content_type='application/json', body='{"error":{"code":"UNAVAILABLE","message":"Synthetic outage"}}'))
        start_sign_out(page)
        main = page.locator('#main.boot-failed')
        expect(main.get_by_role('heading', name='Couldn’t sign you out', exact=True)).to_be_visible()
        expect(main.get_by_role('alert')).to_have_text('You may still be signed in on this device.')
        again = main.get_by_role('button', name='Try again', exact=True)
        expect(again).to_be_focused()
        expect(page.get_by_role('heading', name='Sign in', exact=True)).to_have_count(0)
        nothing_of_the_account(page, name)
        page.unroute('**/auth/logout')
        again.click()
        expect(page.get_by_role('heading', name='Sign in', exact=True)).to_be_visible()
        assert page.request.get(live_server + '/api/me').status == 401
    finally:
        context.close()


RESTORED = "window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))"


@pytest.mark.parametrize('how', ['another tab signed out', 'session ended', 'same session'])
def test_a_tab_restored_from_the_page_cache_checks_the_session(browser, live_server, how):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        expect_loaded(page, 'home')
        name = page.evaluate("fetch('/api/me').then(r=>r.json()).then(m=>m.display_name)")
        if how == 'another tab signed out':
            page.evaluate("localStorage.setItem('fmuc.signed-out-at',String(Date.now()))")
        if how == 'session ended':
            context.clear_cookies()
        with page.expect_response(lambda r: r.url.endswith('/api/me')) if how != 'another tab signed out' else nullcontext():
            page.evaluate(RESTORED)
        if how == 'same session':
            expect(page.get_by_role('button', name=ACCOUNT)).to_be_visible()
            expect(page.get_by_role('heading', name='Sign in', exact=True)).to_have_count(0)
        else:
            expect(page.get_by_role('heading', name='Sign in', exact=True)).to_be_visible()
            nothing_of_the_account(page, name)
    finally:
        context.close()

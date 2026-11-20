"""Addresses that name a tab: one this account cannot open, or one that does not exist, says so instead of silently
showing another tab (LIVE-10, LIVE-25)."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('address,said', [
    ('system/audit', 'You can’t open that tab'),            # a tab of the page this role may not open
    ('system/no-such-tab', 'That tab doesn’t exist'),       # a tab the page does not have
    ('configuration/no-such-section', 'That tab doesn’t exist'),
])
def test_inaccessible_tab_address_explains(browser, live_server, h, address, said):
    role = 'admin' if address.startswith('configuration') else 'coordinator'
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page, 'home')
        page.evaluate('a => location.hash = a', address)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', address.split('/')[0])
        expect(page.locator('#toast-tray').get_by_text(said, exact=False)).to_be_visible()
        assert address.split('/')[1] not in page.url  # the address now names what is shown
    finally:
        context.close()


@pytest.mark.parametrize('address', ['__proto__', 'hasOwnProperty', 'constructor', 'toString'])
@pytest.mark.parametrize('first_load', [True, False], ids=['first-load', 'hash-navigation'])
def test_object_member_address_shows_home_without_errors(browser, live_server, h, address, first_load):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        page_errors, console_errors = [], []
        page.on('pageerror', lambda error: page_errors.append(str(error)))
        page.on('console', lambda message: console_errors.append(message.text) if message.type == 'error' else None)
        if first_load:
            page.goto(live_server + '/#' + address)
        else:
            page.evaluate('address => location.hash = address', address)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home')
        expect(page.locator('#toast-tray').get_by_text(
            'That link doesn’t open a page here, so Home is shown.', exact=True)).to_be_visible()
        expect(page).to_have_url(live_server + '/#home')
        assert page_errors == []
        assert console_errors == []
    finally:
        context.close()


@pytest.mark.parametrize('address,target', [('planner', 'schedule'), ('directory', 'people')])
@pytest.mark.parametrize('first_load', [True, False], ids=['first-load', 'hash-navigation'])
def test_function_and_string_aliases_still_open_their_page(browser, live_server, h, address, target, first_load):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page_errors, console_errors = [], []
        page.on('pageerror', lambda error: page_errors.append(str(error)))
        page.on('console', lambda message: console_errors.append(message.text) if message.type == 'error' else None)
        if first_load:
            page.goto(live_server + '/#' + address)
        else:
            page.evaluate('address => location.hash = address', address)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', target)
        assert page.evaluate('location.hash.split(/[/?]/)[0]') == '#' + target
        expect(page.locator('#toast-tray').get_by_text(
            'That link doesn’t open a page here, so Home is shown.', exact=True)).to_have_count(0)
        assert page_errors == []
        assert console_errors == []
    finally:
        context.close()

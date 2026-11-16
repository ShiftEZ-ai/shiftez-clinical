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

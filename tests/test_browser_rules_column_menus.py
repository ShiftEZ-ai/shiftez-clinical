"""the one-row Rules list retires per-column menus from the Rules list; Needs attention keeps shared table menus elsewhere."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import flag, weekly

pytestmark = pytest.mark.browser


def column(page, name):
    """Shared helper retained for Needs-attention's table menus; Rules-list menus are retired below."""
    page.locator('#main thead .dt-sort').filter(has_text=re.compile(f'^{re.escape(name)}')).click()
    menu = page.get_by_role('menu', name=f'{name} column')
    expect(menu).to_be_visible()
    return menu


def test_rules_list_retires_rule_column_menus(browser, live_server, h):
    """The old Rules-list column assertions are intentionally dropped with the table."""
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        expect(main.locator('.rt-scroll')).to_be_visible()
        expect(main.locator('.rt-scroll thead')).to_have_count(0)
        expect(main.locator('[data-key^="column:"]')).to_have_count(0)
        expect(main.get_by_role('button', name='All rules', exact=True)).to_have_attribute('aria-expanded', 'false')
    finally:
        context.close()

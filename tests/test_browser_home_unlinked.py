"""maintenance: Home's "not linked to a staff profile" card is for a staff sign-in without a profile only. An admin holds the
staff role implicitly (apps/api/auth.py) and an auditor has no personal work, so neither sees it; and it is never Home's
first element (Needs you leads)."""
import re

import pytest
from playwright.sync_api import expect

from .governance_support import identity
from .test_browser import browser, live_server, sign_in  # noqa: F401

TITLE = 'Your sign-in isn’t linked to a staff profile'


@pytest.mark.parametrize('roles,shown', [(['admin'], False), (['auditor'], False), (['staff'], True)], ids=['admin', 'auditor', 'staff'])
def test_unlinked_card_only_for_personal_work(browser, live_server, h, roles, shown):
    name = 'unlinked_home_' + roles[0]
    identity(h, name, roles)
    context, page = sign_in(browser, live_server, name)
    try:
        main = page.locator('#main .home-main')
        expect(main).to_be_visible()
        if shown:
            expect(main.locator('.home-unlinked')).to_contain_text(TITLE)
            expect(main.locator(':scope > :first-child')).not_to_have_class(re.compile(r'\bhome-unlinked\b'))
        else:
            expect(main.locator('.home-card').first).to_be_visible()
            expect(page.locator('#main')).not_to_contain_text(TITLE)
    finally:
        context.close()

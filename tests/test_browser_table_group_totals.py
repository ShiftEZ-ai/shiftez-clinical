"""A paged shared table (table_view.ts) counts each group whole: a group heading says how many rows the group has on
every page, and its box selects all of them, not only the 50 rows of the page shown (a rule's lines database, 520
lines in one group)."""
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import catalogue_rule, flag, line, selection

pytestmark = pytest.mark.browser


def test_a_group_on_a_paged_table_counts_and_selects_every_row(browser, live_server, h):
    flag(h)
    start = date(2026, 1, 5)
    catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}}, starts_on=str(start + timedelta(weeks=n)),
                                               ends_on=str(start + timedelta(weeks=n + 1))) for n in range(520)])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules/duties_per_week')
        main = page.locator('#main')
        group = main.locator('tr.dt-group')
        expect(main.locator('tr[data-row]')).to_have_count(50)  # one page
        expect(group).to_have_count(1)
        expect(group.locator('.gt-count')).to_have_text('520')
        group.locator('input.bulk-check').check()
        expect(selection(page).locator('.sel-badge')).to_have_text('520')
        expect(group.locator('input.bulk-check')).to_be_checked()
        # The next page: the same group, the same total, still all selected.
        main.get_by_role('button', name='Next page').click()
        expect(main.locator('.dt-pager')).to_contain_text('Page 2 of 11')
        expect(group.locator('.gt-count')).to_have_text('520')
        expect(group.locator('input.bulk-check')).to_be_checked()
        group.locator('input.bulk-check').uncheck()
        expect(selection(page)).to_have_count(0)
    finally:
        context.close()

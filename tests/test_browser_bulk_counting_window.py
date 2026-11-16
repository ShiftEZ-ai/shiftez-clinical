"""Bulk Change… on saved quota lines edits their Counting window with the line panel's own editor; Preview shows each
line's old → new value, Propose leaves one version waiting, and Undo retires it (rc.17 F1)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_bulk_changes import change_row, errors_of, panel, submit
from .test_browser_rule_catalogue import catalogue_rule, flag, line, selection
from .test_rule_catalogue_api import person


pytestmark = pytest.mark.browser
QUOTA = {'starts_on': '2026-10-01', 'ends_on': '2027-01-01', 'window_alignment': 'fixed', 'window_days': 7}


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_counting_window_changes_in_bulk_with_preview_per_line_and_undo(browser, live_server, h, scheme):
    flag(h)
    lines = [line({'min': {'value': 2, 'level': 'mandatory'}}, who=person(p), settings={'block_quota': dict(QUOTA)})
             for p in h.seed['people'][:2]]
    rule = catalogue_rule(h, 'duties_per_period', lines)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 1000}, color_scheme=scheme)
    errors = errors_of(page)
    try:
        navigate(page, 'rules/duties_per_period')
        for row in page.locator('#main tr[data-row]').all():
            row.locator('input.bulk-check').check()
        selection(page).get_by_role('button', name='Propose a change…').click()
        row = change_row(page, 'Counting window')
        # The line panel's editor: counting dates, the blocks' anchor and Repeats (mixed: each line keeps its own).
        expect(row.get_by_label('Blocks start on', exact=True)).to_be_visible()
        repeat = row.get_by_role('checkbox', name='Repeats while the line is valid')
        assert repeat.evaluate('c => c.indeterminate')
        submit(page)
        expect(panel(page)).to_contain_text('Choose counting dates, repeat or the block anchor.')
        row.get_by_label('From', exact=True).fill('01/11/2026')
        submit(page)
        expect(panel(page).get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
        change = panel(page).locator('.ba-lines')
        expect(change.locator('.ba-line')).to_have_count(2)
        expect(change.locator('.ba-line').first).to_contain_text(re.compile(r'· All clinics: Counting starts \w+ 1 Oct 2026 → \w+ 1 Nov 2026'))
        panel(page).locator('.bulk-foot .button.primary').click()
        expect(panel(page).get_by_role('heading', name='Proposed · waiting for approval')).to_be_visible()
        policies = h.get('admin', '/api/catalogues')['policies']
        [waiting] = [p for p in policies if p.get('status') == 'proposed']
        assert [l['settings']['block_quota'] for l in waiting['definition']['rule']['lines']] == [
            {**QUOTA, 'starts_on': '2026-11-01'}] * 2
        assert next(p for p in policies if p['id'] == rule['id'])['status'] == 'active'
        panel(page).get_by_role('button', name='Undo the whole change').click()
        expect(page.locator('.toast', has_text='Undone')).to_be_visible()
        # Retired versions load through the pageable history endpoint, not the operational catalogue (21e096a0d).
        after = {p['id']: p['status'] for p in h.get('admin', '/api/catalogues')['policies']
                 + h.get('admin', '/api/policies?status=retired')['policies']}
        assert after[waiting['id']] != 'proposed' and after[rule['id']] == 'active', after
        assert not errors, errors
    finally:
        context.close()

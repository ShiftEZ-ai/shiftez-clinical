"""Insights version picker and a person's duties (audit I-14, I-15, I-22, perf PI-9): the version is named once, by its
rota and dates, Superseded only when it is; choosing a version or a breakdown does not read the version list again; and
View duties opens the docked duties panel with formatted days and times."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401
from .test_browser_insights_system import two_versions, source

pytestmark = pytest.mark.browser


def test_versions_are_named_by_rota_and_dates_and_superseded_is_marked(browser, live_server, h):
    one = h.publish(h.shift(h.workspace('Fictional first rota'))['workspace'])
    amended = h.workspace('Fictional amended rota', base=one['publication_id'])
    h.publish(h.shift(amended, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace'])
    pubs = h.get('coordinator', '/api/publications')['publications']
    old = next(p for p in pubs if not p['current'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'insights/workload')
        # Grouped by the month each version starts in (I-15); the shared picker searches long lists.
        assert source(page).locator('optgroup').evaluate_all('gs => gs.map(g => g.label)') == ['Oct 2026']
        labels = source(page).locator('option').all_text_contents()
        assert [l for l in labels if l.endswith(' · Superseded')] == [next(l for l in labels if l.startswith(old['name']))], labels
        assert all(not re.search(r'Version \d|current|—', l) for l in labels), labels
        source(page).select_option(old['id'])
        head = page.locator('#main .panel-head')
        expect(head.locator('.badge')).to_have_text('Superseded')
        # The subtitle no longer repeats the version, its time or zone (I-15, I-22).
        expect(head).not_to_contain_text('as of')
        expect(head).not_to_contain_text('Asia/')
    finally:
        context.close()


def test_choosing_a_version_or_breakdown_does_not_read_the_version_list_again(browser, live_server, h):
    pubs = two_versions(h)
    older = next(p for p in pubs if p['sequence'] == 1)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'insights/workload')
        expect(page.locator('[data-table="workload"]')).to_be_visible()
        lists = []
        page.on('request', lambda r: lists.append(r.url) if r.method == 'GET' and re.search(r'/api/publications(\?|$)', r.url) else None)
        page.get_by_label('Report breakdown', exact=True).select_option('week')
        expect(page.get_by_role('columnheader', name='From', exact=True).first).to_be_visible()
        source(page).select_option(older['id'])
        expect(source(page)).to_have_value(older['id'])
        expect(page.get_by_role('columnheader', name='From', exact=True).first).to_be_visible()
        assert lists == [], lists  # budget: 0 version-list reads for in-page changes (PI-9)
        # Another tab opened within the visit answers the list from the reply this tab kept (api.ts keptReads,
        # c6d9edec1): still no read, until a change can alter it.
        page.get_by_role('tab', name='Exports', exact=True).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-tab', 'exports')
        assert lists == [], lists
    finally:
        context.close()


def test_view_duties_opens_the_docked_duties_panel_with_formatted_rows(browser, live_server, h):
    two_versions(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'insights/workload')
        page.get_by_label('Report breakdown', exact=True).select_option('')
        row = page.locator('[data-table="workload"] tbody tr').first
        name = row.locator('td').first.inner_text().strip()
        row.locator('td').nth(2).click()  # the row itself opens the duties (I-28), as its View duties button does
        panel = page.get_by_role('region', name='Duties · ' + name)
        expect(panel).to_be_visible()
        expect(page.get_by_role('dialog')).to_have_count(0)  # a docked panel, never a modal (I-14)
        expect(panel.locator('thead th')).to_have_text(['Day', 'Time', 'Duty'])
        first = panel.locator('tbody tr').first
        expect(first.locator('td').nth(0)).to_have_text(re.compile(r'^(Sun|Mon|Tue|Wed|Thu|Fri|Sat) \d{1,2} [A-Z][a-z]{2}$'))
        expect(first.locator('td').nth(1)).to_have_text('08:00–12:00')
        # The time stays on one line.
        assert first.locator('td').nth(1).evaluate('n => n.getClientRects().length && n.firstElementChild.getClientRects().length') == 1
        panel.get_by_role('button', name='Close panel').click()
        expect(panel).to_have_count(0)
    finally:
        context.close()

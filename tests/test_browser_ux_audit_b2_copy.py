"""UX audit B2 (web): New draft copies the rota before it by date (any equal-length period, dated choices, no "Match
sessions by"), names itself from its dates, warns about days in no rota with one click to fill them; issue cards say
when a rule's window looks back before the rota. Desktop 1440 (New draft is an operator screen: desktop only)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import PAGE_LOAD_MS, browser, live_server, navigate, new_draft, sign_in, wait_held  # noqa: F401

pytestmark = pytest.mark.browser
TODAY = '2026-10-03T09:00:00+03:00'
DESKTOP = {'width': 1440, 'height': 1000}


def open_new_draft(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    page.clock.set_fixed_time(TODAY)
    navigate(page, 'planner')
    new_draft(page)
    return context, page, page.get_by_role('dialog', name='New rota', exact=True)


def set_day(dialog, label, day):
    field = dialog.get_by_label(label, exact=True)
    field.fill(day)
    field.press('Tab')


def test_copy_previous_preselects_the_rota_before_and_copies_a_fortnight(browser, live_server, h):
    a = h.workspace('Fortnight A', start='2026-10-04', end='2026-10-18')
    h.shift(a, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')
    h.workspace('Fortnight B', start='2026-10-18', end='2026-11-01')
    context, page, dialog = open_new_draft(browser, live_server)
    try:
        source = dialog.get_by_label('Previous rota', exact=True)
        # Starts after the latest rota; that rota is the one to copy, listed with its dates; the name follows the dates.
        expect(dialog.get_by_label('First day', exact=True)).to_have_value('Sun 1 Nov 2026')
        expect(dialog.get_by_label('Rota name', exact=True)).to_have_value('Rota 1–14 Nov 2026')
        dialog.get_by_label('Start from', exact=True).select_option('previous')
        expect(source.locator('option:checked')).to_have_text('Fortnight B · 18–31 Oct 2026 · draft')  # ME-05 (7227a049c): each said with its state
        expect(dialog.get_by_text('Copies sessions day by day; the last day follows its length.')).to_be_visible()
        expect(dialog.get_by_label('Last day', exact=True)).to_have_value('Sat 14 Nov 2026')  # 14 days, as the source
        expect(dialog.get_by_text('Match sessions by')).to_have_count(0)
        # A new first day picks the rota ending before it again.
        set_day(dialog, 'First day', '2026-10-18')
        set_day(dialog, 'Last day', '2026-10-31')
        expect(source).to_have_value(a['id'])
        expect(source.locator('option:checked')).to_have_text('Fortnight A · 4–17 Oct 2026 · draft')
        expect(dialog.get_by_label('Rota name', exact=True)).to_have_value('Rota 18–31 Oct 2026')
        dialog.get_by_label('Run auto-schedule after setup', exact=True).uncheck()
        # The page refresh after creating is held: the draft is already saved, so its message shows meanwhile.
        held = []
        page.route('**/api/me', lambda route: held.append(route))
        try:
            dialog.get_by_role('button', name=re.compile('^Create draft')).click()
            wait_held(page, held)
            expect(page.locator('.toast')).to_contain_text('Draft saved · 1 session created. Nothing published.', timeout=PAGE_LOAD_MS)
        finally:
            # Removing the handler already resumes its held routes in Playwright 1.64.
            page.unroute_all(behavior='ignoreErrors')
        expect(dialog).to_have_count(0)
        created = next(w for w in h.get('coordinator', '/api/workspaces')['workspaces'] if w['name'] == 'Rota 18–31 Oct 2026')
        shifts = h.detail(created)['shifts']
        assert [s['start'][:10] for s in shifts] == ['2026-10-19']  # 5 Oct + 14 days
    finally:
        context.close()


def test_days_in_no_rota_are_named_with_one_click_to_use_them(browser, live_server, h):
    h.workspace('Early October', start='2026-10-04', end='2026-10-18')
    h.workspace('Late October', start='2026-10-20', end='2026-11-01')
    context, page, dialog = open_new_draft(browser, live_server)
    try:
        gap = dialog.locator('.notice').filter(has_text='18–19 Oct 2026 are in no rota.')
        expect(gap).to_be_visible()
        # Dates that leave a day before them in no rota say so too.
        set_day(dialog, 'First day', '2026-11-03')
        set_day(dialog, 'Last day', '2026-11-16')
        expect(dialog.locator('.notice').filter(has_text='1–2 Nov 2026 are in no rota.')).to_be_visible()
        gap.get_by_role('button', name='Use these dates', exact=True).click()
        expect(dialog.get_by_label('First day', exact=True)).to_have_value('Sun 18 Oct 2026')
        expect(dialog.get_by_label('Last day', exact=True)).to_have_value('Mon 19 Oct 2026')
        expect(dialog.get_by_label('Rota name', exact=True)).to_have_value('Rota 18–19 Oct 2026')
        expect(dialog.locator('.notice').filter(has_text='in no rota')).to_have_count(0)
    finally:
        context.close()


def test_a_week_limit_reaching_before_the_rota_says_it_looks_back(browser, live_server, h):
    h.policy({'kind': 'max_duties', 'window': 'week', 'maximum': 1}, scope={'person_id': h.seed['people'][0]})
    w = h.workspace('Look-back week', start='2026-10-05', end='2026-10-12')
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    w = h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        expect(page.locator('#main[data-loaded-view=schedule] .issues-head')).to_be_visible(timeout=20000)
        main = page.locator('#main')
        reason = main.locator('td[data-col=reason]').filter(has_text='From the 1-week look-back before this rota (4–10 Oct 2026)')
        expect(reason).not_to_have_count(0)
    finally:
        context.close()

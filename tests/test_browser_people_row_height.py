"""People × Days, View settings › Row height: Fit content (the default) grows a cell to its duties; Same for every row
keeps each cell one line, a day's further duties one "+n" beside the first chip that lists them (each opens as its chip
does); saved with the view; print always fits content. Fixtures are fictional and made through the API."""
from datetime import date, timedelta
import re

import pytest
from playwright.sync_api import expect

from .legend_guard import assert_legend_matches
from .test_browser import (browser, choose_radio, close_popover, live_server, navigate, pick_view, planner_tool,  # noqa: F401
                           sign_in, view_settings)
from .test_browser_operator_role_coverage import all_roles  # noqa: F401
from .test_browser_schedule import checked, settled, today

pytestmark = pytest.mark.browser
VIEW = '#schedule-view'


@pytest.fixture
def doubled(all_roles):
    """A one-week draft whose first person works two duties today. Returns (draft, person, today, the duties)."""
    h = all_roles
    day = date.fromisoformat(today())
    week_start = h.get('coordinator', '/api/me')['organisation']['week_start']
    first = day - timedelta(days=(day.weekday() - week_start) % 7)
    person = h.seed['people'][0]
    w = h.workspace('Fictional row height rota', first.isoformat(), (first + timedelta(days=7)).isoformat())
    w = h.shift(w, f'{day}T08:00:00+03:00', f'{day}T12:00:00+03:00', people=[person])['workspace']
    w = h.shift(w, f'{day}T13:00:00+03:00', f'{day}T17:00:00+03:00', people=[person])['workspace']
    w = checked(h, w)
    shifts = sorted(h.detail(w)['shifts'], key=lambda s: s['start'])
    assert len(shifts) == 2
    return w, person, day.isoformat(), shifts


def cell(scope, person, day):
    return scope.locator(f'[data-row-person="{person}"] .pg-cell[data-day="{day}"]')


def test_same_row_height_keeps_one_line_and_lists_the_days_duties(browser, live_server, doubled):
    w, person, day, shifts = doubled
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/people/{day}?edit={w['id']}", alert=True)
        expect(page.locator(VIEW)).to_have_attribute('aria-label', 'People view')
        settled(page)
        here = cell(page.locator(VIEW), person, day)
        # Fit content (the default): both duties are chips and the cell grows to hold them.
        expect(here.locator('.pg-chip')).to_have_count(2)
        expect(here.locator('.pg-more')).to_have_count(0)
        panel = view_settings(page.locator('.planner-toolbar'))
        expect(panel.get_by_role('radiogroup', name='Row height', exact=True).get_by_role('radio', name='Fit content', exact=True)) \
            .to_have_attribute('aria-checked', 'true')
        choose_radio(panel, 'Row height', 'Same for every row')
        close_popover(page)
        settled(page)
        # Same for every row: one line, the first duty's chip and "+1" beside it, every cell the same height.
        expect(page.locator(f'{VIEW} .people-grid')).to_have_class(re.compile(r'\buniform\b'))
        expect(here.locator('.pg-chip')).to_have_count(1)
        more = here.get_by_role('button', name=re.compile(r'^1 more duty for '))
        expect(more).to_have_text(re.compile(r'^\+1'))
        heights = page.locator(f'{VIEW} [data-row-person] .pg-cell').evaluate_all('ns => [...new Set(ns.map(n => n.getBoundingClientRect().height))]')
        assert heights == [32], heights
        box = more.bounding_box()
        assert box['height'] >= 24, box
        # The key names it; every mark on screen has its entry.
        expect(page.locator(f'{VIEW} .view-legend li').filter(has_text='More duties')).to_have_count(1)
        assert_legend_matches(page, VIEW, expect_marks=['more'])
        # "+1" lists the day's duties; each opens as its chip does.
        more.click()
        listed = page.get_by_role('dialog', name='All Duties', exact=True)
        expect(listed.locator('.dt-pop-label')).to_have_text('Duties · 2')
        expect(listed.get_by_role('button')).to_have_count(2)
        later = shifts[1]
        listed.get_by_role('button', name=f"{later['type_snapshot']['name']} · 13:00–17:00", exact=True).click()
        session = page.locator('.wk-panel')
        expect(session).to_be_visible()
        expect(session).to_contain_text('13:00')
        page.keyboard.press('Escape')
        # Print always fits content: both chips, no "+n".
        planner_tool(page, 'Export…')
        preview = page.get_by_role('dialog', name='Print or export', exact=True).get_by_role('img', name='Preview of the pages', exact=True)
        expect(preview.locator('.people-grid').first).to_be_visible()
        expect(preview.locator('.people-grid.uniform')).to_have_count(0)
        expect(cell(preview, person, day).first.locator('.pg-chip')).to_have_count(2)
        expect(preview.locator('.pg-more')).to_have_count(0)
    finally:
        context.close()


def test_a_saved_view_keeps_same_row_height(browser, live_server, all_roles, doubled):
    """PlannerViewSettings.row_height round-trips: a saved view's Row height applies when it opens and shows in View settings."""
    w, person, day, _ = doubled
    saved = all_roles.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Fictional one-line rows',
        'definition': {'planner': {'mode': 'person', 'range': 'week', 'row_height': 'same'}}})['view']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/people/{day}?edit={w['id']}", alert=True)
        settled(page)
        expect(page.locator(f'{VIEW} .people-grid.uniform')).to_have_count(0)
        pick_view(page, saved['name'])
        settled(page)
        expect(page.locator(f'{VIEW} .people-grid')).to_have_class(re.compile(r'\buniform\b'))
        expect(cell(page.locator(VIEW), person, day).locator('.pg-chip')).to_have_count(1)
        panel = view_settings(page.locator('.planner-toolbar'))
        expect(panel.get_by_role('radiogroup', name='Row height', exact=True).get_by_role('radio', name='Same for every row', exact=True)) \
            .to_have_attribute('aria-checked', 'true')
        close_popover(page)
    finally:
        context.close()

"""UX audit B2 (web) H3, H4 and H1 on screen: one rota, one set of numbers. Home, the sidebar, the Schedule footer and
Submit for review all read the rota summary (GET /api/workspaces/{id}/summary, packages/domain/rota_summary.py), each
in its own labelled unit (places, sessions, blocking issues); the score shows its uncapped value beside "N blocking";
auto-schedule keeps usual assignees by default and reports who explicit Rebalance takes off and why. Desktop 1440 light and dark
(these are operator screens; phones keep the employee views, see test_browser_home)."""
import re
from datetime import timedelta
from functools import partial

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one
from packages.domain.cp_sat import generate as cp_generate

from .test_browser import auto_setup, wait_run_created, browser, live_server, navigate, open_rota, open_scheduling, sign_in  # noqa: F401
from .test_browser_cp_sat import cp_server  # noqa: F401
from .test_browser_home import local_today, needs
from .test_h1_template_conflicts import approved_leave, assigned_template

pytestmark = pytest.mark.browser
DESKTOP = [pytest.param({'width': 1440, 'height': 1000}, 'light', id='1440-light'),
           pytest.param({'width': 1440, 'height': 1000}, 'dark', id='1440-dark')]


def plural(n, one, many=None):
    return f"{n} {one if n == 1 else many or one + 's'}"


def counted_rota(h):
    """A checked draft starting in three days: a pair clinic half staffed, one empty and one full (places in different
    sessions, so places and sessions differ), with the gaps also blocking issues."""
    typ = h.configure('assignment_types', {'name': 'Pair clinic H3', 'starts_on': '2020-01-01',
                                           'definition': {'requirements': [{'minimum': 2}]}})['record']['id']
    day = local_today() + timedelta(days=3)
    w = h.workspace('Counted rota', day.isoformat(), (day + timedelta(days=7)).isoformat())
    for i, staff in enumerate(([h.seed['people'][0]], [], h.seed['people'][:2])):
        d = (day + timedelta(days=i)).isoformat()
        w = h.shift(w, start=f'{d}T08:00:00+03:00', end=f'{d}T12:00:00+03:00', people=staff, typ=typ)['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    return w, h.get('coordinator', f"/api/workspaces/{w['id']}/summary")


@pytest.mark.parametrize('viewport,theme', DESKTOP)
def test_one_rota_has_the_same_numbers_on_home_sidebar_footer_and_submit(browser, live_server, h, viewport, theme):
    """D9: one count everywhere. The rota's Fix list (the server's one object) gives Home, the footer and Submit the same
    must-fix causes; places stay places (filled of total) and sessions stay sessions (the sidebar's need-staff count)."""
    w, s = counted_rota(h)
    gaps = s['sessions_with_gaps']
    fix = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['summary']
    must = fix['must']
    assert gaps >= 2 and must and s['places_open'] != gaps  # places and sessions really differ here
    assert (fix['places_filled'], fix['places_total']) == (s['places_filled'], s['places_needed'])
    pct = s['places_filled'] * 100 // s['places_needed']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport)
    page.emulate_media(color_scheme=theme)
    try:
        # Home: one row for the draft with its must-fix causes, and the month line with its places filled.
        navigate(page, 'home')
        row = needs(page, 'Counted rota')
        expect(row.locator('.home-count')).to_have_text(str(must))
        expect(row).to_contain_text('Must fix before submitting')
        expect(row).not_to_contain_text('blocking issue')
        expect(page.locator('#main .home-month').filter(has_text=f'Draft · Counted rota · {pct}% filled')).to_have_count(1)
        # Sidebar: the draft sessions with open places, in sessions.
        expect(page.locator('#department-nav [data-route=schedule]')).to_have_accessible_description(
            f"{plural(gaps, 'session')} with open places in drafts")
        # Schedule footer: the same must-fix causes; the score sits beside them, capped and saying so (owner, 7 Nov 2026:
        # shown while anything must be fixed; the must-fix pill gives the one count).
        open_rota(page, w['id'])
        footer = page.locator('footer.app-footer')
        expect(footer.get_by_role('button', name=re.compile(rf'^{must} must fix'))).to_be_visible()
        expect(footer.locator('.score-pill .score-blocking')).to_have_text('· capped')
        # Submit for review: the same causes and the same places, one unit per tile (SR-33).
        page.get_by_role('button', name='Submit for review…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Submit Counted rota for review')
        expect(dialog.locator('.fix-gate')).to_contain_text(re.compile(rf"{must} causes? must be fixed first"))
        expect(dialog.locator('.fix-row')).to_have_count(must)
        expect(dialog.locator('.summary-item').filter(has_text='Places filled')).to_contain_text(f"{fix['places_filled']} of {fix['places_total']}")
        expect(dialog.locator('.summary-item').filter(has_text='Sessions')).to_contain_text(str(len(s['sessions'])))
        expect(dialog.get_by_role('button', name='Submit for review', exact=True)).to_be_disabled()
    finally:
        context.close()


@pytest.mark.parametrize('viewport,theme', DESKTOP)
@pytest.mark.parametrize('mode', ['fill_gaps', 'rebalance'], ids=['default-keep', 'explicit-rebalance'])
def test_auto_schedule_reports_kept_or_replaced_usual_assignees(browser, cp_server, h, viewport, theme, mode):
    series = assigned_template(h)
    w = h.command('coordinator', 'workspace.create', {'name': 'Replaced rota', 'starts_on': '2026-10-04',
                                                      'ends_on': '2026-10-11', 'series_ids': [series['id']]})['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        db.scalar(select(m.Assignment).join(m.Shift).where(m.Shift.workspace_id == w['id'])).fixed = True
    approved_leave(h, h.seed['people'][0])
    context, page = sign_in(browser, cp_server, 'coordinator', viewport=viewport)
    page.emulate_media(color_scheme=theme)
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        auto_setup(panel)
        keep = panel.get_by_label('Keep existing assignments', exact=True)
        expect(keep).to_be_checked()
        if mode == 'rebalance':
            keep.uncheck()
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar')).to_be_visible()
        wait_run_created(page)
        process_one(h.app.state.engine, scheduler=partial(cp_generate, time_budget_seconds=5))
        job = h.get('coordinator', '/api/jobs')['jobs'][0]
        completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        assert completed['effective_options']['mode'] == mode
        review = panel.get_by_role('region', name='Review')
        expect(review).to_be_visible(timeout=20000)
        if mode == 'fill_gaps':
            # Kept: the usual assignee on leave stays (nothing taken off) and what that leaves is still to fix, said
            # in the server's words with its one action (A3).
            assert not completed['result']['removals']
            must = [c for c in completed['result']['fix_list_after']['causes'] if c['level'] == 'must']
            assert must
            still = review.get_by_role('region', name='Still to fix')
            for cause in must:
                expect(still).to_contain_text(cause['sentence'])
            expect(panel).not_to_contain_text('Taken off')
            return
        replaced = completed['result']['summary']['replacements'][0]
        # Replaced: the change is counted by the cause it works on in the panel; who was taken off and why, in the
        # planner's words (never the engine's text), in the run's full list of changes.
        changes = review.get_by_role('region', name='Changes')
        expect(changes.get_by_role('heading')).to_have_text(re.compile(r'^\d+ changes?$'))
        expect(review).not_to_contain_text('Known unavailability')
        panel.get_by_role('button', name='Review latest result', exact=True).click()
        dialog = page.get_by_role('dialog', name='Auto-schedule results')
        expect(dialog).to_contain_text('1 person taken off')
        expect(dialog.get_by_role('row').filter(has_text=replaced['person_name']).first).to_be_visible()
        expect(dialog).not_to_contain_text('Known unavailability')
    finally:
        context.close()
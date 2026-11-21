"""Schedule Timeline layout, card properties (View settings › Properties) and trainee pairing marks in People."""
from tests.historical_staffing import retained_staffing
import os
import re
import pytest
from playwright.sync_api import expect
from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_browser import show_names, browser, live_server, sign_in, navigate, close_popover, view_settings, choose_radio, choose_layout, toolbar  # noqa: F401
from .test_browser_schedule_layouts import busy_day

pytestmark = pytest.mark.browser
SHOTS = os.getenv("VIEW_SHOTS")


def shot(page, name):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, name + ".png"))


def paired(h):
    """A teaching clinic on 4 Oct with a trainee and their supervisor, and on 5 Oct the trainee alone."""
    trainee, supervisor = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Paired teaching clinic', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': False}}})['record']
    at = lambda day: (f'2026-10-0{day}T08:00:00+03:00', f'2026-10-0{day}T12:00:00+03:00')  # noqa: E731
    s, e = at(4)
    w = h.shift(h.workspace('Teaching rota'), s, e, typ=typ['id'], people=[
        {'person_id': trainee, 'start': s, 'end': e, 'participation': 'trainee', 'supervisor_person_id': supervisor},
        {'person_id': supervisor, 'start': s, 'end': e, 'participation': 'supervisor'}])['workspace']
    s, e = at(5)
    return retained_staffing(h, w, s, e, typ=typ['id'], people=[{'person_id': trainee, 'start': s, 'end': e, 'participation': 'trainee'}])['workspace']


# Not staff: a doctor's View settings have no people rows (SW3-C-18, fe_c9 test_a_doctors_view_and_export_settings_…).
@pytest.mark.parametrize('role', ['admin', 'coordinator', 'approver', 'auditor', 'programme_lead'])
# Both widths and both themes for every role (the diagonal): counts, names and no sideways scroll, never a colour.
@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_timeline_has_a_column_per_day_rows_by_group_then_person_and_more(browser, live_server, h, role, width, scheme):
    long_name = 'Alexandra Fictional Department A Training Registrar Morgan'
    long_duty = 'Fictional Family Medicine Specialist Continuity And Complex Follow-up Duty'
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed['people'][0]).display_name = long_name
        db.get(m.AssignmentType, h.seed['type']).name = long_duty
    if role == 'programme_lead':
        identity(h, role, [role])
    w = busy_day(h)
    editing = role in {'admin', 'coordinator'}
    if not editing:
        h.publish(w)
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'schedule/week/2026-10-04' + (f"?edit={w['id']}" if editing else ''))
        panel = view_settings(page.locator('.planner-toolbar'))
        choose_layout(panel, 'Timeline')
        choose_radio(panel, 'Range', 'Month')
        close_popover(page)
        show_names(page, 'full')
        grid = page.get_by_role('region', name='Timeline view').get_by_role('grid', name=re.compile('^Timeline, '))
        expect(grid.locator('.pg-head .pg-day')).to_have_count(31)
        expect(grid.locator('.pg-group')).to_have_count(1)  # one service
        expect(grid.locator('.tl-cover .pg-name')).to_have_text(re.compile('^(Staffed / needed|Sessions)$'))
        # Alex works two sessions with the same duty code: its counted chip lists both.
        more = grid.get_by_role('button', name=f'2 {long_duty} sessions, {long_name}, 4 Oct 2026', exact=True)
        expect(more.locator('b')).to_have_text(re.compile(r'^2×\S+$'))
        expect(more).to_have_accessible_name(f'2 {long_duty} sessions, {long_name}, 4 Oct 2026')
        more.focus()
        expect(more).to_be_focused()
        more.press('Enter')
        pop = page.get_by_role('dialog', name=long_name + ', 4 Oct 2026', exact=True)
        expect(pop.locator('.tl-day li')).to_have_count(2)
        expect(pop.locator('.tl-day li').first).to_contain_text(long_duty)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        close_popover(page)
        expect(more).to_be_focused()
        expect(grid.locator('.pg-hours').filter(has_text=re.compile(r'^\d+ h$')).first).to_be_visible()
        if not editing:
            for label in ('Add session', 'Auto-schedule', 'Publish…', 'Submit for review…'):
                expect(page.get_by_role('button', name=label, exact=True)).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        shot(page, f'timeline-{role}-{width}-{scheme}')
    finally:
        context.close()


def test_timeline_sort_orders_people_within_a_group(browser, live_server, h):
    """Sort applies to Timeline rows: people within a group by name (default), hours (most first) or short code."""
    a, b, c = h.seed['people'][:3]
    w = h.shift(h.workspace('Timeline sort rota'), people=[a, b, c])['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T16:00:00+03:00', people=[c])['workspace']  # Taylor: 12 h
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        bar = page.locator('.planner-toolbar')
        choose_layout(view_settings(bar), 'Timeline')
        close_popover(page)
        grid = page.get_by_role('region', name='Timeline view').get_by_role('grid', name=re.compile('^Timeline, '))
        names = grid.locator('.pg-row:not(.pg-head):not(.pg-group):not(.tl-cover) .pg-name strong')
        expect(names).to_have_text(['Alex Morgan', 'Jordan Lee', 'Taylor Noor'])
        sort = toolbar(bar, 'Sort')
        expect(sort.get_by_role('menuitemradio')).to_have_text(['Name', 'Hours, most first', 'Short code'])
        sort.get_by_role('menuitemradio', name='Hours, most first').click()
        expect(names).to_have_text(['Taylor Noor', 'Alex Morgan', 'Jordan Lee'])
        expect(bar.get_by_role('button', name='Sort: Hours, most first', exact=True)).to_be_visible()
    finally:
        context.close()


def test_card_properties_change_what_each_card_shows(browser, live_server, h):
    w = h.shift(h.workspace('Card rota'), people=[])['workspace']  # an open place
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')['workspace']
    h.lifecycle(w, 'validate')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        bar = page.locator('.planner-toolbar')
        rowhead = page.locator('.wk-rowhead').first
        expect(rowhead.locator('.wk-fact')).to_have_count(2)  # time and place
        panel = view_settings(bar, 'Properties')
        panel.get_by_role('button', name='Show Duty code', exact=True).click()
        panel.get_by_role('button', name='Hide Location', exact=True).click()
        panel.get_by_label('Show Open places as', exact=True).select_option('count')
        shot(page, 'properties')
        close_popover(page)
        expect(rowhead.locator('.wk-fact')).to_have_count(1)
        expect(page.locator('.pchip:not(.gap) .pchip-meta').first).to_have_text(re.compile(r'^[A-Z]{2,3}$'))
        expect(page.locator('.pchip.gap').first).to_have_text('+1')
        # Hide all keeps only the person's label.
        view_settings(bar, 'Properties').get_by_role('button', name='Hide all', exact=True).click()
        close_popover(page)
        expect(page.locator('.pchip.gap')).to_have_count(0)
        expect(rowhead.locator('.wk-fact')).to_have_count(0)
    finally:
        context.close()


def test_people_mark_trainees_with_and_without_their_supervisor(browser, live_server, h):
    w = paired(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/people/2026-10-04?edit={w['id']}")
        grid = page.get_by_role('grid', name='Duties by person and day')
        with_lead = grid.get_by_role('button', name=re.compile(r'Paired teaching clinic · .*4 Oct 2026 .* · with '))
        expect(with_lead.locator('.pr')).to_have_text(re.compile('^↳ '))
        alone = grid.get_by_role('button', name=re.compile(r'Paired teaching clinic · .*5 Oct 2026 .* · without their supervisor'))
        expect(alone).to_have_class(re.compile(r'\balone\b'))
        expect(page.locator('#main .view-legend')).to_contain_text('Trainee without their supervisor')
        shot(page, 'people-pairs')
    finally:
        context.close()

"""People × Days, Layers › Rotations (on by default): a day with no duty and no leave names the person's rotation by its
type's short name, read out whole; protected time is an outlined PT chip under the day's duties; both are keyed, and
turning the layer off clears them. Fixtures are fictional and made through the API."""
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from .legend_guard import assert_legend_matches
from .test_browser import browser, close_popover, live_server, navigate, sign_in, view_settings  # noqa: F401
from .test_browser_operator_role_coverage import all_roles  # noqa: F401
from .test_browser_schedule import checked, settled, today
from .test_residency import REASON, lead_of

pytestmark = pytest.mark.browser
VIEW = '#schedule-view'
TYPE = 'Fictional emergency rotation'


@pytest.fixture
def placed(all_roles):
    """A one-week draft; its first person is on a three-month rotation over it, works on today and has protected time
    that morning. Returns (draft, person, today, another day of the week)."""
    h = all_roles
    day = date.fromisoformat(today())
    week_start = h.get('coordinator', '/api/me')['organisation']['week_start']
    first = day - timedelta(days=(day.weekday() - week_start) % 7)
    other = first if day != first else first + timedelta(days=1)
    begins = (first - timedelta(days=31)).replace(day=1)
    until = (begins + timedelta(days=400)).isoformat()
    person = h.seed['people'][0]
    p = h.configure('programmes', {'name': 'Fictional rotation programme', 'starts_on': min('2026-01-01', begins.isoformat())})['record']
    lead_of(h, p)
    cohort = h.configure('cohorts', {'name': 'Fictional rotation cohort', 'programme_id': p['id'], 'starts_on': begins.isoformat(),
                                     'ends_on': until})['record']
    kind = h.configure('rotation_types', {'name': TYPE, 'short_name': 'EM', 'starts_on': begins.isoformat()})['record']
    h.configure('programme_memberships', {'person_id': person, 'programme_id': p['id'], 'starts_on': begins.isoformat(), 'ends_on': until},
                role='programme_lead')
    h.command('programme_lead', 'rotation.plan', {'cohort_id': cohort['id'], 'person_ids': [person], 'starts_on': begins.isoformat(),
                                                  'blocks': [{'rotation_type_id': kind['id'], 'length': 3, 'unit': 'months'}], 'reason': REASON})
    w = h.workspace('Fictional rotations layer rota', first.isoformat(), (first + timedelta(days=7)).isoformat())
    w = h.shift(w, f'{day}T13:00:00+03:00', f'{day}T17:00:00+03:00', people=[person])['workspace']
    h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'protected_time', 'start': f'{day}T08:00:00+03:00',
                                                     'end': f'{day}T12:00:00+03:00', 'private_reason': 'Fictional study morning'})
    return checked(h, w), person, day.isoformat(), other.isoformat()


def cell(page, person, day):
    return page.locator(f'{VIEW} [data-row-person="{person}"] .pg-cell[data-day="{day}"]')


def test_people_by_days_names_rotations_and_protected_time(browser, live_server, placed):
    w, person, day, other = placed
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/people/{day}?edit={w['id']}", alert=True)
        expect(page.locator(VIEW)).to_have_attribute('aria-label', 'People view')
        settled(page)
        # A day with no duty and no leave: the type's short name, muted and centred, cut with … and read out whole.
        free = cell(page, person, other).locator('.pg-rot')
        expect(free).to_have_text('EM')
        expect(free).to_have_attribute('role', 'img')
        expect(free).to_have_attribute('aria-label', f'Rotation · {TYPE}')
        expect(free).to_have_attribute('data-tip', f'Rotation · {TYPE}')
        assert free.evaluate("n => [getComputedStyle(n).textOverflow, getComputedStyle(n).textAlign]") == ['ellipsis', 'center']
        # A day with a duty names no rotation; its protected time is the outlined PT chip, under the duty.
        worked = cell(page, person, day)
        expect(worked.locator('.pg-chip')).to_have_count(1)
        expect(worked.locator('.pg-rot')).to_have_count(0)
        pt = worked.get_by_role('img', name='Protected time', exact=True)
        expect(pt).to_have_text('PT')
        assert worked.evaluate("""c => { const kids = [...c.children];
            return kids.findIndex(n => n.classList.contains('pg-chip')) < kids.findIndex(n => n.classList.contains('pg-pt')); }""")
        assert pt.evaluate("n => getComputedStyle(n).backgroundColor") == 'rgba(0, 0, 0, 0)', 'PT is outlined, never filled'
        # Both are entries in the view's one key.
        key = page.locator(f'{VIEW} .view-legend')
        expect(key.locator('li').filter(has_text='Protected time')).to_have_count(1)
        expect(key.locator('li').filter(has_text=f'Rotation · {TYPE}')).to_have_count(1)
        assert_legend_matches(page, VIEW, expect_marks=['pt', 'rot:EM'])
        # Layers › Rotations off: neither is drawn.
        layers = view_settings(page.locator('.planner-toolbar'), 'Layers')
        switch = layers.get_by_label('Rotations', exact=True)
        expect(switch).to_be_checked()
        switch.uncheck()
        close_popover(page)
        settled(page)
        expect(page.locator(f'{VIEW} .pg-rot, {VIEW} .pg-pt')).to_have_count(0)
        expect(cell(page, person, day).locator('.pg-chip')).to_have_count(1)
    finally:
        context.close()

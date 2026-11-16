"""Issued-duty fixtures and a bounded fairness drill; collection is preparation only.

Home/Schedule absence controls must be wired against the final frontend commit before
their journeys are added. No invented warning selectors or simulated safety responses.
"""
import re

import pytest
from playwright.sync_api import expect

from .browser_acceptance_support import shot
from .test_browser import browser, live_server, sign_in, navigate, expect_loaded  # noqa: F401
from .test_browser import fix_pill
from .test_browser_fairness_dashboard import october, section
from .test_published_operational import absence, attention, frozen

pytestmark = pytest.mark.browser


def assert_amendment_session(page, action, amendment, h, names):
    assert action['amendment_shift_state'] == 'matched'
    assert action['amendment_shift_id'] != action['shift_id']
    expect(page).to_have_url(re.compile('edit=' + amendment['id']))
    expect(page).to_have_url(re.compile('shift=' + action['amendment_shift_id']))
    expect(page).to_have_url(re.compile('panel=session'))
    expect_loaded(page, 'schedule')
    expect(page.get_by_role('button', name='Switch draft · ' + amendment['name'], exact=True)).to_be_visible()
    panel = page.locator('.wk-panel')
    expect(panel).to_be_visible()
    shift = next(s for s in h.detail(amendment)['shifts'] if s['id'] == action['amendment_shift_id'])
    people = panel.locator('.wk-session .wk-person')
    expect(people).to_have_count(len(shift['participants']))
    for person in shift['participants']:
        expect(people.get_by_text(names[person['person_id']], exact=True)).to_be_visible()
    expect(panel.get_by_role('button', name=re.compile('^Close the other session'))).to_have_count(0)
    expect(page.locator('.toast').filter(has_text=re.compile('^This session (was removed|moved to)'))).to_have_count(0)


@pytest.fixture
def published_absence_case(h, request):
    """Two issued people, then approved own absence and private other-person absence.

    Indirect parameter: leave or pattern. The ordinary distinct-person approval and
    publication helpers own setup; the issued records are captured before either absence.
    """
    kind = getattr(request, 'param', 'leave')
    assert kind in ('leave', 'pattern')
    own, other = h.seed['people'][:2]
    start, end = '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00'
    created = h.shift(h.workspace('Fictional issued absence journey'), start, end, people=[own])
    own_shift = created['shift_id']
    created = h.shift(created['workspace'], start, end, people=[other], location=1)
    w = created['workspace']
    pub = h.publish(w)['publication_id']
    issued = frozen(h, pub)
    assert attention(h) == []
    absence(h, kind)
    h.command('coordinator', 'availability.record', {'person_id': other, 'kind': 'actual_absence',
        'start': '2026-10-06T09:00:00+03:00', 'end': '2026-10-06T10:00:00+03:00',
        'private_reason': 'PRIVATE_OTHER_ISSUED_ABSENCE'})
    team, = attention(h)
    # Cover is the coordinator's work (maintenance, SW3-C-01): staff Home has no row; they read their own warning on Schedule.
    assert team['count'] == 2 and attention(h, 'staff') == []
    assert team['action']['publication_id'] == pub
    assert frozen(h, pub) == issued
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    return {'workspace': w, 'publication_id': pub, 'shift_id': own_shift,
            'other_shift_id': created['shift_id'], 'person_id': own, 'other_person_id': other,
            'names': names, 'issued': issued, 'team_attention': team}


@pytest.mark.parametrize('published_absence_case,width,scheme', [
    pytest.param('leave', 1440, 'light', id='leave-1440-light'),
    pytest.param('pattern', 390, 'dark', id='pattern-390-dark'),
], indirect=['published_absence_case'])
def test_issued_absence_fairness_heading_uses_planned_metric_not_session_rows(
        browser, live_server, h, published_absence_case, width, scheme):
    case = published_absence_case
    name = case['names'][case['person_id']]
    context, page = sign_in(browser, live_server, 'coordinator',
                            viewport={'width': width, 'height': 1000}, color_scheme=scheme)
    try:
        navigate(page, 'insights/fairness')
        october(page)
        page.get_by_label('Measure', exact=True).select_option('hours')
        with page.expect_response(lambda r: '/api/fairness/sessions?' in r.url) as response:
            section(page, 'distribution').get_by_role('button',
                name=re.compile('^' + re.escape(name) + r': 4 h\b')).click()
        assert response.value.ok, response.value.text()
        result = response.value.json()
        assert result['metric_value'] == 4 and result['total'] == 1
        assert result['basis'] == 'published_plan' and result['unit'] == 'h'
        assert result['publications'][0]['publication_id'] == case['publication_id']
        drill = page.get_by_role('region', name='Hours · ' + name, exact=True)
        # The shared panel header owns the aggregate. Its table/subtotals alone cannot prove this.
        expect(drill.locator('.side-head-text > small')).to_contain_text(re.compile(r'^4 h\b'))
        expect(drill.locator('tbody tr')).to_have_count(1)
        shot(page, f'issued-absence-fairness-{width}-{scheme}', full_page=False)
        assert frozen(h, case['publication_id']) == case['issued']
    finally:
        context.close()


@pytest.mark.parametrize('published_absence_case,role,scheme', [
    pytest.param('leave', 'coordinator', 'light', id='leave-coordinator-1440-light'),
    pytest.param('pattern', 'admin', 'dark', id='pattern-admin-1440-dark'),
], indirect=['published_absence_case'])
def test_published_unavailability_home_schedule_amendment_preserves_issued_approval(
        browser, live_server, h, published_absence_case, role, scheme):
    case = published_absence_case
    context, page = sign_in(browser, live_server, role, color_scheme=scheme)
    try:
        navigate(page, 'home')
        row = page.locator('[data-need=published_absence_conflicts]')
        expect(row).to_be_visible()
        expect(row).to_contain_text('2')
        expect(page.locator('.home-clear')).to_have_count(0)
        shot(page, f'published-team-home-1440-{scheme}', full_page=False)
        navigate(page, f"schedule/week/2026-10-06?source={case['publication_id']}")
        warning = fix_pill(page, 'err')
        expect(warning).to_contain_text('2')
        # maintenance line 10: a published rota's footer is its own published status on one line: its health group with the
        # absences as ONE named button, never a draft's check pill (no "Needs a check", "Check again", checking words).
        footer = page.locator('.app-footer')
        expect(footer.get_by_role('group', name='Rota health', exact=True)).to_have_count(1)
        expect(footer.get_by_role('button', name='Published duties overlap unavailability', exact=True)).to_have_count(1)
        expect(warning).to_have_text(re.compile(r'^\s*2\s+unavailable on duty\s*$'))
        expect(footer.locator('.page-status')).not_to_contain_text(
            re.compile(r"Needs a check|Check again|Check stopped|Check hasn't started|Checking|Waiting to check"))
        warning.click()
        pop = page.locator('.issue-pop')
        expect(pop).to_contain_text('Recorded unavailability')
        # B1/D10/D13 (FE-B): a planner replaces the person in the published session's own panel, not through a
        # whole-month amendment; nothing is changed until the replacement is chosen and sent.
        replace = pop.get_by_role('button', name='Replace…', exact=True)
        expect(replace).to_have_count(2)
        expect(pop.get_by_role('button', name='Change published rota…', exact=True)).to_have_count(0)
        shot(page, f'published-team-warning-1440-{scheme}', full_page=False)
        with page.expect_response(lambda r: r.url.endswith('/api/commands')
                                  and (r.request.post_data_json or {}).get('kind') == 'publication.changes.prepare') as prepared_reply:
            replace.first.click()
        assert prepared_reply.value.ok, prepared_reply.value.text()
        expect(page.locator('.dock-panel').get_by_role('heading', name=re.compile('^Replace '))).to_be_visible()
        # Opening Replace prepares the publication's one change list (D12), a draft amendment of it: from now on every
        # entry point continues that amendment. The issued publication is untouched.
        assert frozen(h, case['publication_id']) == case['issued']
        prepared, = attention(h)
        amendment = h.get(role, '/api/workspaces/' + prepared['action']['amendment_workspace_id'])['workspace']
        assert amendment['base_publication_id'] == case['publication_id']
        assert frozen(h, case['publication_id']) == case['issued']
        team, = attention(h)
        assert team['count'] == 2 and team['action']['amendment_workspace_id'] == amendment['id']
        assert team['action']['amendment_shift_state'] == 'matched'
        assert team['action']['amendment_shift_id'] in {s['id'] for s in h.detail(amendment)['shifts']}
        assert not [i for i in h.get(role, '/api/home')['attention'] if i['key'] == 'leave_conflicts']
        navigate(page, 'home')
        row.get_by_role('button', name='Open amendment', exact=True).click()
        assert_amendment_session(page, team['action'], amendment, h, case['names'])
        shot(page, f'published-amendment-home-target-1440-{scheme}', full_page=False)
        # The published warning uses the same canonical destination as Home, for its own selected duty.
        publication = h.get(role, '/api/publications/' + case['publication_id'])
        target = publication['operational']['warnings'][0]['action']
        navigate(page, f"schedule/week/2026-10-06?source={case['publication_id']}")
        fix_pill(page, 'err').click()
        page.locator('.issue-pop').get_by_role('button', name='Open amendment', exact=True).first.click()
        assert_amendment_session(page, target, amendment, h, case['names'])
        shot(page, f'published-amendment-warning-target-1440-{scheme}', full_page=False)
        page.reload()
        assert_amendment_session(page, target, amendment, h, case['names'])
        shot(page, f'published-amendment-reloaded-1440-{scheme}', full_page=False)
        # Paging changes the explicit target; closing removes it so a reload respects the close.
        next_session = page.locator('.wk-panel').get_by_role('button', name='Next session', exact=True)
        direction = next_session if next_session.is_enabled() else page.locator('.wk-panel').get_by_role(
            'button', name='Previous session', exact=True)
        other = next(s['id'] for s in h.detail(amendment)['shifts'] if s['id'] != target['amendment_shift_id'])
        direction.click()
        assert_amendment_session(page, {**target, 'amendment_shift_id': other}, amendment, h, case['names'])
        page.locator('.wk-panel').get_by_role('button', name='Close panel', exact=True).click()
        expect(page.locator('.wk-panel')).to_have_count(0)
        expect(page).not_to_have_url(re.compile(r'[?&](shift|panel)='))
        page.reload()
        expect_loaded(page, 'schedule')
        expect(page.locator('.wk-panel')).to_have_count(0)
        assert frozen(h, case['publication_id']) == case['issued']
        publication = h.get(role, '/api/publications/' + case['publication_id'])
        assert publication['original_validation']['valid']
        assert publication['operational']['conflicting_assignments'] == 2
    finally:
        context.close()


@pytest.mark.parametrize('published_absence_case,scheme', [
    pytest.param('leave', 'light', id='leave-390-light'),
    pytest.param('pattern', 'dark', id='pattern-390-dark'),
], indirect=['published_absence_case'])
def test_staff_own_published_warning_is_private_readonly_on_phone(
        browser, live_server, h, published_absence_case, scheme):
    import json
    case = published_absence_case
    context, page = sign_in(browser, live_server, 'staff', viewport={'width': 390, 'height': 844}, color_scheme=scheme)
    try:
        navigate(page, 'home')
        # SW3-C-01: nothing here is for the doctor to do (cover is the coordinator's): not in Needs you.
        expect(page.locator('.home-needs .home-clear')).to_be_visible()
        expect(page.locator('[data-need=published_absence_conflicts]')).to_have_count(0)
        shot(page, f'published-own-home-390-{scheme}', full_page=False)
        navigate(page, 'schedule/week/2026-10-06')
        # Their own duty is information on the Schedule, not an error to fix.
        warning = page.locator('.app-footer .health-pill.info')
        expect(warning).to_contain_text('1')
        warning.click()
        pop = page.locator('.issue-pop')
        expect(pop).to_contain_text('Recorded unavailability')
        expect(pop.get_by_role('button', name=re.compile('Assign|Change|Edit|amend', re.I))).to_have_count(0)
        expect(page.locator('body')).not_to_contain_text(case['names'][case['other_person_id']])
        expect(page.locator('body')).not_to_contain_text('PRIVATE_OTHER_ISSUED_ABSENCE')
        for path in ['/api/home', '/api/publications/' + case['publication_id'], '/api/schedule/sessions?start=2026-10-04&end=2026-10-11']:
            body = json.dumps(h.get('staff', path))
            assert 'PRIVATE_OTHER_ISSUED_ABSENCE' not in body
            assert case['other_person_id'] not in body
        shot(page, f'published-own-schedule-warning-390-{scheme}', full_page=False)
        assert frozen(h, case['publication_id']) == case['issued']
    finally:
        context.close()

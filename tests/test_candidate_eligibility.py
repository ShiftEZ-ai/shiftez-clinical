"""Boolean candidate audiences retain canonical exclusions without constructing rejected explanations."""
from copy import deepcopy

import pytest

from packages.domain import candidate_evidence as candidates, fairness
from packages.domain.score_normalization import workload_normalization
from tests.test_schedule_score import data, shift


@pytest.mark.parametrize('blocker', [None, 'inactive', 'membership', 'rank', 'clinic', 'leave', 'rotation', 'restriction'])
def test_boolean_candidate_eligibility_matches_complete_evidence(blocker):
    session = shift('candidate', 5, [])
    snapshot = data([session])
    snapshot['policies'] = []
    if blocker == 'inactive':
        snapshot['people']['a']['active'] = False
    elif blocker == 'membership':
        snapshot['memberships'] = [row for row in snapshot['memberships'] if row['person_id'] != 'a']
    elif blocker == 'rank':
        session['type_snapshot']['definition']['allowed_category_ids'] = ['other-rank']
    elif blocker == 'clinic':
        session['service_ids'] = ['clinic']
    elif blocker == 'leave':
        snapshot['availability'] = [{'id': 'leave', 'person_id': 'a', 'start': session['start'], 'end': session['end']}]
    elif blocker == 'rotation':
        snapshot['rotations'] = [{'id': 'rotation', 'person_id': 'a', 'department_available': False,
                                  'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}]
    elif blocker == 'restriction':
        from tests.test_finding_causes import policy
        policy(snapshot, {'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'})
    before = deepcopy(snapshot)
    evidence = candidates.candidate_evidence(snapshot, 'a', session)
    assert candidates.candidate_eligible(snapshot, 'a', session) is evidence['eligible_and_available']
    assert evidence['eligible_and_available'] is (blocker is None)
    assert candidates.candidate_evidence(snapshot, 'a', session) == evidence
    assert snapshot == before


def test_fixed_audiences_skip_outsider_evidence_but_keep_retained_people(monkeypatch):
    sessions = [shift(f'fixed-{day}', day, ['c']) for day in range(1, 29)]
    for session in sessions:
        session['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': ['a']}}
    snapshot = data(sessions)
    before = deepcopy(snapshot)
    calls, evidence = [], candidates.candidate_evidence
    probes, eligible = [], candidates.candidate_eligible

    def counted(context, pid, session):
        calls.append(pid)
        return evidence(context, pid, session)

    def eligibility(context, pid, session):
        probes.append(pid)
        return eligible(context, pid, session)

    monkeypatch.setattr(candidates, 'candidate_evidence', counted)
    monkeypatch.setattr(candidates, 'candidate_eligible', eligibility)
    assert fairness.eligible_people(snapshot, None, list(snapshot['people'])) == ['a', 'c']
    assert workload_normalization(snapshot, 'duties') == (('a',), 28)
    assert calls == ['a', 'c', 'a']
    assert probes == ['a', 'c', 'a']
    assert snapshot == before
    session = sessions[0]
    assert candidates.candidate_eligible(snapshot, 'c', session)
    session['participants'] = []
    assert not candidates.candidate_eligible(snapshot, 'c', session)
    assert evidence(snapshot, 'c', session)['exclusions'] == [{'code': 'USUAL_ASSIGNEES_FIXED', 'series_id': None}]


@pytest.mark.parametrize('day,expected', [(4, False), (5, True)])
@pytest.mark.parametrize('clinic', [None, 'clinic'])
def test_fixed_audiences_keep_dated_unassigned_trainees(day, expected, clinic):
    session = shift('fixed-trainee', day, [])
    session['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': ['a']}}
    session['type_snapshot']['definition']['supervision'] = {
        'trainee_category_ids': ['trainee'], 'capability_id': 'supervisor'}
    snapshot = data([session])
    snapshot['categories']['trainee'] = {}
    snapshot['memberships'] = [row for row in snapshot['memberships'] if row['person_id'] != 'b'] + [
        {'person_id': 'b', 'department_id': 'd', 'category_id': 'trainee', 'starts_on': '2026-10-05'}]
    if clinic:
        session['service_ids'] = [clinic]
        snapshot['eligibilities'] = [{'id': f'eligibility-{pid}', 'person_id': pid, 'service_id': clinic,
                                      'starts_on': '2020-01-01'} for pid in snapshot['people']]
    assert candidates.candidate_eligible(snapshot, 'b', session) is expected
    assert candidates.candidate_evidence(snapshot, 'b', session)['eligible_and_available'] is expected
    assert fairness.eligible_people(snapshot, None, ['b']) == (['b'] if expected else [])
    assert workload_normalization(snapshot, 'duties')[0] == (('a', 'b') if expected else ('a',))


@pytest.mark.parametrize('kind', ['ordinary', 'supervised'])
def test_potential_audience_falls_back_for_unrestricted_or_trainee_opportunities(kind):
    fixed = shift('fixed', 5, [])
    fixed['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': ['a']}}
    other = deepcopy(fixed)
    other['id'] = 'other'
    if kind == 'ordinary':
        other.pop('series_snapshot')
    else:
        other['type_snapshot']['definition']['supervision'] = {
            'trainee_category_ids': ['trainee'], 'capability_id': 'supervisor'}
    context = {'operational': False}
    assert candidates.potential_candidate_people(context, [fixed]) == {'a'}
    assert candidates.potential_candidate_people(context, [fixed, other]) is None
    snapshot = data([fixed, other])
    if kind == 'supervised':
        snapshot['categories']['trainee'] = {}
        snapshot['memberships'][1]['category_id'] = 'trainee'
    assert 'b' in fairness.eligible_people(snapshot, None, list(snapshot['people']))
    assert 'b' in workload_normalization(snapshot, 'duties')[0]


@pytest.mark.parametrize('exclusion', ['inactive', 'membership'])
def test_potential_audience_still_checks_usual_person_qualification(exclusion):
    session = shift('fixed', 5, [])
    session['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': ['a']}}
    snapshot = data([session])
    if exclusion == 'inactive':
        snapshot['people']['a']['active'] = False
    else:
        snapshot['memberships'] = [row for row in snapshot['memberships'] if row['person_id'] != 'a']
    assert candidates.potential_candidate_people({'operational': False}, [session]) == {'a'}
    assert fairness.eligible_people(snapshot, None, list(snapshot['people'])) == []
    assert workload_normalization(snapshot, 'duties')[0] == ()


def test_potential_audience_preserves_recorded_historical_people():
    from tests.test_engine_fairness_review import policy
    session = shift('fixed', 5, [])
    session['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': ['a']}}
    snapshot = data([session])
    goal = policy({'metric': 'duties'})
    identity = fairness.goal_identity(goal)
    snapshot['fairness_history'] = {identity: {'people': {'d': {'value': 1}}}}
    assert fairness.eligible_people(snapshot, goal, ['d']) == ['d']


@pytest.mark.parametrize('matches', [False, True])
def test_potential_audience_keeps_matching_historical_participations(matches):
    from tests.test_engine_fairness_review import policy
    current = shift('fixed', 5, [])
    current['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': ['a']}}
    historical = {**deepcopy(current), **shift('issued', 4, ['d'])}
    snapshot = data([current])
    snapshot['people']['d']['active'] = False
    snapshot['_fair_share_history_shifts'] = [historical]
    goal = policy({'metric': 'duties'})
    goal.update(department_id='d')
    goal['definition']['scope'] = {'person_id': 'd' if matches else 'c'}
    assert fairness.eligible_people(snapshot, goal, ['d']) == (['d'] if matches else [])


def test_ordinary_department_audience_skips_people_without_any_matching_clinic(monkeypatch):
    sessions = [shift(f'ordinary-{n}', 1 + n % 28, []) for n in range(500)]
    for session in sessions:
        session['service_ids'] = ['clinic']
    snapshot = data(sessions)
    people = [f'person-{n:03}' for n in range(150)]
    snapshot['people'] = {pid: {'display_name': pid, 'active': True} for pid in people}
    snapshot['memberships'] = [{'person_id': pid, 'department_id': 'd', 'category_id': 'rank',
                                'starts_on': '2020-01-01'} for pid in people]
    snapshot['eligibilities'] = [{'id': f'eligibility-{pid}', 'person_id': pid, 'service_id': 'clinic',
                                  'starts_on': '2020-01-01'} for pid in people[:2]]
    before = deepcopy(snapshot)
    probes, eligible = [], candidates.candidate_eligible

    def counted(context, pid, session):
        probes.append(pid)
        return eligible(context, pid, session)

    monkeypatch.setattr(candidates, 'candidate_eligible', counted)
    assert workload_normalization(snapshot, 'duties') == (tuple(people[:2]), 500)
    assert probes == people[:2], 'Missing clinic records must not trigger a person-by-session search'
    assert snapshot == before


@pytest.mark.parametrize('scopes,records,expected', [
    ([], {}, ()),
    ([[]], {}, tuple('abcd')),
    ([['clinic']], {}, ()),
    ([['clinic'], []], {}, tuple('abcd')),
    ([['one', 'two']], {'a': ['one'], 'b': ['one', 'two'], 'c': ['two']}, ('b',)),
    ([['one', 'two'], ['three']], {'a': ['one', 'two'], 'b': ['three'], 'c': ['one']}, ('a', 'b')),
])
def test_clinic_audience_bound_preserves_empty_and_multiple_service_choices(scopes, records, expected):
    sessions = [shift(f'services-{n}', 5, []) for n in range(len(scopes))]
    for session, services in zip(sessions, scopes):
        session['service_ids'] = services
    snapshot = data(sessions)
    snapshot['eligibilities'] = [{'id': f'{pid}-{service}', 'person_id': pid, 'service_id': service,
                                  'starts_on': '2020-01-01'} for pid, services in records.items() for service in services]
    assert workload_normalization(snapshot, 'duties') == (expected, len(sessions))


@pytest.mark.parametrize('change', ['future', 'expired', 'membership', 'rank', 'leave'])
def test_clinic_audience_bound_keeps_canonical_dated_qualification(change, monkeypatch):
    session = shift('dated-clinic', 5, [])
    session['service_ids'] = ['clinic']
    snapshot = data([session])
    record = {'id': 'eligibility', 'person_id': 'a', 'service_id': 'clinic', 'starts_on': '2020-01-01'}
    snapshot['eligibilities'] = [record]
    if change == 'future':
        record['starts_on'] = '2026-10-06'
    elif change == 'expired':
        record['ends_on'] = '2026-10-05'
    elif change == 'membership':
        snapshot['memberships'] = [row for row in snapshot['memberships'] if row['person_id'] != 'a']
    elif change == 'rank':
        session['type_snapshot']['definition']['allowed_category_ids'] = ['other-rank']
    else:
        snapshot['availability'] = [{'id': 'leave', 'person_id': 'a', 'start': session['start'], 'end': session['end']}]
    probes, eligible = [], candidates.candidate_eligible

    def counted(context, pid, session):
        probes.append(pid)
        return eligible(context, pid, session)

    monkeypatch.setattr(candidates, 'candidate_eligible', counted)
    assert workload_normalization(snapshot, 'duties')[0] == ()
    assert probes == ['a'], 'Coarse clinic matches still require the canonical eligibility check'

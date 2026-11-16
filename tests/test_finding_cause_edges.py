"""Cause explanations must use the failing interval and offer fixes that can help."""
import copy

import pytest

from packages.contracts.schemas import SeriesDefinition
from packages.domain.validation import evaluate
from packages.domain.time import aware
from .test_finding_causes import (cause_data, cause_snapshot, finding, policy, quota,
                                 type_definition, uid)


@pytest.mark.parametrize('blocker', ['leave', 'membership'])
def test_coverage_candidates_are_qualified_for_the_observed_interval(cause_data, blocker):
    data, _ = cause_data
    shift = data['shifts'][0]
    if blocker == 'leave':
        type_definition(data, requirements=[{'from_minute': 0, 'to_minute': 120, 'minimum': 1}])
        data['availability'] = [{'id': uid('late-leave-' + pid), 'person_id': pid, 'kind': 'approved_leave',
            'start': '2026-10-04T10:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'} for pid in data['people']]
    else:
        shift.update(start='2026-10-04T20:00:00+03:00', end='2026-10-05T04:00:00+03:00')
        type_definition(data, requirements=[{'from_minute': 0, 'to_minute': 240, 'minimum': 1}])
        for membership in data['memberships']:
            membership['ends_on'] = '2026-10-05'
    result = finding(data, 'COVERAGE_GAP')
    assert result['cause']['code'] == 'unfilled'
    assert result['cause']['eligible_people'] == len(data['people'])


def test_coverage_candidate_cache_does_not_share_answers_across_intervals(cause_data):
    data, _ = cause_data
    type_definition(data, requirements=[{'minimum': 1}])
    for person in data['people']:
        data['availability'].append({'id': uid('mid-session-' + person), 'person_id': person,
            'kind': 'approved_leave', 'start': '2026-10-04T10:00:00+03:00',
            'end': '2026-10-04T12:00:00+03:00'})
    # A rule clock makes both intervals explicit even without assigned people.
    policy(data, {'kind': 'minimum_cover', 'minimum': 1},
           {'start_time': '08:00', 'end_time': '10:00'}, 'morning-boundary')
    rows = [row for row in evaluate(data)['findings'] if row['code'] == 'COVERAGE_GAP']
    early = next(row for row in rows if aware(row['start']) == aware('2026-10-04T08:00:00+03:00'))
    late = next(row for row in rows if aware(row['start']) == aware('2026-10-04T10:00:00+03:00'))
    assert (early['cause']['code'], early['cause']['eligible_people']) == ('unfilled', len(data['people']))
    assert (late['cause']['code'], late['cause']['eligible_people']) == ('person_unavailable', 0)
    assert [hint['action'] for hint in late['fix_hints']] == ['open_leave']


@pytest.mark.parametrize('irrelevant', ['department', 'future', 'expired'])
def test_unrelated_or_ineffective_maximum_does_not_remove_quota_places(cause_data, irrelevant):
    data, pid = cause_data
    quota(data, pid)
    ceiling = policy(data, {'kind': 'maximum_participants', 'maximum': 0}, name='irrelevant-ceiling')
    if irrelevant == 'department':
        ceiling['department_id'] = uid('other-department')
    elif irrelevant == 'future':
        ceiling['starts_on'] = '2027-01-01'
    else:
        ceiling['ends_on'] = '2026-10-01'
    result = finding(data)
    assert result['cause']['code'] == 'unfilled'
    assert (result['cause']['places_available'], result['cause']['possible']) == (1, 1)


def test_capacity_hint_does_not_suggest_more_seats_for_a_second_personal_duty(cause_data):
    data, pid = cause_data
    type_definition(data, requirements=[{'minimum': 0, 'maximum': 10}])
    quota(data, pid, amount=2)
    result = finding(data)
    assert result['cause']['code'] == 'capacity_short'
    assert [hint['action'] for hint in result['fix_hints']] == ['add_sessions']


@pytest.mark.parametrize('maximum', [None, 10])
def test_coverage_people_shortage_points_to_people_instead_of_more_session_capacity(cause_data, maximum):
    data, _ = cause_data
    type_definition(data, requirements=[{'minimum': len(data['people']) + 1, 'maximum': maximum}])
    result = finding(data, 'COVERAGE_GAP')
    assert result['cause']['code'] == 'capacity_short'
    assert [hint['action'] for hint in result['fix_hints']] == ['open_eligibility']


def test_operational_coverage_uses_current_type_eligibility(cause_data):
    data, _ = cause_data
    shift = data['shifts'][0]
    original = copy.deepcopy(shift['type_snapshot']['definition'])
    type_definition(data, requirements=[{'minimum': 1}], allowed_category_ids=[uid('not-a-held-rank')])
    shift['type_snapshot']['definition'] = original
    data['operational'] = True
    result = finding(data, 'COVERAGE_GAP')
    assert result['cause']['code'] == 'no_eligible_people'
    assert result['cause']['eligible_people'] == 0


def test_explanations_do_not_turn_a_relaxed_coverage_gap_into_a_new_problem(cause_data):
    from packages.domain.feasibility import compare_findings

    data, _ = cause_data
    type_definition(data, requirements=[{'minimum': 2}])
    before = evaluate(data)
    type_definition(data, requirements=[{'minimum': 1}])
    after = evaluate(data)
    phase = lambda checked: {'status': 'requirements_conflict', 'findings': checked['findings'],
                             'diagnostics': [], 'candidate_evidence': []}
    relaxed = compare_findings(phase(before), phase(after))
    assert relaxed['no_new_problems'] and relaxed['comparison_complete']
    assert relaxed['unchanged_existing_count'] == 1 and relaxed['new_or_worse_findings'] == []
    assert not compare_findings(phase(after), phase(before))['no_new_problems']


@pytest.mark.parametrize('change', ['explanation', 'requirement'])
def test_catalogue_impact_ignores_only_explanation_metadata_in_nested_findings(cause_data, monkeypatch, change):
    from apps.api.services import catalogue_preview

    data, _ = cause_data
    before = evaluate(data)
    child = copy.deepcopy(before['findings'][0])
    child.update(code='CAPABILITY_GAP', detail={'required': 2, 'available': 0, 'capability_id': uid('capability')})
    before['findings'][0]['detail']['derived_findings'] = [child]
    after = copy.deepcopy(before)
    if change == 'explanation':
        after['findings'][0]['cause']['possible'] = 1
        nested = after['findings'][0]['detail']['derived_findings'][0]
        nested['cause']['possible'] = 1
        nested['detail'].update(display='0 of 2 (prorated: 1 of 5 days)', proration={'working_days': 1})
        nested['fix_hints'][0]['prefill']['person_id'] = uid('display-person')
    else:
        after['findings'][0]['detail']['derived_findings'][0]['detail']['required'] = 3
    monkeypatch.setattr(catalogue_preview, 'evaluate', lambda snapshot, **kwargs: before if snapshot == 'before' else after)
    result = catalogue_preview.evaluate_preview({'response': {}, 'workspaces': [{
        'workspace_id': uid('preview-workspace'), 'name': 'Synthetic impact', 'status': 'draft',
        'before': 'before', 'after': 'after', 'saved': None}]})['impacts'][0]
    assert bool(result['introduced']) is (change == 'requirement')
    assert bool(result['resolved']) is (change == 'requirement')


def test_summary_retains_cause_hidden_under_an_assigned_person_blocker(cause_data):
    from packages.domain.rota_summary import rota_summary

    data, _ = cause_data
    shift = data['shifts'][0]
    shift['participants'] = [{'id': uid('blocked-assignment-' + pid), 'person_id': pid,
        'start': shift['start'], 'end': shift['end'], 'participation': 'staff', 'overtime': False}
        for pid in data['people']]
    data['availability'] = [{'id': uid('full-leave-' + pid), 'person_id': pid, 'kind': 'approved_leave',
        'start': shift['start'], 'end': shift['end']} for pid in data['people']]
    checked = evaluate(data)
    assert not any(row['code'] == 'COVERAGE_GAP' for row in checked['findings'])
    summary = rota_summary(checked)
    group = next(group for group in summary['cause_groups'] if group['code'] == 'person_unavailable')
    assert group['count'] == 1
    assert set(group['finding_keys']) <= {row['key'] for row in summary['findings']}
    assert summary['issues']['blocking'] == checked['error_count']


@pytest.mark.parametrize('fixed', [None, False, True])
def test_matching_template_keeps_its_usual_assignee_condition_context(cause_data, monkeypatch, fixed):
    from packages.domain import finding_causes

    data, pid = cause_data
    shift = data['shifts'][0]
    quota(data, pid, scope={'conditions': {'conditions': [{'field': 'usual_assignee_ids', 'values': [pid]}]}},
          assignment_type_ids=[shift['assignment_type_id']])
    definition = SeriesDefinition(anchor='2026-09-06', service_ids=shift['service_ids'],
        default_person_ids=[pid], usual_assignees_fixed=bool(fixed),
        days=[{'weekday': 6, 'start_time': '08:00', 'end_time': '12:00'}]).model_dump(mode='json')
    if fixed is None:
        definition.pop('usual_assignees_fixed')
    series_id = uid('usual-assignee-series')
    data['recurring_series'] = [{'id': series_id, 'name': 'Matching usual physicians', 'active': True,
        'department_id': data['workspace']['department_id'], 'assignment_type_id': shift['assignment_type_id'],
        'location_id': shift['location_id'], 'revision': 1, 'starts_on': '2026-09-01', 'ends_on': '2026-10-01',
        'definition': definition}]
    source, observed, expand = copy.deepcopy(data['recurring_series']), [], finding_causes.expand

    def expanded(parsed, *args):
        observed.append(parsed.usual_assignees_fixed)
        return expand(parsed, *args)

    monkeypatch.setattr(finding_causes, 'expand', expanded)
    data['shifts'], data['focus_ids'] = [], []
    result = finding(data)
    assert result['cause']['code'] == 'no_candidate_sessions'
    assert [(row['id'], row['action']) for row in result['fix_hints'][0]['matching_series']] == [(series_id, 'extend')]
    assert observed and all(value is bool(fixed) for value in observed)
    assert data['recurring_series'] == source


def test_lead_turn_quota_does_not_claim_an_unqualified_person_can_lead(cause_data):
    data, pid = cause_data
    capability = next(iter(data['capabilities']))
    type_definition(data, lead={'minimum': 1, 'capability_ids': [capability]})
    data['grants'] = [row for row in data['grants'] if row['person_id'] != pid]
    quota(data, pid, metric='lead_turns')
    result = finding(data)
    assert result['cause']['code'] == 'no_eligible_people'
    assert result['cause']['eligible_people'] == 0 and result['cause']['possible'] == 0
    assert [hint['action'] for hint in result['fix_hints']] == ['open_eligibility']


@pytest.mark.parametrize('blocker', ['leave', 'rotation'])
def test_unavailable_coverage_pools_share_one_scan_per_interval(cause_data, monkeypatch, blocker):
    from packages.domain import validation

    data, _ = cause_data
    type_definition(data, requirements=[{'minimum': 1, 'maximum': 2}])
    shift = data['shifts'][0]
    data['shifts'] = [{**copy.deepcopy(shift), 'id': uid(f'empty-pool-{blocker}-{n}'),
        **({'start': '2026-10-05T08:00:00+03:00', 'end': '2026-10-05T12:00:00+03:00'} if n == 7 else {})}
        for n in range(8)]
    data['focus_ids'] = [row['id'] for row in data['shifts']]
    if blocker == 'leave':
        data['availability'] = [{'id': uid('empty-pool-leave-' + pid), 'person_id': pid,
            'kind': 'approved_leave', 'start': '2026-10-04T00:00:00+03:00',
            'end': '2026-10-06T00:00:00+03:00'} for pid in data['people']]
    else:
        data['rotations'] = [{'id': uid('empty-pool-rotation-' + pid), 'person_id': pid,
            'department_id': data['workspace']['department_id'], 'starts_on': '2026-10-04',
            'ends_on': '2026-10-06', 'department_available': False} for pid in data['people']]
    scans, synthetic_participants = [], []
    available = validation.available_for_coverage
    participation = validation.participation_for

    def counted_available(snapshot, pid, a, b):
        scans.append((pid, a, b))
        return available(snapshot, pid, a, b)

    def counted_participation(*args):
        synthetic_participants.append(args[1])
        return participation(*args)

    monkeypatch.setattr(validation, 'available_for_coverage', counted_available)
    monkeypatch.setattr(validation, 'participation_for', counted_participation)
    rows = [row for row in evaluate(data)['findings'] if row['code'] == 'COVERAGE_GAP']
    assert len(rows) == len(data['shifts'])
    assert len(scans) == len(set(scans)) == 2 * len(data['people'])
    assert synthetic_participants == []
    assert all(row['cause'] == {'code': 'person_unavailable', 'sessions_found': 1,
        'eligible_people': 0, 'places_available': 2, 'days_in_window': 1, 'days_available': None,
        'required': 1, 'possible': 0, 'actual': 0, 'eligible_people_basis': 'potential_cover_known_records',
        'basis': 'known_records_not_solver_proof',
        'population_people': len(data['people']), 'qualified_people': len(data['people'])} for row in rows)
    assert all([hint['action'] for hint in row['fix_hints']] ==
               (['open_leave'] if blocker == 'leave' else ['open_rotation']) for row in rows)
    assert all(row['fix_hints'][0]['prefill']['shift_ids'] == [row['shift_id']] for row in rows)


def test_empty_coverage_pool_respects_rotation_boundary_and_fresh_evaluation(cause_data):
    data, _ = cause_data
    data['_coverage_availability_cache'] = {}
    type_definition(data, requirements=[{'minimum': 1}])
    first = data['shifts'][0]
    second = {**copy.deepcopy(first), 'id': uid('after-away-boundary'),
              'start': '2026-10-05T08:00:00+03:00', 'end': '2026-10-05T12:00:00+03:00'}
    data['shifts'].append(second)
    data['focus_ids'].append(second['id'])
    data['rotations'] = [{'id': uid('boundary-away-' + pid), 'person_id': pid,
        'department_id': data['workspace']['department_id'], 'starts_on': '2026-10-04',
        'ends_on': '2026-10-05', 'department_available': False} for pid in data['people']]
    original = copy.deepcopy(data)
    before = {row['shift_id']: row['cause'] for row in evaluate(data)['findings'] if row['code'] == 'COVERAGE_GAP'}
    assert data == original
    assert (before[first['id']]['code'], before[first['id']]['eligible_people']) == ('person_unavailable', 0)
    assert (before[second['id']]['code'], before[second['id']]['eligible_people']) == ('unfilled', len(data['people']))
    for rotation in data['rotations']:
        rotation['department_available'] = True
    after = [row['cause'] for row in evaluate(data)['findings'] if row['code'] == 'COVERAGE_GAP']
    assert len(after) == 2 and all(cause['code'] == 'unfilled' and
        cause['eligible_people'] == len(data['people']) for cause in after)


def test_available_coverage_pool_keeps_unavailable_participant_condition_context(cause_data, monkeypatch):
    from packages.domain import validation

    data, pid = cause_data
    type_definition(data, requirements=[{'minimum': 1}])
    shift = data['shifts'][0]
    data['availability'] = [{'id': uid('context-leave-' + other), 'person_id': other,
        'kind': 'approved_leave', 'start': shift['start'], 'end': shift['end']}
        for other in data['people'] if other != pid]
    pools = []
    original = validation.rule_coverage_people

    def contextual(snapshot, peer, a, b, **kwargs):
        if peer['participants']:
            pools.append({person['person_id'] for person in peer['participants']})
        return original(snapshot, peer, a, b, **kwargs)

    monkeypatch.setattr(validation, 'rule_coverage_people', contextual)
    result = finding(data, 'COVERAGE_GAP')
    assert result['cause']['code'] == 'unfilled' and result['cause']['eligible_people'] == 1
    assert pools == [set(data['people'])]


@pytest.mark.parametrize('locked', [False, True])
def test_unfilled_session_fix_uses_its_lock_fact(cause_data, locked):
    from packages.domain.rota_summary import fix_list_context, rota_summary

    data, _ = cause_data
    type_definition(data, requirements=[{'minimum': 1}])
    data['shifts'][0]['locked'] = locked
    original = copy.deepcopy(data)
    checked = {**evaluate(data), 'fix_list_context': fix_list_context(data)}
    cause, = rota_summary(checked)['fix_list']['causes']
    assert cause['action']['kind'] == ('choose_cover' if locked else 'assign')
    assert cause['action']['scope']['shift_ids'] == data['focus_ids']
    assert cause['safety'] and not cause['acknowledgeable']
    historical = copy.deepcopy(checked)
    for session in historical['fix_list_context']['sessions'].values():
        session.pop('locked')
    retained = copy.deepcopy(historical)
    assert rota_summary(historical, data['shifts'])['fix_list']['causes'][0]['action'] == cause['action']
    assert historical == retained
    assert data == original


def test_skill_shortage_fix_names_the_credentialer_without_granting_authority(cause_data):
    from packages.domain.rota_summary import fix_list_context, rota_summary

    data, _ = cause_data
    capability = next(iter(data['capabilities']))
    type_definition(data, requirements=[{'minimum': 1, 'capabilities': [
        {'capability_id': capability, 'minimum': 1}]}])
    data['grants'] = []
    original = copy.deepcopy(data)
    checked = {**evaluate(data), 'fix_list_context': fix_list_context(data)}
    cause, = rota_summary(checked)['fix_list']['causes']
    assert cause['action']['kind'] == 'open_eligibility'
    assert 'A credentialer grants the required skill' in cause['meta']
    assert any(row['code'] == 'CAPABILITY_GAP' for row in cause['evidence'])
    assert cause['safety'] and not cause['acknowledgeable']
    assert data == original





@pytest.mark.parametrize('kind,expected,qualified_count,action', [
    ('empty', 'empty_population', 0, 'open_rotation'),
    ('unqualified', 'no_eligible_people', 0, 'open_eligibility'),
])
def test_coverage_population_and_qualification_remain_distinct(cause_data, kind, expected, qualified_count, action):
    data, _ = cause_data
    type_definition(data, requirements=[{'minimum': 1}])
    if kind == 'empty':
        policy(data, {'kind': 'minimum_cover', 'minimum': 1}, {'person_id': uid('outside-population')}, 'empty-population')
    else:
        data['eligibilities'] = []
    original = copy.deepcopy(data)
    result = finding(data, 'SCOPED_COVERAGE_GAP' if kind == 'empty' else 'COVERAGE_GAP')
    assert result['cause']['code'] == expected
    assert result['cause']['eligible_people'] == result['cause']['possible'] == qualified_count
    assert result['cause']['population_people'] == (0 if kind == 'empty' else len(data['people']))
    assert result['cause']['qualified_people'] == qualified_count
    assert [hint['action'] for hint in result['fix_hints']] == [action]
    assert data == original

@pytest.mark.parametrize('blocker,actions', [
    ('leave', ['open_leave']), ('rotation', ['open_rotation']),
    ('both', ['open_leave', 'open_rotation']), ('split', ['open_leave', 'open_rotation']),
    ('leave-with-unqualified-away', ['open_leave']),
])
def test_coverage_unavailability_actions_use_only_qualified_interval_facts(cause_data, blocker, actions):
    data, _ = cause_data
    type_definition(data, requirements=[{'minimum': 1}])
    shift = data['shifts'][0]
    people = list(data['people'])
    for n, pid in enumerate(people):
        leave = blocker in {'leave', 'both'} or blocker == 'split' and n == 0 or blocker == 'leave-with-unqualified-away' and n > 0
        away = blocker in {'rotation', 'both'} or blocker == 'split' and n > 0 or blocker == 'leave-with-unqualified-away' and n == 0
        if leave:
            data['availability'].append({'id': uid('action-leave-' + pid), 'person_id': pid,
                'kind': 'approved_leave', 'start': shift['start'], 'end': shift['end'],
                'reason': 'PRIVATE_REASON_NOT_FOR_CAUSES', 'category': 'PRIVATE_CATEGORY_NOT_FOR_CAUSES'})
        if away:
            data['rotations'].append({'id': uid('action-away-' + pid), 'person_id': pid,
                'department_id': data['workspace']['department_id'], 'starts_on': '2026-10-04',
                'ends_on': '2026-10-05', 'department_available': False})
    if blocker == 'leave-with-unqualified-away':
        data['eligibilities'] = [r for r in data['eligibilities'] if r['person_id'] != people[0]]
    original = copy.deepcopy(data)
    result = finding(data, 'COVERAGE_GAP')
    assert result['cause']['code'] == 'person_unavailable'
    assert result['cause']['eligible_people'] == result['cause']['possible'] == 0
    assert result['cause']['population_people'] == len(people)
    assert result['cause']['qualified_people'] == len(people) - (blocker == 'leave-with-unqualified-away')
    assert [hint['action'] for hint in result['fix_hints']] == actions
    assert all(hint['prefill']['shift_ids'] == [shift['id']] for hint in result['fix_hints'])
    assert result['severity'] == 'error' and result['shift_id'] == shift['id']
    assert not any('PRIVATE_' in str(value) for value in result.values())
    assert data == original


def test_coverage_absence_actions_reuse_records_once_per_person_interval_and_refresh(cause_data, monkeypatch):
    from packages.domain import candidate_evidence

    data, _ = cause_data
    type_definition(data, requirements=[{'minimum': 1}])
    shift = data['shifts'][0]
    data['shifts'] = [{**copy.deepcopy(shift), 'id': uid(f'absence-record-peer-{n}'),
        **({'start': '2026-10-05T08:00:00+03:00', 'end': '2026-10-05T12:00:00+03:00'} if n == 7 else {})}
        for n in range(8)]
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    data['rotations'] = [{'id': uid('record-away-' + pid), 'person_id': pid,
        'department_id': data['workspace']['department_id'], 'starts_on': '2026-10-04',
        'ends_on': '2026-10-06', 'department_available': False} for pid in data['people']]
    scans, interval = [], []
    original_rows, original_absence = candidate_evidence.person_rows, candidate_evidence._absence_exclusions

    def absence(snapshot, pid, a, b):
        interval[:] = [a, b]
        return original_absence(snapshot, pid, a, b)

    def rows(snapshot, name, pid):
        scans.append((name, pid, *interval))
        return original_rows(snapshot, name, pid)

    monkeypatch.setattr(candidate_evidence, '_absence_exclusions', absence)
    monkeypatch.setattr(candidate_evidence, 'person_rows', rows)
    before = copy.deepcopy(data)
    checked = [r for r in evaluate(data)['findings'] if r['code'] == 'COVERAGE_GAP']
    assert len(checked) == 8 and data == before
    assert all(r['cause']['code'] == 'person_unavailable' and
               [hint['action'] for hint in r['fix_hints']] == ['open_rotation'] for r in checked)
    assert len(scans) == len(set(scans)) <= 4 * len(data['people'])
    changed = copy.deepcopy(data)
    for rotation in changed['rotations']:
        rotation['department_available'] = True
    scans.clear()
    fresh = [r for r in evaluate(changed)['findings'] if r['code'] == 'COVERAGE_GAP']
    assert len(fresh) == 8 and all(r['cause']['code'] == 'unfilled' and
        r['cause']['eligible_people'] == len(data['people']) for r in fresh)
    assert len(scans) == len(set(scans)) <= 4 * len(data['people'])


def test_coverage_facts_do_not_leak_between_evaluations(cause_data):
    data, _ = cause_data
    type_definition(data, requirements=[{'minimum': 1}])
    data['rotations'] = [{'id': uid('evaluation-away-' + pid), 'person_id': pid,
        'department_id': data['workspace']['department_id'], 'starts_on': '2026-10-04',
        'ends_on': '2026-10-06', 'department_available': False} for pid in data['people']]
    first = finding(data, 'COVERAGE_GAP')
    assert first['cause']['code'] == 'person_unavailable'
    assert [hint['action'] for hint in first['fix_hints']] == ['open_rotation']
    data['rotations'] = []
    second = finding(data, 'COVERAGE_GAP')
    assert second['cause']['code'] == 'unfilled'
    assert second['cause']['eligible_people'] == len(data['people'])
    assert all(hint['action'] != 'open_rotation' for hint in second['fix_hints'])


@pytest.mark.parametrize('starts_on,ends_on,blocked', [
    ('2026-10-05', '2026-10-07', True),
    ('2026-10-01', '2026-10-05', True),
    ('2026-10-01', '2026-10-04', False),
])
def test_coverage_rotation_facts_follow_local_midnight_boundaries(cause_data, starts_on, ends_on, blocked):
    from packages.domain.finding_causes import FindingCauses
    from packages.domain.validation import available_for_coverage

    data, _ = cause_data
    type_definition(data, requirements=[{'minimum': 1}])
    shift = data['shifts'][0]
    shift.update(start='2026-10-04T19:00:00+00:00', end='2026-10-04T23:00:00+00:00')
    data['rotations'] = [{'id': uid('boundary-fact-' + pid), 'person_id': pid,
        'department_id': data['workspace']['department_id'], 'starts_on': starts_on,
        'ends_on': ends_on, 'department_available': False} for pid in data['people']]
    cache = {}
    snapshot = {**data, '_coverage_availability_cache': cache}
    start, end = aware(shift['start']), aware(shift['end'])
    for pid in data['people']:
        assert available_for_coverage(snapshot, pid, start, end) is (not blocked)
        assert bool(cache[(pid, start, end)]) is blocked
    result = FindingCauses(data).coverage('COVERAGE_GAP', shift['id'], start, end, {'required': 1})
    assert (result['cause']['code'], result['cause']['eligible_people']) == (
        ('person_unavailable', 0) if blocked else ('unfilled', len(data['people'])))
    assert ([hint['action'] for hint in result['fix_hints']] == ['open_rotation']) is blocked

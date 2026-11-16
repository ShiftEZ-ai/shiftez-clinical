"""Per-filter buckets reach the existing validator, quota evaluator and both scheduling engines."""
import copy
import csv
import io
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import fairness as fairness_service
from packages.contracts.schemas import PolicyDefinition
from packages.domain.cp_sat import generate as solve
from packages.domain.solver_provenance import policy_id
from packages.domain.generation import generate as greedy
from packages.domain.fairness import balance_goals, goal_identity
from packages.domain.quotas import quota_csv, quota_identity
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate


def _line(field, values, count=None):
    return {'field': field, 'op': 'any', 'values': list(values), **({'count': count} if count else {})}


def _policy(data, rule, scope, *, severity='mandatory'):
    return {'id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'count-consumer-rule')), 'name': 'Count fixture',
        'kind': rule['kind'], 'revision': 1, 'status': 'active', 'active': True,
        'department_id': data['workspace']['department_id'], 'starts_on': '2020-01-01', 'ends_on': None,
        'definition': PolicyDefinition.model_validate({'scope': scope, 'rule': rule, 'severity': severity}).model_dump(mode='json')}


def _world(h, *, assigned=True):
    w = h.shift(h.workspace(), people=[h.seed['people'][0]] if assigned else [])['workspace']
    return h.snapshot(w)


def _seven(data):
    """A-only x3, B-only x3, and one duty in both clinics: together 7, each 4/4."""
    first = data['shifts'][0]
    clinics = list(data['services'])[:2]
    for definition in (first['type_snapshot']['definition'], data['types'][first['assignment_type_id']]['definition']):
        definition.update(allow_shared_services=True, shared_duty_reason='Synthetic shared clinic duty')
    data['shifts'] = []
    for n in range(7):
        shift = copy.deepcopy(first)
        shift.update(id=f'count-duty-{n}', start=f'2026-10-{4+n:02}T08:00:00+03:00',
                     end=f'2026-10-{4+n:02}T12:00:00+03:00', locked=True,
                     service_ids=clinics[:1] if n < 3 else clinics[1:] if n < 6 else clinics)
        for p in shift['participants']:
            p.update(id=f'count-participant-{n}', shift_id=shift['id'], start=shift['start'], end=shift['end'])
        data['shifts'].append(shift)
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    return clinics


def _count_scope(clinics, count):
    return {'conditions': {'match': 'all', 'conditions': [_line('service_ids', clinics, count)]}}


@pytest.mark.parametrize('kind,limit,actual', [('max_duties', 6, 7), ('max_hours', 24, 28)])
def test_workload_limits_apply_once_to_every_matching_bucket(h, kind, limit, actual):
    data = _world(h)
    clinics = _seven(data)
    rule = {'kind': kind, 'window': 'week', 'maximum' if kind == 'max_duties' else 'hours': limit}
    data['policies'] = [_policy(data, rule, _count_scope(clinics, 'together'))]
    together = [f for f in evaluate(data)['findings'] if f['rule_id'] == data['policies'][0]['id']]
    assert len(together) == 1 and together[0]['detail']['actual'] == actual
    assert 'count_bucket' not in together[0]['detail']
    data['policies'] = [_policy(data, rule, _count_scope(clinics, 'each'))]
    assert not [f for f in evaluate(data)['findings'] if f['rule_id'] == data['policies'][0]['id']]


def _quota(data, clinics, count, *, comparison='maximum', amount=6):
    scope = {**_count_scope(clinics, count), 'person_id': next(iter(data['people']))}
    rule = {'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
            'metric': 'duties', 'comparison': comparison, 'amount': amount}
    data['policies'] = [_policy(data, rule, scope)]
    data['quota_context'] = {'complete': True, 'start': '2026-10-04T00:00:00+03:00',
                             'end': '2026-10-11T00:00:00+03:00', 'publications': []}


def test_quota_rows_count_a_shared_duty_once_in_each_bucket_and_export_the_keys(h):
    data = _world(h)
    clinics = _seven(data)
    _quota(data, clinics, 'together')
    together = evaluate(data)['quota_report']
    assert [(r['actual'], r['status']) for r in together['rows']] == [(7, 'above_maximum')]
    assert 'count_bucket' not in next(csv.DictReader(io.StringIO(quota_csv(together, 'fixture', 1))))
    _quota(data, clinics, 'each')
    each = evaluate(data)['quota_report']
    assert [(r['actual'], r['status']) for r in each['rows']] == [(4, 'met'), (4, 'met')]
    assert len({quota_identity(row) for row in each['rows']}) == 2
    assert all('count_bucket' in row for row in csv.DictReader(io.StringIO(quota_csv(each, 'fixture', 1))))
    assert 'count_bucket' not in str(each['rows'][0]['scope'])


def _ranks(data, h):
    a, b, c = h.seed['people']
    ranks = [h.seed['category'], h.seed['trainee_category']]
    for row in data['memberships']:
        row['category_id'] = ranks[1] if row['person_id'] == c else ranks[0]
    scope = {'conditions': {'match': 'all', 'conditions': [_line('category_ids', ranks, 'each')]}}
    return (a, b, c), ranks, scope


@pytest.mark.parametrize('kind', ['minimum_cover', 'maximum_participants'])
def test_staffing_reports_only_the_broken_rank_bucket(h, kind):
    data = _world(h)
    (a, b, _), ranks, scope = _ranks(data, h)
    shift = data['shifts'][0]
    shift['participants'] = [dict(shift['participants'][0], id='staff-' + pid, person_id=pid) for pid in (a, b)]
    data['policies'] = [_policy(data, {'kind': kind, 'minimum' if kind == 'minimum_cover' else 'maximum': 1}, scope)]
    findings = [f for f in evaluate(data)['findings'] if f['rule_id'] == data['policies'][0]['id']]
    assert len(findings) == 1
    assert findings[0]['detail']['available' if kind == 'minimum_cover' else 'scheduled'] == (0 if kind == 'minimum_cover' else 2)
    assert findings[0]['detail']['count_bucket'][-1][1] == ranks[1 if kind == 'minimum_cover' else 0]


@pytest.mark.parametrize('engine', [solve, greedy])
def test_schedulers_fill_each_staffing_rank(h, engine):
    data = _world(h, assigned=False)
    (_, _, c), ranks, scope = _ranks(data, h)
    data['policies'] = [_policy(data, {'kind': 'minimum_cover', 'minimum': 1}, scope)]
    result = engine(data, 10)
    assert result['status'] == 'candidate_feasible', result.get('issues', result)
    assignments = [r['participant']['person_id'] for r in result['additions']]
    assert len(assignments) == 2 and c in assignments
    assert not [f for f in result['validation']['findings'] if f['rule_id'] == data['policies'][0]['id']]


@pytest.mark.parametrize('engine', [solve, greedy])
def test_schedulers_use_the_matching_counted_catalogue_line(h, engine):
    data = _world(h, assigned=False)
    (_, _, c), ranks, scope = _ranks(data, h)
    clinic_a = data['shifts'][0]['service_ids'][0]
    clinic_b = next(key for key in data['services'] if key != clinic_a)
    lines = [
        {'id': str(uuid.uuid4()), 'who': scope['conditions'],
         'where': {'conditions': [_line('service_ids', [clinic_a])]}, 'numbers': {'min': {'value': 1}}},
        {'id': str(uuid.uuid4()), 'who': {'conditions': [_line('category_ids', [ranks[0]])]},
         'where': {'conditions': [_line('service_ids', [clinic_b])]}, 'numbers': {'min': {'value': 1}}},
    ]
    data['policies'] = [_policy(data, {'kind': 'catalogue', 'id': 'kind_per_session', 'lines': lines}, {})]
    result = engine(data, 10)
    assert result['status'] == 'candidate_feasible', result.get('issues', result)
    assert c in {r['participant']['person_id'] for r in result['additions']}
    assert not [f for f in result['validation']['findings'] if f['rule_id'] == data['policies'][0]['id']]


def test_counted_staffing_compiles_the_same_solver_model_each_time(h):
    data = _world(h, assigned=False)
    _, _, scope = _ranks(data, h)
    data['policies'] = [_policy(data, {'kind': 'minimum_cover', 'minimum': 1}, scope)]
    models = []
    for _ in range(3):
        result = solve(copy.deepcopy(data), 10, workers=1, feasibility_only=True,
                       _on_model=lambda model: models.append(str(model.proto)))
        assert result['status'] == 'model_ready'
    assert len(set(models)) == 1


def test_each_workload_caching_preserves_greedy_validation_and_entire_model(h, monkeypatch):
    from packages.domain import conditions, cp_sat_workload, generation, validation
    from tests.scheduler_departments import apply, department
    data, _ = department(h, 11, people=20, days=7, rules='full', clinics=2)
    scope = _count_scope(list(data['services'])[:2], 'each')
    for rule in ({'kind': 'max_duties', 'maximum': 4, 'window': 'week'},
                 {'kind': 'max_hours', 'hours': 8, 'window': 'day'}):
        data['policies'].append(h.policy(rule, scope, severity='advisory'))
    monkeypatch.setattr(generation, 'time', SimpleNamespace(monotonic=lambda: 0.0))
    original = conditions.rule_holds
    original_workload_matches = cp_sat_workload.workload_matches
    matched = []
    def workload_matches(data, *args):
        assert '_workload_matches_cache' in data
        result = original_workload_matches(data, *args)
        matched.append(bool(data['_workload_matches_cache']))
        return result
    monkeypatch.setattr(cp_sat_workload, 'workload_matches', workload_matches)
    def uncached_workload_matches(data, *args):
        return original_workload_matches({**data, '_workload_matches_cache': None}, *args)
    def uncached(*args):
        holds = original(*args)
        holds.bucket_cache = None
        return holds
    outputs = []
    for cached in (True, False):
        if not cached:
            monkeypatch.setattr(conditions, 'rule_holds', uncached)
            monkeypatch.setattr(generation, 'rule_holds', uncached)
            monkeypatch.setattr(cp_sat_workload, 'workload_matches', uncached_workload_matches)
            monkeypatch.setattr(validation, 'workload_matches', uncached_workload_matches)
        candidate = greedy(data, 300)
        assert candidate['status'] in {'candidate_feasible', 'candidate_with_gaps'} and candidate['additions']
        models = []
        compiled = solve(data, 10, feasibility_only=True, _on_model=lambda model: models.append(str(model.proto)))
        assert compiled['status'] == 'model_ready'
        outputs.append((candidate, evaluate(apply(data, candidate)), models[0]))
    assert outputs[0] == outputs[1]
    assert any(matched), 'model preparation must populate the workload matching cache'


@pytest.mark.parametrize('quota', [False, True])
def test_solver_and_validator_agree_on_each_workload_bounds(h, quota):
    data = _world(h)
    clinics = _seven(data)
    if quota:
        _quota(data, clinics, 'each')
    else:
        data['policies'] = [_policy(data, {'kind': 'max_duties', 'maximum': 6, 'window': 'week'}, _count_scope(clinics, 'each'))]
    result = solve(data, 10, feasibility_only=True)
    assert result['status'] == 'candidate_feasible', result.get('issues', result)
    assert result['validation']['valid']


def test_zero_quota_buckets_survive_empty_assignment_reports(h):
    data = _world(h, assigned=False)
    clinics = list(data['services'])[:2]
    _quota(data, clinics, 'each', comparison='minimum', amount=1)
    rows = evaluate(data)['quota_report']['rows']
    assert len(rows) == 2 and {r['actual'] for r in rows} == {0}
    assert {r['count_bucket'][-1][1] for r in rows} == set(clinics)
    result = solve(data, 10, feasibility_only=True)
    assert result['status'] == 'requirements_conflict', result


def test_score_treats_each_advisory_staffing_bucket_as_one_obligation(h):
    data = _world(h)
    _, _, scope = _ranks(data, h)
    data['policies'] = [_policy(data, {'kind': 'minimum_cover', 'minimum': 1}, scope, severity='advisory')]
    result = schedule_score(data, evaluate(data), {'scoring': {'weights': {
        'coverage': 0, 'rules': 1, 'fairness': 0, 'preferences': 0, 'stability': 0}}})
    assert result['components']['rules']['value'] == .5


def test_explicit_together_keeps_legacy_validation_and_score_outputs(h):
    data = _world(h)
    clinics = _seven(data)
    data['policies'] = [_policy(data, {'kind': 'max_duties', 'maximum': 6, 'window': 'week'}, _count_scope(clinics, None))]
    original = evaluate(data)
    score = schedule_score(data, original)
    explicit = copy.deepcopy(data)
    explicit['policies'][0]['definition']['scope']['conditions']['conditions'][0]['count'] = 'together'
    assert evaluate(explicit) == original
    assert schedule_score(explicit, original) == score


def test_each_balance_goal_reads_frozen_duties_for_separate_history_and_planned_counts(h):
    person = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[person])['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[person], service=1)['workspace']
    h.publish(w)
    policy = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'period': 'month', 'aim': 'target', 'amount': 1},
                      _count_scope(h.seed['services'], 'each'), severity='advisory')
    current = h.workspace(start='2026-10-11', end='2026-10-18')
    current = h.shift(current, '2026-10-11T08:00:00+03:00', '2026-10-11T12:00:00+03:00', people=[person])['workspace']
    data = h.snapshot(current)
    reports = [g for g in evaluate(data)['goals'] if g['policy_id'] == policy['id']]
    by_clinic = {g['count_bucket'][-1][1]: next(r for r in g['people'] if r['person_id'] == person) for g in reports}
    assert (by_clinic[h.seed['services'][0]]['prior'], by_clinic[h.seed['services'][0]]['planned']) == (1, 1)
    assert (by_clinic[h.seed['services'][1]]['prior'], by_clinic[h.seed['services'][1]]['planned']) == (1, 0)
    goals = [g for g in balance_goals(data) if g['id'] == policy['id']]
    assert len({goal_identity(g) for g in goals}) == 2
    assert all(len('goal:' + goal_identity(g)) <= 96 for g in goals)
    publication = h.publish(current)['publication_id']
    with transaction(h.app.state.engine) as db:
        keys = set(db.scalars(select(m.FairnessLedger.metric_key).where(
            m.FairnessLedger.publication_id == publication, m.FairnessLedger.metric_key.like('goal:' + policy['id'] + '%'))))
    assert keys == {'goal:' + goal_identity(g) for g in goals}


def test_counted_catalogue_goal_lines_keep_separate_people_and_history(h):
    a, b, _ = h.seed['people']
    w = h.shift(h.workspace(), people=[a])['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[b])['workspace']
    h.publish(w)
    data = h.snapshot(h.shift(h.workspace(start='2026-10-11', end='2026-10-18'),
                             '2026-10-11T08:00:00+03:00', '2026-10-11T12:00:00+03:00', people=[])['workspace'])
    clinic = data['shifts'][0]['service_ids'][0]
    lines = [{'id': str(uuid.uuid4()), 'who': {'conditions': [_line('person_ids', [pid])]},
              'where': {'conditions': [_line('service_ids', [clinic], 'each')]},
              'numbers': {'target': {'value': amount, 'level': 'medium'}}}
             for pid, amount in ((a, 1), (b, 2))]
    data['policies'] = [_policy(data, {'kind': 'catalogue', 'id': 'fair_share', 'lines': lines,
        'settings': {'balance_goal': {'metric': 'duties', 'period': 'month', 'aim': 'target'}}}, {})]
    goals = balance_goals(data)
    assert len(goals) == 2 and goals[0]['count_bucket'] == goals[1]['count_bucket']
    assert len({goal_identity(goal) for goal in goals}) == 2
    with transaction(h.app.state.engine) as db:
        history = fairness_service.history(db, data, goals)
    for goal, pid in zip(goals, (a, b)):
        assert set(history[goal_identity(goal)]['people']) == {pid}
        assert history[goal_identity(goal)]['people'][pid]['value'] == 1
    reordered = copy.deepcopy(data)
    reordered['policies'][0]['definition']['rule']['lines'].reverse()
    assert {goal_identity(goal) for goal in balance_goals(reordered)} == {goal_identity(goal) for goal in goals}


@pytest.mark.parametrize('metric,days,move,expected', [
    ('duties', (2, 7), True, (1, 1)),
    ('weekends', (2, 3), False, (1, 0)),
])
@pytest.mark.parametrize('same_id', [False, True])
def test_counted_history_uses_each_publications_frozen_place_ancestry(h, monkeypatch, metric, days, move, expected, same_id):
    data = _world(h)
    pid = h.seed['people'][0]
    data['people'] = {pid: data['people'][pid]}
    data['workspace'].update(starts_on='2026-10-15', ends_on='2026-10-22')
    room, north, south = (str(uuid.uuid4()) for _ in range(3))
    old = {room: {'parent_id': north}, north: {'parent_id': None}, south: {'parent_id': None}}
    new = {**old, room: {'parent_id': south if move else north}}
    data['locations'] = new
    publications = []
    for index, day in enumerate(days):
        shift = copy.deepcopy(data['shifts'][0])
        shift.update(id='historical-place-' + str(0 if same_id else index), location_id=room,
                     start=f'2026-10-{day:02}T08:00:00+03:00', end=f'2026-10-{day:02}T12:00:00+03:00')
        shift['participants'] = [{**shift['participants'][0], 'shift_id': shift['id'],
                                  'start': shift['start'], 'end': shift['end']}]
        publications.append(SimpleNamespace(id='publication-' + str(index), workspace_id='past-' + str(index), effective_until=None,
            snapshot={'shifts': [shift], 'locations': old if index == 0 else new}))
    # Each bucket has a current eligible audience, even when its historical count is zero.
    focused = []
    for index, place in enumerate((north, south)):
        duty = copy.deepcopy(data['shifts'][0])
        duty.update(id='future-place-' + str(index), location_id=place, participants=[],
                    start=f'2026-10-{15 + index:02}T08:00:00+03:00', end=f'2026-10-{15 + index:02}T12:00:00+03:00')
        focused.append(duty)
    data['shifts'], data['focus_ids'] = focused, [duty['id'] for duty in focused]
    data['policies'] = [_policy(data, {'kind': 'balance_goal', 'metric': metric, 'period': 'month',
        'aim': 'target', 'amount': 1}, _count_scope_for_places(north, south), severity='advisory')]
    goals = balance_goals(data)
    monkeypatch.setattr('apps.api.services.schedule.current_publications', lambda *_: publications)
    db = SimpleNamespace(execute=lambda _query: [('available_days', pid, None, date(2026, 10, 1), 14.0)])
    history = fairness_service.history(db, data, goals)
    counts = {goal['count_bucket'][-1][1]: history[goal_identity(goal)]['people'][pid]['value'] for goal in goals}
    assert (counts[north], counts[south]) == expected


def _count_scope_for_places(*places):
    return {'conditions': {'conditions': [_line('location_ids', places, 'each')]}}


def test_counted_quota_inherits_the_strength_of_its_own_repeating_window(h):
    pid = h.seed['people'][0]
    w = h.workspace(start='2026-10-04', end='2026-10-18')
    for day in (5, 12):
        w = h.shift(w, f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00', people=[pid])['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid]}
    clinics = list(data['services'])[:2]
    ranks = [h.seed['category'], h.seed['trainee_category']]
    membership = next(row for row in data['memberships'] if row['person_id'] == pid)
    data['memberships'] = [{**membership, 'category_id': ranks[0], 'ends_on': '2026-10-11'},
                           {**membership, 'category_id': ranks[1], 'starts_on': '2026-10-11', 'ends_on': None}]
    dates = {'starts_on': '2026-10-04', 'ends_on': '2026-10-18'}
    where = {'conditions': [_line('service_ids', clinics, 'each')]}
    lines = [{'id': str(uuid.uuid4()), 'who': {'conditions': [_line('category_ids', [rank])]},
              'where': where, 'numbers': {'min': {'value': 2, 'level': level}}, **dates}
             for rank, level in zip(ranks, ('high', 'low'))]
    lines.append({'id': str(uuid.uuid4()), 'who': {'conditions': [_line('person_ids', [pid])]},
                  'where': where, 'numbers': {'min': {'value': 2, 'level': None}}, **dates})
    data['policies'] = [_policy(data, {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': lines,
        'settings': {'block_quota': {**dates, 'window_alignment': 'calendar_week'}}}, {})]
    data['quota_context'] = {'complete': True, 'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-18T00:00:00+03:00'}
    rows = evaluate(data)['quota_report']['rows']
    assert {(row['starts_on'], row['count_bucket'][-1][1], row['strength'], row['actual']) for row in rows} == {
        (start, clinic, strength, 1 if clinic == data['shifts'][0]['service_ids'][0] else 0)
        for start, strength in (('2026-10-04', 'high'), ('2026-10-11', 'low')) for clinic in clinics}
    assert len(rows) == 4 and all(row['line'] == 2 for row in rows)
    result = solve(data, 10, feasibility_only=True)
    assert result['status'] == 'candidate_feasible', result
    assert len(result['validation']['quota_report']['rows']) == 4


def test_counted_staffing_safety_keeps_the_stricter_broader_line(h):
    data = _world(h)
    (a, _, _), ranks, scope = _ranks(data, h)
    clinic = data['shifts'][0]['service_ids'][0]
    where = {'conditions': [_line('service_ids', [clinic], 'each')]}
    lines = [{'id': str(uuid.uuid4()), 'who': scope['conditions'], 'where': where, 'numbers': {'min': {'value': 2}}},
             {'id': str(uuid.uuid4()), 'who': {'conditions': [_line('person_ids', [a])]},
              'where': where, 'numbers': {'min': {'value': 1}}}]
    data['policies'] = [_policy(data, {'kind': 'catalogue', 'id': 'kind_per_session', 'lines': lines}, {})]
    findings = [f for f in evaluate(data)['findings'] if f['rule_id'] == data['policies'][0]['id']]
    assert len(findings) == 1
    observations = [findings[0], *findings[0]['detail']['derived_findings']]
    assert len(observations) == 2 and {f['detail']['line'] for f in observations} == {0}
    assert {f['detail']['count_bucket'][0][1] for f in observations} == set(ranks)
    assert all(f['severity'] == 'error' and f['detail']['required'] == 2 for f in observations)


@pytest.mark.parametrize('engine', [solve, greedy])
def test_schedulers_require_a_person_holding_all_selected_tags(h, engine, monkeypatch):
    data = _world(h, assigned=False)
    shift = data['shifts'][0]
    # Isolate the scoped demand from the fixture's independent general staffing requirement.
    for definition in (shift['type_snapshot']['definition'], data['types'][shift['assignment_type_id']]['definition']):
        definition['requirements'] = []
    a, b, c = h.seed['people']
    for pid, tags in ((a, ['night_capable']), (b, ['senior']), (c, ['night_capable', 'senior'])):
        data['people'][pid]['tags'] = tags
    scope = {'conditions': {'conditions': [{'field': 'tags', 'op': 'all', 'values': ['night_capable', 'senior']}]}}
    data['policies'] = [_policy(data, {'kind': 'minimum_cover', 'minimum': 1}, scope)]
    if engine is greedy:
        # This checks matching, not a wall-clock timeout under parallel load.
        monkeypatch.setattr('packages.domain.generation.time', SimpleNamespace(monotonic=lambda: 0.0))
    result = engine(data, 10, **({'deterministic': True} if engine is solve else {}))
    assert result['status'] == 'candidate_feasible', result.get('issues', result)
    assert {r['participant']['person_id'] for r in result['additions']} == {c}
    assert result['validation']['valid']


def test_supervision_limits_keep_each_matching_supervisor_bucket(h):
    trainee, supervisor, _ = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Counted named supervision', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 3, 'require_pairing': True}}})['record']
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    people = [{'person_id': trainee, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': supervisor},
              {'person_id': supervisor, 'start': start, 'end': end, 'participation': 'supervisor'}]
    data = h.snapshot(h.shift(h.workspace(), typ=typ['id'], people=people)['workspace'])
    for membership in data['memberships']:
        if membership['person_id'] == trainee:
            membership['category_id'] = h.seed['trainee_category']
    data['people'][supervisor]['tags'] = ['night_capable', 'senior']
    scope = {'conditions': {'conditions': [_line('tags', ['night_capable', 'senior'], 'each')]}}
    data['policies'] = [_policy(data, {'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 0}, scope)]
    findings = [f for f in evaluate(data)['findings'] if f['code'] == 'SUPERVISOR_SCOPED_CAPACITY']
    assert len(findings) == 2 and {f['detail']['count_bucket'][-1][1] for f in findings} == {'night_capable', 'senior'}
    models = []
    result = solve(data, 10, feasibility_only=True, _on_model=models.append)
    assert result['status'] == 'model_ready', result
    assert sum(policy_id(c.name) == data['policies'][0]['id'] for c in models[0].proto.constraints) == 2
    data['shifts'][0]['locked'] = True
    locked_result = solve(data, 10, feasibility_only=True)
    assert locked_result['status'] == 'candidate_with_gaps', locked_result
    assert any(f.get('code') == 'SUPERVISOR_SCOPED_CAPACITY' and f.get('severity') == 'error'
               for f in locked_result['validation'].get('findings', []))
    assert locked_result['validation']['valid'] is False
    assert any(f.get('code') == 'SUPERVISOR_SCOPED_CAPACITY'
               for f in locked_result['summary'].get('fixed_breaches', []))


def _supervision_zero_snapshot(h, *, locked):
    trainee, supervisor, _ = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Zero supervision capacity', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}],
            'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 3, 'require_pairing': True}}})['record']
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    people = [{'person_id': trainee, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': supervisor},
              {'person_id': supervisor, 'start': start, 'end': end, 'participation': 'supervisor'}]
    workspace = h.shift(h.workspace(), typ=typ['id'], people=people)['workspace']
    if locked:
        workspace = h.shift(workspace, typ=typ['id'], people=[], start='2026-10-05T08:00:00+03:00',
                            end='2026-10-05T12:00:00+03:00')['workspace']
    data = h.snapshot(workspace)
    for membership in data['memberships']:
        if membership['person_id'] == trainee:
            membership['category_id'] = h.seed['trainee_category']
    data['people'][supervisor]['tags'] = ['night_capable', 'senior']
    data['policies'] = [_policy(data, {'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 0},
                                {'conditions': {'conditions': [_line('tags', ['night_capable', 'senior'], 'each')]}})]
    data['shifts'][0]['locked'] = locked
    return data


@pytest.mark.parametrize('locked', [False, True])
def test_zero_supervision_capacity_is_enforced_by_both_schedulers(h, locked):
    data = _supervision_zero_snapshot(h, locked=locked)
    finding = next(f for f in evaluate(data)['findings'] if f['code'] == 'SUPERVISOR_SCOPED_CAPACITY')
    assert finding['severity'] == 'error'
    cp_sat = solve(copy.deepcopy(data), 10, feasibility_only=True)
    if locked:
        assert cp_sat['status'] == 'candidate_with_gaps', cp_sat
        assert not cp_sat['validation']['valid']
        assert any(f.get('code') == 'SUPERVISOR_SCOPED_CAPACITY' and f.get('severity') == 'error'
                   for f in cp_sat['validation'].get('findings', []))
        assert any(f.get('code') == 'SUPERVISOR_SCOPED_CAPACITY'
                   for f in cp_sat['summary'].get('fixed_breaches', []))
        editable = next(s['id'] for s in data['shifts'] if not s['locked'])
        assert cp_sat['additions'] and all(a['shift_id'] == editable for a in cp_sat['additions'])
    else:
        assert cp_sat['status'] == 'candidate_feasible', cp_sat
        assert cp_sat['validation']['valid']
        assert not any(f.get('code') == 'SUPERVISOR_SCOPED_CAPACITY' and f.get('severity') == 'error'
                       for f in cp_sat['validation'].get('findings', []))
        trainee, supervisor, _ = h.seed['people']
        assert any(r.get('person_id') == trainee for r in cp_sat.get('removals', []))
        assert not any((a.get('participant') or {}).get('supervisor_person_id') == supervisor
                       for a in cp_sat.get('additions', []))
    greedy_result = greedy(copy.deepcopy(data), 10)
    assert greedy_result['status'] != 'candidate_feasible'

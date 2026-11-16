"""Configurable conditional cover and overtime run counting, shared by every scheduling engine."""
import copy
import time
import uuid
from datetime import datetime, timedelta

import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain import cp_sat, generation
from packages.domain.conditions import active_rules
from packages.domain.time import aware
from packages.domain.validation import (consecutive_day_count, evaluate, staffing_counts, staffing_credit,
                                        workload_day_observations)
from tests.scheduler_departments import apply, department


def tree(field, *values):
    return {'match': 'all', 'conditions': [{'field': field, 'op': 'any', 'values': list(values)}]}


def policy(data, rule, scope=None, *, severity='mandatory', exceptions=False):
    return {'id': str(uuid.uuid4()), 'revision': 1, 'name': 'Synthetic configurable staffing',
            'kind': rule['kind'], 'status': 'active', 'active': True,
            'department_id': data['workspace']['department_id'], 'starts_on': '2020-01-01', 'ends_on': None,
            'definition': PolicyDefinition.model_validate({'rule': rule, 'scope': scope or {},
                'severity': severity, 'exception_allowed': exceptions}).model_dump(mode='json')}


def solve(data, engine):
    return cp_sat.generate(data, 5, workers=1, deterministic=True) if engine == 'cp_sat' else generation.generate(data, 5)


def cover_world(h, *, assigned=False, aggregation='assignment'):
    typ = h.configure('assignment_types', {'name': 'Synthetic conditional cover', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    workspace = h.shift(h.workspace(), typ=typ['id'], people=h.seed['people'] if assigned else [])['workspace']
    data = h.snapshot(workspace)
    data['shifts'][0]['participants'].sort(key=lambda row: h.seed['people'].index(row['person_id']))
    rule = {'kind': 'minimum_cover', 'minimum': 3, 'aggregation': aggregation,
            'credit_conditions': tree('person_ids', h.seed['people'][0]), 'credit_minimum_others': 2}
    data['policies'] = [policy(data, rule,
        {'service_ids': h.seed['services']} if aggregation != 'assignment' else {})]
    return data


@pytest.mark.parametrize('engine', ['cp_sat', 'greedy'])
def test_conditional_credit_fills_with_independently_qualifying_companions(h, engine):
    data = cover_world(h)
    result = solve(data, engine)
    assert result['status'] == 'candidate_feasible', result
    assert {row['participant']['person_id'] for row in result['additions']} == set(h.seed['people'])
    assert not [f for f in evaluate(apply(data, result))['findings'] if f['code'] == 'SCOPED_COVERAGE_GAP']


@pytest.mark.parametrize('engine', ['cp_sat', 'greedy'])
def test_conditional_people_never_provide_circular_credit(h, engine):
    data = cover_world(h)
    data['policies'][0]['definition']['rule']['credit_conditions'] = tree('person_ids', *h.seed['people'][:2])
    result = solve(data, engine)
    assert result['status'] != 'candidate_feasible', result


@pytest.mark.parametrize('field', ['person_ids', 'category_ids', 'tags', 'programme_ids'])
def test_conditional_credit_uses_shared_dynamic_person_scopes(h, field):
    data = cover_world(h, assigned=True)
    conditional, *others = h.seed['people']
    value = conditional
    if field == 'tags':
        value = 'Synthetic conditional population'
        data['people'][conditional]['tags'] = [value]
    elif field in ('category_ids', 'programme_ids'):
        value = str(uuid.uuid4())
        definitions, records, key = (('categories', 'memberships', 'category_id') if field == 'category_ids' else
                                     ('programmes', 'programme_memberships', 'programme_id'))
        data[definitions][value] = {'id': value, 'active': True, 'starts_on': '2020-01-01', 'ends_on': None}
        data[records].append({'person_id': conditional, key: value, 'department_id': data['workspace']['department_id'],
                              'starts_on': '2020-01-01', 'ends_on': None})
    configured = data['policies'][0]
    configured['definition']['rule']['credit_conditions'] = tree(field, value)
    shift = data['shifts'][0]
    a, b = aware(shift['start']), aware(shift['end'])
    assert set(staffing_credit(data, configured, shift, a, b, dict.fromkeys([conditional, others[0]], 1))) == {others[0]}
    assert set(staffing_credit(data, configured, shift, a, b, dict.fromkeys(h.seed['people'], 1))) == set(h.seed['people'])
    # A companion selected by a different outer scope cannot supply this rule's credit.
    configured['definition']['scope'] = {'person_ids': [conditional, others[0]]}
    assert set(staffing_credit(data, configured, shift, a, b, dict.fromkeys(h.seed['people'], 1))) == {others[0]}


def test_pooled_credit_requires_companions_on_the_same_duty(h):
    data = cover_world(h, assigned=True, aggregation='service_group')
    shift = data['shifts'][0]
    conditional, *others = shift['participants']
    shift['participants'] = [conditional]
    peer = copy.deepcopy(shift)
    peer.update(id=str(uuid.uuid4()), participants=others)
    data['shifts'].append(peer)
    data['focus_ids'].append(peer['id'])
    [configured] = active_rules(data['policies'])
    counts = staffing_counts(data, configured, shift, aware(shift['start']), aware(shift['end']))
    assert counts[0]['available'] == 2
    peer['participants'].append(copy.deepcopy(others[0]))
    assert staffing_counts(data, configured, shift, aware(shift['start']), aware(shift['end']))[0]['available'] == 2


def test_conditional_credit_reacts_to_dated_membership_and_partial_companion_presence(h):
    data = cover_world(h, assigned=True)
    shift = data['shifts'][0]
    shift.update(start='2026-10-04T23:00:00+03:00', end='2026-10-05T03:00:00+03:00')
    for row in shift['participants']:
        row.update(start=shift['start'], end=shift['end'])
    shift['participants'] = shift['participants'][:2]
    programme = str(uuid.uuid4())
    data['programmes'][programme] = {'id': programme, 'active': True, 'starts_on': '2020-01-01', 'ends_on': None}
    data['programme_memberships'].append({'person_id': h.seed['people'][0], 'programme_id': programme,
                                         'starts_on': '2026-10-05', 'ends_on': None})
    rule = data['policies'][0]['definition']['rule']
    rule.update(minimum=2, credit_conditions=tree('programme_ids', programme))
    gaps = [f for f in evaluate(data)['findings'] if f['code'] == 'SCOPED_COVERAGE_GAP']
    assert len(gaps) == 1 and aware(gaps[0]['start']) == aware('2026-10-05T00:00:00+03:00'), gaps
    assert gaps[0]['detail']['available'] == 1
    data = cover_world(h, assigned=True)
    data['shifts'][0]['participants'][-1]['end'] = '2026-10-04T10:00:00+03:00'
    gaps = [f for f in evaluate(data)['findings'] if f['code'] == 'SCOPED_COVERAGE_GAP']
    assert len(gaps) == 1 and aware(gaps[0]['start']) == aware('2026-10-04T10:00:00+03:00'), gaps
    assert gaps[0]['detail']['available'] == 1


def day_entries(markers):
    start = datetime.fromisoformat('2026-10-04T08:00:00+03:00')
    return [({'id': str(n)}, {'overtime': overtime}, start + timedelta(days=n),
             start + timedelta(days=n, hours=4)) for n, overtime in enumerate(markers)]


@pytest.mark.parametrize('markers,extension,expected', [
    ([False, False, True], 0, 3),
    ([False, False, True], 1, 2),
    ([False, True, False], 1, 2),
    ([False, False, True, True], 1, 3),
    ([False, True, False, False], 2, 3),
    ([True, True], 1, 1),
])
def test_overtime_extension_counts_a_continuous_run(markers, extension, expected):
    entries = day_entries(markers)
    run, ordinary = list(workload_day_observations(entries, 'UTC'))[-1]
    assert consecutive_day_count({'maximum': 2, 'overtime_extension_days': extension}, len(run), ordinary) == expected


def test_a_mixed_regular_and_overtime_day_is_one_ordinary_day():
    entries = day_entries([False, True])
    shift, _, a, b = entries[-1]
    entries.append((shift, {'overtime': False}, a, b))
    run, ordinary = list(workload_day_observations(entries, 'UTC'))[-1]
    assert len(run) == ordinary == 2
    assert consecutive_day_count({'maximum': 1, 'overtime_extension_days': 1}, len(run), ordinary) == 2


def test_hypothetical_participation_without_overtime_marker_is_ordinary():
    entries = day_entries([False, False])
    for _, participant, _, _ in entries:
        participant.pop('overtime')
    run, ordinary = list(workload_day_observations(entries, 'UTC'))[-1]
    assert len(run) == ordinary == 2
    assert consecutive_day_count({'overtime_extension_days': 1}, len(run), ordinary) == 2


def run_world(h, markers, settings, *, exceptions=False, severity='mandatory'):
    typ = h.configure('assignment_types', {'name': 'Synthetic overtime run', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']
    workspace = h.workspace()
    person = h.seed['people'][0]
    for day, overtime in enumerate(markers, 4):
        start, end = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
        workspace = h.shift(workspace, start=start, end=end, typ=typ['id'],
            people=[{'person_id': person, 'start': start, 'end': end, 'overtime': overtime, 'fixed': True}])['workspace']
    data = h.snapshot(workspace)
    data['shifts'].sort(key=lambda shift: shift['start'])
    data['people'] = {person: data['people'][person]}
    data['policies'] = [policy(data, {'kind': 'max_consecutive_days', 'maximum': 2, **settings},
                               exceptions=exceptions, severity=severity)]
    data['generation_options'] = {'mode': 'fill_gaps'}
    return data


@pytest.mark.parametrize('engine', ['cp_sat', 'greedy'])
@pytest.mark.parametrize('markers,settings,valid', [
    ([False, False, True], {}, False),
    ([False, False, True], {'include_overtime': False}, True),
    ([False, False, True], {'overtime_extension_days': 1}, True),
    ([False, True, False], {'overtime_extension_days': 1}, True),
    ([False, False, True, True], {'overtime_extension_days': 1}, False),
    ([False, True, False, False], {'overtime_extension_days': 2}, False),
])
def test_engines_and_validator_agree_on_overtime_runs(h, engine, markers, settings, valid):
    data = run_world(h, markers, settings)
    findings = [f for f in evaluate(data)['findings'] if f['code'] == 'CONSECUTIVE_DAYS']
    assert bool(findings) is not valid
    result = solve(data, engine)
    assert (result['status'] == 'candidate_feasible') is valid, result


@pytest.mark.parametrize('engine', ['cp_sat', 'greedy'])
def test_overtime_exclusion_breaks_the_counted_run(h, engine):
    data = run_world(h, [False, False, True, False], {'include_overtime': False})
    assert not [f for f in evaluate(data)['findings'] if f['code'] == 'CONSECUTIVE_DAYS']
    assert solve(data, engine)['status'] == 'candidate_feasible'


@pytest.mark.parametrize('engine', ['cp_sat', 'greedy'])
@pytest.mark.parametrize('settings,valid', [({}, False), ({'include_overtime': False}, True),
                                          ({'overtime_extension_days': 1}, True)])
def test_engines_choose_overtime_only_when_the_configured_run_allows_it(h, engine, settings, valid):
    data = run_world(h, [False, False, True], settings)
    data['shifts'][-1]['participants'] = []
    data['generation_session_ids'] = [data['shifts'][-1]['id']]
    data['policies'].append(policy(data, {'kind': 'allocation_overtime'}, severity='advisory'))
    result = solve(data, engine)
    assert (result['status'] == 'candidate_feasible') is valid, result
    if valid:
        assert len(result['additions']) == 1 and result['additions'][0]['participant']['overtime']
        assert not [f for f in evaluate(apply(data, result))['findings'] if f['code'] == 'CONSECUTIVE_DAYS']


def test_extension_keeps_exception_bound_to_the_entire_actual_run(h):
    data = run_world(h, [False, True, False, False], {'overtime_extension_days': 2}, exceptions=True)
    configured = data['policies'][0]
    data['exceptions'] = [{'id': str(uuid.uuid4()), 'status': 'approved', 'policy_id': configured['id'],
        'policy_revision': configured['revision'], 'person_id': h.seed['people'][0],
        'starts_on': '2026-10-04', 'ends_on': '2026-10-08'}]
    assert cp_sat.generate(data, 5, workers=1)['status'] == 'candidate_feasible'
    data['exceptions'][0]['starts_on'] = '2026-10-05'
    assert cp_sat.generate(data, 5, workers=1)['status'] != 'candidate_feasible'


def test_advisory_extension_penalty_and_reach_share_the_counted_day_metric(h):
    from packages.domain.score import schedule_score
    from packages.domain.rule_map import _reach_workload_observations
    data = run_world(h, [False, True, False, False], {'overtime_extension_days': 2},
                     exceptions=True, severity='advisory')
    configured, person = data['policies'][0], h.seed['people'][0]
    observations = _reach_workload_observations(data, configured, person,
        [(s, p, aware(p['start']), aware(p['end'])) for s in data['shifts'] for p in s['participants']],
        aware(data['shifts'][0]['start']), aware(data['shifts'][-1]['end']))
    assert observations[-1]['actual'] == 3 and observations[-1]['limit'] == 2
    before = cp_sat.generate(data, 5, workers=1)
    assert before['status'] == 'candidate_feasible'
    assert before['objective_components']['rules']['value'] == schedule_score(
        data, evaluate(data))['components']['rules']['value'] < 1
    assert before['compiled_component_values']['rules'] == pytest.approx(
        before['objective_components']['rules']['value'], abs=0.0001)
    assert len([f for f in before['validation']['findings'] if f['code'] == 'CONSECUTIVE_DAYS']) == 1
    data['exceptions'] = [{'id': str(uuid.uuid4()), 'status': 'approved', 'policy_id': configured['id'],
        'policy_revision': configured['revision'], 'person_id': person,
        'starts_on': '2026-10-04', 'ends_on': '2026-10-08'}]
    after = cp_sat.generate(data, 5, workers=1)
    assert after['status'] == 'candidate_feasible'
    assert after['objective_components']['rules']['value'] == schedule_score(
        data, evaluate(data))['components']['rules']['value'] == 1
    assert after['compiled_component_values']['rules'] == pytest.approx(1, abs=0.0001)
    assert not [f for f in after['validation']['findings'] if f['code'] == 'CONSECUTIVE_DAYS']


@pytest.fixture(scope='module')
def department_new_staffing_constraints(scale_harness):
    h = scale_harness('h')
    data, _ = department(h, 7, people=150, days=28, clinics=9, rules='none', leave_rate=0)
    configured = copy.deepcopy(data)
    configured['policies'] = [
        policy(configured, {'kind': 'minimum_cover', 'minimum': 1,
            'credit_conditions': tree('person_ids', list(data['people'])[0]), 'credit_minimum_others': 2}),
        policy(configured, {'kind': 'max_consecutive_days', 'maximum': 4, 'overtime_extension_days': 1}),
    ]
    sizes, timings, results = [], [], []
    for snapshot in (data, configured):
        captured = []
        started = time.process_time()
        result = cp_sat.generate(snapshot, 5, workers=1, deterministic=True, feasibility_only=True, _on_model=captured.append)
        elapsed = time.process_time() - started
        results.append(result)
        sizes.append((len(captured[0].proto.variables), len(captured[0].proto.constraints)))
        timings.append(elapsed)
    print('conditional staffing and overtime run model', {'people': len(data['people']), 'sessions': len(data['shifts']),
          'cpu_seconds': [round(elapsed, 3) for elapsed in timings], 'model_sizes': sizes})
    return data, sizes, timings, results


def test_new_staffing_constraints_compile_with_a_department_scale_cpu_budget(department_new_staffing_constraints):
    data, sizes, _, results = department_new_staffing_constraints
    for result in results:
        assert result['status'] == 'model_ready', result
    places = len(data['people']) * len(data['shifts'])
    assert len(data['shifts']) >= 500
    assert sizes[1][0] - sizes[0][0] < places
    assert sizes[1][1] - sizes[0][1] < 2 * places


@pytest.mark.cpu_budget
def test_new_staffing_constraints_department_scale_cpu_budget(department_new_staffing_constraints):
    from tests.test_scheduler_performance import _budget
    timings = department_new_staffing_constraints[2]
    for elapsed in timings:
        assert elapsed < _budget(90), elapsed

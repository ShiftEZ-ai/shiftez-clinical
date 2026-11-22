"""Line validity selects focal duties; counted runs and windows retain their neighbours."""
import copy
import json

import pytest
from ortools.sat.python import cp_model

from packages.contracts.rule_catalogue import CATALOGUE_MAPPING, V2_CATALOGUE_BY_ID
from packages.contracts.schemas import SHAPE_KINDS, WORKLOAD_COUNT_KINDS
from packages.domain.cp_sat import generate
from packages.domain.cp_sat_patterns import add_patterns
from packages.domain.rule_map import reach
from packages.domain.time import aware
from packages.domain.validation import VALIDITY_FOCAL_ONLY_KINDS, evaluate
from tests.test_autoscheduler_audit_safety import _assignment
from tests.test_engine_workload_review import one_person, policy
from tests.test_rule_catalogue_guard import catalogue, line


@pytest.fixture
def counting_world(h):
    pid = h.seed['people'][0]
    return one_person(h.snapshot(h.shift(h.workspace(), people=[pid])['workspace']), pid)


def mirror(world, dates, focal, rule, start, end=None, *, ranked=False):
    data = copy.deepcopy(world)
    pid = next(iter(data['people']))
    prototype = data['shifts'][0]
    data['workspace'].update(starts_on=min(focal), ends_on='2027-01-01')
    data['shifts'], data['focus_ids'] = [], []
    for day in dates:
        shift = copy.deepcopy(prototype)
        shift.update(id='synthetic-' + day, start=day + 'T08:00:00+03:00', end=day + 'T12:00:00+03:00')
        shift['type_snapshot']['definition']['requirements'][0].update(minimum=1, target=1, maximum=1)
        shift['participants'] = [_assignment(shift, pid)]
        data['shifts'].append(shift)
        if day in focal:
            data['focus_ids'].append(shift['id'])
    selected = {}
    if ranked:
        rank = next(row['category_id'] for row in data['memberships'] if row['person_id'] == pid)
        selected = {'category_ids': [rank]}
    p = policy(data, rule, selected, severity='advisory' if rule['kind'] in {
        'run_length', 'days_off_run', 'weekend_shape', 'shift_succession', 'balance_goal'} else 'mandatory')
    p.update(starts_on=start, ends_on=end)
    data['policies'] = [p]
    return data


def findings(data):
    return [{k: f.get(k) for k in ('code', 'start', 'end', 'detail')}
            for f in evaluate(data)['findings'] if f.get('rule_id') == data['policies'][0]['id']]


CASES = [
    ('days', {'kind': 'max_consecutive_days', 'maximum': 2},
     ['2026-11-29', '2026-11-30', '2026-12-01'], 'CONSECUTIVE_DAYS', 'days', 3),
    ('weekends', {'kind': 'max_consecutive_weekends', 'maximum': 1, 'weekend_start': 4, 'weekend_days': 2},
     ['2026-11-27', '2026-12-04'], 'CONSECUTIVE_WEEKENDS', 'weekends', 2),
    ('weekly-duties', {'kind': 'max_duties', 'maximum': 1, 'window': 'week'},
     ['2026-11-30', '2026-12-01'], 'MAX_DUTIES', 'actual', 2),
    ('weekly-hours', {'kind': 'max_hours', 'hours': 4, 'window': 'week'},
     ['2026-11-30', '2026-12-01'], 'MAX_HOURS', 'actual', 8),
    ('rolling-duties', {'kind': 'max_duties', 'maximum': 1, 'window': 'rolling', 'window_days': 7},
     ['2026-11-30', '2026-12-01'], 'MAX_DUTIES', 'actual', 2),
    ('rolling-hours', {'kind': 'max_hours', 'hours': 4, 'window': 'rolling', 'window_days': 7},
     ['2026-11-30', '2026-12-01'], 'MAX_HOURS', 'actual', 8),
]


@pytest.mark.parametrize('name,rule,dates,code,metric,actual', CASES, ids=[c[0] for c in CASES])
@pytest.mark.parametrize('boundary', ['start', 'end'])
def test_full_counts_and_solver_agree_at_validity_boundary(counting_world, name, rule, dates, code, metric, actual, boundary):
    focal = [dates[-1]] if boundary == 'start' else [dates[0]]
    data = mirror(counting_world, dates, focal, rule,
                  '2026-12-01' if boundary == 'start' else '2026-09-01',
                  None if boundary == 'start' else '2026-12-01')
    rows = findings(data)
    assert rows and {row['code'] for row in rows} == {code}, rows
    assert {row['detail'][metric] for row in rows} == {actual}, rows
    # Only the focal duty is editable: published neighbour work is a single fixed commitment.
    for shift in data['shifts']:
        if shift['id'] in data['focus_ids']:
            shift['participants'] = []
    data['generation_options'] = {'mode': 'rebalance', 'shortlist_width': 0}
    models = []
    solved = generate(data, 3, workers=1, feasibility_only=True, _on_model=models.append)
    assert models, solved
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 3
    solver.parameters.num_search_workers = 1
    status = solver.solve(models[0])
    assert status == cp_model.INFEASIBLE, (solver.status_name(status), solved)
    data['policies'][0]['definition']['rule']['hours' if rule['kind'] == 'max_hours' else 'maximum'] = actual
    allowed = generate(data, 3, workers=1, feasibility_only=True)
    assert allowed['status'] == 'candidate_feasible' and len(allowed['additions']) == 1, allowed
    assert not [f for f in allowed['validation']['findings'] if f.get('rule_id') == data['policies'][0]['id']]


@pytest.mark.parametrize('dates,starts_on,weekends', [
    pytest.param(['2026-11-27', '2026-12-04', '2026-12-16'], '2026-12-10', 0, id='weekday-only-in-validity'),
    pytest.param(['2026-11-27', '2026-12-04'], '2026-12-10', 0, id='no-duty-in-validity'),
    pytest.param(['2026-11-27', '2026-12-04', '2026-12-16'], '2026-11-27', 2, id='weekends-in-validity'),
])
def test_consecutive_weekends_require_an_applicable_weekend_duty(counting_world, dates, starts_on, weekends):
    rule = {'kind': 'max_consecutive_weekends', 'maximum': 1, 'weekend_start': 4, 'weekend_days': 2}
    data = mirror(counting_world, dates, dates, rule, starts_on)
    [policy] = data['policies']
    rows = [row for row in evaluate(data)['findings'] if row['code'] == 'CONSECUTIVE_WEEKENDS']
    assert [(row['severity'], row['detail']['weekends']) for row in rows] == ([('error', weekends)] if weekends else [])

    a, b = aware(dates[0] + 'T00:00:00+03:00'), aware('2027-01-01T00:00:00+03:00')
    result = reach(data, [policy], a, b, details=True)
    [line] = result['details'][policy['id']]['lines']
    [person] = line['people']
    assert max(row['actual'] for row in person['counts']) == weekends

    for shift in data['shifts']:
        shift['participants'] = []
    data['generation_options'] = {'mode': 'rebalance', 'shortlist_width': 0}
    solved = generate(data, 5, workers=1, feasibility_only=True)
    assert solved['status'] == ('requirements_conflict' if weekends else 'candidate_feasible'), solved
    assert len(solved['additions']) == (0 if weekends else len(dates)), solved


@pytest.mark.parametrize('name,rule,dates,code,metric,actual', CASES[:4], ids=[c[0] for c in CASES[:4]])
def test_same_catalogue_numbers_have_identical_checks(counting_world, name, rule, dates, code, metric, actual):
    data = mirror(counting_world, dates, [dates[-1]], rule, '2026-09-01')
    pid = next(iter(data['people']))
    rank = next(row['category_id'] for row in data['memberships'] if row['person_id'] == pid)
    entry = {'days': 'days_in_a_row', 'weekends': 'weekends_in_a_row',
             'weekly-duties': 'duties_per_week', 'weekly-hours': 'hours_per_week'}[name]
    broad = catalogue(entry, line(id='same-line', starts_on='2026-09-01',
        numbers={'max': {'value': rule.get('maximum', rule.get('hours'))}}))
    broad['department_id'] = data['workspace']['department_id']
    narrow = copy.deepcopy(broad)
    narrow['definition']['rule']['lines'][0].update(starts_on='2026-12-01',
        who={'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [rank]}]})
    data['policies'] = [broad]
    before = findings(data)
    data['policies'] = [narrow]
    after = findings(data)
    assert before == after and after, (before, after)


@pytest.mark.parametrize('level', ['mandatory', 'high'])
@pytest.mark.parametrize('parent_start', ['2026-09-01', '2026-12-01'])
def test_inherited_rank_line_counts_work_before_its_validity(counting_world, level, parent_start):
    data = mirror(counting_world, ['2026-11-29', '2026-11-30', '2026-12-01'], ['2026-12-01'],
                  {'kind': 'max_consecutive_days', 'maximum': 2}, '2026-09-01')
    pid = next(iter(data['people']))
    rank = next(row['category_id'] for row in data['memberships'] if row['person_id'] == pid)
    p = catalogue('days_in_a_row',
        line(id='everyone', starts_on=parent_start, numbers={'max': {'value': 2, 'level': level}}),
        line(id='rank', starts_on='2026-12-01', numbers={'max': {'value': 2, 'level': None}},
             who={'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [rank]}]}))
    p['department_id'] = data['workspace']['department_id']
    data['policies'] = [p]
    [row] = findings(data)
    assert row['code'] == 'CONSECUTIVE_DAYS' and row['detail']['days'] == 3


@pytest.mark.parametrize('rule,dates,code', [
    ({'kind': 'run_length', 'minimum': 3}, ['2026-11-29', '2026-11-30', '2026-12-01'], None),
    ({'kind': 'run_length', 'maximum': 2}, ['2026-11-29', '2026-11-30', '2026-12-01'], 'RUN_TOO_LONG'),
    ({'kind': 'days_off_run', 'minimum': 2}, ['2026-11-29', '2026-12-01'], 'DAYS_OFF_SHORT'),
    ({'kind': 'weekend_shape', 'shape': 'complete',
      'weekend_start': 4, 'weekend_days': 2}, ['2026-12-04', '2026-12-05'], None),
])
def test_other_run_rules_count_before_validity(counting_world, rule, dates, code):
    data = mirror(counting_world, dates, [dates[-1]], rule, dates[-1])
    rows = findings(data)
    assert [r['code'] for r in rows] == ([code] if code else []), rows
    assert bool(shape_cost(data)) == bool(code)
    data['policies'][0]['starts_on'] = '2027-01-01'
    assert findings(data) == []  # Context alone cannot activate a line.
    assert shape_cost(data) == 0


@pytest.mark.parametrize('start,end,expected', [
    ('2026-11-30', '2026-12-01', ['DAYS_OFF_SHORT']),
    ('2026-12-01', None, []),
    ('2026-09-01', '2026-11-30', []),
])
def test_days_off_between_fixed_duties_requires_an_effective_focus_day(counting_world, start, end, expected):
    data = mirror(counting_world, ['2026-11-29', '2026-11-30', '2026-12-01'], ['2026-11-30'],
                  {'kind': 'days_off_run', 'minimum': 2}, start, end)
    data['shifts'][1]['participants'] = []
    assert [row['code'] for row in findings(data)] == expected
    assert bool(shape_cost(data)) == bool(expected)


def shape_cost(data):
    from packages.domain.time import aware
    model, flags = cp_model.CpModel(), {}
    pid = next(iter(data['people']))
    entries = [(s, p, aware(p['start']), aware(p['end']), model.new_constant(1))
               for s in data['shifts'] for p in s['participants']]
    add_patterns(model, data, {pid: entries}, {'medium': 1}, shape_breaches=flags)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 3
    solver.parameters.num_search_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    return sum(solver.value(flag) for group in flags.values() for flag in group)


@pytest.mark.parametrize('period', [{'period': 'month'}, {'period': 'rolling', 'period_days': 31}])
def test_fairness_already_counts_the_full_month_or_rolling_window(counting_world, period):
    from packages.domain.fairness import balance_goals, goal_report
    data = mirror(counting_world, ['2026-12-01', '2026-12-15'], ['2026-12-01', '2026-12-15'],
                  {'kind': 'balance_goal', 'metric': 'duties', **period}, '2026-12-15')
    data['workspace'].update(starts_on='2026-12-01', ends_on='2027-01-01')
    [goal] = balance_goals(data)
    [person] = goal_report(data, goal)['people']
    assert person['planned'] == 2


@pytest.mark.parametrize('kind', ['max_consecutive_days', 'max_consecutive_weekends'])
def test_two_neighbour_duties_in_one_day_or_weekend_count_once(counting_world, kind):
    weekends = kind == 'max_consecutive_weekends'
    dates = ['2026-11-27', '2026-11-28', '2026-12-04'] if weekends else [
        '2026-11-29', '2026-11-30', '2026-12-01']
    rule = {'kind': kind, 'maximum': 1 if weekends else 2}
    if weekends:
        rule.update(weekend_start=4, weekend_days=2)
    data = mirror(counting_world, dates, [dates[-1]], rule, '2026-12-01')
    if not weekends:
        extra = copy.deepcopy(data['shifts'][1])
        extra.update(id='second-neighbour-duty', start=dates[1] + 'T14:00:00+03:00', end=dates[1] + 'T18:00:00+03:00')
        extra['participants'] = [_assignment(extra, next(iter(data['people'])))]
        data['shifts'].append(extra)
    [row] = findings(data)
    assert row['detail']['weekends' if weekends else 'days'] == (2 if weekends else 3)


def test_published_neighbour_is_loaded_once_at_the_line_boundary(h):
    pid = h.seed['people'][0]
    h.policy({'kind': 'max_consecutive_days', 'maximum': 2})
    old = h.workspace(start='2026-11-29', end='2026-12-01')
    for day in ['2026-11-29', '2026-11-30']:
        old = h.shift(old, start=day + 'T08:00:00+03:00', end=day + 'T12:00:00+03:00', people=[pid])['workspace']
    h.publish(old)
    new = h.shift(h.workspace(start='2026-12-01', end='2027-01-01'),
                  start='2026-12-01T08:00:00+03:00', end='2026-12-01T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(new)
    assert len(data['shifts']) == len({s['id'] for s in data['shifts']}) == 3
    focal = next(s for s in data['shifts'] if s['id'] in data['focus_ids'])
    focal['participants'] = [_assignment(focal, pid)]
    cap = next(p for p in data['policies'] if p['definition']['rule']['kind'] == 'max_consecutive_days')
    cap['starts_on'] = '2026-12-01'
    data['policies'] = [cap]
    [row] = findings(data)
    assert row['code'] == 'CONSECUTIVE_DAYS' and row['detail']['days'] == 3


def test_published_neighbours_after_line_end_count_once_in_full(h):
    pid = h.seed['people'][0]
    h.policy({'kind': 'max_consecutive_days', 'maximum': 2})
    published = h.workspace('December published continuation', start='2026-12-01', end='2026-12-03')
    for day in ('01', '02'):
        published = h.shift(published, start=f'2026-12-{day}T08:00:00+03:00',
                            end=f'2026-12-{day}T12:00:00+03:00', people=[pid])['workspace']
    issued = h.publish(published)
    draft = h.workspace('November focal duty', start='2026-11-30', end='2026-12-03')
    draft = h.shift(draft, start='2026-11-30T08:00:00+03:00', end='2026-11-30T12:00:00+03:00',
                    people=[pid])['workspace']
    data = h.snapshot(draft)
    assert issued['publication_id']
    assert {s['workspace_id'] for s in data['shifts']} == {published['id'], draft['id']}
    cap = next(p for p in data['policies'] if p['definition']['rule']['kind'] == 'max_consecutive_days')
    cap['starts_on'], cap['ends_on'] = '2026-11-01', '2026-12-01'
    data['policies'] = [cap]
    rows = findings(data)
    assert len(rows) == 1 and rows[0]['code'] == 'CONSECUTIVE_DAYS'
    assert rows[0]['detail']['days'] == 3


def test_three_draft_mirrors(counting_world, tmp_path):
    census = {}
    for name, dates, start, end in [
        ('December', ['2026-11-29', '2026-11-30', '2026-12-01'], '2026-12-01', '2027-01-01'),
        ('November approved leave cover', ['2026-10-30', '2026-10-31', '2026-11-01'], '2026-11-01', '2026-12-01'),
        ('8-14 November', ['2026-11-06', '2026-11-07', '2026-11-08'], '2026-11-08', '2026-11-15'),
    ]:
        data = mirror(counting_world, dates, [dates[-1]], {'kind': 'max_consecutive_days', 'maximum': 2}, start)
        data['workspace'].update(starts_on=start, ends_on=end)
        census[name] = findings(data)
    (tmp_path / 'findings.json').write_text(json.dumps(census, indent=2), encoding='utf-8')
    assert all(len(rows) == 1 and rows[0]['detail']['days'] == 3 for rows in census.values()), census


def published_pair_world(h, rule, *, previous, current, locations=()):
    """Build a real published neighbour and a current focal duty through the fixture lifecycle."""
    pid = h.seed['people'][0]
    h.policy(rule, severity='mandatory')
    old = h.workspace('Published boundary neighbour', start=previous[0][:10], end='2026-12-01')
    old = h.shift(old, start=previous[0], end=previous[1], people=[pid],
                  location=locations[0] if locations else 0)['workspace']
    issued = h.publish(old)
    current_workspace = h.workspace('Current boundary duty', start='2026-12-01', end='2027-01-01')
    current_workspace = h.shift(current_workspace, start=current[0], end=current[1], people=[pid],
                                 location=locations[1] if locations else 0)['workspace']
    data = h.snapshot(current_workspace)
    assert issued['publication_id']
    assert {s['workspace_id'] for s in data['shifts']} == {old['id'], current_workspace['id']}
    p = next(p for p in data['policies'] if p['definition']['rule']['kind'] == rule['kind'])
    p['starts_on'], p['ends_on'] = '2026-12-01', None
    data['policies'] = [p]
    return data


def test_min_rest_counts_published_neighbour_before_line_start(h):
    data = published_pair_world(h, {'kind': 'min_rest', 'hours': 8},
                                previous=('2026-11-30T20:00:00+03:00', '2026-11-30T22:00:00+03:00'),
                                current=('2026-12-01T00:00:00+03:00', '2026-12-01T01:00:00+03:00'))
    assert [row['code'] for row in findings(data)] == ['MIN_REST']


def test_travel_buffer_counts_published_neighbour_before_line_start(h):
    locations = h.seed['locations'][:2]
    data = published_pair_world(h, {'kind': 'travel_buffer', 'minutes': 300,
        'from_location_id': locations[0], 'to_location_id': locations[1]},
        previous=('2026-11-30T20:00:00+03:00', '2026-11-30T22:00:00+03:00'),
        current=('2026-12-01T00:00:00+03:00', '2026-12-01T01:00:00+03:00'), locations=(0, 1))
    assert [row['code'] for row in findings(data)] == ['TRAVEL_BUFFER']


def test_forbidden_transition_counts_published_neighbour_before_line_start(h):
    assignment_type = h.seed['type']
    data = published_pair_world(h, {'kind': 'forbidden_transition',
        'from_type_ids': [assignment_type], 'to_type_ids': [assignment_type]},
        previous=('2026-11-30T20:00:00+03:00', '2026-11-30T22:00:00+03:00'),
        current=('2026-12-01T00:00:00+03:00', '2026-12-01T01:00:00+03:00'))
    assert [row['code'] for row in findings(data)] == ['FORBIDDEN_TRANSITION']


def test_v2_weekly_and_monthly_minimums_and_targets_are_block_quotas():
    entry = V2_CATALOGUE_BY_ID['workload_limit']
    assert CATALOGUE_MAPPING['duties_per_week'] == CATALOGUE_MAPPING['duties_per_month'] == 'workload_limit'
    assert {slot['kind'] for key in ('min', 'target') for slot in entry['numbers'][key]} == {'block_quota'}
    assert 'block_quota' not in VALIDITY_FOCAL_ONLY_KINDS
    assert VALIDITY_FOCAL_ONLY_KINDS < WORKLOAD_COUNT_KINDS
    assert SHAPE_KINDS == {'run_length', 'days_off_run', 'weekend_shape', 'shift_succession', 'together_apart'}


def _shape_breach_flags(data, assigned_focal):
    model, flags = cp_model.CpModel(), {}
    pid = next(iter(data['people']))
    entries = []
    for s in data['shifts']:
        for p in s['participants']:
            if s['id'] in data['focus_ids']:
                x = model.new_bool_var('focal')
                model.add(x == int(assigned_focal))
            else:
                x = model.new_constant(1)
            entries.append((s, p, aware(p['start']), aware(p['end']), x))
    add_patterns(model, data, {pid: entries}, {'medium': 1}, shape_breaches=flags)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    return sum(solver.value(f) for group in flags.values() for f in group)


def test_multi_row_shape_breach_is_gated_like_validation(counting_world):
    """A three-day weekend shape has several rows per breach; with the focal day unassigned no breach is charged."""
    rule = {'kind': 'weekend_shape', 'shape': 'complete', 'weekend_start': 4, 'weekend_days': 3}
    dates, start = ['2026-12-04', '2026-12-05'], '2026-12-05'
    data = mirror(counting_world, dates, [dates[-1]], rule, start)
    with_focal = [r['code'] for r in findings(data)]
    cost_with = _shape_breach_flags(data, True)
    focal = next(s for s in data['shifts'] if s['id'] in data['focus_ids'])
    saved, focal['participants'] = focal['participants'], []
    without_focal = [r['code'] for r in findings(data)]
    focal['participants'] = saved
    cost_without = _shape_breach_flags(data, False)
    assert with_focal and cost_with
    assert not without_focal and not cost_without, (without_focal, cost_without)

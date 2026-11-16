"""Score arithmetic parity on small quantities and changing assignment subjects."""
from copy import deepcopy
import json
from fractions import Fraction
from math import isqrt

import pytest
from ortools.sat.python import cp_model

from packages.domain.cp_sat import generate
from packages.domain.cp_sat_score import SCORE_SCALE, ScoreModel, rule_component, workload_component, stability_component, coverage_component, weighted_component
from packages.domain.score import schedule_score
from packages.domain.score_normalization import workload_normalization
from packages.domain.validation import evaluate
from tests.test_schedule_score import data, shift


def test_score_bounds_use_extremes_for_holed_domains_and_fixed_boolean_literals():
    model = cp_model.CpModel()
    holed = model.new_int_var_from_domain(cp_model.Domain.from_intervals([[-7, -4], [2, 9]]), 'holed')
    other = model.new_int_var(-2, 5, 'other')
    chosen = model.new_bool_var('chosen')
    fixed = model.new_constant(1)
    score = ScoreModel(model)
    assert score.bounds(holed) == (-7, 9)
    assert score.bounds(2 * holed - 3 * other + 5) == (-24, 29)
    assert score.bounds(chosen.Not()) == (0, 1)
    assert score.bounds(fixed.Not()) == (0, 0)
    assert score.bounds(7 + 3 * fixed) == (10, 10)


@pytest.mark.parametrize('weights', [(35, 25, 20, 15, 5), (100, 99, 98, 97, 96), (0, 0, 0, 0, 0),
    (2_000,) * 5, (1_000_000,) * 5, (1_000_000, 999_999, 999_997, 999_983, 999_979)])
def test_active_weight_mean_preserves_every_subset_score_order_and_tie(weights):
    model = cp_model.CpModel()
    present = [model.new_bool_var('present') for _ in weights]
    component_losses = [SCORE_SCALE, SCORE_SCALE // 2 + 1, 1, 0, SCORE_SCALE // 2]
    components = {str(i): (loss, active) for i, (loss, active) in enumerate(zip(component_losses, present))}
    losses, scale = weighted_component(model, components, dict(zip(components, weights)), 750_001)
    radix = isqrt(scale // SCORE_SCALE) if len(losses) > 1 else 1
    values = []

    class Collect(cp_model.CpSolverSolutionCallback):
        def on_solution_callback(self):
            active_weights = [weight * self.value(active) for weight, active in zip(weights, present)]
            expected = Fraction(sum(w * v for w, v in zip(active_weights, component_losses)), sum(active_weights) or 1)
            encoded = self.value(losses[0]) if len(losses) == 1 else radix * self.value(losses[0]) + self.value(losses[1])
            assert abs(Fraction(encoded, scale) - expected / SCORE_SCALE) < Fraction(1, SCORE_SCALE)
            values.append((expected, encoded))

    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.enumerate_all_solutions = True
    assert solver.solve(model, Collect()) == cp_model.OPTIMAL, model.validate()
    assert len(values) == 32
    for (left, a), (right, b) in zip(sorted(values), sorted(values)[1:]):
        assert (left < right) == (a < b)
        assert (left == right) == (a == b)
    # The largest supported planning slice still has a valid int64 objective.
    assignments = model.new_int_var(0, 750_000, 'assignments')
    solver.parameters.enumerate_all_solutions = False
    for index, part in enumerate(losses):
        objective = 750_001 * part + assignments if index == len(losses) - 1 else part
        model.minimize(objective)
        assert not model.validate()
        assert solver.solve(model) == cp_model.OPTIMAL
        model.add(objective == solver.value(objective))
    assert ScoreModel(model).bounds(750_001 * losses[-1] + assignments)[1] < 2**62


@pytest.mark.parametrize('extra_tick', [0, 1])
def test_fractional_weighted_tick_precedes_assignment_tie_break(extra_tick):
    model = cp_model.CpModel()
    present = model.new_bool_var('preference_active')
    losses, _ = weighted_component(model, {'rules': (SCORE_SCALE // 2, 1),
        'preferences': (SCORE_SCALE // 2 + extra_tick, present)}, {'rules': 100, 'preferences': 1})
    model.minimize(2 * losses[0] + 1 - present)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
    assert solver.value(present) == 1 - extra_tick


@pytest.mark.parametrize('equal', [False, True])
def test_large_coprime_weights_preserve_sub_tick_comparisons_before_assignment_ties(equal):
    model = cp_model.CpModel()
    alternative = model.new_bool_var('alternative')
    weights = {'a': 1_000_000, 'b': 999_999, 'c': 999_997}
    # The second ratio wins by just 1/(1,999,999 * 1,999,997) of one component tick.
    expected = [Fraction(1_000_000 * 500_000 + 999_999 * 500_001, 1_999_999),
                Fraction(1_000_000 * 166_668 + 999_997 * 833_334, 1_999_997)]
    assert expected[1] < expected[0]
    components = {'a': (500_000 - 333_332 * alternative, 1),
                  'b': (500_001, 1 - alternative), 'c': (833_334, alternative)}
    if equal:
        components = {name: (500_000, present) for name, (_, present) in components.items()}
    losses, _ = weighted_component(model, components, weights, 750_001)
    assert len(losses) == 2
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    for index, part in enumerate(losses):
        objective = 750_001 * part + alternative if index == len(losses) - 1 else part
        model.minimize(objective)
        assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
        model.add(objective == solver.value(objective))
    assert solver.value(alternative) == (0 if equal else 1)


@pytest.mark.parametrize('variable_loss', [False, True])
def test_large_fixed_denominator_keeps_constant_objective_losses_exact(variable_loss):
    model = cp_model.CpModel()
    change = model.new_bool_var('change')
    components = {'rules': (333_333, 1), 'stability': (SCORE_SCALE * change if variable_loss else 0, 1)}
    losses, _ = weighted_component(model, components, {'rules': 999_999, 'stability': 1_000_000}, 750_001)
    assignments = model.new_int_var(0, 750_000, 'assignments')
    model.add(assignments == 750_000 * (1 - change))
    objective = 750_001 * losses[0] + assignments
    model.minimize(objective)
    assert model.proto.objective.offset == 0  # Large offsets would lose integer bits in the proto's double.
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
    expected_change = int(not variable_loss)
    assert solver.value(change) == expected_change
    expected = 750_001 * 999_999 * 333_333 + 750_000 * (1 - expected_change)
    assert expected > 2**53
    assert solver.value(objective) == expected


def test_large_score_weights_keep_configured_fill_stages_before_precision_prefix(h):
    first = h.seed['people'][0]
    h.policy({'kind': 'allocation_preference', 'priority': 1, 'session_priority': 1},
             {'person_id': first}, severity='advisory')
    workspace = h.shift(h.workspace(), people=[])['workspace']
    snapshot = h.snapshot(workspace)
    snapshot['generation_options'] = {'allocation_order': 'tiered', 'scoring': {'weights': {
        'coverage': 1_000_000, 'rules': 999_999, 'fairness': 999_997, 'preferences': 999_983, 'stability': 999_979}}}
    captured = []

    def capture(model, objectives):
        assert model.validate() == ''
        captured.extend(name for name, _, _ in objectives)

    result = generate(snapshot, 5, deterministic=True, _on_objectives=capture)
    assert result['status'] == 'model_ready', result
    stages = [name for name in captured if name.startswith('fill:')]
    assert stages and 'weighted_prefix' in captured
    assert captured[0] == 'coverage' and captured[-1] == 'weighted'
    assert max(captured.index(name) for name in stages) < captured.index('weighted_prefix')


@pytest.mark.parametrize('counts, minimum, targets, expected', [
    ((0,), 1, (2,), Fraction(1)),
    ((1,), 1, (2,), Fraction(1, 4)),
    ((2,), 1, (2,), Fraction(0)),
    ((1,), 0, (2,), Fraction(1, 8)),
    ((2,), 0, (2,), None),
    ((0,), 0, (), None),
    ((1, 2), 0, (2, 3), Fraction(5, 44)),
    ((0, 1), 1, (2,), Fraction(4, 9)),
])
def test_compiled_coverage_has_the_score_loss_and_active_weight(counts, minimum, targets, expected):
    model = cp_model.CpModel()
    rows = []
    for count, duration in zip(counts, (30, 60)):
        cover = model.new_int_var(0, 3, 'eligible_cover')
        model.add(cover == count)
        rows.append((cover, minimum, targets, duration * 60_000_000))
    loss, present = coverage_component(model, rows)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
    assert solver.value(present) == int(expected is not None)
    if expected is not None:
        assert solver.value(loss) / SCORE_SCALE == pytest.approx(float(expected), abs=0.000002)


@pytest.mark.parametrize('minutes', [(15, 30), (240, 480), (30, 30), (0, 240)])
@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
@pytest.mark.parametrize('partial', [False, True])
def test_workload_component_matches_score_with_sub_hour_and_absent_people(minutes, measure, partial):
    from datetime import timedelta
    from packages.domain.time import aware
    sessions = []
    for pid, amount in zip(('a', 'b'), minutes):
        if not amount:
            continue
        session = shift(pid, 4, [pid])
        session['end'] = (aware(session['start']) + timedelta(minutes=amount)).isoformat()
        session['participants'][0]['end'] = session['end']
        session['type_snapshot']['definition'] = {'requirements': [{'minimum': 1}]}
        if partial:
            session['participants'][0]['end'] = (aware(session['start']) + timedelta(minutes=amount / 2)).isoformat()
        sessions.append(session)
    snapshot = data(sessions)
    model = cp_model.CpModel()
    choices, participants = {}, {}
    for session in sessions:
        assigned = {p['person_id']: p for p in session['participants']}
        for pid in snapshot['people']:
            key = session['id'], pid
            participants[key] = assigned.get(pid, {'person_id': pid, 'start': session['start'], 'end': session['end']})
            choices[key] = model.new_bool_var('assigned')
            model.add(choices[key] == int(pid in assigned))
    loss, present = workload_component(model, snapshot, choices, participants, measure)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 5
    assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
    scored = schedule_score(snapshot, {'findings': [], 'coverage': []}, {'balance_measure': measure})
    json.dumps(scored)  # fractional cover budgets must still produce persistable scores
    expected = scored['components']['fairness']['value']
    assert bool(solver.value(present)) == (expected is not None)
    if expected is not None:
        assert 1 - solver.value(loss) / SCORE_SCALE == pytest.approx(expected, abs=0.000051)


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
@pytest.mark.parametrize('inactive_flag', ['cancelled', 'closed'])
def test_workload_budget_uses_required_and_desired_windows_without_counting_overlap_twice(measure, inactive_flag):
    session = shift('s', 4, ['a'])
    session['type_snapshot']['definition'] = {'requirements': [
        {'minimum': 1, 'target': 2, 'from_minute': 0, 'to_minute': 240},
        {'minimum': 3, 'from_minute': 120, 'to_minute': 360}]}
    outside = {**shift('outside', 5, ['a']), inactive_flag: True}
    snapshot = data([session, outside])
    audience, demand = workload_normalization(snapshot, measure)
    # Two hours at 2, four at 3 and two at 0: 16 seat-hours over an 8-hour duty.
    assert audience == tuple('abcd')
    assert demand == (Fraction(2) if measure == 'duties' else 16 * 3_600_000_000)


@pytest.mark.parametrize('assignments,hours,perfect', [
    ('a', (4,), True), ('aabc', (4, 4, 4, 4), True),
    ('aaab', (4, 4, 4, 4), False), ('ab', (4, 1), True)])
@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
def test_indivisible_workload_shares_match_solver_and_padding_never_improves(assignments, hours, perfect, measure):
    from datetime import timedelta
    from packages.domain.time import aware
    from tests.test_schedule_score import coverage

    sessions, covered = [], []
    for i, (pid, length) in enumerate(zip(assignments, hours)):
        session = shift('s' + str(i), 4 + i, [pid])
        session['end'] = (aware(session['start']) + timedelta(hours=length)).isoformat()
        session['participants'][0]['end'] = session['end']
        sessions.append(session)
        covered.append({**coverage(session['id'], 4 + i, 1, 1), 'end': session['end']})
    snapshot = data(sessions)
    snapshot['people'] = {pid: snapshot['people'][pid] for pid in 'abc'}
    validation, options = {'coverage': covered, 'findings': []}, {'balance_measure': measure}
    result = schedule_score(snapshot, validation, options)
    if perfect:
        assert result['score'] == result['people']['a']['score'] == 100
        assert result['components']['fairness']['value'] == 1
    else:
        assert result['score'] < 100 and result['people']['a']['score'] < 100
        assert result['components']['fairness']['value'] < 1
    model = cp_model.CpModel()
    participants = {(s['id'], p['person_id']): p for s in sessions for p in s['participants']}
    loss, present = workload_component(model, snapshot, dict.fromkeys(participants, 1), participants, measure)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 5
    assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
    assert solver.value(present) == 1
    assert 1 - solver.value(loss) / SCORE_SCALE == pytest.approx(result['components']['fairness']['value'], abs=0.000051)
    for session in sessions:
        for pid in 'abc':
            if any(p['person_id'] == pid for p in session['participants']):
                continue
            session['participants'].append({**session['participants'][0], 'person_id': pid})
            padded = schedule_score(snapshot, validation, options)
            assert padded['components']['fairness']['value'] <= result['components']['fairness']['value']
            assert padded['score'] <= result['score']
            result = padded


@pytest.mark.parametrize('excluded', ['inactive', 'membership', 'rank', 'service', 'leave', 'rotation', 'restriction'])
@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
def test_workload_audience_uses_the_same_eligible_people_for_score_and_solver(excluded, measure):
    from tests.test_schedule_score import policy

    snapshot = data([shift('s1', 4, ['a']), shift('s2', 5, ['b'])])
    if excluded == 'inactive':
        for pid in 'cd':
            snapshot['people'][pid]['active'] = False
    elif excluded == 'membership':
        snapshot['memberships'] = [r for r in snapshot['memberships'] if r['person_id'] in 'ab']
    elif excluded == 'rank':
        snapshot['categories']['other'] = {}
        for row in snapshot['memberships']:
            if row['person_id'] in 'cd':
                row['category_id'] = 'other'
        for session in snapshot['shifts']:
            session['type_snapshot']['definition']['allowed_category_ids'] = ['rank']
    elif excluded == 'service':
        for session in snapshot['shifts']:
            session['service_ids'] = ['clinic']
        snapshot['eligibilities'] = [{'person_id': pid, 'service_id': 'clinic'} for pid in 'ab']
    elif excluded == 'leave':
        snapshot['availability'] = [{'id': pid, 'person_id': pid, 'start': '2026-10-04T00:00:00+00:00',
                                     'end': '2026-10-06T00:00:00+00:00'} for pid in 'cd']
    elif excluded == 'rotation':
        snapshot['rotations'] = [{'id': pid, 'person_id': pid, 'department_available': False,
                                 'starts_on': '2026-10-04', 'ends_on': '2026-10-06'} for pid in 'cd']
    else:
        restricted = policy('restricted', severity='mandatory')
        restricted['revision'] = 1
        restricted['definition'].update(scope={'person_ids': ['c', 'd']},
            rule={'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'})
        snapshot['policies'].append(restricted)
    original = deepcopy(snapshot)
    audience, demand = workload_normalization(snapshot, measure)
    assert audience == ('a', 'b')
    assert demand == (2 if measure == 'duties' else 16 * 3_600_000_000)
    model = cp_model.CpModel()
    participants = {(s['id'], p['person_id']): p for s in snapshot['shifts'] for p in s['participants']}
    loss, present = workload_component(model, snapshot, dict.fromkeys(participants, 1), participants, measure)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 5
    assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
    scored = schedule_score(snapshot, {'findings': [], 'coverage': []}, {'balance_measure': measure})
    assert scored['components']['fairness']['value'] == 1
    assert solver.value(present) == 1 and solver.value(loss) == 0
    assert snapshot == original
    session = snapshot['shifts'][0]
    session['participants'].append({**session['participants'][0], 'person_id': 'b'})
    assert workload_normalization(snapshot, measure) == (audience, demand)
    padded = schedule_score(snapshot, {'findings': [], 'coverage': []}, {'balance_measure': measure})
    assert padded['components']['fairness']['value'] <= scored['components']['fairness']['value']


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
def test_workload_audience_checks_qualification_before_candidate_evidence(monkeypatch, measure):
    snapshot = data([shift('s', 4, [])])
    snapshot['people']['c']['active'] = False
    snapshot['memberships'] = [row for row in snapshot['memberships'] if row['person_id'] != 'd']
    checked = set()

    def evidence(data, pid, session):
        assert pid in ('a', 'b'), 'Ineligible people must not need candidate evidence'
        checked.add(pid)
        return {'eligible_and_available': pid == 'a'}

    monkeypatch.setattr('packages.domain.candidate_evidence.candidate_evidence', evidence)
    assert workload_normalization(snapshot, measure) == (('a',), 1 if measure == 'duties' else 8 * 3_600_000_000)
    assert checked == {'a', 'b'}


@pytest.mark.parametrize('excluded', ['nonfocused', 'cancelled', 'closed'])
def test_workload_audience_ignores_eligibility_only_on_excluded_sessions(excluded):
    snapshot = data([shift('outside', 3, []), shift('focused', 4, ['a'])])
    for row in snapshot['memberships']:
        if row['person_id'] in 'cd':
            row['ends_on'] = '2026-10-04'
    if excluded == 'nonfocused':
        snapshot['focus_ids'] = ['focused']
    else:
        snapshot['shifts'][0][excluded] = True
    assert workload_normalization(snapshot, 'duties') == (('a', 'b'), 1)


@pytest.mark.parametrize('inactive_flag', ['cancelled', 'closed'])
@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
def test_inactive_duty_never_dilutes_rule_or_workload_loss(inactive_flag, measure):
    from tests.test_schedule_score import policy

    sessions = [shift(f's{i}', i + 4, [pid]) for i, pid in enumerate('aaab')]
    rule = policy('cover')
    rule['definition']['rule'] = {'kind': 'minimum_cover', 'minimum': 2}
    snapshot = data(sessions, [rule])
    snapshot['people'] = {pid: snapshot['people'][pid] for pid in 'ab'}
    validation = {'coverage': [], 'findings': [{'code': 'SCOPED_COVERAGE_GAP', 'severity': 'warning',
        'rule_id': rule['id'], 'person_id': None, 'shift_id': 's0', 'detail': {}}]}
    options = {'balance_measure': measure}
    before = schedule_score(snapshot, validation, options)
    assert before['components']['rules']['value'] < 1
    assert before['components']['fairness']['value'] < 1
    excluded = {**shift('excluded', 8, ['a']), inactive_flag: True, 'end': '2026-10-09T08:00:00+00:00'}
    excluded['participants'][0]['end'] = excluded['end']
    snapshot['shifts'].append(excluded)
    snapshot['focus_ids'].append('excluded')
    after = schedule_score(snapshot, validation, options)
    assert after['components'] == before['components']
    assert after['uncapped_score'] == before['uncapped_score']


@pytest.mark.parametrize('requirements', [[], [{'minimum': 0}]])
@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
def test_zero_workload_budget_stays_neutral_when_unneeded_people_are_added(requirements, measure):
    session = shift('s', 4, [])
    session['type_snapshot']['definition']['requirements'] = requirements
    snapshot = data([session])
    for count in range(5):
        session['participants'] = shift('s', 4, 'abcd'[:count])['participants']
        assert workload_normalization(snapshot, measure) == (tuple('abcd'), 0)
        result = schedule_score(snapshot, {'findings': [], 'coverage': []}, {'balance_measure': measure})
        assert result['components']['fairness']['value'] == (1 if count else None)
        assert all(row['fairness'] == 1 for row in result['people'].values())
        model = cp_model.CpModel()
        choices, participants = {}, {}
        for pid in 'abcd':
            key = session['id'], pid
            participants[key] = shift('s', 4, [pid])['participants'][0]
            choices[key] = model.new_bool_var('assigned')
            model.add(choices[key] == int(pid in 'abcd'[:count]))
        loss, present = workload_component(model, snapshot, choices, participants, measure)
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        solver.parameters.max_time_in_seconds = 5
        assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
        assert solver.value(present) == 1 and solver.value(loss) == 0


@pytest.mark.parametrize('minimum', [0, 1])
def test_rule_denominator_is_fixed_before_first_assignment_and_empty_rota_scores_zero(minimum):
    from tests.test_schedule_score import policy

    session = shift('s', 4, [])
    session['type_snapshot']['definition'] = {'requirements': [{'minimum': minimum}]}
    snapshot = data([session], [policy('advisory')])
    before = schedule_score(snapshot, {'findings': [], 'coverage': []})
    session['participants'] = shift('s', 4, ['a'])['participants']
    after = schedule_score(snapshot, {'findings': [], 'coverage': []})
    assert before['components']['rules']['value'] == 1
    assert before['components']['fairness']['value'] is None
    assert before['uncapped_score'] == 0
    assert after['components']['rules']['value'] == after['components']['fairness']['value'] == 1
    assert after['uncapped_score'] == 100


@pytest.fixture
def covered_advisory_snapshot(h):
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, severity='advisory')
    workspace = h.shift(h.workspace())['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00')['workspace']
    return h.snapshot(workspace)


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
def test_fill_gaps_does_not_add_people_to_dilute_an_existing_advisory_breach(covered_advisory_snapshot, measure):
    snapshot = covered_advisory_snapshot
    before = schedule_score(snapshot, evaluate(snapshot), {'balance_measure': measure})
    assert before['components']['coverage']['value'] == 1
    assert before['components']['rules']['value'] < 1
    snapshot['generation_options'] = {'mode': 'fill_gaps', 'balance_measure': measure, 'shortlist_width': 0}
    result = generate(snapshot, 5, deterministic=True)
    assert result['status'] == 'candidate_feasible', result
    assert result['additions'] == result['removals'] == []
    assert result['summary']['score_before'] == result['summary']['score']


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
def test_saved_score_cannot_improve_by_adding_unneeded_people(covered_advisory_snapshot, measure):
    snapshot = covered_advisory_snapshot
    options = {'balance_measure': measure}
    before = schedule_score(snapshot, evaluate(snapshot), options)
    padded = deepcopy(snapshot)
    session = padded['shifts'][0]
    first = session['participants'][0]
    session['participants'] += [{**first, 'id': 'extra-' + pid, 'person_id': pid}
                                for pid in snapshot['people'] if pid != first['person_id']]
    after = schedule_score(padded, evaluate(padded), options)
    assert before['components']['coverage']['value'] == after['components']['coverage']['value'] == 1
    assert before['components']['rules']['value'] == after['components']['rules']['value'] < 1
    assert before['components']['fairness']['value'] is not None
    assert after['components']['fairness']['value'] <= before['components']['fairness']['value']
    assert after['uncapped_score'] <= before['uncapped_score']


def test_rule_component_keeps_a_fixed_denominator_with_unassigned_choices(covered_advisory_snapshot):
    from packages.domain.conditions import active_rules
    from packages.domain.score import rule_copy_key, settings_of
    from packages.domain.validation import finding_observations

    snapshot = covered_advisory_snapshot
    validation = evaluate(snapshot)
    snapshot['policies'] = active_rules(snapshot['policies'], context=snapshot)
    policies = {policy['id']: policy for policy in snapshot['policies']}
    model = cp_model.CpModel()
    choices, participants, breaches = {}, {}, {}
    for session in snapshot['shifts']:
        assigned = {p['person_id']: p for p in session['participants']}
        for pid in snapshot['people']:
            key = session['id'], pid
            participants[key] = assigned.get(pid, {'person_id': pid, 'start': session['start'], 'end': session['end']})
            choices[key] = model.new_bool_var('assigned')
            model.add(choices[key] == int(pid in assigned))
    for finding in finding_observations(validation):
        if finding['rule_id'] in policies and finding['severity'] == 'warning' and finding['code'] not in {'BALANCE_GOAL', 'BALANCE_PAIR'}:
            key = rule_copy_key(policies[finding['rule_id']]), finding['person_id'] or finding['shift_id']
            breaches.setdefault(key, []).append(1)
    loss, present = rule_component(model, snapshot, choices, participants, breaches, [], settings_of(None)['strengths'])
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 5
    assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
    expected = schedule_score(snapshot, validation)['components']['rules']['value']
    assert solver.value(present) == 1
    assert 1 - solver.value(loss) / SCORE_SCALE == pytest.approx(expected, abs=0.000051)


def test_rule_opportunities_include_dated_partial_participation_before_anyone_is_assigned(h):
    from packages.domain.conditions import scope_matches_throughout
    from packages.domain.score import rule_copy_key, settings_of
    from packages.domain.score_normalization import rule_normalization
    from packages.domain.time import aware
    from packages.domain.validation import finding_observations, policy_applies

    first, second = h.seed['people'][:2]
    category = h.configure('categories', {'name': 'Dated workload cohort', 'starts_on': '2020-01-01'})['record']['id']
    for pid in (first, second):
        h.command('coordinator', 'membership.create', {'person_id': pid, 'category_id': category, 'starts_on': '2026-10-05'})
    rule = h.policy({'kind': 'max_hours', 'hours': 0.5, 'window': 'week'},
                    {'category_ids': [category]}, severity='advisory')
    typ = h.configure('assignment_types', {'name': 'Optional overnight opportunity', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0}]}})['record']['id']
    workspace = h.shift(h.workspace(), typ=typ, start='2026-10-04T23:00:00+03:00',
                        end='2026-10-05T01:00:00+03:00', people=[])['workspace']
    snapshot = h.snapshot(workspace)
    session, = snapshot['shifts']
    participant = {'id': 'partial', 'shift_id': session['id'], 'person_id': first,
                   'start': '2026-10-05T00:00:00+03:00', 'end': session['end'],
                   'participation': 'staff', 'overtime': False}
    assert not scope_matches_throughout(snapshot, rule['definition']['scope'], first, session,
                                        aware(session['start']), aware(session['end']))
    assert policy_applies(snapshot, rule, first, session, aware(session['start']), aware(session['end']))
    assert policy_applies(snapshot, rule, first, session, aware(participant['start']), aware(participant['end']))
    padded = deepcopy(snapshot)
    padded['shifts'][0]['participants'] = [participant]
    strengths = settings_of(None)['strengths']
    # Both people enter the cohort midway through the session: two fixed medium-strength opportunities.
    assert rule_normalization(snapshot, strengths, []) == rule_normalization(padded, strengths, []) == 6
    options = {'scoring': {'weights': {'coverage': 0, 'rules': 1, 'fairness': 0, 'preferences': 0, 'stability': 0}}}
    for source, assigned, expected in ((snapshot, False, 1), (padded, True, 0.5)):
        validation = evaluate(source)
        breaches = [row for row in finding_observations(validation) if row['code'] == 'MAX_HOURS']
        assert len(breaches) == int(assigned)
        scored = schedule_score(source, validation, options)
        assert scored['components']['rules']['value'] == expected
        assert scored['score'] == (100 * expected if assigned else 0)
        model = cp_model.CpModel()
        choices, participants = {}, {}
        for pid in source['people']:
            key = session['id'], pid
            participants[key] = participant if pid == first else {
                'person_id': pid, 'start': session['start'], 'end': session['end']}
            choices[key] = model.new_bool_var('assigned')
            model.add(choices[key] == int(assigned and pid == first))
        loss, present = rule_component(model, source, choices, participants,
                                       {(rule_copy_key(rule), first): [choices[session['id'], first]]}, [], strengths)
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        solver.parameters.max_time_in_seconds = 5
        assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
        assert solver.value(present) == 1
        assert 1 - solver.value(loss) / SCORE_SCALE == expected


def test_stability_empty_baseline_is_satisfied_and_filling_costs_nothing():
    snapshot = data([shift('open', 4, [])])
    model = cp_model.CpModel()
    loss, present, baseline = stability_component(model, snapshot, {}, {}, {})
    assert (loss, present, baseline) == (0, 1, {})


@pytest.mark.parametrize('comparison', ['minimum', 'maximum', 'target'])
@pytest.mark.parametrize('amount', [0, 0.25, 2])
def test_optional_quota_rule_loss_matches_score_before_and_after_assignment(h, comparison, amount):
    from packages.domain.cp_sat_quotas import add_quotas
    from packages.domain.score import settings_of

    person = h.seed['people'][0]
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'session_units', 'comparison': comparison, 'amount': amount},
             {'person_id': person}, severity='advisory')
    h.policy({'kind': 'max_duties', 'maximum': 10, 'window': 'week'},
             {'person_id': person}, severity='advisory')
    typ = h.configure('assignment_types', {'name': 'Optional quota opportunities', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']['id']
    workspace = h.workspace()
    for day in (4, 5, 6):
        workspace = h.shift(workspace, typ=typ, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00', people=[])['workspace']
    snapshot = h.snapshot(workspace)
    snapshot['people'] = {person: snapshot['people'][person]}
    options = {'scoring': {'weights': {'coverage': 0, 'rules': 1, 'fairness': 0, 'preferences': 0, 'stability': 0}}}
    for actual in range(4):
        model = cp_model.CpModel()
        choices, participants, by_person, quota_items = {}, {}, {person: []}, []
        for index, session in enumerate(snapshot['shifts']):
            participant = {'id': 'optional-' + session['id'], 'shift_id': session['id'], 'person_id': person,
                           'start': session['start'], 'end': session['end'], 'participation': 'staff', 'overtime': False}
            session['participants'] = [participant] if index < actual else []
            key = session['id'], person
            participants[key] = participant
            choices[key] = model.new_bool_var('assigned')
            model.add(choices[key] == int(index < actual))
            by_person[person].append((session, participant, None, None, choices[key]))
        assert add_quotas(model, snapshot, by_person, None, score_items=quota_items) == []
        loss, present = rule_component(model, snapshot, choices, participants, {}, quota_items, settings_of(None)['strengths'])
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
        deviation = (abs(actual - amount) if comparison == 'target' else
                     max(0, amount - actual) if comparison == 'minimum' else max(0, actual - amount))
        expected_loss = min(1, deviation / max(amount, 1)) / 2  # one quota row and one satisfied rule opportunity
        scored = schedule_score(snapshot, evaluate(snapshot), options, baseline={})
        assert solver.value(present) == 1
        assert solver.value(loss) / SCORE_SCALE == pytest.approx(expected_loss, abs=0.000001)
        assert scored['components']['rules']['value'] == round(1 - expected_loss, 4)
        assert scored['score'] == (round(100 * (1 - expected_loss), 1) if actual else 0)
        assert sum(row['points'] for row in scored['contributions']) == pytest.approx(100 - scored['score'])
        rules_points = sum(row['points'] for row in scored['contributions'] if row['component'] == 'rules')
        assert rules_points == pytest.approx(100 * expected_loss, abs=0.05)


def test_satisfied_rules_do_not_encourage_unneeded_optional_assignments(h):
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, severity='advisory')
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'target', 'amount': 0}, severity='advisory')
    typ = h.configure('assignment_types', {'name': 'Unneeded optional opportunities', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']['id']
    workspace = h.shift(h.workspace(), typ=typ, people=[])['workspace']
    result = generate(h.snapshot(workspace), 5)
    assert result['status'] == 'candidate_feasible'
    assert result['additions'] == result['removals'] == []
    assert result['summary']['score']['score'] == 0


@pytest.mark.parametrize('numerator,denominator', [
    (0, 0), (1, 0), (1, 3), (16_260_074_975_999, 16_260_074_976_000),
    (16_260_074_976_000, 16_260_074_976_000), (16_260_074_976_000, 7),
    (16_260_074_976_000, 13_008_059_980_800),
    (8_130_037_487_999, 16_260_074_976_000),
])
@pytest.mark.parametrize('bound', [SCORE_SCALE, 500_000, 1_500_000])
def test_large_ratio_keeps_exact_ticks_and_clipping(numerator, denominator, bound):
    model = cp_model.CpModel()
    top = 16_260_074_976_000
    n = model.new_int_var(0, top, 'numerator')
    d = model.new_int_var(0, top, 'denominator')
    ratio = ScoreModel(model).ratio(n, d, bound)
    model.add(n == numerator)
    model.add(d == denominator)
    assert model.validate() == ''
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.value(ratio) == min(bound, SCORE_SCALE * numerator // max(1, denominator))


def test_unsupported_score_precision_fails_explicitly():
    from packages.domain.cp_sat_score import ScorePrecisionError
    model = cp_model.CpModel()
    q = ScoreModel(model)
    quantity = model.new_int_var(0, 2**60, 'quantity')
    with pytest.raises(ScorePrecisionError, match='integer precision'):
        q.ratio(quantity, quantity + 1)



def test_workload_normalization_reuses_only_unchanged_trial_inputs(h, monkeypatch):
    from packages.domain import candidate_evidence
    a, b, c = h.seed['people']
    workspace = h.shift(h.workspace(), people=[a])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[b])['workspace']
    source = h.snapshot(workspace)
    original = deepcopy(source)
    context = {**source, '_workload_normalization_cache': {}}
    calls, evidence = [], candidate_evidence.candidate_evidence

    def counted(*args):
        calls.append((args[1], args[2]['id']))
        return evidence(*args)

    monkeypatch.setattr(candidate_evidence, 'candidate_evidence', counted)
    audience, demand = workload_normalization(context, 'duties')
    assert set(audience) == {a, b, c} and demand == 2
    prepared = len(calls)
    trial = {**context, 'shifts': [{**row, 'participants': []} for row in source['shifts']]}
    assert workload_normalization(trial, 'duties') == (audience, demand)
    assert len(calls) == prepared, 'Assignment-only trials reuse the complete fixed audience and demand'
    filtered = {**trial, 'shifts': trial['shifts'][:1]}
    assert filtered['focus_ids'] == source['focus_ids']
    assert workload_normalization(filtered, 'duties') == (audience, 1)
    assert workload_normalization(trial, 'duties') == (audience, demand)
    narrowed = {**trial, 'focus_ids': [source['shifts'][0]['id']]}
    assert workload_normalization(narrowed, 'duties')[1] == 1
    assert workload_normalization(narrowed, 'reserved_hours')[1] == 4 * 3_600_000_000
    changed = {**trial, 'people': {pid: {**person, 'active': pid != c} for pid, person in source['people'].items()},
               '_workload_normalization_cache': {}}
    assert set(workload_normalization(changed, 'duties')[0]) == {a, b}
    ineligible = {**trial, 'eligibilities': [row for row in source['eligibilities'] if row['person_id'] != c],
                  '_workload_normalization_cache': {}}
    assert set(workload_normalization(ineligible, 'duties')[0]) == {a, b}
    leave = {**narrowed, 'availability': [{'id': 'dated-leave', 'person_id': a,
        'start': source['shifts'][0]['start'], 'end': source['shifts'][0]['end'], 'kind': 'recorded_leave'}],
        '_workload_normalization_cache': {}}
    assert set(workload_normalization(leave, 'duties')[0]) == {b, c}
    retimed = {**leave, 'shifts': [{**source['shifts'][0], 'start': '2026-10-06T08:00:00+03:00',
                                  'end': '2026-10-06T12:00:00+03:00'}], '_workload_normalization_cache': {}}
    assert set(workload_normalization(retimed, 'duties')[0]) == {a, b, c}
    for view in (trial, filtered, narrowed, changed, ineligible, leave, retimed):
        uncached = {key: value for key, value in view.items() if key != '_workload_normalization_cache'}
        assert workload_normalization(view, 'duties') == workload_normalization(uncached, 'duties')
    assert source == original


def test_workload_trial_cache_ignores_manual_people_on_fixed_usual_duties(h):
    a, b, _ = h.seed['people']
    workspace = h.shift(h.workspace(), people=[b])['workspace']
    source = h.snapshot(workspace)
    source['shifts'][0]['series_snapshot'] = {'definition': {
        'usual_assignees_fixed': True, 'default_person_ids': [a]}}
    context = {**source, '_workload_normalization_cache': {}}
    assert workload_normalization(context, 'duties')[0] == (a,)
    trial = {**context, 'shifts': [{**source['shifts'][0], 'participants': []}]}
    assert workload_normalization(trial, 'duties')[0] == (a,)
    uncached = {key: value for key, value in trial.items() if key != '_workload_normalization_cache'}
    assert workload_normalization(trial, 'duties') == workload_normalization(uncached, 'duties')


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
@pytest.mark.parametrize('cached', [False, True])
def test_removing_a_manual_nonusual_assignee_preserves_workload_audience_and_compiled_presence(measure, cached):
    snapshot = data([shift('s' + str(n), 4 + n, ['a']) for n in range(4)])
    snapshot['people'] = {pid: snapshot['people'][pid] for pid in 'ab'}
    for session in snapshot['shifts']:
        session['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': ['a']}}
    first = snapshot['shifts'][0]
    first['participants'].append({**first['participants'][0], 'person_id': 'b'})
    if cached:
        snapshot['_workload_normalization_cache'] = {}
    expected = (('a',), 4 if measure == 'duties' else 32 * 3_600_000_000)
    assert workload_normalization(snapshot, measure) == expected
    removed = {**snapshot, 'shifts': [{**session, 'participants': [p for p in session['participants']
                                    if p['person_id'] != 'b']} for session in snapshot['shifts']]}
    assert workload_normalization(removed, measure) == expected
    uncached = {key: value for key, value in removed.items() if key != '_workload_normalization_cache'}
    assert workload_normalization(uncached, measure) == expected
    options = {'balance_measure': measure}
    before = schedule_score(snapshot, {'coverage': [], 'findings': []}, options)
    after = schedule_score(removed, {'coverage': [], 'findings': []}, options)
    assert before['components']['fairness']['value'] is after['components']['fairness']['value'] is None
    participants = {(session['id'], p['person_id']): p for session in snapshot['shifts'] for p in session['participants']}
    choices = {key: int(key[1] == 'a') for key in participants}
    model = cp_model.CpModel()
    loss, present = workload_component(model, snapshot, choices, participants, measure)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
    assert solver.value(present) == 0 and solver.value(loss) == 0


@pytest.mark.parametrize('usual_excluded', [None, 'inactive', 'membership'])
def test_fixed_usual_workload_audience_preserves_dated_qualified_trainees(usual_excluded):
    snapshot = data([shift('first', 4, ['a', 'c']), shift('second', 5, ['a'])])
    snapshot['categories']['trainee'] = {}
    for session in snapshot['shifts']:
        session['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': ['a']}}
        session['type_snapshot']['definition']['supervision'] = {
            'trainee_category_ids': ['trainee'], 'capability_id': 'supervisor'}
    for row in snapshot['memberships']:
        if row['person_id'] == 'b':
            row.update(category_id='trainee', starts_on='2026-10-05')
        elif row['person_id'] == 'a' and usual_excluded == 'membership':
            row['ends_on'] = '2026-10-04'
    if usual_excluded == 'inactive':
        snapshot['people']['a']['active'] = False
    audience = ('a', 'b') if usual_excluded is None else ('b',)
    assert workload_normalization(snapshot, 'duties') == (audience, 2)
    empty = {**snapshot, 'shifts': [{**session, 'participants': []} for session in snapshot['shifts']]}
    assert workload_normalization(empty, 'duties') == (audience, 2)


@pytest.mark.parametrize('missing', ['requirements', 'active'])
def test_fixed_usual_legacy_workload_keeps_the_documented_missing_fact_fallback(missing):
    snapshot = data([shift('first', 4, ['a']), shift('second', 5, ['b'])])
    for session in snapshot['shifts']:
        session['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': ['a']}}
        if missing == 'requirements':
            session['type_snapshot']['definition'].pop('requirements')
    if missing == 'active':
        snapshot['people']['b'].pop('active')
    assert workload_normalization(snapshot, 'duties') == ((), None)
    before = schedule_score(snapshot, {'coverage': [], 'findings': []})
    snapshot['shifts'][1]['participants'] = []
    after = schedule_score(snapshot, {'coverage': [], 'findings': []})
    assert before['components']['fairness']['value'] is not None
    assert after['components']['fairness']['value'] is None


@pytest.mark.parametrize('outsider', ['inactive', 'inactive_nonmember', 'nonmember', 'other_department',
                                     'expired', 'inactive_rank', 'unassigned_member'])
def test_rule_opportunities_ignore_unrelated_people_but_keep_active_unassigned_members(outsider):
    from packages.domain.conditions import active_rules
    from packages.domain.score import rule_copy_key, settings_of
    from packages.domain.score_normalization import rule_opportunities
    from tests.test_schedule_score import policy, warning

    rule = policy('advisory')
    snapshot = data([shift('s', 4, ['a'])], [rule])
    snapshot['shifts'][0]['service_ids'] = ['clinic']  # Rule subjects need membership, not duty-clinic qualification.
    snapshot['people'] = {'a': snapshot['people']['a']}
    snapshot['memberships'] = [row for row in snapshot['memberships'] if row['person_id'] == 'a']
    validation = {'coverage': [], 'findings': [warning(rule['id'], 'a')]}
    options = {'scoring': {'weights': {'coverage': 0, 'rules': 1, 'fairness': 0, 'preferences': 0, 'stability': 0}}}
    assert schedule_score(snapshot, validation, options)['score'] == 0
    snapshot['people']['b'] = {'display_name': 'B', 'active': outsider not in {'inactive', 'inactive_nonmember'}}
    if outsider not in {'nonmember', 'inactive_nonmember'}:
        membership = {'person_id': 'b', 'department_id': 'd', 'category_id': 'rank', 'starts_on': '2020-01-01'}
        if outsider == 'other_department':
            membership['department_id'] = 'another department'
        elif outsider == 'expired':
            membership['ends_on'] = '2026-10-04'
        elif outsider == 'inactive_rank':
            membership['category_id'] = 'retired'
            snapshot['categories']['retired'] = {'active': False}
        snapshot['memberships'].append(membership)
    snapshot['policies'] = active_rules(snapshot['policies'], context=snapshot)
    subjects = {person for _, person in rule_opportunities(snapshot)}
    assert subjects == ({'a', 'b'} if outsider == 'unassigned_member' else {'a'})
    expected = .5 if outsider == 'unassigned_member' else 0
    scored = schedule_score(snapshot, validation, options)
    assert scored['components']['rules']['value'] == expected and scored['score'] == 100 * expected
    model = cp_model.CpModel()
    loss, present = rule_component(model, snapshot, {}, {}, {(rule_copy_key(rule), 'a'): [1]}, [],
                                   settings_of(None)['strengths'])
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
    assert solver.value(present) == 1 and 1 - solver.value(loss) / SCORE_SCALE == expected


def test_rule_opportunities_require_membership_and_rule_scope_on_the_same_dated_interval():
    from packages.domain.conditions import active_rules
    from packages.domain.score import rule_copy_key
    from packages.domain.score_normalization import rule_opportunities
    from tests.test_schedule_score import policy

    session = shift('overnight', 4, [])
    session.update(start='2026-10-04T23:00:00+00:00', end='2026-10-05T01:00:00+00:00')
    sunday, monday = policy('sunday'), policy('monday')
    sunday['definition']['scope'] = {'weekdays': [6]}
    monday['definition']['scope'] = {'weekdays': [0]}
    snapshot = data([session], [sunday, monday])
    snapshot['people'] = {pid: snapshot['people'][pid] for pid in 'ab'}
    snapshot['memberships'] = [{'person_id': 'a', 'department_id': 'd', 'category_id': 'rank', 'starts_on': '2026-10-05'},
                               {'person_id': 'b', 'department_id': 'd', 'category_id': 'rank',
                                'starts_on': '2020-01-01', 'ends_on': '2026-10-05'}]
    snapshot['policies'] = active_rules(snapshot['policies'], context=snapshot)
    expected = {(rule_copy_key(sunday), 'b'), (rule_copy_key(monday), 'a')}
    assert set(rule_opportunities(snapshot)) == expected
    session['participants'] = [{'person_id': 'a', 'start': '2026-10-05T00:00:00+00:00', 'end': session['end']}]
    assert set(rule_opportunities(snapshot)) == expected


def test_rule_opportunities_skip_history_before_expanding_subjects(monkeypatch):
    from packages.domain import score_normalization as normalization
    from packages.domain import conditions
    from tests.test_schedule_score import policy
    session = shift('overnight', 4, [])
    session.update(start='2026-10-04T23:00:00+00:00', end='2026-10-05T01:00:00+00:00')
    active = policy('partial')
    active['starts_on'] = '2026-10-05'
    snapshot = data([session], [active])
    snapshot['policies'] = [active]
    expected = normalization.rule_opportunities(snapshot)
    assert expected
    old = [{**policy(str(i)), 'status': 'retired' if i % 2 else 'active',
            'ends_on': '2025-01-01'} for i in range(1000)]
    future = {**policy('future'), 'starts_on': '2026-10-06'}
    snapshot['policies'].extend([*old, future])
    original, visited = conditions.counting_views, []
    def bounded(data, policies):
        visited.extend(p['id'] for p in policies)
        return original(data, policies)
    monkeypatch.setattr(conditions, 'counting_views', bounded)
    assert normalization.rule_opportunities(snapshot) == expected
    assert visited == ['partial']
    assert len(snapshot['policies']) == 1002, 'Keep source policies available for referenced selectors'

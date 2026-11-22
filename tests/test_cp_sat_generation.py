import copy
from fractions import Fraction
import pytest
from ortools.sat.python import cp_model

from packages.domain.cp_sat import canonical_order, generate, objective_bound
from packages.domain.cp_sat_score import ScoreModel, ScorePrecisionError, fractional_scale
from packages.domain.score import schedule_score
from packages.domain.solver_control import SolverControl
from packages.domain.validation import evaluate
from packages.contracts.schemas import MAX_WISH_POINTS


@pytest.mark.parametrize('case', ['coefficient', 'aggregate', 'duplicate', 'domain', 'compound'])
def test_score_model_rejects_unsafe_expression_bounds(case):
    model = cp_model.CpModel()
    x, y = model.new_bool_var('x'), model.new_bool_var('y')
    half = cp_model.INT_MAX // 4 + 1
    expression = {
        'coefficient': 2 * half * x,
        'aggregate': half * x + half * y,
        'duplicate': half * x + half * x,
        'domain': (cp_model.INT_MAX // 20 + 1) * model.new_int_var(0, 10, 'count'),
        'compound': half * x + half * y + 1,
    }[case]
    with pytest.raises(ScorePrecisionError, match='integer precision'):
        ScoreModel(model).var(expression)
    assert not model.has_objective()


def test_score_model_checks_native_output_domains_and_simplifies_fixed_values():
    model = cp_model.CpModel()
    q = ScoreModel(model)
    large = model.new_int_var(0, 2**62, 'large_count')
    with pytest.raises(ScorePrecisionError, match='integer precision'):
        q.maximum([0, large], 2**62)
    fixed = model.new_int_var(0, 0, 'fixed')
    half = cp_model.INT_MAX // 4 + 1
    assert q.var(3 * half * fixed) == 0
    assert q.bounds(half * (1 - model.new_bool_var('offset'))) == (0, half)
    assert q.var(2 * half) == 2 * half  # Python constants require no native intermediate.


def test_fractional_scale_preserves_safe_weights_signs_offsets_and_small_positive_terms():
    model = cp_model.CpModel()
    x, y = model.new_bool_var('x'), model.new_bool_var('y')
    count = model.new_int_var(-2, 4, 'count')
    a, b = fractional_scale([7, 1])
    model.minimize(a * (3 * x + 2 * (1 - x)) + b * (5 * count + y + 2))
    assert dict(zip(model.proto.objective.vars, model.proto.objective.coeffs)) == {x.index: 7, count.index: 5, y.index: 1}
    assert model.proto.objective.offset == 16
    assert not model.validate()
    model.minimize(ScoreModel(model).var((cp_model.INT_MAX // 2) * x))
    assert not model.validate()


def test_fractional_scale_preserves_max_budget_daily_costs_with_precise_preferences():
    model = cp_model.CpModel()
    small, large, allocation = [model.new_bool_var(name) for name in ('small', 'large', 'allocation')]
    daily = 1000 * (1 / MAX_WISH_POINTS) * 1.001 / 14
    factors = fractional_scale([daily, 100_000 * daily, Fraction(1, 100_000_000)])
    assert factors is not None
    model.minimize(sum(factor * variable for factor, variable in zip(factors, (small, large, allocation))))
    assert not model.validate()
    coefficients = dict(zip(model.proto.objective.vars, model.proto.objective.coeffs))
    assert coefficients[small.index] > 0
    assert coefficients[large.index] / coefficients[small.index] == pytest.approx(100_000, rel=0.00002)
    assert coefficients[allocation.index] > 0
    assert fractional_scale([1e-16]) is None


@pytest.mark.parametrize('allocation_scale', [1, 100_000_000, 1_000_000_000])
def test_repeating_wish_costs_fit_thousands_of_breaches_and_preserve_max_budget_ratios(allocation_scale):
    model = cp_model.CpModel()
    small, large, repeating, allocation = [model.new_bool_var(name) for name in ('small', 'large', 'repeating', 'allocation')]
    breaches = [model.new_bool_var('breach') for _ in range(4500)]
    daily = 3000 / MAX_WISH_POINTS
    parts = [(daily, [small]), (100_000 * daily, [large]), (3000 * 0.3 / 7, [repeating]),
             (30_000, breaches), (Fraction(1, allocation_scale), [allocation])]
    factors = fractional_scale([factor for factor, _ in parts])
    assert factors is not None
    terms = [factor * sum(costs) for factor, (_, costs) in zip(factors, parts)]
    model.minimize(sum(terms))
    assert not model.validate()
    coefficients = dict(zip(model.proto.objective.vars, model.proto.objective.coeffs))
    scale = coefficients[allocation.index] * allocation_scale
    for raw, variable in ((daily, small), (100_000 * daily, large), (3000 * 0.3 / 7, repeating)):
        assert coefficients[variable.index] / scale == pytest.approx(raw, rel=0.0001)
    assert coefficients[large.index] > coefficients[small.index] > 0
    # Guard the complete scaled tier, including repeated literals, before native multiplication.
    repeats = cp_model.INT_MAX // (2 * 30_000 * scale * len(breaches)) + 1
    with pytest.raises(ScorePrecisionError, match='integer precision'):
        ScoreModel(model).var(sum(terms) + factors[-2] * sum(breaches * repeats))


def test_department_month_with_repeating_wishes_and_thousands_of_shape_breaches(h, monkeypatch):
    from datetime import timedelta
    from tests.scheduler_departments import START, department

    data, _ = department(h, 7, people=150, days=28, rules='none', leave_rate=0, clinics=9)
    policy = h.policy({'kind': 'run_length', 'minimum': 2, 'maximum': 5}, severity='advisory')
    policy['definition']['strength'] = 'critical'
    data['policies'].append(policy)
    data['wishes'] = [{'id': f'w{i}', 'person_id': pid, 'first_day': START.isoformat(),
                       'last_day': (START + timedelta(days=6)).isoformat(), 'kind': 'prefer_off',
                       'assignment_type_id': None, 'points': 3 * (1 + i % 2), 'weight': 0.3 * (1 + i % 2)}
                      for i, pid in enumerate(data['people'])]
    assert len(data['people']) == 150 and len(data['shifts']) == 532

    from packages.domain import cp_sat_score_preferences
    compile_patterns = cp_sat_score_preferences.add_patterns
    breaches = {}

    def counted_patterns(model, data, by_person, strengths, *, check=None, shape_breaches):
        result = compile_patterns(model, data, by_person, strengths, check=check, shape_breaches=shape_breaches)
        breaches.update(shape_breaches)
        return result

    monkeypatch.setattr(cp_sat_score_preferences, 'add_patterns', counted_patterns)

    def check_objectives(model, tiers):
        assert sum(len(flags) for flags in breaches.values()) > 4000
        assert len({pid for _, pid in breaches}) >= 149  # The one external rotation has no available duties.
        assert [name for name, _, _ in tiers] == ['coverage', 'desired_coverage', 'weighted']
        for _, terms, _ in tiers:
            assert terms
            model.minimize(sum(terms))
            assert not model.validate()
            assert all(abs(coefficient) <= cp_model.INT_MAX // 2 for coefficient in model.proto.objective.coeffs)

    assert generate(data, 60, deterministic=True, _on_objectives=check_objectives)['status'] == 'model_ready'


@pytest.mark.parametrize('complement', [False, True])
def test_large_integer_weighted_values_remain_exact_after_coverage(h, monkeypatch, complement):
    from packages.domain import cp_sat_score
    cost = 2**53 + 1
    typ = h.configure('assignment_types', {'name': 'Equal candidate clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']
    data = h.snapshot(h.shift(h.workspace(), typ=typ['id'], people=[])['workspace'])

    def large_weighted(model, components, weights, tie_multiplier):
        flag = model.new_bool_var('large_weighted_cost')
        model.add(flag == int(not complement))
        loss = model.new_int_var(0, cost, 'large_weighted_loss')
        model.add(loss == cost * (1 - flag if complement else flag))
        return [loss], cost

    monkeypatch.setattr(cp_sat_score, 'weighted_component', large_weighted)
    result = generate(data, 20, deterministic=True)
    assert result['status'] == 'candidate_feasible'
    assert result['optimality_proven']
    assert len(result['additions']) == 1
    assert [row['name'] for row in result['objectives']] == ['coverage', 'weighted']
    assert all(row['proven_optimal'] for row in result['objectives'])
    assert result['objectives'][0]['value'] == 0
    expected = result['weighted_tie_multiplier'] * cost + 1
    assert expected > 2**53 and expected % 2 == 1  # A float objective receipt would lose this last bit.
    assert result['objectives'][1]['value'] == expected
    assert objective_bound(cost, 0.1) == cost + cost // 1000


def test_score_model_uses_known_variable_bounds_and_checks_compound_terms(monkeypatch):
    model = cp_model.CpModel()
    x, y = model.new_bool_var('x'), model.new_int_var(-2, 4, 'y')
    flatten, calls = cp_model.FlatIntExpr, []

    def counted(term):
        calls.append(term)
        return flatten(term)

    monkeypatch.setattr(cp_model, 'FlatIntExpr', counted)
    q = ScoreModel(model)
    assert q.bounds(x) == (0, 1) and q.bounds(y) == (-2, 4) and q.bounds(2) == (2, 2)
    assert calls == []
    assert q.bounds(3 * x + y + 2) == (0, 9)
    assert len(calls) == 1


def test_static_configuration_failures_are_reported_before_search(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    original = h.snapshot(w)
    for code in ('TYPE_INACTIVE', 'LOCATION_CLOSED', 'RESOURCE_CAPACITY'):
        data = copy.deepcopy(original)
        shift = data['shifts'][0]
        if code == 'TYPE_INACTIVE':
            data['types'][shift['assignment_type_id']]['active'] = False
        elif code == 'LOCATION_CLOSED':
            data['closures'].append({'location_id': shift['location_id'], 'start': shift['start'], 'end': shift['end']})
        else:
            data['shifts'].extend({**copy.deepcopy(shift), 'id': f'additional-room-duty-{i}'} for i in range(2))
            data['focus_ids'] = [s['id'] for s in data['shifts']]
        stages = []
        result = generate(data, 5, progress=lambda p: stages.append(p['stage']))
        assert result['status'] == 'invalid_inputs'
        assert code in {i['code'] for i in result['issues']}
        assert code in {f['code'] for f in evaluate(data)['findings']}
        assert result['phase_timings']['search_seconds'] == 0
        assert 'finding' not in stages
        assert result['additions'] == result['removals'] == []


def test_full_period_reassignment_respects_rest_and_availability(h):
    flexible, first_only = h.seed['people'][:2]
    h.policy({'kind': 'min_rest', 'hours': 24})
    h.policy({'kind': 'restriction', 'weekdays': [0], 'start_time': '00:00', 'end_time': '24:00'},
             {'person_id': first_only})
    w = h.shift(h.workspace(), people=[flexible])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (flexible, first_only)}
    data['generation_options'] = {'mode': 'rebalance'}
    original = copy.deepcopy(data)
    result = generate(data, 5)
    assert data == original
    assert result['status'] == 'candidate_feasible', result
    assert result['validation']['valid']
    assert result['optimality_proven']
    assert len(result['removals']) == 1
    assert result['removals'][0]['person_id'] == flexible
    shifts = {s['id']: s for s in data['shifts']}
    for change in result['additions'] + result['removals']:
        shift = shifts[change['shift_id']]
        assert change['session_label'] == shift['type_snapshot']['name']
        assert change['service_names'] == [data['services'][sid]['name'] for sid in shift['service_ids']]
        assert change['location_name'] == data['locations'][shift['location_id']]['name']
    assigned = {r['participant']['start'][:10]: r['participant']['person_id'] for r in result['additions']}
    assert assigned == {'2026-10-04': first_only, '2026-10-05': flexible}
    before = canonical_order(copy.deepcopy(original))
    assert result['summary']['score_before'] == schedule_score(before, evaluate(before))
    after = copy.deepcopy(before)
    removed = {r['assignment_id'] for r in result['removals']}
    for shift in after['shifts']:
        shift['participants'] = [p for p in shift['participants'] if p['id'] not in removed]
        shift['participants'].extend(a['participant'] for a in result['additions'] if a['shift_id'] == shift['id'])
    assert result['summary']['score'] == schedule_score(after, evaluate(after))
    assert result['summary']['score']['score'] > result['summary']['score_before']['score']
    assert result['summary']['score']['components']['stability']['value'] is None
    data['shifts'][0]['locked'] = True
    assert generate(data, 5)['status'] == 'requirements_conflict'


def test_diagnostic_proposal_score_matches_full_rota_after_apply_with_kept_scope(h):
    typ = h.configure('assignment_types', {'name': 'Four required places', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 4, 'maximum': 4}]}})['record']['id']
    workspace = h.shift(h.workspace(), people=[h.seed['people'][0]])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', typ=typ, people=[])['workspace']
    before = canonical_order(h.snapshot(workspace))
    selected = next(s for s in before['shifts'] if s['assignment_type_id'] == typ)
    kept = next(s for s in before['shifts'] if s['id'] != selected['id'])
    before['generation_session_ids'] = [selected['id']]
    current_score = schedule_score(before, evaluate(before))
    original = copy.deepcopy(before)

    result = generate(before, 5, diagnostic_proposal=True)
    assert before == original
    assert result['status'] == 'candidate_with_gaps'
    assert result['additions'] and not result['removals']
    assert {a['shift_id'] for a in result['additions']} == {selected['id']}
    summary = result['summary']
    assert summary['score_before'] == current_score
    assert summary['existing_assignments'] == summary['changes_kept'] == 1

    # Applying the proposal keeps the complete rota, including sessions outside the run's selection.
    after = copy.deepcopy(before)
    for shift in after['shifts']:
        shift['participants'].extend(a['participant'] for a in result['additions'] if a['shift_id'] == shift['id'])
    assert next(s for s in after['shifts'] if s['id'] == kept['id'])['participants'] == kept['participants']
    applied_score = schedule_score(after, evaluate(after))
    assert summary['score_saved'] == summary['score'] == applied_score
    assert not current_score['publishable'] and not applied_score['publishable']
    assert applied_score['score'] == 59 < applied_score['uncapped_score']
    assert summary['score_delta'] == round(applied_score['uncapped_score'] - current_score['uncapped_score'], 1) > 0


def test_valid_schedule_survives_timeout_while_improving(h, monkeypatch):
    # Desired cover makes the first objective nonempty; the advisory workload cap leaves a nonzero weighted loss.
    h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'day'}, severity='advisory')
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    data['shifts'][0]['type_snapshot']['definition']['requirements'][0].update(minimum=0, target=1, maximum=1)
    original_solve = SolverControl.solve
    phases = []

    def stop_improvement(self, solver, model, tier):
        if tier is None:  # Fixed coverage choices still need their derived score values.
            return original_solve(self, solver, model, tier)
        phases.append(self.context['objective_names'][tier])
        assert model.has_objective()
        # The first search finds a schedule; improving it then runs out of time.
        return cp_model.UNKNOWN if phases[-1] == 'weighted' else original_solve(self, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', stop_improvement)
    result = generate(data, 5)
    assert phases[0] == 'coverage' and phases[-1] == 'weighted'
    # A timed-out weighted search retains the coverage incumbent and reports it as feasible, without an optimum.
    assert [t['name'] for t in result['objectives']] == ['coverage', 'desired_coverage', 'weighted']
    assert result['status'] == 'candidate_feasible'
    assert result['validation']['valid'] and not result['optimality_proven']
    assert result['weighted_status'] == 'FEASIBLE'
    assert all(row['proven_optimal'] for row in result['objectives'][:-1])
    assert not result['objectives'][-1]['proven_optimal'] and result['objectives'][-1]['value'] > 0
    assert len(result['additions']) == 1


@pytest.mark.parametrize('completion_status,expected', [
    (cp_model.UNKNOWN, 'needs_more_time'), (cp_model.INFEASIBLE, 'translation_error')])
def test_uncompleted_score_is_never_retained_as_a_candidate(h, monkeypatch, completion_status, expected):
    original_solve, completions = SolverControl.solve, []

    def incomplete(self, solver, model, tier):
        if tier is None:
            completions.append(tier)
            return completion_status
        return original_solve(self, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', incomplete)
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    data['shifts'][0]['type_snapshot']['definition']['requirements'][0].update(minimum=0, target=1, maximum=1)
    data['generation_options'] = {'shortlist_width': 0}
    result = generate(data, 5)
    assert completions and result['status'] == expected
    assert result['weighted_status'] == 'UNKNOWN'
    assert not result['additions'] and not result['removals']


def test_solver_respects_configurable_rest_across_editing_scope(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'min_rest', 'hours': 12})
    w = h.shift(h.workspace(), start='2026-10-04T16:00:00+03:00',
                end='2026-10-05T00:00:00+03:00', people=[person])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {person: data['people'][person]}
    data['generation_session_ids'] = [data['shifts'][1]['id']]
    assert generate(data, 5)['status'] == 'requirements_conflict'
    next(p for p in data['policies'] if p['kind'] == 'min_rest')['definition']['rule']['hours'] = 8
    assert generate(data, 5)['status'] == 'candidate_feasible'


def test_rest_and_coverage_conflict_has_a_source_linked_sufficient_set(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'min_rest', 'hours': 12})
    w = h.shift(h.workspace(), start='2026-10-04T16:00:00+03:00',
                end='2026-10-05T00:00:00+03:00', people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {person: data['people'][person]}
    result = generate(data, 5)
    assert result['status'] == 'requirements_conflict'
    assert result['weighted_status'] == 'INFEASIBLE'
    proof, = result['diagnostics']
    rest = next(p for p in data['policies'] if p['kind'] == 'min_rest')
    assert proof['proof_kind'] == 'sufficient_set_with_background'
    assert proof['proven'] and not proof['minimal'] and not proof['exhaustive']
    assert {r['rule_id'] for r in proof['requirements'] if r['kind'] == 'policy'} == {rest['id']}
    assert {r['shift_id'] for r in proof['requirements'] if r['kind'] == 'duty'} == set(data['focus_ids'])
    assert proof['input_fingerprint'] == data['fingerprint']
    assert result['phase_timings']['diagnostic_seconds'] >= 0
    # Changing the configured rest requirement really makes this known case feasible.
    rest['definition']['rule']['hours'] = 8
    assert generate(data, 5)['status'] == 'candidate_feasible'


def test_unsupported_rule_is_rejected_before_search(h):
    h.policy({'kind': 'allocation_preference', 'priority': 1}, severity='advisory')
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    next(p for p in data['policies'] if p['kind'] == 'allocation_preference')['definition']['rule']['kind'] = 'future_rule_not_translated'
    result = generate(data, 5)
    assert result['status'] == 'unsupported_rules'
    assert result['issues'][0]['field'] == 'definition.rule.kind'
    assert result['additions'] == result['removals'] == []


@pytest.mark.parametrize('setting', [
    {'share_adjustment': {'mode': 'percent', 'value': 1e-20}},
    {'share_adjustment': {'mode': 'amount', 'value': 1e-20}},
    {'share_adjustment': {'mode': 'amount', 'value': -1e-20}},
    {'tolerance_percent': 1e-20},
    {'tolerance_units': 1e-20},
], ids=['percent', 'amount', 'negative-amount', 'tolerance-percent', 'tolerance-units'])
def test_contract_valid_small_fair_share_dials_return_structured_precision_issue(h, setting):
    from packages.contracts.schemas import PolicyDefinition
    from packages.domain.solver_rules import compatibility_issues
    from .test_balance_between_people import catalogue

    workspace = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(workspace)
    goal = catalogue(people=h.seed['people'][:2])
    goal['department_id'] = h.seed['department_id']
    goal['definition']['rule']['lines'][0]['settings'] = {'balance_goal': setting}
    goal['definition'] = PolicyDefinition.model_validate(goal['definition']).model_dump(mode='json')
    data['policies'].append(goal)
    assert compatibility_issues(data) == []
    original = copy.deepcopy(data)
    result = generate(data, 5)
    assert result['status'] == 'invalid_inputs', result
    assert [issue['code'] for issue in result['issues']] == ['SCORE_PRECISION_UNSUPPORTED']
    assert result['additions'] == result['removals'] == []
    assert not result['optimality_proven'] and result['weighted_status'] == 'UNKNOWN'
    assert data == original


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
@pytest.mark.parametrize('field', ['tolerance_percent', 'tolerance_units'])
def test_small_workload_tolerances_return_structured_precision_issue(h, measure, field):
    from packages.contracts.schemas import PolicyDefinition
    from packages.domain.solver_rules import compatibility_issues

    workspace = h.shift(h.workspace(), people=[])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(workspace)
    people = set(h.seed['people'][:2])
    data['people'] = {pid: person for pid, person in data['people'].items() if pid in people}
    data['memberships'] = [row for row in data['memberships'] if row['person_id'] in people]
    goal = next(p for p in data['policies'] if p['definition']['rule'].get('id') == 'fair_share')
    line = next(line for line in goal['definition']['rule']['lines']
                if line.get('settings', {}).get('balance_goal', {}).get('aim') == 'workload_spread')
    line['settings']['balance_goal'][field] = 1e-20
    goal['definition'] = PolicyDefinition.model_validate(goal['definition']).model_dump(mode='json')
    data['generation_options'] = {'balance_measure': measure}
    assert compatibility_issues(data) == []
    original = copy.deepcopy(data)
    result = generate(data, 5)
    assert result['status'] == 'invalid_inputs', result
    assert [issue['code'] for issue in result['issues']] == ['SCORE_PRECISION_UNSUPPORTED']
    assert result['additions'] == result['removals'] == []
    assert not result['optimality_proven'] and result['weighted_status'] == 'UNKNOWN'
    assert data == original


@pytest.mark.parametrize('setting', ['fairness_worst_off_emphasis', 'desired_cover_factor'])
def test_small_global_scoring_fractions_return_structured_precision_issue(h, setting):
    from packages.contracts.schemas import GenerationOptions, TypeDefinition

    workspace = h.workspace()
    for day in (4, 5, 6, 7):
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(workspace)
    for shift in data['shifts']:
        shift['type_snapshot']['definition'] = TypeDefinition.model_validate({
            **shift['type_snapshot']['definition'],
            'requirements': [{'minimum': 1, 'target': 2, 'maximum': 2}]}).model_dump(mode='json')
    data['generation_options'] = GenerationOptions.model_validate({
        'scoring': {setting: 1e-20}}).model_dump(mode='json')
    original = copy.deepcopy(data)
    result = generate(data, 5)
    assert result['status'] == 'invalid_inputs', result
    assert [issue['code'] for issue in result['issues']] == ['SCORE_PRECISION_UNSUPPORTED']
    assert result['additions'] == result['removals'] == []
    assert not result['optimality_proven'] and result['weighted_status'] == 'UNKNOWN'
    assert data == original


def test_named_supervision_respects_scoped_supervisor_limits(h):
    trainee, supervisor = h.seed['people'][:2]
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Supervised FM clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}],
            'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
            'require_pairing': True}}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (trainee, supervisor)}
    result = generate(data, 5)
    assert result['status'] == 'candidate_feasible', result
    participant = next(r['participant'] for r in result['additions'] if r['participant']['person_id'] == trainee)
    assert participant['supervisor_person_id'] == supervisor
    assert result['validation']['valid']
    on_leave = copy.deepcopy(data)
    shift = on_leave['shifts'][0]
    on_leave['availability'].append({'id': 'supervisor-leave', 'person_id': supervisor,
        'kind': 'recorded_leave', 'start': shift['start'], 'end': shift['end']})
    assert generate(on_leave, 5)['status'] == 'requirements_conflict'
    h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'trainee_category_ids': [], 'maximum': 0},
             {'person_id': supervisor})
    preview = h.command('coordinator', 'workspace.baseline.refresh.preview', {}, w['id'], w['revision'])
    refreshed = h.command('coordinator', 'workspace.baseline.refresh.apply', {
        'expected_baseline_hash': preview['baseline']['snapshot_hash'],
        'decisions': {source: 'use_department' for source in preview['conflicts']},
        'reason': 'Refresh the draft baseline after the reviewed supervision limit change',
    }, w['id'], w['revision'])
    assert refreshed['applied'] is True
    limited = h.snapshot(refreshed['workspace'])
    limited['people'] = data['people']
    assert generate(limited, 5)['status'] == 'requirements_conflict'


def test_fixed_clinician_with_verified_grant_can_be_named_supervisor(h):
    trainee, clinician = h.seed['people'][:2]
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Fixed supervised FM clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}],
            'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
                'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
                'require_pairing': True}}})['record']
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    workspace = h.workspace()
    result = h.shift(workspace, typ=typ['id'], people=[{'person_id': clinician,
        'start': start, 'end': end, 'participation': 'staff', 'fixed': True}])
    data = h.snapshot(result['workspace'])
    data['people'] = {pid: data['people'][pid] for pid in (trainee, clinician)}
    planned = generate(data, 5)
    assert planned['status'] == 'candidate_feasible', planned
    assert next(a['participant'] for a in planned['additions'] if a['participant']['person_id'] == trainee)['supervisor_person_id'] == clinician
    assert planned['validation']['valid']
    without_grant = copy.deepcopy(data)
    without_grant['grants'] = [g for g in data['grants'] if g['person_id'] != clinician]
    assert generate(without_grant, 5)['status'] == 'requirements_conflict'


@pytest.mark.parametrize('gap', [False, True])
def test_overnight_supervision_respects_consecutive_grants(h, gap):
    trainee, supervisor = h.seed['people'][:2]
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'UCC overnight supervision', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}],
            'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
            'require_pairing': True}}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=[], start='2026-10-04T20:00:00+03:00', end='2026-10-05T04:00:00+03:00')['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (trainee, supervisor)}
    grant = next(g for g in data['grants'] if g['person_id'] == supervisor and g['capability_id'] == h.seed['supervisor_capability'])
    data['grants'].remove(grant)
    data['grants'].extend([{**grant, 'id': 'before-midnight', 'ends_on': '2026-10-05'},
                          {**grant, 'id': 'after-midnight', 'starts_on': '2026-10-06' if gap else '2026-10-05'}])
    witness = copy.deepcopy(data)
    shift = witness['shifts'][0]
    shift['participants'] = [{'id': pid, 'person_id': pid, 'shift_id': shift['id'], 'start': shift['start'],
        'end': shift['end'], 'participation': role, 'overtime': False, 'revision': 1,
        'supervisor_person_id': supervisor if pid == trainee else None}
        for pid, role in ((trainee, 'trainee'), (supervisor, 'supervisor'))]
    assert evaluate(witness)['valid'] is not gap
    result = generate(data, 5)
    assert result['status'] == ('requirements_conflict' if gap else 'candidate_feasible')


@pytest.mark.parametrize('named', [False, True])
@pytest.mark.parametrize('unrelated_rule', [False, True, 'preference', 'advisory_quota', 'mandatory_quota', 'limit'])
def test_split_overnight_authority_does_not_invent_full_duty_pairing(h, named, unrelated_rule):
    import uuid
    trainee, supervisor = h.seed['people'][:2]
    h.command('coordinator', 'membership.create', {'person_id': trainee, 'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'UCC overnight handover', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 3, 'maximum': 3}], 'supervision': {
            'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
            'trainees_per_supervisor': 1, 'require_pairing': named}}})['record']
    if unrelated_rule == 'preference':
        h.policy({'kind': 'allocation_preference', 'priority': 50, 'supervisor_ids': [supervisor]},
                 {'person_id': trainee}, severity='advisory')
    elif unrelated_rule in ('advisory_quota', 'mandatory_quota'):
        h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week', 'group_by': 'supervisor'},
                 {'person_id': trainee}, severity='advisory' if unrelated_rule == 'advisory_quota' else 'mandatory')
    draft = h.workspace()
    w = h.shift(draft, typ=typ['id'], people=[], start='2026-10-04T20:00:00+03:00', end='2026-10-05T04:00:00+03:00')['workspace']
    data = h.snapshot(w)
    if unrelated_rule == 'limit':
        h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'trainee_category_ids': [], 'maximum': 1},
                 {'person_id': supervisor})
        data = h.snapshot(w)
    elif unrelated_rule:
        h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'trainee_category_ids': [], 'maximum': 0},
                 {'weekdays': [2]})  # Wednesday rule cannot constrain Sunday/Monday UCC.
        data = h.snapshot(w)
    covering = str(uuid.uuid4())
    data['people'] = {pid: data['people'][pid] for pid in (trainee, supervisor)}
    data['people'][covering] = {**data['people'][supervisor], 'id': covering}
    for key in ('memberships', 'eligibilities'):
        data[key].extend({**row, 'id': str(uuid.uuid4()), 'person_id': covering} for row in list(data[key]) if row['person_id'] == supervisor)
    grant = next(g for g in data['grants'] if g['person_id'] == supervisor and g['capability_id'] == h.seed['supervisor_capability'])
    data['grants'].remove(grant)
    data['grants'].extend([{**grant, 'id': 'before-midnight', 'ends_on': '2026-10-05'},
                          {**grant, 'id': 'after-midnight', 'person_id': covering, 'starts_on': '2026-10-05'}])
    witness = copy.deepcopy(data)
    shift = witness['shifts'][0]
    shift['participants'] = [{'id': pid, 'person_id': pid, 'shift_id': shift['id'], 'start': shift['start'], 'end': shift['end'],
        'participation': 'trainee' if pid == trainee else 'supervisor', 'overtime': False, 'revision': 1,
        'supervisor_person_id': supervisor if named and pid == trainee else None} for pid in (trainee, supervisor, covering)]
    required = named or unrelated_rule in ('mandatory_quota', 'limit')
    assert evaluate(witness)['valid'] is not required
    assert generate(data, 5)['status'] == ('requirements_conflict' if required else 'candidate_feasible')


def test_scoped_cover_and_overflow_use_distinct_configured_people(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'minimum_cover', 'minimum': 1}, {'person_id': person})
    h.policy({'kind': 'maximum_participants', 'maximum': 0, 'overflow_maximum': 1,
              'overflow_category_ids': [h.seed['category']]})
    typ = h.configure('assignment_types', {'name': 'FM configured cover', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=[])['workspace']
    result = generate(h.snapshot(w), 5)
    assert result['status'] == 'candidate_feasible', result
    assert [a['participant']['person_id'] for a in result['additions']] == [person]
    assert any(f['code'] == 'CAPACITY_OVERFLOW' for f in result['validation']['findings'])


def test_distinct_capabilities_cannot_be_filled_by_one_person(h):
    typ = h.configure('assignment_types', {'name': 'Distinct clinical authority', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'distinct_capability_seats': True,
            'capabilities': [{'capability_id': h.seed['capability'], 'minimum': 1},
                             {'capability_id': h.seed['supervisor_capability'], 'minimum': 1}]}]}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=[])['workspace']
    data = h.snapshot(w)
    first = h.seed['people'][0]
    data['people'] = {first: data['people'][first]}
    assert generate(data, 5)['status'] == 'requirements_conflict'
    assert generate(h.snapshot(w), 5)['status'] == 'candidate_feasible'


def test_soft_rest_never_weakens_a_mandatory_rest_rule(h):
    person = h.seed['people'][0]
    h.stored_advisory_safety({'kind': 'min_rest', 'hours': 24})
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {person: data['people'][person]}
    # Safety kinds are always mandatory, including a stored advisory minimum rest.
    assert generate(data, 5)['status'] == 'requirements_conflict'
    next(p for p in data['policies'] if p['kind'] == 'min_rest')['definition']['severity'] = 'mandatory'
    assert generate(data, 5)['status'] == 'requirements_conflict'


@pytest.mark.parametrize('window', ['day', 'week', 'rolling'])
@pytest.mark.parametrize('kind', ['max_hours', 'max_duties'])
def test_workload_limits_cover_both_fm_clinics_and_configured_windows(h, window, kind):
    rule = {'kind': kind, 'window': window, **({'hours': 4} if kind == 'max_hours' else {'maximum': 1})}
    if window == 'rolling':
        rule['window_days'] = 1
    h.policy(rule)
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    person = h.seed['people'][0]
    data['people'] = {person: data['people'][person]}
    assert generate(data, 5)['status'] == 'requirements_conflict'
    next(p for p in data['policies'] if p['kind'] == kind)['definition']['rule'].update({'hours': 8} if kind == 'max_hours' else {'maximum': 2})
    result = generate(data, 5)
    assert result['status'] == 'candidate_feasible', result
    assert len(result['additions']) == 2
    assert result['validation']['valid']


@pytest.mark.parametrize('metric,amount', [('duties', 1), ('session_units', 2), ('scheduled_hours', 4),
    ('reserved_hours', 4), ('workload_credit', 6), ('training_credit', 2)])
def test_quota_metrics_use_shared_assignment_attribution(h, metric, amount):
    person = h.seed['people'][0]
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': metric, 'comparison': 'minimum', 'amount': amount}, {'person_id': person})
    typ = h.configure('assignment_types', {'name': 'FM clinic credits', 'starts_on': '2020-01-01',
        'definition': {'requirements': [], 'session_units': 2, 'workload_credit_per_hour': 1.5,
                       'training_credit_per_hour': .5}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=[])['workspace']
    data = h.snapshot(w)
    result = generate(data, 5)
    assert result['status'] == 'candidate_feasible', result
    assert [r['participant']['person_id'] for r in result['additions']] == [person]
    row = result['validation']['quota_report']['rows'][0]
    assert row['actual'] == row['amount'] == amount
    data['quota_context']['complete'] = False
    invalid = generate(data, 5)
    assert invalid['status'] == 'invalid_inputs'
    assert invalid['issues'][0]['code'] == 'QUOTA_CONTEXT_MISSING'


def test_next_day_prohibition_is_separate_from_elapsed_rest(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'forbidden_transition', 'from_type_ids': [h.seed['type']], 'to_type_ids': [h.seed['type']]})
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T16:00:00+03:00', end='2026-10-05T20:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {person: data['people'][person]}
    assert generate(data, 5)['status'] == 'requirements_conflict'  # 28 hours rest still prohibits this next-day pair.
    next(p for p in data['policies'] if p['kind'] == 'forbidden_transition')['definition']['severity'] = 'advisory'
    # Safety kinds are always mandatory, including a stored advisory next-day combination.
    result = generate(data, 5)
    assert result['status'] == 'requirements_conflict'


def test_consecutive_days_include_protected_history_and_exact_exception_window(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'max_consecutive_days', 'maximum': 2}, exception=True)
    w = h.workspace()
    for day in (4, 5, 6):
        w = h.shift(w, start=f'2026-10-{day:02}T08:00:00+03:00', end=f'2026-10-{day:02}T12:00:00+03:00',
                    people=[person] if day < 6 else [])['workspace']
    data = h.snapshot(w)
    data['people'] = {person: data['people'][person]}
    data['generation_session_ids'] = [data['shifts'][-1]['id']]
    assert generate(data, 5)['status'] == 'requirements_conflict'
    policy = next(p for p in data['policies'] if p['kind'] == 'max_consecutive_days')
    data['exceptions'].append({'id': 'test-exception', 'status': 'approved', 'policy_id': policy['id'],
        'policy_revision': policy['revision'], 'person_id': person, 'starts_on': '2026-10-04', 'ends_on': '2026-10-07'})
    assert generate(data, 5)['status'] == 'candidate_feasible'
    data['exceptions'][0]['starts_on'] = '2026-10-05'
    assert generate(data, 5)['status'] == 'requirements_conflict'


def test_travel_buffer_respects_locations_and_exact_threshold(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'travel_buffer', 'from_location_id': h.seed['locations'][0],
              'to_location_id': h.seed['locations'][1], 'minutes': 61})
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00', location=1, people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {person: data['people'][person]}
    assert generate(data, 5)['status'] == 'requirements_conflict'
    next(p for p in data['policies'] if p['kind'] == 'travel_buffer')['definition']['rule']['minutes'] = 60
    assert generate(data, 5)['status'] == 'candidate_feasible'


def test_weekend_limit_counts_configured_weekends_and_overtime_condition(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'max_consecutive_weekends', 'maximum': 1, 'weekend_start': 4, 'weekend_days': 2})
    w = h.workspace(start='2026-10-01', end='2026-10-15')
    for day in (2, 9):
        w = h.shift(w, start=f'2026-10-{day:02}T08:00:00+03:00', end=f'2026-10-{day:02}T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {person: data['people'][person]}
    assert generate(data, 5)['status'] == 'requirements_conflict'
    weekend = next(p for p in data['policies'] if p['kind'] == 'max_consecutive_weekends')['definition']['rule']
    weekend['overtime_only'] = True
    assert generate(data, 5)['status'] == 'candidate_feasible'
    weekend.update(overtime_only=False, weekend_start=5)
    assert generate(data, 5)['status'] == 'candidate_feasible'


def test_overtime_is_permitted_for_whole_duties_and_never_bypasses_rest(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'max_hours', 'hours': 4, 'window': 'week', 'include_overtime': False})
    cap = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': 'duties', 'comparison': 'maximum', 'amount': 1, 'overtime': 'only'}, {'person_id': person})
    # Non-safety scheduling inputs are frozen when the draft is created; the
    # minimum-rest safety rule below is deliberately added after the draft.
    overtime = h.policy({'kind': 'allocation_overtime', 'required_policy_ids': [cap['id']]},
                        {'person_id': person}, severity='advisory')
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {person: data['people'][person]}
    without_overtime = copy.deepcopy(data)
    without_overtime['policies'] = [p for p in without_overtime['policies'] if p['id'] != overtime['id']]
    assert generate(without_overtime, 5)['status'] == 'requirements_conflict'
    data = h.snapshot(w)
    data['people'] = {person: data['people'][person]}
    result = generate(data, 5)
    assert result['status'] == 'candidate_feasible', result
    assert sum(a['participant']['overtime'] for a in result['additions']) == 1
    incomplete = copy.deepcopy(data)
    next(p for p in incomplete['policies'] if p['kind'] == 'allocation_overtime')['definition']['scope']['end_time'] = '09:00'
    assert generate(incomplete, 5)['status'] == 'requirements_conflict'
    h.policy({'kind': 'min_rest', 'hours': 24})
    restricted = h.snapshot(w)
    restricted['people'] = data['people']
    result = generate(restricted, 5)
    assert result['status'] == 'requirements_conflict', result


@pytest.mark.parametrize('relationship', ['any', 'mentor'])
def test_cp_sat_prefers_configured_supervisors_without_granting_authority(h, monkeypatch, relationship):
    from . import test_generation_department as existing
    monkeypatch.setattr(existing, 'generate', generate)
    existing.test_named_supervisor_preference_guides_pairing_without_granting_authority(h, relationship)


def test_person_preference_never_overrides_recorded_leave(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_preference', 'priority': 50}, {'person_id': person}, severity='advisory')
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    assert [r['participant']['person_id'] for r in generate(data, 5)['additions']] == [person]
    shift = data['shifts'][0]
    data['availability'].append({'id': 'test-leave', 'person_id': person, 'kind': 'recorded_leave',
                                 'start': shift['start'], 'end': shift['end']})
    result = generate(data, 5)
    assert result['status'] == 'candidate_feasible'
    assert all(r['participant']['person_id'] != person for r in result['additions'])


def test_configured_duty_balancing_distributes_work(h):
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'balance_by': 'duties', 'balance_weight': 2}, severity='advisory')
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    result = generate(h.snapshot(w), 5)
    assert result['status'] == 'candidate_feasible'
    assert len({r['participant']['person_id'] for r in result['additions']}) == 2


def test_prepared_restrictions_preserve_complete_candidate_evidence(h, monkeypatch):
    from packages.domain import cp_sat
    from packages.domain.candidate_evidence import candidate_evidence
    h.policy({'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'},
             {'person_id': h.seed['people'][0]})
    h.policy({'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'},
             {'service_id': h.seed['services'][1]})
    h.policy({'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '16:00', 'end_time': '20:00'})
    w = h.shift(h.workspace(), people=[])['workspace']
    snapshot = h.snapshot(w)
    def checked(data, pid, shift):
        actual = candidate_evidence(data, pid, shift)
        assert actual == candidate_evidence(snapshot, pid, shift)
        return actual
    monkeypatch.setattr(cp_sat, 'candidate_evidence', checked)
    assert generate(snapshot, 5)['status'] == 'candidate_feasible'


@pytest.mark.parametrize('amount', [1, 2])
@pytest.mark.parametrize('advisory', [False, True])
def test_diagnostic_proposal_keeps_mandatory_minimum_with_unrelated_staffing_gap(h, amount, advisory):
    person = h.seed['people'][0]
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'minimum', 'amount': amount}, {'person_id': person})
    h.policy({'kind': 'restriction', 'weekdays': [0]})
    if advisory:
        h.policy({'kind': 'max_duties', 'maximum': 10, 'window': 'week'}, severity='advisory')
    optional = h.configure('assignment_types', {'name': 'Optional quota duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [] if amount == 1 else [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    workspace = h.shift(h.workspace(), typ=optional, people=[])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    before = canonical_order(h.snapshot(workspace))
    before['people'] = {person: before['people'][person]}
    assert generate(before, 5)['status'] == 'requirements_conflict'

    result = generate(before, 5, diagnostic_proposal=True)
    assert result['status'] == 'candidate_with_gaps'
    assert [(a['shift_id'], a['participant']['person_id']) for a in result['additions']] == [
        (next(s['id'] for s in before['shifts'] if s['assignment_type_id'] == optional), person)]
    quota, = result['validation']['quota_report']['rows']
    assert quota['actual'] == 1
    assert quota['status'] == ('met' if amount == 1 else 'below_minimum')
    assert sum(f['code'] == 'QUOTA_BELOW_MINIMUM' for f in result['validation']['findings']) == int(amount > 1)
    after = copy.deepcopy(before)
    for shift in after['shifts']:
        shift['participants'].extend(a['participant'] for a in result['additions'] if a['shift_id'] == shift['id'])
    score = result['summary']['score_saved']
    assert score == schedule_score(after, evaluate(after))
    assert score['components']['rules']['value'] == (1 if advisory else None)
    assert result['compiled_component_values']['rules'] == (1 if advisory else None)
    assert not any(c['component'] == 'rules' for c in score['contributions'])


def test_diagnostic_quota_minimum_preserves_staffing_priority(h):
    person = h.seed['people'][0]
    optional = h.configure('assignment_types', {'name': 'Optional quota duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']['id']
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'minimum', 'amount': 1, 'assignment_type_ids': [optional]},
             {'person_id': person})
    h.policy({'kind': 'restriction', 'weekdays': [0]})
    workspace = h.shift(h.workspace(), typ=optional, people=[])['workspace']
    workspace = h.shift(workspace, location=1, people=[])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(workspace)
    data['people'] = {person: data['people'][person]}
    result = generate(data, 5, diagnostic_proposal=True)
    assert result['status'] == 'candidate_with_gaps'
    required = next(s for s in data['shifts'] if s['location_id'] == h.seed['locations'][1])
    assert [a['shift_id'] for a in result['additions']] == [required['id']]
    quota, = result['validation']['quota_report']['rows']
    assert quota['actual'] == 0 and quota['status'] == 'below_minimum'


@pytest.mark.parametrize('diagnostic', [False, True])
@pytest.mark.parametrize('safeguard', ['rest', 'maximum'])
def test_diagnostic_proposal_exposes_gaps_without_relaxing_rest_or_quota_maximum(h, diagnostic, safeguard):
    person = h.seed['people'][0]
    if safeguard == 'rest':
        h.policy({'kind': 'min_rest', 'hours': 24})
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    quotas = [('minimum', 2)] + ([('maximum', 1)] if safeguard == 'maximum' else [])
    for comparison, amount in quotas:
        h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                  'metric': 'duties', 'comparison': comparison, 'amount': amount}, {'person_id': person})
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {person: data['people'][person]}
    original = copy.deepcopy(data)
    result = generate(data, 5, diagnostic_proposal=diagnostic)
    assert data == original
    if not diagnostic:
        assert result['status'] == 'requirements_conflict'
        assert not result['additions']
        return
    assert result['status'] == 'candidate_with_gaps', result
    assert result['diagnostic_proposal'] and not result['optimality_proven']
    assert len(result['additions']) == 1
    errors = {f['code'] for f in result['validation']['findings'] if f['severity'] == 'error'}
    assert errors == {'COVERAGE_GAP', 'SCOPED_COVERAGE_GAP', 'QUOTA_BELOW_MINIMUM'}
    assert not result['validation']['valid']
    with pytest.raises(ValueError, match='cannot certify feasibility'):
        generate(data, 5, diagnostic_proposal=True, feasibility_only=True)


def test_context_check_preserves_direct_shortage_after_conflict_diagnosis(h):
    h.policy({'kind': 'restriction', 'weekdays': [6]})
    w = h.shift(h.workspace(), people=[])['workspace']
    result = generate(h.snapshot(w), 5, feasibility_only=True)
    assert result['status'] == 'requirements_conflict'
    shortage = next(d for d in result['diagnostics'] if d['code'] == 'INSUFFICIENT_WHOLE_DUTY_CANDIDATES')
    assert shortage['required'] == 1 and shortage['available_upper_bound'] == 0
    assert shortage['candidates'] and shortage['input_fingerprint']
    assert not result['diagnostics_complete']


def test_context_check_reports_bad_credit_precision_despite_another_staffing_shortage(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'session_units', 'comparison': 'minimum', 'amount': 1}, {'person_id': person})
    h.policy({'kind': 'restriction', 'weekdays': [0]})
    typ = h.configure('assignment_types', {'name': 'FM fractional clinic credit', 'starts_on': '2020-01-01',
        'definition': {'requirements': [], 'session_units': 0.0000000001}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    result = generate(data, 5, feasibility_only=True)
    assert result['status'] == 'invalid_inputs', result
    assert 'QUOTA_PRECISION_UNSUPPORTED' in {i['code'] for i in result['issues']}
    assert result['diagnostics']  # The independent shortage is still evidenced.
    assert result['additions'] == result['removals'] == []


@pytest.mark.parametrize('fixed', [False, True])
def test_extra_cover_keeps_manual_regular_placements_without_inventing_overtime_permission(h, fixed):
    from .test_overtime_extra_cover import extra_type
    duty_type = extra_type(h, 'gap')
    person = h.seed['people'][0]
    workspace = h.shift(h.workspace(), typ=duty_type['id'], people=[{
        'person_id': person, 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'overtime': False, 'fixed': fixed,
    }])['workspace']
    data = h.snapshot(workspace)
    data['people'] = {person: data['people'][person]}
    data['generation_options'] = {'mode': 'rebalance' if fixed else 'fill_gaps'}
    result = generate(data, 5)
    assert result['status'] == 'candidate_feasible', result
    assert result['additions'] == result['removals'] == []
    assert result['validation']['valid']
    assert {f['code'] for f in result['validation']['findings']} == {'EXTRA_COVER_NOT_OVERTIME'}


def test_extra_cover_rebalance_converts_regular_placement_with_overtime_permission(h):
    from .test_overtime_extra_cover import extra_type
    duty_type = extra_type(h, 'gap')
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    workspace = h.shift(h.workspace(), typ=duty_type['id'], people=[{
        'person_id': person, 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'overtime': False, 'fixed': False,
    }])['workspace']
    data = h.snapshot(workspace)
    data['people'] = {person: data['people'][person]}
    data['generation_options'] = {'mode': 'rebalance'}
    original, = data['shifts'][0]['participants']

    result = generate(data, 5)

    assert result['status'] == 'candidate_feasible', result
    assert result['optimality_proven'] and result['validation']['valid']
    removal, = result['removals']
    addition, = result['additions']
    assert removal['assignment_id'] == original['id']
    assert addition['participant']['person_id'] == person
    assert addition['participant']['overtime'] is True
    assert 'EXTRA_COVER_NOT_OVERTIME' not in {f['code'] for f in result['validation']['findings']}
    assert result['compiled_component_values']['stability'] == 0
    assert result['objective_components']['stability']['value'] == 0


def test_extra_cover_feasibility_and_unknown_search_never_claim_optimality(h, monkeypatch):
    from .test_overtime_extra_cover import extra_type
    duty_type = extra_type(h)
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    workspace = h.shift(h.workspace(), typ=duty_type['id'], people=[])['workspace']
    data = h.snapshot(workspace)
    feasible = generate(data, 5, feasibility_only=True)
    assert feasible['status'] == 'candidate_feasible' and feasible['validation']['valid']
    assert not feasible['optimality_proven']
    monkeypatch.setattr(SolverControl, 'solve', lambda *args, **kwargs: cp_model.UNKNOWN)
    unknown = generate(data, 5)
    assert unknown['status'] == 'needs_more_time'
    assert unknown['additions'] == unknown['removals'] == []
    assert not unknown.get('optimality_proven', False)

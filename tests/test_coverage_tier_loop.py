"""Regression coverage for tier continuation, budgets and stage receipts."""
import copy
from datetime import timedelta
from random import Random

import pytest
from ortools.sat.python import cp_model
from pydantic import ValidationError

from packages.contracts.schemas import GenerationOptions
from packages.domain import cp_sat
from packages.domain.cp_sat import generate
from packages.domain.fill_priority import FillPriority, allocation_tier
from packages.domain.generation import generate as greedy_generate
from packages.domain.solver_control import SolverControl
from packages.domain.time import aware
from packages.domain.validation import evaluate, finding_observations
from tests.scheduler_departments import apply, department


def tier_loop_data(h):
    h.policy({'kind': 'restriction'}, severity='advisory')
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    data['people'] = {pid: data['people'][pid] for pid in h.seed['people'][:2]}
    data['shifts'][0]['type_snapshot']['definition']['requirements'] = [
        {'minimum': 0, 'target': 1, 'maximum': 1, 'from_minute': 0, 'to_minute': None,
         'capabilities': [], 'distinct_capability_seats': False}]
    data['generation_options'] = {'shortlist_width': 0, 'allocation_order': 'tiered'}
    return data


def synthetic_fill_tiers(monkeypatch, count):
    # Fixed-by-constraint positive costs exercise real search rather than the domain-floor shortcut.
    def objectives(self, stage=None):
        if stage not in (None, 'desired'):
            return []
        rows = []
        for index in range(count):
            cost = self.model.new_int_var(0, 1, f'synthetic_fill_{index}')
            self.model.add(cost == 1)
            rows.append((f'fill:desired:synthetic-{index}', [cost], 0))
        return rows
    monkeypatch.setattr(FillPriority, 'objectives', objectives)


@pytest.mark.parametrize('failure', [cp_model.UNKNOWN, cp_model.INFEASIBLE])
def test_unknown_fill_tier_continues_to_later_tiers_without_claiming_optimality(h, monkeypatch, failure):
    data = tier_loop_data(h)
    synthetic_fill_tiers(monkeypatch, 2)
    original, searched = SolverControl.solve, []

    def solve(self, solver, model, tier):
        if tier is not None:
            name = self.context['objective_names'][tier]
            searched.append(name)
            if name == 'fill:desired:synthetic-0':
                assert self.remaining() > 0
                return failure
        return original(self, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', solve)
    result = generate(data, 10, deterministic=True)
    assert searched[-2:] == ['fill:desired:synthetic-1', 'weighted'], searched
    rows = {row['name']: row for row in result['objectives']}
    assert not rows['fill:desired:synthetic-0']['proven_optimal']
    assert rows['fill:desired:synthetic-0']['search_status'] == cp_model.CpSolver().status_name(failure)
    assert rows['fill:desired:synthetic-0']['value'] == 1
    assert rows['fill:desired:synthetic-0'].get('solver_inconsistency', False) == (failure == cp_model.INFEASIBLE)
    warnings = [warning for warning in result.get('warnings', []) if 'Solver inconsistency' in warning]
    assert bool(warnings) == (failure == cp_model.INFEASIBLE)
    if warnings:
        assert 'desired cover' in warnings[0] and 'fill:desired:synthetic-0' not in warnings[0] and 'INFEASIBLE' in warnings[0]
        assert len(warnings) == 1
    assert result['weighted_status'] == 'FEASIBLE' and not result['optimality_proven']
    assert result['validation']['valid'] and result['status'] == 'candidate_feasible'


def test_unknown_coverage_does_not_start_later_tiers(h, monkeypatch):
    data = tier_loop_data(h)
    data['shifts'][0]['type_snapshot']['definition']['requirements'][0]['target'] = 1
    original, searched = SolverControl.solve, []

    def solve(self, solver, model, tier):
        if tier is not None:
            name = self.context['objective_names'][tier]
            searched.append(name)
            if name == 'coverage':
                self.last_solve_deterministic_time = 0
                return cp_model.UNKNOWN
        return original(self, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', solve)
    result = generate(data, 10, deterministic=True)
    assert searched == ['coverage', 'coverage']
    assert result['weighted_status'] == 'UNKNOWN'
    assert not result['optimality_proven']


@pytest.mark.parametrize('failure', [cp_model.UNKNOWN, cp_model.INFEASIBLE])
def test_no_incumbent_coverage_failure_falls_through_to_full_pass(h, monkeypatch, failure):
    data, _ = department(h, 7, people=5, days=2, rules='none', leave_rate=0)
    data['generation_options'] = {'shortlist_width': 1}
    original, searched = SolverControl.solve, []

    def solve(self, solver, model, tier):
        if tier is not None:
            name = self.context['objective_names'][tier]
            searched.append((name, self.context.get('candidates')))
            if name == 'coverage' and self.context.get('candidates') == 'shortlist':
                self.last_solve_deterministic_time = 0
                return failure
        return original(self, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', solve)
    result = generate(data, 5, deterministic=True)
    coverage_attempts = [candidates for name, candidates in searched if name == 'coverage']
    assert coverage_attempts and coverage_attempts[0] == 'shortlist'
    assert 'all' in coverage_attempts
    assert result['status'] == 'candidate_feasible', result
    assert result['weighted_status'] in {'FEASIBLE', 'OPTIMAL'}


def test_coverage_gate_reports_skipped_later_tiers(h, monkeypatch):
    data = tier_loop_data(h)
    requirement = data['shifts'][0]['type_snapshot']['definition']['requirements'][0]
    requirement.update(minimum=3, target=4, maximum=4)
    requirement['capabilities'] = []
    checkpoints = []
    baseline = generate(data, 5, deterministic=True, diagnostic_proposal=True, checkpoint=checkpoints.append)
    assert baseline['status'] == 'candidate_with_gaps' and checkpoints
    resume = next(state for state in reversed(checkpoints) if state.get('checkpoint_assignments'))
    original, seen = SolverControl.solve, []

    def solve(self, solver, model, tier):
        if tier is not None:
            name = self.context['objective_names'][tier]
            seen.append((name, self.context.get('candidates')))
            if name == 'coverage':
                self.last_solve_deterministic_time = 0
                return cp_model.UNKNOWN
        return original(self, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', solve)
    result = generate(data, 5, deterministic=True, diagnostic_proposal=True, resume_state=resume)
    rows = {row['name']: row for row in result['objectives']}
    assert rows['desired_coverage']['search_status'] == 'NOT_RUN'
    assert rows['weighted']['search_status'] == 'NOT_RUN'
    assert rows['desired_coverage']['value'] is None or rows['desired_coverage']['value'] != rows['coverage']['value']
    warnings = [warning for warning in result.get('warnings', []) if 'Skipped later stages' in warning]
    assert len(warnings) == 1
    assert result['weighted_status'] == 'UNKNOWN'


def test_proven_coverage_does_not_report_skipped_later_tiers(h, monkeypatch):
    data = tier_loop_data(h)
    requirement = data['shifts'][0]['type_snapshot']['definition']['requirements'][0]
    requirement.update(minimum=1, target=2, maximum=2)
    checkpoints = []
    baseline = generate(data, 5, deterministic=True, diagnostic_proposal=True, checkpoint=checkpoints.append)
    assert baseline['status'] in {'candidate_feasible', 'candidate_with_gaps'} and checkpoints
    resume = next(state for state in reversed(checkpoints) if state.get('checkpoint_assignments'))
    original = SolverControl.solve

    def solve(self, solver, model, tier):
        if tier is not None and self.context['objective_names'][tier] == 'coverage':
            self.last_solve_deterministic_time = 0
            return cp_model.UNKNOWN
        return original(self, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', solve)
    result = generate(data, 5, deterministic=True, diagnostic_proposal=True, resume_state=resume)
    rows = {row['name']: row for row in result['objectives']}
    assert rows['coverage']['proven_optimal']
    assert rows['desired_coverage'].get('search_status') != 'NOT_RUN'
    assert rows['weighted'].get('search_status') != 'NOT_RUN'
    assert not any('Skipped later stages' in warning for warning in result.get('warnings', []))


@pytest.mark.parametrize('target, expected', [
    (None, ['coverage', 'weighted']),
    (0, ['coverage', 'weighted']),
    (1, ['coverage', 'desired_coverage', 'weighted']),
])
def test_desired_coverage_stage_requires_an_explicit_target(h, target, expected):
    data = tier_loop_data(h)
    data['generation_options']['allocation_order'] = 'weighted'
    data['shifts'][0]['type_snapshot']['definition']['requirements'][0]['target'] = target
    tiers = []
    result = generate(data, 5, _on_objectives=lambda model, rows: tiers.extend(rows))
    assert result['status'] == 'model_ready'
    assert [name for name, _, _ in tiers] == expected


@pytest.mark.parametrize('diagnostic', [False, True], ids=['strict', 'diagnostic'])
@pytest.mark.parametrize('allocation_order', ['tiered', 'weighted'])
def test_random_small_instances_preserve_the_required_only_optimum(h, monkeypatch, diagnostic, allocation_order):
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'session_priority': 10},
             {'service_ids': [h.seed['services'][0]]}, 'advisory')
    template = tier_loop_data(h)
    rng = Random(668)
    solve = SolverControl.solve
    observed_optima = set()
    for case in range(10):
        data = copy.deepcopy(template)
        people = rng.randint(1, 2)
        data['people'] = dict(list(data['people'].items())[:people])
        data['generation_options']['allocation_order'] = allocation_order
        shifts, remaining = [], people
        for index in range(rng.randint(2, 4)):
            shift = copy.deepcopy(template['shifts'][0])
            shift['id'] = f'{shift["id"]}-{index}'
            shift['end'] = (aware(shift['start']) + timedelta(hours=rng.choice((1, 2, 8)))).isoformat()
            minimum = rng.randint(0, 2 if diagnostic else remaining)
            remaining -= minimum
            requirement = shift['type_snapshot']['definition']['requirements'][0]
            requirement.update(minimum=minimum, target=minimum + rng.randint(1, 3), maximum=None)
            shift['service_ids'] = [h.seed['services'][index % 2]]
            shifts.append(shift)
        data.update(shifts=shifts, focus_ids=[shift['id'] for shift in shifts])
        for location in data['locations'].values():
            location['capacity'] = len(shifts)
        frozen = copy.deepcopy(data)
        captured = []
        ready = generate(data, 5, diagnostic_proposal=diagnostic,
                         _on_objectives=lambda model, tiers: captured.append((model, tiers)))
        assert ready['status'] == 'model_ready', (case, ready.get('issues'), ready.get('message'))
        model, tiers = captured[0]
        coverage = cp_model.FlatIntExpr(sum(next(terms for name, terms, _ in tiers if name == 'coverage')))
        model.minimize(coverage)
        oracle = cp_model.CpSolver()
        oracle.parameters.num_workers = 1
        oracle.parameters.max_time_in_seconds = 2
        assert oracle.solve(model) == cp_model.OPTIMAL, case
        optimum = oracle.value(coverage)
        observed_optima.add(optimum)
        observed = []

        def watched(self, solver, candidate, tier):
            status = solve(self, solver, candidate, tier)
            if tier is not None and status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                solution = solver.response_proto.solution
                value = coverage.offset + sum(c * solution[v.index] for v, c in zip(coverage.vars, coverage.coeffs))
                observed.append((self.context['objective_names'][tier], value))
            return status

        with monkeypatch.context() as patch:
            patch.setattr(SolverControl, 'solve', watched)
            result = generate(data, 5, deterministic=True, diagnostic_proposal=diagnostic)
        assert data == frozen
        assert result['status'] == ('candidate_with_gaps' if diagnostic else 'candidate_feasible'), (case, result)
        final = next(row for row in result['objectives'] if row['name'] == 'coverage')
        assert final['value'] == optimum and final['proven_optimal'], (case, optimum, result)
        assert observed and observed[0][0] == 'coverage'
        assert all(value == optimum for _, value in observed), (case, optimum, observed)
        assert observed[-1][0] == 'weighted', observed
    assert 0 in observed_optima
    if diagnostic:
        assert any(value > 0 for value in observed_optima)


@pytest.mark.parametrize('mismatch', [False, True], ids=['normal', 'injected-mismatch'])
def test_validator_required_cover_deficit_matches_the_coverage_receipt(h, monkeypatch, mismatch):
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    data = tier_loop_data(h)
    data['generation_options']['allocation_order'] = 'weighted'
    first = data['shifts'][0]
    first['type_snapshot']['definition']['requirements'][0].update(minimum=3, target=4, maximum=4)
    second = copy.deepcopy(first)
    second.update(id=first['id'] + '-short', end=(aware(first['start']) + timedelta(hours=1)).isoformat())
    second['type_snapshot']['definition']['requirements'][0].update(minimum=2, target=3, maximum=3)
    data['shifts'].append(second)
    data['focus_ids'].append(second['id'])
    if mismatch:
        original_deficit = cp_sat._required_coverage_deficit
        monkeypatch.setattr(cp_sat, '_required_coverage_deficit',
                            lambda validation: original_deficit(validation) + 1)
    result = generate(data, 5, deterministic=True, diagnostic_proposal=True)
    assert result['status'] == 'candidate_with_gaps', result
    validation = evaluate(apply(data, result))
    gaps = [row for row in finding_observations(validation)
            if row['code'] in {'COVERAGE_GAP', 'SCOPED_COVERAGE_GAP'} and row['severity'] == 'error']
    assert {row['code'] for row in gaps} == {'COVERAGE_GAP', 'SCOPED_COVERAGE_GAP'}
    deficit = sum((aware(row['end']) - aware(row['start'])).total_seconds() / 60
                  * (row['detail']['required'] - row['detail']['available']) for row in gaps)
    coverage = next(row for row in result['objectives'] if row['name'] == 'coverage')
    assert deficit == coverage['value'] > 0
    warnings = [warning for warning in result.get('warnings', []) if 'Solver inconsistency' in warning]
    assert bool(warnings) is mismatch
    assert coverage.get('solver_inconsistency', False) is mismatch
    assert coverage['proven_optimal'] is not mismatch
    if warnings:
        assert 'required cover' in warnings[0]


def _scoped_gap_data(h, *, context=False):
    data = tier_loop_data(h)
    first = data['shifts'][0]
    second = copy.deepcopy(first)
    second.update(id=first['id'] + '-outside', start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00')
    second['type_snapshot']['definition']['requirements'][0].update(minimum=3, target=None, maximum=3)
    if context:
        second['workspace_id'] = 'published-context'
    data['shifts'].append(second)
    data['focus_ids'] = [first['id'], second['id']]
    data['generation_session_ids'] = [first['id']]
    return data, second['id']


def test_capability_floor_gap_does_not_raise_coverage_mismatch(h):
    data = tier_loop_data(h)
    capability = h.seed['capability']
    requirement = data['shifts'][0]['type_snapshot']['definition']['requirements'][0]
    requirement.update(minimum=0, target=None, maximum=1,
                       capabilities=[{'capability_id': capability, 'minimum': 1}])
    data['grants'] = [grant for grant in data['grants'] if grant['capability_id'] != capability]
    result = generate(data, 5, deterministic=True, diagnostic_proposal=True)
    assert any(row['code'] == 'CAPABILITY_GAP' for row in result['validation']['findings'])
    coverage = next(row for row in result['objectives'] if row['name'] == 'coverage')
    assert not coverage.get('solver_inconsistency', False), result
    assert not any('Solver inconsistency' in warning for warning in result.get('warnings', [])), result


def test_non_selected_session_gap_does_not_raise_coverage_mismatch(h):
    data, outside_id = _scoped_gap_data(h)
    result = generate(data, 5, deterministic=True, diagnostic_proposal=True)
    assert any(row['shift_id'] == outside_id and row['code'] == 'COVERAGE_GAP'
               for row in result['validation']['findings'])
    coverage = next(row for row in result['objectives'] if row['name'] == 'coverage')
    assert not coverage.get('solver_inconsistency', False), result
    assert not any('Solver inconsistency' in warning for warning in result.get('warnings', [])), result


def test_context_month_gap_does_not_raise_coverage_mismatch(h):
    data, context_id = _scoped_gap_data(h, context=True)
    result = generate(data, 5, deterministic=True, diagnostic_proposal=True)
    context_findings = [row for row in result['validation']['findings'] if row['shift_id'] == context_id]
    assert any(row['code'] == 'COVERAGE_GAP' and row.get('context') for row in context_findings)
    coverage = next(row for row in result['objectives'] if row['name'] == 'coverage')
    assert not coverage.get('solver_inconsistency', False), result
    assert not any('Solver inconsistency' in warning for warning in result.get('warnings', [])), result


def test_grouped_lead_gap_does_not_raise_coverage_mismatch(h):
    data = tier_loop_data(h)
    first = data['shifts'][0]
    from tests.test_engine_workload_review import policy as synthetic_policy
    from tests.test_governed_leadership import line as lead_line
    requirement = lead_line(first['assignment_type_id'], [h.seed['people'][2]], minimum=1, maximum=1)
    requirement['settings']['team_lead_requirement']['count_scope'] = 'concurrent_duties'
    data['policies'].append(synthetic_policy(data, {
        'kind': 'catalogue', 'id': 'team_leads', 'catalogue_version': 1, 'lines': [requirement]}))
    first.update(start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00')
    second = copy.deepcopy(first)
    second.update(id=first['id'] + '-second', start='2026-10-06T10:00:00+03:00', end='2026-10-06T14:00:00+03:00')
    third = copy.deepcopy(first)
    third.update(id=first['id'] + '-third', start='2026-10-06T11:00:00+03:00', end='2026-10-06T15:00:00+03:00')
    data['shifts'] = [first, second, third]
    data['focus_ids'] = [shift['id'] for shift in data['shifts']]
    for location in data['locations'].values():
        location['capacity'] = len(data['shifts'])
    result = generate(data, 5, deterministic=True, diagnostic_proposal=True)
    lead_gaps = [row for row in result['validation']['findings'] if row['code'] == 'LEAD_GAP']
    assert lead_gaps and any(len(row['detail']['affected_shift_ids']) > 1 for row in lead_gaps), result
    coverage = next(row for row in result['objectives'] if row['name'] == 'coverage')
    assert coverage['value'] > 0
    assert not coverage.get('solver_inconsistency', False), result
    assert not any('Solver inconsistency' in warning for warning in result.get('warnings', [])), result


def fill_budget_check(h, monkeypatch, time_share=None):
    data = tier_loop_data(h)
    if time_share is not None:
        data['generation_options']['fill_tier_time_share'] = time_share
    frozen = copy.deepcopy(data)
    synthetic_fill_tiers(monkeypatch, 20)
    original, slices = SolverControl.solve, []

    def solve(self, solver, model, tier):
        status = original(self, solver, model, tier)
        if tier is not None:
            name = self.context['objective_names'][tier]
            budget = solver.parameters.max_deterministic_time
            slices.append((name, budget))
            if name.startswith('fill:'):
                # Deterministic accounting emulates a tier using its entire slice without wall sleeps.
                self.last_solve_deterministic_time = budget
                return cp_model.FEASIBLE
        return status

    monkeypatch.setattr(SolverControl, 'solve', solve)
    result = generate(data, 10, deterministic=True)
    assert data == frozen
    fill = [budget for name, budget in slices if name.startswith('fill:')]
    weighted = next(budget for name, budget in slices if name == 'weighted')
    print('fill budget', {'fill_seconds': sum(fill), 'weighted_seconds': weighted, 'slices': slices})
    share = 0.5 if time_share is None else time_share
    assert len(fill) == (0 if share == 0 else 20) and result['validation']['valid']
    assert weighted >= (1 - share) * 9.8, slices
    assert sum(fill) <= share * 10 + 1e-6, slices
    if share == 0:
        assert all(row['search_status'] == 'NOT_RUN' for row in result['objectives'] if row['name'].startswith('fill:'))


def test_many_fill_tiers_reserve_the_configured_post_coverage_share_for_weighted(h, monkeypatch):
    fill_budget_check(h, monkeypatch)


def test_desired_coverage_keeps_the_fill_reserve(h, monkeypatch):
    data = tier_loop_data(h)
    requirement = data['shifts'][0]['type_snapshot']['definition']['requirements'][0]
    requirement.update(minimum=0, target=3, maximum=3)
    data['generation_options']['fill_tier_time_share'] = 0.5
    synthetic_fill_tiers(monkeypatch, 1)
    original, slices = SolverControl.solve, []

    def solve(self, solver, model, tier):
        status = original(self, solver, model, tier)
        if tier is not None:
            name = self.context['objective_names'][tier]
            budget = solver.parameters.max_deterministic_time
            slices.append((name, budget))
            if name == 'desired_coverage' or name.startswith('fill:'):
                self.last_solve_deterministic_time = budget
                return cp_model.FEASIBLE
        return status

    monkeypatch.setattr(SolverControl, 'solve', solve)
    result = generate(data, 10, deterministic=True)
    desired = [budget for name, budget in slices if name == 'desired_coverage']
    fills = [budget for name, budget in slices if name.startswith('fill:')]
    rows = {row['name']: row for row in result['objectives']}
    assert desired and sum(desired) <= 5 + 1e-6, slices
    assert fills and rows['fill:desired:synthetic-0']['search_status'] != 'NOT_RUN', result


@pytest.mark.parametrize('share', [0, 0.25, 1])
def test_configured_fill_time_share_controls_the_total_budget(h, monkeypatch, share):
    fill_budget_check(h, monkeypatch, share)


def test_fill_tier_time_share_has_a_validated_default():
    assert GenerationOptions().fill_tier_time_share == 0.5
    for value in (0, 0.25, 1):
        assert GenerationOptions(fill_tier_time_share=value).fill_tier_time_share == value
    for value in (-0.1, 1.1, float('inf'), float('nan')):
        with pytest.raises(ValidationError):
            GenerationOptions(fill_tier_time_share=value)


def test_mandatory_fill_receipts_and_rule_off_scarce_capacity_control(h):
    desired = h.configure('assignment_types', {'name': 'Desired-only pool', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'target': 2, 'maximum': 2}]}})['record']['id']
    required = h.configure('assignment_types', {'name': 'Rule-required pool', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']['id']
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'session_priority': 10},
             {'service_ids': [h.seed['services'][0]]}, 'advisory')
    h.policy({'kind': 'minimum_cover', 'minimum': 1}, {'assignment_type_id': required})
    workspace = h.shift(h.workspace(), people=[], typ=desired)['workspace']
    workspace = h.shift(workspace, people=[], typ=required, service=1, location=1)['workspace']
    data = h.snapshot(workspace)
    data['people'] = {pid: data['people'][pid] for pid in h.seed['people'][:2]}
    data['generation_options'] = {'allocation_order': 'tiered', 'shortlist_width': 0}
    required_id = next(s['id'] for s in data['shifts'] if s['assignment_type_id'] == required)
    on = generate(data, 5, deterministic=True)
    off_data = copy.deepcopy(data)
    off_data['policies'] = [p for p in off_data['policies'] if p['definition']['rule']['kind'] != 'minimum_cover']
    off = generate(off_data, 5, deterministic=True)
    assert sum(row['shift_id'] == required_id for row in on['additions']) == 1, on
    assert sum(row['shift_id'] == required_id for row in off['additions']) == 0, off
    assert on['validation']['valid'] and off['validation']['valid']
    on_names = [row['name'] for row in on['objectives']]
    off_names = [row['name'] for row in off['objectives']]
    assert any(name.startswith('fill:mandatory_cover:') for name in on_names), on_names
    assert not any(name.startswith('fill:mandatory_cover:') for name in off_names), off_names
    assert max(i for i, name in enumerate(on_names) if name.startswith('fill:mandatory_cover:')) < min(
        i for i, name in enumerate(on_names) if name.startswith('fill:desired:'))


@pytest.mark.parametrize('engine,diagnostic', [(generate, False), (generate, True), (greedy_generate, False)])
@pytest.mark.parametrize('allocation_order', ['tiered', 'weighted'])
def test_short_mandatory_cover_cannot_be_traded_for_long_desired_cover(h, engine, diagnostic, allocation_order):
    desired = h.configure('assignment_types', {'name': 'Long desired cover', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'target': 1, 'maximum': 1}]}})['record']['id']
    required = h.configure('assignment_types', {'name': 'Short required cover', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']['id']
    h.policy({'kind': 'minimum_cover', 'minimum': 1}, {'assignment_type_id': required})
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'session_priority': 10},
             {'assignment_type_id': desired}, 'advisory')
    workspace = h.shift(h.workspace(), people=[], typ=desired,
        start='2026-10-05T08:00:00+03:00', end='2026-10-05T16:00:00+03:00')['workspace']
    workspace = h.shift(workspace, people=[], typ=required, service=1, location=1,
        start='2026-10-05T10:00:00+03:00', end='2026-10-05T11:00:00+03:00')['workspace']
    data = h.snapshot(workspace)
    data['people'] = {h.seed['people'][0]: data['people'][h.seed['people'][0]]}
    data['generation_options'] = {'allocation_order': allocation_order, 'shortlist_width': 0}
    required_id = next(s['id'] for s in data['shifts'] if s['assignment_type_id'] == required)
    def run(candidate):
        frozen = copy.deepcopy(candidate)
        result = (engine(candidate, 5, deterministic=True, diagnostic_proposal=diagnostic)
                  if engine is generate else engine(candidate, 5))
        assert candidate == frozen
        return result
    on = run(data)
    off_data = copy.deepcopy(data)
    off_data['policies'] = [p for p in off_data['policies'] if p['definition']['rule']['kind'] != 'minimum_cover']
    off = run(off_data)
    assert len(off['additions']) == 1 and off['additions'][0]['shift_id'] != required_id, off
    assert len(on['additions']) == 1 and on['additions'][0]['shift_id'] == required_id, on
    if diagnostic:
        assert on['status'] == 'candidate_with_gaps' and not on['optimality_proven']


@pytest.mark.parametrize('engine,diagnostic', [(generate, False), (generate, True), (greedy_generate, False)])
def test_mandatory_minimum_allows_requested_additional_desired_cover(h, engine, diagnostic):
    duty = h.configure('assignment_types', {'name': 'Required and desired places', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'target': 2, 'maximum': 2}]}})['record']['id']
    h.policy({'kind': 'minimum_cover', 'minimum': 1}, {'assignment_type_id': duty})
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'session_priority': 10},
             {'assignment_type_id': duty}, 'advisory')
    data = h.snapshot(h.shift(h.workspace(), people=[], typ=duty)['workspace'])
    data['people'] = {pid: data['people'][pid] for pid in h.seed['people'][:2]}
    data['generation_options'] = {'allocation_order': 'tiered', 'shortlist_width': 0}
    result = (engine(data, 5, deterministic=True, diagnostic_proposal=diagnostic)
              if engine is generate else engine(data, 5))
    assert len(result['additions']) == 2 and result['validation']['valid'], result


@pytest.mark.xfail(strict=False, reason='Deferred (c): ordered-priorities-tiers design; '
                   'strength-weighted sums are not order-only')
def test_allocation_tier_is_order_only_instead_of_summing_matching_lines(h):
    first, second = h.seed['people'][:2]
    for _ in range(2):
        h.policy({'kind': 'allocation_preference', 'priority': 0, 'session_priority': 1}, {'person_id': first}, 'advisory')
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'session_priority': 2}, {'person_id': second}, 'advisory')
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    shift = data['shifts'][0]
    first_key, second_key = allocation_tier(data, shift, first), allocation_tier(data, shift, second)
    print('allocation key', {'two_place_1': first_key, 'one_place_2': second_key,
                               'types': [type(first_key).__name__, type(second_key).__name__]})
    assert first_key < second_key


def test_allocation_tier_ignores_other_number_fields_and_fairness(h, monkeypatch):
    from packages.domain import fairness_distribution
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_preference', 'session_priority': 1, 'priority': 100},
             {'person_id': person}, 'advisory')
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    monkeypatch.setattr(fairness_distribution, 'marginal_priority', lambda *args: pytest.fail('fairness entered tier'))
    key = allocation_tier(data, data['shifts'][0], person)
    control = copy.deepcopy(data)
    control['policies'] = copy.deepcopy(control['policies'])
    for policy in control['policies']:
        if policy['definition']['rule']['kind'] == 'allocation_preference':
            policy['definition']['rule']['priority'] = 0
    assert key > 0 and key == allocation_tier(control, control['shifts'][0], person)

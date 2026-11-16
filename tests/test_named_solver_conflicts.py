"""Named proofs describe the strict model and never authorize a safety waiver."""
import json
import time

import pytest
from ortools.sat.python import cp_model

from packages.contracts.schemas import GenerationOptions, SolverConflictExplanation
from packages.domain.cp_sat import generate, _prepare
from packages.domain.solver_control import SolverControl
from packages.domain.solver_diagnostics import explain_conflict
from packages.domain.solver_provenance import policy_name


def policy(kind, line, *, ident='rules', slot=None, **rule):
    return {'id': ident, 'name': 'Synthetic staffing' if kind == 'minimum_cover' else 'Synthetic period caps',
            'revision': 2, 'starts_on': '2026-12-01', 'ends_on': '2027-01-01',
            'template_line': {'id': f'line-{line}', 'line': line, 'slot': slot},
            'definition': {'severity': 'mandatory', 'scope': {}, 'rule': {'kind': kind, **rule}}}


def capacity_fixture(sessions=2, people=3, caps=(2, 1, 0)):
    model = cp_model.CpModel()
    floor = policy('minimum_cover', 7, slot='min', minimum=2)
    variables = {(s, p): model.new_bool_var(f'person-{p}-session-{s}') for s in range(sessions) for p in range(people)}
    data = {'fingerprint': 'frozen', 'organisation': {'timezone': 'Asia/Riyadh'}, 'policies': [floor],
            'people': {str(p): {'display_name': f'Person {p}'} for p in range(people)}, 'shifts': []}
    for s in range(sessions):
        start, end = f'2026-12-{4 + s:02}T08:00:00+03:00', f'2026-12-{4 + s:02}T12:00:00+03:00'
        model.add(-sum(variables[s, p] for p in range(people)) <= -2).with_name(policy_name(
            floor, data=data, person_ids=map(str, range(people)), session_ids=[str(s)], start=start, end=end))
        data['shifts'].append({'id': str(s), 'type_snapshot': {'name': 'Clinic'}, 'start': start, 'end': end})
    for p, cap in enumerate(caps):
        source = policy('block_quota', p + 10, slot='max', metric='duties', comparison='maximum', amount=cap)
        row = {'person_id': str(p), 'starts_on': '2026-12-01', 'ends_on': '2027-01-01'}
        model.add(sum(variables[s, p] for s in range(sessions)) <= cap).with_name(policy_name(
            source, data=data, row=row, person_ids=[str(p)], session_ids=map(str, range(sessions))))
        data['policies'].append(source)
    return model, data


def test_named_source_slots_and_confirmed_arithmetic_survive_model_serialization():
    model, data = capacity_fixture()
    before = str(model.proto)
    from packages.domain.solver_resume import pack, unpack
    data = unpack(pack(data), model)
    assert all(len(c.name) < 300 for c in model.proto.constraints)
    restored = cp_model.CpModel()
    assert restored.proto.parse_text_format(before)
    proof = explain_conflict(restored, data, SolverControl(), 60)
    SolverConflictExplanation.model_validate(proof)
    assert proof['proven'] and proof['strict_status'] == proof['explanation_status'] == 'INFEASIBLE'
    assert proof['basis'] == 'capacity_relaxation' and not proof['minimal'] and not proof['exhaustive']
    sources = {(r['line_id'], r['line'], r['slot']) for r in proof['requirements']}
    assert sources == {('line-7', 7, 'min'), ('line-11', 11, 'max'), ('line-12', 12, 'max')}
    cause, = proof['causes']
    assert (cause['evidence']['required_places'], cause['evidence']['maximum_possible']) == (4, 3)
    assert cause['evidence']['complete'] and 'need 4 places' in cause['message']
    assert set(cause['evidence']['session_ids']) == {'0', '1'}
    assert {p['name'] for p in proof['people']} == {'Person 0', 'Person 1', 'Person 2'}
    caps = [d for d in cause['decisions'] if d['kind'] == 'propose_cap_change']
    assert caps and all(d['requires_approval'] and d['effect_status'] == 'not_checked' and len(d['person_ids']) == 1 for d in caps)
    assert all(d['line_id'] != 'line-7' for d in caps)
    assert len([d for d in cause['decisions'] if d['kind'] == 'open_rule' and d['line_id'] == 'line-7']) == 1
    eligibility, = [d for d in cause['decisions'] if d['kind'] == 'review_eligibility']
    assert eligibility['session_ids'] == ['0', '1']
    assert (eligibility['starts_on'], eligibility['ends_on']) == ('2026-12-04', '2026-12-06')
    assert not any('definition' in r for r in proof['requirements'])
    assert str(model.proto) == before


def test_unknown_explanation_keeps_strict_infeasibility_without_inventing_names():
    model, data = capacity_fixture()
    control = SolverControl()
    control.solve = lambda *args: cp_model.UNKNOWN
    proof = explain_conflict(model, data, control, 60)
    assert proof['strict_status'] == 'INFEASIBLE' and proof['explanation_status'] == 'UNKNOWN'
    assert not proof['proven'] and not proof['requirements'] and not proof['causes']
    SolverConflictExplanation.model_validate(proof)


def test_conditionally_disabled_impossibility_is_never_a_constant_named_proof():
    model, data = capacity_fixture(sessions=1, people=3, caps=(1, 1, 1))
    off = model.new_bool_var('off')
    model.add(off == 0)
    model.add_bool_or([]).only_enforce_if(off).with_name(policy_name(data['policies'][0]))
    proof = explain_conflict(model, data, SolverControl(), 60)
    assert proof['implementation_error'] and not proof['proven']


def test_core_refines_a_line_to_its_conflicting_person_and_window():
    model, data = capacity_fixture()
    irrelevant = model.new_bool_var('unrelated-person-window')
    source = data['policies'][-1]
    model.add(irrelevant <= 0).with_name(policy_name(source, row={
        'starts_on': '2027-01-01', 'ends_on': '2027-02-01'}, person_ids=['other']))
    proof = explain_conflict(model, data, SolverControl(), 60)
    assert proof['proven']
    assert all('other' not in r['person_ids'] for r in proof['requirements'])


def test_unknown_refinement_retains_the_first_confirmed_witness():
    model, data = capacity_fixture()
    control, calls = SolverControl(), []
    original = control.solve

    def first_proof_only(*args):
        calls.append(True)
        return original(*args) if len(calls) == 1 else cp_model.UNKNOWN

    control.solve = first_proof_only
    proof = explain_conflict(model, data, control, 60)
    assert proof['proven'] and proof['basis'] == 'capacity_relaxation'
    assert proof['explanation_status'] == 'INFEASIBLE' and proof['requirements']
    assert not proof['causes'][0]['evidence']['complete']


def test_lp_construction_consumes_the_same_explanation_allowance(monkeypatch):
    from types import SimpleNamespace
    from packages.domain import solver_capacity
    tick = [100.0]
    monkeypatch.setattr(solver_capacity.time, 'monotonic', lambda: tick[0])

    def create_solver(name):
        tick[0] += 2
        return SimpleNamespace(NOT_SOLVED=solver_capacity.pywraplp.Solver.NOT_SOLVED)

    monkeypatch.setattr(solver_capacity.pywraplp.Solver, 'CreateSolver', create_solver)
    status, value = solver_capacity.lp(object(), [0], 1, control=SimpleNamespace(check=lambda: None))
    assert status == solver_capacity.pywraplp.Solver.NOT_SOLVED and value is None


@pytest.mark.postgres
def test_catalogue_compiler_preserves_line_ids_indexes_slots_and_quota_windows(pg):
    from tests.test_rule_lines import line, rule
    typ = pg.configure('assignment_types', {'name': 'Synthetic line provenance', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0}]}})['record']['id']
    workspace = pg.shift(pg.workspace(), typ=typ, people=[])['workspace']
    data = pg.snapshot(workspace)
    minimum, maximum = line(numbers={'min': {'value': 2}}), line(numbers={'max': {'value': 0}},
        settings={'block_quota': {'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}})
    floor = rule('people_per_session', minimum, department_id=workspace['department_id'])
    cap = rule('duties_per_period', maximum, department_id=workspace['department_id'])
    data['policies'] = [floor, cap]
    data['quota_context'] = {'complete': True, 'start': '2026-10-04T00:00:00+03:00',
                             'end': '2026-10-11T00:00:00+03:00'}
    result = generate(data, 3, deterministic=True, feasibility_only=True)
    assert result['status'] == 'requirements_conflict', result.get('issues')
    proof = result['conflict']
    assert proof['proven']
    assert {(r['line_id'], r['line'], r['slot']) for r in proof['requirements']} == {
        (minimum['id'], 0, 'min'), (maximum['id'], 0, 'max')}
    caps = [r for r in proof['requirements'] if r['rule_id'] == cap['id']]
    assert all(r['person_ids'] and r['starts_on'] == '2026-10-04' and r['ends_on'] == '2026-10-11' for r in caps)


@pytest.mark.postgres
def test_strict_unknown_never_claims_that_rules_clash(pg, monkeypatch):
    from tests.test_solver_diagnostic_continuation import shortfall
    def unknown(control, *args):
        control.last_solve_deterministic_time = 0
        return cp_model.UNKNOWN
    monkeypatch.setattr(SolverControl, 'solve', unknown)
    result = generate(shortfall(pg), 3, deterministic=True)
    assert result['status'] == 'needs_more_time' and result['weighted_status'] == 'UNKNOWN'
    assert not result.get('conflict')


@pytest.mark.postgres
def test_explanation_allowance_reuses_versioned_department_settings_and_undo(pg):
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from tests.test_staff_wishes import department_settings
    before = pg.get('admin', '/api/me')
    dept, org = before['department'], before['organisation']
    options = {**dept['generation_defaults'], 'diagnostic_seconds': 37}
    saved = pg.command('admin', 'department.settings', {**department_settings(pg), 'generation_defaults': options},
                       dept['id'], org['revision'])
    assert saved['department']['generation_defaults']['diagnostic_seconds'] == 37
    with transaction(pg.app.state.engine) as db:
        history = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'department_settings')))
        assert any(row.snapshot['generation_defaults']['diagnostic_seconds'] == 37 for row in history)
    undone = pg.command('admin', 'department.settings', {**saved['previous'], 'reason': 'Undo explanation allowance'},
                        dept['id'], saved['organisation']['revision'])
    assert GenerationOptions.model_validate(undone['department']['generation_defaults']) == GenerationOptions.model_validate(dept['generation_defaults'])


@pytest.fixture(scope='module')
def department_conflict(scale_harness):
    from tests.scheduler_departments import department
    pg = scale_harness('pg')
    floor = pg.policy({'kind': 'minimum_cover', 'minimum': 1})
    cap = pg.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
                     'metric': 'duties', 'comparison': 'maximum', 'amount': 0})
    data, _ = department(pg, 17, people=150, days=28, clinics=9, rules='minimal', leave_rate=0)
    data['policies'].extend({**data['policies'][0], 'id': f'history-{n}', 'revision': n + 1,
                             'active': False, 'status': 'retired'} for n in range(1000))
    built = _prepare(data, 300, 1, SolverControl(), GenerationOptions(), True, False)
    assert 'model' in built, built
    return built, floor['id'], cap['id']


@pytest.mark.postgres
@pytest.mark.parametrize('allowance', [300, None], ids=['measure-generous', 'configured-default'])
def test_department_sized_compilation_returns_a_bounded_named_proof(department_conflict, request, allowance):
    built, floor_id, cap_id = department_conflict
    data = built['data']
    allowance = allowance or GenerationOptions().diagnostic_seconds
    started, cpu = time.monotonic(), time.process_time()
    proof = explain_conflict(built['model'], data, SolverControl(), allowance)
    measured = {'seconds': time.monotonic() - started, 'cpu_seconds': time.process_time() - cpu,
                'people': len(data['people']), 'sessions': len(data['focus_ids']),
                'retired_rules': 1000, 'requirements': len(proof['requirements']),
                'allowance_seconds': allowance, 'solver_status': proof['solver_status'],
                'explanation_status': proof['explanation_status']}
    request.node.user_properties.append(('named_conflict_measurement', json.dumps(measured)))
    assert measured['people'] >= 150 and measured['sessions'] >= 500
    assert proof['proven'] and proof['explanation_status'] == 'INFEASIBLE', measured
    assert {floor_id, cap_id} <= {r['rule_id'] for r in proof['requirements']}
    assert not any(r.get('rule_id', '').startswith('history-') for r in proof['requirements'])
    assert proof['timing']['budget_seconds'] == allowance


@pytest.mark.postgres
def test_worker_and_api_retain_strict_proof_beside_a_diagnostic_proposal(pg, monkeypatch):
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.worker import main as worker
    from packages.domain import cp_sat
    from packages.domain.solver_rules import COMPILER_VERSION
    from tests.test_solver_diagnostic_continuation import shortfall

    data = shortfall(pg, quota=True)
    workspace = pg.detail(data['workspace']['id'])['workspace']
    job_id = pg.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                        revision=workspace['revision'])['job']['id']
    with transaction(pg.app.state.engine, write=True) as db:
        job = db.get(m.Job, job_id)
        job.input_snapshot = {**job.input_snapshot, 'generation_engine': 'cp_sat', 'compiler_version': COMPILER_VERSION}
    original = cp_sat.generate

    def bounded(snapshot, **kwargs):
        kwargs['workers'] = 1
        return original(snapshot, 3, deterministic=True, **kwargs)

    monkeypatch.setattr(cp_sat, 'generate', bounded)
    assert worker.process_one(pg.app.state.engine)
    result = pg.get('coordinator', '/api/jobs/' + job_id)['job']['result']
    assert result['status'] == 'candidate_with_gaps' and result['diagnostic_proposal']
    assert result['weighted_status'] in {'FEASIBLE', 'OPTIMAL'} and not result['optimality_proven']
    assert result['conflict']['strict_status'] == result['conflict']['explanation_status'] == 'INFEASIBLE'
    assert result['conflict']['proven'] and not result['validation']['valid']
    SolverConflictExplanation.model_validate(result['conflict'])

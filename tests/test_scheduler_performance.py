"""Generous performance guards for the auto-scheduler and the validator.

A month for 80 people with every rule kind: model size is the deterministic guard (it grew
quadratically before: pair variables per trainee x supervisor, duplicate helper literals);
CPU-time limits exclude other processes' contention and only catch big regressions.
"""
import copy
import gc
import json
import time
import uuid
from itertools import count

import pytest

from packages.domain.cp_sat import generate
from packages.domain.validation import evaluate
from tests.scheduler_departments import apply, assert_no_mandatory_single_addition, department


@pytest.fixture
def stable_department_harness(tmp_path, monkeypatch):
    """Seed fresh IDs in a fixed order: the solver's canonical model order includes database-generated IDs."""
    from apps.api.db import make_engine, transaction
    from packages.contracts.schemas import ROLE_NAMES
    from scripts.manage import migrate, demo_data
    from tests.conftest import Harness, TEST_PASSWORD

    identifiers = count(1)
    monkeypatch.setattr(uuid, 'uuid4', lambda: uuid.UUID(int=next(identifiers), version=4))
    url = 'sqlite:///' + str(tmp_path / 'stable-department.db')
    migrate(url)
    engine = make_engine(url)
    with transaction(engine, write=True) as db:
        seed = demo_data(db, {role: TEST_PASSWORD for role in ROLE_NAMES})
    engine.dispose()
    harness = Harness(url, seed)
    yield harness
    for client in harness.clients.values():
        client.close()
    harness.app.state.engine.dispose()


@pytest.fixture(scope='module')
def eighty_people_month(scale_harness):
    h = scale_harness('h')
    data, _ = department(h, 7, people=80, days=28, rules='full', leave_rate=0.05)
    preparation = []
    started = time.process_time()

    def progress(update):
        if update['stage'] == 'finding' and not preparation:
            preparation.append(time.process_time() - started)

    result = generate(data, 240, feasibility_only=True, progress=progress)
    elapsed = time.process_time() - started
    size = result['model_size']
    print('scheduler performance', {'cpu_seconds': round(elapsed, 1), 'preparation_cpu_seconds': preparation,
                                    **result['phase_timings'], **size})
    draft = apply(data, result)
    started = time.process_time()
    validation = evaluate(draft)
    return result, preparation, elapsed, validation, time.process_time() - started


def test_month_for_eighty_people_builds_a_compact_model_and_validates_quickly(eighty_people_month):
    result, preparation, _, validation, _ = eighty_people_month
    assert result['status'] == 'candidate_feasible', result.get('status')
    size = result['model_size']
    # 80 people x 84 sessions: about 8,000 candidate places. Before these changes the model had ~50k variables.
    assert size['variables'] < 20_000 and size['constraints'] < 40_000, size
    assert len(preparation) == 1, preparation
    assert validation['error_count'] == 0


@pytest.mark.cpu_budget
def test_month_for_eighty_people_cpu_budget(eighty_people_month):
    _, preparation, elapsed, _, validation_elapsed = eighty_people_month
    # The department-scale profile sets the under-10-second target; this smaller
    # guard catches the same preparation regressions with a CI-aware margin.
    assert preparation[0] < _budget(30), preparation
    assert elapsed < 240
    assert validation_elapsed < 5


def _budget(seconds):
    """CPU-time budgets hold on a developer machine; coverage tracing (the CI pipeline) makes the pure-Python
    model build several times slower, so budgets scale with it. Either way a several-fold regression fails."""
    import coverage
    return seconds * (3 if coverage.Coverage.current() else 1)


def test_preparation_repeat_keeps_model_shape_and_fixed_seed_solution(h):
    """Run-local indexes are an optimization only: repeated fixed input builds the same model and answer."""
    from ortools.sat.python import cp_model

    data, _ = department(h, 41, people=18, days=7, rules='full', leave_rate=0.05)
    models = []
    for _ in range(2):
        captured = []
        result = generate(copy.deepcopy(data), 30, deterministic=True,
                          _on_objectives=lambda model, tiers: captured.append(model))
        assert result['status'] == 'model_ready', result
        assert len(captured) == 1
        models.append(captured[0])
    sizes = [(len(model.proto.variables), len(model.proto.constraints)) for model in models]
    assert sizes[0] == sizes[1]

    def solution(model):
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 41
        solver.parameters.max_time_in_seconds = 10
        status = solver.Solve(model)
        assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE), status
        return tuple(solver.ResponseProto().solution)

    assert solution(models[0]) == solution(models[1])


@pytest.fixture(scope='module')
def live_solution_department_month():
    """Bound the added live read/diff cost for all 75,000 department-scale candidate pairs.

    Reuse the callback's existing checkpoint list. Its unchanged native copy (~47 ms on Windows/OR-Tools 9.15)
    is measured separately; this event budget does not claim the complete callback takes 20 ms.
    """
    from ortools.sat.python import cp_model
    from packages.domain.solver_control import SolverControl

    model = cp_model.CpModel()
    choices = {(f'session-{session}', f'person-{person}'): model.new_bool_var('')
               for session in range(500) for person in range(150)}
    coverage = [(f'session-{session}',
                 sum(choices[f'session-{session}', f'person-{person}'] for person in range(150)), 2)
                for session in range(500)]
    response = cp_model.CpSolverResponse()
    response.solution.extend(int(index % 150 < 2) for index in range(len(choices)))
    copy_started = time.process_time()
    solution = list(response.solution)
    checkpoint_copy = time.process_time() - copy_started
    control = SolverControl()
    control.live_solutions(choices, set(), coverage, 8)
    first = control.live_event(solution, 0, 0, 0)
    started = time.process_time()
    events = [control.live_event(solution, 0, 0, index + 1) for index in range(50)]
    per_event = (time.process_time() - started) / len(events)
    measured = {'people': 150, 'sessions': 500, 'candidate_pairs': len(choices),
                'event_cpu_seconds': per_event, 'existing_checkpoint_copy_cpu_seconds': checkpoint_copy}
    print('live solution department month', measured)
    return first, events, measured


def test_live_solution_department_month_bounds_payload_and_keeps_headcount(live_solution_department_month):
    first, events, measured = live_solution_department_month
    assert measured['candidate_pairs'] == 75_000
    assert first['truncated'] and first['adds'] == first['removes'] == []
    assert all(event['open_places'] == 0 and not event['truncated'] for event in events)
    assert all(event['adds'] == event['removes'] == [] for event in events)


@pytest.mark.cpu_budget
def test_live_solution_department_month_event_cpu_budget(live_solution_department_month, request):
    from tests.test_performance_budget import python_budget
    measured = live_solution_department_month[2]
    request.node.user_properties.append(('live_solution_performance', json.dumps(measured)))
    assert measured['event_cpu_seconds'] < python_budget(.020), measured


@pytest.fixture(scope='module')
def department_year_date_set_checks():
    """500 sessions x 150 people must not rescan 100 yearly entries per person/session."""
    from datetime import date, timedelta
    from packages.domain import date_sets
    from packages.domain.conditions import conditions_match
    from packages.domain.resources import date_set_rule
    entries = [{'id': str(n), 'name': f'Entry {n}', 'repeat': 'yearly_gregorian',
                'first_day': '2024-01-01', 'last_day': '2024-12-31'} for n in range(100)]
    first = date(2026, 1, 1)
    shifts = [{'start': f'{first + timedelta(days=n % 365)}T08:00:00Z',
               'end': f'{first + timedelta(days=n % 365)}T12:00:00Z'} for n in range(500)]
    data = {'organisation': {'timezone': 'UTC'}, 'shifts': shifts,
            'workspace': {'starts_on': '2026-01-01', 'ends_on': '2027-01-01'},
            'date_sets': {'holidays': {'id': 'holidays', 'name': 'Holidays', 'entries': entries}}}
    tree = {'conditions': [{'field': 'person_ids', 'values': [str(n) for n in range(150)]},
                            {'field': 'date_set_ids', 'values': ['holidays'], 'entry_names': ['Entry 99']}]}
    calendar_rule = {'date_set_id': 'holidays', 'entry_names': ['Entry 99'], 'windows': []}
    calendar = {'definition': {'date_set_rules': [calendar_rule]}}
    started = time.process_time()
    cache = data['_date_set_cache'] = date_sets.prepare_date_sets(data)

    def unexpected_resolution(*args, **kwargs):
        raise AssertionError('A hot-path lookup must use the snapshot day index')

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(date_sets, 'resolve_date_sets', unexpected_resolution)
        matches = [conditions_match(data, tree, str(person), shift, None, None)
                   for shift in shifts for person in range(150)]
        days = [(date_set_rule(calendar, first + timedelta(days=n), data=data) is calendar_rule,
                 len(date_sets.date_set_markers(data, first + timedelta(days=n)))) for n in range(365)]
    elapsed = time.process_time() - started
    print('date-set department year lookups', {'cpu_seconds': elapsed, 'person_session_checks': 75_000})
    return cache, matches, days, elapsed


def test_date_set_day_index_keeps_department_year_checks_bounded(department_year_date_set_checks):
    cache, matches, days, _ = department_year_date_set_checks
    assert len(cache['days']) == 365 and all(isinstance(v, frozenset) for v in cache['days'].values())
    assert all(matches)
    assert all(matched and markers == 100 for matched, markers in days)


@pytest.mark.cpu_budget
def test_date_set_day_index_department_year_cpu_budget(department_year_date_set_checks):
    elapsed = department_year_date_set_checks[3]
    assert elapsed < _budget(3), elapsed


def test_parallel_clinics_grow_the_model_linearly(h):
    """Four parallel morning and afternoon clinics a day: the model grows with candidate places, not with their
    square (pairwise overlap, rest and travel constraints once made 142 people x 476 sessions 1.26M constraints)."""
    sizes = []
    for clinics in (1, 4):
        data, _ = department(h, 3, people=40, days=14, rules='full', leave_rate=0.05, clinics=clinics)
        result = generate(data, 60, feasibility_only=True)
        assert result['status'] == 'candidate_feasible', result.get('status')
        places = sum(1 for s in data['shifts'] for _ in data['people'])
        sizes.append({**result['model_size'], 'places': places})
    print('parallel clinic model growth', sizes)
    growth = sizes[1]['places'] / sizes[0]['places']  # 3 -> 9 sessions a day: 3x the places
    assert sizes[1]['variables'] < 1.3 * growth * sizes[0]['variables'], sizes
    assert sizes[1]['constraints'] < 1.3 * growth * sizes[0]['constraints'], sizes


def _assert_score_components_match(result):
    # The objective receipt uses the run's explicit change baseline; the saved-score receipt omits stability.
    objective_components = result['objective_components']
    for name, value in result['compiled_component_values'].items():
        expected = objective_components[name]['value']
        if expected is None:
            assert value is None, (name, value)
        else:
            assert value is not None and abs(value - expected) <= 0.0001, (name, value, expected)


def _run(data):
    """A default run as the worker makes it (2 solver workers, 5-minute budget); the score is packages/domain/score.py's."""
    # process_time includes both solver threads; keep the existing aggregate budget.
    started = time.process_time()
    result = generate(data, 300, workers=2)
    elapsed = time.process_time() - started
    names = [t['name'] for t in result.get('objectives', [])]
    saved_score = result.get('summary', {}).get('score', {})
    print('auto-schedule', {'cpu_seconds': round(elapsed, 1), 'status': result['status'],
                            **result.get('phase_timings', {}), **result.get('model_size', {}),
                            'objectives': [(t['name'], t['value'], t['proven_optimal']) for t in result.get('objectives', [])],
                            'score': saved_score.get('score'), 'components': saved_score.get('components'),
                            'objective_components': result.get('objective_components'),
                            'compiled_component_values': result.get('compiled_component_values')})
    assert result['status'] == 'candidate_feasible', result.get('status')
    assert result['validation']['error_count'] == 0
    _assert_score_components_match(result)
    return result, elapsed, names


@pytest.fixture(scope='module')
def department_month_priorities(scale_harness):
    """The department benchmark reaches coverage and the weighted score with the default worker search."""
    data, _ = department(scale_harness('h'), 7, people=142, days=28, rules='full', leave_rate=0.05)
    return _run(data)


def test_department_month_reaches_every_priority_quickly(department_month_priorities):
    result, _, names = department_month_priorities
    assert names == ['coverage', 'weighted']
    assert result['objectives'][0]['value'] == 0
    components = result['summary']['score']['components']
    assert components['coverage']['value'] == 1
    # Fixed normalization measured 69.4 (the committed C1 candidate rescored 65.3); retain 1.3 points of variation.
    assert result['summary']['score']['score'] >= 68.1, result['summary']['score']


@pytest.mark.cpu_budget
def test_department_month_priorities_cpu_budget(department_month_priorities):
    assert department_month_priorities[1] < _budget(150)


@pytest.fixture(scope='module')
def parallel_clinic_week_priorities(scale_harness):
    """142 people, a week of 8 parallel clinics (119 sessions): before, the model alone was quadratic in clinics."""
    data, _ = department(scale_harness('h'), 7, people=142, days=7, rules='full', leave_rate=0.05, clinics=8)
    return _run(data)


def test_parallel_clinic_week_reaches_every_priority(parallel_clinic_week_priorities):
    result, _, names = parallel_clinic_week_priorities
    assert names == ['coverage', 'weighted']
    assert result['objectives'][0]['value'] == 0
    components = result['summary']['score']['components']
    assert components['coverage']['value'] == 1
    # Fixed normalization measured 71.4 (the committed C1 candidate rescored 67.8); retain 1.6 points of variation.
    assert result['summary']['score']['score'] >= 69.8, result['summary']['score']


@pytest.mark.cpu_budget
def test_parallel_clinic_week_priorities_cpu_budget(parallel_clinic_week_priorities):
    assert parallel_clinic_week_priorities[1] < _budget(90)


@pytest.fixture
def weighted_department_measurement(stable_department_harness, request, tmp_path):
    """Optimize a full-rule department month; feasibility-only misses objective construction and search costs."""
    from apps.api.snapshots import snapshot_hash

    data, _ = department(stable_department_harness, 7, people=150, days=28, rules='full', leave_rate=0.05, clinics=9)
    assert len(data['people']) >= 150 and len(data['shifts']) >= 500
    fingerprint = snapshot_hash(data)
    (tmp_path / 'department-150-532.json').write_text(json.dumps(data, sort_keys=True, separators=(',', ':')), encoding='utf-8')
    (tmp_path / 'department-150-532.sha256').write_text(fingerprint + '\n', encoding='utf-8')
    began, cpu = time.perf_counter(), time.process_time()
    result = generate(data, 300, workers=2)
    timings = {'wall_seconds': time.perf_counter() - began, 'cpu_seconds': time.process_time() - cpu,
               **result['phase_timings']}
    evidence = {'people': len(data['people']), 'sessions': len(data['shifts']), 'solve_seconds': 300, 'workers': 2,
                'snapshot_sha256': fingerprint,
                'timings': timings,
                'model_size': result['model_size'], 'status': result['status'],
                'objectives': result['objectives'], 'weighted_status': result.get('weighted_status'),
                'objective_components': result.get('objective_components'),
                'compiled_component_values': result.get('compiled_component_values')}
    request.node.user_properties.append(('weighted_department_performance', json.dumps(evidence)))
    print('weighted department performance', evidence)
    return result, evidence


def test_weighted_objective_is_bounded_for_150_people_and_532_sessions(weighted_department_measurement):
    result, evidence = weighted_department_measurement
    assert result['status'] == 'candidate_feasible', evidence
    assert result['validation']['error_count'] == 0
    assert [step['name'] for step in result['objectives']] == ['coverage', 'weighted'], evidence
    assert result['weighted_status'] in {'OPTIMAL', 'FEASIBLE'}, evidence
    assert result['objectives'][0]['value'] == 0, evidence
    assert result['objective_components']['coverage']['value'] == 1, evidence
    _assert_score_components_match(result)
    assert result['model_size']['variables'] < 180_000, evidence
    assert result['model_size']['constraints'] < 300_000, evidence


@pytest.mark.cpu_budget
def test_weighted_department_150_people_532_sessions_cpu_budget(weighted_department_measurement):
    _, evidence = weighted_department_measurement
    assert evidence['timings']['cpu_seconds'] < _budget(180), evidence


def test_fractional_hours_amount_dial_compiles_at_department_scale(h):
    from packages.contracts.schemas import TypeDefinition
    from tests.test_balance_between_people import catalogue
    from tests.test_engine_fairness_review import uid

    data, _ = department(h, 7, people=150, days=28, rules='none', leave_rate=0, clinics=9)
    data['rotations'] = []
    for shift in data['shifts']:
        shift['start'] = shift['start'][:10] + 'T08:00:00+03:00'
        shift['end'] = shift['start'][:10] + 'T16:30:00+03:00'
        shift['type_snapshot']['definition'] = TypeDefinition.model_validate(
            {'requirements': [{'minimum': 1, 'maximum': 1}]}).model_dump(mode='json')
    person = next(iter(data['people']))
    goal = catalogue(people=(person,), metric='hours', period='month')
    goal['department_id'] = data['workspace']['department_id']
    lines = goal['definition']['rule']['lines']
    lines[0]['settings'] = {'balance_goal': {'share_adjustment': {'mode': 'amount', 'value': 1}}}
    lines.insert(0, {'id': uid(900), 'numbers': {'apply': {'value': None, 'level': 'medium'}}})
    data['policies'] = [goal]
    assert len(data['people']) >= 150 and all(p['active'] for p in data['people'].values())
    assert len(data['shifts']) >= 500
    captured = []
    result = generate(data, 1, deterministic=True, _on_objectives=lambda model, tiers: captured.append(model))
    assert result['status'] == 'model_ready', result
    assert len(captured) == 1 and captured[0].validate() == ''


@pytest.mark.cpu_budget
def test_overtime_only_zero_maximum_binds_extra_cover_for_a_department_month(h, request):
    from ortools.sat.python import cp_model

    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.snapshots import domain_snapshot
    from tests.test_overtime_extra_cover import extra_type

    cover = extra_type(h)
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    data, info = department(h, 7, people=150, days=28, rules='none', leave_rate=0, clinics=9)
    workspace = data['workspace']
    h.policy({'kind': 'block_quota', 'starts_on': workspace['starts_on'], 'ends_on': workspace['ends_on'],
              'metric': 'duties', 'comparison': 'maximum', 'amount': 0, 'overtime': 'only'},
             {'person_id': person})
    with transaction(h.app.state.engine) as db:
        current = domain_snapshot(db, db.get(m.Workspace, workspace['id']), mandatory_rules=True)
    data.update({key: current[key] for key in ('policies', 'quota_context') if key in current})
    data['types'][cover['id']] = cover
    target = next(s for s in data['shifts'] if s['assignment_type_id'] == info['types']['am'])
    for shift in data['shifts']:
        shift.update(assignment_type_id=cover['id'], type_snapshot=cover)
    assert len(data['people']) == 150 and len(data['shifts']) == 532

    models = []
    started = time.process_time()
    result = generate(data, 60, deterministic=True, feasibility_only=True, _on_model=models.append)
    prepared = time.process_time() - started
    assert result['status'] == 'model_ready', result.get('status')
    model, = models
    size = {'variables': len(model.proto.variables), 'constraints': len(model.proto.constraints)}
    request.node.user_properties.append(('overtime_maximum_performance', json.dumps({
        'people': len(data['people']), 'sessions': len(data['shifts']), 'preparation_cpu_seconds': prepared, **size,
    })))
    assert prepared < _budget(60), prepared
    assert size['variables'] < 180_000 and size['constraints'] < 300_000, size

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.max_time_in_seconds = 10
    assert solver.solve(model) == cp_model.OPTIMAL
    index, = [i for i, variable in enumerate(model.proto.variables)
              if variable.name == f"{target['id']}:{person}"]
    model.add(model.get_bool_var_from_proto_index(index) == 1)
    assert solver.solve(model) == cp_model.INFEASIBLE


@pytest.fixture(scope='module')
def eighty_people_balance_goals(scale_harness):
    """Goals (equal duties, fair weekends, trainee x consultant minimum) stay a small fraction of the model."""
    h = scale_harness('h')
    data, info = department(h, 7, people=80, days=28, rules='full', leave_rate=0.05)
    consultants = [p for p in data['people'] if p not in info['trainees']][:10]
    people = lambda ids: {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': sorted(ids)}]}
    goals = [{'kind': 'balance_goal', 'metric': 'duties', 'period': 'month', 'aim': 'equal_share'},
             {'kind': 'balance_goal', 'metric': 'weekends', 'period': 'month', 'aim': 'equal_share'},
             {'kind': 'balance_goal', 'metric': 'pair_exposure', 'aim': 'min_per_pair', 'amount': 1, 'partners': people(consultants)}]
    with_goals = copy.deepcopy(data)
    for n, rule in enumerate(goals):
        with_goals['policies'].append({'id': f'00000000-0000-4000-8000-00000000000{n}', 'name': rule['metric'], 'kind': 'balance_goal',
            'status': 'active', 'active': True, 'revision': 1, 'department_id': data['workspace']['department_id'],
            'starts_on': '2020-01-01', 'ends_on': None, 'definition': {'severity': 'advisory', 'strength': 'high', 'exception_allowed': False,
            'scope': {'conditions': people(info['trainees'])} if rule['metric'] == 'pair_exposure' else {}, 'rule': rule}})
    sizes, results, preparations = [], [], []
    for snapshot in (data, with_goals):
        preparation = []
        started = time.process_time()

        def progress(update):
            if update['stage'] == 'finding' and not preparation:
                preparation.append(time.process_time() - started)

        result = generate(snapshot, 240, progress=progress)
        results.append(result)
        preparations.append(preparation)
        sizes.append({**result['model_size'], 'preparation_cpu_seconds': preparation[0], 'cpu_seconds': time.process_time() - started})
    print('balance goal overhead', sizes)
    return sizes, results, preparations


def test_balance_goals_add_little_to_a_month_for_eighty_people(eighty_people_balance_goals):
    sizes, results, preparations = eighty_people_balance_goals
    for result, preparation in zip(results, preparations):
        assert result['status'] == 'candidate_feasible', result.get('status')
        assert len(preparation) == 1, preparation
    assert sizes[1]['variables'] < 1.25 * sizes[0]['variables'] and sizes[1]['constraints'] < 1.25 * sizes[0]['constraints'], sizes


@pytest.mark.cpu_budget
def test_balance_goals_eighty_people_month_cpu_budget(eighty_people_balance_goals):
    sizes = eighty_people_balance_goals[0]
    assert sizes[1]['preparation_cpu_seconds'] < sizes[0]['preparation_cpu_seconds'] + 20, sizes


@pytest.fixture(scope='module')
def department_overtime_goals():
    """150 people and 1,500 sessions: compile the offered-share goals without a quarter-long solve."""
    from tests.test_overtime_offered import balance, goal_model, offer, snapshot

    data = snapshot(150, first='2026-10-01', last='2027-01-01')
    for n, pid in enumerate(data['people']):
        offer(data, pid, 'scheduler-scale-' + str(n), first='2026-10-01', last='2027-01-01', weekdays=[0, 1, 2, 3, 4])
        offer(data, pid, 'scheduler-overlap-' + str(n), first='2026-10-01', last='2027-01-01',
              start='10:00', end='14:00', weekdays=[0, 1, 2, 3, 4])
    for metric in ('overtime_duties', 'overtime_hours'):
        balance(data, metric, period='quarter', name=metric)
    started = time.process_time()
    model, choices, terms, worst = goal_model(data, 1500, candidates=3)
    elapsed = time.process_time() - started
    size = {'variables': len(model.proto.variables), 'constraints': len(model.proto.constraints)}
    measured = {'people': len(data['people']), 'sessions': len(data['shifts']), 'offers': len(data['scheduling_facts']),
                'candidate_places': len(choices), 'cpu_seconds': elapsed, **size}
    print('offered overtime quarter model', measured)
    return data, terms, worst, size, measured, elapsed


def test_overtime_offered_goals_stay_compact_for_a_department_quarter(department_overtime_goals, request):
    data, terms, worst, size, measured, _ = department_overtime_goals
    request.node.user_properties.append(('offered_goal_performance', json.dumps(measured)))
    assert len(data['people']) == 150 and len(data['shifts']) == 1500
    assert terms and worst > 0
    assert size['variables'] < 6500 and size['constraints'] < 5000, measured


@pytest.mark.cpu_budget
def test_overtime_offered_goals_department_quarter_cpu_budget(department_overtime_goals, request):
    from tests.test_performance_budget import python_budget
    _, _, _, _, measured, elapsed = department_overtime_goals
    request.node.user_properties.append(('offered_goal_performance', json.dumps(measured)))
    assert elapsed < python_budget(8), measured


@pytest.fixture(scope='module')
def department_request_overtime_index():
    """150 people x 500 sessions must not scan the year's 7,950 approved request lines per candidate."""
    from datetime import date, timedelta
    from packages.domain.conditions import active_rules
    from packages.domain.generation import allocation_settings, allocation_strengths
    from tests.test_overtime_offer_index import department_offers, probe, request_offer
    from tests.test_overtime_offered import ident

    data, line = department_offers(150)
    source = {'policy_id': ident('historical-source-policy'), 'line_id': line['id'], 'line_index': 0}
    year = date(2026, 1, 1)
    for person, pid in enumerate(data['people']):
        for week in range(53):
            first = year + timedelta(days=7 * week)
            request_offer(data, pid, f'year-request-{person}-{week}', str(first),
                          str(min(first + timedelta(days=7), date(2027, 1, 1))), source, weekdays=[0, 1, 2, 3, 4])
    data['shifts'] = [dict(probe(str(date(2026, 10, 1) + timedelta(days=n % 31))), id=f'session-{n}')
                      for n in range(500)]
    started = time.process_time()
    data['policies'] = active_rules(data['policies'], context=data)
    data['_allocation_strengths'] = allocation_strengths(data)
    data['_scope_interval_cache'], data['_policy_applies_cache'] = {}, {}
    # Exactly the same resolved input collection; repeated full scans would erase the indexing benefit.
    class ResolvedPolicies(list):
        def __iter__(self):
            assert not getattr(self, 'indexed', False), 'Allocation rescanned historical request policies'
            return super().__iter__()

    policies = data['policies'] = ResolvedPolicies(data['policies'])
    first_person = next(iter(data['people']))
    assert allocation_settings(data, data['shifts'][0], first_person)[1]
    policies.indexed = True
    prepared = time.process_time() - started
    started = time.process_time()
    for shift in data['shifts']:
        expected = date.fromisoformat(shift['start'][:10]).weekday() < 5
        for pid in data['people']:
            assert allocation_settings(data, shift, pid)[1] is expected
    lookup_cpu = time.process_time() - started
    measured = {'people': len(data['people']), 'sessions': len(data['shifts']),
                'year_request_lines': len(data['scheduling_facts']),
                'indexed_person_days': len(data['_allocation_index']['requests']),
                'preparation_cpu_seconds': prepared, 'lookup_cpu_seconds': lookup_cpu}
    print('request overtime year index', measured)
    return measured


def test_request_overtime_index_bounds_a_month_with_a_year_of_department_requests(department_request_overtime_index, request):
    measured = department_request_overtime_index
    request.node.user_properties.append(('request_overtime_performance', json.dumps(measured)))
    assert measured['year_request_lines'] == 7950
    assert measured['indexed_person_days'] == 150 * 22


@pytest.mark.cpu_budget
def test_request_overtime_index_department_year_cpu_budget(department_request_overtime_index, request):
    from tests.test_performance_budget import python_budget
    measured = department_request_overtime_index
    request.node.user_properties.append(('request_overtime_performance', json.dumps(measured)))
    assert measured['preparation_cpu_seconds'] < python_budget(8), measured
    assert measured['lookup_cpu_seconds'] < python_budget(8), measured


def test_work_patterns_and_wishes_stay_linear_in_person_days(h):
    """Four pattern rules and a wish per person-day add at most ~5 literals per person-day (no pairwise blow-up)."""
    from datetime import timedelta
    from tests.scheduler_departments import START
    data, info = department(h, 5, people=40, days=14, rules='full', leave_rate=0.05)
    models = []
    baseline = generate(data, 60, deterministic=True,
                        _on_objectives=lambda model, tiers: models.append(model))
    assert baseline['status'] == 'model_ready', baseline
    before = {'variables': len(models[0].proto.variables), 'constraints': len(models[0].proto.constraints)}
    del models[:]
    for rule in ({'kind': 'run_length', 'minimum': 2, 'maximum': 5}, {'kind': 'days_off_run', 'minimum': 2},
                 {'kind': 'weekend_shape', 'shape': 'complete'},
                 {'kind': 'shift_succession', 'mode': 'rest_after_series', 'from_type_ids': [info['types']['night']], 'series_length': 2, 'rest_days': 2}):
        data['policies'].append({**h.policy(rule, severity='advisory')})
    data['workspace'].update(starts_on=START.isoformat(), ends_on=(START + timedelta(days=14)).isoformat())
    data['wishes'] = [{'id': f'w{i}', 'person_id': pid, 'first_day': (START + timedelta(days=i % 14)).isoformat(),
                       'last_day': (START + timedelta(days=i % 14)).isoformat(), 'kind': ('prefer_off', 'prefer_on')[i % 2],
                       'assignment_type_id': None, 'points': 1, 'weight': 0.1} for i, pid in enumerate(data['people'])]
    result = generate(data, 60)
    assert result['status'] == 'candidate_feasible'
    after = result['model_size']
    person_days = len(data['people']) * 14
    print('pattern model growth', before, after)
    assert after['variables'] - before['variables'] < 5 * person_days, (before, after)
    assert after['constraints'] - before['constraints'] < 8 * person_days, (before, after)


@pytest.fixture(scope='module')
def personal_pattern_department_month(scale_harness):
    """All four personal patterns for 150 people over 532 sessions: shared day flags and bounded preparation."""
    from datetime import timedelta
    from tests.scheduler_departments import START

    h = scale_harness('h')
    data, _ = department(h, 17, people=150, days=28, rules='full', leave_rate=0.05, clinics=9)
    models = []
    baseline = generate(data, 10, deterministic=True,
                        _on_objectives=lambda model, tiers: models.append(model))
    assert baseline['status'] == 'model_ready', baseline
    before = {'variables': len(models[0].proto.variables), 'constraints': len(models[0].proto.constraints)}
    del models[:]
    kinds = [('max_days_in_row', 4), ('min_days_off_together', 2), ('max_weekends_in_row', 1), ('whole_weekends', None)]
    data['wishes'] = [{'id': f'pattern-{pid}-{kind}', 'person_id': pid, 'kind': kind, 'limit': limit,
                       'first_day': START.isoformat(), 'last_day': (START + timedelta(days=27)).isoformat(),
                       'assignment_type_id': None, 'points': 1, 'weight': 0.1}
                      for pid in data['people'] for kind, limit in kinds]
    preparation = []
    started = time.process_time()

    def progress(update):
        if update['stage'] == 'finding' and not preparation:
            preparation.append(time.process_time() - started)

    result = generate(data, 240, progress=progress)
    elapsed = time.process_time() - started
    after = result['model_size']
    print('personal pattern department month', {'cpu_seconds': elapsed, 'preparation_cpu_seconds': preparation,
          'people': len(data['people']), 'sessions': len(data['shifts']), 'wishes': len(data['wishes']),
          'before': before, 'after': after})
    started = time.process_time()
    validation = evaluate(apply(data, result))
    validation_elapsed = time.process_time() - started
    return data, before, result, preparation, elapsed, validation, validation_elapsed


def test_personal_patterns_stay_within_department_month_budgets(personal_pattern_department_month):
    data, before, result, preparation, _, validation, _ = personal_pattern_department_month
    assert len(data['people']) == 150 and len(data['shifts']) >= 500
    assert result['status'] == 'candidate_feasible', result.get('status')
    after = result['model_size']
    person_days = len(data['people']) * 28
    assert after['variables'] - before['variables'] < 12 * person_days, (before, after)
    assert after['constraints'] - before['constraints'] < 20 * person_days, (before, after)
    assert len(preparation) == 1, preparation
    assert validation['error_count'] == 0


@pytest.mark.cpu_budget
def test_personal_pattern_department_month_cpu_budget(personal_pattern_department_month):
    _, _, _, preparation, elapsed, _, validation_elapsed = personal_pattern_department_month
    assert preparation[0] < _budget(60), preparation
    assert elapsed < _budget(240), elapsed
    assert validation_elapsed < _budget(5)


@pytest.fixture(scope='module')
def greedy_department_fortnight(scale_harness):
    """Bounded greedy (packages/domain/generation.py), 40 people, two weeks, two parallel clinics (154 additions): about
    35 s before its run-wide caches (every trial re-read every rule, window and session from scratch, and the
    session-capacity check was quadratic), about 5 s with them, about 1.5 s since a trial re-checks only the sessions it
    touches and the person it adds (generation.Recheck). Include daily open extra cover and its solver summary
    within the same budget. Finishing inside the limit is what makes it deterministic."""
    from packages.domain.generation import generate as greedy
    h = scale_harness('h')
    data, info = department(h, 11, people=40, days=14, rules='full', leave_rate=0.05, clinics=2)
    extra_type = h.configure('assignment_types', {'name': 'Synthetic performance extra cover', 'starts_on': '2020-01-01',
        'definition': {'extra_cover': True, 'extra_cover_unfilled': 'open',
                       'requirements': [{'minimum': 1, 'maximum': 1,
                                         'capabilities': [{'capability_id': h.seed['capability'], 'minimum': 1}]}]}})['record']
    data['types'][extra_type['id']] = extra_type
    morning_templates = {s['start']: s for s in data['shifts'] if s['assignment_type_id'] == info['types']['am']}
    extra_ids = set()
    for template in morning_templates.values():
        shift = copy.deepcopy(template)
        shift.update(id=str(uuid.uuid5(uuid.NAMESPACE_URL, 'performance-extra-cover:' + template['id'])),
                     assignment_type_id=extra_type['id'], type_snapshot=copy.deepcopy(extra_type))
        data['shifts'].append(shift)
        data['focus_ids'].append(shift['id'])
        extra_ids.add(shift['id'])
    original = copy.deepcopy(data)
    started = time.process_time()
    first = greedy(data, 300)
    elapsed = time.process_time() - started
    print('greedy fortnight', round(elapsed, 1), 'CPU s', len(first['additions']), 'additions')
    return data, original, first, extra_ids, elapsed


def test_greedy_finishes_a_department_fortnight_well_within_its_time_limit(greedy_department_fortnight):
    data, original, first, extra_ids, _ = greedy_department_fortnight
    assert_no_mandatory_single_addition(original, first)
    assert first['status'] == 'candidate_feasible', first['status']
    assert first['validation']['error_count'] == 0
    assert first['additions'] and not any(a['shift_id'] in extra_ids for a in first['additions'])
    assert first['summary']['open_extra_cover_session_count'] == len(extra_ids) == 14
    assert first['summary']['open_extra_cover_places_count'] == len(extra_ids)
    assert first['summary']['open_places_count'] == 0
    # The caller's snapshot is untouched (the run's caches live on its own copy) and nothing leaks into the result.
    assert data == original
    assert not any(k.startswith('_') for k in first['validation'])


@pytest.mark.cpu_budget
def test_greedy_department_fortnight_cpu_budget(greedy_department_fortnight):
    elapsed = greedy_department_fortnight[4]
    assert elapsed < _budget(8), elapsed


@pytest.fixture(scope='module')
def department_each_workload_rules(scale_harness):
    """Four two-clinic Each lines compared with Together in the same 80-person, 196-session month/process."""
    from types import SimpleNamespace
    from packages.domain.generation import generate as greedy

    h = scale_harness('h')
    data, _ = department(h, 11, people=80, days=28, rules='full', leave_rate=0.05, clinics=3)
    clinics = list(data['services'])[:2]
    for rule in ({'kind': 'max_duties', 'maximum': 4, 'window': 'week'},
                 {'kind': 'max_hours', 'hours': 32, 'window': 'week'},
                 {'kind': 'max_duties', 'maximum': 1, 'window': 'day'},
                 {'kind': 'max_hours', 'hours': 8, 'window': 'day'}):
        scope = {'conditions': {'conditions': [{'field': 'service_ids', 'op': 'any', 'values': clinics, 'count': 'each'}]}}
        data['policies'].append(h.policy(rule, scope, severity='advisory'))

    # CPU budgets exclude other workers' contention; the production greedy timeout remains unchanged.
    together = copy.deepcopy(data)
    for policy in together['policies'][-4:]:
        policy['definition']['scope']['conditions']['conditions'][0]['count'] = 'together'
    timings, sizes, additions, outcomes = {}, {}, {}, []
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr('packages.domain.generation.time', SimpleNamespace(monotonic=lambda: 0.0))
        for count, snapshot in (('together', together), ('each', data)):
            original = copy.deepcopy(snapshot)
            timings[count] = {}
            # Exclude earlier fixtures from each measurement; GC stays enabled inside the operation.
            gc.collect()
            began, cpu = time.perf_counter(), time.process_time()
            result = greedy(snapshot, 300)
            timings[count]['greedy'] = {'wall_seconds': time.perf_counter() - began, 'cpu_seconds': time.process_time() - cpu}
            greedy_unchanged = snapshot == original
            additions[count] = len(result['additions'])

            draft = apply(snapshot, result)
            gc.collect()
            began, cpu = time.perf_counter(), time.process_time()
            validation = evaluate(draft)
            timings[count]['evaluate'] = {'wall_seconds': time.perf_counter() - began, 'cpu_seconds': time.process_time() - cpu}

            models = []
            gc.collect()
            began, cpu = time.perf_counter(), time.process_time()
            compiled = generate(snapshot, 10, deterministic=True, feasibility_only=True, _on_model=models.append)
            timings[count]['model'] = {'wall_seconds': time.perf_counter() - began, 'cpu_seconds': time.process_time() - cpu}
            sizes[count] = {'variables': len(models[0].proto.variables), 'constraints': len(models[0].proto.constraints)}
            outcomes.append((result, validation, compiled, greedy_unchanged, snapshot == original))
    ratios = {phase: timings['each'][phase]['cpu_seconds'] / timings['together'][phase]['cpu_seconds']
              for phase in ('greedy', 'evaluate', 'model')}
    print('Each workload department performance', {'people': len(data['people']), 'sessions': len(data['shifts']),
          'additions': additions, 'timings': timings, 'ratios': ratios, 'sizes': sizes})
    return timings, ratios, sizes, outcomes


def test_each_workload_rules_stay_within_department_validation_and_scheduler_budgets(department_each_workload_rules, request):
    timings, ratios, sizes, outcomes = department_each_workload_rules
    # Keep successful paired measurements in CI's existing JUnit artifact too.
    request.node.user_properties.append(('each_workload_performance', json.dumps({'timings': timings, 'ratios': ratios})))
    for result, validation, compiled, greedy_unchanged, model_unchanged in outcomes:
        assert result['status'] == 'candidate_feasible', result.get('status')
        assert result['validation']['error_count'] == 0
        assert greedy_unchanged
        assert validation['error_count'] == 0
        assert compiled['status'] == 'model_ready', compiled
        assert model_unchanged
    size = sizes['each']
    assert size['variables'] < 40_000 and size['constraints'] < 60_000, size


@pytest.mark.cpu_budget
def test_each_workload_department_validation_and_scheduler_cpu_budget(department_each_workload_rules, request):
    timings, ratios, _, _ = department_each_workload_rules
    request.node.user_properties.append(('each_workload_performance', json.dumps({'timings': timings, 'ratios': ratios})))
    # Same-process ratios calibrate hardware and coverage; 3x leaves >=1.5x measured headroom.
    assert all(ratio < 3 for ratio in ratios.values()), (ratios, timings)
    # Three times the old coverage-aware ceilings: catastrophic guards, not the regression budget.
    for phase, ceiling in (('greedy', 30), ('evaluate', 3), ('model', 36)):
        assert timings['each'][phase]['cpu_seconds'] < _budget(ceiling), timings


@pytest.fixture(scope='module')
def department_preparation_163_people(scale_harness):
    """Persist the preparation profile's active rules and retired history in PostgreSQL."""
    from datetime import date
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from packages.contracts.schemas import PolicyDefinition
    from tests.test_backend_scale import scale_department

    h = scale_harness('pg')
    workspace, data, _, _ = scale_department(h, assigned=False)
    people = list(data['people'])
    rules = [
        ({'kind': 'minimum_cover', 'minimum': 2}, {}, 'mandatory'),
        ({'kind': 'max_duties', 'maximum': 1, 'window': 'day'}, {}, 'mandatory'),
        ({'kind': 'max_duties', 'maximum': 5, 'window': 'week'}, {}, 'mandatory'),
        ({'kind': 'max_hours', 'hours': 32, 'window': 'week'}, {}, 'advisory'),
        ({'kind': 'balance_goal', 'metric': 'duties', 'period': 'month', 'aim': 'equal_share'}, {}, 'advisory'),
        ({'kind': 'balance_goal', 'metric': 'weekends', 'period': 'month', 'aim': 'equal_share'}, {}, 'advisory'),
        ({'kind': 'allocation_preference', 'priority': 5, 'session_priority': 2}, {'person_id': people[0]}, 'advisory'),
        ({'kind': 'allocation_preference', 'priority': 3, 'session_priority': 1}, {'person_id': people[1]}, 'advisory'),
    ]
    with transaction(h.app.state.engine, write=True) as db:
        for shift in db.scalars(select(m.Shift).where(m.Shift.workspace_id == workspace['id'])):
            snapshot = copy.deepcopy(shift.type_snapshot)
            snapshot['definition']['requirements'][0].update(minimum=2, target=3, maximum=3)
            shift.type_snapshot = snapshot
        for index, (rule, scope, severity) in enumerate(rules):
            definition = PolicyDefinition.model_validate({'severity': severity, 'scope': scope, 'rule': rule})
            db.add(m.Policy(department_id=h.seed['department_id'], name=f'Preparation rule {index}',
                kind=rule['kind'], definition=definition.model_dump(mode='json'), status='active', active=True,
                starts_on=date(2020, 1, 1), created_by=h.seed['identities']['coordinator'],
                approved_by=h.seed['identities']['approver'], owner='Synthetic policy owner', source='Preparation fixture'))
        db.flush()
        active = list(db.scalars(select(m.Policy).where(m.Policy.department_id == h.seed['department_id'],
                                                       m.Policy.status == 'active')))
        assert len(active) == 12
        for index, source in enumerate(active):
            for revision in range(83 if index < 5 else 82):
                db.add(m.Policy(department_id=h.seed['department_id'], name=f'Retired {source.kind} {revision}',
                    kind=source.kind, definition=copy.deepcopy(source.definition), status='retired', active=False,
                    revision=revision + 1, starts_on=date(2018, 1, 1), ends_on=date(2019, 1, 1),
                    created_by=h.seed['identities']['coordinator'], approved_by=h.seed['identities']['approver'],
                    owner='Synthetic policy owner', source='Preparation fixture'))
    workspace = h.refresh_draft_baseline(h.detail(workspace)['workspace'])
    data = h.snapshot(workspace)
    assert h.app.state.engine.dialect.name == 'postgresql'
    assert len(data['people']) == 163 and len(data['shifts']) == 768
    assert len(data['policies']) == 1001 and sum(p['status'] == 'active' for p in data['policies']) == 12
    assert not any(s['participants'] for s in data['shifts'])
    return data


@pytest.mark.cpu_budget
@pytest.mark.postgres
def test_department_preparation_163_people_768_sessions_cpu_budget(department_preparation_163_people, request):
    from packages.contracts.schemas import GenerationOptions
    from packages.domain.cp_sat import _prepare
    from packages.domain.solver_control import SolverControl

    data = department_preparation_163_people
    options = GenerationOptions.model_validate(data.get('generation_options') or {})
    control = SolverControl()
    control.configure_stop_conditions(options, data['organisation']['timezone'])
    gc.collect()
    started = time.process_time()
    prepared = _prepare(data, options.search_minutes * 60, 1, control, options, False, False)
    elapsed = time.process_time() - started
    request.node.user_properties.append(('preparation_cpu_seconds', elapsed))
    assert 'model' in prepared and 'score_program' in prepared, prepared.get('status')
    assert control.search_spent == 0 and control._solve_started is None
    control.search_budget = options.search_minutes * 60
    assert control.remaining() == options.search_minutes * 60
    assert elapsed < _budget(60), elapsed

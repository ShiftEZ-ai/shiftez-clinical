"""D54 population parity and API request-thread regressions."""
import asyncio
from copy import deepcopy

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import jobs
from apps.worker.main import process_one
from packages.domain import cp_sat, generation
from packages.domain.generation_scope import planning_population_people, union_population
from packages.domain.input_manifest import manifest
from packages.domain.validation import evaluate
from packages.domain.rota_summary import rota_summary
from tests.test_block_quotas import quota
from tests.test_generation_population import scope, create, apply, result


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_validation_job_create_defers_snapshot_and_rendering(h, monkeypatch):
    w = h.shift(h.workspace(), people=[])['workspace']
    import apps.api.main as main
    original = main.JSONResponse
    renders = []
    class CheckedResponse(original):
        def render(self, content):
            if isinstance(content, dict) and content.get('job', {}).get('kind') == 'validation':
                with pytest.raises(RuntimeError, match='no running event loop'):
                    asyncio.get_running_loop()
                renders.append(True)
            return super().render(content)
    monkeypatch.setattr(main, 'JSONResponse', CheckedResponse)
    with monkeypatch.context() as patch:
        patch.setattr(jobs, 'domain_snapshot', lambda *a, **kw: pytest.fail('Snapshot belongs in the worker'))
        first = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'},
                          revision=w['revision'])['job']
        again = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'},
                          revision=w['revision'])['job']
        assert first['id'] == again['id']
        invalid = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation',
            'session_ids': [h.detail(w)['shifts'][0]['id']]}, revision=w['revision'], status=422)
        assert invalid['error']['code'] == 'INVALID_SCOPE'
    assert len(renders) == 2
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Job, first['id']).input_snapshot
        assert stored['validation_basis'] == 'current' and 'people' not in stored and 'shifts' not in stored
    assert process_one(h.app.state.engine)
    fresh = h.command('coordinator', 'workspace.validate', {'reason': 'Compare complete validation'}, target=w['id'], revision=w['revision'])['validation']
    completed = result(h, first)
    assert completed['valid'] == fresh['valid']
    assert completed['error_count'] == fresh['error_count']


@pytest.mark.parametrize('representation', ['scope', 'map'])
def test_quota_minimum_target_population_keeps_maximum_and_safety(h, representation):
    inside, outside = h.seed['people'][:2]
    quota(h, scope={}, comparison='minimum', amount=1)
    quota(h, scope={}, comparison='target', amount=1, severity='advisory')
    quota(h, scope={'person_id': outside}, comparison='maximum', amount=0)
    h.policy({'kind': 'restriction'}, {'person_id': outside})
    w = h.shift(h.workspace(), people=[outside], seed_restricted=True)['workspace']
    data = h.snapshot(w)
    unscoped = evaluate(data)
    scoped = deepcopy(data)
    if representation == 'scope':
        scoped['generation_scope'] = scope('person_ids', inside)
    else:
        scoped['planning_population'] = {s['id']: [inside] for s in data['shifts']}
    check = evaluate(scoped)
    assert any(f['code'] == 'QUOTA_BELOW_MINIMUM' and f['person_id'] == inside for f in check['findings'])
    assert all(r['person_id'] == inside for r in check['quota_report']['rows'] if r['comparison'] in {'minimum', 'target'})
    assert any(r['person_id'] == outside for r in check['quota_report']['rows'] if r['comparison'] == 'maximum')
    safety = lambda c: [f for f in c['findings'] if f['code'] not in {'QUOTA_BELOW_MINIMUM', 'QUOTA_BELOW_TARGET', 'BALANCE_GOAL'}]
    assert safety(check) == safety(unscoped)
    assert str(check['planning_population']['outside_count']) in rota_summary(check)['population_note']
    assert 'planning_population' not in unscoped


@pytest.mark.parametrize('solver', [generation.generate, cp_sat.generate])
def test_solver_and_validator_enforce_only_population_minima(h, solver):
    inside = h.seed['people'][1]
    quota(h, scope={}, comparison='minimum', amount=1)
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    data['generation_scope'] = scope('person_ids', inside)
    generated = solver(data, 10)
    assert generated['status'] == 'candidate_feasible', generated
    assert {a['participant']['person_id'] for a in generated['additions']} == {inside}
    assert not any(f['code'] == 'QUOTA_BELOW_MINIMUM' for f in generated['validation']['findings'])
    assert {r['person_id'] for r in generated['validation']['quota_report']['rows']} == {inside}


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_applied_union_query_undo_redo_and_unrestricted_absorption(h, monkeypatch):
    first, second = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Population passes', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 3}]}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=[])['workspace']
    original_changed = jobs.changed
    def receipt_before_changed(db, actor, workspace, *args, **kwargs):
        from sqlalchemy import select
        applied = list(db.scalars(select(m.Job).where(m.Job.workspace_id == workspace.id,
            m.Job.kind == 'generation', m.Job.status == 'applied')))
        assert applied and all(j.result['reconciliation'].get('population_recorded') for j in applied)
        return original_changed(db, actor, workspace, *args, **kwargs)
    monkeypatch.setattr(jobs, 'changed', receipt_before_changed)
    first_job = create(h, w, scope('person_ids', first))
    assert process_one(h.app.state.engine)
    w = apply(h, w, first_job)['workspace']
    assert planning_population_people(h.snapshot(w)) == {first}
    # A second empty session lets the next population contribute its own pass.
    w = h.shift(w, typ=typ['id'], people=[], start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    second_job = create(h, w, scope('person_ids', second))
    while h.get('coordinator', '/api/jobs/' + second_job['id'])['job']['status'] == 'queued':
        assert process_one(h.app.state.engine)
    w = apply(h, w, second_job)['workspace']
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if 'FROM jobs' in statement:
            statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        data = h.snapshot(w)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    assert planning_population_people(data) == {first, second}
    assert len(statements) == 1 and ('JSON_EXTRACT' in statements[0] or ' -> ' in statements[0] or ' #> ' in statements[0])
    assert h.get('coordinator', f'/api/workspaces/{w["id"]}/summary')['population_note'].startswith('Checked for: 2 people')
    w = h.command('coordinator', 'workspace.undo', {'reason': 'Undo second population'}, w['id'], w['revision'])['workspace']
    assert planning_population_people(h.snapshot(w)) == {first}
    w = h.command('coordinator', 'workspace.redo', {'reason': 'Restore second population'}, w['id'], w['revision'])['workspace']
    assert planning_population_people(h.snapshot(w)) == {first, second}
    # The receipt keeps this audience even if its original sessions no longer exist.
    with transaction(h.app.state.engine) as db:
        receipt = db.get(m.Job, second_job['id']).result['reconciliation']
    assert union_population({**data, 'shifts': [], 'focus_ids': []}, [receipt]) == sorted({first, second})
    w = h.shift(w, typ=typ['id'], people=[], start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00')['workspace']
    unrestricted_job = create(h, w)
    while h.get('coordinator', '/api/jobs/' + unrestricted_job['id'])['job']['status'] == 'queued':
        assert process_one(h.app.state.engine)
    w = apply(h, w, unrestricted_job)['workspace']
    assert planning_population_people(h.snapshot(w)) is None
    w = h.command('coordinator', 'workspace.undo', {'reason': 'Undo unrestricted pass'}, w['id'], w['revision'])['workspace']
    assert planning_population_people(h.snapshot(w)) == {first, second}
    assert union_population(data, [{}]) is None
    unrestricted = {**data, 'applied_planning_population': None}
    assert union_population(unrestricted, [{'generation_scope': scope('person_ids', first)}]) is None
    assert manifest(data)['token'] != manifest(unrestricted)['token']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_department_scale_validation_queue_and_inline_check(h, monkeypatch, record_property):
    from tests.test_backend_scale import scale_department, measure, post
    from apps.worker.main import process_one
    w, data, _, _ = scale_department(h)
    quota(h, scope={}, comparison='minimum', amount=1)
    quota(h, scope={}, comparison='target', amount=8, severity='advisory')
    assert len(data['people']) >= 150 and len(data['focus_ids']) >= 535
    _, snapshot_metrics = measure(h, 'stability_snapshot', lambda: h.snapshot(w))
    with monkeypatch.context() as patch:
        patch.setattr(jobs, 'domain_snapshot', lambda *a, **kw: pytest.fail('Heavy snapshot in queue request'))
        queued, queue_metrics = measure(h, 'stability_queue', lambda: post(h, 'job.create',
            {'workspace_id': w['id'], 'kind': 'validation'}, revision=w['revision']))
    assert h.get('coordinator', '/api/jobs/' + queued['job']['id'])['job']['status'] == 'queued'
    processed, worker_metrics = measure(h, 'stability_worker', lambda: process_one(h.app.state.engine))
    assert processed
    checked, validation_metrics = measure(h, 'stability_inline', lambda: post(h, 'workspace.validate', {'reason': 'Department scale check'},
        target=w['id'], revision=w['revision']))
    assert checked['validation']['error_count'] >= 0
    assert h.get('coordinator', '/api/jobs/' + queued['job']['id'])['job']['status'] == 'succeeded'
    for name, metric in [('snapshot', snapshot_metrics), ('queue', queue_metrics), ('worker', worker_metrics), ('inline', validation_metrics)]:
        record_property(name, metric)
    return locals()


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.wall_benchmark
def test_department_scale_validation_wall_budgets(h, monkeypatch, record_property):
    state = test_department_scale_validation_queue_and_inline_check(h, monkeypatch, record_property)
    assert state['worker_metrics']['wall_seconds'] < 15, state['worker_metrics']
    assert state['validation_metrics']['wall_seconds'] < 15, state['validation_metrics']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_department_scale_population_apply_preserves_authorised_cover_gap_budget(h):
    """A December-shaped population pass may move an existing scoped gap, but cannot add to its deficit budget."""
    from tests.test_backend_scale import scale_department

    w, data, _, _ = scale_department(h)
    assert len(data['people']) >= 150 and len(data['focus_ids']) >= 500
    gap_type = h.configure('assignment_types', {'name': 'Scale weekend cover (test)', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 4}]}})['record']
    assigned_people = {p['person_id'] for shift in data['shifts'] for p in shift['participants']}
    unavailable = {row['person_id'] for row in data.get('rotations', []) if not row.get('department_available', True)}
    people = [pid for pid in data['people'] if pid not in assigned_people | unavailable][:4]
    assert len(people) == 4
    for day, assigned in zip(range(25, 31), [people[:3], people[:3], people[:3], people[:3], people[:3], people]):
        w = h.shift(w, typ=gap_type['id'], people=assigned,
            start=f'2026-10-{day:02d}T08:00:00+03:00', end=f'2026-10-{day:02d}T12:00:00+03:00')['workspace']
    gap_rule = h.policy({'kind': 'minimum_cover', 'minimum': 4}, {'assignment_type_id': gap_type['id']})
    detail = h.detail(w)
    synthetic = sorted([row for row in detail['shifts'] if row['assignment_type_id'] == gap_type['id']],
                       key=lambda row: row['start'])
    added, full = synthetic[:-1], synthetic[-1]
    # Five sessions start one person short; the sixth is full. The proposal fills
    # the first and removes one from the full session, preserving five gaps.
    population = scope('person_ids', *people, protection='rebalance_selected')
    baseline = h.snapshot(w)
    baseline['generation_scope'] = population
    before_errors = [f for f in evaluate(baseline)['findings']
                     if f.get('severity') == 'error' and f.get('code') == 'SCOPED_COVERAGE_GAP'
                     and f.get('rule_id') == gap_rule['id']]
    assert len(before_errors) == 5
    selected = people
    job = create(h, w, population, generation_options={'mode': 'rebalance'})
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        target = added[0]
        removed = next(p for p in full['participants'] if p['person_id'] == selected[-1])
        row.status = 'succeeded'
        row.result = {'status': 'candidate_with_gaps',
            'generation_scope': row.input_snapshot['generation_scope'],
            'generation_scope_source': row.input_snapshot['generation_scope_source'],
            'additions': [{'shift_id': target['id'], 'participant': {'person_id': selected[-1],
                'start': target['start'], 'end': target['end']}}],
            'removals': [{'shift_id': full['id'], 'assignment_id': removed['id'], 'person_id': removed['person_id']}]}
    applied = apply(h, w, job)
    assert applied['reconciliation']['status'] == 'applied', applied['reconciliation']
    assert applied['reconciliation']['addition_indexes'] == [0]
    assert applied['reconciliation']['removal_indexes'] == [0]
    after = h.detail(w)
    target_after = next(row for row in after['shifts'] if row['id'] == target['id'])
    full_after = next(row for row in after['shifts'] if row['id'] == full['id'])
    assert selected[-1] in {p['person_id'] for p in target_after['participants']}
    assert selected[-1] not in {p['person_id'] for p in full_after['participants']}
    checked = h.snapshot(w)
    checked['generation_scope'] = population
    after_errors = [f for f in evaluate(checked)['findings']
                    if f.get('severity') == 'error' and f.get('code') == 'SCOPED_COVERAGE_GAP'
                    and f.get('rule_id') == gap_rule['id']]
    assert len(after_errors) == 5

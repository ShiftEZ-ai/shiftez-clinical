"""Failed read warming is bounded maintenance, never a perpetual department evaluation."""
from datetime import timedelta
from copy import deepcopy
import json
import time

import pytest
from sqlalchemy import delete, event, select, update

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.api.logging_setup import JsonFormatter
from apps.api.services import read_projection_warming as warming, rota_summary, workspace_reads
from apps.api.settings import Settings
from tests.test_fix_list_performance import backend
from tests.clock_support import freeze_clock


def test_warming_checks_source_only_for_stale_work(backend, monkeypatch):
    engine = backend.app.state.engine
    calls = []
    source_identity = warming.source_identity

    def observed(db):
        calls.append(True)
        return source_identity(db)

    monkeypatch.setattr(warming, 'source_identity', observed)
    assert not warming.warm_one(engine)
    assert calls == []
    backend.workspace()
    assert warming.warm_one(engine)
    assert not warming.warm_one(engine)
    assert calls == [True]


@pytest.mark.parametrize('stage', ['stored_check', 'fingerprint', 'summary_check'])
@pytest.mark.parametrize('in_generator', [False, True])
def test_failed_projection_backs_off_logs_once_and_recovers_after_input_change(backend, monkeypatch, caplog, stage, in_generator):
    h = backend
    workspace = h.workspace()
    engine = h.app.state.engine
    with transaction(engine) as db:
        stored_validation = deepcopy(db.get(m.Workspace, workspace['id']).validation)
    module, name = {'stored_check': (workspace_reads, 'stored_checks'),
                    'fingerprint': (snapshots, 'fingerprints'),
                    'summary_check': (rota_summary, 'summary_check')}[stage]
    original, attempts = getattr(module, name), []
    clock = [m.now()]
    freeze_clock(monkeypatch, lambda: clock[0])

    def failed(*args, **kwargs):
        attempts.append(True)
        if in_generator:
            def rows():
                next(iter(()))
                yield None
            return next(rows())
        return next(iter(()))

    monkeypatch.setattr(module, name, failed)
    assert not warming.warm_one(engine)
    assert attempts == [True]
    failures = lambda: [r for r in caplog.records if r.message == 'read_projection_warm_failed']
    [warning] = failures()
    assert warning.workspace_id == workspace['id'] and warning.projection_kind == stage
    assert warning.error_code == ('RuntimeError' if in_generator else 'StopIteration')
    assert warning.exc_info and 'StopIteration' in json.loads(JsonFormatter().format(warning))['stack']
    writes = []
    def capture(_c, _cu, statement, _p, _ctx, _many):
        if statement.lstrip().split()[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'SAVEPOINT'}:
            writes.append(statement)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        began = time.process_time()
        for _ in range(25):
            assert not warming.warm_one(engine)
        cpu = time.process_time() - began
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert attempts == [True] and not writes and len(failures()) == 1
    print(f'warming_backoff backend={engine.dialect.name} stage={stage} idle_25_cpu={cpu:.4f}s')
    with transaction(engine) as db:
        row = db.scalar(select(m.WorkspaceReadProjection).where(
            m.WorkspaceReadProjection.workspace_id == workspace['id'], m.WorkspaceReadProjection.kind == 'warm'))
        failure = row.value['failure']
        assert failure['retry_seconds'] == Settings().worker_projection_retry_seconds
        assert db.get(m.Workspace, workspace['id']).validation == stored_validation
    with pytest.raises(RuntimeError, match='warming failed'):
        warming.warm_all(engine)
    assert attempts == [True]  # Deployment cannot claim success, or bypass an unchanged failure's cooldown.
    clock[0] += timedelta(seconds=failure['retry_seconds'] + 1)
    assert not warming.warm_one(engine)
    assert len(attempts) == 2 and len(failures()) == 1
    with transaction(engine) as db:
        row = db.scalar(select(m.WorkspaceReadProjection).where(
            m.WorkspaceReadProjection.workspace_id == workspace['id'], m.WorkspaceReadProjection.kind == 'warm'))
        assert row.value['failure']['retry_seconds'] == 2 * failure['retry_seconds']
    monkeypatch.setattr(module, name, original)
    with transaction(engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    assert warming.warm_one(engine)
    assert not warming.warm_one(engine)
    with transaction(engine) as db:
        row = db.scalar(select(m.WorkspaceReadProjection).where(
            m.WorkspaceReadProjection.workspace_id == workspace['id'], m.WorkspaceReadProjection.kind == 'warm'))
        assert 'failure' not in row.value
    return locals()


@pytest.mark.parametrize('stage', ['stored_check', 'fingerprint', 'summary_check'])
@pytest.mark.parametrize('in_generator', [False, True])
@pytest.mark.cpu_budget
def test_failed_projection_backoff_cpu_budget(backend, monkeypatch, caplog, stage, in_generator):
    state = test_failed_projection_backs_off_logs_once_and_recovers_after_input_change(
        backend, monkeypatch, caplog, stage, in_generator)
    assert state['cpu'] < 3, state['cpu']


def test_sqlite_warmer_skips_a_stale_write_snapshot_without_marking_it_warm(h, monkeypatch, caplog):
    workspace = h.shift(h.workspace())['workspace']
    engine = h.app.state.engine
    projection = m.WorkspaceReadProjection
    key = (projection.workspace_id == workspace['id'], projection.kind.in_(('fingerprint', 'warm')))
    with transaction(engine, write=True) as db:
        db.add(m.ReadProjectionWarmCursor(key='workspaces'))
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
        db.execute(delete(projection).where(*key))

    fingerprints = snapshots.fingerprints

    def race(*args, **kwargs):
        # Another connection commits after the warmer establishes its read snapshot.
        with engine.begin() as writer:
            writer.execute(update(m.ReadProjectionWarmCursor).where(m.ReadProjectionWarmCursor.key == 'workspaces')
                           .values(after_id=workspace['id']))
        return fingerprints(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(snapshots, 'fingerprints', race)
        assert not warming.warm_one(engine)

    with transaction(engine) as db:
        assert not db.scalars(select(projection.kind).where(*key)).all()
    assert any(record.message == 'read_projection_store_skipped' for record in caplog.records)
    assert warming.warm_one(engine)


def test_empty_and_missing_warming_inputs_do_not_spin(backend, monkeypatch):
    h = backend
    engine = h.app.state.engine
    assert not warming.warm_one(engine)  # No workspace or publication.
    for n in range(3):
        workspace = h.workspace('Empty rota ' + str(n))
        with transaction(engine, write=True) as db:
            db.get(m.Workspace, workspace['id']).validation = None
    assert warming.warm_all(engine) == 3  # No shifts, participants, validation or published versions.
    def forbidden(*args, **kwargs):
        pytest.fail('An empty unchanged workspace was evaluated again')
    monkeypatch.setattr(rota_summary, 'summary_check', forbidden)
    monkeypatch.setattr(snapshots, 'fingerprints', forbidden)
    began = time.process_time()
    assert warming.warm_all(engine) == 0
    for _ in range(25):
        assert not warming.warm_one(engine)
    cpu = time.process_time() - began
    print(f'empty_warming backend={engine.dialect.name} idle_25_cpu={cpu:.4f}s')
    return locals()


@pytest.mark.cpu_budget
def test_empty_and_missing_warming_inputs_cpu_budget(backend, monkeypatch):
    state = test_empty_and_missing_warming_inputs_do_not_spin(backend, monkeypatch)
    assert state['cpu'] < 3, state['cpu']


@pytest.mark.postgres
def test_department_scale_warming_finishes_once_then_uses_no_evaluator(pg, monkeypatch):
    from tests.test_backend_scale import scale_department
    workspace, data, _, _ = scale_department(pg)
    assert len(data['people']) == 163 and len(data['focus_ids']) == 768
    with transaction(pg.app.state.engine, write=True) as db:
        db.get(m.Organisation, pg.seed['organisation_id']).input_revision += 1
    assert warming.warm_all(pg.app.state.engine) == 1
    def forbidden(*args, **kwargs):
        pytest.fail('Unchanged department warming repeated scheduling work')
    monkeypatch.setattr(rota_summary, 'summary_check', forbidden)
    monkeypatch.setattr(snapshots, 'fingerprints', forbidden)
    began = time.process_time()
    for _ in range(25):
        assert not warming.warm_one(pg.app.state.engine)
    cpu = time.process_time() - began
    print(f'department_warming people=163 sessions=768 idle_25_cpu={cpu:.4f}s')
    return locals()


@pytest.mark.postgres
@pytest.mark.cpu_budget
def test_department_scale_warming_cpu_budget(pg, monkeypatch):
    state = test_department_scale_warming_finishes_once_then_uses_no_evaluator(pg, monkeypatch)
    assert state['cpu'] < 3, state['cpu']


def test_failed_workspace_does_not_starve_other_workspaces(backend, monkeypatch, caplog):
    h = backend
    rows = sorted([h.workspace('First'), h.workspace('Second')], key=lambda w: w['id'])
    original = rota_summary.summary_check
    seen = []
    def summary(db, workspace):
        seen.append(workspace.id)
        if workspace.id == rows[0]['id']:
            raise StopIteration()
        return original(db, workspace)
    monkeypatch.setattr(rota_summary, 'summary_check', summary)
    assert not warming.warm_one(h.app.state.engine)
    assert warming.warm_one(h.app.state.engine)
    assert not warming.warm_one(h.app.state.engine)
    assert seen == [w['id'] for w in rows]
    [warning] = [r for r in caplog.records if r.message == 'read_projection_warm_failed']
    assert warning.workspace_id == rows[0]['id']


def test_missing_source_identity_waits_without_evaluation_and_recovers(backend, monkeypatch, caplog):
    h = backend
    clock = [m.now()]
    freeze_clock(monkeypatch, lambda: clock[0])
    original = warming.source_identity
    monkeypatch.setattr(warming, 'source_identity', lambda db: None)
    assert not warming.warm_one(h.app.state.engine)
    workspace = h.workspace()
    assert not warming.warm_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        row = db.scalar(select(m.WorkspaceReadProjection).where(
            m.WorkspaceReadProjection.workspace_id == workspace['id'], m.WorkspaceReadProjection.kind == 'warm'))
        before = deepcopy(row.value)
        retry_seconds = before['failure']['retry_seconds']
        assert before['source'] is None and before['failure']['retry_at'] > clock[0].timestamp()
    writes = []
    def capture(_c, _cu, statement, _p, _ctx, _many):
        if statement.lstrip().split()[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'SAVEPOINT'}:
            writes.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        clock[0] += timedelta(seconds=max(1, retry_seconds - 1))
        assert not warming.warm_one(h.app.state.engine)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    assert not writes
    with transaction(h.app.state.engine) as db:
        row = db.scalar(select(m.WorkspaceReadProjection).where(
            m.WorkspaceReadProjection.workspace_id == workspace['id'], m.WorkspaceReadProjection.kind == 'warm'))
        assert row.value == before
    [warning] = [r for r in caplog.records if r.message == 'read_projection_warm_failed']
    assert warning.workspace_id == workspace['id'] and warning.projection_kind == 'source_identity'
    clock[0] += timedelta(seconds=2)
    monkeypatch.setattr(warming, 'source_identity', original)
    assert warming.warm_one(h.app.state.engine)
    assert not warming.warm_one(h.app.state.engine)


def test_projection_retry_settings_are_configurable_and_bounded(monkeypatch):
    from dataclasses import replace
    monkeypatch.setenv('ROTA_ENV', 'test')
    monkeypatch.setenv('ROTA_WORKER_PROJECTION_RETRY_SECONDS', '60')
    monkeypatch.setenv('ROTA_WORKER_PROJECTION_RETRY_MAX_SECONDS', '120')
    settings = Settings.from_env()
    settings.check(require_authentication=False)
    assert (settings.worker_projection_retry_seconds, settings.worker_projection_retry_max_seconds) == (60, 120)
    for values in ({'worker_projection_retry_seconds': 0}, {'worker_projection_retry_max_seconds': 30},
                   {'worker_projection_retry_max_seconds': 86401}):
        with pytest.raises(RuntimeError, match='Projection retry'):
            replace(settings, **values).check(require_authentication=False)


def test_retry_delay_caps_and_distinct_failures_keep_their_traceback(h, monkeypatch, caplog):
    from dataclasses import replace
    h.workspace()
    settings = replace(h.settings, worker_projection_retry_seconds=30, worker_projection_retry_max_seconds=60)
    clock = [m.now()]
    freeze_clock(monkeypatch, lambda: clock[0])
    error = [StopIteration]
    def failed(*args, **kwargs):
        raise error[0]()
    monkeypatch.setattr(rota_summary, 'summary_check', failed)
    for expected in (30, 60, 60):
        assert not warming.warm_one(h.app.state.engine, settings=settings)
        with transaction(h.app.state.engine) as db:
            row = db.scalar(select(m.WorkspaceReadProjection).where(m.WorkspaceReadProjection.kind == 'warm'))
            assert row.value['failure']['retry_seconds'] == expected
        clock[0] += timedelta(seconds=expected + 1)
    assert len([r for r in caplog.records if r.message == 'read_projection_warm_failed']) == 1
    error[0] = ValueError
    assert not warming.warm_one(h.app.state.engine, settings=settings)
    warnings = [r for r in caplog.records if r.message == 'read_projection_warm_failed']
    assert [r.error_code for r in warnings] == ['StopIteration', 'ValueError']
    assert all(r.exc_info for r in warnings)

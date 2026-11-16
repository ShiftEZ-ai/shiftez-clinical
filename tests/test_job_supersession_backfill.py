"""Runs made before jobs.supersedes gain their supersession records from the worker's bounded, idempotent fill, by
the same rule Apply uses; nothing else on a run changes and a second sweep writes nothing."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, insert, select, update

from apps.api import models as m
from apps.api.db import transaction
from apps.api.settings import Settings
from apps.api.services.jobs import backfill_supersessions, superseded_runs
from apps.worker.main import fill_supersessions, _supersessions_filled


def old_runs(h, name, specs):
    """Generation runs as an older build stored them: no supersession record."""
    w = h.shift(h.workspace(name), people=[])['workspace']
    w = h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[])['workspace']
    first, second = h.snapshot(w)['focus_ids'][:2]
    sessions = {'both': [first, second], 'first': [first], 'second': [second]}
    began = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = [{'id': f'{name}-{index}', 'department_id': h.seed['department_id'], 'workspace_id': w['id'],
             'requested_by': h.seed['identities']['coordinator'], 'kind': 'generation', 'status': status,
             'input_fingerprint': 'old', 'created_at': began + timedelta(minutes=index),
             'input_snapshot': {'generation_engine': 'legacy', 'focus_ids': [first, second],
                                'generation_session_ids': sessions[covers], **({'generation_scope': scope} if scope else {})},
             'result': {'status': 'candidate_feasible', 'additions': []}}
            for index, (status, covers, scope) in enumerate(specs)]
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Job), rows)
    return w['id'], [row['id'] for row in rows]


def stored(h, ids):
    with transaction(h.app.state.engine) as db:
        return {job.id: (job.supersedes, job.result, job.input_snapshot, job.read_metadata, job.status)
                for job in db.scalars(select(m.Job).where(m.Job.id.in_(ids)))}


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_old_runs_gain_their_supersessions_once_by_the_shared_rule(request, backend):
    h = request.getfixturevalue(backend)
    group = {'conditions': {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any',
                                                            'values': [h.seed['people'][0]]}]}, 'protection': 'keep_existing'}
    workspace, ids = old_runs(h, 'old', [('succeeded', 'both', None), ('succeeded', 'first', None), ('failed', 'both', None),
                              ('succeeded', 'second', group), ('dismissed', 'both', None), ('succeeded', 'second', None)])
    before = stored(h, ids)
    assert all(value[0] is None for value in before.values())
    with transaction(h.app.state.engine, write=True) as db:
        assert backfill_supersessions(db) == len(ids)
    after = stored(h, ids)
    # Each run lists the older live runs it supersedes: shared sessions, same population (or unscoped), never a
    # failed or discarded run. Every other field is untouched.
    assert {ident: value[0] for ident, value in after.items()} == {
        ids[0]: [], ids[1]: [ids[0]], ids[2]: [ids[0], ids[1]], ids[3]: [ids[0]], ids[4]: sorted(ids[:2] + ids[3:4]),
        ids[5]: [ids[0], ids[3]]}
    assert {ident: value[1:] for ident, value in after.items()} == {ident: value[1:] for ident, value in before.items()}
    # Reads now agree with Apply's exact helper: a run listed only by failed or discarded runs is not superseded.
    listed = {job['id']: job['currency'] for job in h.get('coordinator', '/api/jobs?workspace_id=' + workspace)['jobs']}
    with transaction(h.app.state.engine) as db:
        exact = superseded_runs(db, list(db.scalars(select(m.Job).where(m.Job.id.in_(ids)))))
    assert {ident for ident, value in listed.items() if value == 'superseded'} == exact == {ids[0], ids[3]}
    # A second sweep writes nothing.
    with transaction(h.app.state.engine, write=True) as db:
        assert backfill_supersessions(db) == 0
    assert stored(h, ids) == after


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_the_fill_is_bounded_per_tick_and_the_worker_finishes_it(request, backend):
    h = request.getfixturevalue(backend)
    _, first = old_runs(h, 'tick-a', [('succeeded', 'both', None), ('succeeded', 'both', None)])
    _, second = old_runs(h, 'tick-b', [('succeeded', 'both', None), ('succeeded', 'both', None)])
    with transaction(h.app.state.engine, write=True) as db:
        assert backfill_supersessions(db, workspaces=1) == 2  # one rota per call
    recorded = {ident for ident, value in stored(h, first + second).items() if value[0] is not None}
    assert recorded in ({*first}, {*second})
    # A run recorded by the current build (an empty record) is never rewritten by the fill.
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(update(m.Job).where(m.Job.id == sorted(recorded)[0]).values(supersedes=[]))
    _supersessions_filled.pop(h.app.state.engine, None)
    assert fill_supersessions(h.app.state.engine) == 2
    assert fill_supersessions(h.app.state.engine) == 0  # throttled, then done
    final = stored(h, first + second)
    assert all(value[0] is not None for value in final.values())
    assert final[sorted(recorded)[0]][0] == []
    assert final[first[1]][0] == [first[0]] and final[second[1]][0] == [second[0]]


def test_a_failing_fill_is_retried_only_after_the_interval_and_logged_once_per_attempt(h, monkeypatch, caplog):
    import apps.api.services.jobs as jobs
    import apps.worker.main as worker
    calls, clock, failures = [], [1000.0], [1]

    original = jobs.prepare_supersession_backfill
    def flaky(*args, **kwargs):
        calls.append(clock[0])
        if failures:
            failures.pop()
            raise RuntimeError('synthetic fill failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(jobs, 'prepare_supersession_backfill', flaky)
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    _supersessions_filled.pop(h.app.state.engine, None)
    with caplog.at_level('WARNING', logger=worker.log.name):
        for tick in (0, 2, 30, 59.9):  # worker ticks inside one interval: a single attempt
            clock[0] = 1000.0 + tick
            worker.process_one(h.app.state.engine)
        assert calls == [1000.0]
        clock[0] = 1120.0  # exponential backoff after the failed attempt
        worker.process_one(h.app.state.engine)
    assert calls == [1000.0, 1120.0]
    assert [r.message for r in caplog.records].count('supersession_fill_failed') == 1


def test_worker_scan_is_read_only_and_writer_batch_is_capped(h):
    workspace, ids = old_runs(h, 'bounded', [('succeeded', 'both', None), ('succeeded', 'both', None)])
    with transaction(h.app.state.engine) as db:
        snapshot = db.get(m.Job, ids[0]).input_snapshot
    began = datetime(2020, 1, 1, tzinfo=timezone.utc)
    rows = [{
        'id': f'bounded-extra-{i}', 'department_id': h.seed['department_id'], 'workspace_id': workspace,
        'requested_by': h.seed['identities']['coordinator'], 'kind': 'generation', 'status': 'succeeded',
        'input_fingerprint': f'bounded-extra-{i}', 'created_at': began + timedelta(minutes=i),
        'input_snapshot': snapshot, 'result': {'status': 'candidate_feasible', 'additions': []},
    } for i in range(1000)]  # department-scale history; only the configured five-row write may hold the writer
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Job), rows)
    settings = replace(h.settings, worker_supersession_interval_seconds=1,
                       worker_supersession_workspaces=1, worker_supersession_batch_size=5)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if 'jobs' in statement or 'advisory_xact_lock' in statement:
            statements.append((statement, bool(conn.info.get('rota_write'))))
    engine = h.app.state.engine
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        _supersessions_filled.pop(engine, None)
        assert fill_supersessions(engine, settings=settings) == 5
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    scans = [statement for statement, writing in statements
             if statement.lstrip().upper().startswith('SELECT') and 'jobs' in statement and not writing]
    writes = [statement for statement, writing in statements
              if 'UPDATE JOBS SET SUPERSEDES' in statement.upper() and writing]
    assert scans and writes
    assert not any('advisory_xact_lock' in statement for statement, _ in statements)
    assert len(writes) == 1


@pytest.mark.parametrize('field', [
    'worker_supersession_interval_seconds', 'worker_supersession_workspaces',
    'worker_supersession_batch_size',
])
def test_supersession_limits_are_validated(field):
    with pytest.raises(RuntimeError, match='SUPERSESSION'):
        replace(Settings(), **{field: 0}).check(require_authentication=False)


def test_supersession_retry_backoff_limit_is_validated():
    with pytest.raises(RuntimeError, match='SUPERSESSION'):
        replace(Settings(), worker_supersession_retry_max_seconds=30).check(require_authentication=False)


def test_empty_history_persists_done_marker_and_stops_job_scans(h, monkeypatch):
    import apps.worker.main as worker
    clock = [1000.0]
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker, '_supersessions_filled', type(worker._supersessions_filled)())
    settings = replace(h.settings, worker_supersession_interval_seconds=1)
    engine = h.app.state.engine
    assert fill_supersessions(engine, settings=settings) == 0
    with transaction(engine) as db:
        cursor = db.get(m.ReadProjectionWarmCursor, 'generation_supersessions')
        assert cursor.after_id == 'done'
    statements = []
    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        clock[0] += 2
        assert fill_supersessions(engine, settings=settings) == 0
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert not any('FROM jobs' in statement for statement in statements)


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_done_marker_rechecks_a_null_job_inserted_after_scan(request, backend, monkeypatch):
    import apps.worker.main as worker

    h = request.getfixturevalue(backend)
    engine = h.app.state.engine
    clock = [1000.0]
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker, '_supersessions_filled', type(worker._supersessions_filled)())
    settings = replace(h.settings, worker_supersession_interval_seconds=1)
    workspace = h.shift(h.workspace('Synthetic marker race'), people=[])['workspace']
    snapshot = h.snapshot(workspace)
    job_id = m.uid()
    original_mark = worker._mark_supersessions_done

    def insert_before_mark(target_engine):
        with transaction(target_engine, write=True) as db:
            db.execute(insert(m.Job), {'id': job_id, 'department_id': h.seed['department_id'],
                'workspace_id': workspace['id'], 'requested_by': h.seed['identities']['coordinator'],
                'kind': 'generation', 'status': 'succeeded', 'input_fingerprint': 'marker-race',
                'input_snapshot': {'generation_engine': 'legacy', 'focus_ids': snapshot['focus_ids'],
                                   'generation_session_ids': snapshot['focus_ids'], '_deferred_generation': False},
                'result': {'status': 'candidate_feasible', 'additions': []}, 'supersedes': None})
        return original_mark(target_engine)

    monkeypatch.setattr(worker, '_mark_supersessions_done', insert_before_mark)
    assert fill_supersessions(engine, settings=settings) == 0
    with transaction(engine) as db:
        cursor = db.get(m.ReadProjectionWarmCursor, 'generation_supersessions')
        assert cursor is None or cursor.after_id != 'done'

    clock[0] += 2
    assert fill_supersessions(engine, settings=settings) == 1
    with transaction(engine) as db:
        assert db.get(m.Job, job_id).supersedes == []


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_null_generation_insert_clears_done_marker_and_next_pass_fills_it(request, backend, monkeypatch):
    import apps.worker.main as worker

    h = request.getfixturevalue(backend)
    engine = h.app.state.engine
    clock = [1000.0]
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker, '_supersessions_filled', type(worker._supersessions_filled)())
    settings = replace(h.settings, worker_supersession_interval_seconds=1)
    assert fill_supersessions(engine, settings=settings) == 0
    with transaction(engine) as db:
        assert db.get(m.ReadProjectionWarmCursor, 'generation_supersessions').after_id == 'done'

    workspace = h.shift(h.workspace('Synthetic null supersession insert'), people=[])['workspace']
    snapshot = h.snapshot(workspace)
    job_id = m.uid()
    with transaction(engine, write=True) as db:
        db.execute(insert(m.Job), {'id': job_id, 'department_id': h.seed['department_id'],
            'workspace_id': workspace['id'], 'requested_by': h.seed['identities']['coordinator'],
            'kind': 'generation', 'status': 'succeeded', 'input_fingerprint': 'null-supersedes',
            'input_snapshot': {'generation_engine': 'legacy', 'focus_ids': snapshot['focus_ids'],
                               'generation_session_ids': snapshot['focus_ids'], '_deferred_generation': False},
            'result': {'status': 'candidate_feasible', 'additions': []}, 'supersedes': None})
        assert db.get(m.ReadProjectionWarmCursor, 'generation_supersessions').after_id is None

    clock[0] += 2
    assert fill_supersessions(engine, settings=settings) == 1
    with transaction(engine) as db:
        assert db.get(m.Job, job_id).supersedes == []

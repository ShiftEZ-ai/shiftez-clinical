"""Job ownership and operator writes remain correct without the global writer key."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.errors import Problem
from apps.worker import main as worker
from apps.worker import workspace_merge
from tests.test_autoschedule_budget_worker import expire, generation_job, saved_state
from tests.test_generation_apply_scale import completed_proposal
from tests.test_p2_groups import fork, preview
from tests.test_planning_groups_drafts import edit, integrated


pytestmark = pytest.mark.postgres


@pytest.fixture
def validation_job(pg):
    workspace = pg.workspace()
    return pg.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'validation'},
                      revision=workspace['revision'])['job']['id']


def test_only_one_worker_claims_a_job(pg, validation_job):
    engine = pg.app.state.engine
    selected = Event()
    first_committed = Event()

    def claim_while_first_is_open():
        # With autoflush disabled, the old unlocked claim can report what it picked
        # before its pending ORM update waits for the first worker to commit.
        with Session(engine, autoflush=False) as db, db.begin():
            try:
                claim = worker.claim_job(db)
            finally:
                selected.set()
            assert first_committed.wait(10)
            return claim

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with Session(engine) as db, db.begin():
                first = worker.claim_job(db)
                assert first['id'] == validation_job
                db.flush()
                second = pool.submit(claim_while_first_is_open)
                assert selected.wait(10)
        finally:
            first_committed.set()
        assert second.result(timeout=10) is None

    with Session(engine) as db:
        job = db.get(m.Job, validation_job)
        assert (job.status, job.attempts) == ('running', 1)


@pytest.fixture
def claim_with_stale_read(pg):
    engine = pg.app.state.engine

    def run(job_id, kind, payload, operation):
        with Session(engine) as db, db.begin():
            claim = worker.claim_job(db)
            assert claim['id'] == job_id

        # The API command commits on its own connection while the worker's
        # transaction retains the old Job in its identity map.
        with Session(engine) as db, db.begin():
            stale = db.get(m.Job, job_id)
            assert stale.status == 'running'
            with ThreadPoolExecutor(max_workers=1) as pool:
                command = pool.submit(pg.command, 'coordinator', kind, payload, job_id)
                changed = command.result(timeout=10)['job']
            result = operation(db, claim)
        with Session(engine) as db:
            return result, changed, db.get(m.Job, job_id)

    return run


def test_finish_does_not_overwrite_committed_cancel(validation_job, claim_with_stale_read):
    finished, cancelled, job = claim_with_stale_read(
        validation_job, 'job.cancel', {'reason': 'Cancel this attempt'},
        lambda db, claim: worker.finish_job(db, claim, None, 'CHECK_FAILED'))

    assert not finished
    assert (job.status, job.revision) == ('cancelled', cancelled['revision'])


def test_lease_renewal_preserves_added_time(pg, claim_with_stale_read):
    alive, extended, job = claim_with_stale_read(
        generation_job(pg), 'job.add_time', {'minutes': 2, 'reason': 'Extend this attempt'},
        lambda db, claim: worker.heartbeat_job(db, claim, {'stage': 'finding', 'search_elapsed_seconds': 30}))

    assert alive
    assert job.search_control == {**extended['search_control'], 'spent_seconds': 30}


def test_mismatched_worker_cannot_renew_or_finish(pg, validation_job, monkeypatch):
    monkeypatch.setattr(worker, 'WORKER_ID', 'first-test-worker')
    engine = pg.app.state.engine
    with Session(engine) as db, db.begin():
        claim = worker.claim_job(db)
        assert claim['id'] == validation_job

    with Session(engine) as db, db.begin():
        job = db.get(m.Job, validation_job)
        job.worker_id = 'next-test-worker'

    snapshot = select(m.Job.__table__).where(m.Job.id == validation_job)
    with Session(engine) as db:
        before = db.execute(snapshot).one()

    with Session(engine) as db, db.begin():
        assert not worker.heartbeat_job(db, claim, {'stage': 'checking', 'elapsed_seconds': 1})
        assert not worker.finish_job(db, claim, None, 'CHECK_FAILED')

    with Session(engine) as db:
        assert db.execute(snapshot).one() == before


def test_expired_lease_is_reclaimed_once(pg, validation_job, monkeypatch):
    monkeypatch.setattr(worker, 'WORKER_ID', 'first-test-worker')
    engine = pg.app.state.engine
    with Session(engine) as db, db.begin():
        first = worker.claim_job(db)
        assert first['id'] == validation_job
    with Session(engine) as db, db.begin():
        expire(db, validation_job)

    monkeypatch.setattr(worker, 'WORKER_ID', 'next-test-worker')
    with Session(engine) as db, db.begin():
        second = worker.claim_job(db)
        assert (second['id'], second['attempt']) == (validation_job, first['attempt'] + 1)

    with Session(engine) as db, db.begin():
        assert worker.claim_job(db) is None
    with Session(engine) as db:
        job = db.get(m.Job, validation_job)
        assert (job.status, job.attempts, job.worker_id) == ('running', second['attempt'], 'next-test-worker')


def test_finish_preserves_the_committed_resume(pg):
    job_id = generation_job(pg)
    engine = pg.app.state.engine
    with Session(engine) as db, db.begin():
        claim = worker.claim_job(db)
        assert claim['id'] == job_id
    worker.prepare_deferred_generation(engine, claim, pg.settings)

    resume = {'key': 'synthetic resume', 'model': 'synthetic model', 'hint': [1]}
    with Session(engine) as db, db.begin():
        assert worker.checkpoint_job(db, claim, resume)
    with Session(engine) as db:
        assert saved_state(db.get(m.Job, job_id)) == resume

    result = {'status': 'needs_more_time', 'additions': [], 'removals': []}
    with Session(engine) as db, db.begin():
        assert worker.finish_job(db, claim, result, None)
    with Session(engine) as db:
        job = db.get(m.Job, job_id)
        assert (job.status, job.error_code) == ('succeeded', None)
        assert job.result['status'] == 'needs_more_time'
        assert job.result['resume_available'] is True
        assert saved_state(job) == resume


def test_merge_refusal_rolls_back_changes_and_commits_job(pg, monkeypatch):
    target, _, _ = integrated(pg)
    source = fork(pg, target)
    shift = pg.detail(source)['shifts'][0]
    source = edit(pg, source, shift, [{'person_id': pg.seed['people'][0],
        'start': shift['start'], 'end': shift['end']}])['workspace']
    proposal = preview(pg, source, target)['merge']
    target_before = pg.snapshot(target)['shifts']
    source_before = pg.snapshot(source)['shifts']
    job = pg.command('coordinator', 'workspace.merge.apply', {
        'merge_id': proposal['id'], 'reason': 'Apply synthetic merge', 'background': True,
    }, target['id'], target['revision'])['job']

    execute = workspace_merge.execute

    def reject_final_write(db, claim, plan, *, replay=False):
        result = execute(db, claim, plan, replay=replay)
        if replay:
            db.flush()
            assert result['applied_bundle_ids']
            assert db.get(m.Workspace, source['id']).archived_at is not None
            raise Problem(409, 'MERGE_STALE', 'Synthetic final merge refusal')
        return result

    monkeypatch.setattr(workspace_merge, 'execute', reject_final_write)
    engine = pg.app.state.engine
    assert worker.process_one(engine)

    with Session(engine) as db:
        receipt = db.get(m.Job, job['id'])
        assert (receipt.status, receipt.error_code, receipt.result) == ('failed', 'MERGE_STALE', None)
        assert receipt.completed_at is not None
        stored_source = db.get(m.Workspace, source['id'])
        assert (stored_source.revision, stored_source.archived_at) == (source['revision'], None)
        assert db.get(m.Workspace, target['id']).revision == target['revision']
        assert db.get(m.WorkspaceMergeProposal, proposal['id']).status == 'preview'
    assert pg.snapshot(target)['shifts'] == target_before
    assert pg.snapshot(source)['shifts'] == source_before


def test_apply_undo_redo_hold_workspace_before_job_lock(pg):
    workspace = pg.shift(pg.workspace(), people=[])['workspace']
    before = pg.snapshot(workspace)['shifts']
    shift, = before
    person = pg.seed['people'][0]
    job = completed_proposal(pg, workspace, [{'shift_id': shift['id'], 'participant': {
        'person_id': person, 'start': shift['start'], 'end': shift['end']}}])
    engine = pg.app.state.engine
    workspace_id = workspace['id']

    def check(kind, payload, revision, status, target=None):
        probes = []

        def observe_job_lock(connection, cursor, statement, parameters, context, executemany):
            query, _, lock = statement.upper().partition(' FOR ')
            if 'FROM JOBS' not in query or not lock.startswith(('UPDATE', 'NO KEY UPDATE')):
                return
            # A separate transaction must already be unable to lock the workspace.
            with Session(engine) as observer:
                with pytest.raises(DBAPIError) as blocked:
                    observer.execute(select(m.Workspace.id).where(m.Workspace.id == workspace_id)
                                     .with_for_update(nowait=True))
                assert blocked.value.orig.sqlstate == '55P03'
            probes.append(True)

        event.listen(engine, 'before_cursor_execute', observe_job_lock)
        try:
            changed = pg.command('coordinator', kind, payload, target, revision)['workspace']
        finally:
            event.remove(engine, 'before_cursor_execute', observe_job_lock)

        assert probes, f'{kind} did not lock the job'
        with Session(engine) as db:
            assert db.get(m.Job, job['id']).status == status
        return changed

    applied = check('generation.apply', {'workspace_id': workspace_id, 'job_id': job['id'],
        'reason': 'Apply synthetic proposal'}, workspace['revision'], 'applied')
    assert [p['person_id'] for p in pg.snapshot(applied)['shifts'][0]['participants']] == [person]

    undone = check('workspace.undo', {'reason': 'Undo synthetic proposal'},
                   applied['revision'], 'succeeded', workspace_id)
    assert pg.snapshot(undone)['shifts'] == before

    redone = check('workspace.redo', {'reason': 'Restore synthetic proposal'},
                   undone['revision'], 'applied', workspace_id)
    assert [p['person_id'] for p in pg.snapshot(redone)['shifts'][0]['participants']] == [person]

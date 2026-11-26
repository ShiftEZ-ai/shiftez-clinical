"""Job ownership and operator writes remain correct without the global writer key."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.worker import main as worker
from tests.test_autoschedule_budget_worker import generation_job


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

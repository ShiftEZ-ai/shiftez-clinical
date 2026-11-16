"""Bounded outbox batches preserve recipient privacy, retries and delivery evidence."""
from datetime import date, timedelta
from time import perf_counter, process_time
import threading
import time
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy import insert

from apps.api import live, models as m
from apps.api.db import retry_write, transaction
from apps.api.services import announcements
from apps.api.version import build
from apps.worker import main as worker
from tests.test_performance_budget import python_budget


def enqueue(h, count):
    person = h.get('staff', '/api/me')['person_id']
    with transaction(h.app.state.engine, write=True) as db:
        events = [m.Outbox(department_id=h.seed['department_id'], event_key=uuid.uuid4().hex,
                          kind='availability', payload={'person_id': person,
                              'actor_id': h.seed['identities']['coordinator']}) for _ in range(count)]
        db.add_all(events)
        db.flush()
        return [event.id for event in events]


@pytest.fixture(scope="module")
def dispatched_department_notifications(scale_harness):
    pg = scale_harness('pg')
    # Each event reaches the staff member, approver and administrator, excluding its coordinator actor.
    event_ids = enqueue(pg, 100)
    started, wall_started = process_time(), perf_counter()
    processed = worker.process_one(pg.app.state.engine)
    elapsed = process_time() - started
    wall_elapsed = perf_counter() - wall_started
    print(f'300 notifications: {elapsed:.3f} CPU s / {wall_elapsed:.3f} wall s')
    return pg, event_ids, elapsed, processed, wall_elapsed


@pytest.mark.postgres
def test_300_notifications_dispatch_within_budget_postgres(dispatched_department_notifications):
    pg, _, _, processed, _ = dispatched_department_notifications
    assert processed
    with transaction(pg.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Notification)) == 300
        assert not db.scalar(select(m.Outbox.id).where(m.Outbox.state != 'available_in_app'))
        assert all(event.attempts == 1 for event in db.scalars(select(m.Outbox)))
    assert len(pg.get('staff', '/api/notifications')['notifications']) == 100
    assert pg.get('coordinator', '/api/notifications')['notifications'] == []
    # A second pass does not duplicate notifications or rewrite completed events.
    assert not worker.process_one(pg.app.state.engine)
    with transaction(pg.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Notification)) == 300
        assert all(event.attempts == 1 for event in db.scalars(select(m.Outbox)))


@pytest.mark.postgres
@pytest.mark.cpu_budget
def test_300_notifications_dispatch_within_budget_postgres_cpu_budget(dispatched_department_notifications):
    _, event_ids, elapsed, _, _ = dispatched_department_notifications
    assert elapsed < python_budget(3), (elapsed, len(event_ids))


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_300_notifications_dispatch_within_budget_postgres_wall_benchmark(dispatched_department_notifications):
    _, event_ids, _, _, elapsed = dispatched_department_notifications
    assert elapsed < python_budget(3), (elapsed, len(event_ids))


@pytest.mark.postgres
def test_legacy_heartbeat_records_worker_release_postgres(pg, monkeypatch):
    engine = pg.app.state.engine
    monkeypatch.setenv('ROTA_BUILD_COMMIT', 'fedcba9876543210')
    with transaction(engine, write=True) as db:
        db.add(m.WorkerHeartbeat(worker_id=worker.WORKER_ID, started_at=m.now()-timedelta(days=1),
                                 last_seen_at=m.now()-timedelta(minutes=1), passes=39, release=None))
    worker._beat.pop(id(engine), None)
    before = m.now()
    worker.heartbeat(engine)
    with transaction(engine) as db:
        row = db.get(m.WorkerHeartbeat, worker.WORKER_ID)
        assert row.release == build()['release'] and row.passes == 40
        assert before <= row.last_seen_at <= m.now()
    assert pg.get('staff', '/api/about')['worker']['release'] == build()['release']


@pytest.mark.postgres
def test_batch_failure_isolated_and_retry_due_time_respected_postgres(pg, monkeypatch):
    engine = pg.app.state.engine
    first, failed, last, future = enqueue(pg, 4)
    moment = m.now()
    monkeypatch.setattr(m, 'now', lambda: moment)
    with transaction(engine, write=True) as db:
        db.get(m.Outbox, future).next_attempt_at = m.now()+timedelta(days=1)
    deliver = worker.deliver_notifications

    def fault(db, event, **recipients):
        deliver(db, event, **recipients)
        if event.id == failed:
            raise RuntimeError('Private failure text must stay out of retry evidence')

    with monkeypatch.context() as context:
        context.setattr(worker, 'deliver_notifications', fault)
        assert worker.process_one(engine)
    with transaction(engine) as db:
        assert db.get(m.Outbox, first).state == db.get(m.Outbox, last).state == 'available_in_app'
        row = db.get(m.Outbox, failed)
        assert row.state == 'retry' and row.attempts == 1 and row.last_error == 'RuntimeError'
        assert row.next_attempt_at > m.now()
        assert db.get(m.Outbox, future).state == 'queued'
        assert db.scalar(select(func.count()).select_from(m.Notification)) == 6
        assert not db.scalar(select(m.Notification.id).where(m.Notification.outbox_id == failed))
    assert not worker.process_one(engine)  # Neither future event nor retry is due yet.
    with transaction(engine, write=True) as db:
        db.get(m.Outbox, failed).next_attempt_at = m.now()-timedelta(seconds=1)
    assert worker.process_one(engine)
    with transaction(engine) as db:
        row = db.get(m.Outbox, failed)
        assert row.state == 'available_in_app' and row.attempts == 2 and row.last_error is None
        assert db.scalar(select(func.count()).select_from(m.Notification)) == 9


@pytest.mark.postgres
def test_reconcile_sql_failure_rolls_back_department_and_dispatches_outbox_postgres(pg, monkeypatch, caplog):
    engine = pg.app.state.engine
    event_id, = enqueue(pg, 1)
    private = 'synthetic-private-recipient@example.test'
    staff = pg.seed['identities']['staff']
    with transaction(engine, write=True) as db:
        healthy = m.Department(organisation_id=pg.seed['organisation_id'], name='Healthy test department')
        db.add(healthy)
        db.flush()
        db.add(m.RoleBinding(department_id=healthy.id, identity_id=staff, role='staff', starts_on=date(2020, 1, 1)))
        db.add(m.Identity(issuer='reconcile-test', subject=private, display_name='Synthetic private recipient'))
        bad = m.DepartmentContent(department_id=pg.seed['department_id'], name='Bad test notice', starts_on=date(2020, 1, 1))
        good = m.DepartmentContent(department_id=healthy.id, name='Healthy test notice', starts_on=date(2020, 1, 1))
        db.add_all([bad, good])
        db.flush()
        bad_id, good_id = bad.id, good.id
    sync_receipts = announcements.sync_receipts

    def fault(db, row, *args, **kwargs):
        targets, inserted = sync_receipts(db, row, *args, **kwargs)
        if row.id == bad_id:
            assert targets and inserted  # Receipt writes precede a real SQL error that aborts PostgreSQL's transaction.
            db.execute(insert(m.Identity).values(issuer='reconcile-test', subject=private,
                display_name='Synthetic private recipient'))
        return targets, inserted

    monkeypatch.setattr(announcements, 'sync_receipts', fault)
    assert worker.process_one(engine)
    with transaction(engine) as db:
        assert db.get(m.DepartmentContent, bad_id).receipts_checked_on is None
        assert not db.scalar(select(m.AnnouncementReceipt.id).where(m.AnnouncementReceipt.content_id == bad_id))
        assert db.get(m.DepartmentContent, good_id).receipts_checked_on is not None
        assert db.scalar(select(m.AnnouncementReceipt.identity_id).where(m.AnnouncementReceipt.content_id == good_id)) == staff
        assert db.get(m.Outbox, event_id).state == 'available_in_app'
        assert db.scalar(select(func.count()).select_from(m.Notification).where(m.Notification.outbox_id == event_id)) == 3
    failures = [record for record in caplog.records if getattr(record, 'error_code', None) == 'IntegrityError']
    assert len(failures) == 1
    assert private not in caplog.text and private not in repr([record.__dict__ for record in caplog.records])


def test_batch_leaves_events_after_limit_for_next_pass(h):
    enqueue(h, 4)
    assert retry_write(h.app.state.engine, lambda db: worker.dispatch_batch(db, limit=2)) == {h.seed['department_id']}
    with transaction(h.app.state.engine) as db:
        states = [row.state for row in db.scalars(select(m.Outbox))]
        assert states.count('available_in_app') == states.count('queued') == 2
        assert db.scalar(select(func.count()).select_from(m.Notification)) == 6


@pytest.mark.postgres
def test_notifications_dispatch_while_generation_job_is_running_postgres(scale_harness):
    pg = scale_harness('pg')
    workspace = pg.workspace()
    pg.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
               revision=workspace['revision'])
    started, release = threading.Event(), threading.Event()
    result, failure = {}, []

    def scheduler(snapshot, **options):
        started.set()
        assert release.wait(8), 'long-job fixture was not released'
        return {'status': 'candidate_feasible', 'additions': [], 'removals': []}

    def run_job():
        try:
            result['worked'] = worker.process_one(pg.app.state.engine, scheduler=scheduler, settings=pg.settings)
        except BaseException as exc:  # surface worker failures after the timing assertion
            failure.append(exc)

    job_thread = threading.Thread(target=run_job)
    job_thread.start()
    assert started.wait(8), 'generation job did not enter its long-running phase'
    event_ids = enqueue(pg, 1)
    with transaction(pg.app.state.engine, write=True) as db:
        live.signal(db, pg.seed['department_id'], scope='inbox')
    stopped = threading.Event()
    dispatcher = threading.Thread(target=worker.notification_loop,
                                  args=(pg.app.state.engine, pg.settings, stopped))
    began = time.perf_counter()
    dispatcher.start()
    try:
        with transaction(pg.app.state.engine) as db:
            while time.perf_counter() - began < 2:
                if db.scalar(select(m.Notification.id).where(m.Notification.outbox_id.in_(event_ids)).limit(1)):
                    break
                threading.Event().wait(.02)
            assert db.scalar(select(m.Notification.id).where(m.Notification.outbox_id.in_(event_ids)).limit(1))
        elapsed = time.perf_counter() - began
        print(f'notification dispatch during long job: {elapsed:.3f}s')
    finally:
        stopped.set()
        release.set()
        dispatcher.join(timeout=8)
        job_thread.join(timeout=8)
    assert not failure, failure
    assert result.get('worked')
    return locals()


@pytest.mark.wall_benchmark
def test_notifications_dispatch_wall_budget(scale_harness):
    state = test_notifications_dispatch_while_generation_job_is_running_postgres(scale_harness)
    assert state['elapsed'] < python_budget(2)

"""Database and application guarantees for live records."""
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.runtime.migration import MigrationContext
from sqlalchemy import event, inspect, select, update
from sqlalchemy.exc import IntegrityError

from apps.api import commands, models as m
from apps.api.db import make_engine, transaction
from apps.api.errors import integrity_problem, unique_violation
from apps.api.services import jobs
from apps.worker import main as worker
from scripts import manage
from .conftest import ROLES, TEST_PASSWORD
from .test_data_safety import session_limits_database  # noqa: F401


def job_row(seed, workspace, kind, **overrides):
    return {'department_id': seed['department_id'], 'requested_by': seed['identities']['coordinator'],
            'workspace_id': workspace, 'kind': kind, 'input_fingerprint': 'a' * 64,
            'input_snapshot': {}, **overrides}


@pytest.mark.parametrize('duplicate', [None, 'calendar_feed', 'validation', 'generation'])
def test_seeded_upgrade_preflights_retained_rows(session_limits_database, duplicate):
    url = session_limits_database
    config = manage.alembic_config(url)
    command.upgrade(config, '0157_leave_balance_policy')
    engine = make_engine(url)
    try:
        with transaction(engine, write=True) as db:
            seed = manage.demo_data(db, {role: TEST_PASSWORD for role in ROLES})
            if duplicate == 'calendar_feed':
                table = m.CalendarFeed.__table__
                values = [{'department_id': seed['department_id'], 'identity_id': seed['identities']['staff'],
                           'person_id': seed['people'][0], 'token_hash': uuid4().hex} for _ in range(2)]
                index_name = 'uq_calendar_feeds_active'
            elif duplicate:
                owner = seed['identities']['coordinator']
                workspace = m.Workspace(department_id=seed['department_id'], name='Retained test rota',
                                        starts_on=date(2026, 1, 1), ends_on=date(2026, 1, 2),
                                        created_by=owner, last_edited_by=owner)
                db.add(workspace)
                db.flush()
                table = m.Job.__table__
                snapshot = {'validation_request_key': 'retained-test-key'} if duplicate == 'validation' else {}
                values = [job_row(seed, workspace.id, duplicate, input_snapshot=snapshot) for _ in range(2)]
                index_name = f'uq_jobs_live_{duplicate}'
            if duplicate:
                db.execute(table.insert(), values)
                retained = db.execute(select(table).order_by(table.c.id)).all()
        if duplicate:
            with pytest.raises(SystemExit, match='0158_live_uniqueness.*duplicate rows') as refusal:
                manage.check_migrations(url)
            assert 'retained-test-key' not in str(refusal.value)
            with engine.connect() as db:
                assert MigrationContext.configure(db).get_current_revision() == '0157_leave_balance_policy'
                assert index_name not in {index['name'] for index in inspect(db).get_indexes(table.name)}
                assert db.execute(select(table).order_by(table.c.id)).all() == retained
            return
        assert manage.check_migrations(url)[0] == '0158_live_uniqueness'
        command.upgrade(config, '0158_live_uniqueness')
        with transaction(engine) as db:
            assert db.get(m.Department, seed['department_id']) is not None
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.parametrize('name,refusal', [
    ('uq_calendar_feeds_active', 'INTEGRITY_CONFLICT'),
    ('uq_jobs_live_validation', None),
    ('uq_jobs_live_generation', 'JOB_ALREADY_RUNNING'),
])
def test_postgres_refuses_duplicate_raw_writes(pg, monkeypatch, name, refusal):
    engine = pg.app.state.engine
    seed = pg.seed
    if name == 'uq_calendar_feeds_active':
        table = m.CalendarFeed.__table__
        values = {'department_id': seed['department_id'], 'identity_id': seed['identities']['staff'],
                  'person_id': seed['people'][0], 'token_hash': uuid4().hex}
        second = {**values, 'token_hash': uuid4().hex}
    else:
        table = m.Job.__table__
        workspace = pg.workspace()['id']
        kind = {'uq_jobs_live_validation': 'validation', 'uq_jobs_live_generation': 'generation'}[name]
        values = job_row(seed, workspace, kind)
        if kind == 'validation':
            values['input_snapshot'] = {'validation_request_key': 'raw-key'}
        second = values

    # The second connection remains open across the first commit. Both bypass all API checks.
    with engine.connect() as first, engine.connect() as second_connection:
        first.execute(table.insert().values(**values))
        first.commit()
        with pytest.raises(IntegrityError) as conflict:
            second_connection.execute(table.insert().values(**second))
        assert unique_violation(conflict.value) == name
        second_connection.rollback()

    if refusal:
        def conflict_at_dispatch(*args, **kwargs):
            raise conflict.value

        monkeypatch.setattr(commands, 'dispatch', conflict_at_dispatch)
        result = pg.command('staff', 'calendar_feed.create', status=409)
        assert result['error']['code'] == refusal
        assert set(result['error']) == {'code', 'message'}
        assert 'path' not in result


@pytest.mark.postgres
@pytest.mark.parametrize('winner_statuses', [
    ('queued',), ('failed',), ('cancelled', 'queued'), ('failed', 'cancelled'),
], ids=['live-winner', 'fresh-insert', 'second-live-winner', 'bounded-conflict'])
def test_validation_conflict_recovers_live_work_or_refuses(pg, monkeypatch, winner_statuses):
    engine = pg.app.state.engine
    workspace_id = pg.workspace()['id']
    seed = pg.seed
    actor = SimpleNamespace(department_id=seed['department_id'], identity_id=seed['identities']['coordinator'])
    # Workspace creation queues its own check. Commit its cancellation before starting the race.
    with engine.begin() as db:
        db.execute(update(m.Job).where(m.Job.workspace_id == workspace_id, m.Job.kind == 'validation')
                   .values(status='cancelled'))
    winners = []

    def commit_winner(session, *_):
        contender = next((row for row in session.new if isinstance(row, m.Job)), None)
        if contender is None or len(winners) == len(winner_statuses):
            return
        winner = str(uuid4())
        key = contender.input_snapshot['validation_request_key']
        with engine.begin() as other:
            other.execute(m.Job.__table__.insert().values(**job_row(seed, workspace_id, 'validation',
                id=winner, input_snapshot={'validation_request_key': key})))
        winners.append(winner)

    def end_winner_after_collision(error):
        name = unique_violation(error)
        if name == 'uq_jobs_live_validation':
            status = winner_statuses[len(winners) - 1]
            if status != 'queued':
                with engine.begin() as other:
                    other.execute(update(m.Job).where(m.Job.id == winners[-1])
                                  .values(status=status, completed_at=m.now()))
        return name

    def queue_check():
        with transaction(engine, write=True) as db:
            workspace = db.get(m.Workspace, workspace_id)
            event.listen(db, 'before_flush', commit_winner)
            db.execute(m.Tag.__table__.insert().values(department_id=actor.department_id, name='Outer write'))
            job = jobs.queue_validation(db, actor, workspace, {'fingerprint': 'a' * 64, 'validation_basis': 'current'})
            assert job.status == 'queued'
            return job.id

    monkeypatch.setattr(jobs, 'unique_violation', end_winner_after_collision)
    refused = winner_statuses == ('failed', 'cancelled')
    if refused:
        with pytest.raises(IntegrityError) as conflict:
            queue_check()
        assert integrity_problem(conflict.value).code == 'STALE_INPUTS'
    else:
        queued = queue_check()
        if winner_statuses[-1] == 'queued':
            assert queued == winners[-1]
        else:
            assert queued not in winners
    assert len(winners) == len(winner_statuses)
    with transaction(engine) as db:
        assert (db.scalar(select(m.Tag.id).where(m.Tag.name == 'Outer write')) is None) == refused
        assert [db.get(m.Job, winner).status for winner in winners] == list(winner_statuses)
        assert db.scalars(select(m.Job.id).where(m.Job.workspace_id == workspace_id,
                                                m.Job.kind == 'validation', m.Job.status == 'queued')).all() == (
            [] if refused else [queued])


@pytest.mark.postgres
def test_live_heavy_slot_leaves_another_kind_queued(pg):
    engine = pg.app.state.engine
    workspace = pg.workspace()['id']
    pending = str(uuid4())
    with engine.begin() as db:
        db.execute(update(m.Job).where(m.Job.workspace_id == workspace, m.Job.kind == 'validation')
                   .values(status='cancelled'))
        db.execute(m.Job.__table__.insert().values(
            **job_row(pg.seed, workspace, 'fill_options', status='running', started_at=m.now())))
        db.execute(m.Job.__table__.insert().values(**job_row(pg.seed, workspace, 'feasibility', id=pending)))
    assert worker.claim_pending(engine) is None
    with transaction(engine) as db:
        assert (db.get(m.Job, pending).status, db.get(m.Job, pending).attempts) == ('queued', 0)

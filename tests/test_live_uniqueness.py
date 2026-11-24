"""Database and application guarantees for live records."""
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError

from apps.api import commands, models as m
from apps.api.db import make_engine, transaction
from apps.api.errors import unique_violation
from apps.api.services import jobs
from apps.worker import main as worker
from scripts import manage
from .conftest import ROLES, TEST_PASSWORD
from .test_data_safety import session_limits_database  # noqa: F401


PRECHECK = Path(__file__).resolve().parents[1] / 'migrations' / '0153_live_uniqueness_precheck.sql'


def job_row(seed, workspace, kind, **overrides):
    return {'department_id': seed['department_id'], 'requested_by': seed['identities']['coordinator'],
            'workspace_id': workspace, 'kind': kind, 'input_fingerprint': 'a' * 64,
            'input_snapshot': {}, **overrides}


def test_seeded_upgrade_has_no_duplicates(session_limits_database):
    url = session_limits_database
    config = manage.alembic_config(url)
    command.upgrade(config, '0152_feasibility_results')
    engine = make_engine(url)
    try:
        with transaction(engine, write=True) as db:
            seed = manage.demo_data(db, {role: TEST_PASSWORD for role in ROLES})
        with engine.connect() as db:
            for query in PRECHECK.read_text(encoding='utf-8').strip().rstrip(';').split(';'):
                assert db.exec_driver_sql(query).all() == []
        assert manage.check_migrations(url)[0] == '0153_live_uniqueness'
        command.upgrade(config, '0153_live_uniqueness')
        with transaction(engine) as db:
            assert db.get(m.Department, seed['department_id']) is not None
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.parametrize('name,refusal', [
    ('uq_calendar_feeds_active', 'INTEGRITY_CONFLICT'),
    ('uq_people_active_short_code', 'SHORT_CODE_TAKEN'),
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
    elif name == 'uq_people_active_short_code':
        table = m.Person.__table__
        values = {'organisation_id': seed['organisation_id'], 'display_name': 'Synthetic person (test)',
                  'short_code': 'CODE01'}
        second = values
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
def test_validation_conflict_returns_winner_and_preserves_outer_write(pg):
    engine = pg.app.state.engine
    workspace_id = pg.workspace()['id']
    seed = pg.seed
    actor = SimpleNamespace(department_id=seed['department_id'], identity_id=seed['identities']['coordinator'])
    # Workspace creation queues its own check. Commit its cancellation before starting the race.
    with engine.begin() as db:
        db.execute(update(m.Job).where(m.Job.workspace_id == workspace_id, m.Job.kind == 'validation')
                   .values(status='cancelled'))
    with transaction(engine, write=True) as db:
        workspace = db.get(m.Workspace, workspace_id)
        winner = str(uuid4())

        def commit_winner(session, *_):
            contender = next((row for row in session.new if isinstance(row, m.Job)), None)
            if contender is not None:
                key = contender.input_snapshot['validation_request_key']
                with engine.begin() as other:
                    other.execute(m.Job.__table__.insert().values(**job_row(seed, workspace_id, 'validation',
                        id=winner, input_snapshot={'validation_request_key': key})))

        event.listen(db, 'before_flush', commit_winner)
        db.execute(m.Tag.__table__.insert().values(department_id=actor.department_id, name='Outer write'))
        result = jobs.queue_validation(db, actor, workspace, {'fingerprint': 'a' * 64, 'validation_basis': 'current'})
        assert result.id == winner
    with transaction(engine) as db:
        assert db.scalar(select(m.Tag.id).where(m.Tag.name == 'Outer write')) is not None
        assert db.scalars(select(m.Job.id).where(m.Job.workspace_id == workspace_id,
                                                m.Job.kind == 'validation', m.Job.status == 'queued')).all() == [winner]


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

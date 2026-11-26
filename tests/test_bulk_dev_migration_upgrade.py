"""Bulk migrations extend the exact admitted dev schema and retain its settings and evidence."""
from datetime import timedelta

from alembic import command
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import MetaData, inspect, select, text

from apps.api import models as m
from apps.api.db import make_engine, transaction
from migrations import safety
from scripts.manage import alembic_config, check_migrations
from .migration_support import historical_seed, without_installation_admin_backfill
from .test_email_migration import migration_url  # noqa: F401


def test_bulk_upgrade_from_dev_head_preserves_settings_and_evidence(migration_url, monkeypatch):
    config = alembic_config(migration_url)
    scripts = ScriptDirectory.from_config(config)
    head, = scripts.get_heads()
    assert scripts.get_revision('0118_shift_occurrence_index').down_revision == '0117_backend_read_indexes'
    assert scripts.get_revision('0117_backend_read_indexes').down_revision == '0116_password_login_hardening'
    assert scripts.get_revision('0116_password_login_hardening').down_revision == '0115_worker_poll_indexes'
    assert scripts.get_revision('0115_worker_poll_indexes').down_revision == '0114_request_decision_times'
    assert scripts.get_revision('0114_request_decision_times').down_revision == '0113_department_quota_calendar'
    assert scripts.get_revision('0113_department_quota_calendar').down_revision == '0112_timeline_leave_policy'
    assert scripts.get_revision('0112_timeline_leave_policy').down_revision == '0111_autoschedule_resume'
    assert scripts.get_revision('0111_autoschedule_resume').down_revision == '0110_feedback_github_reservation'
    assert scripts.get_revision('0110_feedback_github_reservation').down_revision == '0098_bulk_privacy'
    assert scripts.get_revision('0098_bulk_privacy').down_revision == '0093_bulk_changes'
    assert scripts.get_revision('0093_bulk_changes').down_revision == '0109_fair_share_dials'
    pending = [revision.revision for revision in reversed(list(scripts.iterate_revisions(head, '0109_fair_share_dials')))]
    command.upgrade(config, '0109_fair_share_dials')
    engine = make_engine(migration_url)
    try:
        before = MetaData()
        before.reflect(engine)
        assert 'bulk_staged' not in before.tables
        assert 'bulk_settings' not in before.tables['departments'].c
        assert 'manual_type_override' not in before.tables['availability'].c
        assert 'batch_id' not in before.tables['definition_history'].c
        with transaction(engine, write=True) as db:
            seed = historical_seed(db)
            department = seed['department_id']
            moment = m.now()
            base = {'revision': 7, 'created_at': moment, 'department_id': department}
            db.execute(before.tables['departments'].update().where(
                before.tables['departments'].c.id == department).values(
                    wish_settings={'types': {'prefer_off': {'default_points': 3}}},
                    email_settings={'enabled': False}, event_horizon_days=91,
                    leave_reason_display='details', swap_open_offers=False,
                    swap_offer_window_days=13, swap_open_offer_cap=31))
            db.execute(before.tables['availability'].insert().values(**base, id='retained-leave',
                person_id=seed['people'][0], start=moment, end=moment + timedelta(hours=1),
                kind='recorded_leave', private_reason='Retained private live record',
                recorded_by=seed['identities']['coordinator'], retracted=False))
            db.execute(before.tables['definition_history'].insert().values(**base, id='retained-history',
                kind='availability.update', object_id='retained-leave', object_revision=7,
                snapshot={'kind': 'recorded_leave', 'private_reason': 'Legacy private copy'},
                actor_id=seed['identities']['coordinator'], reason='Retained attribution'))
            db.execute(before.tables['audit_events'].insert().values(**base, id='retained-audit',
                actor_id=seed['identities']['coordinator'], action='availability.update', target_id='retained-leave',
                reason='Retained audit evidence', detail={'fields': ['end']}, correlation_id='synthetic-migration'))
            saved = {name: [dict(row) for row in db.execute(select(table).order_by(*table.primary_key.columns)).mappings()]
                     for name, table in before.tables.items() if name != 'alembic_version'}
        confirmations = set(safety.reviewed_confirmations()).intersection(pending)
        assert {'0098_bulk_privacy', '0114_request_decision_times', '0116_password_login_hardening',
                '0117_backend_read_indexes'} <= confirmations
        # Every pending reviewed revision still needs its exact ID, never a blanket bypass.
        with monkeypatch.context() as production:
            production.setenv('ROTA_ENV', 'production')
            for revision in confirmations:
                with pytest.raises(SystemExit, match='--confirm-destructive ' + revision):
                    check_migrations(migration_url, confirmations - {revision})
            assert check_migrations(migration_url, confirmations) == pending
        command.upgrade(config, 'head')
        command.upgrade(config, 'head')
        after = MetaData()
        after.reflect(engine)
        with engine.connect() as db:
            current = without_installation_admin_backfill({
                name: list(db.execute(select(after.tables[name]).order_by(*after.tables[name].primary_key.columns)).mappings())
                for name in saved})
            for name, rows in saved.items():
                if name == 'definition_history':
                    rows = [{**row, 'snapshot': {'kind': 'recorded_leave'}} for row in rows]
                assert [{key: row[key] for key in before.tables[name].c.keys()} for row in current[name]] == rows, name
            assert db.scalar(text('SELECT version_num FROM alembic_version')) == head
            assert db.scalar(text('SELECT count(*) FROM bulk_staged')) == 0
            assert db.scalar(text('SELECT bulk_settings FROM departments WHERE id = :id'), {'id': department}) in ({}, '{}')
            assert not db.scalar(text('SELECT manual_type_override FROM availability'))
            assert db.scalar(text('SELECT batch_id FROM definition_history')) is None
            if engine.dialect.name == 'postgresql':
                assert db.scalar(text("SELECT prosecdef FROM pg_proc WHERE oid = "
                    "'rota_redact_person_history(text)'::regprocedure"))
                assert db.scalar(text("SELECT rota_redact_person_history('missing-person')")) == 0
        assert any(index['name'] == 'ix_bulk_staged_owner_status' for index in inspect(engine).get_indexes('bulk_staged'))
    finally:
        engine.dispose()

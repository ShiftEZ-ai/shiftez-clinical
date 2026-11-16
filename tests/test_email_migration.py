"""Additive M4 upgrades preserve existing receipts, email deliveries, consent and constraints."""
from datetime import date, timedelta
import uuid

import pytest
from alembic import command
from sqlalchemy import MetaData, create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from apps.api.db import make_engine
from apps.api import models as m
from scripts.manage import alembic_config


@pytest.fixture(params=['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)])
def migration_url(request, tmp_path):
    if request.param == 'sqlite':
        yield 'sqlite:///' + str(tmp_path / 'm4-upgrade.db')
        return
    url = request.getfixturevalue('postgres_worker_url')
    schema = 'rota_test_' + uuid.uuid4().hex
    admin = create_engine(url, isolation_level='AUTOCOMMIT', hide_parameters=True)
    try:
        with admin.connect() as db:
            db.execute(text('CREATE SCHEMA ' + schema))
        yield make_url(url).update_query_dict({'options': '-csearch_path=' + schema}).render_as_string(hide_password=False)
    finally:
        with admin.connect() as db:
            db.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
        admin.dispose()


@pytest.mark.parametrize('previous', ['0082_events', '0087_email_delivery'])
def test_m4_upgrade_preserves_existing_data_and_checks(migration_url, previous):
    url = migration_url
    config = alembic_config(url)
    command.upgrade(config, previous)
    engine = make_engine(url)
    before = MetaData()
    before.reflect(engine)
    assert 'reminder_empty_checked_on' not in before.tables['department_content'].c
    original_checks = {table: {r['sqltext'] for r in inspect(engine).get_check_constraints(table)}
                       for table in ('events', 'department_content')}
    # Reflected tables retain the exact schema and every column already present at each predecessor.
    tables = ['organisations', 'departments', 'identities', 'department_content', 'announcement_receipts', 'event_types', 'events',
              'event_occurrences']
    with engine.begin() as db:
        moment = m.now()
        base = {'revision': 7, 'created_at': moment}
        db.execute(before.tables['organisations'].insert().values(**base, id='org', name='Synthetic', timezone='UTC', week_start=0,
                   input_revision=1, availability_revision=1))
        db.execute(before.tables['departments'].insert().values(**base, id='dept', organisation_id='org', name='Synthetic',
                   generation_defaults={}))
        db.execute(before.tables['identities'].insert().values(**base, id='reader', issuer='synthetic', subject='reader',
                   display_name='Synthetic reader', disabled=False))
        content = {'also_email': True, 'email_sender': 'microsoft', 'email_sender_identity_id': 'reader'} if previous == '0087_email_delivery' else {}
        db.execute(before.tables['department_content'].insert().values(**base, **content, id='content', department_id='dept', name='Evidence',
                   description='Retained', kind='announcement', url='', active=True, starts_on=date(2026, 1, 1),
                   receipts_checked_on=date(2026, 1, 1), last_reminded_at=moment))
        db.execute(before.tables['announcement_receipts'].insert().values(**base, id='receipt', content_id='content',
                   identity_id='reader', delivered_at=moment, read_at=moment, acknowledged_at=moment))
        db.execute(before.tables['event_types'].insert().values(**base, id='type', department_id='dept', name='Synthetic type',
                   starts_on=date(2026, 1, 1)))
        db.execute(before.tables['events'].insert().values(**base, **content, id='event', department_id='dept',
                   event_type_id='type', heading='Synthetic event', start=moment, end=moment + timedelta(hours=1)))
        db.execute(before.tables['event_occurrences'].insert().values(**base, id='occurrence', department_id='dept', event_id='event',
                   original_start=moment, start=moment, end=moment + timedelta(hours=1), data={}))
        db.execute(before.tables['announcement_receipts'].insert().values(**base, id='event-receipt',
                   event_occurrence_id='occurrence', identity_id='reader', delivered_at=moment, acknowledged_at=moment))
        if previous == '0087_email_delivery':
            for oid, kind, state, payload in [('source', 'announcement', 'available_in_app', {'content_id': 'content', 'revision': 7}),
                                              ('child', 'email', 'sent', {'source_outbox_id': 'source'})]:
                db.execute(before.tables['outbox'].insert().values(**base, id=oid, department_id='dept', event_key='synthetic-' + oid,
                           kind=kind, payload=payload, state=state, attempts=1, next_attempt_at=moment))
            db.execute(before.tables['email_deliveries'].insert().values(**base, id='delivery', department_id='dept',
                       outbox_id='child', source_outbox_id='source', receipt_id='receipt', identity_id='reader',
                       sender_identity_id='reader', sender_mode='microsoft', address='reader@example.test', state='sent', sent_at=moment))
            db.execute(before.tables['mailbox_connections'].insert().values(**base, id='consent', department_id='dept', identity_id='reader',
                       encrypted_refresh_token='synthetic-opaque-ciphertext', configuration_hash='a' * 64, updated_at=moment))
            tables += ['outbox', 'email_deliveries', 'mailbox_connections']
        saved = {table: [dict(row) for row in db.execute(select(before.tables[table]).order_by(before.tables[table].c.id)).mappings()]
                 for table in tables}
    # Qualify M4 itself; later migrations may append their own authenticated maintenance identity.
    command.upgrade(config, '0096_meetings_email_merge')
    after = MetaData()
    after.reflect(engine)
    with engine.connect() as db:
        for table, rows in saved.items():
            actual = list(db.execute(select(after.tables[table]).order_by(after.tables[table].c.id)).mappings())
            assert len(actual) == len(rows)
            assert [{key: current[key] for key in previous_row} for current, previous_row in zip(actual, rows)] == rows
        actual = dict(db.execute(select(after.tables['department_content'])).mappings().one())
    assert actual['reminder_empty_checked_on'] is None
    if previous == '0082_events':
        assert actual['also_email'] is None and actual['email_sender'] is None
    for table, checks in original_checks.items():
        assert {r['sqltext'] for r in inspect(engine).get_check_constraints(table)} == checks
        foreign_keys = inspect(engine).get_foreign_keys(table)
        assert any(fk['constrained_columns'] == ['email_sender_identity_id'] and fk['referred_table'] == 'identities'
                   and fk['referred_columns'] == ['id'] for fk in foreign_keys)
        with pytest.raises(IntegrityError), engine.begin() as db:
            db.execute(after.tables[table].update().values(email_sender_identity_id='missing-identity'))
        with engine.begin() as db:
            db.execute(after.tables[table].update().values(email_sender_identity_id='reader'))
    assert {'email_deliveries', 'mailbox_connections', 'mailbox_oauth_flows'} <= set(after.tables)
    engine.dispose()

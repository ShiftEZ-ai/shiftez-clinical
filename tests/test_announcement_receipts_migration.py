"""M1 extends existing evidence without reinterpreting or overwriting it."""
from datetime import datetime, timezone

from alembic import command
from sqlalchemy import MetaData, select
from apps.api.db import make_engine
from scripts.manage import alembic_config
from .migration_support import add_historical_admin


def test_migration_copies_only_legacy_read_evidence_and_keeps_all_rows(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'legacy.db')
    config = alembic_config(url)
    command.upgrade(config, '0070_open_ended_attachments')
    engine = make_engine(url)
    metadata = MetaData()
    metadata.reflect(engine)
    at = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
    common = {'revision': 1, 'created_at': at}
    with engine.begin() as db:
        db.execute(metadata.tables['organisations'].insert().values(**common, id='org', name='Synthetic',
            timezone='UTC', week_start=0, input_revision=1, availability_revision=1))
        db.execute(metadata.tables['departments'].insert().values(**common, id='dept', organisation_id='org',
            name='Synthetic', generation_defaults={}))
        db.execute(metadata.tables['identities'].insert().values(**common, id='reader', issuer='synthetic',
            subject='reader', display_name='Synthetic reader', disabled=False))
        add_historical_admin(db, 'dept', 'reader')
        db.execute(metadata.tables['outbox'].insert().values(**common, id='event', department_id='dept', event_key='e',
            kind='review', payload={}, state='available_in_app', attempts=1, next_attempt_at=at))
        db.execute(metadata.tables['notifications'].insert(), [
            {**common, 'id': 'read', 'outbox_id': 'event', 'identity_id': 'reader', 'title': 'Read', 'message': 'Evidence', 'acknowledged_at': at}])
        db.execute(metadata.tables['identities'].insert().values(**common, id='other', issuer='synthetic',
            subject='other', display_name='Other reader', disabled=False))
        db.execute(metadata.tables['notifications'].insert().values(**common, id='unread', outbox_id='event',
            identity_id='other', title='Unread', message='Evidence', acknowledged_at=None))
    command.upgrade(config, 'head')
    after = MetaData()
    after.reflect(engine)
    with engine.connect() as db:
        rows = {r['id']: r for r in db.execute(select(after.tables['notifications'])).mappings()}
        assert set(rows) == {'read', 'unread'}
        assert rows['read']['read_at'] == rows['read']['acknowledged_at'] == at.replace(tzinfo=None)
        assert rows['unread']['read_at'] is None and rows['unread']['acknowledged_at'] is None
        assert all(r['revision'] == 1 and r['source_kind'] is None and r['source_id'] is None for r in rows.values())
        dept = db.execute(select(after.tables['departments'])).mappings().one()
        assert dept['announcement_send_time'] == '07:00' and dept['announcement_auto_reminder_days'] is None
    engine.dispose()

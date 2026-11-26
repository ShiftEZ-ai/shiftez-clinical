"""M2 adds event storage without changing announcement delivery or acknowledgement evidence."""
from datetime import date, datetime, timezone

import pytest
from alembic import command
from sqlalchemy import MetaData, select
from sqlalchemy.exc import IntegrityError

from apps.api.db import make_engine
from scripts.manage import alembic_config
from .migration_support import add_historical_admin


def test_m2_upgrade_preserves_m1_evidence_and_requires_exactly_one_receipt_source(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'm1-evidence.db')
    config = alembic_config(url)
    command.upgrade(config, '0080_announcement_receipts')
    engine = make_engine(url)
    before = MetaData()
    before.reflect(engine)
    moment = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
    common = {'revision': 7, 'created_at': moment}
    with engine.begin() as db:
        db.execute(before.tables['organisations'].insert().values(**common, id='org', name='Synthetic',
            timezone='UTC', week_start=0, input_revision=1, availability_revision=1))
        db.execute(before.tables['departments'].insert().values(**common, id='dept', organisation_id='org',
            name='Existing department', generation_defaults={}))
        db.execute(before.tables['identities'].insert().values(**common, id='reader', issuer='synthetic',
            subject='reader', display_name='Synthetic reader', disabled=False))
        add_historical_admin(db, 'dept', 'reader')
        db.execute(before.tables['identities'].insert().values(**common, id='other-reader', issuer='synthetic',
            subject='other-reader', display_name='Other synthetic reader', disabled=False))
        db.execute(before.tables['department_content'].insert().values(**common, id='notice', department_id='dept',
            name='Existing notice', description='Preserved agenda', starts_on=date(2020, 1, 1), active=True,
            kind='announcement', url='', audience={'everyone': True}, importance='must_acknowledge'))
        db.execute(before.tables['announcement_receipts'].insert().values(**common, id='receipt',
            content_id='notice', identity_id='reader', delivered_at=moment, read_at=moment, acknowledged_at=moment))
        db.execute(before.tables['outbox'].insert().values(**common, id='outbox', department_id='dept', event_key='e',
            kind='announcement', payload={}, state='available_in_app', attempts=1, next_attempt_at=moment))
        db.execute(before.tables['notifications'].insert().values(**common, id='notification', outbox_id='outbox',
            identity_id='reader', title='Preserved notice', message='Preserved message', source_kind='department_content',
            source_id='notice', read_at=moment, acknowledged_at=moment))
        receipt_before = dict(db.execute(select(before.tables['announcement_receipts'])).mappings().one())
        notification_before = dict(db.execute(select(before.tables['notifications'])).mappings().one())
        content_before = dict(db.execute(select(before.tables['department_content'])).mappings().one())
    command.upgrade(config, '0082_events')
    after = MetaData()
    after.reflect(engine)
    with engine.begin() as db:
        receipt = dict(db.execute(select(after.tables['announcement_receipts'])).mappings().one())
        assert {key: receipt[key] for key in receipt_before} == receipt_before
        assert receipt['event_occurrence_id'] is None and receipt['response'] is None and receipt['responded_at'] is None
        assert dict(db.execute(select(after.tables['notifications'])).mappings().one()) == notification_before
        assert dict(db.execute(select(after.tables['department_content'])).mappings().one()) == content_before
        department = db.execute(select(after.tables['departments'])).mappings().one()
        assert department['event_horizon_days'] == 365 and department['event_receipt_window_days'] == 14
        assert list(db.execute(select(after.tables['event_types']))) == []  # no seeding of existing departments
        db.execute(after.tables['event_types'].insert().values(**common, id='type', department_id='dept',
            name='Synthetic type', starts_on=date(2020, 1, 1)))
        db.execute(after.tables['events'].insert().values(**common, id='event', department_id='dept',
            event_type_id='type', heading='Synthetic event', start=moment, end=datetime(2026, 10, 1, 9, tzinfo=timezone.utc)))
        db.execute(after.tables['event_occurrences'].insert().values(**common, id='occurrence', department_id='dept',
            event_id='event', original_start=moment, start=moment, end=datetime(2026, 10, 1, 9, tzinfo=timezone.utc), data={}))
        db.execute(after.tables['announcement_receipts'].insert().values(**common, id='event-receipt',
            event_occurrence_id='occurrence', identity_id='reader', response='going', responded_at=moment))
        db.execute(after.tables['tags'].insert().values(**common, id='tag', department_id='dept', name='CME',
            colour='', description='', labels=['events', 'department_content'], active=True))
        db.execute(after.tables['tag_members'].insert(), [{**common, 'id': 'tag-event', 'department_id': 'dept',
            'tag_id': 'tag', 'kind': 'events', 'member_id': 'event'}, {**common, 'id': 'tag-notice', 'department_id': 'dept',
            'tag_id': 'tag', 'kind': 'department_content', 'member_id': 'notice'}])
    receipt_table = after.tables['announcement_receipts']
    invalid = [
        {'content_id': None, 'event_occurrence_id': None},
        {'content_id': 'notice', 'event_occurrence_id': 'occurrence'},
        {'event_occurrence_id': 'occurrence'},  # unique occurrence/identity
        {'content_id': 'notice'},  # existing unique announcement/identity
        {'event_occurrence_id': 'occurrence', 'response': 'maybe', 'identity_id': 'other-reader'},
    ]
    for index, values in enumerate(invalid):
        with pytest.raises(IntegrityError), engine.begin() as db:
            db.execute(receipt_table.insert().values(**common, id=f'invalid-{index}', **{'identity_id': 'reader', **values}))
    retained_tables = ('department_content', 'events', 'event_occurrences', 'announcement_receipts', 'notifications', 'tag_members')
    with engine.connect() as db:
        saved = {name: list(db.execute(select(after.tables[name]).order_by(after.tables[name].c.id)).mappings())
            for name in retained_tables}
    # A database already stamped with M2 must still receive the later M1 bookkeeping addition.
    command.upgrade(config, 'head')
    current = MetaData()
    current.reflect(engine)
    with engine.connect() as db:
        for name, previous_rows in saved.items():
            rows = list(db.execute(select(current.tables[name]).order_by(current.tables[name].c.id)).mappings())
            assert len(rows) == len(previous_rows)
            for previous, row in zip(previous_rows, rows):
                assert {key: row[key] for key in previous} == dict(previous)
        assert db.execute(select(current.tables['department_content'].c.reminder_empty_checked_on)).scalar_one() is None
    engine.dispose()

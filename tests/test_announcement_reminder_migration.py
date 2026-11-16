"""Empty reminder checks add bookkeeping without changing prior receipt evidence."""
from datetime import date, datetime, timezone

from alembic import command
from sqlalchemy import MetaData, select

from apps.api.db import make_engine
from scripts.manage import alembic_config


def test_empty_reminder_upgrade_keeps_content_and_receipts(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'reminder-upgrade.db')
    config = alembic_config(url)
    command.upgrade(config, '0080_announcement_receipts')
    engine = make_engine(url)
    before = MetaData()
    before.reflect(engine)
    moment = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
    common = {'revision': 1, 'created_at': moment}
    with engine.begin() as db:
        db.execute(before.tables['organisations'].insert().values(**common, id='org', name='Synthetic',
            timezone='UTC', week_start=0, input_revision=1, availability_revision=1))
        db.execute(before.tables['departments'].insert().values(**common, id='dept', organisation_id='org',
            name='Synthetic', generation_defaults={}))
        db.execute(before.tables['identities'].insert().values(**common, id='reader', issuer='synthetic',
            subject='reader', display_name='Synthetic reader', disabled=False))
        db.execute(before.tables['department_content'].insert().values(**common, id='notice', department_id='dept',
            name='Synthetic notice', kind='announcement', description='Keep evidence', url='', active=True,
            starts_on=date(2020, 1, 1), audience={'everyone': True}, importance='must_acknowledge',
            receipts_checked_on=date(2026, 10, 1), last_reminded_at=moment))
        db.execute(before.tables['announcement_receipts'].insert().values(**common, id='receipt', content_id='notice',
            identity_id='reader', delivered_at=moment, read_at=moment))
        expected = {name: dict(db.execute(select(before.tables[name])).mappings().one())
            for name in ('department_content', 'announcement_receipts')}
    command.upgrade(config, 'head')
    after = MetaData()
    after.reflect(engine)
    with engine.connect() as db:
        for name, saved in expected.items():
            current = db.execute(select(after.tables[name])).mappings().one()
            assert {key: current[key] for key in saved} == saved
        assert db.execute(select(after.tables['department_content'].c.reminder_empty_checked_on)).scalar_one() is None
    engine.dispose()

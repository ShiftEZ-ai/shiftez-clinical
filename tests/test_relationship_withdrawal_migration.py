"""Withdrawal state expands the schema without rewriting existing dated records."""
import importlib
from datetime import date

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_withdrawal_migration_preserves_rows_dates_and_server_defaults():
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    tables = [sa.Table(name, metadata, sa.Column('id', sa.String, primary_key=True),
                       sa.Column('starts_on', sa.Date, nullable=False), sa.Column('ends_on', sa.Date),
                       sa.CheckConstraint('ends_on IS NULL OR ends_on > starts_on'))
              for name in ('memberships', 'eligibilities', 'programme_memberships', 'rotations')]
    try:
        with engine.begin() as db:
            metadata.create_all(db)
            for table in tables:
                db.execute(table.insert(), [
                    {'id': 'past', 'starts_on': date(2020, 1, 1), 'ends_on': date(2020, 1, 2)},
                    {'id': 'future', 'starts_on': date(2028, 2, 1), 'ends_on': date(2028, 2, 2)},
                    {'id': 'open', 'starts_on': date(2030, 1, 1), 'ends_on': None}])
            before = {table.name: list(db.execute(table.select().order_by(table.c.id))) for table in tables}
            migration = importlib.import_module('migrations.versions.0147_relationship_withdrawal')
            with Operations.context(MigrationContext.configure(db)):
                migration.upgrade()
            for table in tables:
                assert list(db.execute(table.select().order_by(table.c.id))) == before[table.name]
                expanded = sa.Table(table.name, sa.MetaData(), autoload_with=db)
                assert list(db.scalars(sa.select(expanded.c.withdrawn))) == [False] * 3
                db.execute(expanded.insert(), {'id': 'new', 'starts_on': date(2031, 1, 1)})
                assert db.scalar(sa.select(expanded.c.withdrawn).where(expanded.c.id == 'new')) is False
    finally:
        engine.dispose()

import importlib
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from migrations import safety


def test_additive_migration_preserves_existing_memberships_with_true_default():
    engine = sa.create_engine('sqlite://')
    try:
        with engine.begin() as db:
            db.execute(sa.text('CREATE TABLE departments (id TEXT PRIMARY KEY)'))
            db.execute(sa.text('CREATE TABLE memberships (id TEXT PRIMARY KEY, person_id TEXT, '
                               'category_id TEXT, starts_on DATE, ends_on DATE)'))
            db.execute(sa.text("INSERT INTO departments VALUES ('synthetic-department')"))
            db.execute(sa.text('INSERT INTO memberships VALUES (:id,:person,:category,:first,:last)'), [
                {'id': 'past', 'person': 'person', 'category': 'old-rank', 'first': '2020-01-01', 'last': '2021-01-01'},
                {'id': 'current', 'person': 'person', 'category': 'new-rank', 'first': '2021-01-01', 'last': None},
                {'id': 'future', 'person': 'another-person', 'category': 'rank', 'first': '2030-01-01', 'last': None}])
            before = list(db.execute(sa.text('SELECT * FROM memberships ORDER BY id')))
            migration = importlib.import_module('migrations.versions.0146_can_take_duty')
            with Operations.context(MigrationContext.configure(db)):
                migration.upgrade()
            assert 'takes_duties' in {column['name'] for column in sa.inspect(db).get_columns('memberships')}
            assert list(db.execute(sa.text('SELECT id,person_id,category_id,starts_on,ends_on '
                                           'FROM memberships ORDER BY id'))) == before
            assert list(db.execute(sa.text('SELECT takes_duties FROM memberships'))) == [(True,)] * 3
            assert db.scalar(sa.text('SELECT new_members_take_duties FROM departments')) == 0
            db.execute(sa.text("INSERT INTO memberships (id) VALUES ('server-default')"))
            assert db.scalar(sa.text("SELECT takes_duties FROM memberships WHERE id='server-default'")) == 1
    finally:
        engine.dispose()


def test_duty_taking_migration_is_additive_without_destructive_confirmation():
    path = Path(__file__).resolve().parents[1] / 'migrations/versions/0146_can_take_duty.py'
    assert safety.analyze(path)['class'] == 'additive'
    assert safety.classification(path) == 'additive'
    assert not safety.declaration_errors(path)
    assert not safety.needs_confirmation(path)

"""Seed historical schemas directly, or bridge current ORM fixtures while preserving the old schema."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateColumn

from apps.api import models as m
from packages.contracts.schemas import ROLE_NAMES


def historical_seed(db):
    connection = db if isinstance(db, Connection) else db.connection()
    metadata = sa.MetaData()
    base = {'revision': 1, 'created_at': datetime(2026, 1, 1, tzinfo=timezone.utc)}

    def insert(table_name, **values):
        table = sa.Table(table_name, metadata, autoload_with=connection)
        key = str(uuid4())
        connection.execute(table.insert(), {**base, 'id': key, **values})
        return key

    organisation = insert('organisations', name='Historical synthetic organisation', timezone='UTC',
                          week_start=0, input_revision=1, availability_revision=1)
    department = insert('departments', organisation_id=organisation, name='Historical synthetic department', generation_defaults={})
    people = [insert('people', organisation_id=organisation, display_name=f'Historical person {number} (test)',
                     active=True, title='', work_phone='', work_extension='', staff_number='', directory_section='', directory_order=1000)
              for number in range(3)]
    identities = {}
    for role in ROLE_NAMES:
        identities[role] = insert('identities', issuer='historical-test', subject=role, display_name=f'Historical {role} (test)',
                                  disabled=False, person_id=people[0] if role == 'staff' else None)
        insert('role_bindings', identity_id=identities[role], department_id=department, role=role,
               starts_on=date(2020, 1, 1), revoked=False)
    effective = {'department_id': department, 'starts_on': date(2020, 1, 1), 'active': True, 'description': ''}
    service = insert('services', name='Historical synthetic service', **effective)
    duty_type = insert('assignment_types', name='Historical synthetic duty', definition={}, **effective)
    return {'organisation_id': organisation, 'department_id': department, 'people': people,
            'identities': identities, 'services': [service], 'type': duty_type}


@contextmanager
def current_seed_columns(engine):
    """Temporarily bridge only models used by demo_data; restore the exact old schema before upgrading."""
    if engine.dialect.name != 'sqlite':
        raise ValueError('Historical seed columns are only for disposable SQLite test fixtures')
    tables = (m.Person.__table__, m.Department.__table__, m.Category.__table__, m.DefinitionHistory.__table__)
    quote = engine.dialect.identifier_preparer.quote
    guards = "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE type IN ('index', 'trigger', 'view') ORDER BY type, name"
    added = []
    with engine.connect() as db:
        inspector = inspect(db)
        original = {table.name: [tuple(row) for row in db.exec_driver_sql(f'PRAGMA table_info({quote(table.name)})')]
                    for table in tables}
        constraints = {table.name: (inspector.get_check_constraints(table.name), inspector.get_foreign_keys(table.name))
                       for table in tables}
        revision = list(db.exec_driver_sql('SELECT version_num FROM alembic_version'))
        guard_definitions = list(db.exec_driver_sql(guards))
        foreign_keys_enabled = db.exec_driver_sql('PRAGMA foreign_keys').scalar_one()
    try:
        with engine.begin() as db:
            for table in tables:
                present = {row[1] for row in original[table.name]}
                for column in table.columns:
                    if column.name not in present:
                        definition = str(CreateColumn(column).compile(dialect=engine.dialect))
                        db.exec_driver_sql(f'ALTER TABLE {quote(table.name)} ADD COLUMN {definition}')
                        added.append((table.name, column.name))
        yield
    finally:
        with engine.begin() as db:
            for table, column in reversed(added):
                present = {row[1] for row in db.exec_driver_sql(f'PRAGMA table_info({quote(table)})')}
                if column in present:
                    db.exec_driver_sql(f'ALTER TABLE {quote(table)} DROP COLUMN {quote(column)}')
            assert {table.name: [tuple(row) for row in db.exec_driver_sql(f'PRAGMA table_info({quote(table.name)})')]
                    for table in tables} == original
            inspector = inspect(db)
            assert {table.name: (inspector.get_check_constraints(table.name), inspector.get_foreign_keys(table.name))
                    for table in tables} == constraints
            assert list(db.exec_driver_sql('SELECT version_num FROM alembic_version')) == revision
            assert list(db.exec_driver_sql(guards)) == guard_definitions
            assert db.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == foreign_keys_enabled

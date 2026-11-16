"""Imported evidence is append-only on both supported databases."""
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from apps.api import models as m
from apps.api.db import transaction


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_source_evidence_cannot_be_rewritten(request, database):
    harness = request.getfixturevalue(database)
    engine = harness.app.state.engine
    with transaction(engine, write=True) as db:
        department = db.scalar(select(m.Department.id))
        db.add(m.ReferenceImport(department_id=department, source_name='synthetic',
                                archive_sha256='a'*64, manifest={'original': True}))
        db.add(m.SourceMapping(department_id=department, source_name='synthetic',
                              source_key='person:one', target_kind='person', target_id='original'))
    for table, column, value in [('reference_imports', 'archive_sha256', 'b'*64),
                                 ('source_mappings', 'target_id', 'forged')]:
        for statement in [f'UPDATE {table} SET {column}=:value', f'DELETE FROM {table}']:
            with pytest.raises(DBAPIError, match='immutable evidence'), engine.begin() as db:
                db.execute(text(statement), {'value': value})
        with engine.connect() as db:
            assert db.scalar(text(f'SELECT COUNT(*) FROM {table}')) == 1

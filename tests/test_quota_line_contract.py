"""The quota contract upgrade rolls back both guards and the version together."""
from datetime import date, datetime, timezone

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, event, text
from sqlalchemy.engine import Engine

from apps.api import models as m
from apps.api.db import make_engine, transaction
from scripts.manage import alembic_config, migrate

from .migration_support import historical_seed


PRIOR = '0059_rule_acknowledgements'


@pytest.fixture
def legacy_quota_db(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'quota-v5.db')
    command.upgrade(alembic_config(url), PRIOR)
    engine = make_engine(url)
    try:
        with transaction(engine, write=True) as db:
            seed = historical_seed(db)
            actor = seed['identities']['coordinator']
            metadata = MetaData()
            workspaces = Table('workspaces', metadata, autoload_with=db.connection())
            publications = Table('publications', metadata, autoload_with=db.connection())
            common = {'department_id': seed['department_id'], 'revision': 1,
                      'created_at': datetime(2026, 10, 1, tzinfo=timezone.utc),
                      'starts_on': date(2026, 10, 4), 'ends_on': date(2026, 10, 11)}
            workspace_id = m.uid()
            db.execute(workspaces.insert(), {**common, 'id': workspace_id, 'name': 'Issued v5 evidence',
                'status': 'published', 'content_revision': 1, 'created_by': actor, 'last_edited_by': actor})
            report = {'metric_version': 'block-quota-v5', 'rows': [{'person_id': seed['people'][0], 'actual': 2}]}
            db.execute(publications.insert(), {**common, 'id': m.uid(), 'workspace_id': workspace_id,
                'sequence': 1, 'current': True, 'published_by': actor,
                'snapshot': {'shifts': [], 'historical_marker': 'keep verbatim'},
                'validation': {'valid': True, 'contracts': {'block_quota': 'block-quota-v5', 'programmes': 'programmes-v1'},
                               'quota_report': report}, 'fingerprint': '0' * 64})
    finally:
        engine.dispose()
    return url


@pytest.mark.parametrize('fail_after', ['DROP TRIGGER publications_validation_contract',
                                       'DROP TRIGGER publications_alpha_contract', 'UPDATE alembic_version'])
def test_failed_guard_replacement_rolls_back_both_guards_and_version(legacy_quota_db, fail_after):
    engine = make_engine(legacy_quota_db)
    guards = text("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name IN "
                  "('publications_validation_contract', 'publications_alpha_contract') ORDER BY name")
    try:
        with engine.connect() as db:
            before_guards = list(db.execute(guards))
            before_rows = list(db.execute(text('SELECT * FROM publications')))
        assert len(before_guards) == 2
        reached = []

        def fail_after_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.startswith(fail_after):
                reached.append(statement)
                raise RuntimeError('Injected quota migration failure')

        event.listen(Engine, 'after_cursor_execute', fail_after_statement)
        try:
            with pytest.raises(RuntimeError, match='Injected quota migration failure'):
                migrate(legacy_quota_db)
        finally:
            event.remove(Engine, 'after_cursor_execute', fail_after_statement)
        assert reached
        with engine.connect() as db:
            assert list(db.execute(guards)) == before_guards
            assert db.scalar(text('SELECT version_num FROM alembic_version')) == PRIOR
            assert list(db.execute(text('SELECT * FROM publications'))) == before_rows
    finally:
        engine.dispose()

"""Planner statistics settings are applied and reversible on PostgreSQL."""
from importlib import import_module

from alembic import command
import pytest
from sqlalchemy import text

from apps.api.db import make_engine
from scripts.manage import alembic_config

from .test_email_migration import migration_url  # noqa: F401


# Autovacuum analyze settings exist only on PostgreSQL; the SQLite upgrade leaves no table options.
@pytest.mark.parametrize('migration_url', [pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_bulk_analyze_settings_at_head(migration_url):
    engine = make_engine(migration_url)
    try:
        migration = import_module('migrations.versions.0148_bulk_analyze')
        config = alembic_config(migration_url)
        query = text("""
            SELECT c.relname, c.reloptions
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relkind = 'r'
        """)

        def reloptions():
            with engine.connect() as db:
                return {name: set(options or ()) for name, options in db.execute(query)}

        command.upgrade(config, migration.down_revision)
        before = reloptions()

        settings = {'autovacuum_analyze_scale_factor=0.02', 'autovacuum_analyze_threshold=50'}
        assert migration.TABLES
        assert set(migration.TABLES) <= before.keys()
        assert all(before[name].isdisjoint(settings) for name in migration.TABLES)
        expected = {**before, **{name: before[name] | settings for name in migration.TABLES}}

        command.upgrade(config, migration.revision)
        assert reloptions() == expected

        command.downgrade(config, migration.down_revision)
        assert reloptions() == before

        command.upgrade(config, 'head')
        at_head = reloptions()
        assert all(settings <= at_head[name] for name in migration.TABLES)
    finally:
        engine.dispose()

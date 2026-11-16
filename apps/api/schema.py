"""Read the one supported schema head from the packaged migration graph.

This avoids a second hand-maintained revision constant drifting after a migration.
Release files are immutable for a running process; deploy/restart with new code.
"""
from functools import lru_cache
from pathlib import Path
from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATIONS = Path(__file__).resolve().parents[2] / 'migrations'


@lru_cache(maxsize=4)
def supported_revision(migrations: Path = MIGRATIONS) -> str:
    config = Config()
    config.set_main_option('script_location', str(migrations.resolve()))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise RuntimeError('The packaged release must have exactly one supported migration head')
    return heads[0]


def require_current_schema(db, revision=None):
    """Fail closed before operational writes on an uninitialised/incompatible DB."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    from .errors import Problem, require
    try:
        versions = list(db.scalars(text("SELECT version_num FROM alembic_version")))
    except SQLAlchemyError as exc:
        raise Problem(503, "DATABASE_NOT_READY", "Database schema is unavailable; no operational change was committed") from exc
    require(versions == [revision or supported_revision()], "MIGRATION_REQUIRED",
            "Database is not at the supported migration revision; no operational change was committed", 503)

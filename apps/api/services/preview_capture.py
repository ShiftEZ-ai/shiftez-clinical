"""Reader phases for rollback-only previews never commit speculative writes."""
from contextlib import contextmanager
from sqlalchemy import text
from apps.api.db import transaction


class RollbackPreview(Exception):
    pass


@contextmanager
def rollback_reader(engine):
    reader = engine.execution_options(isolation_level='REPEATABLE READ') if engine.dialect.name == 'postgresql' else engine
    try:
        with transaction(reader) as db:
            if engine.dialect.name == 'sqlite':
                db.execute(text('BEGIN'))
            yield db
            raise RollbackPreview()
    except RollbackPreview:
        pass

"""Fixed-window request limits shared by every worker through the database.

Each subject costs one upsert in a short transaction: failed and rejected
requests still count, and no row lock is held while the request runs.
The first hit of a window also prunes expired counters through the expires_at index.
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from . import models as m
from .auth import digest
from .db import liveness_write
from .errors import Problem


def counter_key(scope: str, subject: str, window: int, moment):
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = moment - epoch
    start = (elapsed.days * 86400 + elapsed.seconds) // window * window
    ends = epoch + min(timedelta(seconds=start + window), datetime.max.replace(tzinfo=timezone.utc) - epoch)
    return digest(f"{scope}:{subject}:{start}"), ends


def hit(conn, scope: str, subject: str, window: int, moment):
    """Increment one shared window counter inside the caller's transaction."""
    key, ends = counter_key(scope, subject, window, moment)
    insert = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
    row = insert(m.RateLimit).values(key_hash=key, hits=1, expires_at=ends)
    hits = conn.execute(row.on_conflict_do_update(index_elements=["key_hash"], set_={"hits": m.RateLimit.hits + 1})
                        .returning(m.RateLimit.hits)).scalar_one()
    if hits == 1:
        conn.execute(delete(m.RateLimit).where(m.RateLimit.expires_at < moment))
    return hits, ends


def enforce(engine, scope: str, subjects, limit: int, window: int, message: str):
    """Raise 429 once any subject exceeds `limit` hits in the current `window` seconds; limit <= 0 disables."""
    subjects = [s for s in subjects if s]
    if limit <= 0 or not subjects:
        return
    def count(conn):
        moment = m.now()
        for subject in subjects:
            hits, ends = hit(conn, scope, subject, window, moment)
            if hits > limit:
                wait = max(1, int((ends - moment).total_seconds() + 0.999))
                return Problem(429, "RATE_LIMITED", message, {"retry_after": wait})

    # The upsert locks only this counter row; it never needs the rota advisory writer.
    problem = liveness_write(engine, lambda db: count(db.connection()),
                             context={"rate_limit_scope": scope})
    if problem:
        raise problem  # Commit every counted subject before refusing the request.


def session_identity(engine, token: str | None) -> str | None:
    """Identity behind a session cookie, so all of one person's sessions share a limit. Bad tokens fail later in authentication."""
    if not token:
        return None
    with engine.connect() as conn:
        return conn.scalar(select(m.Session.identity_id).where(m.Session.token_hash == digest(token)))

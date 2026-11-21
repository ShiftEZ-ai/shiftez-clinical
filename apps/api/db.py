"""One write-serialization contract for ALL dependency-changing commands.

PostgreSQL: transaction-level application advisory lock taken before any reads.
READ COMMITTED + a shared lock across all application writers provides a coherent
final check/commit boundary. This intentionally trades write throughput for a simple
single-institution invariant. Direct operator SQL must be done in maintenance mode.
SQLite: BEGIN IMMEDIATE is a tested local-development analogue, not production.
"""
from contextlib import closing, contextmanager, nullcontext
from contextvars import ContextVar
from functools import cache
import logging
import os
from pathlib import Path
from threading import Lock, get_native_id
import time
from sqlalchemy import and_, create_engine, event, exists, inspect, or_, select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError, TimeoutError as PoolTimeout
from sqlalchemy.orm import Session, with_loader_criteria
from .errors import Problem
from .settings import Settings

# Security/integrity primitive, not a departmental operational setting.
WRITE_LOCK_KEY = 704391602
log = logging.getLogger("rota.db")

# Set by the HTTP middleware for the lifetime of one request.  SQLAlchemy's
# event hooks run in the same context as sync FastAPI handlers, so this keeps
# DB time out of request handlers and never carries SQL text or parameters.
request_db_time_ms: ContextVar[dict | None] = ContextVar("request_db_time_ms", default=None)
_lock_diagnostic_guard = Lock()
_lock_diagnostic_last_at = None
_lock_diagnostic_suppressed = 0


@event.listens_for(Session, 'do_orm_execute')
def filter_withdrawn_relationships(state):
    """Live reads exclude withdrawals, including joins/counts; history and Undo opt in explicitly."""
    # shortcut: can_take_duty/takes_duties is absent here; maintenance must test withdrawn-row exclusion when it lands.
    if state.is_select and not state.execution_options.get('include_withdrawn'):
        from . import models as m
        state.statement = state.statement.options(with_loader_criteria(
            m.Withdrawable, lambda cls: cls.withdrawn.is_(False), include_aliases=True))
        state.statement = state.statement.options(with_loader_criteria(
            m.Mentorship, live_mentorship_clause(), include_aliases=True))


# Reuse the SQL predicate and evaluate current rows on every read.
@cache
def live_mentorship_clause():
    """Owned attachments follow live rotations; adopted or independently edited mentorships stay live."""
    from . import models as m
    links, rotations = m.RotationAttachment.__table__.alias(), m.Rotation.__table__.alias()
    owned = exists(select(1).where(links.c.mentorship_id == m.Mentorship.id,
                   links.c.synced_revision == m.Mentorship.revision)).correlate_except(links)
    active = exists(select(1).select_from(links.join(rotations, rotations.c.id == links.c.rotation_id)).where(
                    links.c.mentorship_id == m.Mentorship.id, rotations.c.withdrawn.is_(False))).correlate_except(links, rotations)
    return or_(~owned, active)


@event.listens_for(Session, 'loaded_as_persistent')
def retain_request_parent(session, row):
    # Session.get uses a weak identity map. Keep these already-loaded rows alive.
    parents = session.info.get('_request_parents')
    if parents is not None:
        from .models import Department, Organisation
        if isinstance(row, (Department, Organisation)):
            parents[type(row), row.id] = row


def current_request_parent(db, model, ident):
    """Preserve the original read point for settings used in an authority decision."""
    if '_request_authority' not in db.info:
        db.info.get('_request_parents', {}).pop((model, ident), None)
    return db.get(model, ident)


def _invalidate_snapshot_input_caches(session, organisation_ids=None):
    cache = session.info.get('_live_safety_sources', {})
    for key in list(cache):
        if organisation_ids is None or key[0] in organisation_ids:
            del cache[key]
    preparation = session.info.get('_planning_preparation', {})
    preparation.get('snapshots', {}).clear()
    for name in ('inputs', 'input_tokens'):
        memo = preparation.get(name, {})
        for key in list(memo):
            if organisation_ids is None or key in organisation_ids:
                del memo[key]
    session.info.pop('write_domain_snapshots', None)
    session.info.pop('_activation_projection_cache', None)
    session.info.pop('_baseline_hints', None)


def _snapshot_stamp_state(session):
    return session.info.setdefault('_snapshot_input_revisions', {
        'general': set(), 'availability': set(), 'savepoints': {}, 'organisations': {}})


def bump_snapshot_input_revisions(session, organisation_ids=(), *, availability=False):
    """One revision authority for flush and session DML, once per transaction.

    Caches are cleared on EVERY mutation, including a later flush after this
    transaction already advanced the counters.
    """
    from . import models as m
    ids = set(organisation_ids)
    _invalidate_snapshot_input_caches(session, ids)
    state = _snapshot_stamp_state(session)
    pending = {row.id: row for row in session.new if isinstance(row, m.Organisation)}
    for organisation_id in ids:
        organisation = pending.get(organisation_id) or session.get(m.Organisation, organisation_id)
        if organisation is None:
            raise ValueError(f'Unresolved snapshot input Organisation id={organisation_id}')
        state['organisations'][organisation_id] = organisation
        if organisation_id not in state['general']:
            organisation.input_revision = (organisation.input_revision or 0) + 1
            state['general'].add(organisation_id)
        if availability and organisation_id not in state['availability']:
            organisation.availability_revision = (organisation.availability_revision or 0) + 1
            state['availability'].add(organisation_id)


def _snapshot_input_parent_paths(cls):
    from . import models as m
    if issubclass(cls, m.RotationAttachment):
        return [('mentorship_id', m.Mentorship)]
    if issubclass(cls, m.AnnouncementReceipt):
        return [('event_occurrence_id', m.EventOccurrence), ('content_id', m.DepartmentContent)]
    if issubclass(cls, m.Identity):
        return [('person_id', m.Person)]
    return [('department_id', m.Department)]


def _notice_receipt(row):
    """An announcement's receipt (delivered, read, acknowledged) is no scheduling input; only an event's reply is.
    A person's first read of a notice moved the availability revision, and every open page read everything again."""
    from . import models as m
    return isinstance(row, m.AnnouncementReceipt) and row.content_id is not None


def _snapshot_input_scope_columns(cls):
    from . import models as m
    return ({'id'} if cls is m.Organisation else {'organisation_id'} if hasattr(cls, 'organisation_id')
            else {name for name, _parent in _snapshot_input_parent_paths(cls)})


def _snapshot_input_organisation_ids(session, row, related=None):
    """Resolve current AND previous ownership, retaining parents for this batch."""
    from . import models as m
    related = {} if related is None else related
    def get(cls, ident):
        key = (cls, ident)
        if key not in related:
            related[key] = next((r for r in session.new if isinstance(r, cls) and r.id == ident), None)
            if related[key] is None:
                related[key] = session.get(cls, ident, execution_options={'include_withdrawn': True})
            if cls is m.Organisation and related[key] is not None:
                # The identity map is weak; one import flush per row must not reload this parent.
                _snapshot_stamp_state(session)['organisations'][ident] = related[key]
        return related[key]
    def values(name):
        current = getattr(row, name, None)
        state = inspect(row)
        history = state.attrs[name].history if name in state.attrs else None
        previous = history.deleted if history is not None else ()
        if history is not None and history.has_changes() and state.persistent and not previous:
            # An expired/deferred ownership column has no old value in ORM history.
            mapper = inspect(type(row))
            identity = [column == getattr(row, column.key) for column in mapper.primary_key]
            previous = (session.scalar(select(getattr(type(row), name)).where(*identity)
                                       .execution_options(include_withdrawn=True)),)
        return {value for value in (current, *previous) if value is not None}
    if isinstance(row, m.Organisation):
        return {row.id} if row.id is not None else None
    if hasattr(row, 'organisation_id'):
        ids = values('organisation_id')
        return ids if ids and all(get(m.Organisation, ident) is not None for ident in ids) else None
    ids = set()
    for name, cls in _snapshot_input_parent_paths(type(row)):
        for ident in values(name):
            parent = get(cls, ident)
            if parent is None:
                return None
            resolved = _snapshot_input_organisation_ids(session, parent, related)
            if resolved is None:
                return None
            ids.update(resolved)
    # An identity without an old or new Person link cannot participate in protected_facts.
    return ids if ids or isinstance(row, m.Identity) else None


def _unresolved_snapshot_input(session, row, related):
    from . import models as m
    message = f'Unresolved snapshot input {type(row).__name__} id={getattr(row, "id", None)}'
    if os.getenv('ROTA_ENV') == 'test':
        raise ValueError(message)
    log.error(message)
    touched = set(_snapshot_stamp_state(session)['general'])
    for known in (*session.identity_map.values(), *session.new, *session.deleted):
        touched.update(_snapshot_input_organisation_ids(session, known, related) or ())
    return touched or set(session.scalars(select(m.Organisation.id)))


def snapshot_input_listener_classes():
    """Expose the canonical class/category map for structural guards."""
    from .snapshots import snapshot_input_specs
    return {cls: category for _name, cls, category, *_ in snapshot_input_specs()}


def _snapshot_attribute_changes(name, old, new, exclusions):
    if name in exclusions:
        return False
    keys = {key.split('.', 1)[1] for key in exclusions if key.startswith(name + '.')}
    if keys and isinstance(old, (dict, type(None))) and isinstance(new, (dict, type(None))):
        return ({key: value for key, value in (old or {}).items() if key not in keys}
                != {key: value for key, value in (new or {}).items() if key not in keys})
    return True


@event.listens_for(Session, 'before_flush')
def stamp_snapshot_inputs(session, _flush_context, _instances):
    """Every registry mutation stamps unless all changed attributes are excluded."""
    from . import models as m
    from .snapshots import snapshot_input_specs
    from sqlalchemy.orm.attributes import NO_VALUE
    specs = {cls: (category, exclusions) for _name, cls, category, _scope, _shape, exclusions in snapshot_input_specs()}
    general, availability, related = set(), set(), {}
    for row in (*session.new, *session.dirty, *session.deleted):
        if isinstance(row, (m.Department, m.Organisation, m.RoleBinding, m.SelfApprovalPermission)):
            session.info.get('_request_parents', {}).clear()
            session.info.get('_request_authority', {}).clear()
        spec = specs.get(type(row))
        if spec is None:
            continue
        # A proposal does not change operative inputs or invalidate its own conversion proof.
        if isinstance(row, m.CatalogueConversion) and row in session.new and row.status == 'proposed':
            continue
        if _notice_receipt(row):
            continue
        category, exclusions = spec
        state = inspect(row)
        if row in session.dirty and row not in session.deleted:
            if not any(attribute.history.has_changes() and _snapshot_attribute_changes(
                    attribute.key, attribute.history.deleted[-1] if attribute.history.deleted else NO_VALUE,
                    getattr(row, attribute.key), exclusions) for attribute in state.attrs):
                continue
        if isinstance(row, m.Organisation) and row in session.new and row.id is None:
            row.id = m.uid()
        ids = _snapshot_input_organisation_ids(session, row, related)
        if ids is None:
            ids = _unresolved_snapshot_input(session, row, related)
        (availability if category == 'availability' else general).update(ids)
    if general:
        bump_snapshot_input_revisions(session, general)
    if availability:
        bump_snapshot_input_revisions(session, availability, availability=True)


@event.listens_for(Session, 'do_orm_execute', retval=True)
def stamp_snapshot_input_statement(execution):
    """Session DML bypasses before_flush; direct Connection DML is guarded in CI.

    SQLAlchemy 2.1 documents these disjoint event paths in orm/events.html and
    orm/queryguide/dml.html. Session-executed Core tables are covered too.
    """
    if not (execution.is_insert or execution.is_update or execution.is_delete):
        return
    from .snapshots import snapshot_input_specs
    from sqlalchemy.sql.elements import BindParameter
    statement, session = execution.statement, execution.session
    table = getattr(statement, 'table', None)
    from .models import Department, Organisation, RoleBinding, SelfApprovalPermission
    if table is not None and getattr(table, 'name', None) in (
            Department.__tablename__, Organisation.__tablename__, RoleBinding.__tablename__,
            SelfApprovalPermission.__tablename__):
        session.info.get('_request_parents', {}).clear()
        session.info.get('_request_authority', {}).clear()
    spec = next((spec for spec in snapshot_input_specs() if spec[1].__tablename__ == getattr(table, 'name', None)), None)
    if spec is None:
        return
    _name, cls, category, _scope, _shape, exclusions = spec
    # These SQLAlchemy 2.1 DML value containers have no public inspection API.
    from sqlalchemy.orm.attributes import NO_VALUE
    values = {getattr(key, 'key', key): value for key, value in (
        getattr(statement, '_values', None) or dict(getattr(statement, '_ordered_values', None) or ())).items()}
    parameters = execution.parameters
    parameter_rows = parameters if isinstance(parameters, list) else [parameters or {}]
    by_id = {row['id']: row for row in parameter_rows if 'id' in row}
    def changed_values(row_parameters):
        return {name: row_parameters.get(value.key, value.value) if isinstance(value, BindParameter) else NO_VALUE
                for name, value in values.items()}
    if execution.is_update and values and set(values) <= exclusions:
        return
    if session.autoflush and execution.execution_options.get('autoflush', True):
        session.flush()
    ids, related = set(), {}
    with session.no_autoflush:
        if execution.is_insert:
            rows = parameters or []
            if isinstance(rows, dict):
                rows = [rows]
            if not rows and statement.select is not None:
                projected = {name: value for name, value in zip(statement._select_names, statement.select.selected_columns)
                             if name in _snapshot_input_scope_columns(cls)}
                if projected and all(isinstance(value, BindParameter) for value in projected.values()):
                    rows = [{name: value.value for name, value in projected.items()}]
                elif projected:
                    rows = [dict(row) for row in session.execute(statement.select.with_only_columns(
                        *(value.label(name) for name, value in projected.items())).distinct()
                        .execution_options(include_withdrawn=True), execution.parameters).mappings()]
                    if not rows:
                        return execution.invoke_statement()
            if not rows:
                rows = list(statement._multi_values[0]) if statement._multi_values else [{}]
            candidates = [cls(**{name: value for name, value in {**changed_values(row), **row}.items()
                                if name in inspect(cls).column_attrs and value is not NO_VALUE}) for row in rows]
        else:
            scope_columns = _snapshot_input_scope_columns(cls)
            query = (select(*(getattr(cls, name) for name in sorted(scope_columns))).distinct()
                     if execution.is_delete else select(cls))
            if statement.whereclause is not None:
                query = query.where(statement.whereclause)
            query_parameters = parameters
            if isinstance(parameters, list):
                primary_key = inspect(cls).primary_key
                identities = [
                    and_(*(column == row[column.key] for column in primary_key))
                    for row in parameter_rows
                    if all(column.key in row for column in primary_key)
                ]
                if identities:
                    query = query.where(or_(*identities))
                    query_parameters = None
            query = query.execution_options(include_withdrawn=True)
            candidates = ([cls(**dict(row)) for row in session.execute(query, query_parameters).mappings()]
                          if execution.is_delete else list(session.scalars(query, query_parameters)))
        loaded_deleted = ([row for row in session.identity_map.values() if isinstance(row, cls)]
                          if execution.is_delete and not execution.is_orm_statement else [])
        unresolved_ownership, updated_columns = [], {}
        for row in candidates:
            if _notice_receipt(row):
                continue
            if by_id:
                parameters_for_row = by_id.get(getattr(row, 'id', None), parameter_rows[0])
            else:
                primary_key = inspect(cls).primary_key
                parameters_for_row = next((values for values in parameter_rows
                    if all(values.get(column.key) == getattr(row, column.key) for column in primary_key)),
                    parameter_rows[0])
            changes = {} if execution.is_delete else changed_values(parameters_for_row)
            if execution.is_update:
                if not values:
                    changes = {name: value for name, value in parameters_for_row.items()
                               if name in inspect(cls).column_attrs and (name != 'id' or not execution.is_executemany)}
                updated_columns[id(row)] = list(changes)
                if changes and not any(_snapshot_attribute_changes(name, getattr(row, name), value, exclusions)
                                       for name, value in changes.items()):
                    continue
            resolved = _snapshot_input_organisation_ids(session, row, related)
            if resolved is None:
                resolved = _unresolved_snapshot_input(session, row, related)
            ids.update(resolved)
            if execution.is_update and changes:
                constants = {name: value for name, value in changes.items() if value is not NO_VALUE}
                if any(value is NO_VALUE and name in _snapshot_input_scope_columns(cls) for name, value in changes.items()):
                    ids.update(_unresolved_snapshot_input(session, row, related))
                    unresolved_ownership.append(row)
                replacement = cls(**{**{column.key: getattr(row, column.key) for column in inspect(cls).columns}, **constants})
                resolved = _snapshot_input_organisation_ids(session, replacement, related)
                ids.update(resolved if resolved is not None else _unresolved_snapshot_input(session, replacement, related))
        result = execution.invoke_statement()
        if getattr(result, 'rowcount', None) == 0:
            return result
        for row in unresolved_ownership:
            session.expire(row, list(_snapshot_input_scope_columns(cls)))
            resolved = _snapshot_input_organisation_ids(session, row, related)
            ids.update(resolved if resolved is not None else _unresolved_snapshot_input(session, row, related))
        if execution.is_update or execution.is_delete and not execution.is_orm_statement:
            for row in loaded_deleted if execution.is_delete else candidates:
                if inspect(row).persistent:
                    # ORM evaluation of explicit bind parameters can also retain a stale value.
                    session.expire(row, updated_columns.get(id(row)) or None)
        if ids:
            bump_snapshot_input_revisions(session, ids, availability=category == 'availability')
        return result


class DatabaseWriteTimeout(Problem):
    def __init__(self):
        super().__init__(503, "COMMAND_WRITE_TIMEOUT",
                         "The system is busy, so this change was not saved. Try again.")


class DatabaseWriteDeferred(Exception):
    """A background pass yielded an occupied writer; no operation has run."""


def _remaining(deadline):
    milliseconds = int((deadline - time.monotonic()) * 1000)
    if milliseconds <= 0:
        raise DatabaseWriteTimeout() from None
    return milliseconds


def is_transient_database_error(exc):
    """Return whether a database error can clear when PostgreSQL/SQLite recovers."""
    if not isinstance(exc, OperationalError):
        return False
    code = getattr(exc.orig, "sqlstate", None)
    detail = str(exc.orig).lower()
    return (code in {"40001", "40P01", "57P01", "57P02", "57P03"}
            or isinstance(code, str) and code.startswith("08")
            or any(marker in detail for marker in (
                "connection failed", "connection failure", "connection refused", "connection reset",
                "connection timed out", "connection timeout", "connection is closed", "could not connect",
                "cannot connect", "server closed the connection", "database system is in recovery mode",
                "database system is not yet accepting connections", "network is unreachable",
                "database is locked")))


def _write_timeouts(connection, cursor, state):
    settings = state["settings"]
    if state["serializing"]:
        remaining = _remaining(state["deadline"] if state["deadline"] is not None else state["lock_deadline"])
        limits = (remaining, remaining)
    else:
        remaining = _remaining(state["deadline"]) if state["deadline"] is not None else settings.command_statement_timeout_ms
        limits = (min(settings.command_lock_timeout_ms, remaining), min(settings.command_statement_timeout_ms, remaining))
    if state.get("limits") == limits:
        return
    if connection.dialect.name == "postgresql":
        cursor.execute("SELECT set_config('lock_timeout', %s, true), set_config('statement_timeout', %s, true)",
                       (str(limits[0]) + "ms", str(limits[1]) + "ms"))
    else:
        cursor.execute(f"PRAGMA busy_timeout={limits[0]}")
    state["limits"] = limits


def _diagnostic_settings(db, settings=None):
    if settings is not None:
        return settings
    bind = db.get_bind()
    return bind.get_execution_options().get("rota_write_settings") or Settings()


def _allow_lock_diagnostic(settings):
    global _lock_diagnostic_last_at, _lock_diagnostic_suppressed
    now = time.monotonic()
    with _lock_diagnostic_guard:
        if (_lock_diagnostic_last_at is not None
                and now - _lock_diagnostic_last_at < settings.command_lock_diagnostic_interval_seconds):
            _lock_diagnostic_suppressed += 1
            return False, 0
        suppressed = _lock_diagnostic_suppressed
        _lock_diagnostic_last_at = now
        _lock_diagnostic_suppressed = 0
        return True, suppressed


def _describe_blockers(db, settings, source, parameters, diagnostic):
    """Describe lock holders without retaining query text or row values in logs."""
    allowed, suppressed = _allow_lock_diagnostic(settings)
    if not allowed:
        return []
    if suppressed:
        log.warning("write_lock_diagnostic_suppressed", extra={"diagnostic": diagnostic, "count": suppressed})
    try:
        with db.begin_nested() as savepoint:
            timeout_ms = settings.command_lock_diagnostic_timeout_ms
            db.execute(text(f"SET LOCAL statement_timeout = '{int(timeout_ms)}ms'"))
            blockers = [dict(row) for row in db.execute(text("""
        WITH blockers AS ({source})
        SELECT b.pid, a.state, a.wait_event_type, a.wait_event, a.query_id,
               CASE WHEN upper(split_part(ltrim(a.query), ' ', 1)) IN
                    ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'COMMIT', 'ROLLBACK', 'WITH')
                    THEN upper(split_part(ltrim(a.query), ' ', 1)) ELSE 'OTHER' END AS statement_kind,
               extract(epoch FROM (clock_timestamp() - a.xact_start)) * 1000 AS transaction_ms,
               b.relation::regclass::text AS relation
        FROM blockers b JOIN pg_stat_activity a ON a.pid = b.pid
        ORDER BY a.xact_start ASC NULLS LAST, b.pid, b.relation
        LIMIT 8
                """.format(source=source)), parameters).mappings()]
            savepoint.rollback()  # Restore the caller's timeout before its remaining writes.
            return blockers
    except SQLAlchemyError as exc:
        log.warning("write_lock_diagnostics_unavailable", extra={"error_code": type(exc).__name__})
        return []


def _advisory_blockers(db, settings=None):
    # Never select raw query text: even pg_stat_activity can contain private literal values.
    settings = _diagnostic_settings(db, settings)
    return _describe_blockers(db, settings, """
        SELECT l.pid, NULL::oid AS relation
        FROM pg_locks l
        WHERE l.locktype = 'advisory' AND l.granted AND l.classid = 0
          AND l.objid = :key AND l.objsubid = 1
          AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
    """, {"key": WRITE_LOCK_KEY}, "advisory")


def _row_lock_blockers(db, settings=None, relations=()):
    settings = _diagnostic_settings(db, settings)
    source = """
        SELECT l.pid, l.relation
        FROM pg_locks l
        JOIN pg_class c ON c.oid = l.relation
        WHERE l.granted AND l.pid <> pg_backend_pid() AND l.mode <> 'AccessShareLock'
          AND c.relname = ANY(:relations)
    """
    return _describe_blockers(db, settings, source, {"relations": list(relations)}, "row_lock")


def log_row_lock_diagnostic(db, settings, context, *, savepoint=None):
    try:
        if savepoint is not None:
            savepoint.rollback()
        blockers = _row_lock_blockers(db, settings, context.get("lock_relations", ()))
    except Exception as diagnostic_error:
        log.warning("write_lock_diagnostics_unavailable",
                    extra={"error_code": type(diagnostic_error).__name__})
        blockers = []
    log.warning("password_login_row_lock_diagnostic", extra={**context, "blockers": blockers})


def make_engine(url: str, *, settings: Settings | None = None):
    settings = settings or Settings(sqlite_synchronous=os.getenv("ROTA_SQLITE_SYNCHRONOUS", Settings.sqlite_synchronous),
        **{name: int(os.getenv("ROTA_" + name.upper(), getattr(Settings, name)))
        for name in ("db_idle_tx_timeout_minutes", "db_pool_size", "db_max_overflow")})
    if url.startswith("sqlite"):
        settings.check_sqlite_synchronous()
    if url.startswith("sqlite:///"):
        path = url.removeprefix("sqlite:///")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    options = {"connect_args": {"check_same_thread": False, "timeout": settings.command_lock_timeout_ms / 1000}} if url.startswith("sqlite") else {
        "pool_pre_ping": True, "isolation_level": "READ COMMITTED",
        "connect_args": {"connect_timeout": settings.db_pool_timeout_seconds}}
    if not url.startswith("sqlite") or url.startswith("sqlite:///") and path != ":memory:":
        options["pool_timeout"] = settings.db_pool_timeout_seconds
        options["pool_size"] = settings.db_pool_size
        options["max_overflow"] = settings.db_max_overflow
    # Logged database errors must not carry bound values (names, private reasons, token hashes).
    engine = create_engine(url, hide_parameters=True, **options).execution_options(rota_write_settings=settings)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def sqlite_pragmas(connection, _record):
            from .services.search import register_sqlite
            register_sqlite(connection)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={settings.command_lock_timeout_ms}")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute({"FULL": "PRAGMA synchronous=FULL", "NORMAL": "PRAGMA synchronous=NORMAL",
                                "OFF": "PRAGMA synchronous=OFF"}[settings.sqlite_synchronous])
    else:
        @event.listens_for(engine, "connect")
        def postgres_timeouts(connection, _record):
            # Includes the short keep-alive and rate-limit writes before command dispatch.
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('lock_timeout', %s, false)", (str(settings.command_lock_timeout_ms) + "ms",))
                cursor.execute(f"SET idle_in_transaction_session_timeout = '{int(settings.db_idle_tx_timeout_minutes)}min'")
            connection.commit()

    @event.listens_for(engine, "before_cursor_execute")
    def bound_statement(connection, cursor, statement, parameters, context, executemany):
        context._rota_started = time.monotonic()
        if state := connection.info.get("rota_write"):
            _write_timeouts(connection, cursor, state)
            kind = statement.lstrip().split(None, 1)[0].upper()
            state["statement_kind"] = kind if kind in {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH"} else "OTHER"
            if kind in {"INSERT", "UPDATE", "DELETE", "WITH"}:
                state["snapshot_epoch"] = state.get("snapshot_epoch", 0) + 1
            state["statement_started"] = time.monotonic()
            state["statement_deadline"] = state["statement_started"] + state["limits"][1] / 1000

    @event.listens_for(engine, "after_cursor_execute")
    def slow_statement(connection, cursor, statement, parameters, context, executemany):
        elapsed = (time.monotonic() - getattr(context, "_rota_started", time.monotonic())) * 1000
        metrics = request_db_time_ms.get()
        if metrics is not None:
            metrics["ms"] += elapsed
        if state := connection.info.get("rota_write"):
            write_elapsed = (time.monotonic() - state["statement_started"]) * 1000
            if write_elapsed >= 250:
                log.warning("write_statement_slow", extra={**state["context"], "statement_kind": state["statement_kind"],
                            "backend_pid": state.get("backend_pid"), "duration_ms": round(write_elapsed, 2)})
    return engine


@event.listens_for(Session, 'after_transaction_create')
def retain_snapshot_input_savepoint(session, transaction):
    if transaction.nested:
        state = _snapshot_stamp_state(session)
        state['savepoints'][transaction] = (set(state['general']), set(state['availability']))


@event.listens_for(Session, 'after_soft_rollback')
def discard_rolled_back_snapshot(session, previous_transaction):
    session.info.get('_request_parents', {}).clear()
    session.info.get('_request_authority', {}).clear()
    session.info.pop('_baseline_hints', None)
    session.info.pop('write_domain_snapshots', None)
    session.info.pop('_live_safety_sources', None)
    session.info.pop('_activation_projection_cache', None)
    session.info.pop('_planning_group_authority_contexts', None)
    state = session.info.get('_snapshot_input_revisions')
    if state is not None and previous_transaction in state['savepoints']:
        state['general'], state['availability'] = state['savepoints'].pop(previous_transaction)


@event.listens_for(Session, 'after_rollback')
def discard_rolled_back_snapshot_inputs(session):
    # Read-only preparation rollbacks retain unchanged frozen inputs between rounds.
    state = session.info.get('_snapshot_input_revisions')
    if state and state['general']:
        _invalidate_snapshot_input_caches(session, state['general'])


@event.listens_for(Session, 'after_transaction_end')
def discard_finished_snapshot_input_state(session, transaction):
    if transaction.parent is None:
        session.info.pop('_request_parents', None)
        session.info.pop('_request_authority', None)
        session.info.pop('_baseline_hints', None)
        session.info.pop('_request_workspace_criteria', None)
        session.info.pop('_snapshot_input_revisions', None)


def allow_long_idle(db, minutes: int):
    """Allow bounded CPU work or streaming until this transaction ends."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text(f"SET LOCAL idle_in_transaction_session_timeout = '{int(minutes)}min'"))


@contextmanager
def _write_phase(state, phase):
    state["phase"], state["phase_started"] = phase, time.monotonic()
    fields = {**state["context"], "phase": phase, "pid": os.getpid(), "native_thread_id": get_native_id()}
    log.info("write_phase_started", extra=fields)
    try:
        yield
    except BaseException as exc:
        log.warning("write_phase_failed", extra={**fields,
                    "phase_wait_ms": round((time.monotonic() - state["phase_started"]) * 1000, 2),
                    "error_code": getattr(getattr(exc, "orig", None), "sqlstate", None) or type(exc).__name__})
        raise
    else:
        log.info("write_phase_completed", extra={**fields,
                 "phase_wait_ms": round((time.monotonic() - state["phase_started"]) * 1000, 2)})


@contextmanager
def transaction(engine, write: bool = False, *, deadline=None, context=None, serialize=True):
    settings = engine.get_execution_options().get("rota_write_settings") or Settings()
    # Bulk imports/fixture setup use this primitive too; retry_write owns the command's total budget.
    connection = None
    started = phase_started = time.monotonic()
    state = {"settings": settings, "deadline": deadline, "context": context or {}, "serializing": True,
             "lock_deadline": started + settings.command_write_budget_ms / 1000}
    phase = "pool"
    session = Session(engine, expire_on_commit=False)
    if request_db_time_ms.get() is not None:
        session.info['_request_workspace_criteria'] = {}
    if deadline is not None:
        session.info['_check_budget'] = lambda: _remaining(deadline)
    try:
        if write:
            connection = engine.connect()
            session.bind = connection
            session.connection()
            log.info("write_connection_acquired", extra={**state["context"], "pool_wait_ms": round((time.monotonic() - started) * 1000, 2)})
            connection.info["rota_write"] = state
            if engine.dialect.name == "sqlite":
                connection.connection.driver_connection.set_progress_handler(
                    lambda: int(time.monotonic() >= (state.get("statement_deadline") or state["deadline"] or float("inf"))), 1000)
            phase, phase_started = "lock", time.monotonic()
            if engine.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            elif not serialize:
                # Liveness writes use row locks only; they never join the global queue.
                pass
            else:
                acquired, state["backend_pid"] = session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key), pg_backend_pid()"), {"key": WRITE_LOCK_KEY}).one()
                if not acquired and engine.get_execution_options().get('rota_defer_busy_writer'):
                    raise DatabaseWriteDeferred()
                if not acquired:
                    log.warning("write_lock_wait", extra={**state["context"], "backend_pid": state["backend_pid"],
                                "blockers": _advisory_blockers(session, settings)})
                    waiting = engine.get_execution_options().get('rota_write_wait_callback')
                    if waiting:
                        # A worker retaining a completed result can report liveness only
                        # during this known lock wait; a hung operation gets no heartbeat.
                        while not acquired:
                            waiting()
                            time.sleep(min(settings.worker_poll_seconds,
                                           _remaining(deadline or state['lock_deadline']) / 1000))
                            acquired = session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": WRITE_LOCK_KEY})
                    else:
                        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": WRITE_LOCK_KEY})
            state["serializing"] = False
            if serialize:
                if request_db_time_ms.get() is not None:
                    # Application writes to these rows hold this lock until transaction end.
                    session.info['_request_parents'] = {}
                    session.info['_request_authority'] = {}
                log.info("write_lock_acquired", extra={**state["context"], "backend_pid": state.get("backend_pid"),
                         "lock_wait_ms": round((time.monotonic() - phase_started) * 1000, 2)})
            if engine.dialect.name == "postgresql" and state["context"].get("lock_diagnostic"):
                # A lock timeout must be observed on this connection before the outer rollback.
                state["lock_diagnostic_savepoint"] = session.begin_nested()
        with _write_phase(state, "operation") if write else nullcontext():
            yield session
        if write:
            with _write_phase(state, "flush"):
                session.flush()
                with closing(connection.connection.driver_connection.cursor()) as cursor:
                    _write_timeouts(connection, cursor, state)
        with _write_phase(state, "commit") if write else nullcontext():
            session.commit()
        if pending := session.info.pop('_live_changes', []):
            from . import live
            for topic, change in pending:
                if change is not None:
                    live.changed(topic, **change)
    except BaseException as exc:
        code = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if (write and engine.dialect.name == "postgresql" and code == "55P03"
                and state["context"].get("lock_diagnostic")):
            log_row_lock_diagnostic(session, settings, state["context"],
                                    savepoint=state.pop("lock_diagnostic_savepoint", None))
        if write:
            (log.debug if isinstance(exc, DatabaseWriteDeferred) else log.warning)("write_transaction_failed", extra={**state["context"], "backend_pid": state.get("backend_pid"),
                        "statement_kind": state.get("statement_kind"), "phase": state.get("phase", phase),
                        "phase_wait_ms": round((time.monotonic() - state.get("phase_started", phase_started)) * 1000, 2),
                        "duration_ms": round((time.monotonic() - started) * 1000, 2),
                        "error_code": getattr(getattr(exc, "orig", None), "sqlstate", None) or type(exc).__name__})
        elif isinstance(exc, DatabaseWriteTimeout) and engine.dialect.name == "postgresql":
            log.warning("write_timeout_diagnostic", extra={**state["context"], "blockers": _advisory_blockers(session, settings)})
        session.rollback()
        raise
    finally:
        with _write_phase(state, "cleanup") if write else nullcontext():
            try:
                if connection is not None:
                    if not connection.closed and not connection.invalidated:
                        connection.info.pop("rota_write", None)
                        if engine.dialect.name == "sqlite":
                            raw = connection.connection.driver_connection
                            raw.set_progress_handler(None, 0)
                            raw.execute(f"PRAGMA busy_timeout={settings.command_lock_timeout_ms}").close()
                    connection.close()
            finally:
                session.close()
                # Session.close expunges ORM rows but keeps info. Preparation and
                # read-projection graphs must end with this transaction, even if
                # a callback/exception temporarily retains the Session itself.
                session.info.clear()


def retry_write(engine, operation, attempts: int = 3, *, context=None, deadline=None, serialize=True):
    """Retry whole transactions for known conflicts, within one elapsed-time budget."""
    settings = engine.get_execution_options().get("rota_write_settings") or Settings()
    started = time.monotonic()
    deadline = min(started + settings.command_write_budget_ms / 1000, deadline if deadline is not None else float("inf"))
    for attempt in range(attempts):
        fields = {**(context or {}), "attempt": attempt + 1, "max_attempts": attempts}
        log.info("write_attempt_started", extra=fields)
        try:
            _remaining(deadline)
            with transaction(engine, write=True, deadline=deadline, context=fields, serialize=serialize) as db:
                result = operation(db)
            log.info("write_completed", extra={**fields, "duration_ms": round((time.monotonic() - started) * 1000, 2)})
            return result
        except (PoolTimeout, DatabaseWriteTimeout):
            log.warning("write_budget_exhausted", extra={**fields, "duration_ms": round((time.monotonic() - started) * 1000, 2)})
            raise DatabaseWriteTimeout() from None
        except OperationalError as exc:
            code = getattr(exc.orig, "sqlstate", None)
            detail = str(exc.orig).lower()
            transient = is_transient_database_error(exc)
            timed_out = code in {"55P03", "57014"} or engine.dialect.name == "sqlite" and "interrupted" in detail
            elapsed = round((time.monotonic() - started) * 1000, 2)
            log.warning("write_attempt_failed", extra={**fields, "error_code": code or type(exc.orig).__name__, "duration_ms": elapsed})
            if timed_out or transient and time.monotonic() >= deadline:
                raise DatabaseWriteTimeout() from None
            if not transient:
                raise
            if attempt + 1 == attempts:
                raise
            delay = min(0.03 * (attempt + 1), _remaining(deadline) / 1000)
            log.warning("write_retry", extra={**fields, "retry_wait_ms": round(delay * 1000, 2), "duration_ms": elapsed})
            time.sleep(delay)


def liveness_write(engine, operation, attempts: int = 3, *, context=None, deadline=None):
    """Run a short auth/session/security write without the rota advisory writer."""
    return retry_write(engine, operation, attempts=attempts, context=context,
                       deadline=deadline, serialize=False)

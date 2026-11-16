"""Durable read work, stored best-effort; authoritative command checks never consume it."""
import logging
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout
from apps.api import models as m
from apps.api.settings import Settings, quota_policy_limit
from packages.domain.input_manifest import digest, toolchain


# These payloads contain only this visible workspace and canonical published context.
CANONICAL_AUDIENCE = digest(None)
log = logging.getLogger('rota.read_projections')


def source_identity(db):
    held = db.info.setdefault('read_projection_inputs', {})
    if 'source' not in held:
        from .catalogue_proof_identity import identity
        source = identity()
        held['source'] = source['source_digest'] if source else None
    return held['source']


def input_stamp(db, workspace):
    # Keep strong references within this transaction (SQLAlchemy's identity map is weak).
    held = db.info.setdefault('read_projection_inputs', {})
    def get(cls, key):
        if (cls, key) not in held:
            held[cls, key] = db.get(cls, key)
        return held[cls, key]
    department = get(m.Department, workspace.department_id)
    org = get(m.Organisation, department.organisation_id)
    # The versioned evaluator/toolchain is the projection contract.  The raw
    # source digest changes on every deployment (including read-only code),
    # which used to discard otherwise valid Postgres projections at restart.
    # Release preparation bumps the evaluator versions when their output
    # changes, so revision-keyed read work survives an API deploy safely.
    return digest([org.input_revision, org.availability_revision,
        workspace.revision, workspace.content_revision, workspace.status, department.generation_defaults,
        digest(toolchain(quota_policy_limit()))])


def _store(db, audience, kind, values, stamps):
    """Write disposable values in a short transaction of its own, skipping rows another transaction holds.

    The reader's transaction never locks projection rows, so a writer (which holds the global write lock)
    never waits for a read request to finish, and readers never wait for each other.
    """
    C = m.WorkspaceReadProjection
    bind = db.get_bind()
    engine = getattr(bind, 'engine', bind)
    if engine.dialect.name != 'postgresql':
        # SQLite (local development) has one writer and no row locks; a second connection would only
        # wait for this transaction, so store in it and let its commit publish the values.
        from sqlalchemy.dialects.sqlite import insert
        statement = insert(C).values([{'workspace_id': wid, 'audience': audience, 'kind': kind,
                                       'value': values[wid], 'stamp': stamps[wid]} for wid in values])
        db.execute(statement.on_conflict_do_update(index_elements=['workspace_id', 'audience', 'kind'],
                   set_={'value': statement.excluded.value, 'stamp': statement.excluded.stamp}))
        return
    from sqlalchemy.dialects.postgresql import insert
    key = (C.audience == audience, C.kind == kind)
    try:
        with engine.begin() as connection:
            # Bound the only possible wait (a concurrent insert of the same new row) like a command lock wait.
            settings = engine.get_execution_options().get('rota_write_settings') or Settings()
            connection.execute(text("SELECT set_config('lock_timeout', :value, true)"),
                               {'value': f'{settings.command_lock_timeout_ms}ms'})
            # A row held by another transaction keeps its value; this one is only a cache refresh.
            lockable = set(connection.scalars(select(C.workspace_id).where(C.workspace_id.in_(values), *key)
                .with_for_update(skip_locked=True)))
            for wid in lockable:
                connection.execute(C.__table__.update().where(C.workspace_id == wid, *key)
                                   .values(value=values[wid], stamp=stamps[wid]))
            if new := [wid for wid in values if wid not in lockable]:
                # DO NOTHING never waits on an existing row's lock, only on a concurrent uncommitted insert.
                connection.execute(insert(C).values([{'workspace_id': wid, 'audience': audience, 'kind': kind,
                    'value': values[wid], 'stamp': stamps[wid]} for wid in new]).on_conflict_do_nothing(
                    index_elements=['workspace_id', 'audience', 'kind']))
    except (OperationalError, PoolTimeout) as exc:
        # Disposable: the next read recomputes. Never fail or delay the response for a cache write.
        log.warning('read_projection_store_skipped', extra={'error_code': type(exc).__name__})


def department_anchor(db, department_id):
    """Return a stable FK anchor for department-level disposable projections."""
    return db.scalar(select(m.Workspace.id).where(
        m.Workspace.department_id == department_id).order_by(m.Workspace.created_at, m.Workspace.id).limit(1))


def department_audience(db, actor, audience_values=(), *, shared_body=False):
    """Keep personal projections actor-scoped; explicitly shared bodies use effective rights."""
    shared_only = db.info.get('workspace_shared_only', False) if db is not None else False
    if shared_body:
        from packages.contracts.schemas import PERMISSIONS
        reader = sorted(name for name, roles in PERMISSIONS.items() if actor.permits(*roles))
        return digest([actor.department_id, reader, shared_only, *audience_values])
    return digest([actor.identity_id, actor.person_id, sorted(actor.roles), shared_only, *audience_values])


def cached_department(db, actor, kind, stamp, build, *, audience_values=(), anchor=None, audience=None):
    """Share an actor-scoped department read across API workers using the existing projection table.

    ``WorkspaceReadProjection`` predates department projections and intentionally
    keeps a workspace FK.  A stable workspace in the department is only storage
    plumbing; the default audience includes the actor and read variant. An explicit
    audience may share a body only after its caller removes personal data. Departments without a
    workspace use the caller's bounded fallback because there is no valid FK
    anchor yet.
    """
    anchor = department_anchor(db, actor.department_id) if anchor is None else anchor
    if anchor is None:
        return build()
    audience = department_audience(db, actor, audience_values) if audience is None else audience
    return cached(db, [anchor], kind, {anchor: stamp}, lambda _anchor: build(), audience=audience)[anchor]


def cached(db, workspaces, kind, stamps, build=None, *, build_many=None, audience=None):
    """One batch read; misses compute without holding locks and are stored best-effort afterwards."""
    if not workspaces:
        return {}
    def acquire(ids):
        return build_many(ids) if build_many else {wid: build(wid) for wid in ids}
    # Activation preparation is a repeatable read.  Consume an already-warmed
    # canonical projection before rebuilding a full rota snapshot; the final
    # writer still rechecks its relational activation stamp.  Misses stay
    # request-local and are deliberately not written from this read path.
    if db.info.get('_activation_read'):
        memo = db.info.setdefault('_activation_projection_cache', {})
        canonical = kind in {'fingerprint', 'feasibility_fingerprint', 'summary_check', 'stored_check'}
        activation_audience = CANONICAL_AUDIENCE if canonical else audience
        if activation_audience is None:
            actor = db.info.get('workspace_actor')
            activation_audience = digest([actor.identity_id, sorted(actor.roles),
                                          db.info.get('workspace_shared_only', False)] if actor else None)
        C = m.WorkspaceReadProjection
        rows = {wid: (stamp, value) for wid, stamp, value in db.execute(
            select(C.workspace_id, C.stamp, C.value).where(
                C.workspace_id.in_(workspaces), C.audience == activation_audience, C.kind == kind))}
        missing = [wid for wid in workspaces
                   if (kind, wid, stamps[wid]) not in memo
                   and (wid not in rows or rows[wid][0] != stamps[wid] or rows[wid][1] is None)]
        memo.update({(kind, wid, stamps[wid]): value for wid, value in acquire(missing).items()})
        return {wid: memo[(kind, wid, stamps[wid])] if (kind, wid, stamps[wid]) in memo else rows[wid][1]
                for wid in workspaces}
    if db.connection().info.get('rota_write') or db.new or db.dirty:
        return acquire(workspaces)
    if audience is None:
        actor = db.info.get('workspace_actor')
        audience = digest([actor.identity_id, sorted(actor.roles), db.info.get('workspace_shared_only', False)]
                          if actor else None)
        if kind in {'fingerprint', 'feasibility_fingerprint', 'summary_check', 'stored_check'}:
            audience = CANONICAL_AUDIENCE
    C = m.WorkspaceReadProjection
    rows = {wid: (stamp, value) for wid, stamp, value in db.execute(select(C.workspace_id, C.stamp, C.value).where(
        C.workspace_id.in_(workspaces), C.audience == audience, C.kind == kind))}
    missing = sorted(wid for wid in workspaces if wid not in rows or rows[wid][0] != stamps[wid])
    values = acquire(missing) if missing else {}
    if values:
        _store(db, audience, kind, values, stamps)
    return {wid: (values[wid] if wid in values else rows[wid][1]) for wid in workspaces}

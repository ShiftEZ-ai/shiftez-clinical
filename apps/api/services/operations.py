"""Department-scoped, payload-free queue observations; never a liveness promise."""
from datetime import timedelta
from sqlalchemy import func, or_, select
from apps.api import models as m
from apps.api.auth import require_permission
from apps.api.errors import require
from apps.api.serialization import record, json_value
from apps.api.settings import Settings
from packages.domain.time import aware
from packages.domain.worker_contract import JOB_LEASE_SECONDS, OUTBOX_STATES, JOB_STATES
from . import collection_filters



def worker_heartbeat_max_age_seconds(db):
    settings = db.get_bind().get_execution_options().get("rota_write_settings") or Settings()
    return settings.worker_heartbeat_max_age_seconds


def _worker_heartbeat_cutoff(db, now, max_age_seconds=None):
    if max_age_seconds is None:
        max_age_seconds = worker_heartbeat_max_age_seconds(db)
    return now - timedelta(seconds=max_age_seconds)


def worker_state(db, now):
    """Report job-worker liveness using the configured heartbeat freshness window."""
    cutoff = _worker_heartbeat_cutoff(db, now)
    last = db.scalar(select(func.max(m.WorkerHeartbeat.last_seen_at)))
    if last is None:
        return {"worker_liveness": "never_seen", "worker_last_seen_age_seconds": None, "workers_running": 0}
    last = last if last.tzinfo else last.replace(tzinfo=now.tzinfo)
    running = db.scalar(select(func.count()).select_from(m.WorkerHeartbeat).where(m.WorkerHeartbeat.last_seen_at >= cutoff))
    return {"worker_liveness": "running" if running else "not_responding",
            "worker_last_seen_age_seconds": max(0, int((now - last).total_seconds())), "workers_running": running}


def latest_worker_heartbeat(db, now):
    """Return the latest fresh job-worker report, or None when no job worker is currently live."""
    return db.execute(select(m.WorkerHeartbeat.release, m.WorkerHeartbeat.last_seen_at)
                      .where(m.WorkerHeartbeat.last_seen_at >= _worker_heartbeat_cutoff(db, now))
                      .order_by(m.WorkerHeartbeat.last_seen_at.desc()).limit(1)).first()

def observe(db, actor, backend, environment, offset=0, state=None, kind=None, q=None, *, collection_filter=None):
    require_permission(actor, "system.operations")
    require(0 <= offset <= 1_000_000, "INVALID_OFFSET", "Invalid outbox pagination offset")
    picked_states = state.split(",") if state is not None else []
    picked_kinds = kind.split(",") if kind is not None else []
    require(all(s in OUTBOX_STATES for s in picked_states), "INVALID_STATE", "Unknown outbox state")
    require(all(0 < len(k) <= 40 for k in picked_kinds), "INVALID_KIND", "Invalid outbox event kind")
    now, dept, limit = m.now(), actor.department_id, 100

    def scoped(cls):
        return cls.department_id == dept

    def count(cls, *conditions):
        return db.scalar(select(func.count()).select_from(cls).where(scoped(cls), *conditions))

    def age(value):
        return None if value is None else round(max(0, (now - aware(value)).total_seconds()), 3)

    def states(cls, column, known):
        result = dict.fromkeys(known, 0)
        result.update(dict(db.execute(select(column, func.count()).where(scoped(cls)).group_by(column)).all()))
        return result

    due = (m.Outbox.state.in_(("queued", "retry")), m.Outbox.next_attempt_at <= now)
    due_since = db.scalar(select(func.min(m.Outbox.next_attempt_at)).where(scoped(m.Outbox), *due))
    queued_since = db.scalar(select(func.min(m.Job.created_at)).where(scoped(m.Job), m.Job.status == "queued"))
    last_import = db.scalar(select(func.max(m.ImportBatch.created_at)).where(scoped(m.ImportBatch)))
    from .condition_context import evaluation_context, required_tokens
    context = evaluation_context(db, actor, as_of=collection_filter.as_of, table_key="operations",
        tokens=required_tokens(collection_filter.conditions)) if collection_filter and collection_filter.conditions else None
    selected = [scoped(m.Outbox)]
    columns = {'id': m.Outbox.id, 'state': m.Outbox.state, 'kind': m.Outbox.kind, 'error': m.Outbox.last_error,
               'created_at': m.Outbox.created_at, 'date': m.Outbox.created_at}
    selected.append(collection_filters.predicate(collection_filter.conditions if collection_filter else None,
        columns, context, table_key='operations'))
    if picked_states:
        selected.append(m.Outbox.state.in_(picked_states))
    if picked_kinds:
        selected.append(m.Outbox.kind.in_(picked_kinds))
    term = (q or "").strip()
    if collection_filter and collection_filter.search_options is not None:
        from .search import predicate
        selected.append(predicate(db, actor, 'operations', term, collection_filter.search_options, as_of=collection_filter.as_of))
    elif term:
        like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        selected.append(or_(*(column.ilike(like, escape="\\") for column in (
            m.Outbox.kind, func.replace(m.Outbox.kind, "_", " "),
            m.Outbox.state, func.replace(m.Outbox.state, "_", " "), m.Outbox.last_error))))
    total = db.scalar(select(func.count()).select_from(m.Outbox).where(*selected))
    query = select(m.Outbox).where(*selected).order_by(m.Outbox.created_at.desc(), m.Outbox.seq.desc())
    query = collection_filters.order_by(query, collection_filter, columns,
                                       default=(m.Outbox.created_at.desc(), m.Outbox.seq.desc()))
    events = list(db.scalars(query.offset(offset).limit(limit)))
    return {"outbox": [record(event, exclude=("payload",)) for event in events],
            "offset": offset, "limit": limit, "total": total, "state": state,
            "kinds": list(db.scalars(select(m.Outbox.kind).where(scoped(m.Outbox)).distinct().order_by(m.Outbox.kind))),
            "has_more": offset + len(events) < total,
            "database_backend": backend, "environment": environment,
            "notification_channel": "in_app", "production_qualified": False,
            "metrics": {
                "metric_version": "department-operations-v1", "observed_at": json_value(now),
                "snapshot_consistency": "best_effort",
                "outbox_states": states(m.Outbox, m.Outbox.state, OUTBOX_STATES),
                "outbox_due_count": count(m.Outbox, *due),
                "outbox_oldest_due_age_seconds": age(due_since),
                "job_states": states(m.Job, m.Job.status, JOB_STATES),
                "oldest_queued_job_age_seconds": age(queued_since),
                "expired_running_job_leases": count(m.Job, m.Job.status == "running",
                                                     func.coalesce(m.Job.heartbeat_at, m.Job.started_at) < now - timedelta(seconds=JOB_LEASE_SECONDS)),
                "running_jobs_without_lease_start": count(m.Job, m.Job.status == "running", m.Job.started_at.is_(None)),
                "job_lease_seconds": JOB_LEASE_SECONDS,
                "import_states": states(m.ImportBatch, m.ImportBatch.status, ("preview", "saved_draft")),
                "latest_import_received_at": json_value(last_import),
                "latest_import_received_age_seconds": age(last_import),
                **worker_state(db, now), "source_completeness": "unqualified",
                "basis": "Best-effort multi-query observation; queues can change between reads and pages. Worker liveness comes from each job worker's heartbeat and the configured freshness window. Import receipt does not establish source completeness. No service-level threshold is assumed."
            }}

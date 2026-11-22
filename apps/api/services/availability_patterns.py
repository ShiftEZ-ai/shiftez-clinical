"""Weekly availability: recurring times someone cannot work (e.g. every Tuesday 13:00–17:00).

Staff propose their own pattern; a coordinator approves it (or records one directly). Only active patterns count:
they are expanded into ordinary unavailability for the dates a rota check looks at, so every check, fill
suggestion and auto-schedule respects them without a second code path.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import and_, func, or_, select
from packages.contracts import schemas as s
from packages.domain.time import aware, local_stamp
from apps.api import models as m
from apps.api.auth import require_role, person_access
from apps.api.errors import require
from apps.api.serialization import record
from .common import scoped, expected, bump_inputs, outbox, history, history_version, supersede_outbox
from . import independence


def require_pattern_person(actor, person_id):
    require(actor.permits("coordinator") or actor.person_id == person_id, "FORBIDDEN", "Staff may set only their own weekly availability", 403)


def propose(db, actor, command):
    require_role(actor, "staff", "coordinator")
    value = s.AvailabilityPatternInput.model_validate(command.payload)
    person_access(db, actor, value.person_id)
    # A coordinator's own pattern is proposed like anyone else's, so someone else approves it.
    coordinator = actor.permits("coordinator") and independence.subject_independent(
        db, actor, (value.person_id,), actor.identity_id)
    require_pattern_person(actor, value.person_id)
    row = m.AvailabilityPattern(department_id=actor.department_id, created_by=actor.identity_id,
                                status="active" if coordinator else "proposed", decided_by=actor.identity_id if coordinator else None,
                                decided_at=m.now() if coordinator else None,
                                **value.model_dump())
    db.add(row)
    db.flush()
    if coordinator:
        independence.ensure_subject(db, actor, "request.decide", (row.person_id,), row.created_by,
                                    "A different coordinator must decide this weekly availability", row.note, row.id)
        bump_inputs(db, actor, availability=True)
    else:
        outbox(db, actor, "availability-pattern:" + row.id, "request", {
            "person_id": row.person_id, "pattern_id": row.id, "pattern_revision": row.revision, "pattern_status": row.status})
    return {"pattern": record(row), "message": "Weekly availability saved." if coordinator else "Weekly availability sent for approval."}


def decide(db, actor, command, *, batch=False, approval_context=None):
    from .availability import decision_allowed
    require_role(actor, "coordinator")
    value = s.PatternDecision.model_validate(command.payload)
    row = scoped(db, m.AvailabilityPattern, command.target_id, actor)
    expected(row, command.expected_revision)
    require(row.status == "proposed", "WORKFLOW_STATE", "Only proposed patterns can be decided", 409)
    require(decision_allowed(db, actor, row, approval_context=approval_context),
            "SELF_APPROVAL", "A different coordinator must decide this weekly availability", 403)
    independence.ensure_subject(db, actor, "request.decide", (row.person_id,), row.created_by,
                        "A different coordinator must decide this weekly availability", value.reason, row.id, context=approval_context)
    row.status, row.decided_by = ("active" if value.decision == "approve" else "declined"), actor.identity_id
    row.decided_at = m.now()
    row.revision += 1
    history(db, actor, "availability_pattern.decide", row, value.reason)
    outbox(db, actor, f"availability-pattern:{row.id}:{row.revision}:decided", "request", {
        "person_id": row.person_id, "pattern_id": row.id, "pattern_revision": row.revision, "pattern_status": row.status})
    if row.status == "active" and not batch:
        bump_inputs(db, actor, availability=True)
    return {"pattern": record(row), **({"inputs_changed": row.status == "active"} if batch else {})}


def require_pattern_change(actor, row, action):
    require(actor.permits("coordinator") or row.person_id == actor.person_id, "FORBIDDEN",
            f"Only the person or a coordinator can {action} this pattern", 403)


def require_pattern_undo(db, actor, row):
    if row.status in {"active", "declined"}:
        require_role(actor, "coordinator")
        require(row.decided_by == actor.identity_id, "FORBIDDEN",
                "Only the coordinator who decided this pattern can undo it", 403)
        if row.created_by == actor.identity_id:
            def matches(saved):
                return (saved.get("id") == row.id and saved.get("status") == row.status
                        and saved.get("decided_by") == actor.identity_id and saved.get("decided_at") is not None
                        and row.decided_at is not None and aware(saved["decided_at"]) == aware(row.decided_at))
            decision = history_version(db, actor, "availability_pattern.decide", row.id)
            proved = decision is not None and matches(decision.snapshot)
            if not proved:
                # Older successful decisions predate lifecycle snapshots; their committed receipts retain provenance.
                E, R = m.AuditEvent, m.CommandReceipt
                receipts = db.scalars(select(R).join(E, R.id == E.detail["receipt_id"].as_string()).where(
                    E.department_id == actor.department_id, R.department_id == actor.department_id,
                    E.actor_id == actor.identity_id, R.actor_id == actor.identity_id,
                    E.action == "availability_pattern.decide", E.target_id == row.id))
                proved = any(matches(saved) for receipt in receipts for saved in (
                    [receipt.result["pattern"]] if "pattern" in receipt.result else
                    [r for r in receipt.result.get("records", []) if r.get("source") == "pattern"]))
            require(proved, "FORBIDDEN", "Only an approved or declined proposal can have its decision undone", 403)


def undo(db, actor, command, *, batch=False):
    """Undo an approval or decline: the pattern waits for a decision again (only whoever decided, only before it ended)."""
    value = s.Decision.model_validate(command.payload)
    row = scoped(db, m.AvailabilityPattern, command.target_id, actor)
    require_pattern_undo(db, actor, row)
    expected(row, command.expected_revision)
    require(row.status in ("active", "declined"), "WORKFLOW_STATE", "Only an approved or declined pattern can be put back", 409)
    was_active = row.status == "active"
    row.status, row.decided_by = "proposed", None
    row.decided_at = None
    row.revision += 1
    history(db, actor, "availability_pattern", row, value.reason)
    outbox(db, actor, f"availability-pattern:{row.id}:{row.revision}:undone", "request", {
        "person_id": row.person_id, "pattern_id": row.id, "pattern_revision": row.revision, "pattern_status": row.status},
        undo_of=f"availability-pattern:{row.id}:{command.expected_revision}:decided")
    if was_active and not batch:
        bump_inputs(db, actor, availability=True)
    return {"pattern": record(row), **({"inputs_changed": was_active} if batch else {})}


def end(db, actor, command):
    require_role(actor, "staff", "coordinator")
    value = s.Decision.model_validate(command.payload)
    row = scoped(db, m.AvailabilityPattern, command.target_id, actor)
    require_pattern_change(actor, row, "end")
    expected(row, command.expected_revision)
    require(row.status in {"proposed", "active"}, "WORKFLOW_STATE", "This pattern has already ended", 409)
    history(db, actor, "availability_pattern.before_end", row, value.reason)
    was_active = row.status == "active"
    row.status = "ended"
    row.decided_at = m.now()
    row.revision += 1
    history(db, actor, "availability_pattern", row, value.reason)
    supersede_outbox(db, actor, "availability-pattern:" + row.id,
                     prefixes=(f"availability-pattern:{row.id}:",))
    if was_active:
        bump_inputs(db, actor, availability=True)
    return {"pattern": record(row)}


def restore(db, actor, command):
    """Restore exactly the state an end retained; restoring a proposal never approves it."""
    require_role(actor, "staff", "coordinator")
    value = s.Decision.model_validate(command.payload)
    row = scoped(db, m.AvailabilityPattern, command.target_id, actor)
    require_pattern_change(actor, row, "restore")
    expected(row, command.expected_revision)
    person_access(db, actor, row.person_id)
    require(row.status == "ended", "WORKFLOW_STATE", "Only an ended pattern can be restored", 409)
    previous = history_version(db, actor, "availability_pattern.before_end", row.id, row.revision - 1)
    require(previous is not None and previous.snapshot.get("status") in {"proposed", "active"}, "WORKFLOW_STATE",
            "The previous pattern state is unavailable; propose the pattern again", 409)
    row.status = previous.snapshot["status"]
    previous_at = previous.snapshot.get("decided_at")
    if "decided_at" not in previous.snapshot and row.status == "active":
        E, R = m.AuditEvent, m.CommandReceipt
        previous_at = db.scalar(select(func.max(E.created_at)).outerjoin(R, R.id == E.detail["receipt_id"].as_string()).where(
            E.department_id == row.department_id, E.action.in_(("availability_pattern.decide", "availability_pattern.propose")),
            or_(E.target_id == row.id, and_(E.action == "availability_pattern.propose", R.result["pattern"]["id"].as_string() == row.id)),
            E.created_at < previous.created_at))
    row.decided_at = aware(previous_at) if previous_at is not None else None
    row.revision += 1
    history(db, actor, "availability_pattern", row, value.reason)
    if row.status == "active":
        bump_inputs(db, actor, availability=True)
    else:
        outbox(db, actor, f"availability-pattern:{row.id}:{row.revision}:restored", "request", {
            "person_id": row.person_id, "pattern_id": row.id, "pattern_revision": row.revision, "pattern_status": row.status})
    return {"pattern": record(row)}


def list_patterns(db, actor):
    query = select(m.AvailabilityPattern).where(m.AvailabilityPattern.department_id == actor.department_id,
                                                 m.AvailabilityPattern.status.in_(("proposed", "active")))
    if not actor.permits("coordinator", "approver"):
        query = query.where(m.AvailabilityPattern.person_id == actor.person_id)
    return {"patterns": [record(r) for r in db.scalars(query.order_by(m.AvailabilityPattern.created_at, m.AvailabilityPattern.seq))]}


def occurrences(p, first, last, tz, *, bounds=None):
    """Blocked times from `first` to `last` inclusive; optional [lo, hi) bounds exclude unrelated occurrences."""
    if bounds is not None:
        lo, hi = (aware(value) for value in bounds)
        if lo >= hi:
            return
        first, last = max(first, lo.astimezone(tz).date()), min(last, (hi - timedelta(microseconds=1)).astimezone(tz).date())
    day = max(first, p.starts_on)
    stop_day = min(last, p.ends_on - timedelta(days=1)) if p.ends_on else last
    while day <= stop_day:
        if day.weekday() in p.weekdays:
            may_overlap = True
            if bounds is not None:
                candidates = []
                for clock in (p.start_time, p.end_time):
                    stamp = datetime.combine(day + timedelta(days=clock == "24:00"),
                        time.fromisoformat("00:00" if clock == "24:00" else clock), tz)
                    candidates.append([aware(stamp.replace(fold=fold)) for fold in (0, 1)])
                # Exclude only when neither offset can overlap; local_stamp still rejects an in-range gap or fold.
                may_overlap = min(candidates[0]) < hi and max(candidates[1]) > lo
            if may_overlap:
                yield (local_stamp(day, p.start_time, tz.key), local_stamp(day, p.end_time, tz.key), day)
        day += timedelta(days=1)


def snapshot_source(department_ids, people):
    return m.AvailabilityPattern, (m.AvailabilityPattern.department_id.in_(department_ids)
        & (m.AvailabilityPattern.status == 'active') & m.AvailabilityPattern.person_id.in_(people))


def expand(db, department_ids, people, lo, hi, zone, *, acquired=None):
    """Active patterns as unavailability entries overlapping [lo, hi) (same shape as recorded availability)."""
    if lo >= hi:
        return []
    tz = ZoneInfo(zone)
    cls, where = snapshot_source(department_ids, people)
    rows = acquired if acquired is not None else db.scalars(select(cls).where(where))
    # Patterns cannot cross midnight; validate only dates inside this half-open query.
    first, last = lo.astimezone(tz).date(), (hi - timedelta(microseconds=1)).astimezone(tz).date()
    return [{"id": f"pattern:{p.id}:{day.isoformat()}", "person_id": p.person_id, "department_id": p.department_id,
             "start": a.isoformat(), "end": b.isoformat(), "kind": "weekly_unavailable", "leave_type": "",
             "request_id": None, "retracted": False, "pattern_id": p.id}
            for p in rows for a, b, day in occurrences(p, first, last, tz, bounds=(lo, hi)) if a < hi and b > lo]

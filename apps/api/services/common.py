from sqlalchemy import or_, select, update
from sqlalchemy import inspect
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from apps.api import models as m
from apps.api.auth import person_access
from apps.api.errors import require
from apps.api.serialization import record
from packages.domain.time import local_midnight

CATALOGUES = {
    "contact_types": m.ContactType,
    "date_sets": m.DateSet,
    "leave_types": m.LeaveType,
    "department_content": m.DepartmentContent,
    "event_types": m.EventType, "events": m.Event,
    "operating_calendars": m.OperatingCalendar, "resource_pools": m.ResourcePool,
    "categories": m.Category, "capabilities": m.Capability, "location_types": m.LocationType,
    "locations": m.Location, "services": m.Service, "people": m.Person, "tags": m.Tag,
    "assignment_types": m.AssignmentType, "programmes": m.Programme, "rotation_types": m.RotationType,
    "cohorts": m.Cohort, "rotations": m.Rotation, "recurring_series": m.RecurringSeries,
    "programme_memberships": m.ProgrammeMembership,
    "mentorships": m.Mentorship,
    "programme_calendar_entries": m.ProgrammeCalendarEntry,
    "rotation_templates": m.RotationTemplate,
}
# Training records: admins, coordinators and programme leads keep them (commands.command_roles, catalogue.write_catalogue).
PROGRAMME_CATALOGUES = frozenset({"rotation_types", "programmes", "cohorts", "rotations", "programme_memberships", "mentorships",
                                  "programme_calendar_entries", "rotation_templates"})


def omitted_update_fields(value, saved):
    """Carry forward stored fields that were absent from the validated request."""
    return {key: saved[key] for key in type(value).model_fields if key in saved and key not in value.model_fields_set}


# The swap reviewers (services/swaps.py). With the two swappers, only they read who swapped with whom.
SWAP_NAME_READERS = ("coordinator", "approver")
SWAP_SUFFIX = " · swap "


def may_name_swap(roles, person_id, swap_people):
    return bool(set(roles) & set(SWAP_NAME_READERS)) or (person_id is not None and person_id in swap_people)


def neutral_rota_name(name):
    """The rota label without the swappers' names that amendments stored before SW3-C-07 (new ones store none)."""
    return str(name).partition(SWAP_SUFFIX)[0] if name else name


def neutral_notification_title(title):
    """A stored publication title ("<rota> · swap A ↔ B published") without the names; the outcome is kept."""
    if not title or SWAP_SUFFIX not in title:
        return title
    outcome = next((end for end in (" published", " was replaced") if title.endswith(end)), "")
    return neutral_rota_name(title) + outcome


def rota_names(db, roles, person_id, labels):
    """{workspace_id: label} for this reader, from {workspace_id: stored label}, with one query per table.

    A swap amendment says who swapped (from the owning swap row, never the stored label) only to the swap reviewers
    and the two swappers; everyone else reads the rota's own name.
    """
    labels = dict(labels)
    swaps = {row.workspace_id: row for row in db.scalars(select(m.SwapRequest).where(
        m.SwapRequest.workspace_id.in_(labels)))} if labels else {}
    swaps = {wid: row for wid, row in swaps.items()
             if may_name_swap(roles, person_id, {row.proposer_person_id, row.recipient_person_id})}
    ids = {pid for row in swaps.values() for pid in (row.proposer_person_id, row.recipient_person_id) if pid}
    people = dict(db.execute(select(m.Person.id, m.Person.display_name).where(m.Person.id.in_(ids))).all()) if ids else {}

    def label(workspace_id, name):
        base, row = neutral_rota_name(name), swaps.get(workspace_id)
        if row is None:
            return base
        direction = "→" if row.kind == "give" else "↔"
        return f"{base}{SWAP_SUFFIX}{people.get(row.proposer_person_id, 'colleague')} {direction} {people.get(row.recipient_person_id, 'colleague')}"[:160]
    return {workspace_id: label(workspace_id, name) for workspace_id, name in labels.items()}


def visible_rota_name(db, actor, workspace_id, name):
    return rota_names(db, actor.roles, actor.person_id, {workspace_id: name})[workspace_id]


def scoped(db, cls, object_id, actor, *, options=(), include_withdrawn=False):
    require(bool(object_id), "TARGET_REQUIRED", "A target record is required")
    row = (db.info.get('bulk', {}).get('relationship_references', {}).get((cls, object_id))
           or db.info.get('bulk_relationship_references', {}).get((cls, object_id))) if not options else None
    if row is not None:
        state = inspect(row)
        if state.session is not db or state.deleted:
            row = None
    row = row or db.get(cls, (actor.department_id, object_id) if cls is m.ContactType else object_id, options=options,
                       execution_options={'include_withdrawn': include_withdrawn})
    require(row is not None and row not in db.deleted and (include_withdrawn or not getattr(row, 'withdrawn', False)),
            "NOT_FOUND", "Record not found", 404)
    if isinstance(row, m.Person):
        return person_access(db, actor, row.id)
    require(getattr(row, "department_id", None) == actor.department_id,
            "NOT_FOUND", "Record not found", 404)
    if isinstance(row, (m.DepartmentContent, m.Event, m.EventOccurrence)):
        from .notification_audience import source_visibility
        visible = source_visibility(db, actor, cls)
        if visible is not True:
            require(db.scalar(select(cls.id).where(cls.id == row.id, visible)) is not None,
                    "NOT_FOUND", "Record not found", 404)
    if isinstance(row, m.Workspace):
        from .workspace_access import require_visible
        require_visible(db, actor, row)
    if isinstance(row, m.Job) and row.kind in {'merge_preview', 'merge_apply'}:
        require(row.requested_by == actor.identity_id, 'NOT_FOUND', 'Record not found', 404)
    return row


def expected(row, value):
    require(value is not None, "REVISION_REQUIRED", "Use the current revision when saving", 428)
    require(row.revision == value, "STALE_REVISION", "Another session changed this record; reload and reconcile", 409,
            {"current_revision": row.revision})


def require_import_unapplied(batch, applied_status="applied", *, has_unapplied_part=True):
    require(batch.status != applied_status and has_unapplied_part, "IMPORT_ALREADY_APPLIED",
            "This import is already applied. Upload the sheet again to review new changes.", 409)


def local_date_span(start, end, timezone):
    try:
        return local_midnight(start, timezone), local_midnight(end, timezone)
    except (ValueError, OverflowError):
        require(False, "INTERVAL_DATES", "Choose dates representable in the organisation's timezone")


def batch_input_invalidations(operation):
    """Compatibility decorator: input writes no longer destroy frozen preparation."""
    return operation


def bump_inputs(db, actor, availability=False):
    # Compatibility marker retained for callers during the listener migration.
    # ``Session.before_flush`` is the only revision authority; explicit calls
    # cannot double-bump or disagree with the table-derived classification.
    return None


def history(db, actor, kind, row, reason, *, snapshot_extra=None):
    if snapshot_extra is None:
        history_many(db, actor, [(kind, row, reason)])
    else:
        history_many(db, actor, [(kind, row, reason)], snapshot_extra=snapshot_extra)


def history_many(db, actor, entries, *, snapshot_extra=None, immediate=False, cohort_programmes=None):
    """Capture one lifecycle state for a batch after a single defaults/update flush."""
    db.execute(update(m.Department).where(m.Department.id == actor.department_id)
               .values(catalogue_revision=m.Department.catalogue_revision + 1).execution_options(autoflush=False))
    bulk = db.info.get("bulk")
    if not bulk or immediate:
        db.flush()
    cohort_ids = {row.cohort_id for kind, row, _ in entries if kind == "rotations"}
    programmes = dict(cohort_programmes or {})
    missing_cohorts = cohort_ids - programmes.keys()
    if missing_cohorts:
        programmes.update(db.execute(select(m.Cohort.id, m.Cohort.programme_id).where(
            m.Cohort.id.in_(missing_cohorts))).all())
    if bulk and not immediate:
        for kind, row, reason in entries:
            if inspect(row).pending:
                materialise_defaults(row)
            if row.id is None:
                row.id = m.uid()
            if row.revision is None:
                row.revision = 1
            bulk["history"].append((kind, row, reason, {**_history_snapshot(row),
                **({"programme_id": programmes[row.cohort_id]} if kind == "rotations" else {}), **(snapshot_extra or {})}))
        return
    db.add_all([m.DefinitionHistory(department_id=actor.department_id, kind=kind,
        object_id=row.id, object_revision=row.revision, snapshot={**_history_snapshot(row),
            **({"programme_id": programmes[row.cohort_id]} if kind == "rotations" else {}), **(snapshot_extra or {})},
        actor_id=actor.identity_id, reason=reason, batch_id=(bulk or {}).get("batch_id")) for kind, row, reason in entries])


def _history_snapshot(row):
    return record(row, exclude=("private_reason",) if isinstance(row, m.Availability) else ())


def history_version(db, actor, kind, object_id, revision=None):
    """Read persisted, pending or queued lifecycle evidence without draining a batch's rollback-aware queue."""
    query = select(m.DefinitionHistory).where(m.DefinitionHistory.department_id == actor.department_id,
        m.DefinitionHistory.kind == kind, m.DefinitionHistory.object_id == object_id)
    if revision is not None:
        query = query.where(m.DefinitionHistory.object_revision == revision)
    stored = db.scalar(query.order_by(m.DefinitionHistory.object_revision.desc()).limit(1))
    versions = [row for row in db.new if isinstance(row, m.DefinitionHistory)
        and row.department_id == actor.department_id and row.kind == kind and row.object_id == object_id
        and (revision is None or row.object_revision == revision)]
    if stored is not None:
        versions.append(stored)
    for queued_kind, row, reason, snapshot in db.info.get("bulk", {}).get("history", []):
        if queued_kind == kind and row.id == object_id and row.department_id == actor.department_id and (
                revision is None or snapshot["revision"] == revision):
            versions.append(SimpleNamespace(snapshot=snapshot, reason=reason, object_revision=snapshot["revision"]))
    return max(versions, key=lambda version: version.object_revision, default=None)


def materialise_defaults(row):
    """A deferred flush must still expose the same IDs/defaults as a single command's reply and history."""
    columns = inspect(row).mapper.columns
    context = SimpleNamespace(get_current_parameters=lambda: {column.key: getattr(row, column.key) for column in columns})
    for column in columns:
        if getattr(row, column.key) is None and column.default is not None:
            default = column.default
            setattr(row, column.key, default.arg(context) if default.is_callable else deepcopy(default.arg))


UNDO_GRACE_SECONDS = 10


def supersede_outbox(db, actor, keys, *, prefixes=()):
    """An inverse can cancel only undispatched events, retaining their outbox evidence."""
    keys = {keys} if isinstance(keys, str) else set(keys)
    with db.no_autoflush:
        stored = list(db.scalars(select(m.Outbox).where(m.Outbox.department_id == actor.department_id,
            or_(m.Outbox.event_key.in_(keys), *(m.Outbox.event_key.startswith(prefix) for prefix in prefixes)),
            m.Outbox.state.in_(["queued", "retry"])))) if keys or prefixes else []
    rows = [row for row in [*stored, *db.new] if isinstance(row, m.Outbox)
        and row.department_id == actor.department_id and row.state in (None, "queued", "retry")
        and (row.event_key in keys or row.event_key.startswith(tuple(prefixes)))]
    for row in rows:
        row.state = "superseded"
    return bool(rows)


def outbox(db, actor, key, kind, payload, at=None, *, undoable=False, undo_of=()):
    """Hold reversible notifications briefly; an explicit inverse retracts the undispatched original."""
    if payload.get('workspace_id'):
        from .workspace_access import access_record
        workspace = db.get(m.Workspace, payload['workspace_id'])
        if workspace is not None and access_record(db, workspace)['visibility'] == 'private':
            return False
    if undo_of and supersede_outbox(db, actor, undo_of):
        return False
    moment = m.now()
    due = at or moment
    if undoable or kind == "request":
        due = max(due, moment + timedelta(seconds=UNDO_GRACE_SECONDS))
    event = m.Outbox(department_id=actor.department_id, event_key=key, kind=kind,
        payload={**payload, "actor_id": actor.identity_id}, next_attempt_at=due)
    db.add(event)
    return event


def range_overlaps(a, b, c, d):
    return (d is None or a < d) and (b is None or c < b)

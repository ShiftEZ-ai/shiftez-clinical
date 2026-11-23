"""Exact-change-bound, rollback-only catalogue impact using the shared evaluator."""
from .workspace_access import shared_clause
from dataclasses import dataclass
from zoneinfo import ZoneInfo
from sqlalchemy import and_, func, or_, select
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import json_value, record
from apps.api.snapshots import domain_snapshot, prepared_event_currency_matches, snapshot_hash, snapshot_inputs, tag_data
from packages.contracts.schemas import CATALOGUE_SCHEMAS, PersonInput
from packages.domain.time import local_midnight
from packages.domain.validation import evaluate
from packages.domain.finding_identity import finding_key, finding_index, recited_findings
from .common import CATALOGUES, PROGRAMME_CATALOGUES, scoped
from .catalogue import CatalogueWrite, programme_write_access, write_catalogue
from .schedule import LIVE

KINDS = {"catalogue.create", "catalogue.update"}
CONTACT_FIELDS = {"title", "work_phone", "work_extension", "pager", "directory_section", "directory_order"}


@dataclass(frozen=True)
class BeforeCapture:
    currency: tuple
    snapshots: dict
    event_windows: list


def contact_update(command, payload, before):
    """Only normalized contact values may differ; defaults in a partial payload still count as changes."""
    if command.kind != "catalogue.update" or payload.catalogue != "people":
        return False
    current = PersonInput.model_validate({k: before[k] for k in PersonInput.model_fields if k in before}).model_dump()
    proposed = PersonInput.model_validate(payload.value).model_dump()
    return {k for k in proposed if proposed[k] != current[k]} <= CONTACT_FIELDS


def contact_inputs(db, actor, person):
    """A people write only changes that person, department tag reconciliation and bookkeeping.

    Compare the same tag representation the domain reads, including labels and every membership; an unchanged
    tags list alone cannot prove that the real writer will leave scheduling inputs unchanged.
    """
    return {"person": {k: v for k, v in person.items() if k not in CONTACT_FIELDS | {"revision"}},
            "tags": tag_data(db, [actor.department_id])}


def operational_day(db, actor):
    org = db.get(m.Organisation, actor.organisation_id)
    return m.now().astimezone(ZoneInfo(org.timezone)).date()


def _candidate_query(actor, day):
    current = select(m.Publication.workspace_id).where(m.Publication.department_id == actor.department_id,
                                                      m.Publication.current.is_(True))
    return select(m.Workspace).where(shared_clause(), m.Workspace.department_id == actor.department_id,
        m.Workspace.ends_on > day,
        or_(and_(m.Workspace.archived_at.is_(None), m.Workspace.status.in_(LIVE)), m.Workspace.id.in_(current)))


def limits(db):
    """Configured interactive bounds (workspaces, upcoming sessions); they size the preview, never a save."""
    from apps.api.settings import Settings
    settings = db.get_bind().get_execution_options().get("rota_write_settings") or Settings()
    return settings.catalogue_preview_max_workspaces, settings.catalogue_preview_max_sessions


def scope(db, actor, *, day=None):
    """Exact SQL totals for the forward scope, and the workspaces to evaluate when it fits the bounds.

    Sessions are counted from the organisation-local day onwards: those are what a configuration change can reach.
    Above either bound nothing is evaluated (partial=True); the caller still saves through the normal checked writer.
    """
    # shortcut: the evaluator still snapshots each candidate's whole period; scope it to upcoming sessions if
    # long-running current publications make a fitting preview slow.
    day = day or operational_day(db, actor)
    candidate_query = _candidate_query(actor, day)
    query = candidate_query.subquery()
    zone = db.get(m.Organisation, actor.organisation_id).timezone
    workspace_count = db.scalar(select(func.count()).select_from(query)) or 0
    session_count = db.scalar(select(func.count()).select_from(m.Shift).where(
        m.Shift.workspace_id.in_(select(query.c.id)),
        m.Shift.start >= local_midnight(day, zone))) or 0
    max_workspaces, max_sessions = limits(db)
    partial = workspace_count > max_workspaces or session_count > max_sessions
    rows = [] if partial else list(db.scalars(candidate_query.order_by(m.Workspace.id)))
    return rows, {"workspace_count": workspace_count, "session_count": session_count, "partial": partial}


def candidates(db, actor):
    """Workspaces a complete forward impact evaluates; empty when the scope is above the interactive bounds."""
    return scope(db, actor)[0]


def _fingerprint_rows(db, actor, day):
    query = _candidate_query(actor, day).with_only_columns(
        m.Workspace.id, m.Workspace.revision, m.Workspace.content_revision,
        m.Workspace.status, m.Workspace.base_publication_id).order_by(m.Workspace.id)
    return [dict(row._mapping) for row in db.execute(query)]


def source_fingerprint(db, actor, command, *, day=None):
    """Bind both current inputs and the exact proposed content/reason/target."""
    org = db.get(m.Organisation, actor.organisation_id)
    day = day or operational_day(db, actor)
    rows = _fingerprint_rows(db, actor, day)
    change = command.model_dump(mode="json", exclude={"expected_fingerprint"})
    if change['payload'].get('catalogue') == 'recurring_series':
        change['payload'].pop('overlap_acknowledgement', None)
    return snapshot_hash({"contract": "catalogue-impact-v1", "department_id": actor.department_id,
        "scope_starts_on": day.isoformat(),
        "input_revision": org.input_revision, "availability_revision": org.availability_revision,
        "workspaces": rows,
        "change": change})


def _require_current(condition):
    require(condition,
            "STALE_PREVIEW", "The proposed change, reason, active workspaces or current inputs changed. Recalculate configuration impact.", 409)


def assert_precondition(db, actor, command):
    _require_current(source_fingerprint(db, actor, command) == command.expected_fingerprint)


def _preview_source(db, actor, command):
    from apps.api.commands import command_roles
    require(command.kind in KINDS, "PREVIEW_UNSUPPORTED", "This preview supports catalogue create/update only")
    require_role(actor, *command_roles(command))
    payload = CatalogueWrite.model_validate(command.payload)
    require(payload.catalogue in CATALOGUES, "UNKNOWN_CATALOGUE", "Unsupported catalogue")
    source = scoped(db, CATALOGUES[payload.catalogue], command.target_id, actor) if command.kind.endswith("update") else None
    if payload.catalogue in PROGRAMME_CATALOGUES:
        if source is not None:
            programme_write_access(db, actor, payload.catalogue, source)
        if payload.catalogue != "programmes":
            programme_write_access(db, actor, payload.catalogue,
                                   CATALOGUE_SCHEMAS[payload.catalogue].model_validate(payload.value))
    return payload, record(source) if source is not None else None


def _preview_token(db, actor, command, day):
    token = source_fingerprint(db, actor, command, day=day)
    if command.expected_fingerprint:
        _require_current(token == command.expected_fingerprint)
    return token


def _capture_currency(db, actor, token):
    # The public token covers input and workspace revisions. Department display and
    # catalogue revisions also appear in snapshots and can change independently.
    org = db.get(m.Organisation, actor.organisation_id)
    departments = tuple(db.execute(select(m.Department.id, m.Department.revision, m.Department.catalogue_revision)
        .where(m.Department.organisation_id == org.id).order_by(m.Department.id)))
    return actor, token, org.revision, departments


def capture_before(db, actor, command, *, skip_contact_snapshots):
    """Read the fresh operational baseline from one consistent read transaction."""
    payload, before_record = _preview_source(db, actor, command)
    day = operational_day(db, actor)
    token = _preview_token(db, actor, command, day)
    if payload.catalogue == 'recurring_series' or (
            skip_contact_snapshots and contact_update(command, payload, before_record)):
        return None
    currency = _capture_currency(db, actor, token)
    rows, _ = scope(db, actor, day=day)
    shared = snapshot_inputs(db, db.get(m.Organisation, actor.organisation_id)) if rows else None
    snapshots, event_windows = {}, []
    for row in rows:
        event_window = []
        snapshots[row.id] = domain_snapshot(db, row, operational=True, shared_inputs=shared, _event_currency=event_window)
        if event_window:
            event_windows.append(event_window)
    return BeforeCapture(currency, snapshots, event_windows)


def prepare_preview(db, actor, command, before):
    """Verify the baseline and capture proposed inputs before rolling back the trial write."""
    payload, before_record = _preview_source(db, actor, command)
    day = operational_day(db, actor)
    token = _preview_token(db, actor, command, day)
    if before is not None:
        _require_current(before.currency == _capture_currency(db, actor, token) and
                         all(prepared_event_currency_matches(db, window) for window in before.event_windows))
    # Pattern versions do not alter materialised duties. Applying/reconciling the
    # pattern has its own draft preview and validator; avoid rechecking every rota.
    rows, totals = (([], {"workspace_count": 0, "session_count": 0, "partial": False})
                    if payload.catalogue == "recurring_series" else scope(db, actor, day=day))
    if before is not None:
        _require_current(before.snapshots.keys() == {row.id for row in rows})
    metadata_only = False
    if contact_update(command, payload, before_record):
        original = contact_inputs(db, actor, before_record)
        savepoint = db.begin_nested()
        try:
            result = write_catalogue(db, actor, command, update=True, preview_only=True)
            metadata_only = original == contact_inputs(db, actor, result["record"])
        finally:
            savepoint.rollback()
    captured = []
    if not metadata_only:
        if before is None and rows:
            return None
        org = db.get(m.Organisation, actor.organisation_id)
        savepoint = db.begin_nested()
        try:
            result = write_catalogue(db, actor, command, update=command.kind.endswith("update"), preview_only=True)
            # The checked writer can reconcile tags and advance inputs: capture a fresh, separate phase.
            shared = snapshot_inputs(db, org) if rows else None
            after = {w.id: domain_snapshot(db, w, operational=True, shared_inputs=shared) for w in rows}
            # No ORM rows or session escape: validation needs only these bounded, plain snapshots.
            captured = [{"workspace_id": w.id, "name": w.name, "status": w.status,
                         "before": before.snapshots[w.id], "after": after[w.id],
                         "saved": domain_snapshot(db, w, shared_inputs=shared, mandatory_rules=False,
                                                  reuse_writer_snapshot=False) if w.status in LIVE else None} for w in rows]
        finally:
            savepoint.rollback()
    output = {"preview": True, "committed": False, "contract": "catalogue-impact-v1",
        **({key: result[key] for key in ('conflicts', 'conflict_token')} if payload.catalogue == 'recurring_series' else {}),
        "source_fingerprint": token, "before": before_record, "proposed": result["record"],
        **totals,
        "scope_starts_on": day.isoformat(),
        "scope": "Ongoing/future workspaces and current publications in this department (period end after the organisation-local date). Completed periods, discarded drafts and other departments are outside this forward operational preview. Historical issued snapshots remain unchanged.",
        "inventory_only": payload.catalogue in {"cohorts", "recurring_series"},
        "message": "Contact details checked. Scheduling inputs are unchanged; no rota validation is needed. Nothing saved or approved." if metadata_only else
            ("Impact not calculated: the upcoming sessions are more than this check covers. Nothing saved or approved; saving still validates each rota."
             if totals["partial"] else
             "Nothing saved or approved. Current-compliance comparison uses the proposed definitions; frozen sessions/publications are not rewritten. Draft type changes still require explicit refresh.")}
    from .leave_disclosure import overlap_labels
    from .contacts import redact_reply
    return {"response": redact_reply(db, actor, overlap_labels(db, actor, output)), "workspaces": captured}


def evaluate_preview(prepared):
    """Evaluate the captured proposal after its transaction released the shared write lock."""
    impacts = []
    for w in prepared["workspaces"]:
        previous = evaluate(w["before"], explain_findings=False)
        proposed = evaluate(w["after"], explain_findings=False)
        old, new = finding_index(previous), finding_index(proposed)
        introduced, resolved = new.keys() - old.keys(), old.keys() - new.keys()
        # Decorate only changed observations; re-citations need no extra evaluation.
        new_findings = finding_index(evaluate(w["after"])) if introduced else {}
        old_findings = finding_index(evaluate(w["before"])) if resolved else {}
        impacts.append({"workspace_id": w["workspace_id"], "name": w["name"], "status": w["status"],
            "before_error_count": previous["error_count"], "after_error_count": proposed["error_count"],
            "before_warning_count": previous["warning_count"], "after_warning_count": proposed["warning_count"],
            "introduced": [f for key, f in new_findings.items() if key in introduced],
            "resolved": [f for key, f in old_findings.items() if key in resolved],
            "recited": recited_findings(old, new),
            "saved_draft_validation": evaluate(w["saved"]) if w["saved"] is not None else None})
    return json_value({**prepared["response"], "impacts": impacts})

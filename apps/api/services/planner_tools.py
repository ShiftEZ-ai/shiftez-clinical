"""Planner-owned views and read-only, revision-bound comparison/impact tools."""
import copy
from collections import defaultdict
from sqlalchemy import func, select
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import json_value, record
from apps.api.snapshots import fingerprint, snapshot_hash, workspace_content
from packages.contracts import schemas as s
from packages.domain.rota_summary import normalized_check
from packages.domain.time import aware
from .common import scoped, expected
from . import table_views

PLANNER_ROLES = {"coordinator", "approver", "programme_lead", "auditor"}
PREVIEW_KINDS = {"shift.action", "shift.copy", "draft.swap", "draft.move", "shift.create", "shift.update", "series.expand", "series.reconcile", "workspace.undo", "workspace.redo"}


def workspace_id(command):
    """Resolve draft edits whose target is the workspace rather than their payload."""
    return command.payload.get('workspace_id') or command.target_id


def source_fingerprint(db, actor, workspace, command):
    """Cheap immutable preview currency; the worker still computes the full receipt."""
    from .read_projections import input_stamp
    token = input_stamp(db, workspace)
    if command.kind == 'series.reconcile':
        value = s.SeriesReconcile.model_validate(command.payload)
        series = scoped(db, m.RecurringSeries, value.series_id, actor)
        token = snapshot_hash([token, series.id, series.revision])
    return token


def command_key(command):
    return snapshot_hash([command.kind, command.target_id, command.expected_revision, command.payload])


def view_command(db, actor, command):
    require_role(actor, *PLANNER_ROLES)
    require(command.kind in {"planner_view.create", "planner_view.update", "planner_view.delete"},
            "UNKNOWN_COMMAND", "Unsupported saved-view command")
    row = None
    previous = None
    if command.kind != "planner_view.create":
        row = scoped(db, m.PlannerView, command.target_id, actor)
        table_views.require_edit(row, actor)
        expected(row, command.expected_revision)
        previous = {key: getattr(row, key) for key in ("name", "definition", "scope")}
        previous["definition"] = table_views.drop_removed_person_view_keys(previous["definition"])
    if command.kind == "planner_view.delete":
        s.Decision.model_validate(command.payload)
        if row.scope == "department":
            table_views.lock_department_views(db, actor)
        db.delete(row)
        return {"deleted_view_id": row.id, "previous_view": previous}
    payload = {"scope": row.scope, **command.payload} if row is not None else command.payload
    value = s.PlannerViewInput.model_validate(payload)
    scope = value.scope
    if scope == "department":
        require_role(actor, "admin", "coordinator")
    definition = value.definition
    table_views.validate_planner_references(db, actor, definition)
    definition_json = definition.model_dump(mode="json")
    if scope == "department" or row is not None and row.scope == "department":
        table_views.lock_department_views(db, actor)
    if row is None or scope != row.scope:
        table_views.check_limit(db, actor, m.PlannerView, scope)
    if row is None:
        row = m.PlannerView(department_id=actor.department_id, owner_id=actor.identity_id,
                            name=value.name, scope=scope, definition=definition_json)
        db.add(row)
    else:
        if row.scope == "department" and scope == "personal":
            row.owner_id = actor.identity_id
        row.name, row.definition, row.scope = value.name, definition_json, scope
        row.revision += 1
    db.flush()
    return {"view": table_views.view_record(row, actor), "previous_view": previous}


def list_views(db, actor):
    visible = m.PlannerView.scope == "department"
    if actor.permits(*PLANNER_ROLES):
        visible |= (m.PlannerView.scope == "personal") & (m.PlannerView.owner_id == actor.identity_id)
    maximum = table_views.LIMIT + table_views.DEPARTMENT_LIMIT
    rows = list(db.scalars(select(m.PlannerView).where(visible,
        m.PlannerView.department_id == actor.department_id).order_by(m.PlannerView.name, m.PlannerView.id).limit(maximum + 1)))
    return {"views": [table_views.view_record(row, actor) for row in rows[:maximum]], "partial": len(rows) > maximum}


def normalise_shift(shift):
    result = {key: copy.deepcopy(shift[key]) for key in ("assignment_type_id", "type_revision", "type_snapshot",
        "location_id", "cancelled", "locked", "attributes", "series_id")}
    result.update({"series_revision": shift.get("series_revision"), "occurrence_on": shift.get("occurrence_on"),
                   "series_snapshot": copy.deepcopy(shift.get('series_snapshot')),
                   "series_modified": shift.get("series_modified", False),
                   "cancellation_reason": shift.get("cancellation_reason")})
    result["service_ids"] = sorted(shift["service_ids"])
    result.update({key: aware(shift[key]).isoformat() for key in ("start", "end")})
    result["participants"] = sorted([{
        **{key: copy.deepcopy(person[key]) if key in person else field.get_default(call_default_factory=True)
           for key, field in s.ParticipantInput.model_fields.items()},
        "start": aware(person["start"]).isoformat(), "end": aware(person["end"]).isoformat(), "segments": [
            {key: aware(part[key]).isoformat() for key in ("start", "end")}
            for part in person.get("segments", [])]} for person in shift["participants"]], key=snapshot_hash)
    return result


def difference(before, after):
    """Exact content multiset + unambiguous anchors; never invent moved-duty lineage."""
    left = [{"id": shift["id"], "value": normalise_shift(shift)} for shift in before]
    right = [{"id": shift["id"], "value": normalise_shift(shift)} for shift in after]
    unchanged = 0
    # Preserve actual lineage first when comparing a mutation of this workspace.
    for entry in left[:]:
        match = next((other for other in right if other["id"] == entry["id"] and other["value"] == entry["value"]), None)
        if match:
            left.remove(entry); right.remove(match); unchanged += 1
    # Equal cloned shifts count once per occurrence even when all IDs differ.
    for entry in left[:]:
        match = next((other for other in right if other["value"] == entry["value"]), None)
        if match:
            left.remove(entry); right.remove(match); unchanged += 1
    changes = []

    def pair(entry, other, basis):
        changes.append({"kind": "changed", "before_id": entry["id"], "after_id": other["id"],
            "before": entry["value"], "after": other["value"], "basis": basis,
            "fields": [key for key in entry["value"] if entry["value"][key] != other["value"][key]]})
        left.remove(entry); right.remove(other)

    for entry in left[:]:
        match = next((other for other in right if other["id"] == entry["id"]), None)
        if match:
            pair(entry, match, "stable_session_id")
    anchor_fields = ("assignment_type_id", "location_id", "start", "end", "series_id")
    anchors = []
    for collection in (left, right):
        groups = defaultdict(list)
        for entry in collection:
            groups[snapshot_hash({key: entry["value"][key] for key in anchor_fields})].append(entry)
        anchors.append(groups)
    ambiguous = False
    for anchor, entries in anchors[0].items():
        others = anchors[1].get(anchor, [])
        if len(entries) == len(others) == 1:
            pair(entries[0], others[0], "unique_type_location_time_anchor")
        elif entries and others:
            ambiguous = True
    for kind, collection in (("removed", left), ("added", right)):
        for entry in collection:
            before_side = kind == "removed"
            changes.append({"kind": kind, "before_id": entry["id"] if before_side else None,
                "after_id": None if before_side else entry["id"], "before": entry["value"] if before_side else None,
                "after": None if before_side else entry["value"], "basis": "unmatched_content", "fields": []})
    return {"changes": changes, "counts": {kind: sum(change["kind"] == kind for change in changes)
            for kind in ("added", "removed", "changed")}, "unchanged": unchanged,
            "ambiguous_pairing": ambiguous, "basis": "Exact content; matching is not a merge or permission to publish"}


def score_summary(db, workspace):
    score = (normalized_check(workspace.validation) or {}).get("score")
    if not score or score['score'] is None:
        return None
    return {"score": score["score"], "publishable": score["publishable"],
            "components": {c: v["value"] for c, v in score["components"].items()},
            # A published rota is frozen: its score is the one it was published with, whatever changed since.
            "fresh": workspace.status in ("published", "superseded") or workspace.validation.get("fingerprint") == fingerprint(db, workspace)}


def score_detail(db, before, after):
    """Per score component, Balance goal and person: before, after and the change (None when either rota is unscored)."""
    from packages.domain.score import COMPONENTS
    a, b = normalized_check(before.validation) or {}, normalized_check(after.validation) or {}
    sa, sb = a.get("score"), b.get("score")
    if not sa or not sb or sa['score'] is None or sb['score'] is None:
        return None
    delta = lambda x, y: None if x is None or y is None else round(y - x, 4)
    components = [{"component": c, "before": sa["components"][c]["value"], "after": sb["components"][c]["value"],
                   "delta": delta(sa["components"][c]["value"], sb["components"][c]["value"])} for c in COMPONENTS]
    ga, gb = ({g.get("goal_id", g["policy_id"]): g for g in v.get("goals") or []} for v in (a, b))
    goals = [{"policy_id": (gb.get(pid) or ga[pid])["policy_id"], "goal_id": pid,
              "name": (gb.get(pid) or ga[pid])["name"], "unit": (gb.get(pid) or ga[pid])["unit"],
              "before": ga.get(pid, {}).get("score"), "after": gb.get(pid, {}).get("score"),
              "delta": delta(ga.get(pid, {}).get("score"), gb.get(pid, {}).get("score"))} for pid in sorted({*ga, *gb}, key=lambda i: ((gb.get(i) or ga[i])["name"], i))]
    pa, pb = sa.get("people") or {}, sb.get("people") or {}
    changed = []
    for pid in {*pa, *pb}:
        x, y = pa.get(pid, {}).get("score"), pb.get(pid, {}).get("score")
        d = delta(x, y)
        if d is None or abs(d) >= 0.1:
            changed.append({"person_id": pid, "before": x, "after": y, "delta": d})
    label = dict(db.execute(select(m.Person.id, m.Person.display_name).where(m.Person.id.in_([r["person_id"] for r in changed]))).all()) if changed else {}
    for r in changed:
        r["name"] = label.get(r["person_id"], "Someone")
    changed.sort(key=lambda r: (-(abs(r["delta"]) if r["delta"] is not None else 100.0), r["name"], r["person_id"]))
    return {"components": components, "goals": goals, "people": changed[:100]}


def compare(db, actor, workspace_id, other_id, limit=100, offset=0):
    require_role(actor, *PLANNER_ROLES)
    before = scoped(db, m.Workspace, workspace_id, actor)
    after = scoped(db, m.Workspace, other_id, actor)
    left, right = workspace_content(db, before), workspace_content(db, after)
    require(max(len(left["shifts"]), len(right["shifts"])) <= 500,
            "COMPARISON_LIMIT", "Compare workspaces within the supported 500-session bound", 409)
    result = difference(left["shifts"], right["shifts"])
    total = len(result["changes"])
    result.update({"before": record(before, exclude=("validation",)), "after": record(after, exclude=("validation",)),
        "before_fingerprint": fingerprint(db, before),
        "after_fingerprint": fingerprint(db, after),
        "scope_matches": before.starts_on == after.starts_on and before.ends_on == after.ends_on,
        # Each rota's score from its last check (None when never checked); the dialog shows the deltas.
        "scores": {"before": score_summary(db, before), "after": score_summary(db, after), "detail": score_detail(db, before, after)},
        "changes": result["changes"][offset:offset+limit], "total": total, "limit": limit, "offset": offset,
        "has_more": offset + limit < total})
    return result


def preview_fingerprint(db, actor, workspace, command):
    from apps.api.snapshots import precondition_snapshot
    current = precondition_snapshot(db, workspace)['fingerprint']
    if command.kind == 'series.reconcile':
        # Materialised duties keep their issued template; reconciliation also consumes the live template.
        value = s.SeriesReconcile.model_validate(command.payload)
        series = scoped(db, m.RecurringSeries, value.series_id, actor)
        return snapshot_hash([current, series.id, series.revision])
    return current


def assert_precondition(db, actor, command):
    if command.preview_job_id is not None:
        job = scoped(db, m.Job, command.preview_job_id, actor)
        require(job.kind == 'planner_preview' and job.status == 'succeeded', 'STALE_PREVIEW',
                'The impact preview is not ready; calculate it again.', 409)
        require(job.requested_by == actor.identity_id, 'STALE_PREVIEW',
                'Only the coordinator who calculated this preview can apply it.', 409)
        workspace = scoped(db, m.Workspace, workspace_id(command), actor)
        expected_revision = job.input_snapshot.get('planner_expected_revision')
        expected_source = job.input_snapshot.get('planner_source_fingerprint')
        require((job.result or {}).get('source_fingerprint') == expected_source,
                'STALE_PREVIEW', 'The impact preview receipt is incomplete. Calculate it again.', 409)
        require(command.expected_revision == expected_revision and command.expected_fingerprint == expected_source,
                'STALE_PREVIEW', 'The impact preview no longer matches this edit. Calculate it again.', 409)
        require(command_key(command) == job.input_snapshot.get('planner_command_key'), 'STALE_PREVIEW',
                'The proposed edit changed after preview. Calculate it again.', 409)
        require(source_fingerprint(db, actor, workspace, command) == expected_source, 'STALE_PREVIEW',
                'The rota or scheduling inputs changed after preview. Calculate it again.', 409)
        return
    if command.expected_fingerprint is None:
        return
    if command.kind in PREVIEW_KINDS:
        workspace = scoped(db, m.Workspace, workspace_id(command), actor)
        # The durable queue uses the cheap source/input stamp.  That stamp is
        # only valid when the receipt is carried through apply; accepting it
        # as a stand-alone fingerprint would bypass the preview job binding.
        require(command.expected_fingerprint != source_fingerprint(db, actor, workspace, command),
                'STALE_PREVIEW', 'Apply this edit with its completed preview receipt.', 409)
    if command.kind in {'staff_import.apply', 'department.create'}:
        return  # These handlers check their reviewed inputs after role guards, before any write.
    from . import catalogue_preview
    if command.kind in catalogue_preview.KINDS:
        catalogue_preview.assert_precondition(db, actor, command)
        return
    require(command.kind in PREVIEW_KINDS, "UNSUPPORTED_PRECONDITION", "This command has no preview fingerprint contract")
    workspace = scoped(db, m.Workspace, workspace_id(command), actor)
    require(preview_fingerprint(db, actor, workspace, command) == command.expected_fingerprint, "STALE_PREVIEW",
            "Inputs changed since the impact preview. Refresh the preview before applying this change.", 409)


def preview(db, actor, command):
    """Run the SAME bounded mutation and validation in an always-rolled-back savepoint."""
    from . import planning
    require_role(actor, "coordinator")
    require(command.kind in PREVIEW_KINDS, "PREVIEW_UNSUPPORTED", "Only supported draft mutations can be previewed")
    assert_precondition(db, actor, command)
    workspace = scoped(db, m.Workspace, workspace_id(command), actor)
    before = workspace_content(db, workspace)
    source_fingerprint = preview_fingerprint(db, actor, workspace, command)
    methods = {"shift.action": planning.shift_action, "shift.copy": planning.copy_shifts,
        "draft.swap": planning.swap, "draft.move": planning.move,
        "series.expand": planning.expand_series, "series.reconcile": planning.reconcile_series,
        "shift.create": lambda db, actor, cmd: planning.save_shift(db, actor, cmd),
        "shift.update": lambda db, actor, cmd: planning.save_shift(db, actor, cmd, update=True),
        "workspace.undo": planning.undo, "workspace.redo": planning.redo}
    savepoint = db.begin_nested()
    had_preview = '_planner_preview' in db.info
    previous_preview = db.info.get('_planner_preview')
    try:
        db.info['_planner_preview'] = True
        result = methods[command.kind](db, actor, command)
        # Every draft mutation returns the sessions its single validation pass read.
        removed = set(result['removed_shift_ids'])
        after = {row['id']: row for row in before['shifts'] if row['id'] not in removed}
        after.update((row['id'], row) for row in result['shifts'])
        preview_result = json_value({"preview": True, "committed": False, "source_revision": before["workspace"]["revision"],
            "source_fingerprint": source_fingerprint, "validation": result["validation"],
            "impact": difference(before["shifts"], sorted(after.values(), key=lambda row: (aware(row['start']), row['id']))),
            "counts": {k: v for k, v in result.items() if k.endswith("_count")},
            "message": "Nothing has been saved. Apply with the same revision and source fingerprint to revalidate atomically."},
            copy_containers=False)
    finally:
        try:
            savepoint.rollback()
        finally:
            if had_preview:
                db.info['_planner_preview'] = previous_preview
            else:
                db.info.pop('_planner_preview', None)
    return preview_result

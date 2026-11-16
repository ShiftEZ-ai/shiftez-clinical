"""Timeline gestures reuse the checked rotation writers and their existing approval and Undo paths."""
from copy import deepcopy

from packages.contracts import schemas as s
from apps.api import models as m
from apps.api.auth import person_access, require_role
from apps.api.errors import Problem, require
from apps.api.serialization import json_value, record
from apps.api.snapshots import snapshot_hash
from packages.domain.recurrence import inactive
from packages.domain.time import aware
from packages.domain.validation import evaluate
from . import catalogue, training
from .common import expected, local_date_span, scoped
from .leave_disclosure import staff_overlap_labels


def _prepare(db, actor, value):
    require_role(actor, "admin", "coordinator", "programme_lead", "staff")
    direct = actor.permits("admin", "coordinator", "programme_lead")
    source = scoped(db, m.Rotation, value.rotation_id, actor) if value.rotation_id else None
    if source is not None:
        person_access(db, actor, source.person_id)
    if direct:
        if source is not None:
            catalogue.programme_write_access(db, actor, "rotations", source)
    else:
        require(source is not None and value.action in {"move", "resize"}
                and source.person_id == actor.person_id and value.person_id == actor.person_id,
                "FORBIDDEN", "Staff may request date changes only for their own rotations", 403)
    if source is not None:
        expected(source, value.expected_revision)
    person_access(db, actor, value.person_id)
    if value.action == "template":
        template = scoped(db, m.RotationTemplate, value.template_id, actor)
        cohort = scoped(db, m.Cohort, value.cohort_id, actor)
        catalogue.programme_access(db, actor, cohort.programme_id)
        require(template.active and template.programme_id in (None, cohort.programme_id),
                "ROTATION_TEMPLATE", "Choose an active template for this programme")
        rotations, start = [], value.starts_on
        for saved in template.blocks:
            block = s.RotationBlock.model_validate(saved)
            end = training._block_end(start, block)
            rotations.append(s.RotationInput(person_id=value.person_id, cohort_id=cohort.id,
                rotation_type_id=block.rotation_type_id, service_id=block.service_id,
                department_available=block.department_available, starts_on=start, ends_on=end))
            start = end
        return rotations, source, not direct
    values = training._rotation_value(source).model_dump() if source is not None else {}
    values.update({key: getattr(value, key) for key in s.RotationInput.model_fields
                   if key in value.model_fields_set})
    values["person_id"], values["starts_on"] = value.person_id, value.starts_on
    if value.ends_on is None and source is not None:
        try:
            values["ends_on"] = value.starts_on + (source.ends_on - source.starts_on)
        except (ValueError, OverflowError):
            require(False, "ROTATION_DATES", "The rotation exceeds the supported date range")
    rotation = s.RotationInput.model_validate(values)
    if direct:
        catalogue.programme_write_access(db, actor, "rotations", rotation)
    else:
        current = training._rotation_value(source)
        require(all(getattr(rotation, key) == getattr(current, key)
                    for key in ("person_id", "cohort_id", "mentor_id", "department_available")),
                "FORBIDDEN", "A rotation request cannot change the person, cohort or attachment", 403)
    return [rotation], source, not direct


def _conflicts(db, actor, value, rotations, source):
    from .availability import rotation_leave_conflicts
    from .timeline import _check_data, prepare_conflict_check
    replace = source.id if source is not None and value.action in {"move", "resize"} else None
    for rotation in rotations:
        try:
            catalogue.validate_references(db, actor, "rotations", rotation, replace)
        except Problem as exc:
            if exc.code != "ROTATION_LEAVE_NOT_ALLOWED":
                raise
    findings = [finding for rotation in rotations
                for finding in rotation_leave_conflicts(db, actor, rotation=rotation)]
    affected = [*rotations, *([source] if replace else [])]
    start, end = min(r.starts_on for r in affected), max(r.ends_on for r in affected)
    data = _check_data(db, actor, start, end)
    lo, hi = local_date_span(start, end, data["organisation"]["timezone"])
    spans = [(rotation.person_id, *local_date_span(rotation.starts_on, rotation.ends_on,
        data["organisation"]["timezone"])) for rotation in affected]
    people = {pid for pid, _, _ in spans}
    data["focus_ids"] = [shift["id"] for shift in data["shifts"] if not inactive(shift)
        and data["types"][shift["assignment_type_id"]]["department_id"] == actor.department_id
        and aware(shift["start"]) < hi and lo < aware(shift["end"])]
    affected_shifts = {shift["id"] for shift in data["shifts"] if any(
        participant["person_id"] == pid and aware(participant["start"]) < end and start < aware(participant["end"])
        for participant in shift["participants"] for pid, start, end in spans)}
    material = lambda finding: snapshot_hash({key: val for key, val in finding.items() if key != "message"})
    # Each evaluation owns its indexes; replacing the list on evaluated data would reuse stale person caches.
    proposed = deepcopy(data)
    previous = {material(finding) for finding in evaluate(prepare_conflict_check(data, people))["findings"]}
    proposed["rotations"] = [row for row in proposed["rotations"] if row["id"] != replace]
    proposed["rotations"].extend({**rotation.model_dump(mode="json"), "id": replace or m.uid(),
        "department_id": actor.department_id} for rotation in rotations)
    for finding in evaluate(prepare_conflict_check(proposed, people))["findings"]:
        person_id, shift_id = finding.get("person_id"), finding.get("shift_id")
        peer_ids = set((finding.get("detail") or {}).get("affected_shift_ids", []))
        in_scope = (person_id in people or person_id is None and (shift_id in affected_shifts or peer_ids & affected_shifts or
            shift_id is None and finding.get("start") and any(
                aware(finding["start"]) < end and start < aware(finding["end"]) for _, start, end in spans)))
        if (in_scope and not finding["code"].startswith("ROTATION_LEAVE_")
                and material(finding) not in previous
                and (not finding.get("start") or aware(finding["start"]) < hi and lo < aware(finding["end"]))):
            findings.append(finding)
    if not actor.permits(*s.TEAM_READER_ROLES):
        findings = staff_overlap_labels(findings)
    return [s.TimelineConflict.model_validate({**finding, "detail": finding.get("detail") or {}}).model_dump(mode="json")
            for finding in findings]


def _apply(db, actor, value, rotations, source, requires_approval):
    if requires_approval:
        proposed = rotations[0]
        result = training.request_change(db, actor, s.Command(kind="rotation.request_change", payload={
            "rotation_id": source.id, "rotation_type_id": proposed.rotation_type_id,
            "service_id": proposed.service_id, "starts_on": proposed.starts_on.isoformat(),
            "ends_on": proposed.ends_on.isoformat(), "private_reason": value.reason}))
        request = result["record"]
        return {"action": value.action, "status": "submitted", "created": [], "records": [],
            "request": request, "undo": {"kind": "request.decide", "target_id": request["id"],
                "expected_revision": request["revision"], "payload": {"decision": "withdraw", "reason": "Withdraw rotation change request"}},
            "message": result["message"]}
    update = value.action in {"move", "resize"}
    previous = record(source) if source is not None else None
    results = [catalogue.write_catalogue(db, actor, s.Command(
        kind="catalogue.update" if update else "catalogue.create",
        target_id=source.id if update else None,
        expected_revision=value.expected_revision if update else None,
        payload={"catalogue": "rotations", "value": rotation.model_dump(mode="json"), "reason": value.reason}),
        update=update) for rotation in rotations]
    records = [result["record"] for result in results]
    undo = ({"kind": "catalogue.update", "target_id": records[0]["id"],
             "expected_revision": records[0]["revision"], "payload": {"catalogue": "rotations",
                "value": results[0]["previous_value"], "reason": value.reason}}
            if update else {"kind": "rotation.undo_plan", "payload": {"items": [
                {"id": row["id"], "revision": row["revision"]} for row in records], "reason": value.reason}})
    return {"action": value.action, "status": "applied", "created": [] if update else records,
        "records": records if update else [], "request": None, "undo": undo,
        "previous_records": [previous] if previous else [],
        "message": f"{len(records)} rotation{'s' if len(records) != 1 else ''} saved."}


def preview(db, actor, value):
    """Run the real checked writer in a rollback-only savepoint, including attachment and request checks."""
    value = s.TimelineRotationInput.model_validate(value)
    rotations, source, requires_approval = _prepare(db, actor, value)
    conflicts = _conflicts(db, actor, value, rotations, source)
    can_apply = not any(f["code"] == "ROTATION_LEAVE_NOT_ALLOWED" for f in conflicts)
    savepoint = db.begin_nested()
    try:
        _apply(db, actor, value, rotations, source, requires_approval)
    except Problem as exc:
        if can_apply or exc.code != "ROTATION_LEAVE_NOT_ALLOWED":
            raise
    finally:
        savepoint.rollback()
    return {"action": value.action, "rotations": [row.model_dump(mode="json") for row in rotations],
            "conflicts": conflicts, "requires_approval": requires_approval, "can_apply": can_apply}


def apply(db, actor, command):
    value = s.TimelineRotationInput.model_validate(command.payload)
    rotations, source, requires_approval = _prepare(db, actor, value)
    conflicts = _conflicts(db, actor, value, rotations, source)
    return json_value({**_apply(db, actor, value, rotations, source, requires_approval), "conflicts": conflicts})


def authorize_result(db, actor, result):
    """Receipt recovery rechecks current programme authority for both sides of a moved rotation."""
    if result.get("status") == "submitted":
        request = scoped(db, m.RequestRecord, result["request"]["id"], actor)
        require(request.person_id == actor.person_id and request.created_by == actor.identity_id,
                "FORBIDDEN", "This request is outside your current authority", 403)
    else:
        training.rotation_records_access(db, actor,
            [*result.get("created", []), *result.get("records", []), *result.get("previous_records", [])])

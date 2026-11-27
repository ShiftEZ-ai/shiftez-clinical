"""Atomic, revision-bound composition of the existing command handlers."""
from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
from functools import partial
import json
import logging
from time import monotonic
from types import SimpleNamespace
from typing import get_args
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from packages.contracts import schemas as s
from packages.contracts.rule_catalogue import catalogue_entry, slot_for_number
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import INTEGRITY_CONFLICT_MESSAGE, VALUE_INVALID_MESSAGE, Problem, require, safe_message, validation_issues
from apps.api.serialization import json_value, record
from apps.api.snapshots import snapshot_hash
from .common import CATALOGUES, PROGRAMME_CATALOGUES, scoped
from .audit_log import event_action, subject_detail
from .workspace_access import audit_envelope


log = logging.getLogger(__name__)
MODELS = {
    'programme_membership': m.ProgrammeMembership, 'rotation': m.Rotation,
    "membership": m.Membership, "eligibility": m.Eligibility, "grant": m.CapabilityGrant,
    "availability": m.Availability, "request": m.RequestRecord,
    "availability_pattern": m.AvailabilityPattern, "tag": m.Tag, "rule": m.Policy,
    "policy": m.Policy, "exception": m.PolicyException, "person": m.Person,
    "wish": m.StaffWish, "role": m.RoleBinding, "identity": m.Identity,
    "job": m.Job,
}
PRIVATE = {"private_reason", "password_hash", "token", "token_ciphertext"}
INDEPENDENCE_CODES = {"SELF_APPROVAL", "SELF_ATTESTATION", "INDEPENDENCE"}
NO_REASON = {"membership.create", "eligibility.create", "grant.create", "availability.record",
             "availability_pattern.propose", "wish.create", "wish.update", "shift.create", "shift.update"}
WORKSPACE_SOURCE = "_bulk_workspace_source"
SESSION_ACTIONS = {"shift.update", "shift.action", "shift.copy"}
BULK_CONTEXT_FIELDS = {"catalogue", "reason", "workspace_id", WORKSPACE_SOURCE}
AGGREGATE_MUTATION_FIELDS = {
    "membership.bulk_create": ("created_ids", "ended"),
    "eligibility.bulk_create": ("created",), "grant.bulk_create": ("created",),
    "eligibility.programme": ("created_ids",),
    "eligibility.undo_create": ("removed",), "grant.undo_create": ("removed",),
}


def _payload(kind, payload, reason):
    return payload if kind in NO_REASON else {**payload, "reason": reason}


def _source_record(row, actor, db):
    if isinstance(row, m.Job):
        return record(row, exclude={'input_snapshot', 'result', 'resume_checkpoint', 'resume_state',
                                   'read_metadata', 'result_summary', 'progress', 'search_control'})
    if isinstance(row, m.StaffWish):
        from .wishes import wish_record
        return wish_record(row, actor)
    exclude = PRIVATE | {'validation'} if isinstance(row, m.Workspace) and 'session_creation' in db.info.get('bulk', {}) else PRIVATE
    return record(row, exclude=exclude)


def _target(db, actor, cls, target, kind=None, payload=None, *, include_withdrawn=False):
    if cls is m.Identity:
        from .access import identity_for_change
        return identity_for_change(db, actor, target)
    row = scoped(db, cls, target, actor, include_withdrawn=include_withdrawn or kind in {
        'membership.restore', 'eligibility.restore', 'programme_membership.restore', 'rotation.restore'})
    catalogue = next((name for name in PROGRAMME_CATALOGUES if CATALOGUES[name] is cls), None)
    if catalogue:
        from .catalogue import programme_write_access
        programme_write_access(db, actor, catalogue, row)
    elif cls is m.StaffWish:
        from .wishes import require_wish_change
        require_wish_change(actor, row)
    elif cls is m.AvailabilityPattern:
        from .availability_patterns import require_pattern_change, require_pattern_undo
        if kind == "availability_pattern.undo":
            require_pattern_undo(db, actor, row)
        elif kind in {"availability_pattern.end", "availability_pattern.restore", "availability_pattern.propose"}:
            require_pattern_change(actor, row, {"availability_pattern.restore": "restore",
                "availability_pattern.end": "end"}.get(kind, "change"))
    elif cls is m.RequestRecord:
        from .availability import require_request_decision, require_request_undo
        if kind == "request.decide":
            require_request_decision(db, actor, row, (payload or {}).get("decision"))
        elif kind == "request.undo":
            require_request_undo(db, actor, row)
        elif kind == "request.create":
            from .availability import require_request_person
            require_request_person(actor, row.person_id)
    elif cls is m.Availability and kind in {"availability.retract", "availability.restore", "availability.record", "availability.undo_create"}:
        from .availability import lead_protects
        lead_protects(db, actor, row.kind, row.person_id, row.start, row.end)
    elif cls is m.RoleBinding and kind in {"role.revoke", "role.restore", "role.grant"}:
        from .access import require_role_change
        require_role_change(db, actor, row.role, grant=kind != "role.revoke")
    return row


def _model(kind, payload):
    if not kind:
        return CATALOGUES.get(payload.get("catalogue"))
    if kind.startswith("shift.") or kind == "issues.resolve":
        return m.Workspace
    if kind.startswith("catalogue."):
        return CATALOGUES.get(payload.get("catalogue"))
    if kind.startswith(("person.workload.", "person.overtime.", "calendar.protection.")):
        return m.SchedulingFact
    return MODELS.get(kind.split(".")[0])


def _target_id(kind, payload, target):
    if kind in {'rotation.remove', 'rotation.restore'} and len(payload.get('items', [])) == 1:
        return payload['items'][0]['id']
    if source := payload.get(WORKSPACE_SOURCE):
        return source.get("workspace_id") or target
    if kind.startswith("shift.") or kind == "issues.resolve":
        return target or payload.get("workspace_id")
    return target


def _bind_target(payload, field, target):
    require(field not in payload or payload[field] == target, "BULK_TARGET_MISMATCH",
            "Action inputs must refer to the selected record", 409)
    payload[field] = target


def _bound_payload(source, kind, payload, workspace_source=None, target_id=None):
    """Bind subject aliases; ordinary editable references still go through their own handlers."""
    if source is None:
        return payload
    part = dict(payload)
    person = source.id if isinstance(source, m.Person) else getattr(source, "person_id", None)
    if person is not None and kind in {"membership.create", "eligibility.create", "grant.create", "availability.record",
                                      "wish.create", "wish.update", "availability_pattern.propose", "request.create"}:
        _bind_target(part, "person_id", person)
    if kind in {"catalogue.create", "catalogue.update"} and person is not None and (
            not isinstance(source, m.Person) or kind == "catalogue.create"):
        if "person_id" in part:
            _bind_target(part, "person_id", person)
        if isinstance(part.get("value", {}), dict):
            part["value"] = dict(part.get("value", {}))
            _bind_target(part["value"], "person_id", person)
    if isinstance(source, m.RoleBinding) and kind == "role.grant":
        _bind_target(part, "identity_id", source.identity_id)
    if isinstance(source, m.Policy) and kind == "rule.propose" and "add_lines" not in part:
        require(source.kind == "catalogue", "BULK_TARGET_MISMATCH", "Action inputs must refer to the selected record", 409)
        _bind_target(part, "catalogue_id", source.definition["rule"]["id"])
        _bind_target(part, "expected_policy_id", source.id)
        part.setdefault("expected_revision", source.revision)
    if isinstance(source, m.SchedulingFact) and kind and kind.endswith(".propose"):
        for field in ("person_id", "calendar_entry_id"):
            _bind_target(part, field, getattr(source, field))
        _bind_target(part, "replaces_id", source.id)
        part.setdefault("expected_revision", source.revision)
    if isinstance(source, m.PolicyException) and kind == "exception.create":
        _bind_target(part, "person_id", source.person_id)
        _bind_target(part, "policy_id", source.policy_id)
    if isinstance(source, m.Workspace):
        if workspace_source or kind == "issues.resolve" or (kind and kind.startswith("shift.") and kind != "shift.undo_batch"):
            _bind_target(part, "workspace_id", source.id)
        if workspace_source:
            if kind == "shift.update":
                _bind_target(part, "shift_id", target_id)
            elif kind in {"shift.action", "shift.copy"}:
                _bind_target(part, "shift_ids", [target_id])
            elif kind == "issues.resolve":
                _bind_target(part, "keys", [workspace_source["issue_key"]])
    return part


def _limits(db, actor, settings):
    from .bulk_settings import limits
    return limits(db, actor, settings)


def prepare(db, actor, payload, settings, *, fingerprint=None):
    """Resolve a selection once and preserve its exact record revisions for staging and execution."""
    require(len(json.dumps(json_value(payload), separators=(",", ":")).encode()) <= 1_048_576,
            "BULK_PAYLOAD_LIMIT", "Bulk changes must fit within 1 MB", 413)
    validated = s.BulkApply.model_validate(payload)
    value = validated.model_dump(mode="json", exclude_none=True)
    if validated.selection is not None:
        value["selection"] = validated.selection.model_dump(mode="json", exclude_none=True, exclude_unset=True)
    maximum = _limits(db, actor, settings)["bulk_max_items"]
    items = value.get("items", [])
    token = None
    collection, records = None, None
    if value.get("selection"):
        from .bulk_selection import resolve_targets
        require(not items, "BULK_SELECTION_ITEMS", "Choose a selection or individual items, not both")
        resolved = resolve_targets(db, actor, value["selection"])
        value["source_selection"] = value["selection"]
        require(resolved["records"] or not fingerprint, "STALE_PREVIEW",
                "The selected records or inputs changed; preview this change again", 409)
        require(len(resolved["records"]) <= maximum, "BULK_ITEM_LIMIT",
                f"Choose at most {maximum} affected records per bulk change")
        token = resolved["token"]
        items = [{"target_id": r["id"], "expected_revision": r.get("revision"), "payload": {}}
                 for r in resolved["records"]]
        # The catalogue is part of a selection's type, never supplied by an individual row.
        collection = resolved["collection"]
        records = resolved["records"]
        if collection in CATALOGUES:
            value["common"] = {**value.get("common", {}), "catalogue": collection}
        if collection == "policy_lines":
            items = _rule_items(db, actor, value, resolved["records"])
            value["common"], value["changes"], value["item_kind"] = {}, [], "rule.propose"
    require(0 < len(items) <= maximum, "BULK_ITEM_LIMIT", f"Choose between 1 and {maximum} records per bulk change")
    _prepare_workspace_items(db, actor, value, items, collection, records)
    footprint = sum(_footprint(item, value) for item in items)
    require(footprint <= maximum, "BULK_ITEM_LIMIT", f"Choose at most {maximum} affected records per bulk change")
    value["items"] = items
    value.pop("selection", None)
    return value, items, token


def _prepare_workspace_items(db, actor, value, items, collection=None, records=None):
    """Collection rows keep their IDs; canonical planner writes are bound to their owning workspace."""
    from .bulk_selection import resolve_targets
    candidates = defaultdict(list)
    previous = {}
    checked = {}
    value.get("common", {}).pop(WORKSPACE_SOURCE, None)
    for item in items:
        supplied = item.get("payload", {}).pop(WORKSPACE_SOURCE, None)
        if isinstance(supplied, dict):
            previous[id(item)] = supplied
        if not item.get("target_id"):
            continue
        try:
            intended = _intended_commands(item, value)
        except Problem:
            continue  # Ordinary row diagnostics own malformed or forbidden commands.
        names = {kind for kind, _ in intended}
        inferred = "sessions" if names and names <= SESSION_ACTIONS else "issues" if names == {"issues.resolve"} else None
        if inferred is None:
            continue
        old = previous.get(id(item))
        if old and old.get("collection") != inferred:
            previous.pop(id(item))
            old = None
        source = collection if collection in {"sessions", "issues"} else inferred
        if source != inferred:
            continue
        try:
            _authorize_item(actor, item, value, checked)
        except Problem:
            continue
        if collection not in {"sessions", "issues"} and not old:
            # Existing planner callers send workspace targets and complete canonical payloads.
            if any(part.get("workspace_id") and (part.get("shift_id") or part.get("shift_ids") or part.get("keys"))
                   for _, part in intended):
                continue
        if source == "sessions" and names <= SESSION_ACTIONS or source == "issues" and names == {"issues.resolve"}:
            candidates[source].append(item)
    sources = {}
    native = {column.key for column in m.Shift.__table__.columns} | {"service_ids", "participants"}
    for source, pending in candidates.items():
        found = records if collection == source else resolve_targets(db, actor, {
            "collection": source, "include_ids": [item["target_id"] for item in pending]})["records"]
        indexed = {row["id"]: row for row in found}
        workspaces = {row.id: row for row in db.scalars(select(m.Workspace).where(
            m.Workspace.department_id == actor.department_id,
            m.Workspace.id.in_({row["workspace_id"] for row in found})))}
        for item in pending:
            payload = dict(item.get("payload", {}))
            old = previous.get(id(item))
            row = indexed.get(item["target_id"])
            marker = {"collection": source, "revision": old.get("revision") if old else item.get("expected_revision")}
            if row:
                workspace = workspaces[row["workspace_id"]]
                marker.update(workspace_id=workspace.id, issue_key=row.get("issue_key"))
                if not old:
                    item["expected_revision"] = workspace.revision
                # Planner replies carry native session data; resolver-only status/person projections are not write snapshots.
                sources[(source, row["id"])] = ({key: field for key, field in row.items() if key in native}
                    if source == "sessions" else row)
            item["payload"] = {**payload, WORKSPACE_SOURCE: marker}
    db.info["bulk_workspace_sources"] = sources


def _workspace_source(item):
    return item.get("payload", {}).get(WORKSPACE_SOURCE)


def _workspace_source_record(db, item):
    source = _workspace_source(item)
    initial = db.info.get("bulk_workspace_sources", {})
    state = db.info.get("bulk")
    records = state.setdefault("workspace_source_records", dict(initial)) if state is not None else initial
    result = deepcopy(records.get((source["collection"], item["target_id"]), {}))
    if result and source["collection"] == "sessions":
        result["service_ids"] = sorted(result["service_ids"])
        result["participants"] = sorted(({key: person[key] for key in s.ParticipantInput.model_fields if key in person}
                                         for person in result["participants"]), key=lambda person: person["person_id"])
    return result


def _command_footprint(kind, payload):
    def size(values):
        # Malformed aggregates are validated by their own handler as row issues.
        return len(values) if isinstance(values, list) else 1

    if kind == "rule.propose" and "add_lines" in payload:
        count = size(payload["add_lines"])
    elif kind in {"eligibility.bulk_create", "grant.bulk_create", "eligibility.programme"}:
        count = size(payload.get("person_ids", [])) * size(payload.get("service_ids", []) or payload.get("capability_ids", []))
    elif kind == "membership.bulk_create":
        count = size(payload.get("person_ids", []))
    elif kind == "tag.members":
        count = size(payload.get("add", [])) + size(payload.get("remove", []))
    elif kind and (kind.endswith(".undo_create") or kind in {'rotation.remove', 'rotation.restore'}) and "items" in payload:
        count = size(payload["items"])
    elif kind in {"catalogue.bulk_update", "request.decide_many", "policy.activate_many", "exception.create_many"}:
        count = size(payload.get("items", []))
    elif kind in {"shift.action", "shift.copy"}:
        count = size(payload.get("shift_ids", []))
    elif kind == "issues.resolve":
        count = size(payload.get("keys", []))
    else:
        count = 1
    return count


def _footprint(item, value):
    kind, payload, changes = _commands(item, value)
    count = _command_footprint(kind, payload)
    fields = [change for change in changes if change.get("field") and not change.get("item_kind")]
    actions = [change for change in changes if change not in fields]
    if actions and not fields and (not kind or not (set(payload) - BULK_CONTEXT_FIELDS)):
        count = 0
    for change in actions:
        if _tag_change(change, payload):
            supplied = change.get("value")
            if supplied is None:
                supplied = change.get("payload", {}).get("tag_ids", [])
            if isinstance(supplied, dict):
                supplied = supplied.get("tag_ids", [])
            count += len(supplied) if isinstance(supplied, list) else 1
        else:
            try:
                action, part = _action(change, payload)
            except Problem:
                # Invalid actions remain row issues in the preview, as in execution.
                count += 1
            else:
                count += _command_footprint(action, part)
    return max(1, count)


def _rule_items(db, actor, value, records):
    """Editing selected lines proposes one complete rule version per affected rule."""
    require(value.get("item_kind") in (None, "rule.propose"), "BULK_GOVERNED",
            "Rule line changes must be proposed for independent approval")
    selected = defaultdict(set)
    for row in records:
        selected[row["policy_id"]].add(row["id"])
    policies = list(db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id,
                                                      m.Policy.id.in_(selected))))
    from .scoring_defaults import seeded_rows
    definitions = {row["id"]: row["definition"] for row in seeded_rows(db, [record(policy) for policy in policies])}
    items = []
    for policy in policies:
        rule = definitions[policy.id]["rule"]
        lines = deepcopy(rule.get("lines", []))
        changes = value.get("changes", [])
        for line in lines:
            if line["id"] not in selected[policy.id]:
                continue
            for change in changes:
                field = change.get("field")
                if field == "counting":
                    entry = catalogue_entry(rule["id"], rule.get("catalogue_version", 1))
                    require(any(slot_for_number(entry, key, rule, line, line["numbers"])["kind"] == "block_quota"
                                for key in line["numbers"]), "BULK_CHANGE_UNSUPPORTED",
                            "Counting windows belong to quota lines")
                    patch = change.get("value")
                    properties = s.BULK_COLLECTIONS["policy_lines"]["fields"]["counting"]["schema"]["properties"]
                    require(change.get("how", "set") == "set" and isinstance(patch, dict) and patch
                            and patch.keys() <= properties.keys(), "BULK_CHANGE_UNSUPPORTED",
                            "Choose counting dates, repeat or the block anchor")
                    patch = {key: json_value(TypeAdapter(s.BlockQuota.model_fields[key].annotation).validate_python(part))
                             for key, part in patch.items()}
                    settings = line.setdefault("settings", {})
                    settings["block_quota"] = {**settings.get("block_quota", {}), **patch}
                    continue
                require(field in s.RuleLine.model_fields, "BULK_CHANGE_UNSUPPORTED",
                        "These rule lines have no such editable field")
                updated = _apply_value(line.get(field), change)
                if field == "numbers" and change.get("how", "set") in {"set", "give"} and isinstance(updated, dict):
                    # Omitted slots/strengths stay unchanged; explicit null still removes/inherits.
                    existing = line.get("numbers", {})
                    updated = {**existing, **{
                        key: {**existing.get(key, {}), **number} if isinstance(number, dict) else number
                        for key, number in updated.items()}}
                line[field] = updated
        from .bulk_rule_lines import proposal_payload
        proposal = proposal_payload(policy, lines, value["reason"], value.get("common", {}), definition=definitions[policy.id])
        items.append({"item_kind": "rule.propose", "target_id": policy.id,
                      "expected_revision": policy.revision, "payload": proposal, "changes": []})
    return items


def _commands(item, value):
    """Translate the universal form to ordinary commands; every actual write stays in dispatch."""
    kind = item.get("item_kind") or item.get("kind") or value.get("item_kind")
    inherited = {} if item.get("resolution_operation") else value
    payload = {**inherited.get("common", {}), **item.get("payload", {})}
    changes = [*inherited.get("changes", []), *item.get("changes", [])]
    if not kind and not payload.get("catalogue"):
        actions = {change.get("item_kind") or change.get("action") for change in changes}
        collections = {name for name, collection in s.BULK_COLLECTIONS.items()
                       if any(descriptor["kind"] == "catalogue.create"
                              and (key in actions or descriptor["kind"] in actions)
                              for key, descriptor in collection["actions"].items())}
        if len(collections) == 1:
            payload["catalogue"] = collections.pop()
        elif actions & {"membership.create", "eligibility.create", "grant.create", "availability.record"}:
            payload["catalogue"] = "people"
    require(bool(kind) or bool(changes), "BULK_KIND_REQUIRED", "Choose a command for each record")
    return kind, payload, changes


def _source_model(kind, payload, changes):
    model = _model(kind or "", payload)
    if model or kind:
        return model
    registry = s.BULK_COLLECTIONS.get(payload.get("catalogue"), {}).get("actions", {})
    actions = [registry.get(name, {}).get("kind", name) for change in changes
               if (name := change.get("item_kind") or change.get("action"))]
    if any(action in {"membership.create", "eligibility.create", "grant.create", "availability.record"} for action in actions):
        return m.Person
    return _model(actions[0], payload) if actions else None


def _action(change, payload):
    action = change.get("item_kind") or change.get("action")
    registry = s.BULK_COLLECTIONS.get(payload.get("catalogue"), {})
    actions = registry.get("actions", {})
    descriptor = actions.get(action) or next((entry for entry in actions.values() if entry["kind"] == action), {})
    action = descriptor.get("kind", action)
    require(action in s.BULK_ITEM_KINDS, "BULK_KIND_FORBIDDEN", "This command is not allowed in a bulk change")
    defaults = descriptor.get("payload", {})
    for supplied in (change.get("payload", {}), change.get("value")):
        if isinstance(supplied, dict):
            for field, target in defaults.items():
                if field in supplied:
                    _bind_target(dict(supplied), field, target)
    part = {**defaults, **change.get("payload", {})}
    if isinstance(change.get("value"), dict):
        part = {**change["value"], **part}
    part.pop(WORKSPACE_SOURCE, None)
    return action, part


def _tag_change(change, payload):
    action = change.get("item_kind") or change.get("action")
    return payload.get("catalogue") in s.TAG_KINDS and (action == "tag" or action == "tag.members" and
        not ({"add", "remove"} & change.get("payload", {}).keys()))


def _intended_commands(item, value):
    kind, payload, changes = _commands(item, value)
    changes = [c for c in changes if c.get("field") not in item.get("resolved_fields", [])]
    fields = [c for c in changes if c.get("field") and not c.get("item_kind")]
    actions = [c for c in changes if c not in fields]
    intended = [(kind, payload)] if kind and (fields or not actions or set(payload) - BULK_CONTEXT_FIELDS) else []
    intended.extend(_action(change, payload) for change in actions)
    return intended


def _authorize_item(actor, item, value, checked=None):
    """Authorize intent even when a tag change expands to no commands."""
    from apps.api.commands import authorization_command, authorize_command_context
    checked = {} if checked is None else checked
    intended = _intended_commands(item, value)
    require(bool(intended), "BULK_KIND_REQUIRED", "Choose a command for the fields being changed")
    for action, part in intended:
        require(action in s.BULK_ITEM_KINDS, "BULK_KIND_FORBIDDEN", "This command is not allowed in a bulk change")
        authorize_command_context(actor, authorization_command(SimpleNamespace(kind=action, payload=part)), checked)


def _source_row(db, actor, item, value, checked=None):
    if checked is None:
        checked = (db.info.get("bulk") or {}).get("authorized_commands")
    _authorize_item(actor, item, value, checked)
    kind, payload, changes = _commands(item, value)
    cls = _source_model(kind, payload, changes)
    target = _target_id(kind or "", payload, item.get("target_id"))
    intended = _intended_commands(item, value)
    child_creates = {"membership.create", "eligibility.create", "grant.create", "availability.record",
                     "catalogue.create", "wish.create", "availability_pattern.propose", "request.create"}
    for action, part in intended:
        other = _model(action, part)
        require(not cls or not other or cls is other or cls is m.Person and action in child_creates
                or action == "tag.members" and any(_tag_change(change, payload) for change in changes),
                "NOT_FOUND", "Record not found", 404)
    row = _target(db, actor, cls, target, kind, payload) if cls and target else None
    if source := _workspace_source(item):
        from . import planning
        from .bulk_selection import collection_readers
        require_role(actor, *collection_readers(source["collection"]))
        original = db.info.get("bulk_workspace_sources", {}).get((source["collection"], item["target_id"]))
        require(original is not None and row is not None and original["workspace_id"] == row.id,
                "NOT_FOUND", "Record not found", 404)
        initial = (db.info.get("bulk") or {}).get("advanced", {}).get((m.Workspace, row.id))
        requested = item.get("expected_revision")
        revision = row.revision if requested is not None and requested in {initial, row.revision} else requested
        planning.draft(db, actor, row.id, revision)
        require(source.get("revision") is not None, "REVISION_REQUIRED", "Use the current revision when saving", 428)
        observed = original["revision"]
        if source["collection"] == "sessions":
            current = db.get(m.Shift, item["target_id"])
            require(current is not None and current not in db.deleted and current.workspace_id == row.id,
                    "NOT_FOUND", "Record not found", 404)
            observed = current.revision
        require(source["revision"] in {original["revision"], observed}, "STALE_REVISION",
                "Another session changed this record; reload and reconcile", 409, {"current_revision": observed})
    if row is not None:
        for action, part in intended:
            if _model(action, part) is cls:
                _target(db, actor, cls, target, action, part)
    if cls is m.Person and row is not None:
        for action, part in intended:
            if action == "wish.create":
                from .wishes import require_wish_person
                require_wish_person(actor, row.id)
            elif action == "availability_pattern.propose":
                from .availability_patterns import require_pattern_person
                require_pattern_person(actor, row.id)
            elif action == "request.create":
                from .availability import require_request_person
                require_request_person(actor, row.id)
            elif action == "availability.record":
                from .availability import lead_protects
                proposed = s.AvailabilityInput.model_validate({**part, "person_id": row.id})
                lead_protects(db, actor, proposed.kind, row.id, proposed.start, proposed.end)
            elif action == "catalogue.create" and part.get("catalogue") in PROGRAMME_CATALOGUES:
                from .catalogue import CatalogueWrite, programme_write_access
                schema = s.CATALOGUE_SCHEMAS[part["catalogue"]]
                nested = part.get("value", {})
                if not isinstance(nested, dict):
                    CatalogueWrite.model_validate({"catalogue": part["catalogue"], "value": nested,
                                                  "reason": value["reason"]})
                supplied = {key: part[key] for key in schema.model_fields if key in part}
                proposed = schema.model_validate({**supplied, **nested, "person_id": row.id})
                programme_write_access(db, actor, part["catalogue"], proposed)
    return row


def _preload(db, actor, items, value):
    ids = defaultdict(set)
    reference_ids = defaultdict(set)
    for item in items:
        kind, payload, changes = _commands(item, value)
        source_model = _source_model(kind, payload, changes)
        if source_model and item.get("target_id"):
            ids[source_model].add(item["target_id"])
        if (_workspace_source(item) or {}).get("collection") == "sessions":
            ids[m.Shift].add(item["target_id"])
        kinds = [kind] if kind else []
        kinds += [c.get("item_kind") for c in changes if c.get("item_kind")]
        for name in kinds:
            model = _model(name, payload)
            target = _target_id(name, payload, item.get("target_id"))
            if model and target:
                ids[model].add(target)
        try:
            intended = _intended_commands(item, value)
        except Problem:
            intended = []  # Row diagnostics retain ownership of malformed actions.
        for action, part in intended:
            if action in {'membership.create', 'eligibility.create', 'grant.create',
                          'membership.bulk_create', 'eligibility.bulk_create', 'grant.bulk_create'}:
                for cls, field in ((m.Person, 'person_id'), (m.Service, 'service_id'),
                                   (m.Category, 'category_id'), (m.Capability, 'capability_id')):
                    if isinstance(ident := part.get(field), str):
                        reference_ids[cls].add(ident)
                    multiple = part.get(field.removesuffix('_id') + '_ids')
                    if isinstance(multiple, list):
                        reference_ids[cls].update(ident for ident in multiple if isinstance(ident, str))
    loaded = []
    for model, targets in ids.items():
        # Person access is checked by the original handler, including shared department membership.
        query = select(model).where(model.id.in_(targets))
        if model is m.Shift:
            query = query.join(m.Workspace, m.Shift.workspace_id == m.Workspace.id).where(m.Workspace.department_id == actor.department_id)
        elif model not in {m.Person, m.Identity}:
            query = query.where(model.department_id == actor.department_id)
        loaded.extend(db.scalars(query))
    for cls, field in ((m.Person, "person_id"), (m.Service, "service_id"),
                       (m.Category, "category_id"), (m.Capability, "capability_id")):
        references = reference_ids[cls] | {getattr(row, field) for row in loaded if getattr(row, field, None)}
        if references:
            query = select(cls).where(cls.id.in_(references))
            query = query.where(cls.organisation_id == actor.organisation_id) if cls is m.Person else query.where(cls.department_id == actor.department_id)
            loaded.extend(db.scalars(query))
    # SQLAlchemy's identity map is weak. Keep the preload graph alive for the
    # whole batch so scoped/reference checks do not re-read each target.
    db.info.setdefault('bulk_preloaded_rows', []).extend(loaded)
    db.info.setdefault('bulk_relationship_references', {}).update(
        {(type(row), row.id): row for row in loaded if getattr(row, 'id', None)})
    from .catalogue import prepare_created_undo
    prepare_created_undo(db, actor, items, value)
    revisions, checked = {}, {}
    for item in items:
        try:
            row = _source_row(db, actor, item, value, checked)
        except (Problem, ValidationError) as error:
            _problem(error)
            continue
        if row is not None:
            revisions[type(row), row.id] = row.revision
    db.info["bulk_source_revisions"] = revisions
    return loaded


def _fingerprint(db, actor, value, items, selection_token):
    org = db.get(m.Organisation, actor.organisation_id)
    targets, checked = [], {}
    authorized = False
    for item in items:
        kind, payload, changes = _commands(item, value)
        target = _target_id(kind or "", payload, item.get("target_id"))
        try:
            row = _source_row(db, actor, item, value, checked)
        except (Problem, ValidationError) as error:
            _problem(error)
            row = None  # Invalid or unauthorized rows must not fingerprint hidden record revisions.
        else:
            authorized = True
        targets.append((target, getattr(row, "revision", None)))
    # `only` chooses which already-previewed rows to apply; it does not alter their proposed content.
    change = {k: v for k, v in value.items() if k not in {"only", "apply_ready_only", "staged_id", "overlap_acknowledgement"}}
    return snapshot_hash({"contract": "bulk-v1", "department": actor.department_id,
        "actor": actor.identity_id, "input_revision": org.input_revision if authorized else None,
        "availability_revision": org.availability_revision if authorized else None, "selection": selection_token,
        "targets": targets, "change": change})


def _authorize_undo(db, actor, value):
    if not value.get("undo_of"):
        return
    source = db.get(m.CommandReceipt, value["undo_of"])
    require(source is not None and source.department_id == actor.department_id and source.command_kind in {
                "bulk.apply", "catalogue.bulk_update", "policy.activate", "policy.activate_many", "policy.retire"},
            "NOT_FOUND", "Bulk change receipt not found", 404)
    own_change = source.actor_id == actor.identity_id
    shared_approver = actor.permits("approver")
    require(own_change or shared_approver, "FORBIDDEN", "Only the person who made this change can undo it", 403)
    reverse = source.result.get("undo")
    require(reverse and reverse.get("kind") == "bulk.apply", "BULK_UNDO_UNAVAILABLE", "This change has no reversible operations", 409)
    stored = s.BulkApply.model_validate(reverse["payload"]).model_dump(mode="json", exclude_none=True)
    stored.pop("selection", None)
    require(value == stored, "BULK_UNDO_CHANGED", "Use the complete Undo returned by this change's receipt", 409)
    if not own_change:
        _, required = _policy_undo_requirements(db, actor, stored.get("items", []))
        require(required > 1, "FORBIDDEN", "Only the person who made this change can undo it", 403)
    return source.id


def _policy_undo_requirements(db, actor, items):
    from .policies import transition_approvals_required

    if not items or any(item.get("item_kind") not in {"policy.retire", "policy.restore"} for item in items):
        return [], 1
    targets = [scoped(db, m.Policy, item["target_id"], actor) for item in items]
    configured = db.get(m.Department, actor.department_id).policy_retire_approvals_required
    required = max(transition_approvals_required(configured, record(policy), item["item_kind"].split(".")[1])
                   for policy, item in zip(targets, items))
    return targets, required


def _policy_undo_approval(db, actor, source_id):
    """Dependent lifecycle inverses are one recorded Undo decision, before any inverse changes its prerequisites."""
    from . import independence, policies

    source = db.get(m.CommandReceipt, source_id)
    value = source.result["undo"]["payload"]
    items = value["items"]
    if len(items) < 2:
        return None
    targets, required = _policy_undo_requirements(db, actor, items)
    if required == 1:
        return None
    require_role(actor, "approver")
    for policy, item in zip(targets, items):
        require(policy.revision == item["expected_revision"], "STALE_REVISION",
                "Another session changed this record; reload and reconcile", 409)
    return policies.transition_approval(db, actor, targets[0], items[0]["item_kind"].split(".")[1],
        value["reason"], {"undo_source": source_id, "items": items}, minimum=required,
        excluded={independence.person_key(db, policy.created_by) for policy in targets if policy.created_by})


def _apply_value(current, change):
    how, value = change.get("how", "set"), change.get("value")
    if how in {"set", "give"}:
        return value
    if how == "clear":
        field = change.get("field", "")
        if field in {"starts_on", "ends_on", "start", "end"} or field.endswith("_id"):
            return None
        return [] if isinstance(current, list) else "" if isinstance(current, str) else None
    if how in {"add", "remove"}:
        values = value if isinstance(value, list) else [value]
        existing = list(current or [])
        if how == "remove":
            return [v for v in existing if v not in values]
        for value in values:
            if value not in existing:
                existing.append(value)
        return existing
    if how == "move_by":
        require(current is not None, "BULK_EMPTY_DATE", "An empty date cannot be moved")
        parsed = datetime.fromisoformat(str(current)) if "T" in str(current) else date.fromisoformat(str(current))
        days = value.get("days") if isinstance(value, dict) else value
        require(isinstance(days, int) and not isinstance(days, bool), "BULK_DATE_OFFSET", "Move dates by a whole number of days")
        try:
            return (parsed + timedelta(days=days)).isoformat()
        except (ValueError, OverflowError) as error:
            raise Problem(422, "BULK_DATE_OFFSET", "The moved date must be within the supported calendar") from error
    raise Problem(422, "BULK_CHANGE_UNSUPPORTED", "Choose a supported way to change this field")


def _expand(db, actor, item, value):
    source = _source_row(db, actor, item, value)
    kind, payload, changes = _commands(item, value)
    workspace_source = payload.pop(WORKSPACE_SOURCE, None)
    target, revision = item.get("target_id"), item.get("expected_revision")
    if workspace_source:
        target = source.id
    cls = _source_model(kind, payload, changes)
    if kind is None and cls is m.Person:
        payload.setdefault("catalogue", "people")
    source_id = _target_id(kind or "", payload, target)
    before = _source_record(source, actor, db) if source is not None else {}
    if before:
        if kind == "job.cancel" and revision is None:
            # Worker progress advances revisions while Stop is open. The job
            # handler locks the row and checks its current queued/running state.
            revision = before["revision"]
        require(revision is not None, "REVISION_REQUIRED", "Use the current revision when saving", 428)
        observed = db.info["bulk"]["advanced"].setdefault((cls, source_id), before["revision"])
        require(revision in {observed, before["revision"]}, "STALE_REVISION",
                f"{before.get('name') or before['id']}: Another session changed this record; reload and reconcile", 409,
                {"current_revision": before["revision"], "id": source_id})
    payload = _bound_payload(source, kind, payload, workspace_source, item.get("target_id"))
    if workspace_source and kind == "shift.update":
        original = _workspace_source_record(db, item)
        participants_supplied = "participants" in payload
        payload = {**{key: original[key] for key in s.ShiftInput.model_fields}, **payload,
                   "workspace_id": source.id, "shift_id": item["target_id"]}
    if kind == "rule.propose" and "add_lines" in payload:
        from .bulk_rule_lines import expand_add_lines
        payload = expand_add_lines(db, actor, item, payload, value["reason"])
    changes = [c for c in changes if c.get("field") not in item.get("resolved_fields", [])]
    fields = [c for c in changes if c.get("field") and not c.get("item_kind")]
    actions = [c for c in changes if c not in fields]
    require(not fields or kind is not None, "BULK_KIND_REQUIRED", "Choose a command for the fields being changed")
    if fields and kind == "rule.propose" and source is not None and source.kind == "catalogue":
        from .bulk_rule_lines import proposal_payload
        from .scoring_defaults import seeded_rows
        definition = seeded_rows(db, [record(source)])[0]["definition"]
        payload = proposal_payload(source, deepcopy(definition["rule"].get("lines", [])), value["reason"], payload,
                                   definition=definition)
    if fields:
        if kind == "catalogue.update":
            catalogue = payload.get("catalogue")
            schema = s.CATALOGUE_SCHEMAS.get(catalogue)
            require(schema is not None, "UNKNOWN_CATALOGUE", "Unsupported catalogue")
            proposed = deepcopy({k: before[k] for k in schema.model_fields if k in before})
            proposed.update(deepcopy(payload.get("value", {})))
            destination = proposed
        else:
            destination = payload
            if kind in {"eligibility.update_dates", "membership.update_dates", "grant.update_dates"}:
                destination = {"starts_on": before.get("starts_on"), "ends_on": before.get("ends_on"), **payload}
            elif kind == "availability.update":
                destination = {k: before.get(k) for k in ("kind", "leave_type_id", "start", "end") } | payload
            elif kind == "wish.update":
                destination = {k: before[k] for k in s.StaffWishInput.model_fields if k in before} | payload
        for change in fields:
            field = change["field"]
            require(field not in PRIVATE | {"id", "revision", "department_id", "organisation_id"},
                    "BULK_CHANGE_UNSUPPORTED", "These records have no such editable field")
            spec = s.BULK_COLLECTIONS[catalogue]["fields"].get(field, {}) if kind == "catalogue.update" else {}
            target_fields = destination.setdefault(spec["path"], {}) if spec.get("path") else destination
            require(isinstance(target_fields, dict), "SCHEMA_INVALID", "Enter a valid recurring session definition")
            target_fields[field] = _apply_value(target_fields.get(field, before.get(field)), change)
        payload = {**payload, "value": destination} if kind == "catalogue.update" else destination
    payload = _bound_payload(source, kind, payload, workspace_source, item.get("target_id"))
    if workspace_source and kind == "shift.update" and not participants_supplied and not any(
            change.get("field") == "participants" for change in fields):
        from packages.domain.time import aware
        from packages.domain.participation import shifted_segments
        interval = s.InstantInterval.model_validate({key: payload[key] for key in ("start", "end")})
        offset = interval.start - aware(original["start"])
        if offset or interval.end != aware(original["end"]):
            payload["participants"] = [{**person,
                "start": (aware(person["start"]) + offset).isoformat(),
                "end": (aware(person["end"]) + offset).isoformat(),
                "segments": shifted_segments(person, offset)} for person in payload["participants"]]
    result = []
    primary = bool(fields) or not actions or bool(set(payload) - BULK_CONTEXT_FIELDS)
    if kind and primary:
        if workspace_source and kind in {"shift.action", "shift.copy", "issues.resolve"}:
            payload.update(workspace_id=source.id, **({"keys": [workspace_source["issue_key"]]}
                if kind == "issues.resolve" else {"shift_ids": [item["target_id"]]}))
        result.append(s.Command(kind=kind, target_id=target, expected_revision=revision,
                                payload=_payload(kind, payload, value["reason"])))
    for change in actions:
        action = change.get("item_kind") or change.get("action")
        catalogue = payload.get("catalogue")
        if _tag_change(change, payload):
            from . import tags as tag_service
            how = change.get("how", "add")
            require(how in {"add", "remove", "set"}, "BULK_CHANGE_UNSUPPORTED", "Choose add, remove or set for tags")
            supplied = change.get("value")
            if supplied is None:
                supplied = change.get("payload", {}).get("tag_ids")
            if isinstance(supplied, dict):
                supplied = supplied.get("tag_ids")
            require(supplied is not None, "BULK_TAGS", "Choose tags from this department")
            supplied = supplied if isinstance(supplied, list) else [supplied]
            require(all(isinstance(tag_id, str) for tag_id in supplied), "BULK_TAGS", "Choose tags from this department")
            tag_ids = set(supplied)
            held = {row.tag_id for row in tag_service.members(db, department_id=actor.department_id,
                                                            kind=catalogue, member_id=target)}
            add = tag_ids - held if how in {"add", "set"} else set()
            remove = held - tag_ids if how == "set" else tag_ids & held if how == "remove" else set()
            require(len(add | remove) <= db.info["bulk"]["max_items"], "BULK_ITEM_LIMIT",
                    "The tag changes exceed the bulk record limit; narrow the selection")
            _tags = tag_service.department_tags(db, actor.department_id)
            for tag_id in sorted(add | remove):
                tag = scoped(db, m.Tag, tag_id, actor)
                ref = {"kind": catalogue, "id": target}
                result.append(s.Command(kind="tag.members", target_id=tag.id, expected_revision=tag.revision,
                    payload={"add": [ref] if tag_id in add else [], "remove": [ref] if tag_id in remove else [],
                             "reason": value["reason"]}))
            continue
        action, part = _action(change, payload)
        part = _bound_payload(source, action, part, workspace_source, item.get("target_id"))
        if workspace_source:
            part.update(workspace_id=source.id, **({"keys": [workspace_source["issue_key"]]}
                if action == "issues.resolve" else {"shift_ids": [item["target_id"]]}))
        if action == "catalogue.create" and cls is m.Person:
            schema = s.CATALOGUE_SCHEMAS.get(part.get("catalogue"))
            fields = {key: part.pop(key) for key in schema.model_fields if key in part} if schema else {}
            part["value"] = {**fields, **part.get("value", {})}
            result.append(s.Command(kind=action, payload=_payload(action, part, value["reason"])))
            continue
        if action in {"membership.create", "eligibility.create", "grant.create", "availability.record"}:
            result.append(s.Command(kind=action, payload=_payload(action, part, value["reason"])))
        else:
            result.append(s.Command(kind=action, target_id=target, expected_revision=revision,
                                    payload=_payload(action, part, value["reason"])))
    for index, command in enumerate(result):
        if command.kind in {'rotation.remove', 'rotation.restore'}:
            require(target is not None and 'items' not in command.payload, 'BULK_TARGET_MISMATCH',
                    'Choose one rotation per bulk row', 409)
            result[index] = command.model_copy(update={'target_id': None, 'expected_revision': None,
                'payload': {'items': [{'id': target, 'revision': revision}],
                            **{key: val for key, val in command.payload.items() if key != 'catalogue'}}})
    return result, before


@contextmanager
def _batch(db, actor, batch_id, undo_source=None, *, session_creation=False):
    previous = db.info.get("bulk")
    state = {"batch_id": batch_id, "history": [], "inputs_changed": False, "availability_changed": False,
             "started": monotonic(), "authorization_commands": [], "authorized_commands": {}, "undo_source": undo_source,
             "advanced": dict(db.info.get("bulk_source_revisions", {}))}
    # _preload runs before the execution context is opened.  Keep its reference
    # graph alive when this batch replaces db.info['bulk']; otherwise every item
    # falls back to a scoped SELECT for the same related rows.
    references = dict(db.info.get('bulk_relationship_references', {}))
    references.update((previous or {}).get("relationship_references", {}))
    if references:
        state["relationship_references"] = references
    db.info["bulk"] = state
    if session_creation:
        state['session_creation'] = {}
    try:
        if undo_source:
            state["policy_undo_approval"] = _policy_undo_approval(db, actor, undo_source)
        with db.no_autoflush:
            yield state
            if session_creation:
                _finish_session_creation(db, actor, state['session_creation'])
            for kind, row, reason, snapshot in state["history"]:
                if snapshot.get("created_at") is None:
                    snapshot["created_at"] = json_value(row.created_at or m.now())
                db.add(m.DefinitionHistory(department_id=actor.department_id, kind=kind,
                    object_id=row.id, object_revision=snapshot["revision"], snapshot=snapshot,
                    actor_id=actor.identity_id, reason=reason, batch_id=batch_id))
        db.flush()
    finally:
        if previous is None:
            db.info.pop("bulk", None)
        else:
            db.info["bulk"] = previous


def create_session(db, actor, workspace, value):
    """Keep ordinary dispatch and row validation; check the completed draft at the batch boundary."""
    from . import planning
    from apps.api.snapshots import workspace_content
    groups = db.info['bulk']['session_creation']
    if workspace.id not in groups:
        groups[workspace.id] = {'workspace': workspace, 'before': workspace_content(db, workspace), 'replies': []}
    group = groups[workspace.id]
    planning.require_change_authority(db, actor, workspace, group['before'], session_definitions=True)
    row = planning.write_shift(db, actor, workspace, value, batch=group)
    if value.participants and row.type_snapshot['definition'].get('supervision'):
        # Automatic pair selection must still see exactly the sessions preceding this row.
        planning.flush_shift_batch(db, group)
        planning.name_attached_supervisors(db, workspace, db.get(m.Shift, row.id))
    workspace.revision += 1
    workspace.content_revision += 1
    reply = {'workspace': record(workspace, exclude=('validation',)), 'shift_id': row.id,
             'shifts': [{'id': row.id}]}
    group['replies'].append(reply)
    return reply


def _compact_session_evidence(value):
    """Keep aggregate evidence once, without absent fields or a duplicated display message."""
    if isinstance(value, list):
        return [_compact_session_evidence(part) for part in value]
    if isinstance(value, dict):
        result = {key: _compact_session_evidence(part) for key, part in value.items() if part is not None}
        if result.get('message') and result.get('message') == result.get('rule_name'):
            result.pop('message')
        return result
    return value


def _finish_session_creation(db, actor, groups):
    from . import planning
    from apps.api.serialization import compact_reply
    for group in groups.values():
        planning.flush_shift_batch(db, group)
        workspace = group['workspace']
        # changed supplies the last revision increment and the one ChangeSet/Undo for the whole draft.
        workspace.revision -= 1
        workspace.content_revision -= 1
        result = planning.changed(db, actor, workspace, group['before'], 'Create session')
        shifts = {row['id']: row for row in result.pop('shifts')}
        created = {reply['shift_id'] for reply in group['replies']}
        peers = [row for sid, row in shifts.items() if sid not in created]
        _count_operations(db.info['bulk'], len(peers))
        delta = result.pop('delta', None)
        aggregates = {key: result.pop(key) for key in ('issues', 'health', 'fix_list', 'fix_list_delta') if key in result}
        summary = compact_reply(result)
        for index, reply in enumerate(group['replies']):
            reply.update(summary, shifts=[shifts[reply['shift_id']]])
            if index == 0:
                reply['shifts'].extend(peers)
                reply['affected_count'] += len(peers)
                reply.update(_compact_session_evidence(aggregates))
                if delta is not None:
                    reply['delta'] = delta


def _merge_edit_deltas(rows):
    """Collapse one-workspace child edit replies into the bulk reply delta.

    A mixed-department or over-limit bulk operation deliberately returns no
    patch; the client then reads the authoritative workspace projection.
    """
    entries = []
    workspace_ids = set()
    for row in rows:
        for reply in row.get('results', []):
            delta = reply.get('delta')
            workspace = reply.get('workspace') or {}
            if delta is None:
                return None
            if workspace.get('id'):
                workspace_ids.add(workspace['id'])
            entries.append(delta)
    if not entries or len(workspace_ids) != 1:
        return None

    unique_entries, seen_entries = [], set()
    for delta in entries:
        marker = json.dumps(delta, sort_keys=True, separators=(',', ':'))
        if marker not in seen_entries:
            seen_entries.add(marker)
            unique_entries.append(delta)
    entries = unique_entries
    merged = deepcopy(entries[0])
    expected_revision = merged.get('revision')
    shifts, removed = {}, set()
    assignments, removed_assignments = {}, set()
    findings, coverage = {}, {}
    finding_shift_scope, finding_person_scope = set(), set()
    coverage_scope, coverage_person_scope = set(), set()

    def row_key(row):
        return tuple(row.get(key) for key in ('id', 'shift_id', 'person_id', 'code', 'rule_id', 'service_id', 'start', 'end'))

    for index, delta in enumerate(entries):
        # The first child starts at the held client revision.  Each subsequent
        # child must continue exactly from the revision produced by its
        # predecessor; a gap falls back to the authoritative full read.
        if delta.get('stale') or (index and delta.get('base_revision') != expected_revision):
            return None
        for row in delta.get('shifts', []):
            shifts[row['id']] = row
            removed.discard(row['id'])
        for shift_id in delta.get('removed_shift_ids', []):
            removed.add(shift_id)
            shifts.pop(shift_id, None)
        for row in delta.get('assignments', []):
            assignments[row_key(row)] = row
        for assignment_id in delta.get('removed_assignment_ids', []):
            removed_assignments.add(assignment_id)
        f = delta.get('findings') or {}
        finding_shift_scope.update(f.get('shift_ids') or [])
        finding_person_scope.update(f.get('person_ids') or [])
        for row in f.get('items', []):
            findings[row_key(row)] = row
        c = delta.get('coverage') or {}
        coverage_scope.update(c.get('shift_ids') or [])
        coverage_person_scope.update(c.get('person_ids') or [])
        for row in c.get('items', []):
            coverage[row_key(row)] = row
        merged['revision'] = delta.get('revision')
        expected_revision = merged['revision']
        merged['summary_counts'] = delta.get('summary_counts', {})
        merged['current_fingerprint'] = delta.get('current_fingerprint', '')
        merged['current_input_revision'] = delta.get('current_input_revision')
        merged['current_availability_revision'] = delta.get('current_availability_revision')

    limits = s.EDIT_DELTA_LIMITS
    if (len(shifts) > limits['shifts'] or len(removed) > limits['shifts']
            or len(findings) > limits['findings'] or len(coverage) > limits['coverage']
            or len(finding_shift_scope) > limits['coverage'] or len(finding_person_scope) > limits['coverage']
            or len(coverage_scope) > limits['coverage'] or len(coverage_person_scope) > limits['coverage']):
        return None
    merged.update(shifts=list(shifts.values()), removed_shift_ids=sorted(removed),
                  assignments=list(assignments.values()), removed_assignment_ids=sorted(removed_assignments),
                  findings={
                      'shift_ids': sorted(finding_shift_scope), 'person_ids': sorted(finding_person_scope),
                      'items': list(findings.values())},
                  coverage={'shift_ids': sorted(coverage_scope), 'person_ids': sorted(coverage_person_scope),
                            'items': list(coverage.values())})
    return merged


def _public_row(reply):
    return reply.get("record") or reply.get("fact") or reply.get("pattern") or reply.get("wish") or reply.get("view") or reply.get("workspace") or reply.get("job") or {}


def _reverse(command, before, reply, reason):
    after = _public_row(reply)
    if reply.get("undo"):
        undo = deepcopy(reply["undo"])
        if undo['kind'] in {'rotation.remove', 'rotation.restore'}:
            return [{'item_kind': undo['kind'], 'target_id': item['id'], 'expected_revision': item['revision'],
                     'payload': {'reason': reason}} for item in undo['payload']['items']]
        if undo["kind"] == "bulk.apply":
            payload = undo["payload"]
            return [{**({"item_kind": payload["item_kind"]} if payload.get("item_kind") else {}), **i,
                     "payload": {**payload.get("common", {}), **i.get("payload", {})}}
                    for i in payload["items"]]
        return [{"item_kind": undo["kind"], "target_id": undo.get("target_id", after.get("id")),
                 "expected_revision": undo.get("expected_revision", after.get("revision")),
                 "payload": undo.get("payload", {})}]
    kind, payload = command.kind, command.payload
    base = {"target_id": after.get("id", command.target_id), "expected_revision": after.get("revision")}
    reverse, data = None, {"reason": reason}
    if kind == "catalogue.update":
        reverse = kind
        schema = s.CATALOGUE_SCHEMAS[payload["catalogue"]]
        data.update(catalogue=payload["catalogue"], value=reply.get("previous_value") or
                    {k: before[k] for k in schema.model_fields if k in before})
    elif kind == "catalogue.create":
        catalogue = payload["catalogue"]
        lifecycle = {"events": {"cancelled": True}, "event_types": {"active": False},
                     "department_content": {"active": False}}.get(catalogue)
        reverse = "catalogue.update" if lifecycle else "catalogue.undo_create"
        data.update(catalogue=catalogue)
        if lifecycle:
            # Occurrences and delivery receipts remain evidence when a creation is undone.
            schema = s.CATALOGUE_SCHEMAS[catalogue]
            data["value"] = schema.model_validate({**{key: after[key] for key in schema.model_fields if key in after},
                                                  **lifecycle}).model_dump(mode="json")
    elif kind in {"eligibility.create", "grant.create", "membership.create"}:
        reverse = kind.split(".")[0] + ".undo_create"
        data["items"] = [{"id": after["id"], "revision": after["revision"]}]
    elif kind in {"eligibility.end", "eligibility.extend", "eligibility.update_dates", "membership.end", "membership.update_dates", "grant.update_dates"}:
        reverse = kind.split(".")[0] + ".update_dates"
        data.update(starts_on=before["starts_on"], ends_on=before["ends_on"])
    elif kind == "grant.revoke":
        reverse = "grant.restore"
        data.update(starts_on=before["starts_on"], ends_on=before["ends_on"])
    elif kind == "grant.restore":
        reverse = "grant.revoke"
        data.update(starts_on=before["starts_on"], ends_on=before["ends_on"])
    elif kind == "availability.update":
        reverse = kind
        data.update({k: before[k] for k in ("kind", "leave_type_id", "start", "end") if k in before})
    elif kind in {"availability.record", "availability.restore"}:
        reverse = "availability.retract"
    elif kind == "availability.retract":
        reverse = "availability.restore"
    elif kind == "tag.members":
        reverse = kind
        data.update(add=reply["removed"], remove=reply["added"])
    elif kind == "request.decide":
        reverse = "request.undo"
    elif kind == "request.undo":
        if payload.get("batch_id"):
            decisions = {"approved": "approve", "rejected": "reject", "withdrawn": "withdraw",
                         "active": "approve", "declined": "decline"}
            return [{"item_kind": "availability_pattern.decide" if row["source"] == "pattern" else "request.decide",
                     "target_id": row["id"], "expected_revision": row["revision"],
                     "payload": {"decision": decisions[status], "reason": reason}}
                    for row, status in zip(reply["records"], reply["previous_statuses"])]
        reverse = "request.decide"
        data["decision"] = {"approved": "approve", "rejected": "reject", "withdrawn": "withdraw"}[before["status"]]
    elif kind == "rule.propose":
        reverse = "policy.decline"
    elif kind.startswith(("person.workload.", "person.overtime.", "calendar.protection.")):
        family, action = kind.rsplit(".", 1)
        if action == "undo" and before.get("status") == "active" and reply.get("restored"):
            data["predecessor_revision"] = reply["restored"]["revision"]
        reverse = family + (".restore_proposal" if action == "decline" or action == "undo" and before.get("status") == "proposed"
                            else ".decline" if action == "restore_proposal" else ".undo")
    elif kind in {"policy.activate", "policy.activate_many"}:
        activated = reply.get("records") or [after]
        inverses = [{"item_kind": "policy.retire", "target_id": r["id"], "expected_revision": r["revision"],
                     "payload": {"reason": reason}} for r in activated]
        inverses.extend({"item_kind": "policy.restore", "target_id": r["id"], "expected_revision": r["revision"],
                         "payload": {"reason": reason}} for r in reply.get("replaced", []))
        return inverses
    elif kind == "policy.retire":
        return [{"item_kind": "policy.retire", "target_id": row["id"], "expected_revision": row["revision"],
                 "payload": {"reason": reason, "retirement_reason": row["retirement_reason"]}}
                for row in reply.get("restored", [])] + [
            {"item_kind": "policy.restore", **base, "payload": data}]
    elif kind == "policy.restore":
        reverse = "policy.retire"
        data["retirement_reason"] = reply["retirement_reason"]
    elif kind == "availability_pattern.propose":
        reverse = "availability_pattern.end"
    elif kind == "availability_pattern.decide":
        reverse = "availability_pattern.undo"
    elif kind == "availability_pattern.undo":
        reverse = "availability_pattern.decide"
        data["decision"] = "approve" if before["status"] == "active" else "decline"
    elif kind == "availability_pattern.end":
        reverse = "availability_pattern.restore"
    elif kind == "availability_pattern.restore":
        reverse = "availability_pattern.end"
    elif kind == "policy.decline":
        reverse = "policy.restore_proposal"
    elif kind == "policy.restore_proposal":
        reverse = "policy.decline"
    elif kind in {"exception.create", "exception.approve", "exception.undo"}:
        reverse = "exception.end"
    elif kind == "exception.end":
        reverse = "exception.undo"
    elif kind == "exception.create_many":
        return [{"item_kind": "exception.end", "target_id": r["id"], "expected_revision": r["revision"],
                 "payload": {"reason": reason}} for r in reply["records"]]
    elif kind in {"role.grant", "role.restore"}:
        reverse = "role.revoke"
    elif kind == "role.revoke":
        reverse = "role.restore"
    elif kind in {"identity.disable", "identity.enable"}:
        reverse = "identity.enable" if kind == "identity.disable" else "identity.disable"
    elif kind == "wish.create":
        reverse = "wish.withdraw"
    elif kind == "wish.update":
        reverse = kind
        data = reply["previous"]
    elif kind in {"wish.withdraw", "wish.restore"}:
        reverse = "wish.restore" if kind == "wish.withdraw" else "wish.withdraw"
    elif kind == "shift.undo_batch":
        return []
    elif kind.startswith("shift.") or kind == "issues.resolve":
        reverse = "shift.undo_batch"
        data = {"reason": reason}
    elif kind == "catalogue.bulk_update":
        return [{"item_kind": "catalogue.update", "target_id": r["id"], "expected_revision": r["revision"],
                 "payload": {"catalogue": payload["catalogue"], "value": old, "reason": reason}}
                for r, old in zip(reply["records"], reply["previous_values"])]
    elif kind in {"eligibility.bulk_create", "grant.bulk_create", "eligibility.programme"}:
        refs = reply.get("created") or [{"id": i, "revision": 1} for i in reply.get("created_ids", [])]
        return [{"item_kind": kind.split(".")[0] + ".undo_create", "target_id": i["id"],
                 "expected_revision": i["revision"], "payload": {"items": [i], "reason": reason}} for i in refs]
    elif kind == "membership.bulk_create":
        return ([{"item_kind": "membership.undo_create", "target_id": i, "expected_revision": 1,
                  "payload": {"items": [{"id": i, "revision": 1}], "reason": reason}} for i in reply["created_ids"]]
                + [{"item_kind": "membership.update_dates", "target_id": r["id"], "expected_revision": r["revision"],
                    "payload": {"ends_on": r["ends_on"], "reason": reason}} for r in reply["ended"]])
    elif kind == "request.decide_many":
        return [{"item_kind": "availability_pattern.undo" if r["source"] == "pattern" else "request.undo",
                 "target_id": r["id"], "expected_revision": r["revision"], "payload": {"reason": reason}}
                for r in reply["records"]]
    # Undo of an undo need not offer a new Undo when its handler has no restoration transition.
    if kind.endswith(".undo_create"):
        return []
    if reverse is None:
        return []
    return [{"item_kind": reverse, **base, "payload": data}]


def governed_policy_undo(db, actor, command, result, receipt_id):
    from .policies import transition_approvals_required

    if command.kind not in {"policy.activate", "policy.activate_many", "policy.retire"}:
        return None
    if result.get("approval") and not result["approval"]["complete"]:
        return None
    items = _reverse(command, {}, result, "Undo rule decision")
    if len(items) < 2:
        return None
    retiring = (result.get("restored", []) if command.kind == "policy.retire"
                else result.get("records") or [result["record"]])
    configured = db.get(m.Department, actor.department_id).policy_retire_approvals_required
    if not any(transition_approvals_required(configured, row, "retire") > 1 for row in retiring):
        return None
    return {"kind": "bulk.apply", "payload": {
        "items": items, "undo_of": receipt_id, "reason": "Undo rule decision"}}


def _registered_reverse(kind, command, before, reply, reason):
    require(command.kind == kind and kind in s.BULK_REVERSE_KINDS,
            "BULK_UNDO_UNSUPPORTED", "This command has no reversible bulk operation")
    if s.BULK_REVERSE_KINDS[kind] is None:
        return []
    return _reverse(command, before, reply, reason)


# Each allow-listed kind declares its inverse in contracts; the functions capture that declaration.
BULK_REVERSE_FUNCTIONS = {kind: partial(_registered_reverse, kind) for kind in s.BULK_ITEM_KINDS}


def _run_item(db, actor, item, value, settings, batch_id):
    audit_keys = ("self_approved", "entered_on_behalf_by")
    previous = {key: deepcopy(db.info[key]) for key in audit_keys if key in db.info}
    try:
        return _dispatch_item(db, actor, item, value, settings, batch_id)
    except Exception:
        for key in audit_keys:
            if key in previous:
                db.info[key] = previous[key]
            else:
                db.info.pop(key, None)
        raise


def _count_operations(state, count):
    state["operations"] = state.get("operations", 0) + count
    require(state["operations"] <= state["max_items"], "BULK_ITEM_LIMIT",
            f"Choose at most {state['max_items']} affected records per bulk change")


def _dispatch_item(db, actor, item, value, settings, batch_id):
    from apps.api.commands import authorization_command, authorize_command_context, command_roles, dispatch
    state = db.info["bulk"]
    if "max_items" not in state:
        state["max_items"] = _limits(db, actor, settings)["bulk_max_items"]
    commands, before = _expand(db, actor, item, value)
    workspace_source = _workspace_source(item)
    own_before = _workspace_source_record(db, item) if workspace_source else None
    if workspace_source and workspace_source["collection"] == "issues" and (
            workspace_source["workspace_id"], workspace_source["issue_key"]) in state.get("resolved_issue_sources", set()):
        current = {**own_before, "resolved": True}
        return {"target_id": item["target_id"], "result": "ready", "before": {"resolved": False},
                "after": {"resolved": True}, "issues": [], "record": current, "results": [],
                "undoable": True, "reversible_count": 0, "irreversible_count": 0}, []
    _count_operations(state, sum(_command_footprint(command.kind, command.payload) for command in commands))
    result, reverse, current, related, removed = [], [], dict(before), {}, False
    reversible_count, irreversible_count = 0, 0
    for command in commands:
        require(command.kind in s.BULK_ITEM_KINDS, "BULK_KIND_FORBIDDEN", "This command is not allowed in a bulk change")
        authorize_command_context(actor, authorization_command(command), state.setdefault("authorized_commands", {}))
        if command.kind == "role.grant":
            require(command.payload.get("role") != "admin", "BULK_ADMIN_ROLE",
                    "Give Admin access individually through Access", 409)
        if command.kind == "person.anonymise" and settings.identity_users_file:
            managed = db.scalar(select(m.Account.id).join(m.Identity, m.Account.identity_id == m.Identity.id).where(
                m.Identity.person_id == command.target_id))
            require(managed is None, "BULK_EXTERNAL_SIDE_EFFECT",
                    "Remove this person's managed sign-in individually through Access", 409)
        if _approval(command.kind) and state.get("approval_error"):
            raise state["approval_error"]
        if command.target_id and current.get("id") == command.target_id:
            command = command.model_copy(update={"expected_revision": item.get("expected_revision")
                    if not result else current.get("revision")})
        cls = _model(command.kind, command.payload)
        source_id = _target_id(command.kind, command.payload, command.target_id)
        prior = _source_record(_target(db, actor, cls, source_id, command.kind, command.payload), actor, db) if cls and source_id else {}
        if prior:
            state = db.info["bulk"]
            advanced = state.setdefault("advanced", {})
            key = (cls, source_id)
            if key in advanced and command.expected_revision in {advanced[key], prior["revision"]}:
                command = command.model_copy(update={"expected_revision": prior["revision"]})
            else:
                advanced.setdefault(key, prior["revision"])
        if command.expected_revision is not None and prior:
            require(prior["revision"] == command.expected_revision, "STALE_REVISION",
                    f"{prior.get('name') or prior.get('id')}: Another session changed this record; reload and reconcile", 409,
                    {"current_revision": prior["revision"], "id": command.target_id})
        if command.kind in {'rotation.remove', 'rotation.restore'} and prior:
            ref = command.payload['items'][0]
            require(ref['revision'] in {state['advanced'][(cls, source_id)], prior['revision']},
                    'STALE_REVISION', 'This rotation changed; reload its current revision', 409)
            command = command.model_copy(update={'payload': {**command.payload,
                'items': [{'id': source_id, 'revision': prior['revision']}]}})
        if command.kind in {"eligibility.update_dates", "grant.update_dates", "membership.update_dates"} and not item.get("allow_shorten"):
            from .catalogue import relationship_shortening_problem
            if problem := relationship_shortening_problem(_target(db, actor, cls, source_id), command.payload):
                raise problem
        if command.kind == "shift.undo_batch":
            require(source_id not in state.get("workspace_changes", {}), "BULK_UNDO_CONFLICT",
                    "Apply edits to a rota and Undo for that rota as separate bulk changes", 409)
        history_start = len(state["history"])
        try:
            approval = state.get("policy_undo_approval")
            if approval and not approval["complete"]:
                reply = {"record": prior, "impact": [], "restored_ids": [], "restored": [], "approval": approval}
            else:
                reply = dispatch(db, actor, command, settings)
                if command.kind in {'rotation.remove', 'rotation.restore'}:
                    if reply.get('kept_records'):
                        issue = reply['kept_records'][0]
                        raise Problem(409, issue['code'], issue['message'])
                    reply['record'] = record(db.get(m.Rotation, source_id,
                                                   execution_options={'include_withdrawn': True}))
        except Problem as error:
            if error.code in INDEPENDENCE_CODES:
                raise Problem(error.status, error.code, error.message, {**_details(error),
                    "handoff_roles": command_roles(command)}) from error
            raise
        state = db.info["bulk"]
        context = authorization_command(command)
        state["authorization_commands"].append(context)
        reply["authorization_command"] = context
        if command.kind.startswith(("person.workload.", "person.overtime.", "calendar.protection.")) and command.kind.endswith(".activate"):
            require((monotonic() - state["started"]) * 1000 < settings.command_write_budget_ms * .9,
                    "BULK_TOO_SLOW", "The approval checks exceed this request's budget; split the selection and preview again", 409)
        after = _public_row(reply)
        if cls is m.Availability and prior and state.get("undo_source") and command.kind != "availability.undo_update":
            # Follow the source receipt's revisions through lifecycle inverses as well as update inverses.
            chain = state.setdefault("availability_undo_revisions", {})
            chain[source_id] = chain.get(source_id, prior["revision"]) - (after["revision"] - prior["revision"])
        if db.info.get("bulk_workspace_sources"):
            cached = state.setdefault("workspace_source_records", dict(db.info.get("bulk_workspace_sources", {})))
            for saved_row in reply.get("shifts", []):
                key = ("sessions", saved_row["id"])
                old = cached.get(key, {})
                cached[key] = {field: saved_row.get(field, old[field]) for field in old} if old else saved_row
            for removed_id in reply.get("removed_shift_ids", []):
                cached.pop(("sessions", removed_id), None)
        if command.kind == "issues.resolve":
            state.setdefault("resolved_issue_sources", set()).update((after["id"], key)
                for key in reply.get("issues", {}).get("removed", []))
        removed |= bool(before.get("id") and before["id"] in reply.get("removed_ids", []))
        if command.kind == "tag.members" and (reply.get("added") or reply.get("removed")):
            related[command.kind] = {"tag_id": after["id"], "added": reply.get("added", []), "removed": reply.get("removed", [])}
        # Reverse commands run in reverse order; each uses the revision produced by the forward command.
        pending_approval = bool(reply.get("approval") and not reply["approval"]["complete"])
        no_change = (pending_approval or (command.kind in {"eligibility.update_dates", "grant.update_dates", "membership.update_dates",
                                      "availability.update", "tag.members"}
                      and after.get("id") == prior.get("id") and after.get("revision") == prior.get("revision"))
                     or (command.kind in AGGREGATE_MUTATION_FIELDS and
                         not any(reply[key] for key in AGGREGATE_MUTATION_FIELDS[command.kind])))
        if not no_change:
            inverses = BULK_REVERSE_FUNCTIONS[command.kind](command, prior, reply, value["reason"])
            # Count transitive changes too, so an admitted change's complete Undo fits the same ceiling.
            inverse_count = sum(_command_footprint(part["item_kind"], part.get("payload", {})) for part in inverses)
            affected = max(inverse_count, reply.get("affected_count", 0),
                           len({(type(row), row.id) for _, row, _, _ in state["history"][history_start:]}))
            if cls is m.Workspace:
                affected = max(affected, len(reply.get("shifts", [])) + len(reply.get("removed_shift_ids", [])))
            reply["affected_count"] = affected
            _count_operations(state, max(0, affected - _command_footprint(command.kind, command.payload)))
            reverse[0:0] = inverses
            reversible_count += bool(inverses)
            irreversible_count += not bool(inverses)
        override_reason = command.payload.get("override_reason")
        override_reason = override_reason.strip() if isinstance(override_reason, str) else ""
        db.add(audit_envelope(db, command, reply, m.AuditEvent(department_id=actor.department_id, actor_id=actor.identity_id,
            action=event_action(command.kind, reply), target_id=command.target_id or after.get("id"),
            reason=override_reason if len(override_reason) >= 3 else value["reason"],
            detail={"batch_id": batch_id, "receipt_id": db.info.get("receipt_id"),
            **subject_detail(db, command.kind, reply, department_id=actor.department_id),
                    **({"context_check_evidence": reply["context_check_evidence"]} if reply.get("context_check_evidence") else {}),
                    **({"resolved_finding": item["resolved_issue"]} if item.get("resolved_issue") else {}),
                    **({"self_approved": used} if (used := db.info.pop("self_approved", None)) else {}),
                    **({"entered_on_behalf_by": sorted(enterers)}
                       if (enterers := db.info.pop("entered_on_behalf_by", None)) else {})},
            correlation_id=db.info.get("correlation_id") or "bulk-preview")))
        if not before or after.get("id") == before.get("id"):
            current = after or current
        elif after:
            related.setdefault(command.kind, after)
        result.append(reply)
    if removed:
        current = {}
    elif before:
        kind, payload, changes = _commands(item, value)
        cls = _source_model(kind, payload, changes)
        source_id = _target_id(kind or "", payload, item.get("target_id"))
        current = _source_record(_target(db, actor, cls, source_id,
            include_withdrawn=bool(current.get('withdrawn'))), actor, db)
    if workspace_source:
        before = own_before
        if workspace_source["collection"] == "sessions":
            current = _workspace_source_record(db, item)
            for command, reply in zip(commands, result):
                if command.kind == "shift.copy":
                    related[command.kind] = [row["id"] for row in reply.get("shifts", [])]
        else:
            workspace = db.get(m.Workspace, workspace_source["workspace_id"])
            from .issues import stored_findings
            resolved = workspace_source["issue_key"] not in stored_findings(workspace) or any(
                row["key"] == workspace_source["issue_key"] for row in workspace.accepted_issues)
            before = {**own_before, "resolved": False}
            current = {**own_before, "resolved": resolved}
    changed = {k for k in before.keys() | current.keys() if k not in PRIVATE | {"revision", "created_at"}
               and before.get(k) != current.get(k)}
    status = "unchanged" if (not (reversible_count or irreversible_count) or
                             before and not changed and not related) else "ready"
    if any(_public_row(reply).get("status") == "proposed" and command.kind.endswith(".propose")
           or reply.get("approval") and not reply["approval"]["complete"] for command, reply in zip(commands, result)):
        status = "waiting"
    return {"target_id": item.get("target_id") or current.get("id"), "result": status,
            "before": {**{k: before.get(k) for k in sorted(changed)}, **{k: None for k in related}},
            "after": {**{k: current.get(k) for k in sorted(changed)}, **related}, "issues": [],
            "record": current, "results": result, "undoable": irreversible_count == 0,
            "reversible_count": reversible_count, "irreversible_count": irreversible_count}, reverse


def _problem(error):
    # Infrastructure failures belong to the outer transaction retry/timeout handlers, never row validation.
    if isinstance(error, SQLAlchemyError) and not isinstance(error, IntegrityError):
        raise error
    if isinstance(error, Problem):
        if error.status >= 500:
            raise error
        return error
    if isinstance(error, IntegrityError):
        return Problem(409, "INTEGRITY_CONFLICT", INTEGRITY_CONFLICT_MESSAGE)
    if isinstance(error, ValidationError):
        messages = []
        for issue in validation_issues(error.errors()):
            message = issue['message'].removeprefix('Value error, ')
            messages.append(f"{issue['field']}: {message}" if issue['field'] else message)
        return Problem(422, "SCHEMA_INVALID", "; ".join(messages))
    if isinstance(error, ValueError):
        return Problem(422, "VALIDATION_ERROR", safe_message(str(error)))
    # Any other exception text can carry internal or submitted values; keep it out of row issues.
    log.warning("bulk_row_error", extra={"error_code": type(error).__name__})
    return Problem(422, "VALIDATION_ERROR", VALUE_INVALID_MESSAGE)


def _details(error):
    # Rule handlers also return lists of line issues; only mapping details contain these optional controls.
    return error.details if isinstance(error.details, dict) else {}


def _issue(error):
    error = _problem(error)
    independence = error.code in INDEPENDENCE_CODES
    roles = _details(error).get("handoff_roles", [])
    key = error.code + ":" + snapshot_hash({"message": error.message})[:12]
    issue = {"code": error.code, "message": error.message, "fixable_here": not independence,
            "group_key": key, "resolutions": [{"id": "skip", "label": "Skip", "operations": [{"skip": True}]}],
            **({"handoff": {"role": next((role for role in roles if role != "admin"), roles[0]),
                              "people": [], "route": "#requests"}} if independence and roles else {})}
    if independence and issue.get("handoff"):
        issue["resolutions"].insert(0, {"id": "send_for_approval", "label": "Send for approval", "best": True,
            "operations": [{"patch_item": {"send_for_approval": True}}]})
    if error.code == "WOULD_SHORTEN":
        issue["resolutions"] = [
            {"id": "keep", "label": "Keep the existing dates", "best": True, "operations": [{"skip": True}]},
            {"id": "shorten_anyway", "label": "Shorten anyway", "operations": [{"patch_item": {"allow_shorten": True}}]},
            *issue["resolutions"]]
    # Existing feasibility receipts only permit acknowledging unresolved findings through their ordinary handler.
    if error.code == "RULE_CHECK_UNRESOLVED" and _details(error).get("severity") == "advisory" and not (
            _details(error).get("full_feasibility_required")):
        issue["resolutions"].insert(0, {"id": "apply_anyway", "label": "Apply anyway", "operations": [
            {"patch_item": {"payload": {"acknowledge_unresolved": True}}}]})
    elif _details(error).get("severity") in {"advisory", "warning"} and not (
            _details(error).get("full_feasibility_required")):
        issue["resolutions"].insert(0, {"id": "apply_anyway", "label": "Apply anyway", "operations": [
            {"patch_item": {"resolved_issue": {"code": error.code, "message": error.message,
                                               "resolution": "apply_anyway"}}}]})
    return issue


def _relationship_resolutions(db, actor, item, value, issue):
    _source_row(db, actor, item, value)
    if (item.get("item_kind") or item.get("kind") or value.get("item_kind")) == "rule.propose":
        from .bulk_rule_lines import resolution_options
        if options := resolution_options(db, actor, item, value, issue):
            issue["resolutions"] = options
            return _group_issue(issue)
    kind, payload, _ = _commands(item, value)
    cls = _model(kind or "", payload)
    if issue["code"] in INDEPENDENCE_CODES and not issue.get("handoff"):
        from apps.api.commands import command_roles
        commands, _ = _expand(db, actor, item, value)
        if commands:
            issue = _issue(Problem(403, issue["code"], issue["message"],
                                  {"handoff_roles": command_roles(commands[0])}))
    if issue.get("handoff"):
        from .independence import today
        _authorize_item(actor, item, value, (db.info.get("bulk") or {}).get("authorized_commands"))
        role = issue["handoff"]["role"]
        day = today(db, actor.department_id)
        eligible = select(m.Identity).join(m.RoleBinding, m.RoleBinding.identity_id == m.Identity.id).where(
            m.RoleBinding.department_id == actor.department_id, m.RoleBinding.role.in_((role, "admin")),
            m.RoleBinding.revoked.is_(False), m.RoleBinding.starts_on <= day,
            (m.RoleBinding.ends_on.is_(None) | (m.RoleBinding.ends_on > day)),
            m.Identity.disabled.is_(False), m.Identity.id != actor.identity_id)
        issue["handoff"]["people"] = [{"id": r.id, "name": r.display_name} for r in db.scalars(eligible).unique()
            if not actor.person_id or r.person_id != actor.person_id]
    if cls not in {m.Eligibility, m.CapabilityGrant, m.Membership}:
        return _group_issue(issue)
    row = scoped(db, cls, item["target_id"], actor) if item.get("target_id") else None
    if issue["code"] == "WOULD_SHORTEN" and row and row.ends_on is None:
        commands, _ = _expand(db, actor, item, value)
        end = next((command.payload.get("ends_on") for command in commands if "ends_on" in command.payload), None)
        if end is not None:
            end_kind = kind.split(".")[0] + ".end" if cls is not m.CapabilityGrant else "grant.update_dates"
            issue["resolutions"] = [
                {"id": "shorten_anyway", "label": "End it on that day", "best": True,
                 "operations": [{"patch_item": {"item_kind": end_kind, "allow_shorten": True,
                                                "payload": {"ends_on": end}}}]},
                {"id": "keep", "label": "Keep it open", "operations": [{"skip": True}]}]
        return _group_issue(issue)
    if issue["code"] != "DUPLICATE_RELATIONSHIP":
        return _group_issue(issue)
    person = getattr(row, "person_id", None) or payload.get("person_id")
    key = {m.Eligibility: "service_id", m.CapabilityGrant: "capability_id", m.Membership: "category_id"}[cls]
    ref = getattr(row, key, None) or payload.get(key)
    later = list(db.scalars(select(cls).where(cls.department_id == actor.department_id, cls.person_id == person,
        getattr(cls, key) == ref, cls.id != (getattr(row, "id", None) or "")).order_by(cls.starts_on)))
    if cls is m.CapabilityGrant:
        service_id = row.service_id if row else payload.get("service_id")
        later = [candidate for candidate in later if not candidate.revoked and candidate.service_id == service_id]
    if row:
        later = [r for r in later if r.starts_on > row.starts_on]
    if not later:
        return _group_issue(issue)
    later_row = later[0]
    commands, _ = _expand(db, actor, item, value)
    proposed_end = next((c.payload.get("ends_on") for c in commands if "ends_on" in c.payload), None)
    proposed_start = next((c.payload.get("starts_on") for c in commands if c.payload.get("starts_on")),
                          json_value(row.starts_on) if row else payload.get("starts_on"))
    # Relationship ranges are half-open: the old end and new start can share a boundary.
    choices = []
    try:
        s.RelationshipUpdateDates.model_validate({"starts_on": proposed_start, "ends_on": later_row.starts_on,
                                                "reason": value["reason"]})
        can_stop = kind != "eligibility.extend" or row.ends_on is not None and later_row.starts_on > row.ends_on
        if can_stop:
            choices.append({"id": "stop_before_later", "label": "Stop before the later record", "best": True,
                            "operations": [{"patch_item": {"payload": {"ends_on": later_row.starts_on.isoformat()}}}]})
    except ValidationError:
        pass
    new_start = date.fromisoformat(proposed_end) if proposed_end else None
    if new_start and new_start > later_row.starts_on and (later_row.ends_on is None or new_start < later_row.ends_on):
        s.RelationshipUpdateDates.model_validate({"starts_on": new_start, "ends_on": later_row.ends_on,
                                                "reason": value["reason"]})
        choices.insert(0, {"id": "shift_later_start", "label": "Move the later record's start", "best": True,
            "operations": [{"item_kind": (kind or "").split(".")[0] + ".update_dates",
                "target_id": later_row.id, "expected_revision": later_row.revision,
                "payload": {"starts_on": new_start.isoformat(), "ends_on": json_value(later_row.ends_on)}}]})
    issue["resolutions"] = choices + issue["resolutions"]
    return _group_issue(issue)


def _group_issue(issue):
    issue["group_key"] = issue["code"] + ":" + snapshot_hash({
        "resolutions": [r["id"] for r in issue["resolutions"]],
        "role": (issue.get("handoff") or {}).get("role")})[:12]
    return issue


def _chosen(value, index, issues):
    resolutions = value.get("resolutions", {})
    return resolutions.get(str(index)) or next((resolutions[i["group_key"]] for i in issues if i["group_key"] in resolutions), None)


def _planned_items(value, items, suggestions=None, only=None):
    """Use one resolved execution set for proof, impact checking and writes."""
    suggestions = suggestions or {}
    plan = {}
    for index, original in enumerate(items):
        preview = suggestions.get(index, {})
        status, item, extra = "execute", original, []
        chosen = None
        if (only is not None and index not in only) or (value.get("apply_ready_only") and suggestions and
                preview.get("result") not in {"ready", "unchanged", "waiting"}):
            status = "staged"
        else:
            chosen = _chosen(value, index, preview.get("issues", []))
            if chosen == "shorten_anyway":
                item = {**item, "allow_shorten": True}
            if chosen in {"skip", "keep"}:
                status = "skipped"
            if chosen and preview.get("issues"):
                try:
                    item, extra, skip = _resolved_item(original, preview["issues"][0], chosen)
                    status = "skipped" if skip else "waiting" if item.get("send_for_approval") else "execute"
                except Exception as error:
                    plan[index] = {"item": original, "extra": [], "status": "problem", "resolution_error": _problem(error)}
                    continue
            if status == "execute" and preview.get("result") == "problem":
                status = "problem"
        plan[index] = {"item": item, "extra": extra, "status": status,
                       **({"resolution_issue": preview["issues"][0]}
                          if chosen and preview.get("issues") else {})}
    return plan


def _prepare_series_context(db, actor, value, plan, *, preview_only=False):
    from . import series_conflicts, catalogue
    pending, acknowledgements, mixed = [], [], False
    for entry in plan.values():
        if entry['status'] != 'execute':
            continue
        for item in [*entry['extra'], entry['item']]:
            try:
                intended = _intended_commands(item, value)
                mixed |= any(kind not in {'catalogue.create', 'catalogue.update', 'catalogue.bulk_update'}
                             or payload.get('catalogue') != 'recurring_series' for kind, payload in intended)
                if not any(kind in {'catalogue.create', 'catalogue.update', 'catalogue.bulk_update'}
                           and payload.get('catalogue') == 'recurring_series' for kind, payload in intended):
                    continue
                commands, _ = _expand(db, actor, item, value)
            except (Problem, ValidationError):
                continue  # The ordinary per-row diagnostics retain malformed-item errors.
            for command in commands:
                if command.payload.get('catalogue') != 'recurring_series':
                    continue
                try:
                    if command.kind == 'catalogue.bulk_update':
                        commands_for_item = catalogue.bulk_commands(db, actor, catalogue.CatalogueBulk.model_validate(command.payload))
                    elif command.kind in {'catalogue.create', 'catalogue.update'}:
                        commands_for_item = [command]
                    else:
                        continue
                    proposed = [series_conflicts.write_item(db, actor, part) for part in commands_for_item]
                except (Problem, ValidationError):
                    continue
                pending.extend(proposed)
                acknowledgements.append(command.payload.get('overlap_acknowledgement'))
    if pending and mixed:
        # Other writes can change overlap inputs mid-batch; retain the live single-write guard.
        require(not value.get('overlap_acknowledgement'), 'SERIES_OVERLAP_BATCH',
                'Preview and apply the recurring-series definitions separately from other kinds of changes', 409)
        db.info['bulk']['series_overlaps'] = None
        return None
    context = series_conflicts.prepare_writes(db, actor, pending, value.get('overlap_acknowledgement'),
        preview_only=preview_only, legacy_acknowledgement=acknowledgements[0] if len(pending) == 1 else None,
        ignore_invalid=True)
    db.info['bulk']['series_overlaps'] = context
    return context


def _series_undo_acknowledgement(db, actor, payload):
    with _batch(db, actor, None):
        context = _prepare_series_context(db, actor, payload, _planned_items(payload, payload['items']), preview_only=True)
    return {'overlap_acknowledgement': context['checked']['conflict_token']} if context and any(
        item['conflicts'] for item in context['checked']['items']) else {}


def _approval(kind):
    return kind in {"policy.activate", "policy.activate_many"} or (
        kind.startswith(("person.workload.", "person.overtime.", "calendar.protection.")) and kind.endswith(".activate"))


def _has_approvals(value, items):
    for item in items:
        kind, payload, changes = _commands(item, value)
        actions = s.BULK_COLLECTIONS.get(payload.get("catalogue"), {}).get("actions", {})
        names = [c.get("item_kind") or c.get("action") or "" for c in changes]
        if _approval(kind or "") or any(_approval(actions.get(name, {}).get("kind", name)) for name in names):
            return True
    return False


def _unchecked_input(command):
    # Existing jobs only model rule/fact overrides; future writes must not silently reuse that proof.
    inactive = {"rule.propose", "policy.decline", "policy.restore_proposal"}
    inactive.update(f"{family}.{action}" for family in ("person.workload", "person.overtime", "calendar.protection")
                    for action in ("propose", "decline", "restore_proposal"))
    return command.kind not in inactive


def _prepare_approval_context(db, actor, value, plan, settings):
    """Bind the complete executed activation union before its first mutation."""
    if settings.generation_engine != "cp_sat" or not _has_approvals(value, [p["item"] for p in plan.values()]):
        return
    from apps.api.commands import authorization_command, authorize_command_context
    from packages.domain.rule_lines import fact_policy
    from .policies import require_context_receipts, RULE_CHECK_REQUIRED_MESSAGE
    state = db.info["bulk"]
    state["max_items"] = _limits(db, actor, settings)["bulk_max_items"]
    state.pop("approval_context", None)
    state.pop("approval_error", None)
    commands = []
    for index, entry in plan.items():
        if entry["status"] != "execute":
            continue
        for item in [*entry["extra"], entry["item"]]:
            expanded, _ = _expand(db, actor, item, value)
            commands.extend((index, command) for command in expanded)
    activations = [(index, command) for index, command in commands if _approval(command.kind)]
    indexes = sorted({index for index, _ in activations})
    if not activations:
        return
    contains_facts = False
    checking_feasibility = False
    try:
        require(not any(_unchecked_input(c) for _, c in commands if not _approval(c.kind)),
                "RULE_CHECK_REQUIRED", RULE_CHECK_REQUIRED_MESSAGE, 409, {"unchecked_bulk_inputs": True})
        pending, decisions, source_records, checked_records = [], [], {}, {}
        sources = state.setdefault("approval_sources", {})
        for _, command in activations:
            authorize_command_context(actor, authorization_command(command), state.setdefault("authorized_commands", {}))
            if command.kind == "policy.activate_many":
                decision = s.PolicyManyActivation.model_validate(command.payload)
                candidates = [(item.policy_id, s.PolicyActivationDecision.model_validate(
                    decision.model_dump(exclude={"items"}))) for item in decision.items]
            elif command.kind == "policy.activate":
                candidates = [(command.target_id, s.PolicyActivationDecision.model_validate(command.payload))]
            else:
                decision = s.FactDecisionInput.model_validate({**command.payload,
                    "fact_id": command.payload.get("fact_id", command.target_id),
                    "expected_revision": command.payload.get("expected_revision", command.expected_revision)})
                candidates = [(decision.fact_id, decision)]
                contains_facts = True
            for target_id, decision in candidates:
                require(target_id not in source_records, "DUPLICATE_POLICY", "Select each policy once")
                cls = m.Policy if command.kind.startswith("policy.") else m.SchedulingFact
                source = sources.setdefault(target_id, record(_target(db, actor, cls, target_id)))
                candidate = {**source, "status": "active"}
                policy = source if cls is m.Policy else fact_policy(candidate)
                adapter = SimpleNamespace(id=target_id, revision=source["revision"],
                    kind=source["kind"], active=policy["active"],
                    starts_on=date.fromisoformat(source["starts_on"]) if source.get("starts_on") else date.min,
                    ends_on=date.fromisoformat(source["ends_on"]) if source.get("ends_on") else None,
                    definition=policy["definition"])
                pending.append((adapter, decision.reason))
                decisions.append(decision)
                source_records[target_id], checked_records[target_id] = source, candidate
        contains_rules = any(not isinstance(d, s.FactDecisionInput) for d in decisions)
        evidence = []
        if contains_facts:
            checking_feasibility = True
            evidence = require_context_receipts(db, actor, pending,
                [d.model_copy(update={"acknowledge_unresolved": False}) for d in decisions],
                source_records=source_records, checked_records=checked_records, compare_problems=False)
            checking_feasibility = False
        if contains_rules:
            # Facts have already passed non-overridable feasibility; rules retain their separate comparison gate.
            comparison = [d.model_copy(update={"acknowledge_unresolved": True}) if isinstance(d, s.FactDecisionInput)
                          else d for d in decisions]
            evidence = require_context_receipts(db, actor, pending, comparison,
                source_records=source_records, checked_records=checked_records, compare_problems=True)
        state["approval_context"] = {"bindings": {ident: {
            "source_hash": snapshot_hash(source_records[ident]), "checked_hash": snapshot_hash(checked_records[ident])}
            for ident in source_records}, "evidence": evidence,
            "feasibility_checked": contains_facts, "comparison_checked": contains_rules}
    except Problem as error:
        _problem(error)
        error = Problem(error.status, error.code, error.message, {**_details(error), "approval_indexes": indexes,
            "failed_index": indexes[0], "completed": 0,
            **({"full_feasibility_required": True} if checking_feasibility else {})})
        state["approval_error"] = error
        raise error


def _resolved_item(item, issue, chosen):
    option = next((r for r in issue["resolutions"] if r["id"] == chosen), None)
    require(option is not None, "BULK_RESOLUTION_STALE", "That suggested fix is no longer available; preview again", 409)
    changed, extra, skip = deepcopy(item), [], False
    if chosen == "apply_anyway":
        changed["resolved_issue"] = {"code": issue["code"], "message": issue["message"], "resolution": chosen}
    for operation in option["operations"]:
        if operation.get("skip"):
            skip = True
        elif operation.get("patch_item"):
            patch = operation["patch_item"]
            changed.update({k: v for k, v in patch.items() if k != "payload"})
            changed["payload"] = {**changed.get("payload", {}), **patch.get("payload", {})}
            changed["resolved_fields"] = [*changed.get("resolved_fields", []), *patch.get("payload", {})]
            changed["changes"] = [c for c in changed.get("changes", []) if c.get("field") not in patch.get("payload", {})]
        elif operation.get("item_kind"):
            extra.append({**operation, "allow_shorten": True, "resolution_operation": True})
    return changed, extra, skip


def _summary(rows):
    counts = {k: 0 for k in get_args(s.BulkPreviewItem.model_fields["result"].annotation)}
    counts.update(Counter(r["result"] for r in rows))
    groups = {}
    for row in rows:
        for issue in row["issues"]:
            group = groups.setdefault(issue["group_key"], {**issue, "indexes": [], "count": 0})
            group["indexes"].append(row["index"])
            group["count"] += 1
    return counts, list(groups.values())


def _retained_inverses(items, anonymised):
    retained = []
    for item in items:
        if item.get("target_id") in anonymised:
            continue
        if (item.get("item_kind") or item.get("kind")) == "tag.members":
            payload = {**item["payload"], **{key: [ref for ref in item["payload"].get(key, [])
                if not (ref["kind"] == "people" and ref["id"] in anonymised)] for key in ("add", "remove")}}
            if not payload["add"] and not payload["remove"]:
                continue
            item = {**item, "payload": payload}
        retained.append(item)
    return retained


def _redact_anonymised_rows(db, rows):
    anonymised = db.info.get("anonymised_records", {})
    if not anonymised:
        return

    def scrub(part):
        if isinstance(part, dict):
            ident, person = part.get("id"), part.get("person_id")
            clean = anonymised.get(ident, {}) if isinstance(ident, str) else {}
            owned = bool(clean) or isinstance(person, str) and person in anonymised
            return {key: "" if owned and key in {"note", "private_reason"} else scrub(clean.get(key, value))
                    for key, value in part.items()}
        return [scrub(value) for value in part] if isinstance(part, list) else part

    for row in rows:
        row.update(scrub(row))
        for reply in row.get("results", []):
            if reply.get("authorization_command", {}).get("kind") == "tag.members":
                refs = reply.get("added", []) + reply.get("removed", [])
                erased = [ref for ref in refs if ref["kind"] == "people" and ref["id"] in anonymised]
                if erased:
                    row["undoable"] = False
                    row["irreversible_count"] = row.get("irreversible_count", 0) + 1
                    if len(erased) == len(refs):
                        row["reversible_count"] -= 1
            if "previous_values" in reply:
                reply["previous_values"] = [{} if part["id"] in anonymised else previous
                    for part, previous in zip(reply["records"], reply["previous_values"])]
            if inverse := reply.get("undo"):
                if inverse["kind"] == "bulk.apply":
                    inverse["payload"]["items"] = _retained_inverses(inverse["payload"]["items"], anonymised)
                    if not inverse["payload"]["items"]:
                        reply["undo"] = None
                elif inverse.get("target_id") in anonymised:
                    reply["undo"] = None
        ids = {record_id for record_id in (row.get("target_id"),
            *(_public_row(reply).get("id") for reply in row.get("results", [])),
            *(part.get("id") for reply in row.get("results", []) for part in reply.get("records", []))) if record_id}
        owned = ids.intersection(anonymised)
        if owned and owned == ids:
            # Anonymisation never copies the removed personal details into its receipt.
            row.update(before={}, after={}, record=anonymised[next(iter(owned))], results=[], undoable=False)
            row["irreversible_count"] = row.get("irreversible_count", 0) + row.get("reversible_count", 0)
            row["reversible_count"] = 0
        elif owned:
            row["undoable"] = False
            row["irreversible_count"] = row.get("irreversible_count", 0) + 1


def _undo_summary(value, items, rows):
    reversible, irreversible = 0, 0
    for row in rows:
        if "reversible_count" not in row:
            kind, payload, changes = _commands(items[row["index"]], value)
            names = [kind] if kind else []
            registry = s.BULK_COLLECTIONS.get(payload.get("catalogue"), {}).get("actions", {})
            names.extend(registry.get(name, {}).get("kind", name) for change in changes
                         if (name := change.get("item_kind") or change.get("action")))
            row["undoable"] = all(s.BULK_REVERSE_KINDS.get(name) is not None for name in names)
            continue
        reversible += row.pop("reversible_count")
        irreversible += row.pop("irreversible_count")
    return {"undoable": all(row.get("undoable", True) for row in rows),
            "undo_partial": reversible > 0 and irreversible > 0,
            "reversible_count": reversible, "irreversible_count": irreversible}


def _impactful(value, items):
    for item in items:
        try:
            intended = _intended_commands(item, value)
        except Problem:
            continue  # Invalid actions are reported by their own row, before any source access.
        if any(kind.startswith(("catalogue.", "tag.", "shift.")) or kind == "issues.resolve" for kind, _ in intended):
            return True
    return False


def _session_writes(value, items, allowed):
    try:
        kinds = {kind for item in items for kind, _ in _intended_commands(item, value)}
    except (Problem, ValidationError):
        return False  # Malformed operations still use the ordinary row diagnostics.
    return bool(kinds) and kinds <= allowed


def _session_creation(value, items):
    return _session_writes(value, items, {'shift.create'})


def _references_id(value, target):
    if not target:
        return False
    if isinstance(value, dict):
        return any(_references_id(part, target) for part in value.values())
    if isinstance(value, list):
        return any(_references_id(part, target) for part in value)
    return value == target


def _metadata_only(row, kind, payload):
    from .catalogue_preview import CONTACT_FIELDS
    if payload.get("catalogue") != "people":
        return False
    if kind == "catalogue.update":
        return bool(row["after"]) and set(row["after"]) <= CONTACT_FIELDS
    if kind == "catalogue.bulk_update":
        return bool(payload.get("set")) and set(payload["set"]) <= CONTACT_FIELDS and not (
            payload.get("active") is not None or payload.get("colour") or payload.get("add_tags") or payload.get("remove_tags"))
    return False


def _check_rows(db, actor, value, items, settings, only=None, undo_source=None):
    previous, state = _copy_preview_metadata(db.info), db.info.get("bulk")
    try:
        result = _simulate_rows(db, actor, value, items, settings, only, undo_source)
        _redact_anonymised_rows(db, result[0])
        return result
    finally:
        _restore_preview_metadata(db, previous, state)


def _impact_sources(db, actor, value, items):
    from . import catalogue_preview
    from apps.api.snapshots import domain_snapshot, snapshot_inputs, workspace_content
    impactful, creation_targets, checked, draft_edits = [], {}, {}, {}
    creation_only = _session_creation(value, items)
    for item in items:
        try:
            if _impactful(value, [item]):
                source = _source_row(db, actor, item, value, checked)
                if isinstance(source, m.Workspace):
                    if creation_only:
                        creation_targets[source.id] = source
                    if source.id not in draft_edits and source.status == 'draft' and any(
                            kind in {'shift.create', 'shift.update'} for kind, _ in _intended_commands(item, value)):
                        draft_edits[source.id] = workspace_content(db, source)
                impactful.append(item)
        except (Problem, ValidationError) as error:
            _problem(error)
            pass  # The ordinary per-row path diagnoses inaccessible or malformed sources without snapshots.
    targets = list(creation_targets.values()) if creation_only else catalogue_preview.candidates(db, actor) if impactful else []
    extra = creation_targets.keys() - {w.id for w in targets}
    if extra:
        # Draft edits still report findings when their rota period has already ended.
        targets.extend(creation_targets[identity] for identity in sorted(extra))
    if creation_only or extra:
        max_workspaces, max_sessions = catalogue_preview.limits(db)
        require(len(targets) <= max_workspaces, 'CONFIG_IMPACT_LIMIT',
                'The complete batch impact exceeds the workspace limit; split the batch', 409)
        count = db.scalar(select(func.count()).select_from(m.Shift).where(m.Shift.workspace_id.in_([w.id for w in targets])))
        require(count <= max_sessions, 'CONFIG_IMPACT_LIMIT',
                'The complete batch impact exceeds the session limit; split the batch', 409)
    org = db.get(m.Organisation, actor.organisation_id) if targets else None
    shared = snapshot_inputs(db, org) if targets else None
    before = {w.id: domain_snapshot(db, w, operational=True, shared_inputs=shared) for w in targets}
    return targets, before, draft_edits


def _admit_session_edits(db, rows, draft_edits):
    from . import planning

    edited = defaultdict(list)
    for row in rows:
        if row['result'] != 'ready':
            continue
        for reply in row.get('results', []):
            kind = reply.get('authorization_command', {}).get('kind')
            workspace_id = (reply.get('workspace') or {}).get('id')
            if kind in {'shift.create', 'shift.update'} and workspace_id in draft_edits:
                edited[workspace_id].append((row, kind, reply['shift_id']))
    for workspace_id, changes in edited.items():
        workspace = db.get(m.Workspace, workspace_id)
        before = draft_edits[workspace_id]
        try:
            # The dispatched writes already checked authority. Compare the whole batch so fixes can land together.
            planning.admit_assignment_change(db, None, workspace, before, 'BULK_EDIT_INVALID', staffing_gaps_only=True)
        except Problem as error:
            details = _details(error)
            finding_ids = {finding.get('shift_id') for finding in details.get('findings', [])}
            affected = [change for change in changes if change[2] in finding_ids] or changes
            for row, kind, _ in affected:
                if row['result'] == 'problem':
                    continue
                code = 'CREATE_INVALID' if error.code == 'BULK_EDIT_INVALID' and kind == 'shift.create' else error.code
                row['issues'].insert(0, _group_issue(_issue(Problem(409, code, error.message))))
                row.update(result='problem', _refusal_details=details)


_SESSION_EDITS = {kind for kind in s.BULK_ITEM_KINDS if kind.startswith('shift.') or kind == 'issues.resolve'}


def _attribute_impacts(db, actor, value, items, rows, sources, undo_source=None):
    from . import catalogue_preview
    from apps.api.snapshots import domain_snapshot, snapshot_inputs
    targets, before, draft_edits = sources
    _admit_session_edits(db, rows, draft_edits)
    if _session_writes(value, items, _SESSION_EDITS):
        admitted = {(reply.get('workspace') or {}).get('id') for row in rows for reply in row.get('results', [])}
        targets = [workspace for workspace in targets if workspace.id in admitted]
    org = db.get(m.Organisation, actor.organisation_id) if targets else None
    shared = snapshot_inputs(db, org) if targets else None
    captured = [{"workspace_id": w.id, "name": w.name, "status": w.status, "before": before[w.id],
                 "after": domain_snapshot(db, w, operational=True, shared_inputs=shared),
                 "saved": None} for w in targets]
    impacts = catalogue_preview.evaluate_preview({"response": {}, "workspaces": captured})["impacts"] if captured else []
    warnings = [{"workspace_id": impact["workspace_id"], **finding} for impact in impacts for finding in impact["introduced"]]
    creation_only = _session_creation(value, items)
    session_only = _session_writes(value, items, {'shift.create', 'shift.update'})
    if session_only:
        known = {(finding.get('workspace_id'), finding.get('code'), finding.get('shift_id'),
                  finding.get('person_id'), finding.get('start'), finding.get('end')) for finding in warnings}
        for row in rows:
            for reply in row.get('results', []):
                workspace_id = (reply.get('workspace') or {}).get('id')
                for finding in (reply.get('validation') or {}).get('findings', []):
                    key = (workspace_id, finding.get('code'), finding.get('shift_id'),
                           finding.get('person_id'), finding.get('start'), finding.get('end'))
                    if key not in known:
                        warnings.append({'workspace_id': workspace_id, **finding})
                        known.add(key)
    statuses = {impact['workspace_id']: impact['status'] for impact in impacts}
    restored_findings = {}
    if undo_source:
        source = db.get(m.CommandReceipt, undo_source)
        restored_findings = {impact['workspace_id']: set(catalogue_preview.finding_index({'findings': impact['resolved']}))
                             for impact in source.result.get('impacts', [])}
    attribution = defaultdict(list)
    created_rows = defaultdict(list)
    potential = []
    for row in rows:
        if row["result"] != "ready":
            continue
        item = items[row["index"]]
        kind, payload, changes = _commands(item, value)
        if _metadata_only(row, kind, payload):
            continue
        potential.append(row)
        cls = _source_model(kind, payload, changes)
        target = row.get("target_id")
        shift_ids = {r["id"] for reply in row.get("results", []) for r in reply.get("shifts", [])}
        if creation_only:
            for shift_id in shift_ids:
                created_rows[shift_id].append(row)
            continue
        for finding in warnings:
            relevant = _references_id(finding, target) or finding.get("shift_id") in shift_ids
            if cls in {m.Service, m.Location, m.AssignmentType}:
                for snapshot in captured:
                    shift = next((shift for shift in snapshot["after"]["shifts"] if shift["id"] == finding.get("shift_id")), None)
                    if shift:
                        relevant |= target in shift.get("service_ids", []) if cls is m.Service else shift.get(
                            "location_id" if cls is m.Location else "assignment_type_id") == target
            if relevant:
                attribution[id(finding)].append(row)
    for finding in warnings:
        if creation_only:
            attribution[id(finding)] = created_rows.get(finding.get('shift_id'), [])
        # Some findings name the resulting session rather than the changed calendar/rank/pool.
        # Keep an unattributable new problem visible on every potentially relevant change.
        for row in attribution[id(finding)] or potential:
            row.setdefault("impact_findings", []).append(finding)
    for row in rows:
        own = row.pop("impact_findings", [])
        if not own:
            continue
        # The validator owns the code and message; mandatory findings never acquire an override operation.
        try:
            require_role(actor, *s.PERMISSIONS["schedule.read_team"])
        except Problem as denied:
            issues = [_group_issue(_issue(denied))]
        else:
            issues = [_group_issue(_issue(Problem(409, f["code"], f["message"], {"severity": f.get("severity"), "finding": f}))) for f in own]
        row["issues"].extend(issues)
        chosen = _chosen(value, row["index"], issues)
        advisory = actor.permits(*s.PERMISSIONS["schedule.read_team"]) and all(f.get("severity") in {"advisory", "warning"} for f in own)
        edited_drafts = {reply['workspace']['id'] for reply in row.get('results', [])
                          if reply.get('authorization_command', {}).get('kind') in {'shift.create', 'shift.update'}
                          and reply.get('workspace', {}).get('status') == 'draft'}
        report_only = session_only and actor.permits(
            *s.PERMISSIONS['schedule.read_team']) and all(
            f['workspace_id'] in edited_drafts and statuses[f['workspace_id']] == 'draft' for f in own)
        replies = row.get('results', [])
        restored_drafts = {reply['workspace']['id'] for reply in replies
                           if reply.get('authorization_command', {}).get('kind') == 'shift.undo_batch'
                           and reply.get('workspace', {}).get('status') == 'draft'}
        # Only the receipt's resolved mandatory findings may return; new or worse mandatory findings still block.
        report_only |= bool(undo_source and replies and len(restored_drafts) == len(replies) and all(
            f['workspace_id'] in restored_drafts and statuses[f['workspace_id']] == 'draft'
            and (f.get('severity') in {'advisory', 'warning'}
                 or catalogue_preview.finding_key({k: v for k, v in f.items() if k != 'workspace_id'})
                    in restored_findings.get(f['workspace_id'], set())) for f in own))
        row["result"] = "ready" if report_only or advisory and (chosen == "apply_anyway" or undo_source) else "skipped" if chosen == "skip" else "problem"
    return rows, warnings if actor.permits(*s.PERMISSIONS["schedule.read_team"]) else [], impacts if actor.permits(*s.PERMISSIONS["schedule.read_team"]) else []


def _simulate_rows(db, actor, value, items, settings, only=None, undo_source=None):
    """Use the existing operational catalogue evaluator for the combined proposed change."""
    sources = _impact_sources(db, actor, value, items)
    rows = _preview_rows(db, actor, value, items, settings, only, undo_source)
    result = _attribute_impacts(db, actor, value, items, rows, sources, undo_source)
    trial = db.info.pop("_bulk_preview_trial", None)
    if trial:
        savepoint, previous, state, plan = trial
        for row in result[0]:
            entry = plan.get(row["index"], {})
            if not row.get("issues") and entry.get("resolution_issue"):
                row["issues"] = [entry["resolution_issue"]]
        stamped_organisations = tuple((db.info.get("_snapshot_input_revisions") or {})
                                      .get("organisations", {}).values())
        savepoint.rollback()
        _restore_preview_metadata(db, previous, state)
        # Rollback restores the row but leaves before_flush counters in the identity map.
        with db.no_autoflush:
            for organisation in stamped_organisations:
                db.expire(organisation, ["input_revision", "availability_revision"])
                db.refresh(organisation, ["input_revision", "availability_revision"])
        try:
            _prepare_approval_context(db, actor, value, plan, settings)
        except Problem as error:
            _problem(error)
            for row in result[0]:
                if row["index"] in _details(error).get("approval_indexes", []):
                    issue = _relationship_resolutions(db, actor, items[row["index"]], value, _issue(error))
                    row.update(result="problem", issues=[issue])
    return result


def _copy_preview_metadata(value):
    # Copy mutable bookkeeping and queued snapshots without cloning their ORM records.
    if isinstance(value, dict):
        copied = value.copy()
        copied.update((key, _copy_preview_metadata(part)) for key, part in value.items())
        return copied
    if isinstance(value, (list, tuple, set)):
        return type(value)(_copy_preview_metadata(part) for part in value)
    return value


def _restore_preview_metadata(db, previous, state):
    # Listener registrations survive SQL rollback; keep their registration markers too.
    listeners = {key: value for key, value in db.info.items() if isinstance(key, str) and key.endswith("_events")}
    if state is not None:
        state.clear()
        state.update(previous["bulk"])
        previous["bulk"] = state
    db.info.clear()
    db.info.update(previous)
    db.info.update(listeners)
    db.info.pop('_planning_group_authority_contexts', None)


@contextmanager
def _preview_savepoint(db):
    savepoint = db.begin_nested()
    previous = _copy_preview_metadata(db.info)
    state = db.info.get("bulk")
    try:
        yield
        savepoint.commit()
    except Exception:
        savepoint.rollback()
        _restore_preview_metadata(db, previous, state)
        raise


def _preview_rows(db, actor, value, items, settings, only=None, undo_source=None):
    resolutions = value.get("resolutions", {})
    if all(key.isdigit() and choice in {"shorten_anyway", "skip", "keep"} for key, choice in resolutions.items()):
        try:
            rows = []
            with _preview_savepoint(db), _batch(db, actor, db.info.get("receipt_id"), undo_source,
                                              session_creation=_session_creation(value, items)):
                _prepare_series_context(db, actor, value, _planned_items(value, items, only=only), preview_only=True)
                _prepare_approval_context(db, actor, value, _planned_items(value, items, only=only), settings)
                for index, original in enumerate(items):
                    if only is not None and index not in only:
                        continue
                    chosen = resolutions.get(str(index))
                    if chosen in {"skip", "keep"}:
                        row = {"target_id": original.get("target_id"), "result": "skipped", "before": {}, "after": {}, "issues": []}
                    else:
                        item = {**original, "allow_shorten": True} if chosen == "shorten_anyway" else original
                        row, _ = _run_item(db, actor, item, value, settings, None)
                    rows.append({"index": index, **row})
            return rows
        except Exception as error:
            _problem(error)
    return _preview_diagnostics(db, actor, value, items, settings, only, undo_source)


def _preview_diagnostics(db, actor, value, items, settings, only=None, undo_source=None):
    rows = []
    with _batch(db, actor, db.info.get("receipt_id"), undo_source,
                session_creation=_session_creation(value, items)) as state:
        plan = _planned_items(value, items, only=only)
        _prepare_series_context(db, actor, value, plan, preview_only=True)
        union_issues = {}
        try:
            _prepare_approval_context(db, actor, value, plan, settings)
        except Problem as error:
            _problem(error)
            # Union failures remain on all activation rows; ordinary handler problems are diagnosed below.
            indexes = _details(error).get("approval_indexes", [])
            issue = _group_issue(_issue(error))
            union_issues = {index: {"result": "ready", "issues": [issue]} for index in indexes}
            plan = _planned_items(value, items, union_issues, only)
            try:
                _prepare_approval_context(db, actor, value, plan, settings)
            except (Problem, ValidationError) as error:
                _problem(error)
                pass
        except ValidationError:
            pass
        trial, previous, trial_state = db.begin_nested(), _copy_preview_metadata(db.info), db.info.get("bulk")
        for index, original in enumerate(items):
            if only is not None and index not in only:
                continue
            entry = plan[index]
            original = entry["item"]
            if error := entry.get("resolution_error"):
                rows.append({"index": index, "target_id": original.get("target_id"), "result": "problem",
                             "before": {}, "after": {}, "issues": [_group_issue(_issue(error))]})
                continue
            if entry["status"] in {"skipped", "waiting"}:
                rows.append({"index": index, "target_id": original.get("target_id"), "result": entry["status"],
                             "before": {}, "after": {}, "issues": union_issues.get(index, {}).get("issues", [])})
                continue
            try:
                with _preview_savepoint(db):
                    row, _ = _run_item(db, actor, original, value, settings, None)
                    if original.get("resolved_issue"):
                        row.update(issues=union_issues.get(index, {}).get("issues", row.get("issues", [])))
            except Exception as caught:
                error = _problem(caught)
                try:
                    issue = _relationship_resolutions(db, actor, original, value, _issue(error))
                except Exception as caught:
                    issue = _group_issue(_issue(_problem(caught)))
                row = {"target_id": original.get("target_id"), "result": "problem",
                       "before": _details(error).get("before", {}),
                       "after": _details(error).get("after", {}), "issues": [issue]}
                if error.code in {"UNASSIGN_INVALID", "RELEASE_UNAVAILABLE_INVALID"}:
                    row["_refusal_details"] = _details(error)
                chosen = _chosen(value, index, [issue])
                if chosen:
                    item = original
                    try:
                        with _preview_savepoint(db):
                            item, extra, skip = _resolved_item(original, issue, chosen)
                            if skip:
                                row["result"] = "skipped"
                            elif item.get("send_for_approval"):
                                row["result"] = "waiting"
                            else:
                                for more in extra:
                                    _run_item(db, actor, more, value, settings, None)
                                row, _ = _run_item(db, actor, item, value, settings, None)
                                row["issues"] = [issue]
                    except Exception as caught:
                        try:
                            next_issue = _relationship_resolutions(db, actor, item, value, _issue(_problem(caught)))
                        except Exception as issue_error:
                            next_issue = _group_issue(_issue(_problem(issue_error)))
                        row["result"], row["issues"] = "problem", [next_issue]
            rows.append({"index": index, **row})
        # Independence, Skip and ready-only may have removed a compensating activation.
        plan = _planned_items(value, items, {row["index"]: row for row in rows}, only)
        context = _prepare_series_context(db, actor, value, plan, preview_only=True)
        if context:
            by_series = {item['series_id']: item for item in context['checked']['items'] if item['series_id']}
            for row in rows:
                for reply in row.get('results', []):
                    checked = by_series.get(reply.get('record', {}).get('id'))
                    if checked is not None:
                        reply.update(overlap_conflicts=checked['conflicts'], conflicts=checked['conflicts'],
                                     conflict_token=context['checked']['conflict_token'])
        db.info["_bulk_preview_trial"] = (trial, previous, trial_state, plan)
    return rows


def preview(db, actor, command, settings):
    from apps.api.commands import command_roles
    from .leave_disclosure import overlap_labels
    require_role(actor, *command_roles(command))
    from .bulk_staged import authorize_payload, project_result
    payload = authorize_payload(db, actor, command.payload) if command.payload.get("staged_id") else command.payload
    value, items, token = prepare(db, actor, payload, settings)
    undo_source = _authorize_undo(db, actor, value)
    loaded = _preload(db, actor, items, value)
    fingerprint = _fingerprint(db, actor, value, items, token)
    only = set(value["only"]) if value.get("only") is not None else None
    savepoint = db.begin_nested()
    try:
        rows, warnings, impacts = _check_rows(db, actor, value, items, settings, only, undo_source)
        for row in rows:
            row.pop("_refusal_details", None)
            if row["result"] == "skipped":
                row["result"] = "unchanged"
        counts, groups = _summary(rows)
        undo = _undo_summary(value, items, rows)
        result = project_result(db, actor, value, {"preview": True, "kind": "bulk.apply", "source_fingerprint": fingerprint,
                "fingerprint": fingerprint, "expected_fingerprint": fingerprint,
                "items": rows, "rows": rows, "counts": counts, "groups": groups, "count": len(rows),
                "warnings": warnings, "impacts": impacts, "undo": None, **undo})
        return overlap_labels(db, actor, result)
    finally:
        savepoint.rollback()


def apply(db, actor, command, settings):
    from .bulk_staged import authorize_payload, project_result
    payload = authorize_payload(db, actor, command.payload, for_apply=True) if command.payload.get("staged_id") else command.payload
    fingerprint = command.expected_fingerprint or command.payload.get("fingerprint")
    value, items, token = prepare(db, actor, payload, settings, fingerprint=fingerprint)
    undo_source = _authorize_undo(db, actor, value)
    source_stage = db.get(m.BulkStaged, value["staged_id"]) if value.get("staged_id") else None
    stage_owner = source_stage.owner_id if source_stage else None
    stage_review_role = source_stage.review_role if source_stage and source_stage.status == "waiting" else None
    loaded = _preload(db, actor, items, value)
    require(token is None or bool(fingerprint), "PREVIEW_REQUIRED", "Preview the selection before applying it", 428)
    if fingerprint:
        require(fingerprint == _fingerprint(db, actor, value, items, token), "STALE_PREVIEW",
                "The selected records or inputs changed; preview this change again", 409)
    only = set(value["only"]) if value.get("only") is not None else None
    require(only is None or all(0 <= i < len(items) for i in only), "BULK_INDEX", "Choose existing rows to apply")
    suggestions = {}
    resolutions = value.get("resolutions", {})
    resolve_issues = any(not key.isdigit() or choice not in {"shorten_anyway", "skip", "keep"}
                         for key, choice in resolutions.items())
    # A stored inverse still needs live impact checking when related records changed after its source receipt.
    impactful = _impactful(value, items)
    creation_only = _session_creation(value, items)
    check_after_write = creation_only or bool(
        undo_source and impactful and not resolve_issues and not value.get("apply_ready_only"))
    if resolve_issues or value.get("apply_ready_only") or impactful and not check_after_write:
        check = db.begin_nested()
        try:
            checked, _, _ = _check_rows(db, actor, value, items, settings, only, undo_source)
            suggestions = {r["index"]: r for r in checked}
        finally:
            check.rollback()
    plan = _planned_items(value, items, suggestions, only)
    if impactful and not check_after_write:
        ready = {index for index, entry in plan.items() if entry["status"] == "execute"}
        if ready != (only if only is not None else set(range(len(items)))) and ready:
            check = db.begin_nested()
            try:
                checked, _, _ = _check_rows(db, actor, value, items, settings, ready, undo_source)
                for row in checked:
                    if row["result"] in {"problem", "skipped"}:
                        issue = row["issues"][0]
                        raise Problem(409, issue["code"], issue["message"],
                                      {**row.get("_refusal_details", {}), "failed_index": row["index"],
                                       "completed": 0, "repreview": True})
            finally:
                check.rollback()
    if suggestions:
        # Rolled-back trials expire ORM targets; refresh together instead of reloading every row during dispatch.
        loaded = _preload(db, actor, items, value)
    rows, undo, kept, waiting = [], [], [], defaultdict(list)
    batch_id = db.info.get("receipt_id") or m.uid()
    sources = _impact_sources(db, actor, value, items) if check_after_write else None
    with _batch(db, actor, batch_id, undo_source, session_creation=creation_only) as batch:
        _prepare_series_context(db, actor, value, plan)
        _prepare_approval_context(db, actor, value, plan, settings)
        for index, original in enumerate(items):
            row_preview = suggestions.get(index, {})
            entry = plan[index]
            if error := entry.get("resolution_error"):
                raise Problem(error.status, error.code, error.message, {"failed_index": index, "completed": 0})
            if entry["status"] == "staged":
                kept.append((index, original))
                continue
            item, extra = entry["item"], entry["extra"]
            if entry["status"] == "skipped":
                kept.append((index, original))
                rows.append({"index": index, "target_id": original.get("target_id"), "result": "skipped", "issues": row_preview.get("issues", [])})
                continue
            if item.get("send_for_approval"):
                role = row_preview["issues"][0].get("handoff", {}).get("role", "approver")
                waiting[role].append(original)
                rows.append({"index": index, "target_id": original.get("target_id"), "result": "waiting",
                             "before": row_preview.get("before", {}), "after": row_preview.get("after", {}),
                             "issues": row_preview["issues"]})
                continue
            if row_preview.get("result") == "problem":
                issue = row_preview["issues"][0]
                raise Problem(409, issue["code"], issue["message"],
                              {**row_preview.get("_refusal_details", {}), "failed_index": index,
                               "completed": 0, "total": len(items)})
            try:
                for additional in extra:
                    _, reverse = _run_item(db, actor, additional, value, settings, batch_id)
                    undo[0:0] = reverse
                row, reverse = _run_item(db, actor, item, value, settings, batch_id)
            except Exception as caught:
                error = _problem(caught)
                raise Problem(error.status, error.code, error.message,
                    {**_details(error), "failed_index": index, "completed": 0, "total": len(items),
                     "target_id": item.get("target_id")}) from error
            undo[0:0] = reverse
            if row["result"] == "ready":
                row["result"] = "changed"
            rows.append({"index": index, **row})
    warnings, impacts = [], []
    if sources is not None:
        # Report actual session IDs; any refused impact rolls back the whole transaction.
        checked, warnings, impacts = _attribute_impacts(db, actor, value, items,
            [{**row, "result": "ready" if row["result"] == "changed" else row["result"]} for row in rows], sources, undo_source)
        for original, row in zip(rows, checked):
            if original["result"] == "changed" and row["result"] in {"problem", "skipped"}:
                issue = row["issues"][0]
                raise Problem(409, issue["code"], issue["message"],
                              {**row.get("_refusal_details", {}), "failed_index": row["index"],
                               "completed": 0, "total": len(items)})
    staged = None
    reviews = []
    if waiting:
        from .bulk_staged import propose
        for role, awaiting in waiting.items():
            preserved = {**value, "items": awaiting, "resolutions": {}}
            for field in ("only", "apply_ready_only", "staged_id"):
                preserved.pop(field, None)
            reviews.append(propose(db, actor, preserved, settings, role, owner_id=stage_owner))
    if kept:
        from .bulk_staged import keep
        preserved = {**value, "items": [i for _, i in kept]}
        for field in ("only", "apply_ready_only", "staged_id"):
            preserved.pop(field, None)
        resolutions = value.get("resolutions", {})
        preserved["resolutions"] = {**{key: choice for key, choice in resolutions.items() if not key.isdigit()},
            **{str(new): resolutions[str(old)] for new, (old, _) in enumerate(kept) if str(old) in resolutions}}
        staged = keep(db, actor, preserved, settings, review_role=stage_review_role, owner_id=stage_owner)
    applied_count = len(rows)
    indexed = {r["index"] for r in rows}
    rows.extend({"index": index, "target_id": item.get("target_id"), "result": "staged",
                 "before": suggestions.get(index, {}).get("before", {}),
                 "after": suggestions.get(index, {}).get("after", {}),
                 "issues": suggestions.get(index, {}).get("issues", [])}
                for index, item in kept if index not in indexed)
    rows.sort(key=lambda row: row["index"])
    counts, groups = _summary(rows)
    anonymised = db.info.get("anonymised_records", {})
    _redact_anonymised_rows(db, rows)
    undo_summary = _undo_summary(value, items, rows)
    if value.get("staged_id"):
        from .bulk_staged import applied
        applied(db, actor, value["staged_id"])
    revisions = {}
    seen_workspaces, coalesced = set(), []
    for item in undo:
        if item.get("item_kind") == "shift.undo_batch":
            if item.get("target_id") in seen_workspaces:
                continue
            seen_workspaces.add(item.get("target_id"))
        coalesced.append(item)
    undo = coalesced
    undo = _retained_inverses(undo, anonymised)
    for item in undo:
        kind = item.get("item_kind") or item.get("kind")
        cls = _model(kind, item.get("payload", {}))
        if cls and item.get("target_id"):
            key = (cls, item["target_id"])
            row = db.get(cls, (actor.department_id, item['target_id']) if cls is m.ContactType else item['target_id'])
            if key not in revisions:
                revisions[key] = getattr(row, "revision", item.get("expected_revision"))
            item["expected_revision"] = revisions[key]
            if refs := item.get("payload", {}).get("items"):
                for ref in refs:
                    if ref.get("id") == item["target_id"]:
                        ref["revision"] = revisions[key]
            # Bind targets to the batch boundary; dispatch advances against the real revisions, including related writes.
            if cls is m.SchedulingFact:
                revisions[key] += 1
                if item.get("payload", {}).get("predecessor_revision") is not None:
                    # Fact predecessors are explicit cross-record preconditions, outside target revision advancement.
                    predecessor = db.get(cls, row.replaces_id)
                    previous_key = (cls, predecessor.id)
                    item["payload"]["predecessor_revision"] = revisions.get(previous_key, predecessor.revision)
                    revisions[previous_key] = item["payload"]["predecessor_revision"] + 1
    undo_resolutions = {str(index): "shorten_anyway" for index, item in enumerate(undo)
                        if (item.get("item_kind") or item.get("kind")) in {
                            "eligibility.update_dates", "grant.update_dates", "membership.update_dates"}}
    item_kinds = {part["kind"] for part in batch["authorization_commands"]}
    item_kinds.update(kind for pending in waiting.values() for item in pending
                      if (kind := item.get("item_kind") or item.get("kind") or value.get("item_kind")))
    undo_payload = {'items': undo, 'resolutions': undo_resolutions, 'undo_of': batch_id,
                    'reason': 'Undo: ' + value['reason'][:490]}
    if undo:
        undo_payload.update(_series_undo_acknowledgement(db, actor, undo_payload))
    delta = _merge_edit_deltas(rows)
    workspace_reply = None
    if delta is not None:
        candidates = [reply for row in rows for reply in row.get('results', []) if reply.get('workspace')]
        workspace_reply = candidates[-1] if candidates else None
    return project_result(db, actor, value, {"batch_id": batch_id, "count": applied_count, "item_kinds": sorted(item_kinds),
            "items": rows, "rows": [] if creation_only else rows, "counts": counts, "groups": groups, **undo_summary,
            "warnings": warnings, "impacts": impacts,
            "undo": {"kind": "bulk.apply", "payload": undo_payload} if undo else None,
            "staged": staged, "reviews": reviews, "staged_count": len(kept),
            "authorization_commands": batch["authorization_commands"],
            **({"delta": delta, "workspace": workspace_reply['workspace'],
                "validation": workspace_reply.get('validation'), "health": workspace_reply.get('health'),
                "fix_list": workspace_reply.get('fix_list'), "fix_list_delta": workspace_reply.get('fix_list_delta')}
                if workspace_reply is not None else {"delta": None}),
            "message": f"{applied_count} records processed."})

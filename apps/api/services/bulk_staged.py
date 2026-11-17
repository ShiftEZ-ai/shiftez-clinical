"""Revision-bound unapplied changes and independent handoffs; expiry preserves history."""
from datetime import timedelta
from copy import deepcopy
from types import SimpleNamespace

from sqlalchemy import String, and_, cast, or_, select

from apps.api import models as m
from apps.api.auth import can_read_rules, department_roles
from apps.api.errors import Problem, require
from apps.api.serialization import record
from packages.contracts import schemas as s
from packages.domain.time import aware
from .bulk_settings import limits
from .common import expected, outbox


EXPIRE_BATCH_SIZE = 500


def _readable(actor, row):
    def rule_command(value):
        if isinstance(value, list):
            return any(rule_command(item) for item in value)
        if isinstance(value, dict):
            return any(isinstance(item, str) and key in {'item_kind', 'kind', 'action'}
                       and item.startswith(('policy.', 'rule.', 'exception.')) or rule_command(item)
                       for key, item in value.items())
        return False

    return can_read_rules(actor) or not rule_command(row.payload)


def _target_people_many(db, rows):
    from . import bulk
    people, targets = {id(row): set() for row in rows}, {}
    for row in rows:
        for item in row.payload.get("items", []):
            kind, payload, changes = bulk._commands(item, row.payload)
            commands = [(kind, payload, item.get("target_id"))]
            for change in changes:
                action = change.get("item_kind") or change.get("action")
                if action:
                    part = {**(change.get("value") if isinstance(change.get("value"), dict) else {}),
                            **change.get("payload", {})}
                    commands.append((action, part, item.get("target_id")))
            for kind, payload, target in commands:
                if payload.get("person_id"):
                    people[id(row)].add(payload["person_id"])
                people[id(row)].update(payload.get("person_ids") or [])
                cls = bulk._model(kind or "", payload)
                target = bulk._target_id(kind or "", payload, target)
                if cls is m.Person and target:
                    people[id(row)].add(target)
                elif cls is not None and hasattr(cls, "person_id") and target:
                    targets.setdefault((cls, row.department_id), {}).setdefault(target, []).append(row)
    for (cls, department), ids in targets.items():
        query = select(cls.id, cls.person_id).where(cls.id.in_(ids))
        if hasattr(cls, "department_id"):
            query = query.where(cls.department_id == department)
        found = dict(db.execute(query).all())
        for pending in [*db.identity_map.values(), *db.new]:
            if isinstance(pending, cls) and pending.id in ids and (
                    not hasattr(cls, "department_id") or pending.department_id == department):
                found[pending.id] = pending.person_id
        for target, person in found.items():
            if person:
                for row in ids[target]:
                    people[id(row)].add(person)
    return people


def _target_people(db, row):
    return _target_people_many(db, [row])[id(row)]


def _reviewer(db, actor, row, owners=None, people=None):
    if row.status != "waiting" or not actor.permits(row.review_role) or row.owner_id == actor.identity_id:
        return False
    owner_person = owners.get(row.owner_id) if owners is not None else db.get(m.Identity, row.owner_id).person_id
    return (not actor.person_id or
            (actor.person_id != owner_person and actor.person_id not in (
                people[id(row)] if people is not None else _target_people(db, row))))


def _project(db, actor, row, value, details=None):
    require(_readable(actor, row), 'NOT_FOUND', 'Staged change not found', 404)
    from .availability import _recorded_details
    from .bulk import PRIVATE
    if details is None:
        details = row.owner_id == actor.identity_id or _recorded_details(db, actor)
    hidden = PRIVATE - ({"private_reason"} if details else set())

    def redact(part):
        if isinstance(part, dict):
            if isinstance(part.get("field"), str) and part["field"] in hidden:
                return {}
            relative = (part.get("kind") == "relative" and set(part) <= {"kind", "token", "n", "unit"}
                        and part.get("token") in {entry["token"] for entry in s.RELATIVE_TOKEN_CATALOGUE})
            # A condition token is a catalogue key, not an access credential.
            return {key: redact(item) for key, item in part.items() if key not in hidden or key == "token" and relative}
        if isinstance(part, list):
            return [redact(item) for item in part if not (
                isinstance(item, dict) and isinstance(item.get("field"), str) and item["field"] in hidden)]
        return part

    return redact(value)


def project_result(db, actor, value, body):
    """The staged payload, preview and apply reply share the same private-field policy."""
    if value.get("staged_id"):
        return _project(db, actor, db.get(m.BulkStaged, value["staged_id"]), body)
    return body


def receipt_result(body):
    """Receipts retain lifecycle evidence, never another copy of the staged personal payload."""
    if not any(isinstance(row, dict) and "payload" in row for row in [body.get("staged"), *body.get("reviews", [])]):
        return body
    result = deepcopy(body)
    for row in [result.get("staged"), *result.get("reviews", [])]:
        if isinstance(row, dict) and "payload" in row:
            row["payload"] = {}
    return result


def receipt_reply(db, actor, body):
    """Only a currently visible, unexpired stage can restore its payload for an idempotent reply."""
    result = deepcopy(body)
    for saved in [result.get("staged"), *result.get("reviews", [])]:
        if not isinstance(saved, dict) or not saved.get("id"):
            continue
        row = db.get(m.BulkStaged, saved["id"])
        if row is None or row.department_id != actor.department_id or not _readable(actor, row) or not (
                row.owner_id == actor.identity_id or _reviewer(db, actor, row)):
            saved["payload"] = {}
            continue
        current = record(row)
        if row.status not in {"active", "waiting"} or aware(row.expires_at) <= m.now():
            current["payload"] = {}
        saved.update(_project(db, actor, row, current))
    from .wishes import outcome_reasons

    def project_wishes(part):
        if isinstance(part, dict):
            for key, value in part.items():
                if key == "outcome_reasons":
                    part[key] = outcome_reasons(actor, value)
                else:
                    project_wishes(value)
        elif isinstance(part, list):
            for value in part:
                project_wishes(value)

    project_wishes(result)
    return result


def reviewers(db, row):
    """Current, independent recipients for the existing notification worker."""
    roles = department_roles(db, row.department_id)
    owner = db.get(m.Identity, row.owner_id)
    people = _target_people(db, row)
    candidates = [identity for identity, held in roles.items() if row.review_role in held and identity != row.owner_id]
    return {identity.id for identity in db.scalars(select(m.Identity).where(m.Identity.id.in_(candidates),
        m.Identity.disabled.is_(False))) if not identity.person_id or
        (identity.person_id != owner.person_id and identity.person_id not in people)}


def _waiting_review(actor):
    return and_(m.BulkStaged.owner_id != actor.identity_id,
        m.BulkStaged.status == "waiting", m.BulkStaged.review_role.in_(actor.roles))


def _open(actor):
    return and_(m.BulkStaged.department_id == actor.department_id,
        m.BulkStaged.status.in_(("active", "waiting")), m.BulkStaged.expires_at > m.now())


def _reviewable(db, actor, rows):
    owners = dict(db.execute(select(m.Identity.id, m.Identity.person_id).where(
        m.Identity.id.in_({row.owner_id for row in rows}))).all()) if rows and actor.person_id else {}
    people = _target_people_many(db, rows) if actor.person_id else {}
    return [row for row in rows if _readable(actor, row) and _reviewer(db, actor, row, owners, people)]


def review_count(db, actor):
    """Read-only badge: the list's independent reviews, with batched owner/target lookups."""
    rows = list(db.scalars(select(m.BulkStaged).where(_open(actor), _waiting_review(actor))))
    return len(_reviewable(db, actor, rows))


def _item_summary(payload):
    return {"item_count": len(payload.get("items", [])),
            "item_kinds": sorted({item.get("item_kind") or item.get("kind") or payload.get("item_kind")
                                   for item in payload.get("items", [])} - {None})}


def _history(db, actor, rows, reason):
    db.flush()
    for row in rows:
        # Staged item payloads can contain private leave reasons; History keeps lifecycle metadata only.
        snapshot = record(row, exclude=("payload",))
        snapshot.update(db.info.get("bulk_staged_authorized", {}).get(row.id) or _item_summary(row.payload))
        db.add(m.DefinitionHistory(department_id=row.department_id, kind="bulk_staged", object_id=row.id,
            object_revision=row.revision, snapshot=snapshot, actor_id=actor.identity_id if actor else row.owner_id, reason=reason,
            batch_id=db.info.get("receipt_id")))


def expire(db, department_id=None, actor_id=None, *, staged_id=None):
    """Worker housekeeping purges due payloads across owners/departments, in bounded batches."""
    query = select(m.BulkStaged).where(or_(
        and_(m.BulkStaged.status.in_(("active", "waiting")), m.BulkStaged.expires_at <= m.now()),
        and_(m.BulkStaged.status.in_(("applied", "expired", "dropped")), cast(m.BulkStaged.payload, String) != "{}")))
    if department_id is not None:
        query = query.where(m.BulkStaged.department_id == department_id)
    if staged_id is not None:
        query = query.where(m.BulkStaged.id == staged_id)
    rows = [row for row in db.scalars(query.order_by(m.BulkStaged.expires_at, m.BulkStaged.seq).limit(EXPIRE_BATCH_SIZE))
            if row.payload or row.status in {"active", "waiting"}]
    expired_rows, closed_rows = [], []
    for row in rows:
        expired = row.status in {"active", "waiting"}
        row.status, row.revision = "expired" if expired else row.status, row.revision + 1
        reason = "Unapplied bulk changes expired" if expired else "Private staged payload removed"
        if expired:
            db.add(m.AuditEvent(department_id=row.department_id, actor_id=actor_id or row.owner_id,
                action="bulk.staged.expire", target_id=row.id, reason=reason,
                detail={"count": len(row.payload.get("items", []))},
                correlation_id=db.info.get("correlation_id", "bulk-staged-expiry")))
        (expired_rows if expired else closed_rows).append(row)
    actor = SimpleNamespace(identity_id=actor_id) if actor_id else None
    if expired_rows:
        _history(db, actor, expired_rows, "Unapplied bulk changes expired")
    if closed_rows:
        _history(db, actor, closed_rows, "Private staged payload removed")
    for row in rows:
        row.payload = {}
    return len(rows)


def _expire(db, actor):
    return expire(db, department_id=actor.department_id, actor_id=actor.identity_id)


def _owned(db, actor, staged_id, owner_only=False):
    row = db.get(m.BulkStaged, staged_id)
    require(row is not None and row.department_id == actor.department_id and
            _readable(actor, row) and
            (row.owner_id == actor.identity_id or not owner_only and _reviewer(db, actor, row)),
            "NOT_FOUND", "Staged change not found", 404)
    return row


def keep(db, actor, payload, settings, review_role=None, *, owner_id=None):
    value = s.BulkApply.model_validate(payload)
    options = limits(db, actor, settings)
    require(not value.selection, "BULK_SELECTION_UNRESOLVED", "Resolve the selection before staging changes")
    require(len(value.items) <= options["bulk_max_items"], "BULK_LIMIT",
            f"A staged change supports at most {options['bulk_max_items']} items")
    saved = value.model_dump(mode="json")
    saved.update(only=None, apply_ready_only=False, staged_id=None)
    moment = m.now()
    row = m.BulkStaged(department_id=actor.department_id, owner_id=owner_id or actor.identity_id, payload=saved,
        created_at=moment, expires_at=moment + timedelta(days=options["bulk_staged_days"]),
        status="waiting" if review_role else "active", review_role=review_role)
    db.add(row)
    _history(db, actor, [row], "Bulk changes sent for independent review" if review_role else
             "Unapplied bulk changes kept staged")
    if removed := db.info.get("anonymised_people"):
        _scrub_personal_rows(db, [row], removed)
    return record(row)


def propose(db, actor, payload, settings, role, *, owner_id=None):
    require(role in s.ROLE_NAMES, "BULK_REVIEW_ROLE", "Choose a current department role for this review")
    saved = keep(db, actor, payload, settings, review_role=role, owner_id=owner_id)
    outbox(db, actor, "bulk-review:" + saved["id"], "bulk_review", {"staged_id": saved["id"]})
    return saved


def save(db, actor, command, settings):
    from . import bulk
    value = s.BulkStageInput.model_validate(command.payload)
    payload, _, _ = bulk.prepare(db, actor, value.payload.model_dump(mode="json", exclude_unset=True), settings)
    return {"staged": keep(db, actor, payload, settings)}


def list_mine(db, actor, settings):
    _expire(db, actor)
    rows = list(db.scalars(select(m.BulkStaged).where(_open(actor),
        or_(m.BulkStaged.owner_id == actor.identity_id, _waiting_review(actor)))
        .order_by(m.BulkStaged.created_at.desc(), m.BulkStaged.seq.desc())))
    waiting = [row for row in rows if row.owner_id != actor.identity_id]
    reviewable = {row.id for row in _reviewable(db, actor, waiting)}
    from .availability import _recorded_details
    details = _recorded_details(db, actor) if waiting else False
    items = [_project(db, actor, row, record(row), details=row.owner_id == actor.identity_id or details)
             for row in rows if _readable(actor, row) and (row.owner_id == actor.identity_id or row.id in reviewable)]
    return {"items": items, "count": len(items)}


def reopen(db, actor, staged_id, settings):
    from . import bulk
    row = _owned(db, actor, staged_id)
    expire(db, department_id=actor.department_id, actor_id=actor.identity_id, staged_id=row.id)
    if row.status == "expired":
        return {"staged": record(row), "preview": None, "expired": True}
    require(row.status in {"active", "waiting"}, "BULK_STAGED_CLOSED", "This staged change is no longer active", 409)
    body = {"staged": record(row), "preview": bulk.preview(db, actor,
        s.Command(kind="bulk.apply", payload={**_project(db, actor, row, row.payload), "staged_id": row.id}), settings)}
    if row.payload.get("source_selection"):
        from .bulk_selection import resolve
        try:
            body["selection_resolution"] = resolve(db, actor, row.payload["source_selection"])
        except Problem as exc:
            # A reviewer may not hold the owner's token; staged items stay revision-bound.
            body["selection_resolution"] = {"unavailable": True, "message": exc.message}
        except ValueError:
            body["selection_resolution"] = {"unavailable": True,
                "message": "This selection can no longer be resolved; refresh or select again."}
    return _project(db, actor, row, body)



def drop(db, actor, command, settings):
    value = s.Decision.model_validate(command.payload)
    row = _owned(db, actor, command.target_id, owner_only=True)
    expected(row, command.expected_revision)
    require(row.status in {"active", "waiting"}, "BULK_STAGED_CLOSED", "This staged change is no longer active", 409)
    row.status, row.revision = "dropped", row.revision + 1
    _history(db, actor, [row], value.reason)
    row.payload = {}
    return {"staged": record(row)}


def _can_apply(db, actor, row):
    require(row.status in {"active", "waiting"} and aware(row.expires_at) > m.now(),
            "BULK_STAGED_CLOSED", "This staged change is no longer active", 409)
    if row.status == "waiting":
        require(_reviewer(db, actor, row), "SELF_APPROVAL", "A different qualified reviewer must apply these changes", 403)


def authorize_payload(db, actor, value, *, for_apply=False):
    """Bind the browser's visible projection, then execute the unchanged server-side private payload."""
    row = _owned(db, actor, value["staged_id"])
    require(row.status in {"active", "waiting"} and aware(row.expires_at) > m.now(),
            "BULK_STAGED_CLOSED", "This staged change is no longer active", 409)
    if for_apply:
        _can_apply(db, actor, row)
    omitted = {"only", "apply_ready_only", "staged_id", "resolutions"}
    original = s.BulkApply.model_validate(_project(db, actor, row, row.payload)).model_dump(
        mode="json", exclude_none=True, exclude=omitted)
    proposed = s.BulkApply.model_validate(value).model_dump(mode="json", exclude_none=True, exclude=omitted)
    require(original == proposed, "BULK_STAGED_CHANGED",
            "The staged changes differ from this request; reopen the original staged change", 409)
    if for_apply:
        db.info.setdefault("bulk_staged_authorized", {})[row.id] = _item_summary(row.payload)
    return {**deepcopy(row.payload), **{key: deepcopy(value[key]) for key in omitted if key in value}}


def applied(db, actor, staged_id):
    row = db.get(m.BulkStaged, staged_id)
    scrubbed_source = (row is not None and row.department_id == actor.department_id and
        staged_id in db.info.get("bulk_staged_authorized", {}) and row.status == "dropped" and
        not row.payload and bool(db.info.get("anonymised_people")))
    if not scrubbed_source:
        row = _owned(db, actor, staged_id)
        _can_apply(db, actor, row)
    row.status, row.revision = "applied", row.revision + 1
    _history(db, actor, [row], "Staged bulk changes applied")
    row.payload = {}
    return record(row)


def _scrub_personal_rows(db, rows, person_ids):
    def contains(part):
        if isinstance(part, dict):
            return any(contains(item) for item in part.values())
        if isinstance(part, list):
            return any(contains(item) for item in part)
        return isinstance(part, str) and part in person_ids

    people = _target_people_many(db, rows)
    owners = set(db.scalars(select(m.Identity.id).where(m.Identity.person_id.in_(person_ids))))
    changed = []
    for row in rows:
        if row.payload and (row.owner_id in owners or person_ids.intersection(people[id(row)]) or contains(row.payload)):
            if row.status in {"active", "waiting"}:
                row.status = "dropped"
            row.revision = (row.revision or 1) + 1
            changed.append(row)
    if changed:
        _history(db, None, changed, "Staged personal changes discarded when personal details were removed")
    for row in changed:
        row.payload = {}
    return len(changed)


def scrub_for_person(db, person_id):
    """Discard staged personal edits, including pending rows and stages created later in this transaction."""
    db.info.setdefault("anonymised_people", set()).add(person_id)
    rows = list(db.scalars(select(m.BulkStaged).where(cast(m.BulkStaged.payload, String) != "{}")))
    rows.extend(row for row in db.new if isinstance(row, m.BulkStaged) and row not in rows)
    return _scrub_personal_rows(db, rows, {person_id})

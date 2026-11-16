"""One Tags Save/Undo either commits metadata and members together or leaves every input unchanged."""
import copy
from datetime import date
import uuid

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services import tags
from packages.contracts.schemas import TagInput
from .test_catalogue_preview import apply, preview
from .test_rule_conditions import line, tree

REFUSAL_ACTIONS = ('command.refused', 'department.score_check_refused')


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)], ids=["sqlite", "postgres"])
def both(request):
    return request.getfixturevalue(request.param)


def value(tag, **changes):
    return {**{key: tag[key] for key in TagInput.model_fields}, **changes}


def ref(kind, identifier):
    return {"kind": kind, "id": identifier}


def save(h, tag=None, *, fields=None, add=(), remove=(), status=200, role="coordinator", key=None):
    payload = {"catalogue": "tags", "value": fields or value(tag), "reason": "Synthetic atomic tag save"}
    if add or remove:
        payload["members"] = {"add": list(add), "remove": list(remove)}
    return h.command(role, "catalogue.update" if tag else "catalogue.create", payload,
                     tag["id"] if tag else None, tag["revision"] if tag else None, status=status, key=key)


def undo(h, result):
    return save(h, result["record"], fields=result["previous_value"] or value(result["record"], active=False),
                add=result["removed"], remove=result["added"])


def held(h, tag):
    with transaction(h.app.state.engine) as db:
        return {(r.kind, r.member_id) for r in db.scalars(select(m.TagMember).where(m.TagMember.tag_id == tag["id"]))}


def state(h):
    with transaction(h.app.state.engine) as db:
        org = db.get(m.Organisation, h.seed["organisation_id"])
        return {"records": {cls.__tablename__: [record(r) for r in db.scalars(select(cls).order_by(cls.id))]
                            for cls in (m.Tag, m.TagMember, m.Person)},
                # command_refusals.py records bounded, payload-free refusal evidence independently of the rolled-back
                # command (cf9303653); exclude those audit rows from the business-state atomicity comparison.
                "counts": {**{cls.__tablename__: db.scalar(select(func.count()).select_from(cls)) for cls in
                              (m.DefinitionHistory, m.CommandReceipt, m.Outbox)},
                           'audit_events': db.scalar(select(func.count()).select_from(m.AuditEvent).where(
                               ~m.AuditEvent.action.in_(REFUSAL_ACTIONS)))},
                "input_revision": org.input_revision, "availability_revision": org.availability_revision}


def refusal_events(h):
    with transaction(h.app.state.engine) as db:
        return [(row.id, row.action, (row.detail or {}).get('error_code')) for row in db.scalars(
            select(m.AuditEvent).where(m.AuditEvent.action.in_(REFUSAL_ACTIONS)).order_by(m.AuditEvent.id))]


def seed_tag(h, name="Before", labels=None, people=True):
    return save(h, fields={"name": name, "labels": labels or ["people"]},
                add=[ref("people", h.seed["people"][0])] if people else [])['record']


@pytest.mark.parametrize("conflict", ["duplicate", "text_rule"])
def test_failed_name_with_members_is_unchanged_and_can_retry_at_original_revision(both, conflict):
    h = both
    tag = seed_tag(h)
    if conflict == "duplicate":
        seed_tag(h, name="Taken", people=False)
        name, code = "tAkEn", "TAG_NAME_TAKEN"
    else:
        h.policy({"kind": "min_rest", "hours": 10}, {"conditions": tree(line("tags", "before"))})
        name, code = "Renamed", "TAG_NAME_IN_USE"
    first, second = [ref("people", person) for person in h.seed["people"][:2]]
    before = state(h)
    failed = save(h, tag, fields=value(tag, name=name), add=[second], remove=[first], status=409)
    assert failed["error"]["code"] == code and state(h) == before
    corrected = value(tag, name="Corrected" if conflict == "duplicate" else tag["name"])
    saved = save(h, tag, fields=corrected, add=[second], remove=[first])
    assert saved["record"]["revision"] == tag["revision"] + 1
    assert held(h, tag) == {("people", second["id"])}
    restored = undo(h, saved)
    assert value(restored["record"]) == value(tag) and held(h, tag) == {("people", first["id"])}


def test_drop_kind_add_new_kind_and_single_inverse_preserve_people_and_input_revision(both):
    h = both
    tag = seed_tag(h)
    before = state(h)
    person, clinic = ref("people", h.seed["people"][0]), ref("services", h.seed["services"][0])
    saved = save(h, tag, fields=value(tag, name="Clinics", labels=["services"], colour="teal"), add=[clinic], remove=[person])
    assert saved["added"] == [clinic] and saved["removed"] == [person]
    assert saved["previous_value"] == value(tag)
    assert saved["record"]["revision"] == tag["revision"] + 1
    assert state(h)["input_revision"] == before["input_revision"] + 1
    assert held(h, tag) == {("services", clinic["id"])}
    assert next(p for p in h.get("coordinator", "/api/catalogues")["people"] if p["id"] == person["id"])["tags"] == []
    restored = undo(h, saved)
    assert value(restored["record"]) == value(tag)
    assert held(h, tag) == {("people", person["id"])}
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person["id"]).tags == [tag["name"]]
    assert state(h)["input_revision"] == before["input_revision"] + 2


def test_create_with_members_and_undo_is_recoverable_and_idempotent(both):
    h = both
    person = ref("people", h.seed["people"][0])
    operation = str(uuid.uuid4())
    created = save(h, fields={"name": "New team", "labels": ["people"]}, add=[person], key=operation)
    after = state(h)
    assert save(h, fields={"name": "New team", "labels": ["people"]}, add=[person], key=operation) == created
    assert state(h) == after
    assert save(h, fields={"name": "Changed payload", "labels": ["people"]}, add=[person], key=operation,
                status=409)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    archived = undo(h, created)["record"]
    assert archived["id"] == created["record"]["id"] and not archived["active"]
    assert held(h, archived) == set()
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person["id"]).tags == [] and db.get(m.Tag, archived["id"]) is not None


def test_invalid_member_create_and_update_never_leave_partial_records(both):
    h = both
    tag = seed_tag(h)
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed["organisation_id"], name="Separate scope")
        db.add(department)
        db.flush()
        foreign = m.Service(department_id=department.id, name="Foreign clinic", starts_on=date(2026, 1, 1))
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
    for role in ("staff", "approver", "auditor", "credentialer"):
        h.client(role)
    before = state(h)
    for identifier in (foreign_id, str(uuid.uuid4())):
        for existing in (None, tag):
            result = save(h, existing, fields={"name": "Failed creation or edit", "labels": ["people", "services"]},
                          add=[ref("services", identifier)], remove=[ref("people", h.seed["people"][0])] if existing else [], status=404)
            assert result["error"]["code"] == "NOT_FOUND" and state(h) == before
    for role in ("staff", "approver", "auditor", "credentialer"):
        previous_refusals = refusal_events(h)
        result = save(h, tag, fields=value(tag, name="Not permitted"),
                      remove=[ref("people", h.seed["people"][0])], role=role, status=403)
        assert state(h) == before
        current_refusals = refusal_events(h)
        new_refusals = [row for row in current_refusals if row[0] not in {old[0] for old in previous_refusals}]
        assert result["error"]["code"] == "FORBIDDEN"
        assert len(new_refusals) == 1 and new_refusals[0][1:] == ('command.refused', 'FORBIDDEN')


def test_invalid_deltas_and_unremoved_kind_fail_without_side_effects(both):
    h = both
    tag = seed_tag(h)
    person, clinic = ref("people", h.seed["people"][0]), ref("services", h.seed["services"][0])
    h.client("admin")
    before = state(h)
    for fields, delta, code, status in [
        (value(tag), {"add": [person], "remove": [person]}, "TAG_MEMBER_CONFLICT", 422),
        (value(tag), {"add": [clinic]}, "TAG_KIND", 422),
        (value(tag, labels=["services"]), {"add": [clinic]}, "TAG_IN_USE", 409),
        (value(tag), {"add": [person] * 501}, "SCHEMA_INVALID", 422),
        (value(tag), {}, "SCHEMA_INVALID", 422),
    ]:
        result = h.command("coordinator", "catalogue.update", {"catalogue": "tags", "value": fields,
            "members": delta, "reason": "Invalid delta"}, tag["id"], tag["revision"], status=status)
        assert result["error"]["code"] == code and state(h) == before
    assert save(h, fields={"name": "No removal", "labels": ["people"]}, remove=[person], status=422)["error"]["code"] == "TAG_MEMBER_REMOVE_ON_CREATE"
    result = h.command("admin", "catalogue.create", {"catalogue": "services", "value": {"name": "Not a tag", "starts_on": "2026-01-01"},
        "members": {"add": [person]}, "reason": "Reject misplaced member delta"}, status=422)
    assert result["error"]["code"] == "TAG_MEMBERS_ONLY" and state(h) == before


def test_exception_after_removal_and_metadata_rolls_back_everything(both, monkeypatch):
    h = both
    tag = seed_tag(h)
    before = state(h)
    original = tags.add_member

    def fail_after_add(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Synthetic failure after tag metadata and membership mutations")

    monkeypatch.setattr(tags, "add_member", fail_after_add)
    result = save(h, tag, fields=value(tag, name="Never persisted", labels=["services"]),
                  add=[ref("services", h.seed["services"][0])], remove=[ref("people", h.seed["people"][0])], status=500)
    assert result["error"]["code"] == "INTERNAL_ERROR" and state(h) == before


def test_stale_save_and_undo_cannot_overwrite_later_changes(both):
    h = both
    tag = seed_tag(h)
    first, second = [ref("people", person) for person in h.seed["people"][:2]]
    saved = save(h, tag, fields=value(tag, name="Renamed"), add=[second], remove=[first])
    before = state(h)
    assert save(h, tag, remove=[first], status=409)["error"]["code"] == "STALE_REVISION" and state(h) == before
    later = save(h, saved["record"], fields=value(saved["record"], colour="blue"))
    before = state(h)
    result = save(h, saved["record"], fields=saved["previous_value"], add=saved["removed"], remove=saved["added"], status=409)
    assert result["error"]["code"] == "STALE_REVISION" and state(h) == before
    assert held(h, later["record"]) == {("people", second["id"])}


def test_standalone_member_command_keeps_changed_only_revision_and_inverse(both):
    h = both
    tag = seed_tag(h)
    person = ref("people", h.seed["people"][0])
    before = state(h)
    unchanged = h.command("coordinator", "tag.members", {"add": [person], "reason": "Already a member"}, tag["id"], tag["revision"])
    assert unchanged["added"] == unchanged["removed"] == []
    assert unchanged["record"]["revision"] == tag["revision"] and state(h)["input_revision"] == before["input_revision"]
    removed = h.command("coordinator", "tag.members", {"remove": [person], "reason": "Remove existing member"}, tag["id"], tag["revision"])
    assert removed["removed"] == [person] and removed["record"]["revision"] == tag["revision"] + 1
    restored = h.command("coordinator", "tag.members", {"add": removed["removed"], "remove": removed["added"], "reason": "Undo"},
                         tag["id"], removed["record"]["revision"])
    assert restored["added"] == [person] and held(h, tag) == {("people", person["id"])}


def test_tag_preview_uses_proposed_kinds_and_the_exact_effective_delta(both):
    h = both
    tag = seed_tag(h)
    first = ref("people", h.seed["people"][0])
    clinic = ref("services", h.seed["services"][0])
    policy = h.policy({"kind": "min_rest", "hours": 10}, {"conditions": tree(line("person_ids", tag["id"]))})
    before = state(h)
    delta = {"add": [clinic], "remove": [first]}
    proposed = value(tag, labels=["services"])
    response = h.client("coordinator").post('/api/tags/' + tag["id"] + '/preview', json={**delta, "value": proposed})
    assert response.status_code == 200, response.text
    rule = next(r for r in response.json()["rules"] if r["policy_id"] == policy["id"])
    assert (rule["people_before"], rule["people_after"]) == (1, 0) and state(h) == before
    invalid = h.client("coordinator").post('/api/tags/' + tag["id"] + '/preview', json={"add": [clinic], "value": proposed})
    assert invalid.status_code == 409 and invalid.json()["error"]["code"] == "TAG_IN_USE"
    legacy = h.client("coordinator").post('/api/tags/' + tag["id"] + '/preview', json={"remove": [first]})
    assert legacy.status_code == 200 and state(h) == before
    assert h.client("staff").post('/api/tags/' + tag["id"] + '/preview', json={**delta, "value": proposed}).status_code == 403
    save(h, tag, fields=proposed, **delta)
    assert held(h, tag) == {("services", clinic["id"])}
    h.shift(h.workspace(), service=0)
    unchanged = h.client("coordinator").post('/api/tags/' + tag["id"] + '/preview', json={"add": [clinic]})
    assert unchanged.status_code == 200 and unchanged.json()["draft_sessions"] == unchanged.json()["drafts"] == 0


def test_catalogue_preview_binds_members_rolls_back_and_preserves_publication(both):
    h = both
    issued = h.publish(h.shift(h.workspace())["workspace"])["publication_id"]
    publication = h.get("coordinator", '/api/publications/' + issued)["snapshot"]
    tag = seed_tag(h, labels=["services"], people=False)
    policy = h.policy({"kind": "restriction", "weekdays": list(range(7)), "start_time": "00:00", "end_time": "24:00"},
                      {"conditions": tree(line("service_ids", tag["id"]))})
    workspace = h.shift(h.workspace("Future draft", "2026-10-11", "2026-10-18"),
                        start="2026-10-11T08:00:00+03:00", end="2026-10-11T12:00:00+03:00")["workspace"]
    cmd = {"kind": "catalogue.update", "target_id": tag["id"], "expected_revision": tag["revision"],
           "payload": {"catalogue": "tags", "value": value(tag, colour="teal"), "reason": "Preview atomic tag edit",
                       "members": {"add": [ref("services", h.seed["services"][0])]}}}
    h.client("admin")
    before = state(h)
    result = preview(h, cmd)
    assert state(h) == before and not result["committed"]
    impact = next(r for r in result["impacts"] if r["workspace_id"] == workspace["id"])
    assert any(f["rule_id"] == policy["id"] for f in impact["introduced"])
    changed = copy.deepcopy(cmd)
    changed["payload"]["members"]["add"] = [ref("services", h.seed["services"][1])]
    assert apply(h, changed, result["source_fingerprint"], status=409)["error"]["code"] == "STALE_PREVIEW"
    assert state(h) == before
    saved = apply(h, cmd, result["source_fingerprint"])
    assert held(h, tag) == {("services", h.seed["services"][0])}
    assert h.get("coordinator", '/api/publications/' + issued)["snapshot"] == publication
    undo(h, saved)
    assert held(h, tag) == set()

"""Staged personal data follows disclosure policy and has a bounded lifetime."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import bulk_staged
from .governance_support import identity
from .test_bulk_selection import actor


PRIVATE_REASON = "Fictional confidential reason for the staged absence"


@pytest.mark.parametrize('metadata', [{}, {'staged': None, 'reviews': []}, {'reviews': [{'id': 'review'}]}])
def test_ordinary_receipt_does_not_copy_unrelated_session_content(monkeypatch, metadata):
    body = {**metadata, 'shifts': [{'id': 'session', 'participants': [{'person_id': 'person'}]}]}
    before = deepcopy(body)

    def unrelated(*args):
        pytest.fail('A receipt without staged payloads has nothing to redact or copy')

    monkeypatch.setattr(bulk_staged, 'deepcopy', unrelated)
    assert bulk_staged.receipt_result(body) == before
    assert body == before


@pytest.mark.parametrize('key', ['staged', 'reviews'])
def test_staged_receipt_still_copies_and_redacts_without_mutating_the_reply(key):
    row = {'id': 'stage', 'payload': {'private_reason': PRIVATE_REASON}}
    body = {key: row if key == 'staged' else [row], 'shifts': [{'id': 'session'}]}
    before = deepcopy(body)
    redacted = bulk_staged.receipt_result(body)
    assert body == before
    assert redacted == {key: {'id': 'stage', 'payload': {}} if key == 'staged' else [{'id': 'stage', 'payload': {}}],
                        'shifts': [{'id': 'session'}]}
    redacted['shifts'][0]['id'] = 'changed'
    assert body == before


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def staged_h(request):
    return request.getfixturevalue(request.param)


def absence(h, person=None):
    return {"item_kind": "availability.record", "reason": "Independent staged absence review",
        "items": [{"payload": {"person_id": person or h.seed["people"][0], "kind": "actual_absence",
            "start": "2026-10-04T10:00:00+03:00", "end": "2026-10-04T11:00:00+03:00",
            "private_reason": PRIVATE_REASON}}]}


def keep(h, payload=None, owner="coordinator", review_role="coordinator"):
    with transaction(h.app.state.engine, write=True) as db:
        return bulk_staged.keep(db, actor(db, h, owner), payload or absence(h), h.settings, review_role)


@pytest.mark.parametrize("mode", ["none", "type", "details"])
@pytest.mark.parametrize("reviewer", ["coordinator", "credentialer"])
def test_staged_list_and_reopen_follow_owner_policy_and_planning_role(staged_h, mode, reviewer):
    h = staged_h
    identity(h, "requesting_coordinator", ["coordinator"], person=h.seed["people"][0])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).leave_reason_display = mode
    staged = keep(h, owner="requesting_coordinator", review_role=reviewer)
    owner = h.get("requesting_coordinator", "/api/bulk/staged")["items"][0]
    assert owner["payload"]["items"][0]["payload"]["private_reason"] == PRIVATE_REASON
    owner_preview = h.client("requesting_coordinator").post(f"/api/bulk/staged/{staged['id']}/reopen")
    assert owner_preview.status_code == 200, owner_preview.text
    assert PRIVATE_REASON in owner_preview.text
    assert owner_preview.json()["preview"]["items"][0]["issues"][0]["code"] == "SELF_APPROVAL"
    visible = h.get(reviewer, "/api/bulk/staged")["items"][0]
    expected = mode == "details" and reviewer == "coordinator"
    assert (PRIVATE_REASON in str(visible)) == expected
    reopened = h.client(reviewer).post(f"/api/bulk/staged/{staged['id']}/reopen")
    assert reopened.status_code == 200, reopened.text
    assert (PRIVATE_REASON in str(reopened.json())) == expected
    with transaction(h.app.state.engine) as db:
        assert db.get(m.BulkStaged, staged["id"]).payload["items"][0]["payload"]["private_reason"] == PRIVATE_REASON


def test_projection_covers_nested_private_keys_and_field_change_encodings(staged_h, monkeypatch):
    h = staged_h
    identity(h, "requesting_coordinator", ["coordinator"], person=h.seed["people"][0])
    staged = keep(h, owner="requesting_coordinator")
    private = {"private_reason": PRIVATE_REASON, "password_hash": "Fictional secret hash",
               "token": "Fictional token", "token_ciphertext": "Fictional ciphertext"}
    encoded = [{"field": key, "how": "set", "value": value} for key, value in private.items()]
    echo = {"items": [{"before": private, "after": {"nested": [private]},
                      "issues": [{"resolutions": [{"operations": [{"patch_item": {"changes": encoded}},
                          {"patch_item": {"field": "private_reason", "value": PRIVATE_REASON}}]}]}]}],
            "undo": {"kind": "bulk.apply", "payload": {"common": private}}}
    from apps.api.services import bulk
    monkeypatch.setattr(bulk, "preview", lambda *args: deepcopy(echo))
    reopened = h.client("coordinator").post(f"/api/bulk/staged/{staged['id']}/reopen")
    assert reopened.status_code == 200, reopened.text
    assert all(secret not in reopened.text for secret in private.values())
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.BulkStaged, staged["id"])
        row.payload = {**row.payload, "common": {"nested": private}, "changes": encoded}
    visible = h.get("coordinator", "/api/bulk/staged")["items"][0]
    assert all(secret not in str(visible) for secret in private.values())
    assert visible["payload"]["changes"] == []


def test_reviewer_applies_redacted_payload_using_private_server_copy_and_refuses_tampering(staged_h):
    h = staged_h
    identity(h, "requesting_coordinator", ["coordinator"], person=h.seed["people"][0])
    staged = keep(h, owner="requesting_coordinator")
    reviewer = h.client("coordinator")
    reopened = reviewer.post(f"/api/bulk/staged/{staged['id']}/reopen")
    assert reopened.status_code == 200, reopened.text
    shown = reopened.json()
    payload = {**shown["staged"]["payload"], "staged_id": staged["id"]}
    assert "private_reason" not in payload["items"][0]["payload"]
    tampered = deepcopy(payload)
    tampered["items"][0]["payload"]["private_reason"] = "Replacement private reason from the browser"
    refused = reviewer.post("/api/commands", headers={"idempotency-key": str(uuid4())},
        json={"kind": "bulk.apply", "payload": tampered})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "BULK_STAGED_CHANGED"
    applied = reviewer.post("/api/commands", headers={"idempotency-key": str(uuid4())}, json={
        "kind": "bulk.apply", "payload": payload,
        "expected_fingerprint": shown["preview"]["source_fingerprint"]})
    assert applied.status_code == 200, applied.text
    assert PRIVATE_REASON not in applied.text
    with transaction(h.app.state.engine) as db:
        record = db.scalar(select(m.Availability).where(m.Availability.recorded_by == h.seed["identities"]["coordinator"],
            m.Availability.private_reason == PRIVATE_REASON))
        assert record is not None
        closed = db.get(m.BulkStaged, staged["id"])
        assert closed.status == "applied" and closed.payload == {}


def test_stage_receipt_retains_no_personal_payload_and_rehydrates_only_live_changes(staged_h):
    h = staged_h
    key = str(uuid4())
    payload = {"payload": absence(h)}
    created = h.command("coordinator", "bulk.stage", payload, key=key)
    assert PRIVATE_REASON in str(created["staged"]["payload"])
    with transaction(h.app.state.engine) as db:
        receipt = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.key == key))
        assert receipt.result["staged"]["payload"] == {}
        assert PRIVATE_REASON not in str(receipt.result)
    assert h.command("coordinator", "bulk.stage", payload, key=key) == created
    staged = created["staged"]
    h.command("coordinator", "bulk.staged.drop", {"reason": "Discard the private staged absence"},
              staged["id"], staged["revision"])
    retried = h.command("coordinator", "bulk.stage", payload, key=key)
    assert retried["staged"]["status"] == "dropped" and retried["staged"]["payload"] == {}
    assert PRIVATE_REASON not in str(retried)


def test_partial_review_keeps_original_owner_without_disclosing_hidden_private_reason(staged_h):
    h = staged_h
    requester = identity(h, "requesting_coordinator", ["coordinator"], person=h.seed["people"][0])
    payload = absence(h)
    refused = deepcopy(payload["items"][0])
    refused["payload"].update(kind="invalid_fixture_kind", start="2026-10-04T12:00:00+03:00",
                              end="2026-10-04T13:00:00+03:00")
    payload["items"].append(refused)
    staged = keep(h, payload, owner="requesting_coordinator")
    client = h.client("coordinator")
    reopened = client.post(f"/api/bulk/staged/{staged['id']}/reopen")
    assert reopened.status_code == 200, reopened.text
    shown = reopened.json()
    changed = client.post("/api/commands", headers={"idempotency-key": str(uuid4())}, json={
        "kind": "bulk.apply", "payload": {**shown["staged"]["payload"], "staged_id": staged["id"],
            "apply_ready_only": True}, "expected_fingerprint": shown["preview"]["source_fingerprint"]})
    assert changed.status_code == 200, changed.text
    remaining = changed.json()["staged"]
    assert remaining["id"] != staged["id"]
    assert remaining["owner_id"] == requester
    assert remaining["status"] == "waiting" and remaining["review_role"] == "coordinator"
    assert PRIVATE_REASON not in changed.text
    reviewer = h.get("coordinator", "/api/bulk/staged")
    assert reviewer["count"] == 1 and PRIVATE_REASON not in str(reviewer)
    original_owner = h.get("requesting_coordinator", "/api/bulk/staged")
    assert original_owner["count"] == 1 and PRIVATE_REASON in str(original_owner)


def test_applying_staged_anonymise_scrubs_its_source_and_finishes_atomically(staged_h):
    h = staged_h
    person = h.seed["people"][0]
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Person, person)
        row.active = False
        revision = row.revision
    payload = {"item_kind": "person.anonymise", "reason": "Remove fictional inactive personal details",
        "items": [{"target_id": person, "expected_revision": revision, "payload": {}}]}
    staged = h.command("admin", "bulk.stage", {"payload": payload})["staged"]
    preview = h.client("admin").post(f"/api/bulk/staged/{staged['id']}/reopen")
    assert preview.status_code == 200, preview.text
    checked = preview.json()
    applied = h.client("admin").post("/api/commands", headers={"idempotency-key": str(uuid4())}, json={
        "kind": "bulk.apply", "payload": {**checked["staged"]["payload"], "staged_id": staged["id"]},
        "expected_fingerprint": checked["preview"]["source_fingerprint"]})
    assert applied.status_code == 200, applied.text
    assert applied.json()["counts"]["changed"] == 1
    with transaction(h.app.state.engine) as db:
        source = db.get(m.BulkStaged, staged["id"])
        assert source.status == "applied" and source.payload == {}
        assert db.get(m.Person, person).display_name.startswith("Former staff member")
        notes = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == "bulk_staged",
            m.DefinitionHistory.object_id == source.id)))
        assert all(note.snapshot["item_count"] == 1 for note in notes)


def test_global_expiry_purges_every_owner_department_and_legacy_terminal_payload(staged_h):
    h = staged_h
    staged = keep(h)
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed["organisation_id"], name="Fictional inactive staging department")
        db.add(other)
        db.flush()
        other_id = other.id
        due = db.get(m.BulkStaged, staged["id"])
        due.expires_at = m.now() - timedelta(seconds=1)
        rows = [due]
        for status in ("waiting", "active", "applied", "dropped", "expired"):
            row = m.BulkStaged(department_id=other.id, owner_id=h.seed["identities"]["credentialer"],
                status=status, payload=deepcopy(staged["payload"]), expires_at=m.now() - timedelta(days=1))
            db.add(row)
            rows.append(row)
        db.flush()
        ids = [row.id for row in rows]
        assert bulk_staged.expire(db) == len(rows)
        assert bulk_staged.expire(db) == 0
    with transaction(h.app.state.engine) as db:
        rows = list(db.scalars(select(m.BulkStaged).where(m.BulkStaged.id.in_(ids))))
        assert all(row.payload == {} for row in rows)
        assert all(row.status not in {"active", "waiting"} for row in rows)
        assert {row.department_id for row in rows} == {h.seed["department_id"], other_id}
        notes = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == "bulk_staged",
            m.DefinitionHistory.object_id.in_(ids))))
        assert all("payload" not in note.snapshot and PRIVATE_REASON not in str(note.snapshot) for note in notes)
        assert any(note.snapshot["item_count"] == 1 and note.snapshot["item_kinds"] == ["availability.record"]
                   for note in notes)


def test_list_omits_expired_backlog_beyond_one_cleanup_batch(staged_h, monkeypatch):
    h = staged_h
    expired = [keep(h) for _ in range(3)]
    live = keep(h)
    with transaction(h.app.state.engine, write=True) as db:
        for staged in expired:
            db.get(m.BulkStaged, staged["id"]).expires_at = m.now() - timedelta(days=1)
    monkeypatch.setattr(bulk_staged, "EXPIRE_BATCH_SIZE", 1)
    result = h.get("coordinator", "/api/bulk/staged")
    assert result["count"] == 1
    assert [row["id"] for row in result["items"]] == [live["id"]]
    with transaction(h.app.state.engine) as db:
        rows = list(db.scalars(select(m.BulkStaged).where(m.BulkStaged.id.in_([row["id"] for row in expired]))))
        assert sum(row.status == "expired" and row.payload == {} for row in rows) == 1
        assert sum(row.status == "waiting" and bool(row.payload) for row in rows) == 2


def test_anonymise_scrubs_owned_direct_indirect_and_pending_stages_but_keeps_unrelated(staged_h):
    h = staged_h
    person = h.seed["people"][0]
    identity(h, "departing_coordinator", ["coordinator"], person=person)
    availability = h.command("coordinator", "availability.record", absence(h)["items"][0]["payload"])["record"]
    direct = keep(h)
    indirect = keep(h, {"item_kind": "availability.retract", "reason": "Review existing absence",
        "items": [{"target_id": availability["id"], "expected_revision": availability["revision"], "payload": {}}]})
    owned = keep(h, absence(h, h.seed["people"][1]), owner="departing_coordinator")
    untouched = keep(h, absence(h, h.seed["people"][1]))
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, person).active = False
        revision = db.get(m.Person, person).revision
        pending = m.BulkStaged(department_id=h.seed["department_id"], owner_id=h.seed["identities"]["coordinator"],
            status="active", payload=absence(h), expires_at=m.now() + timedelta(days=1))
        db.add(pending)
        assert bulk_staged.scrub_for_person(db, person) == 4
        assert pending.payload == {} and pending.status == "dropped"
        late = bulk_staged.keep(db, actor(db, h), absence(h), h.settings)
        assert late["payload"] == {} and late["status"] == "dropped"
    after_helper = keep(h)
    removed = h.command("admin", "person.anonymise", {"reason": "Remove fictional inactive staff details"}, person, revision)
    assert removed["scrubbed"]["bulk_staged"] == 1
    with transaction(h.app.state.engine) as db:
        assert all(db.get(m.BulkStaged, entry["id"]).payload == {} for entry in (direct, indirect, owned, after_helper))
        assert db.get(m.BulkStaged, untouched["id"]).payload == untouched["payload"]


def test_reviewer_listing_batches_owner_and_target_reads(staged_h, request):
    h = staged_h
    reviewer_person = h.seed["people"][1]
    identity(h, "linked_reviewer", ["coordinator"], person=reviewer_person)
    with transaction(h.app.state.engine, write=True) as db:
        availability = m.Availability(department_id=h.seed["department_id"], person_id=h.seed["people"][0],
            kind="actual_absence", start=m.now(), end=m.now() + timedelta(hours=1), private_reason=PRIVATE_REASON,
            recorded_by=h.seed["identities"]["coordinator"])
        db.add(availability)
        db.flush()
        payload = {"item_kind": "availability.retract", "reason": "Independent batch review",
            "items": [{"target_id": availability.id, "expected_revision": availability.revision, "payload": {}}]}
        for number in range(30):
            owner = m.Identity(issuer="local-development", subject=f"fictional-staged-owner-{number}",
                display_name=f"Fictional staged owner {number}")
            db.add(owner)
            db.flush()
            db.add(m.BulkStaged(department_id=h.seed["department_id"], owner_id=owner.id, status="waiting",
                review_role="coordinator", payload=payload, expires_at=m.now() + timedelta(days=1)))
    h.client("linked_reviewer")
    queries, lock_queries = [], []

    def capture(connection, cursor, statement, parameters, context, many):
        normalized = " ".join(statement.lower().split())
        if normalized in {"select pg_try_advisory_xact_lock(%(key)s), pg_backend_pid()",
                          "select pg_advisory_xact_lock(%(key)s)"}:
            lock_queries.append(normalized)
        elif normalized.startswith("select"):
            queries.append(normalized)

    event.listen(h.app.state.engine, "before_cursor_execute", capture)
    try:
        result = h.get("linked_reviewer", "/api/bulk/staged")
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", capture)
    request.node.user_properties.extend((("staged_listing_data_selects", len(queries)),
        ("staged_listing_lock_selects", len(lock_queries)), ("staged_listing_selects", queries)))
    assert result["count"] == 30
    assert len(queries) <= 12, queries
    assert sum("from availability" in query for query in queries) == 1
    assert sum("from identities" in query for query in queries) <= 2

"""Review regressions: handoffs preserve authority, and shared reads have a bounded scope."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.auth import Actor, digest
from apps.api.commands import command_roles
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import bulk, independence
from apps.worker import main as worker
from packages.contracts import schemas as s
from .clock_support import freeze_clock
from .governance_support import identity
from .test_bulk import preview
from .test_bulk_staged import staged_change


@pytest.mark.parametrize("role", ["staff", "auditor", "programme_lead"])
@pytest.mark.parametrize("kind", ["role.grant", "identity.disable", "person.anonymise", "grant.create"])
def test_forbidden_items_never_offer_or_accept_an_approval_handoff(h, role, kind):
    if role == "programme_lead":
        identity(h, role, [role])
    item = {"payload": {}}
    if kind == "role.grant":
        item["payload"] = {"identity_id": h.seed["identities"][role], "role": "admin", "starts_on": "2026-10-01"}
    elif kind == "identity.disable":
        item.update(target_id=h.seed["identities"]["coordinator"], expected_revision=1)
    elif kind == "person.anonymise":
        item.update(target_id=h.seed["people"][0], expected_revision=1)
    payload = {"item_kind": kind, "items": [item], "reason": "Synthetic permission boundary"}
    client = h.client(role)
    with transaction(h.app.state.engine) as db:
        counts = [db.scalar(select(func.count()).select_from(cls)) for cls in (m.BulkStaged, m.Outbox, m.CommandReceipt)]
    checked = preview(h, role, payload)
    issue = checked["items"][0]["issues"][0]
    assert issue["code"] == "FORBIDDEN"
    assert "handoff" not in issue
    assert [choice["id"] for choice in issue["resolutions"]] == ["skip"]
    forged = {**payload, "resolutions": {"0": "send_for_approval"}}
    assert preview(h, role, forged)["items"][0]["result"] == "problem"
    response = client.post("/api/commands", json={"kind": "bulk.apply", "payload": forged},
                           headers={"idempotency-key": m.uid()})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "BULK_RESOLUTION_STALE"
    with transaction(h.app.state.engine) as db:
        assert [db.scalar(select(func.count()).select_from(cls)) for cls in (m.BulkStaged, m.Outbox, m.CommandReceipt)] == counts
        assert db.get(m.Identity, h.seed["identities"]["coordinator"]).disabled is False


def test_independence_handoff_uses_the_permission_map_instead_of_message_words(h, monkeypatch):
    text = "Synthetic coordinator attestation needs an approver"

    def independent(*args, **kwargs):
        raise Problem(403, "SELF_APPROVAL", text)

    monkeypatch.setattr(independence, "ensure", independent)
    monkeypatch.setitem(s.PERMISSIONS, "eligibility.create", ("credentialer", "admin"))
    payload = {"item_kind": "eligibility.create", "reason": "Synthetic permission-map review", "items": [{"payload": {
        "person_id": h.seed["people"][0], "service_id": h.seed["services"][0], "starts_on": "2028-01-01"}}]}
    issue = preview(h, "credentialer", payload)["items"][0]["issues"][0]
    assert issue["message"] == text
    assert issue["handoff"]["role"] == "credentialer"
    assert issue["resolutions"][0]["id"] == "send_for_approval"


@pytest.mark.parametrize("prefix,canonical,payload,permission", [
    ("catalogue", "catalogue.update", {"catalogue": "people"}, "catalogue.department"),
    ("catalogue", "catalogue.update", {"catalogue": "programmes"}, "catalogue.programmes"),
    ("catalogue", "catalogue.update", {"catalogue": "services"}, "catalogue.update"),
    ("eligibility", "eligibility.create", {}, "eligibility.create"),
    ("grant", "grant.create", {}, "grant.create"),
    ("role", "role.grant", {}, "role.grant"),
])
def test_future_command_prefixes_keep_the_shared_permission_fallback(monkeypatch, prefix, canonical, payload, permission):
    monkeypatch.setitem(s.PERMISSIONS, permission, ("auditor",))
    monkeypatch.setitem(s.PERMISSIONS, "access.system_roles", ())
    assert command_roles(s.Command(kind=prefix + ".future_action", payload=payload)) == ["auditor"]
    assert command_roles(s.Command(kind=canonical, payload=payload)) == ["auditor"]


@pytest.mark.parametrize("read,scope", [("resolve", "bulk-resolve"), ("rows", "bulk-rows"),
                                       ("collections", "bulk-collections"), ("reopen", "bulk-preview")])
def test_bulk_reads_keep_their_own_session_rate_limit_without_using_commands(h, read, scope, monkeypatch):
    freeze_clock(monkeypatch)
    _, stage = staged_change(h)
    client = h.client("coordinator")
    object.__setattr__(h.settings, "rate_bulk_preview_per_minute", 1)
    def request():
        if read == "collections":
            return client.get("/api/bulk/collections")
        if read == "reopen":
            return client.post(f"/api/bulk/staged/{stage['id']}/reopen")
        body = {"collection": "people"} if read == "resolve" else {"selection": {"collection": "people"}, "limit": 1}
        return client.post("/api/bulk/" + read, json=body)
    accepted = request()
    assert accepted.status_code == 200, accepted.text
    refused = request()
    assert refused.status_code == 429 and refused.json()["error"]["code"] == "RATE_LIMITED"
    assert h.client("admin").post("/api/bulk/resolve", json={"collection": "people"}).status_code == 200
    with transaction(h.app.state.engine) as db:
        session = db.scalar(select(m.Session).where(m.Session.identity_id == h.seed["identities"]["coordinator"]))
        window = int(m.now().timestamp()) // 60 * 60
        assert db.scalar(select(m.RateLimit.hits).where(
            m.RateLimit.key_hash == digest(f"{scope}:{session.id}:{window}"))) == 2
        assert db.scalar(select(m.RateLimit.hits).where(m.RateLimit.key_hash == digest(
            f"commands:{h.seed['identities']['coordinator']}:{window}"))) == 2  # Only fixture commands.


def test_bulk_navigation_and_resolution_do_not_spend_the_preview_budget(h, monkeypatch):
    from .test_bulk import people_change
    freeze_clock(monkeypatch)
    client = h.client("coordinator")
    object.__setattr__(h.settings, "rate_bulk_preview_per_minute", 1)
    assert client.get("/api/bulk/collections").status_code == 200
    assert client.post("/api/bulk/resolve", json={"collection": "people"}).status_code == 200
    assert client.post("/api/bulk/rows", json={"selection": {"collection": "people"}, "limit": 1}).status_code == 200
    command = {"kind": "bulk.apply", "payload": people_change(h)}
    accepted = client.post("/api/commands/preview", json=command)
    assert accepted.status_code == 200, accepted.text
    refused = client.post("/api/commands/preview", json=command)
    assert refused.status_code == 429 and refused.json()["error"]["code"] == "RATE_LIMITED"


def test_preview_restores_nontransactional_anonymisation_state(h):
    person = h.configure("people", {"display_name": "Synthetic inactive preview person", "active": False})["record"]
    payload = {"item_kind": "person.anonymise", "reason": "Synthetic rollback boundary", "items": [
        {"target_id": person["id"], "expected_revision": person["revision"], "payload": {}}]}
    with transaction(h.app.state.engine, write=True) as db:
        actor = Actor(h.seed["identities"]["admin"], h.seed["department_id"], h.seed["organisation_id"],
                      "Synthetic admin", frozenset({"admin"}), None, "synthetic-session")
        db.info["anonymised_people"] = {"previous"}
        db.info["bulk_staged_authorized"] = {"previous": {"item_count": 1}}
        checked = bulk.preview(db, actor, s.Command(kind="bulk.apply", payload=payload), h.settings)
        assert checked["items"][0]["result"] == "ready"
        assert db.info["anonymised_people"] == {"previous"}
        assert db.info["bulk_staged_authorized"] == {"previous": {"item_count": 1}}
        assert db.get(m.Person, person["id"]).display_name == "Synthetic inactive preview person"


@pytest.mark.parametrize("role", ["coordinator", "approver", "staff", "programme_lead"])
def test_collection_counts_endpoint_validates_and_excludes_unreadable_types(h, role):
    if role == "programme_lead":
        identity(h, role, [role])
    response = h.client(role).get("/api/bulk/collections")
    assert response.status_code == 200, response.text
    counts = s.BulkCollections.model_validate(response.json()).collections
    assert "identities" not in counts and "roles" not in counts
    selected = "availability" if role == "staff" else "people"
    resolved = h.client(role).post("/api/bulk/resolve", json={"collection": selected})
    assert resolved.status_code == 200, resolved.text
    assert counts[selected].count == resolved.json()["count"]


def test_worker_purges_global_staged_expiry_and_drains_full_batches(h, monkeypatch):
    from apps.api.services import bulk_staged

    _, stage = staged_change(h)
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed["organisation_id"], name="Synthetic retention department")
        db.add(other); db.flush()
        expired = []
        for department, owner, state in [
            (h.seed["department_id"], h.seed["identities"]["coordinator"], "active"),
            (other.id, h.seed["identities"]["staff"], "waiting"),
            (other.id, h.seed["identities"]["credentialer"], "dropped"),
        ]:
            row = m.BulkStaged(department_id=department, owner_id=owner, status=state,
                              expires_at=m.now() - timedelta(days=1), payload=stage["payload"])
            db.add(row); db.flush(); expired.append(row.id)
    monkeypatch.setattr(bulk_staged, "EXPIRE_BATCH_SIZE", 2)
    monkeypatch.setattr(worker, "_bulk_staged_pruned", {})
    assert worker.prune_bulk_staged(h.app.state.engine) == 2
    assert worker.prune_bulk_staged(h.app.state.engine) == 1
    assert worker.prune_bulk_staged(h.app.state.engine) == 0  # Throttled after draining the backlog.
    with transaction(h.app.state.engine) as db:
        rows = [db.get(m.BulkStaged, target) for target in expired]
        assert [row.payload for row in rows] == [{}, {}, {}]
        assert [row.status for row in rows] == ["expired", "expired", "dropped"]
        assert db.get(m.BulkStaged, stage["id"]).payload
        notes = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id.in_(expired))))
        assert len(notes) == 3 and all("payload" not in note.snapshot for note in notes)

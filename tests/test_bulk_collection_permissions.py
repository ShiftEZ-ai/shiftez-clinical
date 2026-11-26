"""Bulk reads share the live list-endpoint permissions and never widen their department or self scope."""
from datetime import date
import uuid

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_feedback_api import screenshot, submit


LISTS = {"history": ("/api/audit", "events"), "outbox": ("/api/operations", "outbox"),
         "identities": ("/api/access", "identities"), "roles": ("/api/access", "bindings"),
         "feedback": ("/api/feedback", "rows")}


def bulk_ids(h, actor, collection, *, status=200, include=None):
    selection = {"collection": collection, **({"include_ids": include} if include is not None else {})}
    resolved = h.client(actor).post("/api/bulk/resolve", json=selection)
    rows = h.client(actor).post("/api/bulk/rows", json={"selection": selection})
    assert resolved.status_code == rows.status_code == status, resolved.text + rows.text
    if status != 200:
        return None
    selected, page = resolved.json(), rows.json()
    assert selected["count"] == page["total"]
    assert {r["id"] for r in selected["sample"]} == {r["id"] for r in page["rows"]}
    assert all(not {"password_hash", "private_reason", "payload", "screenshot", "screenshot_media_type"}.intersection(r)
               for r in selected["sample"] + page["rows"])
    return {r["id"] for r in page["rows"]}


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_bulk_sign_in_choices_share_access_issuers_and_protect_system_maintenance(request, database):
    from dataclasses import replace
    from sqlalchemy import select
    from apps.api.main import create_app
    from scripts.system_command import MAINTENANCE_ISSUER
    h = request.getfixturevalue(database)
    h.settings = replace(h.settings, oidc_issuer="https://example.invalid/synthetic-bulk-idp")
    h.app.state.engine.dispose()
    h.app = create_app(h.settings)
    with transaction(h.app.state.engine, write=True) as db:
        allowed = m.Identity(issuer=h.settings.oidc_issuer, subject="synthetic-allowed-issuer", display_name="Allowed sign-in")
        hidden = m.Identity(issuer="urn:synthetic:another-provider", subject="synthetic-hidden-issuer", display_name="Hidden provider")
        db.add_all([allowed, hidden]); db.flush()
        maintenance = db.scalar(select(m.Identity).where(m.Identity.issuer == MAINTENANCE_ISSUER))
        allowed_id, hidden_id, maintenance_id = allowed.id, hidden.id, maintenance.id
    listed = {row["id"] for row in h.get("admin", "/api/access")["identities"]}
    assert allowed_id in listed and hidden_id not in listed and maintenance_id not in listed
    assert bulk_ids(h, "admin", "identities") == listed
    assert bulk_ids(h, "admin", "identities", include=[hidden_id, maintenance_id]) == set()
    assert bulk_ids(h, "admin", "identities", include=[allowed_id]) == {allowed_id}
    for command in ("identity.enable", "role.grant"):
        payload = {"reason": "System actors never become interactive sign-ins"}
        if command == "role.grant":
            payload.update(identity_id=maintenance_id, role="staff", starts_on="2020-01-01")
        refused = h.command("admin", command, payload,
            maintenance_id if command == "identity.enable" else None, status=409)
        assert refused["error"]["code"] == "AUTOMATION_IDENTITY"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Identity, maintenance_id).disabled
        assert db.scalar(select(m.RoleBinding.id).where(m.RoleBinding.identity_id == maintenance_id)) is None


@pytest.mark.parametrize("collection", ["history", "outbox", "identities", "roles"])
@pytest.mark.parametrize("actor", ["admin", "operator", "auditor", "staff"])
def test_bulk_administrative_reads_match_list_endpoint_grants(h, collection, actor):
    path, key = LISTS[collection]
    allowed = actor in ({"admin", "operator"} if collection in {"identities", "roles"} else {"operator", "auditor"})
    expected = 200 if allowed else 403
    listed = h.get(actor, path, status=expected)
    selected = bulk_ids(h, actor, collection, status=expected)
    if allowed:
        assert selected == {row["id"] for row in listed[key]}


@pytest.mark.parametrize("actor", ["admin", "operator", "auditor", "staff"])
def test_feedback_bulk_selection_matches_own_or_all_list_scope(h, actor):
    own = submit(h, actor, message="Synthetic own report", screenshot=screenshot())["id"]
    other = submit(h, "coordinator", message="Synthetic other report")["id"]
    listed = {row["id"] for row in h.get(actor, "/api/feedback")["rows"]}
    assert listed == ({own, other} if actor in {"operator", "auditor"} else {own})
    assert bulk_ids(h, actor, "feedback", include=[own, other]) == listed


@pytest.mark.parametrize("actor", ["admin", "operator", "auditor", "staff"])
def test_feedback_bulk_selection_uses_current_read_all_permission(h, actor, monkeypatch):
    own = submit(h, actor, message="Synthetic own remapped report")["id"]
    other = submit(h, "coordinator", message="Synthetic other remapped report")["id"]
    monkeypatch.setitem(s.PERMISSIONS, "feedback.read_all", ("staff",))
    listed = {row["id"] for row in h.get(actor, "/api/feedback")["rows"]}
    assert listed == ({own, other} if actor in {"admin", "staff"} else {own})
    assert bulk_ids(h, actor, "feedback", include=[own, other]) == listed


@pytest.mark.parametrize("collection", ["identities", "roles"])
@pytest.mark.parametrize("actor", ["admin", "operator", "auditor", "staff", "coordinator", s.INSTALLATION_ADMIN_ROLE])
def test_access_bulk_selection_uses_current_system_role_permission(h, collection, actor, monkeypatch):
    monkeypatch.setitem(s.PERMISSIONS, "access.system_roles", ("coordinator",))
    expected = 200 if actor in {"admin", "coordinator"} else 403
    limited = actor in s.PERMISSIONS["access.installation_admin"] and expected == 403
    if limited:
        holder_id = h.get(s.INSTALLATION_ADMIN_ROLE, "/api/me")["identity_id"]
    listed = h.get(actor, "/api/access", status=200 if limited else expected)
    selected = bulk_ids(h, actor, collection, status=expected)
    if limited:
        assert set(listed) == {"bindings", "identities"}
        assert listed["identities"] and all(set(row) == {"id", "display_name", "disabled"} for row in listed["identities"])
        assert all(row["role"] == s.INSTALLATION_ADMIN_ROLE for row in listed["bindings"])
        assert {row["identity_id"] for row in listed["bindings"]} == {holder_id}
        assert selected is None
    elif expected == 200:
        assert selected == {row["id"] for row in listed[LISTS[collection][1]]}


@pytest.mark.parametrize("collection,permission", [("history", "audit.read"), ("outbox", "system.operations")])
@pytest.mark.parametrize("actor", ["admin", "operator", "auditor", "coordinator"])
def test_bulk_history_and_queue_use_current_permission_map(h, collection, permission, actor, monkeypatch):
    monkeypatch.setitem(s.PERMISSIONS, permission, ("coordinator",))
    expected = 200 if actor in {"admin", "coordinator"} else 403
    listed = h.get(actor, LISTS[collection][0], status=expected)
    selected = bulk_ids(h, actor, collection, status=expected)
    if expected == 200:
        assert selected == {row["id"] for row in listed[LISTS[collection][1]]}


@pytest.mark.parametrize("collection", ["identities", "roles", "history", "outbox", "feedback"])
def test_developer_bulk_reads_preserve_list_department_and_provisioning_scope(h, collection):
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed["organisation_id"], name="Foreign synthetic department")
        db.add(department); db.flush()
        identity = m.Identity(issuer="local-development", subject="foreign-bulk-reader", display_name="Foreign identity")
        db.add(identity); db.flush()
        binding = m.RoleBinding(department_id=department.id, identity_id=identity.id, role="staff", starts_on=date(2020, 1, 1))
        audit = m.AuditEvent(department_id=department.id, actor_id=identity.id, action="synthetic.foreign",
                             reason="Foreign audit entry", detail={}, correlation_id=str(uuid.uuid4()))
        outbox = m.Outbox(department_id=department.id, event_key=str(uuid.uuid4()), kind="request", payload={"private_reason": "Secret"})
        feedback = m.FeedbackReport(department_id=department.id, reporter_identity_id=identity.id, kind="problem",
            message="Foreign feedback", route="#home", environment="dev", viewport={}, fingerprint="f" * 64)
        db.add_all([binding, audit, outbox, feedback]); db.flush()
        foreign = {"identities": identity.id, "roles": binding.id, "history": audit.id,
                   "outbox": outbox.id, "feedback": feedback.id}[collection]
    selected = bulk_ids(h, "operator", collection, include=[foreign])
    if collection == "identities":
        # Access intentionally includes same-organisation sign-ins so they can receive a department binding.
        listed = {row["id"] for row in h.get("operator", "/api/access")["identities"]}
        assert foreign in listed and selected == {foreign}
    else:
        assert selected == set()

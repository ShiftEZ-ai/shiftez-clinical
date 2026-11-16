"""Authentication configuration trust boundaries, encrypted Undo and recovery access."""
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from apps.api import models as m, oidc
from apps.api.auth import Actor, current_roles
from apps.api.data_crypto import decrypt_secret
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.main import create_app
from apps.api.serialization import record
from apps.api.services import auth_configuration as config
from apps.api.services.independence import today
from migrations.safety import unreviewed
from packages.contracts import schemas as s
from packages.domain import time as clock
from .governance_support import developer, identity
from .test_bulk_pending_privacy import DATABASES

PATH = "/api/admin/auth-config"
SECRET = "synthetic-institutional-client-secret"
pytestmark = pytest.mark.usefixtures("public_oidc_dns")


def payload(**extra):
    return {"expected_revision": 0, "confirmed": True, "reason": "Synthetic IT configuration", **extra}


def provider(h, **extra):
    values = {"mode": "both", "institutional_enabled": True, "issuer": "https://idp.test", "client_id": "rota-test",
              "allowed_hosts": ["idp.test"], "institutional_bindings": [
                  {"identity_id": h.seed["identities"]["staff"], "subject": "hospital-staff-subject"}]}
    return payload(**(values | extra))


@pytest.fixture(params=DATABASES)
def department_admins(request):
    h = request.getfixturevalue(request.param)
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed["organisation_id"], name="Other department (test)")
        identity = m.Identity(issuer="local-development", subject="other-admin", display_name="Other admin (test)",
                              password_hash=db.get(m.Identity, h.seed["identities"]["admin"]).password_hash)
        db.add_all([department, identity])
        db.flush()
        db.add(m.RoleBinding(identity_id=identity.id, department_id=department.id, role="admin", starts_on=m.now().date()))
        department_id = department.id
    return h, h.client("admin"), h.client("other-admin"), department_id


@pytest.mark.parametrize("omit_issuer", [False, True])
def test_other_department_admin_can_change_shared_settings_and_undo_with_unchanged_links(department_admins, omit_issuer):
    h, admin, other, department_id = department_admins
    saved = admin.put(PATH, json=provider(h))
    assert saved.status_code == 200, saved.text
    links = saved.json()["institutional_bindings"]
    assert other.get(PATH).json()["institutional_bindings"] == links
    submitted = [{key: value for key, value in link.items() if not omit_issuer or key != "issuer"} for link in links]
    changed = other.put(PATH, json=provider(h, expected_revision=1, remember_me_days=45, institutional_bindings=submitted))
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == 2 and changed.json()["remember_me_days"] == 45
    assert changed.json()["institutional_bindings"] == links and changed.json()["can_undo"]
    undone = other.post(PATH + "/undo", json=payload(expected_revision=2))
    assert undone.status_code == 200, undone.text
    assert undone.json()["revision"] == 3 and undone.json()["remember_me_days"] == 30
    assert undone.json()["institutional_bindings"] == links
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AuthConfiguration, config.SINGLETON)
        assert row.previous["data"]["remember_me_days"] == 45
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.department_id == department_id,
                                                         m.AuditEvent.action.like("auth.config.%"))))
        assert {event.action for event in events} == {"auth.config.update", "auth.config.undo"}
        assert all(event.detail["changed_fields"] == ["remember_me_days"] for event in events)


@pytest.mark.parametrize("operation", ["new", "subject", "identity", "issuer", "remove", "reset", "undo"])
def test_other_department_admin_cannot_add_change_or_remove_links(department_admins, operation):
    h, admin, other, _ = department_admins
    saved = admin.put(PATH, json=provider(h))
    assert saved.status_code == 200, saved.text
    links = saved.json()["institutional_bindings"]
    submitted = provider(h, expected_revision=1, institutional_bindings=[dict(link) for link in links])
    if operation == "new":
        submitted["institutional_bindings"].append({"identity_id": h.seed["identities"]["approver"], "subject": "new-subject"})
    elif operation == "subject":
        submitted["institutional_bindings"][0]["subject"] = "changed-subject"
    elif operation == "identity":
        submitted["institutional_bindings"][0]["identity_id"] = h.seed["identities"]["approver"]
    elif operation == "issuer":
        submitted.update(issuer="https://replacement.test", allowed_hosts=["replacement.test"])
        submitted["institutional_bindings"][0]["issuer"] = submitted["issuer"]
    elif operation == "remove":
        submitted["institutional_bindings"] = []
    if operation == "reset":
        response = other.request("DELETE", PATH, json=payload(expected_revision=1))
    elif operation == "undo":
        response = other.post(PATH + "/undo", json=payload(expected_revision=1))
    else:
        response = other.put(PATH, json=submitted)
    assert response.status_code == 403 and response.json()["error"]["code"] == "OPERATOR_REQUIRED", response.text
    assert other.get(PATH).json() == saved.json()
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action.like("auth.config.%"))))
        assert len(events) == 1


def test_other_department_unchanged_link_still_requires_matching_provider_issuer(department_admins):
    h, admin, other, _ = department_admins
    saved = admin.put(PATH, json=provider(h))
    assert saved.status_code == 200, saved.text
    response = other.put(PATH, json=provider(h, expected_revision=1, issuer="https://replacement.test",
                                            allowed_hosts=["replacement.test"],
                                            institutional_bindings=saved.json()["institutional_bindings"]))
    assert response.status_code == 422 and response.json()["error"]["code"] == "AUTH_BINDING_ISSUER"
    assert other.get(PATH).json() == saved.json()


@pytest.mark.parametrize("ended", ["expired", "revoked"])
@pytest.mark.parametrize("operation", ["remove", "reset", "undo"])
def test_ended_role_binding_cleanup_and_exact_undo(department_admins, ended, operation):
    h, admin, other, _ = department_admins
    object.__setattr__(h.settings, "fmuc_data_key", "17" * 32)
    target = identity(h, "departed-signin", ["staff"])
    links = [{"identity_id": target, "issuer": "https://idp.test", "subject": "departed-subject"}]
    saved = admin.put(PATH, json=provider(h, institutional_bindings=links, client_secret=SECRET))
    assert saved.status_code == 200, saved.text
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.AuthConfiguration, config.SINGLETON)
        original = {"data": row.data, "secret_ciphertext": row.secret_ciphertext}
        grant = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == target))
        if ended == "expired":
            grant.ends_on = today(db, h.seed["department_id"])
        else:
            grant.revoked = True
    with transaction(h.app.state.engine) as db:
        assert not current_roles(db, target, h.seed["department_id"])

    for client in (other, admin):
        if operation == "remove":
            response = client.put(PATH, json=provider(h, expected_revision=1, institutional_bindings=[]))
        elif operation == "reset":
            response = client.request("DELETE", PATH, json=payload(expected_revision=1))
        else:
            response = client.post(PATH + "/undo", json=payload(expected_revision=1))
        if client is other:
            assert response.status_code == 403 and response.json()["error"]["code"] == "OPERATOR_REQUIRED", response.text
            assert admin.get(PATH).json() == saved.json()
        else:
            assert response.status_code == 200, response.text
            assert response.json()["institutional_bindings"] == [] and response.json()["revision"] == 2

    creation = admin.put(PATH, json=provider(h, expected_revision=2, institutional_bindings=links))
    assert creation.status_code == 422 and creation.json()["error"]["code"] == "AUTH_BINDING_INVALID", creation.text
    refused = other.post(PATH + "/undo", json=payload(expected_revision=2))
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "OPERATOR_REQUIRED", refused.text
    restored = admin.post(PATH + "/undo", json=payload(expected_revision=2))
    assert restored.status_code == 200, restored.text
    assert restored.json()["institutional_bindings"] == links and restored.json()["revision"] == 3
    replacement = admin.put(PATH, json=provider(h, expected_revision=3,
                                               institutional_bindings=[dict(links[0], subject="replacement-subject")]))
    assert replacement.status_code == 422 and replacement.json()["error"]["code"] == "AUTH_BINDING_INVALID", replacement.text
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AuthConfiguration, config.SINGLETON)
        assert row.data == original["data"] and row.secret_ciphertext == original["secret_ciphertext"]
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action.like("auth.config.%"))
                                .order_by(m.AuditEvent.created_at)))
        action = "update" if operation == "remove" else operation
        assert [event.action for event in events] == ["auth.config.update", "auth.config." + action, "auth.config.undo"]
        assert all(event.action in s.AUDIT_ACTION_CATALOGUE for event in events)
        assert all(event.actor_id == h.seed["identities"]["admin"] for event in events)


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("enabled", [False, True])
def test_activating_existing_binding_requires_live_access(request, database, enabled):
    h = request.getfixturevalue(database)
    admin = h.client("admin")
    target = identity(h, "departed-signin", ["staff"])
    links = [{"identity_id": target, "issuer": "https://idp.test", "subject": "departed-subject"}]
    saved = admin.put(PATH, json=provider(h, mode="password_only", institutional_enabled=enabled, institutional_bindings=links))
    assert saved.status_code == 200, saved.text
    with transaction(h.app.state.engine, write=True) as db:
        grant = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == target))
        grant.ends_on = today(db, h.seed["department_id"])
    response = admin.put(PATH, json=provider(h, expected_revision=1, institutional_bindings=links))
    assert response.status_code == 422 and response.json()["error"]["code"] == "AUTH_BINDING_INVALID", response.text
    assert admin.get(PATH).json() == saved.json()


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("operation", ["remove", "reset", "undo"])
def test_ended_role_binding_cleanup_keeps_future_system_grant_protection(request, database, operation):
    from datetime import timedelta
    h = request.getfixturevalue(database)
    admin, operator = h.client("admin"), h.client(developer(h))
    target = identity(h, "departed-signin", ["staff"])
    links = [{"identity_id": target, "issuer": "https://idp.test", "subject": "departed-subject"}]
    saved = operator.put(PATH, json=provider(h, institutional_bindings=links))
    assert saved.status_code == 200, saved.text
    with transaction(h.app.state.engine, write=True) as db:
        grant = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == target))
        day = today(db, h.seed["department_id"])
        grant.ends_on = day
        db.add(m.RoleBinding(identity_id=target, department_id=h.seed["department_id"], role="operator",
                             starts_on=day + timedelta(days=1)))
    with transaction(h.app.state.engine) as db:
        assert not current_roles(db, target, h.seed["department_id"])
    for client in (admin, operator):
        if operation == "remove":
            response = client.put(PATH, json=provider(h, expected_revision=1, institutional_bindings=[]))
        elif operation == "reset":
            response = client.request("DELETE", PATH, json=payload(expected_revision=1))
        else:
            response = client.post(PATH + "/undo", json=payload(expected_revision=1))
        if client is admin:
            assert response.status_code == 403 and response.json()["error"]["code"] == "FORBIDDEN", response.text
            assert admin.get(PATH).json() == saved.json()
        else:
            assert response.status_code == 200 and response.json()["institutional_bindings"] == [], response.text
    refused = admin.post(PATH + "/undo", json=payload(expected_revision=2))
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "FORBIDDEN", refused.text
    restored = operator.post(PATH + "/undo", json=payload(expected_revision=2))
    assert restored.status_code == 200 and restored.json()["institutional_bindings"] == links, restored.text


@pytest.mark.parametrize("operation", ["unchanged", "subject", "remove", "reset", "undo"])
def test_protected_system_links_only_require_system_authority_when_edited(h, operation):
    links = [{"identity_id": h.seed["identities"]["operator"], "issuer": "https://idp.test", "subject": "hospital-developer"}]
    data = s.AuthConfigInput.model_validate(provider(h, institutional_bindings=links)).model_dump(
        include=set(s.AuthSettings.model_fields), mode="json")
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.AuthConfiguration(id=config.SINGLETON, data=data,
                                  previous={"data": s.AuthSettings().model_dump(mode="json"), "secret_ciphertext": None}))
    admin = h.client("admin")
    if operation == "reset":
        response = admin.request("DELETE", PATH, json=payload(expected_revision=1))
    elif operation == "undo":
        response = admin.post(PATH + "/undo", json=payload(expected_revision=1))
    else:
        changed = [] if operation == "remove" else [dict(links[0], subject="changed" if operation == "subject" else links[0]["subject"])]
        response = admin.put(PATH, json=provider(h, expected_revision=1, remember_me_days=45, institutional_bindings=changed))
    if operation == "unchanged":
        assert response.status_code == 200, response.text
        assert response.json()["institutional_bindings"] == links
    else:
        assert response.status_code == 403 and response.json()["error"]["code"] == "FORBIDDEN", response.text
        assert admin.get(PATH).json()["institutional_bindings"] == links


@pytest.mark.parametrize("identity_state", ["missing", "automation"])
def test_unchanged_links_still_require_an_existing_signin_identity(h, identity_state):
    admin = h.client("admin")
    saved = admin.put(PATH, json=provider(h))
    assert saved.status_code == 200, saved.text
    links = saved.json()["institutional_bindings"]
    with transaction(h.app.state.engine, write=True) as db:
        if identity_state == "missing":
            links[0]["identity_id"] = "00000000-0000-0000-0000-000000000000"
            row = db.get(m.AuthConfiguration, config.SINGLETON)
            row.data = {**row.data, "institutional_bindings": links}
        else:
            from scripts.system_command import MAINTENANCE_ISSUER
            db.get(m.Identity, links[0]["identity_id"]).issuer = MAINTENANCE_ISSUER
    response = admin.put(PATH, json=provider(h, expected_revision=1, remember_me_days=45, institutional_bindings=links))
    code = "AUTH_BINDING_INVALID" if identity_state == "missing" else "AUTOMATION_IDENTITY"
    assert response.status_code in {409, 422} and response.json()["error"]["code"] == code
    assert admin.get(PATH).json()["revision"] == 1


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("binding_state", ["current", "previous", "both", "unrelated"])
def test_anonymise_removes_institutional_subjects_from_configuration_and_undo(request, database, binding_state):
    h = request.getfixturevalue(database)
    object.__setattr__(h.settings, "fmuc_data_key", "17" * 32)
    owner = h.seed["identities"]["staff"]
    person = h.configure("people", {"display_name": "Synthetic departing sign-in owner", "active": False})["record"]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, owner).person_id = person["id"]
    other = {"identity_id": h.seed["identities"]["approver"], "issuer": "https://idp.test", "subject": "other-subject"}
    previous = [other] + ([{"identity_id": owner, "issuer": "https://idp.test", "subject": "private-old-subject"}]
                          if binding_state in {"previous", "both"} else [])
    current = [other] + ([{"identity_id": owner, "issuer": "https://idp.test", "subject": "private-current-subject"}]
                         if binding_state in {"current", "both"} else [])
    admin = h.client("admin")
    assert admin.put(PATH, json=provider(h, institutional_bindings=previous, client_secret=SECRET)).status_code == 200
    assert admin.put(PATH, json=provider(h, expected_revision=1, institutional_bindings=current)).status_code == 200
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AuthConfiguration, config.SINGLETON)
        ciphertext, data, prior = row.secret_ciphertext, row.data, row.previous
    h.command("admin", "person.anonymise", {"reason": "Remove synthetic former sign-in details"},
              person["id"], person["revision"])
    changed = binding_state != "unrelated"
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AuthConfiguration, config.SINGLETON)
        assert row.revision == 2 + changed and row.secret_ciphertext == ciphertext
        assert row.data == {**data, "institutional_bindings": [other]}
        assert row.previous == {**prior, "data": {**prior["data"], "institutional_bindings": [other]}}
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "auth.config.anonymise")))
        assert len(events) == int(changed)
        assert all(event.action in s.AUDIT_ACTION_CATALOGUE for event in events)
        assert "private-" not in json.dumps([row.data, row.previous, [event.detail for event in events]])
    restored = admin.post(PATH + "/undo", json=payload(expected_revision=2 + changed))
    assert restored.status_code == 200, restored.text
    assert restored.json()["institutional_bindings"] == [other]
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AuthConfiguration, config.SINGLETON)
        assert row.secret_ciphertext == ciphertext
        assert "private-" not in json.dumps([row.data, row.previous])


def test_default_is_password_only_despite_legacy_provider_environment(h):
    settings = replace(h.settings, oidc_issuer="https://legacy-idp.test", oidc_client_secret=SECRET)
    with TestClient(create_app(settings, h.app.state.engine), base_url=settings.public_origin) as client:
        public = client.get("/auth/config").json()
        assert public["mode"] == "password_only" and public["institutional_login"] is False
        assert client.get("/auth/start", follow_redirects=False).status_code == 404
    assert SECRET not in json.dumps(public)


@pytest.mark.parametrize("role", s.ROLE_NAMES)
def test_configuration_domain_requires_exact_admin_role(h, role):
    with transaction(h.app.state.engine) as db:
        actor = Actor(h.seed["identities"]["staff"], h.seed["department_id"], h.seed["organisation_id"],
                      "Synthetic reviewer", frozenset({role}), None, "test-session")
        if role == "admin":
            assert config.status(db, actor, h.settings)["revision"] == 0
        else:
            with pytest.raises(Problem) as error:
                config.status(db, actor, h.settings)
            assert error.value.status == 403


def test_admin_api_requires_session_csrf_origin_confirmation_and_revision(h):
    admin = h.client("admin")
    assert h.client("staff").get(PATH).status_code == 403
    assert h.client("staff").put(PATH, json=payload()).status_code == 403
    assert admin.put(PATH, json=payload(), headers={"origin": "https://evil.test"}).status_code == 403
    assert admin.put(PATH, json=payload(), headers={"x-csrf-token": "wrong"}).status_code == 403
    assert admin.put(PATH, json=payload(confirmed=False)).status_code == 422
    assert admin.put(PATH, json=payload(expected_revision=1)).status_code == 409
    assert admin.post(PATH + "/test-connection", json={"expected_revision": 0},
                      headers={"x-csrf-token": "wrong"}).status_code == 403
    assert admin.put(PATH, json=payload()).status_code == 200
    assert admin.put(PATH, json=payload()).status_code == 409


def test_secret_is_write_only_encrypted_and_undo_restores_ciphertext(h):
    object.__setattr__(h.settings, "fmuc_data_key", "17" * 32)
    admin = h.client("admin")
    saved = admin.put(PATH, json=provider(h, client_secret=SECRET))
    assert saved.status_code == 200, saved.text
    assert saved.json()["client_secret_set"] is True
    assert "client_secret" not in saved.json() and SECRET not in saved.text
    assert saved.json()["redirect_uri"] == h.settings.public_origin + "/auth/callback"
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AuthConfiguration, config.SINGLETON)
        original = row.secret_ciphertext
        assert SECRET not in original and decrypt_secret(original, config.SECRET_CONTEXT, h.settings) == SECRET
        assert "secret_ciphertext" not in record(row) and "previous" not in record(row)
    cleared = admin.put(PATH, json=provider(h, expected_revision=1, client_secret=""))
    assert cleared.status_code == 200, cleared.text
    assert not cleared.json()["client_secret_set"]
    restored = admin.post(PATH + "/undo", json=payload(expected_revision=2))
    assert restored.status_code == 200 and restored.json()["client_secret_set"]
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AuthConfiguration, config.SINGLETON)
        assert row.secret_ciphertext == original
        assert SECRET not in json.dumps(row.previous)
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action.like("auth.config.%"))))
        assert len(events) == 3 and all(SECRET not in json.dumps(e.detail) for e in events)
        assert all(e.detail["client_secret_changed"] for e in events)
        assert config.effective(db, h.settings, secret=True).oidc_client_secret == SECRET


def test_secret_requires_deployment_key_and_wrong_key_never_affects_password_admin(h):
    admin = h.client("admin")
    assert admin.put(PATH, json=provider(h, client_secret=SECRET)).status_code == 409
    object.__setattr__(h.settings, "fmuc_data_key", "18" * 32)
    assert admin.put(PATH, json=provider(h, mode="institutional_only", client_secret=SECRET)).status_code == 200
    object.__setattr__(h.settings, "fmuc_data_key", "19" * 32)
    assert admin.get(PATH).status_code == 200
    with transaction(h.app.state.engine) as db:
        with pytest.raises(Problem):
            config.effective(db, h.settings, secret=True)
        assert config.password_eligible(db, db.get(m.Identity, h.seed["identities"]["admin"]), h.settings)


def test_reset_is_off_and_audited_without_deleting_configuration(h):
    admin = h.client("admin")
    assert admin.put(PATH, json=provider(h)).status_code == 200
    response = admin.request("DELETE", PATH, json=payload(expected_revision=1))
    assert response.status_code == 200
    assert response.json()["mode"] == "password_only" and not response.json()["institutional_enabled"]
    assert response.json()["can_undo"]
    assert admin.post(PATH + "/undo", json=payload(expected_revision=2)).json()["mode"] == "both"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.AuthConfiguration, config.SINGLETON) is not None


@pytest.mark.parametrize("extra", [
    {"issuer": "http://idp.test"}, {"issuer": ('https://user:secret' + chr(64) + 'idp.test')},
    {"issuer": "https://idp.test?query=secret"}, {"issuer": "https://idp.test/"},
    {"discovery_url": "https://untrusted.test/discovery"}, {"allowed_hosts": ["*.test"]},
    {"allowed_domains": ["https://hospital.test"]}, {"scopes": ["profile"]},
    {"institutional_enabled": False}, {"client_id": ""},
])
def test_invalid_provider_configuration_is_rejected(h, extra):
    assert h.client("admin").put(PATH, json=provider(h, **extra)).status_code == 422


def test_binding_requires_existing_department_identity_and_unique_subjects(h):
    admin = h.client("admin")
    invalid = provider(h)
    invalid["institutional_bindings"][0]["identity_id"] = "00000000-0000-0000-0000-000000000000"
    assert admin.put(PATH, json=invalid).status_code == 422
    invalid = provider(h)
    invalid["institutional_bindings"] *= 2
    assert admin.put(PATH, json=invalid).status_code == 422


def test_admin_cannot_rebind_an_identity_with_protected_system_authority(h):
    binding = [{"identity_id": h.seed["identities"]["operator"], "subject": "hospital-developer"}]
    assert h.client("admin").put(PATH, json=provider(h, institutional_bindings=binding)).status_code == 403


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("actor", ["admin", "developer"])
@pytest.mark.parametrize("starts_on,ends_on,revoked,protected", [
    ("2026-10-25", "2026-10-26", False, True),
    ("2026-10-27", "2026-10-28", False, True),
    ("2026-10-24", "2026-10-25", False, False),
    ("2026-10-25", "2026-10-26", True, False),
], ids=["active-local-day", "future-grant", "expired-local-day", "revoked"])
def test_protected_binding_uses_organisation_local_expiry(request, database, monkeypatch, actor,
                                                        starts_on, ends_on, revoked, protected):
    h = request.getfixturevalue(database)
    monkeypatch.setattr(clock, "_offset", clock._offset)
    clock.pin_clock("2026-10-26T01:00:00+00:00")
    developer(h)
    target = identity(h, "protected-signin", ["staff", "operator"])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed["organisation_id"]).timezone = "America/New_York"
        grant = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == target, m.RoleBinding.role == "operator"))
        grant.starts_on, grant.ends_on, grant.revoked = date.fromisoformat(starts_on), date.fromisoformat(ends_on), revoked
    with transaction(h.app.state.engine) as db:
        assert m.now().date() == date(2026, 10, 26) and today(db, h.seed["department_id"]) == date(2026, 10, 25)
        active = starts_on == "2026-10-25" and not revoked
        assert ("operator" in current_roles(db, target, h.seed["department_id"])) is active
    links = [{"identity_id": target, "issuer": "https://idp.test", "subject": "original-subject"}]
    saved = h.client("developer").put(PATH, json=provider(h, institutional_bindings=links))
    assert saved.status_code == 200, saved.text
    replacement = [dict(links[0], subject="replacement-subject")]
    response = h.client(actor).put(PATH, json=provider(h, expected_revision=1, institutional_bindings=replacement))
    if protected and actor == "admin":
        assert response.status_code == 403 and response.json()["error"]["code"] == "FORBIDDEN", response.text
        assert h.client(actor).get(PATH).json() == saved.json()
    else:
        assert response.status_code == 200, response.text
        assert response.json()["institutional_bindings"] == replacement and response.json()["revision"] == 2


def test_admin_cannot_rebind_an_identity_managed_across_departments(h):
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed["organisation_id"], name="Other department (test)")
        db.add(department)
        db.flush()
        db.add(m.RoleBinding(identity_id=h.seed["identities"]["staff"], department_id=department.id,
                             role="staff", starts_on=m.now().date()))
    response = h.client("admin").put(PATH, json=provider(h))
    assert response.status_code == 403 and response.json()["error"]["code"] == "OPERATOR_REQUIRED"


def test_future_permanent_grant_cannot_replace_a_current_permanent_recovery_admin(h):
    from datetime import timedelta
    admin = h.client("admin")
    with transaction(h.app.state.engine, write=True) as db:
        grant = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == h.seed["identities"]["admin"],
                                                     m.RoleBinding.role == "admin"))
        grant.ends_on = m.now().date() + timedelta(days=1)
        db.add(m.RoleBinding(identity_id=grant.identity_id, department_id=grant.department_id, role="admin",
                             starts_on=m.now().date() + timedelta(days=2)))
    response = admin.put(PATH, json=provider(h))
    assert response.status_code == 409 and response.json()["error"]["code"] == "BREAK_GLASS_REQUIRED"


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("operation", ["remove", "reset", "undo"])
def test_binding_cleanup_cannot_bypass_last_password_admin(request, database, operation):
    from datetime import timedelta
    h = request.getfixturevalue(database)
    admin = h.client("admin")
    saved = admin.put(PATH, json=provider(h))
    assert saved.status_code == 200, saved.text
    with transaction(h.app.state.engine, write=True) as db:
        grant = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == h.seed["identities"]["admin"],
                                                     m.RoleBinding.role == "admin"))
        grant.ends_on = today(db, h.seed["department_id"]) + timedelta(days=1)
    if operation == "remove":
        response = admin.put(PATH, json=provider(h, expected_revision=1, institutional_bindings=[]))
    elif operation == "reset":
        response = admin.request("DELETE", PATH, json=payload(expected_revision=1))
    else:
        response = admin.post(PATH + "/undo", json=payload(expected_revision=1))
    assert response.status_code == 409 and response.json()["error"]["code"] == "BREAK_GLASS_REQUIRED", response.text
    assert admin.get(PATH).json() == saved.json()


def test_connection_checks_saved_revision_without_authorizing_or_logging_in(h, monkeypatch):
    admin = h.client("admin")
    assert admin.put(PATH, json=provider(h)).status_code == 200
    calls = []
    monkeypatch.setattr(oidc, "document", lambda settings: calls.append(settings.oidc_issuer) or {"jwks_uri": "https://idp.test/keys"})
    monkeypatch.setattr(oidc, "signing_keys", lambda settings, url: {"keys": [{"kid": "test"}]})
    response = admin.post(PATH + "/test-connection", json={"expected_revision": 1})
    assert response.status_code == 200 and response.json()["ok"] is True
    assert "test sign-in" in response.json()["message"] and calls == ["https://idp.test"]
    assert admin.post(PATH + "/test-connection", json={"expected_revision": 0}).status_code == 409


def test_connection_change_during_remote_test_is_rejected(h, monkeypatch):
    admin = h.client("admin")
    assert admin.put(PATH, json=provider(h)).status_code == 200
    def remote(settings):
        assert admin.put(PATH, json=provider(h, expected_revision=1)).status_code == 200
        return {"ok": True, "revision": 1, "message": "test"}
    monkeypatch.setattr(config, "test_connection", remote)
    assert admin.post(PATH + "/test-connection", json={"expected_revision": 1}).status_code == 409


def test_config_is_environment_database_local(h, tmp_path):
    assert h.client("admin").put(PATH, json=provider(h)).status_code == 200
    from scripts.manage import migrate
    from apps.api.db import make_engine
    other = "sqlite:///" + str(tmp_path / "other-environment.db")
    migrate(other)
    engine = make_engine(other)
    try:
        with transaction(engine) as db:
            assert config.current(db, h.settings).mode == "password_only"
    finally:
        engine.dispose()


def test_managed_recovery_admin_survives_institutional_only_and_cannot_be_disabled(h, tmp_path):
    from tests.test_password_login import provision, client_for, USERNAME, PASSWORD
    settings, users, identity_id = provision(h, tmp_path)
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.RoleBinding(identity_id=identity_id, department_id=h.seed["department_id"], role="admin", starts_on=m.now().date()))
        # The second admin is institutional, without a usable local password.
        db.get(m.Identity, h.seed["identities"]["admin"]).password_hash = None
    with client_for(h, settings) as admin:
        assert admin.post("/auth/login", json={"username": USERNAME, "password": PASSWORD}).status_code == 200
        admin.headers["x-csrf-token"] = admin.cookies["rota_csrf"]
        assert admin.put(PATH, json=provider(h, mode="institutional_only")).status_code == 200
        assert admin.post("/auth/identify", json={"identifier": USERNAME}).json() == {"method": "password"}
        assert admin.post("/auth/login", json={"username": USERNAME, "password": PASSWORD}).status_code == 200
    with transaction(h.app.state.engine, write=True) as db:
        identity = db.get(m.Identity, identity_id)
        assert config.local_password_admin(db, identity, settings)
        identity.disabled = True
        with pytest.raises(Problem) as error:
            config.protect_break_glass(db, settings)
        assert error.value.code == "BREAK_GLASS_REQUIRED"
        identity.disabled = False
    assert users.has_credential(USERNAME)


def test_enabling_requires_recovery_admin_with_usable_password(h):
    admin = h.client("admin")
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["admin"]).password_hash = None
    assert admin.put(PATH, json=provider(h, mode="institutional_only")).status_code == 409


def test_auth_migration_is_additive_and_exposes_required_columns(h):
    migration = Path(__file__).resolve().parents[1] / "migrations/versions/0120_auth_configuration.py"
    assert unreviewed(migration) == []
    tables = inspect(h.app.state.engine)
    assert "auth_configuration" in tables.get_table_names()
    columns = {column["name"]: column for column in tables.get_columns("sessions")}
    assert columns["remember_me"]["nullable"] is False


@pytest.mark.parametrize("action", ["identity.disable", "role.revoke"])
@pytest.mark.parametrize("mode", ["both", "institutional_only"])
def test_access_api_preserves_final_managed_password_admin_before_file_change(h, tmp_path, action, mode):
    from tests.test_password_login import provision, client_for
    settings, users, identity_id = provision(h, tmp_path)
    with client_for(h, settings) as admin:
        assert admin.post("/auth/login", json={"username": "admin", "password": h.seed["credentials"]["admin"]}).status_code == 200
        admin.headers["x-csrf-token"] = admin.cookies["rota_csrf"]
        with transaction(h.app.state.engine, write=True) as db:
            grant = m.RoleBinding(identity_id=identity_id, department_id=h.seed["department_id"], role="admin", starts_on=m.now().date())
            db.add(grant)
            db.flush()
            grant_id = grant.id
        assert admin.put(PATH, json=provider(h, mode=mode)).status_code == 200
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Identity, h.seed["identities"]["admin"]).password_hash = None
        before = users.path.read_bytes()
        target = identity_id if action == "identity.disable" else grant_id
        response = admin.post("/api/commands", headers={"idempotency-key": "last-recovery-" + action}, json={
            "kind": action, "target_id": target, "expected_revision": 1,
            "payload": {"reason": "Synthetic recovery guard check"}})
        assert response.status_code == 409 and response.json()["error"]["code"] == "BREAK_GLASS_REQUIRED"
        assert users.path.read_bytes() == before


def test_access_bind_and_pickers_follow_database_provider_and_preserve_old_managed_accounts(h, tmp_path):
    from tests.test_password_login import provision
    _, _, identity_id = provision(h, tmp_path, issuer="https://historical-provider.test")
    admin = h.client("admin")
    assert admin.put(PATH, json=provider(h)).status_code == 200
    from apps.api.services.access import sign_in_identities
    with transaction(h.app.state.engine) as db:
        from apps.api.auth import authenticate
        actor = authenticate(db, admin.cookies["rota_session"], settings=h.settings)
        assert identity_id in set(db.scalars(select(m.Identity.id).where(sign_in_identities(actor, h.settings))))
    reply = h.command("admin", "identity.bind", {"issuer": "https://idp.test", "subject": "new-institutional-subject",
                     "display_name": "Institutional Person (test)", "reason": "Synthetic DB-configured binding"})
    assert reply["record"]["issuer"] == "https://idp.test"
    assert admin.put(PATH, json=provider(h, expected_revision=1, issuer="https://replacement.test",
                                       allowed_hosts=["replacement.test"], institutional_bindings=[])).status_code == 200
    with transaction(h.app.state.engine) as db:
        assert identity_id in set(db.scalars(select(m.Identity.id).where(sign_in_identities(actor, h.settings))))


@pytest.mark.parametrize("change_during_exchange", [False, True])
def test_callback_rejects_configuration_changes_before_and_during_exchange(h, monkeypatch, change_during_exchange):
    from urllib.parse import parse_qs, urlparse
    admin = h.client("admin")
    assert admin.put(PATH, json=provider(h)).status_code == 200
    monkeypatch.setattr(oidc, "document", lambda settings: {"authorization_endpoint": "https://idp.test/authorize"})
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        started = client.get("/auth/start?remember_me=true", follow_redirects=False)
        assert started.status_code == 303
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        if not change_during_exchange:
            assert admin.put(PATH, json=provider(h, expected_revision=1)).status_code == 200
        def exchange(settings, flow, code):
            assert flow["remember_me"] is True
            assert admin.put(PATH, json=provider(h, expected_revision=1)).status_code == 200
            return {"sub": "hospital-staff-subject"}, None
        monkeypatch.setattr(oidc, "exchange", exchange)
        response = client.get("/auth/callback", params={"state": state, "code": "synthetic-code"}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/?signin_error=config"
        assert "rota_session" not in client.cookies


def test_api_institutional_callback_retains_remember_and_existing_identity(h, monkeypatch):
    from urllib.parse import parse_qs, urlparse
    assert h.client("admin").put(PATH, json=provider(h)).status_code == 200
    monkeypatch.setattr(oidc, "document", lambda settings: {"authorization_endpoint": "https://idp.test/authorize"})
    monkeypatch.setattr(oidc, "exchange", lambda settings, flow, code: ({"sub": "hospital-staff-subject"}, None))
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        started = client.get("/auth/start?remember_me=true", follow_redirects=False)
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        response = client.get("/auth/callback", params={"state": state, "code": "synthetic-code"}, follow_redirects=False)
        assert response.status_code == 303
        me = client.get("/api/me")
        assert me.status_code == 200
        with transaction(h.app.state.engine) as db:
            session = db.scalar(select(m.Session).where(m.Session.identity_id == h.seed["identities"]["staff"]))
            assert session.remember_me and session.auth_method == "oidc"


TRUST_UPDATES = [
    {'discovery_url': 'https://attacker.example.test/discovery',
     'allowed_hosts': ['idp.test', 'attacker.example.test']},
    {'allowed_hosts': ['idp.test', 'keys.attacker.example.test']},
    {'client_id': 'different-synthetic-client'},
    {'client_secret': SECRET},
    {'scopes': ['openid']},
    {'username_claim': 'login'},
    {'email_claim': 'alternate_email'},
    {'groups_claim': 'roles'},
    {'allowed_domains': ['institution.example.test']},
    {'mode': 'institutional_only'},
    {'mode': 'password_only', 'institutional_enabled': False},
]


def retained_system_link(h, *, data_changes=None, previous=None):
    object.__setattr__(h.settings, 'fmuc_data_key', '17' * 32)
    links = [{'identity_id': h.seed['identities']['operator'], 'issuer': 'https://idp.test',
              'subject': 'synthetic-existing-developer'}]
    data = s.AuthConfigInput.model_validate(provider(h, institutional_bindings=links,
                                                    **(data_changes or {}))).model_dump(
        include=set(s.AuthSettings.model_fields), mode='json')
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.AuthConfiguration(id=config.SINGLETON, revision=1, data=data, previous=previous))
    return links


def auth_role_grants(h):
    with transaction(h.app.state.engine) as db:
        return [record(row) for row in db.scalars(select(m.RoleBinding).order_by(m.RoleBinding.id))]


@pytest.mark.parametrize('mutation', TRUST_UPDATES)
@pytest.mark.parametrize('actor', ['admin', 'other_department_admin'])
def test_retained_developer_link_rechecks_provider_trust_authority(department_admins, mutation, actor):
    h, admin, other, _ = department_admins
    links = retained_system_link(h)
    client = other if actor == 'other_department_admin' else admin
    before = client.get(PATH).json()
    grants = auth_role_grants(h)
    response = client.put(PATH, json=provider(h, expected_revision=1, institutional_bindings=links, **mutation))
    assert response.status_code == 403
    assert response.json()['error']['code'] == ('OPERATOR_REQUIRED' if actor == 'other_department_admin'
                                               else 'FORBIDDEN')
    assert client.get(PATH).json() == before
    assert auth_role_grants(h) == grants
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AuthConfiguration, config.SINGLETON)
        assert row.revision == 1 and row.secret_ciphertext is None and row.previous is None
        assert not list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action.like('auth.config.%'))))


@pytest.mark.parametrize('database', DATABASES)
@pytest.mark.parametrize('mutation', TRUST_UPDATES)
def test_authorized_provider_trust_edit_keeps_exact_retained_links(request, database, mutation):
    h = request.getfixturevalue(database)
    links = retained_system_link(h)
    client = h.client(developer(h))
    grants = auth_role_grants(h)
    changed = client.put(PATH, json=provider(h, expected_revision=1, institutional_bindings=links, **mutation))
    assert changed.status_code == 200, changed.text
    assert changed.json()['revision'] == 2 and changed.json()['institutional_bindings'] == links
    undone = client.post(PATH + '/undo', json=payload(expected_revision=2))
    assert undone.status_code == 200 and undone.json()['institutional_bindings'] == links
    if 'client_secret' in mutation:
        assert changed.json()['client_secret_set'] is True and undone.json()['client_secret_set'] is False
    assert auth_role_grants(h) == grants


@pytest.mark.parametrize('database', DATABASES)
@pytest.mark.parametrize('mutation', TRUST_UPDATES)
def test_same_department_admin_may_edit_trust_for_ordinary_retained_links(request, database, mutation):
    h = request.getfixturevalue(database)
    object.__setattr__(h.settings, 'fmuc_data_key', '17' * 32)
    client = h.client('admin')
    saved = client.put(PATH, json=provider(h))
    assert saved.status_code == 200
    links = saved.json()['institutional_bindings']
    changed = client.put(PATH, json=provider(h, expected_revision=1, institutional_bindings=links, **mutation))
    assert changed.status_code == 200 and changed.json()['institutional_bindings'] == links


@pytest.mark.parametrize('operation', ['undo', 'reset'])
@pytest.mark.parametrize('actor', ['admin', 'other_department_admin', 'developer'])
def test_undo_reset_rechecks_retained_system_link_trust(department_admins, operation, actor):
    h, admin, other, _ = department_admins
    links = [{'identity_id': h.seed['identities']['operator'], 'issuer': 'https://idp.test',
              'subject': 'synthetic-existing-developer'}]
    prior = s.AuthConfigInput.model_validate(provider(h, institutional_bindings=links)).model_dump(
        include=set(s.AuthSettings.model_fields), mode='json')
    retained_system_link(h, data_changes=TRUST_UPDATES[0],
                         previous={'data': prior, 'secret_ciphertext': None})
    client = h.client(developer(h)) if actor == 'developer' else other if actor == 'other_department_admin' else admin
    before = client.get(PATH).json()
    changed = (client.post(PATH + '/undo', json=payload(expected_revision=1)) if operation == 'undo'
               else client.request('DELETE', PATH, json=payload(expected_revision=1)))
    if actor == 'developer':
        assert changed.status_code == 200 and changed.json()['revision'] == 2
    else:
        assert changed.status_code == 403 and client.get(PATH).json() == before


@pytest.mark.parametrize('omit_issuer', [False, True])
def test_other_department_admin_keeps_remember_only_access_with_protected_links(department_admins, omit_issuer):
    h, _, other, _ = department_admins
    links = retained_system_link(h)
    submitted = [{key: value for key, value in link.items() if not omit_issuer or key != 'issuer'} for link in links]
    grants = auth_role_grants(h)
    changed = other.put(PATH, json=provider(h, expected_revision=1, remember_me_days=45,
                                          institutional_bindings=submitted))
    assert changed.status_code == 200 and changed.json()['institutional_bindings'] == links
    undone = other.post(PATH + '/undo', json=payload(expected_revision=2))
    assert undone.status_code == 200 and undone.json()['remember_me_days'] == 30
    assert undone.json()['institutional_bindings'] == links and auth_role_grants(h) == grants

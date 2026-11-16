"""Person sign-ins: scoped reads, minimal metadata, audited revocation and bounded database reads."""
import json
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, inspect, select
from .clock_support import freeze_clock
from apps.api import auth, models as m
from apps.api.db import transaction
from apps.api.services import person_accounts
from migrations.safety import unreviewed
from packages.contracts import schemas as s
from packages.domain.time import aware


@pytest.fixture
def linked(h):
    with transaction(h.app.state.engine, write=True) as db:
        staff = db.get(m.Identity, h.seed["identities"]["staff"])
        department_id = db.scalar(select(m.Department.id))
        for role in s.ROLE_NAMES:
            if role not in h.seed["identities"]:
                identity = m.Identity(issuer="local-development", subject=role, display_name=f"{role} (test)",
                                      password_hash=staff.password_hash, person_id=h.seed["people"][0])
                db.add(identity)
                db.flush()
                h.seed["identities"][role] = identity.id
                db.add(m.RoleBinding(identity_id=identity.id, department_id=department_id, role=role,
                                     starts_on=date(2020, 1, 1)))
            else:
                db.get(m.Identity, h.seed["identities"][role]).person_id = h.seed["people"][0]
        other = m.Identity(issuer="local-development", subject="other-person", display_name="Other person (test)",
                           person_id=h.seed["people"][1])
        db.add(other)
        db.flush()
        db.add(m.RoleBinding(identity_id=other.id, department_id=department_id, role="staff", starts_on=date(2020, 1, 1)))
        managed = m.Identity(issuer="https://idp.example.test", subject="synthetic-managed-subject",
                             display_name="Managed sign-in (test)", person_id=h.seed["people"][0])
        db.add(managed)
        db.flush()
        h.seed["account_identity"] = managed.id
        db.add(m.RoleBinding(identity_id=managed.id, department_id=department_id, role="staff", starts_on=date(2020, 1, 1)))
        db.add(m.Account(identity_id=managed.id, username="staff.account", email="staff@example.test",
                         accepted_at=m.now() - timedelta(days=2), password_changed_at=m.now() - timedelta(days=1)))
        for identity in (staff, other, managed):
            db.add(m.Session(identity_id=identity.id, department_id=department_id,
                             token_hash=auth.digest(f"synthetic-session-{identity.id}"),
                             csrf_hash=auth.digest("synthetic-csrf"), expires_at=m.now() + timedelta(hours=1)))
    return h


@pytest.mark.parametrize("role", s.ROLE_NAMES)
@pytest.mark.parametrize("area", ("accounts", "sessions"))
def test_person_account_reads_are_admin_or_self_for_every_role(linked, role, area):
    own = linked.seed["people"][0]
    peer = linked.seed["people"][1]
    result = linked.get(role, f"/api/people/{own}/{area}")
    assert result["person_id"] == own and result[area]
    linked.get(role, f"/api/people/{peer}/{area}", status=200 if role == "admin" else 403)
    with transaction(linked.app.state.engine, write=True) as db:
        db.get(m.Identity, linked.seed["identities"][role]).person_id = None
    linked.get(role, f"/api/people/{own}/{area}", status=200 if role == "admin" else 403)


def test_person_accounts_contract_and_session_metadata_never_expose_credentials(linked):
    person = linked.seed["people"][0]
    accounts = linked.get("staff", f"/api/people/{person}/accounts")
    sessions = linked.get("staff", f"/api/people/{person}/sessions")
    s.PersonAccounts.model_validate(accounts)
    s.PersonSessions.model_validate(sessions)
    account = next(a for a in accounts["accounts"] if a["identity_id"] == linked.seed["account_identity"])
    assert account["username"] == "staff.account" and account["roles"] == ["staff"]
    assert account["last_sign_in_at"] is not None
    assert account["accepted_at"] is not None and account["password_changed_at"] is not None
    assert any(row["current"] for row in sessions["sessions"])
    assert any(row["last_active_at"] is None and row["device_label"] is None for row in sessions["sessions"])
    serialised = json.dumps({"accounts": accounts, "sessions": sessions})
    for secret in ("token_hash", "csrf_hash", "password_hash", "subject", "issuer", "synthetic-csrf",
                   linked.client("staff").cookies["rota_session"]):
        assert secret not in serialised
    page = linked.get("staff", f"/api/people/{person}/sessions?limit=1&offset=1")
    assert page["total"] == sessions["total"] and len(page["sessions"]) == 1
    assert page["sessions"][0]["id"] == sessions["sessions"][1]["id"]
    linked.get("staff", f"/api/people/{person}/sessions?limit=0", status=422)
    linked.get("staff", f"/api/people/{person}/sessions?limit=201", status=422)
    linked.get("staff", f"/api/people/{person}/sessions?offset=-1", status=422)


def test_sessions_and_last_sign_in_are_department_scoped(linked):
    person = linked.seed["people"][0]
    original = linked.get("staff", f"/api/people/{person}/accounts")
    with transaction(linked.app.state.engine, write=True) as db:
        org_id = db.scalar(select(m.Organisation.id))
        department = m.Department(organisation_id=org_id, name="Other department (test)")
        foreign_org = m.Organisation(name="Other organisation (test)")
        db.add_all([department, foreign_org])
        db.flush()
        foreign_person = m.Person(organisation_id=foreign_org.id, display_name="Foreign person (test)")
        remote_person = m.Person(organisation_id=org_id, display_name="Other department person (test)")
        db.add_all([foreign_person, remote_person])
        db.flush()
        remote_identity = m.Identity(issuer="test", subject="remote", display_name="Remote sign-in (test)", person_id=remote_person.id)
        db.add(remote_identity)
        db.flush()
        db.add(m.RoleBinding(identity_id=remote_identity.id, department_id=department.id,
                             role="staff", starts_on=date(2020, 1, 1)))
        other_session = m.Session(identity_id=linked.seed["identities"]["staff"], department_id=department.id,
                                  token_hash=auth.digest("synthetic-foreign"), csrf_hash=auth.digest("synthetic-foreign-csrf"),
                                  created_at=m.now() + timedelta(days=1), expires_at=m.now() + timedelta(days=2))
        db.add(other_session)
        db.flush()
        remote_id, foreign_id, other_session_id = remote_person.id, foreign_person.id, other_session.id
    accounts = linked.get("staff", f"/api/people/{person}/accounts")
    assert accounts == original
    sessions = linked.get("staff", f"/api/people/{person}/sessions")
    assert other_session_id not in {row["id"] for row in sessions["sessions"]}
    linked.command("admin", "session.revoke", {"reason": "Wrong department"}, other_session_id, 1, status=404)
    for outside in (remote_id, foreign_id):
        for area in ("accounts", "sessions"):
            linked.get("admin", f"/api/people/{outside}/{area}", status=404)


@pytest.mark.parametrize("operation", ["projection", "revoke"])
def test_device_sessions_use_tightened_inherited_app_limits(linked, monkeypatch, operation):
    object.__setattr__(linked.settings, "session_idle_minutes", 60)
    with transaction(linked.app.state.engine, write=True) as db:
        department = db.get(m.Department, linked.seed["department_id"])
        department.session_idle_minutes = department.session_max_hours = None
    staff = linked.client("staff")
    linked.client("admin")
    moment = m.now()
    with transaction(linked.app.state.engine, write=True) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == auth.digest(staff.cookies["rota_session"])))
        session.created_at = session.last_active_at = moment - timedelta(minutes=10)
        session.expires_at = moment + timedelta(minutes=50)
        session_id, revision = session.id, session.revision
    person = linked.seed["people"][0]
    before = next(row for row in linked.get("admin", f"/api/people/{person}/sessions")["sessions"]
                  if row["id"] == session_id)
    assert before["active"] and before["can_revoke"]
    assert aware(datetime.fromisoformat(before["expires_at"])) == moment + timedelta(minutes=50)
    object.__setattr__(linked.settings, "session_idle_minutes", 5)
    monkeypatch.setenv("ROTA_SESSION_IDLE_MINUTES", "120")  # The app's captured settings must win over later environment reads.
    rejected = staff.get("/api/me")
    assert rejected.status_code == 401 and rejected.json()["error"]["code"] == "SESSION_EXPIRED"
    if operation == "projection":
        result = linked.get("admin", f"/api/people/{person}/sessions")
        s.PersonSessions.model_validate(result)
        row = next(row for row in result["sessions"] if row["id"] == session_id)
        assert not row["active"] and not row["can_revoke"]
        assert aware(datetime.fromisoformat(row["expires_at"])) == moment - timedelta(minutes=5)
    else:
        result = linked.command("admin", "session.revoke", {"reason": "Already expired device"},
                                session_id, revision, status=409)
        assert result["error"]["code"] == "SESSION_INACTIVE"
    with transaction(linked.app.state.engine) as db:
        session = db.get(m.Session, session_id)
        assert not session.revoked and session.revision == revision
        assert aware(session.expires_at) == moment + timedelta(minutes=50)
        assert aware(session.last_active_at) == moment - timedelta(minutes=10)
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(
            m.AuditEvent.target_id == session_id, m.AuditEvent.action == "auth.session_revoke")) == 0
        assert db.scalar(select(func.count()).select_from(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == "session", m.DefinitionHistory.object_id == session_id)) == 0


def test_admin_and_self_can_revoke_one_session_with_audit_and_revision(linked):
    person = linked.seed["people"][0]
    linked.client("staff")
    sessions = linked.get("staff", f"/api/people/{person}/sessions")["sessions"]
    target = next(row for row in sessions if row["identity_id"] == linked.seed["identities"]["staff"] and not row["current"])
    linked.command("staff", "session.revoke", {"reason": "Lost device"}, target["id"], status=428)
    linked.command("staff", "session.revoke", {"reason": "Lost device"}, target["id"], target["revision"] + 1, status=409)
    result = linked.command("staff", "session.revoke", {"reason": "Lost device"}, target["id"], target["revision"])
    assert result["session"]["revoked"] and not result["session"]["active"]
    assert result["session"]["revision"] == target["revision"] + 1
    linked.command("staff", "session.revoke", {"reason": "Already ended"}, target["id"], result["session"]["revision"], status=409)
    assert linked.get("staff", "/api/me")["person_id"] == person
    with transaction(linked.app.state.engine) as db:
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "session.revoke"))
        assert audit.actor_id == linked.seed["identities"]["staff"] and audit.target_id == target["id"]
        assert audit.reason == "Lost device" and not {"token_hash", "csrf_hash"} & audit.detail.keys()
        subject = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'session',
                                                               m.DefinitionHistory.object_id == target['id']))
        assert subject.snapshot == {'id': target['id'], 'person_id': person, 'revoked': True}
    peer = linked.seed["people"][1]
    target = linked.get("admin", f"/api/people/{peer}/sessions")["sessions"][0]
    linked.command("staff", "session.revoke", {"reason": "Not mine"}, target["id"], target["revision"], status=403)
    assert linked.command("admin", "session.revoke", {"reason": "Reported lost"}, target["id"], target["revision"])["session"]["revoked"]


@pytest.mark.parametrize("role", s.ROLE_NAMES)
def test_session_revoke_enforces_self_for_every_non_admin_role(linked, role):
    peer = linked.seed["people"][1]
    target = linked.get("admin", f"/api/people/{peer}/sessions")["sessions"][0]
    result = linked.command(role, "session.revoke", {"reason": "Peer session"}, target["id"], target["revision"],
                            status=200 if role == "admin" else 403)
    if role == "admin":
        assert result["session"]["revoked"]
    own = linked.seed["people"][0]
    target = next(row for row in linked.get(role, f"/api/people/{own}/sessions")["sessions"]
                  if row["identity_id"] == linked.seed["identities"]["staff"] and not row["current"])
    assert linked.command(role, "session.revoke", {"reason": "Own linked device"}, target["id"], target["revision"])["session"]["revoked"]
    if role != "admin":
        with transaction(linked.app.state.engine, write=True) as db:
            db.get(m.Identity, linked.seed["identities"][role]).person_id = None
        linked.command(role, "session.revoke", {"reason": "No longer linked"}, target["id"], target["revision"],
                       status=404 if role == "staff" else 403)


def test_self_revoke_ends_current_session_and_receipt_rechecks_person_access(linked):
    import uuid
    own = linked.seed["people"][0]
    target = next(row for row in linked.get("staff", f"/api/people/{own}/sessions")["sessions"]
                  if row["identity_id"] == linked.seed["identities"]["staff"] and not row["current"])
    key = str(uuid.uuid4())
    linked.command("staff", "session.revoke", {"reason": "Lost device"}, target["id"], target["revision"], key=key)
    with transaction(linked.app.state.engine, write=True) as db:
        db.get(m.Identity, linked.seed["identities"]["staff"]).person_id = linked.seed["people"][1]
        db.get(m.Session, target["id"]).identity_id = linked.seed["identities"]["coordinator"]
    linked.command("staff", "session.revoke", {"reason": "Lost device"}, target["id"], target["revision"], key=key, status=403)
    current = next(row for row in linked.get("staff", f"/api/people/{linked.seed['people'][1]}/sessions")["sessions"] if row["current"])
    linked.command("staff", "session.revoke", {"reason": "This device"}, current["id"], current["revision"])
    linked.get("staff", "/api/me", status=401)


@pytest.mark.parametrize("agent,label", (
    (None, None),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36 Edg/130.0", "Edge / Windows / Desktop"),
    ("Mozilla/5.0 (X11; Linux x86_64) Firefox/131.0", "Firefox / Linux / Desktop"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1 Version/18.0 Mobile/15E Safari/604.1", "Safari / iOS / Phone"),
    ("Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) CriOS/130.0 Mobile/15E Safari/604.1", "Chrome / iOS / Tablet"),
    ("Mozilla/5.0 (Linux; Android 15; private-device-name) Chrome/130.0 Mobile Safari/537.36", "Chrome / Android / Phone"),
    ("identifying-person-and-device-name", "Unknown browser / Unknown OS / Desktop"),
))
def test_user_agent_is_only_fixed_non_identifying_labels(agent, label):
    assert auth.device_label(agent) == label


@pytest.mark.parametrize("peer,label", ((None, None), ("testclient", None), ("127.0.0.1", "Local network"),
                                         ("192.0.2.12", "Private network"), ("8.8.8.8", "Public network")))
def test_network_description_never_contains_an_address(peer, label):
    assert auth.network_label(peer) == label


def test_login_records_only_device_label_and_metadata_migration_is_additive(linked):
    client = TestClient(linked.app, base_url=linked.settings.public_origin)
    client.headers.update({"origin": linked.settings.public_origin,
                           "user-agent": "Mozilla/5.0 (Linux; Android 15; sensitive-model-id) Chrome/130.0 Mobile Safari/537.36"})
    from tests.conftest import TEST_PASSWORD
    response = client.post("/auth/login", json={"username": "staff", "password": TEST_PASSWORD})
    assert response.status_code == 200
    with transaction(linked.app.state.engine) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == auth.digest(client.cookies["rota_session"])))
        assert session.device_label == "Chrome / Android / Phone" and session.last_active_at is not None
        assert session.network_label is None
        audit = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "auth.sign_in",
                                                     m.AuditEvent.target_id == session.id)).one()
        assert audit.detail["person_id"] == linked.seed["people"][0]
        assert audit.detail["device_label"] == session.device_label and audit.detail["network_label"] is None
        assert "sensitive-model-id" not in json.dumps(audit.detail) and "token_hash" not in audit.detail
    migration = Path(__file__).resolve().parents[1] / "migrations/versions/0062_session_devices.py"
    assert unreviewed(migration) == []
    columns = {c["name"]: c for c in inspect(linked.app.state.engine).get_columns("sessions")}
    assert all(columns[name]["nullable"] for name in ("last_active_at", "device_label", "network_label"))
    client.close()


def test_oidc_callback_records_the_same_sanitized_metadata(linked, monkeypatch):
    from urllib.parse import parse_qs, urlparse
    from apps.api import oidc
    from apps.api.main import create_app
    from apps.api.settings import Settings
    from packages.contracts.schemas import AuthSettings
    settings = Settings(environment="test", database_url=linked.settings.database_url, oidc_issuer="https://idp.example.test",
                        oidc_client_id="test-client", oidc_client_secret="synthetic-client-secret",
                        oidc_redirect_uri=linked.settings.public_origin + "/auth/callback", oidc_allowed_hosts="idp.example.test")
    with transaction(linked.app.state.engine, write=True) as db:
        identity = m.Identity(issuer=settings.oidc_issuer, subject="synthetic-provider-subject", display_name="Provider person (test)",
                              person_id=linked.seed["people"][0])
        db.add(identity)
        db.flush()
        db.add(m.RoleBinding(identity_id=identity.id, department_id=db.scalar(select(m.Department.id)),
                             role="staff", starts_on=date(2020, 1, 1)))
        db.add(m.AuthConfiguration(id="environment", data=AuthSettings(
            mode="both", institutional_enabled=True, issuer=settings.oidc_issuer, client_id=settings.oidc_client_id,
            allowed_hosts=["idp.example.test"], institutional_bindings=[
                {"identity_id": identity.id, "issuer": settings.oidc_issuer, "subject": identity.subject}]).model_dump()))
    linked.app.state.engine.dispose()
    linked.app = create_app(settings)
    monkeypatch.setattr(oidc, "document", lambda settings: {"authorization_endpoint": "https://idp.example.test/authorize"})
    monkeypatch.setattr(oidc, "exchange", lambda settings, flow, code: ({"sub": "synthetic-provider-subject"}, "synthetic-id-token"))
    with TestClient(linked.app, base_url=settings.public_origin) as client:
        client.headers["user-agent"] = "Mozilla/5.0 (Windows NT 10.0) Firefox/130.0"
        started = client.get("/auth/start", follow_redirects=False)
        assert started.status_code == 303
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        response = client.get(f"/auth/callback?state={state}&code=synthetic-code", follow_redirects=False)
        assert response.status_code == 303
        with transaction(linked.app.state.engine) as db:
            session = db.scalar(select(m.Session).where(m.Session.token_hash == auth.digest(client.cookies["rota_session"])))
            assert session.device_label == "Firefox / Windows / Desktop" and session.network_label is None
            audit = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "auth.sign_in",
                                                         m.AuditEvent.target_id == session.id)).one()
            assert audit.actor_id == session.identity_id and audit.detail["person_id"] == linked.seed["people"][0]
            assert audit.detail["device_label"] == session.device_label and audit.correlation_id


def test_keep_alive_tracks_activity_at_absolute_cap_and_never_revives_sessions(linked, monkeypatch):
    token = linked.client("staff").cookies["rota_session"]
    with transaction(linked.app.state.engine, write=True) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == auth.digest(token)))
        created = aware(session.created_at)
        department = db.get(m.Department, session.department_id)
        cap = created + timedelta(hours=auth.session_limits(department, linked.settings)[1])
        session.expires_at = cap
        session.last_active_at = cap - timedelta(minutes=17)  # recent actual activity near the absolute cap
        session_id = session.id
    moment = cap - timedelta(minutes=15)
    monkeypatch.setattr(auth, "now", lambda: moment)
    auth.keep_alive(linked.app.state.engine, token, linked.settings)
    with transaction(linked.app.state.engine, write=True) as db:
        session = db.get(m.Session, session_id)
        assert aware(session.last_active_at) == moment and aware(session.expires_at) == cap
        session.revoked = True
    monkeypatch.setattr(auth, "now", lambda: moment + timedelta(minutes=2))
    auth.keep_alive(linked.app.state.engine, token, linked.settings)
    with transaction(linked.app.state.engine, write=True) as db:
        session = db.get(m.Session, session_id)
        assert session.revoked and aware(session.last_active_at) == moment
        session.revoked = False
        session.expires_at = moment
    auth.keep_alive(linked.app.state.engine, token, linked.settings)
    with transaction(linked.app.state.engine) as db:
        session = db.get(m.Session, session_id)
        assert aware(session.expires_at) == moment and aware(session.last_active_at) == moment


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_unlimited_devices_remain_revocable_disabled_and_audited(request, database):
    h = request.getfixturevalue(database)
    person = h.seed["people"][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["staff"]).person_id = person
    department = h.get("admin", "/api/me")["department"]
    h.command("admin", "department.sessions", {"idle_minutes": 480, "max_hours": 24,
                                                "idle_unlimited": True, "max_unlimited": True},
              department["id"], department["revision"])
    staff = h.client("staff")
    token = staff.cookies["rota_session"]
    with transaction(h.app.state.engine, write=True) as db:
        current = db.scalar(select(m.Session).where(m.Session.token_hash == auth.digest(token)))
        current.expires_at = m.now() - timedelta(days=100)  # inapplicable finite cache
        peers = [m.Session(identity_id=current.identity_id, department_id=current.department_id,
                           token_hash=auth.digest(f"unlimited-device-{index}"), csrf_hash=auth.digest("device-csrf"),
                           expires_at=m.now() - timedelta(days=100), expiry_unlimited=True) for index in range(2)]
        db.add_all(peers)
        db.flush()
        peer_ids, current_id, identity_id = [row.id for row in peers], current.id, current.identity_id
    result = h.get("staff", f"/api/people/{person}/sessions")
    s.PersonSessions.model_validate(result)
    rows = {row["id"]: row for row in result["sessions"]}
    assert all(rows[key]["active"] and rows[key]["can_revoke"] and rows[key]["expires_at"] is None
               for key in [current_id, *peer_ids])
    response = staff.post("/api/commands", headers={"idempotency-key": str(uuid.uuid4()), "x-csrf-token": "incorrect"},
                          json={"kind": "session.revoke", "target_id": peer_ids[0], "expected_revision": rows[peer_ids[0]]["revision"],
                                "payload": {"reason": "Lost device"}})
    assert response.status_code == 403 and response.json()["error"]["code"] == "CSRF_FAILED"
    before_revoke = m.now()
    h.command("staff", "session.revoke", {"reason": "Lost device"}, peer_ids[0], rows[peer_ids[0]]["revision"])
    assert h.command("staff", "account.end_other_sessions", {"reason": "Sign out other devices"})["ended"] == 1
    after_revoke = m.now()
    assert staff.get("/api/pulse").status_code == 200
    with transaction(h.app.state.engine, write=True) as db:
        assert not db.get(m.Session, current_id).revoked
        for key in peer_ids:
            session = db.get(m.Session, key)
            assert session.revoked and not session.expiry_unlimited
            assert before_revoke <= aware(session.expires_at) <= after_revoke
            event = db.scalars(select(m.AuditEvent).where(m.AuditEvent.target_id == key,
                                                         m.AuditEvent.action == "auth.session_revoke")).one()
            assert event.actor_id == identity_id and event.detail["session_id"] == key
        db.get(m.Identity, h.seed["identities"]["staff"]).disabled = True
    assert staff.get("/api/pulse").status_code == 401
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Session, current_id).expiry_unlimited


def test_person_account_query_budget_at_department_scale(linked):
    person = linked.seed["people"][0]
    linked.client("admin")
    with transaction(linked.app.state.engine, write=True) as db:
        department = db.scalar(select(m.Department))
        for index in range(150):
            member = m.Person(organisation_id=department.organisation_id, display_name=f"Member {index} (test)")
            db.add(member)
            db.flush()
            identity = m.Identity(issuer="budget", subject=str(index), display_name=member.display_name, person_id=member.id)
            db.add(identity)
            db.flush()
            db.add(m.RoleBinding(identity_id=identity.id, department_id=department.id, role="staff", starts_on=date(2020, 1, 1)))
            db.add(m.Session(identity_id=identity.id, department_id=department.id, token_hash=auth.digest(f"budget-{index}"),
                             csrf_hash=auth.digest("budget-csrf"), expires_at=m.now() + timedelta(hours=1)))
    statements = []

    def counted(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(linked.app.state.engine, "before_cursor_execute", counted)
    try:
        with transaction(linked.app.state.engine) as db:
            actor = auth.authenticate(db, linked.client("admin").cookies["rota_session"])
            statements.clear()
            accounts = person_accounts.accounts(db, actor, person)
            assert accounts["accounts"] and len(statements) <= 8
            statements.clear()
            sessions = person_accounts.sessions(db, actor, person, settings=linked.settings)
            assert sessions["sessions"] and len(statements) <= 5
    finally:
        event.remove(linked.app.state.engine, "before_cursor_execute", counted)


@pytest.mark.parametrize("role", s.ROLE_NAMES)
def test_end_other_sessions_keeps_current_and_other_identities_for_every_role(linked, role):
    token = linked.client(role).cookies["rota_session"]
    identity_id = linked.seed["identities"][role]
    with transaction(linked.app.state.engine, write=True) as db:
        current = db.scalar(select(m.Session).where(m.Session.token_hash == auth.digest(token)))
        identity = db.get(m.Identity, identity_id)
        other = auth.issue_session(db, identity, current.department_id, linked.settings,
                                   user_agent="Mozilla/5.0 (Windows NT 10.0) Firefox/130.0", peer="192.0.2.13")
        other_id = db.scalar(select(m.Session.id).where(m.Session.token_hash == auth.digest(other["token"])))
        current_id = current.id
        peer_session = m.Session(identity_id=linked.seed["account_identity"], department_id=current.department_id,
                                 token_hash=auth.digest("peer-account-session"), csrf_hash=auth.digest("synthetic-peer-csrf"),
                                 expires_at=m.now() + timedelta(hours=1))
        ended_session = m.Session(identity_id=identity_id, department_id=current.department_id,
                                  token_hash=auth.digest("already-ended-session"), csrf_hash=auth.digest("ended-csrf"),
                                  expires_at=m.now() + timedelta(hours=1), revoked=True)
        expired_session = m.Session(identity_id=identity_id, department_id=current.department_id,
                                    token_hash=auth.digest("expired-other-session"), csrf_hash=auth.digest("expired-csrf"),
                                    expires_at=m.now() - timedelta(hours=1))
        db.add_all([peer_session, ended_session, expired_session])
        db.flush()
        untouched_ids = (peer_session.id, ended_session.id, expired_session.id)
        active_others = db.scalar(select(func.count()).select_from(m.Session).where(
            m.Session.identity_id == identity_id, m.Session.id != current_id,
            m.Session.revoked.is_(False), m.Session.expires_at > m.now()))
    linked.command(role, "account.end_other_sessions", {"reason": "Wrong account"},
                   linked.seed["account_identity"], status=403)
    linked.command(role, "account.end_other_sessions", {}, identity_id, status=422)
    key = str(uuid.uuid4())
    result = linked.command(role, "account.end_other_sessions", {"reason": "End my other devices"}, identity_id, key=key)
    assert result["identity_id"] == identity_id and result["ended"] == active_others
    assert linked.get(role, "/api/me")["identity_id"] == identity_id
    with transaction(linked.app.state.engine, write=True) as db:
        assert not db.get(m.Session, current_id).revoked and db.get(m.Session, current_id).revision == 1
        assert db.get(m.Session, other_id).revoked and db.get(m.Session, other_id).revision == 2
        assert all(db.get(m.Session, session_id).revision == 1 for session_id in untouched_ids)
        assert not db.get(m.Session, untouched_ids[0]).revoked and not db.get(m.Session, untouched_ids[2]).revoked
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "auth.session_revoke",
                                                           m.AuditEvent.detail["identity_id"].as_string() == identity_id)))
        assert len(events) == active_others and all(row.actor_id == identity_id for row in events)
        revoked = next(row for row in events if row.target_id == other_id)
        assert revoked.detail["device_label"] == "Firefox / Windows / Desktop"
        assert revoked.detail["network_label"] == "Private network"
        assert revoked.reason == "Session revoked"
        fresh = auth.issue_session(db, db.get(m.Identity, identity_id), db.get(m.Session, current_id).department_id,
                                   linked.settings)
        fresh_id = db.scalar(select(m.Session.id).where(m.Session.token_hash == auth.digest(fresh["token"])))
    replay = linked.command(role, "account.end_other_sessions", {"reason": "End my other devices"}, identity_id, key=key)
    assert replay == result
    assert linked.get(role, f"/api/commands/{key}")["result"] == result
    with transaction(linked.app.state.engine) as db:
        assert not db.get(m.Session, fresh_id).revoked
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(
            m.AuditEvent.action == "auth.session_revoke", m.AuditEvent.detail["identity_id"].as_string() == identity_id)) == active_others


def test_end_other_sessions_does_not_need_a_linked_person(linked):
    with transaction(linked.app.state.engine, write=True) as db:
        identity = db.get(m.Identity, linked.seed["identities"]["staff"])
        identity.person_id = None
    result = linked.command("staff", "account.end_other_sessions", {"reason": "End own devices"})
    assert result["ended"] == 1 and linked.get("staff", "/api/me")["person_id"] is None


def test_end_own_other_department_session_audits_only_in_that_department(linked):
    linked.client("staff")
    person_id, identity_id = linked.seed["people"][0], linked.seed["identities"]["staff"]
    with transaction(linked.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=linked.seed["organisation_id"], name="Other department (test)")
        db.add(department)
        db.flush()
        session = m.Session(identity_id=identity_id, department_id=department.id,
                            token_hash=auth.digest("other-department-device"), csrf_hash=auth.digest("other-department-csrf"),
                            expires_at=m.now() + timedelta(hours=1))
        db.add(session)
        db.flush()
        session_id, department_id = session.id, department.id
        auth.session_event(db, session, person_id, "auth.sign_in")
    assert linked.command("staff", "account.end_other_sessions", {"reason": "End own other devices"})["ended"] == 2
    with transaction(linked.app.state.engine) as db:
        revoked = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "auth.session_revoke",
                                                       m.AuditEvent.target_id == session_id)).one()
        assert revoked.department_id == department_id and db.get(m.Session, session_id).revoked
    history = linked.get("staff", f"/api/people/{person_id}/auth-activity")
    assert session_id not in {row["target_id"] for row in history["events"]}


def test_end_other_sessions_uses_existing_account_rate_limit_and_csrf(linked, monkeypatch):
    freeze_clock(monkeypatch)
    client = linked.client("staff")
    client.headers.pop("x-csrf-token")
    response = client.post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())},
                           json={"kind": "account.end_other_sessions", "payload": {"reason": "End other devices"}})
    assert response.status_code == 403 and response.json()["error"]["code"] == "CSRF_FAILED"
    client.headers["x-csrf-token"] = client.cookies["rota_csrf"]
    with transaction(linked.app.state.engine, write=True) as db:
        db.execute(delete(m.RateLimit))
    key = str(uuid.uuid4())
    for _ in range(60):
        linked.command("staff", "account.end_other_sessions", {"reason": "End other devices"}, key=key)
    blocked = client.post("/api/commands", headers={"idempotency-key": key},
                          json={"kind": "account.end_other_sessions", "payload": {"reason": "End other devices"}})
    assert blocked.status_code == 429 and 1 <= int(blocked.headers["retry-after"]) <= 3600
    assert blocked.json()["error"]["message"] == "Too many account changes in a short time; try again later"
    linked.command("coordinator", "account.end_other_sessions", {"reason": "Own account still allowed"})


@pytest.mark.parametrize("role", s.ROLE_NAMES)
def test_auth_history_is_self_or_admin_for_every_role(linked, role):
    own, peer = linked.seed["people"][:2]
    data = linked.get(role, f"/api/people/{own}/auth-activity")
    s.PersonAuthActivity.model_validate(data)
    assert data["events"] and "people" not in data and data["person_id"] == own
    assert all(row["action"].startswith("auth.") and row["detail"]["person_id"] == own for row in data["events"])
    assert all("actor_display_name" not in row for row in data["events"])
    linked.get(role, f"/api/people/{peer}/auth-activity", status=200 if role == "admin" else 403)
    linked.get(role, "/api/audit", status=200 if role in {"operator", "auditor"} else 403)
    with transaction(linked.app.state.engine, write=True) as db:
        db.get(m.Identity, linked.seed["identities"][role]).person_id = None
    linked.get(role, f"/api/people/{own}/auth-activity", status=200 if role == "admin" else 403)


def test_auth_activity_contract_pagination_bounds_and_authentication(linked):
    person_id = linked.seed["people"][0]
    path = f"/api/people/{person_id}/auth-activity"
    with TestClient(linked.app, base_url=linked.settings.public_origin) as anonymous:
        assert anonymous.get(path).status_code == 401
    page = linked.get("staff", path)
    assert set(page) == {"person_id", "events", "offset", "limit", "total", "has_more"}
    assert page["limit"] == 100 and page["offset"] == 0 and page["total"] == 1 and not page["has_more"]
    schema = linked.app.openapi()["paths"]["/api/people/{person_id}/auth-activity"]["get"]
    assert schema["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/PersonAuthActivity")
    for query in ("limit=0", "limit=101", "offset=-1", "offset=1000001", "offset=invalid"):
        linked.get("staff", path + "?" + query, status=422)
    tail = linked.get("staff", path + "?limit=1&offset=1000000")
    assert tail["events"] == [] and tail["total"] == 1 and not tail["has_more"]


@pytest.mark.parametrize("role", s.ROLE_NAMES)
def test_auth_history_records_logout_and_revoke_without_credentials_or_private_details(linked, role):
    client = linked.client(role)
    identity_id, person_id = linked.seed["identities"][role], linked.seed["people"][0]
    token, csrf = client.cookies["rota_session"], client.cookies["rota_csrf"]
    with transaction(linked.app.state.engine, write=True) as db:
        current = db.scalar(select(m.Session).where(m.Session.token_hash == auth.digest(token)))
        current_id = current.id
        other = auth.issue_session(db, db.get(m.Identity, identity_id), current.department_id, linked.settings)
        other_id = db.scalar(select(m.Session.id).where(m.Session.token_hash == auth.digest(other["token"])))
    linked.command(role, "session.revoke", {"reason": "Sensitive synthetic reason"}, other_id, 1)
    response = client.post("/auth/logout")
    assert response.status_code == 200 and response.json() == {"signed_out": True}
    assert client.get("/api/me").status_code == 401
    with transaction(linked.app.state.engine, write=True) as db:
        assert db.get(m.Session, current_id).revision == 2
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action.in_(auth.SESSION_EVENT_REASONS))))
        assert {"auth.sign_in", "auth.sign_out", "auth.session_revoke"} <= {row.action for row in events}
        assert all(set(row.detail) == {"person_id", "identity_id", "session_id", "device_label", "network_label"}
                   | ({"method"} if row.action == "auth.sign_in" else set())
                   for row in events)
        logout = next(row for row in events if row.action == "auth.sign_out")
        assert logout.target_id == current_id and logout.actor_id == identity_id and logout.correlation_id
        revoked = next(row for row in events if row.action == "auth.session_revoke")
        assert revoked.reason == "Session revoked"
        db.add(m.AuditEvent(department_id=revoked.department_id, actor_id=identity_id,
                            action=revoked.action, target_id=revoked.target_id, reason="Sensitive synthetic reason",
                            detail={**revoked.detail, "private_note": token}, correlation_id="synthetic-redaction-check"))
        db.get(m.Identity, identity_id).person_id = None
    if role == "admin":
        linked.clients.pop(role).close()
    history = linked.get("admin", f"/api/people/{person_id}/auth-activity")
    assert {current_id, other_id} <= {row["target_id"] for row in history["events"]}
    serialised = json.dumps(history)
    for secret in (token, csrf, other["token"], "token_hash", "csrf_hash", "Sensitive synthetic reason"):
        assert secret not in serialised


def test_auth_history_retains_events_after_session_pruning_and_pages_beyond_200(linked, monkeypatch):
    linked.client("staff")
    identity_id, person_id = linked.seed["identities"]["staff"], linked.seed["people"][0]
    with transaction(linked.app.state.engine, write=True) as db:
        department_id = db.scalar(select(m.Department.id))
        for index in range(205):
            session = m.Session(identity_id=identity_id, department_id=department_id,
                                token_hash=auth.digest(f"pruned-session-{index}"), csrf_hash=auth.digest("pruned-csrf"),
                                expires_at=m.now() - timedelta(days=8), created_at=m.now() - timedelta(days=9, minutes=index),
                                device_label="Firefox / Windows / Desktop", network_label="Private network")
            db.add(session)
            db.flush()
            auth.session_event(db, session, person_id, "auth.sign_in")
        db.add(m.AuditEvent(department_id=department_id, actor_id=identity_id, action="account.invite",
                            target_id=identity_id, reason="Unrelated private detail",
                            detail={"person_id": person_id, "private_note": "hidden"}, correlation_id="synthetic"))
    from apps.worker import main as worker
    monkeypatch.setattr(worker, "_pruned", {})
    assert worker.prune_security_state(linked.app.state.engine) >= 205
    with transaction(linked.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Session).where(
            m.Session.identity_id == identity_id, m.Session.expires_at < m.now())) == 0
    history = linked.get("staff", f"/api/people/{person_id}/auth-activity?offset=200&limit=100")
    assert history["total"] == 206 and len(history["events"]) == 6 and not history["has_more"]
    assert all(row["action"] == "auth.sign_in" for row in history["events"])
    assert sum(row["detail"]["device_label"] == "Firefox / Windows / Desktop" for row in history["events"]) >= 5
    assert "Unrelated private detail" not in json.dumps(history) and "people" not in history
    first = linked.get("staff", f"/api/people/{person_id}/auth-activity?offset=0&limit=100")
    assert first["has_more"] and first["total"] == history["total"]
    assert not {row["id"] for row in history["events"]} & {row["id"] for row in first["events"]}


@pytest.mark.parametrize("role", s.ROLE_NAMES)
def test_auth_history_returns_empty_for_new_department_person_and_rejects_outsiders(linked, role):
    person_id = linked.configure("people", {"display_name": "New person without rank or account (test)"})["record"]["id"]
    result = linked.get(role, f"/api/people/{person_id}/auth-activity", status=200 if role == "admin" else 403)
    if role == "admin":
        assert result["events"] == [] and "people" not in result and result["total"] == 0 and not result["has_more"]
    with transaction(linked.app.state.engine, write=True) as db:
        other_department = m.Department(organisation_id=linked.seed["organisation_id"], name="Other department (test)")
        outside = m.Person(organisation_id=linked.seed["organisation_id"], display_name="Outside person (test)")
        db.add_all([other_department, outside])
        db.flush()
        db.add(m.DefinitionHistory(department_id=other_department.id, kind="people", object_id=outside.id,
                                   object_revision=outside.revision, actor_id=linked.seed["identities"]["admin"],
                                   snapshot={"id": outside.id}, reason="Other department person (test)"))
        outside_id = outside.id
    linked.get(role, f"/api/people/{outside_id}/auth-activity", status=404 if role == "admin" else 403)


@pytest.mark.postgres
def test_bulk_session_revoke_and_durable_auth_history_on_postgres(pg):
    client = pg.client('staff')
    person_id, identity_id = pg.seed['people'][0], pg.seed['identities']['staff']
    with transaction(pg.app.state.engine, write=True) as db:
        current = db.scalar(select(m.Session).where(m.Session.token_hash == auth.digest(client.cookies['rota_session'])))
        current_id = current.id
        other = auth.issue_session(db, db.get(m.Identity, identity_id), current.department_id, pg.settings,
                                   user_agent='Firefox/130.0 (Windows)', peer='192.0.2.13')
        other_id = db.scalar(select(m.Session.id).where(m.Session.token_hash == auth.digest(other['token'])))
    key = str(uuid.uuid4())
    result = pg.command('staff', 'account.end_other_sessions', {'reason': 'End my other devices'}, key=key)
    assert result['ended'] == 1 and pg.get('staff', '/api/me')['identity_id'] == identity_id
    assert pg.command('staff', 'account.end_other_sessions', {'reason': 'End my other devices'}, key=key) == result
    with transaction(pg.app.state.engine) as db:
        assert not db.get(m.Session, current_id).revoked
        assert db.get(m.Session, other_id).revoked and db.get(m.Session, other_id).revision == 2
    history = pg.get('staff', f'/api/people/{person_id}/auth-activity?limit=2')
    s.PersonAuthActivity.model_validate(history)
    assert history['total'] == 3 and len(history['events']) == 2 and history['has_more']
    tail = pg.get('staff', f'/api/people/{person_id}/auth-activity?offset=2&limit=2')
    assert len(tail['events']) == 1 and not tail['has_more']
    revoked = next(row for row in history['events'] + tail['events'] if row['action'] == 'auth.session_revoke')
    assert revoked['target_id'] == other_id and revoked['detail']['device_label'] == 'Firefox / Windows / Desktop'
    assert revoked['detail']['network_label'] == 'Private network'
    assert client.post('/auth/logout').status_code == 200
    history = pg.get('admin', f'/api/people/{person_id}/auth-activity')
    s.PersonAuthActivity.model_validate(history)
    assert history['total'] == 4 and any(row['action'] == 'auth.sign_out' for row in history['events'])

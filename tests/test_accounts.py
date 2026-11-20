"""Managed accounts: invitation, acceptance, one-time links, password change and reset, sessions, session limits."""
import json
import uuid
from datetime import date, datetime, timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from .clock_support import freeze_clock
from .conftest import TEST_PASSWORD
from apps.api import models as m
from apps.api.auth import keep_alive, digest, login_keys, SESSION_COOKIE_MAX_AGE_SECONDS
from apps.api.db import transaction
from apps.api.main import create_app
from apps.api.settings import Settings
from packages.contracts import schemas as s
from packages.domain.time import aware

PASSWORD = "correct horse battery 12"


@pytest.fixture
def acc(h, tmp_path):
    """The standard harness with a sign-in users file the API manages."""
    return with_users_file(h, tmp_path)


@pytest.fixture
def maintenance_connections(monkeypatch):
    """Observe real connections owned by the standalone command runner, and clean up on test failure."""
    from apps.api import db
    make_engine = db.make_engine
    engines, opened, closed = [], [], []

    def tracked_engine(*args, **kwargs):
        engine = make_engine(*args, **kwargs)
        engines.append(engine)
        event.listen(engine, "connect", lambda connection, _: opened.append(connection))
        event.listen(engine, "close", lambda connection, _: closed.append(connection))
        return engine

    monkeypatch.setattr(db, "make_engine", tracked_engine)
    try:
        yield opened, closed
    finally:
        for engine in engines:
            engine.dispose()


def with_users_file(h, tmp_path):
    users = tmp_path / "users.yml"
    users.write_text(json.dumps({"users": {}}), encoding="utf-8")
    h.app.state.engine.dispose()
    h.settings = Settings(environment="test", database_url=h.settings.database_url, identity_users_file=str(users))
    h.app = create_app(h.settings)
    h.users = users
    return h


def anonymous(h):
    client = TestClient(h.app, base_url=h.settings.public_origin)
    client.headers["origin"] = h.settings.public_origin
    return client


def invite(h, username="new.person", roles=("staff",), status=200):
    return h.command("admin", "account.invite", {"display_name": "New Person (test)", "email": f"{username}@example.test",
                                                 "username": username, "roles": list(roles), "reason": "Synthetic account invitation"}, status=status)


def token_of(result):
    return result["one_time_link"].rsplit("/", 1)[1]


def sign_in(h, username, password):
    client = anonymous(h)
    response = client.post("/auth/login", json={"username": username, "password": password})
    if response.status_code == 200:
        client.headers["x-csrf-token"] = client.cookies["rota_csrf"]
    return client, response.status_code


def users_file(h):
    return json.loads(h.users.read_text(encoding="utf-8"))["users"]


def test_invite_accept_and_sign_in(acc, monkeypatch):
    result = invite(acc)
    link = result["one_time_link"]
    assert link.startswith(acc.settings.public_origin + "/invite/") and len(token_of(result)) >= 43
    token = token_of(result)
    # The token exists only in the response: never in the receipt, the audit log or the link record.
    with transaction(acc.app.state.engine) as db:
        stored = json.dumps([r.result for r in db.scalars(select(m.CommandReceipt))]) + \
            json.dumps([e.detail for e in db.scalars(select(m.AuditEvent))])
        assert token not in stored
        assert db.scalar(select(m.AccountLink)).token_hash == digest(token)
    assert users_file(acc)["new.person"]["disabled"] is True
    access = acc.get("admin", "/api/access")
    identity = result["record"]["id"]
    assert access["accounts"][identity]["invite_open"] and access["accounts"][identity]["accepted_at"] is None
    assert access["accounts_enabled"] is True
    assert {b["role"] for b in access["bindings"] if b["identity_id"] == identity} == {"staff"}
    invite_audit = next(e for e in acc.get("auditor", "/api/audit")["events"] if e["action"] == "account.invite")
    assert invite_audit["detail"]["roles"] == ["staff"]
    assert invite_audit["reason"] == "Synthetic account invitation"
    assert "Staff" in invite_audit["summary"]
    moment = freeze_clock(monkeypatch)
    monkeypatch.setattr('apps.api.auth.now', lambda: moment)
    assert sign_in(acc, "new.person", PASSWORD)[1] == 401  # not accepted yet

    public = anonymous(acc)
    info = public.get(f"/api/account-links/{token}").json()
    assert info == {"purpose": "invite", "display_name": "New Person (test)", "username": "new.person", "expires_at": info["expires_at"]}
    assert public.post(f"/api/account-links/{token}", json={"password": "short"}).status_code == 422
    weak = public.post(f"/api/account-links/{token}", json={"password": "new.person@example.test"})
    assert weak.status_code == 422 and weak.json()["error"]["code"] == "PASSWORD_WEAK"
    no_origin = TestClient(acc.app, base_url=acc.settings.public_origin)
    assert no_origin.post(f"/api/account-links/{token}", json={"password": PASSWORD}).status_code == 403
    accepted = public.post(f"/api/account-links/{token}", json={"password": PASSWORD})
    assert accepted.status_code == 200, accepted.text
    assert users_file(acc)["new.person"]["disabled"] is False
    assert public.post(f"/api/account-links/{token}", json={"password": PASSWORD + "x"}).status_code == 404  # single use
    assert public.get(f"/api/account-links/{token}").status_code == 404

    with transaction(acc.app.state.engine) as db:
        deadline = aware(db.get(m.RateLimit, digest("login-delay:" + login_keys("new.person", "testclient", acc.settings)[2])).expires_at)
    freeze_clock(monkeypatch, deadline)
    monkeypatch.setattr('apps.api.auth.now', lambda: deadline)
    client, status = sign_in(acc, "new.person", PASSWORD)
    assert status == 200
    me = client.get("/api/me").json()
    assert me["account"] == {"username": "new.person"} and me["roles"] == ["staff"]
    with transaction(acc.app.state.engine) as db:
        actions = [e.action for e in db.scalars(select(m.AuditEvent).where(m.AuditEvent.target_id == identity))]
        assert "account.invite_accepted" in actions
        assert db.get(m.Identity, identity).last_sign_in_at is not None


def test_links_expire_and_resend_or_revoke_replace_them(acc):
    first = invite(acc)
    identity = first["record"]["id"]
    second = acc.command("admin", "account.resend", {"reason": "Lost the message"}, identity)
    public = anonymous(acc)
    assert public.get(f"/api/account-links/{token_of(first)}").status_code == 404
    assert public.get(f"/api/account-links/{token_of(second)}").status_code == 200
    acc.command("admin", "account.revoke_invite", {"reason": "Sent to the wrong person"}, identity)
    assert public.get(f"/api/account-links/{token_of(second)}").status_code == 404
    acc.command("admin", "account.revoke_invite", {"reason": "Again"}, identity, status=409)
    third = acc.command("admin", "account.resend", {"reason": "Right person now"}, identity)
    with transaction(acc.app.state.engine, write=True) as db:
        db.execute(update(m.AccountLink).where(m.AccountLink.token_hash == digest(token_of(third)))
                   .values(expires_at=m.now() - timedelta(minutes=1)))
    assert public.post(f"/api/account-links/{token_of(third)}", json={"password": PASSWORD}).status_code == 404
    assert acc.get("admin", "/api/access")["accounts"][identity]["invite_open"] is False
    assert public.get("/api/account-links/not-a-real-token-but-long-enough").status_code == 404


def accepted(acc, username="new.person"):
    result = invite(acc, username)
    assert anonymous(acc).post(f"/api/account-links/{token_of(result)}", json={"password": PASSWORD}).status_code == 200
    return result["record"]["id"]


def test_reset_link_sets_a_new_password_and_ends_sessions(acc, monkeypatch):
    identity = accepted(acc)
    client, _ = sign_in(acc, "new.person", PASSWORD)
    assert client.get("/api/me").status_code == 200
    reset = acc.command("admin", "account.reset_link", {"reason": "Forgot password"}, identity)
    assert "/reset/" in reset["one_time_link"]
    public = anonymous(acc)
    assert public.get(f"/api/account-links/{token_of(reset)}").json()["purpose"] == "reset"
    assert public.post(f"/api/account-links/{token_of(reset)}", json={"password": "another passphrase 34"}).status_code == 200
    assert client.get("/api/me").status_code == 401  # the old session ended
    with transaction(acc.app.state.engine) as db:
        revoked = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "auth.session_revoke",
                                                       m.AuditEvent.detail["identity_id"].as_string() == identity)).one()
        assert revoked.actor_id == identity and revoked.reason == "Session revoked"
    assert sign_in(acc, "new.person", PASSWORD)[1] == 401
    with transaction(acc.app.state.engine) as db:
        deadline = aware(db.get(m.RateLimit, digest("login-delay:" + login_keys("new.person", "testclient", acc.settings)[2])).expires_at)
    freeze_clock(monkeypatch, deadline)
    monkeypatch.setattr('apps.api.auth.now', lambda: deadline)
    assert sign_in(acc, "new.person", "another passphrase 34")[1] == 200
    # An invitation not accepted yet cannot get a reset link; an accepted one cannot be re-sent.
    pending = invite(acc, "pending.person")["record"]["id"]
    acc.command("admin", "account.reset_link", {"reason": "Try"}, pending, status=409)
    acc.command("admin", "account.resend", {"reason": "Try"}, identity, status=409)


def test_change_own_password(acc, monkeypatch):
    freeze_clock(monkeypatch)
    identity = accepted(acc)
    client, _ = sign_in(acc, "new.person", PASSWORD)
    other, _ = sign_in(acc, "new.person", PASSWORD)
    wrong = client.post("/api/account/password", json={"current_password": "not the password", "new_password": "fresh passphrase 56"})
    assert wrong.status_code == 403 and wrong.json()["error"]["code"] == "PASSWORD_INCORRECT"
    same = client.post("/api/account/password", json={"current_password": PASSWORD, "new_password": PASSWORD})
    assert same.status_code == 422
    changed = client.post("/api/account/password", json={"current_password": PASSWORD, "new_password": "fresh passphrase 56"})
    assert changed.status_code == 200 and changed.json()["sessions_ended"] == 1
    assert client.get("/api/me").status_code == 200  # this session stays
    assert other.get("/api/me").status_code == 401  # the other one ended
    with transaction(acc.app.state.engine) as db:
        revoked = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "auth.session_revoke",
                                                       m.AuditEvent.detail["identity_id"].as_string() == identity)).one()
        password_change = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "account.password_change")).one()
        assert revoked.actor_id == identity and revoked.correlation_id == password_change.correlation_id
    assert sign_in(acc, "new.person", "fresh passphrase 56")[1] == 200
    # A sign-in the app does not manage is told where to change it; attempts are rate limited.
    admin = acc.client("admin")
    assert admin.post("/api/account/password", json={"current_password": "x", "new_password": "fresh passphrase 56"}).json()["error"]["code"] == "ACCOUNT_NOT_MANAGED"
    for _ in range(4):
        client.post("/api/account/password", json={"current_password": "wrong", "new_password": "fresh passphrase 78"})
    assert client.post("/api/account/password", json={"current_password": "wrong", "new_password": "fresh passphrase 78"}).status_code == 429


def test_permissions_duplicates_and_unconfigured_environment(acc, h):
    invite(acc)
    invite(acc, status=409)  # username taken
    acc.command("coordinator", "account.invite", {"display_name": "X", "email": "x@example.test", "username": "xx", "reason": "Unauthorised invite"}, status=403)
    acc.command("admin", "account.invite", {"display_name": "X", "email": "not-an-email", "username": "xx", "reason": "Invalid invite input"}, status=422)
    acc.command("admin", "account.invite", {"display_name": "X", "email": "x@example.test", "username": "Not Valid", "reason": "Invalid invite input"}, status=422)


def test_account_invite_requires_an_audit_reason(h):
    refused = h.command("admin", "account.invite", {"display_name": "Missing reason (test)",
        "email": "missing.reason@example.test", "username": "missing.reason", "roles": ["staff"]}, status=422)
    assert refused["error"]["code"] == "SCHEMA_INVALID"


def test_server_invite_admin_prints_a_one_time_link(acc, monkeypatch, capsys, maintenance_connections):
    """infra/production/ops.sh invite-admin runs scripts/local-deploy/bootstrap_admin.py --on-server in an API container."""
    import importlib.util
    from pathlib import Path
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("bootstrap_admin", scripts / "local-deploy/bootstrap_admin.py")
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    for key, value in {"ROTA_ENV": "test", "DATABASE_URL": acc.settings.database_url, "PUBLIC_ORIGIN": acc.settings.public_origin,
                       "ROTA_IDENTITY_USERS_FILE": str(acc.users)}.items():
        monkeypatch.setenv(key, value)
    args = ["bootstrap_admin.py", "--on-server", "--username", "first.admin", "--display-name", "First Admin (test)",
            "--email", "first.admin@example.test"]
    monkeypatch.setattr("sys.argv", list(args))
    with pytest.raises(SystemExit, match="already exists"):  # the harness has an admin
        bootstrap.main()
    opened, closed = maintenance_connections
    assert opened and closed == opened, "A refused bootstrap must close its owned database connections"
    monkeypatch.setattr("sys.argv", args + ["--force-reason", "Second admin for the go-live (test)"])
    bootstrap.main()
    assert closed == opened, "A successful bootstrap must close its owned database connections"
    out = capsys.readouterr().out
    assert "Invitation created for 'first.admin' (admin) in production" in out
    assert acc.settings.public_origin + "/invite/" in out
    assert users_file(acc)["first.admin"]["disabled"] is True
    with transaction(acc.app.state.engine) as db:
        event = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "account.invite")).one()
        assert event.reason == "Second admin for the go-live (test)" and event.detail["maintenance"] is True


def test_accounts_need_the_users_file(h):
    result = h.command("admin", "account.invite", {"display_name": "X", "email": "x@example.test", "username": "xx",
        "reason": "Accounts service availability check"}, status=409)
    assert result["error"]["code"] == "ACCOUNTS_UNAVAILABLE"
    assert h.get("admin", "/api/access")["accounts_enabled"] is False


def test_swap_automation_is_hidden_from_accounts_and_pickers(acc):
    from apps.api.services.swaps import system_actor
    with transaction(acc.app.state.engine, write=True) as db:
        department_id = db.scalar(select(m.Department.id))
        automation = db.get(m.Identity, system_actor(db, department_id).identity_id)
        # Even a misconfigured automation principal is never offered as a human account.
        automation.disabled, automation.person_id = False, acc.seed["people"][0]
        db.add(m.Account(identity_id=automation.id, username="swap.automation", email="swap@example.test"))
        db.add(m.RoleBinding(identity_id=automation.id, department_id=department_id,
                             role="approver", starts_on=date(2020, 1, 1)))
        db.add(m.AuditEvent(department_id=department_id, actor_id=automation.id, action="workspace.publish",
                            reason="Synthetic automatic publication", detail={}, correlation_id="test"))
        identity_id = automation.id
    access = acc.get("admin", "/api/access")
    assert acc.seed["identities"]["admin"] in {row["id"] for row in access["identities"]}
    assert identity_id not in {row["id"] for row in access["identities"]}
    assert identity_id not in {row["identity_id"] for row in access["bindings"]}
    assert identity_id not in access["accounts"]
    accounts = acc.get("admin", f"/api/people/{acc.seed['people'][0]}/accounts")["accounts"]
    assert accounts and identity_id not in {row["identity_id"] for row in accounts}
    reviewers = acc.get("admin", "/api/review-delegations")["reviewers"]
    assert reviewers and identity_id not in {row["id"] for row in reviewers}
    audit = acc.get("operator", "/api/audit")
    assert identity_id not in {row["id"] for row in audit["people"]}
    assert any(row["actor_id"] == identity_id for row in audit["events"])


@pytest.mark.parametrize("disabled", (True, False))
def test_swap_automation_cannot_be_enabled_given_password_linked_or_granted_role(acc, disabled):
    from apps.api.services.swaps import system_actor
    token = uuid.uuid4().hex
    with transaction(acc.app.state.engine, write=True) as db:
        department_id = db.scalar(select(m.Department.id))
        automation = db.get(m.Identity, system_actor(db, department_id).identity_id)
        automation.disabled = disabled
        identity_id, revision, issuer, subject = automation.id, automation.revision, automation.issuer, automation.subject
        db.add(m.Account(identity_id=identity_id, username="swap.automation", email="swap@example.test", accepted_at=m.now()))
        db.add(m.AccountLink(identity_id=identity_id, department_id=department_id, purpose="reset", token_hash=digest(token),
                             created_by=acc.seed["identities"]["admin"], expires_at=m.now() + timedelta(hours=1)))
    attempts = (
        ("identity.enable", {"reason": "Cannot enable automation"}, identity_id, revision),
        ("role.grant", {"identity_id": identity_id, "role": "staff", "starts_on": "2020-01-01",
                        "reason": "Cannot grant automation access"}, None, None),
        ("identity.bind", {"issuer": issuer, "subject": subject, "display_name": "Automation",
                           "person_id": acc.seed["people"][0], "reason": "Cannot link automation to a person"}, None, None),
        ("account.reset_link", {"reason": "Cannot set automation password"}, identity_id, None),
    )
    for kind, payload, target, expected_revision in attempts:
        result = acc.command("admin", kind, payload, target, expected_revision, status=409)
        assert result["error"]["code"] == "AUTOMATION_IDENTITY"
    response = anonymous(acc).post(f"/api/account-links/{token}", json={"password": PASSWORD})
    assert response.status_code == 409 and response.json()["error"]["code"] == "AUTOMATION_IDENTITY"
    with transaction(acc.app.state.engine) as db:
        identity = db.get(m.Identity, identity_id)
        assert identity.disabled == disabled and identity.revision == revision
        assert identity.person_id is None and identity.password_hash is None
        assert db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == identity_id)) is None
        assert db.scalar(select(m.AccountLink).where(m.AccountLink.identity_id == identity_id)).used_at is None
    assert users_file(acc) == {}


def test_disable_follows_in_the_sign_in_service_and_end_sessions(acc):
    identity = accepted(acc)
    client, _ = sign_in(acc, "new.person", PASSWORD)
    record = acc.get("admin", "/api/access")["identities"]
    revision = next(i for i in record if i["id"] == identity)["revision"]
    disabled = acc.command("admin", "identity.disable", {"reason": "Left the department"}, identity, revision)["record"]
    assert users_file(acc)["new.person"]["disabled"] is True
    assert client.get("/api/me").status_code == 401
    acc.command("admin", "identity.enable", {"reason": "Back"}, identity, disabled["revision"])
    assert users_file(acc)["new.person"]["disabled"] is False
    client, _ = sign_in(acc, "new.person", PASSWORD)
    ended = acc.command("admin", "account.end_sessions", {"reason": "Lost phone"}, identity)
    assert ended["ended"] == 2 and client.get("/api/me").status_code == 401  # both sessions (disable keeps them for Undo)
    with transaction(acc.app.state.engine) as db:
        revoked = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "auth.session_revoke",
                                                            m.AuditEvent.detail["identity_id"].as_string() == identity)))
        assert len(revoked) == 2 and all(e.actor_id == acc.seed["identities"]["admin"] for e in revoked)
    acc.command("admin", "account.end_sessions", {"reason": "Mine"}, acc.seed["identities"]["admin"], status=409)
    acc.command("staff", "account.end_sessions", {"reason": "Not allowed"}, identity, status=403)


def test_session_settings(acc):
    department = acc.get("admin", "/api/me")["department"]
    sessions = acc.get("admin", "/api/access")["sessions"]
    assert sessions == {"idle_minutes": 480, "max_hours": 24, "idle_unlimited": False, "max_unlimited": False,
                        "inherited": {"idle_minutes": False, "max_hours": False},
                        "defaults": {"idle_minutes": 480, "max_hours": 24},
                        "limits": s.SESSION_LIMITS}
    assert sessions["limits"]["idle_minutes"]["min"] == 5 and sessions["limits"]["max_hours"]["min"] == 1
    for bad in ({"idle_minutes": 4, "max_hours": 8}, {"idle_minutes": 60, "max_hours": 0},
                {"idle_minutes": 120, "max_hours": 1},
                {"idle_minutes": s.SESSION_LIMITS["idle_minutes"]["max"] + 1, "max_hours": s.SESSION_DURATION_MAX_HOURS},
                {"idle_minutes": 60, "max_hours": s.SESSION_DURATION_MAX_HOURS + 1}):
        acc.command("admin", "department.sessions", {**bad, "reason": "Out of bounds"}, department["id"], department["revision"], status=422)
    acc.command("coordinator", "department.sessions", {"idle_minutes": 30, "max_hours": 4}, department["id"], department["revision"], status=403)
    result = acc.command("admin", "department.sessions", {"idle_minutes": 5, "max_hours": 2, "reason": "Shared computers"},
                         department["id"], department["revision"])
    assert result["sessions"]["idle_minutes"] == 5 and result["previous"] == {
        "idle_minutes": 480, "max_hours": 24, "idle_unlimited": False, "max_unlimited": False}
    # New sessions use the new limits; policy changes immediately reconcile open sessions.
    client, _ = sign_in(acc, "coordinator", TEST_PASSWORD)
    with transaction(acc.app.state.engine) as db:
        newest = db.scalars(select(m.Session).order_by(m.Session.created_at.desc())).first()
        assert newest.expires_at.replace(tzinfo=None) <= (m.now() + timedelta(minutes=5, seconds=5)).replace(tzinfo=None)
    admin_token = acc.client("admin").cookies["rota_session"]
    keep_alive(acc.app.state.engine, admin_token, acc.settings)
    with transaction(acc.app.state.engine) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == digest(admin_token)))
        assert session.expires_at.replace(tzinfo=None) <= (m.now() + timedelta(minutes=5, seconds=5)).replace(tzinfo=None)
        assert db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == "department_sessions")) is not None


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("idle", [1, 2, 3, 4])
def test_inherited_legacy_sessions_read_save_and_undo_without_lengthening(request, database, idle, monkeypatch):
    from dataclasses import replace
    from apps.api import auth
    from apps.api.settings import new_department_session_limits
    h = request.getfixturevalue(database)
    monkeypatch.setenv("ROTA_SESSION_IDLE_MINUTES", str(idle))
    monkeypatch.setenv("ROTA_SESSION_HOURS", "4")
    h.settings = replace(h.settings, session_idle_minutes=idle, session_hours=4)
    engine = h.app.state.engine
    h.app = create_app(h.settings, engine=engine)
    h.clients = {}
    with transaction(engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.session_idle_minutes = department.session_max_hours = None
        new = m.Department(organisation_id=department.organisation_id, name="Legacy deployment new department",
                           **new_department_session_limits())
        explicit = m.Department(organisation_id=department.organisation_id, name="Explicit inheritance",
                                session_idle_minutes=None, session_max_hours=None)
        db.add_all([new, explicit])
        db.flush()
        assert new.session_idle_minutes is None and new.session_max_hours == 4
        assert explicit.session_idle_minutes is None and explicit.session_max_hours is None
        assert auth.session_limits(new, h.settings) == auth.session_limits(explicit, h.settings) == (idle, 4)
    department = h.get("admin", "/api/me")["department"]
    view = h.get("admin", "/api/access")["sessions"]
    assert view["idle_minutes"] == idle and view["max_hours"] == 4
    assert view["defaults"] == {"idle_minutes": idle, "max_hours": 4}
    assert view["inherited"] == {"idle_minutes": True, "max_hours": True}
    inherited = {"idle_minutes": None, "max_hours": None}
    same = h.command("admin", "department.sessions", inherited, department["id"], department["revision"])
    assert same["department"]["revision"] == department["revision"]
    changed = h.command("admin", "department.sessions", {"idle_minutes": None, "max_hours": 2},
                         department["id"], same["department"]["revision"])
    assert changed["sessions"]["idle_minutes"] == idle
    assert changed["previous"] == {**inherited, "idle_unlimited": False, "max_unlimited": False}
    restored = h.command("admin", "department.sessions", {**changed["previous"], "reason": "Undo"},
                          department["id"], changed["department"]["revision"])
    new_limit = h.command("admin", "department.sessions", {"idle_minutes": 5, "max_hours": None},
                           department["id"], restored["department"]["revision"])
    restored = h.command("admin", "department.sessions", {**new_limit["previous"], "reason": "Undo"},
                          department["id"], new_limit["department"]["revision"])
    assert restored["sessions"]["idle_minutes"] == idle and restored["sessions"]["inherited"]["idle_minutes"]
    for new_idle in (1, 2, 3, 4):
        h.command("admin", "department.sessions", {"idle_minutes": new_idle, "max_hours": None},
                  department["id"], restored["department"]["revision"], status=422)
    h.command("coordinator", "department.sessions", inherited, department["id"],
              restored["department"]["revision"], status=403)
    staff = h.client("staff")
    with transaction(engine) as db:
        department = db.get(m.Department, department["id"])
        assert department.session_idle_minutes is None and department.session_max_hours is None
        session = db.scalar(select(m.Session).where(m.Session.token_hash == digest(staff.cookies["rota_session"])))
        deadline = aware(session.created_at) + timedelta(minutes=idle)
        assert aware(session.expires_at) == deadline and not session.expiry_unlimited
    monkeypatch.setattr(auth, "now", lambda: deadline + timedelta(seconds=1))
    assert staff.get("/api/pulse").status_code == 401


def test_inherited_session_values_still_validate_the_resolved_finite_pair(h):
    from dataclasses import replace
    h.settings = replace(h.settings, session_idle_minutes=120)
    h.app = create_app(h.settings, engine=h.app.state.engine)
    h.clients = {}
    department = h.get("admin", "/api/me")["department"]
    h.command("admin", "department.sessions", {"idle_minutes": None, "max_hours": 1},
              department["id"], department["revision"], status=422)
    result = h.command("admin", "department.sessions", {"idle_minutes": None, "max_hours": None},
                       department["id"], department["revision"])
    assert result["sessions"]["idle_minutes"] == 120 and result["sessions"]["max_hours"] == 8


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("idle,hours", [(1440, 720),
                                       (s.SESSION_LIMITS["idle_minutes"]["max"], s.SESSION_DURATION_MAX_HOURS)])
def test_large_session_limits_are_saved_and_enforced(request, database, idle, hours, monkeypatch):
    from apps.api import auth
    h = request.getfixturevalue(database)
    department = h.get("admin", "/api/me")["department"]
    saved = h.command("admin", "department.sessions", {"idle_minutes": idle, "max_hours": hours},
                      department["id"], department["revision"])
    assert saved["sessions"]["idle_minutes"] == idle and saved["sessions"]["max_hours"] == hours
    assert h.get("admin", "/api/access")["sessions"] == saved["sessions"]
    with anonymous(h) as client:
        response = client.post("/auth/login", json={"username": "staff", "password": TEST_PASSWORD})
        assert response.status_code == 200, response.text
        expected_age = min(hours * 3600, SESSION_COOKIE_MAX_AGE_SECONDS)
        from http.cookies import SimpleCookie
        assert all(expected_age - 5 <= int(next(iter(SimpleCookie(cookie).values()))["max-age"]) <= expected_age
                   for cookie in response.headers.get_list("set-cookie"))
        key = digest(client.cookies["rota_session"])
        with transaction(h.app.state.engine) as db:
            session = db.scalar(select(m.Session).where(m.Session.token_hash == key))
            created = aware(session.created_at)
            assert abs(aware(session.expires_at) - (created + timedelta(minutes=idle))) < timedelta(seconds=1)
        # Still signed in beyond the previous 8-hour idle maximum; activity moves the idle deadline.
        moment = created + timedelta(hours=12)
        monkeypatch.setattr(auth, "now", lambda: moment)
        assert client.get("/api/me").status_code == 200
        with transaction(h.app.state.engine, write=True) as db:
            session = db.scalar(select(m.Session).where(m.Session.token_hash == key))
            cap = created + timedelta(hours=hours)
            assert aware(session.expires_at) == min(cap, moment + timedelta(minutes=idle))
            session.expires_at = cap - timedelta(seconds=30)  # simulate continuous activity until near the absolute cap
            session.last_active_at = cap - timedelta(minutes=2)
        moment = cap - timedelta(minutes=1)
        keep_alive(h.app.state.engine, client.cookies["rota_session"], h.settings)
        with transaction(h.app.state.engine) as db:
            assert aware(db.scalar(select(m.Session.expires_at).where(m.Session.token_hash == key))) == cap
        moment = cap + timedelta(seconds=1)
        assert client.get("/api/me").status_code == 401
    # Values beyond the shared technical ceiling fail validation before reaching PostgreSQL integer storage.
    monkeypatch.setattr(auth, "now", m.now)
    for bad in ({"idle_minutes": idle, "max_hours": s.SESSION_DURATION_MAX_HOURS + 1},
                {"idle_minutes": s.SESSION_LIMITS["idle_minutes"]["max"] + 1, "max_hours": hours}):
        h.command("admin", "department.sessions", bad, department["id"], saved["department"]["revision"], status=422)


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_large_session_expiry_stops_at_datetime_max(request, database, monkeypatch):
    from apps.api import auth
    h = request.getfixturevalue(database)
    department = h.get("admin", "/api/me")["department"]
    h.command("admin", "department.sessions",
              {"idle_minutes": s.SESSION_LIMITS["idle_minutes"]["max"], "max_hours": s.SESSION_DURATION_MAX_HOURS},
              department["id"], department["revision"])
    latest = aware(datetime.max)
    moment = latest - timedelta(days=1)
    monkeypatch.setattr(auth, "now", lambda: moment)
    with anonymous(h) as client:
        response = client.post("/auth/login", json={"username": "staff", "password": TEST_PASSWORD})
        assert response.status_code == 200, response.text
        assert aware(response.json()["expires_at"]) == latest
        with transaction(h.app.state.engine, write=True) as db:
            counter = db.get(m.RateLimit, auth.login_account_bucket_keys("staff", h.settings)[0])
            assert counter.hits == 0 and aware(counter.expires_at) == latest
            session = db.scalar(select(m.Session).where(m.Session.token_hash == digest(client.cookies["rota_session"])))
            session.created_at = moment
        moment += timedelta(hours=1)
        keep_alive(h.app.state.engine, client.cookies["rota_session"], h.settings)
        with transaction(h.app.state.engine) as db:
            session = db.scalar(select(m.Session).where(m.Session.token_hash == digest(client.cookies["rota_session"])))
            assert aware(session.expires_at) == latest and aware(session.last_active_at) == moment


def test_legacy_first_provider_sign_in_binds_an_old_accepted_invitation(acc):
    from apps.api import oidc
    from apps.api.services import accounts
    acc.settings = Settings(environment="test", database_url=acc.settings.database_url, identity_users_file=str(acc.users),
                            oidc_issuer="https://idp.example.test")
    with transaction(acc.app.state.engine, write=True) as db:
        admin = db.get(m.Identity, acc.seed["identities"]["admin"])
        department = db.scalars(select(m.Department)).first()
        from apps.api.auth import Actor
        actor = Actor(admin.id, department.id, department.organisation_id, admin.display_name, frozenset({"admin"}), None, "test")
        from packages.contracts import schemas as s
        result = accounts.invite(db, actor, s.AccountInvite(display_name="Provider Person (test)", email="pp@example.test",
                                                            username="provider.person", roles=["staff"],
                                                            reason="Synthetic provider invitation"), acc.settings)
        token = db.info.pop("one_time_link").rsplit("/", 1)[1]
        identity_id = result["record"]["id"]
        identity = db.get(m.Identity, identity_id)
        assert identity.issuer == "local-development" and identity.subject == "provider.person"
        # Existing pre-redesign invitations retain their historical binding behavior in the legacy helper.
        identity.issuer, identity.subject = acc.settings.oidc_issuer, "pending:provider.person"
        subject = str(uuid.uuid4())
        with pytest.raises(Exception):  # not accepted yet: no binding, no access
            oidc.finish_flow(db, acc.settings, {"sub": subject, "preferred_username": "provider.person"})
    with transaction(acc.app.state.engine, write=True) as db:
        assert accounts.accept_link(db, token, PASSWORD, acc.settings, "test") == {"accepted": True, "sign_in": "/"}
    with transaction(acc.app.state.engine, write=True) as db:
        with pytest.raises(Exception):  # only the invited username binds
            oidc.finish_flow(db, acc.settings, {"sub": subject, "preferred_username": "someone.else"})
    with transaction(acc.app.state.engine, write=True) as db:
        values = oidc.finish_flow(db, acc.settings, {"sub": subject, "preferred_username": "provider.person"})
        assert values["token"]
        assert db.get(m.Identity, identity_id).subject == subject
    with transaction(acc.app.state.engine, write=True) as db:  # later sign-ins find the bound subject directly
        assert oidc.finish_flow(db, acc.settings, {"sub": subject})["token"]


@pytest.mark.postgres
def test_invitation_and_session_limits_on_postgres(pg, tmp_path):
    acc = with_users_file(pg, tmp_path)
    token = token_of(invite(acc))
    assert anonymous(acc).post(f"/api/account-links/{token}", json={"password": PASSWORD}).status_code == 200
    assert sign_in(acc, "new.person", PASSWORD)[1] == 200
    department = acc.get("admin", "/api/me")["department"]
    acc.command("admin", "department.sessions", {"idle_minutes": 30, "max_hours": 12}, department["id"], department["revision"])
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError), transaction(acc.app.state.engine, write=True) as db:  # the database refuses it too
        db.execute(update(m.Department).values(session_idle_minutes=4))

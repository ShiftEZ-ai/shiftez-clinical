"""Idle session timeout, pruning of expired security state, fail-safe ROTA_ENV, no echoed values in errors, proxy trust."""
from .clock_support import freeze_clock
from .conftest import TEST_PASSWORD
from datetime import date, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from fastapi.testclient import TestClient
from pydantic import BaseModel, field_validator
import pytest
from sqlalchemy import delete, func, select
from apps.api import models as m
from apps.api.auth import digest, SESSION_COOKIE_MAX_AGE_SECONDS
from apps.api.db import transaction
from apps.api.settings import Settings, new_department_session_limits
from packages.contracts.schemas import SESSION_DURATION_MAX_HOURS, SESSION_LIMITS
from packages.domain.time import aware
from apps.worker import main as worker

ROOT = Path(__file__).resolve().parents[1]


def session_of(h, role):
    return digest(h.client(role).cookies["rota_session"])


def expiry(h, token_hash):
    with transaction(h.app.state.engine) as db:
        return aware(db.scalar(select(m.Session.expires_at).where(m.Session.token_hash == token_hash)))


def shift_session(h, token_hash, *, expires_in, created_ago=timedelta(0)):
    with transaction(h.app.state.engine, write=True) as db:
        row = db.scalar(select(m.Session).where(m.Session.token_hash == token_hash))
        row.expires_at, row.created_at = m.now() + expires_in, m.now() - created_ago


def near(value, target, slack=timedelta(seconds=30)):
    return abs(value - target) < slack


def cookie_ages(response):
    cookies = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        cookies.load(header)
    return {name: int(cookie["max-age"]) for name, cookie in cookies.items()
            if name in {"rota_session", "__Host-rota", "rota_csrf"}}


def test_sign_in_lasts_the_idle_limit_and_activity_slides_it_at_most_once_a_minute(h):
    key = session_of(h, "staff")
    assert near(expiry(h, key), m.now() + timedelta(minutes=480))
    shift_session(h, key, expires_in=timedelta(minutes=5))
    assert h.client("staff").get("/api/me").status_code == 200
    assert near(expiry(h, key), m.now() + timedelta(minutes=480))
    shift_session(h, key, expires_in=timedelta(minutes=479, seconds=30))  # touched under a minute ago: no write
    before = expiry(h, key)
    h.client("staff").get("/api/me")
    assert expiry(h, key) == before


def test_background_checks_do_not_count_as_activity(h):
    key = session_of(h, "staff")
    shift_session(h, key, expires_in=timedelta(minutes=5))
    before = expiry(h, key)
    assert h.client("staff").get("/api/pulse").status_code == 200
    assert expiry(h, key) == before


def test_api_activity_and_commands_never_reemit_session_cookies(h):
    client = h.client("staff")
    token, csrf = client.cookies["rota_session"], client.cookies["rota_csrf"]
    response = client.get("/api/me")
    assert not response.headers.get_list("set-cookie")
    assert client.cookies["rota_session"] == token and client.cookies["rota_csrf"] == csrf
    me = response.json()
    admin = h.client("admin")
    response = admin.post("/api/commands", headers={"idempotency-key": "session-cookie-renewal"},
                          json={"kind": "department.sessions", "target_id": me["department"]["id"],
                                "expected_revision": me["department"]["revision"],
                                "payload": {"idle_minutes": 60, "max_hours": 8}})
    assert response.status_code == 200 and not response.headers.get_list("set-cookie")
    assert not client.get("/api/pulse").headers.get_list("set-cookie")
    rejected = client.post("/api/commands", headers={"x-csrf-token": "incorrect", "idempotency-key": "rejected-cookie-renewal"},
                           json={"kind": "account.end_other_sessions", "payload": {"reason": "Synthetic rejected request"}})
    assert rejected.status_code == 403 and not rejected.headers.get_list("set-cookie")
    shift_session(h, digest(token), expires_in=timedelta(seconds=-1))
    response = client.get("/api/me")
    assert response.status_code == 401 and not response.headers.get_list("set-cookie")


def test_absolute_cap_and_no_revival_after_idle_expiry(h):
    key = session_of(h, "staff")
    shift_session(h, key, expires_in=timedelta(minutes=2), created_ago=timedelta(hours=24) - timedelta(minutes=10))
    response = h.client("staff").get("/api/me")
    assert response.status_code == 200 and not response.headers.get_list("set-cookie")
    assert near(expiry(h, key), m.now() + timedelta(minutes=10))  # sign-in + 24 h, not now + 480 min
    shift_session(h, key, expires_in=timedelta(seconds=-1))
    assert h.client("staff").get("/api/me").status_code == 401
    assert expiry(h, key) < m.now()


def test_idle_limit_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv("ROTA_SESSION_IDLE_MINUTES", "15")
    assert Settings.from_env().session_idle_minutes == 15
    for bad in (0, 8 * 60 + 1):
        with pytest.raises(RuntimeError, match="ROTA_SESSION_IDLE_MINUTES"):
            Settings(environment="test", session_idle_minutes=bad).check()


def test_session_defaults_share_department_validation(monkeypatch):
    monkeypatch.setenv("ROTA_SESSION_IDLE_MINUTES", "1440")
    monkeypatch.setenv("ROTA_SESSION_HOURS", "720")
    settings = Settings.from_env()
    settings.check()
    assert settings.session_idle_minutes == 1440 and settings.session_hours == 720
    Settings(environment="test", session_idle_minutes=5, session_hours=1).check()
    Settings(environment="test", session_idle_minutes=SESSION_LIMITS["idle_minutes"]["max"],
             session_hours=SESSION_DURATION_MAX_HOURS).check()
    for idle, hours in ((0, 8), (60, 0), (61, 1), (60, SESSION_DURATION_MAX_HOURS + 1),
                        (SESSION_LIMITS["idle_minutes"]["max"] + 1, SESSION_DURATION_MAX_HOURS)):
        with pytest.raises(RuntimeError, match="ROTA_SESSION"):
            Settings(environment="test", session_idle_minutes=idle, session_hours=hours).check()


@pytest.mark.parametrize("idle", [1, 2, 3, 4])
def test_legacy_deployment_idle_limits_remain_valid_without_becoming_admin_overrides(monkeypatch, idle):
    from pydantic import ValidationError
    from packages.contracts.schemas import DeploymentSessionSettingsInput, SessionSettingsInput
    monkeypatch.setenv("ROTA_SESSION_IDLE_MINUTES", str(idle))
    monkeypatch.setenv("ROTA_SESSION_HOURS", "4")
    settings = Settings.from_env()
    settings.check()
    assert settings.session_idle_minutes == idle and settings.session_hours == 4
    assert new_department_session_limits() == {"session_idle_minutes": None, "session_max_hours": 4}
    with pytest.raises(ValidationError):
        SessionSettingsInput(idle_minutes=idle, max_hours=4)
    for missing in ({"idle_minutes": None}, {"max_hours": None}):
        with pytest.raises(ValidationError):
            SessionSettingsInput(**missing)
    for invalid in (None, True, "1", 0):
        with pytest.raises(ValidationError):
            DeploymentSessionSettingsInput(idle_minutes=invalid, max_hours=4)
    with pytest.raises(ValidationError):
        DeploymentSessionSettingsInput(idle_minutes=idle, max_hours=None)


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_new_department_defaults_preserve_legacy_inheritance(request, database, monkeypatch):
    from apps.api import auth
    monkeypatch.delenv("ROTA_SESSION_IDLE_MINUTES", raising=False)
    monkeypatch.delenv("ROTA_SESSION_HOURS", raising=False)
    h = request.getfixturevalue(database)
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        assert (department.session_idle_minutes, department.session_max_hours) == (480, 24)
        another = m.Department(organisation_id=department.organisation_id, name="New department (test)")
        db.add(another)
        db.flush()
        assert (another.session_idle_minutes, another.session_max_hours) == (480, 24)
        assert not another.session_idle_unlimited and not another.session_max_unlimited
        department.session_idle_minutes = department.session_max_hours = None
    assert new_department_session_limits() == {"session_idle_minutes": 480, "session_max_hours": 24}
    with transaction(h.app.state.engine) as db:
        department = db.get(m.Department, h.seed["department_id"])
        assert auth.session_limits(department, h.settings) == (60, 8)
    monkeypatch.setenv("ROTA_SESSION_HOURS", "4")
    assert new_department_session_limits() == {"session_idle_minutes": 240, "session_max_hours": 4}
    configured = Settings.from_env()
    configured.check()
    with transaction(h.app.state.engine) as db:
        department = db.get(m.Department, h.seed["department_id"])
        assert auth.session_limits(department, configured) == (60, 4)
        from apps.api.services.accounts import session_view
        assert session_view(department, configured)["defaults"] == {"idle_minutes": 240, "max_hours": 4}
    monkeypatch.setenv("ROTA_SESSION_IDLE_MINUTES", "15")
    monkeypatch.setenv("ROTA_SESSION_HOURS", "3")
    configured = Settings.from_env()
    assert new_department_session_limits() == {"session_idle_minutes": 15, "session_max_hours": 3}
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        assert department.session_idle_minutes is None and department.session_max_hours is None
        assert auth.session_limits(department, configured) == (15, 3)
        issued = auth.issue_session(db, db.get(m.Identity, h.seed["identities"]["staff"]), department.id, configured)
        session = db.get(m.Session, issued["session_id"])
        assert aware(session.expires_at) == aware(session.created_at) + timedelta(minutes=15)
        assert not session.expiry_unlimited
    monkeypatch.setenv("ROTA_SESSION_IDLE_MINUTES", "0")
    with pytest.raises(RuntimeError, match="ROTA_SESSION"):
        new_department_session_limits()


def set_session_policy(h, **values):
    department = h.get("admin", "/api/me")["department"]
    return h.command("admin", "department.sessions", values, department["id"], department["revision"])


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("unlimited", [False, True])
def test_only_login_and_refresh_set_cookies_for_absolute_deadline_and_unlimited_transport_cap(
        request, database, unlimited, monkeypatch):
    from apps.api import auth
    h = request.getfixturevalue(database)
    set_session_policy(h, idle_minutes=5, max_hours=1, idle_unlimited=unlimited, max_unlimited=unlimited)
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        client.headers["origin"] = h.settings.public_origin
        response = client.post("/auth/login", json={"username": "staff", "password": TEST_PASSWORD})
        assert response.status_code == 200
        ages = cookie_ages(response)
        assert len(ages) == 2
        if unlimited:
            assert set(ages.values()) == {SESSION_COOKIE_MAX_AGE_SECONDS}
            assert response.json()["expires_at"] is None
        else:
            assert all(3595 <= age <= 3600 for age in ages.values())
            assert near(aware(response.json()["expires_at"]), m.now() + timedelta(minutes=5))
        cookies = response.headers.get_list("set-cookie")
        assert "HttpOnly" in next(cookie for cookie in cookies if cookie.startswith("rota_session="))
        assert "HttpOnly" not in next(cookie for cookie in cookies if cookie.startswith("rota_csrf="))
        client.headers["x-csrf-token"] = client.cookies["rota_csrf"]
        original = client.cookies["rota_session"]
        later = m.now() + timedelta(minutes=2)
        monkeypatch.setattr(auth, "now", lambda: later)
        activity = client.get("/api/me")
        assert activity.status_code == 200 and not activity.headers.get_list("set-cookie")
        if not unlimited:
            assert near(expiry(h, digest(original)), later + timedelta(minutes=5))
        refreshed = client.post("/auth/refresh")
        assert refreshed.status_code == 200
        ages = cookie_ages(refreshed)
        assert len(ages) == 2
        assert client.cookies["rota_session"] != original
        if unlimited:
            assert set(ages.values()) == {SESSION_COOKIE_MAX_AGE_SECONDS}
        else:
            assert all(3475 <= age <= 3480 for age in ages.values())


def test_cookie_transport_never_extends_expired_or_revoked_server_sessions(h, monkeypatch):
    from apps.api import auth
    from datetime import datetime
    moment = m.now()
    monkeypatch.setattr(auth, "now", lambda: moment)
    assert auth.session_cookie_max_age(moment - timedelta(seconds=1)) == 0
    assert auth.session_cookie_max_age(moment + timedelta(seconds=1, microseconds=900000)) == 1
    assert auth.session_cookie_max_age(aware(datetime.max)) == SESSION_COOKIE_MAX_AGE_SECONDS
    client = h.client("staff")
    token = client.cookies["rota_session"]
    with transaction(h.app.state.engine, write=True) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == digest(token)))
        session.revoked = True
    assert client.get("/api/me").status_code == 401
    assert client.post("/auth/refresh").status_code == 401
    admin = h.client("admin")
    response = admin.get("/api/me")
    assert response.status_code == 200 and not response.headers.get_list("set-cookie")
    with transaction(h.app.state.engine, write=True) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == digest(admin.cookies["rota_session"])))
        db.get(m.Identity, session.identity_id).person_id = h.seed["people"][0]
        target, revision = session.id, session.revision
    response = admin.post("/api/commands", headers={"idempotency-key": "revoke-current-cookie"},
                          json={"kind": "session.revoke", "target_id": target, "expected_revision": revision,
                                "payload": {"reason": "This device"}})
    assert response.status_code == 200 and not response.headers.get_list("set-cookie")
    assert admin.get("/api/me").status_code == 401
    assert admin.post("/auth/refresh").status_code == 401


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("idle_unlimited,max_unlimited", [(True, False), (False, True), (True, True)])
def test_each_unlimited_choice_has_real_deadline_semantics(request, database, idle_unlimited, max_unlimited, monkeypatch):
    from apps.api import auth
    h = request.getfixturevalue(database)
    saved = set_session_policy(h, idle_minutes=5, max_hours=1,
                               idle_unlimited=idle_unlimited, max_unlimited=max_unlimited)
    assert saved["sessions"]["idle_unlimited"] is idle_unlimited
    assert saved["sessions"]["max_unlimited"] is max_unlimited
    assert (saved["sessions"]["idle_minutes"], saved["sessions"]["max_hours"]) == (5, 1)
    client = h.client("staff")
    key = digest(client.cookies["rota_session"])
    moment = m.now()
    created = moment - timedelta(hours=2 if max_unlimited else .5)
    active = created if idle_unlimited else moment - timedelta(minutes=1)
    deadline = auth.session_expiry(created, active, None if idle_unlimited else 5, None if max_unlimited else 1)
    with transaction(h.app.state.engine, write=True) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == key))
        session.created_at, session.last_active_at = created, active
        session.expiry_unlimited = deadline is None
        session.expires_at = deadline or moment - timedelta(days=4000)
    assert client.get("/api/pulse").status_code == 200  # background reads do not invent activity
    with transaction(h.app.state.engine) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == key))
        assert aware(session.last_active_at) == active
        assert auth.session_deadline(session) == deadline
    later = (moment + timedelta(days=365 * 5000) if idle_unlimited and max_unlimited else
             created + timedelta(hours=1, seconds=1) if not max_unlimited else moment + timedelta(minutes=6))
    monkeypatch.setattr(auth, "now", lambda: later)
    assert client.get("/api/pulse").status_code == (200 if deadline is None else 401)


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_policy_transitions_and_undo_never_revive_an_ended_session(request, database, monkeypatch):
    from apps.api import auth
    h = request.getfixturevalue(database)
    set_session_policy(h, idle_minutes=5, max_hours=24, idle_unlimited=True, max_unlimited=True)
    staff = h.client("staff")
    key = digest(staff.cookies["rota_session"])
    moment = m.now()
    monkeypatch.setattr(auth, "now", lambda: moment)
    freeze_clock(monkeypatch, lambda: moment)
    with transaction(h.app.state.engine, write=True) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == key))
        session.created_at = session.last_active_at = moment
    finite = set_session_policy(h, idle_minutes=5, max_hours=24)
    with transaction(h.app.state.engine) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == key))
        assert not session.expiry_unlimited and aware(session.expires_at) == moment + timedelta(minutes=5)
        assert aware(session.last_active_at) == moment  # changing a setting did not count as staff activity
    moment += timedelta(minutes=3)
    h.get("admin", "/api/me")  # the admin stays active; the staff device remains unattended
    moment += timedelta(minutes=3)
    restored = set_session_policy(h, **finite["previous"], reason="Undo")
    assert restored["sessions"]["idle_unlimited"] and restored["sessions"]["max_unlimited"]
    assert staff.get("/api/pulse").status_code == 401  # no staff request happened during the finite interval
    with transaction(h.app.state.engine) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == key))
        assert not session.expiry_unlimited and aware(session.expires_at) < moment
    # A fresh session is genuinely unlimited, and the API has no fabricated expiry date.
    with TestClient(h.app, base_url=h.settings.public_origin) as fresh:
        fresh.headers["origin"] = h.settings.public_origin
        response = fresh.post("/auth/login", json={"username": "staff", "password": TEST_PASSWORD})
        assert response.status_code == 200 and response.json()["expires_at"] is None
        fresh.headers["x-csrf-token"] = fresh.cookies["rota_csrf"]
        fresh_token = digest(fresh.cookies["rota_session"])
        with transaction(h.app.state.engine) as db:
            session = db.scalar(select(m.Session).where(m.Session.token_hash == fresh_token))
            assert session.expiry_unlimited and aware(session.expires_at) == moment
        assert fresh.post("/auth/logout", headers={"x-csrf-token": "incorrect"}).status_code == 403
        assert fresh.post("/auth/logout").status_code == 200
        with transaction(h.app.state.engine) as db:
            session = db.scalar(select(m.Session).where(m.Session.token_hash == fresh_token))
            assert session.revoked and not session.expiry_unlimited
            assert aware(session.expires_at) == moment


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_background_auth_and_policy_changes_enforce_last_actual_activity(request, database):
    from apps.api import auth
    h = request.getfixturevalue(database)
    set_session_policy(h, idle_minutes=5, max_hours=1)
    staff = h.client("staff")
    key = digest(staff.cookies["rota_session"])
    activity = m.now() - timedelta(minutes=10)
    with transaction(h.app.state.engine, write=True) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == key))
        session.created_at = session.last_active_at = activity
        session.expires_at = m.now() + timedelta(hours=1)  # stale cache cannot override the current finite policy
    assert staff.get("/api/pulse").status_code == 401
    with transaction(h.app.state.engine) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == key))
        assert aware(session.last_active_at) == activity
    set_session_policy(h, idle_minutes=5, max_hours=1, idle_unlimited=True, max_unlimited=True)
    with transaction(h.app.state.engine) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == key))
        assert not session.expiry_unlimited and aware(session.expires_at) == activity + timedelta(minutes=5)
    assert staff.get("/api/pulse").status_code == 401


@pytest.mark.postgres
def test_stale_keep_alive_cannot_overwrite_a_concurrent_policy_transition(pg, monkeypatch):
    from contextlib import contextmanager
    from sqlalchemy.sql.dml import Update
    from apps.api import auth
    token = pg.client("staff").cookies["rota_session"]
    department = pg.get("admin", "/api/me")["department"]
    activity = m.now() - timedelta(minutes=2)
    with transaction(pg.app.state.engine, write=True) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == digest(token)))
        session.last_active_at = activity
        session.expires_at = m.now() + timedelta(hours=1)
        session_id, revision = session.id, session.revision
    changed = []

    class BeforeRefresh:
        @contextmanager
        def begin(self):
            with pg.app.state.engine.begin() as connection:
                class BetweenReadAndWrite:
                    def execute(self, statement):
                        if isinstance(statement, Update) and not changed:
                            changed.append(pg.command("admin", "department.sessions", {"idle_minutes": 5, "max_hours": 24},
                                                      department["id"], department["revision"]))
                        return connection.execute(statement)
                yield BetweenReadAndWrite()

    class SessionAdapter:
        def __init__(self, connection):
            self._connection = connection

        def connection(self):
            return self._connection

    def test_liveness_write(engine, operation, **_):
        with engine.begin() as connection:
            return operation(SessionAdapter(connection))

    monkeypatch.setattr(auth, "liveness_write", test_liveness_write)

    auth.keep_alive(BeforeRefresh(), token, pg.settings)
    assert changed
    with transaction(pg.app.state.engine) as db:
        session = db.get(m.Session, session_id)
        assert aware(session.last_active_at) == activity
        assert aware(session.expires_at) == activity + timedelta(minutes=5)
        assert session.revision == revision + 1 and not session.expiry_unlimited


def test_unlimited_flags_are_explicit_and_retained_numbers_stay_validated():
    from pydantic import ValidationError
    from packages.contracts.schemas import SessionSettingsInput
    for idle_unlimited, max_unlimited in ((True, False), (False, True), (True, True)):
        value = SessionSettingsInput(idle_minutes=120, max_hours=1, idle_unlimited=idle_unlimited, max_unlimited=max_unlimited)
        assert value.idle_minutes == 120 and value.max_hours == 1
    for payload in ({"idle_minutes": 120, "max_hours": 1},
                    {"idle_minutes": 4, "max_hours": 1, "idle_unlimited": True},
                    {"idle_minutes": 5, "max_hours": 0, "max_unlimited": True},
                    {"idle_minutes": 5, "max_hours": 1, "max_unlimited": "true"}):
        with pytest.raises(ValidationError):
            SessionSettingsInput(**payload)


def test_worker_prunes_only_expired_security_state_in_bounded_batches(h, monkeypatch):
    engine = h.app.state.engine
    key = session_of(h, "staff")
    with transaction(engine, write=True) as db:
        live = db.scalar(select(m.Session).where(m.Session.token_hash == key))
        audits = db.scalar(select(func.count()).select_from(m.AuditEvent))
        db.execute(delete(m.RateLimit))
        for n, ended in enumerate((timedelta(days=8), timedelta(days=9), timedelta(days=1))):
            db.add(m.Session(identity_id=live.identity_id, department_id=live.department_id, token_hash=f"old{n}",
                             csrf_hash="x", expires_at=m.now() - ended))
        for revoked in (False, True):
            db.add(m.Session(identity_id=live.identity_id, department_id=live.department_id,
                             token_hash=f"unlimited-{revoked}", csrf_hash="x", expiry_unlimited=True,
                             revoked=revoked, expires_at=m.now() - timedelta(days=10)))
        for n, age in enumerate((timedelta(days=2), timedelta(days=3), timedelta(minutes=5))):
            db.add(m.LoginAttempt(key_hash=f"k{n}", succeeded=False, created_at=m.now() - age))
        for n, remaining in enumerate((timedelta(days=-2), timedelta(days=-3), timedelta(minutes=5))):
            db.add(m.RateLimit(key_hash=f"limit{n}", hits=1, expires_at=m.now() + remaining))
    monkeypatch.setattr(worker, "_pruned", {})
    assert worker.prune_security_state(engine, batch=1) == 3  # one of each per batch
    assert worker.prune_security_state(engine) == 0  # at most every 10 minutes
    monkeypatch.setattr(worker, "_pruned", {})
    assert worker.prune_security_state(engine) == 4
    with transaction(engine) as db:
        assert set(db.scalars(select(m.Session.token_hash).where(m.Session.token_hash.like("old%")))) == {"old2"}
        assert set(db.scalars(select(m.LoginAttempt.key_hash).where(m.LoginAttempt.key_hash.like("k%")))) == {"k2"}
        assert set(db.scalars(select(m.RateLimit.key_hash))) == {"limit2"}
        assert set(db.scalars(select(m.Session.token_hash).where(m.Session.token_hash.like("unlimited-%")))) == {"unlimited-False"}
        assert db.scalar(select(func.count()).select_from(m.AuditEvent)) == audits
    assert h.client("staff").get("/api/me").status_code == 200


def test_unset_environment_means_production(monkeypatch):
    monkeypatch.delenv("ROTA_ENV", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///fictional.db")
    settings = Settings.from_env()
    assert settings.environment == "production"
    with pytest.raises(RuntimeError, match="PostgreSQL|refused in production"):
        settings.check()  # refuses unsafe production configuration rather than enabling legacy test identities


def test_deployment_names_the_badge_and_defaults_to_rota_env(monkeypatch):
    """ROTA_DEPLOYMENT is display only (DEV runs ROTA_ENV=production with ROTA_DEPLOYMENT=dev); unset it follows
    ROTA_ENV, so a production server that never set it still shows Production, never Test."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///fictional.db")
    for rota_env, deployment, expected in (("production", None, "production"), ("production", "dev", "dev"),
                                           ("production", " Production ", "production"), ("test", None, "test")):
        monkeypatch.setenv("ROTA_ENV", rota_env)
        if deployment is None:
            monkeypatch.delenv("ROTA_DEPLOYMENT", raising=False)
        else:
            monkeypatch.setenv("ROTA_DEPLOYMENT", deployment)
        settings = Settings.from_env()
        assert (settings.environment, settings.deployment) == (rota_env, expected)


class Dated(BaseModel):
    day: str

    @field_validator("day")
    @classmethod
    def parse(cls, value):
        date.fromisoformat(value)  # library error text quotes the input
        return value


def test_errors_carry_field_and_message_but_never_input_values(h):
    @h.app.get("/api/test-value-error")
    def value_error(kind: str):
        if kind == "ours":
            raise ValueError("End date must follow start date")
        int("private-internal-value")

    @h.app.post("/api/test-validation")
    def validation(data: Dated):
        return {}

    with TestClient(h.app, base_url=h.settings.public_origin) as c:
        ours = c.get("/api/test-value-error", params={"kind": "ours"})
        assert ours.status_code == 422 and ours.json()["error"]["message"] == "End date must follow start date"
        library = c.get("/api/test-value-error", params={"kind": "library"})
        assert library.status_code == 422 and "private-internal-value" not in library.text
        invalid = c.post("/api/test-validation", headers={"origin": h.settings.public_origin}, json={"day": "secret-token-value"})
        assert invalid.status_code == 422 and "secret-token-value" not in invalid.text
        assert invalid.json()["error"]["details"][0]["field"] == "body.day"


def test_only_the_proxy_hop_is_trusted_for_client_addresses():
    compose = (ROOT / "tests/fixtures/guards/compose.yml").read_text(encoding="utf-8")
    assert "FORWARDED_ALLOW_IPS: ${APP_EDGE_IP:?" in compose
    caddy = (ROOT / "tests/fixtures/guards/Caddyfile").read_text(encoding="utf-8")
    assert "trusted_proxies static {$APP_TRUSTED_PROXIES}" in caddy
    assert "private_ranges" not in caddy
    # Production server: clients reach Caddy directly (hospital LANs are private ranges too), so only an explicitly
    # configured load balancer is trusted; startup resolves Caddy's actual peer without changing existing networks.
    production = (ROOT / "tests/fixtures/guards/compose.production.yml").read_text(encoding="utf-8")
    assert "FORWARDED_ALLOW_IPS: ${APP_EDGE_IP:-}" in production
    assert "ipv4_address:" not in production and "ipam:" not in production
    assert "APP_TRUSTED_PROXIES: ${APP_TRUSTED_PROXIES:-127.0.0.1/32}" in production
    edge = (ROOT / "tests/fixtures/guards/Caddyfile").read_text(encoding="utf-8")
    assert "trusted_proxies static {$APP_TRUSTED_PROXIES}" in edge and "private_ranges" not in edge
    assert "header_up X-Forwarded-For {client_ip}" in edge and "header_up X-Forwarded-For {client_ip}" in caddy

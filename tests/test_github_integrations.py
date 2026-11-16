"""App-managed GitHub credentials stay private, scoped and revision guarded."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import logging
from threading import Event
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.auth import digest
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.serialization import record
from apps.api.services import github_integrations as github
from packages.contracts.schemas import PERMISSIONS, ROLE_NAMES
from .clock_support import freeze_clock
from .governance_support import identity


TOKEN = "github_pat_fictional_integration_test_only_1234567890ABCD"
OTHER_TOKEN = "github_pat_fictional_replacement_test_only_9876543210WXYZ"
REPO = "example/fictional-feedback"
KEY = "17" * 32
STATUS_FIELDS = {"connected", "source", "repo", "token_hint", "connected_by_name", "connected_at",
                 "last_tested_at", "last_test_ok", "last_used_at", "token_expires_at", "key_configured", "revision"}


def configure(h, **values):
    for name, value in {"fmuc_data_key": KEY, **values}.items():
        object.__setattr__(h.settings, name, value)


def repository_response(monkeypatch, *, status=200, body=None, headers=None, content=None, error=None):
    calls = []

    def get(url, **options):
        calls.append((url, options))
        if error:
            raise error("GitHub private failure " + TOKEN)
        payload = {"has_issues": True, "permissions": {"push": False, "triage": False}} if body is None else body
        return httpx.Response(status, headers=headers, request=httpx.Request("GET", url),
                              **({"content": content} if content is not None else {"json": payload}))

    monkeypatch.setattr(github.httpx, "get", get)
    return calls


def connect(h, token=TOKEN, repo=REPO, *, revision=None, key=None, role="operator", status=200):
    return h.command(role, "integration.github.connect", {"repo": repo, "token": token},
                     revision=revision, key=key, status=status)


def read(h, role="operator"):
    return h.get(role, "/api/integrations/github")


def saved(h):
    with transaction(h.app.state.engine) as db:
        row = github.credential(db, h.seed["department_id"])
        if row is not None:
            row.token_ciphertext  # Load the deferred bytes while this test session is open.
        return row


def report(h):
    return h.command("staff", "feedback.create", {"kind": "problem", "message": "Synthetic integration feedback",
                                                 "route": "#planner"})["id"]


def export(h, report_id, *, create=False, status=200):
    return h.command("operator", "feedback.export_github", {"id": report_id, "create_issue": create}, status=status)


def test_status_shapes_and_missing_key_leave_reads_available(h, monkeypatch):
    calls = repository_response(monkeypatch)
    empty = read(h)
    assert set(empty) == STATUS_FIELDS
    assert empty == {name: False if name in {"connected", "key_configured"} else None for name in STATUS_FIELDS}
    rejected = connect(h, status=409)
    assert rejected["error"]["code"] == "INTEGRATION_KEY_MISSING"
    assert saved(h) is None and not calls
    configure(h)
    assert read(h) == {**empty, "key_configured": True}


def test_connect_encrypts_bytes_and_exposes_only_safe_status_receipts_audit_and_logs(h, monkeypatch, caplog):
    configure(h)
    calls = repository_response(monkeypatch)
    key = str(uuid.uuid4())
    caplog.set_level(logging.INFO)
    connected = connect(h, key=key)
    current = read(h)
    assert set(connected) == STATUS_FIELDS | {"receipt_id", "activity"}
    assert {k: connected[k] for k in STATUS_FIELDS} == current
    assert current["connected"] and current["source"] == "app" and current["key_configured"]
    assert current["repo"] == REPO and current["token_hint"] == TOKEN[-4:]
    assert current["connected_by_name"] == h.get("operator", "/api/me")["display_name"]
    assert current["connected_at"] and current["last_tested_at"] and current["last_test_ok"] is True
    assert current["last_used_at"] is None and current["token_expires_at"] is None and current["revision"] >= 1
    assert len(calls) == 1
    url, options = calls[0]
    assert url == "https://api.github.com/repos/" + REPO
    assert options["headers"]["Authorization"] == "Bearer " + TOKEN
    assert options["timeout"] > 0 and not options.get("follow_redirects", False)
    row = saved(h)
    assert isinstance(row.token_ciphertext, bytes) and TOKEN.encode() not in row.token_ciphertext
    assert github.decrypt(row, h.settings) == TOKEN
    assert row.connected_by == h.seed["identities"]["operator"]
    receipt = h.get("operator", "/api/commands/" + key)
    assert receipt["result"] == connected
    with transaction(h.app.state.engine) as db:
        receipts = [record(row) for row in db.scalars(select(m.CommandReceipt))]
        audits = [record(row) for row in db.scalars(select(m.AuditEvent))]
    assert any(row["action"] == "integration.github.connect" for row in audits)
    for text in (json.dumps(connected), json.dumps(current), json.dumps(receipt), json.dumps(receipts), json.dumps(audits), caplog.text):
        assert TOKEN not in text and "Authorization" not in text


@pytest.mark.parametrize("role", ROLE_NAMES)
def test_status_and_all_integration_writes_obey_current_roles(h, monkeypatch, role):
    configure(h)
    calls = repository_response(monkeypatch)
    if role not in h.seed["identities"]:
        identity(h, role, [role])
    connected = connect(h)
    allowed = role in PERMISSIONS["integrations.github"]
    h.get(role, "/api/integrations/github", status=200 if allowed else 403)
    assert ("integrations.github" in h.get(role, "/api/me")["permissions"]) is allowed
    if allowed:
        tested = h.command(role, "integration.github.test", {})
        replaced = connect(h, OTHER_TOKEN, revision=tested["revision"], role=role)
        h.command(role, "integration.github.disconnect", {"revision": replaced["revision"]})
    else:
        connect(h, OTHER_TOKEN, revision=connected["revision"], role=role, status=403)
        h.command(role, "integration.github.test", {}, status=403)
        h.command(role, "integration.github.disconnect", {"revision": connected["revision"]}, status=403)
        assert read(h)["revision"] == connected["revision"] and len(calls) == 1


def test_integration_endpoints_receipts_and_me_use_the_current_permission_map(h, monkeypatch):
    configure(h)
    calls = repository_response(monkeypatch)
    monkeypatch.setitem(PERMISSIONS, "integrations.github", ("coordinator",))
    assert "integrations.github" in h.get("coordinator", "/api/me")["permissions"]
    assert "integrations.github" not in h.get("operator", "/api/me")["permissions"]
    assert read(h, "coordinator")["connected"] is False
    h.get("operator", "/api/integrations/github", status=403)
    connect(h, status=403)
    h.command("operator", "integration.github.test", {}, status=403)
    h.command("operator", "integration.github.disconnect", {"revision": 1}, status=403)
    assert not calls
    key = str(uuid.uuid4())
    connected = connect(h, role="coordinator", key=key)
    tested = h.command("coordinator", "integration.github.test", {})
    assert tested["last_test_ok"] is True and len(calls) == 2
    assert not h.command("coordinator", "integration.github.disconnect", {"revision": tested["revision"]})["connected"]
    assert h.get("coordinator", "/api/commands/" + key)["result"] == connected
    assert connect(h, role="coordinator", key=key) == connected and len(calls) == 2
    monkeypatch.setitem(PERMISSIONS, "integrations.github", ("operator",))
    assert "integrations.github" not in h.get("coordinator", "/api/me")["permissions"]
    h.get("coordinator", "/api/commands/" + key, status=403)
    connect(h, role="coordinator", key=key, status=403)
    assert len(calls) == 2 and saved(h) is None


def test_status_and_commands_require_authentication_csrf_origin_and_idempotency(h, monkeypatch):
    configure(h)
    calls = repository_response(monkeypatch)
    body = {"kind": "integration.github.connect", "payload": {"repo": REPO, "token": TOKEN}}
    with TestClient(h.app, base_url=h.settings.public_origin) as anonymous:
        assert anonymous.get("/api/integrations/github").status_code == 401
        assert anonymous.post("/api/commands", json=body, headers={"origin": h.settings.public_origin,
                            "idempotency-key": str(uuid.uuid4())}).status_code == 401
    client = h.client("operator")
    assert client.post("/api/commands", json=body).status_code == 428
    assert client.post("/api/commands", json=body, headers={"idempotency-key": str(uuid.uuid4()),
                       "x-csrf-token": "forged"}).status_code == 403
    assert client.post("/api/commands", json=body, headers={"idempotency-key": str(uuid.uuid4()),
                       "origin": "https://attacker.invalid"}).status_code == 403
    assert saved(h) is None and not calls


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("kind", ["connect", "test"])
@pytest.mark.parametrize("age_hours", [12, 25])
def test_connection_checks_use_inherited_app_settings_and_reject_expired_sessions(request, database, monkeypatch,
                                                                                kind, age_hours):
    h = request.getfixturevalue(database)
    monkeypatch.setenv("ROTA_SESSION_IDLE_MINUTES", "60")
    monkeypatch.setenv("ROTA_SESSION_HOURS", "8")
    configure(h, session_idle_minutes=480, session_hours=24)
    calls = repository_response(monkeypatch)
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.session_idle_minutes = department.session_max_hours = None
    client = h.client("operator")
    if kind == "test":
        connect(h)
    with transaction(h.app.state.engine, write=True) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == digest(client.cookies["rota_session"])))
        session.created_at = m.now() - timedelta(hours=age_hours)
        session.last_active_at = m.now() - timedelta(minutes=5)
        session.expires_at = m.now() + timedelta(hours=1)
    before = len(calls)
    valid = age_hours < 24
    result = h.command("operator", "integration.github." + kind,
                       {"repo": REPO, "token": TOKEN} if kind == "connect" else {}, status=200 if valid else 401)
    if valid:
        assert result["connected"] and result["last_test_ok"] is True
        assert len(calls) == before + 1
    else:
        assert result["error"]["code"] == "SESSION_EXPIRED" and len(calls) == before


@pytest.mark.parametrize("options", [
    {"status": 401}, {"status": 403}, {"status": 404}, {"status": 503}, {"status": 302},
    {"body": {"has_issues": False, "message": TOKEN}}, {"body": {"has_issues": "true"}}, {"body": []},
    {"content": ("invalid JSON " + TOKEN).encode()}, {"error": httpx.ReadTimeout},
])
def test_rejected_repository_is_plain_422_without_persisting_credentials(h, monkeypatch, caplog, options):
    configure(h)
    repository_response(monkeypatch, **options)
    caplog.set_level(logging.INFO)
    rejected = connect(h, status=422)
    assert rejected["error"]["code"] == "INTEGRATION_GITHUB_REJECTED"
    assert "token" in rejected["error"]["message"].lower()
    assert TOKEN not in json.dumps(rejected) and TOKEN not in caplog.text
    assert saved(h) is None and not read(h)["connected"]
    with transaction(h.app.state.engine) as db:
        assert not list(db.scalars(select(m.CommandReceipt).where(m.CommandReceipt.command_kind == "integration.github.connect")))
        assert not list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "integration.github.connect")))


@pytest.mark.parametrize("payload", [
    {"repo": "https://attacker.invalid/repo", "token": TOKEN}, {"repo": "example/../escape", "token": TOKEN},
    {"repo": "example/repo?token=private", "token": TOKEN}, {"repo": REPO}, {"repo": REPO, "token": 123},
    {"repo": REPO, "token": "tiny"}, {"repo": REPO, "token": "a-token\ninvalid"},
    {"repo": REPO, "token": TOKEN, "reason": TOKEN}, {"repo": REPO, "token": TOKEN, "catalogue": TOKEN},
])
def test_connect_rejects_invalid_payload_without_echoing_or_saving_tokens(h, monkeypatch, caplog, payload):
    configure(h)
    calls = repository_response(monkeypatch)
    result = h.command("operator", "integration.github.connect", payload, status=422)
    assert TOKEN not in json.dumps(result) and TOKEN not in caplog.text
    assert saved(h) is None and not calls


def test_replacement_checks_revision_before_network_and_uses_new_ciphertext(h, monkeypatch):
    configure(h)
    calls = repository_response(monkeypatch)
    first = connect(h)
    previous = saved(h).token_ciphertext
    assert connect(h, OTHER_TOKEN, status=428)["error"]["code"] == "REVISION_REQUIRED"
    assert connect(h, OTHER_TOKEN, revision=first["revision"] + 1, status=409)["error"]["code"] == "STALE_REVISION"
    assert len(calls) == 1 and saved(h).token_ciphertext == previous
    replaced = connect(h, OTHER_TOKEN, repo="example/replacement", revision=first["revision"])
    assert replaced["revision"] == first["revision"] + 1 and replaced["token_hint"] == OTHER_TOKEN[-4:]
    assert replaced["repo"] == "example/replacement" and replaced["last_used_at"] is None
    assert saved(h).token_ciphertext != previous and github.decrypt(saved(h), h.settings) == OTHER_TOKEN
    connect(h, revision=first["revision"], status=409)
    h.command("operator", "integration.github.disconnect", {"revision": first["revision"]}, status=409)
    assert read(h)["revision"] == replaced["revision"]


def test_rejected_replacement_preserves_the_previous_connection(h, monkeypatch):
    configure(h)
    repository_response(monkeypatch)
    first = connect(h)
    ciphertext = saved(h).token_ciphertext
    repository_response(monkeypatch, status=401, body={"message": OTHER_TOKEN})
    connect(h, OTHER_TOKEN, revision=first["revision"], status=422)
    assert read(h) == {k: first[k] for k in STATUS_FIELDS}
    assert saved(h).token_ciphertext == ciphertext


def test_replay_is_idempotent_and_different_token_with_same_key_conflicts(h, monkeypatch):
    configure(h)
    calls = repository_response(monkeypatch)
    key = str(uuid.uuid4())
    first = connect(h, key=key)
    assert connect(h, key=key) == first
    changed = connect(h, OTHER_TOKEN, key=key, status=409)
    assert changed["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(calls) == 1 and read(h)["revision"] == first["revision"]


@pytest.mark.parametrize("header", ["2030-01-01T09:30:00Z", "Tue, 01 Jan 2030 12:30:00 +0300",
                                    "2030-01-01 09:30:00 UTC", "2030-01-01 12:30:00 +0300"])
def test_connect_and_test_record_expiry_success_failure_and_current_revision(h, monkeypatch, header):
    configure(h)
    repository_response(monkeypatch, headers={"github-authentication-token-expiration": header})
    first = connect(h)
    expiry = datetime(2030, 1, 1, 9, 30, tzinfo=timezone.utc)
    assert datetime.fromisoformat(first["token_expires_at"].replace("Z", "+00:00")) == expiry
    repository_response(monkeypatch, status=401, headers={"github-authentication-token-expiration": header})
    failed = h.command("operator", "integration.github.test", {})
    assert failed["last_test_ok"] is False and failed["last_tested_at"]
    assert failed["revision"] == first["revision"] + 1 and failed["token_expires_at"] == first["token_expires_at"]
    assert failed["connected"] and failed["source"] == "app"
    repository_response(monkeypatch, headers={"github-authentication-token-expiration": "invalid-date"})
    tested = h.command("operator", "integration.github.test", {})
    assert tested["last_test_ok"] is True and tested["token_expires_at"] == first["token_expires_at"]
    assert tested["revision"] == failed["revision"] + 1


@pytest.mark.parametrize("options,ok", [({"error": httpx.ReadTimeout}, False), ({"status": 401}, False), ({}, True)])
def test_test_preserves_stored_expiry_when_check_returns_none(h, monkeypatch, options, ok):
    configure(h)
    repository_response(monkeypatch, headers={"github-authentication-token-expiration": "2030-01-01T09:30:00Z"})
    first = connect(h)
    assert first["token_expires_at"] is not None
    stored_expiry = saved(h).token_expires_at
    repository_response(monkeypatch, **options)
    tested = h.command("operator", "integration.github.test", {})
    assert tested["last_test_ok"] is ok and tested["last_tested_at"]
    assert tested["revision"] == first["revision"] + 1
    assert tested["token_expires_at"] == first["token_expires_at"]
    assert read(h)["token_expires_at"] == first["token_expires_at"]
    assert saved(h).token_expires_at == stored_expiry


def test_test_requires_an_app_connection_and_disconnect_checks_payload_and_envelope(h, monkeypatch):
    configure(h, feedback_github_repo=REPO, feedback_github_token=TOKEN)
    calls = repository_response(monkeypatch)
    missing = h.command("operator", "integration.github.test", {}, status=409)
    assert missing["error"]["code"] == "INTEGRATION_NOT_CONNECTED" and not calls
    first = connect(h)
    h.command("operator", "integration.github.test", {"token": TOKEN}, status=422)
    for payload in ({}, {"revision": True}, {"revision": "1"}, {"revision": first["revision"], "token": TOKEN}):
        h.command("operator", "integration.github.disconnect", payload, status=422)
    h.command("operator", "integration.github.disconnect", {"revision": first["revision"]},
              revision=first["revision"] + 1, status=422)
    h.command("operator", "integration.github.disconnect", {"revision": first["revision"]}, target=str(uuid.uuid4()), status=422)
    assert read(h)["revision"] == first["revision"] and len(calls) == 1


@pytest.mark.parametrize("fallback", [False, True])
def test_disconnect_removes_ciphertext_retains_evidence_and_reveals_optional_fallback(h, monkeypatch, fallback):
    configure(h, feedback_github_repo=REPO if fallback else "", feedback_github_token=OTHER_TOKEN if fallback else "")
    repository_response(monkeypatch)
    report_id = report(h)
    first = connect(h)
    key = str(uuid.uuid4())
    disconnected = h.command("operator", "integration.github.disconnect", {"revision": first["revision"]}, key=key)
    assert saved(h) is None and disconnected["connected"] is fallback
    assert disconnected["source"] == ("env" if fallback else None) and disconnected["revision"] is None
    assert disconnected["token_hint"] == (OTHER_TOKEN[-4:] if fallback else None)
    assert export(h, report_id)["configured"] is fallback
    assert h.command("operator", "integration.github.disconnect", {"revision": first["revision"]}, key=key) == disconnected
    posts = []

    def post(url, **options):
        posts.append((url, options))
        return httpx.Response(201, json={"html_url": "https://github.com/" + REPO + "/issues/11"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    assert export(h, report_id, create=True)["configured"] is fallback
    assert len(posts) == int(fallback)
    if fallback:
        assert posts[0][1]["headers"]["Authorization"] == "Bearer " + OTHER_TOKEN
    with transaction(h.app.state.engine) as db:
        actions = list(db.scalars(select(m.AuditEvent.action).where(m.AuditEvent.action.like("integration.github.%"))))
        assert actions.count("integration.github.connect") == actions.count("integration.github.disconnect") == 1


def test_reconnecting_cannot_reuse_a_revoked_credentials_revision(h, monkeypatch):
    configure(h)
    repository_response(monkeypatch)
    first = connect(h)
    h.command("operator", "integration.github.disconnect", {"revision": first["revision"]})
    connect(h, OTHER_TOKEN, revision=first["revision"], status=409)
    reconnected = connect(h, OTHER_TOKEN)
    assert reconnected["revision"] > first["revision"]
    h.command("operator", "integration.github.disconnect", {"revision": first["revision"]}, status=409)
    connect(h, revision=first["revision"], status=409)
    assert read(h)["revision"] == reconnected["revision"]


def test_export_prefers_app_token_and_marks_only_real_successful_issue_use(h, monkeypatch):
    configure(h, feedback_github_repo="fallback/repository", feedback_github_token=OTHER_TOKEN)
    repository_response(monkeypatch)
    connect(h)
    report_id = report(h)
    calls = []

    def post(url, **options):
        calls.append((url, options))
        return httpx.Response(201, json={"html_url": "https://github.com/" + REPO + "/issues/7"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    assert export(h, report_id)["configured"] and not calls and read(h)["last_used_at"] is None
    created = export(h, report_id, create=True)
    assert created["github_issue_url"] == "https://github.com/" + REPO + "/issues/7"
    assert read(h)["last_used_at"] and len(calls) == 1
    assert calls[0][0] == "https://api.github.com/repos/" + REPO + "/issues"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer " + TOKEN
    assert TOKEN not in json.dumps(created) and OTHER_TOKEN not in json.dumps(created)
    export(h, report_id, create=True)
    assert len(calls) == 1


def test_real_issue_403_returns_plain_issues_permission_message_without_private_body(h, monkeypatch, caplog):
    configure(h)
    repository_response(monkeypatch)
    connect(h)
    report_id = report(h)
    monkeypatch.setattr(httpx, "post", lambda url, **options: httpx.Response(
        403, json={"message": TOKEN + " upstream private body"}, request=httpx.Request("POST", url)))
    caplog.set_level(logging.INFO)
    rejected = export(h, report_id, create=True, status=422)
    assert rejected["error"]["code"] == "FEEDBACK_GITHUB_FORBIDDEN"
    assert "Issues write" in rejected["error"]["message"]
    assert TOKEN not in json.dumps(rejected) and "upstream private body" not in json.dumps(rejected)
    assert TOKEN not in caplog.text and read(h)["last_used_at"] is None
    assert h.get("operator", "/api/feedback/" + report_id)["github_issue_url"] is None


@pytest.mark.parametrize("key,code", [("", "INTEGRATION_KEY_MISSING"), ("29" * 32, "INTEGRATION_KEY_INVALID")])
def test_missing_or_changed_key_does_not_break_preview_and_cannot_silently_use_env_token(h, monkeypatch, key, code):
    configure(h, feedback_github_repo="fallback/repository", feedback_github_token=OTHER_TOKEN)
    repository_response(monkeypatch)
    first = connect(h)
    report_id = report(h)
    preview = export(h, report_id)
    ciphertext = saved(h).token_ciphertext
    configure(h, fmuc_data_key=key)
    current = read(h)
    assert current["connected"] and current["source"] == "app" and current["key_configured"] is bool(key)
    assert current["revision"] == first["revision"]
    copied = export(h, report_id)
    assert copied["configured"] and copied["title"] == preview["title"] and copied["body"] == preview["body"]
    assert copied["github_issue_url"] is None and read(h)["last_used_at"] is None
    assert saved(h).token_ciphertext == ciphertext
    assert h.command("operator", "integration.github.test", {}, status=409)["error"]["code"] == code
    assert export(h, report_id, create=True, status=409)["error"]["code"] == code
    assert read(h)["revision"] == first["revision"]
    # Revocation does not require decryption, so loss of the data key does not trap the credential.
    configure(h, fmuc_data_key="")
    assert h.command("operator", "integration.github.disconnect", {"revision": first["revision"]})["source"] == "env"


def test_authenticated_encryption_binds_ciphertext_to_department_and_kind(h, monkeypatch):
    configure(h)
    repository_response(monkeypatch)
    connect(h)
    row = saved(h)
    row.department_id = str(uuid.uuid4())
    with pytest.raises(Problem) as wrong_department:
        github.decrypt(row, h.settings)
    assert wrong_department.value.code == "INTEGRATION_KEY_INVALID" and TOKEN not in str(wrong_department.value)
    row = saved(h)
    row.kind = "another_integration"
    with pytest.raises(Problem) as wrong_kind:
        github.decrypt(row, h.settings)
    assert wrong_kind.value.code == "INTEGRATION_KEY_INVALID"
    row = saved(h)
    row.token_ciphertext = row.token_ciphertext[:-1] + bytes([row.token_ciphertext[-1] ^ 1])
    with pytest.raises(Problem) as damaged:
        github.decrypt(row, h.settings)
    assert damaged.value.code == "INTEGRATION_KEY_INVALID"


def test_connect_and_test_share_department_hourly_limit_including_rejections(h, monkeypatch):
    configure(h, session_idle_minutes=480)
    moment = freeze_clock(monkeypatch)
    calls = repository_response(monkeypatch)
    first = connect(h)
    identity(h, "second_developer", ["operator"])
    for index in range(9):
        h.command("operator" if index % 2 else "second_developer", "integration.github.test", {})
    client = h.client("second_developer")
    blocked = client.post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())},
                          json={"kind": "integration.github.connect", "payload": {"repo": REPO, "token": TOKEN},
                                "expected_revision": read(h)["revision"]})
    assert blocked.status_code == 429 and len(calls) == 10
    assert blocked.json()["error"]["code"] == "RATE_LIMITED" and 1 <= int(blocked.headers["retry-after"]) <= 3600
    freeze_clock(monkeypatch, moment + timedelta(hours=1, seconds=1))
    repository_response(monkeypatch, status=401)
    for _ in range(10):
        connect(h, revision=read(h)["revision"], status=422)
    assert h.command("operator", "integration.github.test", {}, status=429)["error"]["code"] == "RATE_LIMITED"
    assert read(h)["revision"] == first["revision"] + 9


@pytest.mark.parametrize("harness", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("kind", ["connect", "test"])
def test_slow_repository_check_does_not_block_an_ordinary_command(harness, kind, request, monkeypatch):
    h = request.getfixturevalue(harness)
    configure(h)
    repository_response(monkeypatch)
    if kind == "test":
        connect(h)
    h.client("operator")
    h.client("coordinator")
    entered, release = Event(), Event()

    def slow_get(url, **options):
        entered.set()
        assert release.wait(30), "Test did not release the GitHub response"
        return httpx.Response(200, json={"has_issues": True}, request=httpx.Request("GET", url))

    monkeypatch.setattr(github.httpx, "get", slow_get)
    with ThreadPoolExecutor(max_workers=2) as pool:
        remote = pool.submit(connect, h) if kind == "connect" else pool.submit(h.command, "operator", "integration.github.test", {})
        try:
            assert entered.wait(10), "GitHub request did not start"
            ordinary = pool.submit(h.workspace, "Synthetic command during GitHub check")
            assert ordinary.result(timeout=10)["name"] == "Synthetic command during GitHub check"
            assert not remote.done() and not release.is_set()
        finally:
            release.set()
        assert remote.result(timeout=10)["last_test_ok"] is True


@pytest.mark.parametrize("harness", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("replace", [False, True])
def test_credential_change_before_token_access_keeps_preflight_coherent(harness, replace, request, monkeypatch):
    h = request.getfixturevalue(harness)
    configure(h)
    calls = repository_response(monkeypatch)
    first = connect(h)
    calls.clear()
    entered, release = Event(), Event()

    def hold_credential(db, row):
        if isinstance(row, m.IntegrationCredential) and not entered.is_set():
            # Metadata is materialized here, before decrypt can load deferred ciphertext.
            entered.set()
            assert release.wait(30), "Test did not release the credential read"

    key = str(uuid.uuid4())
    event.listen(Session, "loaded_as_persistent", hold_credential)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(h.command, "operator", "integration.github.test", {}, key=key, status=409)
            try:
                assert entered.wait(10), "Credential metadata was not read"
                if replace:
                    connect(h, OTHER_TOKEN, repo="another/feedback", revision=first["revision"])
                else:
                    h.command("operator", "integration.github.disconnect", {"revision": first["revision"]})
                after_change = read(h)
                ciphertext = saved(h).token_ciphertext if replace else None
            finally:
                release.set()
            result = pending.result(timeout=10)
    finally:
        event.remove(Session, "loaded_as_persistent", hold_credential)

    assert result["error"]["code"] == "STALE_REVISION"
    assert calls[-1][0] == "https://api.github.com/repos/" + REPO
    assert calls[-1][1]["headers"]["Authorization"] == "Bearer " + TOKEN
    assert read(h) == after_change
    if replace:
        assert saved(h).token_ciphertext == ciphertext
    else:
        assert saved(h) is None
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.CommandReceipt.id).where(m.CommandReceipt.key == key)) is None
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == "integration.github.test")) is None


@pytest.mark.parametrize("harness", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("reconnect", [False, True])
def test_in_flight_check_cannot_update_a_replaced_or_reconnected_credential(harness, reconnect, request, monkeypatch):
    h = request.getfixturevalue(harness)
    configure(h)
    repository_response(monkeypatch)
    first = connect(h)
    get = github.httpx.get
    entered, release = Event(), Event()

    def slow_get(url, **options):
        if options["headers"]["Authorization"] == "Bearer " + TOKEN:
            entered.set()
            assert release.wait(30), "Test did not release the old credential's response"
        return get(url, **options)

    monkeypatch.setattr(github.httpx, "get", slow_get)
    with ThreadPoolExecutor(max_workers=2) as pool:
        remote = pool.submit(h.command, "operator", "integration.github.test", {}, status=409)
        try:
            assert entered.wait(10)
            if reconnect:
                h.command("operator", "integration.github.disconnect", {"revision": first["revision"]})
            connect(h, OTHER_TOKEN, revision=None if reconnect else first["revision"])
            replacement = read(h)
            assert not remote.done()
        finally:
            release.set()
        assert remote.result(timeout=10)["error"]["code"] == "STALE_REVISION"
    assert read(h) == replacement


@pytest.mark.parametrize("harness", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_in_flight_check_reauthenticates_before_writing(harness, request, monkeypatch):
    h = request.getfixturevalue(harness)
    configure(h)
    repository_response(monkeypatch)
    first = connect(h)
    entered, release = Event(), Event()

    def slow_get(url, **options):
        entered.set()
        assert release.wait(30)
        return httpx.Response(200, json={"has_issues": True}, request=httpx.Request("GET", url))

    monkeypatch.setattr(github.httpx, "get", slow_get)
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        remote = pool.submit(h.command, "operator", "integration.github.test", {}, key=key, status=401)
        try:
            assert entered.wait(10)
            with transaction(h.app.state.engine, write=True) as db:
                for session in db.scalars(select(m.Session).where(m.Session.identity_id == h.seed["identities"]["operator"])):
                    session.revoked = True
        finally:
            release.set()
        assert remote.result(timeout=10)["error"]["code"] == "SESSION_EXPIRED"
    assert saved(h).revision == first["revision"]
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.CommandReceipt.id).where(m.CommandReceipt.key == key)) is None


@pytest.mark.parametrize("kind", ["connect", "test", "disconnect"])
def test_integration_write_conflicts_are_not_retried(h, kind, monkeypatch):
    configure(h)
    calls = repository_response(monkeypatch)
    first = connect(h) if kind != "connect" else None
    calls.clear()
    commits = []

    def fail_commit(db):
        if db.info.get("receipt_id"):
            commits.append(db.info["receipt_id"])
            raise OperationalError("COMMIT", {}, Exception("database is locked"))

    event.listen(Session, "before_commit", fail_commit)
    try:
        payload = {"repo": REPO, "token": TOKEN} if kind == "connect" else {}
        if kind == "disconnect":
            payload = {"revision": first["revision"]}
        result = h.command("operator", "integration.github." + kind, payload, status=503)
    finally:
        event.remove(Session, "before_commit", fail_commit)
    assert result["error"]["code"] == "COMMAND_WRITE_TIMEOUT"
    assert len(commits) == 1 and len(calls) == int(kind != "disconnect")
    if kind == "connect":
        assert saved(h) is None
    else:
        assert saved(h).revision == first["revision"]


@pytest.mark.parametrize("harness", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_replay_committed_during_check_preparation_skips_revision_validation_and_http(harness, request, monkeypatch):
    from apps.api import rate_limit
    h = request.getfixturevalue(harness)
    configure(h)
    calls = repository_response(monkeypatch)
    h.client("operator")
    entered, release = Event(), Event()
    enforce = rate_limit.enforce

    def held_limit(engine, scope, *args, **kwargs):
        if scope == "integration-github-check" and not entered.is_set():
            entered.set()
            assert release.wait(30), "Test did not release the second request's preparation"
        return enforce(engine, scope, *args, **kwargs)

    monkeypatch.setattr(rate_limit, "enforce", held_limit)
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(connect, h, key=key)
        try:
            assert entered.wait(10)
            first = connect(h, key=key)
        finally:
            release.set()
        assert pending.result(timeout=10) == first
    assert len(calls) == 1


def test_maintenance_batch_keeps_direct_dispatch_and_single_attempt(h, monkeypatch):
    from apps.api import db
    from apps.api.settings import Settings
    from scripts import system_command
    configure(h)
    calls = repository_response(monkeypatch)
    monkeypatch.setattr(Settings, "from_env", lambda: h.settings)
    monkeypatch.setattr(db, "make_engine", lambda *args, **kwargs: h.app.state.engine)
    retry_write = db.retry_write
    attempts = []

    def write(engine, operation, **options):
        attempts.append(options["attempts"])
        return retry_write(engine, operation, **options)

    monkeypatch.setattr(db, "retry_write", write)
    connected, tested = system_command.run([
        {"kind": "integration.github.connect", "payload": {"repo": REPO, "token": TOKEN}},
        {"kind": "integration.github.test", "payload": {}},
    ], "synthetic-maintenance")
    assert connected["connected"] and tested["last_test_ok"]
    assert tested["revision"] == connected["revision"] + 1
    assert len(calls) == 2 and attempts == [1]


@pytest.mark.parametrize("envelope,code", [
    ({"target_id": "00000000-0000-0000-0000-000000000001"}, "INTEGRATION_TARGET"),
    ({"expected_fingerprint": "a" * 64}, "UNSUPPORTED_PRECONDITION"),
])
def test_invalid_check_envelope_is_rejected_before_network(h, monkeypatch, envelope, code):
    configure(h)
    calls = repository_response(monkeypatch)
    response = h.client("operator").post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())},
        json={"kind": "integration.github.connect", "payload": {"repo": REPO, "token": TOKEN}, **envelope})
    assert response.status_code == 422 and response.json()["error"]["code"] == code
    assert not calls and saved(h) is None


@pytest.mark.postgres
def test_postgres_credentials_are_independent_per_department_through_revoke_and_reconnect(pg, monkeypatch):
    configure(pg)
    repository_response(monkeypatch)
    first = connect(pg)
    with transaction(pg.app.state.engine, write=True) as db:
        department = db.get(m.Department, pg.seed["department_id"])
        other = m.Department(organisation_id=department.organisation_id, name="Fictional independent department")
        db.add(other)
        db.flush()
        department_id = other.id
    identity(pg, "other_developer", ["operator"], department=department_id)
    assert not read(pg, "other_developer")["connected"]
    pg.command("other_developer", "integration.github.test", {}, status=409)
    pg.command("other_developer", "integration.github.disconnect", {"revision": first["revision"]}, status=409)
    second = connect(pg, OTHER_TOKEN, repo="another/feedback", role="other_developer")
    assert second["repo"] == "another/feedback" and read(pg)["repo"] == REPO
    with transaction(pg.app.state.engine) as db:
        rows = list(db.scalars(select(m.IntegrationCredential)))
        assert len(rows) == 2 and {row.department_id for row in rows} == {pg.seed["department_id"], department_id}
        assert all(TOKEN.encode() not in row.token_ciphertext and OTHER_TOKEN.encode() not in row.token_ciphertext for row in rows)
    pg.command("operator", "integration.github.disconnect", {"revision": first["revision"]})
    assert not read(pg)["connected"] and read(pg, "other_developer")["repo"] == "another/feedback"
    reconnected = connect(pg)
    assert reconnected["revision"] > first["revision"]
    assert read(pg, "other_developer")["revision"] == second["revision"]

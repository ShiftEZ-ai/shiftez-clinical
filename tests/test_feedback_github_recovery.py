"""External issue attempts survive uncertainty without another POST or a database lock."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
import uuid
from datetime import date

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.db import DatabaseWriteTimeout, transaction
from apps.api.errors import Problem
from apps.api.services import feedback
from scripts import system_command

REPO = "example/fictional-feedback"
TOKEN = "fictional-opaque-export-credential"
ISSUE = f"https://github.com/{REPO}/issues/42"


def setup(h, message="Synthetic feedback"):
    object.__setattr__(h.settings, "feedback_github_repo", REPO)
    object.__setattr__(h.settings, "feedback_github_token", TOKEN)
    return h.command("staff", "feedback.create", {"kind": "problem", "message": message, "route": "#planner"})["id"]


def export(h, report_id, *, status=200, key=None):
    return h.command("operator", "feedback.export_github", {"id": report_id, "create_issue": True}, status=status, key=key)


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("failure", ["response", "commit", "budget", "server"])
def test_uncertain_then_retry_recovers_marked_issue_without_a_second_post(request, database, failure, monkeypatch):
    h = request.getfixturevalue(database)
    report_id = setup(h)
    key = str(uuid.uuid4())
    posted = []

    def post(url, **options):
        posted.append(options["json"])
        assert feedback.github_marker(report_id) in posted[0]["body"]
        with transaction(h.app.state.engine) as db:
            assert db.get(m.FeedbackReport, report_id).github_export_repo == REPO
        if failure == "response":
            raise httpx.ReadTimeout("private upstream failure " + TOKEN)
        if failure == "server":
            return httpx.Response(503, json={"message": "private upstream failure " + TOKEN},
                                  request=httpx.Request("POST", url))
        return httpx.Response(201, json={"html_url": ISSUE}, request=httpx.Request("POST", url))

    def fail_commit(db):
        if db.info.get("receipt_id"):
            if failure == "budget":
                raise DatabaseWriteTimeout()
            raise OperationalError("COMMIT", {}, Exception("database is locked"))

    monkeypatch.setattr(httpx, "post", post)
    if failure in {"commit", "budget"}:
        event.listen(Session, "before_commit", fail_commit)
    try:
        failed = export(h, report_id, key=key, status=502)
    finally:
        if failure in {"commit", "budget"}:
            event.remove(Session, "before_commit", fail_commit)
    assert failed["error"]["code"] == "FEEDBACK_GITHUB_UNCERTAIN" and TOKEN not in str(failed)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.FeedbackReport, report_id)
        assert row.github_issue_url is None and row.github_export_repo == REPO
        assert db.scalar(select(m.CommandReceipt.id).where(m.CommandReceipt.key == key)) is None

    def get(url, **options):
        assert url == f"https://api.github.com/repos/{REPO}/issues"
        assert options["params"]["state"] == "all" and options["headers"]["Authorization"] == "Bearer " + TOKEN
        return httpx.Response(200, json=[{"html_url": ISSUE, "body": posted[0]["body"], "state": "closed"}],
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    recovered = export(h, report_id, key=key)
    assert recovered["github_issue_url"] == ISSUE and len(posted) == 1
    assert export(h, report_id, key=key) == recovered
    assert h.get("operator", "/api/feedback/" + report_id)["github_issue_url"] == ISSUE
    assert "github_export_repo" not in h.get("operator", "/api/feedback/" + report_id)
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.AuditEvent.id).where(
            m.AuditEvent.action == "feedback.export_github", m.AuditEvent.target_id == report_id)))) == 1


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_reservation_write_timeout_is_not_reported_as_a_remote_attempt(request, database, monkeypatch):
    h = request.getfixturevalue(database)
    report_id = setup(h)
    h.client("operator")
    key = str(uuid.uuid4())
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("An uncommitted reservation cannot send an issue"))

    def fail_reservation(db):
        # Flush has cleared db.dirty; inspect this transaction's reservation before commit.
        row = db.get(m.FeedbackReport, report_id)
        if row is not None and row.github_export_repo:
            raise DatabaseWriteTimeout()

    event.listen(Session, "before_commit", fail_reservation)
    try:
        failed = export(h, report_id, key=key, status=503)
    finally:
        event.remove(Session, "before_commit", fail_reservation)
    assert failed["error"]["code"] == "COMMAND_WRITE_TIMEOUT" and TOKEN not in str(failed)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.FeedbackReport, report_id).github_export_repo is None
        assert db.scalar(select(m.CommandReceipt.id).where(m.CommandReceipt.key == key)) is None


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_slow_issue_creation_allows_unrelated_writes_and_concurrent_retry_cannot_post(request, database, monkeypatch):
    h = request.getfixturevalue(database)
    report_id = setup(h)
    h.client("operator")
    h.client("coordinator")
    entered, release = Event(), Event()
    posts = []

    def create(*args):
        posts.append(args)
        entered.set()
        assert release.wait(30), "Synthetic GitHub response was not released"
        return ISSUE

    monkeypatch.setattr(feedback, "create_github_issue", create)
    monkeypatch.setattr(httpx, "get", lambda url, **options: httpx.Response(
        200, json=[], request=httpx.Request("GET", url)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(export, h, report_id)
        try:
            assert entered.wait(10), "Synthetic GitHub request did not start"
            ordinary = pool.submit(h.workspace, "Synthetic command during issue creation")
            assert ordinary.result(timeout=10)["name"] == "Synthetic command during issue creation"
            assert export(h, report_id, status=502)["error"]["code"] == "FEEDBACK_GITHUB_UNCERTAIN"
        finally:
            release.set()
        assert pending.result(timeout=10)["github_issue_url"] == ISSUE
    assert len(posts) == 1


@pytest.mark.parametrize("field,value,code", [("target", "00000000-0000-0000-0000-000000000001", "FEEDBACK_TARGET"),
                                             ("revision", 1, "FEEDBACK_REVISION")])
def test_invalid_envelope_cannot_reserve_or_send_an_issue(h, monkeypatch, field, value, code):
    report_id = setup(h)
    monkeypatch.setattr(feedback, "create_github_issue", lambda *args: pytest.fail("Invalid request contacted GitHub"))
    rejected = h.command("operator", "feedback.export_github", {"id": report_id, "create_issue": True},
                         **{field: value}, status=422)
    assert rejected["error"]["code"] == code
    with transaction(h.app.state.engine) as db:
        assert db.get(m.FeedbackReport, report_id).github_export_repo is None


def test_credential_rotation_during_post_keeps_the_sent_sanitized_body(h, monkeypatch):
    report_id = setup(h, "Synthetic diagnostics " + TOKEN)
    sent = []

    def create(repo, token, title, body):
        assert TOKEN not in body
        sent.append(body)
        object.__setattr__(h.settings, "feedback_github_token", "replacement-opaque-credential")
        return ISSUE

    monkeypatch.setattr(feedback, "create_github_issue", create)
    result = h.command("operator", "feedback.export_github", {"id": report_id, "create_issue": True, "include_message": True})
    assert result["body"] == sent[0] and TOKEN not in str(result)
    with transaction(h.app.state.engine) as db:
        assert TOKEN not in str(db.get(m.CommandReceipt, result["receipt_id"]).result)


@pytest.mark.parametrize("status", [401, 403, 404, 410, 422, 429])
def test_definitive_rejection_allows_a_corrected_request_but_uncertainty_does_not(h, monkeypatch, status):
    report_id = setup(h)
    calls = []
    key = str(uuid.uuid4())

    def post(url, **options):
        calls.append(options)
        return httpx.Response(status if len(calls) == 1 else 201,
                              json={"message": "private upstream failure " + TOKEN} if len(calls) == 1 else {"html_url": ISSUE},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: pytest.fail("A definite rejection must allow another POST"))
    rejected = export(h, report_id, status=422, key=key)
    assert rejected["error"]["code"] == ("FEEDBACK_GITHUB_FORBIDDEN" if status == 403 else "FEEDBACK_GITHUB_REJECTED")
    assert TOKEN not in str(rejected) and "private upstream failure" not in str(rejected)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.FeedbackReport, report_id)
        assert row.github_export_repo is None and row.github_issue_url is None
        assert db.scalar(select(m.CommandReceipt.id).where(m.CommandReceipt.key == key)) is None
    corrected_token = "corrected-opaque-export-credential"
    object.__setattr__(h.settings, "feedback_github_token", corrected_token)
    assert export(h, report_id, key=key)["github_issue_url"] == ISSUE
    assert export(h, report_id, key=key)["github_issue_url"] == ISSUE and len(calls) == 2
    assert [call["headers"]["Authorization"] for call in calls] == ["Bearer " + TOKEN, "Bearer " + corrected_token]


@pytest.mark.parametrize("status", [401, 404])
def test_recovery_read_rejection_keeps_the_uncertain_reservation(h, monkeypatch, status):
    report_id = setup(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.FeedbackReport, report_id).github_export_repo = REPO
    monkeypatch.setattr(httpx, "get", lambda url, **options: httpx.Response(
        status, request=httpx.Request("GET", url)))
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: pytest.fail("Recovery must not send another POST"))
    assert export(h, report_id, status=502)["error"]["code"] == "FEEDBACK_GITHUB_UNCERTAIN"
    with transaction(h.app.state.engine) as db:
        row = db.get(m.FeedbackReport, report_id)
        assert row.github_export_repo == REPO and row.github_issue_url is None


def test_replacing_credential_during_post_does_not_mark_the_replacement_used(h, monkeypatch):
    from apps.api.services import github_integrations
    report_id = setup(h)
    object.__setattr__(h.settings, "fmuc_data_key", "17" * 32)
    monkeypatch.setattr(github_integrations, "check_repository", lambda *args: (True, None))
    first = h.command("operator", "integration.github.connect", {"repo": REPO, "token": TOKEN})

    def create(*args):
        h.command("operator", "integration.github.connect", {"repo": "another/feedback", "token": "replacement-opaque-token"},
                  revision=first["revision"])
        return ISSUE

    monkeypatch.setattr(feedback, "create_github_issue", create)
    assert export(h, report_id)["github_issue_url"] == ISSUE
    replacement = h.get("operator", "/api/integrations/github")
    assert replacement["repo"] == "another/feedback" and replacement["last_used_at"] is None


@pytest.mark.parametrize("change,status", [("session", 401), ("role", 403), ("department", 409)])
def test_export_rechecks_access_and_department_before_saving_remote_outcome(h, monkeypatch, change, status):
    report_id = setup(h)
    h.client("operator")
    identity_id = h.seed["identities"]["operator"]

    def create(*args):
        with transaction(h.app.state.engine, write=True) as db:
            session = db.scalars(select(m.Session).where(m.Session.identity_id == identity_id)).one()
            if change == "session":
                session.revoked = True
            elif change == "role":
                for role in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == identity_id)):
                    role.revoked = True
            else:
                other = m.Department(organisation_id=h.seed["organisation_id"], name="Synthetic other export department")
                db.add(other)
                db.flush()
                db.add(m.RoleBinding(identity_id=identity_id, department_id=other.id, role="operator", starts_on=date(2020, 1, 1)))
                session.department_id = other.id
        return ISSUE

    monkeypatch.setattr(feedback, "create_github_issue", create)
    rejected = export(h, report_id, status=status)
    assert rejected["error"]["code"] == {"session": "SESSION_EXPIRED", "role": "FORBIDDEN",
                                         "department": "INTEGRATION_CONTEXT_CHANGED"}[change]
    with transaction(h.app.state.engine) as db:
        row = db.get(m.FeedbackReport, report_id)
        assert row.github_export_repo == REPO and row.github_issue_url is None
        assert not list(db.scalars(select(m.CommandReceipt.id).where(m.CommandReceipt.command_kind == "feedback.export_github")))


def test_maintenance_cannot_send_issues_under_its_atomic_write(h):
    report_id = setup(h)
    with pytest.raises(Problem) as rejected:
        with transaction(h.app.state.engine, write=True) as db:
            identity = system_command.maintenance_identity(db, m, select)
            department = db.get(m.Department, h.seed["department_id"])
            system_command.apply(db, identity, department, [{"kind": "feedback.export_github", "payload": {
                "id": report_id, "create_issue": True}}], "synthetic-maintenance-export", h.settings)
    assert rejected.value.code == "FEEDBACK_GITHUB_PREPARE_REQUIRED"


def test_recovery_pages_raw_closed_issues_skips_pull_requests_and_never_follows_links(monkeypatch):
    report_id = str(uuid.uuid4())
    marker = feedback.github_marker(report_id)
    pages = []

    def get(url, **options):
        assert url == f"https://api.github.com/repos/{REPO}/issues"
        pages.append(options["params"]["page"])
        rows = ([{"pull_request": {}, "body": marker, "html_url": ISSUE}] * 100 if len(pages) == 1 else
                [{"body": marker, "html_url": ISSUE}])
        return httpx.Response(200, json=rows, headers={"Link": '<https://attacker.invalid>; rel="next"'},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    assert feedback.find_github_issue(REPO, TOKEN, report_id) == ISSUE and pages == [1, 2]


@pytest.mark.parametrize("rows", [[], [{"body": "unrelated"}], [{"body": "MARKER", "html_url": "https://attacker.invalid"}],
                                  {}, [None], [{"body": 12}], [{"body": "MARKER", "html_url": None}]])
def test_unconfirmed_recovery_is_plain_and_never_creates_an_issue(monkeypatch, rows):
    report_id = str(uuid.uuid4())
    rows = [{**row, "body": feedback.github_marker(report_id)} if isinstance(row, dict) and row.get("body") == "MARKER"
             else row for row in rows] if isinstance(rows, list) else rows
    monkeypatch.setattr(httpx, "get", lambda url, **options: httpx.Response(
        200, json=rows, request=httpx.Request("GET", url)))
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: pytest.fail("Recovery must not POST"))
    with pytest.raises(Problem) as rejected:
        feedback.find_github_issue(REPO, TOKEN, report_id)
    assert rejected.value.code == "FEEDBACK_GITHUB_UNCERTAIN" and TOKEN not in str(rejected.value)



@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def in_flight_issue_creation(request, monkeypatch):
    h = request.getfixturevalue(request.param)
    report_id = setup(h)
    h.client("operator")
    h.client("coordinator")
    posts, timings = [], []

    def create(*args):
        posts.append(args)
        # These requests must finish before GitHub returns, while the original export is still in flight.
        started = time.perf_counter()
        ordinary = h.workspace("Synthetic command during issue creation")
        timings.append(time.perf_counter() - started)
        assert ordinary["name"] == "Synthetic command during issue creation"
        assert export(h, report_id, status=502)["error"]["code"] == "FEEDBACK_GITHUB_UNCERTAIN"
        return ISSUE

    monkeypatch.setattr(feedback, "create_github_issue", create)
    monkeypatch.setattr(httpx, "get", lambda url, **options: httpx.Response(
        200, json=[], request=httpx.Request("GET", url)))
    result = export(h, report_id)
    return result, posts, timings[0]


@pytest.mark.wall_benchmark
def test_unrelated_write_during_issue_creation_wall_budget(in_flight_issue_creation):
    assert in_flight_issue_creation[-1] < 10

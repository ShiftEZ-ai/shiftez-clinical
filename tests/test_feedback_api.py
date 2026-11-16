"""Feedback stays private, bounded and auditable throughout submission and triage."""
import base64
from datetime import timedelta
import json
import struct
import uuid
import zlib

from fastapi.testclient import TestClient
import httpx
import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from tests.outbox_support import process_one_after_undo as process_one
from packages.contracts.schemas import FEEDBACK_KINDS, FEEDBACK_STATUSES, PERMISSIONS, ROLE_NAMES
from .clock_support import freeze_clock
from .governance_support import identity


def png(size=None):
    """A real one-pixel PNG, optionally padded with a valid ancillary text chunk."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    raw += chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\xff"))
    end = chunk(b"IEND", b"")
    if size is not None:
        raw += chunk(b"tEXt", b"Padding\x00" + b"x" * (size - len(raw) - len(end) - 20))
    return raw + end


PNG = png()
WEBP = base64.b64decode("UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA")


def screenshot(raw=PNG, media_type="image/png"):
    return {"data": base64.b64encode(raw).decode("ascii"), "media_type": media_type}


def submit(h, role="staff", status=200, **values):
    payload = {"kind": "problem", "message": "A fictional action failed", "route": "#planner?date=2026-10-04", **values}
    response = h.client(role).post("/api/feedback", json=payload, headers={"idempotency-key": str(uuid.uuid4())})
    assert response.status_code == status, response.text
    return response.json()


def detail(h, report, role="operator"):
    return h.get(role, "/api/feedback/" + (report if isinstance(report, str) else report["id"]))


def test_submission_receipts_replay_without_an_occurrence_or_hourly_charge(h):
    client = h.client("staff")
    body = {"kind": "question", "message": "A synthetic question", "route": "#home"}
    assert client.post("/api/feedback", json=body).status_code == 428
    key = str(uuid.uuid4())
    headers = {"idempotency-key": key}
    first = client.post("/api/feedback", json=body, headers=headers)
    assert first.status_code == 200
    assert set(first.json()) == {"id", "matched_report_id"}
    for _ in range(11):
        repeated = client.post("/api/feedback", json=body, headers=headers)
        assert repeated.status_code == 200 and repeated.json() == first.json()
    assert client.post("/api/feedback", json={**body, "message": "Changed"}, headers=headers).status_code == 409
    assert h.get("staff", "/api/feedback")["total"] == 1
    assert detail(h, first.json())["occurrences"] == 1
    receipt = h.get("staff", "/api/commands/" + key)
    assert receipt["result"]["id"] == first.json()["id"]


def test_concurrent_replays_create_one_report(h):
    from concurrent.futures import ThreadPoolExecutor
    client = h.client("staff")
    headers = {"idempotency-key": str(uuid.uuid4())}
    body = {"kind": "suggestion", "message": "Synthetic suggestion", "route": "#home"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post("/api/feedback", json=body, headers=headers), range(2)))
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json() == responses[1].json()
    assert h.get("staff", "/api/feedback")["total"] == 1


def test_command_envelope_cannot_misstate_feedback_target_or_revision(h):
    row = detail(h, submit(h))
    h.command("operator", "feedback.update", {"id": row["id"], "revision": row["revision"], "status": "fixed"},
              target=str(uuid.uuid4()), status=422)
    h.command("operator", "feedback.update", {"id": row["id"], "revision": row["revision"], "status": "fixed"},
              revision=row["revision"] + 1, status=422)
    assert detail(h, row)["revision"] == row["revision"]


def test_remote_issue_creation_is_not_retried_after_a_transient_commit_failure(h, monkeypatch):
    from apps.api.services import feedback
    from sqlalchemy.exc import OperationalError
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    object.__setattr__(h.settings, "feedback_github_token", "fictional-token")
    object.__setattr__(h.settings, "feedback_github_repo", "example/feedback")
    row = submit(h)
    client = h.client("operator")
    calls = []
    def create(*args):
        calls.append(args)
        def fail_commit(db):
            raise OperationalError("COMMIT", {}, Exception("database is locked"))
        event.listen(Session, "before_commit", fail_commit, once=True)
        return "https://github.com/example/feedback/issues/1"
    monkeypatch.setattr(feedback, "create_github_issue", create)
    response = client.post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())},
                           json={"kind": "feedback.export_github", "payload": {"id": row["id"], "create_issue": True}})
    assert response.status_code == 502 and len(calls) == 1
    assert response.json()["error"]["code"] == "FEEDBACK_GITHUB_UNCERTAIN"
    assert "Check the configured repository" in response.json()["error"]["message"]
    assert "Retry with the same" not in response.text
    assert detail(h, row)["github_issue_url"] is None


def update(h, report, role="operator", http_status=200, **values):
    return h.command(role, "feedback.update", {"id": report["id"], "revision": report["revision"], **values}, status=http_status)


def notes(h, role="staff"):
    return [n for n in h.get(role, "/api/notifications")["notifications"] if n["kind"] == "feedback"]


@pytest.mark.parametrize("role", ROLE_NAMES)
def test_every_signed_in_role_can_submit_and_read_own_feedback(h, role):
    if role not in h.seed["identities"]:
        identity(h, role, [role])
    result = submit(h, role, kind="question", message="How do I change the fictional view?")
    row = detail(h, result, role)
    assert row["kind"] == "question" and row["status"] == "new" and row["revision"] == 1
    assert row["reporter_identity_id"] == h.seed["identities"][role]
    assert row["reporter_name"] == h.get(role, "/api/me")["display_name"]
    assert row["duplicates_count"] == 0
    assert row["occurrences"] == 1 and not row["has_screenshot"]
    mine = h.get(role, "/api/feedback?mine=true")["rows"]
    assert mine == [row]


def test_reporter_name_uses_current_identity_on_lists_details_and_triage_replies(h):
    reports = {role: submit(h, role, route="#" + role) for role in ("staff", "admin")}
    with transaction(h.app.state.engine, write=True) as db:
        for role in reports:
            db.get(m.Identity, h.seed["identities"][role]).display_name = "Renamed fictional " + role
        db.get(m.Person, h.seed["people"][0]).display_name = "Different fictional person name"
    for reader in ("operator", "auditor"):
        rows = h.get(reader, "/api/feedback")["rows"]
        assert {r["id"]: r["reporter_name"] for r in rows} == {
            report["id"]: "Renamed fictional " + role for role, report in reports.items()}
        for role, report in reports.items():
            assert detail(h, report, reader)["reporter_name"] == "Renamed fictional " + role
    for role in reports:
        assert h.get(role, "/api/feedback?mine=true")["rows"][0]["reporter_name"] == "Renamed fictional " + role
    changed = update(h, detail(h, reports["staff"]), status="triaged")["report"]
    assert changed["reporter_name"] == "Renamed fictional staff" and changed["duplicates_count"] == 0
    restored = h.command("operator", "feedback.undo", {"id": changed["id"], "revision": changed["revision"]})["report"]
    assert restored["reporter_name"] == changed["reporter_name"] and restored["duplicates_count"] == 0


def test_flat_active_view_roundtrips_without_changing_navigation_choices(h):
    state = {"active_view": "page/tab:list", "group_by": "status", "sort": ["kind", "asc"], "layout": "table"}
    created = submit(h, view_state=state)
    assert detail(h, created, "staff")["view_state"] == state
    assert h.get("staff", "/api/feedback?mine=true")["rows"][0]["view_state"] == state


@pytest.mark.parametrize("harness", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_recent_routes_are_redacted_in_storage_privileged_reads_and_export(request, harness):
    h = request.getfixturevalue(harness)
    routes = ["#home", "#/planner?date=private-date", "/people/FictionalPatient?token=private-token",
              "#/people/FictionalPatient%3Ftoken=encoded-secret", "#system/feedback#selection=private",
              "/%70eople/FictionalPatient", "#/people/FictionalPatient", "#home", "#/configuration/rules", "/"]
    redacted = ["/home", "/planner", "/people/[record]", "/people/[record]", "/system/feedback",
                "/people/[record]", "/people/[record]", "/home", "/configuration/rules", "/"]
    created = submit(h, recent_routes=routes)
    assert detail(h, created, "staff")["recent_routes"] == redacted
    assert detail(h, created)["recent_routes"] == redacted
    for role in PERMISSIONS["feedback.read_all"]:
        assert h.get(role, "/api/feedback")["rows"][0]["recent_routes"] == redacted
    with transaction(h.app.state.engine) as db:
        assert db.get(m.FeedbackReport, created["id"]).recent_routes == redacted
    exported = h.command("operator", "feedback.export_github", {"id": created["id"]})
    assert "Recent routes:\n" + "\n".join("- " + path for path in redacted) in exported["body"]
    for private in ("private-date", "FictionalPatient", "private-token", "encoded-secret", "selection=private"):
        assert private not in json.dumps(exported)


@pytest.mark.parametrize("harness", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_feedback_without_recent_routes_still_reads_and_exports(request, harness):
    h = request.getfixturevalue(harness)
    created = submit(h)
    assert detail(h, created)["recent_routes"] == []
    for role in PERMISSIONS["feedback.read_all"]:
        assert h.get(role, "/api/feedback")["rows"][0]["recent_routes"] == []
    body = h.command("operator", "feedback.export_github", {"id": created["id"]})["body"]
    assert "Recent routes:\n- None captured" in body


def test_export_reapplies_route_redaction_to_stored_recent_routes(h):
    created = submit(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.FeedbackReport, created["id"]).recent_routes = [
            "#/people/FictionalPatient?token=private-token", "/planner%3Fperson=private-person"]
    body = h.command("operator", "feedback.export_github", {"id": created["id"]})["body"]
    assert "Recent routes:\n- /people/[record]\n- /planner" in body
    for private in ("FictionalPatient", "private-token", "private-person"):
        assert private not in body


@pytest.mark.parametrize("kind", FEEDBACK_KINDS)
def test_all_kinds_preserve_context_without_accepting_forged_reporter_or_roles(h, kind):
    payload = {"kind": kind, "message": "Synthetic feedback", "expected": "The fictional control opens",
        "view_state": {"active_view": "Week", "filters": [], "grouping": "service", "dates": {"start": "2026-10-04"},
                       "open_panel": "feedback", "selection_count": 2},
        "page_title": "Fictional planner", "app_version": "0.8.0", "build_commit": "a" * 40,
        "environment": "dev", "role_names": ["admin"], "viewport": {"w": 1440, "h": 900}, "theme": "dark",
        "user_agent": "Mozilla/5.0 Chrome/130.0.0.0", "recent_errors": [{"at": m.now().isoformat().replace("+00:00", "Z"),
            "message": "Synthetic failed save", "source": "unhandledrejection"}], "request_ids": [str(uuid.uuid4())],
        "mark": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}, "screenshot": screenshot()}
    result = submit(h, **payload)
    row = detail(h, result, "staff")
    for key in ("kind", "message", "expected", "view_state", "page_title", "app_version", "build_commit", "viewport",
                "theme", "user_agent", "request_ids", "mark"):
        assert row[key] == payload[key], key
    assert row["recent_errors"] == [{**payload["recent_errors"][0], "message": "[free-text error redacted]"}]
    assert row["reporter_person_id"] == h.seed["people"][0] and row["role_names"] == ["staff"]
    assert row["has_screenshot"] and "screenshot" not in row and "screenshot_data" not in row
    submit(h, status=422, reporter_identity_id=h.seed["identities"]["admin"])


def test_submission_and_commands_require_authentication_origin_and_csrf(h):
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        assert client.get("/api/feedback").status_code == 401
        assert client.get("/api/feedback/missing").status_code == 401
        assert client.get("/api/feedback/missing/screenshot").status_code == 401
        assert client.post("/api/feedback", json={"kind": "problem", "message": "x", "route": "#home"}, headers={"idempotency-key": str(uuid.uuid4())}).status_code in {401, 403}
    client = h.client("staff")
    body = {"kind": "problem", "message": "x", "route": "#home"}
    assert client.post("/api/feedback", json=body, headers={"origin": "https://attacker.invalid", "idempotency-key": str(uuid.uuid4())}).status_code == 403
    assert client.post("/api/feedback", json=body, headers={"x-csrf-token": "forged", "idempotency-key": str(uuid.uuid4())}).status_code == 403


@pytest.mark.parametrize("role", ROLE_NAMES)
def test_foreign_reports_are_readable_only_by_developer_and_auditor(h, role):
    if role not in h.seed["identities"]:
        identity(h, role, [role])
    reporter = "staff" if role != "staff" else "coordinator"
    created = submit(h, reporter, screenshot=screenshot(), recent_routes=["#/people/FictionalPatient?secret=private"])
    privileged = role in PERMISSIONS["feedback.read_all"]
    expected_status = 200 if privileged else 404
    row = h.get(role, "/api/feedback/" + created["id"], status=expected_status)
    if privileged:
        assert row["recent_routes"] == ["/people/[record]"]
    assert h.client(role).get("/api/feedback/" + created["id"] + "/screenshot").status_code == expected_status
    listed = h.get(role, "/api/feedback?mine=false")["rows"]
    assert (created["id"] in {r["id"] for r in listed}) is privileged
    if privileged:
        assert next(r for r in listed if r["id"] == created["id"])["recent_routes"] == ["/people/[record]"]


@pytest.mark.parametrize("role", tuple(r for r in ROLE_NAMES if r != "operator"))
def test_triage_undo_and_export_are_developer_only_including_auditor(h, role):
    if role not in h.seed["identities"]:
        identity(h, role, [role])
    report = detail(h, submit(h, role))
    h.command(role, "feedback.update", {"id": report["id"], "revision": report["revision"], "status": "triaged"}, status=403)
    h.command(role, "feedback.undo", {"id": report["id"], "revision": report["revision"]}, status=403)
    h.command(role, "feedback.export_github", {"id": report["id"]}, status=403)
    assert detail(h, report)["revision"] == 1


def test_lists_filter_search_paginate_and_never_serialize_screenshot(h):
    ids = [submit(h, kind=kind, message=message, route="#" + str(index), screenshot=screenshot())["id"]
           for index, (kind, message) in enumerate((("problem", "Needle save"), ("suggestion", "Needle search"),
                    ("question", "How?"), ("confusing", "Which button?"), ("problem", "Another failure")))]
    triaged = update(h, detail(h, ids[0]), status="triaged")["report"]
    other = submit(h, "coordinator", message="Coordinator only", route="#other")
    assert h.get("operator", "/api/feedback?status=triaged")["rows"][0]["id"] == triaged["id"]
    assert {r["id"] for r in h.get("operator", "/api/feedback?kind=problem&q=Needle")["rows"]} == {ids[0]}
    assert h.get("operator", "/api/feedback?q=absent")["total"] == 0
    first = h.get("operator", "/api/feedback?page=1&page_size=2")
    second = h.get("operator", "/api/feedback?page=2&page_size=2")
    assert first["total"] == second["total"] == 6 and first["page_size"] == 2
    assert len(first["rows"]) == len(second["rows"]) == 2
    assert {r["id"] for r in first["rows"]}.isdisjoint(r["id"] for r in second["rows"])
    assert h.get("operator", "/api/feedback?mine=true")["total"] == 0
    assert {r["id"] for r in h.get("staff", "/api/feedback")["rows"]} == set(ids)
    assert other["id"] not in {r["id"] for r in h.get("staff", "/api/feedback?q=Coordinator")["rows"]}
    for row in h.get("operator", "/api/feedback")["rows"]:
        assert not {"screenshot", "screenshot_data", "screenshot_media_type"} & row.keys()


@pytest.mark.parametrize("query", ("status=unknown", "kind=unknown", "page=0", "page=-1", "page_size=0", "page_size=1001"))
def test_invalid_list_queries_are_rejected(h, query):
    h.get("operator", "/api/feedback?" + query, status=422)


@pytest.mark.parametrize("harness", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_duplicate_filter_pages_all_visible_links_and_counts_independently_of_page_filters(request, harness):
    h = request.getfixturevalue(harness)
    root = submit(h, message="Unique linked root needle")
    children = [submit(h, role) for role in ("staff", "coordinator", "coordinator", "staff", "coordinator")]
    unrelated = submit(h, "coordinator", route="#unrelated")
    child_ids = {child["id"] for child in children}
    for reader in ("operator", "auditor"):
        pages = [h.get(reader, f"/api/feedback?duplicate_of={root['id']}&page_size=2&page={page}")
                 for page in range(1, 4)]
        assert all(page["total"] == 5 and page["page_size"] == 2 for page in pages)
        listed = [row for page in pages for row in page["rows"]]
        assert len(listed) == 5 and {row["id"] for row in listed} == child_ids
        assert all(row["duplicate_of"] == root["id"] and row["duplicates_count"] == 0 for row in listed)
        assert all(row["reporter_name"] == h.get("staff" if row["reporter_identity_id"] == h.seed["identities"]["staff"]
                                                else "coordinator", "/api/me")["display_name"] for row in listed)
        filtered = h.get(reader, "/api/feedback?status=new&kind=problem&q=needle&page_size=1")
        assert filtered["total"] == 1 and filtered["rows"][0]["id"] == root["id"]
        assert filtered["rows"][0]["duplicates_count"] == 5
        assert detail(h, root, reader)["duplicates_count"] == 5
        assert detail(h, unrelated, reader)["duplicates_count"] == 0
    for reader in ("staff", "coordinator"):
        own = {child["id"] for child in children if detail(h, child)["reporter_identity_id"] == h.seed["identities"][reader]}
        for mine in ("false", "true"):
            listed = h.get(reader, f"/api/feedback?duplicate_of={root['id']}&mine={mine}")
            assert listed["total"] == len(own) and {row["id"] for row in listed["rows"]} == own
    h.get("coordinator", "/api/feedback/" + root["id"], status=404)
    assert detail(h, root, "staff")["duplicates_count"] == 2
    assert next(row for row in h.get("staff", "/api/feedback?mine=true")["rows"]
                if row["id"] == root["id"])["duplicates_count"] == 2
    assert h.get("operator", "/api/feedback?duplicate_of=" + str(uuid.uuid4()))["total"] == 0
    h.get("operator", "/api/feedback?duplicate_of=invalid", status=422)


def test_duplicate_counts_follow_direct_links_relinking_status_changes_and_undo(h):
    first = detail(h, submit(h, route="#one"))
    second = detail(h, submit(h, route="#two"))
    child = detail(h, submit(h, route="#one"))
    assert detail(h, first)["duplicates_count"] == 1 and detail(h, first)["occurrences"] == 2
    relinked = update(h, child, duplicate_of=second["id"])["report"]
    assert detail(h, first)["duplicates_count"] == 0 and detail(h, first)["occurrences"] == 2
    assert detail(h, second)["duplicates_count"] == 1
    restored = h.command("operator", "feedback.undo", {"id": child["id"], "revision": relinked["revision"]})["report"]
    assert restored["duplicate_of"] == first["id"]
    assert detail(h, first)["duplicates_count"] == 1 and detail(h, second)["duplicates_count"] == 0
    chained = update(h, detail(h, first), status="duplicate", duplicate_of=second["id"])["report"]
    assert chained["duplicates_count"] == 1 and detail(h, second)["duplicates_count"] == 1
    assert h.get("operator", "/api/feedback?duplicate_of=" + second["id"])["rows"][0]["id"] == first["id"]
    update(h, restored, status="triaged")
    assert detail(h, first)["duplicates_count"] == 0


@pytest.mark.parametrize("role", ("operator", "auditor"))
def test_privileged_mine_lists_count_only_own_links(h, role):
    root = submit(h, role)
    own = submit(h, role)
    submit(h, "staff")
    assert detail(h, root, role)["duplicates_count"] == 2
    listed = h.get(role, "/api/feedback?mine=true")
    assert listed["total"] == 2
    assert next(row for row in listed["rows"] if row["id"] == root["id"])["duplicates_count"] == 1
    filtered = h.get(role, f"/api/feedback?mine=true&duplicate_of={root['id']}")
    assert filtered["total"] == 1 and filtered["rows"][0]["id"] == own["id"]


@pytest.mark.parametrize("patch", (
    {"kind": "other"}, {"message": ""}, {"message": "x" * 4001}, {"expected": "x" * 2001},
    {"user_agent": "x" * 301}, {"recent_errors": [{"at": "2026-10-03T06:00:00Z", "message": "x", "source": "console"}] * 21},
    {"recent_errors": [{"at": "2026-10-03T06:00:00Z", "message": "x" * 301, "source": "console"}]},
    {"request_ids": [str(uuid.uuid4())] * 21}, {"environment": "staging"}, {"theme": "sepia"},
    {"recent_routes": ["#home"] * 11}, {"recent_routes": ["#/" + "x" * 1999]},
    {"recent_routes": ["#" + "x/" * 500]},  # Raw route fits; redacted segments exceed the output ceiling.
    {"recent_routes": [1]}, {"recent_routes": [True]}, {"recent_routes": [None]},
    {"recent_routes": [{"route": "#home"}]}, {"recent_routes": ["https://example.com"]},
    {"recent_routes": ["#//example.com"]}, {"recent_routes": ["#home\nprivate"]},
    {"view_state": {"record_contents": {"private_reason": "Fictional confidential text"}}},
    {"view_state": {"filters": [{"private_reason": "Fictional confidential text"}]}},
    {"view_state": {"active_view": {"diagnosis": "Fictional confidential text"}}},
    {"view_state": {"filters": [{"value": {"diagnosis": "Fictional confidential text"}}]}},
    {"view_state": {"dates": {"start": {"diagnosis": "Fictional confidential text"}}}},
    {"view_state": {"filters": [{"value": "x" * 16384}]}},
    {"mark": {"x": -0.1, "y": 0, "w": 0.2, "h": 0.2}},
    {"mark": {"x": 0.9, "y": 0, "w": 0.2, "h": 0.2}},
))
def test_schema_limits_and_context_privacy_reject_invalid_reports_without_persistence(h, patch):
    submit(h, status=422, **patch)
    assert h.get("operator", "/api/feedback")["total"] == 0


def test_text_and_context_limits_accept_the_boundary(h):
    # JSON byte size, including punctuation, is the view-state ceiling.
    context = {"filters": [{"value": ""}]}
    fixed = len(json.dumps(context, ensure_ascii=False).encode())
    context["filters"][0]["value"] = "x" * (16384 - fixed)
    result = submit(h, message="x" * 4000, expected="x" * 2000, user_agent="x" * 300, view_state=context,
        recent_errors=[{"at": m.now().isoformat(), "message": "x" * 300, "source": "console"}] * 20,
        request_ids=[str(uuid.uuid4()) for _ in range(20)], recent_routes=["#home"] * 9 + ["#/" + "x" * 1998])
    assert len(detail(h, result)["message"]) == 4000
    assert detail(h, result)["recent_routes"] == ["/home"] * 9 + ["/[record]"]


def test_feedback_body_has_its_own_three_megabyte_ceiling(h):
    # A permitted two-megabyte screenshot exceeds the ordinary API's smaller body ceiling after base64 encoding.
    raw = png(2 * 1024 * 1024)
    assert len(raw) == 2 * 1024 * 1024
    result = submit(h, screenshot=screenshot(raw))
    assert detail(h, result)["has_screenshot"]
    too_large = {"kind": "problem", "message": "x", "route": "#home", "unused": "x" * (3 * 1024 * 1024)}
    response = h.client("staff").post("/api/feedback", json=too_large, headers={"idempotency-key": str(uuid.uuid4())})
    assert response.status_code == 413
    assert h.get("operator", "/api/feedback")["total"] == 1


@pytest.mark.parametrize("image", (screenshot(b"not an image"), screenshot(PNG, "image/webp"),
    screenshot(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
    {"data": "not valid base64!", "media_type": "image/png"}, screenshot(png(2 * 1024 * 1024 + 1)),
    screenshot(PNG[:33]), screenshot(PNG[:29] + b"\x00\x00\x00\x00" + PNG[33:]),
    screenshot(WEBP[:-1], "image/webp"), screenshot(WEBP[:4] + b"\x00\x00\x00\x00" + WEBP[8:], "image/webp")))
def test_screenshot_validation_rejects_invalid_or_oversized_images(h, image):
    response = h.client("staff").post("/api/feedback", json={"kind": "problem", "message": "x", "route": "#home", "screenshot": image}, headers={"idempotency-key": str(uuid.uuid4())})
    assert response.status_code in {413, 422}, response.text
    assert h.get("operator", "/api/feedback")["total"] == 0


@pytest.mark.parametrize("raw,media_type", ((PNG, "image/png"), (WEBP, "image/webp")))
def test_screenshot_reads_are_private_bounded_and_audited_without_image_or_message_in_audit(h, raw, media_type):
    result = submit(h, screenshot=screenshot(raw, media_type), message="Fictional private report text")
    for role in ("staff", "operator", "auditor"):
        response = h.client(role).get("/api/feedback/" + result["id"] + "/screenshot")
        assert response.status_code == 200 and response.content == raw
        assert response.headers["content-type"].split(";")[0] == media_type
        assert "private" in response.headers["cache-control"] or "no-store" in response.headers["cache-control"]
        assert response.headers["x-content-type-options"] == "nosniff"
    events = [e for e in h.get("operator", "/api/audit")["events"]
              if e["target_id"] == result["id"] and e["action"] == "feedback.screenshot.read"]
    assert {e["actor_id"] for e in events} == {h.seed["identities"][r] for r in ("staff", "operator", "auditor")}
    assert "Fictional private report text" not in json.dumps(events)
    assert base64.b64encode(raw).decode() not in json.dumps(events)
    empty = submit(h, route="#empty")
    assert h.client("staff").get("/api/feedback/" + empty["id"] + "/screenshot").status_code == 404


def test_rate_limit_is_ten_per_hour_per_person_even_across_accounts(h, monkeypatch):
    moment = freeze_clock(monkeypatch)
    identity(h, "same_person", ["staff"], h.seed["people"][0])
    identity(h, "other_person", ["staff"], h.seed["people"][1])
    for index in range(10):
        submit(h, route="#limit" + str(index))
    for role in ("staff", "same_person"):
        response = h.client(role).post("/api/feedback", json={"kind": "question", "message": "x", "route": "#home"}, headers={"idempotency-key": str(uuid.uuid4())})
        assert response.status_code == 429 and 1 <= int(response.headers["retry-after"]) <= 3600
        assert response.json()["error"]["code"] == "RATE_LIMITED"
    submit(h, "other_person")
    later = moment + timedelta(hours=1, seconds=1)
    freeze_clock(monkeypatch, later)
    h.clients.pop("same_person").close()  # Renew the ordinary idle session as the next hourly window begins.
    submit(h, "same_person")


def test_fingerprints_ignore_query_values_but_distinguish_kind_path_and_first_error(h):
    error = [{"at": m.now().isoformat(), "message": "Synthetic failed save", "source": "console"}]
    original = submit(h, route="#planner?date=one", recent_errors=error)
    repeat = submit(h, "coordinator", route="#planner?date=two", recent_errors=error, message="Another account saw this")
    assert repeat.get("matched_report_id") == original["id"]
    root, linked = detail(h, original), detail(h, repeat)
    assert root["occurrences"] == 2 and linked["duplicate_of"] == original["id"]
    assert root["fingerprint"] == linked["fingerprint"]
    for values in ({"kind": "suggestion"}, {"route": "#requests?date=one"},
                   {"recent_errors": [{**error[0], "message": "Different error"}]}):
        result = submit(h, **{"route": "#planner?date=one", "recent_errors": error, **values})
        assert result.get("matched_report_id") is None
    h.get("coordinator", "/api/feedback/" + original["id"], status=404)


def test_repeats_link_to_the_canonical_report_and_resurface_a_resolved_problem(h):
    original = submit(h)
    fixed = update(h, detail(h, original), status="fixed", resolved_in="0.8.1")["report"]
    second = submit(h, "coordinator")
    third = submit(h, "approver")
    assert second.get("matched_report_id") == third.get("matched_report_id") == original["id"]
    root = detail(h, original)
    assert root["status"] == "new" and root["occurrences"] == 3 and root["revision"] > fixed["revision"]
    assert detail(h, second)["duplicate_of"] == detail(h, third)["duplicate_of"] == original["id"]


def test_fingerprint_does_not_match_reports_older_than_thirty_days(h):
    original = submit(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.FeedbackReport, original["id"]).created_at = m.now() - timedelta(days=30, seconds=1)
    recent = submit(h, "coordinator")
    assert recent.get("matched_report_id") is None
    assert detail(h, original)["occurrences"] == detail(h, recent)["occurrences"] == 1


def test_status_update_checks_revision_notifies_reporter_and_undo_restores_latest_developer_change(h):
    original = detail(h, submit(h, message="Fictional private content"))
    fixed = update(h, original, status="fixed", resolved_in="0.8.1", triage_note="Synthetic correction shipped")["report"]
    assert fixed["status"] == "fixed" and fixed["revision"] == original["revision"] + 1
    update(h, original, http_status=409, triage_note="Stale write")
    process_one(h.app.state.engine)
    notice = notes(h)[0]
    assert "fixed" in notice["message"] and "0.8.1" in notice["message"]
    assert notice["link"]["feedback_report_id"] == original["id"]
    assert "Fictional private content" not in json.dumps(notice) and not notes(h, "coordinator")
    noted = update(h, fixed, triage_note="Internal follow-up")["report"]
    process_one(h.app.state.engine)
    assert len(notes(h)) == 1
    h.command("operator", "feedback.undo", {"id": fixed["id"], "revision": fixed["revision"]}, status=409)
    undone = h.command("operator", "feedback.undo", {"id": noted["id"], "revision": noted["revision"]})["report"]
    assert undone["triage_note"] == fixed["triage_note"] and undone["status"] == "fixed"
    assert undone["revision"] == noted["revision"] + 1
    h.command("operator", "feedback.undo", {"id": undone["id"], "revision": undone["revision"]}, status=409)
    events = [e for e in h.get("operator", "/api/audit")["events"] if e["target_id"] == original["id"]]
    assert {"feedback.create", "feedback.update", "feedback.undo"} <= {e["action"] for e in events}
    assert "Fictional private content" not in json.dumps(events) and "Internal follow-up" not in json.dumps(events)


@pytest.mark.parametrize("status", FEEDBACK_STATUSES)
def test_each_status_is_saved_and_changes_are_reported_in_inbox(h, status):
    original = detail(h, submit(h, route="#status"))
    if status == "new":
        original = update(h, original, status="triaged")["report"]
        process_one(h.app.state.engine)
    previous_notices = len(notes(h))
    values = {"status": status}
    if status == "duplicate":
        values["duplicate_of"] = submit(h, "coordinator", route="#canonical")["id"]
    if status == "fixed":
        values["resolved_in"] = "0.8.1"
    changed = update(h, original, **values)["report"]
    assert detail(h, changed)["status"] == status
    process_one(h.app.state.engine)
    assert len(notes(h)) == previous_notices + 1
    assert all(note["link"]["feedback_report_id"] == original["id"] for note in notes(h))


def test_undoing_status_restores_report_and_notifies_reporter(h):
    original = detail(h, submit(h))
    fixed = update(h, original, status="fixed", resolved_in="0.8.1")["report"]
    process_one(h.app.state.engine)
    restored = h.command("operator", "feedback.undo", {"id": fixed["id"], "revision": fixed["revision"]})["report"]
    assert restored["status"] == "new" and restored["resolved_in"] is None
    process_one(h.app.state.engine)
    assert len(notes(h)) == 2
    assert all(note["link"]["feedback_report_id"] == original["id"] for note in notes(h))


@pytest.mark.parametrize("values", ({"status": "invalid"}, {"triage_note": "x" * 2001},
    {"resolved_in": "x" * 81}, {"revision": 0}, {"message": "Developers cannot overwrite original feedback"}))
def test_update_rejects_invalid_or_nontriage_fields_without_mutation(h, values):
    original = detail(h, submit(h))
    h.command("operator", "feedback.update", {"id": original["id"], "revision": original["revision"], **values}, status=422)
    assert detail(h, original)["revision"] == original["revision"]


def test_duplicate_updates_validate_target_and_prevent_cycles(h):
    first = detail(h, submit(h, route="#one"))
    second = detail(h, submit(h, route="#two"))
    duplicate = update(h, first, status="duplicate", duplicate_of=second["id"])["report"]
    assert duplicate["duplicate_of"] == second["id"]
    h.command("operator", "feedback.update", {"id": second["id"], "revision": second["revision"], "status": "duplicate", "duplicate_of": first["id"]}, status=422)
    h.command("operator", "feedback.update", {"id": second["id"], "revision": second["revision"], "status": "duplicate", "duplicate_of": second["id"]}, status=422)
    h.command("operator", "feedback.update", {"id": second["id"], "revision": second["revision"], "status": "duplicate", "duplicate_of": str(uuid.uuid4())}, status=404)
    h.command("operator", "feedback.update", {"id": second["id"], "revision": second["revision"], "status": "duplicate"}, status=422)


def test_developer_home_attention_counts_only_new_reports_without_private_details(h):
    first = detail(h, submit(h, route="#one", message="Fictional private home content"))
    submit(h, route="#two")
    waiting = next(item for item in h.get("operator", "/api/home")["attention"] if item["key"] == "feedback")
    assert waiting["count"] == 2 and "Fictional private home content" not in json.dumps(waiting)
    update(h, first, status="triaged")
    assert next(item for item in h.get("operator", "/api/home")["attention"] if item["key"] == "feedback")["count"] == 1
    for role in ("staff", "coordinator", "auditor", "admin"):
        assert "feedback" not in {item["key"] for item in h.get(role, "/api/home")["attention"]}


def test_export_preview_is_minimised_and_message_requires_explicit_opt_in(h, monkeypatch):
    from apps.api.services import feedback
    def forbid_network(*args, **kwargs):
        pytest.fail("Preview and unconfigured exports must never contact GitHub")
    monkeypatch.setattr(feedback, "create_github_issue", forbid_network)
    request_id = str(uuid.uuid4())
    created = submit(h, message="Fictional personal message", expected="Fictional private expectation",
        page_title="Fictional private page name", route="#planner?person=private-person&token=private-token",
        view_state={"filters": [{"value": "Fictional private filter"}]}, screenshot=screenshot(),
        app_version="0.8.0", build_commit="a" * 40, user_agent="Mozilla/5.0 Chrome/130.0.0.0",
        viewport={"w": 1440, "h": 900}, recent_errors=[{"at": m.now().isoformat(),
            "message": "TypeError: Failed to fetch", "source": "console"}], request_ids=[request_id])
    exported = h.command("operator", "feedback.export_github", {"id": created["id"]})
    text = exported["title"] + exported["body"]
    for secret in ("Fictional personal message", "Fictional private expectation", "Fictional private page name",
                   "Fictional private filter", "private-person", "private-token", h.get("staff", "/api/me")["display_name"],
                   base64.b64encode(PNG).decode()):
        assert secret not in text
    for expected in ("planner", "0.8.0", "a" * 40, "1440", "900", "TypeError", request_id, created["id"]):
        assert expected in text
    assert not exported["configured"] and not exported.get("github_issue_url")
    included = h.command("operator", "feedback.export_github", {"id": created["id"], "include_message": True})
    assert "Fictional personal message" in included["body"]
    assert "Fictional private expectation" not in included["body"]
    h.command("operator", "feedback.export_github", {"id": created["id"], "create_issue": True})


def test_export_redacts_free_text_errors_and_private_browser_suffixes(h, monkeypatch):
    from apps.api.services import feedback
    def forbid_network(*args, **kwargs):
        pytest.fail("Sanitisation preview must not contact GitHub")
    monkeypatch.setattr(feedback, "create_github_issue", forbid_network)
    name = h.get("staff", "/api/me")["display_name"]
    report = submit(h, user_agent="Mozilla/5.0 Chrome/130.0.0.0 Fictional_Private_UA",
        recent_errors=[{"at": m.now().isoformat(), "source": "console", "message":
            f"TypeError: {name} alice@example.invalid token=fictional-sensitive-token /api/person?name=FictionalPatient"}])
    result = h.command("operator", "feedback.export_github", {"id": report["id"]})
    for private in (name, "alice@example.invalid", "fictional-sensitive-token", "FictionalPatient", "Fictional_Private_UA"):
        assert private not in result["body"] and private not in result["title"]


@pytest.mark.parametrize("version,commit", [
    ("", ""), ("0.8.0-rc.13", "abcdef0"), ("v0.8.0-rc.13+abcdef0", "A" * 40),
    ("0.8.0", "1" * 40), ("development", "local-build"), ("0.8.0", ""), ("", "abcdef0"),
])
def test_normal_export_preserves_present_version_build_and_labels_absent_metadata(h, version, commit):
    report = submit(h, app_version=version, build_commit=commit)
    body = h.command("operator", "feedback.export_github", {"id": report["id"]})["body"]
    assert f"Version: {version or 'Not captured'}\n" in body
    assert f"Build: {commit or 'Not captured'}\n" in body


def test_export_scrubs_personal_and_secret_metadata(h):
    name = h.get("staff", "/api/me")["display_name"]
    report = submit(h, app_version=name + " alice@example.invalid", build_commit="local-build token=fictional-secret")
    body = h.command("operator", "feedback.export_github", {"id": report["id"]})["body"]
    assert name not in body and "alice@example.invalid" not in body and "fictional-secret" not in body
    assert "Version: [redacted]\n" in body and "Build: [redacted]\n" in body


@pytest.mark.parametrize("field", ("app_version", "build_commit"))
@pytest.mark.parametrize("value", ("diagnosis=FictionalPrivateCondition", "Fictional patient private clinical prose"))
def test_export_never_includes_clinical_prose_from_free_text_metadata(h, field, value):
    report = submit(h, **{field: value})
    body = h.command("operator", "feedback.export_github", {"id": report["id"]})["body"]
    assert value not in body and "[redacted]" in body


def test_configured_export_requires_explicit_creation_mocks_network_and_stores_url(h, monkeypatch):
    from apps.api.services import feedback
    object.__setattr__(h.settings, "feedback_github_token", "fictional-token-only")
    object.__setattr__(h.settings, "feedback_github_repo", "example/fictional-feedback")
    calls = []
    def create(repo, token, title, body):
        calls.append((repo, token, title, body))
        return "https://github.com/example/fictional-feedback/issues/42"
    monkeypatch.setattr(feedback, "create_github_issue", create)
    created = submit(h, recent_routes=["#/people/FictionalPatient?token=private-token"])
    preview = h.command("operator", "feedback.export_github", {"id": created["id"]})
    assert preview["configured"] and not calls
    exported = h.command("operator", "feedback.export_github", {"id": created["id"], "create_issue": True})
    assert len(calls) == 1 and calls[0][:2] == ("example/fictional-feedback", "fictional-token-only")
    assert "Recent routes:\n- /people/[record]" in calls[0][3]
    assert "FictionalPatient" not in calls[0][3] and "private-token" not in calls[0][3]
    assert exported["github_issue_url"] == detail(h, created)["github_issue_url"] == "https://github.com/example/fictional-feedback/issues/42"
    assert "fictional-token-only" not in json.dumps(exported)
    h.command("operator", "feedback.export_github", {"id": created["id"], "create_issue": True})
    assert len(calls) == 1


def test_failed_github_export_keeps_report_and_does_not_expose_token(h, monkeypatch):
    from apps.api.services import feedback
    object.__setattr__(h.settings, "feedback_github_token", "fictional-secret-token")
    object.__setattr__(h.settings, "feedback_github_repo", "example/fictional-feedback")
    def fail(*args):
        raise httpx.HTTPError("Fictional remote error containing fictional-secret-token")
    monkeypatch.setattr(feedback, "create_github_issue", fail)
    created = submit(h)
    result = h.command("operator", "feedback.export_github", {"id": created["id"], "create_issue": True}, status=502)
    assert "fictional-secret-token" not in json.dumps(result)
    assert not detail(h, created).get("github_issue_url")


@pytest.mark.parametrize("field,value", [("create_issue", "true"), ("create_issue", 1),
                                        ("include_message", "true"), ("include_message", 1)])
def test_export_requires_literal_boolean_consent(h, monkeypatch, field, value):
    from apps.api.services import feedback
    object.__setattr__(h.settings, "feedback_github_token", "fictional-token")
    object.__setattr__(h.settings, "feedback_github_repo", "example/feedback")
    monkeypatch.setattr(feedback, "create_github_issue", lambda *args: pytest.fail("Invalid consent must not export"))
    created = submit(h)
    h.command("operator", "feedback.export_github", {"id": created["id"], field: value}, status=422)
    assert not detail(h, created)["github_issue_url"]


@pytest.mark.parametrize("status,url,valid", ((201, "https://github.com/example/fictional-feedback/issues/42", True),
    (200, "https://github.com/example/fictional-feedback/issues/42", False),
    (201, "https://attacker.invalid/issues/42", False),
    (201, "https://github.com/example/other-repository/issues/42", False),
    (201, "https://github.com/example/fictional-feedback/issues/42?secret=value", False),
    (401, None, False)))
def test_github_adapter_targets_fixed_api_and_accepts_only_a_valid_created_issue_url(monkeypatch, status, url, valid):
    from apps.api.services.feedback import create_github_issue
    calls = []
    def post(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return httpx.Response(status, json={"html_url": url}, request=httpx.Request("POST", endpoint))
    monkeypatch.setattr(httpx, "post", post)
    if valid:
        assert create_github_issue("example/fictional-feedback", "fictional-token", "Sanitised title", "Sanitised body") == url
    else:
        with pytest.raises(Problem) as failed:
            create_github_issue("example/fictional-feedback", "fictional-token", "Sanitised title", "Sanitised body")
        assert failed.value.status == (422 if 400 <= status < 500 else 502)
        assert "fictional-token" not in str(failed.value)
    assert calls[0][0] == "https://api.github.com/repos/example/fictional-feedback/issues"
    assert calls[0][1]["json"] == {"title": "Sanitised title", "body": "Sanitised body"}
    assert calls[0][1]["headers"]["Authorization"] == "Bearer fictional-token"


@pytest.mark.parametrize("repo", ("https://attacker.invalid/repo", "example/../escape", "example/repo?token=private", ""))
def test_github_adapter_rejects_ambiguous_repository_before_network(monkeypatch, repo):
    from apps.api.services.feedback import create_github_issue
    def forbid_network(*args, **kwargs):
        pytest.fail("An invalid repository must not cause a network request")
    monkeypatch.setattr(httpx, "post", forbid_network)
    with pytest.raises(Problem) as failed:
        create_github_issue(repo, "fictional-token", "title", "body")
    assert failed.value.status == 409


def test_department_scoping_applies_to_reads_matching_commands_and_home(h):
    original = submit(h)
    with transaction(h.app.state.engine, write=True) as db:
        existing = db.get(m.Department, h.seed["department_id"])
        other = m.Department(organisation_id=existing.organisation_id, name="Fictional other department")
        db.add(other)
        db.flush()
        department_id = other.id
    identity(h, "other_developer", ["operator"], department=department_id)
    identity(h, "other_staff", ["staff"], department=department_id)
    for role in ("other_developer", "other_staff"):
        h.get(role, "/api/feedback/" + original["id"], status=404)
        assert h.get(role, "/api/feedback")["total"] == 0
        assert h.get(role, "/api/feedback?duplicate_of=" + original["id"])["total"] == 0
        assert "feedback" not in {i["key"] for i in h.get(role, "/api/home")["attention"]}
    foreign = submit(h, "other_staff")
    assert foreign.get("matched_report_id") is None
    h.command("other_developer", "feedback.update", {"id": original["id"], "revision": 1, "status": "triaged"}, status=404)
    h.command("other_developer", "feedback.export_github", {"id": original["id"]}, status=404)
    h.command("operator", "feedback.update", {"id": original["id"], "revision": 1, "status": "duplicate", "duplicate_of": foreign["id"]}, status=404)
    assert detail(h, original)["occurrences"] == 1
    assert detail(h, original)["duplicates_count"] == 0


@pytest.mark.postgres
def test_postgres_feedback_lifecycle_and_private_receipts(pg, monkeypatch):
    from apps.api.services import feedback
    monkeypatch.setattr(feedback, "create_github_issue", lambda *args: pytest.fail("Preview must not call GitHub"))
    first = submit(pg, screenshot=screenshot())
    repeat = submit(pg, "coordinator")
    assert repeat["matched_report_id"] == first["id"]
    assert detail(pg, first)["occurrences"] == 2
    pg.get("coordinator", "/api/feedback/" + first["id"], status=404)
    assert pg.client("auditor").get("/api/feedback/" + first["id"] + "/screenshot").content == PNG
    fixed = update(pg, detail(pg, first), status="fixed", resolved_in="0.8.1")["report"]
    process_one(pg.app.state.engine)
    assert notes(pg)[0]["link"]["feedback_report_id"] == first["id"]
    undone = pg.command("operator", "feedback.undo", {"id": first["id"], "revision": fixed["revision"]})["report"]
    assert undone["status"] == "new" and undone["resolved_in"] is None
    assert pg.get("operator", "/api/feedback?status=duplicate")["rows"][0]["id"] == repeat["id"]
    assert not pg.command("operator", "feedback.export_github", {"id": first["id"]})["configured"]


@pytest.mark.postgres
def test_postgres_concurrent_person_limit_and_duplicate_count_are_atomic(pg, monkeypatch):
    freeze_clock(monkeypatch)
    from concurrent.futures import ThreadPoolExecutor
    for _ in range(9):
        submit(pg)
    client = pg.client("staff")
    def send(_):
        return client.post("/api/feedback", json={"kind": "problem", "message": "Synthetic retry",
                           "route": "#planner"}, headers={"idempotency-key": str(uuid.uuid4())})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, range(2)))
    assert sorted(response.status_code for response in responses) == [200, 429]
    reports = pg.get("operator", "/api/feedback")
    assert reports["total"] == 10
    assert next(row for row in reports["rows"] if not row["duplicate_of"])["occurrences"] == 10

"""Bulk planner records obey workspace read authority and reflect each stored check."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .governance_support import identity


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)], ids=["sqlite", "postgres"])
def planner_harness(request):
    return request.getfixturevalue(request.param)


def test_bulk_sessions_and_issues_match_workspace_read_permissions_for_every_role(planner_harness):
    h = planner_harness
    workspace = h.shift(h.workspace("Private draft planner records"))["workspace"]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, workspace["id"]).validation = {"findings": [
            {"code": "SYNTHETIC_PRIVATE_FINDING", "severity": "error", "message": "Private draft finding"}]}
    for role in s.ROLE_NAMES:
        if role not in h.seed["identities"]:
            identity(h, role, [role])
        client = h.client(role)
        canonical = client.get("/api/workspaces/" + workspace["id"])
        assert canonical.status_code in (200, 403), canonical.text
        assert client.get("/api/workspaces").status_code == canonical.status_code
        for collection in ("sessions", "issues"):
            selection = {"collection": collection}
            for path, payload in (("/api/bulk/resolve", selection),
                                  ("/api/bulk/rows", {"selection": selection})):
                response = client.post(path, json=payload)
                assert response.status_code == canonical.status_code, (role, path, collection, response.text)
                if canonical.status_code == 200:
                    assert response.json()["count"] == 1
                else:
                    assert response.json()["error"]["code"] == canonical.json()["error"]["code"] == "FORBIDDEN"
        visible = client.get("/api/bulk/collections").json()["collections"]
        assert ("sessions" in visible) == (canonical.status_code == 200)
        assert ("issues" in visible) == (canonical.status_code == 200)


def test_issue_pages_reflect_new_and_disappearing_findings_without_workspace_revision(planner_harness):
    h = planner_harness
    workspace = h.workspace("Checks change without editing the draft")
    client = h.client("coordinator")
    selection = {"collection": "issues"}
    one = {"code": "SYNTHETIC_FIRST", "severity": "error", "message": "First current finding"}
    two = {"code": "SYNTHETIC_SECOND", "severity": "warning", "message": "New current finding"}
    for findings in ([], [one], [one, two], [two], []):
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Workspace, workspace["id"])
            assert row.revision == workspace["revision"]
            row.validation = {"findings": findings, "evaluated_at": "2026-10-03T06:00:00+00:00"}
        resolved = client.post("/api/bulk/resolve", json=selection)
        assert resolved.status_code == 200, resolved.text
        expected_ids = {row["id"] for row in resolved.json()["sample"]}
        assert resolved.json()["count"] == len(findings)
        returned = []
        for offset in range(max(1, len(findings))):
            page = client.post("/api/bulk/rows", json={"selection": selection,
                "columns": ["code", "message"], "offset": offset, "limit": 1})
            assert page.status_code == 200, page.text
            assert page.json()["count"] == len(findings)
            returned.extend(page.json()["rows"])
        assert {row["id"] for row in returned} == expected_ids
        assert {(row["code"], row["message"]) for row in returned} == {
            (finding["code"], finding["message"]) for finding in findings}

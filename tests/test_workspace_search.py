"""Command-menu rota search queries the permitted history before paging, without loading snapshots."""
from datetime import date, datetime, timedelta, timezone

import pytest

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)], ids=["sqlite", "postgres"])
def both(request):
    return request.getfixturevalue(request.param)


def workspace_row(h, name, age=0, department=None, archived=False):
    return m.Workspace(department_id=department or h.seed["department_id"], name=name,
                       starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11), status="draft",
                       created_by=h.seed["identities"]["coordinator"], last_edited_by=h.seed["identities"]["coordinator"],
                       created_at=datetime(2026, 10, 3, tzinfo=timezone.utc) - timedelta(minutes=age),
                       archived_at=datetime(2026, 10, 3, tzinfo=timezone.utc) if archived else None)


def read(h, role="coordinator", **params):
    response = h.client(role).get("/api/workspaces", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_workspace_search_precedes_paging_and_keeps_archive_date_scope(both):
    h = both
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([workspace_row(h, f"Recent ordinary rota {n}", n) for n in range(60)])
        matches = [workspace_row(h, f"Historical Needle {n}", 100+n) for n in range(3)]
        archived = workspace_row(h, "Historical Needle archived", 110, archived=True)
        db.add_all([*matches, archived]); db.flush()
        expected = [r.id for r in matches]
        archived_id = archived.id
    first = read(h, limit=50)
    assert not set(expected) & {r["id"] for r in first["workspaces"]}
    pages = [read(h, q="  HISTORICAL needle  ", limit=2, offset=n) for n in (0, 2)]
    assert [r["id"] for page in pages for r in page["workspaces"]] == expected
    assert all(page["total"] == 3 for page in pages)
    assert pages[0]["has_more"] and not pages[1]["has_more"]
    assert read(h, q="HistoricalNeedle")["total"] == 3
    assert read(h, q="Historical", archived=True)["workspaces"][0]["id"] == archived_id
    assert read(h, q="Historical", start="2026-10-11")["total"] == 0
    assert read(h, q="Historical", end="2026-10-04")["total"] == 0
    assert read(h, q="no such name")["workspaces"] == []


def test_workspace_search_treats_wildcards_literally_and_bounds_query(both):
    h = both
    with transaction(h.app.state.engine, write=True) as db:
        literal = workspace_row(h, r"100% planning_under\review")
        db.add_all([literal, workspace_row(h, "100X planningXunderXreview")]); db.flush()
        expected = literal.id
    for q in ("%", "_", "\\", r"100% planning_under\review"):
        result = read(h, q=q)
        assert result["total"] == 1 and result["workspaces"][0]["id"] == expected
    assert h.client("coordinator").get("/api/workspaces", params={"q": "x" * 161}).status_code == 422


def test_workspace_search_preserves_department_and_role_scope(both):
    h = both
    identity(h, "programme_lead", ["programme_lead"])
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed["organisation_id"], name="Separate search department")
        db.add(other); db.flush()
        local = workspace_row(h, "Scope needle")
        db.add_all([local, workspace_row(h, "Scope needle", department=other.id)]); db.flush()
        expected = local.id
    for role in ("admin", "coordinator", "approver", "programme_lead", "auditor"):
        result = read(h, role, q="scope needle")
        assert result["total"] == 1 and result["workspaces"][0]["id"] == expected
    for role in ("staff", "credentialer", "operator"):
        assert h.client(role).get("/api/workspaces", params={"q": "scope needle"}).status_code == 403


def test_workspace_results_link_exact_publication_without_snapshot_payload(both):
    h = both
    draft = h.workspace("Duplicate rota name")
    published = h.publish(h.shift(h.workspace("Duplicate rota name"))["workspace"])
    result = read(h, q="duplicate rota name")
    by_id = {r["id"]: r for r in result["workspaces"]}
    assert by_id[draft["id"]]["publication_id"] is None
    assert by_id[published["workspace"]["id"]]["publication_id"] == published["publication_id"]
    for workspace_id, row in by_id.items():
        assert h.detail(workspace_id)["workspace"]["publication_id"] == row["publication_id"]
    assert all(not {"snapshot", "review_snapshot", "validation"} & set(row) for row in result["workspaces"])
    matching = read(h, q="published")
    assert matching["total"] == 1 and matching["workspaces"][0]["id"] == published["workspace"]["id"]


def test_workspace_detail_publication_identity_invalidates_existing_etag(both):
    h = both
    w = h.shift(h.workspace("Detail publication identity"))["workspace"]
    client = h.client("coordinator")
    url = "/api/workspaces/" + w["id"]
    before = client.get(url)
    assert before.status_code == 200
    assert before.json()["workspace"]["publication_id"] is None
    old_tag = before.headers["ETag"]
    assert client.get(url, headers={"If-None-Match": old_tag}).status_code == 304
    published = h.publish(w)
    after = client.get(url, headers={"If-None-Match": old_tag})
    assert after.status_code == 200
    assert after.headers["ETag"] != old_tag
    assert after.json()["workspace"]["id"] == w["id"]
    assert after.json()["workspace"]["publication_id"] == published["publication_id"]
    assert read(h, q=w["name"])["workspaces"][0]["publication_id"] == published["publication_id"]
    assert client.get(url, headers={"If-None-Match": after.headers["ETag"]}).status_code == 304

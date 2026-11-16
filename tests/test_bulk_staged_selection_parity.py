"""Staging preserves the same selection and independent-review access as Preview."""
import pytest

from .governance_support import identity
from .test_bulk import REASON


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("selection_kind", ["all", "empty_where", "exclude", "one", "empty"])
def test_stage_preserves_preview_selection_and_explicit_empty_ids(request, database, selection_kind):
    h = request.getfixturevalue(database)
    selection = {"collection": "people"}
    if selection_kind == "empty_where":
        selection["where"] = {}
    elif selection_kind == "exclude":
        selection["exclude_ids"] = h.seed["people"][:1]
    elif selection_kind in {"one", "empty"}:
        selection["include_ids"] = h.seed["people"][:1] if selection_kind == "one" else []
    payload = {"item_kind": "catalogue.update", "selection": selection,
        "changes": [{"field": "title", "value": "Staged collection title"}], "reason": REASON}
    client = h.client("coordinator")
    checked = client.post("/api/commands/preview", json={"kind": "bulk.apply", "payload": payload})
    if selection_kind == "empty":
        assert checked.status_code == 422, checked.text
        staged = h.command("coordinator", "bulk.stage", {"payload": payload}, status=422)
        assert staged["error"]["code"] == checked.json()["error"]["code"] == "BULK_ITEM_LIMIT"
        assert h.get("coordinator", "/api/bulk/staged") == {"items": [], "count": 0}
        return
    assert checked.status_code == 200, checked.text
    preview = checked.json()
    staged = h.command("coordinator", "bulk.stage", {"payload": payload})["staged"]
    assert "selection" not in staged["payload"] or staged["payload"]["selection"] is None
    assert {row["target_id"] for row in staged["payload"]["items"]} == {
        row["target_id"] for row in preview["items"]}
    assert len(staged["payload"]["items"]) == preview["count"] > 0
    reopened = client.post(f"/api/bulk/staged/{staged['id']}/reopen")
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["preview"]["counts"] == preview["counts"]
    assert reopened.json()["preview"]["items"] == preview["items"]


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_reviewer_with_no_owned_stages_lists_and_reopens_waiting_handoff_by_staged_id(request, database):
    h = request.getfixturevalue(database)
    person = h.seed["people"][0]
    identity(h, "requesting_credentialer", ["credentialer"], person=person)
    service = h.configure("services", {"name": "Independent handoff clinic", "starts_on": "2020-01-01"})["record"]
    payload = {"item_kind": "eligibility.create", "reason": REASON,
        "items": [{"payload": {"person_id": person, "service_id": service["id"], "starts_on": "2026-10-01"}}]}
    sent = h.command("requesting_credentialer", "bulk.apply", {
        **payload, "resolutions": {"0": "send_for_approval"}})
    staged_id = sent["reviews"][0]["id"]
    me = h.get("credentialer", "/api/me")
    assert me["bulk_staged_count"] == 0 and me["bulk_review_count"] == 1
    assert h.get("requesting_credentialer", "/api/me")["bulk_review_count"] == 0
    listed = h.get("credentialer", "/api/bulk/staged")
    assert listed["count"] == 1
    assert listed["items"][0]["id"] == staged_id
    assert listed["items"][0]["status"] == "waiting"
    assert me["bulk_review_count"] == len([row for row in listed["items"] if row["owner_id"] != me["identity_id"]])
    reopened = h.client("credentialer").post(f"/api/bulk/staged/{staged_id}/reopen")
    assert reopened.status_code == 200, reopened.text
    body = reopened.json()
    assert body["staged"]["id"] == staged_id
    assert body["preview"]["counts"]["ready"] == 1
    assert body["preview"]["source_fingerprint"]

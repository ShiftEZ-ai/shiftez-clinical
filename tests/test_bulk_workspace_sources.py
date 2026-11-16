"""Registry rows lower to canonical workspace writes without losing source identity."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import workspace_shifts
from .governance_support import identity
from .test_bulk import REASON, preview
from .test_bulk_nested_limits import limits


def sessions(h, *, issues=False):
    if issues:
        h.policy({"kind": "restriction"}, {"person_id": h.seed["people"][0]}, severity="advisory")
    first = h.shift(h.workspace())
    second = h.shift(first["workspace"], start="2026-10-05T08:00:00+03:00", end="2026-10-05T12:00:00+03:00")
    return second["workspace"], [first["shifts"][0], second["shifts"][0]]


def rows(h, workspace):
    with transaction(h.app.state.engine) as db:
        return workspace_shifts(db, workspace["id"])


def apply(h, body, fingerprint=None, *, status=200, role="coordinator"):
    response = h.client(role).post("/api/commands", headers={"idempotency-key": m.uid()}, json={
        "kind": "bulk.apply", "payload": body, **({"expected_fingerprint": fingerprint} if fingerprint else {})})
    assert response.status_code == status, response.text
    return response.json()


@pytest.mark.parametrize("mode", ["selection", "items"])
def test_registry_session_field_changes_keep_own_rows_and_share_workspace_revision_and_undo(h, mode):
    workspace, selected = sessions(h)
    body = {"item_kind": "shift.update", "changes": [{"field": "location_id", "how": "set", "value": h.seed["locations"][1]}],
            "reason": REASON}
    body.update({"selection": {"collection": "sessions", "include_ids": [row["id"] for row in selected]}} if mode == "selection" else {
        "items": [{"target_id": row["id"], "expected_revision": row["revision"]} for row in selected]})
    before = rows(h, workspace)
    limits(h, 2)
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["ready"] == 2, checked
    assert {row["target_id"] for row in checked["items"]} == {row["id"] for row in selected}
    for row in checked["items"]:
        assert row["before"]["location_id"] == h.seed["locations"][0]
        assert row["after"]["location_id"] == h.seed["locations"][1]
        assert row["record"]["workspace_id"] == workspace["id"]
    assert rows(h, workspace) == before
    applied = apply(h, body, checked["fingerprint"])
    assert applied["counts"]["changed"] == 2
    assert all(row["location_id"] == h.seed["locations"][1] for row in rows(h, workspace))
    assert len(applied["undo"]["payload"]["items"]) == 1
    assert all("_bulk_workspace_source" not in row["authorization_command"]["payload"]
               for item in applied["items"] for row in item["results"])
    h.command("coordinator", **applied["undo"])
    assert rows(h, workspace) == before


@pytest.mark.parametrize("mode", ["selection", "items"])
def test_registry_issue_actions_lower_keys_and_undo_multiple_selected_rows_in_one_workspace(h, mode):
    workspace, _ = sessions(h, issues=True)
    selected = h.client("coordinator").post("/api/bulk/resolve", json={"collection": "issues"}).json()["sample"]
    assert len(selected) == 2
    body = {"changes": [{"item_kind": "issues.resolve", "payload": {"action": "remove"}}], "reason": REASON}
    body.update({"selection": {"collection": "issues", "include_ids": [row["id"] for row in selected]}} if mode == "selection" else {
        "items": [{"target_id": row["id"], "expected_revision": row["revision"]} for row in selected]})
    before = rows(h, workspace)
    limits(h, 2)
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["ready"] == 2, checked
    assert {row["target_id"] for row in checked["items"]} == {row["id"] for row in selected}
    assert all(row["after"]["resolved"] is True for row in checked["items"])
    statuses = {row["id"]: row["status"] for row in selected}
    assert all(row["record"]["status"] == statuses[row["target_id"]] for row in checked["items"])
    assert rows(h, workspace) == before
    applied = apply(h, body, checked["fingerprint"])
    assert applied["counts"]["changed"] == 2 and rows(h, workspace) == []
    assert len(applied["undo"]["payload"]["items"]) == 1
    h.command("coordinator", **applied["undo"])
    assert rows(h, workspace) == before


def test_registry_single_issue_action_fits_one_record_limit(h):
    workspace, _ = sessions(h, issues=True)
    issue = h.client("coordinator").post("/api/bulk/resolve", json={"collection": "issues"}).json()["sample"][0]
    limits(h, 1)
    body = {"selection": {"collection": "issues", "include_ids": [issue["id"]]},
        "changes": [{"item_kind": "issues.resolve", "payload": {"action": "remove"}}], "reason": REASON}
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["ready"] == 1, checked
    result = apply(h, body, checked["fingerprint"])
    assert result["counts"]["changed"] == 1 and len(rows(h, workspace)) == 1
    h.command("coordinator", **result["undo"])
    assert len(rows(h, workspace)) == 2


def test_session_selection_fingerprint_binds_assignment_only_workspace_changes(h):
    workspace, selected = sessions(h)
    body = {"item_kind": "shift.update", "selection": {"collection": "sessions", "include_ids": [selected[0]["id"]]},
        "changes": [{"field": "location_id", "how": "set", "value": h.seed["locations"][1]}], "reason": REASON}
    checked = preview(h, "coordinator", body)
    with transaction(h.app.state.engine, write=True) as db:
        assignment = db.scalar(select(m.Assignment).where(m.Assignment.shift_id == selected[0]["id"]))
        assignment.fixed, assignment.revision = True, assignment.revision + 1
        db.get(m.Workspace, workspace["id"]).revision += 1
        assert db.get(m.Shift, selected[0]["id"]).revision == selected[0]["revision"]
    refused = apply(h, body, checked["fingerprint"], status=409)
    assert refused["error"]["code"] == "STALE_PREVIEW"


@pytest.mark.parametrize("wrong_collection", ["issues", "sessions"])
def test_forged_workspace_source_binding_is_rederived_from_authorized_collection_rows(h, wrong_collection):
    workspace, selected = sessions(h, issues=True)
    hidden = h.workspace(name="Another source must stay unchanged")
    source = "sessions" if wrong_collection == "issues" else "issues"
    row = selected[0] if source == "sessions" else h.client("coordinator").post(
        "/api/bulk/resolve", json={"collection": "issues"}).json()["sample"][0]
    body = {"items": [{"target_id": row["id"], "expected_revision": row["revision"], "payload": {
        "_bulk_workspace_source": {"collection": wrong_collection, "workspace_id": hidden["id"], "revision": row["revision"]}}}],
        "reason": REASON}
    if source == "sessions":
        body.update(item_kind="shift.update", changes=[{"field": "location_id", "value": h.seed["locations"][1]}])
    else:
        body["changes"] = [{"item_kind": "issues.resolve", "payload": {"action": "remove"}}]
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["ready"] == 1, checked
    apply(h, body, checked["fingerprint"])
    assert rows(h, hidden) == []


def test_staged_session_binding_does_not_gain_a_fresh_workspace_revision_on_reopen(h):
    workspace, selected = sessions(h)
    body = {"item_kind": "shift.update", "items": [{"target_id": selected[0]["id"],
        "expected_revision": selected[0]["revision"]}], "changes": [{"field": "location_id", "value": h.seed["locations"][1]}],
        "reason": REASON}
    staged = h.command("coordinator", "bulk.stage", {"payload": body})["staged"]
    missing_revision = {**staged["payload"], "items": [{key: value for key, value in staged["payload"]["items"][0].items()
                                                    if key != "expected_revision"}]}
    missing = preview(h, "coordinator", missing_revision)
    assert missing["items"][0]["issues"][0]["code"] == "REVISION_REQUIRED"
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, workspace["id"]).revision += 1
    checked = preview(h, "coordinator", staged["payload"])
    assert checked["counts"]["problem"] == 1
    assert checked["items"][0]["issues"][0]["code"] == "STALE_REVISION"


@pytest.mark.parametrize("role", ["staff", "programme_lead", "credentialer", "auditor"])
def test_session_adapter_denies_write_roles_before_projection_even_with_forged_revision(h, role):
    _, selected = sessions(h)
    if role not in h.seed["identities"]:
        identity(h, role, [role])
    body = {"item_kind": "shift.update", "items": [{"target_id": selected[0]["id"], "expected_revision": 999,
        "payload": {"_bulk_workspace_source": {"collection": "sessions", "revision": 999}}}],
        "changes": [{"field": "location_id", "value": h.seed["locations"][1]}], "reason": REASON}
    row = preview(h, role, body)["items"][0]
    assert row["issues"][0]["code"] == "FORBIDDEN"
    assert row["before"] == row["after"] == {} and not row.get("record")


def test_multiple_selected_issues_on_the_same_removed_session_share_one_checked_write(h):
    for _ in range(2):
        h.policy({"kind": "restriction"}, {"person_id": h.seed["people"][0]}, severity="advisory")
    made = h.shift(h.workspace())
    selected = h.client("coordinator").post("/api/bulk/resolve", json={"collection": "issues"}).json()["sample"]
    assert len(selected) == 2
    body = {"selection": {"collection": "issues", "include_ids": [row["id"] for row in selected]},
        "changes": [{"item_kind": "issues.resolve", "payload": {"action": "remove"}}], "reason": REASON}
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["ready"] == 2, checked
    result = apply(h, body, checked["fingerprint"])
    assert result["counts"]["changed"] == 2 and len(result["authorization_commands"]) == 1
    assert rows(h, made["workspace"]) == []
    h.command("coordinator", **result["undo"])
    assert len(rows(h, made["workspace"])) == 1


def test_session_participant_change_returns_native_current_participants_without_stale_person_projection(h):
    workspace, selected = sessions(h)
    row = selected[0]
    body = {"item_kind": "shift.update", "selection": {"collection": "sessions", "include_ids": [row["id"]]},
        "changes": [{"field": "participants", "value": [{"person_id": h.seed["people"][1],
            "start": row["start"], "end": row["end"]}]}], "reason": REASON}
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["ready"] == 1, checked
    record = checked["items"][0]["record"]
    assert [person["person_id"] for person in record["participants"]] == [h.seed["people"][1]]
    assert "person_ids" not in record and "status" not in record
    apply(h, body, checked["fingerprint"])
    saved = next(saved for saved in rows(h, workspace) if saved["id"] == row["id"])
    assert [{key: person[key] for key in ("person_id", "start", "end")} for person in saved["participants"]] == [
        {key: person[key] for key in ("person_id", "start", "end")} for person in record["participants"]]


def test_session_date_change_does_not_echo_old_as_of_status_in_native_preview(h):
    _, selected = sessions(h)
    row = selected[0]
    with transaction(h.app.state.engine, write=True) as db:
        participant = db.scalar(select(m.Assignment).where(m.Assignment.shift_id == row["id"]))
        participant.fixed = True
        participant.segments = [{"start": "2026-10-04T08:00:00+03:00", "end": "2026-10-04T09:00:00+03:00"},
                                {"start": "2026-10-04T10:00:00+03:00", "end": "2026-10-04T12:00:00+03:00"}]
        db.add(m.Assignment(shift_id=row["id"], person_id=h.seed["people"][1],
                            start=participant.start, end=participant.end))
    selection = {"collection": "sessions", "where": {"as_of": "2026-10-05", "filters": {"id": [row["id"]]}}}
    resolved = h.client("coordinator").post("/api/bulk/resolve", json=selection).json()
    assert resolved["sample"][0]["status"] == "ended"
    body = {"item_kind": "shift.update", "selection": selection, "changes": [
        {"field": "start", "how": "move_by", "value": {"days": 2}},
        {"field": "end", "how": "move_by", "value": {"days": 2}}],
        "reason": REASON}
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["ready"] == 1, [(issue["code"], issue["message"])
        for item in checked["items"] for issue in item["issues"]]
    assert "status" not in checked["items"][0]["record"]
    changed = checked["items"][0]["record"]
    assert changed["start"].startswith("2026-10-06")
    participant = next(person for person in changed["participants"] if person["person_id"] == h.seed["people"][0])
    assert participant["start"] == changed["start"] and participant["end"] == changed["end"]
    assert participant["fixed"] and len(participant["segments"]) == 2
    assert all(segment["start"].startswith("2026-10-06") and segment["end"].startswith("2026-10-06")
               for segment in participant["segments"])
    apply(h, body, checked["fingerprint"])
    assert h.client("coordinator").post("/api/bulk/resolve", json=selection).json()["sample"][0]["status"] == "future"


def test_selection_that_became_empty_after_preview_is_stale_but_an_unpreviewed_empty_selection_is_invalid(h):
    service = h.configure("services", {"name": "Synthetic disappearing selected clinic", "starts_on": "2020-01-01"})["record"]
    body = {"item_kind": "catalogue.update", "selection": {"collection": "services", "include_ids": [service["id"]]},
        "changes": [{"field": "description", "value": "Synthetic selected edit"}], "reason": REASON}
    checked = preview(h, "admin", body)
    assert checked["counts"]["ready"] == 1
    h.command("admin", "catalogue.undo_create", {"catalogue": "services", "reason": REASON},
              service["id"], service["revision"])
    assert apply(h, body, checked["fingerprint"], status=409, role="admin")["error"]["code"] == "STALE_PREVIEW"
    assert apply(h, body, status=422, role="admin")["error"]["code"] == "BULK_ITEM_LIMIT"


@pytest.mark.parametrize("kind,payload", [("shift.copy", {"offset_days": 2}), ("shift.action", {"action": "cancel"})])
def test_registry_session_actions_take_only_business_inputs_and_derive_workspace_and_session_ids(h, kind, payload):
    workspace, selected = sessions(h)
    before = rows(h, workspace)
    limits(h, 1)
    body = {"selection": {"collection": "sessions", "include_ids": [selected[0]["id"]]},
        "changes": [{"item_kind": kind, "payload": payload}], "reason": REASON}
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["ready"] == 1, checked
    assert rows(h, workspace) == before
    result = apply(h, body, checked["fingerprint"])
    assert result["counts"]["changed"] == 1
    assert result["items"][0]["target_id"] == selected[0]["id"]
    current = rows(h, workspace)
    assert len(current) == (3 if kind == "shift.copy" else 2)
    if kind == "shift.action":
        assert next(row for row in current if row["id"] == selected[0]["id"])["cancelled"]
    h.command("coordinator", **result["undo"])
    assert rows(h, workspace) == before


def test_location_only_session_change_compares_canonical_inputs_not_assignment_ids_or_insertion_order(h):
    people = sorted(h.seed["people"][:2], reverse=True)
    made = h.shift(h.workspace(), people=people)
    row = made["shifts"][0]
    body = {"item_kind": "shift.update", "selection": {"collection": "sessions", "include_ids": [row["id"]]},
        "changes": [{"field": "location_id", "value": h.seed["locations"][1]}], "reason": REASON}
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["ready"] == 1, checked
    changed = checked["items"][0]
    assert changed["before"] == {"location_id": h.seed["locations"][0]}
    assert changed["after"] == {"location_id": h.seed["locations"][1]}
    participants = changed["record"]["participants"]
    assert [person["person_id"] for person in participants] == sorted(people)
    assert all(not ({"id", "revision", "created_at", "source_at"} & set(person)) for person in participants)
    result = apply(h, body, checked["fingerprint"])
    assert result["items"][0]["before"] == changed["before"]
    assert result["items"][0]["after"] == changed["after"]

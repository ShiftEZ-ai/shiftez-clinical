"""Related clinic locations use the same ancestor projection as canonical conditions."""
import pytest


def post(h, endpoint, payload):
    response = h.client("coordinator").post("/api/bulk/" + endpoint, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("source", ["session", "recurring"])
@pytest.mark.parametrize("path", ["projection", "flat", "tree"])
def test_related_clinic_location_ancestors_match_projection_flat_and_tree_filters(h, source, path):
    parent = next(row for row in h.get("admin", "/api/catalogues")["locations"]
                  if row["id"] == h.seed["locations"][0])
    floor = h.configure("locations", {"name": "Synthetic clinic floor", "starts_on": "2020-01-01",
        "location_type_id": parent["location_type_id"], "parent_id": parent["id"]})["record"]
    room = h.configure("locations", {"name": "Synthetic clinic room", "starts_on": "2020-01-01",
        "location_type_id": parent["location_type_id"], "parent_id": floor["id"]})["record"]
    service = h.seed["services"][0]
    if source == "session":
        workspace = h.workspace()
        h.command("coordinator", "shift.create", {"workspace_id": workspace["id"],
            "assignment_type_id": h.seed["type"], "location_id": room["id"], "service_ids": [service],
            "start": "2026-10-04T08:00:00+03:00", "end": "2026-10-04T12:00:00+03:00",
            "participants": [], "attributes": {}}, revision=workspace["revision"])
    else:
        h.configure("recurring_series", {"name": "Synthetic clinic in a nested room", "starts_on": "2020-01-01",
            "assignment_type_id": h.seed["type"], "location_id": room["id"],
            "definition": {"anchor": "2026-10-04", "days": [
                {"weekday": 0, "start_time": "08:00", "end_time": "12:00"}], "service_ids": [service]}})
    selection = {"collection": "eligibilities", "where": {"as_of": "2026-10-04",
        "filters": {"service_id": [service]}}}
    expected = post(h, "resolve", selection)
    assert expected["count"] > 1
    # The canonical field already includes the building as well as the leaf room.
    canonical = {**selection, "where": {**selection["where"], "conditions": {
        "conditions": [{"field": "location_ids", "values": [parent["id"]]}]}}}
    assert post(h, "resolve", canonical)["count"] == expected["count"]
    if path == "flat":
        selection["where"]["filters"]["service.location_ids"] = [parent["id"]]
    elif path == "tree":
        selection["where"]["conditions"] = {"match": "all", "conditions": [
            {"match": "any", "conditions": [
                {"field": "service.location_ids", "values": [parent["id"]]}]},
            {"field": "service.location_ids", "op": "none", "values": [h.seed["locations"][1]]}]}
    resolved = post(h, "resolve", selection)
    assert resolved["count"] == expected["count"]
    payload = {"selection": selection, "columns": ["service.location_ids"],
        "sort": [{"field": "service.location_ids"}], "group": "service.location_ids", "limit": 1}
    first = post(h, "rows", payload)
    assert first["count"] == expected["count"] and first["has_more"]
    ancestors = sorted([parent["id"], floor["id"], room["id"]])
    assert first["rows"][0]["service.location_ids"] == ancestors
    assert first["groups"] == [{"value": ancestors, "count": expected["count"]}]
    later = post(h, "rows", {**payload, "offset": 1})
    assert later["rows"][0]["service.location_ids"] == ancestors
    assert later["groups"] == first["groups"]
    assert post(h, "rows", payload) == first

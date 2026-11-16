"""Nullable open-offer recipients never confer staff access through bulk reads."""
import pytest

from .governance_support import identity
from .test_open_swaps import payload
from .test_staff_swaps import fixed_clock, source_pair  # noqa: F401


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_unlinked_staff_cannot_resolve_or_read_unclaimed_open_swaps(database, kind, request):
    h = request.getfixturevalue(database)
    publication, first, _, _ = source_pair(h)
    secret = "Participant-only explanation must not reach an unlinked identity"
    offer = h.command("staff", "swap.offer", {**payload(publication, first, kind), "reason": secret})["swap"]
    assert offer["open_offer"] and offer["recipient_person_id"] is None
    identity(h, "unlinked_bulk_staff", ["staff"])
    assert h.get("unlinked_bulk_staff", "/api/me")["person_id"] is None

    for selection in (
        {"collection": "swaps"},
        {"collection": "swaps", "include_ids": [offer["id"]]},
        {"collection": "swaps", "where": {"filters": {"id": [offer["id"]]}}},
    ):
        for endpoint, body, records in (
            ("/api/bulk/resolve", selection, "sample"),
            ("/api/bulk/rows", {"selection": selection, "columns": ["id", "reason", "recipient_person_id"]}, "rows"),
        ):
            response = h.client("unlinked_bulk_staff").post(endpoint, json=body)
            assert response.status_code == 200, response.text
            assert response.json()["count"] == 0 and response.json()[records] == []
            assert secret not in response.text and offer["id"] not in response.text

    selection = {"collection": "swaps", "include_ids": [offer["id"]]}
    resolved = h.client("staff").post("/api/bulk/resolve", json=selection)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["count"] == 1 and resolved.json()["sample"][0]["id"] == offer["id"]
    rows = h.client("staff").post("/api/bulk/rows", json={"selection": selection, "columns": ["id", "reason"]})
    assert rows.status_code == 200, rows.text
    assert rows.json()["count"] == 1 and rows.json()["rows"] == [
        {"id": offer["id"], "revision": offer["revision"], "status": offer["status"], "reason": secret}]

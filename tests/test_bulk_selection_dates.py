"""Rule-line filters validate dates before querying, including empty collections."""
import pytest


@pytest.mark.parametrize("endpoint", ["resolve", "rows"])
@pytest.mark.parametrize(("field", "value"), [
    ("starts_on", ["not-a-date"]),
    ("ends_on", {"gte": "not-a-date"}),
    ("starts_on", ["2026-02-30"]),
    ("ends_on", {"lt": "2026-02-30"}),
    ("starts_on", [None]),
    ("ends_on", [42]),
])
def test_rule_line_date_filter_rejects_invalid_scalar_and_range(h, endpoint, field, value):
    selection = {"collection": "policy_lines", "where": {"filters": {field: value}}}
    payload = selection if endpoint == "resolve" else {"selection": selection}
    response = h.client("coordinator").post("/api/bulk/" + endpoint, json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["error"] == {"code": "INVALID_FILTER", "message": "Choose dates for this filter", "details": None}

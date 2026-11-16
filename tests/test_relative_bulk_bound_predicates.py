"""Typed operands keep whole-record exclusions and included policy-version predicates."""
from .clock_support import freeze_clock
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk_linked_people import post
from .test_rule_catalogue_api import approve, line, propose


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("collection", ["sessions", "recurring_series"])
@pytest.mark.parametrize("op", ["is_not", "not_contains"])
def test_typed_negative_person_columns_exclude_every_matching_participant(request, database, collection, op):
    h = request.getfixturevalue(database)
    first, second = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, first).staff_number = "FIRST"
        db.get(m.Person, second).staff_number = "OTHER"
    if collection == "sessions":
        saved = h.shift(h.workspace(), people=[first, second])
        record_id = saved["shift_id"]
    else:
        record_id = h.configure("recurring_series", {"name": "Synthetic typed exclusion witnesses",
            "starts_on": "2026-10-01", "location_id": h.seed["locations"][0],
            "assignment_type_id": h.seed["type"], "definition": {"anchor": "2026-10-04",
                "days": [{"weekday": 6, "start_time": "08:00", "end_time": "12:00"}],
                "default_person_ids": [first, second], "service_ids": [h.seed["services"][0]]}})["record"]["id"]
    selection = {"collection": collection, "where": {"as_of": "2026-10-04", "filters": {"id": [record_id]}}}
    for value, person, expected in (("FIRST", None, 0), ("OTHER", first, 0), ("ABSENT", first, 1)):
        leaves = [{"field": "person.staff_number", "op": op,
                   "value": {"kind": "absolute", "value": value}}]
        if person:
            leaves.append({"field": "person_ids", "op": "is", "value": person})
        selection["where"]["conditions"] = {"conditions": leaves}
        payload = {"selection": selection, "columns": ["person.staff_number"], "limit": 1}
        for _ in range(2):
            assert post(h, "resolve", selection)["count"] == post(h, "rows", payload)["count"] == expected
    selection["where"]["conditions"] = {"conditions": [
        {"field": "person_ids", "op": "is", "value": second},
        {"field": "person.staff_number", "op": "contains", "value": "FIRST"}]}
    assert post(h, "resolve", selection)["count"] == post(h, "rows", {"selection": selection})["count"] == 0


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("binding", ["flat", "literal", "wrapped"])
def test_included_line_is_bound_to_its_policy_version_and_complete_relative_predicate(request, database, binding, monkeypatch):
    h = request.getfixturevalue(database)
    freeze_clock(monkeypatch, lambda: datetime(2026, 12, 31, 6, tzinfo=timezone.utc))
    authored = line(ends_on="2027-01-01", numbers={"max": {"value": 3}})
    retired = approve(h, propose(h, "duties_per_week", [authored])["record"])
    active = approve(h, propose(h, "duties_per_week", [{**authored, "ends_on": "2027-01-02"}],
        policy_id=retired["id"], revision=retired["revision"])["record"])
    operand = {"kind": "absolute", "value": retired["id"]} if binding == "wrapped" else retired["id"]
    selection = {"collection": "policy_lines", "include_ids": [authored["id"]], "where": {
        "conditions": {"conditions": [{"field": "policy_id", "op": "is", "value": operand},
            {"field": "ends_on", "op": "is", "value": {"kind": "relative", "token": "today"}}]}}}
    if binding == "flat":
        selection["where"]["filters"] = {"policy_id": [retired["id"]]}
        selection["where"]["conditions"]["conditions"].pop(0)
    for selected in ({key: value for key, value in selection.items() if key != "include_ids"}, selection):
        result = post(h, "resolve", selected)
        assert result["count"] == 1
        assert [(row["policy_id"], row["id"]) for row in result["sample"]] == [(retired["id"], authored["id"])]
        payload = {"selection": selected, "columns": ["ends_on"], "limit": 1}
        page = post(h, "rows", payload)
        assert page["count"] == 1 and page["rows"][0]["policy_id"] == retired["id"]
        assert all(row["policy_id"] != active["id"] for row in page["rows"])
        assert post(h, "rows", {**payload, "offset": 1})["rows"] == []
        assert post(h, "rows", payload) == page
    no_match = deepcopy(selection)
    no_match["where"]["conditions"]["conditions"][-1]["value"]["token"] = "tomorrow"
    assert post(h, "resolve", no_match)["count"] == post(h, "rows", {"selection": no_match})["count"] == 0

"""Relative operands retain the merged bulk version, participant and search semantics."""
from .clock_support import freeze_clock
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk_linked_people import post
from .test_bulk_literal_search import search_records
from .test_rule_catalogue_api import approve, line, propose


def relative(token):
    return {"kind": "relative", "token": token}


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_included_line_versions_apply_complete_relative_and_empty_or_predicate(request, database, monkeypatch):
    from packages.domain.rule_lines import scoring_default_id

    h = request.getfixturevalue(database)
    freeze_clock(monkeypatch, lambda: datetime(2026, 12, 31, 6, tzinfo=timezone.utc))
    seeded = post(h, "rows", {"selection": {"collection": "policy_lines", "where": {"conditions": {
        "conditions": [{"field": "status", "op": "not_empty"}]}}}, "columns": ["catalogue_id", "ends_on"]})
    assert seeded["count"] == 1
    [fair_share] = seeded["rows"]
    assert fair_share["id"] == scoring_default_id(h.seed["department_id"], "workload")
    assert (fair_share["catalogue_id"], fair_share["status"], fair_share["ends_on"]) == ("fair_share", "active", None)
    authored = line(note="", ends_on="2027-01-01", numbers={"max": {"value": 3}})
    retired = approve(h, propose(h, "duties_per_week", [authored])["record"])
    active = approve(h, propose(h, "duties_per_week", [{**authored, "note": "Present", "ends_on": "2027-01-02"}],
        policy_id=retired["id"], revision=retired["revision"])["record"])
    proposed = propose(h, "duties_per_week", [{**authored, "ends_on": None}],
        policy_id=active["id"], revision=active["revision"])["record"]
    selection = {"collection": "policy_lines", "include_ids": [authored["id"]], "where": {
        "conditions": {"match": "any", "conditions": [
            {"conditions": [{"field": "status", "op": "is", "value": "ACTIVE"},
                {"field": "ends_on", "op": "is", "value": relative("tomorrow")}]},
            {"conditions": [{"field": "note", "op": "empty"},
                {"field": "ends_on", "op": "between", "value": [relative("yesterday"), "2026-12-31"]}]}]}}}
    expected = {retired["id"], active["id"]}
    result = post(h, "resolve", selection)
    assert result["count"] == 2 and {row["policy_id"] for row in result["sample"]} == expected
    payload = {"selection": selection, "columns": ["note", "ends_on"], "limit": 1}
    pages = [post(h, "rows", {**payload, "offset": offset}) for offset in (0, 1, 2, 0)]
    assert all(page["count"] == 2 for page in pages)
    assert {row["policy_id"] for page in pages[:2] for row in page["rows"]} == expected
    assert pages[2]["rows"] == [] and pages[0] == pages[3]
    for op in ("empty", "not_empty"):
        selected = deepcopy(selection)
        selected["where"]["conditions"] = {"conditions": [{"field": "status", "op": op}]}
        expected_rows = ({(policy["id"], authored["id"]) for policy in (retired, active, proposed)} |
            {(fair_share["policy_id"], fair_share["id"])}) if op == "not_empty" else set()
        resolved = post(h, "resolve", selected)
        page = post(h, "rows", {"selection": selected})
        assert resolved["count"] == page["count"] == len(expected_rows)
        assert {(row["policy_id"], row["id"]) for row in resolved["sample"]} == expected_rows
        assert {(row["policy_id"], row["id"]) for row in page["rows"]} == expected_rows


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_relative_person_aliases_keep_one_participant_witness_and_aggregate_negatives(request, database, monkeypatch):
    h = request.getfixturevalue(database)
    freeze_clock(monkeypatch, lambda: datetime(2026, 12, 31, 6, tzinfo=timezone.utc))
    first, second = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["coordinator"]).person_id = first
        db.get(m.Person, first).staff_number = ""
        db.get(m.Person, second).staff_number = "OTHER-PARTICIPANT"
    row = h.configure("recurring_series", {"name": "Synthetic relative witnesses", "starts_on": "2026-10-01",
        "ends_on": "2027-01-01", "location_id": h.seed["locations"][0], "assignment_type_id": h.seed["type"],
        "definition": {"anchor": "2026-10-04", "days": [{"weekday": 6, "start_time": "08:00", "end_time": "12:00"}],
            "default_person_ids": [first, second], "service_ids": [h.seed["services"][0]]}})["record"]
    leaves = [{"field": "person.display_name", "op": "is", "value": relative("me")},
        {"field": "ends_on", "op": "within", "value": relative("this_year")},
        {"field": "status", "op": "is", "value": {"kind": "absolute", "value": "Active"}}]
    for person_leaf, expected in (({"field": "person.staff_number", "op": "contains", "value": "OTHER"}, 0),
                                   ({"field": "person.staff_number", "op": "empty"}, 1)):
        selection = {"collection": "recurring_series", "where": {"filters": {"id": [row["id"]]},
            "conditions": {"conditions": [*leaves, person_leaf]}}}
        payload = {"selection": selection, "columns": ["person.display_name", "person.staff_number"], "limit": 1}
        for _ in range(2):
            assert post(h, "resolve", selection, "coordinator")["count"] == post(h, "rows", payload, "coordinator")["count"] == expected
    selection["where"]["conditions"] = {"conditions": [{**leaves[0], "op": "is_not"}]}
    assert post(h, "resolve", selection, "coordinator")["count"] == post(h, "rows", {"selection": selection}, "coordinator")["count"] == 0


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_relative_linked_person_condition_retains_literal_sql_search_and_scalar_projection(request, database):
    h = request.getfixturevalue(database)
    collection, ids = search_records(h, "linked_person", "Merge%Person")
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["coordinator"]).person_id = h.seed["people"][0]
    selection = {"collection": collection, "where": {"q": "MERGE%PERSON", "filters": {"id": ids},
        "conditions": {"conditions": [{"field": "person.display_name", "op": "is", "value": relative("me")}]}}}
    assert [row["id"] for row in post(h, "resolve", selection, "coordinator")["sample"]] == ids[:1]
    payload = {"selection": selection, "columns": ["person.display_name"], "limit": 1}
    page = post(h, "rows", payload, "coordinator")
    assert page["count"] == 1 and page["rows"][0]["id"] == ids[0]
    assert page["rows"][0]["person.display_name"] == "Merge%Person"
    assert post(h, "rows", payload, "coordinator") == page

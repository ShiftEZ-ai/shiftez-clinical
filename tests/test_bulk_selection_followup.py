"""Explicit ticks and Sheet columns use the same advertised collection contract."""
import uuid

import pytest

from apps.api.db import transaction
from apps.api.services import bulk_selection as bulk
from packages.contracts import schemas as s
from .test_bulk_selection import actor


def resolve(h, selection):
    response = h.client("admin").post("/api/bulk/resolve", json=selection)
    assert response.status_code == 200, response.text
    return response.json()


def ids(result):
    return {row["id"] for row in result["sample"]}


@pytest.mark.parametrize("collection,seed_key", [("people", "people"), ("services", "services")])
def test_include_ids_alone_selects_exactly_those_ids(h, collection, seed_key):
    chosen = h.seed[seed_key][0]
    assert ids(resolve(h, {"collection": collection, "include_ids": [chosen]})) == {chosen}
    assert resolve(h, {"collection": collection, "include_ids": []})["count"] == 0
    assert resolve(h, {"collection": collection})["count"] > 1


@pytest.mark.parametrize("criteria", [
    {"names": ["Staff 01(test)"]},
    {"where": {"q": "Staff 01(test)"}},
    {"where": {"filters": {"display_name": ["Staff 01(test)"]}}},
])
def test_include_ids_add_to_matches_and_exclude_ids_remove_from_union(h, criteria):
    first, second = h.seed["people"][:2]
    selection = {"collection": "people", **criteria, "include_ids": [second]}
    assert ids(resolve(h, selection)) == {first, second}
    assert ids(resolve(h, {**selection, "exclude_ids": [first]})) == {second}
    assert ids(resolve(h, {**selection, "exclude_ids": [second]})) == {first}


def test_id_filter_is_exact_and_include_ids_add_to_it(h):
    first, second = h.seed["people"][:2]
    selection = {"collection": "people", "where": {"filters": {"id": [first]}}}
    assert ids(resolve(h, selection)) == {first}
    assert ids(resolve(h, {**selection, "include_ids": [second]})) == {first, second}
    assert resolve(h, {"collection": "people", "where": {"filters": {"id": []}}})["count"] == 0


def test_include_ids_add_outside_parent_matches_but_preserve_parent_counts(h):
    first, second = h.seed["people"][:2]
    all_rows = resolve(h, {"collection": "eligibilities"})["sample"]
    first_ids = {row["id"] for row in all_rows if row["person_id"] == first}
    outside = next(row["id"] for row in all_rows if row["person_id"] == second)
    selection = {"collection": "eligibilities", "parent": {"collection": "people", "selection": {
        "collection": "people", "include_ids": [first]}}, "include_ids": [outside]}
    found = resolve(h, selection)
    assert ids(found) == first_ids | {outside}
    assert found["by_parent"] == {first: len(first_ids)}
    assert ids(resolve(h, {**selection, "exclude_ids": [outside]})) == first_ids


def rule_lines(h):
    lines = [{"id": str(uuid.uuid4()), "who": {"conditions": [{
        "field": "person_ids", "op": "any", "values": [person]}]},
        "numbers": {"max": {"value": 3}}} for person in h.seed["people"][:2]]
    proposal = h.command("coordinator", "rule.propose", {"catalogue_id": "duties_per_week", "lines": lines,
        "reason": "Synthetic exact line-selection contract"})["record"]
    return proposal, lines


def test_rule_lines_include_and_filter_use_line_ids_and_rows_keep_policy_id(h):
    policy, lines = rule_lines(h)
    from .test_rule_catalogue_api import approve
    policy = approve(h, policy)
    first, second = (line["id"] for line in lines)
    selection = {"collection": "policy_lines", "include_ids": [first]}
    found = resolve(h, selection)
    assert ids(found) == {first}
    assert found["sample"][0]["policy_id"] == policy["id"]
    assert resolve(h, {"collection": "policy_lines", "include_ids": [policy["id"]]})["count"] == 0
    selection = {"collection": "policy_lines", "where": {"filters": {"id": [first]}}, "include_ids": [second]}
    assert ids(resolve(h, selection)) == {first, second}
    assert ids(resolve(h, {**selection, "exclude_ids": [first]})) == {second}
    response = h.client("coordinator").post("/api/bulk/rows", json={"selection": {
        "collection": "policy_lines", "where": {"filters": {"id": [first]}}}, "columns": ["numbers"]})
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [{"id": first, "line_id": first, "policy_id": policy["id"],
        "revision": policy["revision"], "status": policy["status"],
        "numbers": policy["definition"]["rule"]["lines"][0]["numbers"]}]


@pytest.mark.parametrize("collection", tuple(s.BULK_COLLECTIONS))
def test_rows_accept_every_advertised_column_of_every_collection(h, collection):
    registry = s.BULK_COLLECTIONS[collection]
    columns = list(dict.fromkeys((*registry["columns"], *registry["fields"], *registry["related_columns"])))
    response = h.client("operator" if collection in {"history", "outbox"} else "admin").post("/api/bulk/rows", json={
        "selection": {"collection": collection}, "columns": columns, "limit": 2})
    assert response.status_code == 200, response.text
    assert all(set(columns) <= row.keys() for row in response.json()["rows"])


def test_sheet_policy_and_session_columns_return_saved_values(h):
    policy, lines = rule_lines(h)
    h.shift(h.workspace())
    with transaction(h.app.state.engine) as db:
        current = actor(db, h, "admin")
        policies = bulk.rows(db, current, {"selection": {"collection": "policies", "include_ids": [policy["id"]]},
            "columns": ["catalogue_id", "lines", "on"]})["rows"]
        sessions = bulk.rows(db, current, {"selection": {"collection": "sessions"},
            "columns": ["participants", "service_ids"]})["rows"]
    assert policies[0]["catalogue_id"] == "duties_per_week" and policies[0]["on"] is True
    assert {line["id"] for line in policies[0]["lines"]} == {line["id"] for line in lines}
    assert sessions[0]["participants"][0]["person_id"] == h.seed["people"][0]
    assert sessions[0]["service_ids"] == [h.seed["services"][0]]

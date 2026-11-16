"""Explicit line ticks add active owners without losing version or department bindings."""
from copy import deepcopy
from datetime import date

import pytest

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk_linked_people import post
from .test_rule_catalogue_api import approve, line, propose


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("binding", ["active", "proposed"])
@pytest.mark.parametrize("path", ["flat", "tree"])
def test_rule_line_include_overlay_adds_other_active_owners_and_preserves_explicit_version(request, database, binding, path):
    h = request.getfixturevalue(database)
    a = line(numbers={"max": {"value": 5}})
    active_a = approve(h, propose(h, "duties_per_week", [a])["record"])
    proposed_a = propose(h, "duties_per_week", [{**a, "numbers": {"max": {"value": 6}}}],
        policy_id=active_a["id"], revision=active_a["revision"])["record"]
    owner = active_a if binding == "active" else proposed_a
    b = line(numbers={"max": {"value": 10}})
    retired_b = approve(h, propose(h, "duties_per_month", [b])["record"])
    active_b = approve(h, propose(h, "duties_per_month", [{**b, "numbers": {"max": {"value": 11}}}],
        policy_id=retired_b["id"], revision=retired_b["revision"])["record"])
    propose(h, "duties_per_month", [{**b, "numbers": {"max": {"value": 12}}}],
        policy_id=active_b["id"], revision=active_b["revision"])
    hidden_line = line(numbers={"max": {"value": 20}})
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed["organisation_id"], name="Other synthetic department")
        db.add(other)
        db.flush()
        definition = deepcopy(active_b["definition"])
        definition["rule"]["lines"] = [hidden_line]
        db.add(m.Policy(department_id=other.id, name="Other department rule", kind="catalogue",
            definition=definition, status="active", starts_on=date(2020, 1, 1), owner="Synthetic owner",
            source="Synthetic scope regression", created_by=h.seed["identities"]["coordinator"]))
    selection = {"collection": "policy_lines", "where": {"filters": {
        "policy_id": [owner["id"]], "id": [a["id"]]}},
        "include_ids": [a["id"], b["id"], hidden_line["id"]]}
    if path == "tree":
        selection["where"] = {"conditions": {"match": "all", "conditions": [
            {"match": "any", "conditions": [{"field": "policy_id", "values": [owner["id"]]}]},
            {"field": "policy_id", "op": "none", "values": [
                proposed_a["id"] if binding == "active" else active_a["id"]]},
            {"field": "id", "values": [a["id"]]}]}}
    expected = {(owner["id"], a["id"]), (active_b["id"], b["id"])}
    resolved = post(h, "resolve", selection)
    assert resolved["count"] == 2
    assert {(r["policy_id"], r["id"]) for r in resolved["sample"]} == expected
    payload = {"selection": selection, "columns": ["numbers"], "sort": [{"field": "policy_id"}], "limit": 1}
    pages = [post(h, "rows", {**payload, "offset": offset}) for offset in (0, 1, 0)]
    assert pages[0] == pages[2]
    assert all(page["count"] == 2 for page in pages)
    assert {(r["policy_id"], r["id"]) for page in pages[:2] for r in page["rows"]} == expected
    assert {r["policy_id"]: r["numbers"]["max"]["value"] for page in pages[:2] for r in page["rows"]} == {
        owner["id"]: 5 if binding == "active" else 6, active_b["id"]: 11}
    for excluded, remaining in ((a["id"], active_b), (b["id"], owner)):
        result = post(h, "resolve", {**selection, "exclude_ids": [excluded]})
        assert result["count"] == 1 and result["sample"][0]["policy_id"] == remaining["id"]

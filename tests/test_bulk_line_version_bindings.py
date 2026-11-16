"""Included line IDs retain the status and policy-parent identity of the selection."""
from copy import deepcopy

import pytest

from .test_bulk import REASON, preview
from .test_bulk_linked_people import post
from .test_rule_catalogue_api import approve, line, propose


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("status", ["retired", "active", "proposed"])
@pytest.mark.parametrize("path", ["flat", "tree", "parent"])
def test_included_rule_lines_keep_status_and_parent_version_in_resolve_rows_and_preview(request, database, status, path):
    h = request.getfixturevalue(database)
    authored = line(numbers={"max": {"value": 3}})
    retired = approve(h, propose(h, "duties_per_week", [authored])["record"])
    active = approve(h, propose(h, "duties_per_week", [{**authored, "numbers": {"max": {"value": 4}}}],
        policy_id=retired["id"], revision=retired["revision"])["record"])
    proposed = propose(h, "duties_per_week", [{**authored, "numbers": {"max": {"value": 5}}}],
        policy_id=active["id"], revision=active["revision"])["record"]
    owner = {"retired": retired, "active": active, "proposed": proposed}[status]
    selection = {"collection": "policy_lines", "where": {"filters": {"id": [authored["id"]]}}}
    if path == "flat":
        selection["where"]["filters"]["status"] = [status]
    elif path == "tree":
        selection["where"]["conditions"] = {"match": "all", "conditions": [
            {"match": "any", "conditions": [{"field": "status", "values": [status]}]}]}
    else:
        selection["parent"] = {"collection": "policies", "selection": {
            "collection": "policies", "include_ids": [owner["id"]]}}
    for selected in (selection, {**selection, "include_ids": [authored["id"]]}):
        result = post(h, "resolve", selected)
        assert result["count"] == 1
        assert [(r["policy_id"], r["id"], r["status"]) for r in result["sample"]] == [
            (owner["id"], authored["id"], status)]
    selected = {**selection, "include_ids": [authored["id"]]}
    payload = {"selection": selected, "columns": ["numbers"], "limit": 1}
    first = post(h, "rows", payload)
    assert first["count"] == 1 and first["rows"][0]["policy_id"] == owner["id"]
    assert post(h, "rows", {**payload, "offset": 1})["rows"] == []
    assert post(h, "rows", payload) == first
    body = {"selection": selected, "item_kind": "rule.propose", "reason": REASON,
        "changes": [{"field": "numbers", "value": {"max": {"value": 6}}}]}
    checked = preview(h, "coordinator", body)
    assert len(checked["items"]) == 1 and checked["items"][0]["target_id"] == owner["id"]
    if status == "active":
        h.command("coordinator", "policy.decline", {"reason": REASON}, proposed["id"], proposed["revision"])
        checked = preview(h, "coordinator", body)
        assert checked["counts"]["waiting"] == 1 and not checked["items"][0]["issues"]
    elif path == "parent":
        incompatible = deepcopy(selected)
        incompatible["where"]["filters"]["status"] = ["active"]
        assert post(h, "resolve", incompatible)["count"] == 0
        assert post(h, "rows", {"selection": incompatible})["count"] == 0


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("retired_matches", [False, True])
def test_included_rule_lines_bind_versions_with_the_complete_mixed_or_predicate(request, database, retired_matches):
    h = request.getfixturevalue(database)
    authored = line(note="Absent" if retired_matches else "Present", numbers={"max": {"value": 3}})
    retired = approve(h, propose(h, "duties_per_week", [authored])["record"])
    active = approve(h, propose(h, "duties_per_week", [{**authored, "note": "Present", "numbers": {"max": {"value": 4}}}],
        policy_id=retired["id"], revision=retired["revision"])["record"])
    propose(h, "duties_per_week", [{**authored, "note": "Present", "numbers": {"max": {"value": 5}}}],
        policy_id=active["id"], revision=active["revision"])
    selection = {"collection": "policy_lines", "where": {"filters": {"id": [authored["id"]]}, "conditions": {"match": "any", "conditions": [
        {"field": "status", "values": ["active"]}, {"field": "note", "values": ["Absent"]}]}}}
    expected = {active["id"]} | ({retired["id"]} if retired_matches else set())
    for selected in (selection, {**selection, "include_ids": [authored["id"]]}):
        resolved = post(h, "resolve", selected)
        assert resolved["count"] == len(expected)
        assert {row["policy_id"] for row in resolved["sample"]} == expected
        payload = {"selection": selected, "columns": ["note", "numbers"], "limit": 1}
        pages = [post(h, "rows", {**payload, "offset": offset}) for offset in range(len(expected) + 1)]
        assert all(page["count"] == len(expected) for page in pages)
        assert {row["policy_id"] for page in pages for row in page["rows"]} == expected
        assert pages[-1]["rows"] == []
        assert post(h, "rows", payload) == pages[0]
        checked = preview(h, "coordinator", {"selection": selected, "item_kind": "rule.propose", "reason": REASON,
            "changes": [{"field": "numbers", "value": {"max": {"value": 6}}}]})
        assert len(checked["items"]) == len(expected)
        assert {item["target_id"] for item in checked["items"]} == expected


@pytest.mark.parametrize("path", ["flat", "tree"])
def test_cross_policy_line_overlay_keeps_the_requested_status(h, path):
    a, b = line(numbers={"max": {"value": 3}}), line(numbers={"max": {"value": 10}})
    first = approve(h, propose(h, "duties_per_week", [a])["record"])
    retired = approve(h, propose(h, "duties_per_month", [b])["record"])
    approve(h, propose(h, "duties_per_month", [{**b, "numbers": {"max": {"value": 11}}}],
        policy_id=retired["id"], revision=retired["revision"])["record"])
    selection = {"collection": "policy_lines", "where": {"filters": {
        "policy_id": [first["id"]], "id": [a["id"]]}}, "include_ids": [b["id"]]}
    if path == "flat":
        selection["where"]["filters"]["status"] = ["retired"]
    else:
        selection["where"]["conditions"] = {"match": "all", "conditions": [
            {"match": "any", "conditions": [{"field": "status", "op": "none", "values": ["active", "proposed"]}]}]}
    resolved = post(h, "resolve", selection)
    assert resolved["count"] == 1 and resolved["sample"][0]["policy_id"] == retired["id"]
    page = post(h, "rows", {"selection": selection, "columns": ["numbers"]})
    assert page["count"] == 1 and page["rows"][0]["policy_id"] == retired["id"]
    assert page["rows"][0]["status"] == "retired"

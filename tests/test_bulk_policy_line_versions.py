"""A saved line identity is bound to the policy version its caller selected."""
from copy import deepcopy

import pytest

from .test_bulk import REASON, preview
from .test_rule_catalogue_api import approve, line, propose


def versions(h):
    lines = [line(numbers={"max": {"value": value}}) for value in (5, 9)]
    for authored, person in zip(lines, h.seed["people"]):
        authored["who"] = {"conditions": [{"field": "person_ids", "values": [person]}]}
    original = approve(h, propose(h, "duties_per_week", lines)["record"])
    changed = deepcopy(original["definition"]["rule"]["lines"])
    changed[0]["numbers"]["max"]["value"] = 6
    current = approve(h, propose(h, "duties_per_week", changed,
        policy_id=original["id"], revision=original["revision"])["record"])
    return original, current, changed


def resolved_and_rows(h, selection):
    response = h.client("coordinator").post("/api/bulk/resolve", json=selection)
    assert response.status_code == 200, response.text
    found = response.json()
    payload = {"selection": selection, "columns": ["numbers"], "limit": 1}
    pages = [h.client("coordinator").post("/api/bulk/rows", json={**payload, "offset": offset})
             for offset in (0, 1, 0)]
    assert all(page.status_code == 200 for page in pages), [page.text for page in pages]
    assert all(page.json()["count"] == found["count"] for page in pages)
    assert pages[0].json() == pages[2].json()
    return found, pages[0].json(), pages[1].json()


@pytest.mark.parametrize("path", ["include", "filter", "tree"])
def test_line_ids_select_only_the_active_version_after_replacement(h, path):
    old, current, lines = versions(h)
    propose(h, "duties_per_week", lines, policy_id=current["id"], revision=current["revision"])
    chosen = lines[0]["id"]
    selection = {"collection": "policy_lines", **({"include_ids": [chosen]} if path == "include" else {
        "where": {"filters": {"id": [chosen]}}} if path == "filter" else {
        "where": {"conditions": {"conditions": [{"field": "id", "values": [chosen]}]}}})}
    found, first, last = resolved_and_rows(h, selection)
    assert found["count"] == 1 and not last["rows"]
    assert found["sample"][0]["policy_id"] == first["rows"][0]["policy_id"] == current["id"]
    assert first["rows"][0]["id"] == chosen
    assert first["rows"][0]["numbers"]["max"]["value"] == 6


@pytest.mark.parametrize("version", ["retired", "active", "proposed"])
def test_policy_id_and_line_id_filters_are_conjunctive_and_preserve_version(h, version):
    old, current, lines = versions(h)
    waiting = propose(h, "duties_per_week", lines,
        policy_id=current["id"], revision=current["revision"])["record"]
    intended = {"retired": old, "active": current, "proposed": waiting}[version]
    selection = {"collection": "policy_lines", "where": {"filters": {
        "policy_id": [intended["id"]], "id": [lines[0]["id"]]}}}
    found, first, last = resolved_and_rows(h, selection)
    assert found["count"] == 1 and not last["rows"]
    assert found["sample"][0]["policy_id"] == first["rows"][0]["policy_id"] == intended["id"]
    assert first["rows"][0]["status"] == version
    assert first["rows"][0]["id"] == lines[0]["id"]
    assert resolved_and_rows(h, {**selection, "where": {"filters": {
        "policy_id": [intended["id"]], "id": [h.seed["people"][0]]}}})[0]["count"] == 0


def test_version_bound_line_change_previews_and_applies_one_current_proposal(h):
    old, current, lines = versions(h)
    body = {"item_kind": "rule.propose", "selection": {"collection": "policy_lines", "where": {
        "filters": {"policy_id": [current["id"]], "id": [lines[0]["id"]]}}},
        "changes": [{"field": "note", "how": "set", "value": "Synthetic version-bound change"}], "reason": REASON}
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["waiting"] == 1 and checked["counts"]["problem"] == 0
    response = h.client("coordinator").post("/api/commands", json={"kind": "bulk.apply", "payload": body,
        "expected_fingerprint": checked["fingerprint"]}, headers={"idempotency-key": current["id"]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["count"] == 1
    saved = result["items"][0]["results"][0]["record"]["definition"]["rule"]["lines"]
    by_id = {authored["id"]: authored for authored in saved}
    assert by_id[lines[0]["id"]]["note"] == "Synthetic version-bound change"
    assert by_id[lines[1]["id"]] == lines[1]


def test_explicit_retired_status_can_read_a_historical_line_id(h):
    old, _, lines = versions(h)
    found, _, _ = resolved_and_rows(h, {"collection": "policy_lines", "where": {
        "filters": {"id": [lines[0]["id"]], "status": ["retired"]}}})
    assert found["count"] == 1 and found["sample"][0]["policy_id"] == old["id"]

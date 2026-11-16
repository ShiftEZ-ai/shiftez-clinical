"""A suggested repair must preserve the earlier line for people outside the change."""
from copy import deepcopy

import pytest

from .test_bulk import REASON, preview
from .test_bulk_rule_lines import _active, _body, _pending
from .test_rule_catalogue_api import approve, line, person


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("dimension", ["person", "rank", "tag"])
def test_shared_personal_line_cannot_be_ended_by_a_partial_person_rank_or_tag_change(request, database, dimension):
    h = request.getfixturevalue(database)
    selected, other = h.seed["people"][:2]
    if dimension == "person":
        condition = person(selected)
    elif dimension == "rank":
        rank = h.configure("categories", {"name": "Selected rank", "starts_on": "2020-01-01"})["record"]
        h.command("admin", "membership.create", {"person_id": selected, "category_id": rank["id"],
                  "starts_on": "2026-10-01"})
        condition = {"conditions": [{"field": "category_ids", "values": [rank["id"]]}]}
    else:
        tag = h.configure("tags", {"name": "Selected people", "labels": ["people"]})["record"]
        h.command("coordinator", "tag.members", {"add": [{"kind": "people", "id": selected}], "reason": REASON},
                  tag["id"], tag["revision"])
        condition = {"conditions": [{"field": "tags", "values": [tag["id"]]}]}
    existing = line(who={"conditions": [{"field": "person_ids", "values": [selected, other]}]},
                    numbers={"max": {"value": 5}}, starts_on="2026-10-01")
    policy = _active(h, [existing])
    body = _body(policy, [{"who": condition, "numbers": {"max": {"value": 3}}, "starts_on": "2026-11-01"}])
    issue = preview(h, "coordinator", body)["items"][0]["issues"][0]
    assert issue["code"] == "OVERLAPS_UNACKNOWLEDGED"
    assert [option["id"] for option in issue["resolutions"]] == ["keep_previous_lines", "skip"]
    # A cached or forged choice must also be rejected when resolutions are re-derived.
    unavailable = {**deepcopy(body), "resolutions": {issue["group_key"]: "end_previous_lines"}}
    refused = preview(h, "coordinator", unavailable)["items"][0]
    assert refused["result"] == "problem" and refused["issues"][0]["code"] == "BULK_RESOLUTION_STALE"
    assert h.command("coordinator", "bulk.apply", unavailable, status=409)["error"]["code"] == "BULK_RESOLUTION_STALE"
    assert _pending(h) == []
    body["resolutions"] = {issue["group_key"]: "keep_previous_lines"}
    assert preview(h, "coordinator", body)["items"][0]["result"] == "waiting"
    proposed = h.command("coordinator", "bulk.apply", body)["items"][0]["results"][0]["record"]
    kept = next(old for old in proposed["definition"]["rule"]["lines"] if old["id"] == existing["id"])
    assert kept.get("ends_on") is None
    active = approve(h, proposed)
    for day in ("2026-11-01", "2027-01-01"):
        rules = h.get("coordinator", f"/api/policies/applies?person_id={other}&day={day}")["rules"]
        assert [(rule["own_line"]["id"], rule["rule"]["maximum"]) for rule in rules
                if rule["policy_id"] == active["id"]] == [(existing["id"], 5)]


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("dimension", ["rank", "tag", "person_and_rank", "person_and_tag", "person_superset"])
def test_personal_limit_survives_when_the_new_group_stops_covering_the_person(request, database, dimension):
    h = request.getfixturevalue(database)
    who = h.seed["people"][0]
    tag = None
    if "rank" in dimension:
        rank = h.configure("categories", {"name": "Dated repair rank", "starts_on": "2020-01-01"})["record"]
        h.command("admin", "membership.create", {"person_id": who, "category_id": rank["id"],
                  "starts_on": "2026-10-01", "ends_on": "2027-01-01"})
        condition = {"field": "category_ids", "values": [rank["id"]]}
    elif "tag" in dimension:
        tag = h.configure("tags", {"name": "Changing repair tag", "labels": ["people"]})["record"]
        tag = h.command("coordinator", "tag.members", {"add": [{"kind": "people", "id": who}], "reason": REASON},
                        tag["id"], tag["revision"])["record"]
        condition = {"field": "tags", "values": [tag["id"]]}
    else:
        condition = {"field": "person_ids", "values": h.seed["people"][:2]}
    conditions = [condition]
    if dimension.startswith("person_and_"):
        conditions.append({"field": "person_ids", "values": [who]})
    existing = line(who=person(who), numbers={"max": {"value": 5}}, starts_on="2026-10-01")
    policy = _active(h, [existing])
    body = _body(policy, [{"who": {"conditions": conditions}, "numbers": {"max": {"value": 3}},
                           "starts_on": "2026-11-01"}])
    issue = preview(h, "coordinator", body)["items"][0]["issues"][0]
    assert issue["code"] == "OVERLAPS_UNACKNOWLEDGED"
    assert [option["id"] for option in issue["resolutions"]] == ["keep_previous_lines", "skip"]
    unavailable = {**deepcopy(body), "resolutions": {issue["group_key"]: "end_previous_lines"}}
    refused = preview(h, "coordinator", unavailable)["items"][0]
    assert refused["result"] == "problem" and refused["issues"][0]["code"] == "BULK_RESOLUTION_STALE"
    assert h.command("coordinator", "bulk.apply", unavailable, status=409)["error"]["code"] == "BULK_RESOLUTION_STALE"
    assert _pending(h) == []
    body["resolutions"] = {issue["group_key"]: "keep_previous_lines"}
    assert preview(h, "coordinator", body)["items"][0]["result"] == "waiting"
    proposed = h.command("coordinator", "bulk.apply", body)["items"][0]["results"][0]["record"]
    kept = next(old for old in proposed["definition"]["rule"]["lines"] if old["id"] == existing["id"])
    assert kept.get("ends_on") is None
    active = approve(h, proposed)
    if tag:
        h.command("coordinator", "tag.members", {"remove": [{"kind": "people", "id": who}], "reason": REASON},
                  tag["id"], tag["revision"])
    for day in ("2026-12-01", "2027-02-01"):
        rules = h.get("coordinator", f"/api/policies/applies?person_id={who}&day={day}")["rules"]
        assert [(rule["own_line"]["id"], rule["rule"]["maximum"]) for rule in rules
                if rule["policy_id"] == active["id"]] == [(existing["id"], 5)]

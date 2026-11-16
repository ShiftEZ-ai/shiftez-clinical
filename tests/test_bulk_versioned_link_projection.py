"""Line projections and pasted-name identities remain specific to their policy version."""
from copy import deepcopy

import pytest

from .test_bulk_linked_people import people_and_accounts, post
from .test_rule_catalogue_api import approve, line, propose


@pytest.mark.parametrize("mode", ["related", "person", "paste", "search"])
def test_shared_line_id_keeps_version_specific_people_through_filter_matching_and_cache(h, mode):
    people, _, _ = people_and_accounts(h)
    authored = line(who={"conditions": [{"field": "person_ids", "values": [people[0]]}]},
        numbers={"max": {"value": 3}})
    active = approve(h, propose(h, "duties_per_week", [authored])["record"])
    changed = deepcopy(authored)
    changed["who"]["conditions"][0]["values"] = [people[1]]
    waiting = propose(h, "duties_per_week", [changed], policy_id=active["id"], revision=active["revision"])["record"]
    selection = {"collection": "policy_lines", "where": {"filters": {"policy_id": [active["id"], waiting["id"]]}}}
    names = {active["id"]: "Synthetic Alex", waiting["id"]: "Synthetic Blair"}
    if mode == "related":
        selection["where"]["filters"]["person.display_name"] = [names[active["id"]]]
    elif mode == "person":
        selection["where"]["conditions"] = {"conditions": [{"field": "person_ids", "values": [people[0]]}]}
    elif mode == "paste":
        selection["names"] = [names[active["id"]]]
    else:
        selection["where"]["q"] = names[active["id"]]
    resolved = post(h, "resolve", selection)
    assert resolved["count"] == 1 and resolved["sample"][0]["policy_id"] == active["id"]
    if mode == "paste":
        assert resolved["matches"] == [{"line": names[active["id"]], "ids": [authored["id"]]}]
    payload = {"selection": selection, "columns": ["person.display_name"],
        "sort": [{"field": "person.display_name"}], "group": "person.display_name", "limit": 1}
    for _ in range(2):
        page = post(h, "rows", payload)
        assert page["count"] == 1 and page["rows"][0]["person.display_name"] == names[active["id"]]
        assert page["rows"][0]["policy_id"] == active["id"]
        assert page["groups"] == [{"value": names[active["id"]], "count": 1}]
    selection["where"]["filters"] = {"policy_id": [active["id"], waiting["id"]]}
    selection["where"].pop("conditions", None)
    selection["where"].pop("q", None)
    selection.pop("names", None)
    pages = [post(h, "rows", {**payload, "offset": offset}) for offset in (0, 1, 0)]
    assert pages[0] == pages[2]
    assert {row["policy_id"]: row["person.display_name"] for page in pages[:2] for row in page["rows"]} == names


@pytest.mark.parametrize("scope", ["direct", "group", "none", "tag", "duty_type", "everyone", "empty"])
def test_rule_line_clinics_share_where_evaluation_for_related_filters_sort_group_and_parent(h, scope):
    services = h.seed["services"][:2]
    expected = {services[0]}
    where = {"conditions": [{"field": "service_ids", "values": [services[0]]}]}
    if scope == "group":
        where = {"match": "any", "conditions": [where, {"conditions": [
            {"field": "service_ids", "values": [services[1]]}]}]}
        expected = set(services)
    elif scope == "none":
        where["conditions"][0]["op"] = "none"
        expected = set(h.seed["services"]) - {services[0]}
    elif scope == "tag":
        tag = h.configure("tags", {"name": "Synthetic line clinics", "labels": ["services"]})["record"]
        h.command("admin", "tag.members", {"add": [{"kind": "services", "id": services[0]}],
            "remove": [], "reason": "Synthetic clinic scope"}, tag["id"], tag["revision"])
        where["conditions"][0]["values"] = [tag["id"]]
    elif scope == "duty_type":
        where["conditions"].append({"field": "assignment_type_ids", "values": [h.seed["type"]]})
    elif scope == "everyone":
        where, expected = None, set(h.seed["services"])
    elif scope == "empty":
        where["conditions"].append({"field": "service_ids", "op": "none", "values": [services[0]]})
        expected = set()
    authored = line(where=where, numbers={"max": {"value": 3}})
    policy = propose(h, "duties_per_week", [authored])["record"]
    selection = {"collection": "policy_lines", "where": {"filters": {"policy_id": [policy["id"]]}}}
    clinics = post(h, "resolve", {"collection": "services", "include_ids": sorted(expected)})["sample"]
    payload = {"selection": selection, "columns": ["service.name", "service.tags", "service.location_ids"],
        "sort": [{"field": "service.name"}], "group": "service.name"}
    first = post(h, "rows", payload)
    names = first["rows"][0]["service.name"]
    assert set(names if isinstance(names, list) else [names] if names is not None else []) == {r["name"] for r in clinics}
    assert first == post(h, "rows", payload)
    for clinic in services:
        filtered = deepcopy(selection)
        filtered["where"]["conditions"] = {"conditions": [{"field": "service_ids", "values": [clinic]}]}
        assert post(h, "resolve", filtered)["count"] == post(h, "rows", {"selection": filtered})["count"] == int(clinic in expected)
        parent = {**selection, "parent": {"collection": "services", "selection": {"collection": "services", "include_ids": [clinic]}}}
        assert post(h, "resolve", parent)["count"] == int(clinic in expected)

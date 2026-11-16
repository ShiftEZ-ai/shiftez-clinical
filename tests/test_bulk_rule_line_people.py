"""Rule-line people are the dated matches of Who, rather than its literal values."""
from copy import deepcopy

import pytest

from .test_bulk_linked_people import people_and_accounts, post
from .test_rule_catalogue_api import line, propose


def authored(h, who):
    value = line(numbers={"max": {"value": 3}})
    value["who"] = who
    record = propose(h, "duties_per_week", [value])["record"]
    return {"collection": "policy_lines", "where": {"as_of": "2026-10-04",
        "filters": {"policy_id": [record["id"]], "id": [value["id"]]}}}, value["id"]


@pytest.mark.parametrize("scope", ["person", "rank", "programme", "group", "none", "everyone", "empty"])
def test_line_related_people_resolve_filter_project_and_page_the_shared_who_matches(h, scope):
    people, values, _ = people_and_accounts(h)
    field = {"person": "person_ids", "rank": "category_ids", "programme": "programme_ids"}.get(scope, "person_ids")
    who = {"conditions": [{"field": field, "values": [values[field]]}]}
    expected = {people[0]}
    if scope == "group":
        who = {"match": "any", "conditions": [who, {"conditions": [
            {"field": "person_ids", "values": [people[1]]}]}]}
        expected = set(people)
    elif scope == "none":
        who["conditions"][0]["op"] = "none"
        expected = set(h.seed["people"]) | {people[1]}
    elif scope == "everyone":
        who = None
        expected = set(h.seed["people"]) | set(people)
    elif scope == "empty":
        who = {"conditions": [{"field": "tags", "values": ["Nobody has this tag"]}]}
        expected = set()
    selection, line_id = authored(h, who)
    columns = ["person.display_name", "person.category_ids"]
    page = post(h, "rows", {"selection": selection, "columns": columns, "limit": 1})
    assert page["count"] == 1 and page["rows"][0]["id"] == line_id
    names = page["rows"][0]["person.display_name"]
    names = names if isinstance(names, list) else [names] if names is not None else []
    resolved_people = post(h, "resolve", {"collection": "people", "include_ids": sorted(expected)})
    assert set(names) == {p["display_name"] for p in resolved_people["sample"]}
    assert page == post(h, "rows", {"selection": selection, "columns": columns, "limit": 1})
    assert not post(h, "rows", {"selection": selection, "columns": columns, "offset": 1, "limit": 1})["rows"]
    for person in people:
        filtered = deepcopy(selection)
        filtered["where"]["conditions"] = {"conditions": [{"field": "person_ids", "values": [person]}]}
        assert post(h, "resolve", filtered)["count"] == post(h, "rows", {"selection": filtered})["count"] == int(person in expected)
    selected = deepcopy(selection)
    selected["where"]["filters"]["person.category_ids"] = [values["category_ids"]]
    assert post(h, "resolve", selected)["count"] == int(people[0] in expected)


def test_line_related_rank_programme_and_tag_match_at_as_of_with_cached_sort_and_groups(h):
    people, values, _ = people_and_accounts(h)
    tag = h.configure("tags", {"name": "Synthetic linked line", "labels": ["people"]})["record"]
    h.command("admin", "tag.members", {"add": [{"kind": "people", "id": people[0]}], "remove": [],
        "reason": "Synthetic linked people"}, tag["id"], tag["revision"])
    selection, line_id = authored(h, {"match": "all", "conditions": [
        {"field": "tags", "values": [tag["id"]]}, {"field": "category_ids", "values": [values["category_ids"]]},
        {"field": "programme_ids", "values": [values["programme_ids"]]}]})
    selection["where"]["conditions"] = {"conditions": [{"field": "person_ids", "values": [people[0]]}]}
    payload = {"selection": selection, "columns": ["person.display_name", "person.category_ids"],
        "sort": [{"field": "person.display_name"}], "group": "person.category_ids", "limit": 1}
    page = post(h, "rows", payload)
    assert page["count"] == 1 and page["rows"][0]["person.category_ids"] == [values["category_ids"]]
    assert page["groups"] == [{"value": [values["category_ids"]], "count": 1}]
    assert page == post(h, "rows", payload)
    for day in ("2026-10-03", "2026-11-01"):
        selection["where"]["as_of"] = day
        assert post(h, "resolve", selection)["count"] == post(h, "rows", payload)["count"] == 0

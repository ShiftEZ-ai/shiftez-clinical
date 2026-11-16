"""Ending a line must not discard its other numeric slots or authored levels."""
from copy import deepcopy

import pytest

from .test_bulk import preview
from .test_bulk_rule_lines import _body, _pending
from .test_rule_catalogue_api import approve, line, person, propose


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("case", ["missing_min", "missing_max", "missing_target", "changed_level",
    "removed_value", "inherited_level", "full_coverage", "full_strengths", "full_inherited_level"])
def test_end_repair_requires_every_earlier_number_and_level(request, database, case):
    h = request.getfixturevalue(database)
    who = h.seed["people"][0]
    old_numbers = {"min": {"value": 2}, "max": {"value": 5}}
    new_numbers = {"min": {"value": 2}, "max": {"value": 3}}
    if case == "missing_min":
        del new_numbers["min"]
    elif case == "missing_max":
        del new_numbers["max"]
    elif case == "missing_target":
        old_numbers["target"] = {"value": 3, "level": "low"}
    elif case == "changed_level":
        old_numbers["min"]["level"] = "high"
        new_numbers["min"]["level"] = "medium"
    elif case == "removed_value":
        new_numbers["min"]["value"] = None
    elif case == "inherited_level":
        new_numbers["min"]["level"] = None
    elif case == "full_strengths":
        for numbers in (old_numbers, new_numbers):
            numbers["min"]["level"] = "high"
            numbers["target"] = {"value": 3, "level": "low"}
    elif case == "full_inherited_level":
        old_numbers["min"]["level"] = new_numbers["min"]["level"] = None
    existing = line(who=person(who), numbers=old_numbers, starts_on="2026-10-01", ends_on="2027-10-01")
    policy = approve(h, propose(h, "duties_per_month", [existing])["record"])
    body = _body(policy, [{"who": person(who), "numbers": new_numbers,
                           "starts_on": "2026-11-01", "ends_on": "2027-10-01"}])
    issue = preview(h, "coordinator", body)["items"][0]["issues"][0]
    assert issue["code"] == "OVERLAPS_UNACKNOWLEDGED"
    can_end = case.startswith("full_")
    options = {option["id"]: option for option in issue["resolutions"]}
    assert ("end_previous_lines" in options) == can_end
    assert "keep_previous_lines" in options
    if can_end:
        assert options["end_previous_lines"]["best"]
    else:
        assert list(options) == ["keep_previous_lines", "skip"]
        unavailable = {**deepcopy(body), "resolutions": {issue["group_key"]: "end_previous_lines"}}
        refused = preview(h, "coordinator", unavailable)["items"][0]
        assert refused["result"] == "problem" and refused["issues"][0]["code"] == "BULK_RESOLUTION_STALE"
        assert h.command("coordinator", "bulk.apply", unavailable, status=409)["error"]["code"] == "BULK_RESOLUTION_STALE"
        assert _pending(h) == []
    body["resolutions"] = {issue["group_key"]: "end_previous_lines" if can_end else "keep_previous_lines"}
    assert preview(h, "coordinator", body)["items"][0]["result"] == "waiting"
    proposed = h.command("coordinator", "bulk.apply", body)["items"][0]["results"][0]["record"]
    lines = proposed["definition"]["rule"]["lines"]
    kept = next(old for old in lines if old["id"] == existing["id"])
    assert kept.get("ends_on") == ("2026-11-01" if can_end else existing["ends_on"])
    expected = new_numbers if can_end else old_numbers
    if can_end:
        added = next(new for new in lines if new["id"] != existing["id"])
        assert added["numbers"] == {key: {"value": number["value"], "level": number.get("level", "mandatory")}
                                    for key, number in new_numbers.items()}
    active = approve(h, proposed)
    rules = h.get("coordinator", f"/api/policies/applies?person_id={who}&day=2026-12-01")["rules"]
    actual = {rule["own_line"]["slot"]: (rule["rule"]["amount"], rule["severity"])
              for rule in rules if rule["policy_id"] == active["id"]}
    assert actual == {key: (number["value"], "mandatory" if number.get("level", "mandatory") in (None, "mandatory") else "advisory")
                      for key, number in expected.items()}

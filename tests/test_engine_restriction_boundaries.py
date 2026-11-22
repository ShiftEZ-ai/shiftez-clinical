"""Restriction scopes apply to the matching part of a duty, including midnight changes."""
import copy

import pytest

from packages.contracts.schemas import PolicyDefinition, TypeDefinition
from packages.domain.conditions import conditions_match
from packages.domain.rule_lines import scope_of
from packages.domain.time import aware
from packages.domain.validation import evaluate, restriction_blocks


POLICY = "00000000-0000-4000-8000-000000000092"
QUOTA = "00000000-0000-4000-8000-000000000093"
SELECTED = "00000000-0000-4000-8000-000000000091"
START = "2026-10-04T22:00:00+03:00"
MIDNIGHT = "2026-10-05T00:00:00+03:00"
END = "2026-10-05T02:00:00+03:00"
FIELDS = ("programme_ids", "category_ids", "capability_ids", "rotation_type_ids", "cohort_ids", "eligible_service_ids")


def selected_scope(field, nested, count="together"):
    group = {"match": "all", "conditions": [{"field": field, "op": "any", "values": [SELECTED], "count": count}]}
    return scope_of({"who": {"match": "all", "conditions": [group]} if nested else group})


def policy(data, rule, scope, identifier=POLICY, **dates):
    return {"id": identifier, "revision": 1, "name": "Synthetic midnight restriction", "active": True,
            "status": "active", "department_id": data["types"][data["shifts"][0]["assignment_type_id"]]["department_id"],
            "starts_on": "2020-01-01", "ends_on": None, "kind": rule["kind"],
            "definition": PolicyDefinition.model_validate({"rule": rule, "scope": scope}).model_dump(mode="json"),
            **dates}


def dated_scope(data, person, field, transition):
    dates = {"starts_on": "2026-10-05" if transition == "join" else "2020-01-01",
             "ends_on": "2026-11-01" if transition == "join" else "2026-10-05"}
    department = data["types"][data["shifts"][0]["assignment_type_id"]]["department_id"]
    if field == "programme_ids":
        data["programmes"][SELECTED] = {"active": True, "starts_on": "2020-01-01", "ends_on": None}
        data["programme_memberships"] = [{"person_id": person, "programme_id": SELECTED, **dates}]
    elif field == "category_ids":
        data["categories"][SELECTED] = {"active": True}
        data["memberships"].append({"person_id": person, "department_id": department, "category_id": SELECTED, **dates})
    elif field == "capability_ids":
        data["capabilities"][SELECTED] = {"active": True}
        data["grants"].append({"person_id": person, "department_id": department, "capability_id": SELECTED,
                               "service_id": None, "revoked": False, **dates})
    elif field == "eligible_service_ids":
        data.setdefault("eligibilities", []).append({"person_id": person, "service_id": SELECTED, **dates})
    else:
        data.setdefault("rotation_types", {})[SELECTED] = {"active": True, "starts_on": "2020-01-01", "ends_on": None}
        data.setdefault("cohorts", {})[SELECTED] = {"active": True, **dates}
        data.setdefault("rotations", []).append({"person_id": person, "rotation_type_id": SELECTED,
            "cohort_id": SELECTED, **(dates if field == "rotation_type_ids" else {
                "starts_on": "2020-01-01", "ends_on": "2026-11-01"})})


def snapshot(h):
    workspace = h.shift(h.workspace(), START, END)["workspace"]
    return h.snapshot(workspace)


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("transition", ("join", "leave"))
def test_equivalent_dated_scopes_block_the_same_midnight_part(field, transition):
    shift = {"id": "duty", "assignment_type_id": "type", "service_ids": ["clinic"], "location_id": None}
    data = {"organisation": {"timezone": "Asia/Riyadh"}, "types": {"type": {"department_id": "department"}},
            "shifts": [shift], "people": {"person": {"tags": []}}, "categories": {}, "capabilities": {},
            "programmes": {}, "memberships": [], "grants": [], "programme_memberships": [], "exceptions": []}
    dated_scope(data, "person", field, transition)
    a, b = aware(START), aware(END)
    for nested in (False, True):
        data["policies"] = [policy(data, {"kind": "restriction"}, selected_scope(field, nested))]
        assert restriction_blocks(data, "person", shift, a, b), (field, transition, nested)
    # Low-level condition matching deliberately still requires membership throughout the interval.
    assert not conditions_match(data, selected_scope(field, True)["conditions"], "person", shift, a, b)


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("transition", ("join", "leave"))
@pytest.mark.parametrize("count", ("together", "each"))
def test_validator_clips_restrictions_to_the_matching_midnight_part(h, field, transition, count):
    data = snapshot(h)
    person = h.seed["people"][0]
    dated_scope(data, person, field, transition)
    expected = (MIDNIGHT, END) if transition == "join" else (START, MIDNIGHT)
    for nested in (False, True):
        candidate = copy.deepcopy(data)
        candidate["policies"] = [policy(candidate, {"kind": "restriction"}, selected_scope(field, nested, count))]
        report = evaluate(candidate)
        findings = [f for f in report["findings"] if f["code"] == "RESTRICTION" and f["rule_id"] == POLICY]
        assert [(aware(f["start"]), aware(f["end"])) for f in findings] == [tuple(map(aware, expected))]
        assert not report["valid"]


@pytest.mark.parametrize("field,records,definitions,key", [
    ("category_ids", "memberships", "categories", "category_id"),
    ("capability_ids", "grants", "capabilities", "capability_id"),
    ("programme_ids", "programme_memberships", "programmes", "programme_id"),
])
@pytest.mark.parametrize("transition", ("join", "leave"))
def test_definition_dates_split_unchanged_person_records(h, field, records, definitions, key, transition):
    data = snapshot(h)
    person = h.seed["people"][0]
    dated_scope(data, person, field, transition)
    row = next(r for r in data[records] if r["person_id"] == person and r.get(key) == SELECTED)
    data[definitions][SELECTED].update(starts_on=row["starts_on"], ends_on=row["ends_on"])
    row.update(starts_on="2020-01-01", ends_on="2026-11-01")
    data["policies"] = [policy(data, {"kind": "restriction"}, selected_scope(field, True))]
    expected = (MIDNIGHT, END) if transition == "join" else (START, MIDNIGHT)
    findings = [f for f in evaluate(data)["findings"] if f["code"] == "RESTRICTION" and f["rule_id"] == POLICY]
    assert [(aware(f["start"]), aware(f["end"])) for f in findings] == [tuple(map(aware, expected))]


@pytest.mark.parametrize("dates,scope_clock,rule_clock,transition,expected", [
    ({"starts_on": "2026-10-05"}, ("23:00", "01:00"), ("00:30", "02:00"), "join",
     ("2026-10-05T00:30:00+03:00", "2026-10-05T01:00:00+03:00")),
    ({"ends_on": "2026-10-05"}, ("23:00", "01:00"), ("22:30", "00:30"), "leave",
     ("2026-10-04T23:00:00+03:00", MIDNIGHT)),
    ({}, ("00:00", "24:00"), ("00:00", "02:00"), "leave", None),
    ({}, ("00:00", "24:00"), ("22:00", "24:00"), "join", None),
])
def test_restrictions_intersect_effective_dates_membership_and_clocks(h, dates, scope_clock, rule_clock,
                                                                    transition, expected):
    data = snapshot(h)
    person = h.seed["people"][0]
    dated_scope(data, person, "programme_ids", transition)
    for nested in (False, True):
        scope = {**selected_scope("programme_ids", nested), "start_time": scope_clock[0], "end_time": scope_clock[1]}
        candidate = copy.deepcopy(data)
        candidate["policies"] = [policy(candidate, {"kind": "restriction", "start_time": rule_clock[0],
                                                  "end_time": rule_clock[1]}, scope, **dates)]
        findings = [f for f in evaluate(candidate)["findings"] if f["code"] == "RESTRICTION" and f["rule_id"] == POLICY]
        assert [(aware(f["start"]), aware(f["end"])) for f in findings] == (
            [tuple(map(aware, expected))] if expected else [])
        assert restriction_blocks(candidate, person, candidate["shifts"][0], aware(START), aware(END)) == bool(expected)


@pytest.mark.parametrize("engine", ("cp_sat", "greedy"))
@pytest.mark.parametrize("nested", (False, True))
def test_neither_engine_fills_a_quota_with_an_overnight_restricted_duty(h, engine, nested):
    from packages.domain.cp_sat import generate as cp_sat_generate
    from packages.domain.generation import generate as greedy_generate

    data = snapshot(h)
    person = h.seed["people"][0]
    data["people"] = {person: data["people"][person]}
    data["memberships"] = [m for m in data["memberships"] if m["person_id"] == person]
    shift = data["shifts"][0]
    shift["participants"] = []
    definition = TypeDefinition(requirements=[{"minimum": 0, "maximum": 1}]).model_dump(mode="json")
    shift["type_snapshot"]["definition"] = definition
    data["types"][shift["assignment_type_id"]]["definition"] = definition
    data["quota_context"] = {"complete": True, "start": "2026-10-04T00:00:00+03:00",
                             "end": "2026-10-06T00:00:00+03:00", "publications": []}
    quota = policy(data, {"kind": "block_quota", "metric": "duties", "comparison": "minimum", "amount": 1,
                          "starts_on": "2026-10-04", "ends_on": "2026-10-06"}, {"person_id": person}, QUOTA)
    for field in FIELDS:
        for transition in ("join", "leave"):
            candidate = copy.deepcopy(data)
            dated_scope(candidate, person, field, transition)
            candidate["policies"] = [quota, policy(candidate, {"kind": "restriction"}, selected_scope(field, nested))]
            solved = (cp_sat_generate(candidate, 3, workers=1, feasibility_only=True) if engine == "cp_sat"
                      else greedy_generate(candidate, 3))
            assert not solved.get("additions"), (engine, field, transition, nested, solved)
            assert solved["status"] != "candidate_feasible", (engine, field, transition, nested, solved)


@pytest.mark.parametrize("weekday,expected", [(6, (START, MIDNIGHT)), (0, (MIDNIGHT, END))])
def test_overnight_restriction_keeps_each_weekdays_clock_attribution(h, weekday, expected):
    data = snapshot(h)
    person = h.seed["people"][0]
    data["policies"] = [policy(data, {"kind": "restriction"}, {"person_id": person, "weekdays": [weekday]})]
    findings = [f for f in evaluate(data)["findings"] if f["code"] == "RESTRICTION" and f["rule_id"] == POLICY]
    assert [(aware(f["start"]), aware(f["end"])) for f in findings] == [tuple(map(aware, expected))]


def test_unrelated_midnight_membership_does_not_multiply_advisory_restriction_cost(h):
    from packages.domain.cp_sat import generate

    data = snapshot(h)
    person = h.seed["people"][0]
    data["people"] = {person: data["people"][person]}
    data["memberships"] = [row for row in data["memberships"] if row["person_id"] == person]
    data["shifts"][0]["participants"] = []
    data["people"][person]["tags"] = ["synthetic"]
    scope = {"person_id": person, "conditions": {"match": "all", "conditions": [
        {"field": "tags", "op": "any", "values": ["synthetic"]}]}}
    data["policies"] = [policy(data, {"kind": "restriction"}, scope)]
    data["policies"][0]["definition"]["severity"] = "advisory"
    before = generate(data, 3, workers=1, deterministic=True)
    dated_scope(data, person, "programme_ids", "join")
    after = generate(data, 3, workers=1, deterministic=True)
    # Safety kinds are always mandatory. The membership change cannot restore the excluded assignment.
    assert before["status"] == after["status"] == "requirements_conflict"
    assert not before["additions"] and not after["additions"]

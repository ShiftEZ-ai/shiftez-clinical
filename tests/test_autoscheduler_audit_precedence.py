"""Regression coverage for audit F4: catalogue Who specificity through nested groups.

Generator assertions use assignment counts independently of the product validator.
"""
import copy
from datetime import datetime
import uuid

import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain import cp_sat, generation, rule_lines
from packages.domain.conditions import active_rules, conditions_match, scope_tree, with_tags
from packages.domain.validation import evaluate, named_people
from .test_rule_catalogue_guard import catalogue, line


ENGINES = ("cp_sat", "greedy")



def tree(field, *values, nested=False):
    condition = {"field": field, "op": "any", "values": list(values)}
    group = {"match": "all", "conditions": [condition]}
    return {"match": "all", "conditions": [group]} if nested else group


def rule(data, entry, *lines):
    policy = catalogue(entry, *lines)
    policy["department_id"] = data["workspace"]["department_id"]
    policy["definition"] = PolicyDefinition.model_validate(policy["definition"]).model_dump(mode="json")
    data["policies"] = [policy]
    return policy


def solve(data, engine):
    if engine == "cp_sat":
        return cp_sat.generate(data, 3, 1, deterministic=True)
    return generation.generate(data, 3)


def outcome(result):
    return {k: result.get(k) for k in ("status", "solver_status", "optimality_proven")}


def proposal(data, result):
    out = copy.deepcopy(data)
    shifts = {s["id"]: s for s in out["shifts"]}
    for removal in result.get("removals", []):
        shifts[removal["shift_id"]]["participants"] = [
            p for p in shifts[removal["shift_id"]]["participants"]
            if p["id"] != removal["assignment_id"]]
    for addition in result.get("additions", []):
        shifts[addition["shift_id"]]["participants"].append(addition["participant"])
    return out


def two_duties(h, *, assigned=False):
    person = h.seed["people"][0]
    w = h.workspace()
    for day in (4, 5):
        w = h.shift(w, start=f"2026-10-{day:02}T08:00:00+03:00",
                    end=f"2026-10-{day:02}T12:00:00+03:00",
                    people=[person] if assigned else [])["workspace"]
    data = h.snapshot(w)
    data["people"] = {person: data["people"][person]}
    data["people"][person]["tags"] = ["audit_group"]
    data["generation_options"] = {"mode": "fill_gaps", "shortlist_width": 0}
    return data, person


@pytest.mark.parametrize("engine", ENGINES)
def test_nested_person_line_has_person_precedence(h, engine):
    """Nesting a Person condition must preserve its priority over a group cap."""
    data, person = two_duties(h)
    rule(data, "duties_per_week",
         line(who=tree("tags", "audit_group"), numbers={"max": {"value": 1}}),
         line(who=tree("person_ids", person, nested=True), numbers={"max": {"value": 2}}))
    result = solve(data, engine)
    candidate = proposal(data, result)
    # The person line permits both duties; each session independently needs one person.
    assigned = [p for s in candidate["shifts"] for p in s["participants"]]
    assert all(len(s["participants"]) == 1 for s in candidate["shifts"]), outcome(result)
    assert len(assigned) == 2 and {p["person_id"] for p in assigned} == {person}


def test_validator_nested_person_line_has_person_precedence(h):
    data, person = two_duties(h, assigned=True)
    policy = rule(data, "duties_per_week",
                  line(who=tree("tags", "audit_group"), numbers={"max": {"value": 1}}),
                  line(who=tree("person_ids", person, nested=True), numbers={"max": {"value": 2}}))
    assert sum(p["person_id"] == person for s in data["shifts"] for p in s["participants"]) == 2
    findings = [f for f in evaluate(data)["findings"] if f.get("rule_id") == policy["id"]]
    assert not findings, findings


@pytest.mark.parametrize("field,expected_tier,expected_winners", [
    ("person_ids", 3, {0: [1, 2], 1: [2]}),
    ("category_ids", 2, {0: [1, 2]}),
    ("programme_ids", 2, {0: [1, 2]}),
    ("tags", 2, {0: [1, 2]}),
    ("capability_ids", 2, {0: [1, 2]}),
    (None, 1, {0: [1], 2: [1]}),
], ids=["person", "rank", "programme", "tag", "skill", "everyone"])
def test_flat_and_nested_lines_have_equivalent_precedence(field, expected_tier, expected_winners):
    value = str(uuid.uuid4())
    for nested in (False, True):
        everyone = line(numbers={"max": {"value": 1}})
        group = line(who=tree("tags", "audit_group", nested=nested), numbers={"max": {"value": 2}})
        selected = line(who=tree(field, value, nested=nested) if field else None,
                        numbers={"max": {"value": 3}})
        policy = catalogue("duties_per_week", everyone, group, selected)
        assert rule_lines.tier(selected) == expected_tier
        assert rule_lines.outranking(policy) == expected_winners
        copies = active_rules([policy])
        assert [c["_catalogue_sources"][rule_lines.source_key(policy, selected)]["tier"]
                for c in copies] == [expected_tier] * 3


def test_sibling_all_groups_share_necessary_people_and_equal_tier_conflicts():
    first, second, third = (str(uuid.uuid4()) for _ in range(3))
    who = {"match": "all", "conditions": [
        tree("person_ids", first, second), tree("person_ids", second, third)]}
    nested = line(who=who, numbers={"max": {"value": 2}})
    flat = line(who=tree("person_ids", second), numbers={"max": {"value": 3}})
    assert named_people({"conditions": who}) == {second}
    assert rule_lines.named_people(nested) == [second]
    assert rule_lines.tier(nested) == 3
    assert rule_lines.both_apply(catalogue("duties_per_week", nested, flat)) == [(0, 1)]


def test_required_person_survives_optional_or_sibling():
    first, second = (str(uuid.uuid4()) for _ in range(2))
    optional = {"match": "any", "conditions": [
        {"field": "person_ids", "values": [second]},
        {"field": "tags", "values": ["audit_group"]}]}
    who = {"match": "all", "conditions": [tree("person_ids", first), optional]}
    assert named_people({"conditions": who}) == {first}
    assert rule_lines.tier(line(who=who)) == 3


@pytest.mark.parametrize("outer,inner", [("any", None), ("all", "any"), ("any", "all"), ("any", "any")],
                         ids=["flat_any", "all_any", "any_all", "any_any"])
def test_singleton_any_groups_keep_person_precedence(outer, inner):
    person = str(uuid.uuid4())
    who = tree("person_ids", person)
    who["match"] = inner or outer
    if inner:
        who = {"match": outer, "conditions": [who]}
    own = line(who=who, numbers={"max": {"value": 2}})
    policy = catalogue("duties_per_week",
                       line(who=tree("tags", "audit_group"), numbers={"max": {"value": 1}}), own)
    assert named_people({"conditions": who}) == {person}
    assert rule_lines.tier(own) == 3
    assert rule_lines.outranking(policy) == {0: [1]}
    assert rule_lines.unconditional_people(own)


@pytest.mark.parametrize("nested", [False, True], ids=["flat", "nested"])
def test_multibranch_any_does_not_grant_person_precedence(nested):
    who = {"match": "any", "conditions": [
        {"field": "person_ids", "values": [str(uuid.uuid4())]},
        {"field": "tags", "values": ["audit_group"]}]}
    if nested:
        who = {"match": "any", "conditions": [who]}
    assert named_people({"conditions": who}) is None
    assert rule_lines.tier(line(who=who)) == 2
    assert not rule_lines.unconditional_people(line(who=who))


@pytest.mark.parametrize("nested", [False, True], ids=["flat", "nested"])
def test_singleton_any_tagged_people_keep_group_precedence(nested):
    person, team = (str(uuid.uuid4()) for _ in range(2))
    who = tree("person_ids", team)
    who["match"] = "any"
    if nested:
        who = {"match": "any", "conditions": [who]}
    data = with_tags({"tags": {team: {"name": "audit_group", "members": {"people": [person]}}},
                      "people": {person: {}},
                      "policies": [catalogue("duties_per_week", line(who=who, numbers={"max": {"value": 2}}))]})
    tagged = data["policies"][0]["definition"]["rule"]["lines"][0]
    assert tagged.get("who_group") is True
    assert named_people({"conditions": tagged["who"]}) == {team, person}
    assert rule_lines.tier(tagged) == 2
    assert not rule_lines.unconditional_people(tagged)


@pytest.mark.parametrize("nested", [False, True], ids=["flat", "nested"])
@pytest.mark.parametrize("compound", [False, True], ids=["person_only", "person_and_tag"])
def test_person_quota_flat_and_nested_block_precedence_is_equivalent(nested, compound):
    person = str(uuid.uuid4())
    who = tree("person_ids", person)
    if compound:
        who["conditions"].append({"field": "tags", "values": ["audit_group"]})
    if nested:
        who = {"match": "all", "conditions": [who]}
    block = {"block_quota": {"starts_on": "2026-10-01", "ends_on": "2026-11-01"}}
    everyone = line(settings=block, numbers={"max": {"value": 1}})
    own = line(who=who, settings=block, numbers={"max": {"value": 2}}, starts_on="2026-10-10")
    policy = catalogue("duties_per_month", everyone, own)
    copies = active_rules([policy])
    data = {"organisation": {"timezone": "UTC"}, "people": {person: {"tags": ["audit_group"]}},
            "policies": copies}
    broad = next(c for c in copies if c["template_line"]["id"] == everyone["id"])
    sources = broad["_catalogue_sources"]
    assert sources[rule_lines.source_key(policy, own)]["whole_person"] is (not compound)
    for day in (5, 15):
        a = datetime.fromisoformat(f"2026-10-{day:02}T08:00:00+00:00")
        b = datetime.fromisoformat(f"2026-10-{day:02}T12:00:00+00:00")
        assert conditions_match(data, scope_tree(broad["definition"]["scope"]), person, None, a, b) is (
            compound and day == 5)
    if compound:
        data["people"][person]["tags"] = []
        assert conditions_match(data, scope_tree(broad["definition"]["scope"]), person, None, a, b)


@pytest.mark.parametrize("nested", [False, True], ids=["flat", "nested"])
@pytest.mark.parametrize("mixed", [False, True], ids=["tag", "person_and_tag"])
def test_people_selected_through_tags_keep_group_precedence(nested, mixed):
    person, team = (str(uuid.uuid4()) for _ in range(2))
    who = tree("person_ids", team)
    if mixed:
        who["conditions"].append({"field": "person_ids", "values": [person]})
    if nested:
        who = {"match": "all", "conditions": [who]}
    policy = catalogue("duties_per_week", line(numbers={"max": {"value": 1}}),
                       line(who=who, numbers={"max": {"value": 2}}),
                       line(who=tree("person_ids", person), numbers={"max": {"value": 3}}))
    data = with_tags({"tags": {team: {"name": "audit_group", "members": {"people": [person]}}},
                      "people": {person: {}}, "policies": [policy]})
    tagged = data["policies"][0]
    assert tagged["definition"]["rule"]["lines"][1].get("who_group") is True
    assert [rule_lines.tier(row) for row in tagged["definition"]["rule"]["lines"]] == [1, 2, 3]
    assert rule_lines.outranking(tagged) == {0: [1, 2], 1: [2]}


@pytest.mark.parametrize("selector", ["or", "negated"])
def test_optional_tag_selector_cannot_demote_required_person(selector):
    person, team = (str(uuid.uuid4()) for _ in range(2))
    optional = {"field": "person_ids", "values": [team], "op": "none"}
    if selector == "or":
        optional = {"match": "any", "conditions": [
            {"field": "person_ids", "values": [team]},
            {"field": "tags", "values": ["other_group"]}]}
    who = tree("person_ids", person)
    who["conditions"].append(optional)
    policy = catalogue("duties_per_week", line(who=who, numbers={"max": {"value": 2}}))
    data = with_tags({"tags": {team: {"name": "audit_group", "members": {"people": []}}},
                      "people": {person: {"tags": ["other_group"]}}, "policies": [policy]})
    required = data["policies"][0]["definition"]["rule"]["lines"][0]
    assert named_people({"conditions": required["who"]}) == {person}
    assert rule_lines.tier(required) == 3


@pytest.mark.parametrize("nested", [False, True], ids=["flat", "nested"])
@pytest.mark.parametrize("selector", ["or", "negated"])
@pytest.mark.parametrize("engine", ENGINES)
def test_optional_or_negated_person_cannot_override_group_cap(h, engine, selector, nested):
    data, person = two_duties(h, assigned=True)
    if selector == "or":
        who = {"match": "any", "conditions": [
            {"field": "person_ids", "values": [person]},
            {"field": "tags", "values": ["audit_group"]}]}
    else:
        who = tree("person_ids", str(uuid.uuid4()))
        who["conditions"][0]["op"] = "none"
    if nested:
        who = {"match": "all", "conditions": [who]}
    policy = rule(data, "duties_per_week",
                  line(who=tree("tags", "audit_group"), numbers={"max": {"value": 1}}),
                  line(who=who, numbers={"max": {"value": 2}}))
    assert rule_lines.tier(policy["definition"]["rule"]["lines"][1]) == 2
    assert named_people({"conditions": who}) is None
    findings = [f for f in evaluate(data)["findings"] if f.get("rule_id") == policy["id"]]
    assert any(f["code"] == "MAX_DUTIES" and f["severity"] == "error" for f in findings), findings
    for shift in data["shifts"]:
        shift["participants"] = []
    result = solve(data, engine)
    candidate = proposal(data, result)
    assert sum(len(s["participants"]) for s in candidate["shifts"]) < 2, outcome(result)


@pytest.mark.parametrize("engine", ENGINES)
def test_nested_person_safety_line_cannot_relax_broader_rest(h, engine):
    person = h.seed["people"][0]
    w = h.shift(h.workspace(), people=[person])["workspace"]
    w = h.shift(w, start="2026-10-04T14:00:00+03:00", end="2026-10-04T18:00:00+03:00",
                people=[person])["workspace"]
    data = h.snapshot(w)
    data["people"] = {person: data["people"][person]}
    earlier, later = sorted(data["shifts"], key=lambda s: s["start"])
    earlier["participants"][0]["fixed"] = True
    policy = rule(data, "rest_between_duties", line(numbers={"min": {"value": 4}}),
                  line(who=tree("person_ids", person, nested=True), numbers={"min": {"value": 1}}))
    assert rule_lines.outranking(policy) == {}
    findings = [f for f in evaluate(data)["findings"] if f.get("rule_id") == policy["id"]]
    assert any(f["code"] == "MIN_REST" and f["severity"] == "error"
               and f["detail"]["required_hours"] == 4 for f in findings), findings
    later["participants"] = []
    data["generation_options"] = {"mode": "fill_gaps", "shortlist_width": 0}
    result = solve(data, engine)
    candidate = proposal(data, result)
    shifts = {s["id"]: s for s in candidate["shifts"]}
    assert [p["person_id"] for p in shifts[earlier["id"]]["participants"]] == [person]
    assert not shifts[later["id"]]["participants"], outcome(result)

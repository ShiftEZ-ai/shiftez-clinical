"""F2 regression: nested programme selectors retain cross-midnight mandatory rest."""
import copy
from datetime import datetime
import uuid

import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain import cp_sat, generation
from packages.domain.conditions import active_rules, rule_holds, OUTRANKED, OVERRIDDEN
from packages.domain.rule_lines import source_key
from packages.domain.time import aware
from packages.domain.validation import evaluate, staffing_counts
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


def dated_programme_rest(h, *, nested, assigned=False):
    person = h.seed["people"][0]
    w = h.workspace()
    w = h.shift(w, start="2026-10-04T18:00:00+03:00", end="2026-10-04T22:00:00+03:00",
                people=[person])["workspace"]
    w = h.shift(w, start="2026-10-04T23:00:00+03:00", end="2026-10-05T03:00:00+03:00",
                people=[person] if assigned else [])["workspace"]
    data = h.snapshot(w)
    data["people"] = {person: data["people"][person]}
    programme = str(uuid.uuid4())
    data["programmes"] = {programme: {"id": programme, "name": "Dated audit programme",
        "active": True, "starts_on": "2020-01-01", "ends_on": None}}
    data["programme_memberships"] = [{"person_id": person, "programme_id": programme,
        "starts_on": "2026-10-05", "ends_on": "2026-10-11"}]
    policy = rule(data, "rest_between_duties",
                  line(who=tree("programme_ids", programme, nested=nested), numbers={"min": {"value": 4}}))
    for shift in data["shifts"]:
        for participant in shift["participants"]:
            participant["fixed"] = True
    data["generation_options"] = {"mode": "fill_gaps", "shortlist_width": 0}
    return data, person, policy


def independent_programme_rest_breaches(data, person):
    """Any worked portion after programme entry needs the programme's four-hour rest."""
    entry = datetime.fromisoformat("2026-10-05T00:00:00+03:00")
    rows = sorted((datetime.fromisoformat(p["start"]), datetime.fromisoformat(p["end"]))
                  for s in data["shifts"] for p in s["participants"] if p["person_id"] == person)
    return [(a, b) for (_, previous_end), (a, b) in zip(rows, rows[1:])
            if entry < b and (a - previous_end).total_seconds() < 4 * 3600]


@pytest.mark.parametrize("engine", ENGINES)
def test_nested_programme_rest_applies_across_membership_midnight(h, engine):
    data, person, _ = dated_programme_rest(h, nested=True)
    result = solve(data, engine)
    candidate = proposal(data, result)
    assert not independent_programme_rest_breaches(candidate, person), outcome(result)


def test_validator_nested_programme_rest_detects_cross_midnight_mutation(h):
    data, person, policy = dated_programme_rest(h, nested=True, assigned=True)
    assert independent_programme_rest_breaches(data, person)
    findings = [f for f in evaluate(data)["findings"] if f.get("rule_id") == policy["id"]]
    assert any(f["code"] == "MIN_REST" and f["severity"] == "error" for f in findings), findings


def midnight_rank_cover(h, *, assigned=False):
    person = h.seed['people'][0]
    start, end = '2026-10-04T23:00:00+03:00', '2026-10-05T03:00:00+03:00'
    workspace = h.shift(h.workspace(), start=start, end=end, people=[person] if assigned else [])['workspace']
    data = h.snapshot(workspace)
    data['people'] = {person: data['people'][person]}
    rank = str(uuid.uuid4())
    data['categories'][rank] = {'id': rank, 'active': True}
    data['memberships'].append({'person_id': person, 'category_id': rank,
        'department_id': data['workspace']['department_id'], 'starts_on': '2026-10-05', 'ends_on': None})
    policy = rule(data, 'people_per_session',
                  line(who=tree('category_ids', rank, nested=True), numbers={'min': {'value': 1}}))
    data['generation_options'] = {'mode': 'fill_gaps', 'shortlist_width': 0}
    return data, person, policy


@pytest.mark.parametrize('engine', ENGINES)
def test_midnight_rank_does_not_satisfy_cover_before_membership(h, engine):
    data, _, _ = midnight_rank_cover(h)
    result = solve(data, engine)
    assert result['status'] != 'candidate_feasible', outcome(result)


def test_validator_midnight_rank_does_not_satisfy_cover_before_membership(h):
    data, _, policy = midnight_rank_cover(h, assigned=True)
    findings = [f for f in evaluate(data)['findings'] if f.get('rule_id') == policy['id']]
    assert any(f['code'] == 'SCOPED_COVERAGE_GAP' and f['severity'] == 'error'
               and aware(f['start']) < aware('2026-10-05T00:00:00+03:00') for f in findings), findings
    [resolved] = active_rules([policy])
    shift = data['shifts'][0]
    counts = staffing_counts(data, resolved, shift, aware(shift['start']), aware(shift['end']))
    assert counts[0]['available'] == 0, counts


@pytest.mark.parametrize('selector', [OUTRANKED, OVERRIDDEN])
def test_midnight_specific_line_does_not_relax_general_before_it_holds(h, selector):
    data, person, _ = midnight_rank_cover(h, assigned=True)
    rank = data['memberships'][-1]['category_id']
    policy = rule(data, 'duties_per_week', line(numbers={'max': {'value': 1}}),
                  line(who={'match': 'all', 'conditions': [
                      {'field': 'person_ids', 'values': [person]},
                      tree('category_ids', rank)]}, numbers={'max': {'value': 2}}))
    copies = active_rules([policy])
    data['policies'] = copies
    shift = data['shifts'][0]
    a, b = aware(shift['start']), aware(shift['end'])
    specific = next(copy for copy in copies if copy['template_line']['line'] == 1)
    if selector == OUTRANKED:
        value = source_key(policy, policy['definition']['rule']['lines'][1])
    else:
        specific = {**specific, 'id': str(uuid.uuid4())}
        data['policies'] = [specific]
        value = specific['id']
    assert not rule_holds(data, person, shift, a, b)(selector, [value])
    assert rule_holds(data, person, shift, aware('2026-10-05T00:00:00+03:00'), b)(selector, [value])


@pytest.mark.parametrize('field', ['programme_ids', 'category_ids', 'rotation_type_ids'])
@pytest.mark.parametrize('nested', [False, True])
def test_dated_rule_line_editor_api_roundtrip_preserves_authoring(h, field, nested):
    from .test_rule_conditions import dimensions
    from .test_rule_catalogue_api import approve, entry, propose
    value = dimensions(h)[field]
    authored = line(who=tree(field, value, nested=nested), numbers={'min': {'value': 4}})
    first = propose(h, 'rest_between_duties', [authored])['record']
    stored = copy.deepcopy(first['definition']['rule']['lines'][0])
    leaves = stored['who']['conditions'][0]['conditions'] if nested else stored['who']['conditions']
    assert leaves[0]['field'] == field and leaves[0]['values'] == [value]
    current = approve(h, first)
    assert entry(h, 'rest_between_duties')['rule']['definition']['rule']['lines'] == [stored]
    edited = {**stored, 'numbers': {'min': {'value': 6, 'level': 'mandatory'}}}
    second = propose(h, 'rest_between_duties', [edited], policy_id=current['id'], revision=current['revision'])['record']
    approve(h, second)
    reloaded = entry(h, 'rest_between_duties')['rule']['definition']['rule']['lines'][0]
    assert reloaded['who'] == stored['who'] and reloaded['id'] == stored['id']
    assert reloaded['numbers']['min']['value'] == 6


@pytest.mark.parametrize('field,flat', [('programme_ids', 'programme_id'), ('category_ids', 'category_ids'),
                                     ('rotation_type_ids', 'rotation_type_id')])
def test_existing_flat_rule_scope_roundtrip_matches_nested_obligation(h, field, flat):
    from .test_rule_conditions import dimensions
    from packages.domain.validation import policy_applies
    value = dimensions(h)[field]
    scope = {flat: [value] if flat == 'category_ids' else value}
    legacy = h.policy({'kind': 'min_rest', 'hours': 4}, scope)
    nested = h.policy({'kind': 'min_rest', 'hours': 4}, {'conditions': tree(field, value, nested=True)})
    rows = h.get('coordinator', '/api/policies?status=active')['policies']
    saved = next(p for p in rows if p['id'] == legacy['id'])
    assert saved['definition']['scope'][flat] == scope[flat]
    start, end = '2026-10-05T23:00:00+03:00', '2026-10-06T03:00:00+03:00'
    workspace = h.shift(h.workspace(), start=start, end=end, people=[])['workspace']
    data = h.snapshot(workspace)
    shift = data['shifts'][0]
    for a, b, expected in [(start, '2026-10-06T00:00:00+03:00', False),
                           ('2026-10-06T00:00:00+03:00', end, True), (start, end, True)]:
        assert policy_applies(data, legacy, h.seed['people'][0], shift, aware(a), aware(b)) == expected
        assert policy_applies(data, nested, h.seed['people'][0], shift, aware(a), aware(b)) == expected

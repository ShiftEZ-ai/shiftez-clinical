"""Regression coverage for nested external work and minimum rest."""


import copy
from datetime import datetime, timedelta

import pytest

from packages.domain.cp_sat import generate as cp_generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.rule_map import reach
from packages.domain.validation import evaluate, finding_observations, workload_gaps


ENGINES = {"cp_sat": cp_generate, "greedy": greedy_generate}


def _stamp(value):
    return datetime.fromisoformat(value)


def _assignment(shift, person, **extra):
    return {"id": shift["id"] + ":" + person, "shift_id": shift["id"],
            "person_id": person, "start": shift["start"], "end": shift["end"],
            "participation": "staff", "overtime": False, "revision": 1, **extra}


def _apply(data, result):
    candidate = copy.deepcopy(data)
    removed = {r["assignment_id"] for r in result["removals"]}
    for shift in candidate["shifts"]:
        shift["participants"] = [p for p in shift["participants"] if p["id"] not in removed]
        shift["participants"].extend(copy.deepcopy(a["participant"]) for a in result["additions"]
                                     if a["shift_id"] == shift["id"])
    return candidate


def _nested_external_work(h, include_long=True):
    person = h.seed["people"][0]
    h.policy({"kind": "min_rest", "hours": 8})
    intervals = [("2026-10-04T21:00:00+03:00", "2026-10-04T22:00:00+03:00")]
    if include_long:
        intervals.append(("2026-10-04T20:00:00+03:00", "2026-10-05T04:00:00+03:00"))
    for start, end in intervals:
        h.command("coordinator", "availability.record", {"person_id": person,
                  "kind": "external_commitment", "start": start, "end": end,
                  "private_reason": "Synthetic overlapping external work evidence"})
    workspace = h.shift(h.workspace(), people=[], start="2026-10-05T08:00:00+03:00",
                        end="2026-10-05T12:00:00+03:00")["workspace"]
    data = h.snapshot(workspace)
    data["people"] = {person: data["people"][person]}
    return data, person


def _external_rest_breaches(data):
    failures = []
    for shift in data["shifts"]:
        for participant in shift["participants"]:
            start = _stamp(participant["start"])
            previous_ends = [_stamp(r["end"]) for r in data["availability"]
                             if r["person_id"] == participant["person_id"]
                             and r["kind"] == "external_commitment" and _stamp(r["end"]) <= start]
            if previous_ends and start - max(previous_ends) < timedelta(hours=8):
                failures.append((participant["person_id"], start - max(previous_ends)))
    return failures


@pytest.mark.parametrize("engine", ENGINES)
def test_nested_external_commitment_cannot_hide_required_rest(h, engine):
    data, person = _nested_external_work(h)
    result = ENGINES[engine](data, time_budget_seconds=3)
    breaches = _external_rest_breaches(_apply(data, result))
    assert not breaches, {"status": result["status"], "reported_valid": result.get("validation", {}).get("valid"),
                          "optimality_proven": result.get("optimality_proven"), "independent_breaches": breaches}


def test_validator_detects_latest_work_end_with_nested_external_commitments(h):
    data, person = _nested_external_work(h, include_long=False)
    shift = data["shifts"][0]
    shift["participants"] = [_assignment(shift, person)]
    assert not _external_rest_breaches(data)
    assert evaluate(data)["valid"]
    data["availability"].append({"id": "longer-external", "person_id": person,
        "kind": "external_commitment", "start": "2026-10-04T20:00:00+03:00", "end": "2026-10-05T04:00:00+03:00"})
    assert _external_rest_breaches(data) == [(person, timedelta(hours=4))]
    assert "MIN_REST" in {f["code"] for f in finding_observations(evaluate(data)) if f["severity"] == "error"}


@pytest.mark.parametrize('intervals,source,gap', [
    ([(20, 28), (21, 22)], 0, 4),  # Nested work cannot bring the occupied end forward.
    ([(20, 24), (21, 28), (22, 23)], 1, 4),  # An overlapping chain extends the end.
    ([(20, 28), (21, 28)], 1, 4),  # Equal ends retain the later source row.
    ([(20, 22), (26, 27)], 1, 5),  # Ordinary intervening work replaces the source.
])
def test_spacing_keeps_the_latest_occupied_end_and_source(intervals, source, gap):
    start = _stamp('2026-10-04T00:00:00+03:00')
    rows = [({'id': 'external:' + str(i), 'location_id': None}, {},
             start + timedelta(hours=a), start + timedelta(hours=b)) for i, (a, b) in enumerate(intervals)]
    rows.append(({'id': 'duty', 'location_id': 'clinic'}, {}, start + timedelta(hours=32), start + timedelta(hours=36)))
    [observation] = workload_gaps({}, {'kind': 'min_rest', 'hours': 8}, rows, {'duty'})
    assert observation['previous']['id'] == 'external:' + str(source)
    assert observation['current']['id'] == 'duty'
    assert observation['actual_hours'] == gap


@pytest.mark.parametrize('engine', ENGINES)
@pytest.mark.parametrize('minutes,blocked', [(240, False), (241, True)])
def test_nested_external_travel_respects_latest_end_and_exact_gap(h, engine, minutes, blocked):
    data, person = _nested_external_work(h)
    policy = next(p for p in data['policies'] if p['kind'] == 'min_rest')
    policy['kind'] = 'travel_buffer'
    policy['definition']['rule'] = {'kind': 'travel_buffer', 'minutes': minutes,
        'from_location_id': h.seed['locations'][1], 'to_location_id': h.seed['locations'][0]}
    result = ENGINES[engine](data, time_budget_seconds=3)
    candidate = _apply(data, result)
    assert bool(candidate['shifts'][0]['participants']) is not blocked, result
    assert result['status'] == ('requirements_conflict' if engine == 'cp_sat' and blocked else
                               'candidate_with_gaps' if blocked else 'candidate_feasible')
    candidate['shifts'][0]['participants'] = [_assignment(candidate['shifts'][0], person)]
    findings = [f for f in finding_observations(evaluate(candidate)) if f['code'] == 'EXTERNAL_LOCATION_UNKNOWN']
    assert bool(findings) is blocked
    if blocked:
        assert findings[0]['detail']['actual_hours'] == 4


@pytest.mark.parametrize('engine', ENGINES)
def test_nested_external_work_allows_exact_minimum_rest(h, engine):
    data, _ = _nested_external_work(h)
    next(p for p in data['policies'] if p['kind'] == 'min_rest')['definition']['rule']['hours'] = 4
    result = ENGINES[engine](data, time_budget_seconds=3)
    assert result['status'] == 'candidate_feasible', result
    assert _apply(data, result)['shifts'][0]['participants']
    assert evaluate(_apply(data, result))['valid']


def test_nested_rest_finding_and_rule_reach_keep_the_long_work_source(h):
    data, person = _nested_external_work(h)
    shift = data['shifts'][0]
    shift['participants'] = [_assignment(shift, person)]
    source = max((r for r in data['availability'] if r['kind'] == 'external_commitment'), key=lambda r: _stamp(r['end']))
    [finding] = [f for f in finding_observations(evaluate(data)) if f['code'] == 'MIN_REST']
    assert finding['detail']['previous_shift_id'] == 'external:' + source['id']
    assert finding['detail']['next_shift_id'] == shift['id']
    assert finding['detail']['actual_hours'] == 4
    details = reach(data, data['policies'], _stamp('2026-10-05T00:00:00+03:00'),
                    _stamp('2026-10-06T00:00:00+03:00'), details=True)['details']
    [line] = details[next(p['id'] for p in data['policies'] if p['kind'] == 'min_rest')]['lines']
    [observation] = line['people'][0]['counts']
    assert observation['previous_shift_id'] == 'external:' + source['id']
    assert observation['next_shift_id'] == shift['id']
    assert observation['actual'] == 4


@pytest.mark.parametrize('engine', ENGINES)
@pytest.mark.parametrize('long_location,blocked', [(0, False), (1, True)])
def test_directed_travel_uses_the_location_of_latest_ending_fixed_work(h, engine, long_location, blocked):
    data, person = _nested_external_work(h)
    data['availability'] = []
    duty = data['shifts'][0]
    for name, start, end, location in [
        ('long', '2026-10-04T20:00:00+03:00', '2026-10-05T04:00:00+03:00', long_location),
        ('nested', '2026-10-04T21:00:00+03:00', '2026-10-05T03:30:00+03:00', 1 - long_location),
    ]:
        fixed = {**copy.deepcopy(duty), 'id': name, 'workspace_id': 'history',
                 'start': start, 'end': end, 'location_id': h.seed['locations'][location]}
        fixed['participants'] = [_assignment(fixed, person)]
        data['shifts'].append(fixed)
    policy = next(p for p in data['policies'] if p['kind'] == 'min_rest')
    policy['kind'] = 'travel_buffer'
    policy['definition']['rule'] = {'kind': 'travel_buffer', 'minutes': 300,
        'from_location_id': h.seed['locations'][1], 'to_location_id': h.seed['locations'][0]}
    result = ENGINES[engine](data, time_budget_seconds=3)
    candidate = _apply(data, result)
    assert bool(candidate['shifts'][0]['participants']) is not blocked, result
    assert result['status'] != 'translation_error', result
    duty['participants'] = [_assignment(duty, person)]
    findings = [f for f in finding_observations(evaluate(data)) if f['code'] == 'TRAVEL_BUFFER']
    assert bool(findings) is blocked
    if blocked:
        assert findings[0]['detail']['previous_shift_id'] == 'long'
        assert findings[0]['detail']['actual_hours'] == 4

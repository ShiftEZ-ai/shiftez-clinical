"""maintenance regression coverage for mandatory cover precedence in both engines."""

import copy
from collections import Counter

import pytest

from packages.domain.cp_sat import generate as cp_sat_generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.fill_priority import STAGE_ORDER, stage_progress
from tests.scheduler_departments import assert_no_mandatory_single_addition


@pytest.mark.parametrize("engine", [greedy_generate, cp_sat_generate])
def test_required_cover_precedes_high_priority_desired_seat(h, engine):
    desired = h.configure(
        "assignment_types",
        {
            "name": "G11 desired seat",
            "starts_on": "2020-01-01",
            "definition": {"requirements": [{"minimum": 1, "target": 2, "maximum": 2}]},
        },
    )["record"]["id"]
    required = h.configure(
        "assignment_types",
        {
            "name": "G11 required seat",
            "starts_on": "2020-01-01",
            "definition": {"requirements": []},
        },
    )["record"]["id"]
    h.policy(
        {"kind": "allocation_preference", "priority": 0, "session_priority": 10},
        {"service_ids": [h.seed["services"][0]], "person_id": h.seed["people"][0]},
        "advisory",
    )
    h.policy({"kind": "minimum_cover", "minimum": 1}, {"assignment_type_id": required})
    workspace = h.shift(h.workspace(), people=[], typ=desired)["workspace"]
    workspace = h.shift(workspace, people=[], typ=required, service=1, location=1)["workspace"]
    data = h.snapshot(workspace)
    data["people"] = {pid: data["people"][pid] for pid in h.seed["people"][:2]}
    data["generation_options"] = {"allocation_order": "tiered"}

    def counts(candidate):
        by_type = {shift["assignment_type_id"]: shift["id"] for shift in data["shifts"]}
        assigned = Counter(row["shift_id"] for row in candidate["additions"])
        return assigned[by_type[required]], assigned[by_type[desired]]

    run = lambda candidate: (
        engine(candidate, time_budget_seconds=5, deterministic=True)
        if engine is cp_sat_generate
        else engine(candidate, time_budget_seconds=5)
    )
    on = run(data)
    required_on, desired_on = counts(on)
    assert on["status"] in {"candidate_feasible", "candidate_with_gaps"}, on
    binding_count = required_on
    assert binding_count == 1
    assert desired_on == 1

    off_data = copy.deepcopy(data)
    off_data["policies"] = [
        policy
        for policy in off_data["policies"]
        if policy["definition"]["rule"].get("kind") != "minimum_cover"
    ]
    off = run(off_data)
    required_off, desired_off = counts(off)
    assert off["status"] in {"candidate_feasible", "candidate_with_gaps"}, off
    binding_count = int(desired_on != desired_off)
    assert required_off == 0 and desired_off == 2, {
        "binding_count": binding_count,
        "required_on": required_on,
        "desired_on": desired_on,
        "required_off": required_off,
        "desired_off": desired_off,
    }


def test_engines_share_fill_stage_order():
    assert STAGE_ORDER == ("mandatory_cover", "mandatory_quota", "desired")


def test_stage_progress_keeps_strict_and_later_stage_rules_distinct():
    assert stage_progress((0, 1, 0), (0, 0, 1), strict_first=True)
    assert not stage_progress((0, 1, 0), (1, 0, 1), strict_first=True)
    assert stage_progress((1, 0, 0), (0, 0, 1), strict_first=True)
    assert stage_progress((1, 0, 1), (1, 0, 0), strict_first=False)
    assert not stage_progress((1, 0, 1), (1, 1, 0), strict_first=False)


@pytest.mark.parametrize("engine", [greedy_generate, cp_sat_generate])
def test_cross_shift_required_cover_precedes_earlier_desired_shift(h, engine):
    desired = h.configure(
        "assignment_types",
        {
            "name": "G11 earlier desired only",
            "starts_on": "2020-01-01",
            "definition": {"requirements": [{"minimum": 0, "target": 2, "maximum": 2}]},
        },
    )["record"]["id"]
    required = h.configure(
        "assignment_types",
        {
            "name": "G11 later required cover",
            "starts_on": "2020-01-01",
            "definition": {"requirements": []},
        },
    )["record"]["id"]
    h.policy({"kind": "allocation_preference", "priority": 0, "session_priority": 10},
             {"service_ids": [h.seed["services"][0]]}, "advisory")
    h.policy({"kind": "minimum_cover", "minimum": 1}, {"assignment_type_id": required})
    workspace = h.shift(workspace := h.workspace(), start="2026-10-05T08:00:00+03:00",
                        end="2026-10-05T12:00:00+03:00", people=[], typ=desired)["workspace"]
    workspace = h.shift(workspace, start="2026-10-05T10:00:00+03:00", end="2026-10-05T14:00:00+03:00",
                        people=[], typ=required, service=1, location=1)["workspace"]
    data = h.snapshot(workspace)
    data["people"] = {pid: data["people"][pid] for pid in h.seed["people"][:2]}
    data["generation_options"] = {"allocation_order": "tiered"}

    def run(candidate):
        return (engine(candidate, time_budget_seconds=5, deterministic=True)
                if engine is cp_sat_generate else engine(candidate, time_budget_seconds=5))

    on = run(data)
    assert_no_mandatory_single_addition(data, on)
    by_type = {shift["assignment_type_id"]: shift["id"] for shift in data["shifts"]}
    assigned = Counter(row["shift_id"] for row in on["additions"])
    assert assigned[by_type[required]] == 1, on
    assert assigned[by_type[desired]] == 1, on
    off_data = copy.deepcopy(data)
    off_data["policies"] = [p for p in off_data["policies"]
                             if p["definition"]["rule"].get("kind") != "minimum_cover"]
    off = run(off_data)
    off_assigned = Counter(row["shift_id"] for row in off["additions"])
    binding_count = int(assigned[by_type[desired]] != off_assigned[by_type[desired]])
    assert off_assigned[by_type[required]] == 0 and off_assigned[by_type[desired]] == 2, {
        "binding_count": binding_count, "on": on, "off": off,
    }


@pytest.mark.parametrize("fixture", ["quota", "desired"])
def test_skipping_empty_strict_sweep_preserves_greedy_additions(h, fixture, monkeypatch):
    from packages.domain import generation

    person = h.seed["people"][0]
    definition = {"requirements": []} if fixture == "quota" else {
        "requirements": [{"minimum": 0, "target": 1, "maximum": 1}],
    }
    duty = h.configure("assignment_types", {"name": f"G11 {fixture} sweep", "starts_on": "2020-01-01",
                                               "definition": definition})["record"]["id"]
    if fixture == "quota":
        h.policy({"kind": "block_quota", "starts_on": "2026-10-04", "ends_on": "2026-10-11",
                  "metric": "duties", "comparison": "target", "amount": 1}, {"person_id": person}, "advisory")
    data = h.snapshot(h.shift(h.workspace(), start="2026-10-05T08:00:00+03:00",
                              end="2026-10-05T12:00:00+03:00", people=[], typ=duty)["workspace"])
    data["people"] = {person: data["people"][person]}
    data["generation_options"] = {"allocation_order": "tiered"}

    original_modes = generation._sweep_modes
    observed = []
    def observed_modes(baseline):
        modes = original_modes(baseline)
        observed.append(modes)
        return modes
    monkeypatch.setattr(generation, "_sweep_modes", observed_modes)
    skipped = greedy_generate(data, time_budget_seconds=5)
    assert observed and observed[0] == (False,), observed

    monkeypatch.setattr(generation, "_sweep_modes", lambda baseline: (True, False))
    unskipped = greedy_generate(data, time_budget_seconds=5)
    assert skipped["additions"] == unskipped["additions"], {
        "fixture": fixture, "skipped": skipped, "unskipped": unskipped,
    }


def test_mandatory_quota_keeps_strict_sweep():
    from packages.domain.generation import _sweep_modes

    baseline = {"findings": [{"code": "QUOTA_BELOW_MINIMUM", "severity": "error",
                               "detail": {"difference": -1}}]}
    assert _sweep_modes(baseline) == (True, False)


@pytest.mark.parametrize("engine", [greedy_generate, cp_sat_generate])
def test_unfillable_mandatory_gap_does_not_stall_later_fill(h, engine):
    desired = h.configure(
        "assignment_types",
        {
            "name": "G11 desired after gap",
            "starts_on": "2020-01-01",
            "definition": {"requirements": [{"minimum": 0, "target": 1, "maximum": 1}]},
        },
    )["record"]["id"]
    quota_duty = h.configure(
        "assignment_types",
        {
            "name": "G11 quota after gap",
            "starts_on": "2020-01-01",
            "definition": {"requirements": []},
        },
    )["record"]["id"]
    required = h.configure(
        "assignment_types",
        {
            "name": "G11 nobody eligible",
            "starts_on": "2020-01-01",
            "definition": {"requirements": []},
        },
    )["record"]["id"]
    person = h.seed["people"][0]
    h.policy(
        {"kind": "allocation_preference", "priority": 0, "session_priority": 10},
        {"service_ids": [h.seed["services"][0]], "person_id": person},
        "advisory",
    )
    h.policy({"kind": "minimum_cover", "minimum": 1}, {"assignment_type_id": required})
    h.policy(
        {"kind": "block_quota", "starts_on": "2026-10-04", "ends_on": "2026-10-11",
         "metric": "duties", "comparison": "target", "amount": 1},
        {"person_id": h.seed["people"][1]},
        "advisory",
    )
    workspace = h.shift(h.workspace(), start="2026-10-05T08:00:00+03:00", end="2026-10-05T12:00:00+03:00",
                        people=[], typ=desired)["workspace"]
    workspace = h.shift(workspace, start="2026-10-05T13:00:00+03:00", end="2026-10-05T17:00:00+03:00",
                        people=[], typ=quota_duty, service=1)["workspace"]
    required_start, required_end = "2026-10-06T08:00:00+03:00", "2026-10-06T12:00:00+03:00"
    workspace = h.shift(workspace, start=required_start, end=required_end, people=[], typ=required, service=1, location=1)["workspace"]
    data = h.snapshot(workspace)
    data["people"] = {pid: data["people"][pid] for pid in h.seed["people"][:2]}
    data["generation_options"] = {"allocation_order": "tiered"}
    for pid in data["people"]:
        data["availability"].append({"id": f"g11-required-leave-{pid}", "person_id": pid,
                                       "kind": "recorded_leave", "start": required_start, "end": required_end})
    if engine is cp_sat_generate:
        result = engine(data, time_budget_seconds=5, deterministic=True, diagnostic_proposal=True)
    else:
        result = engine(data, time_budget_seconds=5)
    assert_no_mandatory_single_addition(data, result)
    by_type = {shift["assignment_type_id"]: shift["id"] for shift in data["shifts"]}
    assigned = Counter(row["shift_id"] for row in result["additions"])
    assert assigned[by_type[desired]] == 1, result
    assert assigned[by_type[quota_duty]] == 1, result
    assert assigned[by_type[required]] == 0, result
    assert any(f.get("shift_id") == by_type[required] and f["code"] == "SCOPED_COVERAGE_GAP"
               for f in result["validation"]["findings"]), result

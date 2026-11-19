"""Overnight rank-boundary supervision regression from scheduler audit F3."""
import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from packages.domain.cp_sat import generate as cp_generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.validation import evaluate


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


def _violations(data, kind, blocked, supervisor):
    """Deliberately fixture-specific arithmetic; no product condition/check helper."""
    shifts = [s for s in data["shifts"] if not s["cancelled"]]
    failures = []
    if kind in {"restriction", "leave_buffer", "unavailable"}:
        failures.extend(s["id"] for s in shifts for p in s["participants"] if p["person_id"] == blocked)
    if kind in {"minimum_cover", "maximum_people"}:
        for shift in shifts:
            size = len({p["person_id"] for p in shift["participants"]})
            if kind == "minimum_cover" and size < 2 or kind == "maximum_people" and size > 1:
                failures.append(shift["id"])
    if kind in {"rest", "next_day", "overlap"}:
        people = {p["person_id"] for s in shifts for p in s["participants"]}
        for person in people:
            rows = sorted((_stamp(p["start"]), _stamp(p["end"])) for s in shifts
                          for p in s["participants"] if p["person_id"] == person)
            for index, (start, end) in enumerate(rows):
                for following, finish in rows[index + 1:]:
                    if kind == "overlap" and start < finish and following < end:
                        failures.append(person)
                    if kind == "next_day" and (following.astimezone(ZoneInfo("Asia/Riyadh")).date()
                                               - start.astimezone(ZoneInfo("Asia/Riyadh")).date()).days == 1:
                        failures.append(person)
                if kind == "rest" and index + 1 < len(rows) and rows[index + 1][0] - end < timedelta(hours=24):
                    failures.append(person)
    if kind in {"supervision", "supervisor_capacity"}:
        for shift in shifts:
            assigned = {p["person_id"]: p for p in shift["participants"]}
            trainees = [p for p in assigned.values() if p["person_id"] != supervisor]
            if trainees and supervisor not in assigned:
                failures.append("missing supervisor")
            if any(p.get("supervisor_person_id") != supervisor for p in trainees):
                failures.append("missing pairing")
            if len(trainees) > 1:
                failures.append("supervisor capacity")
    return failures


@pytest.mark.parametrize("engine", ENGINES)
def test_overnight_trainee_rank_boundary_has_a_valid_named_pair(h, engine):
    trainee, supervisor = h.seed["people"][:2]
    h.command("coordinator", "membership.create", {"person_id": trainee,
              "category_id": h.seed["trainee_category"], "starts_on": "2026-10-05"})
    typ = h.configure("assignment_types", {"name": "Audit overnight rank transition", "starts_on": "2020-01-01",
        "definition": {"requirements": [{"minimum": 2, "maximum": 2}], "supervision": {
            "trainee_category_ids": [h.seed["trainee_category"]], "capability_id": h.seed["supervisor_capability"],
            "trainees_per_supervisor": 1, "require_pairing": True}}})["record"]["id"]
    workspace = h.shift(h.workspace(), people=[], typ=typ, start="2026-10-04T20:00:00+03:00",
                        end="2026-10-05T04:00:00+03:00")["workspace"]
    data = h.snapshot(workspace)
    data["people"] = {p: data["people"][p] for p in (trainee, supervisor)}
    witness = copy.deepcopy(data)
    shift = witness["shifts"][0]
    shift["participants"] = [_assignment(shift, trainee, participation="trainee", supervisor_person_id=supervisor),
                             _assignment(shift, supervisor, participation="supervisor")]
    assert _stamp(shift["start"]).astimezone(ZoneInfo("Asia/Riyadh")).date().isoformat() == "2026-10-04"
    assert _stamp(shift["end"]).date().isoformat() == "2026-10-05"
    assert not _violations(witness, "supervision", trainee, supervisor)
    assert evaluate(witness)["valid"], evaluate(witness)
    result = ENGINES[engine](data, time_budget_seconds=3)
    assert result["status"] == "candidate_feasible", result
    assert not _violations(_apply(data, result), "supervision", trainee, supervisor)


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("trainee_half", ["before", "after"])
@pytest.mark.parametrize("supervisor_half", ["before", "after", "both"])
def test_rank_boundary_pairing_requires_authority_for_each_trainee_segment(h, engine, trainee_half, supervisor_half):
    trainee, supervisor = h.seed["people"][:2]
    rank = h.seed["trainee_category"]
    dates = {"starts_on": "2026-10-05"} if trainee_half == "after" else {
        "starts_on": "2020-01-01", "ends_on": "2026-10-05"}
    h.command("coordinator", "membership.create", {"person_id": trainee, "category_id": rank, **dates})
    typ = h.configure("assignment_types", {"name": "Segment authority", "starts_on": "2020-01-01",
        "definition": {"requirements": [{"minimum": 2, "maximum": 2}], "supervision": {
            "trainee_category_ids": [rank], "capability_id": h.seed["supervisor_capability"],
            "trainees_per_supervisor": 1, "require_pairing": True}}})["record"]["id"]
    workspace = h.shift(h.workspace(), people=[], typ=typ, start="2026-10-04T20:00:00+03:00",
                        end="2026-10-05T04:00:00+03:00")["workspace"]
    data = h.snapshot(workspace)
    data["people"] = {p: data["people"][p] for p in (trainee, supervisor)}
    for grant in data["grants"]:
        if grant["person_id"] == supervisor and grant["capability_id"] == h.seed["supervisor_capability"]:
            if supervisor_half == "before":
                grant["ends_on"] = "2026-10-05"
            elif supervisor_half == "after":
                grant["starts_on"] = "2026-10-05"
    witness = copy.deepcopy(data)
    shift = witness["shifts"][0]
    shift["participants"] = [_assignment(shift, trainee, supervisor_person_id=supervisor),
                             _assignment(shift, supervisor, participation="supervisor")]
    feasible = supervisor_half in {trainee_half, "both"}
    assert evaluate(witness)["valid"] is feasible
    result = ENGINES[engine](data, time_budget_seconds=3)
    if feasible:
        assert result["status"] == "candidate_feasible", result
        candidate = _apply(data, result)
        assert not _violations(candidate, "supervision", trainee, supervisor)
        assert evaluate(candidate)["valid"]
    else:
        assert result["status"] == ("requirements_conflict" if engine == "cp_sat" else "candidate_with_gaps"), result
        assert all(p["person_id"] != trainee for s in _apply(data, result)["shifts"] for p in s["participants"])


@pytest.mark.parametrize("engine", ENGINES)
def test_nonoverlapping_trainee_segments_share_one_supervisor_place(h, engine):
    first, second, supervisor = h.seed["people"]
    rank = h.seed["trainee_category"]
    for person, dates in ((first, {"starts_on": "2020-01-01", "ends_on": "2026-10-05"}),
                          (second, {"starts_on": "2026-10-05"})):
        h.command("coordinator", "membership.create", {"person_id": person, "category_id": rank, **dates})
    typ = h.configure("assignment_types", {"name": "Changing trainee cohort", "starts_on": "2020-01-01",
        "definition": {"requirements": [{"minimum": 3, "maximum": 3}], "supervision": {
            "trainee_category_ids": [rank], "capability_id": h.seed["supervisor_capability"],
            "trainees_per_supervisor": 1, "require_pairing": True}}})["record"]["id"]
    workspace = h.shift(h.workspace(), people=[], typ=typ, start="2026-10-04T20:00:00+03:00",
                        end="2026-10-05T04:00:00+03:00")["workspace"]
    data = h.snapshot(workspace)
    data["grants"] = [g for g in data["grants"] if g["person_id"] not in {first, second}]
    witness = copy.deepcopy(data)
    shift = witness["shifts"][0]
    shift["participants"] = [_assignment(shift, person, supervisor_person_id=supervisor) for person in (first, second)]
    shift["participants"].append(_assignment(shift, supervisor, participation="supervisor"))
    assert evaluate(witness)["valid"]
    result = ENGINES[engine](data, time_budget_seconds=3)
    assert result["status"] == "candidate_feasible", result
    candidate = _apply(data, result)
    assigned = {p["person_id"]: p for p in candidate["shifts"][0]["participants"]}
    assert all(assigned[person]["supervisor_person_id"] == supervisor for person in (first, second))
    assert evaluate(candidate)["valid"]
# Include this file in hosted supervision and team-lead qualification.

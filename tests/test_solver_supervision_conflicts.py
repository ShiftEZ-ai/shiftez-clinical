"""Retained overlapping commitments cannot provide supervision for new trainees."""
import copy

import pytest

from packages.domain.cp_sat import generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.time import aware
from packages.domain.validation import evaluate, finding_observations


def supervision_snapshot(h, pairing="counted"):
    trainee, supervisor = h.seed["people"][:2]
    rank = h.seed["trainee_category"]
    h.command("coordinator", "membership.create", {
        "person_id": trainee, "category_id": rank, "starts_on": "2020-01-01"})
    if pairing == "exact":
        h.policy({"kind": "supervision_limit", "mode": "allowed_ranks", "trainee_category_ids": [rank]},
                 {"person_id": supervisor})
    typ = h.configure("assignment_types", {
        "name": "Synthetic overlapping supervision", "starts_on": "2020-01-01",
        "definition": {"requirements": [{"minimum": 2, "target": 2, "maximum": 2}],
                       "supervision": {"trainee_category_ids": [rank],
                                       "capability_id": h.seed["supervisor_capability"],
                                       "trainees_per_supervisor": 1,
                                       "require_pairing": pairing != "aggregate"}}})["record"]["id"]
    other_type = h.configure("assignment_types", {
        "name": "Synthetic competing reservation", "starts_on": "2020-01-01",
        "definition": {"requirements": [{"minimum": 0, "maximum": 1}]}})["record"]["id"]
    start, end = "2026-10-04T08:00:00+03:00", "2026-10-04T12:00:00+03:00"
    workspace = h.shift(h.workspace(), typ=typ, people=[{
        "person_id": supervisor, "start": start, "end": end,
        "participation": "supervisor", "fixed": True}])["workspace"]
    workspace = h.shift(workspace, typ=other_type, location=1, people=[{
        "person_id": supervisor, "start": start, "end": end, "fixed": True}])["workspace"]
    data = h.snapshot(workspace)
    data["people"] = {pid: data["people"][pid] for pid in (trainee, supervisor)}
    data["generation_options"] = {"mode": "fill_gaps", "shortlist_width": 0}
    training = next(s for s in data["shifts"] if s["assignment_type_id"] == typ)
    other = next(s for s in data["shifts"] if s["assignment_type_id"] == other_type)
    return data, training, other, trainee, supervisor


def add_trainee(training, trainee, supervisor):
    training["participants"].append({
        "id": "synthetic-trainee", "shift_id": training["id"], "person_id": trainee,
        "start": training["start"], "end": training["end"], "participation": "trainee",
        "supervisor_person_id": supervisor, "overtime": False, "revision": 1})


@pytest.mark.parametrize("case,blocked", [
    ("full-overlap", True), ("partial-overlap", True), ("reservation-break", True),
    ("outside-focus", True), ("touching", False), ("cancelled", False), ("removed", False)])
def test_validator_excludes_supervisor_with_conflicting_reserved_work(h, case, blocked):
    data, training, other, trainee, supervisor = supervision_snapshot(h)
    add_trainee(training, trainee, supervisor)
    if case in {"partial-overlap", "reservation-break"}:
        other["start"], other["end"] = "2026-10-04T09:00:00+03:00", "2026-10-04T11:00:00+03:00"
        other["participants"][0].update(start=other["start"], end=other["end"])
    if case == "reservation-break":
        other["participants"][0]["segments"] = [
            {"start": other["start"], "end": "2026-10-04T09:30:00+03:00"},
            {"start": "2026-10-04T10:30:00+03:00", "end": other["end"]}]
    if case == "outside-focus":
        data["focus_ids"] = [training["id"]]
    if case == "touching":
        other["start"], other["end"] = training["end"], "2026-10-04T16:00:00+03:00"
        other["participants"][0].update(start=other["start"], end=other["end"])
    if case == "cancelled":
        other["cancelled"] = True
    if case == "removed":
        other["participants"] = []
    findings = list(finding_observations(evaluate(data)))
    supervision = [f for f in findings if f["shift_id"] == training["id"]
                   and f["code"] in {"SUPERVISION_PAIRING", "SUPERVISION_GAP"}]
    assert bool(supervision) is blocked, findings
    assert any(f["code"] == "TIME_OVERLAP" for f in findings) is blocked
    if case == "reservation-break":
        assert any(f["code"] == "SUPERVISION_PAIRING"
                   and aware(f["start"]) <= aware("2026-10-04T09:30:00+03:00")
                   and aware(f["end"]) >= aware("2026-10-04T10:30:00+03:00") for f in supervision)


@pytest.mark.parametrize("pairing", ["counted", "exact", "aggregate"])
@pytest.mark.parametrize("engine", ["diagnostic", "ordinary", "greedy"])
def test_generation_never_pairs_new_trainees_to_a_double_booked_kept_supervisor(h, pairing, engine):
    data, training, _, trainee, supervisor = supervision_snapshot(h, pairing)
    source = copy.deepcopy(data)
    if engine == "greedy":
        result = greedy_generate(data, time_budget_seconds=5)
    else:
        result = generate(data, time_budget_seconds=5, diagnostic_proposal=engine == "diagnostic")
    assert result["status"] in {"candidate_with_gaps", "requirements_conflict"}, result
    assert not [a for a in result["additions"] if a["participant"]["person_id"] == trainee], result
    assert not result["removals"], result
    assert any(f["code"] == "TIME_OVERLAP" and f["person_id"] == supervisor
               for f in finding_observations(result.get("validation") or evaluate(data)))
    assert data == source


@pytest.mark.parametrize("supervisor_preference", [False, True])
def test_rebalance_can_remove_the_supervisors_other_assignment_before_pairing(h, supervisor_preference):
    data, training, other, trainee, supervisor = supervision_snapshot(h)
    if supervisor_preference:
        h.policy({"kind": "allocation_preference", "priority": 1, "supervisor_ids": [supervisor]},
                 {"person_id": trainee}, severity="advisory")
        data["policies"] = h.snapshot(data["workspace"])["policies"]
    other["participants"][0]["fixed"] = False
    data["generation_options"]["mode"] = "rebalance"
    result = generate(data, time_budget_seconds=5)
    assert result["status"] == "candidate_feasible", result
    assert any(r["assignment_id"] == other["participants"][0]["id"] for r in result["removals"]), result
    proposed = [a["participant"] for a in result["additions"] if a["shift_id"] == training["id"]]
    assert any(p["person_id"] == trainee and p["supervisor_person_id"] == supervisor for p in proposed), result
    assert result["validation"]["valid"], result


def test_validator_refreshes_supervision_when_another_session_changes(h):
    data, training, other, trainee, supervisor = supervision_snapshot(h)
    add_trainee(training, trainee, supervisor)
    other_people = other["participants"]
    other["participants"] = []
    session_cache = {}
    assert evaluate(data, session_cache=session_cache)["valid"]
    other["participants"] = other_people
    repeated = evaluate(data, session_cache=session_cache)
    assert any(f["code"] == "SUPERVISION_PAIRING" and f["person_id"] == trainee
               for f in finding_observations(repeated)), repeated


def test_named_supervisor_must_be_present_for_every_trainee_segment(h):
    data, training, other, trainee, supervisor = supervision_snapshot(h)
    other["participants"] = []
    add_trainee(training, trainee, supervisor)
    training["participants"][0]["segments"] = [
        {"start": training["start"], "end": "2026-10-04T09:00:00+03:00"},
        {"start": "2026-10-04T11:00:00+03:00", "end": training["end"]}]
    assert any(f["code"] == "SUPERVISION_PAIRING" and f["person_id"] == trainee
               for f in finding_observations(evaluate(data)))


@pytest.mark.parametrize("trainee_presence", ["outside-conflict", "during-conflict"])
def test_trainee_presence_outside_another_reservation_can_still_be_supervised(h, trainee_presence):
    data, training, other, trainee, supervisor = supervision_snapshot(h)
    add_trainee(training, trainee, supervisor)
    other["start"], other["end"] = "2026-10-04T09:00:00+03:00", "2026-10-04T11:00:00+03:00"
    other["participants"][0].update(start=other["start"], end=other["end"])
    trainee_row = training["participants"][-1]
    trainee_row["segments"] = [
        {"start": training["start"], "end": other["start"]},
        {"start": other["end"], "end": training["end"]}]
    if trainee_presence == "during-conflict":
        trainee_row["segments"][0]["end"] = "2026-10-04T10:00:00+03:00"
    findings = list(finding_observations(evaluate(data)))
    pairing_errors = [f for f in findings if f["code"] == "SUPERVISION_PAIRING"
                      and f["person_id"] == trainee]
    assert not any(f["code"] == "PARTICIPATION_INVALID" for f in findings), findings
    assert bool(pairing_errors) is (trainee_presence == "during-conflict"), pairing_errors


@pytest.mark.parametrize("engine", ["greedy", "ordinary", "diagnostic"])
@pytest.mark.parametrize("pairing", ["aggregate", "counted", "exact"])
def test_generation_does_not_worsen_an_existing_supervision_gap(h, engine, pairing):
    data, training, _, trainee, supervisor = supervision_snapshot(h, pairing)
    another_trainee = h.seed["people"][2]
    h.command("coordinator", "membership.create", {
        "person_id": another_trainee, "category_id": h.seed["trainee_category"], "starts_on": "2020-01-01"})
    current = h.snapshot(data["workspace"])
    data["people"][another_trainee] = current["people"][another_trainee]
    data["memberships"] = current["memberships"]
    add_trainee(training, trainee, supervisor)
    training["participants"][-1].update(
        fixed=True, supervisor_person_id=None if pairing == "aggregate" else supervisor)
    for definition in (training["type_snapshot"]["definition"], data["types"][training["assignment_type_id"]]["definition"]):
        definition["requirements"][0].update(minimum=3, target=3, maximum=3)
    result = (greedy_generate(data, time_budget_seconds=5) if engine == "greedy" else
              generate(data, time_budget_seconds=5, diagnostic_proposal=engine == "diagnostic"))
    assert not [a for a in result["additions"] if a["participant"]["person_id"] == another_trainee], result
    assert result["status"] in {"candidate_with_gaps", "requirements_conflict"}, result


@pytest.mark.parametrize("diagnostic", [False, True])
def test_unchanged_supervision_gap_outside_selected_sessions_does_not_block_safe_work(h, diagnostic):
    data, original, _, trainee, supervisor = supervision_snapshot(h)
    workspace = h.shift(data["workspace"], typ=original["assignment_type_id"], people=[],
                        start="2026-10-05T08:00:00+03:00", end="2026-10-05T12:00:00+03:00")["workspace"]
    refreshed = h.snapshot(workspace)
    data["workspace"] = refreshed["workspace"]
    selected = next(s for s in refreshed["shifts"] if s["start"].startswith("2026-10-05"))
    data["shifts"].append(selected)
    data["focus_ids"].append(selected["id"])
    data["generation_session_ids"] = [selected["id"]]
    add_trainee(original, trainee, supervisor)
    original["participants"][-1]["fixed"] = True
    result = generate(data, time_budget_seconds=5, diagnostic_proposal=diagnostic)
    assert result["status"] == "candidate_with_gaps", result
    assert {a["shift_id"] for a in result["additions"]} == {selected["id"]}, result
    assert {a["participant"]["person_id"] for a in result["additions"]} == {trainee, supervisor}, result
    assert not result["removals"], result

"""Session trials reuse independent checks while supervision follows current reservations."""
import copy
from collections import Counter

import pytest

from packages.domain import validation
from packages.domain.generation import generate


def snapshot(h):
    trainee, supervisor, other_person = h.seed["people"]
    h.command("coordinator", "membership.create", {
        "person_id": trainee, "category_id": h.seed["trainee_category"], "starts_on": "2020-01-01"})
    typ = h.configure("assignment_types", {"name": "Cache supervised duty", "starts_on": "2020-01-01",
        "definition": {"requirements": [], "supervision": {
            "trainee_category_ids": [h.seed["trainee_category"]], "capability_id": h.seed["supervisor_capability"],
            "trainees_per_supervisor": 1, "require_pairing": True}}})["record"]["id"]
    plain = h.configure("assignment_types", {"name": "Cache independent duty", "starts_on": "2020-01-01",
        "definition": {"requirements": []}})["record"]["id"]
    workspace = h.workspace()
    for start, end in (("2026-10-04T08:00:00+03:00", "2026-10-04T12:00:00+03:00"),
                       ("2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00"),
                       ("2026-10-04T12:00:00+03:00", "2026-10-04T16:00:00+03:00")):
        people = [{"person_id": supervisor, "start": start, "end": end,
                   "participation": "supervisor", "fixed": True},
                  {"person_id": trainee, "start": start, "end": end,
                   "participation": "trainee", "supervisor_person_id": supervisor}]
        workspace = h.shift(workspace, typ=typ, people=people, start=start, end=end)["workspace"]
    workspace = h.shift(workspace, typ=plain, location=1, people=[],
                        start="2026-10-04T09:00:00+03:00", end="2026-10-04T11:00:00+03:00")["workspace"]
    data = h.snapshot(workspace)
    ordered = sorted((s for s in data["shifts"] if s["assignment_type_id"] == typ), key=lambda s: s["start"])
    first, adjacent, distant = ordered
    independent = next(s for s in data["shifts"] if s["assignment_type_id"] == plain)
    return data, first, adjacent, distant, independent, trainee, supervisor, other_person


def assignment(shift, person, **extra):
    return {"id": "cache-" + person, "shift_id": shift["id"], "person_id": person,
            "start": shift["start"], "end": shift["end"], "participation": "staff",
            "overtime": False, "revision": 1, **extra}


def count_checks(monkeypatch):
    counts = Counter()
    original = validation.validate_session_setup

    def counted(data, shift, *args, **kwargs):
        counts[shift["id"]] += 1
        return original(data, shift, *args, **kwargs)

    monkeypatch.setattr(validation, "validate_session_setup", counted)
    return counts


def test_unchanged_supervised_sessions_reuse_their_checks(h, monkeypatch):
    data, *_ = snapshot(h)
    counts, cache = count_checks(monkeypatch), {}
    before = validation.evaluate(data, session_cache=cache)
    counts.clear()
    after = validation.evaluate(data, session_cache=cache)
    assert counts == {}, counts
    assert after == before


def test_new_supervisor_reservation_rechecks_only_the_intersecting_supervised_session(h, monkeypatch):
    data, first, _, _, independent, _, supervisor, _ = snapshot(h)
    counts, cache = count_checks(monkeypatch), {}
    validation.evaluate(data, session_cache=cache)
    changed = {**independent, "participants": [assignment(independent, supervisor)]}
    trial = {**data, "shifts": [changed if s is independent else s for s in data["shifts"]]}
    counts.clear()
    after = validation.evaluate(trial, session_cache=cache)
    assert counts == {first["id"]: 1, independent["id"]: 1}, counts
    assert any(f["code"] == "SUPERVISION_PAIRING" and f["shift_id"] == first["id"]
               for f in validation.finding_observations(after))
    assert after == validation.evaluate(trial)


@pytest.mark.parametrize('start,end', [
    ('2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'),
    ('2026-10-04T10:00:00+03:00', '2026-10-04T12:00:00+03:00'),
    ('2026-10-04T08:00:00+03:00', '2026-10-04T10:00:00+03:00')])
@pytest.mark.parametrize('explain_findings', [False, True])
def test_repeated_cache_reads_keep_supervisor_dependencies_when_reservation_is_removed(h, start, end, explain_findings):
    data, first, _, _, independent, _, supervisor, _ = snapshot(h)
    competing = {**independent, 'start': start, 'end': end}
    competing['participants'] = [assignment(competing, supervisor)]
    blocked = {**data, 'shifts': [competing if row is independent else row for row in data['shifts']]}
    cache = {}
    for view, expected_gap in ((blocked, True), (data, False), (blocked, True), (data, False)):
        fresh = validation.evaluate(view, explain_findings=explain_findings)
        for _ in range(3):
            cached = validation.evaluate(view, session_cache=cache, explain_findings=explain_findings)
            assert cached == fresh
            codes = {row['code'] for row in validation.finding_observations(cached) if row['shift_id'] == first['id']}
            supervision_codes = {'SUPERVISION_GAP', 'SUPERVISION_PAIRING'}
            assert codes & supervision_codes == (supervision_codes if expected_gap else set())


@pytest.mark.parametrize("change", ["cancel", "remove-session", "remove-person", "move-reservation", "replace-person"])
def test_supervision_cache_tracks_in_place_changes_to_independent_reservations(h, monkeypatch, change):
    data, first, adjacent, _, independent, _, supervisor, other_person = snapshot(h)
    independent["participants"] = [assignment(independent, supervisor)]
    counts, cache = count_checks(monkeypatch), {}
    validation.evaluate(data, session_cache=cache)
    expected = {first["id"]: 1, independent["id"]: 1}
    if change == "cancel":
        independent["cancelled"] = True
        expected.pop(independent["id"])
    elif change == "remove-session":
        data["shifts"].remove(independent)
        data["focus_ids"].remove(independent["id"])
        expected.pop(independent["id"])
    elif change == "remove-person":
        independent["participants"].clear()
    elif change == "replace-person":
        independent["participants"][0]["person_id"] = other_person
    else:
        independent.update(start="2026-10-04T12:00:00+03:00", end="2026-10-04T14:00:00+03:00")
        independent["participants"][0].update(start=independent["start"], end=independent["end"])
        expected[adjacent["id"]] = 1
    counts.clear()
    after = validation.evaluate(data, session_cache=cache)
    assert counts == expected, counts
    assert after == validation.evaluate(data)


@pytest.mark.parametrize("start,end,affected", [
    ("2026-10-04T07:00:00+03:00", "2026-10-04T08:00:00+03:00", None),
    ("2026-10-04T11:00:00+03:00", "2026-10-04T12:00:00+03:00", "first"),
    ("2026-10-04T12:00:00+03:00", "2026-10-04T13:00:00+03:00", "adjacent")])
def test_supervision_cache_uses_half_open_reservation_boundaries(h, monkeypatch, start, end, affected):
    data, first, adjacent, _, independent, _, supervisor, _ = snapshot(h)
    counts, cache = count_checks(monkeypatch), {}
    validation.evaluate(data, session_cache=cache)
    independent.update(start=start, end=end)
    independent["participants"] = [assignment(independent, supervisor)]
    expected = {independent["id"]: 1}
    if affected:
        expected[(first if affected == "first" else adjacent)["id"]] = 1
    counts.clear()
    after = validation.evaluate(data, session_cache=cache)
    assert counts == expected, counts
    assert after == validation.evaluate(data)


def test_trainee_reservation_does_not_invalidate_other_session_supervision(h, monkeypatch):
    data, _, _, _, independent, trainee, _, _ = snapshot(h)
    counts, cache = count_checks(monkeypatch), {}
    validation.evaluate(data, session_cache=cache)
    independent["participants"].append(assignment(independent, trainee, participation="trainee"))
    counts.clear()
    after = validation.evaluate(data, session_cache=cache)
    assert counts == {independent["id"]: 1}, counts
    assert any(f["code"] == "TIME_OVERLAP" and f["person_id"] == trainee
               for f in validation.finding_observations(after))
    assert after == validation.evaluate(data)


def test_cache_detects_in_place_edits_to_the_supervised_sessions_own_participants(h, monkeypatch):
    data, first, _, _, _, trainee, _, _ = snapshot(h)
    counts, cache = count_checks(monkeypatch), {}
    validation.evaluate(data, session_cache=cache)
    next(p for p in first["participants"] if p["person_id"] == trainee)["supervisor_person_id"] = None
    counts.clear()
    after = validation.evaluate(data, session_cache=cache)
    assert counts == {first["id"]: 1}, counts
    assert any(f["code"] == "SUPERVISION_PAIRING" and f["person_id"] == trainee
               for f in validation.finding_observations(after))
    assert after == validation.evaluate(data)


@pytest.mark.parametrize("supplied_commitments", [False, True])
def test_evaluate_keeps_the_callers_snapshot_and_commitment_index_unchanged(h, supplied_commitments):
    data, _, _, _, _, _, supervisor, _ = snapshot(h)
    if supplied_commitments:
        data["_supervision_commitments"] = {supervisor: [("caller-owned", "sentinel", "sentinel")]}
    original = copy.deepcopy(data)
    validation.evaluate(data, session_cache={})
    assert data == original
    assert ("_supervision_commitments" in data) is supplied_commitments


def test_greedy_rebuilds_supervision_after_removing_a_competing_reservation(h):
    data, first, _, _, independent, trainee, supervisor, _ = snapshot(h)
    data["shifts"] = [first, independent]
    data["focus_ids"] = [first["id"], independent["id"]]
    data["people"] = {p: data["people"][p] for p in (trainee, supervisor)}
    first["participants"] = [p for p in first["participants"] if p["person_id"] == supervisor]
    independent["participants"] = [assignment(independent, supervisor, fixed=False)]
    for definition in (first["type_snapshot"]["definition"], data["types"][first["assignment_type_id"]]["definition"]):
        definition["requirements"] = [{"minimum": 2, "target": 2, "maximum": 2,
                                       "from_minute": 0, "to_minute": None,
                                       "capabilities": [], "distinct_capability_seats": False}]
    data["generation_options"] = {"mode": "rebalance"}
    original = copy.deepcopy(data)
    result = generate(data, time_budget_seconds=5)
    assert result["status"] == "candidate_feasible", result
    assert any(r["assignment_id"] == independent["participants"][0]["id"] for r in result["removals"]), result
    assert any(a["shift_id"] == first["id"] and a["participant"]["person_id"] == trainee
               and a["participant"]["supervisor_person_id"] == supervisor for a in result["additions"]), result
    assert data == original
# Include this file in hosted supervision and team-lead qualification.

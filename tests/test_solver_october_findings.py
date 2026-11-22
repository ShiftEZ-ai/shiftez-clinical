"""Synthetic October shortfalls retain canonical detail and honest search status."""
import copy

import pytest

from packages.domain.solver_diagnostics import explain_unmet_requirements
from packages.domain.validation import evaluate, finding_observations
from packages.domain.cp_sat import generate


@pytest.fixture
def october_shortfalls(h):
    intern, *fellows = h.seed["people"][:3]
    optional = h.configure("assignment_types", {
        "name": "Synthetic clinical session", "starts_on": "2020-01-01",
        "definition": {"requirements": [], "session_units": 1}})["record"]["id"]
    quota = h.policy({"kind": "block_quota", "starts_on": "2026-10-04", "ends_on": None,
                      "repeat": True, "window_alignment": "fixed", "window_days": 7,
                      "metric": "session_units", "comparison": "minimum", "amount": 6},
                     {"person_id": intern})
    sport = h.cover_target(2, {"person_ids": fellows, "service_id": h.seed["services"][1]})
    workspace = h.workspace(start="2026-10-04", end="2026-10-18")
    for day in ("2026-10-04", "2026-10-05", "2026-10-11", "2026-10-12"):
        workspace = h.shift(workspace, start=day + "T08:00:00+03:00", end=day + "T12:00:00+03:00",
                            typ=optional, people=[intern])["workspace"]
    for day in ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
                "2026-10-12", "2026-10-13", "2026-10-14"):
        workspace = h.shift(workspace, start=day + "T13:00:00+03:00", end=day + "T16:00:00+03:00",
                            typ=optional, service=1, people=[])["workspace"]
    data = h.snapshot(workspace)
    return data, evaluate(data), quota["id"], sport["id"], intern


def objectives(*, coverage=True, minima=True, weighted=True, stalled=()):
    return [{"name": name, "proven_optimal": proven, "stopped_without_improvement": name in stalled}
            for name, proven in (("coverage", coverage), ("diagnostic_minima", minima), ("weighted", weighted))]


def test_october_diagnostics_keep_both_weekly_minima_and_every_scoped_target(october_shortfalls):
    data, validation, quota, sport, intern = october_shortfalls
    canonical = [f for f in finding_observations(validation)
                 if f["code"] in {"QUOTA_BELOW_MINIMUM", "SCOPED_COVERAGE_GAP"}]
    assert len(canonical) == 9
    rows = explain_unmet_requirements(data, validation, objectives(
        minima=False, weighted=False, stalled={"diagnostic_minima", "weighted"}))
    weekly = [row for row in rows if row["rule_id"] == quota]
    targets = [row for row in rows if row["rule_id"] == sport]
    assert len(weekly) == 2 and len(targets) == 7
    assert {row["person_id"] for row in weekly} == {intern}
    assert len({(row["start"], row["end"]) for row in weekly}) == 2
    assert {(row["actual"], row["required"], row["tier"], row["severity"]) for row in weekly} == {
        (2, 6, "diagnostic_minima", "error")}
    assert len({row["shift_id"] for row in targets}) == 7
    assert {(row["actual"], row["required"], row["tier"], row["severity"]) for row in targets} == {
        (0, 2, "weighted", "warning")}
    assert all(row["code"] == "SEARCH_STOPPED_WITHOUT_IMPROVEMENT" and row["proven"] is False for row in rows)
    assert all(str(row["actual"]) in row["message"] and str(row["required"]) in row["message"] for row in rows)
    assert {(row["finding_code"], row["rule_id"], row["shift_id"], row["person_id"], row["start"], row["end"])
            for row in rows} == {
        (f["code"], f["rule_id"], f["shift_id"], f["person_id"], f["start"], f["end"]) for f in canonical}


def test_october_diagnostics_use_the_relevant_and_earlier_objective_receipts(october_shortfalls):
    data, validation, quota, sport, _ = october_shortfalls
    cases = [
        (objectives(minima=False), "SEARCH_INCOMPLETE", "SEARCH_INCOMPLETE"),
        (objectives(coverage=False), "SEARCH_INCOMPLETE", "SEARCH_INCOMPLETE"),
        (objectives(weighted=False, stalled={"weighted"}), "OBJECTIVE_TRADEOFF", "SEARCH_STOPPED_WITHOUT_IMPROVEMENT"),
        (objectives(), "OBJECTIVE_TRADEOFF", "OBJECTIVE_TRADEOFF"),
        (objectives()[:2], "OBJECTIVE_TRADEOFF", "SEARCH_INCOMPLETE"),
        ([], "SEARCH_INCOMPLETE", "SEARCH_INCOMPLETE"),
    ]
    for receipts, quota_code, sport_code in cases:
        rows = explain_unmet_requirements(data, validation, receipts)
        assert {row["code"] for row in rows if row["rule_id"] == quota} == {quota_code}, receipts
        assert {row["code"] for row in rows if row["rule_id"] == sport} == {sport_code}, receipts
        assert all(row["proven"] is False for row in rows)
        for row in rows:
            if row["code"] == "OBJECTIVE_TRADEOFF":
                assert "does not prove the individual requirement impossible" in row["message"]


def test_mandatory_scoped_cover_uses_coverage_priority(october_shortfalls):
    data, _, _, sport, _ = october_shortfalls
    data = copy.deepcopy(data)
    [line] = next(p for p in data["policies"] if p["id"] == sport)["definition"]["rule"]["lines"]
    line["numbers"] = {"min": {"value": line["numbers"]["target"]["value"], "level": "mandatory"}}
    rows = explain_unmet_requirements(data, evaluate(data), objectives(
        coverage=False, stalled={"coverage"}))
    targets = [row for row in rows if row["rule_id"] == sport]
    assert len(targets) == 7
    assert {(row["tier"], row["severity"], row["code"]) for row in targets} == {
        ("coverage", "error", "SEARCH_STOPPED_WITHOUT_IMPROVEMENT")}


def test_generated_diagnostic_includes_canonical_shortfall_and_visible_warning(h):
    person = h.seed["people"][0]
    typ = h.configure("assignment_types", {
        "name": "Synthetic single quota opportunity", "starts_on": "2020-01-01",
        "definition": {"requirements": [{"minimum": 1, "maximum": 1}]}})["record"]["id"]
    policy = h.policy({"kind": "block_quota", "starts_on": "2026-10-04", "ends_on": "2026-10-11",
                       "metric": "duties", "comparison": "minimum", "amount": 2}, {"person_id": person})
    data = h.snapshot(h.shift(h.workspace(), typ=typ, people=[])["workspace"])
    data["people"] = {person: data["people"][person]}
    result = generate(data, 10, workers=1, deterministic=True, diagnostic_proposal=True)
    assert result["status"] == "candidate_with_gaps", result
    finding, = [f for f in finding_observations(result["validation"])
                if f["code"] == "QUOTA_BELOW_MINIMUM" and f["rule_id"] == policy["id"]]
    diagnostic, = [row for row in result["allocation_diagnostics"] if row["rule_id"] == policy["id"]]
    assert diagnostic["finding_code"] == finding["code"]
    assert (diagnostic["actual"], diagnostic["required"]) == (
        finding["detail"]["actual"], finding["detail"]["amount"]) == (1, 2)
    assert diagnostic["message"] in result["warnings"]
    assert diagnostic["proven"] is False

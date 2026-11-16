"""A missing required person is one issue; independent staffing needs stay visible."""
import pytest

from packages.domain.validation import ENGINE_VERSION, evaluate


def requirement(h, minimum=1, **values):
    return {"minimum": minimum, "target": 2,
            "capabilities": [{"capability_id": h.seed["capability"], "minimum": 1}], **values}


def rota(h, requirements, people):
    typ = h.configure("assignment_types", {"name": "Coverage findings fixture", "starts_on": "2020-01-01",
        "definition": {"requirements": requirements}})["record"]["id"]
    result = h.shift(h.workspace(), typ=typ, people=people)
    return h.snapshot(result["workspace"]), result["validation"]


@pytest.mark.parametrize("backend", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_empty_session_has_one_issue_and_accurate_coverage_and_counts(request, backend):
    h = request.getfixturevalue(backend)
    data, validation = rota(h, [requirement(h)], [])
    assert validation["engine_version"] == ENGINE_VERSION
    assert validation["error_count"] == 1 and validation["warning_count"] == 0
    assert [f["code"] for f in validation["findings"]] == ["COVERAGE_GAP"]
    detail = validation["findings"][0]["detail"]
    assert all(row['cause']['code'] and row['fix_hints'] for row in detail['derived_findings'])
    detail = {**detail, 'derived_findings': [{key: row[key] for key in ('code', 'severity', 'detail')}
                                           for row in detail['derived_findings']]}
    assert detail == {"required": 1, "available": 0, "derived_findings": [
        {"code": "COVERAGE_TARGET", "severity": "warning", "detail": {"target": 2, "available": 0}},
        {"code": "CAPABILITY_GAP", "severity": "error",
         "detail": {"capability_id": h.seed["capability"], "required": 1, "available": 0}}]}
    assert [(r["required"], r["available_eligible"], r["status"]) for r in validation["coverage"]] == [(1, 0, "gap")]
    assert evaluate(data)["findings"] == validation["findings"]


def test_suppression_follows_intervals_and_keeps_distinct_capability_and_target_needs(h):
    data, _ = rota(h, [requirement(h, minimum=2, to_minute=120),
                       requirement(h, from_minute=120)], h.seed["people"][:1])
    data["grants"] = []
    validation = evaluate(data)
    before = [f["code"] for f in validation["findings"] if f["start"] == "2026-10-04T05:00:00+00:00"]
    after = [f["code"] for f in validation["findings"] if f["start"] == "2026-10-04T07:00:00+00:00"]
    assert before == ["COVERAGE_GAP"]
    assert set(after) == {"CAPABILITY_GAP", "COVERAGE_TARGET"}
    assert validation["error_count"] == 2 and validation["warning_count"] == 1
    assert [r["status"] for r in validation["coverage"]] == ["gap", "gap"]


def test_headcount_gap_suppresses_derived_findings_across_overlapping_requirements(h):
    _, validation = rota(h, [requirement(h, minimum=2, capabilities=[]),
                            requirement(h, minimum=0)], [])
    assert [f["code"] for f in validation["findings"]] == ["COVERAGE_GAP"]
    assert validation["error_count"] == 1 and validation["warning_count"] == 0
    assert [f["code"] for f in validation["findings"][0]["detail"]["derived_findings"]] == ["COVERAGE_TARGET", "CAPABILITY_GAP"]


def test_grouping_retains_the_greedy_shortfall_priority_and_counts_each_need_once(h):
    from packages.domain.generation import deficit_score
    _, validation = rota(h, [requirement(h, minimum=2), requirement(h, minimum=2)], [])
    assert validation["error_count"] == 1 and validation["warning_count"] == 0
    assert sum(row["code"] == "COVERAGE_TARGET"
               for row in validation["findings"][0]["detail"]["derived_findings"]) == 1
    # desired cover counts target - available since maintenance (design row 22); the duplicate requirement is still counted once
    assert deficit_score(validation) == (3, 0, 2)


def test_independent_group_cover_need_is_not_hidden_by_session_gap(h):
    group = h.policy({"kind": "minimum_cover", "minimum": 3, "aggregation": "service_group"},
                     {"service_ids": h.seed["services"][:2]})
    data, _ = rota(h, [requirement(h)], [])
    workspace = h.workspace("Other clinic cover")
    peer = h.shift(workspace, people=h.seed["people"][:1], service=1)["workspace"]
    data["shifts"] += h.snapshot(peer)["shifts"]
    data["focus_ids"] += [s["id"] for s in h.snapshot(peer)["shifts"]]
    validation = evaluate(data)
    assert {f["code"] for f in validation["findings"]} == {"COVERAGE_GAP", "SCOPED_COVERAGE_GAP"}
    assert validation["error_count"] == 2 and validation["warning_count"] == 0
    [pooled] = [f for f in validation["findings"] if f["rule_id"] == group["id"]]
    assert pooled["detail"]["required"] == 3 and pooled["detail"]["available"] == 1
    assert set(pooled["detail"]["affected_shift_ids"]) == set(data["focus_ids"])


def test_greedy_can_fill_a_place_when_a_skill_gap_becomes_visible(h):
    from packages.domain.generation import generate
    data, _ = rota(h, [requirement(h, minimum=2,
        capabilities=[{"capability_id": h.seed["capability"], "minimum": 2}])], [])
    people = sorted(h.seed["people"][:3])
    data["people"] = {pid: data["people"][pid] for pid in people}
    data["grants"] = [r for r in data["grants"] if r["person_id"] != people[0]]
    result = generate(data, 5)
    assert result["status"] == "candidate_feasible", result
    assert len(result["additions"]) == 3
    assert result["validation"]["error_count"] == 0

"""Current operational supervision and reach share the validator's interval facts."""
import copy
import json
from uuid import uuid4

import pytest

from packages.domain import validation
from packages.domain.time import aware
from tests.test_solver_supervision_cache import snapshot


@pytest.mark.parametrize("change", ["capability", "trainee_rank", "add", "remove"])
def test_operational_supervision_helpers_use_the_effective_definition(h, change):
    data, shift, _, _, _, trainee, supervisor, _ = snapshot(h)
    data["shifts"] = [shift]
    data["focus_ids"] = [shift["id"]]
    current = data["types"][shift["assignment_type_id"]]["definition"]
    person = next(p for p in shift["participants"] if p["person_id"] == trainee)
    person["participation"] = "staff"
    if change == "capability":
        current["supervision"]["capability_id"] = str(uuid4())
    elif change == "trainee_rank":
        current["supervision"]["trainee_category_ids"] = []
    elif change == "add":
        shift["type_snapshot"]["definition"]["supervision"] = None
    else:
        current["supervision"] = None
    historical = copy.deepcopy(data)
    data["operational"] = True
    refreshed = copy.deepcopy(data)
    refreshed["shifts"][0]["type_snapshot"]["definition"] = copy.deepcopy(current)
    policy = {"id": "capacity", "department_id": data["workspace"]["department_id"],
              "active": True, "status": "active", "starts_on": "2020-01-01",
              "definition": {"scope": {}, "rule": {"kind": "supervision_limit", "mode": "capacity",
                  "maximum": 1, "trainee_category_ids": []}}}
    start, end = aware(shift["start"]), aware(shift["end"])

    def observations(source):
        observed = source["shifts"][0]
        learner = next(p for p in observed["participants"] if p["person_id"] == trainee)
        mentor = next(p for p in observed["participants"] if p["person_id"] == supervisor)
        return {
            "trainees": validation.trainee_intervals(source, observed, learner),
            "participation": validation.participation_for(source, trainee, source["shifts"][0], start, end),
            "supervisor": validation.qualified_supervisor(source, observed, mentor, start, end),
            "counts": validation.supervision_counts(source, policy, source["shifts"][0], start, end),
            "findings": validation.evaluate(source, explain_findings=False)["findings"],
        }

    original = observations(historical)
    expected = observations(refreshed)
    actual = observations(data)
    assert actual == expected
    assert {**data, "operational": False} == historical
    assert observations({**data, "operational": False}) == original


def test_current_supervision_after_publication_preserves_issued_evidence(h):
    trainee, supervisor = h.seed["people"][:2]
    typ = h.configure("assignment_types", {"name": "Operational supervision", "starts_on": "2020-01-01",
        "definition": {"requirements": [], "supervision": {
            "trainee_category_ids": [h.seed["trainee_category"]], "capability_id": h.seed["supervisor_capability"],
            "trainees_per_supervisor": 1, "require_pairing": True}}})["record"]
    start, end = "2026-10-04T08:00:00+03:00", "2026-10-04T12:00:00+03:00"
    workspace = h.shift(h.workspace(), typ=typ["id"], people=[
        {"person_id": supervisor, "start": start, "end": end, "participation": "supervisor"},
        {"person_id": trainee, "start": start, "end": end, "participation": "trainee",
         "supervisor_person_id": supervisor}])["workspace"]
    issued = h.publish(workspace)
    url = "/api/publications/" + issued["publication_id"]
    original = h.get("coordinator", url)["snapshot"]
    issued_evidence = h.get("coordinator", url)["original_validation"]
    issued_evidence_bytes = json.dumps(issued_evidence, sort_keys=True, separators=(",", ":")).encode()
    report_url = "/api/reports/" + issued["publication_id"]
    report = h.get("coordinator", report_url)
    assert h.get("coordinator", url + "/coverage")["evaluation"]["valid"]
    skill = h.configure("capabilities", {"name": "New supervision skill", "starts_on": "2020-01-01"})["record"]["id"]
    definition = copy.deepcopy(typ["definition"])
    definition["supervision"]["capability_id"] = skill
    h.configure("assignment_types", {"name": typ["name"], "starts_on": typ["starts_on"],
        "definition": definition}, typ["id"], typ["revision"])
    checked = h.get("coordinator", url + "/coverage")["evaluation"]
    findings = {f["code"]: f for f in validation.finding_observations(checked)}
    # Ruling B: issued sessions keep the frozen capability_id; today's capability catalogue cannot rewrite them.
    assert not {"SUPERVISION_GAP", "SUPERVISION_PAIRING"} & findings.keys()
    assert next(row for row in checked["coverage"] if row["shift_id"] == original["shifts"][0]["id"])["places_open"] == 0
    assert checked["valid"]
    after = h.get("coordinator", url)
    assert after["snapshot"] == original
    assert json.dumps(after["original_validation"], sort_keys=True, separators=(",", ":")).encode() == issued_evidence_bytes
    after_report = h.get("coordinator", report_url)
    assert {k: v for k, v in after_report.items() if k != "as_of"} == {
        k: v for k, v in report.items() if k != "as_of"}
    data = h.snapshot(issued["workspace"])
    data["operational"] = True
    data["grants"] = [g for g in data["grants"] if not (
        g["person_id"] == supervisor and g["capability_id"] == h.seed["supervisor_capability"])]
    data["_person_rows"] = validation.index_person_rows(data)
    findings = {f["code"]: f for f in validation.finding_observations(validation.evaluate(data))}
    assert {"SUPERVISION_GAP", "SUPERVISION_PAIRING"} <= findings.keys()
    assert findings["SUPERVISION_GAP"]["detail"] == {
        "trainees": 1, "qualified_supervisors": 0, "required": 1, "available": 0}


@pytest.mark.parametrize("maximum", [0, 1])
@pytest.mark.parametrize("commitment", ["middle", "cancelled", "before", "after", "full", "trainee", "stale_index"])
def test_supervision_reach_matches_safe_windows_and_named_counts(h, maximum, commitment):
    from packages.contracts.schemas import PolicyDefinition
    from packages.domain.rule_map import reach
    from tests.test_solver_supervision_cache import assignment

    data, shift, _, _, independent, trainee, supervisor, _ = snapshot(h)
    data["shifts"] = [shift, independent]
    data["focus_ids"] = [shift["id"]]
    start, end = aware(shift["start"]), aware(shift["end"])
    if commitment == "before":
        independent.update(start="2026-10-04T06:00:00+03:00", end=shift["start"])
    elif commitment == "after":
        independent.update(start=shift["end"], end="2026-10-04T14:00:00+03:00")
    elif commitment == "full":
        independent.update(start=shift["start"], end=shift["end"])
    elif commitment == "cancelled":
        independent["cancelled"] = True
    person = trainee if commitment == "trainee" else supervisor
    independent["participants"] = [assignment(independent, person)]
    if commitment == "stale_index":
        data["_supervision_commitments"] = {supervisor: []}
    policy = {"id": str(uuid4()), "department_id": data["workspace"]["department_id"],
              "name": "Supervisor capacity", "status": "active", "active": True, "starts_on": "2020-01-01",
              "definition": PolicyDefinition.model_validate({"severity": "mandatory", "scope": {"person_id": supervisor},
                  "rule": {"kind": "supervision_limit", "mode": "capacity", "maximum": maximum}}).model_dump(mode="json")}
    data["policies"] = [policy]
    original = copy.deepcopy(data)
    result = reach(data, [policy], start, end, details=True)
    [line] = result["details"][policy["id"]]["lines"]
    [person] = line["people"]
    rows = line["sessions"]
    assert person["person_id"] == supervisor
    assert person["counts"] == rows
    expected = ([(start, aware(independent["start"])), (aware(independent["end"]), end)]
                if commitment in {"middle", "stale_index"} else [] if commitment == "full" else [(start, end)])
    assert [(aware(r["starts_on"]), aware(r["ends_on"])) for r in rows] == expected
    assert all(r["shift_id"] == shift["id"] and r["person_id"] == supervisor
               and r["actual"] == 1 and r["limit"] == maximum and r["comparison"] == "maximum" for r in rows)
    assert next(p for p in shift["participants"] if p["person_id"] == trainee)["supervisor_person_id"] == supervisor
    findings = list(validation.finding_observations(validation.evaluate(data, explain_findings=False)))
    unsafe = [(aware(f["start"]), aware(f["end"])) for f in findings
              if f["code"] in {"SUPERVISION_GAP", "SUPERVISION_PAIRING"}]
    assert unsafe == ([(aware(independent["start"]), aware(independent["end"]))] * 2
                      if commitment in {"middle", "full", "stale_index"} else [])
    scoped = [(aware(f["start"]), aware(f["end"])) for f in findings if f["code"] == "SUPERVISOR_SCOPED_CAPACITY"]
    assert scoped == (expected if maximum == 0 else [])
    assert data == original
# Include this file in hosted supervision and team-lead qualification.

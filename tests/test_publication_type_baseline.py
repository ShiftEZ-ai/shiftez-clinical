"""Issued duty types stay frozen; operational availability and safety still bind."""
from copy import deepcopy
from datetime import date

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import RequirementSegment, requirement_key
from packages.domain.cp_sat import generate
from packages.domain.rota_summary import normalized_check
from packages.domain.validation import effective_issued_definition, effective_type_definition, evaluate
from .test_published_operational import absence, frozen, issue


BACKENDS = pytest.mark.parametrize("backend", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
TYPE_CHANGES = ("active", "starts_on", "ends_on", "requirements", "eligibility", "supervision", "combined")


def change_type(h, change, type_id=None):
    type_id = type_id or h.seed["type"]
    typ = next(row for row in h.get("admin", "/api/catalogues")["assignment_types"]
               if row["id"] == type_id)
    value = {key: deepcopy(typ[key]) for key in ("name", "active", "starts_on", "ends_on", "definition")}
    if change in ("active", "combined"):
        value["active"] = False
    if change in ("starts_on", "combined"):
        value["starts_on"] = "2026-12-01"
    if change == "ends_on":
        value["ends_on"] = "2026-10-04"
    if change in ("requirements", "combined"):
        value["definition"]["requirements"][0]["minimum"] = 3
    if change in ("eligibility", "combined"):
        value["definition"]["allowed_category_ids"] = [h.seed["trainee_category"]]
    if change in ("supervision", "combined"):
        value["definition"]["supervision"] = {"trainee_category_ids": [h.seed["category"]],
            "capability_id": h.seed["supervisor_capability"], "trainees_per_supervisor": 1, "require_pairing": True}
    # Reproduce historic stored dates that today's catalogue date-exclusion guard rejects.
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.AssignmentType, typ["id"])
        for key in ("active", "starts_on", "ends_on", "definition"):
            setattr(row, key, date.fromisoformat(value[key]) if key.endswith("_on") and value[key] else value[key])
        row.revision += 1
        db.get(m.Organisation, h.seed["organisation_id"]).input_revision += 1


@BACKENDS
@pytest.mark.parametrize("change", TYPE_CHANGES)
def test_published_coverage_uses_issued_type_after_catalogue_change(request, backend, change):
    h = request.getfixturevalue(backend)
    _, pub, ids = issue(h, (4, 5))
    path = f"/api/publications/{pub}/coverage"
    original = h.get("coordinator", path)["evaluation"]
    before = frozen(h, pub)
    assert original["findings"] == []
    change_type(h, change)
    current_body = h.get("coordinator", path)
    current = current_body["evaluation"]
    assert current_body["basis"] == (
        "As published, re-checked against today's availability and safety rules. Not attendance.")
    if change in {"requirements", "supervision", "combined"}:
        assert current["coverage"] != original["coverage"]
    else:
        assert current["coverage"] == original["coverage"]
    notices = [f for f in current["findings"] if f["code"] == "TYPE_VERSION_CHANGED"]
    assert len(notices) == 1
    assert notices[0]["severity"] == "info"
    assert notices[0]["shift_id"] is None
    assert notices[0]["detail"]["type_id"] == h.seed["type"]
    assert notices[0]["detail"]["type_name"]
    assert notices[0]["detail"]["issued_session_count"] == len(ids)
    assert not any(f["code"] == "TYPE_VERSION_CHANGED" and f["severity"] != "info"
                   for f in current["findings"])
    assert current["error_count"] == sum(f["severity"] == "error" for f in current["findings"])
    assert current["warning_count"] == sum(f["severity"] == "warning" for f in current["findings"])
    if change in {"requirements", "supervision", "combined"}:
        assert any(f["severity"] == "error" and f["message"].startswith("Today's requirement:")
                   for f in current["findings"])
    else:
        assert not any(f["message"].startswith("Today's requirement:") for f in current["findings"])
        if change == "eligibility":
            assert current["error_count"] == original["error_count"]
            assert current["warning_count"] == original["warning_count"]
    assert frozen(h, pub) == before


@BACKENDS
@pytest.mark.parametrize("issued_shape", ["empty", "missing"])
def test_published_empty_requirement_rows_bind_current_minimum(request, backend, issued_shape):
    h = request.getfixturevalue(backend)
    typ = next(row for row in h.get("admin", "/api/catalogues")["assignment_types"]
               if row["id"] == h.seed["type"])
    issued_definition = deepcopy(typ["definition"])
    if issued_shape == "empty":
        issued_definition["requirements"] = []
    else:
        issued_definition.pop("requirements", None)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.AssignmentType, h.seed["type"])
        row.definition = issued_definition
        row.revision += 1
        db.get(m.Organisation, h.seed["organisation_id"]).input_revision += 1
    _, pub, _ = issue(h, (4,))
    current_definition = deepcopy(issued_definition)
    current_definition["requirements"] = [RequirementSegment(minimum=2).model_dump(mode="json")]
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.AssignmentType, h.seed["type"])
        row.definition = current_definition
        row.revision += 1
        db.get(m.Organisation, h.seed["organisation_id"]).input_revision += 1
    evaluation = h.get("coordinator", f"/api/publications/{pub}/coverage")["evaluation"]
    assert any(f["code"] == "COVERAGE_GAP" and f["severity"] == "error"
               and f["detail"]["required"] == 2
               and f["message"].startswith("Today's requirement:") for f in evaluation["findings"])


@BACKENDS
def test_published_feasibility_binds_current_minimum(request, backend):
    h = request.getfixturevalue(backend)
    person = h.seed["people"][0]
    typ = h.configure("assignment_types", {"name": "Published minimum", "starts_on": "2020-01-01",
        "definition": {"requirements": [{"minimum": 1, "maximum": 3}]}})["record"]
    workspace = h.shift(h.workspace("Published current minimum"), typ=typ["id"], people=[person])["workspace"]
    published = h.publish(workspace)["workspace"]
    definition = deepcopy(typ["definition"])
    definition["requirements"][0]["minimum"] = 2
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.AssignmentType, typ["id"])
        row.definition = definition
        row.revision += 1
        db.get(m.Organisation, h.seed["organisation_id"]).input_revision += 1
    data = h.snapshot(published)
    data["operational"] = True
    data["people"] = {person: data["people"][person]}
    result = generate(data, 5, feasibility_only=True, deterministic=True)
    assert result["status"] == "requirements_conflict", result


@BACKENDS
def test_published_feasibility_binds_current_pairing(request, backend):
    h = request.getfixturevalue(backend)
    trainee, supervisor = h.seed["people"][:2]
    h.command("coordinator", "membership.create", {"person_id": trainee,
        "category_id": h.seed["trainee_category"], "starts_on": "2020-01-01"})
    typ = h.configure("assignment_types", {"name": "Published pairing", "starts_on": "2020-01-01",
        "definition": {"requirements": [{"minimum": 1, "maximum": 2}], "supervision": {
            "trainee_category_ids": [h.seed["trainee_category"]],
            "capability_id": h.seed["supervisor_capability"], "trainees_per_supervisor": 1,
            "require_pairing": False}}})["record"]
    start, end = "2026-10-04T08:00:00+03:00", "2026-10-04T12:00:00+03:00"
    workspace = h.shift(h.workspace("Published current pairing"), start, end, typ=typ["id"], people=[
        {"person_id": supervisor, "start": start, "end": end, "participation": "supervisor", "fixed": True},
        {"person_id": trainee, "start": start, "end": end, "participation": "trainee", "fixed": True}])["workspace"]
    published = h.publish(workspace)["workspace"]
    definition = deepcopy(typ["definition"])
    definition["supervision"]["require_pairing"] = True
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.AssignmentType, typ["id"])
        row.definition = definition
        row.revision += 1
        db.get(m.Organisation, h.seed["organisation_id"]).input_revision += 1
    data = h.snapshot(published)
    data["operational"] = True
    data["people"] = {pid: data["people"][pid] for pid in (trainee, supervisor)}
    result = generate(data, 5, feasibility_only=True, deterministic=True)
    assert result["status"] == "requirements_conflict", result


def test_legacy_balance_demand_uses_tightened_published_minimum():
    from ortools.sat.python import cp_model
    from packages.domain.cp_sat_goals import add_balance_goals
    from .test_engine_fairness_review import A, B, T, duty, policy, world
    from .test_weighted_goals import STRENGTHS

    data = world([duty(1, 4, [A])])
    data["operational"] = True
    data["workspace"].update(id="issued", status="published")
    data["shifts"][0]["workspace_id"] = "issued"
    data["types"][T]["definition"] = {"requirements": [{"minimum": 4}]}
    data["policies"] = [policy({"metric": "duties", "aim": "equal_share", "share_by": "equal"})]
    original = deepcopy(data)

    def penalty(source):
        model = cp_model.CpModel()
        shift = source["shifts"][0]
        choices = {(shift["id"], pid): model.new_constant(int(pid == A)) for pid in (A, B)}
        participants = {(shift["id"], pid): {"person_id": pid, "start": shift["start"], "end": shift["end"]}
                        for pid in (A, B)}
        terms, _ = add_balance_goals(model, source, choices, participants, {}, {}, set(source["focus_ids"]), STRENGTHS)
        model.minimize(sum(terms))
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        solver.parameters.max_time_in_seconds = 2
        assert solver.solve(model) == cp_model.OPTIMAL
        return solver.objective_value

    materialized = deepcopy(data)
    materialized["operational"] = False
    materialized["shifts"][0]["type_snapshot"]["definition"]["requirements"] = [{"minimum": 4}]
    assert penalty(data) == penalty(materialized)
    assert penalty(data) < penalty({**data, "operational": False})
    assert data == original


@BACKENDS
def test_published_type_changes_emit_one_notice_per_changed_type(request, backend):
    h = request.getfixturevalue(backend)
    first = next(row for row in h.get("admin", "/api/catalogues")["assignment_types"]
                 if row["id"] == h.seed["type"])
    second = h.configure("assignment_types", {"name": "Second published duty", "starts_on": first["starts_on"],
        "definition": deepcopy(first["definition"])})["record"]
    w = h.workspace("Two changed duty types")
    w = h.shift(w, "2026-10-04T08:00:00+03:00", "2026-10-04T12:00:00+03:00", typ=h.seed["type"])["workspace"]
    w = h.shift(w, "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00", typ=second["id"])["workspace"]
    pub = h.publish(w)["publication_id"]
    change_type(h, "eligibility", h.seed["type"])
    change_type(h, "eligibility", second["id"])
    evaluation = h.get("coordinator", f"/api/publications/{pub}/coverage")["evaluation"]
    notices = [f for f in evaluation["findings"] if f["code"] == "TYPE_VERSION_CHANGED"]
    assert len(notices) == 2
    assert {f["detail"]["type_id"] for f in notices} == {h.seed["type"], second["id"]}
    assert all(f["severity"] == "info" and f["shift_id"] is None for f in notices)


@BACKENDS
def test_published_type_change_keeps_live_approved_leave(request, backend):
    h = request.getfixturevalue(backend)
    _, pub, ids = issue(h, (6,))
    before = frozen(h, pub)
    change_type(h, "combined")
    absence(h, "leave")
    findings = h.get("coordinator", f"/api/publications/{pub}/coverage")["evaluation"]["findings"]
    assert any(f["code"] == "UNAVAILABLE" and f["severity"] == "error" and f["shift_id"] == ids[0]
               for f in findings)
    assert not {"TYPE_INACTIVE", "NOT_ELIGIBLE", "SUPERVISION_MISSING"} & {f["code"] for f in findings}
    assert frozen(h, pub) == before


@BACKENDS
def test_published_type_change_keeps_live_minimum_rest(request, backend):
    h = request.getfixturevalue(backend)
    h.policy({"kind": "min_rest", "hours": 8})
    _, pub, ids = issue(h, (4,))
    path = f"/api/publications/{pub}/coverage"
    assert h.get("coordinator", path)["evaluation"]["valid"]
    before = frozen(h, pub)
    change_type(h, "combined")
    h.command("coordinator", "availability.record", {"person_id": h.seed["people"][0],
        "kind": "external_commitment", "start": "2026-10-04T00:00:00+03:00",
        "end": "2026-10-04T06:00:00+03:00", "private_reason": "Synthetic external duty"})
    findings = h.get("coordinator", path)["evaluation"]["findings"]
    assert any(f["code"] == "MIN_REST" and f["severity"] == "error" and f["shift_id"] == ids[0]
               for f in findings)
    assert frozen(h, pub) == before


@BACKENDS
def test_draft_validation_keeps_current_validity_and_frozen_definition(request, backend):
    h = request.getfixturevalue(backend)
    w = h.shift(h.workspace())["workspace"]
    change_type(h, "combined")
    findings = evaluate(h.snapshot(w))["findings"]
    assert {(f["code"], f["severity"]) for f in findings} == {
        ("TYPE_INACTIVE", "error"), ("TYPE_VERSION_CHANGED", "error"), ("CATALOGUE_VALIDITY_CHANGED", "warning")}


@pytest.mark.parametrize("status,shift_workspace,current", [
    ("draft", "own", True), ("published", "own", False), ("superseded", "own", False),
    ("draft", "published-context", False),
])
def test_operational_type_source_preserves_draft_preview_and_published_context(status, shift_workspace, current):
    issued, latest = {"requirements": [{"minimum": 1}]}, {"requirements": [{"minimum": 3}]}
    data = {"operational": True, "workspace": {"id": "own", "status": status},
            "types": {"type": {"definition": latest}}}
    shift = {"workspace_id": shift_workspace, "assignment_type_id": "type", "type_snapshot": {"definition": issued}}
    effective = effective_type_definition(data, shift)
    if current:
        assert effective == latest
    else:
        assert effective["requirements"][0]["minimum"] == 3


@BACKENDS
def test_operational_type_validity_still_checks_the_issued_dates(request, backend):
    h = request.getfixturevalue(backend)
    data = h.snapshot(h.shift(h.workspace())["workspace"])
    data["operational"] = True
    data["workspace"]["status"] = "published"
    for field, value in (("active", False), ("starts_on", "2026-12-01"), ("ends_on", "2026-10-04")):
        trial = deepcopy(data)
        trial["shifts"][0]["type_snapshot"][field] = value
        assert any(f["code"] == "TYPE_INACTIVE" and f["severity"] == "error" for f in evaluate(trial)["findings"])


def test_effective_issued_definition_tightens_safety_and_freezes_other_fields():
    issued = {"colour": "blue", "allowed_category_ids": ["issued"],
              "requirements": [{"from_minute": 0, "minimum": 2, "target": 3, "maximum": 5}],
              "supervision": {"trainee_category_ids": ["trainee"], "capability_id": "skill",
                              "trainees_per_supervisor": 3, "require_pairing": False}}
    current = {"colour": "red", "allowed_category_ids": ["current"],
              "requirements": [{"from_minute": 0, "minimum": 4, "target": 4, "maximum": 8}],
              "supervision": {"trainee_category_ids": ["other"], "capability_id": "other-skill",
                              "trainees_per_supervisor": 1, "require_pairing": True}}
    result = effective_issued_definition(issued, current)
    assert result["colour"] == "blue"
    assert result["allowed_category_ids"] == ["issued"]
    assert result["requirements"][0]["minimum"] == 4
    assert result["requirements"][0]["maximum"] == 5
    assert result["requirements"][0]["target"] == 3
    assert result["supervision"]["trainees_per_supervisor"] == 1
    assert result["supervision"]["require_pairing"] is True
    assert result["supervision"]["trainee_category_ids"] == ["trainee"]
    assert result["supervision"]["capability_id"] == "skill"


def test_effective_issued_definition_does_not_relax_safety():
    issued = {"requirements": [{"from_minute": 0, "minimum": 4, "maximum": 5}],
              "supervision": {"trainee_category_ids": ["trainee"], "capability_id": "skill",
                              "trainees_per_supervisor": 1, "require_pairing": True}}
    current = {"requirements": [{"from_minute": 0, "minimum": 2, "maximum": 8}],
              "supervision": {"trainee_category_ids": ["other"], "capability_id": "other-skill",
                              "trainees_per_supervisor": 3, "require_pairing": False}}
    result = effective_issued_definition(issued, current)
    assert result["requirements"][0]["minimum"] == 4
    assert result["requirements"][0]["maximum"] == 5
    assert result["supervision"]["trainees_per_supervisor"] == 1
    assert result["supervision"]["require_pairing"] is True


def test_requirement_key_is_public_and_canonical():
    assert requirement_key({"from_minute": 0, "to_minute": 60,
                            "capabilities": [{"capability_id": "b"}, {"capability_id": "a"},
                                             {"capability_id": "a"}],
                            "distinct_capability_seats": True}) == (0, 60, ("a", "a", "b"), True)


def test_effective_issued_definition_binds_current_minimum_without_issued_rows():
    for issued in ({}, {"requirements": []}):
        before = deepcopy(issued)
        result = effective_issued_definition(issued, {"requirements": [{"minimum": 2}]})
        assert result["requirements"] == [{"minimum": 2}]
        assert issued == before


def test_effective_issued_definition_keeps_same_slot_capabilities_separate():
    issued = {"requirements": [
        {"from_minute": 0, "to_minute": 60, "minimum": 1, "capabilities": [{"capability_id": "a", "minimum": 1}]},
        {"from_minute": 0, "to_minute": 60, "minimum": 1, "capabilities": [{"capability_id": "b", "minimum": 1}]},
    ]}
    current = {"requirements": [
        {"from_minute": 0, "to_minute": 60, "minimum": 1, "capabilities": [{"capability_id": "a", "minimum": 1}]},
        {"from_minute": 0, "to_minute": 60, "minimum": 1, "capabilities": [{"capability_id": "b", "minimum": 2}]},
    ]}
    result = effective_issued_definition(issued, current)
    assert [row["capabilities"][0]["minimum"] for row in result["requirements"]] == [1, 2]


def test_effective_issued_definition_binds_raised_capability_minimum():
    issued = {"requirements": [{"from_minute": 0, "to_minute": 60, "minimum": 1,
                                 "capabilities": [{"capability_id": "a", "minimum": 1}]}]}
    current = {"requirements": [{"from_minute": 0, "to_minute": 60, "minimum": 1,
                                  "capabilities": [{"capability_id": "a", "minimum": 3}]}]}
    result = effective_issued_definition(issued, current)
    assert result["requirements"][0]["capabilities"][0]["minimum"] == 3


def test_effective_issued_definition_appends_new_current_segment():
    issued = {"requirements": [{"from_minute": 0, "to_minute": 60, "minimum": 1}]}
    current = {"requirements": [
        {"from_minute": 0, "to_minute": 60, "minimum": 1},
        {"from_minute": 60, "to_minute": 120, "minimum": 2},
    ]}
    result = effective_issued_definition(issued, current)
    assert [(row["from_minute"], row["to_minute"], row["minimum"]) for row in result["requirements"]] == [
        (0, 60, 1), (60, 120, 2)]


def test_effective_issued_definition_retains_removed_current_segment():
    issued = {"requirements": [{"from_minute": 0, "to_minute": 60, "minimum": 3}]}
    result = effective_issued_definition(issued, {"requirements": []})
    assert result["requirements"] == issued["requirements"]


def test_info_findings_are_excluded_from_counts_and_fix_projection():
    info = {"code": "TYPE_VERSION_CHANGED", "severity": "info", "shift_id": None,
            "person_id": None, "rule_id": None, "start": None, "detail": {}}
    error = {"code": "COVERAGE_GAP", "severity": "error", "shift_id": "s", "person_id": None,
             "rule_id": None, "start": None, "detail": {}}
    warning = {"code": "CATALOGUE_VALIDITY_CHANGED", "severity": "warning", "shift_id": None,
               "person_id": None, "rule_id": None, "start": None, "detail": {}}
    result = normalized_check({"findings": [info, error, warning], "coverage": [],
                               "error_count": 99, "warning_count": 99})
    assert result["error_count"] == 1
    assert result["warning_count"] == 1
    assert [f["severity"] for f in result["findings"]] == ["info", "error", "warning"]

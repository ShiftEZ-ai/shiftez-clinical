"""The existing ``keep_off`` rule expresses rotation clinic allowlists.

One safety line says "none of these places".  Combining the allowed clinics in
one ``where`` tree makes the line an allowlist: a duty is blocked only when it
matches none of the selected services or locations.  These tests deliberately
exercise the API-authored catalogue line through every consumer instead of
adding a second rotation-specific policy model.
"""
from copy import deepcopy
import uuid

from packages.domain.candidate_evidence import candidate_evidence
from packages.domain.conditions import active_rules
from packages.domain.cp_sat import generate as cp_sat_generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.time import aware
from packages.domain.validation import qualified, restriction_blocks


def _line(*, rotation_type_id, service_id, location_id, starts_on="2026-10-04", ends_on="2026-10-11"):
    return {
        "id": str(uuid.uuid4()),
        "who": {"conditions": [{"field": "rotation_type_ids", "op": "any", "values": [rotation_type_id]}]},
        "where": {"conditions": [
            {"field": "service_ids", "op": "none", "values": [service_id]},
            {"field": "location_ids", "op": "none", "values": [location_id]},
        ]},
        "starts_on": starts_on,
        "ends_on": ends_on,
        "numbers": {"apply": {"value": None, "level": "mandatory"}},
    }


def _rotation_fixture(h, person_id, *, starts_on="2026-10-04", ends_on="2026-10-11"):
    programme = h.configure("programmes", {"name": "Allowed places programme", "starts_on": "2020-01-01"})["record"]
    cohort = h.configure("cohorts", {"name": "Allowed places cohort", "programme_id": programme["id"],
                                      "starts_on": "2020-01-01", "ends_on": "2027-01-01"})["record"]
    h.configure("programme_memberships", {"person_id": person_id, "programme_id": programme["id"],
                                           "starts_on": "2020-01-01", "ends_on": "2027-01-01"},
                role="coordinator")
    rotation_type = h.configure("rotation_types", {"name": "UCC-Procedure", "starts_on": "2020-01-01"})["record"]
    h.configure("rotations", {"person_id": person_id, "cohort_id": cohort["id"],
                               "rotation_type_id": rotation_type["id"], "service_id": h.seed["services"][0],
                               "starts_on": starts_on, "ends_on": ends_on}, role="coordinator")
    return rotation_type


def _activate_keep_off(h, rotation_type_id, *, service_id=None, location_id=None,
                       starts_on="2026-10-04", ends_on="2026-10-11"):
    service_id = service_id or h.seed["services"][0]
    location_id = location_id or h.seed["locations"][1]
    line = _line(rotation_type_id=rotation_type_id, service_id=service_id,
                 location_id=location_id, starts_on=starts_on, ends_on=ends_on)
    response = h.command("coordinator", "rule.propose", {
        "catalogue_id": "keep_off", "lines": [line], "acknowledged_overlaps": [],
        "reason": "Synthetic rotation clinic allowlist",
    })
    record = response["record"]
    live = h.command("approver", "policy.activate", {"reason": "Independent synthetic review"},
                     record["id"], record["revision"])["record"]
    return live, line


def _workspace_with_places(h, person_id, *, include_outside=True, assigned=True):
    w = h.workspace("keep off rotation places", "2026-10-04", "2026-10-13")
    participant = lambda start, end: [person_id] if assigned else []
    # Blocked: service 1 and location 0 match neither none-of selector.
    blocked = h.shift(w, "2026-10-04T08:00:00+03:00", "2026-10-04T12:00:00+03:00",
                      people=participant("", ""), service=1, location=0, seed_restricted=assigned)["workspace"]
    # Allowed through the service selector, even at location 0.
    service_allowed = h.shift(blocked, "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00",
                             people=participant("", ""), service=0, location=0)["workspace"]
    # Allowed through the location selector, even for service 1.
    location_allowed = h.shift(service_allowed, "2026-10-06T08:00:00+03:00", "2026-10-06T12:00:00+03:00",
                                people=participant("", ""), service=1, location=1)["workspace"]
    if include_outside:
        outside = h.shift(location_allowed, "2026-10-12T08:00:00+03:00", "2026-10-12T12:00:00+03:00",
                           people=participant("", ""), service=1, location=0)["workspace"]
    else:
        outside = location_allowed
    return outside


def test_keep_off_rotation_line_is_authored_and_explained_everywhere(h):
    person = h.seed["people"][0]
    rotation_type = _rotation_fixture(h, person)
    policy, source_line = _activate_keep_off(h, rotation_type["id"])
    w = _workspace_with_places(h, person)

    stored = next(entry for entry in h.get("coordinator", "/api/rules/catalogue")["entries"]
                   if entry["id"] == "keep_off")["rule"]
    stored_line = stored["definition"]["rule"]["lines"][0]
    assert stored_line["id"] == source_line["id"]
    assert {condition["op"] for condition in stored_line["where"]["conditions"]} == {"none"}
    assert stored_line["who"]["conditions"][0]["field"] == "rotation_type_ids"

    checked = h.lifecycle(w, "validate")["workspace"]
    data = h.snapshot(checked)
    rule_data = {**data, "policies": active_rules(data["policies"], context=data)}
    blocked, service_allowed, location_allowed, outside = sorted(data["shifts"], key=lambda row: row["start"])
    findings = [finding for finding in checked["validation"]["findings"] if finding.get("person_id") == person]
    restricted = [finding for finding in findings if finding["code"] == "RESTRICTION" and finding["rule_id"] == policy["id"]]
    assert [finding["shift_id"] for finding in restricted] == [blocked["id"]]
    assert not [finding for finding in restricted if finding["shift_id"] in {service_allowed["id"], location_allowed["id"], outside["id"]}]
    assert restriction_blocks(rule_data, person, blocked, aware(blocked["start"]), aware(blocked["end"]))
    assert qualified(rule_data, person, service_allowed, service_allowed["type_snapshot"]["definition"],
                     aware(service_allowed["start"]), aware(service_allowed["end"]))

    mapped = h.get("coordinator", f"/api/workspaces/{checked['id']}/rule-map")
    mapped_rule = next(row for row in mapped["rules"] if row["id"] == policy["id"])
    assert mapped_rule["sessions"] == [0]

    causes = h.get("coordinator", f"/api/workspaces/{checked['id']}/fix-list")["causes"]
    cause = next(cause for cause in causes if any(line["rule_id"] == policy["id"] for line in cause["rule_lines"]))
    assert cause["level"] == "must"
    assert "Keep off" in cause["sentence"]
    assert any(evidence["reason"] == "Keep off" for evidence in cause["evidence"])

    shift_rules = h.get("coordinator", f"/api/workspaces/{checked['id']}/shifts/{blocked['id']}/rules")
    [person_rules] = [row for row in shift_rules["people"] if row["person_id"] == person]
    applied = next(row for row in person_rules["rules"] if row["id"] == policy["id"])
    assert applied["status"] == "broken" and applied["safety"] is True
    assert applied["messages"] == ["Keep off"]

    applies = h.get("coordinator", f"/api/policies/applies?person_id={person}&day=2026-10-05")
    applied = next(row for row in applies["rules"] if row["policy_id"] == policy["id"])
    assert "safety: every line that holds applies" in applied["why"]


def test_keep_off_rotation_line_blocks_solver_greedy_candidates_and_fill_options(h):
    person = h.seed["people"][0]
    rotation_type = _rotation_fixture(h, person)
    policy, _ = _activate_keep_off(h, rotation_type["id"])
    empty = _workspace_with_places(h, person, assigned=False, include_outside=False)
    data = h.snapshot(empty)
    ordered = sorted(data["shifts"], key=lambda row: row["start"])
    blocked, service_allowed, location_allowed = ordered
    data["people"] = {person: data["people"][person]}
    data["policies"] = active_rules(data["policies"], context=data)

    for result in (cp_sat_generate(deepcopy(data), 5, 1, deterministic=True), greedy_generate(deepcopy(data), 5)):
        additions = {row["shift_id"]: row["participant"]["person_id"] for row in result["additions"]}
        assert blocked["id"] not in additions and set(additions) <= {service_allowed["id"], location_allowed["id"]}, result

    evidence = candidate_evidence(data, person, blocked)
    assert not evidence["eligible_and_available"]
    assert any(row["code"] == "RESTRICTION" and row["rule_id"] == policy["id"] for row in evidence["exclusions"])
    assert not any(row["code"] == "ELIGIBILITY_MISSING" for row in evidence["exclusions"])
    assert all(candidate_evidence(data, person, shift)["eligible_and_available"]
               for shift in (service_allowed, location_allowed))

    options = h.client("coordinator").post("/api/planner/fill-options", json={
        "workspace_id": empty["id"], "shift_id": blocked["id"], "expected_revision": empty["revision"],
    })
    assert options.status_code == 200, options.text
    candidate = next(row for row in options.json()["candidates"] if row["person_id"] == person)
    assert candidate["status"] == "blocked"
    assert any(finding["code"] == "RESTRICTION" for finding in candidate["findings"])


def test_keep_off_rotation_line_blocks_swap_recipient_with_public_wording(h):
    from tests.governance_support import identity
    from tests.test_swap_eligibility import recipient

    _, recipient_person = h.seed["people"][:2]
    identity(h, "staff_two", ["staff"], recipient_person)
    rotation_type = _rotation_fixture(h, recipient_person, starts_on="2026-10-10", ends_on="2026-10-17")
    _activate_keep_off(h, rotation_type["id"], service_id=h.seed["services"][1], location_id=h.seed["locations"][1],
                       starts_on="2026-10-10", ends_on="2026-10-17")
    w = h.workspace("keep off swap", "2026-10-10", "2026-10-17")
    w = h.shift(w, "2026-10-10T08:00:00+03:00", "2026-10-10T12:00:00+03:00",
                people=[h.seed["people"][0]], service=0, location=0)["workspace"]
    w = h.shift(w, "2026-10-11T08:00:00+03:00", "2026-10-11T12:00:00+03:00",
                people=[recipient_person], service=1, location=1)["workspace"]
    publication_id = h.publish(w)["publication_id"]
    source = h.get("coordinator", f"/api/publications/{publication_id}")["snapshot"]
    first = source["shifts"][0]["participants"][0]["id"]

    choice = recipient(h.get("staff", f"/api/swaps/options?assignment_id={first}&recipient_id={recipient_person}&kind=give"), recipient_person)
    assert choice["eligible"] is False
    assert "protected time" in choice["message"]
    assert any(finding["code"] == "RESTRICTION" and "protected time" in finding["message"]
               for finding in choice["findings"])
    response = h.command("staff", "swap.offer", {
        "kind": "give", "publication_id": publication_id, "assignment_id": first,
        "recipient_person_id": recipient_person, "expires_at": "2026-10-09T00:00:00+03:00",
        "reason": "Synthetic rotation clinic check",
    }, status=422)
    assert response["error"]["code"] == "SWAP_INELIGIBLE"
    assert "RESTRICTION" in {finding["code"] for finding in response["error"]["details"]["findings"]}

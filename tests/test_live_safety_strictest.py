"""Live safety revisions keep the strictest frozen value in an existing draft."""
from copy import deepcopy
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.workspace_baselines import live_safety_inputs, latest, verify
from packages.domain.validation import evaluate
from apps.api.workspace_baselines import _strictest_policy


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def database(request):
    return request.getfixturevalue(request.param)


def _findings(h, workspace):
    h.lifecycle(workspace, "validate")
    return evaluate(h.snapshot(workspace))["findings"]


def _has(findings, codes):
    return any(row.get("code") in codes for row in findings)


def _legacy_revision(h, current, hours):
    # policy.create with replaces_id creates a new policy id and retires the
    # predecessor on activation; no governed command creates a same-id legacy
    # revision, so this covers the real replacement path.
    proposal = h.command("coordinator", "policy.create", {
        "name": "Revised minimum rest",
        "owner": "Synthetic safety owner",
        "source": "Strictest live revision fixture",
        "starts_on": "2020-01-01",
        "definition": {"rule": {"kind": "min_rest", "hours": hours}, "replaces_id": current["id"]},
    })["record"]
    return h.command("approver", "policy.activate", {"reason": "Independent safety review"},
                     proposal["id"], proposal["revision"])["record"]


def _catalogue_revision(h, entry, current, line):
    proposal = h.command("coordinator", "rule.propose", {
        "catalogue_id": entry,
        "catalogue_version": 2,
        "expected_policy_id": current["id"],
        "expected_revision": current["revision"],
        "lines": [line],
        "reason": "Strictest live catalogue revision fixture",
    })["record"]
    return h.command("approver", "policy.activate", {"reason": "Independent catalogue review"},
                     proposal["id"], proposal["revision"])["record"]


# The governed rule.propose path always allocates a replacement Policy row;
# there is no command-level same-id catalogue line edit to exercise here.


def _catalogue(h, entry, number, key):
    line = {"id": str(uuid4()), "numbers": {key: {"value": number, "level": "mandatory"}}}
    proposal = h.command("coordinator", "rule.propose", {
        "catalogue_id": entry,
        "catalogue_version": 2,
        "lines": [line],
        "reason": "Strictest live catalogue fixture",
    })["record"]
    return h.command("approver", "policy.activate", {"reason": "Independent catalogue review"},
                     proposal["id"], proposal["revision"])["record"], line


def _rest_rota(h, person, gap_hours):
    workspace = h.workspace("Strictest rest draft")
    first_start = datetime.fromisoformat("2026-10-04T08:00:00+03:00")
    first_end = datetime.fromisoformat("2026-10-04T12:00:00+03:00")
    workspace = h.shift(workspace, people=[person],
                        start=first_start.isoformat(), end=first_end.isoformat())["workspace"]
    second_start = first_end + timedelta(hours=gap_hours)
    second_end = second_start + timedelta(hours=4)
    workspace = h.shift(workspace, people=[person],
                        start=second_start.isoformat(), end=second_end.isoformat())["workspace"]
    return workspace


@pytest.mark.parametrize("frozen,live,gap,expected", [(11, 12, 11.5, True), (12, 10, 11, True)],
                         ids=["tightening", "relaxation"])
def test_live_strictest_legacy_min_rest(database, frozen, live, gap, expected):
    h = database
    current = h.policy({"kind": "min_rest", "hours": frozen})
    draft = _rest_rota(h, h.seed["people"][0], gap)
    issued = h.publish(_rest_rota(h, h.seed["people"][1], 13))
    before = h.get("coordinator", "/api/publications/" + issued["publication_id"])["snapshot"]
    _legacy_revision(h, current, live)
    findings = _findings(h, draft)
    assert _has(findings, {"MIN_REST"}) is expected, findings
    fresh = _rest_rota(h, h.seed["people"][0], gap)
    assert _has(_findings(h, fresh), {"MIN_REST"}) is (live > gap)
    assert h.get("coordinator", "/api/publications/" + issued["publication_id"])["snapshot"] == before


@pytest.mark.parametrize("frozen,live,people,code,expected", [
    (2, 1, 1, {"COVERAGE_GAP", "SCOPED_COVERAGE_GAP"}, True),
    (2, 3, 2, {"COVERAGE_GAP", "SCOPED_COVERAGE_GAP"}, True),
], ids=["relaxation", "tightening"])
def test_live_strictest_catalogue_minimum_cover(database, frozen, live, people, code, expected):
    h = database
    current, line = _catalogue(h, "people_per_session", frozen, "min")
    draft = h.shift(h.workspace("Strictest cover draft"), people=h.seed["people"][:people])["workspace"]
    issued = h.publish(h.shift(h.workspace("Issued cover"), people=h.seed["people"][:2])["workspace"])
    before = h.get("coordinator", "/api/publications/" + issued["publication_id"])["snapshot"]
    _catalogue_revision(h, "people_per_session", current,
                        {**line, "numbers": {"min": {"value": live, "level": "mandatory"}}})
    assert _has(_findings(h, draft), code) is expected
    fresh = h.shift(h.workspace("Fresh cover draft"), people=h.seed["people"][:people])["workspace"]
    assert _has(_findings(h, fresh), code) is (live > people)
    assert h.get("coordinator", "/api/publications/" + issued["publication_id"])["snapshot"] == before


@pytest.mark.parametrize("frozen,live,gap,expected", [(12, 10, 11, True), (10, 12, 11, True)],
                         ids=["relaxation", "tightening"])
def test_live_strictest_catalogue_duty_spacing(database, frozen, live, gap, expected):
    h = database
    current, line = _catalogue(h, "duty_spacing", frozen, "min")
    draft = _rest_rota(h, h.seed["people"][0], gap)
    issued = h.publish(_rest_rota(h, h.seed["people"][1], 13))
    before = h.get("coordinator", "/api/publications/" + issued["publication_id"])["snapshot"]
    _catalogue_revision(h, "duty_spacing", current,
                        {**line, "numbers": {"min": {"value": live, "level": "mandatory"}}})
    assert _has(_findings(h, draft), {"MIN_REST"}) is expected
    fresh = _rest_rota(h, h.seed["people"][0], gap)
    assert _has(_findings(h, fresh), {"MIN_REST"}) is (live > gap)
    assert h.get("coordinator", "/api/publications/" + issued["publication_id"])["snapshot"] == before


def _supervised_rota(h, trainees, day="2026-10-04"):
    # The harness seeds people[0] as a trainee; use the qualified second
    # person as supervisor and keep the two roles disjoint.
    supervisor = h.seed["people"][1]
    trainee_pool = [h.seed["people"][0], *h.seed["people"][2:]]
    trainee_ids = trainee_pool[:trainees]
    ready = getattr(h, "_strictest_trainees", set())
    for trainee in trainee_ids:
        if trainee not in ready:
            h.command("coordinator", "membership.create", {
                "person_id": trainee, "category_id": h.seed["trainee_category"], "starts_on": "2020-01-01"})
            ready.add(trainee)
    h._strictest_trainees = ready
    catalogue = h.get("credentialer", "/api/catalogues")
    eligible = getattr(h, "_strictest_eligible", set())
    eligible.update((row["person_id"], row["service_id"])
                    for row in catalogue["eligibilities"])
    for person in {supervisor, *trainee_ids}:
        key = (person, h.seed["services"][0])
        if key not in eligible:
            h.command("credentialer", "eligibility.create", {
                "person_id": person, "service_id": h.seed["services"][0], "starts_on": "2020-01-01"})
            eligible.add(key)
    h._strictest_eligible = eligible
    grants = {(row["person_id"], row["capability_id"]) for row in catalogue["grants"]}
    if (supervisor, h.seed["supervisor_capability"]) not in grants:
        h.command("credentialer", "grant.create", {
            "person_id": supervisor, "capability_id": h.seed["supervisor_capability"],
            "starts_on": "2020-01-01", "evidence": "Strictest supervision fixture"})
    typ = getattr(h, "_strictest_supervision_type", None)
    if typ is None:
        typ = h.configure("assignment_types", {"name": "Strictest supervision", "starts_on": "2020-01-01",
            "definition": {"requirements": [], "supervision": {
                "trainee_category_ids": [h.seed["trainee_category"]],
                "capability_id": h.seed["supervisor_capability"], "trainees_per_supervisor": 2,
                "require_pairing": True}}})["record"]["id"]
        h._strictest_supervision_type = typ
    start = f"{day}T08:00:00+03:00"
    end = f"{day}T12:00:00+03:00"
    people = [{"person_id": supervisor, "start": start,
               "end": end, "participation": "supervisor"}]
    for person in trainee_ids:
        people.append({"person_id": person, "start": start,
                       "end": end, "participation": "trainee",
                       "supervisor_person_id": supervisor})
    return h.shift(h.workspace("Strictest supervision draft"), typ=typ, people=people,
                   start=start, end=end)["workspace"]


# For supervision capacity, a lower maximum is stricter; a higher maximum is a relaxation.
@pytest.mark.parametrize("frozen,live,trainees,expected", [(1, 2, 2, True), (2, 1, 2, True)],
                         ids=["relaxation", "tightening"])
def test_live_strictest_catalogue_supervision(database, frozen, live, trainees, expected):
    h = database
    current, line = _catalogue(h, "supervision", frozen, "max")
    draft = _supervised_rota(h, trainees, "2026-10-04")
    issued = h.publish(_supervised_rota(h, 1, "2026-10-06"))
    before = h.get("coordinator", "/api/publications/" + issued["publication_id"])["snapshot"]
    _catalogue_revision(h, "supervision", current,
                        {**line, "numbers": {"max": {"value": live, "level": "mandatory"}}})
    assert _has(_findings(h, draft), {"SUPERVISOR_CAPACITY", "SUPERVISOR_SCOPED_CAPACITY", "SUPERVISION_GAP"}) is expected
    fresh = _supervised_rota(h, trainees, "2026-10-08")
    fresh_findings = _findings(h, fresh)
    assert _has(fresh_findings, {"SUPERVISOR_CAPACITY", "SUPERVISOR_SCOPED_CAPACITY", "SUPERVISION_GAP"}) is (live < trainees)
    assert h.get("coordinator", "/api/publications/" + issued["publication_id"])["snapshot"] == before


def _numeric_policy(kind, field, value):
    return {"id": "same-policy", "revision": 1, "status": "active", "active": True,
            "definition": {"rule": {"kind": kind, field: value}}}


@pytest.mark.parametrize("kind,field,frozen,live,expected", [
    ("min_rest", "hours", 12, 10, 12),
    ("min_rest", "hours", 10, 12, 12),
    ("minimum_cover", "minimum", 2, 1, 2),
    ("minimum_cover", "minimum", 2, 3, 3),
    ("maximum_participants", "maximum", 3, 5, 3),
    ("maximum_participants", "maximum", 5, 3, 3),
    ("supervision_limit", "maximum", 3, 5, 3),
    ("supervision_limit", "maximum", 5, 3, 3),
], ids=["rest-relax", "rest-tighten", "cover-relax", "cover-tighten",
        "participants-relax", "participants-tighten", "supervision-relax", "supervision-tighten"])
def test_reader_strictest_numeric_safety_revision(kind, field, frozen, live, expected):
    """The private reader merge keeps the stricter value for each numeric safety kind."""
    [merged] = _strictest_policy(_numeric_policy(kind, field, frozen),
                                 _numeric_policy(kind, field, live), kind)
    assert merged["definition"]["rule"][field] == expected


def _catalogue_policy(catalogue_id, key, value, revision=1):
    return {"id": "same-policy", "revision": revision, "status": "active", "active": True,
            "definition": {"rule": {"kind": "catalogue", "id": catalogue_id,
                                      "catalogue_version": 2,
                                      "lines": [{"id": "line", "numbers": {
                                          key: {"value": value, "level": "mandatory"}}}]}}}


@pytest.mark.parametrize("catalogue_id,key,frozen,live,expected", [
    ("people_per_session", "min", 2, 1, 2),
    ("people_per_session", "min", 2, 3, 3),
    ("duty_spacing", "min", 12, 10, 12),
    ("duty_spacing", "min", 10, 12, 12),
    ("supervision", "max", 1, 2, 1),
    ("supervision", "max", 2, 1, 1),
], ids=["cover-relax", "cover-tighten", "rest-relax", "rest-tighten",
        "supervision-relax", "supervision-tighten"])
def test_reader_strictest_catalogue_safety_slot(catalogue_id, key, frozen, live, expected):
    """The private merge keeps catalogue safety minima upward and maxima downward."""
    [merged] = _strictest_policy(_catalogue_policy(catalogue_id, key, frozen),
                                 _catalogue_policy(catalogue_id, key, live), "catalogue")
    assert merged["definition"]["rule"]["lines"][0]["numbers"][key]["value"] == expected


@pytest.mark.parametrize("catalogue_id,key,frozen,live,expected", [
    ("people_per_session", "min", 2, 1, 2),
    ("people_per_session", "min", 2, 3, 3),
    ("duty_spacing", "min", 12, 10, 12),
    ("duty_spacing", "min", 10, 12, 12),
    ("supervision", "max", 1, 2, 1),
    ("supervision", "max", 2, 1, 1),
], ids=["cover-relax", "cover-tighten", "rest-relax", "rest-tighten",
        "supervision-relax", "supervision-tighten"])
def test_reader_public_catalogue_safety_slot(database, catalogue_id, key, frozen, live, expected):
    """The public draft overlay exposes catalogue safety with minima upward and maxima downward."""
    h = database
    draft = h.workspace("Public catalogue strictest reader")
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, draft["id"])
        frozen_inputs = verify(latest(db, workspace.id))
        frozen_inputs = {**frozen_inputs,
                         "policies": [_catalogue_policy(catalogue_id, key, frozen, 1)]}
        current_inputs = {**deepcopy(frozen_inputs),
                          "policies": [_catalogue_policy(catalogue_id, key, live, 2)]}
        projected = live_safety_inputs(db, workspace, frozen_inputs, current=current_inputs)
    assert projected["policies"][0]["definition"]["rule"]["lines"][0]["numbers"][key]["value"] == expected


def test_mandatory_overlay_preserves_a_frozen_catalogue_restriction(h):
    from packages.contracts.schemas import safety_rule

    draft = h.workspace("Frozen catalogue restriction")
    frozen = _catalogue_policy("workload_limit", "apply", None)
    frozen["definition"]["rule"]["lines"][0]["settings"] = {"restriction": {}}
    live = _catalogue_policy("workload_limit", "max", 2, 2)
    live["definition"]["severity"] = "mandatory"
    live["definition"]["rule"]["lines"][0]["settings"] = {"block_quota": {"metric": "duties"}}
    assert safety_rule(frozen["definition"]) and not safety_rule(live["definition"])
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, draft["id"])
        inputs = verify(latest(db, workspace.id))
        old_inputs = {**inputs, "policies": [frozen]}
        live_inputs = {**inputs, "policies": [live]}
        projected = live_safety_inputs(db, workspace, old_inputs, current=live_inputs, include_mandatory=True)
    restriction, quota = projected["policies"]
    assert restriction == frozen
    assert quota["_source_policy_id"] == frozen["id"] and quota["id"] != frozen["id"]
    assert quota["definition"] == live["definition"]


def test_reader_strictest_team_lead_lines():
    frozen = {"id": "same-policy", "definition": {"rule": {"kind": "catalogue", "id": "team_leads",
        "lines": [{"id": "line", "numbers": {"min": {"value": 2}, "max": {"value": 3}}}]}}}
    live = {"id": "same-policy", "definition": {"rule": {"kind": "catalogue", "id": "team_leads",
        "lines": [{"id": "line", "numbers": {"min": {"value": 1}, "max": {"value": 5}}}]}}}
    [merged] = _strictest_policy(frozen, live, "team_lead_requirement")
    numbers = merged["definition"]["rule"]["lines"][0]["numbers"]
    assert numbers["min"]["value"] == 2 and numbers["max"]["value"] == 3

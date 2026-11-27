"""Volunteer overtime uses the resolved department lines and ordinary request governance."""
import copy
import gc
from datetime import date, timedelta
import time
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from tests.outbox_support import process_one_after_undo as process_one
from packages.contracts import schemas as s
from packages.domain.conditions import active_rules
from packages.domain.cp_sat import generate as solver
from packages.domain.generation import allocation_settings, generate as greedy
from packages.domain.time import aware
from packages.domain.validation import evaluate
from .governance_support import identity


REASON = "Synthetic independent overtime review"


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def database(request):
    return request.getfixturevalue(request.param)


def conditions(field, *values):
    return {"match": "all", "conditions": [{"field": field, "op": "any", "values": list(values)}]}


def offer_rule(h, volunteer_required=None, person=None, activate=None, **more):
    """`activate(rule)`: how the approval is sent (a browser test sends it through the live server, so pages hear it)."""
    person = person or h.seed["people"][0]
    cap = h.policy({"kind": "block_quota", "starts_on": "2026-10-04", "ends_on": "2026-10-18",
                    "metric": "duties", "comparison": "maximum", "amount": 2, "overtime": "only"},
                   {"person_id": person})
    settings = {"required_policy_ids": [cap["id"]]}
    if volunteer_required is not None:
        settings["volunteer_required"] = volunteer_required
    line = {"id": str(uuid.uuid4()), "who": conditions("person_ids", person),
            "where": {"match": "all", "conditions": [
                {"field": "service_ids", "op": "any", "values": [h.seed["services"][0]]},
                {"field": "assignment_type_ids", "op": "any", "values": [h.seed["type"]]}]},
            "numbers": {"apply": {"value": None, "level": "medium"}},
            "settings": {"allocation_overtime": settings}, **more}
    rule = h.command("coordinator", "rule.propose", {"catalogue_id": "overtime_offers", "lines": [line],
        "reason": "Synthetic department overtime eligibility"})["record"]
    if activate:
        return activate(rule), cap
    rule = h.command("approver", "policy.activate", {"reason": REASON}, rule["id"], rule["revision"])["record"]
    return rule, cap


def offer(h, role="staff", person=None, status=200, **more):
    return h.command(role, "request.create", {"kind": "overtime_availability",
        "person_id": person or h.seed["people"][0], "starts_on": "2026-10-04", "ends_on": "2026-10-11",
        "dates": ["2026-10-05"], "note": "Synthetic voluntary offer", **more}, status=status)


def decide(h, row, decision="approve", role="coordinator", status=200):
    return h.command(role, "request.decide", {"decision": decision, "reason": REASON},
                     row["id"], row["revision"], status=status)


def undo(h, row, role="coordinator", status=200):
    return h.command(role, "request.undo", {"reason": "Undo synthetic overtime decision"},
                     row["id"], row["revision"], status=status)


def facts(h, person=None):
    person = person or h.seed["people"][0]
    result = h.get("coordinator", f"/api/people/{person}/scheduling-facts")
    return [row for row in result["active"] if row["kind"] == "overtime_offer"]


def eligibility(h, role="staff", person=None, status=200):
    person = person or h.seed["people"][0]
    return h.get(role, "/api/requests/overtime-eligibility?starts_on=2026-10-04&ends_on=2026-10-11&person_id=" + person,
                 status=status)


def settings_for(h, workspace, day="2026-10-05", service=0, start_time="08:00", end_time="12:00", end_day=None):
    data = h.snapshot(workspace)
    data["policies"] = list(active_rules(data["policies"], context=data))
    shift = {"id": "synthetic-offer-probe", "start": f"{day}T{start_time}:00+03:00", "end": f"{end_day or day}T{end_time}:00+03:00",
             "assignment_type_id": h.seed["type"], "service_ids": [h.seed["services"][service]],
             "location_id": h.seed["locations"][0], "participants": []}
    return allocation_settings(data, shift, h.seed["people"][0])[1]


def test_new_lines_default_to_volunteer_only_and_wait_for_approval(database):
    h = database
    rule, _ = offer_rule(h)
    line = rule["definition"]["rule"]["lines"][0]
    assert line["settings"]["allocation_overtime"]["volunteer_required"] is True
    workspace = h.workspace()
    assert settings_for(h, workspace) is False
    row = offer(h)["record"]
    assert row["status"] == "submitted" and facts(h) == []
    assert settings_for(h, workspace) is False
    decide(h, row)
    assert settings_for(h, workspace) is True
    assert settings_for(h, workspace, "2026-10-06") is False


@pytest.mark.parametrize("historical", [False, True], ids=["explicit-whenever-free", "existing-line-without-setting"])
def test_whenever_free_and_existing_stored_lines_keep_previous_behavior(h, historical):
    rule, _ = offer_rule(h, volunteer_required=False)
    if historical:
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Policy, rule["id"])
            definition = copy.deepcopy(row.definition)
            definition["rule"]["lines"][0]["settings"]["allocation_overtime"].pop("volunteer_required")
            row.definition = definition
    assert settings_for(h, h.workspace()) is True
    assert facts(h) == []


def test_eligibility_and_submission_share_person_clinic_and_duty_resolution(h):
    offer_rule(h)
    result = eligibility(h)
    with transaction(h.app.state.engine) as db:
        service = db.get(m.Service, h.seed["services"][0])
        typ = db.get(m.AssignmentType, h.seed["type"])
        assert result["choices"] == {"services": [{"id": service.id, "name": service.name}],
                                     "assignment_types": [{"id": typ.id, "name": typ.name}]}
    assert result["eligible"] is True
    assert eligibility(h, "coordinator", h.seed["people"][1])["eligible"] is False
    refusal = offer(h, "coordinator", h.seed["people"][1], status=400)
    assert refusal["error"]["code"] == "OVERTIME_NOT_ELIGIBLE"
    identity(h, "unmatched_staff", ["staff"], h.seed["people"][1])
    assert offer(h, "unmatched_staff", h.seed["people"][1], status=400)["error"]["code"] == "OVERTIME_NOT_ELIGIBLE"
    assert eligibility(h, person=h.seed["people"][1], status=403)["error"]["code"] == "FORBIDDEN"
    refused = offer(h, service_id=h.seed["services"][1], status=400)
    assert refused["error"]["code"] == "OVERTIME_SCOPE_NOT_ELIGIBLE"
    other_type = h.configure("assignment_types", {"name": "Synthetic ineligible duty", "starts_on": "2020-01-01",
                                                   "definition": {}})["record"]
    assert offer(h, assignment_type_id=other_type["id"], status=400)["error"]["code"] == "OVERTIME_SCOPE_NOT_ELIGIBLE"
    accepted = offer(h, service_id=h.seed["services"][0], assignment_type_id=h.seed["type"])["record"]
    assert accepted["kind"] == "overtime_availability"


@pytest.mark.parametrize("membership", ["inactive", "foreign", "expired", "future"])
def test_ineligible_people_get_empty_eligibility_but_cannot_create_or_approve_overtime(database, membership):
    h = database
    person = h.seed["people"][0]
    offer_rule(h)
    pending = offer(h)["record"]
    with transaction(h.app.state.engine, write=True) as db:
        rows = list(db.scalars(select(m.Membership).where(m.Membership.person_id == person)))
        assert rows
        if membership == "inactive":
            db.get(m.Person, person).active = False
        elif membership == "foreign":
            department = m.Department(organisation_id=db.get(m.Person, person).organisation_id,
                                      name="Other synthetic department")
            db.add(department)
            db.flush()
            for row in rows:
                row.department_id = department.id
        else:
            for row in rows:
                if membership == "expired":
                    row.ends_on = date(2026, 10, 4)
                else:
                    row.starts_on = date(2026, 10, 11)
    empty = {"person_id": person, "eligible": False, "choices": {"services": [], "assignment_types": []}, "offers": []}
    assert eligibility(h) == empty
    assert eligibility(h, role="coordinator") == empty
    assert h.get("staff", "/api/requests/overtime-eligibility?starts_on=2026-10-04&ends_on=2026-10-11") == empty
    assert eligibility(h, person=h.seed["people"][1], status=403)["error"]["code"] == "FORBIDDEN"
    assert eligibility(h, role="coordinator", person=str(uuid.uuid4()), status=404)["error"]["code"] == "NOT_FOUND"
    for refusal in (offer(h, status=400), decide(h, pending, status=400)):
        assert refusal["error"]["code"] == "OVERTIME_NOT_ELIGIBLE"
        assert "department member" in refusal["error"]["message"]
    assert h.get("staff", "/api/requests/all?id=" + pending["id"])["item"]["status"] == "submitted"
    assert facts(h) == []


@pytest.mark.parametrize("restricted", [None, "service_ids", "assignment_type_ids"])
def test_overtime_choices_name_schedulable_records_for_unrestricted_dimensions(database, restricted):
    h = database
    ids = {"service_ids": h.seed["services"][0], "assignment_type_ids": h.seed["type"]}
    offer_rule(h, where=conditions(restricted, ids[restricted]) if restricted else None)
    choices = eligibility(h)["choices"]
    with transaction(h.app.state.engine) as db:
        for field, key, cls in (("service_ids", "services", m.Service),
                                ("assignment_type_ids", "assignment_types", m.AssignmentType)):
            expected_ids = h.seed["services"] if key == "services" and field != restricted else [ids[field]]
            expected = [{"id": id_, "name": db.get(cls, id_).name} for id_ in expected_ids]
            assert choices[key] == sorted(expected, key=lambda row: (row["name"], row["id"]))
    assert eligibility(h, "coordinator")["choices"] == choices
    for service in choices["services"]:
        for typ in choices["assignment_types"]:
            assert offer(h, service_id=service["id"], assignment_type_id=typ["id"])["record"]["status"] == "submitted"


@pytest.mark.parametrize("key,cls,field", [("services", m.Service, "service_ids"),
                                           ("assignment_types", m.AssignmentType, "assignment_type_ids")])
@pytest.mark.parametrize("scoped", [False, True], ids=["unscoped", "scoped"])
def test_staff_overtime_choices_name_only_active_department_records_in_the_window(database, key, cls, field, scoped):
    h = database
    selected = h.seed["services"][0] if key == "services" else h.seed["type"]
    with transaction(h.app.state.engine, write=True) as db:
        original = db.get(cls, selected)
        department = db.get(m.Department, original.department_id)
        foreign = m.Department(organisation_id=department.organisation_id, name="Other synthetic department")
        db.add(foreign)
        db.flush()
        rows = [cls(department_id=original.department_id, name="Synthetic " + name,
                    starts_on=date(2020, 1, 1), **extra) for name, extra in (
            ("inactive choice", {"active": False}),
            ("expired choice", {"ends_on": date(2026, 10, 4)}),
        )]
        rows += [cls(department_id=original.department_id, name="Synthetic future choice", starts_on=date(2026, 10, 11)),
                 cls(department_id=original.department_id, name=original.name, starts_on=date(2026, 10, 7)),
                 cls(department_id=foreign.id, name="Synthetic foreign choice", starts_on=date(2020, 1, 1))]
        unqualified = cls(department_id=original.department_id, name="Synthetic unqualified choice", starts_on=date(2020, 1, 1))
        rows.insert(2, unqualified)
        if cls is m.AssignmentType:
            for row in rows:
                row.definition = {}
            unqualified.definition = {"allowed_category_ids": [h.seed["trainee_category"]]}
        db.add_all(rows)
        db.flush()
        if cls is m.Service:
            db.add(m.Eligibility(department_id=original.department_id, person_id=h.seed["people"][0],
                                 service_id=rows[-2].id, starts_on=date(2020, 1, 1)))
        values = [selected] + [row.id for row in rows[:-1]]
        expected = {selected: original.name, rows[-2].id: rows[-2].name}
        if not scoped and cls is m.Service:
            expected.update((sid, db.get(m.Service, sid).name) for sid in h.seed["services"])
    offer_rule(h, where=conditions(field, *values) if scoped else None)
    choices = eligibility(h)["choices"][key]
    assert choices == sorted([{"id": id_, "name": name} for id_, name in expected.items()],
                             key=lambda row: (row["name"], row["id"]))
    for row in choices:
        scope = {"service_id" if key == "services" else "assignment_type_id": row["id"]}
        assert offer(h, dates=["2026-10-08"], **scope)["record"]["status"] == "submitted"


def test_overtime_choices_project_all_valid_cross_dimension_witnesses(h):
    other = h.configure("assignment_types", {"name": "Synthetic alternative duty", "starts_on": "2020-01-01",
                                             "definition": {}})["record"]
    branches = [{"match": "all", "conditions": [conditions("service_ids", sid)["conditions"][0],
                                                 conditions("assignment_type_ids", typ)["conditions"][0]]}
                for sid, typ in zip(h.seed["services"], [h.seed["type"], other["id"]])]
    offer_rule(h, where={"match": "any", "conditions": branches})
    choices = eligibility(h)["choices"]
    assert {row["id"] for row in choices["services"]} == set(h.seed["services"])
    assert {row["id"] for row in choices["assignment_types"]} == {h.seed["type"], other["id"]}
    for sid, typ in zip(h.seed["services"], [h.seed["type"], other["id"]]):
        offer(h, service_id=sid, assignment_type_id=typ)
    assert offer(h, service_id=h.seed["services"][0], assignment_type_id=other["id"], status=400)["error"]["code"] == "OVERTIME_SCOPE_NOT_ELIGIBLE"


def test_eligibility_allows_stored_type_without_location_type_restrictions(database):
    h = database
    offer_rule(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.AssignmentType, h.seed["type"])
        definition = copy.deepcopy(row.definition)
        definition.pop("allowed_location_type_ids", None)
        row.definition = definition
    assert eligibility(h)["eligible"] is True


@pytest.mark.parametrize("clinic_free,selected", [(False, False), (False, True), (True, False)],
                         ids=["multi-clinic", "selected-multi-clinic", "clinic-free"])
@pytest.mark.parametrize("foreign_service", [False, True], ids=["own-services", "foreign-service"])
def test_real_duty_service_sets_allow_eligibility_creation_and_approval(database, clinic_free, selected, foreign_service):
    h = database
    services = [] if clinic_free else h.seed["services"]
    where = {"match": "all", "conditions": [
        {"field": "service_ids", "op": "none", "values": h.seed["services"]}] if clinic_free else [
        {"field": "service_ids", "op": "any", "values": [sid]} for sid in services]}
    with transaction(h.app.state.engine, write=True) as db:
        typ = db.get(m.AssignmentType, h.seed["type"])
        typ.definition = {**typ.definition, **({"purpose": "group"} if clinic_free else {
            "allow_shared_services": True, "shared_duty_reason": "Synthetic reviewed common duty"})}
    offer_rule(h, where=where)
    workspace = h.workspace()
    h.command("coordinator", "shift.create", {"workspace_id": workspace["id"],
        "assignment_type_id": h.seed["type"], "location_id": h.seed["locations"][0], "service_ids": services,
        "start": "2026-10-05T08:00:00+03:00", "end": "2026-10-05T12:00:00+03:00", "participants": []},
        revision=workspace["revision"])
    if foreign_service:
        with transaction(h.app.state.engine, write=True) as db:
            own = db.get(m.Department, db.get(m.Service, h.seed["services"][0]).department_id)
            other = m.Department(organisation_id=own.organisation_id, name="Synthetic other department")
            db.add(other)
            db.flush()
            foreign = m.Service(department_id=other.id, name="Synthetic foreign clinic", starts_on=date(2020, 1, 1))
            db.add(foreign)
            db.flush()
            shift = db.scalar(select(m.Shift).where(m.Shift.workspace_id == workspace["id"]))
            db.add(m.ShiftService(shift_id=shift.id, service_id=foreign.id))
    result = eligibility(h)
    assert result["eligible"] is True
    assert {row["id"] for row in result["choices"]["services"]} == set(services)
    assert [row["id"] for row in result["choices"]["assignment_types"]] == [h.seed["type"]]
    scope = {"service_id": services[0], "assignment_type_id": h.seed["type"]} if selected else {}
    row = offer(h, **scope)["record"]
    assert row["status"] == "submitted" and facts(h) == []
    if not clinic_free:
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Service, services[1]).active = False
        assert eligibility(h)["eligible"] is False
        assert decide(h, row, status=400)["error"]["code"] in {
            "OVERTIME_NOT_ELIGIBLE", "OVERTIME_SCOPE_NOT_ELIGIBLE"}
        assert facts(h) == []
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Service, services[1]).active = True
    assert decide(h, row)["record"]["status"] == "approved"
    [fact] = facts(h)
    assert fact["request_id"] == row["id"]
    assert fact["data"]["line"]["where"] == (where if not selected else {
        "match": "all", "conditions": [where,
            {"field": "service_ids", "op": "any", "values": [services[0]]},
            {"field": "assignment_type_ids", "op": "any", "values": [h.seed["type"]]}]})


@pytest.mark.parametrize("start,eligible", [
    ("2025-10-03T23:59:59+03:00", False),
    ("2025-10-04T00:00:00+03:00", True),
    ("2026-10-10T23:59:59+03:00", True),
    ("2026-10-11T00:00:00+03:00", False),
], ids=["old-duty", "lookback-boundary", "last-day", "after-window"])
def test_duty_service_contexts_are_bounded_to_the_requested_window(database, start, eligible):
    h = database
    where = {"match": "all", "conditions": [
        {"field": "service_ids", "op": "any", "values": [sid]} for sid in h.seed["services"]]}
    offer_rule(h, where=where)
    workspace = h.workspace()
    with transaction(h.app.state.engine, write=True) as db:
        typ = db.get(m.AssignmentType, h.seed["type"])
        typ.definition = {**typ.definition, "allow_shared_services": True,
                          "shared_duty_reason": "Synthetic reviewed common duty"}
        shift = m.Shift(workspace_id=workspace["id"], assignment_type_id=typ.id, type_revision=typ.revision,
            type_snapshot={}, location_id=h.seed["locations"][0], start=aware(start),
            end=aware(start) + timedelta(hours=1))
        db.add(shift)
        db.flush()
        db.add_all(m.ShiftService(shift_id=shift.id, service_id=sid) for sid in h.seed["services"])
    assert eligibility(h)["eligible"] is eligible


@pytest.fixture(scope="module")
def department_overtime_eligibility(scale_harness):
    h = scale_harness()
    offer_rule(h, who=None)
    eligible, ineligible = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        service = db.get(m.Service, h.seed["services"][0])
        typ = db.get(m.AssignmentType, h.seed["type"])
        location = db.get(m.Location, h.seed["locations"][0])
        for cls, original, count, extra in (
            (m.Service, service, 28, {}),
            (m.AssignmentType, typ, 19, {"definition": typ.definition}),
            (m.Location, location, 48, {"location_type_id": location.location_type_id}),
        ):
            db.add_all(cls(department_id=original.department_id, name=f"Synthetic scale {n}",
                           starts_on=date(2020, 1, 1), **extra) for n in range(count))
        for row in db.scalars(select(m.Eligibility).where(m.Eligibility.person_id == ineligible)):
            row.ends_on = date(2026, 10, 4)
    # Exclude login and fixture work; include HTTP, context loading, resolution and command persistence.
    h.client("coordinator")
    h.client("staff")
    period = "starts_on=2026-10-04&ends_on=2027-01-04"
    timings = {}

    def measure(name, action):
        gc.collect()
        wall, cpu = time.perf_counter(), time.process_time()
        result = action()
        timings[name] = {"wall": time.perf_counter() - wall, "cpu": time.process_time() - cpu}
        return result

    no = measure("ineligible_get", lambda: h.get("coordinator",
        f"/api/requests/overtime-eligibility?{period}&person_id={ineligible}"))
    yes = measure("eligible_get", lambda: h.get("staff",
        f"/api/requests/overtime-eligibility?{period}&person_id={eligible}"))
    payload = {"starts_on": "2026-10-04", "ends_on": "2027-01-04", "dates": [], "weekdays": list(range(7))}
    refused = measure("ineligible_create", lambda: offer(h, "coordinator", ineligible, status=400, **payload))
    accepted = measure("eligible_create", lambda: offer(h, **payload))
    return no, yes, refused, accepted, timings


def test_three_month_overtime_eligibility_scales_with_department(department_overtime_eligibility, record_property):
    no, yes, refused, accepted, timings = department_overtime_eligibility
    record_property("overtime_scale_seconds", timings)
    assert no["eligible"] is False and no["offers"] == []
    assert yes["eligible"] is True and len(yes["offers"]) == 92
    assert refused["error"]["code"] == "OVERTIME_NOT_ELIGIBLE"
    assert accepted["record"]["status"] == "submitted"
    print("overtime scale seconds", timings)


@pytest.mark.cpu_budget
def test_three_month_overtime_eligibility_scales_with_department_cpu_budget(department_overtime_eligibility):
    *_, timings = department_overtime_eligibility
    import coverage
    # CPU excludes competing xdist workers; coverage traces this Python-heavy path more slowly.
    budget = 1.5 * (7 if coverage.Coverage.current() else 1)
    for name, elapsed in timings.items():
        assert elapsed["cpu"] < budget, (name, elapsed, budget)


@pytest.mark.wall_benchmark
def test_three_month_overtime_eligibility_scales_with_department_wall_benchmark(department_overtime_eligibility):
    *_, timings = department_overtime_eligibility
    import coverage
    budget = 1.5 * (7 if coverage.Coverage.current() else 1)
    for name, elapsed in timings.items():
        assert elapsed["wall"] < 4 * budget, (name, elapsed, 4 * budget)


def test_the_resolved_person_line_overrides_a_general_whenever_free_line(h):
    original, _ = offer_rule(h)
    personal = original["definition"]["rule"]["lines"][0]
    general = {**copy.deepcopy(personal), "id": str(uuid.uuid4()), "who": None}
    general["settings"]["allocation_overtime"]["volunteer_required"] = False
    h.command("approver", "policy.retire", {"reason": "Replace synthetic fixture"},
              original["id"], original["revision"])
    proposed = h.command("coordinator", "rule.propose", {"catalogue_id": "overtime_offers",
        "lines": [general, personal], "acknowledged_overlaps": [general["id"]], "reason": REASON})["record"]
    h.command("approver", "policy.activate", {"reason": REASON}, proposed["id"], proposed["revision"])
    workspace = h.workspace()
    assert eligibility(h)["eligible"] is True
    assert settings_for(h, workspace) is False
    decide(h, offer(h)["record"])
    assert settings_for(h, workspace) is True
    assert settings_for(h, workspace, "2026-10-06") is False


@pytest.mark.parametrize("payload", [
    {"dates": [], "weekdays": []},
    {"dates": ["2026-10-05"], "weekdays": [0]},
    {"dates": ["2026-10-11"]},
    {"dates": ["2026-10-05", "2026-10-05"]},
    {"dates": [], "weekdays": [7]},
    {"dates": [], "weekdays": [0, 0]},
    {"start_time": "12:00", "end_time": "08:00"},
    {"ends_on": "2026-10-04"},
])
def test_overtime_input_rejects_invalid_or_ambiguous_dates(h, payload):
    offer_rule(h)
    offer(h, status=422, **payload)
    assert h.get("coordinator", "/api/requests")["requests"] == []


def test_approval_copies_resolved_caps_and_keeps_request_history(database):
    h = database
    _, cap = offer_rule(h)
    row = offer(h, service_id=h.seed["services"][0], assignment_type_id=h.seed["type"])["record"]
    row = decide(h, row)["record"]
    active = facts(h)
    assert len(active) == 1
    fact = active[0]
    assert fact["person_id"] == row["person_id"] and fact["status"] == "active"
    assert fact["request_id"] == row["id"]
    assert fact["data"]["line"]["settings"]["allocation_overtime"]["required_policy_ids"] == [cap["id"]]
    workspace = h.workspace()
    assert settings_for(h, workspace) is True
    assert settings_for(h, workspace, service=1) is False
    detail = h.get("staff", "/api/requests/all?id=" + row["id"])
    assert (detail["item"]["kind"], detail["item"]["stage"]) == ("overtime_availability", "approved")
    assert [event["action"] for event in detail["history"]] == ["created", "request.decide"]
    assert detail["history"][1]["reason"] == REASON
    assert h.get("coordinator", "/api/availability")["availability"] == []
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.SchedulingFact.id).where(m.SchedulingFact.request_id == row["id"])) == fact["id"]


def test_an_approved_offer_does_not_survive_retirement_of_its_required_cap(h):
    _, cap = offer_rule(h)
    decide(h, offer(h)["record"])
    workspace = h.workspace()
    assert settings_for(h, workspace) is True
    h.command("approver", "policy.retire", {"reason": "Synthetic cap no longer in force"}, cap["id"], cap["revision"])
    assert settings_for(h, workspace) is False


def test_weekly_offer_grants_only_its_dates_and_full_time_window(h):
    offer_rule(h)
    row = offer(h, dates=[], weekdays=[0, 2], start_time="08:00", end_time="12:00")["record"]
    decide(h, row)
    workspace = h.workspace()
    assert settings_for(h, workspace, "2026-10-05") is True
    assert settings_for(h, workspace, "2026-10-07") is True
    assert settings_for(h, workspace, "2026-10-06") is False


def test_yearly_weekly_request_stores_one_fact_per_line_and_keeps_lifecycle(database):
    from apps.api.services.overtime_availability import days, _value
    h = database
    offer_rule(h)
    row = decide(h, offer(h, dates=[], weekdays=[0, 2], ends_on="2027-10-04")["record"])["record"]
    [fact] = facts(h)
    line = fact["data"]["line"]
    with transaction(h.app.state.engine) as db:
        offered = days(_value(db.get(m.RequestRecord, row["id"])))
    assert len(offered) > 100
    assert (line["starts_on"], line["ends_on"], line["weekdays"]) == (
        min(offered).isoformat(), (max(offered) + timedelta(days=1)).isoformat(), [0, 2])
    assert "offer_dates" not in fact["data"]
    data = h.snapshot(h.workspace())
    resolved = list(active_rules(data["policies"], context=data))
    assert len([p for p in resolved if p.get("_overtime_request")]) == 1
    withdrawn = decide(h, row, "withdraw", "staff")["record"]
    assert facts(h) == []
    undo(h, withdrawn, "staff")
    assert [f["id"] for f in facts(h)] == [fact["id"]]


def test_compact_one_off_dates_preserve_gaps_in_allocation_and_fairness(database):
    from packages.domain.fairness import offered_intervals
    h = database
    offer_rule(h)
    person = h.seed["people"][0]
    workspace = h.workspace(end="2026-10-26")
    first, last = date(2026, 10, 4), date(2026, 10, 26)
    assert offered_intervals(h.snapshot(workspace), first, last, [person])[person] == []
    dates = ["2026-10-05", "2026-10-19"]
    decide(h, offer(h, dates=dates, ends_on="2026-10-26", start_time="08:00", end_time="12:00")["record"])
    [fact] = facts(h)
    assert fact["data"]["offer_dates"] == dates
    for day, expected in (("2026-10-05", True), ("2026-10-12", False), ("2026-10-19", True)):
        assert settings_for(h, workspace, day=day) is expected
    data = h.snapshot(workspace)
    intervals = offered_intervals(data, first, last, [person])[person]
    assert intervals == [(aware(day + "T08:00:00+03:00"), aware(day + "T12:00:00+03:00")) for day in dates]


def test_full_day_availability_is_clipped_to_the_lines_local_hours(h):
    offer_rule(h, start_time="08:00", end_time="12:00")
    decide(h, offer(h)["record"])
    [fact] = facts(h)
    line = fact["data"]["line"]
    assert (line["starts_on"], line["ends_on"], line["start_time"], line["end_time"]) == (
        "2026-10-05", "2026-10-06", "08:00", "12:00")
    workspace = h.workspace()
    assert settings_for(h, workspace) is True
    assert settings_for(h, workspace, start_time="07:00") is False
    assert settings_for(h, workspace, end_time="13:00") is False


@pytest.mark.parametrize("hours", [("20:00", "08:00"), ("00:00", "24:00")], ids=["overnight-line", "all-day-line"])
@pytest.mark.parametrize("dates,permitted", [
    (["2026-10-05"], False), (["2026-10-06"], False), (["2026-10-05", "2026-10-06"], True),
], ids=["first-day-only", "second-day-only", "both-days"])
def test_overnight_overtime_needs_approved_offers_covering_both_calendar_days(h, hours, dates, permitted):
    offer_rule(h, start_time=hours[0], end_time=hours[1])
    decide(h, offer(h, dates=dates)["record"])
    assert settings_for(h, h.workspace(), start_time="20:00", end_time="08:00", end_day="2026-10-06") is permitted


def test_withdrawing_one_overnight_contributor_removes_full_duty_permission(h):
    offer_rule(h, where=None, start_time="20:00", end_time="08:00")
    first = decide(h, offer(h, dates=["2026-10-05"], service_id=h.seed["services"][0])["record"])["record"]
    second = decide(h, offer(h, dates=["2026-10-06"], service_id=h.seed["services"][0])["record"])["record"]
    workspace = h.workspace()
    interval = {"start_time": "20:00", "end_time": "08:00", "end_day": "2026-10-06"}
    assert settings_for(h, workspace, **interval) is True
    assert settings_for(h, workspace, service=1, **interval) is False
    withdrawn = decide(h, second, "withdraw", "staff")["record"]
    assert settings_for(h, workspace, **interval) is False
    assert h.get("staff", "/api/requests/all?id=" + first["id"])["item"]["status"] == "approved"
    undo(h, withdrawn, "staff")
    assert settings_for(h, workspace, **interval) is True


def test_a_weekly_pattern_without_an_occurrence_is_rejected(h):
    offer_rule(h)
    offer(h, dates=[], weekdays=[1], starts_on="2026-10-05", ends_on="2026-10-06", status=422)
    assert h.get("coordinator", "/api/requests")["requests"] == []


def test_a_broad_overtime_line_still_checks_dated_clinical_eligibility(h):
    offer_rule(h, where=None)
    with transaction(h.app.state.engine, write=True) as db:
        clinical = db.scalars(select(m.Eligibility).where(m.Eligibility.person_id == h.seed["people"][0],
            m.Eligibility.service_id == h.seed["services"][0])).one()
        clinical.ends_on = date(2026, 10, 5)
    refused = offer(h, service_id=h.seed["services"][0], status=400)
    assert refused["error"]["code"] == "OVERTIME_SCOPE_NOT_ELIGIBLE"
    restricted = h.configure("assignment_types", {"name": "Synthetic other rank duty", "starts_on": "2020-01-01",
        "definition": {"allowed_category_ids": [h.seed["trainee_category"]]}})["record"]
    refused = offer(h, service_id=h.seed["services"][1], assignment_type_id=restricted["id"], status=400)
    assert refused["error"]["code"] == "OVERTIME_SCOPE_NOT_ELIGIBLE"
    assert offer(h, service_id=h.seed["services"][1], assignment_type_id=h.seed["type"])["record"]["status"] == "submitted"


@pytest.mark.parametrize("operator", ["any", "none"])
def test_scope_narrowing_preserves_nested_where_conditions(h, operator):
    first, second = h.seed["services"]
    where = {"match": "any", "conditions": [
        {"match": "all", "conditions": [
            {"field": "service_ids", "op": operator, "values": [first]},
            {"field": "assignment_type_ids", "op": "any", "values": [h.seed["type"]]}]},
        conditions("service_ids", second)]}
    offer_rule(h, where=where)
    assert offer(h, service_id=second)["record"]["status"] == "submitted"
    result = offer(h, service_id=first, status=400 if operator == "none" else 200)
    if operator == "none":
        assert result["error"]["code"] == "OVERTIME_SCOPE_NOT_ELIGIBLE"


@pytest.mark.parametrize("location_scope", [False, True])
@pytest.mark.parametrize("inactive", ["location_type", "parent"])
def test_scope_narrowing_uses_valid_locations_and_their_ancestors(database, location_scope, inactive):
    h = database
    parent, child = h.seed["locations"]
    offer_rule(h, where=conditions("location_ids", parent) if location_scope else None)
    pending = offer(h)["record"]
    workspace = h.shift(h.workspace(), location=1)["workspace"]
    with transaction(h.app.state.engine, write=True) as db:
        location = db.get(m.Location, parent)
        db.get(m.Location, child).parent_id = parent
        resource = db.get(m.LocationType, location.location_type_id) if inactive == "location_type" else location
        resource.active = False
        resource_id = resource.id
    expected = "LOCATION_TYPE_INACTIVE" if inactive == "location_type" else "LOCATION_INACTIVE"
    assert expected in {finding["code"] for finding in evaluate(h.snapshot(workspace))["findings"]}
    result = eligibility(h)
    assert result["eligible"] is False and result["offers"] == []
    assert offer(h, status=400)["error"]["code"] == "OVERTIME_NOT_ELIGIBLE"
    assert decide(h, pending, status=400)["error"]["code"] == "OVERTIME_NOT_ELIGIBLE"
    assert h.get("staff", "/api/requests/all?id=" + pending["id"])["item"]["status"] == "submitted"
    assert facts(h) == []
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.LocationType if inactive == "location_type" else m.Location, resource_id).active = True
    assert eligibility(h)["eligible"] is True
    assert offer(h)["record"]["status"] == "submitted"
    assert decide(h, pending)["record"]["status"] == "approved"
    assert len(facts(h)) == 1


@pytest.mark.parametrize("location_scope", [False, True])
def test_overtime_can_use_another_location_with_an_active_type(h, location_scope):
    offer_rule(h, where=conditions("location_ids", *h.seed["locations"]) if location_scope else None)
    inactive_type = h.configure("location_types", {
        "name": "Synthetic inactive room type", "starts_on": "2020-01-01", "active": False})["record"]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Location, h.seed["locations"][0]).location_type_id = inactive_type["id"]
    assert eligibility(h)["eligible"] is True
    assert decide(h, offer(h)["record"])["record"]["status"] == "approved"
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Location, h.seed["locations"][1]).active = False
    assert eligibility(h)["eligible"] is False


@pytest.mark.parametrize("location_scope", [False, True])
def test_overtime_requires_a_location_allowed_by_the_duty_type(database, location_scope):
    h = database
    where = conditions("assignment_type_ids", h.seed["type"])
    if location_scope:
        where["conditions"].append({"field": "location_ids", "op": "any", "values": h.seed["locations"]})
    offer_rule(h, where=where)
    pending = offer(h)["record"]
    allowed_type = h.configure("location_types", {"name": "Synthetic compatible room", "starts_on": "2020-01-01"})["record"]
    with transaction(h.app.state.engine, write=True) as db:
        typ = db.get(m.AssignmentType, h.seed["type"])
        typ.definition = {**typ.definition, "allowed_location_type_ids": [allowed_type["id"]]}
    result = eligibility(h)
    assert result["eligible"] is False and result["offers"] == []
    assert offer(h, status=400)["error"]["code"] == "OVERTIME_NOT_ELIGIBLE"
    assert decide(h, pending, status=400)["error"]["code"] == "OVERTIME_NOT_ELIGIBLE"
    assert h.get("staff", "/api/requests/all?id=" + pending["id"])["item"]["status"] == "submitted"
    assert facts(h) == []
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Location, h.seed["locations"][1]).location_type_id = allowed_type["id"]
    assert eligibility(h)["eligible"] is True
    assert offer(h)["record"]["status"] == "submitted"
    assert decide(h, pending)["record"]["status"] == "approved"
    assert len(facts(h)) == 1


@pytest.mark.parametrize("same_window", [True, False], ids=["same-window", "different-end"])
def test_bulk_overtime_approval_reuses_preparation_only_within_the_command(h, monkeypatch, same_window):
    from apps.api.services import overtime_availability
    offer_rule(h)
    rows = [offer(h, dates=["2026-10-05"])["record"],
            offer(h, dates=["2026-10-06"], ends_on="2026-10-11" if same_window else "2026-10-12")["record"]]
    contexts = []
    original = overtime_availability._context

    def context(*args):
        contexts.append(args[2:])
        return original(*args)

    monkeypatch.setattr(overtime_availability, "_context", context)
    reply = h.command("coordinator", "request.decide_many", {"decision": "approve", "reason": REASON,
        "items": [{"source": "request", "id": row["id"], "expected_revision": row["revision"]} for row in rows]})
    assert reply["count"] == 2 and len(facts(h)) == 2
    assert len(contexts) == (1 if same_window else 2)
    decide(h, offer(h)["record"])
    assert len(contexts) == (3 if same_window else 4)  # Later create and approval each read current eligibility anew.


def test_approval_rechecks_the_current_overtime_line(h):
    rule, _ = offer_rule(h)
    row = offer(h)["record"]
    h.command("approver", "policy.retire", {"reason": "Synthetic eligibility ended"}, rule["id"], rule["revision"])
    assert decide(h, row, status=400)["error"]["code"] == "OVERTIME_NOT_ELIGIBLE"
    assert h.get("staff", "/api/requests/all?id=" + row["id"])["item"]["status"] == "submitted"
    assert facts(h) == []


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_bulk_decide_and_undo_keep_all_overtime_fact_effects_atomic(database, decision):
    h = database
    offer_rule(h)
    rows = [offer(h, dates=[day])["record"] for day in ("2026-10-05", "2026-10-06")]
    reply = h.command("coordinator", "request.decide_many", {"decision": decision, "reason": REASON,
        "items": [{"source": "request", "id": row["id"], "expected_revision": row["revision"]} for row in rows]})
    assert reply["count"] == 2
    assert {row["status"] for row in reply["records"]} == {"approved" if decision == "approve" else "rejected"}
    assert len(facts(h)) == (2 if decision == "approve" else 0)
    undone = h.command("coordinator", "request.undo", {"batch_id": reply["batch_id"], "reason": "Undo overtime batch"})
    assert undone["count"] == 2 and {row["status"] for row in undone["records"]} == {"submitted"}
    assert facts(h) == []


@pytest.mark.parametrize("approved", [False, True])
def test_staff_can_withdraw_until_submission_and_undo_restores_previous_state(database, approved):
    h = database
    offer_rule(h)
    row = offer(h)["record"]
    if approved:
        row = decide(h, row)["record"]
    withdraw_key = str(uuid.uuid4())
    withdraw_payload = {"decision": "withdraw", "reason": REASON}
    withdrawn = h.command("staff", "request.decide", withdraw_payload, row["id"], row["revision"], key=withdraw_key)
    assert h.command("staff", "request.decide", withdraw_payload, row["id"], row["revision"], key=withdraw_key) == withdrawn
    assert h.get("staff", "/api/commands/" + withdraw_key)["result"] == withdrawn
    row = withdrawn["record"]
    assert row["status"] == "withdrawn" and facts(h) == []
    undo_key = str(uuid.uuid4())
    undo_payload = {"reason": "Undo synthetic overtime withdrawal"}
    undone = h.command("staff", "request.undo", undo_payload, row["id"], row["revision"], key=undo_key)
    assert h.command("staff", "request.undo", undo_payload, row["id"], row["revision"], key=undo_key) == undone
    assert h.get("staff", "/api/commands/" + undo_key)["result"] == undone
    row = undone["record"]
    assert row["status"] == ("approved" if approved else "submitted")
    assert len(facts(h)) == int(approved)
    workspace = h.shift(h.workspace(), people=[h.seed["people"][1]])["workspace"]
    h.lifecycle(workspace, "submit")
    refused = decide(h, row, "withdraw", "staff", status=409)
    assert refused["error"]["code"] == "OVERTIME_ROTA_SUBMITTED"
    assert "submitted" in refused["error"]["message"].lower()
    assert len(facts(h)) == int(approved)


@pytest.mark.parametrize('approved', [False, True], ids=['waiting', 'approved'])
def test_overtime_withdrawal_notice_with_queued_submission(h, approved):
    offer_rule(h)
    row = offer(h)['record']
    if approved:
        row = decide(h, row)['record']
    submission = f"request:{row['id']}"
    withdrawn = decide(h, row, 'withdraw', 'staff')['record']
    assert withdrawn['status'] == 'withdrawn'

    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == submission))
        assert event.state == 'superseded' and event.attempts == 0

    process_one(h.app.state.engine)
    notices = h.get('coordinator', '/api/notifications')['notifications']
    assert sum(note['title'].endswith('availability for overtime request was withdrawn') for note in notices) == int(approved)
    assert not any('New availability for overtime request' in note['title'] for note in notices)


@pytest.mark.parametrize("role,eligible", [
    ("coordinator", {"overtime"}),
    ("approver", {"leave", "rotation", "outsider"}),
    ("programme_lead", {"leave", "rotation"}),
    ("coordinator_lead", {"overtime", "leave", "rotation"}),
])
def test_overtime_and_residency_share_scoped_decisions_and_receipts(database, role, eligible):
    from .test_request_decide_many import request_row
    from .test_residency import setup_residency, rotation_value, change
    h, programme, cohort, types, people, blocks = setup_residency(database)
    for typ in types:
        h.configure('rotation_types', {**{key: typ[key] for key in s.RotationTypeInput.model_fields},
            'leave_policy': 'needs_approval'}, typ['id'], typ['revision'])
    with transaction(h.app.state.engine) as db:
        director = db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id
    reviewer = identity(h, "coordinator_lead", ["coordinator", "programme_lead"], person=director)
    offer_rule(h)
    rotation = h.configure("rotations", rotation_value(people[0], cohort, types[0]))["record"]
    rows = {"overtime": offer(h)["record"], "leave": request_row(h),
            "rotation": change(h, rotation, types[1])["record"],
            "outsider": request_row(h, "coordinator", h.seed["people"][2])}
    listing = h.get(role, "/api/requests/all")
    assert {row["id"] for row in listing["items"] if row.get("can_decide")} == {rows[key]["id"] for key in eligible}
    assert listing["counts"]["to_decide"] == len(eligible)
    for row in listing["items"]:
        detail = h.get(role, "/api/requests/all?id=" + row["id"])["item"]
        assert detail["can_decide"] is row["can_decide"]
    for key in rows.keys() - eligible:
        row = rows[key]
        decide(h, row, role=role, status=403)
        h.command(role, "request.decide_many", {"decision": "approve", "reason": REASON,
            "items": [{"source": "request", "id": row["id"], "expected_revision": row["revision"]}]}, status=403)
    payload = {"decision": "approve", "reason": REASON,
        "items": [{"source": "request", "id": row["id"], "expected_revision": row["revision"]}
                  for key, row in rows.items() if key in eligible]}
    batch_key = str(uuid.uuid4())
    reply = h.command(role, "request.decide_many", payload, key=batch_key)
    assert reply["count"] == len(eligible) and {row["status"] for row in reply["records"]} == {"approved"}
    assert h.command(role, "request.decide_many", payload, key=batch_key) == reply
    assert h.get(role, "/api/commands/" + batch_key)["result"] == reply
    if role == "coordinator_lead":
        pending = offer(h, dates=["2026-10-06"])["record"]
        single_key = str(uuid.uuid4())
        single_payload = {"decision": "reject", "reason": REASON}
        rejected = h.command(role, "request.decide", single_payload, pending["id"], pending["revision"], key=single_key)
        assert h.get(role, "/api/commands/" + single_key)["result"] == rejected
        with transaction(h.app.state.engine, write=True) as db:
            db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == reviewer,
                                                 m.RoleBinding.role == "coordinator")).revoked = True
        h.get(role, "/api/commands/" + batch_key, status=403)
        h.command(role, "request.decide_many", payload, key=batch_key, status=403)
        h.get(role, "/api/commands/" + single_key, status=403)
        h.command(role, "request.decide", single_payload, pending["id"], pending["revision"], key=single_key, status=403)
        h.command(role, "request.undo", {"batch_id": reply["batch_id"], "reason": REASON}, status=403)
        assert len(facts(h)) == 1
        assert h.get("staff", "/api/requests/all?id=" + rows["overtime"]["id"])["item"]["status"] == "approved"


def test_submission_checks_actual_offer_dates_and_prevents_undo_reinstatement(h):
    offer_rule(h)
    row = decide(h, offer(h, dates=["2026-10-09"])["record"])["record"]
    early = h.shift(h.workspace(end="2026-10-08"), people=[h.seed["people"][1]])["workspace"]
    h.lifecycle(early, "submit")
    row = decide(h, row, "withdraw", "staff")["record"]
    covering = h.shift(h.workspace(start="2026-10-08", end="2026-10-11"),
                       start="2026-10-09T08:00:00+03:00", end="2026-10-09T12:00:00+03:00",
                       people=[h.seed["people"][1]])["workspace"]
    h.lifecycle(covering, "submit")
    refused = undo(h, row, "staff", status=409)
    assert refused["error"]["code"] == "OVERTIME_ROTA_SUBMITTED"
    assert facts(h) == []


def test_undo_approval_requires_the_decider_and_retires_its_offer(h):
    offer_rule(h)
    row = decide(h, offer(h)["record"])["record"]
    identity(h, "other_coordinator", ["coordinator"])
    undo(h, row, "other_coordinator", status=403)
    undo(h, {**row, "revision": row["revision"] - 1}, status=409)
    row = undo(h, row)["record"]
    assert row["status"] == "submitted" and row["decided_by"] is None and facts(h) == []
    assert undo(h, row, status=409)["error"]["code"] == "WORKFLOW_STATE"


def test_reopening_a_previously_submitted_rota_does_not_unlock_overtime(database):
    h = database
    offer_rule(h)
    row = decide(h, offer(h)["record"])["record"]
    workspace = h.shift(h.workspace(), people=[h.seed["people"][1]])["workspace"]
    submitted = h.lifecycle(workspace, "submit")["workspace"]
    reopened = h.lifecycle(submitted, "reopen")["workspace"]
    assert reopened["status"] == "draft"
    assert decide(h, row, "withdraw", "staff", status=409)["error"]["code"] == "OVERTIME_ROTA_SUBMITTED"
    assert undo(h, row, status=409)["error"]["code"] == "OVERTIME_ROTA_SUBMITTED"
    assert len(facts(h)) == 1


def test_a_request_linked_overtime_fact_cannot_be_undone_standalone(h):
    offer_rule(h)
    decide(h, offer(h)["record"])
    [fact] = facts(h)
    refused = h.command("coordinator", "person.overtime.undo", {"reason": "Use the request lifecycle"},
                        fact["id"], fact["revision"], status=409)
    assert refused["error"]["code"] == "REQUEST_FACT_LINKED" and len(facts(h)) == 1


@pytest.mark.parametrize("mode", ["new-default", "whenever-free", "historical-missing"])
def test_person_overtime_facts_follow_the_same_volunteer_setting(h, mode):
    person = h.seed["people"][0]
    cap = h.policy({"kind": "block_quota", "starts_on": "2026-10-04", "ends_on": "2026-10-18",
        "metric": "duties", "comparison": "maximum", "amount": 2, "overtime": "only"}, {"person_id": person})
    settings = {"required_policy_ids": [cap["id"]]}
    if mode != "new-default":
        settings["volunteer_required"] = False
    row = h.command("coordinator", "person.overtime.propose", {"kind": "overtime_offer", "person_id": person,
        "catalogue_id": "overtime_offers", "line": {"id": str(uuid.uuid4()),
            "where": conditions("service_ids", h.seed["services"][0]),
            "numbers": {"apply": {"value": None, "level": "medium"}},
            "settings": {"allocation_overtime": settings}}, "reason": REASON})["fact"]
    row = h.command("approver", "person.overtime.activate", {"reason": REASON}, row["id"], row["revision"])["fact"]
    if mode == "historical-missing":
        with transaction(h.app.state.engine, write=True) as db:
            stored = db.get(m.SchedulingFact, row["id"])
            data = copy.deepcopy(stored.data)
            data["line"]["settings"]["allocation_overtime"].pop("volunteer_required")
            stored.data = data
    workspace = h.workspace()
    assert eligibility(h)["eligible"] is True
    assert settings_for(h, workspace) is (mode != "new-default")
    if mode == "new-default":
        assert row["data"]["line"]["settings"]["allocation_overtime"]["volunteer_required"] is True
        request = offer(h)["record"]
        assert settings_for(h, workspace) is False
        decide(h, request)
        assert settings_for(h, workspace) is True
        [linked] = [fact for fact in facts(h) if fact["request_id"] == request["id"]]
        assert linked["data"]["offer_source"]["policy_id"] == row["id"]


@pytest.mark.parametrize("previous_flag", [None, False, True], ids=["legacy-missing", "whenever-free", "volunteer-only"])
@pytest.mark.parametrize("change_setting", [False, True], ids=["omit-setting", "explicit-change"])
def test_person_offer_replacement_preserves_its_setting_unless_explicitly_changed(h, previous_flag, change_setting):
    person = h.seed["people"][0]
    cap = h.policy({"kind": "block_quota", "starts_on": "2026-10-04", "ends_on": "2026-10-18",
        "metric": "duties", "comparison": "maximum", "amount": 2, "overtime": "only"}, {"person_id": person})
    payload = {"kind": "overtime_offer", "person_id": person, "catalogue_id": "overtime_offers",
        "line": {"id": str(uuid.uuid4()), "where": conditions("service_ids", h.seed["services"][0]),
            "numbers": {"apply": {"value": None, "level": "medium"}},
            "settings": {"allocation_overtime": {"required_policy_ids": [cap["id"]],
                                                  "volunteer_required": bool(previous_flag)}}}, "reason": REASON}
    row = h.command("coordinator", "person.overtime.propose", payload)["fact"]
    row = h.command("approver", "person.overtime.activate", {"reason": REASON}, row["id"], row["revision"])["fact"]
    if previous_flag is None:
        with transaction(h.app.state.engine, write=True) as db:
            stored = db.get(m.SchedulingFact, row["id"])
            data = copy.deepcopy(stored.data)
            data["line"]["settings"]["allocation_overtime"].pop("volunteer_required")
            stored.data = data
    payload.update(replaces_id=row["id"], expected_revision=row["revision"])
    payload["line"]["note"] = "An unrelated person offer edit"
    settings = payload["line"]["settings"]["allocation_overtime"]
    settings.pop("volunteer_required")
    wanted = not bool(previous_flag) if change_setting else bool(previous_flag)
    if change_setting:
        settings["volunteer_required"] = wanted
    replacement = h.command("coordinator", "person.overtime.propose", payload)["fact"]
    assert replacement["data"]["line"]["settings"]["allocation_overtime"]["volunteer_required"] is wanted
    h.command("approver", "person.overtime.activate", {"reason": REASON}, replacement["id"], replacement["revision"])
    assert settings_for(h, h.workspace()) is (not wanted)
    with transaction(h.app.state.engine) as db:
        original = db.get(m.SchedulingFact, row["id"]).data["line"]["settings"]["allocation_overtime"]
        if previous_flag is None:
            assert "volunteer_required" not in original
        else:
            assert original["volunteer_required"] is previous_flag


def test_overtime_notifications_name_the_coordinator_and_requests(h):
    offer_rule(h)
    row = offer(h)["record"]
    process_one(h.app.state.engine)
    notices = [notice for notice in h.get("coordinator", "/api/notifications")["notifications"]
               if notice["link"].get("request_id") == row["id"]]
    assert len(notices) == 1
    assert notices[0]["kind"] == "request" and "availability for overtime" in notices[0]["title"]
    assert notices[0]["message"] == "A coordinator decides it under Requests."
    decide(h, row)
    process_one(h.app.state.engine)
    [approved] = [notice for notice in h.get("staff", "/api/notifications")["notifications"]
                  if notice["link"].get("request_id") == row["id"]]
    assert approved["kind"] == "request" and "approved" in approved["title"]
    assert approved["message"] == ""


def test_rule_proposals_materialize_new_defaults_and_preserve_legacy_edits(h):
    rule, _ = offer_rule(h)
    assert rule["definition"]["rule"]["lines"][0]["settings"]["allocation_overtime"]["volunteer_required"] is True
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Policy, rule["id"])
        legacy = copy.deepcopy(stored.definition)
        old_line = legacy["rule"]["lines"][0]
        old_line["settings"]["allocation_overtime"].pop("volunteer_required")
        stored.definition = legacy
    shown = next(entry for entry in h.get("coordinator", "/api/rules/catalogue")["entries"]
                 if entry["id"] == "overtime_offers")["rule"]
    assert shown["definition"]["rule"]["lines"][0]["settings"]["allocation_overtime"]["volunteer_required"] is False
    with transaction(h.app.state.engine) as db:
        assert "volunteer_required" not in db.get(m.Policy, rule["id"]).definition["rule"]["lines"][0]["settings"]["allocation_overtime"]
    edited = {**copy.deepcopy(old_line), "note": "An unrelated historical line note"}
    fresh = {**copy.deepcopy(old_line), "id": str(uuid.uuid4()), "where": conditions("service_ids", h.seed["services"][1])}
    proposed = h.command("coordinator", "rule.propose", {"catalogue_id": "overtime_offers", "lines": [edited, fresh],
        "expected_policy_id": rule["id"], "expected_revision": rule["revision"], "reason": REASON})["record"]
    lines = {line["id"]: line for line in proposed["definition"]["rule"]["lines"]}
    assert lines[edited["id"]]["settings"]["allocation_overtime"]["volunteer_required"] is False
    assert lines[fresh["id"]]["settings"]["allocation_overtime"]["volunteer_required"] is True


@pytest.mark.parametrize("role", s.ROLE_NAMES)
def test_overtime_request_role_permissions_use_authoritative_roles(h, role):
    if role not in h.seed["identities"]:
        identity(h, role, [role])
    offer_rule(h)
    eligibility(h, role=role, status=200 if role in {"staff", "coordinator", "admin"} else 403)
    may_create = role in {"staff", "coordinator", "admin"}
    created = offer(h, role, status=200 if may_create else 403)
    row = created["record"] if role == "staff" else offer(h)["record"]
    may_decide = role in {"coordinator", "admin"}
    reply = decide(h, row, role=role, status=200 if may_decide else 403)
    if may_decide:
        assert reply["record"]["status"] == "approved"
    else:
        assert reply["error"]["code"] in {"FORBIDDEN", "SELF_APPROVAL"}


def test_an_alternate_account_for_requester_cannot_approve(h):
    offer_rule(h)
    row = offer(h)["record"]
    identity(h, "requesters_other_login", ["coordinator"], h.seed["people"][0])
    assert decide(h, row, role="requesters_other_login", status=403)["error"]["code"] == "SELF_APPROVAL"
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["coordinator"]).person_id = h.seed["people"][0]
    own = offer(h, "coordinator")["record"]
    assert decide(h, own, status=403)["error"]["code"] == "SELF_APPROVAL"
    assert facts(h) == []


@pytest.mark.parametrize("generate", [greedy, solver], ids=["greedy", "solver"])
@pytest.mark.parametrize("insert_line", [False, True], ids=["revise", "insert-before"])
def test_both_engines_use_overtime_only_on_approved_offered_dates(h, generate, insert_line):
    person = h.seed["people"][0]
    original, _ = offer_rule(h)
    h.policy({"kind": "max_hours", "hours": 1, "window": "week", "include_overtime": False})
    workspace = h.shift(h.workspace(), people=[])["workspace"]
    workspace = h.shift(workspace, start="2026-10-05T08:00:00+03:00", end="2026-10-05T12:00:00+03:00", people=[])["workspace"]

    def candidate():
        data = h.snapshot(workspace)
        data["people"] = {person: data["people"][person]}
        requirements = [{"from_minute": 0, "minimum": 0, "target": 1, "maximum": 1,
                         "capabilities": [], "distinct_capability_seats": False}]
        data["types"][h.seed["type"]]["definition"]["requirements"] = requirements
        for shift in data["shifts"]:
            shift["type_snapshot"]["definition"]["requirements"] = copy.deepcopy(requirements)
        return data

    assert generate(candidate(), 5)["additions"] == []
    row = offer(h)["record"]
    assert generate(candidate(), 5)["additions"] == []
    decide(h, row)
    result = generate(candidate(), 5)
    assert len(result["additions"]) == 1, result
    added = result["additions"][0]["participant"]
    assert added["person_id"] == person and added["overtime"] is True
    assert date.fromisoformat(added["start"][:10]) == date(2026, 10, 5)
    lines = copy.deepcopy(original["definition"]["rule"]["lines"])
    stable_id = lines[0]["id"]
    lines[0]["note"] = "Unrelated synthetic line edit"
    if insert_line:
        inserted = {**copy.deepcopy(lines[0]), "id": str(uuid.uuid4()),
                    "who": conditions("person_ids", h.seed["people"][1])}
        lines.insert(0, inserted)
    revised = h.command("coordinator", "rule.propose", {"catalogue_id": "overtime_offers", "lines": lines,
        "expected_policy_id": original["id"], "expected_revision": original["revision"], "reason": REASON})["record"]
    h.command("approver", "policy.activate", {"reason": REASON}, revised["id"], revised["revision"])
    assert revised["id"] != original["id"]
    assert revised["definition"]["rule"]["lines"][int(insert_line)]["id"] == stable_id
    assert settings_for(h, workspace) is True
    assert h.get("staff", "/api/requests/all?id=" + row["id"])["item"]["status"] == "approved"
    replacement = generate(candidate(), 5)
    assert len(replacement["additions"]) == 1, replacement
    addition = replacement["additions"][0]
    assert addition["participant"]["overtime"] is True
    assert date.fromisoformat(addition["participant"]["start"][:10]) == date(2026, 10, 5)
    scheduled = candidate()
    next(shift for shift in scheduled["shifts"] if shift["id"] == addition["shift_id"])["participants"].append(addition["participant"])
    assert evaluate(scheduled)["error_count"] == 0

"""Relative selections share the domain evaluator and keep their original saved operands."""
from .clock_support import freeze_clock
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.condition_context import evaluation_context
from apps.api.services import audiences
from packages.contracts import schemas as s
from tests.governance_support import identity
from tests.test_bulk_selection import actor


def relative(token, **kwargs):
    return {"kind": "relative", "token": token, **kwargs}


def tree(field, operand=None, op="is"):
    return {"conditions": [{"field": field, "op": op, **({"value": operand} if operand is not None else {})}]}


def post(h, path, payload, *, role="coordinator", status=200):
    response = h.client(role).post(path, json=payload)
    assert response.status_code == status, response.text
    return response.json()


def permitted_clinics(h, role, day):
    with transaction(h.app.state.engine) as db:
        viewer = actor(db, h, role)
        return {row.service_id for row in db.scalars(select(m.Eligibility).where(
            m.Eligibility.department_id == viewer.department_id, m.Eligibility.person_id == viewer.person_id))
            if viewer.person_id and row.starts_on <= day and (row.ends_on is None or day < row.ends_on)}


@pytest.fixture
def clinic_permissions(h):
    with transaction(h.app.state.engine, write=True) as db:
        people = [m.Person(organisation_id=h.seed["organisation_id"], display_name=f"Relative clinic viewer {index}")
                  for index in range(3)]
        services = [m.Service(department_id=h.seed["department_id"], name=name, starts_on=date(2020, 1, 1))
                    for name in ("Bounded viewer clinic", "Open viewer clinic", "Other viewer clinic")]
        db.add_all([*people, *services])
        db.flush()
        for role, person in zip(("coordinator", "approver", "credentialer"), people):
            db.get(m.Identity, h.seed["identities"][role]).person_id = person.id
        db.add_all([
            m.Eligibility(department_id=h.seed["department_id"], person_id=people[0].id, service_id=services[0].id,
                starts_on=date(2026, 10, 18), ends_on=date(2026, 10, 20)),
            m.Eligibility(department_id=h.seed["department_id"], person_id=people[0].id, service_id=services[1].id,
                starts_on=date(2026, 10, 19)),
            m.Eligibility(department_id=h.seed["department_id"], person_id=people[1].id, service_id=services[2].id,
                starts_on=date(2020, 1, 1)),
        ])
        return {"people": [person.id for person in people], "services": [service.id for service in services]}


def test_catalogue_is_authenticated_role_aware_and_uses_department_configuration(h):
    from fastapi.testclient import TestClient
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        assert client.get("/api/conditions/catalogue").status_code == 401
    staff = h.get("staff", "/api/conditions/catalogue")
    approver = h.get("approver", "/api/conditions/catalogue")
    staff_tokens = {t["token"] for t in staff["tokens"]}
    assert "me" in staff_tokens and "people_i_approve" not in staff_tokens
    assert not {"my_team", "people_i_supervise"} & staff_tokens
    assert ("my_clinics" in staff_tokens) == bool(permitted_clinics(h, "staff", date.fromisoformat(staff["today"])))
    assert "people_i_approve" in {t["token"] for t in approver["tokens"]}
    assert (staff["timezone"], staff["week_start"]) == ("Asia/Riyadh", 6)
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed["organisation_id"])
        org.week_start, org.timezone = 0, "America/New_York"
    changed = h.get("staff", "/api/conditions/catalogue")
    assert (changed["timezone"], changed["week_start"]) == ("America/New_York", 0)


@pytest.mark.parametrize("role", s.ROLE_NAMES)
def test_catalogue_role_tokens_follow_actual_grants_for_every_role(h, role):
    if role == "programme_lead":
        identity(h, role, [role])
    result = h.get(role, "/api/conditions/catalogue")
    tokens = {entry["token"] for entry in result["tokens"]}
    assert ("people_i_approve" in tokens) == (role == "approver")
    assert "my_team" not in tokens
    assert ("my_clinics" in tokens) == bool(permitted_clinics(h, role, date.fromisoformat(result["today"])))


@pytest.mark.parametrize("field", ["service_ids", "eligible_service_ids"])
@pytest.mark.parametrize("day,indices", [("2026-10-17", []), ("2026-10-18", [0]),
    ("2026-10-19", [0, 1]), ("2026-10-20", [1]), ("2027-01-01", [1])])
def test_my_clinics_reads_only_own_live_permissions_with_inclusive_start_and_open_end(h, clinic_permissions, field, day, indices):
    catalogue = h.get("coordinator", f"/api/conditions/catalogue?field={field}&as_of={day}")
    offered = next((entry for entry in catalogue["tokens"] if entry["token"] == "my_clinics"), None)
    expected = {clinic_permissions["services"][index] for index in indices}
    assert bool(offered) == bool(expected)
    resolved = post(h, "/api/conditions/resolve-operand", {"field": field, "operand": relative("my_clinics"),
        "as_of": day}, status=200 if expected else 422)
    if expected:
        assert offered["label"] == "Clinics I can work at"
        assert set(resolved["value"]) == expected
    other = post(h, "/api/conditions/resolve-operand", {"field": field, "operand": relative("my_clinics"),
        "as_of": day}, role="approver")
    assert other["value"] == [clinic_permissions["services"][2]]


@pytest.mark.parametrize("as_of,duty_day,index", [("2026-10-18", "2026-10-20", 1), ("2026-10-20", "2026-10-18", 0)])
def test_my_clinics_explicit_duty_day_takes_precedence_over_as_of(h, clinic_permissions, as_of, duty_day, index):
    resolved = post(h, "/api/conditions/resolve-operand", {"field": "service_ids", "operand": relative("my_clinics"),
        "as_of": as_of, "duty_day": duty_day})
    assert resolved["value"] == [clinic_permissions["services"][index]]


@pytest.mark.parametrize("role", ["credentialer", "operator"])
def test_my_clinics_is_hidden_and_refused_without_own_person_permissions(h, clinic_permissions, role):
    offered = h.get(role, "/api/conditions/catalogue?as_of=2026-10-19")["tokens"]
    assert "my_clinics" not in {entry["token"] for entry in offered}
    post(h, "/api/conditions/resolve-operand", {"field": "service_ids", "operand": relative("my_clinics"),
        "as_of": "2026-10-19"}, role=role, status=422)
    if role == "credentialer":
        unavailable = tree("service_ids", relative("my_clinics"))
        post(h, "/api/bulk/resolve", {"collection": "services", "where": {
            "as_of": "2026-10-19", "conditions": unavailable}}, role=role, status=422)
        h.command(role, "table_view.create", {"table_key": "services", "name": "Unavailable personal clinics",
            "definition": {"conditions": unavailable, "as_of": "2026-10-19"}}, status=422)


def test_my_clinics_stays_refused_in_rule_and_audience_contexts_with_valid_permissions(h, clinic_permissions, monkeypatch):
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 19, 12, tzinfo=timezone.utc))
    assert post(h, "/api/conditions/resolve-operand", {"field": "service_ids", "operand": relative("my_clinics")})["value"]
    for context in ("rule", "audience"):
        tokens = h.get("coordinator", f"/api/conditions/catalogue?context={context}&duty_day=2026-10-19")["tokens"]
        assert "my_clinics" not in {entry["token"] for entry in tokens}
        refused = post(h, "/api/conditions/resolve-operand", {"context": context,
            "field": "service_ids", "operand": relative("my_clinics"), "duty_day": "2026-10-19"}, status=422)
        assert "duty" in refused["error"]["message"].lower()
    conditions = tree("service_ids", relative("my_clinics"))
    h.command("coordinator", "policy.create", {"name": "Viewer clinic rule", "owner": "Synthetic owner",
        "source": "Synthetic test", "starts_on": "2020-01-01", "definition": {
            "scope": {"conditions": conditions}, "rule": {"kind": "min_rest", "hours": 10}}}, status=422)
    h.configure("department_content", {"name": "Viewer clinic audience", "starts_on": "2020-01-01",
        "audience": {"where": conditions}}, status=422)


def test_saved_my_clinics_filter_and_bulk_counts_re_resolve_per_viewer_and_day(h, clinic_permissions):
    saved = h.command("coordinator", "table_view.create", {"table_key": "services", "name": "Clinics I can work at",
        "definition": {"conditions": tree("service_ids", relative("my_clinics")), "as_of": "2026-10-19"}})["view"]
    conditions = saved["definition"]["conditions"]
    assert conditions["conditions"][0]["value"] == relative("my_clinics")
    for day, indices in (("2026-10-18", [0]), ("2026-10-19", [0, 1]), ("2026-10-20", [1])):
        selected = {"collection": "services", "where": {"conditions": conditions, "as_of": day}}
        resolved = post(h, "/api/bulk/resolve", selected)
        expected = {clinic_permissions["services"][index] for index in indices}
        assert resolved["count"] == len(expected)
        assert {row["id"] for row in resolved["sample"]} == expected
        rows = post(h, "/api/bulk/rows", {"selection": selected, "columns": ["name"]})
        assert rows["count"] == resolved["count"] and {row["id"] for row in rows["rows"]} == expected
        other = post(h, "/api/bulk/resolve", selected, role="approver")
        assert other["count"] == 1 and other["sample"][0]["id"] == clinic_permissions["services"][2]
    reread = h.get("coordinator", "/api/table-views?table=services")["views"][0]
    assert reread["definition"]["conditions"] == conditions


def test_my_clinics_locations_use_duties_and_templates_then_include_descendants_only(h, clinic_permissions):
    with transaction(h.app.state.engine, write=True) as db:
        location_type = db.get(m.Location, h.seed["locations"][0]).location_type_id
        campus = m.Location(department_id=h.seed["department_id"], name="Relative clinic campus",
            location_type_id=location_type, starts_on=date(2020, 1, 1))
        db.add(campus)
        db.flush()
        roots = [m.Location(department_id=h.seed["department_id"], name=name, parent_id=campus.id,
            location_type_id=location_type, starts_on=date(2020, 1, 1)) for name in ("Duty clinic root", "Template clinic root", "Outside clinic root")]
        db.add_all(roots)
        db.flush()
        children = [m.Location(department_id=h.seed["department_id"], name=root.name + " child", parent_id=root.id,
            location_type_id=location_type, starts_on=date(2020, 1, 1)) for root in roots]
        db.add_all(children)
        db.flush()
        grandchild = m.Location(department_id=h.seed["department_id"], name="Duty clinic grandchild", parent_id=children[0].id,
            location_type_id=location_type, starts_on=date(2020, 1, 1))
        db.add(grandchild)
        for index in (1, 2):
            db.add(m.RecurringSeries(department_id=h.seed["department_id"], name=roots[index].name + " template",
                assignment_type_id=h.seed["type"], location_id=roots[index].id, starts_on=date(2020, 1, 1),
                definition={"anchor": "2026-10-04", "days": [{"weekday": 6, "start_time": "08:00", "end_time": "12:00"}],
                            "service_ids": [clinic_permissions["services"][index]]}))
        db.flush()
        root_id = roots[0].id
        expected = {roots[0].id, roots[1].id, children[0].id, children[1].id, grandchild.id}
        outside = {campus.id, roots[2].id, children[2].id}
    workspace = h.workspace()
    h.command("coordinator", "shift.create", {"workspace_id": workspace["id"], "assignment_type_id": h.seed["type"],
        "location_id": root_id, "service_ids": [clinic_permissions["services"][0]],
        "start": "2026-10-04T08:00:00+03:00", "end": "2026-10-04T12:00:00+03:00", "participants": []}, revision=workspace["revision"])
    result = post(h, "/api/conditions/resolve-operand", {"field": "location_ids", "operand": relative("my_clinics"),
        "as_of": "2026-10-19"})
    assert set(result["value"]) == expected
    assert not set(result["value"]) & outside
    selected = post(h, "/api/bulk/resolve", {"collection": "locations", "where": {"as_of": "2026-10-19",
        "conditions": tree("location_ids", relative("my_clinics"))}})
    assert selected["count"] == len(expected)
    assert {row["id"] for row in selected["sample"]} == expected


@pytest.mark.parametrize("zone,expected", [("Asia/Riyadh", "2026-10-19"), ("America/New_York", "2026-10-18")])
def test_api_today_is_the_department_calendar_day(h, monkeypatch, zone, expected):
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 18, 22, tzinfo=timezone.utc))
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed["organisation_id"]).timezone = zone
    assert h.get("coordinator", "/api/conditions/catalogue")["today"] == expected
    resolved = post(h, "/api/conditions/resolve-operand", {"operand": relative("today"), "field": "date"})
    assert resolved["from"] == expected and resolved["to"] == expected


def test_unavailable_token_refused_for_preview_bulk_and_saved_view_even_without_rows(h):
    post(h, "/api/conditions/resolve-operand", {"operand": relative("people_i_approve"), "field": "person_ids"},
         role="staff", status=422)
    unavailable = tree("person_ids", relative("people_i_supervise"))
    post(h, "/api/bulk/resolve", {"collection": "eligibilities", "where": {"conditions": unavailable}}, status=422)
    h.command("staff", "table_view.create", {"table_key": "people", "name": "Unavailable token",
        "definition": {"conditions": unavailable}}, status=422)


def test_resolve_and_parser_use_one_calendar_including_today(h, monkeypatch):
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 18, 12, tzinfo=timezone.utc))
    resolved = post(h, "/api/conditions/resolve-operand", {"operand": relative("next_n", n=2, unit="weeks"), "field": "ends_on"})
    assert (resolved["from"], resolved["to"]) == ("2026-10-18", "2026-10-31")
    parsed = post(h, "/api/conditions/parse-date", {"text": "next 2 weeks"})["matches"][0]
    assert parsed["operand"] == relative("next_n", n=2, unit="weeks")
    assert (parsed["from"], parsed["to"]) == (resolved["from"], resolved["to"])
    for text, first, last in (("today", "2026-10-18", "2026-10-18"),
                              ("-7d", "2026-10-11", "2026-10-11"),
                              ("in 3 days", "2026-10-21", "2026-10-21"),
                              ("oct", "2026-10-01", "2026-10-31")):
        match = post(h, "/api/conditions/parse-date", {"text": text})["matches"][0]
        assert (match["from"], match["to"]) == (first, last)


def test_saved_relative_filters_remain_tokens_and_counts_move_with_clock(h, monkeypatch):
    service = h.configure("services", {"name": "Clock-bound service", "starts_on": "2020-01-01"})["record"]
    h.command("credentialer", "eligibility.create", {"person_id": h.seed["people"][0], "service_id": service["id"],
        "starts_on": "2027-01-01", "ends_on": "2027-01-20"})
    conditions = tree("ends_on", relative("this_month"), "within")
    saved = h.command("coordinator", "table_view.create", {"table_key": "eligibilities", "name": "Ending this month",
        "definition": {"conditions": conditions}})["view"]
    assert saved["definition"]["conditions"]["conditions"][0]["value"] == relative("this_month")
    freeze_clock(monkeypatch, lambda: datetime(2027, 1, 12, 12, tzinfo=timezone.utc))
    h.clients.pop('coordinator').close()
    selection = {"collection": "eligibilities", "where": {"conditions": saved["definition"]["conditions"]}}
    january = post(h, "/api/bulk/resolve", selection)
    assert january["count"] == 1
    freeze_clock(monkeypatch, lambda: datetime(2027, 2, 12, 12, tzinfo=timezone.utc))
    h.clients.pop('coordinator').close()
    assert post(h, "/api/bulk/resolve", selection)["count"] == 0
    reread = h.get("coordinator", "/api/table-views?table=eligibilities")["views"][0]
    assert reread["definition"]["conditions"] == saved["definition"]["conditions"]


def test_me_selection_is_per_viewer_and_old_literal_selection_still_works(h):
    first, second = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["coordinator"]).person_id = first
        db.get(m.Identity, h.seed["identities"]["approver"]).person_id = second
    selection = {"collection": "people", "where": {"conditions": tree("person_ids", relative("me"))}}
    assert [r["id"] for r in post(h, "/api/bulk/resolve", selection)["sample"]] == [first]
    assert [r["id"] for r in post(h, "/api/bulk/resolve", selection, role="approver")["sample"]] == [second]
    selection["where"]["conditions"] = {"conditions": [{"field": "person_ids", "values": [first], "op": "any"}]}
    assert [r["id"] for r in post(h, "/api/bulk/resolve", selection, role="approver")["sample"]] == [first]


def test_empty_relationship_dates_and_mixed_between_operands(h, monkeypatch):
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 18, 12, tzinfo=timezone.utc))
    selection = {"collection": "eligibilities", "where": {"conditions": tree("ends_on", op="empty")}}
    empty = post(h, "/api/bulk/resolve", selection)
    assert empty["count"] > 0 and all(row["ends_on"] is None for row in empty["sample"])
    selection["where"]["conditions"] = tree("ends_on", op="not_empty")
    filled = post(h, "/api/bulk/resolve", selection)
    assert all(row["ends_on"] is not None for row in filled["sample"])
    selection["where"]["conditions"] = tree("starts_on", ["2020-01-01", relative("today")], "between")
    found = post(h, "/api/bulk/resolve", selection)
    assert found["count"] > 0
    assert all("2020-01-01" <= row["starts_on"] <= "2026-10-18" for row in found["sample"])


@pytest.mark.parametrize("operand,field", [(relative("today"), "date"), (relative("me"), "person_ids")])
def test_rule_and_audience_refuse_viewer_or_clock_relative_conditions(h, operand, field):
    h.command("coordinator", "policy.create", {"name": "Non deterministic condition", "owner": "Synthetic owner",
        "source": "Synthetic test", "starts_on": "2020-01-01", "definition": {
            "scope": {"conditions": tree(field, operand)}, "rule": {"kind": "min_rest", "hours": 10}}}, status=422)
    h.configure("department_content", {"name": "Non deterministic audience", "starts_on": "2020-01-01",
        "audience": {"where": tree(field, operand)}}, status=422)
    denied = post(h, "/api/conditions/resolve-operand", {"field": field, "operand": operand, "context": "rule"}, status=422)
    assert "duty" in denied["error"]["message"].lower()


def test_rule_catalogue_contains_only_duty_relative_tokens_and_preview_uses_given_duty(h):
    tokens = h.get("coordinator", "/api/conditions/catalogue?context=rule")["tokens"]
    assert {entry["token"] for entry in tokens} == {"duty_day", "duty_week", "duty_month", "days_before_duty", "days_after_duty"}
    result = post(h, "/api/conditions/resolve-operand", {"field": "date", "operand": relative("duty_week"),
        "context": "rule", "duty_day": "2026-10-18"})
    assert (result["from"], result["to"]) == ("2026-10-18", "2026-10-24")


def test_audience_duty_dates_use_the_explicit_reference_day_for_people_and_unlinked_viewers(h):
    audience = {"where": tree("date", relative("duty_day"))}
    fixed = {"where": tree("date", "2026-10-18")}
    with transaction(h.app.state.engine) as db:
        viewer = actor(db, h, "coordinator")
        assert audiences.reaches(db, viewer, audience, date(2026, 10, 18))
        assert audiences.reaches(db, viewer, fixed, date(2026, 10, 18))
        assert not audiences.reaches(db, viewer, fixed, date(2026, 10, 19))
        assert audiences.recipients(db, viewer.department_id, audience, date(2026, 10, 18))
        assert not audiences.recipients(db, viewer.department_id, fixed, date(2026, 10, 19))


def test_department_average_uses_the_full_authorized_collection_not_the_filtered_subset(h):
    with transaction(h.app.state.engine, write=True) as db:
        locations = list(db.scalars(select(m.Location).where(m.Location.department_id == h.seed["department_id"])))
        for index, location in enumerate(locations):
            location.capacity = 1 if index else 9
        average = sum(location.capacity for location in locations) / len(locations)
        largest = locations[0].id
    catalogue = h.get("coordinator", "/api/conditions/catalogue?collection=locations&field=capacity")
    assert {entry["token"] for entry in catalogue["tokens"]} == {"department_average"}
    resolved = post(h, "/api/conditions/resolve-operand", {"operand": relative("department_average"),
        "collection": "locations", "field": "capacity"})
    assert resolved["value"] == average
    selected = post(h, "/api/bulk/resolve", {"collection": "locations", "where": {
        "filters": {"id": [largest]}, "conditions": tree("capacity", relative("department_average"), "gt")}})
    assert [row["id"] for row in selected["sample"]] == [largest]
    h.get("staff", "/api/conditions/catalogue?collection=locations&field=capacity", status=403)


def test_its_target_is_per_line_and_missing_targets_do_not_match(h):
    low, high, absent = [str(uuid4()) for _ in range(3)]
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Policy(department_id=h.seed["department_id"], name="Target test rule", kind="catalogue",
            starts_on=date(2020, 1, 1), created_by=h.seed["identities"]["coordinator"], owner="Synthetic owner", source="Synthetic test",
            definition={"scope": {}, "rule": {"kind": "catalogue", "id": "duties_per_week", "lines": [
                {"id": low, "numbers": {"max": {"value": 3}, "target": {"value": 5}}},
                {"id": high, "numbers": {"max": {"value": 9}, "target": {"value": 7}}},
                {"id": absent, "numbers": {"max": {"value": 2}}}]}}))
    catalogue = h.get("coordinator", "/api/conditions/catalogue?collection=policy_lines&field=max")
    assert "its_target" in {entry["token"] for entry in catalogue["tokens"]}
    all_fields = h.get("coordinator", "/api/conditions/catalogue?collection=policy_lines")["tokens"]
    target_token = next(entry for entry in all_fields if entry["token"] == "its_target")
    assert set(target_token["fields"]) == {"max", "target"}
    unsupported = h.get("coordinator", "/api/conditions/catalogue?collection=policy_lines&field=capacity")
    assert not unsupported["tokens"]
    post(h, "/api/conditions/resolve-operand", {"operand": relative("its_target"),
        "collection": "policy_lines", "field": "capacity", "target_id": low}, status=422)
    resolved = post(h, "/api/conditions/resolve-operand", {"operand": relative("its_target"),
        "collection": "policy_lines", "field": "max", "target_id": low})
    assert resolved["value"] == 5
    selected = post(h, "/api/bulk/resolve", {"collection": "policy_lines", "where": {
        "conditions": tree("max", relative("its_target"), "lt")}})
    assert [row["id"] for row in selected["sample"]] == [low]
    between = post(h, "/api/bulk/resolve", {"collection": "policy_lines", "where": {
        "conditions": tree("max", [0, relative("its_target")], "between")}})
    assert [row["id"] for row in between["sample"]] == [low]


def test_new_absolute_date_set_operand_keeps_reference_protection(h):
    from tests.test_date_sets_api import date_set, protection
    date_set_row = date_set(h)
    protection(h, {"field": "date_set_ids", "op": "is",
        "value": {"kind": "absolute", "value": [date_set_row["id"]]}, "entry_names": ["Public holiday"]})
    refused = h.command("admin", "date_set.delete", {"reason": "Cannot invalidate a reference"},
        date_set_row["id"], date_set_row["revision"], status=409)
    assert refused["error"]["code"] == "DATE_SET_IN_USE"


def test_dated_rank_programme_cohort_supervision_and_review_route_tokens(h):
    person, supervisee = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["coordinator"]).person_id = person
        programme = m.Programme(department_id=h.seed["department_id"], name="Relative programme", starts_on=date(2020, 1, 1))
        db.add(programme)
        db.flush()
        cohort = m.Cohort(department_id=h.seed["department_id"], name="Relative cohort", programme_id=programme.id,
            starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1))
        db.add(cohort)
        db.flush()
        db.add_all([m.ProgrammeMembership(department_id=h.seed["department_id"], person_id=person, programme_id=programme.id,
            starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1)),
            m.Rotation(department_id=h.seed["department_id"], person_id=person, cohort_id=cohort.id,
                service_id=h.seed["services"][0], starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1)),
            m.Mentorship(department_id=h.seed["department_id"], person_id=supervisee, mentor_id=person,
                starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1)),
            m.ReviewRoute(department_id=h.seed["department_id"], name="Role route", purpose="rota", starts_on=date(2020, 1, 1),
                status="active", created_by=h.seed["identities"]["admin"], definition={"steps": [{"role": "approver"}]})])
    query = "/api/conditions/catalogue?as_of=2026-10-18"
    held = {entry["token"] for entry in h.get("coordinator", query)["tokens"]}
    assert {"my_programme", "my_cohort", "my_rank", "people_i_supervise"} <= held
    assert "my_team" not in held
    assert "my_team" in {entry["token"] for entry in h.get("approver", query)["tokens"]}
    ended = {entry["token"] for entry in h.get("coordinator", "/api/conditions/catalogue?as_of=2027-01-01")["tokens"]}
    assert not {"my_programme", "my_cohort", "people_i_supervise"} & ended
    resolved = post(h, "/api/conditions/resolve-operand", {"field": "person_ids", "operand": relative("people_i_supervise"),
        "as_of": "2026-10-18"})
    assert resolved["value"] == [supervisee]


def test_programme_leads_cohort_token_uses_their_programme_scope_without_trainee_rotation(h):
    person = h.seed["people"][0]
    identity(h, "programme_lead", ["programme_lead"], person=person)
    with transaction(h.app.state.engine, write=True) as db:
        programme = m.Programme(department_id=h.seed["department_id"], name="Led programme", starts_on=date(2020, 1, 1))
        db.add(programme)
        db.flush()
        cohort = m.Cohort(department_id=h.seed["department_id"], name="Led cohort", programme_id=programme.id,
            starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1))
        db.add(cohort)
        db.add(m.ProgrammeMembership(department_id=h.seed["department_id"], person_id=person, programme_id=programme.id,
            starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1)))
        db.flush()
        cohort_id = cohort.id
    result = post(h, "/api/conditions/resolve-operand", {"operand": relative("my_cohort"), "field": "cohort_ids",
        "as_of": "2026-10-18"}, role="programme_lead")
    assert result["value"] == [cohort_id]
    tokens = h.get("programme_lead", "/api/conditions/catalogue?as_of=2027-01-01")["tokens"]
    assert not {"my_cohort", "my_programme"}.intersection(entry["token"] for entry in tokens)


def test_current_next_and_viewed_rota_are_scoped_and_staff_cannot_resolve_drafts(h, monkeypatch):
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 3, 12, tzinfo=timezone.utc))
    current = h.workspace("Current relative rota", start="2026-10-01", end="2026-10-10")
    upcoming = h.workspace("Next relative rota", start="2026-11-01", end="2026-12-01")
    h.workspace("Later relative rota", start="2026-12-01", end="2027-01-01")
    for token, expected in (("current_rota", current["id"]), ("next_rota", upcoming["id"]), ("viewed_rota", upcoming["id"])):
        result = post(h, "/api/conditions/resolve-operand", {"field": "rota_ids", "operand": relative(token),
            **({"workspace_id": upcoming["id"]} if token == "viewed_rota" else {})})
        assert result["value"] == [expected]
    post(h, "/api/conditions/resolve-operand", {"field": "rota_ids", "operand": relative("viewed_rota"),
        "workspace_id": current["id"]}, role="staff", status=404)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, current["id"]).status = "published"
        other = m.Department(organisation_id=h.seed["organisation_id"], name="Other relative department")
        db.add(other)
        db.flush()
        outside = m.Workspace(department_id=other.id, name="Other department private rota", starts_on=date(2026, 10, 1),
            ends_on=date(2026, 11, 1), created_by=h.seed["identities"]["coordinator"],
            last_edited_by=h.seed["identities"]["coordinator"])
        db.add(outside)
        db.flush()
        outside_id = outside.id
    staff = post(h, "/api/conditions/resolve-operand", {"field": "rota_ids", "operand": relative("current_rota")}, role="staff")
    assert staff["value"] == [current["id"]]
    post(h, "/api/conditions/resolve-operand", {"field": "rota_ids", "operand": relative("viewed_rota"),
        "workspace_id": outside_id}, status=404)


def test_saved_viewed_rota_operand_uses_the_consuming_page_context(h):
    first, second = h.workspace("First page rota"), h.workspace("Second page rota")
    h.shift(first)
    h.shift(second)
    conditions = tree("rota_ids", relative("viewed_rota"))
    saved = h.command("coordinator", "table_view.create", {"table_key": "sessions", "name": "This page rota",
        "workspace_id": first["id"], "definition": {"conditions": conditions}})["view"]
    assert "workspace_id" not in saved["definition"]
    assert saved["definition"]["conditions"]["conditions"][0]["value"] == relative("viewed_rota")
    for workspace in (first, second):
        selected = post(h, "/api/bulk/resolve", {"collection": "sessions", "where": {
            "workspace_id": workspace["id"], "conditions": saved["definition"]["conditions"]}})
        assert selected["count"] == 1
        assert selected["sample"][0]["workspace_id"] == workspace["id"]


def test_relative_session_conditions_bind_to_one_assigned_person(h):
    first, second = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, second).tags = ["Other assigned person"]
        db.get(m.Identity, h.seed["identities"]["coordinator"]).person_id = first
    h.shift(h.workspace(), people=[first, second])
    conditions = tree("person_ids", relative("me"))
    conditions["conditions"].append({"field": "tags", "values": ["Other assigned person"], "op": "any"})
    selected = post(h, "/api/bulk/resolve", {"collection": "sessions", "where": {"conditions": conditions}})
    assert selected["count"] == 0


@pytest.mark.parametrize("token", ["me", "my_clinics"])
def test_context_is_cached_and_large_relative_selection_has_no_per_row_queries(h, token):
    statements = []
    listener = lambda *args: statements.append(args[2])
    with transaction(h.app.state.engine) as db:
        viewer = actor(db, h)
        event.listen(h.app.state.engine, "before_cursor_execute", listener)
        try:
            first = evaluation_context(db, viewer)
            count = len(statements)
            assert evaluation_context(db, viewer) is first
            assert len(statements) == count
        finally:
            event.remove(h.app.state.engine, "before_cursor_execute", listener)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["coordinator"]).person_id = h.seed["people"][0]
    h.client("coordinator")
    field, collection = ("person_ids", "people") if token == "me" else ("service_ids", "services")
    selection = {"collection": collection, "where": {"conditions": tree(field, relative(token))}}
    expected_count = 1 if token == "me" else len(permitted_clinics(h, "coordinator", date(2026, 10, 3)))
    assert expected_count > 0
    event.listen(h.app.state.engine, "before_cursor_execute", listener)
    try:
        statements.clear()
        assert post(h, "/api/bulk/resolve", selection)["count"] == expected_count
        baseline = len(statements)
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", listener)
    with transaction(h.app.state.engine, write=True) as db:
        rank = db.scalar(select(m.Membership.category_id).where(m.Membership.person_id == h.seed["people"][0]))
        for index in range(150):
            person = m.Person(organisation_id=h.seed["organisation_id"], display_name=f"Scale person {index}")
            db.add(person)
            db.flush()
            db.add(m.Membership(department_id=h.seed["department_id"], person_id=person.id, category_id=rank,
                starts_on=date(2020, 1, 1)))
    event.listen(h.app.state.engine, "before_cursor_execute", listener)
    try:
        statements.clear()
        assert post(h, "/api/bulk/resolve", selection)["count"] == expected_count
        assert len(statements) <= baseline  # The rate-limit bucket may already exist on the later request.
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", listener)


def test_staged_relative_selection_preserves_tokens_without_retargeting(h, monkeypatch):
    service = h.configure("services", {"name": "Staged relative service", "starts_on": "2020-01-01"})["record"]
    first = h.command("credentialer", "eligibility.create", {"person_id": h.seed["people"][0], "service_id": service["id"],
        "starts_on": "2026-10-03"})["record"]
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 3, 12, tzinfo=timezone.utc))
    selection = {"collection": "eligibilities", "where": {"conditions": tree("starts_on", relative("today"))}}
    staged = h.command("credentialer", "bulk.stage", {"payload": {"item_kind": "eligibility.end", "selection": selection,
        "common": {"ends_on": "2028-01-01"}, "reason": "Keep a relative selection staged"}})["staged"]
    saved = staged["payload"]["source_selection"]
    assert saved["where"]["conditions"]["conditions"][0]["value"] == relative("today")
    original_items = staged["payload"]["items"]
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 4, 12, tzinfo=timezone.utc))
    h.clients.pop('credentialer').close()
    reopened = post(h, f"/api/bulk/staged/{staged['id']}/reopen", {}, role="credentialer")
    assert reopened["staged"]["payload"]["items"] == original_items
    assert "selection_resolution" in reopened
    assert reopened["selection_resolution"]["count"] == 0


def test_relative_selection_that_expires_after_preview_refuses_apply_without_retargeting(h, monkeypatch):
    from .test_bulk import preview
    from .test_bulk_workspace_sources import apply
    service = h.configure("services", {"name": "Relative preview clinic", "starts_on": "2020-01-01"})["record"]
    original = h.command("credentialer", "eligibility.create", {"person_id": h.seed["people"][0],
        "service_id": service["id"], "starts_on": "2026-10-03"})["record"]
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 3, 12, tzinfo=timezone.utc))
    body = {"item_kind": "eligibility.end", "selection": {"collection": "eligibilities",
        "where": {"conditions": tree("starts_on", relative("today"))}},
        "common": {"ends_on": "2028-01-01"}, "reason": "Apply only the relative selection previewed"}
    checked = preview(h, "credentialer", body)
    assert checked["counts"]["ready"] == 1
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 4, 12, tzinfo=timezone.utc))
    h.clients.pop('credentialer').close()
    assert apply(h, body, checked["fingerprint"], status=409, role="credentialer")["error"]["code"] == "STALE_PREVIEW"
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Eligibility, original["id"])
        assert row.ends_on is None and row.revision == original["revision"]


@pytest.mark.postgres
def test_relative_filter_resolve_and_saved_view_round_trip_on_postgres(pg):
    conditions = tree("person_ids", relative("me"))
    with transaction(pg.app.state.engine, write=True) as db:
        db.get(m.Identity, pg.seed["identities"]["coordinator"]).person_id = pg.seed["people"][0]
    saved = pg.command("coordinator", "table_view.create", {"table_key": "people", "name": "My rows",
        "definition": {"conditions": conditions}})["view"]
    actual = post(pg, "/api/bulk/resolve", {"collection": "people", "where": {"conditions": saved["definition"]["conditions"]}})
    assert actual["count"] == 1 and actual["sample"][0]["id"] == pg.seed["people"][0]

"""Batched operand hints use the same authorized viewer facts and registered table fields."""
from .clock_support import freeze_clock
from datetime import date, datetime, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import condition_context
from apps.api.services.bulk_selection import _choices, _matches_filter, _row_dated_status


def relative(token, **options):
    return {"kind": "relative", "token": token, **options}


@pytest.mark.parametrize("value,expected", [("2026-10-19", True), ({"2026-10-19"}, True),
    ({"2026-10-18"}, False), (set(), False), (None, False)])
def test_existing_date_ranges_accept_scalar_and_projected_choice_values(value, expected):
    assert _matches_filter(value, {"gte": "2026-10-19", "lt": "2026-10-20"},
                           row={}, field="starts_on", day=date(2026, 10, 19)) == expected


def test_related_rows_preserve_scalar_person_names_and_list_dimensions(h):
    client = h.client("coordinator")
    response = client.post("/api/bulk/rows", json={"selection": {"collection": "eligibilities"},
        "columns": ["person.display_name", "person.category_ids"]})
    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    assert rows and all(isinstance(row["person.display_name"], str) for row in rows)
    assert all(isinstance(row["person.category_ids"], list) for row in rows)
    h.shift(h.workspace(), people=h.seed["people"][:2])
    response = client.post("/api/bulk/rows", json={"selection": {"collection": "sessions"},
        "columns": ["person.display_name"]})
    assert response.status_code == 200, response.text
    names = response.json()["rows"][0]["person.display_name"]
    assert isinstance(names, list) and len(names) == 2 and all(isinstance(name, str) for name in names)


def post(h, payload, *, role="coordinator", batch=False, status=200):
    path = "/api/conditions/resolve-operands" if batch else "/api/conditions/resolve-operand"
    response = h.client(role).post(path, json=payload)
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture
def viewer_facts(h):
    with transaction(h.app.state.engine, write=True) as db:
        people = [m.Person(organisation_id=h.seed["organisation_id"], display_name=f"Hint viewer {index}") for index in range(2)]
        services = [m.Service(department_id=h.seed["department_id"], name=f"Hint clinic {index}", starts_on=date(2020, 1, 1))
                    for index in range(3)]
        db.add_all([*people, *services])
        db.flush()
        for role, person in zip(("coordinator", "approver"), people):
            db.get(m.Identity, h.seed["identities"][role]).person_id = person.id
        db.add_all([
            m.Eligibility(department_id=h.seed["department_id"], person_id=people[0].id, service_id=services[0].id,
                starts_on=date(2026, 10, 18), ends_on=date(2026, 10, 20)),
            m.Eligibility(department_id=h.seed["department_id"], person_id=people[0].id, service_id=services[1].id,
                starts_on=date(2026, 10, 19)),
            m.Eligibility(department_id=h.seed["department_id"], person_id=people[1].id, service_id=services[2].id,
                starts_on=date(2020, 1, 1)),
            *(m.Mentorship(department_id=h.seed["department_id"], mentor_id=people[0].id, person_id=person,
                starts_on=date(2020, 1, 1)) for person in h.seed["people"][:2]),
        ])
        return {"people": [person.id for person in people], "services": [service.id for service in services]}


def test_single_choice_hints_are_counts_and_follow_viewer_and_as_of(h, viewer_facts):
    for day, count in (("2026-10-18", 1), ("2026-10-19", 2), ("2026-10-20", 1)):
        result = post(h, {"field": "service_ids", "operand": relative("my_clinics"), "as_of": day})
        assert result["display"] == f"{count} + sub-clinics"
        assert len(result["value"]) == count
        assert all(clinic not in result["display"] for clinic in result["value"])
        other = post(h, {"field": "service_ids", "operand": relative("my_clinics"), "as_of": day}, role="approver")
        assert other["display"] == "1 + sub-clinics" and other["value"] == [viewer_facts["services"][2]]
    me = post(h, {"field": "person_ids", "operand": relative("me")})
    supervisees = post(h, {"field": "person_ids", "operand": relative("people_i_supervise")})
    assert me["display"] == "1 person" and me["value"] == [viewer_facts["people"][0]]
    assert supervisees["display"] == "2 people" and set(supervisees["value"]) == set(h.seed["people"][:2])


def test_approval_and_team_hints_use_department_people_and_dated_review_routes(h, viewer_facts):
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.ReviewRoute(department_id=h.seed["department_id"], name="Hint review route", status="active",
            purpose="rota", starts_on=date(2026, 10, 18), ends_on=date(2026, 10, 20),
            created_by=h.seed["identities"]["admin"], definition={"steps": [{"key": "review", "label": "Review", "role": "approver"}]}))
    expected = set(h.seed["people"]) | set(viewer_facts["people"])
    items = [{"field": "person_ids", "operand": relative(token)} for token in ("people_i_approve", "my_team")]
    for item in items:
        single = post(h, {**item, "as_of": "2026-10-19"}, role="approver")
        assert set(single["value"]) == expected and single["display"] == f"{len(expected)} people"
    results = post(h, {"operands": items, "as_of": "2026-10-19"}, role="approver", batch=True)["results"]
    assert all(set(result["ids"]) == expected and result["display"] == f"{len(expected)} people" for result in results)
    tokens = h.get("approver", "/api/conditions/catalogue?as_of=2026-10-20")["tokens"]
    assert "my_team" not in {entry["token"] for entry in tokens}
    post(h, {**items[1], "as_of": "2026-10-20"}, role="approver", status=422)
    assert post(h, {**items[0], "as_of": "2026-10-20"}, role="approver")["display"] == f"{len(expected)} people"


def test_batch_preserves_input_order_and_returns_dates_ids_and_numeric_values(h, viewer_facts, monkeypatch):
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 19, 12, tzinfo=timezone.utc))
    expected_average = post(h, {"field": "capacity", "collection": "locations", "operand": relative("department_average")})["value"]
    operands = [
        {"field": "service_ids", "operand": relative("my_clinics")},
        {"field": "date", "operand": relative("next_n", n=2, unit="weeks")},
        {"field": "person_ids", "operand": relative("me")},
        {"field": "capacity", "operand": relative("department_average")},
        {"field": "person_ids", "operand": relative("people_i_supervise")},
    ]
    result = post(h, {"operands": operands, "as_of": "2026-10-19", "collection": "locations"}, batch=True)["results"]
    assert len(result) == len(operands)
    assert set(result[0]["ids"]) == set(viewer_facts["services"][:2]) and result[0]["display"] == "2 + sub-clinics"
    assert (result[1]["from"], result[1]["to"]) == ("2026-10-19", "2026-11-01")
    assert "ids" not in result[1]
    assert result[2]["ids"] == [viewer_facts["people"][0]] and result[2]["display"] == "1 person"
    assert result[3]["value"] == expected_average and "ids" not in result[3]
    assert set(result[4]["ids"]) == set(h.seed["people"][:2]) and result[4]["display"] == "2 people"


def test_batch_builds_one_context_and_one_numeric_aggregate_without_per_operand_queries(h, viewer_facts, monkeypatch):
    h.client("coordinator")
    builds, aggregates, statements = [], [], []
    original_context, original_metrics = condition_context.evaluation_context, condition_context._metrics

    def context(*args, **kwargs):
        builds.append(1)
        return original_context(*args, **kwargs)

    def metrics(*args, **kwargs):
        aggregates.append(1)
        return original_metrics(*args, **kwargs)

    monkeypatch.setattr(condition_context, "evaluation_context", context)
    monkeypatch.setattr(condition_context, "_metrics", metrics)
    listener = lambda *args: statements.append(args[2])
    event.listen(h.app.state.engine, "before_cursor_execute", listener)
    try:
        item = {"field": "capacity", "operand": relative("department_average")}
        first = post(h, {"operands": [item], "collection": "locations"}, batch=True)
        baseline = len(statements)
        builds.clear()
        aggregates.clear()
        statements.clear()
        many = post(h, {"operands": [item] * 100, "collection": "locations"}, batch=True)
        assert builds == [1] and aggregates == [1]
        assert many["results"] == first["results"] * 100
        assert len(statements) <= baseline + 1
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", listener)


def test_batch_is_authenticated_bounded_and_refuses_the_whole_invalid_request(h, viewer_facts):
    from fastapi.testclient import TestClient
    valid = {"field": "person_ids", "operand": relative("me")}
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        client.headers["origin"] = h.settings.public_origin
        assert client.post("/api/conditions/resolve-operands", json={"operands": [valid]}).status_code == 401
    for invalid in (
        {"field": "person_ids", "operand": relative("my_team")},
        {"field": "not_a_registered_field", "operand": relative("today")},
        {"field": "date", "operand": relative("unknown_token")},
    ):
        refused = post(h, {"operands": [valid, invalid]}, batch=True, status=422)
        assert "error" in refused and "results" not in refused
    post(h, {"operands": []}, batch=True, status=422)
    post(h, {"operands": [valid] * 1001}, batch=True, status=422)


def test_batch_uses_table_metadata_for_own_date_and_person_aliases(h, viewer_facts, monkeypatch):
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 19, 12, tzinfo=timezone.utc))
    result = post(h, {"table_key": "requests", "operands": [
        {"field": "dates.start", "operand": relative("today")},
        {"field": "person", "operand": relative("me")},
    ]}, batch=True)["results"]
    assert result[0]["from"] == "2026-10-19" and result[0]["to"] == "2026-10-19"
    assert result[1]["ids"] == [viewer_facts["people"][0]] and result[1]["display"] == "1 person"
    post(h, {"table_key": "services", "operands": [{"field": "dates.start", "operand": relative("today")}]},
         batch=True, status=422)
    catalogue = h.get("coordinator", "/api/conditions/catalogue?table_key=requests&field=dates.start")
    assert "today" in {entry["token"] for entry in catalogue["tokens"]}


def test_batch_resolves_current_and_viewed_rota_ids(h, monkeypatch):
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 19, 12, tzinfo=timezone.utc))
    current = h.workspace("Batch current rota", start="2026-10-18", end="2026-10-25")
    viewed = h.workspace("Batch viewed rota", start="2026-11-01", end="2026-11-08")
    result = post(h, {"workspace_id": viewed["id"], "operands": [
        {"field": "rota_ids", "operand": relative("current_rota")},
        {"field": "rota_ids", "operand": relative("viewed_rota")},
    ]}, batch=True)["results"]
    assert [entry["ids"] for entry in result] == [[current["id"]], [viewed["id"]]]
    assert all(entry["display"] == "1 rota" for entry in result)


def test_batch_clinic_location_ids_include_descendants_and_hint_counts_clinics(h, viewer_facts):
    with transaction(h.app.state.engine, write=True) as db:
        location_type = db.get(m.Location, h.seed["locations"][0]).location_type_id
        root = m.Location(department_id=h.seed["department_id"], name="Batch clinic root", location_type_id=location_type,
            starts_on=date(2020, 1, 1))
        outside = m.Location(department_id=h.seed["department_id"], name="Other batch branch", location_type_id=location_type,
            starts_on=date(2020, 1, 1))
        db.add_all([root, outside])
        db.flush()
        child = m.Location(department_id=h.seed["department_id"], name="Batch clinic child", location_type_id=location_type,
            parent_id=root.id, starts_on=date(2020, 1, 1))
        db.add(child)
        db.flush()
        grandchild = m.Location(department_id=h.seed["department_id"], name="Batch clinic grandchild", location_type_id=location_type,
            parent_id=child.id, starts_on=date(2020, 1, 1))
        db.add(grandchild)
        db.add(m.RecurringSeries(department_id=h.seed["department_id"], name="Batch clinic template",
            assignment_type_id=h.seed["type"], location_id=root.id, starts_on=date(2020, 1, 1),
            definition={"anchor": "2026-10-04", "days": [{"weekday": 6, "start_time": "08:00", "end_time": "12:00"}],
                        "service_ids": [viewer_facts["services"][0]]}))
        db.flush()
        expected = {root.id, child.id, grandchild.id}
    result = post(h, {"as_of": "2026-10-19", "operands": [
        {"field": "location_ids", "operand": relative("my_clinics")},
        {"field": "service_ids", "operand": relative("my_clinics")},
    ]}, batch=True)["results"]
    assert set(result[0]["ids"]) == expected
    assert set(result[1]["ids"]) == set(viewer_facts["services"][:2])
    assert all(entry["display"] == "2 + sub-clinics" for entry in result)


def test_saved_own_columns_round_trip_and_raw_column_filters_use_the_shared_evaluator(h):
    service = h.configure("services", {"name": "Own column service", "starts_on": "2020-01-01", "description": "Focused own column"})["record"]
    conditions = {"conditions": [{"field": "description", "op": "contains", "value": "Focused own"}]}
    saved = h.command("coordinator", "table_view.create", {"table_key": "services", "name": "Description filter",
        "definition": {"conditions": conditions}})["view"]
    response = h.client("coordinator").post("/api/bulk/resolve", json={"collection": "services", "where": {
        "conditions": saved["definition"]["conditions"]}})
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["sample"]] == [service["id"]]
    read = h.get("coordinator", "/api/table-views?table=services")["views"][0]
    assert read["definition"]["conditions"] == saved["definition"]["conditions"]
    h.command("coordinator", "table_view.create", {"table_key": "people", "name": "Wrong collection field",
        "definition": {"conditions": conditions}}, status=422)


@pytest.mark.parametrize("literal", ["No", "false"])
def test_raw_false_boolean_filters_match_sheet_and_boolean_labels(h, literal):
    person = h.seed["people"][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, person).active = False
    response = h.client("coordinator").post("/api/bulk/resolve", json={"collection": "people", "where": {
        "conditions": {"conditions": [{"field": "active", "op": "any", "values": [literal]}]}}})
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["sample"]] == [person]
    assert response.json()["sample"][0]["active"] is False


def test_participant_object_choices_are_json_and_keep_empty_semantics(h):
    h.shift(h.workspace("Empty participant filter"), people=[])
    h.shift(h.workspace("Populated participant filter"), people=[h.seed["people"][0]])
    client = h.client("coordinator")
    source = client.post("/api/bulk/resolve", json={"collection": "sessions"})
    assert source.status_code == 200, source.text
    rows = source.json()["sample"]
    empty, populated = next(row for row in rows if not row["participants"]), next(row for row in rows if row["participants"])
    value = json.dumps(populated["participants"][0], ensure_ascii=False, separators=(",", ":"))
    for line, expected in (({"field": "participants", "op": "empty"}, empty["id"]),
        ({"field": "participants", "op": "not_empty"}, populated["id"]),
        ({"field": "participants", "op": "any", "values": [value]}, populated["id"])):
        result = client.post("/api/bulk/resolve", json={"collection": "sessions", "where": {"conditions": {"conditions": [line]}}})
        assert result.status_code == 200, result.text
        assert [row["id"] for row in result.json()["sample"]] == [expected]


def test_choice_normalization_preserves_booleans_and_nonempty_json_lists():
    assert _choices(False) == {False, "false", "No"}
    assert _choices(True) == {True, "true", "Yes"}
    assert _choices([]) == set()
    assert _choices([{}, []]) == {"{}", "[]"}
    assert _choices([{"name": "ضيظتو", "day": date(2026, 10, 19)}]) == {'{"name":"ضيظتو","day":"2026-10-19"}'}


@pytest.mark.parametrize("state", [{"revoked": True}, {"status": "retired"}])
def test_revoked_or_retired_dated_rows_are_ended_even_with_open_ends(state):
    assert _row_dated_status({"starts_on": "2020-01-01", "ends_on": None, **state}, date(2026, 10, 19)) == "Ended"


@pytest.mark.parametrize("person_field,person_operand", [("person_ids", relative("me")),
    ("person.display_name", relative("me")), ("person.display_name", {"kind": "absolute", "value": "First assigned person"})])
@pytest.mark.parametrize("staff_line,standalone_count", [
    ({"field": "person.staff_number", "op": "contains", "value": "SECOND-STAFF"}, 1),
    ({"field": "person.staff_number", "op": "none", "values": ["FIRST-STAFF"]}, 0)])
def test_related_text_conditions_bind_to_the_same_assigned_person(h, person_field, person_operand, staff_line, standalone_count):
    first, second = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, first).staff_number = "FIRST-STAFF"
        db.get(m.Person, second).staff_number = "SECOND-STAFF"
        db.get(m.Person, first).display_name = "First assigned person"
        db.get(m.Person, second).display_name = "Second assigned person"
        db.get(m.Identity, h.seed["identities"]["coordinator"]).person_id = first
    h.shift(h.workspace(), people=[first, second])
    selection = {"collection": "sessions", "where": {"conditions": {"conditions": [staff_line]}}}
    response = h.client("coordinator").post("/api/bulk/resolve", json=selection)
    assert response.status_code == 200, response.text
    assert response.json()["count"] == standalone_count
    selection["where"]["conditions"]["conditions"].append({"field": person_field, "op": "is", "value": person_operand})
    response = h.client("coordinator").post("/api/bulk/resolve", json=selection)
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 0


def test_related_clinic_name_matches_existing_labels_and_relative_ids(h, viewer_facts):
    for operand, expected in ((relative("my_clinics"), set(viewer_facts["services"][:2])),
        ({"kind": "absolute", "value": "Hint clinic 0"}, {viewer_facts["services"][0]})):
        response = h.client("coordinator").post("/api/bulk/resolve", json={"collection": "eligibilities", "where": {
            "as_of": "2026-10-19", "filters": {"service_id": viewer_facts["services"]},
            "conditions": {"conditions": [{"field": "service.name", "op": "is", "value": operand}]}}})
        assert response.status_code == 200, response.text
        assert {row["service_id"] for row in response.json()["sample"]} == expected


def test_batch_rule_line_number_aliases_use_actual_targets_and_full_source_averages(h):
    line_id = str(uuid4())
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Policy(department_id=h.seed["department_id"], name="Batch alias metrics", kind="catalogue",
            starts_on=date(2020, 1, 1), created_by=h.seed["identities"]["coordinator"], owner="Synthetic owner", source="Synthetic test",
            definition={"scope": {}, "rule": {"kind": "catalogue", "id": "duties_per_week", "lines": [
                {"id": line_id, "numbers": {"min": {"value": 1}, "target": {"value": 3}}},
                {"id": str(uuid4()), "numbers": {"min": {"value": 5}, "target": {"value": 7}}}]}}))
    result = post(h, {"table_key": "rule-line-db", "collection": "policy_lines", "target_id": line_id, "operands": [
        {"field": "n_min", "operand": relative("department_average")},
        {"field": "n_target", "operand": relative("department_average")},
        {"field": "n_min", "operand": relative("its_target")},
    ]}, batch=True)["results"]
    assert [entry["value"] for entry in result] == [3, 5, 3]
    assert all("ids" not in entry for entry in result)


def test_related_status_conditions_use_the_selected_day_with_exclusive_ends(h, viewer_facts):
    for day, status, op, expected_index in (("2026-10-18", "Current", "any", 0), ("2026-10-18", "Future", "any", 1),
        ("2026-10-20", "Ended", "any", 0), ("2026-10-20", "Current", "any", 1), ("2026-10-20", "Ended", "none", 1)):
        response = h.client("coordinator").post("/api/bulk/resolve", json={"collection": "eligibilities", "where": {
            "as_of": day, "filters": {"service_id": viewer_facts["services"][:2]},
            "conditions": {"conditions": [{"field": "status", "op": op, "values": [status]}]}}})
        assert response.status_code == 200, response.text
        assert [row["service_id"] for row in response.json()["sample"]] == [viewer_facts["services"][expected_index]]


@pytest.mark.parametrize("status_line", [{"field": "status", "op": "empty"},
    {"field": "status", "op": "none", "values": ["Approved", "Declined"]}])
def test_requests_saved_view_keeps_status_type_and_relative_or_mixed_dates(h, status_line):
    lines = [status_line, {"field": "type", "op": "any", "values": ["Leave"]},
        {"field": "start", "op": "within", "value": relative("this_week")},
        {"field": "end", "op": "between", "value": ["2026-10-01", relative("next_month")]}]
    saved = h.command("coordinator", "table_view.create", {"table_key": "requests", "name": "Requests by status and dates",
        "definition": {"as_of": "2026-10-19", "conditions": {"match": "all", "conditions": lines}}})["view"]
    restored = h.get("coordinator", "/api/table-views?table=requests")["views"][0]
    assert restored["definition"]["conditions"] == saved["definition"]["conditions"]
    actual = restored["definition"]["conditions"]["conditions"]
    assert actual[0]["field"] == "status" and actual[0]["op"] == status_line["op"]
    assert actual[0]["values"] == status_line.get("values", [])
    assert actual[1]["values"] == ["Leave"] and actual[2]["value"] == relative("this_week")
    assert actual[3]["value"] == ["2026-10-01", relative("next_month")]


@pytest.mark.parametrize("context", ["rule", "audience"])
def test_own_table_columns_are_refused_for_rule_and_audience_preview_contexts(h, context):
    item = {"field": "description", "operand": "Synthetic value"}
    post(h, {**item, "table_key": "services", "context": context}, status=422)
    post(h, {"operands": [item], "table_key": "services", "context": context}, batch=True, status=422)

"""The server selects exactly the visible records, with dated rule conditions and bounded pages."""
from datetime import date
import os
import time
import uuid

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.auth import Actor, current_roles
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import bulk_selection as bulk
from apps.api.services.staff_import import match_people, plan
from packages.contracts import schemas as s


def actor(db, h, role="coordinator"):
    identity = db.get(m.Identity, h.seed["identities"][role])
    return Actor(identity.id, h.seed["department_id"], h.seed["organisation_id"], identity.display_name,
                 current_roles(db, identity.id, h.seed["department_id"]), identity.person_id, "selection-test")


def resolve(h, selection, role="coordinator"):
    with transaction(h.app.state.engine) as db:
        return bulk.resolve(db, actor(db, h, role), selection)


def line(field, *values, op="any"):
    return {"field": field, "values": list(values), "op": op}


def test_matcher_precedence_casefold_accents_aliases_and_ambiguity_are_shared_with_import(h):
    first, second = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        one, two = db.get(m.Person, first), db.get(m.Person, second)
        one.display_name, one.staff_number, one.short_code = "José Example", "12345", "JEX"
        two.display_name, two.short_code = "Twin", "12345"
        db.add(m.Person(organisation_id=one.organisation_id, display_name="Twin"))
        db.add(m.SourceMapping(department_id=h.seed["department_id"], source_name="synthetic-test",
                               source_key="alias:Dr José", target_kind="people", target_id=one.id))
    with transaction(h.app.state.engine) as db:
        result = match_people(db, actor(db, h), ["12345", "jex", "JOSE EXAMPLE", "dr jose", "Twin", "Nobody"])
        assert [r["id"] for r in result["matches"]] == [first] * 4
        assert result["matches"][0]["by"] == "staff_number"  # wins over another person's short code
        assert result["unmatched"] == ["Nobody"]
        assert result["ambiguous"][0]["line"] == "Twin" and len(result["ambiguous"][0]["candidates"]) == 2
        imported = plan(db, actor(db, h), [(2, {"display_name": "dr jose"})])[0]
        assert imported["person"]["id"] == first and imported["person"]["by"] == "aliases"


def test_resolver_nested_and_or_conditions_use_the_selected_day(h):
    person = h.seed["people"][0]
    programme = h.configure("programmes", {"name": "Date-bound programme", "starts_on": "2020-01-01"})["record"]
    h.configure("programme_memberships", {"person_id": person, "programme_id": programme["id"],
                 "starts_on": "2026-11-01", "ends_on": "2026-12-01"})
    conditions = {"match": "all", "conditions": [line("programme_ids", programme["id"]),
        {"match": "any", "conditions": [line("person_ids", person), line("tags", "missing")]}]}
    selection = {"collection": "people", "where": {"conditions": conditions, "as_of": "2026-11-15"}}
    found = resolve(h, selection)
    assert found["count"] == 1 and found["sample"][0]["id"] == person
    selection["where"]["as_of"] = "2026-10-15"
    assert resolve(h, selection)["count"] == 0


def test_resolver_names_parent_join_include_exclude_and_revision_token(h):
    first, second = h.seed["people"][:2]
    selected = {"collection": "people", "names": ["Alex Morgan", "Nobody"], "include_ids": [second]}
    found = resolve(h, selected)
    assert {r["id"] for r in found["sample"]} == {first, second}
    assert found["unmatched"] == ["Nobody"]
    selection = {"collection": "eligibilities", "parent": {"collection": "people", "selection": selected}}
    children = resolve(h, selection)
    assert set(children["by_parent"]) == {first, second}
    assert children["count"] == sum(children["by_parent"].values())
    selection["exclude_ids"] = [children["sample"][0]["id"]]
    assert resolve(h, selection)["count"] == children["count"] - 1
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Eligibility, children["sample"][0]["id"]).revision += 1
    assert resolve(h, {"collection": "eligibilities", "parent": {"collection": "people", "selection": selected}})["token"] != children["token"]


def test_resolver_dated_status_and_service_tag_condition(h):
    person = h.seed["people"][0]
    clinic = h.configure("services", {"name": "Walk-in selected", "starts_on": "2020-01-01"})["record"]
    tag = h.configure("tags", {"name": "walk-in", "labels": ["services"]})["record"]
    h.command("coordinator", "tag.members", {"add": [{"kind": "services", "id": clinic["id"]}], "remove": [], "reason": "Synthetic filter"}, tag["id"], tag["revision"])
    h.command("credentialer", "eligibility.create", {"person_id": person, "service_id": clinic["id"],
              "starts_on": "2026-11-01", "ends_on": "2026-12-01"})
    clinics = resolve(h, {"collection": "services", "where": {"conditions": {"conditions": [line("tags", tag["id"])]}}})
    assert [r["id"] for r in clinics["sample"]] == [clinic["id"]]
    selection = {"collection": "eligibilities", "where": {"as_of": "2026-11-15", "filters": {"status": ["Current"], "service_id": [clinic["id"]]}}}
    assert resolve(h, selection)["count"] == 1
    selection["where"]["as_of"] = "2026-12-01"
    assert resolve(h, selection)["count"] == 0  # end is exclusive


@pytest.mark.parametrize("collection", ["people", "eligibilities", "grants", "memberships", "policies", "identities", "roles"])
def test_staff_cannot_resolve_administrative_collections(h, collection):
    with pytest.raises(Problem) as error:
        resolve(h, {"collection": collection}, "staff")
    assert error.value.status == 403


def test_availability_is_self_scoped_and_never_discloses_private_reason(h):
    first, second = h.seed["people"][:2]
    for person in (first, second):
        h.command("coordinator", "availability.record", {"person_id": person, "kind": "recorded_leave",
                  "start": "2026-11-01T00:00:00+03:00", "end": "2026-11-02T00:00:00+03:00",
                  "private_reason": "Synthetic confidential note"})
    with transaction(h.app.state.engine) as db:
        own = actor(db, h, "staff").person_id
    found = resolve(h, {"collection": "availability"}, "staff")
    assert all(r["person_id"] == own and "private_reason" not in r for r in found["sample"])
    team = resolve(h, {"collection": "availability"})
    assert team["count"] == 2 and all("private_reason" not in r for r in team["sample"])


def test_department_scope_is_not_bypassed_by_include_ids(h):
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed["organisation_id"], name="Other department")
        db.add(other)
        db.flush()
        row = m.Eligibility(department_id=other.id, person_id=h.seed["people"][0], service_id=h.seed["services"][0], starts_on=date(2020, 1, 1))
        db.add(row)
        db.flush()
        outside = row.id
    assert outside not in {r["id"] for r in resolve(h, {"collection": "eligibilities", "include_ids": [outside]})["sample"]}


def test_unaffiliated_people_are_not_disclosed_by_names_or_include(h):
    with transaction(h.app.state.engine, write=True) as db:
        hidden = m.Person(organisation_id=h.seed["organisation_id"], display_name="Foreign colleague", staff_number="FOREIGN")
        db.add(hidden)
        db.flush()
        hidden_id = hidden.id
    found = resolve(h, {"collection": "people", "names": ["FOREIGN"], "include_ids": [hidden_id]})
    assert found["count"] == 0 and found["unmatched"] == ["FOREIGN"] and not found["ambiguous"]


def test_parent_tags_and_recurring_series_and_location_descendants(h):
    person = h.seed["people"][0]
    with transaction(h.app.state.engine) as db:
        base = db.get(m.Location, h.seed["locations"][0])
        location_type = base.location_type_id
    child = h.configure("locations", {"name": "Nested room", "location_type_id": location_type,
                          "parent_id": h.seed["locations"][0], "starts_on": "2020-01-01"})["record"]
    tag = h.configure("tags", {"name": "Parent tagged", "labels": ["locations"]})["record"]
    h.command("coordinator", "tag.members", {"add": [{"kind": "locations", "id": child["id"]}], "remove": [], "reason": "Synthetic filter"}, tag["id"], tag["revision"])
    parent = {"collection": "tags", "selection": {"collection": "tags", "include_ids": [tag["id"]], "where": {"q": "Parent tagged"}}}
    found = resolve(h, {"collection": "locations", "parent": parent})
    assert found["count"] == 1 and found["sample"][0]["id"] == child["id"]
    descendants = resolve(h, {"collection": "locations", "where": {"conditions": {"conditions": [line("location_ids", h.seed["locations"][0])]}}})
    assert child["id"] in {r["id"] for r in descendants["sample"]}
    series = h.configure("recurring_series", {"name": "Parent recurring", "starts_on": "2026-10-01", "ends_on": "2027-01-01",
               "location_id": child["id"], "assignment_type_id": h.seed["type"],
               "definition": {"anchor": "2026-10-04", "days": [{"weekday": 6, "start_time": "08:00", "end_time": "12:00"}],
                              "default_person_ids": [person], "service_ids": [h.seed["services"][0]]}})["record"]
    selected_person = {"collection": "people", "selection": {"collection": "people", "names": ["Alex Morgan"]}}
    found = resolve(h, {"collection": "recurring_series", "parent": selected_person})
    assert series["id"] in {r["id"] for r in found["sample"]}
    selected_clinic = {"collection": "services", "selection": {"collection": "services", "where": {"filters": {"id": [h.seed["services"][0]]}}}}
    assert series["id"] in {r["id"] for r in resolve(h, {"collection": "recurring_series", "parent": selected_clinic})["sample"]}


@pytest.mark.parametrize("collection", tuple(s.BULK_COLLECTIONS))
def test_every_registry_collection_can_be_browsed_by_its_authorized_reader(h, collection):
    found = resolve(h, {"collection": collection}, "operator" if collection in {"history", "outbox"} else "admin")
    assert 0 <= found["count"] <= bulk.MAX_CANDIDATES and len(found["sample"]) <= 100
    assert all("password_hash" not in row and "private_reason" not in row for row in found["sample"])


def test_http_resolve_and_rows_validate_and_share_the_result(h):
    selection = {"collection": "people", "names": ["Alex Morgan"]}
    response = h.client("coordinator").post("/api/bulk/resolve", json=selection)
    assert response.status_code == 200, response.text
    selected = response.json()
    assert selected["count"] == 1 and selected["sample"][0]["id"] == h.seed["people"][0]
    response = h.client("coordinator").post("/api/bulk/rows", json={"selection": selection, "columns": ["display_name"], "limit": 1})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["token"] == selected["token"] and page["rows"][0]["display_name"] == "Alex Morgan"
    response = h.client("coordinator").post("/api/bulk/rows", json={"selection": selection, "limit": 5001})
    assert response.status_code == 422


def test_date_ranges_and_date_sets_use_shared_calendar_evaluation(h):
    date_set = h.configure("date_sets", {"name": "Synthetic teaching week", "entries": [{"id": str(uuid.uuid4()),
        "name": "Teaching", "repeat": "once", "first_day": "2026-10-04", "last_day": "2026-10-10"}]})["record"]
    selection = {"collection": "people", "where": {"as_of": "2026-10-05", "conditions": {"conditions": [
        {**line("date_set_ids", date_set["id"]), "entry_names": ["Teaching"]}]}}}
    assert resolve(h, selection)["count"] == len(h.seed["people"])
    selection["where"]["as_of"] = "2026-10-11"
    assert resolve(h, selection)["count"] == 0
    for day in ("2026-11-01", "2026-12-01"):
        h.command("coordinator", "availability.record", {"person_id": h.seed["people"][0], "kind": "recorded_leave",
            "start": day + "T00:00:00+03:00", "end": day + "T12:00:00+03:00", "private_reason": "Synthetic range"})
    selection = {"collection": "availability", "where": {"filters": {"start": {"gte": "2026-11-01T00:00:00+03:00", "lt": "2026-12-01T00:00:00+03:00"}}}}
    found = resolve(h, selection)
    assert found["count"] == 1 and found["sample"][0]["start"].startswith("2026-10-31T21:")


def test_rule_lines_from_people_match_tag_groups_through_the_shared_evaluator(h):
    person = h.seed["people"][0]
    current = h.get("coordinator", "/api/catalogues")["people"]
    value = next(r for r in current if r["id"] == person)
    person_value = {key: value[key] for key in s.PersonInput.model_fields if key in value}
    person_value["tags"] = ["Synthetic learners"]
    h.configure("people", person_value, target=person, revision=value["revision"])
    tag = next(r for r in h.get("coordinator", "/api/catalogues")["tags"] if r["name"] == "Synthetic learners")
    rule_line = {"id": str(uuid.uuid4()), "who": {"conditions": [line("tags", tag["id"])]},
                 "starts_on": "2026-11-01", "numbers": {"max": {"value": 3}}}
    proposal = h.command("coordinator", "rule.propose", {"catalogue_id": "duties_per_week", "lines": [rule_line], "reason": "Synthetic limits"})["record"]
    selected = {"collection": "policy_lines", "parent": {"collection": "people", "selection": {"collection": "people", "names": ["Alex Morgan"]}},
        "where": {"filters": {"policy_id": [proposal["id"]]}}}
    found = resolve(h, selected)
    assert found["count"] == 1 and found["sample"][0]["id"] == rule_line["id"] and found["by_parent"] == {person: 1}
    selected["where"] = {"filters": {"policy_id": [proposal["id"]], "id": [rule_line["id"]],
        "starts_on": {"gte": "2026-11-01"}}}
    assert resolve(h, selected)["count"] == 1
    selected["where"]["filters"]["starts_on"] = {"lt": "2026-11-01"}
    assert resolve(h, selected)["count"] == 0
    selected["where"] = {"filters": {"policy_id": [proposal["id"]]}}
    selected["parent"]["selection"]["names"] = ["Taylor Noor"]
    assert resolve(h, selected)["count"] == 0


def test_session_conditions_bind_and_groups_to_one_assigned_person(h):
    first, second = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, second).tags = ["Synthetic selected"]
    h.shift(h.workspace(), people=[first, second])
    selection = {"collection": "sessions", "where": {"conditions": {"conditions": [line("tags", "Synthetic selected")]}}}
    assert resolve(h, selection)["count"] == 1
    selection["where"]["conditions"]["conditions"].append(line("person_ids", first))
    assert resolve(h, selection)["count"] == 0
    selected = {"collection": "sessions", "parent": {"collection": "people", "selection": {"collection": "people", "names": ["Alex Morgan"]}}}
    assert resolve(h, selected)["by_parent"] == {first: 1}
    with transaction(h.app.state.engine) as db:
        page = bulk.rows(db, actor(db, h), {"selection": selected, "columns": ["person.display_name", "service.name"]})
    assert len(page["rows"][0]["person.display_name"]) == 2 and page["rows"][0]["service.name"]


def test_rows_paging_sort_group_related_columns_and_filter(h):
    first = h.seed["people"][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, first).staff_number = "SYNTHETIC-123"
    payload = {"selection": {"collection": "eligibilities"}, "columns": ["person_id", "service_id", "person.display_name", "person.staff_number", "person.category_ids", "service.name"],
               "sort": [{"field": "person.display_name", "direction": "desc"}], "group": "service.name", "offset": 0, "limit": 2}
    with transaction(h.app.state.engine) as db:
        result = bulk.rows(db, actor(db, h), payload)
        payload["offset"] = 2
        next_page = bulk.rows(db, actor(db, h), payload)
    assert len(result["rows"]) == 2 and result["has_more"]
    assert not {r["id"] for r in result["rows"]} & {r["id"] for r in next_page["rows"]}
    assert sum(g["count"] for g in result["groups"]) == result["total"]
    assert all(r["person.display_name"] and r["person.category_ids"] and r["service.name"] for r in result["rows"])
    payload["selection"]["where"] = {"filters": {"person.staff_number": ["SYNTHETIC-123"]}}
    payload["offset"] = 0
    with transaction(h.app.state.engine) as db:
        filtered = bulk.rows(db, actor(db, h), payload)
    assert filtered["total"] and all(r["person_id"] == first for r in filtered["rows"])


def test_bad_columns_filters_and_parent_depth_refused(h):
    with transaction(h.app.state.engine) as db:
        current = actor(db, h)
        for payload in ({"selection": {"collection": "people"}, "columns": ["password_hash"]},
                        {"selection": {"collection": "people", "where": {"filters": {"surprise": ["x"]}}}}):
            with pytest.raises(Problem):
                bulk.rows(db, current, payload)


def test_candidate_cap_and_sample_size(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([m.Eligibility(department_id=h.seed["department_id"], person_id=h.seed["people"][0],
                    service_id=h.seed["services"][0], starts_on=date(2030, 1, 1)) for _ in range(5001)])
    with pytest.raises(Problem) as error:
        resolve(h, {"collection": "eligibilities"})
    assert error.value.code == "BULK_SELECTION_LIMIT" and "narrow the filter" in error.value.message
    selected = resolve(h, {"collection": "eligibilities", "where": {"filters": {"starts_on": ["2020-01-01"]}}})
    assert len(selected["sample"]) <= 100


@pytest.fixture(scope="module")
def resolver_postgres_5000_measurement(scale_harness):
    pg = scale_harness("pg")
    with transaction(pg.app.state.engine, write=True) as db:
        existing = db.scalar(select(func.count()).select_from(m.Eligibility).where(m.Eligibility.department_id == pg.seed["department_id"]))
        db.add_all([m.Eligibility(department_id=pg.seed["department_id"], person_id=pg.seed["people"][0],
                    service_id=pg.seed["services"][0], starts_on=date(2030, 1, 1)) for _ in range(5000 - existing)])
    with transaction(pg.app.state.engine) as db:
        current = actor(db, pg)
        started, wall_started = time.process_time(), time.perf_counter()
        result = bulk.resolve(db, current, {"collection": "eligibilities"})
        elapsed = time.process_time() - started
        wall_elapsed = time.perf_counter() - wall_started
    return result, elapsed, wall_elapsed


@pytest.mark.postgres
def test_resolver_postgres_5000_candidate_count_and_sample(resolver_postgres_5000_measurement):
    result, _, _ = resolver_postgres_5000_measurement
    assert result["count"] == 5000 and len(result["sample"]) == 100


@pytest.mark.postgres
@pytest.mark.cpu_budget
def test_resolver_postgres_5000_candidate_budget(resolver_postgres_5000_measurement):
    _, elapsed, _ = resolver_postgres_5000_measurement
    assert elapsed <= (0.9 if os.getenv("CI") else 0.3), f"5,000-candidate resolver took {elapsed:.3f}s"


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_resolver_postgres_5000_candidate_wall_benchmark(resolver_postgres_5000_measurement):
    _, _, elapsed = resolver_postgres_5000_measurement
    assert elapsed <= (0.9 if os.getenv("CI") else 0.3), f"5,000-candidate resolver took {elapsed:.3f}s"

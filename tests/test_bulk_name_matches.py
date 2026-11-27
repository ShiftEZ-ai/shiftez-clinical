"""Pasted-line diagnostics use the shared matcher and return collection record IDs."""
from datetime import date

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import bulk_selection as bulk
from packages.contracts import schemas as s
from scripts.generate_contracts import render


def resolve(h, selection, role="coordinator"):
    response = h.client(role).post("/api/bulk/resolve", json=selection)
    assert response.status_code == 200, response.text
    result = response.json()
    s.BulkResolved.model_validate(result)
    return result


def test_people_report_every_line_with_zero_one_or_many_matches_using_the_shared_matcher(h, monkeypatch):
    first, second, third = h.seed["people"][:3]
    with transaction(h.app.state.engine, write=True) as db:
        person = db.get(m.Person, first)
        person.display_name, person.staff_number, person.short_code = "Jos\u00e9 Example", "HR42", "JEX"
        db.get(m.Person, second).short_code = "HR42"
        for pid in (second, third):
            db.get(m.Person, pid).display_name = "Twin"
        db.add(m.SourceMapping(department_id=h.seed["department_id"], source_name="Synthetic matches",
            source_key="alias:Dr Jos\u00e9", target_kind="people", target_id=first))
    calls, original = [], bulk.match_people

    def match(*args, **kwargs):
        calls.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(bulk, "match_people", match)
    lines = ["Nobody", "hr42", "jex", "JOSE EXAMPLE", "  DR JOSE  ", "Twin", "Twin", "Jos\u00e9 Example"]
    result = resolve(h, {"collection": "people", "names": lines})
    normalized = [line.strip() for line in lines]  # The shared request schema trims surrounding whitespace.
    assert calls == [normalized]
    assert [match["line"] for match in result["matches"]] == normalized
    assert [set(match["ids"]) for match in result["matches"]] == [
        set(), {first}, {first}, {first}, {first}, {second, third}, {second, third}, {first}]
    assert result["count"] == 1 and result["sample"][0]["id"] == first
    assert result["unmatched"] == ["Nobody"]
    assert [row["line"] for row in result["ambiguous"]] == ["Twin", "Twin"]


def test_non_people_report_name_and_short_code_matches_without_selecting_ambiguity(h):
    first = h.configure("categories", {"name": "\u00c9quipe", "short_code": "EQ", "starts_on": "2020-01-01"})["record"]
    second = h.configure("categories", {"name": "Equipe", "short_code": "EQ2", "starts_on": "2020-01-01"})["record"]
    lines = ["Unknown rank", "eq", "\u00e9quipe", "eq2", "EQ"]
    result = resolve(h, {"collection": "categories", "names": lines})
    assert [row["line"] for row in result["matches"]] == lines
    assert [set(row["ids"]) for row in result["matches"]] == [
        set(), {first["id"]}, {first["id"], second["id"]}, {second["id"]}, {first["id"]}]
    assert result["count"] == 2 and result["unmatched"] == ["Unknown rank"]
    assert result["ambiguous"][0]["line"] == "\u00e9quipe"
    ambiguous = resolve(h, {"collection": "categories", "names": ["EQUIPE"]})
    assert ambiguous["count"] == 0
    assert set(ambiguous["matches"][0]["ids"]) == {first["id"], second["id"]}


def test_child_matches_are_collection_ids_including_ambiguous_candidates_with_department_scope(h):
    first, second = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, first).display_name = db.get(m.Person, second).display_name = "Twin"
        db.get(m.Person, first).staff_number = "HR42"
        extra = m.Eligibility(department_id=h.seed["department_id"], person_id=first,
            service_id=h.seed["services"][0], starts_on=date(2030, 1, 1))
        other = m.Department(organisation_id=h.seed["organisation_id"], name="Other synthetic department")
        db.add_all([extra, other])
        db.flush()
        hidden = m.Eligibility(department_id=other.id, person_id=first,
            service_id=h.seed["services"][0], starts_on=date(2030, 1, 1))
        db.add(hidden)
        db.flush()
        hidden_id = hidden.id
    records = resolve(h, {"collection": "eligibilities"})["sample"]
    first_ids = {row["id"] for row in records if row["person_id"] == first}
    twin_ids = {row["id"] for row in records if row["person_id"] in {first, second}}
    assert len(first_ids) > 1
    result = resolve(h, {"collection": "eligibilities", "names": ["HR42", "Twin", "Nobody"]})
    assert [set(row["ids"]) for row in result["matches"]] == [first_ids, twin_ids, set()]
    assert {row["id"] for row in result["sample"]} == first_ids
    assert hidden_id not in twin_ids
    ambiguous = resolve(h, {"collection": "eligibilities", "names": ["Twin"]})
    assert ambiguous["count"] == 0 and set(ambiguous["matches"][0]["ids"]) == twin_ids
    explicit = next(iter(first_ids))
    selected = resolve(h, {"collection": "eligibilities", "names": ["Twin"], "include_ids": [explicit]})
    assert selected["count"] == 1 and selected["sample"][0]["id"] == explicit
    assert set(selected["matches"][0]["ids"]) == twin_ids


def test_child_line_matches_include_ids_beyond_the_first_hundred_sample_rows(h):
    first = h.seed["people"][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.Eligibility(department_id=h.seed["department_id"], person_id=first,
            service_id=h.seed["services"][0], starts_on=date(2030, 1, 1)) for _ in range(120))
    result = resolve(h, {"collection": "eligibilities", "names": ["Staff 01(test)"]})
    assert result["count"] > 120 and len(result["sample"]) == 100
    assert len(set(result["matches"][0]["ids"])) == result["count"]
    assert {row["id"] for row in result["sample"]} < set(result["matches"][0]["ids"])


@pytest.mark.parametrize("criteria", ["filter", "conditions", "parent", "exclude"])
def test_matches_respect_filters_parents_and_exclusions_without_adding_explicit_ticks(h, criteria):
    first, second = h.seed["people"][:2]
    selection = {"collection": "eligibilities", "names": ["Staff 01(test)"]}
    all_rows = resolve(h, {"collection": "eligibilities"})["sample"]
    first_ids = {row["id"] for row in all_rows if row["person_id"] == first}
    explicit = next(row["id"] for row in all_rows if row["person_id"] == second)
    selection["include_ids"] = [explicit]
    if criteria == "filter":
        selection["where"] = {"filters": {"person_id": [second]}}
    elif criteria == "conditions":
        selection["where"] = {"conditions": {"conditions": [{"field": "person_ids", "values": [second]}]}}
    elif criteria == "parent":
        selection["parent"] = {"collection": "people", "selection": {"collection": "people", "include_ids": [second]}}
    else:
        selection["exclude_ids"] = list(first_ids)
    result = resolve(h, selection)
    assert result["matches"] == [{"line": "Staff 01(test)", "ids": []}]
    assert {row["id"] for row in result["sample"]} == {explicit}


def test_matches_cannot_disclose_another_department_or_persons_private_rows(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Person(organisation_id=h.seed["organisation_id"], display_name="Outside department", staff_number="FOREIGN"))
    result = resolve(h, {"collection": "people", "names": ["FOREIGN"]})
    assert result["matches"] == [{"line": "FOREIGN", "ids": []}]
    h.command("coordinator", "availability.record", {"person_id": h.seed["people"][1], "kind": "recorded_leave",
        "start": "2026-11-01T00:00:00+03:00", "end": "2026-11-02T00:00:00+03:00", "private_reason": "Private synthetic note"})
    with transaction(h.app.state.engine) as db:
        other_name = db.get(m.Person, h.seed["people"][1]).display_name
    result = resolve(h, {"collection": "availability", "names": [other_name]}, role="staff")
    assert result["count"] == 0 and result["matches"] == [{"line": other_name, "ids": []}]


def test_matches_default_to_empty_and_generate_a_typed_collection_id_list(h):
    assert resolve(h, {"collection": "people"})["matches"] == []
    assert resolve(h, {"collection": "people", "names": []})["matches"] == []
    assert s.BulkResolved(count=0, token="synthetic").matches == []
    source, schemas = render()
    assert schemas["BulkResolved"]["properties"]["matches"]["items"] == {"$ref": "#/$defs/BulkNameMatch"}
    assert 'export type BulkNameMatch = {' in source and '"ids": Array<string>;' in source
    assert '"matches"?: Array<BulkNameMatch>;' in source


def test_expanded_match_ids_are_bounded_before_building_the_output():
    records = [{"id": str(index), "person_id": "person"} for index in range(bulk.MAX_CANDIDATES)]
    match = {"line": "Synthetic person", "ids": ["person"]}
    assert len(bulk._matched_lines([match], records, "person_id")[0]["ids"]) == bulk.MAX_CANDIDATES
    with pytest.raises(Problem) as error:
        bulk._matched_lines([match] * bulk.MAX_CANDIDATES, records, "person_id")
    assert error.value.status == 422 and error.value.code == "BULK_SELECTION_LIMIT"
    assert "narrow the pasted list" in error.value.message


def test_excessive_child_match_expansion_is_a_clear_api_refusal(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Eligibility(department_id=h.seed["department_id"], person_id=h.seed["people"][0],
            service_id=h.seed["services"][0], starts_on=date(2030, 1, 1)))
    response = h.client("coordinator").post("/api/bulk/resolve", json={
        "collection": "eligibilities", "names": ["Staff 01(test)"] * bulk.MAX_CANDIDATES})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "BULK_SELECTION_LIMIT"
    assert response.json()["error"]["message"] == "More than 5,000 matching record IDs; narrow the pasted list"

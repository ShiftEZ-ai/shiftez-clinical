"""Related person columns filter the same dated person as the rest of each condition group."""
from copy import deepcopy

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_bulk_linked_people import people_and_accounts, post
from .test_bulk_rule_line_people import authored


def subjects(h, collection):
    people, first, _ = people_and_accounts(h)
    rank = h.configure("categories", {"name": "Other synthetic rank", "starts_on": "2020-01-01"})["record"]["id"]
    programme = h.configure("programmes", {"name": "Other synthetic programme", "starts_on": "2020-01-01"})["record"]["id"]
    h.command("admin", "membership.create", {"person_id": people[1], "category_id": rank, "starts_on": "2026-10-04"})
    h.configure("programme_memberships", {"person_id": people[1], "programme_id": programme,
        "starts_on": "2026-10-04", "ends_on": "2027-01-01"})
    with transaction(h.app.state.engine, write=True) as db:
        for index, pid in enumerate(people):
            person = db.get(m.Person, pid)
            person.staff_number = f"ALIAS-{index}"
            person.tags = [f"alias witness {index}"]
    expected = {
        "person.display_name": ("Synthetic Alex", "Synthetic Blair"),
        "person.staff_number": ("ALIAS-0", "ALIAS-1"),
        "person.tags": ("alias witness 0", "alias witness 1"),
        "person.category_ids": (first["category_ids"], rank),
        "person.programme_ids": (first["programme_ids"], programme),
    }
    if collection == "policy_lines":
        selection, _ = authored(h, {"conditions": [{"field": "person_ids", "values": people}]})
    elif collection == "sessions":
        for pid in people:
            h.command("credentialer", "eligibility.create", {"person_id": pid,
                "service_id": h.seed["services"][0], "starts_on": "2020-01-01"})
        saved = h.shift(h.workspace(), people=people)
        selection = {"collection": collection, "where": {"filters": {"id": [saved["shift_id"]]}}}
    elif collection == "eligibilities":
        row = h.command("credentialer", "eligibility.create", {"person_id": people[0],
            "service_id": h.seed["services"][0], "starts_on": "2020-01-01"})["record"]
        selection = {"collection": collection, "where": {"filters": {"id": [row["id"]]}}}
    else:
        row = h.configure(collection, {"name": "Synthetic alias witnesses", "starts_on": "2026-10-01",
            "location_id": h.seed["locations"][0], "assignment_type_id": h.seed["type"],
            "definition": {"anchor": "2026-10-04", "days": [{"weekday": 6, "start_time": "08:00", "end_time": "12:00"}],
                "default_person_ids": people, "service_ids": [h.seed["services"][0]]}})["record"]
        selection = {"collection": collection, "where": {"filters": {"id": [row["id"]]}}}
    selection["where"]["as_of"] = "2026-10-04"
    return selection, people, expected


@pytest.mark.parametrize("collection", ["policy_lines", "sessions", "recurring_series"])
@pytest.mark.parametrize("path", ["tree", "flat", "mixed"])
@pytest.mark.parametrize("alias", tuple(s._PERSON_BULK_COLUMNS))
def test_every_related_person_alias_binds_to_the_same_dated_witness_in_resolve_rows_and_cache(h, collection, path, alias):
    selection, people, values = subjects(h, collection)
    assert alias in s.BULK_COLLECTIONS[collection]["related_columns"]
    for value, expected in ((values[alias][1], 0), (values[alias][0], 1)):
        chosen = deepcopy(selection)
        person = {"field": "person_ids", "values": [people[0]]}
        condition = {"field": alias, "values": [value]}
        if path == "flat":
            chosen["where"]["filters"].update(person_ids=person["values"], **{alias: condition["values"]})
        elif path == "mixed":
            chosen["where"]["filters"][alias] = condition["values"]
            chosen["where"]["conditions"] = {"conditions": [person]}
        else:
            chosen["where"]["conditions"] = {"conditions": [person, {"match": "any", "conditions": [condition]}]}
        payload = {"selection": chosen, "columns": [alias], "sort": [{"field": alias}], "group": alias, "limit": 1}
        for _ in range(2):
            assert post(h, "resolve", chosen)["count"] == post(h, "rows", payload)["count"] == expected
        assert post(h, "rows", {**payload, "offset": 1})["rows"] == []
        if alias in {"person.category_ids", "person.programme_ids"} and expected:
            for day in ("2026-10-03", "2026-11-01"):
                chosen["where"]["as_of"] = day
                assert post(h, "resolve", chosen)["count"] == post(h, "rows", payload)["count"] == 0


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_rule_line_alias_only_groups_never_mix_two_peoples_facts(request, database):
    h = request.getfixturevalue(database)
    selection, _, values = subjects(h, "policy_lines")
    selection["where"]["conditions"] = {"conditions": [
        {"field": "person.display_name", "values": [values["person.display_name"][0]]},
        {"field": "person.staff_number", "values": [values["person.staff_number"][1]]}]}
    assert post(h, "resolve", selection)["count"] == post(h, "rows", {"selection": selection})["count"] == 0
    selection["where"]["conditions"]["conditions"][1]["values"] = [values["person.staff_number"][0]]
    assert post(h, "resolve", selection)["count"] == post(h, "rows", {"selection": selection})["count"] == 1


@pytest.mark.parametrize("collection", ["policy_lines", "sessions", "recurring_series", "eligibilities"])
@pytest.mark.parametrize("path", ["tree", "flat"])
@pytest.mark.parametrize("label", ["MixedCase Witness", "Straẞe Witness"])
def test_related_person_tags_use_canonical_names_and_ids_for_every_filter_path(h, collection, path, label):
    selection, people, _ = subjects(h, collection)
    tag = h.configure("tags", {"name": label, "labels": ["people"]})["record"]
    h.command("admin", "tag.members", {"add": [{"kind": "people", "id": people[0]}], "remove": [],
        "reason": "Synthetic related tag"}, tag["id"], tag["revision"])
    for wanted in (tag["name"].upper(), tag["name"].lower(), tag["id"]):
        chosen = deepcopy(selection)
        if path == "flat":
            chosen["where"]["filters"]["person.tags"] = [wanted]
        else:
            chosen["where"]["conditions"] = {"conditions": [{"field": "person.tags", "values": [wanted]}]}
        assert post(h, "resolve", chosen)["count"] == post(h, "rows", {"selection": chosen})["count"] == 1
        negative = deepcopy(selection)
        negative["where"]["conditions"] = {"conditions": [{"field": "person.tags", "op": "none", "values": [wanted]}]}
        assert post(h, "resolve", negative)["count"] == post(h, "rows", {"selection": negative})["count"] == 0


@pytest.mark.parametrize("path", ["tree", "flat"])
@pytest.mark.parametrize("label", ["MixedCase Clinic", "Straẞe Clinic"])
def test_related_service_tags_use_canonical_names_and_ids_without_changing_display_labels(h, path, label):
    selection, _, _ = subjects(h, "eligibilities")
    tag = h.configure("tags", {"name": label, "labels": ["services"]})["record"]
    h.command("admin", "tag.members", {"add": [{"kind": "services", "id": h.seed["services"][0]}], "remove": [],
        "reason": "Synthetic related clinic tag"}, tag["id"], tag["revision"])
    for wanted in (tag["name"].upper(), tag["name"].lower(), tag["id"]):
        chosen = deepcopy(selection)
        if path == "flat":
            chosen["where"]["filters"]["service.tags"] = [wanted]
        else:
            chosen["where"]["conditions"] = {"conditions": [{"field": "service.tags", "values": [wanted]}]}
        assert post(h, "resolve", chosen)["count"] == 1
        rows = post(h, "rows", {"selection": chosen, "columns": ["service.tags"]})
        assert rows["count"] == 1
        assert tag["name"] in rows["rows"][0]["service.tags"]
        negative = deepcopy(selection)
        negative["where"]["conditions"] = {"conditions": [{"field": "service.tags", "op": "none", "values": [wanted]}]}
        assert post(h, "resolve", negative)["count"] == post(h, "rows", {"selection": negative})["count"] == 0

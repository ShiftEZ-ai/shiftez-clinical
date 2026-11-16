"""Import plans and bulk name selections share safe, forgiving person matching."""
import csv
import io

import pytest

from apps.api import models as m
from apps.api.db import transaction


def import_preview(h, **cells):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(cells))
    writer.writeheader()
    writer.writerow(cells)
    return h.command("coordinator", "staff_import.preview", {
        "source_name": "Synthetic matching review", "csv_text": output.getvalue()})


def resolve_name(h, name):
    response = h.client("coordinator").post("/api/bulk/resolve", json={"collection": "people", "names": [name]})
    assert response.status_code == 200, response.text
    return response.json()


def test_case_only_staff_number_collision_is_ambiguous_in_import_and_bulk_resolver(h):
    first, second = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, first).staff_number = "Case-42"
        db.get(m.Person, second).staff_number = "case-42"
    before = h.get("coordinator", "/api/catalogues")["people"]

    imported = import_preview(h, display_name=before[0]["display_name"], staff_number="CASE-42")
    row = imported["rows"][0]
    assert row["person"]["id"] is None and row["person"]["by"] == "staff_number"
    assert "2 people already have this staff number" in row["errors"]["staff_number"]
    assert imported["summary"]["error_rows"] == 1
    assert imported["summary"]["new_people"] == imported["summary"]["updated_people"] == 0
    selected = resolve_name(h, "CASE-42")
    assert selected["count"] == 0 and selected["sample"] == [] and selected["unmatched"] == []
    assert len(selected["ambiguous"]) == 1 and selected["ambiguous"][0]["line"] == "CASE-42"
    assert {person["id"] for person in selected["ambiguous"][0]["candidates"]} == {first, second}

    batch = imported["batch"]
    refused = h.command("coordinator", "staff_import.apply", {"reason": "Synthetic ambiguous import"},
                        batch["id"], batch["revision"], status=422)
    assert refused["error"]["code"] == "IMPORT_ROW_ERRORS"
    assert h.get("coordinator", "/api/catalogues")["people"] == before


@pytest.mark.parametrize("cells,name,matched_by", [
    ({"display_name": "José Example", "staff_number": "HR42"}, "hr42", "staff_number"),
    ({"display_name": "José Example", "short_code": "jex"}, "jex", "short_code"),
    ({"display_name": "JOSE EXAMPLE"}, "JOSE EXAMPLE", "display_name"),
    ({"display_name": "  dr jose  "}, "  DR JOSE  ", "aliases"),
])
def test_normalized_identifiers_names_and_aliases_select_the_same_import_and_bulk_person(h, cells, name, matched_by):
    first, second = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        person = db.get(m.Person, first)
        person.display_name, person.staff_number, person.short_code = "José Example", "hr42", "JEX"
        db.get(m.Person, second).short_code = "HR42"  # A staff number outranks another person's short code.
        for key in ("alias:Dr José", "alias:dr jose"):
            db.add(m.SourceMapping(department_id=h.seed["department_id"], source_name="Synthetic aliases",
                                   source_key=key, target_kind="people", target_id=first))

    imported = import_preview(h, **cells)["rows"][0]
    assert not imported["errors"]
    assert imported["person"]["id"] == first and imported["person"]["by"] == matched_by
    selected = resolve_name(h, name)
    assert selected["count"] == 1 and [row["id"] for row in selected["sample"]] == [first]
    assert selected["unmatched"] == selected["ambiguous"] == []


def test_normalized_source_alias_collision_is_ambiguous_in_import_and_bulk_resolver(h):
    first, second = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        for person_id, key in ((first, "alias:Dr José"), (second, "alias:DR JOSE")):
            db.add(m.SourceMapping(department_id=h.seed["department_id"], source_name="Synthetic aliases",
                                   source_key=key, target_kind="people", target_id=person_id))

    imported = import_preview(h, display_name="dr jose")["rows"][0]
    assert imported["person"]["id"] is None and imported["person"]["by"] == "aliases"
    assert imported["errors"]["display_name"] == (
        "More than one person matches these rota names or aliases; give a staff number or short code")
    selected = resolve_name(h, "dr jose")
    assert selected["count"] == 0 and selected["unmatched"] == []
    assert {person["id"] for person in selected["ambiguous"][0]["candidates"]} == {first, second}

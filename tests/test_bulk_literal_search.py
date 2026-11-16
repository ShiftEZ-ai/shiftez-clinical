"""Bulk text search treats SQL pattern characters literally on every search path."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity


def search_records(h, source, text):
    first, second = h.seed["people"][:2]
    services = h.seed["services"][:2]
    decoy = text.replace("%", "X").replace("_", "X").replace("\\", "X")
    if source in {"direct_person", "linked_person", "linked_staff_number", "account_person", "participant"}:
        field = "staff_number" if source == "linked_staff_number" else "display_name"
        with transaction(h.app.state.engine, write=True) as db:
            setattr(db.get(m.Person, first), field, text)
            setattr(db.get(m.Person, second), field, decoy)
    if source in {"direct_service", "linked_service"}:
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Service, services[0]).name = text
            db.get(m.Service, services[1]).name = decoy
    if source == "direct_person":
        return "people", [first, second]
    if source == "direct_service":
        return "services", services
    if source in {"linked_person", "linked_staff_number", "linked_service"}:
        pairs = [(first, services[0]), (second, services[0])] if source != "linked_service" else [
            (first, services[0]), (first, services[1])]
        with transaction(h.app.state.engine) as db:
            ids = [db.scalar(select(m.Eligibility.id).where(
                m.Eligibility.person_id == person, m.Eligibility.service_id == service)) for person, service in pairs]
        assert all(ids)
        return "eligibilities", ids
    if source in {"linked_identity", "account_person"}:
        ids = [identity(h, "literal-search-" + str(index), ["staff"],
                        person=person if source == "account_person" else None)
               for index, person in enumerate((first, second))]
        if source == "account_person":
            return "identities", ids
        with transaction(h.app.state.engine, write=True) as db:
            for account, name in zip(ids, (text, decoy)):
                db.get(m.Identity, account).display_name = name
            bindings = [db.scalar(select(m.RoleBinding.id).where(m.RoleBinding.identity_id == account)) for account in ids]
        return "roles", bindings
    assert source == "participant"
    one = h.shift(h.workspace("Literal participant search"), people=[first])
    h.shift(one["workspace"], people=[second],
            start="2026-10-05T08:00:00+03:00", end="2026-10-05T12:00:00+03:00")
    with transaction(h.app.state.engine) as db:
        by_person = dict(db.execute(select(m.Assignment.person_id, m.Shift.id).join(
            m.Shift, m.Shift.id == m.Assignment.shift_id).where(m.Shift.workspace_id == one["workspace"]["id"])).all())
    return "sessions", [by_person[first], by_person[second]]


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("source", ["direct_person", "direct_service", "linked_person", "linked_staff_number", "linked_identity",
                                    "account_person", "linked_service", "participant"])
@pytest.mark.parametrize("character", ["%", "_", "\\"])
def test_resolve_and_rows_search_literal_pattern_characters(request, database, source, character):
    h = request.getfixturevalue(database)
    text = "Literal" + character + "Search"
    collection, ids = search_records(h, source, text)
    selection = {"collection": collection, "where": {"q": text.upper(), "filters": {"id": ids}}}
    client = h.client("admin")
    resolved = client.post("/api/bulk/resolve", json=selection)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["count"] == 1, resolved.text
    assert [row["id"] for row in resolved.json()["sample"]] == ids[:1]
    page = client.post("/api/bulk/rows", json={"selection": selection, "limit": 1})
    assert page.status_code == 200, page.text
    assert page.json()["count"] == 1
    assert [row["id"] for row in page.json()["rows"]] == ids[:1]

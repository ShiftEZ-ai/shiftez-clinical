"""Advertised computed columns are filterable through the projection used by their own rows."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import events
from apps.api.services import bulk_selection
from packages.contracts import schemas as s
from .test_bulk_linked_people import post
from .test_events import event
from .test_rule_catalogue_api import line, propose
from .test_scheduling_facts_api import workload
from .test_bulk_selection import actor


@pytest.mark.parametrize("collection,field", [
    ("events", "tag_ids"), ("policies", "catalogue_id"), ("scheduling_facts", "catalogue_id"),
    ("sessions", "service_ids"),
])
def test_registry_computed_columns_share_projection_in_flat_tree_rows_and_cache(h, collection, field):
    assert field in s.BULK_COLLECTIONS[collection]["columns"]
    if collection == "events":
        tag = h.configure("tags", {"name": "Synthetic event facet", "labels": ["events"]})["record"]
        row = event(h, tag_ids=[tag["id"]])
        expected = tag["id"]
    elif collection == "policies":
        expected = "duties_per_week"
        row = propose(h, expected, [line(numbers={"max": {"value": 3}})])["record"]
    elif collection == "scheduling_facts":
        expected = "workload_limit"
        row = h.command("coordinator", "person.workload.propose", workload(h))["fact"]
    else:
        saved = h.shift(h.workspace())
        row, expected = h.detail(saved["workspace"])["shifts"][0], h.seed["services"][0]
    for path in ("flat", "tree"):
        selection = {"collection": collection, "where": {"filters": {"id": [row["id"]]}}}
        if path == "flat":
            selection["where"]["filters"][field] = [expected]
        else:
            selection["where"]["conditions"] = {"conditions": [{"field": field, "values": [expected]}]}
        for _ in range(2):
            assert post(h, "resolve", selection)["count"] == 1
            page = post(h, "rows", {"selection": selection, "columns": [field]})
            assert page["count"] == 1 and page["rows"][0][field] in (expected, [expected])


def test_event_canonical_and_bulk_tags_share_pending_and_deleted_members(h):
    tag = h.configure("tags", {"name": "Pending event facet", "labels": ["events"]})["record"]
    row = event(h)
    with transaction(h.app.state.engine, write=True) as db:
        saved = db.get(m.Event, row["id"])
        member = m.TagMember(department_id=h.seed["department_id"], tag_id=tag["id"], kind="events", member_id=saved.id)
        db.add(member)
        with db.no_autoflush:
            assert events.event_value(db, saved)["tag_ids"] == [tag["id"]]
            assert events.event_tag_ids(db, saved.department_id, [saved.id])[saved.id] == [tag["id"]]
        db.flush()
        db.delete(member)
        with db.no_autoflush:
            assert events.event_value(db, saved)["tag_ids"] == []


@pytest.mark.parametrize("mode", ["flat", "conditions"])
def test_role_projection_keeps_only_the_sign_ins_active_roles_in_rows_and_revoke_preview(h, mode):
    from .test_bulk_account_roles import accounts, role_selection
    from .test_bulk import preview
    identities = accounts(h)
    with transaction(h.app.state.engine) as db:
        bindings = list(db.scalars(select(m.RoleBinding).where(
            m.RoleBinding.identity_id.in_(identities.values()))))
        expected = {row.id for row in bindings if row.identity_id in {identities['unlinked_lead'], identities['linked_lead']}}
        selection = role_selection([row.id for row in bindings], mode, collection='roles')
    for _ in range(2):
        page = post(h, 'rows', {'selection': selection})
        assert page['count'] == 2 and {row['id'] for row in page['rows']} == expected
    checked = preview(h, 'admin', {'item_kind': 'role.revoke', 'selection': selection, 'reason': 'Revoke selected active access'})
    assert checked['counts']['ready'] == 2 and {row['target_id'] for row in checked['items']} == expected


@pytest.mark.parametrize("collection", tuple(s.BULK_COLLECTIONS))
def test_every_registry_collection_condition_uses_the_same_flat_and_tree_holdings(h, collection):
    values = {"person_ids": h.seed["people"][0], "service_ids": h.seed["services"][0],
        "eligible_service_ids": h.seed["services"][0], "location_ids": h.seed["locations"][0],
        "assignment_type_ids": h.seed["type"], "usual_assignee_ids": h.seed["people"][0],
        "roles": "coordinator", "tags": "Synthetic unmatched tag"}
    with transaction(h.app.state.engine) as db:
        current = actor(db, h, "operator" if collection in {"history", "outbox"} else "admin")
        registry = s.BULK_COLLECTIONS[collection]
        fields = [definition["key"] for definition in registry["conditions"]]
        statuses = registry["status"]["options"]
        if statuses and "status" not in fields:
            fields.append("status")
        for field in fields:
            kind = s.condition_field_definition(field, collection)["value_kind"]
            value = (statuses[0]["value"] if field == "status" else
                     "2026-10-04" if kind == "date" else 1 if kind == "number" else
                     "Synthetic unmatched text" if kind == "text" else values.get(field, h.seed["people"][0]))
            condition = ({"field": field, "op": "is", "value": value} if kind in {"date", "number", "text"} else
                         {"field": field, "values": [value]})
            try:
                flat = bulk_selection.resolve_targets(db, current, {"collection": collection,
                    "where": {"as_of": "2026-10-04", "filters": {field: [value]}}})
                tree = bulk_selection.resolve_targets(db, current, {"collection": collection,
                    "where": {"as_of": "2026-10-04", "conditions": {"conditions": [condition]}}})
            except Exception as exc:
                exc.add_note(f"Collection parity: {collection}.{field}, kind={kind}, literal={value!r}")
                raise
            assert [bulk_selection._row_ref(row, collection) for row in flat["records"]] == [
                bulk_selection._row_ref(row, collection) for row in tree["records"]], (collection, field, kind, value)

"""Multi-person records use their canonical participants and frozen recurrence fields."""
from copy import deepcopy
from datetime import timedelta

import pytest

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk_linked_people import people_and_accounts, post
from .test_staff_swaps import fixed_clock, source_pair  # noqa: F401


@pytest.mark.parametrize("collection", ["sessions", "recurring_series"])
@pytest.mark.parametrize("op", ["any", "none"])
def test_usual_assignee_condition_uses_frozen_recurrence_for_resolve_rows_and_cache(h, collection, op):
    first, second = h.seed["people"][:2]
    series = h.configure("recurring_series", {"name": "Synthetic usual assignees", "starts_on": "2026-10-01",
        "ends_on": "2027-01-01", "location_id": h.seed["locations"][0], "assignment_type_id": h.seed["type"],
        "definition": {"anchor": "2026-10-04", "days": [{"weekday": 6, "start_time": "08:00", "end_time": "12:00"}],
            "default_person_ids": [first], "service_ids": [h.seed["services"][0]]}})["record"]
    if collection == "sessions":
        saved = h.shift(h.workspace(), people=[second])
        row = h.detail(saved["workspace"])["shifts"][0]
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Shift, row["id"]).series_snapshot = deepcopy(series)
            # The live series changes; the occurrence retains its original usual assignee.
            db.get(m.RecurringSeries, series["id"]).definition = {**series["definition"], "default_person_ids": [second]}
    else:
        row = series
    selection = {"collection": collection, "where": {"filters": {"id": [row["id"]]}, "conditions": {
        "conditions": [{"field": "usual_assignee_ids", "op": op, "values": [first]}]}}}
    expected = int(op == "any")
    for _ in range(2):
        assert post(h, "resolve", selection)["count"] == expected
        assert post(h, "rows", {"selection": selection})["count"] == expected
    selection["where"]["conditions"]["conditions"][0]["values"] = [second]
    assert post(h, "resolve", selection)["count"] == 1 - expected


@pytest.mark.parametrize("field", ["person_ids", "category_ids", "programme_ids"])
@pytest.mark.parametrize("op", ["any", "none"])
def test_swap_conditions_include_each_participant_and_preserve_scope(h, field, op):
    publication, first, _, _ = source_pair(h)
    people, values, _ = people_and_accounts(h)
    # Put the dated synthetic rank/programme on the second participant too.
    h.command("admin", "membership.create", {"person_id": h.seed["people"][1], "category_id": values["category_ids"],
        "starts_on": "2026-10-04", "ends_on": "2026-11-01"})
    h.configure("programme_memberships", {"person_id": h.seed["people"][1], "programme_id": values["programme_ids"],
        "starts_on": "2026-10-04", "ends_on": "2026-11-01"})
    swap = h.command("staff", "swap.offer", {"publication_id": publication, "assignment_id": first,
        "recipient_person_id": h.seed["people"][1], "expires_at": (m.now() + timedelta(hours=1)).isoformat(),
        "reason": "Synthetic participant condition"})["swap"]
    expected = int(op == "any")
    value = h.seed["people"][1] if field == "person_ids" else values[field]
    selection = {"collection": "swaps", "where": {"as_of": "2026-10-04", "filters": {"id": [swap["id"]]},
        "conditions": {"conditions": [{"field": field, "op": op, "values": [value]}]}}}
    for role in ("staff", "coordinator"):
        assert post(h, "resolve", selection, role)["count"] == post(h, "rows", {"selection": selection}, role)["count"] == expected
    assert post(h, "resolve", selection, "staff_three")["count"] == 0
    selection["where"].pop("conditions")
    selection["where"]["filters"][field] = [value]
    assert post(h, "resolve", selection)["count"] == post(h, "rows", {"selection": selection})["count"] == 1


@pytest.mark.parametrize("collection", ["sessions", "recurring_series"])
@pytest.mark.parametrize("field", ["person_ids", "category_ids", "programme_ids"])
def test_multi_person_flat_filters_and_negative_trees_share_participant_projection(h, collection, field):
    people, values, _ = people_and_accounts(h)
    if collection == "sessions":
        saved = h.shift(h.workspace(), people=people)
        row = h.detail(saved["workspace"])["shifts"][0]
    else:
        row = h.configure("recurring_series", {"name": "Synthetic participant projection", "starts_on": "2026-10-01",
            "ends_on": "2027-01-01", "location_id": h.seed["locations"][0], "assignment_type_id": h.seed["type"],
            "definition": {"anchor": "2026-10-04", "days": [{"weekday": 6, "start_time": "08:00", "end_time": "12:00"}],
                "default_person_ids": people, "service_ids": [h.seed["services"][0]]}})["record"]
    selection = {"collection": collection, "where": {"as_of": "2026-10-04", "filters": {"id": [row["id"]], field: [values[field]]}}}
    assert post(h, "resolve", selection)["count"] == post(h, "rows", {"selection": selection})["count"] == 1
    selection["where"]["filters"].pop(field)
    selection["where"]["conditions"] = {"conditions": [{"field": field, "op": "none", "values": [values[field]]}]}
    assert post(h, "resolve", selection)["count"] == post(h, "rows", {"selection": selection})["count"] == 0


@pytest.mark.parametrize("collection", ["sessions", "recurring_series", "swaps", "policy_lines"])
@pytest.mark.parametrize("mode", ["tree", "flat", "nested", "mixed"])
def test_participant_groups_bind_positive_fields_to_one_person_and_exclude_all_for_negative_fields(h, collection, mode):
    from .test_rule_catalogue_api import line, propose
    first, second = h.seed["people"][:2]
    if collection == "swaps":
        publication, assignment, _, _ = source_pair(h)
        row = h.command("staff", "swap.offer", {"publication_id": publication, "assignment_id": assignment,
            "recipient_person_id": second, "expires_at": (m.now() + timedelta(hours=1)).isoformat(),
            "reason": "Synthetic group witness"})["swap"]
    elif collection == "sessions":
        saved = h.shift(h.workspace(), people=[first, second])
        row = h.detail(saved["workspace"])["shifts"][0]
    elif collection == "recurring_series":
        row = h.configure(collection, {"name": "Synthetic group witnesses", "starts_on": "2026-10-01",
            "location_id": h.seed["locations"][0], "assignment_type_id": h.seed["type"],
            "definition": {"anchor": "2026-10-04", "days": [{"weekday": 6, "start_time": "08:00", "end_time": "12:00"}],
                "default_person_ids": [first, second], "service_ids": [h.seed["services"][0]]}})["record"]
    else:
        authored = line(who={"conditions": [{"field": "person_ids", "values": [first, second]}]},
            numbers={"max": {"value": 3}})
        proposal = propose(h, "duties_per_week", [authored])["record"]
        row = {"id": authored["id"], "policy_id": proposal["id"]}
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, second).tags = ["Synthetic witness"]
    filters = {"id": [row["id"]]}
    if collection == "policy_lines":
        filters["policy_id"] = [row["policy_id"]]
    for person, expected in ((first, 0), (second, 1)):
        where = {"filters": deepcopy(filters)}
        tag = {"field": "tags", "values": ["Synthetic witness"]}
        chosen = {"field": "person_ids", "values": [person]}
        if mode == "flat":
            where["filters"].update(tags=tag["values"], person_ids=chosen["values"])
        elif mode == "mixed":
            where["filters"]["person_ids"] = chosen["values"]
            where["conditions"] = {"conditions": [tag]}
        else:
            where["conditions"] = {"conditions": [tag, chosen if mode == "tree" else {"match": "any", "conditions": [chosen]}]}
        selection = {"collection": collection, "where": where}
        for _ in range(2):
            assert post(h, "resolve", selection)["count"] == post(h, "rows", {"selection": selection})["count"] == expected
    for tree, expected in (({"conditions": [{**tag, "op": "none"}]}, 0),
                           ({"match": "any", "conditions": [{**tag, "op": "none"}, chosen]}, 1)):
        selection = {"collection": collection, "where": {"filters": filters, "conditions": tree}}
        assert post(h, "resolve", selection)["count"] == post(h, "rows", {"selection": selection})["count"] == expected

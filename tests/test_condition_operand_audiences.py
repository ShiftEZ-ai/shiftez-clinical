"""Audience guards, email recipients and tag references read every absolute operand shape."""
from datetime import date
from types import SimpleNamespace

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import email_audiences, policies, tags
from packages.contracts import schemas as s
from .test_bulk_selection import actor
from .test_events import event_type, value


SHAPES = ("legacy", "raw", "wrapped", "wrapped_list")


def literal(field, identifier, shape, *, negative=False):
    if shape == "legacy":
        return {"field": field, "op": "none" if negative else "any", "values": [identifier]}
    operand = identifier if shape == "raw" else {"kind": "absolute", "value": [identifier] if shape == "wrapped_list" else identifier}
    return {"field": field, "op": "is_not" if negative else "is", "value": operand}


def audience(line):
    return {"where": {"conditions": [{"conditions": [line]}]}}


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("reference", ["foreign_service", "foreign_person", "wrong_record_kind", "wrong_tag_kind"])
def test_event_and_type_audience_guards_refuse_every_absolute_shape_and_sign(h, shape, reference):
    typ = event_type(h)
    if reference == "wrong_tag_kind":
        target = h.configure("tags", {"name": "Programme audience tag", "labels": ["programmes"]})["record"]["id"]
        field, code = "service_ids", "TAG_KIND"
    elif reference == "wrong_record_kind":
        target, field, code = h.seed["services"][0], "category_ids", "AUDIENCE_REFERENCE"
    else:
        with transaction(h.app.state.engine, write=True) as db:
            if reference == "foreign_person":
                org = m.Organisation(name="Foreign operand audience organisation", timezone="Asia/Riyadh", week_start=6)
                db.add(org)
                db.flush()
                row = m.Person(organisation_id=org.id, display_name="Foreign operand audience person")
                field = "person_ids"
            else:
                dept = m.Department(organisation_id=h.seed["organisation_id"], name="Foreign operand audience department")
                db.add(dept)
                db.flush()
                row = m.Service(department_id=dept.id, name="Foreign operand audience clinic", starts_on=date(2020, 1, 1))
                field = "service_ids"
            db.add(row)
            db.flush()
            target, code = row.id, "AUDIENCE_REFERENCE"
    for negative in (False, True):
        selected = audience(literal(field, target, shape, negative=negative))
        invalid_type = h.configure("event_types", {"name": "Rejected operand audience type", "starts_on": "2020-01-01",
            "default_audience": selected}, status=422)
        assert invalid_type["error"]["code"] == code
        invalid_event = h.configure("events", value(typ, audience=selected), role="coordinator", status=422)
        assert invalid_event["error"]["code"] == code


@pytest.fixture
def unlinked_recipients(h):
    with transaction(h.app.state.engine, write=True) as db:
        selected, member, unaffiliated = people = [m.Person(organisation_id=h.seed["organisation_id"],
            display_name=f"Operand email-only person {index}", work_email=f"operand-{index}@example.test") for index in range(3)]
        db.add_all(people)
        db.flush()
        db.add(m.Membership(department_id=h.seed["department_id"], person_id=member.id,
            category_id=h.seed["category"], starts_on=date(2020, 1, 1)))
        return {"selected": selected.id, "member": member.id, "unaffiliated": unaffiliated.id}


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("negative", [False, True])
def test_email_preview_includes_named_unlinked_people_without_widening_negative_audiences(h, unlinked_recipients, shape, negative):
    chosen = unlinked_recipients["selected"]
    selected = audience(literal("person_ids", chosen, shape, negative=negative))
    with transaction(h.app.state.engine) as db:
        expected = ({person.id for person in email_audiences.extra_people(db, h.seed["department_id"],
            {"everyone": True}, date(2026, 10, 4))} - {chosen}) if negative else {chosen}
        assert (unlinked_recipients["member"] in expected) == negative
        assert unlinked_recipients["unaffiliated"] not in expected
        assert {person.id for person in email_audiences.extra_people(db, h.seed["department_id"], selected,
            date(2026, 10, 4))} == expected
    event = h.configure("events", value(event_type(h), audience=selected), role="coordinator")["record"]
    result = h.get("coordinator", f"/api/events/{event['id']}/email-preview")
    assert {person["person_id"] for person in result["recipients"] + result["missing_addresses"]
            if person["identity_id"] is None} == expected
    assert not unlinked_recipients["unaffiliated"] in {person["person_id"] for person in result["recipients"]}


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("negative", [False, True])
def test_tag_name_reference_counts_and_rename_guard_follow_absolute_operands(h, shape, negative):
    tag = h.configure("tags", {"name": "Operand referenced team", "labels": ["people"]})["record"]
    chosen = literal("tags", tag["name"].upper(), shape, negative=negative)
    policy = h.policy({"kind": "min_rest", "hours": 11}, {"conditions": {"conditions": [chosen]}})
    model_tag = SimpleNamespace(id=tag["id"], name=tag["name"])
    assert tags.names_used(policy["definition"]) == {tag["name"].lower()}
    assert tags.lines_naming(policy["definition"], model_tag) == 1
    listed = next(row for row in h.get("coordinator", "/api/catalogues")["tags"] if row["id"] == tag["id"])
    assert listed["used_by"] == {"lines": 1, "rules": 1}
    refused = h.configure("tags", {"name": "Renamed operand team", "labels": ["people"]}, tag["id"], tag["revision"], status=409)
    assert refused["error"]["code"] == "TAG_NAME_IN_USE"


@pytest.mark.parametrize("shape", SHAPES)
def test_tag_id_references_count_once_and_native_selectors_keep_name_guards(shape):
    tag = SimpleNamespace(id="tag-operand-id", name="Operand team")
    definition = {"rule": {"lines": [{"who": {"conditions": [literal("person_ids", tag.id, shape)]},
        "where": {"conditions": [literal("tags", tag.name, shape, negative=True)]}}]}}
    assert tags.lines_naming(definition, tag) == 2
    assert tags.names_used(definition) == {tag.name.lower()}
    relative = {"scope": {"conditions": {"conditions": [{"field": "tags", "value": {"kind": "relative", "token": "me"}}]}}}
    assert tags.lines_naming(relative, tag) == 0 and tags.names_used(relative) == set()


@pytest.mark.parametrize("shape", SHAPES)
def test_policy_reference_guard_uses_shared_reader_for_models_and_flat_projection(h, shape):
    with transaction(h.app.state.engine) as db:
        viewer = actor(db, h)
        policies.condition_references(db, viewer, s.Conditions.model_validate({"conditions": [
            literal("service_ids", h.seed["services"][0], shape)]}))
        policies.condition_references(db, viewer, SimpleNamespace(conditions=[SimpleNamespace(
            field="service_ids", values=[h.seed["services"][0]])]))
        with pytest.raises(Problem) as refused:
            policies.condition_references(db, viewer, s.Conditions.model_validate({"conditions": [
                literal("category_ids", h.seed["services"][0], shape, negative=True)]}))
        assert refused.value.status == 404

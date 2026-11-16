"""Creation-owned links share the record ceiling with their parent and checked Undo."""
import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk import preview
from .test_bulk_nested_limits import limits
from .test_rotation_attachments import rotation_value


REASON = "Count creation-owned related records"


def creation(h, kind):
    if kind.startswith("person"):
        name = "Counted initial team"
        if kind != "person_new_tag":
            h.configure("tags", {"name": name, "labels": ["services"] if kind == "person_adds_label" else ["people"]})
        return "people", {"display_name": "Synthetic counted colleague", "tags": [name]}, None
    if kind == "tag_members":
        return "tags", {"name": "Counted initial team", "labels": ["people", "services"]}, {"add": [
            {"kind": "people", "id": h.seed["people"][0]}, {"kind": "services", "id": h.seed["services"][0]}]}
    value = rotation_value(h)
    if kind == "rotation_adopted":
        h.configure("mentorships", {key: value[key] for key in ("person_id", "mentor_id", "starts_on", "ends_on")}
            | {"service_ids": [value["service_id"]], "strength": "required"}, role="coordinator")
    return "rotations", value, None


def counts(h):
    with transaction(h.app.state.engine) as db:
        return [db.scalar(select(func.count()).select_from(cls)) for cls in (m.Person, m.Tag, m.TagMember, m.Rotation,
            m.RotationAttachment, m.Mentorship, m.DefinitionHistory, m.AuditEvent, m.CommandReceipt)]


@pytest.mark.parametrize("kind, expected, undo_expected", [
    ("person_new_tag", 3, 2), ("person_existing_tag", 2, 2), ("person_adds_label", 3, 2),
    ("tag_members", 4, 4), ("rotation_owned", 3, 3), ("rotation_adopted", 2, 2)])
@pytest.mark.parametrize("at_limit", [False, True])
def test_bulk_creation_counts_owned_links_and_undo_uses_its_actual_record_limit(h, kind, expected, undo_expected, at_limit):
    catalogue, value, members = creation(h, kind)
    limits(h, expected if at_limit else expected - 1)
    body = {"item_kind": "catalogue.create", "items": [{"payload": {"catalogue": catalogue, "value": value,
            **({"members": members} if members else {})}}], "reason": REASON}
    h.client("coordinator")
    before = counts(h)
    checked = preview(h, "coordinator", body)
    assert counts(h) == before
    if not at_limit:
        assert checked["items"][0]["issues"][0]["code"] == "BULK_ITEM_LIMIT"
        assert h.command("coordinator", "bulk.apply", body, status=409)["error"]["code"] == "BULK_ITEM_LIMIT"
        assert counts(h) == before
        return
    assert checked["counts"]["problem"] == 0
    applied = h.command("coordinator", "bulk.apply", body)
    assert applied["items"][0]["results"][0]["affected_count"] == expected
    row = applied["items"][0]["record"]
    undo = applied["undo"]
    limits(h, undo_expected - 1)
    before_undo = counts(h)
    checked = preview(h, "coordinator", undo["payload"])
    assert checked["items"][0]["issues"][0]["code"] == "BULK_ITEM_LIMIT"
    assert h.command("coordinator", **undo, status=422)["error"]["code"] == "BULK_ITEM_LIMIT"
    assert counts(h) == before_undo
    limits(h, undo_expected)
    assert preview(h, "coordinator", undo["payload"])["counts"]["problem"] == 0
    undone = h.command("coordinator", **undo)
    assert undone["items"][0]["results"][0]["affected_count"] == undo_expected
    with transaction(h.app.state.engine) as db:
        cls = {"people": m.Person, "tags": m.Tag, "rotations": m.Rotation}[catalogue]
        assert db.get(cls, row["id"]) is None
        if catalogue == "rotations":
            assert db.get(m.RotationAttachment, row["id"]) is None
            assert db.scalar(select(func.count()).select_from(m.Mentorship)) == before[5]


@pytest.mark.parametrize('maximum', [2, 3])
def test_nested_catalogue_update_keeps_its_handlers_related_record_count(h, maximum):
    person = h.configure('people', {'display_name': 'Synthetic counted existing colleague'}, role='coordinator')['record']
    limits(h, maximum)
    body = {'item_kind': 'catalogue.bulk_update', 'items': [{'payload': {'catalogue': 'people',
        'items': [{'id': person['id'], 'revision': person['revision']}], 'set': {'tags': ['Counted nested team']}}}],
        'reason': REASON}
    before = counts(h)
    checked = preview(h, 'coordinator', body)
    assert counts(h) == before
    if maximum == 2:
        assert checked['items'][0]['issues'][0]['code'] == 'BULK_ITEM_LIMIT'
        assert h.command('coordinator', 'bulk.apply', body, status=409)['error']['code'] == 'BULK_ITEM_LIMIT'
        assert counts(h) == before
    else:
        assert checked['counts']['problem'] == 0
        applied = h.command('coordinator', 'bulk.apply', body)
        assert applied['items'][0]['results'][0]['affected_count'] == 3
        h.command('coordinator', **applied['undo'])
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Person, person['id']).tags == []

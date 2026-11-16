"""Rotation creation Undo removes owned links without erasing independent records."""
from datetime import date, datetime

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from .test_bulk import preview
from .test_bulk_single_commands import undo as undo_command
from .test_rotation_attachments import rotation_value
from .governance_support import identity


REASON = "Synthetic rotation creation Undo"


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def rotation_h(request):
    return request.getfixturevalue(request.param)


def create_rotation(h, value, bulk):
    if bulk:
        result = h.command("coordinator", "bulk.apply", {"item_kind": "catalogue.create",
            "common": {"catalogue": "rotations"}, "items": [{"payload": {"value": value}}], "reason": REASON})
        return result["items"][0]["record"], result["undo"]
    row = h.configure("rotations", value, role="coordinator")["record"]
    return row, {"kind": "catalogue.undo_create", "target_id": row["id"], "expected_revision": row["revision"],
                 "payload": {"catalogue": "rotations", "reason": REASON}}


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("attachment_kind", ["owned", "adopted", "edited", "shared"])
def test_rotation_creation_undo_allows_owned_link_and_preserves_independent_attachment(rotation_h, bulk, attachment_kind):
    h = rotation_h
    value = rotation_value(h)
    if attachment_kind == "adopted":
        h.configure("mentorships", {key: value[key] for key in ("person_id", "mentor_id", "starts_on", "ends_on")}
            | {"service_ids": [value["service_id"]], "strength": "required"}, role="coordinator")
    row, undo = create_rotation(h, value, bulk)
    with transaction(h.app.state.engine, write=True) as db:
        link = db.get(m.RotationAttachment, row["id"])
        attachment_id = link.mentorship_id
        if attachment_kind == "shared":
            # Historical rotations can share an attachment; its lifecycle must retain the other link.
            other = m.Rotation(department_id=h.seed["department_id"], **{key: val for key, val in value.items()
                if key not in {"mentor_id", "starts_on", "ends_on"}}, starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1))
            db.add(other)
            db.flush()
            other_id = other.id
            db.add(m.RotationAttachment(rotation_id=other.id, mentorship_id=attachment_id,
                                       synced_revision=link.synced_revision))
    if attachment_kind == "edited":
        attachment = next(row for row in h.get("coordinator", "/api/catalogues")["mentorships"] if row["id"] == attachment_id)
        fields = ("person_id", "mentor_id", "programme_id", "starts_on", "ends_on", "assignment_type_ids", "service_ids")
        h.configure("mentorships", {key: attachment[key] for key in fields} | {"strength": "required"},
                    attachment_id, attachment["revision"], role="coordinator")
    with transaction(h.app.state.engine) as db:
        before = record(db.get(m.Mentorship, attachment_id))
    if bulk:
        assert preview(h, "coordinator", undo["payload"])["counts"]["problem"] == 0
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Rotation, row["id"]) is not None
            assert db.get(m.RotationAttachment, row["id"]) is not None
            assert record(db.get(m.Mentorship, attachment_id)) == before
    undo_command(h, "coordinator", undo)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Rotation, row["id"]) is None
        assert db.get(m.RotationAttachment, row["id"]) is None
        attachment = db.get(m.Mentorship, attachment_id)
        if attachment_kind == "owned":
            assert attachment is None
            assert db.scalar(select(m.DefinitionHistory.id).where(m.DefinitionHistory.object_id == attachment_id,
                m.DefinitionHistory.object_revision == 2)) is not None
        else:
            assert record(attachment) == before
        if attachment_kind == "shared":
            assert db.get(m.RotationAttachment, other_id).mentorship_id == attachment_id


@pytest.mark.parametrize("bulk", [False, True])
def test_rotation_creation_undo_keeps_owned_attachment_when_independent_reference_refuses(rotation_h, bulk):
    h = rotation_h
    row, undo = create_rotation(h, rotation_value(h), bulk)
    with transaction(h.app.state.engine, write=True) as db:
        attachment_id = db.get(m.RotationAttachment, row["id"]).mentorship_id
        reference = m.RequestRecord(department_id=h.seed["department_id"], person_id=row["person_id"],
            kind="rotation_change", rotation_id=row["id"], start=datetime.fromisoformat("2026-10-01T00:00:00+03:00"),
            end=datetime.fromisoformat("2026-11-01T00:00:00+03:00"), private_reason="Synthetic change request",
            created_by=h.seed["identities"]["staff"])
        db.add(reference)
        db.flush()
        reference_id = reference.id
    if bulk:
        check = preview(h, "coordinator", undo["payload"])
        assert check["items"][0]["issues"][0]["code"] == "INTEGRITY_CONFLICT"
    assert undo_command(h, "coordinator", undo, status=409)["error"]["code"] == "INTEGRITY_CONFLICT"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Rotation, row["id"]).revision == row["revision"]
        assert db.get(m.RotationAttachment, row["id"]).mentorship_id == attachment_id
        assert db.get(m.Mentorship, attachment_id).revision == 1
        assert db.get(m.RequestRecord, reference_id).rotation_id == row["id"]


def test_programme_creation_undo_authorises_hidden_source_before_revision(h):
    hidden = h.configure("programmes", {"name": "Synthetic hidden programme", "starts_on": "2026-01-01"})["record"]
    identity(h, "unrelated_lead", ["programme_lead"])
    for revision in (hidden["revision"], hidden["revision"] + 20):
        responses = [h.command("unrelated_lead", "catalogue.undo_create", {"catalogue": "programmes", "reason": REASON},
                               hidden["id"], revision, status=403),
                     h.command("unrelated_lead", "catalogue.bulk_update", {"catalogue": "programmes", "active": False,
                        "items": [{"id": hidden["id"], "revision": revision}], "reason": "Undo"}, status=403)]
        for response in responses:
            assert response["error"]["code"] == "FORBIDDEN"
            assert response["error"]["details"] is None
            assert hidden["name"] not in str(response)

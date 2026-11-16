"""An irreversible bulk operation still shares the affected-record ceiling."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import digest
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services.common import materialise_defaults
from .test_bulk import REASON, preview
from .test_bulk_nested_limits import limits
from .test_bulk_pending_privacy import PRIVATE_TYPES, private_row


def setup(h, kind):
    person = h.configure("people", {"display_name": "Synthetic bounded privacy owner", "active": False,
        **({"tags": ["Synthetic private team"]} if kind == "tags" else {})})["record"]
    refs, affected = [(m.Person, person["id"])], 1
    with transaction(h.app.state.engine, write=True) as db:
        rows = []
        if kind == "tags":
            refs.extend((m.TagMember, row.id) for row in db.scalars(select(m.TagMember).where(
                m.TagMember.kind == "people", m.TagMember.member_id == person["id"])))
        if kind == "private":
            rows = [private_row(h, cls, person["id"]) for cls, _ in PRIVATE_TYPES]
        elif kind == "identity":
            identity = m.Identity(issuer="synthetic-limit", subject=m.uid(), display_name="Synthetic private owner",
                                  person_id=person["id"])
            materialise_defaults(identity)
            db.add(identity); db.flush()
            rows = [identity, m.Account(identity_id=identity.id, username="synthetic-limit-owner", email="private@example.invalid"),
                m.Session(identity_id=identity.id, department_id=h.seed["department_id"], token_hash=digest(m.uid()),
                    csrf_hash=digest(m.uid()), expires_at=m.now() + timedelta(days=1))]
        elif kind == "feed":
            rows = [m.CalendarFeed(department_id=h.seed["department_id"], identity_id=h.seed["identities"]["admin"],
                person_id=person["id"], token_hash=digest(m.uid()))]
        elif kind == "delivery":
            event = m.Outbox(department_id=h.seed["department_id"], event_key=m.uid(), kind="email", payload={})
            db.add(event); db.flush()
            rows = [m.EmailDelivery(department_id=h.seed["department_id"], outbox_id=event.id, source_outbox_id=event.id,
                person_id=person["id"], sender_mode="smtp", address="private@example.invalid")]
        for row in rows:
            materialise_defaults(row)
            db.add(row)
            refs.append((type(row), row.id))
        affected += len(rows)
    if kind == "tags":
        affected += 1
    if kind == "stage":
        stage = h.command("admin", "bulk.stage", {"payload": {"item_kind": "catalogue.update",
            "common": {"catalogue": "people"}, "items": [{"target_id": person["id"],
                "expected_revision": person["revision"], "payload": {"value": {"display_name": "Synthetic staged detail"}}}],
            "reason": REASON}})["staged"]
        refs.append((m.BulkStaged, stage["id"]))
        affected += 1
    return person, refs, affected


def states(h, refs):
    with transaction(h.app.state.engine) as db:
        return [record(row) if (row := db.get(cls, ident)) is not None else None for cls, ident in refs]


@pytest.mark.parametrize("kind", ["tags", "private", "identity", "feed", "delivery", "stage"])
def test_bulk_anonymise_counts_owned_records_even_without_an_inverse(h, kind):
    person, refs, affected = setup(h, kind)
    body = {"item_kind": "person.anonymise", "items": [{"target_id": person["id"],
        "expected_revision": person["revision"]}], "reason": REASON}
    before = states(h, refs)
    limits(h, affected - 1)
    checked = preview(h, "admin", body)
    assert checked["counts"]["problem"] == 1
    assert checked["items"][0]["issues"][0]["code"] == "BULK_ITEM_LIMIT"
    refused = h.command("admin", "bulk.apply", body, status=422)
    assert refused["error"]["code"] == "BULK_ITEM_LIMIT"
    assert states(h, refs) == before
    limits(h, affected)
    assert preview(h, "admin", body)["counts"]["problem"] == 0
    assert states(h, refs) == before
    applied = h.command("admin", "bulk.apply", body)
    assert applied["undo"] is None and applied["undoable"] is False
    assert states(h, refs)[0]["display_name"].startswith("Former staff member ")

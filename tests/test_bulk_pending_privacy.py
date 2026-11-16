"""Anonymisation sees the same personal data before and after a bulk flush."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import Actor, digest
from apps.api.db import transaction
from apps.api.services import leavers
from apps.api.services.common import materialise_defaults
from packages.contracts import schemas as s
from .test_bulk import REASON, preview


SECRET = "Synthetic confidential personal note"
DATABASES = ["h", pytest.param("pg", marks=pytest.mark.postgres)]
PRIVATE_TYPES = [(m.Availability, "private_reason"), (m.RequestRecord, "private_reason"),
                 (m.AvailabilityPattern, "note"), (m.StaffWish, "note")]


def actor(h):
    return Actor(h.seed["identities"]["admin"], h.seed["department_id"], h.seed["organisation_id"],
                 "Synthetic admin", frozenset({"admin"}), None, "synthetic-session")


def anonymise(db, h, person):
    return leavers.anonymise(db, actor(h), s.Command(kind="person.anonymise", target_id=person.id,
        expected_revision=person.revision, payload={"reason": REASON}), h.settings)


def private_row(h, model, person_id, value=SECRET):
    common = {"department_id": h.seed["department_id"], "person_id": person_id}
    creator = h.seed["identities"]["coordinator"]
    if model is m.Availability:
        row = model(**common, start=m.now(), end=m.now() + timedelta(hours=1), kind="recorded_leave",
                    private_reason=value, recorded_by=creator)
    elif model is m.RequestRecord:
        row = model(**common, start=m.now(), end=m.now() + timedelta(hours=1),
                    private_reason=value, created_by=creator)
    elif model is m.AvailabilityPattern:
        row = model(**common, weekdays=[0], start_time="08:00", end_time="09:00", starts_on=date(2026, 10, 4),
                    note=value, status="active", created_by=creator)
    else:
        row = model(**common, first_day=date(2026, 10, 4), last_day=date(2026, 10, 4),
                    kind="prefer_off", points=1, note=value, created_by=creator)
    materialise_defaults(row)
    return row


@pytest.mark.parametrize("database", DATABASES)
def test_bulk_wish_create_then_anonymise_scrubs_pending_private_note(request, database):
    h = request.getfixturevalue(database)
    person = h.configure("people", {"display_name": "Synthetic inactive preference owner", "active": False})["record"]
    payload = {"items": [
        {"item_kind": "wish.create", "payload": {"person_id": person["id"], "first_day": "2026-10-04",
            "last_day": "2026-10-04", "kind": "prefer_off", "points": 1, "note": SECRET}},
        {"item_kind": "person.anonymise", "target_id": person["id"],
         "expected_revision": person["revision"], "payload": {}}], "reason": REASON}
    checked = preview(h, "admin", payload)
    assert checked["counts"]["problem"] == 0
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.StaffWish.id).where(m.StaffWish.person_id == person["id"])) is None
        assert db.get(m.Person, person["id"]).display_name == person["display_name"]
    h.command("admin", "bulk.apply", payload)
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.StaffWish.note).where(m.StaffWish.person_id == person["id"])) == ""


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("model,column", PRIVATE_TYPES, ids=[model.__tablename__ for model, _ in PRIVATE_TYPES])
@pytest.mark.parametrize("state", ["pending", "dirty", "reassigned"])
def test_anonymise_scrubs_current_unit_of_work_private_records(request, database, model, column, state):
    h = request.getfixturevalue(database)
    person = h.configure("people", {"display_name": "Synthetic private record owner", "active": False})["record"]
    other_person = h.seed["people"][1]
    with transaction(h.app.state.engine, write=True) as db:
        row = private_row(h, model, other_person if state == "reassigned" else person["id"],
                          "" if state == "dirty" else SECRET)
        untouched = private_row(h, model, other_person)
        moved_away = private_row(h, model, person["id"])
        db.add_all([untouched, moved_away])
        if state != "pending":
            db.add(row)
        db.flush()
        if state == "pending":
            db.add(row)
        elif state == "dirty":
            setattr(row, column, SECRET)
        else:
            row.person_id = person["id"]
        # A dirty row moved away must follow its current owner, not the SQL predicate's old owner.
        moved_away.person_id = other_person
        with db.no_autoflush:
            db.info["bulk"] = {"inputs_changed": False, "availability_changed": False, "history": []}
            result = anonymise(db, h, db.get(m.Person, person["id"]))
            assert result["scrubbed"][model.__tablename__] == 1
            assert getattr(row, column) == ""
            assert getattr(untouched, column) == getattr(moved_away, column) == SECRET
        row_id, untouched_id, moved_id = row.id, untouched.id, moved_away.id
    with transaction(h.app.state.engine) as db:
        assert getattr(db.get(model, row_id), column) == ""
        assert getattr(db.get(model, untouched_id), column) == getattr(db.get(model, moved_id), column) == SECRET


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("state", ["pending", "dirty"])
def test_anonymise_revokes_current_sign_ins_sessions_and_feeds(request, database, state):
    h = request.getfixturevalue(database)
    person = h.configure("people", {"display_name": "Synthetic account owner", "active": False})["record"]
    other_person = h.seed["people"][1]
    with transaction(h.app.state.engine, write=True) as db:
        identity = m.Identity(issuer="synthetic-private", subject=m.uid(), display_name=SECRET,
                              person_id=other_person if state == "dirty" else person["id"])
        materialise_defaults(identity)
        db.add(identity); db.flush()
        account = m.Account(identity_id=identity.id, username="synthetic-private-account", email="synthetic@example.invalid")
        session = m.Session(identity_id=identity.id, department_id=h.seed["department_id"],
            token_hash=digest(m.uid()), csrf_hash=digest(m.uid()), expires_at=m.now(), expiry_unlimited=True,
            device_label="Synthetic browser", network_label="Synthetic network")
        feed = m.CalendarFeed(department_id=h.seed["department_id"], identity_id=identity.id,
            person_id=other_person if state == "dirty" else person["id"], token_hash=digest(m.uid()),
            revoked=state == "dirty")
        for row in (account, session, feed):
            materialise_defaults(row)
        db.add_all([account, session, feed])
        if state == "dirty":
            db.flush()
            identity.person_id, feed.person_id, feed.revoked = person["id"], person["id"], False
        with db.no_autoflush:
            db.info["bulk"] = {"inputs_changed": False, "availability_changed": False, "history": []}
            before = m.now()
            result = anonymise(db, h, db.get(m.Person, person["id"]))
            assert identity.disabled and identity.display_name == result["record"]["display_name"]
            assert account.email == "" and account.username == f"former-{identity.id[:8]}"
            assert session.revoked and not session.expiry_unlimited and before <= session.expires_at <= m.now()
            assert feed.revoked and result["scrubbed"]["calendar_feeds"] == 1
        identity_id, session_id, feed_id = identity.id, session.id, feed.id
    with transaction(h.app.state.engine) as db:
        identity = db.get(m.Identity, identity_id)
        account = db.scalar(select(m.Account).where(m.Account.identity_id == identity_id))
        assert identity.disabled
        assert identity.subject == identity.subject_normalized == f"former:{identity_id}"
        assert account.username_normalized == account.username == f"former-{identity_id[:8]}"
        assert account.email_normalized == account.email == ""
        assert db.get(m.Session, session_id).revoked and db.get(m.CalendarFeed, feed_id).revoked
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "auth.session_revoke",
                                                   m.AuditEvent.target_id == session_id))
        assert audit.detail["person_id"] == person["id"]


@pytest.mark.parametrize("database", DATABASES)
def test_anonymise_disables_pending_identity(request, database):
    h = request.getfixturevalue(database)
    person = h.configure("people", {"display_name": "Synthetic pending sign-in owner", "active": False})["record"]
    with transaction(h.app.state.engine, write=True) as db:
        identity = m.Identity(issuer="synthetic-private", subject=m.uid(), display_name=SECRET, person_id=person["id"])
        db.add(identity)
        with db.no_autoflush:
            db.info["bulk"] = {"inputs_changed": False, "availability_changed": False, "history": []}
            result = anonymise(db, h, db.get(m.Person, person["id"]))
            assert identity.disabled and identity.display_name == result["record"]["display_name"]
            assert identity.revision == 2
        identity_id = identity.id
    with transaction(h.app.state.engine) as db:
        identity = db.get(m.Identity, identity_id)
        assert identity.subject == identity.subject_normalized == f"former:{identity_id}"


@pytest.mark.parametrize("database", DATABASES)
def test_anonymise_scrubs_pending_and_queued_availability_history_only(request, database):
    h = request.getfixturevalue(database)
    person = h.configure("people", {"display_name": "Synthetic history owner", "active": False})["record"]
    with transaction(h.app.state.engine, write=True) as db:
        availability = private_row(h, m.Availability, person["id"])
        unrelated = private_row(h, m.Availability, h.seed["people"][1])
        versions = [m.DefinitionHistory(department_id=h.seed["department_id"], kind="availability.update",
            object_id=row.id, object_revision=1, snapshot={"private_reason": SECRET, "kind": "recorded_leave"},
            actor_id=h.seed["identities"]["coordinator"], reason=REASON) for row in (availability, unrelated)]
        db.add_all([availability, unrelated, *versions])
        queued = {"private_reason": SECRET, "kind": "recorded_leave"}
        with db.no_autoflush:
            db.info["bulk"] = {"inputs_changed": False, "availability_changed": False,
                "history": [("availability.restore", availability, REASON, queued)]}
            result = anonymise(db, h, db.get(m.Person, person["id"]))
            assert result["scrubbed"]["availability_history"] == 2
            assert versions[0].snapshot == queued == {"kind": "recorded_leave"}
            assert versions[1].snapshot["private_reason"] == SECRET


def test_anonymise_does_not_reintroduce_deleted_private_records(h):
    person = h.configure("people", {"display_name": "Synthetic deleted preference owner", "active": False})["record"]
    with transaction(h.app.state.engine, write=True) as db:
        row = private_row(h, m.StaffWish, person["id"])
        db.add(row); db.flush(); db.delete(row)
        with db.no_autoflush:
            db.info["bulk"] = {"inputs_changed": False, "availability_changed": False, "history": []}
            result = anonymise(db, h, db.get(m.Person, person["id"]))
            assert result["scrubbed"]["staff_wishes"] == 0 and row in db.deleted
    with transaction(h.app.state.engine) as db:
        assert db.get(m.StaffWish, row.id) is None

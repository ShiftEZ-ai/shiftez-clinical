"""person.anonymise: a leaver's personal details are removed; rotas, publications and the audit log stay intact."""
import json
from datetime import date, timedelta
import pytest
from sqlalchemy import select
from apps.api import models as m
from apps.api.auth import CATALOGUE_READ_ROLES
from apps.api.db import transaction
from apps.api.services import leavers
from packages.contracts.schemas import DEFAULT_CONTACT_TYPES, INSTALLATION_ADMIN_ROLE, PersonInput, ROLE_NAMES
from tests.governance_support import identity
from tests.test_accounts import invite, users_file, with_users_file

FIELDS = ("display_name", "title", "directory_section", "staff_number")
CONTACT_VALUES = {
    "work_email": "leaver@example.test",
    "work_phone": "+966 11 000 0000",
    "work_extension": "1234",
    "pager": "4321",
}


def set_contact(h, person_id, legacy_field, value):
    assert legacy_field in leavers.ANONYMISED_FIELDS
    contact_type = next(row for row in DEFAULT_CONTACT_TYPES if row["legacy_field"] == legacy_field)
    return h.command("admin", "person.contact.set", {
        "person_id": person_id, "contact_type_id": contact_type["id"], "value": value,
        "reason": "Synthetic leaver contact",
    })


@pytest.mark.parametrize("backend", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("role", tuple(role for role in ROLE_NAMES if role != "admin"))
def test_anonymised_leaver_is_absent_from_other_directory_and_people_reads(request, role, backend):
    h = request.getfixturevalue(backend)
    if role not in h.seed["identities"]:
        identity(h, role, [role])
    original = {
        "display_name": "Fictional leaver with private details",
        "given_name": "Fictional", "family_name": "Leaver", "title": "Private title",
        "staff_number": "PRIVATE-LEAVER", "directory_section": "Private section",
        "short_code": "PLV", "rota_name": "Private rota", "colour": "blue",
    }
    person = h.configure("people", original)["record"]
    for field, value in CONTACT_VALUES.items():
        set_contact(h, person["id"], field, value)
    person = next(row for row in h.get("admin", "/api/catalogues")["people"] if row["id"] == person["id"])
    person = h.configure("people", {"display_name": person["display_name"], "active": False},
                         person["id"], person["revision"])["record"]
    h.command("admin", "person.anonymise", {"reason": "Remove synthetic leaver details"}, person["id"], person["revision"])

    directory = h.get(role, "/api/directory")["people"]
    directory_text = json.dumps(directory)
    assert person["id"] not in {row["id"] for row in directory}
    assert all(value not in directory_text for value in (*original.values(), *CONTACT_VALUES.values()))

    if role in CATALOGUE_READ_ROLES:
        row = next(row for row in h.get(role, "/api/catalogues?include=people")["people"] if row["id"] == person["id"])
        assert row["display_name"] == f"Former staff member {person['id'][:8]}"
        assert all(row.get(field, "") == "" for field in leavers.ANONYMISED_FIELDS)
        assert all(value not in json.dumps(row) for value in (*original.values(), *CONTACT_VALUES.values()))

    admin_row = next(row for row in h.get("admin", "/api/catalogues")["people"] if row["id"] == person["id"])
    assert admin_row["display_name"] == f"Former staff member {person['id'][:8]}"
    assert all(admin_row.get(field, "") == "" for field in leavers.ANONYMISED_FIELDS)


def test_anonymising_a_leaver_preserves_the_last_installation_admin(h):
    person_id = h.seed["people"][0]
    grant = {"role": INSTALLATION_ADMIN_ROLE, "starts_on": "2020-01-01", "reason": "Synthetic installation grant"}
    h.command("operator", "role.grant", {**grant, "identity_id": h.seed["identities"]["staff"]})
    other = h.command("operator", "role.grant", {**grant, "identity_id": h.seed["identities"]["coordinator"]})["record"]
    person = next(p for p in h.get("admin", "/api/catalogues")["people"] if p["id"] == person_id)
    person = h.configure("people", {"display_name": person["display_name"], "active": False},
                         person_id, person["revision"])["record"]
    reason = {"reason": "Remove synthetic leaver details"}
    refused = h.command("admin", "person.anonymise", reason, person_id, person["revision"], status=403)
    assert refused["error"]["code"] == "FORBIDDEN"

    h.command("operator", "role.revoke", {"reason": "End synthetic access"}, other["id"], other["revision"])
    h.command("operator", "role.grant", {**grant, "identity_id": h.seed["identities"]["admin"],
        "ends_on": (date.today() + timedelta(days=30)).isoformat()})
    refused = h.command("admin", "person.anonymise", reason, person_id, person["revision"], status=409)
    assert refused["error"]["code"] == "LAST_INSTALLATION_ADMIN"
    assert h.get("staff", "/api/admin/branding")["can_edit"]


def test_anonymise_a_leaver(h, tmp_path):
    acc = with_users_file(h, tmp_path)
    person_id = acc.get("staff", "/api/me")["person_id"]
    waiting = {"person_id": person_id, "kind": "meeting", "start": "2026-10-07T08:00:00+03:00",
               "end": "2026-10-07T12:00:00+03:00", "private_reason": "Synthetic previous request note"}
    request = acc.command("staff", "request.create", waiting)["record"]
    acc.command("staff", "request.edit", {**waiting, "private_reason": "Synthetic current request note"},
                request["id"], request["revision"])
    with transaction(acc.app.state.engine) as db:
        assert db.get(m.RequestRecord, request["id"]).private_undo_note == waiting["private_reason"]
    acc.command("coordinator", "availability.record", {"person_id": person_id, "kind": "recorded_leave", "start": "2026-10-06T08:00:00+03:00",
                                                       "end": "2026-10-06T12:00:00+03:00", "private_reason": "Hospital appointment (test)"})
    feed = acc.command("staff", "calendar_feed.create", {})["path"]
    account = invite(acc, "leaving.person")
    with transaction(acc.app.state.engine, write=True) as db:
        db.get(m.Identity, account["record"]["id"]).person_id = person_id
    published = acc.publish(acc.shift(acc.workspace(), people=[person_id])["workspace"])
    person = next(p for p in acc.get("admin", "/api/catalogues")["people"] if p["id"] == person_id)
    for field, value in CONTACT_VALUES.items():
        set_contact(acc, person_id, field, value)
    person = next(p for p in acc.get("admin", "/api/catalogues")["people"] if p["id"] == person_id)

    refused = acc.command("admin", "person.anonymise", {"reason": "Left in 2026"}, person_id, person["revision"], status=409)
    assert refused["error"]["code"] == "PERSON_ACTIVE"
    person = acc.configure("people", {**{k: person[k] for k in FIELDS}, "active": False, "tags": ["Night"],
        "given_name": "Leaver", "family_name": "Example", "short_code": "LVR", "rota_name": "Private rota",
        "colour": "blue"}, person_id, person["revision"])["record"]
    acc.command("coordinator", "person.anonymise", {"reason": "Left in 2026"}, person_id, person["revision"], status=403)

    result = acc.command("admin", "person.anonymise", {"reason": "Left the department in 2026; details removed"}, person_id, person["revision"])
    assert result["scrubbed"]["availability"] == 1 and result["scrubbed"]["calendar_feeds"] == 1
    record = result["record"]
    assert record["display_name"] == f"Former staff member {person_id[:8]}"
    assert all(record[k] == "" for k in ("given_name", "family_name", "title", "work_email", "staff_number",
                                          "work_phone", "work_extension", "pager", "directory_section", "short_code",
                                          "rota_name", "colour"))
    assert "leaving.person" not in users_file(acc)  # sign-in entry (and its password hash) gone
    assert acc.client("staff").get("/api/me").status_code == 401  # sessions ended, sign-in disabled
    from fastapi.testclient import TestClient
    assert TestClient(acc.app, base_url=acc.settings.public_origin).get(feed).status_code != 200
    with transaction(acc.app.state.engine) as db:
        assert db.scalar(select(m.Availability.private_reason).where(m.Availability.person_id == person_id)) == ""
        request_row = db.get(m.RequestRecord, request["id"])
        assert request_row.private_reason == "" and request_row.private_undo_note is None
        identity = db.get(m.Identity, account["record"]["id"])
        assert identity.disabled and identity.display_name == record["display_name"]
        assert db.scalar(select(m.Account.email).where(m.Account.identity_id == identity.id)) == ""
        # Immutable by design: the publication still holds what was published; the audit records why, not old values.
        assert db.get(m.Publication, published["publication_id"]) is not None
        assert db.scalar(select(m.TagMember.id).where(m.TagMember.member_id == person_id)) is None  # out of their tags
        event = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "person.anonymise")).one()
        assert event.reason == "Left the department in 2026; details removed" and "Hospital appointment" not in json.dumps(event.detail)


def test_anonymisation_refreshes_cached_published_contacts_without_changing_publication(h):
    person = next(row for row in h.get('admin', '/api/catalogues')['people']
                  if row['id'] == h.seed['people'][0])
    contact_fields = {typ['legacy_field'] for typ in DEFAULT_CONTACT_TYPES}
    value = {key: person[key] for key in PersonInput.model_fields if key not in contact_fields}
    value.update({'display_name': 'Fictional leaving clinician', 'short_code': 'LVR'})
    person = h.configure('people', value, person['id'], person['revision'])['record']
    set_contact(h, person['id'], 'pager', '4321')
    person = next(row for row in h.get('admin', '/api/catalogues')['people'] if row['id'] == person['id'])
    published = h.publish(h.shift(h.workspace(), people=[person['id']])['workspace'])
    person = h.configure('people', {**value, 'active': False}, person['id'], person['revision'])['record']
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, published['publication_id'])
        issued_snapshot, issued_check = publication.snapshot, publication.validation

    client = h.client('coordinator')
    path = '/api/schedule/sessions?start=2026-10-01&end=2026-11-01'
    cached = client.get(path)
    assert cached.json()['people'][person['id']]['pager'] == '4321'
    reason = 'Remove fictional leaver contact details'
    result = h.command('admin', 'person.anonymise', {'reason': reason}, person['id'], person['revision'])

    fresh = client.get(path, headers={'if-none-match': cached.headers['etag']})
    assert fresh.status_code == 200 and fresh.headers['etag'] != cached.headers['etag']
    contact = fresh.json()['people'][person['id']]
    assert contact['display_name'] == result['record']['display_name'] == f"Former staff member {person['id'][:8]}"
    assert contact.get('pager', '') == contact.get('short_code', '') == ''
    assert client.get(path, headers={'if-none-match': fresh.headers['etag']}).status_code == 304
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, published['publication_id'])
        assert publication.snapshot == issued_snapshot and publication.validation == issued_check
        event = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'person.anonymise')).one()
        assert event.reason == reason


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('has_identity', [True, False])
def test_anonymisation_redacts_delivery_addresses_and_prevents_pending_sends(request, backend, has_identity, monkeypatch):
    from apps.api.serialization import record
    from apps.worker import email_delivery
    from .test_email_delivery import configuration, dispatch, no_account_person, queued, source

    h = request.getfixturevalue(backend)
    configuration(h)
    with transaction(h.app.state.engine) as db:
        person_id = db.get(m.Identity, h.seed['identities']['staff']).person_id
    if not has_identity:
        person_id = no_account_person(h)
    before = {}
    for state in ('queued', 'sent', 'failed', 'draft'):
        content_id, parent_id = source(h, audience={'person_ids': [person_id]})
        assert queued(h, parent_id) == 1
        with transaction(h.app.state.engine, write=True) as db:
            delivery = db.scalars(select(m.EmailDelivery).where(m.EmailDelivery.source_outbox_id == parent_id)).one()
            delivery.state = state
            delivery.sent_at = m.now() if state == 'sent' else None
            delivery.error_code = None if state in {'queued', 'sent'} else 'SYNTHETIC_DELIVERY_FAILURE'
            if has_identity and state == 'sent':
                delivery.person_id = None  # Older evidence may link the recipient only through their identity.
            child = db.get(m.Outbox, delivery.outbox_id)
            child.state = 'failed' if state == 'draft' else state
            db.flush()
            before[delivery.id] = record(delivery)
    other_person_id = no_account_person(h, email_address='unrelated@example.test')
    with transaction(h.app.state.engine, write=True) as db:
        other = m.EmailDelivery(department_id=h.seed['department_id'], source_outbox_id=parent_id,
            outbox_id=m.uid(), person_id=other_person_id, sender_mode='smtp', address='unrelated@example.test', state='sent')
        db.add(m.Outbox(id=other.outbox_id, department_id=h.seed['department_id'], event_key=m.uid(),
            kind='email', payload={'source_outbox_id': parent_id}, state='sent'))
        db.flush()
        db.add(other)
        person = db.get(m.Person, person_id)
        person.active = False
        revision = person.revision
    result = h.command('admin', 'person.anonymise', {'reason': 'Remove synthetic former recipient details'}, person_id, revision)
    assert result['scrubbed']['email_deliveries'] == 4
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person_id).work_email == ''
        for delivery_id, previous in before.items():
            assert record(db.get(m.EmailDelivery, delivery_id)) == {**previous, 'address': '', 'revision': previous['revision'] + 1}
        assert db.get(m.EmailDelivery, other.id).address == 'unrelated@example.test'
    preview = h.get('coordinator', f'/api/content/{content_id}/email-preview')
    redacted = [delivery for delivery in preview['email_deliveries'] if delivery['id'] in before]
    assert len(redacted) == 1 and redacted[0]['address'] == ''
    monkeypatch.setattr(email_delivery, 'send', lambda *args: pytest.fail('Anonymised recipients must never be emailed'))
    dispatch(h)
    with transaction(h.app.state.engine) as db:
        pending_id = next(delivery_id for delivery_id, previous in before.items() if previous['state'] == 'queued')
        pending = db.get(m.EmailDelivery, pending_id)
        assert pending.address == '' and pending.error_code == 'EMAIL_ACCESS_ENDED'
        assert db.get(m.Outbox, pending.outbox_id).state == 'available_in_app'

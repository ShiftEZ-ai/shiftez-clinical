"""Email-off paths avoid receipt writes; recipient previews use bounded SQL at department scale."""
from datetime import date
from email import message_from_bytes, policy

import pytest
from sqlalchemy import event as sql_event, insert, select

from apps.api import models as m
from apps.api.db import transaction
from apps.worker import email_delivery
from apps.api.services import audiences
from .test_email_api import notice
from .test_email_delivery import configuration, queued, source
from .test_events import event, event_type, repeat


@pytest.mark.parametrize('kind', ['announcement', 'event'])
@pytest.mark.parametrize('choice', ['explicit_off', 'inherited_off', 'disabled_sender', 'department_off'])
def test_email_queue_does_not_sync_receipts_when_email_is_off(h, monkeypatch, kind, choice):
    configuration(h, smtp_enabled=choice != 'department_off', announcement_default=choice == 'explicit_off')
    also_email = None if choice == 'inherited_off' else choice != 'explicit_off'
    sender = 'microsoft' if choice == 'disabled_sender' else None
    if kind == 'announcement':
        rid, parent_id = source(h, also_email=also_email, email_sender=sender)
    else:
        row = event(h, typ=event_type(h, notify_inbox=False, also_email=choice == 'explicit_off'),
                    repeat=repeat(), repeat_until='2026-10-19')
        rid = row['id']
        with transaction(h.app.state.engine, write=True) as db:
            for occurrence in db.scalars(select(m.EventOccurrence).where(m.EventOccurrence.event_id == rid)):
                occurrence.data = {**occurrence.data, 'also_email': also_email, 'email_sender': sender}
            parent_id = db.scalar(select(m.Outbox.id).where(m.Outbox.kind == 'event',
                m.Outbox.payload['event_id'].as_string() == rid))

    def unexpected_sync(*args, **kwargs):
        pytest.fail('An email-off source must not synchronize receipts')

    monkeypatch.setattr(email_delivery.announcements, 'sync_receipts', unexpected_sync)
    monkeypatch.setattr(email_delivery.event_delivery, 'sync_receipts', unexpected_sync)
    assert queued(h, parent_id) == 0
    with transaction(h.app.state.engine) as db:
        assert not list(db.scalars(select(m.AnnouncementReceipt)))
        assert not list(db.scalars(select(m.EmailDelivery)))
        if kind == 'announcement':
            assert db.get(m.DepartmentContent, rid).receipts_checked_on is None


def test_event_email_choice_is_checked_per_occurrence(h):
    configuration(h)
    row = event(h, typ=event_type(h, notify_inbox=False), repeat=repeat(), repeat_until='2026-10-12')
    with transaction(h.app.state.engine, write=True) as db:
        occurrences = list(db.scalars(select(m.EventOccurrence).where(m.EventOccurrence.event_id == row['id'])
                                     .order_by(m.EventOccurrence.start)))
        assert len(occurrences) == 2
        enabled_id, disabled_id = occurrences[0].id, occurrences[1].id
        occurrences[0].data = {**occurrences[0].data, 'also_email': True}
        parent_id = db.scalar(select(m.Outbox.id).where(m.Outbox.kind == 'event',
            m.Outbox.payload['event_id'].as_string() == row['id']))
    assert queued(h, parent_id) == 1
    with transaction(h.app.state.engine) as db:
        assert {r.event_occurrence_id for r in db.scalars(select(m.AnnouncementReceipt))} == {enabled_id}
        assert {r.event_occurrence_id for r in db.scalars(select(m.EmailDelivery))} == {enabled_id}
        assert disabled_id != enabled_id


@pytest.mark.parametrize('kind', ['content', 'event'])
def test_150_recipient_email_preview_query_count_is_bounded(h, kind):
    with transaction(h.app.state.engine, write=True) as db:
        people = [{'id': m.uid(), 'organisation_id': h.seed['organisation_id'], 'display_name': f'Email person {i}',
                   'work_email': f'work-{i}@example.test' if i % 3 == 0 else ''} for i in range(150)]
        identities = [{'id': m.uid(), 'issuer': 'email-scale', 'subject': str(i),
                       'display_name': f'Email identity {i}', 'person_id': person['id']}
                      for i, person in enumerate(people)]
        db.execute(insert(m.Person), people)
        db.execute(insert(m.Identity), identities)
        db.execute(insert(m.RoleBinding), [{'identity_id': ident['id'], 'department_id': h.seed['department_id'],
            'role': 'staff', 'starts_on': date(2020, 1, 1)} for ident in identities])
        db.execute(insert(m.Account), [{'identity_id': ident['id'], 'username': f'email-scale-{i}',
            'email': f'account-{i}@example.test' if i % 3 != 2 else 'invalid-address'}
            for i, ident in enumerate(identities)])
    urls = []
    for selected in (people[:1], people):
        audience = {'everyone': False, 'person_ids': [person['id'] for person in selected]}
        row = notice(h, audience=audience) if kind == 'content' else event(h, audience=audience)
        urls.append(f"/api/{kind if kind == 'content' else 'events'}/{row['id']}/email-preview")
    h.client('coordinator')
    statements, counts, results = [], [], []

    def count_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sql_event.listen(h.app.state.engine, 'before_cursor_execute', count_statement)
    try:
        for url in urls:
            statements.clear()
            results.append(h.get('coordinator', url))
            counts.append(len(statements))
    finally:
        sql_event.remove(h.app.state.engine, 'before_cursor_execute', count_statement)
    one, many = results
    assert one['addresses'] == ['work-0@example.test']
    assert len(many['recipients']) == 100 and len(many['missing_addresses']) == 50
    assert len(many['addresses']) == 100
    assert many['addresses'] == sorted([f'work-{i}@example.test' if i % 3 == 0 else f'account-{i}@example.test'
                                        for i in range(150) if i % 3 != 2])
    assert {r['display_name'] for r in many['recipients'] + many['missing_addresses']} == {
        person['display_name'] for person in people}
    assert counts[1] <= counts[0] + 2, counts
    assert counts[1] < 60, counts


def test_recurring_draft_queries_and_fact_preparations_are_bounded(h, monkeypatch):
    with transaction(h.app.state.engine, write=True) as db:
        people = [{'id': m.uid(), 'organisation_id': h.seed['organisation_id'], 'display_name': f'Draft person {i}',
                   'work_email': f'draft-{i}@example.test'} for i in range(150)]
        identities = [{'id': m.uid(), 'issuer': 'draft-scale', 'subject': str(i),
                       'display_name': person['display_name'], 'person_id': person['id']}
                      for i, person in enumerate(people)]
        db.execute(insert(m.Person), people)
        db.execute(insert(m.Identity), identities)
        db.execute(insert(m.RoleBinding), [{'identity_id': identity['id'], 'department_id': h.seed['department_id'],
            'role': 'staff', 'starts_on': date(2020, 1, 1)} for identity in identities])
    audience = {'person_ids': [p['id'] for p in people]}
    single = event(h, audience=audience, location_id=h.seed['locations'][0])
    series = event(h, audience=audience, location_id=h.seed['locations'][0], repeat=repeat(), repeat_until='2027-09-27')
    h.client('coordinator')
    counts, preparations = [], []
    statement_count, preparation_count = 0, 0
    original = audiences.profile_context

    def prepare(*args, **kwargs):
        nonlocal preparation_count
        preparation_count += 1
        return original(*args, **kwargs)

    def count_statement(*args):
        nonlocal statement_count
        statement_count += 1

    monkeypatch.setattr(audiences, 'profile_context', prepare)
    sql_event.listen(h.app.state.engine, 'before_cursor_execute', count_statement)
    try:
        for row, attachments in ((single, 1), (series, 52)):
            statement_count = preparation_count = 0
            response = h.client('coordinator').get(f"/api/events/{row['id']}/draft.eml")
            assert response.status_code == 200, response.text
            message = message_from_bytes(response.content, policy=policy.default)
            assert {address.addr_spec for address in message['Bcc'].addresses} == {p['work_email'] for p in people}
            assert len(list(message.iter_attachments())) == attachments
            counts.append(statement_count)
            preparations.append(preparation_count)
    finally:
        sql_event.remove(h.app.state.engine, 'before_cursor_execute', count_statement)
    # Deterministic work budgets apply unchanged on slower CI runners.
    assert preparations == [2, 2], preparations
    assert counts[1] <= counts[0] + 2 and counts[1] < 65, counts


@pytest.mark.parametrize('audience_kind', ['roles', 'people_without_accounts'])
def test_recurring_draft_keeps_date_specific_audience_facts(h, audience_kind):
    row = event(h, audience={'everyone': True} if audience_kind == 'people_without_accounts' else {'roles': ['staff']},
                repeat=repeat(), repeat_until='2026-10-12')
    with transaction(h.app.state.engine, write=True) as db:
        if audience_kind == 'roles':
            binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == h.seed['identities']['staff']))
            binding.ends_on = date(2026, 10, 11)
        else:
            person = m.Person(organisation_id=h.seed['organisation_id'], display_name='Dated email member',
                              work_email='dated@example.test')
            db.add(person)
            db.flush()
            db.add(m.Membership(department_id=h.seed['department_id'], person_id=person.id,
                category_id=h.seed['category'], starts_on=date(2020, 1, 1), ends_on=date(2026, 10, 11)))
    response = h.client('coordinator').get(f"/api/events/{row['id']}/draft.eml")
    assert response.status_code == 409 and response.json()['error']['code'] == 'EMAIL_AUDIENCES_DIFFER'

"""Email queue contact projection preserves per-recipient evidence with bounded SQL reads."""
from collections import Counter
from datetime import date
from time import perf_counter, process_time

import pytest
from sqlalchemy import event as sql_event, insert, select

from apps.api import models as m
from apps.api.db import transaction
from apps.worker import email_delivery as email
from tests.test_email_delivery import configuration, source
from tests.test_event_delivery import scheduled


@pytest.mark.parametrize('kind', ['announcement', 'event'])
def test_queue_batches_contacts_and_preserves_recipient_evidence(h, kind):
    configuration(h)
    people = [{'id': m.uid(), 'organisation_id': h.seed['organisation_id'],
               'display_name': f'Synthetic queue person {i}',
               'work_email': f'work-{i}@example.test' if i % 5 == 0 else
                   'invalid-work' if i % 5 == 3 else ''} for i in range(150)]
    identities = [{'id': m.uid(), 'issuer': 'synthetic-queue-audit', 'subject': str(i),
                   'display_name': person['display_name'], 'person_id': person['id']}
                  for i, person in enumerate(people)]
    extra = {'id': m.uid(), 'organisation_id': h.seed['organisation_id'],
             'display_name': 'Synthetic unlinked recipient', 'work_email': 'unlinked@example.test'}
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Person), [*people, extra])
        db.execute(insert(m.Identity), identities)
        db.execute(insert(m.RoleBinding), [{'identity_id': identity['id'],
            'department_id': h.seed['department_id'], 'role': 'staff', 'starts_on': date(2020, 1, 1)}
            for identity in identities])
        db.execute(insert(m.Account), [{'identity_id': identity['id'], 'username': f'queue-audit-{i}',
            'email': 'invalid-account' if i % 5 == 2 else f'account-{i}@example.test'}
            for i, identity in enumerate(identities) if i % 5 != 4])
        db.get(m.Identity, identities[-1]['id']).disabled = True
        db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == identities[-2]['id'])).revoked = True
    audience = {'person_ids': [p['id'] for p in [*people, extra]]}
    if kind == 'announcement':
        source_id, parent_id = source(h, audience=audience)
        occurrence_id = None
    else:
        source_id, occurrences = scheduled(h, notify=False, audience=audience)
        occurrence_id = occurrences[0]
        with transaction(h.app.state.engine, write=True) as db:
            occurrence = db.get(m.EventOccurrence, occurrence_id)
            occurrence.data = {**occurrence.data, 'also_email': True,
                               'email_sender_identity_id': h.seed['identities']['coordinator']}
            parent_id = db.scalar(select(m.Outbox.id).where(m.Outbox.kind == 'event',
                m.Outbox.payload['event_id'].as_string() == source_id))
    reads = Counter()

    def measured(conn, cursor, statement, parameters, context, executemany):
        sql = ' '.join(statement.lower().split())
        if sql.startswith('select'):
            reads['total'] += 1
            for table in ('identities', 'people', 'accounts'):
                if ' from ' + table + ' ' in sql:
                    reads[table] += 1

    with transaction(h.app.state.engine, write=True) as db:
        parent = db.get(m.Outbox, parent_id)
        sql_event.listen(h.app.state.engine, 'before_cursor_execute', measured)
        cpu, wall = process_time(), perf_counter()
        try:
            assert email.queue(db, parent) == 149
        finally:
            elapsed = {'cpu_seconds': process_time() - cpu, 'wall_seconds': perf_counter() - wall}
            sql_event.remove(h.app.state.engine, 'before_cursor_execute', measured)
        print('queue contacts', {'kind': kind, 'population': 150, **reads, **elapsed})
        deliveries = list(db.scalars(select(m.EmailDelivery).where(m.EmailDelivery.source_outbox_id == parent_id)))
        expected = {identity['id']: (identity['person_id'],
            f'work-{i}@example.test' if i % 5 == 0 else f'account-{i}@example.test' if i % 5 == 1 else '')
            for i, identity in enumerate(identities[:-2])}
        expected[None] = (extra['id'], extra['work_email'])
        assert len(deliveries) == len(expected)
        for delivery in deliveries:
            person_id, address = expected[delivery.identity_id]
            assert (delivery.person_id, delivery.address, delivery.sender_mode, delivery.sender_identity_id,
                    delivery.event_occurrence_id, delivery.state, delivery.error_code, delivery.sent_at,
                    delivery.revision) == (person_id, address, 'smtp', h.seed['identities']['coordinator'],
                        occurrence_id, 'queued' if address else 'failed',
                        None if address else 'EMAIL_ADDRESS_MISSING', None, 1)
            receipt = db.get(m.AnnouncementReceipt, delivery.receipt_id) if delivery.receipt_id else None
            if delivery.identity_id:
                assert (receipt.identity_id, receipt.person_id, receipt.content_id, receipt.event_occurrence_id,
                        receipt.delivered_at, receipt.read_at, receipt.acknowledged_at) == (
                            delivery.identity_id, person_id, source_id if kind == 'announcement' else None,
                            occurrence_id, None, None, None)
            else:
                assert receipt is None
            child = db.get(m.Outbox, delivery.outbox_id)
            suffix = receipt.id if receipt else f'person:{person_id}:{occurrence_id or ""}'
            assert (child.event_key, child.kind, child.state, child.last_error, child.attempts,
                    child.payload, child.revision) == (f'email:{parent_id}:{suffix}', 'email',
                        'queued' if address else 'failed', None if address else 'EMAIL_ADDRESS_MISSING', 0,
                        {'source_outbox_id': parent_id, **({'occurrence_revision': 1} if occurrence_id else {})}, 1)
        assert email.queue(db, parent) == 0
        assert set(db.scalars(select(m.EmailDelivery.id))) == {d.id for d in deliveries}
    assert reads['accounts'] <= 1, reads
    assert reads['total'] <= 60, reads


def test_queue_keeps_account_only_recipients_and_reloads_contacts_between_sources(h):
    configuration(h)
    with transaction(h.app.state.engine, write=True) as db:
        identity = m.Identity(issuer='synthetic-queue-audit', subject='account-only',
                              display_name='Synthetic account-only recipient')
        db.add(identity)
        db.flush()
        identity_id = identity.id
        db.add(m.Account(identity_id=identity_id, username='queue-account-only', email='first@example.test'))
        db.add(m.RoleBinding(identity_id=identity_id, department_id=h.seed['department_id'],
                            role='staff', starts_on=date(2020, 1, 1)))
    parents = [source(h)[1] for _ in range(3)]
    with transaction(h.app.state.engine, write=True) as db:
        assert email.queue(db, db.get(m.Outbox, parents[0])) == 2
        account = db.scalar(select(m.Account).where(m.Account.identity_id == identity_id))
        account.email = 'second@example.test'
        assert email.queue(db, db.get(m.Outbox, parents[1])) == 2
        db.get(m.Identity, identity_id).disabled = True
        assert email.queue(db, db.get(m.Outbox, parents[2])) == 1
        deliveries = list(db.scalars(select(m.EmailDelivery).where(m.EmailDelivery.identity_id == identity_id)))
        assert {(row.source_outbox_id, row.address, row.person_id, row.state) for row in deliveries} == {
            (parents[0], 'first@example.test', None, 'queued'),
            (parents[1], 'second@example.test', None, 'queued')}

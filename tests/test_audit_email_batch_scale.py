"""Email audience work is bounded by this transaction's recipients, with fresh eligibility on each batch."""
from collections import Counter
from datetime import date
import time

import pytest
from sqlalchemy import event as sql_event, insert, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import audiences
from apps.worker import email_delivery as email
from .test_email_delivery import configuration, queued, source


def backlog(h, size, *, audience=None):
    configuration(h, batch_size=200)
    with transaction(h.app.state.engine, write=True) as db:
        people = [{'id': m.uid(), 'organisation_id': h.seed['organisation_id'],
                   'display_name': f'Batch person {i}', 'work_email': f'batch-{i}@example.test'}
                  for i in range(size)]
        identities = [{'id': m.uid(), 'issuer': 'batch-scale', 'subject': str(i),
                       'display_name': f'Batch identity {i}', 'person_id': person['id']}
                      for i, person in enumerate(people)]
        db.execute(insert(m.Person), people)
        db.execute(insert(m.Identity), identities)
        db.execute(insert(m.RoleBinding), [{'identity_id': identity['id'], 'department_id': h.seed['department_id'],
            'role': 'staff', 'starts_on': date(2020, 1, 1)} for identity in identities])
        db.execute(insert(m.Membership), [{'person_id': person['id'], 'department_id': h.seed['department_id'],
            'category_id': h.seed['category'], 'starts_on': date(2020, 1, 1)} for person in people])
    content_id, parent_id = source(h, audience=audience or {'person_ids': [p['id'] for p in people]})
    assert queued(h, parent_id) == size
    return content_id, parent_id


@pytest.mark.parametrize('size,limit', [(15, 200), (150, 200), (150, 1)])
def test_dispatch_profiles_only_the_selected_batch_once(h, monkeypatch, size, limit):
    _, parent_id = backlog(h, size)
    counts = Counter()
    profiles = audiences.profiles
    recipient_context = audiences.recipient_context
    matches = audiences.matches

    def observed_profiles(db, department_id, organisation_id, person_ids, *args, **kwargs):
        counts['profile_visits'] += len(person_ids)
        return profiles(db, department_id, organisation_id, person_ids, *args, **kwargs)

    def observed_context(*args, **kwargs):
        counts['contexts'] += 1
        return recipient_context(*args, **kwargs)

    def observed_match(*args, **kwargs):
        counts['matches'] += 1
        return matches(*args, **kwargs)

    def statement(*args):
        counts['sql'] += 1

    def send(db, delivery, message, *args):
        counts['sent'] += 1
        assert message['Bcc'] == delivery.address
        assert ',' not in message['Bcc']

    monkeypatch.setattr(audiences, 'profiles', observed_profiles)
    monkeypatch.setattr(audiences, 'recipient_context', observed_context)
    monkeypatch.setattr(audiences, 'matches', observed_match)
    monkeypatch.setattr(email, 'send', send)
    sql_event.listen(h.app.state.engine, 'before_cursor_execute', statement)
    started = time.perf_counter()
    try:
        with transaction(h.app.state.engine) as db:
            assert email.dispatch(db, h.settings, limit=limit) == {h.seed['department_id']}
    finally:
        sql_event.remove(h.app.state.engine, 'before_cursor_execute', statement)
    print('email batch', {'size': size, 'limit': limit, 'seconds': round(time.perf_counter() - started, 6), **counts})
    sent = min(size, limit)
    assert counts['sent'] == sent
    # Deterministic work budgets apply on shared CI; no wall-clock threshold depends on runner contention.
    assert counts['contexts'] <= 1, counts
    assert counts['profile_visits'] <= sent, counts
    assert counts['matches'] <= sent, counts
    assert counts['sql'] <= 30 * sent + 40, counts
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.AnnouncementReceipt).where(
            m.AnnouncementReceipt.content_id == db.get(m.Outbox, parent_id).payload['content_id'])))) == size


@pytest.mark.parametrize('change', ['revoked', 'disabled', 'membership', 'audience', 'address'])
def test_next_dispatch_reloads_changed_recipient_eligibility_and_address(h, monkeypatch, change):
    _, parent_id = backlog(h, 3)
    with transaction(h.app.state.engine, write=True) as db:
        content = db.get(m.DepartmentContent, db.get(m.Outbox, parent_id).payload['content_id'])
        content.audience = {'category_ids': [h.seed['category']]}
    sent = []
    monkeypatch.setattr(email, 'send', lambda db, delivery, message, *args: sent.append((delivery.identity_id, delivery.address)))
    # Reuse the Session too: a dispatch call, rather than a process/Session lifetime, bounds the cache.
    with transaction(h.app.state.engine) as db:
        email.dispatch(db, h.settings, limit=1)
        pending = list(db.scalars(select(m.EmailDelivery).join(m.Outbox, m.Outbox.id == m.EmailDelivery.outbox_id)
            .where(m.Outbox.state == 'queued')))
        assert len(sent) == 1 and len(pending) == 2
        for delivery in pending:
            if change == 'revoked':
                db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == delivery.identity_id)).revoked = True
            elif change == 'disabled':
                db.get(m.Identity, delivery.identity_id).disabled = True
            elif change == 'membership':
                db.scalar(select(m.Membership).where(m.Membership.person_id == delivery.person_id)).ends_on = date(2026, 10, 2)
            elif change == 'address':
                db.get(m.Person, delivery.person_id).work_email = f'changed-{delivery.person_id}@example.test'
        if change == 'audience':
            db.get(m.DepartmentContent, content.id).audience = {'roles': ['operator']}
        db.flush()
        email.dispatch(db, h.settings)
        assert len(sent) == (3 if change == 'address' else 1)
        for delivery in pending:
            assert delivery.state == ('sent' if change == 'address' else 'failed')
            if change == 'address':
                assert (delivery.identity_id, f'changed-{delivery.person_id}@example.test') in sent
            else:
                assert delivery.error_code == 'EMAIL_ACCESS_ENDED'


@pytest.mark.parametrize('day,role', [(date(2026, 10, 3), 'staff'), (date(2027, 10, 3), 'operator')])
@pytest.mark.parametrize('shared_facts', [False, True])
def test_scoped_context_uses_only_requested_enabled_identity_and_dated_roles(h, day, role, shared_facts):
    backlog(h, 3)
    with transaction(h.app.state.engine) as db:
        deliveries = list(db.scalars(select(m.EmailDelivery)))
        selected = deliveries[0]
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == selected.identity_id))
        binding.ends_on = date(2027, 1, 1)
        db.add(m.RoleBinding(identity_id=selected.identity_id, department_id=h.seed['department_id'],
            role='operator', starts_on=date(2027, 1, 1)))
        db.get(m.Identity, deliveries[1].identity_id).disabled = True
        db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == deliveries[2].identity_id)).revoked = True
        db.flush()
        facts = audiences.recipient_facts(db, h.seed['department_id']) if shared_facts else None
        context = audiences.recipient_context(db, h.seed['department_id'], day, facts=facts,
            identity_ids={delivery.identity_id for delivery in deliveries})
        assert context[0][selected.identity_id] == frozenset({role})
        assert deliveries[2].identity_id not in context[0]
        assert set(context[0]) <= {delivery.identity_id for delivery in deliveries}
        assert {identity.id for identity in context[1]} == {selected.identity_id}
        assert set(context[2]) == {selected.person_id, None}

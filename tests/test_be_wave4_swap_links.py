"""Delivered Inbox links follow forward-only Undo without granting access to a later recipient's consent."""
from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import swaps
from tests.outbox_support import dispatch_batch_after_undo
from .governance_support import swap_automation, transition
from .test_open_swaps import audience_ready
from .test_staff_swaps import fixed_clock, offered, source_pair  # noqa: F401


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)], ids=['sqlite', 'postgres'])
def store(request):
    return request.getfixturevalue(request.param)


def setup_offer(h, kind='exchange'):
    swap_automation(h, prepare=False, publish=False)
    publication, first, second, _ = source_pair(h, third=True)
    return offered(h, publication, first, kind=kind), second


def respond(h, row, second, who='staff_two'):
    return transition(h, row, 'accept' if row['kind'] == 'give' else 'counteroffer', who,
        {'reason': 'Synthetic recipient consent', **({'assignment_id': second} if row['kind'] == 'exchange' else {})})['swap']


def deliver(h):
    with transaction(h.app.state.engine, write=True) as db:
        dispatch_batch_after_undo(db)


def notice(h, row, action, who):
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.Outbox).where(
            m.Outbox.event_key == f"swap:{row['id']}:{row['revision']}:{action}"))
        assert event.state == 'available_in_app'
        event_id = event.id
    notes = h.get(who, '/api/notifications')['notifications']
    return next(item for item in notes if item['outbox_id'] == event_id)


@pytest.mark.parametrize('kind', ['exchange', 'give'])
def test_delivered_offer_and_response_links_open_latest_request_after_immediate_confirm_undo(store, kind):
    h = store
    original, second = setup_offer(h, kind)
    deliver(h)
    offer_notice = notice(h, original, 'offered', 'staff_two')
    responded = respond(h, original, second)
    deliver(h)
    response_action = 'accept' if kind == 'give' else 'counteroffer'
    response_notice = notice(h, responded, response_action, 'staff')
    assert offer_notice['link']['swap_id'] == response_notice['link']['swap_id'] == original['id']

    confirmed = transition(h, responded, 'confirm')['swap']
    reopened = transition(h, confirmed, 'undo')['swap']
    assert reopened['id'] != original['id'] and reopened['status'] == 'counteroffered'
    with transaction(h.app.state.engine) as db:
        for key in (f"swap:{original['id']}:{confirmed['revision']}:confirm", f"swap:{reopened['id']}:1:reopened"):
            assert db.scalar(select(m.Outbox.state).where(m.Outbox.event_key == key)) == 'superseded'
    for who, saved in [('staff', response_notice), ('staff_two', offer_notice)]:
        detail = h.get(who, '/api/requests/all?id=' + saved['link']['swap_id'])
        assert detail['item']['id'] == reopened['id'] and detail['item']['status'] == 'counteroffered'
        assert {entry['action'] for entry in detail['history']} == {
            'swap.offered', 'swap.' + response_action, 'swap.confirm', 'swap.undo', 'swap.reopened'}
        assert next(n for n in h.get(who, '/api/notifications')['notifications'] if n['id'] == saved['id']) == saved
    # Reviewers get the same current thread; historical swap commands/read endpoints remain bound to the source.
    for who in ('coordinator', 'approver'):
        assert h.get(who, '/api/requests/all?id=' + original['id'])['item']['id'] == reopened['id']
    assert h.get('staff', '/api/swaps/' + original['id'])['swap']['status'] == 'withdrawn'

    confirmed_again = transition(h, reopened, 'confirm')['swap']
    latest = transition(h, confirmed_again, 'undo')['swap']
    assert h.get('staff_two', '/api/requests/all?id=' + original['id'])['item']['id'] == latest['id']
    page = h.get('staff', '/api/requests/all?type=swap')
    assert [item['id'] for item in page['items']] == [latest['id']]


def test_old_swap_link_rechecks_department_person_current_role_and_request_filters(h):
    original, second = setup_offer(h)
    responded = respond(h, original, second)
    confirmed = transition(h, responded, 'confirm')['swap']
    reopened = transition(h, confirmed, 'undo')['swap']
    path = '/api/requests/all?id=' + original['id']
    assert h.get('staff_two', path)['item']['id'] == reopened['id']
    h.get('staff_three', path, status=404)
    h.get('coordinator', path + '&person_id=' + h.seed['people'][2], status=404)
    h.get('staff', path + '&type=leave', status=404)
    h.get('staff', '/api/requests/all?id=' + str(uuid.uuid4()), status=404)
    with transaction(h.app.state.engine) as db:
        assert swaps.latest_thread_id(db, SimpleNamespace(department_id=str(uuid.uuid4())), original['id']) is None

    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['staff_two']).person_id = None
    h.get('staff_two', path, status=404)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['staff_two']).person_id = h.seed['people'][1]
        db.get(m.Person, h.seed['people'][1]).active = False
    h.get('staff_two', path, status=404)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed['people'][1]).active = True
        for binding in db.scalars(select(m.RoleBinding).where(
                m.RoleBinding.identity_id == h.seed['identities']['staff_two'], m.RoleBinding.role == 'staff')):
            binding.ends_on = m.now().date()
        db.add(m.RoleBinding(identity_id=h.seed['identities']['staff_two'], department_id=h.seed['department_id'],
                             role='auditor', starts_on=m.now().date()))
    h.get('staff_two', path, status=404)


def test_old_open_offer_link_cannot_reveal_a_different_recipients_later_thread(h):
    swap_automation(h, prepare=False, publish=False)
    publication, first, second, _ = source_pair(h, third=True)
    original = h.command('staff', 'swap.offer', {'kind': 'give', 'publication_id': publication,
        'assignment_id': first, 'expires_at': (m.now() + timedelta(days=1)).isoformat(),
        'reason': 'Participant-only original offer reason'})['swap']
    audience_ready(h)
    responded = respond(h, original, second)
    deliver(h)
    taken = notice(h, responded, 'accept', 'staff')
    reopened = transition(h, responded, 'undo', 'staff_two')['swap']
    audience_ready(h)
    claimed = respond(h, reopened, second, 'staff_three')
    path = '/api/requests/all?id=' + taken['link']['swap_id']
    h.get('staff_two', path, status=404)
    current = h.get('staff_three', path)
    assert current['item']['id'] == claimed['id']
    assert current['item']['recipient_person_id'] == h.seed['people'][2]
    assert [entry['action'] for entry in current['history']] == ['swap.accept']

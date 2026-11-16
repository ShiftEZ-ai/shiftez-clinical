"""Notifications speak to the reader and name the current navigation destination."""
from types import SimpleNamespace as NS
import pytest
from packages.contracts import schemas as s
from apps.api.services.availability import REQUEST_STAGE
from apps.worker import main as worker
from apps.api.errors import Problem
from apps.api.services import swaps


class FakeDb:
    def __init__(self, row, people):
        self.row, self.people = row, people

    def get(self, model, key):
        return self.people.get(key) if model is worker.m.Person else self.row


def test_swap_text_addresses_the_reader():
    row = NS(kind='exchange', proposer_person_id='a', recipient_person_id='b')
    db = FakeDb(row, {'a': NS(display_name='Dr A'), 'b': NS(display_name='Dr B')})
    event = NS(kind='swap', payload={'swap_id': 's'}, event_key='swap:s:offered')
    say = lambda person: worker.describe(db, event, None, NS(person_id=person))[1]
    assert say('a').startswith('You offered a duty to Dr B. Dr B can offer')
    assert say('b').startswith('Dr A offered a duty to you. You can offer')
    assert say('c').startswith('Dr A offered a duty to Dr B.')
    assert 'Open Requests' not in say('a')


def test_declined_swap_notification_matches_request_status_wording():
    row = NS(kind='exchange', proposer_person_id='a', recipient_person_id='b')
    db = FakeDb(row, {'a': NS(display_name='Dr A'), 'b': NS(display_name='Dr B')})
    event = NS(kind='swap', payload={'swap_id': 's'}, event_key='swap:s:reject')
    for reader in ('a', 'b', 'reviewer'):
        title, message = worker.describe(db, event, None, NS(person_id=reader))
        assert title == 'Swap declined'
        assert 'was closed without changes.' in message
        assert 'Open Requests' not in message


@pytest.mark.parametrize(('step', 'title', 'message'), [
    ('offered', 'Give-away offered', 'Dr A offered a duty to Dr B. Dr B can accept without offering a duty in return.'),
    ('accept', 'Give-away accepted by recipient', 'Dr B accepted the duty from Dr A. Confirmation from Dr A is still needed.'),
    ('confirm', 'Give-away agreed', 'Dr A and Dr B agreed the duty transfer. The published duty changes only after review and publication.'),
    ('prepare', 'Give-away rota change in review', 'The duty transfer from Dr A to Dr B is waiting for approval.'),
    ('prepare_refused', 'Give-away needs coordinator attention', 'The agreed duty transfer from Dr A to Dr B could not be prepared automatically. A coordinator can read the reason and arrange the amendment.'),
    ('publish_refused', 'Give-away needs coordinator attention', 'The duty transfer from Dr A to Dr B was approved but could not be published automatically. A coordinator can read the reason; the published duty has not changed.'),
    ('published', 'Give-away published', 'The duty transfer from Dr A to Dr B is now in the published rota.'),
    ('withdraw', 'Give-away withdrawn', 'The give-away from Dr A to Dr B was withdrawn.'),
    ('reject', 'Give-away declined', 'The give-away from Dr A to Dr B was closed without changes.'),
    ('reopened', 'Give-away step undone', 'A step on the give-away from Dr A to Dr B was undone; the give-away is open again where it was before.'),
])
def test_give_away_notifications_name_the_single_duty_transfer(step, title, message):
    row = NS(kind='give', proposer_person_id='a', recipient_person_id='b')
    db = FakeDb(row, {'a': NS(display_name='Dr A'), 'b': NS(display_name='Dr B')})
    event = NS(kind='swap', payload={'swap_id': 's'}, event_key=f'swap:s:{step}')
    assert worker.describe(db, event, None, NS(person_id='reviewer')) == (
        title, message)


def test_give_away_acceptance_addresses_each_participant_and_requires_confirmation():
    row = NS(kind='give', proposer_person_id='a', recipient_person_id='b')
    db = FakeDb(row, {'a': NS(display_name='Dr A'), 'b': NS(display_name='Dr B')})
    event = NS(kind='swap', payload={'swap_id': 's'}, event_key='swap:s:accept')
    assert worker.describe(db, event, None, NS(person_id='a'))[1] == (
        'Dr B accepted the duty from you. Confirmation from you is still needed.')
    assert worker.describe(db, event, None, NS(person_id='b'))[1] == (
        'You accepted the duty from Dr A. Confirmation from Dr A is still needed.')


@pytest.mark.parametrize('kind,subject', [('exchange', 'pair'), ('give', 'duty transfer')])
def test_swap_undo_error_names_the_confirmed_consent(kind, subject):
    last = NS(action='accept' if kind == 'give' else 'counteroffer', actor_id='recipient')
    results = iter((last, None))
    db = NS(scalar=lambda query: next(results))
    row = NS(id='swap', kind=kind, status='accepted', workspace_id=None)
    with pytest.raises(Problem) as error:
        swaps.undo(db, NS(identity_id='recipient'), row, 'Undo acceptance')
    assert error.value.code == 'SWAP_UNDO_UNAVAILABLE'
    assert error.value.message == f'The {subject} has already been confirmed'


@pytest.mark.parametrize('kind', ['exchange', 'give'])
@pytest.mark.parametrize('step', ['offered', 'taken'])
def test_open_offer_notifications_remain_generic_after_recipient_claims(kind, step):
    row = NS(kind=kind, proposer_person_id='a', recipient_person_id=None)
    db = FakeDb(row, {'a': NS(display_name='Dr A'), 'b': NS(display_name='Dr B')})
    event = NS(kind='swap', payload={'swap_id': 's', 'open_offer_step': step}, event_key='swap:s:accept')
    label = 'give-away' if kind == 'give' else 'swap'
    if step == 'taken':
        expected = (f'Open {label} offer taken', 'A colleague responded first. Confirmation from the proposer is still needed.')
    else:
        response = 'accept without offering a duty in return' if kind == 'give' else 'offer one of their own duties in return'
        expected = (f'Open {label} offer', f'An open {label} offer is available. Eligible colleagues can {response}; '
                    'the proposer then confirms.')
    for recipient in (None, 'b'):
        row.recipient_person_id = recipient
        for reader in ('a', 'b', 'losing_colleague', 'reviewer'):
            assert worker.describe(db, event, None, NS(person_id=reader)) == expected


@pytest.mark.parametrize('step', ['offered', 'taken'])
def test_open_offer_audience_requires_current_active_staff_binding(step):
    department = NS(id='department', organisation_id='organisation')
    people = [NS(id=pid, active=pid != 'inactive', organisation_id='elsewhere' if pid == 'foreign' else 'organisation')
              for pid in ('active', 'inactive', 'foreign', 'disabled', 'auditor', 'outside')]
    identities = {pid: NS(id=pid, person_id=pid, disabled=pid == 'disabled') for pid in
                  ('active', 'inactive', 'foreign', 'disabled', 'auditor', 'outside')}
    identities.update({'unbound': NS(id='unbound', person_id=None, disabled=False),
                       'coordinator': NS(id='coordinator', person_id='inactive', disabled=False),
                       'approver': NS(id='approver', person_id=None, disabled=False)})
    roles = {iid: frozenset([iid if iid in {'auditor', 'coordinator', 'approver'} else 'staff']) for iid in identities}
    event = NS(kind='swap', department_id=department.id, payload={
        'person_ids': ['active', 'inactive', 'foreign', 'disabled', 'auditor', None], 'open_offer_step': step})
    db = NS(get=lambda model, key: department, scalars=lambda query: people)
    assert worker.recipients_of(db, event, None, roles, identities) == {'active', 'coordinator', 'approver'}


def test_request_and_weekly_notifications_point_to_requests():
    row = NS(person_id='a', kind='leave', status='submitted')
    db = FakeDb(row, {'a': NS(display_name='Dr A')})
    event = NS(kind='request', payload={'request_id': 'r', 'person_id': 'a'}, event_key='request:r')
    for reader in ('a', 'reviewer'):
        for status in ('submitted', 'approved', 'rejected', 'withdrawn'):
            row.status = status
            event.payload['request_status'] = status
            title, message = worker.describe(db, event, None, NS(person_id=reader))
            if status == 'submitted':
                assert 'Requests' in message
            else:
                assert title.endswith('was ' + s.REQUEST_STAGE_LABELS[REQUEST_STAGE[status]].lower()) and message == ''
    weekly = NS(kind='availability_pattern', payload={'person_id': 'a'})
    assert worker.describe(db, weekly, None, NS(person_id='reviewer'))[1] == 'Open Requests to approve or decline it.'


@pytest.mark.parametrize('key,status', [('request:r', 'sent'), ('request-decision:r', 'updated')])
def test_legacy_pending_events_do_not_invent_a_past_decision(key, status):
    row = NS(person_id='a', kind='leave', status='approved')
    db = FakeDb(row, {'a': NS(display_name='Dr A')})
    event = NS(kind='request', payload={'request_id': 'r', 'person_id': 'a'}, event_key=key)
    assert worker.describe(db, event, None, NS(person_id='a'))[0].endswith('was ' + status)


@pytest.mark.parametrize('status,suffix', [('active', 'approved'), ('declined', 'declined')])
def test_pattern_notification_uses_captured_decision_not_current_pattern(status, suffix):
    db = FakeDb(NS(status='proposed'), {'a': NS(display_name='Dr A')})
    event = NS(kind='request', payload={'person_id': 'a', 'pattern_id': 'p', 'pattern_revision': 2,
        'pattern_status': status}, event_key='availability-pattern:p:2:decided')
    assert worker.describe(db, event, None, NS(person_id='a')) == (f'Your weekly unavailability was {suffix}', '')
    assert worker.describe(db, event, None, NS(person_id='reviewer')) == (f"Dr A's weekly unavailability was {suffix}", '')
    # A delayed proposal must not borrow the latest approval, and a captured Undo must not announce approval again.
    db.row.status = 'active'
    event.payload['pattern_status'] = 'proposed'
    event.event_key = 'availability-pattern:p'
    assert worker.describe(db, event, None, NS(person_id='a'))[0] == 'Weekly unavailability to approve for Dr A'
    event.event_key = 'availability-pattern:p:3:undone'
    assert worker.describe(db, event, None, NS(person_id='a'))[0] == 'Weekly unavailability decision undone'


def test_publication_review_and_announcement_destinations_use_current_names():
    reader = NS(person_id=None)
    event = NS(kind='publication', payload={})
    publication = NS(workspace_id='w', sequence=1, current=True, snapshot={})
    db = FakeDb(NS(name='October rota', return_note={}), {})
    assert worker.describe(db, event, publication, reader)[1] == 'The rota is final. Open Schedule or Home to see your duties.'
    publication.current = False
    assert worker.describe(db, event, publication, reader)[1].endswith('Open Schedule for the current duties.')
    for kind in ('review', 'returned'):
        message = worker.describe(db, NS(kind=kind, payload={'workspace_id': 'w'}), None, reader)[1]
        assert 'Open Schedule' in message
    ready = worker.describe(db, NS(kind='review', payload={'workspace_id': 'w', 'action': 'submitted'}), None, reader)
    assert ready == ('October rota: ready for review', 'Open Schedule to review the pending step.')
    notice = NS(kind='announcement', payload={'content_id': 'c'})
    db.row = NS(name='Notice', importance='normal', description='')
    assert worker.describe(db, notice, None, reader)[1] == 'Open Home for the details.'
    db.row.description = 'x' * 1000
    message = worker.describe(db, notice, None, reader)[1]
    assert len(message) <= 500 and message.endswith('(more on Home)')

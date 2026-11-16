"""Request notification text uses the contract's public stages, preserving persisted workflow states."""
from types import SimpleNamespace as NS

import pytest

from apps.api.services.availability import REQUEST_STAGE
from apps.worker import main as worker
from packages.contracts import schemas as s
from tests.test_notification_wording import FakeDb


@pytest.mark.parametrize('status', ['approved', 'rejected', 'withdrawn'])
@pytest.mark.parametrize('kind', ['leave', 'overtime_availability'])
@pytest.mark.parametrize('reader', ['person', 'reviewer'])
def test_request_titles_use_shared_stage_labels_for_the_person_and_reviewers(status, kind, reader):
    request = NS(person_id='person', kind=kind, status=status)
    db = FakeDb(request, {'person': NS(display_name='Synthetic person')})
    event = NS(kind='request', event_key='request-decision:request',
               payload={'request_id': 'request', 'person_id': 'person', 'request_status': status, 'request_kind': kind})
    title, message = worker.describe(db, event, None, NS(person_id=reader))
    who = 'Your' if reader == 'person' else "Synthetic person's"
    noun = 'leave' if kind == 'leave' else 'availability for overtime'
    assert title == f'{who} {noun} request was {s.REQUEST_STAGE_LABELS[REQUEST_STAGE[status]].lower()}'
    assert 'rejected' not in title
    assert message == ''
    assert request.status == status


def test_declined_wording_comes_from_the_contract_instead_of_a_worker_copy(monkeypatch):
    request = NS(person_id='person', kind='leave', status='rejected')
    db = FakeDb(request, {})
    event = NS(kind='request', event_key='request-decision:request',
               payload={'request_id': 'request', 'person_id': 'person', 'request_status': 'rejected', 'request_kind': 'leave'})
    assert s.REQUEST_STAGE_LABELS[REQUEST_STAGE['rejected']] == 'Declined'
    assert worker.describe(db, event, None, NS(person_id='person'))[0] == 'Your leave request was declined'
    monkeypatch.setitem(s.REQUEST_STAGE_LABELS, 'declined', 'Changed shared label')
    assert worker.describe(db, event, None, NS(person_id='person'))[0] == 'Your leave request was changed shared label'


@pytest.mark.parametrize('kind,decider', [('leave', 'An approver'), ('overtime_availability', 'A coordinator')])
def test_submitted_requests_keep_the_action_and_correct_decider(kind, decider):
    request = NS(person_id='person', kind=kind, status='submitted')
    db = FakeDb(request, {})
    event = NS(kind='request', event_key='request:request',
               payload={'request_id': 'request', 'person_id': 'person', 'request_status': 'submitted', 'request_kind': kind})
    title, message = worker.describe(db, event, None, NS(person_id='person'))
    assert title.endswith('request was sent')
    assert message == f'{decider} decides it under Requests.'


def test_declined_rotation_swap_addresses_both_affected_people():
    request = NS(person_id='first', kind='rotation_change', status='rejected', details={'swap': {'person_id': 'second'}})
    event = NS(kind='request', event_key='request-decision:request',
               payload={'request_id': 'request', 'person_id': 'first', 'request_status': 'rejected', 'request_kind': 'rotation_change'})
    for person in ('first', 'second'):
        assert worker.describe(FakeDb(request, {}), event, None, NS(person_id=person))[0] == 'Your rotation change request was declined'


@pytest.mark.parametrize('status', ['submitted', 'approved', 'rejected', 'withdrawn'])
def test_delayed_request_notice_keeps_its_recorded_status_and_kind(status):
    request = NS(person_id='person', kind='leave', status='withdrawn' if status != 'withdrawn' else 'submitted')
    event = NS(kind='request', event_key='request-decision:request', payload={
        'request_id': 'request', 'person_id': 'person', 'request_status': status, 'request_kind': 'overtime_availability'})
    title, message = worker.describe(FakeDb(request, {}), event, None, NS(person_id='person'))
    label = 'sent' if status == 'submitted' else s.REQUEST_STAGE_LABELS[REQUEST_STAGE[status]].lower()
    assert title == f'Your availability for overtime request was {label}'
    assert message == ("A coordinator decides it under Requests." if status == 'submitted' else '')

"""A decision a person makes on their own request under a self-approval permission is marked on the request itself:
its row and the decision in its history say Self-approved, as the audit log already does. An independent decision is not."""
import pytest

from apps.api.services import workspace_access
from .governance_support import identity
from .test_availability_patterns import pattern
from .test_request_decide_many import request_row

GRANT = {'actions': ['request.decide'], 'starts_on': '2026-01-01', 'ends_on': '2027-01-01',
         'reason': 'Synthetic bounded self-decision permission'}


def listed(h, role, row_id):
    return next(i for i in h.get(role, '/api/requests/all?status=approved')['items'] if i['id'] == row_id)


def test_a_self_approved_decision_marks_the_row_and_its_history(h):
    person = h.seed['people'][0]
    owner = identity(h, 'pattern_owner', ['staff', 'coordinator'], person=person)
    h.command('admin', 'self_approval.grant', {'identity_id': owner, **GRANT})
    row = h.command('pattern_owner', 'availability_pattern.propose', pattern(person))['pattern']
    h.command('pattern_owner', 'availability_pattern.decide', {'decision': 'approve', 'reason': 'Reviewed with a colleague by phone'},
              row['id'], row['revision'])
    detail = h.get('coordinator', '/api/requests/all?id=' + row['id'])
    assert detail['item']['self_approved'] is True
    decided = [e for e in detail['history'] if e['action'].endswith('.decide')]
    assert decided and decided[-1].get('self_approved') is True
    assert all(not e.get('self_approved') for e in detail['history'] if not e['action'].endswith('.decide'))
    assert listed(h, 'coordinator', row['id'])['self_approved'] is True


def test_an_independent_decision_is_not_marked(h):
    person = h.seed['people'][0]
    row = h.command('staff', 'availability_pattern.propose', pattern(person))['pattern']
    h.command('coordinator', 'availability_pattern.decide', {'decision': 'approve', 'reason': 'Independent review'},
              row['id'], row['revision'])
    detail = h.get('coordinator', '/api/requests/all?id=' + row['id'])
    assert 'self_approved' not in detail['item']  # an independent decision leaves the item as it always was
    assert not any(e.get('self_approved') for e in detail['history'])
    assert 'self_approved' not in listed(h, 'coordinator', row['id'])


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_recorded_leave_uses_the_subject_and_persists_the_self_approval_mark(h, monkeypatch):
    person, other = h.seed['people'][:2]
    coordinator = identity(h, 'leave_coordinator', ['coordinator'], person=person)
    permission = h.command('admin', 'self_approval.grant', {'identity_id': coordinator, **GRANT})['permission']
    payload = {'person_id': person, 'kind': 'recorded_leave', 'start': '2026-10-04T10:00:00Z',
               'end': '2026-10-04T11:00:00Z'}
    assert h.command('leave_coordinator', 'availability.record', payload, status=422)['error']['code'] == 'SCHEMA_INVALID'
    refused = h.command('leave_coordinator', 'availability.record', {**payload, 'private_reason': 'Routine update'},
                        status=422)
    assert refused['error']['code'] == 'SELF_APPROVAL_REASON'
    assert h.get('coordinator', '/api/requests/all?status=approved')['items'] == []

    reason = 'Reviewed with a colleague by phone'
    own = h.command('leave_coordinator', 'availability.record', {**payload, 'private_reason': reason})['record']
    independent = h.command('leave_coordinator', 'availability.record', {
        **payload, 'person_id': other, 'private_reason': 'Routine update'})
    expected_mark = [{'action': 'request.decide', 'permission_id': permission['id']}]
    marked, = (e for e in h.get('auditor', '/api/audit?self_approved=true')['events'] if e['action'] == 'availability.record')
    assert marked['target_id'] == own['id'] and marked['detail']['self_approved'] == expected_mark
    assert reason not in str(marked)

    envelope = workspace_access.audit_envelope

    def untargeted(db, command, result, event):
        event.target_id = None
        return envelope(db, command, result, event)

    with monkeypatch.context() as legacy:
        legacy.setattr(workspace_access, 'audit_envelope', untargeted)
        earlier = h.command('leave_coordinator', 'availability.record', {**payload, 'private_reason': reason,
            'start': '2026-10-05T10:00:00Z', 'end': '2026-10-05T11:00:00Z'})['record']

    independent_event, = (e for e in h.get('auditor', '/api/audit')['events']
                          if e['detail'].get('receipt_id') == independent['receipt_id'])
    assert independent_event['action'] == 'availability.record' and independent_event['target_id'] is None
    assert 'self_approved' not in independent_event['detail']

    for row, self_approved in [(own, True), (earlier, True), (independent['record'], False)]:
        detail = h.get('coordinator', '/api/requests/all?id=' + row['id'])
        item = listed(h, 'coordinator', row['id'])
        assert item['stage'] == detail['item']['stage'] == 'approved'
        assert all(x.get('self_approved', False) is self_approved for x in (row, item, detail['item']))
        assert [e['action'] for e in detail['history'] if e.get('self_approved')] == (
            ['availability.record'] if self_approved else [])
        assert reason not in str(detail)


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('self_decision', [True, False], ids=['self', 'independent'])
def test_admin_request_decision_mark_follows_the_audit(h, self_decision):
    person = h.seed['people'][0]
    admin = identity(h, 'request_admin', ['admin'], person=person)
    grant = h.command('admin', 'self_approval.grant', {'identity_id': admin, **GRANT})['permission']
    row = request_row(h, 'request_admin', person, kind='administrative')
    role = 'request_admin' if self_decision else 'approver'
    reason = 'Reviewed with a colleague by phone'
    decided = h.command(role, 'request.decide', {'decision': 'approve', 'reason': reason},
                        row['id'], row['revision'])['record']
    assert decided['status'] == 'approved' and decided['decision_reason'] == reason
    assert (decided['created_by'] == decided['decided_by'] == admin) is self_decision

    detail = h.get('request_admin', '/api/requests/all?id=' + row['id'])
    event, = (e for e in detail['history'] if e['action'] == 'request.decide')
    assert event['reason'] == reason
    saved, = (e for e in h.get('auditor', '/api/audit')['events']
              if e['action'] == 'request.decide' and e['target_id'] == row['id'])
    home = h.get('request_admin', '/api/home')['attention']
    announcements = h.get('request_admin', '/api/audit?self_approved=true&people=false')['events']
    projections = [listed(h, 'request_admin', row['id']), detail['item'], event, decided]
    if self_decision:
        mark = [{'action': 'request.decide', 'permission_id': grant['id']}]
        assert saved['detail']['self_approved'] == mark
        assert [(e['id'], e['detail']['self_approved']) for e in announcements] == [(saved['id'], mark)]
        assert [item['count'] for item in home if item['key'] == 'self_approved'] == [1]
        assert all(item.get('self_approved') is True for item in projections)
    else:
        assert 'self_approved' not in saved['detail']
        assert announcements == []
        assert all(item['key'] != 'self_approved' for item in home)
        assert all('self_approved' not in item for item in projections)

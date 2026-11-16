"""Absence retains required sessions; explicit closure is inactive in consumer reads."""
from datetime import timedelta

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.validation import evaluate
from .governance_support import identity
from .test_home import keys
from .test_person_workload import endpoint, periods
from .test_series_reconciliation import materialise
from .test_source_parity_series import leave, series


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def database(request):
    return request.getfixturevalue(request.param)


def request_leave(h, partial):
    return h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'leave',
        'start': '2026-10-05T08:00:00+03:00' if partial else '2026-10-05T00:00:00+03:00',
        'end': '2026-10-05T10:00:00+03:00' if partial else '2026-10-06T00:00:00+03:00',
        'private_reason': 'Confidential synthetic request'})['record']


@pytest.mark.parametrize('published', [False, True])
@pytest.mark.parametrize('partial', [False, True])
def test_request_impacts_retain_absent_occurrences_without_auto_closure(database, published, partial):
    h = database
    source = series(h, usual_assignees_fixed=True, close_when_all_usual_unavailable=True)
    request = request_leave(h, partial)
    # Absence approval must not hide existing assignments or remove their needed place.
    workspace = materialise(h, source)
    publication = h.publish(workspace) if published else None
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Independent synthetic approval'},
              request['id'], request['revision'])
    row = h.detail(workspace)['shifts'][0]
    assert not row['closed'] and not row['cancelled']
    items = {role: h.get(role, '/api/requests/all?id=' + request['id'])['item'] for role in ('coordinator', 'staff')}
    for role, item in items.items():
        expected = int(published or role == 'coordinator')
        assert item['schedule_impact']['count'] == expected
        assert item['schedule_impact']['draft_count'] == int(not published and role == 'coordinator')
        assert item['impact_count'] == int(published)
        assert len(item['schedule_impact']['duties']) == expected
    assert ('leave_conflicts' in keys(h.get('coordinator', '/api/home'))) is (not published)
    if published and not partial:
        with transaction(h.app.state.engine) as db:
            before = db.get(m.Publication, publication['publication_id']).snapshot
        item = items['coordinator']
        h.command('coordinator', 'availability.retract', {'reason': 'Synthetic retraction'},
                  item['record_id'], item['record_revision'])
        assert h.get('staff', '/api/requests/all?id=' + request['id'])['item']['schedule_impact']['count'] == 1
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Publication, publication['publication_id']).snapshot == before


def test_partial_absence_approved_before_materialising_has_provenance_without_assignment_conflicts(database):
    h = database
    source = series(h, usual_assignees_fixed=True, close_when_all_usual_unavailable=True)
    request = request_leave(h, partial=True)
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Independent synthetic approval'},
              request['id'], request['revision'])
    workspace = materialise(h, source)
    row = h.detail(workspace)['shifts'][0]
    person = h.seed['people'][0]
    assert not row['closed'] and not row['cancelled']
    assert not any(participant['person_id'] == person for participant in row['participants'])
    assert row['usual_assignee_unavailability'] == [{'person_id': person, 'codes': ['UNAVAILABLE'],
                                                   'fully_unavailable': False}]
    [skipped] = row['template_skipped']
    assert skipped['person_id'] == person and skipped['code'] == 'UNAVAILABLE' and skipped['message']
    assert skipped['start'] == row['start'] and skipped['end'] == row['end']
    for role in ('coordinator', 'staff'):
        item = h.get(role, '/api/requests/all?id=' + request['id'])['item']
        assert item['schedule_impact']['count'] == item['schedule_impact']['draft_count'] == item['impact_count'] == 0
        assert item['schedule_impact']['duties'] == []
    assert 'leave_conflicts' not in keys(h.get('coordinator', '/api/home'))


@pytest.mark.parametrize('open_alternative', [False, True])
def test_closed_published_occurrences_are_never_swap_choices_or_default_duties(database, open_alternative):
    h = database
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    workspace = materialise(h, series(h, usual_assignees_fixed=True, close_when_all_usual_unavailable=True))
    session = h.detail(workspace)['shifts'][0]
    workspace = h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'], 'shift_ids': [session['id']],
        'action': 'cancel', 'reason': 'Explicitly close the required session'}, revision=workspace['revision'])['workspace']
    if open_alternative:
        workspace = h.shift(workspace, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00')['workspace']
    publication = h.publish(workspace)
    path = '/api/publications/' + publication['publication_id']
    before = h.get('coordinator', path)['snapshot']
    closed = next(row for row in before['shifts'] if row.get('cancelled'))['participants'][0]['id']
    open_ids = [row['participants'][0]['id'] for row in before['shifts'] if not row.get('cancelled')]
    for _ in range(2):
        options = h.get('staff', '/api/swaps/options')
        assert [row['assignment_id'] for row in options['assignments']] == open_ids
        assert options['assignment_id'] == (open_ids[0] if open_ids else None)
        assert bool(options['recipients']) is open_alternative
        error = h.get('staff', '/api/swaps/options?assignment_id=' + closed, status=409)
        assert error['error']['code'] == 'SWAP_ASSIGNMENT_UNAVAILABLE'
        error = h.command('staff', 'swap.offer', {'publication_id': publication['publication_id'],
            'assignment_id': closed, 'recipient_person_id': h.seed['people'][1],
            'expires_at': (m.now() + timedelta(days=1)).isoformat(), 'reason': 'Synthetic exchange request'}, status=409)
        assert error['error']['code'] == 'SWAP_ASSIGNMENT_UNAVAILABLE'
    assert h.get('coordinator', path)['snapshot'] == before


def test_draft_workload_uses_explicit_closure_for_totals_limits_and_quotas_and_reopens(database):
    h = database
    person = h.seed['people'][0]
    h.policy({'kind': 'max_hours', 'hours': 40}, {'person_id': person}, severity='advisory')
    h.policy({'kind': 'max_duties', 'maximum': 10}, {'person_id': person}, severity='advisory')
    quota = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                      'metric': 'duties', 'comparison': 'target', 'amount': 2},
                     {'person_id': person}, severity='advisory')
    workspace = materialise(h, series(h, usual_assignees_fixed=True, close_when_all_usual_unavailable=True))
    before = h.detail(workspace)['shifts'][0]
    workspace = h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'], 'shift_ids': [before['id']],
        'action': 'cancel', 'reason': 'Explicitly close the required session'}, revision=workspace['revision'])['workspace']
    assert before['participants']
    for closed in (True, False):
        detail = h.detail(workspace)
        assert detail['shifts'][0]['cancelled'] is closed
        assert not detail['shifts'][0]['closed']
        assert detail['shifts'][0]['participants'] == before['participants']
        checked = next(row for row in evaluate(h.snapshot(workspace))['quota_report']['rows']
                       if row['policy_id'] == quota['id'])
        read = h.get('coordinator', endpoint(person, workspace_id=workspace['id']))
        for period in periods(read).values():
            expected = 0 if closed else 1
            assert period['totals']['duties'] == expected
            assert period['totals']['hours'] == period['totals']['reserved_hours'] == 4 * expected
            assert period['totals']['session_units'] == expected
            for kind, amount in [('max_hours', 4 * expected), ('max_duties', expected)]:
                assert max(row['actual'] for row in period['limits'] if row['kind'] == kind) == amount
            counted = next(row for row in period['quotas'] if row['policy_id'] == quota['id'])
            assert counted['actual'] == checked['actual'] == expected
        if closed:
            workspace = h.command('coordinator', 'workspace.undo', {'reason': 'Restore the required session'},
                                  workspace['id'], workspace['revision'])['workspace']

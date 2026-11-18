"""Home names approved blocking requests over live draft duties, with bounded SQL reads."""
from datetime import timedelta

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import FINDING_MESSAGES, ROLE_NAMES
from packages.domain.finding_causes import COVERAGE_CODES, DUTIES_DURING_APPROVED_LEAVE, MINIMUM_SHORTFALL_CODES
from packages.domain.rota_summary import fix_list
from packages.domain.score import NOT_BREACHES
from packages.domain.solver_summary import REASON_KIND
from packages.domain.time import aware
from .governance_support import identity
from .test_home import keys


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def database(request):
    return request.getfixturevalue(request.param)


def test_approved_leave_names_the_draft_duty_only_for_coordinators(database):
    h = database
    w = h.shift(h.workspace('Duty needing replacement'))['workspace']
    request = h.command('staff', 'request.create', {'person_id': h.seed['people'][0],
        'start': '2026-10-04T09:00:00+03:00', 'end': '2026-10-04T10:00:00+03:00',
        'private_reason': 'Confidential synthetic reason'})['record']
    assert 'leave_conflicts' not in keys(h.get('coordinator', '/api/home'))
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Independent test decision'},
              request['id'], request['revision'])
    with transaction(h.app.state.engine) as db:
        shift = db.scalar(select(m.Shift).where(m.Shift.workspace_id == w['id']))
        duty = db.scalar(select(m.Assignment).where(m.Assignment.shift_id == shift.id))
        expected = {'person': 'Alex Morgan', 'person_id': duty.person_id, 'assignment_id': duty.id, 'start': aware(duty.start).isoformat(), 'end': aware(duty.end).isoformat(),
                    'rota_id': w['id'], 'rota_name': w['name'], 'shift_id': shift.id, 'request_id': request['id']}
    item = next(i for i in h.get('coordinator', '/api/home')['attention'] if i['key'] == 'leave_conflicts')
    assert item == {'key': 'leave_conflicts', 'count': 1, 'classification': DUTIES_DURING_APPROVED_LEAVE, 'sample': [expected]}
    impact = h.get('coordinator', '/api/requests/all?id=' + request['id'])['item']['schedule_impact']
    assert impact['draft_count'] == 1 and impact['duties'][0]['assignment_id'] == duty.id
    for role in ROLE_NAMES:
        username = 'conflict-' + role
        identity(h, username, [role])
        assert ('leave_conflicts' in keys(h.get(username, '/api/home'))) == (role in ('coordinator', 'admin'))


def test_fix_list_uses_the_home_leave_classification_and_offers_cover(database):
    h = database
    person = h.seed['people'][0]
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'minimum', 'amount': 3}, {'person_id': person})
    request = h.command('staff', 'request.create', {'person_id': person,
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-11T00:00:00+03:00',
        'private_reason': 'Confidential synthetic reason'})['record']
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Independent test decision'},
              request['id'], request['revision'])
    w = h.shift(h.workspace('Fix list approved leave'), people=[person])['workspace']
    checked = h.lifecycle(w, 'validate')['workspace']
    fix = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")
    leave = [cause for cause in fix['causes'] if cause['sentence'] == 'Duties during approved leave']
    assert len(leave) == 1
    cause = leave[0]
    assert (cause['checks'], cause['action']['kind']) == (1, 'choose_cover')
    assert cause['checks'] >= 1 and cause['action']['kind'] == 'choose_cover'
    assert cause['eligible_people'] == 0
    assert [fix['kind'] for fix in cause['fixes']][:2] == ['choose_cover', 'open_eligibility']
    assert cause['action']['scope']['classification'] == DUTIES_DURING_APPROVED_LEAVE
    assert cause['action']['scope']['shift_ids'] == [h.detail(checked['id'])['shifts'][0]['id']]
    item = next(i for i in h.get('coordinator', '/api/home')['attention'] if i['key'] == 'leave_conflicts')
    assert item['classification'] == DUTIES_DURING_APPROVED_LEAVE


def test_leave_counts_each_duty_once_and_keeps_its_related_evidence():
    leave = {'classification': DUTIES_DURING_APPROVED_LEAVE, 'person_id': 'person', 'severity': 'error'}
    first = {**leave, 'code': 'UNAVAILABLE', 'shift_id': 'first'}
    second = {**first, 'shift_id': 'second'}
    quota = {**leave, 'code': 'QUOTA_BELOW_MINIMUM', 'cause': {'eligible_people': 1,
             'eligible_people_basis': 'potential_cover_known_records'}}
    findings = [first, {**first, 'start': '2026-10-04T10:00:00Z'}, second, quota]
    places = {'places_filled': 0, 'places_needed': 0, 'places_open': 0}
    cause, = fix_list({'findings': findings}, places)['causes']
    assert cause['checks'] == 2
    assert len(cause['evidence']) == 4
    assert [fix['kind'] for fix in cause['fixes']] == ['choose_cover', 'assign']
    assert cause['safety'] and not cause['acknowledgeable']


def test_non_leave_fix_list_actions_keep_their_order():
    codes = FINDING_MESSAGES.keys() | COVERAGE_CODES | MINIMUM_SHORTFALL_CODES | NOT_BREACHES | REASON_KIND.keys()
    places = {'places_filled': 0, 'places_needed': 1, 'places_open': 1}
    expected_orders = {
        **dict.fromkeys(COVERAGE_CODES, ['open_eligibility', 'assign']),
        'SUPERVISION_GAP': ['open_eligibility', 'assign_supervisor'],
        'LEAD_GAP': ['choose_cover', 'open_eligibility', 'assign'],
        'BALANCE_GOAL': ['rebalance'], 'BALANCE_PAIR': ['rebalance'],
        'TIME_OVERLAP': ['open_line', 'clash'],
        'TYPE_VERSION_CHANGED': ['refresh_sessions'],
        'QUOTA_BELOW_MINIMUM': ['autoschedule_scoped'], 'QUOTA_ABOVE_MAXIMUM': ['autoschedule_scoped'],
    }
    for code in sorted(codes):
        finding = {'code': code, 'severity': 'warning', 'shift_id': 'session', 'person_id': 'person'}
        if code in COVERAGE_CODES:
            finding['cause'] = {'code': 'no_eligible_people', 'eligible_people': 0,
                                'eligible_people_basis': 'potential_cover_known_records'}
        result = fix_list({'findings': [finding]}, places)['causes']
        expected = expected_orders.get(code, [] if code in NOT_BREACHES else ['open_line'])
        if not expected:
            assert not result, code
            continue
        cause, = result
        assert [fix['kind'] for fix in cause['fixes']] == expected, code
        assert cause['action']['kind'] == expected[0], code


def conflict(db, h, start, *, workspace_values=None, shift_values=None, assignment_values=None, request_values=None):
    """Independent synthetic duty/request rows; workflow approval itself is covered above."""
    workspace_values = workspace_values or {}
    status, archived_at = workspace_values.get('status', 'draft'), workspace_values.get('archived_at')
    w = m.Workspace(**{'department_id': h.seed['department_id'], 'name': 'Conflict fixture',
        'starts_on': start.date(), 'ends_on': start.date() + timedelta(days=1),
        'created_by': h.seed['identities']['coordinator'], 'last_edited_by': h.seed['identities']['coordinator'],
        **workspace_values, 'status': 'draft', 'archived_at': None})
    db.add(w)
    db.flush()
    shift = m.Shift(**{'workspace_id': w.id, 'assignment_type_id': h.seed['type'], 'type_revision': 1,
        'location_id': h.seed['locations'][0], 'type_snapshot': {}, 'start': start, 'end': start + timedelta(hours=4),
        **(shift_values or {})})
    db.add(shift)
    db.flush()
    duty = m.Assignment(**{'shift_id': shift.id, 'person_id': h.seed['people'][0], 'start': start,
        'end': start + timedelta(hours=4), **(assignment_values or {})})
    request = m.RequestRecord(**{'department_id': h.seed['department_id'], 'person_id': h.seed['people'][0],
        'start': start + timedelta(hours=1), 'end': start + timedelta(hours=2), 'kind': 'leave', 'status': 'approved',
        'private_reason': 'Private synthetic reason', 'created_by': h.seed['identities']['staff'], **(request_values or {})})
    db.add_all([duty, request])
    db.flush()
    w.status, w.archived_at = status, archived_at
    db.flush()
    return w, shift, duty, request


def test_only_future_blocking_requests_over_active_draft_participation_count(database):
    h = database
    now = m.now()
    with transaction(h.app.state.engine, write=True) as db:
        foreign = m.Department(organisation_id=h.seed['organisation_id'], name='Other department')
        db.add(foreign)
        db.flush()
        scenarios = [
            {'workspace_values': {'status': state}} for state in ('published', 'submitted', 'approved', 'superseded')
        ] + [
            {'workspace_values': {'archived_at': now}},
            {'shift_values': {'cancelled': True}},
            *[{'request_values': {'status': state}} for state in ('submitted', 'rejected', 'withdrawn')],
            {'request_values': {'counts_as_unavailable': False}},
            {'request_values': {'person_id': h.seed['people'][1]}},
            {'request_values': {'department_id': foreign.id}},
            {'workspace_values': {'department_id': foreign.id}},
        ]
        for index, values in enumerate(scenarios, 1):
            conflict(db, h, now + timedelta(days=index), **values)
        # A past request and a completed participant duty are excluded, even if their session continues.
        conflict(db, h, now - timedelta(days=2))
        conflict(db, h, now - timedelta(hours=4),
                 assignment_values={'person_id': h.seed['people'][1], 'end': now + timedelta(hours=1)},
                 shift_values={'end': now + timedelta(hours=1)}, request_values={'person_id': h.seed['people'][1]})
        conflict(db, h, now - timedelta(hours=3), shift_values={'end': now + timedelta(hours=3)},
                 assignment_values={'end': now - timedelta(hours=1)},
                 request_values={'start': now - timedelta(hours=2), 'end': now + timedelta(hours=1)})
        for index, (a, b) in enumerate(((0, 1), (2, 3), (3, 4)), 20):
            start = now + timedelta(days=index)
            # Touching endpoints and time outside a partial participation are not overlaps.
            conflict(db, h, start, assignment_values={'start': start + timedelta(hours=1), 'end': start + timedelta(hours=2)},
                     request_values={'start': start + timedelta(hours=a), 'end': start + timedelta(hours=b)})
    assert 'leave_conflicts' not in keys(h.get('coordinator', '/api/home'))


def test_conflict_count_and_samples_use_two_queries_across_rotas(database):
    h = database
    now = m.now()
    with transaction(h.app.state.engine, write=True) as db:
        # Insert in reverse order; the middle shift starts first but its participant starts later.
        late = conflict(db, h, now + timedelta(days=3))
        middle = conflict(db, h, now + timedelta(days=2), shift_values={'start': now + timedelta(hours=12)},
                          request_values={'kind': 'meeting'})
        early = conflict(db, h, now + timedelta(days=1))
        duplicate = m.RequestRecord(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
            start=early[3].start, end=early[3].end, kind='leave', status='approved', private_reason='Another request',
            created_by=h.seed['identities']['staff'])
        db.add(duplicate)
        db.flush()
        expected = [middle[1].id, early[1].id, late[1].id]
        expected_requests = [middle[3].id, min(early[3].id, duplicate.id), late[3].id]
    h.client('coordinator')
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        if 'join requests on' in statement.lower():
            statements.append((statement.lower(), parameters))

    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        items = h.get('coordinator', '/api/home')['attention']
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    item = next(i for i in items if i['key'] == 'leave_conflicts')
    assert item['count'] == 3 and [sample['shift_id'] for sample in item['sample']] == expected
    assert [sample['request_id'] for sample in item['sample']] == expected_requests
    assert len(statements) == 2
    assert 'count(distinct assignments.id)' in statements[0][0]
    assert 'limit' in statements[1][0] and 'order by shifts.start' in statements[1][0]

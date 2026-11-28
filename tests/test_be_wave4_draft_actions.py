"""One-person moves and clearing selected sessions are atomic, validated, audited and undoable."""
from tests.historical_staffing import retained_staffing
from tests.legacy_lead import historical_type

import uuid

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s


REASON = 'Move one person into the selected open place'
DAY = ('2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00')
NEXT = ('2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')


def pair(h, typ=None):
    created = h.shift(h.workspace(), *DAY, typ=typ)
    other = h.shift(created['workspace'], *NEXT, people=[], typ=typ)
    detail = h.detail(other['workspace'])
    rows = {row['id']: row for row in detail['shifts']}
    return detail['workspace'], rows[created['shift_id']], rows[other['shift_id']]


def command(w, source, target):
    return {'kind': 'draft.move', 'expected_revision': w['revision'], 'payload': {
        'workspace_id': w['id'], 'assignment_id': source['participants'][0]['id'],
        'target_shift_id': target['id'], 'reason': REASON}}


def edit_command(w, shift, participants):
    return {'kind': 'shift.update', 'expected_revision': w['revision'], 'payload': {
        'workspace_id': w['id'], 'shift_id': shift['id'], 'override_reason': REASON,
        **{key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')},
        'participants': [{key: row[key] for key in s.ParticipantInput.model_fields if key in row}
                         for row in participants]}}


def supervised_type(h):
    trainee, supervisor = h.seed['people'][:2]
    rank = h.configure('categories', {'name': 'Synthetic optional trainee rank', 'starts_on': '2020-01-01'})['record']
    h.command('coordinator', 'membership.create', {'person_id': trainee, 'category_id': rank['id'],
        'starts_on': '2020-01-01'})
    with transaction(h.app.state.engine) as db:
        capability = db.scalar(select(m.CapabilityGrant.capability_id).where(m.CapabilityGrant.person_id == supervisor))
    typ = h.configure('assignment_types', {'name': 'Synthetic optional supervision', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}], 'supervision': {'trainee_category_ids': [rank['id']],
            'capability_id': capability, 'trainees_per_supervisor': 1, 'require_pairing': False}}})['record']['id']
    return typ, trainee, supervisor


def post(h, value, *, status=200, role='coordinator', key=None):
    response = h.client(role).post('/api/commands', headers={'idempotency-key': key or str(uuid.uuid4())}, json=value)
    assert response.status_code == status, response.text
    return response.json()


def state(h, w):
    detail = h.detail(w)
    with transaction(h.app.state.engine) as db:
        counts = tuple(db.scalar(select(func.count()).select_from(cls)) for cls in (
            m.ChangeSet, m.AuditEvent, m.CommandReceipt, m.Outbox))
    return detail, counts


def test_move_preview_apply_idempotency_audit_and_exact_undo(h):
    w, source, target = pair(h)
    value = command(w, source, target)
    before = state(h, w)
    value['expected_fingerprint'] = before[0]['current_fingerprint']
    preview = h.client('coordinator').post('/api/planner/preview', json=value)
    assert preview.status_code == 200, preview.text
    assert preview.json()['impact']['counts'] == {'added': 0, 'removed': 0, 'changed': 2}
    assert state(h, w) == before
    key = str(uuid.uuid4())
    result = post(h, value, key=key)
    detail, counts = state(h, w)
    moved = {row['id']: row for row in detail['shifts']}
    participant = moved[target['id']]['participants'][0]
    assert not moved[source['id']]['participants']
    assert participant['id'] == source['participants'][0]['id']
    assert participant['person_id'] == h.seed['people'][0]
    assert (participant['start'], participant['end']) == (target['start'], target['end'])
    assert participant['segments'] == [] and participant['source'] == 'manual'
    assert participant['source_actor_id'] == h.seed['identities']['coordinator']
    assert set(row['id'] for row in result['shifts']) == {source['id'], target['id']}
    assert all(moved[row['id']]['revision'] == row['revision'] + 1 for row in (source, target))
    assert counts[:3] == tuple(count + 1 for count in before[1][:3])
    assert any(f['code'] == 'COVERAGE_GAP' and f['shift_id'] == source['id']
               for f in detail['workspace']['validation']['findings'])
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'draft.move'))
        assert event.reason == REASON and event.detail['receipt_id'] == result['receipt_id']
    assert post(h, value, key=key) == result
    assert state(h, w) == (detail, counts)
    restored = h.command('coordinator', 'workspace.undo', {'reason': 'Restore both original placements'},
                         w['id'], result['workspace']['revision'])
    assert h.detail(restored['workspace'])['shifts'] == before[0]['shifts']


@pytest.mark.parametrize('case,code,status', [
    ('missing_assignment', 'NOT_FOUND', 404), ('missing_target', 'INVALID_MOVE', 422),
    ('same_session', 'INVALID_MOVE', 422), ('foreign_workspace', 'INVALID_MOVE', 422),
    ('source_locked', 'ASSIGNMENT_LOCKED', 409), ('target_locked', 'ASSIGNMENT_LOCKED', 409),
    ('source_cancelled', 'SESSION_CLOSED', 409), ('target_cancelled', 'SESSION_CLOSED', 409),
    ('fixed', 'ASSIGNMENT_FIXED', 409), ('duplicate', 'DUPLICATE_PARTICIPATION', 422),
    ('full', 'NO_OPEN_PLACE', 409), ('stale_revision', 'STALE_REVISION', 409),
    ('stale_fingerprint', 'STALE_PREVIEW', 409), ('inactive_person', 'MOVE_INVALID', 422),
])
def test_invalid_move_rolls_back_content_revisions_and_evidence(h, case, code, status):
    w, source, target = pair(h)
    value = command(w, source, target)
    if case == 'missing_assignment':
        value['payload']['assignment_id'] = str(uuid.uuid4())
    elif case == 'missing_target':
        value['payload']['target_shift_id'] = str(uuid.uuid4())
    elif case == 'same_session':
        value['payload']['target_shift_id'] = source['id']
    elif case == 'foreign_workspace':
        value['payload']['target_shift_id'] = h.shift(h.workspace('Other draft'), people=[])['shift_id']
    elif case == 'stale_revision':
        value['expected_revision'] = 1
    elif case == 'stale_fingerprint':
        value['expected_fingerprint'] = '0' * 64
    else:
        with transaction(h.app.state.engine, write=True) as db:
            if case.endswith('_locked') or case.endswith('_cancelled'):
                row = db.get(m.Shift, source['id'] if case.startswith('source') else target['id'])
                setattr(row, 'locked' if case.endswith('_locked') else 'cancelled', True)
            elif case == 'fixed':
                db.get(m.Assignment, source['participants'][0]['id']).fixed = True
            elif case == 'inactive_person':
                db.get(m.Person, h.seed['people'][0]).active = False
            else:
                db.add(m.Assignment(shift_id=target['id'], person_id=h.seed['people'][0 if case == 'duplicate' else 1],
                    start=db.get(m.Shift, target['id']).start, end=db.get(m.Shift, target['id']).end))
    before = state(h, w)
    assert post(h, value, status=status)['error']['code'] == code
    assert state(h, w) == before


@pytest.mark.parametrize('case', ['overlap', 'leave', 'rest', 'maximum'])
def test_move_conflicts_are_not_partially_saved(h, case):
    w, source, target = pair(h)
    if case == 'leave':
        h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0],
            'start': NEXT[0], 'end': NEXT[1], 'kind': 'recorded_leave', 'private_reason': 'Synthetic private leave'})
    elif case == 'maximum':
        h.policy({'kind': 'maximum_participants', 'maximum': 0}, {'weekdays': [0]})
    else:
        hours = ('08:00', '12:00') if case == 'overlap' else ('18:00', '22:00')
        w = h.shift(w, f'2026-10-05T{hours[0]}:00+03:00', f'2026-10-05T{hours[1]}:00+03:00',
                    service=1, location=1)['workspace']
        if case == 'rest':
            h.policy({'kind': 'min_rest', 'hours': 11})
    before = state(h, w)
    assert post(h, command(w, source, target), status=422)['error']['code'] == 'MOVE_INVALID'
    assert state(h, w) == before


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_move_replies_with_its_admission_before_display_check(h, monkeypatch):
    from packages.domain import fairness, rota_summary, score
    from .test_be18_command_latency import worker_up
    from apps.worker.main import process_one

    w, source, target = pair(h)
    worker_up(h)

    def no_display(*args, **kwargs):
        pytest.fail('The move must leave display output to the validation job')

    with monkeypatch.context() as patch:
        patch.setattr(fairness, 'evaluate_goals', no_display)
        patch.setattr(rota_summary, 'rota_summary', no_display)
        patch.setattr(score, 'schedule_score', no_display)
        result = post(h, command(w, source, target))
    assert result['receipt_id'] and result['workspace']['undo_change_id']
    assert result['validation']['scope'] == 'command_changes'
    assert 'summary_counts' not in result['delta'] and 'fix_list' not in result
    moved = {row['id']: row['participants'] for row in result['shifts']}
    assert not moved[source['id']]
    assert [p['id'] for p in moved[target['id']]] == [source['participants'][0]['id']]
    assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        check = db.get(m.Workspace, w['id']).validation
        assert not check.get('scope') and check.get('score')
        assert any(f['code'] == 'COVERAGE_GAP' and f['shift_id'] == source['id'] for f in check['findings'])


def test_move_preserves_unrelated_existing_mandatory_problems(h):
    w, source, target = pair(h)
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[])['workspace']
    result = post(h, command(w, source, target))
    assert h.detail(result['workspace'])['workspace']['validation']['valid'] is False


@pytest.mark.parametrize('mode', ['preview', 'apply'])
def test_move_preserves_unrelated_existing_overlap_and_its_resolution_actions(h, mode):
    w, source, target = pair(h)
    interval = ('2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00')
    w = h.shift(w, *interval, people=[h.seed['people'][1]])['workspace']
    w = h.shift(w, *interval, people=[h.seed['people'][1]], service=1, location=1)['workspace']
    before = state(h, w)
    existing = [row for row in before[0]['workspace']['validation']['findings'] if row['code'] == 'TIME_OVERLAP']
    assert existing and all(row['person_id'] == h.seed['people'][1] for row in existing)
    value = command(w, source, target)
    if mode == 'preview':
        preview = h.client('coordinator').post('/api/planner/preview', json=value)
        assert preview.status_code == 200, preview.text
        validation = preview.json()['validation']
        assert state(h, w) == before
    else:
        result = post(h, value)
        detail = h.detail(result['workspace'])
        validation = detail['workspace']['validation']
        rows = {row['id']: row for row in detail['shifts']}
        assert not rows[source['id']]['participants']
        assert rows[target['id']]['participants'][0]['person_id'] == h.seed['people'][0]
        assert [row for row in detail['shifts'] if row['id'] not in {source['id'], target['id']}] == [
            row for row in before[0]['shifts'] if row['id'] not in {source['id'], target['id']}]
        restored = h.command('coordinator', 'workspace.undo', {'reason': 'Restore the moved placement'},
                             w['id'], result['workspace']['revision'])
        assert h.detail(restored['workspace'])['shifts'] == before[0]['shifts']
    overlaps = [row for row in validation['findings'] if row['code'] == 'TIME_OVERLAP']
    assert overlaps == existing
    assert all(row['detail']['overlap']['cancel_actions'] for row in overlaps)
    assert validation['valid'] is False


def refuses_a_new_missing_team_lead(h, action):
    typ = historical_type(h, {'name': 'Synthetic lead duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}], 'lead': {'minimum': 1}}})
    first = h.shift(h.workspace(), *DAY, typ=typ['id'], people=[{'person_id': h.seed['people'][0],
        'start': DAY[0], 'end': DAY[1], 'team_lead': True}])
    second = h.shift(first['workspace'], *NEXT, people=[])
    w = second['workspace']
    rows = {row['id']: row for row in h.detail(w)['shifts']}
    before = state(h, w)
    source = rows[first['shift_id']]
    value = command(w, source, rows[second['shift_id']]) if action == 'move' else edit_command(w, source, [])
    error = post(h, value, status=422)
    assert error['error']['code'] == ('MOVE_INVALID' if action == 'move' else 'EDIT_INVALID')
    assert any(f['code'] == 'LEAD_GAP' for f in error['error']['details']['findings'])
    assert state(h, w) == before


def test_move_cannot_leave_a_new_missing_team_lead(h):
    refuses_a_new_missing_team_lead(h, 'move')


def test_edit_cannot_leave_a_new_missing_team_lead(h):
    refuses_a_new_missing_team_lead(h, 'edit')


def test_existing_team_lead_gap_does_not_block_building_the_draft(h):
    typ = historical_type(h, {'name': 'Synthetic incomplete lead duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}], 'lead': {'minimum': 1}}})
    created = h.shift(h.workspace(), *DAY, typ=typ['id'], people=[])
    w = created['workspace']
    shift = h.detail(w)['shifts'][0]
    assert any(f['code'] == 'LEAD_GAP' for f in h.detail(w)['workspace']['validation']['findings'])
    result = post(h, edit_command(w, shift, [{'person_id': h.seed['people'][0], 'start': DAY[0], 'end': DAY[1]}]))
    detail = h.detail(result['workspace'])
    assert detail['shifts'][0]['participants'][0]['person_id'] == h.seed['people'][0]
    assert any(f['code'] == 'LEAD_GAP' and f['shift_id'] == shift['id']
               for f in detail['workspace']['validation']['findings'])
    w = result['workspace']
    fix_list = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")
    cause = next(c for c in fix_list['causes'] if any(
        f['code'] == 'LEAD_GAP' and f['shift_id'] == shift['id'] and f['severity'] == 'error' for f in c['evidence']))
    assert cause['level'] == 'must' and cause['safety'] is True and cause['acknowledgeable'] is False
    assert fix_list['summary']['submit_blocked'] is True
    before = state(h, w)
    submit = {'kind': 'workspace.submit', 'target_id': w['id'], 'expected_revision': w['revision'],
              'payload': {'reason': 'Review the missing team lead'}}
    assert post(h, submit, status=422)['error']['code'] == 'VALIDATION_FAILED'
    assert state(h, w) == before
    submit['payload']['acknowledgements'] = [{'cause_id': cause['id'], 'reason': 'Try to acknowledge the safety gap'}]
    assert post(h, submit, status=409)['error']['code'] == 'SAFETY_RULE'
    after, counts = state(h, w)
    assert after == before[0] and after['workspace']['status'] == 'draft'
    changes, audit, receipts, outbox = before[1]
    assert counts == (changes, audit + 1, receipts, outbox)


def test_move_refuses_full_lead_gap_target_as_no_open_place(h):
    """A missing role on a full session is not a vacant seat for planning.move."""
    typ = historical_type(h, {'name': 'Full lead target', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}],
                       'lead': {'minimum': 1, 'maximum': 1,
                                'capability_ids': [h.seed['supervisor_capability']], 'category_ids': []}}})
    w = h.workspace('Full lead target')
    source = h.shift(w, *DAY, people=[h.seed['people'][2]])
    target = h.shift(source['workspace'], *NEXT, typ=typ['id'], people=h.seed['people'][:2])
    w = target['workspace']
    rows = {row['id']: row for row in h.detail(w)['shifts']}
    before = state(h, w)
    error = post(h, command(w, rows[source['shift_id']], rows[target['shift_id']]), status=409)
    assert error['error']['code'] == 'NO_OPEN_PLACE'
    assert state(h, w) == before


def test_move_cannot_leave_a_trainee_without_their_supervisor(h):
    supervisor, trainee = h.seed['people'][:2]
    rank = h.configure('categories', {'name': 'Synthetic trainee rank', 'starts_on': '2020-01-01'})['record']
    h.command('coordinator', 'membership.create', {'person_id': trainee, 'category_id': rank['id'],
        'starts_on': '2020-01-01'})
    with transaction(h.app.state.engine) as db:
        capability = db.scalar(select(m.CapabilityGrant.capability_id).where(m.CapabilityGrant.person_id == supervisor))
    typ = h.configure('assignment_types', {'name': 'Synthetic supervised duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}], 'supervision': {'trainee_category_ids': [rank['id']],
            'capability_id': capability, 'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']
    first = h.shift(h.workspace(), *DAY, typ=typ['id'], people=[
        {'person_id': supervisor, 'start': DAY[0], 'end': DAY[1], 'participation': 'supervisor'},
        {'person_id': trainee, 'start': DAY[0], 'end': DAY[1], 'participation': 'trainee', 'supervisor_person_id': supervisor}])
    second = h.shift(first['workspace'], *NEXT, people=[])
    w = second['workspace']
    rows = {row['id']: row for row in h.detail(w)['shifts']}
    source = rows[first['shift_id']]
    source['participants'].sort(key=lambda p: p['person_id'] != supervisor)
    before = state(h, w)
    error = post(h, command(w, source, rows[second['shift_id']]), status=422)
    assert error['error']['code'] == 'MOVE_INVALID' and state(h, w) == before


@pytest.mark.parametrize('action', ['move', 'edit'])
def test_move_cannot_create_a_new_supervision_gap(h, action):
    typ, trainee, supervisor = supervised_type(h)
    first = h.shift(h.workspace(), *DAY, typ=typ, people=[
        {'person_id': supervisor, 'start': DAY[0], 'end': DAY[1], 'participation': 'supervisor'},
        {'person_id': trainee, 'start': DAY[0], 'end': DAY[1], 'participation': 'trainee'}])
    second = h.shift(first['workspace'], *NEXT, people=[])
    w = second['workspace']
    rows = {row['id']: row for row in h.detail(w)['shifts']}
    source = rows[first['shift_id']]
    source['participants'].sort(key=lambda p: p['person_id'] != supervisor)
    before = state(h, w)
    value = (command(w, source, rows[second['shift_id']]) if action == 'move'
             else edit_command(w, source, source['participants'][1:]))
    error = post(h, value, status=422)
    assert error['error']['code'] == ('MOVE_INVALID' if action == 'move' else 'EDIT_INVALID')
    assert any(f['code'] == 'SUPERVISION_GAP' for f in error['error']['details']['findings'])
    assert state(h, w) == before


def test_existing_supervision_gap_does_not_block_unrelated_move(h):
    typ, trainee, _ = supervised_type(h)
    gap = retained_staffing(h, h.workspace(), *DAY, typ=typ, people=[{'person_id': trainee,
        'start': DAY[0], 'end': DAY[1], 'participation': 'trainee'}])
    source = h.shift(gap['workspace'], '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00',
        people=[h.seed['people'][2]])
    target = h.shift(source['workspace'], '2026-10-07T08:00:00+03:00', '2026-10-07T12:00:00+03:00', people=[])
    w = target['workspace']
    rows = {row['id']: row for row in h.detail(w)['shifts']}
    result = post(h, command(w, rows[source['shift_id']], rows[target['shift_id']]))
    validation = h.detail(result['workspace'])['workspace']['validation']
    assert any(f['code'] == 'SUPERVISION_GAP' and f['shift_id'] == gap['shift_id']
               for f in validation['findings'])
    moved = next(row for row in result['shifts'] if row['id'] == target['shift_id'])
    assert moved['participants'][0]['person_id'] == h.seed['people'][2]


@pytest.mark.parametrize('aggregation', ['assignment', 'service_group'])
def test_move_uses_policy_headcount_even_when_type_has_no_minimum(h, aggregation):
    typ = h.configure('assignment_types', {'name': 'Policy staffed duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0}]}})['record']
    h.policy({'kind': 'minimum_cover', 'minimum': 1, 'aggregation': aggregation},
             {'weekdays': [0], 'service_id': h.seed['services'][0]})
    first = h.shift(h.workspace(), *DAY)
    second = h.shift(first['workspace'], *NEXT, people=[], typ=typ['id'])
    w = second['workspace']
    if aggregation == 'service_group':
        w = h.shift(w, *NEXT, people=[], typ=typ['id'], location=1)['workspace']
    detail = h.detail(w)
    rows = {row['id']: row for row in detail['shifts']}
    target = rows[second['shift_id']]
    if aggregation == 'service_group':
        # Only one peer owns the pooled finding; the other is still a valid target for its group vacancy.
        anchor = next(f['shift_id'] for f in detail['workspace']['validation']['findings']
                      if f['code'] == 'SCOPED_COVERAGE_GAP')
        target = next(row for row in rows.values() if row['id'] not in {first['shift_id'], anchor})
    moved = post(h, command(w, rows[first['shift_id']], target))
    assert next(row for row in moved['shifts'] if row['id'] == target['id'])['participants']


@pytest.mark.parametrize('side', ['source', 'target'])
def test_move_preserves_usual_fixed_recurring_people(h, side):
    from tests.test_source_parity_series import series
    from tests.test_series_reconciliation import materialise
    recurring = series(h, usual_assignees_fixed=True)
    w = materialise(h, recurring)
    fixed = h.detail(w)['shifts'][0]
    created = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00',
        people=[] if side == 'source' else [h.seed['people'][1]])
    rows = {row['id']: row for row in h.detail(created['workspace'])['shifts']}
    source, target = ((rows[fixed['id']], rows[created['shift_id']]) if side == 'source'
                      else (rows[created['shift_id']], rows[fixed['id']]))
    before = state(h, created['workspace'])
    error = post(h, command(created['workspace'], source, target), status=409)
    assert error['error']['code'] == ('ASSIGNMENT_FIXED' if side == 'source' else 'USUAL_ASSIGNEES_FIXED')
    assert state(h, created['workspace']) == before


def test_closed_recurring_assignment_evidence_survives_move_and_unassign(h):
    from tests.test_source_parity_series import series
    from tests.test_series_reconciliation import materialise
    recurring = series(h, usual_assignees_fixed=True, close_when_all_usual_unavailable=True)
    w = materialise(h, recurring)
    session = h.detail(w)['shifts'][0]
    w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [session['id']],
        'action': 'cancel', 'reason': 'Explicitly close the recurring session'}, revision=w['revision'])['workspace']
    closed = h.detail(w)['shifts'][0]
    created = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[])
    target = next(row for row in h.detail(created['workspace'])['shifts'] if row['id'] == created['shift_id'])
    before = state(h, created['workspace'])
    assert post(h, command(created['workspace'], closed, target), status=409)['error']['code'] == 'SESSION_CLOSED'
    post(h, {'kind': 'shift.action', 'expected_revision': created['workspace']['revision'], 'payload': {
        'workspace_id': w['id'], 'shift_ids': [closed['id']], 'action': 'unassign', 'reason': REASON}}, status=409)
    assert state(h, created['workspace']) == before


@pytest.mark.parametrize('archived', [False, True])
def test_move_cannot_edit_review_or_archived_draft(h, archived):
    w, source, target = pair(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, w['id'])
        if archived:
            row.archived_at = m.now()
        else:
            row.status = 'submitted'
    before = state(h, w)
    assert post(h, command(w, source, target), status=409)['error']['code'] == 'WORKFLOW_STATE'
    assert state(h, w) == before


@pytest.mark.parametrize('role', ['staff', 'approver', 'auditor', 'credentialer', 'operator'])
def test_move_and_unassign_cannot_edit_through_read_only_roles(h, role):
    w, source, target = pair(h)
    h.client(role)
    before = state(h, w)
    post(h, command(w, source, target), status=403, role=role)
    post(h, {'kind': 'shift.action', 'expected_revision': w['revision'], 'payload': {
        'workspace_id': w['id'], 'shift_ids': [source['id']], 'action': 'unassign', 'reason': REASON}},
        status=403, role=role)
    after, counts = state(h, w)
    assert after == before[0]
    assert counts == (before[1][0], before[1][1] + 2, before[1][2], before[1][3])  # audited refusals only


def test_unassign_keeps_sessions_and_metadata_and_has_one_undo(h):
    w, source, target = pair(h)
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00',
                people=[h.seed['people'][1]])['workspace']
    before = state(h, w)
    selected = [row['id'] for row in before[0]['shifts']]
    value = {'kind': 'shift.action', 'expected_revision': w['revision'], 'payload': {
        'workspace_id': w['id'], 'shift_ids': selected, 'action': 'unassign', 'reason': 'Clear selected session people'}}
    preview = h.client('coordinator').post('/api/planner/preview', json=value)
    assert preview.status_code == 200 and state(h, w) == before
    result = post(h, value)
    after, counts = state(h, w)
    assert len(after['shifts']) == len(before[0]['shifts']) and all(not row['participants'] for row in after['shifts'])
    unchanged = ('id', 'assignment_type_id', 'type_snapshot', 'location_id', 'service_ids', 'start', 'end', 'attributes', 'locked', 'cancelled')
    assert [{key: row[key] for key in unchanged} for row in after['shifts']] == [
        {key: row[key] for key in unchanged} for row in before[0]['shifts']]
    assert counts[:3] == tuple(count + 1 for count in before[1][:3])
    restored = h.command('coordinator', 'workspace.undo', {'reason': 'Undo selected session clearing'},
                         w['id'], result['workspace']['revision'])
    assert h.detail(restored['workspace'])['shifts'] == before[0]['shifts']


@pytest.mark.parametrize('fixed_kind', ['explicit', 'usual'])
def test_unassign_refuses_fixed_selection_before_clearing_any_people(h, fixed_kind):
    from tests.test_bulk import preview as bulk_preview
    from tests.test_source_parity_series import series
    from tests.test_series_reconciliation import materialise

    if fixed_kind == 'usual':
        w = materialise(h, series(h, usual_assignees_fixed=True))
        fixed = h.detail(w)['shifts'][0]
        # The frozen series setting protects its usual person independently of the row's fixed flag.
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Assignment, fixed['participants'][0]['id']).fixed = False
    else:
        created = h.shift(h.workspace(), *DAY, people=[{
            'person_id': h.seed['people'][0], 'start': DAY[0], 'end': DAY[1], 'fixed': True}])
        w = created['workspace']
        fixed = h.detail(w)['shifts'][0]
    editable = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00',
                       people=[h.seed['people'][1]])
    w = editable['workspace']
    before = state(h, w)
    payload = {'workspace_id': w['id'], 'shift_ids': [editable['shift_id'], fixed['id']],
               'action': 'unassign', 'reason': 'Keep fixed placements while reviewing selected sessions'}
    value = {'kind': 'shift.action', 'expected_revision': w['revision'], 'payload': payload}
    preview = h.client('coordinator').post('/api/planner/preview', json=value)
    assert preview.status_code == 409, preview.text
    assert preview.json()['error']['code'] == 'ASSIGNMENT_FIXED'
    assert state(h, w) == before
    assert post(h, value, status=409)['error']['code'] == 'ASSIGNMENT_FIXED'
    assert state(h, w) == before

    batch = {'items': [{'item_kind': 'shift.action', 'target_id': w['id'],
        'expected_revision': w['revision'], 'payload': payload}], 'reason': payload['reason']}
    checked = bulk_preview(h, 'coordinator', batch)
    assert checked['counts']['problem'] == 1
    assert 'ASSIGNMENT_FIXED' in {issue['code'] for issue in checked['items'][0]['issues']}
    assert state(h, w) == before
    refused = h.command('coordinator', 'bulk.apply', batch, status=409)
    assert refused['error']['code'] == 'ASSIGNMENT_FIXED'
    assert state(h, w) == before

    # Unselected fixed work does not prevent a normal clear and its existing workspace Undo.
    payload['shift_ids'] = [editable['shift_id']]
    preview = h.client('coordinator').post('/api/planner/preview', json=value)
    assert preview.status_code == 200, preview.text
    assert state(h, w) == before
    changed = post(h, value)
    rows = {row['id']: row for row in h.detail(w)['shifts']}
    assert rows[fixed['id']]['participants'] == next(
        row['participants'] for row in before[0]['shifts'] if row['id'] == fixed['id'])
    assert not rows[editable['shift_id']]['participants']
    restored = h.command('coordinator', 'workspace.undo', {'reason': 'Restore the editable placement'},
                         w['id'], changed['workspace']['revision'])
    assert h.detail(restored['workspace'])['shifts'] == before[0]['shifts']


@pytest.mark.parametrize('case', ['locked', 'foreign', 'missing', 'too_many', 'cancelled', 'read_only'])
def test_unassign_selection_is_bounded_and_all_or_nothing(h, case):
    w, source, target = pair(h)
    selected = [source['id'], target['id']]
    if case == 'foreign':
        selected.append(h.shift(h.workspace('Foreign session'))['shift_id'])
    elif case == 'missing':
        selected.append(str(uuid.uuid4()))
    elif case == 'too_many':
        selected = [str(uuid.uuid4()) for _ in range(501)]
    else:
        with transaction(h.app.state.engine, write=True) as db:
            if case == 'read_only':
                db.get(m.Workspace, w['id']).status = 'submitted'
            else:
                setattr(db.get(m.Shift, target['id']), 'locked' if case == 'locked' else 'cancelled', True)
    before = state(h, w)
    post(h, {'kind': 'shift.action', 'expected_revision': w['revision'], 'payload': {
        'workspace_id': w['id'], 'shift_ids': selected, 'action': 'unassign', 'reason': REASON}},
        status=422 if case == 'too_many' else 404 if case in {'foreign', 'missing'} else 409)
    assert state(h, w) == before
    assert s.ShiftAction.model_json_schema()['properties']['shift_ids']['maxItems'] == 500


def test_unassign_honours_configured_bulk_selection_limit(h):
    w, source, target = pair(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).bulk_settings = {'bulk_max_items': 1, 'bulk_direct_max': 1}
    before = state(h, w)
    payload = {'items': [{'item_kind': 'shift.action', 'target_id': w['id'], 'expected_revision': w['revision'],
        'payload': {'workspace_id': w['id'], 'shift_ids': [source['id'], target['id']], 'action': 'unassign'}}],
        'reason': REASON}
    error = h.command('coordinator', 'bulk.apply', payload, status=422)
    assert error['error']['code'] == 'BULK_ITEM_LIMIT' and state(h, w) == before


def test_unassign_bulk_apply_uses_existing_batch_undo(h):
    typ = h.configure('assignment_types', {'name': 'Optional staffed session', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0}]}})['record']
    w, source, target = pair(h, typ['id'])
    before = state(h, w)
    result = h.command('coordinator', 'bulk.apply', {'items': [{
        'item_kind': 'shift.action', 'target_id': w['id'], 'expected_revision': w['revision'],
        'payload': {'workspace_id': w['id'], 'shift_ids': [source['id'], target['id']], 'action': 'unassign'}}],
        'reason': 'Clear selected people in one reviewed bulk edit'})
    assert all(not row['participants'] for row in h.detail(w)['shifts'])
    assert state(h, w)[1][0] == before[1][0] + 1
    h.command('coordinator', **result['undo'])
    assert h.detail(w)['shifts'] == before[0]['shifts']


def test_unassign_bulk_mandatory_gap_gate_remains_atomic(h):
    w, source, target = pair(h)
    before = state(h, w)
    result = h.command('coordinator', 'bulk.apply', {'items': [{
        'item_kind': 'shift.action', 'target_id': w['id'], 'expected_revision': w['revision'],
        'payload': {'workspace_id': w['id'], 'shift_ids': [source['id'], target['id']], 'action': 'unassign'}}],
        'reason': 'Retain the existing bulk clinical-impact gate'}, status=409)
    assert result['error']['code'] == 'COVERAGE_GAP' and state(h, w) == before

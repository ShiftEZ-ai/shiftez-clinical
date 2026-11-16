"""Draft session imports save their real findings without relaxing governed writes."""
import copy
from datetime import date, timedelta
import uuid

import pytest
from sqlalchemy import func, select

from apps.api import commands, models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_bulk import REASON, preview
from .test_bulk_workspace_undo_composition import snapshot
from .test_resources_calendars import pool, typ


DATABASES = ['h', pytest.param('pg', marks=pytest.mark.postgres)]


def creation(h, workspace, *, day=None):
    day = day or workspace['starts_on']
    return {'item_kind': 'shift.create', 'target_id': workspace['id'],
        'expected_revision': workspace['revision'], 'payload': {
            'workspace_id': workspace['id'], 'assignment_type_id': h.seed['type'],
            'location_id': h.seed['locations'][0], 'service_ids': [h.seed['services'][0]],
            'start': f'{day}T08:00:00+03:00', 'end': f'{day}T12:00:00+03:00',
            'participants': [], 'attributes': {}}}


def batch(h, workspace):
    second = (date.fromisoformat(workspace['starts_on']) + timedelta(days=1)).isoformat()
    return {'items': [creation(h, workspace), creation(h, workspace, day=second)], 'reason': REASON}


def state(h, *workspaces):
    """Include transactional evidence, not just the visible sessions."""
    with transaction(h.app.state.engine) as db:
        counts = tuple(db.scalar(select(func.count()).select_from(cls)) for cls in (
            m.Shift, m.Assignment, m.ShiftService, m.ChangeSet, m.AuditEvent, m.CommandReceipt))
    return [snapshot(h, workspace) for workspace in workspaces], counts


@pytest.mark.parametrize('database', DATABASES)
def test_draft_session_import_reports_real_findings_is_atomic_idempotent_and_undoable(request, database):
    h = request.getfixturevalue(database)
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    workspace = h.workspace('Synthetic unfilled session import')
    body = batch(h, workspace)
    before = state(h, workspace)

    checked = preview(h, 'coordinator', body)
    typed_preview = s.BulkPreview.model_validate(checked)
    assert typed_preview.counts['ready'] == 2 and typed_preview.counts['problem'] == 0
    assert all(row.issues for row in typed_preview.items)
    assert any(finding['severity'] == 'error' for finding in typed_preview.warnings)
    assert state(h, workspace) == before
    trial_ids = {shift['id'] for item in checked['items'] for reply in item['results']
                 for shift in reply.get('shifts', [])}

    key = str(uuid.uuid4())
    applied = h.command('coordinator', 'bulk.apply', body, key=key)
    typed = s.BulkApplyResult.model_validate(applied)
    assert typed.counts['changed'] == 2 and typed.counts['problem'] == 0
    assert typed.batch_id == typed.receipt_id
    assert typed.undoable and not typed.undo_partial
    detail = h.detail(workspace)
    actual_ids = {shift['id'] for shift in detail['shifts']}
    assert len(actual_ids) == 2 and actual_ids.isdisjoint(trial_ids)
    assert {finding['shift_id'] for finding in typed.warnings} == actual_ids
    assert all(finding['workspace_id'] == workspace['id'] and finding['severity'] == 'error'
               for finding in typed.warnings)
    check = detail['workspace']['validation']
    assert check['valid'] is False and check['error_count'] > 0
    assert {(finding['code'], finding.get('shift_id')) for finding in typed.warnings} <= {
        (finding['code'], finding.get('shift_id')) for finding in check['findings']}
    impact = next(row for row in typed.impacts if row.workspace_id == workspace['id'])
    assert impact.status == 'draft' and impact.after_error_count == check['error_count']
    assert {finding['shift_id'] for finding in impact.introduced} == actual_ids
    assert all(finding['severity'] == 'error' for finding in impact.introduced)

    with transaction(h.app.state.engine) as db:
        changes = list(db.scalars(select(m.ChangeSet).where(m.ChangeSet.workspace_id == workspace['id'])))
        assert len(changes) == 1 and changes[0].before_snapshot['shifts'] == before[0][0][1]
        events = list(db.scalars(select(m.AuditEvent).where(
            m.AuditEvent.detail['batch_id'].as_string() == applied['batch_id'])))
        assert sorted(event.action for event in events) == ['bulk.apply', 'shift.create', 'shift.create']
        assert all(event.detail['receipt_id'] == applied['receipt_id'] for event in events)
        assert all(event.reason == REASON for event in events)
    after = state(h, workspace)
    assert h.command('coordinator', 'bulk.apply', body, key=key) == applied
    assert state(h, workspace) == after

    inverse = applied['undo']
    assert inverse['kind'] == 'bulk.apply' and len(inverse['payload']['items']) == 1
    assert inverse['payload']['items'][0]['item_kind'] == 'shift.undo_batch'
    assert inverse['payload']['items'][0]['target_id'] == workspace['id']
    h.command('coordinator', **inverse)
    restored = h.detail(workspace)
    assert restored['shifts'] == before[0][0][1]
    assert restored['workspace']['status'] == 'draft'
    with transaction(h.app.state.engine) as db:
        assert all(change.undone for change in db.scalars(select(m.ChangeSet).where(
            m.ChangeSet.workspace_id == workspace['id'])))


def test_historical_draft_creation_still_reports_its_blocking_findings(h):
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    workspace = h.workspace('Synthetic historical draft import', start='2026-09-20', end='2026-09-27')
    applied = h.command('coordinator', 'bulk.apply', batch(h, workspace))
    saved = {shift['id'] for shift in h.detail(workspace)['shifts']}
    assert applied['counts']['changed'] == 2
    assert {finding['shift_id'] for finding in applied['warnings']} == saved
    assert all(finding['severity'] == 'error' for finding in applied['warnings'])
    assert any(impact['workspace_id'] == workspace['id'] for impact in applied['impacts'])


@pytest.mark.parametrize('database', DATABASES)
def test_invalid_second_creation_rolls_back_the_first_dispatch_and_all_evidence(request, database, monkeypatch):
    h = request.getfixturevalue(database)
    workspace = h.workspace('Synthetic atomic draft import')
    body = batch(h, workspace)
    body['items'][1] = creation(h, workspace, day=workspace['ends_on'])
    before = state(h, workspace)
    original = commands.dispatch
    observed = []

    def dispatch(db, actor, command, settings, *args, **kwargs):
        if command.kind == 'shift.create':
            pending = db.info.get('bulk', {}).get('session_creation', {}).get(workspace['id'], {}).get('shifts', [])
            observed.append(len(pending) + db.scalar(select(func.count()).select_from(m.Shift).where(
                m.Shift.workspace_id == workspace['id'])))
        return original(db, actor, command, settings, *args, **kwargs)

    monkeypatch.setattr(commands, 'dispatch', dispatch)
    error = h.command('coordinator', 'bulk.apply', body, status=422)['error']
    assert error['code'] == 'OUTSIDE_WORKSPACE'
    assert error['details']['failed_index'] == 1 and error['details']['completed'] == 0
    assert observed == [0, 1]
    assert state(h, workspace) == before


@pytest.mark.parametrize('role', ['staff', 'credentialer'])
def test_session_creation_batch_keeps_coordinator_permission(h, role):
    workspace = h.workspace('Synthetic protected session import')
    body = batch(h, workspace)
    h.client(role)
    before = state(h, workspace)
    checked = preview(h, role, body)
    assert checked['counts']['problem'] == 2
    assert all(item['issues'][0]['code'] == 'FORBIDDEN' for item in checked['items'])
    assert not checked['warnings'] and not checked['impacts']
    assert state(h, workspace) == before
    assert h.command(role, 'bulk.apply', body, status=403)['error']['code'] == 'FORBIDDEN'
    # Refusals append their own evidence after the business transaction rolls back.
    expected_counts = (*before[1][:4], before[1][4] + 1, before[1][5])
    assert state(h, workspace) == (before[0], expected_counts)
    with transaction(h.app.state.engine) as db:
        [refusal] = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'command.refused')).all()
        assert refusal.actor_id == h.seed['identities'][role]
        assert refusal.detail == {'error_code': 'FORBIDDEN', 'command_kind': 'bulk.apply', 'status': 403,
                                 'target_id': None, 'target_kind': 'bulk', 'roles': []}


@pytest.mark.parametrize('status,mixed', [('submitted', False), ('approved', False),
    ('published', False), ('published', True)])
def test_governed_target_refuses_creation_and_rolls_back_a_neighbouring_draft(h, status, mixed):
    workspace = h.shift(h.workspace('Synthetic governed import target'))['workspace']
    workspace = h.lifecycle(workspace, 'submit')['workspace']
    if status in {'approved', 'published'}:
        workspace = h.lifecycle(workspace, 'approve', 'approver')['workspace']
    if status == 'published':
        workspace = h.lifecycle(workspace, 'publish')['workspace']
    draft = h.workspace('Synthetic draft beside governed target') if mixed else None
    items = ([creation(h, draft)] if mixed else []) + [creation(h, workspace)]
    body = {'items': items, 'reason': REASON}
    workspaces = [workspace] + ([draft] if draft else [])
    before = state(h, *workspaces)
    checked = preview(h, 'coordinator', body)
    assert checked['items'][-1]['result'] == 'problem'
    assert checked['items'][-1]['issues'][0]['code'] == 'WORKFLOW_STATE'
    assert state(h, *workspaces) == before
    error = h.command('coordinator', 'bulk.apply', body, status=409)['error']
    assert error['code'] == 'WORKFLOW_STATE' and error['details']['failed_index'] == int(mixed)
    assert state(h, *workspaces) == before


def test_mixed_configuration_and_creation_keeps_the_mandatory_impact_guard(h):
    resource = pool(h, capacity=2)
    duty = typ(h, resource, resources=[{'resource_pool_id': resource['id'], 'units': 2}])
    workspace = h.shift(h.workspace('Synthetic mixed import guard'), typ=duty['id'])['workspace']
    body = {'items': [{'item_kind': 'catalogue.update', 'target_id': resource['id'],
        'expected_revision': resource['revision'], 'payload': {'catalogue': 'resource_pools'},
        'changes': [{'field': 'capacity', 'value': 1}]},
        creation(h, workspace, day='2026-10-05')], 'reason': REASON}
    before = state(h, workspace)
    checked = preview(h, 'admin', copy.deepcopy(body))
    assert checked['items'][0]['result'] == 'problem'
    issue = next(issue for issue in checked['items'][0]['issues'] if issue['code'] == 'POOLED_RESOURCE_CAPACITY')
    assert {resolution['id'] for resolution in issue['resolutions']} == {'skip'}
    assert state(h, workspace) == before
    assert h.command('admin', 'bulk.apply', body, status=409)['error']['code'] == 'POOLED_RESOURCE_CAPACITY'
    assert state(h, workspace) == before
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ResourcePool, resource['id']).capacity == 2


def test_creation_batch_checks_each_draft_once_and_returns_its_final_rows(h, monkeypatch):
    from apps.api.services import planning
    first, second = h.workspace('First batch draft'), h.workspace('Second batch draft')
    original, checked = planning.revalidate, []

    def revalidate(db, workspace, *args, **kwargs):
        checked.append(workspace.id)
        return original(db, workspace, *args, **kwargs)

    monkeypatch.setattr(planning, 'revalidate', revalidate)
    result = h.command('coordinator', 'bulk.apply', {'items': [creation(h, first),
        creation(h, second), creation(h, first, day='2026-10-05')], 'reason': REASON})
    assert sorted(checked) == sorted([first['id'], second['id']])
    for workspace, count in ((first, 2), (second, 1)):
        detail = h.detail(workspace)
        assert detail['workspace']['revision'] == workspace['revision'] + count
        saved = {row['id']: row for row in detail['shifts']}
        replies = [reply for item in result['items'] for reply in item['results']
                   if reply['workspace']['id'] == workspace['id']]
        assert {reply['shift_id'] for reply in replies} == saved.keys()
        assert all(reply['workspace']['revision'] == detail['workspace']['revision'] for reply in replies)
        assert {row['id'] for reply in replies for row in reply['shifts']} == saved.keys()
        assert all('start' in row and 'participants' in row for reply in replies for row in reply['shifts'])
        assert sum('health' in reply for reply in replies) == 1


def test_creation_batch_keeps_automatic_manual_supervisor_pairing(h):
    from .test_manual_attachment_pairing import pairing_setup, participants
    flexible, constrained, first, second, duty = pairing_setup(h, 'required')
    workspace = h.workspace('Supervised batch import')
    item = creation(h, workspace)
    item['payload'].update(assignment_type_id=duty,
        participants=participants([flexible, constrained], [first, second]))
    result = h.command('coordinator', 'bulk.apply', {'items': [item], 'reason': REASON})
    for row in (result['items'][0]['results'][0]['shifts'][0], h.detail(workspace)['shifts'][0]):
        pairs = {person['person_id']: person['supervisor_person_id'] for person in row['participants']}
        assert pairs[constrained] == first and pairs[flexible] == second


def test_creation_preview_recovers_from_an_invalid_row_for_apply_ready_only(h):
    workspace = h.workspace('Partial synthetic import')
    body = {'items': [creation(h, workspace), creation(h, workspace, day=workspace['ends_on']),
        creation(h, workspace, day='2026-10-05')], 'reason': REASON, 'apply_ready_only': True}
    checked = preview(h, 'coordinator', body)
    assert checked['counts']['ready'] == 2 and checked['counts']['problem'] == 1
    assert h.detail(workspace)['shifts'] == []
    result = h.command('coordinator', 'bulk.apply', body)
    assert result['counts']['changed'] == 2
    assert len(h.detail(workspace)['shifts']) == 2


@pytest.mark.parametrize('limit', ['MAX_WORKSPACES', 'MAX_SESSIONS'])
def test_creation_batch_retains_complete_impact_limits(h, monkeypatch, limit):
    from apps.api.services import catalogue_preview
    first = h.shift(h.workspace('Existing batch target'))['workspace']
    second = h.workspace('Other batch target')
    body = {'items': [creation(h, first), creation(h, second)], 'reason': REASON}
    monkeypatch.setattr(catalogue_preview, 'limits', lambda db: (1, 5000) if limit == 'MAX_WORKSPACES' else (40, 0))
    before = state(h, first, second)
    assert h.command('coordinator', 'bulk.apply', body, status=409)['error']['code'] == 'CONFIG_IMPACT_LIMIT'
    assert state(h, first, second) == before


def test_creation_batch_counts_preexisting_sessions_changed_by_overlap_flags(h):
    from .test_bulk_nested_limits import limits
    workspace = h.shift(h.workspace('Overlap peer ceiling'))['workspace']
    items = [creation(h, workspace), creation(h, workspace)]
    for item in items:
        item['payload']['participants'] = [{'person_id': h.seed['people'][0],
            'start': item['payload']['start'], 'end': item['payload']['end']}]
    limits(h, 2)
    before = state(h, workspace)
    error = h.command('coordinator', 'bulk.apply', {'items': items, 'reason': REASON}, status=422)['error']
    assert error['code'] == 'BULK_ITEM_LIMIT'
    assert state(h, workspace) == before

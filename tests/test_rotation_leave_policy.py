"""Rotation leave policy uses the same dated domain rule at each write and rota check."""
from importlib import import_module
from datetime import date
from json import dumps

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from packages.domain.validation import evaluate, rotation_blocks, rotation_leave_findings

REASON = 'Synthetic rotation leave policy fixture'
START, END = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'


def resident(h, policy='not_allowed', *, create=True):
    programme = h.configure('programmes', {'name': 'Synthetic leave programme', 'starts_on': '2026-01-01'})['record']
    cohort = h.configure('cohorts', {'name': 'Synthetic leave cohort', 'programme_id': programme['id'],
        'starts_on': '2026-01-01', 'ends_on': '2027-01-01'})['record']
    person_id = h.seed['people'][0]
    h.configure('programme_memberships', {'person_id': person_id, 'programme_id': programme['id'],
        'starts_on': '2026-01-01', 'ends_on': '2027-01-01'})
    typ = h.configure('rotation_types', {'name': 'Synthetic rotation type', 'starts_on': '2026-01-01',
                                       'leave_policy': policy})['record']
    value = {'person_id': person_id, 'cohort_id': cohort['id'], 'rotation_type_id': typ['id'],
             'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
    return typ, value, h.configure('rotations', value)['record'] if create else None


def request_leave(h, *, status=200, start=START, end=END):
    return h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'start': start,
        'end': end, 'private_reason': REASON}, status=status)


def record_away(h, kind='recorded_leave', *, status=200, start=START, end=END):
    return h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': kind,
        'start': start, 'end': end, 'private_reason': REASON}, status=status)


def domain_data(policy='not_allowed', kind='recorded_leave'):
    return {'organisation': {'timezone': 'Asia/Riyadh'},
        'rotations': [{'id': 'rotation', 'person_id': 'person', 'rotation_type_id': 'type',
                       'starts_on': '2026-10-04', 'ends_on': '2026-10-05', 'department_available': False}],
        'rotation_types': {'type': {'leave_policy': policy}},
        'availability': [{'id': 'away', 'person_id': 'person', 'kind': kind, 'start': START, 'end': END}]}


@pytest.mark.parametrize('policy,severity', [('not_allowed', 'error'), ('needs_approval', 'warning'), ('allowed', None)])
@pytest.mark.parametrize('kind', ['leave', 'recorded_leave', 'approved_leave', 'actual_absence', 'external_commitment', 'off_duty'])
def test_domain_rotation_policy(policy, severity, kind):
    findings = rotation_leave_findings(domain_data(policy, kind))
    assert [finding['severity'] for finding in findings] == ([severity] if severity else [])
    if findings:
        assert findings[0]['person_id'] == 'person'
        assert findings[0]['detail']['rotation_id'] == 'rotation'
        assert findings[0]['detail']['request_id' if kind == 'leave' else 'availability_id'] == 'away'


@pytest.mark.postgres
def test_rotation_leave_policy_postgres_request_approval_and_snapshot(pg):
    h = pg
    typ, _, _ = resident(h)
    request_leave(h, status=409)
    changed = h.configure('rotation_types', {**{key: typ[key] for key in s.RotationTypeInput.model_fields},
        'leave_policy': 'needs_approval'}, typ['id'], typ['revision'])
    fact = record_away(h, 'off_duty')
    assert not fact.get('approval_required') and fact['record']['kind'] == 'off_duty'
    pending = record_away(h, 'recorded_leave')
    assert pending['approval_required'] and pending['record']['status'] == 'submitted'
    row = pending['record']
    approved = h.command('approver', 'request.decide', {'decision': 'approve', 'reason': REASON},
        row['id'], row['revision'])['record']
    assert approved['status'] == 'approved'
    findings = evaluate(h.snapshot(h.workspace()))['findings']
    assert any(f['code'] == 'ROTATION_LEAVE_NEEDS_APPROVAL' and f['severity'] == 'warning' for f in findings)
    restored = h.configure('rotation_types', changed['previous_value'], typ['id'], changed['record']['revision'])['record']
    assert restored['leave_policy'] == 'not_allowed'
    actual = record_away(h, 'actual_absence')
    assert actual['record']['counts_as_unavailable'] and actual['record']['request_id'] is None
    assert actual['conflicts'][0]['severity'] == 'error' and not actual.get('approval_required')


@pytest.mark.parametrize('kind', ['protected_time', 'meeting', 'academic', 'academic_time', 'overtime_offer', 'weekly_unavailable'])
def test_protected_and_academic_time_never_conflict(kind):
    assert rotation_leave_findings(domain_data(kind=kind)) == []


def test_domain_uses_local_midnights_exclusive_ends_and_requested_window():
    data = domain_data()
    data['availability'][0].update(start='2026-10-03T21:00:00Z', end='2026-10-04T00:00:00Z')
    assert len(rotation_leave_findings(data)) == 1
    data['availability'][0].update(start='2026-10-04T21:00:00Z', end='2026-10-04T23:00:00Z')
    assert rotation_leave_findings(data) == []
    data = domain_data()
    data['workspace'] = {'starts_on': '2026-10-05', 'ends_on': '2026-10-06'}
    assert rotation_leave_findings(data) == []
    data.pop('workspace')
    data['availability'][0]['retracted'] = True
    assert rotation_leave_findings(data) == []


def test_irrelevant_extreme_rotation_does_not_normalize_outside_supported_utc_dates():
    data = domain_data()
    data['rotations'][0].update(starts_on='0001-01-01', ends_on='0001-01-04')
    data['workspace'] = {'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
    assert rotation_leave_findings(data) == []
    data.pop('workspace')
    data['availability'] = []
    assert rotation_leave_findings(data) == []


def test_external_rotations_still_block_duties_when_leave_is_allowed():
    from packages.domain.time import aware
    data = domain_data('allowed')
    assert rotation_blocks(data, 'person', aware(START), aware(END))
    data['rotations'][0]['department_available'] = True
    assert not rotation_blocks(data, 'person', aware(START), aware(END))


def test_additive_migration_defaults_existing_types_and_rejects_invalid_values():
    migration = import_module('migrations.versions.0112_timeline_leave_policy')
    assert migration.down_revision == '0111_autoschedule_resume'
    engine = sa.create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE rotation_types (id TEXT PRIMARY KEY, name TEXT NOT NULL)'))
        connection.execute(sa.text("INSERT INTO rotation_types VALUES ('old', 'Preserved existing type')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.execute(sa.text('SELECT id, name, leave_policy FROM rotation_types')).one() == (
            'old', 'Preserved existing type', 'not_allowed')
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text("UPDATE rotation_types SET leave_policy='invalid'"))
    engine.dispose()


def test_policy_catalogue_validation_audit_undo_and_old_client_omission(h):
    typ, _, _ = resident(h)
    value = {key: typ[key] for key in s.RotationTypeInput.model_fields}
    changed = h.configure('rotation_types', {**value, 'leave_policy': 'allowed'}, typ['id'], typ['revision'])
    assert changed['record']['leave_policy'] == 'allowed'
    h.configure('rotation_types', {**value, 'leave_policy': 'invalid'}, typ['id'], changed['record']['revision'], status=422)
    omitted = h.configure('rotation_types', {key: val for key, val in value.items() if key != 'leave_policy'},
                          typ['id'], changed['record']['revision'])
    assert omitted['record']['leave_policy'] == 'allowed'
    restored = h.configure('rotation_types', changed['previous_value'], typ['id'], omitted['record']['revision'])['record']
    assert restored['leave_policy'] == 'not_allowed'
    history = h.get('admin', '/api/catalogues/rotation_types/' + typ['id'] + '/history')['versions']
    assert [version['snapshot']['leave_policy'] for version in history] == ['not_allowed', 'allowed', 'allowed', 'not_allowed']


def test_not_allowed_blocks_planned_leave_requests_and_records(h):
    resident(h)
    rejected = record_away(h, status=409)
    assert rejected['error']['code'] == 'ROTATION_LEAVE_NOT_ALLOWED'
    assert request_leave(h, status=409)['error']['code'] == 'ROTATION_LEAVE_NOT_ALLOWED'
    assert h.get('coordinator', '/api/availability')['availability'] == []


def test_needs_approval_routes_planned_leave_record_and_undoes(h):
    resident(h, 'needs_approval')
    submitted = record_away(h)
    request = submitted['record']
    assert submitted['approval_required'] and request['status'] == 'submitted'
    assert REASON not in str(submitted)
    assert submitted['conflicts'][0]['code'] == 'ROTATION_LEAVE_NEEDS_APPROVAL'
    assert h.get('coordinator', '/api/availability')['availability'] == []
    approved = h.command('approver', 'request.decide', {'decision': 'approve', 'reason': REASON},
                         request['id'], request['revision'])['record']
    absence = h.get('coordinator', '/api/availability')['availability'][0]
    assert absence['kind'] == 'recorded_leave' and absence['request_id'] == request['id']
    h.command('approver', 'request.undo', {'reason': REASON}, approved['id'], approved['revision'])
    assert all(row['retracted'] for row in h.get('coordinator', '/api/availability')['availability'])
    retracted = h.get('coordinator', '/api/availability')['availability'][0]
    assert h.command('coordinator', 'availability.restore', {'reason': REASON}, retracted['id'], retracted['revision'],
                     status=409)['error']['code'] == 'ROTATION_LEAVE_APPROVAL_REQUIRED'


@pytest.mark.postgres
def test_meeting_approval_cannot_authorize_leave_during_a_rotation(pg):
    h = pg
    resident(h, 'needs_approval')
    meeting = h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'meeting',
        'start': START, 'end': END, 'private_reason': REASON})['record']
    approved = h.command('approver', 'request.decide', {'decision': 'approve', 'reason': REASON},
                         meeting['id'], meeting['revision'])
    assert approved['conflicts'] == []
    protected, = h.get('coordinator', '/api/availability')['availability']
    assert (protected['kind'], protected['request_id']) == ('protected_time', meeting['id'])

    # Exercise the request kind guard even when the stored leave type matches the edit.
    leave_type = h.configure('leave_types', {'name': 'Synthetic leave type', 'short_code': 'TST'})['record']['id']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.RequestRecord, meeting['id']).leave_type_id = leave_type
    refused = h.command('coordinator', 'availability.update', {'kind': 'approved_leave',
        'leave_type_id': leave_type, 'reason': REASON}, protected['id'], protected['revision'], status=409)
    assert refused['error']['code'] == 'ROTATION_LEAVE_APPROVAL_REQUIRED'
    assert h.get('coordinator', '/api/availability')['availability'] == [protected]


@pytest.mark.postgres
def test_protected_time_rejects_dates_outside_the_local_range(pg):
    # The end fits in UTC but falls past the final day in the fixture's local timezone.
    start, end = '9999-12-31T20:00:00Z', '9999-12-31T21:00:00Z'
    refused = record_away(pg, 'protected_time', start=start, end=end, status=422)
    assert refused['error']['code'] == 'INTERVAL_DATES'
    assert pg.get('coordinator', '/api/availability')['availability'] == []

    protected = record_away(pg, 'protected_time')['record']
    refused = pg.command('coordinator', 'availability.update', {'start': start, 'end': end, 'reason': REASON},
                         protected['id'], protected['revision'], status=422)
    assert refused['error']['code'] == 'INTERVAL_DATES'
    assert pg.get('coordinator', '/api/availability')['availability'] == [protected]


@pytest.mark.parametrize('policy,severity', [('not_allowed', 'error'), ('needs_approval', 'warning')])
@pytest.mark.parametrize('kind', ['actual_absence', 'external_commitment', 'off_duty'])
def test_actual_facts_record_update_restore_and_undo_without_approval(h, policy, severity, kind):
    resident(h, policy)
    saved = record_away(h, kind)
    row = saved['record']
    assert row['kind'] == kind and row['request_id'] is None and not saved.get('approval_required')
    assert saved['conflicts'][0]['severity'] == severity
    assert saved['conflicts'][0]['detail']['availability_id'] == row['id']
    assert REASON not in str(saved)
    updated = h.command('coordinator', 'availability.update', {'start': '2026-10-05T08:00:00+03:00',
        'end': '2026-10-05T12:00:00+03:00', 'reason': REASON}, row['id'], row['revision'])
    assert updated['conflicts'][0]['severity'] == severity
    undo = updated['undo']
    undone = h.command('coordinator', undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'])
    assert undone['record']['start'] == row['start'] and undone['conflicts'][0]['severity'] == severity
    retracted = h.command('coordinator', 'availability.retract', {'reason': REASON}, row['id'], undone['record']['revision'])
    restore = retracted['undo']
    restored = h.command('coordinator', restore['kind'], restore['payload'], restore['target_id'], restore['expected_revision'])
    assert not restored['record']['retracted'] and restored['conflicts'][0]['severity'] == severity
    workspace = h.workspace()
    h.shift(workspace, seed_restricted=True)
    findings = evaluate(h.snapshot(workspace))['findings']
    assert any(finding['code'] == 'UNAVAILABLE' and finding['person_id'] == row['person_id'] for finding in findings)
    assert any(finding['code'].startswith('ROTATION_LEAVE_') and finding['severity'] == severity for finding in findings)
    with transaction(h.app.state.engine) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(m.RequestRecord)) == 0
        created = db.scalars(sa.select(m.AuditEvent).where(m.AuditEvent.department_id == h.seed['department_id'],
            m.AuditEvent.action == 'availability.record',
            m.AuditEvent.detail['receipt_id'].as_string() == saved['receipt_id'])).one()
        assert created.actor_id == h.seed['identities']['coordinator'] and created.target_id is None
        receipt = db.get(m.CommandReceipt, saved['receipt_id'])
        assert receipt.command_kind == 'availability.record' and receipt.result['record']['id'] == row['id']
        assert receipt.result['record']['kind'] == kind and REASON not in str(created.detail)
        actions = set(db.scalars(sa.select(m.AuditEvent.action).where(m.AuditEvent.target_id == row['id'])))
    assert {'availability.update', 'availability.undo_update', 'availability.retract', 'availability.restore'} <= actions
    refused = h.command('coordinator', 'availability.update', {'kind': 'recorded_leave', 'reason': REASON},
        row['id'], restored['record']['revision'], status=409)
    assert refused['error']['code'] == ('ROTATION_LEAVE_NOT_ALLOWED' if policy == 'not_allowed' else 'ROTATION_LEAVE_APPROVAL_REQUIRED')
    assert h.get('coordinator', '/api/availability')['availability'][0]['kind'] == kind


def test_needs_approval_request_returns_shared_warning(h):
    resident(h, 'needs_approval')
    reply = request_leave(h)
    assert reply['record']['status'] == 'submitted'
    assert reply['warnings'] == [reply['conflicts'][0]['message']]
    assert reply['conflicts'][0]['severity'] == 'warning'


def test_catalogue_rotation_warning_keeps_its_distinct_conflict_key(h):
    _, value, _ = resident(h, 'needs_approval', create=False)
    record_away(h, 'actual_absence')
    reply = h.configure('rotations', value)
    assert reply['rotation_leave_conflicts'] == reply['conflicts']
    assert reply['warnings'] == [reply['conflicts'][0]['message']]
    assert not reply.get('overlap_conflicts')
    s.CatalogueConflictDetails.model_validate({key: reply[key] for key in
        ('rotation_leave_conflicts', 'conflicts', 'warnings')})


@pytest.mark.parametrize('kind', ['protected_time', 'recorded_leave'])
def test_allowed_records_directly_and_protected_time_is_exempt(h, kind):
    resident(h, 'not_allowed' if kind == 'protected_time' else 'allowed')
    result = record_away(h, kind)
    assert result['record']['kind'] == kind and not result.get('approval_required')
    assert len(h.get('coordinator', '/api/availability')['availability']) == 1


def test_rotation_create_change_plan_and_request_approval_recheck_leave(h):
    typ, value, _ = resident(h, create=False)
    absence = record_away(h)['record']
    assert h.configure('rotations', value, status=409)['error']['code'] == 'ROTATION_LEAVE_NOT_ALLOWED'
    assert h.command('coordinator', 'rotation.plan', {'cohort_id': value['cohort_id'],
        'person_ids': [value['person_id']], 'starts_on': value['starts_on'],
        'blocks': [{'rotation_type_id': typ['id'], 'length': 1, 'unit': 'months'}],
        'reason': REASON}, status=409)['error']['code'] == 'ROTATION_LEAVE_NOT_ALLOWED'
    rotation = h.configure('rotations', {**value, 'starts_on': '2026-11-01', 'ends_on': '2026-12-01'})['record']
    assert h.configure('rotations', value, rotation['id'], rotation['revision'], status=409)['error']['code'] == 'ROTATION_LEAVE_NOT_ALLOWED'
    h.command('coordinator', 'availability.retract', {'reason': REASON}, absence['id'], absence['revision'])
    # A pending request cannot become approved after the type's policy changes.
    setting = {key: typ[key] for key in s.RotationTypeInput.model_fields}
    allowed = h.configure('rotation_types', {**setting, 'leave_policy': 'allowed'}, typ['id'], typ['revision'])['record']
    request = h.command('coordinator', 'request.create', {'person_id': value['person_id'],
        'start': '2026-11-04T08:00:00+03:00', 'end': '2026-11-04T12:00:00+03:00', 'private_reason': REASON})['record']
    h.configure('rotation_types', setting, typ['id'], allowed['revision'])
    assert h.command('approver', 'request.decide', {'decision': 'approve', 'reason': REASON}, request['id'],
                     request['revision'], status=409)['error']['code'] == 'ROTATION_LEAVE_NOT_ALLOWED'


def test_reference_import_keeps_an_approved_leave_conflict_visible(h, monkeypatch):
    from apps.api.services import independence

    _, value, _ = resident(h, create=False)
    monkeypatch.setattr(independence, 'today', lambda db, department_id: date(2026, 11, 1))
    client = h.client('coordinator')
    endpoint = '/api/rotation-board/reference-preview'
    payload = {'file_text': dumps({'rotations': [value]}), 'reason': REASON}
    pending = request_leave(h)['record']
    refused = client.post(endpoint, json=payload)
    assert refused.status_code == 409, refused.text
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': REASON},
              pending['id'], pending['revision'])
    assert h.configure('rotations', value, status=409)['error']['code'] == 'ROTATION_LEAVE_NOT_ALLOWED'

    future = {**payload, 'file_text': dumps({'rotations': [{**value, 'ends_on': '2026-11-02'}]})}
    assert h.command('coordinator', 'rotation.import_reference', future,
                     status=422)['error']['code'] == 'REFERENCE_ROTATION_DATES'
    review = client.post(endpoint, json=payload)
    assert review.status_code == 200, review.text
    preview = review.json()
    assert preview['count'] == 1 and preview['conflicts'][0]['row'] == 1
    assert 'not allowed' in preview['conflicts'][0]['messages'][0]
    assert not h.get('coordinator', '/api/catalogues')['rotations']
    assert h.command('coordinator', 'rotation.import_reference', payload,
                     status=409)['error']['code'] == 'STALE_PREVIEW'

    payload['expected_fingerprint'] = preview['expected_fingerprint']
    imported = h.command('coordinator', 'rotation.import_reference', payload)
    assert imported['conflicts'] == preview['conflicts']
    rotation = imported['created'][0]
    board = h.get('coordinator', '/api/rotation-board?start=2026-10-01&end=2026-11-01')
    assert board['reference_leave_conflicts'] == [rotation['id']]
    findings = evaluate(h.snapshot(h.workspace()))['findings']
    assert any(f['code'] == 'ROTATION_LEAVE_NOT_ALLOWED' and f['severity'] == 'error'
               and f['detail']['rotation_id'] == rotation['id'] for f in findings)
    assert h.command('coordinator', 'rotation.import_reference', payload,
                     status=409)['error']['code'] == 'DUPLICATE_IMPORT'


def test_recheck_and_snapshot_carry_policy_and_shared_findings(h):
    typ, value, _ = resident(h, 'allowed')
    absence = record_away(h)['record']
    workspace = h.workspace()
    data = h.snapshot(workspace)
    assert data['rotation_types'][typ['id']]['leave_policy'] == 'allowed'
    assert not [finding for finding in evaluate(data)['findings'] if finding['code'].startswith('ROTATION_LEAVE')]
    # Existing data is preserved when the policy changes; the ordinary rota validator reports it.
    setting = {key: typ[key] for key in s.RotationTypeInput.model_fields}
    changed = h.configure('rotation_types', {**setting, 'leave_policy': 'not_allowed'}, typ['id'], typ['revision'])['record']
    data = h.snapshot(workspace)
    findings = [finding for finding in evaluate(data)['findings'] if finding['code'].startswith('ROTATION_LEAVE')]
    assert findings == rotation_leave_findings(data)
    assert findings[0]['detail']['availability_id'] == absence['id'] and findings[0]['severity'] == 'error'
    h.configure('rotation_types', {**setting, 'leave_policy': 'needs_approval'}, typ['id'], changed['revision'])
    assert rotation_leave_findings(h.snapshot(workspace))[0]['severity'] == 'warning'


def test_nonblocking_leave_still_checks_rotation_without_blocking_duties(h):
    typ, _, _ = resident(h, 'allowed')
    leave_type = h.get('staff', '/api/leave-types')['leave_types'][0]
    h.configure('leave_types', {**{key: leave_type[key] for key in s.LeaveTypeInput.model_fields},
        'counts_as_unavailable': False}, leave_type['id'], leave_type['revision'])
    absence = h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0],
        'kind': 'recorded_leave', 'start': START, 'end': END, 'private_reason': REASON,
        'leave_type_id': leave_type['id']})['record']
    h.configure('rotation_types', {**{key: typ[key] for key in s.RotationTypeInput.model_fields},
        'leave_policy': 'not_allowed'}, typ['id'], typ['revision'])
    workspace = h.workspace()
    h.shift(workspace)
    data = h.snapshot(workspace)
    assert absence['id'] not in {row['id'] for row in data['availability']}
    assert absence['id'] in {row['id'] for row in data['rotation_leave_availability']}
    findings = evaluate(data)['findings']
    assert any(finding['code'] == 'ROTATION_LEAVE_NOT_ALLOWED' for finding in findings)
    assert not any(finding['code'] == 'UNAVAILABLE' for finding in findings)


@pytest.mark.parametrize('policy,code', [('not_allowed', 'ROTATION_LEAVE_NOT_ALLOWED'),
                                       ('needs_approval', 'ROTATION_LEAVE_APPROVAL_REQUIRED')])
def test_planned_leave_policy_cannot_be_bypassed_by_update_restore_or_undo(h, policy, code):
    typ, _, _ = resident(h, 'allowed')
    absence = record_away(h)['record']
    retained = record_away(h)['record']
    updated = h.command('coordinator', 'availability.update', {
        'start': '2026-11-04T08:00:00+03:00', 'end': '2026-11-04T12:00:00+03:00', 'reason': REASON},
        absence['id'], absence['revision'])
    setting = {key: typ[key] for key in s.RotationTypeInput.model_fields}
    h.configure('rotation_types', {**setting, 'leave_policy': policy}, typ['id'], typ['revision'])
    assert h.command('coordinator', 'availability.update', {'start': START, 'end': END, 'reason': REASON},
        absence['id'], updated['record']['revision'], status=409)['error']['code'] == code
    undo = updated['undo']
    assert h.command('coordinator', undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'],
                     status=409)['error']['code'] == code
    # A record retained from before the policy change is not silently reinstated.
    retracted = h.command('coordinator', 'availability.retract', {'reason': REASON},
                           retained['id'], retained['revision'])['record']
    assert h.command('coordinator', 'availability.restore', {'reason': REASON}, retracted['id'], retracted['revision'],
                     status=409)['error']['code'] == code


@pytest.mark.parametrize('policy,code', [('not_allowed', 'ROTATION_LEAVE_NOT_ALLOWED'),
                                       ('needs_approval', 'ROTATION_LEAVE_APPROVAL_REQUIRED')])
@pytest.mark.parametrize('kind', ['actual_absence', 'external_commitment', 'off_duty'])
def test_existing_planned_entry_can_be_corrected_to_actual_fact_but_undo_still_checks_plan(h, policy, code, kind):
    typ, _, _ = resident(h, 'allowed')
    row = record_away(h)['record']
    h.configure('rotation_types', {**{key: typ[key] for key in s.RotationTypeInput.model_fields},
        'leave_policy': policy}, typ['id'], typ['revision'])
    updated = h.command('coordinator', 'availability.update', {'kind': kind, 'reason': REASON}, row['id'], row['revision'])
    assert updated['record']['kind'] == kind and updated['conflicts']
    undo = updated['undo']
    assert h.command('coordinator', undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'],
                     status=409)['error']['code'] == code


def approved_leave(h, *, start=START, end=END):
    request = request_leave(h, start=start, end=end)['record']
    approved = h.command('approver', 'request.decide', {'decision': 'approve', 'reason': REASON},
                         request['id'], request['revision'])['record']
    absence = next(row for row in h.get('coordinator', '/api/availability')['availability']
                   if row['request_id'] == request['id'])
    return approved, absence


@pytest.mark.parametrize('policy,severity', [('not_allowed', 'error'), ('needs_approval', 'warning')])
@pytest.mark.parametrize('kind', ['actual_absence', 'external_commitment', 'off_duty'])
def test_request_undo_of_undo_restores_receipt_bound_actual_fact(h, policy, severity, kind):
    typ, _, _ = resident(h, 'allowed')
    approved, absence = approved_leave(h)
    fact = h.command('coordinator', 'availability.update', {'kind': kind, 'reason': REASON},
                     absence['id'], absence['revision'])['record']
    assert fact['request_id'] == approved['id']
    h.configure('rotation_types', {**{key: typ[key] for key in s.RotationTypeInput.model_fields},
        'leave_policy': policy}, typ['id'], typ['revision'])
    undone = h.command('approver', 'bulk.apply', {'items': [{'item_kind': 'request.undo',
        'target_id': approved['id'], 'expected_revision': approved['revision'], 'payload': {}}], 'reason': REASON})
    if policy == 'not_allowed':
        waiting = h.get('approver', '/api/requests?status=submitted')['requests'][0]
        assert h.command('approver', 'request.decide', {'decision': 'approve', 'reason': REASON},
            waiting['id'], waiting['revision'], status=409)['error']['code'] == 'ROTATION_LEAVE_NOT_ALLOWED'
    restored = h.command('approver', **undone['undo'])
    assert restored['items'][0]['results'][0]['conflicts'][0]['severity'] == severity
    with transaction(h.app.state.engine) as db:
        rows = list(db.scalars(sa.select(m.Availability).where(m.Availability.request_id == approved['id'])))
        assert len(rows) == 1 and rows[0].id == fact['id'] and rows[0].kind == kind and not rows[0].retracted
        assert db.get(m.RequestRecord, approved['id']).status == 'approved'


@pytest.mark.parametrize('include_actual', [False, True])
def test_request_undo_of_undo_never_bypasses_planned_leave_in_a_mixed_receipt(h, include_actual):
    typ, _, _ = resident(h, 'allowed')
    rows = [approved_leave(h)]
    if include_actual:
        rows.append(approved_leave(h, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00'))
        _, actual = rows[0]
        h.command('coordinator', 'availability.update', {'kind': 'off_duty', 'reason': REASON},
                  actual['id'], actual['revision'])
    h.configure('rotation_types', {**{key: typ[key] for key in s.RotationTypeInput.model_fields},
        'leave_policy': 'not_allowed'}, typ['id'], typ['revision'])
    undone = h.command('approver', 'bulk.apply', {'items': [{'item_kind': 'request.undo',
        'target_id': request['id'], 'expected_revision': request['revision'], 'payload': {}} for request, _ in rows], 'reason': REASON})
    assert h.command('approver', **undone['undo'], status=409)['error']['code'] == 'ROTATION_LEAVE_NOT_ALLOWED'
    with transaction(h.app.state.engine) as db:
        assert all(db.get(m.Availability, absence['id']).retracted for _, absence in rows)
        assert all(db.get(m.RequestRecord, request['id']).status == 'submitted' for request, _ in rows)


@pytest.mark.parametrize('foreign_source', ['rotation', 'availability'])
def test_shared_person_rule_applies_across_departments_without_disclosing_foreign_ids(h, foreign_source):
    from packages.domain.time import aware
    typ, value, _ = resident(h, create=False)
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Synthetic other department')
        db.add(department)
        db.flush()
        if foreign_source == 'rotation':
            programme = m.Programme(department_id=department.id, name='Foreign synthetic programme', starts_on=date(2026, 1, 1))
            db.add(programme)
            db.flush()
            cohort = m.Cohort(department_id=department.id, name='Foreign synthetic cohort', programme_id=programme.id,
                starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1))
            foreign_type = m.RotationType(department_id=department.id, name='Foreign synthetic rotation type',
                starts_on=date(2026, 1, 1), leave_policy='not_allowed')
            db.add_all([cohort, foreign_type])
            db.flush()
            source = m.Rotation(department_id=department.id, person_id=value['person_id'], cohort_id=cohort.id,
                rotation_type_id=foreign_type.id, starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1))
        else:
            source = m.Availability(department_id=department.id, person_id=value['person_id'], kind='recorded_leave',
                start=aware(START), end=aware(END), private_reason='Foreign private reason must stay private',
                recorded_by=h.seed['identities']['coordinator'])
        db.add(source)
        db.flush()
        private_ids = [source.id, department.id, *([foreign_type.id] if foreign_source == 'rotation' else [])]
    result = (request_leave(h, status=409) if foreign_source == 'rotation'
              else h.configure('rotations', value, status=409))
    assert result['error']['code'] == 'ROTATION_LEAVE_NOT_ALLOWED'
    assert not any(ident in str(result) for ident in private_ids)
    assert 'Foreign private reason' not in str(result)


def weekly(h, role='coordinator', *, status=200):
    return h.command(role, 'availability_pattern.propose', {'person_id': h.seed['people'][0], 'weekdays': [6],
        'start_time': '08:00', 'end_time': '12:00', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, status=status)


@pytest.mark.parametrize('policy', ['not_allowed', 'needs_approval'])
def test_weekly_patterns_do_not_block_rotation_plans_or_new_pattern_requests(h, policy):
    _, value, _ = resident(h, policy, create=False)
    pattern = weekly(h)['pattern']
    assert pattern['status'] == 'active'
    h.configure('rotations', value)
    assert weekly(h, 'staff')['pattern']['status'] == 'proposed'
    data = h.snapshot(h.workspace())
    assert any(row['kind'] == 'weekly_unavailable' for row in data['availability'])
    assert not any(finding['code'].startswith('ROTATION_LEAVE_') for finding in evaluate(data)['findings'])


@pytest.mark.parametrize('policy', ['not_allowed', 'needs_approval'])
def test_weekly_patterns_keep_original_approval_and_undo_semantics(h, policy):
    resident(h, policy)
    result = weekly(h, 'staff')
    pattern = result['pattern']
    assert pattern['status'] == 'proposed'
    h.command('staff', 'availability_pattern.decide', {'decision': 'approve', 'reason': REASON},
              pattern['id'], pattern['revision'], status=403)
    approved = h.command('coordinator', 'availability_pattern.decide', {'decision': 'approve', 'reason': REASON},
                          pattern['id'], pattern['revision'])['pattern']
    assert approved['status'] == 'active'
    data = h.snapshot(h.workspace())
    assert any(row['kind'] == 'weekly_unavailable' for row in data['availability'])
    assert not any(finding['code'].startswith('ROTATION_LEAVE_') for finding in evaluate(data)['findings'])
    undone = h.command('coordinator', 'availability_pattern.undo', {'reason': REASON}, approved['id'], approved['revision'])['pattern']
    assert undone['status'] == 'proposed'


def test_weekly_approval_restore_and_rota_recheck_ignore_changed_rotation_policy(h):
    typ, _, _ = resident(h, 'allowed')
    proposed = weekly(h, 'staff')['pattern']
    setting = {key: typ[key] for key in s.RotationTypeInput.model_fields}
    h.configure('rotation_types', {**setting, 'leave_policy': 'not_allowed'}, typ['id'], typ['revision'])
    approved = h.command('coordinator', 'availability_pattern.decide', {'decision': 'approve', 'reason': REASON},
                          proposed['id'], proposed['revision'])['pattern']
    data = h.snapshot(h.workspace())
    assert not any(finding['code'].startswith('ROTATION_LEAVE_') for finding in evaluate(data)['findings'])
    ended = h.command('staff', 'availability_pattern.end', {'reason': REASON}, approved['id'], approved['revision'])['pattern']
    assert h.command('staff', 'availability_pattern.restore', {'reason': REASON}, ended['id'], ended['revision'])['pattern']['status'] == 'active'


@pytest.mark.parametrize('policy', ['not_allowed', 'needs_approval'])
def test_restoring_direct_weekly_time_away_keeps_original_active_state(h, policy):
    typ, _, _ = resident(h, 'allowed')
    active = weekly(h)['pattern']
    ended = h.command('staff', 'availability_pattern.end', {'reason': REASON}, active['id'], active['revision'])['pattern']
    h.configure('rotation_types', {**{key: typ[key] for key in s.RotationTypeInput.model_fields},
        'leave_policy': policy}, typ['id'], typ['revision'])
    restored = h.command('staff', 'availability_pattern.restore', {'reason': REASON}, ended['id'], ended['revision'])
    assert restored['pattern']['status'] == 'active' and restored['pattern']['decided_by'] == active['decided_by']

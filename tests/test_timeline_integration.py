"""Timeline integration with existing collection, bulk and receipt boundaries."""
import json

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.time import aware
from packages.domain.validation import evaluate


def test_bulk_planned_leave_routes_to_approval_and_undo_withdraws_request(h):
    pid = h.seed['people'][0]
    programme = h.configure('programmes', {'name': 'Synthetic bulk rotation', 'starts_on': '2026-01-01'})['record']
    cohort = h.configure('cohorts', {'name': 'Synthetic bulk cohort', 'programme_id': programme['id'],
        'starts_on': '2026-01-01', 'ends_on': '2027-01-01'})['record']
    h.configure('programme_memberships', {'person_id': pid, 'programme_id': programme['id'],
        'starts_on': '2026-01-01', 'ends_on': '2027-01-01'})
    typ = h.configure('rotation_types', {'name': 'Synthetic approved leave', 'starts_on': '2026-01-01',
        'leave_policy': 'needs_approval'})['record']
    h.configure('rotations', {'person_id': pid, 'cohort_id': cohort['id'], 'rotation_type_id': typ['id'],
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01'})
    payload = {'item_kind': 'availability.record', 'items': [{'payload': {'person_id': pid,
        'kind': 'recorded_leave', 'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
        'private_reason': 'Synthetic pending bulk leave'}}], 'reason': 'Synthetic bulk preview and Undo'}
    preview = h.client('coordinator').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
    assert preview.status_code == 200, preview.text
    assert preview.json()['counts']['ready'] == 1
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.RequestRecord)) == 0
    applied = h.command('coordinator', 'bulk.apply', payload)
    with transaction(h.app.state.engine) as db:
        [request] = db.scalars(select(m.RequestRecord))
        request_id = request.id
        assert request.status == 'submitted' and request.details['availability_kind'] == 'recorded_leave'
        assert db.scalar(select(func.count()).select_from(m.Availability)) == 0
    undo = applied['undo']
    h.command('coordinator', undo['kind'], undo['payload'], undo.get('target_id'), undo.get('expected_revision'))
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RequestRecord, request_id).status == 'withdrawn'


@pytest.mark.parametrize('kind', ['actual_absence', 'external_commitment', 'off_duty'])
def test_bulk_actual_fact_preview_apply_receipt_and_undo_under_blocked_rotation(h, kind):
    from .test_rotation_leave_policy import END, REASON, START, resident
    _, value, _ = resident(h)
    payload = {'item_kind': 'availability.record', 'items': [{'payload': {
        'person_id': value['person_id'], 'kind': kind, 'start': START, 'end': END, 'private_reason': REASON}}],
        'reason': 'Synthetic actual-fact bulk preview and Undo'}
    preview = h.client('coordinator').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
    assert preview.status_code == 200, preview.text
    assert preview.json()['counts']['ready'] == 1
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Availability)) == 0
        assert db.scalar(select(func.count()).select_from(m.RequestRecord)) == 0
    key = 'timeline-bulk-actual-' + kind
    applied = h.command('coordinator', 'bulk.apply', payload, key=key)
    assert h.command('coordinator', 'bulk.apply', payload, key=key) == applied
    with transaction(h.app.state.engine) as db:
        [row] = db.scalars(select(m.Availability))
        absence_id = row.id
        assert row.kind == kind and not row.retracted
        assert db.scalar(select(func.count()).select_from(m.RequestRecord)) == 0
        assert db.scalar(select(func.count()).select_from(m.CommandReceipt).where(m.CommandReceipt.key == key)) == 1
    undo = applied['undo']
    h.command('coordinator', undo['kind'], undo['payload'], undo.get('target_id'), undo.get('expected_revision'))
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Availability, absence_id).retracted
        assert db.scalar(select(func.count()).select_from(m.Availability)) == 1


def test_timeline_routes_use_same_contract_and_filter_before_paging(h):
    pid = h.seed['people'][0]
    params = {'start': '2026-10-01', 'end': '2026-11-01', 'person_ids': [pid]}
    bulk = h.client('coordinator').get('/api/timeline', params=params)
    assert bulk.status_code == 200, bulk.text
    single = h.client('coordinator').get(f'/api/people/{pid}/timeline', params={
        'start': params['start'], 'end': params['end']})
    assert single.status_code == 200, single.text
    assert single.json()['people'] == bulk.json()['people']
    assert single.json()['layers'] == bulk.json()['layers']
    filt = {'conditions': {'conditions': [{'field': 'person_ids', 'values': [pid]}]}}
    filtered = h.client('coordinator').get('/api/timeline', params={
        'start': params['start'], 'end': params['end'], 'filter': json.dumps(filt), 'offset': 1, 'limit': 1})
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()['total'] == 1 and filtered.json()['people'] == []
    h.get('staff', f'/api/people/{h.seed["people"][1]}/timeline?start=2026-10-01&end=2026-11-01', status=403)


def test_legacy_rotation_writes_show_the_shared_approval_warning(h):
    programme = h.configure('programmes', {'name': 'Synthetic warning programme', 'starts_on': '2026-01-01'})['record']
    cohort = h.configure('cohorts', {'name': 'Synthetic warning cohort', 'programme_id': programme['id'],
        'starts_on': '2026-01-01', 'ends_on': '2028-01-01'})['record']
    typ = h.configure('rotation_types', {'name': 'Synthetic approval warning', 'starts_on': '2026-01-01',
        'leave_policy': 'needs_approval'})['record']
    first, second = h.seed['people'][:2]
    for pid in (first, second):
        h.configure('programme_memberships', {'person_id': pid, 'programme_id': programme['id'],
            'starts_on': '2026-01-01', 'ends_on': '2028-01-01'})
        h.command('coordinator', 'availability.record', {'person_id': pid, 'kind': 'off_duty',
            'start': '2026-10-05T08:00:00+03:00', 'end': '2026-10-05T12:00:00+03:00',
            'private_reason': 'Synthetic time away before rotation'})
    value = {'person_id': first, 'cohort_id': cohort['id'], 'rotation_type_id': typ['id'],
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
    made = h.configure('rotations', value)
    changed = h.configure('rotations', {**value, 'ends_on': '2026-10-30'},
        made['record']['id'], made['record']['revision'])
    planned = h.command('coordinator', 'rotation.plan', {'cohort_id': cohort['id'], 'person_ids': [second],
        'starts_on': '2026-10-01', 'blocks': [{'rotation_type_id': typ['id'], 'length': 1, 'unit': 'weeks'}],
        'reason': 'Synthetic rotation warning plan'})
    for reply in (made, changed, planned):
        assert reply['conflicts'][0]['code'] == 'ROTATION_LEAVE_NEEDS_APPROVAL'
        assert reply['warnings'] == [reply['conflicts'][0]['message']]
        assert reply['warnings'][0] in reply['message']


@pytest.mark.parametrize('kind', ['actual_absence', 'external_commitment', 'off_duty'])
def test_actual_facts_remain_recordable_and_undoable_with_timeline_conflict_zones(h, kind):
    from .test_rotation_leave_policy import END, REASON, START, resident
    _, value, rotation = resident(h)
    assert rotation['department_available']
    workspace = h.shift(h.workspace(), start=START, end=END)['workspace']
    publication = h.publish(workspace)['publication_id']
    with transaction(h.app.state.engine) as db:
        issued_fingerprint = db.get(m.Publication, publication).fingerprint
    saved = h.command('coordinator', 'availability.record', {'person_id': value['person_id'], 'kind': kind,
        'start': START, 'end': END, 'private_reason': REASON})
    absence = saved['record']
    assert absence['kind'] == kind and not saved.get('approval_required')

    def timeline_fact(expected_start, *, present=True, duty_blocked=True):
        body = h.get('coordinator', f'/api/people/{value["person_id"]}/timeline?start=2026-10-01&end=2026-11-01')
        person = body['people'][0]
        records = [item for item in person['items'] if item['id'] == absence['id']]
        conflicts = [finding for finding in person['conflicts'] if
                     finding['code'] == 'ROTATION_LEAVE_NOT_ALLOWED' and
                     finding['detail'].get('availability_id') == absence['id']]
        assert bool(records) == present
        assert conflicts == []  # A changed input invalidates the stored publication check.
        if present:
            assert records[0]['details']['availability_kind'] == kind
            assert aware(records[0]['start']) == aware(expected_start)
        assert not person['conflicts']
        current = evaluate(h.snapshot(workspace))['findings']
        assert any(finding['code'] == 'UNAVAILABLE' for finding in current) == duty_blocked
        assert person['duty_density'] == [{'day': '2026-10-04', 'count': 1}]
        assert REASON not in json.dumps(body)
        with transaction(h.app.state.engine) as db:
            assert db.scalar(select(func.count()).select_from(m.RequestRecord)) == 0
            assert db.get(m.Publication, publication).fingerprint == issued_fingerprint

    timeline_fact(START)
    shifted_start, shifted_end = '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00'
    changed = h.command('coordinator', 'availability.update', {'start': shifted_start,
        'end': shifted_end, 'reason': REASON}, absence['id'], absence['revision'])
    timeline_fact(shifted_start, duty_blocked=False)
    undo = changed['undo']
    restored = h.command('coordinator', undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'])['record']
    timeline_fact(START)
    retracted = h.command('coordinator', 'availability.retract', {'reason': REASON}, restored['id'], restored['revision'])
    timeline_fact(START, present=False, duty_blocked=False)
    undo = retracted['undo']
    h.command('coordinator', undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'])
    timeline_fact(START)
    findings = evaluate(h.snapshot(workspace))['findings']
    assert any(finding['code'] == 'ROTATION_LEAVE_NOT_ALLOWED' and finding['severity'] == 'error' for finding in findings)
    assert any(finding['code'] == 'UNAVAILABLE' for finding in findings)


@pytest.mark.parametrize('policy', ['not_allowed', 'needs_approval'])
def test_weekly_availability_keeps_normal_duty_blocking_without_rotation_conflict_zones(h, policy):
    from .test_availability_patterns import pattern
    from .test_rotation_leave_policy import END, START, resident
    _, value, rotation = resident(h, policy)
    assert rotation['department_available']
    workspace = h.shift(h.workspace(), start=START, end=END)['workspace']
    h.publish(workspace)
    saved = h.command('coordinator', 'availability_pattern.propose', pattern(value['person_id'],
        starts_on='2026-10-01', ends_on='2026-11-01'))
    assert saved['pattern']['status'] == 'active' and not saved.get('conflicts')
    person = h.get('coordinator', f'/api/people/{value["person_id"]}/timeline?start=2026-10-01&end=2026-11-01')['people'][0]
    assert any(item['source'] == 'availability_patterns' and item['kind'] == 'time_away' for item in person['items'])
    assert person['conflicts'] == []
    assert not any(finding['code'].startswith('ROTATION_LEAVE_') for finding in person['conflicts'])
    findings = evaluate(h.snapshot(workspace))['findings']
    assert any(finding['code'] == 'UNAVAILABLE' for finding in findings)
    assert not any(finding['code'].startswith('ROTATION_LEAVE_') for finding in findings)


@pytest.mark.parametrize('kind', ['actual_absence', 'external_commitment', 'off_duty'])
def test_actual_fact_is_excluded_from_cp_sat_candidates_despite_the_rotation_conflict(h, kind):
    from packages.domain.cp_sat import generate
    from .test_rotation_leave_policy import END, REASON, START, resident
    _, value, rotation = resident(h)
    assert rotation['department_available']
    h.command('coordinator', 'availability.record', {'person_id': value['person_id'], 'kind': kind,
        'start': START, 'end': END, 'private_reason': REASON})
    created = h.shift(h.workspace(), start=START, end=END, people=[])
    variables = set()
    result = generate(h.snapshot(created['workspace']), 5, feasibility_only=True,
        _on_model=lambda model: variables.update(variable.name for variable in model.proto.variables))
    assert result['status'] == 'model_ready', result
    choice = f'{created["shift_id"]}:'
    assert any(variable.startswith(choice) for variable in variables), 'Other eligible people remain candidates'
    assert choice + value['person_id'] not in variables

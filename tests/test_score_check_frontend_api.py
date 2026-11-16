"""The Upload → Check contract delegates writes to the ordinary import workflow."""
import io
import csv
import uuid

import pytest

pytestmark = pytest.mark.usefixtures('score_check_enabled')
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .governance_support import identity
from .test_score_check_ai import enable, profile


def dto(value):
    return {key: item for key, item in value.items() if key not in {'activity', 'receipt_id'}}


def upload(h, rows=None, *, role='coordinator', status=200, key=None):
    rows = rows or [['Alex Morgan', '2026-10-04', 'Clinical review session', 'Continuity clinic', '08:00', '12:00']]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['Group', 'Residents'])
    writer.writerow(['Person', 'Date', 'Code', 'Clinic', 'Start', 'End'])
    writer.writerows(rows)
    response = h.client(role).post('/api/score-imports',
        headers={'idempotency-key': key or str(uuid.uuid4())},
        files=[('files', ('residents.csv', out.getvalue().encode(), 'text/csv'))])
    assert response.status_code == status, response.text
    return dto(response.json())


def change(h, value, action, *, status=200, role='coordinator', **payload):
    return dto(h.command(role, 'score_import.' + action, {'import_id': value['id'], **payload},
                        revision=value['revision'], status=status))


def check(h, value):
    return h.get('coordinator', '/api/score-imports/' + value['id'] + '/check')


def decide(h, value, item, decision, **kwargs):
    return change(h, value, 'decide', item_id=item['id'], decision=decision, **kwargs)


def current(h, value):
    return h.get('coordinator', '/api/score-imports/' + value['id'])


def test_upload_has_one_public_multipart_contract(h):
    client = h.client('coordinator')
    for kind in ('score_import.upload', 'score_import.add_files', 'score_check.upload'):
        assert h.command('coordinator', kind, {}, status=422)['error']['code'] == 'UPLOAD_MULTIPART'
    for path in ('/api/score-imports', '/api/score-imports/unknown/sources'):
        response = client.post(path, json={'files': []}, headers={'idempotency-key': str(uuid.uuid4())})
        assert response.status_code == 415 and response.json()['error']['code'] == 'UPLOAD_MULTIPART'
    assert client.post('/api/score-checks/upload', json={}).status_code == 405
    paths = h.app.openapi()['paths']
    for path in ('/api/score-imports', '/api/score-imports/{import_id}/sources'):
        assert set(paths[path]['post']['requestBody']['content']) == {'multipart/form-data'}
    assert '/api/score-checks/upload' not in paths


def test_import_frontend_metadata_time_and_evidence_are_canonical(h):
    value = upload(h)
    assert set(value) == {'id', 'revision', 'saved_workspace_id', 'sources', 'time', 'profile_id', 'groups'}
    assert s.ScoreImport.model_validate(value).time is None
    source, = value['sources']
    assert source == {'id': source['id'], 'file_name': 'residents.csv', 'format': 'csv',
        'group': 'Residents', 'period_start': '2026-10-04', 'period_end': '2026-10-05', 'period_inferred': True,
        'people_count': 1, 'status': 'ready', 'unclear_cells': 0, 'error': None}
    assert value['groups'] == ['Residents']
    value = change(h, value, 'update_source', source_id=source['id'], group='Visiting team', people_count=17,
                   period_start='2026-10-01', period_end='2026-11-01')
    assert value['sources'][0]['group'] == 'Visiting team'
    assert value['sources'][0]['people_count'] == 17
    assert value['sources'][0]['period_inferred'] is False
    value = change(h, value, 'update_source', source_id=source['id'], period_end='2026-10-31')
    assert value['sources'][0]['period_start'] == '2026-10-01'
    assert value['sources'][0]['period_end'] == '2026-10-31'
    value = change(h, value, 'time', time={'work_hours': 40, 'people': 4})
    assert value['time'] == {'work_hours': 40, 'people': 4}
    assert current(h, value) == value
    with transaction(h.app.state.engine) as db:
        batch = db.get(m.ScoreCheckImportBatch, value['id'])
        stored = db.get(m.ImportSource, source['id'])
        assert batch.manual_time_hours == 40 and batch.manual_time_people == 4
        assert len(stored.extraction['people']) == 1 and stored.content
    assert change(h, value, 'time', time=None)['time'] is None


def test_import_source_remove_restore_discard_preserve_raw_evidence_and_revision(h):
    value = upload(h)
    source = value['sources'][0]
    removed = change(h, value, 'remove_source', source_id=source['id'])
    assert removed['sources'] == [] and removed['revision'] == value['revision'] + 1
    assert change(h, value, 'restore_source', source_id=source['id'], status=409)['error']['code'] == 'STALE_REVISION'
    restored = change(h, removed, 'restore_source', source_id=source['id'])
    assert restored['sources'][0] == source
    discarded = change(h, restored, 'discard')
    assert discarded['sources'][0] == source
    assert change(h, discarded, 'time', time={'work_hours': 1, 'people': 1}, status=409)['error']['code'] == 'IMPORT_STATE'
    restored = change(h, discarded, 'restore')
    assert restored['sources'][0] == source
    with transaction(h.app.state.engine) as db:
        row = db.get(m.ImportSource, source['id'])
        assert row.removed_at is None and row.content and row.extraction
        assert db.get(m.ScoreCheckImportBatch, value['id']).status == 'check'
        actions = list(db.scalars(select(m.AuditEvent.action).where(m.AuditEvent.action.like('score_import.%'))))
        assert {'score_import.remove_source', 'score_import.restore_source', 'score_import.discard', 'score_import.restore'} <= set(actions)


def test_frontend_check_flattens_confident_unknown_unsure_and_cells_and_can_undo(h):
    value = upload(h, [
        ['Alex Morgan', '2026-10-04', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
        ['Jordn Lee', '2026-10-05', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
        ['Unknown Visitor', '2026-10-06', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
        ['Alex Morgan', 'soon', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
    ])
    result = check(h, value)
    assert s.ImportCheck.model_validate(result).matched == 2
    assert result['threshold'] == .9
    assert {item['section'] for item in result['items']} == {'missing', 'unknown', 'unsure', 'cell'}
    assert all(value['sources'][0]['file_name'] in item['detail'] for item in result['items'] if item['section'] != 'missing')
    assert all(item['reading_context']['cell_ref'] in item['detail'] for item in result['items'] if item['section'] == 'cell')
    unsure = next(item for item in result['items'] if item['section'] == 'unsure' and item['kind'] == 'person')
    assert unsure['as_written'] == 'Jordn Lee' and unsure['suggested']['label'] == 'Jordan Lee'
    result = decide(h, value, unsure, {'kind': 'confirm', 'target_id': h.seed['people'][0]})
    chosen = next(item for item in result['items'] if item['id'] == unsure['id'])
    assert chosen['decision']['kind'] == 'match'
    value = current(h, value)
    result = decide(h, value, unsure, None)
    assert next(item for item in result['items'] if item['id'] == unsure['id'])['decision'] is None
    value = current(h, value)
    cell = next(item for item in result['items'] if item['section'] == 'cell')
    result = decide(h, value, cell, {'kind': 'skip'})
    assert next(item for item in result['items'] if item['id'] == cell['id'])['decision']['kind'] == 'skip'
    assert current(h, value)['sources'][0]['unclear_cells'] == 0


def test_frontend_missing_decisions_gate_save_and_saved_workspace_uses_existing_writer(h):
    value = upload(h)
    assert change(h, value, 'save', status=409)['error']['code'] == 'IMPORT_ALARMS'
    for item in check(h, value)['items']:
        if item['kind'] == 'clinic':
            decide(h, value, item, {'kind': 'confirm', 'location_id': h.seed['locations'][0]})
            value = current(h, value)
    for item in check(h, value)['items']:
        if item['section'] == 'missing':
            decide(h, value, item, {'kind': 'acknowledge'})
            value = current(h, value)
    result = change(h, value, 'save')
    assert set(result) == {'workspace_id'}
    assert current(h, value)['saved_workspace_id'] == result['workspace_id']
    workspace = h.detail(result['workspace_id'])
    assert workspace['workspace']['origin'] == 'import'
    assert workspace['workspace']['status'] == 'draft'
    assert len(workspace['shifts']) == 1


def test_frontend_import_writes_and_reads_require_coordinator(h):
    value = upload(h)
    assert h.get('admin', '/api/score-imports/' + value['id']) == value
    for role in s.ROLE_NAMES:
        if role in {'coordinator', 'admin'}:
            continue
        if role not in h.seed['identities']:
            identity(h, role, [role])
        upload(h, role=role, status=403)
        h.get(role, '/api/score-imports/' + value['id'], status=403)
        h.get(role, '/api/score-imports/' + value['id'] + '/check', status=403)
        change(h, value, 'discard', role=role, status=403)


def test_frontend_add_files_is_revision_checked_and_upload_replays_one_receipt(h):
    key = str(uuid.uuid4())
    value = upload(h, key=key)
    assert upload(h, key=key) == value
    content = b'Person,Date,Code,Clinic,Start,End\nJordan Lee,2026-10-05,Clinical review session,Continuity clinic,08:00,12:00\n'
    client = h.client('coordinator')
    url = '/api/score-imports/' + value['id'] + '/sources'
    assert client.post(url, files={'files': ('fellows.csv', content, 'text/csv')},
        headers={'idempotency-key': str(uuid.uuid4())}).status_code == 428
    response = client.post(url, data={'revision': str(value['revision'])},
        files={'files': ('fellows.csv', content, 'text/csv')}, headers={'idempotency-key': str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    updated = response.json()
    assert len(updated['sources']) == 2 and updated['revision'] == value['revision'] + 1
    result = change(h, updated, 'check')
    assert result['import_id'] == value['id'] and result['revision'] == updated['revision'] + 1
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.ScoreCheckImportBatch)))) == 1
        assert len(list(db.scalars(select(m.ImportSource)))) == 2


def test_frontend_profile_requires_fresh_consent_and_affects_only_future_files(h):
    value = upload(h)
    configured = profile(h, enabled=True)
    enable(h)
    change(h, value, 'profile', profile_id=configured['id'], profile_revision=configured['revision'], status=422)
    value = change(h, value, 'profile', profile_id=configured['id'], profile_revision=configured['revision'],
                   privacy_acknowledged=True)
    assert value['profile_id'] == configured['id'] and value['sources'][0]['status'] == 'ready'
    with transaction(h.app.state.engine, write=True) as db:
        assert db.scalar(select(m.ScoreCheckExtractionJob)) is None
        stored = db.get(m.AiProviderProfile, configured['id'])
        stored.revision += 1
    change(h, value, 'profile', profile_id=configured['id'], profile_revision=configured['revision'],
           privacy_acknowledged=True, status=409)
    client = h.client('coordinator')
    response = client.post('/api/score-imports/' + value['id'] + '/sources',
        data={'revision': str(value['revision']), 'profile_revision': str(configured['revision']), 'privacy_acknowledged': 'true'},
        files={'files': ('next.csv', b'Person,Date,Code\nAlex Morgan,2026-10-05,AM\n', 'text/csv')},
        headers={'idempotency-key': str(uuid.uuid4())})
    assert response.status_code == 409, response.text
    assert change(h, value, 'profile', profile_id=None)['profile_id'] is None


def test_frontend_decisions_require_valid_catalogue_values_and_explicit_cell_reading(h):
    value = upload(h, [
        ['Unknown Visitor', '2026-10-04', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
        ['Alex Morgan', 'soon', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
    ])
    result = check(h, value)
    unknown = next(item for item in result['items'] if item['section'] == 'unknown')
    decide(h, value, unknown, {'kind': 'add'}, status=422)
    # Admin-only person fields are refused, not silently dropped (score_check.add_missing creates a name only).
    decide(h, value, unknown, {'kind': 'add', 'value': {'display_name': 'Unknown Visitor', 'staff_number': 'x1'}}, status=422)
    result = decide(h, value, unknown, {'kind': 'add', 'value': {'display_name': 'Unknown Visitor'}})
    assert next(item for item in result['items'] if item['id'] == unknown['id'])['decision']['kind'] == 'add'
    value = current(h, value)
    decide(h, value, unknown, None)
    value = current(h, value)
    cell = next(item for item in check(h, value)['items'] if item['section'] == 'cell')
    decide(h, value, cell, {'kind': 'confirm', 'target_id': h.seed['people'][0]}, status=422)
    reading = {**cell['reading_context'], 'person': 'Alex Morgan',
        'date': '2026-10-04', 'duty_code': 'Clinical review session', 'clinic': 'Continuity clinic',
        'start': '13:00', 'end': '17:00'}
    result = decide(h, value, cell, {'kind': 'confirm', 'reading': reading})
    assert next(item for item in result['items'] if item['id'] == cell['id'])['decision']['kind'] == 'confirm'
    assert current(h, value)['sources'][0]['unclear_cells'] == 0


def test_frontend_clinic_match_requires_a_scoped_location(h):
    value = upload(h, [['Alex Morgan', '2026-10-04', 'Clinical review session', 'Unknown clinic', '08:00', '12:00']])
    item = next(item for item in check(h, value)['items'] if item['kind'] == 'clinic')
    decision = {'kind': 'match', 'target_id': h.seed['services'][0]}
    assert decide(h, value, item, decision, status=422)['error']['code'] == 'IMPORT_LOCATION'
    result = decide(h, value, item, {**decision, 'location_id': h.seed['locations'][0]})
    assert next(row for row in result['items'] if row['id'] == item['id'])['decision']['kind'] == 'match'


def test_coordinator_can_add_a_clinic_through_check_and_undo_it(h):
    value = upload(h, [['Alex Morgan', '2026-10-04', 'Clinical review session', 'Unknown clinic', '08:00', '12:00']])
    item = next(item for item in check(h, value)['items'] if item['kind'] == 'clinic')
    result = decide(h, value, item, {'kind': 'add', 'value': {'name': 'Unknown clinic', 'starts_on': '2026-10-01'},
        'location_id': h.seed['locations'][0]})
    assert next(row for row in result['items'] if row['id'] == item['id'])['decision']['kind'] == 'add'
    value = current(h, value)
    with transaction(h.app.state.engine) as db:
        decision = db.scalar(select(m.ScoreCheckGapDecision).where(m.ScoreCheckGapDecision.batch_id == value['id'],
            m.ScoreCheckGapDecision.gap_id == item['id'], m.ScoreCheckGapDecision.undone_at.is_(None)))
        clinic = db.get(m.Service, decision.target_id)
        assert clinic.name == 'Unknown clinic' and clinic.department_id == h.seed['department_id']
    result = decide(h, value, item, None)
    assert next(row for row in result['items'] if row['id'] == item['id'])['decision'] is None


@pytest.mark.parametrize('legacy', [False, True], ids=['canonical', 'legacy-core'])
def test_unknown_duty_codes_cannot_create_duty_types_from_check(h, legacy):
    value = upload(h, [['Alex Morgan', '2026-10-04', 'Unknown rota code', 'Continuity clinic', '08:00', '12:00']])
    item = next(item for item in check(h, value)['items'] if item['kind'] == 'duty_code')
    with transaction(h.app.state.engine) as db:
        before = list(db.scalars(select(m.AssignmentType.id)))
        definition = db.get(m.AssignmentType, h.seed['type']).definition
    replacement = {'name': 'Unknown rota code', 'starts_on': '2026-10-01', 'definition': definition}
    if legacy:
        response = h.command('coordinator', 'score_check.decide', {'batch_id': value['id'], 'gap_id': item['id'],
            'action': 'add', 'replacement': replacement}, revision=value['revision'], status=422)
    else:
        response = decide(h, value, item, {'kind': 'add', 'value': replacement}, status=422)
    assert response['error']['code'] == 'GAP_ACTION'
    assert 'Configuration' in response['error']['message']
    assert current(h, value)['revision'] == value['revision']
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.AssignmentType.id))) == before


def test_frontend_reading_profile_time_and_restore_never_resubmit_finished_jobs(h):
    value = upload(h)
    configured = profile(h, enabled=True)
    enable(h)
    source_id = value['sources'][0]['id']
    with transaction(h.app.state.engine, write=True) as db:
        job = m.ScoreCheckExtractionJob(department_id=h.seed['department_id'], source_id=source_id,
            profile_id=configured['id'], requested_by=h.seed['identities']['coordinator'], kind='extraction')
        db.add(job)
        db.flush()
        job_id = job.id
        source = db.get(m.ImportSource, source_id)
        source.status, source.extraction, source.extraction_job_id = 'reading', None, job_id
        db.get(m.ScoreCheckImportBatch, value['id']).status = 'reading'
    value = change(h, value, 'profile', profile_id=configured['id'], profile_revision=configured['revision'],
                   privacy_acknowledged=True)
    assert value['sources'][0]['status'] == 'reading'
    value = change(h, value, 'time', time={'work_hours': 2, 'people': 1})
    assert value['time'] == {'work_hours': 2, 'people': 1}
    value = change(h, value, 'remove_source', source_id=source_id)
    with transaction(h.app.state.engine, write=True) as db:
        job = db.get(m.ScoreCheckExtractionJob, job_id)
        job.state, job.error_code = 'failed', 'SCORE_SOURCE_CHANGED'
    value = change(h, value, 'restore_source', source_id=source_id)
    assert value['sources'][0]['status'] == 'failed'
    assert value['sources'][0]['error'] == 'SCORE_SOURCE_CHANGED'
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.ScoreCheckExtractionJob, job_id).state = 'queued'
        db.get(m.ImportSource, source_id).status = 'reading'
        db.get(m.ScoreCheckImportBatch, value['id']).status = 'reading'
    value = change(h, value, 'discard')
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.ScoreCheckExtractionJob, job_id).state = 'failed'
    value = change(h, value, 'restore')
    assert value['sources'][0]['status'] == 'failed'
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.ScoreCheckExtractionJob)))) == 1
        assert db.get(m.ImportSource, source_id).content

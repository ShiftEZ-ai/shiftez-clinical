"""Uploaded rota evidence stays scoped, reviewable and subject to ordinary approval."""
import base64
import copy
import csv
import io
import uuid
from datetime import date

import pytest

pytestmark = pytest.mark.usefixtures('score_check_enabled')
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.commands import execute
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import catalogue, score_check
from packages.contracts import schemas as s
from .governance_support import identity


def internal_upload(h, payload, *, role='coordinator', status=200, key=None):
    client = h.client(role)
    try:
        result = execute(h.app.state.engine, h.settings, client.cookies.get('rota_session'),
            client.headers.get('x-csrf-token'), key or str(uuid.uuid4()),
            s.Command(kind='score_check.upload', payload=payload), str(uuid.uuid4()))
    except Problem as error:
        assert error.status == status, error.message
        return {'error': {'code': error.code, 'message': error.message}}
    assert status == 200, result
    return result


def csv_upload(h, rows=None, *, role='coordinator', status=200, key=None, text=None, **metadata):
    catalogues = h.get('coordinator', '/api/catalogues')
    person = next(p for p in catalogues['people'] if p['id'] == h.seed['people'][0])
    typ = next(t for t in catalogues['assignment_types'] if t['id'] == h.seed['type'])
    service = next(c for c in catalogues['services'] if c['id'] == h.seed['services'][0])
    rows = rows if rows is not None else [[person['display_name'], '2026-10-04', typ['name'], service['name'], '08:00', '12:00']]
    content = io.StringIO()
    writer = csv.writer(content)
    writer.writerow(['Group', 'Residents'])
    writer.writerow(['Person', 'Date', 'Code', 'Clinic', 'Start', 'End'])
    writer.writerows(rows)
    payload = {'files': [{'file_name': 'residents.csv',
        'content_base64': base64.b64encode((content.getvalue() if text is None else text).encode()).decode()}], **metadata}
    return internal_upload(h, payload, role=role, status=status, key=key)


def change(h, batch, operation, *, status=200, role='coordinator', **payload):
    return h.command(role, 'score_check.' + operation, {'batch_id': batch['id'], **payload},
                     revision=batch['revision'], status=status)


def decide(h, batch, gap, action, **payload):
    return change(h, batch, 'decide', gap_id=gap['id'], action=action, **payload)['batch']


def gap_for(batch, label, kind=None):
    return next(g for g in batch['gaps'] if g['as_written'] == label and (kind is None or g['kind'] == kind))


def acknowledge_missing(h, batch):
    for gap in batch['gaps']:
        if gap['kind'] == 'missing' and not gap.get('decision'):
            batch = decide(h, batch, gap, 'acknowledge')
    return batch


def mixed_upload(h, **metadata):
    return csv_upload(h, [
        ['Alex Morgan', '2026-10-04', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
        ['Jordn Lee', '2026-10-05', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
        ['Unlisted Visitor', '2026-10-06', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
        ['Alex Morgan', 'soon', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
    ], **metadata)['batch']


def catalogue_actor(h, role):
    return Actor(h.seed['identities'][role], h.seed['department_id'], h.seed['organisation_id'],
                 f'Synthetic {role}', frozenset({role}), None, 'synthetic-session')


def test_score_check_catalogue_permission_key_is_rejected(h):
    command = s.Command(kind='catalogue.create', payload={'catalogue': 'people',
        'value': {'display_name': 'Synthetic invalid permission'}, 'reason': 'Boundary test'})
    with transaction(h.app.state.engine, write=True) as db:
        with pytest.raises(Problem) as refused:
            catalogue.write_catalogue(db, catalogue_actor(h, 'coordinator'), command, permission='catalogue.department')
    assert refused.value.status == 403 and refused.value.code == 'FORBIDDEN'


def test_score_check_catalogue_permission_rejects_another_catalogue(h):
    command = s.Command(kind='catalogue.create', payload={'catalogue': 'assignment_types',
        'value': {}, 'reason': 'Boundary test'})
    with transaction(h.app.state.engine, write=True) as db:
        with pytest.raises(Problem) as refused:
            catalogue.write_catalogue(db, catalogue_actor(h, 'coordinator'), command,
                                     permission='score_check.add_missing')
    assert refused.value.status == 403 and refused.value.code == 'FORBIDDEN'


def test_score_check_catalogue_permission_rejects_updates(h):
    command = s.Command(kind='catalogue.update', target_id=h.seed['people'][0], expected_revision=1,
        payload={'catalogue': 'people', 'value': {'display_name': 'Synthetic update'}, 'reason': 'Boundary test'})
    with transaction(h.app.state.engine, write=True) as db:
        with pytest.raises(Problem) as refused:
            catalogue.write_catalogue(db, catalogue_actor(h, 'coordinator'), command, update=True,
                                     permission='score_check.add_missing')
    assert refused.value.status == 403 and refused.value.code == 'FORBIDDEN'


def test_catalogue_creation_undo_without_permission_is_rejected(h):
    command = s.Command(kind='catalogue.undo_create', target_id=h.seed['people'][0], expected_revision=1,
        payload={'catalogue': 'people', 'reason': 'Boundary test'})
    with transaction(h.app.state.engine, write=True) as db:
        with pytest.raises(Problem) as refused:
            catalogue.undo_catalogue_created(db, catalogue_actor(h, 'staff'), command, permission=None)
    assert refused.value.status == 403 and refused.value.code == 'FORBIDDEN'


def test_score_check_add_permission_is_exposed_from_the_shared_catalogue(h):
    assert 'score_check.add_missing' in h.get('coordinator', '/api/me')['permissions']
    assert 'score_check.add_missing' not in h.get('staff', '/api/me')['permissions']


@pytest.mark.parametrize('entity_kind', ['person', 'clinic'])
def test_score_check_add_requires_the_named_permission_for_people_and_services(h, monkeypatch, entity_kind):
    rows = ([['Unlisted Visitor', '2026-10-06', 'Clinical review session', 'Continuity clinic', '08:00', '12:00']]
            if entity_kind == 'person' else
            [['Alex Morgan', '2026-10-06', 'Clinical review session', 'Unlisted clinic', '08:00', '12:00']])
    batch = csv_upload(h, rows)['batch']
    gap = gap_for(batch, 'Unlisted Visitor' if entity_kind == 'person' else 'Unlisted clinic')
    replacement = {'display_name': 'Unlisted Visitor'} if entity_kind == 'person' else {
        'name': 'Unlisted clinic', 'starts_on': '2026-10-04'}
    monkeypatch.setitem(s.PERMISSIONS, 'score_check.add_missing', ('admin',))
    refused = change(h, batch, 'decide', gap_id=gap['id'], action='add', replacement=replacement, status=403)
    assert refused['error']['code'] == 'FORBIDDEN'


def foreign_catalogues(h):
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Separate synthetic department',
                                  score_check_enabled=True)
        db.add(department)
        db.flush()
        effective = {'department_id': department.id, 'starts_on': date(2020, 1, 1)}
        category = m.Category(name='Foreign synthetic category', **effective)
        person = m.Person(organisation_id=h.seed['organisation_id'], display_name='Foreign department staff')
        service = m.Service(name='Foreign synthetic clinic', **effective)
        typ = m.AssignmentType(name='Foreign synthetic duty', definition={}, **effective)
        location_type = m.LocationType(name='Foreign synthetic room kind', **effective)
        db.add_all([category, person, service, typ, location_type])
        db.flush()
        location = m.Location(name='Foreign synthetic room', location_type_id=location_type.id, capacity=2, **effective)
        db.add(location)
        db.add(m.Membership(person_id=person.id, category_id=category.id, **effective))
        db.flush()
        return {'department': department.id, 'person_id': person.id, 'service_id': service.id,
                'type_id': typ.id, 'location_id': location.id}


def test_upload_infers_metadata_keeps_evidence_private_and_gap_ids_stable(h):
    batch = mixed_upload(h)
    assert (batch['starts_on'], batch['ends_on']) == ('2026-10-04', '2026-10-07')
    assert batch['status'] == 'check' and batch['can_save'] is False
    source, = batch['files']
    assert source['group'] == 'Residents' and source['format'] == 'csv'
    assert source['result']['assignment_count'] == 3
    assert source['status'] == 'needs_review'
    assert 'content' not in source and 'extraction' not in source
    assert batch['confident_matches'] == 2  # A clinic still needs its location before automatic acceptance.
    assert gap_for(batch, 'Alex Morgan', 'uncertain')['confidence'] == 1
    assert gap_for(batch, 'Jordn Lee')['kind'] == 'uncertain'
    assert gap_for(batch, 'Unlisted Visitor')['kind'] == 'unknown'
    assert any(g['kind'] == 'unreadable' and g['cell_refs'] == ['CSV!B6'] for g in batch['gaps'])
    assert gap_for(batch, 'Taylor Noor')['kind'] == 'missing'
    assert set(g['kind'] for g in batch['gaps']) == {'unknown', 'uncertain', 'unreadable', 'missing'}
    ids = {g['id'] for g in batch['gaps']}
    edited = change(h, batch, 'update', name='Reviewed department rota',
        files=[{'source_id': source['id'], 'group': 'Fellows', 'work_hours_each': 2.5,
                'made_by': h.seed['people'][:2]}])['batch']
    assert {g['id'] for g in edited['gaps']} == ids
    assert edited['files'][0]['group'] == 'Fellows'
    assert edited['files'][0]['work_hours_each'] == 2.5
    assert h.get('coordinator', '/api/score-checks/' + batch['id'])['batch'] == edited
    assert [b['id'] for b in h.get('coordinator', '/api/score-checks')['batches']] == [batch['id']]


def test_missing_acknowledgement_is_required_and_every_decision_can_be_undone(h):
    batch = mixed_upload(h)
    original_counts = batch['counts']
    refused = change(h, batch, 'save', status=409)
    assert refused['error']['code'] == 'IMPORT_ALARMS'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Workspace)) == 0
    decisions = []
    for gap in batch['gaps']:
        if gap['kind'] == 'missing':
            batch = decide(h, batch, gap, 'acknowledge')
            decisions.append((gap['id'], next(g for g in batch['gaps'] if g['id'] == gap['id'])['decision']['id']))
    assert batch['counts']['missing'] == 0 and batch['can_save'] is True
    for gap, action, extra in [(gap_for(batch, 'Jordn Lee'), 'confirm', {}),
            (gap_for(batch, 'Unlisted Visitor'), 'exclude', {}),
            (next(g for g in batch['gaps'] if g['kind'] == 'unreadable'), 'exclude', {}),
            (gap_for(batch, 'Continuity clinic'), 'confirm', {'location_id': h.seed['locations'][0]})]:
        batch = decide(h, batch, gap, action, **extra)
        decisions.append((gap['id'], next(g for g in batch['gaps'] if g['id'] == gap['id'])['decision']['id']))
    assert batch['counts']['unresolved'] == 0
    for gap_id, decision_id in reversed(decisions):
        batch = change(h, batch, 'undo', decision_id=decision_id)['batch']
        assert next(g for g in batch['gaps'] if g['id'] == gap_id).get('decision') is None
    assert batch['counts'] == original_counts and batch['can_save'] is False


def test_unknown_person_add_match_exclude_and_undo_do_not_grant_clinical_authority(h):
    batch = mixed_upload(h)
    unknown = gap_for(batch, 'Unlisted Visitor')
    before = h.get('admin', '/api/catalogues')['people']
    batch = decide(h, batch, unknown, 'add', replacement={'display_name': 'Unlisted Visitor'})
    decision = gap_for(batch, 'Unlisted Visitor')['decision']
    assert decision['action'] == 'add' and decision['can_undo']
    with transaction(h.app.state.engine) as db:
        added = db.get(m.Person, decision['target_id'])
        history = db.scalar(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == 'people', m.DefinitionHistory.object_id == added.id))
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'score_check.decide').order_by(
            m.AuditEvent.created_at.desc(), m.AuditEvent.seq.desc()))
        assert added.display_name == 'Unlisted Visitor'
        assert history.actor_id == h.seed['identities']['coordinator']
        assert audit.actor_id == h.seed['identities']['coordinator']
        assert db.scalar(select(func.count()).select_from(m.CapabilityGrant).where(m.CapabilityGrant.person_id == added.id)) == 0
        assert db.scalar(select(func.count()).select_from(m.Eligibility).where(m.Eligibility.person_id == added.id)) == 0
        assert db.scalar(select(func.count()).select_from(m.Identity).where(m.Identity.person_id == added.id)) == 0
    batch = change(h, batch, 'undo', decision_id=decision['id'])['batch']
    assert h.get('admin', '/api/catalogues')['people'] == before
    batch = decide(h, batch, gap_for(batch, 'Unlisted Visitor'), 'match', target_id=h.seed['people'][2])
    matched = gap_for(batch, 'Unlisted Visitor')['decision']
    assert matched['target_id'] == h.seed['people'][2]
    assert batch['counts']['unknown'] == 0
    batch = change(h, batch, 'undo', decision_id=matched['id'])['batch']
    batch = decide(h, batch, gap_for(batch, 'Unlisted Visitor'), 'exclude')
    assert gap_for(batch, 'Unlisted Visitor')['decision']['action'] == 'exclude'


def test_score_check_add_undo_refuses_another_coordinator(h):
    batch = mixed_upload(h)
    batch = decide(h, batch, gap_for(batch, 'Unlisted Visitor'), 'add',
                   replacement={'display_name': 'Unlisted Visitor'})
    decision = gap_for(batch, 'Unlisted Visitor')['decision']
    identity(h, 'other-coordinator', ['coordinator'])
    refused = change(h, batch, 'undo', role='other-coordinator', decision_id=decision['id'], status=403)
    assert refused['error']['code'] == 'FORBIDDEN'


def test_score_check_add_undo_refuses_an_edited_record(h):
    batch = mixed_upload(h)
    batch = decide(h, batch, gap_for(batch, 'Unlisted Visitor'), 'add',
                   replacement={'display_name': 'Unlisted Visitor'})
    decision = gap_for(batch, 'Unlisted Visitor')['decision']
    person = next(row for row in h.get('coordinator', '/api/catalogues')['people'] if row['id'] == decision['target_id'])
    h.configure('people', {'display_name': 'Edited visitor', 'active': person['active']}, person['id'], person['revision'],
                role='coordinator')
    refused = change(h, batch, 'undo', decision_id=decision['id'], status=409)
    assert refused['error']['code'] == 'STALE_REVISION'


def test_score_check_add_undo_refuses_a_referenced_record(h):
    batch = mixed_upload(h)
    batch = decide(h, batch, gap_for(batch, 'Unlisted Visitor'), 'add',
                   replacement={'display_name': 'Unlisted Visitor'})
    decision = gap_for(batch, 'Unlisted Visitor')['decision']
    workspace = h.workspace()
    h.shift(workspace, people=[decision['target_id']])
    refused = change(h, batch, 'undo', decision_id=decision['id'], status=409)
    assert refused['error']['code'] == 'INTEGRITY_CONFLICT'


@pytest.mark.parametrize('label,replacement', [
    ('Unlisted Visitor', {'name': 'Mismatched clinic', 'starts_on': '2026-10-04'}),
    ('Unlisted clinic', {'display_name': 'Mismatched person'}),
])
def test_score_check_add_rejects_a_replacement_for_the_wrong_gap_kind(h, label, replacement):
    rows = ([['Unlisted Visitor', '2026-10-06', 'Clinical review session', 'Continuity clinic', '08:00', '12:00']]
            if label == 'Unlisted Visitor' else
            [['Alex Morgan', '2026-10-06', 'Clinical review session', 'Unlisted clinic', '08:00', '12:00']])
    batch = csv_upload(h, rows)['batch']
    gap = gap_for(batch, label)
    refused = change(h, batch, 'decide', gap_id=gap['id'], action='add', replacement=replacement, status=422)
    assert refused['error']['code'] == 'SCHEMA_INVALID'
    assert h.get('coordinator', '/api/score-checks/' + batch['id'])['batch']['revision'] == batch['revision']


def test_score_check_add_rejects_admin_only_replacement_fields(h):
    batch = mixed_upload(h)
    gap = gap_for(batch, 'Unlisted Visitor')
    refused = change(h, batch, 'decide', gap_id=gap['id'], action='add',
                     replacement={'display_name': 'Unlisted Visitor', 'active': False}, status=422)
    assert refused['error']['code'] == 'SCHEMA_INVALID'
    assert h.get('coordinator', '/api/score-checks/' + batch['id'])['batch']['revision'] == batch['revision']


def test_resolve_checks_original_cell_and_period_before_acceptance(h):
    batch = mixed_upload(h, starts_on='2026-10-04', ends_on='2026-10-11')
    gap = next(g for g in batch['gaps'] if g['kind'] == 'unreadable')
    replacement = {'file_ref': gap['file_ref'], 'cell_ref': gap['cell_refs'][0],
        'person': 'Alex Morgan', 'date': '2026-10-04', 'duty_code': 'Clinical review session',
        'clinic': 'Continuity clinic', 'start': '13:00', 'end': '17:00'}
    for field, value, code in [('cell_ref', 'CSV!Z999', 'EXTRACTION_CELL'),
                               ('date', '2026-11-01', 'EXTRACTION_PERIOD')]:
        refused = change(h, batch, 'decide', gap_id=gap['id'], action='resolve',
                         replacement={**replacement, field: value}, status=422)
        assert refused['error']['code'] == code
        assert h.get('coordinator', '/api/score-checks/' + batch['id'])['batch']['revision'] == batch['revision']
    resolved = decide(h, batch, gap, 'resolve', replacement=replacement)
    assert resolved['counts']['unreadable'] == 0
    assert resolved['counts']['unresolved'] == batch['counts']['unresolved'] - 1
    decision = next(g for g in resolved['gaps'] if g['id'] == gap['id'])['decision']
    restored = change(h, resolved, 'undo', decision_id=decision['id'])['batch']
    assert restored['counts'] == batch['counts']


def test_decisions_require_current_revision_valid_action_and_no_blind_overwrite(h):
    batch = mixed_upload(h)
    gap = gap_for(batch, 'Unlisted Visitor')
    assert change(h, batch, 'decide', gap_id=gap['id'], action='acknowledge', status=422)['error']['code'] == 'GAP_ACTION'
    assert change(h, batch, 'decide', gap_id=gap['id'], action='match', status=422)['error']['code'] == 'SCHEMA_INVALID'
    updated = decide(h, batch, gap, 'exclude')
    assert change(h, batch, 'update', name='Stale update', status=409)['error']['code'] == 'STALE_REVISION'
    assert change(h, updated, 'decide', gap_id=gap['id'], action='exclude', status=409)['error']['code'] == 'GAP_DECIDED'


@pytest.mark.parametrize('role', [r for r in s.ROLE_NAMES if r not in {'admin', 'coordinator'}])
def test_import_routes_and_commands_refuse_every_other_role(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    batch = csv_upload(h)['batch']
    assert csv_upload(h, role=role, status=403)['error']['code'] == 'FORBIDDEN'
    h.get(role, '/api/score-checks', status=403)
    h.get(role, '/api/score-checks/' + batch['id'], status=403)
    for action, payload in [('update', {'name': 'Unauthorized'}), ('check', {}), ('save', {}),
            ('decide', {'gap_id': batch['gaps'][0]['id'], 'action': 'exclude'}),
            ('undo', {'decision_id': str(uuid.uuid4())})]:
        change(h, batch, action, role=role, status=403, **payload)


@pytest.mark.parametrize('role', ['admin', 'coordinator'])
def test_upload_uses_command_bus_idempotency_and_effective_planning_permissions(h, role):
    key = str(uuid.uuid4())
    first = csv_upload(h, role=role, key=key)['batch']
    assert csv_upload(h, role=role, key=key)['batch'] == first
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.ScoreCheckImportBatch)) == 1
        assert db.scalar(select(func.count()).select_from(m.ImportSource)) == 1


def test_upload_rejects_missing_csrf_idempotency_and_wrong_envelope(h):
    client = h.client('coordinator')
    files = [('files', ('rota.csv', b'Person,Date,Code\nAlex,2026-10-04,AM', 'text/csv'))]
    csrf = client.headers.pop('x-csrf-token')
    try:
        assert client.post('/api/score-imports', files=files,
            headers={'idempotency-key': str(uuid.uuid4())}).status_code == 403
    finally:
        client.headers['x-csrf-token'] = csrf
    assert client.post('/api/score-imports', files=files).status_code == 428
    wrong = client.post('/api/score-imports', json={'files': []},
                        headers={'idempotency-key': str(uuid.uuid4())})
    assert wrong.status_code == 415 and wrong.json()['error']['code'] == 'UPLOAD_MULTIPART'


@pytest.mark.parametrize('mutation,code', [
    ('person_id', 'EXTRACTION_ID'), ('type_id', 'EXTRACTION_ID'), ('service_id', 'EXTRACTION_ID'),
    ('location_id', 'EXTRACTION_ID'), ('file', 'EXTRACTION_FILE'), ('cell', 'EXTRACTION_CELL'),
    ('person', 'EXTRACTION_DECLARATION'), ('code', 'EXTRACTION_DECLARATION'),
    ('clinic', 'EXTRACTION_DECLARATION'), ('date', 'EXTRACTION_PERIOD'),
])
def test_untrusted_extraction_never_applies_foreign_ids_or_invented_references(h, monkeypatch, mutation, code):
    parse = score_check.parse_score_check_file
    foreign = foreign_catalogues(h) if mutation.endswith('_id') else {}

    def malicious(*args, **kwargs):
        result = copy.deepcopy(parse(*args, **kwargs))
        if mutation in {'person_id', 'type_id', 'service_id', 'location_id'}:
            collection, field = {'person_id': ('people', 'suggested_person_id'),
                'type_id': ('duty_codes', 'suggested_type_id'),
                'service_id': ('clinics', 'suggested_service_id'),
                'location_id': ('clinics', 'suggested_location_id')}[mutation]
            result[collection][0][field] = foreign[mutation]
        else:
            field, value = {'file': ('file_ref', 'other-upload'), 'cell': ('cell_ref', 'CSV!Z999'),
                'person': ('person', 'Invented staff'), 'code': ('duty_code', 'Invented code'),
                'clinic': ('clinic', 'Invented clinic'), 'date': ('date', '2026-11-01')}[mutation]
            result['assignments'][0][field] = value
        return result

    monkeypatch.setattr(score_check, 'parse_score_check_file', malicious)
    refused = csv_upload(h, status=422, starts_on='2026-10-04', ends_on='2026-10-11')
    assert refused['error']['code'] == code
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.ScoreCheckImportBatch)) == 0
        assert db.scalar(select(func.count()).select_from(m.ImportSource)) == 0
        assert db.scalar(select(func.count()).select_from(m.Shift)) == 0


def test_department_scope_blocks_batch_decision_source_and_made_by_ids(h):
    batch = mixed_upload(h)
    foreign = foreign_catalogues(h)
    identity(h, 'other-coordinator', ['coordinator'], department=foreign['department'])
    assert h.get('other-coordinator', '/api/score-checks')['batches'] == []
    h.get('other-coordinator', '/api/score-checks/' + batch['id'], status=404)
    change(h, batch, 'update', role='other-coordinator', name='Cross department', status=404)
    refused = change(h, batch, 'decide', gap_id=gap_for(batch, 'Unlisted Visitor')['id'],
        action='match', target_id=foreign['person_id'], status=422)
    assert refused['error']['code'] == 'IMPORT_PERSON'
    refused = change(h, batch, 'update', files=[{'source_id': batch['files'][0]['id'],
        'made_by': [foreign['person_id']]}], status=422)
    assert refused['error']['code'] == 'IMPORT_PERSON'
    assert h.get('coordinator', '/api/score-checks/' + batch['id'])['batch']['revision'] == batch['revision']


def test_decisions_and_file_edits_cannot_be_borrowed_from_another_import(h):
    batch = mixed_upload(h)
    other = csv_upload(h)['batch']
    batch = decide(h, batch, gap_for(batch, 'Unlisted Visitor'), 'exclude')
    decision = gap_for(batch, 'Unlisted Visitor')['decision']
    before = {b['id']: h.get('coordinator', '/api/score-checks/' + b['id'])['batch'] for b in (batch, other)}
    assert change(h, other, 'undo', decision_id=decision['id'], status=404)['error']['code'] == 'NOT_FOUND'
    assert change(h, other, 'update', files=[{'source_id': batch['files'][0]['id'],
        'group': 'Wrong import'}], status=404)['error']['code'] == 'NOT_FOUND'
    assert h.get('coordinator', '/api/score-checks/' + other['id'])['batch'] == before[other['id']]
    assert h.get('coordinator', '/api/score-checks/' + batch['id'])['batch'] == before[batch['id']]


def test_confirm_cannot_substitute_another_person_or_attach_a_location(h):
    batch = mixed_upload(h)
    gap = gap_for(batch, 'Jordn Lee')
    assert change(h, batch, 'decide', gap_id=gap['id'], action='confirm',
        target_id=h.seed['people'][0], status=422)['error']['code'] == 'GAP_MATCH'
    assert change(h, batch, 'decide', gap_id=gap['id'], action='confirm',
        location_id=h.seed['locations'][0], status=422)['error']['code'] == 'GAP_LOCATION'
    foreign = foreign_catalogues(h)
    assert change(h, batch, 'decide', gap_id=gap_for(batch, 'Continuity clinic')['id'],
        action='match', target_id=h.seed['services'][0], location_id=foreign['location_id'],
        status=404)['error']['code'] == 'NOT_FOUND'


def test_saving_automatically_uses_confident_matches_and_leaves_uncertain_evidence_out(h):
    batch = mixed_upload(h)
    batch = decide(h, batch, gap_for(batch, 'Continuity clinic'), 'confirm', location_id=h.seed['locations'][0])
    batch = acknowledge_missing(h, batch)
    assert batch['counts']['unknown'] == batch['counts']['uncertain'] == batch['counts']['unreadable'] == 1
    saved = change(h, batch, 'save')
    shift, = h.detail(saved['workspace'])['shifts']
    assert [p['person_id'] for p in shift['participants']] == [h.seed['people'][0]]
    assert saved['batch']['counts']['unresolved'] == 3


def test_missing_coverage_acknowledgement_does_not_weaken_normal_validation(h):
    typ = h.configure('assignment_types', {'name': 'Synthetic two-person session', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}]}})['record']
    batch = csv_upload(h, [['Alex Morgan', '2026-10-04', typ['name'], 'Unlisted clinic', '08:00', '12:00']],
        starts_on='2026-10-04', ends_on='2026-10-11')['batch']
    batch = decide(h, batch, gap_for(batch, 'Unlisted clinic'), 'match',
        target_id=h.seed['services'][0], location_id=h.seed['locations'][0])
    coverage = [g for g in batch['gaps'] if g['kind'] == 'missing' and g['entity_kind'] == 'coverage']
    assert coverage, 'The shared evaluator must expose the missing second required person before Save'
    batch = acknowledge_missing(h, batch)
    saved = change(h, batch, 'save')
    workspace = h.detail(saved['workspace'])['workspace']
    assert workspace['validation']['valid'] is False
    assert any(f['code'] == 'COVERAGE_GAP' for f in workspace['validation']['findings'])
    manual = h.shift(h.workspace('Identical incomplete manual rota'), typ=typ['id'])['workspace']
    for key in ('score', 'uncapped_score', 'components', 'capped', 'settings'):
        assert workspace['validation']['score'][key] == manual['validation']['score'][key]
    assert workspace['validation']['score']['score'] == 59
    refused = h.lifecycle(workspace, 'submit', status=422)
    assert refused['error']['code'] == 'VALIDATION_FAILED'


def saved_import(h, *, role='coordinator'):
    batch = csv_upload(h, [['Alex Morgan', '2026-10-04', 'Clinical review session',
        'Unlisted clinic', '08:00', '12:00']], role=role,
        starts_on='2026-10-04', ends_on='2026-10-11')['batch']
    batch = decide(h, batch, gap_for(batch, 'Unlisted clinic'), 'match',
        target_id=h.seed['services'][0], location_id=h.seed['locations'][0], role=role)
    batch = acknowledge_missing(h, batch)
    return change(h, batch, 'save', role=role)


@pytest.mark.parametrize('role', ['admin', 'coordinator'])
def test_import_is_an_editable_normal_draft_with_same_score_and_independent_approval(h, role):
    result = saved_import(h, role=role)
    imported = result['workspace']
    assert imported['origin'] == 'import' and imported['status'] == 'draft'
    assert result['batch']['status'] == 'saved' and result['batch']['workspace_id'] == imported['id']
    assert all(not g['decision']['can_undo'] for g in result['batch']['gaps'] if g.get('decision'))
    detail = h.detail(imported)
    shift, = detail['shifts']
    assert shift['assignment_type_id'] == h.seed['type']
    assert shift['location_id'] == h.seed['locations'][0] and shift['service_ids'] == [h.seed['services'][0]]
    assert [p['person_id'] for p in shift['participants']] == [h.seed['people'][0]]
    manual = h.shift(h.workspace('Identical manual score fixture'))['workspace']
    assert detail['workspace']['validation']['score'] == manual['validation']['score']
    assert detail['workspace']['validation']['valid'] is True
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Publication)) == 0
        assert db.scalar(select(func.count()).select_from(m.FairnessLedger)) == 0
    refused = change(h, result['batch'], 'save', status=409)
    assert refused['error']['code'] == 'IMPORT_STATE'
    # Ordinary Planner edits keep the imported origin and re-use its normal evaluator.
    edited = h.shift(detail['workspace'], start='2026-10-05T08:00:00+03:00',
                     end='2026-10-05T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    assert edited['origin'] == 'import' and edited['revision'] > imported['revision']
    submitted = h.lifecycle(edited, 'submit')['workspace']
    h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['coordinator'],
        'role': 'approver', 'starts_on': '2020-01-01', 'reason': 'Synthetic independent-review regression'})
    refused = h.lifecycle(submitted, 'approve', status=403)
    assert refused['error']['code'] == 'SELF_APPROVAL'
    approved = h.lifecycle(submitted, 'approve', 'approver')['workspace']
    published = h.lifecycle(approved, 'publish')['workspace']
    assert published['status'] == 'published' and published['origin'] == 'import'
    with transaction(h.app.state.engine) as db:
        publication, = db.scalars(select(m.Publication))
        assert publication.workspace_id == imported['id']
        assert publication.current is True
        ledger = list(db.scalars(select(m.FairnessLedger)))
        assert ledger and {row.publication_id for row in ledger} == {publication.id}
        assert sum(row.value for row in ledger if row.metric_key == 'duties') == 2


def test_inferred_period_fully_contains_overnight_duties(h):
    batch = csv_upload(h, [['Alex Morgan', '2026-10-04', 'Clinical review session',
        'Unlisted clinic', '20:00', '04:00']])['batch']
    assert (batch['starts_on'], batch['ends_on']) == ('2026-10-04', '2026-10-06')
    assert (batch['files'][0]['starts_on'], batch['files'][0]['ends_on']) == ('2026-10-04', '2026-10-06')
    batch = decide(h, batch, gap_for(batch, 'Unlisted clinic'), 'match',
        target_id=h.seed['services'][0], location_id=h.seed['locations'][0])
    batch = acknowledge_missing(h, batch)
    saved = change(h, batch, 'save')
    shift, = h.detail(saved['workspace'])['shifts']
    assert shift['start'].startswith('2026-10-04T17:00:00')
    assert shift['end'].startswith('2026-10-05T01:00:00')


@pytest.mark.parametrize('undo_resolution', [False, True])
def test_resolve_unrecognized_layout_after_group_edit_introduces_labels_and_supports_undo(h, undo_resolution):
    batch = csv_upload(h, text='Free-form notes\nWritten clinician on Sunday\n',
        starts_on='2026-10-04', ends_on='2026-10-11')['batch']
    assert batch['files'][0]['result']['assignment_count'] == 0
    source = batch['files'][0]
    batch = change(h, batch, 'update', files=[{'source_id': source['id'], 'group': 'Fellows'}])['batch']
    cell = next(g for g in batch['gaps'] if g['entity_kind'] == 'cell' and g['cell_refs'] == ['CSV!A2'])
    replacement = {'file_ref': source['file_ref'], 'cell_ref': 'CSV!A2', 'person': 'Written clinician',
        'date': '2026-10-04', 'duty_code': 'Written session', 'clinic': 'Written clinic',
        'start': '08:00', 'end': '12:00'}
    batch = decide(h, batch, cell, 'resolve', replacement=replacement)
    resolution = next(g for g in batch['gaps'] if g['id'] == cell['id'])['decision']
    assert batch['files'][0]['result']['assignment_count'] == 1
    assert batch['files'][0]['group'] == 'Fellows'
    for label, target, extra in [('Written clinician', h.seed['people'][0], {}),
            ('Written session', h.seed['type'], {}),
            ('Written clinic', h.seed['services'][0], {'location_id': h.seed['locations'][0]})]:
        gap = gap_for(batch, label)
        assert gap['kind'] == 'unknown' and gap.get('decision') is None
        batch = decide(h, batch, gap, 'match', target_id=target, **extra)
    if undo_resolution:
        batch = change(h, batch, 'undo', decision_id=resolution['id'])['batch']
        assert batch['files'][0]['result']['assignment_count'] == 0
        assert next(g for g in batch['gaps'] if g['id'] == cell['id']).get('decision') is None
        batch = acknowledge_missing(h, batch)
        assert change(h, batch, 'save', status=422)['error']['code'] == 'EMPTY_IMPORT'
    else:
        batch = acknowledge_missing(h, batch)
        saved = change(h, batch, 'save')
        shift, = h.detail(saved['workspace'])['shifts']
        assert [p['person_id'] for p in shift['participants']] == [h.seed['people'][0]]


@pytest.mark.parametrize('edit', [{'group': None}, {'made_by': None},
    {'starts_on': None}, {'ends_on': None}, {'starts_on': '2026-10-09', 'ends_on': '2026-10-04'}])
def test_source_metadata_rejects_invalid_null_or_reversed_values_without_partial_changes(h, edit):
    batch = csv_upload(h, starts_on='2026-10-04', ends_on='2026-10-11')['batch']
    before = h.get('coordinator', '/api/score-checks/' + batch['id'])['batch']
    refused = change(h, batch, 'update', files=[{'source_id': batch['files'][0]['id'], **edit}], status=422)
    assert refused['error']['code'] in {'IMPORT_FILE_VALUE', 'IMPORT_PERIOD', 'SCHEMA_INVALID'}
    assert h.get('coordinator', '/api/score-checks/' + batch['id'])['batch'] == before


def test_source_and_batch_metadata_previous_value_restores_the_complete_edit(h):
    batch = csv_upload(h, starts_on='2026-10-04', ends_on='2026-10-11')['batch']
    source = batch['files'][0]
    result = change(h, batch, 'update', name='Corrected title', confidence_threshold=.8,
        manual_time_hours=6, manual_time_people=2,
        files=[{'source_id': source['id'], 'group': 'Fellows', 'starts_on': '2026-10-04',
                'ends_on': '2026-10-10', 'work_hours_each': 3, 'made_by': h.seed['people'][:2]}])
    previous = result['previous_value']
    assert previous['files'][0]['source_id'] == source['id']
    restored = change(h, result['batch'], 'update', **previous)['batch']
    for key in ('name', 'starts_on', 'ends_on', 'confidence_threshold', 'manual_time_hours', 'manual_time_people', 'period_inferred'):
        assert restored[key] == batch[key]
    for key in ('group', 'starts_on', 'ends_on', 'work_hours_each', 'made_by'):
        assert restored['files'][0][key] == source[key]


def test_missing_staff_alarms_follow_exclude_match_and_undo_decisions(h):
    batch = csv_upload(h)['batch']
    assert not any(g['kind'] == 'missing' and g['suggested_target_id'] == h.seed['people'][0] for g in batch['gaps'])
    person = gap_for(batch, 'Alex Morgan', 'uncertain')
    batch = decide(h, batch, person, 'exclude')
    missing = gap_for(batch, 'Alex Morgan', 'missing')
    assert missing['suggested_target_id'] == h.seed['people'][0] and batch['can_save'] is False
    excluded = gap_for(batch, 'Alex Morgan', 'uncertain')['decision']
    batch = change(h, batch, 'undo', decision_id=excluded['id'])['batch']
    assert not any(g['id'] == missing['id'] for g in batch['gaps'])
    batch = decide(h, batch, gap_for(batch, 'Alex Morgan', 'uncertain'), 'match', target_id=h.seed['people'][2])
    assert gap_for(batch, 'Alex Morgan', 'missing')['id'] == missing['id']
    assert not any(g['kind'] == 'missing' and g['suggested_target_id'] == h.seed['people'][2] for g in batch['gaps'])


def test_excluding_unreadable_cell_skips_original_assignment_evidence(h, monkeypatch):
    parse = score_check.parse_score_check_file

    def ambiguous(*args, **kwargs):
        result = parse(*args, **kwargs)
        item = result['assignments'][0]
        result['unparsed'].append({'file_ref': item['file_ref'], 'cell_ref': item['cell_ref'],
            'value': 'Ambiguous source cell', 'reason': 'Needs a decision before using this assignment'})
        return result

    monkeypatch.setattr(score_check, 'parse_score_check_file', ambiguous)
    batch = csv_upload(h)['batch']
    batch = decide(h, batch, gap_for(batch, 'Continuity clinic'), 'confirm', location_id=h.seed['locations'][0])
    cell = next(g for g in batch['gaps'] if g['kind'] == 'unreadable')
    batch = decide(h, batch, cell, 'exclude')
    assert gap_for(batch, 'Alex Morgan', 'missing')['suggested_target_id'] == h.seed['people'][0]
    batch = acknowledge_missing(h, batch)
    assert change(h, batch, 'save', status=422)['error']['code'] == 'EMPTY_IMPORT'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Workspace)) == 0
        assert db.scalar(select(func.count()).select_from(m.Shift)) == 0

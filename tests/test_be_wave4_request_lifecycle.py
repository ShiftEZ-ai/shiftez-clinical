"""Creation Undo hides operational records while retaining immutable evidence and ownership."""
import csv
from io import StringIO
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from tests.governance_support import identity


def absence(h, day='04'):
    return h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0],
        'kind': 'actual_absence', 'start': f'2026-10-{day}T08:00:00+03:00',
        'end': f'2026-10-{day}T10:00:00+03:00', 'private_reason': 'Synthetic private explanation'})


def undo(h, value, role='coordinator', status=200):
    return h.command(role, value['kind'], value['payload'], value['target_id'], value['expected_revision'], status=status)


def test_creation_undo_hides_record_and_totals_but_keeps_audit(h):
    before = h.get('coordinator', '/api/requests/all')['total']
    created = absence(h)
    row = created['record']
    assert created['undo']['kind'] == 'availability.undo_create'
    assert h.get('coordinator', '/api/requests/all')['total'] == before + 1
    result = undo(h, created['undo'])
    assert result['voided'] and 'undo' not in result
    for role in ('coordinator', 'staff'):
        listed = h.get(role, '/api/requests/all')
        assert row['id'] not in {item['id'] for item in listed['items']}
        assert listed['total'] == before
        assert listed['counts']['stages'].get('withdrawn', 0) == 0
        assert row['id'] not in {item['id'] for item in h.get(role, '/api/availability')['availability']}
    person = h.get('coordinator', f"/api/people/{row['person_id']}/availability?start=2026-10-01&end=2026-11-01")
    assert person['availability'] == []
    h.get('coordinator', '/api/requests/all?id=' + row['id'], status=404)
    with transaction(h.app.state.engine) as db:
        saved = db.get(m.Availability, row['id'])
        assert saved.retracted and saved.private_reason == 'Synthetic private explanation'
        marker = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'availability.undo_create',
            m.DefinitionHistory.object_id == row['id']))
        assert marker.object_revision == saved.revision and 'private_reason' not in marker.snapshot
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'availability.undo_create',
                                                     m.AuditEvent.target_id == row['id']))
    h.command('coordinator', 'availability.restore', {'reason': 'Ordinary restore'}, row['id'],
              result['record']['revision'], status=409)


def test_manual_retraction_remains_withdrawn_and_counted(h):
    created = absence(h)['record']
    total = h.get('coordinator', '/api/requests/all')['total']
    h.command('coordinator', 'availability.retract', {'reason': 'Retract recorded absence'}, created['id'], created['revision'])
    listed = h.get('coordinator', '/api/requests/all')
    assert listed['total'] == total
    assert next(item for item in listed['items'] if item['id'] == created['id'])['stage'] == 'withdrawn'


def test_creation_undo_checks_creator_receipt_and_unchanged_revision(h):
    first, second = absence(h), absence(h, '05')
    identity(h, 'other_recorder', ['coordinator'])
    undo(h, first['undo'], role='other_recorder', status=404)
    h.command('coordinator', 'availability.undo_create', {**first['undo']['payload'],
        'receipt_id': second['receipt_id']}, first['record']['id'], first['record']['revision'], status=409)
    edited = h.command('coordinator', 'availability.update', {'end': '2026-10-04T11:00:00+03:00',
        'reason': 'Later correction'}, first['record']['id'], first['record']['revision'])['record']
    h.command('coordinator', 'availability.undo_create', first['undo']['payload'], edited['id'], edited['revision'], status=409)
    assert all(not row['retracted'] for row in h.get('coordinator', '/api/availability')['availability'])


def test_bulk_creation_undo_uses_server_receipt_and_excludes_selection(h):
    payload = {'item_kind': 'availability.record', 'reason': 'Synthetic bulk recording', 'items': [
        {'payload': {'person_id': h.seed['people'][0], 'kind': 'actual_absence',
            'start': f'2026-10-{day}T08:00:00+03:00', 'end': f'2026-10-{day}T10:00:00+03:00',
            'private_reason': 'Synthetic private explanation'}} for day in ('04', '05')]}
    preview = h.client('coordinator').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
    assert preview.status_code == 200, preview.text
    created = h.command('coordinator', 'bulk.apply', payload)
    inverse = created['undo']
    assert all(item['item_kind'] == 'availability.undo_create' for item in inverse['payload']['items'])
    h.command('coordinator', inverse['kind'], inverse['payload'])
    assert h.get('coordinator', '/api/requests/all')['total'] == 0
    with transaction(h.app.state.engine) as db:
        from apps.api.auth import Actor
        from apps.api.services.bulk_selection import _base_query
        actor = Actor(identity_id=h.seed['identities']['coordinator'], department_id=h.seed['department_id'],
            organisation_id=h.seed['organisation_id'], person_id=None, roles={'coordinator'}, session_id='synthetic',
            display_name='Synthetic coordinator')
        assert not list(db.scalars(_base_query(db, actor, 'availability')))


@pytest.mark.parametrize('role', ['staff', 'second_signin'])
@pytest.mark.parametrize('bulk', [False, True])
def test_request_person_can_withdraw_coordinator_submitted_request_and_audit_it(h, role, bulk):
    if role == 'second_signin':
        identity(h, role, ['staff'], person=h.seed['people'][0])
    request = h.command('coordinator', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'meeting',
        'start': '2026-10-04T09:00:00+03:00', 'end': '2026-10-04T10:00:00+03:00',
        'private_reason': 'Synthetic request for colleague'})['record']
    identity(h, 'unrelated_staff', ['staff'], person=h.seed['people'][1])
    h.command('unrelated_staff', 'request.decide', {'decision': 'withdraw', 'reason': 'Not mine'},
              request['id'], request['revision'], status=403)
    changed = h.command(role, 'request.decide', {'decision': 'withdraw', 'reason': 'No longer needed'},
                        request['id'], request['revision'])['record']
    assert changed['status'] == 'withdrawn' and changed['created_by'] == request['created_by']
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'request.decide',
                                                    m.AuditEvent.target_id == request['id']))
        assert event.actor_id == h.seed['identities'][role]
    for denied in ('coordinator', 'unrelated_staff'):
        h.command(denied, 'request.undo', {'reason': 'Undo someone else'}, changed['id'], changed['revision'], status=403)
    key = 'withdrawal-undo-' + role + str(bulk)
    if bulk:
        undo_payload = {'items': [{'item_kind': 'request.undo', 'target_id': changed['id'],
            'expected_revision': changed['revision'], 'payload': {'reason': 'Undo my withdrawal'}}], 'reason': 'Undo my withdrawal'}
        result = h.command(role, 'bulk.apply', undo_payload, key=key)
        assert h.command(role, 'bulk.apply', undo_payload, key=key) == result
        restored = result['items'][0]['results'][0]['record']
    else:
        result = h.command(role, 'request.undo', {'reason': 'Undo my withdrawal'}, changed['id'], changed['revision'], key=key)
        assert h.command(role, 'request.undo', {'reason': 'Undo my withdrawal'}, changed['id'], changed['revision'], key=key) == result
        restored = result['record']
    receipt = h.get(role, '/api/commands/' + key)
    assert receipt['status'] == 'committed' and receipt['result'] == result
    assert restored['status'] == 'submitted' and 'withdrawn_by' not in restored['details']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities'][role]).person_id = None
    h.get(role, '/api/commands/' + key, status=403)


def test_audit_target_filter_is_shared_by_page_and_csv(h):
    first, second = h.workspace('First target'), h.workspace('Second target')
    h.lifecycle(first, 'validate')
    h.lifecycle(second, 'validate')
    page = h.get('auditor', '/api/audit?target_id=' + first['id'])
    assert page['target_id'] == first['id']
    assert page['events'] and {item['target_id'] for item in page['events']} == {first['id']}
    response = h.client('auditor').get('/api/audit.csv?target_id=' + first['id'])
    assert response.status_code == 200
    rows = list(csv.DictReader(StringIO(response.text)))
    assert rows and {row['reference'] for row in rows} == {event['correlation_id'] for event in page['events']}
    assert second['id'] not in response.text and 'private explanation' not in response.text


def test_frontend_backend_commands_and_approval_contracts_are_discoverable(h):
    contracts = h.get('coordinator', '/api/contracts')['schemas']
    assert {'assignment_id', 'target_shift_id', 'workspace_id', 'reason'} <= contracts['DraftMove']['properties'].keys()
    assert 'unassign' in contracts['ShiftAction']['properties']['action']['enum']
    assert {'receipt_id', 'reason'} <= contracts['AvailabilityUndoCreate']['properties'].keys()
    assert {'approved_by_name', 'approved_at'} <= contracts['PolicyVersionApproval']['properties'].keys()
    assert 'current_load' in contracts['ExceptionApproval']['properties']


def test_creation_undo_cannot_be_restored_later_in_the_same_bulk_transaction(h):
    created = absence(h)
    row, inverse = created['record'], created['undo']
    reply = h.command('coordinator', 'bulk.apply', {'items': [
        {'item_kind': inverse['kind'], 'target_id': row['id'], 'expected_revision': row['revision'], 'payload': inverse['payload']},
        {'item_kind': 'availability.restore', 'target_id': row['id'], 'expected_revision': row['revision'] + 1,
         'payload': {'reason': 'Try to restore voided creation'}}], 'reason': 'Synthetic atomic test'}, status=409)
    assert reply['error']['code'] == 'CREATION_UNDONE'
    with transaction(h.app.state.engine) as db:
        assert not db.get(m.Availability, row['id']).retracted
        assert not db.scalar(select(m.DefinitionHistory.id).where(m.DefinitionHistory.kind == 'availability.undo_create',
                                                                 m.DefinitionHistory.object_id == row['id']))

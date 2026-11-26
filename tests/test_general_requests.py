import pytest
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import transaction
from tests.governance_support import identity
from tests.outbox_support import process_one_after_undo as process_one


def test_author_changes_waiting_request_and_undo_restores_it(h):
    person = h.seed['people'][0]
    original = {'person_id': person, 'kind': 'administrative', 'start': '2026-10-04T09:00:00+03:00',
                'end': '2026-10-04T10:00:00+03:00', 'private_reason': 'Synthetic original request note'}
    row = h.command('staff', 'request.create', original)['record']
    detail_url = '/api/requests/all?id=' + row['id']
    before = h.get('staff', detail_url)['item']
    draft = h.shift(h.workspace(), start='2026-10-05T09:00:00+03:00', end='2026-10-05T11:00:00+03:00',
                    people=[person])['workspace']
    h.publish(draft)
    edited = {**original, 'kind': 'leave', 'start': '2026-10-05T09:00:00+03:00',
              'end': '2026-10-05T10:00:00+03:00', 'private_reason': 'Synthetic updated request note'}

    assert h.command('coordinator', 'request.edit', edited, row['id'], row['revision'],
                     status=403)['error']['code'] == 'FORBIDDEN'
    key = m.uid()
    reply = h.command('staff', 'request.edit', edited, row['id'], row['revision'], key=key)
    assert h.committed('staff', key)['undo'] == reply['undo']
    assert original['private_reason'] not in str(h.committed('staff', key))
    assert edited['private_reason'] not in str(reply)
    with transaction(h.app.state.engine) as db:
        receipt = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.key == key))
        assert all(note not in str(receipt.result) for note in (original['private_reason'], edited['private_reason']))
        assert db.get(m.RequestRecord, row['id']).private_undo_note == original['private_reason']
    detail = h.get('staff', detail_url)
    after = detail['item']
    assert (after['id'], after['stage'], after['kind']) == (row['id'], 'waiting', 'leave')
    assert after['start'] != before['start'] and after['impact_count'] == 1
    assert after['private_reason'] == edited['private_reason']
    assert 'private_undo_note' not in after
    assert detail['history'][-1]['action'] == 'request.edit'
    assert edited['private_reason'] not in str(detail['history'])
    assert not h.get('staff', '/api/availability')['availability']

    coordinator = h.client('coordinator')
    selection = {'collection': 'requests', 'include_ids': [row['id']]}
    page = coordinator.post('/api/bulk/rows', json={'selection': selection})
    assert page.status_code == 200, page.text
    assert any(item['id'] == row['id'] for item in page.json()['rows'])
    assert all('private_undo_note' not in item for item in page.json()['rows'])
    searched = coordinator.post('/api/bulk/rows', json={
        'selection': {'collection': 'requests', 'where': {'q': original['private_reason']}}})
    assert searched.status_code == 200 and searched.json()['total'] == 0, searched.text
    filtered = coordinator.post('/api/bulk/rows', json={
        'selection': {'collection': 'requests', 'where': {'filters': {'private_undo_note': [original['private_reason']]}}}})
    assert filtered.status_code == 422, filtered.text

    undo = reply['undo']
    restored = h.command('staff', undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'])['record']
    detail = h.get('staff', detail_url)
    for field in ('start', 'end', 'kind', 'private_reason'):
        assert detail['item'][field] == before[field]
    assert detail['history'][-1]['action'] == 'request.undo_edit'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RequestRecord, row['id']).private_undo_note is None
    decision = h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Synthetic approval'},
                         row['id'], restored['revision'])['record']
    assert h.command('staff', 'request.edit', edited, row['id'], decision['revision'],
                     status=409)['error']['code'] == 'WORKFLOW_STATE'


def test_request_edit_receipts_require_current_subject_access(h):
    role = 'request_editor'
    author = identity(h, role, ['staff', 'coordinator'], h.seed['people'][0])
    payload = {'person_id': h.seed['people'][1], 'kind': 'meeting', 'start': '2026-10-04T09:00:00+03:00',
               'end': '2026-10-04T10:00:00+03:00', 'private_reason': 'Synthetic original request note'}
    row = h.command(role, 'request.create', payload)['record']
    edited = {**payload, 'end': '2026-10-04T11:00:00+03:00'}
    edit_key, undo_key = m.uid(), m.uid()
    result = h.command(role, 'request.edit', edited, row['id'], row['revision'], key=edit_key)
    undo = result['undo']
    h.command(role, undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'], key=undo_key)
    assert h.committed(role, edit_key)['record']['id'] == row['id']
    assert h.committed(role, undo_key)['record']['id'] == row['id']

    with transaction(h.app.state.engine, write=True) as db:
        db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == author,
                                             m.RoleBinding.role == 'coordinator')).revoked = True

    for key in (edit_key, undo_key):
        reply = h.get(role, '/api/commands/' + key, status=403)
        assert reply['error']['code'] == 'FORBIDDEN' and 'result' not in reply
    assert h.command(role, 'request.edit', edited, row['id'], row['revision'], status=403,
                     key=edit_key)['error']['code'] == 'FORBIDDEN'
    assert h.command(role, undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'], status=403,
                     key=undo_key)['error']['code'] == 'FORBIDDEN'


@pytest.mark.parametrize('legacy', [False, True])
def test_undo_waiting_leave_restores_previous_type_and_availability(h, legacy):
    original_type = {'name': 'Synthetic leave type', 'short_code': 'TSTA', 'counts_as_unavailable': False}
    original = h.configure('leave_types', original_type)['record']
    active = h.configure('leave_types', {'name': 'Synthetic other type', 'short_code': 'TSTB'})['record']
    payload = {'person_id': h.seed['people'][0], 'kind': 'leave', 'start': '2026-10-04T09:00:00+03:00',
               'end': '2026-10-04T10:00:00+03:00', 'private_reason': 'Synthetic leave request',
               'leave_type_id': original['id']}
    row = h.command('staff', 'request.create', payload)['record']
    if legacy:
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.RequestRecord, row['id']).leave_type_id = None
    h.configure('leave_types', {**original_type, 'active': False, 'counts_as_unavailable': True},
                original['id'], original['revision'])
    result = h.command('staff', 'request.edit', {**payload, 'leave_type_id': active['id']}, row['id'], row['revision'])
    current = result['record']
    assert current['leave_type_id'] == active['id'] and current['counts_as_unavailable'] is True
    assert h.command('staff', 'request.edit', payload, row['id'], current['revision'],
                     status=422)['error']['code'] == 'LEAVE_TYPE_ARCHIVED'
    undo = result['undo']
    restored = h.command('staff', undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'])['record']
    assert restored['leave_type_id'] == (None if legacy else original['id'])
    assert restored['counts_as_unavailable'] is False


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('kind', ['leave', 'meeting', 'clinic_change', 'administrative'])
@pytest.mark.parametrize('state', ['submitted', 'approved', 'rejected', 'withdrawn'])
def test_exact_request_duplicate_is_checked_on_write_and_replay_stays_idempotent(request, database, kind, state):
    import uuid
    h = request.getfixturevalue(database)
    payload = {'person_id': h.seed['people'][0], 'kind': kind, 'start': '2026-10-04T09:00:00+03:00',
        'end': '2026-10-04T10:00:00+03:00', 'private_reason': 'Private synthetic original explanation'}
    key = str(uuid.uuid4())
    result = h.command('staff', 'request.create', payload, key=key)
    assert h.command('staff', 'request.create', payload, key=key) == result
    row = result['record']
    if state != 'submitted':
        row = h.command('staff' if state == 'withdrawn' else 'approver', 'request.decide',
            {'decision': {'approved': 'approve', 'rejected': 'reject', 'withdrawn': 'withdraw'}[state],
             'reason': 'Synthetic existing workflow decision'}, row['id'], row['revision'])['record']
    before = h.get('coordinator', '/api/requests')['requests']
    duplicate_payload = {**payload, 'private_reason': 'Different private reason does not make another request'}
    if state in ('submitted', 'approved'):
        refused = h.command('staff', 'request.create', duplicate_payload, status=409)
        assert refused['error']['code'] == 'REQUEST_DUPLICATE'
        assert refused['error']['details'] == {'request_id': row['id']}
        assert 'Private synthetic' not in str(refused)
        assert h.get('coordinator', '/api/requests')['requests'] == before
    else:
        assert h.command('staff', 'request.create', duplicate_payload)['record']['id'] != row['id']
    # A genuine partial overlap is a separate request; only leave carries the existing overlap warning.
    different = h.command('staff', 'request.create', {**payload, 'end': '2026-10-04T11:00:00+03:00'})
    assert different['record']['id'] != row['id']
    assert bool(different['warnings']) == (kind == 'leave')  # declined/withdrawn cases now have the new submitted row


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_concurrent_distinct_keys_cannot_create_two_semantically_equal_requests(request, database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import uuid
    h = request.getfixturevalue(database)
    clients = [h.client('staff'), h.client('coordinator')]
    payload = {'person_id': h.seed['people'][0], 'kind': 'meeting',
        'start': '2026-10-05T09:00:00+03:00', 'end': '2026-10-05T10:00:00+03:00',
        'private_reason': 'Synthetic concurrent submission'}
    assert not h.get('staff', '/api/requests')['requests']
    gate = Barrier(2)
    def send(client):
        gate.wait(timeout=10)
        return client.post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())},
            json={'kind': 'request.create', 'payload': payload})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send, client) for client in clients]
        responses = [future.result(timeout=20) for future in futures]
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    created = next(r.json()['record'] for r in responses if r.status_code == 200)
    refused = next(r.json()['error'] for r in responses if r.status_code == 409)
    assert refused['code'] == 'REQUEST_DUPLICATE' and refused['details'] == {'request_id': created['id']}
    with transaction(h.app.state.engine) as db:
        rows = list(db.scalars(select(m.RequestRecord).where(m.RequestRecord.kind == 'meeting')))
        assert len(rows) == 1 and rows[0].id == created['id']
        assert len(list(db.scalars(select(m.Outbox).where(m.Outbox.payload['request_id'].as_string() == created['id'])))) == 1


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_request_duplicate_identity_keeps_person_kind_and_resolved_leave_type_distinct(request, database):
    h = request.getfixturevalue(database)
    types = [h.configure('leave_types', {'name': 'Synthetic leave ' + code, 'short_code': code})['record']['id']
        for code in ['TST1', 'TST2']]
    payload = {'person_id': h.seed['people'][0], 'kind': 'leave', 'leave_type_id': types[0],
        'start': '2026-10-06T09:00:00+03:00', 'end': '2026-10-06T10:00:00+03:00', 'private_reason': 'Synthetic discriminator'}
    original = h.command('coordinator', 'request.create', payload)['record']
    replies = [h.command('coordinator', 'request.create', {**payload, 'leave_type_id': types[1]}),
        h.command('coordinator', 'request.create', {**payload, 'person_id': h.seed['people'][1]}),
        h.command('coordinator', 'request.create', {**payload, 'kind': 'meeting', 'leave_type_id': None})]
    assert len({original['id'], *(r['record']['id'] for r in replies)}) == 4


@pytest.mark.parametrize('kind', ['clinic_change', 'administrative'])
def test_nonleave_approval_does_not_invent_absence_or_publish(h, kind):
    request = h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': kind,
        'start': '2026-10-04T09:00:00+03:00', 'end': '2026-10-04T10:00:00+03:00',
        'private_reason': 'Fictional operational request'})['record']
    decision = h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Reviewed operational request'},
                         request['id'], request['revision'])['record']
    assert decision['status'] == 'approved'
    assert decision['kind'] == kind
    assert not h.get('staff', '/api/availability')['availability']
    assert not h.get('staff', '/api/publications')['publications']


def meeting_request(h, hour=9):
    return h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'meeting',
        'start': f'2026-10-04T{hour:02}:00:00+03:00', 'end': f'2026-10-04T{hour + 1:02}:00:00+03:00',
        'private_reason': 'Synthetic meeting'})['record']


def approve_meeting(h, meeting):
    return h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Approved meeting'},
                     meeting['id'], meeting['revision'])['record']


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_approved_meeting_opens_cover_in_a_reloaded_draft(request, database):
    h = request.getfixturevalue(database)
    person = h.seed['people'][0]
    duty = h.shift(h.workspace())
    url = f"/api/workspaces/{duty['workspace']['id']}"
    meeting = meeting_request(h)
    before = h.get('coordinator', url + '/summary')
    assert before['places_open'] == 0 and before['issues']['blocking'] == 0

    approved = approve_meeting(h, meeting)
    assert (approved['status'], approved['kind']) == ('approved', 'meeting')
    first, = h.get('staff', '/api/availability')['availability']
    assert (first['kind'], first['request_id']) == ('protected_time', meeting['id'])
    summary = h.get('coordinator', url + '/summary')
    assert (summary['places_filled'], summary['places_open']) == (0, 1)
    assert summary['sessions'][0]['person_ids'] == [person]
    conflict, = [row for row in summary['findings'] if row['code'] == 'UNAVAILABLE']
    assert (conflict['person_id'], conflict['shift_id'], conflict['severity']) == (person, duty['shift_id'], 'error')
    fix = h.get('coordinator', url + '/fix-list')
    assert fix['summary']['must'] > 0
    assert not h.get('staff', '/api/publications')['publications']

    waiting = h.command('approver', 'request.undo', {'reason': 'Undo meeting approval'},
                        approved['id'], approved['revision'])['record']
    restored = h.get('coordinator', url + '/summary')
    assert restored['places_open'] == 0 and restored['issues']['blocking'] == 0
    assert waiting['status'] == 'submitted'
    assert approve_meeting(h, waiting)['status'] == 'approved'
    reapproved = h.get('coordinator', url + '/summary')
    assert reapproved['places_open'] == 1
    assert [row['person_id'] for row in reapproved['findings'] if row['code'] == 'UNAVAILABLE'] == [person]
    rows = h.get('staff', '/api/availability')['availability']
    assert len(rows) == 2 and all(row['request_id'] == meeting['id'] and row['kind'] == 'protected_time' for row in rows)
    assert [(row['id'], row['retracted']) for row in rows if row['id'] == first['id']] == [(first['id'], True)]
    assert sum(not row['retracted'] for row in rows) == 1


@pytest.mark.postgres
def test_approved_meeting_at_duty_end_keeps_cover(pg):
    h = pg
    duty = h.shift(h.workspace())
    approve_meeting(h, meeting_request(h, hour=12))
    summary = h.get('coordinator', f"/api/workspaces/{duty['workspace']['id']}/summary")
    assert (summary['places_filled'], summary['places_open']) == (1, 0)
    assert all(row['code'] != 'UNAVAILABLE' for row in summary['findings'])


@pytest.mark.postgres
def test_approved_meeting_flags_published_duty_and_preserves_issued_evidence(pg):
    h = pg
    duty = h.shift(h.workspace())
    publication_id = h.publish(duty['workspace'])['publication_id']
    with transaction(h.app.state.engine) as db:
        issued = db.get(m.Publication, publication_id)
        before = (issued.snapshot, issued.validation, issued.fingerprint, issued.current)

    approve_meeting(h, meeting_request(h))
    after = h.get('coordinator', f'/api/publications/{publication_id}')
    warning, = after['operational']['warnings']
    assert (warning['shift_id'], warning['person_id']) == (duty['shift_id'], h.seed['people'][0])
    assert warning['action']['key'] == 'amend_publication'
    with transaction(h.app.state.engine) as db:
        issued = db.get(m.Publication, publication_id)
        assert (issued.snapshot, issued.validation, issued.fingerprint, issued.current) == before


def test_request_rota_link_is_scoped_reviewed_traceability(h):
    request = h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'meeting',
        'start': '2026-10-05T09:00:00+03:00', 'end': '2026-10-05T10:00:00+03:00',
        'private_reason': 'Fictional team meeting'})['record']
    workspace = h.shift(h.workspace())['workspace']
    payload = {'workspace_id': workspace['id'], 'reason': 'Meeting scheduled in reviewed rota'}
    h.command('coordinator', 'request.link_rota', payload, request['id'], request['revision'], status=409)
    request = h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Meeting approved'},
                        request['id'], request['revision'])['record']
    h.command('staff', 'request.link_rota', payload, request['id'], request['revision'], status=403)
    outside = h.workspace(start='2026-11-01', end='2026-11-08')
    h.command('coordinator', 'request.link_rota', {**payload, 'workspace_id': outside['id']},
              request['id'], request['revision'], status=422)
    linked = h.command('coordinator', 'request.link_rota', payload, request['id'], request['revision'])['record']
    h.command('coordinator', 'request.link_rota', payload, request['id'], request['revision'], status=409)
    own = h.get('staff', '/api/requests')['requests'][0]
    assert own['rota_status'] == 'draft' and 'workspace_id' not in own
    assert not h.get('staff', '/api/publications')['publications']
    h.publish(workspace)
    assert h.get('staff', '/api/requests')['requests'][0]['rota_status'] == 'published'
    h.command('coordinator', 'request.link_rota', {'workspace_id': None, 'reason': 'Correct mistaken reference'},
              linked['id'], linked['revision'])
    assert h.get('staff', '/api/requests')['requests'][0]['rota_status'] is None


def test_request_withdrawal_cannot_be_decided_or_withdrawn_by_someone_else(h):
    request = h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'clinic_change',
        'start': '2026-10-04T09:00:00+03:00', 'end': '2026-10-04T10:00:00+03:00',
        'private_reason': 'Fictional clinic change'})['record']
    h.command('coordinator', 'request.decide', {'decision': 'withdraw', 'reason': 'Not the submitter'},
              request['id'], request['revision'], status=403)
    withdrawn = h.command('staff', 'request.decide', {'decision': 'withdraw', 'reason': 'No longer needed'},
                          request['id'], request['revision'])['record']
    assert withdrawn['status'] == 'withdrawn'
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Too late to approve'},
              withdrawn['id'], withdrawn['revision'], status=409)


def test_status_filter_lists_only_waiting_requests(h):
    def ask(day):
        return h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'clinic_change',
            'start': f'2026-10-{day}T09:00:00+03:00', 'end': f'2026-10-{day}T10:00:00+03:00', 'private_reason': 'Fictional'})['record']
    waiting, done = ask('04'), ask('05')
    h.command('staff', 'request.decide', {'decision': 'withdraw', 'reason': 'No longer needed'}, done['id'], done['revision'])
    for role in ('staff', 'approver'):
        assert [r['id'] for r in h.get(role, '/api/requests?status=submitted')['requests']] == [waiting['id']]
        assert {r['id'] for r in h.get(role, '/api/requests')['requests']} == {waiting['id'], done['id']}
    h.get('approver', '/api/requests?status=nonsense', status=422)


@pytest.mark.parametrize('state', ['submitted', 'approved', 'withdrawn', 'rejected'])
@pytest.mark.postgres
def test_overlapping_leave_warns_without_changing_existing_records(pg, state):
    h = pg
    payload = {'person_id': h.seed['people'][0], 'kind': 'leave', 'start': '2026-10-04T00:00:00+03:00',
               'end': '2026-10-06T00:00:00+03:00', 'private_reason': 'Synthetic leave'}
    old = h.command('staff', 'request.create', payload)['record']
    if state != 'submitted':
        old = h.command('staff' if state == 'withdrawn' else 'approver', 'request.decide',
            {'decision': {'approved': 'approve', 'withdrawn': 'withdraw', 'rejected': 'reject'}[state],
             'reason': 'Synthetic leave decision'}, old['id'], old['revision'])['record']
    before = h.get('coordinator', '/api/requests')['requests']
    absences = h.get('coordinator', '/api/availability')['availability']
    result = h.command('staff', 'request.create', {**payload, 'start': '2026-10-05T12:00:00+03:00',
                                                'end': '2026-10-07T00:00:00+03:00'})
    assert bool(result['warnings']) == (state in ('submitted', 'approved'))
    assert ('Warning:' in result['message']) == bool(result['warnings'])
    assert result['record']['status'] == 'submitted'
    assert [r for r in h.get('coordinator', '/api/requests')['requests'] if r['id'] != result['record']['id']] == before
    assert h.get('coordinator', '/api/availability')['availability'] == absences


@pytest.mark.postgres
def test_leave_overlap_checks_half_open_dates_person_and_recorded_absence(pg):
    h = pg
    payload = {'person_id': h.seed['people'][0], 'kind': 'leave', 'start': '2026-10-04T00:00:00+03:00',
               'end': '2026-10-05T00:00:00+03:00', 'private_reason': 'Synthetic leave'}
    h.command('coordinator', 'availability.record', {**payload, 'kind': 'recorded_leave'})
    assert not h.command('staff', 'request.create', {**payload, 'start': payload['end'],
        'end': '2026-10-06T00:00:00+03:00'})['warnings']  # adjacent days do not overlap
    assert not h.command('coordinator', 'request.create', {**payload, 'person_id': h.seed['people'][1]})['warnings']
    assert not h.command('staff', 'request.create', {**payload, 'kind': 'meeting'})['warnings']
    assert h.command('staff', 'request.create', payload)['warnings']


@pytest.mark.postgres
def test_leave_notifications_have_one_category_including_historical_events(pg):
    h = pg
    request = h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'leave',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00',
        'private_reason': 'Synthetic leave'})['record']
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Synthetic approval'},
              request['id'], request['revision'])
    with transaction(h.app.state.engine, write=True) as db:
        generated = list(db.scalars(select(m.Outbox).where(m.Outbox.payload['request_id'].as_string() == request['id'])))
        assert {event.kind for event in generated} == {'request'}
        legacy = m.Outbox(department_id=h.seed['department_id'], event_key='synthetic-legacy-leave', kind='availability',
            payload={'request_id': request['id'], 'person_id': h.seed['people'][0], 'actor_id': h.seed['identities']['approver']})
        db.add(legacy); db.flush(); legacy_id = legacy.id
    process_one(h.app.state.engine)
    notices = [n for n in h.get('staff', '/api/notifications')['notifications'] if n['link'].get('request_id') == request['id']]
    assert len(notices) == 2 and {n['kind'] for n in notices} == {'request'}
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Outbox, legacy_id).kind == 'availability'  # reads never rewrite stored history

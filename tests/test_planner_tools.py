"""Real-command preview rollback, exact comparisons and private saved-view semantics."""
import copy
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import snapshot_hash
from packages.contracts import schemas as s
from .governance_support import identity


@pytest.mark.parametrize('historical', [False, True])
def test_compare_empty_rota_has_no_score_or_delta_and_keeps_stored_evidence(h, historical):
    empty = h.workspace('Empty comparison')
    populated = h.shift(h.workspace('Populated comparison'))['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Workspace, empty['id'])
        if historical:
            stored.validation = {**stored.validation, 'score': {**stored.validation['score'],
                'score': 100, 'raw': 100, 'uncapped_score': 100, 'publishable': True}}
        original = copy.deepcopy(stored.validation)
    for before, after, empty_side in ((empty, populated, 'before'), (populated, empty, 'after')):
        scores = h.get('coordinator', f"/api/workspaces/{before['id']}/compare?other_id={after['id']}")['scores']
        assert scores[empty_side] is None and scores['detail'] is None
        assert scores['after' if empty_side == 'before' else 'before']['score'] is not None
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, empty['id']).validation == original


def preview(h, w, action='cancel', role='coordinator', status=200):
    shift_id = h.detail(w)['shifts'][0]['id']
    command = {'kind':'shift.action', 'expected_revision':w['revision'], 'payload':{
        'workspace_id':w['id'], 'shift_ids':[shift_id], 'action':action, 'reason':'Preview exact bulk action'}}
    response = h.client(role).post('/api/planner/preview', json=command)
    assert response.status_code == status, response.text
    return response.json(), command


def test_preview_runs_actual_mutation_but_persists_no_row_or_receipt(h):
    w = h.shift(h.workspace())['workspace']
    before = h.detail(w)
    def counts():
        with transaction(h.app.state.engine) as db:
            return [db.scalar(select(func.count()).select_from(model)) for model in
                (m.Workspace,m.Shift,m.Assignment,m.ChangeSet,m.CommandReceipt,m.AuditEvent,m.Outbox)]
    before_counts = counts()
    result, command = preview(h,w)
    assert result['preview'] and not result['committed']
    assert result['impact']['counts'] == {'added':0,'removed':0,'changed':1}
    assert result['impact']['changes'][0]['fields'] == ['cancelled', 'cancellation_reason']
    assert h.detail(w) == before and counts() == before_counts
    assert result['source_fingerprint'] == before['current_fingerprint']
    response = h.client('coordinator').post('/api/commands', headers={'idempotency-key':str(uuid.uuid4())},
        json={**command,'expected_fingerprint':result['source_fingerprint']})
    assert response.status_code == 200, response.text
    assert h.detail(w)['shifts'][0]['cancelled']
    assert counts()[3:6] == [before_counts[i]+1 for i in range(3,6)]


def test_changed_inputs_block_apply_and_keep_preview_from_becoming_authority(h):
    w=h.shift(h.workspace())['workspace'];result,cmd=preview(h,w)
    h.command('coordinator','availability.record',{'person_id':h.seed['people'][0],
        'start':'2026-10-04T08:00:00+03:00','end':'2026-10-04T09:00:00+03:00',
        'kind':'actual_absence','private_reason':'Unrelated protected explanation'})
    response=h.client('coordinator').post('/api/commands',headers={'idempotency-key':str(uuid.uuid4())},
        json={**cmd,'expected_fingerprint':result['source_fingerprint']})
    assert response.status_code==409 and response.json()['error']['code']=='STALE_PREVIEW'
    assert not h.detail(w)['shifts'][0]['cancelled']


def test_preview_stale_revisions_roles_csrf_and_unsupported_mutations_are_refused(h):
    w=h.shift(h.workspace())['workspace']
    for role in ['staff','approver','auditor','credentialer','operator']:
        preview(h,w,role=role,status=403)
    result,cmd=preview(h,w)
    response=h.client('coordinator').post('/api/planner/preview',json={**cmd,'expected_revision':1})
    assert response.status_code==409 and response.json()['error']['code']=='STALE_REVISION'
    response=h.client('coordinator').post('/api/planner/preview',json={**cmd,'kind':'workspace.publish'})
    assert response.status_code==422 and response.json()['error']['code']=='PREVIEW_UNSUPPORTED'
    response=h.client('coordinator').post('/api/planner/preview',json=cmd,headers={'x-csrf-token':'invalid'})
    assert response.status_code==403
    assert h.detail(w)['workspace']['revision']==w['revision']


def test_failed_preview_leaves_preexisting_draft_untouched(h):
    w=h.shift(h.workspace())['workspace'];before=h.detail(w)
    cmd={'kind':'shift.copy','expected_revision':w['revision'],'payload':{
        'workspace_id':w['id'],'shift_ids':[before['shifts'][0]['id']],'offset_days':92,'reason':'Out of bounds'}}
    response=h.client('coordinator').post('/api/planner/preview',json=cmd)
    assert response.status_code==422 and response.json()['error']['code']=='OUTSIDE_WORKSPACE'
    assert h.detail(w)==before


@pytest.mark.parametrize('existing_marker', [False, True])
@pytest.mark.parametrize('failure', [False, True])
def test_preview_restores_its_private_marker_after_success_or_failure(h, monkeypatch, existing_marker, failure):
    from apps.api.auth import Actor
    from apps.api.errors import Problem
    from apps.api.services import planner_tools, planning

    workspace = h.shift(h.workspace())['workspace']
    shift_id = h.detail(workspace)['shifts'][0]['id']
    command = s.Command(kind='shift.action', expected_revision=workspace['revision'], payload={
        'workspace_id': workspace['id'], 'shift_ids': [shift_id], 'action': 'cancel',
        'reason': 'Exercise preview rollback'})
    actor = Actor(h.seed['identities']['coordinator'], h.seed['department_id'], h.seed['organisation_id'],
                  'Synthetic coordinator', frozenset({'coordinator'}), None, 'test')
    previous = {'outer': 'preview context'}
    original = planning.shift_action

    def change(db, actor, value):
        assert db.info['_planner_preview'] is True
        result = original(db, actor, value)
        if failure:
            raise Problem(409, 'SYNTHETIC_PREVIEW_FAILURE', 'Synthetic failure after validation')
        return result

    monkeypatch.setattr(planning, 'shift_action', change)
    with transaction(h.app.state.engine, write=True) as db:
        if existing_marker:
            db.info['_planner_preview'] = previous
        if failure:
            with pytest.raises(Problem, match='Synthetic failure after validation'):
                planner_tools.preview(db, actor, command)
        else:
            assert planner_tools.preview(db, actor, command)['impact']['counts']['changed'] == 1
        assert ('_planner_preview' in db.info) is existing_marker
        if existing_marker:
            assert db.info['_planner_preview'] is previous
        assert not db.get(m.Shift, shift_id).cancelled
        assert db.get(m.Workspace, workspace['id']).revision == workspace['revision']


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_preview_skips_session_flags_but_keeps_full_validation_and_apply_parity(request, database, monkeypatch, caplog):
    import fastapi.routing
    from apps.api.services import planning, series_conflicts
    from apps.api.services.planner_tools import difference
    from packages.domain import overlap
    from packages.domain.input_manifest import manifest

    h = request.getfixturevalue(database)
    created = h.shift(h.workspace())
    workspace, shift_id = created['workspace'], created['shift_id']
    workspace = h.shift(workspace, people=[h.seed['people'][0]], service=1, location=1)['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    before = h.detail(workspace)
    shift = next(row for row in before['shifts'] if row['id'] == shift_id)
    payload = {key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=workspace['id'], shift_id=shift_id, participants=[{
        'person_id': h.seed['people'][0], 'start': shift['start'], 'end': shift['end'], 'team_lead': True}])
    command = {'kind': 'shift.update', 'expected_revision': workspace['revision'], 'payload': payload}

    def unnecessary(*args, **kwargs):
        pytest.fail('A preview needs full validation, without a second session-flag projection')

    encoder = fastapi.routing.jsonable_encoder

    def encode_once(value, *args, **kwargs):
        if isinstance(value, dict) and (value.get('preview') is True or 'receipt_id' in value):
            pytest.fail('JSON-ready previews and commands must not traverse FastAPI encoding again')
        return encoder(value, *args, **kwargs)

    preview_inputs, revalidate = [], planning.revalidate

    def capture_inputs(*args, **kwargs):
        result = revalidate(*args, **kwargs)
        preview_inputs.append(copy.deepcopy(kwargs['data_out'][0]))
        return result

    evaluations, evaluate = [], planning.evaluate

    def complete_check(data, **kwargs):
        result = evaluate(data, **kwargs)
        evaluations.append(copy.deepcopy(result))
        return result

    with monkeypatch.context() as patch:
        patch.setattr(overlap, 'per_session_overlaps', unnecessary)
        patch.setattr(series_conflicts, 'session_actions', unnecessary)
        patch.setattr(fastapi.routing, 'jsonable_encoder', encode_once)
        patch.setattr(planning, 'revalidate', capture_inputs)
        patch.setattr(planning, 'evaluate', complete_check)
        response = h.client('coordinator').post('/api/planner/preview', json=command)
    assert response.status_code == 200, response.text
    previewed = response.json()
    assert len(evaluations) == 1, 'One complete validation per preview'
    [preview_data] = preview_inputs
    # Unchanged receipts may be reused, but the complete result must equal a fresh whole-rota check.
    canonical = evaluate(preview_data)
    assert {key: evaluations[0][key] for key in canonical} == canonical
    assert evaluations[0]['fingerprint'] == previewed['validation']['fingerprint']
    assert not any(row.name == 'rota.api' and row.getMessage() == 'unhandled_error' for row in caplog.records)
    assert h.detail(workspace) == before
    findings = previewed['validation']['findings']
    assert any(row['code'] == 'TIME_OVERLAP' and row['detail']['overlap']['cancel_actions'] for row in findings)
    assert any(row['code'] == 'COVERAGE_GAP' and row['cause'] and row['fix_hints'] for row in findings)
    key = str(uuid.uuid4())
    with monkeypatch.context() as patch:
        patch.setattr(fastapi.routing, 'jsonable_encoder', encode_once)
        applied = h.command('coordinator', 'shift.update', payload, revision=workspace['revision'], key=key)
        assert h.command('coordinator', 'shift.update', payload, revision=workspace['revision'], key=key) == applied
    saved_data = h.snapshot(applied['workspace'])
    for check, data in ((previewed['validation'], preview_data), (applied['validation'], saved_data)):
        current = manifest(data)
        assert check['fingerprint'] == check['input_manifest']['token'] == current['token']
        assert check['input_manifest']['components'] == current['components']
        assert check['input_manifest']['dirty_hint'] == current['dirty_hint']
    # Separate executions create distinct assignment IDs; semantic currency binds those real row identities.
    assert previewed['validation']['fingerprint'] != applied['validation']['fingerprint']
    metadata = {'evaluated_at', 'fingerprint', 'input_manifest'}
    def semantic(value):
        value = copy.deepcopy(value)
        # ``checked_at`` is a fresh-read timestamp nested in the delta summary;
        # it must remain for the client cache key, but is not preview/apply parity.
        value['fix_list_delta']['summary'].pop('checked_at', None)
        return {key: child for key, child in value.items() if key not in metadata}
    assert semantic(previewed['validation']) == semantic(applied['validation'])
    assert previewed['impact'] == difference(before['shifts'], h.detail(applied['workspace'])['shifts'])
    assert len([row for row in h.detail(applied['workspace'])['shifts'] if row.get('double_booking_conflicts')]) == 2


@pytest.mark.parametrize('action', ['cancel', 'remove'])
def test_preview_delta_matches_saved_impact_with_unchanged_sessions(h, action):
    from apps.api.services.planner_tools import difference
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    before = h.detail(w)['shifts']
    result, command = preview(h, w, action)
    assert h.detail(w)['shifts'] == before
    response = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())}, json=command)
    assert response.status_code == 200, response.text
    assert result['impact'] == difference(before, h.detail(w)['shifts'])
    assert result['impact']['unchanged'] == 1


def test_preconditioned_command_replay_remains_stable_after_inputs_change(h):
    w=h.shift(h.workspace())['workspace'];result,cmd=preview(h,w)
    key=str(uuid.uuid4());payload={**cmd,'expected_fingerprint':result['source_fingerprint']}
    first=h.client('coordinator').post('/api/commands',headers={'idempotency-key':key},json=payload)
    assert first.status_code==200
    replay=h.client('coordinator').post('/api/commands',headers={'idempotency-key':key},json=payload)
    assert replay.status_code==200 and replay.json()==first.json()


def _assign_command(h, workspace, shift, person_id):
    return {'kind': 'shift.update', 'payload': {
        key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')
    } | {'workspace_id': workspace['id'], 'shift_id': shift['id'],
         'participants': [{'person_id': person_id, 'start': shift['start'], 'end': shift['end']} ]},
        'expected_revision': workspace['revision']}


def test_assign_replay_returns_original_edit_reply_and_applies_once(h):
    workspace = h.shift(h.workspace('Idempotent assign replay'), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    command = _assign_command(h, workspace, shift, h.seed['people'][0])
    key = 'assign-replay-' + uuid.uuid4().hex
    client = h.client('coordinator')

    first = client.post('/api/commands', headers={'idempotency-key': key}, json=command)
    replay = client.post('/api/commands', headers={'idempotency-key': key}, json=command)

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    saved = h.detail(workspace)['shifts'][0]
    assert [row['person_id'] for row in saved['participants']] == [h.seed['people'][0]]


def test_concurrent_same_key_assign_applies_once(h):
    workspace = h.shift(h.workspace('Concurrent idempotent assign'), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    command = _assign_command(h, workspace, shift, h.seed['people'][0])
    key = 'assign-concurrent-' + uuid.uuid4().hex
    base = h.client('coordinator')

    def submit():
        client = TestClient(h.app, base_url=h.settings.public_origin)
        client.cookies.update(base.cookies)
        client.headers.update({'origin': h.settings.public_origin, 'x-csrf-token': base.headers['x-csrf-token']})
        return client.post('/api/commands', headers={'idempotency-key': key}, json=command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: submit(), range(2)))

    assert [reply.status_code for reply in replies] == [200, 200]
    assert replies[0].json() == replies[1].json()
    assert len(h.detail(workspace)['shifts'][0]['participants']) == 1
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.CommandReceipt).where(m.CommandReceipt.key == key)) == 1


def test_assign_replay_with_different_payload_is_rejected(h):
    workspace = h.shift(h.workspace('Idempotency conflict assign'), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    command = _assign_command(h, workspace, shift, h.seed['people'][0])
    key = 'assign-conflict-' + uuid.uuid4().hex
    client = h.client('coordinator')
    first = client.post('/api/commands', headers={'idempotency-key': key}, json=command)
    changed = _assign_command(h, workspace, shift, h.seed['people'][1])
    rejected = client.post('/api/commands', headers={'idempotency-key': key}, json=changed)

    assert first.status_code == 200
    assert rejected.status_code == 409
    assert rejected.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'


def test_legacy_command_receipt_hash_shape_is_preserved(h):
    key=str(uuid.uuid4());cmd={'kind':'workspace.create','target_id':None,'expected_revision':None,
        'payload':{'name':'Legacy-shape hash','starts_on':'2026-10-04','ends_on':'2026-10-11'}}
    response=h.client('coordinator').post('/api/commands',headers={'idempotency-key':key},json=cmd)
    assert response.status_code==200
    with transaction(h.app.state.engine) as db:
        row=db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.key==key))
        assert row.payload_hash==snapshot_hash(cmd)


def test_preview_fingerprint_is_rejected_on_unsupported_commands(h):
    response=h.client('coordinator').post('/api/commands',headers={'idempotency-key':str(uuid.uuid4())},json={
        'kind':'workspace.create','expected_fingerprint':'a'*64,'payload':{
            'name':'Not a previewable change','starts_on':'2026-10-04','ends_on':'2026-10-11'}})
    assert response.status_code==422 and response.json()['error']['code']=='UNSUPPORTED_PRECONDITION'


def test_comparison_ignores_clone_ids_and_shows_exact_participant_changes(h):
    old=h.shift(h.workspace())['workspace'];pub=h.publish(old)
    new=h.workspace('Amendment for comparison',base=pub['publication_id'])
    identical=h.get('approver',f'/api/workspaces/{old["id"]}/compare?other_id={new["id"]}')
    assert identical['scope_matches'] and identical['unchanged']==1 and identical['total']==0
    row=h.detail(new)['shifts'][0]
    payload={key:row[key] for key in ['assignment_type_id','location_id','service_ids','start','end','attributes']}
    payload.update({'workspace_id':new['id'],'shift_id':row['id'],'participants':[{
        'person_id':h.seed['people'][1],'start':row['start'],'end':row['end']}]})
    new=h.command('coordinator','shift.update',payload,revision=new['revision'])['workspace']
    result=h.get('approver',f'/api/workspaces/{old["id"]}/compare?other_id={new["id"]}')
    assert result['counts']=={'added':0,'removed':0,'changed':1}
    change=result['changes'][0]
    assert change['fields']==['participants'] and change['basis']=='unique_type_location_time_anchor'
    assert change['before']['participants'][0]['person_id']==h.seed['people'][0]
    assert change['after']['participants'][0]['person_id']==h.seed['people'][1]
    h.get('staff',f'/api/workspaces/{old["id"]}/compare?other_id={new["id"]}',status=403)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('field', ['team_lead', 'supervisor_person_id', 'fixed'])
def test_participant_only_changes_are_compared_and_previewed(request, database, field):
    h = request.getfixturevalue(database)
    old = h.shift(h.workspace())['workspace']
    publication = h.publish(old)
    new = h.workspace('Participant-only amendment', base=publication['publication_id'])
    before = h.detail(new)
    row = before['shifts'][0]
    participant = {key: row['participants'][0][key] for key in s.ParticipantInput.model_fields}
    participant[field] = h.seed['people'][1] if field == 'supervisor_person_id' else True
    payload = {key: row[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=new['id'], shift_id=row['id'], participants=[participant])
    command = {'kind': 'shift.update', 'expected_revision': new['revision'], 'payload': payload}
    response = h.client('coordinator').post('/api/planner/preview', json=command)
    if field == 'supervisor_person_id':
        # This fixture has no supervision rule or present supervisor: D65 refuses the newly unsafe link.
        assert response.status_code == 422, response.text
        assert response.json()['error']['code'] == 'EDIT_INVALID'
        assert h.detail(new) == before
        return
    assert response.status_code == 200, response.text
    previewed = response.json()
    impact = previewed['impact']
    assert impact['counts'] == {'added': 0, 'removed': 0, 'changed': 1}
    assert impact['unchanged'] == 0 and h.detail(new) == before
    assert previewed['source_fingerprint'] == before['current_fingerprint']
    applied = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())},
        json={**command, 'expected_fingerprint': previewed['source_fingerprint']})
    assert applied.status_code == 200, applied.text
    compared = h.get('approver', f'/api/workspaces/{old["id"]}/compare?other_id={new["id"]}')
    assert compared['counts'] == impact['counts'] and compared['unchanged'] == 0
    for result in (impact, compared):
        change = result['changes'][0]
        assert change['fields'] == ['participants']
        left, right = change['before']['participants'][0], change['after']['participants'][0]
        assert set(right) == set(s.ParticipantInput.model_fields)
        assert {key for key in left if left[key] != right[key]} == {field}
        assert right[field] == participant[field]


def test_comparison_uses_contract_defaults_for_legacy_participants(h):
    from apps.api.services.planner_tools import difference
    row = h.detail(h.shift(h.workspace())['workspace'])['shifts'][0]
    legacy = copy.deepcopy(row)
    legacy['id'] = str(uuid.uuid4())
    legacy['participants'][0]['id'] = str(uuid.uuid4())
    legacy['participants'][0]['shift_id'] = legacy['id']
    for key, field in s.ParticipantInput.model_fields.items():
        if not field.is_required():
            legacy['participants'][0].pop(key)
    result = difference([row], [legacy])
    assert result['unchanged'] == 1 and result['changes'] == []


def test_comparison_partial_pages_and_different_periods_are_explicit(h):
    one=h.shift(h.workspace())['workspace']
    two=h.shift(h.workspace('Other period',start='2026-10-11',end='2026-10-18'),
        start='2026-10-11T08:00:00+03:00',end='2026-10-11T12:00:00+03:00')['workspace']
    url=f'/api/workspaces/{one["id"]}/compare?other_id={two["id"]}&limit=1'
    result=h.get('coordinator',url)
    assert not result['scope_matches'] and result['has_more'] and result['total']==2
    second=h.get('coordinator',url+'&offset=1')
    assert not second['has_more'] and second['changes']!=result['changes']
    h.get('coordinator',url.replace('limit=1','limit=1000'),status=422)


def test_exact_same_id_matching_does_not_reassign_identical_duplicate_lineage(h):
    from apps.api.services.planner_tools import difference
    w=h.shift(h.workspace())['workspace'];a=h.detail(w)['shifts'][0]
    b=copy.deepcopy(a);b['id']=str(uuid.uuid4());after_a=copy.deepcopy(a);after_a['cancelled']=True
    result=difference([a,b],[after_a,b])
    assert result['unchanged']==1 and result['changes'][0]['before_id']==a['id']
    assert result['changes'][0]['basis']=='stable_session_id'


@pytest.mark.parametrize('mode',['week','month','person','list'])
def test_private_saved_view_round_trip_and_scope(h,mode):
    definition={'mode':mode,'search':'Synthetic search','person_id':h.seed['people'][0],
        'location_id':h.seed['locations'][0],'assignment_type_id':h.seed['type']}
    before=h.get('coordinator','/api/pulse')['input_revision']
    row=h.command('coordinator','planner_view.create',{'name':'My '+mode,'definition':definition})['view']
    saved=h.get('coordinator','/api/planner/views')['views']
    assert saved==[row] and saved[0]['definition']==s.PlannerViewSettings(**definition).model_dump(mode='json')
    assert h.get('approver','/api/planner/views')['views']==[]
    h.command('approver','planner_view.update',{'name':'Another user','definition':{}},row['id'],row['revision'],status=404)
    row=h.command('coordinator','planner_view.update',{'name':'Updated '+mode,'definition':{'mode':'list'}},row['id'],row['revision'])['view']
    assert row['definition']['person_id'] is None and row['revision']==2
    assert h.get('coordinator','/api/pulse')['input_revision']==before
    h.command('coordinator','planner_view.delete',{'reason':'Remove my own preference'},row['id'],row['revision'])
    assert h.get('coordinator','/api/planner/views')['views']==[]


def test_saved_view_limits_schema_and_permissions_fail_closed(h):
    for role in ['staff','operator','credentialer']:
        h.command(role,'planner_view.create',{'name':'Forbidden','definition':{}},status=403)
    for definition in [{'mode':'unknown'},{'schema_version':2},{'search':'x'*121},{'clinical_override':True}]:
        h.command('coordinator','planner_view.create',{'name':'Invalid','definition':definition},status=422)
    h.command('coordinator','planner_view.create',{'name':'Unknown catalogue','definition':{'location_id':str(uuid.uuid4())}},status=404)
    for i in range(20):
        h.command('coordinator','planner_view.create',{'name':str(i),'definition':{}})
    assert len(h.get('coordinator','/api/planner/views')['views'])==20
    h.command('coordinator','planner_view.create',{'name':'Too many','definition':{}},status=409)


def test_workspace_list_reports_total_and_does_not_silently_truncate(h):
    for i in range(3):h.workspace(str(i))
    page=h.get('coordinator','/api/workspaces?limit=2')
    assert page['total']==3 and page['has_more'] and len(page['workspaces'])==2
    last=h.get('coordinator','/api/workspaces?limit=2&offset=2')
    assert last['total']==3 and not last['has_more'] and len(last['workspaces'])==1
    assert not {w['id'] for w in page['workspaces']} & {w['id'] for w in last['workspaces']}
    h.get('coordinator','/api/workspaces?limit=101',status=422)


def test_preview_shared_reply_keeps_disclosed_leave_out_of_saved_validation_and_undo(h):
    from tests.test_leave_disclosure import setting
    from tests.test_series_overlap_api import state
    from tests.test_series_overlap_leave_flags import absence, NOTE
    from tests.test_series_overlap_schedule import DAY

    workspace = h.shift(h.workspace(), *DAY)['workspace']
    absence(h)
    setting(h, 'details')
    workspace = h.lifecycle(workspace, 'validate')['workspace']
    before = h.detail(workspace)
    shift = before['shifts'][0]
    payload = {key: shift[key] for key in ('assignment_type_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=workspace['id'], shift_id=shift['id'], location_id=h.seed['locations'][1],
                   participants=[{'person_id': h.seed['people'][0], 'start': DAY[0], 'end': DAY[1]}],
                   override_reason='Review synthetic leave conflict before changing the location')
    command = {'kind': 'shift.update', 'expected_revision': workspace['revision'], 'payload': payload}
    with transaction(h.app.state.engine) as db:
        stored = copy.deepcopy(db.get(m.Workspace, workspace['id']).validation)
        undo_count = db.scalar(select(func.count()).select_from(m.ChangeSet))
    persisted = state(h)
    for _ in range(2):
        response = h.client('coordinator').post('/api/planner/preview', json=command)
        assert response.status_code == 200, response.text
        assert any(row['code'] == 'UNAVAILABLE' and row['leave']['note'] == NOTE
                   for row in response.json()['validation']['findings'])
        assert state(h) == persisted and h.detail(workspace) == before
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Workspace, workspace['id']).validation == stored
            assert db.scalar(select(func.count()).select_from(m.ChangeSet)) == undo_count
    applied = h.command('coordinator', 'shift.update', payload, revision=workspace['revision'])
    assert applied['workspace']['revision'] == workspace['revision'] + 1
    with transaction(h.app.state.engine) as db:
        assert NOTE not in str(db.get(m.Workspace, workspace['id']).validation)
        assert db.scalar(select(func.count()).select_from(m.ChangeSet)) == undo_count + 1

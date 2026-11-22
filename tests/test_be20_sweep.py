"""Sweep-2 independent review, occupied-place merge, and implicit department rota regressions."""
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.api.services import reviews
from apps.api.services.read_projection_warming import warm_all
from apps.api.services.read_projections import CANONICAL_AUDIENCE
from apps.worker.main import process_one
from packages.domain.workspace_merge import plan, overlay
from packages.domain.validation import evaluate
from .governance_support import identity
from .test_published_changes import issued, change
from .test_p2_groups import fork, preview, apply
from .test_planning_groups_drafts import group, settings, integrated, edit, REASON


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('amendment', [False, True])
def test_return_resubmit_approve_is_not_contribution_and_home_uses_step_authority(h, amendment):
    identity(h, 'programme_lead', ['programme_lead'])
    if amendment:
        pub, shifts = issued(h)
        result = change(h, pub, shifts[0])
        w = h.command('coordinator', 'publication.changes.submit', {'reason': REASON},
                      target=pub, revision=result['workspace']['revision'])['workspace']
    else:
        w = h.lifecycle(h.shift(h.workspace())['workspace'], 'submit')['workspace']
    w = h.lifecycle(w, 'reopen', role='approver')['workspace']
    w = h.lifecycle(w, 'submit')['workspace']
    data = h.get('approver', '/api/workspaces/' + w['id'] + '/review')
    assert not data['reviewer_involved'] and data['review_progress']['can_decide']
    assert data['review_progress']['decision_role'] == 'approver'
    assert any(i['key'] == 'reviews' and i['rota_id'] == w['id'] for i in h.get('approver', '/api/home')['attention'])
    assert not h.get('programme_lead', '/api/workspaces/' + w['id'])['review_progress']['can_decide']
    assert h.lifecycle(w, 'approve', role='approver')['workspace']['status'] == 'approved'
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, w['id'])
        assert not reviews.contributed(db, workspace, h.seed['identities']['approver'])
        assert reviews.contributed(db, workspace, h.seed['identities']['coordinator'])


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_amendment_review_on_home_uses_stored_fingerprint(h, monkeypatch):
    def submit(kind, target, revision):
        # Read Home before a workspace detail request can warm the fingerprint.
        response = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid4())}, json={
            'kind': kind, 'target_id': target, 'expected_revision': revision, 'payload': {'reason': REASON}})
        assert response.status_code == 200, response.text
        return response.json()['workspace']

    identity(h, 'programme_lead', ['programme_lead'])
    pub, shifts = issued(h)
    result = change(h, pub, shifts[0])
    w = submit('publication.changes.submit', pub, result['workspace']['revision'])

    def no_snapshot(*args, **kwargs):
        pytest.fail('Home must use the stored scheduling fingerprint')

    def review_items(role):
        with monkeypatch.context() as guard:
            guard.setattr(snapshots, 'domain_snapshot', no_snapshot)
            return [item for item in h.get(role, '/api/home')['attention'] if item['key'] == 'reviews']

    assert [item['rota_id'] for item in review_items('approver')] == [w['id']]
    w = h.lifecycle(w, 'reopen', role='approver')['workspace']
    assert not review_items('approver')
    w = submit('workspace.submit', w['id'], w['revision'])
    items = review_items('approver')
    assert [(item['rota_id'], item['amendment']['changes']) for item in items] == [(w['id'], 1)]
    assert not review_items('programme_lead')
    assert not review_items('coordinator')
    assert h.lifecycle(w, 'approve', role='programme_lead', status=403)['error']['code'] == 'REVIEW_STEP_ROLE'


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_home_hides_amendment_with_stale_worker_fingerprint(h):
    pub, shifts = issued(h)
    made = change(h, pub, shifts[0])
    # Submit without the harness's detail read, which can warm the fingerprint.
    response = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid4())}, json={
        'kind': 'publication.changes.submit', 'target_id': pub, 'expected_revision': made['workspace']['revision'],
        'payload': {'reason': REASON}})
    assert response.status_code == 200, response.text
    w = response.json()['workspace']
    engine = h.app.state.engine
    key = (w['id'], CANONICAL_AUDIENCE, 'fingerprint')
    with transaction(engine) as db:
        assert db.get(m.WorkspaceReadProjection, key) is None
    warm_all(engine)
    with transaction(engine) as db:
        saved = db.get(m.WorkspaceReadProjection, key)
        assert saved is not None and saved.value == w['submitted_fingerprint']

    def review_ids():
        return [item['rota_id'] for item in h.get('approver', '/api/home')['attention'] if item['key'] == 'reviews']

    assert w['id'] in review_ids()
    h.policy({'kind': 'min_rest', 'hours': 11})
    assert w['id'] not in review_ids()
    assert h.lifecycle(w, 'approve', role='approver', status=409)['error']['code'] == 'STALE_INPUTS'


def test_groups_refuse_missing_department_coordinator_and_default_existing_and_new_rotas(h):
    old = h.workspace('Existing default rota')
    definition = {'name': 'Configured population', 'starts_on': old['starts_on'], 'ends_on': old['ends_on'],
        'owner_identity_ids': [h.seed['identities']['coordinator']], 'primary_priority': 0,
        'membership': {'match': 'all', 'conditions': [{'field': 'person_ids', 'values': h.seed['people']}]}}
    refusal = h.command('admin', 'planning_group.create', {'definition': definition, 'reason': REASON}, status=422)
    assert refusal['error']['code'] == 'PLANNING_COORDINATOR_REQUIRED'
    setting = settings(h)
    g = h.command('admin', 'planning_group.create', {'definition': definition, 'reason': REASON})['group']
    assert h.detail(old)['workspace']['purpose'] == 'integrated'
    other = h.workspace('Second alternative')
    assert h.detail(other)['workspace']['purpose'] == 'alternative'
    private = fork(h, old)
    assert h.detail(private)['workspace']['purpose'] == 'sandbox'
    explicit = h.command('coordinator', 'workspace.create', {'name': 'Explicit alternative', 'starts_on': old['starts_on'],
        'ends_on': old['ends_on'], 'purpose': 'alternative'})['workspace']
    assert h.detail(explicit)['workspace']['purpose'] == 'alternative'
    assert h.command('admin', 'planning.settings', {'definition': {'coordinator_identity_ids': []}, 'reason': REASON},
        revision=setting['revision'], status=422)['error']['code'] == 'PLANNING_COORDINATOR_REQUIRED'
    future = h.command('coordinator', 'workspace.create', {'name': 'Group default created', 'starts_on':'2026-11-01',
        'ends_on':'2026-11-08'})['workspace']
    group(h, starts_on='2026-11-01', ends_on='2026-11-08')
    assert h.detail(future)['workspace']['purpose'] == 'integrated'
    assert g['definition']['active']


def test_all_groups_generation_has_no_previous_group_population(h):
    w, _, g = integrated(h)
    first = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind':'generation',
        'planning_group_version_id':g['id'], 'lookahead':'none'}, revision=w['revision'])['job']
    h.command('coordinator', 'job.cancel', {'reason': REASON}, first['id'], first['revision'])
    # Configured department coordinator can ask for all groups with no person selector.
    all_groups = h.command('admin', 'job.create', {'workspace_id':w['id'], 'kind':'generation'}, revision=w['revision'])['job']
    assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        data = db.get(m.Job, all_groups['id']).input_snapshot
        assert not data.get('planning_group_id') and not data.get('generation_scope')
        assert set(data['people']) >= set(h.seed['people'])


def oracle(base_people, yours, theirs, maximum=1):
    ids = {key: str(uuid4()) for key in set(base_people + yours + theirs)}
    sid = str(uuid4())
    def content(people):
        return {'workspace':{}, 'shifts':[{'id':sid,'start':'2026-10-04T08:00:00+03:00','end':'2026-10-04T12:00:00+03:00',
            'type_snapshot':{'definition':{'requirements':[{'minimum':maximum,'maximum':maximum}]}},
            'participants':[{'person_id':ids[key],'start':'2026-10-04T08:00:00+03:00','end':'2026-10-04T12:00:00+03:00'} for key in people]}]}
    base, source, target = map(content, (base_people, yours, theirs))
    bundles = plan(str(uuid4()),base,base,source,target,{sid:sid},assignment_allowed=lambda *args:True,adjustment_allowed=lambda *args:True)
    return target,bundles,ids


@pytest.mark.parametrize('base,yours,theirs,places', [(['a'],['b'],['c'],1),([],['b'],['c'],1),
    (['a'],[],['c'],1),(['a'],['b'],[],1),(['a'],['a','b'],['a','c'],2)])
def test_occupied_place_three_way_oracle_requires_choice_and_replaces_atomically(base,yours,theirs,places):
    target,bundles,ids=oracle(base,yours,theirs,places)
    assert len(bundles)==1 and bundles[0]['kind']=='both_changed'
    assert bundles[0]['allowed_choices']==['rota','yours']
    kept=overlay(target,bundles,{bundles[0]['id']:'rota'})
    chosen=overlay(target,bundles,{bundles[0]['id']:'yours'})
    assert {p['person_id'] for p in kept['shifts'][0]['participants']}=={ids[k] for k in theirs}
    assert {p['person_id'] for p in chosen['shifts'][0]['participants']}=={ids[k] for k in yours}
    assert len(chosen['shifts'][0]['participants'])<=places
    assert len({p['id'] for p in chosen['shifts'][0]['participants'] if 'id' in p})==len([p for p in chosen['shifts'][0]['participants'] if 'id' in p])


def test_noncompeting_places_and_identical_replacements_are_not_conflicts():
    target,bundles,ids=oracle(['a','b'],['c','b'],['a','d'],2)
    assert all(b['kind']=='fits' for b in bundles)
    chosen=overlay(target,bundles,{b['id']:'yours' for b in bundles})
    assert {p['person_id'] for p in chosen['shifts'][0]['participants']}=={ids['c'],ids['d']}
    _,bundles,_=oracle(['a'],['b'],['b'])
    assert all(b['kind']=='fits' for b in bundles)


def test_validator_distinguishes_surplus_places_from_a_configured_safety_ceiling(h):
    w=h.shift(h.workspace(),people=h.seed['people'][:2])['workspace'];data=h.snapshot(w)
    check=evaluate(data)
    assert any(f['code']=='SURPLUS_COVER' and f['severity']=='warning' for f in check['findings'])
    for shift in data['shifts']:
        for requirement in shift['type_snapshot']['definition']['requirements']:
            requirement['maximum']=1
    assert any(f['code']=='MAXIMUM_PARTICIPANTS' and f['severity']=='error' for f in evaluate(data)['findings'])


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_surplus_merge_cannot_bypass_places_even_without_a_clinical_maximum(h):
    w, sh, g = integrated(h, people=[h.seed['people'][0]])
    h.command('admin', 'planning_group.update', {'definition': {**g['definition'], 'membership': {
        'match':'all','conditions':[{'field':'person_ids','values':h.seed['people'][:2]}]}},'reason':REASON},
        g['group_id'], g['revision'])
    source=fork(h,w);local=h.detail(source)['shifts'][0]
    source=edit(h,source,local,[*local['participants'],{'person_id':h.seed['people'][1],
        'start':local['start'],'end':local['end']}])['workspace']
    proposal=preview(h,source,w)
    assert not proposal['merge']['can_apply']
    assert apply(h,w,proposal,status=409)['error']['code']=='MERGE_CONFLICT'
    assert len(h.detail(w)['shifts'][0]['participants'])==1


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('mode', ['none', 'type', 'details'])
def test_absence_reason_and_note_follow_leave_display_for_decider_and_decision_reply(h, mode):
    pub, shifts = issued(h)
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        department.leave_reason_display = mode
        department.revision += 1
    request = h.command('staff', 'request.create', {'kind':'absence_report', 'publication_id':pub,
        'assignment_id':shifts[0]['participants'][0]['id'], 'reason':'Private explanation', 'category': 'other',
        'note':'Staff cover explanation'})['record']
    def check(item, progress, own=False):
        assert item['details'].get('absence_reason') == ('other' if own or mode != 'none' else None)
        assert item['details'].get('note') == ('Staff cover explanation' if own or mode == 'details' else None)
        assert item['absence_progress'] == progress and 'private_reason' not in item
    check(request, 'waiting_for_approval', own=True)
    item = h.get('approver', '/api/requests/all?id=' + request['id'])['item']
    check(item, 'waiting_for_approval')
    approved = h.command('approver', 'request.decide', {'decision':'approve','reason':REASON},
        request['id'], request['revision'])['record']
    check(approved, 'finding_cover')
    check(h.get('approver', '/api/requests/all?id=' + request['id'])['item'], 'finding_cover')
    h.get('staff_two', '/api/requests/all?id=' + request['id'], status=404)
    with transaction(h.app.state.engine) as db:
        absence = db.scalar(select(m.Availability).where(m.Availability.request_id == request['id']))
        assert absence.private_reason == '' and not absence.leave_type


def test_private_fork_queues_a_complete_current_check_instead_of_blocking_creation(h):
    from apps.worker.main import process_one
    from apps.api.services.planning import complete_check
    w = h.shift(h.workspace())['workspace']
    source = fork(h,w)
    with transaction(h.app.state.engine) as db:
        draft = db.get(m.Workspace,source['id'])
        assert draft.validation is None
        job = db.scalar(select(m.Job).where(m.Job.workspace_id == source['id'],m.Job.kind == 'validation'))
        assert job.status == 'queued'
    assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        draft = db.get(m.Workspace,source['id'])
        assert complete_check(draft.validation,h.snapshot(source),db.get(m.Department,draft.department_id).generation_defaults)


def test_temporally_separate_places_merge_independently():
    target,bundles,ids=oracle(['a','b'],['c','b'],['a','d'],2)
    # The full-session two-place oracle above is independent too; now divide it into one-place slots.
    sid=target['shifts'][0]['id']
    def content(people):
        value=deepcopy(target);value['shifts'][0]['participants']=[]
        value['shifts'][0]['type_snapshot']['definition']['requirements']=[{'minimum':1,'maximum':1}]
        for key in people:
            first=key in ('a','c')
            value['shifts'][0]['participants'].append({'person_id':ids[key],
                'start':'2026-10-04T08:00:00+03:00' if first else '2026-10-04T10:00:00+03:00',
                'end':'2026-10-04T10:00:00+03:00' if first else '2026-10-04T12:00:00+03:00'})
        return value
    base,source,target=map(content,(['a','b'],['c','b'],['a','d']))
    bundles=plan(str(uuid4()),base,base,source,target,{sid:sid},assignment_allowed=lambda *args:True,adjustment_allowed=lambda *args:True)
    assert all(b['kind']=='fits' for b in bundles)
    merged=overlay(target,bundles,{b['id']:'yours' for b in bundles})
    assert {p['person_id'] for p in merged['shifts'][0]['participants']}=={ids['c'],ids['d']}


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_concurrent_replacements_need_choice_and_apply_one_checked_candidate(h, monkeypatch):
    own, yours, theirs = h.seed['people'][:3]
    w, sh, g = integrated(h, people=[own])
    h.command('admin','planning_group.update',{'definition':{**g['definition'],'membership':{
        'match':'all','conditions':[{'field':'person_ids','values':[own,yours,theirs]}]}},'reason':REASON},
        g['group_id'],g['revision'])
    source=fork(h,w);local=h.detail(source)['shifts'][0]
    source=edit(h,source,local,[{'person_id':yours,'start':local['start'],'end':local['end']}])['workspace']
    w=edit(h,w,sh,[{'person_id':theirs,'start':sh['start'],'end':sh['end']}])['workspace']
    from packages.domain import score
    scores=[];original_score=score.schedule_score
    def counted_score(*args,**options):
        scores.append(True)
        return original_score(*args,**options)
    monkeypatch.setattr(score,'schedule_score',counted_score)
    from packages.domain import validation
    partials=[];original_validate=validation.evaluate
    def count_scope(data, **options):
        if data.get('_partial'):
            partials.append(data['_partial'])
        return original_validate(data, **options)
    monkeypatch.setattr(validation, 'evaluate', count_scope)
    proposal=preview(h,source,w)
    assert not scores  # preview still checks every constraint, without an unused department score
    assert partials and partials[-1]['complete'] and not partials[-1]['shifts']
    bundle,=proposal['merge']['bundles']
    assert bundle['kind']=='both_changed' and not proposal['merge']['selected_bundle_ids']
    assert len(h.detail(w)['shifts'][0]['participants'])==1
    calls=[];original=validation.evaluate
    def counted(data,**options):
        if data.get('_partial'):
            partials.append(data['_partial'])
        calls.append(data['fingerprint'])
        return original(data,**options)
    monkeypatch.setattr(validation,'evaluate',counted)
    merged=apply(h,w,proposal,bundle_ids=[bundle['id']],choices={bundle['id']:'yours'})['workspace']
    assert len(calls)==1  # one canonical accepted-candidate check, replayed after exact manifest verification
    assert partials[-1]['complete'] and partials[-1]['shifts'] == {sh['id']}
    assert len(scores)==1
    detail=h.detail(merged)
    assert [p['person_id'] for p in detail['shifts'][0]['participants']]==[yours]
    assert not any(f['code']=='SURPLUS_COVER' for f in detail['workspace']['validation']['findings'])
    fresh=evaluate(h.snapshot(merged))
    for key in ('findings','coverage','valid','error_count','warning_count'):
        assert detail['workspace']['validation'][key]==fresh[key]
    assert detail['workspace']['validation']['score']


def test_explicit_designation_overrides_an_implicit_default_without_rewriting_access_history(h):
    group(h)
    first=h.workspace('Implicit group rota')
    second=h.workspace('Explicit choice')
    assert h.detail(first)['workspace']['purpose']=='integrated'
    chosen=h.command('admin','workspace.designate',{'reason':REASON},second['id'],second['revision'])['workspace']
    assert chosen['purpose']=='integrated' and h.detail(first)['workspace']['purpose']=='alternative'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace,first['id']).access_version_id is None


def test_old_check_contract_cannot_hide_surplus_in_an_unchanged_session(h):
    w=h.shift(h.workspace(),people=h.seed['people'][:2])['workspace']
    w=h.shift(w,start='2026-10-05T08:00:00+03:00',end='2026-10-05T12:00:00+03:00')['workspace']
    data=h.snapshot(w);previous=evaluate(data,session_parts=True)
    previous['contracts'].pop('cover_places')
    previous['findings']=[f for f in previous['findings'] if f['code']!='SURPLUS_COVER']
    partial={**data,'_partial':{'shifts':{data['shifts'][1]['id']},'people':set(data['people']),
        'complete':True,'previous':previous}}
    assert any(f['code']=='SURPLUS_COVER' and f['shift_id']==data['shifts'][0]['id'] for f in evaluate(partial)['findings'])


def test_legacy_review_permission_still_requires_current_inputs(h):
    w=h.lifecycle(h.shift(h.workspace())['workspace'],'submit')['workspace']
    with transaction(h.app.state.engine,write=True) as db:
        candidate=db.get(m.Workspace,w['id']);candidate.review_snapshot=None
        candidate.submitted_fingerprint='0'*64
    progress=h.get('approver','/api/workspaces/'+w['id']+'/review')['review_progress']
    assert progress['stale_inputs'] and not progress['can_decide']

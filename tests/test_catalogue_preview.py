"""Exact content/revision/authority-bound impact. A preview must leave no writes."""
import copy
import uuid
import pytest
from sqlalchemy import func, select
from apps.api import models as m
from apps.api.db import transaction
from .test_resources_calendars import pool, typ, calendar, attach


def change(resource, **values):
    return {'kind':'catalogue.update','target_id':resource['id'],'expected_revision':resource['revision'],
        'payload':{'catalogue':'resource_pools','value':{'name':resource['name'],'starts_on':resource['starts_on'],
            'capacity':resource['capacity'],**values},'reason':'Preview a synthetic capacity change'}}


def preview(h,cmd,role='admin',status=200):
    response=h.client(role).post('/api/catalogue/preview',json=cmd)
    assert response.status_code==status,response.text
    return response.json()


def apply(h,cmd,token,status=200,key=None):
    response=h.client('admin').post('/api/commands',headers={'idempotency-key':key or str(uuid.uuid4())},
        json={**cmd,'expected_fingerprint':token})
    assert response.status_code==status,response.text
    return response.json()


def state(h):
    with transaction(h.app.state.engine) as db:
        org=db.get(m.Organisation,h.seed['organisation_id'])
        return [db.scalar(select(func.count()).select_from(cls)) for cls in [m.ResourcePool,m.OperatingCalendar,
            m.Workspace,m.Shift,m.Assignment,m.DefinitionHistory,m.ChangeSet,m.AuditEvent,m.CommandReceipt,m.Outbox]]+[org.input_revision,org.availability_revision]


def test_preview_uses_proposed_capacity_but_rolls_back_everything(h):
    r=pool(h,capacity=2);t=typ(h,r,resources=[{'resource_pool_id':r['id'],'units':2}])
    result=h.shift(h.workspace(),typ=t['id']);w=result['workspace'];cmd=change(r,capacity=1)
    before=state(h);detail=h.detail(w);value=preview(h,cmd)
    assert state(h)==before and h.detail(w)==detail
    assert value['before']['capacity']==2 and value['proposed']['capacity']==1
    assert value['workspace_count']==1 and not value['partial'] and not value['committed']
    assert 'POOLED_RESOURCE_CAPACITY' in {f['code'] for f in value['impacts'][0]['introduced']}
    assert value['impacts'][0]['before_error_count']==0 and value['impacts'][0]['after_error_count']>0
    saved=apply(h,cmd,value['source_fingerprint'])
    assert saved['record']['capacity']==1
    assert h.detail(w)['workspace']['revision']==w['revision']
    assert not h.lifecycle(w,'validate')['validation']['valid']


def test_pattern_preview_does_not_revalidate_or_rewrite_existing_duties(h, monkeypatch):
    from .test_series_reconciliation import template, materialise
    series = template(h)
    workspace = materialise(h, series)
    before = h.detail(workspace)
    value = {k: series[k] for k in ('name', 'starts_on', 'ends_on', 'assignment_type_id', 'location_id', 'definition')}
    value = copy.deepcopy(value)
    value['definition']['days'][0]['start_time'] = '09:00'
    cmd = {'kind': 'catalogue.update', 'target_id': series['id'], 'expected_revision': series['revision'],
           'payload': {'catalogue': 'recurring_series', 'value': value, 'reason': 'Change future pattern timing'}}
    monkeypatch.setattr('apps.api.services.catalogue_preview.evaluate',
                        lambda *_: pytest.fail('Pattern edits do not change materialised duties'))
    result = preview(h, cmd)
    assert result['inventory_only'] and result['impacts'] == [] and not result['committed']
    assert h.detail(workspace) == before
    saved = apply(h, cmd, result['source_fingerprint'])['record']
    assert saved['definition'] == value['definition'] and saved['revision'] == series['revision'] + 1
    after = h.detail(workspace)
    assert after['shifts'] == before['shifts'] and after['workspace'] == before['workspace']
    assert after['current_input_revision'] == before['current_input_revision'] + 1
    assert after['current_fingerprint'] == before['current_fingerprint']


@pytest.mark.parametrize('mutate',['reason','value','target','kind','revision'])
def test_exact_proposed_content_and_reason_must_match(h,mutate):
    r=pool(h);cmd=change(r,capacity=2);value=preview(h,cmd);other=copy.deepcopy(cmd)
    if mutate=='reason':other['payload']['reason']='An entirely different reason'
    elif mutate=='value':other['payload']['value']['capacity']=3
    elif mutate=='target':other['target_id']=str(uuid.uuid4())
    elif mutate=='kind':other['kind']='catalogue.create'
    else:other['expected_revision']+=1
    assert apply(h,other,value['source_fingerprint'],409)['error']['code']=='STALE_PREVIEW'


@pytest.mark.parametrize('mutate',['new_workspace','edit_workspace','availability','configuration'])
def test_current_input_and_draft_changes_invalidate_configuration_preview(h,mutate):
    r=pool(h);w=h.shift(h.workspace())['workspace'];cmd=change(r,capacity=2);p=preview(h,cmd)
    if mutate=='new_workspace':h.workspace('A new active draft')
    elif mutate=='edit_workspace':h.shift(w,start='2026-10-05T08:00:00+03:00',end='2026-10-05T12:00:00+03:00')
    elif mutate=='availability':h.command('coordinator','availability.record',{'person_id':h.seed['people'][0],
        'start':'2026-10-04T08:00:00+03:00','end':'2026-10-04T09:00:00+03:00','kind':'actual_absence','private_reason':'A secret never displayed'})
    else:pool(h,name='Another configured pool')
    assert apply(h,cmd,p['source_fingerprint'],409)['error']['code']=='STALE_PREVIEW'


def test_successful_receipt_replays_even_after_inputs_change(h):
    r=pool(h);cmd=change(r,capacity=2);p=preview(h,cmd);key=str(uuid.uuid4())
    saved=apply(h,cmd,p['source_fingerprint'],key=key);h.workspace('Later draft')
    assert apply(h,cmd,p['source_fingerprint'],key=key)==saved


@pytest.mark.parametrize('role',['staff','coordinator','approver','credentialer','auditor','operator'])
def test_configuration_preview_permissions_match_real_commands(h,role):
    r=pool(h);preview(h,change(r),role,status=403)


def test_configuration_preview_csrf_and_unknown_command_fail_closed(h):
    r=pool(h);cmd=change(r)
    response=h.client('admin').post('/api/catalogue/preview',json=cmd,headers={'x-csrf-token':'wrong'})
    assert response.status_code==403
    preview(h,{**cmd,'kind':'workspace.publish'},status=422)
    preview(h,{**cmd,'payload':{**cmd['payload'],'catalogue':'unknown'}},status=422)
    preview(h,{**cmd,'target_id':str(uuid.uuid4())},status=404)


def test_failed_schema_preview_rolls_back_without_bumping_inputs(h):
    r=pool(h);before=state(h)
    preview(h,change(r,capacity=-1),status=422)
    assert state(h)==before


def test_new_catalogue_preview_and_apply_do_not_create_duplicate_records(h):
    cmd={'kind':'catalogue.create','payload':{'catalogue':'operating_calendars','value':{
        'name':'Preview not a real row','starts_on':'2020-01-01','definition':{'timezone':'Asia/Riyadh'}},
        'reason':'Create bounded weekly opening'}}
    h.client('admin')  # Establish the audited sign-in before measuring preview side effects.
    before=state(h);p=preview(h,cmd)
    assert state(h)==before and p['before'] is None
    saved=apply(h,cmd,p['source_fingerprint'])
    records=h.get('admin','/api/catalogues')['operating_calendars']
    # The demo seed already contains the departmental working-days calendar.
    assert len(records)==2 and saved['record']['id'] in {record['id'] for record in records}


def test_current_publications_included_without_rewriting_frozen_snapshot(h):
    cal=calendar(h);attach(h,cal)
    w=h.shift(h.workspace())['workspace'];pub=h.publish(w)
    old=h.get('coordinator','/api/publications/'+pub['publication_id'])['snapshot']
    cmd={'kind':'catalogue.update','target_id':cal['id'],'expected_revision':cal['revision'],
        'payload':{'catalogue':'operating_calendars','value':{'name':cal['name'],'starts_on':cal['starts_on'],
            'definition':{'timezone':'Asia/Riyadh','week':[]}},'reason':'Propose closed opening'}}
    p=preview(h,cmd);assert p['impacts'][0]['status']=='published'
    assert 'CALENDAR_CLOSED' in {f['code'] for f in p['impacts'][0]['introduced']}
    apply(h,cmd,p['source_fingerprint'])
    assert h.get('coordinator','/api/publications/'+pub['publication_id'])['snapshot']==old


def test_fresh_type_change_shows_operational_and_explicit_refresh_consequences(h):
    w=h.shift(h.workspace())['workspace']
    t=next(t for t in h.get('admin','/api/catalogues')['assignment_types'] if t['id']==h.seed['type'])
    definition=copy.deepcopy(t['definition']);definition['requirements'][0]['minimum']=3
    cmd={'kind':'catalogue.update','target_id':t['id'],'expected_revision':t['revision'],
        'payload':{'catalogue':'assignment_types','value':{'name':t['name'],'starts_on':t['starts_on'],'definition':definition},'reason':'New staffing level'}}
    p=preview(h,cmd);impact=p['impacts'][0]
    assert 'COVERAGE_GAP' in {f['code'] for f in impact['introduced']}
    assert 'TYPE_VERSION_CHANGED' in {f['code'] for f in impact['saved_draft_validation']['findings']}
    assert h.detail(w)['workspace']['revision']==w['revision']


def test_colour_only_type_change_needs_no_draft_refresh(h):
    w=h.shift(h.workspace())['workspace']
    t=next(t for t in h.get('admin','/api/catalogues')['assignment_types'] if t['id']==h.seed['type'])
    definition={**copy.deepcopy(t['definition']),'colour':'violet'}
    cmd={'kind':'catalogue.update','target_id':t['id'],'expected_revision':t['revision'],
        'payload':{'catalogue':'assignment_types','value':{'name':t['name'],'description':t['description'],'starts_on':t['starts_on'],'definition':definition},'reason':'Planner colour'}}
    p=preview(h,cmd);apply(h,cmd,p['source_fingerprint'])
    codes={f['code'] for f in h.lifecycle(w,'validate')['workspace']['validation']['findings']}
    assert 'TYPE_VERSION_CHANGED' not in codes


@pytest.mark.parametrize('bounds', [(3, 5000), (40, 1)])
def test_preview_above_its_bound_reports_exact_totals_and_never_blocks_save(h, monkeypatch, bounds):
    # SW3-D-01: a department-scale forward scope used to refuse the preview (409), so the save was never sent.
    from apps.api.services import catalogue_preview
    r=pool(h)
    for n in range(4):h.shift(h.workspace('Bounded draft '+str(n)))
    monkeypatch.setattr(catalogue_preview, 'limits', lambda db: bounds)
    result=preview(h,change(r,capacity=2))
    assert result['partial'] and result['impacts']==[]
    assert (result['workspace_count'], result['session_count'])==(4, 4)
    apply(h,change(r,capacity=2),result['source_fingerprint'])
    assert next(p for p in h.get('admin','/api/catalogues')['resource_pools'] if p['id']==r['id'])['capacity']==2


def test_preview_bound_counts_only_upcoming_sessions(h, monkeypatch):
    from apps.api.services import catalogue_preview
    r=pool(h)
    w=h.workspace('Spans today','2026-10-01','2026-10-11')
    w=h.shift(w,start='2026-10-02T08:00:00+03:00',end='2026-10-02T12:00:00+03:00')['workspace']
    w=h.shift(w,start='2026-10-05T08:00:00+03:00',end='2026-10-05T12:00:00+03:00')['workspace']
    monkeypatch.setattr(catalogue_preview, 'limits', lambda db: (40, 1))
    result=preview(h,change(r,capacity=2))
    assert not result['partial'] and result['session_count']==1
    assert [i['workspace_id'] for i in result['impacts']]==[w['id']]


def test_catalogue_refuses_to_silently_omit_rules_at_display_bound(h):
    from datetime import date
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.Policy(department_id=h.seed['department_id'], name=f'Bound check {i}',
            kind='min_rest', definition={'rule': {'kind': 'min_rest', 'hours': 12}},
            owner='Synthetic owner', source='Synthetic display-bound check', starts_on=date(2026, 1, 1),
            created_by=h.seed['identities']['coordinator']) for i in range(4001))
    response = h.get('coordinator', '/api/catalogues', status=409)
    assert response['error']['code'] == 'CATALOGUE_LIMIT'


def test_programme_impact_is_operational_but_does_not_commit(h):
    cmd={'kind':'catalogue.create','payload':{'catalogue':'programmes','value':{
        'name':'Not a clinical credit engine','starts_on':'2020-01-01'},'reason':'Catalogue inventory'}}
    p=preview(h,cmd,role='coordinator')
    assert not p['inventory_only'] and not p['committed']


def test_completed_periods_do_not_accumulate_against_forward_operational_preview(h):
    r=pool(h)
    old=h.workspace('Completed historical period','2025-01-06','2025-01-13')
    old=h.shift(old,start='2025-01-06T08:00:00+03:00',end='2025-01-06T12:00:00+03:00')['workspace']
    issued=h.publish(old);snapshot=h.get('coordinator','/api/publications/'+issued['publication_id'])['snapshot']
    current=h.shift(h.workspace())['workspace']
    value=preview(h,change(r,capacity=2))
    assert value['workspace_count']==1 and value['impacts'][0]['workspace_id']==current['id']
    assert 'Completed periods' in value['scope'] and value['scope_starts_on']
    apply(h,change(r,capacity=2),value['source_fingerprint'])
    assert h.get('coordinator','/api/publications/'+issued['publication_id'])['snapshot']==snapshot


@pytest.mark.parametrize('bound', [(1, 5000), (40, 1)])
def test_discarded_drafts_are_outside_preview_and_its_bounds(h, monkeypatch, bound):
    from apps.api.services import catalogue_preview
    resource = pool(h)
    current = h.shift(h.workspace('Current draft'))['workspace']
    discarded = h.shift(h.workspace('Discarded draft'))['workspace']
    discarded = h.command('coordinator', 'workspace.archive', {'reason': 'Keep only as discarded history'},
                          discarded['id'], discarded['revision'])['workspace']
    before, detail = state(h), h.detail(discarded)
    # Each draft has one session. Discarded rows must consume neither bound, regardless of retained history.
    monkeypatch.setattr(catalogue_preview, 'limits', lambda db: bound)
    result = preview(h, change(resource, capacity=2))
    assert not result['partial'] and result['workspace_count'] == 1 and [i['workspace_id'] for i in result['impacts']] == [current['id']]
    assert 'discarded drafts' in result['scope']
    assert state(h) == before and h.detail(discarded) == detail


def test_discard_and_restore_each_invalidate_configuration_preview(h):
    resource = pool(h)
    workspace = h.shift(h.workspace())['workspace']
    command = change(resource, capacity=2)
    active = preview(h, command)
    workspace = h.command('coordinator', 'workspace.archive', {'reason': 'Discard this draft'},
                          workspace['id'], workspace['revision'])['workspace']
    assert apply(h, command, active['source_fingerprint'], 409)['error']['code'] == 'STALE_PREVIEW'
    discarded = preview(h, command)
    assert discarded['workspace_count'] == 0 and discarded['impacts'] == []
    workspace = h.command('coordinator', 'workspace.restore', {'reason': 'Restore this draft'},
                          workspace['id'], workspace['revision'])['workspace']
    assert apply(h, command, discarded['source_fingerprint'], 409)['error']['code'] == 'STALE_PREVIEW'
    restored = preview(h, command)
    assert restored['workspace_count'] == 1 and restored['impacts'][0]['workspace_id'] == workspace['id']
    assert apply(h, command, restored['source_fingerprint'])['record']['capacity'] == 2


def test_organisation_local_date_rollover_invalidates_forward_scope_preview(h,monkeypatch):
    from datetime import timedelta
    from apps.api.services import catalogue_preview
    r=pool(h);cmd=change(r,capacity=2);value=preview(h,cmd)
    original=catalogue_preview.operational_day
    monkeypatch.setattr(catalogue_preview,'operational_day',lambda db,actor:original(db,actor)+timedelta(days=1))
    assert apply(h,cmd,value['source_fingerprint'],409)['error']['code']=='STALE_PREVIEW'

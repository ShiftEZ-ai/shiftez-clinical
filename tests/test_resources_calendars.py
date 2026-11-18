"""Independent calendar/capacity arithmetic plus migrated API/lifecycle evidence."""
import copy
from datetime import date
import uuid
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from packages.contracts import schemas as s
from packages.domain.resources import closed_intervals
from packages.domain.time import aware
from packages.domain.validation import evaluate
from apps.api.db import transaction
from apps.api import models as m


def pool(h, **changes):
    return h.configure('resource_pools', {'name': 'Synthetic interchangeable equipment', 'starts_on': '2020-01-01',
                                         'capacity': 1, **changes})['record']


def calendar(h, week=None, overrides=None, **changes):
    return h.configure('operating_calendars', {'name': 'Synthetic opening only', 'starts_on': '2020-01-01',
        'definition': {'timezone': 'Asia/Riyadh', 'week': week if week is not None else [
            {'weekday': 6, 'windows': [{'start_time': '08:00', 'end_time': '12:00'}]}],
            'overrides': overrides or []}, **changes})['record']


def update(h, kind, row, **changes):
    keys = s.CATALOGUE_SCHEMAS[kind].model_fields
    value = {k: copy.deepcopy(v) for k, v in row.items() if k in keys}
    value.update(changes)
    return h.configure(kind, value, row['id'], row['revision'])['record']


def attach(h, cal, location=0):
    row = next(r for r in h.get('admin','/api/catalogues')['locations'] if r['id']==h.seed['locations'][location])
    return update(h, 'locations', row, calendar_id=cal['id'])


def typ(h, resource, **definition):
    return h.configure('assignment_types', {'name':'Resource-consuming synthetic duty', 'starts_on':'2020-01-01',
        'definition': {'resources':[{'resource_pool_id':resource['id'],'units':1}], **definition}})['record']


def codes(result):
    return {f['code'] for f in result['validation']['findings']}


@pytest.mark.parametrize('windows',[
    [{'start_time':'22:00','end_time':'02:00'}], [{'start_time':'24:00','end_time':'24:00'}],
    [{'start_time':'08:00','end_time':'08:00'}],
    [{'start_time':'08:00','end_time':'12:00'},{'start_time':'11:59','end_time':'14:00'}]])
def test_invalid_opening_windows_do_not_save(windows):
    with pytest.raises(ValidationError):s.OpeningDay(weekday=0,windows=windows)


@pytest.mark.parametrize('changes',[
    {'timezone':'Not/AZone'}, {'timezone':'/absolute/path'},
    {'week':[{'weekday':0},{'weekday':0}]}, {'week':[{'weekday':7}]},
    {'overrides':[{'on':'2026-10-04','label':'one'},{'on':'2026-10-04','label':'two'}]},
    {'javascript':'return true'}, {'schema_version':2}])
def test_unsupported_calendar_definitions_fail_closed(changes):
    with pytest.raises(ValidationError):s.OperatingCalendarDefinition.model_validate({'timezone':'Asia/Riyadh',**changes})


@pytest.mark.parametrize('capacity',[0,-1,1001,True,1.5,'2'])
def test_capacity_is_bounded_integral_not_a_boolean(capacity):
    with pytest.raises(ValidationError):s.ResourcePoolInput(name='Pool',starts_on='2020-01-01',capacity=capacity)


def test_date_replacement_exact_gaps_and_adjacent_windows(h):
    cal=calendar(h,overrides=[{'on':'2026-10-04','label':'Split opening','windows':[
        {'start_time':'08:00','end_time':'09:00'},{'start_time':'10:00','end_time':'11:00'},
        {'start_time':'11:00','end_time':'12:00'}]}])
    attach(h,cal)
    result=h.shift(h.workspace())
    gaps=[f for f in result['validation']['findings'] if f['code']=='CALENDAR_CLOSED']
    assert [(f['start'],f['end']) for f in gaps]==[('2026-10-04T06:00:00+00:00','2026-10-04T07:00:00+00:00')]
    assert not result['validation']['valid']


def test_closed_holiday_then_replacement_opening_changes_validation(h):
    cal=calendar(h,overrides=[{'on':'2026-10-04','label':'Test closed holiday'}]);attach(h,cal)
    result=h.shift(h.workspace());assert 'CALENDAR_CLOSED' in codes(result)
    definition=copy.deepcopy(cal['definition']);definition['overrides'][0]['windows']=[{'start_time':'08:00','end_time':'12:00'}]
    update(h,'operating_calendars',cal,definition=definition)
    result=h.lifecycle(result['workspace'],'validate');assert result['validation']['valid']


def test_unattached_closed_calendar_has_no_inert_global_authority(h):
    calendar(h,week=[])
    assert h.shift(h.workspace())['validation']['valid']


def test_location_conflict_identifies_published_work(h):
    location = next(r for r in h.get('admin', '/api/catalogues')['locations'] if r['id'] == h.seed['locations'][0])
    update(h, 'locations', location, capacity=1)
    published = h.shift(h.workspace('Published FM clinic'))['workspace']
    h.publish(published)
    draft = h.shift(h.workspace('Alternative FM clinic'), people=[])
    issues = [f for f in draft['validation']['findings'] if f['code'] == 'RESOURCE_CAPACITY']
    assert issues and issues[0]['detail']['published_sessions'] == 1
    assert issues[0]['detail']['scheduled_sessions'] == 2


@pytest.mark.parametrize('capacity', [1, 2, 3])
@pytest.mark.parametrize('adjacent', [False, True])
def test_location_capacity_boundary_keeps_other_setup_checks(h, capacity, adjacent):
    workspace = h.shift(h.workspace())['workspace']
    workspace = (h.shift(workspace, '2026-10-04T10:00:00+03:00', '2026-10-04T14:00:00+03:00', people=[])
                 if adjacent else h.shift(workspace, people=[]))['workspace']
    if adjacent:
        workspace = h.shift(workspace, '2026-10-04T12:00:00+03:00', '2026-10-04T16:00:00+03:00', people=[])['workspace']
    data = h.snapshot(workspace)
    location = data['locations'][h.seed['locations'][0]]
    location.update(capacity=capacity, active=False)
    findings = {finding['code'] for finding in evaluate(data)['findings']}
    assert ('RESOURCE_CAPACITY' in findings) is (capacity < 2)
    assert 'LOCATION_INACTIVE' in findings


def test_clinic_calendar_blocks_duties_and_is_preserved_in_publication(h):
    cal = calendar(h, week=[])
    service = next(r for r in h.get('admin', '/api/catalogues')['services'] if r['id'] == h.seed['services'][0])
    update(h, 'services', service, mode='virtual', calendar_id=cal['id'])
    result = h.shift(h.workspace())
    assert 'CALENDAR_CLOSED' in codes(result)
    h.lifecycle(result['workspace'], 'submit', status=422)
    update(h, 'operating_calendars', cal, definition={'timezone': 'Asia/Riyadh',
        'week': [{'weekday': 6, 'windows': [{'start_time': '08:00', 'end_time': '12:00'}]}]})
    published = h.publish(result['workspace'])
    with transaction(h.app.state.engine) as db:
        snapshot = db.get(m.Publication, published['publication_id']).snapshot
        assert snapshot['services'][service['id']]['mode'] == 'virtual'
        assert cal['id'] in snapshot['operating_calendars']


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_used_place_and_clinic_stay_frozen_in_draft_and_published_recheck(request, backend):
    h = request.getfixturevalue(backend)
    original_calendar = calendar(h)
    location_id, service_id = h.seed['locations'][0], h.seed['services'][0]
    service = next(r for r in h.get('admin', '/api/catalogues')['services'] if r['id'] == service_id)
    service = update(h, 'services', service, calendar_id=original_calendar['id'])
    draft = h.shift(h.workspace('Frozen used catalogue rows'))['workspace']
    checked = h.snapshot(draft)
    frozen_capacity = checked['locations'][location_id]['capacity']
    closed_calendar = calendar(h, week=[])
    location = next(r for r in h.get('admin', '/api/catalogues')['locations'] if r['id'] == location_id)
    location = update(h, 'locations', location, capacity=frozen_capacity + 1)
    service = update(h, 'services', service, calendar_id=closed_calendar['id'])
    after = h.snapshot(draft)
    assert after['locations'][location_id] == checked['locations'][location_id]
    assert after['services'][service_id] == checked['services'][service_id]
    assert h.lifecycle(draft, 'validate')['validation']['valid']
    published = h.publish(draft)
    path = '/api/publications/' + published['publication_id']
    evidence = h.get('coordinator', path)['snapshot']
    for key in ('locations', 'services', 'resource_pools', 'operating_calendars', 'date_sets'):
        assert evidence[key] == after[key]
    update(h, 'locations', location, capacity=frozen_capacity + 2)
    update(h, 'services', service, calendar_id=None)
    rechecked = h.snapshot(published['workspace'])
    assert rechecked['locations'][location_id] == checked['locations'][location_id]
    assert rechecked['services'][service_id] == checked['services'][service_id]
    assert h.get('coordinator', path + '/coverage')['evaluation']['valid']
    assert h.get('coordinator', path)['snapshot'] == evidence


@pytest.mark.parametrize('change',[{'active':False},{'starts_on':'2026-10-05'},{'ends_on':'2026-10-04'}])
def test_calendar_effective_lifecycle_is_consumed(h,change):
    cal=calendar(h,**change);attach(h,cal)
    assert 'CALENDAR_CLOSED' in codes(h.shift(h.workspace()))


def test_calendar_effective_override_outside_range_rejected(h):
    h.configure('operating_calendars',{'name':'Out of range','starts_on':'2026-10-05',
        'definition':{'timezone':'Asia/Riyadh','overrides':[{'on':'2026-10-04','label':'Not effective'}]}},status=422)


def test_calendar_parent_intersection_and_overnight_midnight_exclusive(h):
    cal=calendar(h,week=[{'weekday':6,'windows':[{'start_time':'22:00','end_time':'24:00'}]},
                         {'weekday':0,'windows':[{'start_time':'00:00','end_time':'02:00'}]}])
    parent=attach(h,cal)
    row=next(r for r in h.get('admin','/api/catalogues')['locations'] if r['id']==h.seed['locations'][1])
    update(h,'locations',row,parent_id=parent['id'])
    result=h.shift(h.workspace(),start='2026-10-04T22:00:00+03:00',end='2026-10-05T02:00:00+03:00',location=1)
    assert result['validation']['valid']
    definition=copy.deepcopy(cal['definition']);definition['week'][1]['windows']=[]
    update(h,'operating_calendars',cal,definition=definition)
    changed=h.lifecycle(result['workspace'],'validate')
    assert 'CALENDAR_CLOSED' in codes(changed)
    gaps=[f for f in changed['validation']['findings'] if f['code']=='CALENDAR_CLOSED']
    assert gaps[0]['start']=='2026-10-04T21:00:00+00:00'


@pytest.mark.parametrize('on,begin,end',[('2026-03-08','02:30','04:00'),('2026-11-01','01:30','04:00')])
def test_dst_ambiguous_calendar_never_guesses_an_opening(on,begin,end):
    cal={'starts_on':'2020-01-01','active':True,'definition':{'timezone':'America/New_York',
        'week':[],'overrides':[{'on':on,'label':'Unresolved boundary','windows':[{'start_time':begin,'end_time':end}]}]}}
    with pytest.raises(ValueError):closed_intervals(cal,aware(on+'T05:00:00Z'),aware(on+'T10:00:00Z'))


def test_demand_is_once_per_common_duty_not_per_service_or_person(h):
    resource=pool(h,capacity=2)
    definition={'allow_shared_services':True,'shared_duty_reason':'Synthetic genuinely common duty',
                'resources':[{'resource_pool_id':resource['id'],'units':2}]}
    t=typ(h,resource,**definition);w=h.workspace()
    result=h.command('coordinator','shift.create',{'workspace_id':w['id'],'assignment_type_id':t['id'],
        'location_id':h.seed['locations'][0],'service_ids':h.seed['services'],
        'start':'2026-10-04T08:00:00+03:00','end':'2026-10-04T12:00:00+03:00',
        'participants':[{'person_id':pid,'start':'2026-10-04T08:00:00+03:00','end':'2026-10-04T12:00:00+03:00'} for pid in h.seed['people'][:2]]},revision=w['revision'])
    assert result['validation']['valid']
    pub=h.publish(result['workspace'])
    report=h.get('coordinator','/api/reports/'+pub['publication_id'])
    row=report['resources']['rows'][0]
    assert row['unit_hours']==8 and row['sessions']==1 and row['peak_required_units']==2
    assert 'resources' not in h.get('staff','/api/reports/'+pub['publication_id'])
    personal=h.get('staff','/api/publications/'+pub['publication_id'])['snapshot']
    assert 'resource_pools' not in personal and 'operating_calendars' not in personal


def test_resource_capacity_counts_other_current_publications_not_alternative_drafts(h, monkeypatch):
    from packages.domain import resources
    resource=pool(h);t=typ(h,resource)
    first=h.shift(h.workspace('First'),typ=t['id'])
    unrelated = ('2026-10-04T13:00:00+03:00', '2026-10-04T14:00:00+03:00')
    first = h.shift(first['workspace'], start=unrelated[0], end=unrelated[1])
    second=h.shift(h.workspace('Alternative'),people=[h.seed['people'][1]],typ=t['id'],location=1)
    assert second['validation']['valid']
    h.publish(first['workspace'])
    unrelated_times = {aware(value) for value in unrelated}

    def resource_time(value):
        instant = aware(value)
        assert instant not in unrelated_times, 'A surrounding duty without resource demand needs no resource-time conversion'
        return instant

    monkeypatch.setattr(resources, 'aware', resource_time)
    second=h.lifecycle(second['workspace'],'validate')
    assert 'POOLED_RESOURCE_CAPACITY' in codes(second)
    h.lifecycle(second['workspace'],'submit',status=422)


def test_resource_recheck_after_approval_and_frozen_history(h):
    resource=pool(h,capacity=2);t=typ(h,resource)
    result=h.shift(h.workspace(),typ=t['id'])
    pub=h.publish(result['workspace']);snapshot=h.get('coordinator','/api/publications/'+pub['publication_id'])['snapshot']
    update(h,'resource_pools',resource,name='Renamed but not rewritten',capacity=3)
    unchanged=h.get('coordinator','/api/publications/'+pub['publication_id'])['snapshot']
    assert unchanged==snapshot and unchanged['resource_pools'][resource['id']]['capacity']==2
    amended=h.workspace('Amended',base=pub['publication_id'])
    amended=h.lifecycle(amended,'submit')['workspace'];amended=h.lifecycle(amended,'approve','approver')['workspace']
    fresh=next(r for r in h.get('admin','/api/catalogues')['resource_pools'] if r['id']==resource['id'])
    update(h,'resource_pools',fresh,active=False)
    h.lifecycle(amended,'publish',status=409)
    current=h.get('coordinator','/api/publications/'+pub['publication_id']+'/coverage')
    assert 'RESOURCE_INACTIVE' in {f['code'] for f in current['evaluation']['findings']}


def test_segmented_units_overlap_and_exact_end_boundaries(h):
    resource=pool(h,capacity=2)
    t=typ(h,resource,resources=[{'resource_pool_id':resource['id'],'units':2,'from_minute':0,'to_minute':120},
                               {'resource_pool_id':resource['id'],'units':1,'from_minute':120}])
    result=h.shift(h.workspace(),typ=t['id'])
    result=h.shift(result['workspace'],start='2026-10-04T10:00:00+03:00',end='2026-10-04T12:00:00+03:00',
        people=[h.seed['people'][1]],typ=typ(h,resource)['id'],location=1)
    assert result['validation']['valid']
    update(h,'resource_pools',resource,capacity=1)
    result=h.lifecycle(result['workspace'],'validate')
    gaps=[f for f in result['validation']['findings'] if f['code']=='POOLED_RESOURCE_CAPACITY']
    assert {f['start'] for f in gaps}=={'2026-10-04T05:00:00+00:00','2026-10-04T07:00:00+00:00'}


def test_resource_location_and_calendar_and_missing_definition(h):
    cal=calendar(h,week=[]);resource=pool(h,location_id=h.seed['locations'][1],calendar_id=cal['id'])
    result=h.shift(h.workspace(),typ=typ(h,resource)['id'])
    assert {'RESOURCE_LOCATION','CALENDAR_CLOSED'}<=codes(result)
    data=h.snapshot(result['workspace']);data['resource_pools']={}
    assert 'RESOURCE_UNKNOWN' in {f['code'] for f in evaluate(data)['findings']}
    data=h.snapshot(result['workspace']);data['operating_calendars']={}
    assert 'CALENDAR_UNKNOWN' in {f['code'] for f in evaluate(data)['findings']}


def test_resource_intervals_must_not_extend_outside_shift(h):
    resource=pool(h);t=typ(h,resource,resources=[{'resource_pool_id':resource['id'],'units':1,'from_minute':300,'to_minute':360}])
    assert 'RESOURCE_INTERVAL_OUTSIDE_SHIFT' in codes(h.shift(h.workspace(),typ=t['id']))


@pytest.mark.parametrize('bad',[
    [{'units':1,'from_minute':120,'to_minute':60}],
    [{'units':1,'from_minute':720}],
    [{'units':1,'to_minute':721}],
    [{'units':True}],
    [{'units':1},{'units':1,'from_minute':120}]])
def test_invalid_resource_requirements_rejected(bad):
    pid=str(uuid.uuid4())
    with pytest.raises(ValidationError):s.TypeDefinition(resources=[{'resource_pool_id':pid,**d} for d in bad])


@pytest.mark.parametrize('role',['staff','coordinator','approver','credentialer','auditor','operator'])
def test_resource_configuration_requires_admin(h,role):
    h.configure('resource_pools',{'name':'Not authorised','starts_on':'2020-01-01','capacity':1},role=role,status=403)


def test_resource_cross_department_references_are_not_authority(h):
    with transaction(h.app.state.engine,write=True) as db:
        department=m.Department(organisation_id=h.seed['organisation_id'],name='Separate scope');db.add(department);db.flush()
        foreign=m.ResourcePool(department_id=department.id,name='Hidden',starts_on=date(2020,1,1),capacity=1)
        db.add(foreign);db.flush();pid=foreign.id
    h.configure('assignment_types',{'name':'Wrong scope','starts_on':'2020-01-01',
        'definition':{'resources':[{'resource_pool_id':pid,'units':1}]}},status=404)
    assert pid not in str(h.get('admin','/api/catalogues'))


def test_additive_location_fk_and_resource_database_checks_are_real(h):
    with pytest.raises(IntegrityError):
        with transaction(h.app.state.engine,write=True) as db:
            db.execute(text('UPDATE locations SET calendar_id=:id WHERE id=:location'),{'id':str(uuid.uuid4()),'location':h.seed['locations'][0]})
    resource=pool(h)
    with pytest.raises(IntegrityError):
        with transaction(h.app.state.engine,write=True) as db:
            db.execute(text('UPDATE resource_pools SET capacity=0 WHERE id=:id'),{'id':resource['id']})


def test_cancelled_session_releases_pool_demand_but_keeps_evidence(h):
    resource=pool(h);t=typ(h,resource)
    result=h.shift(h.workspace(),typ=t['id'])
    result=h.shift(result['workspace'],people=[h.seed['people'][1]],typ=t['id'],location=1)
    assert 'POOLED_RESOURCE_CAPACITY' in codes(result)
    row=h.detail(result['workspace'])['shifts'][0]
    result=h.command('coordinator','shift.action',{'workspace_id':result['workspace']['id'],'shift_ids':[row['id']],
        'action':'cancel','reason':'Release capacity explicitly'},revision=result['workspace']['revision'])
    assert result['validation']['valid'] and len(h.detail(result['workspace'])['shifts'])==2

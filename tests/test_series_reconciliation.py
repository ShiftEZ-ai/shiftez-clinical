"""Real migrated database/HTTP tests for logical occurrence versioning and atomic slices."""
import copy
import uuid
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from .test_catalogue_preview import state
from .test_resources_calendars import update

REASON = 'Reviewed synthetic following-occurrence update'


def template(h, days=(6, 0, 2), acknowledge_overlaps=False, **changes):
    value = {'name':'Reviewed series '+uuid.uuid4().hex[:6],
        'starts_on':'2026-10-01','ends_on':'2026-11-01',
        'assignment_type_id':h.seed['type'],'location_id':h.seed['locations'][0],
        'definition':{'anchor':'2026-10-04','days':[{'weekday':d,'start_time':'08:00','end_time':'12:00'} for d in days],
                      'service_ids':[h.seed['services'][0]]}, **changes}
    if acknowledge_overlaps:
        preview = h.client('admin').post('/api/series/preview', json={'value': value})
        assert preview.status_code == 200, preview.text
        return h.command('admin', 'catalogue.create', {'catalogue': 'recurring_series', 'value': value,
            'reason': 'Explicitly review conflicting synthetic template setup',
            'overlap_acknowledgement': preview.json()['conflict_token']})['record']
    return h.configure('recurring_series', value)['record']


def materialise(h, series, w=None):
    w = w or h.workspace()
    return h.command('coordinator','series.expand',{'workspace_id':w['id'],'series_id':series['id']}, revision=w['revision'])['workspace']


def test_batch_expansion_previews_once_skips_existing_and_undoes_together(h):
    first, second = template(h, days=(0,)), template(h, days=(1, 2))
    w = materialise(h, first)
    before = h.detail(w)['shifts']
    body = {'kind':'series.expand', 'expected_revision':w['revision'],
            'payload':{'workspace_id':w['id'], 'series_ids':[first['id'], second['id'], second['id']]}}
    response = h.client('coordinator').post('/api/planner/preview', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['counts']['created_count'] == 2
    assert h.detail(w)['shifts'] == before
    result = h.command('coordinator', body['kind'], body['payload'], revision=w['revision'])
    assert result['created_count'] == 2 and len(h.detail(w)['shifts']) == 3
    assert result['workspace']['revision'] == w['revision'] + 1
    h.command('coordinator','workspace.undo',{'reason':REASON},w['id'],result['workspace']['revision'])
    assert h.detail(w)['shifts'] == before


def test_batch_expansion_rolls_back_all_patterns_when_one_is_inactive(h):
    first, second = template(h, days=(0,)), template(h, days=(1,))
    second = update(h, 'recurring_series', second, active=False)
    w = h.workspace()
    before = h.detail(w)
    result = h.command('coordinator','series.expand',
        {'workspace_id':w['id'], 'series_ids':[first['id'],second['id']]}, revision=w['revision'], status=422)
    assert result['error']['code'] == 'SERIES_INACTIVE'
    assert h.detail(w) == before


def amend_template(h, series, start='09:00', end='13:00', acknowledge_overlaps=False, **overrides):
    definition=copy.deepcopy(series['definition'])
    for day in definition['days']:
        day.update(start_time=start,end_time=end)
    definition.update(overrides)
    if acknowledge_overlaps:
        from packages.contracts.schemas import SeriesInput
        value = {key: series[key] for key in SeriesInput.model_fields}
        value['definition'] = definition
        preview = h.client('admin').post('/api/series/preview', json={'value': value,
            'series_id': series['id'], 'expected_revision': series['revision']})
        assert preview.status_code == 200 and preview.json()['conflicts'], preview.text
        return h.command('admin', 'catalogue.update', {'catalogue': 'recurring_series', 'value': value,
            'reason': REASON, 'overlap_acknowledgement': preview.json()['conflict_token']},
            series['id'], series['revision'])['record']
    return update(h,'recurring_series',series,definition=definition)


def payload(w, series, **values):
    return {'workspace_id':w['id'],'series_id':series['id'],'expected_series_revision':series['revision'],
            'from_on':'2026-10-05','replace_modified':False,'participant_policy':'preserve_offsets','reason':REASON,**values}


def reconcile(h,w,series,status=200,**values):
    return h.command('coordinator','series.reconcile',payload(w,series,**values),revision=w['revision'],status=status)


def edit(h,w,row,**values):
    body={k:row[k] for k in ['assignment_type_id','location_id','service_ids','start','end','attributes']}
    body.update(participants=[{k:v for k,v in p.items() if k in ['person_id','start','end','participation','overtime']} for p in row['participants']])
    body.update(values)
    return h.command('coordinator','shift.update',{'workspace_id':w['id'],'shift_id':row['id'],**body},revision=w['revision'])['workspace']


def test_new_occurrences_have_unique_known_version_and_expansion_rejects_changed_template(h):
    series=template(h);w=materialise(h,series)
    before=h.detail(w)['shifts']
    assert len(before)==3 and {r['series_revision'] for r in before}=={1}
    assert len({r['occurrence_on'] for r in before})==3 and not any(r['series_modified'] for r in before)
    series=amend_template(h,series)
    failure=h.command('coordinator','series.expand',{'workspace_id':w['id'],'series_id':series['id']},revision=w['revision'],status=409)
    assert failure['error']['code']=='SERIES_CHANGED' and h.detail(w)['shifts']==before


def test_following_slice_preserves_earlier_content_and_stable_session_ids(h):
    series=template(h);w=materialise(h,series);before=h.detail(w)['shifts']
    series=amend_template(h,series)
    result=reconcile(h,w,series);after=h.detail(w)['shifts']
    assert result['updated_count']==2 and result['preserved_count']==1
    assert {r['id'] for r in after}=={r['id'] for r in before}
    assert after[0]==before[0] and after[1]['start'].startswith('2026-10-05T06:00')
    assert [r['series_revision'] for r in after]==[1,2,2]
    assert result['workspace']['status']=='draft' and not result['validation']['valid']


def test_new_weekdays_exclusions_and_effective_end_are_one_atomic_reconciliation(h):
    series=template(h);w=materialise(h,series);original=h.detail(w)['shifts']
    series=amend_template(h,series,days=[{'weekday':1,'start_time':'09:00','end_time':'13:00'},
                                       {'weekday':2,'start_time':'09:00','end_time':'13:00'}],excluded_dates=['2026-10-07'])
    result=reconcile(h,w,series)
    assert (result['created_count'],result['updated_count'],result['removed_count'],result['preserved_count'])==(1,0,2,1)
    after=h.detail(w)['shifts'];assert [r['occurrence_on'] for r in after]==['2026-10-04','2026-10-06']
    assert after[0]==original[0]


def test_retired_template_removes_only_reviewed_following_slice(h):
    series=template(h);w=materialise(h,series);series=update(h,'recurring_series',series,active=False)
    result=reconcile(h,w,series)
    assert result['removed_count']==2 and len(h.detail(w)['shifts'])==1


@pytest.mark.parametrize('action',['lock','cancel','edit','people'])
def test_manually_modified_or_locked_occurrences_never_silently_overwritten(h,action):
    series=template(h);w=materialise(h,series);row=h.detail(w)['shifts'][1]
    if action in {'lock','cancel'}:
        w=h.command('coordinator','shift.action',{'workspace_id':w['id'],'shift_ids':[row['id']],'action':action,'reason':REASON},revision=w['revision'])['workspace']
    else:
        values={'attributes':{}} if action=='edit' else {'participants':[{'person_id':h.seed['people'][0],'start':row['start'],'end':row['end']}]}
        w=edit(h,w,row,**values)
    series=amend_template(h,series);before=h.detail(w);counts=state(h)
    result=reconcile(h,w,series,status=409)
    assert result['error']['code']==('ASSIGNMENT_LOCKED' if action=='lock' else 'SERIES_MODIFIED')
    assert h.detail(w)==before and state(h)==counts
    if action=='lock':
        assert reconcile(h,w,series,status=409,replace_modified=True)['error']['code']=='ASSIGNMENT_LOCKED'
    else:
        result=reconcile(h,w,series,replace_modified=True)
        rows=h.detail(w)['shifts']
        assert not any(r['cancelled'] for r in rows)
        assert any(r['series_modified'] for r in rows) == (action == 'people')


def test_explicit_preservation_moves_complete_participation_with_duty(h):
    series=template(h,days=(0,));w=materialise(h,series);row=h.detail(w)['shifts'][0]
    w=edit(h,w,row,participants=[{'person_id':h.seed['people'][0],'start':'2026-10-05T08:00:00+03:00',
        'end':'2026-10-05T12:00:00+03:00','overtime':True}])
    series=amend_template(h,series)
    reconcile(h,w,series,replace_modified=True)
    p=h.detail(w)['shifts'][0]['participants'][0]
    assert p['start'].startswith('2026-10-05T06:00') and p['end'].startswith('2026-10-05T10:00') and p['overtime']


def test_shortening_refuses_to_truncate_participants_and_clear_is_explicit(h):
    series=template(h,days=(0,));w=materialise(h,series);row=h.detail(w)['shifts'][0]
    w=edit(h,w,row,participants=[{'person_id':h.seed['people'][0],'start':row['start'],'end':row['end']}])
    series=amend_template(h,series,end='11:00');before=h.detail(w)
    assert reconcile(h,w,series,status=409,replace_modified=True)['error']['code']=='SERIES_PARTICIPANT_OUTSIDE'
    assert h.detail(w)==before
    result=reconcile(h,w,series,replace_modified=True,participant_policy='clear')
    assert not h.detail(w)['shifts'][0]['participants'] and not result['validation']['valid']


def test_cancelling_earlier_instance_does_not_require_replacing_it(h):
    series=template(h);w=materialise(h,series);row=h.detail(w)['shifts'][0]
    w=h.command('coordinator','shift.action',{'workspace_id':w['id'],'shift_ids':[row['id']],'action':'cancel','reason':REASON},revision=w['revision'])['workspace']
    before=h.detail(w)['shifts'][0];series=amend_template(h,series)
    reconcile(h,w,series);assert h.detail(w)['shifts'][0]==before


@pytest.mark.parametrize('values,code,status',[
    ({'expected_series_revision':100},'STALE_REVISION',409),
    ({'from_on':'2026-10-11'},'OUTSIDE_WORKSPACE',422),
    ({'from_on':'2026-10-03'},'OUTSIDE_WORKSPACE',422),
    ({'series_id':str(uuid.uuid4())},'NOT_FOUND',404),
])
def test_reconciliation_checks_template_scope_version_and_date(h,values,code,status):
    series=template(h);w=materialise(h,series);before=h.detail(w)
    assert reconcile(h,w,series,status=status,**values)['error']['code']==code
    assert h.detail(w)==before


@pytest.mark.parametrize('role',['staff','approver','credentialer','auditor','operator'])
def test_reconciliation_is_only_a_coordinator_draft_authority(h,role):
    series=template(h);w=materialise(h,series)
    h.command(role,'series.reconcile',payload(w,series),revision=w['revision'],status=403)


def test_reconciliation_preview_rolls_back_and_stale_inputs_prevent_apply(h):
    series=template(h);w=materialise(h,series);series=amend_template(h,series)
    body={'kind':'series.reconcile','expected_revision':w['revision'],'payload':payload(w,series)}
    before=h.detail(w);counts=state(h)
    response=h.client('coordinator').post('/api/planner/preview',json=body);assert response.status_code==200,response.text
    preview=response.json();assert preview['counts']['updated_count']==2 and not preview['committed']
    assert before==h.detail(w) and counts==state(h)
    series=amend_template(h,series,start='10:00',end='14:00')
    response=h.client('coordinator').post('/api/commands',json={**body,'expected_fingerprint':preview['source_fingerprint']},headers={'idempotency-key':str(uuid.uuid4())})
    assert response.status_code==409 and response.json()['error']['code']=='STALE_PREVIEW'
    after=h.detail(w)
    assert before['shifts']==after['shifts'] and before['workspace']==after['workspace']
    assert after['current_input_revision']==before['current_input_revision']+1


def test_undo_restores_exact_old_lineage_and_participation(h):
    series=template(h);w=materialise(h,series);before=h.detail(w)['shifts'];series=amend_template(h,series)
    result=reconcile(h,w,series)
    h.command('coordinator','workspace.undo',{'reason':REASON},w['id'],result['workspace']['revision'])
    assert h.detail(w)['shifts']==before


def test_ongoing_fixed_pattern_is_bounded_by_draft_and_can_be_reconciled(h):
    series = template(h, ends_on=None)
    w = materialise(h, series)
    rows = h.detail(w)['shifts']
    assert len(rows) == 3
    assert all(w['starts_on'] <= r['occurrence_on'] < w['ends_on'] for r in rows)
    series = amend_template(h, series)
    result = reconcile(h, w, series)
    assert len(h.detail(result['workspace'])['shifts']) == 3


def test_ordinary_copies_detach_but_amendments_preserve_lineage(h):
    series=template(h,days=(0,));w=materialise(h,series);row=h.detail(w)['shifts'][0]
    w=edit(h,w,row,participants=[{'person_id':h.seed['people'][0],'start':row['start'],'end':row['end']}])
    result=h.command('coordinator','shift.copy',{'workspace_id':w['id'],'shift_ids':[row['id']],'offset_days':1,'reason':REASON},revision=w['revision'])
    shifts=h.detail(w)['shifts'];assert shifts[1]['series_id'] is None and shifts[1]['occurrence_on'] is None and not shifts[1]['series_modified']
    pub=h.publish(result['workspace'])
    amended=h.command('coordinator','workspace.create',{'name':'Amend preserved provenance','starts_on':w['starts_on'],'ends_on':w['ends_on'],'base_publication_id':pub['publication_id']})['workspace']
    copied=h.detail(amended)['shifts'];assert copied[0]['occurrence_on']==shifts[0]['occurrence_on'] and copied[0]['series_modified']
    assert copied[0]['id']!=shifts[0]['id'] and copied[1]['series_id'] is None
    # New columns are also protected by the inherited published-content guards.
    with pytest.raises(DBAPIError),h.app.state.engine.begin() as db:
        db.execute(text('UPDATE shifts SET series_modified=false WHERE id=:id'),{'id':shifts[0]['id']})


def test_legacy_unknown_lineage_is_not_fabricated(h):
    series=template(h);w=materialise(h,series)
    with h.app.state.engine.begin() as db:
        db.execute(text('UPDATE shifts SET series_revision=NULL, occurrence_on=NULL WHERE workspace_id=:id'),{'id':w['id']})
    before=h.detail(w)
    assert reconcile(h,w,series,status=409)['error']['code']=='SERIES_LINEAGE_UNKNOWN'
    assert h.command('coordinator','series.expand',{'workspace_id':w['id'],'series_id':series['id']},revision=w['revision'],status=409)['error']['code']=='SERIES_LINEAGE_UNKNOWN'
    assert before==h.detail(w)


@pytest.mark.parametrize('assignment',["series_revision=0", "series_revision=NULL", "occurrence_on=NULL", "series_id=NULL"])
def test_database_lineage_guards_reject_incoherent_metadata(h,assignment):
    series=template(h);w=materialise(h,series)
    with pytest.raises(DBAPIError),h.app.state.engine.begin() as db:
        db.execute(text('UPDATE shifts SET '+assignment+' WHERE workspace_id=:id'),{'id':w['id']})


def test_duplicate_known_occurrence_is_rejected_by_database(h):
    series=template(h);w=materialise(h,series)
    with pytest.raises(DBAPIError),h.app.state.engine.begin() as db:
        db.execute(text("UPDATE shifts SET occurrence_on='2026-10-04' WHERE workspace_id=:id"),{'id':w['id']})


def test_invalid_participant_policy_and_unreviewed_unknown_fields_rejected(h):
    series=template(h);w=materialise(h,series)
    reconcile(h,w,series,status=422,participant_policy='truncate')
    reconcile(h,w,series,status=422,ignore_locks=True)

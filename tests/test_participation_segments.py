"""Independent expected intervals, persisted workflows and versioned report evidence."""
from tests.historical_staffing import retained_staffing
from packages.domain.time import now as clock_now
import copy
import csv
import io
from datetime import timedelta
import uuid
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from packages.contracts.schemas import ParticipantInput
from packages.domain.participation import presence_intervals
from packages.domain.reports import workload, assignments_csv, calendar
from packages.domain.time import aware, union_hours
from packages.domain.validation import evaluate
from apps.api.services.swaps import semantic_content
from apps.api.db import transaction
from apps.api import models as m
from scripts.backup import backup, restore
from .conftest import Harness
from .governance_support import identity, swap_automation, transition
from .test_series_reconciliation import template, materialise, amend_template, reconcile, edit


def at(clock, day='2026-10-04'):
    return f'{day}T{clock}:00+03:00'


def person(pid, day='2026-10-04', **extra):
    return {'person_id':pid,'start':at('08:00',day),'end':at('12:00',day),
            'participation':'staff','overtime':False,'segments':[
                {'start':at('08:00',day),'end':at('10:00',day)},
                {'start':at('10:30',day),'end':at('12:00',day)}],**extra}


def with_relief(h, day='2026-10-04', main=0, overtime=False):
    return [person(h.seed['people'][main],day,overtime=overtime),
            {'person_id':h.seed['people'][2],'start':at('08:00',day),'end':at('12:00',day)}]


def codes(result):
    return {f['code'] for f in result['findings'] if f['severity']=='error'}


@pytest.mark.parametrize('change', ['overlap','touch','unsorted','wrong_first','wrong_last','naive','null','extra','too_many'])
def test_invalid_segment_definitions_rejected(change):
    data=person(str(uuid.uuid4()))
    if change=='overlap':data['segments'][1]['start']=at('09:59')
    if change=='touch':data['segments'][1]['start']=at('10:00')
    if change=='unsorted':data['segments'].reverse()
    if change=='wrong_first':data['segments'][0]['start']=at('08:01')
    if change=='wrong_last':data['segments'][-1]['end']=at('11:59')
    if change=='naive':data['segments'][0]['start']='2026-10-04T08:00:00'
    if change=='null':data['segments']=None
    if change=='extra':data['segments'][0]['paid']=True
    if change=='too_many':data['segments']*=5
    with pytest.raises(ValidationError):ParticipantInput.model_validate(data)


def test_legacy_and_explicit_segments_have_independent_expected_durations():
    data=person(str(uuid.uuid4()))
    assert union_hours(presence_intervals(data))==3.5
    del data['segments']
    assert union_hours(presence_intervals(data))==4
    data['segments']=[]
    assert union_hours(presence_intervals(data))==4


def test_explicit_offsets_preserve_elapsed_hours_across_dst():
    data={'person_id':str(uuid.uuid4()),'start':'2026-10-25T00:00:00+01:00','end':'2026-10-25T03:00:00+00:00',
          'segments':[{'start':'2026-10-25T00:00:00+01:00','end':'2026-10-25T01:30:00+01:00'},
                      {'start':'2026-10-25T01:30:00+00:00','end':'2026-10-25T03:00:00+00:00'}]}
    assert union_hours(presence_intervals(data))==3


def test_gap_is_not_clinical_cover_and_whole_duty_relief_restores_cover(h):
    w=h.workspace()
    result=h.shift(w,people=[person(h.seed['people'][0])]);w=result['workspace']
    gaps=[f for f in result['validation']['findings'] if f['code']=='COVERAGE_GAP']
    assert len(gaps)==1 and aware(gaps[0]['start'])==aware(at('10:00')) and aware(gaps[0]['end'])==aware(at('10:30'))
    h.lifecycle(w,'submit',status=422)
    row=h.detail(w)['shifts'][0]
    w=edit(h,w,row,participants=with_relief(h))
    assert h.detail(w)['workspace']['validation']['valid']
    pub=h.publish(w)
    report=h.get('coordinator','/api/reports/'+pub['publication_id'])
    rows={r['person_id']:r for r in report['rows']}
    assert report['metric_version']=='scheduled-v3'
    assert rows[h.seed['people'][0]]['scheduled_hours']==3.5
    assert rows[h.seed['people'][0]]['reserved_hours']==4
    assert rows[h.seed['people'][0]]['off_cover_hours']==.5
    assert rows[h.seed['people'][0]]['assignments']==1
    assert rows[h.seed['people'][2]]['scheduled_hours']==4
    assert rows[h.seed['people'][2]]['off_cover_hours']==0


def test_gaps_do_not_allow_another_duty_or_hide_absence(h):
    w=h.shift(h.workspace(),people=with_relief(h))['workspace']
    result=h.shift(w,start=at('10:00'),end=at('10:30'),location=1)
    assert 'TIME_OVERLAP' in codes(result['validation'])
    h.command('coordinator','availability.record',{'person_id':h.seed['people'][0],
        'kind':'actual_absence','start':at('10:00'),'end':at('10:30'),'private_reason':'Private test absence'})
    assert 'UNAVAILABLE' in codes(evaluate(h.snapshot(result['workspace'])))


def test_existing_hours_limit_keeps_reserved_envelope_basis(h):
    h.policy({'kind':'max_hours','hours':3.75,'window':'day'})
    result=h.shift(h.workspace(),people=with_relief(h))
    assert 'MAX_HOURS' in codes(result['validation'])


def test_corrupt_segments_fail_closed_in_shared_validator(h):
    w=h.shift(h.workspace(),people=with_relief(h))['workspace']
    data=h.snapshot(w);data['shifts'][0]['participants'][0]['segments']={'unsupported':'not a list'}
    assert 'PARTICIPATION_INVALID' in codes(evaluate(data))


def test_presence_exports_overtime_and_personal_projection_agree(h):
    pub=h.publish(h.shift(h.workspace(),people=with_relief(h,overtime=True))['workspace'])
    path='/api/reports/'+pub['publication_id']
    report=h.get('staff',path);row=report['rows'][0]
    assert row['scheduled_hours']==row['planned_overtime_hours']==row['workload_credit']==3.5
    assert row['planned_overtime_assignments']==1 and row['reserved_hours']==4
    snapshot=h.get('staff','/api/publications/'+pub['publication_id'])['snapshot']
    assert len(snapshot['people'])==1 and len(snapshot['shifts'][0]['participants'])==1
    assert len(snapshot['shifts'][0]['participants'][0]['segments'])==2
    parsed=list(csv.DictReader(io.StringIO(assignments_csv(snapshot,pub['publication_id'],1,h.seed['people'][0]))))
    assert len(parsed)==2 and {r['presence_segment'] for r in parsed}=={'1','2'}
    assert len({r['participation_id'] for r in parsed})==1
    assert all(r['reserved_start']==at('08:00').replace('08:00:00+03:00','05:00:00+00:00') for r in parsed)
    ics=calendar(snapshot,pub['publication_id'],1,h.seed['people'][0]).replace('\r\n ', '')
    assert ics.count('BEGIN:VEVENT')==2 and len({line for line in ics.splitlines() if line.startswith('UID:')})==2
    assert 'DTEND:20261004T070000Z' in ics and 'DTSTART:20261004T073000Z' in ics


def test_v1_contiguous_snapshot_keeps_old_rows_columns_and_uid(h):
    pub=h.publish(h.shift(h.workspace())['workspace'])
    snapshot=h.get('coordinator','/api/publications/'+pub['publication_id'])['snapshot']
    snapshot['metric_version']='scheduled-v1'
    for sh in snapshot['shifts']:
        for p in sh['participants']:p.pop('segments')
    result=workload(snapshot)
    assert result['metric_version']=='scheduled-v1' and result['rows'][0]['scheduled_hours']==4
    assert 'reserved_hours' not in result['rows'][0]
    assert 'presence_segment' not in assignments_csv(snapshot,pub['publication_id'],1).splitlines()[0]
    assert '-presence-' not in calendar(snapshot,pub['publication_id'],1,h.seed['people'][0])
    snapshot['metric_version']='future-unsupported'
    with pytest.raises(ValueError,match='Unsupported'):workload(snapshot)


def test_copy_undo_amendment_and_issued_guard_preserve_segments(h):
    w=h.shift(h.workspace(),people=with_relief(h))['workspace'];original=h.detail(w)['shifts'][0]
    w=h.command('coordinator','shift.copy',{'workspace_id':w['id'],'shift_ids':[original['id']],
        'offset_days':1,'reason':'Copy with explicit gaps'},revision=w['revision'])['workspace']
    copyrow=h.detail(w)['shifts'][1]
    first=next(p for p in original['participants'] if p['person_id']==h.seed['people'][0])
    second=next(p for p in copyrow['participants'] if p['person_id']==h.seed['people'][0])
    assert aware(second['segments'][0]['end'])-aware(first['segments'][0]['end'])==timedelta(days=1)
    w=h.command('coordinator','workspace.undo',{'reason':'Undo copied duty'},w['id'],w['revision'])['workspace']
    assert h.detail(w)['shifts'][0]['participants']==original['participants']
    pub=h.publish(w)
    amendment=h.workspace(base=pub['publication_id'])
    assert [p['segments'] for p in h.detail(amendment)['shifts'][0]['participants']]==[p['segments'] for p in original['participants']]
    with pytest.raises(DBAPIError),h.app.state.engine.begin() as db:
        db.execute(text("UPDATE assignments SET segments='[]' WHERE id=:id"),{'id':first['id']})


def test_following_reconciliation_preserves_segment_elapsed_offsets(h):
    series=template(h,days=(0,));w=materialise(h,series);row=h.detail(w)['shifts'][0]
    w=edit(h,w,row,participants=with_relief(h,day='2026-10-05'))
    series=amend_template(h,series,start='09:00',end='13:00')
    w=reconcile(h,w,series,replace_modified=True)['workspace']
    p=next(p for p in h.detail(w)['shifts'][0]['participants'] if p['person_id']==h.seed['people'][0])
    assert aware(p['segments'][0]['end'])==aware(at('11:00','2026-10-05'))
    assert aware(p['segments'][1]['start'])==aware(at('11:30','2026-10-05'))


def test_consent_hash_includes_segments_but_keeps_legacy_empty_hash(h):
    w=h.shift(h.workspace())['workspace'];rows=h.detail(w)['shifts'];old=copy.deepcopy(rows)
    old[0]['participants'][0].pop('segments')
    assert semantic_content(old)==semantic_content(rows)
    rows[0]['participants'][0]['segments']=person(h.seed['people'][0])['segments']
    assert semantic_content(old)!=semantic_content(rows)


def test_complete_segmented_staff_swap_retains_relief_and_exact_pair(h):
    swap_automation(h, prepare=False, publish=False)
    identity(h,'staff_two',['staff'],h.seed['people'][1])
    # A consent expires within a day and before both duties begin: the duties lie ahead of today, whatever today is.
    day=lambda n:(clock_now().date()+timedelta(days=n)).isoformat()  # noqa: E731
    w=h.shift(h.workspace(start=day(3),end=day(10)),start=at('08:00',day(3)),end=at('12:00',day(3)),people=with_relief(h,day=day(3)))['workspace']
    w=h.shift(w,start=at('08:00',day(4)),end=at('12:00',day(4)),
              people=with_relief(h,day=day(4),main=1))['workspace']
    pub=h.publish(w);snapshot=h.get('coordinator','/api/publications/'+pub['publication_id'])['snapshot']
    first=next(p for p in snapshot['shifts'][0]['participants'] if p['person_id']==h.seed['people'][0])
    second=next(p for p in snapshot['shifts'][1]['participants'] if p['person_id']==h.seed['people'][1])
    swap=h.command('staff','swap.offer',{'publication_id':pub['publication_id'],'assignment_id':first['id'],
        'recipient_person_id':h.seed['people'][1],'expires_at':(m.now()+timedelta(hours=1)).isoformat(),'reason':'Whole segmented duty exchange'})['swap']
    assert len(swap['offered_assignment']['segments'])==2
    swap=transition(h,swap,'counteroffer','staff_two',{'assignment_id':second['id'],'reason':'Whole duty counterpart'})['swap']
    swap=transition(h,swap,'confirm')['swap'];result=transition(h,swap,'prepare','coordinator')
    new=h.publish(result['workspace']);after=h.get('coordinator','/api/publications/'+new['publication_id'])['snapshot']
    p=next(p for p in after['shifts'][0]['participants'] if p['person_id']==h.seed['people'][1])
    assert p['segments']==first['segments']
    assert len(after['shifts'][0]['participants'])==2
    assert h.get('coordinator','/api/publications/'+pub['publication_id'])['snapshot']==snapshot


def test_logical_restore_preserves_segmented_evidence_and_reports(h,tmp_path):
    pub=h.publish(h.shift(h.workspace(),people=with_relief(h))['workspace'])
    before=h.get('coordinator','/api/reports/'+pub['publication_id'])
    archive=tmp_path/'segments.backup';backup(h.settings.database_url,archive)
    url='sqlite:///'+str(tmp_path/'restored.db');restore(archive,url,True)
    restored=Harness(url,h.seed)
    try:
        with transaction(restored.app.state.engine) as db:
            sessions=db.query(m.Session).all()
            assert sessions and all(s.revoked for s in sessions)
            old_ids={s.id for s in sessions}
        after=restored.get('coordinator','/api/reports/'+pub['publication_id'])
        assert after['rows']==before['rows']
        with transaction(restored.app.state.engine) as db:
            assert all(s.revoked for s in db.query(m.Session).all() if s.id in old_ids)
            assert sum(not s.revoked for s in db.query(m.Session).all())==1
    finally:
        for client in restored.clients.values():client.close()
        restored.app.state.engine.dispose()


def test_segment_only_edit_changes_preview_but_cannot_edit_approved_content(h):
    w=h.shift(h.workspace(),people=with_relief(h))['workspace'];before=h.detail(w);sh=copy.deepcopy(before['shifts'][0])
    payload={key:sh[key] for key in ('assignment_type_id','location_id','service_ids','start','end','attributes')}
    payload.update({'workspace_id':w['id'],'shift_id':sh['id'],'participants':[
        {key:p[key] for key in ('person_id','start','end','participation','overtime','segments')} for p in sh['participants']]})
    main=next(p for p in payload['participants'] if p['person_id']==h.seed['people'][0])
    main['segments'][1]['start']=at('10:45')
    command={'kind':'shift.update','expected_revision':w['revision'],'payload':payload}
    preview=h.client('coordinator').post('/api/planner/preview',json=command)
    assert preview.status_code==200,preview.text
    value=preview.json();assert value['impact']['counts']=={'added':0,'removed':0,'changed':1}
    assert value['impact']['changes'][0]['fields']==['participants']
    assert 'COVERAGE_GAP' not in codes(value['validation']) and h.detail(w)==before
    # Whole-duty relief still covers the changed gap; the preview must not mutate the draft.
    submitted=h.lifecycle(w,'submit')['workspace'];approved=h.lifecycle(submitted,'approve','approver')['workspace']
    # A reviewed proposal cannot be edited in place; a separate draft is required.
    blocked=h.client('coordinator').post('/api/commands',headers={'idempotency-key':str(uuid.uuid4())},
        json={**command,'expected_revision':approved['revision']})
    assert blocked.status_code==409


def test_supervisor_off_cover_gap_never_supervises_a_present_trainee(h):
    typ=next(row for row in h.get('admin','/api/catalogues')['assignment_types'] if row['id']==h.seed['type'])
    value={key:typ[key] for key in ('name','description','starts_on','ends_on','active','definition')}
    value['definition']['supervision']={'capability_id':h.seed['supervisor_capability'],'trainee_category_ids':[h.seed['trainee_category']], 'trainees_per_supervisor':1}
    h.configure('assignment_types',value,target=typ['id'],revision=typ['revision'])
    w = retained_staffing(h, h.workspace(), people=[person(h.seed['people'][0], participation='supervisor'),
        {'person_id':h.seed['people'][1],'start':at('08:00'),'end':at('12:00'),'participation':'trainee'}])['workspace']
    assert 'SUPERVISION_GAP' in codes(w['validation'])
    finding=next(f for f in w['validation']['findings'] if f['code']=='SUPERVISION_GAP')
    assert aware(finding['start'])==aware(at('10:00')) and aware(finding['end'])==aware(at('10:30'))

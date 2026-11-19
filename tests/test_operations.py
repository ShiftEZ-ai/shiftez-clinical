"""Constructed operational scenarios: no historical rota is treated as an oracle."""
from .clock_support import freeze_clock
from datetime import date
import csv
import io
import uuid
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one
from packages.contracts.schemas import SeriesDefinition
from packages.domain.recurrence import expand
from packages.domain.time import aware

REASON='Independent operational acceptance fixture'
def codes(result): return {f['code'] for f in result['validation']['findings']}

def csv_source(rows, extra_columns=()):
    out=io.StringIO();w=csv.writer(out);w.writerow(['assignment_type','location','service','person','start','end','overtime',*extra_columns]);w.writerows(rows);return out.getvalue()

def batch(h,w,source):
    return h.command('coordinator','import.preview',{'workspace_id':w['id'],'source_name':'Synthetic September regression.csv','csv_text':source},revision=w['revision'])['batch']

def mappings(h,people):
    return [{'row_number':i+2,'assignment_type_id':h.seed['type'],'location_id':h.seed['locations'][0],'service_id':h.seed['services'][0],'person_id':pid} for i,pid in enumerate(people)]


def test_saved_rota_import_cannot_be_applied_twice(h):
    from apps.api.serialization import record

    workspace = h.workspace()
    source = csv_source([['type', 'location', 'service', 'person', '2026-10-05T08:00:00+03:00',
                          '2026-10-05T12:00:00+03:00', 'false']])
    imported = batch(h, workspace, source)
    payload = {'batch_id': imported['id'], 'workspace_id': workspace['id'],
               'mappings': mappings(h, [h.seed['people'][0]]), 'reason': REASON}
    saved = h.command('coordinator', 'import.apply', payload, revision=workspace['revision'])['workspace']
    before = h.detail(saved)
    audit = h.get('auditor', '/api/audit')['events']
    with transaction(h.app.state.engine) as db:
        stored = record(db.get(m.ImportBatch, imported['id']))
    refused = h.command('coordinator', 'import.apply', payload, revision=saved['revision'], status=409)
    assert refused['error']['code'] == 'IMPORT_ALREADY_APPLIED'
    assert h.detail(saved) == before
    assert h.get('auditor', '/api/audit')['events'] == audit
    with transaction(h.app.state.engine) as db:
        assert record(db.get(m.ImportBatch, imported['id'])) == stored


def test_september_overtime_individual_assignments_not_marked_shift_count(h):
    w=h.workspace('Synthetic September distinction',start='2026-09-20',end='2026-09-27')
    source=csv_source([['Unknown source labels','Review location','Department service','Person A','2026-09-20T08:00:00+03:00','2026-09-20T12:00:00+03:00','true'],['same','same','same','Person B','2026-09-20T08:00:00+03:00','2026-09-20T12:00:00+03:00','true'],['same','same','same','Person A','2026-09-21T08:00:00+03:00','2026-09-21T12:00:00+03:00','true']])
    b=batch(h,w,source)
    assert not h.detail(w)['shifts']
    result=h.command('coordinator','import.apply',{'batch_id':b['id'],'workspace_id':w['id'],'mappings':mappings(h,[h.seed['people'][0],h.seed['people'][1],h.seed['people'][0]]),'reason':REASON},revision=w['revision'])
    assert result['source_rows']==3 and len(h.detail(w)['shifts'])==2
    published=h.publish(result['workspace'])
    report=h.get('coordinator','/api/reports/'+published['publication_id'])
    assert report['overtime_marked_shifts']==2
    assert sum(r['planned_overtime_assignments'] for r in report['rows'])==3
    assert sum(r['planned_overtime_hours'] for r in report['rows'])==12
    assert report['attendance_basis'].startswith('none')
    # An exact retry of source content is rejected; another command key cannot duplicate a batch.
    h.command('coordinator','import.preview',{'workspace_id':w['id'],'source_name':'same.csv','csv_text':source},revision=result['workspace']['revision'],status=409)


@pytest.mark.parametrize('lead', ['TRUE', 'false', None, 'maybe', ''])
def test_import_team_lead_is_explicit_and_independent_of_overtime(h, lead):
    w=h.workspace()
    row=['type','location','service','Person A','2026-10-05T08:00:00+03:00','2026-10-05T12:00:00+03:00','false']
    other=[*row[:3],'Person B',*row[4:6],'true']
    source=csv_source([row+[lead],other+['false']], ['team_lead']) if lead is not None else csv_source([row,other])
    b=batch(h,w,source)
    payload={'batch_id':b['id'],'workspace_id':w['id'],'mappings':mappings(h,h.seed['people'][:2]),'reason':REASON}
    if lead in {'maybe',''}:
        assert b['rows'][0]['errors']==["team_lead must be true or false on each person's row"]
        h.command('coordinator','import.apply',payload,revision=w['revision'],status=422)
        assert not h.detail(w)['shifts']
        return
    assert not any(r['errors'] for r in b['rows'])
    result=h.command('coordinator','import.apply',payload,revision=w['revision'])
    expected={h.seed['people'][0]:(lead=='TRUE',False),h.seed['people'][1]:(False,True)}
    participants=h.detail(result['workspace'])['shifts'][0]['participants']
    assert {p['person_id']:(p['team_lead'],p['overtime']) for p in participants}==expected
    published=h.publish(result['workspace'])
    snapshot=h.get('coordinator','/api/publications/'+published['publication_id'])['snapshot']
    assert {p['person_id']:(p['team_lead'],p['overtime']) for p in snapshot['shifts'][0]['participants']}==expected


def test_import_shared_clinics_requires_complete_mapping_and_counts_one_duty(h):
    w=h.workspace()
    source=csv_source([['type','location','Physical | Virtual','Person A','2026-10-05T08:00:00+03:00','2026-10-05T12:00:00+03:00','false']])
    b=batch(h,w,source)
    assert not b['rows'][0]['errors']
    mapped=mappings(h,[h.seed['people'][0]])
    payload={'batch_id':b['id'],'workspace_id':w['id'],'mappings':mapped,'reason':REASON}
    rejected=h.command('coordinator','import.apply',payload,revision=w['revision'],status=422)
    assert rejected['error']['code']=='INCOMPLETE_SERVICE_MAPPING'
    mapped[0]['additional_service_ids']=[h.seed['services'][1]]
    rejected=h.command('coordinator','import.apply',payload,revision=w['revision'],status=422)
    assert rejected['error']['code']=='UNSUPPORTED_SHARED_DUTY'
    assert not h.detail(w)['shifts']
    typ=h.configure('assignment_types',{'name':'Reviewed shared coverage','starts_on':'2020-01-01',
        'definition':{'allow_shared_services':True,'shared_duty_reason':'One team covers both clinics'}})['record']
    mapped[0]['assignment_type_id']=typ['id']
    result=h.command('coordinator','import.apply',payload,revision=w['revision'])
    shifts=h.detail(result['workspace'])['shifts']
    assert len(shifts)==1 and len(shifts[0]['participants'])==1
    assert set(shifts[0]['service_ids'])==set(h.seed['services'][:2])
    published=h.publish(result['workspace'])
    report=h.get('coordinator','/api/reports/'+published['publication_id'])
    assert sum(row['scheduled_hours'] for row in report['rows'])==4


def test_stale_import_explicit_refresh_preserves_source_and_revalidates(h, monkeypatch):
    w=h.workspace();source=csv_source([['new','new','new','ambiguous','2026-10-05T08:00:00+03:00','2026-10-05T12:00:00+03:00','false']]);b=batch(h,w,source)
    w=h.shift(w)['workspace']
    before=h.detail(w)['shifts']
    payload={'batch_id':b['id'],'workspace_id':w['id'],'mappings':mappings(h,[h.seed['people'][0]]),'reason':REASON}
    assert h.command('coordinator','import.apply',payload,revision=w['revision'],status=409)['error']['code']=='STALE_IMPORT'
    refreshed=h.command('coordinator','import.refresh',{'batch_id':b['id'],'workspace_id':w['id'],'reason':REASON},revision=w['revision'])['batch']
    assert refreshed['id']==b['id'] and refreshed['source_hash']==b['source_hash'] and refreshed['rows']==b['rows']
    applied_at=m.now()
    freeze_clock(monkeypatch, lambda:applied_at)
    saved=h.command('coordinator','import.apply',payload,revision=w['revision'])
    assert saved['validation']['valid'] and len(h.detail(w)['shifts'])==2
    for shift in saved['shifts']:
        for participant in shift['participants']:
            assert participant['source']=='import' and participant['source_run_id'] is None
            assert participant['source_actor_id']==h.seed['identities']['coordinator']
            assert aware(participant['source_at'])==m.now()
    h.command('coordinator','import.refresh',{'batch_id':b['id'],'workspace_id':w['id'],'reason':REASON},revision=saved['workspace']['revision'],status=409)
    undone=h.command('coordinator','workspace.undo',{'reason':REASON},w['id'],saved['workspace']['revision'])['workspace']
    assert h.detail(undone)['shifts']==before


@pytest.mark.parametrize('start,end,ot',[('not a date','2026-10-05T12:00:00+03:00','false'),('2026-10-05T08:00:00','2026-10-05T12:00:00','false'),('2026-10-05T13:00:00+03:00','2026-10-05T12:00:00+03:00','false'),('2026-10-05T08:00:00+03:00','2026-10-05T12:00:00+03:00','maybe')])
def test_import_row_errors_never_become_assignments(h,start,end,ot):
    w=h.workspace();b=batch(h,w,csv_source([['a','b','c','unknown',start,end,ot]]))
    assert b['rows'][0]['errors']
    h.command('coordinator','import.apply',{'workspace_id':w['id'],'batch_id':b['id'],'mappings':mappings(h,[h.seed['people'][0]]),'reason':REASON},revision=w['revision'],status=422)
    assert not h.detail(w)['shifts']


def test_unknown_import_identity_is_not_inferred(h):
    w=h.workspace();b=batch(h,w,csv_source([['a','b','c','Alex Morgan','2026-10-05T08:00:00+03:00','2026-10-05T12:00:00+03:00','false']]))
    h.command('coordinator','import.apply',{'workspace_id':w['id'],'batch_id':b['id'],'mappings':mappings(h,[str(uuid.uuid4())]),'reason':REASON},revision=w['revision'],status=404)
    assert not h.detail(w)['shifts']


def test_alternate_week_wall_times_overnight_exceptions_and_horizon():
    d=SeriesDefinition(anchor='2026-10-05',interval_weeks=2,days=[{'weekday':0,'start_time':'22:00','end_time':'02:00'},{'weekday':2,'start_time':'08:00','end_time':'24:00'}],excluded_dates=['2026-10-07'])
    result=expand(d,date(2026,10,5),date(2026,10,26))
    assert [r['start'].isoformat() for r in result]==['2026-10-05T19:00:00+00:00','2026-10-19T19:00:00+00:00','2026-10-21T05:00:00+00:00']
    assert [(r['end']-r['start']).total_seconds()/3600 for r in result]==[4,4,16]
    with pytest.raises(ValueError):expand(d,date(2026,1,1),date(2028,1,1))


def test_series_expansion_does_not_duplicate_existing_instances(h):
    series=h.configure('recurring_series',{'name':'Random recurring '+uuid.uuid4().hex[:8],'starts_on':'2026-10-01','ends_on':'2026-11-01','assignment_type_id':h.seed['type'],'location_id':h.seed['locations'][0],'definition':{'anchor':'2026-10-04','days':[{'weekday':6,'start_time':'22:00','end_time':'02:00'}],'service_ids':[h.seed['services'][0]]}})['record']
    w=h.workspace();made=h.command('coordinator','series.expand',{'workspace_id':w['id'],'series_id':series['id']},revision=w['revision'])
    assert made['created_count']==1
    h.command('coordinator','series.expand',{'workspace_id':w['id'],'series_id':series['id']},revision=made['workspace']['revision'],status=409)
    assert len(h.detail(w)['shifts'])==1


def test_bulk_cancel_lock_and_undo_keep_one_draft_history(h):
    created=h.shift(h.workspace());w=created['workspace'];sid=created['shift_id']
    locked=h.command('coordinator','shift.action',{'workspace_id':w['id'],'shift_ids':[sid],'action':'lock','reason':REASON},revision=w['revision'])
    h.command('coordinator','shift.action',{'workspace_id':w['id'],'shift_ids':[sid],'action':'cancel','reason':REASON},revision=locked['workspace']['revision'],status=409)
    undone=h.command('coordinator','workspace.undo',{'reason':REASON},w['id'],locked['workspace']['revision'])
    assert not h.detail(w)['shifts'][0]['locked']
    cancelled=h.command('coordinator','shift.action',{'workspace_id':w['id'],'shift_ids':[sid],'action':'cancel','reason':REASON},revision=undone['workspace']['revision'])
    assert cancelled['validation']['valid']
    h.command('coordinator','workspace.undo',{'reason':REASON},w['id'],cancelled['workspace']['revision'])
    assert not h.detail(w)['shifts'][0]['cancelled']


def test_failed_swap_rolls_back_both_sides_and_revision(h):
    w=h.shift(h.workspace())['workspace']
    w=h.shift(w,start='2026-10-05T08:00:00+03:00',end='2026-10-05T12:00:00+03:00',people=[h.seed['people'][1]])['workspace']
    h.command('coordinator','availability.record',{'person_id':h.seed['people'][0],'start':'2026-10-05T08:00:00+03:00','end':'2026-10-05T12:00:00+03:00','kind':'actual_absence','private_reason':'Synthetic absence'})
    before=h.detail(w)
    first,second=[s['participants'][0]['id'] for s in before['shifts']]
    h.command('coordinator','draft.swap',{'workspace_id':w['id'],'first_assignment_id':first,'second_assignment_id':second,'reason':REASON},revision=w['revision'],status=422)
    after=h.detail(w)
    assert after['shifts']==before['shifts'] and after['workspace']['revision']==before['workspace']['revision']


def test_explicit_policy_exception_does_not_waive_independent_time_conflict(h):
    p=h.policy({'kind':'max_hours','hours':1,'window':'day'},exception=True)
    w=h.shift(h.workspace())['workspace']
    request=h.command('coordinator','exception.create',{'policy_id':p['id'],'person_id':h.seed['people'][0],'starts_on':'2026-10-04','ends_on':'2026-10-05','reason':REASON})['record']
    h.command('approver','exception.approve',{'reason':REASON},request['id'],request['revision'])
    result=h.lifecycle(w,'validate');assert result['validation']['valid'] and 'RULE_EXCEPTION' in codes(result)
    conflict=h.shift(result['workspace'],service=1,location=1)
    assert not conflict['validation']['valid'] and 'TIME_OVERLAP' in codes(conflict)


def test_new_category_does_not_grant_clinical_or_application_privileges(h):
    new=h.configure('categories',{'name':'Senior administrator '+uuid.uuid4().hex[:8],'starts_on':'2026-10-01'})['record']
    person=h.configure('people',{'display_name':'New test clinician'})['record']
    h.command('coordinator','membership.create',{'person_id':person['id'],'category_id':new['id'],'starts_on':'2026-10-01'})
    result=h.shift(h.workspace(),people=[person['id']])
    assert not result['validation']['valid']
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.CapabilityGrant.id).where(m.CapabilityGrant.person_id==person['id']))
        assert not db.scalar(select(m.Identity.id).where(m.Identity.person_id==person['id']))


@pytest.mark.parametrize('kind',['validation','generation'])
def test_durable_worker_jobs_do_not_publish(h,kind):
    w=h.shift(h.workspace(),people=[])['workspace']
    job=h.command('coordinator','job.create',{'workspace_id':w['id'],'kind':kind},revision=w['revision'])['job']
    assert job['status']=='queued'
    process_one(h.app.state.engine)
    result=h.get('coordinator','/api/jobs/'+job['id'])['job']
    assert result['status']=='succeeded'
    assert h.get('coordinator','/api/publications')['publications']==[]
    if kind=='generation':
        assert result['result']['status']=='candidate_feasible'
        applied=h.command('coordinator','generation.apply',{'workspace_id':w['id'],'job_id':job['id'],'reason':REASON},revision=w['revision'])
        assert applied['validation']['valid'] and applied['workspace']['status']=='draft'


def test_undoing_an_applied_proposal_marks_it_no_longer_applied(h):
    """Undo of "Apply suggestions" puts the job back to succeeded, so the Auto-schedule panel stops saying "Assignments
    added to draft"; applying it again re-checks it first (maintenance); another edit's undo leaves an applied job alone."""
    w=h.shift(h.workspace(),people=[])['workspace']
    job=h.command('coordinator','job.create',{'workspace_id':w['id'],'kind':'generation'},revision=w['revision'])['job']
    process_one(h.app.state.engine)
    w=h.command('coordinator','generation.apply',{'workspace_id':w['id'],'job_id':job['id'],'reason':REASON},revision=w['revision'])['workspace']
    assert h.get('coordinator','/api/jobs/'+job['id'])['job']['status']=='applied'
    w=h.command('coordinator','workspace.undo',{'reason':REASON},w['id'],w['revision'])['workspace']
    assert h.get('coordinator','/api/jobs/'+job['id'])['job']['status']=='succeeded'
    # Applying it again checks it against the current rota first (maintenance reconciliation, A4): what still fits is applied.
    again=h.command('coordinator','generation.apply',{'workspace_id':w['id'],'job_id':job['id'],'reason':REASON},revision=w['revision'])
    assert again['reconciliation']['status']=='applied' and again['reconciliation']['validation_current']
    assert h.get('coordinator','/api/jobs/'+job['id'])['job']['status']=='applied'
    w=h.command('coordinator','workspace.undo',{'reason':REASON},again['workspace']['id'],again['workspace']['revision'])['workspace']
    assert not any(s['participants'] for s in h.detail(w)['shifts'])  # the re-check's Apply is one undoable edit too
    # Apply a fresh proposal, edit afterwards, undo only the edit: the proposal stays applied.
    job=h.command('coordinator','job.create',{'workspace_id':w['id'],'kind':'generation'},revision=w['revision'])['job']
    while process_one(h.app.state.engine):  # also the complete check the last Apply queued (maintenance)
        pass
    w=h.command('coordinator','generation.apply',{'workspace_id':w['id'],'job_id':job['id'],'reason':REASON},revision=w['revision'])['workspace']
    w=h.shift(w,start='2026-10-05T08:00:00+03:00',end='2026-10-05T12:00:00+03:00')['workspace']
    h.command('coordinator','workspace.undo',{'reason':REASON},w['id'],w['revision'])
    assert h.get('coordinator','/api/jobs/'+job['id'])['job']['status']=='applied'


def test_generation_scope_and_duplicate_job_admission(h):
    w=h.shift(h.workspace(),people=[])['workspace']
    other=h.shift(h.workspace('Other period'),people=[])['workspace']
    sid=h.snapshot(w)['focus_ids'][0]
    foreign=h.snapshot(other)['focus_ids'][0]
    h.command('coordinator','job.create',{'workspace_id':w['id'],'kind':'generation','session_ids':[foreign]},revision=w['revision'],status=422)
    job=h.command('coordinator','job.create',{'workspace_id':w['id'],'kind':'generation','session_ids':[sid]},revision=w['revision'])['job']
    h.command('coordinator','job.create',{'workspace_id':w['id'],'kind':'generation'},revision=w['revision'],status=409)
    assert [j['id'] for j in h.get('coordinator','/api/jobs?workspace_id='+w['id'])['jobs']]==[job['id']]
    assert h.get('coordinator','/api/jobs?workspace_id='+other['id'])['jobs']==[]
    process_one(h.app.state.engine)
    result=h.get('coordinator','/api/jobs/'+job['id'])['job']['result']
    assert result['status']=='candidate_feasible'
    assert {a['shift_id'] for a in result['additions']}=={sid}


def test_stale_generated_candidate_and_cancelled_job_are_not_applied(h):
    w=h.shift(h.workspace(),people=[])['workspace']
    job=h.command('coordinator','job.create',{'workspace_id':w['id'],'kind':'generation'},revision=w['revision'])['job']
    process_one(h.app.state.engine)
    w=h.shift(w,start='2026-10-05T08:00:00+03:00',end='2026-10-05T12:00:00+03:00')['workspace']
    # The rota changed under it (a session added): an unrelated edit never throws the proposal away. It is checked
    # against the current rota (maintenance, A4) and only its own placements are applied; the new session is left alone.
    added=next(s for s in h.detail(w)['shifts'] if s['start'].startswith('2026-10-05'))
    reconciled=h.command('coordinator','generation.apply',{'workspace_id':w['id'],'job_id':job['id'],'reason':REASON},revision=w['revision'])
    assert reconciled['reconciliation']['status']=='applied' and reconciled['reconciliation']['changed_facts']
    shifts={s['id']:s for s in h.detail(w)['shifts']}
    assert shifts[added['id']]['participants']==added['participants'] and any(s['participants'] for i,s in shifts.items() if i!=added['id'])
    w=reconciled['workspace']
    second=h.command('coordinator','job.create',{'workspace_id':w['id'],'kind':'validation'},revision=w['revision'])['job']
    h.command('operator','job.cancel',{'reason':REASON},second['id'],second['revision']);process_one(h.app.state.engine)
    cancelled=h.get('coordinator','/api/jobs/'+second['id'])['job']
    assert cancelled['status']=='cancelled' and cancelled['completed_at'] and not cancelled['started_at']


def test_job_cancel_only_if_queued_is_atomic_and_preserves_claimed_work(h):
    w = h.workspace()
    queued = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'},
                       revision=w['revision'])['job']
    cancelled = h.command('coordinator', 'job.cancel', {'reason': REASON, 'only_if_queued': True}, queued['id'])
    assert cancelled['cancelled'] is True and cancelled['job']['status'] == 'cancelled'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'job.cancel',
                                                       m.AuditEvent.target_id == queued['id']))

    running = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'},
                        revision=w['revision'])['job']
    from apps.worker.main import WORKER_ID, claim_job
    with transaction(h.app.state.engine, write=True) as db:
        claim = claim_job(db)
        assert claim and claim['id'] == running['id']
    refused = h.command('coordinator', 'job.cancel', {'reason': REASON, 'only_if_queued': True}, running['id'])
    assert refused['cancelled'] is False and refused['job']['status'] == 'running'
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, running['id'])
        assert row.status == 'running' and row.completed_at is None and row.worker_id == WORKER_ID
        assert not db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'job.cancel',
                                                           m.AuditEvent.target_id == running['id']))
    process_one(h.app.state.engine, recover_owned=True)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, running['id']).status == 'succeeded'


def test_worker_rechecks_revoked_requester_access(h):
    w=h.workspace();job=h.command('coordinator','job.create',{'workspace_id':w['id'],'kind':'validation'},revision=w['revision'])['job']
    with transaction(h.app.state.engine,write=True) as db:
        role=db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id==h.seed['identities']['coordinator']));role.revoked=True
    process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        result=db.get(m.Job,job['id']);assert result.status=='failed' and result.error_code=='ACCESS_REVOKED' and result.completed_at


def test_database_rejects_moving_published_rows_into_editable_draft(h):
    published=h.publish(h.shift(h.workspace())['workspace']);draft=h.shift(h.workspace('Alternative'))
    with transaction(h.app.state.engine) as db:
        pub=db.get(m.Publication,published['publication_id'])
        sid=db.scalar(select(m.Shift.id).where(m.Shift.workspace_id==pub.workspace_id))
        aid=db.scalar(select(m.Assignment.id).where(m.Assignment.shift_id==sid))
        wid=pub.workspace_id
    for statement,params in [('UPDATE shifts SET workspace_id=:destination WHERE id=:source',{'destination':draft['workspace']['id'],'source':sid}),('UPDATE assignments SET shift_id=:destination WHERE id=:source',{'destination':draft['shift_id'],'source':aid}),('UPDATE workspaces SET status=\'draft\' WHERE id=:source',{'source':wid})]:
        with pytest.raises(IntegrityError),transaction(h.app.state.engine,write=True) as db:db.execute(text(statement),params)


def test_generation_applies_only_the_chosen_suggestions(h):
    w=h.shift(h.workspace(),people=[])['workspace']
    w=h.shift(w,start='2026-10-05T08:00:00+03:00',end='2026-10-05T12:00:00+03:00',people=[])['workspace']
    job=h.command('coordinator','job.create',{'workspace_id':w['id'],'kind':'generation'},revision=w['revision'])['job']
    process_one(h.app.state.engine)
    additions=h.get('coordinator','/api/jobs/'+job['id'])['job']['result']['additions']
    assert len(additions)>=2
    h.command('coordinator','generation.apply',{'workspace_id':w['id'],'job_id':job['id'],'reason':REASON,'addition_indexes':[99]},revision=w['revision'],status=422)
    h.command('coordinator','generation.apply',{'workspace_id':w['id'],'job_id':job['id'],'reason':REASON,'addition_indexes':[]},revision=w['revision'],status=422)
    h.command('coordinator','generation.apply',{'workspace_id':w['id'],'job_id':job['id'],'reason':REASON,'addition_indexes':[0]},revision=w['revision'])
    assigned=[p for s in h.detail(w)['shifts'] for p in s['participants']]
    assert [(p['person_id'],p['start']) for p in assigned]==[(additions[0]['participant']['person_id'],additions[0]['participant']['start'])]


def test_long_rota_is_scheduled_a_month_at_a_time(h):
    w=h.workspace('Two months','2026-10-04','2026-12-04')
    w=h.shift(w,start='2026-10-05T08:00:00+03:00',end='2026-10-05T12:00:00+03:00',people=[])['workspace']
    w=h.shift(w,start='2026-11-20T08:00:00+03:00',end='2026-11-20T12:00:00+03:00',people=[])['workspace']
    shifts=sorted(h.detail(w)['shifts'],key=lambda s:s['start'])
    whole=h.command('coordinator','job.create',{'workspace_id':w['id'],'kind':'generation'},revision=w['revision'])['job']
    process_one(h.app.state.engine)
    assert h.get('coordinator','/api/jobs/'+whole['id'])['job']['result']['status'].startswith('unsupported')
    month=h.command('coordinator','job.create',{'workspace_id':w['id'],'kind':'generation','session_ids':[shifts[0]['id']]},revision=w['revision'])['job']
    process_one(h.app.state.engine)
    result=h.get('coordinator','/api/jobs/'+month['id'])['job']['result']
    assert result['status'] in {'candidate_feasible','candidate_with_gaps'} and {a['shift_id'] for a in result['additions']}=={shifts[0]['id']}

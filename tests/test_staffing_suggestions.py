"""No speculative persistence; independently asserted current staffing consequences."""
import copy
import time
import uuid
import pytest
from sqlalchemy import select
from packages.contracts import schemas as s
from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import staffing
from .test_catalogue_preview import state
from .test_participation_segments import at, person, with_relief

PATH='/api/planner/staffing-suggestions'


def source(h,people=None):
    result=h.shift(h.workspace(),people=[] if people is None else people)
    w=result['workspace'];sh=h.detail(w)['shifts'][0]
    return w,sh


def ask(h,w,sh,role='coordinator',status=200,**extra):
    response=h.client(role).post(PATH,json={'workspace_id':w['id'],'shift_id':sh['id'],'expected_revision':w['revision'],**extra})
    assert response.status_code==status,response.text
    return response.json()


def row_for(value,pid):
    return next(row for row in value['candidates'] if row['person_id']==pid)


def test_addition_options_use_same_validator_and_do_not_persist(h):
    w,sh=source(h);before=state(h);detail=h.detail(w)
    result=ask(h,w,sh)
    assert state(h)==before and h.detail(w)==detail
    assert result['total']==3 and result['evaluated_count']==3 and not result['has_more']
    assert [row['display_name'] for row in result['candidates']]==['Alex Morgan','Jordan Lee','Taylor Noor']
    assert all(row['whole_candidate_valid'] and row['mandatory_findings_remaining']==0 for row in result['candidates'])
    assert result['baseline_mandatory_findings']>0
    assert result['source_fingerprint']==h.snapshot(w)['fingerprint']
    assert 'private_reason' not in str(result) and 'type_snapshot' not in str(result)


def test_department_candidates_prepare_once_and_reuse_unchanged_checks(monkeypatch):
    from collections import Counter
    from types import SimpleNamespace
    from packages.contracts import schemas as s
    from packages.domain import validation
    from tests.test_schedule_score_scale import department
    data = department()
    data['fingerprint'] = 'synthetic-staffing-scale'
    data['shifts'][0]['locked'] = False
    original = copy.deepcopy(data)
    workspace = SimpleNamespace(**data['workspace'], status='draft', revision=1)
    actor = SimpleNamespace(department_id=workspace.department_id, permits=lambda *roles: True)
    monkeypatch.setattr(staffing, 'scoped', lambda *args: workspace)
    monkeypatch.setattr(staffing, 'domain_snapshot', lambda *args: data)
    counts, samples = Counter(), []
    real_evaluate = staffing.evaluate
    def checked(trial, **kwargs):
        result = real_evaluate(trial, **kwargs)
        samples.append((trial, result))
        return result
    monkeypatch.setattr(staffing, 'evaluate', checked)
    for name in ('active_rules', 'validate_session_setup'):
        real = getattr(validation, name)
        def counted(*args, _real=real, _name=name, **kwargs):
            counts[_name] += 1
            return _real(*args, **kwargs)
        monkeypatch.setattr(validation, name, counted)
    result = staffing.suggestions(None, actor, s.StaffingSuggestionsInput(
        workspace_id=workspace.id, expected_revision=1, shift_id=data['shifts'][0]['id'], limit=10))
    assert result['evaluated_count'] == 10
    assert data == original
    assert counts['active_rules'] == 1
    assert counts['validate_session_setup'] <= len(data['shifts']) + 10
    # The complete candidate result, including causes and hints, still equals a fresh full check.
    trial, actual = samples[-1]
    raw = {**original, 'shifts': trial['shifts']}
    assert actual == real_evaluate(raw)


def test_fill_options_department_scale_stays_within_three_seconds(monkeypatch):
    """The live replacement path evaluates a 150-person/450-session rota once per request."""
    from types import SimpleNamespace
    from tests.test_schedule_score_scale import department
    data = department()
    data['fingerprint'] = 'synthetic-fill-options-scale'
    data['shifts'][0]['locked'] = False
    workspace = SimpleNamespace(**data['workspace'], status='draft', revision=1)
    db = SimpleNamespace(scalar=lambda *args: None)  # Ordinary draft, without a publication change list.
    actor = SimpleNamespace(department_id=workspace.department_id, permits=lambda *roles: True)
    monkeypatch.setattr(staffing, 'scoped', lambda *args: workspace)
    monkeypatch.setattr(staffing, 'domain_snapshot', lambda *args: data)
    from apps.api.services import published_changes
    monkeypatch.setattr(published_changes, 'validation_scope', lambda *args, **kwargs: None)
    started = time.perf_counter()
    result = staffing.fill_options(db, actor, s.FillOptionsInput(
        workspace_id=workspace.id, expected_revision=workspace.revision, shift_id=data['shifts'][0]['id']))
    elapsed = time.perf_counter() - started
    print(f'fill-options 150 people / 450 sessions: {elapsed:.3f}s')
    assert len(data['people']) == 150 and len(data['shifts']) == 450
    assert result['candidates'] and len(result['candidates']) == 148
    evaluate = staffing.evaluate
    monkeypatch.setattr(staffing, 'evaluate', lambda data, **kwargs: evaluate(
        data, **{key: value for key, value in kwargs.items() if key != 'trial_cache'}))
    uncached = staffing.fill_options(db, actor, s.FillOptionsInput(
        workspace_id=workspace.id, expected_revision=workspace.revision, shift_id=data['shifts'][0]['id']))
    assert result['candidates'] == uncached['candidates']
    return locals()


@pytest.mark.wall_benchmark
def test_fill_options_department_scale_wall_budget(monkeypatch):
    state = test_fill_options_department_scale_stays_within_three_seconds(monkeypatch)
    assert state['elapsed'] < 3, state['elapsed']


@pytest.mark.parametrize('replace', [False, True])
def test_fill_options_reports_first_page_and_stops_before_remaining_candidates(monkeypatch, replace):
    """Async workers must expose a recoverable page while the remaining people are still being checked."""
    from types import SimpleNamespace
    from packages.domain.solver_control import Cancelled
    from tests.test_schedule_score_scale import department
    data = department()
    data['fingerprint'] = 'f' * 64
    data['shifts'][0]['locked'] = False
    workspace = SimpleNamespace(**data['workspace'], status='draft', revision=1)
    db = SimpleNamespace(scalar=lambda *args: None)
    actor = SimpleNamespace(department_id=workspace.department_id, permits=lambda *roles: True)
    monkeypatch.setattr(staffing, 'scoped', lambda *args: workspace)
    monkeypatch.setattr(staffing, 'domain_snapshot', lambda *args: data)
    from apps.api.services import published_changes
    monkeypatch.setattr(published_changes, 'validation_scope', lambda *args, **kwargs: None)
    participant = data['shifts'][0]['participants'][0] if replace else None
    if participant:
        from datetime import datetime, timedelta
        participant['start'] = (datetime.fromisoformat(participant['start']) + timedelta(hours=1)).isoformat()
        participant['fixed'] = True
        from apps.api.services import planning_groups, workspace_access
        monkeypatch.setattr(planning_groups, 'unrestricted', lambda *args: False)
        monkeypatch.setattr(workspace_access, 'access_record', lambda *args: {})
    pages = []
    with pytest.raises(Cancelled):
        staffing.fill_options(db, actor, s.FillOptionsInput(
            workspace_id=workspace.id, expected_revision=workspace.revision, shift_id=data['shifts'][0]['id'],
            replace_assignment_id=participant['id'] if participant else None),
            progress=pages.append, cancelled=lambda: bool(pages))
    assert pages and pages[0]['evaluated_count'] == 20 and pages[0]['total'] == 148
    assert len(pages[0]['candidates']) == 20
    envelope = s.FillOptionsResult.model_validate({key: value for key, value in pages[0].items()
                                                 if key not in {'evaluated_count', 'total'}})
    assert envelope.slot == {
        'start': participant['start'] if participant else data['shifts'][0]['start'],
        'end': data['shifts'][0]['end'], 'participation': 'staff',
        'overtime': participant['overtime'] if participant else False, 'segments': []}
    assert bool(envelope.replace_refusal) is replace
    assert all(isinstance(row['override_allowed'], bool) for row in envelope.candidates)


@pytest.mark.parametrize('role',['staff','approver','credentialer','auditor','operator'])
def test_only_authorised_coordinator_can_request_staffing_options(h,role):
    w,sh=source(h)
    assert ask(h,w,sh,role=role,status=403)['error']['code']=='FORBIDDEN'


@pytest.mark.parametrize('bad',[
    {'limit':0},{'limit':21},{'offset':-1},{'offset':10001},{'search':'x'*81},
    {'slot':{'start':at('12:00'),'end':at('08:00')}},{'automatic_publish':True},
    {'replace_assignment_id':str(uuid.uuid4()),'slot':{'start':at('08:00'),'end':at('12:00')}}])
def test_bounded_schema_rejects_invalid_or_unsupported_inputs(h,bad):
    w,sh=source(h);ask(h,w,sh,status=422,**bad)


def test_origin_csrf_and_session_are_not_waived_for_readonly_post(h):
    w,sh=source(h);client=h.client('coordinator')
    payload={'workspace_id':w['id'],'shift_id':sh['id'],'expected_revision':w['revision']}
    assert client.post(PATH,json=payload,headers={'origin':'https://other.invalid'}).status_code==403
    assert client.post(PATH,json=payload,headers={'x-csrf-token':'wrong'}).json()['error']['code']=='CSRF_FAILED'
    with transaction(h.app.state.engine,write=True) as db:
        identity=db.get(m.Identity,h.seed['identities']['coordinator'])
        identity.disabled=True
    assert client.post(PATH,json=payload).status_code==401


def test_current_known_unavailability_changes_options_and_fingerprint(h):
    w,sh=source(h);before=ask(h,w,sh)
    h.command('coordinator','availability.record',{'person_id':h.seed['people'][0], 'kind':'actual_absence',
        'start':at('08:00'),'end':at('12:00'),'private_reason':'SENSITIVE reason must never leak'})
    after=ask(h,w,sh);row=row_for(after,h.seed['people'][0])
    assert row['person_blockers']>0 and not row['whole_candidate_valid']
    assert any(f['code']=='UNAVAILABLE' for f in row['findings'])
    assert before['source_fingerprint']!=after['source_fingerprint']
    assert 'SENSITIVE' not in str(after)


def test_capability_change_is_not_label_based_eligibility(h):
    typ=next(row for row in h.get('admin','/api/catalogues')['assignment_types'] if row['id']==h.seed['type'])
    value={key:typ[key] for key in ('name','description','starts_on','ends_on','active','definition')}
    value['definition']['requirements'][0]['capabilities']=[{'capability_id':h.seed['capability'],'minimum':1}]
    h.configure('assignment_types',value,target=typ['id'],revision=typ['revision'])
    w,sh=source(h)
    with transaction(h.app.state.engine,write=True) as db:
        for grant in db.scalars(select(m.CapabilityGrant).where(m.CapabilityGrant.person_id==h.seed['people'][0])):
            grant.revoked=True
    result=ask(h,w,sh);candidate=row_for(result,h.seed['people'][0])
    assert not candidate['whole_candidate_valid'] and any(f['code']=='CAPABILITY_GAP' for f in candidate['findings'])
    assert row_for(result,h.seed['people'][1])['whole_candidate_valid']


def test_replacement_preserves_whole_segmented_slot_and_others(h):
    w,sh=source(h,people=with_relief(h))
    original=next(p for p in sh['participants'] if p['person_id']==h.seed['people'][0])
    result=ask(h,w,sh,replace_assignment_id=original['id'])
    assert result['total']==1 and result['candidates'][0]['person_id']==h.seed['people'][1]
    assert len(result['slot']['segments'])==2 and result['slot']['overtime']==original['overtime']
    assert result['candidates'][0]['whole_candidate_valid']
    assert h.detail(w)['shifts'][0]==sh


def test_existing_presence_gap_requires_a_whole_replacement_duty(h):
    w,sh=source(h,people=[person(h.seed['people'][0])])
    before=h.detail(w)
    result=ask(h,w,sh,status=422,slot={'start':at('10:00'),'end':at('10:30'),'participation':'staff'})
    assert result['error']['code']=='FULL_ASSIGNMENT_REQUIRED'
    complete=ask(h,w,sh)
    assert all(row['whole_candidate_valid'] for row in complete['candidates'])
    assert h.detail(w)==before


def test_search_pagination_and_empty_result_are_explicit(h):
    w,sh=source(h)
    one=ask(h,w,sh,limit=1);two=ask(h,w,sh,limit=1,offset=1)
    assert one['has_more'] and two['has_more'] and one['candidates'][0]['person_id']!=two['candidates'][0]['person_id']
    last=ask(h,w,sh,limit=1,offset=2);assert not last['has_more']
    found=ask(h,w,sh,search='MORGAN');assert found['total']==1
    missing=ask(h,w,sh,search="'; DROP TABLE people; --")
    assert missing['total']==0 and missing['candidates']==[] and not missing['has_more']
    assert one['request_fingerprint']!=two['request_fingerprint']
    ask(h,w,sh,search='no-match',slot={'start':at('07:00'),'end':at('08:00')},status=422)


def test_locked_cancelled_foreign_and_stale_targets_are_refused(h):
    w,sh=source(h);other,other_sh=source(h)
    assert ask(h,w,other_sh,status=404)['error']['code']=='NOT_FOUND'
    assert ask(h,w,sh,status=404,replace_assignment_id=str(uuid.uuid4()))['error']['code']=='NOT_FOUND'
    w2=h.command('coordinator','shift.action',{'workspace_id':w['id'],'shift_ids':[sh['id']],
        'action':'lock','reason':'Explicit lock for staffing test'},revision=w['revision'])['workspace']
    ask(h,w,sh,status=409)
    assert ask(h,w2,sh,status=409)['error']['code']=='ASSIGNMENT_LOCKED'


def test_official_commitments_count_but_alternative_drafts_do_not(h):
    official=h.shift(h.workspace(),people=[h.seed['people'][0]])['workspace']
    w,sh=source(h)
    before=ask(h,w,sh);assert row_for(before,h.seed['people'][0])['whole_candidate_valid']
    h.publish(official)
    after=ask(h,w,sh);row=row_for(after,h.seed['people'][0])
    assert any(f['code']=='TIME_OVERLAP' for f in row['findings']) and row['person_blockers']>0
    assert not row['whole_candidate_valid']


def test_scope_limits_fail_instead_of_certifying_truncated_candidates(h,monkeypatch):
    w,sh=source(h)
    monkeypatch.setattr(staffing,'MAX_SCOPE_SHIFTS',0)
    assert ask(h,w,sh,status=409)['error']['code']=='STAFFING_SCOPE_LIMIT'
    monkeypatch.setattr(staffing,'MAX_SCOPE_SHIFTS',1000)
    monkeypatch.setattr(staffing,'MAX_DIRECTORY_MATCHES',2)
    assert ask(h,w,sh,status=409)['error']['code']=='STAFFING_DIRECTORY_LIMIT'
    assert ask(h,w,sh,search='Alex')['total']==1


def test_selected_person_still_needs_exact_preview_and_ordinary_publication(h):
    w,sh=source(h);result=ask(h,w,sh);chosen=result['candidates'][0]
    data={key:sh[key] for key in ('assignment_type_id','location_id','service_ids','attributes','start','end')}
    data.update(workspace_id=w['id'],shift_id=sh['id'],participants=[{'person_id':chosen['person_id'],**result['slot']}])
    command={'kind':'shift.update','expected_revision':w['revision'],'payload':data}
    preview=h.client('coordinator').post('/api/planner/preview',json=command)
    assert preview.status_code==200 and preview.json()['validation']['valid']
    saved=h.client('coordinator').post('/api/commands',json={**command,'expected_fingerprint':preview.json()['source_fingerprint']},headers={'idempotency-key':str(uuid.uuid4())})
    assert saved.status_code==200 and saved.json()['workspace']['status']=='draft'
    h.publish(saved.json()['workspace'])
    assert len(h.get('coordinator','/api/publications')['publications'])==1


def test_partial_staffing_slot_rejected_even_when_no_candidate_matches(h):
    w,sh=source(h)
    for search in ('', 'Nobody matches this'):
        result=ask(h,w,sh,status=422,search=search,slot={'start':at('08:00'),'end':at('10:00')})
        assert result['error']['code']=='FULL_ASSIGNMENT_REQUIRED'


def test_replacement_keeps_configured_fixed_supervisor_and_lead_flags(h):
    start=at('08:00');end=at('12:00')
    w,sh=source(h,people=[{'person_id':h.seed['people'][0], 'start':start, 'end':end,
        'fixed':True, 'team_lead':True, 'supervisor_person_id':h.seed['people'][2]}])
    result=ask(h,w,sh,replace_assignment_id=sh['participants'][0]['id'])
    assert result['slot']['fixed'] and result['slot']['team_lead']
    assert result['slot']['supervisor_person_id']==h.seed['people'][2]
# Include this file in hosted supervision and team-lead qualification.

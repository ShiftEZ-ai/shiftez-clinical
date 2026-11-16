"""Real command checks for rest boundaries and physician-linked recurring clinics."""
import copy
import uuid
import pytest
from .test_series_reconciliation import template, materialise, reconcile, amend_template, edit


@pytest.mark.parametrize('overtime', [False, True])
def test_conditional_overtime_transition_respects_cross_period_ordinary_work(h, overtime):
    h.policy({'kind': 'forbidden_transition', 'from_type_ids': [h.seed['type']],
              'to_type_ids': [h.seed['type']], 'overtime_only': True})
    h.publish(h.shift(h.workspace(start='2026-10-03', end='2026-10-04'),
        '2026-10-03T08:00:00+03:00', '2026-10-03T12:00:00+03:00')['workspace'])
    result = h.shift(h.workspace(), people=[{'person_id': h.seed['people'][0], 'overtime': overtime,
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'}])
    assert ('FORBIDDEN_TRANSITION' in {f['code'] for f in result['validation']['findings']}) is overtime
    if overtime:
        h.lifecycle(result['workspace'], 'submit', status=422)
    else:
        h.publish(result['workspace'])


def test_half_day_protected_time_is_shared_by_allocation_publication_and_calendar(h):
    from packages.domain.generation import generate
    person = h.seed['people'][0]
    h.command('coordinator', 'availability.record', {'person_id': person,
        'kind': 'protected_time', 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'private_reason': 'Confidential academic detail'})
    morning = h.shift(h.workspace())
    assert 'UNAVAILABLE' in {f['code'] for f in morning['validation']['findings']}
    h.lifecycle(morning['workspace'], 'submit', status=422)
    h.policy({'kind': 'minimum_cover', 'minimum': 1})
    empty = h.shift(h.workspace(), people=[])['workspace']
    candidate = generate(h.snapshot(empty))
    assert candidate['status'] == 'candidate_feasible'
    assert candidate['additions'] and all(a['participant']['person_id'] != person for a in candidate['additions'])
    afternoon = h.shift(h.workspace(), '2026-10-04T12:00:00+03:00', '2026-10-04T16:00:00+03:00')
    assert afternoon['validation']['valid']
    h.publish(afternoon['workspace'])
    calendar = h.get('staff', '/api/calendar?start=2026-10-04&end=2026-10-05')
    assert {e['kind'] for e in calendar['events']} == {'availability', 'duty'}
    assert all(e['person_id'] == person for e in calendar['events'])
    assert 'Confidential academic detail' not in str(calendar)
    assert any(e['label'] == 'Protected time' for e in calendar['events'])  # its type, as the leave setting allows


@pytest.mark.parametrize('next_date,blocked', [('2026-10-04', True), ('2026-10-05', False)])
def test_prohibited_next_day_pair_is_distinct_from_rest_and_crosses_periods(h, next_date, blocked):
    evening = h.configure('assignment_types', {'name': 'Evening test duty', 'starts_on': '2020-01-01',
        'definition': {}})['record']
    h.policy({'kind': 'min_rest', 'hours': 8})
    h.policy({'kind': 'forbidden_transition', 'from_type_ids': [evening['id']], 'to_type_ids': [h.seed['type']]},
             scope={'assignment_type_id': h.seed['type']})
    prior = h.shift(h.workspace(start='2026-10-03', end='2026-10-04'),
        '2026-10-03T20:00:00+03:00', '2026-10-04T00:00:00+03:00', typ=evening['id'])['workspace']
    h.publish(prior)
    w = h.workspace()
    begin, end = next_date + 'T08:00:00+03:00', next_date + 'T12:00:00+03:00'
    payload = {'workspace_id': w['id'], 'assignment_type_id': h.seed['type'],
        'location_id': h.seed['locations'][0], 'service_ids': [h.seed['services'][0]],
        'start': begin, 'end': end, 'participants': [{'person_id': h.seed['people'][0], 'start': begin, 'end': end}]}
    preview = h.client('coordinator').post('/api/planner/preview', json={
        'kind': 'shift.create', 'expected_revision': w['revision'], 'payload': payload})
    assert preview.status_code == 200, preview.text
    result = h.command('coordinator', 'shift.create', payload, revision=w['revision'])
    for validation in [preview.json()['validation'], result['validation']]:
        codes = {f['code'] for f in validation['findings']}
        assert ('FORBIDDEN_TRANSITION' in codes) is blocked
        assert 'MIN_REST' not in codes
    if blocked:
        h.lifecycle(result['workspace'], 'submit', status=422)
    else:
        assert h.publish(result['workspace'])['publication_id']


@pytest.mark.parametrize('start,valid', [('07:59', False), ('08:00', True)])
def test_rest_across_published_period_preview_and_publication(h, start, valid):
    h.policy({'kind':'min_rest','hours':8})
    previous=h.shift(h.workspace(start='2026-10-03',end='2026-10-04'),
                     '2026-10-03T20:00:00+03:00','2026-10-04T00:00:00+03:00')['workspace']
    h.publish(previous)
    w=h.workspace()
    begin='2026-10-04T'+start+':00+03:00'; end='2026-10-04T12:00:00+03:00'
    payload={'workspace_id':w['id'],'assignment_type_id':h.seed['type'],
             'location_id':h.seed['locations'][0],'service_ids':[h.seed['services'][0]],
             'start':begin,'end':end,'participants':[{'person_id':h.seed['people'][0],'start':begin,'end':end}]}
    response=h.client('coordinator').post('/api/planner/preview',json={'kind':'shift.create','expected_revision':w['revision'],'payload':payload})
    assert response.status_code==200,response.text
    preview=response.json()['validation']
    assert preview['valid'] is valid
    assert bool([f for f in preview['findings'] if f['code']=='MIN_REST']) is not valid
    assert not h.detail(w)['shifts']
    w=h.command('coordinator','shift.create',payload,revision=w['revision'])['workspace']
    assert w['validation']['valid'] is valid
    if valid:
        result=h.publish(w)
        assert result['publication_id']
    else:
        assert h.lifecycle(w,'submit',status=422)['error']['code']=='VALIDATION_FAILED'


def test_fixed_clinic_assignee_timing_conflicts_and_following_update(h):
    series=template(h,days=(0,))
    definition=copy.deepcopy(series['definition']);definition['default_person_ids']=[h.seed['people'][0]]
    series=h.configure('recurring_series',{**{k:series[k] for k in ['name','starts_on','ends_on','assignment_type_id','location_id']},'definition':definition},series['id'],series['revision'])['record']
    w=materialise(h,series)
    clinic=h.detail(w)['shifts'][0]
    assert clinic['participants'][0]['person_id']==h.seed['people'][0]
    assert clinic['participants'][0]['start']==clinic['start'] and clinic['participants'][0]['end']==clinic['end']
    assert w['validation']['valid']
    # A conflicting departmental shift cannot reach publication.
    conflict=h.shift(w,'2026-10-05T09:00:00+03:00','2026-10-05T11:00:00+03:00',location=1)['workspace']
    assert 'TIME_OVERLAP' in {f['code'] for f in conflict['validation']['findings']}
    h.lifecycle(conflict,'submit',status=422)
    # Reconciliation retains explicitly assigned staff; new weekdays get usual assignees.
    other=h.workspace('Following clinic template')
    other=materialise(h,series,other)
    row=h.detail(other)['shifts'][0]
    other=edit(h,other,row,participants=[{'person_id':h.seed['people'][1],'start':row['start'],'end':row['end']}])
    definition=copy.deepcopy(series['definition']);definition['days'].append({'weekday':2,'start_time':'10:00','end_time':'14:00'})
    value={**{k:series[k] for k in ['name','starts_on','ends_on','assignment_type_id','location_id']},'definition':definition}
    preview=h.client('admin').post('/api/series/preview',json={'value':value,
        'series_id':series['id'],'expected_revision':series['revision']})
    assert preview.status_code==200,preview.text
    assert preview.json()['conflicts'], 'The edited template retains the intentional conflicting shift'
    series=h.command('admin','catalogue.update',{'catalogue':'recurring_series','value':value,
        'reason':'Explicitly review the conflicting synthetic following-template update',
        'overlap_acknowledgement':preview.json()['conflict_token']},series['id'],series['revision'])['record']
    other=reconcile(h,other,series,replace_modified=True)['workspace']
    assignments={r['occurrence_on']:r['participants'][0]['person_id'] for r in h.detail(other)['shifts']}
    assert assignments=={'2026-10-05':h.seed['people'][1],'2026-10-07':h.seed['people'][0]}


def test_recurring_assignee_does_not_grant_eligibility_and_duration_is_enforced(h):
    person=h.configure('people',{'display_name':'Uncredentialed fictional physician'})['record']
    series=template(h,days=(0,))
    definition=copy.deepcopy(series['definition']);definition['default_person_ids']=[person['id']]
    definition['days'][0].update(start_time='00:00',end_time='23:59')
    series=h.configure('recurring_series',{**{k:series[k] for k in ['name','starts_on','ends_on','assignment_type_id','location_id']},'definition':definition},series['id'],series['revision'])['record']
    w=h.workspace()
    result=h.command('coordinator','series.expand',{'workspace_id':w['id'],'series_id':series['id']},revision=w['revision'],status=422)
    assert result['error']['code']=='DURATION_LIMIT' and not h.detail(w)['shifts']
    series=amend_template(h,series,start='08:00',end='12:00')
    w=materialise(h,series,w)
    assert not w['validation']['valid']
    assert any(f['person_id']==person['id'] for f in w['validation']['findings'])
    h.lifecycle(w,'submit',status=422)


def test_recurring_unknown_and_duplicate_people_rejected(h):
    series=template(h)
    for ids,status in [([str(uuid.uuid4())],404),([h.seed['people'][0]]*2,422)]:
        definition={**series['definition'],'default_person_ids':ids}
        h.configure('recurring_series',{**{k:series[k] for k in ['name','starts_on','ends_on','assignment_type_id','location_id']},'definition':definition},series['id'],series['revision'],status=status)

@pytest.mark.parametrize('starts_on,scope', [('2020-01-01',{'weekdays':[0]}),('2026-10-05',{})])
def test_rest_scope_matches_target_duty_but_counts_preceding_work(h,starts_on,scope):
    proposal=h.command('coordinator','policy.create',{'name':'Eight hours before clinic','owner':'Fictional owner',
        'source':'Scoped rest boundary regression','starts_on':starts_on,
        'definition':{'scope':scope,'rule':{'kind':'min_rest','hours':8},'severity':'mandatory'}})['record']
    h.command('approver','policy.activate',{'reason':'Independent test review'},proposal['id'],proposal['revision'])
    w=h.shift(h.workspace(),'2026-10-04T20:00:00+03:00','2026-10-05T00:00:00+03:00')['workspace']
    w=h.shift(w,'2026-10-05T07:00:00+03:00','2026-10-05T11:00:00+03:00')['workspace']
    findings=[f for f in w['validation']['findings'] if f['code']=='MIN_REST']
    assert findings, 'Rest must count the preceding duty outside the target scope/effective dates'
    h.lifecycle(w,'submit',status=422)


def test_validator_change_requires_fresh_approval(h, monkeypatch):
    from apps.api import snapshots
    w=h.shift(h.workspace())["workspace"]
    w=h.lifecycle(w,"submit")["workspace"]
    w=h.lifecycle(w,"approve","approver")["workspace"]
    old=h.detail(w)["current_fingerprint"]
    from packages.domain import validation
    monkeypatch.setattr(validation,"ENGINE_VERSION","next-test-validator")
    assert h.detail(w)["current_fingerprint"] != old
    h.lifecycle(w,"publish",status=409)


def test_ucc_rest_is_configurable_and_allows_split_fm_clinics(h):
    from packages.domain.validation import evaluate
    h.policy({'kind': 'min_rest', 'hours': 12}, {'service_id': h.seed['services'][1]})
    w = h.shift(h.workspace(), service=0)['workspace']
    w = h.shift(w, '2026-10-04T13:00:00+03:00', '2026-10-04T16:00:00+03:00', service=0)['workspace']
    assert not any(f['code'] == 'MIN_REST' for f in w['validation']['findings'])
    w = h.shift(w, '2026-10-05T00:00:00+03:00', '2026-10-05T04:00:00+03:00', service=1)['workspace']
    w = h.shift(w, '2026-10-05T12:00:00+03:00', '2026-10-05T16:00:00+03:00', service=0)['workspace']
    data = h.snapshot(w)
    assert len([f for f in evaluate(data)['findings'] if f['code'] == 'MIN_REST']) == 2
    for policy in data['policies']:
        if policy['kind'] == 'min_rest':
            policy['definition']['rule']['hours'] = 8
    assert not any(f['code'] == 'MIN_REST' for f in evaluate(data)['findings'])

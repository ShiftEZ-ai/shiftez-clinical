"""Clinic closure and cover use ordinary revision-bound draft commands."""
import pytest


def action(h, w, shift_ids, kind, reason='Reviewed clinic absence resolution', status=200):
    return h.command('coordinator', 'shift.action', {'workspace_id': w['id'],
        'shift_ids': shift_ids, 'action': kind, 'reason': reason}, revision=w['revision'], status=status)


def test_closure_reason_survives_publication_and_amendment_without_counting_work(h):
    w=h.shift(h.workspace())['workspace'];sid=h.detail(w)['shifts'][0]['id']
    action(h,w,[sid],'cancel',reason='',status=422)
    action(h,w,[sid],'cancel',reason='   ',status=422)
    w=action(h,w,[sid],'cancel',reason='Clinic closed: no authorised cover')['workspace']
    assert w['validation']['valid']
    published=h.publish(w)
    calendar=h.get('coordinator','/api/calendar?start=2026-10-04&end=2026-10-11')
    assert not calendar['events']
    assert calendar['sessions'][0]['cancelled']
    assert calendar['sessions'][0]['assigned_people']==0
    assert calendar['sessions'][0]['cancellation_reason']=='Clinic closed: no authorised cover'
    own=h.get('staff','/api/calendar?start=2026-10-04&end=2026-10-11')
    assert not own['events'] and not own['sessions']
    draft=h.workspace(base=published['publication_id'])
    copied=h.detail(draft)['shifts'][0]
    assert copied['cancellation_reason']==calendar['sessions'][0]['cancellation_reason']
    draft=action(h,draft,[copied['id']],'reopen')['workspace']
    assert h.detail(draft)['shifts'][0]['cancellation_reason'] is None
    restored=h.lifecycle(draft,'undo')['workspace']
    assert h.detail(restored)['shifts'][0]['cancellation_reason']==copied['cancellation_reason']


@pytest.mark.parametrize('locked',[False,True])
def test_auto_cover_releases_only_absent_person_and_keeps_other_fixed_work(h,locked):
    start='2026-10-04T08:00:00+03:00';end='2026-10-04T12:00:00+03:00'
    people=[{'person_id':pid,'start':start,'end':end,'fixed':True} for pid in h.seed['people'][:2]]
    w=h.shift(h.workspace(),people=people)['workspace'];sid=h.detail(w)['shifts'][0]['id']
    h.command('coordinator','availability.record',{'person_id':h.seed['people'][0],
        'kind':'actual_absence','start':start,'end':end,'private_reason':'Never include in cover explanation'})
    if locked:
        w=action(h,w,[sid],'lock')['workspace']
        action(h,w,[sid],'release_unavailable',status=409)
        assert len(h.detail(w)['shifts'][0]['participants'])==2
        return
    payload={'workspace_id':w['id'],'shift_ids':[sid],'action':'release_unavailable','reason':'Prepare eligible automatic cover'}
    preview=h.client('coordinator').post('/api/planner/preview',json={
        'kind':'shift.action','expected_revision':w['revision'],'payload':payload})
    assert preview.status_code==200
    assert len(h.detail(w)['shifts'][0]['participants'])==2
    assert 'Never include' not in preview.text
    w=action(h,w,[sid],'release_unavailable')['workspace']
    shift=h.detail(w)['shifts'][0]
    assert not shift['cancelled'] and len(shift['participants'])==1
    assert shift['participants'][0]['person_id']==h.seed['people'][1]
    assert shift['participants'][0]['fixed']
    assert not any(f['code']=='UNAVAILABLE' for f in w['validation']['findings'])
    action(h,w,[sid],'release_unavailable',status=409)
    w=h.lifecycle(w,'undo')['workspace']
    assert len(h.detail(w)['shifts'][0]['participants'])==2


@pytest.mark.postgres
def test_closure_publication_postgres(pg):
    test_closure_reason_survives_publication_and_amendment_without_counting_work(pg)

"""Backend contracts awaited by the frontend audit lanes."""
from datetime import timedelta

from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.rota_summary import rota_summary


def test_open_places_and_sessions_with_gaps_agree():
    summary = rota_summary({'coverage': [
        {'shift_id': 'empty', 'required': 2, 'available_eligible': 1, 'status': 'gap'},
        {'shift_id': 'skill', 'required': 2, 'available_eligible': 2, 'status': 'gap'},
        {'shift_id': 'skill', 'required': 1, 'available_eligible': 1, 'status': 'gap'},
        {'shift_id': 'covered', 'required': 1, 'available_eligible': 1, 'status': 'covered'}]})
    assert summary['places_open'] == 1 and summary['other_gaps'] == 1
    assert summary['sessions_with_gaps'] == 2
    assert {s['shift_id'] for s in summary['sessions'] if s['other_gaps']} == {'skill'}


def test_job_dismiss_persists_and_is_audited(h):
    workspace = h.workspace()
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                    revision=workspace['revision'])['job']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.status, row.result = 'succeeded', {'status': 'candidate_feasible', 'additions': []}
    dismissed = h.command('coordinator', 'job.dismiss', {'reason': 'Discard this proposal'}, job['id'], job['revision'])
    assert dismissed['job']['status'] == 'dismissed'
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'dismissed'
    assert h.get('coordinator', '/api/jobs')['jobs'][0]['status'] == 'dismissed'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'job.dismiss')).target_id == job['id']
    h.command('coordinator', 'job.dismiss', {'reason': 'Again'}, job['id'], dismissed['job']['revision'], status=409)
    h.command('staff', 'job.dismiss', {'reason': 'Forbidden'}, job['id'], status=403)


def test_inbox_paging_is_stable_and_scoped(h):
    now = m.now()
    with transaction(h.app.state.engine, write=True) as db:
        for index in range(3):
            event = m.Outbox(department_id=h.seed['department_id'], event_key=m.uid(), kind='test', payload={})
            db.add(event)
            db.flush()
            db.add(m.Notification(outbox_id=event.id, identity_id=h.seed['identities']['staff'],
                title=str(index), message='Synthetic', created_at=now + timedelta(seconds=index)))
        db.add(m.Notification(outbox_id=event.id, identity_id=h.seed['identities']['approver'],
                              title='Private to approver', message='Synthetic'))
    first = h.get('staff', '/api/notifications?limit=2')
    last = h.get('staff', '/api/notifications?limit=2&offset=2')
    assert first['has_more'] and first['limit'] == 2 and first['offset'] == 0
    assert not last['has_more'] and [n['title'] for n in first['notifications'] + last['notifications']] == ['2', '1', '0']
    h.get('staff', '/api/notifications?limit=201', status=422)


def test_home_rule_change_deep_links_to_proposal(h):
    rule = h.command('coordinator', 'policy.create', {'name': 'Awaiting rule', 'owner': 'Synthetic',
        'source': 'Synthetic', 'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'maximum_participants', 'maximum': 3}, 'scope': {}, 'severity': 'mandatory'}})['record']
    item = next(i for i in h.get('approver', '/api/home')['attention'] if i['key'] == 'rules')
    assert item['link_target'] == '#rules?source=' + rule['id']

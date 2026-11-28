"""Scoped audit fixes: independent decisions, durable facts, bounded history and private diagnostics."""
import copy
import json
import uuid
from datetime import date, datetime

import pytest
from sqlalchemy import event, insert

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .governance_support import identity
from .test_rule_catalogue_api import approve, line, propose
from .test_scheduling_facts_api import decide, workload


@pytest.fixture(params=['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)])
def audit_h(request):
    return request.getfixturevalue('pg' if request.param == 'postgresql' else 'h')


def test_home_and_requests_share_independent_programme_decisions(audit_h):
    h = audit_h
    trainee, foreign, leader = h.seed['people'][:3]
    lead = identity(h, 'workflow-lead', ['programme_lead'], leader)
    alternate = identity(h, 'workflow-alternate', ['staff'], leader)
    identity(h, 'workflow-mixed-reader', ['programme_lead', 'auditor'], leader)
    with transaction(h.app.state.engine, write=True) as db:
        p = m.Programme(department_id=h.seed['department_id'], name='Fictional owned programme',
                        starts_on=date(2026, 1, 1), owner_identity_id=lead)
        db.add(p)
        db.flush()
        for person in (trainee, leader):
            db.add(m.ProgrammeMembership(department_id=p.department_id, person_id=person, programme_id=p.id,
                                         starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1)))
        pending_for_lead = []
        for person, creator, kind, status in (
            (trainee, h.seed['identities']['staff'], 'leave', 'submitted'),
            (trainee, h.seed['identities']['staff'], 'leave', 'submitted'),
            (foreign, h.seed['identities']['staff'], 'leave', 'submitted'),
            (leader, h.seed['identities']['staff'], 'leave', 'submitted'),
            (trainee, lead, 'leave', 'submitted'),
            (trainee, alternate, 'leave', 'submitted'),
            (trainee, h.seed['identities']['staff'], 'overtime_availability', 'submitted'),
            (trainee, h.seed['identities']['staff'], 'leave', 'rejected')):
            row = m.RequestRecord(department_id=p.department_id, person_id=person, created_by=creator, kind=kind,
                start=datetime.fromisoformat('2026-10-06T09:00:00+03:00'),
                end=datetime.fromisoformat('2026-10-06T12:00:00+03:00'), status=status,
                private_reason='Fictional private reason never on Home')
            db.add(row)
            if (person, kind, status) == (trainee, 'leave', 'submitted'):
                pending_for_lead.append(row)
        db.flush()
        expected_ids = {row.id for row in pending_for_lead}
    for role in ('workflow-lead', 'workflow-mixed-reader'):
        attention = {r['key']: r for r in h.get(role, '/api/home')['attention']}
        requests = h.get(role, '/api/requests/all')['counts']
        assert attention['requests']['count'] == requests['to_decide'] == 4
        mine = h.get(role, '/api/requests/all?for_me=true')
        assert {row['id'] for row in mine['items']} == expected_ids
        assert {r['person'] for r in attention['requests']['sample']} == {'Staff 01(test)'}
        assert 'private' not in json.dumps(attention['requests']).lower()
    # Approvers cannot decide overtime offers; coordinators can. The shared count unions roles only once.
    identity(h, 'workflow-combined', ['approver', 'coordinator'])
    for role, expected in (('approver', 6), ('coordinator', 1), ('workflow-combined', 7)):
        attention = {r['key']: r for r in h.get(role, '/api/home')['attention']}
        assert attention['requests']['count'] == h.get(role, '/api/requests/all')['counts']['to_decide'] == expected


def test_facts_leave_waiting_but_keep_scoped_terminal_details_and_history(audit_h):
    h = audit_h
    row = h.command('coordinator', 'person.workload.propose', workload(h))['fact']
    url = '/api/requests/all?id=' + row['id']
    assert h.get('approver', url)['item']['can_decide']
    for action, status, stage in [('activate', 'active', 'approved'), ('retire', 'retired', 'withdrawn')]:
        row = decide(h, row, action)['fact']
        for role in ('approver', 'coordinator', 'auditor'):
            result = h.get(role, url)
            assert result['fact']['status'] == result['item']['status'] == status
            assert result['item']['stage'] == stage and not result['item']['can_decide']
            assert result['item']['decided_at'] is not None
            assert any(e['action'] == 'person.workload.' + action for e in result['history'])
            assert row['id'] not in {r['id'] for r in h.get(role, '/api/requests/all?status=waiting')['items']}
            assert row['id'] in {r['id'] for r in h.get(role, '/api/requests/all')['items']}
        h.get('staff', url, status=404)
    declined = h.command('coordinator', 'person.workload.propose', workload(h))['fact']
    decide(h, declined, 'decline')
    assert h.get('approver', '/api/requests/all?id=' + declined['id'])['item']['stage'] == 'declined'
    assert h.get('approver', '/api/requests/all')['counts']['to_decide'] == 0
    identity(h, 'workflow-unowned-lead', ['programme_lead'])
    h.get('workflow-unowned-lead', url, status=404)
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Fictional separate department')
        db.add(other)
        db.flush()
        db.get(m.SchedulingFact, row['id']).department_id = other.id
    h.get('approver', url, status=404)


def test_home_counts_fact_once_separately_from_requests(audit_h):
    h = audit_h
    h.command('coordinator', 'person.workload.propose', workload(h))
    h.command('staff', 'request.create', {'person_id': h.seed['people'][0],
        'start': '2026-10-06T09:00:00+03:00', 'end': '2026-10-06T12:00:00+03:00',
        'private_reason': 'Fictional leave reason'})
    attention = {r['key']: r['count'] for r in h.get('approver', '/api/home')['attention']}
    assert attention['requests'] == attention['scheduling_facts'] == 1
    assert h.get('approver', '/api/requests/all')['counts']['to_decide'] == 2


def test_catalogue_history_is_sql_paged_beyond_generic_ceiling_with_provenance(audit_h):
    h = audit_h
    original = approve(h, propose(h, 'duties_per_week', [line(numbers={'max': {'value': 5}})])['record'])
    with transaction(h.app.state.engine, write=True) as db:
        base = db.get(m.Policy, original['id'])
        definition = copy.deepcopy(base.definition)
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Fictional history department')
        db.add(other)
        db.flush()
        records = []
        for index in range(4005):
            records.append(dict(id=str(uuid.uuid4()), revision=2, department_id=base.department_id,
                name='Fictional retained version', kind='catalogue', definition=definition, active=False,
                starts_on=date(2026, 1, 1), status='retired', owner='Fictional owner', source='Retained provenance',
                created_by=base.created_by, approved_by=base.approved_by))
        ids = [r['id'] for r in records]
        for dept, entry, version in ((base.department_id, 'hours_per_week', 1),
                                     (other.id, 'duties_per_week', 1), (base.department_id, 'workload_limit', 2)):
            body = copy.deepcopy(definition)
            body['rule'].update(id=entry, catalogue_version=version)
            records.append({**records[0], 'id': str(uuid.uuid4()), 'department_id': dept, 'definition': body})
        db.execute(insert(m.Policy), records)
    # Beyond the generic ceiling the retired list is paged, never read whole (maintenance: history no longer breaks loading).
    listing = h.get('coordinator', '/api/policies?status=retired')
    assert len(listing['policies']) == listing['limit'] == 4000 and listing['has_more'] is True
    url = '/api/rules/catalogue/duties_per_week/history'
    statements = []
    def capture(conn, cursor, statement, parameters, context, many):
        if 'FROM policies' in statement and 'ORDER BY' in statement:
            statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        first = h.get('coordinator', url + '?limit=100')
        last = h.get('coordinator', url + '?page=41&limit=100')
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    assert first['page'] == 1 and first['limit'] == 100 and first['has_more']
    assert len(first['policies']) == 100 and not last['has_more'] and len(last['policies']) == 6
    assert {p['id'] for p in first['policies']} <= set(ids)
    assert all(p['definition']['rule']['id'] == 'duties_per_week' for p in last['policies'])
    retained = next(p for p in last['policies'] if p['id'] == original['id'])
    assert retained['approved_by_name'] and retained['approved_at'] and retained['reason']
    assert statements and all('LIMIT' in q and 'OFFSET' in q and 'definition' in q for q in statements)
    for role in ('approver', 'credentialer', 'auditor'):
        allowed = role in s.PERMISSIONS['rules.read']
        result = h.get(role, url + '?limit=1', status=200 if allowed else 403)
        if allowed:
            assert result['policies'][0]['id'] == first['policies'][0]['id']
    identity(h, 'workflow-history-lead', ['programme_lead'])
    h.get('workflow-history-lead', url + '?limit=1')
    for role in ('staff', 'operator'):
        h.get(role, url, status=403)
    h.get('coordinator', '/api/rules/catalogue/unknown/history', status=404)
    h.get('coordinator', url + '?limit=101', status=422)
    h.get('coordinator', url + '?page=0', status=422)
    assert not h.get('coordinator', url + '?page=42&limit=100')['policies']


def test_feedback_sanitizes_current_route_and_errors_before_storage_without_rewriting_history(audit_h):
    h = audit_h
    sentinel = 'SyntheticUnissuedBearerOnly'
    payload = {'kind': 'problem', 'message': 'Fictional diagnostic report',
        'route': '#calendar/' + sentinel + '?token=' + sentinel,
        'recent_routes': ['#/accounts/reset/' + sentinel, '#home?token=' + sentinel],
        'recent_errors': [{'at': m.now().isoformat(), 'source': 'console',
                           'message': 'TypeError: https://example.invalid/reset/' + sentinel},
                          {'at': m.now().isoformat(), 'source': 'https://example.invalid/' + sentinel,
                           'message': 'ghp_FictionalSyntheticCredential'},
                          {'at': m.now().isoformat(), 'source': 'error', 'message': 'Failed to fetch'}]}
    saved = h.command('staff', 'feedback.create', payload)
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.FeedbackReport, saved['id'])
        assert sentinel not in json.dumps([stored.route, stored.recent_routes, stored.recent_errors])
        assert stored.route == '/calendar/[record]'
        assert [e['message'] for e in stored.recent_errors] == [
            'TypeError: [details redacted]', '[free-text error redacted]', 'Failed to fetch']
        assert stored.recent_errors[1]['source'] == 'unknown'
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.FeedbackReport, saved['id'])
        stored.route = '#home?legacy=' + sentinel
        original = copy.deepcopy(stored.recent_errors)
    h.command('staff', 'feedback.create', {**payload, 'kind': 'question'})
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.FeedbackReport, saved['id'])
        assert stored.route == '#home?legacy=' + sentinel and stored.recent_errors == original


def test_calendar_protection_direct_link_remains_readable_in_its_programme(audit_h):
    h = audit_h
    lead = identity(h, 'workflow-calendar-lead', ['programme_lead'])
    p = h.configure('programmes', {'name': 'Fictional lead programme', 'starts_on': '2026-01-01'})['record']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, p['id']).owner_identity_id = lead
    entry = h.configure('programme_calendar_entries', {'name': 'Fictional protected teaching',
        'programme_id': p['id'], 'kind': 'academic_block', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'})['record']
    row = h.command('coordinator', 'calendar.protection.propose', {'kind': 'protected_time',
        'calendar_entry_id': entry['id'], 'catalogue_id': 'protected_time', 'line': {'id': str(uuid.uuid4()),
        'weekdays': [1], 'start_time': '09:00', 'end_time': '11:00',
        'starts_on': entry['starts_on'], 'ends_on': entry['ends_on'],
        'numbers': {'apply': {'value': None, 'level': 'mandatory'}}}, 'reason': 'Fictional teaching protection'})['fact']
    for action in ('activate', 'retire'):
        row = decide(h, row, action)['fact']
        detail = h.get('workflow-calendar-lead', '/api/requests/all?id=' + row['id'])
        assert detail['fact']['status'] == row['status'] and not detail['item']['can_decide']
        assert detail['history'][-1]['action'] == 'calendar.protection.' + action
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, p['id']).owner_identity_id = None
    h.get('workflow-calendar-lead', '/api/requests/all?id=' + row['id'], status=404)


def test_generation_recovery_is_evidenced_actor_scoped_and_ephemeral(audit_h):
    from apps.api.snapshots import fingerprint
    h = audit_h
    w = h.workspace()
    identity(h, 'workflow-maintainer', ['coordinator', 'operator'])
    for index, (status, result, error, stale, action) in enumerate([
        ('succeeded', {'status': 'unsupported_rules'}, None, False, 'review_rules'),
        ('succeeded', {'status': 'implementation_error'}, None, False, 'report_problem'),
        ('succeeded', {'status': 'translation_error'}, None, False, 'report_problem'),
        ('failed', None, 'WORKER_LEASE_EXPIRED', False, 'report_problem'),
        ('queued', None, None, False, None),
        ('running', None, None, False, None),
        ('succeeded', {'status': 'unsupported_rules'}, None, True, None),
        ('failed', None, 'RuntimeError', True, None),
        ('succeeded', {'status': 'needs_more_time'}, None, False, None),
    ]):
        job_workspace = h.workspace('Running generation fixture') if status == 'running' else w
        with transaction(h.app.state.engine, write=True) as db:
            job = m.Job(department_id=h.seed['department_id'], workspace_id=job_workspace['id'],
                requested_by=h.seed['identities']['coordinator'], kind='generation', status=status,
                input_fingerprint='stale' if stale else fingerprint(db, db.get(m.Workspace, job_workspace['id'])),
                input_snapshot={'generation_engine': 'legacy'}, result=result, error_code=error)
            db.add(job)
            db.flush()
            job_id = job.id
        for role in ('coordinator', 'workflow-maintainer'):
            detail = h.get(role, '/api/jobs/' + job_id)['job']
            assert ('recovery' in detail) == (action is not None)
            if action:
                recovery = detail['recovery']
                assert recovery['action'] == action
                permission = 'rule.propose' if action == 'review_rules' else 'feedback.triage'
                assert recovery['owner_roles'] == list(s.PERMISSIONS[permission])
                assert recovery['cause_code'] == (error or result['status'])
                assert recovery['link_target'] == ('#rules' if action == 'review_rules' else
                    '#system/feedback' if role == 'workflow-maintainer' else None)
            if result is None:
                assert detail['result'] is None
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Job, job_id).result == result
        assert all('recovery' not in job for job in h.get('coordinator', '/api/jobs?workspace_id=' + job_workspace['id'])['jobs'])
    for role in ('staff', 'auditor', 'approver', 'operator'):
        h.get(role, '/api/jobs/' + job_id, status=403)


def test_worker_exception_produces_recovery_without_copying_private_error_text(audit_h):
    from apps.worker.main import process_one
    h = audit_h
    w = h.workspace()
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'},
                    revision=w['revision'])['job']
    def fail(snapshot, **options):
        raise RuntimeError('Fictional private implementation detail')
    assert process_one(h.app.state.engine, scheduler=fail)
    detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert detail['status'] == 'failed' and detail['result'] is None
    assert detail['recovery']['cause_code'] == 'RuntimeError'
    assert detail['recovery']['action'] == 'report_problem'
    assert detail['recovery']['link_target'] is None
    assert 'Fictional private' not in json.dumps(detail)


def test_demo_seed_marks_every_person_as_test_data(h):
    people = h.get('coordinator', '/api/catalogues')['people']
    labels = sorted(person['display_name'] for person in people)
    assert labels == ['Staff 01(test)', 'Staff 02(test)', 'Staff 03(test)']

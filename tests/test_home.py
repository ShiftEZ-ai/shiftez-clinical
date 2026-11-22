"""GET /api/home: role-scoped counts and short lists, cheap at department scale."""
from packages.domain.time import now as clock_now
import random
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import coverage
import pytest
from sqlalchemy import insert

from apps.api import models as m
from apps.api.auth import SESSION_EVENT_REASONS
from apps.api.db import transaction
from packages.domain.quotas import METRIC_VERSION
from .governance_support import identity

# What each role may be told about (the union for a person holding several roles).
ALLOWED = {
    'staff': {'my_swaps'},
    'coordinator': {'blocking', 'resubmit', 'overlap', 'open_places', 'ready_to_submit', 'unchecked', 'to_publish',
                    'leave_conflicts', 'patterns', 'swaps_to_apply', 'no_rank'},
    'approver': {'reviews', 'to_publish', 'requests', 'rules', 'exceptions'},
    'programme_lead': {'reviews', 'rotations_soon'},
    'credentialer': {'no_eligibility'},
    'auditor': {'in_review', 'self_approved', 'failed_notifications', 'failed_runs'},
    'operator': {'failed_notifications', 'failed_runs', 'self_approved', 'feedback'},
}


def keys(reply):
    return {i['key'] for i in reply['attention']}


def counts(reply):
    return {i['key']: i['count'] for i in reply['attention']}


def test_roles_see_only_their_work(h):
    me = h.seed['people'][0]
    w = h.shift(h.workspace('Home draft'), people=[])['workspace']  # one session nobody is on yet
    request = h.command('staff', 'request.create', {'person_id': me, 'start': '2026-10-06T09:00:00+03:00',
        'end': '2026-10-06T12:00:00+03:00', 'private_reason': 'Private family arrangement'})['record']
    h.command('coordinator', 'policy.create', {'name': 'Home proposed rule', 'owner': 'Test policy owner', 'source': 'Constructed fixture',
        'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'maximum_participants', 'maximum': 3}, 'scope': {}, 'severity': 'mandatory'}})
    identity(h, 'programme', ['programme_lead'])
    for role in ('staff', 'coordinator', 'approver', 'credentialer', 'auditor', 'operator', 'programme'):
        reply = h.get(role, '/api/home')
        allowed = ALLOWED['programme_lead' if role == 'programme' else role]
        assert keys(reply) <= allowed, (role, keys(reply))
        assert all(i['count'] > 0 for i in reply['attention'])
    approver = {i['key']: i for i in h.get('approver', '/api/home')['attention']}
    assert approver['requests']['count'] == 1 and approver['rules']['count'] == 1
    # A row says which: the person and times of the first requests, the rule's name and who proposed it, nothing private.
    sample = approver['requests']['sample']
    assert [(r['person'], r['kind'], r['start'][:10]) for r in sample] == [('Alex Morgan', 'leave', '2026-10-06')]
    assert set(sample[0]) == {'person', 'kind', 'start', 'end'}
    assert approver['rules']['sample'] == [{'name': 'Home proposed rule', 'by': 'Coordinator · Demo'}]
    assert keys(h.get('coordinator', '/api/home')) & {'unchecked', 'blocking'}
    item = next(i for i in h.get('coordinator', '/api/home')['attention'] if i.get('rota_id'))
    assert item['rota_id'] == w['id'] and item['rota_name'] == 'Home draft'
    # Staff read their own requests (latest first), never a private reason.
    mine = h.get('staff', '/api/home')['my_requests']
    assert [r['id'] for r in mine] == [request['id']] and mine[0]['status'] == 'submitted' and 'private_reason' not in mine[0]
    assert h.get('approver', '/api/home')['my_requests'] == []  # no linked person
    # Recent changes only for those who read the audit log.
    assert h.get('auditor', '/api/home')['recent_changes'] and not h.get('coordinator', '/api/home')['recent_changes']
    # An administrator holds the department roles: their union, ordered by priority, each kind once.
    admin = h.get('admin', '/api/home')['attention']
    assert len({i['key'] for i in admin}) == len(admin)
    assert 'requests' in {i['key'] for i in admin} and {i['key'] for i in admin} & {'unchecked', 'blocking'}


def test_self_approved_row_opens_decisions_the_reader_may_read(h):
    """The owner and administrators hold no audit role, yet Home's Self-approved decisions row is theirs: it counts their
    own decisions and the self-approved view lists exactly those; audit readers keep the department's; nobody else's."""
    def grant(**subject):
        return h.command('admin', 'self_approval.grant', {'actions': ['rule.activate', 'rota.approve'], 'starts_on': '2026-01-01',
                         'ends_on': '2027-01-01', 'reason': 'Only one approver this quarter', **subject})['permission']
    grant(identity_id=h.seed['identities']['admin'])  # the only admin granting themselves: self-approved
    identity(h, 'owner', ['coordinator', 'approver', 'admin'])
    grant(identity_id=h.seed['identities']['owner'])
    rule = h.command('owner', 'policy.create', {'name': 'Own rest rule', 'owner': 'QA', 'source': 'Home self-approval test',
        'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'min_rest', 'hours': 11}}})['record']
    h.command('owner', 'policy.activate', {'reason': 'Reviewed with the clinical lead by phone'}, rule['id'], rule['revision'])
    everyone = {e['id']: e['actor_id'] for e in h.get('auditor', '/api/audit?self_approved=true')['events']}
    assert sorted(everyone.values()) == sorted([h.seed['identities']['admin'], h.seed['identities']['owner']])
    for who in ('admin', 'owner'):
        assert counts(h.get(who, '/api/home'))['self_approved'] == 1
        reply = h.get(who, '/api/audit?self_approved=true')
        assert [e['actor_id'] for e in reply['events']] == [h.seed['identities'][who]] and reply['people'] == []
        # Naming someone else does not widen it; the rest of the audit log stays with audit readers.
        other = h.seed['identities']['owner' if who == 'admin' else 'admin']
        assert h.get(who, f'/api/audit?self_approved=true&actor_id={other}')['events'] == h.get(who, '/api/audit?self_approved=true')['events']
        assert h.client(who).get('/api/audit').status_code == 403
        assert h.client(who).get('/api/audit.csv?self_approved=true').status_code == 403
    assert counts(h.get('auditor', '/api/home'))['self_approved'] == 2
    # A role that decided nothing for itself is told nothing and reads nothing.
    assert 'self_approved' not in keys(h.get('coordinator', '/api/home'))
    assert h.get('coordinator', '/api/audit?self_approved=true')['events'] == []
    assert h.client('staff').get('/api/audit').status_code == 403


def test_home_cache_is_scoped_to_the_actor(h):
    leave_type = h.get('staff', '/api/leave-types')['leave_types'][0]['id']
    request = h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'leave',
        'leave_type_id': leave_type, 'start': '2026-10-06T09:00:00+03:00',
        'end': '2026-10-06T12:00:00+03:00', 'private_reason': 'Actor cache probe'})['record']
    own = h.get('staff', '/api/home')['my_requests']
    coordinator = h.get('coordinator', '/api/home')['my_requests']
    assert request['id'] in {row['id'] for row in own}
    assert request['id'] not in {row['id'] for row in coordinator}


def test_coordinator_home_keeps_last_fix_summary_for_a_stale_draft(h):
    w = h.shift(h.workspace('Stale Home draft'), people=[])['workspace']
    before = next(row for row in h.get('coordinator', '/api/home')['attention'] if row.get('rota_id') == w['id'])
    before_checked_at = before['fix_list']['summary']['checked_at']
    w = h.shift(w, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00', people=[])['workspace']

    rows = [row for row in h.get('coordinator', '/api/home')['attention'] if row.get('rota_id') == w['id']]
    assert len(rows) == 1
    row = rows[0]
    assert row['checked'] is False
    assert row['fix_list']['summary']['checked_at'] == before_checked_at


def test_home_lists_each_unchecked_draft_as_its_own_attention_row(h):
    drafts = [h.workspace(f'Unchecked draft {index}') for index in range(3)]
    rows = [row for row in h.get('coordinator', '/api/home')['attention'] if row['key'] == 'unchecked']

    assert {row['rota_id'] for row in rows} == {draft['id'] for draft in drafts}
    assert all(row['count'] == 1 and row['checked'] is False for row in rows)


def test_home_counts_only_decisions_this_person_may_take(h):
    """Their own leave request, weekly availability and proposed rule are not theirs to decide (the commands refuse them
    without a self-approval permission), so Home does not offer them; someone else's are offered."""
    me = h.seed['people'][0]
    identity(h, 'staff-approver', ['staff', 'approver', 'coordinator'], me)
    h.command('staff-approver', 'request.create', {'person_id': me, 'start': '2026-10-06T09:00:00+03:00',
        'end': '2026-10-06T12:00:00+03:00', 'private_reason': 'Own request'})
    h.command('staff-approver', 'availability_pattern.propose', {'person_id': me, 'weekdays': [1], 'start_time': '08:00',
        'end_time': '12:00', 'starts_on': '2026-10-04'})
    h.command('staff-approver', 'policy.create', {'name': 'Own proposed rule', 'owner': 'Test policy owner', 'source': 'Constructed fixture',
        'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'maximum_participants', 'maximum': 3}, 'scope': {}, 'severity': 'mandatory'}})
    mine = keys(h.get('staff-approver', '/api/home'))
    assert not mine & {'requests', 'patterns', 'rules'}, mine
    other = counts(h.get('approver', '/api/home'))
    assert other['requests'] == 1 and other['rules'] == 1
    assert counts(h.get('coordinator', '/api/home'))['patterns'] == 1


def test_a_rota_months_ahead_waits_on_home_for_whoever_may_review_it(h):
    """A submitted rota three months ahead is outside Home's near-dates window, yet the Schedule offers it for review:
    Home offers it too, to the approver only, and offers it to publish once approved."""
    identity(h, 'programme', ['programme_lead'])
    identity(h, 'planner-approver', ['coordinator', 'approver'])
    start = clock_now().date() + timedelta(days=99)

    def submitted(name, role, day):
        at = lambda hour: f'{day.isoformat()}T{hour:02}:00:00+03:00'
        w = h.command(role, 'workspace.create', {'name': name, 'starts_on': day.isoformat(),
                      'ends_on': (day + timedelta(days=7)).isoformat()})['workspace']
        w = h.shift(w, start=at(8), end=at(12), role=role)['workspace']
        return h.lifecycle(w, 'submit', role=role)['workspace']

    # Whoever made the rota may not approve it, however far its month: Home follows the same rule.
    own = submitted('Own far rota', 'planner-approver', start + timedelta(days=35))
    progress = h.get('planner-approver', '/api/workspaces/' + own['id'])['review_progress']
    assert not progress['can_decide'] and progress['decision_reason'] == 'SELF_APPROVAL'
    # Submitted last, so a later submission does not leave it out of date (that would be Rota to resubmit).
    far = submitted('Far review rota', 'coordinator', start)
    assert h.get('approver', '/api/workspaces/' + far['id'])['review_progress']['can_decide']
    review = next(i for i in h.get('approver', '/api/home')['attention'] if i['key'] == 'reviews')
    assert review['rota_id'] == far['id'] and review['rota_name'] == 'Far review rota'
    for role in ('coordinator', 'programme', 'staff'):
        assert 'reviews' not in keys(h.get(role, '/api/home')), role
    attention = h.get('planner-approver', '/api/home')['attention']
    mine = next((i for i in attention if i['key'] == 'reviews'), None)
    assert mine and mine['count'] == 1 and mine['rota_id'] == far['id'], attention
    approved = h.lifecycle(far, 'approve', role='approver')['workspace']
    assert approved['status'] == 'approved'
    publish = next(i for i in h.get('approver', '/api/home')['attention'] if i['key'] == 'to_publish')
    assert publish['rota_id'] == far['id']


def test_people_attention_counts_only_active_people_with_the_relevant_gap(h):
    missing_eligibility = h.configure('people', {'display_name': 'Active eligibility gap'})['record']
    missing_rank = h.configure('people', {'display_name': 'Active rank gap'})['record']
    h.configure('people', {'display_name': 'Inactive both gaps', 'active': False})
    h.command('coordinator', 'membership.create', {'person_id': missing_eligibility['id'],
        'category_id': h.seed['category'], 'starts_on': '2020-01-01'})
    h.command('credentialer', 'eligibility.create', {'person_id': missing_rank['id'],
        'service_id': h.seed['services'][0], 'starts_on': '2020-01-01'})
    for role, expected in [('admin', {'no_eligibility', 'no_rank'}),
                           ('credentialer', {'no_eligibility'}), ('coordinator', {'no_rank'})]:
        counts = {item['key']: item['count'] for item in h.get(role, '/api/home')['attention']
                  if item['key'] in {'no_eligibility', 'no_rank'}}
        assert counts == dict.fromkeys(expected, 1)


def test_decided_and_offered_work_moves_between_people(h):
    me = h.seed['people'][0]
    request = h.command('staff', 'request.create', {'person_id': me, 'start': '2026-10-06T09:00:00+03:00',
        'end': '2026-10-06T12:00:00+03:00', 'private_reason': 'Private'})['record']
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Approved for cover'}, request['id'], request['revision'])
    assert 'requests' not in keys(h.get('approver', '/api/home'))
    assert h.get('staff', '/api/home')['my_requests'][0]['status'] == 'approved'
    pattern = {'person_id': me, 'weekdays': [1], 'start_time': '08:00', 'end_time': '12:00', 'starts_on': '2026-10-04', 'note': 'School run'}
    h.command('staff', 'availability_pattern.propose', pattern)
    assert counts(h.get('coordinator', '/api/home'))['patterns'] == 1
    assert 'patterns' not in keys(h.get('approver', '/api/home'))


def test_swap_rows_name_both_people_and_the_offered_duty(h):
    start = (clock_now() + timedelta(days=3)).astimezone(timezone(timedelta(hours=3))).replace(hour=8, minute=0, second=0, microsecond=0)
    day = start.date().isoformat()
    w = h.shift(h.workspace('Home swap rota', day, (start.date() + timedelta(days=7)).isoformat()),
                start.isoformat(), (start + timedelta(hours=4)).isoformat())['workspace']
    w = h.shift(w, (start + timedelta(days=1)).isoformat(), (start + timedelta(days=1, hours=4)).isoformat(),
                people=[h.seed['people'][1]])['workspace']
    publication = h.publish(w)['publication_id']
    other = h.seed['people'][1]
    identity(h, 'colleague', ['staff'], other)
    duty = h.get('staff', '/api/swaps/options')['assignments'][0]
    h.command('staff', 'swap.offer', {'publication_id': publication, 'assignment_id': duty['assignment_id'], 'recipient_person_id': other,
                                      'expires_at': (clock_now() + timedelta(days=1)).isoformat(), 'reason': 'Private family matter'})
    item = next(i for i in h.get('colleague', '/api/home')['attention'] if i['key'] == 'my_swaps')
    assert item['count'] == 1
    assert item['sample'] == [{'proposer': 'Alex Morgan', 'recipient': 'Jordan Lee', 'proposer_id': h.seed['people'][0],
                               'start': item['sample'][0]['start'], 'end': item['sample'][0]['end']}]
    assert datetime.fromisoformat(item['sample'][0]['start']) == start  # the offered duty, never the message
    assert 'my_swaps' not in keys(h.get('staff', '/api/home'))  # the proposer waits; nothing needs them yet


def test_empty_department(h):
    with transaction(h.app.state.engine, write=True) as db:
        dept = m.Department(organisation_id=h.seed['organisation_id'], name='Empty department')
        db.add(dept)
        db.flush()
        dept_id = dept.id
    for role in ('coordinator', 'approver', 'auditor', 'credentialer'):
        identity(h, 'empty-' + role, [role], department=dept_id)
        reply = h.get('empty-' + role, '/api/home')
        assert reply['my_requests'] == []
        assert reply['recent_changes'] == []
        assert keys(reply) <= {'no_rank', 'no_eligibility'}, (role, reply)
    assert h.get('empty-coordinator', '/api/home')['rotas'] == 0
    assert h.get('empty-credentialer', '/api/home')['rotas'] is None


def test_home_reuses_revision_keyed_result_and_invalidates_after_write(h, monkeypatch):
    from apps.api.services import home as home_service
    calls = 0
    original = home_service.fingerprints

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(home_service, 'fingerprints', counted)
    first = h.get('coordinator', '/api/home')
    second = h.get('coordinator', '/api/home')
    assert second == first and calls == 1
    h.command('coordinator', 'policy.create', {'name': 'Cache invalidation rule', 'owner': 'Test owner',
        'source': 'Scale test', 'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'maximum_participants', 'maximum': 3}, 'scope': {}, 'severity': 'mandatory'}})
    h.get('coordinator', '/api/home')
    assert calls == 2


def test_home_cache_stamp_tracks_validation_job_status_without_revision_or_audit(h):
    w = h.shift(h.workspace('Home check liveness'), people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'},
                    revision=w['revision'])['job']

    def summary():
        return next(row for row in h.get('coordinator', '/api/home')['attention']
                    if row.get('rota_id') == w['id'])['fix_list']['summary']

    assert summary()['check_status'] == 'queued'
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Job, job['id']).status = 'running'
    assert summary()['check_status'] == 'running'
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Job, job['id']).status = 'failed'
    failed = summary()
    assert failed['checking'] is False and failed['check_status'] == 'failed'


def test_summary_etag_tracks_worker_liveness_without_a_write(h):
    w = h.shift(h.workspace('Summary liveness'), people=[])['workspace']
    worker_id = 'summary-etag-worker-' + str(uuid.uuid4())
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.WorkerHeartbeat(worker_id=worker_id, started_at=clock_now(), last_seen_at=clock_now()))
    path = f"/api/workspaces/{w['id']}/summary"
    client = h.client('coordinator')
    running = client.get(path)
    assert running.status_code == 200 and running.json()['worker_liveness'] == 'running'
    assert client.get(path, headers={'If-None-Match': running.headers['etag']}).status_code == 304
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.WorkerHeartbeat, worker_id).last_seen_at = clock_now() - timedelta(
            seconds=h.settings.worker_heartbeat_max_age_seconds + 1)
    stale = client.get(path, headers={'If-None-Match': running.headers['etag']})
    assert stale.status_code == 200 and stale.json()['worker_liveness'] == 'not_responding'
    assert client.get(path, headers={'If-None-Match': stale.headers['etag']}).status_code == 304


def test_recent_changes_filters_auth_before_limiting_and_keeps_department_scope(h):
    now, actor = clock_now(), h.seed['identities']['staff']
    person = h.seed['people'][0]
    auth_ids = {str(uuid.uuid4()) for _ in range(45)}
    sessions = {event_id: str(uuid.uuid4()) for event_id in auth_ids}
    actions = list(SESSION_EVENT_REASONS)
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Other department')
        db.add(other)
        db.flush()
        db.execute(insert(m.AuditEvent), [{'department_id': h.seed['department_id'], 'actor_id': actor,
            'action': 'shift.updated', 'created_at': now - timedelta(minutes=60 + n), 'reason': 'Synthetic change',
            'detail': {}, 'correlation_id': f'change-{n}'} for n in range(45)])
        db.execute(insert(m.AuditEvent), [{'id': event_id, 'department_id': h.seed['department_id'],
            'actor_id': actor, 'target_id': sessions[event_id], 'action': actions[n % len(actions)],
            'created_at': now - timedelta(minutes=n),
            'reason': SESSION_EVENT_REASONS[actions[n % len(actions)]],
            'detail': {'person_id': person, 'identity_id': actor, 'session_id': sessions[event_id]}, 'correlation_id': f'auth-{n}'}
            for n, event_id in enumerate(sorted(auth_ids))])
        db.add(m.AuditEvent(department_id=other.id, actor_id=actor, action='shift.updated',
            created_at=now + timedelta(minutes=1), reason='Other department change', detail={}, correlation_id='foreign'))
    for role in ('operator', 'auditor'):
        recent = h.get(role, '/api/home')['recent_changes']
        assert [row['correlation_id'] for row in recent] == [f'change-{n}' for n in range(40)]
        assert {row['department_id'] for row in recent} == {h.seed['department_id']}
    assert h.get('coordinator', '/api/home')['recent_changes'] == []
    assert h.get('admin', '/api/home')['recent_changes'] == []
    # Home's presentation filter does not remove durable audit or person access history.
    assert auth_ids <= {row['id'] for row in h.get('auditor', '/api/audit?limit=100')['events']}
    history = h.get('admin', f'/api/people/{person}/auth-activity?limit=100')
    assert auth_ids <= {row['id'] for row in history['events']}


def test_ready_to_submit_requires_a_non_cancelled_session(h):
    empty = h.workspace('Checked empty draft')
    assert empty['validation']
    assert 'ready_to_submit' not in keys(h.get('coordinator', '/api/home'))
    result = h.shift(empty)
    workspace, shift_id = result['workspace'], result['shift_id']
    ready = next(row for row in h.get('coordinator', '/api/home')['attention'] if row['key'] == 'ready_to_submit')
    assert ready['count'] == 1 and ready['rota_id'] == workspace['id']
    h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'], 'shift_ids': [shift_id],
        'action': 'cancel', 'reason': 'Synthetic cancellation'}, revision=workspace['revision'])
    assert 'ready_to_submit' not in keys(h.get('coordinator', '/api/home'))


def big_department(h, people=160, months=12, per_day=18, today_sessions=0):
    """160 people, a year of monthly publications (~540 sessions a month), three live drafts with stored checks, a year of
    requests and audit history. Inserted directly: Home must not care how much history there is. `today_sessions`: that
    many sessions today in this month's rota as Shift rows (what the windowed reads use), three people each."""
    s, rng = h.seed, random.Random(4)
    coordinator = s['identities']['coordinator']
    ids = [str(uuid.uuid4()) for _ in range(people)]
    today = clock_now().date()
    first = date(today.year - 1, today.month, 1)
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Person), [{'id': p, 'organisation_id': s['organisation_id'], 'display_name': f'Person {k:03}'} for k, p in enumerate(ids)])
        db.execute(insert(m.Membership), [{'person_id': p, 'department_id': s['department_id'], 'starts_on': date(2020, 1, 1),
                                           'category_id': s['category']} for p in ids[:-5]])
        db.execute(insert(m.Eligibility), [{'person_id': p, 'department_id': s['department_id'], 'starts_on': date(2020, 1, 1),
                                            'service_id': s['services'][0]} for p in ids[:-7]])
        start = first
        for k in range(months + 1):
            end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
            w = m.Workspace(department_id=s['department_id'], name=f'Rota {start:%b %Y}', starts_on=start, ends_on=end, status='draft',
                            created_by=coordinator, last_edited_by=coordinator)
            db.add(w)
            db.flush()
            if start <= today < end:
                for k in range(today_sessions):  # published duties are immutable: written while still a draft
                    a = datetime(today.year, today.month, today.day, 4 + k % 4 * 4, tzinfo=timezone.utc)
                    row = m.Shift(workspace_id=w.id, assignment_type_id=s['type'], type_revision=1, location_id=s['locations'][k % 2],
                                  type_snapshot={'name': f'Clinic {k % 4}', 'definition': {}}, start=a, end=a + timedelta(hours=4))
                    db.add(row)
                    db.flush()
                    db.add(m.ShiftService(shift_id=row.id, service_id=s['services'][k % 2]))
                    db.add_all(m.Assignment(shift_id=row.id, person_id=p, start=a, end=a + timedelta(hours=4), team_lead=j == 0)
                               for j, p in enumerate(ids[3 * k:3 * k + 3]))
                db.flush()
            w.status = 'published'
            shifts, day = [], start
            while day < end:
                for n in range(per_day):
                    a = datetime(day.year, day.month, day.day, 5 + n % 3 * 4, tzinfo=timezone.utc)
                    b = a + timedelta(hours=4)
                    team = rng.sample(ids, 2) if n % 9 else []
                    shifts.append({'id': str(uuid.uuid4()), 'cancelled': False, 'assignment_type_id': s['type'], 'service_ids': [s['services'][0]],
                                   'location_id': s['locations'][n % 2], 'type_snapshot': {'name': f'Clinic {n % 4}', 'definition': {}},
                                   'start': a.isoformat(), 'end': b.isoformat(),
                                   'participants': [{'id': str(uuid.uuid4()), 'person_id': p, 'start': a.isoformat(), 'end': b.isoformat(),
                                                     'overtime': j == 1, 'team_lead': j == 0} for j, p in enumerate(team)]})
                day += timedelta(days=1)
            db.add(m.Publication(department_id=s['department_id'], workspace_id=w.id, sequence=k + 1, starts_on=start, ends_on=end, current=True,
                                 published_by=coordinator, fingerprint='0' * 64,
                                 validation={'contracts': {'block_quota': METRIC_VERSION, 'programmes': 'programmes-v1'}},
                                 snapshot={'timezone': 'Asia/Riyadh', 'shifts': shifts, 'services': {s['services'][0]: {'name': 'Continuity clinic'}},
                                           'locations': {s['locations'][0]: {'name': 'North'}, s['locations'][1]: {'name': 'South'}}}))
            start = end
        for k in range(3):
            coverage_rows = [{'shift_id': str(uuid.uuid4()), 'status': 'gap' if n % 7 == 0 else 'covered'} for n in range(540)]
            findings = [{'code': 'COVERAGE_GAP', 'severity': 'error', 'shift_id': row['shift_id']}
                        for row in coverage_rows if row['status'] == 'gap']
            findings += [{'code': 'MIN_REST', 'severity': 'error', 'person_id': f'person-{n}'} for n in range(k)]
            db.add(m.Workspace(department_id=s['department_id'], name=f'Draft {k}', starts_on=start, ends_on=start + timedelta(days=28), status='draft',
                               created_by=coordinator, last_edited_by=coordinator,
                               validation={'valid': False, 'error_count': len(findings), 'warning_count': 0,
                                           'coverage': coverage_rows, 'findings': findings}))
        db.flush()
        from apps.api.snapshots import fingerprint
        from sqlalchemy import select
        for workspace in db.scalars(select(m.Workspace).where(m.Workspace.department_id == s['department_id'], m.Workspace.status == 'draft')):
            workspace.validation = {**workspace.validation, 'fingerprint': fingerprint(db, workspace)}
        db.execute(insert(m.RequestRecord), [{'department_id': s['department_id'], 'person_id': ids[n % people], 'kind': 'leave',
            'start': datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=n % 300), 'end': datetime(2026, 1, 2, tzinfo=timezone.utc) + timedelta(days=n % 300),
            'private_reason': 'Synthetic', 'status': 'submitted' if n % 50 == 0 else 'approved', 'created_by': coordinator} for n in range(3000)])
        db.execute(insert(m.AuditEvent), [{'department_id': s['department_id'], 'actor_id': coordinator, 'action': 'shift.updated',
            'reason': 'Synthetic', 'detail': {}, 'correlation_id': str(n)} for n in range(5000)])
    return ids


def department_home_measurement(h):
    big_department(h)
    for role in ('admin', 'coordinator', 'approver', 'auditor', 'staff'):
        h.get(role, '/api/home')  # warm sign-in and caches
    began, wall_started = time.process_time(), time.perf_counter()
    for _ in range(3):
        coordinator = h.get('coordinator', '/api/home')
    elapsed = (time.process_time() - began) / 3
    wall_elapsed = (time.perf_counter() - wall_started) / 3
    print(f'GET /api/home (coordinator, 160 people, 13 publications, 3 drafts): {elapsed * 1000:.0f} CPU ms / {wall_elapsed * 1000:.0f} wall ms')
    # Before and after, for the record: the reads the old Overview + Today made for a coordinator against those Home makes.
    day = clock_now().date().isoformat()
    after = (clock_now().date() + timedelta(days=1)).isoformat()
    old = ['/api/content', '/api/workspaces?limit=50&offset=0', '/api/publications', '/api/requests?status=submitted',
           '/api/swaps?limit=100&offset=0', '/api/availability-patterns', f'/api/calendar?start={day}&end={after}']
    new = ['/api/home', '/api/content', f'/api/calendar?start={day}&end={after}']
    for name, paths in (('old Overview + Today', old), ('Home', new)):
        began = time.perf_counter()
        for path in paths:
            h.get('coordinator', path)
        print(f'{name}: {len(paths)} requests, {(time.perf_counter() - began) * 1000:.0f} ms')
    return h, coordinator, elapsed, wall_elapsed


@pytest.fixture(scope='module')
def department_home_scale(scale_harness):
    return department_home_measurement(scale_harness('h'))


@pytest.fixture(scope='module')
def department_home_postgres_scale(scale_harness):
    return department_home_measurement(scale_harness('pg'))


def assert_department_home(result):
    h, coordinator, _, _ = result
    attention = {i['key']: i for i in coordinator['attention']}
    gaps = [i for i in coordinator['attention'] if i['key'] == 'blocking']
    assert len(gaps) == 3 and all(i['fix_list']['summary']['places_open'] == 78 and i['rotas'] == 1 for i in gaps)
    assert len({i['rota_id'] for i in gaps}) == 3
    assert sorted(i['count'] for i in gaps) == [1, 2, 2]
    assert not any(i['key'] == 'open_places' for i in coordinator['attention'])
    assert attention['no_rank']['count'] == 5
    assert counts(h.get('approver', '/api/home'))['requests'] == 60
    assert counts(h.get('credentialer', '/api/home'))['no_eligibility'] == 7
    assert len(h.get('auditor', '/api/home')['recent_changes']) <= 40


def test_department_scale_is_fast_and_right(department_home_scale):
    assert_department_home(department_home_scale)


def test_department_scale_home_rank_count_avoids_full_people_projection(department_home_scale, monkeypatch):
    from apps.api.services import people, read_projections

    h, *_ = department_home_scale
    materialized = []
    project = people.current_people

    def count_people(*args, **kwargs):
        result = project(*args, **kwargs)
        materialized.append(len(result['people']))
        return result

    monkeypatch.setattr(people, 'current_people', count_people)
    monkeypatch.setattr(read_projections, 'cached_department',
                        lambda db, actor, kind, stamp, build, **kwargs: build())

    assert counts(h.get('coordinator', '/api/home'))['no_rank'] == 5
    assert sum(materialized) <= 5, materialized


@pytest.mark.cpu_budget
def test_department_scale_is_fast_and_right_cpu_budget(department_home_scale):
    _, _, elapsed, _ = department_home_scale
    # Coverage tracing is several times slower; a full-history read would exceed either limit.
    budget = 5 if coverage.Coverage.current() else 1.5
    assert elapsed < budget, (elapsed, budget)


@pytest.mark.wall_benchmark
def test_department_scale_home_wall_benchmark(department_home_scale):
    _, _, _, elapsed = department_home_scale
    budget = 5 if coverage.Coverage.current() else 1.5
    assert elapsed < budget, (elapsed, budget)


@pytest.mark.postgres
def test_department_scale_postgres(department_home_postgres_scale):
    assert_department_home(department_home_postgres_scale)


@pytest.mark.postgres
@pytest.mark.cpu_budget
def test_department_scale_postgres_cpu_budget(department_home_postgres_scale):
    _, _, elapsed, _ = department_home_postgres_scale
    budget = 5 if coverage.Coverage.current() else 1.5
    assert elapsed < budget, (elapsed, budget)


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_department_scale_home_postgres_wall_benchmark(department_home_postgres_scale):
    _, _, _, elapsed = department_home_postgres_scale
    budget = 5 if coverage.Coverage.current() else 1.5
    assert elapsed < budget, (elapsed, budget)


@pytest.mark.postgres
def test_roles_postgres(pg):
    test_roles_see_only_their_work(pg)

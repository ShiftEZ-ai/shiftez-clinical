"""Requests filters intersect authority; completed facts remain discoverable and actionable."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_requests_search import both, read, request_row  # noqa: F401
from .test_scheduling_fact_approvals import calendar_entry, propose
from .test_scheduling_facts_api import decide


def assert_page(result, expected, *, all_stages=True):
    assert {row['id'] for row in result['items']} == set(expected)
    assert result['total'] == len(expected)
    if all_stages:
        assert sum(result['counts']['stages'].values()) == len(expected)
    assert not result['has_more']


def test_person_filter_intersects_every_source_and_each_mixed_role(both):
    h = both
    own, trainee, foreign = h.seed['people'][:3]
    roles = {'lead-staff': ['programme_lead', 'staff'],
             'lead-staff-auditor': ['programme_lead', 'staff', 'auditor'],
             'lead-staff-approver': ['programme_lead', 'staff', 'approver']}
    for name, granted in roles.items():
        identity(h, name, granted, own)
    identity(h, 'fixture-recipient', ['staff'], foreign)
    workspace = h.shift(h.workspace())['workspace']
    publication = h.publish(workspace)['publication_id']
    assignment = h.get('coordinator', '/api/publications/' + publication)['snapshot']['shifts'][0]['participants'][0]['id']
    rows = {pid: [] for pid in (own, trainee, foreign)}
    with transaction(h.app.state.engine, write=True) as db:
        programme = m.Programme(department_id=h.seed['department_id'], name='Fictional filter programme',
                                starts_on=date(2026, 1, 1), owner_identity_id=h.seed['identities']['lead-staff'])
        db.add(programme)
        db.flush()
        db.add_all(m.ProgrammeMembership(department_id=programme.department_id, programme_id=programme.id,
            person_id=pid, starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1)) for pid in (own, trainee))
        for pid in rows:
            start = datetime(2026, 10, 4, tzinfo=timezone.utc)
            creator = h.seed['identities']['coordinator']
            created = [request_row(h, pid, 'Fictional request'),
                m.Availability(department_id=programme.department_id, person_id=pid, kind='recorded_leave',
                    start=start, end=start + timedelta(days=1), recorded_by=creator, private_reason='Private fixture'),
                m.AvailabilityPattern(department_id=programme.department_id, person_id=pid, starts_on=start.date(),
                    weekdays=[1], start_time='09:00', end_time='11:00', created_by=creator, status='proposed'),
                m.StaffWish(department_id=programme.department_id, person_id=pid, first_day=start.date(),
                    last_day=start.date(), kind='prefer_off', points=1, created_by=creator, status='open')]
            db.add_all(created)
            db.flush()
            rows[pid] = [row.id for row in created]
        swap = m.SwapRequest(department_id=programme.department_id, publication_id=publication,
            proposer_id=h.seed['identities']['staff'], proposer_person_id=own,
            recipient_id=h.seed['identities']['fixture-recipient'], recipient_person_id=foreign,
            first_assignment_id=assignment, expires_at=m.now() + timedelta(days=1), status='offered', reason='Fictional swap')
        db.add(swap)
        db.flush()
        swap_id = swap.id
    facts = {pid: propose(h, person=pid)['id'] for pid in rows}
    for role in roles:
        team = role == 'lead-staff-approver'
        for query in ({'person_id': trainee}, {'person_ids': trainee}):
            expected = [*rows[trainee], facts[trainee]] if team else [rows[trainee][0], facts[trainee]]
            assert_page(read(h, role, **query), expected)
        assert_page(read(h, role, person_id=own), [*rows[own], facts[own], swap_id])
        assert_page(read(h, role, person_id=trainee, person_ids=own), [])
        expected = [*rows[foreign], facts[foreign], swap_id] if team else (
            [swap_id, facts[foreign]] if role == 'lead-staff-auditor' else [swap_id])
        assert_page(read(h, role, person_id=foreign), expected)
        own_request = read(h, role, id=rows[own][0])['item']
        assert not own_request.get('can_decide', False)
    for query in ({'person_id': trainee}, {'person_ids': trainee}):
        assert_page(read(h, 'staff', **query), [])
    assert_page(read(h, 'staff', person_id=own), [*rows[own], swap_id])
    identity(h, 'unlinked-filter-staff', ['staff'])
    assert_page(read(h, 'unlinked-filter-staff', person_id=trainee), [])


@pytest.mark.parametrize('kind', ['workload', 'target', 'overtime_offer', 'protected_time'])
def test_decided_fact_history_uses_scoped_projection_filters_and_paging(both, kind):
    h = both
    entry = calendar_entry(h) if kind == 'protected_time' else None
    facts = [propose(h, kind, entry=entry) for _ in range(4)]
    active = decide(h, facts[0])['fact']
    declined = decide(h, facts[1], 'decline')['fact']
    retired = decide(h, decide(h, facts[2])['fact'], 'retire')['fact']
    terminal = {row['id']: row for row in (active, declined, retired)}
    query = {'decided': 'true', 'type': 'other'}
    for role in ('approver', 'coordinator', 'auditor'):
        result = read(h, role, **query)
        assert_page(result, terminal)
        assert result['counts']['stages']['waiting'] == 0
        assert {row['stage'] for row in result['items']} == {'approved', 'declined', 'withdrawn'}
        for item in result['items']:
            detail = read(h, role, id=item['id'])
            assert detail['item'] == item and detail['fact'] == terminal[item['id']]
            assert detail['history'][-1]['at'] == item['decided_at']
            assert not item['can_decide']
        assert_page(read(h, role, **query, status='approved'), [active['id']], all_stages=False)
        assert_page(read(h, role, **query, q='no such fact'), [])
        assert_page(read(h, role, decided='true', type='leave'), [])
        person = active['person_id']
        if person:
            assert_page(read(h, role, **query, person_id=person), terminal)
            assert_page(read(h, role, **query, person_ids=h.seed['people'][1]), [])
        tree = {'conditions': [{'field': 'status', 'values': ['declined']},
                               {'field': 'start', 'op': 'on_or_after', 'value': '2026-01-01'}]}
        # Only calendar facts have explicit effective dates in these fixtures.
        if entry:
            assert_page(read(h, role, **query, filter=json.dumps({'conditions': tree})), [declined['id']])
        pages = [read(h, role, **query, sort_by='decided', sort_direction='desc', limit=1, offset=n) for n in range(3)]
        ids = [page['items'][0]['id'] for page in pages]
        assert set(ids) == set(terminal) and len(set(ids)) == 3
        assert [page['has_more'] for page in pages] == [True, True, False]
    assert_page(read(h, type='other'), [row['id'] for row in facts])
    waiting = read(h, type='other', status='waiting')
    assert_page(waiting, [facts[3]['id']], all_stages=False)
    assert waiting['counts']['stages'] == {**dict.fromkeys(waiting['counts']['stages'], 0),
        'waiting': 1, 'approved': 1, 'declined': 1, 'withdrawn': 1}
    assert read(h)['counts']['to_decide'] == 1
    attention = {row['key']: row for row in h.get('approver', '/api/home')['attention']}
    assert [row['id'] for row in attention['scheduling_facts']['sample']] == [facts[3]['id']]
    assert_page(read(h, 'staff', **query), [])
    identity(h, 'unowned-terminal-lead', ['programme_lead'])
    assert_page(read(h, 'unowned-terminal-lead', **query), [])
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Fictional foreign fact department')
        db.add(other)
        db.flush()
        for row in terminal.values():
            db.get(m.SchedulingFact, row['id']).department_id = other.id
    assert_page(read(h, **query), [])
    for row in terminal.values():
        h.get('approver', '/api/requests/all?id=' + row['id'], status=404)
    decide(h, active, 'retire', status=404)


def test_status_history_keeps_withdrawals_and_recorded_absence_distinct_from_decisions(both):
    h = both
    entry = calendar_entry(h)
    retired = decide(h, decide(h, propose(h, 'protected_time', entry=entry))['fact'], 'retire')['fact']
    pending = propose(h, 'protected_time', entry=entry)
    own = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        withdrawn = request_row(h, own, 'Fictional requester withdrawal', kind='meeting', status='withdrawn')
        absence = m.Availability(department_id=h.seed['department_id'], person_id=own,
            kind='recorded_leave', start=datetime(2026, 10, 4, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, tzinfo=timezone.utc), recorded_by=h.seed['identities']['coordinator'],
            private_reason='Fictional recorded absence')
        db.add_all([withdrawn, absence]);db.flush()
        withdrawal_id, absence_id = withdrawn.id, absence.id
    assert_page(read(h), [retired['id'], pending['id'], withdrawal_id, absence_id])
    selected = read(h, type='other', status='withdrawn')
    assert_page(selected, [retired['id'], withdrawal_id], all_stages=False)
    assert selected['counts']['stages']['withdrawn'] == 2
    assert selected['counts']['to_decide'] == 1
    assert_page(read(h, status='approved'), [absence_id], all_stages=False)
    assert_page(read(h, decided='true'), [retired['id']])
    assert_page(read(h, 'staff'), [withdrawal_id, absence_id])
    assert_page(read(h, person_id=h.seed['people'][1]), [])


def test_pure_approver_discovers_and_reads_calendar_management_without_programmes_grant(both):
    h = both
    lead_id = identity(h, 'protection-lead', ['programme_lead'])
    programme = h.configure('programmes', {'name': 'Fictional protected programme', 'starts_on': '2026-01-01'})['record']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, programme['id']).owner_identity_id = lead_id
    entry = calendar_entry(h, programme, name='Owned protection discovery')
    foreign_entry = calendar_entry(h, name='Foreign protection discovery')
    active = decide(h, propose(h, 'protected_time', entry=entry))['fact']
    for role in ('approver', 'coordinator', 'protection-lead', 'auditor'):
        assert_page(read(h, role, decided='true', q=entry['name']), [active['id']])
        panel = h.get(role, '/api/programme-calendar-entries/' + entry['id'] + '/protections')
        assert panel['calendar_entry_id'] == entry['id'] and panel['protections'] == [active]
    assert h.get('approver', '/api/me')['roles'] == ['approver']
    for role in ('coordinator', 'protection-lead', 'auditor', 'staff'):
        decide(h, active, 'retire', role=role, status=403)
    foreign_fact = decide(h, propose(h, 'protected_time', entry=foreign_entry))['fact']
    assert_page(read(h, 'protection-lead', decided='true', q=foreign_entry['name']), [])
    h.get('protection-lead', '/api/requests/all?id=' + foreign_fact['id'], status=404)
    h.get('protection-lead', '/api/programme-calendar-entries/' + foreign_entry['id'] + '/protections', status=403)
    for role in ('approver', 'auditor'):
        identity(h, 'protection-lead-' + role, ['programme_lead', role])
        assert h.get('protection-lead-' + role, '/api/programme-calendar-entries/' + foreign_entry['id'] +
                     '/protections')['protections'] == [foreign_fact]
    retired = decide(h, active, 'retire')['fact']
    panel_path = '/api/programme-calendar-entries/' + entry['id'] + '/protections'
    assert h.get('approver', panel_path)['protections'] == [retired]
    assert_page(read(h, 'approver', decided='true', q=entry['name'], status='withdrawn'), [retired['id']], all_stages=False)
    restored = decide(h, retired, 'undo')['fact']
    assert restored['status'] == 'active' and h.get('approver', panel_path)['protections'] == [restored]
    for role in ('staff', 'operator', 'credentialer'):
        h.get(role, '/api/programme-calendar-entries/' + entry['id'] + '/protections', status=403)
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Fictional foreign protection department')
        db.add(other)
        db.flush()
        db.get(m.ProgrammeCalendarEntry, foreign_entry['id']).department_id = other.id
    h.get('approver', '/api/programme-calendar-entries/' + foreign_entry['id'] + '/protections', status=404)

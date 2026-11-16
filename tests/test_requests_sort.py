"""Requests sorts the whole authorized history, with SQL/merge ties identical across page boundaries."""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_requests_search import both, read, request_row  # noqa: F401


def mixed_history(h):
    """More than one page in one source, plus every other source and equal keys across sources."""
    me, other, third = h.seed['people'][:3]
    identity(h, 'staff_two', ['staff'], other)
    workspace = h.shift(h.workspace())['workspace']
    publication = h.publish(workspace)['publication_id']
    shift = h.get('coordinator', '/api/publications/' + publication)['snapshot']['shifts'][0]
    assignment = shift['participants'][0]['id']
    records = []
    with transaction(h.app.state.engine, write=True) as db:
        names = {me: 'Ada 2', other: 'Zulu 10', third: 'ÉCLAIR'}
        for pid, name in names.items():
            db.get(m.Person, pid).display_name = name
        for n in range(220):
            row = request_row(h, (me, other, third)[n % 3], 'Sort needle', day=n % 11,
                              kind='meeting' if n % 4 == 0 else 'leave',
                              status='approved' if n % 3 == 0 else 'submitted')
            records.append(('request', row, 'other' if row.kind != 'leave' else 'leave', row.start))
        for n in range(7):
            pid = (me, other, third)[n % 3]
            day = date(2026, 10, 4) + timedelta(days=n)
            at = datetime.combine(day, time(), timezone.utc)
            base = dict(department_id=h.seed['department_id'], person_id=pid)
            creator = h.seed['identities']['coordinator']
            row = m.Availability(**base, start=at, end=at + timedelta(days=1), kind='recorded_leave',
                                 private_reason='Hidden sort secret', recorded_by=creator)
            records.append(('availability', row, 'leave', at))
            row = m.AvailabilityPattern(**base, starts_on=day, weekdays=[1], start_time='13:00', end_time='17:00',
                                        note='Sort needle', status='proposed', created_by=creator)
            records.append(('pattern', row, 'weekly', at))
            row = m.StaffWish(**base, first_day=day, last_day=day, kind='prefer_off', points=1,
                              note='Sort needle', status='open', created_by=creator)
            records.append(('wish', row, 'wish', at))
        first = db.get(m.Assignment, assignment)
        row = m.SwapRequest(department_id=h.seed['department_id'], publication_id=publication,
                            proposer_id=h.seed['identities']['staff'], proposer_person_id=me,
                            recipient_person_id=other, recipient_id=h.seed['identities']['staff_two'],
                            first_assignment_id=assignment, expires_at=m.now() + timedelta(days=1),
                            status='offered', reason='Sort needle')
        at = first.start.replace(tzinfo=timezone.utc) if first.start.tzinfo is None else first.start.astimezone(timezone.utc)
        records.append(('swap', row, 'swap', at))
        db.add_all([row for _, row, _, _ in records])
        db.flush()
        return [dict(id=row.id, source=source, type=kind, when=at,
                     person=names[getattr(row, 'person_id', None) or row.proposer_person_id].lower())
                for source, row, kind, at in records]


@pytest.mark.parametrize('key', ['type', 'person', 'when'])
@pytest.mark.parametrize('direction', ['asc', 'desc'])
def test_full_history_sort_pages_and_ties_match_in_both_directions(both, key, direction):
    h = both
    records = mixed_history(h)
    expected = sorted(records, key=lambda r: (r['source'], r['id']))
    expected.sort(key=lambda r: r[key], reverse=direction == 'desc')
    pages = [read(h, sort_by=key, sort_direction=direction, offset=offset)
             for offset in range(0, len(records), 100)]
    actual = [row for page in pages for row in page['items']]
    assert [row['id'] for row in actual] == [row['id'] for row in expected]
    assert len({row['id'] for row in actual}) == len(records) == 242
    assert {row['source'] for row in actual} == {'request', 'availability', 'pattern', 'wish', 'swap'}
    assert all(page['total'] == len(records) for page in pages)
    assert [page['has_more'] for page in pages] == [True, True, False]
    assert all(page['counts'] == pages[0]['counts'] for page in pages)
    assert sum(pages[0]['counts']['stages'].values()) == len(records)
    assert [row['id'] for row in read(h, sort_by=key, sort_direction=direction)['items']] == [r['id'] for r in expected[:100]]


def test_sort_keeps_search_filters_stage_counts_and_private_fields_scoped(both):
    h = both
    mixed_history(h)
    me, other = h.seed['people'][:2]
    with transaction(h.app.state.engine, write=True) as db:
        for person in (me, other):
            db.get(m.Person, person).display_name = 'Sort needle ' + db.get(m.Person, person).display_name
        foreign = m.Department(organisation_id=h.seed['organisation_id'], name='Sort isolation')
        db.add(foreign)
        db.flush()
        db.add(request_row(h, me, 'Sort needle', day=1000, department=foreign.id))
    query = dict(q='Sort needle', type='leave,other', status='waiting', person_ids=','.join((me, other)))
    baseline = read(h, **query)
    for key in ('type', 'person', 'when'):
        for role in ('admin', 'coordinator', 'approver'):
            result = read(h, role, **query, sort_by=key, sort_direction='desc')
            assert result['total'] == baseline['total']
            assert result['counts'] == read(h, role, **query)['counts']
            assert all(row['person_id'] in (me, other) and row['stage'] == 'waiting' for row in result['items'])
            assert max(row['start'] for row in result['items']) < '2027'
        mine = read(h, 'staff', sort_by=key, sort_direction='desc', person_ids=other)
        assert all(row['person_id'] == me for row in mine['items'])
        # The only match is the actor's swap with this selected recipient.
        assert mine['total'] == 1 and {row['source'] for row in mine['items']} == {'swap'}
        for role in ('staff', 'coordinator', 'approver'):
            hidden = read(h, role, q='Hidden sort secret', sort_by=key)
            assert hidden['items'] == [] and hidden['total'] == 0
            assert sum(hidden['counts']['stages'].values()) == 0
            visible = read(h, role, q='recorded leave', sort_by=key)
            assert visible['items'] and all('private_reason' not in row for row in visible['items'])
    identity(h, 'unlinked_staff', ['staff'])
    identity(h, 'programme_lead', ['programme_lead'])
    assert read(h, 'unlinked_staff', sort_by='person', person_ids=me)['total'] == 0
    for role in ('credentialer', 'operator'):
        assert h.client(role).get('/api/requests/all', params={'sort_by': 'when'}).status_code == 403
    for role in ('auditor', 'programme_lead'):
        assert h.get(role, '/api/requests/all?sort_by=when')['items'] == []


def test_explicit_sort_reaches_the_opposite_end_and_default_is_unchanged(both):
    h = both
    with transaction(h.app.state.engine, write=True) as db:
        rows = [request_row(h, h.seed['people'][0], 'Sort dates', day=n) for n in range(205)]
        db.add_all(rows)
        db.flush()
        ids = [row.id for row in rows]
    assert [row['id'] for row in read(h)['items']] == ids[:100]
    assert [row['id'] for row in read(h, sort_by='when', sort_direction='desc')['items']] == ids[::-1][:100]
    assert [row['id'] for row in read(h, sort_direction='desc')['items']] == ids[:100]
    assert [row['id'] for row in read(h, 'staff', sort_by='when', sort_direction='desc')['items']] == ids[::-1][:100]


def test_sort_rejects_unsupported_fields_and_directions(h):
    for params in ({'sort_by': 'detail'}, {'sort_by': 'impact'}, {'sort_by': 'private_reason'},
                   {'sort_by': 'when desc; drop table people'}, {'sort_direction': 'sideways'}):
        assert h.client('approver').get('/api/requests/all', params=params).status_code == 422

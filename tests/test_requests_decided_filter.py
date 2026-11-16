"""Recent decisions are filtered before paging, so recorded leave cannot hide them."""
from datetime import datetime, timedelta, timezone

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import availability


def decision_history(h, size=1000):
    at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.Availability(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
            kind='recorded_leave', start=at + timedelta(days=index), end=at + timedelta(days=index + 1),
            private_reason='Synthetic recorded leave', recorded_by=h.seed['identities']['coordinator'])
            for index in range(size))
        rows = [m.RequestRecord(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
            kind='meeting', start=at + timedelta(days=50 - index), end=at + timedelta(days=51 - index),
            status='approved' if index % 2 else 'rejected', private_reason='Synthetic decision',
            created_by=h.seed['identities']['staff'], decided_by=h.seed['identities']['approver'],
            decided_at=at + timedelta(minutes=index)) for index in range(7)]
        # A withdrawn request without a decider is not a decision made by an approver.
        db.add(m.RequestRecord(department_id=h.seed['department_id'], person_id=h.seed['people'][0], kind='meeting',
            start=at, end=at + timedelta(hours=1), status='withdrawn', private_reason='Synthetic withdrawal',
            created_by=h.seed['identities']['staff'], decided_at=m.now()))
        db.add_all(rows)
        db.flush()
        return [row.id for row in reversed(rows)]


def test_explicit_decisions_are_filtered_before_limit_and_stage_counts(h, monkeypatch):
    expected = decision_history(h)
    projected = []
    project = availability._project

    def count_projection(*args, **kwargs):
        projected.append(args[3])
        return project(*args, **kwargs)

    monkeypatch.setattr(availability, '_project', count_projection)
    path = '/api/requests/all?type=leave,other&decided=true&sort_by=decided&sort_direction=desc&limit=5'
    first = h.get('approver', path)
    assert [row['id'] for row in first['items']] == expected[:5]
    assert {row['source'] for row in first['items']} == {'request'}
    assert first['total'] == sum(first['counts']['stages'].values()) == 7
    assert first['has_more']
    # Hydrate only the page; this deterministic call budget holds under CI coverage and CPU contention.
    assert len(projected) == 5
    second = h.get('approver', path + '&offset=5')
    assert [row['id'] for row in second['items']] == expected[5:]
    assert not second['has_more']
    assert [row['id'] for row in h.get('staff', path)['items']] == expected[:5]
    assert h.get('approver', '/api/requests/all?type=leave,other&limit=5')['total'] == 1008


def test_decided_filter_keeps_existing_department_and_person_scope(h):
    expected = decision_history(h, size=5)
    with transaction(h.app.state.engine, write=True) as db:
        original = db.get(m.RequestRecord, expected[0])
        original.person_id = h.seed['people'][1]
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Other decisions')
        db.add(department)
        db.flush()
        db.add(m.RequestRecord(department_id=department.id, person_id=h.seed['people'][0], kind='meeting',
            start=m.now(), end=m.now() + timedelta(hours=1), status='approved', private_reason='Synthetic foreign',
            created_by=h.seed['identities']['staff'], decided_by=h.seed['identities']['approver'], decided_at=m.now()))
    path = '/api/requests/all?type=leave,other&decided=true&sort_by=decided&sort_direction=desc'
    assert [row['id'] for row in h.get('staff', path)['items']] == expected[1:]
    assert [row['id'] for row in h.get('approver', path)['items']] == expected

"""Live-form preview latency includes authentication, SQL and response serialization."""
import time
from datetime import date, timedelta

import pytest
from sqlalchemy import event, select
from apps.api import models as m
from apps.api.db import transaction
from packages.domain.time import aware, now
from tests.test_series_overlap_api import candidate, preview, state, reviewed
from tests.test_series_overlap_boundaries import series_month


@pytest.fixture
def live_preview(h, series_month):
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.Policy(department_id=h.seed['department_id'], name=f'Synthetic unrelated rule {n}',
            kind='max_hours', starts_on=date(2020, 1, 1), owner='Synthetic test owner', source='Synthetic scale fixture',
            created_by=h.seed['identities']['admin'], definition={'schema_version': 1,
                'severity': 'advisory', 'scope': {}, 'rule': {'kind': 'max_hours', 'hours': 40}}) for n in range(1000))
    with transaction(h.app.state.engine) as db:
        people = list(db.scalars(select(m.Person.id).order_by(m.Person.id).limit(100)))
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).leave_reason_display = 'details'
        db.add_all(m.Availability(department_id=h.seed['department_id'], person_id=pid, kind='recorded_leave',
            start=aware('2026-10-05T08:00:00+03:00'), end=aware('2026-10-05T12:00:00+03:00'),
            leave_type='Annual leave', private_reason='Synthetic scale leave note',
            recorded_by=h.seed['identities']['coordinator']) for pid in people)
    value = candidate(h)
    value['definition']['default_person_ids'] = people
    client = h.client('admin')
    before = state(h)
    statements, elapsed, replies = [], [], []

    def queried(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(h.app.state.engine, 'before_cursor_execute', queried)
    try:
        for _ in range(5):
            before_queries = len(statements)
            began = time.process_time()
            response = client.post('/api/series/preview', json={'value': value})
            elapsed.append(time.process_time() - began)
            assert response.status_code == 200, response.text
            assert statements[before_queries:].count('SELECT') < 40
            replies.append(response.json())
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', queried)
    assert state(h) == before
    return elapsed, statements, replies


def test_live_series_preview_is_read_only_and_repeatable(live_preview):
    _, statements, replies = live_preview
    assert not {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'} & set(statements)
    assert replies[0]['conflicts'] and all(reply == replies[0] for reply in replies)
    flags = [flag for flag in replies[0]['conflicts'] if flag['code'] == 'UNAVAILABLE']
    assert len(flags) == 100 and all(flag['label'] == 'Annual leave: Synthetic scale leave note' for flag in flags)


@pytest.mark.cpu_budget
def test_live_series_preview_stays_under_300ms(live_preview):
    from tests.test_scheduler_performance import _budget
    elapsed, _, _ = live_preview
    milliseconds = [round(value * 1000, 2) for value in elapsed]
    print('live recurring preview CPU ms:', milliseconds)
    # Keep the existing all-but-slowest CPU ceiling; descheduled time is excluded.
    assert sorted(elapsed)[-2] < _budget(0.3), milliseconds


@pytest.mark.parametrize('saved', [False, True])
def test_series_previews_do_not_write_even_when_session_activity_is_due(h, saved):
    value = candidate(h)
    row = reviewed(h, value) if saved else None
    client = h.client('admin')
    with transaction(h.app.state.engine, write=True) as db:
        session = db.scalar(select(m.Session).where(m.Session.identity_id == h.seed['identities']['admin']))
        session.last_active_at = now() - timedelta(minutes=2)
        previous = (session.id, session.last_active_at, session.expires_at, session.revision)
    writes = []

    def queried(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().split()[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
            writes.append(statement)

    event.listen(h.app.state.engine, 'before_cursor_execute', queried)
    try:
        response = (client.get(f"/api/series/{row['id']}/preview") if saved else
                    client.post('/api/series/preview', json={'value': value}))
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', queried)
    assert response.status_code == 200, response.text
    assert writes == [] and 'set-cookie' not in response.headers
    with transaction(h.app.state.engine) as db:
        session = db.get(m.Session, previous[0])
        assert (aware(session.last_active_at), aware(session.expires_at), session.revision) == (
            aware(previous[1]), aware(previous[2]), previous[3])


def test_live_preview_does_not_revive_an_expired_session(h):
    client = h.client('admin')
    with transaction(h.app.state.engine, write=True) as db:
        session = db.scalar(select(m.Session).where(m.Session.identity_id == h.seed['identities']['admin']))
        session.expires_at = now() - timedelta(seconds=1)
    response = client.post('/api/series/preview', json={'value': candidate(h)})
    assert response.status_code == 401 and 'set-cookie' not in response.headers


def test_live_preview_keeps_calendar_closures_with_reduced_inputs(h):
    from tests.test_date_set_calendars import date_set, opening
    reviewed(h, candidate(h, 'Synthetic peer on another clinic calendar', service=1))
    opening(h, date_set(h, '2026-10-05', '2026-10-05'))
    result = preview(h, candidate(h))
    assert len(result['occurrences']) == 3
    assert {row['date'] for row in result['conflicts']} == {'2026-10-12', '2026-10-19', '2026-10-26'}

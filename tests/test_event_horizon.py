"""Rota windows expand blocking series independently of the delivery horizon."""
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.events import blocking_occurrences
from packages.domain.cp_sat import generate
from packages.domain.time import aware
from packages.domain.validation import evaluate
from .test_events import definition, event, event_type, input_of, input_revisions, occurrences, repeat


def horizon(h, days):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).event_horizon_days = days


def projected(h, start, end):
    with transaction(h.app.state.engine) as db:
        return blocking_occurrences(db, [h.seed['department_id']], aware(start), aware(end))


@pytest.mark.parametrize('day', ['2026-10-18', '2027-10-10'])
@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_api_validation_and_solver_respect_series_beyond_delivery_horizon(request, database, day):
    h = request.getfixturevalue(database)
    horizon(h, 7)
    blocked, available = h.seed['people'][:2]
    row = event(h, event_type(h, blocks_duties=True, default_audience={'person_ids': [blocked]}), repeat=repeat())
    before = occurrences(h, end='2026-10-10')
    assert len(before) == 1
    end = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    w = h.shift(h.workspace(start=day, end=end), start=f'{day}T08:00:00+03:00',
        end=f'{day}T12:00:00+03:00', people=[blocked], seed_restricted=True)['workspace']
    assert any(f['code'] == 'RESTRICTION' and f['person_id'] == blocked for f in w['validation']['findings'])
    data = h.snapshot(w)
    assert any(f['code'] == 'RESTRICTION' and f['person_id'] == blocked for f in evaluate(data)['findings'])
    data['shifts'][0]['participants'] = []
    data['people'] = {pid: data['people'][pid] for pid in (blocked, available)}
    result = generate(data, 5, deterministic=True)
    assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
    assert result['additions'] and {item['participant']['person_id'] for item in result['additions']} == {available}
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.EventOccurrence).where(m.EventOccurrence.event_id == row['id'])) == 1


def test_projection_preserves_individual_cancellations_moves_and_current_series_snapshot(h):
    horizon(h, 28)
    row = event(h, event_type(h, blocks_duties=True), repeat=repeat())
    original = {item['start'][:10]: item for item in occurrences(h)}
    cancelled, moved = original['2026-10-11'], original['2026-10-18']
    h.command('coordinator', 'event.occurrence.update', {'value': input_of(cancelled['data'], cancelled=True),
        'reason': 'Cancel one occurrence'}, cancelled['id'], cancelled['revision'])
    h.command('coordinator', 'event.occurrence.update', {'value': input_of(moved['data'],
        start='2026-10-25T15:00:00+03:00', end='2026-10-25T16:00:00+03:00'),
        'reason': 'Move one occurrence'}, moved['id'], moved['revision'])
    horizon(h, 7)
    current = definition(h, row)
    h.configure('events', input_of(current, heading='Current series snapshot'), row['id'], current['revision'], role='coordinator')
    assert projected(h, '2026-10-11T00:00:00+03:00', '2026-10-19T00:00:00+03:00') == []
    future = projected(h, '2026-10-25T00:00:00+03:00', '2026-10-26T00:00:00+03:00')
    assert {aware(item.start) for item in future} == {aware('2026-10-25T09:00:00+03:00'), aware('2026-10-25T15:00:00+03:00')}
    restored = next(item for item in future if item.id == original['2026-10-25']['id'])
    assert restored.data['heading'] == 'Current series snapshot'
    with transaction(h.app.state.engine) as db:
        assert not db.get(m.EventOccurrence, restored.id).cancelled, 'shortening the horizon retains a matching occurrence'
    current = definition(h, row)
    h.configure('events', input_of(current, cancelled=True), row['id'], current['revision'], role='coordinator')
    assert projected(h, '2026-10-25T00:00:00+03:00', '2026-10-26T00:00:00+03:00') == []


@pytest.mark.parametrize('repeat_until', [None, '2026-10-12'])
def test_projection_includes_overnight_overlap_and_honours_exclusive_series_and_type_ends(h, repeat_until):
    horizon(h, 7)
    event(h, event_type(h, blocks_duties=True, ends_on='2026-10-19'),
        start='2026-10-04T23:00:00+03:00', end='2026-10-05T01:00:00+03:00', repeat_until=repeat_until,
        repeat=repeat(days=[{'weekday': 6, 'start_time': '23:00', 'end_time': '01:00'}]))
    [overlap] = projected(h, '2026-10-12T00:00:00+03:00', '2026-10-12T01:00:00+03:00')
    assert overlap.start == aware('2026-10-11T23:00:00+03:00')
    assert projected(h, '2026-10-12T01:00:00+03:00', '2026-10-12T02:00:00+03:00') == []
    assert projected(h, '2026-10-19T00:00:00+03:00', '2026-10-19T01:00:00+03:00') == []


def test_audience_access_change_invalidates_series_without_materialised_occurrences(h):
    horizon(h, 7)
    person = h.seed['people'][0]
    row = event(h, event_type(h, blocks_duties=True, default_audience={'roles': ['approver']}),
        start='2026-10-18T09:00:00+03:00', end='2026-10-18T10:00:00+03:00', repeat=repeat())
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.EventOccurrence).where(m.EventOccurrence.event_id == row['id'])) == 0
    w = h.shift(h.workspace(start='2026-10-18', end='2026-10-19'),
        start='2026-10-18T08:00:00+03:00', end='2026-10-18T12:00:00+03:00', people=[person])['workspace']
    before, revisions = h.snapshot(w), input_revisions(h)
    h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['staff'], 'role': 'approver',
        'starts_on': '2020-01-01', 'reason': 'Include future meeting attendee'})
    assert input_revisions(h) == tuple(value + 1 for value in revisions)
    current = h.snapshot(w)
    assert current['fingerprint'] != before['fingerprint']
    unavailable = lambda snapshot: any(f['code'] == 'RESTRICTION' and f['person_id'] == person
                                       for f in evaluate(snapshot)['findings'])
    assert not unavailable(before) and unavailable(current)


def test_projection_keeps_saved_timezone_after_organisation_timezone_changes(h):
    horizon(h, 7)
    event(h, event_type(h, blocks_duties=True, ends_on='2026-10-19'),
        start='2026-10-04T23:00:00+03:00', end='2026-10-05T01:00:00+03:00',
        repeat=repeat(days=[{'weekday': 6, 'start_time': '23:00', 'end_time': '01:00'}]))
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).timezone = 'UTC'
    assert len(projected(h, '2026-10-11T21:00:00+00:00', '2026-10-11T22:00:00+00:00')) == 1
    # The last overnight meeting ends after the saved department-local type cutoff, although before midnight UTC.
    assert projected(h, '2026-10-18T20:00:00+00:00', '2026-10-18T23:00:00+00:00') == []


@pytest.mark.parametrize('start,end', [
    ('2027-03-13T00:00:00-05:00', '2027-03-14T00:00:00-05:00'),
    ('2027-03-15T00:00:00-04:00', '2027-03-16T00:00:00-04:00'),
])
def test_projection_does_not_expand_nonoverlapping_days_with_nonexistent_wall_times(h, start, end):
    horizon(h, 7)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).timezone = 'America/New_York'
    event(h, event_type(h, blocks_duties=True), start='2026-10-04T02:30:00-04:00', end='2026-10-04T03:30:00-04:00',
        repeat=repeat(timezone='America/New_York', days=[{'weekday': 6, 'start_time': '02:30', 'end_time': '03:30'},
            {'weekday': 2, 'start_time': '23:00', 'end_time': '01:00'}]))
    assert projected(h, start, end) == []

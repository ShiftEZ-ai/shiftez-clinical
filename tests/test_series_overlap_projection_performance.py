"""Ordinary calendar reads skip full snapshots while retaining reservation/leave conflicts."""
import time
from datetime import date, timedelta
from random import Random

import pytest
from sqlalchemy import event, select

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.api.services import availability_patterns, planning
from apps.api.services import calendar as calendar_service
from packages.contracts import schemas as s
from packages.domain.overlap import evaluate_overlaps, per_session_overlaps
from packages.domain.time import aware
from tests.test_schedule_window import _big_month
from tests.test_series_overlap_api import absence, candidate, reviewed
from tests.test_series_reconciliation import materialise


@pytest.fixture
def ordinary_calendar_month(h, monkeypatch):
    workspace = h.workspace('Synthetic ordinary month', '2026-10-01', '2026-11-01')
    with transaction(h.app.state.engine, write=True) as db:
        extra = [m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Synthetic person {n}')
                 for n in range(147)]
        db.add_all(extra)
        db.flush()
        people = list(h.seed['people']) + [row.id for row in extra]
        db.add_all(m.Membership(department_id=h.seed['department_id'], person_id=row.id,
            category_id=h.seed['category'], starts_on=date(2026, 1, 1)) for row in extra)
        _big_month(db, workspace['id'], people, db.get(m.AssignmentType, h.seed['type']),
                   h.seed['locations'], h.seed['services'], date(2026, 10, 1), Random(23), '2026-09-30T00:00:00Z')

    def full_snapshot_is_unnecessary(*args, **kwargs):
        raise AssertionError('Ordinary calendars must not load the full domain snapshot')
    monkeypatch.setattr(snapshots, 'domain_snapshot', full_snapshot_is_unnecessary)
    client, statements, elapsed, queries, projection_cpu = h.client('coordinator'), [], [], [], []
    project = snapshots.project_series_shifts

    def measured_projection(*args):
        began = time.process_time()
        result = project(*args)
        projection_cpu.append(time.process_time() - began)
        return result
    monkeypatch.setattr(calendar_service, 'project_series_shifts', measured_projection)

    def queried(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)

    event.listen(h.app.state.engine, 'before_cursor_execute', queried)
    try:
        for _ in range(3):
            before = len(statements)
            began = time.process_time()
            response = client.get('/api/calendar?start=2026-10-01&end=2026-11-01&workspace_id=' + workspace['id'])
            elapsed.append(time.process_time() - began)
            queries.append(len(statements) - before)
            assert response.status_code == 200, response.text
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', queried)
    body = response.json()
    assert len(body['people']) == 150 and len(body['sessions']) == 496
    assert any(row.get('double_booking_conflicts') for row in body['sessions'])
    return elapsed, statements, queries, projection_cpu


def test_ordinary_calendar_batches_overlap_inputs_without_full_snapshot(ordinary_calendar_month):
    _, statements, queries, _ = ordinary_calendar_month
    print('ordinary calendar SELECT queries:', queries)
    assert max(queries) < 40, queries
    assert not any('FROM policies' in sql or 'FROM wishes' in sql for sql in statements)


@pytest.mark.cpu_budget
def test_ordinary_calendar_projection_cpu_budget(ordinary_calendar_month):
    from tests.test_scheduler_performance import _budget
    elapsed, _, _, projection_cpu = ordinary_calendar_month
    print('ordinary calendar HTTP CPU ms:', [round(value * 1000, 2) for value in elapsed])
    print('ordinary calendar projection CPU ms:', [round(value * 1000, 2) for value in projection_cpu])
    assert max(projection_cpu) < _budget(0.1), projection_cpu


@pytest.mark.parametrize('initially_absent', [False, True])
def test_ordinary_projection_matches_full_overlap_inputs_with_frozen_peer_closure(h, initially_absent):
    if initially_absent:
        leave = absence(h, day='2026-10-05')
    value = candidate(h, 'Synthetic issued peer')
    value['definition']['close_when_all_usual_unavailable'] = True
    peer = reviewed(h, value)
    issued = materialise(h, peer)
    peer_id = h.detail(issued)['shifts'][0]['id']
    if initially_absent:
        issued = h.command('coordinator', 'shift.action', {'workspace_id': issued['id'], 'shift_ids': [peer_id],
            'action': 'cancel', 'reason': 'Explicit coordinator closure'}, revision=issued['revision'])['workspace']
    h.publish(issued)
    if initially_absent:
        h.command('coordinator', 'availability.retract', {'reason': 'Synthetic change after issue'}, leave['id'], leave['revision'])
    else:
        absence(h, day='2026-10-05')
    own = h.shift(h.workspace(), '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')['workspace']
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, own['id'])
        rows = snapshots.workspace_shifts(db, workspace.id)
        data = snapshots.domain_snapshot(db, workspace, own_shifts=rows, include_fairness=False)
        expected = per_session_overlaps(data, evaluate_overlaps(data, include_fixed=True)).get(rows[0]['id'], [])
        [actual] = snapshots.project_series_shifts(db, workspace, rows)
    assert actual.get('double_booking_conflicts', []) == expected
    assert any(row['other_shift_id'] == peer_id for row in expected) is not initially_absent
    assert any(row['code'] == 'UNAVAILABLE' for row in expected) is not initially_absent


def test_ordinary_projection_trims_partly_replaced_published_sessions(h, monkeypatch):
    workspace = h.workspace('Synthetic issued ordinary sessions', '2026-10-01', '2026-11-01')
    for day in ('2026-10-05', '2026-10-12'):
        workspace = h.shift(workspace, day + 'T08:00:00+03:00', day + 'T12:00:00+03:00')['workspace']
    publication_id = h.publish(workspace)['publication_id']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Publication, publication_id).effective_until = date(2026, 10, 8)
    with transaction(h.app.state.engine) as db:
        issued = db.get(m.Workspace, workspace['id'])
        rows = snapshots.workspace_shifts(db, issued.id)
        monkeypatch.setattr(snapshots, 'domain_snapshot', lambda *args, **kwargs: pytest.fail('Full snapshot loaded'))
        actual = snapshots.project_series_shifts(db, issued, rows)
    assert [row['start'][:10] for row in actual] == ['2026-10-05']


@pytest.mark.parametrize('closed_peer', [False, True])
def test_ordinary_projection_retains_maximum_peer_and_its_complete_dst_absences(h, monkeypatch, closed_peer):
    from tests.test_series_overlap_schedule import publish_synthetic_rows

    person = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).timezone = 'America/New_York'
    created = h.shift(h.workspace('Synthetic preceding DST duty', '2026-10-31', '2026-11-03'),
        '2026-10-31T22:00:00-04:00', '2026-11-01T06:00:00-05:00')
    with transaction(h.app.state.engine, write=True) as db:
        peer = db.get(m.Shift, created['shift_id'])
        peer.end = aware(peer.start) + timedelta(minutes=s.MAX_DUTY_DURATION_MINUTES)
        for participant in db.scalars(select(m.Assignment).where(m.Assignment.shift_id == peer.id)):
            participant.end = peer.end
        if closed_peer:
            peer.cancelled, peer.cancellation_reason = True, 'Explicit synthetic closure'
            peer.series_snapshot = {'name': 'Synthetic closed DST peer', 'definition': {
                'timezone': 'America/New_York', 'default_person_ids': [person],
                'close_when_all_usual_unavailable': True}}
            for on, start, end in [('2026-10-31', '22:00', '24:00'),
                                   ('2026-11-01', '00:00', '24:00'), ('2026-11-02', '00:00', '09:00')]:
                day = date.fromisoformat(on)
                db.add(m.AvailabilityPattern(department_id=h.seed['department_id'], person_id=person,
                    weekdays=[day.weekday()], start_time=start, end_time=end,
                    starts_on=day, ends_on=day + timedelta(days=1), status='active',
                    created_by=h.seed['identities']['coordinator']))
    publish_synthetic_rows(h, created['workspace'])
    with transaction(h.app.state.engine, write=True) as db:
        db.scalar(select(m.Publication).where(
            m.Publication.workspace_id == created['workspace']['id'])).effective_until = date(2026, 11, 1)
    own = h.shift(h.workspace('Synthetic next-day DST draft', '2026-11-02', '2026-11-03'),
        '2026-11-02T00:05:00-05:00', '2026-11-02T00:10:00-05:00', seed_restricted=True)['workspace']
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, own['id'])
        rows = snapshots.workspace_shifts(db, workspace.id)
        data = snapshots.domain_snapshot(db, workspace, own_shifts=rows, include_fairness=False)
        expected = per_session_overlaps(data, evaluate_overlaps(data, include_fixed=True)).get(rows[0]['id'], [])

        def full_snapshot_is_unnecessary(*args, **kwargs):
            raise AssertionError('Ordinary projections must retain complete peer inputs without a full snapshot')

        monkeypatch.setattr(snapshots, 'domain_snapshot', full_snapshot_is_unnecessary)
        [actual] = snapshots.project_series_shifts(db, workspace, rows)
    assert actual.get('double_booking_conflicts', []) == expected
    assert {row['other_shift_id'] for row in expected if row['code'] == 'TIME_OVERLAP'} == (
        set() if closed_peer else {created['shift_id']})
    assert any(row['code'] == 'UNAVAILABLE' for row in expected) is closed_peer


def test_daily_previews_chunk_disjoint_sql_windows_and_deduplicate_long_inputs(h):
    first = date(2026, 1, 1)
    peer_day = first + timedelta(days=199)
    created = h.shift(h.workspace('Synthetic long preview peer', str(peer_day), str(peer_day + timedelta(days=2))),
        str(peer_day) + 'T08:00:00+03:00', str(peer_day) + 'T12:00:00+03:00')
    with transaction(h.app.state.engine, write=True) as db:
        peer = db.get(m.Shift, created['shift_id'])
        peer.end = aware(peer.start) + timedelta(minutes=s.MAX_DUTY_DURATION_MINUTES)
        for participant in db.scalars(select(m.Assignment).where(m.Assignment.shift_id == peer.id)):
            participant.end = peer.end
        absence = m.Availability(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
            start=peer.start, end=peer.end, kind='recorded_leave', private_reason='Synthetic long absence',
            recorded_by=h.seed['identities']['coordinator'])
        db.add(absence)
        db.flush()
        absence_id = absence.id
    items = []
    for year in (2026, 2028, 2030):
        on = first.replace(year=year)
        value = candidate(h, 'Synthetic daily preview ' + str(year))
        value.update(starts_on=str(on), ends_on=str(on + timedelta(days=366)))
        value['definition'].update(anchor=str(on), days=[
            {'weekday': weekday, 'start_time': '08:00', 'end_time': '09:00'} for weekday in range(7)])
        items.append({'value': value})
    response = h.client('admin').post('/api/series/preview', json={'items': items})
    assert response.status_code == 200, response.text
    results = response.json()['items']
    assert [len(item['occurrences']) for item in results] == [366, 366, 366]
    assert not any(item['conflicts'] for item in results[1:])
    conflicts = results[0]['conflicts']
    assert len(conflicts) == 4
    assert {row['other_shift_id'] for row in conflicts if row['code'] == 'TIME_OVERLAP'} == {created['shift_id']}
    assert {row['availability_id'] for row in conflicts if row['code'] == 'UNAVAILABLE'} == {absence_id}
    assert {row['date'] for row in conflicts} == {str(peer_day), str(peer_day + timedelta(days=1))}


@pytest.mark.parametrize('closed_peer', [False, True])
def test_disjoint_batch_preview_bounds_peer_expansion_and_deduplicates_dates(h, monkeypatch, closed_peer):
    peer_value = candidate(h, 'Synthetic ongoing overnight peer')
    peer_value['ends_on'] = None
    peer_value['definition']['close_when_all_usual_unavailable'] = closed_peer
    peer_value['definition']['days'] = [
        {'weekday': day, 'start_time': '22:00', 'end_time': '06:00'} for day in range(7)]
    typed = s.SeriesInput.model_validate(peer_value)
    with transaction(h.app.state.engine, write=True) as db:
        peer = m.RecurringSeries(department_id=h.seed['department_id'],
            **typed.model_dump(exclude={'definition'}), definition=peer_value['definition'])
        db.add(peer)
        db.flush()
        peer_id = peer.id
    weekly = h.command('coordinator', 'availability_pattern.propose', {
        'person_id': h.seed['people'][0], 'weekdays': list(range(7)), 'start_time': '03:00',
        'end_time': '04:00', 'starts_on': '2026-10-01'})['pattern']
    if closed_peer:
        for start, end in [('22:00', '24:00'), ('00:00', '06:00')]:
            h.command('coordinator', 'availability_pattern.propose', {
                'person_id': h.seed['people'][0], 'weekdays': list(range(7)),
                'start_time': start, 'end_time': end, 'starts_on': '2026-10-01'})
    expanded_days, availability_windows = [], []
    original_series, original_availability = planning.series_occurrences, availability_patterns.expand

    def bounded_series(data, template, definition, start, stop):
        if template.id == peer_id:
            assert (stop - start).days <= 8, 'Disjoint candidates must not expand the intervening decade'
            expanded_days.extend(start + timedelta(days=offset) for offset in range((stop - start).days))
        return original_series(data, template, definition, start, stop)

    def bounded_availability(db, departments, people, lo, hi, zone):
        assert (hi - lo).days <= 4, 'Recurring availability must use the same bounded candidate windows'
        availability_windows.append((lo, hi))
        return original_availability(db, departments, people, lo, hi, zone)

    monkeypatch.setattr(planning, 'series_occurrences', bounded_series)
    monkeypatch.setattr(availability_patterns, 'expand', bounded_availability)
    values = []
    days = ('2026-10-05', '2036-10-06') + (() if closed_peer else ('2026-10-05',))
    for day in days:
        start = date.fromisoformat(day)
        value = candidate(h, 'Synthetic disjoint candidate ' + str(len(values)))
        value.update(starts_on=day, ends_on=(start + timedelta(days=1)).isoformat())
        value['definition']['days'] = [{'weekday': start.weekday(), 'start_time': '02:00', 'end_time': '05:00'}]
        values.append(value)
    response = h.client('admin').post('/api/series/preview', json={'items': [{'value': value} for value in values]})
    assert response.status_code == 200, response.text
    assert len(expanded_days) <= 8 and len(expanded_days) == len(set(expanded_days))
    assert len(availability_windows) == 2
    for value, item in zip(values, response.json()['items'], strict=True):
        # The legacy closure flag never suppresses a required peer occurrence.
        [peer_conflict] = [row for row in item['conflicts'] if row['other_series_id'] == peer_id]
        assert peer_conflict['code'] == 'TIME_OVERLAP'
        assert peer_conflict['other_occurrence_on'] == (
            date.fromisoformat(value['starts_on']) - timedelta(days=1)).isoformat()
        assert any(row['code'] == 'UNAVAILABLE' and row['availability_id'] == 'pattern:' + weekly['id'] + ':' + value['starts_on']
                   for row in item['conflicts'])

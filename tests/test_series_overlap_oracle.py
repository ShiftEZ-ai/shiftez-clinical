"""Fixed-seed parity of the windowed readers against complete, all-pairs UTC inputs."""
from datetime import date, datetime, time, timedelta, timezone
from random import Random
from time import process_time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.contracts import schemas as s
from tests.test_series_overlap_api import candidate
from tests.test_series_overlap_schedule import publish_synthetic_rows


@pytest.fixture(params=[(zone, transition) for transition in ('2026-03-08', '2026-11-01')
                       for zone in ('America/New_York', 'Asia/Riyadh')])
def random_duty_overlap_cases(h, request):
    zone, transition = request.param
    began = process_time()
    rng, tz = Random(548), ZoneInfo(zone)
    utc, day = timezone.utc, timedelta(days=1)
    people = h.seed['people'][:2]
    duties, absences, items, candidates = [], [], [], []

    def instant(on, minutes=0):
        return (datetime.combine(on, time(), tz) + timedelta(minutes=minutes)).astimezone(utc)

    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).timezone = zone
        typ = record(db.get(m.AssignmentType, h.seed['type']))

    # A decade between windows must not cause a decade of stored or recurring inputs to be read.
    for index, boundary in enumerate([date.fromisoformat(transition), date.fromisoformat(transition).replace(year=2036)]):
        on = boundary + day
        old = h.workspace('Synthetic preceding publication', str(boundary - day), str(on + day))
        new = h.workspace('Synthetic replacement publication', str(boundary), str(on + day))
        value = candidate(h, 'Synthetic oracle candidate ' + str(index))
        value.update(starts_on=str(on), ends_on=str(on + day))
        value['definition'].update(timezone=zone, anchor=str(on), default_person_ids=people,
            days=[{'weekday': on.weekday(), 'start_time': '00:05', 'end_time': '00:10'}])
        typed = s.SeriesInput.model_validate(value)
        with transaction(h.app.state.engine, write=True) as db:
            series = m.RecurringSeries(department_id=h.seed['department_id'],
                **typed.model_dump(exclude={'definition'}), definition=value['definition'])
            db.add(series)
            db.flush()
            items.append({'series_id': series.id, 'expected_revision': series.revision, 'value': value})

            def duty(a, b, audience, *, definition=None, series_id=None, closed=False, eligible=True):
                workspace = old if a.astimezone(tz).date() < boundary else new
                row = m.Shift(workspace_id=workspace['id'], assignment_type_id=h.seed['type'],
                    cancelled=closed, cancellation_reason='Explicit synthetic closure' if closed else None,
                    type_revision=typ['revision'], type_snapshot=typ, location_id=h.seed['locations'][0],
                    start=a, end=b, attributes={}, series_id=series_id, series_revision=series.revision if series_id else None,
                    occurrence_on=a.astimezone(tz).date() if series_id else None,
                    series_snapshot={'name': 'Synthetic closed peer', 'definition': definition} if definition else None)
                db.add(row)
                db.flush()
                db.add(m.ShiftService(shift_id=row.id, service_id=h.seed['services'][0]))
                db.add_all(m.Assignment(shift_id=row.id, person_id=pid, start=a, end=b) for pid in audience)
                duties.append({'id': row.id, 'start': a, 'end': b, 'people': audience, 'closed': closed,
                               'usual': definition.get('default_person_ids', []) if definition else [],
                               'eligible': eligible, 'workspace_id': workspace['id']})
                return row.id

            sid = duty(instant(on, 5), instant(on, 10), people, series_id=series.id, definition=value['definition'])
            candidates.append(sid)
            for n in range(48):
                a = instant(on + rng.choice([-day, timedelta(), day]), rng.choice([0, 15, 30, 1380, 1410, 1439]))
                length = [1, s.MAX_DUTY_DURATION_MINUTES][n] if n < 2 else rng.randint(1, s.MAX_DUTY_DURATION_MINUTES)
                duty(a, a + timedelta(minutes=length), rng.sample(people, rng.randint(1, 2)))
            # This supported maximum duty starts two dates before the candidate; the old publication ends in between.
            a = instant(boundary - day, 22 * 60)
            duty(a, a + timedelta(minutes=s.MAX_DUTY_DURATION_MINUTES), people)
            # An explicit closure crosses DST; full absence alone would keep it open.
            a, b = instant(boundary, 30), instant(on, 15)
            definition = {**value['definition'], 'close_when_all_usual_unavailable': True}
            duty(a, b, people[:1], definition=definition, closed=True)
            for pid in people:
                for starts_on, start, end in [(boundary, '00:30', '00:45'), (boundary, '00:45', '24:00'), (on, '00:00', '00:15')]:
                    pattern = m.AvailabilityPattern(department_id=h.seed['department_id'], person_id=pid,
                        weekdays=[starts_on.weekday()], start_time=start, end_time=end,
                        starts_on=starts_on, ends_on=starts_on + day, status='active',
                        created_by=h.seed['identities']['coordinator'])
                    db.add(pattern)
                    db.flush()
                    minute = lambda clock: int(clock[:2]) * 60 + int(clock[3:])
                    absences.append((pid, instant(starts_on, minute(start)), instant(starts_on, minute(end)),
                                     f'pattern:{pattern.id}:{starts_on}'))
            # A replaced publication's later stored row is not in force, even when its instants overlap.
            decoy = duty(instant(on, 5), instant(on, 10), people, eligible=False)
            db.get(m.Shift, decoy).workspace_id = old['id']
        publish_synthetic_rows(h, old, sequence=index * 2 + 1)
        with transaction(h.app.state.engine, write=True) as db:
            db.scalar(select(m.Publication).where(m.Publication.workspace_id == old['id'])).effective_until = boundary
        publish_synthetic_rows(h, new, sequence=index * 2 + 2)

    for row in duties:
        if not row['closed']:
            continue
        # Independently verify the complete absence envelope on the explicitly closed peer.
        for pid in row['usual']:
            cursor = row['start']
            for a, b in sorted((a, b) for person, a, b, _ in absences if person == pid):
                if a <= cursor:
                    cursor = max(cursor, b)
            assert cursor >= row['end']

    # Independent nested loops: no domain overlap, recurrence, envelope or availability helpers.
    def oracle(focus, audience):
        result = set()
        for row in duties:
            if row['id'] not in focus or row['closed'] or not row['eligible']:
                continue
            for peer in duties:
                lo, hi = max(row['start'], peer['start']), min(row['end'], peer['end'])
                if row['id'] != peer['id'] and peer['eligible'] and not peer['closed'] and lo < hi:
                    for pid in set(row['people']) & set(peer['people']) & set(audience):
                        result.add((row['id'], 'TIME_OVERLAP', peer['id'], pid, lo, hi))
            for pid, a, b, aid in absences:
                lo, hi = max(row['start'], a), min(row['end'], b)
                if pid in row['people'] and pid in audience and lo < hi:
                    result.add((row['id'], 'UNAVAILABLE', aid, pid, lo, hi))
        return result

    def flags(rows, identities=None):
        identities = identities or {}
        return {(identities.get(row['shift_id'], row['shift_id']), row['code'],
                 row['other_shift_id'] or row['availability_id'], row['person_id'],
                 datetime.fromisoformat(row['start']).astimezone(utc), datetime.fromisoformat(row['end']).astimezone(utc))
                for row in rows}

    comparisons = []
    for item in items:
        on = date.fromisoformat(item['value']['starts_on'])
        for role, audience in [('coordinator', people), ('staff', people[:1])]:
            body = h.get(role, f'/api/schedule/sessions?start={on}&end={on + day}')
            visible = {row['id'] for row in body['shifts']}
            found = [flag for row in body['shifts'] for flag in row.get('double_booking_conflicts', [])]
            comparisons.append((flags(found), oracle(visible, audience)))
    response = h.client('admin').post('/api/series/preview', json={'items': items})
    assert response.status_code == 200, response.text
    for item, sid in zip(response.json()['items'], candidates):
        identities = {row['shift_id']: sid for row in item['conflicts']}
        comparisons.append((flags(item['conflicts'], identities), oracle({sid}, people)))
        comparisons.append((len(flags(item['conflicts'], identities)), len(item['conflicts'])))
    return comparisons, process_time() - began


def test_random_duty_windows_match_all_pairs_oracle(random_duty_overlap_cases):
    assert any(expected for _, expected in random_duty_overlap_cases[0] if isinstance(expected, set))
    for actual, expected in random_duty_overlap_cases[0]:
        assert actual == expected


@pytest.mark.cpu_budget
def test_random_duty_windows_stay_bounded(random_duty_overlap_cases):
    assert random_duty_overlap_cases[1] < 30, 'The fixed-seed property check must remain bounded'

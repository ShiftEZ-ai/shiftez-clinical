"""Calendar reads keep membership work within the requested person and programme."""
from collections import Counter
from dataclasses import replace
from datetime import date
from time import perf_counter

import pytest
from sqlalchemy import event, insert

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services import calendar


FIRST, LAST = date(2026, 10, 1), date(2026, 11, 1)


@pytest.fixture(scope='module')
def calendar_department(scale_harness):
    h = scale_harness()
    actor = Actor(h.seed['identities']['coordinator'], h.seed['department_id'], h.seed['organisation_id'],
                  'Synthetic calendar reader', frozenset({'coordinator'}), h.seed['people'][0], '')
    with transaction(h.app.state.engine, write=True) as db:
        extra = [m.Person(organisation_id=actor.organisation_id, display_name=f'Synthetic calendar person {i}')
                 for i in range(147)]
        programmes = [m.Programme(department_id=actor.department_id, name=f'Synthetic programme {i}',
                                 starts_on=date(2020, 1, 1)) for i in range(10)]
        db.add_all([*extra, *programmes])
        db.flush()
        ids = [*h.seed['people'], *(p.id for p in extra)]
        db.execute(insert(m.Membership), [dict(department_id=actor.department_id, person_id=p.id,
            category_id=h.seed['category'], starts_on=FIRST, ends_on=LAST) for p in extra])
        memberships = [dict(department_id=actor.department_id, person_id=pid, programme_id=p.id,
                            starts_on=FIRST, ends_on=LAST) for p in programmes for pid in ids]
        # Two adjacent partial memberships do not individually confirm the whole academic block.
        memberships[0]['ends_on'] = date(2026, 10, 15)
        memberships.append({**memberships[0], 'starts_on': date(2026, 10, 15), 'ends_on': LAST})
        # Multiple overlapping facts for one person must never inflate the confirmation count.
        memberships.append(dict(memberships[1]))
        db.execute(insert(m.ProgrammeMembership), memberships)
        entries = [m.ProgrammeCalendarEntry(department_id=actor.department_id, programme_id=p.id,
            name=f'Synthetic academic date {i}', kind='notice', starts_on=FIRST, ends_on=LAST)
            for p in programmes for i in range(50)]
        db.add_all(entries)
        db.flush()
        expected = [{**record(entry), 'programme_name': p.name, 'confirmed_members': len(ids)}
                    for p in programmes for entry in entries if entry.programme_id == p.id]
    return h, actor, sorted(expected, key=lambda r: (r['starts_on'], r['id']))


class CountedMembership:
    def __init__(self, row, work):
        self.row, self.work = row, work

    def __getattr__(self, name):
        self.work[name] += 1
        return getattr(self.row, name)


class CountedSession:
    def __init__(self, db):
        self.db, self.work = db, Counter()

    def __getattr__(self, name):
        return getattr(self.db, name)

    def scalars(self, query):
        rows = self.db.scalars(query)
        if query.column_descriptions[0]['expr'] is m.ProgrammeMembership:
            return [CountedMembership(row, self.work) for row in rows]
        return rows


def test_academic_calendar_indexes_memberships_once_per_request(calendar_department):
    h, actor, expected = calendar_department
    with transaction(h.app.state.engine) as db:
        counted = CountedSession(db)
        began = perf_counter()
        result = calendar.calendar(counted, actor, FIRST, LAST)
        elapsed = perf_counter() - began
    assert result['programme_calendar'] == expected
    assert len(result['people']) == 150 and len(expected) == 500
    print('academic calendar', {'seconds': elapsed, 'membership_reads': dict(counted.work)})
    assert counted.work['programme_id'] <= 2 * 1502, 'Index programme membership once, not once per academic date'


@pytest.mark.parametrize('role', ['staff', 'coordinator', 'auditor'])
def test_person_calendar_does_not_hydrate_peer_memberships(calendar_department, role):
    h, actor, expected = calendar_department
    actor = replace(actor, roles=frozenset({role}))
    loaded = Counter()

    def hydrated(row, _):
        loaded[type(row).__name__] += 1

    for model in (m.Membership, m.ProgrammeMembership):
        event.listen(model, 'load', hydrated)
    try:
        with transaction(h.app.state.engine) as db:
            result = calendar.calendar(db, actor, FIRST, LAST, person_id=actor.person_id)
    finally:
        for model in (m.Membership, m.ProgrammeMembership):
            event.remove(model, 'load', hydrated)
    if role == 'staff':
        expected = [{key: value for key, value in row.items() if key != 'confirmed_members'} for row in expected]
    else:
        expected = [{**row, 'confirmed_members': int(row['confirmed_members'] == 150)} for row in expected]
    assert result['programme_calendar'] == expected
    assert len(result['people']) == 1 and result['people'][0]['id'] == actor.person_id
    print('personal calendar', role, dict(loaded))
    assert loaded['Membership'] <= 2 and loaded['ProgrammeMembership'] <= 11


@pytest.mark.postgres
def test_postgres_calendar_membership_projection(pg):
    department = calendar_department.__wrapped__(lambda: pg)
    test_academic_calendar_indexes_memberships_once_per_request(department)
    for role in ('staff', 'coordinator', 'auditor'):
        test_person_calendar_does_not_hydrate_peer_memberships(department, role)

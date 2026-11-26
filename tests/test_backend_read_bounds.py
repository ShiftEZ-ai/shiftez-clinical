"""Read budgets stay bounded by the requested period and source, not staff/history."""
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import time

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services import calendar, fairness, people, schedule_view
from tests.test_backend_scale import measure
from tests.test_performance_budget import python_budget


FIRST, LAST = date(2026, 10, 1), date(2026, 11, 1)


@pytest.fixture(scope='module')
def read_department(scale_harness):
    h = scale_harness()
    actor = Actor(h.seed['identities']['coordinator'], h.seed['department_id'], h.seed['organisation_id'],
                  'Synthetic read viewer', frozenset({'coordinator'}), h.seed['people'][0], '')
    with transaction(h.app.state.engine, write=True) as db:
        extra = [m.Person(organisation_id=actor.organisation_id, display_name=f'Synthetic reader {i}') for i in range(147)]
        programme = m.Programme(department_id=actor.department_id, name='Synthetic programme', starts_on=date(2020, 1, 1))
        rotation_type = m.RotationType(department_id=actor.department_id, name='Synthetic attachment', starts_on=date(2020, 1, 1))
        services = [m.Service(department_id=actor.department_id, name=f'Synthetic clinic {i}', starts_on=date(2020, 1, 1))
                    for i in range(50)]
        db.add_all([*extra, programme, rotation_type, *services])
        db.flush()
        seeded_people = h.seed['people']
        ids = [*seeded_people, *(p.id for p in extra)]
        cohort = m.Cohort(department_id=actor.department_id, programme_id=programme.id, name='Synthetic cohort',
                          starts_on=date(2020, 1, 1), ends_on=date(2028, 1, 1))
        db.add(cohort)
        db.flush()
        capability = db.scalar(select(m.Capability))
        additions = []
        for i, pid in enumerate(ids):
            for year in range(2022, 2027):
                span = dict(department_id=actor.department_id, person_id=pid,
                            starts_on=date(year, 1, 1), ends_on=date(year + 1, 1, 1))
                additions.extend([m.Membership(**span, category_id=h.seed['category']),
                    m.ProgrammeMembership(**span, programme_id=programme.id)])
                # The seed already supplies current eligibilities and grants for its people.
                if year != FIRST.year or pid not in seeded_people:
                    additions.extend([m.Eligibility(**span, service_id=services[i % len(services)].id),
                        m.CapabilityGrant(**span, capability_id=capability.id, granted_by=actor.identity_id, evidence='Synthetic')])
                for month in range(1, 13):
                    additions.append(m.Rotation(**{**span, 'starts_on': date(year, month, 1),
                        'ends_on': date(year + month // 12, month % 12 + 1, 1)},
                        cohort_id=cohort.id, service_id=services[i % len(services)].id, rotation_type_id=rotation_type.id))
        for start, end in ((date(2026, 9, 1), FIRST), (LAST, date(2026, 12, 1))):
            span = dict(department_id=actor.department_id, person_id=ids[0], starts_on=start, ends_on=end)
            additions.extend([m.Membership(**span, category_id=h.seed['category']),
                m.ProgrammeMembership(**span, programme_id=programme.id),
                m.Eligibility(**span, service_id=services[0].id),
                m.CapabilityGrant(**span, capability_id=capability.id, granted_by=actor.identity_id, evidence='Boundary'),
                m.Rotation(**span, cohort_id=cohort.id, service_id=services[0].id, rotation_type_id=rotation_type.id)])
        additions.extend(m.ProgrammeCalendarEntry(department_id=actor.department_id, programme_id=programme.id,
            name=f'Synthetic academic date {i}', kind='notice', starts_on=FIRST, ends_on=LAST) for i in range(50))
        workspace = m.Workspace(department_id=actor.department_id, name='Synthetic source', starts_on=FIRST,
                                ends_on=LAST, status='draft', created_by=actor.identity_id, last_edited_by=actor.identity_id)
        db.add(workspace)
        db.flush()
        typ = db.get(m.AssignmentType, h.seed['type'])
        shifts = []
        for i in range(500):
            start = datetime(2026, 10, 1 + i % 30, 8, tzinfo=timezone.utc)
            shifts.append(m.Shift(id=m.uid(), workspace_id=workspace.id, assignment_type_id=typ.id,
                type_revision=typ.revision, type_snapshot=record(typ), location_id=h.seed['locations'][0],
                start=start, end=start + timedelta(hours=4)))
        db.add_all(shifts)
        db.flush()
        additions.extend(m.Assignment(shift_id=row.id, person_id=ids[i % len(ids)], start=row.start, end=row.end)
                         for i, row in enumerate(shifts))
        db.add_all(additions)
        source_id = workspace.id
    return h, actor, ids, source_id


@pytest.fixture(scope='module')
def dated_read_measurement(read_department):
    h, actor, ids, _ = read_department
    began = time.process_time()
    with transaction(h.app.state.engine) as db:
        data = fairness.department_data(db, actor, FIRST, LAST)
        with pytest.MonkeyPatch.context() as patch:
            original = people.project_people
            loaded = {}
            def projected(value, day):
                loaded.update({key: len(value[key]) for key in ('memberships', 'programme_memberships', 'rotations')})
                return original(value, day)
            patch.setattr(people, 'project_people', projected)
            current = people.current_people(db, actor, FIRST, person_ids=ids)
    elapsed = time.process_time() - began
    print('dated reads', {'cpu_seconds': elapsed, 'fairness_rows': {
        key: len(data[key]) for key in ('memberships', 'grants', 'programme_memberships', 'rotations')}, 'people_rows': loaded})
    return data, loaded, current, elapsed


def test_dated_readers_exclude_five_year_history_and_touching_boundaries(dated_read_measurement):
    data, loaded, current, _ = dated_read_measurement
    for key in ('memberships', 'grants', 'programme_memberships', 'rotations'):
        assert len(data[key]) <= 156, (key, len(data[key]))
        assert all(row['starts_on'] < LAST.isoformat() and (not row.get('ends_on') or row['ends_on'] > FIRST.isoformat())
                   for row in data[key])
    assert all(count <= 153 for count in loaded.values()), loaded
    assert current['active_people'] == 150 and current['people_without_current_rank'] == 0
    assert all(row['current_rotation_type_ids'] and row['current_programme_ids'] for row in current['people'].values())


@pytest.fixture
def coordinator_read_client(read_department, monkeypatch):
    h, _, _, _ = read_department
    monkeypatch.setattr(m, 'clock_now', lambda: datetime(2026, 10, 2, 12, tzinfo=timezone.utc))
    return h.client('coordinator')


@pytest.mark.parametrize('path,person_rows', [
    ('/api/catalogues?include=people', True),
    ('/api/directory', False),
    ('/api/people/summary', False),
])
def test_people_reads_use_one_person_query(read_department, coordinator_read_client, path, person_rows):
    h, _, ids, _ = read_department
    statements, loaded = [], []

    def sql(_conn, _cursor, statement, _parameters, _context, _many):
        if ' from people ' in ' '.join(statement.lower().split()):
            statements.append(statement)

    def row(_session, instance):
        if isinstance(instance, m.Person):
            loaded.append(instance.id)

    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    event.listen(Session, 'loaded_as_persistent', row)
    try:
        label = path.rsplit('/', 1)[-1].split('?')[0]
        response, _ = measure(h, label, lambda: coordinator_read_client.get(path))
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
        event.remove(Session, 'loaded_as_persistent', row)
    assert response.status_code == 200
    if path.endswith('/summary'):
        assert response.json() == {'active_people': len(ids), 'people_without_current_rank': 0}
    else:
        assert {person['id'] for person in response.json()['people']} == set(ids)
    assert (len(statements), len(loaded)) == (1, len(ids) if person_rows else 0)


def test_rule_reach_reads_only_period_facts_and_live_rules(read_department, coordinator_read_client):
    h, actor, ids, _ = read_department
    rule = {'rule': {'kind': 'max_hours', 'hours': 48}, 'scope': {}, 'severity': 'mandatory'}
    with transaction(h.app.state.engine, write=True) as db:
        active = m.Policy(department_id=actor.department_id, kind='max_hours', name='Synthetic current rule',
            definition=rule, starts_on=FIRST, status='active', owner='', source='', created_by=actor.identity_id)
        db.add(active)
        db.add_all(m.Policy(department_id=actor.department_id, kind='max_hours', name='Synthetic retired rule',
            definition=rule, starts_on=date(2022, 1, 1), status='retired', owner='', source='',
            created_by=actor.identity_id) for _ in range(50))
        db.flush()
        active_id = active.id
        live_ids = set(db.scalars(select(m.Policy.id).where(m.Policy.status.in_(('active', 'proposed')))))

    tables = {cls.__tablename__ for cls in
              (m.Policy, m.Membership, m.Eligibility, m.CapabilityGrant, m.ProgrammeMembership, m.Rotation)}
    statements, loaded = Counter(), Counter()

    def sql(_conn, _cursor, statement, _parameters, _context, _many):
        statement = ' '.join(statement.lower().split())
        statements.update(table for table in tables if f' from {table} ' in statement)

    def row(_session, instance):
        if instance.__tablename__ in tables:
            loaded[instance.__tablename__] += 1

    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    event.listen(Session, 'loaded_as_persistent', row)
    try:
        response, _ = measure(h, 'rule reach', lambda: coordinator_read_client.get(f'/api/policies/reach?day={FIRST.isoformat()}'))
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
        event.remove(Session, 'loaded_as_persistent', row)
    assert response.status_code == 200
    assert set(response.json()['reach']) == live_ids
    assert response.json()['reach'][active_id] == len(ids)
    assert statements == dict.fromkeys(tables, 1)
    assert loaded['policies'] == len(live_ids)
    assert all(len(ids) <= loaded[table] <= len(ids) + 3 for table in tables - {'policies'}), loaded


@pytest.mark.cpu_budget
def test_dated_reader_cpu_budget(dated_read_measurement):
    _, _, _, elapsed = dated_read_measurement
    assert elapsed < python_budget(1)


@pytest.fixture(scope='module')
def source_read_measurement(read_department):
    h, actor, ids, source_id = read_department
    with transaction(h.app.state.engine) as db, pytest.MonkeyPatch.context() as monkeypatch:
        source = db.get(m.Workspace, source_id)
        monkeypatch.setattr(schedule_view, 'sources', lambda *a, **k: ('UTC', [(
            {'id': source_id, 'kind': 'draft'}, source, (source.revision, source.content_revision))]))
        monkeypatch.setattr(schedule_view, 'date_markers', lambda *a, **k: {})
        monkeypatch.setattr(schedule_view, 'schedule_occurrences', lambda *a, **k: [])
        monkeypatch.setattr(schedule_view, '_summaries', {})
        loaded, original = [], schedule_view.summary
        def counted(*args, **kwargs):
            loaded.append(1)
            return original(*args, **kwargs)
        monkeypatch.setattr(schedule_view, 'summary', counted)
        ownership = dict(db.execute(select(m.Assignment.shift_id, m.Assignment.person_id)).all())
        began = time.process_time()
        for pid in ids + ids[:1]:
            viewer = replace(actor, person_id=pid, roles=frozenset({'staff'}))
            _, body = schedule_view.days(db, viewer, FIRST, LAST)
            duties = [duty for entries in body['days'].values() for counts in entries for duty in counts['duties']]
            assert duties and all(ownership[duty['shift_id']] == pid for duty in duties)
            assert sum(counts['sessions'] for entries in body['days'].values() for counts in entries) == len(duties)
        elapsed = time.process_time() - began
        result = len(loaded), len(schedule_view._summaries), elapsed
        print('source cache', {'loads': result[0], 'entries': result[1], 'cpu_seconds': elapsed})
        return result


def test_source_cache_reused_across_150_staff_without_exposing_peer_duties(source_read_measurement):
    loads, entries, _ = source_read_measurement
    assert loads == entries == 1


@pytest.mark.cpu_budget
def test_source_projection_cpu_budget(source_read_measurement):
    _, _, elapsed = source_read_measurement
    assert elapsed < python_budget(0.5)


@pytest.fixture(scope='module')
def calendar_read_measurement(read_department):
    h, actor, _, _ = read_department
    selects = Counter()
    def sql(conn, cursor, statement, parameters, context, many):
        text = ' '.join(statement.lower().split())
        for table in ('services', 'rotation_types', 'programmes'):
            if f' from {table} ' in text:
                selects[table] += 1
    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    began = time.process_time()
    try:
        with transaction(h.app.state.engine) as db:
            result = calendar.calendar(db, actor, FIRST, LAST)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    elapsed = time.process_time() - began
    print('calendar catalogue reads', {'queries': dict(selects), 'cpu_seconds': elapsed})
    return result, selects, elapsed


def test_calendar_batches_catalogue_labels_without_per_row_gets(calendar_read_measurement):
    result, selects, _ = calendar_read_measurement
    rotations = [row for row in result['events'] if row['kind'] == 'rotation']
    assert len(rotations) == 150 and {row['label'] for row in rotations} == {'Synthetic attachment'}
    assert len(result['programme_calendar']) == 50
    assert {row['programme_name'] for row in result['programme_calendar']} == {'Synthetic programme'}
    assert selects == Counter(services=1, rotation_types=1, programmes=1)


@pytest.mark.cpu_budget
def test_calendar_projection_cpu_budget(calendar_read_measurement):
    _, _, elapsed = calendar_read_measurement
    assert elapsed < python_budget(0.5)


def test_workload_keeps_dated_facts_for_quota_context_outside_displayed_period(h):
    person = h.seed['people'][0]
    programme = h.configure('programmes', {'name': 'Later synthetic programme', 'starts_on': '2026-01-01'})['record']
    h.configure('programme_memberships', {'person_id': person, 'programme_id': programme['id'],
        'starts_on': '2026-11-01', 'ends_on': '2026-12-01'}, role='coordinator')
    h.publish(h.shift(h.workspace())['workspace'])
    later = h.workspace('Later quota work', '2026-11-01', '2026-11-08')
    for day in (5, 6):
        later = h.shift(later, f'2026-11-{day:02}T08:00:00+03:00',
                        f'2026-11-{day:02}T12:00:00+03:00')['workspace']
    h.publish(later)
    quota = h.policy({'kind': 'block_quota', 'starts_on': '2026-09-01', 'ends_on': '2026-12-01',
        'metric': 'duties', 'comparison': 'maximum', 'amount': 1},
        {'person_id': person, 'programme_id': programme['id']}, severity='advisory')
    result = h.get('staff', f'/api/people/{person}/workload?on=2026-10-06')
    month = next(row for row in result['periods'] if row['key'] == 'this_month')
    report = next(row for row in month['quotas'] if row['policy_id'] == quota['id'])
    assert (report['actual'], report['status'], month['complete']) == (2, 'above_maximum', True)

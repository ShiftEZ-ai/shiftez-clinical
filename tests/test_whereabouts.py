"""One-line timeline behavior, source boundaries and deterministic database-work budgets."""
from collections import Counter
from datetime import date, datetime, timedelta
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services.timeline import list_data
from packages.contracts import schemas as s
from packages.domain.quotas import METRIC_VERSION
from packages.domain.time import local_midnight, now as clock_now
from packages.domain.whereabouts import project, short_label
from tests.test_timeline_data import actor_for

START, END = date(2026, 10, 1), date(2026, 11, 1)
PATH = '/api/timeline?start=2026-10-01&end=2026-11-01&bucket=month&layers=none'


@pytest.mark.parametrize('name,explicit,academic,want', [
    ('Atrium area', '', False, 'AA'), ('Zinc zone', 'ZZ', False, 'ZZ'),
    ('Academic block', '', True, 'Acad'), ('Conference', '', False, 'Conf'),
    ('Sick', '', False, 'Sick'), ('Study', '', False, 'Study'), ('Ophthalmology', '', False, 'Opht'),
    # One word stays whole up to 8 letters: Annual, never "Annu" (TIMELINE-UX).
    ('Annual', '', False, 'Annual'), ('Business', '', False, 'Business'), ('Professional', '', False, 'Prof')])
def test_short_labels(name, explicit, academic, want):
    assert short_label(name, explicit, academic=academic) == want


def fixture(h, *, source='published', people=None, starts=START, ends=END):
    people = people or h.seed['people'][:2]
    dep, author = h.seed['department_id'], h.seed['identities']['coordinator']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, dep).leave_reason_display = 'type'
        area = m.Tag(department_id=dep, name='Field medicine', short_name='FM', labels=['services'], colour='teal')
        leave_type = m.LeaveType(department_id=dep, name='Study', short_code='ST', short_name='Study', colour='blue')
        workspace = m.Workspace(department_id=dep, name='Synthetic October rota', starts_on=starts,
            ends_on=ends, status='draft', created_by=author, last_edited_by=author)
        db.add_all([area, leave_type, workspace]); db.flush()
        for sid in h.seed['services']:
            db.add(m.TagMember(department_id=dep, tag_id=area.id, kind='services', member_id=sid))
        typ = db.get(m.AssignmentType, h.seed['type'])
        for offset in (3, 10, 17, 24):
            day = starts + timedelta(days=offset)
            if day >= ends:
                continue
            lo = local_midnight(day, 'Asia/Riyadh') + timedelta(hours=8)
            shift = m.Shift(workspace_id=workspace.id, assignment_type_id=typ.id, type_revision=typ.revision,
                type_snapshot={'name': typ.name, 'definition': typ.definition}, location_id=h.seed['locations'][0],
                start=lo, end=lo + timedelta(hours=4))
            db.add(shift); db.flush()
            for sid in h.seed['services']:
                db.add(m.ShiftService(shift_id=shift.id, service_id=sid))
            for pid in people:
                db.add(m.Assignment(shift_id=shift.id, person_id=pid, start=lo, end=lo + timedelta(hours=4)))
        db.flush()
        if source == 'published':
            workspace.status = 'published'
            db.add(m.Publication(department_id=dep, workspace_id=workspace.id, sequence=1, starts_on=starts,
                ends_on=ends, published_by=author, snapshot={}, validation={'contracts': {'block_quota': METRIC_VERSION, 'programmes': 'programmes-v1'}}, fingerprint='0' * 64))
        lo = local_midnight(starts + timedelta(days=10), 'Asia/Riyadh')
        db.add(m.Availability(person_id=people[0], department_id=dep, start=lo, end=lo+timedelta(days=1),
            kind='recorded_leave', leave_type_id=leave_type.id, private_reason='PRIVATE REASON NEVER RETURN', recorded_by=author))
        db.flush()
        return {'area': area.id, 'leave': leave_type.id, 'workspace': workspace.id, 'people': people}


def test_projection_contract_opt_in_counts_and_hide_fallthrough(h, monkeypatch):
    ids = fixture(h)
    params = '&person_ids=' + ids['people'][0]
    body = h.get('coordinator', PATH + params)
    s.TimelineResponse.model_validate(body)
    person, = body['people']
    assert person['items'] == []
    assert person['areas'] == [{'id': ids['area'], 'label': 'Field medicine', 'short_name': 'FM', 'count': 4}]
    segments = person['segments']
    assert [row['kind'] for row in segments] == ['work', 'leave', 'work']
    assert segments[1]['starts_on'] == '2026-10-11' and segments[1]['ends_on'] == '2026-10-12'
    assert segments[1]['leave_type_id'] == ids['leave']
    assert segments[1]['short_label'] == 'Study'
    assert segments[0]['areas'][0]['colour'] == 'teal'
    assert all(row['source'] == 'published' and row['rota'] == 'Synthetic October rota' for row in segments)
    assert [sum(c['count'] for c in row['clinics']) for row in segments] == [2, 0, 4]
    assert 'PRIVATE REASON' not in json.dumps(body)
    single = h.get('coordinator', f"/api/people/{ids['people'][0]}/timeline?start=2026-10-01&end=2026-11-01&bucket=month&layers=")
    assert single['people'] == body['people']
    for hide in ('hide=leave', 'hide_leave_types=' + ids['leave']):
        rows = h.get('coordinator', PATH + params + '&' + hide)['people'][0]['segments']
        assert len(rows) == 1 and rows[0]['kind'] == 'work'
        assert [c['count'] for c in rows[0]['clinics']] == [4, 4]
    from apps.api.services import whereabouts
    monkeypatch.setattr(whereabouts, 'projection', lambda *a, **kw: pytest.fail('ordinary timeline projected work'))
    ordinary = h.get('coordinator', '/api/timeline?start=2026-10-01&end=2026-11-01&layers=none')
    assert all('segments' not in row and 'areas' not in row for row in ordinary['people'])


def test_outside_segments_are_server_owned_and_reasoned(h):
    start = clock_now().date() + timedelta(days=3)  # the one clock (tests/test_clock_guard.py)
    end = start + timedelta(days=31)
    with transaction(h.app.state.engine, write=True) as db:
        dep, org = h.seed['department_id'], h.seed['organisation_id']
        rank = db.scalar(select(m.Category).where(m.Category.department_id == dep))
        person = m.Person(organisation_id=org, display_name='Synthetic membership boundaries', short_code='SMB')
        db.add(person); db.flush()
        db.add_all([
            m.Membership(department_id=dep, person_id=person.id, category_id=rank.id,
                         starts_on=start + timedelta(days=3), ends_on=start + timedelta(days=5)),
            m.Membership(department_id=dep, person_id=person.id, category_id=rank.id,
                         starts_on=start + timedelta(days=7), ends_on=start + timedelta(days=9)),
        ])
    body = h.get('coordinator', f'/api/timeline?start={start}&end={end}&bucket=day&hide=work&layers=rank&person_ids=' + person.id)
    s.TimelineResponse.model_validate(body)
    outside = [row for row in body['people'][0]['segments'] if row['kind'] == 'outside']
    assert [(row['starts_on'], row['ends_on'], row['reason']) for row in outside] == [
        (start.isoformat(), (start + timedelta(days=3)).isoformat(), 'not_started'),
        ((start + timedelta(days=5)).isoformat(), (start + timedelta(days=7)).isoformat(), 'gap'),
        ((start + timedelta(days=9)).isoformat(), end.isoformat(), 'left')]


def test_source_selection_drafts_and_usual_only_in_future_rota_free_months(h):
    ids = fixture(h, source='draft')
    with transaction(h.app.state.engine, write=True) as db:
        typ = db.get(m.AssignmentType, h.seed['type'])
        db.add(m.RecurringSeries(department_id=h.seed['department_id'], name='Usual synthetic work',
            starts_on=date(2026, 1, 1), ends_on=date(2027, 2, 1), assignment_type_id=typ.id,
            location_id=h.seed['locations'][0], definition={'anchor': '2026-01-05', 'timezone': 'Asia/Riyadh',
                'days': [{'weekday': 0, 'start_time': '08:00', 'end_time': '12:00'}],
                'service_ids': h.seed['services'][:1], 'default_person_ids': ids['people']}))
    rows = h.get('coordinator', PATH + '&hide=leave')['people']
    assert rows[0]['segments'][0]['source'] == 'draft'
    assert all(not row['segments'] for row in h.get('coordinator', PATH + '&source=published&hide=leave')['people'])
    future = h.get('coordinator', PATH.replace('2026-10-01', '2026-11-01').replace('end=2026-11-01', 'end=2026-12-01'))
    worked = [row for row in future['people'] if row['areas']]
    assert len(worked) == 2
    assert worked[0]['segments'][0]['source'] == 'usual'
    assert all(area['count'] == 5 for row in worked for area in row['areas'])


def test_works_in_area_filter_before_paging(h):
    ids = fixture(h)
    tree = {'conditions': [{'field': 'works_in_area', 'op': 'any', 'values': [ids['area']]}]}
    response = h.client('coordinator').get('/api/timeline', params={'start': str(START), 'end': str(END), 'bucket': 'month', 'layers': 'none', **{'filter': json.dumps({'conditions': tree}), 'limit': 1}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['total'] == 2 and len(body['people']) == 1 and body['has_more']
    assert body['people'][0]['id'] in ids['people']


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_all_role_visibility_and_leave_disclosure(h, role):
    ids = fixture(h)
    with transaction(h.app.state.engine) as db:
        base = actor_for(h, db)
        actor = Actor(base.identity_id, base.department_id, base.organisation_id, 'Synthetic reader',
                      frozenset({role}), ids['people'][0 if role == 'staff' else 1], 'test')
        if role not in s.TEAM_READER_ROLES and role != 'staff':
            with pytest.raises(Problem):
                list_data(db, actor, START, END, bucket='month', layers='none')
            return
        body = list_data(db, actor, START, END, bucket='month', layers='none')
        if role == 'staff':
            assert [p['id'] for p in body['people']] == ids['people'][:1]
        else:
            assert set(ids['people']) <= {p['id'] for p in body['people']}
        assert 'PRIVATE REASON' not in json.dumps(body, default=str)
        db.get(m.Department, base.department_id).leave_reason_display = 'none'
        hidden = list_data(db, actor, START, END, bucket='month', layers='none')
        leaves = [seg for p in hidden['people'] for seg in p['segments'] if seg['kind'] == 'leave']
        assert all(seg['leave_type_id'] is None for seg in leaves) if role != 'staff' else leaves[0]['leave_type'] == 'Study'
        filtered = list_data(db, actor, START, END, bucket='month', layers='none', hide_leave_types=ids['leave'])
        filtered_leaves = [seg for p in filtered['people'] for seg in p['segments'] if seg['kind'] == 'leave']
        assert len(filtered_leaves) == (0 if role == 'staff' else len(leaves))


def test_precedence_exact_short_spans_and_segment_local_counts():
    start, end = date(2026, 10, 1), date(2026, 10, 9)
    area = {'id': 'area', 'label': 'Field medicine', 'short_name': 'FM', 'colour': None}
    work = {start+timedelta(days=i): {'areas': Counter(area=1), 'clinics': Counter(clinic=1)} for i in range(8)}
    spans = [{'id': str(i), 'kind': kind, 'label': kind, 'short_label': kind,
              'starts_on': start+timedelta(days=i), 'ends_on': start+timedelta(days=7)}
             for i, kind in enumerate(('home', 'away', 'protected', 'academic', 'leave'))]
    result = project(start, end, 'month', work, spans, {'area': area}, {'clinic': {'label': 'Clinic'}}, {})
    assert [s['kind'] for s in result['segments']] == ['work', 'away', 'protected', 'academic', 'leave', 'work']
    assert result['segments'][0]['clinics'][0]['count'] == result['segments'][-1]['clinics'][0]['count'] == 1
    outside = project(start, end, 'month', work,
                      [{'id': 'outside', 'kind': 'outside', 'label': 'Out', 'short_label': 'Out',
                        'starts_on': start, 'ends_on': start + timedelta(days=3)},
                       {'id': 'leave', 'kind': 'leave', 'label': 'Leave', 'short_label': 'Leave',
                        'starts_on': start + timedelta(days=1), 'ends_on': start + timedelta(days=3)}],
                      {'area': area}, {'clinic': {'label': 'Clinic'}}, {})
    assert outside['segments'][0]['kind'] == 'outside'


def test_one_stretch_spans_the_rotas_it_crosses_and_names_them_all():
    """Consecutive periods that read the same are one stretch even when each comes from its own rota (TIMELINE-UX:
    "FM, FM, FM" chips became one bar); the source stays part of a stretch, so published and draft never merge."""
    start, end = date(2026, 10, 5), date(2026, 10, 19)
    area = {'id': 'area', 'label': 'Field medicine', 'short_name': 'FM', 'colour': None}
    work = {start + timedelta(days=i): {'areas': Counter(area=1), 'clinics': Counter(clinic=1)} for i in range(14)}
    roster = {start + timedelta(days=i): ('published', 'Rota A' if i < 7 else 'Rota B') for i in range(14)}
    one, = project(start, end, 'week', work, [], {'area': area}, {'clinic': {'label': 'Clinic'}}, roster)['segments']
    assert (one['starts_on'], one['ends_on'], one['rota'], one['rotas']) == (start, end, 'Rota A', ['Rota A', 'Rota B'])
    assert one['clinics'] == [{'id': 'clinic', 'label': 'Clinic', 'count': 14}]
    roster.update({start + timedelta(days=i): ('draft', 'Rota B') for i in range(7, 14)})
    split = project(start, end, 'week', work, [], {'area': area}, {'clinic': {'label': 'Clinic'}}, roster)['segments']
    assert [(row['source'], row['rota']) for row in split] == [('published', 'Rota A'), ('draft', 'Rota B')]


@pytest.mark.parametrize('share,most,want', [(25, 2, 'A · B'), (50, 2, 'A'), (25, 1, 'A'), (10, 3, 'A · B · C')])
def test_area_share_and_most_areas_are_settings(share, most, want):
    start, end = date(2026, 10, 1), date(2026, 11, 1)
    areas = {k: {'id': k, 'label': k, 'short_name': k, 'colour': None} for k in 'ABC'}
    work = {start: {'areas': Counter(A=6, B=3, C=1), 'clinics': Counter(), 'duties': 10}}
    rows = project(start, end, 'month', work, [], areas, {}, {}, area_share=share, max_areas=most)['segments']
    assert {row['short_label'] for row in rows} == {want}


def test_two_areas_read_in_the_departments_area_order_whichever_leads():
    """The same two areas always read in one order (name order, as the key and hues): "AA · ZZ" in a month ZZ led too."""
    start, end = date(2026, 10, 1), date(2026, 12, 1)
    areas = {'uc': {'id': 'uc', 'label': 'Urgent Cover', 'short_name': 'ZZ', 'colour': None},
             'fm': {'id': 'fm', 'label': 'Field Medicine', 'short_name': 'AA', 'colour': None}}
    work = {date(2026, 10, 5): {'areas': Counter(uc=6, fm=4), 'clinics': Counter(), 'duties': 10},
            date(2026, 11, 5): {'areas': Counter(fm=6, uc=4), 'clinics': Counter(), 'duties': 10}}
    rows = project(start, end, 'month', work, [], areas, {}, {})['segments']
    assert [row['short_label'] for row in rows] == ['AA · ZZ']  # one stretch: both months read the same
    assert [a['id'] for a in rows[0]['areas']] == ['fm', 'uc']


def test_the_departments_whereabouts_settings_reach_the_projection(h, monkeypatch):
    ids = fixture(h)
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        department.display_settings = {**(department.display_settings or {}), 'whereabouts': {'area_share': 60, 'max_areas': 1}}
    from apps.api.services import whereabouts
    seen, real = [], whereabouts.project
    monkeypatch.setattr(whereabouts, 'project', lambda *a, **kw: seen.append(kw) or real(*a, **kw))
    h.get('coordinator', PATH + '&person_ids=' + ids['people'][0])
    assert seen and all((kw['area_share'], kw['max_areas']) == (60, 1) for kw in seen)
    assert s.WhereaboutsSettings() == s.WhereaboutsSettings(area_share=25, max_areas=2)


def test_academic_protected_weekdays_rotation_and_hide(h):
    from tests.test_timeline_data import timeline_records
    ids = timeline_records(h)
    # The synthetic academic block is narrowed, leaving later Monday protection and rotation visible.
    with transaction(h.app.state.engine, write=True) as db:
        academic = db.scalar(select(m.ProgrammeCalendarEntry).where(m.ProgrammeCalendarEntry.department_id == h.seed['department_id']))
        academic.starts_on, academic.ends_on = date(2026, 10, 5), date(2026, 10, 6)
        fact = db.scalar(select(m.SchedulingFact).where(m.SchedulingFact.kind == 'protected_time'))
        # Keep the approved line; remove calendar membership scoping for this personal protected-time fixture.
        fact.calendar_entry_id = None
        rotation = db.get(m.Rotation, ids['rotation'])
        rotation.department_available = False
    path = PATH + '&person_ids=' + ids['person'] + '&hide=leave'
    rows = h.get('coordinator', path)['people'][0]['segments']
    assert {'academic', 'protected', 'away'} <= {row['kind'] for row in rows}, rows
    protected = [row for row in rows if row['kind'] == 'protected']
    assert all(date.fromisoformat(row['starts_on']).weekday() == 0 and
               date.fromisoformat(row['ends_on']) - date.fromisoformat(row['starts_on']) == timedelta(days=1) for row in protected)
    rows = h.get('coordinator', path + ',academic')['people'][0]['segments']
    assert {row['kind'] for row in rows} == {'away'}
    assert h.get('coordinator', path + ',academic,rotation')['people'][0]['segments'] == []


def test_private_drafts_cancelled_duties_and_publication_effective_ranges(h):
    ids = fixture(h)
    with transaction(h.app.state.engine, write=True) as db:
        pub = db.scalar(select(m.Publication))
        pub.effective_until = date(2026, 10, 15)
    row = h.get('coordinator', PATH + '&hide=leave&person_ids=' + ids['people'][0])['people'][0]
    assert row['areas'][0]['count'] == 2
    assert all(clinic['count'] == 2 for segment in row['segments'] for clinic in segment['clinics'])


@pytest.mark.postgres
def test_postgres_projection_local_days_and_contract(pg, monkeypatch):
    test_projection_contract_opt_in_counts_and_hide_fallthrough(pg, monkeypatch)


def test_department_year_sql_work_budget(h, monkeypatch, record_property):
    start, end = date(2026, 1, 1), date(2027, 1, 1)
    with transaction(h.app.state.engine, write=True) as db:
        dep, org = h.seed['department_id'], h.seed['organisation_id']
        rank = db.scalar(select(m.Category).where(m.Category.department_id == dep))
        persons = [m.Person(organisation_id=org, display_name=f'Synthetic WIE {i:03d}', short_code=f'W{i:03d}') for i in range(150)]
        db.add_all(persons); db.flush()
        person_ids = [person.id for person in persons]
        db.add_all([m.Membership(department_id=dep, person_id=pid, category_id=rank.id, starts_on=start, ends_on=end) for pid in person_ids])
    fixture(h, source='draft', people=person_ids, starts=start, ends=end)
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.scalar(select(m.Workspace).where(m.Workspace.name == 'Synthetic October rota'))
        typ = db.get(m.AssignmentType, h.seed['type'])
        sessions, assignments = [], []
        # 500 sessions/month × 12, all 150 people; SQL inputs, not solver or elapsed-time budgets.
        for month in range(1, 13):
            for n in range(500):
                lo = local_midnight(date(2026, month, n % 28 + 1), 'Asia/Riyadh') + timedelta(hours=8)
                sid, aid = m.uid(), m.uid()
                sessions.append({'id': sid, 'workspace_id': workspace.id, 'assignment_type_id': typ.id, 'type_revision': typ.revision,
                    'type_snapshot': {'name': typ.name, 'definition': typ.definition}, 'location_id': h.seed['locations'][0],
                    'start': lo, 'end': lo + timedelta(hours=4)})
                assignments.append({'id': aid, 'shift_id': sid, 'person_id': person_ids[n % 150], 'start': lo, 'end': lo + timedelta(hours=4)})
        db.execute(m.Shift.__table__.insert(), sessions)
        db.execute(m.ShiftService.__table__.insert(), [{'shift_id': row['id'], 'service_id': h.seed['services'][0]} for row in sessions])
        db.execute(m.Assignment.__table__.insert(), assignments)
    from apps.api.services import timeline
    monkeypatch.setattr(timeline, '_check_data', lambda *a, **kw: pytest.fail('snapshot in bounded read'))
    monkeypatch.setattr(timeline, 'evaluate', lambda *a, **kw: pytest.fail('validator in bounded read'))
    statements = []
    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    def read(ids):
        statements.clear()
        event.listen(h.app.state.engine, 'before_cursor_execute', count)
        try:
            with transaction(h.app.state.engine) as db:
                body = list_data(db, actor_for(h, db), start, end, person_ids=ids, bucket='month', layers='none', limit=150)
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', count)
        return body, len(statements), list(statements)
    one, one_count, _ = read(person_ids[:1])
    all_people, count_queries, queries = read(person_ids)
    record_property('queries_one_person', one_count)
    record_property('queries_150_people', count_queries)
    record_property('sessions', 6000)
    assert len(all_people['people']) == all_people['total'] == 150
    assert all(row['areas'] for row in all_people['people'])
    assert count_queries <= one_count + 1
    assert count_queries <= 40, (count_queries, queries)
    assert sum('count(distinct' in query.lower() for query in queries) == 1
    assert sum(len(row['segments']) for row in all_people['people']) <= 150 * 14
    assert sum(area['count'] for row in all_people['people'] for area in row['areas']) == 6600


def test_draft_privacy_cancellation_and_staff_schedule_visibility(h):
    ids = fixture(h, source='draft')
    assert any(p['areas'] for p in h.get('coordinator', PATH)['people'])
    assert all(not p['areas'] for p in h.get('staff', PATH)['people'])
    with transaction(h.app.state.engine, write=True) as db:
        shifts = list(db.scalars(select(m.Shift).where(m.Shift.workspace_id == ids['workspace']).order_by(m.Shift.start)))
        shifts[0].cancelled = True
    assert next(p for p in h.get('coordinator', PATH)['people'] if p['id'] == ids['people'][0])['areas'][0]['count'] == 3
    with transaction(h.app.state.engine, write=True) as db:
        access = m.WorkspaceAccessVersion(workspace_id=ids['workspace'], visibility='private', purpose='sandbox',
            actor_id=h.seed['identities']['coordinator'], reason='Synthetic private sandbox')
        db.add(access); db.flush()
        db.get(m.Workspace, ids['workspace']).access_version_id = access.id
    assert all(not p['areas'] for p in h.get('coordinator', PATH)['people'])
    h.get('coordinator', PATH + '&workspace_id=' + ids['workspace'], status=404)


def test_two_significant_areas_majority_source_and_segment_spanning_periods():
    start, end = date(2026, 10, 1), date(2026, 12, 1)
    areas = {key: {'id': key, 'label': key, 'short_name': key, 'colour': None} for key in ('A', 'B', 'C')}
    work = {}
    for month in (10, 11):
        for n, key in enumerate(('A', 'A', 'A', 'A', 'B', 'B', 'C')):
            work[date(2026, month, n+1)] = {'areas': Counter({key: 1}), 'clinics': Counter({key: 1}), 'duties': 1}
    roster = {start+timedelta(days=i): ('published', 'Rota') for i in range((end-start).days)}
    result = project(start, end, 'month', work, [], areas, {k: {'label': k} for k in areas}, roster)
    segment, = result['segments']
    assert [a['id'] for a in segment['areas']] == ['A', 'B']
    assert {c['id']: c['count'] for c in segment['clinics']} == {'A': 8, 'B': 4, 'C': 2}
    assert segment['ends_on'] == end
    # 15/31 published days: the entire column is draft, avoiding one-day source slivers.
    roster = {start+timedelta(days=i): ('published' if i < 15 else 'draft', 'Rota') for i in range(31)}
    result = project(start, date(2026, 11, 1), 'month', work, [], areas, {k: {'label': k} for k in areas}, roster)
    assert len(result['segments']) == 1 and result['segments'][0]['source'] == 'draft'


@pytest.mark.parametrize('query', [
    'bucket=quarter', 'bucket=month&source=unknown', 'bucket=month&hide=unknown',
    'bucket=month&hide_leave_types=invalid', 'source=published'])
def test_projection_request_validation(h, query):
    h.get('coordinator', '/api/timeline?start=2026-10-01&end=2026-11-01&' + query, status=422)

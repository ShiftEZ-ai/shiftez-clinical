"""Windowed schedule reads: day summaries (/api/schedule/days) and published sessions (/api/schedule/sessions), built
on the schedule in force (docs/design/schedule-unification.md §7, §9)."""
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from random import Random

import pytest
from sqlalchemy import event as sql_event, select

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services import events, schedule_view
from packages.contracts import schemas as s
from .governance_support import identity
from .test_event_calendar import event_row

DAY4, DAY9 = ("2026-10-04T08:00:00+03:00", "2026-10-04T12:00:00+03:00"), ("2026-10-09T08:00:00+03:00", "2026-10-09T12:00:00+03:00")


def without_operational_timestamp(body):
    """Request-time observation clocks differ; warning facts and their fingerprint must still match."""
    return {**body, 'sources': [{**source, **({'operational': {key: value for key, value in source['operational'].items()
        if key != 'as_of'}} if 'operational' in source else {})} for source in body['sources']]}


def published_rota(h):
    """P: 4–11 Oct, the staff member on 4 and 9 Oct, a colleague on 9 Oct."""
    a, b, _ = h.seed["people"]
    w = h.shift(h.workspace("October rota"), *DAY4, people=[a])["workspace"]
    w = h.shift(w, *DAY9, people=[a, b])["workspace"]
    return h.publish(w)["publication_id"]


def days(h, role="coordinator", start="2026-10-01", end="2026-11-01", status=200):
    return h.get(role, f"/api/schedule/days?start={start}&end={end}", status)


def sessions(h, role="coordinator", start="2026-10-01", end="2026-11-01", exclude=None, status=200):
    return h.get(role, f"/api/schedule/sessions?start={start}&end={end}" + (f"&exclude={exclude}" if exclude else ""), status)


def test_admin_only_reads_planner_days_sessions_and_all_events(h):
    identity(h, 'admin_only', ['admin'])
    publication_id = published_rota(h)
    draft = h.shift(h.workspace('Unpublished rota'), people=[])['workspace']
    everyone = event_row(h)
    outside = event_row(h, audience={'roles': ['staff']}, settings={'calendar': 'none'})
    expected = days(h), sessions(h)
    client = h.client('admin_only')
    start, end = date(2026, 10, 1), date(2026, 11, 1)
    with transaction(h.app.state.engine) as db:
        actor = replace(authenticate(db, client.cookies['rota_session']), roles=frozenset({'admin'}))
        for reader, planned in zip((schedule_view.days, schedule_view.sessions), expected):
            _, found = reader(db, actor, start, end)
            assert {row['id'] for row in found['sources']} == (
                {publication_id, draft['id']} if reader == schedule_view.days else {publication_id})
            assert {row['id']: row['in_audience'] for row in found['events']} == {
                everyone['id']: True, outside['id']: False}
            assert without_operational_timestamp(found) == without_operational_timestamp(planned)
        found = events.occurrences(db, actor, start, end)
        assert {row['id'] for row in found['occurrences']} == {everyone['id'], outside['id']}


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_schedule_events_are_a_separate_viewer_layer_for_every_permitted_role(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    everyone = event_row(h, settings={'calendar': 'none', 'attendance': 'must_acknowledge'})
    own = event_row(h, audience={'roles': [role]})
    outside = event_row(h, settings={'blocks_duties': True}, audience={'where': {'match': 'all', 'conditions': [
        {'field': 'person_ids', 'op': 'any', 'values': ['unrelated-person']}]}})
    foreign = event_row(h)
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Other event department')
        db.add(department)
        db.flush()
        for model, key in ((m.EventType, 'event_type_id'), (m.Event, 'event_id'), (m.EventOccurrence, 'id')):
            db.get(model, foreign[key]).department_id = department.id
    allowed = role in {'admin', 'coordinator', 'approver', 'programme_lead', 'auditor', 'staff'}
    for endpoint in ('days', 'sessions'):
        response = h.client(role).get(f'/api/schedule/{endpoint}?start=2026-10-01&end=2026-11-01')
        assert response.status_code == (200 if allowed else 403), response.text
        if not allowed:
            continue
        body = response.json()
        expected = {everyone['id']: True, own['id']: True}
        if role != 'staff':
            expected[outside['id']] = False
        assert {row['id']: row['in_audience'] for row in body['events']} == expected
        listed = h.get(role, '/api/events/occurrences?starts_on=2026-10-01&ends_on=2026-11-01')['occurrences']
        assert {row['id'] for row in listed} == set(expected)
        assert body['sources'] == []
        assert all(s.ScheduleEventRow.model_validate(row) for row in body['events'])
        assert all(not {'audience', 'counts', 'receipt'} & row.keys() for row in body['events'])
        if endpoint == 'days':
            assert body['days'] == {}
        else:
            assert body['shifts'] == body['coverage'] == body['findings'] == []
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.AnnouncementReceipt))) == []


@pytest.mark.parametrize('endpoint', ['days', 'sessions'])
@pytest.mark.parametrize('role', ['staff', 'coordinator'])
def test_schedule_event_etag_tracks_edits_cancellation_and_current_audience(h, endpoint, role):
    person_id = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities'][role]).person_id = person_id
        tag = m.Tag(department_id=h.seed['department_id'], name='Synthetic schedule audience', labels=['people'])
        db.add(tag)
        db.flush()
        member = m.TagMember(department_id=h.seed['department_id'], tag_id=tag.id, kind='people', member_id=person_id)
        db.add(member)
        db.flush()
        tag_id, member_id = tag.id, member.id
    row = event_row(h, audience={'where': {'match': 'all', 'conditions': [
        {'field': 'person_ids', 'op': 'any', 'values': [tag_id]}]}})
    path = f'/api/schedule/{endpoint}?start=2026-10-01&end=2026-11-01'
    client = h.client(role)
    first = client.get(path)
    tag = first.headers['etag']
    assert [r['id'] for r in first.json()['events']] == [row['id']]
    assert first.json()['events'][0]['in_audience'] is True
    assert client.get(path, headers={'if-none-match': tag}).status_code == 304
    # Audience facts can change without changing an occurrence's revision.
    with transaction(h.app.state.engine, write=True) as db:
        db.delete(db.get(m.TagMember, member_id))
    outside = client.get(path, headers={'if-none-match': tag})
    assert outside.status_code == 200 and outside.headers['etag'] != tag
    assert [(r['id'], r['in_audience']) for r in outside.json()['events']] == (
        [] if role == 'staff' else [(row['id'], False)])
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.TagMember(department_id=h.seed['department_id'], tag_id=tag_id, kind='people', member_id=person_id))
        occurrence = db.get(m.EventOccurrence, row['id'])
        occurrence.data = {**occurrence.data, 'heading': 'Changed meeting'}
        occurrence.cancelled, occurrence.revision = True, occurrence.revision + 1
    changed = client.get(path, headers={'if-none-match': tag})
    assert changed.status_code == 200 and changed.headers['etag'] != tag
    [event] = changed.json()['events']
    assert event['in_audience'] is True
    assert (event['id'], event['heading'], event['cancelled'], event['revision']) == (row['id'], 'Changed meeting', True, 2)
    # Planner cache keys also belong to the viewer, even where the duty layer is identical.
    other = 'coordinator' if role == 'staff' else 'staff'
    assert h.client(other).get(path, headers={'if-none-match': changed.headers['etag']}).status_code == 200


def test_schedule_events_observe_local_overlap_and_the_full_year_window(h):
    row = event_row(h, start='2026-10-03T23:30:00+03:00', end='2026-10-04T00:30:00+03:00')
    event_row(h, start='2026-10-03T22:00:00+03:00', end='2026-10-04T00:00:00+03:00')
    event_row(h, start='2026-10-05T00:00:00+03:00', end='2026-10-05T01:00:00+03:00')
    for reader in (days, sessions):
        found = reader(h, 'staff', start='2026-10-04', end='2026-10-05')
        assert [r['id'] for r in found['events']] == [row['id']]
    far = event_row(h, start='2027-10-30T09:00:00+03:00', end='2027-10-30T10:00:00+03:00')
    assert far['id'] in {r['id'] for r in days(h, 'staff', start='2026-10-01', end='2027-11-05')['events']}


def test_schedule_viewer_queries_are_batched_and_still_resolve_each_event_date(h):
    me = h.get('staff', '/api/me')['person_id']
    h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['staff'], 'role': 'credentialer',
        'starts_on': '2026-10-04', 'ends_on': '2026-10-05', 'reason': 'Synthetic dated audience budget'})
    category = h.configure('categories', {'name': 'Short event audience', 'starts_on': '2026-01-01'})['record']
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Membership(department_id=h.seed['department_id'], person_id=me, category_id=category['id'],
            starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 5)))
    audience = {'where': {'match': 'all', 'conditions': [
        {'field': 'roles', 'values': ['credentialer']}, {'field': 'category_ids', 'values': [category['id']]}]}}
    first = event_row(h, audience=audience)
    engine, queries = h.app.state.engine, []

    def statement(conn, cursor, sql, parameters, context, executemany):
        if sql.lstrip().upper().startswith('SELECT'):
            queries.append(sql)

    sql_event.listen(engine, 'before_cursor_execute', statement)
    try:
        assert [row['id'] for row in days(h, 'staff')['events']] == [first['id']]
        small = len(queries)
    finally:
        sql_event.remove(engine, 'before_cursor_execute', statement)
    for day in range(5, 11):
        event_row(h, audience=audience, start=f'2026-10-{day:02}T09:00:00+03:00', end=f'2026-10-{day:02}T10:00:00+03:00')
    queries.clear()
    sql_event.listen(engine, 'before_cursor_execute', statement)
    try:
        assert [row['id'] for row in days(h, 'staff')['events']] == [first['id']]
        assert len(queries) <= small + 1, 'viewer facts must be loaded once across event dates'
    finally:
        sql_event.remove(engine, 'before_cursor_execute', statement)


def test_events_never_add_duties_cover_or_workload_to_a_published_schedule(h):
    pub_id = published_rota(h)
    before_days, before_sessions = days(h), sessions(h)
    before_workload = h.get('coordinator', f'/api/reports/{pub_id}')['rows']
    event_row(h)
    after_days, after_sessions = days(h), sessions(h)
    assert {key: value for key, value in without_operational_timestamp(after_days).items() if key != 'events'} == {
        key: value for key, value in without_operational_timestamp(before_days).items() if key != 'events'}
    assert {key: value for key, value in without_operational_timestamp(after_sessions).items() if key != 'events'} == {
        key: value for key, value in without_operational_timestamp(before_sessions).items() if key != 'events'}
    assert h.get('coordinator', f'/api/reports/{pub_id}')['rows'] == before_workload


def test_days_show_one_dot_per_source_with_open_places_and_errors_from_the_stored_check(h):
    pid = published_rota(h)
    h.policy({"kind": "maximum_participants", "maximum": 1, "aggregation": "assignment"})
    d = h.workspace("Next week", "2026-10-11", "2026-10-18")
    d = h.shift(d, "2026-10-12T08:00:00+03:00", "2026-10-12T12:00:00+03:00", people=[])["workspace"]
    d = h.shift(d, "2026-10-13T08:00:00+03:00", "2026-10-13T12:00:00+03:00", people=h.seed["people"][:2])["workspace"]
    h.lifecycle(h.detail(d)["workspace"], "validate")
    found = days(h)
    sources = {s["id"]: s for s in found["sources"]}
    assert list(sources) == [pid, d["id"]] and found["timezone"]
    assert (sources[pid]["kind"], sources[pid]["status"], sources[pid]["sequence"], sources[pid]["in_force_until"], sources[pid]["checked"]) == (
        "published", "published", 1, "2026-10-11", True)
    assert (sources[d["id"]]["kind"], sources[d["id"]]["status"], sources[d["id"]]["in_force_until"], sources[d["id"]]["sequence"]) == (
        "draft", "draft", None, None)
    assert sources[d["id"]]["checked"] is True and sources[d["id"]]["name"] == "Next week" and sources[pid]["name"] == "October rota"
    assert set(found["days"]) == {"2026-10-04", "2026-10-09", "2026-10-12", "2026-10-13"}  # unplanned days carry no dot
    assert found["days"]["2026-10-04"] == [{"source": pid, "sessions": 1, "open": 0, "errors": 0, "warnings": 0, "issue": None,
                                           "places_needed": 1, "places_filled": 1, "places_open": 0}]
    [gap] = found["days"]["2026-10-12"]
    assert (gap["source"], gap["sessions"], gap["open"]) == (d["id"], 1, 1)
    assert (gap["places_needed"], gap["places_filled"], gap["places_open"]) == (1, 0, 1)
    [crowded] = found["days"]["2026-10-13"]
    assert crowded["open"] == 0 and crowded["errors"] >= 1 and crowded["issue"]
    assert "duties" not in crowded
    # Windowed: only days inside [start, end).
    assert set(days(h, start="2026-10-05", end="2026-10-13")["days"]) == {"2026-10-09", "2026-10-12"}


def test_a_replacement_takes_the_days_from_its_first_day(h):
    pid = published_rota(h)
    q = h.command("coordinator", "workspace.create", {"name": "Replacement", "starts_on": "2026-10-08", "ends_on": "2026-10-15",
                                                      "replaces_publication_id": pid})["workspace"]
    q = h.shift(q, "2026-10-09T13:00:00+03:00", "2026-10-09T17:00:00+03:00", people=[h.seed["people"][1]])["workspace"]
    # Still a draft: both are shown on 9 Oct (the draft as its own layer).
    assert {e["source"] for e in days(h)["days"]["2026-10-09"]} == {pid, q["id"]}
    qid = h.publish(q)["publication_id"]
    found = days(h)
    assert [(s["id"], s["in_force_until"]) for s in found["sources"]] == [(pid, "2026-10-08"), (qid, "2026-10-15")]
    assert found["days"]["2026-10-09"] == [{"source": qid, "sessions": 1, "open": 0, "errors": 0, "warnings": 0, "issue": None,
                                           "places_needed": 1, "places_filled": 1, "places_open": 0}]
    assert [e["source"] for e in found["days"]["2026-10-04"]] == [pid]
    window = sessions(h)
    assert [(s["start"][:10], s["publication_id"]) for s in window["shifts"]] == [("2026-10-04", pid), ("2026-10-09", qid)]
    assert {c["shift_id"] for c in window["coverage"]} <= {s["id"] for s in window["shifts"]}
    assert set(window["people"]) == {h.seed["people"][0], h.seed["people"][1]}
    assert set(window["locations"]) == {h.seed["locations"][0]} and set(window["services"]) == {h.seed["services"][0]}
    assert set(window["people"][h.seed["people"][0]]) == {"id", "display_name", "short_code"}


def test_staff_see_only_their_own_published_duties(h):
    a, b, _ = h.seed["people"]
    pid = published_rota(h)
    h.shift(h.workspace("Draft with me", "2026-10-11", "2026-10-18"), "2026-10-12T08:00:00+03:00", "2026-10-12T12:00:00+03:00", people=[a])
    # A published rota the staff member is not on.
    other = h.shift(h.workspace("Colleagues", "2026-10-18", "2026-10-25"), "2026-10-19T08:00:00+03:00", "2026-10-19T12:00:00+03:00", people=[b])
    h.publish(other["workspace"])
    mine = days(h, "staff")
    assert [s["id"] for s in mine["sources"]] == [pid]  # no drafts, no rotas they are not on
    assert set(mine["days"]) == {"2026-10-04", "2026-10-09"}
    [entry] = mine["days"]["2026-10-09"]
    assert (entry["sessions"], entry["open"], entry["errors"], entry["warnings"], entry["issue"]) == (1, 0, 0, 0, None)
    [duty] = entry["duties"]
    assert set(duty) == {"shift_id", "name", "short_code", "colour", "assignment_type_id", "start", "end"}
    assert (duty["name"], duty["assignment_type_id"]) == ("Clinical review session", h.seed["type"]) and duty["start"].startswith("2026-10-09T05:00")
    window = sessions(h, "staff")
    assert [s["start"][:10] for s in window["shifts"]] == ["2026-10-04", "2026-10-09"]
    assert all([p["person_id"] for p in s["participants"]] == [a] for s in window["shifts"])
    assert (window["coverage"], window["findings"], list(window["people"])) == ([], [], [a])
    team = sessions(h)
    assert len(team["shifts"]) == 3 and len(team["shifts"][1]["participants"]) == 2 and team["coverage"]


def test_exclude_leaves_out_the_amended_rota(h):
    pid = published_rota(h)
    workspace_id = sessions(h)["sources"][0]["workspace_id"]
    for value in (pid, workspace_id, f"other,{pid}"):
        left = sessions(h, exclude=value)
        assert (left["shifts"], left["sources"]) == ([], [])
    assert len(sessions(h, exclude="unrelated")["shifts"]) == 2


def test_ranges_and_roles(h):
    for start, end in (("2026-10-01", "2026-10-01"), ("2026-10-02", "2026-10-01"), ("2026-01-01", "2027-02-06")):
        assert days(h, start=start, end=end, status=422)["error"]["code"] == "VALUE_INVALID"
    days(h, start="2026-01-01", end="2027-02-05")  # 400 days
    assert sessions(h, start="2026-10-01", end="2027-01-02", status=422)["error"]["code"] == "VALUE_INVALID"
    sessions(h, start="2026-10-01", end="2027-01-01")  # 92 days
    for role in ("credentialer", "operator"):
        days(h, role, status=403)
        sessions(h, role, status=403)
    for role in ("approver", "auditor", "admin"):
        days(h, role)


def test_unchanged_windows_answer_304_and_a_new_publication_changes_the_tag(h):
    published_rota(h)
    client = h.client("coordinator")
    for path in ("/api/schedule/days?start=2026-10-01&end=2026-11-01", "/api/schedule/sessions?start=2026-10-01&end=2026-11-01"):
        first = client.get(path)
        tag = first.headers["etag"]
        again = client.get(path, headers={"if-none-match": tag})
        assert (again.status_code, again.headers["etag"], again.content) == (304, tag, b"")
    tags = {p: client.get(p).headers["etag"] for p in ("/api/schedule/days?start=2026-10-01&end=2026-11-01",
                                                          "/api/schedule/sessions?start=2026-10-01&end=2026-11-01")}
    later = h.shift(h.workspace("Later", "2026-10-11", "2026-10-18"), "2026-10-12T08:00:00+03:00", "2026-10-12T12:00:00+03:00")
    h.publish(later["workspace"])
    for path, tag in tags.items():
        fresh = client.get(path, headers={"if-none-match": tag})
        assert fresh.status_code == 200 and fresh.headers["etag"] != tag
    # Staff and the team never share a tag.
    assert h.client("staff").get("/api/schedule/days?start=2026-10-01&end=2026-11-01").headers["etag"] != client.get(
        "/api/schedule/days?start=2026-10-01&end=2026-11-01").headers["etag"]


def test_window_tags_change_when_the_organisation_timezone_changes(h):
    published_rota(h)
    client = h.client('coordinator')
    paths = [f'/api/schedule/{kind}?start=2026-10-01&end=2026-11-01' for kind in ('days', 'sessions')]
    tags = {path: client.get(path).headers['etag'] for path in paths}
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    h.command('admin', 'department.settings', {'department_name': dept['name'],
        'department_revision': dept['revision'], 'organisation_name': org['name'],
        'timezone': 'America/Los_Angeles', 'week_start': org['week_start'],
        'reason': 'Correct the fictional department timezone'}, dept['id'], org['revision'])
    for path, tag in tags.items():
        fresh = client.get(path, headers={'if-none-match': tag})
        assert fresh.status_code == 200 and fresh.headers['etag'] != tag
        assert fresh.json()['timezone'] == 'America/Los_Angeles'
    assert '2026-10-03' in client.get(paths[0]).json()['days']


def test_sessions_tag_changes_when_current_contact_or_catalogue_details_change(h):
    published_rota(h)
    client = h.client('coordinator')
    path = '/api/schedule/sessions?start=2026-10-01&end=2026-11-01'
    tag = client.get(path).headers['etag']
    person = next(row for row in h.get('coordinator', '/api/catalogues')['people']
                  if row['id'] == h.seed['people'][0])
    pager_type = next(t for t in h.get('admin', '/api/contact-types')['contact_types'] if t['legacy_field'] == 'pager')
    current_pager = next((c for c in h.get('coordinator', f'/api/people/{person["id"]}/card')['contacts']
                          if c['kind'] == 'pager'), None)
    h.command('admin', 'person.contact.set', {
        'person_id': person['id'], 'contact_type_id': pager_type['id'], 'value': '9876',
        'reason': 'Synthetic contact edit'},
        target=current_pager['id'] if current_pager else None,
        revision=current_pager['revision'] if current_pager else None)
    contact_window = client.get(path, headers={'if-none-match': tag})
    assert contact_window.status_code == 200 and contact_window.headers['etag'] != tag
    assert contact_window.json()['people'][person['id']]['pager'] == '9876'
    person = next(row for row in h.get('coordinator', '/api/catalogues')['people']
                  if row['id'] == person['id'])
    h.configure('people', {'display_name': 'Corrected fictional name'}, person['id'], person['revision'])
    fresh = client.get(path, headers={'if-none-match': contact_window.headers['etag']})
    assert fresh.status_code == 200 and fresh.headers['etag'] != contact_window.headers['etag']
    assert fresh.json()['people'][person['id']]['display_name'] == 'Corrected fictional name'
    assert client.get(path, headers={'if-none-match': fresh.headers['etag']}).status_code == 304


def _budget(seconds):
    """Coverage tracing (the CI pipeline) makes pure-Python work several times slower; budgets scale with it."""
    import coverage
    return seconds * (5 if coverage.Coverage.current() else 1)


def _big_month(db, workspace_id, people, typ, places, services, first, rng, checked_at):
    """~500 sessions over a month, 2–4 people each, with a stored check (some open places and errors)."""
    shifts, assignments, links, coverage, findings = [], [], [], [], []
    for day in range(31):
        for k in range(16):
            hours = [(5, 9), (10, 14), (17, 20)][k % 3]  # UTC, Riyadh +3
            on = first + timedelta(days=day)
            start = datetime(on.year, on.month, on.day, hours[0], tzinfo=timezone.utc)
            end = datetime(on.year, on.month, on.day, hours[1], tzinfo=timezone.utc)
            s = m.Shift(id=f"{workspace_id[:8]}-{day}-{k}", workspace_id=workspace_id, assignment_type_id=typ.id, type_revision=typ.revision,
                        type_snapshot=record(typ), location_id=places[k % 2], start=start, end=end, attributes={})
            shifts.append(s)
            links.append(m.ShiftService(shift_id=s.id, service_id=services[k % 2]))
            for person in rng.sample(people, rng.randint(2, 4)):
                assignments.append(m.Assignment(shift_id=s.id, person_id=person, start=start, end=end))
            coverage.append({"shift_id": s.id, "start": start.isoformat(), "end": end.isoformat(), "status": "gap" if k == 0 else "covered"})
            if k % 5 == 0:
                findings.append({"code": "SYNTHETIC", "message": "Synthetic breach", "severity": "error", "shift_id": s.id,
                                 "person_id": None, "start": start.isoformat()})
    db.add_all(shifts)
    db.flush()
    db.add_all(links + assignments)
    return {"evaluated_at": checked_at, "valid": False, "coverage": coverage, "findings": findings}


@pytest.fixture(scope="module")
def department_schedule_windows(scale_harness):
    """150 people, ~500 sessions a month published and ~500 more in a checked draft for the next month."""
    h = scale_harness()
    rng = Random(7)
    published = h.workspace("Big October", "2026-10-01", "2026-11-01")
    draft = h.workspace("Big November", "2026-11-01", "2026-12-01")
    staff_person = h.seed["people"][0]
    from packages.domain.validation import evaluate
    contracts = evaluate(h.snapshot(published))["contracts"]  # the publication guard requires the current check contracts
    with transaction(h.app.state.engine, write=True) as db:
        w = db.get(m.Workspace, published["id"])
        org_id = db.get(m.Department, w.department_id).organisation_id
        extra = [m.Person(organisation_id=org_id, display_name=f"Synthetic person {i:03}") for i in range(147)]
        db.add_all(extra)
        db.flush()
        people = list(h.seed["people"]) + [p.id for p in extra]
        typ = db.get(m.AssignmentType, h.seed["type"])
        check = _big_month(db, w.id, people, typ, h.seed["locations"], h.seed["services"], date(2026, 10, 1), rng, "2026-09-30T00:00:00+00:00")
        w.status = "published"
        pub = m.Publication(workspace_id=w.id, department_id=w.department_id, sequence=1, starts_on=w.starts_on, ends_on=w.ends_on,
                            current=True, published_by=w.created_by, snapshot={}, validation={**check, "contracts": contracts}, fingerprint="0" * 64)
        db.add(pub)
        db.flush()
        on = {a.person_id for a in db.scalars(select(m.Assignment).join(m.Shift).where(m.Shift.workspace_id == w.id))}
        db.add_all([m.PublicationPerson(publication_id=pub.id, person_id=p) for p in on])
        nov = db.get(m.Workspace, draft["id"])
        nov.validation = _big_month(db, nov.id, people, typ, h.seed["locations"], h.seed["services"], date(2026, 11, 1), rng,
                                    "2026-10-30T00:00:00+00:00")
    staff_present = staff_person in on
    timings = {}

    def timed(name, role, path, runs=3):
        client = h.client(role)
        client.get(path)  # sign-in and first-request costs are not the endpoint's
        best = None
        for run in range(runs):
            from apps.api.services import schedule_view
            schedule_view._summaries.clear()  # cold: no memoised summaries
            began = time.process_time()
            response = client.get(path)
            elapsed = time.process_time() - began
            assert response.status_code == 200, response.text
            best = elapsed if best is None else min(best, elapsed)
        timings[name] = best
        return response.json()

    year = timed("days 365 (coordinator)", "coordinator", "/api/schedule/days?start=2026-03-01&end=2027-03-01")
    weeks = timed("sessions 42 (coordinator)", "coordinator", "/api/schedule/sessions?start=2026-09-28&end=2026-11-09")
    timed("days 365 (staff)", "staff", "/api/schedule/days?start=2026-03-01&end=2027-03-01")
    timed("sessions 42 (staff)", "staff", "/api/schedule/sessions?start=2026-09-28&end=2026-11-09")
    print("schedule window timings: " + ", ".join(f"{k} {v * 1000:.0f} ms" for k, v in timings.items()))
    return staff_present, year, weeks, timings


def test_department_scale_windows_answer_quickly(department_schedule_windows):
    staff_present, year, weeks, _ = department_schedule_windows
    assert staff_present
    assert len(year["days"]) == 62 and sum(e["sessions"] for d in year["days"].values() for e in d) == 31 * 16 * 2
    assert sum(e["open"] for d in year["days"].values() for e in d) == 62
    assert len(weeks["shifts"]) == 31 * 16 and len(weeks["people"]) > 140 and len(weeks["coverage"]) == 31 * 16


@pytest.mark.cpu_budget
def test_department_scale_windows_answer_quickly_cpu_budget(department_schedule_windows):
    *_, timings = department_schedule_windows
    # Measured about 0.1-0.25 s each on a developer machine (SQLite); a several-fold regression fails.
    for name, elapsed in timings.items():
        assert elapsed < _budget(1.0), (name, elapsed)


def test_team_calendar_reads_only_its_window_from_session_rows_and_ignores_snapshots(h):
    """/api/calendar (the People view's academic band, Today, My duties) shares the windowed reader: an emptied snapshot
    changes nothing, and a replacement being shown ends the old rota from its first day."""
    a, b, _ = h.seed["people"]
    pid = published_rota(h)
    # A publication whose snapshot is empty (as rows written directly are): the window never reads it (it 500'd before).
    other = h.shift(h.workspace("Snapshot-less", "2026-10-20", "2026-10-27"), "2026-10-21T08:00:00+03:00", "2026-10-21T12:00:00+03:00", people=[b])["workspace"]
    from packages.domain.validation import evaluate
    contracts = evaluate(h.snapshot(other))["contracts"]
    with transaction(h.app.state.engine, write=True) as db:
        w = db.get(m.Workspace, other["id"])
        w.status = "published"
        db.add(m.Publication(workspace_id=w.id, department_id=w.department_id, sequence=9, starts_on=w.starts_on, ends_on=w.ends_on,
                             current=True, published_by=w.created_by, snapshot={}, validation={"valid": True, "coverage": [], "findings": [], "contracts": contracts},
                             fingerprint="0" * 64))
    assert [e["person_id"] for e in h.get("coordinator", "/api/calendar?start=2026-10-21&end=2026-10-22")["events"] if e["kind"] == "duty"] == [b]
    cal = h.get("coordinator", "/api/calendar?start=2026-10-09&end=2026-10-10")
    assert sorted(e["person_id"] for e in cal["events"] if e["kind"] == "duty") == sorted([a, b])
    assert [s["source_id"] for s in cal["sessions"]] == [pid]
    assert cal["sessions"][0]["location_name"] != "Location not recorded"
    assert h.get("coordinator", "/api/calendar?start=2026-10-05&end=2026-10-06")["sessions"] == []
    q = h.command("coordinator", "workspace.create", {"name": "Replacement", "starts_on": "2026-10-08", "ends_on": "2026-10-15",
                                                      "replaces_publication_id": pid})["workspace"]
    shown = h.get("coordinator", f"/api/calendar?start=2026-10-09&end=2026-10-10&workspace_id={q['id']}")
    assert all(s["source_id"] != pid for s in shown["sessions"])
    staff = h.get("staff", "/api/calendar?start=2026-10-01&end=2026-10-31")
    assert {e["person_id"] for e in staff["events"]} == {a}

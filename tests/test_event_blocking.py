"""Event attendance becomes canonical protected-time facts for checks and scheduling."""
from datetime import date

import pytest
from sqlalchemy import event as sql_event, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.events import protected_facts
from packages.domain.cp_sat import generate
from packages.domain.time import aware
from packages.domain.validation import evaluate
from .test_event_calendar import event_row, respond


def test_expected_event_blocks_tagged_people_without_signins_and_cancellation_clears_it(h):
    with transaction(h.app.state.engine, write=True) as db:
        person = m.Person(organisation_id=h.seed["organisation_id"], display_name="Fictional attendee", active=True)
        db.add(person)
        db.flush()
        person_id = person.id
        first = db.scalar(select(m.Membership).where(m.Membership.person_id == h.seed["people"][0]))
        db.add(m.Membership(department_id=h.seed["department_id"], person_id=person_id,
            category_id=first.category_id, starts_on=date(2020, 1, 1)))
        tag = m.Tag(department_id=h.seed["department_id"], name="Teaching attendees", labels=["people"])
        db.add(tag)
        db.flush()
        db.add(m.TagMember(department_id=h.seed["department_id"], tag_id=tag.id, kind="people", member_id=person_id))
        tag_id = tag.id
    attendee = event_row(h, settings={"blocks_duties": True}, audience={"where": {
        "match": "all", "conditions": [{"field": "person_ids", "op": "any", "values": [tag_id]}]}})
    # The same event type can remain available for duties by configuration.
    event_row(h, settings={"blocks_duties": False})
    w = h.shift(h.workspace(), people=[person_id], seed_restricted=True)['workspace']
    data = h.snapshot(w)
    blocks = [r for r in data["scheduling_facts"] if r.get("event_occurrence_id") == attendee["id"]]
    assert blocks[0]["data"]["line"]["who"]["conditions"][0]["values"] == [person_id]
    assert blocks[0]['kind'] == blocks[0]['data']['catalogue_id'] == 'protected_time'
    assert not any(r.get('event_occurrence_id') for r in data['availability'])
    assert any(f["code"] == "RESTRICTION" and f["person_id"] == person_id for f in evaluate(data)["findings"])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.EventOccurrence, attendee["id"]).cancelled = True
    assert not any(r.get("event_occurrence_id") == attendee["id"] for r in h.snapshot(w)["scheduling_facts"])


def test_optional_rsvp_blocks_only_going_but_expected_attendance_remains_required(h):
    me = h.get("staff", "/api/me")["person_id"]
    optional = event_row(h, settings={"blocks_duties": True, "attendance": "optional", "rsvp": True})
    expected = event_row(h, settings={"blocks_duties": True, "attendance": "expected", "rsvp": True})
    w = h.workspace()

    def mine():
        return {r["event_occurrence_id"] for r in h.snapshot(w)["scheduling_facts"]
                if r.get("event_occurrence_id") and me in r["data"]["line"]["who"]["conditions"][0]["values"]}

    assert mine() == {expected["id"]}
    respond(h, optional, "going")
    respond(h, expected, "not_going")
    assert mine() == {optional["id"], expected["id"]}
    respond(h, optional, "not_going")
    assert mine() == {expected["id"]}


def test_auto_schedule_uses_event_protection_for_open_duties(h):
    blocked, available = h.seed["people"][:2]
    event_row(h, settings={"blocks_duties": True}, audience={"where": {
        "match": "all", "conditions": [{"field": "person_ids", "op": "any", "values": [blocked]}]}})
    w = h.shift(h.workspace(), people=[])["workspace"]
    data = h.snapshot(w)
    data["people"] = {pid: data["people"][pid] for pid in (blocked, available)}
    result = generate(data, 5, deterministic=True)
    assert result["status"] == "candidate_feasible", result
    assert result["validation"]["valid"]
    assert result["additions"] and {a["participant"]["person_id"] for a in result["additions"]} == {available}


def test_event_attendance_blocks_duties_in_other_departments_for_validator_and_solver(h):
    blocked, available = h.seed['people'][:2]
    row = event_row(h, settings={'blocks_duties': True}, audience={'person_ids': [blocked]})
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Other event department')
        db.add(department)
        db.flush()
        for cls, key in ((m.EventType, 'event_type_id'), (m.Event, 'event_id'), (m.EventOccurrence, 'id')):
            db.get(cls, row[key]).department_id = department.id
    w = h.shift(h.workspace(), people=[blocked], seed_restricted=True)['workspace']
    data = h.snapshot(w)
    assert any(f['code'] == 'RESTRICTION' and f['person_id'] == blocked for f in evaluate(data)['findings'])
    data['shifts'][0]['participants'] = []
    data['people'] = {pid: data['people'][pid] for pid in (blocked, available)}
    result = generate(data, 5, deterministic=True)
    assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
    assert result['additions'] and {r['participant']['person_id'] for r in result['additions']} == {available}


def test_event_rule_map_uses_each_exact_interval_without_blocking_the_whole_rota(h):
    from packages.domain.rule_map import rule_map
    person = h.seed['people'][0]
    first = event_row(h, settings={'blocks_duties': True}, start='2026-10-04T08:30:00+03:00', end='2026-10-04T08:45:00+03:00')
    second = event_row(h, settings={'blocks_duties': True}, start='2026-10-04T10:30:00+03:00', end='2026-10-04T10:45:00+03:00')
    w = h.workspace()
    for start, end, restricted in (('2026-10-04T08:00:00+03:00', '2026-10-04T09:00:00+03:00', True),
                                   ('2026-10-04T10:00:00+03:00', '2026-10-04T11:00:00+03:00', True),
                                   ('2026-10-05T08:00:00+03:00', '2026-10-05T09:00:00+03:00', False)):
        w = h.shift(w, start, end, people=[person], seed_restricted=restricted)['workspace']
    mapped = rule_map(h.snapshot(w))
    for occurrence, expected_session in ((first, 0), (second, 1)):
        [entry] = [row for row in mapped['rules'] if row['id'].startswith(f"event:{occurrence['id']}:")]
        assert entry['sessions'] == [expected_session]
        assert entry['days'] == [0] and entry['people'][person] == [[0, 1]]
        assert entry['session_side'] is True


@pytest.mark.parametrize('start,end,clash', [
    ('2026-10-04T23:00:00+03:00', '2026-10-04T23:59:30+03:00', False),
    ('2026-10-04T23:59:30+03:00', '2026-10-05T00:00:30+03:00', True),
    ('2026-10-05T00:00:29+03:00', '2026-10-05T01:00:00+03:00', True),
    ('2026-10-05T00:00:30+03:00', '2026-10-05T01:00:00+03:00', False),
])
def test_event_protection_uses_exact_overnight_instants_and_exclusive_end(h, start, end, clash):
    row = event_row(h, settings={'blocks_duties': True, 'calendar': 'none'},
        start='2026-10-04T23:59:30+03:00', end='2026-10-05T00:00:30+03:00')
    w = h.shift(h.workspace(), start, end, seed_restricted=clash)['workspace']
    data = h.snapshot(w)
    findings = [f for f in evaluate(data)['findings'] if f['code'] == 'RESTRICTION']
    assert bool(findings) == clash
    if clash:
        assert all(f['rule_id'].startswith(f"event:{row['id']}:") for f in findings)
        assert all(aware(f['start']) >= aware('2026-10-04T23:59:30+03:00')
            and aware(f['end']) <= aware('2026-10-05T00:00:30+03:00') for f in findings)
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.SchedulingFact))) == [], 'event protections are derived, never persisted'


def test_everyone_is_department_scoped_but_explicit_audience_can_name_another_person(h):
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed["organisation_id"], name="Another department")
        person = m.Person(organisation_id=h.seed["organisation_id"], display_name="Fictional other-department attendee")
        db.add_all([other, person])
        db.flush()
        category = m.Category(department_id=other.id, name="Synthetic other department rank", starts_on=date(2020, 1, 1))
        db.add(category)
        db.flush()
        db.add(m.Membership(department_id=other.id, person_id=person.id, category_id=category.id, starts_on=date(2020, 1, 1)))
        person_id = person.id
    everyone = event_row(h, settings={"blocks_duties": True})
    explicit = event_row(h, settings={"blocks_duties": True}, audience={"where": {
        "match": "all", "conditions": [{"field": "person_ids", "op": "any", "values": [person_id]}]}})
    rows = h.snapshot(h.workspace())["scheduling_facts"]
    other_blocks = {r["event_occurrence_id"] for r in rows if r.get("event_occurrence_id") and person_id in r["data"]["line"]["who"]["conditions"][0]["values"]}
    assert other_blocks == {explicit["id"]}
    assert any(r.get("event_occurrence_id") == everyone["id"] and h.seed["people"][0] in r["data"]["line"]["who"]["conditions"][0]["values"] for r in rows)


@pytest.mark.postgres
def test_event_protection_queries_do_not_grow_with_attendees_occurrences_or_dates(pg):
    h = pg
    event_row(h, settings={"blocks_duties": True})
    with transaction(h.app.state.engine, write=True) as db:
        people = [m.Person(organisation_id=h.seed["organisation_id"], display_name=f"Fictional event attendee {i}") for i in range(150)]
        db.add_all(people)
        db.flush()
        ids = [p.id for p in people]
        db.add_all([m.Membership(department_id=h.seed["department_id"], person_id=pid,
            category_id=h.seed["category"], starts_on=date(2020, 1, 1)) for pid in ids])
    engine, queries = h.app.state.engine, []

    def statement(conn, cursor, sql, parameters, context, executemany):
        if sql.lstrip().upper().startswith("SELECT"):
            queries.append(sql)

    def blocks(person_ids):
        with transaction(engine) as db:
            return protected_facts(db, [h.seed["department_id"]], person_ids,
                aware("2026-10-04T00:00:00+03:00"), aware("2026-10-05T00:00:00+03:00"), "Asia/Riyadh")

    sql_event.listen(engine, "before_cursor_execute", statement)
    try:
        assert len(blocks(ids[:1])) == 1
        small = len(queries)
        queries.clear()
        assert len(blocks(ids)[0]["data"]["line"]["who"]["conditions"][0]["values"]) == 150
        assert len(queries) <= small + 1, "attendees must be loaded with set queries"
    finally:
        sql_event.remove(engine, "before_cursor_execute", statement)
    event_row(h, settings={'blocks_duties': True}, start='2026-10-05T09:00:00+03:00', end='2026-10-05T10:00:00+03:00')
    queries.clear()
    sql_event.listen(engine, 'before_cursor_execute', statement)
    try:
        with transaction(engine) as db:
            facts = protected_facts(db, [h.seed['department_id']], ids,
                aware('2026-10-04T00:00:00+03:00'), aware('2026-10-06T00:00:00+03:00'), 'Asia/Riyadh')
        assert len(facts) == 2
        assert len(queries) <= small + 1, 'event dates must reuse their loaded person facts'
    finally:
        sql_event.remove(engine, 'before_cursor_execute', statement)
    for i in range(6):
        event_row(h, settings={"blocks_duties": True}, heading=f"Parallel event {i}")
    queries.clear()
    sql_event.listen(engine, "before_cursor_execute", statement)
    try:
        assert len(blocks(ids)) == 7
        assert len(queries) <= small + 1, "same-day occurrences must share audience facts"
    finally:
        sql_event.remove(engine, "before_cursor_execute", statement)

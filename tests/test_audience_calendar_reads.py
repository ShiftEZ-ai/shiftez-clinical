"""Audience windows reuse calendar metadata while evaluating every occurrence's local date."""
from datetime import date

import pytest
from sqlalchemy import event as sql_events

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.services import audiences, events
from .governance_support import identity
from .test_events import event, repeat


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("linked", [False, True], ids=["unlinked", "linked"])
def test_occurrence_viewer_calendar_is_loaded_once_per_read(request, database, linked):
    h = request.getfixturevalue(database)
    person_id = h.seed["people"][0] if linked else None
    identity_id = identity(h, "calendar_viewer", ["staff"], person_id)
    viewer = Actor(identity_id, h.seed["department_id"], h.seed["organisation_id"],
                   "Synthetic calendar viewer", frozenset({"staff"}), person_id, "")
    row = event(h, repeat=repeat(), repeat_until="2026-10-19", audience={"where": {"conditions": [
        {"field": "roles", "values": ["staff"]},
        {"field": "date", "op": "is", "value": "2026-10-11"}]}})
    statements = []

    def sql(conn, cursor, statement, parameters, execution_context, executemany):
        statements.append(" ".join(statement.lower().split()))

    sql_events.listen(h.app.state.engine, "before_cursor_execute", sql)
    try:
        with transaction(h.app.state.engine) as db:
            results = events.viewer_occurrence_audiences(db, viewer, date(2026, 10, 1), date(2026, 11, 1))
            assert [(occurrence.event_id, visible) for occurrence, visible in results] == [
                (row["id"], False), (row["id"], True), (row["id"], False)]
    finally:
        sql_events.remove(h.app.state.engine, "before_cursor_execute", sql)
    assert sum(" from organisations " in statement for statement in statements) == 1
    if not linked:
        for cls in (m.Person, m.Membership, m.Eligibility, m.ProgrammeMembership, m.CapabilityGrant,
                    m.Category, m.Capability, m.Programme):
            assert not any(f" from {cls.__tablename__} " in statement for statement in statements)


def test_empty_profile_context_reuses_configured_calendar_without_more_sql(h):
    with transaction(h.app.state.engine, write=True) as db:
        organisation = db.get(m.Organisation, h.seed["organisation_id"])
        organisation.timezone, organisation.week_start = "Europe/London", 0
    viewer = Actor(h.seed["identities"]["coordinator"], h.seed["department_id"], h.seed["organisation_id"],
                   "Synthetic unlinked viewer", frozenset({"coordinator"}), None, "")
    statements = []

    def sql(conn, cursor, statement, parameters, execution_context, executemany):
        statements.append(statement)

    sql_events.listen(h.app.state.engine, "before_cursor_execute", sql)
    try:
        with transaction(h.app.state.engine) as db:
            context = audiences.profile_context(db, viewer.department_id, viewer.organisation_id, set())
            assert context == {"organisation": {"timezone": "Europe/London", "week_start": 0}}
            assert len(statements) == 1
            statements.clear()
            for day in (date(2026, 10, 24), date(2026, 10, 25), date(2026, 10, 26)):
                _, person_id, holds, _ = audiences.viewer(db, viewer, day, context=context, tags={}, bindings=[])
                calendar = holds["_condition_context"]
                assert person_id is None and holds["date"] == day
                assert calendar.timezone == "Europe/London" and calendar.week_start == 0
                assert calendar.today == calendar.duty_day == day
            assert not statements
    finally:
        sql_events.remove(h.app.state.engine, "before_cursor_execute", sql)

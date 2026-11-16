"""Audience filtering precedes paging and batches reuse the one set of current audience facts."""
from datetime import date
import pytest
from sqlalchemy import event, select
from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.services import audiences


@pytest.mark.postgres
def test_content_filters_audiences_in_sql_before_paging_postgres(pg):
    day = date(2026, 10, 3)
    with transaction(pg.app.state.engine, write=True) as db:
        staff = db.get(m.Identity, pg.seed["identities"]["staff"])
        actor = Actor(staff.id, pg.seed["department_id"], pg.seed["organisation_id"], staff.display_name,
                      frozenset({"staff"}), staff.person_id, "")
        db.add_all(m.DepartmentContent(department_id=actor.department_id, name=f"A private {i:03}",
            starts_on=day, audience={"everyone": False, "roles": ["coordinator"]}) for i in range(250))
        db.add(m.DepartmentContent(department_id=actor.department_id, name="Z for staff", starts_on=day,
            audience={"everyone": False, "where": {"match": "all", "conditions": [
                {"field": "roles", "op": "any", "values": ["staff"]},
                {"field": "person_ids", "op": "any", "values": [staff.person_id]}]}}))
        db.flush()
        query = select(m.DepartmentContent).where(m.DepartmentContent.department_id == actor.department_id)
        visible = audiences.visible_content(db, actor, day, query).order_by(m.DepartmentContent.name).limit(1)
        assert [row.name for row in db.scalars(visible)] == ["Z for staff"]


def test_recipient_context_reuses_current_facts_for_many_notices(h):
    day = date(2026, 10, 3)
    with transaction(h.app.state.engine) as db:
        context = audiences.recipient_context(db, h.seed["department_id"], day)
        staff = db.get(m.Identity, h.seed["identities"]["staff"])
        statements = []

        def sql(conn, cursor, statement, parameters, execution_context, executemany):
            statements.append(statement)

        event.listen(h.app.state.engine, "before_cursor_execute", sql)
        try:
            for _ in range(10):
                roles = audiences.recipients(db, h.seed["department_id"], {"roles": ["staff"]}, day, context)
                named = audiences.recipients(db, h.seed["department_id"],
                    {"roles": ["coordinator"], "person_ids": [staff.person_id]}, day, context)
                tree = audiences.recipients(db, h.seed["department_id"], {"where": {"match": "all", "conditions": [
                    {"field": "roles", "op": "any", "values": ["staff"]}]}}, day, context)
                assert roles == tree == {staff.id}
                assert staff.id in named and h.seed["identities"]["coordinator"] in named
                assert h.seed["identities"]["admin"] not in roles
        finally:
            event.remove(h.app.state.engine, "before_cursor_execute", sql)
        assert not statements

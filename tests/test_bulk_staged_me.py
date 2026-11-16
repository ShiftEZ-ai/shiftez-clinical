"""The signed-in user's staged-change badge is scoped and read-only."""
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_bulk_staged import staged_change


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_me_counts_zero_one_two_and_excludes_other_owners_departments_and_inactive_rows(database, request):
    h = request.getfixturevalue(database)
    assert s.Me.model_validate(h.get("coordinator", "/api/me")).bulk_staged_count == 0
    _, first = staged_change(h)
    assert h.get("coordinator", "/api/me")["bulk_staged_count"] == 1
    staged_change(h)
    assert h.get("coordinator", "/api/me")["bulk_staged_count"] == 2
    with transaction(h.app.state.engine, write=True) as db:
        other_department = m.Department(organisation_id=h.seed["organisation_id"], name="Synthetic other department")
        db.add(other_department)
        db.flush()
        mine, other = h.seed["identities"]["coordinator"], h.seed["identities"]["credentialer"]
        current = h.seed["department_id"]
        for owner, department, status, expiry in [
            (other, current, "active", m.now() + timedelta(days=1)),
            (mine, other_department.id, "active", m.now() + timedelta(days=1)),
            (mine, current, "active", m.now() - timedelta(seconds=1)),
            (mine, current, "active", m.now()),
            *((mine, current, status, m.now() + timedelta(days=1))
              for status in ("dropped", "applied", "expired", "waiting")),
        ]:
            db.add(m.BulkStaged(owner_id=owner, department_id=department, status=status,
                                expires_at=expiry, payload=first["payload"]))
    assert h.get("coordinator", "/api/me")["bulk_staged_count"] == 2
    assert h.get("credentialer", "/api/me")["bulk_staged_count"] == 1
    h.command("coordinator", "bulk.staged.drop", {"reason": "Drop the synthetic retained change"},
              first["id"], first["revision"])
    assert h.get("coordinator", "/api/me")["bulk_staged_count"] == 1


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_me_uses_one_indexed_count_without_expiring_rows_or_adding_queries_to_lists(database, request, monkeypatch):
    from apps.api.services import bulk_staged

    h = request.getfixturevalue(database)
    _, staged = staged_change(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.BulkStaged, staged["id"])
        row.expires_at = m.now() - timedelta(seconds=1)
        before = (row.status, row.revision,
                  db.scalar(select(func.count()).select_from(m.DefinitionHistory)),
                  db.scalar(select(func.count()).select_from(m.AuditEvent)))

    def no_lazy_expiry(*args):
        pytest.fail("The signed-in identity read must not expire staged changes")

    monkeypatch.setattr(bulk_staged, "_expire", no_lazy_expiry)
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        if "bulk_staged" in statement.lower():
            statements.append((statement, parameters))

    engine = h.app.state.engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert h.get("coordinator", "/api/me")["bulk_staged_count"] == 0
        assert len(statements) == 2
        statement, parameters = next(entry for entry in statements if "bulk_staged.owner_id =" in entry[0].lower())
        assert "select count(*)" in statement.lower() and "payload" not in statement.lower()
        review_statement = next(sql for sql, _ in statements if "bulk_staged.owner_id !=" in sql.lower())
        assert review_statement.lower().startswith("select ")
        statements.clear()
        h.get("coordinator", "/api/catalogues?boot=true")
        h.get("coordinator", "/api/workspaces")
        assert statements == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    index = next(index for index in m.BulkStaged.__table__.indexes if index.name == "ix_bulk_staged_owner_status")
    assert [column.name for column in index.columns] == ["department_id", "owner_id", "status", "expires_at"]
    with transaction(engine) as db:
        row = db.get(m.BulkStaged, staged["id"])
        assert (row.status, row.revision,
                db.scalar(select(func.count()).select_from(m.DefinitionHistory)),
                db.scalar(select(func.count()).select_from(m.AuditEvent))) == before
        if engine.dialect.name == "sqlite":
            plan = db.connection().exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters).all()
            assert any("COVERING INDEX ix_bulk_staged_owner_status" in entry[3] for entry in plan), plan


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_me_review_count_matches_the_lists_waiting_rows_and_excludes_unrelated_stages(database, request):
    from .governance_support import identity
    from .test_bulk import REASON

    h = request.getfixturevalue(database)
    identity(h, "review_owner", ["credentialer"], person=h.seed["people"][0])
    assert h.get("credentialer", "/api/me")["bulk_review_count"] == 0
    service = h.configure("services", {"name": "Counted review clinic", "starts_on": "2020-01-01"})["record"]
    body = {"item_kind": "eligibility.create", "reason": REASON, "resolutions": {"0": "send_for_approval"},
            "items": [{"payload": {"person_id": h.seed["people"][0], "service_id": service["id"],
                                   "starts_on": "2026-10-01"}}]}
    first = h.command("review_owner", "bulk.apply", body)["reviews"][0]
    reviewer = h.get("credentialer", "/api/me")
    assert reviewer["bulk_review_count"] == 1
    assert h.get("review_owner", "/api/me")["bulk_review_count"] == 0
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed["organisation_id"], name="Other review department")
        db.add(other)
        db.flush()
        current, owner, mine = h.seed["department_id"], h.seed["identities"]["review_owner"], reviewer["identity_id"]
        for department, who, status, role, expiry in [
            (current, owner, "waiting", "credentialer", m.now() + timedelta(days=1)),
            (other.id, owner, "waiting", "credentialer", m.now() + timedelta(days=1)),
            (current, mine, "waiting", "credentialer", m.now() + timedelta(days=1)),
            (current, owner, "waiting", "approver", m.now() + timedelta(days=1)),
            (current, owner, "waiting", "credentialer", m.now() - timedelta(seconds=1)),
            (current, owner, "waiting", "credentialer", m.now()),
            *((current, owner, status, "credentialer", m.now() + timedelta(days=1))
              for status in ("active", "dropped", "expired", "applied")),
        ]:
            db.add(m.BulkStaged(department_id=department, owner_id=who, status=status, review_role=role,
                                expires_at=expiry, payload=first["payload"]))
    reviewer = h.get("credentialer", "/api/me")
    waiting = [row for row in h.get("credentialer", "/api/bulk/staged")["items"]
               if row["owner_id"] != reviewer["identity_id"]]
    assert reviewer["bulk_staged_count"] == 0
    assert reviewer["bulk_review_count"] == len(waiting) == 2


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_me_review_count_preserves_independence_with_batched_queries_as_backlog_grows(database, request):
    from .governance_support import identity

    h = request.getfixturevalue(database)
    other_person, reviewer_person = h.seed["people"][:2]
    reviewer_id = identity(h, "linked_reviewer", ["coordinator"], person=reviewer_person)
    owner_id = identity(h, "review_owner", ["coordinator"], person=other_person)
    alternate_id = identity(h, "reviewer_other_sign_in", ["coordinator"], person=reviewer_person)

    with transaction(h.app.state.engine, write=True) as db:
        targets = []
        for person in (other_person, reviewer_person):
            row = m.Availability(department_id=h.seed["department_id"], person_id=person,
                kind="actual_absence", start=m.now(), end=m.now() + timedelta(hours=1),
                recorded_by=owner_id, private_reason="Synthetic review count fixture")
            db.add(row)
            targets.append(row)
        db.flush()
        payload = {"item_kind": "availability.retract", "reason": "Independent review count",
            "items": [{"target_id": targets[0].id, "expected_revision": targets[0].revision, "payload": {}}]}

        def stage(owner, value):
            row = m.BulkStaged(department_id=h.seed["department_id"], owner_id=owner,
                status="waiting", review_role="coordinator", expires_at=m.now() + timedelta(days=1), payload=value)
            db.add(row)
            db.flush()
            return row.id

        allowed = {stage(owner_id, payload)}
        own = stage(reviewer_id, payload)
        excluded = {
            stage(alternate_id, payload),
            stage(owner_id, {"item_kind": "availability.record", "reason": "Own person excluded",
                "items": [{"payload": {"person_id": reviewer_person}}]}),
            stage(owner_id, {**payload, "items": [{"target_id": targets[1].id, "payload": {}}]}),
        }

    h.client("linked_reviewer")  # Sign-in queries are outside the measured reads.
    queries = []

    def capture(connection, cursor, statement, parameters, context, many):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select") and "pg_advisory" not in normalized and "pg_try_advisory" not in normalized:
            queries.append(normalized)

    event.listen(h.app.state.engine, "before_cursor_execute", capture)
    try:
        small = h.get("linked_reviewer", "/api/me")
        small_queries = list(queries)
        with transaction(h.app.state.engine, write=True) as db:
            for number in range(30):
                owner = m.Identity(issuer="local-development", subject=f"count-owner-{number}",
                                   display_name=f"Synthetic review owner {number}")
                db.add(owner)
                db.flush()
                allowed.add(stage(owner.id, payload))
        queries.clear()
        large = h.get("linked_reviewer", "/api/me")
        large_queries = list(queries)
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", capture)
    assert small["bulk_review_count"] == 1
    assert large["bulk_review_count"] == 31
    assert len(small_queries) == len(large_queries), large_queries
    assert sum("from bulk_staged" in query for query in large_queries) == 2
    assert sum("from availability" in query for query in large_queries) == 1
    assert sum("from identities" in query and "identities.id in" in query for query in large_queries) == 1
    request.node.user_properties.extend((("me_selects_small_backlog", len(small_queries)),
                                        ("me_selects_large_backlog", len(large_queries))))
    listed = h.get("linked_reviewer", "/api/bulk/staged")["items"]
    reviewer_rows = {row["id"] for row in listed if row["owner_id"] != reviewer_id}
    assert reviewer_rows == allowed and large["bulk_review_count"] == len(reviewer_rows)
    assert {row["id"] for row in listed} == allowed | {own}
    assert excluded.isdisjoint(row["id"] for row in listed)

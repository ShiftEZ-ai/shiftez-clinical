"""Receipt evidence distinguishes inbox delivery, reading and an explicit acknowledgement."""
from datetime import date, timedelta

import pytest
from sqlalchemy import delete, event, func, insert, select

from apps.api import models as m
from apps.api.commands import NON_SERIALIZED_COMMANDS
from apps.api.db import retry_write, transaction
from apps.api.serialization import record
from apps.api.services import announcements, audiences
from apps.worker import main as worker
from tests.outbox_support import dispatch_batch_after_undo, process_one_after_undo as process_one
from packages.contracts.schemas import CONTENT_PAGE_SIZE, ROLE_NAMES
from packages.domain.time import aware
from .clock_support import freeze_clock
from .governance_support import identity


def notice(h, audience=None, *, importance="normal", notify=False, name="Receipt notice", starts_on="2020-01-01"):
    return h.command("coordinator", "catalogue.create", {"catalogue": "department_content", "reason": "Tell the team",
        "value": {"name": name, "description": "Synthetic notice", "starts_on": starts_on, "kind": "announcement",
                  "audience": audience or {"everyone": True}, "importance": importance, "notify": notify}})["record"]


def required_links(h):
    # Older saved links remain readable; the current contract rejects creating these combinations.
    with transaction(h.app.state.engine, write=True) as db:
        rows = [m.DepartmentContent(department_id=h.seed['department_id'], name=f'A staff link {index}', kind='link',
            url='https://example.org/resource', starts_on=date(2020, 1, 1), audience={'roles': ['staff']},
            importance='must_acknowledge') for index in range(4)]
        db.add_all(rows)
        db.flush()
        return [record(row) for row in rows]


def notes(h, content_id, role="staff"):
    return [n for n in h.get(role, "/api/notifications")["notifications"] if n["source_id"] == content_id]


def receipts(h, content_id, role="coordinator", query=""):
    return h.get(role, f"/api/content/{content_id}/receipts{query}")


def counts(h, content_id):
    return next(row["receipts"] for row in h.get("coordinator", "/api/catalogues")["department_content"]
                if row["id"] == content_id)


def seen(h, content_ids, role="staff"):
    return h.command(role, "department_content.seen", {"ids": content_ids})


def test_get_content_does_not_write_receipts_notifications_or_daily_reconciliation(h):
    row = notice(h, {"roles": ["staff"]}, notify=True)
    engine = h.app.state.engine
    process_one(engine)
    identity(h, "late_home_reader", ["staff"])
    with transaction(engine, write=True) as db:
        db.get(m.DepartmentContent, row["id"]).receipts_checked_on = date(2020, 1, 1)
    before = receipts(h, row["id"])["receipts"]
    before_notes = notes(h, row["id"])
    h.client("late_home_reader")
    statements = []

    def record_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        shown = next(item for item in h.get("staff", "/api/content")["items"] if item["id"] == row["id"])
        late = next(item for item in h.get("late_home_reader", "/api/content")["items"] if item["id"] == row["id"])
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
    assert shown["receipt"]["read_at"] is None and late["receipt"] is None
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE", "BEGIN IMMEDIATE")) for sql in statements)
    assert receipts(h, row["id"])["receipts"] == before and notes(h, row["id"]) == before_notes
    with transaction(engine) as db:
        assert db.get(m.DepartmentContent, row["id"]).receipts_checked_on == date(2020, 1, 1)


def test_seen_ignores_ineligible_content_and_validates_the_page_bound(h):
    row = notice(h, {"roles": ["staff"]}, importance="must_acknowledge")
    hidden = notice(h, {"roles": ["coordinator"]}, name="Other audience")
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed["organisation_id"], name="Other synthetic department")
        db.add(other)
        db.flush()
        invalid = [m.DepartmentContent(department_id=department_id, name=name, starts_on=start,
            ends_on=end, active=active, kind=kind, audience={"roles": ["staff"]})
            for name, department_id, start, end, active, kind in [
                ("Other department", other.id, date(2020, 1, 1), None, True, "announcement"),
                ("Inactive", h.seed["department_id"], date(2020, 1, 1), None, False, "announcement"),
                ("Future", h.seed["department_id"], m.now().date() + timedelta(days=1), None, True, "announcement"),
                ("Expired", h.seed["department_id"], date(2020, 1, 1), m.now().date(), True, "announcement"),
                ("Resource", h.seed["department_id"], date(2020, 1, 1), None, True, "link")]]
        db.add_all(invalid)
        db.flush()
        invalid_ids = [item.id for item in invalid] + [hidden["id"], m.uid()]
    seen(h, [row["id"], *invalid_ids])
    with transaction(h.app.state.engine) as db:
        own = list(db.scalars(select(m.AnnouncementReceipt).where(
            m.AnnouncementReceipt.identity_id == h.seed["identities"]["staff"],
            m.AnnouncementReceipt.content_id.in_([row["id"], *invalid_ids]))))
        assert len(own) == 1 and own[0].content_id == row["id"]
        assert own[0].read_at and own[0].acknowledged_at is None
    seen(h, [row["id"]] * CONTENT_PAGE_SIZE)
    for ids in ([], [row["id"]] * (CONTENT_PAGE_SIZE + 1), ["invalid-id"]):
        h.command("staff", "department_content.seen", {"ids": ids}, status=422)


def test_opening_inbox_and_mark_all_read_never_acknowledge(h):
    row = notice(h, {"roles": ["staff"]}, importance="must_acknowledge", notify=True)
    process_one(h.app.state.engine)
    note = notes(h, row["id"])[0]
    assert note["kind"] == "announcement" and note["source_kind"] == "department_content"
    assert note["acknowledgement_required"] is True
    assert note["read_at"] is None and note["acknowledged_at"] is None
    h.command("staff", "notification.ack", target=note["id"], revision=note["revision"])
    opened = notes(h, row["id"])[0]
    assert opened["read_at"] and opened["acknowledged_at"] is None
    h.command("staff", "notification.acknowledge", {"reason": "Stale view"}, note["id"], note["revision"], status=409)
    h.command("staff", "notification.ack_all")
    assert counts(h, row["id"]) == {"audience": 1, "delivered": 1, "read": 1, "acknowledged": 0}
    h.command("staff", "notification.acknowledge", target=opened["id"], revision=opened["revision"], status=422)
    h.command("staff", "notification.acknowledge", {"reason": "Explicit acknowledgement"}, opened["id"], opened["revision"])
    acknowledged = notes(h, row["id"])[0]
    assert acknowledged["read_at"] == opened["read_at"] and acknowledged["acknowledged_at"]
    assert counts(h, row["id"])["acknowledged"] == 1


@pytest.mark.parametrize("backend", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_home_read_preserves_inbox_acknowledgement_but_keeps_receipt_and_consent_revision_guards(request, backend):
    h = request.getfixturevalue(backend)
    row = notice(h, {"roles": ["staff"]}, importance="must_acknowledge", notify=True)
    process_one(h.app.state.engine)
    inbox = notes(h, row["id"])[0]
    own = receipts(h, row["id"])["receipts"][0]

    read = seen(h, [row["id"]])["receipts"][row["id"]]
    assert read["read_at"] and read["acknowledged_at"] is None and read["revision"] == own["revision"] + 1
    updated = notes(h, row["id"])[0]
    assert updated["read_at"] and updated["revision"] == inbox["revision"]
    h.command("staff", "notification.acknowledge", {"reason": "Stale Home receipt"}, own["id"], own["revision"], status=409)

    h.command("staff", "notification.acknowledge", {"reason": "I acknowledge"}, inbox["id"], inbox["revision"])
    acknowledged = notes(h, row["id"])[0]
    assert acknowledged["acknowledged_at"] and acknowledged["revision"] == inbox["revision"] + 1
    assert counts(h, row["id"]) == {"audience": 1, "delivered": 1, "read": 1, "acknowledged": 1}
    h.command("staff", "notification.acknowledge", {"reason": "Stale inbox action"}, inbox["id"], inbox["revision"], status=409)


def test_mark_all_sets_read_for_required_notices_but_never_acknowledges(h):
    required = notice(h, {"roles": ["staff"]}, importance="must_acknowledge", notify=True)
    normal = notice(h, {"roles": ["staff"]}, notify=True, name="Information only")
    process_one(h.app.state.engine)
    result = h.command("staff", "notification.ack_all", {"reason": "Read the inbox"})
    assert result["read"] == result["acknowledged"] == 2  # legacy response key remains compatible
    for row in (required, normal):
        note = notes(h, row["id"])[0]
        assert note["read_at"] and note["acknowledged_at"] is None
        assert counts(h, row["id"])["acknowledged"] == 0
    assert notes(h, normal["id"])[0]["acknowledgement_required"] is False


def test_home_only_notices_have_receipts_and_can_be_acknowledged_without_inbox(h):
    row = notice(h, {"roles": ["staff"]}, importance="must_acknowledge")
    before = receipts(h, row["id"])["receipts"]
    assert len(before) == 1 and before[0]["delivered_at"] is None and before[0]["read_at"] is None
    assert notes(h, row["id"]) == []
    shown = next(item for item in h.get("staff", "/api/content")["items"] if item["id"] == row["id"])
    assert shown["receipt"]["read_at"] is None
    seen(h, [row["id"]])
    shown = next(item for item in h.get("staff", "/api/content")["items"] if item["id"] == row["id"])
    own = shown["receipt"]
    assert own["read_at"] and own["acknowledged_at"] is None
    assert "receipts" not in shown  # staff do not get author projections
    h.command("staff", "notification.acknowledge", {"reason": "I acknowledge"}, own["id"], own["revision"])
    assert counts(h, row["id"]) == {"audience": 1, "delivered": 0, "read": 1, "acknowledged": 1}


def test_every_role_can_record_seen_idempotently_then_acknowledge_its_own_required_notice(h):
    for role in ROLE_NAMES:
        identity(h, "receipt_" + role, [role])
    row = notice(h, importance="must_acknowledge", notify=True)
    process_one(h.app.state.engine)
    for role in ROLE_NAMES:
        username = "receipt_" + role
        note = notes(h, row["id"], username)[0]
        assert note["read_at"] is None
        seen(h, [row["id"], row["id"]], username)
        note = notes(h, row["id"], username)[0]
        assert note["read_at"] and note["acknowledged_at"] is None
        seen(h, [row["id"]], username)
        assert notes(h, row["id"], username)[0] == note
        h.command(username, "notification.acknowledge", {"reason": "I acknowledge"}, note["id"], note["revision"])
        acknowledged = notes(h, row["id"], username)[0]
        assert acknowledged["read_at"] and acknowledged["acknowledged_at"]


def test_information_only_notices_refuse_explicit_acknowledgement(h):
    row = notice(h, {"roles": ["staff"]}, notify=True)
    process_one(h.app.state.engine)
    note = notes(h, row["id"])[0]
    refused = h.command("staff", "notification.acknowledge", {"reason": "Not required"}, note["id"], note["revision"], status=409)
    assert refused["error"]["code"] == "ACKNOWLEDGEMENT_NOT_REQUIRED"
    unchanged = notes(h, row["id"])[0]
    assert unchanged["read_at"] is None and unchanged["acknowledged_at"] is None


@pytest.mark.parametrize("previous_reminder", [False, True])
def test_no_pending_reminders_preserve_the_cooldown_for_a_later_recipient(h, previous_reminder):
    row = notice(h, {"roles": ["staff"]})
    seen(h, [row["id"]])
    if previous_reminder:
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.DepartmentContent, row["id"]).last_reminded_at = m.now() - timedelta(days=2)
    previous = next(item["last_reminded_at"] for item in h.get("coordinator", "/api/catalogues")["department_content"]
                    if item["id"] == row["id"])
    for _ in range(2):
        empty = h.command("coordinator", "department_content.remind", {"reason": "Everyone already read"}, row["id"], row["revision"])
        assert empty["reminded"] == 0 and empty["record"]["last_reminded_at"] == previous
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Outbox).where(m.Outbox.kind == "announcement")) == 0
    identity(h, "later_recipient", ["staff"])
    queued = h.command("coordinator", "department_content.remind", {"reason": "New unread recipient"}, row["id"], row["revision"])
    assert queued["reminded"] == 1 and queued["record"]["last_reminded_at"] != previous
    h.command("coordinator", "department_content.remind", {"reason": "Repeated request"}, row["id"], row["revision"], status=429)


@pytest.mark.parametrize("notify", [False, True])
def test_active_notice_adds_late_joiners_without_duplicate_receipts(h, notify):
    row = notice(h, {"roles": ["staff"]}, notify=notify)
    process_one(h.app.state.engine)
    original = receipts(h, row["id"])["receipts"][0]
    other = next(pid for pid in h.seed["people"] if pid != h.get("staff", "/api/me")["person_id"])
    late_id = identity(h, "late_reader", ["staff"], other)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.DepartmentContent, row["id"]).receipts_checked_on = date(2020, 1, 1)
    process_one(h.app.state.engine)
    listed = receipts(h, row["id"])["receipts"]
    assert {item["identity_id"] for item in listed} == {original["identity_id"], late_id}
    assert next(item["id"] for item in listed if item["identity_id"] == original["identity_id"]) == original["id"]
    late = next(item for item in listed if item["identity_id"] == late_id)
    assert late["read_at"] is None
    assert counts(h, row["id"]) == {"audience": 2, "delivered": 2 * int(notify), "read": 0, "acknowledged": 0}
    seen(h, [row["id"]], "late_reader")
    assert counts(h, row["id"]) == {"audience": 2, "delivered": 2 * int(notify), "read": 1, "acknowledged": 0}
    assert len(notes(h, row["id"], "late_reader")) == int(notify)
    process_one(h.app.state.engine)
    assert receipts(h, row["id"])["total"] == 2
    assert len(notes(h, row["id"], "late_reader")) == int(notify)


@pytest.mark.postgres
def test_seen_does_not_take_global_writer_or_scan_department_recipients(pg, monkeypatch):
    freeze_clock(monkeypatch)  # Keep rate-limit cleanup out of this statement-count guard.
    engine = pg.app.state.engine
    pg.client("staff")
    statements = []
    contexts = []
    recipient_context = audiences.recipient_context

    def capture_context(*args, **kwargs):
        contexts.append(True)
        return recipient_context(*args, **kwargs)

    monkeypatch.setattr(audiences, "recipient_context", capture_context)

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    def seed_people(count, offset):
        with transaction(engine, write=True) as db:
            organisation_id = db.get(m.Department, pg.seed["department_id"]).organisation_id
            people = [{"id": m.uid(), "organisation_id": organisation_id, "display_name": f"Seen scale person {offset + i}"}
                      for i in range(count)]
            identities = [{"id": m.uid(), "issuer": "local-development", "subject": f"seen-scale-{offset + i}",
                           "display_name": person["display_name"], "person_id": person["id"]}
                          for i, person in enumerate(people)]
            db.execute(insert(m.Person), people)
            db.execute(insert(m.Identity), identities)
            db.execute(insert(m.RoleBinding), [{"identity_id": item["id"], "department_id": pg.seed["department_id"],
                "role": "staff", "starts_on": date(2020, 1, 1)} for item in identities])

    def notice_row(name):
        with transaction(engine, write=True) as db:
            row = m.DepartmentContent(department_id=pg.seed["department_id"], name=name, description="Scale notice",
                starts_on=date(2020, 1, 1), audience={"roles": ["staff"]})
            db.add(row)
            db.flush()
            return row.id

    seed_people(9, 0)
    warmup_id = notice_row("Seen scale warmup")
    seen(pg, [warmup_id])
    small_id = notice_row("Seen scale 10")
    event.listen(engine, "before_cursor_execute", capture)
    try:
        before = len(statements)
        seen(pg, [small_id])
        small = statements[before:]
        seed_people(140, 9)
        large_id = notice_row("Seen scale 150")
        before = len(statements)
        seen(pg, [large_id])
        large = statements[before:]
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    lock_statements = [sql.lower() for sql in small + large if "advisory_xact_lock" in sql.lower()]
    assert not lock_statements
    assert not contexts
    assert len(large) == len(small), (len(small), len(large))


def test_non_serialized_commands_only_change_their_own_rows(h):
    row = notice(h, {"roles": ["staff"]})
    engine = h.app.state.engine

    def state():
        with transaction(engine) as db:
            input_change_rows = sum(len(event.detail.get("input_changes") or []) for event in db.scalars(
                select(m.AuditEvent).where(m.AuditEvent.department_id == h.seed["department_id"])))
            workspaces = tuple(tuple(item) for item in db.execute(select(m.Workspace.id, m.Workspace.revision,
                m.Workspace.content_revision).where(m.Workspace.department_id == h.seed["department_id"]).order_by(m.Workspace.id)))
            policies = tuple(tuple(item) for item in db.execute(select(m.Policy.id, m.Policy.revision)
                .where(m.Policy.department_id == h.seed["department_id"]).order_by(m.Policy.id)))
            facts = tuple(tuple(item) for item in db.execute(select(m.SchedulingFact.id, m.SchedulingFact.revision)
                .where(m.SchedulingFact.department_id == h.seed["department_id"]).order_by(m.SchedulingFact.id)))
            return input_change_rows, workspaces, policies, facts

    for kind in sorted(NON_SERIALIZED_COMMANDS):
        before = state()
        h.command("staff", kind, {"ids": [row["id"]]})
        assert state() == before


def test_people_leaving_audience_keep_evidence_but_leave_current_totals(h):
    row = notice(h, {"roles": ["staff"]}, notify=True)
    process_one(h.app.state.engine)
    seen(h, [row["id"]])
    original = receipts(h, row["id"])["receipts"][0]
    with transaction(h.app.state.engine, write=True) as db:
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == original["identity_id"],
            m.RoleBinding.role == "staff"))
        binding.revoked = True
    assert counts(h, row["id"]) == {"audience": 0, "delivered": 0, "read": 0, "acknowledged": 0}
    historical = receipts(h, row["id"])["receipts"][0]
    assert historical["id"] == original["id"] and historical["read_at"] == original["read_at"]
    assert historical["in_audience"] is False


def test_receipts_are_author_only_paged_and_acknowledgement_is_own_only(h):
    row = notice(h, importance="must_acknowledge", notify=True)
    process_one(h.app.state.engine)
    for role in ("staff", "approver", "operator", "auditor", "credentialer"):
        h.get(role, f"/api/content/{row['id']}/receipts", status=403)
    first = receipts(h, row["id"], "admin", "?page=1&page_size=2")
    second = receipts(h, row["id"], query="?page=2&page_size=2")
    assert first["total"] > 2 and len(first["receipts"]) == 2
    assert not ({r["id"] for r in first["receipts"]} & {r["id"] for r in second["receipts"]})
    all_rows = receipts(h, row["id"])["receipts"]
    account_only = next(item for item in all_rows if item["person"] is None)
    assert account_only["identity"]["id"] == account_only["identity_id"]
    assert account_only["identity"]["display_name"]
    h.get("coordinator", f"/api/content/{row['id']}/receipts?page_size=201", status=422)
    other = next(item for item in receipts(h, row["id"])["receipts"]
                 if item["identity_id"] != h.seed["identities"]["staff"])
    h.command("staff", "notification.acknowledge", {"reason": "Not mine"}, other["id"], other["revision"], status=404)
    other_note = notes(h, row["id"], "admin")[0]
    h.command("staff", "notification.ack", {"reason": "Not mine"}, other_note["id"], other_note["revision"], status=404)
    with transaction(h.app.state.engine, write=True) as db:
        other_department = m.Department(organisation_id=db.get(m.Department, h.seed["department_id"]).organisation_id,
                                        name="Separate synthetic department")
        db.add(other_department)
        db.flush()
        other_department_id = other_department.id
    identity(h, "other_admin", ["admin"], department=other_department_id)
    h.get("other_admin", f"/api/content/{row['id']}/receipts", status=404)


@pytest.mark.parametrize("importance,should_remind", [("normal", False), ("must_acknowledge", True)])
def test_reminders_target_unread_or_unacknowledged_and_enforce_configured_interval(h, importance, should_remind):
    other = next(pid for pid in h.seed["people"] if pid != h.get("staff", "/api/me")["person_id"])
    identity(h, "unread_colleague", ["staff"], other)
    row = notice(h, {"roles": ["staff"]}, importance=importance, notify=True)
    process_one(h.app.state.engine)
    note = notes(h, row["id"])[0]
    h.command("staff", "notification.ack", {"reason": "Opened"}, note["id"], note["revision"])
    h.command("staff", "department_content.remind", {"reason": "Not an author"}, row["id"], row["revision"], status=403)
    h.command("coordinator", "department_content.remind", {"reason": "Follow up"}, row["id"], row["revision"])
    process_one(h.app.state.engine)
    assert len(notes(h, row["id"])) == 1 + int(should_remind)
    assert len(notes(h, row["id"], "unread_colleague")) == 2
    current = next(item for item in h.get("coordinator", "/api/catalogues")["department_content"] if item["id"] == row["id"])
    h.command("coordinator", "department_content.remind", {"reason": "Too soon"}, row["id"], current["revision"], status=429)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).announcement_reminder_interval_hours = 2
        db.get(m.DepartmentContent, row["id"]).last_reminded_at = m.now() - timedelta(hours=3)
    h.command("coordinator", "department_content.remind", {"reason": "Interval elapsed"}, row["id"], current["revision"])
    process_one(h.app.state.engine)
    assert len(notes(h, row["id"])) == 1 + 2 * int(should_remind)


@pytest.mark.parametrize("importance", ["normal", "must_acknowledge"])
def test_automatic_reminders_are_opt_in_and_rechecked_daily(h, importance):
    row = notice(h, {"roles": ["staff"]}, importance=importance)
    engine = h.app.state.engine
    process_one(engine)
    assert notes(h, row["id"]) == []
    if importance == "must_acknowledge":
        seen(h, [row["id"]])  # reading alone does not stop a required acknowledgement reminder
    with transaction(engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).announcement_auto_reminder_days = 3
        db.get(m.DepartmentContent, row["id"]).receipts_checked_on = date(2020, 1, 1)
    process_one(engine)
    assert len(notes(h, row["id"])) == 1
    process_one(engine)
    assert len(notes(h, row["id"])) == 1
    with transaction(engine, write=True) as db:
        content = db.get(m.DepartmentContent, row["id"])
        content.receipts_checked_on = date(2020, 1, 1)
        content.last_reminded_at = m.now() - timedelta(days=2)
    process_one(engine)
    assert len(notes(h, row["id"])) == 1  # auto interval has not elapsed
    with transaction(engine, write=True) as db:
        content = db.get(m.DepartmentContent, row["id"])
        content.receipts_checked_on = date(2020, 1, 1)
        content.last_reminded_at = m.now() - timedelta(days=4)
    process_one(engine)
    assert len(notes(h, row["id"])) == 2
    if importance == "must_acknowledge":
        note = notes(h, row["id"])[0]
        h.command("staff", "notification.acknowledge", {"reason": "Now acknowledged"}, note["id"], note["revision"])
        with transaction(engine, write=True) as db:
            content = db.get(m.DepartmentContent, row["id"])
            content.receipts_checked_on = date(2020, 1, 1)
            content.last_reminded_at = m.now() - timedelta(days=4)
        process_one(engine)
        assert len(notes(h, row["id"])) == 2


def test_automatic_reminder_becoming_due_after_daily_audience_check_is_sent(h):
    row = notice(h, {"roles": ["staff"]})
    engine = h.app.state.engine
    process_one(engine)
    with transaction(engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).announcement_auto_reminder_days = 1
        content = db.get(m.DepartmentContent, row["id"])
        assert content.receipts_checked_on == m.now().date()
        content.last_reminded_at = m.now() - timedelta(days=1, minutes=1)
    process_one(engine)
    assert len(notes(h, row["id"])) == 1


@pytest.mark.parametrize("backend", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("importance", ["normal", "must_acknowledge"])
@pytest.mark.parametrize("unchecked", [False, True])
def test_satisfied_auto_reminders_leave_two_dispatches_idle(request, backend, importance, unchecked, monkeypatch):
    h = request.getfixturevalue(backend)
    row = notice(h, {"roles": ["staff"]}, importance=importance, notify=True)
    engine = h.app.state.engine
    process_one(engine)
    seen(h, [row["id"]])
    if importance == "must_acknowledge":
        note = notes(h, row["id"])[0]
        h.command("staff", "notification.acknowledge", {"reason": "I acknowledge"}, note["id"], note["revision"])
    with transaction(engine, write=True) as db:
        day = announcements.today(db, h.seed["department_id"])
        db.get(m.Department, h.seed["department_id"]).announcement_auto_reminder_days = 1
        content = db.get(m.DepartmentContent, row["id"])
        content.receipts_checked_on = day - timedelta(days=1) if unchecked else day
        original_outbox = set(db.scalars(select(m.Outbox.id)))
        original_receipts = list(db.execute(select(m.AnnouncementReceipt.id, m.AnnouncementReceipt.read_at,
            m.AnnouncementReceipt.acknowledged_at).where(m.AnnouncementReceipt.content_id == row["id"])))
    # Fix the department day so this daily-boundary check also works around local midnight.
    monkeypatch.setattr(announcements, "today", lambda db, department_id: day)
    contexts, signals = [], []
    recipient_context = audiences.recipient_context

    def context(*args, **kwargs):
        contexts.append(True)
        return recipient_context(*args, **kwargs)

    monkeypatch.setattr(audiences, "recipient_context", context)
    monkeypatch.setattr(worker.live, "signal", lambda db, department_id, **change: signals.append(department_id))
    dispatched = [retry_write(engine, dispatch_batch_after_undo) for _ in range(2)]
    assert dispatched == [set(), set()]
    assert signals == []
    assert len(contexts) == int(unchecked)  # One daily check, no due-only audience reloads.
    with transaction(engine) as db:
        assert set(db.scalars(select(m.Outbox.id))) == original_outbox
        assert db.get(m.DepartmentContent, row["id"]).last_reminded_at is None
        assert db.get(m.DepartmentContent, row["id"]).receipts_checked_on == day
        assert list(db.execute(select(m.AnnouncementReceipt.id, m.AnnouncementReceipt.read_at,
            m.AnnouncementReceipt.acknowledged_at).where(m.AnnouncementReceipt.content_id == row["id"]))) == original_receipts


@pytest.mark.parametrize("backend", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_daily_late_receipt_reports_one_change_then_dispatch_is_idle(request, backend, monkeypatch):
    h = request.getfixturevalue(backend)
    row = notice(h, {"roles": ["staff"]})
    late_id = identity(h, "daily_late_recipient", ["staff"])
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        day = announcements.today(db, h.seed["department_id"])
        db.get(m.DepartmentContent, row["id"]).receipts_checked_on = day - timedelta(days=1)
    monkeypatch.setattr(announcements, "today", lambda db, department_id: day)
    signals = []
    monkeypatch.setattr(worker.live, "signal", lambda db, department_id, **change: signals.append(department_id))
    assert retry_write(engine, dispatch_batch_after_undo) == {h.seed["department_id"]}
    assert retry_write(engine, dispatch_batch_after_undo) == set()
    assert signals == [h.seed["department_id"]]
    with transaction(engine) as db:
        targets = set(db.scalars(select(m.AnnouncementReceipt.identity_id).where(
            m.AnnouncementReceipt.content_id == row["id"])))
        assert targets == {h.seed["identities"]["staff"], late_id}
        assert db.scalar(select(func.count()).select_from(m.Outbox)) == 0


@pytest.mark.parametrize("backend", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_daily_existing_receipt_delivery_reports_one_change_then_dispatch_is_idle(request, backend, monkeypatch):
    h = request.getfixturevalue(backend)
    row = notice(h, {"roles": ["staff"]})
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        day = announcements.today(db, h.seed["department_id"])
        content = db.get(m.DepartmentContent, row["id"])
        receipt = db.scalar(select(m.AnnouncementReceipt).where(m.AnnouncementReceipt.content_id == row["id"]))
        assert receipt is not None and receipt.delivered_at is None
        original_receipt = (receipt.id, receipt.identity_id, receipt.read_at, receipt.acknowledged_at)
        event = m.Outbox(department_id=h.seed["department_id"], event_key="notification-only-recovery",
            kind="announcement", payload={"content_id": row["id"], "revision": content.revision}, state="available_in_app")
        db.add(event)
        db.flush()
        event_id = event.id
        content.sent_outbox_id = event_id
        content.receipts_checked_on = day - timedelta(days=1)
        original_outbox = set(db.scalars(select(m.Outbox.id)))
        assert db.scalar(select(func.count()).select_from(m.Notification).where(m.Notification.outbox_id == event_id)) == 0
    monkeypatch.setattr(announcements, "today", lambda db, department_id: day)
    signals = []
    monkeypatch.setattr(worker.live, "signal", lambda db, department_id, **change: signals.append(department_id))
    assert retry_write(engine, dispatch_batch_after_undo) == {h.seed["department_id"]}
    assert retry_write(engine, dispatch_batch_after_undo) == set()
    assert signals == [h.seed["department_id"]]
    with transaction(engine) as db:
        recovered = list(db.scalars(select(m.AnnouncementReceipt).where(m.AnnouncementReceipt.content_id == row["id"])))
        assert len(recovered) == 1
        receipt, = recovered
        assert (receipt.id, receipt.identity_id, receipt.read_at, receipt.acknowledged_at) == original_receipt
        assert receipt.delivered_at is not None
        delivered = list(db.scalars(select(m.Notification).where(m.Notification.outbox_id == event_id)))
        assert len(delivered) == 1 and delivered[0].identity_id == receipt.identity_id
        assert delivered[0].source_id == row["id"] and delivered[0].source_kind == "department_content"
        assert delivered[0].read_at is None and delivered[0].acknowledged_at is None
        assert set(db.scalars(select(m.Outbox.id))) == original_outbox


def test_legacy_delivery_recovers_read_evidence_without_inventing_acknowledgement(h):
    moment = m.now() - timedelta(days=1)
    with transaction(h.app.state.engine, write=True) as db:
        content = m.DepartmentContent(department_id=h.seed["department_id"], name="Migrated notice",
            description="Historical notice", starts_on=date(2020, 1, 1), kind="announcement",
            importance="normal", audience={"roles": ["staff"]})
        db.add(content)
        db.flush()
        content_id = content.id
        outbox = m.Outbox(department_id=h.seed["department_id"], event_key="legacy-receipt-event",
            kind="announcement", payload={"content_id": content_id, "revision": content.revision}, state="available_in_app")
        db.add(outbox)
        db.flush()
        old = m.Notification(outbox_id=outbox.id, identity_id=h.seed["identities"]["staff"], title="Historical notice",
            message="Preserved history", created_at=moment, read_at=moment, acknowledged_at=moment)
        db.add(old)
        db.flush()
        notification_id = old.id
    assert process_one(h.app.state.engine)
    recovered = receipts(h, content_id)["receipts"][0]
    assert recovered["delivered_at"] and recovered["read_at"] and recovered["acknowledged_at"] is None
    legacy = notes(h, content_id)[0]
    assert legacy["id"] == notification_id and legacy["acknowledged_at"] is None
    current = next(row for row in h.get("coordinator", "/api/catalogues")["department_content"] if row["id"] == content_id)
    h.command("coordinator", "catalogue.update", {"catalogue": "department_content", "reason": "Require acknowledgement",
        "value": {"name": current["name"], "description": current["description"], "starts_on": current["starts_on"],
                  "kind": "announcement", "audience": current["audience"], "importance": "must_acknowledge"}},
        content_id, current["revision"])
    pending = notes(h, content_id)[0]
    assert pending["id"] == notification_id and pending["acknowledgement_required"] is True
    assert pending["acknowledged_at"] is None and pending["read_at"] == recovered["read_at"]
    assert counts(h, content_id) == {"audience": 1, "delivered": 1, "read": 1, "acknowledged": 0}
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Notification, notification_id)
        assert stored.read_at == stored.acknowledged_at == moment.replace(tzinfo=None)
    reply = h.command("staff", "notification.acknowledge", {"reason": "I acknowledge"}, pending["id"], pending["revision"])
    acknowledged = notes(h, content_id)[0]
    canonical = receipts(h, content_id)["receipts"][0]
    assert aware(acknowledged["acknowledged_at"]) == aware(canonical["acknowledged_at"]) == aware(reply["receipt"]["acknowledged_at"])
    assert acknowledged["acknowledged_at"] and acknowledged["read_at"] == recovered["read_at"]
    assert counts(h, content_id) == {"audience": 1, "delivered": 1, "read": 1, "acknowledged": 1}


def test_reading_after_a_reminder_is_queued_prevents_its_delivery(h):
    row = notice(h, {"roles": ["staff"]}, notify=True)
    process_one(h.app.state.engine)
    h.command("coordinator", "department_content.remind", {"reason": "Please read"}, row["id"], row["revision"])
    seen(h, [row["id"]])
    process_one(h.app.state.engine)
    assert len(notes(h, row["id"])) == 1


def test_department_settings_expose_defaults_validate_and_control_future_send_time(h):
    me = h.get("admin", "/api/me")
    org, dept = me["organisation"], me["department"]
    assert dept["announcement_send_time"] == "07:00"
    assert dept["announcement_auto_reminder_days"] is None
    assert dept["announcement_reminder_interval_hours"] == 24
    payload = {"department_name": dept["name"], "department_revision": dept["revision"],
        "organisation_name": org["name"], "timezone": org["timezone"], "week_start": org["week_start"],
        "reason": "Configure announcement delivery"}
    for bad in ({"announcement_send_time": "25:00"}, {"announcement_auto_reminder_days": 0},
                {"announcement_reminder_interval_hours": 0}):
        h.command("admin", "department.settings", {**payload, **bad}, dept["id"], org["revision"], status=422)
    saved = h.command("admin", "department.settings", {**payload, "announcement_send_time": "09:45",
        "announcement_auto_reminder_days": 3, "announcement_reminder_interval_hours": 48}, dept["id"], org["revision"])
    assert saved["department"]["announcement_send_time"] == "09:45"
    assert saved["previous"]["announcement_auto_reminder_days"] is None
    row = notice(h, {"roles": ["staff"]}, notify=True, starts_on="2099-01-01")
    with transaction(h.app.state.engine) as db:
        queued = db.scalar(select(m.Outbox).where(m.Outbox.kind == "announcement"))
        assert queued.next_attempt_at.isoformat().startswith("2099-01-01T06:45")  # department timezone is UTC+03
        assert db.scalar(select(func.count()).select_from(m.AnnouncementReceipt).where(m.AnnouncementReceipt.content_id == row["id"])) == 0


def test_content_filters_before_paging_and_seen_marks_only_rendered_receipts_read(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.DepartmentContent), [
            {"department_id": h.seed["department_id"], "name": f"Private {index:03}", "description": "Other audience",
             "kind": "announcement", "starts_on": date(2020, 1, 1), "audience": {"roles": ["coordinator"]}}
            for index in range(205)])
    rows = [notice(h, {"roles": ["staff"]}, name=f"Visible {index}") for index in range(3)]
    first = h.get("staff", "/api/content?page=1&page_size=2")
    assert all(counts(h, row["id"])["read"] == 0 for row in rows)
    seen(h, [item["id"] for item in first["items"]])
    assert sum(counts(h, row["id"])["read"] for row in rows) == 2
    second = h.get("staff", "/api/content?page=2&page_size=2")
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert {item["id"] for item in first["items"] + second["items"]} == {row["id"] for row in rows}
    seen(h, [item["id"] for item in second["items"]])
    assert all(counts(h, row["id"])["read"] == 1 for row in rows)


def test_default_home_page_leaves_the_fifth_notice_unread(h):
    rows = [notice(h, {"roles": ["staff"]}, name=f"Home notice {index}") for index in range(5)]
    shown = h.get("staff", "/api/content")
    assert shown["page_size"] == 4 and shown["total"] == 5
    assert [item["id"] for item in shown["items"]] == [row["id"] for row in rows[:4]]
    seen(h, [item["id"] for item in shown["items"]])
    assert [counts(h, row["id"])["read"] for row in rows] == [1, 1, 1, 1, 0]


@pytest.mark.parametrize("backend", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_home_prioritises_viewers_pending_acknowledgements_before_paging(request, backend):
    h = request.getfixturevalue(backend)
    rows = [notice(h, {"roles": ["staff"]}, importance="must_acknowledge", name=f"Home notice {name}")
            for name in "ABCDE"]
    identity(h, "late_home_reader", ["staff"])
    first = h.get("late_home_reader", "/api/content")
    assert first["page_size"] == 4 and first["total"] == 5
    assert [item["id"] for item in first["items"]] == [row["id"] for row in rows[:4]]
    assert all(item["receipt"] is None for item in first["items"])
    own = seen(h, [row["id"] for row in rows[:4]], "late_home_reader")["receipts"]
    for receipt in own.values():
        h.command("late_home_reader", "notification.acknowledge", {"reason": "I acknowledge"},
                  receipt["id"], receipt["revision"])

    shown = h.get("late_home_reader", "/api/content")
    assert shown["total"] == 5 and shown["page_size"] == 4
    assert [item["id"] for item in shown["items"]] == [rows[4]["id"], *[row["id"] for row in rows[:3]]]
    assert shown["items"][0]["receipt"] is None
    other = h.get("staff", "/api/content")
    assert other["total"] == 5
    assert [item["id"] for item in other["items"]] == [row["id"] for row in rows[:4]]


def test_home_sorts_acknowledged_required_notices_with_important_notices(h):
    normal = notice(h, name="0 normal notice")
    important = notice(h, importance="important", name="A important notice")
    acknowledged = notice(h, importance="must_acknowledge", name="B acknowledged notice")
    pending = notice(h, importance="must_acknowledge", name="Z pending notice")
    own = seen(h, [acknowledged["id"], pending["id"]])["receipts"][acknowledged["id"]]
    h.command("staff", "notification.acknowledge", {"reason": "I acknowledge"}, own["id"], own["revision"])

    shown = h.get("staff", "/api/content")
    assert shown["total"] == 4
    assert [item["id"] for item in shown["items"]] == [row["id"] for row in (pending, important, acknowledged, normal)]


@pytest.mark.parametrize("backend", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_required_links_cannot_hide_a_pending_home_only_announcement(request, backend):
    h = request.getfixturevalue(backend)
    links = required_links(h)
    row = notice(h, {"roles": ["staff"]}, importance="must_acknowledge", name="Z required announcement")

    shown = h.get("staff", "/api/content")
    assert shown["total"] == 5 and shown["page_size"] == 4
    assert [item["id"] for item in shown["items"]] == [row["id"], *[link["id"] for link in links[:3]]]
    assert shown["items"][0]["acknowledgement_required"] is True
    assert all(item["receipt"] is None and not item["acknowledgement_required"] for item in shown["items"][1:])
    assert notes(h, row["id"]) == []


def test_existing_link_content_can_still_be_sent_to_the_inbox(h):
    row = h.command("coordinator", "catalogue.create", {"catalogue": "department_content", "reason": "Share a link",
        "value": {"name": "Existing department link", "kind": "link", "url": "https://example.org/department",
            "description": "Synthetic link notice", "starts_on": "2020-01-01", "audience": {"roles": ["staff"]},
            "notify": True}})["record"]
    process_one(h.app.state.engine)
    delivered = notes(h, row["id"])[0]
    assert delivered["kind"] == "announcement" and delivered["acknowledgement_required"] is False
    shown = next(item for item in h.get("staff", "/api/content")["items"] if item["id"] == row["id"])
    assert shown["kind"] == "link" and shown["url"] == "https://example.org/department"


@pytest.mark.postgres
def test_150_person_receipt_and_delivery_queries_do_not_grow_per_person(pg):
    engine = pg.app.state.engine
    with transaction(engine, write=True) as db:
        organisation_id = db.get(m.Department, pg.seed["department_id"]).organisation_id
        people = [{"id": m.uid(), "organisation_id": organisation_id, "display_name": f"Receipt person {i}",
                   "tags": ["receipt-scale"]} for i in range(150)]
        identities = [{"id": m.uid(), "issuer": "local-development", "subject": f"receipt-scale-{i}",
                       "display_name": person["display_name"], "person_id": person["id"]} for i, person in enumerate(people)]
        db.execute(insert(m.Person), people)
        db.execute(insert(m.Identity), identities)
        db.execute(insert(m.RoleBinding), [{"identity_id": item["id"], "department_id": pg.seed["department_id"],
            "role": "staff", "starts_on": date(2020, 1, 1)} for item in identities])
    pg.client("coordinator")  # exclude authentication work from the comparison
    statements = []

    def count_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        one = notice(pg, {"person_ids": [people[0]["id"]], "everyone": False}, notify=True, name="One recipient")
        process_one(engine)
        small = len(statements)
        statements.clear()
        many = notice(pg, {"everyone": False, "where": {"match": "all", "conditions": [
            {"field": "tags", "op": "any", "values": ["receipt-scale"]}]}}, notify=True, name="150 recipients")
        process_one(engine)
        large = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
    assert counts(pg, one["id"])["delivered"] == 1
    assert counts(pg, many["id"]) == {"audience": 150, "delivered": 150, "read": 0, "acknowledged": 0}
    assert large <= small + 15, (small, large)


@pytest.mark.postgres
def test_years_of_distinct_audiences_keep_count_queries_within_postgres_parameter_limit(pg):
    import random
    from apps.api.auth import Actor
    from apps.api.services.announcements import counts as announcement_counts

    engine, moment = pg.app.state.engine, m.now()
    with transaction(engine, write=True) as db:
        people = [{"id": m.uid(), "organisation_id": pg.seed["organisation_id"], "display_name": f"History reader {i}"}
                  for i in range(150)]
        identities = [{"id": m.uid(), "issuer": "local-development", "subject": f"history-reader-{i}",
                       "display_name": person["display_name"], "person_id": person["id"]} for i, person in enumerate(people)]
        db.execute(insert(m.Person), people)
        db.execute(insert(m.Identity), identities)
        db.execute(insert(m.RoleBinding), [{"identity_id": item["id"], "department_id": pg.seed["department_id"],
            "role": "staff", "starts_on": date(2020, 1, 1)} for item in identities])
        rng = random.Random(80)
        selections = [sorted(rng.sample(range(150), 75)) for _ in range(1000)]
        assert len({tuple(selected) for selected in selections}) == 1000
        notices = [{"id": m.uid(), "department_id": pg.seed["department_id"], "name": f"History notice {index}",
            "kind": "announcement", "starts_on": date(2020, 1, 1),
            "audience": {"everyone": False, "person_ids": [people[i]["id"] for i in selected]}}
            for index, selected in enumerate(selections)]
        db.execute(insert(m.DepartmentContent), notices)
        # One read receipt per notice proves aggregation while the 75,000 audience memberships exercise query bounds.
        db.execute(insert(m.AnnouncementReceipt), [{"content_id": row["id"], "identity_id": identities[selected[0]]["id"],
            "person_id": people[selected[0]]["id"], "delivered_at": moment, "read_at": moment}
            for row, selected in zip(notices, selections)])
    with transaction(engine) as db:
        rows = list(db.scalars(select(m.DepartmentContent).where(m.DepartmentContent.id.in_([row["id"] for row in notices]))))
        actor = Actor(pg.seed["identities"]["coordinator"], pg.seed["department_id"], pg.seed["organisation_id"],
                      "History coordinator", frozenset({"coordinator"}), None, "synthetic-session")
        result = announcement_counts(db, actor, rows)
    assert len(result) == 1000
    assert all(value == {"audience": 75, "delivered": 1, "read": 1, "acknowledged": 0} for value in result.values())


def test_a_notice_receipt_is_no_scheduling_input_but_an_event_reply_still_is(h):
    """A notice delivered, read and acknowledged leaves the rota's inputs, fingerprints and revisions alike, unchanged:
    a person's first read used to move the availability revision, so every open page read everything again. An event
    reply still moves the availability revision (its fingerprints stay: tests/test_events.py)."""
    from tests.test_events import event, event_type, input_revisions, occurrences, semantic_fingerprints
    workspace = h.shift(h.workspace())['workspace']
    row = notice(h, {'roles': ['staff']}, importance='must_acknowledge')
    with transaction(h.app.state.engine, write=True) as db:  # first sight: the read inserts the receipt
        db.execute(delete(m.AnnouncementReceipt).where(m.AnnouncementReceipt.content_id == row['id']))
    before, semantic = input_revisions(h), semantic_fingerprints(h, workspace)
    receipt = seen(h, [row['id']])['receipts'][row['id']]
    assert receipt['revision'] == 2  # inserted, then read: the first sight
    h.command('staff', 'notification.acknowledge', {'reason': 'Acknowledged by recipient'}, receipt['id'], receipt['revision'])
    assert input_revisions(h) == before
    assert semantic_fingerprints(h, workspace) == semantic
    made = event(h, event_type(h, blocks_duties=False, rsvp=True))
    occurrence = next(item for item in occurrences(h, 'staff') if item['event_id'] == made['id'])
    before = input_revisions(h)
    h.command('staff', 'event.rsvp', {'response': 'going', 'reason': 'Optional response'}, occurrence['id'], occurrence['revision'])
    assert input_revisions(h)[1] == before[1] + 1

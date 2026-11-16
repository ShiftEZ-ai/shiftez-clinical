"""Decision chronology uses recorded events, never the dates a request is about."""
from .clock_support import freeze_clock
from datetime import date, datetime, timedelta, timezone
import importlib
import json
from itertools import count

from alembic import command
import pytest
from sqlalchemy import MetaData, select

from apps.api import models as m
from apps.api.db import make_engine, transaction
from apps.api.services import availability, availability_patterns
from packages.domain.time import aware
from scripts.manage import alembic_config
from .migration_support import historical_seed
from .test_email_migration import migration_url  # noqa: F401
from .test_overtime_volunteer import offer, offer_rule
from .test_request_decide_many import decide, pattern_row, request_row
from .test_requests_search import both, read  # noqa: F401
from .test_staff_swaps import source_pair


AT = datetime(2026, 10, 3, 9, tzinfo=timezone.utc)


def emitted_clock(monkeypatch, *, tied):
    """Control the shared provider before insertion, including already-bound audit defaults."""
    from packages.domain import time as clock

    fixed, stamps = AT - clock._offset, count()
    class EmittedDatetime(datetime):
        fromisoformat = staticmethod(datetime.fromisoformat)
        combine = staticmethod(datetime.combine)

        @classmethod
        def now(cls, tz=None):
            instant = fixed + timedelta(seconds=0 if tied else next(stamps))
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(clock, "datetime", EmittedDatetime)


@pytest.mark.parametrize("decision,role", [("approve", "approver"), ("reject", "approver"), ("withdraw", "staff")])
def test_request_decision_undo_and_redecision_use_actual_instants(both, monkeypatch, decision, role):
    h = both
    row = request_row(h, kind="meeting")
    assert row["decided_at"] is None
    freeze_clock(monkeypatch, lambda: AT)
    decided = h.command(role, "request.decide", {"decision": decision, "reason": "Reviewed now"},
                        row["id"], row["revision"])["record"]
    assert aware(decided["decided_at"]) == AT
    assert aware(h.get("staff", "/api/requests/all?id=" + row["id"])["item"]["decided_at"]) == AT
    assert aware(h.get("staff", "/api/home")["my_requests"][0]["decided_at"]) == AT
    undone = h.command(role, "request.undo", {"reason": "Return to waiting"}, row["id"], decided["revision"])["record"]
    assert undone["decided_at"] is None
    freeze_clock(monkeypatch, lambda: AT + timedelta(hours=1))
    again = h.command("approver", "request.decide", {"decision": "reject", "reason": "Reviewed again"},
                      row["id"], undone["revision"])["record"]
    assert aware(again["decided_at"]) == AT + timedelta(hours=1)


def test_mixed_batch_and_weekly_end_restore_preserve_decision_chronology(both, monkeypatch):
    h = both
    rows = [request_row(h, kind="meeting"), pattern_row(h)]
    freeze_clock(monkeypatch, lambda: AT)
    reply = decide(h, rows, role="admin")
    assert all(aware(row["decided_at"]) == AT for row in reply["records"])
    weekly = reply["records"][1]
    freeze_clock(monkeypatch, lambda: AT + timedelta(hours=1))
    ended = h.command("staff", "availability_pattern.end", {"reason": "End weekly"},
                      weekly["id"], weekly["revision"])["pattern"]
    assert aware(ended["decided_at"]) == AT + timedelta(hours=1)
    restored = h.command("staff", "availability_pattern.restore", {"reason": "Restore weekly"},
                         ended["id"], ended["revision"])["pattern"]
    assert aware(restored["decided_at"]) == AT
    undone = h.command("admin", "availability_pattern.undo", {"reason": "Undo approval"},
                       restored["id"], restored["revision"])["pattern"]
    assert undone["decided_at"] is None
    direct = h.command("coordinator", "availability_pattern.propose", {
        "person_id": h.seed["people"][1], "weekdays": [1], "start_time": "13:00", "end_time": "17:00",
        "starts_on": "2026-10-01"})["pattern"]
    assert direct["status"] == "active" and aware(direct["decided_at"]) == AT + timedelta(hours=1)
    freeze_clock(monkeypatch, lambda: AT + timedelta(hours=2))
    reapproved = h.command("coordinator", "availability_pattern.decide", {"decision": "approve", "reason": "Fresh weekly decision"},
                          undone["id"], undone["revision"])["pattern"]
    assert aware(reapproved["decided_at"]) == AT + timedelta(hours=2)


@pytest.mark.parametrize("missing_metadata", [False, True])
def test_overtime_undo_withdrawal_restores_original_approval_time(both, monkeypatch, missing_metadata):
    h = both
    offer_rule(h)
    row = offer(h)["record"]
    freeze_clock(monkeypatch, lambda: AT)
    approved = h.command("coordinator", "request.decide", {"decision": "approve", "reason": "Approve offer"},
                         row["id"], row["revision"])["record"]
    freeze_clock(monkeypatch, lambda: AT + timedelta(hours=1))
    withdrawn = h.command("staff", "request.decide", {"decision": "withdraw", "reason": "Withdraw offer"},
                          row["id"], approved["revision"])["record"]
    assert aware(withdrawn["decided_at"]) == AT + timedelta(hours=1)
    expected = aware(approved["decided_at"])
    if missing_metadata:
        with transaction(h.app.state.engine, write=True) as db:
            stored = db.get(m.RequestRecord, row["id"])
            stored.overtime = {key: value for key, value in stored.overtime.items() if key != "previous_decided_at"}
            events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.target_id == row["id"],
                m.AuditEvent.action == "request.decide").order_by(m.AuditEvent.created_at)))
            stored.decided_at = events[-1].created_at
    restored = h.command("staff", "request.undo", {"reason": "Restore approved offer"},
                         row["id"], withdrawn["revision"])["record"]
    assert restored["status"] == "approved" and aware(restored["decided_at"]) == expected


@pytest.mark.parametrize("direct", [False, True])
def test_legacy_weekly_restore_recovers_approval_from_before_end_audit(both, direct):
    h = both
    if direct:
        row = h.command("coordinator", "availability_pattern.propose", {"person_id": h.seed["people"][0],
            "weekdays": [1], "start_time": "13:00", "end_time": "17:00", "starts_on": "2026-10-01"})["pattern"]
    else:
        row = pattern_row(h)
        row = h.command("coordinator", "availability_pattern.decide", {"decision": "approve", "reason": "Weekly approved"},
                        row["id"], row["revision"])["pattern"]
    with transaction(h.app.state.engine, write=True) as db:
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == (
            "availability_pattern.propose" if direct else "availability_pattern.decide")))
        expected = aware(event.created_at)
        stored = db.get(m.AvailabilityPattern, row["id"])
        db.add(m.DefinitionHistory(department_id=h.seed["department_id"], kind="availability_pattern.before_end",
            object_id=row["id"], object_revision=row["revision"], snapshot={"status": "active"},
            actor_id=h.seed["identities"]["staff"], reason="Legacy end", created_at=expected + timedelta(seconds=1)))
        stored.status, stored.revision, stored.decided_at = "ended", row["revision"] + 1, expected + timedelta(seconds=1)
    restored = h.command("staff", "availability_pattern.restore", {"reason": "Restore legacy weekly"},
                         row["id"], row["revision"] + 1)["pattern"]
    assert restored["status"] == "active" and aware(restored["decided_at"]) == expected


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_decided_sort_pages_complete_history_and_keeps_unknown_last(both, direction):
    h = both
    with transaction(h.app.state.engine, write=True) as db:
        rows = []
        for number in range(205):
            row = m.RequestRecord(department_id=h.seed["department_id"], person_id=h.seed["people"][0],
                kind="meeting", start=AT + timedelta(days=300 - number), end=AT + timedelta(days=301 - number),
                private_reason="Hidden history", created_by=h.seed["identities"]["staff"], status="approved",
                decided_at=AT + timedelta(minutes=number // 2) if number < 203 else None)
            rows.append(row)
        weekly = m.AvailabilityPattern(department_id=h.seed["department_id"], person_id=h.seed["people"][0],
            weekdays=[1], start_time="13:00", end_time="17:00", starts_on=date(2026, 1, 1), status="active",
            created_by=h.seed["identities"]["coordinator"], decided_at=AT + timedelta(minutes=70))
        db.add_all([*rows, weekly])
        db.flush()
        expected = [("request", row.id, row.decided_at) for row in rows] + [("pattern", weekly.id, weekly.decided_at)]
    expected.sort(key=lambda row: (row[0], row[1]))
    known = sorted((row for row in expected if row[2] is not None), key=lambda row: row[2], reverse=direction == "desc")
    expected = known + [row for row in expected if row[2] is None]
    actual = [row for offset in (0, 100, 200)
              for row in read(h, "staff", sort_by="decided", sort_direction=direction, offset=offset)["items"]]
    assert [row["id"] for row in actual] == [row[1] for row in expected]
    assert [row["decided_at"] for row in actual[-2:]] == [None, None]


def test_other_request_kinds_expose_evidence_time_without_request_date_fallback(both):
    h = both
    recorded = h.command("coordinator", "availability.record", {"person_id": h.seed["people"][0],
        "kind": "actual_absence", "start": "2026-11-01T08:00:00+03:00", "end": "2026-11-01T10:00:00+03:00",
        "private_reason": "Synthetic recorded absence"})["record"]
    wish = h.command("staff", "wish.create", {"person_id": h.seed["people"][0], "first_day": "2026-11-03",
        "last_day": "2026-11-03", "kind": "prefer_off", "points": 1, "note": "Synthetic wish"})["wish"]
    assert h.get("staff", "/api/requests/all?id=" + wish["id"])["item"]["decided_at"] is None
    wish = h.command("staff", "wish.withdraw", {"reason": "Withdraw synthetic wish"}, wish["id"], wish["revision"])["wish"]
    with transaction(h.app.state.engine) as db:
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.target_id == wish["id"], m.AuditEvent.action == "wish.withdraw"))
        expected = aware(audit.created_at)
    items = {row["id"]: row for row in read(h, "staff", sort_by="decided", sort_direction="desc")["items"]}
    assert aware(items[recorded["id"]]["decided_at"]) == aware(recorded["created_at"])
    assert aware(items[wish["id"]]["decided_at"]) == expected


def test_swap_and_published_wish_decision_times_use_their_immutable_events(both):
    h = both
    publication, first, _, _ = source_pair(h)
    swap = h.command("staff", "swap.offer", {"kind": "give", "publication_id": publication,
        "assignment_id": first, "recipient_person_id": h.seed["people"][1],
        "expires_at": (m.now() + timedelta(hours=2)).isoformat(), "reason": "Synthetic decision chronology"})["swap"]
    assert h.get("staff", "/api/requests/all?id=" + swap["id"])["item"]["decided_at"] is None
    withdrawn = h.command("staff", "swap.withdraw", {"reason": "Withdraw offer"}, swap["id"], swap["revision"])["swap"]
    with transaction(h.app.state.engine, write=True) as db:
        event = db.scalar(select(m.SwapEvent).where(m.SwapEvent.swap_id == swap["id"], m.SwapEvent.action == "withdraw"))
        withdrawn_at = aware(event.created_at)
        published_at = aware(db.get(m.Publication, publication).created_at)
        wish = m.StaffWish(department_id=h.seed["department_id"], person_id=h.seed["people"][0],
            first_day=date(2026, 11, 3), last_day=date(2026, 11, 3), kind="prefer_off", points=1, note="Synthetic outcome",
            status="open", outcome="declined", publication_id=publication, created_by=h.seed["identities"]["staff"])
        db.add(wish)
        db.flush()
        wish_id = wish.id
    assert aware(h.get("staff", "/api/requests/all?id=" + swap["id"])["item"]["decided_at"]) == withdrawn_at
    assert aware(h.get("staff", "/api/requests/all?id=" + wish_id)["item"]["decided_at"]) == published_at
    reopened = h.command("staff", "swap.undo", {"reason": "Undo withdrawal"}, swap["id"], withdrawn["revision"])["swap"]
    with transaction(h.app.state.engine) as db:
        assert aware(db.scalar(select(availability._decision_time("swap")).where(m.SwapRequest.id == swap["id"]))) == withdrawn_at
    accepted = h.command("staff_two", "swap.accept", {"reason": "Accept transfer"}, reopened["id"], reopened["revision"])["swap"]
    h.command("staff_two", "swap.undo", {"reason": "Undo acceptance"}, accepted["id"], accepted["revision"])
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.SwapEvent).where(m.SwapEvent.swap_id == accepted["id"], m.SwapEvent.action == "undo"))
        assert aware(db.scalar(select(availability._decision_time("swap")).where(m.SwapRequest.id == accepted["id"]))) == aware(event.created_at)


def test_collection_multi_sort_applies_all_keys_before_paging(both):
    h = both
    with transaction(h.app.state.engine, write=True) as db:
        rows = []
        for number in range(205):
            row = m.RequestRecord(department_id=h.seed["department_id"], person_id=h.seed["people"][0],
                kind="meeting" if number % 2 else "leave", start=AT + timedelta(days=number),
                end=AT + timedelta(days=number + 1), private_reason="Synthetic multi sort", status="approved",
                created_by=h.seed["identities"]["staff"], decided_at=AT + timedelta(minutes=number // 3))
            rows.append(row)
        db.add_all(rows)
        db.flush()
        expected = sorted(rows, key=lambda row: row.id)
        expected.sort(key=lambda row: row.start)
        expected.sort(key=lambda row: row.decided_at, reverse=True)
        expected.sort(key=lambda row: "leave" if row.kind == "leave" else "other")
        ids = [row.id for row in expected]
    selection = json.dumps({"sort": ["type", "asc"], "then_sort": [["decided_at", "desc"], ["start", "asc"]]})
    actual = [row for offset in (0, 100, 200)
              for row in read(h, "staff", filter=selection, offset=offset)["items"]]
    assert [row["id"] for row in actual] == ids


def test_migration_backfills_matching_audit_only_and_preserves_existing_evidence(migration_url):
    config = alembic_config(migration_url)
    command.upgrade(config, "0113_department_quota_calendar")
    engine = make_engine(migration_url)
    try:
        before = MetaData()
        before.reflect(engine)
        with transaction(engine, write=True) as db:
            seed = historical_seed(db)
            bind = db.connection()
            requests, patterns, events = (before.tables[key] for key in ("requests", "availability_patterns", "audit_events"))
            common = {"revision": 2, "created_at": AT, "department_id": seed["department_id"]}
            for key, status in (("decided", "approved"), ("waiting", "submitted"), ("unknown", "approved"),
                                ("withdrawn", "withdrawn"), ("restored", "approved"), ("superseded", "approved")):
                bind.execute(requests.insert().values(**common, id=key, person_id=seed["people"][0], kind="meeting",
                    start=AT + timedelta(days=50), end=AT + timedelta(days=51), private_reason="Preserved reason",
                    status=status, created_by=seed["identities"]["staff"],
                    decided_by=seed["identities"]["staff" if key == "restored" else "approver"],
                    decision_reason="Original approval", counts_as_unavailable=True))
            bind.execute(patterns.insert().values(**common, id="weekly", person_id=seed["people"][0],
                weekdays=[1], start_time="13:00", end_time="17:00", starts_on=date(2026, 1, 1), note="Preserved weekly",
                status="active", created_by=seed["identities"]["coordinator"], decided_by=seed["identities"]["coordinator"]))
            for key in ("decided", "waiting"):
                bind.execute(events.insert().values(**common, id="audit-" + key, target_id=key,
                    action="request.decide", actor_id=seed["identities"]["approver"], reason="Original approval",
                    detail={"batch_id": "retained-batch"}, correlation_id="retained-correlation"))
            receipts = before.tables["command_receipts"]
            bind.execute(receipts.insert().values(**common, id="receipt-weekly", command_kind="availability_pattern.propose",
                required_roles=["coordinator"], actor_id=seed["identities"]["coordinator"], key="receipt-weekly",
                payload_hash="f" * 64, result={"pattern": {"id": "weekly", "status": "active"}}))
            bind.execute(events.insert().values(**common, id="audit-weekly", target_id=None,
                action="availability_pattern.propose", actor_id=seed["identities"]["coordinator"], reason="Direct recording",
                detail={"receipt_id": "receipt-weekly"}, correlation_id="retained-correlation"))
            for key, states, decider in (
                    ("withdrawn", ["withdrawn"], "staff"),
                    ("restored", ["approved", "withdrawn", "approved"], "staff"),
                    ("superseded", ["approved", "submitted", "approved"], "approver")):
                for number, status in enumerate(states):
                    action = "request.undo" if (key == "restored" and number == 2 or key == "superseded" and number == 1) else "request.decide"
                    receipt = "receipt-" + key + str(number)
                    stamp = {**common, "created_at": AT + timedelta(hours=number)}
                    result = {"records": [{"id": key, "source": "request", "status": status}]}
                    bind.execute(receipts.insert().values(**stamp, id=receipt, command_kind=action, required_roles=[decider],
                        actor_id=seed["identities"][decider], key=receipt, payload_hash="f" * 64, result=result))
                    bind.execute(events.insert().values(**stamp, id="audit-" + key + str(number), target_id=key,
                        action=action, actor_id=seed["identities"][decider], reason="Original approval",
                        detail={"receipt_id": receipt, "batch_id": receipt}, correlation_id="retained-correlation"))
            original = {table.name: {row["id"]: dict(row) for row in bind.execute(select(table)).mappings()}
                        for table in (requests, patterns, events, receipts)}
        command.upgrade(config, "head")
        after = MetaData()
        after.reflect(engine)
        with engine.connect() as db:
            for name, old in original.items():
                columns = list(before.tables[name].c.keys())
                assert {row["id"]: dict(row) for row in db.execute(select(*(after.tables[name].c[key] for key in columns))).mappings()} == old
            rows = {row.id: row.decided_at for row in db.execute(select(after.tables["requests"]))}
            assert aware(rows["decided"]) == AT
            assert rows["waiting"] is None and rows["unknown"] is None
            assert aware(rows["withdrawn"]) == aware(rows["restored"]) == AT
            assert aware(rows["superseded"]) == AT + timedelta(hours=2)
            assert aware(db.execute(select(after.tables["availability_patterns"].c.decided_at)).scalar_one()) == AT
            migration = importlib.import_module("migrations.versions.0114_request_decision_times")
            migration.backfill(db)
            assert {row.id: row.decided_at for row in db.execute(select(after.tables["requests"]))} == rows
    finally:
        engine.dispose()


@pytest.mark.parametrize("approval_kind", ["single", "batch", "bulk", "bulk_batch", "bulk_steps", "bulk_tied"])
def test_legacy_repeated_overtime_withdrawal_undo_uses_approval_receipt(both, monkeypatch, approval_kind):
    from packages.domain import time as clock

    h = both
    reason = "Same authorised overtime decision reason"
    offer_rule(h)
    h.command("admin", "self_approval.grant", {"identity_id": h.seed["identities"]["coordinator"],
        "actions": ["request.decide"], "starts_on": "2026-01-01", "ends_on": "2026-10-12", "reason": reason})
    # Move the shared clock so audit defaults and explicit decision times agree, then restore it after this test.
    monkeypatch.setattr(clock, "_offset", clock._offset)
    clock.pin_clock(AT)
    row = offer(h, role="coordinator")["record"]
    first = None
    original_record = availability.record
    def legacy_record(stored, *args, **kwargs):
        result = original_record(stored, *args, **kwargs)
        if isinstance(stored, m.RequestRecord):
            result.pop("decided_at", None)
        return result
    # Emit the old receipt shape directly; immutable audit and receipt rows are never edited.
    with monkeypatch.context() as legacy:
        legacy.setattr(availability, "record", legacy_record)
        if approval_kind in {"bulk_steps", "bulk_tied"}:
            emitted_clock(legacy, tied=approval_kind == "bulk_tied")
        if approval_kind == "single":
            approved = h.command("coordinator", "request.decide", {"decision": "approve", "reason": reason},
                                 row["id"], row["revision"])["record"]
        elif approval_kind == "batch":
            approved = h.command("coordinator", "request.decide_many", {"items": [{"id": row["id"],
                "expected_revision": row["revision"], "source": "request"}], "decision": "approve", "reason": reason})["records"][0]
        elif approval_kind == "bulk":
            approved = h.command("coordinator", "bulk.apply", {"items": [{"item_kind": "request.decide",
                "target_id": row["id"], "expected_revision": row["revision"], "payload": {"decision": "approve"}}],
                "reason": reason})["items"][0]["results"][0]["record"]
        elif approval_kind == "bulk_batch":
            approved = h.command("coordinator", "bulk.apply", {"items": [{"item_kind": "request.decide_many",
                "payload": {"decision": "approve", "items": [{"source": "request", "id": row["id"],
                    "expected_revision": row["revision"]}]}}], "reason": reason})["items"][0]["results"][0]["records"][0]
        else:
            applied = h.command("coordinator", "bulk.apply", {"items": [{"item_kind": "request.decide",
                "target_id": row["id"], "expected_revision": row["revision"], "payload": {"decision": decision}}
                for decision in ("approve", "withdraw")], "reason": reason})
            approved, first = [item["results"][0]["record"] for item in applied["items"]]
    assert "decided_at" not in approved
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.target_id == row["id"],
            m.AuditEvent.action == "request.decide").order_by(m.AuditEvent.created_at)))
        assert len(events) == (2 if first else 1)
        approval_at = aware(events[0].created_at)
        if first:
            assert (len({aware(event.created_at) for event in events}) == 1) == (approval_kind == "bulk_tied")
    def legacy_backfill():
        with transaction(h.app.state.engine, write=True) as db:
            stored = db.get(m.RequestRecord, row["id"])
            stored.overtime = {key: value for key, value in stored.overtime.items() if key != "previous_decided_at"}
            stored.decided_at = None
            db.flush()
            importlib.import_module("migrations.versions.0114_request_decision_times").backfill(db.connection())

    def restored_approval(record):
        assert record["status"] == "approved"
        if approval_kind == "bulk_tied":
            assert record["decided_at"] is None
        else:
            assert aware(record["decided_at"]) == approval_at

    clock.pin_clock(AT + timedelta(hours=1))
    if first is None:
        first = h.command("coordinator", "request.decide", {"decision": "withdraw", "reason": reason},
                          row["id"], approved["revision"])["record"]
    legacy_backfill()
    restored = h.command("coordinator", "request.undo", {"reason": reason}, row["id"], first["revision"])["record"]
    restored_approval(restored)
    clock.pin_clock(AT + timedelta(hours=2))
    latest = h.command("coordinator", "request.decide", {"decision": "withdraw", "reason": reason},
                       row["id"], restored["revision"])["record"]
    legacy_backfill()
    undone = h.command("coordinator", "request.undo", {"reason": reason}, row["id"], latest["revision"])["record"]
    restored_approval(undone)


@pytest.mark.parametrize("source", ["pattern", "request"])
@pytest.mark.parametrize("tied", [False, True])
def test_migration_correlates_each_bulk_operation_with_its_own_snapshot(both, monkeypatch, source, tied):
    h = both
    reason = "Synthetic complete multi-step receipt evidence"
    if source == "request":
        offer_rule(h)
        h.command("admin", "self_approval.grant", {"identity_id": h.seed["identities"]["coordinator"],
            "actions": ["request.decide"], "starts_on": "2026-01-01", "ends_on": "2026-10-12", "reason": reason})
        row = offer(h, role="coordinator")["record"]
        service, model = availability, m.RequestRecord
        commands = [("request.decide", {"decision": "approve"}), ("request.decide", {"decision": "withdraw"})]
    else:
        row = pattern_row(h)
        service, model = availability_patterns, m.AvailabilityPattern
        commands = [("availability_pattern.decide", {"decision": "approve"}), ("availability_pattern.end", {})]
    original_record = service.record
    def legacy_record(stored, *args, **kwargs):
        return {key: value for key, value in original_record(stored, *args, **kwargs).items() if key != "decided_at"}
    with monkeypatch.context() as legacy:
        legacy.setattr(service, "record", legacy_record)
        # Emit deterministic audit order before insertion; never edit retained audit or receipt rows.
        emitted_clock(legacy, tied=tied)
        h.command("coordinator", "bulk.apply", {"items": [{"item_kind": kind, "target_id": row["id"],
            "expected_revision": row["revision"], "payload": payload} for kind, payload in commands], "reason": reason})
    with transaction(h.app.state.engine, write=True) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.target_id == row["id"],
            m.AuditEvent.action.in_([kind for kind, _ in commands])).order_by(m.AuditEvent.created_at)))
        assert len(events) == 2
        assert (len({aware(event.created_at) for event in events}) == 1) == tied
        expected_at = aware(events[-1].created_at)
        stored = db.get(model, row["id"])
        stored.decided_at = None
        db.flush()
        importlib.import_module("migrations.versions.0114_request_decision_times").backfill(db.connection())
        restored_at = db.scalar(select(model.decided_at).where(model.id == row["id"]))
    if tied and source == "request":
        assert restored_at is None
    else:
        assert aware(restored_at) == expected_at

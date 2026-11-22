"""Bulk diagnostics preserve the shared database retry and availability contract."""
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout

from apps.api import commands, models as m
from apps.api.db import DatabaseWriteTimeout, transaction
from apps.api.errors import Problem
from .test_bulk_single_commands import relationship


REASON = "Synthetic bulk database failure"


class DatabaseFailure(Exception):
    def __init__(self, sqlstate):
        super().__init__("Synthetic infrastructure failure")
        self.sqlstate = sqlstate


def failure(kind):
    if kind == "pool":
        return PoolTimeout("Synthetic exhausted connection pool")
    if kind == "deadline":
        return DatabaseWriteTimeout()
    return OperationalError("Synthetic SQL", {}, DatabaseFailure({
        "deadlock": "40P01", "statement": "57014", "unavailable": "08006"}[kind]))


def change(h):
    first, payload, role = relationship(h, "membership")
    second = h.command(role, "membership.create", {**payload, "person_id": h.seed["people"][1]})["record"]
    value = {"item_kind": "membership.update_dates", "reason": REASON,
             "items": [{"target_id": row["id"], "expected_revision": row["revision"],
                        "payload": {"ends_on": "2026-12-01"}} for row in (first, second)]}
    return value, [first, second]


def request(h, endpoint, value):
    if endpoint == "preview":
        return h.client("coordinator").post("/api/commands/preview", json={"kind": "bulk.apply", "payload": value})
    return h.client("coordinator").post("/api/commands", headers={"idempotency-key": m.uid()},
                                        json={"kind": "bulk.apply", "payload": value})


@pytest.mark.parametrize("endpoint", ["apply", "preview"])
@pytest.mark.parametrize("kind", ["statement", "pool", "unavailable", "deadline"])
def test_bulk_dispatch_database_failure_keeps_503_and_rolls_back(h, monkeypatch, endpoint, kind):
    value, before = change(h)
    original = commands.dispatch
    seen = []

    def broken(db, actor, command, settings, *args, **kwargs):
        if command.kind == "membership.update_dates":
            seen.append(command.target_id)
            if command.target_id == before[1]["id"]:
                raise failure(kind)
        return original(db, actor, command, settings, *args, **kwargs)

    monkeypatch.setattr(commands, "dispatch", broken)
    response = request(h, endpoint, value)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == (
        "DATABASE_UNAVAILABLE" if kind == "unavailable" else "COMMAND_WRITE_TIMEOUT")
    expected_seen = [row["id"] for row in before]
    if kind == "unavailable":
        expected_seen *= 3
    assert seen == expected_seen  # Recovery retries repeat the whole transaction, never row diagnostics.
    with transaction(h.app.state.engine) as db:
        for previous in before:
            current = db.get(m.Membership, previous["id"])
            assert (current.revision, current.ends_on.isoformat()) == (previous["revision"], previous["ends_on"])
        assert db.scalar(select(func.count()).select_from(m.CommandReceipt).where(
            m.CommandReceipt.command_kind == "bulk.apply")) == 0


@pytest.mark.parametrize("endpoint", ["apply", "preview"])
def test_bulk_dispatch_deadlock_retries_the_whole_transaction(h, monkeypatch, endpoint):
    value, before = change(h)
    original = commands.dispatch
    seen = []
    failed = False

    def transient(db, actor, command, settings, *args, **kwargs):
        nonlocal failed
        if command.kind == "membership.update_dates":
            seen.append(command.target_id)
            if command.target_id == before[1]["id"] and not failed:
                failed = True
                raise failure("deadlock")
        return original(db, actor, command, settings, *args, **kwargs)

    monkeypatch.setattr(commands, "dispatch", transient)
    response = request(h, endpoint, value)
    assert response.status_code == 200, response.text
    assert seen == [row["id"] for row in before] * 2
    assert response.json()["counts"]["changed" if endpoint == "apply" else "ready"] == 2
    with transaction(h.app.state.engine) as db:
        for previous in before:
            current = db.get(m.Membership, previous["id"])
            assert current.revision == previous["revision"] + (endpoint == "apply")
            assert current.ends_on.isoformat() == ("2026-12-01" if endpoint == "apply" else previous["ends_on"])
        assert db.scalar(select(func.count()).select_from(m.CommandReceipt).where(
            m.CommandReceipt.command_kind == "bulk.apply")) == (endpoint == "apply")


@pytest.mark.parametrize("endpoint", ["apply", "preview"])
def test_bulk_resolution_database_failure_reaches_transaction_handler(h, monkeypatch, endpoint):
    first, payload, role = relationship(h, "membership")
    later = h.command(role, "membership.create", {**payload, "starts_on": "2026-12-01", "ends_on": "2027-03-01"})["record"]
    value = {"item_kind": "membership.update_dates", "reason": REASON,
             "items": [{"target_id": first["id"], "expected_revision": first["revision"],
                        "payload": {"ends_on": "2027-02-01"}}], "resolutions": {"0": "shift_later_start"}}
    original = commands.dispatch

    def broken(db, actor, command, settings, *args, **kwargs):
        if command.kind == "membership.update_dates" and command.target_id == later["id"]:
            raise failure("unavailable")
        return original(db, actor, command, settings, *args, **kwargs)

    monkeypatch.setattr(commands, "dispatch", broken)
    response = request(h, endpoint, value)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Membership, first["id"]).revision == first["revision"]
        assert db.get(m.Membership, later["id"]).revision == later["revision"]


@pytest.mark.parametrize("endpoint", ["apply", "preview"])
def test_bulk_rule_resolution_generation_keeps_database_failures(h, monkeypatch, endpoint):
    from apps.api.services import rule_catalogue
    from .test_bulk_rule_lines import _active, _body
    from .test_rule_catalogue_api import line, person

    who = person(h.seed["people"][0])
    previous = line(who=who, numbers={"max": {"value": 5}}, starts_on="2026-10-01")
    policy = _active(h, [previous])
    value = _body(policy, [{"who": who, "numbers": {"max": {"value": 3}}, "starts_on": "2026-11-01"}])
    value["resolutions"] = {"0": "end_previous_lines"}
    original = rule_catalogue.check

    def broken(db, actor, catalogue_id, proposal):
        # Only the proposed overlap resolution ends the previous personal line.
        if any(part.get("ends_on") for part in proposal["lines"]):
            raise failure("unavailable")
        return original(db, actor, catalogue_id, proposal)

    monkeypatch.setattr(rule_catalogue, "check", broken)
    response = request(h, endpoint, value)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, policy["id"]).revision == policy["revision"]
        assert not db.scalar(select(m.Policy.id).where(m.Policy.status == "proposed"))


@pytest.mark.parametrize("endpoint", ["apply", "preview"])
@pytest.mark.parametrize("prior_refusal", [False, True])
def test_bulk_diagnostic_approval_rechecks_preserve_deadlines(h, monkeypatch, endpoint, prior_refusal):
    from apps.api.services import bulk

    value, before = change(h)
    value["resolutions"] = {"0": "shift_later_start"}  # Exercise diagnostic approval rechecks.
    calls = 0

    def delayed(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            if prior_refusal:
                raise Problem(409, "RULE_CHECK_REQUIRED", "Synthetic union refusal", {"approval_indexes": [0]})
            return
        raise DatabaseWriteTimeout()

    monkeypatch.setattr(bulk, "_prepare_approval_context", delayed)
    response = request(h, endpoint, value)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "COMMAND_WRITE_TIMEOUT"
    assert calls == 2
    with transaction(h.app.state.engine) as db:
        assert [db.get(m.Membership, row["id"]).revision for row in before] == [row["revision"] for row in before]


@pytest.mark.parametrize("raised, message", [
    (KeyError("synthetic_internal_column"), "A value is not valid for this request"),
    (ValueError("The end date must follow the start date"), "The end date must follow the start date"),
    (ValueError("invalid literal for int() with base 10: 'synthetic_input'"), "A value is not valid for this request"),
])
def test_bulk_preview_row_exception_text_is_screened(h, monkeypatch, raised, message):
    value, before = change(h)
    original = commands.dispatch

    def broken(db, actor, command, settings, *args, **kwargs):
        if command.kind == "membership.update_dates" and command.target_id == before[1]["id"]:
            raise raised
        return original(db, actor, command, settings, *args, **kwargs)

    monkeypatch.setattr(commands, "dispatch", broken)
    response = request(h, "preview", value)
    assert response.status_code == 200, response.text
    row = response.json()["items"][1]
    assert row["result"] == "problem"
    assert [(issue["code"], issue["message"]) for issue in row["issues"]] == [("VALIDATION_ERROR", message)]
    assert "synthetic_" not in response.text

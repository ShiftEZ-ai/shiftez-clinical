"""Planning requests and every shared transaction exit release PostgreSQL transactions and locks."""
import asyncio
import time
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine, event, select, text

from apps.api import models as m
from apps.api.db import WRITE_LOCK_KEY, transaction


@pytest.fixture(scope="module")
def cleanup_harness(scale_harness):
    return scale_harness("pg")


def assert_connections_released(harness):
    # Observe from autocommit: this check must not create its own idle transaction.
    observer = create_engine(harness.settings.database_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    try:
        with observer.connect() as connection:
            assert connection.scalar(text("""
                SELECT count(*) FROM pg_stat_activity
                WHERE datname = current_database() AND state LIKE 'idle in transaction%'
            """)) == 0
            assert connection.scalar(text("""
                SELECT count(*) FROM pg_locks
                WHERE locktype = 'advisory' AND granted AND classid = 0 AND objid = :key
                  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
            """), {"key": WRITE_LOCK_KEY}) == 0
        assert harness.app.state.engine.pool.checkedout() == 0
    finally:
        observer.dispose()


@pytest.mark.postgres
@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("outcome", ["return", "exception", "cancelled"])
def test_shared_transaction_releases_connections_on_every_exit(cleanup_harness, write, outcome):
    pg = cleanup_harness
    original = None

    def operation():
        nonlocal original
        with transaction(pg.app.state.engine, write=write) as session:
            department = session.get(m.Department, pg.seed["department_id"])
            original = department.name
            department.name = "Synthetic transaction cleanup"
            session.flush()
            if outcome == "exception":
                raise ValueError("Synthetic interrupted request")
            if outcome == "cancelled":
                raise asyncio.CancelledError()
            return department.id

    if outcome == "return":
        assert operation() == pg.seed["department_id"]
    else:
        with pytest.raises(ValueError if outcome == "exception" else asyncio.CancelledError):
            operation()
    assert_connections_released(pg)
    with transaction(pg.app.state.engine) as session:
        assert session.get(m.Department, pg.seed["department_id"]).name == (
            "Synthetic transaction cleanup" if outcome == "return" else original)
    assert_connections_released(pg)


@pytest.mark.postgres
def test_overlap_removal_preview_save_and_read_requests_release_transactions(cleanup_harness):
    pg = cleanup_harness
    workspace = pg.shift(pg.workspace("Synthetic overlapping October sessions"))["workspace"]
    workspace = pg.shift(workspace, start="2026-10-04T09:00:00+03:00", end="2026-10-04T11:00:00+03:00")["workspace"]
    before = pg.detail(workspace)
    assert any(f["code"] == "TIME_OVERLAP" for f in before["workspace"]["validation"]["findings"])
    command = {"kind": "shift.action", "expected_revision": workspace["revision"], "payload": {
        "workspace_id": workspace["id"], "shift_ids": [before["shifts"][1]["id"]],
        "action": "remove", "reason": "Remove the synthetic overlapping session"}}
    client = pg.client("coordinator")
    preview = client.post("/api/planner/preview", json=command)
    assert preview.status_code == 200, preview.text
    assert preview.json()["impact"]["counts"]["removed"] == 1
    assert_connections_released(pg)
    assert len(pg.detail(workspace)["shifts"]) == 2, "preview must roll back its savepoint"
    assert_connections_released(pg)

    saved = client.post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())}, json={
        **command, "expected_fingerprint": preview.json()["source_fingerprint"]})
    assert saved.status_code == 200, saved.text
    assert_connections_released(pg)
    for path in ("/api/me", "/api/catalogues?boot=true", "/api/workspaces/" + workspace["id"]):
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert_connections_released(pg)
    after = pg.detail(workspace)
    assert len(after["shifts"]) == 1
    assert not any(f["code"] == "TIME_OVERLAP" for f in after["workspace"]["validation"]["findings"])
    stale = client.post("/api/planner/preview", json=command)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "STALE_REVISION"
    assert_connections_released(pg)


@pytest.mark.postgres
@pytest.mark.parametrize("interrupted", [False, True])
def test_why_releases_read_transaction_before_pure_candidate_checks(cleanup_harness, monkeypatch, interrupted):
    from apps.api.services import staffing

    pg = cleanup_harness
    workspace = pg.shift(pg.workspace("Synthetic explanation read lifecycle"))["workspace"]
    shift = pg.detail(workspace)["shifts"][0]
    original = staffing.check_people
    checked = []

    def check_without_transaction(*args, **kwargs):
        assert_connections_released(pg)
        checked.append(True)
        if interrupted:
            raise ValueError("Synthetic explanation interrupted")
        return original(*args, **kwargs)

    monkeypatch.setattr(staffing, "check_people", check_without_transaction)
    response = pg.client("coordinator").post("/api/planner/why", json={
        "workspace_id": workspace["id"], "expected_revision": workspace["revision"], "shift_ids": [shift["id"]]})
    assert response.status_code == (422 if interrupted else 200), response.text
    assert checked
    assert_connections_released(pg)


@pytest.mark.postgres
@pytest.mark.parametrize("change", ["access_revoked", "department_switched"])
def test_why_reauthenticates_after_pure_computation(cleanup_harness, monkeypatch, change):
    from apps.api.services import staffing

    pg = cleanup_harness
    workspace = pg.shift(pg.workspace("Synthetic explanation authorization"))["workspace"]
    shift = pg.detail(workspace)["shifts"][0]
    identity_id = pg.seed["identities"]["coordinator"]
    original = staffing.check_people
    changed_session = []

    def change_access(*args, **kwargs):
        assert_connections_released(pg)
        with transaction(pg.app.state.engine, write=True) as session:
            if change == "access_revoked":
                session.get(m.Identity, identity_id).disabled = True
            else:
                department = m.Department(organisation_id=pg.seed["organisation_id"], name="Synthetic switched department")
                session.add(department)
                session.flush()
                session.add(m.RoleBinding(identity_id=identity_id, department_id=department.id, role="coordinator",
                                          starts_on=m.now().date()))
                login = session.scalar(select(m.Session).where(m.Session.identity_id == identity_id, m.Session.revoked.is_(False)))
                changed_session.append(login.id)
                login.department_id = department.id
        return original(*args, **kwargs)

    monkeypatch.setattr(staffing, "check_people", change_access)
    try:
        response = pg.client("coordinator").post("/api/planner/why", json={
            "workspace_id": workspace["id"], "expected_revision": workspace["revision"], "shift_ids": [shift["id"]]})
        expected = (401, "ACCESS_REVOKED") if change == "access_revoked" else (409, "PLANNER_CONTEXT_CHANGED")
        assert (response.status_code, response.json()["error"]["code"]) == expected, response.text
        assert "sessions" not in response.json()
        assert_connections_released(pg)
    finally:
        with transaction(pg.app.state.engine, write=True) as session:
            session.get(m.Identity, identity_id).disabled = False
            for session_id in changed_session:
                session.get(m.Session, session_id).department_id = pg.seed["department_id"]


@pytest.mark.postgres
@pytest.mark.parametrize("stage", ["validation", "pairing", "score"])
def test_write_budget_expires_during_pure_checks_and_rolls_back(cleanup_harness, monkeypatch, stage):
    from apps.api import db
    from apps.api.services import planning
    from packages.domain import score

    pg = cleanup_harness
    workspace = pg.workspace("Synthetic CPU budget rollback")
    if stage == "pairing":
        from .test_attachments import END, START, people, setup

        trainee, first, mentor, typ = setup(pg)
        command = {"kind": "shift.create", "expected_revision": workspace["revision"], "payload": {
            "workspace_id": workspace["id"], "assignment_type_id": typ, "location_id": pg.seed["locations"][0],
            "service_ids": [pg.seed["services"][0]], "start": START, "end": END,
            "participants": people(trainee, first, mentor)}}
    else:
        workspace = pg.shift(workspace)["workspace"]
        shift = pg.detail(workspace)["shifts"][0]
        command = {"kind": "shift.action", "expected_revision": workspace["revision"], "payload": {
            "workspace_id": workspace["id"], "shift_ids": [shift["id"]],
            "action": "remove" if stage == "validation" else "lock", "reason": "Synthetic CPU budget rollback"}}
    before = pg.detail(workspace)
    checkpoints, sql_during_compute, computing = [], [], [False]
    clock = [0.0]
    owner, name = (score, "schedule_score") if stage == "score" else (planning, "evaluate")
    original = getattr(owner, name)

    def expire_inside_compute(data, *args, **kwargs):
        assert ("_partial" in data) == (stage == "pairing")
        check = data["_check_budget"]

        def checkpoint():
            checkpoints.append(True)
            if len(checkpoints) == 2:
                clock[0] += pg.settings.command_write_budget_ms / 1000 + 1
            check()

        computing[0] = True
        try:
            return original({**data, "_check_budget": checkpoint}, *args, **kwargs)
        finally:
            computing[0] = False

    def record_statement(connection, cursor, statement, parameters, context, many):
        if computing[0]:
            sql_during_compute.append(True)

    key = str(uuid.uuid4())
    event.listen(pg.app.state.engine, "before_cursor_execute", record_statement)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(owner, name, expire_inside_compute)
            patch.setattr(db, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=time.sleep))
            response = pg.client("coordinator").post("/api/commands", headers={"idempotency-key": key}, json=command)
    finally:
        event.remove(pg.app.state.engine, "before_cursor_execute", record_statement)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "COMMAND_WRITE_TIMEOUT"
    assert len(checkpoints) == 2 and not sql_during_compute
    assert_connections_released(pg)
    assert pg.detail(workspace) == before
    with transaction(pg.app.state.engine) as session:
        assert session.scalar(select(m.CommandReceipt.id).where(m.CommandReceipt.key == key)) is None
    assert_connections_released(pg)


@pytest.mark.postgres
def test_runtime_budget_callback_stays_out_of_saved_snapshots(cleanup_harness):
    import json
    from apps.api.services import planning

    pg = cleanup_harness
    workspace = pg.shift(pg.workspace("Synthetic snapshot serialization"))["workspace"]
    captured = []
    with transaction(pg.app.state.engine, write=True, deadline=time.monotonic() + 120) as session:
        stored = session.get(m.Workspace, workspace["id"])
        planning.revalidate(session, stored, data_out=captured)
        assert "_check_budget" in session.info
        assert "_check_budget" not in captured[0]
        json.dumps(captured[0])
        json.dumps(stored.validation)
    assert_connections_released(pg)

"""Login admission/finalization must not queue behind unrelated department scheduling writes."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import perf_counter
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, insert, select, text

from apps.api import models as m
from apps.api.auth import digest
from apps.api.db import WRITE_LOCK_KEY, transaction
from apps.worker import main as worker
from tests.conftest import TEST_PASSWORD
from tests.test_backend_scale import measure, scale_department
from tests.test_performance_budget import python_budget


@pytest.mark.postgres
def test_department_login_completes_while_scheduling_writer_is_held(pg):
    scale_department(pg)
    with transaction(pg.app.state.engine, write=True) as db:
        db.execute(insert(m.AuthLoginEvent), [{'account_hash': m.uid(), 'peer_hash': m.uid(),
            'outcome': 'INVALID_LOGIN', 'correlation_id': 'synthetic-login-history'} for _ in range(1000)])
    with TestClient(pg.app, base_url=pg.settings.public_origin) as client, ThreadPoolExecutor(max_workers=1) as pool:
        client.headers['origin'] = pg.settings.public_origin
        # An actual application writer remains held until the response succeeds, not a timed sleep.
        with transaction(pg.app.state.engine, write=True):
            result, timing = measure(pg, 'login-during-writer', lambda: pool.submit(client.post,
                '/auth/login', json={'username': 'staff', 'password': TEST_PASSWORD}).result(timeout=15))
            assert result.status_code == 200, result.text
    assert timing['sql'].get('SELECT', 0) <= 55, timing
    return locals()


@pytest.mark.postgres
@pytest.mark.cpu_budget
def test_department_login_cpu_budget(pg):
    state = test_department_login_completes_while_scheduling_writer_is_held(pg)
    assert state['timing']['cpu_seconds'] < python_budget(1), state['timing']


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_department_login_wall_budget(pg):
    state = test_department_login_completes_while_scheduling_writer_is_held(pg)
    assert state['timing']['wall_seconds'] < python_budget(3), state['timing']


@pytest.mark.postgres
def _auth_liveness_writes_bypass_global_writer_and_keep_security_guards(pg):
    """Auth rows use their own locks, so a held rota writer cannot stall liveness paths."""
    object.__setattr__(pg.settings, "password_login_account_limit", 1)
    object.__setattr__(pg.settings, "password_login_progressive_delay", False)
    with TestClient(pg.app, base_url=pg.settings.public_origin) as client:
        client.headers["origin"] = pg.settings.public_origin
        with pg.app.state.engine.begin() as holder:
            holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": WRITE_LOCK_KEY})
            started = perf_counter()
            logged_in = client.post("/auth/login", json={"username": "staff", "password": TEST_PASSWORD})
            elapsed = perf_counter() - started
            assert logged_in.status_code == 200, logged_in.text
            client.headers["x-csrf-token"] = client.cookies["rota_csrf"]
            refreshed = client.post("/auth/refresh")
            assert refreshed.status_code == 200, refreshed.text
            client.headers["x-csrf-token"] = client.cookies["rota_csrf"]
            signed_out = client.post("/auth/logout")
            assert signed_out.status_code == 200, signed_out.text

            rejected = client.post("/auth/login", json={"username": "staff", "password": TEST_PASSWORD + " wrong"})
            limited = client.post("/auth/login", json={"username": "staff", "password": TEST_PASSWORD + " wrong"})
            assert rejected.status_code == 401, rejected.text
            assert limited.status_code == 429, limited.text

    with pg.app.state.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(m.AuthLoginEvent).where(
            m.AuthLoginEvent.account_hash == digest("account:staff"),
            m.AuthLoginEvent.outcome == "INVALID_LOGIN")) >= 1
    return elapsed


@pytest.mark.postgres
def test_auth_liveness_writes_bypass_global_writer_and_keep_security_guards(pg):
    _auth_liveness_writes_bypass_global_writer_and_keep_security_guards(pg)


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_auth_liveness_writes_bypass_global_writer_wall_benchmark(pg):
    elapsed = _auth_liveness_writes_bypass_global_writer_and_keep_security_guards(pg)
    assert elapsed < 1


@pytest.mark.postgres
def test_auth_liveness_during_real_generation_command_does_not_wait_on_credential_rows(pg):
    workspace = pg.workspace("Long liveness rota (test)")
    job = pg.command("coordinator", "job.create", {"workspace_id": workspace["id"], "kind": "generation"},
                     revision=workspace["revision"])["job"]
    entered, release = Event(), Event()

    def slow_generation(snapshot, *, progress, cancelled):
        entered.set()
        assert release.wait(15)
        return {"status": "candidate_feasible", "additions": [], "removals": []}

    with TestClient(pg.app, base_url=pg.settings.public_origin) as client, ThreadPoolExecutor(max_workers=1) as pool:
        client.headers["origin"] = pg.settings.public_origin
        running = pool.submit(worker.process_one, pg.app.state.engine, scheduler=slow_generation)
        try:
            assert entered.wait(15), "generation command did not enter its real scheduler"
            logged_in = client.post("/auth/login", json={"username": "staff", "password": TEST_PASSWORD})
            assert logged_in.status_code == 200, logged_in.text
            client.headers["x-csrf-token"] = client.cookies["rota_csrf"]
            refreshed = client.post("/auth/refresh")
            assert refreshed.status_code == 200, refreshed.text
            client.headers["x-csrf-token"] = client.cookies["rota_csrf"]
            signed_out = client.post("/auth/logout")
            assert signed_out.status_code == 200, signed_out.text
        finally:
            release.set()
        assert running.result(timeout=15), job

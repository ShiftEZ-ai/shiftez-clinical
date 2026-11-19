"""Shared login admission is reserved before verification, including concurrent PostgreSQL requests."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select

from apps.api import auth, models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.rate_limit import counter_key
from packages.contracts.schemas import LoginInput
from packages.domain.time import aware
from .conftest import TEST_PASSWORD


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def throttle_store(request):
    return request.getfixturevalue(request.param)


@pytest.mark.parametrize("same_pair", [True, False])
def test_parallel_progressive_reservations_are_bounded(throttle_store, monkeypatch, same_pair):
    h = throttle_store
    moment = m.now()
    monkeypatch.setattr(auth, "now", lambda: moment)
    settings = replace(h.settings, password_login_account_limit=3)
    verified, barrier = [], Barrier(2)
    def verify(*args):
        verified.append(True)
        return False
    monkeypatch.setattr(auth, "PASSWORDS", SimpleNamespace(verify=verify))
    def attempt(index):
        barrier.wait(timeout=10)
        return auth.password_login(h.app.state.engine, LoginInput(username="staff", password="wrong password"),
                                   "same-peer" if same_pair else f"peer-{index}", settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(8)))
    admitted = 1 if same_pair else 3
    assert sorted(result.status for result in results) == [401] * admitted + [429] * (8 - admitted)
    assert len(verified) == admitted
    with transaction(h.app.state.engine) as db:
        account = auth.login_keys("staff", "unused", settings)[0]
        key, _ = counter_key("login-account", account, settings.password_login_window_seconds, moment)
        assert db.scalar(select(m.RateLimit.hits).where(m.RateLimit.key_hash == key)) == admitted
        if not same_pair:
            delay_key = auth.login_account_bucket_keys("staff", settings)[1]
            assert aware(db.get(m.RateLimit, delay_key).expires_at) == moment + timedelta(seconds=1)


@pytest.mark.postgres
def test_postgres_delay_lock_uses_time_after_wait(pg, monkeypatch):
    moment = [m.now()]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    engine, waiting = pg.app.state.engine, Event()
    delay_key = auth.digest("login-delay:" + auth.login_keys("staff", "victim-peer", pg.settings)[2])
    with transaction(engine, write=True) as db:
        db.add(m.RateLimit(key_hash=delay_key, hits=0, expires_at=moment[0] + timedelta(seconds=1)))
    def attempting_lock(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO rate_limits" in statement and delay_key in parameters.values():
            waiting.set()
    event.listen(engine, "before_cursor_execute", attempting_lock)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with engine.begin() as blocker:
                blocker.execute(select(m.RateLimit.key_hash).where(m.RateLimit.key_hash == delay_key).with_for_update()).one()
                pending = pool.submit(auth.password_login, engine,
                                      LoginInput(username="staff", password=TEST_PASSWORD), "victim-peer", pg.settings)
                assert waiting.wait(10)
                assert not pending.done()
                moment[0] += timedelta(seconds=2)
            assert not isinstance(pending.result(timeout=15), Problem)
    finally:
        event.remove(engine, "before_cursor_execute", attempting_lock)


def test_older_success_preserves_newer_pair_reservation(h, monkeypatch):
    moment = [m.now()]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    entered, resume = Event(), Event()
    original = auth.PASSWORDS.verify
    def verify(encoded, password):
        if password == TEST_PASSWORD:
            entered.set()
            assert resume.wait(15)
        return original(encoded, password)
    monkeypatch.setattr(auth, "PASSWORDS", SimpleNamespace(verify=verify))
    data = LoginInput(username="staff", password=TEST_PASSWORD)
    engine, peer = h.app.state.engine, "same-peer"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(auth.password_login, engine, data, peer, h.settings)
        try:
            assert entered.wait(10)
            moment[0] += timedelta(seconds=1)
            failed = auth.password_login(engine, LoginInput(username="staff", password="wrong password"), peer, h.settings)
            assert isinstance(failed, Problem) and failed.status == 401
        finally:
            resume.set()
        assert not isinstance(pending.result(timeout=15), Problem)
    blocked = auth.password_login(engine, data, peer, h.settings)
    assert isinstance(blocked, Problem) and blocked.status == 429 and blocked.details["retry_after"] == 2


def test_intervening_success_cannot_refund_a_new_pair_failure(throttle_store, monkeypatch):
    h = throttle_store
    moment = [m.now()]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    entered, resume = Event(), Event()
    original = auth.PASSWORDS.verify
    def verify(encoded, password):
        if password == TEST_PASSWORD and not entered.is_set():
            entered.set()
            assert resume.wait(15)
        return original(encoded, password)
    monkeypatch.setattr(auth, "PASSWORDS", SimpleNamespace(verify=verify))
    engine, peer = h.app.state.engine, "same-peer"
    correct = LoginInput(username="staff", password=TEST_PASSWORD)
    wrong = LoginInput(username="staff", password="wrong password")
    with ThreadPoolExecutor(max_workers=1) as pool:
        older = pool.submit(auth.password_login, engine, correct, peer, h.settings)
        try:
            assert entered.wait(10)
            moment[0] += timedelta(seconds=1)
            assert not isinstance(auth.password_login(engine, correct, peer, h.settings), Problem)
            assert auth.password_login(engine, wrong, peer, h.settings).status == 401
        finally:
            resume.set()
        assert not isinstance(older.result(timeout=15), Problem)
    with transaction(engine) as db:
        key, _ = counter_key("login-pair", auth.login_keys("staff", peer, h.settings)[2],
                             h.settings.password_login_window_seconds, moment[0])
        assert db.get(m.RateLimit, key).hits == 1
    moment[0] += timedelta(seconds=1)
    assert auth.password_login(engine, wrong, peer, h.settings).status == 401
    assert auth.password_login(engine, correct, peer, h.settings).details["retry_after"] == 2


@pytest.mark.parametrize("scope", ["account", "address"])
def test_refused_rotating_inputs_leave_no_new_delay_rows(throttle_store, monkeypatch, scope):
    h = throttle_store
    monkeypatch.setattr(auth, "LOGIN_ACCOUNT_BUCKET_COUNT", 1)
    moment = m.now()
    monkeypatch.setattr(auth, "now", lambda: moment)
    settings = replace(h.settings, password_login_account_limit=1 if scope == "account" else 100,
                       password_login_address_limit=1 if scope == "address" else 100)
    engine = h.app.state.engine
    assert auth.password_login(engine, LoginInput(username="staff", password="wrong password"),
                               "first-peer", settings).status == 401
    with transaction(engine) as db:
        existing = set(db.scalars(select(m.RateLimit.key_hash)))
    placeholders = []
    for index in range(12):
        name = "staff" if scope == "account" else f"unknown-{index}"
        peer = f"new-peer-{index}" if scope == "account" else "first-peer"
        keys = auth.login_keys(name, peer, settings)
        placeholders.append(auth.digest("login-delay:" + keys[2]))
        if scope == "address":
            placeholders.extend(key for key in auth.login_account_bucket_keys(name, settings) if key not in existing)
        assert auth.password_login(engine, LoginInput(username=name, password="wrong password"), peer, settings).status == 429
    with transaction(engine) as db:
        assert not db.scalar(select(m.RateLimit.key_hash).where(m.RateLimit.key_hash.in_(placeholders)))
        counter = auth.login_account_bucket_keys("staff", settings)[0]
        assert db.get(m.RateLimit, counter).hits == 1
        if scope == "account":
            key = auth.login_account_bucket_keys("staff", settings)[1]
            assert aware(db.get(m.RateLimit, key).expires_at) == moment + timedelta(seconds=1)
        else:
            key, _ = counter_key("login-address", auth.login_keys("unused", "first-peer", settings)[1],
                                 settings.password_login_window_seconds, moment)
            assert db.get(m.RateLimit, key).hits == 1


@pytest.mark.postgres
@pytest.mark.parametrize("account_active", [False, True])
def test_postgres_expired_delay_prune_between_upsert_and_lock(pg, monkeypatch, account_active):
    from apps.api.rate_limit import enforce
    moment = m.now()
    monkeypatch.setattr(auth, "now", lambda: moment)
    engine = pg.app.state.engine
    keys = auth.login_keys("staff", "pruned-peer", pg.settings)
    pair_key = auth.digest("login-delay:" + keys[2])
    account_key = auth.login_account_bucket_keys("staff", pg.settings)[1]
    expired = moment - timedelta(seconds=1)
    account_deadline = moment + timedelta(seconds=1) if account_active else expired
    with transaction(engine, write=True) as db:
        db.add_all([m.RateLimit(key_hash=pair_key, hits=0, expires_at=expired),
                    m.RateLimit(key_hash=account_key, hits=0, expires_at=account_deadline)])
    before_lock, resume = Event(), Event()
    def pause_before_lock(conn, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement and "rate_limits" in statement:
            before_lock.set()
            assert resume.wait(15)
    event.listen(engine, "before_cursor_execute", pause_before_lock)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(auth.password_login, engine,
                                  LoginInput(username="staff", password="wrong password"), "pruned-peer", pg.settings)
            try:
                assert before_lock.wait(10)
                # PostgreSQL's regular request limiter deliberately does not take the command advisory lock.
                enforce(engine, "delay-prune-race", ["request"], 10, 900, "Synthetic test limit")
                with engine.connect() as conn:
                    remaining = set(conn.scalars(select(m.RateLimit.key_hash)
                                    .where(m.RateLimit.key_hash.in_((pair_key, account_key)))))
                assert remaining == ({account_key} if account_active else set())
            finally:
                resume.set()
            result = pending.result(timeout=15)
        assert isinstance(result, Problem)
        assert result.status == (429 if account_active else 401)
        with transaction(engine) as db:
            if account_active:
                assert aware(db.get(m.RateLimit, account_key).expires_at) == account_deadline
                assert result.message == "Too many sign-in attempts for this account; try again in 1 second"
            else:
                assert aware(db.get(m.RateLimit, pair_key).expires_at) == moment + timedelta(seconds=1)
    finally:
        event.remove(engine, "before_cursor_execute", pause_before_lock)


@pytest.mark.parametrize("username", ["staff", "unknown-person"])
def test_consecutive_account_delay_spans_windows_and_many_addresses(throttle_store, monkeypatch, username):
    h = throttle_store
    moment = [m.now()]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    verified = []
    def verify(encoded, password):
        verified.append(password)
        return password == TEST_PASSWORD
    monkeypatch.setattr(auth, "PASSWORDS", SimpleNamespace(verify=verify))
    engine = h.app.state.engine
    def attempt(peer, name=username, password="wrong password"):
        return auth.password_login(engine, LoginInput(username=name, password=password), peer, h.settings)
    for index in range(100):
        assert attempt(f"spread-{index}").status == 401
        if index < 99:
            moment[0] += timedelta(seconds=h.settings.password_login_window_seconds + 1)
    key = auth.login_account_bucket_keys(username, h.settings)[0]
    for failures in range(100, 113):
        wait = min(86400, 60 * 2 ** (failures - 100))
        for index in range(3):
            blocked = attempt(f"blocked-{failures}-{index}")
            assert (blocked.status, blocked.code, blocked.message, blocked.details) == (
                429, "LOGIN_RATE_LIMIT",
                f"Too many sign-in attempts for this account; try again in {wait} seconds", {"retry_after": wait})
        with transaction(engine) as db:
            row = db.get(m.RateLimit, key)
            assert row.hits == failures
            retention = aware(row.expires_at)
        assert len(verified) == failures
        assert not isinstance(attempt("shared-hospital", "approver", TEST_PASSWORD), Problem)
        verified.pop()
        with transaction(engine) as db:
            assert aware(db.get(m.RateLimit, key).expires_at) == retention
            assert not db.get(m.Identity, h.seed["identities"]["staff"]).disabled
        if failures < 112:
            moment[0] += timedelta(seconds=wait)
            assert attempt(f"spread-{failures}").status == 401


def test_success_clears_consecutive_account_delay_without_resetting_another_user(throttle_store, monkeypatch):
    h = throttle_store
    moment = [m.now()]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    key, delay = auth.login_account_bucket_keys("staff", h.settings)
    other = auth.login_account_bucket_keys("approver", h.settings)[0]
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([m.RateLimit(key_hash=key, hits=100, expires_at=moment[0] + timedelta(days=30)),
                    m.RateLimit(key_hash=delay, hits=0, expires_at=moment[0] + timedelta(seconds=60)),
                    m.RateLimit(key_hash=other, hits=7, expires_at=moment[0] + timedelta(days=30))])
    correct = LoginInput(username="staff", password=TEST_PASSWORD)
    assert auth.password_login(h.app.state.engine, correct, "victim-peer", h.settings).details["retry_after"] == 60
    moment[0] += timedelta(seconds=60)
    assert not isinstance(auth.password_login(h.app.state.engine, correct, "victim-peer", h.settings), Problem)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RateLimit, key).hits == 0
        assert db.get(m.RateLimit, other).hits == 7
    wrong = LoginInput(username="staff", password="wrong password")
    assert auth.password_login(h.app.state.engine, wrong, "victim-peer", h.settings).status == 401
    assert auth.password_login(h.app.state.engine, wrong, "victim-peer", h.settings).details["retry_after"] == 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RateLimit, key).hits == 1


def test_older_success_cannot_clear_new_account_failures_after_a_success_reset(throttle_store, monkeypatch):
    h = throttle_store
    moment = m.now()
    monkeypatch.setattr(auth, "now", lambda: moment)
    entered, resume = Event(), Event()
    original = auth.PASSWORDS.verify
    def verify(encoded, password):
        if password == TEST_PASSWORD and not entered.is_set():
            entered.set()
            assert resume.wait(15)
        return original(encoded, password)
    monkeypatch.setattr(auth, "PASSWORDS", SimpleNamespace(verify=verify))
    engine = h.app.state.engine
    correct = LoginInput(username="staff", password=TEST_PASSWORD)
    wrong = LoginInput(username="staff", password="wrong password")
    with ThreadPoolExecutor(max_workers=1) as pool:
        older = pool.submit(auth.password_login, engine, correct, "old-peer", h.settings)
        try:
            assert entered.wait(10)
            assert not isinstance(auth.password_login(engine, correct, "new-success", h.settings), Problem)
            assert auth.password_login(engine, wrong, "new-failure", h.settings).status == 401
        finally:
            resume.set()
        assert not isinstance(older.result(timeout=15), Problem)
    with transaction(engine) as db:
        key = auth.login_account_bucket_keys("staff", h.settings)[0]
        assert db.get(m.RateLimit, key).hits == 1


def test_busy_verifier_does_not_reserve_a_consecutive_failure(h, monkeypatch):
    from threading import BoundedSemaphore
    semaphore = BoundedSemaphore(1)
    assert semaphore.acquire(blocking=False)
    monkeypatch.setattr(auth, "PASSWORD_VERIFICATIONS", semaphore)
    try:
        result = auth.password_login(h.app.state.engine, LoginInput(username="staff", password="wrong password"),
                                     "busy-peer", h.settings)
        assert result.status == 429 and result.details["retry_after"] == 1
    finally:
        semaphore.release()
    with transaction(h.app.state.engine) as db:
        key = auth.login_account_bucket_keys("staff", h.settings)[0]
        assert db.get(m.RateLimit, key) is None


def test_consecutive_account_delay_reaches_signin_retry_after_contract(h, monkeypatch):
    moment = m.now()
    monkeypatch.setattr(auth, "now", lambda: moment)
    key = auth.login_account_bucket_keys("staff", h.settings)[0]
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.RateLimit(key_hash=key, hits=99, expires_at=moment + timedelta(days=30)))
    client = h.client("coordinator")
    payload = {"username": "staff", "password": "wrong password"}
    assert client.post("/auth/login", json=payload).status_code == 401
    response = client.post("/auth/login", json=payload)
    assert response.status_code == 429
    assert response.json()["error"]["details"]["retry_after"] == 60
    assert response.headers["retry-after"] == "60"


def test_inactive_consecutive_counter_expires_without_becoming_a_cooldown(throttle_store, monkeypatch):
    h = throttle_store
    moment = m.now()
    monkeypatch.setattr(auth, "now", lambda: moment)
    key = auth.login_account_bucket_keys("staff", h.settings)[0]
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.RateLimit(key_hash=key, hits=112, expires_at=moment))
    result = auth.password_login(h.app.state.engine, LoginInput(username="staff", password="wrong password"),
                                 "fresh-peer", h.settings)
    assert result.status == 401
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RateLimit, key).hits == 1
        assert aware(db.get(m.RateLimit, key).expires_at) > moment + timedelta(days=29)


def test_busy_verifier_preserves_the_existing_account_retry_after(h, monkeypatch):
    from threading import BoundedSemaphore
    moment = m.now()
    monkeypatch.setattr(auth, "now", lambda: moment)
    counter, delay = auth.login_account_bucket_keys("staff", h.settings)
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([m.RateLimit(key_hash=counter, hits=111, expires_at=moment + timedelta(days=30)),
                    m.RateLimit(key_hash=delay, hits=0, expires_at=moment + timedelta(days=1))])
    semaphore = BoundedSemaphore(1)
    assert semaphore.acquire(blocking=False)
    monkeypatch.setattr(auth, "PASSWORD_VERIFICATIONS", semaphore)
    try:
        result = auth.password_login(h.app.state.engine, LoginInput(username="staff", password="wrong password"),
                                     "busy-peer", h.settings)
        assert result.status == 429 and result.details["retry_after"] == 86400
    finally:
        semaphore.release()
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RateLimit, counter).hits == 111


def test_counter_timestamp_ceiling_cannot_reuse_a_reservation_stamp(throttle_store, monkeypatch):
    h = throttle_store
    moment = [aware(datetime.max) - timedelta(days=1)]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    verified = []
    monkeypatch.setattr(auth, "PASSWORDS", SimpleNamespace(verify=lambda *_: verified.append(True) or False))
    data = LoginInput(username="staff", password="wrong password")
    assert auth.password_login(h.app.state.engine, data, "first-address", h.settings).status == 401
    moment[0] += timedelta(seconds=61)
    result = auth.password_login(h.app.state.engine, data, "second-address", h.settings)
    assert (result.status, result.code, result.details) == (429, "LOGIN_RATE_LIMIT", {"retry_after": 86400 - 61})
    assert verified == [True]
    with transaction(h.app.state.engine) as db:
        counter = auth.login_account_bucket_keys("staff", h.settings)[0]
        row = db.get(m.RateLimit, counter)
        assert row.hits == 1 and aware(row.expires_at) == aware(datetime.max)
        assert not db.get(m.Identity, h.seed["identities"]["staff"]).disabled


def test_distinct_signin_names_have_a_fixed_consecutive_bucket_bound(throttle_store, monkeypatch):
    h = throttle_store
    moment = [m.now()]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    monkeypatch.setattr(auth, "LOGIN_ACCOUNT_BUCKET_COUNT", 16, raising=False)
    monkeypatch.setattr(auth, "PASSWORDS", SimpleNamespace(verify=lambda *_: False))
    for secret in ("", "01" * 32, "02" * 32):
        settings = replace(h.settings, fmuc_data_key=secret)
        for index in range(64):
            result = auth.password_login(h.app.state.engine,
                LoginInput(username=f"unknown-bucket-{index}", password="wrong password"), "one-address", settings)
            assert result.status == 401
            moment[0] += timedelta(seconds=settings.password_login_window_seconds + 1)
        with transaction(h.app.state.engine) as db:
            retained = db.scalar(select(func.count()).select_from(m.RateLimit)
                .where(m.RateLimit.expires_at > moment[0] + timedelta(days=1)))
            assert retained <= 16


def test_bucket_key_space_is_fixed_across_names_and_secret_rotation():
    from apps.api.settings import Settings
    assert auth.LOGIN_ACCOUNT_BUCKET_COUNT == 1 << 16
    counter_keys, delay_keys = set(), set()
    for secret in ("", "01" * 32, "02" * 32):
        settings = Settings(environment="production" if secret else "development", fmuc_data_key=secret)
        for index in range((1 << 16) + 1):
            counter, delay = auth.login_account_bucket_keys(f"unknown-{index}", settings)
            counter_keys.add(counter)
            delay_keys.add(delay)
    assert len(counter_keys) <= auth.LOGIN_ACCOUNT_BUCKET_COUNT
    assert len(delay_keys) <= auth.LOGIN_ACCOUNT_BUCKET_COUNT


def test_colliding_names_share_delay_and_success_clears_the_bucket(throttle_store, monkeypatch):
    h = throttle_store
    moment = [m.now()]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    monkeypatch.setattr(auth, "LOGIN_ACCOUNT_BUCKET_COUNT", 1)
    counter, delay = auth.login_account_bucket_keys("staff", h.settings)
    assert (counter, delay) == auth.login_account_bucket_keys("unknown-collision", h.settings)
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.RateLimit(key_hash=counter, hits=99, expires_at=moment[0] + timedelta(days=30)))
    def attempt(name, password="wrong password"):
        return auth.password_login(h.app.state.engine, LoginInput(username=name, password=password), name, h.settings)
    assert attempt("unknown-collision").status == 401
    for name in ("staff", "unknown-fresh-collision"):
        response = attempt(name)
        assert response.status == 429 and response.details["retry_after"] == 60
    moment[0] += timedelta(seconds=60)
    assert not isinstance(attempt("staff", TEST_PASSWORD), Problem)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.RateLimit, counter)
        assert row.hits == 0
        expiry = aware(row.expires_at)
        assert not db.get(m.Identity, h.seed["identities"]["staff"]).disabled
    from apps.api.rate_limit import hit
    with transaction(h.app.state.engine, write=True) as db:
        hit(db.connection(), "prune-test", "zero-bucket", 60, expiry + timedelta(microseconds=1))
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RateLimit, counter) is None


def test_progressive_deadlines_stop_at_datetime_max(throttle_store, monkeypatch):
    h = throttle_store
    latest = aware(datetime.max)
    moment = latest - timedelta(microseconds=500_000)
    monkeypatch.setattr(auth, "now", lambda: moment)
    counter, account_delay = auth.login_account_bucket_keys("staff", h.settings)
    pair_delay = auth.digest("login-delay:" + auth.login_keys("staff", "last-peer", h.settings)[2])
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.RateLimit(key_hash=counter, hits=99, expires_at=latest - timedelta(microseconds=1)))
    verified = []
    def verify(*args):
        verified.append(True)
        return False
    monkeypatch.setattr(auth, "PASSWORDS", SimpleNamespace(verify=verify))
    data = LoginInput(username="staff", password="wrong password")
    result = auth.password_login(h.app.state.engine, data, "last-peer", h.settings)
    assert result.status == 401 and verified == [True]
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RateLimit, counter).hits == 100
        assert all(aware(db.get(m.RateLimit, key).expires_at) == latest
                   for key in (counter, account_delay, pair_delay))
    blocked = auth.password_login(h.app.state.engine, data, "last-peer", h.settings)
    assert blocked.status == 429 and blocked.details["retry_after"] == 1
    assert verified == [True]


@pytest.mark.parametrize("failures", [0, 99])
def test_saturated_consecutive_stamp_refuses_without_reusing_a_reservation(throttle_store, monkeypatch, failures):
    h = throttle_store
    latest = aware(datetime.max)
    moment = latest - timedelta(days=2)
    monkeypatch.setattr(auth, "now", lambda: moment)
    counter, account_delay = auth.login_account_bucket_keys("staff", h.settings)
    pair_delay = auth.digest("login-delay:" + auth.login_keys("staff", "saturated-peer", h.settings)[2])
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.RateLimit(key_hash=counter, hits=failures, expires_at=latest))
    monkeypatch.setattr(auth, "PASSWORDS", SimpleNamespace(verify=lambda *_: pytest.fail("No distinct reservation stamp remains")))
    monkeypatch.setattr(auth, "PASSWORD_VERIFICATIONS", SimpleNamespace(
        acquire=lambda **_: pytest.fail("A refused reservation must not take a verifier slot")))
    result = auth.password_login(h.app.state.engine, LoginInput(username="staff", password=TEST_PASSWORD),
                                 "saturated-peer", h.settings)
    assert (result.status, result.code, result.details) == (429, "LOGIN_RATE_LIMIT", {"retry_after": 86400})
    with transaction(h.app.state.engine) as db:
        row = db.get(m.RateLimit, counter)
        assert row.hits == failures and aware(row.expires_at) == latest
        assert db.get(m.RateLimit, account_delay) is None and db.get(m.RateLimit, pair_delay) is None

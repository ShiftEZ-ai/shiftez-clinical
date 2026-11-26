"""Password verification reserves bounded capacity without blocking departmental writes."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import logging
from threading import BoundedSemaphore, Event
from types import SimpleNamespace
from time import process_time

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api import auth, identity_store, models as m
from apps.api.db import transaction
from apps.api.rate_limit import counter_key
from packages.contracts.schemas import LoginInput
from .test_password_login import PASSWORD, USERNAME, client_for, failure_counts, pin_login_clock, post_login, provision
from .test_performance_budget import python_budget


@pytest.mark.parametrize('backend', ['managed', 'dummy', 'fallback', 'local'])
def test_password_verification_saturation_refuses_rotating_names_without_waiting(h, monkeypatch, backend):
    entered, resume = Event(), Event()
    calls = []

    def verify(*args):
        calls.append(args)
        entered.set()
        assert resume.wait(15)
        return False

    def managed_verify(*args):
        if backend == 'fallback':
            raise identity_store.IdentityStoreError('Synthetic unreadable users file')
        verify(*args)
        return None

    users = SimpleNamespace(verify_credential=managed_verify) if backend in {'managed', 'fallback'} else None
    monkeypatch.setattr(identity_store, 'store_for', lambda _: users)
    monkeypatch.setattr(auth, 'PASSWORDS', SimpleNamespace(verify=verify))
    monkeypatch.setattr(auth, 'PASSWORD_VERIFICATIONS', BoundedSemaphore(1), raising=False)
    with client_for(h, h.settings) as client, ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(post_login, client, 'staff' if backend == 'local' else 'unknown-held')
        try:
            assert entered.wait(15)
            refused = pool.submit(post_login, client, 'another-unknown-name').result(timeout=5)
            assert refused.status_code == 429, refused.text
            assert refused.json()['error']['code'] == 'LOGIN_RATE_LIMIT'
            assert refused.headers['retry-after'] == '1'
            assert len(calls) == 1
        finally:
            resume.set()
        assert pending.result(timeout=15).status_code == 401
        assert post_login(client, 'unknown-after-release').status_code == 401
        assert len(calls) == 2


def test_rehash_holds_the_password_verification_slot(h, tmp_path, monkeypatch):
    import json
    settings, users, _ = provision(h, tmp_path)
    data = json.loads(users.path.read_text(encoding='utf-8'))
    upgraded = data['users'][USERNAME]['password']
    data['users'][USERNAME]['password'] = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    users.path.write_text(json.dumps(data), encoding='utf-8')
    entered, resume = Event(), Event()

    def rehash(password):
        entered.set()
        assert resume.wait(15)
        return upgraded

    monkeypatch.setattr(identity_store, 'hash_password', rehash)
    monkeypatch.setattr(auth, 'PASSWORD_VERIFICATIONS', BoundedSemaphore(1), raising=False)
    with client_for(h, settings) as client, ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(post_login, client)
        try:
            assert entered.wait(15)
            refused = pool.submit(post_login, client, 'unknown-during-rehash').result(timeout=5)
            assert refused.status_code == 429, refused.text
            assert refused.json()['error']['code'] == 'LOGIN_RATE_LIMIT'
        finally:
            resume.set()
        assert pending.result(timeout=15).status_code == 200
        assert post_login(client).status_code == 200


def test_password_verification_error_releases_admission(h, monkeypatch, caplog):
    def failed(*args):
        raise RuntimeError('Synthetic verifier failure')

    monkeypatch.setattr(identity_store, 'store_for', lambda _: SimpleNamespace(verify_credential=failed))
    monkeypatch.setattr(auth, 'PASSWORD_VERIFICATIONS', BoundedSemaphore(1), raising=False)
    caplog.set_level(logging.ERROR, "rota.auth")
    with pytest.raises(RuntimeError, match='Synthetic verifier failure'):
        auth.password_login(h.app.state.engine, LoginInput(username='unknown-failure', password=PASSWORD), 'testclient', h.settings)
    assert any(record.message == "password_login_failed" for record in caplog.records)
    monkeypatch.setattr(identity_store, 'store_for', lambda _: None)
    monkeypatch.setattr(auth, 'PASSWORDS', SimpleNamespace(verify=lambda *_: False))
    with client_for(h, h.settings) as client:
        assert post_login(client, 'unknown-after-error').status_code == 401


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def login_store(request):
    return request.getfixturevalue(request.param)


def test_rotating_names_cannot_evade_address_backstop_and_other_addresses_remain_usable(login_store, tmp_path, monkeypatch):
    pin_login_clock(monkeypatch)
    settings, _, _ = provision(login_store, tmp_path)
    settings = replace(settings, password_login_address_limit=3)
    with client_for(login_store, settings) as client:
        for number in range(3):
            assert post_login(client, f'unknown-{number}').status_code == 401
        assert post_login(client).status_code == 429
    with TestClient(login_store.app, base_url=settings.public_origin, client=('198.51.100.42', 12345)) as other:
        other.headers['origin'] = settings.public_origin
        # The seeded local identity uses a separate address and has independent failure capacity.
        from .conftest import TEST_PASSWORD
        assert post_login(other, 'admin', TEST_PASSWORD).status_code == 200


@pytest.mark.parametrize('change', ['disabled', 'unaccepted', 'rebound', 'reset', 'store_disabled', 'removed', 'roles'])
def test_writes_continue_during_hash_and_stale_credentials_never_issue_sessions(login_store, tmp_path, monkeypatch, change):
    pin_login_clock(monkeypatch)
    settings, users, identity_id = provision(login_store, tmp_path)
    verified, resume = Event(), Event()
    original = identity_store.AutheliaUsersFile.verify_credential

    def pause(store, username, password):
        credential = original(store, username, password)
        verified.set()
        assert resume.wait(15), 'The unrelated write must finish while verification is paused'
        return credential

    monkeypatch.setattr(identity_store.AutheliaUsersFile, 'verify_credential', pause)
    with client_for(login_store, settings) as client, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(post_login, client)
        try:
            assert verified.wait(15)
            with transaction(login_store.app.state.engine, write=True) as db:
                account = db.scalar(select(m.Account).where(m.Account.identity_id == identity_id))
                if change == 'disabled':
                    db.get(m.Identity, identity_id).disabled = True
                elif change == 'unaccepted':
                    account.accepted_at = None
                elif change == 'rebound':
                    account.identity_id = login_store.seed['identities']['admin']
                elif change == 'reset':
                    users.set_password(USERNAME, PASSWORD + ' replacement')
                elif change == 'store_disabled':
                    users.set_disabled(USERNAME, True)
                elif change == 'removed':
                    users.remove(USERNAME)
                else:
                    for role in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == identity_id)):
                        role.revoked = True
        finally:
            resume.set()
        response = pending.result(timeout=15)
    if change == 'roles':
        assert response.status_code == 403 and response.json()['error']['code'] == 'UNPROVISIONED_IDENTITY', response.text
        assert failure_counts(login_store, settings) == [0, 0]
    else:
        assert response.status_code == 401, response.text
        assert failure_counts(login_store, settings) == [1, 1]
    with transaction(login_store.app.state.engine) as db:
        assert not list(db.scalars(select(m.Session).where(m.Session.identity_id == identity_id)))


def test_parallel_success_releases_only_its_own_failure_reservation(login_store, tmp_path, monkeypatch):
    pin_login_clock(monkeypatch)
    settings, _, _ = provision(login_store, tmp_path)
    settings = replace(settings, password_login_account_limit=2)
    entered, resume = Event(), Event()
    original = identity_store.AutheliaUsersFile.verify_credential

    def pause(store, username, password):
        if password == PASSWORD:
            entered.set()
            assert resume.wait(15)
        return original(store, username, password)

    monkeypatch.setattr(identity_store.AutheliaUsersFile, 'verify_credential', pause)
    with client_for(login_store, settings) as client, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(post_login, client)
        try:
            assert entered.wait(15)
            assert post_login(client, password=PASSWORD + ' wrong').status_code == 401
            assert post_login(client, password=PASSWORD + ' wrong').status_code == 429
        finally:
            resume.set()
        assert pending.result(timeout=15).status_code == 200
    assert failure_counts(login_store, settings) == [1, 1]


@pytest.mark.postgres
def test_correct_password_reports_sign_in_busy_during_credential_edit_and_retry_succeeds(pg, tmp_path, monkeypatch):
    pin_login_clock(monkeypatch)
    settings, _, identity_id = provision(pg, tmp_path)
    settings = replace(settings, password_login_progressive_delay=True)
    engine = pg.app.state.engine
    with client_for(pg, settings) as client:
        holder = engine.connect()
        tx = holder.begin()
        try:
            holder.execute(update(m.Account).where(m.Account.identity_id == identity_id)
                           .values(revision=m.Account.revision + 1))
            before = failure_counts(pg, settings)
            busy = post_login(client)
            assert busy.status_code == 409, busy.text
            assert busy.json()["error"]["code"] == "SIGN_IN_BUSY"
            assert busy.headers["retry-after"] == "1"
            assert failure_counts(pg, settings) == before
            with transaction(engine) as db:
                events = list(db.scalars(select(m.AuthLoginEvent)))
                assert [event.outcome for event in events] == ["SIGN_IN_BUSY"]
                keys = auth.login_keys(USERNAME, "testclient", settings)
                consecutive, account_delay = auth.login_account_bucket_keys(USERNAME, settings)
                pair_delay = auth.digest("login-delay:" + keys[2])
                pair_key, _ = counter_key("login-pair", keys[2], settings.password_login_window_seconds, auth.now())
                assert db.scalar(select(m.RateLimit.hits).where(m.RateLimit.key_hash == pair_key)) is None
                assert db.get(m.RateLimit, pair_delay) is None
                assert db.scalar(select(m.RateLimit.hits).where(m.RateLimit.key_hash == consecutive)) == 0
                assert db.get(m.RateLimit, account_delay) is None
        finally:
            tx.rollback()
            holder.close()
        retried = post_login(client)
        assert retried.status_code == 200, retried.text


def test_local_password_reset_during_verification_wins(login_store, monkeypatch):
    from .conftest import TEST_PASSWORD
    identity_id = login_store.seed['identities']['staff']
    with transaction(login_store.app.state.engine) as db:
        stored = db.get(m.Identity, identity_id).password_hash
    verified, resume = Event(), Event()
    original = type(auth.PASSWORDS).verify

    def pause(hasher, encoded, password):
        result = original(hasher, encoded, password)
        if encoded == stored:
            verified.set()
            assert resume.wait(15)
        return result

    monkeypatch.setattr(type(auth.PASSWORDS), 'verify', pause)
    with client_for(login_store, login_store.settings) as client, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(post_login, client, 'staff', TEST_PASSWORD)
        try:
            assert verified.wait(15)
            with transaction(login_store.app.state.engine, write=True) as db:
                db.get(m.Identity, identity_id).password_hash = None
        finally:
            resume.set()
        assert pending.result(timeout=15).status_code == 401


def test_managed_login_rehashes_weaker_credential_then_rechecks_the_upgrade(login_store, tmp_path, monkeypatch):
    import json
    settings, users, _ = provision(login_store, tmp_path)
    data = json.loads(users.path.read_text(encoding='utf-8'))
    data['users'][USERNAME]['password'] = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PASSWORD)
    users.path.write_text(json.dumps(data), encoding='utf-8')
    verified = []
    original = type(identity_store.HASHER).verify

    def record(hasher, stored, password):
        verified.append(stored)
        return original(hasher, stored, password)

    monkeypatch.setattr(type(identity_store.HASHER), 'verify', record)
    with client_for(login_store, settings) as client:
        assert post_login(client).status_code == 200
    assert len(verified) == 1  # Final credential revalidation must not hash again under the write lock.
    upgraded = json.loads(users.path.read_text(encoding='utf-8'))['users'][USERNAME]['password']
    assert not identity_store.HASHER.check_needs_rehash(upgraded)


@pytest.mark.parametrize('change', ['rehash', 'reset', 'disable', 'remove'])
def test_login_rechecks_a_concurrent_rehash_without_overwriting_credential_changes(
        login_store, tmp_path, monkeypatch, change):
    import json
    settings, users, identity_id = provision(login_store, tmp_path)
    data = json.loads(users.path.read_text(encoding='utf-8'))
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PASSWORD)
    data['users'][USERNAME]['password'] = weak
    users.path.write_text(json.dumps(data), encoding='utf-8')
    original = type(identity_store.HASHER).verify
    changed, winner = False, None

    def interleave(hasher, stored, password):
        nonlocal changed, winner
        result = original(hasher, stored, password)
        if stored == weak and not changed:
            changed = True
            if change == 'rehash':
                assert users.verify_credential(USERNAME, PASSWORD) is not None
            elif change == 'reset':
                users.set_password(USERNAME, PASSWORD + ' replacement')
            elif change == 'disable':
                users.set_disabled(USERNAME, True)
            else:
                users.remove(USERNAME)
            winner = users.path.read_bytes()
        return result

    monkeypatch.setattr(type(identity_store.HASHER), 'verify', interleave)
    with client_for(login_store, settings) as client:
        response = post_login(client)
    assert response.status_code == (200 if change == 'rehash' else 401), response.text
    assert changed and users.path.read_bytes() == winner
    assert failure_counts(login_store, settings) == ([0, 0] if change == 'rehash' else [1, 1])
    with transaction(login_store.app.state.engine) as db:
        sessions = list(db.scalars(select(m.Session).where(m.Session.identity_id == identity_id)))
        assert len(sessions) == (1 if change == 'rehash' else 0)


def test_concurrent_rehash_reverification_is_bounded(tmp_path, monkeypatch):
    import json
    path = tmp_path / 'users.yml'
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    path.write_text(json.dumps({'users': {USERNAME: {'displayname': 'Synthetic (test)',
                                                   'password': weak.hash(PASSWORD)}}}), encoding='utf-8')
    users = identity_store.AutheliaUsersFile(path)
    original, attempts = users._change, []

    def replace_before_rehash(apply):
        attempts.append(True)
        original(lambda current: current[USERNAME].update(password=weak.hash(PASSWORD)))
        original(apply)

    monkeypatch.setattr(users, '_change', replace_before_rehash)
    assert users.verify_credential(USERNAME, PASSWORD) is None
    assert len(attempts) == 2


@pytest.mark.parametrize('parameters', [dict(time_cost=4), dict(memory_cost=65544), dict(parallelism=5),
                                      dict(hash_len=33), dict(salt_len=17), dict(time_cost=1, memory_cost=65544)])
def test_login_never_rehashes_down_any_argon_parameter(tmp_path, monkeypatch, parameters):
    import json
    settings = dict(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)
    stored = PasswordHasher(**{**settings, **parameters}).hash(PASSWORD)
    path = tmp_path / 'users.yml'
    path.write_text(json.dumps({'users': {USERNAME: {'displayname': 'Synthetic (test)', 'password': stored}}}), encoding='utf-8')
    users = identity_store.AutheliaUsersFile(path)
    monkeypatch.setattr(users, '_change', lambda *_: pytest.fail('Stronger or mixed parameters must be preserved'))
    assert users.verify(USERNAME, PASSWORD)
    assert json.loads(path.read_text(encoding='utf-8'))['users'][USERNAME]['password'] == stored


@pytest.mark.cpu_budget
def test_production_argon_password_roundtrips_have_bounded_cpu(h, tmp_path):
    settings, _, _ = provision(h, tmp_path)
    with client_for(h, settings) as client:
        started = process_time()
        for _ in range(3):
            response = post_login(client)
            assert response.status_code == 200, response.text
        elapsed = process_time() - started
    print('password production_argon_roundtrips cpu_seconds', elapsed)
    assert elapsed < python_budget(3)


@pytest.mark.cpu_budget
def test_throttled_password_spray_has_bounded_cpu_and_never_hashes(h, monkeypatch):
    pin_login_clock(monkeypatch)
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        key, ends = counter_key('login-address', auth.login_keys('unknown', 'testclient', h.settings)[1],
                                h.settings.password_login_window_seconds, auth.now())
        db.add(m.RateLimit(key_hash=key, hits=h.settings.password_login_address_limit, expires_at=ends))
    monkeypatch.setattr(identity_store.AutheliaUsersFile, 'verify_credential', lambda *_: pytest.fail('Throttled requests cannot hash'))
    started = process_time()
    for number in range(50):
        response = auth.password_login(engine, LoginInput(username=f'unknown-{number}', password=PASSWORD), 'testclient', h.settings)
        assert response.status == 429
    elapsed = process_time() - started
    print('password throttle cpu_seconds', elapsed)
    assert elapsed < python_budget(2)

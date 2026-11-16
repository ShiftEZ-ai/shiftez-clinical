"""Read-only GitHub checks release database locks and cannot apply to changed credentials."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
import threading
import uuid

import httpx
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import github_integrations as github
from .test_github_integrations import TOKEN, OTHER_TOKEN, REPO, configure, connect, read, saved, repository_response


@contextmanager
def paused_repository(monkeypatch, token=TOKEN, *, only_first=False):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def get(url, **options):
        calls.append((url, options))
        if options['headers']['Authorization'] == 'Bearer ' + token and (not only_first or len(calls) == 1):
            entered.set()
            assert release.wait(15), 'The synthetic GitHub response was not released'
        return httpx.Response(200, json={'has_issues': True}, request=httpx.Request('GET', url))

    monkeypatch.setattr(github.httpx, 'get', get)
    try:
        yield entered, release, calls
    finally:
        release.set()


def send(client, kind, payload, key=None, revision=None):
    return client.post('/api/commands', headers={'idempotency-key': key or str(uuid.uuid4())},
                       json={'kind': 'integration.github.' + kind, 'payload': payload, 'expected_revision': revision})


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('kind', ['connect', 'test'])
def test_held_repository_check_allows_an_unrelated_write(database, request, monkeypatch, kind):
    h = request.getfixturevalue(database)
    configure(h)
    repository_response(monkeypatch)
    if kind == 'test':
        connect(h)
    client = h.client('operator')
    h.client('coordinator')
    payload = {'repo': REPO, 'token': TOKEN} if kind == 'connect' else {}
    with paused_repository(monkeypatch) as (entered, release, calls), ThreadPoolExecutor(max_workers=1) as pool:
        checking = pool.submit(send, client, kind, payload)
        try:
            assert entered.wait(10), 'The repository check did not start'
            workspace = h.workspace('An unrelated write during the GitHub check')
            assert workspace['name'] == 'An unrelated write during the GitHub check'
            assert not checking.done(), 'The remote check must still be held when the unrelated write commits'
        finally:
            release.set()
        reply = checking.result(timeout=10)
    assert reply.status_code == 200, reply.text
    assert reply.json()['last_test_ok'] is True and len(calls) == 1


@pytest.mark.parametrize('kind,existing,change', [
    ('connect', False, 'replace'), ('connect', True, 'replace'), ('connect', True, 'reconnect'),
    ('test', True, 'replace'), ('test', True, 'reconnect'),
])
def test_held_result_cannot_change_a_newer_credential_lifetime(h, monkeypatch, kind, existing, change):
    configure(h)
    repository_response(monkeypatch)
    first = connect(h) if existing else None
    client = h.client('operator')
    checked_token = OTHER_TOKEN if kind == 'connect' else TOKEN
    payload = {'repo': REPO, 'token': checked_token} if kind == 'connect' else {}
    final_token = 'github_pat_fictional_final_connection_only_567890QRST'
    key = str(uuid.uuid4())
    with paused_repository(monkeypatch, checked_token) as (entered, release, _), ThreadPoolExecutor(max_workers=1) as pool:
        checking = pool.submit(send, client, kind, payload, key, first['revision'] if first else None)
        try:
            assert entered.wait(10)
            if change == 'reconnect':
                h.command('operator', 'integration.github.disconnect', {'revision': first['revision']})
            current = connect(h, final_token, revision=first['revision'] if first and change == 'replace' else None)
        finally:
            release.set()
        reply = checking.result(timeout=10)
    assert reply.status_code == 409 and reply.json()['error']['code'] == 'STALE_REVISION', reply.text
    assert read(h)['revision'] == current['revision']
    assert github.decrypt(saved(h), h.settings) == final_token
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.key == key)) is None


@pytest.mark.parametrize('change,status', [('role', 403), ('session', 401), ('department', 409)])
def test_repository_result_rechecks_current_access_and_department(h, monkeypatch, change, status):
    configure(h)
    client = h.client('operator')
    identity_id, department_id = h.seed['identities']['operator'], h.seed['department_id']
    with transaction(h.app.state.engine, write=True) as db:
        session_id = db.scalar(select(m.Session.id).where(m.Session.identity_id == identity_id))
        if change == 'department':
            other = m.Department(organisation_id=h.seed['organisation_id'], name='Fictional other connection department')
            db.add(other)
            db.flush()
            other_id = other.id
            db.add(m.RoleBinding(identity_id=identity_id, department_id=other_id, role='operator', starts_on=date(2020, 1, 1)))
    key = str(uuid.uuid4())
    with paused_repository(monkeypatch) as (entered, release, _), ThreadPoolExecutor(max_workers=1) as pool:
        checking = pool.submit(send, client, 'connect', {'repo': REPO, 'token': TOKEN}, key)
        try:
            assert entered.wait(10)
            with transaction(h.app.state.engine, write=True) as db:
                if change == 'role':
                    for binding in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == identity_id,
                                              m.RoleBinding.department_id == department_id)):
                        binding.revoked = True
                elif change == 'session':
                    db.get(m.Session, session_id).revoked = True
                else:
                    db.get(m.Session, session_id).department_id = other_id
        finally:
            release.set()
        reply = checking.result(timeout=10)
    assert reply.status_code == status, reply.text
    if change == 'department':
        assert reply.json()['error']['code'] == 'INTEGRATION_CONTEXT_CHANGED'
    with transaction(h.app.state.engine) as db:
        assert not list(db.scalars(select(m.IntegrationCredential)))
        assert db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.key == key)) is None


@pytest.mark.parametrize('kind', ['connect', 'test'])
def test_receipt_replay_calls_no_upstream_even_after_department_rate_limit(h, monkeypatch, kind):
    configure(h)
    calls = repository_response(monkeypatch)
    if kind == 'test':
        connect(h)
    client = h.client('operator')
    key = str(uuid.uuid4())
    payload = {'repo': REPO, 'token': TOKEN} if kind == 'connect' else {}
    first = send(client, kind, payload, key)
    assert first.status_code == 200, first.text
    for _ in range(10 - len(calls)):
        h.command('operator', 'integration.github.test', {})
    assert len(calls) == 10
    monkeypatch.setattr(github.httpx, 'get', lambda *args, **kwargs: pytest.fail('A receipt replay called GitHub'))
    replay = send(client, kind, payload, key)
    assert replay.status_code == 200 and replay.json() == first.json(), replay.text


def test_concurrent_same_key_reuses_committed_receipt_before_stale_context_check(h, monkeypatch):
    configure(h)
    client = h.client('operator')
    key = str(uuid.uuid4())
    payload = {'repo': REPO, 'token': TOKEN}
    with paused_repository(monkeypatch, only_first=True) as (entered, release, calls), ThreadPoolExecutor(max_workers=1) as pool:
        checking = pool.submit(send, client, 'connect', payload, key)
        try:
            assert entered.wait(10)
            committed = send(client, 'connect', payload, key)
            assert committed.status_code == 200, committed.text
        finally:
            release.set()
        replay = checking.result(timeout=10)
    assert replay.status_code == 200 and replay.json() == committed.json(), replay.text
    assert len(calls) == 2 and read(h)['revision'] == committed.json()['revision']
    with transaction(h.app.state.engine) as db:
        receipts = list(db.scalars(select(m.CommandReceipt).where(m.CommandReceipt.key == key)))
        audits = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'integration.github.connect')))
        assert len(receipts) == len(audits) == 1


@pytest.mark.parametrize('kind', ['connect', 'test'])
def test_receipt_committed_between_preflight_reads_skips_another_check(h, monkeypatch, kind):
    from apps.api import rate_limit
    configure(h)
    calls = repository_response(monkeypatch)
    if kind == 'test':
        connect(h)
    previous_calls = len(calls)
    client = h.client('operator')
    key = str(uuid.uuid4())
    payload = {'repo': REPO, 'token': TOKEN} if kind == 'connect' else {}
    nested = []
    original = rate_limit.enforce
    interleaved = False

    def enforce(*args, **kwargs):
        nonlocal interleaved
        result = original(*args, **kwargs)
        if args[1] == 'integration-github-check' and not interleaved:
            interleaved = True
            nested.append(send(client, kind, payload, key))
        return result

    monkeypatch.setattr(rate_limit, 'enforce', enforce)
    reply = send(client, kind, payload, key)
    assert len(nested) == 1 and nested[0].status_code == 200, [r.text for r in nested]
    assert reply.status_code == 200 and reply.json() == nested[0].json(), reply.text
    assert len(calls) == previous_calls + 1, 'The committed replay must not make another repository GET'


@pytest.mark.parametrize("harness", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("replace", [False, True])
def test_credential_change_before_token_access_keeps_preflight_coherent(harness, replace, request, monkeypatch):
    h = request.getfixturevalue(harness)
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    configure(h)
    calls = repository_response(monkeypatch)
    first = connect(h)
    calls.clear()
    entered, release = threading.Event(), threading.Event()

    def hold_credential(db, row):
        if isinstance(row, m.IntegrationCredential) and not entered.is_set():
            # Metadata is materialized here, before decrypt can load deferred ciphertext.
            entered.set()
            assert release.wait(30), "Test did not release the credential read"

    key = str(uuid.uuid4())
    event.listen(Session, "loaded_as_persistent", hold_credential)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(h.command, "operator", "integration.github.test", {}, key=key, status=409)
            try:
                assert entered.wait(10), "Credential metadata was not read"
                if replace:
                    connect(h, OTHER_TOKEN, repo="another/feedback", revision=first["revision"])
                else:
                    h.command("operator", "integration.github.disconnect", {"revision": first["revision"]})
                after_change = read(h)
                ciphertext = saved(h).token_ciphertext if replace else None
            finally:
                release.set()
            result = pending.result(timeout=10)
    finally:
        event.remove(Session, "loaded_as_persistent", hold_credential)

    assert result["error"]["code"] == "STALE_REVISION"
    assert calls[-1][0] == "https://api.github.com/repos/" + REPO
    assert calls[-1][1]["headers"]["Authorization"] == "Bearer " + TOKEN
    assert read(h) == after_change
    if replace:
        assert saved(h).token_ciphertext == ciphertext
    else:
        assert saved(h) is None
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.CommandReceipt.id).where(m.CommandReceipt.key == key)) is None
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == "integration.github.test")) is None

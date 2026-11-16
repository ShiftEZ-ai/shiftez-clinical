"""A manual rota check computes from a read snapshot, then saves only current results."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import uuid

import pytest
from sqlalchemy import select, text

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning

CHECK_PAYLOAD = {'reason': 'Check synthetic rota'}


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def check_harness(request):
    h = request.getfixturevalue(request.param)
    h.client('coordinator')
    h.client('staff')
    h.client('admin')
    h.client('approver')
    h.checked_workspace = h.workspace()
    return h


@pytest.fixture
def paused_check(monkeypatch):
    entered, release = Event(), Event()
    evaluate = planning.evaluate

    def held(data, **options):
        if not entered.is_set():
            assert all(isinstance(data.get(key), dict) for key in (
                '_scope_interval_cache', '_policy_applies_cache', '_allocation_match_cache'))
            entered.set()
            assert release.wait(30), 'Test did not release the rota check'
        return evaluate(data, **options)

    monkeypatch.setattr(planning, 'evaluate', held)
    yield entered, release
    release.set()


def stored_check(h, workspace):
    with transaction(h.app.state.engine) as db:
        from packages.domain.validation import INTERNAL_CHECK_KEYS
        check = db.get(m.Workspace, workspace['id']).validation
        return {key: value for key, value in check.items() if key not in INTERNAL_CHECK_KEYS}


def assert_no_receipt(h, key):
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.CommandReceipt.id).where(m.CommandReceipt.key == key)) is None
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'workspace.validate')) is None


def test_slow_check_allows_an_ordinary_write_and_preserves_reply_and_audit(check_harness, paused_check):
    h = check_harness
    workspace = h.checked_workspace
    before = stored_check(h, workspace)
    entered, release = paused_check
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        check = pool.submit(h.command, 'coordinator', 'workspace.validate', CHECK_PAYLOAD, workspace['id'], workspace['revision'], key=key)
        try:
            assert entered.wait(10)
            saved = pool.submit(h.command, 'staff', 'table_view.create', {
                'table_key': 'people', 'name': 'During rota check', 'definition': {}})
            assert saved.result(timeout=10)['view']['name'] == 'During rota check'
            assert not check.done()
            assert stored_check(h, workspace) == before
        finally:
            release.set()
        result = check.result(timeout=10)
    assert result['workspace']['revision'] == workspace['revision']
    assert result['validation'] == stored_check(h, workspace)
    with transaction(h.app.state.engine) as db:
        receipt = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.key == key))
        assert receipt.result['receipt_id'] == result['receipt_id']
        assert 'findings' not in receipt.result['validation']
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'workspace.validate'))
        assert audit.target_id == workspace['id'] and audit.detail['receipt_id'] == receipt.id
        assert list(db.scalars(select(m.ChangeSet).where(m.ChangeSet.workspace_id == workspace['id']))) == []


@pytest.mark.parametrize('change', ['workspace', 'policy', 'configuration', 'wish'])
def test_check_rejects_inputs_changed_during_computation(check_harness, paused_check, change):
    h = check_harness
    workspace = h.checked_workspace
    before = stored_check(h, workspace)
    entered, release = paused_check
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        check = pool.submit(h.command, 'coordinator', 'workspace.validate', CHECK_PAYLOAD, workspace['id'], workspace['revision'],
                            key=key, status=409)
        try:
            assert entered.wait(10)
            if change == 'workspace':
                h.command('coordinator', 'workspace.archive', {'reason': 'Concurrent draft change'},
                          workspace['id'], workspace['revision'])
            elif change == 'policy':
                h.policy({'kind': 'restriction'}, {'person_id': h.seed['people'][0]})
            elif change == 'configuration':
                h.configure('categories', {'name': 'Concurrent category', 'starts_on': '2020-01-01'})
            else:
                h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'kind': 'prefer_off',
                          'first_day': '2026-10-05', 'last_day': '2026-10-05', 'points': 1})
        finally:
            release.set()
        result = check.result(timeout=10)
    assert result['error']['code'] == 'STALE_INPUTS'
    assert result['error']['details']['recoverable']
    assert result['error']['details']['validation']
    assert stored_check(h, workspace) == before
    assert_no_receipt(h, key)


def test_check_reauthenticates_before_saving(check_harness, paused_check):
    h = check_harness
    workspace = h.checked_workspace
    before = stored_check(h, workspace)
    entered, release = paused_check
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        check = pool.submit(h.command, 'coordinator', 'workspace.validate', CHECK_PAYLOAD, workspace['id'], workspace['revision'],
                            key=key, status=401)
        try:
            assert entered.wait(10)
            with transaction(h.app.state.engine, write=True) as db:
                for session in db.scalars(select(m.Session).where(m.Session.identity_id == h.seed['identities']['coordinator'])):
                    session.revoked = True
        finally:
            release.set()
        assert check.result(timeout=10)['error']['code'] == 'SESSION_EXPIRED'
    assert stored_check(h, workspace) == before
    assert_no_receipt(h, key)


def test_check_uses_a_coherent_read_snapshot(check_harness, monkeypatch):
    h = check_harness
    workspace = h.checked_workspace
    entered, release = Event(), Event()
    snapshot = planning.domain_snapshot
    seen = []

    def held(db, workspace, **options):
        if not seen:
            assert 'rota_write' not in db.connection().info
            if db.bind.dialect.name == 'postgresql':
                assert db.scalar(text('SHOW transaction_isolation')) == 'repeatable read'
                assert db.scalar(text('SHOW transaction_read_only')) == 'on'
            entered.set()
            assert release.wait(120)
        else:
            assert 'rota_write' not in db.connection().info
            if db.bind.dialect.name == 'postgresql':
                assert db.scalar(text('SHOW transaction_isolation')) == 'repeatable read'
                assert db.scalar(text('SHOW transaction_read_only')) == 'on'
        data = snapshot(db, workspace, **options)
        seen.append([row['name'] for row in data['categories'].values()])
        return data

    monkeypatch.setattr(planning, 'domain_snapshot', held)
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        check = pool.submit(h.command, 'coordinator', 'workspace.validate', CHECK_PAYLOAD, workspace['id'], workspace['revision'],
                            key=key, status=409)
        try:
            assert entered.wait(10)
            h.configure('categories', {'name': 'Added after snapshot began', 'starts_on': '2020-01-01'})
        finally:
            release.set()
        assert check.result(timeout=10)['error']['code'] == 'STALE_INPUTS'
    assert 'Added after snapshot began' not in seen[0]
    assert 'Added after snapshot began' in seen[-1]
    assert_no_receipt(h, key)


def test_receipt_replay_skips_computation_even_after_inputs_change(check_harness, monkeypatch):
    h = check_harness
    workspace = h.checked_workspace
    key = str(uuid.uuid4())
    result = h.command('coordinator', 'workspace.validate', CHECK_PAYLOAD, workspace['id'], workspace['revision'], key=key)
    h.command('coordinator', 'workspace.archive', {'reason': 'Changed after check'}, workspace['id'], workspace['revision'])

    def unexpected(*args, **kwargs):
        pytest.fail('A committed receipt must not run the check again')

    monkeypatch.setattr(planning, 'revalidate', unexpected)
    replay = h.command('coordinator', 'workspace.validate', CHECK_PAYLOAD, workspace['id'], workspace['revision'], key=key)
    # The harness hydrates the immutable summary from the unchanged stored full check.
    assert replay == result
    conflict = h.command('coordinator', 'workspace.validate', {'reason': 'Different content'},
                         workspace['id'], workspace['revision'], key=key, status=409)
    assert conflict['error']['code'] == 'IDEMPOTENCY_CONFLICT'


def test_receipt_committed_during_check_replays_before_stale_input_guard(check_harness, paused_check):
    h = check_harness
    workspace = h.checked_workspace
    entered, release = paused_check
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(h.command, 'coordinator', 'workspace.validate', CHECK_PAYLOAD,
                            workspace['id'], workspace['revision'], key=key)
        try:
            assert entered.wait(10)
            committed = h.command('coordinator', 'workspace.validate', CHECK_PAYLOAD,
                                  workspace['id'], workspace['revision'], key=key)
            h.command('coordinator', 'workspace.archive', {'reason': 'After concurrent check'},
                      workspace['id'], workspace['revision'])
        finally:
            release.set()
        assert first.result(timeout=10) == committed
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.CommandReceipt.id).where(m.CommandReceipt.key == key)))) == 1
        assert len(list(db.scalars(select(m.AuditEvent.id).where(m.AuditEvent.action == 'workspace.validate')))) == 1

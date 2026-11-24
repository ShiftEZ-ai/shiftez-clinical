"""Synchronous impact previews roll back and leave the instance writer available."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text, update

from apps.api import commands, models as m
from apps.api.services import planner_tools
from tests.clock_support import freeze_clock
from tests.test_backend_scale import scale_department
from tests.test_planner_tools import _assign_command


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_preview_comparison_does_not_block_an_independent_session_save(scale_harness, database, monkeypatch):
    h = scale_harness(database)
    workspace, data, _, _ = scale_department(h)
    sid = min(data['focus_ids'])
    if database == 'pg':
        draft = h.shift(h.workspace('Independent draft', start='2026-12-06', end='2026-12-13'),
                        start='2026-12-06T08:00:00+03:00', end='2026-12-06T12:00:00+03:00',
                        people=[])['workspace']
        save = _assign_command(h, draft, h.detail(draft)['shifts'][0], h.seed['people'][0])
    preview = {'kind': 'shift.action', 'expected_revision': workspace['revision'], 'payload': {
        'workspace_id': workspace['id'], 'shift_ids': [sid], 'action': 'cancel', 'reason': 'Trial change'}}
    client = h.client('coordinator')
    before = h.detail(workspace)
    freeze_clock(monkeypatch)
    headers = {'accept-encoding': 'identity'}
    reference = client.post('/api/planner/preview', headers=headers, json=preview)
    assert reference.status_code == 200, reference.text

    if database == 'h':
        # SQLite requires its database writer for the trial mutation.
        repeated = client.post('/api/planner/preview', headers=headers, json=preview)
        assert repeated.status_code == 200, repeated.text
        assert repeated.content == reference.content
        assert h.detail(workspace) == before
        return

    comparing, resume = Event(), Event()
    difference = planner_tools.difference

    def pause_comparison(*args):
        comparing.set()
        assert resume.wait(20), 'Concurrent save did not finish'
        return difference(*args)

    retry_write = commands.retry_write

    def save_with_short_budget(engine, operation, **options):
        settings = engine.get_execution_options()['rota_write_settings']
        bounded = engine.execution_options(rota_write_settings=replace(settings, command_write_budget_ms=2_000))
        return retry_write(bounded, operation, **options)

    monkeypatch.setattr(planner_tools, 'difference', pause_comparison)
    monkeypatch.setattr(commands, 'retry_write', save_with_short_budget)
    with TestClient(h.app, base_url=h.settings.public_origin, cookies=client.cookies,
                    headers={**client.headers, **headers}) as preview_client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(preview_client.post, '/api/planner/preview', json=preview)
            try:
                assert comparing.wait(20), 'Preview did not reach the comparison'
                saved = client.post('/api/commands', headers={'idempotency-key': str(uuid4())}, json=save)
            finally:
                resume.set()
            concurrent = result.result(timeout=20)
    assert concurrent.status_code == 200, concurrent.text
    assert concurrent.content == reference.content
    assert h.detail(workspace) == before
    assert saved.status_code == 200, saved.text


@pytest.mark.parametrize('hold_through_timeout', [False, True], ids=['release', 'timeout'])
def test_same_workspace_save_during_preview_trial(pg, monkeypatch, request, hold_through_timeout):
    workspace = pg.shift(pg.workspace(), people=[])['workspace']
    workspace = pg.shift(workspace, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00',
                         people=[])['workspace']
    client = pg.client('coordinator')
    freeze_clock(monkeypatch)
    before = pg.detail(workspace)
    trial_shift, save_shift = before['shifts']
    person = pg.seed['people'][0]
    save = _assign_command(pg, workspace, save_shift, person)
    preview = {'kind': 'shift.action', 'expected_revision': workspace['revision'], 'payload': {
        'workspace_id': workspace['id'], 'shift_ids': [trial_shift['id']], 'action': 'cancel', 'reason': 'Trial change'}}
    headers = {'accept-encoding': 'identity'}
    reference = client.post('/api/planner/preview', headers=headers, json=preview)
    assert reference.status_code == 200, reference.text

    engine = pg.app.state.engine
    settings = engine.get_execution_options()['rota_write_settings']
    budget_ms = 5_000
    engine.update_execution_options(rota_write_settings=replace(settings, command_write_budget_ms=budget_ms))
    comparing, resume = Event(), Event()
    run_preview, difference = planner_tools.preview, planner_tools.difference

    def identify_trial(db, *args):
        nonlocal trial_pid
        trial_pid = db.scalar(text('SELECT pg_backend_pid()'))
        return run_preview(db, *args)

    def pause_comparison(*args):
        comparing.set()
        assert resume.wait(15), 'Concurrent save did not finish'
        return difference(*args)

    def save_session():
        started = monotonic()
        reply = client.post('/api/commands', headers={'idempotency-key': str(uuid4())}, json=save)
        return reply, round((monotonic() - started) * 1000)

    trial_pid = None
    monkeypatch.setattr(planner_tools, 'preview', identify_trial)
    monkeypatch.setattr(planner_tools, 'difference', pause_comparison)
    with TestClient(pg.app, base_url=pg.settings.public_origin, cookies=client.cookies,
                    headers={**client.headers, **headers}) as preview_client, ThreadPoolExecutor(2) as pool:
        pending_preview = pool.submit(preview_client.post, '/api/planner/preview', json=preview)
        try:
            assert comparing.wait(15), 'Preview did not finish its trial writes'
            pending_save = pool.submit(save_session)
            deadline = monotonic() + budget_ms / 1000
            with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as observer:
                while not pending_save.done() and monotonic() < deadline:
                    lock = observer.scalar(text("SELECT wait_event FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                        "AND :trial = ANY(pg_blocking_pids(pid))"), {'trial': trial_pid})
                    if lock:
                        break
                    resume.wait(0.01)
                else:
                    pytest.fail('The save did not wait on the preview transaction')
            blocked_at = monotonic()
            if hold_through_timeout:
                refused, _ = pending_save.result(timeout=budget_ms / 1000)
                assert refused.status_code == 503, refused.text
                assert refused.json()['error']['code'] == 'COMMAND_WRITE_TIMEOUT'
        finally:
            resume.set()
        observed_wait_ms = round((monotonic() - blocked_at) * 1000)
        saved, elapsed_ms = pending_save.result(timeout=15)
        concurrent = pending_preview.result(timeout=15)

    request.node.user_properties.extend([('lock_wait_event', lock), ('observed_wait_ms', observed_wait_ms),
                                         ('save_status', saved.status_code), ('save_elapsed_ms', elapsed_ms)])
    assert concurrent.status_code == 200, concurrent.text
    assert concurrent.content == reference.content
    if hold_through_timeout:
        assert pg.detail(workspace) == before
        saved, _ = save_session()
    assert saved.status_code == 200, saved.text
    detail = pg.detail(workspace)
    assert detail['workspace']['revision'] == before['workspace']['revision'] + 1
    assert detail['workspace']['content_revision'] == before['workspace']['content_revision'] + 1
    assert detail['shifts'][0] == trial_shift
    assert detail['shifts'][1]['revision'] == save_shift['revision'] + 1
    assert [row['person_id'] for row in detail['shifts'][1]['participants']] == [person]
    assert [row['id'] for row in detail['shifts']] == [trial_shift['id'], save_shift['id']]
    with engine.connect() as observer:
        assert observer.scalar(select(func.count()).select_from(m.ChangeSet).where(
            m.ChangeSet.workspace_id == workspace['id'],
            m.ChangeSet.resulting_content_revision > before['workspace']['content_revision'])) == 1


@pytest.mark.parametrize('attempts_in_conflict', [1, 3], ids=['recover', 'exhausted'])
def test_preview_returns_a_retriable_response_when_serialization_retries_are_exhausted(
        pg, monkeypatch, attempts_in_conflict):
    workspace = pg.shift(pg.workspace())['workspace']
    before = pg.detail(workspace)
    shift_id = before['shifts'][0]['id']
    preview = {'kind': 'shift.action', 'expected_revision': workspace['revision'], 'payload': {
        'workspace_id': workspace['id'], 'shift_ids': [shift_id], 'action': 'cancel', 'reason': 'Trial change'}}
    client = pg.client('coordinator')
    freeze_clock(monkeypatch)
    reference = client.post('/api/planner/preview', json=preview)
    assert reference.status_code == 200, reference.text

    engine = pg.app.state.engine
    workspace_content = planner_tools.workspace_content
    conflicts = []

    def change_row_version(db, workspace):
        content = workspace_content(db, workspace)
        if len(conflicts) < attempts_in_conflict:
            # The backend replaces the row version after the preview has read its snapshot.
            with engine.begin() as other:
                other.execute(update(m.Shift).where(m.Shift.id == shift_id).values(revision=m.Shift.revision))
        return content

    def capture_conflict(context):
        if getattr(context.original_exception, 'sqlstate', None) == '40001':
            conflicts.append(True)

    monkeypatch.setattr(planner_tools, 'workspace_content', change_row_version)
    event.listen(engine, 'handle_error', capture_conflict)
    try:
        response = client.post('/api/planner/preview', json=preview)
    finally:
        event.remove(engine, 'handle_error', capture_conflict)
    assert len(conflicts) == attempts_in_conflict
    if attempts_in_conflict == 1:
        assert response.status_code == 200, response.text
        assert response.content == reference.content
    else:
        assert response.status_code == 503, response.text
        assert response.json()['error']['code'] == 'COMMAND_WRITE_TIMEOUT'
    assert pg.detail(workspace) == before

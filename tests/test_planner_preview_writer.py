"""Synchronous impact previews roll back and leave the instance writer available."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api import commands
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

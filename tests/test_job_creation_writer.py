"""Feasibility evidence is captured before the shared writer and checked again at admission."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import logging
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import delete

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import jobs
from tests.clock_support import freeze_clock
from tests.test_backend_scale import scale_department
from tests.test_events import event, event_type, repeat
from tests.test_feasibility_creation_performance import proposed_batch


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_feasibility_captures_no_snapshots_in_the_command_writer(scale_harness, database, monkeypatch, caplog, request):
    h = scale_harness(database)
    workspace, data, *_ = scale_department(h)
    changes = proposed_batch(h, data, count=1)
    other = h.workspace('Independent draft', start='2026-12-06', end='2026-12-13')
    payload = {'workspace_id': workspace['id'], 'kind': 'feasibility', 'rule_changes': changes}
    key = str(uuid4())
    original = jobs.domain_snapshot
    started, resume = Event(), Event()
    captures = []

    def capture(db, *args, **kwargs):
        captures.append(bool(db.connection().info.get('rota_write')))
        if len(captures) == 1:
            started.set()
            assert resume.wait(20), 'The concurrent save did not finish'
        return original(db, *args, **kwargs)

    monkeypatch.setattr(jobs, 'domain_snapshot', capture)
    caplog.set_level(logging.INFO, logger='rota.db')
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(h.command, 'coordinator', 'job.create', payload, revision=workspace['revision'], key=key)
        try:
            assert started.wait(15), 'The job did not start capturing evidence'
            assert captures == [False], 'Feasibility started a full capture under the command writer'
            save_log_start = len(caplog.records)
            began = monotonic()
            saved = h.shift(other, start='2026-12-06T08:00:00+03:00', end='2026-12-06T12:00:00+03:00', people=[])
            elapsed = round((monotonic() - began) * 1000, 2)
        finally:
            resume.set()
        queued = pending.result(timeout=20)

    assert captures == [False, False]
    assert saved['workspace']['revision'] == other['revision'] + 1
    acquired, = [row for row in caplog.records[save_log_start:] if row.message == 'write_lock_acquired'
                 and getattr(row, 'command_kind', None) == 'shift.create']
    request.node.user_properties.extend([('save_writer_wait_ms', acquired.lock_wait_ms), ('save_elapsed_ms', elapsed)])
    assert h.command('coordinator', 'job.create', payload, revision=workspace['revision'], key=key) == queued
    assert captures == [False, False]


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_feasibility_refuses_changed_inputs_without_storing_a_receipt(h, monkeypatch):
    workspace = h.workspace()
    key = str(uuid4())
    payload = {'workspace_id': workspace['id'], 'kind': 'feasibility'}
    original = jobs.domain_snapshot
    captured, resume = Event(), Event()

    def capture(db, *args, **kwargs):
        result = original(db, *args, **kwargs)
        if kwargs.get('policy_overrides') is not None:
            captured.set()
            assert resume.wait(20), 'The input edit did not finish'
        return result

    monkeypatch.setattr(jobs, 'domain_snapshot', capture)
    h.client('coordinator')
    h.client('admin')
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(h.command, 'coordinator', 'job.create', payload, revision=workspace['revision'],
                              key=key, status=409)
        try:
            assert captured.wait(15), 'The job did not capture both snapshots'
            h.configure('services', {'name': 'Additional service', 'starts_on': '2026-01-01'})
        finally:
            resume.set()
        refused = pending.result(timeout=20)

    assert refused['error']['code'] == 'STALE_INPUTS'
    assert h.committed('coordinator', key) is None
    assert h.command('coordinator', 'job.create', payload, revision=workspace['revision'], key=key)['job']['kind'] == 'feasibility'


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_feasibility_refuses_an_expired_projected_occurrence_on_the_same_day(h, monkeypatch):
    clock = [datetime.fromisoformat('2026-10-11T06:58:00+00:00')]
    freeze_clock(monkeypatch, lambda: clock[0])
    event(h, event_type(h, blocks_duties=True, default_audience={'everyone': True}), repeat=repeat())
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(delete(m.EventOccurrence))
    workspace = h.workspace()
    key = str(uuid4())
    payload = {'workspace_id': workspace['id'], 'kind': 'feasibility'}
    original = jobs.domain_snapshot

    def capture(db, *args, **kwargs):
        result = original(db, *args, **kwargs)
        if kwargs.get('policy_overrides') is not None:
            clock[0] = datetime.fromisoformat('2026-10-11T07:01:00+00:00')
        return result

    monkeypatch.setattr(jobs, 'domain_snapshot', capture)
    refused = h.command('coordinator', 'job.create', payload, revision=workspace['revision'], key=key, status=409)
    assert refused['error']['code'] == 'STALE_INPUTS'
    assert h.committed('coordinator', key) is None

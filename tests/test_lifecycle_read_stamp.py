"""A lifecycle check is stored only for the inputs its writer can prove: counters, the draft baseline and the clock."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import delete

from apps.api import models as m
from apps.api.db import transaction
from tests.clock_support import freeze_clock
from tests.test_events import event, event_type, repeat
from tests.test_lifecycle_short_writer import forbid_writer_snapshots
from tests.test_workspace_check_concurrency import (  # noqa: F401
    check_harness, paused_check, CHECK_PAYLOAD, stored_check)


@pytest.fixture
def clock(monkeypatch):
    # Sunday 09:30 in the event's zone; frozen before the harness signs anyone in.
    now = {'value': datetime(2026, 10, 11, 6, 30, tzinfo=timezone.utc)}
    freeze_clock(monkeypatch, lambda: now['value'])
    return now


def legacy_draft(h):
    """An older draft created before baselines were captured with every draft."""
    with transaction(h.app.state.engine, write=True) as db:
        actor = h.seed['identities']['coordinator']
        row = m.Workspace(id=m.uid(), created_at=m.now(), department_id=h.seed['department_id'], created_by=actor,
                          last_edited_by=actor, name='Legacy draft without a baseline', starts_on=date(2026, 10, 4),
                          ends_on=date(2026, 10, 11), status='draft', revision=1, content_revision=1)
        db.add(row)
        db.flush()
        return {'id': row.id, 'revision': row.revision}


def during_check(h, paused_check, kind, workspace, action, status):
    entered, release = paused_check
    path = {'validate': None, 'submit': 'submit'}[kind]
    with ThreadPoolExecutor(max_workers=1) as pool:
        if path is None:
            reply = pool.submit(h.command, 'coordinator', 'workspace.validate', CHECK_PAYLOAD,
                                workspace['id'], workspace['revision'], status=status)
        else:
            reply = pool.submit(h.lifecycle, workspace, path, status=status)
        try:
            assert entered.wait(10)
            action()
        finally:
            release.set()
        return reply.result(timeout=30)


@pytest.mark.parametrize('kind', ['validate', 'submit'])
def test_a_first_baseline_captured_during_the_check_is_seen_by_the_writer(check_harness, paused_check, monkeypatch, kind):
    h = check_harness
    workspace = legacy_draft(h)
    calls = forbid_writer_snapshots(monkeypatch)
    reply = during_check(h, paused_check, kind, workspace, lambda: h.command(
        'coordinator', 'workspace.baseline.capture', {'reason': 'Capture during check'},
        workspace['id'], workspace['revision']), 409)
    # No counter moved, yet the writer re-read the inputs outside the lock and found the captured baseline.
    assert reply['error']['code'] == 'STALE_INPUTS' and reply['error']['details']['recoverable']
    assert reply['error']['details']['current_fingerprint'] == h.snapshot(workspace)['fingerprint']
    assert len(calls) == 3, 'The check, the capture and one canonical re-read, all outside the writer'


@pytest.mark.parametrize('later, outcome', [
    # Sunday 09:30 to 10:30 in the event's zone: the weekly 09:00-10:00 occurrence ends during the check.
    ('2026-10-11T07:30:00+00:00', 'STALE_INPUTS'),
    # Nothing ends: the projection is unchanged and the check is stored as computed, with no second read.
    ('2026-10-11T06:45:00+00:00', None),
])
def test_an_unmaterialised_occurrence_ending_during_the_check_is_seen_by_the_writer(
        clock, check_harness, request, monkeypatch, later, outcome):
    h = check_harness
    event(h, event_type(h, blocks_duties=True, default_audience={'everyone': True}), repeat=repeat())
    with transaction(h.app.state.engine, write=True) as db:
        # Not yet materialised (the daily horizon job lags): every occurrence is projected from the clock.
        db.execute(delete(m.EventOccurrence))
    workspace = h.shift(h.workspace('Recurring event currency', start='2026-10-03', end='2026-10-25'))['workspace']
    # Pause only the check itself, not the rota's creation.
    paused_check = request.getfixturevalue('paused_check')
    calls = forbid_writer_snapshots(monkeypatch)
    reply = during_check(h, paused_check, 'validate', workspace,
                         lambda: clock.update(value=datetime.fromisoformat(later)), 409 if outcome else 200)
    if outcome:
        assert reply['error']['code'] == outcome and reply['error']['details']['recoverable']
        assert len(calls) == 2
    else:
        assert reply['validation'] == stored_check(h, workspace)
        assert len(calls) == 1

"""Comparable PostgreSQL writer measurements; existing performance budgets remain authoritative."""
import json
import os
from pathlib import Path
from time import perf_counter

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api import workspace_baselines
from apps.api.db import transaction
from apps.api.snapshots import currency_hint
from tests.test_bulk_single_commands import SOURCE, recorded_leave
from tests.test_bulk_draft_session_creation import creation


@pytest.mark.postgres
@pytest.mark.parametrize('writer', ['sessions_500', 'availability_import_500', 'session_save'])
def test_snapshot_stamp_writer_cost(pg, writer):
    h = pg
    workspace = h.workspace()
    h.client('coordinator')
    if writer == 'availability_import_500':
        manifest = {'archive_sha256': 'c' * 64, 'files': [{'path': 'content/leave.json', 'value': {
            'leave': [{'staffId': 'stamp-person', 'from': '2026-10-04', 'to': '2026-10-05',
                       'type': 'Other', 'note': ''} for _ in range(500)]}}]}
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.ReferenceImport(department_id=h.seed['department_id'], source_name=SOURCE,
                                     archive_sha256=manifest['archive_sha256'], manifest=manifest))
            db.add(m.SourceMapping(department_id=h.seed['department_id'], source_name=SOURCE,
                                  source_key='person:stamp-person', target_kind='people', target_id=h.seed['people'][0]))
    statements = []
    def counted(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.split()[0])
    event.listen(h.app.state.engine, 'before_cursor_execute', counted)
    try:
        start = perf_counter()
        if writer == 'availability_import_500':
            with transaction(h.app.state.engine, write=True) as db:
                result = recorded_leave(db, manifest)
            assert result['added'] == 500
        elif writer == 'sessions_500':
            result = h.command('coordinator', 'bulk.apply', {'items': [creation(h, workspace) for _ in range(500)],
                                'reason': 'Snapshot stamp measurement'})
            assert result['counts']['changed'] == 500
        else:
            result = h.command('coordinator', 'shift.create', creation(h, workspace)['payload'],
                               revision=workspace['revision'])
            assert result['shift_id']
        elapsed = (perf_counter() - start) * 1000
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', counted)
    metrics = {'writer': writer, 'statements': len(statements), 'ms': round(elapsed, 3)}
    print(metrics)
    if output := os.getenv('G4_STAMP_MEASUREMENTS'):
        with Path(output).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(metrics) + '\n')


@pytest.mark.postgres
def test_currency_hint_baseline_read_cost_is_session_memoized(pg, monkeypatch):
    from tests.test_backend_scale import scale_department
    h = pg
    workspace, _, _, _ = scale_department(h)
    calls = 0
    real_latest = workspace_baselines.latest

    def counted_latest(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_latest(*args, **kwargs)

    monkeypatch.setattr(workspace_baselines, 'latest', counted_latest)
    statements = []
    def counted_sql(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.split()[0].upper())
    start = perf_counter()
    event.listen(h.app.state.engine, 'before_cursor_execute', counted_sql)
    try:
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Workspace, workspace['id'])
            for _ in range(100):
                currency_hint(db, row)
            location = db.get(m.Location, h.seed['locations'][0])
            location.name = location.name + ' (stamp)'
            db.flush()
            currency_hint(db, row)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', counted_sql)
    metrics = {'currency_hint_calls': 101, 'latest_calls': calls,
               'sql': len(statements), 'ms': round((perf_counter() - start) * 1000, 3)}
    print(metrics)
    if output := os.getenv('G4_STAMP_MEASUREMENTS'):
        with Path(output).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'writer': 'currency_hint_department_scale', **metrics}) + '\n')
    assert calls == 2


@pytest.mark.postgres
def test_nonblocking_event_invalidation_cost(pg):
    from tests.test_backend_scale import measure
    from tests.test_events import event as create_event, event_type, semantic_fingerprints
    from tests.test_be18_command_latency import edit
    h = pg
    published = h.shift(h.workspace())['workspace']
    h.publish(published)
    draft = h.shift(h.workspace('Event invalidation cost', start='2026-10-11', end='2026-10-18'),
        start='2026-10-11T08:00:00+03:00', end='2026-10-11T12:00:00+03:00')['workspace']
    typ = event_type(h, blocks_duties=False, rsvp=False)
    path = '/api/schedule/sessions?start=2026-10-04&end=2026-10-18'
    h.get('coordinator', path)
    def observed(label, operation):
        result, metrics = measure(h, label, operation)
        row = {'writer': label, 'statements': sum(metrics['sql'].values()),
               'ms': round(metrics['wall_seconds'] * 1000, 3)}
        if output := os.getenv('G4_STAMP_MEASUREMENTS'):
            with Path(output).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row) + '\n')
        return result
    observed('warmed_schedule_read', lambda: h.get('coordinator', path))
    shift = h.detail(draft)['shifts'][0]
    saved = observed('warmed_session_save', lambda: edit(h, draft, shift, shift['participants']))
    draft = saved['workspace']
    shift = h.detail(draft)['shifts'][0]
    semantic = semantic_fingerprints(h, draft)
    observed('nonblocking_event_write', lambda: create_event(h, typ))
    assert semantic_fingerprints(h, draft) == semantic
    observed('event_followed_by_schedule_read', lambda: h.get('coordinator', path))
    observed('event_followed_by_session_save', lambda: edit(h, draft, shift, shift['participants']))


@pytest.mark.postgres
def test_nonblocking_event_stamps_preserve_existing_approval_and_publication(pg):
    from tests.test_events import event as create_event, event_type, semantic_fingerprints, definition, input_of
    h = pg
    typ = event_type(h, blocks_duties=False, rsvp=False)
    workspace = h.shift(h.workspace())['workspace']
    before = semantic_fingerprints(h, workspace)
    submitted = h.lifecycle(workspace, 'submit')['workspace']
    row = create_event(h, typ)
    assert semantic_fingerprints(h, submitted) == before
    approved = h.lifecycle(submitted, 'approve', 'approver')['workspace']
    master = definition(h, row)
    h.configure('events', input_of(master, heading='Still nonblocking'), row['id'], master['revision'], role='coordinator')
    assert semantic_fingerprints(h, approved) == before
    issued = h.lifecycle(approved, 'publish')
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication, issued['publication_id']).fingerprint == before[1]


@pytest.mark.postgres
def test_full_fairness_rebuild_cost(pg):
    from apps.api.services import fairness
    from tests.test_backend_scale import scale_department, measure
    h = pg
    workspace, _, _, _ = scale_department(h)
    h.publish(workspace)
    with transaction(h.app.state.engine, write=True) as db:
        first = fairness.backfill(db, h.seed['department_id'])
    def rebuild():
        with transaction(h.app.state.engine, write=True) as db:
            return fairness.backfill(db, h.seed['department_id'])
    result, metrics = measure(h, 'full_fairness_rebuild_768', rebuild)
    assert result['publications'] == first['publications'] == 1
    assert result['rows'] == first['rows'] and result['rows'] > 0
    if output := os.getenv('G4_STAMP_MEASUREMENTS'):
        with Path(output).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'writer': 'full_fairness_rebuild_768', 'statements': sum(metrics['sql'].values()),
                                     'ms': round(metrics['wall_seconds'] * 1000, 3), 'rows': result['rows']}) + '\n')

"""Scoped invalidations are bounded hints; lost information always requests a full refresh."""
import ast
import asyncio
from dataclasses import replace
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url

from apps.api import live
from apps.api.db import transaction
from apps.api.settings import Settings
from packages.contracts import schemas as s


@pytest.fixture
def history():
    live.attach(None)
    live.configure(replace(Settings(), live_history_size=3, live_history_topics=4))
    try:
        yield live
    finally:
        live.detach(live._loop)


def test_history_deduplicates_within_exact_version_and_overflow_is_unknown(history):
    change = {'scope': 'workspace', 'id': 'rota'}
    history._bump('department', change)
    history._bump('department', change)
    through = history.version('department')
    history._bump('department', {'scope': 'inbox', 'id': None})
    assert history.changes_since('department', 0, through) == [change]
    assert history.changes_since('department', 0) == [change, {'scope': 'inbox', 'id': None}]
    history._bump('department', change)
    assert history.changes_since('department', 0) is None
    assert history.changes_since('department', through) == [{'scope': 'inbox', 'id': None}, change]
    history._bump('department')
    assert history.changes_since('department', through) is None
    assert history.changes_since('department', history.version('department')) == []


def test_workspace_validation_change_keeps_job_identity(history):
    history._bump('department', {'scope': 'workspace', 'id': 'rota', 'job_id': 'check'})
    assert history.changes_since('department', 0) == [
        {'scope': 'workspace', 'id': 'rota', 'job_id': 'check'}]
    history._bump('department', {'scope': 'workspace', 'id': 'rota', 'job_id': 'new-check'})
    assert history.changes_since('department', 1) == [
        {'scope': 'workspace', 'id': 'rota', 'job_id': 'new-check'}]


def _workspace_edit_result(*, base=4, revision=5, stale=False, partial=False):
    workspace_id, shift_id = str(uuid4()), str(uuid4())
    delta = {'base_revision': base, 'revision': revision, 'stale': stale,
             'shifts': [{'id': shift_id, 'participants': []}], 'removed_shift_ids': [],
             'assignments': [], 'removed_assignment_ids': [],
             'findings': {'shift_ids': [shift_id], 'person_ids': [], 'items': []},
             'coverage': {'shift_ids': [shift_id], 'person_ids': [], 'items': []},
             'summary_counts': {}, 'current_fingerprint': 'f' * 64}
    if partial:
        delta.pop('coverage')
    return workspace_id, {'workspace': {'id': workspace_id, 'revision': revision, 'content_revision': revision + 1},
                          'validation': {'fingerprint': 'f' * 64}, 'health': {'checked': True},
                          'fix_list_delta': {'cleared': [], 'new': [], 'changed': []}, 'delta': delta}


def test_workspace_edit_delta_is_carried_only_when_revisions_match():
    workspace_id, result = _workspace_edit_result()
    result['workspace']['undo_edit'] = {'own': True, 'author': 'Synthetic editor'}
    changes = live.command_changes('shift.update', {'workspace_id': workspace_id}, result)
    assert changes and changes[0]['scope'] == 'workspace' and changes[0]['id'] == workspace_id
    assert changes[0]['delta']['base_revision'] == 4
    assert changes[0]['delta']['revision'] == result['workspace']['revision']
    assert changes[0]['workspace']['content_revision'] == result['workspace']['content_revision']
    assert 'undo_edit' not in changes[0]['workspace']
    assert changes[0]['health'] == result['health'] and changes[0]['fix_list_delta'] == result['fix_list_delta']
    s.LiveChange.model_validate(changes[0])

    _, mismatched = _workspace_edit_result(revision=6)
    mismatched['delta']['revision'] = 5
    bare = live.command_changes('shift.update', {'workspace_id': mismatched['workspace']['id']}, mismatched)[0]
    assert set(bare) == {'scope', 'id'}


def test_workspace_edit_delta_missing_or_partial_falls_back_to_bare_invalidation():
    workspace_id, missing = _workspace_edit_result()
    missing['delta'] = None
    assert set(live.command_changes('shift.update', {'workspace_id': workspace_id}, missing)[0]) == {'scope', 'id'}
    workspace_id, stale = _workspace_edit_result(stale=True)
    assert set(live.command_changes('shift.update', {'workspace_id': workspace_id}, stale)[0]) == {'scope', 'id'}
    workspace_id, partial = _workspace_edit_result(partial=True)
    assert set(live.command_changes('shift.update', {'workspace_id': workspace_id}, partial)[0]) == {'scope', 'id'}


def test_workspace_edit_delta_reaches_the_pulse_history(history):
    workspace_id, result = _workspace_edit_result()

    async def scenario():
        history.attach(asyncio.get_running_loop())
        change = live.command_changes('shift.update', {'workspace_id': workspace_id}, result)[0]
        history.receive(json.dumps({'department_id': 'department', **change}))
        await drain_callbacks()
        assert history.changes_since('department', 0) == [change]

    asyncio.run(scenario())


def test_oversized_workspace_edit_delta_keeps_a_bare_wakeup():
    workspace_id, result = _workspace_edit_result()
    result['delta']['shifts'] = [{'id': result['delta']['shifts'][0]['id'], 'payload': 'x' * 200} for _ in range(100)]
    calls = []
    db = SimpleNamespace(bind=SimpleNamespace(dialect=SimpleNamespace(name='postgresql')),
                         execute=lambda sql, params: calls.append((str(sql), params)))
    live.signal(db, 'department', scope='workspace', id=workspace_id, delta=result['delta'],
                workspace=result['workspace'], validation=result['validation'], health=result['health'],
                fix_list_delta=result['fix_list_delta'])
    assert calls
    payload = json.loads(calls[0][1]['payload'])
    assert set(payload) == {'department_id', 'scope', 'id'}


def test_eviction_reconnect_and_scope_wide_changes(history):
    history._bump('department', {'scope': 'workspace', 'id': 'rota'})
    seen = history.version('department')
    history._bump('department', {'scope': 'workspace', 'id': None})
    assert history.changes_since('department', 0) == [{'scope': 'workspace', 'id': None}]
    history.invalidate()
    assert history.changes_since('department', seen) is None
    for index in range(6):
        history._bump(str(index), {'scope': 'inbox', 'id': None})
    assert len(history._changes) == len(history._versions) == history._history_topics
    history._bump('department', {'scope': 'inbox', 'id': None})
    assert history.changes_since('department', seen) is None


def test_first_event_evicted_before_stream_read_is_not_silently_lost(history):
    seen = history.version('department')
    history._bump('department', {'scope': 'jobs', 'id': None})
    for index in range(history._history_topics):
        history._bump(str(index), {'scope': 'inbox', 'id': None})
    assert history.version('department') != seen
    assert history.changes_since('department', seen) is None


async def drain_callbacks():
    done = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(done.set_result, None)
    await done


def test_private_generation_changes_never_enter_department_history(history):
    async def scenario():
        history.attach(asyncio.get_running_loop())
        topic = history.generation_topic('department', 'workspace')
        history.changed(topic, scope='jobs')
        await drain_callbacks()
        assert history.version('department') == 0
        assert history.version(topic) > 0
        assert history.version(history.generation_topic('department')) > 0
        assert history.changes_since(('department', topic), (0, 0)) == [{'scope': 'jobs', 'id': None}]
        assert history.changes_since('department', 0) == []
    asyncio.run(scenario())


def test_legacy_and_invalid_notifications_fall_back(history):
    async def scenario():
        history.attach(asyncio.get_running_loop())
        for payload in ['department', json.dumps({'department_id': 'department'}),
                        json.dumps({'department_id': 'department', 'scope': 'future', 'id': None}),
                        json.dumps({'department_id': 'department', 'scope': 'jobs', 'id': {}})]:
            seen = history.version('department')
            history.receive(payload)
            await drain_callbacks()
            assert history.changes_since('department', seen) is None
    asyncio.run(scenario())


@pytest.mark.parametrize('scope', get_args(s.LiveScope))
def test_signal_carries_contract_scope_in_compact_notify(scope):
    calls = []
    db = SimpleNamespace(bind=SimpleNamespace(dialect=SimpleNamespace(name='postgresql')),
                         execute=lambda sql, params: calls.append((str(sql), params)))
    live.signal(db, 'department', scope=scope, id=None)
    assert calls[0][0] == 'SELECT pg_notify(:channel, :payload)'
    assert calls[0][1]['channel'] == live.CHANNEL
    value = json.loads(calls[0][1]['payload'])
    assert value == {'department_id': 'department', 'scope': scope, 'id': None}
    assert s.LiveChange.model_validate({k: v for k, v in value.items() if k != 'department_id'})
    before = len(calls)
    live.signal(db, 'department', scope=scope, id='x' * 8000)
    assert len(calls) == before


def test_signal_carries_validation_job_id_without_widening_the_pulse_shape():
    calls = []
    db = SimpleNamespace(bind=SimpleNamespace(dialect=SimpleNamespace(name='postgresql')),
                         execute=lambda sql, params: calls.append((str(sql), params)))
    live.signal(db, 'department', scope='workspace', id='rota', job_id='check')
    value = json.loads(calls[0][1]['payload'])
    assert value == {'department_id': 'department', 'scope': 'workspace', 'id': 'rota', 'job_id': 'check'}
    assert s.LiveChange.model_validate({k: v for k, v in value.items() if k != 'department_id'}).job_id == 'check'


def test_signal_drops_unknown_scope_instead_of_emitting_an_unscoped_change():
    calls = []
    db = SimpleNamespace(bind=SimpleNamespace(dialect=SimpleNamespace(name='postgresql')),
                         execute=lambda sql, params: calls.append((str(sql), params)))
    live.signal(db, 'department', scope='future')
    live.signal(db, 'department')
    assert calls == []


@pytest.mark.parametrize('kind,scope', [
    ('workspace.create', 'workspace'), ('shift.action', 'workspace'), ('draft.move', 'workspace'),
    ('workspace.publish', 'publication'), ('policy.activate', 'rules'), ('rule.catalogue.check', 'rules'),
    ('membership.create', 'people'), ('catalogue.update', 'catalogue'), ('availability.record', 'availability'),
    ('request.decide', 'requests'), ('swap.confirm', 'swaps'), ('notification.ack_all', 'inbox'),
    ('job.cancel', 'jobs'), ('score_import.check', 'jobs'), ('ai_provider.save', 'settings'),
    ('role.revoke', 'access'), ('review_route.activate', 'access'), ('feedback.update', 'feedback'),
])
def test_command_writer_families_emit_reader_scope(kind, scope):
    changes = live.command_changes(kind)
    assert {'scope': scope, 'id': None} in changes
    for change in changes:
        s.LiveChange.model_validate(change)


def test_absence_request_live_payload_is_compact_and_private_text_free():
    changes = live.command_changes('request.create', {
        'kind': 'absence_report', 'reason': 'Private health explanation',
        'category': 'sick', 'note': 'Private note'}, result={
            'record': {'details': {'absence_reason': 'sick', 'note': 'Private note'}}})
    assert changes == [{'scope': 'requests', 'id': None}, {'scope': 'availability', 'id': None}]
    assert 'Private health explanation' not in json.dumps(changes)
    assert 'Private note' not in json.dumps(changes)


def test_catalogue_and_mixed_bulk_scopes_and_unknown_writer():
    assert live.command_changes('catalogue.update', {'catalogue': 'people'}) == [
        {'scope': 'catalogue', 'id': 'people'}, {'scope': 'people', 'id': None}]
    changed = live.command_changes('bulk.apply', result={'item_kinds': ['shift.action', 'role.revoke', 'shift.copy']})
    assert changed == [{'scope': 'workspace', 'id': None}, {'scope': 'access', 'id': None}]
    assert live.command_changes('bulk.apply', result={'item_kinds': ['new.kind']}) is None
    assert live.command_changes('new.kind') is None
    assert live.command_changes('rule.catalogue.check', result={'job': {'id': 'private'}}) == [
        {'scope': 'rules', 'id': None}, {'scope': 'jobs', 'id': 'private'}]
    assert live.command_changes('workspace.merge.apply', result={'job': {'id': 'private'}}) == [
        {'scope': 'workspace', 'id': None}, {'scope': 'jobs', 'id': 'private'}]


def test_all_signal_calls_are_explicit_and_dispatch_families_are_mapped():
    for path in Path('apps').rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'signal':
                if isinstance(node.func.value, ast.Name) and node.func.value.id == 'live':
                    assert any(key.arg in {'scope', None} for key in node.keywords), (path, node.lineno)
    # A new literal dispatch branch must declare its reader scope, not quietly weaken invalidation.
    tree = ast.parse(Path('apps/api/commands.py').read_text(encoding='utf-8'))
    dispatch = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'dispatch')
    kinds = {node.value for node in ast.walk(dispatch) if isinstance(node, ast.Constant)
             and isinstance(node.value, str) and '.' in node.value and ' ' not in node.value
             and node.value[0].islower() and not node.value.startswith('.')}
    # Deliberately silent: bulk.apply announces its items' kinds; a read receipt changes no page anyone has open.
    assert not {kind for kind in kinds if live.command_changes(kind) is None} - {'bulk.apply', 'department_content.seen'}


def test_history_limits_are_configurable(monkeypatch):
    monkeypatch.setenv('ROTA_LIVE_HISTORY_SIZE', '5')
    monkeypatch.setenv('ROTA_LIVE_HISTORY_TOPICS', '12')
    settings = Settings.from_env()
    assert (settings.live_history_size, settings.live_history_topics) == (5, 12)
    settings = replace(settings, live_history_size=0)
    with pytest.raises(RuntimeError, match='ROTA_LIVE_HISTORY_SIZE'):
        settings.check(require_authentication=False)


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_command_emission_and_pulse_compatibility(h, monkeypatch):
    calls = []
    original = live.signal
    def signal(db, department, **change):
        calls.append((department, change))
        original(db, department, **change)
    monkeypatch.setattr(live, 'signal', signal)
    workspace = h.workspace('Scoped change')
    h.shift(workspace)
    assert calls and all(value[0] == h.seed['department_id']
                         and value[1]['scope'] == 'workspace' and value[1]['id'] == workspace['id'] for value in calls)
    assert calls[-1][1]['delta']['revision'] == calls[-1][1]['workspace']['revision']
    assert calls[-1][1]['workspace']['content_revision'] == workspace['content_revision'] + 1
    calls.clear()
    h.configure('categories', {'name': 'Scoped category', 'starts_on': '2020-01-01'})
    assert calls == [(h.seed['department_id'], {'scope': 'catalogue', 'id': 'categories'})]
    pulse = h.get('coordinator', '/api/pulse')
    assert set(pulse) == {'input_revision', 'availability_revision', 'activity', 'roles', 'unread',
                          'as_of', 'build', 'department_id', 'changes'}
    assert pulse['changes'] is None
    s.LivePulse.model_validate(pulse)
    stream = h.client('coordinator').get('/api/events?once=true')
    value = json.loads(next(line[6:] for line in stream.text.splitlines() if line.startswith('data: ')))
    assert value['changes'] is None and value['activity'] == pulse['activity']
    assert 'generation_progress' not in h.get('staff', '/api/pulse')


def _listen_process(conninfo, department, pipe):
    """Separate address space, real LISTEN and the same receive/history path as the API."""
    import psycopg
    async def run():
        live.attach(asyncio.get_running_loop())
        with psycopg.connect(conninfo, autocommit=True) as conn:
            conn.execute(f'LISTEN {live.CHANNEL}')
            pipe.send('ready')
            notes = conn.notifies(timeout=30)
            for note in notes:
                if json.loads(note.payload).get('department_id') != department:
                    continue
                live.receive(note.payload)
                await drain_callbacks()
                pipe.send(live.changes_since(department, 0))
                break
            notes.close()
        live.detach(asyncio.get_running_loop())
    asyncio.run(run())
    pipe.close()


@pytest.mark.postgres
def test_commit_notify_reaches_three_independent_processes_and_rollback_is_silent(pg):
    context = multiprocessing.get_context('spawn')
    conninfo = make_url(pg.settings.database_url).set(drivername='postgresql').render_as_string(hide_password=False)
    children = []
    try:
        for _ in range(3):
            parent, child = context.Pipe()
            process = context.Process(target=_listen_process, args=(conninfo, pg.seed['department_id'], child))
            process.start()
            child.close()
            children.append((process, parent))
        for _, pipe in children:
            assert pipe.poll(45) and pipe.recv() == 'ready'
        with transaction(pg.app.state.engine) as db:
            live.signal(db, pg.seed['department_id'], scope='rules')
            db.rollback()
        with transaction(pg.app.state.engine, write=True) as db:
            live.signal(db, pg.seed['department_id'], scope='workspace')
        for process, pipe in children:
            assert pipe.poll(45)
            assert pipe.recv() == [{'scope': 'workspace', 'id': None}]
            process.join(10)
            assert process.exitcode == 0
    finally:
        for process, pipe in children:
            if process.is_alive():
                process.terminate()
                process.join(10)
            pipe.close()


def test_sse_delivers_repeated_scopes_and_safe_fallbacks_without_counter_changes(h, history, monkeypatch):
    from starlette.requests import Request
    from apps.api import models as m
    cookie = h.client('coordinator').cookies['rota_session']
    endpoint = next(route.endpoint for route in h.app.routes if route.path == '/api/events')
    department = h.seed['department_id']
    actions = iter(['scoped', 'scoped', 'unscoped', 'overflow', 'poll'])

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def ready(topics, seen, timeout):
        action = next(actions)
        if action == 'poll':
            with transaction(h.app.state.engine, write=True) as db:
                org = db.get(m.Organisation, db.get(m.Department, department).organisation_id)
                org.input_revision += 1
        else:
            for _ in range(history._history_size + 1 if action == 'overflow' else 1):
                history._bump(department, None if action == 'unscoped' else {'scope': 'jobs', 'id': None})

    monkeypatch.setattr(history, 'wait', ready)

    async def scenario():
        request = Request({'type': 'http', 'path': '/api/events',
                           'headers': [(b'cookie', f'rota_session={cookie}'.encode())]}, receive)
        response = await endpoint(request)
        iterator = response.body_iterator
        try:
            assert await anext(iterator) == ': connected\n\n'
            values = []
            for _ in range(6):
                chunk = await anext(iterator)
                assert chunk.startswith('event: pulse\n')
                values.append(json.loads(chunk.split('data: ', 1)[1]))
            assert [value['changes'] for value in values] == [None,
                [{'scope': 'jobs', 'id': None}], [{'scope': 'jobs', 'id': None}], None, None, None]
            assert len({value['activity'] for value in values}) == 1
            assert values[-1]['input_revision'] > values[0]['input_revision']
        finally:
            await iterator.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize(('status', 'event', 'data'), [
    (503, 'error', {'retry': True}),
    (401, 'expired', {}),
])
def test_sse_classifies_pulse_failures_without_false_expiry(h, monkeypatch, status, event, data):
    from starlette.requests import Request
    from apps.api import main
    from apps.api.errors import Problem

    cookie = h.client('coordinator').cookies['rota_session']
    endpoint = next(route.endpoint for route in h.app.routes if route.path == '/api/events')
    activity = main.live_updates.activity
    calls = 0

    def fail_after_initial(db, department):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise Problem(status, 'SESSION_EXPIRED' if status == 401 else 'PULSE_FAILED', 'synthetic pulse failure')
        return activity(db, department)

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def ready(*_):
        return None

    monkeypatch.setattr(main.live_updates, 'activity', fail_after_initial)
    monkeypatch.setattr(main.live_updates, 'wait', ready)

    async def scenario():
        request = Request({'type': 'http', 'path': '/api/events',
                           'headers': [(b'cookie', f'rota_session={cookie}'.encode())]}, receive)
        response = await endpoint(request)
        iterator = response.body_iterator
        try:
            initial = await anext(iterator)
            assert initial == ': connected\n\n'
            initial = await anext(iterator)
            assert initial.startswith('event: pulse\n')
            failure = await anext(iterator)
            assert failure == f'event: {event}\ndata: {json.dumps(data)}\n\n'
        finally:
            await iterator.aclose()

    asyncio.run(scenario())


def test_score_worker_emits_jobs_scope(monkeypatch):
    from apps.worker import score_check
    calls = []
    monkeypatch.setattr(live, 'signal', lambda db, topic, **change: calls.append((topic, change)))
    job = SimpleNamespace(kind='test', department_id='department', id='job-test', revision=1)
    score_check._store(None, job, None, None)
    assert calls == [(live.job_topic('department', 'job-test'), {'scope': 'jobs', 'id': 'job-test'})]


def test_worker_completion_emits_jobs_and_validation_workspace(h, monkeypatch):
    from apps.worker import main as worker
    from apps.api.db import retry_write
    from tests.test_autoschedule_live_worker import generation_job
    from apps.api import models as m
    from apps.api.snapshots import fingerprint
    workspace, job = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.kind = 'validation'
        expected = fingerprint(db, db.get(m.Workspace, workspace['id']))
        row.input_fingerprint = expected
    calls = []
    monkeypatch.setattr(live, 'signal', lambda db, topic, **change: calls.append((topic, change)))
    assert retry_write(engine, lambda db: worker.finish_job(db, claim, {'fingerprint': expected}, None))
    assert calls == [(live.job_topic(h.seed['department_id'], job['id']),
                      {'scope': 'jobs', 'id': job['id']}),
                     (h.seed['department_id'], {'scope': 'workspace', 'id': workspace['id'], 'job_id': job['id']})]


def test_dispatch_reconciles_event_windows_as_availability_and_catalogue(h, monkeypatch):
    from apps.worker import main as worker
    from apps.api.services import announcements, events
    from apps.api.db import retry_write
    department = h.seed['department_id']
    calls = []
    monkeypatch.setattr(announcements, 'reconcile', lambda db: set())
    monkeypatch.setattr(events, 'reconcile', lambda db: {department})
    monkeypatch.setattr(live, 'signal', lambda db, topic, **change: calls.append((topic, change)))
    assert retry_write(h.app.state.engine, worker.dispatch_batch) == {department}
    assert calls == [(department, {'scope': 'inbox', 'id': department}),
                     (department, {'scope': 'catalogue', 'id': 'events'}),
                     (department, {'scope': 'availability'})]


def test_place_ids_are_carried_for_workspace_job_and_person_changes():
    assert live.command_changes('shift.action', {'workspace_id': 'rota-a'}, {'workspace': {'id': 'rota-a'}}) == [
        {'scope': 'workspace', 'id': 'rota-a'}]
    assert live.command_changes('job.cancel', {}, {'job': {'id': 'job-a'}}, target_id='job-a') == [
        {'scope': 'jobs', 'id': 'job-a'}]
    assert live.command_changes('availability.record', {'person_id': 'person-a'}, {}) == [
        {'scope': 'availability', 'id': 'person-a'}, {'scope': 'requests', 'id': None}]


def test_job_events_use_private_topics_and_generation_fans_out_only_to_coordinators(history):
    async def scenario():
        history.attach(asyncio.get_running_loop())
        job = history.job_topic('department', 'job-a')
        history.changed(job, scope='jobs', id='job-a')
        generation = history.generation_topic('department', 'rota-a')
        history.changed(generation, scope='jobs', id='job-a')
        await drain_callbacks()
        assert history.version('department') == 0
        assert history.changes_since(job, 0) == [{'scope': 'jobs', 'id': 'job-a'}]
        assert history.version(history.generation_topic('department')) > 0
        assert history.changes_since('department', 0) == []
    asyncio.run(scenario())


def test_unknown_command_does_not_emit_an_unscoped_signal():
    assert live.command_changes('future.command') is None


def test_a_content_read_receipt_announces_no_change():
    """One person marking announcements seen changes no page anyone has open: no live change, so no open Home re-reads
    its account and lists and draws again (it restarted the page's own reads)."""
    assert live.command_changes('department_content.seen', {'ids': ['x']}, {'receipts': {}}) is None
    assert live.command_changes('department_content.create', {}, {}) is not None  # content itself is still announced

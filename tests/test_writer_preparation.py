"""PERF-7: expensive checks run before the global writer; the writer re-verifies, then writes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import re
from threading import Event, local
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, update

from apps.api import db as database, models as m
from apps.api.db import transaction
from apps.api.services import jobs, planning
from packages.domain.validation import INTERNAL_CHECK_KEYS
from tests.test_published_changes import change, issued


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def harness(request):
    return request.getfixturevalue(request.param)


@pytest.fixture
def writer_evaluations(monkeypatch):
    """True for each complete rota evaluation run while this thread holds the global write lock."""
    held, calls = local(), []
    real_transaction, evaluate = database.transaction, planning.evaluate

    @contextmanager
    def tracked(engine, write=False, **options):
        with real_transaction(engine, write, **options) as session:
            previous = getattr(held, 'writer', False)
            held.writer = write and options.get('serialize', True)
            try:
                yield session
            finally:
                held.writer = previous

    def counted(data, **options):
        calls.append(getattr(held, 'writer', False))
        return evaluate(data, **options)

    monkeypatch.setattr(database, 'transaction', tracked)
    monkeypatch.setattr(planning, 'evaluate', counted)
    return calls


def public(check):
    return {key: value for key, value in check.items() if key not in INTERNAL_CHECK_KEYS | {'evaluated_at'}}


def test_publication_reuses_approved_check_without_evaluating_under_the_writer(harness, writer_evaluations):
    """Submit and approve leave the writer free of checks. Publish runs its complete check there."""
    h = harness
    workspace = h.shift(h.workspace())['workspace']
    writer_evaluations.clear()
    workspace = h.lifecycle(workspace, 'submit')['workspace']
    workspace = h.lifecycle(workspace, 'approve', 'approver')['workspace']
    assert not any(writer_evaluations), 'Submit and approve run no complete check under the writer'
    writer_evaluations.clear()

    published = h.lifecycle(workspace, 'publish')

    assert published['publication_id']
    assert published['workspace']['status'] == 'published'
    assert writer_evaluations == [True], 'Publish evaluates once under the writer and never during admission'


def test_batch_check_prepares_validation_and_overlap_flags_together(harness, writer_evaluations, monkeypatch):
    from apps.api.services import planning_preparation
    h = harness
    workspace = h.shift(h.workspace())['workspace']
    shift = h.detail(workspace)['shifts'][0]
    passes, bind = [], planning_preparation.bind

    def bound(db, plan, **options):
        passes.append(options.get('replay', False))
        return bind(db, plan, **options)

    monkeypatch.setattr(planning_preparation, 'bind', bound)
    writer_evaluations.clear()
    result = h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'],
        'shift_ids': [shift['id']], 'action': 'lock', 'reason': 'Keep this reviewed assignment'},
        revision=workspace['revision'])
    assert passes == [False, True], 'One preparation acquires the check and both overlap projections, followed by the writer'
    assert writer_evaluations == [False], 'The complete batch is checked once, outside the writer'
    assert result['shifts'][0]['locked']


def test_batched_snapshot_records_keep_canonical_serialization(harness, monkeypatch):
    from apps.api import snapshots
    from apps.api.serialization import record
    h = harness
    original, acquisitions = snapshots.records_many, []

    def compared(db, sources, **options):
        acquired = original(db, sources, **options)
        for name, (cls, where) in sources.items():
            expected = [record(row, exclude=options.get('exclude', {}).get(name, ()))
                        for row in db.scalars(select(cls).where(where))]
            assert {row['id']: row for row in acquired[name]} == {row['id']: row for row in expected}, name
        acquisitions.append(1)
        return acquired

    monkeypatch.setattr(snapshots, 'records_many', compared)
    with transaction(h.app.state.engine) as db:
        result = snapshots.snapshot_inputs(db, db.get(m.Organisation, h.seed['organisation_id']))
    assert acquisitions == [1]
    assert all('pager' not in person for person in result['people'].values())


def test_batched_window_rows_keep_canonical_inputs(harness, monkeypatch):
    from apps.api import snapshots
    from apps.api.serialization import record
    from tests.test_events import event, event_type
    from tests.test_availability_patterns import pattern
    h = harness
    workspace = h.shift(h.workspace())['workspace']
    person = h.seed['people'][0]
    h.command('coordinator', 'availability_pattern.propose', pattern(person))
    event(h, event_type(h, blocks_duties=True, attendance='expected', rsvp=False))
    original, acquired_names = snapshots.rows_many, []

    def compared(db, sources):
        acquired = original(db, sources)
        for name, (cls, where) in sources.items():
            expected = {row.id: record(row) for row in db.scalars(select(cls).where(where))}
            assert {row.id: record(row) for row in acquired[name]} == expected, name
            acquired_names.append(name)
        return acquired

    with monkeypatch.context() as patch:
        patch.setattr(snapshots, 'rows_many', compared)
        batched = h.snapshot(workspace)
    canonical = h.snapshot(workspace)
    assert batched == canonical
    assert set(acquired_names) == {'availability', 'patterns', 'events', 'event_occurrences'}
    assert any(row.get('pattern_id') for row in batched['availability'])
    assert any(row.get('event_occurrence_id') for row in batched['scheduling_facts'])


def test_batched_scheduling_facts_keep_timestamp_tie_insertion_order(harness):
    from datetime import timedelta
    from apps.api import snapshots
    from apps.api.serialization import record
    from packages.domain.time import now
    h = harness
    stamp = now()
    ids = ['ffffffff-ffff-ffff-ffff-ffffffffffff', '00000000-0000-0000-0000-000000000001',
           '00000000-0000-0000-0000-000000000002']
    with transaction(h.app.state.engine, write=True) as db:
        for ident, created_at in zip(ids, [stamp, stamp, stamp - timedelta(days=1)]):
            db.add(m.SchedulingFact(id=ident, created_at=created_at, department_id=h.seed['department_id'],
                kind='workload', person_id=h.seed['people'][0], status='active',
                proposed_by=h.seed['identities']['coordinator'], approved_by=h.seed['identities']['approver'],
                reason='Synthetic approved ordering fixture', data={}))
            db.flush()
    with transaction(h.app.state.engine) as db:
        expected = [record(row) for row in db.scalars(select(m.SchedulingFact).where(
            m.SchedulingFact.department_id == h.seed['department_id']).order_by(
                m.SchedulingFact.created_at, m.SchedulingFact.seq))]
        assert [row['id'] for row in expected] == [ids[2], ids[0], ids[1]]
        assert snapshots.scheduling_inputs(db, [h.seed['department_id']])['scheduling_facts'] == expected
        shared = snapshots.snapshot_inputs(db, db.get(m.Organisation, h.seed['organisation_id']))
        assert shared['scheduling_facts'] == expected
        assert all('seq' not in row for row in shared['scheduling_facts'])


def test_writer_reacquires_inputs_after_preparation(harness, monkeypatch):
    h = harness
    workspace = h.shift(h.workspace())['workspace']
    shift = h.detail(workspace)['shifts'][0]
    evaluate, changed = planning.evaluate, []

    def checked(data, **options):
        result = evaluate(data, **options)
        if not changed:
            # The computation runs after preparation rolls back, so another
            # committed edit can invalidate it before the writer acquires inputs.
            with transaction(h.app.state.engine, write=True) as db:
                db.get(m.Workspace, workspace['id']).content_revision += 1
            changed.append(1)
        return result

    monkeypatch.setattr(planning, 'evaluate', checked)
    error = h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'],
        'shift_ids': [shift['id']], 'action': 'lock', 'reason': 'Keep the reviewed assignment'},
        revision=workspace['revision'], status=409)['error']
    assert changed == [1] and error['code'] == 'STALE_INPUTS'
    assert not h.detail(workspace)['shifts'][0]['locked']


def test_published_change_checks_run_before_the_writer_with_identical_results(harness, writer_evaluations):
    h = harness
    pub, shifts = issued(h)
    writer_evaluations.clear()
    first = change(h, pub, shifts[0])
    second = change(h, pub, shifts[1])
    undone = h.command('coordinator', 'publication.change.undo', {'change_id': second['change']['id'],
                       'reason': 'Keep the second duty as issued'}, target=pub, revision=second['workspace']['revision'])
    assert first['validation']['scope'] == 'published_changes'
    calls = len(writer_evaluations)
    sent = h.command('coordinator', 'publication.changes.submit', {'reason': 'Review this published cover'},
                     target=pub, revision=undone['workspace']['revision'])
    assert len(writer_evaluations) > calls, 'Submitting checks the candidate'
    assert writer_evaluations and not any(writer_evaluations), 'No complete check runs under the global write lock'
    assert len(sent['change_diff']['sessions']) == 1
    assert next(c for c in sent['changes'] if c['status'] == 'submitted')['urgency'] == 'routine'
    # The prepared check the writer stored equals a fresh complete check of the committed candidate.
    with transaction(h.app.state.engine) as db:
        candidate = db.get(m.Workspace, sent['workspace']['id'])
        assert candidate.status == 'submitted'
        stored = public(candidate.validation)
        fresh = public(planning.revalidate(db, candidate, store=False, include_internal=True))
    for key in ('valid', 'error_count', 'warning_count', 'findings', 'coverage', 'scope', 'fingerprint'):
        assert stored[key] == fresh[key], key
    assert candidate.submitted_fingerprint == fresh['fingerprint']


def save_unrelated(h, kind, name):
    if kind == 'saved_view':
        result = h.command('staff', 'table_view.create', {
            'table_key': 'people', 'name': name, 'definition': {}})
        assert result['view']['name'] == name
    else:
        from apps.api.services.common import bump_inputs
        with transaction(h.app.state.engine, write=True) as db:
            department = m.Department(organisation_id=h.seed['organisation_id'], name=name, weekend_days=[1])
            db.add(department)
            db.flush()
            bump_inputs(db, SimpleNamespace(organisation_id=h.seed['organisation_id'], department_id=department.id))
    return True


@pytest.mark.parametrize('unrelated', ['saved_view', 'other_department_inputs'])
def test_publish_preparation_does_not_block_an_unrelated_write(harness, monkeypatch, unrelated):
    """An unrelated write waits for the final publication check, then both writes succeed."""
    h = harness
    w = h.shift(h.workspace())['workspace']
    w = h.lifecycle(w, 'submit')['workspace']
    w = h.lifecycle(w, 'approve', 'approver')['workspace']
    h.client('staff')
    entered, waiting, release, checked = Event(), Event(), Event(), Event()
    revalidate = planning.revalidate
    engine = h.app.state.engine
    lock_statement = 'BEGIN IMMEDIATE' if engine.dialect.name == 'sqlite' else 'SELECT pg_advisory_xact_lock('

    def paused(db, workspace, *args, **kwargs):
        writer = db.connection().info.get('rota_write')
        assert writer is not None and not writer['serializing'], 'Pause after taking the publication writer'
        entered.set()
        assert release.wait(30), 'Test did not release the final publication check'
        result = revalidate(db, workspace, *args, **kwargs)
        checked.set()
        return result

    def attempting_lock(_connection, _cursor, statement, *_):
        if entered.is_set() and statement.startswith(lock_statement):
            waiting.set()

    def acquired_lock(_connection, _cursor, statement, *_):
        if entered.is_set() and statement.startswith(lock_statement):
            assert checked.is_set(), 'The unrelated write acquired the writer during the publication check'

    monkeypatch.setattr(planning, 'revalidate', paused)
    event.listen(engine, 'before_cursor_execute', attempting_lock)
    event.listen(engine, 'after_cursor_execute', acquired_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            published = pool.submit(h.lifecycle, w, 'publish')
            try:
                assert entered.wait(10), 'Publish did not enter its final check'
                saved = pool.submit(save_unrelated, h, unrelated, 'During publication')
                assert waiting.wait(10), 'The unrelated write did not reach the writer lock'
                assert not saved.done() and not published.done()
            finally:
                release.set()
            publication = published.result(timeout=30)
            assert publication['publication_id']
            assert publication['workspace']['status'] == 'published'
            assert saved.result(timeout=30)
    finally:
        event.remove(engine, 'before_cursor_execute', attempting_lock)
        event.remove(engine, 'after_cursor_execute', acquired_lock)


@pytest.mark.parametrize('unrelated', ['saved_view', 'other_department_inputs'])
def test_change_submit_preparation_does_not_block_an_unrelated_write(harness, monkeypatch, unrelated):
    h = harness
    pub, shifts = issued(h)
    pending = change(h, pub, shifts[0])
    h.client('staff')
    entered, release = Event(), Event()
    evaluate = planning.evaluate

    def paused(data, **options):
        if not entered.is_set():
            entered.set()
            assert release.wait(30), 'Test did not release the change check'
        return evaluate(data, **options)

    monkeypatch.setattr(planning, 'evaluate', paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = pool.submit(h.command, 'coordinator', 'publication.changes.submit', {'reason': 'Review this cover'},
                           pub, pending['workspace']['revision'])
        try:
            assert entered.wait(10)
            saved = pool.submit(save_unrelated, h, unrelated, 'During change check')
            assert saved.result(timeout=10)
            assert not sent.done()
        finally:
            release.set()
        assert sent.result(timeout=30)['workspace']['status'] == 'submitted'


def job_columns_read(engine, statements, *, result_reads=None):
    """Collect whole-column reads and optionally track queries that use the result in JSON paths."""
    def listen(_connection, cursor, statement, *_):
        if 'FROM jobs' not in statement:
            return
        names = {column[0] for column in cursor.description or ()}
        if names & {'input_snapshot', 'result'}:
            statements.append(statement)
        if result_reads is not None and re.search(r'\bjobs\.result\b', statement):
            result_reads.append(statement)
    event.listen(engine, 'after_cursor_execute', listen)
    return lambda: event.remove(engine, 'after_cursor_execute', listen)


def test_queued_validation_coalesces_without_loading_job_inputs(harness):
    h = harness
    w = h.shift(h.workspace())['workspace']
    engine = h.app.state.engine
    actor = SimpleNamespace(department_id=h.seed['department_id'], identity_id=h.seed['identities']['coordinator'])
    with transaction(engine, write=True) as db:
        workspace = db.get(m.Workspace, w['id'])
        current = {'fingerprint': 'f' * 64, 'validation_basis': 'current',
                   'workspace': {'content_revision': workspace.content_revision}}
        queued = jobs.queue_validation(db, actor, workspace, current).id
    statements = []
    stop = job_columns_read(engine, statements)
    try:
        with transaction(engine, write=True) as db:
            workspace = db.get(m.Workspace, w['id'])
            # Same content revision with a current basis: the queued check is reused.
            assert jobs.queue_validation(db, actor, workspace, {**current, 'fingerprint': 'e' * 64}).id == queued
            workspace.content_revision += 1
            replaced = jobs.queue_validation(db, actor, workspace, {**current, 'fingerprint': 'd' * 64}).id
    finally:
        stop()
    assert statements == [], 'Pending checks are compared without parsing their inputs or results'
    assert replaced != queued
    with transaction(engine) as db:
        old = db.get(m.Job, queued)
        assert (old.status, old.error_code) == ('failed', 'STALE_INPUTS')
        assert db.get(m.Job, replaced).status == 'queued'


def test_undoing_an_applied_proposal_loads_only_its_own_job_result(harness):
    from tests.test_generation_apply_scale import completed_proposal
    h = harness
    workspace = h.shift(h.workspace(), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    other = completed_proposal(h, workspace, [{'shift_id': shift['id'], 'participant': {
        'person_id': h.seed['people'][1], 'start': shift['start'], 'end': shift['end']}}])
    with transaction(h.app.state.engine, write=True) as db:
        # An earlier applied run for another edit keeps its status when this edit is undone.
        row = db.get(m.Job, other['id'])
        row.status, row.result = 'applied', {**row.result, 'applied_content_revision': 999}
    job = completed_proposal(h, workspace, [{'shift_id': shift['id'], 'participant': {
        'person_id': h.seed['people'][2], 'start': shift['start'], 'end': shift['end']}}])
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'], 'job_id': job['id'],
                        'reason': 'Apply reviewed synthetic proposal'}, revision=workspace['revision'])
    statements, result_reads = [], []
    stop = job_columns_read(h.app.state.engine, statements, result_reads=result_reads)
    try:
        h.command('coordinator', 'workspace.undo', {'reason': 'Undo the applied proposal'},
                  workspace['id'], applied['workspace']['revision'])
    finally:
        stop()
    assert not any('input_snapshot' in statement.split('FROM jobs')[0] for statement in statements)
    assert not statements, 'Undo must read the receipt and revision without loading a full job result'
    # Any result access must use the applied revision filter for the edit being undone.
    assert result_reads and all(
        re.search(r'\bcoalesce\([^=]*\bjobs\.result\b[^=]*\)\s*=',
                  statement.partition('FROM jobs')[2].partition('WHERE')[2])
        for statement in result_reads
    )
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Job.status).where(m.Job.id == other['id'])) == 'applied'


def test_legacy_job_metadata_recovers_generation_manifest_on_result_change(harness):
    from tests.test_generation_apply_scale import completed_proposal
    h = harness
    workspace = h.shift(h.workspace(), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    job = completed_proposal(h, workspace, [{'shift_id': shift['id'], 'participant': {
        'person_id': h.seed['people'][1], 'start': shift['start'], 'end': shift['end']}}])
    manifest = {'baseline': {'hash': 'legacy-manifest'}}
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.input_snapshot = {**row.input_snapshot, 'generation_manifest': manifest}
        db.flush()
        legacy = {**row.read_metadata, 'result': {
            key: value for key, value in (row.read_metadata.get('result') or {}).items()
            if key != 'generation_manifest'}}
        db.execute(update(m.Job).where(m.Job.id == job['id']).values(read_metadata=legacy))
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.status = 'applied'
        row.result = {**row.result, 'applied_content_revision': 999}
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).read_metadata['result']['generation_manifest'] == manifest


def redo_after_undone_batch(h, sessions, person):
    """A proposal that placed `person` on `sessions` new sessions, applied, then undone."""
    from tests.test_generation_apply_scale import completed_proposal
    workspace = h.workspace()
    for day in range(sessions):
        workspace = h.shift(workspace, f'2026-10-0{4 + day}T08:00:00+03:00', f'2026-10-0{4 + day}T12:00:00+03:00',
                            people=[])['workspace']
    additions = [{'shift_id': shift['id'], 'participant': {'person_id': person, 'start': shift['start'], 'end': shift['end']}}
                 for shift in h.detail(workspace)['shifts']]
    job = completed_proposal(h, workspace, additions)
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'], 'job_id': job['id'],
                        'reason': 'Apply reviewed synthetic proposal'}, revision=workspace['revision'])['workspace']
    return h.command('coordinator', 'workspace.undo', {'reason': 'Undo'}, applied['id'], applied['revision'])['workspace']


def test_redo_placement_check_is_bounded_however_many_sessions_it_puts_back(harness, monkeypatch):
    from apps.api.services import planning_preparation
    h = harness
    # A zero-limit rule for someone else: every re-placed session needs its zero-limit trial.
    h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'week'}, scope={'person_id': h.seed['people'][2]})
    work = {}
    for sessions in (2, 6):
        undone = redo_after_undone_batch(h, sessions, h.seed['people'][sessions // 6])
        passes, single = [], []
        bind = planning_preparation.bind
        monkeypatch.setattr(planning_preparation, 'bind', lambda *a, **k: passes.append(1) or bind(*a, **k))

        def listen(_connection, _cursor, statement, *_):
            text = ' '.join(statement.split())
            if 'FROM shift_services WHERE shift_services.shift_id =' in text or 'WHERE shifts.id =' in text:
                single.append(text)
        event.listen(h.app.state.engine, 'before_cursor_execute', listen)
        try:
            redone = h.command('coordinator', 'workspace.redo', {'reason': 'Redo'}, undone['id'], undone['revision'])
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', listen)
            monkeypatch.setattr(planning_preparation, 'bind', bind)
        assert len(h.detail(redone['workspace'])['shifts']) == sessions
        assert all(s['participants'] for s in h.detail(redone['workspace'])['shifts'])
        work[sessions] = (len(passes), single)
    assert work[2][0] == work[6][0], 'Preparation passes do not grow with the sessions Redo puts back'
    assert work[6][1] == [], 'No per-session clinic or session query'


def test_redo_still_refuses_a_placement_a_zero_limit_now_blocks(harness):
    h = harness
    person = h.seed['people'][1]
    undone = redo_after_undone_batch(h, 3, person)
    h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'week'}, scope={'person_id': person})
    # shortcut: maximum 0 becomes a prohibition (live) in the follow-up feat/prohibition-classification; remove this refresh then
    undone = h.refresh_draft_baseline(undone)
    refused = h.command('coordinator', 'workspace.redo', {'reason': 'Redo'}, undone['id'], undone['revision'], status=409)
    assert refused['error']['code'] == 'REDO_BLOCKED' and refused['error']['details']['person_ids'] == [person]
    assert not any(s['participants'] for s in h.detail(undone)['shifts'])


def test_concurrent_catalogue_reads_after_a_write_build_each_list_once(harness, monkeypatch):
    """SW3-D-05: after a save every open page asks for changed catalogues at once; one request builds each list."""
    import threading
    from sqlalchemy import update
    from apps.api import main as api
    from apps.api.services import policies
    h = harness
    h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'}, severity='advisory')
    known = ','.join(h.get('coordinator', '/api/catalogues?known=')['_versions'].values())
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(update(m.Department).values(catalogue_revision=m.Department.catalogue_revision + 1))
    entered, release, waiting, builds = Event(), Event(), Event(), []
    real = policies.policy_versions

    def counted(*args, **kwargs):
        builds.append(1)
        entered.set()
        assert release.wait(30), 'Test did not release the list build'
        return real(*args, **kwargs)

    class Watched(threading.Event):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    monkeypatch.setattr(policies, 'policy_versions', counted)
    monkeypatch.setattr(api, 'Event', Watched)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(h.get, 'coordinator', '/api/catalogues?known=' + known)
        try:
            assert entered.wait(10)
            second = pool.submit(h.get, 'coordinator', '/api/catalogues?known=' + known)
            assert waiting.wait(10), 'The second reader waits for the build in progress'
        finally:
            release.set()
        replies = first.result(timeout=30), second.result(timeout=30)
    assert len(builds) == 1
    assert replies[0]['_versions'] == replies[1]['_versions']


def test_a_finished_rota_check_matches_the_rota_it_checked(h):
    """STAB-B queues a check with an intent token; once it ran, the job carries the fingerprint it checked, so the
    rota's current fingerprint calls it current (the auto-schedule panel's Open fix list, history, Home)."""
    from apps.worker.main import process_one
    w = h.shift(h.workspace('Check is current'), people=[])['workspace']
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'}, revision=w['revision'])
    process_one(h.app.state.engine)
    job = h.get('coordinator', f"/api/jobs?workspace_id={w['id']}")['jobs'][0]
    current = h.get('coordinator', f"/api/workspaces/{w['id']}?view=compact")['current_fingerprint']
    assert job['status'] == 'succeeded' and job['input_fingerprint'] == current

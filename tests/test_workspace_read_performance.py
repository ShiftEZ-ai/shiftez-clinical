"""Query/work budgets are deterministic on native, traced and shared CI runners."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier, Event
import weakref

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.orm import defer

from apps.api import main, models as m, snapshots
from apps.api.db import transaction
from apps.api.services import rota_summary as service
from tests.test_backend_scale import measure, scale_department
from tests.test_fix_list_performance import backend
from tests.clock_support import freeze_clock


def test_department_workspace_reads_fetch_only_baseline_metadata(backend, monkeypatch):
    freeze_clock(monkeypatch, datetime(2026, 10, 15, tzinfo=timezone.utc))
    h = backend
    workspace, _, *_ = scale_department(h)
    client = h.client('coordinator')
    detail = f"/api/workspaces/{workspace['id']}"
    paths = [detail, detail + '?view=compact', '/api/workspaces?limit=100&offset=0']
    first = [client.get(path) for path in paths]
    assert all(response.status_code == 200 for response in first)
    reads = []

    def counted(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith('SELECT') and 'workspace_baselines.canonical_payload' in statement:
            reads.append(path)

    engine = h.app.state.engine
    event.listen(engine, 'before_cursor_execute', counted)
    try:
        for path, before in zip(paths, first, strict=True):
            after = client.get(path)
            assert (after.status_code, after.content) == (before.status_code, before.content)
    finally:
        event.remove(engine, 'before_cursor_execute', counted)
    assert not reads, f'Selected the baseline payload {len(reads)} times in warmed workspace reads'


def test_list_checks_stream_without_loading_workspace_validation_entities(backend):
    from apps.api.services.workspace_reads import stored_checks
    from apps.api.services.planning import health
    h = backend
    workspaces = [h.shift(h.workspace('Stream ' + str(n)))['workspace'] for n in range(3)]
    engine = h.app.state.engine
    with transaction(engine) as db:
        expected = {w['id']: health(db.get(m.Workspace, w['id'])) for w in workspaces}
    with transaction(engine, write=True) as db:
        db.execute(delete(m.WorkspaceReadProjection).where(m.WorkspaceReadProjection.kind == 'stored_check'))
    queries = []

    def counted(_conn, _cursor, statement, _parameters, context, _many):
        if 'workspaces.validation' in statement:
            queries.append(context.execution_options.get('yield_per'))

    with transaction(engine) as db:
        rows = list(db.scalars(select(m.Workspace).options(defer(m.Workspace.validation)).where(
            m.Workspace.id.in_([w['id'] for w in workspaces]))))
        event.listen(engine, 'before_cursor_execute', counted)
        try:
            checks = stored_checks(db, rows)
        finally:
            event.remove(engine, 'before_cursor_execute', counted)
        assert {wid: check['health'] for wid, check in checks.items()} == expected
        assert all('validation' not in row.__dict__ for row in rows)
        assert queries == [1]  # Legacy acquisition still streams exactly once for the batch.
        queries.clear()
        assert stored_checks(db, rows) == checks
        assert queries == []
        assert snapshots.fingerprints(db, rows, checks=checks) == {
            row.id: snapshots.domain_snapshot(db, row)['fingerprint'] for row in rows}
        assert all('validation' not in row.__dict__ for row in rows)


def test_batch_fingerprints_release_each_rota_snapshot(backend, monkeypatch):
    h = backend
    workspaces = [h.shift(h.workspace('Batch ' + str(n)))['workspace'] for n in range(3)]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    original, references, retained = snapshots.domain_snapshot, [], []

    class Snapshot(dict):
        pass

    def tracked(*args, **kwargs):
        retained.append(sum(ref() is not None for ref in references))
        value = Snapshot(original(*args, **kwargs))
        references.append(weakref.ref(value))
        return value

    monkeypatch.setattr(snapshots, 'domain_snapshot', tracked)
    with transaction(h.app.state.engine) as db:
        rows = [db.get(m.Workspace, w['id']) for w in workspaces]
        expected = {w.id: original(db, w)['fingerprint'] for w in rows}
        assert snapshots.fingerprints(db, rows) == expected
        assert not db.info.get('read_snapshots')
    assert len(references) == len(workspaces)
    assert retained == [0] * len(workspaces)
    assert all(ref() is None for ref in references)


def test_collection_reads_rebuild_only_rotas_under_review(backend, monkeypatch):
    h = backend
    freeze_clock(monkeypatch)
    draft = h.shift(h.workspace('Draft'))['workspace']
    review = h.lifecycle(h.shift(h.workspace('Review'))['workspace'], 'submit')['workspace']
    # Any department input change makes every stored hint and projection stamp stale.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    rebuilt, original = [], snapshots.domain_snapshot
    def counted(db, workspace, *args, **kwargs):
        rebuilt.append(workspace.id)
        return original(db, workspace, *args, **kwargs)
    monkeypatch.setattr(snapshots, 'domain_snapshot', counted)
    listed = {w['id']: w for w in h.get('coordinator', '/api/workspaces')['workspaces']}
    strip = {w['id']: w for w in h.get('coordinator', '/api/home/rotas?start=2026-10-01&end=2026-11-01')['workspaces']}
    # Drafts can be many private copies: unconfirmed, never rebuilt. A rota under review keeps its exact currency.
    assert draft['id'] not in rebuilt and review['id'] in rebuilt
    assert listed[draft['id']]['health']['fresh'] is False and strip[draft['id']]['health']['fresh'] is False
    assert listed[review['id']]['outdated'] is False
    # Home's Needs you stays exact for drafts as well, so their blocking issues never drop out.
    assert review['id'] in str(h.get('approver', '/api/home'))
    # Once the worker warms the draft's projection, collections reuse it without a rebuild.
    from apps.api.services.read_projection_warming import warm_all
    warm_all(h.app.state.engine)
    rebuilt.clear()
    listed = {w['id']: w for w in h.get('coordinator', '/api/workspaces')['workspaces']}
    assert draft['id'] not in rebuilt and listed[draft['id']]['health']['fresh'] is True


def test_department_reads_reuse_current_work_without_snapshots_or_evaluation(backend, monkeypatch):
    h = backend
    freeze_clock(monkeypatch)
    w, data, _, _ = scale_department(h)
    # The scale helper imports rows directly; model the input revision of the supported command path.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    paths = {'/api/workspaces': 25, f'/api/workspaces/{w["id"]}/fix-list': 40,
             f'/api/workspaces/{w["id"]}': 65, '/api/home': 120,
             '/api/home/rotas?start=2026-10-01&end=2026-11-01': 25, '/api/requests/all': 120}
    before = [h.get('coordinator', path) for path in paths]
    def forbidden(*args, **kwargs):
        pytest.fail('A repeated read must not rebuild a snapshot, evaluate or score')
    monkeypatch.setattr(snapshots, 'domain_snapshot', forbidden)
    monkeypatch.setattr(service, 'domain_snapshot', forbidden)
    monkeypatch.setattr(service, 'evaluate', forbidden)
    monkeypatch.setattr(service, 'schedule_score', forbidden)
    assert len(data['people']) >= 150 and len(data['focus_ids']) >= 500
    for path, expected in zip(paths, before):
        value, work = measure(h, path, lambda: h.get('coordinator', path))
        assert value == expected
        # Measured scratch baseline: 57–365 queries, plus repeated full scheduling acquisition.
        # A generous fixed query budget is independent of CPU speed/CI tracing.
        assert work['sql'].get('SELECT', 0) <= paths[path], work


def test_read_projection_stamp_ignores_deployment_source_digest(backend, monkeypatch):
    """Revision/toolchain currency keeps durable rows valid across an API-only restart."""
    from apps.api.services import read_projections
    h = backend
    workspace = h.workspace()
    with transaction(h.app.state.engine) as db:
        first = read_projections.input_stamp(db, db.get(m.Workspace, workspace['id']))
        monkeypatch.setattr(read_projections, 'source_identity', lambda _db: 'different-deploy-source')
        second = read_projections.input_stamp(db, db.get(m.Workspace, workspace['id']))
    assert first == second


def test_home_and_requests_use_durable_department_reads_after_process_cache_reset(h, monkeypatch):
    """Department reads remain shared when another API worker has no local cache."""
    home_client = h.client('coordinator')
    first_home = home_client.get('/api/home')
    assert first_home.status_code == 200
    from apps.api.services import home as home_service
    home_service._HOME_CACHE.clear()
    monkeypatch.setattr(home_service, '_home_uncached',
                        lambda *_args, **_kwargs: pytest.fail('durable Home miss rebuilt the page'))
    assert home_client.get('/api/home').json() == first_home.json()

    request_client = h.client('coordinator')
    first_requests = request_client.get('/api/requests/all')
    assert first_requests.status_code == 200
    from apps.api.services import availability
    original = availability.list_all
    monkeypatch.setattr(availability, 'list_all',
                        lambda *_args, **_kwargs: pytest.fail('durable Requests miss rebuilt the page'))
    assert request_client.get('/api/requests/all').json() == first_requests.json()
    monkeypatch.setattr(availability, 'list_all', original)


def test_causes_summary_does_not_require_shift_rows(h):
    """The compact causes view deliberately omits shifts and still answers from a reusable check."""
    workspace = h.workspace('Causes without shifts')
    response = h.client('coordinator').get(f"/api/workspaces/{workspace['id']}/summary?view=causes")
    assert response.status_code == 200
    assert set(response.json()) == {'workspace_id', 'revision', 'fingerprint', 'observations',
                                    'repair_tasks', 'cause_groups'}


def test_rule_map_projection_hit_does_not_rebuild_snapshot(backend, monkeypatch):
    h = backend
    workspace = h.shift(h.workspace('Rule map projection'))['workspace']
    path = f'/api/workspaces/{workspace["id"]}/rule-map'
    calls, original = [], main.domain_snapshot

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(main, 'domain_snapshot', counted)
    first = h.get('coordinator', path)
    assert len(calls) == 1
    second = h.get('coordinator', path)
    assert len(calls) == 1
    assert second['rules'] == first['rules'] and second['markers'] == first['markers']


def test_rule_map_returns_uncached_data_when_projection_write_is_locked(backend, caplog):
    h = backend
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    workspace = h.shift(h.workspace('Rule map projection'))['workspace']
    path = f'/api/workspaces/{workspace["id"]}/rule-map'
    expected = h.get('coordinator', path)
    assert expected['rules']
    engine = h.app.state.engine
    projection = m.WorkspaceReadProjection
    key = (projection.workspace_id == workspace['id'], projection.kind == 'rule_map')
    with transaction(engine, write=True) as db:
        db.execute(delete(projection).where(*key))

    with engine.begin() as writer:
        if engine.dialect.name == 'sqlite':
            writer.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            writer.exec_driver_sql('LOCK TABLE workspace_read_projections IN SHARE MODE')
        actual = h.get('coordinator', path)
        assert writer.scalar(select(projection.kind).where(*key)) is None

    actual.pop('elapsed_ms')
    expected.pop('elapsed_ms')
    assert actual == expected

    [warning] = [record for record in caplog.records if record.message == 'read_projection_store_skipped']
    assert warning.error_code == 'OperationalError' and warning.exc_info is None


def test_stale_summary_is_recomputed_once_without_replacing_clinical_evidence(backend, monkeypatch):
    h = backend
    freeze_clock(monkeypatch)
    w = h.shift(h.workspace(), people=[])['workspace']
    stored = h.detail(w)['workspace']['validation']
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    calls, evaluate = [], service.evaluate
    def counted(*args, **kwargs):
        calls.append(1)
        return evaluate(*args, **kwargs)
    monkeypatch.setattr(service, 'evaluate', counted)
    first = h.get('coordinator', f'/api/workspaces/{w["id"]}/summary')
    assert first['fix_list'] == h.get('coordinator', f'/api/workspaces/{w["id"]}/fix-list')
    assert first == h.get('coordinator', f'/api/workspaces/{w["id"]}/summary')
    assert calls == [1]
    assert h.detail(w)['workspace']['validation'] == stored
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    unrelated = h.get('coordinator', f'/api/workspaces/{w["id"]}/summary')
    assert unrelated == first and calls == [1]
    h.policy({'kind': 'maximum_participants', 'maximum': 0})
    changed = h.get('coordinator', f'/api/workspaces/{w["id"]}/summary')
    assert changed['fingerprint'] != first['fingerprint'] and calls == [1, 1]


@pytest.mark.postgres
def test_concurrent_stale_reads_do_not_wait_on_a_refresh_lock(pg, monkeypatch):
    h = pg
    freeze_clock(monkeypatch)
    w = h.shift(h.workspace(), people=[])['workspace']
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    # Establish scoped currency first; its lock otherwise coalesces readers before the summary barrier.
    h.get('coordinator', '/api/workspaces')
    client = h.client('coordinator')
    ready, release, together = Event(), Event(), Barrier(4)
    readers, calls, evaluate = [], [], service.evaluate
    def queried(_conn, _cursor, statement, parameters, _context, _many):
        if ('FROM workspace_read_projections' in statement and 'FOR UPDATE' not in statement
                and 'summary_check' in str(parameters)):
            readers.append(1)
            if len(readers) == 4:
                ready.set()
    def counted(*args, **kwargs):
        calls.append(1)
        assert release.wait(60), 'Test did not release the shared refresh'
        return evaluate(*args, **kwargs)
    def read(_n):
        together.wait(60)
        response = client.get(f'/api/workspaces/{w["id"]}/summary')
        assert response.status_code == 200, response.text
        return response.json()
    monkeypatch.setattr(service, 'evaluate', counted)
    event.listen(h.app.state.engine, 'before_cursor_execute', queried)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(read, n) for n in range(4)]
            try:
                assert ready.wait(60), 'Concurrent readers never reached the shared projection'
            finally:
                release.set()
            values = [future.result() for future in futures]
    finally:
        release.set()
        event.remove(h.app.state.engine, 'before_cursor_execute', queried)
    assert 1 <= len(calls) <= 4 and all(value == values[0] for value in values)
    with transaction(h.app.state.engine) as db:
        row = db.scalar(select(m.WorkspaceReadProjection).where(
            m.WorkspaceReadProjection.workspace_id == w['id'], m.WorkspaceReadProjection.kind == 'summary_check'))
        assert row.value['validation']['fingerprint'] == values[0]['fingerprint']


@pytest.mark.postgres
def test_a_slow_read_never_holds_a_projection_row_that_a_writer_needs(pg, monkeypatch):
    """SW3-D-21: a writer holding the global write lock queued behind a read request's projection row locks."""
    h = pg
    freeze_clock(monkeypatch)
    w = h.shift(h.workspace(), people=[])['workspace']
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        db.execute(delete(m.WorkspaceReadProjection).where(m.WorkspaceReadProjection.workspace_id == w['id']))
    inside, release, evaluate = Event(), Event(), service.evaluate
    def slow(*args, **kwargs):
        inside.set()
        assert release.wait(60), 'Test did not release the reader'
        return evaluate(*args, **kwargs)
    monkeypatch.setattr(service, 'evaluate', slow)
    client = h.client('coordinator')
    with ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(client.get, f'/api/workspaces/{w["id"]}/summary')
        try:
            # The reader has refreshed this rota's stored check and fingerprint and is now evaluating.
            assert inside.wait(60), 'The summary read never reached its evaluation'
            with transaction(engine, write=True) as db:
                workspace = db.get(m.Workspace, w['id'])
                assert workspace.validation is not None  # loaded, so the flush upserts its stored check
                workspace.name = 'Renamed while a summary is read'
        finally:
            release.set()
        assert reader.result().status_code == 200
    assert h.detail(w)['workspace']['name'] == 'Renamed while a summary is read'


def test_review_reads_reuse_resolved_currency_without_changing_command_checks(backend, monkeypatch):
    from apps.api.services import reviews
    h = backend
    freeze_clock(monkeypatch)
    w = h.lifecycle(h.shift(h.workspace())['workspace'], 'submit')['workspace']
    paths = ['/api/home', f'/api/workspaces/{w["id"]}']
    before = [h.get('approver', path) for path in paths]
    with transaction(h.app.state.engine) as db:
        # The API adds the viewer's decision access to the shared progress.
        assert reviews.progress(db, db.get(m.Workspace, w['id'])).items() <= before[1]['review_progress'].items()
    assert before[1]['review_progress']['stale_inputs'] is False
    monkeypatch.setattr(snapshots, 'domain_snapshot', lambda *a, **kw: pytest.fail('Repeated review snapshot'))
    monkeypatch.setattr(reviews, 'fingerprint', lambda *a, **kw: pytest.fail('Duplicate review fingerprint'))
    assert [h.get('approver', path) for path in paths] == before


def test_generation_job_reads_reuse_resolved_currency(backend, monkeypatch):
    h = backend
    freeze_clock(monkeypatch)
    w = h.shift(h.workspace())['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'},
                    revision=w['revision'])['job']
    paths = ['/api/jobs', '/api/jobs/' + job['id']]
    before = [h.get('coordinator', path) for path in paths]
    monkeypatch.setattr(snapshots, 'domain_snapshot', lambda *a, **kw: pytest.fail('Repeated job snapshot'))
    for path, expected in zip(paths, before):
        value, work = measure(h, 'job read', lambda: h.get('coordinator', path))
        assert value == expected and work['sql'].get('SELECT', 0) <= 30, work


def test_rule_review_polls_reuse_each_drafts_feasibility_inputs(backend, monkeypatch):
    """maintenance/SW3-B-19: every review poll rebuilt a full snapshot per overlapping draft."""
    from tests.test_rule_catalogue_api import line, propose, approve
    h = backend
    current = approve(h, propose(h, 'duties_per_week', [line(numbers={'max': {'value': 4}})])['record'])
    drafts = [h.shift(h.workspace(f'Review {n}'))['workspace'] for n in range(2)]
    pending = propose(h, 'duties_per_week', [line(numbers={'max': {'value': 3}})],
                      policy_id=current['id'], revision=current['revision'])['record']
    path = '/api/policies/' + pending['id'] + '/review'
    first = h.get('approver', path)
    assert {d['id'] for d in drafts} <= {e['workspace_id'] for e in first['effect']}
    built, original = [], snapshots.domain_snapshot
    def counted(db, workspace, *args, **kwargs):
        built.append(workspace.id)
        return original(db, workspace, *args, **kwargs)
    monkeypatch.setattr(snapshots, 'domain_snapshot', counted)
    assert h.get('approver', path) == first and built == []
    h.shift(drafts[0], '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')
    built.clear()
    review = h.get('approver', path)
    assert built == []
    assert {effect['workspace_id']: effect['status'] for effect in review['effect']} == {
        draft['id']: 'not_checked' for draft in drafts}


def test_department_workspace_list_batches_stored_check_stamps(backend):
    h = backend
    workspace, _, *_ = scale_department(h)
    extra = [h.workspace(f'Scale draft {index}')['id'] for index in range(4)]
    client = h.client('coordinator')
    path = '/api/workspaces?limit=100&offset=0'
    first = client.get(path)
    assert first.status_code == 200
    assert {workspace['id'], *extra} <= {row['id'] for row in first.json()['workspaces']}
    stamp_reads = 0

    def count(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal stamp_reads
        columns = statement.split('FROM', 1)[0]
        if 'workspace_read_projections.stamp' in columns and 'workspace_read_projections.value' not in columns:
            stamp_reads += 1

    engine = h.app.state.engine
    event.listen(engine, 'before_cursor_execute', count)
    try:
        after = client.get(path)
    finally:
        event.remove(engine, 'before_cursor_execute', count)
    assert (after.status_code, after.content) == (first.status_code, first.content)
    assert stamp_reads <= 2, stamp_reads


def test_department_amendment_reads_its_shift_links_once(backend):
    h = backend
    original, *_ = scale_department(h)
    issued = h.publish(h.detail(original)['workspace'])['published_view']['publication_id']
    amendment = h.workspace('Synthetic amendment', start=original['starts_on'], end=original['ends_on'], base=issued)
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        shift = db.scalar(select(m.Shift).where(m.Shift.workspace_id == amendment['id'])
                          .order_by(m.Shift.start, m.Shift.id).limit(1))
        shift.locked = True
        changed_id = shift.id
    h.command('coordinator', 'workspace.validate', {'reason': 'Check amended rota'},
              amendment['id'], amendment['revision'])
    assert h.detail(amendment)['workspace']['validation_current']
    links = []

    def count_links(_conn, _cursor, sql, _args, _context, _many):
        if 'FROM shift_services' in sql and 'UNION ALL' in sql:
            links.append(sql)

    event.listen(engine, 'before_cursor_execute', count_links)
    try:
        detail = h.detail(amendment)
    finally:
        event.remove(engine, 'before_cursor_execute', count_links)

    assert len(detail['shifts']) >= 500
    [change] = detail['change_diff']['sessions']
    assert change['after']['id'] == changed_id and change['after']['locked'] and not change['before']['locked']
    assert all('wish_effects' not in participant for participant in change['after']['participants'])
    assert len(links) == 1, 'The review and full rota share the current session links'

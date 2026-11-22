"""Demo blockers: prepared activation, idle workers and resolved offered-overtime audiences."""
import time
import tracemalloc
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.exc import SQLAlchemyError

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import DatabaseWriteDeferred, retry_write, transaction
from apps.api.services import policies, rule_effects, read_projection_warming
from apps.worker import main as worker, score_check
from packages.contracts import schemas as s
from tests.test_fix_list_performance import backend
from tests.test_rule_templates import propose
from tests.test_overtime_extra_cover import extra_type
from tests.test_rule_catalogue_api import propose as catalogue_propose, line, person

def _rss_kib(field):
    with open('/proc/self/status') as status:
        return next(int(row.split()[1]) for row in status if row.startswith(field + ':'))


def _start_peak_rss():
    """Reset this process's peak RSS (Linux clear_refs 5) and return the resident size it restarts from.

    ru_maxrss is the worker process's lifetime peak, so an earlier department-scale test in the same worker
    would fail this per-request bound. None where /proc is unavailable; tracemalloc still bounds the request."""
    try:
        with open('/proc/self/clear_refs', 'w') as refs:
            refs.write('5')
        return _rss_kib('VmRSS')
    except (OSError, StopIteration):
        return None


@pytest.mark.parametrize('checked', [False, True])
def test_activation_heavy_work_precedes_writer_and_keeps_history(backend, monkeypatch, checked):
    h = backend
    payload = {'reason': 'Independent stability review'}
    if checked:
        from tests.test_contextual_feasibility import shortage_review, use_checked_activation
        use_checked_activation(h)
        _, proposal, receipt = shortage_review(h)
        payload['check_job_ids'] = [receipt['id']]
    else:
        h.shift(h.workspace())
        proposal = propose(h, {'kind': 'restriction'})
    phases = []
    for name in ('policy_impact', 'require_context_receipts'):
        original = getattr(policies, name)
        def observed(db, *args, _name=name, _original=original, **kwargs):
            phases.append((_name, bool(db.connection().info.get('rota_write'))))
            return _original(db, *args, **kwargs)
        monkeypatch.setattr(policies, name, observed)
    result = h.command('approver', 'policy.activate', payload, proposal['id'], proposal['revision'])
    assert phases == ([('require_context_receipts', False)] if checked else []) + ([] if checked else [('policy_impact', False)])
    assert result['record']['status'] == 'active'
    if checked:
        assert result['context_check_evidence'][0]['check_job_id'] == receipt['id']
        assert result['impact'] == []
    else:
        assert result['impact']
    with transaction(h.app.state.engine) as db:
        rows = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == proposal['id'])))
        assert {r.snapshot['status'] for r in rows} == {'proposed', 'active'}
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.target_id == proposal['id'],
                                                   m.AuditEvent.action == 'policy.activate'))
        assert audit is not None
        if checked:
            assert audit.detail['context_check_evidence'] == result['context_check_evidence']


def test_activation_receipts_reuse_currency_without_building_snapshots(backend, monkeypatch):
    """A current receipt must not materialise every rota just to re-hash its inputs."""
    h = backend
    from tests.test_contextual_feasibility import shortage_review, use_checked_activation
    use_checked_activation(h)
    _, proposal, receipt = shortage_review(h)
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, h.client('approver').cookies['rota_session'])
        policy = db.get(m.Policy, proposal['id'])
        decision = s.PolicyActivationDecision.model_validate({
            'reason': 'Independent stability review', 'check_job_ids': [receipt['id']]})
        monkeypatch.setattr(policies, 'feasibility_fingerprints',
                            lambda *args, **kwargs: pytest.fail('current activation receipt rebuilt a full snapshot'))
        rss_start = _start_peak_rss()
        tracemalloc.start()
        try:
            evidence = policies.require_context_receipts(db, actor, [(policy, decision.reason)], [decision],
                                                         compare_problems=True)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        rss_peak = _rss_kib('VmHWM') if rss_start is not None else None
    assert evidence and evidence[0]['check_job_id'] == receipt['id']
    # The pre-fix path reached ~6.2 GiB RSS in the shared 8 GiB cgroup.  Keep
    # the synchronous activation path below the owner's 1.5 GiB per-request
    # ceiling (8 GiB / 4 workers with interpreter and database headroom).
    if rss_start is not None:
        rss_bytes = (rss_peak - rss_start) * 1024
        assert rss_bytes < int(1.5 * 1024 * 1024 * 1024), f'activation receipt peak RSS: {rss_bytes}'
    assert peak < int(1.5 * 1024 * 1024 * 1024), f'activation receipt peak allocation: {peak}'


@pytest.mark.parametrize('change', ['inputs', 'availability', 'draft', 'proposal', 'new_draft'])
def test_activation_refuses_changed_preparation(backend, monkeypatch, change):
    h = backend
    workspace = h.workspace()
    proposal = propose(h, {'kind': 'restriction'})
    original = policies.prepare_activation
    def prepare(db, *args, **kwargs):
        prepared = original(db, *args, **kwargs)
        # A fresh transaction commits after the read snapshot and before the writer.
        with transaction(h.app.state.engine, write=True) as other:
            if change in {'inputs', 'availability'}:
                row = other.get(m.Organisation, h.seed['organisation_id'])
                field = 'input_revision' if change == 'inputs' else 'availability_revision'
                setattr(row, field, getattr(row, field) + 1)
            elif change == 'draft':
                other.get(m.Workspace, workspace['id']).content_revision += 1
            elif change == 'proposal':
                other.get(m.Policy, proposal['id']).revision += 1
            else:
                other.add(m.Workspace(department_id=h.seed['department_id'], name='Concurrent draft',
                    starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11), created_by=h.seed['identities']['coordinator'],
                    last_edited_by=h.seed['identities']['coordinator']))
        return prepared
    monkeypatch.setattr(policies, 'prepare_activation', prepare)
    result = h.command('approver', 'policy.activate', {'reason': 'Independent stability review'},
                       proposal['id'], proposal['revision'], status=409)
    assert result['error']['code'] == 'STALE_INPUTS'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, proposal['id']).status == 'proposed'


def test_reach_excludes_archived_disjoint_dates_and_services(backend):
    h = backend
    matched = h.shift(h.workspace(), service=0)['workspace']
    h.shift(h.workspace('Disjoint clinic'), service=1)
    archived = h.workspace('Archived')
    h.workspace('Earlier', '2025-01-01', '2025-02-01')
    h.workspace('Later', '2027-01-01', '2027-02-01')
    proposal = propose(h, {'kind': 'restriction'}, {'service_ids': [h.seed['services'][0]]},
                       starts_on='2026-10-01', ends_on='2026-11-01')
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, archived['id']).archived_at = m.now()
    client = h.client('approver')
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'])
        assert [w.id for w in rule_effects.affected_workspaces(db, actor, [db.get(m.Policy, proposal['id'])])] == [matched['id']]


def test_idle_passes_write_nothing_or_recompute_until_revision_moves(backend, monkeypatch):
    h = backend
    workspace = h.workspace()
    engine = h.app.state.engine
    # Finish the first maintenance pass and drain disposable projections/outboxes.
    for _ in range(5):
        worker.process_one(engine, settings=h.settings)
    writes = []
    def capture(_c, _cu, statement, _p, _ctx, _many):
        if statement.lstrip().split()[0].upper() in {'INSERT', 'UPDATE', 'DELETE'} or 'pg_advisory_xact_lock' in statement:
            writes.append(statement)
    monkeypatch.setattr(worker, 'heartbeat', lambda *a, **kw: None)  # Periodic operational liveness is intentional.
    import apps.api.services.rota_summary as summaries
    original = summaries.summary_check
    def unexpected(*a, **kw):
        pytest.fail('Idle worker recomputed an unchanged projection')
    monkeypatch.setattr(summaries, 'summary_check', unexpected)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        start = time.process_time()
        for _ in range(10):
            assert not worker.process_one(engine, settings=h.settings)
            assert not score_check.process_one(engine, h.settings)
        print('idle_10_passes_cpu_seconds', round(time.process_time() - start, 4))
        assert writes == []
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    monkeypatch.setattr(summaries, 'summary_check', original)
    with transaction(engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).availability_revision += 1
    assert read_projection_warming.warm_one(engine)
    assert not read_projection_warming.warm_one(engine)


@pytest.mark.postgres
def test_busy_worker_yields_without_waiting_or_reporting_timeout(pg, monkeypatch, tmp_path):
    engine = pg.app.state.engine.execution_options(rota_defer_busy_writer=True)
    alive = tmp_path / 'alive'
    monkeypatch.setattr(worker, 'ALIVE_FILE', str(alive))
    called = []
    with transaction(pg.app.state.engine, write=True):
        with pytest.raises(DatabaseWriteDeferred):
            retry_write(engine, lambda db: called.append(True))
        worker._touch_alive_file()
        assert alive.read_text() and called == []
    retry_write(engine, lambda db: called.append(True))
    assert called == [True]


def test_fairness_api_and_solver_use_same_catalogue_overtime_audience(backend):
    h = backend
    people = h.seed['people'][:2]
    proposal = catalogue_propose(h, 'overtime_offers', [line(who=person(pid), numbers={'apply': {'level': 'medium'}},
        settings={'allocation_overtime': {'volunteer_required': False}}) for pid in people])['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent offers'}, proposal['id'], proposal['revision'])
    goal = h.policy({'kind': 'balance_goal', 'metric': 'overtime_duties', 'share_by': 'offered'}, severity='advisory')
    typ = extra_type(h)
    w = h.shift(h.workspace(), typ=typ['id'], people=[{'person_id': people[0], 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'overtime': True}])['workspace']
    snapshot = h.snapshot(w)
    from packages.domain.cp_sat import generate
    # Retained assignment fixes the planned amount; solve and read see the exact same saved work.
    snapshot['shifts'][0]['locked'] = True
    solved = generate(snapshot, workers=1, time_budget_seconds=10)
    assert solved['status'] == 'candidate_feasible', solved
    expected = next(r for r in solved['validation']['goals'] if r['policy_id'] == goal['id'])
    actual = next(r for r in h.get('coordinator', '/api/fairness/goals?workspace_id=' + w['id'])['goals']
                  if r['policy_id'] == goal['id'])
    fields = lambda report: [(r['person_id'], r['planned']) for r in report['people']]
    assert len(actual['people']) == 2
    assert sum(r['planned'] for r in actual['people']) == 1
    assert fields(actual) == fields(expected)


def test_department_scale_activation_prepares_once_and_writes_briefly(backend, monkeypatch):
    """Compare the retained, unprepared activation path to the public prepared path."""
    from datetime import timedelta
    from packages.contracts.schemas import Command, PolicyDefinition
    from apps.api.snapshots import feasibility_fingerprint, feasibility_fingerprints
    h = backend
    saved = h.shift(h.workspace())
    workspace, sample = saved['workspace'], saved['shifts'][0]
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        members = list(db.scalars(select(m.Membership).where(m.Membership.department_id == department.id)))
        known = {row.person_id for row in members}
        people = [m.Person(id=m.uid(), organisation_id=department.organisation_id, display_name=f'Scale person {i}')
                  for i in range(150 - len(known))]
        db.add_all(people)
        db.flush()
        db.add_all(m.Membership(department_id=department.id, person_id=p.id, category_id=members[0].category_id,
                               starts_on=date(2020, 1, 1)) for p in people)
        definition = PolicyDefinition.model_validate({'rule': {'kind': 'max_duties', 'maximum': 7}}).model_dump(mode='json')
        db.add_all(m.Policy(department_id=department.id, name=f'Historical rule {i}', kind='max_duties',
            definition=definition, status='retired', active=False, starts_on=date(2020, 1, 1),
            owner='Synthetic', source='Scale regression', created_by=h.seed['identities']['coordinator']) for i in range(1100))
        workspaces = [db.get(m.Workspace, workspace['id'])]
        for i in range(23):
            row = m.Workspace(id=m.uid(), department_id=department.id, name=f'Disjoint draft {i}',
                starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11),
                created_by=h.seed['identities']['coordinator'], last_edited_by=h.seed['identities']['coordinator'])
            db.add(row)
            workspaces.append(row)
        db.flush()
        from packages.domain.time import aware
        shifts, links = [], []
        for i, row in enumerate(workspaces):
            for j in range(21 if i == 0 else 22):
                start = aware(sample['start']) + timedelta(hours=j)
                shift = dict(id=m.uid(), workspace_id=row.id, assignment_type_id=sample['assignment_type_id'],
                    type_revision=sample['type_revision'], type_snapshot=sample['type_snapshot'],
                    location_id=sample['location_id'], start=start, end=start + timedelta(hours=1))
                shifts.append(shift)
                links.append(dict(shift_id=shift['id'], service_id=h.seed['services'][0 if i == 0 else 1]))
        db.execute(insert(m.Shift), shifts)
        db.execute(insert(m.ShiftService), links)
    with transaction(engine) as db:
        rows = list(db.scalars(select(m.Workspace).where(m.Workspace.department_id == h.seed['department_id'])))
        began = time.perf_counter()
        currencies = feasibility_fingerprints(db, rows)
        fingerprint_batch = time.perf_counter() - began
        same = feasibility_fingerprint(db, rows[0]) == currencies[rows[0].id]
    assert len(currencies) == len(rows)
    assert same
    proposal = propose(h, {'kind': 'max_duties', 'maximum': 0}, {'service_ids': [h.seed['services'][0]]}, severity='advisory')
    client = h.client('approver')
    command = Command(kind='policy.activate', target_id=proposal['id'], expected_revision=proposal['revision'],
                      payload={'reason': 'Independent scale review'})
    class RollbackMeasurement(Exception):
        pass
    # The fallback is the integration baseline algorithm (including CI-UNIT-4 batching).
    # Disable only this patch's reach pruning, then roll back all synthetic lifecycle writes.
    with monkeypatch.context() as baseline:
        baseline.setattr(rule_effects, 'reachable_workspaces', lambda db, actor, policies, rows: rows)
        began = time.perf_counter()
        with pytest.raises(RollbackMeasurement):
            with transaction(engine, write=True) as db:
                actor = authenticate(db, client.cookies['rota_session'])
                old = policies.activate_policies(db, actor, [command])
                raise RollbackMeasurement()
        before = time.perf_counter() - began
    began = time.perf_counter()
    with transaction(engine) as db:
        actor = authenticate(db, client.cookies['rota_session'])
        prepared = policies.prepare_activation(db, actor, command)
    preparation = time.perf_counter() - began
    assert prepared['workspace_ids'] == [workspace['id']]
    with monkeypatch.context() as writer:
        writer.setattr(policies, 'policy_impact', lambda *a, **kw: pytest.fail('Impact ran under the write lock'))
        writer.setattr(policies, 'require_context_receipts', lambda *a, **kw: pytest.fail('Receipts ran under the write lock'))
        began = time.perf_counter()
        with transaction(engine, write=True) as db:
            actor = authenticate(db, client.cookies['rota_session'])
            db.info['_activation_preparation'] = prepared
            result = policies.activate_policies(db, actor, [command])
        after = time.perf_counter() - began
    assert result['records'][0]['status'] == old['records'][0]['status'] == 'active'
    for rows in (result['impact'], old['impact']):
        for row in rows:
            row['evaluation'].pop('evaluated_at', None)
    assert result['impact'] == [row for row in old['impact'] if row['workspace_id'] == workspace['id']]
    print(f'STAB_SCALE backend={engine.dialect.name} people=150 drafts=24 sessions=528 history=1100 '
          f'fingerprint_batch={fingerprint_batch:.3f}s baseline_writer={before:.3f}s '
          f'preparation={preparation:.3f}s prepared_writer={after:.3f}s')
    return locals()


@pytest.mark.wall_benchmark
def test_department_scale_activation_wall_budgets(backend, monkeypatch):
    state = test_department_scale_activation_prepares_once_and_writes_briefly(backend, monkeypatch)
    assert state['fingerprint_batch'] < 10, state['fingerprint_batch']
    assert state['preparation'] < 10, state['preparation']


def test_deferred_push_passes_keep_alive_file_fresh_and_back_off(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv('ROTA_ENV', 'test')
    monkeypatch.setattr('sys.argv', ['worker', '--push-only'])
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    monkeypatch.setattr(worker, 'make_engine', lambda *a, **kw: object())
    alive = tmp_path / 'alive'
    monkeypatch.setattr(worker, 'ALIVE_FILE', str(alive))
    def busy(*args, **kwargs):
        raise DatabaseWriteDeferred()
    monkeypatch.setattr(worker, 'process_push_one', busy)
    pauses = []
    def pause(seconds):
        assert alive.read_text()
        alive.unlink()  # Every resumed pass must recreate the file.
        pauses.append(seconds)
        if len(pauses) == 4:
            raise SystemExit('enough passes')
    monkeypatch.setattr(worker.time, 'sleep', pause)
    with pytest.raises(SystemExit, match='enough passes'):
        worker.main()
    assert pauses == [2, 4, 8, 16]
    assert not any('timeout' in r.message for r in caplog.records)


def test_push_only_listener_wakes_main_dispatch_and_refreshes_keep_alive(monkeypatch, tmp_path):
    monkeypatch.setenv('ROTA_ENV', 'test')
    monkeypatch.setattr('sys.argv', ['worker', '--push-only'])
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    monkeypatch.setattr(worker, 'make_engine', lambda *args, **kwargs: SimpleNamespace(
        dialect=SimpleNamespace(name='postgresql')))
    alive = tmp_path / 'alive'
    monkeypatch.setattr(worker, 'ALIVE_FILE', str(alive))
    settings = worker.Settings.from_env()
    monkeypatch.setattr(worker.Settings, 'from_env', classmethod(lambda cls: settings))

    class EventProbe:
        def __init__(self):
            self.set_state = False
            self.waits = []

        def is_set(self):
            return self.set_state

        def set(self):
            self.set_state = True

        def clear(self):
            self.set_state = False

        def wait(self, timeout=None):
            self.waits.append((timeout, self.set_state))
            return self.set_state

    event_objects = [EventProbe(), EventProbe(), EventProbe()]
    events = iter(event_objects)
    monkeypatch.setattr(worker.threading, 'Event', lambda: next(events))

    class InlineThread:
        def __init__(self, *, target, args=(), kwargs=None, **options):
            self.target, self.args, self.kwargs = target, args, kwargs or {}
            self.ident = None
            self.joined = False

        def start(self):
            self.target(*self.args, **self.kwargs)

        def join(self, timeout=None):
            self.joined = True

    monkeypatch.setattr(worker.threading, 'Thread', InlineThread)
    listener_options = []

    def listener(engine, settings, stopped, wake, **options):
        listener_options.append(options)
        # API availability commands publish this non-inbox scope; push-only wakes on every commit.
        worker._wake_job_waiter(SimpleNamespace(payload='{"scope":"availability"}'), wake, wake_scope=None)

    monkeypatch.setattr(worker, 'notification_loop', listener)
    passes = []

    def dispatch(engine, settings, **kwargs):
        assert alive.read_text()
        passes.append(True)
        alive.unlink()
        if len(passes) == 2:
            raise SystemExit('done')
        return False

    monkeypatch.setattr(worker, 'process_push_one', dispatch)
    monkeypatch.setattr(worker.time, 'sleep', lambda seconds: pytest.fail('listener path used polling sleep'))
    with pytest.raises(SystemExit, match='done'):
        worker.main()

    assert listener_options == [{'dispatch': False}]
    assert len(passes) == 2  # The notification caused the second pass immediately.
    assert event_objects[1].is_set() and event_objects[2].waits == [(settings.worker_poll_seconds, True)]
    assert not alive.exists()  # The second pass refreshed it before dispatching.


def test_push_only_idle_passes_keep_alive_and_bound_backoff(monkeypatch, tmp_path):
    monkeypatch.setenv('ROTA_ENV', 'test')
    monkeypatch.setattr('sys.argv', ['worker', '--push-only'])
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    monkeypatch.setattr(worker, 'make_engine', lambda *args, **kwargs: object())
    alive = tmp_path / 'alive'
    monkeypatch.setattr(worker, 'ALIVE_FILE', str(alive))
    settings = worker.Settings.from_env()
    monkeypatch.setattr(worker.Settings, 'from_env', classmethod(lambda cls: settings))
    passes, pauses = [], []

    def idle(engine, current, **kwargs):
        assert alive.read_text()
        passes.append(True)
        alive.unlink()
        return False

    def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 5:
            raise SystemExit('enough idle passes')

    monkeypatch.setattr(worker, 'process_push_one', idle)
    monkeypatch.setattr(worker.time, 'sleep', pause)
    with pytest.raises(SystemExit, match='enough idle passes'):
        worker.main()

    assert len(passes) == 5
    assert pauses == [2, 4, 8, 16, settings.worker_idle_max_seconds]
    assert all(wait <= min(settings.worker_idle_max_seconds, 2 ** index * settings.worker_poll_seconds)
               for index, wait in enumerate(pauses))
    assert settings.worker_idle_max_seconds <= 60
    assert settings.worker_notification_poll_seconds <= settings.worker_idle_max_seconds
    with pytest.raises(RuntimeError, match='liveness window'):
        worker.Settings(worker_idle_max_seconds=61).check(require_authentication=False)


def test_push_only_transient_database_error_refreshes_keep_alive_and_continues(monkeypatch, tmp_path):
    monkeypatch.setenv('ROTA_ENV', 'test')
    monkeypatch.setattr('sys.argv', ['worker', '--push-only'])
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    monkeypatch.setattr(worker, 'make_engine', lambda *args, **kwargs: object())
    monkeypatch.setattr(worker, 'is_transient_database_error', lambda exc: True)
    alive = tmp_path / 'alive'
    monkeypatch.setattr(worker, 'ALIVE_FILE', str(alive))
    settings = worker.Settings.from_env()
    monkeypatch.setattr(worker.Settings, 'from_env', classmethod(lambda cls: settings))
    passes, pauses = [], []

    def unavailable(engine, current, **kwargs):
        assert alive.read_text()
        passes.append(True)
        alive.unlink()
        raise SQLAlchemyError('database unavailable')

    def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 3:
            raise SystemExit('enough unavailable passes')

    monkeypatch.setattr(worker, 'process_push_one', unavailable)
    monkeypatch.setattr(worker.time, 'sleep', pause)
    with pytest.raises(SystemExit, match='enough unavailable passes'):
        worker.main()

    assert len(passes) == 3
    assert pauses == [2, 4, 8]


def test_score_check_deferred_passes_back_off_without_timeout_storm(monkeypatch):
    from apps.api.settings import Settings
    class Stop:
        def __init__(self):
            self.pauses = []
        def is_set(self):
            return len(self.pauses) == 5
        def wait(self, seconds):
            self.pauses.append(seconds)
    stopped = Stop()
    def busy(*args):
        raise DatabaseWriteDeferred()
    monkeypatch.setattr(score_check, 'process_one', busy)
    score_check.run_loop(None, Settings(), stopped)
    assert stopped.pauses == [2, 4, 8, 16, 30]


@pytest.mark.postgres
def test_completed_result_lock_wait_reports_liveness_until_writer_is_available(pg, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    alive = tmp_path / 'alive'
    monkeypatch.setattr(worker, 'ALIVE_FILE', str(alive))
    waiting = Event()
    calls = []
    def report_wait():
        worker._touch_alive_file()
        waiting.set()
    engine = pg.app.state.engine.execution_options(rota_write_wait_callback=report_wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(pg.app.state.engine, write=True):
            result = pool.submit(retry_write, engine, lambda db: calls.append('saved'))
            assert waiting.wait(10)
            assert alive.read_text() and calls == []
        result.result(timeout=10)
    assert calls == ['saved']

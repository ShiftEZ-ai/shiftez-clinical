"""One publication projection per fan-out; portable benchmark and bounded connection pools."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
from time import process_time, perf_counter
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.exc import TimeoutError

from apps.api import models as m
from apps.api.db import make_engine
from apps.api.services import schedule
from apps.api.settings import Settings
from apps.worker import main as worker
from scripts import benchmark_scheduler
from tests.test_performance_budget import python_budget


def publication_fixture():
    people = [f'person-{n}' for n in range(150)]
    snapshot = {'timezone': 'UTC', 'workspace': {'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
        'shifts': [{'id': f'shift-{n}', 'assignment_type_id': 'type',
            'start': f'{date(2026, 10, 1) + timedelta(days=n % 30)}T08:00:00Z',
            'participants': [{'person_id': people[n % 150],
                'start': f'{date(2026, 10, 1) + timedelta(days=n % 30)}T08:00:00Z'}]} for n in range(500)],
        'wishes': [{'id': p, 'person_id': p, 'kind': 'prefer_off', 'first_day': '2026-10-01',
            'last_day': '2026-10-30', 'outcome': 'granted'} for p in people]}
    publication = NS(id='publication', workspace_id='workspace', sequence=1, current=True, snapshot=snapshot,
        starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1), effective_until=date(2026, 10, 16))
    identities = {p: NS(id=p, person_id=p) for p in people}
    return publication, identities


class FanoutDb:
    def __init__(self, publication, identities):
        self.publication, self.identities = publication, identities
        self.notifications, self.done = [], []

    def get(self, model, key):
        if model is m.Publication:
            return self.publication
        if model is m.Workspace:
            return NS(name='Synthetic rota')
        assert model is m.Identity
        return self.identities[key]

    def scalars(self, query):
        return self.done

    def scalar(self, query):
        assert query.column_descriptions[0]['entity'] is m.SwapRequest
        return None

    def add(self, row):
        self.notifications.append(row)

    def flush(self):
        pass


@pytest.fixture(scope='module')
def publication_fanout():
    publication, identities = publication_fixture()
    frozen = deepcopy(publication.snapshot)
    expected = {p: worker.wish_line(None, publication, identity) for p, identity in identities.items()}
    db = FanoutDb(publication, identities)
    event = NS(id='event', kind='publication', payload={'publication_id': publication.id})
    builds = []
    project = schedule.in_force_snapshot
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(worker, 'recipients_of', lambda *args: set(identities))
        def counted(db, publication):
            builds.append(publication.id)
            return project(db, publication)
        patch.setattr(schedule, 'in_force_snapshot', counted)
        cpu, wall = process_time(), perf_counter()
        worker.deliver_notifications(db, event, roles={}, identities=identities)
        elapsed, wall = process_time() - cpu, perf_counter() - wall
        db.done = list(identities)
        worker.deliver_notifications(db, event, roles={}, identities=identities)
    print('publication fanout', {'recipients': 150, 'sessions': 500, 'snapshot_builds': len(builds),
        'cpu_seconds': elapsed, 'wall_seconds': wall})
    return db, publication, frozen, expected, builds, elapsed


def test_publication_fanout_builds_snapshot_once_and_keeps_personal_outcomes(publication_fanout):
    db, publication, frozen, expected, builds, _ = publication_fanout
    assert builds == [publication.id]
    assert len(db.notifications) == 150
    for note in db.notifications:
        assert note.message.endswith(expected[note.identity_id])
    assert publication.snapshot == frozen


@pytest.mark.cpu_budget
def test_publication_fanout_cpu_budget(publication_fanout):
    assert publication_fanout[-1] < python_budget(0.1)


def test_legacy_publication_wishes_are_loaded_in_one_query():
    queries = []
    db = NS(execute=lambda query: queries.append(query) or [('one', 'granted'), ('one', 'partly'), ('two', 'declined')])
    publication = NS(id='legacy', snapshot={})
    wishes = worker.publication_wishes(db, publication)
    assert len(queries) == 1
    assert worker.wish_line(db, publication, NS(person_id='one'), wishes).startswith(' Your preferences: 1 granted, 1 partly granted.')
    assert '1 not granted' in worker.wish_line(db, publication, NS(person_id='two'), wishes)
    assert worker.wish_line(db, publication, NS(person_id=None), wishes) == ''
    assert len(queries) == 1


def test_file_connection_pool_honours_configured_capacity_and_memory_sqlite_still_works(tmp_path):
    settings = Settings(environment='test', db_pool_size=2, db_max_overflow=1, db_pool_timeout_seconds=1)
    engine = make_engine('sqlite:///' + str(tmp_path / 'pool.db'), settings=settings)
    try:
        with ExitStack() as stack:
            for _ in range(3):
                stack.enter_context(engine.connect())
            assert engine.pool.size() == 2 and engine.pool.overflow() == 1
            with pytest.raises(TimeoutError):
                engine.connect()
    finally:
        engine.dispose()
    memory = make_engine('sqlite:///:memory:', settings=settings)
    try:
        with memory.connect() as db:
            assert db.exec_driver_sql('SELECT 1').scalar_one() == 1
    finally:
        memory.dispose()


def test_connection_factory_loads_only_database_pool_environment(monkeypatch, tmp_path):
    monkeypatch.setenv('ROTA_DB_POOL_SIZE', '3')
    monkeypatch.setenv('ROTA_DB_MAX_OVERFLOW', '0')
    monkeypatch.setenv('ROTA_RATE_COMMANDS_PER_MINUTE', 'not-an-integer')
    monkeypatch.setenv('OIDC_CLIENT_SECRET_FILE', str(tmp_path / 'absent-secret'))
    engine = make_engine('sqlite:///' + str(tmp_path / 'env-pool.db'))
    try:
        settings = engine.get_execution_options()['rota_write_settings']
        assert (settings.db_pool_size, settings.db_max_overflow) == (3, 0)
        assert engine.pool.size() == 3
    finally:
        engine.dispose()


def test_benchmark_accepts_smoke_budget_without_unix_resource_and_preserves_solver_status(tmp_path, monkeypatch):
    source = tmp_path / 'input.json'
    source.write_text(json.dumps({'input': {'synthetic': True}}), encoding='utf-8')
    output = tmp_path / 'output'
    calls = []
    def solve(snapshot, *, time_budget_seconds):
        calls.append((snapshot, time_budget_seconds))
        return {'status': 'unknown_time_limit', 'optimality_proven': False}
    monkeypatch.setattr(benchmark_scheduler, 'cp_sat', solve)
    monkeypatch.setattr(benchmark_scheduler, 'resource', None)
    monkeypatch.setattr(benchmark_scheduler.sys, 'argv', ['benchmark_scheduler', str(source), 'cp_sat', str(output),
        '--source-commit', 'frozen', '--expected-sha256', hashlib.sha256(source.read_bytes()).hexdigest(), '--seconds', '60'])
    benchmark_scheduler.main()
    summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
    assert calls == [({'synthetic': True}, 60)]
    assert summary['status'] == 'unknown_time_limit' and not summary['optimality_proven']
    assert summary['peak_rss_mib'] is None
    assert not summary['validation_performed']

"""Large request/job graphs have explicit lifetimes; response reclamation is optional."""
import asyncio
from dataclasses import replace
import json

import pytest

from apps.api import memory, snapshots
from apps.api.db import transaction
from apps.api.settings import Settings
from apps import process_memory


def test_transaction_releases_snapshot_and_projection_buffers(h):
    with transaction(h.app.state.engine) as db:
        db.info.update(write_domain_snapshots={'pickle': b'large snapshot'},
                       read_projection_inputs={'projection': object()})
    assert db.info == {}
    with pytest.raises(RuntimeError):
        with transaction(h.app.state.engine) as failed:
            failed.info['write_domain_snapshots'] = {'pickle': b'large snapshot'}
            raise RuntimeError('Synthetic failure')
    assert failed.info == {}


def test_snapshot_cache_discards_superseded_write_epochs(monkeypatch):
    from types import SimpleNamespace
    writer = {'snapshot_epoch': 1}
    db = SimpleNamespace(info={}, flush=lambda: None,
                         connection=lambda: SimpleNamespace(info={'rota_write': writer}))
    monkeypatch.setattr(snapshots, 'record', lambda *_args, **_kw: {'id': 'workspace'})
    monkeypatch.setattr(snapshots, '_domain_snapshot', lambda *_args, **_kw: {'epoch': writer['snapshot_epoch']})
    for epoch in range(1, 5):
        writer['snapshot_epoch'] = epoch
        assert snapshots.domain_snapshot(db, object()) == {'epoch': epoch}
        assert len(db.info['write_domain_snapshots']) == 1
        assert snapshots.domain_snapshot(db, object()) == {'epoch': epoch}


@pytest.mark.parametrize('threshold,rss,reclaims', [(0, 2048, False), (512, 256, False), (512, 1024, True)])
def test_reclaims_only_after_complete_response_above_configured_threshold(monkeypatch, threshold, rss, reclaims):
    steps = []
    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'complete'})
        steps.append('dependency_cleanup')
    async def send(message):
        steps.append(message['type'])
    monkeypatch.setattr(memory, 'resident_bytes', lambda: rss * 1024 * 1024)
    monkeypatch.setattr(memory, 'release_memory', lambda: steps.append('reclaim'))
    asyncio.run(memory.MemoryReclamationMiddleware(app, threshold)({'type': 'http', 'path': '/api/workspaces'}, None, send))
    assert steps[:3] == ['http.response.start', 'http.response.body', 'dependency_cleanup']
    assert ('reclaim' in steps) is reclaims


def test_a_large_steady_process_reclaims_on_growth_not_after_every_response(monkeypatch):
    """A full collection holds the GIL; at a ~1 GiB working set it ran after every response (SW3 worker kills)."""
    rss, reclaims = [1024], []
    async def app(scope, receive, send):
        pass
    monkeypatch.setattr(memory, 'resident_bytes', lambda: rss[0] * 1024 * 1024)
    monkeypatch.setattr(memory, 'release_memory', lambda: reclaims.append(rss[0]))
    middleware = memory.MemoryReclamationMiddleware(app, 512)
    for value in (1024, 1024, 1100, 1500, 1600, 1100, 1500, 1700):
        rss[0] = value
        asyncio.run(middleware({'type': 'http', 'path': '/api/home'}, None, None))
    # 1024 reclaims (floor 1024); 1100/1500 < 1536; 1600 reclaims; the floor falls to 1100, so 1500 < 1612
    # waits and 1700 (grew 600 MiB from 1100) reclaims.
    assert reclaims == [1024, 1600, 1700]


def test_debug_probe_is_opt_in_and_contains_only_memory_and_job_identity(tmp_path, monkeypatch):
    path = tmp_path / 'memory.jsonl'
    monkeypatch.delenv('ROTA_MEMORY_PROBE_FILE', raising=False)
    process_memory.memory_probe('after_job', {'id': 'job', 'input': {'private': 'not logged'}})
    assert not path.exists()
    monkeypatch.setenv('ROTA_MEMORY_PROBE_FILE', str(path))
    process_memory.memory_probe('after_job', {'id': 'job', 'input': {'private': 'not logged'}})
    assert set(json.loads(path.read_text())) == {'stage', 'pid', 'rss_bytes', 'job_id'}


def test_reclamation_threshold_is_environment_backed_and_can_be_disabled(monkeypatch):
    monkeypatch.setenv('ROTA_MEMORY_RECLAIM_THRESHOLD_MB', '768')
    assert Settings.from_env().memory_reclaim_threshold_mb == 768
    replace(Settings(environment='test'), memory_reclaim_threshold_mb=0).check()
    with pytest.raises(RuntimeError, match='ROTA_MEMORY_RECLAIM_THRESHOLD_MB'):
        replace(Settings(environment='test'), memory_reclaim_threshold_mb=-1).check()


def test_completed_solver_callback_cycles_are_released():
    import weakref
    class SolverGraph:
        pass
    graph = SolverGraph()
    graph.callback = lambda held=graph: held
    reference = weakref.ref(graph)
    del graph
    process_memory.release_memory()
    assert reference() is None


@pytest.mark.parametrize('fails', [False, True])
def test_worker_releases_completed_resume_graph_on_success_or_failure(h, monkeypatch, fails):
    import weakref
    from apps.worker import main as worker
    workspace = h.workspace()
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                    revision=workspace['revision'])['job']
    references, reclaimed = [], []
    original_claim, original_release = worker.claim_job, worker._release_job_memory
    class ResumeGraph:
        pass
    def claim(*args, **kwargs):
        value = original_claim(*args, **kwargs)
        if value:
            graph = ResumeGraph()
            graph.callback = lambda held=graph: held
            value['resume_state'] = {'model': graph}
            references.append(weakref.ref(graph))
        return value
    def release(value):
        original_release(value)
        assert references and references[0]() is None
        reclaimed.append(value)
    def solve(*args, **kwargs):
        if fails:
            raise RuntimeError('Synthetic solver failure')
        return {'status': 'candidate_feasible', 'additions': [], 'removals': []}
    monkeypatch.setattr(worker, 'claim_job', claim)
    monkeypatch.setattr(worker, '_release_job_memory', release)
    assert worker.process_one(h.app.state.engine, scheduler=solve)
    assert reclaimed == [{'id': job['id']}]

from collections import Counter
from copy import deepcopy

from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.domain import cp_sat
from packages.domain.solver_control import Cancelled
from tests.scheduler_departments import department


def test_model_prepares_rule_boundaries_once_per_window(h, monkeypatch):
    snapshot, _ = department(h, 51, people=10, days=2, rules='full', clinics=2)
    intervals = cp_sat.scope_intervals
    calls = Counter()

    def counted(data, scope, person, start, end):
        calls[id(scope), start, end] += 1
        return intervals(data, scope, person, start, end)

    monkeypatch.setattr(cp_sat, 'scope_intervals', counted)
    def prepare_model():
        models = []
        result = cp_sat.generate(deepcopy(snapshot), 30, deterministic=True,
                                 _on_objectives=lambda model, _: models.append(str(model.proto)))
        assert result['status'] == 'model_ready'
        return models[0]

    cached_model = prepare_model()
    assert calls and set(calls.values()) == {1}
    cache = cp_sat.cache
    monkeypatch.setattr(cp_sat, 'cache', lambda fn: fn if fn.__name__ == 'rule_boundaries' else cache(fn))
    calls.clear()
    assert prepare_model() == cached_model
    assert max(calls.values()) > 1


def test_checkpoint_preparation_has_its_own_phase(h, monkeypatch):
    snapshot = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    clock = [0]
    monkeypatch.setattr(cp_sat.time, 'monotonic', lambda: clock[0])

    def checkpoint(_state):
        clock[0] += 2
        raise Cancelled()

    result = cp_sat.generate(snapshot, 30, deterministic=True, checkpoint=checkpoint)
    assert result['status'] == 'cancelled'
    assert result['phase_timings']['checkpoint_preparation_seconds'] == 2
    assert result['phase_timings']['preparation_seconds'] == 0


def test_worker_preserves_request_preparation_in_progress_and_result(h):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    payload = {'workspace_id': workspace['id'], 'kind': 'generation'}
    job = h.command('coordinator', 'job.create', payload, revision=workspace['revision'])['job']

    def scheduler(_snapshot, *, progress, cancelled):
        assert not cancelled()
        progress({'stage': 'preparing', 'phase_timings': {'preparation_seconds': 2}})
        return {'status': 'needs_more_time', 'phase_timings': {'preparation_seconds': 2}}

    assert h.process_jobs(scheduler=scheduler)
    with transaction(h.app.state.engine) as db:
        row = db.scalar(select(m.Job).where(m.Job.id == job['id']))
        assert row.status == 'succeeded'
        timings = row.result['phase_timings']
        assert timings['preparation_seconds'] == 2
        assert timings['request_preparation_seconds'] >= 0
        assert row.progress['phase_timings'] == timings

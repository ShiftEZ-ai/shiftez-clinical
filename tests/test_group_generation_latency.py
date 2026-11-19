"""Group-generation enqueue latency benchmark."""
from statistics import median
from time import perf_counter

import pytest

from .test_backend_scale import scale_department
from .test_planning_groups_drafts import group, settings


pytestmark = [pytest.mark.postgres, pytest.mark.wall_benchmark]

# Dev measured 0.797 s p95; this 2 s ceiling leaves runner variance while
# keeping the group-generation enqueue regression visible.
P95_WALL_BUDGET_SECONDS = 2.0
SAMPLES = 5


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


def _group_generation_samples(pg):
    workspace, data, _, _ = scale_department(pg, assigned=False)
    configured = group(pg, people=list(data['people']))
    settings(pg, group_lookahead='none')
    workspace = pg.command('admin', 'workspace.designate', {'reason': 'Synthetic group latency benchmark'},
                           workspace['id'], workspace['revision'])['workspace']
    samples = []
    for _ in range(SAMPLES):
        started = perf_counter()
        job = pg.command('coordinator', 'job.create', {
            'workspace_id': workspace['id'], 'kind': 'generation',
            'planning_group_version_id': configured['id'], 'lookahead': 'none'},
            revision=workspace['revision'])['job']
        elapsed = perf_counter() - started
        cancelled = pg.command('coordinator', 'job.cancel', {'reason': 'Cancel synthetic group latency sample'},
                               job['id'], job['revision'])['job']
        assert cancelled['status'] == 'cancelled'
        samples.append(elapsed)
    return workspace, samples


@pytest.fixture(scope='module')
def group_generation_latency(scale_harness):
    workspace, samples = _group_generation_samples(scale_harness('pg'))
    metrics = {'samples': samples, 'p50': median(samples), 'p95': _percentile(samples, 0.95)}
    print('group_generation_latency', metrics)
    return workspace, metrics


def test_group_generation_latency_shape(group_generation_latency):
    workspace, metrics = group_generation_latency
    assert len(metrics['samples']) == SAMPLES
    assert workspace['id']
    assert all(sample >= 0 for sample in metrics['samples'])


def test_group_generation_enqueue_stays_within_dev_budget(group_generation_latency):
    _, metrics = group_generation_latency
    assert metrics['p95'] < P95_WALL_BUDGET_SECONDS, metrics

"""Department-scale read preparation and lifecycle writer budgets."""
import json
import logging
from logging.handlers import BufferingHandler
import pytest
from apps.api import commands, models as m, snapshots
from apps.api.db import transaction
from apps.api.services import fairness
from tests.test_backend_scale import measure
from tests.test_fairness_goals_performance import command, lifecycle_case
from tests.test_performance_budget import python_budget

pytestmark = pytest.mark.postgres


def _assert_lifecycle_sql(metrics):
    for name, metric in metrics.items():
        assert sum(metric['sql'].values()) <= {'goals': 35, 'validate': 75, 'submit': 95, 'approve': 125}[name], (name, metric)


def test_lifecycle_and_fairness_at_department_scale(pg, monkeypatch, request):
    workspace = lifecycle_case(pg)
    snapshots_read = []
    original = snapshots._domain_snapshot
    def bounded_snapshot(db, *args, **kwargs):
        assert not db.connection().info.get('rota_write'), 'Do not rebuild canonical inputs under the writer'
        snapshots_read.append(1)
        return original(db, *args, **kwargs)
    monkeypatch.setattr(snapshots, '_domain_snapshot', bounded_snapshot)
    logger, captured = logging.getLogger('rota.db'), BufferingHandler(10000)
    level = logger.level
    logger.setLevel(logging.INFO); logger.addHandler(captured)
    metrics = {}
    try:
        for name, operation, target in [
            ('goals', lambda: pg.get('coordinator', '/api/fairness/goals?workspace_id=' + workspace['id']), (fairness, 'goals')),
            ('validate', lambda: command(pg, workspace, 'validate'), (commands, '_execute')),
            ('submit', lambda: command(pg, workspace, 'submit'), (commands, '_execute')),
            ('approve', lambda: command(pg, workspace, 'approve', 'approver'), (commands, '_execute')),
        ]:
            captured.buffer.clear()
            snapshots_read.clear()
            result, timing = measure(pg, 'lifecycle-' + name, operation, monkeypatch, target)
            metrics[name] = {**timing, 'writes': [r.duration_ms / 1000 for r in captured.buffer if r.msg == 'write_completed'],
                                       'snapshots': len(snapshots_read)}
            if name == 'goals':
                assert result['goals']
            else:
                workspace = result['workspace']
                assert workspace['status'] == {'validate': 'draft', 'submit': 'submitted', 'approve': 'approved'}[name]
    finally:
        logger.removeHandler(captured); logger.setLevel(level)
    request.node.user_properties.append(('lifecycle_read_scale', json.dumps(metrics)))
    print('lifecycle_read_scale', json.dumps(metrics))
    _assert_lifecycle_sql(metrics)
    with transaction(pg.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        assert row.submitted_fingerprint == snapshots.fingerprint(db, row)
    return locals()


@pytest.mark.cpu_budget
def test_lifecycle_and_fairness_cpu_budgets(pg, monkeypatch, request):
    state = test_lifecycle_and_fairness_at_department_scale(pg, monkeypatch, request)
    for name, timing in state['metrics'].items():
        assert timing['snapshots'] == 1, (name, timing)
        assert timing['cpu_seconds'] < python_budget(4 if name == 'goals' else 6), (name, timing)
        assert all(seconds < python_budget(2) for seconds in timing['writes']), (name, timing)


@pytest.mark.wall_benchmark
def test_lifecycle_and_fairness_wall_budgets(pg, monkeypatch, request):
    state = test_lifecycle_and_fairness_at_department_scale(pg, monkeypatch, request)
    for name, timing in state['metrics'].items():
        assert timing['wall_seconds'] < python_budget(6 if name == 'goals' else 8), (name, timing)

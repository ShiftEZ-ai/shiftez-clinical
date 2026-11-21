"""Department-scale score-review read budgets."""
from collections import Counter
from time import process_time

import pytest
from sqlalchemy import event

from apps.api.services import score_review
from tests.test_backend_scale import scale_department
from tests.test_performance_budget import python_budget


@pytest.fixture
def department_score_review_measurement(h, monkeypatch):
    workspace, _, _, _ = scale_department(h, assigned=True)
    workspace = h.command('coordinator', 'workspace.validate',
                          {'reason': 'Build department-scale score review evidence'},
                          workspace['id'], workspace['revision'])['workspace']
    calls = Counter()
    for name in ('evaluate', 'schedule_score'):
        original = getattr(score_review, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            calls[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(score_review, name, counted)

    statements = Counter()

    def count_sql(_connection, _cursor, statement, _parameters, _context, _many):
        statements[statement.split()[0].upper()] += 1

    event.listen(h.app.state.engine, 'before_cursor_execute', count_sql)
    started = process_time()
    try:
        response = h.client('coordinator').get(f"/api/workspaces/{workspace['id']}/score-review")
    finally:
        elapsed = process_time() - started
        event.remove(h.app.state.engine, 'before_cursor_execute', count_sql)
    return response, calls, statements, elapsed


def test_department_score_review_reuses_current_validation_and_bounds_queries(department_score_review_measurement):
    response, calls, statements, _ = department_score_review_measurement
    assert response.status_code == 200, response.text
    assert not calls, calls
    assert statements['SELECT'] <= 31, statements


@pytest.mark.cpu_budget
def test_department_score_review_elapsed_budget(department_score_review_measurement):
    _, _, statements, elapsed = department_score_review_measurement
    assert elapsed < python_budget(3), (elapsed, statements)

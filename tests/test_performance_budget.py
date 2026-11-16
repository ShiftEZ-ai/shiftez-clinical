"""Shared tracing allowance for pure-Python scale guards; native solver budgets stay separate."""
import ast
import os
from pathlib import Path

import coverage
import pytest


def python_budget(seconds):
    """Keep untraced ceilings; use the measured ~7x rule-map/dynamic-catalogue tracing allowance."""
    return seconds * (7 if coverage.Coverage.current() or os.getenv('SCALE_CPROFILE') else 1)


@pytest.mark.parametrize('filename,function,condition', [
    ('test_backend_scale.py', 'test_department_interactive_paths', "sum(measured['sql'].values()) <= sql_budgets[name]"),
    ('test_scheduler_performance.py', 'test_month_for_eighty_people_builds_a_compact_model_and_validates_quickly', 'len(preparation) == 1'),
    ('test_command_write_timeouts.py', 'test_sqlite_held_writer_times_out_without_running_operation', 'not calls'),
    ('test_count_bucket_budget.py', 'test_maximum_bucket_line_at_department_scale', 'peak < 16 * 1024 * 1024'),
    ('test_person_score.py', 'test_person_score_active_opportunities_department_query_and_time_budget', 'len(count) <= 90'),
    ('test_quota_counting_periods.py', 'test_counting_window_expansion_cpu_is_bounded_for_a_department_year', 'len(policies) == 150 * 53'),
    ('test_quota_repeating_validity.py', 'test_far_away_fixed_anchor_expands_only_workspace_windows_within_cpu_budget', 'len(policies) <= 150 * 6'),
    ('test_requests_unified.py', 'test_department_scale_page_is_fast_and_small', 'size < 200_000'),
    ('test_rule_catalogue_payload_performance.py', 'test_department_preview_and_proposal_are_summaries_with_full_evidence_on_demand', 'len(response.content) <= 300_000'),
    ('test_qualification_preflight.py', 'test_timeout_terminates_owned_descendant_and_preserves_unowned_process', 'descendant_dead'),
])
def test_mixed_work_and_execution_bounds_stay_in_core_tests(filename, function, condition):
    tree = ast.parse(Path(__file__).with_name(filename).read_text(encoding='utf-8'))
    target = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function)
    assert not any(ast.unparse(decorator).startswith('pytest.mark.cpu_budget')
                   for decorator in target.decorator_list)
    remaining = {ast.dump(node.test) for node in ast.walk(target) if isinstance(node, ast.Assert)}
    assert ast.dump(ast.parse(condition, mode='eval').body) in remaining


def test_owned_descendant_timeout_budget_stays_in_marked_test():
    tree = ast.parse(Path(__file__).with_name('test_qualification_preflight.py').read_text(encoding='utf-8'))
    target = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'test_owned_descendant_timeout_cpu_budget')
    assert any(ast.unparse(decorator) == 'pytest.mark.wall_benchmark' for decorator in target.decorator_list)
    assert any(isinstance(node, ast.Assert) and ast.dump(node.test) ==
               ast.dump(ast.parse('terminated_probe[-1] < 4', mode='eval').body) for node in ast.walk(target))


def test_nightly_greedy_keeps_180_second_traced_ceiling(monkeypatch):
    from tests.test_scheduler_performance import _budget
    monkeypatch.setattr(coverage.Coverage, 'current', lambda: True)
    actual = _budget(60)
    assert actual == 180


@pytest.mark.parametrize('traced,profiled,factor', [(False, False, 1), (True, False, 7),
                                                 (False, True, 7), (True, True, 7)])
def test_python_budget_keeps_calibrated_regression_headroom(monkeypatch, traced, profiled, factor):
    monkeypatch.setattr(coverage.Coverage, 'current', lambda: traced)
    monkeypatch.delenv('SCALE_CPROFILE', raising=False)
    if profiled:
        monkeypatch.setenv('SCALE_CPROFILE', '1')
    for baseline, ceiling in ((0.35, 1), (0.8, 1.25)):
        actual = python_budget(ceiling)
        assert actual == ceiling * factor
        assert baseline * factor < actual < baseline * factor * 4

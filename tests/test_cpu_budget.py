"""Keep timing budgets isolated and operation counts in the core gate."""
import ast
from pathlib import Path
import re

import pytest


COUNTER = re.compile(r'(^|_)(queries|query|statements?|calls?|reads?|count|counter)($|_)')
CLOCKS = {'process_time', 'process_time_ns', 'perf_counter', 'perf_counter_ns',
          'thread_time', 'thread_time_ns', 'monotonic', 'monotonic_ns', 'time'}
TIMING_VALUE = re.compile(r'(^|_)(elapsed|cpu|wall|timings?|times|seconds|took|built)($|_)')


@pytest.fixture(scope='session')
def parsed_test_modules():
    modules = []
    for path in Path(__file__).parent.glob('test_*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        modules.append((path, tree, functions))
    providers = {}
    exports = {path.stem: {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
               | {name.asname or name.name for node in tree.body if isinstance(node, ast.ImportFrom) for name in node.names}
               | {target.id for node in tree.body if isinstance(node, ast.Assign) for target in node.targets if isinstance(target, ast.Name)}
               for path, tree, _ in modules}
    while True:
        updated = {path.stem: timing_aliases(tree, providers=providers) & exports[path.stem] for path, tree, _ in modules}
        if updated == providers:
            break
        providers = updated
    modules = [(path, (timing_aliases(tree, providers=providers), providers), functions) for path, tree, functions in modules]
    return modules


def reported_search_budget(node):
    """The reported remaining-budget identity is a functional contract, not a measured runtime ceiling."""
    if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant) and node.left.value == 0
            and len(node.ops) == 2 and isinstance(node.ops[0], ast.Lt) and isinstance(node.ops[1], ast.LtE)):
        return False
    limit, remaining = node.comparators
    if not (isinstance(limit, ast.Subscript) and isinstance(limit.slice, ast.Constant)
            and limit.slice.value == 'time_limit_seconds' and isinstance(remaining, ast.BinOp)
            and isinstance(remaining.op, ast.Sub) and isinstance(remaining.left, ast.Constant)
            and isinstance(remaining.left.value, (int, float)) and remaining.left.value > 0):
        return False
    start = remaining.right
    return (isinstance(start, ast.Subscript) and isinstance(start.slice, ast.Constant)
            and start.slice.value == 'search_started_seconds' and ast.dump(start.value) == ast.dump(limit.value))


def timing_aliases(tree, aliases=(), providers=None):
    """Follow clocks through real helper definitions/imports, not common helper names."""
    providers = providers or {}
    aliases = set(aliases) | CLOCKS | {'time.' + clock for clock in CLOCKS}
    body = getattr(tree, 'body', [])
    definitions = [node for node in body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for node in body:
        if isinstance(node, ast.ImportFrom):
            clocks = CLOCKS if node.module == 'time' else providers.get((node.module or '').rsplit('.', 1)[-1], ())
            aliases.update(value.asname or value.name for value in node.names if value.name in clocks)
        elif isinstance(node, ast.Import):
            for value in node.names:
                clocks = CLOCKS if value.name == 'time' else providers.get(value.name.rsplit('.', 1)[-1], ())
                aliases.update((value.asname or value.name) + '.' + clock for clock in clocks)
    # A local definition shadows an imported function with the same name.
    aliases.difference_update(node.name for node in definitions)
    while True:
        previous = aliases.copy()
        for node in definitions:
            if any(isinstance(value, ast.Call) and ast.unparse(value.func) in aliases for value in ast.walk(node)):
                aliases.add(node.name)
        for node in body:
            if (isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
                    and isinstance(node.value, (ast.Name, ast.Attribute)) and ast.unparse(node.value) in aliases):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                aliases.update(target.id for target in targets if isinstance(target, ast.Name))
        if aliases == previous:
            return aliases


def timing_assertions(tree, aliases=(), providers=None):
    """Catch direct clocks/helpers, elapsed variables and measured metric ceilings."""
    aliases = timing_aliases(tree, aliases, providers)
    clock_values = set()
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None]
    while True:
        previous = clock_values.copy()
        for node in assignments:
            if any((isinstance(value, ast.Call) and
                    ast.unparse(value.func) in aliases)
                   or (isinstance(value, ast.Name) and value.id in clock_values) for value in ast.walk(node.value)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                clock_values.update(value.id for target in targets for value in ast.walk(target) if isinstance(value, ast.Name))
        if clock_values == previous:
            break

    def measured(value):
        if isinstance(value, ast.Call):
            name = ast.unparse(value.func)
            if name in aliases:
                return True
            if name in {'len', 'isinstance', 'type'}:
                return False
        if isinstance(value, ast.Subscript):
            # A timing report may also carry correctness fields such as row counts.
            key = value.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                return bool(TIMING_VALUE.search(key.value)) or key.value in {'pin', 'highlight'}
        if isinstance(value, ast.Name) and (TIMING_VALUE.search(value.id) or value.id in clock_values):
            return True
        return any(measured(child) for child in ast.iter_child_nodes(value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            direct = any(isinstance(value, ast.Call) and
                         ast.unparse(value.func) in aliases for value in ast.walk(node.test))
            ceiling = any(isinstance(value, ast.Compare) and
                          any(isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for op in value.ops) and
                          not reported_search_budget(value) and
                          not (all(isinstance(op, ast.GtE) for op in value.ops) and
                               all(isinstance(v, ast.Constant) and v.value == 0 for v in value.comparators)) and
                          measured(value) for value in ast.walk(node.test))
            if direct or ceiling:
                yield node.lineno


def test_unmarked_tests_do_not_assert_timing_budgets(parsed_test_modules):
    failures = []
    for path, (aliases, providers), functions in parsed_test_modules:
        for node in functions:
            if node.name.startswith('test_') and not any(
                    ast.unparse(decorator) in {'pytest.mark.cpu_budget', 'pytest.mark.wall_benchmark'}
                    for decorator in node.decorator_list):
                failures.extend(f'{path.name}:{line} ({node.name})' for line in timing_assertions(node, aliases, providers))
    assert not failures, 'Move timing assertions into cpu_budget or wall_benchmark tests: ' + ', '.join(failures)


def counter_assertions(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            for value in ast.walk(node.test):
                name = value.id if isinstance(value, ast.Name) else value.attr if isinstance(value, ast.Attribute) else ''
                if COUNTER.search(name) or (isinstance(value, ast.Constant) and value.value == 'sql'):
                    yield node.lineno
                    break
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr.startswith(('assert_called', 'assert_not_called', 'assert_has_calls', 'assert_any_call')):
                yield node.lineno


def test_cpu_budget_tests_do_not_assert_operation_counts(parsed_test_modules):
    failures = []
    for path, _imports, functions in parsed_test_modules:
        for node in functions:
            if any(
                    ast.unparse(decorator) in {'pytest.mark.cpu_budget', 'pytest.mark.wall_benchmark'}
                    for decorator in node.decorator_list):
                failures.extend(f'{path.name}:{line} ({node.name})' for line in counter_assertions(node))
    assert not failures, 'Keep operation-count assertions unmarked in core tests: ' + ', '.join(failures)


@pytest.mark.parametrize('expression', [
    'len(queries) <= 20', 'len(statements) <= 32', 'outcome_calls == [1]',
    'len(day_calls) < 10', 'reader.call_count == 1', "metrics['sql']['SELECT'] < 25",
    "sum('snapshot' in statement for statement in statements) == 1",
])
def test_counter_guard_detects_count_assertions(expression):
    assert list(counter_assertions(ast.parse('assert ' + expression))) == [1]


def test_counter_guard_allows_timing_assertions_and_catches_mock_checks():
    assert not list(counter_assertions(ast.parse('assert elapsed < budget')))
    assert list(counter_assertions(ast.parse('reader.assert_called_once()'))) == [1]


@pytest.mark.parametrize('expression', [
    'time.process_time() - started < 2', 'perf_counter() - began < 5',
    'time.thread_time() < limit', 'time.monotonic() - started >= 4.5',
    'elapsed < _budget(60)', "metrics['cpu_seconds'] < python_budget(1)",
    "metrics['wall_seconds'] < 5", 'min(times) < 2', 'took < budget',
    "timings['pin'] < 2500", 'measure(h, "read", operation) < 5',
    'checked(definition)[0] < 2', '_run(data)[1] < 150', 'built < 5000',
    "u['search_started_seconds'] < 2", "u['time_limit_seconds'] < 10",
    "0 < u['time_limit_seconds'] <= 30 - other['search_started_seconds']",
    "0 < u['time_limit_seconds'] <= time.monotonic() - u['search_started_seconds']",
])
def test_timing_guard_detects_budget_assertions(expression):
    helpers = ('def measure(*args): return time.perf_counter()\n'
               'def checked(*args): return time.perf_counter(), None\n'
               'def _run(*args): return None, time.process_time()\n')
    assert list(timing_assertions(ast.parse(helpers + 'assert ' + expression))) == [4]


@pytest.mark.parametrize('name', ['checked', '_run', 'measure'])
def test_timing_guard_does_not_treat_semantic_helpers_as_timers(name):
    source = f'def {name}(data): return staffing.check_people(data)[0]\nassert {name}(data)["supervisor_id"] == person_id'
    assert not list(timing_assertions(ast.parse(source)))


def test_timing_guard_resolves_imported_helper_provenance_and_aliases():
    timed = timing_aliases(ast.parse('def checked(data): return time.perf_counter()'))
    semantic = timing_aliases(ast.parse('def checked(data): return staffing.check_people(data)[0]'))
    source = 'from provider import checked as verify\nassert verify(data) < 2'
    assert list(timing_assertions(ast.parse(source), providers={'provider': timed})) == [2]
    assert not list(timing_assertions(ast.parse(source), providers={'provider': semantic}))
    qualified = 'import provider as observed\nassert observed.checked(data) < 2'
    assert list(timing_assertions(ast.parse(qualified), providers={'provider': timed})) == [2]
    assert not list(timing_assertions(ast.parse(qualified), providers={'provider': semantic}))


def test_timing_guard_preserves_local_timer_wrappers_and_function_aliases():
    source = ('from time import perf_counter as clock\n'
              'def checked(data): return clock()\n'
              'def wrapped(data): return checked(data)\n'
              'observed = wrapped\nassert observed(data) < 2')
    assert list(timing_assertions(ast.parse(source))) == [5]


def test_timing_guard_local_semantic_definition_shadows_a_timer_import():
    timed = timing_aliases(ast.parse('def checked(data): return time.perf_counter()'))
    source = ('from provider import checked\n'
              'def checked(data): return staffing.check_people(data)[0]\n'
              'assert checked(data)["supervisor_id"] == person_id')
    assert not list(timing_assertions(ast.parse(source), providers={'provider': timed}))


def test_timing_guard_detects_imported_clock_alias():
    source = 'from time import perf_counter as clock\nassert clock() - began < 2'
    assert list(timing_assertions(ast.parse(source))) == [2]


def test_timing_guard_detects_clock_values_with_arbitrary_names():
    source = 'from time import thread_time as clock\nvalue = clock() - began\nresult = value\nassert result < 2'
    assert list(timing_assertions(ast.parse(source))) == [4]


def test_timing_guard_detects_class_method_assertions():
    tree = ast.parse('class TestExample:\n def test_budget(self):\n  assert time.perf_counter() - began < 2')
    assert list(timing_assertions(tree)) == [3]


@pytest.mark.parametrize('expression', [
    'len(queries) <= 20', "timings['rows'] <= 45", 'len(preparation) == 1',
    "result['phase_timings']['search_seconds'] == 0", "result['phase_timings']['diagnostic_seconds'] >= 0",
    "isinstance(result['elapsed_ms'], int)", 'actual == ceiling * factor',
    "all(0 < u['time_limit_seconds'] <= 30 - u['search_started_seconds'] and u['search_started_seconds'] >= 0 for u in searching)",
])
def test_timing_guard_allows_correctness_assertions(expression):
    # Zero/positive reported durations describe the result contract, rather than a runtime budget.
    tree = ast.parse('assert ' + expression)
    assert not list(timing_assertions(tree))


def test_department_preparation_cache_correctness_stays_in_blocking_cpu_lane():
    tree = ast.parse(Path('tests/test_solver_search_budget.py').read_text(encoding='utf-8'))
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == 'test_department_preparation_cache_measurement')
    markers = {ast.unparse(decorator) for decorator in node.decorator_list}
    assert 'pytest.mark.cpu_budget' in markers and 'pytest.mark.wall_benchmark' not in markers


@pytest.mark.parametrize('module_name,fixture_name', [
    ('test_rule_lines_scale', 'department_rule_checks'),
    ('test_home_request_summary', 'personal_home_summary_scale'),
])
def test_operation_budget_fixtures_read_process_cpu(module_name, fixture_name, monkeypatch):
    import importlib
    from types import SimpleNamespace
    module = importlib.import_module('tests.' + module_name)
    ticks = iter([0.0, 0.3])
    walls = iter([0.0, 30_000.0])
    monkeypatch.setattr(module, 'time', SimpleNamespace(process_time=lambda: next(ticks),
                                                       perf_counter=lambda: next(walls)))
    if module_name == 'test_rule_lines_scale':
        monkeypatch.setattr(module, 'R', SimpleNamespace(**{name: lambda *args: [] for name in
                            ('duplicates', 'outranking_by_number', 'both_apply', 'inherited', 'attention')}))
        monkeypatch.setattr(module, 'department_rule', lambda: ({'definition': {'rule': {}}}, {}))
        measured, _ = getattr(module, fixture_name).__wrapped__()
        assert measured == 0.3
    else:
        monkeypatch.setattr(module, 'big_department', lambda h: ['person'])
        monkeypatch.setattr(module, 'identity', lambda *args: None)
        harness = SimpleNamespace(get=lambda *args: {'my_requests': []})
        _, _, measured, wall = getattr(module, fixture_name).__wrapped__(SimpleNamespace(param='h'), lambda name: harness)
        assert measured == 0.3 / 3
        assert wall == 10_000


def test_rule_map_cpu_budget_excludes_descheduled_time():
    from tests.test_rule_map import test_large_department_mapping_time
    test_large_department_mapping_time((None, None, 10_000, 0.1))

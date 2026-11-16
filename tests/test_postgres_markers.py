"""Database-free test selection excludes every test requiring disposable PostgreSQL."""
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
COLLECTION_AUDIT = """
import ast
from functools import cache
import inspect
import json
from pathlib import Path
import sys
import textwrap
import pytest
from scripts import affected_tests as selector

@cache
def selects_runtime_fixture(function):
    source = ast.parse(textwrap.dedent(inspect.getsource(function)))
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == 'getfixturevalue' for node in ast.walk(source))

def postgres_parameter(item):
    parameters = getattr(getattr(item, 'callspec', None), 'params', {})
    return any(isinstance(value, str) and value in {'pg', 'postgres', 'postgresql'}
               for value in parameters.values())

def needs_postgres(item):
    if {'pg', 'postgres_worker_url', 'postgres_template'} & set(item.fixturenames):
        return True
    callspec = getattr(item, 'callspec', None)
    if (any(getattr(callspec, 'params', {}).get(name) == 'postgresql' for name in ('h', 'harness'))
            or ('postgresql' in getattr(callspec, 'id', '').split('-') and postgres_parameter(item))):
        return True
    # Dynamic fixture lookup is absent from pytest's static fixture closure. Inspect
    # the selected test and fixtures, so a plain config value such as image='postgres'
    # is never treated as a database dependency merely because of its spelling.
    if not postgres_parameter(item):
        return False
    functions = [item.obj, *(definitions[-1].func for definitions in
                            item._fixtureinfo.name2fixturedefs.values() if definitions)]
    return any(selects_runtime_fixture(function) for function in functions)

def missing_postgres_markers(items):
    return [item.nodeid for item in items if needs_postgres(item)
            and any(item.get_closest_marker(name) is None for name in ('postgres', 'postgresql'))]

def core_collection_gaps(items, targets):
    missing, quarantined, excluded = [], [], []
    for target in targets:
        matches = [item for item in items if item.nodeid == target
                   or item.nodeid.startswith(target + '[') or item.nodeid.startswith(target + '::')]
        if not matches:
            missing.append(target)
        for item in matches:
            if item.get_closest_marker('quarantine') is not None:
                quarantined.append(item.nodeid)
            if (item.get_closest_marker('browser') is not None) != (selector.suite(target) == 'browser'):
                excluded.append(item.nodeid)
    return {'missing_core': sorted(set(missing)), 'quarantined_core': sorted(set(quarantined)),
            'excluded_core': sorted(set(excluded))}

class Audit:
    def pytest_collection_finish(self, session):
        dependent = [item for item in session.items if needs_postgres(item)]
        missing = missing_postgres_markers(dependent)
        dynamic = [item for item in dependent
                   if not {'pg', 'postgres_worker_url', 'postgres_template'} & set(item.fixturenames)]
        config_only = {item.nodeid: item.get_closest_marker('postgres') is not None
                       for item in session.items if postgres_parameter(item) and not needs_postgres(item)}
        targets = (*selector.SAFETY_CORE, *selector.WORK_CORE,
                   *(target for group in selector.SMOKE.values() for target in group))
        core = core_collection_gaps(session.items, targets)
        Path(sys.argv[1]).write_text(json.dumps({'dependent': len(dependent), 'dynamic': len(dynamic),
                                               'config_only': config_only, 'missing': missing, **core}))

raise SystemExit(pytest.main(['--collect-only', '-q', 'tests', '-p', 'no:cacheprovider'], plugins=[Audit()]))
"""


def test_postgres_fixture_dependencies_have_selectable_marker(tmp_path):
    report = tmp_path / 'postgres-markers.json'
    collected = subprocess.run([sys.executable, '-c', COLLECTION_AUDIT, str(report)], cwd=ROOT,
                               env={**os.environ, 'PYTEST_ADDOPTS': ''}, capture_output=True, text=True)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    result = json.loads(report.read_text())
    assert result['dependent'] > 0, 'The audit must collect the real PostgreSQL-dependent tests'
    assert result['dynamic'] > 0, 'The audit must include request.getfixturevalue backend selections'
    oracle = 'tests/test_ci_changes.py::test_pin_consistency_rejects_stale_tag_only_copies[postgres]'
    assert result['config_only'].get(oracle) is False, 'A configuration-only PostgreSQL value stays selectable without a database'
    assert result['missing'] == [], 'Tests requiring PostgreSQL need postgres and postgresql markers:\n' + '\n'.join(result['missing'])

    assert result['missing_core'] == [], 'Mandatory safety/work targets were not collected:\n' + '\n'.join(result['missing_core'])
    assert result['quarantined_core'] == [], 'Mandatory safety/work cases cannot silently quarantine:\n' + '\n'.join(result['quarantined_core'])
    assert result['excluded_core'] == [], 'Mandatory safety/work cases must match their execution marker:\n' + '\n'.join(result['excluded_core'])

    probe = tmp_path / 'probe'
    probe.mkdir()
    (probe / 'conftest.py').write_text('''
import json
from pathlib import Path
from tests.conftest import pytest_collection_modifyitems

def pytest_deselected(items):
    Path(__file__).with_name('deselected.json').write_text(json.dumps({
        item.name: [mark.name for mark in item.iter_markers()] for item in items}))

def pytest_collection_finish(session):
    Path(__file__).with_name('selected.json').write_text(json.dumps([item.name for item in session.items]))
''')
    (probe / 'test_probe.py').write_text('''
import pytest

@pytest.fixture
def h(request):
    return request.param

@pytest.fixture
def pg():
    pytest.fail('Collection must never set up PostgreSQL')

@pytest.fixture
def postgres_worker_url():
    pytest.fail('Collection must never set up PostgreSQL')

@pytest.fixture
def postgres_template():
    pytest.fail('Collection must never set up PostgreSQL')

@pytest.fixture
def inherited(pg):
    return pg

@pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True)
def test_h(h): pass
@pytest.mark.parametrize('backend', ['sqlite', 'pg'], ids=['sqlite', 'postgresql'])
def test_backend(backend): pass
@pytest.mark.parametrize('h', ['postgresql'], indirect=True, ids=['remote'])
def test_custom_id(h): pass
def test_pg(pg): pass
def test_transitive(inherited): pass
def test_worker(postgres_worker_url): pass
def test_template(postgres_template): pass
@pytest.mark.postgres
def test_legacy(): pass
@pytest.mark.postgresql
def test_alias(): pass
@pytest.mark.parametrize('image', ['postgres'])
def test_configuration_only(image): pass
@pytest.mark.parametrize('dialect', [object()], ids=['postgresql'])
def test_dialect_only(dialect): pass
''')
    collected = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q', str(probe),
                               '-c', str(ROOT / 'pyproject.toml'), '--confcutdir', str(probe),
                               '-p', 'no:cacheprovider', '-m', 'not postgresql'], cwd=ROOT,
                              env={**os.environ, 'PYTEST_ADDOPTS': ''}, capture_output=True, text=True)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    assert set(json.loads((probe / 'selected.json').read_text())) == {
        'test_h[sqlite]', 'test_backend[sqlite]', 'test_configuration_only[postgres]', 'test_dialect_only[postgresql]'}
    deselected = json.loads((probe / 'deselected.json').read_text())
    assert set(deselected) == {'test_h[postgresql]', 'test_backend[postgresql]', 'test_pg', 'test_transitive',
                               'test_worker', 'test_template', 'test_legacy', 'test_alias', 'test_custom_id[remote]'}
    assert all({'postgres', 'postgresql'} <= set(markers) for markers in deselected.values())


def test_core_collection_guard_detects_missing_quarantined_and_wrong_suite_cases():
    # Synthetic collection items exercise the real audit function, without executing product tests.
    from types import SimpleNamespace
    namespace = {}
    exec(COLLECTION_AUDIT.split('class Audit:', 1)[0], namespace)
    guard, selector = namespace['core_collection_gaps'], namespace['selector']
    target = selector.SAFETY_CORE[0]
    def item(markers=()):
        return SimpleNamespace(nodeid=target + '::test_fixture_case',
                               get_closest_marker=lambda name: object() if name in markers else None)
    assert guard([item()], [target]) == {'missing_core': [], 'quarantined_core': [], 'excluded_core': []}
    assert guard([], [target])['missing_core'] == [target]
    assert guard([item(('quarantine',))], [target])['quarantined_core'] == [item().nodeid]
    assert guard([item(('browser',))], [target])['excluded_core'] == [item().nodeid]
    node = next(target for target in selector.SAFETY_CORE if '::' in target and selector.suite(target) == 'unit')
    parameter = SimpleNamespace(nodeid=node + '[postgres]', get_closest_marker=lambda name: None)
    assert guard([parameter], [node])['missing_core'] == []

    unmarked = SimpleNamespace(nodeid='test_h[postgresql]', fixturenames=[],
                               callspec=SimpleNamespace(params={'h': 'postgresql'}, id='postgresql'),
                               get_closest_marker=lambda name: None)
    assert namespace['missing_postgres_markers']([unmarked]) == [unmarked.nodeid]
    unmarked.get_closest_marker = lambda name: object() if name == 'postgres' else None
    assert namespace['missing_postgres_markers']([unmarked]) == [unmarked.nodeid]
    unmarked.get_closest_marker = lambda name: object()
    assert namespace['missing_postgres_markers']([unmarked]) == []

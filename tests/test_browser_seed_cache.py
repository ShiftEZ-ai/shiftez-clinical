"""Non-browser proof: configured snapshot reuse preserves DB, result and login isolation."""
from contextlib import closing
import sqlite3
import importlib
import pytest
from types import SimpleNamespace

from tests.conftest import sqlite_harness
from tests.browser_seed import capture_state
from tests.browser_seed import authenticated


def test_seed_reuse_clones_data_and_never_overwrites_composed_setup(tmp_path, database_template, scenario_cache):
    calls = []
    def build(h, name='A'):
        calls.append(name)
        w = h.workspace(name=name)
        return {'workspace': w, 'nested': []}
    def harness(name):
        path = tmp_path / name
        path.mkdir()
        return sqlite_harness(path, database_template)
    first = harness('one')
    h = next(first)
    result = scenario_cache(h, build)
    result['nested'].append('private mutation')
    original = result['workspace']['id']
    with closing(sqlite3.connect(h.settings.database_url.removeprefix('sqlite:///'))) as db:
        db.execute("UPDATE workspaces SET name='Changed' WHERE id=?", (original,))
        db.commit()
    second = harness('two')
    other = next(second)
    restored = scenario_cache(other, build)
    assert calls == ['A'] and restored['nested'] == []
    assert other.detail(original)['workspace']['name'] == 'A'
    assert other.browser_auth is not h.browser_auth
    scenario_cache(other, build, (), {'name': 'B'})
    assert calls == ['A', 'B']
    first.close()
    second.close()


def test_auth_reuse_validates_real_session_and_clears_invalid_cookies():
    calls = []
    context = SimpleNamespace(add_cookies=lambda v: calls.append(v), clear_cookies=lambda: calls.append('clear'))
    context.request = SimpleNamespace(get=lambda url: SimpleNamespace(ok=True))
    assert not authenticated(context, 'http://fixture', {}) and not calls
    assert authenticated(context, 'http://fixture', {'session': 'synthetic'})
    context.request.get = lambda url: SimpleNamespace(ok=False)
    assert not authenticated(context, 'http://fixture', {'session': 'expired'})
    assert calls[-1] == 'clear'


@pytest.mark.parametrize('module,name', [('test_browser_bulk_changes','permission_overlap'),
    ('test_browser_rule_catalogue_v2','converted_world'), ('test_browser_bulk_sheet','walk_in'),
    ('test_browser_person_profile','related'), ('test_browser_overflow','adversarial'), ('test_browser_sticky','busy_draft')])
def test_audited_browser_roots_can_be_snapshotted_without_chromium(tmp_path, database_template, scenario_cache, module, name):
    build = getattr(importlib.import_module('tests.' + module), name).__wrapped__
    generators = []
    results = []
    states = []
    counts = []
    calls = []
    def observed(h):
        calls.append(True)
        return build(h)
    try:
        for index in range(2):
            path = tmp_path / str(index)
            path.mkdir()
            fixture = sqlite_harness(path, database_template)
            generators.append(fixture)
            h = next(fixture)
            results.append(scenario_cache(h, observed))
            states.append(capture_state(h))
            with closing(sqlite3.connect(h.settings.database_url.removeprefix('sqlite:///'))) as db:
                tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                counts.append({table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables})
        assert results[0] == results[1] and states[0] == states[1] and counts[0] == counts[1] and len(calls) == 1
    finally:
        for fixture in generators: fixture.close()

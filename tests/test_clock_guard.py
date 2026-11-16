"""Date guard: the app, its domain code and the tests read one clock, packages/domain/time.now, which the tests pin
(tests/conftest.py TEST_NOW) so fixtures with fixed dates never drift into the past as real days pass. Reading the
machine clock directly (datetime.now, date.today, datetime.utcnow) fails here; a line that must use it (e.g. a
third-party library that checks against the machine clock) says so with "real clock" in a comment.
"""
import ast
from collections import Counter
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MACHINE_CLOCK = re.compile(r"\bdatetime\.(now|utcnow|today)\(|\bdate\.today\(")
SEAM = ROOT / "packages" / "domain" / "time.py"

# Unauthenticated readiness age and worker retry dates only. Remove allowances with their patches;
# exact counts make the baseline shrink rather than leave permission to reintroduce a removed patch.
MODEL_CLOCK_EXCEPTIONS = {
    ('test_readiness.py', 'test_only_service_readiness_requires_a_recent_worker_using_the_deployment_threshold'): 1,
    ('test_worker_outbox.py', 'test_batch_failure_isolated_and_retry_due_time_respected_postgres'): 1,
}


def model_now_patches(source):
    found = []
    tree = ast.parse(source)
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not ((isinstance(node.func, ast.Attribute) and node.func.attr == 'setattr')
                or (isinstance(node.func, ast.Name) and node.func.id == 'setattr')):
            continue
        target = ast.unparse(node.args[0])
        field = node.args[1] if len(node.args) > 1 else None
        if ((target.split('.')[-1] in ('m', 'models') and isinstance(field, ast.Constant) and field.value == 'now')
                or (isinstance(node.args[0], ast.Constant) and node.args[0].value == 'apps.api.models.now')):
            parents = [function for function in functions if function.lineno <= node.lineno <= function.end_lineno]
            tests = [function for function in parents if function.name.startswith('test_')]
            function = min(tests or parents, key=lambda function: function.lineno).name if parents else '<module>'
            found.append((function, node.lineno))
    return found


@pytest.mark.parametrize('source,forbidden', [
    ('monkeypatch.setattr(m, "now", clock)', True),
    ("patch.setattr(models, 'now', clock)", True),
    ('monkeypatch.setattr(feedback.m, "now", clock)', True),
    ('monkeypatch.setattr("apps.api.models.now", clock)', True),
    ('setattr(models, "now", clock)', True),
    ('monkeypatch.setattr(m, "clock_now", clock)', False),
    ('monkeypatch.setattr(auth, "now", clock)', False),
    ('freeze_clock(monkeypatch, moment)', False),
])
def test_partial_clock_guard_recognises_direct_and_string_patches(source, forbidden):
    assert bool(model_now_patches(source)) is forbidden


def test_api_tests_freeze_the_shared_clock_instead_of_replacing_models_now():
    found = Counter()
    for path in (ROOT / 'tests').rglob('*.py'):
        if path == ROOT / 'tests' / 'clock_support.py':
            continue
        found.update((path.relative_to(ROOT / 'tests').as_posix(), function)
                     for function, _ in model_now_patches(path.read_text(encoding='utf-8')))
    assert dict(found) == MODEL_CLOCK_EXCEPTIONS, (
        'Use tests.clock_support.freeze_clock for new patches; remove allowances when a legacy patch is removed. '
        f'Unexpected patches: {dict(found - Counter(MODEL_CLOCK_EXCEPTIONS))}; '
        f'stale allowances: {dict(Counter(MODEL_CLOCK_EXCEPTIONS) - found)}')


@pytest.mark.parametrize('change', ['new_test', 'new_helper', 'removed_patch', 'reintroduced_patch'])
def test_model_clock_allowlist_is_a_shrinking_ratchet(tmp_path, monkeypatch, change):
    folder = tmp_path / 'tests'
    folder.mkdir()
    path = folder / 'clock_cases.py'
    original = 'def test_legacy():\n    monkeypatch.setattr(m, "now", clock)\n'
    path.write_text(original, encoding='utf-8')
    monkeypatch.setitem(globals(), 'ROOT', tmp_path)
    monkeypatch.setitem(globals(), 'MODEL_CLOCK_EXCEPTIONS', {('clock_cases.py', 'test_legacy'): 1})
    test_api_tests_freeze_the_shared_clock_instead_of_replacing_models_now()
    if change == 'new_test':
        path.write_text(original + original.replace('test_legacy', 'test_new'), encoding='utf-8')
    elif change == 'new_helper':
        nested = folder / 'nested'
        nested.mkdir()
        (nested / 'clock_support.py').write_text(original, encoding='utf-8')
    elif change == 'removed_patch':
        path.write_text('def test_legacy():\n    pass\n', encoding='utf-8')
    else:
        monkeypatch.setitem(globals(), 'MODEL_CLOCK_EXCEPTIONS', {})
    with pytest.raises(AssertionError, match='Use tests.clock_support.freeze_clock'):
        test_api_tests_freeze_the_shared_clock_instead_of_replacing_models_now()


def test_everything_reads_the_one_clock():
    found = []
    # Operational scripts (backups, releases, evidence) stamp the machine time on purpose; the seed script does not.
    paths = [p for folder in ("apps", "packages", "tests") for p in (ROOT / folder).rglob("*.py")] + [ROOT / "scripts" / "manage.py"]
    for path in paths:
        if path in (SEAM, Path(__file__).resolve()):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if MACHINE_CLOCK.search(line) and "real clock" not in line:
                found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not found, "Read packages.domain.time.now() (apps.api.models.now) instead of the machine clock:\n" + "\n".join(found)

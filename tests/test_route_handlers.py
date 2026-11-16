"""Browser tests never pass a builtin method (`held.append`) as a route handler: Playwright tags the handler object
with an attribute, which a builtin cannot take (AttributeError, #624). A lambda or a def works."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from .test_browser import holding

BUILTIN_METHODS = {'append', 'add', 'extend', 'insert', 'setdefault', 'update', 'put', 'put_nowait', 'appendleft'}


def builtin_handlers(source, name='<test>'):
    found = []
    for call in ast.walk(ast.parse(source)):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == 'route'):
            continue
        handler = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == 'handler'), None)
        if isinstance(handler, ast.Attribute) and handler.attr in BUILTIN_METHODS:
            found.append(f'{name}:{call.lineno}')
    return found


def test_the_check_finds_a_builtin_handler():
    assert builtin_handlers("page.route('**/api/me', held.append)") == ['<test>:1']
    assert builtin_handlers("context.route('**', handler=seen.add)") == ['<test>:1']
    assert builtin_handlers("page.route('**/api/me', lambda route: held.append(route))\nself.page.route(p, self.answer)") == []


def test_no_route_handler_is_a_builtin_method():
    found = [hit for path in sorted(Path(__file__).parent.glob('*.py'))
             for hit in builtin_handlers(path.read_text(encoding='utf-8'), path.name)]
    assert not found, f'Use `lambda route: held.append(route)` (or Held in test_browser.py): {found}'


@pytest.mark.parametrize('patterns', [('**/api/me',), ('**/api/bulk/settings', '**/api/commands/preview')])
@pytest.mark.parametrize('failed', [False, True])
def test_holding_releases_every_request_once_before_unregistering(patterns, failed):
    handlers, events = {}, []

    def unroute(pattern):
        events.append(('unroute', pattern))
        del handlers[pattern]

    page = SimpleNamespace(route=lambda pattern, handler: handlers.update({pattern: handler}), unroute=unroute)
    try:
        with holding(page) as held:
            for pattern in patterns:
                held.hold(pattern)
                for index in range(2):
                    request = (pattern, index)
                    handlers[pattern](SimpleNamespace(continue_=lambda request=request: events.append(('continue', request))))
                assert len(held[pattern]) == 2
            if failed:
                raise AssertionError('test failed with requests still held')
            held.release(patterns[0])
            assert held[patterns[0]] == []
    except AssertionError as exc:
        if not failed:
            raise
        assert str(exc) == 'test failed with requests still held'

    assert not handlers
    assert events == [event for pattern in patterns for event in
                      [('continue', (pattern, 0)), ('continue', (pattern, 1)), ('unroute', pattern)]]


def test_browser_regexes_have_no_backspaces_and_busy_class_matches():
    import re

    busy = None
    for path in sorted(Path(__file__).parent.glob('test_browser*.py')):
        for call in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name) and call.func.value.id == 're'
                    and call.func.attr == 'compile' and call.args and isinstance(call.args[0], ast.Constant)):
                continue
            pattern = call.args[0].value
            assert '\x08' not in pattern, f'{path.name}:{call.lineno}: literal backspace in regex'
            if path.name == 'test_browser_bulk_ux.py' and 'is-busy' in pattern:
                busy = re.compile(pattern)
    assert busy is not None and busy.search('button primary is-busy')
    assert not busy.search('button primary is-busier')


def test_the_browser_fixture_runs_cyclic_gc_only_on_the_test_thread_after_its_contexts_close():
    """G62: a held route's suspended coroutine, collected on a thread with another running loop, sent to the driver from
    there and broke the connection (KeyError '<call id>'). The fixture keeps cyclic GC off during the test and collects on
    teardown, after the contexts are closed."""
    import gc
    from . import test_browser
    events, finalizers = [], []
    context = SimpleNamespace(close=lambda: events.append('context closed'))
    raw = SimpleNamespace(on=lambda *a: None, remove_listener=lambda *a: None, contexts=[context])
    session = SimpleNamespace(ensure=lambda: raw, alive=lambda: True)
    request = SimpleNamespace(addfinalizer=finalizers.append, fixturenames=[], node=SimpleNamespace(nodeid='t'))
    collect = gc.collect
    try:
        gc.collect = lambda *a: events.append(('collected', gc.isenabled()))
        fixture = test_browser.browser.__wrapped__(session, request)
        next(fixture)
        assert not gc.isenabled()
        with pytest.raises(StopIteration):
            next(fixture)
        for finalizer in reversed(finalizers):
            finalizer()
        assert events == ['context closed', ('collected', False)] and gc.isenabled()
    finally:
        gc.collect = collect
        gc.enable()

"""Flake guard: a request/response wait is opened before the action that causes the request, around it.

`with page.expect_response(...): pass` (nothing inside) and a bare `page.wait_for_event('request…')` listen only from
that moment: a request that was already sent (a timer tick, a refresh, the click just before) is missed, and the
wait then times out (R17, R19). Put the trigger inside the with-block; for a timer, drive it there with
page.clock.run_for(...). A wait that truly needs no trigger (a poll repeating faster than the wait's timeout) is listed
below by file and function, with its reason.
"""
import ast
from pathlib import Path

WAITS = {'expect_request', 'expect_response', 'expect_request_finished', 'expect_navigation', 'expect_event',
         'expect_popup', 'expect_download', 'expect_websocket'}
NETWORK_EVENTS = {'request', 'response', 'requestfinished', 'requestfailed'}
ALLOWED = {
    ('test_browser_score_check.py', 'test_coordinator_sees_where_files_go_before_uploading_and_a_pdf_or_photo_is_read'):
        'the import is read again every 2 s while its file is "Reading" (asserted just before)',
    ('test_browser_autoschedule_live.py', 'wait_reads'): 'loops on its own read counter; the job is read on a timer',
    ('test_browser_autoschedule_live.py', 'test_a_run_that_ends_after_another_rota_opens_leaves_that_rota_alone'):
        'any later read: the running job shown on the strip (2/5) is read on a timer',
}


def _untriggered(tree):
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.With) and any(
                    isinstance(i.context_expr, ast.Call) and getattr(i.context_expr.func, 'attr', None) in WAITS
                    for i in node.items) and all(
                    isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                    for s in node.body):
                yield fn.name, node.lineno
            if isinstance(node, ast.Call) and getattr(node.func, 'attr', None) == 'wait_for_event' and node.args and \
                    isinstance(node.args[0], ast.Constant) and node.args[0].value in NETWORK_EVENTS:
                yield fn.name, node.lineno


def test_request_waits_wrap_their_trigger():
    found, used = [], set()
    for p in sorted(Path(__file__).parent.glob('test_browser*.py')):
        for name, line in _untriggered(ast.parse(p.read_text(encoding='utf-8'))):
            if (p.name, name) in ALLOWED:
                used.add((p.name, name))
            else:
                found.append(f'{p.name}:{line} ({name})')
    assert not found, f'Request/response waits with no trigger inside, opened after it may have fired: {found}'
    assert used == set(ALLOWED), f'Stale allowlist entries: {sorted(set(ALLOWED) - used)}'


def test_the_guard_sees_both_shapes():
    tree = ast.parse("def t(page):\n    page.click('x')\n    with page.expect_response('**/a'):\n        pass\n"
                     "    page.wait_for_event('requestfinished')\n    with page.expect_request('**/b'):\n"
                     "        page.click('y')\n")
    assert [line for _, line in _untriggered(tree)] == [3, 5]

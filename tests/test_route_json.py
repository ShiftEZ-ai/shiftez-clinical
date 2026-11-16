"""Simulated browser answers are prefetched through the real API without a browser or live socket."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_json_route_prefetches_real_answer_and_only_fulfils_at_callback_time(h, monkeypatch):
    real = h.get('admin', '/api/about')
    transform = Mock(side_effect=lambda body: {**body, 'worker': None})
    handler = h.route_json('admin', '/api/about', transform)
    transform.assert_called_once_with(real)
    monkeypatch.setattr(h, 'get', Mock(side_effect=AssertionError('Unexpected callback API read')))
    route = SimpleNamespace(fulfill=Mock(), fetch=Mock(side_effect=AssertionError('Unexpected socket fetch')))

    handler(route)
    handler(route)

    assert route.fulfill.call_count == 2
    assert route.fulfill.call_args.kwargs == {'json': {**real, 'worker': None}}
    route.fetch.assert_not_called()
    h.get.assert_not_called()
    transform.assert_called_once()


def test_json_route_reports_upstream_failure_before_installing_a_handler(h):
    transform = Mock()
    with pytest.raises(AssertionError):
        h.route_json('staff', '/api/not-a-real-endpoint', transform)
    transform.assert_not_called()


def route_fetch_bypasses(source):
    tree = ast.parse(source)
    callbacks = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'route':
            callbacks.extend(node.args[1:2])
            callbacks.extend(keyword.value for keyword in node.keywords if keyword.arg == 'handler')
    names = {node.id for node in callbacks if isinstance(node, ast.Name)}
    handlers = [node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    handlers.extend(node for node in callbacks if isinstance(node, ast.Lambda))
    handlers.extend(node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Lambda)
                    and any(isinstance(target, ast.Name) and target.id in names for target in node.targets))
    bypasses = set()
    for handler in handlers:
        parameters = handler.args.posonlyargs + handler.args.args
        if not parameters:
            continue
        for node in ast.walk(handler):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == parameters[0].arg
                    and node.func.attr == 'fetch'):
                bypasses.add(node.lineno)
    return bypasses


@pytest.mark.parametrize('source,expected', [
    ('def catalogues(r):\n    r.fetch(url="/api/catalogues")\ncontext.route("**", catalogues)', {2}),
    ('page.route("**", lambda intercepted: intercepted.fetch())', {1}),
    ('async def handle(answer):\n    await answer.fetch()\npage.route("**", handler=handle)', {2}),
    ('handle = lambda incoming: incoming.fetch()\npage.route("**", handle)', {1}),
    ('def handle(r):\n    response = page.request.fetch(r.request.url)\n'
     '    client = page.request\n    client.fetch(r.request.url)\n'
     '    httpx.get(r.request.url)\n    r.fulfill(json=response.json())\npage.route("**", handle)', set()),
    ('def route_fetch(route):\n    return route.fetch()\n'
     'page.route("**", lambda r: route_fetch(r))', set()),
], ids=['renamed-parameter', 'inline-lambda', 'keyword-async-handler', 'assigned-lambda',
        'request-clients', 'shared-forwarder'])
def test_route_fetch_guard_recognizes_handlers_without_flagging_request_clients(source, expected):
    assert route_fetch_bypasses(source) == expected


def test_socket_route_fetches_stay_in_the_shared_forwarding_helper():
    bypasses = {f'{path.name}:{line}' for path in Path(__file__).parent.rglob('*.py')
                for line in route_fetch_bypasses(path.read_text(encoding='utf-8'))}
    assert not bypasses, f'Use Harness.route_json for simulations or shared route_fetch for forwarding: {bypasses}'

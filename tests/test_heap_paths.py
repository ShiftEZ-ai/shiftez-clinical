"""Heap diagnostics stay bounded and report failures without replacing the leak assertion."""
import json

import pytest

from . import heap_paths


HEAP = {
    'snapshot': {'meta': {
        'node_fields': ['type', 'name', 'id', 'edge_count', 'detachedness'],
        'edge_fields': ['type', 'name_or_index', 'to_node'],
        'node_types': [['synthetic', 'native']], 'edge_types': [['property', 'weak']]}},
    'nodes': [0, 0, 1, 1, 0, 1, 1, 3, 0, 2], 'edges': [0, 2, 5],
    'strings': ['(GC roots)', 'Detached HTMLDivElement', 'view']}


class CDP:
    def __init__(self, *, nodes=1, fail=None, payload=None):
        self.nodes, self.fail = nodes, fail
        self.payload = json.dumps(HEAP) if payload is None else payload
        self.calls, self.listeners = [], []

    def on(self, event, receive):
        self.listeners.append(receive)

    def remove_listener(self, event, receive):
        self.listeners.remove(receive)
        if self.fail == 'remove_listener':
            raise RuntimeError(self.fail)

    def send(self, method, params=None):
        self.calls.append(method)
        if method == self.fail:
            raise RuntimeError(method)
        if method == 'Memory.getDOMCounters':
            return {'nodes': self.nodes}
        if method == 'HeapProfiler.takeHeapSnapshot':
            for receive in self.listeners:
                receive({'chunk': self.payload})


@pytest.mark.parametrize('fail', [
    'Memory.getDOMCounters', 'HeapProfiler.enable', 'HeapProfiler.takeHeapSnapshot', 'remove_listener'])
def test_heap_snapshot_errors_are_report_only(fail):
    cdp = CDP(fail=fail)
    assert heap_paths.detached_paths(heap_paths.snapshot(cdp)) == f'heap paths unavailable: {fail}'
    assert cdp.listeners == []


def test_heap_snapshot_skips_a_page_above_the_node_cap():
    cdp = CDP(nodes=heap_paths.MAX_SNAPSHOT_NODES + 1)
    assert heap_paths.detached_paths(heap_paths.snapshot(cdp)).startswith('heap paths unavailable: node cap exceeded')
    assert cdp.calls == ['Memory.getDOMCounters']
    assert cdp.listeners == []


def test_heap_traversal_skips_a_graph_above_the_node_cap(monkeypatch):
    monkeypatch.setattr(heap_paths, 'MAX_SNAPSHOT_NODES', 1)
    assert heap_paths.detached_paths(HEAP) == 'heap paths unavailable: node cap exceeded (2 > 1)'


def test_malformed_heap_data_is_report_only():
    cdp = CDP(payload='invalid JSON')
    assert heap_paths.detached_paths(heap_paths.snapshot(cdp)).startswith('heap paths unavailable: ')
    assert cdp.listeners == []
    assert heap_paths.detached_paths({}).startswith('heap paths unavailable: ')


def test_heap_paths_still_reports_a_retained_node_at_the_cap(monkeypatch):
    monkeypatch.setattr(heap_paths, 'MAX_SNAPSHOT_NODES', 2)
    cdp = CDP(nodes=2)
    assert heap_paths.detached_paths(heap_paths.snapshot(cdp)) == {
        'detached_native': 1, 'holders': [('Detached HTMLDivElement <- (GC roots) .view', 1)]}
    assert cdp.listeners == []

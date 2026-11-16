"""Failure diagnostic for the browser memory tests: which owner keeps detached DOM alive. Takes a V8 heap snapshot over
CDP and returns, for the detached trees still reachable from the GC roots, the shortest strong retaining path (weak
edges skipped), grouped by path, so a failing leak assertion names its holder instead of only a node count."""
import json
import re
from collections import Counter, deque

EPHEMERON = re.compile(r'part of key \(.*@(\d+)\) -> value')
MAX_SNAPSHOT_NODES = 200_000


def snapshot(cdp):
    chunks = []
    receive = lambda e: chunks.append(e['chunk'])  # noqa: E731
    try:
        count = cdp.send('Memory.getDOMCounters')['nodes']
        if count > MAX_SNAPSHOT_NODES:
            return f'heap paths unavailable: node cap exceeded ({count} > {MAX_SNAPSHOT_NODES})'
        cdp.on('HeapProfiler.addHeapSnapshotChunk', receive)
        try:
            cdp.send('HeapProfiler.enable')
            cdp.send('HeapProfiler.takeHeapSnapshot', {'reportProgress': False, 'captureNumericValue': False})
            return json.loads(''.join(chunks))
        finally:
            cdp.remove_listener('HeapProfiler.addHeapSnapshotChunk', receive)
    except Exception as err:
        return f'heap paths unavailable: {err}'


def detached_paths(data, top=8, hops=16):
    try:
        if isinstance(data, str):
            return data
        count = len(data['nodes']) // len(data['snapshot']['meta']['node_fields'])
        if count > MAX_SNAPSHOT_NODES:
            return f'heap paths unavailable: node cap exceeded ({count} > {MAX_SNAPSHOT_NODES})'
        return _detached_paths(data, top, hops)
    except Exception as err:
        return f'heap paths unavailable: {err}'


def _detached_paths(data, top, hops):
    meta = data['snapshot']['meta']
    nf, ef = meta['node_fields'], meta['edge_fields']
    nn, en = len(nf), len(ef)
    ntypes, etypes = meta['node_types'][0], meta['edge_types'][0]
    nodes, edges, strings = data['nodes'], data['edges'], data['strings']
    n_type, n_name, n_edges, n_id = nf.index('type'), nf.index('name'), nf.index('edge_count'), nf.index('id')
    n_det = nf.index('detachedness') if 'detachedness' in nf else None
    e_type, e_name, e_to = ef.index('type'), ef.index('name_or_index'), ef.index('to_node')
    count = len(nodes) // nn
    first, at = [0] * (count + 1), 0
    for i in range(count):
        first[i] = at
        at += nodes[i * nn + n_edges] * en
    first[count] = at
    name = lambda i: strings[nodes[i * nn + n_name]]  # noqa: E731
    detached = lambda i: (nodes[i * nn + n_det] == 2) if n_det is not None else name(i).startswith('Detached')  # noqa: E731
    index = {nodes[i * nn + n_id]: i for i in range(count)}
    parent, via, pending = [-1] * count, [-1] * count, {}

    def reach(i, e, j):
        if parent[j] < 0:
            parent[j], via[j] = i, e
            queue.append(j)
            for later in pending.pop(j, ()):  # WeakMap values whose key is now held
                reach(*later)

    parent[0], queue = 0, deque([0])
    while queue:
        i = queue.popleft()
        for e in range(first[i], first[i + 1], en):
            kind = etypes[edges[e + e_type]]
            if kind == 'weak':
                continue
            j = edges[e + e_to] // nn
            # A WeakMap's table holds a value only while its key lives: follow it once the key is reached.
            key = EPHEMERON.search(strings[edges[e + e_name]]) if kind not in ('element', 'hidden') else None
            k = index.get(int(key.group(1))) if key else None
            if k is not None and parent[k] < 0:
                pending.setdefault(k, []).append((k, e, j))
                continue
            reach(i if k is None else k, e, j)  # a value's holder is its key's

    def label(e):
        kind, n = etypes[edges[e + e_type]], edges[e + e_name]
        return f'[{n}]' if kind in ('element', 'hidden') else strings[n]

    def path(j):
        steps = [name(j)[:80]]
        while j and len(steps) < hops:
            i = parent[j]
            steps.append(f'{name(i)[:60]} .{label(via[j])}')
            j = i
        return ' <- '.join(steps)

    top_of = {}

    def boundary(j):  # the first detached object on j's retaining path: what a live owner holds
        chain = []
        while j not in top_of and j and detached(parent[j]):
            chain.append(j)
            j = parent[j]
        found = top_of.get(j, j)
        for c in chain:
            top_of[c] = found
        top_of[j] = found
        return found

    reachable = [j for j in range(count) if parent[j] >= 0 and detached(j) and ntypes[nodes[j * nn + n_type]] == 'native']
    held = Counter(boundary(j) for j in reachable)
    by_path = Counter()
    for k, n in held.items():
        by_path[path(k)] += n
    return {'detached_native': len(reachable), 'holders': by_path.most_common(top)}

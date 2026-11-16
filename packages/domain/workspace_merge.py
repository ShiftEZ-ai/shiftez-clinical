"""Three-way intent bundles. No SQL, overwrites of unrelated work, or second validator."""
from copy import deepcopy
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from .proposal_reconciliation import participant
from .participation import presence_intervals
from .time import aware


def definition(row):
    return {key: row.get(key) for key in ('assignment_type_id', 'type_revision', 'type_snapshot',
        'location_id', 'start', 'end', 'cancelled', 'locked', 'attributes', 'service_ids')}


def bundle_id(source_id, kind, key):
    return str(uuid5(NAMESPACE_URL, f'{source_id}:{kind}:{key}'))


def assignment(row):
    return participant(row) if row is not None else None


def adjustment_key(row):
    # Same source, person and effective window is one decision, even when its value changes.
    return tuple(str(row.get(k) or '') for k in ('policy_id', 'line_id', 'number', 'person_id', 'starts_on', 'ends_on'))


def plan(source_id, base, fork, source, target, session_map, *, assignment_allowed, adjustment_allowed):
    old = {s['id']: s for s in fork['shifts']}
    proposed = {s['id']: s for s in source['shifts']}
    parent = {s['id']: s for s in base['shifts']}
    current = {s['id']: s for s in target['shifts']}
    bundles, people, links = [], {}, []
    def person_bundle(pid):
        return people.setdefault(pid, {'id': bundle_id(source_id, 'person', pid), 'kind': 'fits',
            'allowed_choices': ['yours'], 'person_id': pid, 'changes': [], 'facts': {}, 'changed_inputs': []})
    def classify(bundle, pid, now, before, yours, rota, *, conflict=False):
        if now is None or not all(assignment_allowed(pid, now, row) for row in (before, yours, rota) if row):
            bundle.update(kind='out_of_scope', allowed_choices=['not_merged'])
            return
        if bundle['kind'] == 'out_of_scope':
            return
        reason = ('This session is locked.' if now.get('locked') else 'This session is closed.' if now.get('cancelled') else
                  'This assignment is fixed.' if rota and rota.get('fixed') else
                  'Another assignment relies on this supervisor.' if yours is None and any(
                      p.get('supervisor_person_id') == pid for p in now['participants']) else None)
        if reason:
            bundle.update(kind='protected_coupling', allowed_choices=['rota', 'not_merged'])
            bundle['facts'].setdefault('protection_reasons', []).append(reason)
        elif bundle['kind'] != 'protected_coupling' and (conflict or rota != before and rota != yours):
            bundle.update(kind='both_changed', allowed_choices=['rota', 'yours'])
    for sid in sorted(old.keys() | proposed.keys()):
        left, right = old.get(sid), proposed.get(sid)
        parent_id = session_map.get(sid)
        original, now = parent.get(parent_id), current.get(parent_id)
        if left is None or right is None or original is None or definition(left) != definition(right):
            if left != right:
                bundles.append({'id': bundle_id(source_id, 'session', sid), 'kind': 'out_of_scope',
                    'allowed_choices': ['not_merged'], 'changes': [], 'facts': {'shift_id': parent_id or sid,
                    'message': 'Session definitions belong to the Rota Coordinator'}, 'changed_inputs': []})
            continue
        a, b, c, initial = ({p['person_id']: p for p in (row or {}).get('participants', [])}
                            for row in (left, right, now, original))
        changed = {pid for pid in a.keys() | b.keys() if assignment(a.get(pid)) != assignment(b.get(pid))}
        if not changed:
            continue
        connected, conflicts = set(), set()
        intervals = [{pid: presence_intervals(p) for pid, p in rows.items()} for rows in (a, b, c, initial)]
        requirements = (now or original).get('type_snapshot', {}).get('definition', {}).get('requirements', [])
        start, end = (aware(original['start']), aware(original['end'])) if requirements else (None, None)
        points = {t for rows in intervals for spans in rows.values() for span in spans for t in span}
        points.update(start + timedelta(minutes=r.get('from_minute', 0)) for r in requirements)
        points.update(start + timedelta(minutes=r['to_minute']) if r.get('to_minute') is not None else end for r in requirements)
        for lo, hi in zip(sorted(points), sorted(points)[1:]):
            aa, bb, cc, ii = ({pid for pid, spans in rows.items() if any(x <= lo and hi <= y for x, y in spans)}
                              for rows in intervals)
            removed, added = aa - bb, bb - aa
            rota_removed, rota_added = ii - cc, cc - ii
            # Replacements own the vacated temporal place; whole-person moves stay inseparable below.
            linked = removed | added if removed and added else set()
            applicable = [r for r in requirements if start + timedelta(minutes=r.get('from_minute', 0)) <= lo
                and hi <= (start + timedelta(minutes=r['to_minute']) if r.get('to_minute') is not None else end)]
            capacity = max([len(aa), *[r.get('maximum') if r.get('maximum') is not None else
                                    max(r.get('minimum', 0), r.get('target') or 0) for r in applicable]])
            combined = (cc - removed) | added
            collision = (bool(removed & rota_removed and (added or rota_added)) and bb != cc or
                         bool(rota_added and added and len(combined) > capacity))
            if collision:
                linked.update(removed | added | rota_added)
                conflicts.update(linked)
            if linked:
                links.append(linked)
                connected.update(linked)
        for pid in sorted(changed | connected):
            before, yours, rota = assignment(a.get(pid)), assignment(b.get(pid)), assignment(c.get(pid))
            bundle = person_bundle(pid)
            bundle['changes'].append({'shift_id': parent_id, 'person_id': pid, 'before': before,
                'yours': yours, 'rota': rota, 'source': {k: b[pid].get(k) for k in
                ('source_actor_id', 'source', 'source_run_id', 'source_at')} if pid in b else {}})
            classify(bundle, pid, now, before, yours, rota, conflict=pid in conflicts)
            if now is not None and definition(original) != definition(now):
                if bundle['kind'] not in {'out_of_scope', 'protected_coupling'}:
                    bundle.update(kind='input_changed', allowed_choices=['rota', 'yours'])
                bundle['changed_inputs'].append({'shift_id': parent_id, 'kind': 'session_definition'})
    # Connect the occupied places and whole-person moves; no partial replacement or partial move is selectable.
    roots = {pid: pid for pid in people}
    def root(pid):
        while roots[pid] != pid:
            roots[pid] = roots[roots[pid]]
            pid = roots[pid]
        return pid
    for linked in links:
        ids = sorted(linked)
        for pid in ids[1:]:
            roots[root(pid)] = root(ids[0])
    merged = {}
    priority = {'fits': 0, 'both_changed': 1, 'input_changed': 2, 'protected_coupling': 3, 'out_of_scope': 4}
    for pid, bundle in people.items():
        merged.setdefault(root(pid), []).append(bundle)
    for parts in merged.values():
        if len(parts) == 1:
            bundles.append(parts[0])
            continue
        winner = max(parts, key=lambda b: priority[b['kind']])
        pids = sorted(b['person_id'] for b in parts)
        changes = [c for b in parts for c in b['changes']]
        bundles.append({**winner, 'id': bundle_id(source_id, 'places', pids), 'person_id': None,
            'changes': changes, 'facts': {'person_ids': pids, 'protection_reasons': sorted({r for b in parts
                for r in b['facts'].get('protection_reasons', [])})},
            'changed_inputs': [c for b in parts for c in b['changed_inputs']]})
    for field in ('rule_adjustments', 'target_overrides'):
        a, b, c = [{adjustment_key(r): r for r in content['workspace'].get(field, [])} for content in (fork, source, target)]
        for key in sorted(a.keys() | b.keys()):
            before, yours, rota = a.get(key), b.get(key), c.get(key)
            if before == yours:
                continue
            kind = ('out_of_scope' if not adjustment_allowed(yours or before) else
                    'both_changed' if rota != before and rota != yours else 'fits')
            bundles.append({'id': bundle_id(source_id, field, key), 'kind': kind, 'field': field,
                'key': list(key), 'before': before, 'yours': yours, 'rota': rota, 'changes': [], 'facts': {},
                'changed_inputs': [], 'allowed_choices': ['not_merged'] if kind == 'out_of_scope' else
                ['rota', 'yours'] if kind == 'both_changed' else ['yours']})
    return sorted(bundles, key=lambda b: b['id'])


def overlay(target, bundles, choices):
    result = deepcopy(target)
    shifts = {s['id']: s for s in result['shifts']}
    for bundle in bundles:
        if choices.get(bundle['id']) not in {'yours', 'left_out'}:
            continue
        if bundle.get('field'):
            field, key = bundle['field'], tuple(bundle['key'])
            rows = [r for r in result['workspace'].get(field, []) if adjustment_key(r) != key]
            if bundle.get('yours') is not None:
                rows.append(deepcopy(bundle['yours']))
            result['workspace'][field] = rows
        for change in bundle['changes']:
            row = shifts[change['shift_id']]
            previous = next((p for p in row['participants'] if p['person_id'] == change['person_id']), None)
            row['participants'] = [p for p in row['participants'] if p['person_id'] != change['person_id']]
            if change['yours'] is not None:
                row['participants'].append({**deepcopy(change['yours']), 'id': (previous or {}).get('id') or
                    bundle_id(bundle['id'], 'assignment', (row['id'], change['person_id'])), 'revision': (previous or {}).get('revision', 0) + 1})
    return result

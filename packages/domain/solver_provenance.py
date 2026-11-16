"""Machine-readable constraint names survive prepared-model serialization and cloning."""
import hashlib
import json

from packages.contracts.schemas import NEVER_OVERRIDDEN


def policy_id(name):
    """Read source identity without requiring the full diagnostic occurrence registry."""
    if not name.startswith('policy:'):
        return None
    value = name.split(':', 1)[1]
    return json.loads(value)['rule_id'] if value.startswith('{') else value


def policy_name(policy, *, data=None, row=None, person_ids=(), session_ids=(), start=None, end=None):
    rule = policy['definition'].get('rule', {})
    line = policy.get('template_line') or {}
    row = row or {}
    static = None
    static_cache = data.get('_policy_name_static') if data is not None else None
    if static_cache is not None:
        static = static_cache.get(id(policy))
    if static is None:
        definition_hash = hashlib.sha256(json.dumps(policy['definition'], sort_keys=True, default=str).encode()).hexdigest()
        static = (definition_hash, {})
        if static_cache is not None:
            static_cache[id(policy)] = static
    reference = {
        'kind': 'policy', 'rule_id': policy['id'], 'revision': policy['revision'], 'name': policy['name'],
        'line_id': row.get('line_id', line.get('id')), 'line': row.get('line', line.get('line')),
        'slot': row.get('slot') or line.get('slot') or rule.get('comparison'),
        'rule_kind': rule.get('kind'), 'safety': rule.get('kind') in NEVER_OVERRIDDEN,
        'person_ids': sorted(set(person_ids)), 'session_ids': sorted(set(session_ids)),
        'starts_on': row.get('starts_on', policy.get('starts_on')), 'ends_on': row.get('ends_on', policy.get('ends_on')),
        'start': str(start) if start is not None else None, 'end': str(end) if end is not None else None,
        'comparison': row.get('comparison', rule.get('comparison')),
        'amount': row.get('amount', rule.get('minimum', rule.get('maximum', rule.get('amount')))),
        'metric': row.get('metric', rule.get('metric')),
        'definition_hash': static[0],
    }
    if 'count_bucket' in policy:
        reference['count_bucket'] = policy['count_bucket']
    # Inherited counting levels can depend on more than the representative source row.
    source_rows = static[1].get(reference['slot'] or '')
    if source_rows is None:
        sources = policy.get('_catalogue_sources') or {}
        inherited = (sources.get(f"{policy['id']}:{line.get('id')}") or {}).get('inherit') or {}
        contributors = {key for slot in (reference['slot'] or '').split('+')
                        for key in (inherited.get(slot) or {}).get('sources', [])}
        source_rows = static[1][reference['slot'] or ''] = tuple(
            (sources[key]['policy_id'], sources[key]['id'], sources[key]['line'])
            for key in sorted(contributors) if key in sources)
    reference['sources'] = [{'rule_id': policy_id, 'line_id': line_id, 'line': source_line}
                            for policy_id, line_id, source_line in source_rows]
    encoded = json.dumps(reference, sort_keys=True, separators=(',', ':'), default=str)
    if data is not None:
        token = hashlib.sha256(encoded.encode()).hexdigest()
        data.setdefault('_solver_provenance', {}).setdefault(token, reference)
        reference = {key: reference[key] for key in ('rule_id', 'revision', 'line_id', 'line', 'slot')}
        reference['ref'] = token
        encoded = json.dumps(reference, sort_keys=True, separators=(',', ':'))
    return 'policy:' + encoded


def requirement(name, data):
    kind, value = name.split(':', 1)
    if kind == 'policy' and value.startswith('{'):
        reference = json.loads(value)
        if 'ref' in reference:
            return data['_solver_provenance'][reference['ref']]
        return reference
    if kind == 'policy':  # Previously prepared models and legacy callers remain readable.
        policy = next((p for p in data.get('policies', []) if p['id'] == value), None)
        if policy is None:
            return None  # Unattributed constraints remain background; never invent a rule label.
        return json.loads(policy_name(policy).split(':', 1)[1])
    shift = next(s for s in data['shifts'] if s['id'] == value)
    return {'kind': 'duty', 'shift_id': value, 'name': shift['type_snapshot']['name'],
            'session_ids': [value], 'person_ids': [], 'start': shift['start'], 'end': shift['end'],
            'service_ids': shift['service_ids'], 'assignment_type_id': shift['assignment_type_id'],
            'type_revision': shift['type_revision'], 'safety': True}


def source_key(reference):
    fields = ('kind', 'rule_id', 'revision', 'line_id', 'line', 'slot', 'sources', 'shift_id')
    return json.dumps({key: reference[key] for key in fields if key in reference}, sort_keys=True, separators=(',', ':'))

"""Substance identity for before/after findings; citations remain available for audit."""
from .input_manifest import digest
from .validation import finding_observations


def finding_substance(value):
    """Keep code, severity, person(s), session(s), start/end/window, outside_rota and
    obligation detail: required/limit/maximum/amount/actual/available, metric,
    comparison, slot, count_bucket, capability, supervision, exception and other
    constraint facts (including rule_severity/strength). Unknown facts stay keyed.

    At the finding and its detail only, exclude citing rule_id, line/line_id and
    precedence rule_tier, display message/key, cause/fix_hints/proration/display
    decorations, and grouped derived_findings. Deeper values stay intact.
    finding_index expands those children into independent substance observations.
    """
    if isinstance(value, dict):
        ignored = {'rule_id', 'line', 'line_id', 'rule_tier', 'message', 'key',
                   'cause', 'fix_hints', 'proration', 'display', 'derived_findings'}
        if value.get('code') == 'UNAVAILABLE':
            ignored |= {'classification', 'leave_shift_ids'}  # Optional leave explanation, not another absence.
        substance = {k: v for k, v in value.items() if k not in ignored}
        if value.get('code') == 'UNAVAILABLE' and substance.get('detail'):
            substance['detail'] = {k: v for k, v in substance['detail'].items()
                                   if k not in {'classification', 'leave_shift_ids'}} or None
        if isinstance(substance.get('detail'), dict):
            substance['detail'] = {k: v for k, v in substance['detail'].items() if k not in ignored}
        return substance
    if isinstance(value, list):
        return [finding_substance(v) for v in value]
    return value


def finding_key(finding):
    return digest(finding_substance(finding))


def citation(finding):
    detail = finding.get('detail') or {}
    line = detail.get('line')
    return {'rule_id': finding.get('rule_id'), 'line_id': detail.get('line_id'),
            'line_number': line + 1 if isinstance(line, int) else None}


def finding_index(validation):
    """One outcome per substance; keep the visible primary citation over grouped duplicates."""
    indexed = {}
    for finding in finding_observations(validation):
        key = finding_key(finding)
        if key not in indexed or citation(indexed[key]) == citation(finding):
            indexed[key] = finding
    return indexed


def recited_findings(before, after):
    """Unchanged outcomes with a changed citation, in stable before-observation order."""
    result = []
    for key, previous in before.items():
        if key not in after:
            continue
        old, new = citation(previous), citation(after[key])
        if old != new:
            result.append({'finding_key': key, 'code': previous['code'],
                **{k: previous.get(k) for k in ('person_id', 'shift_id', 'start', 'end')},
                **{f'{k}_before': v for k, v in old.items()},
                **{f'{k}_after': v for k, v in new.items()}})
    return result

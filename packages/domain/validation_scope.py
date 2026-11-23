"""Conservative dependency closure; rule applicability stays in the canonical validator."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from .time import aware


def affected_sessions(shifts, changed, *, spans=None):
    """Include simultaneous peers on either side of an edit (cover, supervision and resources)."""
    changed_spans = [(aware(row['start']), aware(row['end'])) for row in changed]
    spans = spans if spans is not None else ((aware(row['start']), aware(row['end']), row['id']) for row in shifts)
    return {sid for start, end, sid in spans if any(start < b and a < end for a, b in changed_spans)}


def assignment_scope(data, before, after):
    people = {p['person_id'] for row in [*before, *after] for p in row['participants']}
    shifts = affected_sessions(data['shifts'], [*before, *after]) | {row['id'] for row in after}
    zone = ZoneInfo(data['organisation']['timezone'])
    dates = set()
    for row in [*before, *after]:
        start = aware(row['start']).astimezone(zone).date()
        end = aware(row['end']).astimezone(zone).date()
        dates.update((start + timedelta(days=offset)).isoformat()
                     for offset in range((end - start).days + 1))
    return {'shifts': shifts, 'people': people, 'dates': dates, 'include_resources': True}


def bounded_assignment_scope(data, before, after):
    """Close an assignment edit over the canonical rule-map reach.

    An unscoped map row is unknown by design.  Returning ``None`` lets the
    caller use its full or asynchronous preview path instead of guessing at
    a safe subset.
    """
    scope = assignment_scope(data, before, after)
    try:
        from .rule_map import rule_map
        mapped = rule_map(data, reach_only=True)
        rows = data['shifts']
        focused = set(data.get('focus_ids') or ())
        by_index = {index: row['id'] for index, row in enumerate(rows)
                    if row['id'] in focused}
        ids, people, dates = set(scope['shifts']), set(scope['people']), set(scope['dates'])
        zone = ZoneInfo(data['organisation']['timezone'])
        first = date.fromisoformat(str(data['workspace']['starts_on']))
        changed_indexes = {index for index, row in enumerate(rows) if row['id'] in ids}
        changed_days = set()
        for row in [*before, *after]:
            start = aware(row['start']).astimezone(zone).date()
            end = aware(row['end']).astimezone(zone).date()
            changed_days.update((start + timedelta(days=offset) - first).days
                                for offset in range((end - start).days + 1))
        for rule in mapped.get('rules') or ():
            sessions = set(rule.get('sessions') or ())
            reached_people = set(rule.get('people') or {})
            reached_days = set(rule.get('days') or ())
            if not sessions and not reached_people and not reached_days:
                return None
            if not (changed_indexes & sessions or people & reached_people or changed_days & reached_days):
                continue
            ids.update(by_index[index] for index in sessions if index in by_index)
            people.update(reached_people)
            dates.update((first + timedelta(days=index)).isoformat() for index in reached_days)
    except (KeyError, TypeError, ValueError, AssertionError):
        return None
    scope.update(shifts=ids, people=people, dates=dates)
    return scope


def evaluate_assignments(data, scope, *, explain_findings=True):
    """The existing trial validator retains all duties and rules, including institutional person windows."""
    from .validation import evaluate, ENGINE_VERSION, check_contracts
    parts = evaluate({**data, '_partial': scope}, explain_findings=explain_findings)
    findings = parts['session_findings'] + parts['person_findings']
    errors = sum(f['severity'] == 'error' for f in findings)
    return {'engine_version': ENGINE_VERSION, 'contracts': check_contracts(),
            'fingerprint': data['fingerprint'], 'scope': 'command_changes',
            'valid': errors == 0, 'error_count': errors,
            'warning_count': sum(f['severity'] == 'warning' for f in findings),
            'findings': findings, 'coverage': parts['coverage'], '_command_scope': scope,
            **({'quota_report': parts['quota_report']} if parts['quota_report'] is not None else {})}

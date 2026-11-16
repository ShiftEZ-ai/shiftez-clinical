"""Operator-ordered staffing populations, selected from immutable structural availability."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from .conditions import change_points, scope_matches_throughout
from .recurrence import inactive, series_candidate_allowed
from .time import aware, local_midnight, overlaps


def _opportunities(data, policy, shift, a, b):
    from .validation import scope_intervals, session_scope
    rule, scope = policy['definition']['rule'], policy['definition']['scope']
    if rule.get('selection_period', 'duty') == 'duty':
        return [(shift, a, b)]
    zone = data['organisation']['timezone']
    day = a.astimezone(ZoneInfo(zone)).date()
    first = day - timedelta(days=(day.weekday() - data['organisation']['week_start']) % 7)
    lo, hi = local_midnight(first, zone), local_midnight(first + timedelta(days=7), zone)
    if policy.get('starts_on'):
        lo = max(lo, local_midnight(date.fromisoformat(str(policy['starts_on'])), zone))
    if policy.get('ends_on'):
        hi = min(hi, local_midnight(date.fromisoformat(str(policy['ends_on'])), zone))
    department = policy['department_id']
    found = []
    for peer in data['shifts']:
        start, end = max(lo, aware(peer['start'])), min(hi, aware(peer['end']))
        if (inactive(peer) or start >= end
                or data['types'][peer['assignment_type_id']]['department_id'] != department):
            continue
        found.extend((peer, c, d) for c, d in scope_intervals(data, session_scope(scope), None, start, end))
    return found


def selected_tier(data, policy, shift, a, b):
    """First nonempty configured tier, or -1. Assignments, rest and quotas never change this pool.

    Availability must cover a complete matching counting interval. Dated person facts can split that interval;
    a short free portion inside an absence or restriction cannot make a person available for the whole duty.
    """
    from .validation import (person_rows, qualified, restriction_blocks, rotation_blocks, valid_dates,
                             effective_type_definition)
    rule, scope = policy['definition']['rule'], policy['definition']['scope']
    order = rule.get('selection_order') or []
    cache = data.get('_staffing_selection_cache')
    zone = data['organisation']['timezone']
    if rule.get('selection_period', 'duty') == 'week':
        day = a.astimezone(ZoneInfo(zone)).date()
        period = day - timedelta(days=(day.weekday() - data['organisation']['week_start']) % 7)
    else:
        period = shift['id'], a, b
    key = id(policy), period
    hit = cache.get(key) if cache is not None else None
    if hit is not None and hit[0] is policy:
        return hit[1]
    triggers = set(rule.get('when_person_ids', []))
    opportunities = _opportunities(data, policy, shift, a, b)
    selected = -1
    for index, conditions in enumerate(order):
        for peer, start, end in opportunities:
            typ = data['types'][peer['assignment_type_id']]
            definition = effective_type_definition(data, peer)
            for pid in data['people']:
                if pid in triggers or not series_candidate_allowed(data, peer, pid):
                    continue
                points = change_points(data, pid, start, end)
                for lo, hi in zip(points, points[1:] + [end]):
                    if (valid_dates(typ, lo, hi, zone)
                            and scope_matches_throughout(data, scope, pid, peer, lo, hi)
                            and scope_matches_throughout(data, {'conditions': conditions}, pid, peer, lo, hi)
                            and qualified(data, pid, peer, definition, lo, hi)
                            and not rotation_blocks(data, pid, lo, hi)
                            and not any(overlaps(lo, hi, aware(row['start']), aware(row['end']))
                                        for row in person_rows(data, 'availability', pid))
                            and not restriction_blocks(data, pid, peer, lo, hi)):
                        selected = index
                        break
                if selected >= 0:
                    break
            if selected >= 0:
                break
        if selected >= 0:
            break
    if cache is not None:
        cache[key] = policy, selected
    return selected

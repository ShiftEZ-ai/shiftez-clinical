"""Fixed-block department commitments: one calculation for validation and reports.

Only candidate work plus current internal publications are counted. Unknown external
work, attendance, payroll and unconfigured pro-rating are deliberately not inferred.
Counts are NOT evidence of qualification, cover or completed education.
"""

from collections import defaultdict
from datetime import date, timedelta
from fractions import Fraction
import copy
import csv
import io
import json
import math
from zoneinfo import ZoneInfo
from packages.contracts.schemas import DEFAULT_QUOTA_PRORATION_BASIS, TypeDefinition
from .recurrence import inactive
from .participation import presence_intervals
from .conditions import (active_rules, bind_level_window, conditions_match, scope_tree, counting_key, counting_views,
                         quota_target_scope, scope_matches_throughout, scope_has_duty_witness, ScopeContextLimit, SCOPE_CONTEXT_BUDGET)
from .input_manifest import digest
from .time import aware, local_midnight, local_stamp, overlaps, union_hours, duration_hours

METRIC_VERSION = "block-quota-v6"
QUOTA_DEVIATION_CODES = {
    "above_maximum": "QUOTA_ABOVE_MAXIMUM",
    "above_target": "QUOTA_ABOVE_TARGET",
    "below_minimum": "QUOTA_BELOW_MINIMUM",
    "below_target": "QUOTA_BELOW_TARGET",
}
# Departmental monthly limits and four-week targets overlap (121 in the baseline).
# shortcut: bounded evaluation; partition planning scope if 256 quotas are exceeded.
MAX_POLICIES = 256
MAX_SUBJECTS = 2000
MAX_SHIFTS = 10000
MAX_PARTICIPANTS = 40000
MAX_ROWS = 10000
MAX_SUBJECT_CONTEXTS = SCOPE_CONTEXT_BUDGET  # one report-wide ceiling, not multiplied by people or quota windows
DEPARTMENT_PRORATION_TEXT = ("Partial minimums and targets use department weekend days or the selected operating calendar "
                             "override and round up; maxima are unchanged.")
BASIS = ("Minimums and maximums include current department publications over the complete block; targets count the effective rota overlap. "
         "other drafts, other departments, external work and attendance are excluded. "
         f"{DEPARTMENT_PRORATION_TEXT} "
         "Approved leave, recorded leave and actual absence reduce the in-scope working days for minimums and targets; maxima are unchanged. Credits are planned, not completed training or pay.")


def window_blocks(start, stop, alignment, window_days=None, week_start=0, anchor_on=None):
    """The back-to-back blocks of a repeating quota over [start, stop): each `window_days` from the start (fixed), each
    week from the department's first weekday (0 = Monday … 6 = Sunday), or each calendar month, cut at both ends."""
    # The anchor fixes a block's phase independently of the interval's effective dates.
    cursor = (anchor_on + timedelta(days=((start - anchor_on).days // window_days) * window_days)
              if alignment == 'fixed' and anchor_on is not None else start)
    while cursor < stop:
        if alignment == 'fixed':
            end = cursor + timedelta(days=window_days)
        elif alignment == 'calendar_week':
            end = cursor + timedelta(days=((week_start - cursor.weekday() - 1) % 7) + 1)
        else:
            end = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
        yield max(start, cursor), min(end, stop)
        cursor = min(end, stop)


def quota_window_amount(policy, fallback=None):
    """Read the shared report's exact final amount without applying proration twice."""
    rule = policy['definition']['rule']
    # The shared report has already resolved this person's working-day bound.
    return Fraction(str(rule['amount'] if fallback is None else fallback))


def dated(policy, **window):
    """One window of a quota: the rule with its own dates; everything else is shared with the rule (read only). A deep
    copy per window per check was a sixth of a department-sized rota check."""
    rule = {**policy['definition']['rule'], **window}
    return {**policy, 'definition': {**policy['definition'], 'rule': rule,
            'scope': bind_level_window(policy['definition']['scope'], rule['starts_on'], rule['ends_on'])},
            **({'_count_scope': bind_level_window(policy['_count_scope'], rule['starts_on'], rule['ends_on'])}
               if '_count_scope' in policy else {})}


def quota_identity(row):
    """The exact expanded bound: source lines, slots and inherited levels may share a policy id."""
    return tuple(row.get(k) for k in ('policy_id', 'starts_on', 'ends_on', 'line_id', 'line', 'slot',
                                     'comparison', 'severity', 'strength')) + (counting_key(row),)


def applicable_policies(data):
    w = data["workspace"]
    policies = [p for p in active_rules(data.get("policies", []), context=data)
            if p["department_id"] == w["department_id"]
            and p["definition"]["rule"]["kind"] == "block_quota"
            and p["definition"]["rule"]["starts_on"] < w["ends_on"]
            and (not p["definition"]["rule"].get("ends_on") or w["starts_on"] < p["definition"]["rule"]["ends_on"])]
    result = []
    for policy in counting_views(data, policies):
        rule = policy['definition']['rule']
        repeat = rule.get('repeat', False)
        if repeat:
            first = max(rule['starts_on'], policy.get('starts_on') or rule['starts_on'])
            ends = [end for end in (rule.get('ends_on'), policy.get('ends_on')) if end]
            last = min(ends) if ends else None
            if last and (first >= last or last <= w['starts_on']):
                continue
            policy = dated(policy, starts_on=first, ends_on=last)
            rule = policy['definition']['rule']
        if rule.get('prorate_department_days'):
            full = policy.get('_quota_full_window') or (rule['starts_on'], rule['ends_on'])
            window = dated(policy, starts_on=max(rule['starts_on'], w['starts_on']), ends_on=min(rule['ends_on'], w['ends_on']))
            window['_quota_full_window'] = full
            if rule['comparison'] == 'target':
                # The rota window clips the evaluation without changing the accepted rule.
                window['_target_rule_fingerprint'] = digest([
                    policy.get('_source_policy_id', policy['id']), rule, full, policy.get('starts_on'), policy.get('ends_on'),
                    quota_target_scope(policy['definition']['scope'])])
            result.append(window)
            continue
        alignment = rule.get('window_alignment', 'rolling')
        if alignment in ('fixed', 'calendar_week', 'calendar_month'):
            # Back-to-back windows (each week from the start date, or each calendar month) that meet this rota.
            lo, hi = date.fromisoformat(w['starts_on']), date.fromisoformat(w['ends_on'])
            original_first = (policy.get('_quota_full_window') or (rule['starts_on'],))[0]
            anchor = date.fromisoformat(str(rule.get('window_anchor_on') or original_first)) if alignment == 'fixed' else None
            first, last = date.fromisoformat(rule['starts_on']), date.fromisoformat(rule['ends_on']) if rule.get('ends_on') else None
            if repeat:
                if alignment == 'fixed':
                    anchor = anchor or first
                    nearby = anchor + timedelta(days=((max(first, lo) - anchor).days // rule['window_days']) * rule['window_days'])
                    first = max(first, nearby)
                    horizon = hi + timedelta(days=rule['window_days'])
                elif alignment == 'calendar_week':
                    first = max(first, lo - timedelta(days=(lo.weekday() - data['organisation'].get('week_start', 0)) % 7))
                    horizon = hi + timedelta(days=7)
                else:
                    first = max(first, date(lo.year, lo.month, 1))
                    final = hi - timedelta(days=1)
                    horizon = date(final.year + final.month // 12, final.month % 12 + 1, 1)
                last = min(last, horizon) if last else horizon
            for start, end in window_blocks(first, last, alignment,
                                            rule.get('window_days'), data['organisation'].get('week_start', 0), anchor):
                if start < hi and lo < end:
                    window = dated(policy, starts_on=start.isoformat(), ends_on=end.isoformat(), window_days=None, window_alignment='rolling',
                                 **({'window_anchor_on': None, 'prorate_partial_windows': False}
                                    if anchor is not None or rule.get('prorate_partial_windows') else {}),
                                 **({'repeat': False} if repeat else {}))
                    if alignment == 'fixed':
                        full_start = anchor + timedelta(days=((start - anchor).days // rule['window_days']) * rule['window_days'])
                        full_end = full_start + timedelta(days=rule['window_days'])
                    elif alignment == 'calendar_week':
                        full_start = start - timedelta(days=(start.weekday() - data['organisation'].get('week_start', 0)) % 7)
                        full_end = full_start + timedelta(days=7)
                    else:
                        full_start = start.replace(day=1)
                        full_end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
                    window['_quota_full_window'] = (full_start.isoformat(), full_end.isoformat())
                    result.append(window)
                    if len(result) > MAX_ROWS:
                        return result
            continue
        days = rule.get('window_days')
        if not days:
            result.append(policy)
            continue
        # Every complete local-date window intersecting the candidate; no truncated
        # boundary windows or fixed-calendar-block substitute for a rolling rule.
        first = max(date.fromisoformat(rule['starts_on']), date.fromisoformat(w['starts_on']) - timedelta(days=days-1))
        last = date.fromisoformat(w['ends_on']) - timedelta(days=1)
        if rule.get('ends_on'):
            last = min(date.fromisoformat(rule['ends_on']) - timedelta(days=days), last)
        for offset in range((last-first).days+1):
            start = first + timedelta(days=offset)
            window = dated(policy, starts_on=start.isoformat(), ends_on=(start+timedelta(days=days)).isoformat(),
                           **({'repeat': False} if repeat else {}))
            result.append({**window, '_quota_full_window': (start.isoformat(), (start+timedelta(days=days)).isoformat())})
            if len(result) > MAX_ROWS:
                return result  # The caller rejects this bound; never certify a prefix.
    return result


def period_bounds(policy, zone):
    rule = policy["definition"]["rule"]
    # A DST transition at local midnight is not silently resolved by a default fold.
    return (local_stamp(date.fromisoformat(rule["starts_on"]), "00:00", zone),
            local_stamp(date.fromisoformat(rule["ends_on"]), "00:00", zone))


def quota_context_bounds(policy, zone, *, effective=None):
    """The session-based denominator needs the entire nominal window in the snapshot."""
    rule = policy['definition']['rule']
    if rule.get('proration_basis') == 'countable_session_days' and rule['comparison'] != 'maximum':
        full = policy.get('_quota_full_window')
        if full:
            first, last = (date.fromisoformat(day) for day in full)
            return local_stamp(first, '00:00', zone), local_stamp(last, '00:00', zone)
    return effective or period_bounds(policy, zone)


def membership_overlaps(row, start, end):
    return row["starts_on"] < end and (not row.get("ends_on") or start < row["ends_on"])


def duty_witness(data, scope, person_id, a, b, department_id, *, budget=None):
    """Reuse an identical subject witness during this report's proration."""
    if budget is not None and budget[0] <= 0:
        raise ScopeContextLimit('The report-wide quota subject context budget is exhausted')
    cache = data.get('_quota_witness_cache')
    if cache is None:
        return scope_has_duty_witness(data, scope, person_id, a, b, department_id, budget=budget)
    from .validation import every_day_all_day
    canonical = {k: v for k, v in scope.items() if v is not None and (v != [] or k == 'weekdays')}
    clocks = ('weekdays', 'start_time', 'end_time')
    if every_day_all_day({k: canonical[k] for k in clocks if k in canonical}):
        canonical = {k: v for k, v in canonical.items() if k not in clocks}
    key = (department_id, person_id, a, b, json.dumps(canonical, sort_keys=True))
    if key not in cache:
        cache[key] = scope_has_duty_witness(data, scope, person_id, a, b, department_id, budget=budget)
    return cache[key]


def departmental_dates(data, person_id, scope, first, last, *, budget=None):
    """Return present department days, excluding every validator-away kind covering local noon.

    Approved leave, recorded leave, and actual absence use the same away-day meaning as UNAVAILABLE.
    """
    from .conditions import rule_holds, _condition_points
    from .fairness import AWAY
    from .validation import category_scope_ids, person_rows
    ranks = category_scope_ids(scope)
    programme_id = scope.get('programme_id')
    tree, zone = scope_tree(scope), data['organisation']['timezone']
    budget = [MAX_SUBJECT_CONTEXTS] if budget is None else budget
    days = []
    a, b = local_midnight(date.fromisoformat(first), zone), local_midnight(date.fromisoformat(last), zone)
    points = set(_condition_points(data, scope.get('conditions'), person_id, a, b))
    # Keep this predicate correct even if a caller's condition points omit a dated person row.
    for name in ('memberships', 'rotations'):
        for row in person_rows(data, name, person_id):
            for field in ('starts_on', 'ends_on'):
                if row.get(field):
                    point = local_midnight(date.fromisoformat(str(row[field])), zone)
                    if a < point < b:
                        points.add(point)
    points = sorted(points)
    for start, end in zip(points, points[1:] + [b]):
        first_day, last_day = start.astimezone(ZoneInfo(zone)).date(), end.astimezone(ZoneInfo(zone)).date()
        held = rule_holds(data, person_id, None, start, end)
        categories = held('category_ids')
        if programme_id and programme_id not in held('programme_ids'):
            continue
        rotation_type = scope.get('rotation_type_id')
        if rotation_type and rotation_type not in held('rotation_type_ids'):
            continue
        if tree and not duty_witness(data, scope, person_id, start, end,
                                              data['workspace']['department_id'], budget=budget):
            continue
        memberships = person_rows(data, 'memberships', person_id)
        rotations = person_rows(data, 'rotations', person_id)
        availability = person_rows(data, 'availability', person_id)
        for n in range((last_day-first_day).days):
            day = first_day + timedelta(days=n)
            day_text = day.isoformat()
            day_start = local_midnight(day, zone)
            day_end = local_midnight(day + timedelta(days=1), zone)
            if not any(m['department_id'] == data['workspace']['department_id']
                       and m['category_id'] in categories
                       and (not ranks or m['category_id'] in ranks)
                       and m['starts_on'] <= day_text and (not m.get('ends_on') or day_text < m['ends_on'])
                       for m in memberships):
                continue
            if any(not r.get('department_available', True)
                   and r['starts_on'] <= day_text and (not r.get('ends_on') or day_text < r['ends_on'])
                   for r in rotations):
                continue
            if any(r.get('kind') == 'off_duty' and r.get('counts_as_unavailable', True)
                   and aware(r['start']) < day_end and day_start < aware(r['end'])
                   for r in availability):
                continue
            noon = local_stamp(day, '12:00', zone)
            if any(r.get('kind') in AWAY and aware(r['start']) <= noon < aware(r['end'])
                   for r in availability):
                continue
            days.append(day)
    return tuple(days)


def departmental_days(data, person_id, scope, first, last, *, budget=None):
    """Calendar-day count retained for existing person-consistency readers."""
    return len(departmental_dates(data, person_id, scope, first, last, budget=budget))


def calendar_working_dates(data, calendar_id, first, last, cache, *, weekend=None):
    from .resources import closed_intervals

    key = ('proration_calendar', calendar_id, tuple(weekend) if weekend is not None else None, first, last)
    if key not in cache:
        working = set()
        calendar = data.get('operating_calendars', {}).get(calendar_id)
        zone = data['organisation']['timezone']
        for n in range((last - first).days):
            day = first + timedelta(days=n)
            if weekend is not None:
                if day.weekday() not in weekend:
                    working.add(day)
                continue
            a, b = local_midnight(day, zone), local_midnight(day + timedelta(days=1), zone)
            if sum(duration_hours(x, y) for x, y in closed_intervals(calendar, a, b, data=data)) < duration_hours(a, b):
                working.add(day)
        cache[key] = working
    return cache[key]


def rotation_working_days(data, pid, present, first, last, department_days, cache):
    from .validation import person_rows

    department_id = data['workspace']['department_id']
    rotations = [row for row in person_rows(data, 'rotations', pid)
                 if row['department_id'] == department_id and row.get('service_id')]
    result = set()
    for day in present:
        active = [data.get('services', {}).get(row['service_id']) for row in rotations
                  if row['starts_on'] <= day.isoformat() < row['ends_on']]
        if any(clinic is None or clinic['department_id'] != department_id for clinic in active):
            raise ValueError('A rotation clinic is unavailable in this quota window')
        calendars = {clinic.get('calendar_id') for clinic in active}
        if not calendars or None in calendars:
            if day in department_days:
                result.add(day)
        for calendar_id in calendars - {None}:
            calendar = data.get('operating_calendars', {}).get(calendar_id)
            if not calendar or calendar['department_id'] != department_id or not calendar.get('active', True):
                raise ValueError('A rotation clinic needs an active operating calendar in this department')
            if day in calendar_working_dates(data, calendar_id, first, last, cache):
                result.add(day)
    return len(result)


def countable_session_days(data, policy, pid, present, first, last, cache, budget):
    from .finding_causes import session_prefill_scope

    rule = policy['definition']['rule']
    full = dated(policy, starts_on=first.isoformat(), ends_on=last.isoformat(), comparison='maximum')
    selector = {**full, 'definition': {**full['definition'], 'scope': session_prefill_scope(full['definition']['scope'])}}
    day_policy = full['definition']['rule']
    key = ('proration_sessions', first, last, json.dumps([
        selector['definition']['scope'], policy.get('starts_on'), policy.get('ends_on'),
        {f: day_policy[f] for f in ('metric', 'assignment_type_ids', 'participation', 'overtime')}],
        sort_keys=True))
    zone = ZoneInfo(data['organisation']['timezone'])
    a, b = period_bounds(full, zone.key)

    def can_count(check, person, shift):
        budget[0] -= 1
        if budget[0] < 0:
            raise ScopeContextLimit('Countable session days exceed the quota scope budget')
        data.get('_check_budget', lambda: None)()
        credit = quota_opportunity(data, check, person, shift, a, b, subject=pid)
        metric = rule['metric']
        if credit is None:
            return False
        if metric in {'duties', 'lead_turns'}:
            return credit['duty']
        if metric == 'session_units':
            return credit['session_units'] > 0
        if metric.endswith('credit'):
            return credit['credits'] > 0
        return bool(credit['intervals'])

    if key not in cache:
        days = defaultdict(list)
        for shift in data['shifts']:
            if inactive(shift) or data['types'][shift['assignment_type_id']]['department_id'] != data['workspace']['department_id']:
                continue
            day = aware(shift['start']).astimezone(zone).date()
            if first <= day < last and can_count(selector, None, shift):
                days[day].append(shift)
        cache[key] = days
    days = cache[key]
    # The full-window session pattern is shared. The person still has to match the rule where they can work.
    personal = dated(full, starts_on=rule['starts_on'], ends_on=rule['ends_on'])
    return (sum(any(can_count(personal, pid, shift) for shift in days.get(day, ())) for day in present), len(days))


def window_proration(data, policy, pid, amount, cache, budget):
    """Resolve a person's lower bound once, before any scheduler consumes the report."""
    from .conditions import quota_day_scope
    rule = policy['definition']['rule']
    if rule['comparison'] == 'maximum':
        return amount, None
    first, last = policy.get('_quota_full_window') or (rule['starts_on'], rule['ends_on'])
    lo, hi = date.fromisoformat(first), date.fromisoformat(last)
    w = data['workspace']
    start, stop = max(first, rule['starts_on'], w['starts_on']), min(last, rule['ends_on'], w['ends_on'])
    start = max(start, policy.get('starts_on') or start)
    stop = min(stop, policy.get('ends_on') or stop)
    scope = quota_day_scope(policy['definition']['scope'])
    key = ('proration_scope', pid, json.dumps(scope, sort_keys=True), start, stop)
    if key not in cache:
        cache[key] = departmental_dates(data, pid, scope, start, stop, budget=budget)
    present = cache[key]
    basis = rule.get('proration_basis', DEFAULT_QUOTA_PRORATION_BASIS)
    if basis == DEFAULT_QUOTA_PRORATION_BASIS and len(present) == (hi-lo).days:
        return amount, None  # Full windows retain even fractional configured amounts.
    if basis == 'countable_session_days':
        days, full_days = countable_session_days(data, policy, pid, present, lo, hi, cache, budget)
        # With no matching sessions the configured minimum remains visible for the shortfall check.
        return prorated_window(amount, days, full_days, first, last, None, basis)
    department = data.get('department', {})
    calendar_id = department.get('operating_calendar_id')
    calendar = data.get('operating_calendars', {}).get(calendar_id)
    if calendar_id is not None:
        if not calendar or calendar.get('department_id') != w['department_id'] or not calendar.get('active', True):
            raise ValueError('The selected department operating calendar must be active and belong to this department')
        weekend = None
    else:
        weekend = department.get('weekend_days', [4, 5])
        if not isinstance(weekend, list) or any(type(day) is not int or not 0 <= day <= 6 for day in weekend):
            raise ValueError('Department weekend days cannot be resolved')
    working = calendar_working_dates(data, calendar_id, lo, hi, cache, weekend=weekend)
    if not working:
        raise ValueError('The department working-day definition has no working days in this quota window')
    days = (rotation_working_days(data, pid, present, lo, hi, working, cache) if basis == 'rotation_weekdays'
            else len(working.intersection(present)))
    return prorated_window(amount, days, len(working), first, last, calendar_id, basis)


def prorated_window(amount, days, full_days, first, last, calendar_id, basis):
    if days >= full_days:
        return amount, None
    prorated = min(amount, max(0, math.ceil(Fraction(str(amount)) * days / full_days)))
    return prorated, {'configured_amount': amount, 'amount': prorated, 'working_days': days,
        'full_working_days': full_days, 'starts_on': first, 'ends_on': last, 'calendar_id': calendar_id, 'rounding': 'up',
        **({'basis': basis} if basis != DEFAULT_QUOTA_PRORATION_BASIS else {})}


def quota_opportunity(data, policy, pid, shift, a, b, *, subject):
    from .validation import participation_for

    rule = policy['definition']['rule']
    participant = {'id': 'opportunity', 'person_id': pid or subject, 'start': shift['start'], 'end': shift['end'],
                   'participation': participation_for(data, pid, shift, aware(shift['start']), aware(shift['end']))
                       if pid is not None else (rule['participation'] or ['staff'])[0],
                   'overtime': rule['overtime'] == 'only', 'team_lead': rule['metric'] == 'lead_turns'}
    return quota_contribution(data, policy, pid, shift, participant, [], a, b)


def quota_contribution(data, policy, pid, shift, participant, members, a, b):
    """Shared attribution of one whole assignment to one canonical quota window.

    Returns None when not counted. Invalid planned presence raises ValueError so
    callers cannot accidentally treat malformed source data as zero contribution.
    """
    from .validation import ancestor_ids
    rule, scope = policy['definition']['rule'], policy['definition']['scope']
    if rule.get('comparison') in {'minimum', 'target'}:
        zone = data['organisation']['timezone']
        first, last = data['workspace']['starts_on'], data['workspace']['ends_on']
        if rule['comparison'] == 'minimum' and shift.get('workspace_id', data['workspace']['id']) != data['workspace']['id']:
            if not any(source['workspace_id'] == shift['workspace_id']
                       for source in data.get('quota_context', {}).get('publications', [])):
                return None
            first, last = rule['starts_on'], rule['ends_on']
        a = max(a, local_midnight(date.fromisoformat(max(first, policy.get('starts_on') or first)), zone))
        b = min(b, local_midnight(date.fromisoformat(min(last, policy.get('ends_on') or last)), zone))
        if a >= b:
            return None
    p = participant
    if (rule['assignment_type_ids'] and shift['assignment_type_id'] not in rule['assignment_type_ids']
            or rule['participation'] and p.get('participation', 'staff') not in rule['participation']
            or rule['overtime'] == 'exclude' and p.get('overtime', False)
            or rule['overtime'] == 'only' and not p.get('overtime', False)
            or rule['metric'] == 'lead_turns' and not p.get('team_lead')
            or scope.get('service_id') and scope['service_id'] not in shift['service_ids']
            or scope.get('location_id') and scope['location_id'] not in ancestor_ids(data, shift['location_id'])):
        return None
    start, end = aware(p['start']), aware(p['end'])
    if policy.get('_live_zero'):
        zone = data['organisation']['timezone']
        if policy.get('starts_on'):
            a = max(a, local_midnight(date.fromisoformat(policy['starts_on']), zone))
        if policy.get('ends_on'):
            b = min(b, local_midnight(date.fromisoformat(policy['ends_on']), zone))
        if a >= b:
            return None
    if not overlaps(a, b, start, end):
        return None
    # Credit requires the full scope throughout the assignment; its placement is owned at duty start.
    if not scope_matches_throughout(data, scope, pid, shift, start, end, rotation_at=start):
        return None
    windows = [(max(a, x), min(b, y)) for x, y in presence_intervals(p) if overlaps(a, b, x, y)]
    definition = shift['type_snapshot']['definition']
    duty = a <= start < b
    units = definition.get('session_units', 1) if duty else 0
    if definition.get('session_unit_basis', 'fixed') == 'proportional':
        units = definition.get('session_units', 1) * union_hours(windows) / duration_hours(
            aware(shift['start']), aware(shift['end']))
    intervals = [(max(a, start), min(b, end))] if rule['metric'] == 'reserved_hours' else windows
    credits = (union_hours(windows) * definition.get(
        rule['metric'] + '_per_hour', TypeDefinition.model_fields[rule['metric'] + '_per_hour'].default)
               if rule['metric'] in {'workload_credit', 'training_credit'} else 0)
    return {'duty': duty, 'session_units': units, 'intervals': intervals,
            'presence': windows, 'credits': credits}


def evaluate_quotas(data, add, ancestor_ids, exception_for, *, policy_ids=None, line_id=None, explain=None,
                    contribution_cache=None, person_shifts=None):
    """Report source policy IDs; internal consumers can match rows to exact evaluation copies."""
    policies = [policy for policy in (data['_quota_policies'] if '_quota_policies' in data else applicable_policies(data))
                if (policy_ids is None or policy['id'] in policy_ids)
                and (line_id is None or (policy.get('template_line') or {}).get('id') == line_id)]
    if data.get("quota_dates"):
        # Candidate checks: only quota windows that contain the session can change; full evaluations never set this.
        lo, hi = data["quota_dates"]
        policies = [p for p in policies if p["definition"]["rule"]["starts_on"] <= hi and lo < p["definition"]["rule"]["ends_on"]]
    if not policies:
        return None
    basis = BASIS
    if any(p['definition']['rule'].get('proration_basis', DEFAULT_QUOTA_PRORATION_BASIS) != DEFAULT_QUOTA_PRORATION_BASIS
           and p['definition']['rule']['comparison'] != 'maximum' for p in policies):
        basis = BASIS.replace(DEPARTMENT_PRORATION_TEXT,
            "Minimums and targets round up using their selected basis: department days, rotation weekdays or matching session days.")
    report = {"metric_version": METRIC_VERSION, "timezone": data["organisation"]["timezone"],
              "basis": basis, "fingerprint": data.get("fingerprint"),
              "source_state": data["workspace"]["status"], "rows": [], "complete": True,
              "source_publications": copy.deepcopy(data.get("quota_context", {}).get("publications", [])),
              "candidate_workspace_id": data["workspace"]["id"],
              "empty_policy_ids": []}
    from .generation_scope import planning_population_people
    planning_people = planning_population_people(data)
    overrides = {(row['policy_id'], row['person_id']): row
                 for row in reversed(data['workspace'].get('target_overrides') or ())}

    legacy_as_issued = report['source_state'] in {'submitted', 'approved', 'published', 'superseded'}

    def unresolved(code, message, policy=None, person=None):
        report["complete"] = False
        add(code, message, person=person, rule=policy["id"] if policy else None)

    def stale_target(override, rule_name, *, reconfirm):
        person = override['person_id']
        person_name = data['people'][person]['display_name']
        detail = {'adjustment_id': override.get('id'), 'person_name': person_name,
                  'rule_name': rule_name, 'amount': override['amount']}
        if reconfirm:
            reason = 'The rule content changed.' if override.get('rule_fingerprints') else 'Its accepted rule content needs verification.'
            message = f'Reconfirm the target adjustment for {person_name} under “{rule_name}”. {reason}'
            detail['reconfirm_command'] = 'workspace.target_override'
        else:
            message = f'Remove the target adjustment for {person_name} under “{rule_name}”. This rule has no adjustable target for this rota.'
        report['complete'] = False
        add('TARGET_OVERRIDE_STALE', message, person=person, rule=override['policy_id'], extra=detail)

    context = data.get("quota_context", {})
    if len({p['id'] for p in policies}) > data.get("quota_policy_limit", MAX_POLICIES) or len(policies) > MAX_ROWS or context.get("limit_exceeded"):
        unresolved("QUOTA_SCOPE_LIMIT", "Quota evaluation exceeds its explicit bounded scope; no truncated result certifies compliance")
        return report
    if context.get("complete") is not True:
        unresolved("QUOTA_CONTEXT_MISSING", "The complete quota block was not loaded; compliance is unresolved")
        return report
    try:
        context_start, context_end = aware(context["start"]), aware(context["end"])
        if context_start >= context_end:
            raise ValueError("Non-positive quota context")
    except (KeyError, ValueError, TypeError, AttributeError):
        unresolved("QUOTA_CONTEXT_MISSING", "Quota source boundaries are missing or invalid; compliance is unresolved")
        return report
    if len(data["shifts"]) > MAX_SHIFTS or sum(len(s["participants"]) for s in data["shifts"]) > MAX_PARTICIPANTS:
        unresolved("QUOTA_SCOPE_LIMIT", "Quota source exceeds 10,000 sessions or 40,000 participants; narrow or separately reconcile the scope")
        return report
    dept = data["workspace"]["department_id"]
    subject_context_budget = [MAX_SUBJECT_CONTEXTS]
    data = {**data, '_quota_subject_context_budget': subject_context_budget, '_quota_witness_cache': {}}
    proration_cache = data.get('_quota_cache', {})
    by_person = defaultdict(list)
    if person_shifts is None:
        for shift in data["shifts"]:
            if inactive(shift) or data["types"][shift["assignment_type_id"]]["department_id"] != dept:
                continue
            for participant in shift["participants"]:
                by_person[participant["person_id"]].append((shift, participant))
    else:
        for pid in data['quota_subjects']:
            by_person[pid] = [(shift, p) for shift in person_shifts(pid)
                              if data['types'][shift['assignment_type_id']]['department_id'] == dept
                              for p in shift['participants'] if p['person_id'] == pid]
    from .validation import category_scope_ids, named_people
    for policy in sorted(policies, key=lambda p: p["id"]):
        data.get('_check_budget', lambda: None)()
        policy_id = policy["id"]
        if not data.get('_quota_evaluation_ids') and (policy.get("_live_zero") or policy.get("_live_mandatory_recheck")):
            policy_id = policy.get("_source_policy_id", policy_id)
        rule, scope = policy["definition"]["rule"], policy["definition"]["scope"]
        try:
            a, b = period_bounds(policy, report["timezone"])
            needed_start, needed_end = quota_context_bounds(policy, report["timezone"], effective=(a, b))
        except ValueError:
            unresolved("QUOTA_TIME_AMBIGUOUS", "The quota block has an ambiguous or nonexistent local-midnight boundary", policy)
            continue
        if context_start > needed_start or context_end < needed_end:
            unresolved("QUOTA_CONTEXT_MISSING", "The complete configured quota interval was not loaded", policy)
            continue
        from .validation import scope_intervals
        # Who a quota window counts depends on the rule and the people's records, never on who works: a scheduling run
        # (generation.py) keeps it for the whole run instead of working it out at every trial.
        cache = data.get("_quota_cache")
        known = cache.get((id(scope), rule["starts_on"], rule["ends_on"])) if cache is not None else None
        if known is not None and known[0] is scope:
            members, subjects = known[1], set(known[2])
        else:
            members = [m for m in data["memberships"] if m["department_id"] == dept
                       and membership_overlaps(m, rule["starts_on"], rule["ends_on"])
                       and (not category_scope_ids(scope) or m["category_id"] in category_scope_ids(scope))]
            named = named_people(scope)
            subjects = {m["person_id"] for m in members if named is None or m["person_id"] in named}
            if data.get("quota_subjects") is not None and cache is None:
                # Candidate checks need only the candidate's own rows; full evaluations never set this.
                subjects &= set(data["quota_subjects"])
            try:
                subjects = {pid for pid in sorted(subjects) if scope_intervals(data, scope, pid, a, b)
                            and duty_witness(data, scope, pid, a, b, dept, budget=subject_context_budget)}
            except ScopeContextLimit:
                unresolved('QUOTA_SCOPE_LIMIT', 'Clinic-scoped quota conditions exceed the bounded scope check; '
                           'narrow the rule scope or separately reconcile this quota before claiming compliance', policy)
                if subject_context_budget[0] <= 0:
                    return report
                continue
            if cache is not None:
                cache[(id(scope), rule["starts_on"], rule["ends_on"])] = (scope, members, frozenset(subjects))
        if data.get("quota_subjects") is not None:
            subjects &= set(data["quota_subjects"])
        if planning_people is not None and rule["comparison"] in {"minimum", "target"}:
            subjects &= planning_people
        if len(subjects) > MAX_SUBJECTS or len(report["rows"]) + len(subjects) > MAX_ROWS:
            unresolved("QUOTA_SCOPE_LIMIT", "Quota subject/report bound exceeded (2,000 people per rule; 10,000 rows); no truncated certification", policy)
            continue
        if not subjects:
            report["empty_policy_ids"].append(policy_id)
        rule_fingerprint = policy.get('_target_rule_fingerprint')
        for pid in sorted(subjects):
            data.get('_check_budget', lambda: None)()
            # Assignment-only trial cache: targets, exceptions, bounds and report completeness stay fresh below.
            signature = (a, b, tuple((id(s), repr(p)) for s, p in by_person[pid])) if contribution_cache is not None else None
            known = contribution_cache.get((id(policy), pid)) if contribution_cache is not None else None
            if known is not None and known[0] is policy and known[1] == signature:
                intervals, credits, duties, invalid, session_units = known[3]
            else:
                intervals, credits, duties, invalid = [], 0.0, set(), False
                session_units = {}
                for shift, p in by_person[pid]:
                    try:
                        contribution = quota_contribution(data, policy, pid, shift, p, members, a, b)
                    except ValueError:
                        invalid = True
                        continue
                    if contribution is None:
                        continue
                    if contribution['duty']:
                        duties.add((shift['id'], p['id']))
                    session_units[(shift['id'], p['id'])] = contribution['session_units']
                    intervals.extend(contribution['intervals'])
                    credits += contribution['credits']
                if contribution_cache is not None:
                    contribution_cache[id(policy), pid] = (policy, signature, tuple(s for s, _ in by_person[pid]),
                        (intervals, credits, duties, invalid, session_units))
            metric, comparison, amount = rule["metric"], rule["comparison"], rule["amount"]
            calculation = None
            if rule.get('prorate_department_days'):
                full_start, full_end = policy.get('_quota_full_window') or (rule['starts_on'], rule['ends_on'])
                full_days = (date.fromisoformat(full_end) - date.fromisoformat(full_start)).days
                raw = amount * full_days / rule['reference_days']
                amount = (math.floor(raw) if rule['rounding'] == 'down' else math.ceil(raw) if rule['rounding'] == 'up' else math.floor(raw + .5)) if metric in {'duties', 'lead_turns'} else round(raw, 4)
                calculation = {'default_amount': rule['amount'], 'reference_days': rule['reference_days'],
                               'department_days': full_days, 'unrounded_amount': round(raw, 4), 'rounding': rule['rounding'],
                               'calculated_amount': amount, 'rule_fingerprint': rule_fingerprint}
            override = overrides.get((policy['id'], pid)) if calculation and comparison == 'target' else None
            fingerprints = (override or {}).get('rule_fingerprints') or ()
            stale_override = bool(override and (fingerprints or not legacy_as_issued)
                                  and rule_fingerprint not in fingerprints)
            person_name = data['people'][pid]['display_name']
            if stale_override:
                stale_target(override, policy['name'], reconfirm=True)
                calculation['override_stale'] = True
            proration = None
            try:
                if override:
                    amount = override['amount']
                    calculation.update(override_amount=amount, override_reason=override['reason'])
                else:
                    amount, proration = window_proration(data, policy, pid, amount, proration_cache, subject_context_budget)
            except ScopeContextLimit:
                unresolved('QUOTA_SCOPE_LIMIT', 'Quota proration exceeds the bounded condition scope; narrow or reconcile this quota', policy)
                if subject_context_budget[0] <= 0:
                    return report
                continue
            except ValueError as error:
                unresolved('QUOTA_PRORATION_UNRESOLVED', str(error), policy, pid)
                continue
            actual = (len(duties) if metric in {"duties", "lead_turns"} else sum(session_units.values()) if metric == "session_units"
                      else credits if metric.endswith("credit") else union_hours(intervals))
            delta = actual - amount
            state = "met"
            if invalid:
                state = "unresolved"
                unresolved("QUOTA_PRESENCE_INVALID", "A counted duty has invalid planned-presence data; quota compliance is unresolved", policy, pid)
            elif stale_override:
                state = "unresolved"
            elif delta > 1e-9 and comparison in {"maximum", "target"}:
                state = "above_" + comparison
            elif delta < -1e-9 and comparison in {"minimum", "target"}:
                state = "below_" + comparison
            exc = exception_for(data, policy, pid, a, b) if state not in {"met", "unresolved"} else None
            row = {"policy_id": policy_id, "policy_revision": policy["revision"], "policy_name": policy["name"],
                   "person_id": pid, "display_name": person_name,
                   "starts_on": rule["starts_on"], "ends_on": rule["ends_on"], "metric": metric,
                   "comparison": comparison, "amount": amount, "actual": round(actual, 4),
                   "difference": round(delta, 4), "status": "excepted" if exc else state,
                   "severity": policy["definition"]["severity"], "strength": policy["definition"].get("strength", "medium"),
                   "balance_between_people": policy["definition"].get("balance_between_people", True),
                   "overtime": rule["overtime"],
                   "assignment_type_ids": list(rule["assignment_type_ids"]), "participation": list(rule["participation"]),
                   "scope": dict(policy.get('_count_scope', scope)), "exception_id": exc["id"] if exc else None,
                   "line": (policy.get("template_line") or {}).get("line"), "line_id": (policy.get("template_line") or {}).get("id"),
                   "slot": (policy.get("template_line") or {}).get("slot"),
                   **({'count_bucket': policy['count_bucket']} if 'count_bucket' in policy else {})}
            report["rows"].append(row)
            row['display'] = f"{row['actual']:g} of {amount:g}" + (
                f" (prorated: {proration['working_days']} of {proration['full_working_days']} days)" if proration else '')
            if proration:
                row['proration'] = proration
            if calculation is not None:
                row['target_calculation'] = calculation
            if state not in {"met", "unresolved"}:
                code = "RULE_EXCEPTION" if exc else QUOTA_DEVIATION_CODES[state]
                severity = "warning" if exc or comparison == "target" or row["severity"] == "advisory" else "error"
                add(code, "Approved full-block quota exception applies" if exc else policy["name"],
                    person=pid, a=a, b=b, severity=severity, rule=policy["id"], extra={
                        "actual": row["actual"], "amount": amount, "metric": metric,
                        "comparison": comparison, "difference": row["difference"], "exception_id": row["exception_id"],
                        'display': row['display'], **({'proration': proration} if proration else {}),
                        'rule_severity': row['severity'], 'rule_strength': row['strength'],
                        **({'slot': row['slot']} if row['slot'] is not None else {}),
                        **({'count_bucket': row['count_bucket']} if 'count_bucket' in row else {}),
                        # Keep each line's observation when the validator groups the visible issue.
                        **({"line": row["line"]} if row["line"] is not None else {})},
                    **({'explanation': explain(policy, pid, a, b, amount, state=state, actual=row['actual'], proration=proration)}
                       if explain is not None and not exc else {}))
    if overrides and policy_ids is None and line_id is None and data.get('quota_subjects') is None and not data.get('quota_dates'):
        # Scoped checks cannot establish that every applicable copy was examined.
        source_ids = {p['id']: p['_source_policy_id'] for p in policies
                      if p.get('_source_policy_id') and (p.get('_live_zero') or p.get('_live_mandatory_recheck'))}
        used, unused, policy_names = set(), {}, {}
        for row in report['rows']:
            policy_id = source_ids.get(row['policy_id'], row['policy_id'])
            policy_names.setdefault(policy_id, row['policy_name'])
            key = policy_id, row['person_id']
            if key not in overrides:
                continue
            if 'override_amount' in row.get('target_calculation', {}):
                used.add(key)
            else:
                unused.setdefault(key, row['policy_name'])
        for key, override in overrides.items():
            policy_id, person = key
            if key in used or policy_id not in policy_names or person not in data['people']:
                continue
            if planning_people is not None and person not in planning_people:
                continue
            if legacy_as_issued and not override.get('rule_fingerprints'):
                continue
            stale_target(override, unused.get(key, policy_names[policy_id]), reconfirm=False)
    # A template's default copy may match nobody while its per-person lines do.
    counted = {r["policy_id"] for r in report["rows"]}
    report["empty_policy_ids"] = [i for i in report["empty_policy_ids"] if i not in counted]
    report["rows"].sort(key=lambda row: (row["display_name"].casefold(), row["person_id"], row["policy_id"], counting_key(row)))
    return report


def project_report(report, person_id=None):
    """Personal exports never expose other people or cross-workspace source IDs."""
    if report.get("metric_version") not in {"block-quota-v1", "block-quota-v2", "block-quota-v3", "block-quota-v4", "block-quota-v5", METRIC_VERSION}:
        raise ValueError("Unsupported quota report version")
    result = copy.deepcopy(report)
    if person_id:
        result["rows"] = [r for r in result["rows"] if r["person_id"] == person_id]
        for row in result['rows']:
            row.get('target_calculation', {}).pop('override_reason', None)
        result.pop("source_publications", None)
        result.pop("empty_policy_ids", None)
    return result


def quota_csv(report, publication_id, sequence):
    from .reports import safe_cell
    out = io.StringIO(newline="")
    writer = csv.writer(out, quoting=csv.QUOTE_ALL)
    columns = ["policy_id", "policy_revision", "policy_name", "person_id", "display_name", "starts_on", "ends_on",
               "metric", "comparison", "amount", "actual", "difference", "status", "severity", "overtime",
               "assignment_type_ids", "participation", "scope", "exception_id"]
    if any('count_bucket' in row for row in report['rows']):
        columns.append('count_bucket')
    calculation_columns = ['default_amount', 'reference_days', 'department_days', 'calculated_amount', 'rounding', 'override_amount', 'override_reason']
    writer.writerow(["publication_id", "publication_sequence", "metric_version", "timezone", "basis", "complete", *columns, *calculation_columns])
    for row in report["rows"]:
        writer.writerow([safe_cell(x) for x in [publication_id, sequence, report["metric_version"], report["timezone"],
                                               report["basis"], report["complete"], *[row.get(k, '') for k in columns],
                                               *[row.get('target_calculation', {}).get(k, '') for k in calculation_columns]]])
    return out.getvalue()

"""Fairness ledger facts and Balance goals: one calculation for the validator, the score, the solver and the dashboard.

The ledger holds, per person and
local day, what each published rota gave them (duties, hours, session units, overtime, lead turns, worked days,
pairs) and the days they were available in the department. A goal compares each person's history plus this rota
with their aim: an equal share of the total pro rata to days available, a target, or a minimum per pair.
"""

from collections import defaultdict
from datetime import date, timedelta
import hashlib
import json
from fractions import Fraction
from math import ceil, gcd
import math
from functools import reduce
from zoneinfo import ZoneInfo

from .conditions import _month_shift, _source_points, change_points, conditions_match, counting_key, counting_views, scope_tree, selector_sources, scope_matches_throughout
from . import leadership
from .time import aware, duration_hours, local_midnight, local_stamp
from .score_normalization import goal_cost, share_satisfaction
from .recurrence import inactive

# v9: bind Fair share adjustments, scoped targets and offered-overtime distributions.
VERSION = 'fairness-v10'
# Stored with new publications; absent on issued evidence means the pre-D53 interpretation.
SEMANTICS = 2
# Ledger metric keys; a key may carry one filter: 'duties|type:<id>', 'duties|service:<id>' or 'duties|location:<id>'
# (the session's own place; a place and everything inside it is read as the keys of each place in that subtree).
BASE = ('duties', 'hours', 'session_units', 'overtime_duties', 'overtime_hours', 'lead_turns', 'days_worked')
AVAILABLE = 'available_days'
LEDGER_KEY = {'duties': 'duties', 'hours': 'hours', 'session_units': 'session_units', 'weekends': 'days_worked',
              'overtime_duties': 'overtime_duties', 'overtime_hours': 'overtime_hours', 'lead_turns': 'lead_turns',
              'supervision': 'supervision'}  # virtual measure: existing supervised rows, grouped by supervisor
UNIT_FORMS = {
    'duties': ('duty', 'duties'), 'hours': ('h', 'h'), 'session_units': ('unit', 'units'),
    'weekends': ('weekend', 'weekends'), 'overtime_duties': ('overtime duty', 'overtime duties'),
    'overtime_hours': ('overtime h', 'overtime h'), 'lead_turns': ('lead turn', 'lead turns'),
    'supervision': ('trainee session supervised', 'trainee sessions supervised'),
    'pair_exposure': ('session together', 'sessions together'),
    **dict.fromkeys(('wishes_submitted', 'wishes_granted', 'wishes_declined'), ('preference point', 'preference points')),
}
UNITS = {metric: plural for metric, (_, plural) in UNIT_FORMS.items()}
_SINGULAR_UNITS = {plural: singular for singular, plural in UNIT_FORMS.values()}

WISH_METRICS = ('wishes_submitted', 'wishes_granted', 'wishes_declined')  # read from the wish ledger, per rota period
SCALE = {'hours': 4, 'overtime_hours': 4, 'session_units': 100}  # solver integers: quarter hours, hundredths of a unit
AWAY = frozenset({'recorded_leave', 'approved_leave', 'actual_absence'})
GOAL_BAND_PARTS = 10
GOAL_BAND_UNITS = {'hours': 4, 'overtime_hours': 4}
COUNT_METRICS = frozenset({'duties', 'weekends', 'overtime_duties', 'lead_turns', 'supervision'})
GOAL_FINDINGS = frozenset({'BALANCE_GOAL', 'BALANCE_PAIR'})  # per person over the goal's period, not per session


def unit_for_amount(unit, amount):
    """Report headings use plural units; findings and Why agree with the displayed amount."""
    return _SINGULAR_UNITS.get(unit, unit) if amount == 1 else unit

def local_day(value, zone):
    return aware(value).astimezone(ZoneInfo(zone)).date()


def legacy_semantics(data):
    return data.get('fairness_semantics', SEMANTICS) < SEMANTICS


def measurement_context(data):
    """Effective dates select the goal for this rota, never truncate the quantity it measures."""
    if legacy_semantics(data) or data.get('_fair_share_measurement'):
        return data
    from .rule_lines import source_lines
    first, last = rota_dates(data)
    cache = data.get('_line_index_cache')
    key = ('fair_share_measurement', id(data['policies']), first, last)
    if cache is not None and key in cache:
        return {**data, **cache[key]}
    policies = []
    def measured(row):
        active = (not row.get('starts_on') or str(row['starts_on']) < str(last)) and (
            not row.get('ends_on') or str(row['ends_on']) > str(first))
        return {**row, 'starts_on': None if active else str(last), 'ends_on': None if active else str(last)}
    for policy in data['policies']:
        rule = policy['definition']['rule']
        if rule['kind'] == 'balance_goal' or rule.get('id') == 'fair_share':
            sources = (source_lines(policy) if rule['kind'] == 'catalogue'
                       else policy.get('_catalogue_sources', {}))
            policy = {**measured(policy), '_catalogue_sources': {k: measured(v) for k, v in sources.items()}}
        policies.append(policy)
    patch = {'policies': policies, '_fair_share_measurement': True, '_line_index_cache': {},
             '_scope_interval_cache': {}, '_policy_applies_cache': {}}
    if cache is not None:
        cache[key] = patch
    return {**data, **patch}


def _contributions(shift, participant):
    hours = duration_hours(participant['start'], participant['end'])
    overtime = bool(participant.get('overtime'))
    return {'duties': 1, 'hours': hours, 'session_units': shift['type_snapshot']['definition'].get('session_units', 1),
            'overtime_duties': int(overtime), 'overtime_hours': hours if overtime else 0,
            'lead_turns': int(bool(participant.get('team_lead'))), 'days_worked': 1}


def contribution(metric, shift, participant):
    """What one participation adds to a metric (weekends and pairs are counted elsewhere)."""
    if metric == 'supervision':
        return 1  # one event from metric_participants, on the named trainee's local start day
    return _contributions(shift, participant)[metric]


def metric_participants(metric, shift):
    """Supervision events retain the trainee's span/day but credit the named supervisor, just as the ledger does."""
    if metric != 'supervision':
        return shift['participants']
    people = {p['person_id']: p for p in shift['participants'] if p.get('person_id')}
    return [{**people.get(p['supervisor_person_id'], {}), 'person_id': p['supervisor_person_id'],
             'start': p['start'], 'end': p['end']}
            for p in shift['participants'] if p.get('person_id') and p.get('supervisor_person_id')]


def filters(shift):
    return ([''] + ['|type:' + shift['assignment_type_id']] + ['|service:' + s for s in shift['service_ids']]
            + (['|location:' + shift['location_id']] if shift.get('location_id') else []))


def places_within(locations, ids):
    """The chosen places and every place inside them (locations: id -> record with parent_id)."""
    chosen, found = set(ids), []
    for lid in locations:
        seen, current = set(), lid
        while current and current not in seen:
            if current in chosen:
                found.append(lid)
                break
            seen.add(current)
            current = (locations.get(current) or {}).get('parent_id')
    return found


def keys_for(rule):
    """The ledger keys a goal sums: its metric, once per chosen duty type or clinic."""
    if rule['metric'] == 'workload':
        return []  # The fixed cover budget belongs to this rota, with no historical ledger quantity.
    base = ('supervised' if rule.get('pairing') == 'supervised' else 'pair') if rule['metric'] == 'pair_exposure' else LEDGER_KEY[rule['metric']]
    if rule.get('assignment_type_ids'):
        return [base + '|type:' + t for t in rule['assignment_type_ids']]
    if rule.get('service_ids'):
        return [base + '|service:' + s for s in rule['service_ids']]
    if rule.get('location_ids'):
        return [base + '|location:' + s for s in rule['location_ids']]
    return [base]


def counts(rule, shift):
    return ((not rule.get('assignment_type_ids') or shift['assignment_type_id'] in rule['assignment_type_ids'])
            and (not rule.get('service_ids') or bool(set(rule['service_ids']) & set(shift['service_ids'])))
            and (not rule.get('location_ids') or shift.get('location_id') in rule['location_ids']))


def member_days(data, pid, first, last):
    """Local dates in [first, last) this person is a member of the rota's department, not away on an external
    rotation and not on leave (leave covering local noon counts the day as away). Kept like conditions.change_points
    when the data opt in (`_person_dates_cache`); never mutate the result."""
    cache = data.get('_person_dates_cache')
    if cache is not None:
        key = ('days', pid, first, last, data['workspace']['department_id'])
        if key not in cache:
            cache[key] = _member_days(data, pid, first, last)
        return cache[key]
    return _member_days(data, pid, first, last)


def _member_days(data, pid, first, last):
    from .validation import person_rows

    department, zone = data['workspace']['department_id'], data['organisation']['timezone']
    memberships = [r for r in person_rows(data, 'memberships', pid) if r['department_id'] == department]
    away_rotations = [r for r in person_rows(data, 'rotations', pid) if not r.get('department_available', True)]
    leave = [(aware(r['start']), aware(r['end'])) for r in person_rows(data, 'availability', pid) if r.get('kind') in AWAY]
    inside = lambda r, d: str(r['starts_on']) <= d and (not r.get('ends_on') or d < str(r['ends_on']))
    days, day = [], first
    while day < last:
        text = day.isoformat()
        if any(inside(r, text) for r in memberships) and not any(inside(r, text) for r in away_rotations):
            noon = local_stamp(day, '12:00', zone)
            if not any(a <= noon < b for a, b in leave):
                days.append(day)
        day += timedelta(days=1)
    return days


def offered_amounts(data, first, last, people=None):
    """Available offered hours in [first, last), from the scheduler's resolved overtime lines."""
    return {pid: sum((duration_hours(a, b) for a, b in intervals), 0.0)
            for pid, intervals in offered_intervals(data, first, last, people).items()}


def offered_intervals(data, first, last, people=None):
    """Disjoint available offer intervals in [first, last), from the resolved overtime lines.

    Source/clock/precedence resolution stays with the existing rule evaluator. A sweep unions
    offers and memberships and subtracts absences once, including partial days and overlaps.
    Index person records once; facts never rescan the department for each person or day.
    """
    from .conditions import active_rules, with_tags
    from .rule_groups import is_cap
    from .rule_lines import expand, fact_policy
    from .validation import index_person_rows, named_people, person_in_scope, person_rows, scope_intervals

    department, zone = data['workspace']['department_id'], data['organisation']['timezone']
    who = set(people if people is not None else (r['person_id'] for r in data.get('memberships', [])
                                               if r['department_id'] == department))
    available = {pid: [] for pid in who}
    if first >= last or not who:
        return available
    from .conditions import resolved_rule_context
    context = resolved_rule_context({**data, '_person_rows': data.get('_person_rows') or index_person_rows(data),
                         '_person_dates_cache': {}, '_scope_interval_cache': {}, '_line_index_cache': {}})
    start, end = local_midnight(first, zone), local_midnight(last, zone)
    offers = defaultdict(list)
    facts = [f for f in data.get('scheduling_facts', []) if f['kind'] == 'overtime_offer']
    successors = defaultdict(list)
    for fact in facts:
        if fact.get('replaces_id') and fact.get('status') in ('active', 'retired') and fact.get('approved_by'):
            successors[fact['replaces_id']].append(fact)

    def bounds(record):
        lo = max(start, local_midnight(date.fromisoformat(str(record['starts_on'])), zone)) if record.get('starts_on') else start
        hi = min(end, local_midnight(date.fromisoformat(str(record['ends_on'])), zone)) if record.get('ends_on') else end
        return lo, hi

    def windows(policy, pid):
        lo, hi = bounds(policy)
        if lo >= hi:
            return []
        scope = policy['definition']['scope']
        points = sorted(set(change_points(context, pid, lo, hi)) | _source_points(
            selector_sources(context.get('policies', []), scope_tree(scope), context.get('_line_index_cache')),
            lo, hi, zone))
        intervals = [window for a, b in zip(points, points[1:] + [hi])
                     if person_in_scope(context, scope, pid, a, b)
                     for window in scope_intervals(context, scope, pid, a, b)]
        if policy.get('_overtime_dates') is None:
            return intervals
        days = [(local_midnight(date.fromisoformat(day), zone),
                 local_midnight(date.fromisoformat(day) + timedelta(days=1), zone))
                for day in policy['_overtime_dates']]
        return [(max(a, start), min(b, end)) for a, b in intervals for start, end in days
                if a < end and start < b]

    rules = context['policies']
    caps = {key for policy in rules if is_cap(policy)
            for key in [policy['id'], *(policy['definition'].get('merged_ids') or [])]}
    for policy in rules:
        if (policy['definition']['rule']['kind'] != 'allocation_overtime'
                or policy['definition']['rule'].get('volunteer_required', False)
                or policy.get('department_id') != department
                or not set(policy['definition']['rule'].get('required_policy_ids', [])) <= caps):
            continue
        named = named_people(policy['definition']['scope'])
        for pid in who if named is None else who & named:
            offers[pid].extend(windows(policy, pid))
    # Active facts are already resolved above; only retired facts add historical windows.
    for fact in facts:
        pid = fact['person_id']
        if (pid not in who or fact.get('department_id', department) != department
                or fact.get('status') != 'retired' or not fact.get('approved_by') or not fact.get('on', True)):
            continue
        lo, hi = bounds(fact)
        line_lo, line_hi = bounds(fact['data']['line'])
        replacements = successors.get(fact['id'], [])
        if replacements:
            cutoff = min(max(bounds(row)[0], bounds(row['data']['line'])[0]) for row in replacements)
        elif fact.get('retired_at'):
            cutoff = aware(fact['retired_at'])
        else:
            continue  # A retired version without lifecycle evidence cannot establish a past active window.
        hi = min(hi, cutoff)
        if max(lo, line_lo) >= min(hi, line_hi):
            continue
        for policy in expand(fact_policy(fact)):
            if (policy.get('active', True) and policy['definition']['rule']['kind'] == 'allocation_overtime'
                    and not policy['definition']['rule'].get('volunteer_required', False)):
                offers[pid].extend((max(a, lo), min(b, hi)) for a, b in windows(policy, pid)
                                   if max(a, lo) < min(b, hi))
    for pid, offered in offers.items():
        events = defaultdict(lambda: [0, 0, 0])

        def add(lo, hi, kind):
            lo, hi = max(start, lo), min(end, hi)
            if lo < hi:
                events[lo][kind] += 1
                events[hi][kind] -= 1

        for lo, hi in offered:
            add(lo, hi, 0)
        for row in person_rows(context, 'memberships', pid):
            if row['department_id'] == department:
                add(*bounds(row), 1)
        for row in person_rows(context, 'rotations', pid):
            if not row.get('department_available', True):
                add(*bounds(row), 2)
        for row in person_rows(context, 'availability', pid):
            if not row.get('retracted') and row.get('counts_as_unavailable', True):
                add(aware(row['start']), aware(row['end']), 2)
        counts, previous = [0, 0, 0], start
        for instant, changes in sorted(events.items()):
            if counts[0] and counts[1] and not counts[2]:
                available[pid].append((previous, instant))
            counts = [value + change for value, change in zip(counts, changes)]
            previous = instant
    return available


def ledger_facts(data):
    """Rows (person, other person, key, local day, value) for the rota in `data` (its own sessions only), plus
    the days each department member was available during it."""
    zone, focus = data['organisation']['timezone'], set(data['focus_ids'])
    rows = defaultdict(float)
    focus_shifts = [shift for shift in data['shifts'] if shift['id'] in focus and not inactive(shift)]
    shared_leads = leadership.lead_turn_pairs(data, shifts=focus_shifts)
    for shift in focus_shifts:
        people = [p for p in shift['participants'] if p.get('person_id')]
        for p in people:
            day = local_day(p['start'], zone)
            contributions = _contributions(shift, p).items()
            for suffix in filters(shift):
                for metric, value in contributions:
                    if metric == 'lead_turns' and (shift['id'], p['person_id']) not in shared_leads:
                        continue
                    if value:
                        rows[p['person_id'], None, metric + suffix, day] += value
                for q in people:
                    if q['person_id'] != p['person_id']:
                        rows[p['person_id'], q['person_id'], 'pair' + suffix, day] += 1
                if p.get('supervisor_person_id'):
                    rows[p['person_id'], p['supervisor_person_id'], 'supervised' + suffix, day] += 1
    # days_worked is 0/1 per day however many duties
    for key in [k for k in rows if k[2].startswith('days_worked')]:
        rows[key] = 1.0
    first, last = date.fromisoformat(str(data['workspace']['starts_on'])), date.fromisoformat(str(data['workspace']['ends_on']))
    department = data['workspace']['department_id']
    for pid in sorted({r['person_id'] for r in data.get('memberships', []) if r['department_id'] == department}):
        for day in member_days(data, pid, first, last):
            rows[pid, None, AVAILABLE, day] = 1.0
    return [(pid, other, key, day, round(value, 4)) for (pid, other, key, day), value in sorted(rows.items(), key=lambda kv: (kv[0][0], kv[0][1] or '', kv[0][2], kv[0][3]))]


def weekend_of(day, rule):
    """The first day of the weekend `day` is in, or None."""
    offset = (day.weekday() - rule.get('weekend_start', 4)) % 7
    return day - timedelta(days=offset) if offset < rule.get('weekend_days', 2) else None


def period_start(rule, first, block=None):
    """Where a goal's period begins, for a rota starting on `first` (the history runs from here to the rota)."""
    kind = rule.get('period', 'rota')
    if kind == 'rolling':
        return first  # history for a rolling window is taken from `period_bounds`
    if kind == 'month':
        return first.replace(day=1)
    if kind == 'quarter':
        return date(first.year, 3 * ((first.month - 1) // 3) + 1, 1)
    if kind == 'year':
        return date(first.year, 1, 1)
    if kind in {'custom', 'since'}:
        return date.fromisoformat(str(rule['starts_on']))
    if kind == 'block' and block:
        return block[0]
    return first


def period_bounds(rule, first, last, block=None, history_start=None):
    """(start, end) of a goal's whole period for a rota [first, last): history is [start, first)."""
    kind = rule.get('period', 'rota')
    if kind == 'all_history':
        return min(first, history_start or first), last
    if kind == 'rolling':
        start = (_month_shift(last, -rule['period_months']) if rule.get('period_months') is not None
                 else last - timedelta(days=rule['period_days']))
        return start, last
    start = period_start(rule, first, block=block)
    if kind == 'month':
        end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    elif kind == 'quarter':
        end = date(start.year + (start.month + 2) // 12, (start.month + 2) % 12 + 1, 1)
    elif kind == 'year':
        end = date(start.year + 1, 1, 1)
    elif kind == 'custom':
        end = date.fromisoformat(str(rule['ends_on']))
    elif kind == 'block' and block:
        end = block[1]
    else:
        end = last
    return start, end


def block_of(data, pid, first):
    """The rotation block (dates) a person is in when the rota starts."""
    from .validation import person_rows

    rows = person_rows(data, 'rotations', pid) if data.get('_person_rows') is not None else data.get('rotations', [])
    for r in rows:
        if r['person_id'] == pid and str(r['starts_on']) <= first.isoformat() < str(r.get('ends_on') or '9999-12-31'):
            return date.fromisoformat(str(r['starts_on'])), date.fromisoformat(str(r['ends_on']))
    return None


def rota_dates(data):
    w = data['workspace']
    return date.fromisoformat(str(w['starts_on'])), date.fromisoformat(str(w['ends_on']))


def goal_period(data, policy, pid):
    rule, workspace = policy['definition']['rule'], data['workspace']
    cache = data.get('_person_dates_cache')
    key = ('goal_period', id(policy), pid if rule.get('period') == 'block' else None,
           workspace['department_id'], workspace['starts_on'], workspace['ends_on'])
    hit = cache.get(key) if cache is not None else None
    if hit is not None and hit[0] is policy:
        return hit[1]
    first, last = rota_dates(data)
    block = block_of(data, pid, first) if rule.get('period') == 'block' else None
    history_start = data.get('fairness_history_start')
    bounds = policy.get('_period_override') or period_bounds(rule, first, last, block,
        date.fromisoformat(str(history_start)) if history_start else None)
    if cache is not None:
        cache[key] = policy, bounds
    return bounds


def goal_intervals(data, policy, pid, first, last):
    """The configured period's matching person intervals inside [first, last), in local dates."""
    cache = data.get('_person_dates_cache')
    if cache is None:
        return _goal_intervals(data, policy, pid, first, last)
    w = data['workspace']
    key = ('goal_intervals', id(policy), pid, first, last, w['department_id'], w['starts_on'], w['ends_on'])
    hit = cache.get(key)
    if hit is None or hit[0] is not policy:
        # Retain the policy so a later comparison copy cannot reuse its identity in this immutable read.
        hit = cache[key] = (policy, _goal_intervals(data, policy, pid, first, last))
    return hit[1]


def _goal_intervals(data, policy, pid, first, last):
    from .validation import person_rows

    data = measurement_context(data)

    if policy.get('_balance_sources'):
        intervals = sorted(interval for source in goal_sources(policy, pid)
                           for interval in goal_intervals(data, source, pid, first, last))
        result = []
        for lo, hi in intervals:
            if result and lo <= result[-1][1]:
                result[-1] = result[-1][0], max(hi, result[-1][1])
            else:
                result.append((lo, hi))
        return result
    start, end = goal_period(data, policy, pid)
    first, last = max(first, start), min(last, end)
    if legacy_semantics(data) and policy.get('starts_on'):
        first = max(first, date.fromisoformat(str(policy['starts_on'])))
    if legacy_semantics(data) and policy.get('ends_on'):
        last = min(last, date.fromisoformat(str(policy['ends_on'])))
    if first >= last:
        return []
    department, zone = data['workspace']['department_id'], data['organisation']['timezone']
    memberships = [r for r in person_rows(data, 'memberships', pid) if r['department_id'] == department]
    a, b = local_midnight(first, zone), local_midnight(last, zone)
    points = sorted(set(change_points(data, pid, a, b)) |
                    _source_points(selector_sources(data.get('policies', []), scope_tree(policy['definition']['scope']),
                                                   data.get('_line_index_cache')), a, b, zone))
    result = []
    for lo, hi in zip(points, points[1:] + [b]):
        first_day, last_day = local_day(lo, zone), local_day(hi, zone)
        if not (any(str(r['starts_on']) <= first_day.isoformat() and
                    (not r.get('ends_on') or last_day.isoformat() <= str(r['ends_on'])) for r in memberships)
                and scope_matches_throughout(data, policy['definition']['scope'], pid, None, lo, hi)):
            continue
        if result and result[-1][1] == first_day:
            result[-1] = (result[-1][0], last_day)
        else:
            result.append((first_day, last_day))
    return result


def people_matching(data, tree, scope, a, b):
    """Department members during [a, b) who match the scope (legacy person fields and conditions) at some moment."""

    department = data['workspace']['department_id']
    members = sorted({r['person_id'] for r in data.get('memberships', []) if r['department_id'] == department
                      and str(r['starts_on']) < str(data['workspace']['ends_on'])
                      and (not r.get('ends_on') or str(data['workspace']['starts_on']) < str(r['ends_on']))})
    result = []
    for pid in members:
        points = change_points(data, pid, a, b)
        if any((scope is None or scope_matches_throughout(data, scope, pid, None, lo, hi)) and (tree is None or conditions_match(data, tree, pid, None, lo, hi))
               for lo, hi in zip(points, points[1:] + [b])):
            result.append(pid)
    return result


def history_windows(data, policy, *, preload=False):
    """Per person, the dates [start, first) whose ledger history the goal adds (empty when it has none)."""
    rule, (first, last) = policy['definition']['rule'], rota_dates(data)
    if rule['metric'] == 'workload' or rule.get('period', 'rota') == 'rota':
        return {}  # Both are confined to this rota, so neither reads historical ledger quantities.
    context = goal_people(data, policy, preload=True) if preload else goal_people(data, policy)
    windows = {}
    for pid in context['who']:
        start, end = goal_period(data, policy, pid)
        end = min(end, first)
        if start < end:
            windows[pid] = (start, end)
    return windows


def lead_eligible(data, type_ids, people):
    """Of `people`, those who may lead at least one of this rota's sessions of these duty types (empty: any duty
    type with a team lead), checked at the session's time by the one lead evaluator."""
    from .validation import lead_qualified
    focus, left, found = set(data.get('focus_ids', ())), set(people), set()
    if not left:
        return found
    # History is checked before validation prepares its runtime rules. Resolve once for this immutable pass.
    data = dict(data)
    leadership.prepare(data)
    historical = data.get('_fair_share_history_shifts', [])
    considered = focus | {shift['id'] for shift in historical}
    for shift in [*data['shifts'], *historical]:
        if not left:
            break
        if shift['id'] not in considered or inactive(shift) or type_ids and shift['assignment_type_id'] not in type_ids:
            continue
        lead = leadership.summary(data, shift)
        if not lead:
            continue
        a, b = aware(shift['start']), aware(shift['end'])
        hits = {pid for pid in left if lead_qualified(data, pid, shift, lead, a, b)}
        found |= hits
        left -= hits
    return found


def supervision_eligible(data, policy, people):
    """Use the clinical supervisor evaluator for the fixed audience, including matching historical duties."""
    from .validation import authority_intervals, participation_for, qualified_supervisor, supervision_commitments
    context = {**data, '_supervision_commitments': supervision_commitments(data)}
    focus, left, found = set(data['focus_ids']), set(people), set()
    historical = data.get('_fair_share_history_shifts', [])
    considered = focus | {shift['id'] for shift in historical}
    for shift in [*data['shifts'], *historical]:
        if not left:
            break
        if shift['id'] not in considered or inactive(shift):
            continue
        a, b = aware(shift['start']), aware(shift['end'])
        day_bounds = {instant for span in authority_intervals(context, a, b) for instant in span}
        def matches(pid):
            points = sorted(day_bounds | set(change_points(context, pid, a, b)))
            return any(goal_applies(context, policy, pid, shift, lo, hi)
                       for lo, hi in zip(points, points[1:] + [b]) if lo < hi)
        hits = {pid for pid in left if matches(pid)
                and qualified_supervisor(context, shift, {'person_id': pid,
                    'participation': participation_for(context, pid, shift, a, b)}, a, b)}
        found |= hits
        left -= hits
    return found


def supervisable_partners(data, policy, shift, supervisor, participants):
    """Trainee attendance that makes this supervisor's session an opportunity (once per session).

    The report and compiler share the same dated clinical qualification and presence predicates;
    the compiler supplies candidate attendance, the report supplies assigned attendance.
    """
    from .validation import effective_type_definition, trainee_intervals, presence_intervals, qualified_supervisor
    pid = supervisor['person_id']
    for trainee in participants:
        if trainee['person_id'] == pid:
            continue
        intervals = trainee_intervals(data, shift, trainee)
        if not intervals and not effective_type_definition(data, shift).get('supervision') and trainee['participation'] == 'trainee':
            intervals = presence_intervals(trainee)
        if any(lo < hi and qualified_supervisor(data, shift, supervisor, lo, hi)
               and goal_applies(data, policy, pid, shift, lo, hi)
               for a, b in intervals for c, d in presence_intervals(supervisor)
               for lo, hi in [(max(a, c), min(b, d))]):
            yield trainee['person_id']


def supervisable_amounts(data, policy, people, *, shifts=None, windows=None):
    """Assigned, eligible sessions with at least one trainee; includes no chosen-pairing assumption."""
    from .validation import supervision_commitments
    focus = set(data['focus_ids'])
    shifts = ([s for s in data['shifts'] if s['id'] in focus] if shifts is None else shifts)
    context = {**data, '_supervision_commitments': supervision_commitments({**data, 'shifts': shifts})}
    amounts = dict.fromkeys(people, 0)
    for shift in shifts:
        if inactive(shift):
            continue
        for supervisor in shift['participants']:
            pid = supervisor['person_id']
            day = local_day(supervisor['start'], data['organisation']['timezone'])
            if pid not in amounts or windows is not None and not windows[pid][0] <= day < windows[pid][1]:
                continue
            if next(supervisable_partners(context, policy, shift, supervisor, shift['participants']), None) is not None:
                amounts[pid] += 1
    return amounts


def _goal_context_key(data, policy):
    """Request/run-local dated facts stay fixed; focused views and usual-person exceptions may change."""
    from .recurrence import series_definition
    cache = data.get('_distribution_cache')
    view = cache.get('goal_shift_view') if cache is not None else None
    focus = frozenset(data['focus_ids'])
    if view is None or view[0] is not data['shifts'] or view[1] != focus:
        shifts = [s for s in data['shifts'] if s['id'] in focus and not inactive(s)]
        usual = [s for s in shifts if series_definition(s).get('usual_assignees_fixed', False)]
        view = data['shifts'], focus, shifts, frozenset(s['id'] for s in shifts), usual
        if cache is not None:
            # The opt-in cache's duty facts are immutable; assignment trials may replace the view or its participants.
            # Retain one view only, so copied workload trials cannot accumulate whole rotas in the cache.
            cache['goal_shift_view'] = view
    shifts, focused_ids, usual = view[2:]
    if policy['definition']['rule']['metric'] == 'workload' and (
            any('active' not in person for person in data['people'].values())
            or any('requirements' not in s['type_snapshot'].get('definition', {}) for s in shifts)):
        return None  # Historical workload audiences depend on assigned people.

    def inputs(source):
        return ({key: source.get(key) for key in ('id', 'revision', 'name', 'definition', 'starts_on', 'ends_on',
                 'template_line', 'count_bucket', '_count_scope')},
                [inputs(s) for s in source.get('_balance_sources', [])])

    options = data['_score_options'] if '_score_options' in data else data.get('generation_options') or {}
    workspace, organisation = data['workspace'], data['organisation']
    return (repr(data.get('generation_scope')), repr(data.get('planning_population')),
            tuple(data['applied_planning_population']) if data.get('applied_planning_population') is not None else None,
            json.dumps(inputs(policy), sort_keys=True, default=str),
            workspace['department_id'], str(workspace['starts_on']), str(workspace['ends_on']),
            organisation['timezone'], organisation.get('week_start'), options.get('balance_measure', 'reserved_hours'),
            focused_ids, tuple(sorted(
                (s['id'], tuple(sorted(p['person_id'] for p in s['participants'])))
                for s in usual)))


def goal_people(data, policy, *, preload=False):
    cache = data.get('_distribution_cache') if not preload else None
    view = _goal_context_key(data, policy) if cache is not None else None
    key = 'goal_people', view
    if view is not None and key in cache:
        return cache[key]
    result = _goal_people(data, policy, preload=preload)
    if view is not None:
        cache[key] = result
    return result


def _goal_people(data, policy, *, preload=False):
    if policy.get('_balance_sources'):
        groups = [goal_people(data, source, preload=preload) for source in policy['_balance_sources']]
        return {key: sorted({pid for group in groups for pid in group[key]}) for key in ('who', 'partners')}
    zone = data['organisation']['timezone']
    first, last = rota_dates(data)
    rule = policy['definition']['rule']
    members = sorted({r['person_id'] for r in data.get('memberships', [])
                      if r['department_id'] == data['workspace']['department_id']})
    from .validation import named_people
    named = named_people(policy.get('_count_scope', policy['definition']['scope']))
    who = [pid for pid in members if (named is None or pid in named)
           and goal_intervals(data, policy, pid, goal_period(data, policy, pid)[0], last)]
    from .generation_scope import planning_population_people
    planning_people = planning_population_people(data)
    if planning_people is not None:
        who = [pid for pid in who if pid in planning_people]
    if policy['definition'].get('strength') == 'off':
        return {'who': [], 'partners': []}
    if preload:
        return {'who': who, 'partners': []}  # Conservative date-window loading precedes session/availability facts.
    who = eligible_people(data, policy, who)
    if rule['metric'] == 'lead_turns':  # lead turns are shared among the people who may lead, not everyone in scope
        eligible = set((data.get('fairness_history') or {}).get(goal_identity(policy), {}).get('people', {}))
        eligible |= lead_eligible(data, rule.get('assignment_type_ids'), [pid for pid in who if pid not in eligible])
        who = [pid for pid in who if pid in eligible]
    if rule['metric'] == 'supervision':
        eligible = set((data.get('fairness_history') or {}).get(goal_identity(policy), {}).get('people', {}))
        eligible |= supervision_eligible(data, policy, [pid for pid in who if pid not in eligible])
        who = [pid for pid in who if pid in eligible]
    partners = []
    if rule['metric'] == 'pair_exposure' and who:
        periods = [goal_period(data, policy, pid) for pid in who]
        start, end = min(start for start, _ in periods), min(last, max(end for _, end in periods))
        context = {**data, 'workspace': {**data['workspace'], 'starts_on': start.isoformat(), 'ends_on': end.isoformat()}}
        partners = people_matching(context, rule['partners'], None, local_midnight(start, zone), local_midnight(end, zone))
    return {'who': who, 'partners': partners}


def eligible_people(data, policy, people):
    """The fixed Fair share audience: people able to work a matching focused session."""
    from .conditions import resolved_rule_context
    data = resolved_rule_context(data)
    from .candidate_evidence import candidate_eligible, potential_candidate_people
    from .validation import person_rows
    focus = set(data['focus_ids'])
    focused = [s for s in data['shifts'] if s['id'] in focus and not inactive(s)]
    if (policy is not None and policy['definition']['rule']['metric'] == 'workload'
            and (any('requirements' not in s['type_snapshot'].get('definition', {}) for s in focused)
                 or any('active' not in person for person in data['people'].values()))):
        # Old publications used assigned people and symmetric deviations, before fixed cover budgets existed.
        assigned = {p['person_id'] for s in focused for p in s['participants']
                    if goal_applies(data, policy, p['person_id'], s, aware(p['start']), aware(p['end']))}
        return [pid for pid in people if pid in assigned]
    # Old publication snapshots predate qualification facts; retain their recorded audience.
    if any('active' not in person for person in data['people'].values()):
        return list(people)
    cache = data.get('_fair_share_eligibility', {})
    shifts = focused
    historical = data.get('_fair_share_history_shifts', []) if policy is not None else []
    historical_ids = {id(shift) for shift in historical}
    shifts = [(shift, data) for shift in shifts] + [
        (shift, {**data, 'locations': shift.get('_fair_share_locations', data.get('locations', {})),
                 '_policy_applies_cache': {}, '_scope_interval_cache': {}, '_bucket_keys_cache': {}})
        for shift in historical]
    history = (data.get('fairness_history') or {}).get(goal_identity(policy), {}) if policy else {}
    recorded = set(history.get('people', {})) | set(history.get('pairs', {}))
    potential = potential_candidate_people(data, (shift for shift, _ in shifts))
    if potential is not None:
        people = [pid for pid in people if pid in potential or pid in recorded]
    found = []
    spans, service_scopes, eligibility_contexts = {}, {}, {}
    for pid in people:
        if pid in recorded:
            found.append(pid)
            continue
        services = {row['service_id'] for row in person_rows(data, 'eligibilities', pid)}
        for shift, context in shifts:
            sid = id(shift)
            if sid not in historical_ids:
                if sid not in service_scopes:
                    service_scopes[sid] = set(shift['service_ids'])
                if not service_scopes[sid] <= services:
                    continue  # Every duty service requires a record before dated checks can succeed.
            if sid not in spans:
                spans[sid] = aware(shift['start']), aware(shift['end'])
            a, b = spans[sid]
            if policy and not goal_applies(context, policy, pid, shift, a, b):
                points = change_points(context, pid, a, b)
                if policy['definition']['rule']['metric'] == 'supervision':
                    from .validation import authority_intervals
                    points = sorted(set(points) | {instant for span in authority_intervals(context, a, b) for instant in span})
                if not any(goal_applies(context, policy, pid, shift, lo, hi)
                           for lo, hi in zip(points, points[1:] + [b]) if lo < hi):
                    continue
            if id(shift) in historical_ids and any(p.get('person_id') == pid for p in shift['participants']):
                found.append(pid)  # A matching published participation is recorded historical eligibility.
                break
            key = (pid, id(shift))
            if key not in cache:
                view = id(context), sid
                if view not in eligibility_contexts:
                    eligibility_contexts[view] = {**context,
                        'policies': data.get('_fair_share_eligibility_policies', context['policies'])}
                cache[key] = candidate_eligible(eligibility_contexts[view], pid, shift)
            if cache[key]:
                found.append(pid)
                break
    return found


def goal_dials(data, policy, pid):
    """Combine winning dated line dials by their available-day exposure, including recorded history."""
    if not policy.get('_balance_sources'):
        return policy['definition']['rule']
    first, last = rota_dates(data)
    history = data.get('fairness_history') or {}
    sources = []
    for source in goal_sources(policy, pid):
        if source['definition'].get('strength') == 'off':
            continue
        start, _ = goal_period(data, source, pid)
        intervals = goal_intervals(data, source, pid, start, last)
        if not intervals:
            continue
        current = sum(len(member_days(data, pid, max(first, lo), hi)) for lo, hi in intervals if hi > first)
        recorded = history.get(goal_identity(source), {}).get('people', {}).get(pid, {})
        prior = (Fraction(str(recorded['available'])) if 'available' in recorded else
                 sum(len(member_days(data, pid, lo, min(first, hi))) for lo, hi in intervals if lo < first))
        sources.append((source['definition']['rule'], Fraction(current) + prior))
    if not sources:
        return policy['definition']['rule']
    total = sum(exposure for _, exposure in sources)
    if not total:
        sources = [(rule, Fraction(1)) for rule, _ in sources]
        total = len(sources)
    percent = amount = Fraction(0)
    tolerances = []
    for rule, exposure in sources:
        dial = rule.get('share_adjustment') or {'mode': 'percent', 'value': 100}
        share = exposure / total
        percent += share * (Fraction(str(dial['value'])) if dial['mode'] == 'percent' else 100)
        amount += share * (Fraction(str(dial['value'])) if dial['mode'] == 'amount' else 0)
        if exposure:
            tolerances.append(tolerance_settings(rule))
    return {**policy['definition']['rule'],
            'share_adjustment': {'mode': 'percent', 'value': percent, '_amount': amount},
            'tolerance_percent': min(pct for pct, _ in tolerances),
            'tolerance_units': min(units for _, units in tolerances)}


def share_adjustment_terms(dial):
    """Canonical relative factor and signed offset, reusable with numeric or symbolic bases."""
    dial = dial or {'mode': 'percent', 'value': 100}
    return (Fraction(str(dial['value'])) / 100 if dial['mode'] == 'percent' else Fraction(1),
            Fraction(str(dial['value'])) if dial['mode'] == 'amount' else Fraction(str(dial.get('_amount', 0))))


def expected_share_terms(avail, adjustments):
    """Fixed share weights and signed offsets, shared with the integer compiler."""
    base = {pid: Fraction(str(value)) for pid, value in avail.items()}
    if not sum(base.values()):
        base = dict.fromkeys(base, Fraction(1))
    weights, amounts = {}, {}
    for pid, value in base.items():
        factor, amounts[pid] = share_adjustment_terms(adjustments.get(pid))
        weights[pid] = value * factor
    if not sum(weights.values()):
        weights = base
    total = sum(weights.values())
    return {pid: value / total for pid, value in weights.items()}, amounts


def expected_shares(total, avail, adjustments, *, terms=None):
    weights, amounts = expected_share_terms(avail, adjustments) if terms is None else terms
    adjusted = {pid: max(0, total * weight + amounts[pid]) for pid, weight in weights.items()}
    denominator = sum(adjusted.values())
    return {pid: total * value / denominator if denominator else total * weights[pid]
            for pid, value in adjusted.items()}


def tolerance_settings(rule):
    units = rule.get('tolerance_units')
    return rule.get('tolerance_percent', 10), GOAL_BAND_UNITS.get(rule['metric'], 1) if units is None else units


def scoring_settings(data):
    from .score import settings_of
    return settings_of(data['_score_options'] if '_score_options' in data else data.get('generation_options'))


def workload_measure(data):
    """The workload unit shared by the score, report and session drill."""
    options = data['_score_options'] if '_score_options' in data else data.get('generation_options') or {}
    return options.get('balance_measure', 'reserved_hours')


def workload_parameters(data, policy):
    """Fixed cover budget and per-person grid limits for an explicit workload-spread goal."""
    from .score_normalization import workload_limit, workload_normalization
    measure = workload_measure(data)
    cache = data.get('_distribution_cache')
    view = _goal_context_key(data, policy) if cache is not None else None
    key = 'workload_parameters', view
    if view is not None and key in cache:
        return cache[key]
    who = goal_people(data, policy)['who']
    audience, demand = workload_normalization(data, measure)
    legacy = demand is None
    who = [pid for pid in who if pid in audience] if demand is not None else who
    unit = 1 if measure == 'duties' else 3_600_000_000
    focus = set(data['focus_ids'])
    def matches(s):
        if not who:
            return False
        a, b = aware(s['start']), aware(s['end'])
        return any(goal_applies(data, policy, pid, s, a, b) for pid in who)

    shifts = [s for s in data['shifts'] if s['id'] in focus and not inactive(s) and matches(s)]
    if demand is None:
        demand = sum((1 if measure == 'duties' else (aware(p['end']) - aware(p['start'])) // timedelta(microseconds=1))
                     for s in shifts for p in s['participants'] if p['person_id'] in who)
    else:
        _, demand = workload_normalization({**data, 'shifts': shifts}, measure)
    quantities = [1] if measure == 'duties' else [
        (aware(s['end']) - aware(s['start'])) // timedelta(microseconds=1) for s in shifts]
    quantum = reduce(gcd, quantities, 0) or 1
    largest = max(quantities, default=1)
    dials = {pid: goal_dials(data, policy, pid) for pid in who}
    first, last = rota_dates(data)
    avail = ({pid: sum(len(member_days(data, pid, lo, hi))
                       for lo, hi in goal_intervals(data, policy, pid, first, last)) for pid in who}
             if policy['definition']['rule'].get('share_by', 'available_days') == 'available_days'
             else dict.fromkeys(who, 1))
    expected = expected_shares(Fraction(demand, unit), avail,
                               {pid: dial.get('share_adjustment') for pid, dial in dials.items()}) if who else {}
    limits = {}
    for pid, aim in expected.items():
        pct, minimum = tolerance_settings(dials[pid])
        limits[pid] = max(largest, quantum * ceil(aim * unit / quantum),
                          (aim + max(Fraction(str(minimum)), aim * Fraction(str(pct)) / 100)) * unit)
    # An unchanged singleton spread has no comparison; a custom overload bound can still matter.
    present = len(who) > 1 or (bool(who) and not legacy and demand > 0
                              and limits[who[0]] != workload_limit({**data, 'shifts': shifts}, measure, who, demand))
    result = {'people': who, 'demand': demand, 'limits': limits, 'expected': expected, 'present': present,
            'legacy': legacy,
            'unit': unit, 'measure': measure, 'quantum': quantum, 'largest': largest}
    if view is not None:
        cache[key] = result
    return result


def workload_report(data, policy, planned_values=None):
    from .score_normalization import share_satisfaction, workload_costs
    # Fixed preparation is shared within this report; default caches expire with the call.
    data = {**data, **{key: data.get(key, {}) for key in (
        "_distribution_cache", "_policy_applies_cache", "_workload_normalization_cache")}}
    parameters = workload_parameters(data, policy)
    who, demand, unit = parameters['people'], parameters['demand'], parameters['unit']
    loads = dict.fromkeys(who, 0)
    focus = set(data['focus_ids'])
    for shift in data['shifts']:
        if shift['id'] not in focus or inactive(shift):
            continue
        for p in shift['participants']:
            pid = p['person_id']
            if pid in loads and goal_applies(data, policy, pid, shift, aware(p['start']), aware(p['end'])):
                loads[pid] += (1 if parameters['measure'] == 'duties' else
                               (aware(p['end']) - aware(p['start'])) // timedelta(microseconds=1))
    if planned_values is not None:
        loads = {pid: planned_values[0].get(pid, 0) * unit for pid in who}
    emphasis = scoring_settings(data)['fairness_worst_off_emphasis']
    if parameters['legacy'] and who:
        value, _, costs, _ = target_share(
            {pid: (load / unit, parameters['expected'][pid]) for pid, load in loads.items()},
            dict.fromkeys(who, 1), emphasis)
        value = float(value)
    else:
        variation, costs = workload_costs(loads, demand, parameters['limits']) if who else (0, {})
        value = float(share_satisfaction(variation, max(costs.values(), default=0), emphasis=emphasis))
    source_rows = {}
    for source in policy.get('_balance_sources') or [policy]:
        for pid in goal_people(data, source)['who']:
            source_rows.setdefault(pid, []).append(source)
    rows = [{'person_id': pid, 'prior': 0, 'planned': loads[pid] / unit, 'actual': loads[pid] / unit,
             'expected': float(parameters['expected'][pid]),
             'low': float(parameters['expected'][pid]) if parameters['legacy'] else 0,
             'high': float(parameters['expected'][pid] if parameters['legacy'] else parameters['limits'][pid] / unit),
             'cost': costs[pid],
             'strengths': [source['definition'].get('strength', 'medium') for source in source_rows[pid]],
             'sources': [{'goal_id': goal_identity(source),
                          'strength': source['definition'].get('strength', 'medium'), 'severity': 'advisory',
                          'weight': source['definition']['rule'].get('weight', 1),
                          'line_id': (source.get('template_line') or {}).get('id'),
                          'line': (source.get('template_line') or {}).get('line'),
                          'slot': (source.get('template_line') or {}).get('slot'),
                          'count_bucket': source.get('count_bucket', [])} for source in source_rows[pid]],
             'status': ('below' if loads[pid] / unit < parameters['expected'][pid] else 'above')
                       if costs[pid] else 'within'} for pid in who]
    return {'policy_id': policy['id'], 'goal_id': goal_identity(policy), 'name': policy['name'],
            'audience_label': _goal_audience_label(data, policy),
            'metric': 'workload', 'aim': 'workload_spread', 'unit': 'duties' if unit == 1 else 'h',
            'weight': policy['definition']['rule'].get('weight', 1),
            'strength': policy['definition'].get('strength', 'medium'), 'history': False, 'period': 'rota',
            'people': rows, 'pairs': [], 'workload_spread': True, 'score_present': parameters['present'],
            'value': value, 'score': round(100 * value, 1),
            'line_id': (policy.get('template_line') or {}).get('id'),
            **{key: (policy.get('template_line') or {}).get(key) for key in ('line', 'slot')}}


def planned(data, policy, who=None, partners=None):
    """This rota's counts per person (and per pair) for a goal, from the rota's own sessions."""
    rule, zone = policy['definition']['rule'], data['organisation']['timezone']
    who = set(who if who is not None else goal_people(data, policy)['who'])
    first, last = rota_dates(data)
    periods = {pid: (max(first, start), min(last, end)) for pid in who
               for start, end in [goal_period(data, policy, pid)]}
    partner_tree = rule.get('partners')
    values, pairs, days = defaultdict(float), defaultdict(float), defaultdict(set)
    shared_leads = None
    if rule['metric'] == 'lead_turns':
        from . import leadership
        shared_leads = leadership.lead_turn_pairs(data)
    focus, partners = set(data['focus_ids']), set(partners or ())
    for shift in data['shifts']:
        data.get('_check_budget', lambda: None)()
        if shift['id'] not in focus or inactive(shift):
            continue
        for p in metric_participants(rule['metric'], shift):
            pid = p.get('person_id')
            if rule['metric'] == 'lead_turns' and (shift['id'], pid) not in shared_leads:
                continue
            if (pid not in who or not periods[pid][0] <= local_day(p['start'], zone) < periods[pid][1]
                    or not goal_applies(data, policy, pid, shift, aware(p['start']), aware(p['end']))):
                continue
            if rule['metric'] == 'weekends':
                weekend = weekend_of(local_day(p['start'], zone), rule)
                if weekend:
                    days[pid].add(weekend)
            elif rule['metric'] == 'pair_exposure':
                others = ([q['person_id'] for q in shift['participants'] if q['person_id'] != pid] if rule.get('pairing', 'present') == 'present'
                          else [p['supervisor_person_id']] if p.get('supervisor_person_id') else [])
                for q in others:
                    if q in partners and conditions_match(data, partner_tree, q, None, aware(shift['start']), aware(shift['end'])):
                        pairs[pid, q] += 1
            else:
                values[pid] += contribution(rule['metric'], shift, p)
    for pid, weekends in days.items():
        values[pid] = len(weekends)
    return values, pairs


def goal_applies(data, policy, pid, shift, a, b):
    """Source Where/Who scopes count a contribution once when any line in a comparison matches."""
    from .validation import person_free
    cache = data.get('_policy_applies_cache')
    if cache is None:
        return _goal_applies(data, policy, pid, shift, a, b)
    entry = cache.get(('goal', id(policy)))
    if entry is None or entry[0] is not policy:
        sources = policy.get('_balance_sources') or [policy]
        independent = not policy.get('_balance_people') and all(
            source['definition']['rule'].get('period') != 'block'
            and person_free(source['definition']['scope']) for source in sources)
        entry = cache['goal', id(policy)] = policy, independent, {}
    if not entry[1] and not data.get('_policy_applies_people'):
        return _goal_applies(data, policy, pid, shift, a, b)
    key = shift['id'], a, b, None if entry[1] else pid
    if key not in entry[2]:
        entry[2][key] = _goal_applies(data, policy, pid, shift, a, b)
    return entry[2][key]


def _goal_applies(data, policy, pid, shift, a, b):
    from .validation import policy_applies
    data = measurement_context(data)
    for source in goal_sources(policy, pid):
        rule = source['definition']['rule']
        start, end = goal_period(data, source, pid)
        scoped = source
        if not legacy_semantics(data):
            cache = data.get('_policy_applies_cache')
            entry = cache.get(('measurement_source', id(source))) if cache is not None else None
            if entry is None or entry[0] is not source:
                entry = source, {**source, 'starts_on': None, 'ends_on': None}
                if cache is not None:
                    cache['measurement_source', id(source)] = entry
            scoped = entry[1]
        if (start <= local_day(a, data['organisation']['timezone']) < end and counts(rule, shift)
                and policy_applies(data, scoped, pid, shift, a, b)):
            return True
    return False


def _report_context(data, policy):
    """Run-local immutable goal inputs; assignments and their counts never enter this cache."""
    cache = data.get('_distribution_cache')
    key = ('report_context', _goal_context_key(data, policy)) if cache is not None else None
    if cache is not None and key in cache:
        return cache[key]
    rule = policy['definition']['rule']
    first, last = rota_dates(data)
    people = goal_people(data, policy)
    history = (data.get('fairness_history') or {}).get(goal_identity(policy), {})
    prior = {pid: float(history.get('people', {}).get(pid, {}).get('value', 0)) for pid in people['who']}
    source_rows = defaultdict(list)
    for source in policy.get('_balance_sources') or []:
        report = goal_report(data, source, planned_values=({}, {}))
        metadata = {key: report.get(key) for key in ('goal_id', 'line_id', 'line', 'slot', 'strength', 'weight')}
        metadata['count_bucket'] = report.get('count_bucket', [])
        metadata['severity'] = source['definition']['severity']
        for row in report['people']:
            source_rows[row['person_id']].append({**row, 'strength': report['strength'], 'source': metadata})
    unit = UNITS[rule['metric']]
    line = policy.get('template_line') or {}
    base = {'policy_id': policy['id'], 'goal_id': goal_identity(policy), 'name': policy['name'],
            'audience_label': _goal_audience_label(data, policy), 'metric': rule['metric'], 'aim': rule['aim'], 'unit': unit,
            'period': rule.get('period', 'rota'), 'strength': policy['definition'].get('strength', 'medium'),
            **({'period_start': str(goal_period(data, policy, people['who'][0])[0]),
                'period_end': str(goal_period(data, policy, people['who'][0])[1])} if people['who'] else {}),
            'weight': rule.get('weight', 1),
            'share_by': rule.get('share_by', 'available_days'),
            'comparison': rule.get('comparison', 'within_group'),
            'history': bool(history), 'line_id': line.get('id'), 'line': line.get('line'), 'slot': line.get('slot'),
            'balance_between_people': policy['definition'].get('balance_between_people', True),
            'applies_to_nobody': not people['who'],
            **({'count_bucket': policy['count_bucket']} if 'count_bucket' in policy else {})}
    if policy.get('_balance_sources'):
        base['source_goal_ids'] = [goal_identity(source) for source in policy['_balance_sources']]
        base.update(line_id=None, line=None, slot=None)

    windows, avail, offered = {}, {}, {}
    if rule['metric'] != 'pair_exposure':
        for pid in people['who']:
            rota_days = sum(len(member_days(data, pid, start, end)) for start, end in goal_intervals(data, policy, pid, first, last))
            avail[pid] = rota_days + float(history.get('people', {}).get(pid, {}).get('available', 0))
            windows[pid] = rota_days
        offered = dict.fromkeys(people['who'], 0.0)
        if rule['metric'] in ('overtime_duties', 'overtime_hours'):
            intervals = defaultdict(list)
            for pid in people['who']:
                offered[pid] = float(history.get('people', {}).get(pid, {}).get('offered', 0))
                for interval in goal_intervals(data, policy, pid, first, last):
                    intervals[interval].append(pid)
            for (start, end), ids in intervals.items():
                for pid, amount in offered_amounts(data, start, end, ids).items():
                    offered[pid] += amount
    dials = {pid: goal_dials(data, policy, pid) for pid in people['who']} if rule['metric'] != 'pair_exposure' else {}
    offered_share = rule.get('share_by') == 'offered' and rule['aim'] != 'target'
    basis = offered if offered_share else avail if rule.get('share_by', 'available_days') == 'available_days' else dict.fromkeys(avail, 1)
    adjustments = {pid: dial.get('share_adjustment') if not offered_share or offered[pid] else None
                   for pid, dial in dials.items()}
    share_terms = expected_share_terms(basis, adjustments) if basis and rule['aim'] != 'target' else None
    if share_terms is not None:
        # Report totals already use float ledger values; prepare the same conversions once per immutable context.
        share_terms = tuple({pid: float(value) for pid, value in terms.items()} for terms in share_terms)
    context = people, history, prior, source_rows, base, windows, avail, offered, dials, share_terms
    if cache is not None:
        cache[key] = context
    return context


def _goal_audience_label(data, policy):
    """A stable, privacy-safe label for the people a goal compares."""
    if policy.get('_balance_sources'):
        labels = [_goal_audience_label(data, source) for source in policy['_balance_sources']]
        return ' + '.join(dict.fromkeys(labels))
    scope = policy['definition'].get('scope') or {}
    catalogues = {'category_ids': 'categories', 'programme_ids': 'programmes', 'rotation_type_ids': 'rotation_types',
                  'cohort_ids': 'cohorts', 'eligible_service_ids': 'services', 'capability_ids': 'capabilities'}

    def label(field, values):
        if field in ('person_id', 'person_ids'):
            return 'Selected people'
        if field == 'tags':
            return ', '.join(values)
        catalogue = catalogues.get(field)
        if not catalogue:
            return ''
        return ', '.join(data.get(catalogue, {}).get(value, {}).get('name') or 'Configured group' for value in values)

    def tree_label(node):
        if 'field' in node:
            name = label(node['field'], node.get('values', []))
            return name if node.get('op', 'any') == 'any' else 'Configured group' if name else ''
        names = [name for child in node.get('conditions', []) if (name := tree_label(child))]
        return (' / ' if node.get('match') == 'any' else ' · ').join(names)

    if scope.get('conditions'):
        return tree_label(scope['conditions']) or 'Everyone'
    names = [label(field, scope.get(field) or [scope[field[:-1]]]) for field in (*catalogues, 'person_ids', 'tags')
             if scope.get(field) or scope.get(field[:-1])]
    return ' · '.join(filter(None, names)) or 'Everyone'


def goal_report(data, policy, planned_values=None, *, _cost_only=False):
    """Per person (or pair): history, this rota, aim and band, normalized cost 0-1. `planned_values` lets the solver
    and a preview pass their own counts; by default they are read from the rota."""
    data = {**data, '_fair_share_eligibility': data.get('_fair_share_eligibility', {})}
    data.get('_check_budget', lambda: None)()
    rule, zone = policy['definition']['rule'], data['organisation']['timezone']
    if rule['metric'] == 'workload':
        return workload_report(data, policy, planned_values)
    if rule.get('distribution', 'people') != 'people':
        from .fairness_distribution import distribution_report
        return distribution_report(data, policy)
    first, last = rota_dates(data)
    people, history, prior, source_rows, base, windows, avail, offered, dials, share_terms = _report_context(data, policy)
    values, pairs = planned_values if planned_values is not None else planned(data, policy, people['who'], people['partners'])

    def prorated(pid):
        start, end = goal_period(data, policy, pid)
        return max(0.0, (min(last, end) - start).days) / max(1, (end - start).days)

    if rule['metric'] == 'pair_exposure':
        rows = []
        prior_pairs = history.get('pairs', {})
        for pid in people['who']:
            data.get('_check_budget', lambda: None)()
            if '_distribution_people' in data and pid not in data['_distribution_people']:
                continue
            aim = rule.get('person_amounts', {}).get(pid, rule.get('amount') or 0) * prorated(pid)
            for q in people['partners']:
                if q == pid:
                    continue
                before = float(prior_pairs.get(pid, {}).get(q, 0))
                actual = before + pairs.get((pid, q), 0)
                cost = min(1.0, max(0.0, aim - actual) / aim) if aim else 0.0
                rows.append({'person_id': pid, 'partner_id': q, 'prior': before, 'planned': pairs.get((pid, q), 0), 'actual': actual,
                             'expected': round(aim, 2), 'low': round(aim, 2), 'high': None, 'cost': goal_cost(cost),
                             'status': 'below' if cost else 'within'})
        report = {**base, 'people': [], 'pairs': rows}
        return {**report, 'score': round(100 * report_value(report), 1)}

    offered_share = rule.get('share_by') == 'offered' and rule['aim'] != 'target'
    actual = {pid: prior[pid] + values.get(pid, 0) for pid in people['who']}
    total = sum(actual.values())
    opportunities = {}
    if rule.get('share_by') == 'supervisable_sessions':
        opportunities = supervisable_amounts(data, policy, people['who'])
        opportunities = {pid: value + history.get('people', {}).get(pid, {}).get('supervisable_sessions', 0)
                         for pid, value in opportunities.items()}
        share_terms = expected_share_terms(opportunities, {pid: dial.get('share_adjustment') for pid, dial in dials.items()})
    shares = expected_shares(total, {}, {}, terms=share_terms) if share_terms is not None else {}
    expected = {}
    for pid in people['who']:
        if rule['aim'] == 'target':
            expected[pid] = (sum(row['expected'] for row in source_rows[pid]) if source_rows
                             else rule.get('person_amounts', {}).get(pid, rule.get('amount') or 0) * prorated(pid))
        elif offered_share and not offered[pid]:
            expected[pid] = 0.0
        else:
            expected[pid] = float(shares[pid])
    rows = []
    for pid in people['who']:
        e, x = expected[pid], actual[pid]
        low, high, tolerance = band(rule['metric'], e, dials[pid])
        no_offer = offered_share and not offered[pid]
        cost = goal_cost(0.0 if no_offer else outside(rule['metric'], x, e, rule['aim'], dials[pid], tolerance=tolerance))
        if _cost_only:
            # Greedy trials need canonical costs and source weights, without public display formatting.
            rows.append({'person_id': pid, 'cost': cost,
                         **({'sources': [row['source'] for row in source_rows[pid]]} if source_rows else {})})
            continue
        rows.append({'person_id': pid, 'prior': round(prior[pid], 2), 'planned': round(values.get(pid, 0), 2), 'actual': round(x, 2),
                     'expected': round(e, 2), 'low': round(low, 2), 'high': None if rule['aim'] == 'max_min' else round(high, 2),
                     'available_days': round(avail[pid], 1), 'rota_days': windows[pid], 'cost': cost,
                     'overtime_offered_hours': offered[pid],
                     **({'supervisable_sessions': opportunities[pid]} if opportunities else {}),
                     'status': 'within' if no_offer else status(rule['metric'], x, e, rule['aim'], dials[pid], tolerance=tolerance),
                     **({'strengths': [row['strength'] for row in source_rows[pid]],
                         'sources': [row['source'] for row in source_rows[pid]]} if source_rows else {})})
    report = {**base, 'people': rows, 'pairs': []}
    if _cost_only:
        return report
    return {**report, 'score': round(100 * report_value(report, strengths_of(data),
                              emphasis=scoring_settings(data)['fairness_worst_off_emphasis']), 1)}


def band(metric, expected, rule=None):
    """(low, high, tolerance) around an expected share.

    A zero-tolerance count goal still has an attainable integer band: floor/ceil of
    the fractional share.  The returned tolerance remains zero for score/display
    compatibility; status/outside use the asymmetric floor/ceil band.
    """
    pct, units = tolerance_settings(rule or {'metric': metric})
    tolerance = max(units, expected * pct / 100)
    if metric in COUNT_METRICS and pct == 0 and units == 0:
        return math.floor(expected), math.ceil(expected), 0
    return max(0.0, expected - tolerance), expected + tolerance, tolerance


def gap_of(value, expected, aim):
    return max(0.0, expected - value) if aim == 'max_min' else abs(value - expected)


def status(metric, value, expected, aim='equal_share', rule=None, *, tolerance=None):
    """'within' the band, or 'above' / 'below' it (a max_min aim is never above)."""
    if metric in COUNT_METRICS and rule is not None and tolerance_settings(rule) == (0, 0):
        low, high, _ = band(metric, expected, rule)
        return 'within' if value >= low and (aim == 'max_min' or value <= high) else 'above' if value > high else 'below'
    tolerance = band(metric, expected, rule)[2] if tolerance is None else tolerance
    return 'within' if gap_of(value, expected, aim) <= tolerance else 'above' if value > expected else 'below'


def outside(metric, value, expected, aim='equal_share', rule=None, *, tolerance=None):
    """Normalized cost 0-1: only the distance outside the band counts, over the aim (at least one unit)."""
    if metric in COUNT_METRICS and rule is not None and tolerance_settings(rule) == (0, 0):
        low, high, _ = band(metric, expected, rule)
        return min(1.0, max(0.0, low - value, value - high if aim != 'max_min' else 0.0) / max(1.0, expected))
    tolerance = band(metric, expected, rule)[2] if tolerance is None else tolerance
    return min(1.0, max(0.0, gap_of(value, expected, aim) - tolerance) / max(1.0, expected))


def share_value(rows, weights=None, emphasis=0.5):
    """1 when everyone is inside their band; else (1 - mean cost) times (1 - half the worst person's cost)."""
    if not rows:
        return 1.0
    weights = weights or dict.fromkeys((r['person_id'] for r in rows), 1)
    mean = sum(r['cost'] * weights[r['person_id']] for r in rows) / sum(weights[r['person_id']] for r in rows)
    return max(0.0, float(share_satisfaction(mean, max(r['cost'] for r in rows), emphasis=emphasis)))


def report_value(report, strengths=None, *, emphasis=0.5):
    """Unrounded ordinary-goal value, shared by its report and greedy marginal trials."""
    if report.get('workload_spread'):
        return report['value'] if report.get('score_present', True) else 1.0
    pairs = report['pairs']
    if pairs:
        return 1 - sum(row['cost'] for row in pairs) / len(pairs)
    return share_value(report['people'], report_weights(report, strengths) if strengths else None, emphasis)


def report_weights(report, strengths):
    """Line strength weighs a person's deviation; a grouped goal has their mean strength among other goals."""
    return {row['person_id']: (sum(strengths.get(source['strength'], 0) * source.get('weight', 1) for source in row['sources'])
                              if row.get('sources') else strengths.get(report['strength'], 0) * report.get('weight', 1))
            for row in report['people']}


def strengths_of(data):
    from packages.contracts.schemas import StrengthMultipliers
    return data.get('_allocation_strengths') or data.get('fairness_strengths') or StrengthMultipliers().model_dump()


def goal_identity(policy):
    """A counted goal's history and ledger key; uncounted keys remain unchanged."""
    if policy.get('_period_override') or policy.get('_comparison_scope'):
        original = {k: v for k, v in policy.items() if k not in {'_period_override', '_comparison_scope'}}
        identity = (goal_identity(original), policy.get('_period_override'), policy.get('_comparison_scope'))
        return policy['id'] + ':' + hashlib.sha256(json.dumps(identity, default=str).encode()).hexdigest()[:32]
    if policy.get('_balance_sources'):
        return policy['id'] + ':' + hashlib.sha256(json.dumps(sorted(
            goal_identity(source) for source in policy['_balance_sources'])).encode()).hexdigest()[:32]
    if 'count_bucket' not in policy:
        line = policy.get('template_line')
        return (policy['id'] + ':' + line['id'] + ':' + line['slot'] + ':' + policy['definition'].get('strength', 'medium')[0]
                if line else policy['id'])
    # Different catalogue lines and inherited levels can share a bucket. Keep their histories separate within
    # the existing 96-character ledger-key contract; source ids stay stable if an operator reorders the lines.
    line, definition = policy.get('template_line') or {}, policy['definition']
    identity = (counting_key(policy), line.get('id'), line.get('slot'),
                definition['severity'], definition.get('strength', 'medium'))
    digest = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()[:32]
    return policy['id'] + ':' + digest


def balance_goals(data):
    from .conditions import active_rules, _narrowed, OVERRIDDEN, OUTRANKED
    from .rule_lines import scoring_default_id, source_key
    # As the checks read rules (a catalogue Fair share rule's lines are goals); overrides as before: none.
    goals = [p for p in active_rules(data['policies'], overrides=False, context=data) if p['status'] == 'active' and p.get('active', True)
             and p['definition']['rule']['kind'] == 'balance_goal'
             and p['department_id'] == data['workspace']['department_id']
             and (not p.get('starts_on') or str(p['starts_on']) < str(data['workspace']['ends_on']))]
    first, last = rota_dates(data)
    # A dated line can still determine shares within a monthly or rolling goal's recorded history.
    goals = [p for p in goals if not p.get('ends_on') or
             str(period_bounds(p['definition']['rule'], first, last)[0] if legacy_semantics(data)
                 else first) < str(p['ends_on'])]

    def lead_default(policy):
        rule = policy['definition']['rule']
        types = rule.get('assignment_type_ids') or []
        identity = (policy.get('template_line') or {}).get('id', policy['id'])
        return (rule['metric'] == 'lead_turns' and len(types) == 1
                and identity == scoring_default_id(policy['department_id'], 'lead:' + types[0]))

    explicit = [p for p in goals if p['definition']['rule']['metric'] == 'lead_turns' and not lead_default(p)]
    for index, goal in enumerate(goals):
        if not lead_default(goal):
            continue
        [type_id] = goal['definition']['rule']['assignment_type_ids']
        replacements = defaultdict(set)
        for policy in explicit:
            types = policy['definition']['rule'].get('assignment_type_ids')
            if types and type_id not in types:
                continue
            line = policy.get('template_line')
            field = OUTRANKED if line and '_catalogue_sources' in policy else OVERRIDDEN
            replacements[field].add(source_key(policy, line) if field == OUTRANKED else policy['id'])
        if replacements:
            # Reuse dated scope exclusions so fallback survives outside each explicit goal's coverage.
            goals[index] = _narrowed(goal, [{'field': field, 'op': 'none', 'values': sorted(values)}
                                            for field, values in replacements.items()])
    return counting_views(data, goals)


def quota_target_key(row):
    """Fairness groups compare one rule/period/metric; disabling balance preserves exact line bounds."""
    from .quotas import quota_identity
    if not row.get('balance_between_people', True):
        return quota_identity(row)
    return (row['policy_id'], row['starts_on'], row['ends_on'], row['metric'], counting_key(row))


def quota_goal_id(policy_id, key):
    return policy_id + ':quota:' + hashlib.sha256(json.dumps(key, default=str).encode()).hexdigest()[:24]


def parse_quota_goal_id(value):
    """Parse the stable ``<policy>:quota:<24 hex>`` goal identity."""
    if not isinstance(value, str):
        return None
    policy_id, separator, rest = value.partition(':')
    marker, separator2, digest = rest.partition(':')
    if not separator or not policy_id or marker != 'quota' or not separator2 or len(digest) != 24:
        return None
    if any(char not in '0123456789abcdef' for char in digest.lower()):
        return None
    return policy_id, digest


def target_share(rows, weights, emphasis=0.5):
    """Quota/workload share loss and its attribution, with each line's strength weighing its proportions."""
    actual = sum(value * weights[pid] for pid, (value, _) in rows.items())
    expected = sum(amount * weights[pid] for pid, (_, amount) in rows.items())
    if not expected:
        parts = dict.fromkeys(rows, 0.0)
    elif not actual:
        parts = {pid: 0.5 * amount * weights[pid] / expected for pid, (_, amount) in rows.items()}
    else:
        parts = {pid: 0.5 * weights[pid] * abs(value / actual - amount / expected)
                 for pid, (value, amount) in rows.items()}
    variation = sum(parts.values())
    costs = {pid: min(1.0, abs(value - amount) / max(amount, 1)) for pid, (value, amount) in rows.items()}
    worst = max(costs, key=lambda pid: (costs[pid], pid))
    parts[worst] += emphasis * costs[worst] * (1 - variation)
    return share_satisfaction(variation, costs[worst], emphasis=emphasis), parts, costs, worst


def _pooled_group_key(policy):
    rule = policy['definition']['rule']
    return (policy['id'], rule['metric'], rule.get('period', 'rota'), rule.get('period_days'), rule.get('period_months'),
            rule.get('starts_on'), rule.get('ends_on'), rule['aim'], rule.get('share_by', 'available_days'),
            rule.get('weekend_start', 4) if rule['metric'] == 'weekends' else None,
            rule.get('weekend_days', 2) if rule['metric'] == 'weekends' else None, counting_key(policy))


def _precedence_groups(policies):
    """Use the resolver's measure/Where key; person dials inherit their matching broader audience.

    A person can belong to multiple tier-2 audiences: keep those comparisons separate,
    restricting the person's copy to each audience instead of merging the groups transitively.
    """
    from .conditions import OUTRANKED
    buckets, separate = {}, []
    for policy in policies:
        metadata = policy.get('_fair_share_group')
        if metadata is None:
            separate.append([policy])
        else:
            key = (policy['id'], metadata['where'], counting_key(policy))
            buckets.setdefault(key, []).append(policy)
    result = list(separate)
    for candidates in buckets.values():
        broad, people = {}, []
        for policy in candidates:
            metadata = policy['_fair_share_group']
            if metadata['tier'] == 3:
                people.append(policy)
            else:
                key = json.dumps(metadata['who'], sort_keys=True)
                broad.setdefault(key, []).append(policy)
        if not broad:
            singles = {}
            for policy in people:
                singles.setdefault(json.dumps(policy['_fair_share_group']['who'], sort_keys=True), []).append(policy)
            result.extend(singles.values())
            continue
        all_sources = sorted({p['_fair_share_group']['source'] for group in broad.values() for p in group})

        def restricted(person, audience, excluded):
            clauses = []
            if audience:
                clauses.append({'field': OUTRANKED, 'op': 'any', 'values': audience})
            if excluded:
                clauses.append({'field': OUTRANKED, 'op': 'none', 'values': excluded})
            definition = person['definition']
            scope = {**definition['scope'], 'conditions': {'match': 'all', 'conditions': [
                *([definition['scope']['conditions']] if definition['scope'].get('conditions') else []), *clauses]}}
            return {**person, '_comparison_scope': [audience, excluded],
                    'definition': {**definition, 'scope': scope},
                    **({'_count_scope': scope} if '_count_scope' in person else {})}

        for group in broad.values():
            tier = group[0]['_fair_share_group']['tier']
            audience = sorted({p['_fair_share_group']['source'] for p in group})
            higher = sorted({p['_fair_share_group']['source'] for other in broad.values() for p in other
                             if p['_fair_share_group']['tier'] > tier})
            result.append([*group, *(restricted(person, audience, higher) for person in people)])
        # A person not matching any broader selector keeps an independent comparison.
        for person in people:
            result.append([restricted(person, [], all_sources)])
    return result


def goal_groups(data, policies=None):
    """Source goals grouped for comparison; all readers share grouping and calendar windows."""
    pooled, scoped, separate = {}, [], []
    for policy in balance_goals(data) if policies is None else policies:
        if policy['definition'].get('strength') == 'off':
            continue
        rule = policy['definition']['rule']
        if rule['metric'] == 'pair_exposure' or rule.get('distribution', 'people') != 'people':
            separate.append([policy])
        elif (policy['definition'].get('balance_between_people', True) if legacy_semantics(data)
              else rule.get('comparison', 'within_group') == 'pooled'):
            pooled.setdefault(_pooled_group_key(policy), []).append(policy)
        elif not legacy_semantics(data):
            scoped.append(policy)
        else:
            separate.append([policy])
    groups = [*pooled.values(), *_precedence_groups(scoped), *separate]
    def nonempty_fragments(groups):
        # Filter after calendar expansion: a person override may start in a later month.
        groups = [[p for p in group if not p.get('_comparison_scope') or goal_people(data, p, preload=True)['who']]
                  for group in groups]
        return [group for group in groups if group]
    if legacy_semantics(data):
        return nonempty_fragments(groups)
    expanded = []
    first, last = rota_dates(data)
    for group in groups:
        rule = group[0]['definition']['rule']
        if rule.get('period') not in {'month', 'quarter', 'year'} or group[0].get('_period_override'):
            expanded.append(group)
            continue
        cursor = period_start(rule, first)
        while cursor < last:
            _, end = period_bounds(rule, cursor, last)
            expanded.append(group if cursor == period_start(rule, first) else
                            [{**p, '_period_override': (cursor, end)} for p in group])
            cursor = end
    return nonempty_fragments(expanded)


def comparison_goal(policies):
    from .validation import named_people
    if len(policies) == 1:
        return policies[0]
    people, broad = defaultdict(list), []
    for policy in policies:
        named = named_people(policy.get('_count_scope', policy['definition']['scope']))
        if named is None:
            broad.append(policy)
        else:
            for pid in named:
                people[pid].append(policy)
    return {**policies[0], '_period_override': policies[0].get('_period_override'),
            '_balance_sources': policies, '_balance_people': dict(people), '_balance_broad': broad}


def goal_sources(policy, pid):
    sources = (policy['_balance_people'].get(pid, []) + policy['_balance_broad']
               if policy.get('_balance_sources') else [policy])
    return [source for source in sources if source['definition'].get('strength') != 'off']


def goal_reports(data, groups=None, planned_values=None):
    """The validator, score, solver and dashboard compare the same across-line audiences and amounts."""
    data = {**data, '_fair_share_eligibility': data.get('_fair_share_eligibility', {})}
    return [goal_report(data, comparison_goal(policies), planned_values=planned_values)
            for policies in (goal_groups(data) if groups is None else groups)]


def quota_goal_reports(data, report=None):
    """Quota obligations keep their own findings; their targets share the same fairness report everywhere."""
    if report is None:
        from .quotas import evaluate_quotas
        from .validation import ancestor_ids, exception_for
        report = evaluate_quotas(data, lambda *args, **kwargs: None, ancestor_ids, exception_for)
    from .quotas import quota_identity
    groups = defaultdict(list)
    for row in (report or {}).get('rows', []):
        if row['comparison'] == 'target' and row['status'] != 'unresolved':
            groups[quota_target_key(row)].append(row)
    goals = []
    strengths = strengths_of(data)
    for key, rows in groups.items():
        values, weights, sources = defaultdict(lambda: [0.0, 0.0]), defaultdict(int), defaultdict(list)
        for row in rows:
            pid = row['person_id']
            values[pid][0] += row['actual']
            values[pid][1] += row['amount']
            weights[pid] += strengths[row.get('strength', 'medium')]
            sources[pid].append({'goal_id': quota_goal_id(row['policy_id'], quota_identity(row)),
                                **{key: row.get(key) for key in ('line_id', 'line', 'slot')},
                                'count_bucket': row.get('count_bucket', []),
                                'severity': row.get('severity', 'advisory'), 'strength': row.get('strength', 'medium')})
        value, _, costs, _ = target_share(values, weights, scoring_settings(data)['fairness_worst_off_emphasis'])
        people = [{'person_id': pid, 'prior': 0, 'planned': actual, 'actual': actual, 'expected': expected,
                   'low': expected, 'high': expected, 'cost': costs[pid],
                   'strengths': [source['strength'] for source in sources[pid]], 'sources': sources[pid],
                   'status': 'within' if costs[pid] == 0 else 'above' if actual > expected else 'below'}
                  for pid, (actual, expected) in sorted(values.items())]
        first = rows[0]
        # A retained publication quota can outlive the active policy row. Keep the
        # score/report readable; the row still carries its immutable name and target.
        policy = next((policy for policy in data['policies'] if policy['id'] == first['policy_id']), None)
        source_ids = sorted({source['goal_id'] for person in sources.values() for source in person})
        goals.append({'policy_id': first['policy_id'], 'goal_id': quota_goal_id(first['policy_id'], key),
                      'audience_label': _goal_audience_label(data, policy) if policy else 'Configured group',
                      'name': first['policy_name'] + ' target', 'metric': first['metric'], 'aim': 'target',
                      'unit': first['metric'].replace('_', ' '), 'period': 'custom',
                      'starts_on': first['starts_on'], 'ends_on': first['ends_on'], 'strength': first.get('strength', 'medium'),
                      'balance_between_people': first.get('balance_between_people', True), 'quota_target': True,
                      'history': False, 'people': people, 'pairs': [], 'score': round(100 * value, 1),
                      **{key: first.get(key) if len(source_ids) == 1 else None for key in ('line_id', 'line', 'slot')},
                      **({'source_goal_ids': source_ids} if len(source_ids) > 1 else {}),
                      **({'count_bucket': first['count_bucket']} if 'count_bucket' in first else {})})
    return goals


def evaluate_goals(data, add, quota_report=None):
    """Validator findings: one advisory finding per person (or pair) outside the goal's band. Returns the reports."""
    reports = []
    names = lambda pid: data['people'].get(pid, {}).get('display_name', 'Someone')
    policies = {policy['id']: policy for policy in data['policies'] + balance_goals(data)}
    groups = goal_groups(data)
    if data.get('person_rule_subjects') is not None:
        # Reduced candidate checks consume findings, not department workload scores.
        groups = [group for group in groups if group[0]['definition']['rule']['metric'] != 'workload']
    for report in goal_reports(data, groups) + quota_goal_reports(data, quota_report):
        policy = policies[report['policy_id']]
        reports.append(report)
        if report.get('applies_to_nobody'):
            add('BALANCE_GOAL', f"{policy['name']}: applies to nobody in this rota", person=None,
                severity='warning', rule=None if policy.get('builtin') else policy['id'],
                extra={'applies_to_nobody': True})
            continue
        if report.get('workload_spread'):
            continue  # Workload spread keeps the former built-in's scoring-only behavior.
        for r in report['people']:
            if r['status'] != 'within':
                aim = 'less than 1' if 0 < r['expected'] < 1 else f"about {r['expected']:g}"
                add('BALANCE_GOAL', f"{policy['name']}: {names(r['person_id'])} has {r['actual']:g} {unit_for_amount(report['unit'], r['actual'])}, aim {aim}",
                    person=r['person_id'], severity='warning', rule=None if policy.get('builtin') else policy['id'],
                    extra={'goal_id': report['goal_id'],
                           **{k: r[k] for k in ('prior', 'planned', 'actual', 'expected', 'low', 'high', 'status')},
                           **({'count_bucket': report['count_bucket']} if 'count_bucket' in report else {})})
        short = defaultdict(list)
        for r in report['pairs']:
            if r['status'] != 'within':
                short[r['person_id']].append(r)
        for pid, rows in short.items():  # one finding per person, not one per pair
            add('BALANCE_PAIR', f"{policy['name']}: {names(pid)} is below {rows[0]['expected']:g} with {len(rows)} partner"
                f"{'s' if len(rows) > 1 else ''} ({', '.join(names(r['partner_id']) for r in rows[:3])}{'…' if len(rows) > 3 else ''})",
                person=pid, severity='warning', rule=policy['id'],
                extra={'goal_id': report['goal_id'],
                       'partners': [{k: r[k] for k in ('partner_id', 'actual', 'expected')} for r in rows],
                       **({'count_bucket': report['count_bucket']} if 'count_bucket' in report else {})})
    return reports

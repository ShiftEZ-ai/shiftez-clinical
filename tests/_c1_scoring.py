"""Frozen formulas from C1 ; no git checkout is needed at test runtime.

Shared collaborators are used within fixtures with no historical publications and valid C1
positive-strength names at arithmetic lookups (lead-off removes the built-in goal first).
goal_period has equivalent cached values; report_value is reached only for its identical
pair branch. Unknown/off lookups in report_weights/score.strength are not compared here.
Keep the captured formulas frozen when changing C2; this is not a historical runtime.
"""

BASELINE_C1_SHA = 'ddb7b28f99afeb4008e1b59c656f368743ec9ccc'

FORMULAS = {
    'score_normalization': r'''
from .recurrence import inactive

from math import ceil, floor, gcd

from fractions import Fraction

from datetime import timedelta

from operator import floordiv, mul, truediv

def workload_normalization(data, measure):
    """Eligible focused audience and fixed cover budget; None means legacy snapshots lack those facts."""
    from .candidate_evidence import candidate_evidence
    from .time import aware
    from .validation import index_person_rows, qualified
    from .recurrence import series_definition

    focus = frozenset(data['focus_ids'])
    shifts = [s for s in data['shifts'] if s['id'] in focus and not inactive(s)]
    # Only trials with unchanged dated facts opt in; assignments never choose this audience.
    cache = data.get('_workload_normalization_cache')
    key = measure, frozenset(s['id'] for s in shifts)
    if cache is not None and key in cache:
        return cache[key]
    if (any('requirements' not in s['type_snapshot'].get('definition', {}) for s in shifts)
            or any('active' not in person for person in data['people'].values())):
        return (), None  # Historical snapshots cannot establish a decision-independent audience and budget.
    data = {**data, '_person_rows': data.get('_person_rows') or index_person_rows(data)}
    eligible_shifts = [{**s, 'participants': []} if series_definition(s).get('usual_assignees_fixed', False) else s
                       for s in shifts]
    audience = tuple(sorted(pid for pid in data['people']
                            if any(qualified(data, pid, s, s['type_snapshot']['definition'],
                                             aware(s['start']), aware(s['end']))
                                   and candidate_evidence(data, pid, s)['eligible_and_available'] for s in eligible_shifts)))
    total = Fraction(0)
    for s in shifts:
        duration = (aware(s['end']) - aware(s['start'])) // timedelta(microseconds=1)
        requirements = s['type_snapshot']['definition']['requirements']
        rows = [(r.get('from_minute', 0) * 60_000_000,
                 min(duration, r['to_minute'] * 60_000_000) if r.get('to_minute') is not None else duration,
                 max(r['minimum'], r.get('target') or 0)) for r in requirements]
        bounds = sorted({0, duration} | {max(0, min(duration, t)) for a, b, _ in rows for t in (a, b)})
        demand = sum((b - a) * max([0] + [need for lo, hi, need in rows if lo <= a and b <= hi])
                     for a, b in zip(bounds, bounds[1:]))
        total += Fraction(demand, max(1, duration)) if measure == 'duties' else demand
    result = audience, total
    if cache is not None:
        cache[key] = result
    return result

def workload_limit(data, measure, audience, demand):
    """Round equal shares to the session grid, allowing any one indivisible session."""
    from functools import reduce
    from .time import aware

    focus = set(data['focus_ids'])
    quantities = [1] if measure == 'duties' else [
        (aware(s['end']) - aware(s['start'])) // timedelta(microseconds=1) for s in data['shifts']
        if s['id'] in focus and not inactive(s)]
    quantum = reduce(gcd, quantities, 0) or 1
    return max(max(quantities, default=1), quantum * ceil(Fraction(demand) / (len(audience) * quantum)))

def workload_costs(loads, demand, limit, *, positive=lambda x: max(0, x), ratio=lambda n, d: min(1.0, float(n / (d or 1)))):
    """Zero demand stays neutral; with demand, extra assignments cannot erase an existing overload loss."""
    if demand == 0:
        return 0, dict.fromkeys(loads, 0)
    n = len(loads)
    excess = {pid: positive(n * (load - limit)) for pid, load in loads.items()}
    variation = ratio(sum(excess.values()), n * demand)
    costs = {pid: ratio(value, demand) for pid, value in excess.items()}
    return variation, costs

def share_satisfaction(mean, worst, *, unit=1, multiply=mul, divide=truediv):
    return multiply(unit - mean, unit - divide(worst, 2))
''',
    'fairness': r'''
from collections import defaultdict

from datetime import date, timedelta

import hashlib

import json

from zoneinfo import ZoneInfo

from .conditions import _source_points, change_points, conditions_match, counting_key, counting_views, scope_tree, selector_sources, scope_matches_throughout

from .time import aware, duration_hours, local_midnight

from .score_normalization import goal_cost, share_satisfaction

from .recurrence import inactive

LEAD_HISTORY_DAYS = 182  # the built-in lead balance counts about six months of published rotas

def lead_eligible(data, type_ids, people):
    """Of `people`, those who may lead at least one of this rota's sessions of these duty types (empty: any duty
    type with a team lead), checked at the session's time by the one lead evaluator."""
    from .validation import lead_qualified
    focus, left, found = set(data.get('focus_ids', ())), set(people), set()
    for shift in data['shifts']:
        lead = shift['type_snapshot']['definition'].get('lead')
        if not left or not lead or shift['id'] not in focus or inactive(shift) or type_ids and shift['assignment_type_id'] not in type_ids:
            continue
        a, b = aware(shift['start']), aware(shift['end'])
        hits = {pid for pid in left if lead_qualified(data, pid, shift, lead, a, b)}
        found |= hits
        left -= hits
    return found

def _report_context(data, policy):
    """Run-local immutable goal inputs; assignments and their counts never enter this cache."""
    cache = data.get('_distribution_cache')
    key = ('report_context', goal_identity(policy))
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
    base = {'policy_id': policy['id'], 'goal_id': goal_identity(policy), 'name': policy['name'], 'metric': rule['metric'], 'aim': rule['aim'], 'unit': unit,
            'period': rule.get('period', 'rota'), 'strength': policy['definition'].get('strength', 'medium'),
            'weight': rule.get('weight', 1),
            'share_by': rule.get('share_by', 'available_days'),
            'history': bool(history), 'line_id': line.get('id'), 'line': line.get('line'), 'slot': line.get('slot'),
            'balance_between_people': policy['definition'].get('balance_between_people', True),
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
    context = people, history, prior, source_rows, base, windows, avail, offered
    if cache is not None:
        cache[key] = context
    return context

def goal_report(data, policy, planned_values=None, *, _cost_only=False):
    """Per person (or pair): history, this rota, aim and band, normalized cost 0-1. `planned_values` lets the solver
    and a preview pass their own counts; by default they are read from the rota."""
    rule, zone = policy['definition']['rule'], data['organisation']['timezone']
    if rule.get('distribution', 'people') != 'people':
        from .fairness_distribution import distribution_report
        return distribution_report(data, policy)
    first, last = rota_dates(data)
    people, history, prior, source_rows, base, windows, avail, offered = _report_context(data, policy)
    values, pairs = planned_values if planned_values is not None else planned(data, policy, people['who'], people['partners'])

    def prorated(pid):
        start, end = goal_period(data, policy, pid)
        return max(0.0, (min(last, end) - start).days) / max(1, (end - start).days)

    if rule['metric'] == 'pair_exposure':
        rows = []
        prior_pairs = history.get('pairs', {})
        for pid in people['who']:
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
    offered_total = sum(offered.values())
    actual = {pid: prior[pid] + values.get(pid, 0) for pid in people['who']}
    total, days = sum(actual.values()), sum(avail.values())
    expected = {}
    for pid in people['who']:
        if rule['aim'] == 'target':
            expected[pid] = (sum(row['expected'] for row in source_rows[pid]) if source_rows
                             else rule.get('person_amounts', {}).get(pid, rule.get('amount') or 0) * prorated(pid))
        elif offered_share:
            expected[pid] = total * offered[pid] / offered_total if offered_total else 0.0
        elif rule.get('share_by', 'available_days') == 'available_days' and days:
            expected[pid] = total * avail[pid] / days
        else:
            expected[pid] = total / len(people['who']) if people['who'] else 0
    rows = []
    for pid in people['who']:
        e, x = expected[pid], actual[pid]
        low, high, tolerance = band(rule['metric'], e)
        no_offer = offered_share and not offered[pid]
        cost = goal_cost(0.0 if no_offer else outside(rule['metric'], x, e, rule['aim'], tolerance=tolerance))
        if _cost_only:
            # Greedy trials need canonical costs and source weights, without public display formatting.
            rows.append({'person_id': pid, 'cost': cost,
                         **({'sources': [row['source'] for row in source_rows[pid]]} if source_rows else {})})
            continue
        rows.append({'person_id': pid, 'prior': round(prior[pid], 2), 'planned': round(values.get(pid, 0), 2), 'actual': round(x, 2),
                     'expected': round(e, 2), 'low': round(low, 2), 'high': None if rule['aim'] == 'max_min' else round(high, 2),
                     'available_days': round(avail[pid], 1), 'rota_days': windows[pid], 'cost': cost,
                     'overtime_offered_hours': offered[pid],
                     'status': 'within' if no_offer else status(rule['metric'], x, e, rule['aim'], tolerance=tolerance),
                     **({'strengths': [row['strength'] for row in source_rows[pid]],
                         'sources': [row['source'] for row in source_rows[pid]]} if source_rows else {})})
    report = {**base, 'people': rows, 'pairs': []}
    if _cost_only:
        return report
    return {**report, 'score': round(100 * share_value(rows, report_weights(report, strengths_of(data))), 1)}

def band(metric, expected):
    """(low, high, tolerance) around an expected share: within 10% or one unit (four hours), whichever is wider."""
    tolerance = max(GOAL_BAND_UNITS.get(metric, 1), expected / GOAL_BAND_PARTS)
    return max(0.0, expected - tolerance), expected + tolerance, tolerance

def status(metric, value, expected, aim='equal_share', *, tolerance=None):
    """'within' the band, or 'above' / 'below' it (a max_min aim is never above)."""
    tolerance = band(metric, expected)[2] if tolerance is None else tolerance
    return 'within' if gap_of(value, expected, aim) <= tolerance else 'above' if value > expected else 'below'

def outside(metric, value, expected, aim='equal_share', *, tolerance=None):
    """Normalized cost 0-1: only the distance outside the band counts, over the aim (at least one unit)."""
    tolerance = band(metric, expected)[2] if tolerance is None else tolerance
    return min(1.0, max(0.0, gap_of(value, expected, aim) - tolerance) / max(1.0, expected))

def share_value(rows, weights=None):
    """1 when everyone is inside their band; else (1 - mean cost) times (1 - half the worst person's cost)."""
    if not rows:
        return 1.0
    weights = weights or dict.fromkeys((r['person_id'] for r in rows), 1)
    mean = sum(r['cost'] * weights[r['person_id']] for r in rows) / sum(weights[r['person_id']] for r in rows)
    return max(0.0, share_satisfaction(mean, max(r['cost'] for r in rows)))

def balance_goals(data):
    from .conditions import active_rules
    # As the checks read rules (a catalogue Fair share rule's lines are goals); overrides as before: none.
    goals = [p for p in active_rules(data['policies'], overrides=False, context=data) if p['status'] == 'active' and p.get('active', True)
             and p['definition']['rule']['kind'] == 'balance_goal'
             and p['department_id'] == data['workspace']['department_id']
             and (not p.get('starts_on') or str(p['starts_on']) < str(data['workspace']['ends_on']))
             and (not p.get('ends_on') or str(data['workspace']['starts_on']) < str(p['ends_on']))]
    return counting_views(data, goals) + lead_goals(data, goals)

def lead_goals(data, goals=()):
    """Built in: lead turns of each duty type with a team lead are shared fairly among the people who may lead it,
    pro rata to days available, over the last months (the ledger). A Balance goal on lead turns for that duty type
    replaces it; `data['lead_balance']` (Scheduling & scoring) sets its strength or switches it off."""
    strength = data.get('lead_balance', 'medium')
    if strength == 'off' or not data.get('workspace'):
        return []
    covered = set()
    for p in goals:
        rule = p['definition']['rule']
        if rule['metric'] == 'lead_turns':
            if not rule.get('assignment_type_ids'):
                return []
            covered.update(rule['assignment_type_ids'])
    focus, names = set(data.get('focus_ids', ())), {}
    for s in data['shifts']:
        if s['id'] in focus and not inactive(s) and s['type_snapshot']['definition'].get('lead') and s['assignment_type_id'] not in covered:
            names.setdefault(s['assignment_type_id'], s['type_snapshot'].get('name') or 'Duty')
    return [{'id': 'lead-balance:' + tid, 'name': 'Fair team-lead turns · ' + name, 'builtin': True, 'revision': 1, 'kind': 'balance_goal',
             'department_id': data['workspace']['department_id'], 'status': 'active', 'active': True, 'starts_on': '2000-01-01', 'ends_on': None,
             'definition': {'severity': 'advisory', 'strength': strength, 'scope': {},
                            'rule': {'kind': 'balance_goal', 'metric': 'lead_turns', 'aim': 'equal_share', 'share_by': 'available_days',
                                     'period': 'rolling', 'period_days': LEAD_HISTORY_DAYS, 'assignment_type_ids': [tid]}}}
            for tid, name in sorted(names.items())]
''',
    'quotas': r'''
from collections import defaultdict

from datetime import date, timedelta

from fractions import Fraction

import copy

import csv

import io

import math

from packages.contracts.schemas import TypeDefinition, quota_target_amount

from .recurrence import inactive

from .participation import presence_intervals

from .conditions import (active_rules, bind_level_window, conditions_match, scope_tree, counting_key, counting_views,
                         scope_matches_throughout, scope_has_duty_witness, ScopeContextLimit, SCOPE_CONTEXT_BUDGET)

from .time import aware, local_midnight, local_stamp, overlaps, union_hours, duration_hours

def lead_caps(data):
    """A duty type's "at most N lead turns per person each week / month" (its lead settings, the one source) as a
    quota over the weeks or months around the rota: critical advisory, so a short-staffed week still gets its lead."""
    w, result = data["workspace"], []
    lo, hi = (str(date.fromisoformat(str(w[k])) + timedelta(days=d)) for k, d in (("starts_on", -31), ("ends_on", 31)))
    for tid, typ in sorted((data.get("types") or {}).items()):
        lead = (typ.get("definition") or {}).get("lead") or {}
        if not lead.get("most_turns") or typ.get("department_id") != w["department_id"]:
            continue
        per = lead.get("most_turns_per", "month")
        result.append({"id": "lead-cap:" + tid, "revision": typ.get("revision", 1), "kind": "block_quota", "builtin": True,
                       "name": f"At most {lead['most_turns']} lead turns a {per} · {typ.get('name', 'Duty')}",
                       "department_id": w["department_id"], "status": "active", "active": True, "starts_on": "2000-01-01", "ends_on": None,
                       "definition": {"severity": "advisory", "strength": "critical", "scope": {}, "rule": {
                           "kind": "block_quota", "metric": "lead_turns", "comparison": "maximum", "amount": lead["most_turns"],
                           "maximum_amount": None, "target_amount": None, "assignment_type_ids": [tid], "participation": [],
                           "overtime": "include", "window_days": None, "window_alignment": "calendar_" + per,
                           "prorate_department_days": False, "reference_days": 28, "rounding": "nearest", "starts_on": lo, "ends_on": hi}}})
    return result

def applicable_policies(data):
    w = data["workspace"]
    policies = [p for p in active_rules(data.get("policies", []), context=data) + lead_caps(data)
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
            result.append(dated(policy, starts_on=max(rule['starts_on'], w['starts_on']), ends_on=min(rule['ends_on'], w['ends_on'])))
            continue
        alignment = rule.get('window_alignment', 'rolling')
        if alignment in ('fixed', 'calendar_week', 'calendar_month'):
            # Back-to-back windows (each week from the start date, or each calendar month) that meet this rota.
            lo, hi = date.fromisoformat(w['starts_on']), date.fromisoformat(w['ends_on'])
            anchor = date.fromisoformat(str(rule['window_anchor_on'])) if rule.get('window_anchor_on') else None
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
                    if rule.get('prorate_partial_windows') and (end - start).days < rule['window_days']:
                        window['_quota_window_ratio'] = ((end - start).days, rule['window_days'])
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
            result.append(dated(policy, starts_on=start.isoformat(), ends_on=(start+timedelta(days=days)).isoformat(),
                                **({'repeat': False} if repeat else {})))
            if len(result) > MAX_ROWS:
                return result  # The caller rejects this bound; never certify a prefix.
    return result
''',
    'cp_sat_score': r'''
from .recurrence import inactive

from fractions import Fraction

from math import gcd, lcm

from ortools.sat.python import cp_model

from .score_normalization import PREFERENCE_MEAN, PREFERENCE_WORST, SCORE_SCALE, share_satisfaction, rule_normalization, rule_loss, workload_normalization, workload_limit, workload_costs, coverage_satisfaction

def workload_component(model, data, choices, participants, measure):
    """Fixed cover budget and audience; only overload above the equal share loses points."""
    from datetime import timedelta
    from math import gcd
    from functools import reduce
    from collections import defaultdict
    from .time import aware

    q = ScoreModel(model)
    people, demand = workload_normalization(data, measure)
    if len(people) < 2:
        return 0, 0
    quantities = {key: 1 if measure == 'duties' else
                  (aware(p['end']) - aware(p['start'])) // timedelta(microseconds=1)
                  for key, p in participants.items() if key[0] in data['focus_ids']}
    limit = workload_limit(data, measure, people, demand)
    scale = demand.denominator
    unit = reduce(gcd, [int(demand * scale), limit * scale, *(amount * scale for amount in quantities.values())]) or 1
    demand = int(demand * scale) // unit
    terms = defaultdict(list)
    for key, amount in quantities.items():
        terms[key[1]].append(amount * scale // unit * choices[key])
    loads = {pid: q.var(sum(terms[pid]), name='score_workload') for pid in people}
    variation, costs = workload_costs(
        loads, demand, limit * scale // unit,
        positive=lambda x: q.maximum([0, x], max(0, q.bounds(x)[1])), ratio=q.ratio)
    return q.share_loss(variation, q.maximum(costs.values())), 1
''',
    'cp_sat_goals': r'''
import math

from collections import defaultdict

from fractions import Fraction

from itertools import islice

from .fairness import (SCALE, GOAL_BAND_PARTS, GOAL_BAND_UNITS, comparison_goal, contribution, counts, goal_applies, goal_groups, goal_identity, goal_people,
                       goal_period, goal_reports, goal_sources, local_day, report_weights, rota_dates, weekend_of)

from .recurrence import inactive

from .time import aware

from .conditions import conditions_match

from .cp_sat_score import SCORE_SCALE, ScorePrecisionError

from .score_normalization import round_goal_cost_ticks

MAX_MIN_SHARE = 5

def add_balance_goals(model, data, choices, participants, overtime_choices, pairings, focus, strengths, leads=None,
                      *, score=None, presence=None):
    terms, worst = [], 0
    from packages.contracts.schemas import GenerationOptions
    maximum = GenerationOptions.model_fields['pair_limit_max_opportunities'].default
    maximum = data.get('generation_options', {}).get('pair_limit_max_opportunities', maximum)
    pair_budget = [maximum, maximum]
    zone = data['organisation']['timezone']
    shifts = {s['id']: s for s in sorted(data['shifts'], key=lambda s: s['id']) if s['id'] in focus and not inactive(s)}
    on_shift = defaultdict(list)
    for sid, pid in sorted(choices):
        if sid in shifts:
            on_shift[sid].append(pid)
    groups = sorted(goal_groups(data), key=lambda group: min(goal_identity(policy) for policy in group))
    weight_scale = math.lcm(*(Fraction(str(p['definition']['rule'].get('weight', 1))).denominator
                              for policies in groups for p in policies))
    for policies in groups:
        policy = policies[0]
        rule = policy['definition']['rule']
        weight = strengths[policy['definition'].get('strength', 'medium')] * Fraction(str(rule.get('weight', 1)))
        if score is None:
            weight = round(weight * weight_scale)
        sources = {goal_identity(source): goal_people(data, source) for source in policies}
        people = {'who': sorted({pid for source in sources.values() for pid in source['who']}),
                  'partners': sorted({pid for source in sources.values() for pid in source['partners']})}
        who = people['who']
        eligible = set(who)
        if not who:
            continue
        comparison = comparison_goal(policies)
        if rule.get('distribution', 'people') != 'people':
            weights = dict.fromkeys(who, weight / strengths['medium'] if score is not None else weight)
            added, upper = _distribution_goals(model, data, policy, choices, participants, pairings, focus, weights, pair_budget,
                                              score=score, presence=presence)
            terms.extend(added)
            worst += upper
            continue
        [base] = goal_reports(data, [policies], planned_values=({}, {}))  # history, availability and fixed aims
        weights = {row['person_id']: (sum(Fraction(str(strengths[source['strength']])) * Fraction(str(source.get('weight', 1)))
                       for source in row['sources']) if row.get('sources') else
                       Fraction(str(strengths[base['strength']])) * Fraction(str(base.get('weight', 1))))
                   for row in base['people']} if score is not None else {
                       pid: round(value * weight_scale) for pid, value in report_weights(base, strengths).items()}

        seen = {}

        def applies(sid, pid):
            # Person predicates can depend on the session too: skill grants may name a clinic.
            # Reuse only the same person's complete session check, as the shared goal report does.
            p, shift = participants[sid, pid], shifts[sid]
            if len(policies) == 1 and not counts(rule, shift):
                return False
            key = (pid, sid)
            if key not in seen:
                seen[key] = goal_applies(data, comparison, pid, shift, aware(p['start']), aware(p['end']))
            return seen[key]

        if rule['metric'] == 'pair_exposure':
            partners = set(people['partners'])
            pair_rows = {(r['person_id'], r['partner_id']): r for r in base['pairs']}
            aims = {key: math.ceil((_target(data, comparison, key[0], sources) if score is not None else row['expected'])
                                    - row['prior'] - 1e-9) for key, row in pair_rows.items()}
            together = defaultdict(list)
            for sid, shift in shifts.items():
                here = on_shift[sid]
                a, b = aware(shift['start']), aware(shift['end'])
                mates = [q for q in here if q in partners and conditions_match(data, rule['partners'], q, None, a, b)]
                for pid in here:
                    if pid not in eligible or not applies(sid, pid):
                        continue
                    for q in mates:
                        if q == pid or aims.get((pid, q), 0) <= 0:
                            continue
                        if rule.get('pairing', 'present') == 'present':
                            together[pid, q].append(sid)
                        elif (sid, pid, q) in pairings:
                            together[pid, q].append(pairings[sid, pid, q])
                        elif participants[sid, pid].get('supervisor_person_id') == q:
                            together[pid, q].append(choices[sid, pid])
            costs = []
            for key, need in sorted(aims.items()):
                if score is not None:
                    row = pair_rows[key]
                    if need <= 0:
                        costs.append((1, 0))
                        continue
                    met = together.get(key, [])
                    pid, q = key
                    if rule.get('pairing', 'present') == 'present':
                        meetings = []
                        for sid in met:
                            both = model.new_bool_var('goal_pair')
                            model.add(both <= choices[sid, pid])
                            model.add(both <= choices[sid, q])
                            model.add(both >= choices[sid, pid] + choices[sid, q] - 1)
                            meetings.append(both)
                    else:
                        meetings = met
                    # Keep the fractional, prorated aim: an integer deficit changes the pair score.
                    aim = _target(data, comparison, pid, sources)
                    prior = Fraction(str(row['prior']))
                    common = math.lcm(aim.denominator, prior.denominator)
                    _score_precision(common, int(aim * common), int(prior * common), common * len(meetings))
                    deficit = _positive_part(score, int((aim - prior) * common) - common * sum(meetings))
                    costs.append((1, _report_cost(score, deficit, int(aim * common))))
                    continue
                if need <= 0:
                    continue
                met = together.get(key, [])
                pid, q = key
                if rule.get('pairing', 'present') == 'present':
                    deficit = _pair_deficit(model, [(choices[sid, pid], choices[sid, q]) for sid in met], need)
                else:
                    # Supervised meetings already have variables: count all of them without growing the model.
                    deficit = model.new_int_var(0, need, 'goal_pair_deficit')
                    model.add(deficit + sum(met) >= need)
                scaled = model.new_int_var(0, 1000, 'goal_pair_cost')
                model.add(scaled * need >= 1000 * deficit)
                terms.append(weight * scaled)
                worst += weight * 1000
            if score is not None and costs:
                terms.append((weight / strengths['medium'], score.mean(costs)))
                if presence is not None:
                    presence.append(1)
            continue

        scale = SCALE.get(rule['metric'], 1)
        rows = {r['person_id']: r for r in base['people']}
        history = (data.get('fairness_history') or {}).get(goal_identity(comparison), {}).get('people', {})
        raw_prior = {pid: _quantity(history.get(pid, {}).get('value', 0)) for pid in who}
        planned, bound, contributions = {}, {}, []
        weekends = defaultdict(lambda: defaultdict(list))
        for (sid, pid), x in sorted(choices.items()):
            if sid not in shifts or pid not in eligible or not applies(sid, pid):
                continue
            p = participants[sid, pid]
            if rule['metric'] == 'weekends':
                weekend = weekend_of(local_day(p['start'], zone), rule)
                if weekend:
                    weekends[pid][weekend].append(x)
                continue
            if rule['metric'] in ('overtime_duties', 'overtime_hours'):
                ot = overtime_choices.get((sid, pid))
                coefficient = contribution(rule['metric'].replace('overtime_', '') if ot is not None else rule['metric'], shifts[sid], p)
                var = ot if ot is not None else x
            elif rule['metric'] == 'lead_turns' and leads is not None and (sid, pid) in leads:
                coefficient, var = 1, leads[sid, pid]
            else:
                coefficient, var = contribution(rule['metric'], shifts[sid], p), x
            if coefficient:
                contributions.append((pid, _quantity(coefficient), var))
        if score is not None:
            scale = math.lcm(1, *(value.denominator for _, value, _ in contributions),
                             *(value.denominator for value in raw_prior.values()))
            _score_precision(scale)
        for pid, value, var in contributions:
            coefficient = round(value * scale)
            planned.setdefault(pid, []).append(coefficient * var)
            bound[pid] = bound.get(pid, 0) + coefficient
        for pid, groups in sorted(weekends.items()):
            flags = []
            for _, xs in sorted(groups.items()):
                flag = xs[0] if len(xs) == 1 else model.new_bool_var('goal_weekend')
                if len(xs) > 1:
                    model.add_max_equality(flag, xs)
                flags.append(flag)
            planned[pid], bound[pid] = [scale * flag for flag in flags], scale * len(flags)
        prior = {pid: round((raw_prior[pid] if score is not None else rows[pid]['prior']) * scale) for pid in who}
        if score is not None:
            _score_precision(sum(bound.values()) + sum(prior.values()))
        counted = {}
        for pid in who:
            counted[pid] = model.new_int_var(0, bound.get(pid, 0), 'goal_planned')
            model.add(counted[pid] == sum(planned.get(pid, [])))
        top = sum(bound.values())
        if score is not None:
            actual = {pid: counted[pid] + prior[pid] for pid in who}
            total = score.var(sum(actual.values()), 0, sum(prior.values()) + top, 'goal_actual_total')
            equal = rule.get('share_by', 'available_days') == 'equal'
            offered = rule.get('share_by') == 'offered' and rule['aim'] != 'target'
            avail = ({pid: _quantity(rows[pid]['overtime_offered_hours']) for pid in who} if offered else
                     {pid: Fraction(1) if equal else Fraction(rows[pid]['rota_days']) +
                      Fraction(str(history.get(pid, {}).get('available', 0))) for pid in who})
            if not sum(avail.values()) and not offered:
                avail = dict.fromkeys(who, Fraction(1))
            days = sum(avail.values()) or 1
            costs = []
            for pid in who:
                if offered and not avail[pid]:
                    costs.append((weights[pid], 0))
                    continue
                if rule['aim'] == 'target':
                    # Grouped reports already sum the canonically rounded source targets.
                    expected = (Fraction(str(rows[pid]['expected'])) if comparison.get('_balance_sources')
                                else _target(data, comparison, pid, sources))
                else:
                    expected = avail[pid] / days
                divisor = expected.denominator
                aim = expected.numerator * scale if rule['aim'] == 'target' else expected.numerator * total
                difference = actual[pid] * divisor - aim
                gap = _positive_part(score, -difference) if rule['aim'] == 'max_min' else score.maximum(
                    [difference, -difference], (prior[pid] + bound.get(pid, 0)) * divisor +
                    expected.numerator * (scale if rule['aim'] == 'target' else sum(prior.values()) + top))
                parts = GOAL_BAND_PARTS
                band_unit = GOAL_BAND_UNITS.get(rule['metric'], 1)
                # Integer bands preserve fractional equal shares.
                tolerance = score.maximum([parts * band_unit * scale * divisor,
                                           aim], max(parts * band_unit * scale * divisor,
                                                     expected.numerator * (scale if rule['aim'] == 'target' else sum(prior.values()) + top)))
                outside = _positive_part(score, parts * gap - tolerance)
                denominator = score.maximum([parts * scale * divisor, parts * aim],
                                            max(parts * scale * divisor, parts * expected.numerator *
                                                (scale if rule['aim'] == 'target' else sum(prior.values()) + top)))
                costs.append((weights[pid], _report_cost(score, outside, denominator)))
            mean = score.mean(costs)
            worst_cost = score.maximum([cost for _, cost in costs], 1_000_000)
            terms.append((sum(weights.values()) / (len(who) * strengths['medium']),
                          score.share_loss(mean, worst_cost)))
            if presence is not None:
                presence.append(1)
            continue
        weighted_costs, worst_bound, total_cost_bound = [], 0, 0
        n = len(who)
        if rule['aim'] == 'target':
            for pid in who:
                aim = round(rows[pid]['expected'] * scale)
                deviation = model.new_int_var(0, max(aim, prior[pid] + bound.get(pid, 0)), 'goal_deviation')
                model.add(deviation >= prior[pid] + counted[pid] - aim)
                model.add(deviation >= aim - prior[pid] - counted[pid])
                cap = max(scale, aim)
                cost_bound = -(-1000 * max(aim, prior[pid] + bound.get(pid, 0)) // cap)
                scaled = model.new_int_var(0, cost_bound, 'goal_cost')
                model.add(scaled * cap >= 1000 * deviation)
                terms.append(MAX_MIN_SHARE * weights[pid] * scaled)
                weighted_costs.append(weights[pid] * scaled)
                worst_bound = max(worst_bound, weights[pid] * cost_bound)
                total_cost_bound += weights[pid] * cost_bound
        else:
            total = model.new_int_var(0, top, 'goal_total')
            model.add(total == sum(counted.values()))
            equal = rule.get('share_by', 'available_days') == 'equal'
            offered = rule.get('share_by') == 'offered'
            if offered:
                # Minute precision keeps partial-hour shares without large second-based coefficients.
                avail = {pid: max(1, round(rows[pid]['overtime_offered_hours'] * 60))
                         if rows[pid]['overtime_offered_hours'] > 0 else 0 for pid in who}
                divisor = math.gcd(*avail.values()) or 1
                avail = {pid: value // divisor for pid, value in avail.items()}
            else:
                avail = {pid: 1 if equal else max(0, round(rows[pid]['available_days'])) for pid in who}
            days = sum(avail.values())
            if not days:
                if offered:
                    continue
                avail, days = {pid: 1 for pid in who}, n
            everyone = sum(prior.values())
            per_seat = {'overtime_duties': 'duties', 'overtime_hours': 'hours', 'lead_turns': 'duties'}.get(rule['metric'], rule['metric'])
            demand = sum(round(contribution(per_seat, s, s) * scale)
                         * max([1] + [r['minimum'] for r in s['type_snapshot']['definition'].get('requirements', [])])
                         for sid, s in shifts.items() if any(pid in eligible and applies(sid, pid)
                                                             for pid in on_shift[sid])) if rule['metric'] != 'weekends' else n * 2
            for pid in who:
                if offered and not avail[pid]:
                    continue  # No offer means no share and no fairness penalty, including historical work.
                gap = (prior[pid] + counted[pid]) * days - avail[pid] * (everyone + total)
                high = (prior[pid] + bound.get(pid, 0)) * days + avail[pid] * (everyone + top)
                deviation = model.new_int_var(0, high, 'goal_deviation')
                model.add(deviation >= -gap)
                if rule['aim'] != 'max_min':
                    model.add(deviation >= gap)
                cap = max(scale * days, avail[pid] * (everyone + demand))
                cost_bound = -(-1000 * high // cap)
                scaled = model.new_int_var(0, cost_bound, 'goal_cost')
                model.add(scaled * cap >= 1000 * deviation)
                if rule['aim'] != 'max_min':
                    terms.append(MAX_MIN_SHARE * weights[pid] * scaled)
                weighted_costs.append(weights[pid] * scaled)
                worst_bound = max(worst_bound, weights[pid] * cost_bound)
                total_cost_bound += weights[pid] * cost_bound
        # The worst-off person (normalized as each person is): a small extra term for shares, the whole term for max-min.
        worst_off = model.new_int_var(0, worst_bound, 'goal_worst_off')
        for cost in weighted_costs:
            model.add(worst_off >= cost)
        share = n * MAX_MIN_SHARE if rule['aim'] == 'max_min' else 1
        terms.append(share * worst_off)
        worst += share * worst_bound + (0 if rule['aim'] == 'max_min' else MAX_MIN_SHARE * total_cost_bound)
    return terms, worst
''',
    'score': r'''
from collections import defaultdict

from math import fsum

from datetime import timedelta

from zoneinfo import ZoneInfo

from .recurrence import inactive

from .conditions import active_rules, counting_key, counting_views

from .fairness import quota_goal_reports, report_weights, share_value, target_share

from .patterns import SHAPE_KINDS, wish_outcomes

from .quotas import quota_identity

from .score_normalization import PREFERENCE_MEAN, PREFERENCE_WORST, rule_normalization, rule_loss, workload_normalization, workload_limit, workload_costs, share_satisfaction, coverage_satisfaction

from .time import aware, duration_hours

from .validation import finding_observations, index_person_rows, named_people, policy_applies, session_scope

def schedule_score(data, validation, options=None, baseline=None, limit=12):
    """`options`: generation options (scoring settings, balance measure). `baseline`: the assignments before a
    change, {(shift, person): flags(participant)} (or bare pairs); stability is scored only when there is one."""
    settings = settings_of(options)
    measure = (options or {}).get('balance_measure', 'reserved_hours')
    zone = ZoneInfo(data['organisation']['timezone'])
    focus = set(data['focus_ids'])
    shifts = {s['id']: s for s in data['shifts'] if not inactive(s) and s['id'] in focus}
    if not shifts:
        return {'version': VERSION, 'score': None, 'raw': None, 'uncapped_score': None,
                'blocking_count': sum(f['severity'] == 'error' for f in validation.get('findings', [])),
                'capped': False, 'capped_points': 0, 'publishable': False,
                'gate': {'code': 'EMPTY_ROTA', 'message': 'Add a session before submitting'},
                'settings': settings, 'components': {c: {'value': None, 'weight': settings['weights'][c], 'lost': 0}
                                                    for c in COMPONENTS},
                'why': [], 'people': {}, 'contributions': [], 'coverage_fraction': None, 'rule_weights': []}
    people = data['people']
    # Templates and overrides read as the checks read them: one copy per per-person line, overridden people left out.
    rules = active_rules(data['policies'], context=data)
    data = {**data, 'policies': rules, '_person_rows': data.get('_person_rows') or index_person_rows(data),
            '_score_options': options or {},
            '_scope_interval_cache': data.get('_scope_interval_cache', {}),
            '_line_index_cache': data.get('_line_index_cache', {}),
            '_person_dates_cache': data.get('_person_dates_cache', {})}
    from .date_sets import prepare_date_sets
    data['_date_set_cache'] = prepare_date_sets(data)
    policies = {p['id']: p for p in reversed(rules)}  # by id: a rule's first copy (its name and default)
    counted = counting_views(data, rules)
    copies = {rule_copy_key(p): p for p in counted}

    def finding_policy(finding):
        detail = finding.get('detail') or {}
        if 'rule_severity' in detail:
            return copies.get((finding.get('rule_id'), detail.get('line'), detail.get('slot'),
                               detail['rule_severity'], detail.get('rule_strength', 'medium'), counting_key(detail)))
        return policies.get(finding.get('rule_id'))  # older stored findings lack copy provenance

    def provenance(source=None):
        source = source or {}
        level = source.get('strength')
        weight = source.get('weight', source.get('rule', {}).get('weight', 1))
        return {'line': source.get('line'), 'slot': source.get('slot'), 'rule_severity': source.get('severity'),
                'rule_strength': level, 'rule_weight': settings['strengths'][level] * weight if level in settings['strengths'] else None}

    def policy_provenance(policy):
        return provenance({**policy['definition'], **(policy.get('template_line') or {}),
                           'strength': policy['definition'].get('strength', 'medium')}) if policy else provenance()
    assigned = defaultdict(list)  # person -> [(shift, participant)]
    for shift in shifts.values():
        for p in shift['participants']:
            assigned[p['person_id']].append((shift, p))

    def who(pid):
        return people.get(pid, {}).get('display_name', 'Someone')

    def when(shift):
        return f"{shift['type_snapshot']['name']} {aware(shift['start']).astimezone(zone):%a %d %b %H:%M}"

    # instance: (component, loss on that component's 0-1 scale, text, person, shift, rule, provenance). A component's
    # instances add up exactly to 1 - its value.
    components, instances = {}, []

    # Coverage: unfilled required seat-hours, then desired cover at a quarter of the weight.
    required, wanted, gaps, desired = 0.0, 0.0, defaultdict(float), defaultdict(float)
    for row in validation.get('coverage', []):
        if row['shift_id'] in shifts and row['required'] and row['status'] in {'covered', 'gap'}:
            hours = duration_hours(aware(row['start']), aware(row['end']))
            required += row['required'] * hours
            gaps[row['shift_id']] += max(0, row['required'] - row['available_eligible']) * hours
    for f in validation.get('findings', []):
        if f['code'] == 'COVERAGE_TARGET' and f['shift_id'] in shifts:
            hours = duration_hours(aware(f['start']), aware(f['end']))
            desired[f['shift_id']] += (f['detail']['target'] - f['detail']['available']) * hours
            wanted += f['detail']['target'] * hours
    if required or desired:
        open_hours, below = sum(gaps.values()), sum(desired.values())
        unfilled = open_hours / required if required else 0.0
        short = min(1.0, below / (required or wanted)) if desired else 0.0
        components['coverage'] = coverage_satisfaction(unfilled, short)
        instances += [('coverage', unfilled * hours / open_hours, f'{hours:g} unfilled seat-hours · {when(shifts[sid])}', None, sid, None, provenance())
                      for sid, hours in gaps.items() if hours]
        instances += [('coverage', 0.25 * short * (1 - unfilled) * hours / below,
                       f'{hours:g} seat-hours below desired cover · {when(shifts[sid])}', None, sid, None, provenance())
                      for sid, hours in desired.items()]

    # Rules: each advisory rule per person (per session for cover rules) and each advisory quota row.
    items = {}  # key -> [strength, cost 0-1, text, person, shift, rule, provenance]
    for policy in counted:
        rule = policy['definition']['rule']
        if policy['definition']['severity'] != 'advisory' or rule['kind'] in {'allocation_preference', 'block_quota', 'balance_goal'} | SHAPE_KINDS:
            continue
        weight = strength(policy, settings)
        if rule['kind'] in SESSION_KINDS:
            session_policy = {**policy, 'definition': {**policy['definition'], 'scope': session_scope(policy['definition']['scope'])}}
            for sid, shift in shifts.items():
                if policy_applies(data, session_policy, None, shift, aware(shift['start']), aware(shift['end'])):
                    items[rule_copy_key(policy), sid] = [weight, 0.0, f"{policy['name']} · {when(shift)}", None, sid, policy['id'], policy_provenance(policy)]
            continue
        named = named_people(policy['definition']['scope'])  # a rule naming people never applies to anyone else
        for pid, rows in assigned.items():
            if (named is None or pid in named) and any(policy_applies(data, policy, pid, s, aware(p['start']), aware(p['end'])) for s, p in rows):
                items[rule_copy_key(policy), pid] = [weight, 0.0, f"{who(pid)} · {policy['name']}", pid, None, policy['id'], policy_provenance(policy)]
    breaches = defaultdict(int)
    for f in finding_observations(validation):
        policy = finding_policy(f)
        if (f['severity'] != 'warning' or f['code'] in NOT_BREACHES or f['code'].startswith('QUOTA_') or not policy
                or policy['definition']['severity'] != 'advisory' or policy['definition']['rule']['kind'] in SHAPE_KINDS):
            continue
        key = (rule_copy_key(policy), f['person_id'] or f['shift_id'])
        if key not in items:  # the validator saw a subject the scan above did not: still counted
            label = who(f['person_id']) if f['person_id'] else when(shifts[f['shift_id']]) if f['shift_id'] in shifts else ''
            items[key] = [strength(policy, settings), 0.0, f"{label} · {policy['name']}", f['person_id'], f['shift_id'], policy['id'], policy_provenance(policy)]
        breaches[key] += 1
        items[key][1] = 1.0  # a broken rule instance is fully violated (worst plausible value 1)
    for key, n in breaches.items():
        if n > 1:
            items[key][2] += f' ({n}×)'
    quota_weight = 0
    quota_goals = any(report.get('quota_target') for report in validation.get('goals', []))
    for row in (validation.get('quota_report') or {}).get('rows', []):
        policy = policies.get(row['policy_id'])
        if not policy or row['status'] == 'unresolved':
            continue
        if row['comparison'] == 'target' or row['severity'] == 'advisory':
            cost = 0.0 if row['status'] in {'met', 'excepted'} else min(1.0, abs(row.get('difference', row['actual'] - row['amount'])) / max(abs(row['amount']), 1))
            quota_weight += settings['strengths'][row.get('strength', policy['definition'].get('strength', 'medium'))]
            amount = format(row['amount'], '.2f' if row['comparison'] == 'target' else 'g')
            items[quota_identity(row), row['person_id']] = [
                settings['strengths'][row.get('strength', policy['definition'].get('strength', 'medium'))], cost,
                f"{who(row['person_id'])} · {row['policy_name']}: {row['actual']:g} vs {row['comparison']} {amount} {row['metric'].replace('_', ' ')}",
                row['person_id'], None, row['policy_id'],
                provenance({**row, 'strength': row.get('strength', policy['definition'].get('strength', 'medium'))})]
    total = rule_normalization(data, settings['strengths'], [quota_weight])
    if total:
        violated = sum(item[0] * item[1] for item in items.values())
        loss = rule_loss(violated, total)
        components['rules'] = 1 - loss
        instances += [('rules', loss * w * c / violated, text, pid, sid, rid, source)
                      for w, c, text, pid, sid, rid, source in items.values() if c]

    # Workload fairness uses a fixed audience and cover budget; only overload loses points.
    measures = []  # (value, loss by person, costs, texts, weight, rule, provenance)
    audience, demand = workload_normalization(data, measure)
    legacy_workload = demand is None
    if legacy_workload:
        audience = tuple(assigned)
    loads = {pid: len(assigned.get(pid, [])) if measure == 'duties' else
             sum((aware(p['end']) - aware(p['start'])) // timedelta(microseconds=1) for _, p in assigned.get(pid, []))
             for pid in audience}
    if len(audience) > 1:
        unit = 1 if measure == 'duties' else 3_600_000_000
        if legacy_workload:
            # Replay the pre-budget formula when historical snapshots lack cover or eligibility facts.
            demand = sum(loads.values())
            mean = demand / len(audience) / unit
            value, loss, costs, _ = target_share({pid: (load / unit, mean) for pid, load in loads.items()},
                                                dict.fromkeys(audience, 1))
        else:
            variation, costs = workload_costs(loads, demand, workload_limit(data, measure, audience, demand))
            value = float(share_satisfaction(variation, max(costs.values())))
            total_cost = sum(costs.values()) or 1
            loss = {pid: (1 - value) * c / total_cost for pid, c in costs.items()}
        texts = {pid: f'{who(pid)} \u00b7 {load / unit:g} {"duties" if measure == "duties" else "h"} vs even share {float(demand) / len(audience) / unit:.1f}'
                 for pid, load in loads.items()}
        measures.append((value, loss, costs, texts, 1, None, {pid: [provenance()] for pid in audience}))
    reports = validation.get('goals', [])
    if not quota_goals:
        reports = reports + quota_goal_reports(data, validation.get('quota_report') or {})
    for report in reports:
        source = {row['person_id']: [provenance(item) for item in row.get('sources') or [{**report, 'severity': 'advisory'}]]
                  for row in report['people'] + report['pairs']}
        # A goal counts by its strength against the other goals; the built-in workload spread counts as medium.
        weight = settings['strengths'][report['strength']] * report.get('weight', 1) / settings['strengths']['medium']
        if report['pairs']:
            rows = report['pairs']
            costs, loss = defaultdict(float), defaultdict(float)
            for r in rows:
                costs[r['person_id']] = max(costs[r['person_id']], r['cost'])
                loss[r['person_id']] += r['cost'] / len(rows)
            texts = {pid: f"{who(pid)} · {report['name']}: {sum(1 for r in rows if r['person_id'] == pid and r['cost'])} partner(s) below {rows[0]['expected']:g}"
                     for pid in costs}
            measures.append((1 - sum(r['cost'] for r in rows) / len(rows), loss, costs, texts, weight, report['policy_id'],
                             source))
            continue
        rows = report['people']
        if not rows or report.get('quota_target'):
            continue  # quota targets belong to the rules component
        row_weights = report_weights(report, settings['strengths'])
        weight = sum(row_weights.values()) / len(row_weights) / settings['strengths']['medium']
        value = share_value(rows, row_weights)
        costs = {r['person_id']: r['cost'] for r in rows}
        # Split the goal's loss over the people outside their band, by how far outside (inside the band loses nothing).
        total = sum(c * row_weights[pid] for pid, c in costs.items()) or 1
        loss = {pid: (1 - value) * c * row_weights[pid] / total for pid, c in costs.items()}
        texts = {r['person_id']: f"{who(r['person_id'])} · {r['actual']:g} {report['unit']} vs aim {r['expected']:.1f} ({report['name']})" for r in rows}
        measures.append((value, loss, costs, texts, weight, report['policy_id'], source))
    fairness_cost = defaultdict(float)  # person -> worst normalized deviation over the goals
    if measures and assigned:
        weights_total = fsum(m[4] for m in measures)
        components['fairness'] = fsum(m[0] * m[4] for m in measures) / weights_total
        for value, loss, costs, texts, weight, rid, source in measures:
            for pid, c in costs.items():
                fairness_cost[pid] = max(fairness_cost[pid], c)
            for pid, part in loss.items():
                if part:
                    total = sum(item['rule_weight'] or 1 for item in source[pid])
                    instances += [('fairness', part * weight / weights_total * (item['rule_weight'] or 1) / total,
                                   texts[pid], pid, None, rid, item) for item in source[pid]]

    # Preferences, per person: allocation preferences (each rule's strength), staff wishes (medium strength x the wish's weight,
    # cost = share of its days not honoured) and shape rules (strength, fully broken when any breach).
    own = defaultdict(list)  # person -> [weight, cost, text, rule, provenance]
    medium = settings['strengths']['medium']
    from .allocation_score import allocation_score_items, SCALE as ALLOCATION_SCALE
    for item in allocation_score_items(data, strengths=settings['strengths']):
        if not item['active']:
            continue
        policy, pid = item['policy'], item['person_id']
        label = (policy['name'] + ' (' + item['label'] + ')') if policy else item['label']
        own[pid].append([float(item['weight']), item['loss'] / ALLOCATION_SCALE,
                         f"{who(pid)}: {label}", policy['id'] if policy else None,
                         policy_provenance(policy)])
    outcomes = wish_outcomes(data)
    for wish in data.get('wishes') or []:
        if wish['id'] in outcomes and wish.get('weight'):
            honoured, n = outcomes[wish['id']]
            own[wish['person_id']].append([medium * wish['weight'], 1 - honoured, f"{who(wish['person_id'])} · preference '{wish_words(wish, data)}' "
                                           + ('not granted' if honoured == 0 else f'granted on {round(honoured * n)} of {n} days'), None, provenance()])
    shapes = {}
    for policy in counted:
        if policy['definition']['rule']['kind'] in SHAPE_KINDS and policy['definition']['severity'] == 'advisory':
            for pid, rows in assigned.items():
                if any(policy_applies(data, policy, pid, s, aware(p['start']), aware(p['end'])) for s, p in rows):
                    shapes[rule_copy_key(policy), pid] = [strength(policy, settings), 0.0, f"{who(pid)} · {policy['name']}", policy['id'], policy_provenance(policy)]
    counts = defaultdict(int)
    for f in validation.get('findings', []):
        policy = finding_policy(f)
        if policy and policy['definition']['rule']['kind'] in SHAPE_KINDS and f['person_id']:
            key = (rule_copy_key(policy), f['person_id'])
            shapes.setdefault(key, [strength(policy, settings), 0.0, f"{who(f['person_id'])} · {policy['name']}", policy['id'], policy_provenance(policy)])[1] = 1.0
            counts[key] += 1
    for (rid, pid), item in shapes.items():
        if counts[rid, pid] > 1:
            item[2] += f' ({counts[rid, pid]}×)'
        own[pid].append(item)
    sat = {pid: 1 - sum(w * c for w, c, _, _, _ in items) / sum(w for w, _, _, _, _ in items) for pid, items in own.items() if sum(w for w, _, _, _, _ in items)}
    if sat and assigned:
        low = min(sat, key=lambda pid: (sat[pid], pid))
        components['preferences'] = (PREFERENCE_MEAN * sum(sat.values()) / len(sat) + PREFERENCE_WORST * sat[low]) / (PREFERENCE_MEAN + PREFERENCE_WORST)
        for pid in sat:
            loss = (PREFERENCE_MEAN * (1 - sat[pid]) / len(sat)
                    + (PREFERENCE_WORST * (1 - sat[low]) if pid == low else 0)) / (PREFERENCE_MEAN + PREFERENCE_WORST)
            spent = sum(w * c for w, c, _, _, _ in own[pid])
            instances += [('preferences', loss * w * c / spent, text, pid, None, rid, source) for w, c, text, rid, source in own[pid] if loss and w * c]

    # Stability: existing assignments removed, moved or swapped away, or with a changed lead/overtime flag, against
    # the baseline (only when there is one). Filling an open place is not a change.
    # ponytail: changes are not yet weighted by how close to today they are.
    changes = defaultdict(int)
    if baseline is not None:
        baseline = baseline if isinstance(baseline, dict) else dict.fromkeys(baseline)  # pairs alone: flags not compared
        now = {(s['id'], p['person_id']): flags(p) for s in shifts.values() for p in s['participants']}
        before = {key: value for key, value in baseline.items() if key[0] in shifts}
        moved = [key for key, value in before.items() if key not in now or value is not None and now[key] != value]
        for _, pid in moved:
            changes[pid] += 1
        components['stability'] = 1 - min(1.0, len(moved) / max(1, len(before)))
        instances += [('stability', (1 - components['stability']) * n / len(moved),
                       f'{who(pid)} · {n} assignment change{"s" if n > 1 else ""}', pid, None, None, provenance()) for pid, n in changes.items()]

    weights = settings['weights']
    present = {c: v for c, v in components.items() if weights[c]}
    total_weight = sum(weights[c] for c in present)
    raw = (100 - sum(100 * weights[c] * (1 - v) / total_weight for c, v in present.items())
           if total_weight else 100)
    empty_loss = raw if not assigned else 0
    raw -= empty_loss
    errors = [f for f in validation.get('findings', []) if f['severity'] == 'error']
    # The displayed gate diagnosis must not depend on the database's rule order.
    first = min(errors, key=lambda f: (
        f['code'], f.get('rule_id') or '', f.get('person_id') or '', f.get('shift_id') or '',
        f.get('start') or '', f.get('end') or '', str(f.get('detail')), f['message'])) if errors else None
    score = min(raw, CAP) if errors else raw
    lines = [(100 * weights[c] * loss / total_weight, c, text, pid, sid, rid, source)
             for c, loss, text, pid, sid, rid, source in instances if c in present]
    if empty_loss:
        lines.append((empty_loss, 'other', 'No assignments in this rota', None, None, None, provenance()))
    if errors and raw > CAP:
        lines.append((raw - CAP, 'gate', f"Capped at {CAP}: not publishable until the rota check errors are fixed ({first['message']})",
                      first['person_id'], first['shift_id'], first['rule_id'], policy_provenance(finding_policy(first))))
    # Equal remainders belong to the same source regardless of rule/report traversal order.
    lines.sort(key=lambda line: (line[1], line[5] or '', line[3] or '', line[4] or '',
                                 str(line[6].get('line')), str(line[6].get('slot')), line[2]))
    shown = round(score, 1)
    tenths = apportion([line[0] for line in lines], round((100 - shown) * 10))
    contributions = sorted(({'component': c, 'points': t / 10, 'text': text, 'person_id': pid, 'shift_id': sid, 'rule_id': rid, **source}
                            for t, (_, c, text, pid, sid, rid, source) in zip(tenths, lines) if t), key=lambda w: (-w['points'], w['text']))
    why = [{k: row[k] for k in ('component', 'points', 'text', 'person_id', 'shift_id', 'rule_id')} for row in contributions]
    lost = defaultdict(int)
    for t, line in zip(tenths, lines):
        lost[line[1]] += t
    top = why[:limit]
    if why[limit:]:
        top.append({'component': 'other', 'points': sum(round(w['points'] * 10) for w in why[limit:]) / 10,
                    'text': f'{len(why) - limit} smaller items', 'person_id': None, 'shift_id': None, 'rule_id': None})

    own_rules = defaultdict(lambda: [0.0, 0.0])
    for w, c, _, pid, _, _, _ in items.values():
        if pid:
            own_rules[pid][0] += w
            own_rules[pid][1] += w * c
    per_person = {}
    for pid in sorted(set(assigned) | (set(own_rules) | set(sat) if assigned else set()) | set(changes)):
        parts = {}
        if 'rules' in present and pid in own_rules:
            parts['rules'] = 1 - own_rules[pid][1] / own_rules[pid][0]
        if 'fairness' in present and pid in fairness_cost:
            parts['fairness'] = 1 - fairness_cost[pid]
        if 'preferences' in present and pid in sat:
            parts['preferences'] = sat[pid]
        if 'stability' in present:
            own = sum(1 for key in baseline if key[1] == pid and key[0] in shifts)
            parts['stability'] = 1 - min(1.0, changes[pid] / max(1, own))
        weight = sum(weights[c] for c in parts)
        per_person[pid] = {'score': round(100 * sum(weights[c] * v for c, v in parts.items()) / weight, 1) if weight else 100.0,
                           **{c: round(v, 3) for c, v in parts.items()},
                           'issues': [{'points': w['points'], 'text': w['text']} for w in why if w['person_id'] == pid][:3]}
    return {'version': VERSION, 'score': shown, 'raw': round(raw, 1), 'uncapped_score': round(raw, 1),
            'blocking_count': len(errors), 'capped': bool(errors and raw > CAP), 'publishable': not errors,
            'gate': {'code': first['code'], 'message': first['message']} if first else None,
            'settings': settings, 'capped_points': lost['gate'] / 10,
            'components': {c: {'value': round(components[c], 4) if c in components else None, 'weight': weights[c],
                               'lost': lost[c] / 10} for c in COMPONENTS},
            'why': top, 'people': per_person, 'contributions': contributions,
            'coverage_fraction': components.get('coverage'),
            'rule_weights': [{'rule_id': p['id'], 'severity': p['definition']['severity'],
                              'strength': p['definition'].get('strength', 'medium'),
                              'weight': strength(p, settings),
                              'line': (p.get('template_line') or {}).get('line'),
                              'slot': (p.get('template_line') or {}).get('slot')}
                             # Metadata order must not depend on the database's source-row order.
                             for p in sorted(rules, key=lambda p: tuple(str(value) for value in rule_copy_key(p)))]}
''',
}

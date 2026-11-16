"""Shared dimensionless score formulas; the solver supplies integer arithmetic."""
from .recurrence import inactive
from math import ceil, floor, gcd
from fractions import Fraction
from datetime import timedelta
from operator import floordiv, mul, truediv

PREFERENCE_MEAN, PREFERENCE_WORST = 7, 3
SCORE_SCALE = 1_000_000


def reduced_component_weights(weights):
    """Cancel a common factor without changing any active-subset weighted mean."""
    divisor = gcd(*weights.values()) or 1
    return {name: weight // divisor for name, weight in weights.items()}


def coverage_satisfaction(unfilled, short, *, factor=0.25, unit=1, multiply=mul, divide=truediv):
    factor = Fraction(str(factor))
    return multiply(unit - unfilled, unit - divide(short * factor.numerator, factor.denominator))


def rule_opportunities(data):
    """Fixed rule subjects, independent of assigned people and solver shortlists."""
    from .conditions import counting_views, change_points, scope_tree, selector_sources, _source_points
    from .patterns import SHAPE_KINDS
    from .score import SESSION_KINDS, rule_copy_key
    from .time import aware
    from .validation import department_memberships, index_person_rows, named_people, policy_applies, session_scope, within_dates

    check = data.get('_check_budget', lambda: None)
    data = {**data, '_person_rows': data.get('_person_rows') or index_person_rows(data),
            '_line_index_cache': data.get('_line_index_cache', {}),
            '_person_dates_cache': data.get('_person_dates_cache', {})}
    focus = set(data['focus_ids'])
    shifts = [s for s in data['shifts'] if s['id'] in focus and not inactive(s)]
    if not shifts:
        return {}
    first, last = min(aware(s['start']) for s in shifts), max(aware(s['end']) for s in shifts)
    # A source outside every focused duty cannot acquire a subject, including at a partial-day boundary.
    relevant = [p for p in data['policies'] if p['status'] == 'active'
                and within_dates(p, first, last, data['organisation']['timezone'])]
    departments = {data['types'][s['assignment_type_id']]['department_id'] for s in shifts}
    members = {row['person_id'] for row in data.get('memberships', []) if row['department_id'] in departments}
    people = [pid for pid, person in data['people'].items() if person.get('active', True) and pid in members]
    instances = {}
    for policy in counting_views(data, relevant):
        check()
        kind = policy['definition']['rule']['kind']
        if policy['definition']['severity'] != 'advisory' or kind in SHAPE_KINDS | {'allocation_preference', 'block_quota', 'balance_goal'}:
            continue
        sources = [policy, *selector_sources(data['policies'], scope_tree(policy['definition']['scope']),
                                            data.get('_line_index_cache'))]

        def matches(scoped, pid, s):
            check()
            a, b = aware(s['start']), aware(s['end'])
            department = data['types'][s['assignment_type_id']]['department_id']

            def member(lo, hi):
                return pid is None or bool(department_memberships(data, pid, department, lo, hi))

            if policy_applies(data, scoped, pid, s, a, b) and member(a, b):
                return True
            # Partial participation can cross a dated fact/line boundary even when the whole session does not match.
            points = sorted({a, b, *(change_points(data, pid, a, b) if pid is not None else []),
                             *_source_points(sources, a, b, data['organisation']['timezone'], data['_line_index_cache'])})
            return len(points) > 2 and any(policy_applies(data, scoped, pid, s, lo, hi) and member(lo, hi)
                                          for lo, hi in zip(points, points[1:]))

        if kind in SESSION_KINDS:
            scoped = {**policy, 'definition': {**policy['definition'], 'scope': session_scope(policy['definition']['scope'])}}
            subjects = [s['id'] for s in shifts if matches(scoped, None, s)]
        else:
            named = named_people(policy['definition']['scope'])
            subjects = [pid for pid in people if (named is None or pid in named)
                        and any(matches(policy, pid, s) for s in shifts)]
        instances.update({(rule_copy_key(policy), subject): policy for subject in subjects})
    return instances


def rule_normalization(data, strengths, quota_weights):
    return (sum(strengths[p['definition'].get('strength', 'medium')] for p in rule_opportunities(data).values())
            + sum(quota_weights))


def rule_loss(violated, total, *, unit=1, clip=min, divide=truediv):
    """Fixed rule opportunities apply even before anyone is assigned."""
    return divide(clip(violated, unit * total), max(1, total))


def workload_normalization(data, measure):
    """Eligible focused audience and fixed cover budget; None means legacy snapshots lack those facts."""
    from .candidate_evidence import candidate_eligible, potential_candidate_people
    from .time import aware
    from .validation import effective_type_definition, index_person_rows, person_rows
    from .recurrence import series_definition

    check = data.get('_check_budget', lambda: None)
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
    potential = potential_candidate_people(data, eligible_shifts)
    service_scopes = {frozenset(s['service_ids']) for s in eligible_shifts}
    audience = []
    for pid in data['people']:
        if potential is not None and pid not in potential:
            continue
        services = {row['service_id'] for row in person_rows(data, 'eligibilities', pid)}
        if not any(scope <= services for scope in service_scopes):
            continue  # Dated qualification cannot succeed without a record for every session clinic.
        for s in eligible_shifts:
            check()
            if candidate_eligible(data, pid, s):
                audience.append(pid)
                break
    audience = tuple(sorted(audience))
    total = Fraction(0)
    for s in shifts:
        check()
        duration = (aware(s['end']) - aware(s['start'])) // timedelta(microseconds=1)
        requirements = effective_type_definition(data, s)['requirements']
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
    excess = {pid: positive(n * (load - (limit[pid] if isinstance(limit, dict) else limit)))
              for pid, load in loads.items()}
    variation = ratio(sum(excess.values()), n * demand)
    costs = {pid: ratio(value, demand) for pid, value in excess.items()}
    return variation, costs


def share_satisfaction(mean, worst, *, emphasis=0.5, unit=1, multiply=mul, divide=truediv):
    emphasis = Fraction(str(emphasis))
    return multiply(unit - mean, unit - divide(worst * emphasis.numerator, emphasis.denominator))


def round_goal_cost_ticks(ticks, *, divide=floordiv):
    """Goal rows use four decimal places, with deterministic half-up rounding."""
    return 100 * divide(ticks + 50, 100)


def goal_cost(value):
    # Suppress floating noise at an integer tick; the solver divides exact integers.
    ticks = floor(value * SCORE_SCALE + 1e-8)
    return round_goal_cost_ticks(ticks) / SCORE_SCALE

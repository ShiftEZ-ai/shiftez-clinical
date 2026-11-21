"""Shape rules and staff wishes over working days, the validator's side (the solver mirrors it in cp_sat_patterns.py).

A day is the local calendar day a duty starts on (a night belongs to the evening it starts). A rule's scope picks the
duties that count; days with no counted duty are off, including days outside what the snapshot knows. A breach is
reported when it touches a day of the rota being checked. All shape rules are advisory: findings are warnings.
"""

from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from packages.contracts.schemas import (PATTERN_WISH_KINDS, SHAPE_KINDS, WEEKEND_START_DEFAULT,
                                         WEEKEND_DAYS_DEFAULT)

from .recurrence import inactive
from .time import aware, local_midnight

ONE = timedelta(days=1)


def day_of(t, zone):
    return aware(t).astimezone(ZoneInfo(zone)).date()


def shape_focus_days(data, focus):
    zone = data['organisation']['timezone']
    return {day_of(s['start'], zone) for s in data['shifts']
            if (focus is None or s['id'] in focus) and not inactive(s)}


def effective_gap_focus(policy, focus_days):
    first, end = (date.fromisoformat(str(policy[field])) if policy.get(field) else None
                  for field in ('starts_on', 'ends_on'))
    return sorted(day for day in focus_days if (first is None or first <= day) and (end is None or day < end))


def gap_touches_focus(first, last, focus_days):
    """An interior day off can be in focus even when both bounding duties are in history."""
    index = bisect_right(focus_days, first)
    return index < len(focus_days) and focus_days[index] < last


def weekend_of(day, rule):
    """First day of the weekend `day` belongs to, or None on a weekday."""
    offset = (day.weekday() - rule['weekend_start']) % 7
    return day - timedelta(days=offset) if offset < rule['weekend_days'] else None


def runs(days, step=ONE):
    """Maximal runs of consecutive days, as (first, last)."""
    out = []
    for d in sorted(days):
        if out and d - out[-1][1] == step:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [tuple(r) for r in out]


def shape_duties(data, policy, pid, entries):
    """The dated duties a shape rule counts, shared by findings and reach observations."""
    from .validation import counting_policy, policy_applies
    zone = data['organisation']['timezone']
    counted = counting_policy(policy)
    return [(day_of(a, zone), shift) for shift, _, a, b, *choice in entries
            if policy_applies(data, counted, pid, shift, a, b)]


def shape_types(rows):
    types = defaultdict(set)
    for day, shift in rows:
        types[day].add(shift['assignment_type_id'])
    return types


def shape_counts(rule, types):
    """Observed run lengths and gaps: (first, last, comparison, actual, limit), including compliant runs."""
    if rule['kind'] == 'run_length':
        return [(a, b, comparison, (b - a).days + 1, rule[field])
                for a, b in runs(types) for comparison, field in (('minimum', 'minimum'), ('maximum', 'maximum'))
                if rule.get(field)]
    if rule['kind'] == 'days_off_run':
        days = sorted(types)
        return [(a, b, 'minimum', (b - a).days - 1, rule['minimum'])
                for a, b in zip(days, days[1:]) if (b - a).days > 1]
    return []


def breaches(rule, types):
    """Every breach of one shape rule for one person: (first day, last day, code, words); the days span the work involved.
    `types`: day -> duty type ids worked (counted duties only). Together/apart is handled by the caller."""
    kind, days, out = rule['kind'], set(types), []
    if kind == 'run_length':
        for a, b, comparison, n, limit in shape_counts(rule, types):
            if comparison == 'minimum' and n < limit:
                out.append((a, b, 'RUN_TOO_SHORT', f'{n} day{"s" if n > 1 else ""} in a row (at least {rule["minimum"]} preferred)'))
            if comparison == 'maximum' and n > limit:
                out.append((a, b, 'RUN_TOO_LONG', f'{n} days in a row (at most {rule["maximum"]} preferred)'))
    elif kind == 'days_off_run':
        for a, b, _, gap, limit in shape_counts(rule, types):
            if gap < limit:
                out.append((a, b, 'DAYS_OFF_SHORT', f'{gap} day{"s" if gap > 1 else ""} off between duties ({rule["minimum"]} together preferred)'))
    elif kind == 'weekend_shape':
        weekends = defaultdict(set)
        for d in days:
            first = weekend_of(d, rule)
            if first is not None:
                weekends[first].add(d)
        if rule['shape'] == 'max_consecutive':
            for a, b in runs(weekends, 7 * ONE):
                n = (b - a).days // 7 + 1
                if n > rule['maximum']:
                    out.append((a, b + (rule['weekend_days'] - 1) * ONE, 'WEEKENDS_TOO_LONG',
                                f'{n} weekends in a row (at most {rule["maximum"]} preferred)'))
        for first, worked in weekends.items():
            span = [first + i * ONE for i in range(rule['weekend_days'])]
            if rule['shape'] == 'complete' and len(worked) < len(span):
                out.append((span[0], span[-1], 'WEEKEND_SPLIT', f'works {len(worked)} of {len(span)} weekend days'))
            if rule['shape'] == 'identical_types' and len({frozenset(types[d]) for d in worked}) > 1:
                out.append((span[0], span[-1], 'WEEKEND_TYPES_DIFFER', 'different duties across the weekend'))
    elif kind == 'shift_succession':
        mode, first = rule['mode'], set(rule['from_type_ids'])
        if mode == 'forward':
            rank = {t: i for i, t in enumerate(rule['type_order'])}
            for d in days:
                now = [rank[t] for t in types[d] if t in rank]
                later = [rank[t] for t in types.get(d + ONE, ()) if t in rank]
                if now and later and min(later) < max(now):
                    out.append((d, d + ONE, 'SHIFT_ORDER', 'an earlier duty the day after a later one'))
        elif mode == 'avoid':
            to = set(rule['to_type_ids'])
            for d in days:
                if first & types[d] and any(to & types.get(d + k * ONE, set()) for k in range(1, rule['within_days'] + 1)):
                    out.append((d, d + rule['within_days'] * ONE, 'SHIFT_SUCCESSION', 'a duty that should not follow so soon'))
        else:
            n, r = rule['series_length'], rule['rest_days']
            for d in days:
                series = all(first & types.get(d - i * ONE, set()) for i in range(n))
                if series and not first & types.get(d + ONE, set()) and any(d + k * ONE in days for k in range(1, r + 1)):
                    out.append((d - (n - 1) * ONE, d + r * ONE, 'REST_AFTER_SERIES', f'work within {r} day{"s" if r > 1 else ""} after a series'))
    return out


def evaluate_shapes(data, commitments, focus, add):
    """Findings for every active advisory shape rule. `commitments`: person -> [(shift, participant, start, end)]."""
    zone = data['organisation']['timezone']
    focus_days = shape_focus_days(data, focus)
    from .validation import policy_applies
    for policy in data['policies']:
        data.get('_check_budget', lambda: None)()
        rule = policy['definition']['rule']
        if rule['kind'] not in SHAPE_KINDS:
            continue
        gap_focus = effective_gap_focus(policy, focus_days) if rule['kind'] == 'days_off_run' else ()
        counted, focal = {}, {}
        for pid, entries in commitments.items():
            data.get('_check_budget', lambda: None)()
            if rule['kind'] == 'together_apart' and pid not in rule['person_ids']:
                continue
            rows = shape_duties(data, policy, pid, entries)
            if rows:
                counted[pid] = rows
                focal[pid] = [(day_of(a, zone), s) for s, _, a, b in entries if s['id'] in focus
                              and policy_applies(data, policy, pid, s, a, b)]

        def report(pid, a, b, code, words, shift=None):
            applicable = [(d, s) for d, s in focal.get(pid, []) if a <= d <= b]
            if not applicable and not gap_touches_focus(a, b, gap_focus):
                return
            shift = shift or (applicable[0][1] if applicable else None)
            add(code, f"{policy['name']}: {words}", shift['id'] if shift else None, pid,
                local_midnight(a, zone), local_midnight(b + ONE, zone), 'warning', policy, {'kind': rule['kind']})

        if rule['kind'] == 'together_apart':
            level = rule.get('level', 'day')
            keys = defaultdict(dict)  # day or session -> person -> shift
            for pid, rows in counted.items():
                for d, s in rows:
                    keys[d if level == 'day' else s['id']][pid] = (d, s)
            for key, who in keys.items():
                d = next(iter(who.values()))[0]
                if rule['mode'] == 'apart' and len(who) > 1:
                    for pid, (_, s) in who.items():
                        report(pid, d, d, 'WORK_APART', f'works with {len(who) - 1} of the people to keep apart', s)
                elif rule['mode'] == 'together' and len(who) < len(rule['person_ids']):
                    for pid, (_, s) in who.items():
                        report(pid, d, d, 'WORK_TOGETHER', f'works without {len(rule["person_ids"]) - len(who)} of the people to keep together', s)
            continue
        for pid, rows in counted.items():
            types = shape_types(rows)
            for a, b, code, words in breaches(rule, types):
                report(pid, a, b, code, words)

    outcomes = wish_outcomes(data) if data.get('wishes') else {}
    for wish in data.get('wishes') or []:
        if data.get('person_rule_subjects') is not None and wish['person_id'] not in data['person_rule_subjects']:
            continue
        if wish_shape(wish) is None or wish['id'] not in outcomes or outcomes[wish['id']][0] == 1:
            continue
        days = wish_days(wish, data)
        add('WISH_UNMET', f"Pattern wish '{wish['kind'].replace('_', ' ')}' not met", None, wish['person_id'],
            local_midnight(days[0], zone), local_midnight(days[-1] + ONE, zone), 'warning',
            extra={'wish_id': wish['id'], 'kind': wish['kind']})


def wish_days(wish, data):
    """The wish's days inside the rota being checked."""
    w = data['workspace']
    lo, hi = max(date.fromisoformat(str(wish['first_day'])), date.fromisoformat(str(w['starts_on']))), \
        min(date.fromisoformat(str(wish['last_day'])), date.fromisoformat(str(w['ends_on'])) - ONE)
    return [lo + i * ONE for i in range((hi - lo).days + 1)]


def wish_shape(wish):
    """A personal pattern uses the same shape definition and evaluator as department patterns, without a rule copy."""
    kind = wish['kind']
    if kind == 'max_days_in_row':
        return {'kind': 'run_length', 'maximum': wish['limit']}
    if kind == 'min_days_off_together':
        return {'kind': 'days_off_run', 'minimum': wish['limit']}
    if kind in {'max_weekends_in_row', 'whole_weekends'}:
        return {'kind': 'weekend_shape', 'shape': 'complete' if kind == 'whole_weekends' else 'max_consecutive',
                'maximum': wish.get('limit'),
                'weekend_start': wish.get('weekend_start') if wish.get('weekend_start') is not None else WEEKEND_START_DEFAULT,
                'weekend_days': wish.get('weekend_days') if wish.get('weekend_days') is not None else WEEKEND_DAYS_DEFAULT}
    return None


def wish_day_effect(wish, types):
    """Directional alignment with a day's duty types; None is neutral (and honoured)."""
    if wish['kind'] in PATTERN_WISH_KINDS:
        return None
    if wish['kind'] == 'prefer_off':
        return 'against' if types else 'grants'
    if wish['kind'] == 'prefer_on':
        return 'grants' if types else 'against'
    matching = wish['assignment_type_id'] in types
    if wish['kind'] == 'prefer_shift_type':
        return 'grants' if matching else 'against' if types else None
    if wish['kind'] == 'avoid_shift_type':
        return 'against' if matching else None
    raise ValueError(f"Unknown wish kind: {wish['kind']}")


def wish_worked(data):
    """Official duty types by person and local start day, for outcomes and frozen publication evidence."""
    zone = data['organisation']['timezone']
    worked = defaultdict(lambda: defaultdict(set))  # person -> day -> duty types
    for s in data['shifts']:
        if not inactive(s):
            for p in s['participants']:
                worked[p['person_id']][day_of(p['start'], zone)].add(s['assignment_type_id'])
    return worked


def wish_evaluations(data, worked=None):
    """Wish id -> (honoured share, days counted, declined days); patterns are granted or declined as a whole.
    `worked` supplies the caller's person/day index so greedy trials never rescan the whole rota.
    """
    if worked is None:
        worked = wish_worked(data)
    out = {}
    for wish in data.get('wishes') or []:
        days = wish_days(wish, data)
        if not days:
            continue
        mine = worked.get(wish['person_id'], {})
        shape = wish_shape(wish)
        if shape is not None:
            broken = bool(breaches(shape, {day: mine[day] for day in days if mine.get(day)}))
            out[wish['id']] = (float(not broken), len(days), days if broken else [])
        else:
            missed = [day for day in days if wish_day_effect(wish, mine.get(day, ())) == 'against']
            out[wish['id']] = (1 - len(missed) / len(days), len(days), missed)
    return out


def wish_outcomes(data, worked=None):
    """Wish id -> (honoured share, days counted), shared by validation, scoring and greedy trials."""
    return {wid: (share, days) for wid, (share, days, _) in wish_evaluations(data, worked).items()}

"""Shape rules and staff wishes as soft costs (mirrors packages/domain/patterns.py, the validator's side).

Per person and day, a "worked" literal is built only for the days a rule or wish looks at, and shared between rules
with the same duties. Department breaches made only of constants cost nothing to try (the validator still reports
them). Each breach costs 1000 x strength; a wish costs 1000 x medium strength x its weight (points share of the budget
x declined-wish catch-up), spread over its days, or once on any breach for a personal pattern, including fixed work.
Encodings: forbidden gap patterns for shortest runs and days off, sliding windows for longest runs, paired
inequalities for weekends, succession and together/apart.
"""
from collections import defaultdict
from datetime import timedelta

from .recurrence import inactive
from .patterns import SHAPE_KINDS, day_of, weekend_of, wish_days, wish_shape
from .validation import policy_applies
from .cp_sat_score import ScoreModel

ONE = timedelta(days=1)


def add_patterns(model, data, by_person, strengths, *, check=None, shape_breaches=None, wish_breaches=None):
    """Returns (raw cost/expression groups, their worst total), scaled together by the caller."""
    zone = data['organisation']['timezone']
    check = check or data.get('_check_budget')
    terms, worst = [], [0]
    fixed = {}
    flags = {}
    score = ScoreModel(model)
    if shape_breaches is not None:
        from .score import rule_copy_key
        focus_days = {day_of(s['start'], zone) for s in data['shifts']
                      if s['id'] in data['focus_ids'] and not inactive(s)}

    def constant(x):
        if x.index not in fixed:
            fixed[x.index] = list(model.proto.variables[x.index].domain) == [1, 1]
        return fixed[x.index]

    def any_of(xs):
        """1 when some duty cannot be changed, 0 with none, else one shared literal per set of duties."""
        if not xs:
            return 0
        if any(constant(x) for x in xs):
            return 1
        key = frozenset(x.index for x in xs)
        if len(key) == 1:
            return xs[0]
        if key not in flags:
            flags[key] = model.new_bool_var('worked')
            model.add_max_equality(flags[key], xs)
            score.record(flags[key], 'max', xs)
        return flags[key]

    def breach(cost, *rows, include_fixed=False, person=None, span=None):
        """One 0/1 breach from rows (plus, minus, k): sum(plus) - sum(minus) - k."""
        if shape_breaches is not None or include_fixed and wish_breaches is not None:
            if shape_breaches is not None and span is not None and not any(span[0] <= day <= span[1] for day in focus_days):
                return
            broken = []
            for index, (plus, minus, k) in enumerate(rows):
                if check and index % 64 == 0:
                    check()
                if k == len(plus) - 1 and all(0 <= lo <= hi <= 1
                        for lo, hi in (score.bounds(t) for t in (*plus, *minus))):
                    broken.append(score.minimum([*plus, *(1 - t for t in minus)], 1))
                else:
                    broken.append(score.positive(sum(plus) - sum(minus) - k))
            flag = score.any(broken)
            if shape_breaches is not None:
                shape_breaches.setdefault((rule_copy_key(policy), person), []).append(flag)
                terms.append(cost * flag)
            else:
                wish_breaches[wish['id']] = flag
                terms.append((cost, [flag]))
            worst[0] += cost
            return
        if include_fixed and any(all(isinstance(t, int) for t in (*plus, *minus))
                                and sum(plus) - sum(minus) > k for plus, minus, k in rows):
            terms.append((cost, [1]))
            worst[0] += cost
            return
        live = []
        for index, (plus, minus, k) in enumerate(rows):
            if check and index % 64 == 0:
                check()
            top = sum(1 if not isinstance(t, int) else t for t in plus) - sum(t for t in minus if isinstance(t, int)) - k
            if top > 0 and any(not isinstance(t, int) for t in (*plus, *minus)):
                live.append((plus, minus, k))
        if not live:
            return
        v = model.new_bool_var('soft_breach')
        for index, (plus, minus, k) in enumerate(live):
            if check and index % 64 == 0:
                check()
            model.add(v >= sum(plus) - sum(minus) - k)
        terms.append((cost, [v]))
        worst[0] += cost

    def shape(rule, by_day, by_type, cost, *, combine=False, person=None):
        if not by_day:
            return
        rows = []

        def emit(*instance, span):
            if check:
                check()
            if combine:
                rows.extend(instance)
            else:
                breach(cost, *instance, person=person, span=span)

        W = lambda d: any_of(by_day.get(d, []))  # noqa: E731

        def T(types, d):
            return any_of([x for t in types for x in by_type[t].get(d, [])])
        days = sorted(by_day)
        kind = rule['kind']
        if kind == 'run_length':
            m, top = rule.get('minimum'), rule.get('maximum')
            for d in days:
                for g in range(1, min(m or 1, (days[-1] - days[0]).days + 2)):  # a run of exactly g < m days starting on d
                    emit(([W(d + i * ONE) for i in range(g)], [W(d - ONE), W(d + g * ONE)], g - 1),
                         span=(d, d + (g - 1) * ONE))
                if top and top < len(days):
                    emit(([W(d + i * ONE) for i in range(top + 1)], [], top), span=(d, d + top * ONE))
        elif kind == 'days_off_run':
            # Longer gaps share cumulative work counts rather than rebuilding every interior sum.
            # A one-day gap is already one literal and needs no extra model variables.
            prefix, fixed_prefix = [0], [0]
            if rule['minimum'] > 2:
                for i in range((days[-1] - days[0]).days + 1):
                    if check and i % 64 == 0:
                        check()
                    previous, worked = prefix[-1], W(days[0] + i * ONE)
                    # Keep fixed work explicit so immutable department breaches remain uncharged.
                    fixed_prefix.append(fixed_prefix[-1] + (worked if isinstance(worked, int) else 0))
                    if isinstance(worked, int):
                        prefix.append(previous)
                    else:
                        total = model.new_int_var(0, i + 1, 'worked_prefix')
                        model.add(total == previous + worked)
                        score.record(total, 'linear', [previous + worked])
                        prefix.append(total)
            for d in days:
                if check:
                    check()
                first = (d - days[0]).days
                for g in range(1, min(rule['minimum'], (days[-1] - d).days)):  # worked, g days off, worked
                    if g == 1:
                        interior = [W(d + ONE)]
                    else:
                        before, after = prefix[first + 1], prefix[first + g + 1]
                        interior = [fixed_prefix[first + g + 1] - fixed_prefix[first + 1],
                                    0 if before is after else after - before]
                    emit(([W(d), W(d + (g + 1) * ONE)], interior, 1), span=(d, d + (g + 1) * ONE))
        elif kind == 'weekend_shape':
            for first in sorted({weekend_of(d, rule) for d in days} - {None}):
                span = [first + i * ONE for i in range(rule['weekend_days'])]
                pairs = [(a, b) for a in span for b in span if a != b]
                if rule['shape'] == 'max_consecutive':
                    top = rule['maximum']
                    if top < len(days):
                        emit(([any_of([x for i in range(rule['weekend_days'])
                                        for x in by_day.get(first + (7 * k + i) * ONE, [])])
                               for k in range(top + 1)], [], top),
                             span=(first, first + (7 * top + rule['weekend_days'] - 1) * ONE))
                elif rule['shape'] == 'complete':
                    emit(*[([W(a)], [W(b)], 0) for a, b in pairs], span=(span[0], span[-1]))
                else:
                    emit(*[([T([t], a), W(b)], [T([t], b)], 1) for a, b in pairs for t in sorted(by_type)],
                         span=(span[0], span[-1]))
        elif kind == 'shift_succession':
            mode, first = rule['mode'], rule['from_type_ids']
            for d in days:
                if mode == 'forward':
                    order = rule['type_order']
                    emit(*[([T([order[i]], d), T([order[j]], d + ONE)], [], 1)
                           for i in range(len(order)) for j in range(i)], span=(d, d + ONE))
                elif mode == 'avoid':
                    emit(*[([T(first, d), T(rule['to_type_ids'], d + k * ONE)], [], 1)
                           for k in range(1, rule['within_days'] + 1)], span=(d, d + rule['within_days'] * ONE))
                else:
                    n = rule['series_length']
                    emit(*[([T(first, d - i * ONE) for i in range(n)] + [W(d + k * ONE)], [T(first, d + ONE)], n)
                           for k in range(1, rule['rest_days'] + 1)],
                         span=(d - (n - 1) * ONE, d + rule['rest_days'] * ONE))
        if combine:
            breach(cost, *rows, include_fixed=True)

    for policy in data['policies']:
        if check:
            check()
        rule = policy['definition']['rule']
        if rule['kind'] not in SHAPE_KINDS or policy['definition']['severity'] != 'advisory':
            continue
        cost = 1000 * strengths[policy['definition'].get('strength', 'medium')]
        people = sorted(rule['person_ids'] if rule['kind'] == 'together_apart' else by_person)
        rows = {}  # person -> [(day, shift, x)]
        for pid in people:
            if check:
                check()
            mine = [(day_of(a, zone), s, x) for s, _, a, b, x in by_person.get(pid, []) if policy_applies(data, policy, pid, s, a, b)]
            if mine:
                rows[pid] = mine
        if rule['kind'] == 'together_apart':
            level = rule.get('level', 'day')
            keys = defaultdict(lambda: defaultdict(list))
            for pid, mine in sorted(rows.items()):
                for d, s, x in mine:
                    keys[d if level == 'day' else s['id']][pid].append(x)
            for key in sorted(keys):
                who = keys[key]
                worked = {pid: any_of(who.get(pid, [])) for pid in rule['person_ids']}
                pairs = [(p, q) for p in rule['person_ids'] for q in rule['person_ids'] if p != q]
                if shape_breaches is not None:
                    day = key if level == 'day' else next(d for d, s, _ in rows[next(iter(who))] if s['id'] == key)
                    for pid in sorted(who):
                        others = [q for q in rule['person_ids'] if q != pid]
                        if rule['mode'] == 'apart':
                            breach(cost, *[([worked[pid], worked[q]], [], 1) for q in others], person=pid, span=(day, day))
                        else:
                            breach(cost, *[([worked[pid]], [worked[q]], 0) for q in others], person=pid, span=(day, day))
                    continue
                if rule['mode'] == 'apart':
                    breach(cost, *[([worked[p], worked[q]], [], 1) for p, q in pairs if p < q])
                else:
                    breach(cost, *[([worked[p]], [worked[q]], 0) for p, q in pairs])
            continue
        for pid, mine in sorted(rows.items()):
            by_day, by_type = defaultdict(list), defaultdict(lambda: defaultdict(list))
            for d, s, x in mine:
                by_day[d].append(x)
                by_type[s['assignment_type_id']][d].append(x)
            shape(rule, by_day, by_type, cost, person=pid)

    # Staff wishes: any duty counts, whatever the rules' scopes.
    if shape_breaches is not None:
        return terms, worst[0]
    medium = strengths['medium']
    wish_duties = {}
    for wish in data.get('wishes') or []:
        if check:
            check()
        days = wish_days(wish, data)
        if not days or not wish.get('weight'):
            continue
        pid = wish['person_id']
        if pid not in wish_duties:
            by_day, by_type = defaultdict(list), defaultdict(lambda: defaultdict(list))
            for s, _, a, _, x in by_person.get(pid, []):
                d = day_of(a, zone)
                by_day[d].append(x)
                by_type[s['assignment_type_id']][d].append(x)
            wish_duties[pid] = by_day, by_type
        by_day, by_type = wish_duties[pid]
        typed = by_type.get(wish.get('assignment_type_id'), {})
        rule = wish_shape(wish)
        if rule is not None:
            if wish_breaches is not None:
                wish_breaches[wish['id']] = 0
            shape(rule, {d: by_day[d] for d in days if by_day.get(d)}, {},
                  1000 * medium * wish['weight'], combine=True)
            continue
        cost = 1000 * medium * wish['weight'] / len(days)
        for d in days:
            w, t = any_of(by_day.get(d, [])), any_of(typed.get(d, []))
            if wish['kind'] == 'prefer_shift_type':
                breach(cost, ([w], [t], 0))
                continue
            x = {'prefer_off': w, 'prefer_on': w, 'avoid_shift_type': t}[wish['kind']]
            if isinstance(x, int):
                continue  # nothing the solver can change on this day
            terms.append((cost, [1 - x if wish['kind'] == 'prefer_on' else x]))
            worst[0] += cost
    return terms, worst[0]

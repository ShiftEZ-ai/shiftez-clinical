"""Integer arithmetic for the score's 0..1 components (one million ticks per unit).

Ratios and products truncate by less than one tick; coefficients keep the configured
weights exactly, rather than rounding each weighted opportunity independently.
"""
from .recurrence import inactive
from fractions import Fraction
from math import gcd, lcm

from ortools.sat.python import cp_model
from .score_normalization import PREFERENCE_MEAN, PREFERENCE_WORST, SCORE_SCALE, share_satisfaction, rule_normalization, rule_loss, workload_normalization, workload_limit, workload_costs, coverage_satisfaction, reduced_component_weights


class ScorePrecisionError(ValueError):
    pass


def begin_score_program(model, prefix_size=None):
    """Record exact derived arithmetic; base decision values come only from the checked prefix."""
    model._score_program = {'prefix_size': len(model.proto.variables) if prefix_size is None else prefix_size,
                            'scale': SCORE_SCALE, 'ops': []}
    return model._score_program


def score_expression(expression):
    if isinstance(expression, (int, dict)):
        return expression
    flat = cp_model.FlatIntExpr(expression)
    offset, terms = int(flat.offset), []
    for variable, coefficient in zip(flat.vars, flat.coeffs):
        low, high = variable.domain.min(), variable.domain.max()
        if low == high:
            offset += int(coefficient) * low
        elif coefficient:
            terms.append([variable.index, int(coefficient)])
    return {'offset': offset, 'terms': terms} if terms else offset


def finish_score_program(model, components, assigned):
    """Keep root dependencies only; incomplete recording cannot become a displayed score."""
    recorded = getattr(model, '_score_program', None)
    if recorded is None:
        return None
    roots = {name: None if pair is None else [score_expression(pair[0]), score_expression(pair[1])]
             for name, pair in components.items()}
    assigned = score_expression(assigned)
    goal_cost_checks = recorded.get('goal_cost_checks', [])
    nodes = {row[0]: row for row in recorded['ops']}
    needed, pending = set(), ([expression for pair in roots.values() if pair is not None for expression in pair]
                             + [assigned] + [expression for row in goal_cost_checks for expression in row])
    while pending:
        expression = pending.pop()
        if isinstance(expression, int):
            continue
        for index, _ in expression['terms']:
            if 0 <= index < recorded['prefix_size'] or index in needed:
                continue
            if index not in nodes:
                return None
            needed.add(index)
            pending.extend(nodes[index][2])
    ops, seen = [], set()
    for output, operation, operands in recorded['ops']:
        if output not in needed:
            continue
        if output in seen or any(not (0 <= index < recorded['prefix_size'] or index in seen)
                for expression in operands if isinstance(expression, dict) for index, _ in expression['terms']):
            return None
        ops.append([output, operation, operands])
        seen.add(output)
    return {'prefix_size': recorded['prefix_size'], 'scale': SCORE_SCALE,
            'ops': ops, 'components': roots, 'assigned': assigned, 'goal_cost_checks': goal_cost_checks}


def fractional_scale(factors, *, relative_unit=10_000):
    """Integer coefficients with exact fractions and bounded relative error for floats."""
    factors = [(Fraction(str(factor)) if isinstance(factor, float) else Fraction(factor), isinstance(factor, float))
               for factor in factors]
    scale = lcm(*(factor.denominator for factor, approximate in factors if not approximate))
    costs = {factor for factor, approximate in factors if approximate and factor}
    # Repeating costs must not inflate every other coefficient or erase a small positive cost.
    while any(abs(round(cost * scale) - cost * scale) * relative_unit > abs(cost * scale) for cost in costs):
        if scale * 10 > 10**12:
            return None
        scale *= 10
    return [round(factor * scale) for factor, _ in factors]


class ScoreModel:
    def __init__(self, model, settings=None):
        self.model = model
        self.settings = settings or {}
        # Domains stay fixed throughout compilation; shortlist narrowing begins only after scoring is built.
        if not hasattr(model, '_score_domains'):
            model._score_domains = {}
        self.domains = model._score_domains

    def record(self, output, operation, operands):
        program = getattr(self.model, '_score_program', None)
        if program is not None:
            program['ops'].append([output.index, operation, [score_expression(x) for x in operands]])

    def temporary(self, operation, operands):
        """Negative registers are metadata only; no native variable or constraint is added."""
        program = self.model._score_program
        index = -len(program['ops']) - 1
        program['ops'].append([index, operation, [score_expression(x) for x in operands]])
        return {'offset': 0, 'terms': [[index, 1]]}

    def variable_bounds(self, variable):
        index = variable.index
        if index not in self.domains:
            domain = variable.domain
            self.domains[index] = domain.min(), domain.max()
        return self.domains[index]

    def bounds(self, expression):
        if isinstance(expression, int):
            return expression, expression
        if isinstance(expression, cp_model.IntVar):
            return self.variable_bounds(expression)
        flat = cp_model.FlatIntExpr(expression)
        low = high = flat.offset
        for variable, coefficient in zip(flat.vars, flat.coeffs):
            a, b = self.variable_bounds(variable)
            low += min(coefficient * a, coefficient * b)
            high += max(coefficient * a, coefficient * b)
        return low, high

    def var(self, expression, low=None, high=None, name='score'):
        if isinstance(expression, int):
            return expression
        original = self.bounds(expression)
        if low is None or high is None:
            low, high = original
        if original[0] == original[1]:
            return original[0]
        if isinstance(expression, cp_model.IntVar) and low <= original[0] and original[1] <= high:
            return expression
        if max(abs(low), abs(high)) >= 2**62:
            raise ScorePrecisionError('Score quantity exceeds supported integer precision')
        value = self.model.new_int_var(low, high, name)
        self.model.add(value == expression)
        self.record(value, 'linear', [expression])
        return value

    def maximum(self, expressions, bound=SCORE_SCALE):
        bounded = [(x, self.bounds(x)) for x in expressions] or [(0, (0, 0))]
        expressions = [low if low == high else x for x, (low, high) in bounded]
        if all(isinstance(x, int) for x in expressions):
            return max(expressions)
        if len(expressions) == 1:
            return expressions[0]
        low = max(low for _, (low, _) in bounded)
        if bound >= 2**62:
            raise ScorePrecisionError('Score quantity exceeds supported integer precision')
        value = self.model.new_int_var(max(0, low), bound, 'score_max')
        self.model.add_max_equality(value, expressions)
        self.record(value, 'max', expressions)
        return value

    def minimum(self, expressions, bound=SCORE_SCALE):
        bounded = [(x, self.bounds(x)) for x in expressions]
        expressions = [low if low == high else x for x, (low, high) in bounded]
        if all(isinstance(x, int) for x in expressions):
            return min(expressions)
        low = min(low for _, (low, _) in bounded)
        high = min(high for _, (_, high) in bounded)
        if low == high:
            return low
        # Constant ceilings above every possible minimum cannot change the result.
        live_high = min(high for _, (low, high) in bounded if low != high)
        expressions = [x for x in expressions if not isinstance(x, int) or x < live_high]
        if len(expressions) == 1:
            return expressions[0]
        value = self.model.new_int_var(low, min(bound, high), 'score_min')
        self.model.add_min_equality(value, expressions)
        self.record(value, 'min', expressions)
        return value

    def any(self, expressions):
        live, minimum = [], 0
        for expression in expressions:
            low, high = self.bounds(expression)
            if low == 1:
                return 1
            if high:
                live.append(low if low == high else expression)
                minimum = max(minimum, low)
        if not live:
            return 0
        if all(isinstance(x, int) for x in live):
            return max(live)
        if len(live) == 1:
            return live[0]
        value = self.model.new_int_var(minimum, 1, 'score_max')
        if all(isinstance(x, cp_model.IntVar) and x.is_boolean for x in live):
            # Binary OR fits one row; wider presence keeps Boolean clauses.
            if len(live) == 2:
                self.model.add_linear_constraint(2 * value - sum(live), 0, 1)
            else:
                self.model.add_bool_or(live).only_enforce_if(value)
                self.model.add_bool_and([x.Not() for x in live]).only_enforce_if(value.Not())
        else:
            self.model.add_max_equality(value, live)
        self.record(value, 'max', live)
        return value

    def positive(self, expression):
        if isinstance(expression, int):
            return int(expression > 0)
        low, high = self.bounds(expression)
        if high <= 0 or low > 0:
            return int(low > 0)
        if (low, high) == (0, 1):
            return expression
        value = self.model.new_bool_var('score_present')
        self.model.add(expression > 0).only_enforce_if(value)
        self.model.add(expression <= 0).only_enforce_if(value.Not())
        self.record(value, 'positive', [expression])
        return value

    def ratio(self, numerator, denominator, bound=SCORE_SCALE):
        if isinstance(numerator, int) and isinstance(denominator, int):
            return min(bound, SCORE_SCALE * numerator // max(1, denominator))
        if isinstance(denominator, int) and denominator > 0:
            flat = cp_model.FlatIntExpr(numerator)
            if all(value * SCORE_SCALE % denominator == 0 for value in [flat.offset, *flat.coeffs]):
                exact = flat.offset * SCORE_SCALE // denominator + sum(
                    coefficient * SCORE_SCALE // denominator * variable for variable, coefficient in zip(flat.vars, flat.coeffs))
                low, high = self.bounds(exact)
                if 0 <= low <= high <= bound:
                    return exact
        numerator = self.var(numerator)
        denominator = self.var(denominator)
        _, top = self.bounds(numerator)
        denominator = self.maximum([1, denominator], max(1, self.bounds(denominator)[1]))
        if isinstance(numerator, int) and isinstance(denominator, int):
            return min(bound, SCORE_SCALE * numerator // denominator)
        if self.bounds(denominator)[1] >= 2**62:
            raise ScorePrecisionError('Score quantity exceeds supported integer precision')
        if top * SCORE_SCALE >= 2**62:
            high = self.bounds(denominator)[1]
            if high * 1_000 >= 2**62:
                raise ScorePrecisionError('Score quantity exceeds supported integer precision')
            # Long division preserves all million ticks without scaling the large numerator at once.
            raw = self.minimum([bound, self.divide(numerator, denominator, top)], bound)
            remainder = numerator
            for _ in range(2):
                rest = self.model.new_int_var(0, min(self.bounds(remainder)[1], high - 1), 'score_remainder')
                self.model.add_modulo_equality(rest, remainder, denominator)
                self.record(rest, 'mod', [remainder, denominator])
                remainder = 1_000 * rest
                digit = self.model.new_int_var(0, 999, 'score_digit')
                self.model.add_division_equality(digit, remainder, denominator)
                self.record(digit, 'div', [remainder, denominator])
                raw = self.minimum([bound, 1_000 * raw + digit], bound)
            return raw
        raw = self.model.new_int_var(0, max(0, top * SCORE_SCALE), 'score_ratio')
        self.model.add_division_equality(raw, SCORE_SCALE * numerator, denominator)
        self.record(raw, 'div', [SCORE_SCALE * numerator, denominator])
        return self.minimum([bound, raw], bound)

    def product(self, left, right):
        if isinstance(left, int) and isinstance(right, int):
            return left * right // SCORE_SCALE
        product = self.model.new_int_var(0, SCORE_SCALE ** 2, 'score_product')
        self.model.add_multiplication_equality(product, [left, right])
        self.record(product, 'mul', [left, right])
        value = self.model.new_int_var(0, SCORE_SCALE, 'score_product_scaled')
        self.model.add_division_equality(value, product, SCORE_SCALE)
        self.record(value, 'div', [product, SCORE_SCALE])
        return value

    def mean(self, items):
        items = [(Fraction(str(weight)), loss) for weight, loss in items if weight]
        unit = lcm(*(weight.denominator for weight, _ in items))
        weights = [(int(weight * unit), loss) for weight, loss in items]
        # Losses already have SCORE_SCALE ticks, so cancel that factor before dividing.
        numerator = self.var(sum(weight * loss for weight, loss in weights))
        total = sum(weight for weight, _ in weights) or 1
        if isinstance(numerator, int):
            return numerator // total
        value = self.model.new_int_var(0, SCORE_SCALE, 'score_mean')
        self.model.add_division_equality(value, numerator, total)
        self.record(value, 'div', [numerator, total])
        return value

    def share_loss(self, mean, worst):
        return SCORE_SCALE - share_satisfaction(mean, worst, unit=SCORE_SCALE,
                                               emphasis=self.settings.get('fairness_worst_off_emphasis', 0.5),
                                               multiply=self.product, divide=self.divide)

    def preferences_loss(self, mean, worst):
        return self.mean([(self.settings.get('preferences_mean_weight', PREFERENCE_MEAN), mean),
                          (self.settings.get('preferences_worst_weight', PREFERENCE_WORST), worst)])

    def divide(self, numerator, denominator, bound=SCORE_SCALE):
        numerator, denominator = self.var(numerator), self.var(denominator)
        denominator = self.maximum([1, denominator], max(1, self.bounds(denominator)[1]))
        if isinstance(numerator, int) and isinstance(denominator, int):
            return numerator // denominator
        if self.bounds(denominator)[1] >= 2**62:
            raise ScorePrecisionError('Score quantity exceeds supported integer precision')
        value = self.model.new_int_var(0, bound, 'score_divide')
        self.model.add_division_equality(value, numerator, denominator)
        self.record(value, 'div', [numerator, denominator])
        return value


def weighted_component(model, components, weights, tie_multiplier=1):
    """Return lexicographic loss parts and scale, preserving component-tick ordering.

    GCD reduction is exact for every active subset. Distinct N/D ratios differ by
    at least 1/(D1*D2), so K=floor(H**2*N/D), H=max(D), preserves strict order
    and exact ties. If K or its intermediates/tie-break would exceed int64, long
    division returns [K//H, K%H] instead. With five weights <=1,000,000, long
    division intermediates are <=max(SCORE_SCALE*H, H**2), not SCORE_SCALE*H**3.
    Optimize parts in order, applying the assignment tie-break only to the last.
    """
    q = ScoreModel(model)
    weights = reduced_component_weights(weights)
    numerator = denominator = 0
    for name, (loss, present) in components.items():
        weight = weights[name]
        if not weight or not q.bounds(present)[1]:
            continue
        gated = q.minimum([loss, SCORE_SCALE * present])
        numerator += weight * gated
        denominator += weight * present
    low, high = q.bounds(denominator)
    if not high:
        return [0], SCORE_SCALE
    if low == high:
        # Objective offsets are doubles; keep large constant losses in an integer variable.
        loss = model.new_int_var(0, SCORE_SCALE * high, 'score_weighted')
        model.add(loss == numerator)
        q.record(loss, 'linear', [numerator])
        return [loss], SCORE_SCALE * high
    scale = high ** 2
    if SCORE_SCALE * scale * max(high, tie_multiplier) + tie_multiplier - 1 < 2**62:
        return [q.divide(scale * numerator, denominator, SCORE_SCALE * scale)], SCORE_SCALE * scale
    denominator = q.maximum([1, denominator], high)
    numerator = q.var(numerator)
    whole = q.divide(numerator, denominator)
    remainder = model.new_int_var(0, high - 1, 'score_remainder')
    model.add_modulo_equality(remainder, numerator, denominator)
    q.record(remainder, 'mod', [numerator, denominator])
    first = q.divide(high * remainder, denominator, high - 1)
    next_remainder = model.new_int_var(0, high - 1, 'score_remainder')
    model.add_modulo_equality(next_remainder, high * remainder, denominator)
    q.record(next_remainder, 'mod', [high * remainder, denominator])
    last = q.divide(high * next_remainder, denominator, high - 1)
    return [q.var(high * whole + first, 0, SCORE_SCALE * high), last], SCORE_SCALE * scale


def coverage_component(model, rows, factor=0.25, *, check=None):
    """Headcount/target coverage from the compiler's existing eligible interval counts."""
    q = ScoreModel(model)
    check = check or (lambda: None)
    rows = [row for row in rows if row[1] or any(row[2])]
    unit = gcd(*(duration for _, _, _, duration in rows)) or 1
    required, gaps, desired, wanted = 0, [], [], []
    for cover, minimum, targets, duration in rows:
        check()
        duration //= unit
        required += minimum * duration
        if minimum:
            gaps.append(duration * q.maximum([0, minimum - cover], minimum))
        for target in targets:
            missing = q.maximum([0, target - cover], target)
            # Headcount gaps retain target findings as derived details, outside the score.
            active = q.minimum([q.positive(missing), q.positive(cover - minimum + 1)], 1)
            desired.append(duration * q.minimum([missing, target * active], target))
            wanted.append(duration * target * active)
    unfilled = q.ratio(sum(gaps), required) if required else 0
    short = q.ratio(sum(desired), required or sum(wanted)) if desired else 0
    value = coverage_satisfaction(unfilled, short, factor=factor, unit=SCORE_SCALE, multiply=q.product, divide=q.divide)
    return SCORE_SCALE - value, 1 if required else q.positive(sum(desired))


def rule_component(model, data, choices, participants, breaches, quota_items, strengths):
    check = data.get('_check_budget', lambda: None)
    q = ScoreModel(model)
    numerator = 0
    for key, flags in breaches.items():
        check()
        numerator += SCORE_SCALE * strengths[key[0][4]] * q.any(flags)
    # Quota rows have a fixed audience and retain their fractional losses.
    numerator += sum(weight * loss for weight, loss, _ in quota_items)
    denominator = rule_normalization(data, strengths, [weight for weight, _, _ in quota_items])
    return rule_loss(numerator, denominator, unit=SCORE_SCALE,
                     clip=lambda value, bound: q.minimum([value, bound], bound), divide=q.divide), int(denominator > 0)


def workload_component(model, data, choices, participants, measure, *, policy=None):
    """Fixed cover budget and audience; only overload above the equal share loses points."""
    from datetime import timedelta
    from math import gcd
    from functools import reduce
    from collections import defaultdict
    from .time import aware

    check = data.get('_check_budget', lambda: None)
    from .fairness import goal_applies, workload_parameters

    q = ScoreModel(model, (data.get('generation_options') or {}).get('scoring'))
    parameters = workload_parameters(data, policy) if policy is not None else None
    people, demand = ((parameters['people'], parameters['demand']) if parameters is not None
                      else workload_normalization(data, measure))
    if not (parameters['present'] if parameters is not None else len(people) > 1):
        return 0, 0
    if not demand:
        return 0, 1
    shifts = {shift['id']: shift for shift in data['shifts']}
    quantities = {}
    focus = set(data['focus_ids'])
    for key, p in participants.items():
        check()
        if (key[0] in focus and not inactive(shifts[key[0]]) and key[1] in people
                and (policy is None or goal_applies(data, policy, key[1], shifts[key[0]], aware(p['start']), aware(p['end'])))):
            quantities[key] = (1 if measure == 'duties' else
                               (aware(p['end']) - aware(p['start'])) // timedelta(microseconds=1))
    limits = parameters['limits'] if parameters is not None else dict.fromkeys(people, workload_limit(data, measure, people, demand))
    scale = lcm(Fraction(demand).denominator, *(Fraction(limit).denominator for limit in limits.values()))
    unit = reduce(gcd, [int(demand * scale), *(int(limit * scale) for limit in limits.values()),
                        *(amount * scale for amount in quantities.values())]) or 1
    demand = int(demand * scale) // unit
    limits = {pid: int(limit * scale) // unit for pid, limit in limits.items()}
    quantities = {key: amount * scale // unit for key, amount in quantities.items()}
    bounds = {pid: [0, 0] for pid in people}
    for key, amount in quantities.items():
        low, high = q.bounds(choices[key])
        bounds[key[1]][0] += min(amount * low, amount * high)
        bounds[key[1]][1] += max(amount * low, amount * high)
    if all(low == high == 0 for low, high in bounds.values()):
        return 0, 1
    n = len(people)
    spans = [demand, n * demand, *limits.values(), *quantities.values(),
             *(n * value for pair in bounds.values() for value in pair), *(n * limit for limit in limits.values()),
             sum(max(0, n * (bounds[pid][1] - limits[pid])) for pid in people)]
    if any(abs(value) >= 2**62 for value in spans):
        raise ScorePrecisionError('Score quantity exceeds supported integer precision')
    terms = defaultdict(list)
    for key, amount in quantities.items():
        check()
        terms[key[1]].append(amount * choices[key])
    loads = {pid: q.var(sum(terms[pid]), name='score_workload') for pid in people}
    variation, costs = workload_costs(
        loads, demand, limits,
        positive=lambda x: q.maximum([0, x], max(0, q.bounds(x)[1])), ratio=q.ratio)
    return q.share_loss(variation, q.maximum(costs.values())), 1


def stability_component(model, snapshot, choices, overtime, leads, *, participants=None):
    from .score import flags, publication_baseline
    check = snapshot.get('_check_budget', lambda: None)
    q = ScoreModel(model)
    published = 'score_baseline' in snapshot
    if published:
        baseline = publication_baseline(snapshot)
        if baseline is None:
            return 0, 0, None
    else:
        baseline = {(s['id'], p['person_id']): flags(p) for s in snapshot['shifts']
                    if s['id'] in snapshot['focus_ids'] and not inactive(s) for p in s['participants']}
    changed = []
    for key, (lead, ot) in baseline.items():
        check()
        choice = choices.get(key, 0)
        differences = [1 - choice]
        for before, terms, field in ((ot, overtime, 'overtime'), (lead, leads, 'team_lead')):
            if key in terms:
                differences.append(choice - terms[key] if before else terms[key])
            elif published and participants is not None and before != bool(participants.get(key, {}).get(field)):
                # Publication flags can differ even when today's flag has no decision variable.
                differences.append(choice)
        changed.append(q.any(differences))
    return q.ratio(sum(changed), len(baseline)), 1, baseline

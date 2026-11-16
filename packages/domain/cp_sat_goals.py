"""Balance goals in the solver: normalized soft terms (soft-constraints doc sections 2 and 3.1).

Per person: the deviation of (history + planned) from the aim, over the worst plausible value, as 0-1000 x strength,
plus a small worst-off (max-min) term per goal. Pairs: the deficit below the minimum per pair. One integer per
person's planned count, one for the goal's total, so the equal-share constraints stay short however many people.
Returns (terms, worst): bounds the sum and includes every applicable pair opportunity.
"""
import math
from collections import defaultdict
from fractions import Fraction
from itertools import islice

from .fairness import (COUNT_METRICS, SCALE, comparison_goal, contribution, counts, expected_share_terms, goal_applies, goal_dials,
                       goal_groups, goal_identity, goal_people, goal_period, goal_reports, goal_sources, local_day,
                       report_weights, rota_dates, scoring_settings, share_adjustment_terms, supervisable_partners, tolerance_settings, weekend_of)
from .recurrence import inactive
from .time import aware
from .conditions import conditions_match
from .cp_sat_score import SCORE_SCALE, ScoreModel, ScorePrecisionError, score_expression
from .score_normalization import round_goal_cost_ticks
from .validation import effective_type_definition


def _distribution_goals(model, data, policy, choices, participants, pairings, focus, weights, pair_budget,
                        *, score=None, presence=None):
    from .fairness_distribution import distribution_plan, pair_opportunities, PairLimitBudgetExceeded
    check = data.get('_check_budget', lambda: None)
    shifts = {s['id']: s for s in data['shifts'] if s['id'] in focus and not inactive(s)}
    paired = defaultdict(dict)
    rule = policy['definition']['rule']
    if rule['distribution'] == 'pair_limit':
        if rule.get('pairing', 'present') == 'supervised':
            for (sid, pid, partner), joint in pairings.items():
                check()
                paired[sid, pid][partner] = joint
        else:
            # shortcut: exact pair products are bounded by the configured budget; use decomposition if larger dense cohorts need one model.
            opportunities = list(islice(pair_opportunities(data, policy, choices, participants, focus), pair_budget[0] + 1))
            if len(opportunities) > pair_budget[0]:
                raise PairLimitBudgetExceeded(policy['id'], pair_budget[1])
            pair_budget[0] -= len(opportunities)
            for sid, pid, partner in opportunities:
                check()
                both = model.new_bool_var('distribution_pair')
                model.add_min_equality(both, [choices[sid, pid], choices[sid, partner]])
                if score is not None:
                    score.record(both, 'min', [choices[sid, pid], choices[sid, partner]])
                paired[sid, pid][partner] = both
    entries = [(shifts[sid], participants[sid, pid], attendance, paired[sid, pid])
               for (sid, pid), attendance in sorted(choices.items()) if sid in shifts]
    plans = distribution_plan(data, policy, entries)
    terms, worst, costs, active = [], 0, defaultdict(list), defaultdict(list)
    for plan in plans:
        check()
        coefficients = [coefficient for coefficient, _ in plan['terms']] + [plan['offset']]
        scale = math.lcm(*(coefficient.denominator for coefficient in coefficients))
        bound = sum(abs(int(coefficient * scale)) for coefficient, _ in plan['terms']) + abs(int(plan['offset'] * scale))
        if score is not None:
            _score_precision(scale, bound)
        if not bound:
            if score is not None:
                costs[plan['person_id']].append((Fraction(1), 0))
                active[plan['person_id']].extend([1] if rule['distribution'] == 'period_spread' else
                                                [x for _, x in plan['actual'] + plan['expected']])
            continue
        expression = sum(int(coefficient * scale) * x for coefficient, x in plan['terms']) + int(plan['offset'] * scale)
        deviation = model.new_int_var(0, bound, 'distribution_deviation')
        if plan['positive']:
            model.add_max_equality(deviation, [0, expression])
        else:
            model.add_abs_equality(deviation, expression)
        if score is not None:
            score.record(deviation, 'max', [0, expression] if plan['positive'] else [expression, -expression])
        denominator = max(Fraction(1), sum(abs(coefficient) for coefficient, _ in plan['denominator']))
        if score is not None:
            pid = plan['person_id']
            costs[pid].append((1 / (denominator * scale), deviation))
            active[pid].extend([1] if rule['distribution'] == 'period_spread' else
                               [x for _, x in plan['actual'] + plan['expected']])
            continue
        cap = max(1, math.ceil(denominator * scale))
        upper = math.ceil(1000 * bound / cap)
        normalized = model.new_int_var(0, upper, 'distribution_cost')
        model.add(normalized * cap >= 1000 * deviation)
        terms.append(weights[plan['person_id']] * normalized)
        worst += weights[plan['person_id']] * upper
    if score is not None and costs:
        rows = []
        for pid, items in sorted(costs.items()):
            check()
            unit = math.lcm(*(coefficient.denominator for coefficient, _ in items))
            numerator = sum(int(coefficient * unit) * deviation for coefficient, deviation in items)
            _score_precision(unit, score.bounds(numerator)[1])
            rows.append(score.ratio(numerator, unit))
        count = sum(score.any(active[pid]) for pid in sorted(costs))
        present = score.positive(count)
        if score.bounds(present)[1]:
            terms.append((next(iter(weights.values())), score.share_loss(
                score.divide(sum(rows), count), score.maximum(rows))))
            if presence is not None:
                presence.append(present)
    return terms, worst


def _pair_deficit(model, attendance, need):
    """Exact remaining meetings; size grows with opportunities, never with the numeric target."""
    deficit = model.new_int_var(0, need, 'goal_pair_deficit')
    if need == 1:
        # States: unmet, met, first person absent, first person present, final. Read each pair's two
        # attendance bits, then deficit+2 (a terminal label distinct from 0/1). Native expansion is linear
        # in opportunities but can be substantial; measured search costs are in solver-performance.md.
        model.add_automaton([v for pair in attendance for v in pair] + [deficit + 2], 0, [4],
                            [(0, 0, 2), (0, 1, 3), (2, 0, 0), (2, 1, 0), (3, 0, 0),
                             (3, 1, 1), (1, 0, 1), (1, 1, 1), (0, 3, 4), (1, 2, 4)])
        if getattr(model, '_score_program', None) is not None:
            score = ScoreModel(model)
            met = [score.temporary('min', [a, b]) for a, b in attendance]
            present = score.temporary('max', met or [0])
            score.record(deficit, 'linear', [{'offset': 1, 'terms': [[present['terms'][0][0], -1]]}])
    else:
        # Larger targets use conjunctions: a target-sized automaton would expand opportunities*need states.
        met = []
        for a, b in attendance:
            both = model.new_bool_var('goal_pair')
            model.add(both <= a)
            model.add(both <= b)
            model.add(both >= a + b - 1)
            ScoreModel(model).record(both, 'min', [a, b])
            met.append(both)
        model.add_max_equality(deficit, [0, need - sum(met)])
        ScoreModel(model).record(deficit, 'max', [0, need - sum(met)])
    return deficit


def add_balance_goals(model, data, choices, participants, overtime_choices, pairings, focus, strengths, leads=None,
                      *, score=None, presence=None):
    terms, worst = [], 0
    worst_off_emphasis = round(10 * scoring_settings(data)['fairness_worst_off_emphasis'])
    check = data.get('_check_budget', lambda: None)
    from packages.contracts.schemas import GenerationOptions
    maximum = GenerationOptions.model_fields['pair_limit_max_opportunities'].default
    maximum = data.get('generation_options', {}).get('pair_limit_max_opportunities', maximum)
    pair_budget = [maximum, maximum]
    zone = data['organisation']['timezone']
    shifts = {s['id']: s for s in sorted(data['shifts'], key=lambda s: s['id']) if s['id'] in focus and not inactive(s)}
    lead_terms = _shared_lead_terms(model, data, shifts, leads)
    on_shift = defaultdict(list)
    for sid, pid in sorted(choices):
        check()
        if sid in shifts:
            on_shift[sid].append(pid)
    groups = sorted(goal_groups(data), key=lambda group: min(goal_identity(policy) for policy in group))
    weight_scale = math.lcm(*(Fraction(str(p['definition']['rule'].get('weight', 1))).denominator
                              for policies in groups for p in policies))
    supervised = defaultdict(list)
    if any(p['definition']['rule']['metric'] == 'supervision' for group in groups for p in group):
        modeled = {(sid, pid) for sid, pid, _ in pairings}
        for (sid, trainee, supervisor), paired in sorted(pairings.items()):
            supervised[sid, supervisor].append((participants[sid, trainee], paired))
        for (sid, pid), participant in sorted(participants.items()):
            supervisor = participant.get('supervisor_person_id')
            if supervisor and (sid, pid) not in modeled and (sid, pid) in choices:
                supervised[sid, supervisor].append((participant, choices[sid, pid]))
    for policies in groups:
        check()
        policy = policies[0]
        rule = policy['definition']['rule']
        weight = strengths.get(policy['definition'].get('strength', 'medium'), 0) * Fraction(str(rule.get('weight', 1)))
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

        if rule['aim'] == 'workload_spread':
            from .cp_sat_score import workload_component
            loss, present = workload_component(model, data, choices, participants,
                                               (data.get('generation_options') or {}).get('balance_measure', 'reserved_hours'),
                                               policy=comparison)
            if present:
                weight = sum(weights.values()) / (len(weights) * strengths['medium'])
                terms.append((weight, loss) if score is not None else int(weight * 1_000_000) * loss)
                if score is not None and presence is not None:
                    presence.append(1)
            continue

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
                check()
                here = on_shift[sid]
                a, b = aware(shift['start']), aware(shift['end'])
                mates = [q for q in here if q in partners and conditions_match(data, rule['partners'], q, None, a, b)]
                for pid in here:
                    check()
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
                check()
                if score is not None:
                    row = pair_rows[key]
                    if need <= 0:
                        costs.append((1, 0))
                        continue
                    met = together.get(key, [])
                    pid, q = key
                    aim = _target(data, comparison, pid, sources)
                    prior = Fraction(str(row['prior']))
                    common = math.lcm(aim.denominator, prior.denominator)
                    _score_precision(common, int(aim * common), int(prior * common), common * len(met))
                    if rule.get('pairing', 'present') == 'present' and 0 < aim - prior <= 1:
                        attendance = []
                        for sid in met:
                            check()
                            attendance.append((choices[sid, pid], choices[sid, q]))
                        # Any meeting satisfies the remaining fractional target; retain its exact unmet cost.
                        deficit = int((aim - prior) * common) * _pair_deficit(model, attendance, 1)
                    elif rule.get('pairing', 'present') == 'present':
                        meetings = []
                        for sid in met:
                            check()
                            both = model.new_bool_var('goal_pair')
                            model.add(both <= choices[sid, pid])
                            model.add(both <= choices[sid, q])
                            model.add(both >= choices[sid, pid] + choices[sid, q] - 1)
                            score.record(both, 'min', [choices[sid, pid], choices[sid, q]])
                            meetings.append(both)
                        deficit = _positive_part(score, int((aim - prior) * common) - common * sum(meetings))
                    else:
                        deficit = _positive_part(score, int((aim - prior) * common) - common * sum(met))
                    # Keep the fractional, prorated aim: an integer deficit changes the pair score.
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
            check()
            if sid not in shifts or pid not in eligible:
                continue
            if rule['metric'] == 'supervision':
                contributions.extend((pid, Fraction(1), paired) for trainee, paired in supervised[sid, pid]
                                     if goal_applies(data, comparison, pid, shifts[sid],
                                                     aware(trainee['start']), aware(trainee['end'])))
                continue
            if not applies(sid, pid):
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
            elif rule['metric'] == 'lead_turns' and lead_terms is not None and (sid, pid) in lead_terms:
                coefficient, var = 1, lead_terms[sid, pid]
            else:
                coefficient, var = contribution(rule['metric'], shifts[sid], p), x
            if coefficient:
                contributions.append((pid, _quantity(coefficient), var))
        if score is not None:
            scale = math.lcm(1, *(value.denominator for _, value, _ in contributions),
                             *(value.denominator for value in raw_prior.values()))
            _score_precision(scale)
        for pid, value, var in contributions:
            check()
            coefficient = round(value * scale)
            planned.setdefault(pid, []).append(coefficient * var)
            bound[pid] = bound.get(pid, 0) + coefficient
        for pid, groups in sorted(weekends.items()):
            check()
            flags = []
            for _, xs in sorted(groups.items()):
                flag = xs[0] if len(xs) == 1 else model.new_bool_var('goal_weekend')
                if len(xs) > 1:
                    model.add_max_equality(flag, xs)
                    if score is not None:
                        score.record(flag, 'max', xs)
                flags.append(flag)
            planned[pid], bound[pid] = [scale * flag for flag in flags], scale * len(flags)
        prior = {pid: round((raw_prior[pid] if score is not None else rows[pid]['prior']) * scale) for pid in who}
        if score is not None:
            _score_precision(sum(bound.values()) + sum(prior.values()))
        counted = {}
        for pid in who:
            check()
            counted[pid] = model.new_int_var(0, bound.get(pid, 0), 'goal_planned')
            model.add(counted[pid] == sum(planned.get(pid, [])))
            if score is not None:
                score.record(counted[pid], 'linear', [sum(planned.get(pid, []))])
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
            dials = {pid: goal_dials(data, comparison, pid) for pid in who}
            if offered:
                dials = {pid: dial if avail[pid] else {**dial, 'share_adjustment': {'mode': 'percent', 'value': 100}}
                         for pid, dial in dials.items()}
            if rule.get('share_by') == 'supervisable_sessions' and rule['aim'] != 'target':
                # Candidate attendance is symbolic. Independent overlaps are already prohibited by
                # the clinical model; treating candidate rows as commitments would exclude alternatives.
                context = {**data, '_supervision_commitments': {}}
                opportunities = {pid: [] for pid in who}
                for sid, pids in on_shift.items():
                    candidates = [participants[sid, pid] for pid in pids]
                    for pid in eligible.intersection(pids):
                        trainee_choices = [choices[sid, trainee] for trainee in supervisable_partners(
                            context, comparison, shifts[sid], participants[sid, pid], candidates)]
                        if trainee_choices:
                            opportunities[pid].append(score.minimum([choices[sid, pid], score.any(trainee_choices)], 1))
                basis = {pid: score.var(sum(opportunities[pid]) + int(history.get(pid, {}).get('supervisable_sessions', 0)))
                         for pid in who}
                aims = _dynamic_share_aims(score, total, scale, basis, dials)
            else:
                aims = _share_aims(score, total, scale, avail, dials) if rule['aim'] != 'target' else {}
            costs = []
            for pid in who:
                check()
                if offered and not avail[pid]:
                    costs.append((weights[pid], 0))
                    continue
                if rule['aim'] == 'target':
                    # Grouped reports already sum the canonically rounded source targets.
                    expected = (Fraction(str(rows[pid]['expected'])) if comparison.get('_balance_sources')
                                else _target(data, comparison, pid, sources))
                    aim, divisor = expected.numerator * scale, expected.denominator
                else:
                    aim, divisor = aims[pid]
                percent, units = tolerance_settings(dials[pid])
                zero_count_band = rule['metric'] in COUNT_METRICS and percent == 0 and units == 0
                difference = _multiply(score, actual[pid], divisor) - aim
                if zero_count_band:
                    # Count totals are integral even when the pro-rata aim is fractional:
                    # floor(aim) and ceil(aim) are the attainable zero-tolerance band.
                    scale_divisor = _multiply(score, divisor, scale)
                    aim_high = score.bounds(aim)[1]
                    lower = score.divide(aim, scale_divisor, max(0, aim_high)) * scale
                    upper = score.divide(aim + scale_divisor - 1, scale_divisor,
                                         max(0, aim_high)) * scale
                    distances = [0, lower - actual[pid]]
                    if rule['aim'] != 'max_min':
                        distances.append(actual[pid] - upper)
                    gap = score.maximum(distances, max(score.bounds(x)[1] for x in distances))
                    gap = _multiply(score, divisor, gap)
                else:
                    gap = _positive_part(score, -difference) if rule['aim'] == 'max_min' else score.maximum(
                        [difference, -difference], max(abs(value) for value in score.bounds(difference)))
                percent, units = Fraction(str(percent)) / 100, Fraction(str(units))
                parts = math.lcm(percent.denominator, units.denominator)
                tolerances = [_multiply(score, int(units * parts) * scale, divisor),
                              _multiply(score, int(percent * parts), aim)]
                tolerance = score.maximum(tolerances, max(score.bounds(value)[1] for value in tolerances))
                outside = _positive_part(score, _multiply(score, parts, gap) - tolerance)
                denominators = [_multiply(score, parts * scale, divisor), _multiply(score, parts, aim)]
                denominator = score.maximum(denominators, max(score.bounds(value)[1] for value in denominators))
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
                check()
                aim = round(rows[pid]['expected'] * scale)
                deviation = model.new_int_var(0, max(aim, prior[pid] + bound.get(pid, 0)), 'goal_deviation')
                model.add(deviation >= prior[pid] + counted[pid] - aim)
                model.add(deviation >= aim - prior[pid] - counted[pid])
                cap = max(scale, aim)
                cost_bound = -(-1000 * max(aim, prior[pid] + bound.get(pid, 0)) // cap)
                scaled = model.new_int_var(0, cost_bound, 'goal_cost')
                model.add(scaled * cap >= 1000 * deviation)
                terms.append(worst_off_emphasis * weights[pid] * scaled)
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
            per_seat = {'overtime_duties': 'duties', 'overtime_hours': 'hours', 'lead_turns': 'duties',
                        'supervision': 'duties'}.get(rule['metric'], rule['metric'])
            demand = sum(round(contribution(per_seat, s, s) * scale)
                         * max([1] + [r['minimum'] for r in effective_type_definition(data, s).get('requirements', [])])
                         for sid, s in shifts.items() if any(pid in eligible and applies(sid, pid)
                                                             for pid in on_shift[sid])) if rule['metric'] != 'weekends' else n * 2
            for pid in who:
                check()
                if offered and not avail[pid]:
                    continue  # No offer means no share and no fairness penalty, including historical work.
                gap = (prior[pid] + counted[pid]) * days - avail[pid] * (everyone + total)
                high = (prior[pid] + bound.get(pid, 0)) * days + avail[pid] * (everyone + top)
                deviation = model.new_int_var(0, high, 'goal_deviation')
                # The legacy proxy path has no symbolic floor/ceil operation; production
                # uses add_score_goals below, whose ScoreModel path mirrors the report band.
                model.add(deviation >= -gap)
                if rule['aim'] != 'max_min':
                    model.add(deviation >= gap)
                cap = max(scale * days, avail[pid] * (everyone + demand))
                cost_bound = -(-1000 * high // cap)
                scaled = model.new_int_var(0, cost_bound, 'goal_cost')
                model.add(scaled * cap >= 1000 * deviation)
                if rule['aim'] != 'max_min':
                    terms.append(worst_off_emphasis * weights[pid] * scaled)
                weighted_costs.append(weights[pid] * scaled)
                worst_bound = max(worst_bound, weights[pid] * cost_bound)
                total_cost_bound += weights[pid] * cost_bound
        # The worst-off person (normalized as each person is): a small extra term for shares, the whole term for max-min.
        worst_off = model.new_int_var(0, worst_bound, 'goal_worst_off')
        for cost in weighted_costs:
            model.add(worst_off >= cost)
        share = n * worst_off_emphasis if rule['aim'] == 'max_min' else 1
        terms.append(share * worst_off)
        worst += share * worst_bound + (0 if rule['aim'] == 'max_min' else worst_off_emphasis * total_cost_bound)
    return terms, worst


def _target(data, comparison, pid, sources):
    """An ungrouped report's unrounded target, including its period and proration."""
    _, last = rota_dates(data)
    result = Fraction(0)
    for source in goal_sources(comparison, pid):
        data.get('_check_budget', lambda: None)()
        if pid not in sources[goal_identity(source)]['who']:
            continue
        rule = source['definition']['rule']
        start, end = goal_period(data, source, pid)
        amount = Fraction(str(rule.get('person_amounts', {}).get(pid, rule.get('amount') or 0)))
        value = amount * Fraction(max(0, (min(last, end) - start).days), max(1, (end - start).days))
        result += value
    return result


def _report_cost(score, numerator, denominator):
    """Goal rows round their normalized cost to four places before share_value combines them."""
    raw = score.ratio(numerator, denominator)
    cost = round_goal_cost_ticks(raw, divide=lambda n, d: score.divide(n, d, 10_000))
    program = getattr(score.model, '_score_program', None)
    if program is not None:
        # Preserve the fraction discarded before canonical goal_cost suppresses floating noise.
        program.setdefault('goal_cost_checks', []).append(
            [score_expression(value) for value in (numerator, denominator, cost)])
    return cost


def _positive_part(score, expression):
    return score.maximum([0, expression], max(0, score.bounds(expression)[1]))


def _multiply(score, left, right, *, offset=0):
    """An exact bounded product; amount dials also need their offset checked before native construction."""
    bounds = [a * b for a in score.bounds(left) for b in score.bounds(right)]
    if max(abs(value) for value in [*bounds, offset, *(value + offset for value in bounds)]) >= 2**62:
        raise ScorePrecisionError('Fair-share quantities exceed supported integer precision; simplify their units or targets.')
    if min(bounds) == max(bounds):
        return bounds[0] + offset
    if isinstance(left, int) or isinstance(right, int):
        return left * right + offset
    left, right = score.var(left), score.var(right)
    product = score.model.new_int_var(min(bounds), max(bounds), 'goal_product')
    score.model.add_multiplication_equality(product, [left, right])
    score.record(product, 'mul', [left, right])
    return product + offset


def _share_aims(score, total, scale, avail, dials):
    """Exact scaled-share numerators/divisors, including clamped and re-normalized amount dials."""
    adjustments = {pid: dial.get('share_adjustment') or {'mode': 'percent', 'value': 100}
                   for pid, dial in dials.items()}
    shares, amounts = expected_share_terms(avail, adjustments)
    if not any(amounts.values()):
        return {pid: (_multiply(score, share.numerator, total), share.denominator) for pid, share in shares.items()}
    unit = math.lcm(*(share.denominator for share in shares.values()),
                    *((amount * scale).denominator for amount in amounts.values()))
    raw = {pid: _positive_part(score, _multiply(score, int(share * unit), total,
                                              offset=int(amounts[pid] * scale * unit)))
           for pid, share in shares.items()}
    denominator = score.var(sum(raw.values()))
    fallback = 1 - score.positive(denominator)
    raw = {pid: value + _multiply(score, int(shares[pid] * unit), fallback) for pid, value in raw.items()}
    denominator = score.var(denominator + _multiply(score, unit, fallback))
    return {pid: (_multiply(score, total, value), denominator) for pid, value in raw.items()}


def _dynamic_share_aims(score, total, scale, basis, dials):
    """Symbolic form of expected_share_terms/expected_shares, with the same zero-basis fallbacks."""
    if all(score.bounds(value)[0] == score.bounds(value)[1] for value in basis.values()):
        return _share_aims(score, total, scale, {pid: Fraction(score.bounds(value)[0])
                                               for pid, value in basis.items()}, dials)
    empty = 1 - score.positive(sum(basis.values()))
    basis = {pid: score.var(value + empty) for pid, value in basis.items()}
    # Unit basis extracts only the canonical adjustment factors/amounts; normalization is symbolic.
    adjustments = {pid: dial.get('share_adjustment') for pid, dial in dials.items()}
    factors = {pid: share_adjustment_terms(dial)[0] for pid, dial in adjustments.items()}
    amounts = {pid: share_adjustment_terms(dial)[1] for pid, dial in adjustments.items()}
    unit = math.lcm(*(factor.denominator for factor in factors.values()))
    weighted = {pid: _multiply(score, int(factors[pid] * unit), value) for pid, value in basis.items()}
    empty = 1 - score.positive(sum(weighted.values()))
    weighted = {pid: score.var(value + _multiply(score, unit, _multiply(score, basis[pid], empty)))
                for pid, value in weighted.items()}
    denominator = score.var(sum(weighted.values()))
    if not any(amounts.values()):
        return {pid: (_multiply(score, total, value), denominator) for pid, value in weighted.items()}
    unit = math.lcm(*((amount * scale).denominator for amount in amounts.values()))
    raw = {pid: _positive_part(score, _multiply(score, unit, _multiply(score, total, value)) +
                             _multiply(score, int(amounts[pid] * scale * unit), denominator))
           for pid, value in weighted.items()}
    empty = 1 - score.positive(sum(raw.values()))
    raw = {pid: score.var(value + _multiply(score, weighted[pid], empty)) for pid, value in raw.items()}
    denominator = score.var(sum(raw.values()))
    return {pid: (_multiply(score, total, value), denominator) for pid, value in raw.items()}


def _score_precision(*values):
    if any(abs(value) * SCORE_SCALE >= 2**62 for value in values):
        raise ScorePrecisionError('Fair-share quantities exceed supported integer precision; simplify their units or targets.')


def _quantity(value):
    # Recover rational time/unit quantities without preserving binary floating-point noise.
    return Fraction(str(value)).limit_denominator(1_000_000)


def _shared_lead_terms(model, data, shifts, leads):
    """Collapse duplicate lead variables for one concurrent-duty obligation."""
    if not leads:
        return leads
    from . import leadership
    terms = dict(leads)
    lead_by_shift = defaultdict(list)
    for (sid, pid), term in leads.items():
        if not (isinstance(term, int) and term == 0):
            lead_by_shift[sid].append((pid, term))
    grouped = defaultdict(dict)
    parent = {}

    def find(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    seen = set()
    for shift in shifts.values():
        for group in leadership.concurrent_groups(data, shift, aware(shift['start']), aware(shift['end'])):
            marker = (group['key'], group['start'], group['end'])
            if marker in seen:
                continue
            seen.add(marker)
            group_entries = defaultdict(list)
            for duty, requirement in group['duties']:
                sid = duty['id']
                if sid not in shifts:
                    continue
                for pid, term in lead_by_shift.get(sid, ()):
                    if leadership.matches(data, pid, duty, requirement, group['start'], group['end']):
                        key = (sid, pid)
                        grouped[pid][key] = term
                        group_entries[pid].append(key)
            for entries in group_entries.values():
                keys = sorted(entries)
                if len(keys) > 1:
                    first = keys[0]
                    for key in keys[1:]:
                        union(first, key)
    components = defaultdict(list)
    for pid, entries in grouped.items():
        for key, term in entries.items():
            components[pid, find(key)].append((key, term))
    for entries in components.values():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda item: item[0])
        canonical, variables = entries[0][0], [term for _, term in entries]
        shared = model.new_bool_var('goal_shared_lead')
        model.add_max_equality(shared, variables)
        terms[canonical] = shared
        for key, _ in entries[1:]:
            terms[key] = 0
    return terms


def add_score_goals(model, data, choices, participants, overtime_choices, pairings, focus, strengths, leads=None,
                    *, presence=None):
    """Fair share and lead goals as the same weighted, normalized losses the schedule score reads."""
    from .cp_sat_score import ScoreModel
    terms, _ = add_balance_goals(model, data, choices, participants, overtime_choices, pairings, focus, strengths,
                                 leads, score=ScoreModel(model, (data.get('generation_options') or {}).get('scoring')),
                                 presence=presence)
    return terms

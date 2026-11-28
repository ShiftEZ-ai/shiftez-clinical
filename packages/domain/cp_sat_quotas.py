"""Translate canonical quota rows and assignment contributions to integer bounds."""
import math
from collections import defaultdict
from datetime import timedelta
from fractions import Fraction

from packages.contracts.schemas import DEFAULT_QUOTA_PRORATION_BASIS

from .quotas import applicable_policies, evaluate_quotas, membership_overlaps, period_bounds, quota_contribution, quota_identity, quota_window_amount
from .fairness import quota_target_key
from .time import aware, overlaps
from .validation import ancestor_ids, category_scope_ids, exception_for
from .solver_provenance import policy_name
from .generation_scope import selected_session_ids


def add_quotas(model, data, by_person, penalise, *, diagnostic_proposal=False, leads=None, score_items=None, score_strengths=None, target_group=None,
               strict_minima=None, diagnostic_domains=None, staffing_targets=None, staffing_demand=None,
               structural_shortfalls=None):
    # Targets, rotation proration, overrides, subjects and context checks stay in
    # the reporting evaluator, including the canonical planning-population audience.
    # Empty work reveals targets without trial schedules.
    empty = {**data, '_quota_evaluation_ids': True, 'shifts': [{**s, 'participants': []} for s in data['shifts']]}
    findings = []
    def record(code, message, **detail):
        if code == 'TARGET_OVERRIDE_STALE':
            detail = {'rule_id': detail['rule'], 'person_id': detail['person'], 'detail': detail['extra']}
        findings.append({'code': code, 'message': message, **detail})
    report = evaluate_quotas(empty, record, ancestor_ids, exception_for)
    if report is None:
        return []
    if not report['complete']:
        if staffing_demand is not None:
            staffing_demand.update(data.get('_generation_editable', selected_session_ids(data)))
        return [f for f in findings if f['code'] not in {'QUOTA_BELOW_MINIMUM', 'QUOTA_BELOW_TARGET'}]
    policies = {}
    for policy in applicable_policies(data):
        rule, line = policy['definition']['rule'], policy.get('template_line') or {}
        policies[quota_identity({'policy_id': policy['id'], 'starts_on': rule['starts_on'], 'ends_on': rule['ends_on'],
            'line_id': line.get('id'), 'line': line.get('line'), 'slot': line.get('slot'), 'comparison': rule['comparison'],
            'severity': policy['definition']['severity'], 'strength': policy['definition'].get('strength', 'medium'),
            **({'count_bucket': policy['count_bucket']} if 'count_bucket' in policy else {})})] = policy
    issues, targets = [], defaultdict(list)
    selected = set(selected_session_ids(data))
    editable = set(data.get('_generation_editable', selected))
    selected_intervals = [(aware(s['start']), aware(s['end'])) for s in data['shifts'] if s['id'] in selected]
    if score_items is not None:
        from packages.contracts.schemas import StrengthMultipliers
        from .cp_sat_score import ScoreModel
        scoring = ScoreModel(model)
        strengths = data.get('_allocation_strengths') or StrengthMultipliers().model_dump()
    for row in report['rows']:
        data.get('_check_budget', lambda: None)()
        policy = policies[quota_identity(row)]
        rule, scope = policy['definition']['rule'], policy['definition']['scope']
        pid = row['person_id']
        scored = score_items is not None and (row['severity'] == 'advisory' or row['comparison'] == 'target')
        if scored:
            score_strength = row.get('strength', policy['definition'].get('strength', 'medium'))
            score_weight = strengths[score_strength]
        a, b = period_bounds(policy, data['organisation']['timezone'])
        if exception_for(data, policy, pid, a, b):
            if scored:
                score_items.append((score_weight, 0, 1))
                if score_strengths is not None:
                    score_strengths.append(score_strength)
            continue
        members = [m for m in data['memberships'] if m['department_id'] == data['workspace']['department_id']
            and membership_overlaps(m, row['starts_on'], row['ends_on'])
            and (not category_scope_ids(scope) or m['category_id'] in category_scope_ids(scope))]
        terms, intervals, priority_terms, session_ids = [], [], [], set()
        context_terms = []
        context_hours = []
        check_shortfall = (structural_shortfalls is not None and row['comparison'] == 'minimum'
                           and row['severity'] == 'mandatory'
                           and rule.get('proration_basis', DEFAULT_QUOTA_PRORATION_BASIS) != DEFAULT_QUOTA_PRORATION_BASIS
                           and any(overlaps(a, b, lo, hi) for lo, hi in selected_intervals))
        potential_by_session = {}
        potential_hours = Fraction(0)
        target = quota_window_amount(policy, row['amount'])
        needs_staff = staffing_demand is not None and row['comparison'] in {'minimum', 'target'} and target > 0
        for shift, participant, _, _, x in by_person.get(pid, []):
            if data['types'][shift['assignment_type_id']]['department_id'] != data['workspace']['department_id']:
                continue
            if rule['metric'] == 'lead_turns':
                # Leading is its own choice: count this form of the duty only when the person also leads it.
                lead = (leads or {}).get((shift['id'], pid), int(bool(participant.get('team_lead'))))
                if isinstance(lead, int):
                    x = x if lead else 0
                elif lead is not x:
                    both = model.new_bool_var('lead_turn')
                    model.add_min_equality(both, [x, lead])
                    x = both
                participant = {**participant, 'team_lead': True}
            try:
                contribution = quota_contribution(data, policy, pid, shift, participant, members, a, b)
            except ValueError:
                issues.append({'code': 'QUOTA_PRESENCE_INVALID', 'rule_id': policy['id'], 'person_id': pid})
                continue
            if contribution is None:
                continue
            session_ids.add(shift['id'])
            definition, metric = shift['type_snapshot']['definition'], row['metric']
            can_add_credit = needs_staff and not isinstance(x, int) and x.domain.max() > x.domain.min()
            if metric in {'reserved_hours', 'scheduled_hours'}:
                intervals.extend((lo, hi, x, (shift['id'], pid)) for lo, hi in contribution['intervals'])
                if can_add_credit and contribution['intervals']:
                    staffing_demand.add(shift['id'])
                if shift['id'] not in editable and hasattr(x, 'domain') and x.domain.min() == x.domain.max() == 1:
                    context_hours.extend((lo, hi) for lo, hi in contribution['intervals'])
                continue
            if metric in {'duties', 'lead_turns'}:
                weight = Fraction(int(contribution['duty']))
            elif metric == 'session_units':
                weight = Fraction(str(definition.get('session_units', 1))) * int(contribution['duty'])
                if definition.get('session_unit_basis', 'fixed') == 'proportional':
                    presence = sum((hi - lo) // timedelta(microseconds=1) for lo, hi in contribution['presence'])
                    span = (aware(shift['end']) - aware(shift['start'])) // timedelta(microseconds=1)
                    weight = Fraction(str(definition.get('session_units', 1))) * Fraction(presence, span)
            else:
                presence = sum((hi - lo) // timedelta(microseconds=1) for lo, hi in contribution['presence'])
                weight = Fraction(presence, 3_600_000_000) * Fraction(str(definition[metric + '_per_hour']))
            if weight:
                terms.append((weight, x))
                if check_shortfall and (x > 0 if isinstance(x, int) else x.domain.max() > 0):
                    potential_by_session[shift['id']] = max(potential_by_session.get(shift['id'], 0), weight)
                if can_add_credit:
                    staffing_demand.add(shift['id'])
                if shift['id'] not in editable and hasattr(x, 'domain') and x.domain.min() == x.domain.max() == 1:
                    context_terms.append(weight)
                if target_group:
                    priority_terms.append((weight, x, [((shift['id'], pid), x)]))
        # Hours are a union, including overlapping protected historical duties.
        boundaries = sorted({t for lo, hi, _, _ in intervals for t in (lo, hi)})
        for lo, hi in zip(boundaries, boundaries[1:]):
            covering = [(x, key) for c, d, x, key in intervals if c <= lo and hi <= d]
            if covering:
                present = model.new_bool_var('quota_time_present')
                model.add_max_equality(present, [x for x, _ in covering])
                weight = Fraction((hi - lo) // timedelta(microseconds=1), 3_600_000_000)
                terms.append((weight, present))
                if check_shortfall and any(x > 0 if isinstance(x, int) else x.domain.max() > 0 for x, _ in covering):
                    potential_hours += weight
                if any(key[0] not in editable and hasattr(x, 'domain') and x.domain.min() == x.domain.max() == 1
                       for x, key in covering):
                    context_hours.append((lo, hi))
                if target_group:
                    priority_terms.append((weight, present, [(key, x) for x, key in covering]))
        scale = math.lcm(target.denominator, *(weight.denominator for weight, _ in terms))
        maximum = sum(int(weight * scale) for weight, _ in terms)
        # Explicit numerical bounds, never silently round unsupported precision.
        if scale > 1_000_000_000 or max(maximum, int(target * scale)) > 2**60:
            issues.append({'code': 'QUOTA_PRECISION_UNSUPPORTED', 'rule_id': policy['id'],
                'field': 'definition.rule.amount', 'message': 'Use simpler quota units or credit precision.'})
            continue
        actual = sum(int(weight * scale) * x for weight, x in terms)
        context_actual = sum(int(weight * scale) for weight in context_terms)
        if context_hours:
            context_actual += sum(
                ((hi - lo) // timedelta(microseconds=1)) * scale // 3_600_000_000
                for lo, hi in context_hours)
        if row['comparison'] == 'target' and score_items is None:
            targets[quota_target_key(row)].append((row, policy, actual, scale, maximum, int(target * scale)))
        tolerance = Fraction(1, 1_000_000_000)
        lower, upper = math.ceil((target - tolerance) * scale), math.floor((target + tolerance) * scale)
        if check_shortfall:
            potential = sum(potential_by_session.values(), potential_hours)
            if potential * scale < lower:
                structural_shortfalls.append({'code': 'QUOTA_STRUCTURAL_SHORTFALL', 'rule_id': policy['id'], 'person_id': pid,
                    'message': f"{policy['name']} needs {float(target):g}; known assignments can supply at most {float(potential):g}.",
                    'required': float(target), 'available_upper_bound': float(potential),
                    'proven': True, 'proof_kind': 'necessary_condition'})
        if target_group and row['comparison'] in {'minimum', 'target'}:
            target_group([(int(weight * scale), x, sources) for weight, x, sources in priority_terms], lower,
                         stage='mandatory_quota' if row['severity'] == 'mandatory' and row['comparison'] == 'minimum' else 'desired')
        diagnostic_minimum = row['severity'] == 'mandatory' and row['comparison'] == 'minimum' and diagnostic_domains is not None
        hard = row['severity'] == 'mandatory' and row['comparison'] != 'target' and not (
            (diagnostic_proposal or diagnostic_minimum) and row['comparison'] == 'minimum')
        if hard:
            # Existing fixed context may already exceed a maximum.  Preserve
            # that fact for reporting, while keeping the maximum binding for
            # any new contribution from the editable sessions.
            bound = max(upper, context_actual) if row['comparison'] == 'maximum' else upper
            constraint = model.add(actual >= lower if row['comparison'] == 'minimum' else actual <= bound).with_name(
                policy_name(policy, data=data, row=row, person_ids=[pid], session_ids=session_ids))
            # An unrelated kept conflict must not turn an impossible selected-window minimum into a proposal.
            if strict_minima is not None and row['comparison'] == 'minimum' and any(
                    overlaps(a, b, lo, hi) for lo, hi in selected_intervals):
                strict_minima.add(constraint.index)
        elif not scored:
            deviation = model.new_int_var(0, max(maximum, int(target * scale)) + 1, 'quota_preference')
            if diagnostic_minimum:
                diagnostic_domains[deviation.index] = max(maximum, int(target * scale)) + 1
            if row['comparison'] in {'minimum', 'target'}:
                constraint = model.add(actual + deviation >= lower).with_name(
                    policy_name(policy, data=data, row=row, person_ids=[pid], session_ids=session_ids))
                if diagnostic_minimum and strict_minima is not None and any(
                        overlaps(a, b, lo, hi) for lo, hi in selected_intervals):
                    strict_minima.add(constraint.index)
            if row['comparison'] in {'maximum', 'target'}:
                model.add(actual - deviation <= upper)
            # Missing the amount by the amount itself (at least one unit: a duty, an hour) is fully broken,
            # whatever the unit, so an hour off a quota no longer outweighs an unfilled seat.
            penalise(deviation, policy, max(int(target * scale), scale), max(maximum, int(target * scale)) + 1)
        if scored:
            # Cancel exact units before fixed-point scoring, keeping the original hard quota bounds untouched.
            denominator = max(int(target * scale), scale)
            divisor = math.gcd(denominator, int(target * scale), *(int(weight * scale) for weight, _ in terms))
            quantity = sum((int(weight * scale) // divisor) * x for weight, x in terms)
            desired, bound = int(target * scale) // divisor, max(maximum, int(target * scale)) // divisor
            deviation = model.new_int_var(0, bound, 'quota_score_deviation')
            if row['comparison'] == 'target':
                model.add_abs_equality(deviation, quantity - desired)
            else:
                difference = desired - quantity if row['comparison'] == 'minimum' else quantity - desired
                model.add_max_equality(deviation, [0, difference])
            denominator //= divisor
            clipped = scoring.minimum([deviation, denominator], denominator)
            loss = scoring.ratio(clipped, denominator)
            score_items.append((score_weight, loss, 1))
            if staffing_targets is not None and row['comparison'] in {'minimum', 'target'}:
                staffing_targets.append(score_weight * loss)
            if score_strengths is not None:
                score_strengths.append(score_strength)
    for rows in targets.values():
        if len(rows) < 2:
            continue
        scale = math.lcm(*(row[3] for row in rows))
        from packages.contracts.schemas import StrengthMultipliers
        strengths = data.get('_allocation_strengths') or StrengthMultipliers().model_dump()
        weights = defaultdict(int)
        for row, *_ in rows:
            weights[row['person_id']] += strengths[row.get('strength', 'medium')]
        maximum = sum(bound * (scale // unit) * weights[row['person_id']]
                      for row, _, _, unit, bound, _ in rows)
        # Share preferences have the same 1/1000 resolution as other soft rules. Usually the exact quota integers
        # can be used directly. Extreme, individually supported denominators use bounded measurement ticks for this
        # added preference only; the original quota bounds and deviations above remain exact.
        exact = maximum <= 2**50
        if not exact:
            precision = min(1000, max(1, min(2**60 // max(1, row[4]) for row in rows)))
            span = sum(Fraction(bound, unit) * weights[row['person_id']]
                       for row, _, _, unit, bound, _ in rows)
            divisor = max(1, math.ceil(span * precision / 2**50))
        people = {}
        for row, policy, actual, unit, bound, target in rows:
            person = people.setdefault(row['person_id'], {'actual': 0, 'target': 0, 'bound': 0, 'weight': 0, 'policy': policy})
            # Each probability normalizes its own total, so target amounts stay exact even if actual quantities
            # require bounded measurement ticks. Tiny supported targets must never disappear from the audience.
            person['target'] += Fraction(target, unit)
            if exact:
                actual, bound = actual * (scale // unit), bound * (scale // unit)
            else:
                ratio = Fraction(precision, unit * divisor)
                bound = bound * ratio.numerator // ratio.denominator
                rounded = model.new_int_var(0, bound, 'quota_share_quantity') if bound else 0
                if bound:
                    model.add_division_equality(rounded, actual * ratio.numerator, ratio.denominator)
                actual = rounded
            person['actual'] += actual
            person['bound'] += bound
            person['weight'] += strengths[row.get('strength', 'medium')]
        expected = sum(person['target'] * person['weight'] for person in people.values())
        maximum = sum(person['bound'] * person['weight'] for person in people.values())
        if not expected:
            continue
        total = sum(person['actual'] * person['weight'] for person in people.values())
        nonzero_total = model.new_int_var(1, max(1, maximum), 'quota_share_total')
        model.add_max_equality(nonzero_total, [1, total])
        for person in people.values():
            amount, value = person['target'], person['actual']
            # Put strength inside the probability before rounding:150 people with critical lines still have
            # meaningful shares. The two divisions introduce less than .002 error per weighted probability.
            actual_share = model.new_int_var(0, 1000, 'quota_actual_share')
            model.add_division_equality(actual_share, 1000 * person['weight'] * value, nonzero_total)
            share = model.new_int_var(0, 1000, 'quota_share')
            model.add_abs_equality(share, actual_share - 1000 * person['weight'] * amount // expected)
            penalise(share, {**person['policy'], '_fairness_weight': 1}, 1000, 1000)
    return issues

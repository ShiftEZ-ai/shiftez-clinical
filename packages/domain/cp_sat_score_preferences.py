"""The score's per-person preferences, compiled with the same applicable subjects and losses."""
from .recurrence import inactive
from collections import defaultdict
from fractions import Fraction
from functools import reduce
from math import gcd

from .conditions import counting_views
from .allocation_score import allocation_score_items
from .cp_sat_patterns import add_patterns
from .cp_sat_score import SCORE_SCALE, ScoreModel, ScorePrecisionError, fractional_scale
from .patterns import SHAPE_KINDS, day_of, wish_days, wish_shape
from .score import rule_copy_key
from .time import aware
from .validation import policy_applies


def add_score_preferences(model, data, choices, participants, pairings, overtime_choices, by_person, strengths):
    check = data.get('_check_budget', lambda: None)
    q = ScoreModel(model, (data.get('generation_options') or {}).get('scoring'))
    focus = set(data['focus_ids'])
    shifts = {s['id']: s for s in data['shifts'] if not inactive(s)}
    counted = counting_views(data, data['policies'])
    shape_policies = [policy for policy in counted
                      if policy['definition']['rule']['kind'] in SHAPE_KINDS and policy['definition']['severity'] == 'advisory']
    focused = defaultdict(list)
    if shape_policies:
        for (sid, pid), choice in sorted(choices.items()):
            check()
            if sid in focus:
                focused[pid].append((shifts[sid], participants[sid, pid], choice))
    own = defaultdict(list)  # person -> (strength, loss ticks, applicable)
    for item in allocation_score_items(data, choices, participants, pairings, overtime_choices, math=q, strengths=strengths):
        own[item['person_id']].append((item['weight'], item['loss'], item['active']))

    # Wishes count all known work on the wished day, including protected history outside the focus.
    zone = data['organisation']['timezone']
    days = defaultdict(lambda: defaultdict(list))
    typed = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    wishes = [wish for wish in data.get('wishes') or [] if wish.get('weight')]
    wish_people = {wish['person_id'] for wish in wishes}
    for pid, entries in by_person.items():
        check()
        if pid not in wish_people:
            continue
        for shift, _, start, _, choice in entries:
            check()
            day = day_of(start, zone)
            days[pid][day].append(choice)
            typed[pid][shift['assignment_type_id']][day].append(choice)
    worked, on_type = {}, {}
    pattern_wishes = [wish for wish in wishes if wish_shape(wish) is not None]
    wish_breaches = {}
    if pattern_wishes:
        add_patterns(model, {**data, 'policies': [], 'wishes': pattern_wishes}, by_person, strengths,
                     check=check, wish_breaches=wish_breaches)
    for wish in wishes:
        check()
        period = wish_days(wish, data)
        if not period or not wish.get('weight'):
            continue
        pid, kind, missed = wish['person_id'], wish['kind'], []
        weight = strengths['medium'] * Fraction(str(wish['weight']))
        if wish['id'] in wish_breaches:
            own[pid].append((weight, SCORE_SCALE * wish_breaches[wish['id']], 1))
            continue
        for day in period:
            check()
            if (pid, day) not in worked:
                worked[pid, day] = q.any(days[pid].get(day, []))
            on = worked[pid, day]
            duty_type = wish.get('assignment_type_id')
            if (pid, duty_type, day) not in on_type:
                on_type[pid, duty_type, day] = q.any(typed[pid][duty_type].get(day, []))
            duty = on_type[pid, duty_type, day]
            missed.append({'prefer_off': on, 'prefer_on': 1 - on,
                           'prefer_shift_type': on - duty, 'avoid_shift_type': duty}[kind])
        own[pid].append((weight, q.ratio(sum(missed), len(period)), 1))

    breached = {}
    if shape_policies:
        add_patterns(model, {**data, 'policies': counted, 'wishes': []}, by_person, strengths,
                     check=check, shape_breaches=breached)
    by_copy = defaultdict(dict)
    for (key, pid), flags in breached.items():
        by_copy[key][pid] = flags
    shapes, presence_by_choices = {}, {}
    for policy in shape_policies:
        check()
        key = rule_copy_key(policy)
        weight = strengths[policy['definition'].get('strength', 'medium')]
        for pid, rows in focused.items():
            check()
            applicable = [choice for shift, participant, choice in rows
                          if policy_applies(data, policy, pid, shift, aware(participant['start']), aware(participant['end']))]
            if applicable:
                choices_key = tuple(choice.index for choice in applicable)
                if choices_key not in presence_by_choices:
                    presence_by_choices[choices_key] = q.any(applicable)
                shapes[key, pid] = [weight, presence_by_choices[choices_key]]
        for pid in by_copy.get(key, {}):
            shapes.setdefault((key, pid), [weight, 0])
    for (key, pid), (weight, applicable) in shapes.items():
        broken = q.any(breached.get((key, pid), []))
        own[pid].append((weight, SCORE_SCALE * broken, q.any([applicable, broken])))

    if not own:
        return 0, 0
    losses, present = [], []
    for pid, items in sorted(own.items()):
        check()
        # Each person's common budget unit cancels; retain their relative wish and rule weights.
        unit = min((Fraction(weight) for weight, _, _ in items if weight), default=Fraction(1))
        weights = fractional_scale([float(Fraction(weight) / unit) for weight, _, _ in items], relative_unit=SCORE_SCALE)
        if weights is None:
            raise ScorePrecisionError('Score quantity exceeds supported integer precision')
        divisor = reduce(gcd, weights) or 1
        weights = [weight // divisor for weight in weights]
        # Check Python integers before native affine multiplication can overflow and hide an unsafe bound.
        for column in (1, 2):
            if sum(weight * max(1, *(abs(bound) for bound in q.bounds(item[column])))
                   for weight, item in zip(weights, items)) >= 2**62:
                raise ScorePrecisionError('Score quantity exceeds supported integer precision')
        denominator = q.var(sum(weight * active for weight, (_, _, active) in zip(weights, items)))
        numerator = q.var(sum(weight * loss for weight, (_, loss, _) in zip(weights, items)))
        losses.append(q.divide(numerator, denominator))
        present.append(q.positive(denominator))
    mean = q.divide(sum(losses), sum(present))
    loss = q.preferences_loss(mean, q.maximum(losses))
    any_assignment = q.any(choice for (sid, _), choice in choices.items() if sid in focus)
    return loss, q.minimum([any_assignment, q.any(present)], 1)

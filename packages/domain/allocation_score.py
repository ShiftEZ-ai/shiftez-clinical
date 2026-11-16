"""Allocation preference items shared by the schedule score and its symbolic objective."""
from .recurrence import inactive
from collections import defaultdict
from fractions import Fraction
from math import lcm

from .generation import allocation_policy_matches, allocation_overtime_allowed, allocation_supervisor_present, preference_window, preferred_supervisors
from .time import aware
from .validation import attached_to, named_people, policy_applies, session_matches, supervision_commitments

from .score_normalization import SCORE_SCALE as SCALE


class _Numbers:
    def maximum(self, values, bound=SCALE):
        return max(list(values) or [0])

    def minimum(self, values, bound=SCALE):
        return min(values)

    def any(self, values):
        return int(any(values))

    def ratio(self, numerator, denominator, bound=SCALE):
        return min(bound, SCALE * numerator // max(1, denominator))


def allocation_score_items(data, choices=None, participants=None, pairings=None, overtime_choices=None, *, math=None, strengths=None):
    """Return {person_id, policy, label, weight, loss ticks, active}; no choices evaluates the observed rota."""
    from packages.contracts.schemas import StrengthMultipliers
    check = data.get('_check_budget', lambda: None)
    q = math or _Numbers()
    strengths = strengths or StrengthMultipliers().model_dump()
    data = {**data, '_allocation_match_cache': data.get('_allocation_match_cache', {})}
    policies = [p for p in data['policies'] if p['definition']['rule']['kind'] == 'allocation_preference']
    overtime_rules = [p for p in data['policies'] if p['definition']['rule']['kind'] == 'allocation_overtime']
    attached_people = {row['person_id'] for row in data.get('mentorships', [])}
    if not policies and not overtime_rules and not attached_people:
        return []
    shifts = {s['id']: s for s in data['shifts'] if not inactive(s)}
    focus = set(data['focus_ids'])
    focused_shifts = [(sid, shift) for sid, shift in shifts.items() if sid in focus]
    actual = {(s['id'], p['person_id']): p for s in shifts.values() for p in s['participants']}
    choices = dict.fromkeys(actual, 1) if choices is None else choices
    participants = actual if participants is None else participants
    pairings, overtime_choices = pairings or {}, overtime_choices or {}
    people = sorted(set(data['people']) | {pid for _, pid in actual})
    assigned_sessions = defaultdict(set)
    if math is None:
        for sid, pid in choices:
            check()
            if sid in focus and choices[sid, pid]:
                assigned_sessions[pid].add(sid)
    paired = defaultdict(dict)
    for (sid, pid, supervisor), pair in pairings.items():
        check()
        paired[sid, pid][supervisor] = pair
    supervisors = {}
    needs_supervisors = any(p['definition']['rule'].get('supervisor_ids')
                            or p['definition']['rule'].get('supervisor_relationship') == 'mentor'
                            or p['definition']['rule'].get('balance_by') in {'supervisor', 'supervisor_group'} for p in policies)

    def participant(sid, pid):
        shift = shifts[sid]
        return participants.get((sid, pid), actual.get((sid, pid), {
            'person_id': pid, 'start': shift['start'], 'end': shift['end'], 'overtime': False, 'participation': 'staff'}))

    if needs_supervisors:
        # Exposure denominators include unselected opportunities; named pairs may reference an absent supervisor.
        all_supervisors = any(p['definition']['rule'].get('balance_by') in {'supervisor', 'supervisor_group'} for p in policies)
        by_session = defaultdict(set)
        if not all_supervisors:
            for sid, pid in choices:
                check()
                by_session[sid].add(pid)
            for sid, _, supervisor in pairings:
                check()
                by_session[sid].add(supervisor)
            for source in (actual, participants):
                for (sid, _), person in source.items():
                    check()
                    if person.get('supervisor_person_id'):
                        by_session[sid].add(person['supervisor_person_id'])
        known_people = set(people)
        if '_supervision_commitments' not in data:
            data = {**data, '_supervision_commitments': supervision_commitments(data)}
        for sid, shift in shifts.items():
            check()
            a, b = aware(shift['start']), aware(shift['end'])
            candidates = people if all_supervisors else sorted(by_session[sid] & known_people)
            supervisors[sid] = {pid for pid in candidates if allocation_supervisor_present(
                data, shift, participant(sid, pid), a, b)}

    def supervisor_terms(sid, pid):
        if paired[sid, pid]:
            return paired[sid, pid]
        named = participant(sid, pid).get('supervisor_person_id')
        return {named: choices.get((sid, pid), 0)} if named else {}

    def applies(policy, sid, pid):
        return allocation_policy_matches(data, policy, shifts[sid], pid)

    def active_preference(policy, sid, pid):
        choice = choices.get((sid, pid), 0)
        rule = policy['definition']['rule']
        if not (rule.get('supervisor_ids') or rule.get('supervisor_relationship') == 'mentor'):
            return choice
        options = supervisor_terms(sid, pid)
        possible = supervisors[sid]
        eligible = preferred_supervisors(data, rule, shifts[sid], pid, possible)
        preferred = q.any(choices.get((sid, supervisor), 0) for supervisor in eligible)
        if options:
            # Optional pair variables may all be zero; only a selected pair overrides co-presence.
            fallback = q.minimum([1 - q.any(options.values()), preferred], 1)
            preferred = q.any([q.any(options.get(supervisor, 0) for supervisor in eligible), fallback])
        return q.minimum([choice, preferred], 1)

    items = []

    def add(pid, policy, label, weight, loss, active):
        items.append({'person_id': pid, 'policy': policy, 'label': label, 'weight': weight, 'loss': loss, 'active': active})

    positive = defaultdict(list)
    ceilings = defaultdict(Fraction)
    for policy in policies:
        check()
        rule = policy['definition']['rule']
        weight = strengths[policy['definition'].get('strength', 'medium')]
        base = rule.get('priority', 0) + rule.get('session_priority', 0)
        extra = rule.get('overtime_priority', 0)
        multiplier = Fraction(weight, strengths['medium'])
        positive_priority = max(0, base, base + extra)
        positive_coefficients = multiplier * max(0, base), multiplier * max(0, base + extra)
        ceiling = multiplier * positive_priority
        named = named_people(policy['definition']['scope'])
        policy_people = people if named is None else sorted(set(people) & named)
        if not policy_people:
            continue
        matching_shifts = {}
        for sid, shift in focused_shifts:
            check()
            if session_matches(data, policy['definition']['scope'], shift):
                matching_shifts[sid] = shift
        for pid in policy_people:
            check()
            matched, against, windows = [], [], set()
            considered = (matching_shifts if math is not None or positive_priority or rule.get('balance_by', 'none') != 'none'
                          else {sid: matching_shifts[sid] for sid in sorted(assigned_sessions[pid]) if sid in matching_shifts})
            for sid, shift in considered.items():
                check()
                if not applies(policy, sid, pid):
                    continue
                choice = choices.get((sid, pid), 0)
                active = active_preference(policy, sid, pid)
                overtime = overtime_choices.get((sid, pid))
                overtime = q.minimum([active, overtime], 1) if overtime is not None else active if participant(sid, pid).get('overtime') else 0
                matched.append(active)
                against.append(int(base < 0) * (active - overtime) + int(base + extra < 0) * overtime)
                if positive_priority:
                    if math is not None or active:
                        positive[sid, pid].append((positive_coefficients[0], active - overtime))
                        positive[sid, pid].append((positive_coefficients[1], overtime))
                    ceilings[sid, pid] += ceiling
                if rule.get('balance_by', 'none') != 'none':
                    windows.add(preference_window(data, policy, shift))
            if matched:
                add(pid, policy, 'allocation preference', weight, q.ratio(sum(against), sum(matched)), q.any(matched))
            for lo, hi in sorted(windows):
                check()
                groups, modes, mode_count = defaultdict(list), [], []
                for sid, shift in shifts.items():
                    check()
                    a, b = aware(shift['start']), aware(shift['end'])
                    if not lo <= a < hi or not policy_applies(data, policy, pid, shift, a, b):
                        continue
                    choice = choices.get((sid, pid), 0)
                    basis = rule['balance_by']
                    if basis in {'supervisor', 'supervisor_group'}:
                        available = supervisors[sid]
                        terms = supervisor_terms(sid, pid)
                        for supervisor in sorted(available):
                            if basis == 'supervisor_group' and supervisor not in rule['supervisor_ids']:
                                continue
                            groups[supervisor if basis == 'supervisor' else None].append(terms.get(supervisor, 0))
                    elif basis == 'service_mode':
                        known = {data['services'][s].get('mode') for s in shift['service_ids']}
                        if known and known <= {'physical', 'virtual'}:
                            fraction = Fraction(int(rule.get('balance_target_mode', 'virtual') in known), len(known))
                            target = Fraction(str(rule.get('balance_target_share', 0.5))).limit_denominator(SCALE)
                            modes.append((fraction - target, choice))
                            mode_count.append(choice)
                    else:
                        groups[None].append(choice)
                exposure, maximum = [], 0
                for xs in groups.values():
                    count = sum(xs)
                    for k, slope in ((1, 1), (2, 1), (3, 2)):
                        if len(xs) > k:
                            exposure.append(slope * q.maximum([0, count - k], len(xs)))
                            maximum += slope * (len(xs) - k)
                balance_weight = weight * Fraction(str(rule.get('balance_weight', 1)))
                if maximum:
                    add(pid, policy, 'allocation exposure', balance_weight, q.ratio(sum(exposure), maximum), q.any(x for xs in groups.values() for x in xs))
                if modes:
                    unit = lcm(*(coefficient.denominator for coefficient, _ in modes))
                    deviation = sum(int(coefficient * unit) * x for coefficient, x in modes)
                    absolute = q.maximum([deviation, -deviation], len(modes) * unit)
                    add(pid, policy, 'service mode share', balance_weight, q.ratio(absolute, unit * sum(mode_count)), q.any(mode_count))

    if ceilings:
        # One fixed opportunity ceiling preserves priority differences between sessions.
        best = max(ceilings.values())
        positive_people = {pid for _, pid in positive}
        for pid in people if math is not None else sorted(assigned_sessions):
            check()
            sessions = sorted(focus & set(shifts) if math is not None else assigned_sessions[pid])
            if pid not in positive_people:
                assigned = [choices.get((sid, pid), 0) for sid in sessions]
                if assigned:
                    active = q.any(assigned)
                    add(pid, None, 'missed positive allocation priority', strengths['medium'], SCALE * active, active)
                continue
            losses, assigned = [], []
            for sid in sessions:
                check()
                coefficients = positive.get((sid, pid), [])
                unit = lcm(best.denominator, *(coefficient.denominator for coefficient, _ in coefficients))
                choice = choices.get((sid, pid), 0)
                achieved = sum(int(coefficient * unit) * active for coefficient, active in coefficients)
                losses.append(q.ratio(int(best * unit) * choice - achieved, int(best * unit)))
                assigned.append(choice)
            if assigned:
                loss = q.ratio(sum(losses), SCALE * sum(assigned))
                add(pid, None, 'missed positive allocation priority', strengths['medium'], loss, q.any(assigned))
    for pid in sorted(attached_people & set(people)):
        check()
        for level, strength in (('required', 'medium'), ('preferred', 'low')):
            matched, missed = [], []
            for sid, shift in focused_shifts:
                check()
                mentors = {mentor for mentor, attachment in attached_to(
                    data, pid, shift, aware(shift['start']), aware(shift['end'])).items() if attachment == level}
                if not mentors:
                    continue
                choice = choices.get((sid, pid), 0)
                paired_with = q.any(supervisor_terms(sid, pid).get(mentor, 0) for mentor in mentors)
                matched.append(choice)
                missed.append(choice - paired_with)
            if matched:
                add(pid, None, f'{level} consultant attachment', strengths[strength], q.ratio(sum(missed), sum(matched)), q.any(matched))
    if overtime_rules:
        options = data.get('_score_options') or data.get('generation_options') or {}
        weight = strengths['medium'] * options.get('overtime_penalty', 1)
        if weight:
            named = [named_people(p['definition']['scope']) for p in overtime_rules]
            overtime_people = people if any(ids is None for ids in named) else sorted(set(people) & set().union(*named))
            for pid in overtime_people:
                check()
                matched, extra = [], []
                for sid, shift in focused_shifts:
                    check()
                    if not allocation_overtime_allowed(data, shift, pid):
                        continue
                    choice = choices.get((sid, pid), 0)
                    matched.append(choice)
                    extra.append(overtime_choices.get((sid, pid), choice if participant(sid, pid).get('overtime') else 0))
                if matched:
                    add(pid, None, 'overtime allocation', weight, q.ratio(sum(extra), sum(matched)), q.any(matched))
    return items

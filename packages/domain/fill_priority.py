"""Configured fill stages over existing staffing and quota opportunities."""
from collections import defaultdict
from datetime import datetime, timedelta
from math import gcd
from typing import NamedTuple
from zoneinfo import ZoneInfo

from .time import aware


STAGE_ORDER = ('mandatory_cover', 'mandatory_quota', 'desired')
STAGE_LABELS = {
    'mandatory_cover': 'required cover',
    'mandatory_quota': 'minimum quotas',
    'desired': 'desired cover',
}


def stage_label(name):
    """Return the user-facing name for a fill or objective stage."""
    stage = name.split(':', 2)[1] if name.startswith('fill:') else name
    if stage == 'coverage':
        stage = STAGE_ORDER[0]
    elif stage == 'diagnostic_minima':
        stage = STAGE_ORDER[1]
    elif stage == 'desired_coverage':
        stage = STAGE_ORDER[2]
    return STAGE_LABELS.get(stage, stage.replace('_', ' '))


def stage_progress(before, after, strict_first=True):
    """Allow progress in the first open stage, or a later stage without regression."""
    first = next((index for index, value in enumerate(before) if value), None)
    if first is None:
        return False
    if strict_first:
        return after[first] < before[first] and all(
            candidate <= current for current, candidate in zip(before[:first], after[:first]))
    target = next((index for index in range(first, len(before)) if after[index] < before[index]), None)
    return target is not None and all(candidate <= current for current, candidate in zip(before[:target], after[:target]))


def allocation_order_rules(policies):
    return [p for p in policies if (rule := p['definition']['rule'])['kind'] == 'allocation_preference'
            and rule.get('session_priority')]


def allocation_tier(data, shift, person_id, supervisor_id=None, *, rules=None):
    """A matching place contributes its position once. The first matching place wins."""
    from .generation import allocation_policy_matches, allocation_supervisor_present, preferred_supervisors
    ranks = []
    present = None
    for policy in allocation_order_rules(data['policies']) if rules is None else rules:
        if not allocation_policy_matches(data, policy, shift, person_id):
            continue
        rule = policy['definition']['rule']
        if rule.get('supervisor_ids') or rule.get('supervisor_relationship') == 'mentor':
            if present is None:
                a, b = aware(shift['start']), aware(shift['end'])
                present = {p['person_id'] for p in shift['participants']
                           if (not supervisor_id or p['person_id'] == supervisor_id)
                           and allocation_supervisor_present(data, shift, p, a, b)}
            if not preferred_supervisors(data, rule, shift, person_id, present):
                continue
        ranks.append(rule['session_priority'])
    return max(ranks, default=0)


class OrderedPlaces(NamedTuple):
    shift_id: str
    start: datetime
    end: datetime
    policy: dict
    people: frozenset[str]
    available: frozenset[str]
    minimum: int
    required: int


def mandatory_order_groups(data, shifts, focus):
    """Requested places by rule, local week, location and descending rank."""
    from .generation import allocation_policy_matches
    from .validation import available_for_coverage, effective_type_definition, person_active_for_interval, session_matches

    policies = [p for p in allocation_order_rules(data['policies']) if p['definition']['severity'] == 'mandatory']
    if not policies:
        return ()
    check = data.get('_check_budget', lambda: None)
    zone = ZoneInfo(data['organisation']['timezone'])
    week_start = data['organisation']['week_start']

    def bucket(shift):
        day = aware(shift['start']).astimezone(zone).date()
        return day - timedelta(days=(day.weekday() - week_start) % 7), shift['location_id']

    active_buckets = {bucket(s) for s in shifts if s['id'] in focus}
    groups = defaultdict(lambda: defaultdict(list))
    for shift in sorted(shifts, key=lambda s: (s['start'], s['id'])):
        check()
        where = bucket(shift)
        if where not in active_buckets:
            continue
        start, end = aware(shift['start']), aware(shift['end'])
        definition = effective_type_definition(data, shift)
        places = [r for r in definition.get('requirements', ()) if r['minimum'] or r.get('target')]
        matching = [p for p in policies if session_matches(data, p['definition']['scope'], shift)]
        if not places or not matching:
            continue
        winners = {}
        active_people = [pid for pid in sorted(data['people']) if person_active_for_interval(data, pid, start, end)]
        for policy in matching:
            rank = policy['definition']['rule']['session_priority']
            for pid in active_people:
                check()
                key = policy['id'], pid
                earlier = winners.get(key)
                if (earlier is None or rank > earlier[0]) and allocation_policy_matches(data, policy, shift, pid):
                    winners[key] = rank, policy
        people = defaultdict(set)
        sources = {}
        for (rule_id, pid), (rank, policy) in winners.items():
            people[rule_id, rank].add(pid)
            sources[rule_id, rank] = policy
        for (rule_id, rank), members in people.items():
            for requirement in places:
                a = start + timedelta(minutes=requirement.get('from_minute', 0))
                b = end if requirement.get('to_minute') is None else start + timedelta(minutes=requirement['to_minute'])
                minimum = requirement['minimum']
                requested = max(minimum, requirement.get('target') or 0)
                available = frozenset(pid for pid in members if available_for_coverage(data, pid, a, b))
                groups[rule_id, where][rank].append(OrderedPlaces(
                    shift['id'], a, b, sources[rule_id, rank], frozenset(members), available, minimum,
                    min(requested, len(available))))
    return [tuple(group[rank] for rank in sorted(group, reverse=True)) for group in groups.values()
            if len(group) > 1 and any(row.shift_id in focus for rows in group.values() for row in rows)]


class FillPriority:
    def __init__(self, model, tiers, *, include_unranked=False):
        self.model = model
        self.costs = defaultdict(list)
        self.tiers = tiers
        self.enabled = any(self.tiers.values())
        # Zero must keep its position ahead of every explicitly deferred place.
        self.include_unranked = include_unranked or min(tiers.values(), default=0) < 0

    def target(self, terms, amount, active=1, *, stage='desired'):
        """Each term is (integer quantity, choice, [(assignment key, contributing choice)])."""
        if not self.enabled or amount <= 0 or isinstance(active, int) and not active:
            return
        terms = [(quantity, chosen, sources) for quantity, chosen, sources in terms if quantity > 0]
        if not any(self.include_unranked or self.tiers[key] for _, _, sources in terms for key, _ in sources):
            return
        unit = gcd(amount, *(quantity for quantity, _, _ in terms))
        amount //= unit
        terms = [(quantity // unit, chosen, sources) for quantity, chosen, sources in terms]
        model = self.model
        credited = defaultdict(list)
        all_credits = []
        for quantity, chosen, sources in terms:
            by_tier = defaultdict(list)
            for key, source in sources:
                tier = self.tiers[key]
                if tier or self.include_unranked:
                    by_tier[tier].append(source)
            credits = []
            for tier, eligible in sorted(by_tier.items()):
                present = eligible[0]
                if len(eligible) > 1:
                    present = model.new_bool_var('fill_stage_present')
                    model.add_max_equality(present, eligible)
                credit = model.new_int_var(0, min(quantity, amount), 'fill_stage_credit')
                model.add(credit <= quantity * present)
                credited[tier].append(credit)
                credits.append(credit)
            model.add(sum(credits) <= quantity * chosen)
            all_credits.extend(credits)
        model.add(sum(all_credits) <= amount * active)
        maximum = sum(quantity for quantity, _, _ in terms)
        upper = max(0, maximum - amount)
        excess = model.new_int_var(0, upper, 'fill_stage_excess')
        model.add_max_equality(excess, [0, sum(quantity * chosen for quantity, chosen, _ in terms) - amount])
        if not isinstance(active, int):
            gated = model.new_int_var(0, upper, 'active_fill_excess')
            model.add_min_equality(gated, [excess, upper * active])
            excess = gated
        for tier, credits in credited.items():
            # A surplus unit gains at most one credit and incurs one excess: adding unnecessary work earns nothing.
            bound = amount + max(0, maximum - amount)
            # shortcut: existing 1/1000 preference units; raise scored precision if smaller differences matter.
            cost = model.new_int_var(0, -(-1000 * bound // amount), 'fill_stage_cost')
            model.add(cost * amount >= 1000 * (amount * active - sum(credits) + excess))
            self.costs[stage, tier].append(cost)

    def objectives(self):
        return [(f'fill:{group}:{tier}', self.costs[group, tier], 0)
                for group, tier in sorted(self.costs, key=lambda key: (STAGE_ORDER.index(key[0]), -key[1]))]

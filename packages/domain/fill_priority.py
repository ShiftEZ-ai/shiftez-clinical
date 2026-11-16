"""Configured fill stages over existing staffing and quota opportunities."""
from collections import defaultdict
from math import gcd


STAGE_ORDER = ('mandatory_cover', 'mandatory_quota', 'desired')


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


def allocation_tier(data, shift, person_id):
    from .generation import allocation_settings
    return allocation_settings(data, shift, person_id, priority_field='session_priority')[0]


class FillPriority:
    def __init__(self, model, data, choices, shifts, enabled):
        self.model = model
        self.costs = defaultdict(list)
        self.tiers = ({key: allocation_tier(data, shifts[key[0]], key[1]) for key in sorted(choices)}
                      if enabled else {})
        self.enabled = any(self.tiers.values())

    def target(self, terms, amount, active=1):
        """Each term is (integer quantity, choice, [(assignment key, contributing choice)])."""
        if not self.enabled or amount <= 0 or isinstance(active, int) and not active:
            return
        terms = [(quantity, chosen, sources) for quantity, chosen, sources in terms if quantity > 0]
        stages = {self.tiers[key] for _, _, sources in terms for key, _ in sources}
        if not terms or not stages:
            return
        unit = gcd(amount, *(quantity for quantity, _, _ in terms))
        amount //= unit
        terms = [(quantity // unit, chosen, sources) for quantity, chosen, sources in terms]
        model = self.model
        credited = defaultdict(list)
        all_credits = []
        for quantity, chosen, sources in terms:
            by_stage = defaultdict(list)
            for key, source in sources:
                by_stage[self.tiers[key]].append(source)
            credits = []
            for stage, eligible in sorted(by_stage.items()):
                present = eligible[0]
                if len(eligible) > 1:
                    present = model.new_bool_var('fill_stage_present')
                    model.add_max_equality(present, eligible)
                credit = model.new_int_var(0, min(quantity, amount), 'fill_stage_credit')
                model.add(credit <= quantity * present)
                credited[stage].append(credit)
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
        for stage, credits in credited.items():
            # A surplus unit gains at most one credit and incurs one excess: adding unnecessary work earns nothing.
            bound = amount + max(0, maximum - amount)
            # shortcut: existing 1/1000 preference units; raise scored precision if smaller differences matter.
            cost = model.new_int_var(0, -(-1000 * bound // amount), 'fill_stage_cost')
            model.add(cost * amount >= 1000 * (amount * active - sum(credits) + excess))
            self.costs[stage].append(cost)

    def objectives(self):
        return [(f'fill:{stage}', terms, 0) for stage, terms in sorted(self.costs.items(), reverse=True)]

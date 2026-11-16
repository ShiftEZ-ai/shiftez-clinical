"""Small, deterministic helpers for the generic ``Spread evenly`` rule.

The first supported holder is a session's supervision team.  Levelling is a
property of one session, so it belongs beside pairing rather than in the
rota-wide fairness ledger.  The matcher is deliberately bounded by the
people in one session and uses a capacitated bipartite matching to minimise
the largest holder load before reporting a finding.
"""

from collections import Counter


def spread_policy_key(policy):
    """Identify one resolved Spread evenly line without collapsing its siblings."""
    template = policy.get('template_line') or {}
    return (policy.get('id'), template.get('id'), template.get('line'), template.get('slot'), policy.get('count_bucket'))


def session_spread_policies(policies, applies, member_id, shift, a, b):
    """Return active generic Spread evenly lines for one session member.

    ``applies`` is the canonical policy scope predicate supplied by the
    caller.  The line's who therefore selects the members being distributed;
    the first supported holder is the session's qualified supervisors.
    """
    result = {}
    for policy in policies:
        if (policy.get('status', 'active') != 'active' or not policy.get('active', True)
                or policy['definition']['rule'].get('kind') != 'spread_evenly'
                or policy['definition']['rule'].get('holder', 'supervisors') != 'supervisors'
                or policy['definition']['rule'].get('within', 'session') != 'session'
                or not applies(policy, member_id, shift, a, b)):
            continue
        result.setdefault(spread_policy_key(policy), policy)
    return list(result.values())


def spread_gap(policy):
    """The configured allowed load gap for a resolved Spread evenly line."""
    rule = policy['definition']['rule']
    configured = int(rule.get('maximum_gap', 1))
    return min(1, configured) if rule.get('mode', 'levelled') == 'levelled' else configured


def levelled_pairing(trainees, supervisors, *, eligible=None, capacities=None):
    """Return a deterministic min-max trainee assignment for one session.

    ``eligible`` maps each trainee to the supervisors they may use.  Empty or
    missing entries mean every supervisor.  ``capacities`` is per supervisor;
    omitted capacities are unbounded for this session.  A result always has
    ``assignment`` and ``counts``; ``reason`` is ``None`` when every trainee
    was assigned, otherwise it explains why a levelled assignment is not
    possible.
    """
    trainees = sorted(set(trainees))
    supervisors = sorted(set(supervisors))
    capacities = {sid: len(trainees) for sid in supervisors} | (capacities or {})
    allowed = {tid: set((eligible or {}).get(tid, supervisors)) & set(supervisors) for tid in trainees}
    if not trainees:
        return {'assignment': {}, 'counts': dict.fromkeys(supervisors, 0), 'maximum': 0, 'minimum': 0,
                'reason': None}
    if not supervisors:
        return {'assignment': {}, 'counts': {}, 'maximum': 0, 'minimum': 0, 'reason': 'no_eligible_supervisor'}
    if any(not allowed[tid] for tid in trainees):
        return {'assignment': {}, 'counts': dict.fromkeys(supervisors, 0), 'maximum': 0, 'minimum': 0,
                'reason': 'eligibility'}

    if all(allowed[tid] == set(supervisors) for tid in trainees) and len({capacities[sid] for sid in supervisors}) == 1:
        counts = dict.fromkeys(supervisors, 0)
        assignment = {}
        for tid in trainees:
            choices = [sid for sid in supervisors if counts[sid] < capacities[sid]]
            if not choices:
                return {'assignment': {}, 'counts': dict.fromkeys(supervisors, 0), 'maximum': 0, 'minimum': 0,
                        'reason': 'capacity'}
            sid = min(choices, key=lambda candidate: (counts[candidate], candidate))
            assignment[tid] = sid
            counts[sid] += 1
        return {'assignment': assignment, 'counts': counts, 'maximum': max(counts.values(), default=0),
                'minimum': min(counts.values(), default=0), 'reason': None}

    # An augmenting-path matching over capacity slots is linear in the tiny session-sized
    # graph and avoids adding one CP-SAT objective term per trainee.
    def match(limit, minimum=0):
        slots = [(sid, slot) for sid in supervisors for slot in range(min(limit, capacities[sid]))]
        required = [(sid, slot) for sid in supervisors for slot in range(min(minimum, capacities[sid]))]
        if len(slots) < len(trainees) or len(required) > len(trainees):
            return None
        owner = {}
        required_set = set(required)

        # First match each lower-bound slot to a distinct trainee.  Keeping
        # this pass separate means a later augmenting path can never empty a
        # slot that established the requested minimum load.
        def claim_required(key, seen):
            for tid in trainees:
                if tid in seen or key[0] not in allowed[tid]:
                    continue
                seen.add(tid)
                old = next((slot for slot, owner_id in owner.items() if owner_id == tid), None)
                if old is None or claim_required(old, seen):
                    owner[key] = tid
                    return True
            return False

        # Fill lower-bound slots first. This makes the second objective
        # (maximise the smallest load) explicit instead of relying on graph
        # traversal order.
        for key in required:
            if not claim_required(key, set()):
                return None

        assigned = {tid: key for key, tid in owner.items()}

        def move_trainee(tid, seen):
            old = assigned.get(tid)
            for sid in sorted(allowed[tid]):
                for slot in range(min(limit, capacities[sid])):
                    key = sid, slot
                    if key in seen or old in required_set and key not in required_set:
                        continue
                    seen.add(key)
                    prior = owner.get(key)
                    if prior == tid:
                        return True
                    if prior is not None and not move_trainee(prior, seen):
                        continue
                    if old is not None and owner.get(old) == tid:
                        del owner[old]
                    owner[key] = tid
                    assigned[tid] = key
                    return True
            return False

        # Constrained trainees first makes the result stable and leaves the
        # widest choices for the later augmenting paths.
        for tid in sorted(trainees, key=lambda item: (len(allowed[item]), item)):
            if tid not in assigned and not move_trainee(tid, set()):
                return None
        return {tid: sid for (sid, _), tid in owner.items()}

    lower = max(1, (len(trainees) + len(supervisors) - 1) // len(supervisors))
    upper = max((capacities[sid] for sid in supervisors), default=0)
    if upper < lower:
        return {'assignment': {}, 'counts': dict.fromkeys(supervisors, 0), 'maximum': 0, 'minimum': 0,
                'reason': 'capacity'}
    while lower < upper:
        middle = (lower + upper) // 2
        if match(middle) is None:
            lower = middle + 1
        else:
            upper = middle
    min_lower, min_upper = 0, min(len(trainees) // len(supervisors), lower)
    while min_lower < min_upper:
        middle = (min_lower + min_upper + 1) // 2
        if match(lower, middle) is None:
            min_upper = middle - 1
        else:
            min_lower = middle
    assignment = match(lower, min_lower)
    if assignment is None:
        return {'assignment': {}, 'counts': dict.fromkeys(supervisors, 0), 'maximum': 0, 'minimum': 0,
                'reason': 'capacity'}
    counts = Counter(assignment.values())
    counts = {sid: counts.get(sid, 0) for sid in supervisors}
    return {'assignment': assignment, 'counts': counts, 'maximum': max(counts.values(), default=0),
            'minimum': min(counts.values(), default=0), 'reason': None}


def levelled_finding(trainees, supervisors, assignments, *, eligible=None, capacities=None, maximum_gap=1):
    """Describe an imbalance or impossible levelled pairing for validation."""
    result = levelled_pairing(trainees, supervisors, eligible=eligible, capacities=capacities)
    actual = Counter(assignments.get(tid) for tid in trainees if assignments.get(tid) in set(supervisors))
    actual = {sid: actual.get(sid, 0) for sid in sorted(set(supervisors))}
    missing = len(trainees) - sum(actual.values())
    unavoidable = result['reason'] is None and result['maximum'] - result['minimum'] > maximum_gap
    if result['reason'] or missing or unavoidable or (actual and max(actual.values()) - min(actual.values()) > maximum_gap):
        reason = result['reason'] or ('unpaired' if missing else
                                      'unavoidable_gap' if unavoidable else 'imbalance')
        return {
            'reason': reason,
            'trainees': len(trainees), 'supervisors': len(supervisors),
            'assigned': sum(actual.values()), 'missing': missing,
            'actual': actual, 'expected': result['counts'],
            'maximum': max(actual.values(), default=0), 'minimum': min(actual.values(), default=0),
            'expected_maximum': result['maximum'], 'expected_minimum': result['minimum'],
        }
    return None


def levelled_order(supervisors, assignments=()):
    """Stable fill order used by greedy and manual pairing suggestions."""
    loads = Counter(assignments)
    return sorted(set(supervisors), key=lambda sid: (loads.get(sid, 0), sid))

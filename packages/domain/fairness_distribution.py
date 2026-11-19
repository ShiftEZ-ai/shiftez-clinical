"""Opt-in Fair share distributions: one set of counted groups for reports and both engines."""

from collections import defaultdict
from datetime import timedelta
from fractions import Fraction

from .conditions import scope_matches_throughout
from .recurrence import inactive
from .time import aware


class PairLimitBudgetExceeded(ValueError):
    def __init__(self, policy_id, maximum):
        self.issue = {'code': 'FAIRNESS_PAIR_BUDGET', 'rule_id': policy_id,
                      'field': 'generation_options.pair_limit_max_opportunities',
                      'message': 'Pair balancing exceeds its model budget; narrow Who or Partners, or increase the pair opportunity budget.',
                      'maximum': maximum}
        super().__init__(self.issue['message'])


def pair_opportunities(data, policy, candidates, participants, focus):
    """Potential shared-session pairs, filtered once by the canonical goal and partner selectors."""
    check = data.get('_check_budget', lambda: None)
    people = _people(data, policy)
    who, partners = set(people['who']), set(people['partners'])
    by_shift = defaultdict(set)
    for sid, pid in candidates:
        check()
        if sid in focus:
            by_shift[sid].add(pid)
    for shift in data['shifts']:
        check()
        sid = shift['id']
        if sid not in by_shift or inactive(shift):
            continue
        for pid in sorted(by_shift[sid] & who):
            check()
            participant = participants[sid, pid]
            a, b = aware(participant['start']), aware(participant['end'])
            if applies(data, policy, pid, shift, a, b):
                for partner in sorted((by_shift[sid] & partners) - {pid}):
                    check()
                    if scope_matches_throughout(data, {'conditions': policy['definition']['rule']['partners']},
                                               partner, shift, a, b):
                        yield sid, pid, partner


def _people(data, policy):
    from .fairness import _goal_context_key, goal_people
    cache = data.get('_distribution_cache')
    key = ('people', _goal_context_key(data, policy)) if cache is not None else None
    if cache is None or key[1] is None:
        people = goal_people(data, policy)
    else:
        if key not in cache:
            cache[key] = goal_people(data, policy)
        people = cache[key]
    if '_distribution_people' in data:
        return {**people, 'who': [pid for pid in people['who'] if pid in data['_distribution_people']]}
    return people


def bucket_of(data, rule, day):
    if rule.get('distribution_period', 'week') == 'rota':
        return ''
    return (day - timedelta(days=(day.weekday() - data['organisation']['week_start']) % 7)).isoformat()


def applies(data, policy, pid, shift, start, end):
    from .fairness import goal_applies, goal_sources
    return any(goal_applies(data, source, pid, shift, start, end)
               and scope_matches_throughout(data, source['definition']['scope'], pid, shift, start, end)
               for source in goal_sources(policy, pid))


def _available_buckets(data, policy, pid):
    from .fairness import _goal_context_key, goal_intervals, goal_period, member_days, rota_dates
    check = data.get('_check_budget', lambda: None)
    cache = data.get('_distribution_cache')
    key = ('available', _goal_context_key(data, policy), pid) if cache is not None else None
    if cache is not None and key[1] is not None and key in cache:
        return cache[key]
    start, end = goal_period(data, policy, pid)
    days = defaultdict(int)
    for lo, hi in goal_intervals(data, policy, pid, start, min(rota_dates(data)[1], end)):
        check()
        for day in member_days(data, pid, lo, hi):
            check()
            days[bucket_of(data, policy['definition']['rule'], day)] += 1
    if cache is not None and key[1] is not None:
        cache[key] = days
    return days


def counted_groups(data, policy, entries=None, *, include_history=True):
    """(person, bucket, partner) -> total/selected linear terms; attendance can be a number or a solver variable.

    Entries are (duty, participation, attendance, {partner: joint attendance}). Numeric published history
    uses this same function, so filters, local weeks and partial participations cannot diverge by engine.
    """
    from .fairness import contribution, local_day, goal_identity
    check = data.get('_check_budget', lambda: None)
    rule = policy['definition']['rule']
    people = _people(data, policy)
    who, partners = set(people['who']), set(people['partners'])
    groups = defaultdict(lambda: {'total': [], 'selected': []})
    if entries is None:
        focus = set(data['focus_ids'])
        entries = ((s, p, 1, {q['person_id']: 1 for q in s['participants'] if q.get('person_id') != p.get('person_id')}
                    if rule.get('pairing', 'present') == 'present' else {})
                   for s in data['shifts'] if s['id'] in focus and not inactive(s)
                   for p in s['participants'])
    for shift, participant, attendance, pairs in entries:
        check()
        pid = participant.get('person_id')
        a, b = aware(participant['start']), aware(participant['end'])
        if pid not in who or inactive(shift) or not applies(data, policy, pid, shift, a, b):
            continue
        bucket = '' if rule['distribution'] == 'share' else bucket_of(data, rule, local_day(a, data['organisation']['timezone']))
        if rule['distribution'] == 'pair_limit':
            if rule.get('pairing', 'present') == 'supervised':
                if not pairs and participant.get('supervisor_person_id'):
                    pairs = {participant['supervisor_person_id']: attendance}
            for partner, joint in pairs.items():
                check()
                if partner in partners and partner != pid and scope_matches_throughout(
                        data, {'conditions': rule['partners']}, partner, shift, a, b):
                    groups[pid, bucket, partner]['total'].append((Fraction(1), joint))
            continue
        coefficient = Fraction(str(contribution(rule['metric'], shift, participant)))
        groups[pid, bucket, None]['total'].append((coefficient, attendance))
        if rule['distribution'] == 'share' and scope_matches_throughout(
                data, {'conditions': rule['within']}, pid, shift, a, b):
            groups[pid, bucket, None]['selected'].append((coefficient, attendance))
    if include_history:
        history = (data.get('fairness_history') or {}).get(goal_identity(policy), {})
        for row in history.get('distribution', []):
            check()
            if row['person_id'] in who:
                group = groups[row['person_id'], row['bucket'], row.get('partner_id')]
                for field in ('total', 'selected'):
                    if row.get(field):
                        group[field].append((Fraction(str(row[field])), 1))
    return dict(groups)


def numeric_groups(groups):
    """Serializable sufficient statistics for published history (never solver variables)."""
    return [{'person_id': pid, 'bucket': bucket, 'partner_id': partner,
             **{field: float(sum(coefficient * value for coefficient, value in terms[field]))
                for field in ('total', 'selected')}}
            for (pid, bucket, partner), terms in sorted(groups.items(), key=lambda item: tuple(v or '' for v in item[0]))]


def distribution_plan(data, policy, entries=None):
    """Linear deviations and their one-sided/absolute meaning, shared by evaluation and CP-SAT compilation."""
    check = data.get('_check_budget', lambda: None)
    rule = policy['definition']['rule']
    groups = counted_groups(data, policy, entries)
    plans = []
    if rule['distribution'] == 'period_spread':
        from .fairness import rota_dates
        # Earlier weeks are fixed history; only rota weeks are decisions.
        first = bucket_of(data, rule, rota_dates(data)[0])
        for pid in _people(data, policy)['who']:
            check()
            days = {bucket: available for bucket, available in _available_buckets(data, policy, pid).items()
                    if bucket >= first}
            terms = [term for (person, bucket, _), group in groups.items()
                     if person == pid and bucket >= first for term in group['total']]
            for bucket, available in sorted(days.items()):
                check()
                share = Fraction(available, sum(days.values()))
                counted = groups.get((pid, bucket, None), {}).get('total', [])
                plans.append({'person_id': pid, 'bucket': bucket, 'partner_id': None,
                              'terms': counted + [(-coefficient * share, value) for coefficient, value in terms],
                              'actual': counted, 'expected': [(coefficient * share, value) for coefficient, value in terms],
                              'positive': False, 'offset': Fraction(0),
                              'denominator': [(Fraction(max(1, sum(days.values()))), 1)]})
        return plans
    for (pid, bucket, partner), group in sorted(groups.items(), key=lambda item: tuple(v or '' for v in item[0])):
        check()
        if rule['distribution'] == 'share':
            share = Fraction(str(rule.get('target_share', .5)))
            expected = [(coefficient * share, value) for coefficient, value in group['total']]
            actual = group['selected']
            terms = actual + [(-coefficient, value) for coefficient, value in expected]
            offset = Fraction(0)
            denominator = [(Fraction(max(1, sum(_available_buckets(data, policy, pid).values()))), 1)]
        else:
            actual, expected = group['total'], []
            terms = actual
            offset = -Fraction(str(rule.get('person_amounts', {}).get(pid, rule.get('amount') or 0)))
            denominator = [(max(Fraction(1), -offset), 1)]
        plans.append({'person_id': pid, 'bucket': bucket, 'partner_id': partner, 'terms': terms,
                      'actual': actual, 'expected': expected, 'offset': offset,
                      'positive': rule['distribution'] == 'pair_limit', 'denominator': denominator})
    return plans


def value(terms):
    return sum(coefficient * attendance for coefficient, attendance in terms)


def loss(plan):
    deviation = value(plan['terms']) + plan['offset']
    return max(Fraction(0), deviation) if plan['positive'] else abs(deviation)


def distribution_report(data, policy):
    from .fairness import UNITS, goal_identity
    check = data.get('_check_budget', lambda: None)
    rule = policy['definition']['rule']
    costs, actual, expected, buckets = defaultdict(float), defaultdict(float), defaultdict(float), defaultdict(list)
    plans = distribution_plan(data, policy)
    for plan in plans:
        check()
        pid = plan['person_id']
        amount = float(value(plan['actual']))
        aim = float(value(plan['expected']) - plan['offset'])
        cost = float(loss(plan) / max(Fraction(1), value(plan['denominator'])))
        costs[pid] += cost
        actual[pid] += amount
        expected[pid] += aim
        buckets[pid].append({'bucket': plan['bucket'], 'partner_id': plan['partner_id'],
                             'actual': amount, 'expected': aim, 'cost': min(1.0, cost)})
    prior = defaultdict(float)
    for plan in distribution_plan(data, policy, entries=[]):
        check()
        prior[plan['person_id']] += float(value(plan['actual']))
    line = policy.get('template_line') or {}
    rows = [{'person_id': pid, 'prior': prior[pid], 'planned': actual[pid] - prior[pid], 'actual': actual[pid], 'expected': expected[pid],
             'low': expected[pid], 'high': expected[pid], 'cost': min(1.0, costs[pid]),
             'status': ('within' if not costs[pid] else 'below' if actual[pid] < expected[pid] else 'above'),
             'buckets': buckets[pid]}
            for pid in sorted(costs)]
    return {'policy_id': policy['id'], 'goal_id': goal_identity(policy), 'name': policy['name'],
            'metric': rule['metric'], 'aim': rule['aim'], 'distribution': rule['distribution'],
            'distribution_period': rule.get('distribution_period', 'week'), 'weight': rule.get('weight', 1),
            'strength': policy['definition'].get('strength', 'medium'), 'unit': UNITS[rule['metric']],
            'period': rule.get('period', 'rota'), 'people': rows, 'pairs': [],
            'history': bool((data.get('fairness_history') or {}).get(goal_identity(policy))),
            'line_id': line.get('id'), 'line': line.get('line'), 'slot': line.get('slot'),
            'balance_between_people': policy['definition'].get('balance_between_people', True),
            **({'count_bucket': policy['count_bucket']} if 'count_bucket' in policy else {}),
            'score': round(100 * (1 - sum(row['cost'] for row in rows) / len(rows)), 1) if rows else 100.0}


def marginal_cost(data, policy, shift, participant, history):
    """Greedy ranking uses the same counted groups and raw deviations as the solver and report."""
    check = data.get('_check_budget', lambda: None)
    pid = participant['person_id']
    rule = policy['definition']['rule']
    affected = {pid}
    focus = set(data['focus_ids'])
    if rule['distribution'] == 'pair_limit' and rule.get('pairing', 'present') == 'present':
        people = _people(data, policy)
        if pid in people['partners']:
            affected.update(p['person_id'] for p in shift['participants'])
        affected.intersection_update(people['who'])
        cache = data.get('_distribution_cache', {})
        key, revision = ('participations',), data.get('_distribution_revision', 0)
        if key not in cache or cache[key][0] != revision:
            by_person = defaultdict(list)
            for duty in data['shifts']:
                check()
                if duty['id'] in focus and not inactive(duty):
                    for person in duty['participants']:
                        by_person[person['person_id']].append((duty, person))
            cache[key] = revision, by_person
        history = [entry for person in sorted(affected) for entry in cache[key][1].get(person, [])]
    data = {**data, '_distribution_people': affected}
    pairs = lambda s, p: ({q['person_id']: 1 for q in s['participants'] if q.get('person_id') != p['person_id']}
                         if rule.get('pairing', 'present') == 'present' else {})
    entries = [(s, p, 1, pairs(s, p)) for s, p in history if s['id'] in focus]
    before = sum(loss(plan) / value(plan['denominator']) for plan in distribution_plan(data, policy, entries))
    trial_shift = {**shift, 'participants': [*shift['participants'], participant]}
    entries = [(trial_shift if s['id'] == shift['id'] else s, p, 1,
                pairs(trial_shift if s['id'] == shift['id'] else s, p)) for s, p in history if s['id'] in focus]
    entries.append((trial_shift, participant, 1, pairs(trial_shift, participant)))
    after = sum(loss(plan) / value(plan['denominator']) for plan in distribution_plan(data, policy, entries))
    return float(after - before)


def greedy_goals(data):
    """An enabled comparison retains all its lines, just like the solver and report."""
    from .fairness import comparison_goal, goal_groups
    return [comparison_goal(group) for group in goal_groups(data) if any(
        p['definition']['rule'].get('optimize_greedy')
        or p['definition']['rule']['metric'] == 'lead_turns'
        or p['definition']['rule'].get('distribution', 'people') != 'people' for p in group)]


def marginal_priority(data, shift, participant, history=(), *, goals=None):
    """Weighted change in the canonical goal value; higher priority means less fairness loss."""
    from .fairness import goal_report, report_weights, strengths_of
    strengths, priority = strengths_of(data), 0.0
    cache = data.get('_distribution_cache', {})
    a, b = aware(participant['start']), aware(participant['end'])
    pid, supervisor = participant['person_id'], participant.get('supervisor_person_id')
    for policy in data.get('_distribution_goals', ()) if goals is None else goals:
        rule = policy['definition']['rule']
        if not (applies(data, policy, pid, shift, a, b) or people_pair_affected(data, policy, shift, pid)
                or rule['metric'] == 'supervision' and supervisor and applies(data, policy, supervisor, shift, a, b)
                or rule.get('share_by') == 'supervisable_sessions' and any(
                    applies(data, policy, p['person_id'], shift, a, b) for p in shift['participants'])):
            continue
        weight_key = ('goal_weight', id(policy))
        if weight_key not in cache:
            weights = report_weights(goal_report(data, policy, planned_values=({}, {})), strengths)
            cache[weight_key] = (sum(weights.values()) / len(weights) if weights else
                                strengths.get(policy['definition'].get('strength', 'medium'), 0) * rule.get('weight', 1))
        cost = (people_marginal_cost(data, policy, shift, participant)
                if rule.get('distribution', 'people') == 'people'
                else marginal_cost(data, policy, shift, participant, history))
        priority -= float(cache[weight_key]) / strengths['medium'] * cost
    return priority


def people_marginal_cost(data, policy, shift, participant):
    """Explicit greedy opt-in for existing goals uses their canonical report, including weekend deduplication."""
    from .fairness import goal_report, planned, report_value, scoring_settings, strengths_of
    check = data.get('_check_budget', lambda: None)
    cache = data.get('_distribution_cache', {})
    key, revision = ('report', id(policy)), data.get('_distribution_revision', 0)
    if (policy['definition']['rule']['metric'] == 'workload'
            or policy['definition']['rule'].get('share_by') == 'supervisable_sessions'):
        if key not in cache or cache[key][0] != revision:
            cache[key] = revision, goal_report(data, policy)
        before = cache[key][1]
        trial_shift = {**shift, 'participants': [p for p in shift['participants']
                       if p['person_id'] != participant['person_id']] + [participant]}
        trial = {**data, 'shifts': [trial_shift if duty['id'] == shift['id'] else duty for duty in data['shifts']]}
        after = goal_report(trial, policy)
        strengths = strengths_of(data)
        emphasis = scoring_settings(data)['fairness_worst_off_emphasis']
        return report_value(before, strengths, emphasis=emphasis) - report_value(after, strengths, emphasis=emphasis)
    pair_goal = policy['definition']['rule']['metric'] == 'pair_exposure'
    if key not in cache or cache[key][0] != revision:
        people = _people(data, policy)
        values, pairs = planned(data, policy, people['who'], people['partners'])
        by_person, focus = defaultdict(list), set(data['focus_ids'])
        for duty in data['shifts']:
            check()
            if duty['id'] in focus and not inactive(duty):
                for person in duty['participants']:
                    by_person[person['person_id']].append(duty)
        before = (goal_report(data, policy, (values, pairs)) if pair_goal
                  else goal_report(data, policy, (values, pairs), _cost_only=True))
        pair_rows = defaultdict(list)
        for row in before['pairs']:
            check()
            pair_rows[row['person_id']].append(row)
        cache[key] = revision, before, values, pairs, by_person, pair_rows
    _, before, values, pairs, by_person, pair_rows = cache[key]
    pid = participant['person_id']
    people = _people(data, policy)
    affected = {pid}
    if policy['definition']['rule']['metric'] == 'supervision':
        affected = ({pid, participant.get('supervisor_person_id')} - {None}) & set(people['who'])
    if pair_goal:
        if policy['definition']['rule'].get('pairing', 'present') == 'present':
            affected.update(p['person_id'] for p in shift['participants'])
        affected.intersection_update(people['who'])
        if not affected or not before['pairs']:
            return 0.0
    trial_shift = {**shift, 'participants': [p for p in shift['participants']
                   if p['person_id'] != participant['person_id']] + [participant]}
    own = list({s['id']: s for person in affected for s in by_person.get(person, [])
                if s['id'] != shift['id']}.values()) + [trial_shift]
    trial = {**data, 'shifts': own, 'focus_ids': [s['id'] for s in own]}
    added, pair_counts = planned(trial, policy, sorted(affected), people['partners'])
    updated = {**values, **{person: added.get(person, 0) for person in affected}}
    updated_pairs = {**{key: count for key, count in pairs.items() if key[0] not in affected}, **pair_counts}
    strengths = strengths_of(data)
    if pair_goal:
        rows = [row for person in sorted(affected) for row in pair_rows[person]]
        after = goal_report({**data, '_distribution_people': affected}, policy, (updated, updated_pairs))
        return (report_value({**before, 'pairs': rows}) - report_value(after)) * len(rows) / len(before['pairs'])
    emphasis = scoring_settings(data)['fairness_worst_off_emphasis']
    value_key, strength_key = ('report_value', id(policy)), (tuple(sorted(strengths.items())), emphasis)
    saved = cache.get(value_key)
    if saved is None or saved[0] is not before or saved[1] != strength_key:
        saved = cache[value_key] = before, strength_key, report_value(before, strengths, emphasis=emphasis)
    after = goal_report(data, policy, (updated, updated_pairs), _cost_only=True)
    return saved[2] - report_value(after, strengths, emphasis=emphasis)


def people_pair_affected(data, policy, shift, pid):
    """A partner outside Who can still change existing people's shared-session exposure."""
    rule = policy['definition']['rule']
    if (rule.get('distribution', 'people') not in {'people', 'pair_limit'} or rule['metric'] != 'pair_exposure'
            or rule.get('pairing', 'present') != 'present'):
        return False
    people = _people(data, policy)
    if pid not in people['partners']:
        return False
    from .fairness import goal_applies
    return any(p['person_id'] in people['who'] and goal_applies(
        data, policy, p['person_id'], shift, aware(p['start']), aware(p['end'])) for p in shift['participants'])

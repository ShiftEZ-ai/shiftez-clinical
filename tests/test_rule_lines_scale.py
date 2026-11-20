"""The line checks at department scale: `duplicates`, `outranking_by_number`, `both_apply`, `inherited` and blank levels
compare lines only within their buckets (same measure, where, days and hours; tiers; people) with a sweep by date, so
1,000 lines in one where stay fast â€” and say exactly what the pairwise reading below says (random line sets)."""
import random
import time

import pytest

from packages.domain import rule_lines as R
from packages.domain.rule_lines import _by_number, _broader_levels, _overlap, has_value, named_people, tier, when

PEOPLE = [f'00000000-0000-4000-9000-{i:012d}' for i in range(1000)]
RANKS = [f'00000000-0000-4000-7000-{i:012d}' for i in range(100)]
DATES = [None, '2026-10-01', '2026-11-01', '2026-12-01', '2027-01-01']


def L(i):
    return f'00000000-0000-4000-8000-{i:012d}'


def made(entry, lines):
    return {'id': 'rule', 'name': entry, 'kind': 'catalogue', 'status': 'active', 'active': True, 'department_id': 'd', 'revision': 1,
            'starts_on': '2026-01-01', 'ends_on': None, 'definition': {'rule': {'kind': 'catalogue', 'id': entry, 'settings': {}, 'lines': lines}}}


# The pairwise reading (before buckets): every line against every other. ------------------------------------------------

def pairwise_duplicates(rule):
    lines, seen, out = rule.get('lines') or [], {}, []
    dates = [when(rule, line) for line in lines]
    for n, line in enumerate(lines):
        key = R.json.dumps([R._canonical_tree(line.get('who')), sorted(line.get('numbers') or {}), *R._where(rule, line),
                            R.measure(rule, line, block=True)], sort_keys=True, default=str)
        out += [(k, n) for k in seen.get(key, []) if _overlap(dates[n], dates[k])]
        seen.setdefault(key, []).append(n)
    return out


def pairwise_outranking_by_number(policy):
    rule = policy['definition']['rule']
    lines = rule.get('lines') or []
    if R.CATALOGUE_BY_ID[rule['id']]['safety'] or len(lines) < 2:
        return {}
    tiers, dates, out = [tier(line) for line in lines], [when(rule, line) for line in lines], {}
    def strength(n, k):
        return lines[n]['numbers'][k].get('level', 'mandatory')
    def same_strength(n, w, k):
        # A more specific line keeps a broader line's binding strength: mandatory and advisory never replace each other.
        return strength(n, k) is None or strength(w, k) is None or (strength(n, k) == 'mandatory') == (strength(w, k) == 'mandatory')
    for (k, _), group in _by_number(rule).items():
        for n in group:
            if wins := [w for w in group if tiers[w] > tiers[n] and _overlap(dates[n], dates[w]) and same_strength(n, w, k)]:
                out[(n, k)] = wins
    return dict(sorted(out.items()))


def pairwise_both_apply(policy):
    rule = policy['definition']['rule']
    lines, same = rule.get('lines') or [], set(pairwise_duplicates(rule))
    tiers, dates, out = [tier(line) for line in lines], [when(rule, line) for line in lines], set()
    for (number, _), group in _by_number(rule).items():
        together = {}
        for n in group:
            if has_value(lines[n], number):
                for key in named_people(lines[n]) if tiers[n] == 3 else [tiers[n]]:
                    together.setdefault(key, []).append(n)
        out.update((k, n) for members in together.values() for i, k in enumerate(members) for n in members[i + 1:]
                   if tiers[k] == tiers[n] and (k, n) not in same and _overlap(dates[k], dates[n]))
    return sorted(out)


def pairwise_inherited(policy, would=None):
    rule = policy['definition']['rule']
    lines = rule.get('lines') or []
    entry = R.CATALOGUE_BY_ID[rule['id']]
    keys = [[k for k in line['numbers'] if k in entry['numbers']] for line in lines]
    tiers, dates, out = [tier(line) for line in lines], [when(rule, line) for line in lines], {}
    for n, line in enumerate(lines):
        mine = (would or {}).get(line['id'])
        for k in entry['numbers']:
            if k == 'apply' or k in line['numbers']:
                continue
            group = R._group(rule, line, k, keys[n] + [k])  # what the blank number would count here
            from_ = [w for w in range(len(lines)) if tiers[w] < tiers[n] and k in lines[w]['numbers']
                     and R._group(rule, lines[w], k, keys[w]) == group
                     and _overlap(dates[n], dates[w]) and (not mine or would.get(lines[w]['id'], set()) & mine)]
            if from_:
                src = next(w for w in from_ if tiers[w] == max(tiers[x] for x in from_))
                if has_value(lines[src], k):
                    out.setdefault(line['id'], {})[k] = {**lines[src]['numbers'][k], 'from': lines[src]['id']}
    return out


def pairwise_broader_levels(lines, by_number):
    out = {}
    for (k, _), group in by_number.items():
        for n in group:
            if lines[n]['numbers'][k].get('level', 'mandatory') is not None:
                continue
            from_ = sorted((w for w in group if tier(lines[w]) < tier(lines[n]) and has_value(lines[w], k)
                            and lines[w]['numbers'][k].get('level', 'mandatory') is not None),
                           key=lambda w: (-tier(lines[w]), w))
            if from_:
                out[(n, k)] = tuple(from_)
    return out


def random_line(r, i, entry):
    who = r.choice([None, {'conditions': [{'field': 'person_ids', 'values': r.sample(PEOPLE[:6], r.choice([1, 1, 2]))}]},
                    {'conditions': [{'field': 'category_ids', 'values': [r.choice(RANKS[:2])]}]}])
    safety = entry == 'rest_between_duties'
    numbers = {k: {'value': 11 if safety else r.choice([None, 1, 2, 3, 5]), 'level': 'mandatory' if safety else r.choice(['mandatory', None, 'medium'])}
               for k in (['min'] if safety else ['min', 'max']) if r.random() < .6} or {'min' if safety else 'max': {'value': 4, 'level': 'mandatory'}}
    starts_on, ends_on = sorted(r.sample(DATES[1:], 2)) if r.random() < .7 else (r.choice(DATES), None)
    return {'id': L(i), 'numbers': numbers, 'starts_on': starts_on, 'ends_on': ends_on, **({'who': who} if who else {}),
            **({'weekdays': [0, 1, 2, 3, 4]} if r.random() < .2 else {})}


@pytest.mark.parametrize('seed', range(400))
def test_buckets_say_what_every_pair_says(seed):
    r = random.Random(seed)
    entry = r.choice(['duties_per_month', 'duties_per_week', 'rest_between_duties'])
    policy = made(entry, [random_line(r, i, entry) for i in range(r.randint(1, 14))])
    rule, lines = policy['definition']['rule'], policy['definition']['rule']['lines']
    would = {line['id']: set(r.sample(PEOPLE[:6], r.randint(0, 4))) for line in lines}
    assert R.duplicates(rule) == pairwise_duplicates(rule)
    assert R.outranking_by_number(policy) == pairwise_outranking_by_number(policy)
    assert R.both_apply(policy) == pairwise_both_apply(policy)
    assert R.inherited(policy) == pairwise_inherited(policy)
    assert R.inherited(policy, would) == pairwise_inherited(policy, would)
    assert _broader_levels(lines, _by_number(rule)) == pairwise_broader_levels(lines, _by_number(rule))


@pytest.mark.parametrize('broad,narrow,wins', [
    ('mandatory', 'medium', False), ('medium', 'mandatory', False),
    ('medium', 'high', True), ('mandatory', None, True), (None, 'medium', True),
])
def test_pairwise_strength_contract_keeps_independent_obligations(broad, narrow, wins):
    lines = [{'id': L(0), 'numbers': {'max': {'value': 5, 'level': broad}}},
             {'id': L(1), 'who': {'conditions': [{'field': 'person_ids', 'values': [PEOPLE[0]]}]},
              'numbers': {'max': {'value': 2, 'level': narrow}}}]
    policy = made('duties_per_week', lines)
    expected = {(0, 'max'): [1]} if wins else {}
    assert pairwise_outranking_by_number(policy) == expected
    assert R.outranking_by_number(policy) == expected


@pytest.mark.parametrize('difference', ['date', 'where', 'safety', 'same_tier'])
def test_pairwise_specificity_never_crosses_date_where_safety_or_tier(difference):
    lines = [{'id': L(0), 'numbers': {'min': {'value': 11, 'level': 'mandatory'}},
              'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
             {'id': L(1), 'who': {'conditions': [{'field': 'person_ids', 'values': [PEOPLE[0]]}]},
              'numbers': {'min': {'value': 12, 'level': 'mandatory'}}}]
    if difference == 'date':
        lines[1]['starts_on'] = '2026-11-01'
    elif difference == 'where':
        lines[1]['where'] = {'conditions': [{'field': 'service_ids', 'values': [L(5)]}]}
    elif difference == 'same_tier':
        lines[0]['who'] = lines[1]['who']
    policy = made('rest_between_duties' if difference == 'safety' else 'duties_per_week', lines)
    assert pairwise_outranking_by_number(policy) == {}
    assert R.outranking_by_number(policy) == {}


def department_rule(people=899):
    """1,000 lines of one rule in one where: everyone, 100 ranks and tags, 899 person lines (people after the 800th: a later year)."""
    q = lambda a, b: {'starts_on': a, 'ends_on': b}
    lines = [{'id': L(0), 'numbers': {'min': {'value': 2, 'level': 'mandatory'}, 'max': {'value': 8, 'level': 'mandatory'}}, **q('2026-01-01', '2027-01-01')}]
    lines += [{'id': L(1 + i), 'who': {'conditions': [{'field': 'category_ids', 'values': [rank]}]},
               'numbers': {'min': {'value': 1 + i % 3, 'level': None}}, **q('2026-01-01', '2027-01-01')} for i, rank in enumerate(RANKS)]
    lines += [{'id': L(101 + i), 'who': {'conditions': [{'field': 'person_ids', 'values': [PEOPLE[i % 800]]}]},
               'numbers': {'max': {'value': 3 + i % 4, 'level': None}}, **q(f'{2026 + i // 800}-01-01', f'{2027 + i // 800}-01-01')}
              for i in range(people)]
    policy = made('duties_per_month', lines)
    would = {line['id']: {PEOPLE[(n - 101) % 800]} if n > 100 else set(PEOPLE[(n * 8) % 800:(n * 8) % 800 + 40]) if n else set(PEOPLE[:800])
             for n, line in enumerate(lines)}
    return policy, would


def checked(policy, would):
    began = time.process_time()
    found = (R.duplicates(policy['definition']['rule']), R.outranking_by_number(policy), R.both_apply(policy), R.inherited(policy, would),
             R.attention([policy], would, '2026-10-04'))
    return time.process_time() - began, found


@pytest.fixture(scope='module')
def department_rule_checks():
    policy, would = department_rule()
    elapsed, found = checked(policy, would)
    print(f'line checks: {elapsed:.2f} CPU s for 1,000 lines in one where')
    return elapsed, found


def test_one_thousand_lines_in_one_where_are_fast(department_rule_checks):
    _, found = department_rule_checks
    duplicates, beaten, both, got, attention = found
    assert duplicates == [] and len(both) == 100 * 99 // 2  # ranks are equal tiers: each pair both applies
    assert beaten[(0, 'max')] == list(range(101, 901)) and beaten[(0, 'min')] == list(range(1, 101))
    assert got[L(101)] == {'min': {'value': 1, 'level': None, 'from': L(100)}}  # a person takes the min of the rank holding for them
    assert {i['kind'] for i in attention} == {'both', 'shadowed'}
    assert [i['line_ids'] for i in attention if i['kind'] == 'shadowed'] == [[L(0)]]


@pytest.mark.cpu_budget
def test_one_thousand_lines_in_one_where_are_fast_cpu_budget(department_rule_checks):
    elapsed, _ = department_rule_checks
    # About 0.3 s on a developer machine (0.65 s pairwise). Coverage tracing (the CI pipeline) makes pure-Python work
    # ~7x slower, so the budget scales with it.
    import coverage
    budget = 10 if coverage.Coverage.current() else 1.5
    assert elapsed < budget, (elapsed, budget)
    # Growth: eight times the person lines take about eight times as long (about 6x here; pairwise 10.5x); best of three.
    small, large = (min(checked(*department_rule(people))[0] for _ in range(3)) for people in (250, 2000))
    assert large < 9 * small, (small, large)

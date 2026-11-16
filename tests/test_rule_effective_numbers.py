"""Blank inherits, end to end (docs/design/rule-catalogue.md §2): the numbers the Rules page shows for a person's line
(its own and the ones it inherits, "2↰"), the numbers "Which rule applies?" lists for that person, and the numbers the
validator checks (its quota rows) are the same, case by case."""
import pytest

from packages.domain.validation import evaluate
from .test_rule_catalogue_api import approve, line, person, propose

DATES = {'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}  # the fixture rota's days: one block
WORDS = {'minimum': 'min', 'maximum': 'max', 'target': 'target'}


def n(value, level='mandatory'):
    return {'value': value, 'level': level}


def rank(h):
    return {'conditions': [{'field': 'category_ids', 'values': [h.seed['category']]}]}


# (case, lines as (who, numbers), {person index: the numbers they get})
CASES = [
    ('a person sets only the max: the min of everyone holds for them', [(None, {'min': n(2), 'max': n(5)}), ('a', {'max': n(3)})],
     {0: {'min': 2, 'max': 3}, 1: {'min': 2, 'max': 5}}),
    ('an explicit None removes it', [(None, {'min': n(2), 'max': n(5)}), ('a', {'min': n(None), 'max': n(3)})],
     {0: {'max': 3}, 1: {'min': 2, 'max': 5}}),
    ('the nearest tier that sets it', [(None, {'min': n(2), 'max': n(5)}), ('rank', {'min': n(1)}), ('a', {'max': n(3)})],
     {0: {'min': 1, 'max': 3}, 1: {'min': 1, 'max': 5}}),
    ('a target alone keeps the range of everyone', [(None, {'min': n(2), 'max': n(5)}), ('a', {'target': n(4, 'medium')})],
     {0: {'min': 2, 'max': 5, 'target': 4}, 1: {'min': 2, 'max': 5}}),
]


@pytest.mark.parametrize('case,given,expected', CASES, ids=[c[0] for c in CASES])
def test_shown_listed_and_checked_numbers_are_the_same(h, case, given, expected):
    people = h.seed['people']
    who = {None: None, 'a': person(people[0]), 'rank': rank(h)}
    lines = [line(numbers=numbers, **({'who': who[w]} if w else {}), **DATES) for w, numbers in given]
    check = h.client('coordinator').post('/api/rules/catalogue/duties_per_month/check', json={'lines': lines}).json()
    authored = approve(h, propose(h, 'duties_per_month', lines, acknowledged=[o['line_id'] for o in check['overlaps']])['record'])
    entry = next(e for e in h.get('auditor', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_month')
    rows = evaluate(h.snapshot(h.workspace()))['quota_report']['rows']
    for index, numbers in expected.items():
        p = people[index]
        # Shown: the most specific line naming them, its own numbers and those it inherits.
        mine = [w for w in ('a', 'rank', None) if w is None or w == 'rank' or index == 0]
        shown_line = next(lines[i] for w in mine for i, (x, _) in enumerate(given) if x == w)
        shown = {k: v['value'] for k, v in entry['inherited'].get(shown_line['id'], {}).items()}
        shown.update({k: v['value'] for k, v in shown_line['numbers'].items() if v['value'] is not None})
        applies = h.get('coordinator', f'/api/policies/applies?person_id={p}&day=2026-10-05')['rules']
        [seeded] = [r for r in applies if r['policy_id'] != authored['id']]
        assert seeded['rule']['metric'] == 'workload' and seeded['own_line']['slot'] == 'apply'
        listed = {r['own_line']['slot']: r['rule']['amount'] for r in applies if r['policy_id'] == authored['id']}
        checked = {WORDS[r['comparison']]: r['amount'] for r in rows if r['person_id'] == p}
        assert shown == listed == checked == numbers, (case, index, shown, listed, checked)

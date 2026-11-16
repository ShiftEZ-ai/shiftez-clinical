"""Blank inherits (owner decision 2026-10-04, docs/design/rule-catalogue.md §3): a more specific line replaces only the
numbers it sets; a blank number (or a blank strength) comes from the nearest broader line that sets it; an explicit None
removes it. One table through `outranking_by_number` and `inherited`, then the copies every check reads (before/after)."""
import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain.conditions import OUTRANKED, active_rules
from packages.domain.rule_lines import inherited, outranking, outranking_by_number

A, B, RANK = '00000000-0000-4000-9000-00000000000a', '00000000-0000-4000-9000-00000000000b', '00000000-0000-4000-9000-000000000001'
BLOCK = {'starts_on': '2026-10-01', 'ends_on': '2027-01-01'}
EVERYONE = {A, B, '00000000-0000-4000-9000-00000000000c'}


def person(*people):
    return {'conditions': [{'field': 'person_ids', 'values': list(people)}]}


def rank():
    return {'conditions': [{'field': 'category_ids', 'values': [RANK]}]}


def L(i):
    """Line i's id ('l0' in the table)."""
    return f'00000000-0000-4000-8000-{i:012d}'


def made(entry, lines):
    lines = [{'id': L(n), **line} for n, line in enumerate(lines)]
    return {'id': 'rule', 'name': entry, 'kind': 'catalogue', 'status': 'active', 'active': True, 'department_id': 'd', 'revision': 1,
            'starts_on': '2026-01-01', 'ends_on': None, 'definition': {'rule': {'kind': 'catalogue', 'id': entry, 'settings': {}, 'lines': lines}}}


def n(value, level='mandatory'):
    return {'value': value, 'level': level}


# (case, entry, lines, who each line holds for today, {(line, number): winners}, {line: {number: (value, from)}})
CASES = [
    ('a person line sets only the max: the min of everyone is inherited', 'duties_per_month',
     [{'numbers': {'min': n(2), 'max': n(5)}, **BLOCK}, {'who': person(A), 'numbers': {'max': n(3)}, **BLOCK}], [EVERYONE, {A}],
     {(0, 'max'): [1]}, {'l1': {'min': (2, 'l0')}}),
    ('an explicit None removes the min for that person', 'duties_per_month',
     [{'numbers': {'min': n(2), 'max': n(5)}, **BLOCK}, {'who': person(A), 'numbers': {'min': n(None), 'max': n(3)}, **BLOCK}], [EVERYONE, {A}],
     {(0, 'max'): [1], (0, 'min'): [1]}, {}),
    ('the nearest tier that sets it: a rank between the person and everyone', 'duties_per_month',
     [{'numbers': {'min': n(2), 'max': n(5)}, **BLOCK}, {'who': rank(), 'numbers': {'min': n(1)}, **BLOCK},
      {'who': person(A), 'numbers': {'max': n(3)}, **BLOCK}], [EVERYONE, {A, B}, {A}],
     {(0, 'max'): [2], (0, 'min'): [1]}, {'l1': {'max': (5, 'l0')}, 'l2': {'min': (1, 'l1')}}),
    ('a rank that does not cover the person: everyone it is', 'duties_per_month',
     [{'numbers': {'min': n(2), 'max': n(5)}, **BLOCK}, {'who': rank(), 'numbers': {'min': n(1)}, **BLOCK},
      {'who': person(A), 'numbers': {'max': n(3)}, **BLOCK}], [EVERYONE, {B}, {A}],
     {(0, 'max'): [2], (0, 'min'): [1]}, {'l1': {'max': (5, 'l0')}, 'l2': {'min': (2, 'l0')}}),
    ('the same numbers on both lines: the whole line is replaced, as before', 'duties_per_week',
     [{'numbers': {'max': n(5)}}, {'who': person(A), 'numbers': {'max': n(3)}}], [EVERYONE, {A}], {(0, 'max'): [1]}, {}),
    ('safety: never outranked, every line applies and the strictest binds', 'rest_between_duties',
     [{'numbers': {'min': n(11)}}, {'who': person(A), 'numbers': {'min': n(None)}}], [EVERYONE, {A}], {}, {}),
    ('quota blocks that never meet: nothing is replaced', 'duties_per_month',
     [{'numbers': {'max': n(5)}, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
      {'who': person(A), 'numbers': {'max': n(2)}, 'starts_on': '2026-12-01', 'ends_on': '2027-01-01'}], [EVERYONE, {A}], {}, {}),
    # dev data (rc.9 compare): a daily duty limit for everyone beside a person's block quota target — different measures in
    # the same where, so the target line inherits no maximum (the daily limit applies on its own) and nothing is replaced.
    ('another measure in the same where: nothing inherited', 'duties_per_period',
     [{'numbers': {'max': n(1)}, 'settings': {'max_duties': {'window': 'day'}}},
      {'who': person(A), 'numbers': {'target': n(16, 'medium')}, 'settings': {'block_quota': BLOCK}}], [EVERYONE, {A}], {}, {}),
    ('the same measure in the same where: the block maximum is inherited', 'duties_per_period',
     [{'numbers': {'min': n(10), 'max': n(20)}, 'settings': {'block_quota': BLOCK}},
      {'who': person(A), 'numbers': {'target': n(16, 'medium')}, 'settings': {'block_quota': BLOCK}}], [EVERYONE, {A}], {},
     {'l1': {'min': (10, 'l0'), 'max': (20, 'l0')}}),
    ('another where: both apply, nothing inherited', 'duties_per_week',
     [{'numbers': {'max': n(5)}}, {'who': person(A), 'where': {'conditions': [{'field': 'service_ids', 'values': ['s']}]}, 'numbers': {'max': n(2)}}],
     [EVERYONE, {A}], {}, {}),
]


@pytest.mark.parametrize('case,entry,lines,reach,by_number,expected', CASES, ids=[c[0] for c in CASES])
def test_a_blank_number_is_inherited_and_a_set_one_replaces(case, entry, lines, reach, by_number, expected):
    row = made(entry, lines)
    assert outranking_by_number(row) == by_number
    assert outranking(row) == {line: sorted({w for (m, _), ws in by_number.items() if m == line for w in ws}) for line in {m for m, _ in by_number}}
    got = inherited(row, {L(i): people for i, people in enumerate(reach)})
    short = {L(i): f'l{i}' for i in range(len(lines))}
    assert {short[k]: {x: (v['value'], short[v['from']]) for x, v in numbers.items()} for k, numbers in got.items()} == expected
    PolicyDefinition.model_validate(row['definition'])  # an explicit None is a valid number


def own_exclusions(copies, line, slot):
    """Who a copy of a line leaves out: the people of person lines (a quota's block) and the outranking lines."""
    c = next(c for c in copies if c['template_line']['id'] == L(int(line[1:])) and c['template_line']['slot'] == slot)
    tree = c['definition']['scope'].get('conditions') or {'conditions': []}
    return sorted(v.rsplit(':', 1)[-1] if x['field'] == OUTRANKED else v for x in tree['conditions']
                  if 'conditions' not in x and x['op'] == 'none' and x['field'] in ('person_ids', OUTRANKED) for v in x['values'])


def test_the_copies_every_check_reads_before_and_after():
    """Before (a whole line replaced, as an explicit None for each number read) and after (blank inherits): the only
    difference is the minimum for everyone, which now holds for the person whose line leaves it blank."""
    blank = made('duties_per_month', [{'numbers': {'min': n(2), 'max': n(5)}, **BLOCK}, {'who': person(A), 'numbers': {'max': n(3)}, **BLOCK}])
    whole = made('duties_per_month', [{'numbers': {'min': n(2), 'max': n(5)}, **BLOCK}, {'who': person(A), 'numbers': {'min': n(None), 'max': n(3)}, **BLOCK}])
    after, before = active_rules([blank]), active_rules([whole])
    assert [c['template_line']['slot'] for c in after] == [c['template_line']['slot'] for c in before] == ['min', 'max', 'max']
    assert own_exclusions(before, 'l0', 'min') == [A] and own_exclusions(after, 'l0', 'min') == []
    assert own_exclusions(before, 'l0', 'max') == own_exclusions(after, 'l0', 'max') == [A]
    assert [c['definition']['rule'].get('amount') for c in after if c['template_line']['id'] == L(1)] == [3]


def test_a_blank_strength_comes_from_the_broader_line():
    row = made('duties_per_week', [{'numbers': {'max': n(5, 'high')}}, {'who': person(A), 'numbers': {'max': n(3, None)}}])
    mine = next(c for c in active_rules([row]) if c['template_line']['id'] == L(1))
    assert (mine['definition']['severity'], mine['definition']['strength']) == ('advisory', 'high')
    alone = made('duties_per_week', [{'who': person(A), 'numbers': {'max': n(3, None)}}])
    assert active_rules([alone])[0]['definition']['severity'] == 'mandatory'  # nothing to take: mandatory where it may be


def test_a_line_splits_where_only_some_of_its_numbers_are_replaced():
    """A run's shortest and longest run are one check; when a narrower line sets only the shortest, the line for everyone
    checks its shortest run for everyone else and its longest run for everyone (that person included)."""
    row = made('days_in_a_row', [{'numbers': {'min': n(2, 'medium'), 'max': n(5, 'medium')}}, {'who': person(A), 'numbers': {'min': n(3, 'medium')}}])
    copies = active_rules([row])
    assert [(c['template_line']['id'], c['template_line']['slot']) for c in copies] == [(L(0), 'min'), (L(1), 'min'), (L(0), 'max')]
    assert own_exclusions(copies, 'l0', 'min') == [L(1)] and own_exclusions(copies, 'l0', 'max') == []

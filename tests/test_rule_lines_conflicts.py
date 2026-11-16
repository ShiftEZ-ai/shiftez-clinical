"""Which catalogue lines are the same line, which outranks which and which both apply (docs/design/rule-catalogue.md
§1, §3): one table of cases through `rule_lines.duplicates`, `outranking` and `both_apply`, the readers the check
endpoint, rule.propose and the resolver share."""
import pytest

from packages.domain.conditions import OUTRANKED, active_rules
from packages.domain.rule_lines import both_apply, duplicates, line_relations, outranking

A, B, RANK, CLINIC, OTHER = (f'00000000-0000-4000-9000-00000000000{n}' for n in range(5))
WEEK = {'max_duties': {'window': 'week', 'window_days': 7, 'include_overtime': True, 'group_by': 'all'}}  # as the migration writes it


def person(*people):
    return {'conditions': [{'field': 'person_ids', 'values': list(people)}]}


def rank(match='all'):
    return {'match': match, 'conditions': [{'field': 'category_ids', 'values': [RANK]}]}


def tag():
    return {'conditions': [{'field': 'tags', 'values': ['exceptional_trainee']}]}


def clinic(c=CLINIC, match='all'):
    return {'match': match, 'conditions': [{'field': 'service_ids', 'values': [c]}]}


def most(n):
    return {'max': {'value': n}}


def block(start, end):
    return {'block_quota': {'starts_on': start, 'ends_on': end, 'window_alignment': 'calendar_week'}}


# (case, entry, rule settings, lines (without ids), duplicates, outranking, both apply)
CASES = [
    ('identical lines', 'duties_per_week', None, [{'numbers': most(5)}, {'numbers': most(4)}], [(0, 1)], {}, []),
    ('the same person line twice', 'duties_per_week', None, [{'who': person(A), 'numbers': most(3)}, {'who': person(A), 'numbers': most(2)}],
     [(0, 1)], {}, []),
    ('other days', 'duties_per_week', None, [{'numbers': most(5), 'weekdays': [0, 1]}, {'numbers': most(5), 'weekdays': [5, 6]}], [], {}, []),
    ('every day written out is every day', 'duties_per_week', None,
     [{'numbers': most(5), 'weekdays': list(range(7))}, {'numbers': most(4)}], [(0, 1)], {}, []),
    ('other hours', 'duties_per_week', None, [{'numbers': most(5), 'start_time': '08:00', 'end_time': '16:00'}, {'numbers': most(5)}], [], {}, []),
    ('dates one after the other', 'duties_per_week', None,
     [{'numbers': most(5), 'ends_on': '2026-11-01'}, {'numbers': most(4), 'starts_on': '2026-11-01'}], [], {}, []),
    ('dates partly overlapping', 'duties_per_week', None,
     [{'numbers': most(5), 'ends_on': '2026-11-02'}, {'numbers': most(4), 'starts_on': '2026-11-01'}], [(0, 1)], {}, []),
    ('a day and a week measure different things', 'duties_per_period', None,
     [{'numbers': most(5), 'settings': {'max_duties': {}}}, {'who': person(A), 'numbers': most(1), 'settings': {'max_duties': {'window': 'day'}}}],
     [], {}, []),
    ('other settings measure different things', 'duties_per_week', None,
     [{'numbers': most(5)}, {'who': person(A), 'numbers': most(3), 'settings': {'max_duties': {'include_overtime': False}}}], [], {}, []),
    ('a person outranks everyone on the same where', 'duties_per_week', None,
     [{'numbers': most(5)}, {'who': person(A), 'numbers': most(3)}], [], {0: [1]}, []),
    ('a person outranks a rank, which outranks everyone', 'duties_per_week', None,
     [{'numbers': most(5)}, {'who': rank(), 'numbers': most(4)}, {'who': person(A), 'numbers': most(3)}], [], {0: [1, 2], 1: [2]}, []),
    ('another where: both apply', 'duties_per_week', None,
     [{'numbers': most(5)}, {'who': person(A), 'where': clinic(), 'numbers': most(2)}], [], {}, []),
    ('two clinics: each where its own', 'duties_per_week', None,
     [{'where': clinic(), 'numbers': most(5)}, {'who': person(A), 'where': clinic(OTHER), 'numbers': most(2)}], [], {}, []),
    ('one condition reads the same under all and any', 'duties_per_week', None,
     [{'where': clinic(), 'numbers': most(5)}, {'who': person(A), 'where': clinic(match='any'), 'numbers': most(2)}], [], {0: [1]}, []),
    ('a rank and a tag: equal tiers both apply', 'duties_per_week', None,
     [{'who': rank(), 'numbers': most(4)}, {'who': tag(), 'numbers': most(6)}], [], {}, [(0, 1)]),
    ('two people: equal tiers that never meet', 'duties_per_week', None,
     [{'who': person(A), 'numbers': most(4)}, {'who': person(B), 'numbers': most(6)}], [], {}, []),
    ('two lines naming one person: both apply', 'duties_per_week', None,
     [{'who': person(A, B), 'numbers': most(4)}, {'who': person(A), 'numbers': most(6), 'weekdays': list(range(7))}], [], {}, [(0, 1)]),
    ('a minimum and a maximum of equal tier never compete', 'duties_per_period', {'block_quota': {'starts_on': '2026-10-04', 'ends_on': '2026-11-01'}},
     [{'who': rank(), 'numbers': {'min': {'value': 1}}}, {'who': tag(), 'numbers': most(3)}], [], {}, []),
    ('safety: every line applies', 'people_per_session', None,
     [{'numbers': {'min': {'value': 2}}}, {'who': rank(), 'numbers': {'min': {'value': 1}}}], [], {}, []),
    ('settings on the rule or on the line read the same', 'duties_per_week', {'max_duties': {'window': 'week'}},
     [{'numbers': most(5)}, {'who': person(A), 'numbers': most(3), 'settings': {'max_duties': {'window': 'week'}}}], [], {0: [1]}, []),
    ('a setting left out is its default', 'duties_per_week', None,
     [{'numbers': most(5), 'settings': WEEK}, {'who': person(A), 'numbers': most(3), 'settings': {'max_duties': {}}}], [], {0: [1]}, []),
    ('a default written out is still the same line', 'duties_per_week', None,
     [{'numbers': most(5), 'settings': WEEK}, {'numbers': most(4), 'settings': {'max_duties': {}}}], [(0, 1)], {}, []),
    ('quota blocks that never meet', 'duties_per_period', None,
     [{'numbers': most(5), 'settings': block('2026-10-04', '2026-11-01')}, {'who': person(A), 'numbers': most(2), 'settings': block('2026-12-06', '2027-01-03')}],
     [], {}, []),
    ('quota blocks that meet', 'duties_per_period', None,
     [{'numbers': most(5), 'settings': block('2026-10-04', '2026-11-01')}, {'who': person(A), 'numbers': most(2), 'settings': block('2026-10-04', '2026-11-01')}],
     [], {0: [1]}, []),
    ('a quota block within the line dates', 'duties_per_period', {'block_quota': {'starts_on': '2026-10-04', 'ends_on': '2026-11-01'}},
     [{'numbers': most(5)}, {'who': person(A), 'numbers': most(2), 'starts_on': '2026-11-01', 'ends_on': '2026-12-01'}], [], {}, []),
]


def made(entry, settings, lines):
    lines = [{'id': f'00000000-0000-4000-8000-{n:012d}', **line} for n, line in enumerate(lines)]
    return {'id': 'rule', 'name': entry, 'kind': 'catalogue', 'status': 'active', 'active': True, 'department_id': 'd', 'revision': 1,
            'starts_on': '2026-01-01', 'ends_on': None,
            'definition': {'rule': {'kind': 'catalogue', 'id': entry, 'settings': settings or {}, 'lines': lines}}}


@pytest.mark.parametrize('case,entry,settings,lines,same,outranks,both', CASES, ids=[c[0] for c in CASES])
def test_same_line_outranking_and_both_apply(case, entry, settings, lines, same, outranks, both):
    rule = made(entry, settings, lines)
    assert duplicates(rule['definition']['rule']) == same
    assert outranking(rule) == outranks
    assert both_apply(rule) == both
    if same:
        return
    # The resolver reads the same answer: a line that gives way carries the lines that win (a quota line a person's own
    # line outranks leaves that person out for the block instead).
    copies = active_rules([rule])
    for c in copies:
        n = c['template_line']['line']
        tree = c['definition']['scope'].get('conditions') or {'conditions': []}
        internal = [x for x in tree['conditions'] if 'conditions' not in x and x['op'] == 'none' and x['field'] in (OUTRANKED, 'person_ids')]
        assert bool(internal) == (n in outranks), (case, n, internal)


def test_relation_effective_numbers_use_the_strictest_limit_when_both_apply():
    rule = made('duties_per_week', None, [
        {'numbers': {'min': {'value': 2}, 'max': {'value': 5}}},
        {'who': person(A), 'where': clinic(), 'numbers': {'min': {'value': 4}, 'max': {'value': 3}}},
    ])
    relation = line_relations(rule)[rule['definition']['rule']['lines'][1]['id']]
    assert relation['both_apply'] and relation['effective_numbers'] == {'min': 4, 'max': 3}


def test_relation_effective_numbers_include_all_applicable_lines():
    rule = made('duties_per_week', None, [
        {'numbers': {'max': {'value': 3}}},
        {'who': rank(), 'where': clinic(OTHER), 'numbers': {'max': {'value': 5}}},
        {'who': person(A), 'where': clinic(), 'numbers': {'max': {'value': 9}}},
    ])
    relation = line_relations(rule)[rule['definition']['rule']['lines'][2]['id']]
    assert relation['both_apply'] and relation['effective_numbers']['max'] == 3


def test_relations_exclude_a_line_whose_dated_population_does_not_reach_the_person():
    rule = made('duties_per_week', None, [
        {'who': rank(), 'numbers': most(5)},
        {'who': person(A), 'numbers': most(3)},
    ])
    first, second = rule['definition']['rule']['lines']
    assert line_relations(rule, {first['id']: {B}, second['id']: {A}}) == {}

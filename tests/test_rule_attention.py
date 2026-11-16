"""Needs attention (docs/design/rule-catalogue.md §8): `rule_lines.attention` reads the rules in force through the same
checks as propose and recommends one fix per issue. One table of cases, then the draft findings, the order and the
department-scale budget."""
import time
import uuid

import pytest

from packages.domain.rule_lines import attention

A, B, C, RANK, TAG = 'a', 'b', 'c', '00000000-0000-4000-9000-000000000001', '00000000-0000-4000-9000-000000000002'
TODAY = '2026-10-04'


def person(*people):
    return {'conditions': [{'field': 'person_ids', 'values': list(people)}]}


def rank():
    return {'conditions': [{'field': 'category_ids', 'values': [RANK]}]}


def tag():
    return {'conditions': [{'field': 'category_ids', 'values': [TAG]}]}


def most(n, level='mandatory'):
    return {'max': {'value': n, 'level': level}}


def made(entry, lines, rid='rule'):
    lines = [{'id': f'{rid}-{n}', 'numbers': {}, **line} for n, line in enumerate(lines)]
    return {'id': rid, 'name': entry, 'kind': 'catalogue', 'status': 'active', 'active': True, 'department_id': 'd', 'revision': 1,
            'starts_on': '2026-01-01', 'ends_on': None, 'definition': {'rule': {'kind': 'catalogue', 'id': entry, 'settings': {}, 'lines': lines}}}


EVERYONE = {A, B, C}
# (case, entry, lines, who each line holds for today, [(kind, line indexes, people, recommended fix, its ops)])
CASES = [
    ('nothing to say', 'duties_per_week', [{'numbers': most(5)}], [EVERYONE], []),
    ('a person outranks everyone: the specific line is intended', 'duties_per_week',
     [{'numbers': most(5)}, {'who': person(A), 'numbers': most(3)}], [EVERYONE, {A}],
     [('overlap', [0, 1], 1, 'acknowledge', [])]),
    ('blank inherits: a rank line setting only the max replaces only the max', 'duties_per_month',
     [{'numbers': {'min': {'value': 2}, 'max': {'value': 5}}, 'starts_on': '2026-10-01', 'ends_on': '2027-01-01'},
      {'who': rank(), 'numbers': {'max': {'value': 4}}, 'starts_on': '2026-10-01', 'ends_on': '2027-01-01'}], [EVERYONE, {A, B}],
     [('overlap', [0, 1], 2, 'acknowledge', [])]),
    ('a person line setting only the max: the min of everyone still holds for them', 'duties_per_month',
     [{'numbers': {'min': {'value': 2}, 'max': {'value': 5}}, 'starts_on': '2026-10-01', 'ends_on': '2027-01-01'},
      {'who': person(A), 'numbers': {'max': {'value': 0}}, 'starts_on': '2026-10-01', 'ends_on': '2027-01-01'}], [EVERYONE, {A}],
     [('overlap', [0, 1], 1, 'acknowledge', []), ('inherits', [1, 0], 1, 'none', [{'line_id': 'rule-1', 'numbers': {'max': {'value': 0}, 'min': {'value': None, 'level': 'mandatory'}}}])]),
    ('an overlap that meets nobody today is not reported', 'duties_per_week',
     [{'numbers': most(5)}, {'who': person(A), 'numbers': most(3)}], [{B}, {A}], []),
    ('equal tiers with the same numbers: keep both, future coverage can differ', 'duties_per_week',
     [{'who': rank(), 'numbers': most(1)}, {'who': tag(), 'numbers': most(1)}], [{A, B}, {A}],
     [('both', [0, 1], 1, 'acknowledge', [])]),
    ('equal tiers with other numbers: keep both, the stricter binds', 'duties_per_week',
     [{'who': rank(), 'numbers': most(1)}, {'who': tag(), 'numbers': most(2)}], [{A, B}, {A}],
     [('both', [0, 1], 1, 'acknowledge', [])]),
    ('the same line twice: keep the first', 'duties_per_week',
     [{'numbers': most(5)}, {'numbers': most(4)}], [EVERYONE, EVERYONE],
     [('duplicate', [0, 1], None, 'remove', [{'line_id': 'rule-1', 'remove': True}])]),
    ('ending within 30 days: let it end', 'duties_per_week',
     [{'numbers': most(5)}, {'where': {'conditions': [{'field': 'service_ids', 'values': [RANK]}]}, 'numbers': most(3), 'ends_on': '2026-11-01'}],
     [EVERYONE, EVERYONE], [('ending', [1], None, 'acknowledge', [])]),
    ('ending later is fine', 'duties_per_week', [{'numbers': most(5), 'ends_on': '2026-11-05'}], [EVERYONE], []),
    ('reaches nobody today: keep it, named people can join or return', 'duties_per_week',
     [{'numbers': most(5)}, {'who': person('gone'), 'where': {'conditions': [{'field': 'service_ids', 'values': [RANK]}]}, 'numbers': most(2)}],
     [EVERYONE, set()], [('nobody', [1], None, 'acknowledge', [])]),
    ('a rank (or programme, rotation, tag) empty today fills again: keep it', 'keep_off',
     [{'who': rank(), 'numbers': {'apply': {'value': None}}}], [set()],
     [('nobody', [0], None, 'acknowledge', [])]),
    ('a line that starts later reaches nobody yet, and that is fine', 'duties_per_week',
     [{'numbers': most(5), 'starts_on': '2026-12-01'}], [set()], []),
]


@pytest.mark.parametrize('case,entry,lines,reach,expected', CASES, ids=[c[0] for c in CASES])
def test_issues_and_their_recommended_fix(case, entry, lines, reach, expected):
    row = made(entry, lines)
    would = {f'rule-{n}': people for n, people in enumerate(reach)}
    found = attention([row], would, TODAY)
    assert [(x['kind'], x['lines'], x['people'], x['fixes'][0]['kind'], x['fixes'][0]['ops']) for x in found] == expected
    for x in found:
        assert sum(f['recommended'] for f in x['fixes']) == 1 and x['fixes'][0]['recommended']
        assert x['line_ids'] == [f'rule-{n}' for n in x['lines']] and x['rule_id'] == 'rule' and x['catalogue_id'] == entry
    assert attention([row], would, TODAY) == found  # deterministic


def test_a_rule_that_is_off_says_nothing():
    row = made('duties_per_week', [{'numbers': most(5)}, {'numbers': most(4)}])
    row['definition']['rule']['on'] = False
    assert attention([row], {}, TODAY) == []


@pytest.mark.parametrize('ends_on', ['2026-10-20', None], ids=['different-date-coverage', 'memberships-can-diverge'])
def test_equal_numbers_and_a_people_subset_today_do_not_prove_a_line_can_be_removed(ends_on):
    row = made('duties_per_week', [
        {'who': rank(), 'numbers': most(1), 'ends_on': ends_on},
        {'who': tag(), 'numbers': most(1)},
    ])
    [both] = [x for x in attention([row], {'rule-0': {A, B}, 'rule-1': {A}}, TODAY) if x['kind'] == 'both']
    assert both['facts'] == {'same_numbers': True, 'covered': False}
    assert both['fixes'][0] == {'kind': 'acknowledge', 'recommended': True, 'ops': []}
    assert both['fixes'][1:] == [
        {'kind': 'remove', 'recommended': False, 'ops': [{'line_id': f'rule-{n}', 'remove': True}], 'line_id': f'rule-{n}'}
        for n in (0, 1)
    ]
    # On the first day the broader membership or line no longer holds, the other line still has a purpose.
    later = attention([row], {'rule-0': set(), 'rule-1': {A, C}}, '2026-10-20')
    assert not any(x['kind'] == 'both' or 'rule-1' in x['line_ids'] for x in later)


def test_a_named_person_can_join_later_without_changing_the_rule():
    row = made('keep_off', [{'who': person(A), 'numbers': {'apply': {'value': None}}}])
    [nobody] = attention([row], {'rule-0': set()}, TODAY)
    assert nobody['kind'] == 'nobody' and nobody['facts'] == {'group': False}
    assert nobody['fixes'] == [
        {'kind': 'acknowledge', 'recommended': True, 'ops': []},
        {'kind': 'remove', 'recommended': False, 'ops': [{'line_id': 'rule-0', 'remove': True}], 'line_id': 'rule-0'},
    ]
    assert attention([row], {'rule-0': {A}}, '2026-11-01') == []


def test_ending_soon_says_the_last_day_and_offers_a_year_more():
    row = made('duties_per_week', [{'numbers': most(5), 'ends_on': '2026-11-01'}])
    [x] = attention([row], {'rule-0': EVERYONE}, TODAY)
    assert x['facts'] == {'last_day': '2026-10-31', 'days': 27}
    assert x['fixes'][1] == {'kind': 'extend', 'recommended': False, 'ops': [{'line_id': 'rule-0', 'ends_on': '2027-11-01'}]}


def test_a_mandatory_line_with_errors_in_a_draft_blocks_it():
    row = made('duties_per_week', [{'numbers': most(5)}, {'who': rank(), 'numbers': most(3, 'medium'), 'weekdays': [0, 1]}])
    findings = [{'rule_id': 'rule', 'severity': 'error', 'message': 'Duties per week', 'person_id': A, 'workspace_id': 'w', 'workspace': 'October',
                 'detail': {'line': 0}},
                {'rule_id': 'rule', 'severity': 'error', 'message': 'Duties per week', 'person_id': B, 'workspace_id': 'w', 'workspace': 'October'},
                {'rule_id': 'rule', 'severity': 'warning', 'message': 'advisory', 'workspace_id': 'w', 'workspace': 'October', 'detail': {'line': 1}},
                {'rule_id': 'other', 'severity': 'error', 'message': 'not this rule', 'workspace_id': 'w', 'workspace': 'October'}]
    [x] = attention([row], {'rule-0': EVERYONE, 'rule-1': {A, B}}, TODAY, findings)
    assert (x['kind'], x['line_ids'], x['facts']['count'], [d['workspace'] for d in x['facts']['drafts']]) == ('blocks', ['rule-0'], 2, ['October'])
    assert [f['kind'] for f in x['fixes']] == ['resolve', 'advisory']
    assert x['fixes'][0]['workspace_id'] == 'w'
    # The same lines blocking two drafts: one issue, both drafts inside (each can be opened).
    november = [{**f, 'workspace_id': 'n', 'workspace': 'November'} for f in findings]
    [two] = attention([row], {'rule-0': EVERYONE, 'rule-1': {A, B}}, TODAY, findings + november)
    assert two['facts']['workspace_ids'] == ['n', 'w'] and two['facts']['count'] == 4
    assert [(f['kind'], f.get('workspace'), f['recommended']) for f in two['fixes']] == [
        ('resolve', 'November', True), ('resolve', 'October', False), ('advisory', None, False)]
    assert x['fixes'][1]['ops'] == [{'line_id': 'rule-0', 'numbers': {'max': {'value': 5, 'level': 'medium'}}}]
    # A safety rule is never offered as advisory; a finding that names no line (or one no longer there) points at the
    # mandatory lines holding for its people.
    safety = made('rest_between_duties', [{'numbers': {'min': {'value': 11}}}, {'who': person(C), 'numbers': {'min': {'value': 12}}}])
    [y] = attention([safety], {'rule-0': EVERYONE, 'rule-1': {C}}, TODAY, [{**findings[0], 'detail': {'line': 7}}])
    assert y['line_ids'] == ['rule-0'] and [f['kind'] for f in y['fixes']] == ['resolve']


def test_kinds_come_in_one_order_and_rules_in_catalogue_order():
    week = made('duties_per_week', [{'numbers': most(5), 'ends_on': '2026-10-10'}, {'numbers': most(4)}], 'week')
    session = made('people_per_session', [{'numbers': {'max': {'value': 3}}}, {'numbers': {'max': {'value': 2}}}], 'session')
    found = attention([week, session], {f'{r}-{n}': EVERYONE for r in ('week', 'session') for n in (0, 1)}, TODAY)
    assert [(x['kind'], x['rule_id']) for x in found] == [('duplicate', 'session'), ('duplicate', 'week'), ('ending', 'week')]
    assert len({x['id'] for x in found}) == len(found)


@pytest.fixture(scope='module')
def department_attention():
    clinics = [str(uuid.uuid4()) for _ in range(7)]
    people = [str(uuid.uuid4()) for _ in range(150)]
    lines = [{'numbers': most(3 + n % 3), 'who': person(people[n % 150]) if n % 10 else None, 'ends_on': '2026-10-20' if n % 50 == 0 else None,
              'where': {'conditions': [{'field': 'service_ids', 'values': [clinics[n % 7]]}]}} for n in range(1000)]
    row = made('duties_per_week', lines)
    would = {line['id']: set(people) if not line.get('who') else set(line['who']['conditions'][0]['values'])
             for line in row['definition']['rule']['lines']}
    findings = [{'rule_id': 'rule', 'severity': 'error', 'message': 'x', 'workspace_id': 'w', 'workspace': 'W', 'detail': {'line': n}} for n in range(500)]
    times = []
    for _ in range(3):  # the best of three: a collector pause is not the check
        began = time.process_time()
        found = attention([row], would, TODAY, findings)
        times.append(time.process_time() - began)
    return found, times


def test_needs_attention_stays_within_budget_at_department_scale(department_attention):
    found, _ = department_attention
    assert {x['kind'] for x in found} == {'overlap', 'duplicate', 'ending', 'blocks'}


@pytest.mark.cpu_budget
def test_needs_attention_stays_within_budget_at_department_scale_cpu_budget(department_attention):
    import coverage
    _, times = department_attention
    # About 0.1 s on a developer machine; coverage tracing (CI) is several times slower.
    assert min(times) < 0.5 * (6 if coverage.Coverage.current() else 1), times


def test_an_acknowledgement_fingerprint_holds_while_the_issue_stands_and_changes_with_its_lines():
    """rule_lines.fingerprint: stable across versions, days and who it reaches; new when an involved line or the numbers
    that give way change; untouched by a change to a line the issue does not involve."""
    from packages.domain.rule_lines import fingerprint
    lines = [{'numbers': most(5)}, {'who': person(A), 'numbers': most(3)},
             {'where': {'conditions': [{'field': 'service_ids', 'values': [RANK]}]}, 'numbers': most(4), 'ends_on': '2026-10-20'}]

    def prints(lines, rid='rule', today=TODAY, reach=(EVERYONE, {A}, EVERYONE)):
        row = made('duties_per_week', lines)
        row['id'] = rid  # another version of the rule
        stored = {line['id']: line for line in row['definition']['rule']['lines']}
        return {x['kind']: fingerprint(x, stored) for x in attention([row], {f'rule-{n}': p for n, p in enumerate(reach)}, today)}
    first = prints(lines)
    assert set(first) == {'overlap', 'ending'} and all(len(f) == 64 for f in first.values())
    assert prints(lines, rid='next-version') == first
    assert prints(lines, today='2026-10-05')['ending'] == first['ending']  # a day later: the days left are not the issue
    assert prints(lines, reach=(EVERYONE, {A, B}, EVERYONE))['overlap'] == first['overlap']  # it reaches more people today
    tighter = prints([lines[0], {**lines[1], 'numbers': most(2)}, lines[2]])
    assert tighter['overlap'] != first['overlap'] and tighter['ending'] == first['ending']  # only the issue whose line changed
    later = prints([lines[0], lines[1], {**lines[2], 'ends_on': '2026-10-25'}])
    assert later['ending'] != first['ending'] and later['overlap'] == first['overlap']

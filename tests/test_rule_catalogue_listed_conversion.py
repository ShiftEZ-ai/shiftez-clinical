"""Listed-only rows can clear Simplify blockers without retiring their rule history."""
from copy import deepcopy
from uuid import UUID

import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain.rule_catalogue_migration import after_snapshot, lines_of, plan_v2
from packages.domain.rule_migration_proof import canonical_full_model, same_full_model, strict_proof
from .test_rule_catalogue_v2_domain import effective
from .test_rule_catalogue_v2_proof import fixture
from .test_rule_simplification import quota, rule


def clinical_target(*, inherited=()):
    people = [str(UUID(int=10000 + n)) for n in range(36)]
    source = rule('Synthetic weekly clinical target',
                  quota('2026-10-04', '2026-10-11', 7, window_alignment='calendar_week'),
                  severity='advisory', listed_only=True,
                  per_person=[{'person_id': person, 'amount': None if n in inherited else 5 + n % 4}
                              for n, person in enumerate(people)])
    source['definition'] = PolicyDefinition.model_validate(source['definition'], context={'historical': True}).model_dump(mode='json')
    return source


def test_all_36_listed_rows_with_their_own_numbers_convert_without_a_frozen_default():
    source = clinical_target()
    plan = plan_v2([source], department_id='d')
    assert plan['blocked'] == []
    assert len(plan['scheduling_facts']) == 36
    assert not any(fact['data']['line'].get('source_default') for fact in plan['scheduling_facts'])
    before = {'policies': [source]}
    assert effective(after_snapshot(before, plan)) == effective(before)


def test_programme_scoped_inherited_rows_become_bound_catalogue_lines():
    programme = str(UUID(int=90000))
    source = clinical_target(inherited=tuple(range(2)))
    for row in source['definition']['per_person']:
        row['programme_id'] = programme
    source['definition'] = PolicyDefinition.model_validate(source['definition'], context={'historical': True}).model_dump(mode='json')

    plan = plan_v2([source], department_id='d')

    assert not plan['blocked'], plan['blocked']
    assert len(plan['scheduling_facts']) == 34
    target = next(item for item in plan['targets'] if item['catalogue_id'] == 'workload_limit')
    inherited = [line for line in target['payload']['definition']['rule']['lines']
                 if line['numbers'].get('target', {}).get('value') == 7]
    assert len(inherited) == 2
    assert all(line['who']['conditions'][0]['field'] == 'programme_ids' for line in inherited)
    assert all('source_default' not in line for line in inherited)

    before = {'policies': [source]}
    after = after_snapshot(before, plan)
    def runtime(data):
        rows = effective(data)
        for row in rows:
            row.get('template_line', {}).pop('amount', None)
        return rows
    assert runtime(after) == runtime(before)


@pytest.mark.parametrize('kind,calendar_entry', [('restriction', None), ('restriction', str(UUID(int=500))),
                                               ('allocation_overtime', None)])
def test_listed_apply_rules_convert_without_requiring_an_impossible_number(h, kind, calendar_entry):
    before = fixture(h, people=22, sessions=2)
    people = list(before['people'])
    source = rule('Protected academic time' if kind == 'restriction' else 'Synthetic overtime offers',
                  {'kind': kind}, severity='mandatory' if kind == 'restriction' else 'advisory',
                  calendar_entry_id=calendar_entry, listed_only=True, per_person=[
                      {'person_id': people[0], 'starts_on': '2026-09-01', 'ends_on': '2026-09-18'},
                      {'person_id': people[1], 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}])
    source['definition'] = PolicyDefinition.model_validate(source['definition'], context={'historical': True}).model_dump(mode='json')
    before['policies'] = [source]
    plan = plan_v2([source], department_id='d')
    assert not plan['blocked'], plan['blocked']
    assert len(plan['scheduling_facts']) == 2
    assert all(not fact['data']['line'].get('source_default') for fact in plan['scheduling_facts'])
    assert all(set(fact['data']['line']['numbers']) == {'apply'} for fact in plan['scheduling_facts'])
    after = after_snapshot(before, plan)
    assert effective(after) == effective(before)
    proof = strict_proof(before, after)
    assert proof['identical'], proof


@pytest.mark.parametrize('comparison', ['minimum', 'maximum', 'range', 'target'])
@pytest.mark.parametrize('inherited', [None, 0, 1])
def test_listed_zero_and_ended_rows_preserve_numbers_and_target_only_inherited_rows(comparison, inherited):
    rows = [{'person_id': str(UUID(int=15000)), 'amount': 0, 'starts_on': '2026-10-01', 'ends_on': '2026-10-03'},
            {'person_id': str(UUID(int=15001)), 'amount': 1}]
    if inherited is not None:
        rows[inherited]['amount'] = None
    source = rule('Synthetic zero and ended quotas', quota('2026-10-01', '2026-10-11', 1, comparison,
                  'duties', **({'maximum_amount': 8} if comparison == 'range' else {})),
                  severity='advisory' if comparison == 'target' else 'mandatory', listed_only=True, per_person=rows)
    source['definition'] = PolicyDefinition.model_validate(source['definition'], context={'historical': True}).model_dump(mode='json')
    plan = plan_v2([source], department_id='d')
    if inherited is not None:
        [blocker] = plan['blocked']
        assert blocker['person_ids'] == [rows[inherited]['person_id']]
        assert blocker['line_ids'] == [lines_of(source)[1][inherited]['id']]
    else:
        assert not plan['blocked']
        before = {'policies': [source]}
        assert effective(after_snapshot(before, plan)) == effective(before)
        assert any(number['value'] == 0 for fact in plan['scheduling_facts']
                   for number in fact['data']['line']['numbers'].values())


@pytest.mark.parametrize('inherited', [(0,), (8, 24)])
def test_blocker_targets_only_rows_still_missing_their_number_and_clears_after_edit(inherited):
    source = clinical_target(inherited=inherited)
    plan = plan_v2([source], department_id='d')
    [blocker] = plan['blocked']
    assert blocker['rule_id'] == source['id']
    assert 'inherited default' in blocker['reason']
    assert blocker['person_ids'] == sorted(source['definition']['per_person'][n]['person_id'] for n in inherited)
    assert blocker['line_ids'] == [lines_of(source)[1][n]['id'] for n in inherited]
    with pytest.raises(ValueError, match='blocked'):
        after_snapshot({'policies': [source]}, plan)
    revised = deepcopy(source)
    for n in inherited:
        revised['definition']['per_person'][n]['amount'] = 7
    revised['revision'] += 1
    cleared = plan_v2([revised], department_id='d')
    assert not cleared['blocked']
    before = {'policies': [revised]}
    assert effective(after_snapshot(before, cleared)) == effective(before)


def test_explicit_listed_rows_keep_inherited_settings_when_first_fact_changes_or_retires():
    source = rule('Synthetic explicit weekly caps', {'kind': 'max_duties', 'maximum': 7, 'window': 'week'},
                  listed_only=True, per_person=[
                      {'person_id': str(UUID(int=11000)), 'amount': 3},
                      {'person_id': str(UUID(int=11001)), 'amount': 4},
                      {'person_id': str(UUID(int=11002)), 'amount': 5},
                  ])
    source['definition'] = PolicyDefinition.model_validate(source['definition'], context={'historical': True}).model_dump(mode='json')
    plan = plan_v2([source], department_id='d')
    assert not plan['blocked']
    before = {'policies': [source]}
    after = after_snapshot(before, plan)
    assert effective(after) == effective(before)
    first = after['scheduling_facts'][0]
    first['data']['line']['numbers']['max']['value'] = 2
    first['data']['line']['settings']['max_duties']['window'] = 'day'
    revised = deepcopy(source)
    revised['definition']['per_person'][0]['amount'] = 2.0
    expected = effective({'policies': [revised]})
    expected[0]['definition']['rule']['window'] = 'day'
    assert effective(after) == expected
    first['status'] = 'retired'
    assert effective(after) == effective(before)[1:]


@pytest.mark.parametrize('target', [None, 4])
def test_explicit_listed_range_rows_keep_each_resolved_maximum_and_target(target):
    source = rule('Synthetic listed ranges',
                  quota('2026-10-04', '2026-10-11', 1, 'range', 'duties', maximum_amount=8, target_amount=target),
                  listed_only=True, per_person=[
                      {'person_id': str(UUID(int=12000)), 'amount': 2, 'settings': {'maximum_amount': 6, 'target_amount': 3}},
                      {'person_id': str(UUID(int=12001)), 'amount': 2},
                  ])
    source['definition'] = PolicyDefinition.model_validate(source['definition'], context={'historical': True}).model_dump(mode='json')
    plan = plan_v2([source], department_id='d')
    assert not plan['blocked']
    before = {'policies': [source]}
    after = after_snapshot(before, plan)
    assert effective(after) == effective(before)
    after['scheduling_facts'][0]['status'] = 'retired'
    assert effective(after) == [copy for copy in effective(before)
                               if copy['template_line']['person_id'] == str(UUID(int=12001))]


@pytest.mark.parametrize('distinct_dates', [False, True])
def test_listed_rows_validate_their_own_ranges_and_blocks_before_strict_proof(h, distinct_dates):
    before = fixture(h, people=22, sessions=2)
    people = list(before['people'])
    rows = [{'person_id': people[0], 'amount': 2, 'settings': {'maximum_amount': 6}},
            {'person_id': people[1], 'amount': 7}]
    if distinct_dates:
        rows[0].update(starts_on='2026-10-01', ends_on='2026-10-03')
        rows[1].update(starts_on='2026-10-04', ends_on='2026-10-07')
    source = rule('Synthetic independent listed blocks',
                  quota('2026-10-01', '2026-10-11', 1, 'range', 'duties', maximum_amount=8),
                  listed_only=True, per_person=rows)
    source['definition'] = PolicyDefinition.model_validate(source['definition'], context={'historical': True}).model_dump(mode='json')
    before['policies'] = [source]
    plan = plan_v2([source], department_id='d')
    assert not plan['blocked']
    after = after_snapshot(before, plan)
    proof = strict_proof(before, after)
    assert proof['identical'], proof
    # Editing either row's own number must still preserve the complete strict proof.
    revised = deepcopy(before)
    revised['policies'][0]['definition']['per_person'][1]['amount'] = 8.0
    after['scheduling_facts'][1]['data']['line']['numbers']['min']['value'] = 8
    proof = strict_proof(revised, after)
    assert proof['identical'], proof


def test_clearing_first_bound_target_preserves_later_target_and_can_restore_it(h):
    before = fixture(h, people=22, sessions=2)
    people = list(before['people'])
    source = rule('Synthetic independent listed targets',
                  quota('2026-10-01', '2026-10-11', 1, 'range', 'duties', maximum_amount=8, target_amount=4),
                  listed_only=True, per_person=[
                      {'person_id': people[0], 'amount': 2, 'settings': {'maximum_amount': 6, 'target_amount': 3}},
                      {'person_id': people[1], 'amount': 2},
                  ])
    source['definition'] = PolicyDefinition.model_validate(source['definition'], context={'historical': True}).model_dump(mode='json')
    before['policies'] = [source]
    after = after_snapshot(before, plan_v2([source], department_id='d'))
    assert strict_proof(before, after)['identical']
    first, second = after['scheduling_facts']
    first['data']['line']['numbers']['target']['value'] = None
    expected = [copy for copy in effective(before)
                if not (copy['template_line']['person_id'] == people[0] and copy['definition']['rule']['comparison'] == 'target')]
    for copy in expected:
        if copy['template_line']['person_id'] == people[0]:
            copy['template_line']['settings']['target_amount'] = None
    assert effective(after) == expected
    # A legacy representation needs an explicit second-row target after clearing its default;
    # the model must match while v2 retains the original source row's provenance above.
    revised = deepcopy(before)
    revised['policies'][0]['definition']['rule']['target_amount'] = None
    revised['policies'][0]['definition']['per_person'][0]['settings']['target_amount'] = None
    revised['policies'][0]['definition']['per_person'][1]['settings'] = {'target_amount': 4}
    assert same_full_model(canonical_full_model(revised), canonical_full_model(after))['equal']
    second['data']['line']['numbers']['target']['value'] = None
    assert not any(copy['definition']['rule']['comparison'] == 'target' for copy in effective(after))
    second['data']['line']['numbers']['target']['value'] = 4
    assert effective(after) == expected


@pytest.mark.parametrize(('comparison', 'key'), [('minimum', 'min'), ('maximum', 'max'), ('target', 'target'),
                                               ('range', 'min'), ('range', 'max')])
@pytest.mark.parametrize('row_index', [0, 1])
def test_cleared_listed_bounds_emit_no_constraint_and_restore_without_a_default(comparison, key, row_index):
    from packages.domain.rule_lines import fact_policy
    from packages.domain.solver_rules import definition_issues
    extra = {'maximum_amount': 8} if comparison == 'range' else {}
    if comparison != 'target':
        extra['target_amount'] = 4
    source = rule('Synthetic independently clearable listed bounds',
                  quota('2026-10-01', '2026-10-11', 8 if comparison == 'maximum' else 1, comparison, 'duties', **extra),
                  severity='advisory' if comparison == 'target' else 'mandatory', listed_only=True,
                  per_person=[{'person_id': str(UUID(int=13000 + n)), 'amount': (6 if comparison == 'maximum' else 2) + n}
                              for n in range(2)])
    source['definition'] = PolicyDefinition.model_validate(source['definition'], context={'historical': True}).model_dump(mode='json')
    before = {'policies': [source]}
    after = after_snapshot(before, plan_v2([source], department_id='d'))
    original = effective(before)
    assert effective(after) == original
    fact = after['scheduling_facts'][row_index]
    previous = fact['data']['line']['numbers'][key]['value']
    fact['data']['line']['numbers'][key]['value'] = None
    assert definition_issues(fact_policy(fact)['definition']) == []
    cleared_comparison = {'min': 'minimum', 'max': 'maximum', 'target': 'target'}[key]
    def meanings(copies):
        return [(p['template_line']['person_id'], p['definition']['rule']['comparison'], p['definition']['rule']['amount'])
                for p in copies]
    expected = [row for row in meanings(original)
                if row[:2] != (fact['person_id'], cleared_comparison)]
    assert meanings(effective(after)) == expected
    fact['data']['line']['numbers'][key]['value'] = previous
    assert effective(after) == original
    for fact in after['scheduling_facts']:
        for number in fact['data']['line']['numbers'].values():
            number['value'] = None
    assert effective(after) == []


def test_cleared_explicit_listed_limit_is_omitted_instead_of_borrowing_another_rows_number():
    source = rule('Synthetic clearable listed limit', {'kind': 'max_duties', 'maximum': 8, 'window': 'week'},
                  listed_only=True, per_person=[{'person_id': str(UUID(int=14000 + n)), 'amount': 2 + n} for n in range(2)])
    source['definition'] = PolicyDefinition.model_validate(source['definition'], context={'historical': True}).model_dump(mode='json')
    before = {'policies': [source]}
    after = after_snapshot(before, plan_v2([source], department_id='d'))
    after['scheduling_facts'][0]['data']['line']['numbers']['max']['value'] = None
    assert effective(after) == effective(before)[1:]

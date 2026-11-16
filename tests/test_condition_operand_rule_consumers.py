"""Rule proof, reach and conversion readers share the evaluator's absolute operand interpretation."""
from copy import deepcopy
import importlib
import json

import pytest

from packages.domain import rule_catalogue_migration, rule_groups, rule_lines
from packages.domain.conditions import INHERITED_LEVEL, OUTRANKED, OVERRIDDEN, OWN_LINE, active_rules, scope_matches_throughout
from packages.domain.time import aware


PERSON = '00000000-0000-4000-8000-000000000001'
RELATED = '00000000-0000-4000-8000-000000000002'


def choice(field, values, shape, *, op='any'):
    line = {'field': field, 'op': op}
    if shape == 'legacy':
        return {**line, 'values': values}
    return {**line, 'value': {'kind': 'absolute', 'value': values[0] if shape == 'absolute-single' else values}}


def source_copies():
    scope = {'person_ids': [PERSON]}
    sources = {
        'mine': {'definition': {'scope': scope}, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
                 'whole_person': True, 'tier': 3, 'numbers': {'max': {'level': 'high'}},
                 'inherit': {'max': {'sources': ['other'], 'fallback': 'low'}}},
        'other': {'definition': {'scope': {'tags': ['vip']}}, 'starts_on': '2026-10-05', 'ends_on': '2026-10-20',
                  'whole_person': False, 'tier': 2, 'numbers': {'max': {'level': 'medium'}}},
    }
    return [{'id': 'original', 'template_line': {'person_id': PERSON}, 'starts_on': '2026-10-01',
             'ends_on': '2026-11-01', '_catalogue_sources': sources,
             'definition': {'rule': {'kind': 'max_hours'}, 'scope': scope, 'severity': 'mandatory'}}]


@pytest.mark.parametrize('field,value,op', [
    (OWN_LINE, 'original', 'none'),
    (OUTRANKED, 'mine', 'none'),
    (OVERRIDDEN, 'original', 'none'),
    (INHERITED_LEVEL, 'mine#max#high@2026-10-04@2026-10-11', 'any'),
])
@pytest.mark.parametrize('shape', ['legacy', 'absolute-list', 'absolute-single'])
def test_internal_rule_meaning_and_id_remapping_are_operand_independent(field, value, op, shape):
    copies = source_copies()
    line = choice(field, [value], shape, op=op)
    policy = {**copies[0], 'definition': {**copies[0]['definition'], 'scope': {'conditions': {'conditions': [line]}}}}
    stored = deepcopy(policy)
    result = rule_groups._canonical_copy(policy, copies, {'original': 'replacement'})
    output = result['definition']['scope']['conditions']['conditions'][0]
    if field in {OWN_LINE, OUTRANKED}:
        assert output == {'field': '~replaced_by', 'op': 'none',
                          'values': [json.dumps(['own', [(PERSON, '2026-10-01', '2026-11-01')]])]}
    elif field == OVERRIDDEN:
        assert output == {'field': OVERRIDDEN, 'op': 'none', 'values': ['replacement']}
    else:
        meaning = rule_groups._level_meaning(copies, value)
        assert meaning[4] == ['2026-10-04', '2026-10-11']
        assert meaning[-1] and json.loads(meaning[-1][0])[-1] == 'medium'
        assert output == {'field': '~inherited_level', 'op': 'any', 'values': [json.dumps(meaning, sort_keys=True)]}
    assert policy == stored


@pytest.mark.parametrize('field', [OWN_LINE, OUTRANKED, OVERRIDDEN, INHERITED_LEVEL])
def test_unresolved_internal_operands_are_preserved_in_proofs(field):
    copies = source_copies()
    line = {'field': field, 'op': 'none', 'value': {'kind': 'relative', 'token': 'me'}}
    policy = {**copies[0], 'definition': {**copies[0]['definition'], 'scope': {'conditions': {'conditions': [line]}}}}
    result = rule_groups._canonical_copy(policy, copies, {'original': 'replacement'})
    assert result['definition']['scope']['conditions']['conditions'] == [line]


@pytest.mark.parametrize('field,flat', [('person_ids', 'person_ids'), ('service_ids', 'service_ids'),
                                     ('programme_ids', 'programme_id'), ('rotation_type_ids', 'rotation_type_id')])
@pytest.mark.parametrize('shape', ['legacy', 'absolute-list', 'absolute-single'])
def test_scope_folding_preserves_typed_operands(field, flat, shape):
    atom = choice(field, [PERSON if field == 'person_ids' else RELATED], shape)
    line = {'who' if field in {'person_ids', 'programme_ids', 'rotation_type_ids'} else 'where': {'conditions': [atom]}}
    stored = deepcopy(line)
    scope = rule_lines.scope_of(line)
    if shape == 'legacy':
        assert scope[flat] == ([PERSON if field == 'person_ids' else RELATED] if flat.endswith('_ids') else RELATED)
        assert 'conditions' not in scope
    else:
        assert flat not in scope
        assert scope['conditions']['conditions'] == [atom]
    assert line == stored


@pytest.mark.parametrize('field', ['programme_ids', 'rotation_type_ids'])
@pytest.mark.parametrize('shape', ['legacy', 'absolute-list', 'absolute-single'])
def test_migration_keeps_dated_singleton_conditions_out_of_flat_scopes(field, shape):
    atom = choice(field, [RELATED], shape)
    scope = {'conditions': {'conditions': [atom]}}
    stored = deepcopy(scope)
    who, where = rule_catalogue_migration.who_where(scope)
    assert where is None
    if shape == 'legacy':
        assert who['conditions'] == [{'match': 'all', 'conditions': [atom]}]
    else:
        assert who['conditions'] == [atom]
    converted = rule_lines.scope_of({'who': who})
    assert field[:-1] not in converted
    assert converted['conditions']
    assert scope == stored


@pytest.mark.parametrize('shape', ['legacy', 'absolute-list', 'absolute-single'])
def test_migrated_programme_condition_still_requires_membership_throughout_the_duty(shape):
    from .test_rule_conditions import data

    atom = choice('programme_ids', ['P'], shape)
    scope = {'conditions': {'conditions': [atom]}}
    who, where = rule_catalogue_migration.who_where(scope)
    converted = rule_lines.scope_of({'who': who, 'where': where})
    a, b = aware('2026-09-30T20:00:00+03:00'), aware('2026-10-01T02:00:00+03:00')
    source = data()
    assert not scope_matches_throughout(source, scope, 'p', None, a, b)
    assert not scope_matches_throughout(source, converted, 'p', None, a, b)
    a = aware('2026-10-01T00:00:00+03:00')
    assert scope_matches_throughout(source, scope, 'p', None, a, b)
    assert scope_matches_throughout(source, converted, 'p', None, a, b)


@pytest.mark.parametrize('shape', ['absolute-list', 'absolute-single'])
def test_fact_narrowing_preserves_absolute_operand_representation(shape):
    values = [PERSON, RELATED] if shape == 'absolute-list' else [PERSON]
    line = {'who': {'conditions': [choice('person_ids', values, shape)]}}
    stored = deepcopy(line)
    narrowed = rule_catalogue_migration._fact_line_people(line, {PERSON})
    assert rule_lines.named_people(narrowed) == [PERSON]
    assert line == stored


def test_rule_map_loads_transitive_override_sources_for_absolute_operands(h, monkeypatch):
    from .test_rule_map import EVERY, base, entry, rule

    module = importlib.import_module('packages.domain.rule_map')
    _, data = base(h)
    person, second, third = h.seed['people']
    general = rule(data, 'General', {'kind': 'max_duties', 'maximum': 5})
    middle = rule(data, 'Middle', {'kind': 'max_duties', 'maximum': 4}, {'person_id': person}, overrides=[general['id']])
    middle.update(starts_on='2026-10-05', ends_on='2026-10-09')
    inner = rule(data, 'Inner', {'kind': 'max_duties', 'maximum': 3}, {'person_id': person}, overrides=[middle['id']])
    inner.update(starts_on='2026-10-06', ends_on='2026-10-08')
    template = rule(data, 'Template', {'kind': 'max_duties', 'maximum': 6},
                    per_person=[{'person_id': person, 'amount': 2, 'starts_on': '2026-10-05', 'ends_on': '2026-10-09'}])
    data['policies'] = [general, middle, inner, template]
    baseline = module.rule_map(deepcopy(data))
    assert entry(baseline, 'General')['people'] == {person: [[0, 1], [2, 4], [5, 7]], second: EVERY, third: EVERY}

    for shape in ('absolute-list', 'absolute-single'):
        seen = set()

        def encoded_rules(*args, **kwargs):
            copies = deepcopy(active_rules(*args, **kwargs))

            def encode(tree):
                for node in (tree or {}).get('conditions', []):
                    if 'conditions' in node:
                        encode(node)
                    elif node['field'] in {OVERRIDDEN, OWN_LINE}:
                        seen.add(node['field'])
                        node.update(choice(node['field'], node.pop('values'), shape, op=node.get('op', 'any')))

            for copy in copies:
                encode(copy['definition']['scope'].get('conditions'))
            return copies

        monkeypatch.setattr(module, 'active_rules', encoded_rules)
        assert module.rule_map(deepcopy(data)) == baseline
        assert seen == {OVERRIDDEN, OWN_LINE}

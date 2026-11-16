"""Every literal consumer reads the same legacy, raw and typed absolute operands."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from packages.contracts.schemas import AbsoluteOperand, Condition
from packages.domain import conditions as c
from packages.domain.time import aware
from packages.domain.validation import evaluate as validate


SHAPES = ('legacy', 'raw', 'absolute-single', 'absolute-list')


def test_empty_tree_stays_false_when_the_evaluator_also_accepts_leaves():
    assert c.evaluate({}, lambda field: set()) is False


def choice(field, values, shape, op='any'):
    line = {'field': field, 'op': op}
    if shape == 'legacy':
        return {**line, 'values': values}
    value = values[0] if len(values) == 1 and shape != 'absolute-list' else values
    return {**line, 'value': value if shape == 'raw' else {'kind': 'absolute', 'value': value}}


@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('container', [dict, SimpleNamespace, Condition.model_validate],
                         ids=['dict', 'projection', 'contract'])
def test_shared_literal_reader_accepts_every_condition_representation(shape, container):
    line = choice('tags', ['Resident'], shape)
    original = deepcopy(line)
    model = container(**line) if container is SimpleNamespace else container(line)
    assert c.absolute_values(model) == ['Resident']
    assert line == original


@pytest.mark.parametrize('operand,expected', [
    (0, [0]), (False, [False]), ('', ['']), ([], []),
    (AbsoluteOperand(kind='absolute', value=0), [0]),
    ([{'kind': 'absolute', 'value': '2026-10-01'}, '2026-10-31'], ['2026-10-01', '2026-10-31']),
    (({'kind': 'absolute', 'value': '2026-10-01'}, {'kind': 'absolute', 'value': '2026-10-31'}),
     ['2026-10-01', '2026-10-31']),
    ({'kind': 'relative', 'token': 'me'}, None),
    (['2026-10-01', {'kind': 'relative', 'token': 'today'}], None),
])
def test_shared_literal_reader_keeps_false_and_zero_and_never_resolves_relative(operand, expected):
    assert c.absolute_values({'value': operand}) == expected
    assert c.absolute_values(SimpleNamespace(value=operand)) == expected


def test_repeated_selector_evaluation_keeps_the_winner_index_bounded():
    source = {'definition': {'scope': {'person_ids': ['P']}}, 'starts_on': '2020-01-01'}
    data = {'policies': [{'id': 'rule', '_catalogue_sources': {'source': source}}],
            '_line_index_cache': {}, 'organisation': {'timezone': 'Asia/Riyadh'}}
    holds = c.rule_holds(data, 'P', None, aware('2026-10-04T08:00:00+03:00'), aware('2026-10-04T12:00:00+03:00'))
    tree = {'conditions': [choice(c.OUTRANKED, ['source'], 'legacy')]}
    for _ in range(10):
        assert c.evaluate(tree, holds)
    assert len(c.source_index(data['policies'], data['_line_index_cache'])[3]) == 1


@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('held,expected', [('stra\u00dfe', True), ('strasse', False)])
def test_tag_literal_matching_keeps_legacy_unicode_normalization(shape, held, expected):
    line = choice('tags', ['Stra\u00dfe'], shape)
    assert c.evaluate({'conditions': [line]}, lambda field: {held}) is expected
    legacy = choice('tags', ['Stra\u00dfe'], 'legacy')
    assert c._covers(line, legacy)
    assert c._covers(legacy, line)


@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('consumer', ['tag_scope', 'necessary_people', 'evaluate_internal', 'implication',
                                    'bucket_visitors', 'bind_level_window'])
def test_literal_consumer_case_table(shape, consumer):
    if consumer == 'tag_scope':
        data = {'tags': {'T': {'name': 'Resident', 'members': {'people': ['P']}}}}
        scope = {'conditions': {'conditions': [choice('person_ids', ['T'], shape, 'all')]}}
        result = c.tag_scope(data, scope)
        [line] = result['conditions']['conditions']
        assert c.absolute_values(line) == ['P']
        assert c.evaluate(result['conditions'], lambda field: {'P'})
        if shape != 'legacy':
            assert 'values' not in line
    elif consumer == 'necessary_people':
        assert list(c.necessary_person_values({'conditions': [choice('person_ids', ['P'], shape)]})) == [['P']]
    elif consumer == 'evaluate_internal':
        calls = []
        def holds(field, values):
            calls.append((field, values))
            return {'source'}
        line = choice(c.OUTRANKED, ['source'], shape, 'none' if shape == 'legacy' else 'is_not')
        assert not c.evaluate({'conditions': [line]}, holds)
        assert calls == [(c.OUTRANKED, ['source'])]
    elif consumer == 'implication':
        # A narrower positive list implies a broader one, including mixed stored formats.
        for other_shape in SHAPES:
            narrow = choice('service_ids', ['A'], shape, 'any' if shape == 'legacy' else 'is')
            broad = choice('service_ids', ['A', 'B'], other_shape, 'any' if other_shape == 'legacy' else 'is')
            assert c.implies({'conditions': {'conditions': [narrow]}}, {'conditions': {'conditions': [broad]}})
            assert not c.implies({'conditions': {'conditions': [broad]}}, {'conditions': {'conditions': [narrow]}})
    else:
        inner = {'conditions': [choice('capability_ids', ['skill'], shape),
                                choice('service_ids', ['clinic'], shape),
                                choice(c.INHERITED_LEVEL, ['source#max#high'], shape)]}
        tree = {'conditions': [choice(c.COUNT_BUCKET, [json.dumps([inner, []])], shape)]}
        if consumer == 'bucket_visitors':
            assert c.has_person_lines(tree)
            assert c.has_session_lines(tree)
            assert c.has_dated_person_lines(tree)
        else:
            bound = c.bind_level_window({'conditions': tree}, '2026-10-04', '2026-10-11')['conditions']
            [bucket] = bound['conditions']
            [descriptor] = c.absolute_values(bucket)
            nested, _ = json.loads(descriptor)
            assert c.absolute_values(nested['conditions'][-1]) == ['source#max#high@2026-10-04@2026-10-11']
            assert 'value' not in bucket and 'value' not in nested['conditions'][-1]


@pytest.mark.parametrize('shape', SHAPES)
@pytest.mark.parametrize('scenario', ['empty_minimum', 'assigned_granted_clinic', 'assigned_other_clinic'])
def test_clinic_skill_quota_subjects_and_validator_match_all_literal_shapes(h, shape, scenario):
    from .test_quota_skill_subjects import _assigned, _quota

    empty = scenario == 'empty_minimum'
    data, policy, person, _ = _quota(h, service=0 if scenario == 'assigned_granted_clinic' else 1,
                                    comparison='min' if empty else 'max')
    if empty:
        data['shifts'], data['focus_ids'] = [], []
        policy['definition']['rule']['lines'][1]['numbers']['min']['value'] = 0
    else:
        data = _assigned(data, person)
    expected = validate(deepcopy(data))
    def convert(node):
        if isinstance(node, dict):
            if 'field' in node and 'values' in node:
                return choice(node['field'], node['values'], shape, node.get('op', 'any'))
            return {key: convert(value) for key, value in node.items()}
        return [convert(value) for value in node] if isinstance(node, list) else node
    typed = deepcopy(data)
    typed['policies'] = convert(typed['policies'])
    result = validate(typed)
    # Report scopes preserve the authored operand shape; every computed quota fact must agree.
    def computed(report):
        return {**report, 'rows': [{key: value for key, value in row.items() if key != 'scope'}
                                  for row in report['rows']]}
    assert computed(result['quota_report']) == computed(expected['quota_report'])
    assert result['findings'] == expected['findings']
    assert result['valid'] is (scenario == 'assigned_granted_clinic')
    assert result['valid'] == expected['valid']
    if empty:
        default = [row for row in result['quota_report']['rows']
                   if row['line_id'] == policy['definition']['rule']['lines'][0]['id']]
        assert len(default) == 1 and default[0]['amount'] == 1 and default[0]['actual'] == 0

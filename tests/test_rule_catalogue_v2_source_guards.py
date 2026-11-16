"""Preserved defaults and mixed safety remain guarded through the existing authoring API."""
import copy
import uuid

import pytest

from .test_rule_catalogue_v2_api import proposed


def line(**more):
    return {'id': str(uuid.uuid4()), 'numbers': {'max': {'value': 5}}, **more}


def propose(h, lines, current=None, **more):
    status = more.pop('status', 200)
    return h.command('coordinator', 'rule.propose', {
        'catalogue_id': 'workload_limit', 'catalogue_version': 2,
        'expected_policy_id': current['id'] if current else None,
        'expected_revision': current['revision'] if current else None,
        'lines': lines, 'reason': 'Review a workload change', **more,
    }, status=status)


def convert(h):
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent department review'},
        conversion['id'], conversion['revision'])


def converted_default(h):
    # The category row really inherits its default. Explicit listed rows no longer
    # preserve an unused default, but stored default carriers retain these guards.
    source = h.command('coordinator', 'policy.create', {
        'name': 'Synthetic rank cap with inherited default', 'owner': 'Synthetic department',
        'source': 'Synthetic source default fixture', 'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'max_duties', 'maximum': 5, 'window': 'week'},
                       'listed_only': True, 'per_person': [{'category_ids': [h.seed['category']], 'amount': None}]},
    })['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent source review'}, source['id'], source['revision'])
    convert(h)
    page = h.get('coordinator', '/api/rules/catalogue?version=2&include_hidden=true')
    current = next(e['rule'] for e in page['entries'] if e['id'] == 'workload_limit')
    assert any(row.get('source_default') for row in current['definition']['rule']['lines'])
    return current


def converted_general(h):
    h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    convert(h)
    page = h.get('coordinator', '/api/rules/catalogue?version=2&include_hidden=true')
    return next(e['rule'] for e in page['entries'] if e['id'] == 'workload_limit')


def test_converted_obligation_cannot_acquire_an_unmapped_numeric_slot(h):
    current = converted_general(h)
    lines = copy.deepcopy(current['definition']['rule']['lines'])
    lines[0]['numbers']['min'] = {'value': 1}
    response = propose(h, lines, current, status=409)
    assert response['error']['code'] == 'SOURCE_NUMBER_IMMUTABLE'


def test_converted_obligation_keeps_its_editable_existing_number(h):
    current = converted_general(h)
    lines = copy.deepcopy(current['definition']['rule']['lines'])
    lines[0]['numbers']['max']['value'] = 8
    waiting = propose(h, lines, current)['record']
    assert waiting['definition']['rule']['lines'][0]['numbers']['max']['value'] == 8
    assert waiting['definition']['rule']['lines'][0]['source_bindings'] == lines[0]['source_bindings']


def test_ordinary_authoring_cannot_invent_a_source_default(h):
    convert(h)
    response = propose(h, [line(source_default=line())], status=409)
    assert response['error']['code'] == 'SOURCE_BINDING_IMMUTABLE'


@pytest.mark.parametrize('change', ['edit_default', 'delete_default', 'delete_carrier'])
def test_converted_inherited_default_cannot_be_changed_or_lost(h, change):
    current = converted_default(h)
    lines = copy.deepcopy(current['definition']['rule']['lines'])
    carrier = next(row for row in lines if row.get('source_default'))
    if change == 'edit_default':
        carrier['source_default']['numbers']['max']['value'] = 8
    elif change == 'delete_default':
        carrier.pop('source_default')
    elif change == 'delete_carrier':
        lines = [row for row in lines if row['id'] != carrier['id']]
    response = propose(h, lines, current, status=409)
    assert response['error']['code'] == 'SOURCE_BINDING_IMMUTABLE'


@pytest.mark.parametrize('on', [True, False])
def test_unchanged_source_default_roundtrips_through_existing_proposal(h, on):
    current = converted_default(h)
    lines = copy.deepcopy(current['definition']['rule']['lines'])
    waiting = propose(h, lines, current, on=on)['record']
    # The typed editor fields are projections; every stored field and preserved baseline must survive unchanged.
    assert waiting['definition']['rule']['lines'] == [{k: v for k, v in row.items() if k not in {'mode', 'measure', 'period'}}
                                                    for row in lines]
    assert waiting['definition']['rule']['on'] is on
    assert waiting['status'] == 'proposed'


def test_person_rule_read_classifies_each_mixed_v2_obligation(h):
    convert(h)
    rows = [line(settings={'max_duties': {'window': 'week'}}),
            line(numbers={'apply': {'value': None}}, settings={'restriction': {}})]
    waiting = propose(h, rows)['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent mixed-rule review'}, waiting['id'], waiting['revision'])
    data = h.get('coordinator', '/api/policies/applies?person_id=' + h.seed['people'][0] + '&day=2026-10-04')
    matched = {row['kind']: row for row in data['rules'] if row['policy_id'] == waiting['id']}
    assert set(matched) == {'max_duties', 'restriction'}
    assert 'safety: every line that holds applies' in matched['restriction']['why']
    assert 'most specific line that sets it' in matched['max_duties']['why']

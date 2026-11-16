"""Admission and storage of Count and has-all-of on the existing condition tree."""
import copy
import json
import uuid

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s


def tree(*children, **settings):
    return {'match': 'all', 'conditions': list(children), **settings}


def condition(field='tags', values=('night_capable', 'senior'), **settings):
    return {'field': field, 'op': 'any', 'values': list(values), **settings}


def catalogue(entry, who=None, where=None):
    return {'rule': {'kind': 'catalogue', 'id': entry, 'lines': [
        {'id': str(uuid.uuid4()), 'who': who, 'where': where, 'numbers': {'min': {'value': 1}}}]}}


@pytest.mark.parametrize('field', sorted(s.MULTI_PERSON_FIELDS))
def test_has_all_of_accepts_only_fields_a_person_can_hold_several_of(field):
    parsed = s.Condition.model_validate(condition(field, op='all'))
    assert parsed.op == 'all' and parsed.count == 'together'


@pytest.mark.parametrize('field', ['person_ids', 'service_ids', 'location_ids', 'assignment_type_ids'])
def test_has_all_of_refuses_person_identity_and_session_fields(field):
    with pytest.raises(ValidationError, match=f'Has all of cannot match {field}'):
        s.Condition.model_validate(condition(field, op='all'))


@pytest.mark.parametrize('count', ['together', 'each'])
def test_is_not_cannot_save_a_count_setting(count):
    with pytest.raises(ValidationError, match='Is not conditions cannot set Count'):
        s.Condition.model_validate(condition(op='none', count=count))
    assert s.Condition.model_validate(condition(op='none')).model_dump() == condition(op='none')


def test_together_is_omitted_at_every_depth_and_each_survives_nested_serialization():
    old = tree(condition(), tree(condition('service_ids', ['clinic']), match='any'))
    explicit = copy.deepcopy(old)
    explicit['count'] = explicit['conditions'][0]['count'] = 'together'
    explicit['conditions'][1]['count'] = explicit['conditions'][1]['conditions'][0]['count'] = 'together'
    for value in (old, explicit):
        parsed = s.Conditions.model_validate(value)
        assert parsed.model_dump() == old
        assert json.loads(parsed.model_dump_json()) == old
        definition = s.PolicyDefinition.model_validate({'rule': {'kind': 'max_duties', 'maximum': 6},
                                                       'scope': {'conditions': value}})
        assert definition.model_dump(mode='json')['scope']['conditions'] == old
    explicit['conditions'][1]['count'] = explicit['conditions'][1]['conditions'][0]['count'] = 'each'
    expected = copy.deepcopy(old)
    expected['conditions'][1]['count'] = expected['conditions'][1]['conditions'][0]['count'] = 'each'
    assert s.Conditions.model_validate(explicit).model_dump(mode='json') == expected


@pytest.mark.parametrize('rule', [{'kind': 'max_duties', 'maximum': 6}, {'kind': 'min_rest', 'hours': 11},
                                {'kind': 'travel_buffer', 'minutes': 30,
                                 'from_location_id': '00000000-0000-0000-0000-000000000001',
                                 'to_location_id': '00000000-0000-0000-0000-000000000002'}])
@pytest.mark.parametrize('who', [tree(condition(count='each')),
                               tree(tree(condition(), count='each', match='any')),
                               tree(condition(), condition('service_ids', ['clinic']), count='each')])
def test_workload_person_filters_and_person_groups_cannot_count_each(rule, who):
    with pytest.raises(ValidationError, match='Workload rules always count per person'):
        s.PolicyDefinition.model_validate({'rule': rule, 'scope': {'conditions': who}})


@pytest.mark.parametrize('entry', ['duties_per_week', 'hours_per_week', 'session_units_per_week',
                                 'rest_between_duties', 'travel_between_places'])
def test_catalogue_workload_validates_person_counts_before_expansion(entry):
    with pytest.raises(ValidationError, match='Workload rules always count per person'):
        s.PolicyDefinition.model_validate(catalogue(entry, who=tree(condition(count='each'))))


def test_catalogue_count_validation_follows_the_kind_actually_selected():
    line = {'id': str(uuid.uuid4()), 'who': tree(condition(count='each')), 'numbers': {'apply': {}}}
    predicate = {'kind': 'catalogue', 'id': 'not_after', 'lines': [line]}
    assert s.PolicyDefinition.model_validate({'rule': predicate}).rule.lines[0].who.count == 'together'
    pattern = {**predicate, 'settings': {'shift_succession': {}}}
    with pytest.raises(ValidationError, match='Workload rules always count per person'):
        s.PolicyDefinition.model_validate({'rule': pattern})


@pytest.mark.parametrize('rule', [{'kind': 'minimum_cover', 'minimum': 1},
                                {'kind': 'maximum_participants', 'maximum': 2},
                                {'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 2}])
def test_staffing_accepts_person_filter_count_each(rule):
    who = tree(condition('category_ids', ['R1', 'R2'], count='each'))
    assert s.PolicyDefinition.model_validate({'rule': rule, 'scope': {'conditions': who}}).scope.conditions.model_dump() == who


@pytest.mark.parametrize('rule', [{'kind': 'max_duties', 'maximum': 6}, {'kind': 'min_rest', 'hours': 11},
                                {'kind': 'travel_buffer', 'minutes': 30,
                                 'from_location_id': '00000000-0000-0000-0000-000000000001',
                                 'to_location_id': '00000000-0000-0000-0000-000000000002'}])
def test_workload_accepts_session_filter_and_group_count_each(rule):
    where = tree(condition('service_ids', ['A', 'B'], count='each'), count='each', match='any')
    assert s.PolicyDefinition.model_validate({'rule': rule, 'scope': {'conditions': where}}).scope.conditions.model_dump() == where
    parsed = s.PolicyDefinition.model_validate(catalogue('duties_per_week', where=where))
    assert parsed.rule.lines[0].where.model_dump() == where


def test_canonical_line_identity_includes_count_and_each_branch_order():
    before = {'where': tree(condition('service_ids', ['A', 'B']), condition('location_ids', ['wing']))}
    key = s.line_key('duties_per_week', before)
    explicit = copy.deepcopy(before)
    explicit['where']['count'] = explicit['where']['conditions'][0]['count'] = 'together'
    assert s.line_key('duties_per_week', explicit) == key
    explicit['where']['conditions'][0]['count'] = 'each'
    assert s.line_key('duties_per_week', explicit) != key
    each = {'where': {**before['where'], 'count': 'each'}}
    reversed_each = {'where': {**each['where'], 'conditions': list(reversed(each['where']['conditions']))}}
    assert s.line_key('duties_per_week', each) != s.line_key('duties_per_week', reversed_each)
    reversed_together = {'where': {**before['where'], 'conditions': list(reversed(before['where']['conditions']))}}
    assert s.line_key('duties_per_week', reversed_together) == key


def create_policy(h, conditions, status):
    return h.command('coordinator', 'policy.create', {
        'name': 'Synthetic Count admission', 'owner': 'Test owner', 'source': 'Synthetic Count contract test',
        'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'max_duties', 'maximum': 6},
                                                'scope': {'conditions': conditions}}}, status=status)


@pytest.mark.parametrize('mode', ['location_all', 'person_all', 'person_each', 'person_group_each', 'none_count'])
def test_invalid_condition_count_and_has_all_of_return_422(h, mode):
    field, values, settings, message = {
        'location_all': ('location_ids', h.seed['locations'], {'op': 'all'}, 'exactly one place/duty type'),
        'person_all': ('person_ids', h.seed['people'], {'op': 'all'}, 'one identity'),
        'person_each': ('category_ids', [h.seed['category']], {'count': 'each'}, 'always count per person'),
        'person_group_each': ('category_ids', [h.seed['category']], {}, 'always count per person'),
        'none_count': ('person_ids', h.seed['people'], {'op': 'none', 'count': 'together'}, 'cannot set Count'),
    }[mode]
    value = tree(condition(field, values, **settings), **({'count': 'each'} if mode == 'person_group_each' else {}))
    assert message in str(create_policy(h, value, status=422))


def test_policy_default_count_storage_is_byte_equivalent_to_old_tree(h):
    old = tree(condition('service_ids', h.seed['services']))
    value = tree(condition('service_ids', h.seed['services'], count='together'), count='together')
    record = h.policy({'kind': 'max_duties', 'maximum': 6}, {'conditions': value})
    assert json.dumps(record['definition']['scope']['conditions']) == json.dumps(old)


def test_has_all_of_person_tags_and_staffing_each_are_admitted_by_api(h):
    all_tags = tree(condition(op='all'))
    assert h.policy({'kind': 'restriction'}, {'conditions': all_tags})['definition']['scope']['conditions'] == all_tags
    each_rank = tree(condition('category_ids', [h.seed['category'], h.seed['trainee_category']], count='each'))
    assert h.policy({'kind': 'minimum_cover', 'minimum': 1}, {'conditions': each_rank})['definition']['scope']['conditions'] == each_rank


def test_catalogue_proposal_rejects_person_each_and_omits_together(h):
    who = tree(condition('category_ids', [h.seed['category']], count='each'))
    payload = {'catalogue_id': 'duties_per_week', 'lines': catalogue('duties_per_week', who=who)['rule']['lines'],
               'reason': 'Synthetic Count contract test'}
    assert 'always count per person' in str(h.command('coordinator', 'rule.propose', payload, status=422))
    old = tree(condition('service_ids', h.seed['services']))
    where = tree(condition('service_ids', h.seed['services'], count='together'), count='together')
    payload['lines'] = catalogue('duties_per_week', where=where)['rule']['lines']
    record = h.command('coordinator', 'rule.propose', payload)['record']
    assert json.dumps(record['definition']['rule']['lines'][0]['where']) == json.dumps(old)

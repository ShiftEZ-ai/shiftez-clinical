"""Literal condition admission is independent of its legacy or typed representation."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s
from packages.contracts.condition_limits import count_bucket_bound


CLINIC = str(uuid4())
OTHER_CLINIC = str(uuid4())


def tree(*lines, **options):
    return {'conditions': list(lines), **options}


def pooled(scope):
    return s.PolicyDefinition.model_validate({'scope': scope, 'rule': {
        'kind': 'minimum_cover', 'aggregation': 'service_group', 'minimum': 1}})


@pytest.mark.parametrize('condition,expected', [
    ({'field': 'service_ids', 'values': [CLINIC]}, [CLINIC]),
    ({'field': 'service_ids', 'values': [CLINIC, OTHER_CLINIC]}, [CLINIC, OTHER_CLINIC]),
    ({'field': 'service_ids', 'op': 'any', 'value': CLINIC}, [CLINIC]),
    ({'field': 'service_ids', 'op': 'is', 'value': CLINIC}, [CLINIC]),
    ({'field': 'service_ids', 'op': 'any', 'value': {'kind': 'absolute', 'value': CLINIC}}, [CLINIC]),
    ({'field': 'service_ids', 'op': 'is', 'value': {'kind': 'absolute', 'value': [CLINIC, OTHER_CLINIC]}}, [CLINIC, OTHER_CLINIC]),
])
def test_pooled_clinics_accept_each_absolute_literal_shape(condition, expected):
    scope = s.Scope(conditions=tree(condition))
    assert scope.chosen_services() == expected
    assert pooled({'conditions': tree(condition)}).scope.chosen_services() == expected


@pytest.mark.parametrize('conditions', [
    tree({'field': 'service_ids', 'op': 'none', 'values': [CLINIC]}),
    tree({'field': 'service_ids', 'op': 'is_not', 'value': CLINIC}),
    tree({'field': 'service_ids', 'op': 'empty'}),
    tree({'field': 'service_ids', 'op': 'not_empty'}),
    tree({'field': 'service_ids', 'op': 'is', 'value': CLINIC}, match='any'),
    tree(tree({'field': 'service_ids', 'op': 'is', 'value': CLINIC})),
    tree(tree({'field': 'service_ids', 'op': 'is', 'value': CLINIC},
              {'field': 'service_ids', 'op': 'is', 'value': OTHER_CLINIC}, match='any')),
])
def test_pooled_clinics_still_require_a_positive_top_level_and_scope(conditions):
    assert s.Scope(conditions=conditions).chosen_services() == []
    with pytest.raises(ValidationError, match='Pooled coverage requires an explicit clinic/service scope'):
        pooled({'conditions': conditions})


@pytest.mark.parametrize('scope', [{'service_id': CLINIC}, {'service_ids': [CLINIC, OTHER_CLINIC]}])
def test_existing_flat_pooled_clinic_scopes_remain_valid(scope):
    assert pooled(scope).scope


@pytest.mark.parametrize('values', [[CLINIC], [CLINIC, OTHER_CLINIC]])
def test_date_set_id_validation_covers_legacy_and_typed_literal_lists(values):
    for condition in ({'field': 'date_set_ids', 'values': values},
                      {'field': 'date_set_ids', 'op': 'is', 'value': {'kind': 'absolute', 'value': values}}):
        assert s.Condition.model_validate(condition).field == 'date_set_ids'
    assert s.Condition(field='date_set_ids', op='is', value=values[0]).field == 'date_set_ids'


@pytest.mark.parametrize('values,message', [(['unknown-id'], 'string_pattern_mismatch'),
                                         ([CLINIC, CLINIC], 'Choose distinct date set IDs')])
def test_date_set_id_refusals_match_for_legacy_and_typed_shapes(values, message):
    for condition in ({'field': 'date_set_ids', 'values': values},
                      {'field': 'date_set_ids', 'op': 'is', 'value': {'kind': 'absolute', 'value': values}}):
        with pytest.raises(ValidationError, match=message):
            s.Condition.model_validate(condition)


def test_count_each_stays_limited_to_bounded_legacy_literal_choices():
    legacy = s.Conditions.model_validate(tree({'field': 'tags', 'values': ['a', 'b'], 'count': 'each'}))
    assert count_bucket_bound(legacy.model_dump()) == 2
    for condition in ({'field': 'tags', 'op': 'is', 'value': {'kind': 'absolute', 'value': ['a', 'b']}},
                      {'field': 'tags', 'op': 'empty'}):
        with pytest.raises(ValidationError, match='Count each applies only to listed absolute choices'):
            s.Condition.model_validate({**condition, 'count': 'each'})
        with pytest.raises(ValidationError, match='Count each applies only to listed absolute choices'):
            s.Conditions.model_validate(tree(condition, count='each'))

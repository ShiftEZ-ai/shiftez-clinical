"""Additive operand contracts and deterministic rule/audience admission."""
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from packages.contracts import schemas as s


def relative(token, **parameters):
    return {'kind': 'relative', 'token': token, **parameters}


def tree(*conditions):
    return {'conditions': list(conditions)}


@pytest.mark.parametrize('field', s.CONDITION_FIELDS)
@pytest.mark.parametrize('op', ['empty', 'not_empty'])
def test_every_field_accepts_empty_without_an_operand(field, op):
    parsed = s.Condition(field=field, op=op)
    assert parsed.model_dump() == {'field': field, 'op': op, 'values': []}
    with pytest.raises(ValidationError, match='takes no operand'):
        s.Condition(field=field, op=op, values=['unexpected'])
    with pytest.raises(ValidationError, match='takes no operand'):
        s.Condition(field=field, op=op, value={'kind': 'absolute', 'value': ''})


@pytest.mark.parametrize('operand', ['2026-10-18', {'kind': 'absolute', 'value': '2026-10-18'},
                                   relative('today')])
def test_date_operands_preserve_the_chosen_shape(operand):
    condition = s.Condition(field='ends_on', op='is', value=operand)
    assert condition.model_dump(mode='json')['value'] == operand


def test_old_conditions_keep_their_exact_serialized_shape():
    for condition in ({'field': 'tags', 'op': 'any', 'values': ['student']},
                      {'field': 'person_ids', 'op': 'none', 'values': ['existing-id']},
                      {'field': 'date_set_ids', 'op': 'all', 'values': [str(uuid4())]}):
        assert s.Condition.model_validate(condition).model_dump(mode='json') == condition
    with pytest.raises(ValidationError, match='at least one'):
        s.Condition(field='tags', values=[])


def test_between_accepts_two_mixed_operands_and_refuses_wrong_arity():
    operands = ['2026-10-01', relative('today')]
    assert s.Condition(field='ends_on', op='between', value=operands).model_dump(mode='json')['value'] == operands
    for value in ('2026-10-18', ['2026-10-18'], ['2026-10-18'] * 3):
        with pytest.raises(ValidationError):
            s.Condition(field='ends_on', op='between', value=value)
    with pytest.raises(ValidationError, match='relative date range'):
        s.Condition(field='ends_on', op='within', value='2026-10-18')


@pytest.mark.parametrize('token', s.RELATIVE_TOKEN_CATALOGUE)
def test_catalogue_and_operand_validator_have_one_complete_token_set(token):
    value = relative(token['token'], **({'n': 2} if token['needs_n'] else {}),
                     **({'unit': 'weeks'} if token['needs_unit'] else {}))
    parsed = s.RelativeOperand.model_validate(value)
    assert parsed.model_dump(mode='json') == value
    assert token['label'] and token['field_kinds'] and token['contexts']
    assert set(token['contexts']) <= {'filter', 'selection', 'bulk', 'rule', 'audience'}
    if token['needs_n']:
        with pytest.raises(ValidationError):
            s.RelativeOperand.model_validate({key: item for key, item in value.items() if key != 'n'})
    else:
        with pytest.raises(ValidationError):
            s.RelativeOperand.model_validate({**value, 'n': 2})


@pytest.mark.parametrize('value', [relative('unknown'), relative('last_n', n=2, unit='years'),
                                 relative('last_n', n=0, unit='days'), relative('last_n', n=True, unit='days'),
                                 relative('last_n', n='2', unit='days'), relative('today', unit='days')])
def test_unknown_or_invalid_token_parameters_are_refused(value):
    with pytest.raises(ValidationError):
        s.RelativeOperand.model_validate(value)


@pytest.mark.parametrize('field,value', [('person_ids', relative('today')), ('ends_on', relative('me')),
                                       ('category_ids', relative('my_programme')), ('service_ids', relative('my_rank'))])
def test_tokens_apply_only_to_their_field_kind_and_dimension(field, value):
    with pytest.raises(ValidationError, match='does not apply'):
        s.Condition(field=field, op='is', value=value)


@pytest.mark.parametrize('op,value', [('before', 7), ('after', True), ('is', 'not-a-date')])
def test_date_operands_are_dates(op, value):
    with pytest.raises(ValidationError):
        s.Condition(field='ends_on', op=op, value=value)


@pytest.mark.parametrize('value', ['7', True, {'kind': 'absolute', 'value': []}])
def test_number_operands_remain_typed(value):
    with pytest.raises(ValidationError, match='number operand'):
        s.Condition(field='number', op='gte', value=value)
    assert s.Condition(field='number', op='gte', value=7).value == 7


@pytest.mark.parametrize('field', ['name', 'person_ids', 'tags', 'service_ids'])
@pytest.mark.parametrize('value', [True, 7, None, [7]])
def test_text_and_choice_operands_reject_other_scalar_types(field, value):
    with pytest.raises(ValidationError):
        s.Condition(field=field, op='is', value={'kind': 'absolute', 'value': value})


@pytest.mark.parametrize('field', ['date', 'ends_on', 'name', 'note', 'number', 'capacity', 'points', 'min', 'max'])
@pytest.mark.parametrize('op', ['any', 'all', 'none'])
def test_new_scalar_fields_do_not_admit_untyped_legacy_choice_operators(field, op):
    with pytest.raises(ValidationError, match='choose a typed operand'):
        s.Condition(field=field, op=op, values=['not-a-typed-operand'])


@pytest.mark.parametrize('field,value', [('date', relative('today')), ('person_ids', relative('me')),
                                       ('service_ids', relative('my_clinics'))])
def test_rule_and_audience_paths_refuse_viewer_or_today_tokens_at_every_depth(field, value):
    conditions = tree(tree({'field': field, 'op': 'is', 'value': value}))
    for schema, payload in ((s.Scope, {'conditions': conditions}),
                            (s.Audience, {'where': conditions}),
                            (s.RuleLine, {'id': str(uuid4()), 'where' if field in s.SESSION_SCOPE_FIELDS else 'who': conditions,
                                          'numbers': {'apply': {}}})):
        with pytest.raises(ValidationError, match='only duty-relative'):
            schema.model_validate(payload)


@pytest.mark.parametrize('token', sorted(s.DUTY_RELATIVE_TOKENS))
def test_rule_and_audience_paths_accept_duty_relative_dates(token):
    operand = relative(token, **({'n': 3} if token in {'days_before_duty', 'days_after_duty'} else {}))
    conditions = tree({'field': 'date', 'op': 'within', 'value': operand})
    assert s.Scope(conditions=conditions).conditions
    assert s.Audience(where=conditions).where
    assert s.RuleLine(id=str(uuid4()), where=conditions, numbers={'apply': {}}).where


def test_count_each_cannot_expand_runtime_relative_or_scalar_values():
    condition = {'field': 'person_ids', 'op': 'is', 'value': relative('me')}
    with pytest.raises(ValidationError, match='listed absolute choices'):
        s.Condition(**condition, count='each')
    with pytest.raises(ValidationError, match='listed absolute choices'):
        s.Conditions(**tree(condition), count='each')


def test_new_operands_participate_in_canonical_rule_line_identity():
    def key(value):
        return s.line_key('workload_limit', {'where': tree({'field': 'date', 'op': 'is', 'value': value})})
    assert key('2026-10-18') == key({'kind': 'absolute', 'value': '2026-10-18'})
    assert key(relative('duty_day')) != key(relative('duty_week'))


def test_resolve_and_parser_request_contracts_bound_and_type_inputs():
    assert s.ConditionResolveInput(operand=relative('today'), field='ends_on').context == 'filter'
    assert s.ConditionDateParseInput(text='next 2 weeks').text == 'next 2 weeks'
    for text in ('', 'x' * 121):
        with pytest.raises(ValidationError):
            s.ConditionDateParseInput(text=text)
    assert TypeAdapter(s.ConditionOperand).validate_python({'kind': 'absolute', 'value': ['clinic-a', 'clinic-b']}).value == ['clinic-a', 'clinic-b']


def test_parser_absolute_month_range_can_be_saved_as_the_chosen_operand():
    from datetime import date
    from packages.domain.conditions import EvaluationContext, parse_date_short_form
    context = EvaluationContext(timezone='Asia/Riyadh', week_start=6, today=date(2026, 10, 18))
    matches = parse_date_short_form('oct', context)
    operand = matches[0]['operand']
    assert s.Condition(field='ends_on', op='is', value=operand).model_dump(mode='json')['value'] == operand
    response = s.ConditionDateParseResult(matches=matches).model_dump(mode='json', by_alias=True)
    assert response['matches'][0]['from'] == '2026-10-01'
    for invalid in ([], ['2026-10-18'], ['2026-10-18'] * 3, [1, 2]):
        with pytest.raises(ValidationError):
            s.Condition(field='ends_on', op='is', value={'kind': 'absolute', 'value': invalid})


def test_my_clinics_catalogue_uses_clinic_permissions_and_stays_viewer_relative():
    token = next(entry for entry in s.RELATIVE_TOKEN_CATALOGUE if entry['token'] == 'my_clinics')
    assert token['label'] == 'Clinics I can work at'
    assert 'clinic permission' in token['description'] and 'As of' in token['description']
    assert token['requirements'] == ('clinic_permission',) and not token['roles']
    assert set(token['fields']) == {'service_ids', 'eligible_service_ids', 'location_ids'}
    assert set(token['contexts']) == {'filter', 'selection', 'bulk'}

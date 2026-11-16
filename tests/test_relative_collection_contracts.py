"""The collection-filter bridge retains facet JSON and uses the shared operand admission."""
import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s


def tree(field, op='any', **operand):
    return {'conditions': [{'field': field, 'op': op, **operand}]}


@pytest.mark.parametrize('table,field,values', [('requests', 'status', ['waiting']),
    ('operations', 'state', ['failed']), ('operations', 'error', ['failure']),
    ('audit', 'action', ['catalogue.create']), ('audit', 'reason', ['Synthetic reason'])])
@pytest.mark.parametrize('op', ['any', 'all', 'none'])
def test_old_registered_collection_facet_shapes_are_unchanged(table, field, values, op):
    original = {'conditions': {'match': 'all', **tree(field, op, values=values)}}
    parsed = s.CollectionFilter.model_validate(original, context={'table_key': table})
    assert parsed.model_dump(mode='json', exclude_none=True) == original


@pytest.mark.parametrize('field', ['start', 'end', 'dates.start', 'dates.end'])
@pytest.mark.parametrize('op', ['is', 'before', 'after', 'on_or_before', 'on_or_after', 'within'])
def test_collection_date_filters_accept_the_shared_relative_operand(field, op):
    operand = {'kind': 'relative', 'token': 'next_n', 'n': 2, 'unit': 'weeks'}
    parsed = s.CollectionFilter.model_validate({'conditions': tree(field, op, value=operand)}, context={'table_key': 'requests'})
    assert parsed.conditions.conditions[0].value.model_dump(mode='json') == operand


@pytest.mark.parametrize('field,source', [('dates.start', 'start'), ('dates.end', 'end')])
def test_date_part_aliases_name_their_registered_source_column(field, source):
    definition = s.condition_field_definition(field, 'requests')
    assert definition['operand_field'] == source and definition['value_kind'] == 'date'


def test_mixed_between_and_empty_have_one_shape_in_all_collection_inputs():
    conditions = {'match': 'all', 'conditions': [
        {'field': 'start', 'op': 'between', 'value': ['2026-10-01', {'kind': 'relative', 'token': 'today'}]},
        {'field': 'status', 'op': 'not_empty'}]}
    filtered = s.CollectionFilter.model_validate({'conditions': conditions}, context={'table_key': 'requests'})
    saved = s.TableViewInput(table_key='requests', name='View', definition={'conditions': conditions})
    selected = s.BulkSelection(collection='requests', where={'conditions': conditions})
    expected = filtered.conditions.model_dump(mode='json')
    assert saved.definition.conditions.model_dump(mode='json') == expected
    assert selected.where.conditions.model_dump(mode='json') == expected


@pytest.mark.parametrize('table,field', [('operations', 'private_reason'), ('audit', 'state'), ('requests', 'error')])
def test_collection_bridge_rejects_unknown_or_unrelated_fields(table, field):
    with pytest.raises(ValidationError, match='Unknown condition field'):
        s.CollectionFilter.model_validate({'conditions': tree(field, values=['hidden'])}, context={'table_key': table})


@pytest.mark.parametrize('table', ['operations', 'audit'])
def test_collection_history_dates_accept_relative_and_empty_tokens(table):
    for field in ('date', 'created_at'):
        assert s.CollectionFilter.model_validate({'conditions': tree(field, 'is', value={'kind': 'relative', 'token': 'today'})},
                                                context={'table_key': table}).conditions
        assert s.CollectionFilter.model_validate({'conditions': tree(field, 'empty')}, context={'table_key': table}).conditions


def test_shared_condition_and_rule_admission_stay_strict():
    with pytest.raises(ValidationError, match='choose a typed operand'):
        s.Condition(field='start', values=['not-a-date'])
    with pytest.raises(ValidationError, match='cannot filter on: state'):
        s.Scope(conditions=tree('state', values=['failed']))
    with pytest.raises(ValidationError, match='only duty-relative'):
        s.Scope(conditions=tree('date', 'is', value={'kind': 'relative', 'token': 'today'}))


def test_usual_assignee_conditions_keep_the_incoming_multivalue_contract():
    assert s.Condition(field='usual_assignee_ids', op='all', values=['person-a', 'person-b']).values == ['person-a', 'person-b']
    assert 'usual_assignee_ids' in s.SESSION_SCOPE_FIELDS
    assert 'all' in s.CONDITION_OPERATORS['people']
    with pytest.raises(ValidationError, match='Has all of cannot match person_ids'):
        s.Condition(field='person_ids', op='all', values=['person-a', 'person-b'])


def test_shared_view_privacy_guard_accepts_known_qualified_identity_columns():
    for field in ('person', 'person_ids', 'person.staff_number', 'identity.display_name'):
        personal = s.CollectionConditions.model_validate(tree(field, values=['someone']))
        with pytest.raises(ValueError, match='cannot filter by person'):
            s.department_view_filters(None, conditions=personal)


@pytest.mark.parametrize('operand', ['00000000-0000-0000-0000-000000000001',
    {'kind': 'absolute', 'value': '00000000-0000-0000-0000-000000000001'}])
def test_computed_policy_reference_accepts_typed_literal_or_wrapped_operand(operand):
    conditions = tree('policy_id', 'is', value=operand)
    selected = s.BulkSelection(collection='policy_lines', where={'conditions': conditions})
    assert selected.where.conditions.conditions[0].model_dump(mode='json')['value'] == operand
    for key in ('policy_lines', 'related-policy_lines', 'bulk-editor-policy_lines'):
        saved = s.TableViewInput(table_key=key, name='Policy line view', definition={'conditions': conditions})
        assert saved.definition.conditions.model_dump(mode='json') == selected.where.conditions.model_dump(mode='json')
    assert 'policy_id' in s.BULK_COLLECTIONS['policy_lines']['parent_of'].values()
    assert {'policy_id', 'line_id', 'catalogue_id'}.isdisjoint(s.BULK_COLLECTIONS['policy_lines']['columns'])
    assert s.condition_field_definition('policy_id', 'policy_lines')['value_kind'] == 'multiple_choice'


@pytest.mark.parametrize('op', ['empty', 'not_empty'])
def test_computed_policy_reference_accepts_operand_free_empty_checks(op):
    selected = s.BulkSelection(collection='policy_lines', where={'conditions': tree('policy_id', op)})
    assert selected.where.conditions.conditions[0].op == op
    assert s.TableViewInput(table_key='related-policy_lines', name='Policy line view',
                           definition={'conditions': tree('policy_id', op)}).definition.conditions.conditions[0].op == op


@pytest.mark.parametrize('field', ['private_reason', 'expected_policy_id', 'expected_revision', 'unknown_column'])
def test_readable_computed_columns_do_not_reintroduce_private_or_write_only_fields(field):
    with pytest.raises(ValidationError, match='Unknown condition field'):
        s.BulkSelection(collection='policy_lines', where={'conditions': tree(field, 'is', value='hidden')})


def test_computed_collection_reference_does_not_widen_clinical_admission():
    with pytest.raises(ValidationError, match='cannot filter on: policy_id'):
        s.Scope(conditions=tree('policy_id', 'is', value='00000000-0000-0000-0000-000000000001'))


def test_parent_condition_metadata_preserves_the_declared_field_type():
    for collection, field in [('programme_memberships', 'person_id'), ('eligibilities', 'service_id')]:
        definition = s.condition_field_definition(field, collection)
        assert definition['operand_field'] in {'person_ids', 'service_ids'}
        assert definition['value_kind'] in {'people', 'single_choice'}

"""Collection column conditions share one contract without widening rule or audience scopes."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s


def line(field, op='any', **value):
    return {'field': field, 'op': op, **(value or {'values': ['Waiting']})}


def saved(table_key, *conditions):
    return s.TableViewInput(table_key=table_key, name='Saved view', definition={'conditions': {'conditions': list(conditions)}})


@pytest.mark.parametrize('key', ['requests', 'person-requests'])
def test_request_own_facets_and_split_dates_save_together(key):
    original = [line('status'), line('type', values=['Leave']),
                line('start', 'within', value={'kind': 'relative', 'token': 'this_month'}),
                line('dates.end', 'before', value='2026-11-01')]
    parsed = saved(key, *original)
    stored = parsed.definition.model_dump(mode='json')['conditions']['conditions']
    assert [item['field'] for item in stored] == ['status', 'type', 'start', 'dates.end']
    assert stored[2]['value'] == original[2]['value']


@pytest.mark.parametrize('table_key,field,op,value', [
    ('requests', 'detail', 'contains', 'training'), ('people', 'order', 'gte', 5),
    ('workload', 'hours', 'lt', 40), ('outbox', 'attempts', 'gte', 2),
    ('feedback', 'report', 'contains', 'filter'), ('feedback-mine', 'occurrences', 'gt', 1),
    ('staff-imports', 'rows', 'gte', 150), ('staff-import-rows', 'row', 'is', 3),
    ('catalogue-tags', 'members', 'gte', 1), ('catalogue-leave_types', 'order', 'lte', 10),
    ('person-sessions', 'device', 'contains', 'Browser'), ('person-work', 'what', 'contains', 'Duty')])
def test_own_text_and_number_columns_have_typed_operands(table_key, field, op, value):
    assert saved(table_key, line(field, op, value=value)).definition.conditions


@pytest.mark.parametrize('table_key,field', [('requests', 'report'), ('outbox', 'wish'), ('people', 'attempts'),
                                          ('made-up-table', 'status')])
def test_registered_columns_cannot_be_used_on_an_unrelated_table(table_key, field):
    with pytest.raises(ValidationError, match='Unknown condition field|This list cannot filter'):
        saved(table_key, line(field))


@pytest.mark.parametrize('table_key', ['requests', 'outbox', 'unregistered-existing-table'])
def test_old_domain_field_conditions_remain_valid_on_every_saved_table(table_key):
    original = line('service_ids', values=['legacy-clinic'])
    parsed = saved(table_key, original)
    assert parsed.definition.model_dump(mode='json')['conditions']['conditions'] == [original]


@pytest.mark.parametrize('table_key', ['related-eligibilities', 'bulk-editor-eligibilities', 'eligibilities'])
def test_related_and_bulk_tables_derive_dates_from_the_existing_collection_schema(table_key):
    assert s.condition_field_definition('ends_on', table_key)['value_kind'] == 'date'
    assert saved(table_key, line('ends_on', 'not_empty', values=[])).definition.conditions
    assert s.condition_field_definition('person.display_name', table_key)['operand_field'] == 'person_ids'


def test_per_table_types_do_not_leak_through_column_name_collisions():
    # The staff-import Person facet holds outcome words; Requests Person holds person display names.
    assert s.condition_field_definition('person', 'requests')['operand_field'] == 'person_ids'
    assert 'operand_field' not in s.condition_field_definition('person', 'staff-import-rows')
    me = line('person', 'is', value={'kind': 'relative', 'token': 'me'})
    assert saved('requests', me).definition.conditions
    with pytest.raises(ValidationError, match='does not apply'):
        saved('staff-import-rows', me)


def test_preparsed_definitions_are_checked_against_the_table():
    definition = s.TableViewDefinition(conditions={'conditions': [line('status')]})
    assert s.TableViewInput(table_key='requests', name='View', definition=definition)
    with pytest.raises(ValidationError, match='Unknown condition field'):
        s.TableViewInput(table_key='unregistered-table', name='View', definition=definition)


@pytest.mark.parametrize('schema,payload', [(s.Scope, {'conditions': {'conditions': [line('status')]}}),
    (s.Audience, {'where': {'conditions': [line('status')]}}),
    (s.RuleLine, {'id': str(uuid4()), 'where': {'conditions': [line('status')]}, 'numbers': {'apply': {}}})])
def test_collection_fields_do_not_widen_rule_or_audience_admission(schema, payload):
    with pytest.raises(ValidationError, match='cannot filter on: status'):
        schema.model_validate(payload)


def test_duty_rule_determinism_remains_enforced_after_broadening_column_keys():
    with pytest.raises(ValidationError, match='only duty-relative'):
        s.Scope(conditions={'conditions': [line('date', 'is', value={'kind': 'relative', 'token': 'today'})]})


def test_own_field_metadata_distinguishes_labels_from_raw_identifiers():
    person = s.condition_field_definition('person', 'requests')
    assert person['value_kind'] == 'people' and person['display_values']
    raw = s.condition_field_definition('person_id', 'programme_memberships')
    assert raw['operand_field'] == 'person_ids' and not raw['display_values']
    assert s.condition_field_definition('c0', 'catalogue-locations')['label'] == 'Type'
    assert s.condition_field_definition('c0', 'catalogue-services')['label'] == 'Mode'


def test_source_record_table_namespace_keeps_own_columns():
    assert saved('source-records-' + str(uuid4()), line('status')).definition.conditions


@pytest.mark.parametrize('condition', [line('c0'), line('c1', 'contains', value='Consultant'),
                                     line('c2', 'gt', value=3), line('c3', 'is', value=5)])
def test_data_dependent_rule_line_columns_admit_their_real_value_kinds(condition):
    assert saved('rule-line-rows', condition).definition.conditions
    assert s.RULE_LINE_COLUMN_LIMIT == 9 + max(map(len, s.LINE_SETTINGS.values()))
    with pytest.raises(ValidationError, match='Unknown condition field'):
        saved('rule-line-rows', line(f'c{s.RULE_LINE_COLUMN_LIMIT}'))


def test_data_dependent_rule_line_columns_do_not_guess_relative_dimensions():
    with pytest.raises(ValidationError, match='does not apply'):
        saved('rule-line-rows', line('c0', 'is', value={'kind': 'relative', 'token': 'me'}))


@pytest.mark.parametrize('op,value,kind', [('is', {'kind': 'absolute', 'value': 5}, 'number'),
    ('is', s.AbsoluteOperand(kind='absolute', value='needle'), 'text'), ('between', [1, 3], 'number'),
    ('between', (s.AbsoluteOperand(kind='absolute', value=1), 3), 'number'), ('any', None, 'multiple_choice')])
def test_dynamic_column_kind_inference_is_shared_for_wire_and_parsed_operands(op, value, kind):
    assert s.condition_line_definition('c0', 'rule-line-rows', op, value)['value_kind'] == kind


@pytest.mark.parametrize('table_key', sorted(set(s.TABLE_CONDITION_DEFINITIONS) | set(s._TABLE_CONDITION_ALIASES)))
def test_every_registered_table_admits_its_own_typed_columns_and_empty_operators(table_key):
    for field in s.table_condition_definitions(table_key):
        key, kind = field['key'], field['value_kind']
        for op in ('empty', 'not_empty'):
            assert saved(table_key, line(key, op, values=[])).definition.conditions
        if kind == 'date':
            condition = line(key, 'is', value='2026-10-18')
            relative = line(key, 'within', value={'kind': 'relative', 'token': 'this_month'})
            assert saved(table_key, relative).definition.conditions
        elif kind == 'text':
            condition = line(key, 'contains', value='needle')
        elif kind == 'number':
            condition = line(key, 'gte', value=3)
        else:
            condition = line(key, values=[str(uuid4()) if key == 'date_set_ids' else 'value'])
        assert saved(table_key, condition).definition.conditions
    with pytest.raises(ValidationError, match='Unknown condition field'):
        saved(table_key, line('unregistered_column'))


def test_bounded_field_keys_and_batch_preview_contract():
    operand = {'kind': 'relative', 'token': 'today'}
    parsed = s.ConditionResolveBatchInput(table_key='requests', operands=[{'field': 'dates.start', 'operand': operand}])
    assert parsed.operands[0].operand.token == 'today'
    assert s.ConditionResolveInput(table_key='requests', field='person', operand={'kind': 'relative', 'token': 'me'})
    for operands in ([], [{'field': 'start', 'operand': operand}] * 1001):
        with pytest.raises(ValidationError):
            s.ConditionResolveBatchInput(table_key='requests', operands=operands)
    with pytest.raises(ValidationError, match='Unknown condition field'):
        s.ConditionResolveBatchInput(table_key='requests', operands=[{'field': 'report', 'operand': operand}])
    for field in ('', 'x' * 101, 'value[0]'):
        with pytest.raises(ValidationError):
            saved('requests', line(field))
    result = s.ConditionResolveBatchResult(results=[{'label': 'Today', 'display': '18 Oct', 'from': '2026-10-18', 'to': '2026-10-18'},
                                                   {'label': 'Me', 'display': 'Test person', 'ids': ['person-a']}])
    assert result.model_dump(mode='json', by_alias=True, exclude_none=True)['results'][1]['ids'] == ['person-a']


@pytest.mark.parametrize('field', ['off_cover', 'reserved', 'ot_offered'])
def test_conditional_workload_numbers_use_number_operands(field):
    assert saved('workload', line(field, 'gte', value=1)).definition.conditions


@pytest.mark.parametrize('field', ['from', 'through'])
def test_conditional_workload_dates_accept_relative_ranges(field):
    assert saved('workload', line(field, 'within', value={'kind': 'relative', 'token': 'this_month'})).definition.conditions


def test_dynamic_rule_catalogue_columns_derive_the_number_keys_from_the_catalogue():
    from packages.contracts.rule_catalogue import NUMBER_ORDER
    for key in NUMBER_ORDER:
        condition = line(f'n_{key}', 'contains', value='On') if key == 'apply' else line(f'n_{key}', 'gte', value=1)
        assert saved('rule-line-db', condition).definition.conditions
    for field in ('mode', 'measure', 'period'):
        assert saved('rule-line-db', line(field, 'contains', value='value')).definition.conditions
    assert {f'n_{key}' for key in NUMBER_ORDER} <= s.table_condition_registry()['rule-line-db'].keys()
    assert s.condition_field_definition('n_target', 'rule-line-db')['operand_field'] == 'target'
    assert saved('rule-line-db', line('n_target', 'is', value={'kind': 'relative', 'token': 'its_target'})).definition.conditions


def test_related_dated_status_and_service_name_keep_their_choice_semantics():
    assert saved('related-eligibilities', line('status', 'empty', values=[])).definition.conditions
    assert saved('related-eligibilities', line('service.name', 'none', values=['Clinic'])).definition.conditions
    metadata = s.condition_field_definition('service.name', 'related-eligibilities')
    assert metadata['operand_field'] == 'service_ids' and metadata['display_values']


def test_foreign_ids_without_a_relative_dimension_remain_choices():
    assert saved('bulk-editor-services', line('calendar_id', 'none', values=[str(uuid4())])).definition.conditions
    assert s.condition_field_definition('calendar_id', 'bulk-editor-services')['value_kind'] == 'multiple_choice'


def test_bulk_selection_validates_operands_using_its_collection_context():
    selection = s.BulkSelection(collection='eligibilities', where={'conditions': {'conditions': [line('service.name', 'none', values=['Clinic'])]}})
    assert selection.where.conditions.conditions[0].field == 'service.name'
    with pytest.raises(ValidationError, match='Unknown condition field'):
        s.BulkSelection(collection='services', where={'conditions': {'conditions': [line('device', 'contains', value='Phone')]}})


def test_related_text_facets_and_dotted_view_properties_keep_existing_shapes():
    condition = line('person.staff_number', 'none', values=['123'])
    definition = {'conditions': {'conditions': [condition]}, 'hidden': ['service.name'],
                  'group': 'person.display_name', 'sort': ['person.staff_number', 'asc'],
                  'filters': {'person.staff_number': ['123']}, 'order': ['service.name'], 'wrap': ['service.name']}
    value = s.TableViewInput(table_key='bulk-editor-eligibilities', name='View', definition=definition)
    assert value.definition.group == 'person.display_name'
    assert value.definition.filters == definition['filters']
    assert saved('related-eligibilities', line('person.staff_number', 'contains', value='12')).definition.conditions
    assert saved('related-services', line('description', 'any', values=['Clinic'])).definition.conditions
    assert saved('related-services', line('description', 'contains', value='Clinic')).definition.conditions

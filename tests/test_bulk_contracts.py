"""Bulk editing consumes the same schemas, conditions and authorization as single edits."""
import json
import uuid
from datetime import date

import pytest
from pydantic import Field, ValidationError

from apps.api.commands import command_roles
from apps.api.services.bulk_selection import collection_readers
from packages.contracts import schemas as s
from scripts.generate_contracts import render


def identifier():
    return str(uuid.uuid4())


def item(**fields):
    return {'target_id': identifier(), 'expected_revision': 1, 'payload': {}, **fields}


@pytest.mark.parametrize('field', ['bulk_staged_count', 'bulk_review_count'])
def test_me_contract_requires_strict_nonnegative_bulk_counts_and_generates_them(field):
    value = {'identity_id': identifier(), 'display_name': 'Synthetic coordinator',
             'department': {'id': identifier(), 'name': 'Synthetic department'},
             'organisation': {'id': identifier(), 'name': 'Synthetic organisation'},
             'roles': ['coordinator'], 'permissions': [], 'person_id': None, 'staff_swap_access': False,
             'self_approval': ['rule.activate'], 'account': {'username': 'synthetic'},
             'duty_types': [{'id': identifier(), 'created_at': '2026-10-18T00:00:00+00:00', 'colour': ''}],
             'environment': 'dev', 'deployment': 'local', 'production_qualified': False,
             'version': '0.0.0', 'release': 'synthetic', 'catalogue_version': 2,
             'bulk_staged_count': 0, 'bulk_review_count': 0}
    assert getattr(s.Me.model_validate(value), field) == 0
    schema = s.Me.model_json_schema()
    assert set(schema['required']) == set(value)
    assert schema['properties'][field]['type'] == 'integer'
    assert schema['properties'][field]['minimum'] == 0
    assert 'default' not in schema['properties'][field]
    for invalid in (-1, True, 0.5, '1', None):
        with pytest.raises(ValidationError):
            s.Me.model_validate({**value, field: invalid})
    with pytest.raises(ValidationError, match=field):
        s.Me.model_validate({key: item for key, item in value.items() if key != field})
    source, generated = render()
    assert generated['Me'] == schema and 'export type Me = {' in source
    assert '"permissions": Array<string>;' in source
    assert f'"{field}": number;' in source
    assert f'"{field}"?:' not in source


def test_registry_reuses_every_catalogue_schema_and_real_field_constraints():
    assert set(s.CATALOGUE_SCHEMAS) <= s.BULK_COLLECTIONS.keys()
    for name, cls in s.CATALOGUE_SCHEMAS.items():
        collection = s.BULK_COLLECTIONS[name]
        assert collection['schema'] == cls.__name__
        contact_fields = {entry['legacy_field'] for entry in s.DEFAULT_CONTACT_TYPES} if cls is s.PersonInput else ()
        assert collection['fields'] == s._bulk_fields(cls, exclude=contact_fields)
    assert s.BULK_COLLECTIONS['people']['fields']['title']['schema']['maxLength'] == 160
    assert 'private_reason' not in s.BULK_COLLECTIONS['availability']['fields']
    assert 'private_reason' not in s.BULK_COLLECTIONS['availability']['columns']
    assert 'reason' not in s.BULK_COLLECTIONS['eligibilities']['fields']


def test_new_bulk_field_is_derived_from_its_contract_without_a_second_field_list():
    class Example(s.Schema):
        omitted: str
        title: str = Field(max_length=12, json_schema_extra={'bulk': True})
        day: date | None = Field(default=None, json_schema_extra={'bulk': True})
        tags: list[str] = Field(default_factory=list, max_length=3, json_schema_extra={'bulk': True})

    fields = s._bulk_fields(Example)
    assert set(fields) == {'title', 'day', 'tags'}
    assert fields['title']['schema']['maxLength'] == 12
    assert fields['day']['how'] == ['set', 'move_by', 'clear']
    assert fields['tags']['how'] == ['add', 'remove', 'set']


def test_every_action_is_allowlisted_and_every_child_has_a_declared_relation():
    assert set(s.BULK_ITEM_KINDS) == set(s.BULK_REVERSE_KINDS)
    for key, collection in s.BULK_COLLECTIONS.items():
        assert set(collection['item_kinds']) <= set(s.BULK_ITEM_KINDS)
        for child in collection['children']:
            assert key in s.BULK_COLLECTIONS[child]['parent_of']
        for condition in collection['conditions']:
            assert condition in s.CONDITION_FIELD_DEFINITIONS
        for action in collection['actions'].values():
            assert action['kind'] in s.BULK_ITEM_KINDS
            if key in {'policies', 'policy_lines', 'scheduling_facts'}:
                assert action['governed']
        assert all(column['read_only'] for column in collection['related_columns'].values())


def test_registry_roles_are_the_command_bus_rules_including_programme_scope():
    registry = s.bulk_collection_registry(command_roles)
    for key, collection in registry.items():
        for action in collection['actions'].values():
            payload = action.get('payload', {'catalogue': key})
            assert action['roles'] == command_roles(s.Command(kind=action['kind'], payload=payload))
    assert registry['eligibilities']['actions']['update_dates']['roles'] == ['credentialer']
    assert 'programme_lead' in registry['people']['actions']['programme']['roles']


@pytest.mark.parametrize('kind', ['bulk.apply', 'workspace.publish', 'workspace.approve', 'staff_import.apply',
                                 'integration.github.connect', 'outbox.retry', 'account.invite'])
def test_bulk_items_refuse_nonallowlisted_and_external_commands(kind):
    with pytest.raises(ValidationError):
        s.BulkApply(item_kind=kind, items=[item()], reason='Synthetic change')


def test_mixed_kinds_and_several_changes_preserve_explicit_null_and_row_indexes():
    payload = s.BulkApply(items=[item(kind='availability.update', payload={'leave_type_id': None}),
        item(item_kind='eligibility.update_dates', changes=[{'field': 'starts_on', 'how': 'move_by', 'value': 1},
                                                          {'field': 'ends_on', 'how': 'clear'}])],
        only=[1], resolutions={'0': 'skip'}, reason='Synthetic change')
    assert payload.items[0].payload == {'leave_type_id': None}
    assert len(payload.items[1].changes) == 2 and payload.only == [1]
    with pytest.raises(ValidationError, match='outside the items'):
        s.BulkApply(item_kind='availability.update', items=[item()], only=[1], reason='Synthetic change')
    with pytest.raises(ValidationError, match='each row index once'):
        s.BulkApply(item_kind='availability.update', items=[item()], only=[0, 0], reason='Synthetic change')


def test_selection_uses_the_shared_grouped_condition_tree_and_two_levels():
    selection = s.BulkSelection(collection='eligibilities', parent={'collection': 'people',
        'selection': {'collection': 'people', 'where': {'as_of': '2026-10-18', 'conditions': {
            'match': 'all', 'conditions': [{'field': 'tags', 'values': ['student']},
                {'match': 'any', 'conditions': [{'field': 'programme_ids', 'values': [identifier()]},
                                               {'field': 'category_ids', 'values': [identifier()]}]}]}}}})
    assert isinstance(selection.parent.selection.where.conditions, s.Conditions)
    assert selection.parent.selection.where.as_of.isoformat() == '2026-10-18'
    with pytest.raises(ValidationError, match='two levels'):
        s.BulkSelection(collection='availability', parent={'collection': 'people', 'selection': {
            'collection': 'people', 'parent': {'collection': 'tags', 'selection': {'collection': 'tags'}}}})
    with pytest.raises(ValidationError, match='no parent-child relationship'):
        s.BulkSelection(collection='availability', parent={'collection': 'services', 'selection': {'collection': 'services'}})


def test_missing_command_schemas_preserve_privacy_and_single_item_validation():
    value = s.AvailabilityUpdate(leave_type_id=None, reason='Synthetic update')
    assert value.model_fields_set == {'leave_type_id', 'reason'}
    assert s.AvailabilityUpdate(kind='approved_leave', reason='Synthetic update').kind == 'approved_leave'
    with pytest.raises(ValidationError):
        s.AvailabilityUpdate(private_reason='Must remain private', reason='Synthetic update')
    with pytest.raises(ValidationError, match='UTC offset'):
        s.AvailabilityUpdate(start='2026-10-18T08:00:00', reason='Synthetic update')
    with pytest.raises(ValidationError):
        s.AvailabilityUpdate(start=None, reason='Synthetic update')
    assert s.RelationshipUpdateDates(ends_on=None, reason='Synthetic update').model_fields_set == {'ends_on', 'reason'}
    with pytest.raises(ValidationError, match='start date is required'):
        s.RelationshipUpdateDates(starts_on=None, reason='Synthetic update')
    assert 'clear' not in s.BULK_COLLECTIONS['eligibilities']['fields']['starts_on']['how']
    assert 'clear' in s.BULK_COLLECTIONS['eligibilities']['fields']['ends_on']['how']


def test_preview_contract_keeps_handler_messages_and_executable_resolution_shapes():
    message = 'Existing handler message, preserved verbatim'
    preview = s.BulkPreview(source_fingerprint='a' * 64, counts={'problem': 1}, items=[{
        'index': 0, 'result': 'problem', 'issues': [{'code': 'DUPLICATE_RELATIONSHIP', 'message': message,
            'fixable_here': True, 'group_key': 'overlap', 'resolutions': [
                {'id': 'skip', 'label': 'Skip', 'operations': [{'skip': True}]},
                {'id': 'stop_before', 'label': 'Stop before', 'operations': [{'patch_item': {'ends_on': '2026-11-30'}}]},
                {'id': 'shift', 'label': 'Shift', 'operations': [{'item_kind': 'eligibility.update_dates',
                    'target_id': identifier(), 'expected_revision': 1, 'payload': {'starts_on': '2026-12-01'}}]}]}]}])
    assert preview.items[0].issues[0].message == message


def test_bulk_row_results_keep_the_documented_preview_and_apply_states():
    schema = s.BulkPreviewItem.model_json_schema()
    description = ' '.join(schema['description'].split())
    states = {'ready', 'unchanged', 'problem', 'waiting', 'changed', 'skipped', 'refused', 'staged'}
    assert set(schema['properties']['result']['enum']) == states
    for state in states:
        assert s.BulkPreviewItem(index=0, result=state).result == state
        assert f'{state} (' in description
    assert 'not applied, kept as a staged change to resolve later' in description
    assert 'shown to users as "Skipped"' in description
    for retired in ('fixed', 'unknown'):
        with pytest.raises(ValidationError):
            s.BulkPreviewItem(index=0, result=retired)


def test_generated_registry_and_types_share_the_exact_server_inputs():
    source, schemas = render()
    for name in ('BulkApply', 'BulkApplyResult', 'BulkPreview', 'BulkSelection', 'BulkResolved', 'BulkRowsInput', 'BulkRows',
                 'BulkStagedRow', 'BulkStagedList', 'AvailabilityUpdate', 'RelationshipUpdateDates'):
        assert (f'export type {name} =' in source or f'export interface {name} ' in source) and name in schemas
    line = next(line for line in source.splitlines() if line.startswith('export const BULK_COLLECTIONS = '))
    exported = json.loads(line.removeprefix('export const BULK_COLLECTIONS = ').removesuffix(' as const;'))
    assert exported == json.loads(json.dumps(s.bulk_collection_registry(command_roles, collection_readers)))
    assert all(collection['schema'] is None or collection['schema'] in schemas
               for collection in s.BULK_COLLECTIONS.values())


def test_bulk_settings_defaults_and_safe_bounds():
    assert s.BulkSettings().model_dump() == {'bulk_max_items': 2000, 'bulk_direct_max': 50,
        'bulk_staged_days': 14, 'rate_bulk_preview_per_minute': 30}
    with pytest.raises(ValidationError):
        s.BulkSettings(bulk_max_items=0)
    with pytest.raises(ValidationError, match='cannot exceed'):
        s.BulkSettings(bulk_max_items=5)
    with pytest.raises(ValidationError):
        s.BulkSettings(bulk_staged_days=366)
    assert s.BulkSettingsInput(bulk_max_items=5, reason='Partial limit update').bulk_max_items == 5
    with pytest.raises(ValidationError, match='cannot exceed'):
        s.BulkSettingsInput(bulk_max_items=5, bulk_direct_max=6, reason='Invalid combined limits')


def test_bulk_setting_fields_export_matches_schema_metadata():
    source, schemas = render()
    properties = s.BulkSettings.model_json_schema()['properties']
    assert {key: field['title'] for key, field in properties.items()} == {
        'bulk_max_items': 'Most items per change',
        'bulk_direct_max': 'Most applied directly',
        'bulk_staged_days': 'Days staged changes are kept',
        'rate_bulk_preview_per_minute': 'Previews per minute',
    }
    assert schemas['BulkSettings']['properties'] == properties
    line = next(line for line in source.splitlines() if line.startswith('export const BULK_SETTING_FIELDS = '))
    exported = json.loads(line.removeprefix('export const BULK_SETTING_FIELDS = ').removesuffix(' as const;'))
    assert exported == {key: {'label': field['title'], **{name: field[name] for name in (
        'description', 'minimum', 'maximum', 'default')}} for key, field in properties.items()}
    assert all(field['description'].strip() and '\n' not in field['description'] for field in exported.values())
    assert 'per person per minute' in exported['rate_bulk_preview_per_minute']['description']


def test_bulk_setting_fields_export_follows_schema_changes_without_a_second_field_list(monkeypatch):
    schema = s.BulkSettings.model_json_schema()
    schema['properties']['future_limit'] = {'type': 'integer', 'title': 'Another limit',
        'description': 'A schema-only test setting.', 'minimum': 2, 'maximum': 9, 'default': 4}
    monkeypatch.setattr(s.BulkSettings, 'model_json_schema', classmethod(lambda cls, **kwargs: schema))
    source, _ = render()
    line = next(line for line in source.splitlines() if line.startswith('export const BULK_SETTING_FIELDS = '))
    exported = json.loads(line.removeprefix('export const BULK_SETTING_FIELDS = ').removesuffix(' as const;'))
    assert exported['future_limit'] == {'label': 'Another limit', 'description': 'A schema-only test setting.',
                                      'minimum': 2, 'maximum': 9, 'default': 4}


def test_api_registry_and_generated_registry_share_roles(h):
    registry = h.get('auditor', '/api/contracts')['bulk_collections']
    assert registry == json.loads(json.dumps(s.bulk_collection_registry(command_roles, collection_readers)))


def test_real_api_preview_resolver_and_rows_match_their_public_contracts(h):
    response = h.client('credentialer').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': {
        'item_kind': 'eligibility.extend', 'items': [item(payload={'ends_on': '2027-01-01'})],
        'reason': 'Synthetic contract verification'}})
    assert response.status_code == 200, response.text
    preview = s.BulkPreview.model_validate(response.json())
    assert preview.items[0].result == 'problem' and preview.items[0].issues[0].code == 'NOT_FOUND'
    for endpoint, cls, payload in (
        ('/api/bulk/resolve', s.BulkResolved, {'collection': 'people'}),
        ('/api/bulk/rows', s.BulkRows, {'selection': {'collection': 'people'}, 'limit': 1}),
    ):
        response = h.client('coordinator').post(endpoint, json=payload)
        assert response.status_code == 200, response.text
        cls.model_validate(response.json())


def test_fair_share_contract_already_accepts_named_consultant_partners():
    value = s.BalanceGoal(kind='balance_goal', metric='pair_exposure', aim='min_per_pair', amount=1,
        partners={'conditions': [{'field': 'person_ids', 'values': [identifier()]}]})
    assert value.partners.conditions[0].field == 'person_ids'


def test_staged_api_rows_match_the_generated_contract(h):
    staged = h.command('credentialer', 'bulk.stage', {'payload': {
        'item_kind': 'eligibility.extend', 'items': [item(payload={'ends_on': '2027-01-01'})],
        'reason': 'Synthetic staged contract verification'}})['staged']
    assert s.BulkStagedRow.model_validate(staged).status == 'active'
    listed = s.BulkStagedList.model_validate(h.get('credentialer', '/api/bulk/staged'))
    assert listed.count == 1 and listed.items[0].id == staged['id']


def test_apply_and_server_built_undo_match_the_generated_contract(h):
    person = h.get('coordinator', '/api/catalogues')['people'][0]
    reply = h.command('coordinator', 'bulk.apply', {
        'item_kind': 'catalogue.update', 'common': {'catalogue': 'people'},
        'items': [{'target_id': person['id'], 'expected_revision': person['revision'],
                   'changes': [{'field': 'title', 'value': 'Contract check'}]}],
        'reason': 'Synthetic apply result contract verification'})
    result = s.BulkApplyResult.model_validate(reply)
    assert result.count == 1 and result.receipt_id == result.batch_id
    assert result.undo.kind == 'bulk.apply'
    assert s.BulkApply.model_validate(result.undo.payload).items[0].target_id == person['id']


def test_registry_action_inputs_are_schema_derived_and_use_plain_labels():
    for collection in s.BULK_COLLECTIONS.values():
        assert collection['group'] in {'People & permissions', 'Rota', 'Rules', 'Configuration'}
        descriptors = [collection, *collection['fields'].values(), *collection['columns'].values(),
                       *collection['related_columns'].values(), *collection['actions'].values()]
        for action in collection['actions'].values():
            assert isinstance(action['fields'], dict)
            assert 'predecessor_revision' not in action['fields']
            descriptors.extend(action['fields'].values())
        for descriptor in descriptors:
            label = descriptor['label']
            assert '_' not in label and 'Id' not in label, label
        for fields in [collection['fields'], *(a['fields'] for a in collection['actions'].values())]:
            for key, field in fields.items():
                assert {'label', 'kind', 'how', 'required', 'nullable', 'schema'} <= field.keys()
                if '_' in key:
                    assert field['label'] != key.replace('_', ' ').title()
                for choice in field.get('choices', []):
                    assert '_' not in choice['label'] and 'Id' not in choice['label']
    people = s.BULK_COLLECTIONS['people']['actions']
    assert people['rank']['fields']['category_id']['ref'] == 'categories'
    assert people['authorise']['fields']['service_id']['ref'] == 'services'
    assert people['grant']['fields']['capability_id']['ref'] == 'capabilities'
    assert set(people['tag']['fields']) == {'tag_ids'} and people['tag']['fields']['tag_ids']['ref'] == 'tags'
    leave = people['record_leave']['fields']
    assert set(leave) == {'kind', 'leave_type_id', 'start', 'end', 'private_reason'}
    assert leave['leave_type_id']['ref'] == 'leave_types' and leave['leave_type_id']['label'] == 'Leave type'
    assert {'value': 'recorded_leave', 'label': 'Recorded leave'} in leave['kind']['choices']
    end = s.BULK_COLLECTIONS['eligibilities']['actions']['end']['fields']['ends_on']
    assert end['required'] and not end['nullable'] and end['label'] == 'Last day'
    anonymise = s.bulk_collection_registry(command_roles)['people']['actions']['anonymise']
    assert anonymise['kind'] == 'person.anonymise' and anonymise['roles'] == ['admin']
    assert anonymise['destructive'] and not anonymise['undoable']


def test_rule_line_fields_keep_the_condition_and_number_contract():
    fields = s.BULK_COLLECTIONS['policy_lines']['fields']
    assert {'who', 'where', 'numbers', 'starts_on', 'ends_on'} <= fields.keys()
    for name in ('who', 'where'):
        assert {'$ref': '#/$defs/Conditions'} in fields[name]['schema']['anyOf']
    assert fields['numbers']['schema']['additionalProperties'] == {'$ref': '#/$defs/LineNumber'}
    assert s.BULK_COLLECTIONS['people']['actions']['rule_number']['fields']['add_lines']['schema']['items'] == {
        '$ref': '#/$defs/BulkRuleLine'}
    value = s.BulkRuleLinesInput(add_lines=[{'who': {'conditions': [{'field': 'person_ids', 'values': [identifier()]}]},
                                              'numbers': {'max': {'value': 3}}}])
    assert value.add_lines[0].id is None
    with pytest.raises(ValidationError):
        s.BulkRuleLinesInput(add_lines=[{'numbers': {'max': {'value': 3}}}], on=False)


def test_saved_views_add_conditions_date_and_layouts_without_invalidating_old_views():
    assert s.TableViewDefinition().layout == 'table'
    for layout in ('calendar', 'sheet'):
        saved = s.TableViewDefinition(layout=layout, as_of='2026-10-18', conditions={'match': 'any', 'conditions': [
            {'field': 'tags', 'values': ['student']}, {'match': 'all', 'conditions': [
                {'field': 'category_ids', 'values': [identifier()]}]}]})
        assert isinstance(saved.conditions, s.Conditions) and saved.as_of == date(2026, 10, 18)
    tree = {'conditions': [{'field': 'tags', 'values': ['student']}]}
    for _ in range(5):
        tree = {'conditions': [tree]}
    assert s.TableViewDefinition(conditions=tree).conditions
    with pytest.raises(ValidationError, match='five levels'):
        s.TableViewDefinition(conditions={'conditions': [tree]})
    with pytest.raises(ValidationError, match='cannot filter by person'):
        s.TableViewInput(table_key='planner', name='Shared', scope='department', definition={'conditions': {
            'conditions': [{'field': 'person_ids', 'values': [identifier()]}]}})


def test_generated_recursive_contracts_export_interfaces():
    source, _ = render()
    for name in ('BulkSelection', 'Conditions', 'RuleLine'):
        assert f'export interface {name} ' in source
        assert f'export type {name} = {name};' not in source

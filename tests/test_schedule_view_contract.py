"""Schedule settings extend the shared saved-view contract without changing old views."""
import json

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s
from scripts.generate_contracts import render


@pytest.mark.parametrize('mode', ['month', 'person', 'timeline'])
def test_old_schedule_settings_round_trip_without_new_fields(mode):
    old = {'schema_version': 1, 'mode': mode, 'search': 'Clinic', 'location_id': None,
           'assignment_type_id': None, 'person_id': None, 'range': None, 'days': None,
           'group_by': 'service', 'sort_by': 'time', 'colour_by': 'type', 'compact': True,
           'density': None, 'only_open': False, 'only_issues': False, 'weekdays': None,
           'week_start': None, 'time_window': None, 'properties': None, 'label': None, 'show_pairs': None}
    parsed = s.PlannerViewSettings.model_validate(old)
    assert parsed.model_dump(mode='json') == old
    assert json.loads(parsed.model_dump_json()) == old
    assert s.PlannerViewSettings.model_validate(parsed.model_dump()).model_dump() == old
    assert all(getattr(parsed, field) is None for field in ('rows', 'columns', 'subgroup', 'cells', 'collapsed', 'filters'))


def test_missing_and_explicit_null_schedule_extensions_keep_legacy_defaults():
    extensions = dict.fromkeys(('rows', 'columns', 'subgroup', 'cells', 'collapsed', 'filters'))
    expected = s.PlannerViewSettings().model_dump(mode='json')
    assert s.PlannerViewSettings.model_validate(extensions).model_dump(mode='json') == expected
    assert not extensions.keys() & expected.keys()
    assert (expected['mode'], expected['group_by'], expected['compact']) == ('week', 'service', False)


@pytest.mark.parametrize('field,values', [
    ('rows', ['type', 'service', 'location', 'time', 'person', 'rank', 'programme', 'tag', 'none']),
    ('subgroup', ['type', 'service', 'location', 'time', 'person', 'rank', 'programme', 'tag', 'none']),
    ('columns', ['day', 'week', 'weekday', 'month', 'type', 'service', 'location', 'time', 'rank', 'programme', 'tag', 'person']),
    ('cells', ['cards', 'codes', 'counts']),
    ('group_by', ['service', 'type', 'location', 'time', 'none', 'rank', 'programme', 'person', 'tag']),
    ('people_sort', ['name', 'name_desc', 'hours_asc', 'hours_desc']),
])
def test_schedule_choices_round_trip_and_reject_unknown_choices(field, values):
    for value in values:
        parsed = s.PlannerViewSettings.model_validate({'group_by': 'none', field: value})
        assert parsed.model_dump(mode='json')[field] == value
    with pytest.raises(ValidationError):
        s.PlannerViewSettings.model_validate({field: 'unsupported'})


@pytest.mark.parametrize('row', ['type', 'service', 'location', 'time', 'person', 'rank', 'programme', 'tag'])
def test_explicit_rows_and_subgroup_must_differ(row):
    with pytest.raises(ValidationError, match='must be different dimensions'):
        s.PlannerViewSettings(rows=row, subgroup=row, group_by='none')
    assert s.PlannerViewSettings(rows=row, subgroup=None, group_by='none').rows == row
    assert s.PlannerViewSettings(rows=None, subgroup=row, group_by='none').subgroup == row


@pytest.mark.parametrize('axes', [('rows', 'columns'), ('rows', 'group_by'), ('rows', 'subgroup'),
                                 ('columns', 'group_by'), ('columns', 'subgroup'), ('group_by', 'subgroup')])
def test_duty_time_cannot_repeat_across_any_axes(axes):
    with pytest.raises(ValidationError, match='must be different dimensions'):
        s.PlannerViewSettings.model_validate({'group_by': 'none', **dict.fromkeys(axes, 'time')})


def test_configurable_axes_cells_and_collapsed_groups_survive_both_input_paths():
    planner = {'mode': 'timeline', 'rows': 'person', 'columns': 'week', 'subgroup': 'rank',
               'cells': 'codes', 'collapsed': ['rank:consultant', 'programme:synthetic'], 'group_by': 'programme'}
    legacy = s.PlannerViewInput(name='By rank', definition=planner)
    shared = s.TableViewInput(table_key='planner', name='By rank', definition={'planner': planner})
    for settings in (legacy.definition, shared.definition.planner):
        assert settings.model_dump(mode='json', exclude_unset=True) == planner
        assert s.PlannerViewSettings.model_validate(settings.model_dump()).collapsed == planner['collapsed']


def test_collapsed_groups_accept_the_documented_bounds():
    value = ['x' * 160] * 200
    assert s.PlannerViewSettings(collapsed=value).collapsed == value
    assert s.PlannerViewSettings(collapsed=[]).model_dump()['collapsed'] == []


@pytest.mark.parametrize('collapsed', [['x'] * 201, ['x' * 161], 'group', [{'key': 'group'}]])
def test_collapsed_groups_reject_unbounded_or_structured_values(collapsed):
    with pytest.raises(ValidationError):
        s.PlannerViewSettings(collapsed=collapsed)


def test_schedule_filters_use_the_same_collection_shape_for_every_required_dimension():
    filters = {field: ['synthetic-id'] for field in (
        'person_ids', 'category_ids', 'programme_ids', 'tags', 'capability_ids', 'service_ids',
        'location_ids', 'assignment_type_ids')}
    filters.update(weekday=['0', '6'], consultation_mode=['remote'], only_open=['true'], only_issues=['true'])
    filters['custom_collection_key'] = ['kept']
    planner = s.PlannerViewSettings(filters=filters)
    collection = s.TableViewDefinition(filters=filters, planner={'filters': filters})
    assert planner.model_dump(mode='json')['filters'] == collection.filters == filters
    assert collection.model_dump(mode='json')['planner']['filters'] == filters
    assert s.PlannerViewSettings(filters={}).model_dump()['filters'] == {}


def test_shared_collection_filter_bounds_remain_available_to_schedule_views():
    filters = {f'field_{i}': ['x' * 160] * 50 for i in range(20)}
    filters['x' * 40] = filters.pop('field_0')
    assert s.TableViewDefinition(filters=filters).filters == filters
    assert s.PlannerViewSettings(filters=filters).filters == filters


@pytest.mark.parametrize('filters', [
    {f'field_{i}': ['value'] for i in range(21)}, {'person_ids': ['person'] * 51},
    {'person_ids': ['x' * 161]}, {'bad key!': ['value']}, {'x' * 41: ['value']},
    {'person_ids': 'person'}, {'person_ids': [{'id': 'person'}]}, {'person_ids': [True]},
    {'person_ids': [['person']]}, [{'field': 'person_ids', 'values': ['person']}],
    {'match': 'all', 'conditions': [{'field': 'person_ids', 'values': ['person']}]},
])
def test_schedule_and_collection_filters_reject_the_same_invalid_shapes(filters):
    for model in (s.TableViewDefinition, s.PlannerViewSettings):
        with pytest.raises(ValidationError):
            model(filters=filters)


def test_property_keys_append_without_reordering_and_keep_existing_visibility():
    old = ('label', 'time', 'lead', 'overtime', 'supervisor', 'open', 'rule', 'code', 'location')
    new = ('duty', 'service', 'hours', 'places', 'tags', 'people', 'leave')  # added hidden: nothing new shows by default (D80)
    assert s.VIEW_PROPERTIES == old + new
    for key in old:
        assert s.ViewProperty(key=key).on is (key != 'code')
    for key in new:
        assert s.ViewProperty(key=key).on is False
        assert s.ViewProperty(key=key, on=True).on is True
    properties = [{'key': key} for key in s.VIEW_PROPERTIES]
    saved = s.PlannerViewSettings(properties=properties).model_dump(mode='json')['properties']
    assert [p['key'] for p in saved] == list(s.VIEW_PROPERTIES)
    assert [p['on'] for p in saved] == [key not in ('code', *new) for key in s.VIEW_PROPERTIES]
    with pytest.raises(ValidationError):
        s.ViewProperty(key='note')


@pytest.mark.parametrize('scope', ['personal', 'department'])
def test_both_saved_planner_inputs_keep_scope(scope):
    shared = s.TableViewInput(table_key='planner', name='Shared contract', definition={}, scope=scope)
    legacy = s.PlannerViewInput(name='Legacy contract', definition={}, scope=scope)
    assert shared.model_dump()['scope'] == legacy.model_dump()['scope'] == scope


def test_saved_planner_scope_defaults_to_personal_and_rejects_unknown_scope():
    for model, payload in ((s.TableViewInput, {'table_key': 'planner'}), (s.PlannerViewInput, {})):
        assert model(name='Personal', definition={}, **payload).scope == 'personal'
        with pytest.raises(ValidationError):
            model(name='Unknown', definition={}, scope='organisation', **payload)


def test_generated_schedule_filters_keep_shared_collection_value_type():
    typescript, _ = render()
    planner = typescript.split('export type PlannerViewSettings = ', 1)[1].split('\n};', 1)[0]
    assert '"filters"?: Record<string, Array<string>> | null;' in planner


def test_coverage_keeps_its_own_optional_axes():
    """D80: Coverage's Rows and Columns are its own (PlannerViewSettings.coverage); absent is the default and not stored."""
    assert 'coverage' not in s.PlannerViewSettings().model_dump()
    saved = s.PlannerViewSettings(mode='coverage', columns='week', coverage={'rows': 'service'}).model_dump(mode='json')
    assert saved['coverage'] == {'rows': 'service'} and saved['columns'] == 'week'
    with pytest.raises(ValidationError):
        s.PlannerViewSettings(coverage={'rows': 'service', 'columns': 'service'})

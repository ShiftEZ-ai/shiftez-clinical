"""Bounded print presets and independent schedule dimensions share the saved-view contracts."""
from itertools import combinations
import uuid

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s
from scripts.generate_contracts import render


def test_print_defaults_and_optional_definition_preserve_old_views():
    assert s.PrintSettings().model_dump(mode='json') == {
        'schema_version': 1, 'paper': 'a4', 'orientation': 'landscape', 'scale': 'fit_width',
        'scale_percent': None, 'margins': 'normal', 'colour': 'colour', 'text_size': 'normal',
        'include': ['title', 'period', 'status', 'key', 'page_numbers', 'printed_by'],
        'repeat_headers': True, 'page_break': 'none', 'range': 'view', 'view_id': None, 'format': 'print'}
    assert 'print' not in s.TableViewDefinition().model_dump(mode='json')
    assert 'print' not in s.TableViewDefinition(print=None).model_dump(mode='json')
    assert s.TableViewInput(table_key='schedule_print', name='Print', definition={'print': {}}).definition.print


@pytest.mark.parametrize('field,values', [
    ('paper', ['a4', 'a3', 'letter', 'legal']), ('orientation', ['landscape', 'portrait']),
    ('scale', ['fit_width', 'fit_page', 'actual', 'custom']), ('margins', ['narrow', 'normal', 'wide']),
    ('colour', ['colour', 'greyscale']), ('text_size', ['small', 'normal', 'large']),
    ('page_break', ['none', 'group', 'week', 'month']), ('range', ['view', 'week', '2weeks', 'month', 'rota']),
    ('format', ['print', 'png', 'csv']),
])
def test_print_choices_are_explicit_and_round_trip(field, values):
    for value in values:
        settings = {field: value, **({'scale_percent': 25} if field == 'scale' and value == 'custom' else {})}
        assert s.PrintSettings.model_validate(settings).model_dump()[field] == value
    with pytest.raises(ValidationError):
        s.PrintSettings.model_validate({field: 'unsupported'})


@pytest.mark.parametrize('settings', [
    {'schema_version': 2}, {'scale': 'custom'}, {'scale': 'custom', 'scale_percent': 24},
    {'scale': 'custom', 'scale_percent': 201}, {'scale_percent': 100}, {'scale': 'actual', 'scale_percent': 100},
    {'include': ['title', 'title']}, {'include': ['unknown']}, {'include': ['title'] * 9},
    {'view_id': 'invalid'}, {'unknown': True},
])
def test_invalid_print_settings_are_rejected(settings):
    with pytest.raises(ValidationError):
        s.PrintSettings.model_validate(settings)


def test_print_bounds_and_all_inclusions():
    for percent in (25, 200):
        assert s.PrintSettings(scale='custom', scale_percent=percent).scale_percent == percent
    include = ['title', 'period', 'status', 'key', 'filters', 'page_numbers', 'printed_by', 'signature']
    settings = s.PrintSettings(include=include, repeat_headers=False, view_id=str(uuid.uuid4()))
    assert settings.include == include and settings.repeat_headers is False
    assert s.PrintSettings(include=[]).include == []


@pytest.mark.parametrize('table,definition', [
    ('planner', {'print': {}}), ('people', {'print': {}}), ('schedule_print', {}), ('schedule_print', {'print': None}),
])
def test_print_settings_are_required_only_on_print_presets(table, definition):
    with pytest.raises(ValidationError):
        s.TableViewInput(table_key=table, name='Invalid', definition=definition)


@pytest.mark.parametrize('pair', list(combinations(('rows', 'columns', 'group_by', 'subgroup'), 2)))
def test_every_pair_of_categorical_axes_must_differ(pair):
    for dimension in ('type', 'service', 'location', 'rank', 'programme', 'tag', 'person'):
        settings = {'group_by': 'none', **dict.fromkeys(pair, dimension)}
        with pytest.raises(ValidationError, match='must be different dimensions'):
            s.PlannerViewSettings.model_validate(settings)


def test_time_axes_and_absent_dimensions_remain_valid():
    for columns in ('day', 'week', 'weekday', 'month'):
        settings = s.PlannerViewSettings(rows='tag', columns=columns, group_by='type', subgroup='person')
        assert settings.columns == columns and settings.rows == 'tag'
    assert s.PlannerViewSettings(rows='none', group_by='none', subgroup='none').subgroup == 'none'
    assert s.PlannerViewSettings().group_by == 'service'


@pytest.mark.parametrize('payload', [
    {'table_key': 'people'}, {'table_key': 'people', 'view_id': 'invalid'},
    {'table_key': 'People!', 'view_id': None}, {'table_key': 'people', 'scope': 'organisation', 'view_id': None},
    {'table_key': 'people', 'clear': True, 'view_id': str(uuid.uuid4())},
])
def test_default_choice_rejects_ambiguous_or_invalid_inputs(payload):
    with pytest.raises(ValidationError):
        s.TableViewDefaultInput.model_validate(payload)


def test_default_choice_and_print_types_are_generated():
    assert s.TableViewDefaultInput(table_key='people', view_id=None).clear is False
    assert s.TableViewDefaultInput(table_key='people', clear=True).view_id is None
    typescript, _ = render()
    for name in ('PrintSettings', 'TableViewDefaultInput', 'TableViewDefaults'):
        assert 'export type ' + name + ' = ' in typescript
    assert '"print"?: PrintSettings | null;' in typescript
    assert '"table_view.set_default"' in typescript

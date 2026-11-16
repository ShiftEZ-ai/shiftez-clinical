"""Export quality/wrapping use the existing versioned preset path, never clinical settings."""
import json
from typing import get_args

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s
from scripts.generate_contracts import render


def test_legacy_print_presets_keep_their_existing_serialization():
    old = s.PrintSettings().model_dump(mode='json')
    assert not {'image_dpi', 'wrap'} & old.keys()
    assert s.PrintSettings(image_dpi=None, wrap=None).model_dump(mode='json') == old
    assert s.PRINT_IMAGE_DPI_DEFAULT in s.PRINT_IMAGE_DPI
    assert get_args(s.PrintSettings.model_fields['image_dpi'].annotation.__args__[0]) == tuple(s.PRINT_IMAGE_DPI)
    source, _ = render()
    for name in ('PRINT_IMAGE_DPI', 'PRINT_IMAGE_DPI_DEFAULT', 'PRINT_WRAP_DEFAULT'):
        assert f'export const {name} = {json.dumps(getattr(s, name))} as const;' in source


@pytest.mark.parametrize('invalid', [{'image_dpi': value} for value in (0, 72, 144, 600, '300', True)] +
                         [{'wrap': value} for value in ('false', 0, 1, [], {})])
def test_export_settings_refuse_unknown_quality_and_coerced_booleans(invalid):
    with pytest.raises(ValidationError):
        s.PrintSettings(**invalid)


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_quality_and_wrapping_round_trip_revision_undo_and_reset(h):
    def payload(settings):
        return {'table_key': 'schedule_print', 'name': 'Synthetic sharp export',
                'definition': {'print': settings}}

    def read(view):
        return next(row for row in h.get('coordinator', '/api/table-views?table=schedule_print')['views']
                    if row['id'] == view['id'])

    before = h.get('coordinator', '/api/pulse')['input_revision']
    view = h.command('coordinator', 'table_view.create', payload({'image_dpi': 300, 'wrap': False}))['view']
    assert read(view)['definition']['print']['image_dpi'] == 300
    assert read(view)['definition']['print']['wrap'] is False
    changed = h.command('coordinator', 'table_view.update', payload({'image_dpi': 150, 'wrap': True}),
                        view['id'], 1)
    assert changed['view']['definition']['print']['image_dpi'] == 150
    undone = h.command('coordinator', 'table_view.update', changed['previous_view'], view['id'], 2)['view']
    assert read(undone)['definition']['print']['wrap'] is False
    assert read(undone)['definition']['print']['image_dpi'] == 300
    h.command('coordinator', 'table_view.update', payload({'image_dpi': 96}), view['id'], 1, status=409)
    reset = h.command('coordinator', 'table_view.update', payload({'image_dpi': None, 'wrap': None}),
                      view['id'], 3)['view']
    assert not {'image_dpi', 'wrap'} & reset['definition']['print'].keys()
    assert h.get('coordinator', '/api/pulse')['input_revision'] == before


def test_cause_group_is_a_canonical_saved_filter_and_sort_key():
    fields = s.table_condition_definitions('resolve-issues')
    assert any(field['key'] == 'cause' for field in fields)
    view = s.TableViewInput(table_key='resolve-issues', name='Shared cause grouping', definition={
        'group': 'cause', 'group_sort': [['cause', 'desc']], 'filters': {'cause': ['No eligible people']},
        'conditions': {'match': 'all', 'conditions': [{'field': 'cause', 'op': 'any', 'values': ['No eligible people']}]}})
    assert view.definition.group_sort == [('cause', 'desc')]

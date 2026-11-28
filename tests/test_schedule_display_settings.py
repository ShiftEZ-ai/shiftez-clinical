"""Optional display settings reuse saved-view validation, revisions and Undo; clinical inputs stay intact."""
import json
from typing import get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s
from scripts.generate_contracts import render


SETTINGS = {'person_display': 'team', 'group_by': 'service', 'subgroup': 'location',
            'group_sort': [['service', 'desc'], ['location', 'asc']],
            'widths': {'row_heading': 240, 'date': 144.5, 'cell': 180},
            'layers': {'draft': True, 'leave': False, 'mine': False, 'mode': 'overlay', 'rotations': False}}


def test_display_extensions_are_absent_from_old_views_and_null_or_empty_resets():
    for model, fields in ((s.PlannerViewSettings, ('person_display', 'group_sort', 'widths')),
                          (s.TableViewDefinition, ('widths', 'group_sort'))):
        old = model().model_dump(mode='json')
        assert not old.keys() & set(fields)
        assert model(**dict.fromkeys(fields)).model_dump(mode='json') == old
        empty = {key: [] if key == 'group_sort' else {} for key in fields if key != 'person_display'}
        assert model(**empty).model_dump(mode='json') == old
    assert s.PLANNER_PERSON_DISPLAY_DEFAULT == 'duty'
    assert s.PlannerViewSettings(person_display='duty').model_dump()['person_display'] == 'duty'


@pytest.mark.parametrize('display', s.PLANNER_PERSON_DISPLAYS)
def test_projection_saves_only_a_display_choice(display):
    value = s.PlannerViewSettings(person_display=display).model_dump(mode='json')
    assert value['person_display'] == display
    assert value['person_id'] is None
    assert not {'workspace_id', 'participants', 'name_legend'} & value.keys()
    for invalid in ('all', {'person_id': str(uuid4())}, ['person'], True):
        with pytest.raises(ValidationError):
            s.PlannerViewSettings(person_display=invalid)


@pytest.mark.parametrize('bad', [
    [['service', 'asc'], ['service', 'desc']], [['rank', 'asc']], [['none', 'asc']],
    [['service', 'up']], [['team', 'asc']], [['service', 'asc', 'extra']],
])
def test_group_order_uses_existing_sort_tuples_for_unique_active_levels(bad):
    with pytest.raises(ValidationError):
        s.PlannerViewSettings(group_by='service', subgroup='location', group_sort=bad)


@pytest.mark.parametrize('bad', [
    [['family', 'asc'], ['family', 'desc']], [['name', 'asc']],
    [['family', 'up']], [['family', 'asc', 'extra']],
])
def test_collection_group_order_rejects_inactive_or_repeated_properties(bad):
    with pytest.raises(ValidationError):
        s.TableViewDefinition(group='family', then='kind', group_sort=bad)


@pytest.mark.parametrize('bad', [0, -1, float('nan'), float('inf'), float('-inf'), True, '240', None])
def test_widths_require_positive_finite_numbers(bad):
    for model, key in ((s.PlannerViewSettings, 'row_heading'), (s.TableViewDefinition, 'name')):
        with pytest.raises(ValidationError):
            model(widths={key: bad})


def test_width_keys_reuse_known_collection_columns_and_schedule_roles():
    for table, key in (('people', 'name'), ('rule-tree', 'where'), ('planner-list', 'when')):
        view = s.TableViewInput(table_key=table, name='Widths', definition={'widths': {key: 200}})
        assert view.definition.widths == {key: 200}
        with pytest.raises(ValidationError, match='known columns'):
            s.TableViewInput(table_key=table, name='Widths', definition={'widths': {'unknown': 200}})
    with pytest.raises(ValidationError, match='Unknown schedule width'):
        s.PlannerViewSettings(widths={str(uuid4()): 200})
    with pytest.raises(ValidationError):
        s.TableViewDefinition(widths={f'column_{i}': 200 for i in range(31)})


def test_generated_metadata_covers_existing_dimensions_sorts_properties_and_display():
    assert tuple(item['key'] for item in s.PLANNER_DIMENSIONS) == get_args(s.PlannerDimension)
    assert get_args(s.PlannerRow) == (*get_args(s.PlannerDimension), 'none')
    columns = get_args(s.PlannerViewSettings.model_fields['columns'].annotation.__args__[0])
    assert set(columns) == set(get_args(s.PlannerDimension)) | {key for key, label in s.PLANNER_TIME_AXES}
    for field, labels in (('sort_by', s.PLANNER_SORT_LABELS), ('colour_by', s.PLANNER_COLOUR_LABELS)):
        assert tuple(labels) == get_args(s.PlannerViewSettings.model_fields[field].annotation)
    assert tuple(s.VIEW_PROPERTY_DEFINITIONS) == s.VIEW_PROPERTIES
    for item in s.VIEW_PROPERTY_DEFINITIONS.values():
        assert set(item['forms']) <= set(get_args(s.ViewProperty.model_fields['form'].annotation))
    source, _ = render()
    for name in ('PLANNER_DIMENSIONS', 'PLANNER_TIME_AXES', 'VIEW_SORT_DIRECTIONS', 'PLANNER_SORT_LABELS',
                 'PLANNER_PEOPLE_SORT_LABELS', 'PLANNER_COLOUR_LABELS', 'PLANNER_PERSON_DISPLAYS',
                 'PLANNER_PERSON_DISPLAY_DEFAULT', 'PLANNER_WIDTH_KEYS', 'VIEW_PROPERTY_DEFINITIONS', 'TABLE_GROUP_FIELDS'):
        assert f'export const {name} = {json.dumps(getattr(s, name))} as const;' in source


@pytest.mark.parametrize('legacy', [False, True])
def test_both_saved_view_paths_keep_display_settings_and_revision_checked_undo(h, legacy):
    prefix, endpoint, role = ('planner_view', '/api/planner/views', 'coordinator') if legacy else (
        'table_view', '/api/table-views?table=planner', 'staff')

    def payload(settings):
        return {'name': 'Synthetic display choices', 'definition': settings if legacy else {'planner': settings},
                **({} if legacy else {'table_key': 'planner'})}

    def settings(view):
        return view['definition'] if legacy else view['definition']['planner']

    def read(view):
        return next(row for row in h.get(role, endpoint)['views'] if row['id'] == view['id'])

    input_revision = h.get('coordinator', '/api/pulse')['input_revision']
    old = h.command(role, prefix + '.create', payload({}))['view']
    assert 'person_display' not in settings(read(old))
    changed = h.command(role, prefix + '.update', payload(SETTINGS), old['id'], old['revision'])
    saved = read(old)
    assert all(settings(saved)[key] == value for key, value in SETTINGS.items())
    assert changed['previous_view']['definition'] == old['definition']
    undone = h.command(role, prefix + '.update', changed['previous_view'], old['id'], saved['revision'])['view']
    assert read(old)['definition'] == old['definition']
    h.command(role, prefix + '.update', payload(SETTINGS), old['id'], saved['revision'], status=409)
    assert undone['revision'] == 3
    assert h.get('coordinator', '/api/pulse')['input_revision'] == input_revision


def test_collection_width_resize_reset_and_undo_share_existing_order_path(h):
    payload = {'table_key': 'rule-tree', 'name': 'Synthetic rules widths',
               'definition': {'widths': {'where': 240}, 'order': ['where', 'who'],
                              'group': 'family', 'then': 'kind',
                              'group_sort': [['family', 'desc'], ['kind', 'asc']]}}
    saved = h.command('coordinator', 'table_view.create', payload)['view']
    reset = h.command('coordinator', 'table_view.update', {**payload, 'definition': {'widths': {}, 'order': []}},
                      saved['id'], saved['revision'])
    assert 'widths' not in reset['view']['definition']
    restored = h.command('coordinator', 'table_view.update', reset['previous_view'], saved['id'], 2)['view']
    assert restored['definition']['widths'] == {'where': 240}
    assert restored['definition']['order'] == ['where', 'who']
    assert restored['definition']['group_sort'] == [['family', 'desc'], ['kind', 'asc']]


@pytest.mark.parametrize('legacy', [False, True])
def test_display_choices_keep_shared_person_reference_guards_and_no_workspace_ids(h, legacy):
    prefix = 'planner_view' if legacy else 'table_view'

    def payload(settings, scope='department'):
        return {'name': 'Synthetic shared projection', 'scope': scope,
                'definition': settings if legacy else {'planner': settings},
                **({} if legacy else {'table_key': 'planner'})}

    saved = h.command('coordinator', prefix + '.create', payload(SETTINGS))['view']
    for private in ({'person_id': h.seed['people'][0]}, {'filters': {'person_ids': [h.seed['people'][0]]}},
                    {'search_options': {'reference': {'kind': 'people', 'id': h.seed['people'][0]}}}):
        h.command('coordinator', prefix + '.update', payload({**SETTINGS, **private}), saved['id'], 1, status=422)
    for unexpected in ({'workspace_id': str(uuid4())}, {'name_legend': {'Code': 'Personal name'}}):
        h.command('coordinator', prefix + '.update', payload({**SETTINGS, **unexpected}), saved['id'], 1, status=422)
    h.command('coordinator', prefix + '.create', payload({**SETTINGS, 'person_id': str(uuid4())}, 'personal'), status=404)


def test_supervision_teams_and_rule_marks_forms_and_the_issue_list_limit():
    """D78: View › Supervision teams is Tree | Box (the first form is the built-in default); a view saved before it
    with "text"/"icon" still validates (the web opens it as the first form, so no migration). Rule marks has one form
    (could-improve marks on or off). An issue popover's length is the contract's ISSUE_LIST_LIMIT, sent to the web."""
    assert s.VIEW_PROPERTY_DEFINITIONS['supervisor']['forms'] == ('tree', 'box')
    assert s.VIEW_PROPERTY_DEFINITIONS['rule']['forms'] == ('icon',)
    for form in ('tree', 'box', 'text', 'icon'):
        assert s.ViewProperty(key='supervisor', form=form).form == form
    assert isinstance(s.ISSUE_LIST_LIMIT, int) and s.ISSUE_LIST_LIMIT >= 1
    source, _ = render()
    assert f'export const ISSUE_LIST_LIMIT = {s.ISSUE_LIST_LIMIT} as const;' in source

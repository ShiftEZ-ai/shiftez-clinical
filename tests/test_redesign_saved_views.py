"""Contract-backed display settings remain versioned, private and reversible on both databases."""
from copy import deepcopy

import pytest


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('table', ['planner', 'people', 'rule-tree'])
def test_display_widths_group_orders_and_person_display_are_revisioned(database, table, request):
    h = request.getfixturevalue(database)
    definition = ({'planner': {'mode': 'week', 'group_by': 'service', 'subgroup': 'type',
        'group_sort': [['service', 'desc']], 'person_display': 'team',
        'filters': {'person_ids': [h.seed['people'][0]]}, 'widths': {'row_heading': 220, 'date': 180, 'cell': 240}}}
        if table == 'planner' else {'group': 'rank', 'then': 'skills', 'group_sort': [['rank', 'desc']],
                                   'widths': {'name': 220, 'rank': 128}}
        if table == 'people' else {'group': 'family', 'widths': {'who': 220, 'where': 180, 'numbers': 128}})
    body = {'table_key': table, 'name': 'Fictional display contract', 'definition': definition}
    original = h.command('coordinator', 'table_view.create', body)['view']
    changed_definition = deepcopy(definition)
    settings = changed_definition.get('planner') or changed_definition
    settings['widths'] = {key: width + 16 for key, width in settings['widths'].items()}
    changed = h.command('coordinator', 'table_view.update', {**body, 'definition': changed_definition}, original['id'], original['revision'])
    stored = h.get('coordinator', '/api/table-views?table=' + table)['views'][0]
    assert (stored['definition'].get('planner') or stored['definition'])['widths'] == settings['widths']
    h.command('coordinator', 'table_view.update', body, original['id'], original['revision'], status=409)
    restored = h.command('coordinator', 'table_view.update', changed['previous_view'], original['id'], stored['revision'])['view']
    assert restored['revision'] > stored['revision'] > original['revision']
    assert restored['definition'] == original['definition']
    assert h.get('admin', '/api/table-views?table=' + table)['views'] == []
    invalid = deepcopy(definition)
    (invalid.get('planner') or invalid)['widths']['unknown_column'] = 220
    h.command('coordinator', 'table_view.update', {**body, 'definition': invalid}, restored['id'], restored['revision'], status=422)
    assert h.get('coordinator', '/api/table-views?table=' + table)['views'][0] == restored

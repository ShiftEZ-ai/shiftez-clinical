"""Timeline display choices survive saved-view create, edit and reload."""
import pytest


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_timeline_bar_settings_save_and_reload(database, request):
    h = request.getfixturevalue(database)
    value = {'table_key': 'rotation-board', 'name': 'Synthetic rotation view',
             'definition': {'layout': 'timeline', 'timeline': {'bar_label': 'name', 'colour_by': 'service'}}}
    view = h.command('coordinator', 'table_view.create', value)['view']
    saved = h.get('coordinator', '/api/table-views?table=rotation-board')['views'][0]
    assert (saved['definition']['timeline']['bar_label'], saved['definition']['timeline']['colour_by']) == ('name', 'service')
    value['definition']['timeline'] = {'bar_label': 'dates', 'colour_by': 'cohort'}
    h.command('coordinator', 'table_view.update', value, view['id'], view['revision'])
    saved = h.get('coordinator', '/api/table-views?table=rotation-board')['views'][0]
    assert (saved['definition']['timeline']['bar_label'], saved['definition']['timeline']['colour_by']) == ('dates', 'cohort')


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_timeline_bar_settings_reject_unknown_state_and_keep_old_defaults(database, request):
    h = request.getfixturevalue(database)
    value = {'table_key': 'rotation-board', 'name': 'Synthetic legacy view',
             'definition': {'layout': 'timeline', 'timeline': {'zoom': 'month'}}}
    view = h.command('coordinator', 'table_view.create', value)['view']
    for invalid in ({'bar_label': 'length'}, {'colour_by': 'person'}):
        h.command('coordinator', 'table_view.update', {**value, 'definition': {'timeline': invalid}},
                  view['id'], view['revision'], status=422)
    saved = h.get('coordinator', '/api/table-views?table=rotation-board')['views'][0]
    assert saved['revision'] == view['revision']
    assert (saved['definition']['timeline']['bar_label'], saved['definition']['timeline']['colour_by']) == ('name_length', 'rotation_type')

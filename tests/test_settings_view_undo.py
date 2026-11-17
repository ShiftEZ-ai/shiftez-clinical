"""Undo inputs retain the same ownership, revision, scope and view-count boundaries as ordinary saves."""
import uuid

import pytest

from packages.contracts.schemas import DisplaySettingsInput
from .test_table_views import definition


def test_display_previous_values_restore_defaults_and_reject_stale_or_unauthorised_undo(h):
    dept = h.get('admin', '/api/me')['department']
    original = DisplaySettingsInput.model_validate({**(dept['display_settings'] or {}), 'reason': 'Read defaults'}).model_dump(
        mode='json', exclude={'reason'})
    saved = h.command('admin', 'department.display', {'names_default': 'code', 'rule_catalogue': True, 'reason': 'Change display'},
                      dept['id'], dept['revision'])
    assert saved['previous'] == original
    undo = {**saved['previous'], 'reason': 'Undo display'}
    h.command('coordinator', 'department.display', undo, dept['id'], saved['department']['revision'], status=403)
    h.command('admin', 'department.display', undo, dept['id'], dept['revision'], status=409)
    restored = h.command('admin', 'department.display', undo, dept['id'], saved['department']['revision'])['department']
    assert restored['display_settings'] == original
    assert h.get('staff', '/api/me')['department']['display_settings'] == original


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_first_display_change_restores_effective_defaults(request, database):
    h = request.getfixturevalue(database)
    department = h.get('admin', '/api/me')['department']
    assert not department['display_settings']
    defaults = DisplaySettingsInput(reason='Read defaults').model_dump(mode='json', exclude={'reason'})
    saved = h.command('admin', 'department.display', {'names_default': 'code', 'rule_catalogue': True,
        'reason': 'First display change'}, department['id'], department['revision'])
    assert saved['previous'] == defaults
    undo = saved['undo']
    restored = h.command('admin', undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'])['department']
    assert restored['display_settings'] == defaults
    assert h.get('staff', '/api/me')['department']['display_settings'] == defaults


def test_saved_view_update_returns_checked_previous_definition_without_cross_owner_access(h):
    value = {'table_key': 'people', 'name': 'Original view', 'definition': definition(q='Alex')}
    original = h.command('staff', 'table_view.create', value)['view']
    changed = h.command('staff', 'table_view.update', {**value, 'name': 'Changed view', 'definition': definition(q='Jordan')},
                        original['id'], original['revision'])
    assert changed['previous_view'] == {k: original[k] for k in ('table_key', 'name', 'definition', 'scope')}
    h.command('coordinator', 'table_view.update', changed['previous_view'], original['id'], changed['view']['revision'], status=404)
    h.command('staff', 'table_view.update', changed['previous_view'], original['id'], original['revision'], status=409)
    h.command('staff', 'table_view.update', {**changed['previous_view'], 'table_key': 'requests'},
              original['id'], changed['view']['revision'], status=409)
    restored = h.command('staff', 'table_view.update', changed['previous_view'], original['id'], changed['view']['revision'])['view']
    assert (restored['name'], restored['definition']) == (original['name'], original['definition'])
    assert restored['owner_id'] == original['owner_id'] and restored['revision'] == original['revision'] + 2


def test_deleted_view_can_be_recreated_only_as_an_owned_valid_view_within_the_limit(h):
    value = {'table_key': 'people', 'name': 'Recover this view', 'definition': definition(q='Alex')}
    original = h.command('staff', 'table_view.create', value)['view']
    h.command('coordinator', 'table_view.delete', {}, original['id'], original['revision'], status=404)
    h.command('staff', 'table_view.delete', {}, original['id'], original['revision'] + 1, status=409)
    deleted = h.command('staff', 'table_view.delete', {}, original['id'], original['revision'])
    assert deleted['deleted_view_id'] == original['id']
    assert h.get('staff', '/api/table-views?table=people')['views'] == []
    recovered = h.command('staff', 'table_view.create', deleted['previous_view'])['view']
    assert recovered['id'] != original['id'] and recovered['owner_id'] == original['owner_id']
    assert {k: recovered[k] for k in ('table_key', 'name', 'definition', 'scope')} == deleted['previous_view']
    assert h.get('coordinator', '/api/table-views?table=people')['views'] == []
    h.command('staff', 'table_view.create', {**deleted['previous_view'], 'owner_id': h.seed['identities']['coordinator']}, status=422)
    for i in range(19):
        h.command('staff', 'table_view.create', {**value, 'name': 'Extra view ' + str(i)})
    h.command('staff', 'table_view.create', deleted['previous_view'], status=409)
    assert len(h.get('staff', '/api/table-views?table=people')['views']) == 20


def test_recreating_deleted_planner_view_revalidates_its_references(h):
    original = h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Scoped planner',
        'definition': {'planner': {'location_id': h.seed['locations'][0]}}})['view']
    deleted = h.command('coordinator', 'table_view.delete', {}, original['id'], original['revision'])
    invalid = {**deleted['previous_view'], 'definition': {'planner': {'location_id': str(uuid.uuid4())}}}
    h.command('coordinator', 'table_view.create', invalid, status=404)
    assert h.get('coordinator', '/api/table-views?table=planner')['views'] == []
    restored = h.command('coordinator', 'table_view.create', deleted['previous_view'])['view']
    assert restored['definition'] == original['definition']

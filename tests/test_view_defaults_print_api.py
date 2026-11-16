"""Shared collections, default precedence and print presets retain privacy, audit and Undo."""
import importlib
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from apps.api import models as m
from apps.api.db import transaction
from migrations import safety
from packages.contracts import schemas as s
from .governance_support import identity


def create(h, table='people', role='coordinator', scope='personal', definition=None, status=200):
    return h.command(role, 'table_view.create', {'table_key': table, 'name': 'Synthetic saved view',
        'scope': scope, 'definition': definition or {}}, status=status)


def listing(h, table='people', role='staff'):
    return h.get(role, '/api/table-views?table=' + table)


def choose(h, view_id=None, *, table='people', scope='personal', clear=False, role='staff', **extra):
    return h.command(role, 'table_view.set_default',
        {'table_key': table, 'scope': scope, 'view_id': view_id, 'clear': clear}, **extra)


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def database(request):
    return request.getfixturevalue(request.param)


@pytest.mark.parametrize('table', ['people', 'rule-tree', 'fairness', 'bulk-eligibilities', 'schedule_print', 'custom-list'])
def test_department_sharing_and_scope_undo_work_for_every_collection(h, table):
    definition = {'print': {}} if table == 'schedule_print' else {}
    saved = create(h, table, scope='department', definition=definition)['view']
    assert listing(h, table)['views'] == [{**saved, 'can_edit': False}]
    value = {'table_key': table, 'name': 'Renamed shared view', 'definition': definition}
    h.command('staff', 'table_view.update', value, saved['id'], saved['revision'], status=403)
    changed = h.command('admin', 'table_view.update', value, saved['id'], saved['revision'])['view']
    assert changed['scope'] == 'department' and changed['revision'] == 2
    personal = h.command('admin', 'table_view.update', {**value, 'scope': 'personal'},
                         saved['id'], changed['revision'])
    assert personal['previous_view']['scope'] == 'department' and listing(h, table)['views'] == []
    restored = h.command('admin', 'table_view.update', personal['previous_view'],
                         saved['id'], personal['view']['revision'])['view']
    assert restored['scope'] == 'department' and listing(h, table)['views'][0]['id'] == saved['id']


@pytest.mark.parametrize('table', ['people', 'rule-tree', 'fairness', 'bulk-eligibilities', 'schedule_print'])
def test_shared_collections_keep_person_filter_privacy(h, table):
    definition = {'filters': {'person_ids': [h.seed['people'][0]]}}
    if table == 'schedule_print':
        definition['print'] = {}
    create(h, table, scope='department', definition=definition, status=422)
    personal = create(h, table, definition=definition)['view']
    value = {'table_key': table, 'scope': 'department', 'name': 'Cannot share a person filter', 'definition': definition}
    h.command('coordinator', 'table_view.update', value, personal['id'], personal['revision'], status=422)
    assert listing(h, table)['views'] == []


def test_shared_fairness_nested_person_filters_are_private(database):
    h = database
    for field in ('who', 'rows', 'cols', 'trend_who'):
        reply = create(h, 'fairness', scope='department', definition={'fairness': {
            field: {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [h.seed['people'][0]]}]}}}, status=422)
        assert 'cannot filter by person' in str(reply['error'])


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_default_and_shared_view_permissions_cover_every_role(h, role):
    if role == 'programme_lead':
        identity(h, role, [role])
    allowed = role in ('admin', 'coordinator')
    create(h, role=role, scope='department', status=200 if allowed else 403)
    own = create(h, role=role)['view']
    assert choose(h, own['id'], role=role)['defaults']['personal_view_id'] == own['id']
    choose(h, role=role, scope='department', status=200 if allowed else 403)
    choose(h, role=role, scope='department', clear=True, status=200 if allowed else 403)


def test_limits_remain_personal_or_department_and_per_table(h):
    with transaction(h.app.state.engine, write=True) as db:
        for table in ('people', 'rule-tree'):
            for scope, count in (('personal', 20), ('department', 50)):
                db.add_all([m.TableView(department_id=h.seed['department_id'], owner_id=h.seed['identities']['coordinator'],
                    table_key=table, name=f'{table} {scope} {i}', definition={}, scope=scope) for i in range(count)])
    for scope in ('personal', 'department'):
        reply = create(h, scope=scope, status=409)
        assert reply['error']['code'] == 'SAVED_VIEW_LIMIT'
        assert 'Schedule' not in reply['error']['message']
        assert create(h, 'another-list', scope=scope)['view']['table_key'] == 'another-list'
    assert len(listing(h, role='coordinator')['views']) == 70
    assert len(listing(h)['views']) == 50


def test_default_tri_state_precedence_and_revision_checked_undo(database):
    h = database
    before = h.get('coordinator', '/api/pulse')['input_revision']
    assert listing(h)['defaults'] == s.TableViewDefaults().model_dump()
    shared = create(h, scope='department')['view']
    personal = create(h, role='staff')['view']
    choose(h, shared['id'], scope='department', role='coordinator')
    inherited = listing(h)['defaults']
    assert inherited == {'department_view_id': shared['id'], 'department_set': True,
                         'personal_view_id': None, 'personal_set': False}
    selected = choose(h, personal['id'])
    assert selected['defaults']['personal_set'] and selected['defaults']['personal_view_id'] == personal['id']
    flags = {row['id']: (row['is_department_default'], row['is_personal_default']) for row in listing(h)['views']}
    assert flags[shared['id']] == (True, False) and flags[personal['id']] == (False, True)
    assert listing(h, role='admin')['defaults']['personal_set'] is False
    initial_undo = h.command('staff', 'table_view.set_default', selected['previous_default'],
                             selected['default']['id'], selected['default']['revision'])
    assert initial_undo['defaults'] == inherited
    choose(h, personal['id'])
    builtin = choose(h)
    assert builtin['defaults']['personal_set'] and builtin['defaults']['personal_view_id'] is None
    restored = h.command('staff', 'table_view.set_default', builtin['previous_default'],
                         builtin['default']['id'], builtin['default']['revision'])
    assert restored['defaults']['personal_view_id'] == personal['id']
    h.command('staff', 'table_view.set_default', builtin['previous_default'],
              builtin['default']['id'], builtin['default']['revision'], status=409)
    cleared = choose(h, clear=True)
    assert cleared['defaults'] == inherited
    assert cleared['default']['id'] == selected['default']['id']
    undone = h.command('staff', 'table_view.set_default', cleared['previous_default'],
                       cleared['default']['id'], cleared['default']['revision'])
    assert undone['defaults']['personal_view_id'] == personal['id']
    choose(h, scope='department', role='admin')
    assert listing(h)['defaults']['department_set'] and listing(h)['defaults']['department_view_id'] is None
    assert h.get('coordinator', '/api/pulse')['input_revision'] == before


def test_deleting_default_view_falls_through_even_with_direct_foreign_key_action(database):
    h = database
    shared = create(h, scope='department')['view']
    personal = create(h, role='staff')['view']
    choose(h, shared['id'], scope='department', role='coordinator')
    choose(h, personal['id'])
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(delete(m.TableView).where(m.TableView.id == personal['id']))
        choice = db.scalar(select(m.TableViewDefault).where(m.TableViewDefault.owner_id == h.seed['identities']['staff']))
        assert choice.view_id is None and choice.built_in is False
    assert listing(h)['defaults'] == {'department_view_id': shared['id'], 'department_set': True,
                                     'personal_view_id': None, 'personal_set': False}
    choose(h, shared['id'])
    h.command('coordinator', 'table_view.delete', {}, shared['id'], shared['revision'])
    assert listing(h)['defaults'] == s.TableViewDefaults().model_dump()
    choose(h)  # An explicit built-in choice survives deletion of unrelated saved views.
    another = create(h, role='staff')['view']
    h.command('staff', 'table_view.delete', {}, another['id'], another['revision'])
    assert listing(h)['defaults']['personal_set'] is True


def test_default_uniqueness_including_null_owner_is_enforced_by_database(database):
    h = database
    for owner in (None, h.seed['identities']['staff']):
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.TableViewDefault(department_id=h.seed['department_id'], owner_id=owner, table_key='people'))
        with pytest.raises(IntegrityError), transaction(h.app.state.engine, write=True) as db:
            db.add(m.TableViewDefault(department_id=h.seed['department_id'], owner_id=owner, table_key='people'))
            db.flush()
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.TableViewDefault(department_id=h.seed['department_id'], owner_id=owner, table_key='rule-tree'))
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.TableViewDefault)) == 4


def test_defaults_reject_foreign_hidden_wrong_table_and_wrong_scope_views(h):
    other_personal = create(h)['view']
    wrong_table = create(h, 'rule-tree', scope='department')['view']
    own = create(h, role='staff')['view']
    choose(h, other_personal['id'], status=404)
    choose(h, wrong_table['id'], status=409)
    choose(h, str(uuid.uuid4()), status=404)
    choose(h, other_personal['id'], scope='department', role='coordinator', status=409)
    with transaction(h.app.state.engine, write=True) as db:
        dept = m.Department(organisation_id=h.seed['organisation_id'], name='Foreign synthetic department')
        db.add(dept)
        db.flush()
        foreign = m.TableView(department_id=dept.id, owner_id=h.seed['identities']['staff'],
                             table_key='people', name='Foreign view', definition={}, scope='department')
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
    choose(h, foreign_id, status=404)
    selected = choose(h, own['id'])
    h.command('coordinator', 'table_view.set_default', selected['previous_default'],
              selected['default']['id'], selected['default']['revision'], status=404)
    assert listing(h)['defaults']['personal_view_id'] == own['id']


def test_privatizing_default_view_cannot_expose_it_to_other_people(h):
    shared = create(h, scope='department')['view']
    choose(h, shared['id'], scope='department', role='coordinator')
    choose(h, shared['id'])
    h.command('admin', 'table_view.update', {'table_key': 'people', 'name': 'Now private',
        'scope': 'personal', 'definition': {}}, shared['id'], shared['revision'])
    assert listing(h)['defaults'] == s.TableViewDefaults().model_dump()
    assert listing(h)['views'] == []


def test_default_receipts_are_audited_and_recheck_department_permission(h):
    key = str(uuid.uuid4())
    result = choose(h, scope='department', role='coordinator', key=key)
    assert choose(h, scope='department', role='coordinator', key=key) == result
    with transaction(h.app.state.engine, write=True) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'table_view.set_default')))
        assert len(events) == 1 and events[0].target_id == result['default']['id']
        assert events[0].detail['receipt_id'] == result['receipt_id']
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == h.seed['identities']['coordinator']))
        binding.role = 'approver'
    choose(h, scope='department', role='coordinator', key=key, status=403)
    h.command('coordinator', 'table_view.set_default', result['previous_default'],
              result['default']['id'], result['default']['revision'], status=403)


def test_print_presets_round_trip_reference_visible_planner_views_and_undo(database):
    h = database
    planner = create(h, 'planner', definition={'planner': {'rows': 'tag', 'columns': 'month', 'group_by': 'type'}})['view']
    definition = {'print': {'view_id': planner['id'], 'paper': 'a3', 'scale': 'custom', 'scale_percent': 125,
                            'include': ['title', 'filters', 'signature'], 'repeat_headers': False, 'format': 'png'}}
    preset = create(h, 'schedule_print', definition=definition)['view']
    stored = listing(h, 'schedule_print', 'coordinator')['views'][0]
    assert stored == preset and stored['definition']['print']['view_id'] == planner['id']
    changed = h.command('coordinator', 'table_view.update', {'table_key': 'schedule_print', 'name': 'Changed',
        'definition': {'print': {'format': 'csv'}}}, preset['id'], preset['revision'])
    restored = h.command('coordinator', 'table_view.update', changed['previous_view'], preset['id'], changed['view']['revision'])['view']
    assert restored['definition'] == preset['definition']
    choose(h, preset['id'], table='schedule_print', role='coordinator')
    assert listing(h, 'schedule_print', 'coordinator')['defaults']['personal_view_id'] == preset['id']
    create(h, 'schedule_print', role='staff', definition=definition, status=404)
    shared_planner = create(h, 'planner', scope='department')['view']
    assert create(h, 'schedule_print', role='staff', definition={'print': {'view_id': shared_planner['id']}})['view']
    people = create(h)['view']
    for ref, status in ((people['id'], 409), (str(uuid.uuid4()), 404)):
        create(h, 'schedule_print', definition={'print': {'view_id': ref}}, status=status)
    with transaction(h.app.state.engine, write=True) as db:
        dept = m.Department(organisation_id=h.seed['organisation_id'], name='Foreign print test department')
        db.add(dept)
        db.flush()
        foreign = m.TableView(department_id=dept.id, owner_id=h.seed['identities']['coordinator'],
                             table_key='planner', name='Foreign planner', definition={}, scope='department')
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
    create(h, 'schedule_print', definition={'print': {'view_id': foreign_id}}, status=404)
    private = create(h, 'planner', definition={'planner': {'person_id': h.seed['people'][0]}})['view']
    create(h, 'schedule_print', scope='department', definition={'print': {'view_id': private['id']}}, status=422)


def test_defaults_migration_is_additive_and_preserves_existing_views(h):
    original = create(h, 'planner', definition={'planner': {'mode': 'month', 'compact': True}})['view']
    with h.app.state.engine.begin() as db:
        db.execute(text('DROP TABLE table_view_defaults'))
        # Reapply only this migration; the fixture already contains every later migration.
        with Operations.context(MigrationContext.configure(db)):
            importlib.import_module('migrations.versions.0120_table_view_defaults').upgrade()
    assert listing(h, 'planner', 'coordinator')['views'] == [original]
    assert listing(h, 'planner')['defaults'] == s.TableViewDefaults().model_dump()
    path = safety.VERSIONS / '0120_table_view_defaults.py'
    assert safety.classification(path) == 'additive'
    assert safety.declaration_errors(path) == [] and not safety.needs_confirmation(path)

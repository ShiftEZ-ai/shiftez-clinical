"""Shared Schedule views retain department isolation, role checks, limits and audit/Undo."""
import uuid

import pytest
from alembic import command
from sqlalchemy import select, text

from apps.api import models as m
from apps.api.db import make_engine, transaction
from packages.contracts import schemas as s
from scripts.manage import alembic_config, migrate
from .governance_support import identity


@pytest.fixture(params=['table', 'planner'])
def views(request):
    return request.param


def payload(views, *, scope='personal', name='Synthetic Schedule', settings=None):
    definition = settings or {}
    return {'name': name, 'scope': scope, 'definition': {'planner': definition} if views == 'table' else definition,
            **({'table_key': 'planner'} if views == 'table' else {})}


def save(h, views, role='coordinator', action='create', row=None, status=200, **extra):
    return h.command(role, ('table_view.' if views == 'table' else 'planner_view.') + action,
                     {} if action == 'delete' and views == 'table' else {'reason': 'Remove synthetic view'} if action == 'delete'
                     else payload(views, **extra), target=row['id'] if row else None,
                     revision=row['revision'] if row else None, status=status)


def listing(h, views, role='coordinator'):
    return h.get(role, '/api/table-views?table=planner' if views == 'table' else '/api/planner/views')['views']


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_saved_name_descending_round_trips_and_undo_preserves_access(request, database, views):
    h = request.getfixturevalue(database)
    original = save(h, views, settings={'people_sort': 'name'})['view']
    changed = save(h, views, action='update', row=original, settings={'people_sort': 'name_desc'})
    stored = listing(h, views)[0]
    settings = stored['definition']['planner'] if views == 'table' else stored['definition']
    assert settings['people_sort'] == 'name_desc'
    assert stored['id'] not in {v['id'] for v in listing(h, views, 'admin')}
    save(h, views, 'admin', action='update', row=stored, settings={'people_sort': 'hours_asc'}, status=404)
    prefix = 'table_view.' if views == 'table' else 'planner_view.'
    restored = h.command('coordinator', prefix + 'update', changed['previous_view'], stored['id'], stored['revision'])['view']
    value = restored['definition']['planner'] if views == 'table' else restored['definition']
    assert value['people_sort'] == 'name' and restored['scope'] == 'personal'
    assert listing(h, views)[0] == restored


def test_settings_api_round_trip_legacy_filters_and_new_display_fields(h, views):
    legacy = {'mode': 'month', 'location_id': h.seed['locations'][0], 'assignment_type_id': h.seed['type'],
              'person_id': h.seed['people'][0]}
    before = h.get('coordinator', '/api/pulse')['input_revision']
    old = save(h, views, settings=legacy)['view']
    assert old['scope'] == 'personal' and old['can_edit'] is True
    assert listing(h, views) == [old]
    updated = save(h, views, action='update', row=old,
                   settings=old['definition']['planner'] if views == 'table' else old['definition'])['view']
    assert updated['definition'] == old['definition']
    settings = {**legacy, 'rows': 'person', 'columns': 'weekday', 'subgroup': 'rank', 'cells': 'counts',
                'collapsed': ['rank:synthetic'], 'group_by': 'tag', 'filters': {'category_ids': ['synthetic-rank'],
                'weekday': ['0', '6'], 'consultation_mode': ['virtual'], 'only_open': ['true'], 'only_issues': ['true']},
                'properties': [{'key': key, 'on': False, 'form': 'text'} for key in s.VIEW_PROPERTIES[-4:]]}
    new = save(h, views, action='update', row=updated, settings=settings)['view']
    stored = listing(h, views)[0]['definition']
    stored = stored['planner'] if views == 'table' else stored
    assert all(stored[key] == value for key, value in settings.items())
    assert h.get('coordinator', '/api/pulse')['input_revision'] == before
    for bad in ({'rows': 'person', 'subgroup': 'person'}, {'filters': {'weekday': 'Monday'}}, {'cells': 'unknown'},
                {'properties': [{'key': 'note'}]}):
        save(h, views, settings=bad, status=422)


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_department_views_role_permissions_and_personal_copy(h, views, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    row = save(h, views, scope='department')['view']
    can_edit = role in {'coordinator', 'admin'}
    assert listing(h, views, role) == [{**row, 'can_edit': can_edit}]
    save(h, views, role, scope='department', name='Second shared', status=200 if can_edit else 403)
    changed = save(h, views, role, action='update', row=row, scope='department', name='Changed shared',
                   status=200 if can_edit else 403)
    save(h, views, role, action='delete', row=changed['view'] if can_edit else row, status=200 if can_edit else 403)
    if views == 'table' or role in {'admin', 'coordinator', 'approver', 'auditor', 'programme_lead'}:
        copy = save(h, views, role, name='Personal copy')['view']
        assert copy['scope'] == 'personal' and copy['can_edit'] is True
        if role != 'coordinator':
            assert copy['id'] not in {r['id'] for r in listing(h, views)}


def test_department_scope_change_undo_and_stale_revision(h, views):
    prefix = 'table_view.' if views == 'table' else 'planner_view.'
    original = save(h, views)['view']
    shared = save(h, views, action='update', row=original, scope='department')
    assert shared['previous_view']['scope'] == 'personal'
    restored = h.command('coordinator', prefix + 'update', shared['previous_view'], original['id'], shared['view']['revision'])['view']
    assert restored['scope'] == 'personal' and listing(h, views, 'staff') == []
    shared = save(h, views, action='update', row=restored, scope='department')['view']
    # Older clients omit scope on update: do not silently privatise a shared view.
    omitted = payload(views, name='Renamed shared')
    omitted.pop('scope')
    renamed = h.command('admin', prefix + 'update', omitted, shared['id'], shared['revision'])['view']
    assert renamed['scope'] == 'department' and renamed['owner_id'] == original['owner_id']
    save(h, views, action='delete', row=shared, status=409)
    deleted = save(h, views, 'admin', action='delete', row=renamed)
    recreated = h.command('admin', prefix + 'create', deleted['previous_view'])['view']
    assert recreated['scope'] == 'department' and listing(h, views, 'staff')[0]['id'] == recreated['id']
    h.command('staff', prefix + 'create', deleted['previous_view'], status=403)
    people = h.command('coordinator', 'table_view.create', {**payload('table', scope='department'), 'table_key': 'people'})['view']
    assert people['id'] in {v['id'] for v in h.get('staff', '/api/table-views?table=people')['views']}


@pytest.mark.parametrize('action', ['create', 'delete'])
def test_department_owner_losing_permission_cannot_edit_or_replay(h, views, action):
    key = str(uuid.uuid4())
    prefix = 'table_view.' if views == 'table' else 'planner_view.'
    value = payload(views, scope='department')
    row = h.command('coordinator', prefix + 'create', value, key=key)['view']
    if action == 'delete':
        key = str(uuid.uuid4())
        value = {'reason': 'Remove shared view'}
        h.command('coordinator', prefix + 'delete', value, row['id'], row['revision'], key=key)
    with transaction(h.app.state.engine, write=True) as db:
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == row['owner_id']))
        binding.role = 'approver'
    if action == 'create':
        assert listing(h, views)[0]['can_edit'] is False
        save(h, views, action='update', row=row, scope='department', status=403)
        save(h, views, action='delete', row=row, status=403)
    h.command('coordinator', prefix + action, value, row['id'] if action == 'delete' else None,
              row['revision'] if action == 'delete' else None, key=key, status=403)


def test_view_limits_are_separate_and_apply_to_scope_transitions(h, views):
    model = m.TableView if views == 'table' else m.PlannerView
    with transaction(h.app.state.engine, write=True) as db:
        for scope, count in [('personal', 20), ('department', 49)]:
            for i in range(count):
                db.add(model(department_id=h.seed['department_id'], owner_id=h.seed['identities']['coordinator'],
                             name=f'{scope} {i}', definition={}, scope=scope, **({'table_key': 'planner'} if views == 'table' else {})))
    save(h, views, scope='department')  # a full personal quota does not consume shared capacity
    rows = listing(h, views)
    assert len(rows) == 70 and sum(r['scope'] == 'department' for r in listing(h, views, 'staff')) == 50
    personal = next(r for r in rows if r['scope'] == 'personal')
    shared = next(r for r in rows if r['scope'] == 'department')
    for scope in ('personal', 'department'):
        assert save(h, views, scope=scope, status=409)['error']['code'] == 'SAVED_VIEW_LIMIT'
    save(h, views, action='update', row=personal, scope='department', status=409)
    # Privatizing a shared view uses the acting account's personal capacity.
    save(h, views, action='update', row=shared, scope='personal', status=409)
    changed = save(h, views, action='update', row=shared, scope='department', name='Within existing quota')['view']
    save(h, views, action='delete', row=personal)
    replacement = save(h, views)['view']  # a full department quota does not consume personal capacity
    save(h, views, action='delete', row=replacement)
    save(h, views, 'admin', action='update', row=shared, scope='personal', status=409)  # stale revision still wins
    converted = save(h, views, action='update', row=changed, scope='personal')['view']
    assert converted['owner_id'] == shared['owner_id'] and converted['scope'] == 'personal'
    save(h, views, scope='department')


def test_department_isolation_and_successful_changes_are_audited(h, views):
    row = save(h, views, scope='department')['view']
    row = save(h, views, 'admin', action='update', row=row, scope='department', name='Audited change')['view']
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
        db.add(department)
        db.flush()
        other_id = department.id
        model = m.TableView if views == 'table' else m.PlannerView
        foreign = model(department_id=other_id, owner_id=h.seed['identities']['coordinator'],
                        scope='department', name='Other department', definition={}, **({'table_key': 'planner'} if views == 'table' else {}))
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
    assert [r['id'] for r in listing(h, views, 'staff')] == [row['id']]
    save(h, views, action='update', row={**row, 'id': foreign_id}, scope='department', status=404)
    save(h, views, action='delete', row={**row, 'id': foreign_id}, status=404)
    save(h, views, action='delete', row=row)
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.target_id == row['id']).order_by(m.AuditEvent.created_at, m.AuditEvent.id)))
    prefix = 'table_view.' if views == 'table' else 'planner_view.'
    assert {event.action for event in events} == {prefix + action for action in ('create', 'update', 'delete')}
    assert len(events) == 3 and all(event.detail['receipt_id'] for event in events)


def test_scope_migration_preserves_existing_definitions_and_defaults_personal(tmp_path):
    from .migration_support import add_historical_admin

    url = 'sqlite:///' + str(tmp_path / 'previous.db')
    command.upgrade(alembic_config(url), '0068_oidc_logout')
    engine = make_engine(url)
    # Raw SQL represents the previous schema: the new ORM requires its new column.
    old = '{"planner":{"mode":"timeline","compact":true}}'
    with transaction(engine, write=True) as db:
        org, dept, owner = [str(uuid.uuid4()) for _ in range(3)]
        for sql, values in [
            ('INSERT INTO organisations (id, revision, created_at, name, timezone, week_start, input_revision, availability_revision) VALUES (:id,1,CURRENT_TIMESTAMP,\'Synthetic\',\'UTC\',0,1,1)', {'id': org}),
            ('INSERT INTO departments (id, revision, created_at, organisation_id, name, generation_defaults) VALUES (:id,1,CURRENT_TIMESTAMP,:org,\'Synthetic\',\'{}\')', {'id': dept, 'org': org}),
            ('INSERT INTO identities (id, revision, created_at, issuer, subject, display_name, disabled) VALUES (:id,1,CURRENT_TIMESTAMP,\'test\',\'test\',\'Synthetic\',false)', {'id': owner}),
        ]:
            db.execute(text(sql), values)
        add_historical_admin(db.connection(), dept, owner)
        for table in ('table_views', 'planner_views'):
            db.execute(text(f'INSERT INTO {table} (id, revision, created_at, department_id, owner_id, name, definition'
                            + (', table_key' if table == 'table_views' else '') + ') VALUES (:id,1,CURRENT_TIMESTAMP,:dept,:owner,\'Old\',:definition'
                            + (", 'planner'" if table == 'table_views' else '') + ')'),
                       {'id': str(uuid.uuid4()), 'dept': dept, 'owner': owner, 'definition': old})
    migrate(url)
    with transaction(engine) as db:
        for table in ('table_views', 'planner_views'):
            assert db.execute(text(f'SELECT definition, scope, revision FROM {table}')).one() == (old, 'personal', 1)
    engine.dispose()


@pytest.mark.postgres
def test_postgres_shared_schedule_views_crud_and_limits(pg):
    for views in ('table', 'planner'):
        test_settings_api_round_trip_legacy_filters_and_new_display_fields(pg, views)
        test_department_isolation_and_successful_changes_are_audited(pg, views)


@pytest.mark.postgres
def test_postgres_schedule_view_limits(pg, views):
    test_view_limits_are_separate_and_apply_to_scope_transitions(pg, views)

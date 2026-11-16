"""Review regressions for canonical Schedule settings and safe shared-view boundaries."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, get_ident

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import planner_tools, table_views
from packages.contracts import schemas as s
from scripts.generate_contracts import render
from .governance_support import identity
from .test_schedule_views_api import listing, payload, save, views


PERSON_FILTER_MESSAGE = 'A view shared with the department cannot filter by person'
PERSON_FILTER_KEYS = ('person_ids', 'person_id', 'person', 'people_ids', 'staff_ids', 'identity_ids',
                      'user_id', 'assignee_ids', 'supervisor_person_id', 'display_name', 'email',
                      'person-id', 'person-ids', 'supervisor-person-id', 'display-name', 'account-id')


def seed_views(h, views, owner, count, scope='personal'):
    model = m.TableView if views == 'table' else m.PlannerView
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([model(department_id=h.seed['department_id'], owner_id=owner, scope=scope,
                          name=f'{scope} synthetic {i}', definition={},
                          **({'table_key': 'planner'} if views == 'table' else {})) for i in range(count)])


def assert_person_filter_rejected(reply):
    error = reply['error']
    messages = [error['message'], *(item['message'] for item in error.get('details') or [])]
    assert PERSON_FILTER_MESSAGE in [message.removeprefix('Value error, ') for message in messages]


def test_both_saved_view_apis_store_the_complete_canonical_settings(h):
    settings = {'mode': 'timeline', 'properties': [{'key': 'time'}], 'rows': None, 'filters': None}
    canonical = s.PlannerViewSettings.model_validate(settings).model_dump(mode='json')
    stored = []
    for api in ('table', 'planner'):
        row = save(h, api, settings=settings)['view']
        definition = row['definition']['planner'] if api == 'table' else row['definition']
        assert definition == canonical
        assert definition['properties'] == [{'key': 'time', 'on': True, 'form': 'chip'}]
        assert 'rows' not in definition and 'filters' not in definition
        updated = save(h, api, action='update', row=row, settings=definition)['view']
        stored.append(updated['definition']['planner'] if api == 'table' else updated['definition'])
        model = m.TableView if api == 'table' else m.PlannerView
        with transaction(h.app.state.engine) as db:
            definition = db.get(model, row['id']).definition
            assert (definition['planner'] if api == 'table' else definition) == canonical
    assert stored == [canonical, canonical]


@pytest.mark.parametrize('acting_role', ['admin', 'second_coordinator'])
def test_privatizing_shared_views_uses_the_actors_ownership_and_quota(h, views, acting_role):
    if acting_role == 'second_coordinator':
        identity(h, acting_role, ['coordinator'])
    owner = h.seed['identities']['coordinator']
    actor = h.seed['identities'][acting_role]
    seed_views(h, views, owner, 20)
    seed_views(h, views, actor, 19)
    shared = save(h, views, scope='department')['view']
    other_shared = save(h, views, scope='department')['view']
    with transaction(h.app.state.engine, write=True) as db:
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == owner))
        binding.role = 'approver'
    changed = save(h, views, acting_role, action='update', row=shared, scope='department')['view']
    assert changed['owner_id'] == owner
    personal = save(h, views, acting_role, action='update', row=changed, scope='personal')['view']
    assert personal['owner_id'] == actor and personal['can_edit'] is True
    assert personal['id'] not in {row['id'] for row in listing(h, views)}
    assert personal['id'] in {row['id'] for row in listing(h, views, acting_role)}
    reply = save(h, views, acting_role, action='update', row=other_shared, scope='personal', status=409)
    assert reply['error']['code'] == 'SAVED_VIEW_LIMIT'
    unchanged = next(row for row in listing(h, views) if row['id'] == other_shared['id'])
    assert unchanged['owner_id'] == owner and unchanged['scope'] == 'department'


@pytest.mark.parametrize('field', ('legacy',) + PERSON_FILTER_KEYS)
def test_shared_schedule_views_reject_person_filters_on_create_promotion_and_implicit_scope(h, views, field):
    person = h.seed['people'][0]
    settings = {'person_id': person} if field == 'legacy' else {'filters': {field: [person]}}
    assert_person_filter_rejected(save(h, views, scope='department', settings=settings, status=422))
    personal = save(h, views, settings=settings)['view']
    assert_person_filter_rejected(save(h, views, action='update', row=personal, scope='department',
                                      settings=settings, status=422))
    assert next(row for row in listing(h, views) if row['id'] == personal['id'])['scope'] == 'personal'
    shared = save(h, views, scope='department')['view']
    value = payload(views, settings=settings)
    value.pop('scope')
    prefix = 'table_view.' if views == 'table' else 'planner_view.'
    reply = h.command('coordinator', prefix + 'update', value, shared['id'], shared['revision'], status=422)
    assert_person_filter_rejected(reply)
    assert next(row for row in listing(h, views) if row['id'] == shared['id']) == shared


@pytest.mark.parametrize('field', PERSON_FILTER_KEYS)
def test_shared_schedule_views_also_reject_top_level_collection_person_filters(h, field):
    value = payload('table', scope='department')
    value['definition']['filters'] = {field: [h.seed['people'][0]]}
    assert_person_filter_rejected(h.command('coordinator', 'table_view.create', value, status=422))
    value['scope'] = 'personal'
    personal = h.command('coordinator', 'table_view.create', value)['view']
    value['scope'] = 'department'
    assert_person_filter_rejected(h.command('coordinator', 'table_view.update', value,
                                           personal['id'], personal['revision'], status=422))
    shared = save(h, 'table', scope='department')['view']
    value.pop('scope')
    assert_person_filter_rejected(h.command('coordinator', 'table_view.update', value,
                                           shared['id'], shared['revision'], status=422))


def test_empty_person_filter_choices_are_safe_to_share_and_promote(h, views):
    settings = {'person_id': None, 'filters': dict.fromkeys(PERSON_FILTER_KEYS, [])}
    personal = save(h, views, settings=settings)['view']
    shared = save(h, views, action='update', row=personal, scope='department', settings=settings)['view']
    assert shared['owner_id'] == personal['owner_id'] and shared['scope'] == 'department'
    value = payload(views, scope='department', settings=settings)
    if views == 'table':
        value['definition']['filters'] = dict.fromkeys(PERSON_FILTER_KEYS, [])
    prefix = 'table_view.' if views == 'table' else 'planner_view.'
    assert h.command('coordinator', prefix + 'create', value)['view']['scope'] == 'department'


def test_table_schedule_listing_reports_truncation(h):
    owner = h.seed['identities']['coordinator']
    seed_views(h, 'table', owner, 20)
    seed_views(h, 'table', owner, 50, scope='department')
    page = h.get('coordinator', '/api/table-views?table=planner')
    assert len(page['views']) == 70 and page['partial'] is False
    seed_views(h, 'table', owner, 1, scope='department')
    page = h.get('coordinator', '/api/table-views?table=planner')
    assert len(page['views']) == 70 and page['partial'] is True
    assert h.get('coordinator', '/api/table-views?table=people') == {
        'views': [], 'partial': False, 'defaults': s.TableViewDefaults().model_dump()}


def test_hidden_card_property_defaults_are_exported_from_the_contract():
    hidden = ('code', 'duty', 'service', 'hours', 'places', 'tags')
    assert s.VIEW_PROPERTIES_HIDDEN_BY_DEFAULT == hidden
    for key in s.VIEW_PROPERTIES:
        assert s.ViewProperty(key=key).on is (key not in hidden)
        assert s.ViewProperty(key=key, on=True).on is True
    typescript, _ = render()
    exported = typescript.split('export const VIEW_PROPERTIES_HIDDEN_BY_DEFAULT = ', 1)[1].split(' as const;', 1)[0]
    assert json.loads(exported) == list(hidden)


@pytest.mark.postgres
@pytest.mark.parametrize('action', ['create', 'promote'])
def test_postgres_department_row_lock_serializes_shared_view_capacity_without_global_lock(pg, views, action):
    second_id = identity(pg, 'second_coordinator', ['coordinator'])
    first_id = pg.seed['identities']['coordinator']
    seed_views(pg, views, first_id, 49, scope='department')
    targets = [None, None]
    if action == 'promote':
        targets = [save(pg, views, role)['view'] for role in ('coordinator', 'second_coordinator')]
    actors = [Actor(owner, pg.seed['department_id'], pg.seed['organisation_id'], 'Synthetic coordinator',
                    frozenset({'coordinator'}), None, 'synthetic-session') for owner in (first_id, second_id)]
    service = table_views if views == 'table' else planner_tools
    prefix = 'table_view.' if views == 'table' else 'planner_view.'
    start = Barrier(2)
    locks = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = ' '.join(statement.lower().split())
        if normalized.startswith('select') and 'departments' in normalized and 'for update' in normalized:
            locks.append(get_ident())

    def attempt(actor, target):
        command = s.Command(kind=prefix + ('update' if target else 'create'),
                            payload=payload(views, scope='department'),
                            target_id=target['id'] if target else None,
                            expected_revision=target['revision'] if target else None)
        start.wait(timeout=20)
        try:
            # Ordinary sessions deliberately bypass transaction()'s global application write lock.
            with Session(pg.app.state.engine) as db, db.begin():
                db.execute(text("SET LOCAL statement_timeout = '20s'"))
                service.view_command(db, actor, command)
            return 'created'
        except Problem as exc:
            return exc.code

    event.listen(pg.app.state.engine, 'before_cursor_execute', observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = [executor.submit(attempt, actor, target) for actor, target in zip(actors, targets)]
            outcomes = [future.result(timeout=30) for future in pending]
    finally:
        event.remove(pg.app.state.engine, 'before_cursor_execute', observe)
    assert sorted(outcomes) == ['SAVED_VIEW_LIMIT', 'created']
    assert len(set(locks)) == 2
    model = m.TableView if views == 'table' else m.PlannerView
    with transaction(pg.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(model).where(
            model.department_id == pg.seed['department_id'], model.scope == 'department')) == 50

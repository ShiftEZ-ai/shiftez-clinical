"""Related Bulk editor columns use the same bounded keys in saved views."""
import pytest
from pydantic import ValidationError
from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s


RELATED_KEYS = sorted({key for collection in s.BULK_COLLECTIONS.values() for key in collection['related_columns']})
KEY_FIELDS = ('hidden', 'filters', 'group', 'then', 'sort', 'wrap', 'order')
LEGACY_CONTACT_KEYS = ('person.work_email', 'person.work_phone', 'person.work_extension', 'person.pager')


def keyed(field, key):
    if field == 'filters':
        return {field: {key: ['synthetic-value']}}
    if field == 'sort':
        return {field: [key, 'asc']}
    return {field: [key] if field in ('hidden', 'wrap', 'order') else key}


@pytest.mark.parametrize('field', KEY_FIELDS)
@pytest.mark.parametrize('key', RELATED_KEYS)
def test_saved_bulk_views_accept_every_advertised_related_column(field, key):
    value = keyed(field, key)
    assert s.TableViewDefinition.model_validate(value).model_dump(mode='json')[field] == value[field]


@pytest.mark.parametrize('field', KEY_FIELDS)
@pytest.mark.parametrize('key', ('person.unknown', 'person.email', 'service.name.extra', '.person.tags',
                               'person..tags', '../person', 'person/display_name', 'Person.display_name',
                               'x' * 41, 'person.' + 'x' * 34))
def test_saved_bulk_view_keys_reject_unknown_paths_and_unbounded_keys(field, key):
    with pytest.raises(ValidationError):
        s.TableViewDefinition.model_validate(keyed(field, key))


@pytest.mark.parametrize('field', KEY_FIELDS)
@pytest.mark.parametrize('key', LEGACY_CONTACT_KEYS)
def test_saved_bulk_view_keys_reject_legacy_contact_paths(field, key):
    with pytest.raises(ValidationError):
        s.TableViewDefinition.model_validate(keyed(field, key))


@pytest.mark.parametrize('field', KEY_FIELDS)
def test_saved_bulk_view_plain_keys_keep_the_existing_forty_character_bound(field):
    key = 'x' * 40
    value = keyed(field, key)
    assert s.TableViewDefinition.model_validate(value).model_dump(mode='json')[field] == value[field]
    assert max(map(len, RELATED_KEYS)) <= 40


def test_saved_bulk_view_keys_accept_valid_person_member_key():
    key = 'person.contact:' + s.DEFAULT_CONTACT_TYPES[0]['id']
    value = keyed('order', key)
    assert s.TableViewDefinition.model_validate(value).model_dump(mode='json')['order'] == value['order']


def test_saved_bulk_view_keys_reject_unknown_person_field_with_member():
    with pytest.raises(ValidationError):
        s.TableViewDefinition.model_validate({'order': ['person.unknown:future_member']})


def test_saved_bulk_views_drop_removed_person_keys_on_read(h):
    created = h.command('coordinator', 'table_view.create', {'table_key': 'people', 'name': 'Removed field',
        'definition': {'hidden': ['person.display_name']}})['view']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.TableView, created['id'])
        row.definition = {'hidden': ['person.future', 'person.display_name'],
                          'filters': {'person.future': ['stale'], 'person.display_name': ['Synthetic person']},
                          'group': 'person.future', 'sort': ['person.future', 'asc'],
                          'order': ['person.future', 'person.display_name']}
    view = next(view for view in h.get('coordinator', '/api/table-views?table=people')['views']
                if view['id'] == created['id'])
    definition = view['definition']
    assert definition['hidden'] == ['person.display_name']
    assert definition['filters'] == {'person.display_name': ['Synthetic person']}
    assert definition['order'] == ['person.display_name']
    assert 'group' not in definition and 'sort' not in definition


# maintenance condition 3 (maintenance 0f10f4d16 removes these contact legacy fields from BULK_COLLECTIONS['people']).
def test_saved_bulk_views_drop_legacy_contact_keys_on_read(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.TableView(id=m.uid(), department_id=h.seed['department_id'],
                           owner_id=h.seed['identities']['coordinator'], table_key='people',
                           name='Legacy contact field', definition={
                               'hidden': ['person.work_email', 'person.display_name'],
                               'filters': {'person.work_email': ['stale'], 'person.display_name': ['Synthetic person']},
                               'group': 'person.work_email', 'sort': ['person.work_email', 'asc']}))
    payload = h.get('coordinator', '/api/table-views?table=people')
    assert 'error' not in payload
    definition = payload['views'][0]['definition']
    assert definition['hidden'] == ['person.display_name']
    assert definition['filters'] == {'person.display_name': ['Synthetic person']}
    assert 'group' not in definition and 'sort' not in definition


def test_saved_bulk_sheet_related_columns_round_trip_and_update(h):
    value = {'table_key': 'bulk-eligibilities', 'name': 'Permissions by person', 'definition': {
        'layout': 'sheet', 'hidden': ['person.staff_number'], 'filters': {'person.display_name': ['Synthetic person']},
        'group': 'person.category_ids', 'then': 'service.location_ids', 'sort': ['person.display_name', 'asc'],
        'wrap': ['person.tags'], 'order': ['person.display_name', 'service.name'], 'as_of': '2026-10-04'}}
    created = h.command('coordinator', 'table_view.create', value)['view']
    stored = h.get('coordinator', '/api/table-views?table=bulk-eligibilities')['views'][0]
    assert stored['id'] == created['id']
    assert all(stored['definition'][key] == expected for key, expected in value['definition'].items())
    value['definition']['sort'] = ['service.name', 'desc']
    h.command('coordinator', 'table_view.update', value, created['id'], created['revision'])
    assert h.get('coordinator', '/api/table-views?table=bulk-eligibilities')['views'][0]['definition']['sort'] == ['service.name', 'desc']


def test_department_shared_views_still_refuse_related_person_filters(h):
    h.command('admin', 'table_view.create', {'table_key': 'planner', 'scope': 'department',
        'name': 'Must stay private', 'definition': {'filters': {'person.display_name': ['Synthetic person']}}}, status=422)
    assert h.get('admin', '/api/table-views?table=planner')['views'] == []

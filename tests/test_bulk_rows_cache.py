"""Paged bulk reads retain identities only and reapply live authorization and revision checks."""
from dataclasses import replace
from datetime import date
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import bulk_selection as bulk
from packages.contracts import schemas as s
from .test_bulk_selection import actor
from .test_bulk_single_commands import relationship


def page(h, payload, role='coordinator'):
    with transaction(h.app.state.engine) as db:
        return bulk.rows(db, actor(db, h, role), payload)


def selection():
    return {'collection': 'eligibilities', 'where': {'as_of': '2026-10-04', 'conditions': {
        'conditions': [{'field': 'category_ids', 'values': []}]}}}


def test_next_page_reuses_resolved_identities_and_loads_only_page_related_context(h, monkeypatch):
    with transaction(h.app.state.engine, write=True) as db:
        category = db.scalar(select(m.Membership.category_id).where(m.Membership.person_id == h.seed['people'][0]))
        db.add_all([m.Eligibility(department_id=h.seed['department_id'], person_id=h.seed['people'][i % 2],
            service_id=h.seed['services'][0], starts_on=date(2020, 1, 1)) for i in range(30)])
    payload = {'selection': selection(), 'columns': ['person.display_name', 'person.category_ids', 'service.name'],
               'sort': [{'field': 'starts_on'}], 'limit': 2}
    payload['selection']['where']['conditions']['conditions'][0]['values'] = [category]
    resolutions, page_people, loaded = [], [], []
    original_records, original_page, original_load = bulk._records, bulk._page_context, bulk._load_page

    def records(*args, **kwargs):
        resolutions.append(True)
        return original_records(*args, **kwargs)

    def context(db, current, day, people, services, fields):
        page_people.append(set(people))
        return original_page(db, current, day, people, services, fields)

    def load(db, current, collection, refs, day):
        loaded.append(len(refs))
        return original_load(db, current, collection, refs, day)

    monkeypatch.setattr(bulk, '_records', records)
    monkeypatch.setattr(bulk, '_page_context', context)
    monkeypatch.setattr(bulk, '_load_page', load)
    first = page(h, payload)
    page_people.clear()
    payload['offset'] = 2
    second = page(h, payload)
    assert len(resolutions) == 1
    assert loaded == [2, 2]
    assert all(len(people) <= 2 for people in page_people)
    assert first['token'] == second['token'] and first['count'] == second['count']
    assert not {r['id'] for r in first['rows']} & {r['id'] for r in second['rows']}
    assert all(row['person.display_name'] and row['person.category_ids'] and row['service.name'] for row in second['rows'])
    with bulk._ROWS_CACHE_LOCK:
        cache = bulk._ROWS_CACHE[h.app.state.engine]
        assert all(set(value) == {'refs', 'token', 'groups', 'expires'} for value in cache.values())
        assert 'Staff 01' not in repr(cache) and 'Staff 03' not in repr(cache)


def test_field_scoped_context_preserves_the_shared_person_and_clinic_facts(h):
    day = date(2026, 10, 4)
    people, services = set(h.seed['people']), set(h.seed['services'])
    fields = {'person_ids', 'category_ids', 'programme_ids', 'tags', 'service_ids', 'location_ids', 'date_set_ids'}
    with transaction(h.app.state.engine) as db:
        current = actor(db, h)
        ordinary = bulk._context(db, current, day, people)
        scoped = bulk._context(db, current, day, people, fields=fields, service_ids=services)
        for person in people:
            for service in services:
                row = {'id': 'synthetic-permission', 'person_id': person, 'service_id': service}
                before = bulk._held(*ordinary, row, 'eligibilities', {})
                after = bulk._held(*scoped, row, 'eligibilities', {})
                assert {field: before(field) for field in fields} == {field: after(field) for field in fields}


def test_scoped_context_expands_programme_tags_inside_nested_groups_on_the_requested_day(h):
    programme = h.configure('programmes', {'name': 'Scoped programme', 'starts_on': '2020-01-01'})['record']
    h.configure('programme_memberships', {'person_id': h.seed['people'][0], 'programme_id': programme['id'],
        'starts_on': '2026-10-04', 'ends_on': '2026-11-01'})
    tag = h.configure('tags', {'name': 'Programme group', 'labels': ['programmes']})['record']
    h.command('coordinator', 'tag.members', {'add': [{'kind': 'programmes', 'id': programme['id']}],
        'reason': 'Synthetic programme selection tag'}, tag['id'], tag['revision'])
    selected = {'collection': 'people', 'where': {'as_of': '2026-10-04',
        'filters': {'id': [h.seed['people'][0]]}, 'conditions': {'conditions': [{'match': 'any', 'conditions': [
            {'field': 'programme_ids', 'values': [tag['id']]},
            {'field': 'person_ids', 'values': [str(uuid.uuid4())]}]}]}}}
    with transaction(h.app.state.engine) as db:
        current = actor(db, h)
        assert bulk.resolve(db, current, selected)['count'] == 1
        selected['where']['as_of'] = '2026-10-03'
        assert bulk.resolve(db, current, selected)['count'] == 0


def test_condition_filter_serializes_only_kept_relationships_and_preserves_date_ranges(h, monkeypatch):
    serialized, original = [], bulk.json_value

    def capture(value):
        if isinstance(value, dict) and 'person_id' in value and 'service_id' in value and 'starts_on' in value:
            serialized.append(value['id'])
        return original(value)

    monkeypatch.setattr(bulk, 'json_value', capture)
    selected = {'collection': 'eligibilities', 'where': {'as_of': '2026-10-04',
        'filters': {'starts_on': {'gte': '2020-01-01'}},
        'conditions': {'conditions': [{'field': 'person_ids', 'values': [h.seed['people'][0]]}]}}}
    with transaction(h.app.state.engine) as db:
        result = bulk.resolve(db, actor(db, h), selected)
    assert result['count'] > 0 and set(serialized) == {row['id'] for row in result['sample']}
    assert all(isinstance(row['starts_on'], str) and row['person_id'] == h.seed['people'][0] for row in result['sample'])


@pytest.mark.parametrize('change', ['inputs', 'availability', 'target', 'add', 'parent'])
def test_page_cache_invalidates_inputs_targets_additions_and_parent_revisions(h, monkeypatch, change):
    payload = {'selection': {'collection': 'eligibilities'}, 'columns': ['ends_on'], 'limit': 2}
    if change == 'parent':
        payload['selection']['parent'] = {'collection': 'people', 'selection': {
            'collection': 'people', 'include_ids': [h.seed['people'][0]]}}
    before = page(h, payload)
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        if change == 'inputs':
            org.input_revision += 1
        elif change == 'availability':
            org.availability_revision += 1
        elif change == 'target':
            row = db.get(m.Eligibility, before['rows'][0]['id'])
            row.revision += 1
            row.ends_on = date(2028, 1, 1)
        elif change == 'parent':
            db.get(m.Person, h.seed['people'][0]).revision += 1
        else:
            db.add(m.Eligibility(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
                service_id=h.seed['services'][0], starts_on=date(2030, 1, 1)))
    calls, original = [], bulk.resolve_targets

    def resolve(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(bulk, 'resolve_targets', resolve)
    after = page(h, payload)
    assert len(calls) == 1
    assert after['token'] != before['token'] or change == 'parent'


def test_cached_page_never_grants_a_read_and_never_caches_live_resolve(h, monkeypatch):
    payload = {'selection': {'collection': 'eligibilities'}, 'limit': 1}
    page(h, payload)
    with transaction(h.app.state.engine) as db:
        current = actor(db, h)
        with pytest.raises(Problem) as error:
            bulk.rows(db, replace(current, roles=frozenset({'staff'})), payload)
        assert error.value.status == 403
    calls, original = [], bulk._records

    def resolve(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(bulk, '_records', resolve)
    with transaction(h.app.state.engine) as db:
        current = actor(db, h)
        bulk.resolve_targets(db, current, payload['selection'])
        bulk.resolve_targets(db, current, payload['selection'])
    assert len(calls) == 2


def test_zero_result_cache_detects_same_count_same_revision_replacement(h):
    with transaction(h.app.state.engine, write=True) as db:
        row = m.Eligibility(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
            service_id=h.seed['services'][0], starts_on=date(2034, 1, 1))
        db.add(row)
        db.flush()
        identifier = row.id
    payload = {'selection': {'collection': 'eligibilities', 'where': {'filters': {'starts_on': ['2035-01-01']}}}}
    assert page(h, payload)['count'] == 0
    with transaction(h.app.state.engine, write=True) as db:
        db.delete(db.get(m.Eligibility, identifier))
        db.add(m.Eligibility(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
            service_id=h.seed['services'][0], starts_on=date(2035, 1, 1)))
    assert page(h, payload)['count'] == 1


def test_broader_dependency_sources_bypass_cache_without_limiting_filtered_results(h, monkeypatch):
    with transaction(h.app.state.engine) as db:
        identifier = db.scalar(select(m.Eligibility.id))
    monkeypatch.setattr(bulk, 'MAX_CANDIDATES', 1)
    payload = {'selection': {'collection': 'eligibilities', 'include_ids': [identifier]}, 'limit': 1}
    assert page(h, payload)['count'] == 1
    assert page(h, payload)['count'] == 1
    with bulk._ROWS_CACHE_LOCK:
        assert not bulk._ROWS_CACHE.get(h.app.state.engine)


def test_cache_is_bounded_per_user_and_expires(h, monkeypatch):
    for i in range(bulk._ROWS_CACHE_PER_USER + 2):
        page(h, {'selection': {'collection': 'eligibilities', 'where': {'q': str(i)}}, 'limit': 1})
    with bulk._ROWS_CACHE_LOCK:
        cache = bulk._ROWS_CACHE[h.app.state.engine]
        assert len(cache) == bulk._ROWS_CACHE_PER_USER
    monkeypatch.setattr(bulk, 'monotonic', lambda: 10**20)
    page(h, {'selection': {'collection': 'eligibilities'}, 'limit': 1})
    with bulk._ROWS_CACHE_LOCK:
        assert len(bulk._ROWS_CACHE[h.app.state.engine]) == 1


def test_groups_and_sorted_pages_remain_consistent_without_caching_group_values(h):
    payload = {'selection': {'collection': 'eligibilities'}, 'columns': ['person.display_name', 'service.name'],
               'sort': [{'field': 'person.display_name', 'direction': 'desc'}], 'group': 'service.name', 'limit': 2}
    first = page(h, payload)
    payload['offset'] = 2
    second = page(h, payload)
    assert first['groups'] == second['groups']
    assert sum(group['count'] for group in first['groups']) == first['count']
    assert all(group['value'] for group in first['groups'])
    assert not {r['id'] for r in first['rows']} & {r['id'] for r in second['rows']}


@pytest.mark.parametrize('collection', ['policies', 'scheduling_facts'])
def test_status_sort_keeps_computed_catalogue_columns_in_groups_and_cached_pages(h, collection):
    if collection == 'policies':
        def propose(catalogue):
            return h.command('coordinator', 'rule.propose', {'catalogue_id': catalogue, 'lines': [],
                'reason': 'Synthetic paged rule metadata'})['record']

        first = propose('duties_per_week')
        h.command('coordinator', 'policy.decline', {'reason': 'Synthetic retired proposal'}, first['id'], first['revision'])
        records = [first, propose('duties_per_week'), propose('duties_per_month')]
        expected = {'duties_per_week': 2, 'duties_per_month': 1}
    else:
        from .test_scheduling_fact_approvals import propose
        records = [propose(h, person=h.seed['people'][0]), propose(h, person=h.seed['people'][1]),
                   propose(h, kind='overtime_offer')]
        expected = {'workload_limit': 2, 'overtime_offers': 1}
    payload = {'selection': {'collection': collection, 'include_ids': [r['id'] for r in records]},
        'columns': ['catalogue_id', 'status'], 'group': 'catalogue_id', 'sort': [{'field': 'status'}], 'limit': 1}
    returned = []
    for offset in range(3):
        response = h.client('coordinator').post('/api/bulk/rows', json={**payload, 'offset': offset})
        assert response.status_code == 200, response.text
        result = response.json()
        assert {group['value']: group['count'] for group in result['groups']} == expected
        returned.extend(result['rows'])
    assert {row['id'] for row in returned} == {row['id'] for row in records}
    assert [row['catalogue_id'] for row in returned] == sorted(row['catalogue_id'] for row in returned)
    for catalogue in expected:
        statuses = [row['status'] for row in returned if row['catalogue_id'] == catalogue]
        assert statuses == sorted(statuses)


def test_status_column_and_filter_share_revocation_and_local_day_semantics(h):
    grant, _, _ = relationship(h, 'grant', starts_on='2026-10-01', ends_on='2027-01-01')
    availability = h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0],
        'kind': 'actual_absence', 'start': '2026-10-03T21:30:00+00:00', 'end': '2026-10-03T22:30:00+00:00',
        'private_reason': 'Synthetic private reason'})['record']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.CapabilityGrant, grant['id']).revoked = True
    payload = {'selection': {'collection': 'grants', 'include_ids': [grant['id']], 'where': {
        'as_of': '2026-10-04', 'filters': {'status': ['Ended']}}}, 'columns': ['status']}
    assert page(h, payload)['rows'][0]['status'] == 'ended'
    payload['selection'] = {'collection': 'availability', 'where': {
        'as_of': '2026-10-04', 'filters': {'status': ['Current'], 'id': [availability['id']]}}}
    row = page(h, payload)['rows'][0]
    assert row['id'] == availability['id'] and row['status'] == 'current'
    payload['columns'] = ['start']
    assert page(h, payload)['rows'][0]['status'] == 'current'
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Availability, availability['id'])
        row.retracted, row.revision = True, row.revision + 1
    assert page(h, payload)['count'] == 0
    payload['selection']['where']['filters']['status'] = ['Ended']
    assert page(h, payload)['rows'][0]['status'] == 'ended'


def test_status_for_switched_off_dated_catalogue_is_filterable(h):
    service = h.configure('services', {'name': 'Switched off clinic', 'starts_on': '2020-01-01', 'active': False})['record']
    payload = {'selection': {'collection': 'services', 'where': {'as_of': '2026-10-04',
        'filters': {'status': ['Inactive'], 'id': [service['id']]}}}, 'columns': ['name']}
    assert page(h, payload)['rows'][0]['status'] == 'inactive'
    payload['selection']['where']['filters']['status'] = ['Current']
    assert page(h, payload)['count'] == 0


def test_page_related_locations_read_only_the_selected_clinics(h):
    child = next(row for row in h.get('admin', '/api/catalogues')['locations'] if row['id'] == h.seed['locations'][0])
    parent = h.configure('locations', {'name': 'Parent clinic place', 'starts_on': '2020-01-01',
        'location_type_id': child['location_type_id']})['record']
    h.configure('locations', {**{key: child[key] for key in s.LocationInput.model_fields if key in child},
        'parent_id': parent['id']}, child['id'], child['revision'])
    for index in (0, 1):
        h.configure('recurring_series', {'name': f'Clinic location {index}', 'starts_on': '2020-01-01',
            'assignment_type_id': h.seed['type'], 'location_id': h.seed['locations'][index],
            'definition': {'anchor': '2026-10-04', 'days': [{'weekday': 0, 'start_time': '08:00', 'end_time': '12:00'}],
                           'service_ids': [h.seed['services'][index]]}})
    payload = {'selection': {'collection': 'eligibilities', 'where': {'filters': {'service_id': [h.seed['services'][0]]}}},
               'columns': ['service.location_ids'], 'limit': 2}
    result = page(h, payload)
    assert result['rows']
    assert all(h.seed['locations'][0] in row['service.location_ids'] and h.seed['locations'][1] not in row['service.location_ids']
               for row in result['rows'])
    with transaction(h.app.state.engine) as db:
        context = bulk._page_context(db, actor(db, h), date(2026, 10, 4), {h.seed['people'][0]},
            {h.seed['services'][0]}, {'location_ids'})
        held = bulk._held(*context, {'service_id': h.seed['services'][0]}, 'eligibilities', {})
        assert {parent['id'], child['id']} <= held('location_ids')
    payload['selection']['where']['conditions'] = {'conditions': [{'field': 'location_ids', 'values': [parent['id']]}]}
    assert page(h, payload)['count'] == result['count']


def test_collection_counts_show_only_readable_scoped_types_and_preserve_zero(h):
    with transaction(h.app.state.engine) as db:
        staff = bulk.collections(db, actor(db, h, 'staff'))['collections']
        coordinator = bulk.collections(db, actor(db, h))['collections']
        for key in staff:
            bulk._base_query(db, actor(db, h, 'staff'), key)
        assert not {'people', 'eligibilities', 'identities', 'roles', 'policies'} & staff.keys()
        assert staff['availability']['count'] == 0
        assert staff['inbox']['count'] >= 0
        expected = bulk.resolve(db, actor(db, h), {'collection': 'eligibilities'})['count']
        assert coordinator['eligibilities']['count'] == expected
        assert coordinator['wishes']['count'] == 0

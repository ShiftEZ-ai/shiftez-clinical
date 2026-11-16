"""Creation Undo batches database guards without hiding live pending references."""
from datetime import date

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services.catalogue import _require_unreferenced, prepare_created_undo


def targets(db, h, rows):
    prepare_created_undo(db, None, [{'target_id': row.id, 'payload': {}} for row in rows],
                         {'item_kind': 'catalogue.undo_create', 'common': {'catalogue': 'services'}})
    db.info['bulk'] = {'test': True}


def test_reference_guard_batches_one_query_per_referencing_table(h):
    with transaction(h.app.state.engine, write=True) as db:
        rows = [m.Service(department_id=h.seed['department_id'], name=f'Synthetic deletion candidate {n}',
                          starts_on=date(2020, 1, 1)) for n in range(50)]
        db.add_all(rows)
        db.flush()
        targets(db, h, rows)
        queries = []
        def queried(*args):
            if args[2].lstrip().upper().startswith('SELECT'):
                queries.append(args[2])
        event.listen(h.app.state.engine, 'before_cursor_execute', queried)
        try:
            for row in rows:
                _require_unreferenced(db, row)
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', queried)
        referencing = 2 + sum(any(fk.column.table is m.Service.__table__ for fk in table.foreign_keys)
                              for table in m.Base.metadata.tables.values())  # Polymorphic tags and source mappings too.
        assert len(queries) == referencing + 1  # One combined query for all JSON reference holders.


def test_reference_cache_sees_new_flushed_deleted_and_rolled_back_references(h):
    with transaction(h.app.state.engine, write=True) as db:
        row = m.Service(department_id=h.seed['department_id'], name='Synthetic changing reference', starts_on=date(2020, 1, 1))
        db.add(row)
        db.flush()
        targets(db, h, [row])
        _require_unreferenced(db, row)
        child = m.Eligibility(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
                              service_id=row.id, starts_on=date(2020, 1, 1))
        db.add(child)
        with db.no_autoflush, pytest.raises(Problem):
            _require_unreferenced(db, row)
        db.flush()
        with pytest.raises(Problem):
            _require_unreferenced(db, row)
        db.delete(child)
        with db.no_autoflush:
            _require_unreferenced(db, row)
        db.flush()
        _require_unreferenced(db, row)
        nested = db.begin_nested()
        child = m.Eligibility(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
                              service_id=row.id, starts_on=date(2020, 1, 1))
        db.add(child)
        db.flush()
        with pytest.raises(Problem):
            _require_unreferenced(db, row)
        nested.rollback()
        _require_unreferenced(db, row)


def test_reference_guard_scopes_polymorphic_tags_and_overlays_pending_kind_changes(h):
    with transaction(h.app.state.engine, write=True) as db:
        service = m.Service(department_id=h.seed['department_id'], name='Synthetic shared tag reference id',
                            starts_on=date(2020, 1, 1))
        tag = m.Tag(department_id=h.seed['department_id'], name='Synthetic tag reference', labels=['people', 'services'])
        db.add_all([service, tag])
        db.flush()
        person = m.Person(id=service.id, organisation_id=h.seed['organisation_id'], display_name='Synthetic same id person')
        member = m.TagMember(department_id=h.seed['department_id'], tag_id=tag.id, kind='people', member_id=person.id)
        db.add_all([person, member])
        db.flush()
        targets(db, h, [service])
        _require_unreferenced(db, service)
        with pytest.raises(Problem):
            _require_unreferenced(db, person)
        member.kind = 'services'
        with db.no_autoflush:
            _require_unreferenced(db, person)
            with pytest.raises(Problem):
                _require_unreferenced(db, service)
        member.kind = 'people'
        with db.no_autoflush:
            _require_unreferenced(db, service)
            with pytest.raises(Problem):
                _require_unreferenced(db, person)


def test_reference_cache_overlays_changed_foreign_keys_and_other_department_rows(h):
    with transaction(h.app.state.engine, write=True) as db:
        rows = [m.Service(department_id=h.seed['department_id'], name=f'Synthetic moved reference {n}',
                          starts_on=date(2020, 1, 1)) for n in range(2)]
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Synthetic referencing department')
        db.add_all([*rows, other])
        db.flush()
        child = m.Eligibility(department_id=other.id, person_id=h.seed['people'][0], service_id=rows[0].id,
                              starts_on=date(2020, 1, 1))
        db.add(child)
        db.flush()
        targets(db, h, rows)
        with pytest.raises(Problem):
            _require_unreferenced(db, rows[0])
        child.service_id = rows[1].id
        with db.no_autoflush:
            _require_unreferenced(db, rows[0])
            with pytest.raises(Problem):
                _require_unreferenced(db, rows[1])


def test_reference_guard_keeps_composite_keys_multiple_foreign_keys_and_self_references(h):
    workspace = h.shift(h.workspace())['workspace']
    shift = h.detail(workspace)['shifts'][0]
    with transaction(h.app.state.engine, write=True) as db:
        service = m.Service(department_id=h.seed['department_id'], name='Synthetic composite reference', starts_on=date(2020, 1, 1))
        db.add(service)
        db.flush()
        db.add(m.ShiftService(shift_id=shift['id'], service_id=service.id))
        db.flush()
        targets(db, h, [service])
        with pytest.raises(Problem):
            _require_unreferenced(db, service)
        people = [m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Synthetic dual reference {n}')
                  for n in range(2)]
        location = m.Location(department_id=h.seed['department_id'], name='Synthetic self reference',
                              location_type_id=h.seed['location_type'], starts_on=date(2020, 1, 1))
        db.add_all([*people, location])
        db.flush()
        db.add(m.Mentorship(department_id=h.seed['department_id'], person_id=people[0].id, mentor_id=people[1].id,
                            starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1)))
        location.parent_id = location.id
        db.flush()
        for row in [*people, location]:
            with pytest.raises(Problem):
                _require_unreferenced(db, row)


def test_bulk_catalogue_creation_undo_has_bounded_guard_and_creator_queries(h):
    created = h.command('admin', 'bulk.apply', {'item_kind': 'catalogue.create', 'common': {'catalogue': 'services'},
        'items': [{'payload': {'value': {'name': f'Synthetic batch Undo {n}', 'starts_on': '2020-01-01'}}} for n in range(40)],
        'reason': 'Synthetic creation Undo query budget'})
    queries = []
    def queried(*args):
        if args[2].lstrip().upper().startswith('SELECT'):
            queries.append(args[2])
    event.listen(h.app.state.engine, 'before_cursor_execute', queried)
    try:
        undone = h.command('admin', **created['undo'])
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', queried)
    assert undone['count'] == 40
    assert len(queries) < 80
    assert sum('definition_history.actor_id' in query for query in queries) <= 2
    with transaction(h.app.state.engine) as db:
        ids = [row['record']['id'] for row in created['items']]
        assert not db.scalar(select(m.Service.id).where(m.Service.id.in_(ids)))


def test_bulk_people_deferred_flush_preserves_short_code_uniqueness_and_reuse(h):
    people = h.get('admin', '/api/catalogues')['people'][:2]
    value = {'item_kind': 'catalogue.update', 'common': {'catalogue': 'people'}, 'items': [
        {'target_id': row['id'], 'expected_revision': row['revision'], 'changes': [{'field': 'short_code', 'value': 'SYNCOD'}]}
        for row in people], 'reason': 'Synthetic pending-code conflict'}
    refused = h.command('admin', 'bulk.apply', value, status=409)
    assert refused['error']['code'] == 'SHORT_CODE_TAKEN'
    with transaction(h.app.state.engine) as db:
        assert all(not db.get(m.Person, row['id']).short_code for row in people)
    value['items'][1]['changes'][0]['value'] = 'SYNNEW'
    changed = h.command('admin', 'bulk.apply', value)
    for item in value['items']:
        item['expected_revision'] += 1
    value['items'][0]['changes'][0]['value'] = 'FREED'
    value['items'][1]['changes'][0]['value'] = 'SYNCOD'
    changed = h.command('admin', 'bulk.apply', value)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, people[1]['id']).short_code == 'SYNCOD'


def test_bulk_people_deferred_flush_refuses_duplicate_new_short_codes(h):
    result = h.command('coordinator', 'bulk.apply', {'item_kind': 'catalogue.create',
        'common': {'catalogue': 'people'}, 'items': [{'payload': {'value': {
            'display_name': f'Synthetic duplicate new colleague {number}', 'short_code': 'SYNNEW'}}}
            for number in range(2)], 'reason': 'Synthetic pending new-code conflict'}, status=409)
    assert result['error']['code'] == 'SHORT_CODE_TAKEN'
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.Person.id).where(m.Person.short_code == 'SYNNEW'))


@pytest.mark.parametrize('bulk', [False, True])
def test_people_creation_undo_uses_the_shared_organisation_scope(h, bulk):
    value = {'display_name': 'Synthetic newly created colleague', 'short_code': 'SYNUND'}
    if bulk:
        created = h.command('coordinator', 'bulk.apply', {'item_kind': 'catalogue.create',
            'common': {'catalogue': 'people'}, 'items': [{'payload': {'value': value}}],
            'reason': 'Synthetic colleague creation'})
        row = created['items'][0]['record']
        undone = h.command('coordinator', **created['undo'])
        assert undone['count'] == 1 and undone['items'][0]['results'][0]['removed'] == 1
    else:
        row = h.configure('people', value, role='coordinator')['record']
        undone = h.command('coordinator', 'catalogue.undo_create', {'catalogue': 'people',
            'reason': 'Synthetic colleague creation Undo'}, row['id'], row['revision'])
        assert undone['removed'] == 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, row['id']) is None
        assert db.scalar(select(m.DefinitionHistory.id).where(
            m.DefinitionHistory.object_id == row['id'], m.DefinitionHistory.object_revision == 2))


@pytest.mark.parametrize('bulk', [False, True])
def test_people_creation_undo_refuses_another_organisation(h, bulk):
    with transaction(h.app.state.engine, write=True) as db:
        organisation = m.Organisation(name='Synthetic separate organisation')
        db.add(organisation)
        db.flush()
        person = m.Person(organisation_id=organisation.id, display_name='Synthetic separate colleague')
        db.add(person)
        db.flush()
        ident = person.id
    payload = {'catalogue': 'people', 'reason': 'Synthetic cross-organisation refusal'}
    if bulk:
        result = h.command('coordinator', 'bulk.apply', {'item_kind': 'catalogue.undo_create',
            'items': [{'target_id': ident, 'expected_revision': 99, 'payload': payload}],
            'reason': payload['reason']}, status=409)
    else:
        result = h.command('coordinator', 'catalogue.undo_create', payload, ident, 99, status=404)
    assert result['error']['code'] == 'NOT_FOUND'
    assert 'current_revision' not in (result['error'].get('details') or {})
    assert 'Synthetic separate colleague' not in str(result)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, ident).revision == 1


def test_bulk_tag_members_keep_wrong_department_foreign_keys_binding(h):
    tag = h.configure('tags', {'name': 'Synthetic cross-FK tag', 'labels': ['people']})['record']
    person_id = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Synthetic inconsistent FK department')
        db.add(department)
        db.flush()
        db.add(m.TagMember(department_id=department.id, tag_id=tag['id'], kind='people', member_id=person_id))
        db.get(m.Person, person_id).tags = [tag['name']]
    value = {'name': 'Synthetic FK renamed', 'labels': ['people']}
    payload = {'item_kind': 'catalogue.update', 'common': {'catalogue': 'tags'}, 'items': [
        {'target_id': tag['id'], 'expected_revision': tag['revision'], 'payload': {'value': value}}],
        'reason': 'Synthetic cross-FK guard preservation'}
    changed = h.command('coordinator', 'bulk.apply', payload)
    tag = changed['items'][0]['record']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person_id).tags == [value['name']]
    payload['items'][0]['expected_revision'] = tag['revision']
    value['labels'] = ['services']
    refused = h.command('coordinator', 'bulk.apply', payload, status=409)
    assert refused['error']['code'] == 'TAG_IN_USE'
    removed = h.command('coordinator', 'bulk.apply', {'item_kind': 'tag.members', 'items': [
        {'target_id': tag['id'], 'expected_revision': tag['revision'], 'payload': {
            'remove': [{'kind': 'people', 'id': person_id}]}}], 'reason': payload['reason']})
    assert removed['items'][0]['results'][0]['removed'] == [{'kind': 'people', 'id': person_id}]
    payload['items'][0]['expected_revision'] = removed['items'][0]['record']['revision']
    h.command('coordinator', 'bulk.apply', payload)
    with transaction(h.app.state.engine) as db:
        assert not db.get(m.Person, person_id).tags
        assert not db.scalar(select(m.TagMember.id).where(m.TagMember.tag_id == tag['id']))

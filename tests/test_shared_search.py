"""Scoped search SQL, local parity and legacy saved-view compatibility without a live database."""
from datetime import date
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, literal, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from apps.api import models as m
from apps.api.errors import Problem
from apps.api.services import search
from packages.contracts import schemas as s

DAY = date(2026, 10, 4)


@pytest.fixture
def db_actor():
    engine = create_engine('sqlite://')
    event.listen(engine, 'connect', lambda connection, _: search.register_sqlite(connection))
    m.Base.metadata.create_all(engine)
    with Session(engine) as db:
        org = m.Organisation(name='Search test')
        db.add(org)
        db.flush()
        department = m.Department(organisation_id=org.id, name='Search department')
        db.add(department)
        db.flush()
        actor = SimpleNamespace(organisation_id=org.id, department_id=department.id, identity_id=str(uuid4()),
            person_id=None, roles=('admin',), permits=lambda *roles: True)
        yield db, actor
    engine.dispose()


def add_person(db, actor, name):
    person = m.Person(organisation_id=actor.organisation_id, display_name=name)
    db.add(person)
    db.flush()
    db.add(m.Membership(department_id=actor.department_id, person_id=person.id, category_id=str(uuid4()), starts_on=DAY))
    db.flush()
    return person


@pytest.mark.parametrize('value,q,exact,sensitive,expected', [
    ('Dr Olivia Quinn', 'Quinn', False, False, True),
    ('Dr Olivia Quinn', 'Quinn', True, False, False),
    ('Quinn', ' quinn ', True, False, True),
    ('Quinn', 'quinn', False, True, False),
    ('QUINN', 'QUINN', True, True, True),
    ('a_b%\\c', '_b%\\', False, False, True),
    ('aXbYc', '_b%', False, False, False),
    ('École İΣ', 'école i̇ς', True, False, True),
    ('tag', '', True, False, True),
])
def test_sql_local_literal_parity(db_actor, value, q, exact, sensitive, expected):
    db, _ = db_actor
    options = s.SearchOptions(match='exact' if exact else 'partial', case_sensitive=sensitive)
    assert bool(db.scalar(select(search.text_predicate([literal(value)], q, options)))) is expected
    assert search.text_matches([value], q, options) is expected


def test_own_person_vs_programme_referencing_person_and_as_of_membership(db_actor):
    db, actor = db_actor
    person = add_person(db, actor, 'Dr Olivia Quinn')
    programme = m.Programme(department_id=actor.department_id, name='Urgent Care', description='', starts_on=DAY)
    db.add(programme)
    db.flush()
    db.add(m.ProgrammeMembership(department_id=actor.department_id, person_id=person.id, programme_id=programme.id,
        starts_on=DAY, ends_on=date(2026, 11, 1)))
    db.flush()
    def ids(collection, model, scope, day=DAY):
        return set(db.scalars(select(model.id).where(search.predicate(db, actor, collection, 'Quinn',
            s.SearchOptions(scope=scope), as_of=day))))
    assert ids('people', m.Person, 'self') == {person.id}
    assert ids('programmes', m.Programme, 'self') == set()
    assert ids('programmes', m.Programme, 'related') == {programme.id}
    assert ids('programmes', m.Programme, 'related', date(2026, 11, 1)) == set()


def test_selected_reference_is_scoped_and_does_not_depend_on_query_label(db_actor):
    db, actor = db_actor
    person = add_person(db, actor, 'Shared name')
    other = add_person(db, actor, 'Shared name')
    # Organisation-level people from another department do not become a directory suggestion or match.
    foreign_department = m.Department(organisation_id=actor.organisation_id, name='Other search department')
    db.add(foreign_department)
    db.flush()
    foreign_actor = SimpleNamespace(**{**vars(actor), 'department_id': foreign_department.id})
    hidden = add_person(db, foreign_actor, 'Shared name')
    options = s.SearchOptions(scope='self', reference={'kind': 'people', 'id': person.id})
    assert set(db.scalars(select(m.Person.id).where(search.predicate(db, actor, 'people', 'stale label', options, as_of=DAY)))) == {person.id}
    result = search.suggestions(db, actor, 'people', 'people', 'Shared', 1)
    assert len(result['items']) == 1 and result['has_more']
    assert hidden.id not in {item['id'] for item in search.suggestions(db, actor, 'people', 'people', 'Shared', 100)['items']}
    options.reference.id = hidden.id
    assert not list(db.scalars(select(m.Person.id).where(search.predicate(db, actor, 'people', '', options, as_of=DAY))))
    assert other.id != person.id


def test_tag_definition_and_tagged_records_are_separate(db_actor):
    db, actor = db_actor
    person = add_person(db, actor, 'Person')
    tag = m.Tag(department_id=actor.department_id, name='Priority', labels=['people'])
    db.add(tag)
    db.flush()
    db.add(m.TagMember(department_id=actor.department_id, tag_id=tag.id, kind='people', member_id=person.id))
    db.flush()
    for scope, expected in (('self', set()), ('related', {person.id}), ('all', {person.id})):
        assert set(db.scalars(select(m.Person.id).where(search.predicate(db, actor, 'people', 'Priority',
            s.SearchOptions(scope=scope), as_of=DAY)))) == expected
    assert set(db.scalars(select(m.Tag.id).where(search.predicate(db, actor, 'tags', 'Priority', s.SearchOptions(scope='self'), as_of=DAY)))) == {tag.id}


def test_condition_references_include_negative_mentions_without_inferring_applicability(db_actor):
    db, actor = db_actor
    person = add_person(db, actor, 'Quinn')
    who = {'conditions': [{'field': 'person_ids', 'op': 'none', 'values': [person.id]}]}
    other = {'conditions': [{'field': 'person_ids', 'values': [str(uuid4())]}]}
    options = s.SearchOptions(scope='related', reference={'kind': 'people', 'id': person.id})
    found = {'people': {person.id}}
    assert search.reference_values(who, 'person_ids') == {person.id}
    assert search.projected_matches({'id':str(uuid4()), 'who':who}, 'policy_lines', 'ignored', options, found)
    assert not search.projected_matches({'id':str(uuid4()), 'who':other, 'note':person.id}, 'policy_lines', '', options, found)
    for document, expected in ((who, True), (other, False), ({'note': person.id}, False)):
        assert bool(db.scalar(select(search.json_reference(literal(json.dumps(document)), 'person_ids', {person.id})))) is expected
    sql = str(select(search.json_reference(m.Policy.definition, 'person_ids', {person.id})).compile(dialect=postgresql.dialect()))
    assert 'jsonb_path_exists' in sql and person.id not in sql


def test_search_options_do_not_change_legacy_serialization_or_allow_bad_inputs():
    for cls in (s.CollectionFilter, s.BulkWhere, s.TableViewDefinition, s.PlannerViewSettings):
        assert 'search_options' not in cls().model_dump(mode='json')
        assert 'search_options' not in cls(search_options=None).model_dump(mode='json')
        saved = cls(search_options={'match':'exact', 'scope':'self'}).model_dump(mode='json')
        assert saved['search_options']['match'] == 'exact'
    for options in ({'match':'regex'}, {'scope':'applies'}, {'case_sensitive':1}, {'reference':{'kind':'unknown','id':str(uuid4())}}):
        with pytest.raises(ValidationError):
            s.SearchOptions.model_validate(options)
    with pytest.raises(ValidationError, match='cannot filter by person'):
        s.TableViewInput(table_key='people', name='Private ref', scope='department', definition={
            'search_options':{'reference':{'kind':'people','id':str(uuid4())}}})


def test_scope_metadata_contains_only_registered_reference_kinds_and_readable_text():
    for collection, meta in search.registry().items():
        assert set(meta['reference_kinds']) <= set(s.SEARCH_ENTITY_KINDS)
        assert not {'private_reason', 'private_undo_note', 'password_hash', 'payload', 'subject'} & set(meta['text_fields'])
    assert 'people' in search.registry()['programmes']['reference_kinds']


def test_shared_collection_pager_filters_before_total_and_page(db_actor):
    from apps.api.services.bulk_selection import rows
    db, actor = db_actor
    first = add_person(db, actor, 'Quinn')
    add_person(db, actor, 'Dr Quinn')
    add_person(db, actor, 'Other')
    def read(options, offset=0, q='Quinn'):
        return rows(db, actor, {'selection': {'collection':'people', 'where':{'q':q,
            'as_of':DAY.isoformat(), 'search_options':options}}, 'columns':['display_name'], 'limit':1, 'offset':offset})
    page = read({'scope':'self'})
    assert page['total'] == 2 and page['has_more'] and len(page['rows']) == 1
    second = read({'scope':'self'}, 1)
    assert second['total'] == 2 and not second['has_more']
    exact = read({'scope':'self', 'match':'exact'})
    assert exact['total'] == 1 and exact['rows'][0]['id'] == first.id
    selected = read({'scope':'self', 'reference':{'kind':'people','id':first.id}}, q='')
    assert selected['total'] == 1


def test_projected_line_does_not_search_derived_applicability_subjects():
    person = str(uuid4())
    row = {'id':str(uuid4()), 'who':{'conditions':[]}, 'person_ids':[person], 'service_ids':[]}
    assert not search.projected_matches(row, 'policy_lines', 'Name', s.SearchOptions(scope='related'), {'people':{person}})


def test_permission_denied_suggestions_and_unsupported_scopes_fail_closed(db_actor):
    db, actor = db_actor
    denied = SimpleNamespace(**{**vars(actor), 'permits': lambda *roles:False})
    with pytest.raises(Problem) as error:
        search.suggestions(db, denied, 'programmes', 'people', 'Quinn', 10)
    assert error.value.status == 403
    with pytest.raises(Problem):
        search.predicate(db, actor, 'location_types', 'anything', s.SearchOptions(scope='related'), as_of=DAY)
    assert search.suggestions(db, actor, 'people', 'people', '', 10) == {'items':[], 'has_more':False}


def test_absolute_operand_reference_matches_local_and_sql(db_actor):
    db, _ = db_actor
    ident = str(uuid4())
    for operand in ({'kind':'absolute', 'value':ident}, {'kind':'absolute','value':[ident]}):
        document = {'conditions':[{'field':'person_ids','value':operand}]}
        assert search.reference_values(document, 'person_ids') == {ident}
        assert db.scalar(select(search.json_reference(literal(json.dumps(document)), 'person_ids', {ident})))


def test_api_reference_endpoint_and_invalid_options_are_permission_checked(h):
    client = h.client('admin')
    response = client.get('/api/search/references', params={'collection':'people','kind':'people','q':'a','limit':1})
    assert response.status_code == 200, response.text
    assert len(response.json()['items']) <= 1
    assert set(response.json()) == {'items','has_more'}
    for path in ('/api/workspaces', '/api/feedback', '/api/notifications'):
        response = client.get(path, params={'search_options':json.dumps({'match':'regex'})})
        assert response.status_code == 422, (path, response.text)
    h.get('operator', '/api/search/references?collection=programmes&kind=people&q=Name', status=403)


def test_search_and_layer_options_persist_through_versioned_saved_view_paths(h):
    definition = {'q':'Quinn','search_options':{'match':'exact','scope':'self'},
        'planner':{'search_options':{'match':'partial','scope':'all'},
            'layers':{'draft':False,'leave':True,'mine':True,'mode':'side'}}}
    saved = h.command('staff','table_view.create', {'table_key':'people','name':'Search settings','definition':definition})['view']
    stored = next(row for row in h.get('staff','/api/table-views?table=people')['views'] if row['id'] == saved['id'])
    assert stored['definition']['search_options']['match'] == 'exact'
    assert stored['definition']['planner']['layers']['mode'] == 'side'
    updated = h.command('staff','table_view.update', {'table_key':'people','name':'Search settings',
        'definition':{**definition,'search_options':{'match':'partial','scope':'self'}}}, target=saved['id'], revision=1)['view']
    assert updated['revision'] == 2 and updated['definition']['search_options']['match'] == 'partial'


def test_tag_tokens_keep_canonical_case_insensitive_identity(db_actor):
    db, actor = db_actor
    person = add_person(db, actor, 'Person with a legacy tag')
    person.tags = ['pRiOrItY']
    db.add(m.Tag(department_id=actor.department_id, name='Priority', labels=['people']))
    db.flush()
    assert set(db.scalars(select(m.Person.id).where(search.predicate(db, actor, 'people', 'Priority',
        s.SearchOptions(scope='related'), as_of=DAY)))) == {person.id}
    row = {'id':str(uuid4()), 'who':{'conditions':[{'field':'tags','values':['pRiOrItY']}]}}
    assert search.projected_matches(row, 'policy_lines', 'Priority', s.SearchOptions(scope='related'), {'_tag_names':{'Priority'}})


def test_request_search_options_keep_person_references_and_private_reasons_distinct(h):
    from datetime import datetime, timedelta, timezone
    from apps.api.db import transaction
    with transaction(h.app.state.engine, write=True) as db:
        person = db.get(m.Person, h.seed['people'][0])
        person.display_name = 'Reference Needle Clinician'
        start = datetime(2026,10,4,tzinfo=timezone.utc)
        row = m.RequestRecord(department_id=h.seed['department_id'], person_id=person.id, start=start,
            end=start+timedelta(days=1), kind='leave', status='submitted', private_reason='Secret Needle Reason',
            created_by=h.seed['identities']['coordinator'])
        db.add(row)
        db.flush()
        ident = row.id
    def read(q, scope, role='coordinator'):
        response = h.client(role).get('/api/requests/all', params={'q':q,
            'filter':json.dumps({'search_options':{'scope':scope,'match':'exact'}})})
        assert response.status_code == 200, response.text
        return response.json()
    assert read('Reference Needle Clinician','self')['total'] == 0
    related = read('Reference Needle Clinician','related')
    assert {row['id'] for row in related['items']} == {ident} and related['total'] == 1
    assert read('Secret Needle Reason','all')['total'] == 0
    assert read('Reference Needle Clinician','related','staff')['total'] == 1
    assert read('Secret Needle Reason','all','staff')['total'] == 0

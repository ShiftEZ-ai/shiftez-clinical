"""Collection trees and ordering retain one evaluator and database-independent semantics."""
from datetime import date
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import literal, select, union_all

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import collection_filters, schedule_view
from packages.contracts import schemas as s
from packages.domain import conditions
from .test_requests_search import both  # noqa: F401


def test_collection_groups_are_recursive_without_widening_clinical_groups():
    tree = {'conditions': [{'field': 'location_ids', 'values': ['building']}]}
    for _ in range(4):
        tree = {'match': 'any', 'conditions': [tree]}
    parsed = s.CollectionConditions.model_validate(tree)
    assert parsed.fields() == {'location_ids'}
    assert conditions.evaluate(parsed.model_dump(mode='json'), lambda _: {'building', 'room'})
    with pytest.raises(ValidationError, match='one level'):
        s.Conditions.model_validate(tree)
    for _ in range(2):
        tree = {'conditions': [tree]}
    with pytest.raises(ValidationError, match='five levels'):
        s.CollectionConditions.model_validate(tree)


def test_collection_tree_total_size_is_bounded():
    leaf = {'field': 'location_ids', 'values': ['building']}
    with pytest.raises(ValidationError, match='200 lines and groups'):
        s.CollectionConditions.model_validate({'conditions': [{'conditions': [leaf] * 20}] * 10})


def test_multikey_sql_sort_precedes_paging_and_keeps_nulls_last(both):
    values = union_all(*(select(literal(a).label('kind'), literal(b).label('number'), literal(i).label('id'))
                         for i, (a, b) in enumerate([('b', 3), ('a', None), ('a', 1), ('a', 2)]))).subquery()
    filtering = s.CollectionFilter(sort=('kind', 'asc'), then_sort=[('number', 'desc')])
    query = collection_filters.order_by(select(values.c.id), filtering,
        {'kind': values.c.kind, 'number': values.c.number}, default=(values.c.id,))
    with transaction(both.app.state.engine) as db:
        assert list(db.scalars(query.limit(3))) == [3, 2, 1]
        assert list(db.scalars(query.offset(3))) == [0]
    for filtering in (s.CollectionFilter(sort=('private', 'asc')),
                      s.CollectionFilter(sort=('kind', 'asc'), then_sort=[('kind', 'desc')])):
        with pytest.raises(Problem):
            collection_filters.sort_keys(filtering, {'kind'})


@pytest.mark.parametrize('action,kind', [('catalogue.update', 'catalogue'), ('workspace.publish', 'workspace'),
                                         ('unqualified', 'unqualified')])
def test_audit_kind_is_the_action_namespace_on_each_database(both, action, kind):
    with transaction(both.app.state.engine) as db:
        assert db.scalar(select(collection_filters.change_kind(literal(action)))) == kind
    assert s.condition_field_definition('kind', 'audit')['value_kind'] == 'multiple_choice'


def test_schedule_tree_reuses_session_ancestors_and_department_dates():
    data = {'organisation': {'timezone': 'Asia/Riyadh'},
            'locations': {'building': {'parent_id': None}, 'room': {'parent_id': 'building'}}}
    row = {'id': 'session', 'workspace_id': 'draft', 'location_id': 'room', 'service_ids': ['clinic'],
           'assignment_type_id': 'duty', 'participants': [{'person_id': 'visible-person'}],
           'start': '2026-10-04T21:30:00+00:00', 'end': '2026-10-04T23:00:00+00:00'}
    tree = {'conditions': [{'field': 'location_ids', 'values': ['building']}, {'match': 'any', 'conditions': [
        {'conditions': [{'field': 'person_ids', 'values': ['visible-person']},
                        {'field': 'date', 'op': 'is', 'value': '2026-10-05'}]},
        {'field': 'service_ids', 'values': ['other-clinic']}]}]}
    context = conditions.EvaluationContext(timezone='Asia/Riyadh', week_start=6, today=date(2026, 10, 3), table_key='schedule')
    tree = collection_filters.validate(tree, {field['key'] for field in s.table_condition_definitions('schedule')}, context)
    filtering = {'tree': tree, 'data': data, 'context': context, 'zone': 'Asia/Riyadh'}
    assert schedule_view._schedule_holds(row, data)('location_ids') == {'building', 'room'}
    assert schedule_view._filter_shifts([row], filtering, s.CollectionFilter()) == [row]
    assert schedule_view._filter_shifts([{**row, 'participants': []}], filtering, s.CollectionFilter()) == []


def test_schedule_multikey_order_uses_real_instants_and_local_days():
    rows = [{'id': 'later', 'workspace_id': 'w', 'start': '2026-10-05T00:30:00+03:00', 'end': '2026-10-05T01:30:00+03:00'},
            {'id': 'earlier', 'workspace_id': 'w', 'start': '2026-10-04T21:00:00+00:00', 'end': '2026-10-04T22:00:00+00:00'}]
    filtering = s.CollectionFilter(sort=('date', 'asc'), then_sort=[('start', 'asc')])
    assert [row['id'] for row in schedule_view._filter_shifts(rows, {'tree': None, 'zone': 'Asia/Riyadh'}, filtering)] == ['earlier', 'later']


def test_schedule_tree_round_trips_in_the_existing_planner_saved_view(both):
    h = both
    tree = {'conditions': [{'field': 'source', 'values': ['published-source']}, {'match': 'any', 'conditions': [
        {'conditions': [{'field': 'location_ids', 'values': [h.seed['locations'][0]]}]}]}]}
    definition = {'conditions': tree, 'sort': ['start', 'asc'], 'then_sort': [['source', 'desc']]}
    created = h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Synthetic filtered schedule',
        'definition': definition})['view']
    saved = h.get('coordinator', '/api/table-views?table=planner')['views'][0]
    assert saved['id'] == created['id']
    assert saved['definition']['conditions']['conditions'][0] == {**tree['conditions'][0], 'op': 'any'}
    assert saved['definition']['then_sort'] == definition['then_sort']
    assert s.CollectionFilter.model_validate(saved['definition'], extra='ignore').conditions.fields() == {'source', 'location_ids'}
    private = {'conditions': [{'conditions': [{'field': 'person_ids', 'values': [h.seed['people'][0]]}]}]}
    h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Private shared tree',
        'scope': 'department', 'definition': {'conditions': private}}, status=422)


def test_schedule_api_filters_hierarchy_day_summaries_privacy_and_etags(both):
    h = both
    with transaction(h.app.state.engine, write=True) as db:
        room = db.get(m.Location, h.seed['locations'][0])
        parent = m.Location(department_id=h.seed['department_id'], name='Synthetic parent building',
            location_type_id=room.location_type_id, starts_on=date(2020, 1, 1), capacity=10)
        db.add(parent)
        db.flush()
        room.parent_id = parent.id
        parent_id = parent.id
    w = h.shift(h.workspace('Synthetic filtered Schedule'))['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00',
                people=[h.seed['people'][1]], location=1)['workspace']
    publication = h.publish(w)['publication_id']
    tree = {'conditions': [{'field': 'source', 'values': [publication]}, {'match': 'any', 'conditions': [
        {'conditions': [{'field': 'location_ids', 'values': [parent_id]}]}]}]}
    params = {'start': '2026-10-01', 'end': '2026-11-01', 'filter': json.dumps({'conditions': tree})}
    client = h.client('coordinator')
    selected = client.get('/api/schedule/sessions', params=params)
    assert selected.status_code == 200, selected.text
    body = selected.json()
    assert len(body['shifts']) == 1 and body['shifts'][0]['location_id'] == h.seed['locations'][0]
    ids = {row['id'] for row in body['shifts']}
    assert {row['shift_id'] for row in body['coverage']} <= ids
    assert client.get('/api/schedule/sessions', params=params,
                      headers={'if-none-match': selected.headers['etag']}).status_code == 304
    day = client.get('/api/schedule/days', params=params)
    assert day.status_code == 200, day.text
    assert set(day.json()['days']) == {'2026-10-04'}
    assert sum(item['sessions'] for item in day.json()['days']['2026-10-04']) == 1
    assert client.get('/api/schedule/days', params={**params, 'start': '2026-10-05'}).json()['days'] == {}
    private = {**params, 'filter': json.dumps({'conditions': {'conditions': [
        {'field': 'person_ids', 'values': [h.seed['people'][1]]}]}})}
    for endpoint, key in [('sessions', 'shifts'), ('days', 'days')]:
        response = h.client('staff').get('/api/schedule/' + endpoint, params=private)
        assert response.status_code == 200, response.text
        assert not response.json()[key]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Location, h.seed['locations'][0]).parent_id = None
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    changed = client.get('/api/schedule/sessions', params=params, headers={'if-none-match': selected.headers['etag']})
    assert changed.status_code == 200 and not changed.json()['shifts']
    assert changed.headers['etag'] != selected.headers['etag']


def test_schedule_participant_set_filter_keeps_a_duty_with_both_people():
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'locations': {}}
    row = {'id': 'session', 'workspace_id': 'draft', 'location_id': None, 'service_ids': ['clinic'],
           'assignment_type_id': 'duty', 'participants': [{'person_id': 'p'}, {'person_id': 'q'}],
           'start': '2026-10-05T08:00:00+03:00', 'end': '2026-10-05T12:00:00+03:00'}
    context = conditions.EvaluationContext(timezone='Asia/Riyadh', week_start=6, table_key='schedule')
    tree = {'conditions': [{'conditions': [{'field': 'person_ids', 'op': 'all', 'values': ['p', 'q']}]}]}
    filtering = {'tree': tree, 'data': data, 'context': context, 'zone': 'Asia/Riyadh'}
    assert schedule_view._filter_shifts([row], filtering, s.CollectionFilter()) == [row]
    assert schedule_view._filter_shifts([{**row, 'id': 'other', 'participants': [{'person_id': 'p'}]}],
                                      filtering, s.CollectionFilter()) == []

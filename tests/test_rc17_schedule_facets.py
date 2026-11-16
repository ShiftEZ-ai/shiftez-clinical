"""Schedule collection facets retain dated facts, coherent participants and read privacy."""
from datetime import date
import json

from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import bulk_selection, schedule_view
from packages.contracts import schemas as s
from packages.domain import conditions
from packages.domain.validation import index_person_rows
from .governance_support import identity
from .test_requests_search import both  # noqa: F401


def line(field, *values, op='any'):
    return {'field': field, 'op': op, 'values': list(values)}


def test_schedule_all_existing_facets_share_contracts_and_participant_evaluation():
    fields = {'category_ids', 'programme_ids', 'tags', 'weekday', 'consultation_mode'}
    for table in ('schedule', 'planner'):
        assert fields <= {field['key'] for field in s.table_condition_definitions(table)}
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'locations': {},
            'people': {'p': {'tags': ['Team']}, 'q': {'tags': []}},
            'services': {'clinic': {'mode': 'physical'}}, 'categories': {'old': {}, 'new': {}},
            'programmes': {'training': {}}, 'memberships': [
                {'person_id': 'p', 'category_id': 'old', 'starts_on': '2020-01-01', 'ends_on': '2026-10-05'},
                {'person_id': 'p', 'category_id': 'new', 'starts_on': '2026-10-05'},
                {'person_id': 'q', 'category_id': 'old', 'starts_on': '2020-01-01'}],
            'programme_memberships': [{'person_id': 'q', 'programme_id': 'training', 'starts_on': '2020-01-01'},
                                     {'person_id': 'p', 'programme_id': 'training', 'starts_on': '2026-10-05'}]}
    data['_person_rows'] = index_person_rows(data)
    rows = [{'id': 'sunday', 'workspace_id': 'w', 'location_id': None, 'service_ids': ['clinic'],
             'assignment_type_id': 'duty', 'participants': [{'person_id': 'p'}, {'person_id': 'q'}],
             'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'},
            {'id': 'monday', 'workspace_id': 'w', 'location_id': None, 'service_ids': ['clinic'],
             'assignment_type_id': 'duty', 'participants': [{'person_id': 'p'}],
             'start': '2026-10-05T08:00:00+03:00', 'end': '2026-10-05T12:00:00+03:00'}]
    context = conditions.EvaluationContext(timezone='Asia/Riyadh', week_start=6, today=date(2026, 10, 3), table_key='schedule')
    tree = {'conditions': [line('category_ids', 'new'), {'match': 'any', 'conditions': [
        line('programme_ids', 'training'), line('tags', 'team')]}, {'conditions': [
        line('weekday', '0'), line('consultation_mode', 'physical')]}]}
    filtering = {'tree': tree, 'data': data, 'context': context, 'zone': 'Asia/Riyadh'}
    assert [row['id'] for row in schedule_view._filter_shifts(rows, filtering, s.CollectionFilter())] == ['monday']
    # P's tag and Q's rank/programme must not combine into a fictitious person.
    filtering['tree'] = {'conditions': [line('category_ids', 'old'), line('programme_ids', 'training'), line('tags', 'team')]}
    assert schedule_view._filter_shifts(rows[:1], filtering, s.CollectionFilter()) == []
    filtering['tree'] = {'conditions': [line('tags', 'team', op='none')]}
    assert schedule_view._filter_shifts(rows, filtering, s.CollectionFilter()) == []
    filtering['tree'] = {'conditions': [{'field': 'tags', 'op': 'is_not', 'value': 'TEAM'}]}
    assert schedule_view._filter_shifts(rows, filtering, s.CollectionFilter()) == []


def test_schedule_person_identity_holds_do_not_require_private_person_records():
    row = {'id': 'minimal', 'workspace_id': 'w', 'participants': [{'person_id': 'visible-peer'}],
           'service_ids': [], 'assignment_type_id': 'duty', 'location_id': None,
           'start': '2026-10-05T08:00:00+03:00', 'end': '2026-10-05T12:00:00+03:00'}
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'locations': {}}
    assert schedule_view._schedule_holds(row, data)('person_ids') == {'visible-peer'}
    assert schedule_view._schedule_holds(row, data, 'visible-peer')('person_ids') == {'visible-peer'}


def fixture(h):
    p, q = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Synthetic supervised facet clinic', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    participants = [{'person_id': p, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': q},
                    {'person_id': q, 'start': start, 'end': end, 'participation': 'supervisor'}]
    w = h.shift(h.workspace('Synthetic facet month'), typ=typ['id'], people=participants)['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[p])['workspace']
    publication = h.publish(w)['publication_id']
    with transaction(h.app.state.engine, write=True) as db:
        rank = m.Category(department_id=h.seed['department_id'], name='Synthetic dated facet rank', starts_on=date(2020, 1, 1))
        programme = m.Programme(department_id=h.seed['department_id'], name='Synthetic dated facet programme', starts_on=date(2020, 1, 1))
        hidden = m.Programme(department_id=h.seed['department_id'], name='Synthetic hidden peer programme', starts_on=date(2020, 1, 1))
        db.add_all([rank, programme, hidden])
        db.flush()
        db.add_all([m.Membership(department_id=h.seed['department_id'], person_id=p, category_id=rank.id,
                       starts_on=date(2026, 10, 5)),
                    m.ProgrammeMembership(department_id=h.seed['department_id'], person_id=p, programme_id=programme.id,
                       starts_on=date(2026, 10, 5), ends_on=date(2026, 11, 1)),
                    m.ProgrammeMembership(department_id=h.seed['department_id'], person_id=q, programme_id=hidden.id,
                       starts_on=date(2020, 1, 1), ends_on=date(2030, 1, 1))])
        db.get(m.Person, p).tags = ['own-team']
        db.get(m.Person, q).tags = ['private-peer-tag']
        db.get(m.Service, h.seed['services'][0]).mode = 'physical'
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
        return p, q, publication, rank.id, programme.id, hidden.id


def read(h, endpoint, tree, role='coordinator', **extra):
    return h.client(role).get('/api/schedule/' + endpoint, params={'start': '2026-10-01', 'end': '2026-11-01',
        'filter': json.dumps({'conditions': tree, 'as_of': '2026-10-03', **extra})})


def test_schedule_month_filters_rank_programme_tags_modes_and_weekday_at_each_duty_date(both):
    h = both
    p, _, _, rank, programme, _ = fixture(h)
    tree = {'conditions': [line('category_ids', rank), {'match': 'any', 'conditions': [
        line('programme_ids', programme), line('tags', 'own-team')]}, {'conditions': [
        line('weekday', '0'), line('consultation_mode', 'physical')]}]}
    sessions = read(h, 'sessions', tree)
    assert sessions.status_code == 200, sessions.text
    assert len(sessions.json()['shifts']) == 1
    assert sessions.json()['shifts'][0]['start'].startswith('2026-10-05')
    assert {row['person_id'] for row in sessions.json()['shifts'][0]['participants']} == {p}
    days = read(h, 'days', tree)
    assert days.status_code == 200, days.text
    assert set(days.json()['days']) == {'2026-10-05'}
    assert days.json()['days']['2026-10-05'][0]['sessions'] == 1
    saved = h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Synthetic facet view',
        'definition': {'conditions': tree}})['view']
    assert s.CollectionFilter.model_validate({'conditions': saved['definition']['conditions']}).conditions.fields() == {
        'category_ids', 'programme_ids', 'tags', 'weekday', 'consultation_mode'}


def test_staff_paired_identity_cannot_become_a_hidden_peer_fact_oracle(both, monkeypatch):
    h = both
    p, q, _, _, programme, hidden = fixture(h)
    original, loaded = bulk_selection._page_context, []

    def preload(db, actor, day, people, *args, **kwargs):
        loaded.append(set(people))
        return original(db, actor, day, people, *args, **kwargs)

    monkeypatch.setattr(bulk_selection, '_page_context', preload)
    visible = read(h, 'sessions', {'conditions': [line('person_ids', q)]}, 'staff')
    assert visible.status_code == 200 and visible.json()['shifts']
    assert q in {row['person_id'] for row in visible.json()['shifts'][0]['participants']}
    for endpoint, key in [('sessions', 'shifts'), ('days', 'days')]:
        for field, value in [('programme_ids', hidden), ('tags', 'private-peer-tag')]:
            response = read(h, endpoint, {'conditions': [line(field, value)]}, 'staff')
            assert response.status_code == 200, response.text
            assert not response.json()[key]
        own = read(h, endpoint, {'conditions': [line('programme_ids', programme)]}, 'staff')
        assert own.status_code == 200 and own.json()[key]
    assert loaded and all(ids <= {p} for ids in loaded)


def test_programme_lead_filters_cannot_read_unled_memberships(both):
    h = both
    _, _, _, _, programme, hidden = fixture(h)
    who = identity(h, 'facet_programme_lead', ['programme_lead'])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, programme).owner_identity_id = who
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    allowed = read(h, 'sessions', {'conditions': [line('programme_ids', programme)]}, 'facet_programme_lead')
    assert allowed.status_code == 200 and len(allowed.json()['shifts']) == 1
    for endpoint, key in [('sessions', 'shifts'), ('days', 'days')]:
        forbidden = read(h, endpoint, {'conditions': [line('programme_ids', hidden)]}, 'facet_programme_lead')
        assert forbidden.status_code == 200 and not forbidden.json()[key]


def test_sort_only_person_facets_preload_with_default_as_of_and_keep_etags(both, monkeypatch):
    h = both
    fixture(h)
    original, loaded = bulk_selection._page_context, []

    def preload(db, actor, day, people, services, fields, **kwargs):
        loaded.append((day, set(fields)))
        return original(db, actor, day, people, services, fields, **kwargs)

    monkeypatch.setattr(bulk_selection, '_page_context', preload)
    params = {'start': '2026-10-01', 'end': '2026-11-01', 'filter': json.dumps({
        'sort': ['category_ids', 'asc'], 'then_sort': [['weekday', 'desc'], ['consultation_mode', 'asc']]})}
    client = h.client('coordinator')
    response = client.get('/api/schedule/sessions', params=params)
    assert response.status_code == 200 and len(response.json()['shifts']) == 2, response.text
    assert loaded and all(day is not None and 'category_ids' in fields for day, fields in loaded)
    assert client.get('/api/schedule/sessions', params=params,
                      headers={'if-none-match': response.headers['etag']}).status_code == 304
    assert client.get('/api/schedule/days', params=params).status_code == 200


def test_same_day_lead_coordinator_grant_and_revoke_invalidate_filter_etags_without_input_bump(both):
    h = both
    _, _, _, _, programme, hidden = fixture(h)
    who = identity(h, 'facet_changing_scope', ['programme_lead'])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, programme).owner_identity_id = who
        # This fixture has no duty-blocking events: role changes do not bump scheduling inputs.
        assert db.scalar(select(m.Event.id).limit(1)) is None
        assert db.scalar(select(m.EventOccurrence.id).limit(1)) is None
        org = db.get(m.Organisation, h.seed['organisation_id'])
        stamp = org.input_revision, org.availability_revision
    params = {'start': '2026-10-01', 'end': '2026-11-01', 'filter': json.dumps({
        'conditions': {'conditions': [line('programme_ids', hidden)]}})}
    client = h.client('facet_changing_scope')
    tags = {}
    for endpoint, key in [('sessions', 'shifts'), ('days', 'days')]:
        response = client.get('/api/schedule/' + endpoint, params=params)
        assert response.status_code == 200 and not response.json()[key], response.text
        tags[endpoint] = response.headers['etag']
    binding = h.command('admin', 'role.grant', {'identity_id': who, 'role': 'coordinator',
        'starts_on': '2020-01-01', 'reason': 'Synthetic filter-scope regression'})['record']
    for permitted in (True, False):
        if not permitted:
            h.command('admin', 'role.revoke', {'reason': 'Synthetic filter-scope regression'},
                      binding['id'], binding['revision'])
        with transaction(h.app.state.engine) as db:
            org = db.get(m.Organisation, h.seed['organisation_id'])
            assert (org.input_revision, org.availability_revision) == stamp
        for endpoint, key in [('sessions', 'shifts'), ('days', 'days')]:
            response = client.get('/api/schedule/' + endpoint, params=params, headers={'if-none-match': tags[endpoint]})
            assert response.status_code == 200, response.text
            assert bool(response.json()[key]) is permitted
            assert response.headers['etag'] != tags[endpoint]
            tags[endpoint] = response.headers['etag']
            assert client.get('/api/schedule/' + endpoint, params=params,
                              headers={'if-none-match': tags[endpoint]}).status_code == 304

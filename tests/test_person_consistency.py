"""Current people, rank codes and dated rules agree without rewriting historic data."""
from datetime import date, datetime

import pytest
from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services.audiences import profiles
from packages.contracts.schemas import CategoryInput
from packages.domain.people import current_people
from packages.domain.quotas import departmental_days
from packages.domain.validation import person_in_scope


def facts():
    return {'organisation': {'timezone': 'Asia/Riyadh'}, 'workspace': {'department_id': 'd'},
        'people': {'p': {'active': True, 'title': ''}, 'q': {'active': True, 'title': 'Explicit title'},
                   'gone': {'active': False, 'title': ''}},
        'categories': {'rank': {'name': 'Current rank', 'active': True, 'starts_on': '2020-01-01'},
                       'ended': {'name': 'Ended rank', 'active': True, 'starts_on': '2020-01-01', 'ends_on': '2026-10-03'}},
        'memberships': [{'person_id': 'p', 'department_id': 'd', 'category_id': 'rank', 'starts_on': '2020-01-01'},
                        {'person_id': 'q', 'department_id': 'd', 'category_id': 'ended', 'starts_on': '2020-01-01'},
                        {'person_id': 'gone', 'department_id': 'd', 'category_id': 'rank', 'starts_on': '2020-01-01'}],
        'programmes': {'prog': {'active': True, 'starts_on': '2026-09-01', 'ends_on': '2026-10-03'}},
        'programme_memberships': [{'person_id': 'p', 'programme_id': 'prog', 'starts_on': '2026-09-01', 'ends_on': '2026-11-01'}],
        'rotation_types': {'type': {'active': True, 'starts_on': '2026-09-01'}},
        'cohorts': {'cohort': {'programme_id': 'prog', 'starts_on': '2026-09-01', 'ends_on': '2026-11-01'}},
        'rotations': [{'person_id': 'p', 'rotation_type_id': 'type', 'cohort_id': 'cohort',
                       'starts_on': '2026-09-01', 'ends_on': '2026-11-01'}]}


def test_current_people_count_and_title_exclude_ended_rank_programme_and_rotation():
    data = facts()
    result = current_people(data, date(2026, 10, 3))
    assert result['active_people'] == 2 and result['people_without_current_rank'] == 1
    assert result['people']['p'] == {'display_title': 'Current rank', 'current_category_ids': ['rank'],
        'current_programme_ids': [], 'current_rotation_type_ids': [], 'current_cohort_ids': []}
    assert result['people']['q']['display_title'] == 'Explicit title'
    assert result['people']['gone']['display_title'] == ''
    assert data['people']['p']['title'] == ''  # display fallback is a projection, never stored data
    previous = current_people(data, date(2026, 10, 2))
    assert previous['people']['p']['current_programme_ids'] == ['prog']
    assert previous['people']['p']['current_rotation_type_ids'] == ['type']
    assert previous['people']['p']['current_cohort_ids'] == ['cohort']


@pytest.mark.parametrize('rank_change', [{'active': False}, {'starts_on': '2026-10-04'}, {'ends_on': '2026-10-03'}])
def test_inactive_future_or_ended_rank_is_not_current(rank_change):
    data = facts()
    data['categories']['rank'].update(rank_change)
    result = current_people(data, date(2026, 10, 3))
    assert result['people_without_current_rank'] == 2
    assert result['people']['p']['display_title'] == ''


@pytest.mark.parametrize('expired', ['rotation', 'cohort', 'programme', 'rotation_type'])
def test_expired_rotation_authority_stops_flat_and_condition_rules(expired):
    data = facts()
    data['programmes']['prog']['ends_on'] = '2026-11-01'
    row = {'rotation': data['rotations'][0], 'cohort': data['cohorts']['cohort'],
           'programme': data['programmes']['prog'], 'rotation_type': data['rotation_types']['type']}[expired]
    row['ends_on'] = '2026-10-03'
    start, end = datetime.fromisoformat('2026-10-03T08:00:00+03:00'), datetime.fromisoformat('2026-10-03T12:00:00+03:00')
    assert not person_in_scope(data, {'rotation_type_id': 'type'}, 'p', start, end)
    assert not person_in_scope(data, {'conditions': {'match': 'all', 'conditions': [
        {'field': 'rotation_type_ids', 'op': 'any', 'values': ['type']}]}}, 'p', start, end)


@pytest.mark.parametrize('scope', [{'programme_id': 'prog'}, {'rotation_type_id': 'type'},
    {'conditions': {'match': 'all', 'conditions': [{'field': 'cohort_ids', 'op': 'any', 'values': ['cohort']}]}}])
def test_quota_departmental_days_stop_at_parent_programme_end(scope):
    assert departmental_days(facts(), 'p', scope, '2026-09-29', '2026-10-06') == 4


def test_rank_codes_are_unique_on_create_update_and_bulk_with_422(h):
    one = h.configure('categories', {'name': 'Synthetic Consultant', 'short_code': 'CON', 'starts_on': '2020-01-01'})['record']
    reply = h.configure('categories', {'name': 'Synthetic Duplicate', 'short_code': 'CON', 'starts_on': '2020-01-01'}, status=422)
    assert reply['error']['code'] == 'RANK_CODE_TAKEN'
    two = h.configure('categories', {'name': 'Synthetic Resident', 'short_code': 'RES', 'starts_on': '2020-01-01'})['record']
    value = {key: two[key] for key in CategoryInput.model_fields}
    value['short_code'] = 'CON'
    h.configure('categories', value, two['id'], two['revision'], status=422)
    h.command('admin', 'catalogue.bulk_update', {'catalogue': 'categories', 'items': [
        {'id': two['id'], 'revision': two['revision']}], 'set': {'short_code': 'CON'}, 'reason': 'Duplicate rank code'}, status=422)
    values = {r['id']: r for r in h.get('admin', '/api/catalogues')['categories']}
    assert values[one['id']]['short_code'] == 'CON' and values[two['id']]['short_code'] == 'RES'
    # The current web form predates rank codes: omitting the new field must preserve a saved code.
    saved = h.configure('categories', {'name': 'Renamed consultant', 'starts_on': '2020-01-01'}, one['id'], one['revision'])['record']
    assert saved['short_code'] == 'CON'
    # A legacy imported grade suffix participates in the same uniqueness check.
    h.configure('categories', {'name': 'Legacy rank · LEG', 'starts_on': '2020-01-01'})
    h.configure('categories', {'name': 'New rank', 'short_code': 'LEG', 'starts_on': '2020-01-01'}, status=422)


def test_existing_duplicate_report_is_department_scoped_and_read_only(h):
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
        db.add(department)
        db.flush()
        ranks = [m.Category(department_id=h.seed['department_id'], name='Imported rank · DUP', starts_on=date(2020, 1, 1)),
                 m.Category(department_id=h.seed['department_id'], name='Explicit duplicate rank', short_code='DUP', starts_on=date(2020, 1, 1)),
                 m.Category(department_id=department.id, name='Other department rank', short_code='DUP', starts_on=date(2020, 1, 1))]
        db.add_all(ranks)
        db.flush()
        before = [record(r) for r in ranks]
        db.add(m.Category(department_id=department.id, name='Other department only', short_code='OTHER', starts_on=date(2020, 1, 1)))
    report = h.get('auditor', '/api/catalogues/categories/duplicate-codes')
    assert report['department_id'] == h.seed['department_id']
    assert len(report['duplicates']) == 1 and report['duplicates'][0]['short_code'] == 'DUP'
    assert {r['id'] for r in report['duplicates'][0]['ranks']} == {r['id'] for r in before[:2]}
    with transaction(h.app.state.engine) as db:
        assert [record(db.get(m.Category, r['id'])) for r in before] == before
    h.get('staff', '/api/catalogues/categories/duplicate-codes', status=403)
    h.configure('categories', {'name': 'Own department', 'short_code': 'OTHER', 'starts_on': '2020-01-01'})
    # Unrelated edits to an existing collision remain possible while the owner resolves its code explicitly.
    value = {key: before[0][key] for key in CategoryInput.model_fields}
    value['description'] = 'Review this existing duplicate'
    h.configure('categories', value, before[0]['id'], before[0]['revision'])


def test_catalogue_directory_and_home_use_current_rank_title_and_counts(h):
    person = h.configure('people', {'display_name': 'Synthetic title fallback'})['record']
    rank = h.configure('categories', {'name': 'Synthetic Current Rank', 'starts_on': '2020-01-01'})['record']
    h.command('coordinator', 'membership.create', {'person_id': person['id'], 'category_id': rank['id'], 'starts_on': '2020-01-01'})
    initial = h.client('coordinator').get('/api/catalogues')
    shown = next(p for p in initial.json()['people'] if p['id'] == person['id'])
    assert shown['title'] == '' and shown['display_title'] == 'Synthetic Current Rank'
    assert shown['current_category_ids'] == [rank['id']]
    assert next(p for p in h.get('staff', '/api/directory')['people'] if p['id'] == person['id'])['title'] == 'Synthetic Current Rank'
    value = {key: rank[key] for key in CategoryInput.model_fields}
    value['ends_on'] = '2026-10-03'
    h.configure('categories', value, rank['id'], rank['revision'])
    fresh = h.client('coordinator').get('/api/catalogues', headers={'If-None-Match': initial.headers['etag']})
    assert fresh.status_code == 200
    shown = next(p for p in fresh.json()['people'] if p['id'] == person['id'])
    assert shown['display_title'] == '' and shown['current_category_ids'] == []
    counts = h.get('coordinator', '/api/people/summary')
    assert counts == {'active_people': 4, 'people_without_current_rank': 1}
    assert next(i['count'] for i in h.get('coordinator', '/api/home')['attention'] if i['key'] == 'no_rank') == 1


def test_audience_ignores_ended_programme_even_with_unended_membership(h):
    person = h.seed['people'][0]
    programme = h.configure('programmes', {'name': 'Synthetic programme', 'starts_on': '2026-09-01', 'ends_on': '2026-11-01'})['record']
    h.configure('programme_memberships', {'person_id': person, 'programme_id': programme['id'],
        'starts_on': '2026-09-01', 'ends_on': '2026-11-01'})
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, programme['id']).ends_on = date(2026, 10, 3)
    with transaction(h.app.state.engine) as db:
        held = profiles(db, h.seed['department_id'], h.seed['organisation_id'], {person}, date(2026, 10, 3))
        assert not held[person].get('programme_ids')
        held = profiles(db, h.seed['department_id'], h.seed['organisation_id'], {person}, date(2026, 10, 2))
        assert held[person]['programme_ids'] == {programme['id']}

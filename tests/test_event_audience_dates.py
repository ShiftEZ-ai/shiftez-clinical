"""Event visibility and duty protection use audience facts on each occurrence's local date."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import audiences
from packages.domain.cp_sat import generate
from packages.domain.validation import evaluate
from .governance_support import identity
from .test_events import event, event_type, occurrences, repeat


EVENT_CONFLICT = 'RESTRICTION'
DATABASES = ['h', pytest.param('pg', marks=pytest.mark.postgres)]


def assert_visibility_and_protection(h, row, audience, expected_days):
    person, identity_id = h.seed['people'][0], h.seed['identities']['staff']
    all_rows = {item['start'][:10]: item for item in occurrences(h) if item['event_id'] == row['id']}
    assert set(all_rows) == {'2026-10-04', '2026-10-11', '2026-10-18'}
    shown = [item for item in occurrences(h, 'staff') if item['event_id'] == row['id']]
    assert {item['start'][:10] for item in shown} == expected_days
    for endpoint in ('days', 'sessions'):
        schedule = h.get('staff', f'/api/schedule/{endpoint}?start=2026-10-01&end=2026-11-01')
        shown = [item for item in schedule['events'] if item['event_id'] == row['id']]
        assert {item['start'][:10] for item in shown} == expected_days
        assert all(item['in_audience'] for item in shown)
    path = h.command('staff', 'calendar_feed.create')['path']
    feed = h.client('staff').get(path)
    assert feed.status_code == 200
    for day, occurrence in all_rows.items():
        expected = day in expected_days
        assert (f"UID:event-{occurrence['id']}@rota.invalid" in feed.text) is expected
        with transaction(h.app.state.engine) as db:
            reached = audiences.recipients(db, h.seed['department_id'], audience, date.fromisoformat(day))
        assert (identity_id in reached) is expected
        next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        workspace = h.shift(h.workspace(start=day, end=next_day),
            start=f'{day}T08:00:00+03:00', end=f'{day}T12:00:00+03:00', people=[person], seed_restricted=expected)['workspace']

        def conflict(validation):
            return any(f['code'] == EVENT_CONFLICT and f.get('person_id') == person
                for f in validation['findings'])

        assert conflict(workspace['validation']) is expected
        snapshot = h.snapshot(workspace)
        assert conflict(evaluate(snapshot)) is expected
        snapshot['shifts'][0]['participants'] = []
        snapshot['people'] = {person: snapshot['people'][person]}
        result = generate(snapshot, 5, deterministic=True)
        assert result['status'] == ('requirements_conflict' if expected else 'candidate_feasible'), result
        if expected:
            assert result['additions'] == []
        else:
            assert result['validation']['valid'] and result['additions']
            assert {item['participant']['person_id'] for item in result['additions']} == {person}


@pytest.mark.parametrize('database', DATABASES)
@pytest.mark.parametrize('tree', [False, True], ids=['compact-role', 'condition-role'])
@pytest.mark.parametrize('starts_on,ends_on,expected_days,current_role', [
    ('2026-10-11', '2026-10-18', {'2026-10-11'}, False),
    ('2020-01-01', '2026-10-04', set(), True),
    ('2026-10-05', None, {'2026-10-11', '2026-10-18'}, False),
], ids=['future-start-inclusive-end-exclusive', 'current-role-ended-before-meetings', 'starts-after-first-meeting'])
def test_role_audience_is_dated_for_each_occurrence(request, database, tree, starts_on, ends_on, expected_days, current_role):
    h = request.getfixturevalue(database)
    h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['staff'], 'role': 'credentialer',
        'starts_on': starts_on, 'ends_on': ends_on, 'reason': 'Synthetic dated meeting audience'})
    assert ('credentialer' in h.get('staff', '/api/me')['roles']) is current_role
    audience = {'where': {'match': 'all', 'conditions': [{'field': 'roles', 'values': ['credentialer']}]}} if tree else {'roles': ['credentialer']}
    row = event(h, event_type(h, blocks_duties=True), audience=audience, repeat=repeat(), repeat_until='2026-10-19')
    assert_visibility_and_protection(h, row, audience, expected_days)


@pytest.mark.parametrize('database', DATABASES)
def test_role_and_programme_audience_share_the_occurrence_date(request, database):
    h = request.getfixturevalue(database)
    person = h.seed['people'][0]
    h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['staff'], 'role': 'credentialer',
        'starts_on': '2026-10-04', 'reason': 'Synthetic future meeting role'})
    programme = h.configure('programmes', {'name': 'Synthetic dated meeting programme',
        'starts_on': '2026-10-11', 'ends_on': '2026-10-18'})['record']
    h.configure('programme_memberships', {'person_id': person, 'programme_id': programme['id'],
        'starts_on': '2026-10-11', 'ends_on': '2026-10-18'})
    audience = {'where': {'match': 'all', 'conditions': [
        {'field': 'roles', 'values': ['credentialer']}, {'field': 'programme_ids', 'values': [programme['id']]}]}}
    row = event(h, event_type(h, blocks_duties=True), audience=audience, repeat=repeat(), repeat_until='2026-10-19')
    assert_visibility_and_protection(h, row, audience, {'2026-10-11'})


@pytest.mark.parametrize('database', DATABASES)
@pytest.mark.parametrize('state', ['disabled', 'revoked', 'no-signin'])
def test_role_audience_excludes_disabled_revoked_and_missing_signins_but_explicit_people_still_block(request, database, state):
    h = request.getfixturevalue(database)
    person = h.seed['people'][1]
    if state != 'no-signin':
        identity_id = identity(h, 'dated-event-attendee', ['credentialer'], person=person)
        with transaction(h.app.state.engine, write=True) as db:
            if state == 'disabled':
                db.get(m.Identity, identity_id).disabled = True
            else:
                binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == identity_id))
                binding.revoked = True
    event(h, event_type(h, blocks_duties=True), audience={'roles': ['credentialer']})
    workspace = h.shift(h.workspace(), people=[person])['workspace']
    assert not any(f['code'] == EVENT_CONFLICT and f.get('person_id') == person
        for f in workspace['validation']['findings'])
    event(h, event_type(h, blocks_duties=True), audience={'person_ids': [person]})
    workspace = h.shift(h.workspace(), people=[person], seed_restricted=True)['workspace']
    assert any(f['code'] == EVENT_CONFLICT and f.get('person_id') == person
        for f in workspace['validation']['findings'])


@pytest.mark.parametrize('database', DATABASES)
def test_occurrence_date_uses_department_local_day_at_utc_boundary(request, database):
    h = request.getfixturevalue(database)
    person = h.seed['people'][0]
    h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['staff'], 'role': 'credentialer',
        'starts_on': '2026-10-04', 'reason': 'Synthetic local-date meeting role'})
    row = event(h, event_type(h, blocks_duties=True), audience={'roles': ['credentialer']},
        start='2026-10-03T21:30:00+00:00', end='2026-10-03T22:00:00+00:00')
    assert {item['event_id'] for item in occurrences(h, 'staff')} == {row['id']}
    workspace = h.shift(h.workspace(start='2026-10-04', end='2026-10-05'),
        start='2026-10-04T00:00:00+03:00', end='2026-10-04T01:30:00+03:00', people=[person], seed_restricted=True)['workspace']
    assert any(f['code'] == EVENT_CONFLICT and f.get('person_id') == person
        for f in workspace['validation']['findings'])

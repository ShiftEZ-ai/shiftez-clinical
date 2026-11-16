"""Review regressions for dated statuses, related facts and role-filter parity."""
from copy import deepcopy
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_bulk_single_commands import relationship


def resolved(h, selection):
    response = h.client('admin').post('/api/bulk/resolve', json=selection)
    assert response.status_code == 200, response.text
    return response.json()


def page(h, selection, **kwargs):
    response = h.client('admin').post('/api/bulk/rows', json={'selection': selection, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('status', ['Current', 'Ended'])
def test_revoked_open_grants_share_resolve_and_paged_status_predicates(h, status):
    grant, payload, role = relationship(h, 'grant', ends_on=None)
    other = h.command(role, 'grant.create', {**payload, 'person_id': h.seed['people'][1]})['record']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.CapabilityGrant, grant['id']).revoked = True
    selection = {'collection': 'grants', 'where': {'as_of': '2026-10-04',
        'filters': {'id': [grant['id'], other['id']], 'status': [status]}}}
    expected = {grant['id']} if status == 'Ended' else {other['id']}
    found = resolved(h, selection)
    assert found['count'] == 1 and {r['id'] for r in found['sample']} == expected
    first = page(h, selection, columns=['status'], limit=1)
    second = page(h, selection, columns=['status'], limit=1, offset=1)
    cached = page(h, selection, columns=['status'], limit=1)
    assert first['count'] == first['total'] == second['count'] == cached['count'] == 1
    assert {r['id'] for r in first['rows']} == expected and not second['rows']
    assert first['rows'] == cached['rows']
    assert first['rows'][0]['status'] == status.casefold()


@pytest.mark.parametrize('collection,field,related', [
    ('memberships', 'category_id', 'person.category_ids'),
    ('programme_memberships', 'programme_id', 'person.programme_ids'),
])
def test_related_person_facts_use_people_context_on_as_of_day(h, collection, field, related):
    catalogue = 'categories' if collection == 'memberships' else 'programmes'
    old, current = [h.configure(catalogue, {'name': name, 'starts_on': '2020-01-01'})['record']
                    for name in ('Historical rank or programme', 'Current rank or programme')]
    person = h.seed['people'][0]
    if collection == 'memberships':
        with transaction(h.app.state.engine, write=True) as db:
            for row in db.scalars(select(m.Membership).where(m.Membership.person_id == person)):
                row.ends_on = date(2025, 1, 1)
    def create(value):
        return (h.command('admin', 'membership.create', value) if collection == 'memberships'
                else h.configure(collection, value))['record']

    historical = create({'person_id': person, field: old['id'],
        'starts_on': '2020-01-01', 'ends_on': '2021-01-01'})
    create({'person_id': person, field: current['id'], 'starts_on': '2026-01-01', 'ends_on': '2027-01-01'})
    selection = {'collection': collection, 'where': {'as_of': '2026-10-04',
        'filters': {'id': [historical['id']]}}}
    row = page(h, selection, columns=[field, related])['rows'][0]
    assert row[field] == old['id'] and row[related] == [current['id']]
    selection['where']['filters'][related] = [current['id']]
    assert resolved(h, selection)['count'] == 1
    sorted_page = page(h, selection, columns=[related], sort=[{'field': related}], group=related)
    assert sorted_page['count'] == 1 and sorted_page['rows'][0][related] == [current['id']]
    selection['where']['filters'][related] = [old['id']]
    assert resolved(h, selection)['count'] == page(h, selection)['count'] == 0


@pytest.mark.parametrize('collection', ['people', 'identities', 'eligibilities'])
def test_roles_filter_matches_shared_condition_path_in_resolve_and_rows(h, collection):
    person = h.seed['people'][1]
    bound = identity(h, 'dated-role-selection', ['programme_lead'], person=person)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == bound))
        row.starts_on, row.ends_on = date(2026, 10, 4), date(2026, 10, 6)
    filtered = {'collection': collection, 'where': {'as_of': '2026-10-04',
        'filters': {'roles': ['programme_lead']}}}
    conditioned = deepcopy(filtered)
    conditioned['where'].pop('filters')
    conditioned['where']['conditions'] = {'conditions': [
        {'field': 'roles', 'op': 'any', 'values': ['programme_lead']}]}
    matches = resolved(h, conditioned)
    assert matches['count'] > 0
    expected = {r['id'] for r in matches['sample']}
    actual = resolved(h, filtered)
    assert actual['count'] == matches['count'] and {r['id'] for r in actual['sample']} == expected
    for selection in (filtered, conditioned):
        rows = page(h, selection)
        assert rows['count'] == matches['count'] and {r['id'] for r in rows['rows']} == expected
    for day in ('2026-10-03', '2026-10-06'):
        for selection in (filtered, conditioned):
            selection['where']['as_of'] = day
            assert resolved(h, selection)['count'] == page(h, selection)['count'] == 0
    filtered['where']['as_of'] = '2026-10-04'
    with transaction(h.app.state.engine, write=True) as db:
        row = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == bound))
        row.revoked, row.revision = True, row.revision + 1
    assert resolved(h, filtered)['count'] == page(h, filtered)['count'] == 0

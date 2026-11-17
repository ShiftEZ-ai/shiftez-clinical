"""Compact conversion summaries retain inspectable, hash-bound census evidence."""
from copy import deepcopy

import pytest

from apps.api import models as m
from apps.api.db import transaction
from tests.test_rule_catalogue_v2_api import proposed


pytestmark = pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)


@pytest.fixture
def proposal(h):
    h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    return proposed(h)


def test_preview_summarises_census_with_full_evidence_on_demand(h):
    source = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    path = '/api/rules/catalogue/migration?version=2'
    summary = h.get('coordinator', path)
    full = h.get('coordinator', path + '&details=true')
    census = full['census']
    assert summary['census'] == {'counts': census['counts'], 'hash': census['hash']}
    assert summary['plan_hash'] == full['plan_hash']
    assert {'sources', 'lines', 'history'} <= census.keys()
    assert source['id'] in {row['source_policy_id'] for row in census['sources']}
    assert census['lines']


def test_proposal_stores_the_hash_of_the_evidence_opened_by_its_approver(h, proposal):
    details = h.get('approver', '/api/rules/catalogue/conversions/' + proposal['id'] + '?details=true')['record']
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.CatalogueConversion, proposal['id'])
        assert stored.plan['census_hash'] == details['plan']['census']['hash'] == proposal['plan']['census']['hash']
        assert stored.plan['census'] == details['plan']['census']
    assert proposal['plan']['census'] == {
        'counts': details['plan']['census']['counts'], 'hash': details['plan']['census']['hash']}
    assert details['plan']['census']['sources'] and details['plan']['census']['lines']


@pytest.mark.parametrize('part', ['lines', 'counts', 'history', 'hash'])
def test_changed_census_refuses_both_evidence_and_approval(h, proposal, part):
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.CatalogueConversion, proposal['id'])
        plan = deepcopy(row.plan)
        if part == 'lines':
            plan['census']['lines'][0]['note'] = 'Changed captured evidence'
        elif part == 'counts':
            plan['census']['counts']['sources'] += 1
        elif part == 'history':
            plan['census']['history'].append({'source_policy_id': plan['bindings'][0]['source_policy_id']})
        else:
            plan['census']['hash'] = '0' * 64
        row.plan = plan
    refused = h.get('approver', '/api/rules/catalogue/conversions/' + proposal['id'] + '?details=true', status=409)
    assert refused['error']['code'] == 'EVIDENCE_MISMATCH' and 'record' not in refused
    decision = h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent evidence review'},
                         proposal['id'], proposal['revision'], status=409)
    assert decision['error']['code'] == 'EVIDENCE_MISMATCH'
    with transaction(h.app.state.engine) as db:
        row = db.get(m.CatalogueConversion, proposal['id'])
        assert row.status == 'proposed' and row.approved_by is None


@pytest.mark.parametrize('older_census', [False, True])
def test_legacy_proposal_without_an_evidence_hash_still_opens(h, proposal, older_census):
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.CatalogueConversion, proposal['id'])
        original = deepcopy(row.plan)
        original.pop('census_hash', None)
        if older_census:
            original['census'].pop('hash')
        row.plan = original
    path = '/api/rules/catalogue/conversions/' + proposal['id']
    assert h.get('approver', path)['record']['id'] == proposal['id']
    assert h.get('approver', path + '?details=true')['record']['plan'] == original

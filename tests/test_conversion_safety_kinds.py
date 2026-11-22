"""Catalogue conversion refuses advisory levels on protected safety rules."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.rule_catalogue import CATALOGUE_MAPPING, NEVER_OVERRIDDEN, RULE_CATALOGUE
from packages.domain.conditions import active_rules
from packages.domain.rule_catalogue_migration import plan_v2
from tests.test_rule_catalogue_api import approve, line, propose
from tests.test_rule_catalogue_v2_api import preview, proposed
from tests.test_rule_catalogue_v2_proof import source as legacy_source, uid
from tests.test_rule_safety_audit import safety_settings


SAFETY_REVIEW_REASON = 'Review this safety rule and approve a mandatory version before conversion'
V1_SAFETY_NUMBERS = [
    pytest.param(entry['id'], key, slot, id=f"{entry['id']}-{key}")
    for entry in RULE_CATALOGUE
    if CATALOGUE_MAPPING[entry['id']] and 'medium' in entry['levels']
    for key, slots in entry['numbers'].items()
    # Contract-advisory slots are scoring preferences under #144.
    for slot in slots if slot['kind'] in NEVER_OVERRIDDEN and not slot['advisory']
]


def store_level(h, source, index, key):
    # Safety kinds are always mandatory. Inject a stored level that pre-dates authoring validation.
    source['definition']['rule']['lines'][index]['numbers'][key]['level'] = 'medium'
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Policy, source['id']).definition = source['definition']
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1


def assert_refused(h, source, reason):
    plan = preview(h, proof=False)
    assert plan['blocked'] == [{
        'rule_id': source['id'], 'name': source['name'], 'kind': source['kind'], 'reason': reason,
        'proposal_ids': [], 'person_ids': [], 'line_ids': [],
    }]
    refused = h.command('coordinator', 'rule.migrate_to_catalogue', {
        'version': 2, 'plan_hash': plan['plan_hash'], 'reason': 'Review safety rule conversion',
    }, status=409)
    assert refused['error']['code'] == 'CONVERSION_BLOCKED'
    assert refused['error']['details'] == plan['blocked']
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Policy, source['id'])
        assert (stored.status, stored.definition) == ('active', source['definition'])
    assert h.get('approver', '/api/rules/catalogue/conversions')['records'] == []


@pytest.mark.parametrize('kind', sorted(NEVER_OVERRIDDEN))
def test_advisory_safety_conversion(h, kind):
    rule = {'kind': kind, **safety_settings(h)[kind]}
    if kind in {'supervision_limit', 'team_lead_requirement'}:
        rejected = h.command('coordinator', 'policy.create', {
            'name': 'Synthetic safety rule', 'owner': 'Synthetic owner', 'source': 'Synthetic safety fixture',
            'starts_on': '2020-01-01', 'definition': {'rule': rule, 'severity': 'advisory'},
        }, status=422)
        assert rejected['error']['code'] == 'SCHEMA_INVALID'
        assert any('Safety rules are always mandatory' in detail['message'] for detail in rejected['error']['details'])
        return

    # Safety kinds are always mandatory. A stored cover minimum cannot become an advisory target.
    source = (h.policy(rule, severity='advisory') if kind == 'spread_evenly'
              else h.stored_advisory_safety(rule))
    assert source['definition']['severity'] == 'advisory'
    reason = "'spread_evenly'" if kind == 'spread_evenly' else SAFETY_REVIEW_REASON
    assert_refused(h, source, reason)


def test_legacy_advisory_cover_converts_per_person_rows_and_inherited_default():
    source = legacy_source(6701, {'kind': 'minimum_cover', 'minimum': 2}, severity='advisory', strength='high',
                           listed_only=True, per_person=[{'person_id': uid(12), 'amount': 3},
                                                         {'person_id': uid(13), 'amount': None}])
    before = {'policies': [source]}
    plan = plan_v2(before['policies'], department_id=source['department_id'])
    # Safety kinds are always mandatory. Conversion cannot turn either minimum into a target.
    assert not plan['targets'] and not plan['bindings']
    [blocked] = plan['blocked']
    assert blocked['rule_id'] == source['id'] and blocked['reason'] == SAFETY_REVIEW_REASON
    assert [(p['definition']['rule']['minimum'], p['definition']['severity']) for p in active_rules([source])] == [
        (3, 'mandatory'), (2, 'mandatory')]


@pytest.mark.parametrize('entry_id,key,slot', V1_SAFETY_NUMBERS)
def test_v1_advisory_safety_conversion(h, entry_id, key, slot):
    kind = slot['kind']
    settings = safety_settings(h)[kind]
    value = settings.pop(slot['field'], None)
    if entry_id == 'companion_cover':
        settings['when_person_ids'] = [h.seed['people'][0]]
    source = approve(h, propose(h, entry_id, [
        line(numbers={key: {'value': value, 'level': 'mandatory'}}, settings={kind: settings}),
    ], catalogue_version=1)['record'])
    store_level(h, source, 0, key)
    assert source['definition']['rule']['lines'][0]['numbers'][key]['level'] == 'medium'
    assert_refused(h, source, SAFETY_REVIEW_REASON)


@pytest.mark.parametrize('level', ['medium', 'mandatory'])
def test_v1_mixed_or_mandatory_safety_conversion(h, level):
    source = approve(h, propose(h, 'rest_between_duties', [
        line(numbers={'min': {'value': 20, 'level': 'mandatory'}}, weekdays=[0]),
        line(numbers={'min': {'value': 12, 'level': 'mandatory'}}, weekdays=[1]),
    ], catalogue_version=1)['record'])
    if level == 'medium':
        store_level(h, source, 1, 'min')
        assert_refused(h, source, SAFETY_REVIEW_REASON)
        return

    change = proposed(h)
    active = h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent safety conversion review'},
                       change['id'], change['revision'])['record']
    assert active['status'] == 'active'
    [binding] = [b for b in active['plan']['bindings'] if b['source_policy_id'] == source['id']]
    first, second = binding['destinations']
    assert first['id'] == second['id']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, source['id']).status == 'retired'
        stored = db.get(m.Policy, first['id'])
        assert stored.status == 'active'
        assert stored.definition['rule']['catalogue_version'] == 2
        destination_ids = {first['line_id'], second['line_id']}
        assert {line['id']: line['numbers']['min']['level'] for line in stored.definition['rule']['lines']
                if line['id'] in destination_ids} == dict.fromkeys(destination_ids, 'mandatory')


def test_v1_cover_converts_with_mandatory_minimum_and_advisory_target(h):
    numbers = {'min': {'value': 2, 'level': 'mandatory'}, 'target': {'value': 3, 'level': 'medium'}}
    source = approve(h, propose(h, 'people_per_session', [line(numbers=numbers)], catalogue_version=1)['record'])
    plan = preview(h, proof=False)
    assert not plan['blocked'], plan['blocked']

    change = proposed(h)
    active = h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent safety conversion review'},
                       change['id'], change['revision'])['record']
    assert active['status'] == 'active'
    [binding] = [b for b in active['plan']['bindings'] if b['source_policy_id'] == source['id']]
    [destination] = binding['destinations']
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Policy, destination['id'])
        assert stored.status == 'active'
        assert stored.definition['rule']['catalogue_version'] == 2
        [converted] = [line for line in stored.definition['rule']['lines'] if line['id'] == destination['line_id']]
        assert converted['numbers'] == numbers

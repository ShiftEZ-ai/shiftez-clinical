"""Strict conversion proof for source variants reconstructed from facts."""
from copy import deepcopy

import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain.conditions import active_rules
from packages.domain.rule_catalogue_migration import after_snapshot, plan_v2
from packages.domain.rule_migration_proof import strict_expanded, strict_proof
from packages.domain.rule_map import rule_map
from packages.domain.validation import finding_observations, named_people, evaluate
from .test_rule_catalogue_v2_proof import fixture, source, uid


def legacy_policy(number, body, *, scope=None, rows=None, listed=False, department_id='d'):
    policy = source(number, body, severity='mandatory')
    policy['department_id'] = department_id
    definition = {**policy['definition'], 'scope': scope or {},
                  **({'per_person': rows, 'listed_only': listed} if rows is not None else {})}
    policy['definition'] = PolicyDefinition.model_validate(
        definition, context={'historical': True}).model_dump(mode='json')
    return policy


def facts_only_snapshot(base, policy):
    proposal = plan_v2([policy], department_id=base['workspace']['department_id'])
    assert not proposal['blocked'], proposal['blocked']
    before = deepcopy(base)
    before.update(policies=[deepcopy(policy)], scheduling_facts=[], source_bindings=[])
    after = deepcopy(before)
    after['scheduling_facts'] = deepcopy(proposal['scheduling_facts'])
    after.pop('source_bindings', None)
    return before, after, proposal


@pytest.mark.parametrize('shape', ['default_rows', 'listed_only', 'excluded_default', 'independent_windows',
                                   'identical_rows', 'mixed_safety'])
def test_fact_projection_strict_proof_keeps_legacy_semantics(h, shape):
    base = fixture(h, people=22, sessions=3)
    people = list(base['people'])
    first, second = people[:2]
    scope = {'conditions': {'match': 'all', 'conditions': [
        {'field': 'person_ids', 'op': 'any', 'values': [first, second]}]}}
    if shape == 'default_rows':
        body = {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'range', 'amount': 4,
                'maximum_amount': 8, 'target_amount': 6, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
        rows = [{'person_id': first, 'amount': 3}, {'person_id': second, 'amount': 5}]
        policy = legacy_policy(600, body, scope=scope, rows=rows,
                              department_id=base['workspace']['department_id'])
    elif shape == 'listed_only':
        body = {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 4,
                'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
        rows = [{'person_id': first, 'amount': 3}, {'person_id': second, 'amount': 5}]
        policy = legacy_policy(601, body, scope={}, rows=rows, listed=True,
                              department_id=base['workspace']['department_id'])
    elif shape == 'excluded_default':
        body = {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 12,
                'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
        policy = legacy_policy(602, body, scope=scope, rows=[{'person_id': first, 'amount': 8}],
                              department_id=base['workspace']['department_id'])
    elif shape == 'independent_windows':
        body = {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 12,
                'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
        rows = [{'person_id': first, 'amount': 8, 'starts_on': '2026-10-01', 'ends_on': '2026-10-08'},
                {'person_id': second, 'amount': 9, 'starts_on': '2026-10-08', 'ends_on': '2026-10-15'}]
        policy = legacy_policy(603, body, scope=scope, rows=rows,
                              department_id=base['workspace']['department_id'])
    elif shape == 'identical_rows':
        body = {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 12,
                'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
        rows = [{'person_id': first, 'amount': 8}, {'person_id': second, 'amount': 8}]
        policy = legacy_policy(604, body, scope=scope, rows=rows,
                              department_id=base['workspace']['department_id'])
    else:
        line = {'id': uid(6050), 'numbers': {'max': {'value': 12, 'level': 'mandatory'},
                                                'apply': {'value': None, 'level': 'mandatory'}},
                'settings': {'max_duties': {'window': 'week'}}, 'who': None}
        body = {'kind': 'catalogue', 'id': 'workload_limit', 'catalogue_version': 2,
                'on': True, 'settings': {}, 'lines': [line]}
        policy = legacy_policy(605, body, scope={}, department_id=base['workspace']['department_id'])
    if shape == 'mixed_safety':
        before = deepcopy(base)
        before.update(policies=[deepcopy(policy)], scheduling_facts=[], source_bindings=[])
        after = deepcopy(before)
        fact = {'id': uid(6051), 'department_id': policy['department_id'], 'person_id': first,
                'kind': 'workload', 'revision': 1, 'status': 'active', 'on': True,
                'data': {'catalogue_id': 'workload_limit', 'catalogue_version': 2,
                         'line': {'id': uid(6052), 'numbers': {'apply': {'value': None, 'level': 'mandatory'}},
                                  'source_bindings': {'apply': {'policy_id': policy['id'], 'line_index': 0,
                                                               'primitive_kind': 'restriction'}}}}}
        after['scheduling_facts'] = [fact]
    else:
        before, after, _ = facts_only_snapshot(base, policy)
    proof = strict_proof(before, after)
    assert proof['identical'], proof


def test_safety_source_facts_do_not_change_active_rules():
    policy = legacy_policy(606, {'kind': 'min_rest', 'hours': 12}, scope={})
    person = '00000000-0000-0000-0000-000000000001'
    line = deepcopy(plan_v2([policy], department_id=policy['department_id'])['targets'][0]['row']
                    ['definition']['rule']['lines'][0])
    line['who'] = {'match': 'all', 'conditions': [
        {'field': 'person_ids', 'op': 'any', 'values': [person]}]}
    fact = {'id': 'safety-fact', 'person_id': person, 'kind': 'workload', 'status': 'active', 'on': True,
            'data': {'catalogue_id': 'duty_spacing', 'catalogue_version': 2,
                     'line': {**line, 'id': 'safety-line'}}}
    without = active_rules({'policies': [policy]})
    with_facts = active_rules({'policies': [policy], 'scheduling_facts': [fact]})
    assert with_facts == without


def test_safety_person_fact_with_distinct_settings_is_appended():
    person = '00000000-0000-0000-0000-000000000001'
    carrier = legacy_policy(612, {'kind': 'min_rest', 'hours': 8})
    proposal = plan_v2([carrier], department_id=carrier['department_id'])
    target = deepcopy(proposal['targets'][0]['row'])
    carrier_line = deepcopy(target['definition']['rule']['lines'][0])
    fact_line = deepcopy(carrier_line)
    fact_line['id'] = uid(6122)
    fact_line['who'] = {'match': 'all', 'conditions': [
        {'field': 'person_ids', 'op': 'any', 'values': [person]}]}
    fact_line['settings'] = {**fact_line['settings'], 'forbidden_transition': {}}
    fact = {'id': uid(6121), 'department_id': carrier['department_id'], 'person_id': person,
            'kind': 'workload', 'revision': 1, 'status': 'active', 'on': True,
            'data': {'catalogue_id': 'duty_spacing', 'catalogue_version': 2, 'line': fact_line}}

    copies = active_rules({'policies': [target], 'scheduling_facts': [fact]})

    assert {copy['template_line']['id'] for copy in copies} == {carrier_line['id'], fact_line['id']}
    assert len(copies) == 2


def safety_paths(default_hours, fact_hours):
    person = '00000000-0000-0000-0000-000000000001'
    carrier = legacy_policy(608 + default_hours + fact_hours, {'kind': 'min_rest', 'hours': default_hours},
                            rows=[{'person_id': person, 'amount': default_hours}])
    proposal = plan_v2([carrier], department_id=carrier['department_id'])
    assert not proposal['blocked'], proposal['blocked']
    target = deepcopy(proposal['targets'][0]['row'])
    binding = deepcopy(proposal['bindings'][0])
    row = deepcopy(target['definition']['rule']['lines'][1])
    fact_line = deepcopy(row)
    fact_line['id'] = uid(6100 + default_hours + fact_hours)
    fact_line['numbers']['min'] = {'value': fact_hours, 'level': 'mandatory'}
    fact = {'id': uid(6200 + default_hours + fact_hours), 'department_id': carrier['department_id'],
            'person_id': person, 'kind': 'workload', 'revision': 1, 'status': 'active', 'on': True,
            'data': {'catalogue_id': 'duty_spacing', 'catalogue_version': 2, 'line': fact_line}}
    binding['destinations'].append({'kind': 'fact', 'id': fact['id'], 'line_id': fact_line['id'],
                                    'source_line_id': row['source_bindings']['min']['line_id'],
                                    'source_line_index': 0, 'person_id': person})
    return person, ({'policies': [target], 'scheduling_facts': [fact], 'source_bindings': [binding]},
                    {'policies': [carrier], 'scheduling_facts': [fact]}), fact


def test_looser_safety_person_fact_survives_both_paths_and_counterfactuals():
    person, paths, fact = safety_paths(12, 8)

    def bound_hours(snapshot):
        copies = active_rules(snapshot)
        applicable = [copy['definition']['rule']['hours'] for copy in copies
                      if (people := named_people(copy['definition']['scope'])) is None or person in people]
        return max(applicable)

    binding_path, projection_first_path = paths
    assert bound_hours(binding_path) == 12.0
    assert bound_hours({**binding_path, 'scheduling_facts': []}) == 12.0
    assert bound_hours(projection_first_path) == 12.0
    assert bound_hours({'policies': [], 'scheduling_facts': [fact]}) == 8.0


def test_identical_safety_person_fact_has_one_rule_map_line_and_finding(h):
    base = fixture(h, people=22, sessions=2)
    person = next(iter(base['people']))
    base['policies'] = []
    base['shifts'][1]['start'] = '2026-10-01T13:00:00+03:00'
    base['shifts'][1]['end'] = '2026-10-01T17:00:00+03:00'
    base['shifts'][1]['participants'][0].update(start=base['shifts'][1]['start'], end=base['shifts'][1]['end'], person_id=person)
    base['shifts'][0]['participants'][0]['person_id'] = person
    carrier = legacy_policy(611, {'kind': 'min_rest', 'hours': 12}, department_id=base['workspace']['department_id'])
    proposal = plan_v2([carrier], department_id=carrier['department_id'])
    assert not proposal['blocked'], proposal['blocked']
    target = deepcopy(proposal['targets'][0]['row'])
    binding = deepcopy(proposal['bindings'][0])
    line = deepcopy(target['definition']['rule']['lines'][0])
    line['id'] = uid(6112)
    line['who'] = {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [person]}]}
    fact = {'id': uid(6111), 'department_id': carrier['department_id'], 'person_id': person,
            'kind': 'workload', 'revision': 1, 'status': 'active', 'on': True,
            'data': {'catalogue_id': 'duty_spacing', 'catalogue_version': 2, 'line': line}}
    binding['destinations'].append({'kind': 'fact', 'id': fact['id'], 'line_id': line['id'],
                                    'source_line_id': target['definition']['rule']['lines'][0]['source_bindings']['min']['line_id'],
                                    'source_line_index': -1, 'person_id': person})
    snapshot = {**base, 'policies': [target], 'scheduling_facts': [fact], 'source_bindings': [binding]}
    mapped = rule_map(snapshot)
    mapped_rule = next(row for row in mapped['rules'] if row['name'] == 'Rest between duties')
    assert len(mapped_rule['lines']) == 1
    findings = [finding for finding in finding_observations(evaluate(snapshot)) if finding.get('person_id') == person]
    assert len(findings) == 1
    try:
        from packages.domain.finding_identity import finding_key as substance_key
    except ModuleNotFoundError:
        substance_key = None
    if substance_key:
        assert len({substance_key(finding) for finding in findings}) == 1


def test_stricter_safety_person_fact_survives_binding_and_projection_first_paths():
    person = '00000000-0000-0000-0000-000000000001'
    carrier = legacy_policy(609, {'kind': 'min_rest', 'hours': 8},
                            rows=[{'person_id': person, 'amount': 8}])
    proposal = plan_v2([carrier], department_id=carrier['department_id'])
    assert not proposal['blocked'], proposal['blocked']
    target = deepcopy(proposal['targets'][0]['row'])
    binding = deepcopy(proposal['bindings'][0])
    row = deepcopy(target['definition']['rule']['lines'][1])
    fact_line = deepcopy(row)
    fact_line['id'] = uid(6092)
    fact_line['numbers']['min'] = {'value': 12, 'level': 'mandatory'}
    fact = {'id': uid(6091), 'department_id': carrier['department_id'], 'person_id': person,
            'kind': 'workload', 'revision': 1, 'status': 'active', 'on': True,
            'data': {'catalogue_id': 'duty_spacing', 'catalogue_version': 2, 'line': fact_line}}
    binding['destinations'].append({'kind': 'fact', 'id': fact['id'], 'line_id': fact_line['id'],
                                    'source_line_id': row['source_bindings']['min']['line_id'],
                                    'source_line_index': 0, 'person_id': person})

    def bound_hours(rules):
        copies = active_rules(rules)
        applicable = [copy['definition']['rule']['hours'] for copy in copies
                      if (people := named_people(copy['definition']['scope'])) is None or person in people]
        return max(applicable)

    binding_path = {'policies': [target], 'scheduling_facts': [fact], 'source_bindings': [binding]}
    projection_first_path = {'policies': [carrier], 'scheduling_facts': [fact]}
    assert bound_hours(binding_path) == 12.0
    assert bound_hours({**binding_path, 'scheduling_facts': []}) == 8.0
    assert bound_hours(projection_first_path) == 12.0
    assert bound_hours({'policies': [carrier], 'scheduling_facts': []}) == 8.0


def test_binding_reconstruction_matches_fact_projection_when_both_are_available(h):
    base = fixture(h, people=22, sessions=3)
    first, second = list(base['people'])[:2]
    policy = legacy_policy(607, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum',
                                 'amount': 12, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
                           scope={'conditions': {'match': 'all', 'conditions': [
                               {'field': 'person_ids', 'op': 'any', 'values': [first, second]}]}},
                           rows=[{'person_id': first, 'amount': 8}],
                           department_id=base['workspace']['department_id'])
    proposal = plan_v2([policy], department_id=base['workspace']['department_id'])
    assert not proposal['blocked'], proposal['blocked']
    before = deepcopy(base)
    before.update(policies=[deepcopy(policy)], scheduling_facts=[], source_bindings=[])
    binding_snapshot = after_snapshot(before, proposal)
    fact_snapshot = deepcopy(before)
    fact_snapshot['scheduling_facts'] = deepcopy(proposal['scheduling_facts'])
    fact_snapshot.pop('source_bindings', None)
    assert strict_expanded(binding_snapshot, fact_snapshot)['equal']

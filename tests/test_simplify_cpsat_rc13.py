"""Simplify and the scheduler validate the same reconstructed catalogue sources."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import PolicyDefinition
from packages.domain import cp_sat, rule_catalogue_migration
from packages.domain.rule_catalogue_migration import after_snapshot, plan_v2
from packages.domain.rule_lines import bound_policies
from packages.domain.rule_migration_proof import canonical_full_model, strict_proof
from packages.domain.rule_serialization import stored_definition
from packages.domain.solver_rules import compatibility_issues, definition_issues
from .test_rule_catalogue_v2_api import proposed
from .test_rule_catalogue_v2_proof import fixture, source, uid


def catalogue_rule(count):
    return {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': [
        {'id': uid(80000 + n), 'weekdays': [n % 7], 'start_time': f'00:{n // 7:02}',
         'numbers': {'max': {'value': 100 + n}}}
        for n in range(count)]}


def test_storage_serializer_strips_only_private_metadata_without_mutating_source():
    definition = {'rule': catalogue_rule(1), '_cache': {'computed': True}}
    line = definition['rule']['lines'][0]
    line.update(_source_index=7, _source_identity=uid(123), public_typo=True)
    line['numbers']['max']['_derived'] = 'not a stored number'
    original = deepcopy(definition)
    clean = stored_definition(definition)
    assert definition == original
    assert '_cache' not in clean
    assert clean['rule']['lines'][0] == {
        'id': uid(80000), 'weekdays': [0], 'start_time': '00:00',
        'numbers': {'max': {'value': 100}}, 'public_typo': True}
    assert any(issue['field'].endswith('.public_typo') for issue in definition_issues(definition))
    del definition['rule']['lines'][0]['public_typo']
    assert definition_issues(definition) == []


@pytest.mark.parametrize('edit', [False, True])
@pytest.mark.parametrize('settings_at', ['rule', 'line'])
def test_catalogue_proposal_stores_no_private_settings_on_create_or_edit(h, edit, settings_at):
    payload = {'catalogue_id': 'duties_per_week', 'catalogue_version': 1,
               'lines': [{'id': uid(81000), 'numbers': {'max': {'value': None}}}],
               'reason': 'Synthetic private settings regression'}
    if edit:
        previous = h.command('coordinator', 'rule.propose', payload)['record']
        previous = h.command('approver', 'policy.activate', {'reason': 'Independent synthetic approval'},
                             previous['id'], previous['revision'])['record']
        payload.update(expected_policy_id=previous['id'], expected_revision=previous['revision'])
    settings = {'max_duties': {'window': 'week', '_source_index': 17,
                              '_cache': {'_derived': ['synthetic metadata']}}}
    (payload if settings_at == 'rule' else payload['lines'][0])['settings'] = settings
    proposed = h.command('coordinator', 'rule.propose', payload)['record']
    assert proposed['definition'] == stored_definition(proposed['definition'])
    body = proposed['definition']['rule']
    assert (body if settings_at == 'rule' else body['lines'][0])['settings'] == {'max_duties': {'window': 'week'}}
    with transaction(h.app.state.engine) as db:
        policy = db.get(m.Policy, proposed['id'])
        saved = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == policy.id))
        assert policy.definition == saved.snapshot['definition'] == proposed['definition']


def test_workload_replacement_does_not_copy_private_legacy_metadata(h):
    from .test_scheduling_facts_api import decide, replace, workload
    original = decide(h, h.command('coordinator', 'person.workload.propose', workload(h))['fact'])['fact']
    with transaction(h.app.state.engine, write=True) as db:
        old = db.get(m.SchedulingFact, original['id'])
        old.data = {**old.data, '_derived': {'_cache': 'synthetic old metadata'}}
    newer = replace(h, original, 4)
    assert newer['data'] == stored_definition(newer['data'])
    assert newer['data']['line']['numbers']['max']['value'] == 4
    with transaction(h.app.state.engine) as db:
        assert db.get(m.SchedulingFact, newer['id']).data == newer['data']
        assert db.get(m.SchedulingFact, original['id']).data['_derived'] == {'_cache': 'synthetic old metadata'}
        saved = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == newer['id']))
        assert saved.snapshot['data'] == newer['data']


@pytest.mark.parametrize('definition', [
    {'rule': catalogue_rule(2)},
    {'severity': 'advisory', 'rule': {'kind': 'allocation_preference', 'priority': 1, 'balance_weight': 0.1234567}},
    {'exception_allowed': True, 'rule': {'kind': 'min_rest', 'hours': 12}},
])
def test_compiler_checks_preserve_typed_definition_precision_and_context(definition):
    value = PolicyDefinition.model_validate(definition, context={'historical': True})
    before = value.model_dump(mode='json')
    for historical in (False, True):
        assert definition_issues(value, historical=historical) == definition_issues(before, historical=historical)
    assert value.model_dump(mode='json') == before


def test_dev_scale_catalogue_lines_pass_identical_strict_runtime_and_proof_models(h):
    before = fixture(h, people=144, sessions=1)
    before['policies'] = [source(9000, catalogue_rule(291))]
    plan = plan_v2(before['policies'], department_id=before['workspace']['department_id'])
    assert plan['blocked'] == []
    after = after_snapshot(before, plan)
    runtime = deepcopy(after)
    runtime['policies'] = bound_policies(runtime['policies'], runtime)
    runtime['_catalogue_v2_materialized'] = True
    assert len(runtime['policies'][0]['definition']['rule']['lines']) == 291
    assert all('_source_index' in line for line in runtime['policies'][0]['definition']['rule']['lines'])
    assert compatibility_issues(after) == compatibility_issues(runtime) == []
    models = [canonical_full_model(data) for data in (before, after, runtime)]
    assert all(model['ready'] for model in models), models
    assert len({model['digest'] for model in models}) == 1
    assert models[0]['constraints'] and models[0]['tiers']


def test_behavior_proof_fails_when_reconstructed_source_fails_scheduler_validation(h, monkeypatch):
    before = fixture(h, people=22, sessions=1)
    before['policies'] = [source(9000, catalogue_rule(2))]
    after = after_snapshot(before, plan_v2(before['policies'], department_id=before['workspace']['department_id']))
    original = rule_catalogue_migration.source_policy

    def broken_adapter(binding, destinations):
        policy = original(binding, destinations)
        policy['definition']['rule']['lines'][0]['public_typo'] = True
        return policy

    monkeypatch.setattr(rule_catalogue_migration, 'source_policy', broken_adapter)
    proof = strict_proof(before, after)
    assert proof['expanded']['equal']  # Primitive/greedy equivalence alone cannot see this defect.
    assert not proof['model']['equal'] and not proof['identical']
    assert proof['model']['after']['status'] == 'unsupported_rules'
    runtime = {**after, 'policies': bound_policies(after['policies'], after), '_catalogue_v2_materialized': True}
    result = cp_sat.generate(runtime, 1, feasibility_only=True)
    assert result['status'] == 'unsupported_rules'
    assert any(issue['field'].endswith('.public_typo') for issue in result['issues'])


def test_activated_simplify_catalogue_runs_real_cp_sat_feasibility_without_definition_errors(h):
    original = h.command('coordinator', 'rule.propose', {
        'catalogue_id': 'duties_per_week', 'catalogue_version': 1, 'lines': catalogue_rule(38)['lines'],
        'reason': 'Synthetic legacy catalogue fixture'})['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent fixture review'}, original['id'], original['revision'])
    workspace = h.workspace()
    h.shift(workspace)
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent CP-SAT regression review'},
              conversion['id'], conversion['revision'])
    snapshot = h.snapshot(workspace)
    assert any('_source_index' in line for policy in snapshot['policies']
               for line in policy['definition']['rule'].get('lines', []))
    assert compatibility_issues(snapshot) == []
    result = cp_sat.generate(snapshot, 2, feasibility_only=True, deterministic=True)
    assert result['status'] in {'candidate_feasible', 'requirements_conflict'}, result
    assert not any(issue.get('code') == 'INVALID_RULE_DEFINITION' for issue in result.get('issues', []))
    with transaction(h.app.state.engine) as db:
        for policy in db.scalars(select(m.Policy)):
            assert policy.definition == stored_definition(policy.definition)


@pytest.mark.parametrize('key', ['min', 'max', 'target'])
def test_approved_listed_bound_clear_builds_real_strict_model_without_restoring_a_default(h, key):
    original = h.command('coordinator', 'policy.create', {
        'name': 'Synthetic listed clinical ranges', 'owner': 'Synthetic department', 'source': 'Synthetic acceptance fixture',
        'starts_on': '2026-01-01', 'definition': {'listed_only': True,
            'rule': {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'range', 'amount': 1, 'maximum_amount': 8,
                     'target_amount': 4, 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'},
            'per_person': [{'person_id': person, 'amount': 2 + n} for n, person in enumerate(h.seed['people'][:2])]}})['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent listed fixture review'}, original['id'], original['revision'])
    workspace = h.workspace()
    h.shift(workspace)
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'}, conversion['id'], conversion['revision'])
    before = h.snapshot(workspace)
    with transaction(h.app.state.engine) as db:
        previous = db.scalar(select(m.SchedulingFact).where(m.SchedulingFact.person_id == h.seed['people'][0]))
        payload = {'kind': previous.kind, 'person_id': previous.person_id, 'replaces_id': previous.id,
                   'expected_revision': previous.revision, 'reason': 'Clear this bound with independent review', **deepcopy(previous.data)}
    payload.pop('exception_allowed', None)
    payload['line']['numbers'][key]['value'] = None
    revised = h.command('coordinator', 'person.workload.propose', payload)['fact']
    h.command('approver', 'person.workload.activate', {'reason': 'Independent bound removal review'}, revised['id'], revised['revision'])
    after = h.snapshot(workspace)
    assert compatibility_issues(after) == []
    proof = strict_proof(before, after)
    assert proof['model']['before']['ready'] and proof['model']['after']['ready'], proof['model']
    assert not proof['model']['equal'] and not proof['identical']

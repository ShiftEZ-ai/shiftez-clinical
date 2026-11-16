"""Technical metadata repair appends versions without changing public rule definitions."""
import copy
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.serialization import record
from packages.domain.rule_serialization import stored_definition
from packages.domain.validation import evaluate
from scripts.repair_rule_definitions import run


def dirty(h, policy, *, invalid=False):
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, policy['id'])
        definition = copy.deepcopy(row.definition)
        definition['_derived'] = {'private': 'never print this'}
        definition['rule']['_source_index'] = 0
        if definition['rule'].get('lines'):
            definition['rule']['lines'][0]['_source_index'] = 0
        if invalid:
            definition['rule']['unrecognized_public_setting'] = 4
        row.definition = definition
        row.revision += 1
        db.flush()
        db.add(m.DefinitionHistory(department_id=row.department_id, kind='policy', object_id=row.id,
            object_revision=row.revision, snapshot=record(row), actor_id=h.seed['identities']['admin'],
            reason='Synthetic legacy build persisted private metadata'))


def state(h):
    with transaction(h.app.state.engine) as db:
        return {cls.__tablename__: [record(row) for row in db.scalars(select(cls).order_by(cls.id))]
                for cls in (m.Organisation, m.Policy, m.DefinitionHistory, m.AuditEvent,
                            m.Publication, m.RuleSourceBinding, m.CatalogueConversion)}


def execute(h, **kwargs):
    return run(h.app.state.engine, h.seed['identities']['admin'], h.seed['department_id'], **kwargs)


def test_repair_dry_run_append_only_history_and_idempotence(h):
    rule = h.policy({'kind': 'max_duties', 'maximum': 3, 'window': 'week'})
    dirty(h, rule)
    before = state(h)
    preview = execute(h)
    assert preview['count'] == 1 and not preview['applied']
    assert state(h) == before
    assert execute(h, apply=True, reason='Owner-authorized metadata cleanup')['count'] == 1
    after = state(h)
    updated = next(row for row in after['policies'] if row['id'] == rule['id'])
    original = next(row for row in before['policies'] if row['id'] == rule['id'])
    assert updated == {**original, 'revision': original['revision'] + 1,
                       'definition': stored_definition(original['definition'])}
    assert after['organisations'][0]['input_revision'] == before['organisations'][0]['input_revision'] + 1
    history = {row['id']: row for row in after['definition_history']}
    assert all(history[row['id']] == row for row in before['definition_history'])
    assert len(history) == len(before['definition_history']) + 1
    assert len(after['audit_events']) == len(before['audit_events']) + 1
    audit = next(row for row in after['audit_events'] if row['action'] == 'policy.repair_private_metadata')
    assert audit['detail'] == {'rules': preview['rules']} and audit['actor_id'] == h.seed['identities']['admin']
    assert execute(h, apply=True, reason='Repeated metadata cleanup')['count'] == 0
    assert state(h) == after


def test_repair_all_current_versions_is_department_scoped(h):
    active = h.policy({'kind': 'max_duties', 'maximum': 3, 'window': 'week'})
    retired = h.policy({'kind': 'max_duties', 'maximum': 4, 'window': 'week'})
    h.command('approver', 'policy.retire', {'reason': 'Retain old version'}, retired['id'], retired['revision'])
    dirty(h, active)
    dirty(h, retired)
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=db.get(m.Department, h.seed['department_id']).organisation_id,
                                  name='Other synthetic department')
        db.add(department)
        db.flush()
        other = m.Policy(department_id=department.id, name='Other synthetic rule', kind='max_duties',
            definition=copy.deepcopy(db.get(m.Policy, active['id']).definition), owner='Test', source='Test',
            created_by=h.seed['identities']['admin'], starts_on=date(2020, 1, 1), status='active')
        db.add(other)
        unrecorded = m.Policy(department_id=h.seed['department_id'], name='Unrecorded synthetic proposal', kind='max_duties',
            definition=copy.deepcopy(other.definition), owner='Test', source='Test',
            created_by=h.seed['identities']['admin'], starts_on=date(2020, 1, 1), status='proposed')
        db.add(unrecorded)
        db.flush()
        other_id = other.id
        unrecorded_id = unrecorded.id
    before = state(h)
    assert execute(h, apply=True, reason='Scope-specific metadata repair')['count'] == 3
    after = state(h)
    assert next(row for row in after['policies'] if row['id'] == other_id) == next(
        row for row in before['policies'] if row['id'] == other_id)
    assert after['organisations'][0]['input_revision'] == before['organisations'][0]['input_revision'] + 1
    retained = sorted((row for row in after['definition_history'] if row['object_id'] == unrecorded_id),
                      key=lambda row: row['object_revision'])
    assert [row['object_revision'] for row in retained] == [1, 2]
    assert retained[0]['snapshot'] == next(row for row in before['policies'] if row['id'] == unrecorded_id)
    assert '_derived' not in retained[1]['snapshot']['definition']


def test_repair_invalid_public_definition_refuses_entire_transaction(h):
    good = h.policy({'kind': 'max_duties', 'maximum': 3, 'window': 'week'})
    invalid = h.policy({'kind': 'max_duties', 'maximum': 4, 'window': 'week'})
    dirty(h, good)
    dirty(h, invalid, invalid=True)
    before = state(h)
    with pytest.raises(Problem, match='still fails strict validation') as error:
        execute(h, apply=True, reason='Cannot sanitize public invalid fields')
    assert error.value.code == 'INVALID_RULE_DEFINITION'
    assert state(h) == before


def test_repair_requires_enabled_admin_and_reason(h):
    rule = h.policy({'kind': 'max_duties', 'maximum': 3, 'window': 'week'})
    dirty(h, rule)
    before = state(h)
    with pytest.raises(Problem) as denied:
        run(h.app.state.engine, h.seed['identities']['coordinator'], h.seed['department_id'],
            apply=True, reason='Unauthorized maintenance')
    assert denied.value.code == 'FORBIDDEN'
    with pytest.raises(Problem) as missing:
        execute(h, apply=True)
    assert missing.value.code == 'REASON_REQUIRED'
    assert state(h) == before
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['admin']).disabled = True
    with pytest.raises(Problem) as disabled:
        execute(h, apply=True, reason='Disabled identity cannot perform maintenance')
    assert disabled.value.code == 'ACCESS_REVOKED'
    assert state(h) == before


def test_repair_preserves_scheduling_and_conversion_evidence_but_guards_undo(h):
    from packages.domain.rule_migration_proof import canonical_full_model
    from tests.test_rule_catalogue_v2_api import proposed
    h.policy({'kind': 'max_duties', 'maximum': 3, 'window': 'week'})
    conversion = proposed(h)
    activated = h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent metadata fixture review'},
                          conversion['id'], conversion['revision'])['record']
    with transaction(h.app.state.engine) as db:
        destination = record(next(policy for policy in db.scalars(select(m.Policy).where(
            m.Policy.kind == 'catalogue', m.Policy.status == 'active'))
            if policy.definition['rule']['id'] == 'workload_limit'))
    workspace = h.shift(h.workspace())['workspace']
    baseline_snapshot = h.snapshot(workspace)
    baseline = evaluate(baseline_snapshot)
    baseline_model = canonical_full_model(baseline_snapshot)
    assert baseline_model['ready']
    dirty(h, destination)
    before = state(h)
    assert execute(h, apply=True, reason='Fix stored private catalogue line keys')['count'] == 1
    after = state(h)
    repaired_snapshot = h.snapshot(workspace)
    validation = evaluate(repaired_snapshot)
    assert validation['fingerprint'] != baseline['fingerprint']
    assert {key: value for key, value in validation.items() if key != 'fingerprint'} == {
        key: value for key, value in baseline.items() if key != 'fingerprint'}
    repaired_model = canonical_full_model(repaired_snapshot)
    assert repaired_model['status'] == baseline_model['status'] == 'model_ready'
    assert repaired_model['variables'] == baseline_model['variables']
    assert repaired_model['constraints'] == baseline_model['constraints']
    assert repaired_model['tiers'] == baseline_model['tiers']
    # Repair creates a new source revision while the compiled model stays identical.
    assert repaired_model['digest'] == baseline_model['digest']
    for table in ('catalogue_conversions', 'rule_source_bindings', 'publications'):
        assert after[table] == before[table]
    undo = h.command('approver', 'rule.catalogue.undo', {'reason': 'Repair is a later recorded version'},
                     activated['id'], activated['revision'], status=409)
    assert undo['error']['code'] == 'LATER_EDIT'


@pytest.mark.parametrize('starts_on,ends_on', [(date(2026, 10, 4), date(2026, 10, 11)),
                                             (date(2026, 9, 20), date(2026, 9, 27))])
def test_repair_refuses_revision_bound_exception_on_reconstructed_source(h, starts_on, ends_on):
    from tests.test_rule_catalogue_v2_api import proposed
    source = h.policy({'kind': 'max_duties', 'maximum': 3, 'window': 'week'}, exception=True)
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion fixture'},
              conversion['id'], conversion['revision'])
    with transaction(h.app.state.engine, write=True) as db:
        destination = record(next(policy for policy in db.scalars(select(m.Policy).where(
            m.Policy.kind == 'catalogue', m.Policy.status == 'active'))
            if any(pin['policy_id'] == source['id'] for line in policy.definition['rule']['lines']
                   for pin in line.get('source_bindings', {}).values())))
        db.add(m.PolicyException(department_id=h.seed['department_id'], policy_id=source['id'],
            policy_revision=source['revision'], person_id=h.seed['people'][0], starts_on=starts_on,
            ends_on=ends_on, reason='Synthetic approved exception', status='approved',
            created_by=h.seed['identities']['coordinator'], approved_by=h.seed['identities']['approver']))
    dirty(h, destination)
    before = state(h)
    with pytest.raises(Problem) as error:
        execute(h, apply=True, reason='Must preserve effective exception')
    assert error.value.code == 'REVISION_DEPENDENCY'
    assert state(h) == before


@pytest.mark.parametrize('field', ['rule_adjustments', 'target_overrides'])
def test_repair_refuses_current_rota_revision_dependencies(h, field):
    source = h.policy({'kind': 'max_duties', 'maximum': 3, 'window': 'week'})
    workspace = h.workspace()
    dirty(h, source)
    with transaction(h.app.state.engine, write=True) as db:
        setattr(db.get(m.Workspace, workspace['id']), field,
                [{'policy_id': source['id'], 'policy_revision': source['revision'] + 1}])
    before = state(h)
    with pytest.raises(Problem) as error:
        execute(h, apply=True, reason='Must preserve current rota adjustment')
    assert error.value.code == 'REVISION_DEPENDENCY'
    assert state(h) == before

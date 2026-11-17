"""Scoring defaults preserve catalogue admission and reviewed conversion semantics."""
from copy import deepcopy
from datetime import date
import importlib
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.serialization import record
from apps.api.services import rule_catalogue, scoring_defaults
from apps.api.services.common import history
from packages.contracts.schemas import PolicyDefinition, TypeDefinition
from packages.domain.rule_catalogue_migration import after_snapshot, plan_v2
from packages.domain.rule_lines import department_catalogue_version
from packages.domain.rule_migration_proof import strict_proof
from .test_rule_catalogue_v2_api import preview, proposed


def stored_rows(db):
    return {cls: {row.id: deepcopy(record(row)) for row in db.scalars(select(cls))}
            for cls in (m.Department, m.Policy, m.PolicySeedLine, m.CatalogueConversion, m.DefinitionHistory)}


@pytest.mark.parametrize('seeder', ['runtime', 'migration'])
@pytest.mark.parametrize('state,version', [
    ('empty', 2), ('primitive', 1), ('v1', 1), ('pending-v1', 2), ('v2', 2),
    ('converted', 2), ('rolled-back', 1),
])
def test_seeding_preserves_department_version_and_existing_evidence(h, seeder, state, version):
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Synthetic seed-version department')
        db.add(department)
        db.flush()
        actor = Actor(h.seed['identities']['admin'], department.id, department.organisation_id,
                      'Synthetic seed reviewer', frozenset({'admin'}), None, 'synthetic-session')
        if state != 'empty':
            rule = ({'kind': 'max_duties', 'maximum': 5, 'window': 'week'} if state == 'primitive' else
                    {'kind': 'catalogue', 'id': 'duties_per_week' if state in {'v1', 'pending-v1', 'rolled-back'}
                     else 'people_per_session', 'catalogue_version': 1 if state in {'v1', 'pending-v1', 'rolled-back'} else 2,
                     'lines': []})
            policy = m.Policy(department_id=department.id, name='Existing authored source', kind=rule['kind'],
                definition=PolicyDefinition.model_validate({'rule': rule}).model_dump(mode='json'),
                starts_on=date(2020, 1, 1), active=True, status='proposed' if state == 'pending-v1' else 'active',
                owner='Synthetic owner', source='Authored fixture, not a scoring default',
                created_by=actor.identity_id, approved_by=None if state == 'pending-v1' else h.seed['identities']['approver'])
            db.add(policy)
            history(db, actor, 'policy', policy, 'Original authored evidence')
        if state in {'converted', 'rolled-back'}:
            conversion = m.CatalogueConversion(department_id=department.id,
                status='active' if state == 'converted' else 'retired', proposed_by=actor.identity_id,
                approved_by=h.seed['identities']['approver'], approved_at=m.now(), reason='Existing reviewed conversion',
                input_revision=1, plan_hash='0' * 64, manifest={}, plan={}, proof={})
            db.add(conversion)
            history(db, actor, 'catalogue_conversion', conversion, 'Original conversion evidence')
        db.flush()
        original = stored_rows(db)

        def seed():
            if seeder == 'runtime':
                scoring_defaults.seed(db, department.id)
            else:
                importlib.import_module('migrations.versions.0109_fair_share_dials').backfill(db.connection())
                db.expire_all()
            db.flush()

        seed()
        seeded = stored_rows(db)
        for cls, rows in original.items():
            assert {ident: seeded[cls][ident] for ident in rows} == rows
        defaults = [row for row in seeded[m.Policy].values()
                    if row['department_id'] == department.id and row['source'] == scoring_defaults.SOURCE]
        assert len(defaults) == 1
        assert defaults[0]['definition']['rule']['catalogue_version'] == version
        assert defaults[0]['definition']['rule']['id'] == 'fair_share'
        assert defaults[0]['definition']['rule']['lines'][0]['id'] == scoring_defaults.line_id(department.id, 'workload')
        seed()
        assert stored_rows(db) == seeded

        command = SimpleNamespace(payload={'catalogue_id': 'workload_limit', 'catalogue_version': 2,
            'lines': [], 'reason': 'Real v2 authoring after defaults were seeded'})
        if version == 2:
            proposal = rule_catalogue.propose(db, actor, command)['record']
            assert proposal['status'] == 'proposed'
            assert proposal['definition']['rule']['catalogue_version'] == 2
        else:
            with pytest.raises(Problem) as error:
                rule_catalogue.propose(db, actor, command)
            assert error.value.code == 'DEPARTMENT_CONVERSION_REQUIRED'


@pytest.mark.parametrize('seeder', ['runtime', 'migration'])
def test_maintenance_seeded_v1_catalogue_keeps_legacy_cap_and_admission(h, seeder):
    with transaction(h.app.state.engine, write=True) as db:
        fair = db.scalar(select(m.Policy).where(m.Policy.department_id == h.seed['department_id']))
        fair.definition = {**fair.definition, 'rule': {**fair.definition['rule'],
            'catalogue_version': 1, 'lines': []}}
        original = deepcopy(record(fair))
        assert original['source'] == scoring_defaults.SOURCE
        assert original['created_by'] == original['approved_by']
        assert department_catalogue_version([original], default_actor_id=fair.created_by) == 1
        typ = db.get(m.AssignmentType, h.seed['type'])
        typ.definition = TypeDefinition.model_validate({**typ.definition,
            'lead': {'minimum': 1, 'maximum': 1, 'most_turns': 5}}).model_dump(mode='json')
        db.flush()
        if seeder == 'runtime':
            scoring_defaults.seed(db, h.seed['department_id'])
        else:
            importlib.import_module('migrations.versions.0109_fair_share_dials').backfill(db.connection())
            db.expire_all()
        assert record(fair) == original

    assert h.get('coordinator', '/api/me')['catalogue_version'] == 1
    policies = h.get('coordinator', '/api/policies?status=active')['policies']
    [cap] = [p for p in policies if p['id'] != fair.id]
    assert cap['definition']['rule']['id'] == 'other_counts_per_period'
    assert cap['definition']['rule']['catalogue_version'] == 1
    proposal = plan_v2(policies, department_id=h.seed['department_id'])
    assert proposal['blocked'] == []
    assert {binding['source_policy_id'] for binding in proposal['bindings']} == {fair.id, cap['id']}
    blocked = h.command('coordinator', 'rule.propose', {'catalogue_id': 'people_per_session',
        'catalogue_version': 2, 'lines': [], 'reason': 'Legacy defaults still require reviewed conversion'}, status=409)
    assert blocked['error']['code'] == 'DEPARTMENT_CONVERSION_REQUIRED'


@pytest.mark.parametrize('lead_cap', [False, True])
def test_native_seed_defaults_convert_with_later_legacy_rules_and_undo_exactly(h, lead_cap):
    if lead_cap:
        with transaction(h.app.state.engine, write=True) as db:
            typ = db.get(m.AssignmentType, h.seed['type'])
            typ.definition = TypeDefinition.model_validate({**typ.definition,
                'lead': {'minimum': 1, 'maximum': 1, 'most_turns': 5, 'most_turns_per': 'month'}}).model_dump(mode='json')
            scoring_defaults.seed(db, h.seed['department_id'], types=[typ], workload=False)
    defaults = [p for p in h.get('coordinator', '/api/policies?status=active')['policies']
                if p['source'] == scoring_defaults.SOURCE]
    assert {p['definition']['rule']['catalogue_version'] for p in defaults} == {2}
    assert {p['definition']['rule']['id'] for p in defaults} == (
        {'fair_share', 'workload_limit'} if lead_cap else {'fair_share'})
    source = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    workspace = h.workspace()
    for day in (4, 5):
        start, end = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
        workspace = h.shift(workspace, start=start, end=end, people=[{
            'person_id': h.seed['people'][0], 'start': start, 'end': end, 'team_lead': lead_cap}])['workspace']
    before = h.snapshot(workspace)
    conversion = proposed(h)
    conversion = h.get('auditor', '/api/rules/catalogue/conversions/' + conversion['id'] + '?details=true')['record']
    bindings = {binding['source_policy_id']: binding for binding in conversion['plan']['bindings']}
    assert set(bindings) == {source['id'], *(p['id'] for p in defaults)}
    for default in defaults:
        assert bindings[default['id']]['rule_shape']['catalogue_version'] == 2
    default_ids = {p['id'] for p in defaults}
    seed_pins = [pin for target in conversion['plan']['targets']
                 for line in target['payload']['definition']['rule']['lines']
                 for pin in line.get('source_bindings', {}).values() if pin['policy_id'] in default_ids]
    assert seed_pins and all(pin['catalogue_version'] == 2 for pin in seed_pins)
    active = h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent mixed-source review'},
        conversion['id'], conversion['revision'])['record']
    after = h.snapshot(workspace)
    assert after['fingerprint'] == before['fingerprint']
    # The semantic token preserves identical obligations; lifecycle revisions retain the conversion.
    proof = strict_proof({**before, 'fingerprint': after['fingerprint']}, after)
    assert proof['identical'], proof
    current = h.get('coordinator', '/api/policies?status=active')['policies']
    assert len([p for p in current if p['definition']['rule'].get('id') == 'fair_share']) == 1

    undone = h.command('approver', 'rule.catalogue.undo', {'reason': 'Restore reviewed original representation'},
        active['id'], active['revision'])['record']
    assert undone['status'] == 'retired'
    expected = deepcopy(before)
    restored = h.snapshot(workspace)
    assert restored['fingerprint'] == after['fingerprint']
    expected['fingerprint'] = restored['fingerprint']
    # The draft retains its captured versions. Activation and Undo advance the live versions twice.
    live = {p['id']: p['revision'] for p in h.get('coordinator', '/api/policies?status=active')['policies']}
    assert {key: live[key] for key in bindings} == {
        key: binding['source_revision'] + 2 for key, binding in bindings.items()}
    proof = strict_proof(expected, restored)
    assert proof['identical'], proof


def test_department_conversion_still_rejects_authored_native_v2(h):
    native = h.command('coordinator', 'rule.propose', {'catalogue_id': 'people_per_session',
        'catalogue_version': 2, 'lines': [], 'reason': 'An authored native v2 rule is not a C2 default'})['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent native rule review'}, native['id'], native['revision'])
    h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    plan = preview(h, proof=False)
    assert any(row['rule_id'] == native['id'] and 'already contains catalogue v2' in row['reason']
               for row in plan['blocked'])


@pytest.mark.parametrize('entry', ['fair_share', 'workload_limit'])
def test_native_defaults_require_the_known_maintenance_actor(h, entry):
    [seed] = h.get('coordinator', '/api/policies?status=active')['policies']
    maintenance_id = seed['created_by']
    assert seed['source'] == scoring_defaults.SOURCE and seed['approved_by'] == maintenance_id
    seed['definition'] = PolicyDefinition.model_validate({'rule': {
        'kind': 'catalogue', 'id': entry, 'catalogue_version': 2, 'lines': []}}).model_dump(mode='json')
    authored = deepcopy(seed)
    authored['created_by'] = authored['approved_by'] = h.seed['identities']['admin']
    original = deepcopy([seed, authored])
    # Equal author/approver IDs and a copied source label do not establish maintenance provenance.
    for policy, allowed_actor in ((seed, None), (seed, authored['created_by']),
                                 (authored, None), (authored, maintenance_id)):
        plan = plan_v2([policy], department_id=h.seed['department_id'], default_actor_id=allowed_actor)
        assert [row['rule_id'] for row in plan['blocked']] == [policy['id']]
        assert 'already contains catalogue v2' in plan['blocked'][0]['reason']
        assert plan['bindings'] == []
    trusted = plan_v2([seed], department_id=h.seed['department_id'], default_actor_id=maintenance_id)
    assert trusted['blocked'] == []
    assert [binding['source_policy_id'] for binding in trusted['bindings']] == [seed['id']]
    assert [seed, authored] == original


@pytest.mark.parametrize('seeder', ['runtime', 'migration'])
@pytest.mark.parametrize('envelope', ['off', 'dated', 'rota_only'])
def test_v2_standalone_defaults_keep_catalogue_admission(h, seeder, envelope):
    workspace = h.workspace()
    with transaction(h.app.state.engine, write=True) as db:
        fair = db.scalar(select(m.Policy).where(m.Policy.department_id == h.seed['department_id']))
        fair.definition = {**fair.definition, 'rule': {**fair.definition['rule'], 'lines': [], 'on': envelope != 'off'}}
        if envelope == 'dated':
            fair.starts_on, fair.ends_on = date(2099, 1, 1), date(2099, 2, 1)
        if envelope == 'rota_only':
            fair.definition = {**fair.definition, 'workspace_id': workspace['id']}
        original = deepcopy(record(fair))

        def seed():
            db.flush()
            if seeder == 'runtime':
                scoring_defaults.seed(db, h.seed['department_id'])
            else:
                importlib.import_module('migrations.versions.0109_fair_share_dials').backfill(db.connection())
                db.expire_all()
            db.flush()

        seed()
        defaults = [record(p) for p in db.scalars(select(m.Policy).where(
            m.Policy.department_id == h.seed['department_id'], m.Policy.kind != 'catalogue'))]
        assert len(defaults) == 1
        assert defaults[0]['kind'] == 'balance_goal'
        assert defaults[0]['source'] == scoring_defaults.SOURCE
        assert defaults[0]['created_by'] == defaults[0]['approved_by'] == fair.created_by
        assert record(fair) == original
        # A later seed must still select the v2 cap entry despite the standalone workload default.
        from tests.legacy_lead import version_historical_lead
        typ = db.get(m.AssignmentType, h.seed['type'])
        version_historical_lead(db, typ, {'minimum': 1, 'maximum': 1, 'most_turns': 5},
                                h.seed['identities']['admin'])
        seed()
        cap = db.scalar(select(m.Policy).where(m.Policy.department_id == h.seed['department_id'],
            m.Policy.kind == 'catalogue', m.Policy.id != fair.id))
        assert cap.definition['rule']['id'] == 'workload_limit'
        assert cap.definition['rule']['catalogue_version'] == 2
        seeded = stored_rows(db)
        seed()
        assert stored_rows(db) == seeded

    assert h.get('coordinator', '/api/me')['catalogue_version'] == 2
    for query, version in (('', 1), ('?version=2', 2)):
        assert h.get('coordinator', '/api/rules/catalogue' + query)['catalogue_version'] == version
    proposal = h.command('coordinator', 'rule.propose', {'catalogue_id': 'people_per_session',
        'catalogue_version': 2, 'lines': [], 'reason': 'V2 authoring after standalone defaults'})['record']
    assert proposal['status'] == 'proposed'
    assert proposal['definition']['rule']['catalogue_version'] == 2


@pytest.mark.parametrize('kind', ['balance_goal', 'block_quota'])
def test_standalone_defaults_need_maintenance_provenance_and_survive_conversion(h, kind):
    [default] = h.get('coordinator', '/api/policies?status=active')['policies']
    actor_id = default['created_by']
    rule = ({'kind': 'balance_goal', 'metric': 'workload', 'aim': 'workload_spread'} if kind == 'balance_goal' else
            {'kind': 'block_quota', 'metric': 'lead_turns', 'comparison': 'maximum', 'amount': 5,
             'starts_on': '2000-01-01', 'repeat': True, 'window_alignment': 'calendar_month'})
    default.update(kind=kind, definition=PolicyDefinition.model_validate(
        {'severity': 'advisory', 'rule': rule}).model_dump(mode='json'))
    original = deepcopy(default)
    assert department_catalogue_version([default], default_actor_id=actor_id) == 2
    for changes, allowed_actor in (({}, None), ({}, h.seed['identities']['admin']),
                                  ({'source': 'Copied default'}, actor_id),
                                  ({'created_by': h.seed['identities']['admin']}, actor_id),
                                  ({'approved_by': h.seed['identities']['approver']}, actor_id)):
        assert department_catalogue_version([{**default, **changes}], default_actor_id=allowed_actor) == 1
    legacy = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    assert h.get('coordinator', '/api/me')['catalogue_version'] == 1
    assert h.get('coordinator', '/api/rules/catalogue?version=2')['catalogue_version'] == 2
    policies = [default, legacy]
    assert department_catalogue_version(policies, default_actor_id=actor_id) == 1
    plan = plan_v2(policies, department_id=h.seed['department_id'], default_actor_id=actor_id)
    assert plan['blocked'] == []
    assert [binding['source_policy_id'] for binding in plan['bindings']] == [legacy['id']]
    assert {row['id'] for row in plan['source_manifest']} == {default['id'], legacy['id']}
    converted = after_snapshot({'policies': policies}, plan)
    assert next(p for p in converted['policies'] if p['id'] == default['id']) == original
    assert department_catalogue_version(converted['policies'], default_actor_id=actor_id) == 2
    assert default == original


@pytest.mark.parametrize('legacy', [False, True])
def test_me_reports_department_catalogue_version_with_fair_share_off(h, legacy):
    with transaction(h.app.state.engine, write=True) as db:
        fair = db.scalar(select(m.Policy).where(m.Policy.department_id == h.seed['department_id']))
        fair.definition = {**fair.definition, 'rule': {**fair.definition['rule'], 'on': False, 'lines': []}}
        scoring_defaults.seed(db, h.seed['department_id'])
    if legacy:
        h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    for role in ('coordinator', 'staff'):
        assert h.get(role, '/api/me')['catalogue_version'] == (1 if legacy else 2)


@pytest.mark.parametrize('seeder', ['runtime', 'migration'])
def test_seeders_do_not_trust_an_enabled_maintenance_identity(h, seeder):
    with transaction(h.app.state.engine, write=True) as db:
        default = db.scalar(select(m.Policy).where(m.Policy.department_id == h.seed['department_id']))
        default.kind = 'balance_goal'
        default.definition = PolicyDefinition.model_validate({'severity': 'advisory', 'rule': {
            'kind': 'balance_goal', 'metric': 'workload', 'aim': 'workload_spread'}}).model_dump(mode='json')
        db.get(m.Identity, default.created_by).disabled = False
        db.flush()
        if seeder == 'runtime':
            scoring_defaults.seed(db, h.seed['department_id'])
        else:
            importlib.import_module('migrations.versions.0109_fair_share_dials').backfill(db.connection())
            db.expire_all()
        catalogues = list(db.scalars(select(m.Policy).where(m.Policy.department_id == h.seed['department_id'],
            m.Policy.kind == 'catalogue')))
        assert len(catalogues) == 1
        assert catalogues[0].definition['rule']['catalogue_version'] == 1


@pytest.mark.parametrize('standalone', [False, True])
def test_me_skips_definition_history_without_default_successors(h, standalone):
    if standalone:
        with transaction(h.app.state.engine, write=True) as db:
            fair = db.scalar(select(m.Policy).where(m.Policy.department_id == h.seed['department_id']))
            fair.definition = {**fair.definition, 'rule': {**fair.definition['rule'], 'on': False, 'lines': []}}
            scoring_defaults.seed(db, h.seed['department_id'])
            [default] = list(db.scalars(select(m.Policy).where(
                m.Policy.department_id == h.seed['department_id'], m.Policy.kind != 'catalogue')))
            assert default.created_by == default.approved_by == fair.created_by
    assert h.get('coordinator', '/api/me')['catalogue_version'] == 2
    queries = []

    def query(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT') and 'definition_history' in statement.lower():
            queries.append(statement)

    event.listen(h.app.state.engine, 'before_cursor_execute', query)
    try:
        assert h.get('coordinator', '/api/me')['catalogue_version'] == 2
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', query)
    assert queries == []


@pytest.mark.parametrize('kind', ['balance_goal', 'block_quota'])
@pytest.mark.parametrize('invalid', [None, 'copied_source', 'unapproved_intermediate', 'merged_intermediate'])
def test_converted_primitive_replacement_uses_authenticated_default_lineage(h, kind, invalid):
    rule = ({'kind': 'balance_goal', 'metric': 'workload', 'aim': 'workload_spread'} if kind == 'balance_goal' else
            {'kind': 'block_quota', 'metric': 'lead_turns', 'comparison': 'maximum', 'amount': 5,
             'starts_on': '2000-01-01', 'repeat': True, 'window_alignment': 'calendar_month'})
    with transaction(h.app.state.engine, write=True) as db:
        maintenance_id = db.scalar(select(m.Policy.created_by).where(
            m.Policy.department_id == h.seed['department_id'], m.Policy.source == scoring_defaults.SOURCE))
        actor = SimpleNamespace(identity_id=h.seed['identities']['coordinator'], department_id=h.seed['department_id'])
        definition = PolicyDefinition.model_validate({'rule': rule, 'severity': 'advisory'}).model_dump(mode='json')

        def retained(name, source, author, approver, status, **edges):
            row = m.Policy(department_id=actor.department_id, name=name, kind=kind,
                definition={**deepcopy(definition), **edges}, starts_on=date(2000, 1, 1),
                active=True, status=status, owner='Synthetic owner', source=source,
                created_by=author, approved_by=approver)
            db.add(row)
            history(db, actor, 'policy', row, 'Retained synthetic provenance')
            return row

        if invalid == 'copied_source':
            current = retained('Copied source', scoring_defaults.SOURCE, actor.identity_id,
                h.seed['identities']['approver'], 'active')
        else:
            seed = retained('Authenticated default', scoring_defaults.SOURCE, maintenance_id, maintenance_id, 'retired')
            intermediate = retained('Earlier adjustment', 'Reviewed default adjustment', actor.identity_id,
                None if invalid == 'unapproved_intermediate' else h.seed['identities']['approver'], 'retired',
                replaces_id=seed.id, merged_ids=[seed.id] if invalid == 'merged_intermediate' else [])
            current = retained('Current adjustment', 'Reviewed default adjustment', actor.identity_id,
                h.seed['identities']['approver'], 'active', replaces_id=intermediate.id)
        db.add(m.CatalogueConversion(department_id=actor.department_id, status='active',
            proposed_by=actor.identity_id, approved_by=h.seed['identities']['approver'], approved_at=m.now(),
            reason='Reviewed synthetic conversion', input_revision=1, plan_hash='0' * 64, manifest={}, plan={}, proof={}))
        current_id = current.id

    response = h.command('coordinator', 'policy.create', {
        'name': 'Next default adjustment', 'owner': 'Synthetic owner', 'source': 'Independent default edit proposal',
        'starts_on': '2000-01-01', 'definition': {'rule': rule, 'severity': 'advisory', 'replaces_id': current_id}},
        status=409 if invalid else 200)
    if invalid:
        assert response['error']['code'] == 'USE_CATALOGUE_V2'
    else:
        assert response['record']['status'] == 'proposed'
        assert response['record']['definition']['replaces_id'] == current_id

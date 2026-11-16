"""Activation keeps admission and lifecycle evidence without rebuilding an unused reach preview."""
from datetime import date
from copy import deepcopy

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.serialization import record
from apps.api.services import policies
from apps.api.services.common import history_many
from apps.api.snapshots import domain_snapshot, snapshot_inputs
from packages.contracts.schemas import PolicyDefinition
from packages.domain import quotas
from .test_condition_count_contracts import condition, tree
from .test_backend_scale import measure
from .test_performance_budget import python_budget
from .test_rule_templates import propose


@pytest.fixture(scope='module', params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def activation_measurement(request, scale_harness):
    h = scale_harness(request.param)
    workspace = h.shift(h.workspace())['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        members = list(db.scalars(select(m.Membership).where(m.Membership.department_id == department.id)))
        people = list(dict.fromkeys(row.person_id for row in members))
        added = [m.Person(id=m.uid(), organisation_id=department.organisation_id, display_name=f'Synthetic person {n}')
                 for n in range(144 - len(people))]
        db.add_all(added)
        db.flush()
        db.add_all([m.Membership(department_id=department.id, person_id=person.id,
                                category_id=members[0].category_id, starts_on=date(2020, 1, 1)) for person in added])
        people += [person.id for person in added]
        definition = PolicyDefinition.model_validate({'rule': {'kind': 'max_duties', 'maximum': 7}}).model_dump(mode='json')
        # Historical versions are present in real department reads even though only current rules apply.
        db.add_all([m.Policy(department_id=department.id, name=f'Synthetic historical rule {n}', kind='max_duties',
            definition=definition, status='retired', active=False, starts_on=date(2020, 1, 1),
            owner='Synthetic owner', source='Synthetic history', created_by=h.seed['identities']['coordinator'])
            for n in range(1045)])
    proposal = propose(h, {'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-11-01',
        'metric': 'session_units', 'comparison': 'target', 'amount': 7, 'window_alignment': 'calendar_week'},
        severity='advisory', listed_only=True, per_person=[{'person_id': pid, 'amount': 7} for pid in people[:38]])
    h.client('approver')
    # Reach details are still available at /check, but activation never consumes them.
    matched, scope_has_duty_witness = [], quotas.scope_has_duty_witness
    def checked(*args, **kwargs):
        matched.append(args[2])
        return scope_has_duty_witness(*args, **kwargs)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(policies, 'people_data', lambda *args: pytest.fail('Activation rebuilt an unused reach preview'))
        monkeypatch.setattr(quotas, 'scope_has_duty_witness', checked)
        activated, metrics = measure(h, '38-row activation', lambda: h.command('approver', 'policy.activate',
            {'reason': 'Independent synthetic scale review'}, proposal['id'], proposal['revision']))
    return h, workspace, proposal, people, matched, activated, metrics


def test_38_row_policy_activation_budget_and_history(activation_measurement):
    h, workspace, proposal, people, matched, activated, metrics = activation_measurement
    assert sorted(matched) == sorted(people[:38]), 'Each named line checks its own person, not all 144 people'
    # 107-108 statements on SQLite/PostgreSQL; 120 rejects an extra query per named line or historical row.
    assert sum(metrics['sql'].values()) <= 120, metrics
    row = activated['record']
    assert row['status'] == 'active' and row['revision'] == proposal['revision'] + 1
    assert row['approved_by'] == h.seed['identities']['approver']
    assert activated['impact'][0]['workspace_id'] == workspace['id']
    assert 'quota_report' not in activated['impact'][0]['evaluation']
    assert activated['impact'][0]['findings']
    with transaction(h.app.state.engine) as db:
        states = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == row['id'])
                                .order_by(m.DefinitionHistory.object_revision)))
        assert [state.snapshot['status'] for state in states] == ['proposed', 'active']
        assert states[0].snapshot == proposal
        assert states[1].snapshot == {**row, 'approval_transition': True}


@pytest.mark.cpu_budget
def test_38_row_policy_activation_budget_and_history_cpu_budget(activation_measurement):
    *_, metrics = activation_measurement
    # Native CPU is .14-.16s idle, .48s with eight workers on two CPUs; keep the shared tracing allowance.
    assert metrics['cpu_seconds'] < python_budget(0.8), metrics


@pytest.mark.wall_benchmark
def test_38_row_policy_activation_budget_and_history_wall_benchmark(activation_measurement):
    *_, metrics = activation_measurement
    assert metrics['wall_seconds'] < python_budget(3), metrics


@pytest.mark.parametrize('scope_kind', ['flat', 'nested', 'any', 'none', 'empty', 'membership_dates'])
def test_quota_named_subjects_preserve_the_unfiltered_report(h, monkeypatch, scope_kind):
    from packages.domain import validation
    pids = h.seed['people']
    quota = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'maximum', 'amount': 0})
    data = h.snapshot(h.shift(h.workspace())['workspace'])
    scope = {
        'flat': {'person_ids': pids[:1]},
        'nested': {'conditions': tree(tree(condition('person_ids', pids[:2]), match='any'),
                                      condition('person_ids', pids[:1]))},
        'any': {'conditions': tree(condition('person_ids', pids[:1]), condition('person_ids', pids[1:2]), match='any')},
        'none': {'conditions': tree(condition('person_ids', pids[:1], op='none'))},
        'empty': {'conditions': tree(condition('person_ids', pids[:1]), condition('person_ids', pids[1:2]))},
        'membership_dates': {'person_ids': pids[:1]},
    }[scope_kind]
    next(policy for policy in data['policies'] if policy['id'] == quota['id'])['definition']['scope'] = scope
    if scope_kind == 'membership_dates':
        for row in data['memberships']:
            if row['person_id'] == pids[0]:
                row['ends_on'] = '2026-10-04'
    def observed():
        findings = []
        report = quotas.evaluate_quotas(deepcopy(data), lambda *args, **kwargs: findings.append((args, kwargs)),
                                       validation.ancestor_ids, validation.exception_for)
        return report, findings
    actual = observed()
    monkeypatch.setattr(validation, 'named_people', lambda scope: set(validation.person_scope_ids(scope)) or None)
    assert actual == observed(), 'The prefilter must preserve every quota row and finding'
    members = {row['person_id'] for row in data['memberships']}
    expected = {'flat': {pids[0]}, 'nested': {pids[0]}, 'any': set(pids[:2]), 'none': members - {pids[0]},
                'empty': set(), 'membership_dates': set()}[scope_kind]
    assert {row['person_id'] for row in actual[0]['rows']} == expected


def test_activation_still_rechecks_resolved_tag_budget(h, monkeypatch):
    tag = h.configure('tags', {'name': 'Activation tag budget', 'labels': ['services']})['record']
    proposal = propose(h, {'kind': 'max_duties', 'maximum': 6},
                       {'conditions': tree(condition('service_ids', [tag['id']], count='each'))})
    monkeypatch.setattr(policies, 'tag_data', lambda *args: {
        tag['id']: {'name': tag['name'], 'members': {'services': [f'synthetic{n}' for n in range(501)]}}})
    reply = h.command('approver', 'policy.activate', {'reason': 'Independent synthetic review'},
                      proposal['id'], proposal['revision'], status=422)
    assert reply['error']['code'] == 'VALUE_INVALID'
    assert 'at least 501' in str(reply)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Policy, proposal['id'])
        assert row.status == 'proposed' and row.revision == proposal['revision']


@pytest.mark.parametrize('invalid', ['schema', 'reference', 'self_approval'])
def test_activation_without_reach_preserves_admission_checks(h, invalid):
    proposal = propose(h, {'kind': 'max_duties', 'maximum': 6})
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, proposal['id'])
        if invalid == 'schema':
            row.definition = {**row.definition, 'unknown_public_field': True}
        elif invalid == 'reference':
            row.definition = {**row.definition, 'scope': {'person_id': m.uid()}}
        else:
            row.created_by = h.seed['identities']['approver']
    code, status = {'schema': ('SOLVER_RULE_INCOMPATIBLE', 422), 'reference': ('NOT_FOUND', 404),
                    'self_approval': ('SELF_APPROVAL', 403)}[invalid]
    reply = h.command('approver', 'policy.activate', {'reason': 'Independent synthetic review'},
                      proposal['id'], proposal['revision'], status=status)
    assert reply['error']['code'] == code


def test_batch_history_captures_defaults_and_each_lifecycle_without_aliasing(h):
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        rows = [m.Policy(department_id=actor.department_id, name=f'History rule {n}', kind='max_duties',
            definition={'rule': {'kind': 'max_duties', 'maximum': n + 1}}, starts_on=date(2020, 1, 1),
            owner='Synthetic owner', source='Synthetic batch history', created_by=actor.identity_id) for n in range(3)]
        db.add_all(rows)
        history_many(db, actor, [('policy', row, 'Proposed') for row in rows])
        proposed = {row.id: record(row) for row in rows}
        for row in rows:
            row.status, row.active, row.revision = 'retired', False, row.revision + 1
            row.definition = {'rule': {**row.definition['rule'], 'maximum': 10}}
        history_many(db, actor, [('policy', row, 'Declined') for row in rows])
        states = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id.in_(proposed))))
        assert len(states) == 6
        for state in states:
            assert state.actor_id == actor.identity_id and state.object_revision in (1, 2)
            assert state.snapshot['id'] == state.object_id and state.snapshot['created_at']
            if state.object_revision == 1:
                assert state.snapshot == proposed[state.object_id]
            else:
                assert state.snapshot['status'] == 'retired' and state.snapshot['definition']['rule']['maximum'] == 10


def test_shared_snapshot_inputs_preserve_overrides_and_do_not_leak_between_rotas(h):
    first, second = h.workspace(), h.workspace(start='2026-10-11', end='2026-10-18')
    policy = propose(h, {'kind': 'max_duties', 'maximum': 6})
    with transaction(h.app.state.engine) as db:
        org = db.get(m.Organisation, db.get(m.Department, h.seed['department_id']).organisation_id)
        shared = snapshot_inputs(db, org)
        original = deepcopy(shared)
        candidate = {**policy, 'status': 'active'}
        for workspace in (first, second):
            row = db.get(m.Workspace, workspace['id'])
            for overrides in ([candidate], None):
                cached = domain_snapshot(db, row, policy_overrides=overrides, shared_inputs=shared)
                ordinary = domain_snapshot(db, row, policy_overrides=overrides)
                assert cached == ordinary
                cached['people'][h.seed['people'][0]]['tags'].append('only this snapshot')
                cached['policies'][0]['definition']['scope']['tags'].append('only this snapshot')
        assert shared == original


@pytest.mark.parametrize('variant', ['complete', 'multiple_lines', 'missing_context', 'quota_limit', 'invalid_presence', 'target_override'])
def test_quota_admission_matches_full_evaluator_report_and_findings(h, variant):
    from packages.domain.validation import evaluate
    h.policy({'kind': 'restriction'}, {'person_id': h.seed['people'][0]})
    quota = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
        'metric': 'duties', 'comparison': 'target', 'amount': 7, 'prorate_department_days': True,
        'reference_days': 28}, severity='advisory')
    data = h.snapshot(h.shift(h.workspace(), seed_restricted=True)['workspace'])
    if variant == 'multiple_lines':
        policy = next(p for p in data['policies'] if p['id'] == quota['id'])
        a, b = h.seed['services'][:2]
        policy['definition'] = {**policy['definition'], 'listed_only': True, 'per_person': [
            {'person_id': h.seed['people'][0], 'service_ids': services, 'amount': 7}
            for services in ([a], [b], [a, b])]}
    elif variant == 'missing_context':
        data['quota_context']['complete'] = False
    elif variant == 'quota_limit':
        data['quota_context']['limit_exceeded'] = True
    elif variant == 'invalid_presence':
        data['shifts'][0]['participants'][0]['segments'] = [{'start': '2026-10-03T00:00:00+03:00',
                                                            'end': '2026-10-03T01:00:00+03:00'}]
    elif variant == 'target_override':
        data['workspace']['target_overrides'] = [{'policy_id': quota['id'], 'policy_revision': quota['revision'] - 1,
                                                'person_id': h.seed['people'][0], 'amount': 8, 'reason': 'Synthetic stale target'}]
    full, admission = evaluate(data), evaluate(data, quota_only=True)
    assert admission['quota_report'] == full['quota_report']
    assert admission['findings'] == [finding for finding in full['findings']
        if finding['code'] != 'BALANCE_GOAL' and (finding['rule_id'] == quota['id'] or finding['code'].startswith('QUOTA_'))]
    assert 'valid' not in admission and 'error_count' not in admission and admission['scope'] == 'quotas'
    assert admission['quota_report']['complete'] == (variant in ('complete', 'multiple_lines'))
    if variant == 'multiple_lines':
        from packages.domain.validation import finding_observations
        assert len(admission['findings']) == 1
        assert len(list(finding_observations(admission))) == 3


@pytest.mark.parametrize('incomplete', [False, True])
def test_quota_activation_and_full_impact_make_the_same_admission_decision(h, monkeypatch, incomplete):
    h.shift(h.workspace())
    proposal = propose(h, {'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
        'metric': 'duties', 'comparison': 'target', 'amount': 7}, severity='advisory')
    client = h.client('approver')
    original = policies.domain_snapshot
    def snapshot(*args, **kwargs):
        data = original(*args, **kwargs)
        if incomplete:
            data['quota_context']['complete'] = False
        return data
    monkeypatch.setattr(policies, 'domain_snapshot', snapshot)
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'])
        policy = db.get(m.Policy, proposal['id'])
        if incomplete:
            refusals = []
            for details in (True, False):
                with pytest.raises(Problem) as error:
                    policies.policy_impact(db, actor, policy, details=details)
                refusals.append((error.value.code, error.value.status, error.value.details))
            assert refusals[0] == refusals[1] and refusals[0][0] == 'QUOTA_IMPACT_UNRESOLVED'
        else:
            full = policies.policy_impact(db, actor, policy)
            summary = policies.policy_impact(db, actor, policy, details=False)
            assert [row['findings'] for row in summary] == [
                [finding for finding in row['findings'] if finding['code'] != 'BALANCE_GOAL'] for row in full]
            assert summary[0]['evaluation'] == {'scope': 'quotas', 'quota_complete': True}
            assert 'quota_report' in full[0]['evaluation']


def test_mixed_rule_batch_and_detailed_quota_preview_keep_full_evaluation(h, monkeypatch):
    h.shift(h.workspace())
    quota = propose(h, {'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
                       'metric': 'duties', 'comparison': 'target', 'amount': 7}, severity='advisory')
    restriction = propose(h, {'kind': 'restriction'})
    client = h.client('approver')
    original, modes = policies.evaluate, []
    def evaluated(data, **kwargs):
        modes.append(kwargs.get('quota_only', False))
        return original(data, **kwargs)
    monkeypatch.setattr(policies, 'evaluate', evaluated)
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'])
        rows = [db.get(m.Policy, rule['id']) for rule in (quota, restriction)]
        policies.policy_impact(db, actor, rows[0], details=True)
        policies.policy_impact(db, actor, rows, details=False)
    assert modes == [False, False]


def test_quota_activation_still_refuses_incomplete_unrelated_existing_quota(h, monkeypatch):
    older = h.policy({'kind': 'block_quota', 'starts_on': '2026-09-01', 'ends_on': '2026-11-01',
                      'metric': 'duties', 'comparison': 'maximum', 'amount': 100})
    h.shift(h.workspace())
    proposal = propose(h, {'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
        'metric': 'duties', 'comparison': 'target', 'amount': 7}, severity='advisory')
    original = policies.domain_snapshot
    def snapshot(*args, **kwargs):
        data = original(*args, **kwargs)
        data['quota_context']['start'] = '2026-10-01T00:00:00+03:00'
        return data
    monkeypatch.setattr(policies, 'domain_snapshot', snapshot)
    reply = h.command('approver', 'policy.activate', {'reason': 'Independent synthetic review'},
                      proposal['id'], proposal['revision'], status=409)
    assert reply['error']['code'] == 'QUOTA_IMPACT_UNRESOLVED'
    assert {finding['rule_id'] for finding in reply['error']['details']['findings']
            if finding['code'] == 'QUOTA_CONTEXT_MISSING'} == {older['id']}

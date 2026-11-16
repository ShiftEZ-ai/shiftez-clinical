"""Cycle-1 T4 backend failures, on migrated scratch databases only."""
import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import select, func
from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning
from packages.domain import proposal_reconciliation
from packages.domain.rule_effects import observed_effect
from .test_scoped_job_inputs import proposal, h_assignments
from .test_be5_governance import binding_fixture
from .test_be5_rota_adjustments import scoped_payload
from .test_series_reconciliation import template, materialise


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_selected_same_session_subset_saves_once_and_undoes_atomically(h):
    recurring = template(h, days=(0,), definition={'anchor': '2026-10-04',
        'days': [{'weekday': 0, 'start_time': '08:00', 'end_time': '12:00'}],
        'service_ids': [h.seed['services'][0]], 'default_person_ids': [h.seed['people'][0]],
        'usual_assignees_fixed': True})
    w = h.shift(materialise(h, recurring), people=[])['workspace']
    job, frozen, original = proposal(h, w)
    row = next(row for row in frozen['shifts'] if not row['participants'])
    # Real solver results include IDs and complete role defaults. Preserve original indexes.
    original['additions'] = [{'shift_id': row['id'], 'participant': {
        'id': str(uuid4()), 'shift_id': row['id'], 'revision': 1,
        'person_id': person, 'start': row['start'], 'end': row['end'],
        'participation': 'staff', 'overtime': False, 'team_lead': False}}
        for person in h.seed['people'][:3]]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Job, job['id']).result = original
        before_changes = db.scalar(select(func.count()).select_from(m.ChangeSet).where(m.ChangeSet.workspace_id == w['id']))
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'addition_indexes': [1, 2], 'removal_indexes': [], 'reason': 'Selected current-input subset'}, revision=w['revision'])
    assert applied['reconciliation']['addition_indexes'] == [1, 2]
    assert {p['person_id'] for p in h_assignments(h, w)} == set(h.seed['people'][:3])
    assert applied['validation']['fingerprint'] == h.snapshot(w)['fingerprint']
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.ChangeSet).where(m.ChangeSet.workspace_id == w['id'])) == before_changes + 1
        assert db.get(m.Job, job['id']).result['additions'] == original['additions']
    undone = h.lifecycle(applied['workspace'], 'undo')['workspace']
    assert {p['person_id'] for p in h_assignments(h, undone)} == {h.seed['people'][0]}


def test_unknown_retention_does_not_claim_unproved_mandatory_conflicts(h, monkeypatch):
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    _, data, result = proposal(h, w)
    current = copy.deepcopy(data)
    first = result['additions'][0]['participant']['person_id']
    current['availability'].append({'id': str(uuid4()), 'person_id': first, 'kind': 'actual_absence',
        'start': result['additions'][0]['participant']['start'], 'end': result['additions'][0]['participant']['end'],
        'counts_as_unavailable': True})
    monkeypatch.setattr(proposal_reconciliation, 'retain_bundles', lambda *args: ([], False, 'UNKNOWN'))
    checked = proposal_reconciliation.reconcile(current, data, result, [0, 1], [])
    assert checked['status'] == 'unresolved' and checked['solver_status'] == 'UNKNOWN'
    unproved = next(row for row in checked['conflicting_placements'] if row['addition_indexes'] == [1])
    assert unproved['code'] == 'RECONCILIATION_UNRESOLVED' and unproved['findings'] == []
    assert checked['reconciliation_allowance_exhausted']


@pytest.mark.postgres
def test_slow_edit_validation_releases_department_writer_for_unrelated_edit(pg, monkeypatch):
    slow = pg.workspace('Slow edit')
    other = pg.workspace('Unrelated edit')
    pg.client('coordinator')
    entered, release = Event(), Event()
    real = planning.evaluate
    def paused(data, **kwargs):
        if data['workspace']['id'] == slow['id'] and data['shifts']:
            entered.set()
            assert release.wait(60)
        return real(data, **kwargs)
    monkeypatch.setattr(planning, 'evaluate', paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(pg.shift, slow, people=[])
        try:
            assert entered.wait(60)
            # A same-rota check can acquire/commit while the edit's pure calculation is paused.
            check = pool.submit(pg.lifecycle, slow, 'validate')
            assert check.result(timeout=10)['workspace']['revision'] == slow['revision']
            quick = pool.submit(pg.shift, other, people=[])
            assert quick.result(timeout=10)['workspace']['revision'] == other['revision'] + 1
        finally:
            release.set()
        assert pending.result(timeout=60)['workspace']['revision'] == slow['revision'] + 1


def test_forbidden_refusal_is_durable_minimized_and_deduplicated(h):
    key, target = str(uuid4()), str(uuid4())
    secret = 'Do not retain this private payload'
    for _ in range(2):
        refused = h.command('staff', 'workspace.rule_adjust', {'reason': secret, 'token': secret}, target, 1, status=403, key=key)
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'command.refused')))
        assert len(events) == 1
        event = events[0]
        assert event.actor_id == h.seed['identities']['staff'] and event.target_id == target
        assert event.detail['command_kind'] == 'workspace.rule_adjust' and event.detail['error_code'] == 'FORBIDDEN'
        assert secret not in str(event.detail) + event.reason
        assert event.correlation_id
        assert not db.scalar(select(func.count()).select_from(m.CommandReceipt).where(m.CommandReceipt.key == key))


def test_home_uses_strict_review_item_independence_even_with_self_permission(h):
    binding, policy, w = binding_fixture(h)
    actor = h.seed['identities']['coordinator']
    h.command('admin', 'role.grant', {'identity_id': actor, 'role': 'approver', 'starts_on': '2020-01-01', 'reason': 'Review authority'})
    h.command('admin', 'self_approval.grant', {'identity_id': actor, 'actions': ['rota.approve'],
        'starts_on': '2026-10-01', 'reason': 'Small team explicit own review grant'})
    adjusted = h.command('coordinator', 'workspace.rule_adjust', scoped_payload(policy, binding, h.seed['people'][0]), w['id'], w['revision'])
    sent = h.lifecycle(adjusted['workspace'], 'submit')['workspace']
    items = h.detail(sent)['review_items']
    refused = h.command('coordinator', 'workspace.approve', {'reason': 'Explicit own review attempt',
        'decisions': [{'item_id': item['id'], 'kind': 'adjustment', 'accept': True} for item in items]},
        sent['id'], sent['revision'], status=403)
    assert refused['error']['code'] == 'SELF_APPROVAL'
    with transaction(h.app.state.engine) as db:
        refusal = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'command.refused'))
        assert refusal.actor_id == actor and refusal.detail['error_code'] == 'SELF_APPROVAL'
    assert not any(item['key'] == 'reviews' for item in h.get('coordinator', '/api/home')['attention'])
    assert any(item['key'] == 'reviews' for item in h.get('approver', '/api/home')['attention'])


def test_effect_reports_advisory_checks_and_keeps_mandatory_admission_separate():
    findings = [{'code': 'BALANCE_GOAL', 'severity': 'warning', 'rule_id': 'fair-share',
        'person_id': str(i), 'detail': {'actual': 2, 'maximum': 1}} for i in range(27)]
    effect = observed_effect({'findings': findings}, {'findings': []})
    assert effect['clears'] == 27 and effect['effect'] == 'clears' and effect['summary'] != 'No change'
    assert effect['mandatory'] == {'clears': 0, 'adds': 0}
    assert effect['advisory'] == {'clears': 27, 'adds': 0}
    reverse = observed_effect({'findings': []}, {'findings': findings})
    assert reverse['adds'] == 27 and reverse['mandatory']['adds'] == 0


@pytest.mark.parametrize('severity', ['error', 'warning'])
def test_effect_does_not_count_a_smaller_remaining_deficit_as_a_cleared_check(severity):
    finding = {'code': 'COVERAGE_GAP', 'severity': severity, 'rule_id': 'cover',
               'shift_id': 'session', 'required': 3, 'available': 0}
    improved = {**finding, 'available': 1}
    effect = observed_effect({'findings': [finding]}, {'findings': [improved]})
    assert effect['clears'] == effect['adds'] == 0
    assert observed_effect({'findings': [improved]}, {'findings': [finding]})['adds'] == 1


@pytest.mark.postgres
def test_prepared_edit_refuses_relevant_input_drift_without_partial_content(pg, monkeypatch):
    w = pg.workspace()
    pg.client('coordinator')
    entered, release = Event(), Event()
    real = planning.evaluate
    def paused(data, **kwargs):
        if data['workspace']['id'] == w['id']:
            entered.set()
            assert release.wait(60)
        return real(data, **kwargs)
    monkeypatch.setattr(planning, 'evaluate', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(pg.shift, w, people=[], status=409)
        try:
            assert entered.wait(60)
            with transaction(pg.app.state.engine, write=True) as db:
                person = db.get(m.Person, pg.seed['people'][0])
                person.tags = ['Changed relevant scheduling input']
        finally:
            release.set()
        result = future.result(timeout=60)
    assert result['error']['code'] == 'STALE_INPUTS' and result['error']['details']['recoverable']
    assert pg.detail(w)['shifts'] == []
    with transaction(pg.app.state.engine) as db:
        assert db.get(m.Workspace, w['id']).revision == w['revision']
        assert not db.scalar(select(func.count()).select_from(m.ChangeSet).where(m.ChangeSet.workspace_id == w['id']))


@pytest.mark.postgres
def test_forbidden_command_and_its_audit_do_not_wait_for_department_writer(pg):
    pg.client('staff')
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(pg.app.state.engine, write=True):
            future = pool.submit(pg.command, 'staff', 'shift.create', {'reason': 'Forbidden role probe'}, status=403)
            assert future.result(timeout=10)['error']['code'] == 'FORBIDDEN'


def test_adjustment_effect_projects_advisory_and_mandatory_deltas_from_one_owner(h):
    w = h.shift(h.workspace())['workspace']
    policy = h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'week'}, severity='advisory')
    adjustment = {'policy_id': policy['id'], 'mode': 'off', 'reason': 'Review advisory-only effect'}
    response = h.client('coordinator').post('/api/workspaces/' + w['id'] + '/adjustment-effect',
        json={'expected_revision': w['revision'], 'adjustment': adjustment})
    assert response.status_code == 200, response.text
    effect = response.json()
    assert effect['cleared_checks'] > 0 and effect['new_must_checks'] == 0
    assert effect['effect']['advisory']['clears'] == effect['cleared_checks']
    assert effect['effect']['mandatory'] == {'clears': 0, 'adds': 0}
    assert len(effect['fix_list_delta']['cleared']) == 1
    assert not h.detail(w)['workspace']['rule_adjustments']


@pytest.mark.postgres
def test_compiled_cover_cap_clash_retains_arithmetic_in_result(pg):
    from packages.domain.cp_sat import generate
    from .test_rule_lines import line, rule
    typ = pg.configure('assignment_types', {'name': 'Compiled cover cap arithmetic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0}]}})['record']['id']
    w = pg.shift(pg.workspace(), typ=typ, people=[])['workspace']
    data = pg.snapshot(w)
    minimum = line(numbers={'min': {'value': 2}})
    maximum = line(numbers={'max': {'value': 0}},
        settings={'block_quota': {'starts_on': w['starts_on'], 'ends_on': w['ends_on']}})
    data['policies'] = [rule('people_per_session', minimum, department_id=w['department_id']),
                        rule('duties_per_period', maximum, department_id=w['department_id'])]
    data['quota_context'] = {'complete': True, 'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-11T00:00:00+03:00'}
    result = generate(data, 60, deterministic=True, feasibility_only=True)
    assert result['status'] == 'requirements_conflict' and result['conflict']['proven']
    cause, = result['conflict']['causes']
    assert cause['evidence']['complete']
    assert cause['evidence']['required_places'] == 2
    # A reduced sufficient conflict can omit some caps; its certified bound describes that named subset.
    assert 0 <= cause['evidence']['maximum_possible'] < 2
    assert {ref['rule_kind'] for ref in cause['requirements']} == {'minimum_cover', 'block_quota'}
    assert set(cause['evidence']['session_ids']) == set(data['focus_ids'])


def test_subset_mismatch_guard_still_rolls_back_the_whole_batch(h, monkeypatch):
    from apps.api.services import generation_reconciliation
    w = h.shift(h.workspace(), people=[])['workspace']
    job, _, _ = proposal(h, w)
    real = generation_reconciliation.prepare
    def corrupt_assignment_identity(*args, **kwargs):
        plan = real(*args, **kwargs)
        plan['assignment_ids'][0] = str(uuid4())
        return plan
    monkeypatch.setattr(generation_reconciliation, 'prepare', corrupt_assignment_identity)
    result = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'reason': 'Check saved batch invariant'}, revision=w['revision'], status=409)
    assert result['error']['code'] == 'RECONCILIATION_MISMATCH'
    assert not h_assignments(h, w)
    assert h.detail(w)['workspace']['content_revision'] == w['content_revision']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).status == 'succeeded'
        assert db.scalar(select(func.count()).select_from(m.ChangeSet).where(m.ChangeSet.workspace_id == w['id'])) == 1


@pytest.mark.parametrize('role', ['staff', 'credentialer', 'operator', 'auditor'])
def test_refusal_audit_includes_pure_operator_and_other_forbidden_roles(h, role):
    from packages.contracts.schemas import ROLE_NAMES
    assert role in ROLE_NAMES
    h.command(role, 'workspace.rule_adjust', {'reason': 'Forbidden role acceptance'}, str(uuid4()), 1, status=403)
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'command.refused'))
        assert event.actor_id == h.seed['identities'][role]
        assert event.detail['command_kind'] == 'workspace.rule_adjust'

from datetime import timedelta

from apps.api import models as m
import pytest
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services.independence import today
from apps.api.services.policies import activate_policies
from apps.worker.main import process_one
from packages.contracts.schemas import Command
from dataclasses import replace
from apps.api.main import create_app
from sqlalchemy import select


def activate(h, policy, job_id, acknowledge=False):
    client = h.client('approver')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        return activate_policies(db, actor, [Command(kind='policy.activate', target_id=policy['id'],
            expected_revision=policy['revision'], payload={'reason': 'Independent contextual review',
            'check_job_ids': [job_id] if job_id else [], 'acknowledge_unresolved': acknowledge})], require_checks=True)


def current_window(h, days=7):
    with transaction(h.app.state.engine) as db:
        start = today(db, h.seed['department_id'])
    return start, start + timedelta(days=days)


def publication_digest(db, publication_id):
    """Hash the publication, its rota rows, revisions and audit evidence."""
    import hashlib
    import json
    from apps.api.serialization import record
    publication = db.get(m.Publication, publication_id)
    workspace = db.get(m.Workspace, publication.workspace_id)
    shifts = list(db.scalars(select(m.Shift).where(m.Shift.workspace_id == workspace.id).order_by(m.Shift.id)))
    shift_ids = [row.id for row in shifts]
    assignments = list(db.scalars(select(m.Assignment).where(m.Assignment.shift_id.in_(shift_ids)).order_by(m.Assignment.id))) if shift_ids else []
    revisions = list(db.scalars(select(m.DefinitionHistory).where(
        m.DefinitionHistory.object_id.in_({publication.id, workspace.id})).order_by(m.DefinitionHistory.id)))
    audits = list(db.scalars(select(m.AuditEvent).where(
        m.AuditEvent.target_id.in_({publication.id, workspace.id})).order_by(m.AuditEvent.id)))
    payload = {
        'publication': record(publication), 'workspace': record(workspace),
        'shifts': [record(row) for row in shifts], 'assignments': [record(row) for row in assignments],
        'revisions': [record(row) for row in revisions], 'audit': [record(row) for row in audits],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def test_safety_activation_survives_fair_share_read(h):
    """A Fair-share read must not make a complete safety effect check appear incomplete."""
    h.get('approver', '/api/rules/catalogue')
    use_checked_activation(h)
    workspace = h.shift(h.workspace('Fair-share read before safety approval'))['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Safety restriction after Fair-share read', 'owner': 'FM coordinator',
        'source': 'Fair-share read bookkeeping regression', 'starts_on': '2026-01-01',
        'definition': {'rule': {'kind': 'min_rest', 'hours': 12}}})['record']
    job = h.command('approver', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=workspace['revision'])['job']
    assert process_one(h.app.state.engine)
    result = h.get('approver', f"/api/jobs/{job['id']}")['job']['result']
    assert result['comparison_complete'] and result['no_new_problems'], result
    decision = h.command('approver', 'policy.activate', {'reason': 'Approved after Fair-share read',
        'check_job_ids': [job['id']]}, policy['id'], policy['revision'])
    assert decision['record']['status'] == 'active'


def test_rule_check_job_list_matches_detail_checked_rules(h):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Protected clinic day', 'owner': 'FM coordinator', 'source': 'Constructed rule review',
        'starts_on': '2026-01-01', 'definition': {'rule': {'kind': 'restriction', 'weekdays': [0]}}})['record']
    workspace = h.detail(workspace)['workspace']
    job = h.command('coordinator', 'job.create', {
        'workspace_id': workspace['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=workspace['revision'])['job']

    detail = h.get('coordinator', f"/api/jobs/{job['id']}")['job']
    listed = h.get('coordinator', f"/api/jobs?workspace_id={workspace['id']}")['jobs'][0]

    assert listed['checked_rules'] == detail['checked_rules'] == [
        {'policy_id': policy['id'], 'temporary_change': False}]
    assert 'bindings' not in listed  # the list derives checked_rules; the input snapshot is never echoed


def test_context_check_proves_new_conflict_and_what_if_is_nonmutating(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Protected clinic day', 'owner': 'FM coordinator', 'source': 'Constructed rule review',
        'starts_on': '2026-01-01', 'definition': {'rule': {'kind': 'restriction'}}})['record']
    before = h.snapshot(w)
    payload = {'workspace_id': w['id'], 'kind': 'feasibility',
               'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]}
    job = h.command('coordinator', 'job.create', payload, revision=w['revision'])['job']
    process_one(h.app.state.engine)
    checked = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert checked['result']['newly_introduced_contradiction']
    assert checked['result']['status'] == 'requirements_conflict'
    assert not checked['check_stale']
    assert h.get('approver', '/api/jobs/' + job['id'])['job']['id'] == job['id']
    assert h.get('approver', '/api/jobs')['jobs'][0]['kind'] == 'feasibility'
    h.get('staff', '/api/jobs/' + job['id'], status=403)
    with pytest.raises(Problem) as error:
        activate(h, policy, job['id'])
    assert error.value.code == 'RULE_INTRODUCES_CONFLICT'
    assert 'additions' not in checked['result']['checks']['proposed']
    h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
              'reason': 'Check results cannot be applied'}, revision=w['revision'], status=409)
    payload['rule_changes'][0]['definition'] = {'rule': {'kind': 'restriction', 'weekdays': [0]}}
    changed = h.command('coordinator', 'job.create', payload, revision=w['revision'])['job']
    process_one(h.app.state.engine)
    result = h.get('coordinator', '/api/jobs/' + changed['id'])['job']['result']
    assert result['full_feasibility_established']
    assert result['bindings'][0]['temporary_change']
    with pytest.raises(Problem) as error:
        activate(h, policy, changed['id'])
    assert error.value.code == 'RULE_CHECK_REQUIRED'  # A changed definition cannot approve the original.
    assert h.snapshot(w) == before
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, policy['id']).status == 'proposed'
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    assert h.get('coordinator', '/api/jobs/' + changed['id'])['job']['check_stale']


def test_rule_check_can_replace_an_editable_assignment_without_changing_the_rota(h):
    w = h.shift(h.workspace())['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Person protected time', 'owner': 'FM coordinator', 'source': 'Constructed person rule review',
        'starts_on': '2026-01-01', 'definition': {'scope': {'person_id': h.seed['people'][0]},
            'rule': {'kind': 'restriction'}}})['record']
    before = h.detail(w)['shifts']
    job = h.command('approver', 'job.create', {'workspace_id': w['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=w['revision'])['job']
    assert process_one(h.app.state.engine)
    checked = h.get('approver', '/api/jobs/' + job['id'])['job']
    result = checked['result']
    assert result['status'] == 'feasible', result
    assert result['full_feasibility_established']
    assert not result['newly_introduced_contradiction']
    assert not checked['check_stale']
    assert all(check['status'] == 'candidate_feasible' and check['validation']['valid']
               and 'additions' not in check and 'removals' not in check
               for check in result['checks'].values())
    assert h.detail(w)['shifts'] == before
    assert not result['no_new_problems']
    assert any(f['code'] == 'FEWER_ELIGIBLE_CANDIDATES' for f in result['new_or_worse_findings'])
    with pytest.raises(Problem) as error:
        activate(h, policy, job['id'])
    assert error.value.code == 'RULE_CHECK_UNRESOLVED'
    decision = activate(h, policy, job['id'], acknowledge=True)
    assert decision['records'][0]['status'] == 'active'
    assert h.detail(w)['shifts'] == before


def test_published_rule_check_reports_exact_protected_rest_gap(h, monkeypatch):
    from packages.domain import feasibility
    evaluations = []
    evaluate = feasibility.evaluate
    def observed(data):
        evaluations.append(data)
        return evaluate(data)
    monkeypatch.setattr(feasibility, 'evaluate', observed)
    w = h.workspace('Published rest evidence')
    w = h.shift(w, start='2026-10-04T16:00:00+03:00', end='2026-10-05T00:00:00+03:00')['workspace']
    w = h.shift(w, start='2026-10-05T16:00:00+03:00', end='2026-10-05T20:00:00+03:00')['workspace']
    published = h.publish(w)['workspace']
    proposed = {'name': '24-hour rest review', 'owner': 'FM coordinator',
                'source': 'Constructed rest review', 'starts_on': '2026-10-04',
                'definition': {'rule': {'kind': 'min_rest', 'hours': 24}}}
    job = h.command('coordinator', 'job.create', {'workspace_id': published['id'],
        'kind': 'feasibility', 'proposed_rule': proposed}, revision=published['revision'])['job']
    process_one(h.app.state.engine)
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert len(evaluations) == 2, 'Published conflict evidence reuses the proposed phase check'
    assert result['newly_introduced_contradiction']
    previous, following = sorted(evaluations[-1]['shifts'], key=lambda shift: shift['start'])
    assert [(f['code'], f['person_id'], f['detail']) for f in result['protected_assignment_conflicts']] == [
        ('MIN_REST', h.seed['people'][0], {'required_hours': 24.0, 'actual_hours': 16.0,
         'previous_shift_id': previous['id'], 'next_shift_id': following['id'],
         'rule_severity': 'mandatory', 'rule_strength': 'medium'})]


def test_published_rule_activation_keeps_issued_evidence_immutable(h):
    start, end = current_window(h)
    w = h.workspace('Published rule activation', str(start), str(end))
    w = h.shift(w, start=f'{start}T16:00:00+03:00', end=f'{start + timedelta(days=1)}T00:00:00+03:00')['workspace']
    w = h.shift(w, start=f'{start + timedelta(days=1)}T16:00:00+03:00',
                end=f'{start + timedelta(days=1)}T20:00:00+03:00')['workspace']
    published = h.publish(w)
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Published minimum rest', 'owner': 'coordinator', 'source': 'Published rule review',
        'starts_on': str(start), 'definition': {'rule': {'kind': 'min_rest', 'hours': 24}}})['record']
    with transaction(h.app.state.engine) as db:
        issued = publication_digest(db, published['publication_id'])
    job = h.command('approver', 'job.create', {'workspace_id': published['workspace']['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=published['workspace']['revision'])['job']
    assert process_one(h.app.state.engine)
    result = h.get('approver', f"/api/jobs/{job['id']}")['job']['result']
    assert result['newly_introduced_contradiction']
    assert any(f['code'] == 'MIN_REST' for f in result['new_or_worse_findings'])
    review = h.get('approver', f"/api/policies/{policy['id']}/review")
    assert review['effect'][0]['job_id'] == job['id']
    assert review['effect'][0]['activation_blocker'] == 'RULE_INTRODUCES_CONFLICT' and not review['ready']
    with transaction(h.app.state.engine) as db:
        assert publication_digest(db, published['publication_id']) == issued
    with pytest.raises(Problem) as error:
        activate(h, policy, job['id'], acknowledge=True)
    assert error.value.code == 'RULE_INTRODUCES_CONFLICT'
    with transaction(h.app.state.engine) as db:
        assert publication_digest(db, published['publication_id']) == issued


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_unarchived_amendment_allows_published_safety_check_and_archive_keeps_impact(request, database):
    h = request.getfixturevalue(database)
    start, end = current_window(h)
    w = h.shift(h.workspace('Published amendment base', str(start), str(end)),
                start=f'{start}T08:00:00+03:00', end=f'{start}T12:00:00+03:00')['workspace']
    published = h.publish(w)
    shift = h.get('coordinator', f"/api/publications/{published['publication_id']}")['snapshot']['shifts'][0]
    amendment = h.command('coordinator', 'publication.change', {
        'kind': 'replace', 'shift_id': shift['id'], 'assignment_id': shift['participants'][0]['id'],
        'person_id': h.seed['people'][1], 'reason': 'Keep the published amendment open during rule review'},
        target=published['publication_id'])['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Published impact before archive', 'owner': 'coordinator', 'source': 'Unarchived amendment review',
        'starts_on': str(start), 'definition': {'rule': {'kind': 'min_rest', 'hours': 12}}})['record']
    job = h.command('approver', 'job.create', {'workspace_id': published['workspace']['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=published['workspace']['revision'])['job']
    assert process_one(h.app.state.engine)
    checked = h.get('approver', f"/api/jobs/{job['id']}")['job']['result']
    assert checked['comparison_complete'] and checked['new_or_worse_findings'] == []
    decision = activate(h, policy, job['id'])
    assert decision['records'][0]['status'] == 'active'
    assert any(row['workspace_id'] == published['workspace']['id'] for row in decision['context_check_evidence'])
    h.command('coordinator', 'workspace.archive', {'reason': 'Discard the unsent amendment'},
              amendment['id'], amendment['revision'])
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, h.client('approver').cookies['rota_session'],
                             h.client('approver').cookies['rota_csrf'])
        from apps.api.services.rule_effects import affected_workspaces
        rows = affected_workspaces(db, actor, [db.get(m.Policy, policy['id'])])
        assert [row.id for row in rows] == [published['workspace']['id']]


def test_activation_requires_a_fresh_matching_receipt(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Monday protection', 'owner': 'FM coordinator', 'source': 'Constructed rule review',
        'starts_on': '2026-01-01', 'definition': {'rule': {'kind': 'restriction', 'weekdays': [0]}}})['record']
    with pytest.raises(Problem) as error:
        activate(h, policy, None)
    assert error.value.code == 'RULE_CHECK_REQUIRED'
    payload = {'workspace_id': w['id'], 'kind': 'feasibility',
               'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]}
    job = h.command('coordinator', 'job.create', payload, revision=w['revision'])['job']
    process_one(h.app.state.engine)
    w = h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[])['workspace']
    with pytest.raises(Problem) as error:
        activate(h, policy, job['id'], acknowledge=True)
    assert error.value.code == 'RULE_CHECK_REQUIRED'
    job = h.command('coordinator', 'job.create', payload, revision=w['revision'])['job']
    process_one(h.app.state.engine)
    decision = activate(h, policy, job['id'])
    assert decision['records'][0]['status'] == 'active'
    assert decision['context_checks'] == [job['id']]


def test_unproven_check_is_not_reported_as_feasible(monkeypatch):
    from packages.domain import feasibility
    monkeypatch.setattr(feasibility, 'generate', lambda *args, **kwargs: {'status': 'unknown_time_limit'})
    result = feasibility.check({'baseline': {'workspace': {'status': 'draft'}},
        'proposed': {'workspace': {'status': 'draft'}}, 'check_seconds': 10,
        'bindings': [], 'fingerprint': 'frozen'})
    assert result['status'] == 'inconclusive'
    assert not result['newly_introduced_contradiction']
    assert not result['full_feasibility_established']
    assert not result['conflict_resolved']


def test_model_errors_are_not_reported_as_inconclusive_searches(monkeypatch):
    from packages.domain import feasibility
    monkeypatch.setattr(feasibility, 'generate', lambda *args, **kwargs: {'status': 'model_invalid'})
    result = feasibility.check({'baseline': {'workspace': {'status': 'draft'}},
        'proposed': {'workspace': {'status': 'draft'}}, 'check_seconds': 10,
        'bindings': [], 'fingerprint': 'frozen'})
    assert result['status'] == 'implementation_error'
    assert not result['full_feasibility_established']


@pytest.mark.parametrize('outcome', ['unknown_time_limit', 'requirements_conflict', 'invalid_inputs'])
def test_incomplete_safety_checks_refuse_even_explicit_review_acknowledgement(h, monkeypatch, outcome):
    from packages.domain import feasibility
    w = h.shift(h.workspace(), people=[])['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Reviewed protection', 'owner': 'FM coordinator', 'source': 'Constructed unresolved review',
        'starts_on': '2026-01-01', 'definition': {'rule': {'kind': 'restriction', 'weekdays': [0]}}})['record']
    monkeypatch.setattr(feasibility, 'generate', lambda *args, **kwargs: {'status': outcome, 'issues': [{'code': 'TYPE_STALE', 'shift_id': 'existing'}]})
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=w['revision'])['job']
    process_one(h.app.state.engine)
    with pytest.raises(Problem) as error:
        activate(h, policy, job['id'])
    assert error.value.code == 'RULE_CHECK_UNRESOLVED'
    with pytest.raises(Problem) as error:
        activate(h, policy, job['id'], acknowledge=True)
    assert error.value.code == 'RULE_CHECK_UNRESOLVED'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, policy['id']).status == 'proposed'


@pytest.mark.parametrize('weekdays, feasible', [([6], False), ([0], True)])
def test_unsaved_rule_checks_context_without_creating_policy_or_receipt_authority(h, weekdays, feasible):
    w = h.shift(h.workspace(), people=[])['workspace']
    before = h.snapshot(w)
    policies = h.get('coordinator', '/api/catalogues')['policies']
    proposed = {'name': 'FM protected clinic time', 'owner': 'FM coordinator',
                'source': 'Unsaved departmental trial', 'starts_on': '2026-01-01',
                'definition': {'rule': {'kind': 'restriction', 'weekdays': weekdays}}}
    payload = {'workspace_id': w['id'], 'kind': 'feasibility', 'proposed_rule': proposed}
    job = h.command('coordinator', 'job.create', payload, revision=w['revision'])['job']
    process_one(h.app.state.engine)
    checked = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert checked['result']['full_feasibility_established'] is feasible
    assert checked['result']['newly_introduced_contradiction'] is (not feasible)
    assert not checked['check_stale']
    assert checked['result']['bindings'] == []
    assert h.snapshot(w) == before
    assert h.get('coordinator', '/api/catalogues')['policies'] == policies
    saved = h.command('coordinator', 'policy.create', proposed)['record']
    with pytest.raises(Problem) as error:
        activate(h, saved, job['id'])
    assert error.value.code == 'RULE_CHECK_REQUIRED'
    h.command('coordinator', 'job.create', {**payload, 'kind': 'generation'}, revision=w['revision'], status=422)


@pytest.mark.parametrize('status,issues,expected', [
    ('invalid_inputs', [{'code': 'TYPE_STALE', 'shift_id': 'existing'}], True),
    ('invalid_inputs', [{'code': 'TYPE_STALE', 'shift_id': 'new'}], False),
    ('invalid_inputs', [], False), ('model_invalid', [{'code': 'TYPE_STALE', 'shift_id': 'existing'}], False),
])
def test_only_identical_existing_setup_issues_can_be_acknowledged(status, issues, expected):
    from packages.domain.feasibility import unchanged_input_issues
    assert unchanged_input_issues({
        'baseline': {'status': 'invalid_inputs', 'issues': [{'code': 'TYPE_STALE', 'shift_id': 'existing'}]},
        'proposed': {'status': status, 'issues': issues}}) is expected


def shortage_review(h, *, rule=None, scope=None):
    typ = h.configure('assignment_types', {'name': 'Existing understaffed clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 20, 'maximum': 20}]}})['record']
    w = h.shift(h.workspace('Existing shortage'), typ=typ['id'], people=[])['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Rest between clinic duties', 'owner': 'FM coordinator', 'source': 'Constructed activation regression',
        'starts_on': '2026-01-01', 'definition': {'rule': rule or {'kind': 'min_rest', 'hours': 12},
                                               'scope': scope or {}}})['record']
    job = h.command('approver', 'job.create', {'workspace_id': w['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=w['revision'])['job']
    assert process_one(h.app.state.engine)
    return w, policy, h.get('approver', '/api/jobs/' + job['id'])['job']


def use_checked_activation(h):
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings, engine=h.app.state.engine)


@pytest.mark.parametrize('drop_choice', [False, True])
def test_fixed_assignment_check_activates_only_with_all_choosable_pairs(h, monkeypatch, drop_choice):
    from packages.domain import feasibility

    person = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[{'person_id': person,
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'fixed': True}])['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Rest with protected work', 'owner': 'FM coordinator', 'source': 'Fixed evidence regression',
        'starts_on': '2026-01-01', 'definition': {'rule': {'kind': 'min_rest', 'hours': 1}}})['record']
    use_checked_activation(h)
    before = h.detail(w)['shifts']
    if drop_choice:
        generate = feasibility.generate
        def incomplete(*args, **kwargs):
            result = generate(*args, **kwargs)
            rows = result['candidate_evidence']
            # Omit the same real choice from both phases, defeating an identity-only comparison.
            missing = next(row for row in rows if row['person_id'] != person)
            rows.remove(missing)
            return result
        monkeypatch.setattr(feasibility, 'generate', incomplete)
    job = h.command('approver', 'job.create', {'workspace_id': w['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=w['revision'])['job']
    assert process_one(h.app.state.engine)
    result = h.get('approver', '/api/jobs/' + job['id'])['job']['result']
    decision = h.command('approver', 'policy.activate', {'reason': 'Reviewed fixed work',
        'check_job_ids': [job['id']]}, policy['id'], policy['revision'], status=409 if drop_choice else 200)
    assert result['comparison_complete'] is (not drop_choice)
    if drop_choice:
        assert decision['error']['code'] == 'RULE_CHECK_UNRESOLVED'
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Policy, policy['id']).status == 'proposed'
    else:
        assert decision['record']['status'] == 'active'
        for phase in ('baseline', 'proposed'):
            rows = h.get('approver', f"/api/jobs/{job['id']}/evidence?phase={phase}")['candidates']
            fixed = next(row for row in rows if row['person_id'] == person)
            assert fixed['fixed'] and fixed['fixed_reason'] == 'ASSIGNMENT_FIXED'
            assert fixed['eligible_and_available'] and not fixed['exclusions']
            assert len(rows) == len(h.seed['people'])
    assert h.detail(w)['shifts'] == before


def test_feasibility_worker_prepares_cp_sat_without_shadowing_inactive(h):
    use_checked_activation(h)
    _, _, checked = shortage_review(h)
    assert checked['result']['checks']['baseline']['status'] == 'requirements_conflict'


def test_existing_shortage_activates_without_override_and_records_audit_evidence(h):
    use_checked_activation(h)
    w, policy, checked = shortage_review(h)
    result = checked['result']
    assert result['pre_existing_conflict'] and not result['full_feasibility_established']
    assert result['comparison_complete'] and result['no_new_problems'], result
    assert result['unchanged_existing_count'] > 0
    assert result['new_or_worse_findings'] == []
    before = h.detail(w)['shifts']
    decision = h.command('approver', 'policy.activate', {'reason': 'Reviewed unchanged existing problems',
        'check_job_ids': [checked['id']]}, policy['id'], policy['revision'])
    assert decision['record']['status'] == 'active'
    assert not decision['unresolved_acknowledged']
    evidence = decision['context_check_evidence'][0]
    assert evidence['summary'] == f"no new problems; {result['unchanged_existing_count']} existing unchanged"
    assert evidence['check_job_id'] == checked['id'] and evidence['workspace_id'] == w['id']
    from sqlalchemy import select
    with transaction(h.app.state.engine) as db:
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'policy.activate',
                                                   m.AuditEvent.target_id == policy['id']))
        assert audit.detail['context_check_evidence'] == decision['context_check_evidence']
        assert db.get(m.Policy, policy['id']).status == 'active'
    assert h.detail(w)['shifts'] == before


@pytest.mark.parametrize('safety', [False, True])
def test_incomplete_baseline_does_not_claim_partial_differences_are_rule_additions_in_audit(h, monkeypatch, safety):
    import copy
    from sqlalchemy import select
    from packages.domain import feasibility

    use_checked_activation(h)
    _, policy, checked = shortage_review(h, rule=None if safety else {'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    baseline = copy.deepcopy(checked['result']['checks']['baseline'])
    baseline.update(status='unknown_time_limit', diagnostics=[])
    phases = iter([baseline, copy.deepcopy(baseline), copy.deepcopy(checked['result']['checks']['proposed'])])
    monkeypatch.setattr(feasibility, 'generate', lambda *args, **kwargs: next(phases))
    with transaction(h.app.state.engine, write=True) as db:
        job = db.get(m.Job, checked['id'])
        result = feasibility.check(job.input_snapshot)
        assert result['new_or_worse_findings'] and not result['comparison_complete']
        assert not result['no_new_problems'] and not result['newly_introduced_contradiction']
        job.result = result

    payload = {'reason': 'Baseline comparison remains unresolved', 'check_job_ids': [checked['id']]}
    refused = h.command('approver', 'policy.activate', payload, policy['id'], policy['revision'], status=409)
    assert refused['error']['code'] == 'RULE_CHECK_UNRESOLVED'
    decision = h.command('approver', 'policy.activate', {**payload, 'acknowledge_unresolved': True},
                         policy['id'], policy['revision'], status=409 if safety else 200)
    if safety:
        assert decision['error']['code'] == 'RULE_CHECK_UNRESOLVED'
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Policy, policy['id']).status == 'proposed'
            assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'policy.activate',
                m.AuditEvent.target_id == policy['id'])) is None
        return
    evidence = decision['context_check_evidence'][0]
    assert evidence['summary'] == 'comparison unresolved'
    assert evidence['new_or_worse_count'] > 0 and not evidence['comparison_complete']
    assert decision['unresolved_acknowledged']
    with transaction(h.app.state.engine) as db:
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'policy.activate',
                                                   m.AuditEvent.target_id == policy['id']))
        assert audit.detail['context_check_evidence'] == decision['context_check_evidence']


def test_worsened_shortage_requires_override_and_does_not_waive_safety(h):
    use_checked_activation(h)
    person = h.seed['people'][0]
    w, policy, checked = shortage_review(h, rule={'kind': 'restriction'}, scope={'person_id': person})
    result = checked['result']
    assert result['comparison_complete'] and not result['no_new_problems'], result
    assert any(f['code'] == 'INSUFFICIENT_WHOLE_DUTY_CANDIDATES' and f['change'] == 'worse'
               for f in result['new_or_worse_findings'])
    assert any(f['code'] == 'FEWER_ELIGIBLE_CANDIDATES' for f in result['new_or_worse_findings'])
    payload = {'reason': 'Reviewed added staffing problem', 'check_job_ids': [checked['id']]}
    refused = h.command('approver', 'policy.activate', payload, policy['id'], policy['revision'], status=409)
    assert refused['error']['code'] == 'RULE_CHECK_UNRESOLVED'
    decision = h.command('approver', 'policy.activate', {**payload, 'acknowledge_unresolved': True},
                         policy['id'], policy['revision'])
    assert decision['unresolved_acknowledged'] and decision['record']['status'] == 'active'
    from packages.domain.validation import evaluate
    data = h.snapshot(w)
    shift = next(s for s in data['shifts'] if s['id'] in data['focus_ids'])
    shift['participants'] = [{'person_id': person, 'start': shift['start'], 'end': shift['end'],
                              'overtime': False, 'supervisor_person_id': None, 'participation': 'full'}]
    assert any(f['rule_id'] == policy['id'] and f['severity'] == 'error' for f in evaluate(data)['findings'])


@pytest.mark.parametrize('invalid', ['failed', 'compiler', 'binding', 'missing_comparison'])
def test_invalid_receipt_cannot_gain_no_new_problems_exemption(h, invalid):
    w, policy, checked = shortage_review(h)
    with transaction(h.app.state.engine, write=True) as db:
        job = db.get(m.Job, checked['id'])
        result = dict(job.result)
        if invalid == 'failed':
            job.status = 'failed'
        elif invalid == 'compiler':
            result['compiler_version'] = 'old-compiler'
        elif invalid == 'binding':
            result['bindings'] = [{**result['bindings'][0], 'checked_hash': 'wrong-definition'}]
        else:
            for field in ('comparison_complete', 'no_new_problems', 'new_or_worse_findings', 'unchanged_existing_count'):
                result.pop(field)
        job.result = result
    with pytest.raises(Problem) as error:
        activate(h, policy, checked['id'])
    assert error.value.code == ('RULE_CHECK_UNRESOLVED' if invalid == 'missing_comparison' else 'RULE_CHECK_REQUIRED')


def test_newly_proven_conflict_is_not_overridden(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'No clinic candidates', 'owner': 'FM coordinator', 'source': 'Constructed safety boundary',
        'starts_on': '2026-01-01', 'definition': {'rule': {'kind': 'restriction'}}})['record']
    job = h.command('approver', 'job.create', {'workspace_id': w['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=w['revision'])['job']
    assert process_one(h.app.state.engine)
    with pytest.raises(Problem) as error:
        activate(h, policy, job['id'], acknowledge=True)
    assert error.value.code == 'RULE_INTRODUCES_CONFLICT'


@pytest.mark.parametrize('context', [
    {'start': '2026-01-01', 'end': '2027-01-01', 'complete': True},
    {'start': '2025-01-01', 'end': '2028-01-01', 'complete': True},
    {'start': '2026-01-01', 'end': '2027-01-01', 'complete': False},
    {'start': '2026-01-01', 'end': '2027-01-01', 'complete': True, 'limit_exceeded': True},
])
@pytest.mark.parametrize('external_data_changed', [False, True])
def test_quota_metadata_and_changed_external_inputs_are_distinguished(h, monkeypatch, context, external_data_changed):
    import copy
    from packages.domain import feasibility
    from packages.domain.candidate_evidence import candidate_evidence
    w = h.shift(h.workspace())['workspace']
    baseline = h.snapshot(w)
    proposed = copy.deepcopy(baseline)
    proposed['quota_context'] = context
    if external_data_changed:
        proposed['availability'].append({'id': 'external-context', 'person_id': h.seed['people'][0],
            'kind': 'recorded_leave', 'start': '2025-01-01T08:00:00+03:00', 'end': '2025-01-01T16:00:00+03:00'})
    def solved(data, *args, **kwargs):
        return {'status': 'candidate_feasible', 'candidate_evidence': [candidate_evidence(data, pid, shift)
            for shift in data['shifts'] if shift['id'] in data['focus_ids'] for pid in data['people']]}
    monkeypatch.setattr(feasibility, 'generate', solved)
    result = feasibility.check({'baseline': baseline, 'proposed': proposed,
                                'check_seconds': 10, 'bindings': [], 'fingerprint': baseline['fingerprint']})
    assert result['full_feasibility_established']
    assert result['new_or_worse_findings'] == []
    complete = context['complete'] and not context.get('limit_exceeded') and not external_data_changed
    assert result['comparison_complete'] is bool(complete)
    assert result['no_new_problems'] is bool(complete)


def test_harmless_block_quota_activates_without_override_when_loaded_inputs_are_identical(h):
    use_checked_activation(h)
    w = h.shift(h.workspace('Harmless block quota'))['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Three duties per block', 'owner': 'FM coordinator', 'source': 'Constructed quota metadata regression',
        'starts_on': '2026-01-01', 'definition': {'rule': {'kind': 'block_quota',
            'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties',
            'comparison': 'maximum', 'amount': 3}}})['record']
    job = h.command('approver', 'job.create', {'workspace_id': w['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=w['revision'])['job']
    with transaction(h.app.state.engine) as db:
        frozen = db.get(m.Job, job['id']).input_snapshot
        assert 'quota_context' not in frozen['baseline'] and frozen['proposed']['quota_context']['complete']
        for collection in ('shifts', 'availability', 'people', 'memberships', 'grants'):
            assert frozen['baseline'][collection] == frozen['proposed'][collection]
    assert process_one(h.app.state.engine)
    result = h.get('approver', '/api/jobs/' + job['id'])['job']['result']
    assert result['comparison_complete'] and result['no_new_problems'], result
    assert result['new_or_worse_findings'] == []
    decision = h.command('approver', 'policy.activate', {'reason': 'No new quota problems',
        'check_job_ids': [job['id']]}, policy['id'], policy['revision'])
    assert decision['record']['status'] == 'active' and not decision['unresolved_acknowledged']

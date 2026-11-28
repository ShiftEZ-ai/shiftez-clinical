"""A catalogue proposal becomes active once another person approves it with the overlapping draft's check as evidence:
propose → check → approve and activate, for a new rule, a revision and a rule proposed Off (the browser journey's API)."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one
from .test_contextual_feasibility import use_checked_activation
from .test_rule_catalogue_api import approve, entry, line, propose


def catalogue_department(h):
    """The catalogue on, with the one approved v1 rule (off, no lines) an unconverted department keeps."""
    department = h.get('admin', '/api/me')['department']
    h.command('admin', 'department.display', {'rule_catalogue': True, 'reason': 'Rule catalogue trial'},
              department['id'], department['revision'])
    approve(h, propose(h, 'weekends_in_a_row', [], on=False)['record'])


def status_of(h, ident):
    live = h.get('approver', '/api/catalogues')['policies'] + h.get('approver', '/api/policies?status=retired')['policies']
    return next(p for p in live if p['id'] == ident)


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('origin', ['new', 'revision', 'off'])
def test_checked_catalogue_proposal_is_active_once_another_person_approves(request, backend, origin):
    h = request.getfixturevalue(backend)
    catalogue_department(h)
    lines = [line(numbers={'max': {'value': 5, 'level': 'mandatory'}})]
    old = approve(h, propose(h, 'duties_per_week', lines)['record']) if origin != 'new' else None
    workspace = h.shift(h.workspace('Catalogue approval context'))['workspace']
    if origin == 'revision':
        lines[0]['numbers']['max']['value'] = 4
    pending = propose(h, 'duties_per_week', lines, on=origin != 'off', policy_id=old and old['id'],
                      revision=old and old['revision'])['record']
    assert pending['status'] == 'proposed'
    use_checked_activation(h)
    h.clients.clear()
    # The proposer cannot activate it, and an approver needs the overlapping draft's check first.
    h.command('coordinator', 'policy.activate', {'reason': 'Proposer approving'}, pending['id'], pending['revision'], status=403)
    refused = h.command('approver', 'policy.activate', {'reason': 'No check yet'}, pending['id'], pending['revision'], status=409)
    assert refused['error']['code'] == 'RULE_CHECK_REQUIRED'
    assert status_of(h, pending['id'])['status'] == 'proposed'
    job = h.command('approver', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': pending['id'], 'expected_revision': pending['revision']}]},
        revision=workspace['revision'])['job']
    assert process_one(h.app.state.engine)
    result = h.get('approver', '/api/jobs/' + job['id'])['job']['result']
    assert result['comparison_complete'] and result['effect']['status'] == 'ready', result.get('effect')
    assert h.get('approver', f"/api/policies/{pending['id']}/review")['ready']
    before = h.detail(workspace)['shifts']
    activated = h.command('approver', 'policy.activate', {'reason': 'Approved as proposed', 'check_job_ids': [job['id']]},
                          pending['id'], pending['revision'])
    assert activated['record']['status'] == 'active' and activated['context_checks'] == [job['id']]
    # Committed and visible to the next read: the live catalogue shows it in force, its predecessor retired.
    current = status_of(h, pending['id'])
    assert current['status'] == 'active' and current['definition']['rule']['on'] == (origin != 'off')
    if old:
        assert status_of(h, old['id'])['status'] == 'retired'
    assert h.detail(workspace)['shifts'] == before


def test_fair_share_proposal_keeps_impact_comparison_complete(h):
    catalogue_department(h)
    historical = h.workspace('Earlier rota', start='2026-09-27', end='2026-10-04')
    historical = h.shift(historical, start='2026-09-30T08:00:00+03:00', end='2026-09-30T12:00:00+03:00')['workspace']
    h.publish(historical)
    workspace = h.shift(h.workspace())['workspace']
    current = entry(h, 'fair_share')['rule']
    existing = current['definition']['rule']['lines']
    pending = propose(h, 'fair_share', [*existing, line(numbers={'apply': {'level': 'medium'}},
        settings={'balance_goal': {'metric': 'duties', 'period': 'rolling', 'period_months': 1}})],
        expected_line_ids=[row['id'] for row in existing])['record']
    use_checked_activation(h)
    h.clients.clear()
    job = h.command('approver', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': pending['id'], 'expected_revision': pending['revision']}]},
        revision=workspace['revision'])['job']
    with transaction(h.app.state.engine) as db:
        snapshot = db.get(m.Job, job['id']).input_snapshot
        assert snapshot['baseline']['fairness_history'] != snapshot['proposed']['fairness_history']

    assert process_one(h.app.state.engine)
    result = h.get('approver', f"/api/jobs/{job['id']}")['job']['result']
    assert result['comparison_complete'] and result['no_new_problems'], result
    review = h.get('approver', f"/api/policies/{pending['id']}/review")
    effect, = review['effect']
    assert effect['job_id'] == job['id'] and effect['comparison_complete']
    assert review['ready'] and not review['requires_acknowledgement']


def rest_line_proposal(h):
    catalogue_department(h)
    workspace = h.shift(h.workspace('Filled rota for rest review'))['workspace']
    workspace = h.shift(workspace, start='2026-10-04T15:00:00+03:00', end='2026-10-04T19:00:00+03:00')['workspace']
    pending = propose(h, 'rest_between_duties', [line(numbers={'min': {'value': 12}})])['record']
    return workspace, pending


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_completed_catalogue_effect_with_new_problems_needs_acknowledgement(request, backend):
    h = request.getfixturevalue(backend)
    workspace, pending = rest_line_proposal(h)
    use_checked_activation(h)
    h.clients.clear()
    job = h.command('approver', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': pending['id'], 'expected_revision': pending['revision']}]},
        revision=workspace['revision'])['job']
    assert process_one(h.app.state.engine)
    checked = h.get('approver', f"/api/jobs/{job['id']}")['job']
    assert not checked['check_stale']
    result = checked['result']
    assert result['comparison_complete'] and result['effect']['status'] == 'ready'
    assert not result['no_new_problems'] and not result['newly_introduced_contradiction']
    assert [problem['code'] for problem in result['new_or_worse_findings']] == ['MIN_REST']

    review = h.get('approver', f"/api/policies/{pending['id']}/review")
    [effect] = review['effect']
    assert effect['job_id'] == job['id'] and effect['status'] == 'ready'
    assert review['can_decide'] and review['requires_acknowledgement']
    assert effect['requires_acknowledgement'] and effect['new_or_worse_count'] == 1

    before = h.detail(workspace)['shifts']
    decision = {'reason': 'Reviewed the rest problem', 'check_job_ids': [job['id']]}
    refused = h.command('approver', 'policy.activate', decision, pending['id'], pending['revision'], status=409)
    assert refused['error']['code'] == 'RULE_CHECK_UNRESOLVED'
    assert refused['error']['message'] == 'Acknowledge the added or worsened rota problems before approving this line'
    assert status_of(h, pending['id'])['status'] == 'proposed'
    activated = h.command('approver', 'policy.activate', {**decision, 'acknowledge_unresolved': True},
                          pending['id'], pending['revision'])
    assert activated['context_checks'] == [job['id']]
    assert status_of(h, pending['id'])['status'] == 'active'
    assert h.detail(workspace)['shifts'] == before

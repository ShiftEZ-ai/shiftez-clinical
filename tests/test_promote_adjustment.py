"""Read-only seeds that turn one rota catalogue adjustment into a department proposal."""

from apps.api import models as m
from apps.api.db import transaction
from .test_rule_catalogue_api import approve, line, propose


def promote(h, policy_id, workspace_id, adjustment_id, status=200):
    response = h.client('coordinator').post(
        f'/api/policies/{policy_id}/promote-adjustment',
        json={'workspace_id': workspace_id, 'adjustment_id': adjustment_id})
    assert response.status_code == status, response.text
    return response.json()


def test_promote_adjustment_returns_a_proposal_seed_without_writing(h):
    policy = approve(h, propose(h, 'duties_per_week', [line(numbers={'max': {'value': 6}})])['record'])
    source_line = policy['definition']['rule']['lines'][0]
    workspace = h.shift(h.workspace())['workspace']
    workspace = h.command('coordinator', 'workspace.rule_adjust', {
        'policy_id': policy['id'], 'line_id': source_line['id'], 'number': 'max',
        'mode': 'set', 'amount': 4, 'reason': 'Short rota cover',
    }, workspace['id'], workspace['revision'])['workspace']
    before = h.detail(workspace)['workspace']['rule_adjustments']
    seed = promote(h, policy['id'], workspace['id'], before[0]['id'])

    assert seed['catalogue_id'] == 'duties_per_week'
    assert seed['expected_policy_id'] == policy['id']
    assert seed['expected_revision'] == policy['revision']
    assert seed['line']['numbers']['max']['value'] == 4
    assert any(row['id'] == seed['line']['id'] for row in seed['lines'])
    assert h.detail(workspace)['workspace']['rule_adjustments'] == before
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, policy['id']).status == 'active'


def test_promote_adjustment_refuses_a_safety_rule_that_would_be_looser(h):
    policy = approve(h, propose(h, 'keep_off', [line(numbers={'apply': {}})])['record'])
    source_line = policy['definition']['rule']['lines'][0]
    workspace = h.shift(h.workspace(), seed_restricted=True)['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace['id'])
        row.rule_adjustments = [{
            'id': '00000000-0000-4000-9000-000000000013', 'policy_id': policy['id'],
            'policy_name': policy['name'], 'policy_revision': policy['revision'],
            'mode': 'set', 'amount': 1, 'line_id': source_line['id'], 'number': 'apply',
            'starts_on': str(row.starts_on), 'ends_on': str(row.ends_on),
            'reason': 'Synthetic unsafe row', 'by': h.seed['identities']['coordinator'],
            'at': '2026-10-04T00:00:00+00:00'}]
    refused = promote(h, policy['id'], workspace['id'], '00000000-0000-4000-9000-000000000013', status=422)
    assert refused['error']['code'] == 'SAFETY_EXCEPTION'


def test_promote_adjustment_requires_the_saved_adjustment(h):
    policy = approve(h, propose(h, 'duties_per_week', [line(numbers={'max': {'value': 6}})])['record'])
    workspace = h.shift(h.workspace())['workspace']
    refused = promote(h, policy['id'], workspace['id'], '00000000-0000-4000-9000-000000000014', status=404)
    assert refused['error']['code'] == 'NOT_FOUND'

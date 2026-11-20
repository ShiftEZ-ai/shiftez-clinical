"""Person profiles retain raw workload totals and the shared prorated quota amounts."""
from copy import deepcopy

import pytest

from apps.api import models as m
from apps.api.db import transaction

from packages.contracts.schemas import PersonFairness, PersonWorkload
from packages.domain.validation import evaluate
from .test_quota_proration_api import partial_quota, rows_for


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('selected_calendar', [True, False])
def test_person_workload_and_fairness_keep_shared_partial_week_amounts(request, backend, selected_calendar):
    h = request.getfixturevalue(backend)
    policy, person, proration = partial_quota(h, selected_calendar)
    rule = policy['definition']['rule']
    target = h.policy({**{key: rule[key] for key in (
        'kind', 'starts_on', 'ends_on', 'window_alignment', 'metric', 'amount')}, 'comparison': 'target'},
        {'person_id': person}, severity='advisory')
    workspace = h.workspace('Partial week workload', '2026-10-01', proration['ends_on'])
    for start, end in [('08:00', '10:00'), ('13:00', '15:00')]:
        workspace = h.shift(workspace, start=f'2026-10-01T{start}:00+03:00',
                            end=f'2026-10-01T{end}:00+03:00', people=[person])['workspace']
    workspace = h.lifecycle(workspace, 'validate')['workspace']
    checked = h.detail(workspace)['workspace']['validation']
    reference = rows_for(checked, policy)
    target_row = next(row for row in checked['quota_report']['rows'] if row['policy_id'] == target['id'])
    assert (reference['minimum']['amount'], reference['maximum']['amount'], target_row['amount']) == (2, 6, 2)

    response = h.get('coordinator', f"/api/people/{person}/workload?workspace_id={workspace['id']}&on=2026-10-01")
    workload = PersonWorkload.model_validate(response)
    rota = next(period for period in workload.periods if period.key == 'this_rota')
    assert rota.complete is True
    # These totals reuse quota attribution without pretending to be a configured bound.
    assert (rota.totals.duties, rota.totals.hours, rota.totals.session_units) == (2, 4, 2)
    rows = {row['comparison']: row for row in rota.quotas if row['policy_id'] == policy['id']}
    columns = ('starts_on', 'ends_on', 'actual', 'amount', 'comparison', 'metric', 'status', 'proration', 'display')
    projection = lambda row: {key: row[key] for key in columns if key in row}
    assert {key: projection(row) for key, row in rows.items()} == {
        key: projection(row) for key, row in reference.items()}
    assert rows['minimum']['proration'] == proration
    assert rows['minimum']['display'] == '2 of 2 (prorated: 1 of 5 days)'
    assert not rows['maximum'].get('proration') and rows['maximum']['display'] == '2 of 6'
    workload_target = next(row for row in rota.quotas if row['policy_id'] == target['id'])
    assert projection(workload_target) == projection(target_row)
    assert workload_target['proration'] == proration

    reach = h.get('coordinator', f"/api/policies/reach?workspace_id={workspace['id']}&policy_id={policy['id']}&details=true")
    counts = {count['comparison']: count for line in reach['details'][policy['id']]['lines']
              for who in line['people'] if who['person_id'] == person for count in who['counts']}
    assert counts == {key: projection(row) for key, row in reference.items()}

    fairness = PersonFairness.model_validate(h.get('coordinator',
        f"/api/people/{person}/fairness?workspace_id={workspace['id']}"))
    [goal] = [goal for goal in fairness.goals if goal.policy_id == target['id'] and goal.quota_target]
    [standing] = goal.people
    assert standing.person_id == person
    assert (standing.actual, standing.expected, standing.status) == (
        workload_target['actual'], workload_target['amount'], 'within')


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_draft_workload_uses_the_checked_target_after_the_department_rule_changes(h):
    person = h.seed['people'][0]
    source = h.policy({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target', 'amount': 16,
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'prorate_department_days': True},
        {'person_id': person}, severity='advisory')
    workspace = h.shift(h.workspace())['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        policy = db.get(m.Policy, source['id'])
        definition = deepcopy(policy.definition)
        definition['rule']['amount'] = 24
        policy.definition = definition
        policy.revision += 1
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    accepted = h.command('coordinator', 'workspace.target_override', {'policy_id': source['id'],
        'person_id': person, 'amount': 32 / 7, 'reason': 'Accept the working-window target'},
        workspace['id'], workspace['revision'])
    checked = next(row for row in accepted['validation']['quota_report']['rows'] if row['policy_id'] == source['id'])
    workload = h.get('coordinator', f"/api/people/{person}/workload?on=2026-10-06&workspace_id={workspace['id']}")
    periods = {period['key']: period for period in workload['periods']}
    draft, = [row for row in periods['this_rota']['quotas'] if row['policy_id'] == source['id']]
    current, = [row for row in periods['this_week']['quotas'] if row['policy_id'] == source['id']]
    assert periods['this_rota']['complete'] and workload['rule_basis'] == 'captured_rota_quotas'
    assert draft['amount'] == checked['amount'] == 4.57 and draft['status'] == checked['status'] != 'unresolved'
    assert draft['policy_revision'] == checked['policy_revision'] == source['revision']
    assert 'override_reason' not in draft['target_calculation']
    assert current['policy_revision'] == source['revision'] + 1 and current['amount'] != 4.57


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize(('state', 'stale'), [('submitted', False), ('approved', False), ('published', False),
                                              pytest.param('submitted', True, id='submitted-mismatch')])
def test_issued_target_override_in_person_workload(h, state, stale):
    person = h.seed['people'][0]
    policy = h.policy({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target', 'amount': 16,
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'prorate_department_days': True},
        {'person_id': person}, severity='advisory')
    workspace = h.shift(h.workspace())['workspace']
    payload = {'policy_id': policy['id'], 'person_id': person, 'amount': 4.57, 'reason': 'Agreed workload'}
    workspace = h.command('coordinator', 'workspace.target_override', payload,
                          workspace['id'], workspace['revision'])['workspace']
    submission = h.lifecycle(workspace, 'submit')
    workspace = submission['workspace']
    if state != 'submitted':
        [item] = submission['review_items']
        workspace = h.command('approver', 'workspace.approve', {'reason': 'Verified the workload target',
            'decisions': [{'item_id': item['id'], 'kind': 'adjustment', 'accept': True}]},
            workspace['id'], workspace['revision'])['workspace']
    if state == 'published':
        h.lifecycle(workspace, 'publish')

    # Model an adjustment issued with an older record format or a different rule fingerprint.
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Workspace, workspace['id'])
        assert stored.status == state
        [adjustment] = stored.target_overrides
        adjustment = dict(adjustment)
        if stale:
            adjustment['rule_fingerprints'] = ['0' * 64]
        else:
            adjustment.pop('rule_fingerprints')
        stored.target_overrides = [adjustment]

    checked = evaluate(h.snapshot(workspace))
    findings = [f for f in checked['findings'] if f['code'] == 'TARGET_OVERRIDE_STALE']
    if stale:
        [finding] = findings
        assert finding['detail']['reconfirm_command'] == 'workspace.target_override'
    else:
        assert not findings
    assert checked['quota_report']['complete'] is (not stale)

    query = '' if state == 'published' else f"&workspace_id={workspace['id']}"
    workload = h.get('coordinator', f"/api/people/{person}/workload?on=2026-10-06{query}")
    assert workload['workspace']['status'] == state
    [rota] = [period for period in workload['periods'] if period['key'] == 'this_rota']
    [row] = [row for row in rota['quotas'] if row['policy_id'] == policy['id']]
    assert row['amount'] == row['target_calculation']['override_amount'] == 4.57
    assert rota['complete'] is (not stale)
    assert (row['status'] == 'unresolved') is stale
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, workspace['id']).target_overrides == [adjustment]

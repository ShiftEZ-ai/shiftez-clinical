"""Person profiles retain raw workload totals and the shared prorated quota amounts."""
import pytest

from packages.contracts.schemas import PersonFairness, PersonWorkload
from .test_quota_proration_api import partial_rota, rows_for


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('selected_calendar', [True, False])
def test_person_workload_and_fairness_keep_shared_partial_week_amounts(request, backend, selected_calendar):
    h = request.getfixturevalue(backend)
    workspace, policy, person, proration = partial_rota(h, selected_calendar)
    for start, end in [('08:00', '10:00'), ('13:00', '15:00')]:
        workspace = h.shift(workspace, start=f'2026-10-01T{start}:00+03:00',
                            end=f'2026-10-01T{end}:00+03:00', people=[person])['workspace']
    rule = policy['definition']['rule']
    target = h.policy({**{key: rule[key] for key in (
        'kind', 'starts_on', 'ends_on', 'window_alignment', 'metric', 'amount')}, 'comparison': 'target'},
        {'person_id': person}, severity='advisory')
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

"""A newly activated mandatory cap binds generation, application and rota review."""
from copy import deepcopy

import pytest

from packages.domain.cp_sat import generate as cp_sat_generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.time import aware
from tests.clock_support import freeze_clock
from tests.test_generation_superseded_apply import apply, group, run
from tests.test_live_zero_limits import overtime_draft
from tests.test_mixed_live_workload_cap import solver_inputs
from tests.test_overtime_extra_cover import place_extra_cover
from tests.test_publish_mandatory import activate_cap, assert_cap_refusal


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_late_mandatory_cap_binds_the_whole_rota_flow(h, monkeypatch):
    freeze_clock(monkeypatch, aware('2026-10-04T00:00:00+03:00'))
    proposal_draft, person = overtime_draft(h)
    target = h.detail(proposal_draft)['shifts'][0]
    duty_type = {'id': target['assignment_type_id']}
    proposal_draft = place_extra_cover(h, proposal_draft, duty_type, person, '2026-10-06')['workspace']
    proposal = run(h, proposal_draft, sessions=[target['id']], population=group(person))
    addition, = proposal['result']['additions']
    assert (addition['shift_id'], addition['participant']['person_id']) == (target['id'], person)
    assert addition['participant']['overtime']

    def over_cap_draft(name):
        draft = h.workspace(name)
        for day in ('2026-10-04', '2026-10-05'):
            draft = place_extra_cover(h, draft, duty_type, person, day)['workspace']
        return draft

    draft = over_cap_draft('Synthetic draft awaiting submission')
    submitted = h.lifecycle(over_cap_draft('Synthetic rota awaiting approval'), 'submit')['workspace']
    ready = h.lifecycle(over_cap_draft('Synthetic rota awaiting publication'), 'submit')['workspace']
    approved = h.lifecycle(ready, 'approve', 'approver')['workspace']

    cap = activate_cap(h, draft, amount=1)
    check = h.lifecycle(draft, 'validate')['validation']
    finding, = [row for row in check['findings'] if row['rule_id'] == cap['id']]
    assert not check['valid']
    assert (finding['code'], finding['person_id'], finding['severity']) == ('QUOTA_ABOVE_MAXIMUM', person, 'error')
    assert (finding['detail']['actual'], finding['detail']['amount']) == (2, 1)

    data = solver_inputs(h, proposal_draft, person)
    for generate in (greedy_generate, cp_sat_generate):
        result = generate(deepcopy(data), 2)
        assert result['validation']['valid']
        assert result['additions'] == result['removals'] == []

    before = h.snapshot(proposal_draft)
    receipt = apply(h, proposal_draft, proposal)['reconciliation']
    assert receipt['status'] == 'unresolved' and receipt['addition_indexes'] == []
    rejected, = [row for placement in receipt['conflicting_placements'] for row in placement['findings']
                 if row['rule_id'] == cap['id']]
    assert (rejected['code'], rejected['person_id'], rejected['severity']) == (
        finding['code'], finding['person_id'], finding['severity'])
    assert (rejected['detail']['actual'], rejected['detail']['amount']) == (2, 1)
    assert h.snapshot(proposal_draft) == before

    assert_cap_refusal(h.lifecycle(draft, 'submit', status=422)['error'], cap, person)
    assert h.lifecycle(submitted, 'approve', 'approver', status=409)['error']['code'] == 'STALE_INPUTS'
    assert_cap_refusal(h.lifecycle(approved, 'publish', status=422)['error'], cap, person)
    assert h.get('coordinator', '/api/publications')['publications'] == []

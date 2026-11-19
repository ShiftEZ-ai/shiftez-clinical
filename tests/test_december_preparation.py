"""Concurrent leaders must not replace duty rows used by workload and quota compilation."""
from collections import Counter
from copy import deepcopy
import json

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.logging_setup import JsonFormatter
from apps.worker.main import process_one
from packages.contracts.schemas import PolicyDefinition
from packages.domain.cp_sat import generate
from packages.domain.validation import evaluate
from tests.scheduler_departments import apply
from tests.test_governed_leadership import line
from tests.test_supervision_balance_integration import paired_department, stable_id
from tests.test_worker_write_timeouts import generation_job

pytestmark = pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)


@pytest.mark.parametrize('limit', ['workload', 'quota'])
def test_concurrent_leaders_with_supervision_fair_share_preserve_duty_limits(h, limit):
    data, goal, supervisors = paired_department(h, groups=2, sessions=4)
    for shift in data['shifts']:
        shift.pop('series_snapshot')
        for participant in shift['participants']:
            participant['fixed'] = False
    data['generation_options']['mode'] = 'rebalance'
    data['quota_context'] = {'complete': True, 'start': '2026-12-01T00:00:00+03:00',
                             'end': '2027-01-01T00:00:00+03:00', 'publications': []}
    settings = goal['definition']['rule']['lines'][0]['settings']['balance_goal']
    settings.update(metric='supervision', share_by='supervisable_sessions', comparison='within_group',
                    period='rolling', period_months=12)
    requirement = line(data['shifts'][0]['assignment_type_id'])
    requirement['who'] = {'conditions': [{'field': 'category_ids', 'values': [h.seed['category']]}]}
    requirement['settings']['team_lead_requirement']['count_scope'] = 'concurrent_duties'
    lead = deepcopy(goal)
    lead['id'] = stable_id('lead')
    lead['definition']['rule'].update(id='team_leads', lines=[requirement])
    duties = deepcopy(goal)
    duties['id'] = stable_id('duties-share')
    duties['definition']['rule']['lines'][0]['settings']['balance_goal'].update(metric='duties', share_by='available_days')

    def policy(name, rule):
        return {**goal, 'id': stable_id(name), 'definition': PolicyDefinition.model_validate({
            'severity': 'mandatory', 'scope': {}, 'rule': rule}).model_dump(mode='json')}

    duty_limit = ({'kind': 'max_duties', 'maximum': 2, 'window': 'week'} if limit == 'workload' else
        {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 2,
         'starts_on': '2026-12-01', 'ends_on': '2027-01-01'})
    data['policies'] = [lead, goal, duties, policy('limit', duty_limit),
        policy('supervision-cap', {'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 1})]
    result = generate(data, 120, workers=1)
    assert result['status'] == 'candidate_feasible', result
    draft = apply(data, result)
    assert evaluate(draft)['valid']
    assert all(len(shift['participants']) == 5 for shift in draft['shifts'])
    counts = Counter(p['person_id'] for shift in draft['shifts'] for p in shift['participants'])
    assert set(counts.values()) == {2}
    for start in {shift['start'] for shift in draft['shifts']}:
        parallel = [shift for shift in draft['shifts'] if shift['start'] == start]
        leaders = [p['person_id'] for shift in parallel for p in shift['participants'] if p.get('team_lead')]
        assert len(leaders) == 1 and leaders[0] in supervisors
        paired = Counter(p['supervisor_person_id'] for shift in parallel for p in shift['participants']
                         if p.get('supervisor_person_id'))
        assert sum(paired.values()) == 2 and max(paired.values()) == 1
    assert {report['metric'] for report in result['validation']['goals']} >= {'supervision', 'duties'}


def test_worker_records_code_location_without_exception_or_rota_data(h, caplog):
    job_id = generation_job(h)
    private = 'PRIVATE rota data, token, and exception reason'

    def fail(cause):
        raise TypeError(private) from cause

    def scheduler(snapshot, **kwargs):
        try:
            raise ValueError(private)
        except ValueError as cause:
            fail(cause)

    process_one(h.app.state.engine, scheduler=scheduler, solver_workers=1)
    record, = [record for record in caplog.records if record.getMessage() == 'job_failed']
    logged = json.loads(JsonFormatter().format(record))
    assert logged['error_code'] == logged['exception_type'] == 'TypeError'
    assert logged['error_location'] == {'module': __name__, 'function': 'fail',
                                      'line': fail.__code__.co_firstlineno + 1}
    assert record.exc_info is None and 'stack' not in logged
    assert private not in json.dumps(logged) and __file__ not in json.dumps(logged)
    with transaction(h.app.state.engine) as db:
        job = db.get(m.Job, job_id)
        assert job.status == 'failed' and job.error_code == 'TypeError' and job.result is None
# Include this file in hosted supervision and team-lead qualification.

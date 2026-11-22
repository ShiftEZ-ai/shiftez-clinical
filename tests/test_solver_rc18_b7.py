"""B7's inherited repairs at department scale and through the durable job/apply API."""
import copy
from dataclasses import replace
from datetime import date, timedelta
import time
import uuid

import pytest

from apps.api.main import create_app
from apps.worker.main import process_one
from packages.domain.cp_sat import generate
from packages.domain.generation import generate as greedy
from packages.domain.validation import evaluate, finding_observations
from tests.scheduler_departments import apply, department
from tests.test_performance_budget import python_budget


@pytest.fixture(scope='module', params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def b7_department(request, scale_harness):
    h = scale_harness(request.param)
    data, _ = department(h, 18, people=150, days=28, rules='none', leave_rate=0, clinics=9)
    intern, *fellows = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Synthetic B7 optional clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [], 'session_units': 1}})['record']
    data['types'][typ['id']] = typ
    weekly = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-18',
        'repeat': True, 'window_alignment': 'fixed', 'window_days': 7,
        'metric': 'session_units', 'comparison': 'minimum', 'amount': 6}, {'person_id': intern})
    sport = h.cover_target(2, {'person_ids': fellows, 'service_id': h.seed['services'][1]})
    missing = h.configure('assignment_types', {'name': 'Synthetic B7 absent service', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    data['types'][missing['id']] = missing
    absent_quota = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': 'duties', 'comparison': 'minimum', 'amount': 1,
        'assignment_type_ids': [missing['id']]}, {'person_id': intern})
    data['policies'] = [weekly, sport]
    data['quota_context'] = h.snapshot(data['workspace'])['quota_context']
    staff = [pid for pid in data['people'] if pid not in h.seed['people'] and
             not any(r['person_id'] == pid for r in data.get('rotations', []))]
    # Keep unrelated clinics staffed and immutable; search sees all 150 people and 532 sessions.
    data['shifts'] = data['shifts'][:-19]
    for index, shift in enumerate(data['shifts']):
        shift.update(assignment_type_id=typ['id'], type_snapshot=copy.deepcopy(typ),
                     service_ids=[h.seed['services'][0]], locked=True,
                     participants=[{'id': str(uuid.uuid4()), 'person_id': staff[index % len(staff)],
                        'start': shift['start'], 'end': shift['end'], 'fixed': True,
                        'overtime': False, 'participation': 'staff'}])
    template = data['shifts'][0]
    selected = []
    for week in (0, 7):
        for day in range(6):
            on = date(2026, 10, 4) + timedelta(days=week + day)
            shift = copy.deepcopy(template)
            shift.update(id=str(uuid.uuid4()), start=f'{on}T08:00:00+03:00',
                         end=f'{on}T12:00:00+03:00', locked=False, participants=[])
            if day < 2:
                shift['participants'] = [{'id': str(uuid.uuid4()), 'person_id': intern,
                    'start': shift['start'], 'end': shift['end'], 'fixed': True,
                    'overtime': False, 'participation': 'staff'}]
            data['shifts'].append(shift)
            selected.append(shift['id'])
    for day in (5, 6, 7, 8, 12, 13, 14):
        shift = copy.deepcopy(template)
        shift.update(id=str(uuid.uuid4()), start=f'2026-10-{day:02}T13:00:00+03:00',
                     end=f'2026-10-{day:02}T16:00:00+03:00', locked=False, participants=[],
                     service_ids=[h.seed['services'][1]])
        data['shifts'].append(shift)
        selected.append(shift['id'])
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    data['generation_session_ids'] = selected
    validation = evaluate(data)
    assert len(data['people']) == 150 and len(data['shifts']) == 532
    assert {f['code'] for f in finding_observations(validation) if f['severity'] == 'error'} == {'QUOTA_BELOW_MINIMUM'}
    rows = validation['quota_report']['rows']
    assert len(rows) == 2 and {(r['actual'], r['amount']) for r in rows} == {(2, 6)}
    gaps = [f for f in finding_observations(validation) if f['code'] == 'SCOPED_COVERAGE_GAP']
    assert len(gaps) == 7 and all(f['detail']['available'] == 0 for f in gaps)
    return data, weekly, sport, absent_quota, intern, fellows


@pytest.mark.parametrize('diagnostic', [False, True])
def test_september_kept_conflict_preserves_empty_quota_at_department_scale(b7_department, diagnostic):
    original, _, _, absent_quota, _, _ = b7_department
    data = copy.deepcopy(original)
    data['policies'] = [absent_quota]
    first, second = data['shifts'][:2]
    assert first['start'] == second['start'] and first['end'] == second['end']
    second['participants'][0]['person_id'] = first['participants'][0]['person_id']
    before = copy.deepcopy(data)
    result = generate(data, 10, deterministic=True, diagnostic_proposal=diagnostic)
    assert data == before
    assert result['status'] == ('candidate_with_gaps' if diagnostic else 'requirements_conflict'), result
    assert not result['additions'] and not result['removals']
    if diagnostic:
        row, = result['allocation_diagnostics']
        assert (row['rule_id'], row['actual'], row['required']) == (absent_quota['id'], 0, 1)
        assert row['message'] in result['warnings']
        assert not result['optimality_proven']
    else:
        assert any(d['proven'] and any(r.get('rule_id') == absent_quota['id']
                   for r in d['requirements']) for d in result['diagnostics'])


@pytest.fixture(scope='module', params=['cp_sat', 'greedy'])
def b7_allocation(b7_department, request):
    data, weekly, sport, _, intern, fellows = b7_department
    before = copy.deepcopy(data)
    began = time.process_time()
    result = generate(data, 10, deterministic=True) if request.param == 'cp_sat' else greedy(data, 60)
    validation = evaluate(apply(data, result))
    cpu = time.process_time() - began
    assert data == before
    return data, weekly, sport, intern, fellows, result, validation, cpu


def test_october_both_generators_meet_reachable_weekly_and_fellow_obligations(b7_allocation):
    data, weekly, sport, intern, fellows, result, validation, _ = b7_allocation
    assert result['status'] == 'candidate_feasible', result
    assert validation['valid'], validation['findings']
    rows = [r for r in validation['quota_report']['rows'] if r['policy_id'] == weekly['id']]
    assert len(rows) == 2 and {(r['actual'], r['amount']) for r in rows} == {(6, 6)}
    assert not any(f['rule_id'] == sport['id'] for f in finding_observations(validation))
    assert sum(a['participant']['person_id'] == intern for a in result['additions']) == 8
    assert {pid: sum(a['participant']['person_id'] == pid for a in result['additions'])
            for pid in fellows} == {pid: 7 for pid in fellows}
    assert len(result['additions']) == 22 and not result['removals']
    assert {a['shift_id'] for a in result['additions']} <= set(data['generation_session_ids'])


@pytest.mark.cpu_budget
def test_b7_department_allocation_cpu_budget(b7_allocation, request):
    *_, cpu = b7_allocation
    request.node.user_properties.append(('b7_allocation_cpu_seconds', cpu))
    assert cpu < python_budget(20), cpu


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def b7_api(request):
    h = request.getfixturevalue(request.param)
    h.app.state.engine.dispose()
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings)
    return h


@pytest.mark.parametrize('diagnostic', [False, True])
def test_september_job_payload_explains_the_rule_and_keeps_apply_guard(b7_api, diagnostic):
    h = b7_api
    person = h.seed['people'][0]
    typ = h.configure('assignment_types', {'name': 'Synthetic absent clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    quota = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': 'duties', 'comparison': 'minimum', 'amount': 1,
        'assignment_type_ids': [typ['id']]}, {'person_id': person})
    booked = {'person_id': person, 'start': '2026-10-04T08:00:00+03:00',
              'end': '2026-10-04T12:00:00+03:00', 'fixed': True}
    workspace = h.shift(h.workspace(), people=[booked])['workspace']
    workspace = h.shift(workspace, location=1, people=[booked])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'],
        'kind': 'generation', 'diagnostic_proposal': diagnostic}, revision=workspace['revision'])['job']
    assert process_one(h.app.state.engine)
    detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    result = detail['result']
    assert detail['status'] == 'succeeded'
    assert detail['outcome'] == 'partial'
    assert result['status'] == 'candidate_with_gaps'
    assert result['diagnostic_proposal'] and not result['validation']['valid']
    row, = result['allocation_diagnostics']
    assert (row['rule_id'], row['actual'], row['required']) == (quota['id'], 0, 1)
    assert row['message'] in result['warnings'] and not row['proven']
    assert bool(result.get('diagnostic_continued')) is (not diagnostic)
    rejected = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job['id'], 'reason': 'Synthetic no-change guard'}, revision=workspace['revision'], status=422)
    assert rejected['error']['code'] == 'NO_ADDITIONS'
    assert sum(len(s['participants']) for s in h.detail(workspace)['shifts']) == 2


def test_october_job_result_applies_reachable_quota_and_fellow_targets_after_reload(b7_api):
    h = b7_api
    intern, *fellows = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Synthetic optional allocation clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [], 'session_units': 1}})['record']
    quota = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': 'session_units', 'comparison': 'minimum', 'amount': 6}, {'person_id': intern})
    h.cover_target(2, {'person_ids': fellows, 'service_id': h.seed['services'][1]})
    workspace = h.workspace()
    for day in range(4, 10):
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00',
            end=f'2026-10-{day:02}T12:00:00+03:00', typ=typ['id'],
            people=[intern] if day < 6 else [])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T13:00:00+03:00', end='2026-10-05T16:00:00+03:00',
                        service=1, typ=typ['id'], people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'],
        'kind': 'generation'}, revision=workspace['revision'])['job']
    assert process_one(h.app.state.engine)
    proposal = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert proposal['status'] == 'candidate_feasible', proposal
    assert len(proposal['additions']) == 6
    h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job['id'], 'reason': 'Apply synthetic B7 proposal'}, revision=workspace['revision'])
    stored = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert stored['status'] == 'applied' and stored['result']['applied']['added_count'] == 6
    validation = evaluate(h.snapshot(h.detail(workspace)['workspace']))
    assert validation['valid'], validation['findings']
    row, = [r for r in validation['quota_report']['rows'] if r['policy_id'] == quota['id']]
    assert (row['actual'], row['amount']) == (6, 6)
    assigned = [p['person_id'] for s in h.detail(workspace)['shifts'] for p in s['participants']]
    assert {pid: assigned.count(pid) for pid in fellows} == {pid: 1 for pid in fellows}

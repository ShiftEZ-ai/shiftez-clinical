"""Department-scale payloads preserve visible results and retain evidence on demand."""
from copy import deepcopy

import pytest
from sqlalchemy import update

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import job_result_summary
from tests.test_backend_scale import measure, scale_department
from tests.test_performance_budget import python_budget


@pytest.mark.postgres
def test_department_fix_list_evidence_is_lazy_and_version_bound(pg):
    workspace, _, _, _ = scale_department(pg, assigned=False)
    pg.command('coordinator', 'workspace.validate', {'reason': 'Synthetic scale check'}, workspace['id'], workspace['revision'])
    path = f'/api/workspaces/{workspace["id"]}/fix-list'
    full = pg.client('coordinator').get(path)
    slim, timing = measure(pg, 'fix-list-lazy', lambda: pg.client('coordinator').get(path+'?view=lean'))
    expected = full.json()
    lean = slim.json()
    assert slim.status_code == 200
    assert {key: lean[key] for key in ('workspace_id', 'revision', 'fingerprint')} == {
        key: expected[key] for key in ('workspace_id', 'revision', 'fingerprint')}
    assert lean['summary'] == {key: expected['summary'].get(key) for key in lean['summary']}
    fields = ('id', 'level', 'sentence', 'shift_ids', 'session_findings', 'person_ids')
    assert [{key: cause[key] for key in fields} for cause in lean['causes']] == [
        {key: cause[key] for key in fields} for cause in expected['causes']]
    assert all('evidence' not in cause for cause in lean['causes'])
    # With 768 open sessions, the lean baseline is 123.6 kB against 1,223.8 kB for the full view.
    assert len(slim.content) < len(full.content) * .2
    assert len(slim.content) < 200_000
    assert timing['sql'].get('SELECT', 0) <= 30
    legacy = pg.client('coordinator').get(path+'?include_evidence=false')
    original = expected['causes'][0]
    without_evidence = {**expected, 'causes': [{**cause, 'evidence': []} for cause in expected['causes']]}
    assert legacy.status_code == 200 and legacy.json() == without_evidence
    url = path+'?cause_id='+original['id']+'&evidence_hash='+original['evidence_hash']
    detail = pg.get('coordinator', url)
    assert detail['causes'] == [original] and detail['summary'] == full.json()['summary']
    assert pg.client('coordinator').get(url.replace(original['evidence_hash'], 'stale')).status_code == 409
    assert pg.client('coordinator').get(path+'?cause_id=missing').status_code == 409
    assert pg.client('staff').get(url).status_code == 403
    detail_url = f"{path}?view=detail&cause_id={original['id']}&revision={lean['revision']}&fingerprint={lean['fingerprint']}"
    assert pg.get('coordinator', detail_url) == {
        'workspace_id': lean['workspace_id'], 'revision': lean['revision'],
        'fingerprint': lean['fingerprint'], 'cause': original}
    assert pg.get('coordinator', detail_url.replace(
        f"revision={lean['revision']}", f"revision={lean['revision'] + 1}"), status=409)['error']['code'] == 'STALE_REVISION'
    assert pg.get('coordinator', detail_url.replace(
        f"fingerprint={lean['fingerprint']}", 'fingerprint=stale'), status=409)['error']['code'] == 'STALE_INPUTS'
    assert pg.client('coordinator').get(path).json() == full.json()
    print('fix-list bytes', {'before': len(full.content), 'after': len(slim.content)})
    return locals()


@pytest.mark.postgres
@pytest.mark.cpu_budget
def test_department_fix_list_evidence_cpu_budget(pg):
    state = test_department_fix_list_evidence_is_lazy_and_version_bound(pg)
    assert state['timing']['cpu_seconds'] < python_budget(1), state['timing']


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_department_fix_list_evidence_wall_budget(pg):
    state = test_department_fix_list_evidence_is_lazy_and_version_bound(pg)
    assert state['timing']['wall_seconds'] < python_budget(3), state['timing']



@pytest.mark.postgres
def test_department_fix_list_opens_lean_and_says_its_bytes(pg):
    """The Fix list opens on view=lean (fix_list.ts readFixList): never evidence, well under the old open read
    (include_evidence=false), its bytes printed against the full object. FIXLIST-UX, 8 Nov 2026, this fixture (1 cause,
    768 open sessions), PERF 10c26e6a2: lean 123.6 KB raw / 37.1 KB gzip; old open 275.3 / 70.0; full 1,223.8 / 104.6.
    What is left is per session (session_findings, shift_ids): LEAD-UIUX designs that separately."""
    import gzip
    workspace, _, _, _ = scale_department(pg, assigned=False)
    pg.command('coordinator', 'workspace.validate', {'reason': 'Synthetic scale check'}, workspace['id'], workspace['revision'])
    path = f'/api/workspaces/{workspace["id"]}/fix-list'
    client = pg.client('coordinator')
    full, before, lean = (client.get(path + q) for q in ('', '?include_evidence=false', '?view=lean'))
    assert lean.status_code == 200 and not any('evidence' in c for c in lean.json()['causes'])
    assert len(lean.content) < len(before.content) * .6 and len(before.content) < len(full.content)
    print('fix-list open bytes', {'full': len(full.content), 'before': len(before.content), 'lean': len(lean.content),
                                  'lean_gzip': len(gzip.compress(lean.content))})

@pytest.mark.postgres
def test_department_job_detail_omits_private_reuse_inputs_in_old_and_new_results(pg):
    workspace, data, additions, _ = scale_department(pg, assigned=False)
    from packages.domain.solver_rules import COMPILER_VERSION
    data.update(generation_engine='cp_sat', compiler_version=COMPILER_VERSION)
    pg.command('coordinator', 'workspace.validate', {'reason': 'Synthetic scale check'}, workspace['id'], workspace['revision'])
    with transaction(pg.app.state.engine, write=True) as db:
        saved = db.get(m.Workspace, workspace['id'])
        raw = {'status': 'candidate_with_gaps', 'validation': deepcopy(saved.validation),
               'additions': additions, 'removals': []}
        job = m.Job(department_id=saved.department_id, workspace_id=saved.id, kind='generation',
            status='succeeded', requested_by=saved.created_by, input_fingerprint=data['fingerprint'],
            input_snapshot=data, result=raw)
        db.add(job); db.flush(); job_id = job.id
        # Existing worker summaries are immutable inputs too; projecting reads must cover pre-upgrade rows.
        db.execute(update(m.Job).where(m.Job.id == job_id).values(result_summary={'result': raw, 'evidence': {}}))
    client = pg.client('coordinator')
    path = '/api/jobs/' + job_id
    full = client.get(path+'?full=true')
    detail, timing = measure(pg, 'job-detail-public', lambda: client.get(path))
    assert detail.status_code == 200 and full.status_code == 200
    actual, expected = detail.json()['job']['result'], job_result_summary(raw)
    for key in ('fix_list_context', 'fix_list_state', 'fix_list_delta', 'input_manifest',
                'score_dependencies', 'diagnostic_version', '_session_parts', '_person_parts'):
        expected['validation'].pop(key, None)
    assert actual == expected
    assert full.json()['job']['result'] == job_result_summary(raw)
    assert len(detail.content) < len(full.content) - 200_000
    assert timing['sql'].get('SELECT', 0) <= 25
    with transaction(pg.app.state.engine) as db:
        saved = db.get(m.Job, job_id)
        assert saved.result == raw and saved.input_snapshot == data
    print('job-detail bytes', {'before': len(full.content), 'after': len(detail.content)})
    return locals()


@pytest.mark.postgres
@pytest.mark.cpu_budget
def test_department_job_detail_cpu_budget(pg):
    state = test_department_job_detail_omits_private_reuse_inputs_in_old_and_new_results(pg)
    assert state['timing']['cpu_seconds'] < python_budget(1), state['timing']


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_department_job_detail_wall_budget(pg):
    state = test_department_job_detail_omits_private_reuse_inputs_in_old_and_new_results(pg)
    assert state['timing']['wall_seconds'] < python_budget(3), state['timing']

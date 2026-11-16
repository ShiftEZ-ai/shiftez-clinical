import copy

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import job_result_summary


def test_job_evidence_is_paginated_and_role_scoped(h):
    workspace = h.workspace()
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'}, revision=workspace['revision'])['job']
    candidates = [{'person_id': str(i), 'shift_id': 'fm-clinic', 'exclusions': []} for i in range(5)]
    raw = {'candidate_evidence': candidates, 'diagnostics': [{'message': 'Staffing shortage', 'candidates': candidates}]}
    original = copy.deepcopy(raw)
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Job, job['id'])
        stored.result = raw
        stored.status = 'succeeded'
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert 'candidate_evidence' not in result and result['candidate_evidence_count'] == 5
    assert 'candidates' not in result['diagnostics'][0]
    assert result['diagnostics'][0]['candidate_count'] == 5
    page = h.get('coordinator', f"/api/jobs/{job['id']}/evidence?offset=2&limit=2")
    assert page['candidates'] == candidates[2:4] and page['has_more'] and page['total'] == 5
    assert h.get('coordinator', f"/api/jobs/{job['id']}/evidence?diagnostic_index=0&offset=4&limit=2")['candidates'] == candidates[4:]
    h.get('staff', f"/api/jobs/{job['id']}/evidence", status=403)
    h.get('approver', f"/api/jobs/{job['id']}/evidence", status=403)
    h.get('coordinator', f"/api/jobs/{job['id']}/evidence?limit=101", status=422)
    assert raw == original
    assert job_result_summary({'checks': {'proposed': raw}})['checks']['proposed']['candidate_evidence_count'] == 5
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Job, job['id'])
        stored.kind = 'feasibility'
        stored.result = {'checks': {'proposed': raw}}
    assert h.get('approver', f"/api/jobs/{job['id']}/evidence?phase=proposed&limit=2")['candidates'] == candidates[:2]
    h.get('approver', f"/api/jobs/{job['id']}/evidence", status=422)
    h.get('staff', f"/api/jobs/{job['id']}/evidence?phase=proposed", status=403)

"""An applied proposal remembers the reviewed subset after a fresh API read."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one
from packages.contracts.schemas import JobResult
from .test_template_scheduler_audit import lead_proposal


def assert_receipt(h, job_id, workspace_id, expected):
    detail = h.get('coordinator', '/api/jobs/' + job_id)['job']
    history = h.get('coordinator', '/api/jobs?workspace_id=' + workspace_id)['jobs']
    listed = next(job for job in history if job['id'] == job_id)
    assert detail['status'] == listed['status'] == 'applied'
    assert detail['result']['applied'] == listed['result']['applied'] == expected
    assert JobResult.model_validate(detail['result']).applied.model_dump() == expected
    assert 'additions' not in listed['result'] and 'removals' not in listed['result']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job_id).result['applied'] == expected
    return detail['result']


@pytest.mark.parametrize('harness_name', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('selection', ['partial', 'all'])
def test_generation_apply_receipt_survives_reload(request, harness_name, selection):
    h = request.getfixturevalue(harness_name)
    w = h.workspace()
    for day in ('04', '05', '06'):
        w = h.shift(w, start=f'2026-10-{day}T08:00:00+03:00',
                    end=f'2026-10-{day}T12:00:00+03:00', people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'},
                    revision=w['revision'])['job']
    process_one(h.app.state.engine)
    proposal = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert len(proposal['additions']) >= 3 and not proposal.get('removals')
    assert JobResult.model_validate(proposal).applied is None
    indexes = [0, 2] if selection == 'partial' else list(range(len(proposal['additions'])))
    payload = {'workspace_id': w['id'], 'job_id': job['id'], 'reason': 'Apply reviewed subset'}
    if selection == 'partial':
        payload.update(addition_indexes=[2, 0, 2], removal_indexes=[])
    h.command('coordinator', 'generation.apply', payload, revision=w['revision'])
    stored = assert_receipt(h, job['id'], w['id'], {
        'addition_indexes': indexes, 'removal_indexes': [], 'added_count': len(indexes), 'removed_count': 0})
    assert stored['additions'] == proposal['additions']
    assigned = [(shift['id'], p['person_id']) for shift in h.detail(w)['shifts'] for p in shift['participants']]
    assert sorted(assigned) == sorted((proposal['additions'][i]['shift_id'],
        proposal['additions'][i]['participant']['person_id']) for i in indexes)


@pytest.mark.parametrize('removal_indexes', [None, [0, 0]])
def test_generation_apply_receipt_counts_replacements_only_after_success(h, removal_indexes):
    w, job, proposal = lead_proposal(h)
    payload = {'workspace_id': w['id'], 'job_id': job['id'], 'addition_indexes': [0, 0],
               'removal_indexes': [], 'reason': 'Apply reviewed replacement'}
    h.command('coordinator', 'generation.apply', payload, revision=w['revision'], status=422)
    assert not h.get('coordinator', '/api/jobs/' + job['id'])['job']['result'].get('applied')
    payload['removal_indexes'] = removal_indexes
    h.command('coordinator', 'generation.apply', payload, revision=w['revision'])
    stored = assert_receipt(h, job['id'], w['id'], {
        'addition_indexes': [0], 'removal_indexes': [0], 'added_count': 1, 'removed_count': 1})
    assert stored['additions'] == proposal['additions'] and stored['removals'] == proposal['removals']


def test_generation_apply_refuses_explicit_empty_duty_selection(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    payload = {'workspace_id': w['id'], 'kind': 'generation'}
    job = h.command('coordinator', 'job.create', payload, revision=w['revision'])['job']
    assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'succeeded' and row.result['additions']
        row.input_snapshot = {**row.input_snapshot, 'generation_session_ids': []}
    before = h.snapshot(w)
    refused = h.command('coordinator', 'generation.apply', {
        'workspace_id': w['id'], 'job_id': job['id'], 'reason': 'Refuse an empty duty selection'},
        revision=w['revision'], status=409)
    assert refused['error']['code'] == 'INVALID_SCOPE'
    assert h.snapshot(w) == before
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'succeeded' and not row.result.get('applied')

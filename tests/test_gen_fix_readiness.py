"""C3-F3: empty readiness and submission share the same prerequisite, even with advisory findings."""
import pytest
from apps.worker.main import process_one
from packages.contracts.schemas import FixList
from packages.domain.rota_summary import submission_prerequisite

pytestmark = pytest.mark.postgres


def advisory(h):
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'minimum', 'amount': 1}, severity='advisory')


@pytest.mark.parametrize('with_advisory', [False, True])
def test_empty_readiness_matches_submit_and_completed_check(pg, with_advisory):
    if with_advisory:
        advisory(pg)
    w = pg.workspace()
    summary = pg.get('coordinator', f'/api/workspaces/{w["id"]}/fix-list')
    FixList.model_validate(summary)
    issue = summary['summary']['submission_issue']
    assert issue == submission_prerequisite(0)
    assert summary['summary']['submit_blocked'] and summary['summary']['score'] is None
    assert pg.detail(w)['workspace']['validation']['score']['gate']['message'] == issue['message']
    if with_advisory:
        assert summary['summary']['could'] > 0
    refused = pg.lifecycle(w, 'submit', status=422)['error']
    assert refused['code'] == issue['code'] and refused['message'] == issue['message']
    assert refused['details']['action'] == issue['action'] == 'add_session'
    job = pg.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'}, revision=w['revision'])['job']
    assert process_one(pg.app.state.engine)
    result = pg.get('coordinator', f'/api/jobs/{job["id"]}')['job']['result']
    assert result['submission_issue'] == issue
    assert result['valid']  # Constraint satisfaction alone must never mean ready to submit.
    added = pg.shift(w)['workspace']
    ready = pg.get('coordinator', f'/api/workspaces/{w["id"]}/fix-list')['summary']
    assert ready['submission_issue'] is None and not ready['submit_blocked']
    assert pg.lifecycle(added, 'submit')['workspace']['status'] == 'submitted'


def test_existing_cancelled_session_keeps_the_existing_submission_policy(pg):
    saved = pg.shift(pg.workspace())
    w = pg.command('coordinator', 'shift.action', {'workspace_id': saved['workspace']['id'],
        'shift_ids': [saved['shift_id']], 'action': 'cancel', 'reason': 'Session no longer needed'},
        revision=saved['workspace']['revision'])['workspace']
    summary = pg.get('coordinator', f'/api/workspaces/{w["id"]}/fix-list')['summary']
    assert summary['submission_issue'] is None and not summary['submit_blocked']
    assert pg.detail(w)['workspace']['validation']['score']['gate']['message'] == 'No active sessions to score'
    assert pg.lifecycle(w, 'submit')['workspace']['status'] == 'submitted'

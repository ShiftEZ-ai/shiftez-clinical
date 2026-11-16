"""Published changes never become whole-rota evidence or a whole-rota score."""
import copy
from datetime import date

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import job_result_summary
from apps.api.services import fairness, planner_tools, planning
from apps.api.snapshots import publication_snapshot
from packages.contracts.schemas import PublishedScheduleSummary
from packages.domain.rota_summary import fix_list, normalized_check, rota_summary
from .test_published_changes import issued, change, drift


def publish_imported_check(h, workspace, check, sequence=1):
    """Insert imported evidence once, retaining the normal immutable-publication guards."""
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace['id'])
        row.validation, row.status = copy.deepcopy(check), 'published'
        publication = m.Publication(workspace_id=row.id, department_id=row.department_id, sequence=sequence,
            starts_on=row.starts_on, ends_on=row.ends_on, current=True,
            published_by=h.seed['identities']['coordinator'], snapshot=publication_snapshot(db, row, check),
            validation=check, fingerprint=check['fingerprint'])
        db.add(publication)
        db.flush()
        db.add_all(m.PublicationPerson(publication_id=publication.id, person_id=pid)
            for pid in {p['person_id'] for shift in publication.snapshot['shifts'] for p in shift['participants']})
        return publication.id


@pytest.mark.parametrize('scope', ['swap', 'published_changes'])
def test_historical_partial_scores_are_hidden_without_rewriting_evidence(h, scope):
    workspace = h.shift(h.workspace())['workspace']
    partial = {**h.detail(workspace)['workspace']['validation'], 'scope': scope}
    assert partial['score']['score'] is not None
    publication_id = publish_imported_check(h, workspace, partial)
    frozen = copy.deepcopy(partial)
    projected = normalized_check(partial)
    assert projected['score'] is None and partial == frozen
    summary = rota_summary(partial)
    assert summary['score'] is None and summary['fix_list']['summary']['score'] is None
    assert fix_list(partial, summary)['summary']['uncapped_score'] is None
    assert job_result_summary({'validation': partial})['validation']['score'] is None
    assert job_result_summary(partial)['score'] is None
    assert h.detail(workspace)['workspace']['validation']['score'] is None
    assert 'schedule_score' not in h.get('coordinator', '/api/reports/' + publication_id)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        assert fairness.schedule(db, row) is None
        assert fairness.score_trend(db, row.department_id, date(2026, 10, 1), date(2026, 11, 1)) == []
        assert planner_tools.score_summary(db, row) is None
        assert row.validation == frozen == db.get(m.Publication, publication_id).validation


@pytest.mark.parametrize('scope', ['swap', 'published_changes'])
def test_partial_checks_cannot_advance_a_whole_rota(h, monkeypatch, scope):
    workspace = h.shift(h.workspace())['workspace']
    revalidate = planning.revalidate
    def partial(*args, **kwargs):
        return {**revalidate(*args, **kwargs), 'scope': scope}
    for action, role in [('submit', 'coordinator'), ('approve', 'approver'), ('publish', 'coordinator')]:
        with monkeypatch.context() as patch:
            patch.setattr(planning, 'revalidate', partial)
            refusal = h.lifecycle(workspace, action, role, status=422)
        assert refusal['error']['code'] == 'PARTIAL_CHECK'
        assert h.detail(workspace)['workspace']['status'] == workspace['status']
        workspace = h.lifecycle(workspace, action, role)['workspace']


@pytest.mark.postgres
def test_amendment_summary_and_lifecycle_keep_d11_scope_and_issue_no_score(pg):
    h = pg
    publication_id, shifts = issued(h)
    frozen = h.get('coordinator', '/api/publications/' + publication_id)['snapshot']
    drift(h)
    workspace = change(h, publication_id, shifts[0])['workspace']
    stored = h.detail(workspace)['workspace']['validation']
    assert stored['scope'] == 'published_changes' and 'score' not in stored
    assert stored['retained_checks']['session_count'] == 1
    summary = h.get('coordinator', f"/api/workspaces/{workspace['id']}/summary")
    assert summary['scope'] == 'published_changes'
    assert summary['score'] is None and summary['issues']['blocking'] == 0
    assert summary['fix_list']['summary']['must'] == 0
    assert h.get('coordinator', f"/api/workspaces/{workspace['id']}/fix-list")['summary']['score'] is None
    assert h.detail(workspace)['workspace']['validation'] == stored
    for action, role in [('submit', 'coordinator'), ('approve', 'approver'), ('publish', 'coordinator')]:
        result = (h.command(role, 'publication.changes.submit', {'reason': 'Review the published-duty change'},
                           target=publication_id, revision=workspace['revision'])
                  if action == 'submit' else h.lifecycle(workspace, action, role))
        workspace = result['workspace']
        check = h.detail(workspace)['workspace']['validation']
        assert check['scope'] == 'published_changes' and 'score' not in check
        assert not any(f['code'] == 'TYPE_VERSION_CHANGED' for f in check['findings'])
    published = h.get('coordinator', '/api/publications/' + result['publication_id'])
    assert published['original_validation']['scope'] == 'published_changes'
    assert 'score' not in published['original_validation']
    assert h.get('coordinator', '/api/publications/' + publication_id)['snapshot'] == frozen


def test_published_window_counts_use_issued_summary_for_multiple_publications(h):
    first_workspace = h.shift(h.workspace('First issued week'))['workspace']
    second_workspace = h.shift(h.workspace('Second issued week', '2026-10-11', '2026-10-18'),
        '2026-10-11T08:00:00+03:00', '2026-10-11T12:00:00+03:00')['workspace']
    frozen = {}
    for sequence, workspace in enumerate((first_workspace, second_workspace), 1):
        detail = h.detail(workspace)
        sid = detail['shifts'][0]['id']
        check = copy.deepcopy(detail['workspace']['validation'])
        check['coverage'] = [dict(check['coverage'][0], status='gap'), dict(check['coverage'][0], status='gap')]
        check['findings'] = [
            {'code': 'SCOPED_COVERAGE_GAP', 'severity': 'error', 'shift_id': sid, 'message': 'Issued safety gap'},
            {'code': 'REST', 'severity': 'warning', 'shift_id': sid, 'message': 'Issued warning'},
            {'code': 'OPEN_EXTRA_COVER', 'severity': 'warning', 'shift_id': sid, 'message': 'Optional overtime',
             'detail': {'required': 2, 'available': 1}}]
        publication_id = publish_imported_check(h, workspace, check, sequence)
        frozen[publication_id] = copy.deepcopy(check)
    first, second = frozen
    path = '/api/schedule/sessions?start=2026-10-04&end=2026-10-18'
    window = h.get('coordinator', path)
    PublishedScheduleSummary.model_validate(window['summary'])
    # must/could: the Fix list's causes over the same checks (SX-13), as the rota's own footer counts them.
    assert window['summary'] == {'sessions_with_gaps': 2, 'blocking': 2, 'advisory': 2, 'extra_cover_sessions': 2, 'must': 1, 'could': 2}
    assert window['summary'] == rota_summary({'coverage': window['coverage'], 'findings': window['findings']})['issued_counts']
    halved = {key: value // 2 for key, value in window['summary'].items() if key not in ('must', 'could')}
    for publication_id in (first, second):
        detail = h.get('coordinator', '/api/publications/' + publication_id)
        # Checks add up across publications; causes do not (one cause may span both weeks): each has its own.
        causes = rota_summary(frozen[publication_id])['fix_list']['summary']
        assert detail['summary'] == {**halved, 'must': causes['must'], 'could': causes['could']}
        assert detail['original_validation'] == frozen[publication_id]
    excluded = h.get('coordinator', path + '&exclude=' + second)
    assert {k: v for k, v in excluded['summary'].items() if k not in ('must', 'could')} == halved
    empty = h.get('coordinator', '/api/schedule/sessions?start=2026-11-01&end=2026-11-02')
    assert not any(empty['summary'].values())
    assert 'summary' not in h.get('staff', path)
    assert 'summary' not in h.get('staff', '/api/publications/' + first)
    client = h.client('coordinator')
    response = client.get(path)
    assert client.get(path, headers={'if-none-match': response.headers['etag']}).status_code == 304

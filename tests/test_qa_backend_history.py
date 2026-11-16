"""Current views agree on historical checks without rewriting issued evidence."""
import copy

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services.issues import issue_key
from packages.domain.rota_summary import normalized_check


@pytest.mark.parametrize('old_fingerprint', [False, True])
def test_historical_leave_check_agrees_in_detail_health_summary_and_resolution(h, old_fingerprint):
    workspace = h.shift(h.workspace())['workspace']
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'recorded_leave',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00', 'private_reason': 'Private'})
    workspace = h.lifecycle(workspace, 'validate')['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Workspace, workspace['id'])
        historical = copy.deepcopy(stored.validation)
        root, = historical['findings']
        gap, = root['detail'].pop('derived_findings')
        root['detail'] = root['detail'] or None
        historical['findings'].append(gap)
        historical['error_count'] = historical['score']['blocking_count'] = 2
        if old_fingerprint:
            historical['fingerprint'] = 'previous-unrelated-revision'
        stored.validation = historical
    before = copy.deepcopy(historical)
    detail = h.detail(workspace)['workspace']
    check = detail['validation']
    assert check['error_count'] == check['score']['blocking_count'] == len(check['findings']) == 1
    assert check['findings'][0]['code'] == 'UNAVAILABLE'
    assert check['findings'][0]['detail']['derived_findings'] == [gap]
    assert check['valid'] is False and check['fingerprint'] == historical['fingerprint']
    summary = h.get('coordinator', f"/api/workspaces/{workspace['id']}/summary")
    listed = next(row for row in h.get('coordinator', '/api/workspaces')['workspaces'] if row['id'] == workspace['id'])
    assert summary['issues']['blocking'] == listed['health']['errors'] == check['error_count']
    if not old_fingerprint:
        blocking = next(row for row in h.get('coordinator', '/api/home')['attention'] if row['key'] == 'blocking')
        assert blocking['count'] == 1
    preview = h.client('coordinator').post('/api/planner/issues/replace-preview', json={
        'workspace_id': workspace['id'], 'expected_revision': workspace['revision'], 'keys': [issue_key(root)]})
    assert preview.status_code == 200, preview.text
    assert preview.json()['replaceable'] == 1
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Workspace, workspace['id'])
        assert stored.validation == before
        assert stored.revision == workspace['revision']
    assert record(m.Publication(validation=historical))['validation'] == before


def test_historical_empty_workspace_projects_no_score_without_changing_its_verdict(h):
    workspace = h.workspace()
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Workspace, workspace['id'])
        historical = copy.deepcopy(stored.validation)
        historical['score'].update(score=100, raw=100, uncapped_score=100, publishable=True)
        stored.validation = historical
    check = h.detail(workspace)['workspace']['validation']
    assert check['score'] is None and check['valid'] == historical['valid']
    assert h.get('coordinator', f"/api/workspaces/{workspace['id']}/summary")['score'] is None
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, workspace['id']).validation == historical


def test_normalized_check_preserves_compact_counts_and_unknown_coverage():
    compact = {'valid': False, 'error_count': 3, 'warning_count': 2}
    assert normalized_check(compact) == compact
    legacy = {'valid': True, 'findings': [], 'score': {'score': 100, 'raw': 100}}
    assert normalized_check(legacy)['score']['score'] == 100
    assert legacy['score'] == {'score': 100, 'raw': 100}
    assert normalized_check({'coverage': [], 'score': {'score': 100}})['score'] is None


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('field,value,label', [('fixed', True, 'Fixed assignment'), ('overtime', True, 'Overtime'),
    ('start', '2026-10-04T09:00:00+03:00', 'Assignment start')])
def test_session_history_tracks_assignment_fields_without_private_provenance(request, database, field, value, label):
    from packages.contracts import schemas as s
    from .test_series_reconciliation import edit
    h = request.getfixturevalue(database)
    w = h.shift(h.workspace())['workspace']
    shift = h.detail(w)['shifts'][0]
    person = shift['participants'][0]
    updated = {k: person[k] for k in s.ParticipantInput.model_fields}
    updated[field] = value
    changes = {'participants': [updated], 'override_reason': 'Synthetic private assignment explanation'}
    if field == 'start':
        updated['end'] = '2026-10-04T13:00:00+03:00'
        changes.update(start=updated['start'], end=updated['end'])
    w = edit(h, w, shift, **changes)
    url = f"/api/workspaces/{w['id']}/shifts/{shift['id']}/history"
    before = h.detail(w)['shifts']
    result = h.get('auditor', url)
    name = next(p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == person['person_id'])
    expected_changes = [f'{label} changed for {name}']
    if field == 'start':
        expected_changes += [f'Assignment end changed for {name}', 'Time changed']
    assert result['entries'][0]['changes'] == expected_changes
    assert result['partial'] is False
    # No assignment snapshot, private explanation or internal source identifiers are added to the public changes.
    assert set(result['entries'][0]) == {'at', 'by', 'reason', 'undone', 'changes'}
    assert 'Synthetic private assignment explanation' not in str(result['entries'][0]['changes'])
    assert h.detail(w)['shifts'] == before
    h.get('staff', url, status=403)
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Restore the assignment'}, w['id'], w['revision'])['workspace']
    assert h.get('auditor', url)['entries'][0]['changes'] == expected_changes
    restored = h.detail(undone)['shifts'][0]
    assert restored['participants'][0][field] == person[field]
    assert (restored['start'], restored['end']) == (shift['start'], shift['end'])
    assert (restored['participants'][0]['start'], restored['participants'][0]['end']) == (person['start'], person['end'])


def test_session_history_ignores_assignment_ids_and_order_but_keeps_the_60_edit_limit(h):
    w = h.shift(h.workspace(), people=h.seed['people'][:2])['workspace']
    shift = h.detail(w)['shifts'][0]
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, w['id'])
        from apps.api.snapshots import workspace_content
        snapshot = workspace_content(db, row)
        old = copy.deepcopy(snapshot)
        old['shifts'][0]['participants'].reverse()
        for participant in old['shifts'][0]['participants']:
            participant['id'] = m.uid()
            participant['revision'] += 1
        db.add_all([m.ChangeSet(workspace_id=w['id'], actor_id=h.seed['identities']['coordinator'],
            before_snapshot=copy.deepcopy(old), resulting_content_revision=row.content_revision,
            reason='Equivalent assignment reconstruction') for _ in range(60)])
    result = h.get('auditor', f"/api/workspaces/{w['id']}/shifts/{shift['id']}/history")
    assert result['entries'] == [] and result['partial'] is True

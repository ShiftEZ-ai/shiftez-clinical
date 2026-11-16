"""Merged read projections preserve published scope and preview dependencies."""
import copy
from datetime import date

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning, published_changes
from packages.domain.rota_summary import rota_summary
from tests.clock_support import freeze_clock
from tests.test_fix_list_performance import backend
from tests.test_published_changes import issued, change
from tests.test_series_reconciliation import template, materialise, amend_template, payload


def test_scoped_summary_cache_tracks_base_publication_window(backend, monkeypatch):
    h = backend
    freeze_clock(monkeypatch)
    publication_id, shifts = issued(h)
    workspace = change(h, publication_id, shifts[0])['workspace']
    with transaction(h.app.state.engine) as db:
        stored = copy.deepcopy(db.get(m.Workspace, workspace['id']).validation)
    checks, revalidate = [], planning.revalidate

    def counted(*args, **kwargs):
        checked = revalidate(*args, **kwargs)
        checks.append(copy.deepcopy(checked))
        return checked

    monkeypatch.setattr(planning, 'revalidate', counted)
    path = f"/api/workspaces/{workspace['id']}/summary"
    first = h.get('coordinator', path)
    assert first == h.get('coordinator', path)
    assert len(checks) == 1
    assert checks[0]['scope'] == 'published_changes'
    assert checks[0]['fix_list_state'] == stored['fix_list_state']
    assert checks[0]['fix_list_delta'] == stored['fix_list_delta']
    with transaction(h.app.state.engine, write=True) as db:
        # Only the base's in-force window changes; its immutable evidence and the draft stay intact.
        db.get(m.Publication, publication_id).effective_until = date(2026, 10, 5)
    second = h.get('coordinator', path)
    assert second['fingerprint'] == first['fingerprint']
    assert len(checks) == 2
    assert checks[0]['retained_checks']['session_count'] == 1
    assert checks[1]['retained_checks']['session_count'] == 0
    assert second == h.get('coordinator', path)
    assert len(checks) == 2
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        captured = []
        canonical = revalidate(db, row, store=False, data_out=captured)
        expected = rota_summary(canonical, [s for s in captured[0]['shifts'] if s['id'] in captured[0]['focus_ids']])
        assert second['issues'] == expected['issues']
        assert second['score'] is None
        assert row.validation == stored


@pytest.mark.parametrize('checking', [False, True])
def test_amendment_summary_never_reuses_a_legacy_complete_check(backend, monkeypatch, checking):
    h = backend
    publication_id, _ = issued(h)
    workspace = h.workspace('Legacy complete amendment check', base=publication_id)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace['id'])
        with monkeypatch.context() as patch:
            patch.setattr(published_changes, 'validation_scope', lambda *args, **kwargs: False)
            legacy = copy.deepcopy(planning.revalidate(db, row, include_internal=True))
        assert 'scope' not in legacy and legacy['score']['score'] is not None
        if checking:
            db.add(m.Job(department_id=row.department_id, workspace_id=row.id, kind='validation',
                         status='queued', requested_by=h.seed['identities']['coordinator'],
                         input_fingerprint=legacy['fingerprint'], input_snapshot={}))
    summary = h.get('coordinator', f"/api/workspaces/{workspace['id']}/summary")
    assert summary['scope'] == 'published_changes' and summary['score'] is None
    assert summary['fix_list']['summary']['checking'] is checking
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, workspace['id']).validation == legacy


def test_reconciliation_preview_binds_template_even_with_updated_payload(backend):
    h = backend
    series = template(h)
    workspace = materialise(h, series)
    series = amend_template(h, series)
    body = {'kind': 'series.reconcile', 'expected_revision': workspace['revision'],
            'payload': payload(workspace, series)}
    response = h.client('coordinator').post('/api/planner/preview', json=body)
    assert response.status_code == 200, response.text
    preview = response.json()
    before = h.detail(workspace)
    series = amend_template(h, series, start='10:00', end='14:00')
    for supplied in (body['payload'], payload(workspace, series)):
        refused = h.command('coordinator', body['kind'], supplied, revision=workspace['revision'],
                            expected_fingerprint=preview['source_fingerprint'], status=409)
        assert refused['error']['code'] == 'STALE_PREVIEW'
    after = h.detail(workspace)
    assert after['workspace'] == before['workspace'] and after['shifts'] == before['shifts']
    body['payload'] = payload(workspace, series)
    fresh = h.client('coordinator').post('/api/planner/preview', json=body)
    assert fresh.status_code == 200, fresh.text
    applied = h.command('coordinator', body['kind'], body['payload'], revision=workspace['revision'],
                        expected_fingerprint=fresh.json()['source_fingerprint'])
    assert applied['updated_count'] == 2

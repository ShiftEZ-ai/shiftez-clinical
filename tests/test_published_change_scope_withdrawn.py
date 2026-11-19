"""Withdrawn publication changes leave later amendments at the no-list boundary."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning, published_changes
from .test_published_changes import change, issued


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_undone_change_keeps_next_amendment_scope_and_no_list_fingerprint(request, database, monkeypatch):
    h = request.getfixturevalue(database)
    publication_id, shifts = issued(h)
    changed = change(h, publication_id, shifts[0])
    undone = h.command('coordinator', 'publication.change.undo', {
        'change_id': changed['change']['id'], 'reason': 'Keep the issued duty unchanged'},
        target=publication_id, revision=changed['workspace']['revision'])
    assert undone['changes'] and all(c['status'] == 'withdrawn' for c in undone['changes'])
    amendment = h.workspace('Amendment after undo', base=publication_id)

    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, amendment['id'])
        row = published_changes.change_list(db, publication_id)
        assert row.workspace_id != workspace.id
        assert all(c['workspace_id'] != workspace.id for c in row.changes)
        captured = []
        checked = planning.revalidate(db, workspace, data_out=captured, store=False, include_internal=True)
        with monkeypatch.context() as no_list:
            no_list.setattr(published_changes, 'change_list', lambda *args: None)
            baseline_data = []
            baseline = planning.revalidate(db, workspace, data_out=baseline_data,
                                           store=False, include_internal=True)

    assert checked.get('scope') == baseline.get('scope') == 'published_changes'
    assert captured[0]['fingerprint'] == baseline_data[0]['fingerprint']


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_foreign_active_change_keeps_full_amendment_check(request, database):
    h = request.getfixturevalue(database)
    publication_id, shifts = issued(h)
    changed = change(h, publication_id, shifts[0])
    amendment = h.workspace('Amendment beside active changes', base=publication_id)

    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, amendment['id'])
        row = published_changes.change_list(db, publication_id)
        assert row.workspace_id == changed['workspace']['id'] != workspace.id
        assert any(c['status'] != 'withdrawn' for c in row.changes)
        checked = planning.revalidate(db, workspace, store=False, include_internal=True)

    assert checked.get('scope') is None

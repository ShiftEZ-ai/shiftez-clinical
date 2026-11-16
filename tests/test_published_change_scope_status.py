"""Amendment lifecycle status keeps the published-change validation boundary stable."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning
from .test_published_changes import issued


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def backend(request):
    return request.getfixturevalue(request.param)


def test_live_amendment_statuses_keep_scope_and_fingerprint(backend):
    h = backend
    publication_id, _ = issued(h)
    amendment = h.workspace('Unlisted amendment', base=publication_id)

    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, amendment['id'])
        assert db.scalar(select(m.PublicationChangeList.id).where(
            m.PublicationChangeList.workspace_id == workspace.id)) is None

        checks = {}
        for status in ('draft', 'submitted', 'approved'):
            workspace.status, workspace.archived_at = status, None
            captured = []
            checks[status] = planning.revalidate(db, workspace, data_out=captured,
                                                  store=False, include_internal=True)
            checks[status] = (checks[status].get('scope'), captured[0]['fingerprint'])

        workspace.status, workspace.archived_at = 'draft', m.now()
        captured = []
        archived = planning.revalidate(db, workspace, data_out=captured,
                                       store=False, include_internal=True)
        workspace.status, workspace.archived_at = 'published', None
        captured = []
        published = planning.revalidate(db, workspace, data_out=captured,
                                        store=False, include_internal=True)

    assert {status: scope for status, (scope, _) in checks.items()} == {
        'draft': 'published_changes', 'submitted': 'published_changes', 'approved': 'published_changes'}
    assert len({fingerprint for _, fingerprint in checks.values()}) == 1
    assert archived.get('scope') is None and published.get('scope') is None

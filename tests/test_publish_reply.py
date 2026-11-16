"""SW3-B-07: publishing replies with the published view, and a database timeout is a retryable 503, never a 500."""
import psycopg
import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction


def test_publish_reply_names_the_published_view(h):
    w = h.shift(h.workspace())['workspace']
    reply = h.publish(w)
    assert reply['workspace']['status'] == 'published'
    assert reply['published_view'] == {'publication_id': reply['publication_id'], 'workspace_id': w['id'],
                                       'sequence': reply['sequence'], 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}


@pytest.mark.postgres
def test_fairness_ledger_timeout_is_a_retryable_503(pg, monkeypatch):
    w = pg.shift(pg.workspace())['workspace']
    w = pg.lifecycle(w, 'submit')['workspace']
    w = pg.lifecycle(w, 'approve', 'approver')['workspace']

    def cancelled(self, row):
        raise psycopg.errors.QueryCanceled('canceling statement due to statement timeout')
    monkeypatch.setattr(psycopg.Copy, 'write_row', cancelled)
    reply = pg.lifecycle(w, 'publish', status=503)
    assert reply['error']['code'] == 'COMMAND_WRITE_TIMEOUT'
    with transaction(pg.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Publication).where(m.Publication.workspace_id == w['id'])) == 0
        assert db.get(m.Workspace, w['id']).status == 'approved'

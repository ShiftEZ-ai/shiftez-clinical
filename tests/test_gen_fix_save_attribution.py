"""Automatic checks store derived evidence without inventing a user action."""
import pytest
from sqlalchemy import select, func
from apps.api import models as m
from apps.api.db import transaction
from .test_gen_fix_auto_check import automatic

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('worker', [False, True])
def test_automatic_check_has_no_user_audit_or_changeset(pg, worker):
    w = pg.workspace()
    with transaction(pg.app.state.engine) as db:
        baseline = db.scalar(select(func.count()).select_from(m.AuditEvent))
    if worker:
        automatic(pg, w)
    else:
        pg.command('coordinator', 'workspace.validate', {'automatic': True, 'reason': 'Automatic rota check'},
                   w['id'], w['revision'])
    with transaction(pg.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.AuditEvent)) == baseline
        assert not list(db.scalars(select(m.ChangeSet).where(m.ChangeSet.workspace_id == w['id'])))
        assert db.get(m.Workspace, w['id']).revision == w['revision']
    pg.command('coordinator', 'workspace.validate', {'reason': 'Requested check'}, w['id'], w['revision'])
    with transaction(pg.app.state.engine) as db:
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'workspace.validate'))
        assert audit.reason == 'Requested check'

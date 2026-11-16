"""Reopening one stage must not depend on its place in bounded housekeeping."""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk_pending_privacy import DATABASES
from .test_bulk_staged import staged_change


@pytest.mark.parametrize("database", DATABASES)
def test_reopen_expires_requested_stage_beyond_the_housekeeping_batch_once(request, database):
    h = request.getfixturevalue(database)
    _, staged = staged_change(h)
    with transaction(h.app.state.engine, write=True) as db:
        requested = db.get(m.BulkStaged, staged["id"])
        requested.expires_at = m.now() - timedelta(hours=1)
        db.add_all([m.BulkStaged(department_id=requested.department_id, owner_id=requested.owner_id,
            status="active", payload=deepcopy(requested.payload), expires_at=m.now() - timedelta(days=2))
            for _ in range(500)])
    for _ in range(2):
        response = h.client("coordinator").post(f"/api/bulk/staged/{staged['id']}/reopen")
        assert response.status_code == 200, response.text
        assert response.json()["expired"] is True and response.json()["preview"] is None
        assert response.json()["staged"]["payload"] == {}
    with transaction(h.app.state.engine) as db:
        requested = db.get(m.BulkStaged, staged["id"])
        assert requested.status == "expired" and requested.payload == {}
        assert requested.revision == staged["revision"] + 1
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(
            m.AuditEvent.action == "bulk.staged.expire", m.AuditEvent.target_id == requested.id)) == 1
        assert db.scalar(select(func.count()).select_from(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == "bulk_staged", m.DefinitionHistory.object_id == requested.id,
            m.DefinitionHistory.object_revision == requested.revision)) == 1

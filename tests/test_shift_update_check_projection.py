"""Replay must keep stored-check projections current when review inputs change."""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import defer

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning_preparation, workspace_reads
from apps.api.services.read_projections import CANONICAL_AUDIENCE


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('field, change', [
    ('accepted_issues', [{'key': 'legacy-issue'}]),
    ('rule_adjustments', [{'policy_id': 'synthetic-rule', 'mode': 'off'}]),
    ('target_overrides', [{'policy_id': 'synthetic-rule', 'amount': 2}]),
])
def test_replay_refreshes_check_projection_on_review_input_change(h, field, change):
    workspace = h.shift(h.workspace())['workspace']
    engine = h.app.state.engine
    ident = workspace['id']
    with transaction(engine, write=True) as db:
        db.add(m.WorkspaceReadProjection(workspace_id=ident, audience=CANONICAL_AUDIENCE,
                kind='warm', value={'synthetic': True}))

    with transaction(engine, write=True) as db:
        planning_preparation.bind(db, {'kind': 'shift.update'}, replay=True)
        row = db.get(m.Workspace, ident, options=(defer(m.Workspace.validation, raiseload=True),))
        setattr(row, field, change)
        db.flush()
        assert 'validation' not in row.__dict__

    with transaction(engine) as db:
        row = db.get(m.Workspace, ident)
        stored = db.get(m.WorkspaceReadProjection, (ident, CANONICAL_AUDIENCE, 'stored_check'))
        assert stored.stamp == workspace_reads.check_stamp(db, row)
        assert stored.value == workspace_reads.check_projection(row, row.validation)
        assert db.scalar(select(m.WorkspaceReadProjection.kind).where(
            m.WorkspaceReadProjection.workspace_id == ident, m.WorkspaceReadProjection.kind == 'warm')) is None

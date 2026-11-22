"""Group authority that changes between preparation and the writer refuses the queued auto-schedule."""
import pytest
from sqlalchemy import select

from apps.api import commands, models as m
from apps.api.db import transaction
from tests.test_planning_groups_drafts import REASON, integrated


def _race(h, monkeypatch, change):
    workspace, _shift, version = integrated(h)
    retry_write = commands.retry_write
    reached = []

    def concurrent(*args, **kwargs):
        if kwargs['context']['command_kind'] == 'job.create' and not reached:
            reached.append(True)
            if change is not None:
                # A real, audited group change committed after preparation and before the writer.
                monkeypatch.setattr(commands, 'retry_write', retry_write)
                h.command('admin', 'planning_group.update', {'definition': change(h, dict(version['definition'])),
                          'reason': REASON}, version['group_id'], version['revision'])
                monkeypatch.setattr(commands, 'retry_write', concurrent)
        return retry_write(*args, **kwargs)

    monkeypatch.setattr(commands, 'retry_write', concurrent)
    result = h.command('coordinator', 'job.create', {'kind': 'generation', 'workspace_id': workspace['id'],
        'planning_group_version_id': version['id'], 'lookahead': 'none'}, revision=workspace['revision'],
        status=(200, 403, 409, 422))
    with transaction(h.app.state.engine) as db:
        jobs = list(db.scalars(select(m.Job.id).where(m.Job.workspace_id == workspace['id'], m.Job.kind == 'generation')))
    return reached, result, jobs


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_control_owner_queues(h, monkeypatch):
    reached, result, jobs = _race(h, monkeypatch, None)
    assert reached == [True] and result.get('job', {}).get('status') == 'queued' and len(jobs) == 1, result


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('kind', ['owner_removed', 'inactive'])
def test_authority_lost_before_writer_refuses(h, monkeypatch, kind):
    def change(h, d):
        if kind == 'owner_removed':
            d['owner_identity_ids'] = [h.seed['identities']['admin']]
            d['co_editor_identity_ids'] = []
        else:
            d['active'] = False
        return d
    reached, result, jobs = _race(h, monkeypatch, change)
    assert reached == [True]
    assert result.get('error', {}).get('code') in {'GROUP_GENERATION_SCOPE', 'GROUP_VERSION_SCOPE'}, result
    assert jobs == []

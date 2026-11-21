"""A session save shares equivalent workload calculations at department scale."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from packages.domain import validation
from tests.test_backend_reads_scale import read_department
from tests.test_backend_scale import post


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_session_save_workload_budget(scale_harness, monkeypatch, backend):
    h = scale_harness(backend)
    workspace = read_department(h)
    shift = next(row for row in h.detail(workspace)['shifts'] if len(row['participants']) == 3)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, workspace['id']).validation = {}
    payload = {key: shift[key] for key in s.ShiftInput.model_fields}
    payload.update(workspace_id=workspace['id'], shift_id=shift['id'], participants=[
        {key: person[key] for key in s.ParticipantInput.model_fields if key in person}
        for person in shift['participants'][:-1]])

    matches = 0
    original = validation.workload_matches

    def count_match(*args):
        nonlocal matches
        matches += 1
        return original(*args)

    monkeypatch.setattr(validation, 'workload_matches', count_match)
    result = post(h, 'shift.update', payload, revision=workspace['revision'])
    assert result['shift_id'] == shift['id']
    assert matches < 50_000, matches

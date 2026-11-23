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
    session_save_workload(scale_harness(backend), monkeypatch, checked=False)


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_checked_session_save_workload_budget(scale_harness, monkeypatch, backend):
    session_save_workload(scale_harness(backend), monkeypatch, checked=True)


def session_save_workload(h, monkeypatch, *, checked):
    workspace = read_department(h)
    shift = next(row for row in h.detail(workspace)['shifts'] if len(row['participants']) == 3)
    if checked:
        post(h, 'workspace.validate', {'reason': 'Check the scale fixture'},
             target=workspace['id'], revision=workspace['revision'])
    else:
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Workspace, workspace['id']).validation = {}
    payload = {key: shift[key] for key in s.ShiftInput.model_fields}
    payload.update(workspace_id=workspace['id'], shift_id=shift['id'], participants=[
        {key: person[key] for key in s.ParticipantInput.model_fields if key in person}
        for person in shift['participants'][:-1]])

    matches = setups = 0
    original = validation.workload_matches
    setup = validation.validate_session_setup

    def count_match(*args):
        nonlocal matches
        matches += 1
        return original(*args)

    def count_setup(*args):
        nonlocal setups
        setups += 1
        return setup(*args)

    monkeypatch.setattr(validation, 'workload_matches', count_match)
    monkeypatch.setattr(validation, 'validate_session_setup', count_setup)
    result = post(h, 'shift.update', payload, revision=workspace['revision'])
    assert result['shift_id'] == shift['id']
    assert matches < 50_000, matches
    if checked:
        # Two admission passes and one rota check each visit at most 30 concurrent sessions.
        assert 0 < setups <= 90, setups

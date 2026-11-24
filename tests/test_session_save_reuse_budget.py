"""A checked session save reuses preparation work without changing its validation scope."""
import pickle
import sys

import pytest
from sqlalchemy import event

from apps.api import commands, snapshots
from apps.api.services import planning_preparation
from packages.contracts import schemas as s
from packages.domain import validation
from tests.test_backend_reads_scale import read_department
from tests.test_backend_scale import post


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_session_save_reuse_budget(scale_harness, monkeypatch, record_property, backend):
    h = scale_harness(backend)
    workspace = read_department(h)
    detail = h.detail(workspace)
    shift = next(row for row in detail['shifts'] if len(row['participants']) == 3)
    post(h, 'workspace.validate', {'reason': 'Check the current rota'}, workspace['id'], workspace['revision'])
    payload = {key: shift[key] for key in s.ShiftInput.model_fields}
    payload.update(workspace_id=workspace['id'], shift_id=shift['id'], participants=[
        {key: person[key] for key in s.ParticipantInput.model_fields if key in person}
        for person in shift['participants'][:-1]])

    rows = scopes = baseline_reads = undo_hashes = 0
    locked = saw_writer = False
    decodes = [0, 0]
    shift_records, edit_scope = snapshots.shift_records, validation.incremental_edit_scope
    snapshot_hash = snapshots.snapshot_hash

    def count_rows(*args, **kwargs):
        nonlocal rows
        projected = shift_records(*args, **kwargs)
        rows += len(projected)
        return projected

    def count_scope(*args, **kwargs):
        nonlocal scopes
        scopes += 1
        return edit_scope(*args, **kwargs)

    def count_hash(value):
        nonlocal undo_hashes
        if isinstance(value, dict) and len(value.get('shifts', [])) == len(detail['shifts']):
            undo_hashes += 1
        return snapshot_hash(value)

    def count_baseline(_connection, _cursor, statement, *_):
        nonlocal baseline_reads
        if statement.startswith('SELECT') and 'workspace_baselines.canonical_payload' in statement:
            baseline_reads += 1

    retry_write, loads = commands.retry_write, pickle.loads
    prepared_decoders = {function.__code__ for function in (
        snapshots.domain_snapshot, planning_preparation.command_compute, planning_preparation.restore_replay)}

    def count_replay(engine, operation, *args, **kwargs):
        if (kwargs.get('context') or {}).get('command_kind') != 'shift.update':
            return retry_write(engine, operation, *args, **kwargs)

        def under_writer(db):
            nonlocal locked, saw_writer
            assert db.connection().info.get('rota_write')
            locked = saw_writer = True
            try:
                return operation(db)
            finally:
                locked = False

        return retry_write(engine, under_writer, *args, **kwargs)

    def count_decode(*args, **kwargs):
        if sys._getframe(1).f_code in prepared_decoders:
            decodes[locked] += 1
        return loads(*args, **kwargs)

    monkeypatch.setattr(snapshots, 'snapshot_hash', count_hash)
    monkeypatch.setattr(commands, 'retry_write', count_replay)
    monkeypatch.setattr(pickle, 'loads', count_decode)
    monkeypatch.setattr(snapshots, 'shift_records', count_rows)
    monkeypatch.setattr(validation, 'incremental_edit_scope', count_scope)
    engine = h.app.state.engine
    event.listen(engine, 'before_cursor_execute', count_baseline)
    try:
        saved = post(h, 'shift.update', payload, revision=workspace['revision'])
    finally:
        event.remove(engine, 'before_cursor_execute', count_baseline)
    assert saved['shift_id'] == shift['id']
    record_property('session_rows_and_validation_scopes', f'{rows},{scopes}')
    record_property('baseline_reads_and_undo_hashes', f'{baseline_reads},{undo_hashes}')
    assert baseline_reads == 1
    assert undo_hashes == 0
    record_property('prepared_graph_decodes', f'{decodes[0]},{decodes[1]}')
    assert saw_writer
    assert rows <= 2 * len(detail['shifts']) + 4, rows
    assert scopes == 1, scopes
    assert decodes[0] >= 4 and decodes[1] == 0, decodes

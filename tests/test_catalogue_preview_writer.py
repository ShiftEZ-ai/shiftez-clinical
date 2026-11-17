"""Configuration previews keep domain work off the serialized writer."""
import pytest
from sqlalchemy import event

from apps.api.services import catalogue_preview
from .test_catalogue_preview import change, preview, state
from .test_resources_calendars import pool, typ


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_catalogue_preview_builds_outside_writer(h, monkeypatch):
    resource = pool(h, capacity=2)
    duty_type = typ(h, resource, resources=[{'resource_pool_id': resource['id'], 'units': 2}])
    workspace = h.shift(h.workspace(), typ=duty_type['id'])['workspace']
    h.client('admin')
    unchanged, detail = state(h), h.detail(workspace)
    original = catalogue_preview.domain_snapshot
    original_evaluate = catalogue_preview.evaluate
    captures, reads, connections = [], [], []

    def snapshot(db, *args, **kwargs):
        assert not db.connection().info.get('rota_write'), 'Snapshot built under the global writer'
        captures.append(True)
        return original(db, *args, **kwargs)

    def statement(connection, cursor, sql, parameters, context, executemany):
        if connection not in connections:
            connections.append(connection)
        if connection.info.get('rota_write') and sql.lstrip().upper().startswith('SELECT'):
            reads.append(sql)

    def evaluate(*args, **kwargs):
        assert not any(not conn.closed and conn.info.get('rota_write') for conn in connections), 'Evaluation under global writer'
        return original_evaluate(*args, **kwargs)

    monkeypatch.setattr(catalogue_preview, 'domain_snapshot', snapshot)
    monkeypatch.setattr(catalogue_preview, 'evaluate', evaluate)
    event.listen(h.app.state.engine, 'before_cursor_execute', statement)
    try:
        result = preview(h, change(resource, capacity=1))
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', statement)
    assert captures and len(reads) <= 55
    assert 'POOLED_RESOURCE_CAPACITY' in {f['code'] for f in result['impacts'][0]['introduced']}
    assert state(h) == unchanged and h.detail(workspace) == detail


def test_changed_inputs_between_capture_and_reader_refuse_preview(h, monkeypatch):
    from contextlib import contextmanager
    from apps.api.services import preview_capture
    resource = pool(h)
    h.shift(h.workspace())
    original = preview_capture.rollback_reader

    @contextmanager
    def changed(engine):
        pool(h, name='Synthetic concurrent configuration')
        with original(engine) as db:
            yield db

    monkeypatch.setattr(preview_capture, 'rollback_reader', changed)
    result = preview(h, change(resource, capacity=2), status=409)
    assert result['error']['code'] == 'STALE_PREVIEW'

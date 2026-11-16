"""Read validators reuse transaction parents and bounded revision metadata."""
import weakref

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import fairness, workspace_reads
from tests.test_backend_scale import measure
from tests.test_fix_list_performance import actor, backend


@pytest.mark.parametrize('finish', ['commit', 'rollback', 'close'])
def test_parent_rows_are_retained_only_until_transaction_end(backend, finish):
    h = backend
    with Session(h.app.state.engine, expire_on_commit=False) as db:
        parents = [(m.Department, h.seed['department_id']), (m.Organisation, h.seed['organisation_id'])]
        references = [weakref.ref(db.get(cls, key)) for cls, key in parents]
        assert all(reference() is not None for reference in references)
        with db.begin_nested():
            pass
        savepoint = db.begin_nested()
        savepoint.rollback()
        assert all(db.get(cls, key) is reference() for (cls, key), reference in zip(parents, references))
        getattr(db, finish)()
        assert all(reference() is None for reference in references)
        assert all(db.get(cls, key) is not None for cls, key in parents)


def test_workspace_validator_batches_revision_metadata(backend):
    h = backend
    workspace = h.workspace()
    statements = []

    def counted(_connection, _cursor, statement, _parameters, _context, _many):
        if 'max(' in statement.lower():
            statements.append(statement)

    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        event.listen(h.app.state.engine, 'before_cursor_execute', counted)
        try:
            first = workspace_reads.fast_etag(db, actor(h), row)
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', counted)
        assert len(statements) == 1
        assert workspace_reads.fast_etag(db, actor(h), row) == first


def test_fairness_route_builds_one_validator_per_read(backend, monkeypatch):
    h = backend
    workspace = h.workspace()
    original, calls = fairness.goals_etag, []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(fairness, 'goals_etag', counted)
    client = h.client('coordinator')
    path = '/api/fairness/goals?workspace_id=' + workspace['id']
    first = client.get(path)
    assert first.status_code == 200
    assert len(calls) == 1
    calls.clear()
    repeat, metrics = measure(h, 'fairness-validator-hit', lambda: client.get(path))
    assert repeat.status_code == 200 and repeat.json() == first.json()
    assert len(calls) == 1
    assert metrics['sql'].get('SELECT', 0) <= 25
    calls.clear()
    unchanged = client.get(path, headers={'if-none-match': first.headers['etag']})
    assert unchanged.status_code == 304 and unchanged.content == b''
    assert len(calls) == 1

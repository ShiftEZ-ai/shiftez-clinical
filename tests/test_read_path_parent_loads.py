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


@pytest.mark.parametrize('change', ['assignment', 'organisation'])
def test_workspace_read_revalidates_changed_body(backend, change):
    h = backend
    workspace = h.shift(h.workspace())['workspace']
    client = h.client('coordinator')
    path = '/api/workspaces/' + workspace['id']
    first = client.get(path)
    assert first.status_code == 200
    before = first.json()
    assert len(before['shifts']) == 1
    unchanged = client.get(path, headers={'If-None-Match': first.headers['etag']})
    assert unchanged.status_code == 304 and unchanged.content == b''
    assert unchanged.headers['etag'] == first.headers['etag']

    if change == 'assignment':
        h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
                people=[h.seed['people'][1]])
    else:
        with transaction(h.app.state.engine, write=True) as db:
            organisation = db.get(m.Organisation, h.seed['organisation_id'])
            assert organisation.timezone != 'UTC'
            organisation.timezone = 'UTC'
            db.flush()
            assert organisation.input_revision > before['current_input_revision']
            saved = db.get(m.Workspace, workspace['id'])
            assert saved.revision == before['workspace']['revision']
            assert saved.content_revision == before['workspace']['content_revision']

    changed = client.get(path, headers={'If-None-Match': first.headers['etag']})
    assert changed.status_code == 200
    assert changed.headers['etag'] != first.headers['etag']
    after = changed.json()
    assert after != before
    if change == 'assignment':
        assert len(after['shifts']) == 2
    else:
        assert after['current_input_revision'] > before['current_input_revision']
        assert after['workspace']['revision'] == before['workspace']['revision']
    fresh = client.get(path)
    assert fresh.status_code == 200 and fresh.json() == after
    unchanged = client.get(path, headers={'If-None-Match': changed.headers['etag']})
    assert unchanged.status_code == 304 and unchanged.content == b''
    assert unchanged.headers['etag'] == changed.headers['etag']


@pytest.mark.parametrize('change', ['assignment', 'organisation'])
def test_fairness_goals_revalidates_changed_body(backend, change):
    h = backend
    policy = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share'}, severity='advisory')
    person = h.seed['people'][0]
    workspace = h.shift(h.workspace(), people=[person])['workspace']
    client = h.client('coordinator')
    path = '/api/fairness/goals?workspace_id=' + workspace['id']
    first = client.get(path)
    assert first.status_code == 200
    before = first.json()
    report = next(goal for goal in before['goals'] if goal['policy_id'] == policy['id'])
    prior = next(row for row in report['people'] if row['person_id'] == person)
    assert prior['planned'] == 1
    unchanged = client.get(path, headers={'If-None-Match': first.headers['etag']})
    assert unchanged.status_code == 304 and unchanged.content == b''
    assert unchanged.headers['etag'] == first.headers['etag']

    if change == 'assignment':
        h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[person])
    else:
        with transaction(h.app.state.engine, write=True) as db:
            organisation = db.get(m.Organisation, h.seed['organisation_id'])
            stamp = organisation.input_revision
            replacement_name = db.get(m.Person, h.seed['people'][1]).display_name
            db.get(m.Person, person).display_name = replacement_name
            db.flush()
            assert organisation.input_revision > stamp
            saved = db.get(m.Workspace, workspace['id'])
            assert (saved.revision, saved.content_revision) == (workspace['revision'], workspace['content_revision'])

    changed = client.get(path, headers={'If-None-Match': first.headers['etag']})
    assert changed.status_code == 200
    assert changed.headers['etag'] != first.headers['etag']
    after = changed.json()
    assert after != before
    report = next(goal for goal in after['goals'] if goal['policy_id'] == policy['id'])
    current = next(row for row in report['people'] if row['person_id'] == person)
    if change == 'assignment':
        assert current['planned'] == 2
    else:
        assert current['name'] == replacement_name and current['name'] != prior['name']
        assert current['planned'] == prior['planned']
    fresh = client.get(path)
    assert fresh.status_code == 200 and fresh.json() == after
    unchanged = client.get(path, headers={'If-None-Match': changed.headers['etag']})
    assert unchanged.status_code == 304 and unchanged.content == b''
    assert unchanged.headers['etag'] == changed.headers['etag']

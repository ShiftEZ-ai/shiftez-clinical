"""Budgets for the foreground API reads that share the authenticated department."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from apps.api import db as database, main, models as m
from apps.api.db import transaction
from apps.api.services import availability, read_projections, workspace_access
from packages.contracts.schemas import ROLE_NAMES
from .clock_support import freeze_clock
from .governance_support import identity
from .test_planning_groups_drafts import private


READS = {
    "/api/me": 17,
    "/api/home": 60,
    "/api/workspaces?limit=50": 30,
    "/api/requests/all?for_me=true": 55,
}


def populated_reads(h):
    h.workspace()
    start = datetime(2026, 10, 4, 6, tzinfo=timezone.utc)
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.RequestRecord(
            department_id=h.seed['department_id'], person_id=person, kind='meeting',
            start=start + timedelta(days=index), end=start + timedelta(days=index, hours=1),
            status='submitted', private_reason='Synthetic calendar request', created_by=h.seed['identities']['staff'],
        ) for index in range(5) for person in h.seed['people'][:10])


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_foreground_endpoint_select_budgets(request, backend):
    h = request.getfixturevalue(backend)
    populated_reads(h)
    client = h.client('admin')
    selects = 0

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal selects
        if statement.lstrip().upper().startswith('SELECT'):
            selects += 1

    engine = h.app.state.engine
    observed = {}
    event.listen(engine, 'before_cursor_execute', count)
    try:
        for path in READS:
            selects = 0
            assert client.get(path).status_code == 200
            observed[path] = selects
    finally:
        event.remove(engine, 'before_cursor_execute', count)

    assert all(count <= READS[path] for path, count in observed.items()), observed


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_warm_home_and_requests_query_and_criteria_budgets(request, backend, monkeypatch):
    freeze_clock(monkeypatch, datetime(2026, 10, 4, tzinfo=timezone.utc))
    h = request.getfixturevalue(backend)
    populated_reads(h)
    client = h.client('coordinator')
    home, requests = '/api/home', '/api/requests/all'
    assert client.get(home).status_code == 200
    first = client.get(requests)
    assert first.status_code == 200 and first.json()['items']

    selects = relationship_options = workspace_builds = 0
    tag_queries = []
    make_option = database.with_loader_criteria
    make_workspace = workspace_access._workspace_graph_criteria
    tag = availability.requests_etag

    def count_sql(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal selects
        selects += statement.lstrip().upper().startswith('SELECT')

    def count_option(*args, **kwargs):
        nonlocal relationship_options
        relationship_options += 1
        return make_option(*args, **kwargs)

    def count_workspace(*args):
        nonlocal workspace_builds
        workspace_builds += 1
        return make_workspace(*args)

    def count_tag(*args):
        before = selects
        value = tag(*args)
        tag_queries.append(selects - before)
        return value

    monkeypatch.setattr(database, 'with_loader_criteria', count_option)
    monkeypatch.setattr(workspace_access, '_workspace_graph_criteria', count_workspace)
    monkeypatch.setattr(availability, 'requests_etag', count_tag)
    engine = h.app.state.engine
    event.listen(engine, 'before_cursor_execute', count_sql)
    try:
        for path, headers, status, budget in (
                (home, {}, 200, 13),
                (requests, {}, 200, 13),
                (requests, {'If-None-Match': first.headers['etag']}, 304, 11)):
            selects = relationship_options = workspace_builds = 0
            assert client.get(path, headers=headers).status_code == status
            assert 0 < selects <= budget, (path, status, selects)
            assert workspace_builds == 1, (path, status, workspace_builds)
            assert relationship_options == 0, (path, status, relationship_options)
    finally:
        event.remove(engine, 'before_cursor_execute', count_sql)
    assert tag_queries == [4, 4]


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_home_and_requests_keep_role_response_bytes(request, backend, monkeypatch):
    freeze_clock(monkeypatch, datetime(2026, 10, 4, tzinfo=timezone.utc))
    h = request.getfixturevalue(backend)
    populated_reads(h)
    draft = private(h)
    with transaction(h.app.state.engine, write=True) as db:
        request_row = db.scalar(select(m.RequestRecord).where(m.RequestRecord.person_id == h.seed['people'][0])
                                .order_by(m.RequestRecord.start).limit(1))
        request_row.workspace_id = draft['id']
    if 'programme_lead' not in h.seed['identities']:
        identity(h, 'programme_lead', ['programme_lead'])
    clients = {role: h.client(role) for role in ROLE_NAMES}
    # Freeze the refusal correlation ID and force full projections for both reads.
    monkeypatch.setattr(main, 'secrets', SimpleNamespace(token_hex=lambda _size: 'a' * 24))
    monkeypatch.setattr(read_projections, 'cached_department', lambda _db, _actor, _kind, _stamp, build, **_: build())

    def responses():
        result = {}
        for role, client in clients.items():
            for path in ('/api/home', '/api/requests/all'):
                response = client.get(path)
                assert response.status_code == (403 if path.endswith('/all') and role in {'credentialer', 'operator'} else 200)
                result[role, path] = response.content, response.headers.get('etag')
        return result

    # The uncached factory constructs the same criteria per SELECT, as before reuse.
    with monkeypatch.context() as reference:
        reference.setattr(database, '_live_relationship_options', database._live_relationship_options.__wrapped__)
        expected = responses()
    assert responses() == expected

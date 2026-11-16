"""Focused regressions for bounded rule-review and workspace-list reads."""

from sqlalchemy import event

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.services.workspace_reads import workspace_list_etag
from apps.api import snapshots


def test_feasibility_currency_does_not_build_objective_inputs(h, monkeypatch):
    workspace = h.shift(h.workspace())['workspace']
    calls = []
    original = snapshots.domain_snapshot

    def counted(db, row, *args, **kwargs):
        calls.append(kwargs)
        return original(db, row, *args, **kwargs)

    monkeypatch.setattr(snapshots, 'domain_snapshot', counted)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        db.info['_activation_read'] = True  # force the bounded builder, bypassing a prior projection
        result = snapshots.feasibility_fingerprints(db, [row])
    assert result[row.id]
    assert calls and all(call['include_fairness'] is True and call['include_objectives'] is False
                         and call['include_fairness_history'] is False for call in calls)


def test_workspace_list_etag_does_not_scan_access_history(h):
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        with transaction(h.app.state.engine) as db:
            actor = Actor(h.seed['identities']['coordinator'], h.seed['department_id'], h.seed['organisation_id'],
                          'Synthetic coordinator', frozenset({'coordinator'}), h.seed['people'][0], 'synthetic-session')
            workspace_list_etag(db, actor, 'compact')
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    assert not any('workspace_access_versions' in statement for statement in statements)

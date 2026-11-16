from datetime import date, timedelta
from types import SimpleNamespace

from sqlalchemy import event

from apps.api import models as m
from apps.api.services import rule_catalogue
from apps.api.snapshots import snapshot_hash
from tests.test_rule_catalogue_api import line
from apps.api.db import transaction


def _actor(h):
    return SimpleNamespace(department_id=h.seed['department_id'], permits=lambda *_roles: True)


def _add_fixture(h, count):
    payloads = []
    with transaction(h.app.state.engine, write=True) as db:
        for index in range(count):
            workspace = m.Workspace(
                department_id=h.seed['department_id'],
                name=f'Source option draft {index:02}',
                starts_on=date(2026, 10, 1) + timedelta(days=index),
                ends_on=date(2026, 10, 8) + timedelta(days=index),
                status='draft',
                created_by=h.seed['identities']['coordinator'],
                last_edited_by=h.seed['identities']['coordinator'],
            )
            db.add(workspace)
            db.flush()
            policies = [{
                'id': f'active-{index}', 'status': 'active',
                'definition': {'rule': {'kind': 'catalogue', 'id': 'duties_per_week',
                                        'lines': [line(numbers={'max': {'value': index + 1}})]}},
            }]
            policies.extend({
                'id': f'retired-{index}-{n}', 'status': 'retired',
                'definition': {'rule': {'kind': 'legacy', 'id': f'legacy-{n}'}},
            } for n in range(300))
            payload = {'policies': policies}
            row = m.WorkspaceBaseline(
                department_id=workspace.department_id, workspace_id=workspace.id,
                workspace_revision=workspace.revision, schema_version=1,
                scope_mode='department', requested_sources=['rules'], included_sources=['rules'],
                omitted_sources=[], dependency_closure={}, source_revisions={},
                provenance={'captured_at': '2026-11-13T00:00:00+00:00'}, canonical_payload=payload,
                payload_hash=snapshot_hash(payload), actor_id=h.seed['identities']['coordinator'],
            )
            db.add(row)
            payloads.append((workspace.id, payload))
    return payloads


def _source_options(h):
    with transaction(h.app.state.engine) as db:
        return rule_catalogue._source_options(db, _actor(h), {
            'duties_per_week': {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': []},
        })


def test_source_options_uses_one_ranked_baseline_read_and_caps_verification(h, monkeypatch):
    _add_fixture(h, 8)
    rule_catalogue._baseline_catalogue_cache.clear()
    from apps.api import workspace_baselines
    calls = []
    original = workspace_baselines.verify
    monkeypatch.setattr(workspace_baselines, 'verify', lambda row: (calls.append(row.id), original(row))[1])
    statements = []
    def sql(_conn, _cursor, statement, _params, _context, _many):
        text = ' '.join(statement.lower().split())
        if text.startswith('select') and 'workspace_baselines' in text:
            statements.append(text)
    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    try:
        options = _source_options(h)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    assert len(statements) == 1
    assert len(calls) <= 3
    assert len(options) == 9
    assert sum(option['changed'] is not None for option in options[1:]) <= 3


def test_source_options_second_read_verifies_no_baselines_after_small_cache_fill(h, monkeypatch):
    _add_fixture(h, 2)
    rule_catalogue._baseline_catalogue_cache.clear()
    from apps.api import workspace_baselines
    calls = []
    original = workspace_baselines.verify
    monkeypatch.setattr(workspace_baselines, 'verify', lambda row: (calls.append(row.id), original(row))[1])
    first = _source_options(h)
    second = _source_options(h)
    assert [option['changed'] for option in first] == [option['changed'] for option in second]
    assert len(calls) == 2


def test_source_options_changed_matches_previous_comparison_after_memo_fill(h):
    payloads = _add_fixture(h, 2)
    rule_catalogue._baseline_catalogue_cache.clear()
    live = {'duties_per_week': {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': []}}
    expected = {}
    for workspace_id, payload in payloads:
        snapshot = rule_catalogue._catalogue_definitions(payload['policies'])
        expected[workspace_id] = sum(snapshot.get(key) != value for key, value in live.items()) + sum(
            key not in live for key in snapshot)
    options = {option['workspace_id']: option for option in _source_options(h)[1:]}
    assert {workspace_id: options[workspace_id]['changed'] for workspace_id in expected} == expected

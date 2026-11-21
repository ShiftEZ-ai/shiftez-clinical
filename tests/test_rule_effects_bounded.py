"""Proposal review work stays bounded as draft count grows."""

from copy import deepcopy
from datetime import date

import pytest
from sqlalchemy import delete, event, select

from apps.api import models as m, snapshots
from apps.api.db import transaction
from tests.test_approval_completeness import checked_approval


def _add_drafts(h, count, *, offset=0):
    identity = h.seed["identities"]["coordinator"]
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.Workspace(
            department_id=h.seed["department_id"], name=f"Bounded review draft {n}",
            starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11), status="draft",
            created_by=identity, last_edited_by=identity)
            for n in range(offset, offset + count))


def _clear_fingerprint_projections(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(delete(m.WorkspaceReadProjection).where(m.WorkspaceReadProjection.kind == "feasibility_fingerprint"))


def _measure_review(h, policy_id, monkeypatch):
    builds = []
    original = snapshots.domain_snapshot

    def counted(*args, **kwargs):
        builds.append(True)
        return original(*args, **kwargs)

    statements = []
    monkeypatch.setattr(snapshots, "domain_snapshot", counted)
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    event.listen(h.app.state.engine, "before_cursor_execute", listener)
    try:
        response = h.client("approver").get(f"/api/policies/{policy_id}/review")
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", listener)
    assert response.status_code == 200, response.text
    return len(builds), len(statements), [statement for statement in statements
                                         if "workspace_baselines" in statement.lower()]


def test_review_effect_work_is_bounded_at_46_overlapping_drafts(h, monkeypatch):
    """A review reads compact receipts once, independent of the draft count."""
    policy = h.policy({"kind": "minimum_cover", "minimum": 1})
    _add_drafts(h, 10)
    ten = _measure_review(h, policy["id"], monkeypatch)

    _clear_fingerprint_projections(h)
    _add_drafts(h, 36, offset=10)
    forty_six = _measure_review(h, policy["id"], monkeypatch)

    assert ten[0] == forty_six[0] == 0
    assert ten[1] == forty_six[1] and ten[1] < 100, {
        "drafts_10": ten, "drafts_46": forty_six,
    }
    assert all("canonical_payload" not in statement.lower()
               for statement in ten[2] + forty_six[2]), {
                   "baseline_statements": ten[2] + forty_six[2],
               }


def test_review_current_receipt_effect_matches_the_saved_effect(h):
    h, workspace, policy, job = checked_approval(h)
    review = h.get('approver', f"/api/policies/{policy['id']}/review")
    stored = h.get('approver', f"/api/jobs/{job['id']}")['job']['result']['effect']
    row = next(effect for effect in review['effect'] if effect['workspace_id'] == workspace['id'])
    assert review.get('not_checked_count', 0) == 0
    assert {key: row[key] for key in ('status', 'clears', 'adds', 'effect', 'summary')} == {
        key: stored[key] for key in ('status', 'clears', 'adds', 'effect', 'summary')}


def test_review_missing_currency_is_explicitly_not_checked(h):
    h, workspace, policy, job = checked_approval(h)
    with transaction(h.app.state.engine, write=True) as db:
        receipt = db.get(m.Job, job['id'])
        metadata = deepcopy(receipt.read_metadata)
        metadata['input_snapshot'].pop('currency_hint', None)
        receipt.read_metadata = metadata
    review = h.get('approver', f"/api/policies/{policy['id']}/review")
    row = next(effect for effect in review['effect'] if effect['workspace_id'] == workspace['id'])
    assert row['status'] == 'not_checked'
    assert review['not_checked_count'] == 1
    assert review['summary'] == '1 drafts not yet re-checked · Check now'


def test_review_live_currency_revision_marks_receipt_not_checked(h):
    h, workspace, policy, job = checked_approval(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    review = h.get('approver', f"/api/policies/{policy['id']}/review")
    row = next(effect for effect in review['effect'] if effect['workspace_id'] == workspace['id'])
    assert row['status'] == 'not_checked' and review['not_checked_count'] == 1


def test_review_skips_drafts_outside_rule_reach(h):
    policy = h.policy({'kind': 'minimum_cover', 'minimum': 1},
                      scope={'service_ids': [h.seed['services'][0]]})
    reached = h.shift(h.workspace('Reached draft'), '2026-10-04T08:00:00+03:00',
                      '2026-10-04T12:00:00+03:00', people=[], service=0)['workspace']
    outside = h.shift(h.workspace('Outside draft'), '2026-10-04T08:00:00+03:00',
                      '2026-10-04T12:00:00+03:00', people=[], service=1)['workspace']
    review = h.get('approver', f"/api/policies/{policy['id']}/review")
    assert [row['workspace_id'] for row in review['effect']] == [reached['id']]
    assert outside['id'] not in {row['workspace_id'] for row in review['effect']}


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_review_baseline_hint_seed_matches_scalar_lookup_with_and_without_rows(h):
    workspace = h.shift(h.workspace("Baseline hint parity"), people=[])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace['id'])
        first = db.scalar(select(m.WorkspaceBaseline).where(m.WorkspaceBaseline.workspace_id == row.id)
                          .order_by(m.WorkspaceBaseline.created_at, m.WorkspaceBaseline.id))
        assert first is not None
        second = m.WorkspaceBaseline(
            department_id=first.department_id, workspace_id=first.workspace_id,
            workspace_revision=first.workspace_revision + 1, schema_version=first.schema_version,
            scope_mode=first.scope_mode, requested_sources=first.requested_sources,
            included_sources=first.included_sources, omitted_sources=first.omitted_sources,
            dependency_closure=first.dependency_closure, source_revisions=first.source_revisions,
            provenance=first.provenance, followed=first.followed, safety_receipts=first.safety_receipts,
            canonical_payload=first.canonical_payload, payload_hash=first.payload_hash, actor_id=first.actor_id)
        db.add(second)
        db.flush()
        db.info.pop('_baseline_hints', None)
        scalar_hint = snapshots._baseline_hint(db, row)
        db.info.pop('_baseline_hints', None)
        snapshots._seed_baseline_hints(db, [row])
        seeded_hint = snapshots._baseline_hint(db, row)
        assert seeded_hint == scalar_hint == [second.id, second.payload_hash]

        empty = m.Workspace(id=m.uid(), department_id=row.department_id, name='No baseline hint',
                            starts_on=date(2027, 1, 4), ends_on=date(2027, 1, 11), status='draft',
                            created_by=row.created_by, last_edited_by=row.last_edited_by)
        db.add(empty)
        db.flush()
        db.info.pop('_baseline_hints', None)
        scalar_empty = snapshots._baseline_hint(db, empty)
        db.info.pop('_baseline_hints', None)
        snapshots._seed_baseline_hints(db, [row, empty])
        assert db.info['_baseline_hints'][empty.id, empty.revision] == [None, None]
        assert snapshots._baseline_hint(db, empty) == scalar_empty == [None, None]

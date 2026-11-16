"""Repeated decisions read queued proposal evidence without approving anything."""
import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.common import history, history_version
from .test_bulk import preview
from .test_bulk_reverse_transitions import FACT_FAMILIES, REASON, fact_payload, proposal
from .test_rule_catalogue_conflicts import line, most
from .test_bulk_selection import actor


@pytest.mark.parametrize('kind', ['legacy_policy', 'catalogue_policy', *FACT_FAMILIES])
def test_repeated_decline_restore_in_one_batch_reads_queued_history_and_undo(h, kind):
    policy = kind.endswith('_policy')
    family = 'policy' if policy else kind
    if kind == 'legacy_policy':
        row = proposal(h)
    elif kind == 'catalogue_policy':
        row = h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_week',
            'lines': [line(numbers=most(5))], **REASON})['record']
    else:
        row = h.command('coordinator', family + '.propose', fact_payload(h, family))['fact']
    model, history_kind = (m.Policy, 'policy') if policy else (m.SchedulingFact, 'scheduling_fact')
    states = ['retired' if policy else 'declined', 'proposed'] * 2
    payload = {'items': [{'item_kind': family + '.' + action, 'target_id': row['id'],
        'expected_revision': row['revision'] + offset, 'payload': REASON}
        for offset, action in enumerate(['decline', 'restore_proposal'] * 2)], **REASON}
    with transaction(h.app.state.engine) as db:
        original_history = db.scalar(select(func.count()).select_from(m.DefinitionHistory))
    check = preview(h, 'coordinator', payload)
    assert check['counts']['problem'] == 0 and len(check['items']) == 4
    with transaction(h.app.state.engine) as db:
        assert db.get(model, row['id']).revision == row['revision']
        assert db.get(model, row['id']).status == 'proposed'
        assert db.scalar(select(func.count()).select_from(m.DefinitionHistory)) == original_history

    applied = h.command('coordinator', 'bulk.apply', payload)
    with transaction(h.app.state.engine) as db:
        current = db.get(model, row['id'])
        assert current.status == 'proposed' and current.approved_by is None
        assert current.revision == row['revision'] + 4
        versions = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == history_kind, m.DefinitionHistory.object_id == row['id'],
            m.DefinitionHistory.batch_id == applied['batch_id']).order_by(m.DefinitionHistory.object_revision)))
        assert [v.object_revision for v in versions] == list(range(row['revision'] + 1, row['revision'] + 5))
        assert [v.snapshot['status'] for v in versions] == states
    assert preview(h, 'coordinator', applied['undo']['payload'])['counts']['problem'] == 0
    undone = h.command('coordinator', **applied['undo'])
    h.command('coordinator', **undone['undo'])
    with transaction(h.app.state.engine) as db:
        current = db.get(model, row['id'])
        assert current.status == 'proposed' and current.approved_by is None
        assert current.revision == row['revision'] + 12


def test_history_version_reads_pending_and_queued_evidence_with_exact_revision(h):
    row = proposal(h)
    with transaction(h.app.state.engine, write=True) as db:
        current = actor(db, h)
        policy = db.get(m.Policy, row['id'])
        policy.status, policy.revision = 'retired', policy.revision + 1
        history(db, current, 'policy', policy, REASON['reason'])
        pending = history_version(db, current, 'policy', row['id'])
        assert pending.object_revision == row['revision'] + 1 and pending.snapshot['status'] == 'retired'
        db.info['bulk'] = {'history': []}
        policy.status, policy.revision = 'proposed', policy.revision + 1
        history(db, current, 'policy', policy, REASON['reason'])
        latest = history_version(db, current, 'policy', row['id'])
        assert latest.object_revision == row['revision'] + 2 and latest.snapshot['status'] == 'proposed'
        assert history_version(db, current, 'policy', row['id'], pending.object_revision) is pending
        assert history_version(db, current, 'policy', row['id'], 999) is None
        db.info.pop('bulk')
        db.rollback()

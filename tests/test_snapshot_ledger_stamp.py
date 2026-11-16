"""Ledger repair is an input writer even when the retained publication is unchanged."""
from sqlalchemy import select, update

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import fairness, wishes
from packages.domain import fairness as domain_fairness
from tests.test_bulk import people_change, preview
from tests.test_fairness import goal
from tests.test_fix_list_performance import actor, backend
from tests.test_snapshot_availability_writers import revisions
from tests.test_staff_wishes import wish
from tests.test_rotation_attachments import rotation_value


def test_rebuild_fairness_stamps_once_and_invalidates_bound_preview(backend):
    h = backend
    workspace = h.shift(h.workspace())['workspace']
    publication_id = h.publish(workspace)['publication_id']
    payload = people_change(h)
    check = preview(h, 'coordinator', payload)
    before = revisions(h)
    with transaction(h.app.state.engine, write=True) as db:
        publication = db.get(m.Publication, publication_id)
        published_revision = publication.revision
        first = fairness.backfill(db, h.seed['department_id'])
        second = fairness.backfill(db, h.seed['department_id'])
        assert first == second and first['rows'] > 0
        assert publication.revision == published_revision
    assert revisions(h) == (before[0] + 1, before[1])
    assert preview(h, 'coordinator', payload)['fingerprint'] != check['fingerprint']
    assert h.command('coordinator', 'bulk.apply', payload, expected_fingerprint=check['fingerprint'], status=409)['error']['code'] == 'STALE_PREVIEW'


def test_rebuilding_changed_fairness_history_moves_semantic_fingerprint(backend):
    h = backend
    goal(h)
    workspace = h.shift(h.workspace())['workspace']
    publication_id = h.publish(workspace)['publication_id']
    later = h.shift(h.workspace(start='2026-10-11', end='2026-10-18'),
        start='2026-10-11T08:00:00+03:00', end='2026-10-11T12:00:00+03:00')['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.scalar(select(m.FairnessLedger).where(m.FairnessLedger.publication_id == publication_id,
            m.FairnessLedger.person_id == h.seed['people'][0], m.FairnessLedger.metric_key == domain_fairness.AVAILABLE))
        assert row is not None
        row.value += 100
    before = h.snapshot(later)
    with transaction(h.app.state.engine, write=True) as db:
        fairness.backfill(db, h.seed['department_id'])
    after = h.snapshot(later)
    assert after['fairness_history'] != before['fairness_history']
    assert after['fingerprint'] != before['fingerprint']


def test_wish_ledger_changes_multiplier_currency_without_a_publication_write(backend):
    h = backend
    wish(h, '2026-10-04', points=4)
    workspace = h.shift(h.workspace())['workspace']
    publication_id = h.publish(workspace)['publication_id']
    wish(h, '2026-10-12', points=2)
    later = h.shift(h.workspace(start='2026-10-11', end='2026-10-18'),
        start='2026-10-12T08:00:00+03:00', end='2026-10-12T12:00:00+03:00')['workspace']
    before = h.snapshot(later)
    stamp = revisions(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.scalar(select(m.WishLedger).where(m.WishLedger.publication_id == publication_id,
            m.WishLedger.person_id == h.seed['people'][0], m.WishLedger.metric == 'wishes_declined'))
        row.value += 1
    after = h.snapshot(later)
    assert revisions(h) == (stamp[0] + 1, stamp[1])
    assert after['wishes'][0]['weight'] != before['wishes'][0]['weight']
    assert after['fingerprint'] != before['fingerprint']


def test_real_wish_ledger_writer_stamps_once_in_its_transaction(backend):
    h = backend
    wish(h, '2026-10-04', points=4)
    workspace = h.shift(h.workspace())['workspace']
    publication_id = h.publish(workspace)['publication_id']
    before = revisions(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace['id'])
        publication = db.get(m.Publication, publication_id)
        organisation = db.get(m.Organisation, h.seed['organisation_id'])
        for _ in range(2):
            wishes.published(db, actor(h), row, publication, organisation.timezone)
        db.flush()
    assert revisions(h) == (before[0] + 1, before[1])


def test_composite_input_rows_use_mapper_keys_for_bulk_updates(backend):
    h = backend
    wish(h, '2026-10-04', points=4)
    published = h.publish(h.shift(h.workspace())['workspace'])
    rotation = h.configure('rotations', rotation_value(h), role='coordinator')['record']
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        ledger = db.scalar(select(m.WishLedger).where(m.WishLedger.publication_id == published['publication_id']))
        attachment = db.get(m.RotationAttachment, rotation['id'])
        before = org.input_revision
        db.execute(update(m.WishLedger), [{
            'publication_id': ledger.publication_id, 'person_id': ledger.person_id, 'metric': ledger.metric,
            'value': ledger.value + 1,
        }])
        db.execute(update(m.RotationAttachment), [{
            'rotation_id': attachment.rotation_id, 'mentorship_id': attachment.mentorship_id,
            'synced_revision': (attachment.synced_revision or 0) + 1,
        }])
        assert org.input_revision == before + 1

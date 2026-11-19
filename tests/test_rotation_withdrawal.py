"""Ordinary rotation removal retains identity, evidence and attachment ownership; creation Undo stays guarded."""
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.contracts import schemas as s
from .test_rotation_attachments import legacy_shared_rotations, rotation_value


REASON = 'Synthetic rotation withdrawal regression'


def future_value(h):
    return {**rotation_value(h), 'starts_on': '2026-12-01', 'ends_on': '2027-01-01'}


def remove(h, rows):
    return h.command('coordinator', 'rotation.remove', {
        'items': [{'id': r['id'], 'revision': r['revision']} for r in rows], 'reason': REASON})


def test_restore_present_rotation_has_specific_conflict(h):
    row = h.configure('rotations', future_value(h), role='coordinator')['record']
    error = h.command('coordinator', 'rotation.restore', {
        'items': [{'id': row['id'], 'revision': row['revision']}], 'reason': REASON}, status=409)['error']
    assert error['code'] == 'ROTATION_NOT_REMOVED'
    assert 'already present' in error['message']


def test_remove_changed_rotation_keeps_it_with_specific_conflict(h):
    row = h.configure('rotations', future_value(h), role='coordinator')['record']
    reply = remove(h, [{**row, 'revision': row['revision'] + 1}])
    assert reply['removed'] == 0 and reply['kept'] == 1
    assert reply['kept_records'] == [{'id': row['id'], 'code': 'STALE_REVISION',
                                    'message': 'This rotation changed; reload its current revision'}]


@pytest.mark.parametrize('attachment', ['owned', 'adopted', 'edited', 'shared'])
def test_rotation_removal_and_served_undo_keep_frozen_evidence_and_attachment_identity(h, attachment):
    value = future_value(h)
    if attachment == 'adopted':
        h.configure('mentorships', {key: value[key] for key in ('person_id', 'mentor_id', 'starts_on', 'ends_on')}
                    | {'service_ids': [value['service_id']], 'strength': 'required'}, role='coordinator')
    rotation = h.configure('rotations', value, role='coordinator')['record']
    with transaction(h.app.state.engine) as db:
        link = db.get(m.RotationAttachment, rotation['id'])
        mentor_id = link.mentorship_id
        mentor = record(link.mentorship)
    if attachment == 'edited':
        mentor = h.configure('mentorships', {key: mentor[key] for key in s.MentorshipInput.model_fields}
                              | {'strength': 'required'}, mentor_id, mentor['revision'], role='coordinator')['record']
    if attachment == 'shared':
        with transaction(h.app.state.engine, write=True) as db:
            other = m.Rotation(department_id=h.seed['department_id'], **{key: val for key, val in value.items()
                if key not in {'mentor_id', 'starts_on', 'ends_on'}}, starts_on=date(2026, 11, 1), ends_on=date(2026, 12, 1))
            db.add(other)
            db.flush()
            db.add(m.RotationAttachment(rotation_id=other.id, mentorship_id=mentor_id, synced_revision=mentor['revision']))
    workspace = h.workspace(start='2026-12-01', end='2026-12-08')
    frozen = h.snapshot(workspace)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, workspace['id']).review_snapshot = frozen
    reply = remove(h, [rotation])
    assert reply['removed'] == 1 and reply['kept'] == 0
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Rotation, rotation['id']) is None
        retained = db.get(m.Rotation, rotation['id'], execution_options={'include_withdrawn': True})
        assert retained.withdrawn and retained.revision == rotation['revision'] + 1
        assert retained.attachment_mentor_id == value['mentor_id']
        assert db.get(m.RotationAttachment, rotation['id']).mentorship_id == mentor_id
        assert record(db.get(m.Mentorship, mentor_id, execution_options={'include_withdrawn': True})) == mentor
        assert db.get(m.Workspace, workspace['id']).review_snapshot == frozen
    live_mentors = h.get('coordinator', '/api/catalogues')['mentorships']
    assert (mentor_id in {r['id'] for r in live_mentors}) == (attachment != 'owned')
    fresh = h.snapshot(h.workspace(start='2026-12-01', end='2026-12-08'))
    assert rotation['id'] not in {r['id'] for r in fresh['rotations']}
    assert (mentor_id in {r['id'] for r in fresh['mentorships']}) == (attachment != 'owned')
    undo = reply['undo']
    h.command('staff', undo['kind'], undo['payload'], status=403)
    stale = {**undo['payload'], 'items': [{'id': rotation['id'], 'revision': 100}]}
    h.command('coordinator', undo['kind'], stale, status=409)
    restored = h.command('coordinator', undo['kind'], undo['payload'])
    assert restored['restored_ids'] == [rotation['id']]
    assert restored['records'][0]['mentor_id'] == value['mentor_id']
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Rotation, rotation['id'])
        assert not row.withdrawn and row.revision == rotation['revision'] + 2
        assert row.attachment_link.mentorship_id == mentor_id
        assert record(db.get(m.Mentorship, mentor_id)) == mentor
        versions = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'rotations',
            m.DefinitionHistory.object_id == row.id).order_by(m.DefinitionHistory.object_revision)))
        assert [v.snapshot['withdrawn'] for v in versions] == [False, True, False]
        assert {v.snapshot['mentor_id'] for v in versions} == {value['mentor_id']}
        actions = set(db.scalars(select(m.AuditEvent.action).where(
            m.AuditEvent.action.in_(['rotation.remove', 'rotation.restore']), m.AuditEvent.reason == REASON)))
        assert actions == {'rotation.remove', 'rotation.restore'}


def test_removed_shared_rotation_does_not_block_live_rotation_edits_or_rewrite_its_attachment(h):
    value = future_value(h)
    first, second = legacy_shared_rotations(h, value)
    with transaction(h.app.state.engine) as db:
        old = record(db.get(m.RotationAttachment, first['id']).mentorship)
    remove(h, [first])
    h.configure('rotations', {**value, 'starts_on': '2026-12-02'}, second['id'], second['revision'], role='coordinator')
    live = h.get('coordinator', '/api/catalogues')['mentorships']
    assert len(live) == 1 and live[0]['id'] != old['id'] and live[0]['starts_on'] == '2026-12-02'
    with transaction(h.app.state.engine) as db:
        assert record(db.get(m.Mentorship, old['id'], execution_options={'include_withdrawn': True})) == old
        assert db.get(m.RotationAttachment, first['id']).mentorship_id == old['id']


def test_rotation_restore_rechecks_ended_membership_without_blocking_removal(h):
    value = future_value(h)
    rotation = h.configure('rotations', value, role='coordinator')['record']
    member = next(r for r in h.get('coordinator', '/api/catalogues')['programme_memberships']
                  if r['person_id'] == value['person_id'])
    h.configure('programme_memberships', {key: member[key] for key in s.ProgrammeMembershipInput.model_fields}
                | {'ends_on': '2026-11-12'}, member['id'], member['revision'], role='coordinator')
    reply = remove(h, [rotation])
    undo = reply['undo']
    assert h.command('coordinator', undo['kind'], undo['payload'], status=422)['error']['code'] == 'ROTATION_MEMBERSHIP'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Rotation, rotation['id']) is None


def test_rotation_restore_rechecks_current_programme_authority(h):
    from .test_residency import lead_of
    value = future_value(h)
    cohort = next(r for r in h.get('coordinator', '/api/catalogues')['cohorts'] if r['id'] == value['cohort_id'])
    lead_of(h, {'id': cohort['programme_id']})
    rotation = h.configure('rotations', value, role='coordinator')['record']
    undo = remove(h, [rotation])['undo']
    other = h.configure('programmes', {'name': 'Other restoration programme', 'starts_on': '2020-01-01'})['record']
    fields = {key: cohort[key] for key in s.CohortInput.model_fields}
    moved = h.configure('cohorts', {**fields, 'programme_id': other['id']}, cohort['id'], cohort['revision'])['record']
    h.command('programme_lead', undo['kind'], undo['payload'], status=403)
    h.configure('cohorts', fields, moved['id'], moved['revision'])
    assert h.command('programme_lead', undo['kind'], undo['payload'])['restored_ids'] == [rotation['id']]


def test_rotation_restore_batch_rolls_back_earlier_rows_when_a_later_row_conflicts(h):
    value = future_value(h)
    first = h.configure('rotations', {**value, 'ends_on': '2026-12-15'}, role='coordinator')['record']
    second = h.configure('rotations', {**value, 'starts_on': '2026-12-15'}, role='coordinator')['record']
    reply = remove(h, [first, second])
    h.configure('rotations', {**value, 'starts_on': '2026-12-15'}, role='coordinator')
    undo = reply['undo']
    assert h.command('coordinator', undo['kind'], undo['payload'], status=409)['error']['code'] == 'ROTATION_OVERLAP'
    with transaction(h.app.state.engine) as db:
        for original in (first, second):
            row = db.get(m.Rotation, original['id'], execution_options={'include_withdrawn': True})
            assert row.withdrawn and row.revision == original['revision'] + 1
# Include this file in hosted supervision and team-lead qualification.

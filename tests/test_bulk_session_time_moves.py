"""Session moves preserve duty reservations instead of silently assigning extra time."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.time import aware
from .test_bulk import REASON, preview
from .test_bulk_workspace_sources import apply
from .test_participation_segments import at


def session(h, *, partial=False, segmented=False):
    made = h.shift(h.workspace(), people=[h.seed['people'][0], h.seed['people'][2]] if segmented else None)
    workspace = made['workspace']
    row = h.detail(workspace)['shifts'][0]
    if partial or segmented:
        with transaction(h.app.state.engine, write=True) as db:
            participant = db.scalar(select(m.Assignment).where(
                m.Assignment.shift_id == row['id'], m.Assignment.person_id == h.seed['people'][0]))
            # Earlier data can contain partial duties; current commands must not expand them to evade validation.
            participant.start = aware(at('10:00' if partial else '08:00'))
            if segmented:
                participant.segments = [
                    {'start': aware(at('10:00' if partial else '08:00')).isoformat(), 'end': aware(at('10:30')).isoformat()},
                    {'start': aware(at('11:00')).isoformat(), 'end': aware(at('12:00')).isoformat()}]
    return workspace, h.detail(workspace)['shifts'][0]


def move(row, *, resize=False):
    return {'item_kind': 'shift.update',
        'selection': {'collection': 'sessions', 'include_ids': [row['id']]},
        'changes': [
            {'field': 'start', 'how': 'move_by', 'value': {'days': 1}},
            {'field': 'end', 'how': 'set', 'value': at('11:00', '2026-10-05')}
                if resize else {'field': 'end', 'how': 'move_by', 'value': {'days': 1}}],
        'reason': REASON}


@pytest.mark.parametrize('segmented', [False, True])
def test_bulk_move_legacy_partial_never_expands_the_duty_or_bypasses_whole_assignment_gate(h, segmented):
    workspace, row = session(h, partial=True, segmented=segmented)
    before = h.detail(workspace)['shifts']
    body = move(row)
    checked = preview(h, 'coordinator', body)
    assert checked['counts']['problem'] == 1
    assert checked['items'][0]['issues'][0]['code'] == 'FULL_ASSIGNMENT_REQUIRED'
    refused = apply(h, body, checked['fingerprint'], status=409)
    assert refused['error']['code'] == 'FULL_ASSIGNMENT_REQUIRED'
    assert h.detail(workspace)['shifts'] == before


@pytest.mark.parametrize('segmented', [False, True])
def test_bulk_move_preserves_each_reserved_bound_and_presence_segment_through_undo(h, segmented):
    workspace, row = session(h, segmented=segmented)
    before = h.detail(workspace)['shifts']
    checked = preview(h, 'coordinator', move(row))
    assert checked['counts']['ready'] == 1, checked['items']
    projected = next(p for p in checked['items'][0]['record']['participants'] if p['person_id'] == h.seed['people'][0])
    original = next(p for p in row['participants'] if p['person_id'] == h.seed['people'][0])
    for field in ('start', 'end'):
        assert aware(projected[field]) == aware(original[field]) + timedelta(days=1)
    assert len(projected['segments']) == len(original['segments'])
    for prior, after in zip(original['segments'], projected['segments']):
        for field in ('start', 'end'):
            assert aware(after[field]) == aware(prior[field]) + timedelta(days=1)
    assert h.detail(workspace)['shifts'] == before
    result = apply(h, move(row), checked['fingerprint'])
    after = next(p for p in h.detail(workspace)['shifts'][0]['participants'] if p['person_id'] == h.seed['people'][0])
    assert {field: after[field] for field in ('start', 'end', 'segments')} == {
        field: projected[field] for field in ('start', 'end', 'segments')}
    h.command('coordinator', **result['undo'])
    assert h.detail(workspace)['shifts'] == before


@pytest.mark.parametrize('segmented', [False, True])
def test_bulk_resize_refuses_a_retained_interval_that_no_longer_fits_without_truncation(h, segmented):
    workspace, row = session(h, segmented=segmented)
    before = h.detail(workspace)['shifts']
    body = move(row, resize=True)
    checked = preview(h, 'coordinator', body)
    assert checked['counts']['problem'] == 1
    issue = checked['items'][0]['issues'][0]
    assert 'Participation must be contained in the session' in issue['message']
    refused = apply(h, body, checked['fingerprint'], status=409)
    assert refused['error']['code'] == issue['code']
    assert h.detail(workspace)['shifts'] == before


@pytest.mark.parametrize('segmented', [False, True])
def test_copy_previous_workspace_does_not_expand_legacy_partial_participation(h, segmented):
    workspace, row = session(h, partial=True, segmented=segmented)
    before = h.detail(workspace)['shifts']
    count = h.get('coordinator', '/api/workspaces')['total']
    refused = h.command('coordinator', 'workspace.create', {
        'name': 'A copy must retain reserved duty intervals',
        'starts_on': '2026-10-11', 'ends_on': '2026-10-18',
        'source_workspace_id': workspace['id'], 'source_revision': workspace['revision'],
        'copy_people': True}, status=422)
    assert refused['error']['code'] == 'FULL_ASSIGNMENT_REQUIRED'
    assert h.detail(workspace)['shifts'] == before
    assert h.get('coordinator', '/api/workspaces')['total'] == count

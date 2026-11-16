"""SW3-A-06: Redo is the inverse of a draft's Undo: same person, refused once stale, audited, re-checked, undoable again."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from tests.test_advisory_undo_fingerprint import advisory_rota
from tests.test_assignment_override_reason import REASON, absence, payload
from tests.test_autoschedule_backend_audit import run_job


def people(h, workspace):
    return [[p['person_id'] for p in s['participants']] for s in h.detail(workspace)['shifts']]


def move_to(h, workspace, person, **extra):
    shift = h.detail(workspace)['shifts'][0]
    return h.command('coordinator', 'shift.update', {**payload(h, workspace, shift, people=[person]), **extra},
                     revision=workspace['revision'])['workspace']


def undo(h, w, **kw):
    return h.command('coordinator', 'workspace.undo', {'reason': 'Undo'}, w['id'], w['revision'], **kw)


def redo(h, w, **kw):
    return h.command('coordinator', 'workspace.redo', {'reason': 'Redo'}, w['id'], w['revision'], **kw)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_redo_puts_back_the_undone_edit_checks_it_and_is_audited_and_undoable(request, database):
    h = request.getfixturevalue(database)
    first, second = h.seed['people'][:2]
    created = h.shift(h.workspace(), people=[first])['workspace']
    edited = move_to(h, created, second)
    assert h.detail(edited)['can_redo'] is False
    undone = undo(h, edited)['workspace']
    assert people(h, undone) == [[first]] and h.detail(undone)['can_redo'] is True
    reply = redo(h, undone)
    redone = reply['workspace']
    assert people(h, redone) == [[second]]
    assert redone['content_revision'] == undone['content_revision'] + 1
    # Re-checked like any edit: the reply carries the rota's fresh health, and Redo is spent.
    assert reply['health']['checked'] is True and h.detail(redone)['can_redo'] is False
    assert redo(h, redone, status=409)['error']['code'] == 'NO_REDO'
    # Undo and Redo alternate as in any editor.
    again = undo(h, redone)['workspace']
    assert people(h, again) == [[first]]
    assert people(h, redo(h, again)['workspace']) == [[second]]
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action.in_(['workspace.undo', 'workspace.redo']))
                                 .order_by(m.AuditEvent.created_at, m.AuditEvent.seq)))
    assert [e.action for e in events] == ['workspace.undo', 'workspace.redo', 'workspace.undo', 'workspace.redo']
    assert {e.reason for e in events if e.action == 'workspace.redo'} == {'Redo'}


def test_a_new_edit_ends_redo_and_a_stale_or_foreign_redo_is_refused(h):
    first, second, third = h.seed['people'][:3]
    created = h.shift(h.workspace(), people=[first])['workspace']
    undone = undo(h, move_to(h, created, second))['workspace']
    # Stale: the revision the client last saw is not the rota's.
    assert redo(h, {**undone, 'revision': undone['revision'] - 1}, status=409)['error']['code'] == 'STALE_REVISION'
    # Someone else's Undo is theirs to redo: this planner has nothing to redo.
    with transaction(h.app.state.engine, write=True) as db:
        step = db.scalars(select(m.ChangeSet).where(m.ChangeSet.workspace_id == undone['id'])
                          .order_by(m.ChangeSet.created_at.desc(), m.ChangeSet.seq.desc())).first()
        mine, step.actor_id = step.actor_id, h.seed['identities']['approver']
    assert h.detail(undone)['can_redo'] is False
    assert redo(h, undone, status=409)['error']['code'] == 'NO_REDO'
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.ChangeSet, step.id).actor_id = mine
    # A new edit clears Redo (the standard editor model).
    later = move_to(h, undone, third)
    assert h.detail(later)['can_redo'] is False
    assert redo(h, later, status=409)['error']['code'] == 'NO_REDO'
    assert people(h, later) == [[third]]


def test_redo_reruns_the_placement_check_when_leave_was_recorded_since(h):
    first, second = h.seed['people'][:2]
    created = h.shift(h.workspace(), people=[second])['workspace']
    undone = undo(h, move_to(h, created, first))['workspace']
    absence(h, 'approved_leave')  # first is now on leave during the session
    refused = redo(h, undone, status=409)['error']
    assert refused['code'] == 'REDO_BLOCKED' and refused['details']['person_ids'] == [first]
    assert people(h, undone) == [[second]] and h.detail(undone)['can_redo'] is True


def test_redo_keeps_an_override_reason_and_its_must_fix_finding(h):
    first, second = h.seed['people'][:2]
    absence(h, 'approved_leave')
    created = h.shift(h.workspace(), people=[second])['workspace']
    undone = undo(h, move_to(h, created, first, override_reason=REASON))['workspace']
    redone = redo(h, undone)['workspace']
    shift = h.detail(redone)['shifts'][0]
    assert shift['participants'][0]['override_reason'] == REASON
    # The conflict still blocks submission: Redo bypasses no must-fix check.
    assert any(f['code'] == 'UNAVAILABLE' and f['severity'] == 'error' for f in h.detail(redone)['workspace']['validation']['findings'])


def test_redo_applies_an_undone_auto_schedule_proposal_again(h):
    workspace, _ = advisory_rota(h)
    job = run_job(h, workspace)
    assert job['result']['additions']
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'], 'job_id': job['id'],
                        'reason': 'Apply proposal'}, revision=workspace['revision'])['workspace']
    undone = undo(h, applied)['workspace']
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'
    redone = redo(h, undone)['workspace']
    again = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert again['status'] == 'applied'
    assert again['result']['applied_content_revision'] == redone['content_revision']
    assert people(h, redone) == people(h, applied)

"""Undo follows the active edit history and stops when its earliest edit has been undone."""
from datetime import datetime

REASON = 'Independent test review decision'


def test_undo_walks_back_available_edits_then_refuses(h):
    created = h.shift(h.workspace()); w = created['workspace']; sid = created['shift_id']
    locked = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [sid], 'action': 'lock', 'reason': REASON}, revision=w['revision'])
    assert locked['workspace']['can_undo'] is True
    assert h.detail(w)['workspace']['can_undo'] is True
    undone = h.command('coordinator', 'workspace.undo', {'reason': REASON}, w['id'], locked['workspace']['revision'])
    assert undone['workspace']['can_undo'] is True
    detail = h.detail(w)
    assert detail['workspace']['can_undo'] is True
    assert not detail['shifts'][0]['locked']
    assert detail['shifts'][0]['lineage_id'] is None
    older = h.command('coordinator', 'workspace.undo', {'reason': REASON}, w['id'], undone['workspace']['revision'])
    assert older['workspace']['can_undo'] is False
    assert h.get('coordinator', f"/api/workspaces/{w['id']}?view=compact")['workspace']['can_undo'] is False
    again = h.command('coordinator', 'workspace.undo', {'reason': REASON}, w['id'], older['workspace']['revision'], status=409)
    assert again['error']['code'] == 'NO_UNDO'
    assert again['error']['message'] == 'Nothing left to undo'
    assert h.detail(w)['shifts'] == []


def test_the_undo_description_names_the_target_and_a_stale_target_cannot_undo_another_edit(h):
    first = h.shift(h.workspace(), people=[], role='admin')['workspace']
    edit = first['undo_edit']
    assert edit['own'] and edit['id'] == first['undo_change_id']
    assert edit['label'] == 'added a session'
    assert datetime.fromisoformat(edit['at']).utcoffset() is not None
    path = f"/api/workspaces/{first['id']}"
    other = h.get('coordinator', path)['workspace']['undo_edit']
    assert other == {**edit, 'own': False, 'author': other['author']}
    assert other['author']
    for view in ('compact', 'schedule'):
        assert h.get('coordinator', f'{path}?view={view}')['workspace']['undo_edit'] == other
    assert h.get('approver', path)['workspace']['undo_edit'] is None

    second = h.shift(first, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[])['workspace']
    refused = h.command('coordinator', 'workspace.undo', {'reason': REASON, 'undo_of': edit['id']},
                        first['id'], second['revision'], status=409)
    assert refused['error']['code'] == 'STALE_UNDO'
    assert len(h.detail(first)['shifts']) == 2
    undone = h.command('coordinator', 'workspace.undo', {'reason': REASON, 'undo_of': second['undo_edit']['id']},
                       first['id'], second['revision'])['workspace']
    assert undone['can_undo'] and undone['undo_edit'] is None
    assert h.detail(first)['workspace']['undo_edit'] == {**other, 'revision': undone['revision']}


def test_undo_stops_at_a_reopened_review_boundary(h):
    created = h.shift(h.workspace())
    w, sid = created['workspace'], created['shift_id']
    submitted = h.lifecycle(w, 'submit')['workspace']
    reopened = h.lifecycle(submitted, 'reopen')['workspace']
    locked = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [sid],
        'action': 'lock', 'reason': REASON}, revision=reopened['revision'])
    undone = h.command('coordinator', 'workspace.undo', {'reason': REASON}, w['id'], locked['workspace']['revision'])
    assert undone['workspace']['can_undo'] is False
    assert not h.detail(w)['shifts'][0]['locked']


def test_undo_can_clear_acknowledgements_without_crossing_the_review_boundary(h):
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': 'duties', 'comparison': 'minimum', 'amount': 3}, {'person_id': h.seed['people'][0]})
    w = h.shift(h.workspace())['workspace']
    cause, = [c for c in h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['causes'] if c['acknowledgeable']]
    submitted = h.command('coordinator', 'workspace.submit', {
        'reason': REASON, 'acknowledgements': [{'cause_id': cause['id'], 'reason': REASON}],
    }, w['id'], w['revision'])['workspace']
    reopened = h.lifecycle(submitted, 'reopen')['workspace']
    assert h.detail(reopened)['workspace']['can_undo'] is True

    undone = h.command('coordinator', 'workspace.undo', {'reason': REASON}, w['id'], reopened['revision'])['workspace']
    assert undone['accepted_issues'] == []
    assert undone['can_undo'] is False
    refused = h.command('coordinator', 'workspace.undo', {'reason': REASON}, w['id'], undone['revision'], status=409)
    assert refused['error']['code'] == 'NO_UNDO'
    assert len(h.detail(w)['shifts']) == 1

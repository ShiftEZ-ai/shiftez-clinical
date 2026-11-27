"""Undo follows the active edit history and stops when its earliest edit has been undone."""
REASON = 'Independent test review decision'


def test_second_undo_is_refused_not_a_redo(h):
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

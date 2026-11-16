"""Undo takes back the last edit once; a second Undo is refused instead of redoing the edit."""
REASON = 'Independent test review decision'


def test_second_undo_is_refused_not_a_redo(h):
    created = h.shift(h.workspace()); w = created['workspace']; sid = created['shift_id']
    locked = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [sid], 'action': 'lock', 'reason': REASON}, revision=w['revision'])
    undone = h.command('coordinator', 'workspace.undo', {'reason': REASON}, w['id'], locked['workspace']['revision'])
    assert not h.detail(w)['shifts'][0]['locked']
    assert h.detail(w)['shifts'][0]['lineage_id'] is None
    again = h.command('coordinator', 'workspace.undo', {'reason': REASON}, w['id'], undone['workspace']['revision'], status=409)
    assert again['error']['code'] == 'NO_UNDO'
    assert not h.detail(w)['shifts'][0]['locked']
    assert h.detail(w)['shifts'][0]['lineage_id'] is None

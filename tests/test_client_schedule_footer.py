"""The Schedule footer's left side, from the real compiled schedule.ts in Node (logic only): it is the published layer's
own status unless a rota in work is open; a published rota never gets the draft's check pill (maintenance line 10)."""
from .test_client_staff_states import node


def test_only_a_rota_in_work_shows_the_drafts_check_status(client_modules):
    states = ['draft', 'submitted', 'approved', 'published', 'superseded', None]
    got = node(client_modules, 'schedule.js', "[m.footerIsPublished(null),m.footerIsPublished(undefined),"
               f"...{states!r}.map(s=>m.footerIsPublished({{status:s}})),m.LIVE]".replace('None', 'undefined'))
    # No rota open, or one that is not in work (published, anything unknown): the published layer's status.
    assert got[:2] == [True, True]
    assert got[2:8] == [False, False, False, True, True, True]
    assert got[8] == states[:3]  # the one list of in-work states

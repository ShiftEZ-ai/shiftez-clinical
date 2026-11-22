"""A session address's "This session moved" note, from the real compiled schedule.ts in Node (logic only). A link to the
session names the day it was on, so any other day is a move; a session panel's own address names only the day in view,
so it says moved only for another week, never for another day of the same week."""
import json

from .test_client_staff_states import node


def moved(client_modules, cases):
    return node(client_modules, 'schedule.js', f"{json.dumps(cases)}.map(([a,b,w,n])=>m.linkMoved(a,b,w,n))")


def test_a_session_panels_address_says_moved_only_when_its_week_changed(client_modules):
    # 2026-03-02 is a Monday. Weeks start on Monday (0) or on Sunday (6).
    assert moved(client_modules, [
        ['2026-03-02', '2026-03-05', 0, False],  # another day of the same week: not a move
        ['2026-03-08', '2026-03-02', 0, False],  # Sunday, back in the Monday week it ends
        ['2026-03-02', '2026-03-09', 0, False],  # the next week: moved
        ['2026-03-08', '2026-03-02', 6, False],  # with Sunday-first weeks that Sunday starts the next week: moved
    ]) == [False, False, True, True]


def test_a_link_to_the_session_says_moved_for_any_other_day(client_modules):
    assert moved(client_modules, [
        ['2026-10-04', '2026-10-10', 6, True],  # Sunday to Saturday of one week: it did move
        ['2026-10-04', '2026-10-04', 6, True],  # the same day
        ['', '2026-10-04', 6, True],  # nothing in view yet: the link only opens there
    ]) == [True, False, False]

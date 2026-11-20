"""The legend guard on the Schedule's grids reviewed: People × Days, the
codes grid, both counts grids, Coverage and List draw one rota holding every kind of mark they have (leave, wishes, a
rule issue, a pair, a trainee alone, a team lead, an open place, a closed session, the published layer); every mark drawn
is stamped with its entry in the view's one key, and every entry has a mark on screen (tests/legend_guard.py)."""
from tests.historical_staffing import retained_staffing
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, pick_view  # noqa: F401
from .test_browser_schedule import at, checked, october, show, settled
from .test_browser_qa_planner import clinic
from .legend_guard import assert_legend_matches

pytestmark = pytest.mark.browser
VIEW = '#schedule-view'


def marked(h):
    """october(): published 4–13 Oct (the grey published layer on 12 Oct) and a draft 14–20 Oct with Alex on 14 Oct. Added
    to the draft: Sam on 14 Oct against a mandatory rule, an open place on 15 Oct, Jordan staffing 16 Oct, Jordan
    supervising Alex (↳) as team lead on 16 Oct, Alex as a trainee alone on 17 Oct, a closed session on 17 Oct, Alex's
    leave on 15 Oct, a wish for time off on 15 Oct and a wish to work on 17 Oct. Returns (draft, clinic type, paired type)."""
    a, b, c = h.seed['people']
    _, d = october(h)
    typ = clinic(h)  # needs one person: an empty session is an open place
    paired = h.configure('assignment_types', {'name': 'Legend paired clinic', 'starts_on': '2020-01-01', 'definition': {
        'supervision': {'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
                        'trainees_per_supervisor': 1}}})['record']['id']
    d = h.shift(d, *at(14, '13', '17'), people=[c], typ=typ)['workspace']
    d = h.shift(d, *at(15, '13', '17'), people=[], typ=typ)['workspace']
    d = h.shift(d, *at(16, '13', '17'), people=[b], typ=typ)['workspace']
    s, e = at(16)
    d = h.shift(d, s, e, typ=paired, people=[{'person_id': a, 'start': s, 'end': e, 'participation': 'trainee', 'supervisor_person_id': b},
        {'person_id': b, 'start': s, 'end': e, 'participation': 'supervisor', 'team_lead': True}])['workspace']
    s, e = at(17)
    d = retained_staffing(h, d, s, e, typ=paired, people=[
        {'person_id': a, 'start': s, 'end': e, 'participation': 'trainee'}])['workspace']
    d = h.shift(d, *at(17, '13', '17'), people=[b])['workspace']
    closed = [x['id'] for x in h.detail(d)['shifts'] if x['assignment_type_id'] == h.seed['type'] and str(x['start']).startswith('2026-10-17') and any(p['person_id'] == b for p in x['participants'])]
    d = h.command('coordinator', 'shift.action', {'workspace_id': d['id'], 'shift_ids': closed, 'action': 'cancel',
        'reason': 'Clinic closed for the legend check'}, revision=d['revision'])['workspace']
    h.policy({'kind': 'restriction'}, {'person_id': c})  # after the draft holds Sam's duty: Sam may not work, a rule issue
    # The wishes first: a wish for a day that already has leave is refused (WISH_ON_LEAVE, wishes.py); leave recorded
    # later over a wish keeps both on the grid.
    h.command('staff', 'wish.create', {'person_id': a, 'first_day': '2026-10-15', 'last_day': '2026-10-15', 'kind': 'prefer_off', 'points': 2})
    h.command('staff', 'wish.create', {'person_id': a, 'first_day': '2026-10-17', 'last_day': '2026-10-17', 'kind': 'prefer_on', 'points': 2})
    h.command('coordinator', 'availability.record', {'person_id': a, 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
        'start': '2026-10-15T00:00:00+03:00', 'end': '2026-10-16T00:00:00+03:00', 'private_reason': 'Fictional leave'})
    # The grids as saved views: codes with Leave and wishes shown, and counts by duty and by person.
    for name, planner in (('Legend codes', {'rows': 'person', 'cells': 'codes', 'properties': [{'key': 'leave', 'on': True, 'form': 'text'}]}),
                          ('Legend counts by duty', {'rows': 'type', 'cells': 'counts'}),
                          ('Legend counts by person', {'rows': 'person', 'cells': 'counts'})):
        h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': name, 'definition': {'planner': {'mode': 'timeline', 'range': 'week', **planner}}})
    return checked(h, d), typ, paired


def test_every_mark_on_the_schedule_grids_has_its_key_entry(browser, live_server, h):
    d, typ, paired = marked(h)
    seed = h.seed['type']
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        # People × Days: duty codes, states, pairs, open places, leave and wishes (leave arrives after the grid).
        show(page, f"schedule/people/2026-10-12?edit={d['id']}")
        expect(page.locator(f'{VIEW} .pg-cell.leave')).to_have_count(1)
        assert_legend_matches(page, VIEW, expect_marks=[f't:{seed}', f't:{typ}', f't:{paired}', 'issue:error', 'closed', 'ctx', 'pair',
                                                        'alone', 'open', 'leave', 'wish:on', 'wish:off', 'weekend'])  # Fri/Sat 16-17 Oct shaded
        # The codes grid (people rows, duty codes) with Leave and wishes.
        show(page, f"schedule/week/2026-10-12?edit={d['id']}")
        pick_view(page, 'Legend codes'); settled(page)
        expect(page.locator(f'{VIEW} .sg-grid:not(.ct-grid) .pg-cell.leave')).to_have_count(1)
        assert_legend_matches(page, VIEW, expect_marks=[f't:{seed}', f't:{typ}', f't:{paired}', 'ctx', 'leave', 'wish:on', 'wish:off'])
        # Counts: by duty (places filled / needed), and by person (duties, with the short Staffed / needed row).
        pick_view(page, 'Legend counts by duty'); settled(page)
        expect(page.locator(f'{VIEW} .ct-grid .sg-count.short').first).to_be_visible()
        assert_legend_matches(page, VIEW, expect_marks=['fill'])
        pick_view(page, 'Legend counts by person'); settled(page)
        expect(page.locator(f'{VIEW} .ct-grid .tl-cover .sg-count.short').first).to_be_visible()
        assert_legend_matches(page, VIEW, expect_marks=['duties', 'short'])
        # Coverage: each cell's state and issue, each row's duty colour.
        show(page, f"schedule/coverage/2026-10-12?edit={d['id']}")
        expect(page.locator(f'{VIEW} .coverage-table')).to_be_visible()
        assert_legend_matches(page, VIEW, expect_marks=[f't:{typ}', 'full', 'short', 'none', 'issue:error'])  # UIUX L5: the shared Must fix mark
        # List: each duty's colour on its dot and pills, and the team lead.
        show(page, f"schedule/list/2026-10-12?edit={d['id']}")
        expect(page.locator(f'{VIEW} [data-table=planner-list] tbody tr').first).to_be_visible()
        assert_legend_matches(page, VIEW, expect_marks=[f'c:t:{typ}', f'c:t:{paired}', 'tag:lead'])
        assert not errors, errors
    finally:
        context.close()

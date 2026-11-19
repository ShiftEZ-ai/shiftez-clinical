"""Recorded supervision teams stay together in one team box (team_view.ts) in Calendar cells without losing member actions."""
import os
from pathlib import Path
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, pick_view, sign_in, show_names  # noqa: F401
from .test_browser_autoschedule_live import Stream
from .test_browser_rule_catalogue import flag
from .test_governed_leadership import activate, line, proposal

pytestmark = pytest.mark.browser
START, END = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
NAMES = ['Fictional Consultant Alexandrina Northumberland',
         'Fictional Trainee Benedicta Featherstonehaugh',
         'Fictional Trainee Constantinopoulos Winterbourne', 'Fictional Independent Quinn']


def fixture(h, *, crowd=0, lead=False, absent=False):
    # This is explicitly an unconverted department; its historical supervision setting is intentional.
    flag(h)
    people = [h.configure('people', {'display_name': name})['record']['id'] for name in NAMES]
    people += [h.configure('people', {'display_name': f'Fictional Unpaired {i}'})['record']['id'] for i in range(crowd)]
    for i, person in enumerate(people):
        h.command('coordinator', 'membership.create', {'person_id': person, 'starts_on': '2020-01-01',
            'category_id': h.seed['trainee_category'] if i in (1, 2) else h.seed['category']})
        h.command('credentialer', 'eligibility.create', {'person_id': person, 'service_id': h.seed['services'][0],
            'starts_on': '2020-01-01'})
    h.command('credentialer', 'grant.create', {'person_id': people[0], 'capability_id': h.seed['supervisor_capability'],
        'starts_on': '2020-01-01', 'evidence': 'Synthetic supervision qualification'})
    typ = h.configure('assignment_types', {'name': 'Fictional supervised clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 3}], 'supervision': {
            'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
            'trainees_per_supervisor': 2}}})['record']['id']
    if lead:
        activate(h, proposal(h, [line(typ, [people[0]])]))
    parts = [{'person_id': p, 'start': START, 'end': END,
              **({'participation': 'supervisor', 'team_lead': lead} if i == 0 else
                 {'participation': 'trainee', 'supervisor_person_id': people[0]} if i in (1, 2) else {})}
             for i, p in enumerate(people)]
    w = h.shift(h.workspace('Fictional team pill acceptance'), typ=typ, people=parts)['workspace']
    if absent:
        h.command('coordinator', 'availability.record', {'person_id': people[2], 'start': START, 'end': END,
            'kind': 'actual_absence', 'private_reason': 'Fictional private reason'})
        w = h.lifecycle(w, 'validate')['workspace']
    return w, h.detail(w)['shifts'][0], people, typ


def calendar(page, w, view):
    navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
    pick_view(page, view)
    # Phone autoAgenda is a different renderer; choose the canonical Calendar Grid control.
    switch = page.get_by_role('radiogroup', name='Calendar layout')
    if switch.count():
        switch.get_by_role('radio', name='Grid', exact=True).click()
    show_names(page, 'full')
    grid = page.locator('#schedule-view .wk-grid')
    expect(grid).to_be_visible()
    return grid


def member(grid, session, person):
    return grid.locator(f'.pchip[data-shift="{session}"][data-person="{person}"]')


def team(grid, session):
    return grid.locator('.team-box').filter(has=grid.page.locator(f'[data-shift="{session}"]'))


def full_names(pill, people, names=None, narrow_ok=False):
    """One line per person reviewed: each member's row shows its short name, ending in … when long,
    and its chip still names the person in full (its accessible label and hover card)."""
    expect(pill).to_have_count(1)
    if names is None:
        names = NAMES[:len(people)]
    for p, name in zip(people, names, strict=True):
        chip = pill.locator(f'[data-person="{p}"]')
        if not chip.count():
            continue  # behind the team's "+N more" (it opens the session with everyone)
        expect(chip).to_have_attribute('aria-label', re.compile('^' + re.escape(name) + ','))
        # A screen cell too narrow for the label shows the person's short form or code instead (app.css @container
        # pill, L7); paper never does. Either way exactly one name form shows and the chip says the full name.
        text = chip.locator('.pchip-name:visible,.pchip-alt:visible' if narrow_ok else '.pchip-name')
        expect(text).to_have_count(1)
        expect(text).to_be_visible()
        expect(text).to_have_text(re.compile(r'\S'))
    assert pill.evaluate("n=>[...n.querySelectorAll('[data-person]')].length+(n.querySelector('.tm-more')?Number(n.querySelector('.tm-more').textContent.match(/\\d+/)[0]):0)") == len(people)


@pytest.mark.parametrize('view', ['Week', 'Month'])
@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')])
def test_team_is_one_outer_pill_with_every_full_name(browser, live_server, h, view, width, scheme):
    w, session, people, _ = fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900}, color_scheme=scheme)
    try:
        grid = calendar(page, w, view)
        pill = team(grid, session['id'])
        full_names(pill, people[:3], narrow_ok=width < 1440)  # a phone cell under 120 px shows the short form (L7, 18d6b2807)
        expect(member(grid, session['id'], people[3])).to_be_visible()
        expect(pill.locator(f'[data-person="{people[3]}"]')).to_have_count(0)
        folder = os.getenv('TEAM_PILL_SHOTS') or (str(Path(os.environ['PW_TRACE_DIR']).parent / 'team-pills')
                                                 if os.getenv('PW_TRACE_DIR') else None)
        if folder:
            Path(folder).mkdir(parents=True, exist_ok=True)
            grid.screenshot(path=str(Path(folder) / f'team-{view}-{width}-{scheme}.png'))
    finally:
        context.close()


@pytest.mark.parametrize('view', ['Week', 'Month'])
def test_busy_cell_limits_whole_teams_atomically(browser, live_server, h, view):
    w, session, people, _ = fixture(h, crowd=8)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        grid = calendar(page, w, view)
        # Participant order is independent of the fixture's insertion order. The team may be wholly behind More.
        initial = team(grid, session['id'])
        assert initial.count() <= 1
        visible_members = grid.locator(','.join(
            f'.pchip[data-shift="{session["id"]}"][data-person="{p}"]:visible' for p in people[:3]))
        assert visible_members.count() in (0, 3)
        if initial.count():
            expect(initial.locator('[data-person]')).to_have_count(3)
        if visible_members.count():
            full_names(initial, people[:3])
        cell = grid.locator('.wk-cell').filter(has=page.locator('.pchip.more'))
        more = cell.locator(':scope > .pchip.more')
        expect(more).to_be_visible()
        # Ten outer items: one three-person team and nine independent people.
        # Every cell says "+N" (DESIGN D8, dom.ts overflowMore); its name says it in full.
        expect(more).to_have_text('+2' if view == 'Week' else '+8')
        expect(more).to_have_accessible_name('2 more people: show all' if view == 'Week'
                                             else re.compile(r'^8 more entries on .+: show all$'))
        more.click()
        scope = grid if view == 'Week' else page.locator('.mo-more')
        full_names(team(scope, session['id']), people[:3])
        for p in people:
            expect(member(scope, session['id'], p)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('view', ['Week', 'Month'])
def test_only_recorded_same_session_supervision_forms_a_team(browser, live_server, h, view):
    w, session, people, typ = fixture(h)
    start, end = '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00'
    w = h.shift(w, start, end, typ=typ, people=[
        {'person_id': people[1], 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': people[0]},
        {'person_id': people[3], 'start': start, 'end': end, 'participation': 'supervisor'}])['workspace']
    other = next(s for s in h.detail(w)['shifts'] if s['id'] != session['id'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        grid = calendar(page, w, view)
        full_names(team(grid, session['id']), people[:3])
        # The named supervisor is not on this session: the trainee is alone in a gold "Supervisor elsewhere" box.
        away = team(grid, other['id'])
        expect(away).to_have_class(re.compile(r'\btm-elsewhere\b'))
        # Said by the one issue marker (D78): a could-improve circle in the supervisor's slot, its words its name.
        expect(away.locator(':scope > .tm-sup > .iss-mark.warning')).to_have_attribute('aria-label', re.compile('Could improve: Not on this session'))
        expect(away).to_have_attribute('aria-label', re.compile(re.escape(f'with {NAMES[0]}, not on this session')))
        expect(away.locator('[data-person]')).to_have_count(1)
        missing = member(grid, other['id'], people[1])
        expect(missing).to_have_attribute('aria-label', re.compile('named supervisor not on this session'))
        expect(missing).to_have_attribute('aria-label', re.compile(re.escape(NAMES[0])))
        expect(member(grid, other['id'], people[0])).to_have_count(0)
        expect(member(grid, other['id'], people[3])).to_be_visible()
    finally:
        context.close()


def test_each_team_member_keeps_their_panel_menu_marks_and_removal(browser, live_server, h):
    w, session, people, _ = fixture(h, lead=True, absent=True)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        grid = calendar(page, w, 'Week')
        full_names(team(grid, session['id']), people[:3])
        expect(member(grid, session['id'], people[0])).to_have_attribute('aria-label', re.compile('team lead'))
        expect(member(grid, session['id'], people[1])).not_to_have_attribute('aria-label', re.compile('team lead'))
        expect(member(grid, session['id'], people[2])).to_have_class(re.compile(r'\babsent\b'))
        expect(team(grid, session['id'])).not_to_have_class(re.compile(r'\babsent\b'))
        for p, name in zip(people[:3], NAMES):
            chip = member(grid, session['id'], p)
            chip.click()
            expect(page.locator('.wk-panel .wk-person strong')).to_have_text(name)
            expect(chip).to_have_attribute('aria-current', 'true')
            expect(grid.locator('.team-box[aria-current]')).to_have_count(0)
            page.keyboard.press('Escape')
            chip.click(button='right')
            menu = page.get_by_role('menu', name='Actions for ' + name, exact=True)
            expect(menu.get_by_role('menuitem', name='Remove from this session', exact=True)).to_be_visible()
            page.keyboard.press('Escape')
        member(grid, session['id'], people[1]).click(button='right')
        page.get_by_role('menuitem', name='Remove from this session', exact=True).click()
        expect(member(grid, session['id'], people[1])).to_have_count(0)
        for p in (people[0], people[2], people[3]):
            expect(member(grid, session['id'], p)).to_be_visible()
        saved = h.detail(w)['shifts'][0]['participants']
        assert {p['person_id'] for p in saved} == {people[0], people[2], people[3]}
        assert next(p for p in saved if p['person_id'] == people[2])['supervisor_person_id'] == people[0]
        expect(page.locator('.toast').get_by_role('button', name='Undo', exact=True)).to_be_visible()
    finally:
        context.close()


def test_live_week_fill_leave_and_shift_keep_unrelated_people_outside_team(browser, live_server, h):
    w, session, people, typ = fixture(h)
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', typ=typ, people=[])['workspace']
    shifts = h.detail(w)['shifts']
    other = next(s for s in shifts if s['id'] != session['id'])
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])
    context, page = sign_in(browser, live_server, 'coordinator')
    stream = Stream(page, live_server, w['id'])
    try:
        grid = calendar(page, w, 'Week')
        auto = page.get_by_role('button', name='Auto-schedule', exact=True)
        if auto.get_attribute('aria-expanded') == 'true':
            auto.click()
        expect(grid).to_have_attribute('aria-busy', 'true')
        stream.progress = {'stage': 'improving', 'elapsed_seconds': 9, 'search_started_seconds': 3, 'time_limit_seconds': 120}
        # A person leaves from inside a team; the open place belongs to the session, outside the team.
        stream.event(removes=[(session['id'], people[1])])
        expect(member(grid, session['id'], people[1])).to_have_count(0)
        expect(team(grid, session['id']).locator('.gap')).to_have_count(0)
        expect(grid.locator(f'.gap[data-shift="{session["id"]}"]')).to_be_visible()
        # An unpaired person moving out and back must never become a trainee by DOM placement.
        stream.event(adds=[(other['id'], people[3])], removes=[(session['id'], people[3])])
        moved = member(grid, other['id'], people[3])
        expect(moved).to_have_attribute('aria-label', re.compile('^' + re.escape(NAMES[3]) + ','))
        stream.event(adds=[(session['id'], people[3])], removes=[(other['id'], people[3])])
        expect(member(grid, session['id'], people[3])).to_have_attribute('aria-label', re.compile('^' + re.escape(NAMES[3]) + ','))
        expect(team(grid, session['id']).locator(f'[data-person="{people[3]}"]')).to_have_count(0)
        stream.event(adds=[(session['id'], people[1])])
        expect(member(grid, session['id'], people[1])).to_have_attribute('aria-label', re.compile('^' + re.escape(NAMES[1]) + ','))
        expect(grid.locator('.asl-caret')).to_have_count(0)
        expect(team(grid, session['id']).locator('.gap')).to_have_count(0)
        # Progress carries person IDs, not a new supervision relation; it may not fabricate one.
        expect(member(grid, session['id'], people[1])).not_to_have_attribute('aria-label', re.compile('supervising'))
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()
# Include this file in hosted supervision and team-lead qualification.

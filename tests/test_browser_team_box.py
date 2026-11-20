"""Supervision teams reviewed: the one person pill for everyone, the supervisor's
first in weight 600 with no badge, the trainees' one indent in with their rank inside the pill, joined by one straight
guide (View › Supervision teams: Tree, the default) or a hairline outline (Box); one line per person (ellipsis; the
full name in the tip and the group's accessible name), at most "Trainees per team" pills then "+N more". A problem is
the shared issue marker (issue_marker.ts) in the supervisor's slot: a triangle for must fix (Needs supervisor,
"3 · limit 2"), a circle for could improve (Unpaired, Not on this session), its words its accessible name, hover card
and popover (hover, keyboard, tap; one Fix or Why); no dashed outline and no words in a dense view, said once. Words
show only in the session panel and the person card. Checked in Week and Month at 1440 and 390, light and dark, and in
Day, List, the phone agenda, the session panel and the person card. Screenshots: TEAM_BOX_SHOTS (a folder) when set."""
from tests.historical_staffing import retained_staffing
from itertools import permutations
import os
from pathlib import Path
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, close_popover, live_server, navigate, open_session, pick_view, route_fetch, sign_in, show_names, view_settings  # noqa: F401
from .test_browser_rule_catalogue import flag

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
SUP, SUP2 = 'Fictional Consultant Alexandrina Northumberland', 'Fictional Bruno Jensen'
T1, T2 = 'Fictional Trainee Benedicta Featherstonehaugh', 'Fictional Trainee Constantinopoulos Winterbourne'
T3, T4, T5 = 'Fictional Trainee Dina Rami Al-Ottoline', 'Fictional Elena Reed', 'Fictional Pavel Lewis'
SOLO = 'Fictional Independent Quinn Montgomery-Smith'
RANK = {T1: 'FR2', T2: 'FR1', T3: 'FR1', T4: 'FR2', T5: 'FR2'}


def day(n, start='08:00', end='12:00'):
    return f'2026-10-0{4 + n}T{start}:00+03:00', f'2026-10-0{4 + n}T{end}:00+03:00'


def fixture(h):
    flag(h)
    ranks = {code: h.configure('categories', {'name': f'Fictional resident {code}', 'short_code': code, 'starts_on': '2020-01-01'})['record']['id']
             for code in ('FR1', 'FR2')}
    ids = {}
    for name in (SUP, SUP2, SOLO, T1, T2, T3, T4, T5):
        pid = ids[name] = h.configure('people', {'display_name': name})['record']['id']
        h.command('coordinator', 'membership.create', {'person_id': pid, 'starts_on': '2020-01-01', 'category_id': ranks[RANK[name]] if name in RANK else h.seed['category']})
        h.command('credentialer', 'eligibility.create', {'person_id': pid, 'service_id': h.seed['services'][0], 'starts_on': '2020-01-01'})
        if name in (SUP, SUP2):
            h.command('credentialer', 'grant.create', {'person_id': pid, 'capability_id': h.seed['supervisor_capability'],
                'starts_on': '2020-01-01', 'evidence': 'Synthetic supervision qualification'})

    def typ(name, limit, **extra):
        supervision = {'trainee_category_ids': list(ranks.values()), 'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': limit, **extra}
        return h.configure('assignment_types', {'name': name, 'starts_on': '2020-01-01',
            'definition': {'requirements': [{'minimum': 1}], 'supervision': supervision}})['record']['id']
    teaching, small, paired = typ('Fictional teaching clinic', 4), typ('Fictional team clinic', 2), typ('Fictional paired clinic', 2, require_pairing=True)

    def who(n, *people):
        a, b = day(n)
        return [{'person_id': ids[name], 'start': a, 'end': b, **({'participation': 'trainee', 'supervisor_person_id': ids[sup]} if sup
                 else {'participation': 'trainee'} if name in RANK else {'participation': 'supervisor'} if name != SOLO else {})} for name, sup in people]

    w = h.workspace('Fictional team box acceptance')
    for n, kind, people in [(0, teaching, [(SUP, None), (T1, SUP), (T2, SUP), (T3, SUP), (T4, SUP), (SOLO, None)]),  # four trainees
                            (1, small, [(SUP, None), (T1, SUP), (T2, SUP), (T3, SUP)]),   # over the limit of 2
                            (2, paired, [(T5, None)]),                                   # needs supervisor
                            (3, small, [(SUP2, None), (T3, None)]),                      # unpaired
                            (4, small, [(SUP2, None), (T2, SUP)])]:                      # supervisor elsewhere
        participants = who(n, *people)
        result = (retained_staffing(h, w, *day(n), typ=kind, people=participants) if n in (1, 2)
                  else h.shift(w, *day(n), typ=kind, people=participants))
        w = result['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    return w, ids


def shoot(node, name):
    if folder := os.getenv('TEAM_BOX_SHOTS'):
        Path(folder).mkdir(parents=True, exist_ok=True)
        node.screenshot(path=str(Path(folder) / f'{name}.png'))


def open_view(page, w, view, layout='Grid'):
    navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
    pick_view(page, view)
    switch = page.get_by_role('radiogroup', name='Calendar layout')
    if switch.count():
        switch.get_by_role('radio', name=layout, exact=True).click()
    show_names(page, 'full')
    expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)


def and_list(names):
    return ', '.join(names[:-1]) + ' and ' + names[-1]


def ranked(*names):
    return [f'{n} ({RANK[n]})' for n in names]


def named(scope, head, trainees, tail=''):
    """A team's group by its full accessible name, in whatever order the session lists the trainees."""
    words = '|'.join(re.escape(f'{head} supervising {and_list(list(p))}{tail}') for p in permutations(trainees))
    return scope.get_by_role('group', name=re.compile(f'^({words})$'))


def one_line(box):
    """Every person pill is one line (no wrapping), however long the name."""
    assert box.evaluate('''b=>[...b.querySelectorAll('.pchip')].every(r=>getComputedStyle(r).whiteSpace==='nowrap'
        && r.getBoundingClientRect().height<=30)'''), 'A person pill wraps'


def marker(box, severity, words, control=True):
    """A team's one issue marker (issue_marker.ts, D78) in its supervisor's slot: the severity's shape, no words drawn,
    and (as its own button) the full words as its accessible name."""
    mark = box.locator(':scope > .tm-sup > .iss-mark')
    expect(mark).to_have_count(1)
    expect(mark).to_have_class(re.compile(rf'\b{severity}\b'))
    expect(mark.locator(f'svg[data-icon="{"alert" if severity == "error" else "notice"}"]')).to_have_count(1)
    if control:
        expect(mark).to_have_attribute('aria-label', re.compile(re.escape(f'{"Must fix" if severity == "error" else "Could improve"}: {words}')))
    return mark


def assert_states(scope, limited, control=True):
    team = named(scope, SUP, ranked(T1, T2, T3, T4))
    expect(team).to_have_count(1)
    # No badge: the supervisor's pill comes first in weight 600. Pills are clean names reviewed: no rank prefix
    # unless the Person label adds it; the group's name still says each trainee's rank.
    expect(team.locator('.tm-badge, .wk-tag.sup, .wk-tag.tr')).to_have_count(0)
    assert int(team.locator('.tm-sup .pchip').evaluate('n=>getComputedStyle(n).fontWeight')) >= 600
    assert int(team.locator('.tm-tr .pchip').first.evaluate('n=>getComputedStyle(n).fontWeight')) < 600
    expect(team.locator('.pchip .p-rank')).to_have_count(0)
    if limited:
        expect(team.locator('.tm-tr:not(:has(.tm-more))')).to_have_count(3)
        more = team.locator('.tm-more')
        expect(more).to_have_text('+1')  # +N everywhere (DESIGN D8); the button's name says it in full
        if more.evaluate('n=>n.tagName') == 'BUTTON':
            expect(more).to_have_accessible_name(re.compile(r'^1 more trainee with .+: open the session$'))
    else:
        expect(team.locator('.tm-tr')).to_have_count(4)
    one_line(team)
    expect(team.locator('.iss-mark')).to_have_count(0)
    over = named(scope, SUP, ranked(T1, T2, T3), '; 3 · limit 2')
    needs = scope.get_by_role('group', name=f'Needs supervisor: {T5} (FR2)', exact=True)
    unpaired = scope.get_by_role('group', name=f'Unpaired: {T3} (FR1)', exact=True)
    away = scope.get_by_role('group', name=f'{T2} (FR1), with {SUP}, not on this session', exact=True)
    for box, severity, words in ((over, 'error', '3 · limit 2'), (needs, 'error', 'Needs supervisor'),
                                 (unpaired, 'warning', 'Unpaired'), (away, 'warning', 'Not on this session')):
        marker(box, severity, words, control)
        # Said once: the marker only. No dashed outline, no words in a dense view, no person mark repeating it.
        expect(box.locator('.iss-mark')).to_have_count(1)
        expect(box.locator('.iss-words, .tm-note')).to_have_count(0)
        assert box.evaluate("""n=>[n,...n.querySelectorAll('.pchip')].every(x=>getComputedStyle(x).borderTopStyle!=='dashed')"""), 'A dashed problem outline'
    # Over the limit: the marker sits on the supervisor; with no supervisor here, it heads the guide alone.
    expect(over.locator('.tm-sup .pchip.sup')).to_have_count(1)
    expect(needs.locator('.tm-sup .pchip')).to_have_count(0)
    expect(needs.locator('.tm-tr .pchip')).to_have_count(1)


def no_page_scroll(page):
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), 'The page scrolls sideways'


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')])
def test_week_and_month_draw_each_team_and_problem_quietly(browser, live_server, h, width, scheme):
    w, ids = fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900}, color_scheme=scheme)
    try:
        open_view(page, w, 'Week')
        grid = page.locator('#schedule-view .wk-grid')
        assert_states(grid, limited=True)
        team = named(grid, SUP, ranked(T1, T2, T3, T4))
        # Members keep their own chips (panel, menu, drag), named by the view's Person label on one line (ending in …),
        # the full name in their label; the view decides the label: Short name shows the short one.
        member = team.locator(draft_chip(ids[T1]))
        if member.count():
            expect(member).to_have_attribute('aria-label', re.compile(f'^{T1},'))
            expect(member.locator('.pchip-name')).to_have_text(T1)
            show_names(page, 'short')
            expect(member.locator('.pchip-name')).to_have_text('F. Featherstonehaugh')
            show_names(page, 'full')
        # The box says the supervision problem; no session pill repeats it, and no SUP/TR tag repeats a role.
        expect(grid.locator('.role-gap')).to_have_count(0)
        expect(grid.locator('.wk-tag.sup, .wk-tag.tr')).to_have_count(0)
        team.locator('.tm-more').click()
        expect(page.locator('.wk-panel .wk-session')).to_be_visible()
        page.keyboard.press('Escape')
        # The threshold is the view's own setting (View settings › Trainees per team, PlannerViewSettings.team_rows).
        rows = view_settings(page.locator('.planner-toolbar')).get_by_label('Trainees per team', exact=True)
        expect(rows).to_have_value('3')
        rows.fill('4'); rows.press('Enter')
        close_popover(page)
        expect(team.locator('.tm-tr')).to_have_count(4)
        expect(team.locator('.tm-more')).to_have_count(0)
        rows = view_settings(page.locator('.planner-toolbar')).get_by_label('Trainees per team', exact=True)
        rows.fill('3'); rows.press('Enter')
        close_popover(page)
        no_page_scroll(page)
        open_view(page, w, 'Month')
        assert_states(page.locator('#schedule-view .wk-grid'), limited=True)
        no_page_scroll(page)
    finally:
        context.close()


def test_a_saved_view_applies_and_shows_its_trainees_per_team(browser, live_server, h):
    """PlannerViewSettings.team_rows round-trips: a saved view's Trainees per team is applied when it opens and shown in
    View settings (an integration merge once dropped the client wiring, so the saved setting was ignored)."""
    w, _ = fixture(h)
    saved = h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Fictional four trainees',
        'definition': {'planner': {'mode': 'week', 'range': 'week', 'team_rows': 4}}})['view']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_view(page, w, 'Week')
        pick_view(page, saved['name'])
        team = named(page.locator('#schedule-view .wk-grid'), SUP, ranked(T1, T2, T3, T4))
        expect(team.locator('.tm-tr')).to_have_count(4)
        expect(team.locator('.tm-more')).to_have_count(0)
        rows = view_settings(page.locator('.planner-toolbar')).get_by_label('Trainees per team', exact=True)
        expect(rows).to_have_value('4')
        close_popover(page)
    finally:
        context.close()


def test_day_list_and_session_panel_use_the_same_group(browser, live_server, h):
    w, _ = fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_view(page, w, 'List')
        table = page.locator('#schedule-view')
        assert_states(table, limited=False)
        open_session(page, 'Fictional teaching clinic')
        panel = page.locator('.wk-panel .wk-session')
        expect(named(panel, SUP, ranked(T1, T2, T3, T4))).to_be_visible()
        page.keyboard.press('Escape')
        # The panel has room: the team's problem is said in words beside its marker.
        open_session(page, 'Fictional team clinic')
        over = named(page.locator('.wk-panel .wk-session'), SUP, ranked(T1, T2, T3), '; 3 · limit 2')
        expect(over.locator('.iss-line .iss-words')).to_have_text('3 · limit 2')
        page.keyboard.press('Escape')
        open_view(page, w, 'Day')
        bar = page.locator('.sd-bar').filter(has=page.locator('.team-box'))
        expect(bar).to_have_count(1)
        # The bar is the control: it says the team in words; the drawn group is not read twice.
        expect(bar.locator('.team-box')).to_have_attribute('aria-hidden', 'true')
        assert int(bar.locator('.tm-sup .pchip').evaluate('n=>getComputedStyle(n).fontWeight')) >= 600
        expect(bar.locator('.team-box button')).to_have_count(0)  # the bar is the one control
        expect(bar).to_have_attribute('aria-label', re.compile(f'{SUP} \\(supervising '))
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_phone_agenda_draws_teams_and_says_them(browser, live_server, h, scheme):
    w, _ = fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 390, 'height': 844}, color_scheme=scheme)
    try:
        open_view(page, w, 'Week', layout='Agenda')
        agenda = page.locator('.ag')
        expect(agenda.locator('.ag-teams .team-box')).to_have_count(5)
        item = agenda.locator('.ag-item').filter(has=page.locator('.team-box.tm-over'))
        # Inside the item (a control): the marker is drawn only; the item says the words.
        expect(item.locator('.team-box.tm-over .tm-sup > .iss-mark.error')).to_have_attribute('aria-hidden', 'true')
        expect(item.locator('.iss-words')).to_have_count(0)
        # Said once: the box, not also an open-place badge.
        expect(item.locator('.ag-open')).to_have_count(0)
        said = item.locator('.ag-teams .sr-only').text_content()
        assert said in {f'{SUP} supervising {and_list(list(p))}; 3 · limit 2' for p in permutations(ranked(T1, T2, T3))}, said
        no_page_scroll(page)
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_phone_agenda_keeps_an_open_place_the_team_box_does_not_explain(browser, live_server, h, scheme):
    """Said once, not hidden: the over-limit team's session is also one person short of its headcount (the check's
    places_open counts both). The box says the supervision shortfall; the badge says only the headcount's open place."""
    w, _ = fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 390, 'height': 844}, color_scheme=scheme)

    def short_by_one(route):
        reply = route_fetch(route, headers={k: v for k, v in route.request.headers.items() if k != 'if-none-match'})
        data = reply.json()
        for row in (data.get('workspace', {}).get('validation') or {}).get('coverage') or []:
            # One more person needed than the eligible headcount, on top of what the coverage already counts open.
            row.update(required=int(row.get('available_eligible') or 0) + 1, status='gap',
                       places_open=int(row.get('places_open') or 0) + 1)
        route.fulfill(status=200, json=data)
    try:
        page.route(re.compile(r'.*/api/workspaces/' + w['id'] + r'(/parts\?.*|\?.*)?$'), short_by_one)
        open_view(page, w, 'Week', layout='Agenda')
        item = page.locator('.ag .ag-item').filter(has=page.locator('.team-box.tm-over'))
        # D78: no words in a dense view. The box says the shortfall with its marker; the item says it in words, once.
        expect(item.locator('.team-box.tm-over .tm-sup > .iss-mark.error')).to_have_count(1)
        expect(item.locator('.ag-teams .sr-only')).to_contain_text('3 · limit 2')
        expect(item.locator('.ag-open')).to_have_text('1 open')
    finally:
        context.close()


@pytest.mark.skipif(not os.getenv('TEAM_BOX_SHOTS'), reason='screenshots only (TEAM_BOX_SHOTS)')
@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_screenshots(browser, live_server, h, scheme):
    """Week and Month at 1440 and 390 and the phone agenda, with no assertions, so the same data can be shot on any
    revision (before/after)."""
    w, _ = fixture(h)
    for width in (1440, 390):
        context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900}, color_scheme=scheme)
        try:
            for view in ('Week', 'Month'):
                open_view(page, w, view)
                grid = page.locator('#schedule-view .wk-grid')
                expect(grid).to_be_visible()
                shoot(grid, f'{view.lower()}-{width}-{scheme}')
            if width == 390:
                open_view(page, w, 'Week', layout='Agenda')
                expect(page.locator('.ag')).to_be_visible()
                shoot(page.locator('.ag'), f'agenda-390-{scheme}')
        finally:
            context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_person_card_today_team_is_the_same_quiet_group(browser, live_server, h, scheme):
    """The person card's today's duty draws its team with the same renderer: no badge, the supervisor in weight 600,
    the trainee's rank as a code (one derived from the rank's name when it has none; the name in the tip), and other
    people's names opening their card."""
    from .test_browser_person_card import TRAINEE, fixture as card_fixture, open_card
    card_fixture(h)
    rank = next(c['name'] for c in h.get('coordinator', '/api/catalogues')['categories'] if c['id'] == h.seed['trainee_category'])
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900}, color_scheme=scheme)
    try:
        panel = open_card(page, TRAINEE)
        team = panel.locator('.pc-duty .team-box')
        expect(team).to_have_count(1)
        expect(team).to_have_attribute('role', 'group')
        expect(team.locator('.tm-badge, .wk-tag')).to_have_count(0)
        assert int(team.locator('.tm-sup .pchip').evaluate('n=>getComputedStyle(n).fontWeight')) >= 600
        # The pill is the clean name; the rank, as configured (its short code, else the server's fallback), is in the group's name.
        expect(team.locator('.pchip .p-rank')).to_have_count(0)
        configured = next(c for c in h.get('coordinator', '/api/catalogues')['categories'] if c['id'] == h.seed['trainee_category'])
        expect(team).to_have_attribute('aria-label', re.compile(re.escape(f"({configured.get('short_code') or rank})")))
        one_line(team)
        expect(team.locator('.person-link')).to_have_count(1)  # the supervisor opens their card; the card's own person does not
        if os.getenv('TEAM_BOX_SHOTS'):
            shoot(page.get_by_role('region', name=TRAINEE, exact=True), f'person-card-1440-{scheme}')
    finally:
        context.close()


def issues_dialog(page):
    return page.get_by_role('dialog', name=re.compile('^Issues: '))


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_issue_marker_opens_on_hover_keyboard_and_tap(browser, live_server, h, width, scheme):
    """The one issue marker (D78): hover or focus shows its words, a click, Enter or a tap pins its popover with one
    action (Fix for must fix, Why for could improve), Esc or a tap outside closes it, and Fix opens the session."""
    w, _ = fixture(h)
    touch = width == 390
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900}, color_scheme=scheme,
                            **({'has_touch': True, 'is_mobile': True} if touch else {}))
    try:
        open_view(page, w, 'Week')
        grid = page.locator('#schedule-view .wk-grid')
        needs = marker(grid.get_by_role('group', name=f'Needs supervisor: {T5} (FR2)', exact=True), 'error', 'Needs supervisor')
        unpaired = marker(grid.get_by_role('group', name=f'Unpaired: {T3} (FR1)', exact=True), 'warning', 'Unpaired')
        box = needs.bounding_box()
        assert box['width'] >= 24 and box['height'] >= 24, box  # WCAG 2.2 SC 2.5.8
        if not touch:
            needs.hover()
            expect(page.locator('.hovercard')).to_contain_text('Needs supervisor')
            # Keyboard: the marker is a button; Enter pins the popover, Esc closes it and focus returns to it.
            needs.focus()
            page.keyboard.press('Enter')
            expect(issues_dialog(page)).to_contain_text('Needs supervisor')
            page.keyboard.press('Escape')
            expect(issues_dialog(page)).to_have_count(0)
            expect(needs).to_be_focused()
            unpaired.click()
        else:
            needs.tap()
            expect(issues_dialog(page)).to_contain_text('Needs supervisor')
            page.locator('#main .wk-nav strong').first.tap()  # the period's plain words: outside, and on no control
            expect(issues_dialog(page)).to_have_count(0)
            unpaired.tap()
        # Could improve: its one action is Why, not Fix.
        expect(issues_dialog(page).get_by_role('button', name='Why', exact=True)).to_have_count(1)
        expect(issues_dialog(page).get_by_role('button', name='Fix', exact=True)).to_have_count(0)
        page.keyboard.press('Escape')
        (needs.tap if touch else needs.click)()
        issues_dialog(page).get_by_role('button', name='Fix', exact=True).click()
        expect(page.locator('.wk-panel .wk-session')).to_be_visible()
    finally:
        context.close()


def supervision_layout(page, form):
    panel = view_settings(page.locator('.planner-toolbar'), 'Properties')
    panel.get_by_label('Show Supervision teams as', exact=True).select_option(form)
    close_popover(page)


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_supervision_teams_tree_or_box_is_a_view_setting(browser, live_server, h, scheme):
    """View › Properties › Supervision teams: Tree (the contract's first form, the default) joins the trainees with one
    straight neutral guide; Box draws a hairline outline. Same pills, same marker, one renderer."""
    w, _ = fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900}, color_scheme=scheme)
    try:
        open_view(page, w, 'Week')
        grid = page.locator('#schedule-view .wk-grid')
        team = named(grid, SUP, ranked(T1, T2, T3, T4))
        expect(team).to_have_class(re.compile(r'\btree\b'))
        guide = team.locator(':scope > .tm-kids').evaluate('n=>{const c=getComputedStyle(n);return [c.borderLeftStyle,c.borderLeftWidth,c.borderTopStyle]}')
        assert guide == ['solid', '1px', 'none'], guide
        assert team.evaluate('n=>getComputedStyle(n).borderTopStyle') == 'none'
        supervision_layout(page, 'box')
        expect(team).to_have_class(re.compile(r'(^|\s)box(\s|$)'))  # not \bbox\b: "team-box" matches that before the redraw
        assert team.evaluate('n=>[getComputedStyle(n).borderTopStyle,getComputedStyle(n).borderTopWidth]') == ['solid', '1px']
        assert team.locator(':scope > .tm-kids').evaluate('n=>getComputedStyle(n).borderLeftStyle') == 'none'
        assert_states(grid, limited=True)
        supervision_layout(page, 'tree')
        expect(team).to_have_class(re.compile(r'\btree\b'))
    finally:
        context.close()


def test_a_cells_first_line_alone_clears_the_selection_box(browser, live_server, h):
    """Only a Week cell's first line clears its selection box; the pills below take the cell's full width, and no pill
    leaves its cell (truncation guard: a long name ends in … inside its pill)."""
    w, _ = fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_view(page, w, 'Week')
        # filter(has=) is searched from the cell, so its locator cannot start at #schedule-view (the cell's ancestor).
        cell = page.locator('#schedule-view .wk-cell').filter(has=page.locator('.wk-check-hit')).filter(
            has=named(page, SUP, ranked(T1, T2, T3, T4))).first
        geometry = cell.evaluate('''c=>{const box=c.querySelector(':scope>.wk-check-hit').getBoundingClientRect();
          const first=c.querySelector(':scope>.wk-first'),head=first.matches('.team-box')?first.querySelector(':scope>.tm-row'):first;
          const kid=c.querySelector('.tm-tr .pchip').getBoundingClientRect();
          return {headRight:head.getBoundingClientRect().right,boxLeft:box.left,kidRight:kid.right};}''')
        assert geometry['headRight'] <= geometry['boxLeft'] + 0.5, geometry
        assert geometry['kidRight'] > geometry['boxLeft'], geometry
        assert page.evaluate('''()=>[...document.querySelectorAll('#schedule-view .wk-cell .pchip')].every(p=>{
          const c=p.closest('.wk-cell').getBoundingClientRect(),r=p.getBoundingClientRect(),n=p.querySelector('.pchip-name');
          return r.right<=c.right+0.5&&(!n||getComputedStyle(n).textOverflow==='ellipsis');})'''), 'A pill leaves its cell'
    finally:
        context.close()


def test_open_place_marker_and_header_roll_ups(browser, live_server, h):
    """A session's open-place issue sits beside its "1 open" place; the day header and the clinic row roll their
    sessions' issues up, and a roll-up lists the same words once with their sessions as chips, then "Open all in Fix list (N)"."""
    from packages.contracts.schemas import ISSUE_LIST_LIMIT
    w, ids = fixture(h)
    paired = next(t['id'] for t in h.get('coordinator', '/api/catalogues')['assignment_types'] if t['name'] == 'Fictional paired clinic')
    two = h.configure('assignment_types', {'name': 'Fictional two-place clinic', 'starts_on': '2020-01-01',
                                           'definition': {'requirements': [{'minimum': 2}]}})['record']['id']
    a, b = day(5, '13:00', '17:00')
    w = h.shift(w, a, b, typ=two, people=[{'person_id': ids[SOLO], 'start': a, 'end': b}])['workspace']
    # More trainees needing a supervisor in one row than a popover lists.
    for n in range(ISSUE_LIST_LIMIT + 1):
        a, b = day(n, '18:00', '20:00')
        w = retained_staffing(h, w, a, b, typ=paired, people=[{'person_id': ids[T4], 'start': a, 'end': b, 'participation': 'trainee'}])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_view(page, w, 'Week')
        grid = page.locator('#schedule-view .wk-grid')
        open_row = grid.locator('.wk-open-row').first
        expect(open_row.locator('.pchip.gap')).to_have_count(1)
        expect(open_row.locator('.iss-mark')).to_have_attribute('aria-label', re.compile('Fewer people than the minimum'))
        expect(grid.locator('.wk-day .iss-mark.error').first).to_be_visible()
        head = grid.locator('.wk-rowhead').filter(has_text='Fictional paired clinic').filter(has_text='18:00').locator('.iss-mark')
        expect(head).to_have_class(re.compile(r'\berror\b'))
        expect(head.locator('.iss-n')).to_have_text(re.compile(r'^\d+$'))
        head.click()
        dialog = issues_dialog(page)
        # The one issue list: the same words once, their sessions as chips (never one line per session), then Open all.
        expect(dialog.locator('.iss-row').first).to_be_visible()
        assert dialog.locator('.iss-row').count() <= ISSUE_LIST_LIMIT
        expect(dialog.locator('.fix-chips .fix-chip').first).to_be_visible()  # the one session-chip model (sessionChips)
        dialog.get_by_role('button', name=re.compile(r'^Open all in Fix list \(\d+\)$')).click()
        # FIXLIST-UX: on a desktop the Fix list is the Planner's side panel; the schedule stays.
        expect(page.locator('.dock-panel.fix-side')).to_be_visible()
        assert '#schedule/issues' not in page.url
    finally:
        context.close()


PILL_STYLE = "n=>{const c=getComputedStyle(n);return [c.fontSize,c.borderTopLeftRadius,c.paddingLeft,c.paddingRight,c.minHeight]}"


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_every_surface_draws_the_same_person_pill(browser, live_server, h, width, scheme):
    """One person pill (person_pill.ts, D78): the Week, the List, the session panel and the phone agenda draw a person
    with the same type size, radius, padding and height (the --pill-* tokens); only Month's compact density is lower."""
    w, _ = fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900}, color_scheme=scheme)
    try:
        open_view(page, w, 'Week')
        looks = {'week': page.locator('#schedule-view .wk-grid .team-box .pchip').first.evaluate(PILL_STYLE)}
        open_view(page, w, 'List')
        looks['list'] = page.locator('#schedule-view .pill-item .pchip').first.evaluate(PILL_STYLE)
        if width == 390:  # a phone's List is cards (table_view.ts cardOf), not the rows open_session clicks
            page.locator('#schedule-view .dt-card.dt-open').filter(has_text='Fictional teaching clinic').first.locator('.dt-card-title').click()
            expect(page.locator('.wk-panel .wk-session')).to_be_visible()
        else:
            open_session(page, 'Fictional teaching clinic')
        looks['panel'] = page.locator('.wk-panel .wk-session .team-box .pchip').first.evaluate(PILL_STYLE)
        page.keyboard.press('Escape')
        if width == 390:
            open_view(page, w, 'Week', layout='Agenda')
            looks['agenda'] = page.locator('.ag .ag-teams .pchip').first.evaluate(PILL_STYLE)
        assert len({tuple(v) for v in looks.values()}) == 1, looks
        assert looks['week'][-1] == '24px', looks  # --pill-h: the WCAG 2.2 SC 2.5.8 target
        open_view(page, w, 'Month')
        month = page.locator('#schedule-view .wk-grid .pchip').first.evaluate(PILL_STYLE)
        assert month[:4] == looks['week'][:4] and month[-1] == '20px', month  # --pill-h-compact, same pill otherwise
    finally:
        context.close()

"""The session panel's Why tab: groups with counts, search and filters, Allow once…, Assign with Undo, A person, and the
schedule ringing what opened the panel (desktop and phone; coordinators act, approvers only read)."""
import re

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, pick_view, open_rota, navigate, view_settings, choose_radio, choose_layout, close_popover  # noqa: F401
from .test_planner_why import why
from .rules_support import rule_row

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
PHONE = {'width': 390, 'height': 844}


def rota(h, exception=False):
    """An open evening session after a long day (a rest rule stops that person), someone on leave, everyone else free."""
    rest = h.policy({'kind': 'max_hours', 'hours': 12, 'window': 'day'} if exception else {'kind': 'min_rest', 'hours': 12}, exception=exception)
    tired, away = h.seed['people'][:2]
    w = h.workspace('Why tab', '2026-10-04', '2026-10-11')
    w = h.shift(w, '2026-10-04T08:00:00+03:00', '2026-10-04T20:00:00+03:00', people=[tired])['workspace']
    w = h.shift(w, '2026-10-04T22:00:00+03:00', '2026-10-05T06:00:00+03:00', people=[], location=1)['workspace']
    h.command('coordinator', 'availability.record', {'person_id': away, 'kind': 'recorded_leave', 'start': '2026-10-04T00:00:00+03:00',
                                                     'end': '2026-10-06T00:00:00+03:00', 'private_reason': 'Private leave reason'})
    w = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']
    night = next(s for s in h.detail(w)['shifts'] if not s['participants'])
    return w, night, rest


def open_why(page, w, phone=False):
    navigate(page, 'planner')
    open_rota(page, w['id'])
    expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)
    if phone:
        pick_view(page, 'Coverage')
        page.locator('.cov-cell.short').first.click()
    else:
        pick_view(page, 'Week')
        page.locator('button.pchip.gap').first.click()
    panel = page.locator('.wk-panel')
    panel.get_by_role('tab', name='Why', exact=True).click()
    expect(panel.locator('.why .wk-loading')).to_have_count(0)
    return panel


def test_why_tab_explains_everyone_and_acts_with_undo(browser, live_server, h):
    w, night, rest = rota(h)
    tired, away = h.seed['people'][:2]
    expected = why(h, w, [night['id']])['sessions'][0]['rows']
    names = {r['person_id']: r['display_name'] for r in expected}
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        panel = open_why(page, w)
        # What opened the panel is ringed on the grid; the panel header says so (no coloured edge stripe).
        # One highlight (dom.ts panelHighlight): the session's cell, not also the chip that opened it.
        expect(page.locator('.is-explained')).to_have_count(1)
        expect(page.locator(f'.wk-cell.is-explained[data-session~="{night["id"]}"]')).to_have_count(1)
        assert page.locator('.wk-cell.is-explained').evaluate("n => getComputedStyle(n, '::before').content") == '"Open in panel"'
        # The ring says it; the panel header does not repeat "highlighted on the schedule" (frontend audit S-10).
        expect(panel.locator('.wk-panel-head')).not_to_contain_text('highlighted on the schedule')
        # Every group with its count, as the API sorts them; leave never says why.
        for group, label in [('free', 'Could work here'), ('rule', 'Blocked by a rule'), ('unavailable', 'Unavailable')]:
            n = sum(r['group'] == group for r in expected)
            expect(panel.get_by_role('button', name=re.compile(rf'^{label} {n}$'))).to_be_visible()
            expect(panel.get_by_role('region', name=f'{label} ({n})')).to_be_visible()
        expect(panel.get_by_role('button', name=re.compile(rf'^All {len(expected)}$'))).to_have_attribute('aria-pressed', 'true')
        blocked = panel.get_by_role('region', name=re.compile(r'^Blocked by a rule')).locator('.why-row').filter(has_text=names[tired])
        expect(blocked).to_contain_text(rest['name'])
        expect(blocked).to_contain_text('2 h rest (needs 12 h)')
        expect(panel.get_by_role('region', name=re.compile(r'^Unavailable'))).to_contain_text(names[away])
        expect(panel).not_to_contain_text('Private leave reason')
        # The pill says the leave type; the words beside it say the dates (not the type again).
        away_row = panel.get_by_role('region', name=re.compile(r'^Unavailable')).locator('.why-row').filter(has_text=names[away])
        expect(away_row.locator('.why-text')).to_have_text(re.compile(r'^Other 4–5 Oct'))
        # Search and filter narrow the list; an empty result says so.
        panel.get_by_label('Search people', exact=True).fill(names[tired])
        expect(panel.locator('.why-row')).to_have_count(1)
        panel.get_by_role('button', name=re.compile(r'^Unavailable 0$')).click()
        expect(panel.get_by_text('Nobody matches.', exact=False)).to_be_visible()
        # No dead end: the empty result's one action clears both the search and the group.
        panel.get_by_role('button', name='Show everyone', exact=True).click()
        expect(panel.get_by_label('Search people', exact=True)).to_have_value('')
        expect(panel.get_by_role('button', name=re.compile(r'^All '))).to_have_attribute('aria-pressed', 'true')
        # Safety findings cannot offer a person exception.
        expect(blocked.get_by_role('button', name=re.compile(r'^Allow once'))).to_have_count(0)
        # Assign someone free: the draft changes, the panel stays on Why with them assigned, and Undo takes them off.
        free = next(r for r in expected if r['group'] == 'free')
        panel.get_by_role('button', name=f'Assign {free["display_name"]}', exact=True).click()
        assigned = panel.get_by_role('region', name=re.compile(r'^Assigned'))
        expect(assigned).to_contain_text(free['display_name'])
        expect(assigned.get_by_role('button', name=f'Remove {free["display_name"]}')).to_be_visible()
        assert [p['person_id'] for s in h.detail(w)['shifts'] if s['id'] == night['id'] for p in s['participants']] == [free['person_id']]
        page.locator('.toast').get_by_role('button', name='Undo').click()
        expect(panel.get_by_role('region', name=re.compile(r'^Assigned'))).to_have_count(0)
        assert not next(s for s in h.detail(w)['shifts'] if s['id'] == night['id'])['participants']
        # A person: their row on each session in view, their duties ringed instead of the session.
        panel.get_by_role('radio', name='A person', exact=True).click()
        panel.get_by_label('Person', exact=True).select_option(tired)
        expect(panel.locator('.why .wk-loading')).to_have_count(0)
        expect(panel.locator('.why-row')).to_have_count(2)
        expect(panel.get_by_role('region', name=re.compile(r'^Assigned'))).to_contain_text('08:00–20:00')
        expect(panel.get_by_role('region', name=re.compile(r'^Blocked by a rule'))).to_contain_text('22:00–06:00')
        expect(page.locator('.wk-cell.is-explained')).to_have_count(0)
        expect(page.locator('.is-explained')).to_have_count(1)
        expect(page.locator(f'.pchip.is-explained[data-person~="{tired}"]')).to_have_count(1)
        # Rules: an empty session has no person on it for a person's rule (rest) to reach; the session is ringed again.
        panel.get_by_role('tab', name='Rules', exact=True).click()
        expect(panel.get_by_text('No rule applies to this session.', exact=True)).to_be_visible()
        expect(page.locator('.wk-cell.is-explained')).to_have_count(1)
        # Every view keeps what the panel explains highlighted.
        for view, node in [('People', None), ('Coverage', 'BUTTON'), ('List', 'TR'), ('Timeline', None)]:
            if view == 'Timeline':
                choose_layout(view_settings(page.locator('.planner-toolbar')), 'Timeline')
                close_popover(page)
            else:
                pick_view(page, view)
            expect(page.locator('.wk-panel')).to_be_visible()
            marked = page.locator(f'.is-explained[data-session~="{night["id"]}"]')
            expect(marked).to_have_count(1 if node else 0)  # nobody works the empty session: no chip to mark
            if node:
                expect(marked).to_have_js_property('tagName', node)
        # A person's duties in the People view, and closing the panel removes every highlight.
        panel.get_by_role('tab', name='Why', exact=True).click()
        pick_view(page, 'People')
        # Still highlighted once every read has answered: the render for the layout chosen first (a month, its academic
        # calendar read late) is stale and must not take the highlight from the view on screen (the CI flake on maintenance).
        page.wait_for_load_state('networkidle')
        expect(page.locator(f'.pg-chip.is-explained[data-person~="{tired}"]')).to_have_count(1)
        page.get_by_role('button', name='Close panel', exact=True).click()
        expect(page.locator('.wk-panel')).to_have_count(0)
        expect(page.locator('.is-explained')).to_have_count(0)
    finally:
        context.close()



def test_a_highlighted_person_is_ringed_on_their_published_chips_too(browser, live_server, h):
    """One person reads the same everywhere: the published read-only chips around a draft (.sd-greychip) carry the
    person's data-person like a draft chip, so Why › A person rings that person's published duties as well as the
    draft's (planner_week highlightFocus), and names them current for screen readers."""
    tired = h.seed['people'][0]
    pub = h.shift(h.workspace('Fictional published day', '2026-10-04', '2026-10-05'),
                  '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00', people=[tired])['workspace']
    h.publish(pub)
    w = h.shift(h.workspace('Fictional draft after it', '2026-10-05', '2026-10-07'),
                '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[tired])['workspace']
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[])['workspace']
    w = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        panel = open_why(page, w)
        published = page.locator(f'#schedule-view .wk-grid .pchip.sd-greychip[data-person~="{tired}"]')
        expect(published).to_have_count(1)
        expect(published).not_to_have_class(re.compile(r'\bis-explained\b'))
        panel.get_by_role('radio', name='A person', exact=True).click()
        panel.get_by_label('Person', exact=True).select_option(tired)
        expect(published).to_have_class(re.compile(r'\bis-explained\b'))
        expect(published).to_have_attribute('aria-current', 'true')
        expect(page.locator(f'#schedule-view {draft_chip(tired, word=True)}.is-explained')).to_have_count(1)
    finally:
        context.close()

def test_why_tab_reads_only_for_approvers_and_is_a_sheet_on_phones(browser, live_server, h):
    w, night, rest = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=PHONE)
    try:
        panel = open_why(page, w, phone=True)
        box = panel.bounding_box()
        assert box and box['width'] >= 389 and box['y'] > 844 * 0.3, box  # a bottom sheet over the schedule
        expect(page.locator('.ag-item.is-explained')).to_have_count(1)
        expect(panel.get_by_role('button', name=re.compile(r'^Assign '))).not_to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        pick_view(page, 'Week')
        page.locator('button.pchip.gap').first.click()
        panel = page.locator('.wk-panel')
        panel.get_by_role('tab', name='Why', exact=True).click()
        expect(panel.locator('.why .wk-loading')).to_have_count(0)
        expect(panel.get_by_role('region', name=re.compile(r'^Blocked by a rule'))).to_contain_text(rest['name'])
        expect(panel.get_by_role('button', name=re.compile(r'^(Assign |Remove |Allow once)'))).to_have_count(0)
        # The day session's Rules tab lists the rest rule that reaches the person on it (the rules panel's rows).
        page.get_by_role('button', name='Close panel', exact=True).click()
        page.locator('button.pchip:not(.gap)').first.click()
        panel.get_by_role('tab', name='Rules', exact=True).click()
        # A row says the rule's sentence, not its name (CONTRACT §10): the row is found by its rule (data-rk-name).
        expect(rule_row(panel.get_by_label('Rules for this session'), rest['name'])).to_be_visible()
    finally:
        context.close()


def test_allow_once_for_an_exception_permitting_limit_prefills_the_real_rule(browser, live_server, h):
    w, _, limit = rota(h, exception=True)
    person = h.seed['people'][0]
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        panel = open_why(page, w)
        blocked = panel.get_by_role('region', name=re.compile(r'^Blocked by a rule')).locator('.why-row').filter(has_text=names[person])
        blocked.get_by_role('button', name=f'Allow once… {names[person]}').click()
        dialog = page.get_by_role('dialog', name='Request a rule exception')
        expect(dialog.get_by_label('Exception-permitting rule')).to_have_value(limit['id'])
        expect(dialog.get_by_label('Person', exact=True)).to_have_value(person)
        expect(dialog.get_by_label('Exception starts')).to_have_value('Sun 4 Oct 2026')
        page.keyboard.press('Escape')
        expect(dialog).to_have_count(0)
    finally:
        context.close()


def test_why_names_every_score_part_and_the_next_best_person(browser, live_server, h):
    """Each score part a placement moves, signed, in the score panel's words, and in "A person" the next-best person for
    the place they hold with the server's uncapped point difference (POST /api/planner/why with runner_up)."""
    tired = h.seed['people'][0]
    # Their wish to work that day sets them apart from everyone else who could take their place.
    h.command('staff', 'wish.create', {'person_id': tired, 'first_day': '2026-10-04', 'last_day': '2026-10-04', 'kind': 'prefer_on', 'points': 3})
    w, night, _ = rota(h)
    day = next(s for s in h.detail(w)['shifts'] if s['participants'])
    labels = {'coverage': 'Coverage', 'rules': 'Rules & targets', 'preferences': 'Preferences', 'fairness': 'Fairness',
              'stability': 'Stability', 'other': 'First assignment', 'gate': 'Publication cap'}
    signed = lambda n: ('+' if n > 0 else '−' if n < 0 else '±') + f'{abs(n):g}'
    free = next(r for r in why(h, w, [night['id']])['sessions'][0]['rows'] if r['group'] == 'free' and r.get('effect'))
    parts = ' · '.join(f"{labels[c]} {signed(free['effect'][c])}" for c in labels if free['effect'][c])
    assert parts, free['effect']
    session = why(h, w, [day['id']], person_id=tired, runner_up=True)['sessions'][0]
    best = session['runner_up']
    assert best, session['score_status']
    d = best['uncapped_point_difference']
    assert d > 0, best  # the draft is capped (an empty night): only the uncapped difference tells the two apart
    n = f"{abs(d):g} point{'' if abs(d) == 1 else 's'}"
    words = f"Next best: {best['display_name']}, {n} lower"
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        panel = open_why(page, w)
        row = panel.get_by_role('region', name=re.compile(r'^Could work here')).locator('.why-row').filter(has_text=free['display_name'])
        expect(row).to_contain_text(parts)
        panel.get_by_role('radio', name='A person', exact=True).click()
        panel.get_by_label('Person', exact=True).select_option(tired)
        expect(panel.locator('.why .wk-loading')).to_have_count(0)
        assigned = panel.get_by_role('region', name=re.compile(r'^Assigned'))
        expect(assigned.locator('.why-runner-up')).to_have_text(f' · {words}')
        # Only the place they hold names a next best: not the night a rule keeps them from.
        expect(panel.get_by_role('region', name=re.compile(r'^Blocked by a rule')).locator('.why-runner-up')).to_have_count(0)
    finally:
        context.close()


def test_first_optional_assignment_and_removal_explain_the_score_in_why_and_hover(browser, live_server, h):
    from packages.domain.score import COMPONENTS

    person = h.seed['people'][0]
    typ = h.configure('assignment_types', {'name': 'Optional Why score fixture', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'maximum': 1}]}})['record']
    w = h.workspace('Why first assignment', '2026-10-04', '2026-10-05')
    w = h.shift(w, typ=typ['id'], people=[person])['workspace']
    session, = h.detail(w)['shifts']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        pick_view(page, 'Week')
        chip = page.locator(draft_chip(person))
        chip.hover()
        hover = page.get_by_role('tooltip').get_by_role('list', name='Why here')
        expect(hover).to_contain_text('Score +100 with them here')
        expect(hover).to_contain_text('First assignment +100')
        expect(hover).not_to_contain_text('Every part of the score is the same either way')
        page.mouse.move(700, 20)
        chip.click()
        panel = page.locator('.wk-panel')
        panel.get_by_role('tab', name='Why', exact=True).click()
        # Why always compares with the person against without, including the row offering Remove.
        for assigned, action in ((True, 'Remove'), (False, 'Assign'), (True, None)):
            current = h.detail(w)
            assert [p['person_id'] for s in current['shifts'] for p in s['participants']] == ([person] if assigned else [])
            response = why(h, current['workspace'], [session['id']], person_id=person)['sessions'][0]
            assert response['score_status'] == 'complete'
            api_row, = response['rows']
            assert api_row['group'] == ('assigned' if assigned else 'free')
            effect = api_row['effect']
            assert effect['points'] == effect['other'] == 100
            assert effect['score_with'] == 100 and effect['score_without'] == 0
            assert all(effect[key] == 0 for key in (*COMPONENTS, 'gate'))
            assert round(sum(effect[key] for key in (*COMPONENTS, 'other', 'gate')), 1) == effect['points']
            group = '^Assigned' if assigned else '^Could work here'
            row = panel.get_by_role('region', name=re.compile(group)).locator('.why-row').filter(has_text=api_row['display_name'])
            expect(row).to_contain_text('First assignment +100')
            expect(row).not_to_contain_text('Every part of the score is the same either way')
            if action:
                row.get_by_role('button', name=f'{action} {api_row["display_name"]}', exact=True).click()
                next_action = 'Assign' if assigned else 'Remove'
                expect(panel.get_by_role('button', name=f'{next_action} {api_row["display_name"]}', exact=True)).to_be_visible()
        page.get_by_role('button', name='Close panel', exact=True).click()
        chip.hover()
        expect(hover).to_contain_text('Score +100 with them here')
        expect(hover).to_contain_text('First assignment +100')
        expect(hover).not_to_contain_text('Every part of the score is the same either way')
    finally:
        context.close()


def test_hover_then_a_person_still_names_the_next_best_person(browser, live_server, h):
    """With one session in view, the hover card's Why here asks for the same person and session as "A person" but without
    the runner-up: the person tab must still ask for it, not reuse the hover answer."""
    tired = h.seed['people'][0]
    h.command('staff', 'wish.create', {'person_id': tired, 'first_day': '2026-10-04', 'last_day': '2026-10-04', 'kind': 'prefer_on', 'points': 3})
    w = h.workspace('Why hover', '2026-10-04', '2026-10-05')
    w = h.shift(w, '2026-10-04T08:00:00+03:00', '2026-10-04T20:00:00+03:00', people=[tired])['workspace']
    w = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']
    day = h.detail(w)['shifts'][0]
    best = why(h, w, [day['id']], person_id=tired, runner_up=True)['sessions'][0]['runner_up']
    assert best and best['uncapped_point_difference'] > 0, best
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        pick_view(page, 'Week')
        chip = page.locator('.pchip:not(.gap)').first
        chip.hover()
        expect(page.get_by_role('tooltip').get_by_role('list', name='Why here')).to_contain_text('with them here')
        page.mouse.move(700, 20)
        chip.click()
        panel = page.locator('.wk-panel')
        panel.get_by_role('tab', name='Why', exact=True).click()
        panel.get_by_role('radio', name='A person', exact=True).click()
        panel.get_by_label('Person', exact=True).select_option(tired)
        expect(panel.locator('.why .wk-loading')).to_have_count(0)
        expect(panel.get_by_role('region', name=re.compile(r'^Assigned')).locator('.why-runner-up')).to_contain_text(f"Next best: {best['display_name']}")
    finally:
        context.close()


def test_next_best_says_when_only_they_clear_the_publication_cap(browser, live_server, h):
    """The person in view breaks a mandatory rest rule but has a wish for the night, so before the cap they score
    better than the next best, who would clear the cap: the line says so instead of only "N points lower"."""
    from .test_staff_wishes import department_settings
    # Isolate the preference/cap comparison from the department's workload fairness defaults.
    h.command('admin', 'department.settings', {**department_settings(h), 'generation_defaults': {'scoring': {'weights':
              {'coverage': 2, 'rules': 0, 'fairness': 0, 'preferences': 1, 'stability': 0}}}},
              h.seed['department_id'], h.get('admin', '/api/me')['organisation']['revision'])
    night_type = h.configure('assignment_types', {'name': 'Synthetic night clinic', 'starts_on': '2020-01-01',
                            'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    h.policy({'kind': 'min_rest', 'hours': 12})
    tired = h.seed['people'][0]
    # Both duties start on 4 Oct: a generic day-on wish would also be met by the day duty alone.
    h.command('staff', 'wish.create', {'person_id': tired, 'first_day': '2026-10-04', 'last_day': '2026-10-04',
              'kind': 'prefer_shift_type', 'assignment_type_id': night_type, 'points': 10})
    w = h.workspace('Why cap', '2026-10-04', '2026-10-06')
    w = h.shift(w, '2026-10-04T08:00:00+03:00', '2026-10-04T20:00:00+03:00', people=[tired])['workspace']
    w = h.shift(w, '2026-10-04T22:00:00+03:00', '2026-10-05T06:00:00+03:00', people=[tired], location=1, typ=night_type)['workspace']
    w = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']
    night = sorted(h.detail(w)['shifts'], key=lambda s: s['start'])[1]
    best = why(h, w, [night['id']], person_id=tired, runner_up=True)['sessions'][0]['runner_up']
    assert best['point_difference'] < 0 < best['uncapped_point_difference'], best
    d = best['uncapped_point_difference']
    words = f"Next best: {best['display_name']}, would clear the publication cap; otherwise {d:g} point{'' if d == 1 else 's'} lower"
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        pick_view(page, 'Week')
        page.locator('.pchip:not(.gap)').first.click()
        panel = page.locator('.wk-panel')
        panel.get_by_role('tab', name='Why', exact=True).click()
        panel.get_by_role('radio', name='A person', exact=True).click()
        panel.get_by_label('Person', exact=True).select_option(tired)
        expect(panel.locator('.why .wk-loading')).to_have_count(0)
        expect(panel.get_by_role('region', name=re.compile(r'^Assigned')).locator('.why-runner-up').filter(has_text=words)).not_to_have_count(0)
    finally:
        context.close()


def test_why_shows_only_groups_that_hold_someone_and_one_primary(browser, live_server, h):
    """G-43: no chip for an empty group; one primary Assign (the best fit), the others secondary; how people are compared
    is help on demand, not a paragraph."""
    w, night, _ = rota(h)
    expected = why(h, w, [night['id']])['sessions'][0]['rows']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        panel = open_why(page, w)
        chips = panel.locator('.why-chip')
        held = {r['group'] for r in expected}
        expect(chips).to_have_count(1 + len(held))
        expect(panel.get_by_role('button', name=re.compile(r'^Assigned \d+$'))).to_have_count(0 if 'assigned' not in held else 1)
        free = panel.get_by_role('region', name=re.compile(r'^Could work here')).get_by_role('button', name=re.compile('^Assign '))
        expect(free.first).to_have_class(re.compile(r'\bprimary\b'))
        if free.count() > 1:
            expect(free.nth(1)).to_have_class(re.compile(r'\bsecondary\b'))
        expect(panel.locator('.why-list .button.primary')).to_have_count(1)
        basis = panel.locator('details.wk-basis')
        expect(basis.locator('summary')).to_have_text('How people are compared')
        expect(basis.locator('p')).to_be_hidden()
    finally:
        context.close()


def test_blocked_by_safety_rule_says_no_exception(browser, live_server, h):
    """SR-21: in Why, a person a rule blocks that takes no exception says so, not an empty cell that reads as loading."""
    blocked = h.seed['people'][1]
    h.policy({'kind': 'restriction'}, {'person_id': blocked})
    w = h.shift(h.workspace('Why no exception rota'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.locator('.wk-grid button.pchip:not(.gap)').first.click()
        panel = page.locator('.wk-panel')
        panel.get_by_role('tab', name='Why', exact=True).click()
        row = panel.get_by_role('region', name=re.compile(r'^Blocked by a rule')).locator('.why-row').first
        expect(row).to_contain_text('No exception allowed', timeout=20000)
    finally:
        context.close()

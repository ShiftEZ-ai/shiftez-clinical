"""maintenance (SWEEP-3 findings left open by maintenance to maintenance): regressions, one or more per finding."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, keyboard_focus, wait_held  # noqa: F401
from .test_browser import whole_list
from .test_browser import fix_pill
from .test_browser_fe_c4 import my_duties, PHONE, DESKTOP
from .test_browser_schedule import show

pytestmark = pytest.mark.browser
SIZES = [(DESKTOP, 'light'), (DESKTOP, 'dark'), (PHONE, 'light'), (PHONE, 'dark')]
LINES = "n => {const r=document.createRange();r.selectNodeContents(n);return new Set([...r.getClientRects()].map(b=>Math.round(b.top))).size}"


@pytest.mark.parametrize('viewport,scheme', SIZES)
def test_a_doctors_coming_up_keeps_each_clinic_on_one_line(browser, live_server, h, viewport, scheme):
    """SW3-C-14: on a desktop Home, Coming up was a 340 px column where every clinic name wrapped to two or three lines
    beside a mostly empty main column."""
    my_duties(h)
    clinic = next(s['name'] for s in h.get('coordinator', '/api/catalogues')['services'] if s['id'] == h.seed['services'][0])
    context, page = sign_in(browser, live_server, 'staff', viewport, color_scheme=scheme)
    try:
        navigate(page, 'home')
        clinics = page.locator('.home-duty-list .home-clinic')
        expect(clinics.first).to_be_visible()
        assert clinics.count() >= 2
        expect(clinics).to_have_text([clinic] * clinics.count())
        if viewport['width'] >= 500:
            assert clinics.evaluate_all(f'ns => ns.map({LINES})') == [1] * clinics.count()
        else:
            assert clinics.evaluate_all('ns=>ns.every(n=>n.scrollWidth<=n.clientWidth && n.scrollHeight<=n.clientHeight)')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        context.close()


def grid(page, view, day):
    show(page, f'schedule/{view}/{day}')
    switch = page.get_by_role('radio', name='Grid', exact=True)
    if switch.count() and switch.get_attribute('aria-checked') != 'true':
        switch.click()
    found = page.locator('#schedule-view .wk-grid')
    expect(found).to_be_visible()
    return found


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_a_phone_month_of_my_duties_fits_with_codes_and_the_day_grid_says_each_fact_once(browser, live_server, h, scheme):
    """SW3-C-17: on a phone the Month grid was 904 px wide in a 358 px box (about 1.5 days in view), a Day/Week row's
    chip repeated its row's time and place, and the key's "Change what is shown" sat half off-screen when the key was long."""
    me, days, ids = my_duties(h)
    context, page = sign_in(browser, live_server, 'staff', PHONE, color_scheme=scheme)
    try:
        month = grid(page, 'month', days[0].isoformat())
        box = month.evaluate('g => [g.getBoundingClientRect().width, g.parentElement.clientWidth]')
        assert box[0] <= box[1] + 1, box
        chips = month.locator('.pchip-name')
        expect(chips).to_have_text(['CRS'] * 3)  # Clinical review session: its code, the session's facts in its label
        assert 'Clinical review session' in month.locator('.pchip').first.get_attribute('aria-label')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        day = grid(page, 'day', days[0].isoformat())
        row = day.locator('.wk-rowhead').first
        expect(row).to_contain_text('13:00–16:00')
        expect(row).to_contain_text('North consulting room')
        # The chip's time-and-place label stays whatever the row shows (S-12, C4-F03 in test_browser_schedule_views).
        expect(day.locator('.pchip-name')).to_have_text(['13:00–16:00 · North consulting room'])
        # A long key wraps onto the lines it needs, never scrolling or hiding an entry (eb8635b6d, UIUX-AUDIT L1); its
        # settings button stays in view.
        key = page.locator('.view-legend:visible').last
        key.evaluate("k => {for (let i = 0; i < 4; i++) k.prepend(Object.assign(document.createElement('li'), {textContent: 'A long key entry ' + i}));}")
        laid = key.evaluate('''k => {const b = k.getBoundingClientRect(), items = [...k.children].filter(n => n.getClientRects().length);
            return {scrolls: k.scrollWidth > k.clientWidth + 0.5 || k.scrollHeight > k.clientHeight + 0.5,
                    lines: new Set(items.map(n => Math.round(n.getBoundingClientRect().top))).size,
                    outside: items.filter(n => {const r = n.getBoundingClientRect();
                        return r.left < b.left - 0.5 || r.right > b.right + 0.5 || r.bottom > b.bottom + 0.5 || r.right > innerWidth;}).map(n => n.textContent)};}''')
        assert not laid['scrolls'] and laid['lines'] > 1 and not laid['outside'], laid
        settings = key.get_by_role('button', name='Change what is shown', exact=True)
        assert settings.evaluate('b => {const r=b.getBoundingClientRect(),k=b.closest(".view-legend").getBoundingClientRect();return r.left>=k.left && r.right<=k.right+0.5 && r.right<=innerWidth}')
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_a_desktop_grid_of_my_duties_says_when_and_where_only_where_the_row_does_not(browser, live_server, h, scheme):
    """SW3-C-17 (desktop): my own duty's chip says when and where on a Week and a Month (its label, S-12: row details
    add information beside it, never remove it; C4-F03 in test_browser_schedule_views)."""
    me, days, ids = my_duties(h)
    context, page = sign_in(browser, live_server, 'staff', DESKTOP, color_scheme=scheme)
    try:
        week = grid(page, 'week', days[0].isoformat())
        expect(week.locator('.pchip-name').first).to_have_text('13:00–16:00 · North consulting room')
        month = grid(page, 'month', days[0].isoformat())
        expect(month.locator('.pchip-name').first).to_have_text('13:00–16:00 · North consulting room')
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1280, 'light'), (1280, 'dark'), (1440, 'light')])
def test_a_reviewers_pinned_actions_never_cover_status(browser, live_server, h, width, scheme):
    """SW3-C-21: at 1280 a reviewer's table (a widened Type column, Approve and Decline in the pinned actions column)
    scrolled sideways and the pinned actions covered Status ("Appro…"). The decisions stack in a narrower list."""
    from .test_browser_requests import scale_requests, leave, day
    scale_requests(h)
    for n in range(3):
        leave(h, on=day(n))
    context, page = sign_in(browser, live_server, 'approver', {'width': width, 'height': 800}, color_scheme=scheme)
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        expect(page.locator('#main .dt-table td.dt-actions-col .button').first).to_be_visible()
        page.evaluate("""() => {const k = Object.keys(localStorage).find(k => k.startsWith('rota.table.') && k.endsWith('requests'));
            const v = JSON.parse(localStorage.getItem(k)); v.widths = {type: 360}; localStorage.setItem(k, JSON.stringify(v));}""")
        page.reload()
        actions = page.locator('#main .dt-table td.dt-actions-col').filter(has=page.get_by_role('button', name='Approve', exact=True))
        expect(actions.first).to_be_visible()
        rows = page.locator('#main .dt-table tbody tr').filter(has=page.get_by_role('button', name='Approve', exact=True))
        overlap = rows.evaluate_all("""rs => rs.slice(0, 10).map(r => {const s = r.querySelector('td[data-col=status]').getBoundingClientRect(),
            a = r.querySelector('td.dt-actions-col').getBoundingClientRect(); return Math.max(0, s.right - a.left);})""")
        assert overlap and max(overlap) < 1, overlap
        badge = page.locator('#main .dt-table td[data-col=status] .badge').first
        assert badge.evaluate('b => b.scrollWidth <= b.clientWidth')  # "Waiting for approval", not "Waiting…"
    finally:
        context.close()


@pytest.mark.parametrize('viewport,scheme', SIZES)
def test_help_shows_on_keyboard_focus_and_not_on_focus_the_app_sets(browser, live_server, h, viewport, scheme):
    """SW3-C-16: help tooltips appeared on focus the app set at load or handed back after a tapped popover
    (:focus-visible alone matches it), covering the period navigation. Help on focus follows keyboard input only."""
    my_duties(h)
    context, page = sign_in(browser, live_server, 'staff', viewport, color_scheme=scheme)
    held = []
    try:
        # The published days' live coverage redraws the Schedule when it lands (app.ts loadLiveCoverage): held here, so the
        # readings below are taken on the button as drawn, and released once that button has keyboard focus.
        page.route('**/api/publications/*/coverage', lambda route: held.append(route))
        navigate(page, 'schedule/week/2026-10-05')
        wait_held(page, held)
        target = page.locator('#main button.dt-tool').and_(page.get_by_role('button', name=re.compile('^View settings')))
        expect(target).to_be_visible()
        tip = target.get_attribute('data-tip')
        assert tip
        # The app moves focus (no key pressed in this page): it is :focus-visible, yet no help opens. The in-page wait
        # outlasts the help's own delay (280 ms), so its timer has run by then.
        said = target.evaluate("""async b => {b.focus(); const visible = b.matches(':focus-visible');
            await new Promise(r => setTimeout(r, 600)); return [visible, document.querySelectorAll('[role=tooltip]').length];}""")
        assert said == [True, 0]
        page.locator('#main').evaluate('n => n.focus()')
        keyboard_focus(target)
        expect(page.get_by_role('tooltip')).to_have_text(tip)
        # WCAG 2.4.3 / 2.4.7: the background redraw rebuilds the toolbar; keyboard focus stays on View settings, visibly.
        target.evaluate('b => {window.focusedBeforeRedraw = b;}')
        for route in held:
            route.continue_()
        page.wait_for_function('() => !window.focusedBeforeRedraw.isConnected')  # the toolbar was rebuilt
        expect(target).to_be_focused()
        assert target.evaluate("b => b.matches(':focus-visible')")
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_text_field_keeps_focus_and_its_caret_across_the_coverage_redraw(browser, live_server, h):
    """WCAG 2.4.3: the live coverage reply redraws the Schedule (app.ts loadLiveCoverage) without waiting for the person,
    unlike a live pulse; the field being typed in keeps focus and its caret (app.ts keepFocus)."""
    my_duties(h)
    context, page = sign_in(browser, live_server, 'staff', DESKTOP)
    held = []
    try:
        page.route('**/api/publications/*/coverage', lambda route: held.append(route))
        navigate(page, 'schedule/week/2026-10-05')
        wait_held(page, held)
        search = page.locator('#main').get_by_role('searchbox', name='Search sessions')
        search.evaluate('n => {window.focusedBeforeRedraw = n;}')
        search.fill('Clinic')  # the search applies itself by a redraw of the page (app.ts refilter)
        page.wait_for_function('() => !window.focusedBeforeRedraw.isConnected')
        expect(search).to_be_focused()
        assert search.evaluate('n => [n.value, n.selectionStart]') == ['Clinic', 6]
        search.evaluate("n => {n.setSelectionRange(2, 2); window.focusedBeforeRedraw = n;}")
        for route in held:
            route.continue_()
        page.wait_for_function('() => !window.focusedBeforeRedraw.isConnected')  # the coverage redraw rebuilt it
        expect(search).to_be_focused()
        assert search.evaluate('n => [n.value, n.selectionStart]') == ['Clinic', 2]
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('viewport,scheme', SIZES)
def test_back_from_the_fix_list_returns_to_the_rota_under_review(browser, live_server, h, viewport, scheme):
    """SW3-B-12: Back from the Fix list (#schedule/issues?draft=…) went to Home: opening it replaced the rota's history
    entry, because the Fix list and the rota are both "the Schedule". The Fix list is its own step."""
    from .test_browser import open_rota
    from .test_browser_fix_list import serve
    w = h.shift(h.workspace('Back from the Fix list'))['workspace']
    h.command('coordinator', 'workspace.submit', {'reason': 'Ready'}, w['id'], w['revision'])
    context, page = sign_in(browser, live_server, 'approver', viewport, color_scheme=scheme)
    try:
        serve(page, w['id'], must=False)
        navigate(page, 'home')
        open_rota(page, w['id'])
        rota = page.evaluate('location.hash')
        assert w['id'] in rota
        fix_pill(page, 'ok').click()
        page.locator('.popover.fix-pop').get_by_role('button', name=re.compile(r'^Open all in Fix list \(\d+\)$')).click()
        expect(page.locator('#main .fix-page')).to_be_visible()
        assert page.evaluate('location.hash').startswith('#schedule/issues')
        page.go_back()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        expect(page.locator('#main .fix-page')).to_have_count(0)
        assert w['id'] in page.evaluate('location.hash') and 'issues' not in page.evaluate('location.hash')
        page.go_back()  # and Back once more is Home, where the approver came from
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

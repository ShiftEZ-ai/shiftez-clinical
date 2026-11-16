"""Schedule redraws release the views they replace (perf audit SC-01 / PL-01): an old week grid, its top bar and the
hidden Auto-schedule panel's fields are collected after a garbage collection, instead of being chained to the live page
by a closure (the previous top bar) or the multiple-choice fields' resize registry."""
import pytest
from playwright.sync_api import expect

from .heap_paths import detached_paths, snapshot
from .test_browser import browser, live_server, sign_in, planner_tool  # noqa: F401
from .test_browser_schedule import october, show, settled

pytestmark = pytest.mark.browser

KEEP = """() => {window.__old = [['grid', document.querySelector('#schedule-view .wk-grid')],
    ['top bar', document.querySelector('.topbar .planner-bar')],
    ['Auto-schedule card', document.querySelector('#auto-panel .auto-body')]].filter(([, n]) => n).map(([k, n]) => [k, new WeakRef(n)]);
    return window.__old.map(([k]) => k);}"""
ALIVE = "() => window.__old.filter(([, r]) => r.deref()).map(([k]) => k)"


def collect(page):
    # Two timers hold an old render for a bounded time, then let go: the closed Auto-schedule panel's 10 s jobs poll
    # (perf PL-03) and a page skeleton's 15 s Retry timer (loading.ts). Let that time pass before looking.
    page.clock.run_for(16000)
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send('Performance.enable')
        for _ in range(2):
            cdp.send('HeapProfiler.collectGarbage')
        return {m['name']: m['value'] for m in cdp.send('Performance.getMetrics')['metrics']}
    finally:
        cdp.detach()


def still_alive(page):
    """The old views still reachable, and on failure the strong paths that hold detached DOM (heap_paths)."""
    alive = page.evaluate(ALIVE)
    if not alive:
        return ''
    cdp = page.context.new_cdp_session(page)
    try:
        return f'an old view is still reachable from the live page: {alive}; held by {detached_paths(snapshot(cdp))}'
    finally:
        cdp.detach()


def step(page, times):
    """Redraw the Schedule: the next week and back, `times` times."""
    label = page.get_by_role('group', name='Schedule').locator('strong').first
    for _ in range(times):
        page.get_by_role('button', name='Next week', exact=True).click()
        expect(label).to_have_text('Sun 18 Oct – Sat 24 Oct')
        settled(page)
        page.get_by_role('button', name='Previous week', exact=True).click()
        expect(label).to_have_text('Sun 11 Oct – Sat 17 Oct')
        settled(page)


def test_schedule_redraws_release_old_views_and_their_listeners(browser, live_server, h):
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    # The rule map stays unanswered through the redraws and the first collection, as on a busy server: a reply still
    # on its way must not keep the views it was asked for alive (run 40243065142 failed only when it was slow).
    held = []
    page.route('**/rule-map', lambda route: held.append(route))
    try:
        show(page, f"schedule/week/2026-10-14?edit={d['id']}")
        page.locator('#schedule-view .wk-grid').hover()  # the map is read on the first hover
        # The closed panel is built after its jobs read (a locator: an element handle would itself keep the node alive).
        # D76: the run card's body (its fields open under Change): KEEP holds it, as querySelector does.
        expect(page.locator('#auto-panel .auto-body').first).to_be_attached()
        kept = page.evaluate(KEEP)
        assert {'grid', 'top bar', 'Auto-schedule card'} <= set(kept), kept
        step(page, 3)
        collect(page)
        assert held, 'the rule map was asked for'
        assert not (alive := still_alive(page)), alive
        with page.expect_response('**/rule-map'):
            for route in held:
                route.continue_()
        page.unroute('**/rule-map')
        # The answer's headers are not the map applied: decorate() marks the view only once the map is ready.
        expect(page.locator('.planner-main[data-rk-rules]')).to_be_attached()
        settled(page)
        before = collect(page)
        # Ten more redraws do not grow the page: nodes and listeners stay within a small, fixed ceiling.
        step(page, 5)
        after = collect(page)
        assert after['Nodes'] - before['Nodes'] < 2000, (before['Nodes'], after['Nodes'])
        assert after['JSEventListeners'] - before['JSEventListeners'] < 300, (before['JSEventListeners'], after['JSEventListeners'])
    finally:
        context.close()


def test_a_rules_panel_waiting_for_the_rule_map_releases_old_views(browser, live_server, h):
    """The pinned Rules panel drawn again on each redraw while the rule map is still on its way does not keep the views
    it was drawn in alive; it fills in once the map arrives."""
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    page.route('**/rule-map', lambda route: held.append(route))
    try:
        show(page, f"schedule/week/2026-10-14?edit={d['id']}")
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota', exact=True)
        expect(panel).to_contain_text('Working out which rules apply')
        assert 'grid' in page.evaluate(KEEP)
        step(page, 2)
        expect(panel).to_contain_text('Working out which rules apply')  # still pinned and waiting: the path under test
        collect(page)
        assert held, 'the rule map was asked for'
        assert not (alive := still_alive(page)), alive
        with page.expect_response('**/rule-map'):
            for route in held:
                route.continue_()
        page.unroute('**/rule-map')
        expect(page.locator('.planner-main[data-rk-rules]')).to_be_attached()  # the map arrived and applied, not an error
        # Its rules, none, or the fold of rules that are met and unchanged ("1 more is met", Why panel v2).
        expect(panel.locator('.rk-body .rk-rules, .rk-body .rk-none, .rk-body .rk-fold').first).to_be_visible()
        expect(panel).not_to_contain_text('Working out which rules apply')
    finally:
        context.close()


def test_opening_and_closing_a_session_panel_keeps_the_grid(browser, live_server, h):
    """Perf PL-02: the session panel opens and closes beside the grid without rebuilding it (the grid and the top bar
    stay the same nodes); the focus goes into the panel and back to the chip that opened it."""
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, f"schedule/week/2026-10-14?edit={d['id']}")
        page.evaluate("() => {document.querySelector('#schedule-view .wk-grid').__kept = 1; document.querySelector('.topbar .planner-bar').__kept = 1;}")
        same = "() => [document.querySelector('#schedule-view .wk-grid'), document.querySelector('.topbar .planner-bar')].map(n => n && n.__kept === 1)"
        chip = page.locator('#schedule-view .wk-grid button.pchip:not(.gap):not(.sd-greychip)').first
        name = chip.get_attribute('aria-label')
        chip.click()
        expect(page.locator('.wk-panel')).to_be_visible()
        expect(page.locator('.wk-panel-head').get_by_role('button', name='Close panel')).to_be_focused()
        assert page.evaluate(same) == [True, True]
        page.keyboard.press('Escape')
        expect(page.locator('.wk-panel')).to_have_count(0)
        assert page.evaluate(same) == [True, True]
        assert page.evaluate('document.activeElement.getAttribute("aria-label")') == name
    finally:
        context.close()

"""The legend guard on the last unkeyed marks (LEAD-UIUX, legend-gaps): the rule map's exception forks that planner_rules
draws onto a view's heads and chips (drawn as an entry of that view's key: dom.ts hostLegend / legendAt), the session
panel's rule rows (its Rules tab and a person's Why list, each with its key), and the Fix list's key following a search
that hides rows in place (dom.ts legend `follow`)."""
import pytest
from playwright.sync_api import expect

from .legend_guard import assert_legend_matches
from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401
from .test_browser_legend_guard_more import serve_chips, three_types
from .test_browser_rules_in_planner import open_planner, rota
from .rules_support import rule_row

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 900}


def test_exception_forks_are_entries_of_the_views_key(browser, live_server, h):
    """Staff 02's own number in the weekly duty limit draws an exception fork on their Week chip: it is stamped as the
    Week key's "exception" entry (the same entry as the rules panel's), never left bare or put in a second key."""
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_planner(page, w)
        page.locator('.wk-day').nth(1).hover()  # the rule map is read on the first hover or focus
        fork = page.locator('#schedule-view .pchip').filter(has_text='Staff 02').locator('.rk-mark')
        expect(fork).to_be_attached()
        assert fork.first.get_attribute('data-lg', timeout=5000).split(':')[-1] == 'exception'
        page.mouse.move(1, 1)
        assert_legend_matches(page, root='#schedule-view', expect_marks=['exception'])
    finally:
        context.close()


def test_the_session_panels_rule_rows_are_keyed(browser, live_server, h):
    """A person's Why list (assignmentRules) and the session's Rules tab (sessionRules) draw state dots and issue marks:
    each list has its key (the rules panel's entries), and the panel draws no hue its keys do not explain."""
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_planner(page, w)
        page.locator('#schedule-view .pchip').filter(has_text='Staff 02').click()
        panel = page.locator('.wk-panel')
        expect(rule_row(panel, 'Weekly duty limit')).to_be_visible()  # the row says a sentence; its rule is data-rk-name
        names = assert_legend_matches(page, root='.wk-panel')
        assert any(n.startswith(('state:', 'issue:')) for n in names), names
        panel.get_by_role('tab', name='Rules', exact=True).click()
        # The Why tab's list (also .rk-rules) stays in its hidden pane: the session's own list is the one to see.
        expect(panel.get_by_role('list', name='Rules for this session', exact=True).locator('.rk-row').first).to_be_visible()
        names = assert_legend_matches(page, root='.wk-panel')
        assert any(n.startswith(('state:', 'issue:')) for n in names), names
    finally:
        context.close()


def test_the_fix_list_key_follows_a_search(browser, live_server, h):
    """A search hides rows in place: the key keeps only the marks still shown (Could improve's level goes with its row)
    and gets them back when the search is cleared."""
    w, shifts, _ = three_types(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        serve_chips(page, w, shifts)
        navigate(page, f'schedule/issues?draft={w["id"]}')
        main = page.locator('#main')
        expect(main.locator('.fix-page .fix-mark.could').first).to_be_visible()
        assert_legend_matches(page, root='#main .fix-page', expect_marks=['issue:error', 'issue:warning'])
        search = main.get_by_role('searchbox', name='Search the fix list')
        search.fill('Three clinics need a Consultant')
        expect(main.locator('.fix-page li[data-cause]:visible .fix-mark.could')).to_have_count(0)
        names = assert_legend_matches(page, root='#main .fix-page', expect_marks=['issue:error'])
        assert 'issue:warning' not in names, names
        search.fill('')
        expect(main.locator('.fix-page li[data-cause]:visible .fix-mark.could').first).to_be_visible()
        assert_legend_matches(page, root='#main .fix-page', expect_marks=['issue:error', 'issue:warning'])
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

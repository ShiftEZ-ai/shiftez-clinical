"""The legend guard (tests/legend_guard.py) on Year, Day, the print/export preview, Home, Fairness and Rules: every mark
drawn is stamped as its entry in the view's one key (dom.ts legend().mark), every entry has a mark on screen, and no mark
of the shared vocabulary is drawn without one reviewed."""
import pytest
from playwright.sync_api import expect

from .legend_guard import MARKS, assert_legend_matches
from .test_browser import browser, live_server, sign_in, navigate, planner_tool  # noqa: F401
from .test_browser_fairness_dashboard import published_month, october as fairness_october, section
from .test_browser_home import populated
from .test_browser_rules_sheet import rules
from .test_browser_schedule import at, checked, october, overlapping, show
from .test_browser_date_sets import date_set

pytestmark = pytest.mark.browser


def day_draft(h):
    """A checked draft, 18-24 Oct: on 20 Oct Alex is double-booked (must fix), Jordan leads a session, one is open."""
    a, b, _ = h.seed['people']
    d = h.shift(h.workspace('Legend day', '2026-10-18', '2026-10-25'), *at(20), people=[a])['workspace']
    d = h.shift(d, *at(20, '09', '13'), people=[a])['workspace']
    start, end = at(20, '14', '18')
    d = h.shift(d, start, end, people=[{'person_id': b, 'start': start, 'end': end, 'team_lead': True}])['workspace']
    d = h.shift(d, *at(20, '15', '19'), people=[])['workspace']
    return checked(h, d)


DAY_MARKS = ['l:draft', 'issue', 'open', 'tag:lead']


def test_year_dots_and_draft_start_are_keyed(browser, live_server, h):
    october(h)
    d2 = overlapping(h)
    date_set(h)  # Founding Day, 22 Feb 2026: Year underlines it (a named date), which the key explains
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, f"schedule/year/2026-10-12?edit={d2['id']}")
        expect(page.get_by_role('list', name='Published and draft days')).to_be_visible()
        # Published, draft and both on one day; no issues; the draft's first day flagged on its month.
        # Today's ring is keyed too when this year holds today (the guard checks it whenever it is drawn).
        assert_legend_matches(page, root='#schedule-view', expect_marks=['s:', 's:draft', 's:both', 't:', 'start', 'date-mark'])
    finally:
        context.close()


def test_day_bars_badges_and_issue_marks_are_keyed(browser, live_server, h):
    d = day_draft(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, f"schedule/day/2026-10-20?edit={d['id']}")
        expect(page.locator('#schedule-view button.sd-bar')).to_have_count(4)
        assert_legend_matches(page, root='#schedule-view', expect_marks=DAY_MARKS)
    finally:
        context.close()


_PAGES = """(marks) => [...document.querySelectorAll('.export-preview .export-page')].map(p => {
  const bodies = [...p.querySelectorAll('.export-body')];
  return {
    stamped: bodies.flatMap(b => [...b.querySelectorAll('[data-lg]')]).flatMap(n => n.getAttribute('data-lg').split(' ').filter(Boolean)),
    entries: [...p.querySelectorAll('.export-key .view-legend:not([hidden]) li[data-lg-entry]')].map(n => n.getAttribute('data-lg-entry')),
    bare: bodies.flatMap(b => [...b.querySelectorAll(marks)]).filter(n => !n.closest('[data-lg], [data-lg-self]')).map(n => n.outerHTML.slice(0, 160)),
    keyInBody: bodies.some(b => !!b.querySelector('.view-legend')),
  };
})"""


def test_export_pages_keep_the_stamps_and_the_key_on_every_page(browser, live_server, h):
    """The preview is inside a dialog (the guard leaves dialogs out), so its pages are checked here the same way: the
    copy keeps every stamp, the key moves out of the copy onto every page, and it lists only marks the document draws."""
    d = day_draft(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, f"schedule/day/2026-10-20?edit={d['id']}")
        assert_legend_matches(page, root='#schedule-view', expect_marks=DAY_MARKS)
        planner_tool(page, 'Export…')
        preview = page.get_by_role('dialog', name='Print or export', exact=True).get_by_role('img', name='Preview of the pages', exact=True)
        expect(preview.locator('.export-page .sd-tl').first).to_be_visible()
        pages = page.evaluate(_PAGES, MARKS)
        assert pages
        drawn = set().union(*(set(p['stamped']) for p in pages))
        for i, p in enumerate(pages):
            assert p['entries'], f'page {i + 1} has no key'
            assert not p['keyInBody'], f'page {i + 1}: the key stayed inside the copied view'
            assert not p['bare'], f'page {i + 1}: marks without a key entry: {p["bare"][:5]}'
            assert set(p['stamped']) <= set(p['entries']), f'page {i + 1}: marks missing from its key: {sorted(set(p["stamped"]) - set(p["entries"]))}'
            assert set(p['entries']) <= drawn, f'page {i + 1}: key entries the export never draws: {sorted(set(p["entries"]) - drawn)}'
        names = {e.split(':', 1)[1] for e in drawn}
        assert set(DAY_MARKS) <= names, sorted(names)
    finally:
        context.close()


@pytest.mark.parametrize('role', ['coordinator', 'staff'])
def test_home_tags_and_away_dots_are_keyed(browser, live_server, h, role):
    populated(h)
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page, 'home')
        main = page.locator('#main')
        if role == 'coordinator':
            today = main.locator('.home-today')
            today.get_by_role('button', name='Show people', exact=True).click()
            expect(today.locator('table .wk-tag.ot')).to_have_text('OT')
            expect(main.locator('.home-away-list li')).to_have_count(1)
            names = assert_legend_matches(page, root='#main', expect_marks=['tag:lead', 'tag:ot'])
            assert any(n.startswith('k:') for n in names), sorted(names)  # the away dot's kind
        else:
            # The duty's own tag (the hero's key draws the same TL swatch beside "Team lead").
            expect(main.locator('.home-hero > p .wk-tag.lead')).to_have_text('TL')
            expect(main.locator('.home-duties .home-duty-list li')).to_have_count(2)
            assert_legend_matches(page, root='#main', expect_marks=['tag:lead'])
    finally:
        context.close()


def test_fairness_points_bars_and_heat_cells_are_keyed(browser, live_server, h):
    published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'insights/fairness')
        fairness_october(page)
        expect(section(page, 'score').locator('.fx-point')).to_have_count(2)
        expect(section(page, 'exposure').locator('table.fx-heat')).to_be_visible()
        expect(section(page, 'distribution').locator('.fx-drow').first).to_be_visible()
        names = assert_legend_matches(page, root='#main', expect_marks=['exp', 'heat5'])
        assert names & {'ok', 'neutral', 'warn', 'err'}, sorted(names)  # the score points' tone
        assert any(n.startswith('fill:') for n in names), sorted(names)
    finally:
        context.close()


def test_rules_marks_and_number_pills_are_keyed(browser, live_server, h):
    rules(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        expect(page.locator('.rl-legend li')).to_have_count(4)
        assert_legend_matches(page, root='#main', expect_marks=['mark:prop', 'mark:mand', 'mark:adv', 'pill'])
    finally:
        context.close()
# Include this file in hosted supervision and team-lead qualification.

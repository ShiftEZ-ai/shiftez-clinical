"""The legend guard on the session card, the person card and the Fix list (owner, 7 Nov 2026: "nothing displayed without
legend!"): each draws its marks through its own shared key (dom.ts `legend`), so every mark it draws is stamped as its
entry, every entry has a mark on screen, and no mark of the shared vocabulary is drawn without its key
(tests/legend_guard.py). The session card (Coverage's cell popover, People × Days' chip popover): its duty hue (the card
and its pills), team boxes and their markers. The person card: today's team box. The Fix list (panel and page): each
session chip's duty-hue dot; a collapsed level enters its chips only once shown."""
import re
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, open_config, open_rota, sign_in, fix_list_reply  # noqa: F401
from .test_browser import fix_pill
from .test_browser_team_box import fixture as team_fixture
from .test_browser_person_card import TRAINEE, fixture as card_fixture, open_card
from .test_browser_fix_list import cause, serve
from .test_browser_qa_planner import clinic
from .legend_guard import assert_legend_matches

pytestmark = pytest.mark.browser
DESKTOP, PHONE = {'width': 1440, 'height': 900}, {'width': 390, 'height': 844}


def test_the_session_card_keys_its_duty_hue_teams_and_their_markers(browser, live_server, h):
    """Coverage's cell popover lists its sessions as the one session item (planner_week sessionItems) under one shared
    key: the team box fixture's sessions of several duty types draw teams, their markers and duty hues."""
    teams, _ = team_fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        navigate(page, f"schedule/coverage/2026-10-04?edit={teams['id']}")
        expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)
        # Staffed cells open their sessions' cards (a cell with one open place goes straight to filling it instead).
        cells = page.locator('#schedule-view button.cov-cell.full')
        expect(cells.first).to_be_visible()
        pop, names = page.locator('.popover.cov-popover'), set()
        for i in range(cells.count()):
            cells.nth(i).click()
            expect(pop.locator('.cov-list li').first).to_be_visible()
            expect(pop.locator('.view-legend')).to_have_count(1)  # one key for the cell's sessions
            names |= assert_legend_matches(page, root='.popover.cov-popover')
            page.keyboard.press('Escape')
            expect(pop).to_have_count(0)
        assert {'team', 'issue:warning'} <= names, names  # a team, and its could-improve marker (Unpaired, Not on this session)
        assert len({n for n in names if n.startswith('c:t:')}) >= 2, names  # the items' duty hues, one per type
    finally:
        context.close()


def test_the_person_card_keys_its_team_box(browser, live_server, h):
    card_fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        panel = open_card(page, TRAINEE)
        expect(panel.locator('.pc-duty .team-box')).to_have_count(1)
        expect(panel.locator('.pc .view-legend')).to_be_visible()
        assert_legend_matches(page, root='.pc', expect_marks=['team'])
    finally:
        context.close()


def three_types(h):
    """A draft with three empty sessions of three duty types: a must-fix cause holds the first two, a could-improve one
    the third."""
    evening = h.configure('assignment_types', {'name': 'Fictional legend evening', 'starts_on': '2020-01-01',
                                               'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    w = h.workspace('Fix list legend')
    for day, typ in ((4, None), (6, clinic(h)), (7, evening)):
        w = h.shift(w, f'2026-10-0{day}T08:00:00+03:00', f'2026-10-0{day}T12:00:00+03:00', people=[], typ=typ)['workspace']
    shifts = sorted(h.detail(w)['shifts'], key=lambda s: s['start'])
    return w, [s['id'] for s in shifts], [s['assignment_type_id'] for s in shifts]


def serve_chips(page, w, shifts):
    body = serve(page, w['id'])
    gap = cause(1, 'must', 2, 'Three clinics need a Consultant', '4–7 Oct 2026', 'assign', shift_ids=shifts[:2])
    gap['action']['scope'].update(shift_ids=shifts[:2], shift_id=shifts[0])
    gap['fixes'] = [{**gap['action'], 'available': True, 'unavailable_reason': None}]
    body['causes'][0] = gap
    could = next(c for c in body['causes'] if c['level'] == 'could')
    could['shift_ids'] = shifts[2:]
    page.route(f'**/api/workspaces/{w["id"]}/fix-list*', lambda r: fix_list_reply(r, body))


def test_the_fix_list_panel_keys_its_level_marks(browser, live_server, h):
    """The Fix list's chips are words (issue_marker sessionChips); its rows' level marks are the issue entries, and only
    the drawn rows count: Could improve opens collapsed, so its entry joins the key when it is shown."""
    w, shifts, _ = three_types(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        serve_chips(page, w, shifts)
        open_rota(page, w['id'])
        fix_pill(page, 'err').click()
        page.locator('.popover.fix-pop').get_by_role('button', name=re.compile(r'^Open all in Fix list \(\d+\)$')).click()
        panel = page.locator('.dock-panel.fix-side')
        expect(panel.locator('li[data-cause=c1] .fix-mark.must')).to_have_count(1)
        expect(panel.locator('.view-legend')).to_be_visible()
        names = assert_legend_matches(page, root='.dock-panel.fix-side', expect_marks=['issue:error'])
        assert 'issue:warning' not in names, names
        panel.locator('section[data-level=could]').get_by_role('button', name='Show', exact=True).click()
        expect(panel.locator('section[data-level=could] .fix-mark.could').first).to_be_visible()
        assert_legend_matches(page, root='.dock-panel.fix-side', expect_marks=['issue:error', 'issue:warning'])
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_the_fix_list_page_keys_its_level_marks(browser, live_server, h):
    w, shifts, _ = three_types(h)
    context, page = sign_in(browser, live_server, 'coordinator', PHONE)
    try:
        serve_chips(page, w, shifts)
        navigate(page, f'schedule/issues?draft={w["id"]}')
        expect(page.locator('#main .fix-page .fix-mark').first).to_be_visible()
        expect(page.locator('#main .fix-page > .view-legend')).to_be_visible()
        assert_legend_matches(page, root='#main .fix-page', expect_marks=['issue:error', 'issue:warning'])
        assert page.evaluate('document.documentElement.scrollWidth') <= PHONE['width']
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_tag_chips_name_themselves(browser, live_server, h):
    """A tag reference chip (tags.ts tagRef) says its tag in its own words: it is data-lg-self at its source, never a
    mark without its key."""
    h.configure('tags', {'name': 'Fictional legend tag', 'labels': ['people']})
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        open_config(page, 'Tags')
        chip = page.locator('.config-pane .tag-ref', has_text='#Fictional legend tag')
        expect(chip.first).to_be_visible()
        expect(chip.first).to_have_attribute('data-lg-self', '')
    finally:
        context.close()

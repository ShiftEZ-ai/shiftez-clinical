"""Sticky blocks: a page header, a toolbar, a tab bar, a grid's day header or a footer, once its scroll container scrolls,
is one opaque block with what it stacks against — nothing scrolled shows above, under or through it (owner, 21 Oct 2026:
"this happens a lot across many screens"). Every page, tab and first opened item of each role (the overflow sweep's
crawl), the Schedule's layouts with a draft open, the session editor and dialogs, at 1440 and 1280 (and 2000 for the
Schedule; 390 for staff), light and dark. The checks are tests/sticky_guard.js.
STICKY_REPORT=<file>: write every finding there and do not fail. STICKY_SHOTS=<folder>: a screenshot of each state."""
from .browser_seed import seeded
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_rota, pick_view, new_context, planner_tool, SCHEDULE_VIEWS  # noqa: F401
from .test_browser_overflow import adversarial, crawl, surfaces, settle

pytestmark = pytest.mark.browser
GUARD = Path(__file__).with_name('sticky_guard.js').read_text(encoding='utf-8')
# selector -> why it may let content show. Keep this short: fix the cause instead.
ALLOW = {}


class Sticky:
    """The overflow sweep's guard interface (check(state)) with the sticky checks."""
    def __init__(self, page, role, widths=(1440, 1280)):
        self.page, self.role, self.widths, self.found = page, role, widths, {}

    def check(self, state, widths=None, height=900, schemes=('light', 'dark'), also=None):
        """`also(page)`: more findings [{kind, where, detail}] in each state (the docked-panel sweep's head checks)."""
        page = self.page
        for width in widths or self.widths:
            page.set_viewport_size({'width': width, 'height': height})
            page.mouse.move(width // 2, 1)
            for scheme in schemes:
                page.emulate_media(color_scheme=scheme)
                settle(page)
                for f in page.evaluate(GUARD, {'allow': list(ALLOW)}) + (also(page) if also else []):
                    self.found.setdefault((state, f['kind'], f['where'], f['detail']), []).append(f'{width}{scheme[0]}')
                if os.getenv('STICKY_SHOTS') and scheme == 'light':
                    folder = Path(os.environ['STICKY_SHOTS']); folder.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(folder / f'{self.role}-{re.sub(r"[^\w]+", "_", state)}-{width}.png'))
        page.set_viewport_size({'width': 1440, 'height': 900})
        page.emulate_media(color_scheme='light')
        settle(page)

    def done(self):
        lines = [f'{self.role} | {state} | {kind} | {where} | {detail} | at {",".join(at)}' for (state, kind, where, detail), at in self.found.items()]
        if os.getenv('STICKY_REPORT'):
            with open(os.environ['STICKY_REPORT'], 'a', encoding='utf-8') as out:
                out.write('\n'.join(lines) + '\n')
            return
        assert not lines, '\n'.join(lines)


@seeded
def busy_draft(h):
    """The overflow sweep's draft (long and Arabic names) with enough sessions that every Schedule layout scrolls."""
    w = adversarial(h)
    people = h.seed['people']
    for day in range(4, 11):
        for k, hour in enumerate((7, 9, 11, 14, 16, 18)):
            w = h.shift(w, f'2026-10-{day:02}T{hour:02}:00:00+03:00', f'2026-10-{day:02}T{hour + 2:02}:00:00+03:00',
                        people=[people[(day + k) % len(people)]], service=k % 2, location=k % 2)['workspace']
    return w


def schedule(page, guard, w):
    """Each layout of the open draft, then the session editor opened from the List (its tabs over a scrolled form)."""
    open_rota(page, w['id'])
    expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)
    for view in SCHEDULE_VIEWS:
        if view == 'Year':
            continue
        pick_view(page, view)
        guard.check(f'schedule {view}', widths=(1440, 1280, 2000))
    page.locator('tr.dt-open td strong').first.click()
    page.locator('.wk-panel').get_by_role('button', name='Edit session', exact=True).click()  # the one session panel (S-09)
    editor = page.locator('.dock-panel')
    expect(editor.get_by_role('heading', name='Edit session and participants')).to_be_visible()
    for _ in range(3):  # more people: the form is taller than the panel
        editor.get_by_role('button', name='Add participant', exact=True).click()
    guard.check('session editor')
    editor.get_by_role('button', name='Close panel').click()
    if page.get_by_role('button', name='Discard changes').count():
        page.get_by_role('button', name='Discard changes').click()
    expect(page.locator('.dock-panel')).to_have_count(0)
    # A modal form step by step (its stepper over the scrolling body), on a short window so the body scrolls.
    page.evaluate("localStorage.setItem('rota.prefs', JSON.stringify({forms:'steps'}))")
    navigate(page, 'rules')
    page.get_by_role('button', name='Add rule', exact=True).click()
    dialog = page.get_by_role('dialog', name='Add rule')
    expect(dialog.locator('.stepper-head')).to_be_visible()
    guard.check('add rule dialog, step by step', height=420)
    page.evaluate("localStorage.setItem('rota.prefs', JSON.stringify({forms:'all'}))")
    dialog.get_by_role('button', name='Close dialog').click()
    expect(dialog).to_have_count(0)


def test_the_guard_finds_a_gap_a_cover_and_a_lost_header(browser):
    """The guard itself: a tab bar under a scroll container's padding (on the page and in an open modal dialog), a row
    painted over a header, a header trapped in an inner overflow box, each reported; the same blocks done right are not."""
    context = new_context(browser)
    page = context.new_page()
    head = 'position:sticky;top:0;height:30px;background:#fff'
    rows = ''.join(f'<p style="margin:0;height:40px">row {k}</p>' for k in range(40))
    try:
        page.set_content(f'''<body style="margin:0">
            <div id=gap style="height:300px;overflow:auto;padding-top:16px"><div class=t1 style="{head}">tabs</div>{rows}</div>
            <div id=ok style="height:300px;overflow:auto;padding-top:16px"><div class=t2 style="{head};top:-16px">tabs</div>{rows}</div>
            <div id=cover style="height:300px;overflow:auto"><div class=t3 style="{head}">tabs</div><p style="position:relative;z-index:5;margin:0;height:900px;background:#eee">over</p></div>
            <div id=lost style="height:300px;overflow:auto"><div style="overflow-x:auto"><div class=t4 style="{head}">head</div>{rows}</div></div></body>''')
        found = {(f['kind'], f['where'].split('.')[-1]) for f in page.evaluate(GUARD, {'allow': []})}
        assert found == {('gap', 't1'), ('covered', 't3'), ('lost', 't4')}, found
        # The same gap inside an open modal dialog (its body scrolls under the dialog head) is reported too.
        page.set_content(f'''<body style="margin:0"><dialog style="padding:0;width:400px">
            <header style="height:40px">Head</header>
            <div id=body style="height:300px;overflow:auto;padding-top:16px"><div class=t5 style="{head}">steps</div>{rows}</div></dialog></body>''')
        page.evaluate("document.querySelector('dialog').showModal()")
        found = {(f['kind'], f['where'].split('.')[-1]) for f in page.evaluate(GUARD, {'allow': []})}
        assert found == {('gap', 't5')}, found
    finally:
        context.close()


@pytest.mark.parametrize('view', ['Week', 'Month'])
def test_the_grid_keeps_its_scroll_across_a_redraw(browser, live_server, h, view):
    """The week and month grids scroll themselves (under the toolbar): a redraw (here a search) keeps where they were;
    another rota starts at the top."""
    w = busy_draft(h)
    other = h.workspace('Another rota', '2026-11-01', '2026-11-29')
    for day in range(1, 8):
        for k, hour in enumerate((7, 9, 11, 14, 16, 18)):
            other = h.shift(other, f'2026-11-{day:02}T{hour:02}:00:00+03:00', f'2026-11-{day:02}T{hour + 2:02}:00:00+03:00', service=k % 2, location=k % 2)['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, w['id'])
        expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)
        pick_view(page, view)
        page.set_viewport_size({'width': 1440, 'height': 600})
        grid = page.locator('.planner-main .wk-scroller')
        # The scroll position as the browser holds it (it may be fractional): compared within a pixel's rounding.
        at = grid.evaluate('g => { g.scrollTop = Math.floor((g.scrollHeight - g.clientHeight) / 2); return g.scrollTop; }')
        assert at > 40, 'the grid scrolls itself'
        old = grid.element_handle()
        page.get_by_role('searchbox').first.fill('Extended')
        page.wait_for_function('old => !old.isConnected', arg=old)
        expect(page.locator('.planner-main .wk-scroller')).to_have_count(1)
        # Put back once the redrawn grid has its height (its fitting runs a frame later): a state, waited for, within a
        # pixel's rounding (the browser may hold a fractional position).
        page.wait_for_function("at => Math.abs((document.querySelector('.planner-main .wk-scroller')?.scrollTop ?? -9) - at) <= 1", arg=at)
        # A slower reflow (a loaded runner's fitting lands frames later) collapses the grid, clamping it to the top, and
        # then gives its height back: the scroll is put back again, however many frames later it comes (a size hook).
        clamped = page.locator('.planner-main .wk-scroller').evaluate("""g => new Promise(done => {
          const frames = (n, then) => n ? requestAnimationFrame(() => frames(n - 1, then)) : then();
          frames(6, () => { g.firstElementChild.style.setProperty('display', 'none');
            frames(2, () => { const top = g.scrollTop; g.firstElementChild.style.removeProperty('display'); done(top); }); });
        })""")
        assert clamped == 0, 'the collapsed grid clamps its scroll'
        page.wait_for_function("at => Math.abs((document.querySelector('.planner-main .wk-scroller')?.scrollTop ?? -9) - at) <= 1", arg=at)
        # Another rota is another view: it starts at the top.
        old = page.locator('.planner-main .wk-scroller').element_handle()
        open_rota(page, other['id'])
        page.wait_for_function('old => !old.isConnected', arg=old)
        grid = page.locator('.planner-main .wk-scroller')
        assert grid.evaluate('g => g.scrollHeight - g.clientHeight') > at, 'it could be scrolled as far'
        assert grid.evaluate('g => g.scrollTop') == 0
    finally:
        context.close()


@pytest.mark.parametrize('role', ['admin', 'coordinator'])
def test_no_sticky_block_lets_content_through(browser, live_server, h, role):
    w = busy_draft(h)
    context, page = sign_in(browser, live_server, role)
    guard = Sticky(page, role)
    try:
        if role == 'coordinator':
            schedule(page, guard, w)
        for address in surfaces(page):
            crawl(page, guard, address)
    finally:
        context.close()
    guard.done()


def test_staff_screens_on_a_phone(browser, live_server, h):
    busy_draft(h)
    context, page = sign_in(browser, live_server, 'staff', viewport={'width': 390, 'height': 844})
    guard = Sticky(page, 'staff', widths=(390, 1440))
    try:
        for address in surfaces(page):
            crawl(page, guard, address)
    finally:
        context.close()
    guard.done()


# The Schedule's own heads with a docked panel open (maintenance: with the rules panel open, the group heads stuck at one top
# showed through and over each other). In each scrolled position: the toolbar, the day header and the group heads are
# opaque (opacity 1 up to their scroller) and do not overlap: the day header starts below the toolbar, a stuck group
# head below the day header, and of the group heads stuck at one top none shows beneath the one on top.
APART = r"""() => {
  const found = [], seen = new Set(), say = (kind, n, detail) => { const k = kind + detail; if (!seen.has(k)) { seen.add(k); found.push({kind, where: n.className.toString().split(' ').slice(0, 2).join('.'), detail}); } };
  const view = document.querySelector('#schedule-view'); if (!view) return [{kind: 'missing', where: '#schedule-view', detail: 'no schedule view'}];
  const bar = document.querySelector('.planner-toolbar');
  const head = [...view.querySelectorAll('.wk-head>*,.pg-head>*')].filter(n => n.getClientRects().length);
  const groups = [...view.querySelectorAll('.wk-grid>.wk-group:not(.wk-subgroup)>.wk-group-head')].filter(n => n.getClientRects().length);
  const scrollerOf = n => { for (let p = n.parentElement; p && p !== document.body; p = p.parentElement) if (/auto|scroll/.test(getComputedStyle(p).overflowY) && p.scrollHeight > p.clientHeight + 24) return p; return document.scrollingElement; };
  const opaque = (n, s) => { for (let p = n; p && p !== s && p !== document.body; p = p.parentElement) if (getComputedStyle(p).opacity !== '1') return p; return null; };
  const s = head.length ? scrollerOf(head[0]) : null; if (!s) return found;
  const before = s.scrollTop, most = s.scrollHeight - s.clientHeight;
  for (const at of [Math.min(most, 160), Math.round(most / 2), most]) {
    s.scrollTop = at;
    const port = s === document.scrollingElement ? 0 : s.getBoundingClientRect().top;
    for (const n of [bar, ...head, ...groups]) { const o = n && opaque(n, s); if (o) say('see-through', n, `"${(n.innerText || '').trim().slice(0, 30)}" opacity ${getComputedStyle(o).opacity} at ${at}`); }
    const h = head[0].getBoundingClientRect();
    if (bar && bar.getBoundingClientRect().bottom > h.top + 1 && h.bottom > port) say('overlap', head[0], `the toolbar covers the day header at ${at}`);
    const stuck = groups.filter(g => Math.abs(g.getBoundingClientRect().top - h.bottom) <= 1);
    for (const g of groups) { const r = g.getBoundingClientRect(); if (r.bottom > h.bottom + 1 && r.top < h.bottom - 1) say('overlap', g, `"${(g.innerText || '').trim().slice(0, 30)}" runs under the day header at ${at}`); }
    if (stuck.length > 1) { const top = stuck[stuck.length - 1].getBoundingClientRect().bottom;
      for (const g of stuck.slice(0, -1)) if (g.getBoundingClientRect().bottom > top + 0.5) say('overlap', g, `"${(g.innerText || '').trim().slice(0, 30)}" shows under the group head stuck over it at ${at}`); }
  }
  s.scrollTop = before;
  return found;
}"""
# A session's own control: People's chips are buttons; span.pg-chip are drawn marks (a day's open places "+N" in the
# head row, the key's swatches), which open nothing.
SESSION_CHIP = "#schedule-view :is(button.pchip:not(.gap):not(.more):not(.sd-greychip),button.pg-chip:not(.sd-greychip))"


def close_panels(page):
    panels = page.locator('.dock-panel, .wk-panel, .rk-panel')
    for _ in range(3):
        if not panels.count():
            return
        # Each panel's own Close ("Close panel"; the rules panel's "Close rules panel (Esc)"): Escape reaches a panel
        # only while focus is inside it (planner_rules.ts), which the sweep's resizes do not keep.
        close = panels.get_by_role('button', name=re.compile(r'^Close (rules )?panel'))
        if close.count():
            close.first.click()
        else:
            page.keyboard.press('Escape')
    expect(panels).to_have_count(0)


def open_fix_list(page):
    page.locator('.topbar').get_by_role('button', name='Rota tools').click()
    page.get_by_role('menuitem', name=re.compile('^Fix list')).click()
    expect(page.locator('.dock-panel.fix-side')).to_be_visible()


def open_rules(page):
    planner_tool(page, 'Rules in this rota')
    expect(page.get_by_role('dialog', name='Rules in this rota')).to_be_visible()


def open_session(page):
    # A chip without an issue marker: a marked chip's centre can be its marker, which opens the issue, not the session.
    page.locator(SESSION_CHIP).filter(has_not=page.locator('[class*="iss-"]')).first.click()
    expect(page.locator('.wk-panel')).to_be_visible()
    # Rules load after the panel; wait for its scrolling content before checking the key.
    expect(page.locator('.wk-panel ul.rk-rules')).to_be_visible()
    keys = page.locator('.wk-panel .view-legend')
    expect(keys.first).to_be_visible()
    for key in keys.all():
        expect(key).to_have_css('position', 'static')


PANELS = {'fix list': open_fix_list, 'rules': open_rules, 'session': open_session}


@pytest.mark.parametrize('panel', list(PANELS))
def test_schedule_heads_stay_opaque_and_apart_with_a_docked_panel_open(browser, live_server, h, panel):
    """maintenance (b): the sweep with each docked panel open (Fix list, rules, session) over Week, Month and People, at 1440
    and 390, light: the sticky guard plus the Schedule's own heads (opaque, never over one another)."""
    w = busy_draft(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    guard = Sticky(page, 'coordinator')
    try:
        open_rota(page, w['id'])
        expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)
        for view in ('Week', 'Month', 'People'):
            pick_view(page, view)
            PANELS[panel](page)
            guard.check(f'schedule {view} with the {panel} panel', widths=(1440, 390), schemes=('light',),
                        also=lambda p: p.evaluate(APART))
            close_panels(page)
    finally:
        context.close()
    guard.done()

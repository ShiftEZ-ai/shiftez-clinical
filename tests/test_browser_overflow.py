"""Layout guard: nothing overflows its box, covers its neighbour, scrolls the page sideways or cuts a focus ring — on
every page and key state (tabs, Configuration sections, Schedule views, the item panels), for every role, at 1440,
1024, 390 and 320 pixels wide, light and dark, with adversarial data (very long and Arabic names, clinics, tags, notes,
attachments, 3-digit counts). The checks are tests/overflow_guard.js; deliberate cases are in ALLOW with a reason.

OVERFLOW_REPORT=<file>: write every finding there and do not fail (to survey a change).
OVERFLOW_SHOTS=<folder>: a screenshot of each state at each width (to look at a fix)."""
from .browser_seed import seeded
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect, TimeoutError as PlaywrightTimeout
from sqlalchemy import insert, update

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_browser import browser, live_server, sign_in, navigate, new_context, open_app, fonts_ready  # noqa: F401

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
GUARD = Path(__file__).with_name('overflow_guard.js').read_text(encoding='utf-8')
WIDTHS = (1440, 1024, 390, 320)
# The planners here; the other roles in test_browser_overflow_roles.py (two files: CI shards split by file).
ROLES = ('admin', 'coordinator', 'approver')
# selector -> why it may overflow / overlap. Keep this short: fix the cause instead.
ALLOW = {
    '.chip-x': 'its hit area is widened by an invisible ::before (WCAG 2.5.8 target size), not drawn',
    '.rl-tick': 'a 2px tick on the rule range bar with its number centred over it',
    **dict.fromkeys(('button.health-pill', 'button.rp', '.dt-chip', '.iss-chip', 'button.iss-mark'),
                    'on a phone its 44 px target is a centred invisible ::after (app.css › Touch targets), not drawn'),
    '.app-footer :is(button,a[href])': 'on a phone its 44 px target is a centred invisible ::after (the footer stays one line)',
}
LONG = "Dr Luca Irwin synthetic overflow fixture synthetic overflow fixture synthetic overflow fixture"
ARABIC = 'د. عبدالودود بن النخلي بن عبدالقهار الرملي'
WORD = 'Supercalifragilisticexpialidociousclinicname'  # one unbreakable word


@seeded
def adversarial(h):
    """The seed department renamed to the longest names people type, plus Arabic, an unbreakable word, a person with
    many tags, long notes, an attachment between the two longest names and 100+ audit events."""
    s = h.seed
    a, b, c = s['people']
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(update(m.Person).where(m.Person.id == a).values(display_name=LONG, title='Consultant in Department A, Clinical Lead',
                                                                   directory_section='Urgent care and walk-in services, ground floor east wing'))
        db.execute(update(m.Person).where(m.Person.id == b).values(display_name=ARABIC))
        db.execute(update(m.Person).where(m.Person.id == c).values(display_name=WORD))
        db.execute(update(m.Service).where(m.Service.id == s['services'][0]).values(name='Continuity of care and chronic disease management clinic (adult, extended hours)'))
        db.execute(update(m.Service).where(m.Service.id == s['services'][1]).values(name='عيادة المراجعة والمتابعة للأمراض المزمنة'))
        db.execute(update(m.Location).where(m.Location.id == s['locations'][0]).values(name='North consulting room ' + WORD))
        db.execute(update(m.AssignmentType).where(m.AssignmentType.id == s['type']).values(name='Extended clinical review session with supervision and teaching'))
        db.execute(update(m.Category).where(m.Category.id == s['category']).values(name='Senior registrar practitioners (family medicine track, year 4+)'))
        for role, ident in s['identities'].items():
            db.execute(update(m.Identity).where(m.Identity.id == ident).values(display_name=f'rota-{role} (test) with a much longer display name than usual'))
        # 120 audit events: 3-digit counts in Recent changes and the audit log.
        actions = ('department.settings', 'catalogue.update', 'tag.members', 'department.display')
        db.execute(insert(m.AuditEvent), [{'department_id': s['department_id'], 'actor_id': s['identities'][('admin', 'coordinator')[k % 2]],
                                           'action': actions[k % 4], 'target_id': s['department_id'], 'detail': {}, 'correlation_id': f'overflow-{k}',
                                           'reason': 'Synthetic long reason ' + WORD} for k in range(120)])
    tags = [h.configure('tags', {'name': f'Very_long_tag_name_{k:03}xy', 'labels': ['people', 'services']})['record'] for k in range(4)]
    for tag in tags:
        h.command('admin', 'tag.members', {'add': [{'kind': 'people', 'id': a}, {'kind': 'services', 'id': s['services'][0]}], 'reason': 'Synthetic'}, tag['id'], tag['revision'])
    h.configure('mentorships', {'person_id': a, 'mentor_id': b, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'strength': 'required',
                                'assignment_type_ids': [s['type']]}, role='coordinator')
    me = h.get('admin', '/api/me')
    h.command('admin', 'department.settings', {'department_name': 'Department A — Extended Hours Department (North)',
              'department_revision': me['department']['revision'], 'organisation_name': 'Example Organisation',
              'timezone': 'Asia/Riyadh', 'week_start': 6, 'reason': 'Synthetic long department name'}, me['department']['id'], me['organisation']['revision'])
    note = 'A long private reason that keeps going: ' + 'conference travel and family commitments, ' * 6 + WORD
    for k in range(3):
        h.request = h.command('staff', 'request.create', {'person_id': a, 'start': f'2026-10-{12 + k}T08:00:00+03:00', 'end': f'2026-10-{12 + k}T17:00:00+03:00',
                                              'private_reason': note})['record']
    w = h.workspace('Draft rota with a very long name for the overflow checks ' + WORD, '2026-10-04', '2026-11-01')
    for day in range(4, 11):
        w = h.shift(w, f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00', people=[a, b] if day % 2 else [c])['workspace']
        w = h.shift(w, f'2026-10-{day:02}T13:00:00+03:00', f'2026-10-{day:02}T17:00:00+03:00', people=[], service=1, location=1)['workspace']
    h.policy({'kind': 'maximum_participants', 'maximum': 3}, scope={'service_ids': [s['services'][0]]})
    identity(h, 'programme', ['programme_lead'])
    return w


def settle(page):
    fonts_ready(page)
    page.wait_for_function('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))')


def test_settle_times_out_when_animation_frames_stop(chromium, monkeypatch):
    context = chromium.ensure().new_context()
    try:
        page = context.new_page()
        page.set_default_timeout(100)
        monkeypatch.setattr(__name__ + '.fonts_ready', lambda _: None)
        page.evaluate('window.requestAnimationFrame = () => 0')
        with pytest.raises(PlaywrightTimeout):
            settle(page)
    finally:
        context.close()


class Guard:
    def __init__(self, page, role):
        self.page, self.role, self.found = page, role, {}

    def check(self, state):
        """Every width, light and dark, of what is on screen now."""
        page = self.page
        for width in WIDTHS:
            page.set_viewport_size({'width': width, 'height': 900})
            page.mouse.move(width // 2, 1)  # off any control: no hover tooltip in the measurements
            for scheme in ('light', 'dark'):
                page.emulate_media(color_scheme=scheme)
                settle(page)
                focus = scheme == 'light'  # ring geometry does not change with the theme
                if focus:
                    page.keyboard.press('Shift')  # keyboard modality: a focused control draws its :focus-visible ring
                for f in page.evaluate(GUARD, {'allow': list(ALLOW), 'focus': focus}):
                    self.found.setdefault((state, f['kind'], f['where'], f['detail']), []).append(f'{width}{scheme[0]}')
                if focus and page.locator('.sidebar').count():  # it opens while it holds keyboard focus: close it again
                    page.locator('.sidebar').dispatch_event('mouseleave')
                if os.getenv('OVERFLOW_SHOTS') and scheme == 'light':
                    folder = Path(os.environ['OVERFLOW_SHOTS']); folder.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(folder / f'{self.role}-{re.sub(r"[^\w]+", "_", state)}-{width}.png'))
        page.set_viewport_size({'width': 1440, 'height': 900})
        settle(page)

    def lines(self):
        return [f'{self.role} | {state} | {kind} | {where} | {detail} | at {",".join(at)}' for (state, kind, where, detail), at in self.found.items()]


OPENED = '.dock-panel, [role=dialog]:visible'


def open_first_row(page, guard, state):
    """The first item of the list on screen opens (a person, request, tag or rule in its side panel; a record in its
    form): checked with each of its tabs, then closed."""
    row = page.locator('#main .rule-item .rl-text, #main .dt-body tbody tr.dt-open strong, #main .dt-body .dt-card.dt-open strong').first
    if not row.count() or not row.is_visible():
        return
    row.click()
    opened = page.locator(OPENED).last
    expect(opened).to_be_visible(timeout=3000)
    guard.check(state + ' › opened')
    tabs = opened.get_by_role('tab')
    for k in range(1, tabs.count()):
        tabs.nth(k).click()
        guard.check(f'{state} › opened › {tabs.nth(k).inner_text().splitlines()[0]}')
    page.keyboard.press('Escape')
    if page.locator(OPENED).count():  # a dialog whose Escape asks first: its own Close / Cancel
        page.locator(OPENED).last.get_by_role('button', name=re.compile(r'^(Close|Cancel)')).first.click()
    expect(page.locator(OPENED)).to_have_count(0)


@pytest.mark.parametrize('layout', ['table', 'cards'])
def test_open_first_row_skips_group_headings_and_noninteractive_rows(browser, monkeypatch, layout):
    context = new_context(browser)
    page = context.new_page()
    if layout == 'table':
        rows = '<table><tbody><tr class="dt-group" data-click="group"><td><strong>Group heading</strong></td></tr>' \
               '<tr data-click="read-only"><td><strong>Read-only record</strong></td></tr>' \
               '<tr class="dt-open" data-click="opener"><td><strong>Openable record</strong></td></tr></tbody></table>'
    else:
        rows = '<div class="dt-group" data-click="group"><strong>Group heading</strong></div>' \
               '<div class="dt-card" data-click="read-only"><strong>Read-only record</strong></div>' \
               '<div class="dt-card dt-open" data-click="opener"><strong>Openable record</strong></div>'
    try:
        page.set_content('<main id="main"><div class="dt-body">' + rows + '</div></main>'
                         '<template id="panel"><div class="dock-panel" role="dialog" aria-label="Opened record" data-tab="Summary">'
                         '<div role="tablist"><button role="tab">Summary</button><button role="tab">Details</button></div>'
                         '<button>Close</button></div></template>')
        page.evaluate("""() => {
            window.clicked = [];
            document.querySelectorAll('[data-click]').forEach(row => row.addEventListener('click', () => {
                window.clicked.push(row.dataset.click);
                if (row.dataset.click !== 'opener') return;
                document.body.append(document.querySelector('#panel').content.cloneNode(true));
                const panel = document.querySelector('.dock-panel');
                panel.querySelectorAll('[role=tab]').forEach(tab => tab.addEventListener('click', () => {
                    panel.dataset.tab = tab.textContent;
                }));
                panel.querySelector(':scope > button').addEventListener('click', () => panel.remove());
            }));
            document.addEventListener('keydown', e => { if (e.key === 'Escape') document.querySelector('.dock-panel')?.remove(); });
        }""")
        checks = []
        guard = Guard(page, 'synthetic')
        def checked(state):
            panel = page.get_by_role('dialog', name='Opened record')
            expect(panel).to_be_visible()
            expect(panel).to_have_attribute('data-tab', 'Details' if checks else 'Summary')
            checks.append(state)
        monkeypatch.setattr(guard, 'check', checked)
        open_first_row(page, guard, 'collection')
        assert page.evaluate('window.clicked') == ['opener']
        assert checks == ['collection › opened', 'collection › opened › Details']
        expect(page.get_by_role('dialog', name='Opened record')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('layout', ['table', 'cards'])
def test_open_first_row_fails_when_an_interactive_record_does_not_open(browser, monkeypatch, layout):
    context = new_context(browser)
    page = context.new_page()
    row = ('<table><tbody><tr class="dt-open"><td><strong>Broken record</strong></td></tr></tbody></table>'
           if layout == 'table' else '<div class="dt-card dt-open"><strong>Broken record</strong></div>')
    try:
        page.set_content('<main id="main"><div class="dt-body">' + row + '</div></main>')
        page.evaluate("""() => {
            window.clicked = false;
            document.querySelector('.dt-open').addEventListener('click', () => { window.clicked = true; });
        }""")
        checks = []
        guard = Guard(page, 'synthetic')
        monkeypatch.setattr(guard, 'check', checks.append)
        with pytest.raises(AssertionError, match='visible'):
            open_first_row(page, guard, 'broken collection')
        assert page.evaluate('window.clicked')
        assert checks == []
    finally:
        context.close()


def crawl(page, guard, address):
    """A page, each of its tabs (or Configuration sections), and the first item of each list."""
    navigate(page, address)
    guard.check(address)
    open_first_row(page, guard, address)
    tabs = page.locator('#main [role=tablist]').first.get_by_role('tab')
    sections = page.locator('#main nav.config-nav button')  # Configuration sections, Programmes' lists
    for items in (tabs, sections):
        for k in range(1 if items is tabs else 0, items.count()):
            if k >= items.count():  # a tab drew a page with fewer of them
                break
            state = f'{address} › {items.nth(k).inner_text().splitlines()[0]}'
            items.nth(k).click()
            guard.check(state)
            open_first_row(page, guard, state)


SCHEDULE = ('schedule/month', 'schedule/week', 'schedule/day', 'schedule/people', 'schedule/coverage', 'schedule/list', 'schedule/year')


def surfaces(page):
    """The pages this role's sidebar offers, with the tabs of merged pages as their own addresses."""
    routes = page.locator('.sidebar [data-route]').evaluate_all('bs => bs.map(b => b.dataset.route)')
    out = []
    for r in routes:
        if r == 'schedule':
            out += SCHEDULE
        elif r in ('insights', 'access', 'system'):
            out.append(r)  # its tabs are crawled
        else:
            out.append(r)
    return out + ['inbox']


@pytest.mark.parametrize('role', ROLES)
def test_no_page_overflows_overlaps_or_clips(browser, live_server, h, role):
    sweep(browser, live_server, h, role)


def sweep(browser, live_server, h, role):
    w = adversarial(h)
    context, page = sign_in(browser, live_server, role)
    guard = Guard(page, role)
    try:
        for address in surfaces(page):
            crawl(page, guard, address)
        if role in ('admin', 'coordinator', 'approver', 'auditor'):
            navigate(page, f'planner/{w["id"]}')
            guard.check('schedule draft')
        if role == 'coordinator':
            # L7: a docked panel narrows the page column; the Week's day heads and toolbar fit what is left.
            from .test_browser_sticky import open_fix_list
            page.set_viewport_size({'width': 1440, 'height': 900})  # the last check ended at 320: dock it on a desktop
            navigate(page, f'schedule/week?draft={w["id"]}')
            open_fix_list(page)
            # The toolbar keeps Add session on a row with other controls (never alone on its own row).
            alone = page.evaluate('''() => { const tb = document.querySelector('.planner-toolbar'), add = tb && tb.querySelector('.add-session');
                if (!add) return false; const top = Math.round(add.getBoundingClientRect().top);
                return [...tb.children].filter(c => c.checkVisibility() && Math.round(c.getBoundingClientRect().top) === top).length === 1; }''')
            assert not alone, 'Add session sits alone on a toolbar row beside the docked Fix list'
            guard.check('schedule week with the Fix list docked')
            page.keyboard.press('Escape')
        if role in ('admin', 'coordinator', 'approver', 'staff'):
            page.evaluate("id => location.hash = 'requests?open=' + id", h.request['id'])
            expect(page.locator('.dock-panel')).to_be_visible()
            guard.check('request panel')
            page.keyboard.press('Escape')
        page.get_by_role('button', name=re.compile(r'^Account menu, ')).click()
        guard.check('account menu')
        page.keyboard.press('Escape')
    finally:
        context.close()
    lines = guard.lines()
    if os.getenv('OVERFLOW_REPORT'):
        with open(os.environ['OVERFLOW_REPORT'], 'a', encoding='utf-8') as out:
            out.write('\n'.join(lines) + '\n')
        return
    assert not lines, '\n'.join(lines)


def test_sign_in_page(browser, live_server):
    context = new_context(browser)
    page = open_app(context, live_server)
    try:
        expect(page.get_by_label('Username or email', exact=True)).to_be_visible()
        guard = Guard(page, 'signed-out')
        guard.check('sign in')
        page.get_by_label('Username or email', exact=True).fill(LONG)
        guard.check('sign in, long username')
    finally:
        context.close()
    assert not guard.lines() or os.getenv('OVERFLOW_REPORT'), '\n'.join(guard.lines())


# A docked panel beside the Schedule (LEAD-UIUX, 7 Nov 2026: a live trace measured the planner 144 px wider than a
# 1280 window with the selection panel open). By the layout (app.css), it cannot happen:
# - Above 1180 px, .planner-body is a row of .planner-main (flex:1, min-width:0, size-contained) and one flex:none panel.
#   Its width is set by side_panel.ts so the page beside it keeps 360 px.
# - The session panel and the selection panel are one slot: opening either closes the other (planner_week onPanel →
#   unpin; planner_rules pin → onOpen → closeWeekPanel).
# - At 1180 px and below, panels stack; at 1100 px and below, the Schedule's side panel is a fixed overlay.
# This proves it on both layouts that dock them, so a future min-width cannot break it unnoticed.
SIDEWAYS = """s => {const d=document.documentElement,p=document.querySelector(s);
  return {page:d.scrollWidth,room:d.clientWidth,right:p?Math.round(p.getBoundingClientRect().right):null};}"""


def no_sideways(page, panel):
    expect(page.locator(panel).first).to_be_visible()
    got = page.evaluate(SIDEWAYS, panel)
    assert got['page'] <= got['room'], f'{panel}: the page scrolls sideways {got}'
    assert got['right'] is not None and got['right'] <= got['room'], f'{panel}: its right edge is off screen {got}'


@pytest.mark.parametrize('width', [1280, 1024])
@pytest.mark.parametrize('layout', ['week', 'coverage'])
def test_a_docked_schedule_panel_never_pushes_the_page_sideways(browser, live_server, h, layout, width):
    from .test_browser_qa_planner import checked_rota, settled
    w, _ = checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    try:
        navigate(page, f"schedule/{layout}/2026-10-04?edit={w['id']}"); settled(page)
        view = page.locator('#schedule-view')
        # The session panel: a person's chip (Week), or a session in a Coverage cell's popover.
        if layout == 'week':
            view.locator(f'.wk-grid {DRAFT_CHIP}').first.click()
        else:
            view.locator('button.cov-cell').first.click()
            page.locator('.cov-popover .ag-item').first.click()
        no_sideways(page, '.wk-panel')
        # The selection panel, which replaces it: Ctrl+click a row head (Week) or a session in the popover (Coverage).
        if layout == 'week':
            view.locator('.wk-rowhead.rk-pinnable').first.click(modifiers=['Control'])
        else:
            view.locator('button.cov-cell').first.click()
            page.locator('.cov-popover .ag-item').first.click(modifiers=['Control'])
        no_sideways(page, '.rk-panel')
    finally:
        context.close()


# X3 (live verify 235391b): a Fix list row's words were squeezed to 49 px, one word per line ("sessio n"), beside its two
# actions in the 440 px dock. The row wraps its actions under the words instead: the words keep at least 12 ch and no
# word is broken, in the dock (desktop) and on the phone's Fix list page (390).
LONG_CAUSE = 'The duty type was edited after this session was added — refresh the session to use the new settings'
ROW_WORDS = """rows => rows.map(t => {
  const probe = document.createElement('span'); probe.textContent = '0'.repeat(12); t.append(probe);
  const ch12 = probe.getBoundingClientRect().width; probe.remove();
  const strong = t.querySelector('strong'), node = strong && strong.firstChild, broken = [];
  if (node && node.nodeType === 3) {
    const text = node.textContent; let at = 0;
    for (const word of text.split(/\\s+/)) { const i = text.indexOf(word, at); at = i + word.length;
      if (!word) continue; const r = document.createRange(); r.setStart(node, i); r.setEnd(node, i + word.length);
      if (r.getClientRects().length > 1) broken.push(word); }
  }
  return {width: t.getBoundingClientRect().width, ch12, broken};
})"""


@pytest.mark.parametrize('where', ['dock-1440', 'phone-390'])
def test_fix_list_row_words_keep_a_readable_width(browser, live_server, h, where):
    from .test_browser import open_rota
    from .test_browser_fix_list import serve
    from .test_browser_fix_list_qa3 import open_panel
    w = h.shift(h.workspace('Fix list row width'), people=[])['workspace']
    size = {'width': 390, 'height': 844} if where == 'phone-390' else {'width': 1440, 'height': 900}
    context, page = sign_in(browser, live_server, 'coordinator', viewport=size)
    try:
        body = serve(page, w['id'])
        body['causes'][0]['sentence'] = LONG_CAUSE  # its action and its "Show evidence": two actions beside the words
        if where == 'phone-390':
            navigate(page, f'schedule/issues?draft={w["id"]}')
            rows = page.locator('#main .fix-page li[data-cause] > .fix-row .home-need-text')
        else:
            open_rota(page, w['id'])
            rows = open_panel(page).locator('li[data-cause] > .fix-row .home-need-text')
        expect(rows.first).to_contain_text(LONG_CAUSE)
        for r in rows.evaluate_all(ROW_WORDS):
            assert r['width'] >= r['ch12'] - 1, r  # at least 12 ch
            assert not r['broken'], r  # no word split across lines
    finally:
        page.unroute_all(behavior='ignoreErrors')

MENU_PROBE = r'''([longLabel, longHint, shortHint]) => {
  // The shared menu's own markup and classes (dom.ts menuItem: icon, label, hint), placed as a popover is.
  const pop = document.createElement('div');
  pop.className = 'popover menu-list'; pop.setAttribute('role', 'menu');
  pop.style.setProperty('left', '8px'); pop.style.setProperty('top', '8px');
  const item = (label, hint, disabled) => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'menu-item'; b.setAttribute('role', 'menuitem'); if (disabled) b.disabled = true;
    const icon = document.createElement('span'); icon.className = 'icon';
    const text = document.createElement('span'); text.textContent = label;
    b.append(icon, text);
    if (hint) { const h = document.createElement('span'); h.className = 'hint'; h.textContent = hint; b.append(h); }
    return b;
  };
  pop.append(item(longLabel, shortHint, false), item(longLabel, longHint, true), item('Open rule', '', false));
  document.body.append(pop);
  const box = pop.getBoundingClientRect(), problems = [];
  for (const span of pop.querySelectorAll('.menu-item > span:not(.icon)')) {
    const node = span.firstChild, words = node.textContent.split(' ');
    let at = 0;
    for (const w of words) {
      const r = document.createRange(); r.setStart(node, at); r.setEnd(node, at + w.length); at += w.length + 1;
      const tops = new Set([...r.getClientRects()].map(x => Math.round(x.top)));
      if (tops.size > 1) problems.push(`"${w}" breaks across lines in "${node.textContent}"`);
      for (const x of r.getClientRects()) if (x.right > box.right + 0.5 || x.left < box.left - 0.5) problems.push(`"${w}" is cut by the menu`);
    }
    if (span.scrollWidth > span.clientWidth + 1) problems.push(`"${node.textContent}" overflows its box`);
  }
  pop.remove();
  return problems;
}'''


@pytest.mark.parametrize('width', [1440, 390])
def test_a_menu_item_shows_its_whole_label_and_hint_without_breaking_a_word(browser, live_server, width):
    """UX-RULE-LINES review, issue 1: the shared menu's hint never wrapped, so "Make stricter for this rota" broke over 4
    lines and a disabled reason was cut mid-word. A long label keeps its words whole, a long disabled reason wraps beneath
    it, and nothing is cut by the menu, at 1440 and 390."""
    context = new_context(browser, {'width': width, 'height': 900})
    try:
        page = open_app(context, live_server)
        page.wait_for_load_state('load')  # app.css applied (the sign-in page carries it)
        fonts_ready(page)
        problems = page.evaluate(MENU_PROBE, ['Make stricter for this rota',
                                              'It has no number to make stricter here; the department rule changes with approval.',
                                              '3 changes'])
        assert problems == [], problems
    finally:
        context.close()

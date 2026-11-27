"""Accessibility and front-end resource regressions that are cheap to check in a real browser."""
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, save_dialog  # noqa: F401

pytestmark = pytest.mark.browser
CONTROLS = {'button', 'link', 'textbox', 'searchbox', 'combobox', 'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'option', 'spinbutton', 'slider', 'listbox'}


def unnamed_controls(page):
    """Controls the browser's own accessibility tree gives no name (what a screen reader would announce as just "button")."""
    cdp = page.context.new_cdp_session(page)
    try:
        nodes = cdp.send('Accessibility.getFullAXTree')['nodes']
        bad = [n for n in nodes if not n.get('ignored') and (n.get('role') or {}).get('value') in CONTROLS
               and not ((n.get('name') or {}).get('value') or '').strip()]
        out = []
        for n in bad[:5]:
            node = cdp.send('DOM.resolveNode', {'backendNodeId': n['backendDOMNodeId']})['object']
            out.append(cdp.send('Runtime.callFunctionOn', {'objectId': node['objectId'], 'returnByValue': True,
                                                          'functionDeclaration': 'function(){return this.outerHTML.slice(0,200)}'})['result']['value'])
        return out
    finally:
        cdp.detach()


def test_every_control_on_key_pages_has_an_accessible_name(browser, live_server, h):
    w = h.shift(h.workspace('Named controls'), people=h.seed['people'][:2])['workspace']
    h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[])
    pages = {'admin': ['overview', 'people', 'configuration', 'access'], 'coordinator': ['planner', 'calendar', 'availability', 'rules'], 'staff': ['published', 'swaps']}
    for role, views in pages.items():
        for viewport in ({'width': 1440, 'height': 900}, {'width': 390, 'height': 844}):
            context, page = sign_in(browser, live_server, role, viewport)
            try:
                for view in views:
                    navigate(page, view)
                    assert unnamed_controls(page) == [], f'{role} {view} {viewport["width"]}px'
            finally:
                context.close()


OBSERVERS = """
// The client's one ResizeObserver (loading.ts observeSize): the boxes it watches.
window.__resizeObservers = new Set();
window.__observed = () => [...window.__resizeObservers].flatMap(w => [...w.__targets]);
const Real = window.ResizeObserver;
window.ResizeObserver = class extends Real {
  constructor(...a) { super(...a); this.__targets = new Set(); window.__resizeObservers.add(this); }
  observe(target, ...a) { super.observe(target, ...a); this.__targets.add(target); }
  unobserve(target) { super.unobserve(target); this.__targets.delete(target); }
  disconnect() { super.disconnect(); this.__targets.clear(); }
};
"""


def test_toolbar_fitting_observers_are_released_when_pages_change(browser, live_server, h):
    h.shift(h.workspace('Observer release'))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.add_init_script(OBSERVERS)
        page.reload()
        expect(page.get_by_role('button', name='Search or jump to')).to_be_visible()
        counts = []
        for _ in range(3):
            for view in ['overview', 'planner', 'people', 'rules', 'availability']:
                navigate(page, view)
                if view == 'people':
                    page.wait_for_function("() => window.__observed().some(n => n.tagName === 'TH' && n.isConnected)")
                    search = page.get_by_role('searchbox', name='Search people', exact=True)
                    search.fill('Staff 01')
                    expect(page.locator('#main tbody tr[data-row]')).to_have_count(1)
                    search.fill('')
                    expect(page.locator('#main tbody tr[data-row]')).to_have_count(len(h.seed['people']))
                # Boxes of the page left (rows, headings, scrollers, fields) leave the observer once it hears they went.
                page.wait_for_function("() => window.__observed().filter(n => n.tagName === 'TH').every(n => n.isConnected)")  # a function: an expression is eval'd under the CSP
            # Each round's observed boxes are kept (references only) for the failure message below.
            counts.append(page.evaluate('() => { const o = window.__observed(); (window.__obsRounds ||= []).push(o.slice()); return o.length; }'))
        # One observer, never growing from round to round.
        observers = page.evaluate('window.__resizeObservers.size')
        if not (observers == 1 and counts[2] <= counts[1]):
            # Diagnostic (failure only): each box still observed, its tag and class, whether it is in the page, and
            # whether it was already observed a round earlier (the leaked ones).
            leaked = page.evaluate('''() => { const [, before, last] = window.__obsRounds, earlier = new Set(before);
                const say = n => `${n.tagName.toLowerCase()}.${(n.getAttribute('class') || '').trim().replace(/\\s+/g, '.')}`
                    + `${n.isConnected ? '' : ' (detached)'}${earlier.has(n) ? ' (kept from round 2)' : ''}`
                    + ` in ${n.parentElement ? n.parentElement.tagName.toLowerCase() + '.' + (n.parentElement.getAttribute('class') || '').trim().split(/\\s+/)[0] : '-'}`;
                return last.filter(n => !n.isConnected || earlier.has(n)).map(say); }''')
            pytest.fail(f'observers={observers} counts={counts} leaked={leaked}')
    finally:
        context.close()


def test_phone_menu_is_keyboard_operable_and_hidden_when_closed(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 390, 'height': 844})
    try:
        navigate(page,'requests')
        # Closed, the off-screen menu is not in the tab order.
        expect(page.locator('#department-nav button').first).to_be_hidden()
        menu = page.get_by_role('button', name='Menu', exact=True)
        menu.focus()
        page.keyboard.press('Enter')
        current = page.locator('#department-nav [aria-current=page]')
        expect(current).to_be_focused()
        # maintenance (WCAG 4.1.2): the open drawer is a modal dialog named Menu, and only while open.
        drawer = page.get_by_role('dialog', name='Menu')
        expect(drawer).to_have_attribute('aria-modal', 'true')
        page.keyboard.press('Escape')
        expect(menu).to_be_focused()
        expect(menu).to_have_attribute('aria-expanded', 'false')
        expect(current).to_be_hidden()
        expect(drawer).to_have_count(0)
        # Its Close button gives the focus back to Menu too.
        page.keyboard.press('Enter')
        expect(current).to_be_focused()
        page.get_by_role('button', name='Close menu').click()
        expect(menu).to_be_focused()
        expect(page.get_by_role('dialog', name='Menu')).to_have_count(0)
    finally:
        context.close()


def test_planner_session_panel_takes_focus_and_gives_it_back(browser, live_server, h):
    h.shift(h.workspace('Panel focus'), people=h.seed['people'][:1])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        chip = page.locator('.wk-grid button.pchip:not(.gap)').first
        name = chip.get_attribute('aria-label')
        chip.focus()
        page.keyboard.press('Enter')
        expect(page.get_by_role('button', name='Close panel')).to_be_focused()
        page.keyboard.press('Escape')
        expect(page.locator('.wk-panel')).to_have_count(0)
        assert page.evaluate('document.activeElement.getAttribute("aria-label")') == name
    finally:
        context.close()


def test_group_toggle_keeps_keyboard_focus_and_phone_checkboxes_are_big_enough(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', {'width': 390, 'height': 844})
    try:
        navigate(page, 'access/history')
        toggle = page.locator('.dt-group-toggle').first
        group = toggle.get_attribute('data-group')
        toggle.focus()
        page.keyboard.press('Enter')
        # The table redraws; focus stays on the same group's (new) toggle, now collapsed, and Enter opens it again.
        again = page.locator(f'.dt-group-toggle[data-group="{group}"]')
        expect(again).to_be_focused()
        expect(again).to_have_attribute('aria-expanded', 'false')
        page.keyboard.press('Enter')
        expect(again).to_be_focused()
        expect(again).to_have_attribute('aria-expanded', 'true')
        # WCAG 2.5.8: row checkboxes are at least 24 px on a phone.
        navigate(page, 'people')
        boxes = page.locator('.bulk-check:visible')
        expect(boxes.first).to_be_visible()
        assert min(min(b['width'], b['height']) for b in boxes.evaluate_all(
            'bs => bs.map(b => b.getBoundingClientRect().toJSON())')) >= 24
    finally:
        context.close()


NON_TEXT = r"""() => {
  const rgb = c => { const m = c.match(/[\d.]+/g).map(Number); return m.slice(0, 3); };
  const lum = c => { const [r, g, b] = rgb(c).map(v => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
                     return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
  const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => p - q); return (y + 0.05) / (x + 0.05); };
  const host = document.createElement('div'); host.className = 'field'; document.querySelector('#main').append(host);
  const make = (tag, cls, parent = host) => { const n = document.createElement(tag); if (cls) n.className = cls; parent.append(n); return n; };
  const out = {}, surfaces = {page: getComputedStyle(document.body).backgroundColor};
  const card = make('div', 'panel'); surfaces.card = getComputedStyle(card).backgroundColor;
  const input = make('input'), select = make('select'), pick = make('button', 'pick-button'), search = make('div', 'dt-search-wrap');
  const sw = make('input', 'switch'); sw.type = 'checkbox';
  for (const [name, n] of Object.entries({input, select, pick, search})) {
    for (const [where, bg] of Object.entries(surfaces)) out[`${name} border on ${where}`] = ratio(getComputedStyle(n).borderTopColor, bg);
  }
  out['switch off track'] = ratio(getComputedStyle(sw).backgroundColor, surfaces.card);
  sw.checked = true; out['switch on track'] = ratio(getComputedStyle(sw).backgroundColor, surfaces.card);
  out['focus ring'] = ratio(getComputedStyle(document.documentElement).getPropertyValue('--accent').trim().replace(/^#(..)(..)(..)$/,
    (_, r, g, b) => `rgb(${parseInt(r, 16)},${parseInt(g, 16)},${parseInt(b, 16)})`), surfaces.page);
  for (const hue of ['teal', 'sky', 'violet', 'amber', 'slate', 'coral', 'indigo', 'pink', ...Array.from({length: 12}, (_, i) => 'rank' + (i + 1))]) {
    const chip = make('span', 'duty-' + hue), dot = make('i', '', chip);
    chip.style.setProperty('background', 'var(--duty-bg)'); dot.style.setProperty('background', 'var(--duty)');
    out[hue + ' dot on its tint'] = ratio(getComputedStyle(dot).backgroundColor, getComputedStyle(chip).backgroundColor);
  }
  host.remove(); return out;
}"""


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_control_boundaries_focus_and_duty_dots_have_3_to_1_contrast(browser, live_server, h, scheme):
    """WCAG 2.2 SC 1.4.11: what identifies a control or its state is at least 3:1 against what is next to it."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.emulate_media(color_scheme=scheme, reduced_motion='reduce')  # read end states, not mid-transition
        navigate(page, 'home')
        ratios = page.evaluate(NON_TEXT)
        low = {k: round(v, 2) for k, v in ratios.items() if v < 3}
        print(scheme, {k: round(v, 2) for k, v in ratios.items()})
        assert not low, low
    finally:
        context.close()


TEXT = r"""() => {
  const rgb = c => c.match(/[\d.]+/g).map(Number).slice(0, 3);
  const lum = c => { const [r, g, b] = rgb(c).map(v => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
                     return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
  const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => p - q); return (y + 0.05) / (x + 0.05); };
  // The colour a node is drawn on: its own background or the first ancestor's that is not transparent.
  const under = n => { for (let e = n; e; e = e.parentElement) { const c = getComputedStyle(e).backgroundColor;
                       if (!/rgba\(.*,\s*0\)$/.test(c) && c !== 'transparent') return c; } return getComputedStyle(document.body).backgroundColor; };
  const host = document.createElement('div'); host.className = 'panel'; document.querySelector('#main').append(host);
  const make = (html) => { const w = document.createElement('div'); w.innerHTML = html; host.append(w); return w.querySelector('[data-probe]'); };
  const out = {};
  // Text that was set in --faint (2.6:1): information, so WCAG 1.4.3 (4.5:1) applies.
  const probes = {
    'week summary count': '<span class="wk-none" data-probe>3 sessions</span>',
    'unstaffed session': '<span class="pchip empty" data-probe>No one yet</span>',
    'rule count': '<span class="rk-count" data-probe>12</span>',
    'unset number': '<span class="rl-num unset"><strong data-probe>—</strong></span>',
    'timeline card foot': '<div class="tl-card-foot" data-probe>2 people</div>',
    'empty coverage cell': '<div class="cov-cell none" data-probe>0</div>',
    'weekday off': '<button class="sr-day" data-probe>M</button>',
  };
  for (const tone of ['ok', 'warn', 'err', 'info', 'draft', 'neutral']) probes[`badge ${tone}`] = `<span class="badge ${tone}" data-probe>State</span>`;
  for (const [name, html] of Object.entries(probes)) { const n = make(html); out[name] = ratio(getComputedStyle(n).color, under(n)); }
  host.remove();
  const icon = document.querySelector('#save-state .icon');
  out['footer save icon'] = icon ? ratio(getComputedStyle(icon).color, under(icon)) : 0;
  return out;
}"""


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_information_text_and_status_icons_are_not_drawn_in_the_faint_token(browser, live_server, h, scheme):
    """WCAG 1.4.3 / 1.4.11 (audit X-25, N-27): week counts, "No one yet", Auto-schedule steps, rule counts, every badge
    tone read at 4.5:1; the footer's save-state icon, the only sign of the save state, at 3:1. --faint is decoration."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.emulate_media(color_scheme=scheme, reduced_motion='reduce')
        navigate(page, 'home')
        # D-4: the save state shows only after the person's own save (tests/test_browser_cycle5_fixes.py): make one.
        page.evaluate("window.importBundle('api.js').then(m => m.command('table_view.create', {table_key: 'people', name: 'Contrast', definition: {}}))")
        expect(page.locator('#save-state .icon')).to_be_attached()
        ratios = page.evaluate(TEXT)
        low = {k: round(v, 2) for k, v in ratios.items() if v < (3 if k == 'footer save icon' else 4.5)}
        assert not low, low
    finally:
        context.close()


FIRST_FIELD = ("() => {const d = [...document.querySelectorAll('dialog[open]')].pop(); const a = document.activeElement;"
               " const first = [...d.querySelectorAll('.dialog-body :is(input:not([type=hidden]):not([disabled]),select:not([disabled]),textarea:not([disabled]),.pick-button)')]"
               ".find(n => n.getClientRects().length && !n.closest('[inert],[hidden]'));"
               " return {inside: d.contains(a), close: a.matches('.dialog-head .icon-button'), first: a === first, label: a.getAttribute('aria-label') || a.id}}")


@pytest.mark.parametrize('role,page_key,opener,title', [
    ('admin', 'people', 'New person', 'New person'), ('staff', 'requests', 'New request', 'New request')])
def test_every_form_dialog_opens_on_its_first_field_and_gives_focus_back(browser, live_server, h, role, page_key, opener, title):
    """X-06 / R-22 / G-16: a form dialog opens on its first field, not on the header's Close; closing gives the focus back
    to its opener, and to the opener's replacement when the page redrew it meanwhile."""
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page, page_key)
        button = page.get_by_role('button', name=opener, exact=True)
        button.click()
        dialog = page.get_by_role('dialog', name=title)
        expect(dialog).to_be_visible()
        page.wait_for_function("() => document.activeElement && !document.activeElement.matches('.dialog-head .icon-button, body')")
        focus = page.evaluate(FIRST_FIELD)
        assert focus['inside'] and not focus['close'] and focus['first'], focus
        page.keyboard.press('Escape')
        expect(dialog).to_have_count(0)
        expect(button).to_be_focused()
        # A redraw replaces the opener (e.g. a polled panel): the focus follows it to the new control.
        page.evaluate("""name => {const b = [...document.querySelectorAll('button')].find(n => n.textContent.trim() === name);
            b.replaceWith(b.cloneNode(true));}""", opener)
        expect(page.get_by_role('button', name=opener, exact=True)).to_be_focused()
    finally:
        context.close()


def test_saving_a_record_gives_the_focus_back_to_that_records_edit_not_the_first_one(browser, live_server, h):
    """Every record has an "Edit" button and saving redraws the list, so the opener is replaced: the focus goes to the
    replacement in the same record row (data-row), never to the first equally named button on the page."""
    made = [h.configure('programmes', {'name': f'Focus programme {n}', 'starts_on': '2026-01-01'})['record'] for n in (1, 2)]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'programmes/programmes')
        second = page.locator(f'#main [data-row="{made[1]["id"]}"]')
        edit = second.get_by_role('button', name='Edit', exact=True)
        edit.focus()
        page.keyboard.press('Enter')
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Name', exact=True).fill('Focus programme 2 renamed')
        replaced = edit.element_handle()
        save_dialog(dialog)
        expect(dialog).to_have_count(0)
        page.wait_for_function('b => !b.isConnected', arg=replaced)  # the list was redrawn: the opener is a new node
        expect(second).to_contain_text('Focus programme 2 renamed')
        expect(edit).to_be_focused()
    finally:
        context.close()


TAB_STOPS = """root => [...root.querySelectorAll('button,a[href],input,select,textarea,[tabindex]')]
  .filter(n => n.tabIndex >= 0 && !n.disabled && n.getClientRects().length > 0).length"""


@pytest.mark.parametrize('layout', ['week', 'month'])
def test_the_schedule_grid_is_one_tab_stop_with_arrow_keys(browser, live_server, h, layout):
    """S-17 (APG grid, roving tabindex): the Week and Month grids are one Tab stop; arrows move between day headers, rows
    and names; Home/End go along the row; Shift+F10 opens the focused item's menu; Tab leaves the grid at once."""
    from .test_browser_schedule import october, show
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, f"schedule/{layout}/2026-10-14?edit={d['id']}")
        grid = page.locator('#schedule-view .wk-grid')
        expect(grid.locator('button.pchip').first).to_be_visible()
        # roving() settles the stop on the frame after the grid last changed (a whole-rota redraw, rule marks): wait for
        # that state, then the scroller holds exactly one stop (the scroller itself is not a stop either).
        page.wait_for_function(f"() => {{ const r = document.querySelector('#schedule-view .wk-scroller'); return !!r && ({TAB_STOPS})(r) === 1; }}")
        assert page.locator('#schedule-view .wk-scroller').evaluate(TAB_STOPS) == 1
        stop = grid.locator('[tabindex="0"]')
        expect(stop).to_have_count(1)
        stop.focus()
        first = page.evaluate('document.activeElement.outerHTML')
        page.keyboard.press('ArrowRight')
        moved = page.evaluate('document.activeElement.outerHTML')
        assert moved != first and grid.evaluate('g => g.contains(document.activeElement)')
        expect(grid.locator('[tabindex="0"]')).to_have_count(1)  # the stop follows focus
        page.keyboard.press('ArrowDown')
        page.keyboard.press('Home')
        home = page.evaluate('document.activeElement.getBoundingClientRect().left')
        page.keyboard.press('End')
        assert page.evaluate('document.activeElement.getBoundingClientRect().left') >= home
        page.keyboard.press('ControlOrMeta+Home')
        assert page.evaluate('document.activeElement === document.querySelector("#schedule-view .wk-grid [tabindex=\\"0\\"]")')
        # A name's menu from the keyboard (a draft name: published names are read-only).
        grid.locator('button.pchip:not(.sd-greychip):not(.gap)').first.focus()
        page.keyboard.press('Shift+F10')
        expect(page.get_by_role('menu').first).to_be_visible()
        page.keyboard.press('Escape')
        # Tab leaves the grid in one press.
        grid.locator('button.pchip').first.focus()
        page.keyboard.press('Tab')
        assert not grid.evaluate('g => g.contains(document.activeElement)')
    finally:
        context.close()


def test_person_chip_menu_is_discoverable_without_right_click(browser, live_server, h):
    """SR-38: a name shows its ⋯ on hover and keyboard focus; clicking it opens the name's menu (Shift+F10 for keys)."""
    w = h.shift(h.workspace('Chip menu affordance'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        chip = page.locator('.wk-grid button.pchip:not(.gap)').first
        more = chip.locator('.pchip-more')
        expect(more).to_be_hidden()
        chip.hover()
        expect(more).to_be_visible()
        more.click()
        expect(page.get_by_role('menu')).to_contain_text('Replace…')
        page.keyboard.press('Escape')
        chip.focus()
        page.keyboard.press('Shift+F10')
        expect(page.get_by_role('menu')).to_contain_text('Exchange with…')
    finally:
        context.close()


SMALL_TARGETS = r"""() => {
  // The shared small controls, built as the client builds them (FE-06, LIVE-13): each is a 24 px pointer target, or its
  // hit area (a ::before inset) reaches 24 px.
  const host = document.createElement('div'); document.querySelector('#main').append(host);
  host.innerHTML = '<div class="toast with-action"><span>Saved</span><button class="toast-action">Undo</button>'
    + '<button class="icon-button bare toast-close">x</button></div>'
    + '<button class="pill-more">+3</button><button class="chip-toggle">Show all</button>'
    + '<span class="role-chip">Lead<button class="chip-x">x</button></span>'
    + '<div class="wk-cell"><label class="wk-check-hit"><input type="checkbox" class="wk-check"></label></div>';
  host.getAnimations({subtree: true}).forEach(a => a.finish());  // measure at rest, not mid toast "pop" (scale .98)
  const out = {};
  for (const n of host.querySelectorAll('button,.wk-check-hit')) {
    const r = n.getBoundingClientRect(), before = getComputedStyle(n, '::before');
    const grow = before.content !== 'none' && before.position === 'absolute' ? -2 * (parseFloat(before.top) || 0) : 0;
    out[n.className] = Math.min(r.width, r.height) + grow;
  }
  host.remove(); return out;
}"""


def test_targets_min_24px_or_spaced(browser, live_server, h):
    """WCAG 2.5.8 Target Size (Minimum): toast Undo/close, "+N" pills, chip toggles, chip remove and session checkboxes."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'home')
        sizes = page.evaluate(SMALL_TARGETS)
        assert all(v >= 24 for v in sizes.values()), sizes
    finally:
        context.close()


PHONE = {'width': 390, 'height': 844}
TARGET_CORNERS = [(-21, -21), (21, -21), (21, 21), (-21, 21)]


def tap_corner(target, painted, offset):
    target.scroll_into_view_if_needed()
    hit, ink = target.bounding_box(), painted.bounding_box()
    assert hit and ink
    point = {'x': hit['width'] / 2 + offset[0], 'y': hit['height'] / 2 + offset[1]}
    x, y = hit['x'] + point['x'], hit['y'] + point['y']
    assert not (ink['x'] <= x < ink['x'] + ink['width'] and ink['y'] <= y < ink['y'] + ink['height'])
    target.tap(position=point)


def test_phone_labelled_checkbox_is_a_native_44px_target(browser, live_server):
    from .test_browser import new_context, open_app

    context = new_context(browser, PHONE, has_touch=True)
    try:
        page = open_app(context, live_server)
        form = page.locator('.login-form')
        form.get_by_label('Username or email').fill('staff')
        form.get_by_role('button', name='Continue', exact=True).click()
        password = form.get_by_label('Password', exact=True)
        checkbox = form.get_by_role('checkbox', name='Remember me', exact=True)
        expect(password).to_be_focused()
        page.keyboard.press('Tab')
        expect(checkbox).to_be_focused()
        page.keyboard.press('Space')
        expect(checkbox).to_be_checked()
        label = checkbox.locator('..')
        bounds = label.bounding_box()
        assert bounds and min(bounds['width'], bounds['height']) >= 44, bounds
        for index, corner in enumerate(TARGET_CORNERS):
            tap_corner(label, checkbox, corner)
            expect(checkbox).to_be_checked(checked=bool(index % 2))
            expect(form.get_by_role('button', name='Sign in', exact=True)).to_be_visible()
            expect(password).to_have_value('')
    finally:
        context.close()


def test_phone_collection_row_target_and_separate_select_all_menu(browser, live_server, h):
    from .test_browser import set_layout, whole_list
    from .test_browser_requests import day, leave

    for offset in (3, 4):
        leave(h, on=day(offset))
    context, page = sign_in(browser, live_server, 'approver', PHONE, has_touch=True)
    try:
        navigate(page, 'requests')
        whole_list(page)
        set_layout(page, 'Table')
        table = page.locator('#main .dt-table')
        boxes = table.locator('tbody td.dt-check .bulk-check')
        row, neighbour = boxes.nth(0), boxes.nth(1)
        expect(neighbour).to_be_visible()
        target = row.locator('..')
        target.scroll_into_view_if_needed()
        hit, ink = target.bounding_box(), row.bounding_box()
        assert hit and min(hit['width'], hit['height']) >= 44, hit
        assert ink and max(ink['width'], ink['height']) <= 24, ink
        for index, corner in enumerate(TARGET_CORNERS):
            tap_corner(target, row, corner)
            expect(row).to_be_checked(checked=not bool(index % 2))
            expect(neighbour).not_to_be_checked()
            expect(page.locator('.dock-panel:visible')).to_have_count(0)
        row.focus()
        page.keyboard.press('Space')
        expect(row).to_be_checked()
        expect(neighbour).not_to_be_checked()
        page.keyboard.press('Space')
        expect(row).not_to_be_checked()

        head = table.locator('.dt-check-head')
        checkbox = head.get_by_role('checkbox')
        menu = head.get_by_role('button', name='Selection options', exact=True)
        head.scroll_into_view_if_needed()
        check_box, menu_box = checkbox.bounding_box(), menu.bounding_box()
        assert check_box and min(check_box['width'], check_box['height']) >= 24, check_box
        assert menu_box and min(menu_box['width'], menu_box['height']) >= 24, menu_box
        assert check_box['x'] + check_box['width'] <= menu_box['x'], (check_box, menu_box)
        page.touchscreen.tap(check_box['x'] + check_box['width'] - 1, check_box['y'] + check_box['height'] / 2)
        expect(checkbox).to_be_checked()
        expect(row).to_be_checked()
        expect(neighbour).to_be_checked()
        options = page.get_by_role('menu', name='Selection options', exact=True)
        expect(options).not_to_be_visible()
        page.touchscreen.tap(menu_box['x'] + 1, menu_box['y'] + menu_box['height'] / 2)
        expect(options).to_be_visible()
        expect(checkbox).to_be_checked()
        expect(row).to_be_checked()
        expect(neighbour).to_be_checked()
    finally:
        context.close()


def test_phone_schedule_legend_overflow_has_a_44px_target_on_one_line(browser, live_server, h):
    from .test_browser_team_box import open_view

    w = h.workspace('Legend touch targets')
    for day in range(4, 8):
        duty = h.configure('assignment_types', {'name': f'Fictional duty {day} with a long title',
                                                'starts_on': '2020-01-01', 'definition': {}})['record']['id']
        w = h.shift(w, f'2026-10-0{day}T08:00:00+03:00', f'2026-10-0{day}T12:00:00+03:00',
                    people=[], typ=duty)['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', PHONE, has_touch=True)
    try:
        open_view(page, w, 'Week', 'Grid')
        legend = page.locator('#schedule-view .view-legend:visible')
        more = legend.locator('.lg-more .pill-more')
        expect(more).to_be_visible()
        mark = more.locator('.lg-more-mark')
        hit = more.bounding_box()
        assert hit and min(hit['width'], hit['height']) >= 44, hit
        assert more.evaluate('button => button.scrollWidth <= button.clientWidth + 1')
        assert legend.evaluate("""key => {
          const bounds = key.getBoundingClientRect();
          const items = [...key.children].filter(item => !item.hidden).map(item => item.getBoundingClientRect());
          return bounds.height >= 44 && key.scrollWidth <= key.clientWidth + 1
            && items.every(item => item.top < items[0].bottom && item.bottom > items[0].top)
            && items.slice(1).every((item, index) => item.left >= items[index].right);
        }""")
        panel = page.get_by_role('dialog', name=f"All {legend.get_attribute('aria-label')}", exact=True)
        for corner in TARGET_CORNERS:
            tap_corner(more, mark, corner)
            expect(panel).to_be_visible()
            page.keyboard.press('Escape')
            expect(panel).not_to_be_visible()
        more.focus()
        for key in ('Enter', 'Space'):
            page.keyboard.press(key)
            expect(panel).to_be_visible()
            page.keyboard.press('Escape')
            expect(panel).not_to_be_visible()
            expect(more).to_be_focused()
    finally:
        context.close()


DESKTOP = {'width': 1440, 'height': 900}
HIT_AREA = r"""nodes => nodes.map(n => {
  // The region that takes the pointer: the box, grown by an absolute ::before (a pseudo-element's clicks land on its
  // element); each corner of it must land on the control itself, never on a neighbour or a clipping parent.
  n.scrollIntoView({block: 'center'});
  const r = n.getBoundingClientRect(), b = getComputedStyle(n, '::before');
  const grown = b.content !== 'none' && b.position === 'absolute';
  const x = grown ? Math.max(0, -parseFloat(b.left) || 0) : 0, y = grown ? Math.max(0, -parseFloat(b.top) || 0) : 0;
  const box = {left: r.left - x, top: r.top - y, right: r.right + x, bottom: r.bottom + y};
  // What each corner that misses lands on instead, so a failure names the neighbour or parent in the way.
  const misses = [[box.left + 1, box.top + 1], [box.right - 1, box.top + 1], [box.right - 1, box.bottom - 1], [box.left + 1, box.bottom - 1]]
    .map(([px, py]) => [px, py, document.elementFromPoint(px, py)]).filter(([, , hit]) => !hit || !(hit === n || n.contains(hit)))
    .map(([px, py]) => `${Math.round(px)},${Math.round(py)} ` + document.elementsFromPoint(px, py).slice(0, 3)
      .map(e => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + [...e.classList].map(c => '.' + c).join('')).join(' < '));
  const name = n.getAttribute('aria-label') || n.querySelector('[aria-label]')?.getAttribute('aria-label') || n.textContent.trim();
  return {name, width: box.right - box.left, height: box.bottom - box.top, lands: !misses.length, misses};
})"""
# The Rules list's row and head, a line, a line panel's reach links and the legend's "+N", built as the client builds
# them (rules_tree.ts, rule_catalogue.ts, rule_line_reach.ts, dom.ts legend), in #main so the work container applies.
RULE_CONTROLS = r"""() => {
  const host = document.createElement('div'); host.id = 'probe-targets'; document.querySelector('#main').prepend(host);
  const box = label => `<label class="dt-check-hit"><input type="checkbox" class="bulk-check" aria-label="${label}"></label>`;
  host.innerHTML = '<div class="rt"><div class="rt-lhead">' + box('Select all lines shown')
    + '<button type="button" class="icon-button bare rt-toggle" aria-label="All rules">&gt;</button><span>Rule</span></div>'
    + '<ul class="rt-list"><li class="rt-item"><div class="rt-row">' + box('Select a rule')
    + '<button type="button" class="icon-button bare rt-toggle" aria-label="A rule lines">&gt;</button>'
    + '<input type="checkbox" role="switch" class="switch" aria-label="A rule on or off" checked>'
    + '<div class="rt-main"><div class="rt-title">A rule</div></div></div>'
    + '<ul class="rt-lines"><li class="rt-ln">' + box('Select line 1 of a rule')
    + '<span class="rt-n">1</span><span class="rt-ln-who">Everyone</span></li></ul></li></ul></div>'
    + '<p>Applies on <button type="button" class="rc-reach-link">line 3</button> to'
    + ' <button type="button" class="rc-reach-link">1 person</button>.</p>'
    + '<ul class="view-legend" aria-label="Key"><li>Key</li><li class="lg-more">'
    + '<button type="button" class="pill-more" aria-label="3 more"><span class="lg-more-mark" aria-hidden="true">+3</span></button></li></ul>';
}"""


@pytest.mark.parametrize('viewport', [DESKTOP, PHONE], ids=['1440', '390'])
def test_selection_boxes_switches_links_and_search_reach_the_target_size(browser, live_server, h, viewport):
    """Issue 1341 (WCAG 2.2 SC 2.5.8; 44 px on a phone, DESIGN.md Touch targets): a row's and select-all's selection box,
    a person's name in a list, the Search field, the Rules list's box, open and on/off, a line's box, the legend's "+N"
    and a line panel's reach links (a link inside a sentence: 24 px at both widths) each take the whole target."""
    from .test_browser import set_layout, whole_list
    from .test_browser_requests import day, leave

    phone = viewport == PHONE
    size = 44 if phone else 24
    for offset in (3, 4):
        leave(h, on=day(offset))
    context, page = sign_in(browser, live_server, 'approver', viewport, has_touch=phone)

    def measure(locator, at_least=size):
        expect(locator.first).to_be_attached()
        found = locator.evaluate_all(HIT_AREA)
        small = [t for t in found if min(t['width'], t['height']) < at_least - 0.01 or not t['lands']]
        assert found and not small, f"{viewport['width']}: " + '; '.join(f"{t['name']} {t['width']:.0f}x{t['height']:.0f} misses {t['misses']}" for t in small)

    try:
        navigate(page, 'requests')
        whole_list(page)
        main = page.locator('#main')
        measure(main.locator('.dt-toolbar .dt-search:visible'))
        set_layout(page, 'Table')
        table = main.locator('.dt-table')
        measure(table.locator('tbody td.dt-check .dt-check-hit'))
        measure(table.locator('th .dt-check-head .dt-check-hit'))
        measure(table.locator('tbody td .link-button.person-link:not(.pchip):visible'))
        set_layout(page, 'Cards')
        cards = main.locator('.dt-card')
        measure(cards.locator('.dt-check-hit'))
        measure(cards.locator('.link-button.person-link:not(.pchip):visible'))
        page.evaluate(RULE_CONTROLS)
        probe = page.locator('#probe-targets')
        measure(probe.locator('.rt-lhead > .dt-check-hit, .rt-lhead > .rt-toggle'))
        measure(probe.locator('.rt-row > .dt-check-hit, .rt-row > .rt-toggle, .rt-row > input.switch'))
        measure(probe.locator('.rt-ln > .dt-check-hit'))
        measure(probe.locator('.view-legend .pill-more'))
        measure(probe.locator('.rc-reach-link'), 24)
        # The label still ticks its box from its edge, and the switch turns from beyond its drawn track.
        row_box = probe.locator('.rt-row > .dt-check-hit')
        hit = row_box.bounding_box()
        assert hit
        page.mouse.click(hit['x'] + 1, hit['y'] + 1)
        expect(row_box.get_by_role('checkbox')).to_be_checked()
        switch = probe.get_by_role('switch', name='A rule on or off')
        track = switch.bounding_box()
        assert track
        page.mouse.click(track['x'] + track['width'] / 2, track['y'] + track['height'] / 2 - (size / 2 - 1))
        expect(switch).not_to_be_checked()
    finally:
        context.close()


def test_disabled_primary_is_distinguishable_dark(browser, live_server, h):
    """LIVE-12: a disabled primary button (Rules › Propose with nothing to propose) keeps its disabled look under the pointer."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.emulate_media(color_scheme='dark', reduced_motion='reduce')
        navigate(page, 'rules')
        # Propose exists only with the rule catalogue on (off here): the shared style is checked on a disabled primary.
        page.evaluate("() => { const b = document.createElement('button'); b.className = 'button primary'; b.id = 'probe-disabled';"
                      " b.textContent = 'Propose'; b.disabled = true; document.querySelector('#main').prepend(b); }")
        propose = page.locator('#probe-disabled')
        expect(propose).to_be_visible()
        propose.hover(force=True)
        look = propose.evaluate("b => { const p = document.createElement('button'); p.className = 'button primary'; b.after(p);"
                                " const on = getComputedStyle(p).backgroundColor; p.remove(); return [getComputedStyle(b).backgroundColor, on]; }")
        assert look[0] != look[1], look
    finally:
        context.close()


def test_focus_not_obscured_by_sticky_bars(browser, live_server, h):
    """WCAG 2.4.11 (maintenance): a control tabbed to at the bottom of a long page stops above the status footer (and below the top bar)."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'home')
        page.evaluate("""() => { const m = document.querySelector('#main');
          const a = document.createElement('button'); a.id = 'probe-a'; a.textContent = 'A';
          const gap = document.createElement('div'); gap.className = 'probe-gap';
          const b = document.createElement('button'); b.id = 'probe-b'; b.textContent = 'B';
          m.append(a, gap, b); gap.style.setProperty('height', '3000px'); a.focus(); window.scrollTo(0, 0); }""")
        page.keyboard.press('Tab')
        expect(page.locator('#probe-b')).to_be_focused()
        place = page.evaluate("""() => ({b: document.querySelector('#probe-b').getBoundingClientRect().toJSON(),
          foot: document.querySelector('.app-footer').getBoundingClientRect().top,
          top: document.querySelector('.topbar').getBoundingClientRect().bottom})""")
        assert place['b']['bottom'] <= place['foot'] and place['b']['top'] >= place['top'], place
    finally:
        context.close()


def test_focus_visible_in_forced_colors(browser, live_server, h):
    """maintenance: Windows High Contrast drops box-shadow rings; a focused field and picker button still draw an outline."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.emulate_media(forced_colors='active')
        navigate(page, 'home')
        styles = page.evaluate("""() => { const host = document.createElement('div'); host.className = 'field';
          document.querySelector('#main').append(host); const out = {};
          for (const [tag, cls] of [['input', ''], ['button', 'pick-button'], ['button', 'button']]) {
            const n = document.createElement(tag); if (cls) n.className = cls; host.append(n); n.focus({focusVisible: true});
            const s = getComputedStyle(n); out[tag + '.' + cls] = [s.outlineStyle, parseFloat(s.outlineWidth)]; }
          host.remove(); return out; }""")
        assert all(style != 'none' and width >= 2 for style, width in styles.values()), styles
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_role_chips_are_neutral(browser, live_server, h, scheme):
    """LIVE-09: access roles are one neutral slate chip whatever the role (DESIGN: colour is for duty types)."""
    context, page = sign_in(browser, live_server, 'admin', color_scheme=scheme)
    try:
        navigate(page, 'access/people')
        chips = page.locator('#main .role-chip')
        expect(chips.first).to_be_visible()
        assert chips.evaluate_all("cs => cs.every(c => c.classList.contains('duty-slate'))")
        assert len(set(chips.evaluate_all("cs => cs.map(c => getComputedStyle(c).backgroundColor)"))) == 1
    finally:
        context.close()


def test_escape_from_account_menu_folds_the_rail(browser, live_server, h):
    """LIVE-49: the account menu opened from the folded rail closes with Escape and takes the rail's overlay with it."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        account = page.locator('.sidebar .account-button')
        account.focus()
        page.keyboard.press('Enter')
        expect(page.get_by_role('dialog', name='Account')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.get_by_role('dialog', name='Account')).to_have_count(0)
        expect(page.locator('.sidebar.peek')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('role,page_key,opener', [
    ('coordinator', 'rules', '.dt-tool[aria-label^="Filter"]'),       # maintenance: a filter popover with a select inside
    ('admin', 'people', '.dt-tool[aria-label^="View settings"]'),       # a view-settings popover (the v1 Rules list has none)
    ('admin', 'people', 'button.dt-more'),                              # a row's menu()
])
def test_closing_a_popover_or_menu_with_escape_gives_the_focus_back_to_its_button(browser, live_server, h, role, page_key, opener):
    """maintenance (WCAG 2.4.3): Escape closes a value list first, then its popover, and the focus is on the button that opened
    it, never on a field of the closed panel. ArrowDown opens a focused select's list; on anything else it does nothing,
    so the second Escape then lands on the button, which keeps the focus."""
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page, page_key)
        button = page.locator(f'#main {opener}').first
        button.focus()
        page.keyboard.press('Enter')
        expect(button).to_have_attribute('aria-expanded', 'true')
        page.keyboard.press('ArrowDown')
        page.keyboard.press('Escape')
        page.keyboard.press('Escape')
        expect(page.locator('.popover, .picker')).to_have_count(0)
        expect(button).to_have_attribute('aria-expanded', 'false')
        expect(button).to_be_focused()
    finally:
        context.close()


TAB_STOP = """() => { const a = document.activeElement;
  const col = a.closest('#main .home-main') ? 'main' : a.closest('#main .home-side') ? 'side' : a.closest('.app-footer') ? 'footer' : 'other';
  return {col, top: a.getBoundingClientRect().top + scrollY, name: (a.getAttribute('aria-label') || a.textContent || a.tagName).trim().slice(0, 40)}; }"""


def test_home_tab_order_reads_column_by_column_and_footer_status_is_not_a_tab_stop(browser, live_server, h):
    """maintenance (WCAG 1.3.2, 2.4.3): Tab reads Home's main column top to bottom, then the side column, never back; the
    footer's connection, save state and environment badge do nothing, so they are no Tab stops (their names stay)."""
    h.shift(h.workspace('Home order'), people=h.seed['people'][:2])
    # The side column needs a control of its own to be read after the main one: a notice with a link (a coordinator
    # with no staff profile has no duties or calendar card there, and nobody is away).
    from datetime import date
    from apps.api import models as m
    from apps.api.db import transaction
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.DepartmentContent(department_id=h.seed['department_id'], name='Home order notice', starts_on=date(2020, 1, 1),
                                   url='https://example.org/home-order'))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'home')
        expect(page.locator('#main .home-grid .home-side')).to_be_visible()
        expect(page.locator('#main .home-side').get_by_role('link', name='Open link: Home order notice')).to_be_visible()
        page.locator('#main').focus()
        stops = []
        for _ in range(150):  # until Tab has passed the footer
            page.keyboard.press('Tab')
            stops.append(page.evaluate(TAB_STOP))
            if stops[-1]['col'] == 'other' and any(s['col'] == 'footer' for s in stops):
                break
        cols = [s['col'] for s in stops if s['col'] in ('main', 'side')]
        assert 'main' in cols and 'side' in cols, stops
        assert cols == sorted(cols, key=['main', 'side'].index), [(s['col'], s['name']) for s in stops]
        for col in ('main', 'side'):
            tops = [s for s in stops if s['col'] == col]
            back = [(a['name'], b['name']) for a, b in zip(tops, tops[1:]) if b['top'] < a['top'] - 4]
            assert not back, back
        footer = page.locator('.app-footer')
        for status in (footer.locator('#connection'), footer.locator('#save-state'), footer.locator('.env-badge')):
            expect(status).to_have_js_property('tabIndex', -1)
    finally:
        context.close()

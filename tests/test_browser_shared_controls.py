"""Shared controls reviewed: a search box on every picker list, the one multiple-choice field (chips
that fit its real width, then "+N"), resizable side panels (360px … 60% of the window, remembered, none on a phone) and
the footer that moves what does not fit behind "? +N". Behavioural checks on desktop and phone, light and dark.

SHARED_SHOTS=<folder>: also save a screenshot of each control in each mode there."""
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser
MODES = [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')]
SHOTS = os.environ.get('SHARED_SHOTS')


def signed_in(browser, live_server, width, scheme, role='coordinator', where='people'):
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 900 if width > 400 else 844})
    page.emulate_media(color_scheme=scheme)
    navigate(page, where)
    return context, page


def fixture(page, html):
    page.evaluate("""html => {document.querySelector('#ui-fixture')?.remove();
      const f=document.createElement('div');f.id='ui-fixture';f.className='panel';f.innerHTML=html;document.querySelector('#main').prepend(f);}""", html)


def shot(page, name, width, scheme, target=None):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        path = str(Path(SHOTS) / f'{name}-{"desktop" if width > 400 else "phone"}-{scheme}.png')
        (target.screenshot(path=path) if target else page.screenshot(path=path))


@pytest.mark.parametrize('width,scheme', MODES[::3], ids=['desktop-light', 'phone-dark'])
def test_a_short_list_has_a_search_box_too(browser, live_server, h, width, scheme):
    context, page = signed_in(browser, live_server, width, scheme)
    try:
        fixture(page, '<div id="short"></div>')
        page.evaluate("""async () => {const {select}=await window.importBundle('dom.js');
          const f=select('Colour',[{value:'r',label:'Red'},{value:'g',label:'Green'},{value:'b',label:'Blue'}],'r');
          document.querySelector('#short').append(f.wrap);}""")
        colour = page.get_by_label('Colour', exact=True)
        colour.focus()
        page.keyboard.press('ArrowDown')
        picker = page.locator('.picker')
        search = picker.get_by_role('combobox', name='Search Colour')
        expect(search).to_be_focused()  # three choices, still a search box
        expect(picker.get_by_role('option')).to_have_count(3)
        page.keyboard.type('gre')
        expect(picker.get_by_role('option')).to_have_count(1)
        page.keyboard.press('Enter')
        expect(picker).to_have_count(0)
        expect(colour).to_have_value('g')
        expect(colour).to_be_focused()
        # A key typed on the closed field starts the search with it.
        page.keyboard.press('b')
        expect(search).to_have_value('b')
        shot(page, 'picker-search', width, scheme, page.locator('.picker') if width > 400 else None)
        page.keyboard.press('Escape')
        page.keyboard.press('Escape')
        expect(picker).to_have_count(0)
    finally:
        context.close()


MULTI = """async ([width, chosen]) => {const {multiPick}=await window.importBundle('dom.js');
  const host=document.querySelector('#multi');host.style.setProperty('width',width+'px');
  const names=['Physical Urgent Care','Virtual UCC','Pediatrics Clinic','Diabetes Clinic','Women health clinic','Travel medicine'];
  const f=multiPick('Clinics',names.map((label,i)=>({value:'c'+i,label,tone:'place'})),chosen,undefined,'All clinics');
  window.multiField=f;host.replaceChildren(f.wrap);}"""


@pytest.mark.parametrize('width,scheme', MODES, ids=['desktop-light', 'desktop-dark', 'phone-light', 'phone-dark'])
def test_multi_value_field_shows_what_fits_then_plus_n(browser, live_server, h, width, scheme):
    context, page = signed_in(browser, live_server, width, scheme)
    try:
        fixture(page, '<div id="multi"></div>')
        page.evaluate(MULTI, [300 if width > 400 else 180, ['c0', 'c1', 'c2']])
        field = page.get_by_role('button', name=re.compile('^Clinics:'))
        expect(field).to_have_accessible_name('Clinics: Physical Urgent Care, Virtual UCC, Pediatrics Clinic')
        box = field.locator('.pick-values')
        chips = field.locator('.pick-chip:visible')
        # Narrow: the first chips and "+N", whose tooltip lists the rest; nothing clipped or overflowing.
        more = field.locator('.pick-more')
        expect(more).to_be_visible()
        hidden = 3 - chips.count()
        assert hidden >= 1
        expect(more).to_have_text(f'+{hidden}')
        rest = ['Physical Urgent Care', 'Virtual UCC', 'Pediatrics Clinic'][3 - hidden:]
        expect(more).to_have_attribute('title', ', '.join(rest))
        assert box.evaluate('b => b.scrollWidth <= b.clientWidth + 1')
        assert field.evaluate('f => f.scrollWidth <= f.clientWidth + 1')
        # As tall as any dropdown field: on a phone, the shared 44px touch target (86193d779, WCAG 2.5.5).
        low, high = (34, 40) if width > 720 else (44, 45)
        assert low <= field.bounding_box()['height'] <= high
        shot(page, 'multi-narrow', width, scheme, page.locator('#multi'))
        # Wider: re-flows on resize (real widths), every chip shows and "+N" goes.
        narrow = chips.count()
        page.evaluate("document.querySelector('#multi').style.setProperty('width',Math.min(620,innerWidth-70)+'px')")
        if width > 400:
            expect(more).to_be_hidden()
            expect(chips).to_have_count(3)
        else:
            expect(chips).to_have_count(narrow + 1)
        assert box.evaluate('b => b.scrollWidth <= b.clientWidth + 1')
        # Keyboard: Enter opens the shared picker (multiple choice); a short list keeps its order, the chosen ticked (X-37).
        field.focus()
        page.keyboard.press('Enter')
        picker = page.locator('.picker')
        expect(picker.get_by_role('combobox', name='Search Clinics')).to_be_focused()
        expect(picker.get_by_role('option', checked=True)).to_have_count(3)
        shot(page, 'multi-open', width, scheme)
        page.keyboard.type('diab')
        page.keyboard.press('Enter')  # ticks Diabetes Clinic
        page.keyboard.press('Escape')
        page.keyboard.press('Escape')
        expect(picker).to_have_count(0)
        expect(field).to_be_focused()
        expect(field.locator('.pick-chip')).to_have_count(4)
        # The hidden <select multiple> holds the value for forms and the unsaved-changes guard.
        assert page.evaluate("[...document.querySelector('#multi select').selectedOptions].map(o=>o.value)") == ['c0', 'c1', 'c2', 'c3']
        assert page.evaluate('window.multiField.values()') == ['c0', 'c1', 'c2', 'c3']
        # Space opens it too; Clear empties it and the field says so.
        page.keyboard.press(' ')
        picker.get_by_role('button', name=re.compile('^Clear')).click()
        page.keyboard.press('Escape')
        expect(field).to_have_accessible_name('Clinics: All clinics')
        expect(field.locator('.pick-text')).to_have_text('All clinics')
    finally:
        context.close()


def test_side_panel_limits_keys_and_sign_out(browser, live_server, h):
    context, page = signed_in(browser, live_server, 1440, 'light')
    try:
        page.locator('#main .dt tbody tr[data-row]').first.locator('strong').first.click()
        panel = page.locator('.dock-panel')
        grip = panel.get_by_role('separator', name='Resize panel')
        expect(grip).to_have_attribute('aria-orientation', 'vertical')
        expect(grip).to_have_attribute('aria-valuemin', '360')
        top = int(grip.get_attribute('aria-valuemax'))
        assert top <= 1440 * 0.6
        grip.focus()
        page.keyboard.press('End')
        expect(grip).to_have_attribute('aria-valuenow', str(top))
        assert abs(panel.bounding_box()['width'] - top) <= 1
        assert page.locator('#main').bounding_box()['width'] >= 360  # the page beside it never disappears
        shot(page, 'panel-wide', 1440, 'light')
        page.keyboard.press('Home')
        expect(grip).to_have_attribute('aria-valuenow', '360')
        page.keyboard.press('ArrowLeft')
        expect(grip).to_have_attribute('aria-valuenow', '376')
        grip.hover()
        shot(page, 'panel-grip', 1440, 'light', panel)
        assert page.evaluate("localStorage.getItem('rota.panel.person')") == '376'
        # Sign-out forgets panel widths with the other per-browser settings.
        page.evaluate("async () => {const {forgetPanelWidths}=await window.importBundle('side_panel.js');forgetPanelWidths();}")
        assert page.evaluate("Object.keys(localStorage).filter(k=>k.startsWith('rota.panel.'))") == []
    finally:
        context.close()


FOOTER = """async () => {const {footerStatus,footerKey,liftFooterStatus,el}=await window.importBundle('dom.js');
  const content=document.createElement('div');
  content.append(footerStatus(el('span',{class:'muted'},'12 lines · 1 change not yet proposed'),el('span',{class:'muted'},'Week 41 · Sun 5 – Sat 11 Oct 2026'),
    el('span',{class:'muted'},'48 sessions'),el('span',{class:'muted'},'3 changes from published'),el('span',{class:'muted'},'Asia/Riyadh time'),
    footerKey(p=>p.append(el('p',{},'What the marks mean')))));
  liftFooterStatus(content);}"""
CUT = """() => {const slot=document.querySelector('.app-footer .page-status'),s=slot.getBoundingClientRect(),bad=[];
  for(const n of slot.querySelectorAll('.footer-status>*')){if(!n.getClientRects().length)continue;const r=n.getBoundingClientRect();
    if(r.right>s.right+1||n.scrollWidth>n.clientWidth+1)bad.push(n.textContent);}
  return bad;}"""


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_footer_moves_what_does_not_fit_behind_the_key(browser, live_server, h, scheme):
    context, page = signed_in(browser, live_server, 1440, scheme)
    try:
        page.evaluate(FOOTER)
        status = page.locator('.app-footer .footer-status')
        key = status.locator('.footer-key')
        expect(status.locator(':scope > span:visible')).to_have_count(5)  # 1440: every item inline
        expect(key).to_have_text('Key')
        assert page.evaluate(CUT) == []
        shot(page, 'footer', 1440, scheme, page.locator('.app-footer'))
        seen = 5
        for width in (1024, 700, 390):
            page.set_viewport_size({'width': width, 'height': 844 if width < 500 else 900})
            shown = status.locator(':scope > span:visible')
            if width < 720:  # a phone: the main count and the button
                expect(shown).to_have_count(1)
            else:
                expect(key).to_have_text(re.compile(r'^(Key|\+\d)$'))
            n = shown.count()
            assert n <= seen
            seen = n
            if n < 5:
                expect(key).to_have_text(f'+{5 - n}')
                # D-116 (dd50e1e04): the one "+N" (dom.ts overflowMore) names what it hides
                expect(key).to_have_accessible_name(f"{5 - n} more status item{'' if 5 - n == 1 else 's'} and the key")
            assert page.evaluate(CUT) == [], width
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            shot(page, f'footer-{width}', width if width > 400 else 390, scheme, page.locator('.app-footer'))
        # The popover lists the hidden items (same words) above the key, then puts them back.
        key.click()
        pop = page.get_by_role('dialog', name='Key')
        hidden = pop.get_by_role('list', name='More status')
        expect(hidden.get_by_role('listitem')).to_have_count(4)
        expect(hidden).to_contain_text('Asia/Riyadh time')
        expect(pop).to_contain_text('What the marks mean')
        shot(page, 'footer-open', 390, scheme)
        page.keyboard.press('Escape')
        expect(pop).to_have_count(0)
        expect(key).to_have_text('+4')
        # Back to a desktop width: everything inline again, the key reads Key.
        page.set_viewport_size({'width': 1440, 'height': 900})
        expect(status.locator(':scope > span:visible')).to_have_count(5)
        expect(key).to_have_text('Key')
    finally:
        context.close()


def panel_has_handle(page, kind, desktop):
    """The panel just opened (and every other side panel on screen) carries the shared handle on desktop, none on a
    phone; no <aside> beside the page is docked any other way."""
    panels = page.locator('.side-panel:visible')
    expect(panels.first).to_be_visible()
    for k in range(panels.count()):
        grip = panels.nth(k).locator(':scope > .panel-grip')
        expect(grip).to_have_count(1)
        if desktop:
            expect(grip).to_be_visible()
            expect(grip).to_have_attribute('role', 'separator')
            expect(grip).to_have_attribute('aria-valuemin', '360')
        else:
            expect(grip).to_be_hidden()
    assert page.evaluate("""() => [...document.querySelectorAll('aside')].filter(a => a.getClientRects().length
      && !a.matches('.sidebar, .setup-aside, .side-panel')).map(a => a.className)""") == [], kind


def close_panels(page):
    panels = page.locator('.side-panel:visible')
    for _ in range(3):
        n = panels.count()
        if not n:
            return
        page.keyboard.press('Escape')
        try:
            expect(panels).not_to_have_count(n, timeout=1500)
        except AssertionError:  # this panel does not close on Escape: its Close button below
            break
    for b in page.locator('.side-panel:visible').get_by_role('button', name=re.compile('^Close')).all():
        if b.is_visible():
            b.click()


@pytest.mark.parametrize('width', [1440, 390], ids=['desktop', 'phone'])
def test_every_side_panel_has_the_resize_handle_on_desktop_only(browser, live_server, h, width):
    from .test_browser import open_config, open_rota, pick_view, planner_tool
    from .test_browser_rule_catalogue import flag, weekly
    desktop = width > 400
    a = h.seed['people'][0]
    request = h.command('staff', 'request.create', {'person_id': a, 'start': '2026-10-12T08:00:00+03:00', 'end': '2026-10-12T17:00:00+03:00', 'private_reason': 'Panel check'})['record']
    h.configure('tags', {'name': 'Panel_check', 'labels': ['people'], 'description': 'Panel check tag'})
    flag(h)
    weekly(h)
    w = h.shift(h.workspace('Panel handles'), people=h.seed['people'][:1])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900 if desktop else 844})
    checked = []

    def check(kind):
        panel_has_handle(page, kind, desktop)
        checked.append(kind)
    try:
        navigate(page, 'people')
        page.locator('#main .dt tbody tr[data-row] strong, #main .dt-card[data-row] .dt-card-title').first.click()
        check('person')
        close_panels(page)
        page.evaluate("id => location.hash = 'requests?open=' + id", request['id'])
        expect(page.locator('.dock-panel')).to_be_visible()
        check('request')
        close_panels(page)
        open_config(page, 'Tags')
        page.locator('.config-pane [data-row]').filter(has_text='Panel_check').get_by_text('Panel check tag').click()
        check('tag')
        close_panels(page)
        navigate(page, 'rules')
        page.get_by_role('button', name='Duties per week lines', exact=True).click()
        page.get_by_role('button', name='Open line 1 of Duties per week', exact=True).click()
        expect(page.locator('.rc-line-panel')).to_be_visible()
        check('rule-line')
        close_panels(page)
        navigate(page, 'planner')
        open_rota(page, w['id'])
        expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)
        if desktop:
            pick_view(page, 'Week')
            page.locator('button.pchip').first.click()
            expect(page.locator('.wk-panel')).to_be_visible()
            check('schedule')
            page.locator('.wk-panel').get_by_role('button', name='Whole session').click()
            page.locator('.wk-panel').get_by_role('button', name='Edit session', exact=True).click()
            expect(page.locator('.dock-panel').get_by_role('heading', name='Edit session and participants')).to_be_visible()
            check('session-edit')
            close_panels(page)
            page.get_by_role('button', name='How to read this schedule').click()
            expect(page.locator('.wk-panel')).to_be_visible()
            check('schedule help')
            close_panels(page)
            page.get_by_role('button', name='Auto-schedule', exact=True).click()
            expect(page.locator('#auto-panel')).to_be_visible()
            check('auto-schedule')
            close_panels(page)
            planner_tool(page, 'Rules in this rota')
            expect(page.locator('.side-panel:visible')).to_have_count(1)
            check('rules in this rota')
            close_panels(page)
            pick_view(page, 'Year')
            expect(page.locator('.sd-panel')).to_be_visible()
            check('day-summary')
            shot(page, 'panel-day-summary', width, 'light')
        else:
            page.locator('button.pchip, .ag-item, .agenda-item').first.click()
            if page.locator('.side-panel:visible').count():
                check('session (phone)')
        assert checked[:4] == ['person', 'request', 'tag', 'rule-line'], checked
        if desktop:
            assert len(checked) == 10, checked  # every kind above (session details are a mode of the session panel, S-09)
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_panel_keeps_its_default_width_and_the_page_its_360px(browser, live_server, h, scheme):
    from .test_browser_overflow import GUARD, ALLOW
    context, page = signed_in(browser, live_server, 1440, scheme)
    try:
        page.evaluate("Object.keys(localStorage).filter(k=>k.startsWith('rota.panel.')).forEach(k=>localStorage.removeItem(k))")
        page.locator('#main .dt tbody tr[data-row]').first.locator('strong').first.click()
        panel = page.locator('.dock-panel')
        expect(panel).to_be_visible()
        assert abs(panel.bounding_box()['width'] - 640) <= 1  # the person panel's own default, untouched by the 60% limit
        shot(page, 'panel-default-1440', 1440, scheme)
        # 1024: the default gives way only so the page beside keeps 360px; nothing in it overflows or is cut.
        page.set_viewport_size({'width': 1024, 'height': 900})
        # Off the panel and off the rail (hovering the rail opens its peek, which widens over 180 ms after 320 ms).
        page.mouse.move(200, 1)
        page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
        page_box = page.locator('.workspace-shell').bounding_box()
        assert page_box['width'] >= 360 - 1, page_box
        assert panel.bounding_box()['x'] >= page_box['x'] + page_box['width'] - 1
        assert page.evaluate(GUARD, {'allow': list(ALLOW), 'focus': False}) == []
        shot(page, 'panel-default-1024', 1024, scheme)
    finally:
        context.close()

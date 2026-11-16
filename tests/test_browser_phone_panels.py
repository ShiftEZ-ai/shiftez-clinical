"""Phones: every side panel is a full-width sheet without a resize grip, between the top bar and the status footer, with
44px controls, no sideways scrolling and nothing clipped at 320, 375 and 390 px, light and dark; closing returns focus
to what opened it. RC_SHOTS=<folder> keeps a screenshot of each sheet."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_rota, planner_tool, auto_setup, whole_list_if_waiting  # noqa: F401
from .test_browser_rule_catalogue import shot
from .test_browser_rules_in_planner import rota
from .test_browser_requests import published, leave, day
from .rules_support import rule_action, rule_row

pytestmark = pytest.mark.browser
WIDTHS = [320, 375, 390]


def phone(width):
    return {'width': width, 'height': 640 if width == 320 else 812 if width == 375 else 844}


SHEET = '''e => {const r=e.getBoundingClientRect(),top=document.querySelector('.topbar')?.getBoundingClientRect().bottom||0,
  foot=document.querySelector('.app-footer'),bottom=foot&&getComputedStyle(foot).display!=='none'&&!e.closest('.dock-panel')&&!e.matches('.dock-panel')?foot.getBoundingClientRect().top:innerHeight;
  const grip=e.querySelector(':scope>.panel-grip'),controls=[...e.querySelectorAll('.button,.icon-button,.link-button,.dt-tool,.step-link,select,input:not([type=checkbox],[type=radio],[type=range],[type=hidden])')]
    .filter(n=>n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden');
  const small=controls.filter(n=>n.getBoundingClientRect().height<43.5).map(n=>(n.getAttribute('aria-label')||n.textContent||n.tagName).trim().slice(0,40));
  const clipped=controls.filter(n=>{const c=n.getBoundingClientRect();return c.left<-1||c.right>innerWidth+1;}).map(n=>(n.getAttribute('aria-label')||n.textContent).trim().slice(0,40));
  return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,limit:top,footer:bottom,grip:!!grip&&getComputedStyle(grip).display!=='none',
    small,clipped,wide:document.documentElement.scrollWidth-innerWidth};}'''


def assert_sheet(page, panel, *, fixed_to_footer=True):
    """Full width, no grip, not under the top bar, resting on the footer, 44px controls, nothing clipped or scrolling sideways."""
    expect(panel).to_be_visible()
    panel.evaluate('e => Promise.allSettled(e.getAnimations({subtree: true}).map(a => a.finished))')  # the sheet's rise
    m = panel.evaluate(SHEET)
    assert m['left'] <= 0.5 and m['right'] >= page.viewport_size['width'] - 0.5, m
    assert not m['grip'], m
    if fixed_to_footer:
        assert m['top'] >= m['limit'] - 1, m
        assert abs(m['bottom'] - m['footer']) <= 1, m
    assert not m['small'], m
    assert not m['clipped'], m
    assert m['wide'] <= 1, m


def open_planner(page, w):
    navigate(page, 'planner')
    open_rota(page, w['id'])
    page.wait_for_function("() => document.querySelectorAll('[data-rk]').length > 0")
    expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('width', WIDTHS)
def test_planner_panels_are_phone_sheets(browser, live_server, h, width, theme):
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator', phone(width))
    page.emulate_media(color_scheme=theme)
    tag = f'{width}-{theme}'
    try:
        open_planner(page, w)
        # Auto-schedule (D76 run card): no steps; Change opens the dates under the setup line; Esc returns to its button.
        auto = page.get_by_role('button', name='Auto-schedule', exact=True)
        auto.click()
        panel = page.locator('#auto-panel')
        assert_sheet(page, panel)
        expect(panel.locator('.step-link')).to_have_count(0)
        auto_setup(panel)
        # The dates are one compact row ("Sun 4 Oct → Sat 10 Oct · 7 days") that opens the calendar.
        dates = panel.locator('.date-box.range')
        summary = dates.locator('.date-summary')
        expect(summary).to_have_text(re.compile(r'^\w{3} \d{1,2} \w{3}( \d{4})? → \w{3} \d{1,2} \w{3}( \d{4})?$'))
        expect(dates.locator('.date-count')).to_have_text('7 days')
        assert dates.evaluate('e => e.getBoundingClientRect().height') <= 48
        assert summary.evaluate('e => e.scrollWidth <= e.clientWidth')
        expect(dates.get_by_role('button', name='Choose dates', exact=True)).to_be_visible()
        summary.click()
        expect(page.locator('.popover.date-pop')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.locator('.popover.date-pop')).to_have_count(0)
        expect(panel).to_be_visible()
        shot(page, f'planner-auto-{tag}', full_page=False)
        page.keyboard.press('Escape')
        expect(panel).to_be_hidden()
        expect(auto).to_be_focused()
        expect(auto).to_have_attribute('aria-expanded', 'false')
        # Rules in this rota: a sheet up to the top bar; Close returns focus to the tools menu's button.
        planner_tool(page, 'Rules in this rota')
        rules = page.get_by_role('dialog', name='Rules in this rota', exact=True)
        assert_sheet(page, rules)
        # Focus moves in on the next frame and stays in through the panel's own redraws (its sentences arriving).
        expect(rules.locator(':focus')).to_have_count(1)
        expect(page.locator('.rk-spot')).to_have_count(0)
        shot(page, f'planner-rules-{tag}', full_page=False)
        # An unsaved change: the Not saved bar sits at the sheet's foot, both choices reachable.
        rule_action(rule_row(rules, 'Weekly duty limit'), 'Adjust for this rota')
        rules.get_by_role('form', name='Adjust for this rota', exact=True).get_by_role('spinbutton').fill('6')
        rules.get_by_role('button', name='Close rules panel (Esc)', exact=True).click()
        bar = rules.get_by_role('alert')
        expect(bar).to_contain_text('Not saved.')
        assert abs(bar.bounding_box()['y'] + bar.bounding_box()['height'] - rules.bounding_box()['y'] - rules.bounding_box()['height']) <= 2
        assert_sheet(page, rules)
        shot(page, f'planner-rules-unsaved-{tag}', full_page=False)
        rules.get_by_role('button', name='Discard changes', exact=True).click()
        expect(rules).to_have_count(0)
        expect(page.get_by_role('button', name='Rota tools', exact=True)).to_be_focused()
        # Help: a sheet that leaves the schedule in sight above it.
        help_button = page.get_by_role('button', name='How to read this schedule', exact=True)
        help_button.click()
        sheet = page.locator('.wk-panel')
        assert_sheet(page, sheet)
        assert sheet.bounding_box()['height'] <= page.viewport_size['height'] * 0.6 + 1
        shot(page, f'planner-help-{tag}', full_page=False)
        sheet.get_by_role('button', name='Close help', exact=True).click()
        expect(sheet).to_have_count(0)
        expect(help_button).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', [(320, 'dark'), (390, 'light')])
def test_approver_rules_sheet_on_a_phone(browser, live_server, h, width, theme):
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'approver', phone(width))
    page.emulate_media(color_scheme=theme)
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        rules = page.get_by_role('dialog', name='Rules in this rota', exact=True)
        assert_sheet(page, rules)
        # Focus moved into the sheet once its rules arrived (they load on first use).
        expect(rules.locator(':focus')).to_have_count(1)
        shot(page, f'planner-rules-approver-{width}-{theme}', full_page=False)
        rules.get_by_role('button', name='Close rules panel (Esc)', exact=True).click()
        expect(rules).to_have_count(0)
        expect(page.get_by_role('button', name='Rota tools', exact=True)).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize('role,width,theme', [
    ('staff', 320, 'light'), ('staff', 375, 'dark'), ('staff', 390, 'light'),
    ('coordinator', 320, 'dark'), ('coordinator', 375, 'light'), ('coordinator', 390, 'dark'),
    ('approver', 320, 'light'), ('approver', 375, 'dark'), ('approver', 390, 'dark')])
def test_request_panel_and_wishes_on_a_phone(browser, live_server, h, role, width, theme):
    published(h)
    leave(h)
    h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'first_day': day(20), 'last_day': day(20), 'kind': 'prefer_off', 'points': 2})
    context, page = sign_in(browser, live_server, role, phone(width))
    page.emulate_media(color_scheme=theme)
    tag = f'{role}-{width}-{theme}'
    try:
        navigate(page, 'requests')
        whole_list_if_waiting(page)  # a decider opens on Waiting for you (maintenance, 998bbca16); the leave is in the whole list
        card = page.locator('#main .dt-card').filter(has_text='Leave').first
        card.locator('.dt-card-title').click()  # the card's centre may be its person link (opens People)
        panel = page.locator('.dock-panel')
        assert_sheet(page, panel, fixed_to_footer=False)
        box = panel.bounding_box()
        assert box['y'] <= 0.5 and box['height'] >= page.viewport_size['height'] - 1  # the whole screen
        shot(page, f'request-panel-{tag}', full_page=False)
        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(panel).to_have_count(0)
        assert page.evaluate("!!document.activeElement.closest('.dt-card')"), page.evaluate("document.activeElement.outerHTML.slice(0,200)")
        page.get_by_role('button', name='Wish calendar', exact=True).click()
        wishes = page.get_by_role('region', name='Wishes')
        expect(wishes).to_be_visible()
        m = wishes.evaluate(SHEET)
        assert not m['small'] and not m['clipped'] and m['wide'] <= 1, m
        days = wishes.locator('.wish-day').evaluate_all('ns => ns.map(n => n.getBoundingClientRect().height)')
        assert days and min(days) >= 44, days
        wishes.scroll_into_view_if_needed()
        shot(page, f'wishes-{tag}', full_page=False)
    finally:
        context.close()

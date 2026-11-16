"""The same-button confirm (`armed()` in dom.ts), owner decision D42 (DESIGN.md): no time limit (WCAG 2.2.1); it
disarms only when the mouse pointer or focus leaves it, on Escape, or on a tap elsewhere; it is used only for actions
that cannot be undone. A reversible action acts at once with Undo (test_browser_guards.py)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401

pytestmark = pytest.mark.browser

ARMED = re.compile(r'\barmed\b')
TEN_MINUTES = 10 * 60 * 1000

# One armed button, a plain button beside it and some text, on the real page with the real dom.js.
FIXTURE = """async () => {
  const {armed, button, el} = await window.importBundle('dom.js');
  window.acted = 0;
  const go = armed(button('Stop sharing', () => { window.acted++; }, 'secondary', {id: 'armed-go'}));
  const host = el('div', {id: 'armed-host'}, go, button('Elsewhere', () => {}, 'ghost', {id: 'armed-other'}),
    el('p', {id: 'armed-text'}, 'Plain text'));
  document.querySelector('#main').prepend(host);
}"""


def acted(page):
    return page.evaluate('() => window.acted')


def test_armed_confirm_has_no_time_limit_and_disarms_on_leaving_escape_or_focus(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        page.evaluate(FIXTURE)
        go = page.locator('#armed-go')
        go.click()
        expect(go).to_have_class(ARMED)
        # Screen readers hear the armed state: its accessible name and a live announcement.
        expect(go).to_have_accessible_name('Confirm? Stop sharing')
        expect(page.locator('#sr-live')).to_have_text('Press again to confirm: Stop sharing')
        # No time limit: long after the old 4 s / 8 s it is still armed, and nothing ran.
        page.clock.fast_forward(TEN_MINUTES)
        expect(go).to_have_class(ARMED)
        assert acted(page) == 0
        # The mouse pointer leaving disarms it.
        page.locator('#armed-other').hover()
        expect(go).not_to_have_class(ARMED)
        expect(go).to_have_accessible_name('Stop sharing')
        # A layout change moving it from under a still pointer is not the user leaving: one more press acts.
        go.click()
        expect(go).to_have_class(ARMED)
        page.locator('#armed-host').evaluate("n => n.style.setProperty('margin-top', '160px')")
        page.wait_for_function("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))")
        expect(go).to_have_class(ARMED)
        go.click()
        expect(go).not_to_have_class(ARMED)
        assert acted(page) == 1
        # Keyboard: Escape disarms and keeps focus; focus leaving disarms; pressing twice acts.
        go.focus()
        page.keyboard.press('Enter')
        expect(go).to_have_class(ARMED)
        page.keyboard.press('Escape')
        expect(go).not_to_have_class(ARMED)
        expect(go).to_be_focused()
        page.keyboard.press('Enter')
        expect(go).to_have_class(ARMED)
        page.keyboard.press('Tab')
        expect(page.locator('#armed-other')).to_be_focused()
        expect(go).not_to_have_class(ARMED)
        assert acted(page) == 1
        go.focus()
        page.keyboard.press('Enter')
        page.clock.fast_forward(TEN_MINUTES)
        page.keyboard.press('Enter')
        expect(go).not_to_have_class(ARMED)
        assert acted(page) == 2
    finally:
        context.close()


def test_armed_confirm_on_a_phone_tap_arms_tap_again_acts_tap_elsewhere_disarms(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 390, 'height': 844},
                            has_touch=True, is_mobile=True)
    try:
        page.evaluate(FIXTURE)
        go = page.locator('#armed-go')
        go.tap()
        expect(go).to_have_class(ARMED)  # the finger lifting is not the pointer leaving
        page.locator('#armed-text').tap()
        expect(go).not_to_have_class(ARMED)
        # A touch browser that never focuses a tapped button (iOS Safari): a tap elsewhere still disarms it.
        go.evaluate('b => b.click()')
        expect(go).to_have_class(ARMED)
        assert page.evaluate("() => document.activeElement?.id !== 'armed-go'")
        page.locator('#armed-text').tap()
        expect(go).not_to_have_class(ARMED)
        assert acted(page) == 0
        go.tap()
        expect(go).to_have_class(ARMED)
        go.tap()
        expect(go).not_to_have_class(ARMED)
        assert acted(page) == 1
    finally:
        context.close()


def test_an_irreversible_action_asks_twice_with_no_time_limit(browser, live_server, h):
    """Stop sharing a calendar link cannot be undone (the link dies; a new one is a different link): it asks twice."""
    person = h.get('staff', '/api/me')['person_id']
    assert person, 'This fixture must have a linked staff person'
    h.publish(h.shift(h.workspace('Armed calendar link'), people=[person])['workspace'])
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'insights/exports')
        page.get_by_role('button', name='Subscribe in my calendar', exact=True).click()
        expect(page.get_by_label('Your private calendar link', exact=True)).to_have_value(re.compile(r'\.ics$'))
        page.get_by_role('button', name='More link actions', exact=True).click()
        page.get_by_role('menuitem', name='Stop sharing', exact=True).click()
        confirm = page.get_by_role('menuitem', name='Confirm? Stop sharing', exact=True)
        expect(confirm).to_have_class(ARMED)
        page.clock.fast_forward(TEN_MINUTES)
        expect(confirm).to_have_class(ARMED)
        assert h.get('staff', '/api/calendar-feed')['active'] is True  # the first press only asks
        confirm.click()
        expect(page.get_by_text('Calendar link stopped.', exact=True)).to_be_visible()
        assert h.get('staff', '/api/calendar-feed')['active'] is False
    finally:
        context.close()

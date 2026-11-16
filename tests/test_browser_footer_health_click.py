"""maintenance hardening: the footer's rota health pill ("N must fix") opens its Rota health popover with a real pointer, at
the pill's centre, after a live redraw of the footer, when the footer redraws between pointer-down and pointer-up, and
after a reload. Footer controls act through one delegated handler (dom.ts footerAct). Real findings: two-place sessions
with one person. (A live update waits while a popover is open, readerBusy, so it never redraws under an open one.)"""
import re
import uuid

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in  # noqa: F401
from .test_browser_fix_list_panel import DESKTOP, real_gaps
from .test_browser import FIX_PILL, fix_pill

pytestmark = pytest.mark.browser

PILL = FIX_PILL + '.err'
# The pill on screen now is not the one marked before (the footer was drawn again).
REDRAWN = f"()=>{{const p=document.querySelector('{PILL}');return !!p&&!p.__before;}}"


def mark_pill(page):
    fix_pill(page, 'err').evaluate('n=>{n.__before=true;}')


def centre(page):
    box = fix_pill(page, 'err').bounding_box()
    return box['x'] + box['width'] / 2, box['y'] + box['height'] / 2


def rota_health(page):
    return page.get_by_role('dialog', name='Rota health')


def live_change(page, h, w):
    """A save from another tab, through the live server: its stream announces it and the open schedule redraws."""
    rev = h.detail(w)['workspace']['revision']
    payload = {'workspace_id': w['id'], 'assignment_type_id': h.seed['type'], 'location_id': h.seed['locations'][0],
               'service_ids': [h.seed['services'][0]], 'start': '2026-10-07T08:00:00+03:00', 'end': '2026-10-07T12:00:00+03:00',
               'participants': [], 'attributes': {}, 'override_reason': 'Footer redraw test'}
    reply = page.evaluate("""async ([payload, key, revision]) => {
        const csrf = document.cookie.split('; ').find(c => c.startsWith('rota_csrf=')).split('=')[1];
        const r = await fetch('/api/commands', {method: 'POST', headers: {'Content-Type': 'application/json',
            'X-CSRF-Token': csrf, 'Idempotency-Key': key}, body: JSON.stringify({kind: 'shift.create', payload, target_id: null, expected_revision: revision})});
        return {status: r.status, body: await r.text()};}""", [payload, str(uuid.uuid4()), rev])
    assert reply['status'] == 200, reply


def test_the_health_pill_opens_with_a_real_pointer_after_a_live_redraw_and_a_reload(browser, live_server, h):
    from .test_browser_team_box import open_view
    w, gaps, gap = real_gaps(h, 'footer click')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_view(page, w, 'Week')
        expect(fix_pill(page, 'err')).to_be_visible()
        # After a live redraw: the new pill opens at its centre with a real pointer.
        mark_pill(page)
        live_change(page, h, w)
        page.wait_for_function(REDRAWN, timeout=20000)
        page.mouse.click(*centre(page))
        expect(rota_health(page)).to_be_visible()
        expect(fix_pill(page, 'err')).to_have_attribute('aria-expanded', 'true')
        page.keyboard.press('Escape')
        expect(rota_health(page)).to_have_count(0)
        # After a reload: the same.
        page.reload()
        expect(fix_pill(page, 'err')).to_be_visible()
        page.mouse.click(*centre(page))
        expect(rota_health(page)).to_be_visible()
        expect(rota_health(page).get_by_role('button', name=re.compile(r'^Open all in Fix list \(\d+\)$'))).to_be_visible()
        # Opened right after a load, it settles above its pill, never over it (maintenance probe: once seen at top 792 of 900).
        page.wait_for_function(f"""()=>{{const p=document.querySelector('{PILL}'),d=document.querySelector('.popover.fix-pop');
          return !!p&&!!d&&d.getBoundingClientRect().bottom<=p.getBoundingClientRect().top+1;}}""", timeout=5000)
    finally:
        context.close()


def test_a_press_the_footer_redraws_under_still_opens_the_pill(browser, live_server, h):
    """The press begins on a pill a live update replaces before the button is released: the pill drawn again under the
    pointer is the one pressed, so the popover opens (once: no second, toggling press)."""
    from .test_browser_team_box import open_view
    w, gaps, gap = real_gaps(h, 'footer press redraw')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_view(page, w, 'Week')
        expect(fix_pill(page, 'err')).to_be_visible()
        mark_pill(page)
        x, y = centre(page)
        page.mouse.move(x, y)
        page.mouse.down()
        live_change(page, h, w)
        page.wait_for_function(REDRAWN, timeout=20000)
        page.mouse.move(*centre(page))
        page.mouse.up()
        expect(rota_health(page)).to_be_visible()
        expect(page.locator('.popover')).to_have_count(1)
    finally:
        context.close()

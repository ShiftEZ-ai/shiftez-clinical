"""Live audit findings LIVE-50…57 (full audit, 24 Oct 2026): one focused check each. LIVE-55 ("was rejected" in the
inbox) is worded by the worker (apps/worker/main.py) and is checked there."""
import re

import pytest
from playwright.sync_api import expect

from .governance_support import identity
from .test_browser import browser, live_server, sign_in, navigate, fill_sign_in  # noqa: F401
from .test_browser import whole_list
from .test_browser_requests import PHONE, day, leave, published, new_request, rows

pytestmark = pytest.mark.browser


def sign_out_and_in(page, username):
    page.get_by_role('button', name=re.compile(r'^Account menu, ')).click()
    page.get_by_role('menuitem', name='Sign out', exact=True).click()
    expect(page.get_by_role('heading', name='Sign in', exact=True)).to_be_visible()
    kept = page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('rota.') && k !== 'rota.account')")
    another = page.get_by_role('button', name='Use another account', exact=True)
    if another.count():
        another.click()
    fill_sign_in(page, username)
    expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible(timeout=20000)
    return kept


def test_live_50_the_next_account_never_inherits_the_remembered_week(browser, live_server, h):
    published(h)
    far = '2027-06-06'
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f'schedule/week/{far}')
        page.evaluate("localStorage.setItem('rota.board.view', JSON.stringify({months: 3}))")  # an unscoped layout key
        kept = sign_out_and_in(page, 'staff')
        assert kept == [], kept  # sign-out leaves nothing of the first account on this device
        assert far not in page.url
        navigate(page, 'schedule')
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        assert far not in page.url, page.url
    finally:
        context.close()


def test_live_51_swap_offer_names_no_colleague_and_closes_the_day_before(browser, live_server, h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    published(h, days=2)  # the staff duty is day() 08:00
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Swap a duty')
        expect(dialog.get_by_label('Swap with', exact=True)).to_have_value('')  # one colleague, still chosen by hand
        # The shared date-and-time field: its .value (date_picker.dateTimeInput) is the local ISO the request sends.
        assert dialog.locator('input[name="Offer open until"]').evaluate('i => i.value') == f'{day(-1)}T08:00'
        dialog.get_by_label('Message to them', exact=True).fill('Could we swap?')
        dialog.get_by_role('button', name='Send offer', exact=True).click()
        assert h.get('staff', '/api/requests/all?type=swap')['items'] == []  # refused until a colleague is chosen
        dialog.get_by_label('Swap with', exact=True).select_option(h.seed['people'][1])
        dialog.get_by_role('button', name='Send offer', exact=True).click()
        expect(page.get_by_text('Offer sent.', exact=False)).to_be_visible()
    finally:
        context.close()


def test_live_52_add_to_calendar_stays_and_toggles_the_subscription(browser, live_server, h):
    published(h)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'home')
        add = page.locator('.home-hero').get_by_role('button', name='Add to calendar', exact=True)
        add.click()
        panel = page.locator('#main .subscription')
        expect(panel.get_by_role('heading', name='Calendar subscription', exact=True)).to_be_visible()
        expect(add).to_be_visible()
        expect(add).to_have_attribute('aria-expanded', 'true')
        expect(panel).not_to_contain_text('The link is private to you')  # no helper paragraph
        add.click()
        expect(panel).to_have_count(0)
        expect(add).to_have_attribute('aria-expanded', 'false')
    finally:
        context.close()


def test_live_53_coming_up_lists_more_duties_with_whole_names(browser, live_server, h):
    w = h.workspace('Coming up rota', day(), day(7))
    for n in range(6):
        w = h.shift(w, f'{day(n)}T08:00:00+03:00', f'{day(n)}T12:00:00+03:00', people=[h.seed['people'][0]])['workspace']
    h.publish(w)
    context, page = sign_in(browser, live_server, 'staff', viewport={'width': 1368, 'height': 900})
    try:
        navigate(page, 'home')
        items = page.locator('#main .home-duty-list li')
        expect(items).to_have_count(5)  # the hero is the first; the five after it, not three
        expect(items.locator('.truncate')).to_have_count(0)
        clipped = items.locator('.home-clinic').evaluate_all('els => els.filter(e => e.scrollWidth > e.clientWidth + 1).length')
        assert clipped == 0
    finally:
        context.close()


def test_live_54_phone_schedule_toolbar_has_no_lone_view_settings_row(browser, live_server, h):
    published(h, days=2)
    context, page = sign_in(browser, live_server, 'staff', viewport=PHONE)
    try:
        navigate(page, 'schedule')
        bar = page.locator('#main .planner-toolbar')
        saved = bar.locator('.dt-saved').bounding_box()
        settings = bar.locator('.dt-glyph').bounding_box()
        assert saved and settings
        assert abs((saved['y'] + saved['height'] / 2) - (settings['y'] + settings['height'] / 2)) < 4, (saved, settings)
    finally:
        context.close()


def test_live_56_self_approval_actions_stay_in_their_table(browser, live_server, h):
    actions = [a['key'] for a in h.get('admin', '/api/self-approval')['actions']]
    h.command('admin', 'self_approval.grant', {'role': 'admin', 'actions': actions, 'starts_on': '2020-01-01',
                                               'reason': 'Too few approvers this quarter'})
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'access/routes')
        table = page.locator('[data-table="self-approval"]')
        cell = table.locator('td[data-col="actions"]').first
        expect(cell.locator('.pill-more')).to_have_text(re.compile(r'^\+\d+$'))
        scroll = table.locator('.table-scroll').first
        assert scroll.evaluate('s => s.scrollWidth <= s.clientWidth + 1')
    finally:
        context.close()


def test_live_57_decline_dialog_guards_and_closes_once_saved(browser, live_server, h):
    leave(h)
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        rows(page).first.get_by_role('button', name='Decline', exact=True).click()
        dialog = page.get_by_role('dialog').filter(has=page.get_by_label('Reason', exact=True))
        dialog.get_by_label('Reason', exact=True).fill('No cover that day')
        page.keyboard.press('Escape')
        page.keyboard.press('Escape')  # a repeated Escape: the browser may close a modal by itself
        expect(dialog.get_by_text('Not saved.', exact=False)).to_be_visible()
        dialog.get_by_role('button', name='Keep editing', exact=True).click()
        page.mouse.click(4, 4)  # the backdrop
        expect(dialog.get_by_text('Not saved.', exact=False)).to_be_visible()
        dialog.get_by_role('button', name='Keep editing', exact=True).click()
        held = []
        page.route('**/api/requests/all*', lambda route: held.append(route))
        dialog.get_by_role('button', name='Decline', exact=True).click()
        expect(dialog).to_have_count(0)  # closed while the list still reloads behind it
        for route in held:
            route.continue_()
        page.unroute('**/api/requests/all*')
    finally:
        context.close()

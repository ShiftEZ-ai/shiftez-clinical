"""Configuration › Department settings › Swaps: the offer window and open-offer cap (admin, with Undo), the staff swap
offer form that follows the window (desktop and phone), and staff/auditor who cannot change them."""
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from packages.domain.time import now as clock_now
from .governance_support import identity
from .test_browser import department_tab, browser, live_server, sign_in, navigate, open_config  # noqa: F401
from .test_browser_requests import published, new_request, no_horizontal_scroll

pytestmark = pytest.mark.browser
WINDOW, CAP = 'Swap offer open for (days)', 'Open swap offers per person'


def save_settings(page, reason):
    page.locator('.config-pane').get_by_role('button', name='Save settings', exact=True).click()
    page.get_by_role('dialog').get_by_label('Reason', exact=True).fill(reason)
    page.get_by_role('dialog').get_by_role('button', name='Save settings', exact=True).click()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_admin_changes_swap_limits_then_reload_and_undo(browser, live_server, h, scheme):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        page.emulate_media(color_scheme=scheme)
        open_config(page, 'Department settings')
        department_tab(page, 'Swaps')
        pane = page.locator('.config-pane')
        window, cap = pane.get_by_label(WINDOW, exact=True), pane.get_by_label(CAP, exact=True)
        expect(window).to_have_value('7')  # the defaults
        expect(cap).to_have_value('25')
        expect(window).to_have_attribute('min', '1')  # bounds from the generated contract
        # Each hint is its control's accessible description (F12), not only text beside it.
        expect(window).to_have_accessible_description('The longest a new swap offer can wait for a reply. It always ends before the duty starts.')
        expect(cap).to_have_accessible_description('Offers waiting for a reply, agreed or in review count until they close.')
        expect(pane.get_by_label('Who approves swaps', exact=True)).to_have_accessible_description(re.compile('swap'))
        expect(pane.get_by_role('group', name='Swap kinds allowed')).to_have_accessible_description('Choose none to stop new swap offers. Offers already made can still complete.')
        window.fill('3')
        cap.fill('4')
        save_settings(page, 'Shorter swap offers')
        expect(page.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        department = h.get('admin', '/api/me')['department']
        assert (department['swap_offer_window_days'], department['swap_open_offer_cap']) == (3, 4)
        page.reload()
        open_config(page, 'Department settings')
        department_tab(page, 'Swaps')
        expect(pane.get_by_label(WINDOW, exact=True)).to_have_value('3')
        expect(pane.get_by_label(CAP, exact=True)).to_have_value('4')
        # Undo restores both.
        pane.get_by_label(CAP, exact=True).fill('5')
        save_settings(page, 'One more open offer')
        page.get_by_role('button', name='Undo', exact=True).click()
        expect(pane.get_by_label(CAP, exact=True)).to_have_value('4')
        assert h.get('admin', '/api/me')['department']['swap_open_offer_cap'] == 4
        # Below the minimum: the form stops at the field, nothing is sent.
        pane.get_by_label(WINDOW, exact=True).fill('0')
        pane.get_by_role('button', name='Save settings', exact=True).click()
        expect(page.get_by_role('dialog')).to_have_count(0)
        assert h.get('admin', '/api/me')['department']['swap_offer_window_days'] == 3
    finally:
        context.close()


def window_days(h, days):
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    h.command('admin', 'department.settings', {'department_name': dept['name'], 'department_revision': dept['revision'],
              'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
              'swap_offer_window_days': days, 'reason': 'Synthetic swap window'}, dept['id'], org['revision'])


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}], ids=['desktop', 'phone'])
def test_staff_offer_form_follows_the_configured_window(browser, live_server, h, viewport):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    published(h, days=2)
    window_days(h, 2)
    context, page = sign_in(browser, live_server, 'staff', viewport=viewport)
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Swap a duty')
        expect(dialog.get_by_text('Up to 2 days, and before both duties.', exact=True)).to_be_visible()  # the field's hint
        expect(dialog).not_to_contain_text('seven days')
        latest = (clock_now() + timedelta(days=2)).astimezone(ZoneInfo('Asia/Riyadh')).date().isoformat()
        assert dialog.locator('input[name="Offer open until"]').get_attribute('max').startswith(latest)
        no_horizontal_scroll(page)
    finally:
        context.close()


@pytest.mark.parametrize('role', ['staff', 'auditor'])
def test_staff_and_auditor_cannot_change_swap_limits(browser, live_server, h, role):
    context, page = sign_in(browser, live_server, role)
    try:
        page.evaluate("location.hash = 'configuration/department'")
        expect(page.locator('#main')).to_be_visible()
        expect(page.get_by_label(WINDOW, exact=True)).to_have_count(0)
        expect(page.get_by_label(CAP, exact=True)).to_have_count(0)
    finally:
        context.close()
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    h.command(role, 'department.settings', {'department_name': dept['name'], 'department_revision': dept['revision'],
              'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
              'swap_open_offer_cap': 1, 'reason': 'Not allowed'}, dept['id'], org['revision'], status=403)
    assert h.get('admin', '/api/me')['department']['swap_open_offer_cap'] == 25

"""Configuration › Department settings › Events: the event horizon and receipt window through the real form."""
import pytest
from playwright.sync_api import expect
from .test_browser import department_tab, browser, live_server, sign_in, open_config  # noqa: F401

pytestmark = pytest.mark.browser

HORIZON, WINDOW = 'Event horizon · days', 'Event receipt window · days'


def test_event_horizon_and_receipt_window_save_reload_and_undo(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Department settings')
        department_tab(page, 'Advanced')
        pane = page.locator('.config-pane')
        horizon, window = pane.get_by_label(HORIZON, exact=True), pane.get_by_label(WINDOW, exact=True)
        expect(horizon).to_have_value('365')
        expect(window).to_have_value('14')
        save = pane.get_by_role('button', name='Save settings', exact=True)
        # Out of bounds: the form stops before asking for a reason, and nothing is saved.
        horizon.fill('3')
        save.click()
        expect(page.get_by_role('dialog')).to_have_count(0)
        assert horizon.evaluate('i => i.validity.rangeUnderflow')
        window.fill('3661')
        assert window.evaluate('i => i.validity.rangeOverflow')
        horizon.fill('120')
        window.fill('30')
        save.click()
        page.get_by_role('dialog').get_by_label('Reason', exact=True).fill('Plan events four months ahead')
        page.get_by_role('dialog').get_by_role('button', name='Save settings', exact=True).click()
        undo = page.get_by_role('button', name='Undo', exact=True)
        expect(undo).to_be_visible()
        department = h.get('admin', '/api/me')['department']
        assert (department['event_horizon_days'], department['event_receipt_window_days']) == (120, 30)
        page.reload()
        department_tab(page, 'Advanced')
        expect(pane.get_by_label(HORIZON, exact=True)).to_have_value('120')
        expect(pane.get_by_label(WINDOW, exact=True)).to_have_value('30')
        pane.get_by_label(HORIZON, exact=True).fill('200')
        save.click()
        page.get_by_role('dialog').get_by_label('Reason', exact=True).fill('A longer horizon')
        page.get_by_role('dialog').get_by_role('button', name='Save settings', exact=True).click()
        page.get_by_role('button', name='Undo', exact=True).click()
        expect(pane.get_by_label(HORIZON, exact=True)).to_have_value('120')
        department = h.get('admin', '/api/me')['department']
        assert (department['event_horizon_days'], department['event_receipt_window_days']) == (120, 30)
    finally:
        context.close()


@pytest.mark.parametrize('role', ['coordinator', 'staff'])
def test_event_settings_are_only_for_admins(browser, live_server, h, role):
    context, page = sign_in(browser, live_server, role)
    try:
        page.goto(live_server + '/#configuration/department')
        expect(page.locator('main')).to_be_visible()
        expect(page.get_by_label(HORIZON, exact=True)).to_have_count(0)
        expect(page.get_by_label(WINDOW, exact=True)).to_have_count(0)
    finally:
        context.close()

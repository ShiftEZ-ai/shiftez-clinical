"""One panel slot per page (side_panel.ts): a docked panel opened from another panel takes its place at its width with
"‹ Back to …" (Edit session from session details from the Schedule's session panel), the unsaved-changes guard runs
before going back, × closes them all, and a page panel opened while a docked one is open replaces it."""
import re

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, open_rota, pick_view  # noqa: F401

pytestmark = pytest.mark.browser


def visible_panels(page):
    return page.locator('.side-panel:visible')


def open_session_panel(page, w):
    navigate(page, 'planner')
    open_rota(page, w['id'])
    expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)
    pick_view(page, 'Week')
    page.locator('button.pchip').first.click()
    panel = page.locator('.wk-panel')
    expect(panel).to_be_visible()
    return panel


def test_panels_opened_from_a_panel_take_its_place_and_go_back(browser, live_server, h):
    w = h.shift(h.workspace('One slot rota'), people=h.seed['people'][:1])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        session = open_session_panel(page, w)
        title = session.locator('h2').inner_text()
        width = session.bounding_box()['width']
        # One session panel (S-09): the whole session is a mode of the same panel, in the same place.
        session.get_by_role('button', name='Whole session').click()
        session = page.locator('.wk-panel')
        expect(session.get_by_role('heading', name='People', exact=True)).to_be_visible()
        expect(visible_panels(page)).to_have_count(1)
        assert abs(session.bounding_box()['width'] - width) <= 2

        # Edit session from the session panel: a docked editor in the same slot, back to the session.
        session.get_by_role('button', name='Edit session', exact=True).click()
        editor = page.locator('.dock-panel')
        expect(editor.get_by_role('heading', name='Edit session and participants')).to_be_visible()
        expect(visible_panels(page)).to_have_count(1)  # never side by side
        assert abs(editor.bounding_box()['width'] - width) <= 2, 'the same slot, the same width'
        back = editor.get_by_role('button', name=re.compile('^Back to '))
        expect(back).to_be_visible()

        # Unsaved changes: Back asks first; Discard goes back to the session panel, as it was.
        editor.locator('form input[type=text]:visible, form textarea:visible').first.fill('Typed, not saved')
        back.click()
        expect(editor.get_by_role('alert')).to_contain_text('Not saved.')
        editor.get_by_role('button', name='Discard changes').click()
        expect(page.locator('.dock-panel')).to_have_count(0)
        expect(page.locator('.wk-panel').get_by_role('button', name='Edit session', exact=True)).to_be_visible()
        expect(visible_panels(page)).to_have_count(1)

        # × closes every panel in the slot.
        page.locator('.wk-panel').get_by_role('button', name='Edit session', exact=True).click()
        page.locator('.dock-panel').get_by_role('button', name='Close panel').click()
        expect(page.locator('.dock-panel')).to_have_count(0)
        expect(visible_panels(page)).to_have_count(0)
    finally:
        context.close()


def test_a_page_panel_replaces_an_open_docked_panel_after_its_guard(browser, live_server, h):
    w = h.shift(h.workspace('Slot replace rota'), people=h.seed['people'][:1])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        pick_view(page, 'Week')
        page.locator('#main').get_by_role('button', name='Add session', exact=True).click()
        editor = page.locator('.dock-panel')
        expect(editor.get_by_role('heading', name='Add session')).to_be_visible()
        expect(editor.get_by_role('button', name=re.compile('^Back to'))).to_have_count(0)  # not opened from a panel
        editor.locator('input:not([type=hidden]):not([type=checkbox]):not([type=radio]):visible, textarea:visible').first.fill('Typed')  # Clinics' ticks come first (SR-12)

        # A session opened on the schedule wants the slot: the editor asks first, the session panel waits.
        page.locator('button.pchip').first.click()
        expect(editor.get_by_role('alert')).to_contain_text('Not saved.')
        expect(page.locator('.wk-panel')).to_be_hidden()
        editor.get_by_role('button', name='Discard changes').click()
        expect(page.locator('.dock-panel')).to_have_count(0)
        expect(page.locator('.wk-panel')).to_be_visible()
        expect(visible_panels(page)).to_have_count(1)
    finally:
        context.close()

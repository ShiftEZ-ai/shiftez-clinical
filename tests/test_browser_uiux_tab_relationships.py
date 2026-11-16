"""Real page tab relationships, including the Schedule's views (one menu choice) and refresh."""
import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from apps.api.db import transaction
from .test_browser import browser, live_server, sign_in, navigate, set_layout, toolbar, close_popover, save_dialog, pick_view, reconnect  # noqa: F401
from .test_browser_uiux_polling import notification

pytestmark = pytest.mark.browser


def relationships(page, group):
    selected = group.locator('[role=tab][aria-selected=true]')
    expect(selected).to_have_count(1)
    expect(group.locator('[role=tab][tabindex="0"]')).to_have_count(1)
    tab_id = selected.get_attribute('id')
    assert tab_id
    controls = selected.get_attribute('aria-controls')
    assert controls
    panel = page.locator('#' + controls)
    expect(panel).to_have_attribute('role', 'tabpanel')
    expect(panel).to_have_attribute('aria-labelledby', tab_id)
    expect(panel).to_have_attribute('tabindex', '0')
    expect(panel).to_be_visible()
    # Every offered tab has a unique, resolvable control relationship.
    tabs = group.get_by_role('tab')
    ids = []
    for index in range(tabs.count()):
        item = tabs.nth(index)
        ident = item.get_attribute('id')
        assert ident and ident not in ids
        ids.append(ident)
        expect(page.locator('#' + ident)).to_have_count(1)
        expect(page.locator('#' + item.get_attribute('aria-controls'))).to_have_count(1)
    return selected, panel


def refresh_with_focus(page, group):
    selected, _ = relationships(page, group)
    selected.focus()
    ident = selected.get_attribute('id')
    previous = group.element_handle()
    with page.expect_response(lambda response: urlsplit(response.url).path == '/api/me'):
        page.evaluate("() => window.dispatchEvent(new Event('online'))")
    page.wait_for_function('old => !old.isConnected', arg=previous)
    expect(page.locator('#' + ident)).to_be_focused()
    relationships(page, group)


def view_menu(page):
    return toolbar(page.locator('.planner-toolbar'), 'View settings').get_by_role('radiogroup', name='Range', exact=True)


def test_page_tabs_link_active_content_through_keyboard_and_refresh(browser, live_server, h):
    """Schedule ranges are one choice (View settings): arrows move focus, Enter opens the view, the view region names
    the view shown, and a live refresh keeps it."""
    data = h.shift(h.workspace('Tab relationships'))
    h.command('coordinator', 'workspace.validate', {'reason': 'Stable tab fixture'},
              data['workspace']['id'], data['workspace']['revision'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        region = page.get_by_role('region', name='Week view', exact=True)
        expect(region).to_be_visible()
        menu = view_menu(page)
        week = menu.get_by_role('radio', name='Week', exact=True)
        expect(week).to_have_attribute('aria-checked', 'true')
        expect(menu.locator('[role=radio][aria-checked=true]')).to_have_count(1)
        week.focus()
        page.keyboard.press('ArrowLeft')
        expect(menu.get_by_role('radio', name='3 days', exact=True)).to_be_focused()
        page.keyboard.press('ArrowLeft')
        expect(menu.get_by_role('radio', name='Day', exact=True)).to_be_focused()
        expect(region).to_be_visible()  # moving focus does not change the view
        page.keyboard.press('Enter')
        day = page.get_by_role('region', name='Day view', exact=True)
        expect(day).to_be_visible()
        close_popover(page)
        with page.expect_response(lambda response: urlsplit(response.url).path == '/api/me'):
            reconnect(page)
        expect(day).to_be_visible()
        expect(view_menu(page).get_by_role('radio', name='Day', exact=True)).to_have_attribute('aria-checked', 'true')
        close_popover(page)
        # The date navigation stays in the toolbar.
        expect(page.locator('.planner-toolbar .wk-nav')).to_be_visible()
    finally:
        context.close()


def test_custom_and_saved_planner_tabs_label_their_actual_content(browser, live_server, h):
    data = h.shift(h.workspace('Custom tab relationships'))
    h.command('coordinator', 'workspace.validate', {'reason': 'Stable tab fixture'},
              data['workspace']['id'], data['workspace']['revision'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        pick_view(page, 'People')
        expect(page.get_by_role('region', name='People view', exact=True)).to_be_visible()
        toolbar(page.locator('.planner-toolbar'), 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
        form = page.get_by_role('dialog', name='New view', exact=True)
        form.get_by_label('View name', exact=True).fill('Saved tab relationship')
        save_dialog(form, 'Save view')
        saved = page.locator('.planner-toolbar button.dt-saved')
        expect(saved).to_have_attribute('aria-label', 'Saved views: Saved tab relationship')
        page.reload()
        navigate(page, 'planner')
        pick_view(page, 'Week')
        pick_view(page, 'Saved tab relationship')
        expect(page.get_by_role('region', name='People view', exact=True)).to_be_visible()
    finally:
        context.close()

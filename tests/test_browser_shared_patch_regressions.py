"""Shared audit fixes through real collection gestures, account settings and help."""
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_table_columns import ORDER, WIDTHS, drag, grip_of

pytestmark = [pytest.mark.browser, pytest.mark.parametrize('h', [pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)]
DISPLAYS = [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')]


def keep_shot(page, name):
    if folder := os.getenv('SHARED_PATCH_SHOTS'):
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / (name + '.png')), animations='disabled')


def assert_reported_width(page):
    grip = grip_of(page, 'rank')
    width = page.evaluate(WIDTHS, 'rank')[0]
    expect(grip).to_have_attribute('aria-valuenow', str(width))
    expect(grip).to_have_attribute('aria-valuetext', f'{width} pixels')
    return width


def clean_dialog_escape(page):
    page.get_by_role('button', name='New person', exact=True).click()
    dialog = page.get_by_role('dialog', name='New person', exact=True)
    expect(dialog).to_be_visible()
    page.keyboard.press('Escape')
    expect(dialog).to_have_count(0)


@pytest.mark.parametrize('gesture', ['resize', 'reorder'])
def test_pointer_gesture_navigation_releases_escape_and_next_click(browser, live_server, h, gesture):
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'people')
        expect(grip_of(page, 'rank')).to_be_visible()
        before = assert_reported_width(page)
        order = page.evaluate(ORDER)
        target = grip_of(page, 'rank') if gesture == 'resize' else page.locator('th[data-col=rank] .dt-sort')
        drag(page, target, 100, release=False)
        expect(page.locator('.dt-resizing' if gesture == 'resize' else '.dt-reordering')).to_be_visible()
        # Real pointer remains down while hash navigation removes its captured element.
        navigate(page, 'home')
        page.mouse.up()
        navigate(page, 'people')
        expect(grip_of(page, 'rank')).to_be_visible()
        assert assert_reported_width(page) == before
        assert page.evaluate(ORDER) == order
        clean_dialog_escape(page)
        page.locator('th[data-col=rank]').get_by_role('button', name='Current rank', exact=True).click()
        expect(page.get_by_role('menu', name='Current rank column')).to_be_visible()
        page.keyboard.press('Escape')
    finally:
        context.close()


def test_resize_release_cancel_keyboard_reset_and_reload_report_current_width(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'people')
        expect(grip_of(page, 'rank')).to_be_visible()
        natural = assert_reported_width(page)  # before focus, not set by its focus handler
        drag(page, grip_of(page, 'rank'), 96)
        saved = assert_reported_width(page)
        assert abs(saved - natural - 96) <= 3
        for finish in ('Escape', 'pointercancel', 'lostpointercapture'):
            grip = grip_of(page, 'rank')
            drag(page, grip, 70, release=False)
            if finish == 'Escape':
                page.keyboard.press('Escape')
            else:
                # Pointer cancellation has no mouse API; cancel the actual captured pointer.
                grip.evaluate("(n,type) => n.dispatchEvent(new PointerEvent(type,{bubbles:true,pointerId:1,pointerType:'mouse'}))", finish)
            page.mouse.up()
            assert assert_reported_width(page) == saved
            expect(page.locator('.dt-resizing')).to_have_count(0)
        page.reload()
        expect(grip_of(page, 'rank')).to_be_visible()
        assert assert_reported_width(page) == saved
        page.set_viewport_size({'width': 1200, 'height': 900})
        assert assert_reported_width(page) == saved
        grip_of(page, 'rank').press('ArrowRight')
        assert assert_reported_width(page) == saved + 16
        grip_of(page, 'rank').press('Shift+ArrowLeft')
        assert assert_reported_width(page) == saved - 48
        # A menu opened immediately after resizing must already offer Reset width.
        page.locator('th[data-col=rank]').get_by_role('button', name='Current rank', exact=True).click()
        menu = page.get_by_role('menu', name='Current rank column')
        menu.get_by_role('menuitem', name='Reset width', exact=True).click()
        assert abs(assert_reported_width(page) - natural) <= 3
        page.locator('th[data-col=rank]').get_by_role('button', name='Current rank', exact=True).click()
        expect(menu.get_by_role('menuitem', name='Reset width', exact=True)).to_have_count(0)
        page.keyboard.press('Escape')
        drag(page, grip_of(page, 'rank'), 80)
        grip_of(page, 'rank').dblclick()
        assert abs(assert_reported_width(page) - natural) <= 3
        order = page.evaluate(ORDER)
        for finish in ('Escape', 'pointercancel'):
            heading = page.locator('th[data-col=rank] .dt-sort')
            drag(page, heading, 140, release=False)
            if finish == 'Escape':
                page.keyboard.press('Escape')
            else:
                heading.evaluate("n=>n.dispatchEvent(new PointerEvent('pointercancel',{bubbles:true,pointerId:1,pointerType:'mouse'}))")
            page.mouse.up()
            assert page.evaluate(ORDER) == order
            expect(page.locator('.dt-reordering,.dt-drop-target,.dt-drag-src')).to_have_count(0)
            page.locator('th[data-col=rank]').get_by_role('button', name='Current rank', exact=True).click()
            expect(menu).to_be_visible()
            page.keyboard.press('Escape')
        clean_dialog_escape(page)
    finally:
        context.close()


def test_153_same_initial_codes_and_saved_override_are_unique_and_stable_in_ui(browser, live_server, h):
    for i in range(153):
        h.configure('people', {'display_name': 'Fictional Avery Example', 'staff_number': f'SYN{i:03}'},
                    role='admin' if i % 2 == 0 else 'coordinator')
    explicit = h.configure('people', {'display_name': 'Fictional Avery Example', 'short_code': 'FAE'})['record']
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'configuration/names')

        def read_codes():
            page.get_by_label('Find people', exact=True).fill('Fictional Avery Example')
            codes = {}
            while True:
                rows = page.locator('#main tr[data-row]')
                expect(rows.first).to_be_visible()
                for row in rows.all():
                    codes[row.get_attribute('data-row')] = row.locator('.code-chip').inner_text()
                next_page = page.get_by_role('button', name='Next page', exact=True)
                if not next_page.count() or next_page.is_disabled():
                    break
                old = rows.first.get_attribute('data-row')
                next_page.click()
                expect(rows.first).not_to_have_attribute('data-row', old)
            return codes

        first = read_codes()
        assert len(first) == 154 and len(set(first.values())) == 154
        assert first[explicit['id']] == 'FAE'
        keep_shot(page, 'names-scale-last-page')
        page.reload()
        expect(page.get_by_label('Find people', exact=True)).to_be_visible()
        previous = page.get_by_role('button', name='Previous page', exact=True)
        while previous.count() and previous.is_enabled():
            previous.click()
        assert read_codes() == first
        assert next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == explicit['id'])['short_code'] == 'FAE'
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', DISPLAYS)
def test_help_and_environment_keyboard_escape_reopen_fit_viewport(browser, live_server, h, width, scheme):
    context, page = sign_in(browser, live_server, 'operator', viewport={'width': width, 'height': 900}, device_scale_factor=1.25)
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'system/operations')
        trigger = page.get_by_role('button', name='How these are measured', exact=True)
        tip = page.get_by_role('tooltip')
        for _ in range(2):
            page.keyboard.press('Shift')
            trigger.focus()
            expect(tip).to_have_text('How these are measured')
            assert tip.evaluate('''n=>{const r=n.getBoundingClientRect();return r.left>=7.5&&r.right<=innerWidth-7.5
                &&r.top>=7.5&&r.bottom<=innerHeight-7.5&&n.scrollWidth<=n.clientWidth;}''')
            trigger.press('Enter')
            details = page.get_by_role('dialog', name='How these are measured', exact=True)
            expect(details).to_contain_text('Counts are read from the database')
            assert details.evaluate('n=>{const r=n.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&n.scrollWidth<=n.clientWidth;}')
            keep_shot(page, f'operations-help-{width}-{scheme}')
            page.keyboard.press('Escape')
            expect(details).to_have_count(0)
            expect(trigger).to_be_focused()
            page.locator('#main').focus()
        # The environment badge is a status image, not a Tab stop (a11y V10, 6078b4ca7): its name says it to a screen
        # reader, and pointing at it shows the tip, which fits the viewport and Escape dismisses (WCAG 1.4.13).
        badge = page.locator('.env-badge')
        expect(badge).to_have_attribute('aria-label', re.compile('Test'))
        assert badge.get_attribute('tabindex') is None
        badge.hover()
        expect(tip).to_contain_text('Test')
        assert tip.evaluate('n=>{const r=n.getBoundingClientRect();return r.left>=7.5&&r.right<=innerWidth-7.5&&n.scrollWidth<=n.clientWidth;}')
        page.keyboard.press('Escape')
        expect(tip).to_have_count(0)
        page.mouse.move(0, 0)
        badge.hover()
        expect(tip).to_be_visible()
        page.keyboard.press('Escape')
    finally:
        context.close()

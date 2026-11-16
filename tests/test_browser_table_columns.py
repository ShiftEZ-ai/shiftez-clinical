"""Shared table columns (table_view.ts): resize by the heading's edge (pointer, keyboard, double-click, Reset width) and
reorder by dragging a heading, without sorting, opening the column menu or selecting text; kept across reload."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser

WIDTHS = "key => [...document.querySelectorAll(`.dt-table [data-col='${key}']`)].map(n => Math.round(n.getBoundingClientRect().width))"
ORDER = "() => [...document.querySelectorAll('.dt-table thead th[data-col]')].map(n => n.dataset.col)"


def grip_of(page, key):
    return page.locator(f".dt-table thead th[data-col='{key}'] .dt-resize")


def drag(page, locator, dx, *, release=True, dy=0):
    box = locator.bounding_box()
    x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
    page.mouse.move(x, y); page.mouse.down(); page.mouse.move(x + dx, y + dy, steps=10)
    if release:
        page.mouse.up()


def test_columns_resize_and_reorder_by_pointer_and_keyboard_and_keep_after_reload(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'people')
        head = page.locator('.dt-table thead')
        expect(page.locator('.dt-table tbody td[data-col="rank"]').first).to_be_visible()
        first = page.evaluate(ORDER)[0]
        # The first column stays first: it cannot be dragged; every other heading can.
        expect(head.locator(f"th[data-col='{first}']")).not_to_have_attribute('data-reorder', '')
        rank = head.locator("th[data-col='rank']")
        start = page.evaluate(WIDTHS, 'rank')[0]

        # Pointer: the edge widens the column, heading and every cell alike; no menu opens, nothing sorts or is selected.
        drag(page, grip_of(page, 'rank'), 120)
        widths = page.evaluate(WIDTHS, 'rank')
        assert abs(widths[0] - (start + 120)) <= 3 and len(set(widths)) == 1, (start, widths)
        expect(page.locator('.popover')).to_have_count(0)
        expect(rank).to_have_attribute('aria-sort', 'none')
        assert page.evaluate('() => getSelection().toString()') == ''
        # Never narrower than a usable minimum.
        drag(page, grip_of(page, 'rank'), -2000)
        assert page.evaluate(WIDTHS, 'rank')[0] == 64
        # Escape mid-drag puts the width back.
        drag(page, grip_of(page, 'rank'), 300, release=False)
        page.keyboard.press('Escape'); page.mouse.up()
        assert page.evaluate(WIDTHS, 'rank')[0] == 64
        expect(page.locator('.dt-table.dt-resizing')).to_have_count(0)
        # Keyboard: the edge is a named separator; → widens by a step and reports the width.
        grip = page.get_by_role('separator', name='Resize Current rank column')
        grip.focus(); page.keyboard.press('ArrowRight'); page.keyboard.press('ArrowRight')
        assert page.evaluate(WIDTHS, 'rank')[0] == 96
        expect(grip).to_have_attribute('aria-valuenow', '96')

        # Drag the heading onto a later column: it takes that column's place; no menu opens, the sort is unchanged.
        order = page.evaluate(ORDER)
        assert order.index('rank') + 2 < len(order), order
        goal = order[order.index('rank') + 2]
        target = head.locator(f"th[data-col='{goal}']")
        tb, rb = target.bounding_box(), rank.locator('.dt-sort').bounding_box()
        drag(page, rank.locator('.dt-sort'), (tb['x'] + tb['width'] / 2) - (rb['x'] + rb['width'] / 2))
        expected = [k for k in order if k != 'rank']
        expected.insert(order.index(goal), 'rank')
        moved = page.evaluate(ORDER)
        assert moved == expected and moved[0] == first, (order, moved)
        expect(page.locator('.popover')).to_have_count(0)
        expect(head.locator("th[data-col='rank']")).to_have_attribute('aria-sort', 'none')
        assert page.evaluate('() => getSelection().toString()') == ''
        # Escape mid-drag leaves the order as it was, with nothing left marked.
        drag(page, head.locator("th[data-col='rank'] .dt-sort"), 400, release=False)
        page.keyboard.press('Escape'); page.mouse.up()
        assert page.evaluate(ORDER) == moved
        expect(page.locator('.dt-drop-target, .dt-drag-src, .dt-reordering')).to_have_count(0)
        # A click still opens the column menu.
        head.get_by_role('button', name='Current rank', exact=True).click()
        expect(page.get_by_role('menu', name='Current rank column')).to_be_visible()
        page.keyboard.press('Escape')

        # Width and order are kept for this account across a reload.
        page.reload(); navigate(page, 'people')
        expect(page.locator('.dt-table tbody td[data-col="rank"]').first).to_be_visible()
        assert page.evaluate(ORDER) == moved and page.evaluate(WIDTHS, 'rank')[0] == 96
        # Reset width (column menu) and double-click on the edge both give the column back its natural width.
        head.get_by_role('button', name='Current rank', exact=True).click()
        page.get_by_role('menu', name='Current rank column').get_by_role('menuitem', name='Reset width').click()
        expect(head.locator("th[data-col='rank']")).not_to_have_class('dt-sized')
        assert abs(page.evaluate(WIDTHS, 'rank')[0] - start) <= 3
        drag(page, grip_of(page, 'rank'), 80)
        grip_of(page, 'rank').dblclick()
        assert abs(page.evaluate(WIDTHS, 'rank')[0] - start) <= 3
    finally:
        context.close()


def test_a_wide_column_scrolls_inside_its_table_not_the_page(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 820, 'height': 900})
    try:
        navigate(page, 'people')
        expect(page.locator('.dt-table tbody td[data-col="rank"]').first).to_be_visible()
        grip = page.get_by_role('separator', name='Resize Current rank column')
        grip.focus()
        for _ in range(12):
            page.keyboard.press('Shift+ArrowRight')
        assert page.evaluate(WIDTHS, 'rank')[0] >= 600
        assert page.evaluate('() => document.scrollingElement.scrollWidth <= innerWidth')
    finally:
        context.close()

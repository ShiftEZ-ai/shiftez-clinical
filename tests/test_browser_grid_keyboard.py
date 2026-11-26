"""maintenance (WCAG 2.1.1 Keyboard, 2.4.3 Focus order; WAI-ARIA APG grid pattern, roving tabindex; APG window splitter):
a keyboard user tabs from the Schedule toolbar into a grid cell, never onto a column's resize grip; arrows move, Enter
opens the session, Escape comes back, Tab leaves. The grip is reached from its heading and keeps focus while it resizes.
The source guard is tests/test_web_grid_keyboard.py."""
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in  # noqa: F401

pytestmark = pytest.mark.browser
IN_GRID = 'g => g.contains(document.activeElement)'
AT_CELL = "g => g.contains(document.activeElement) && !!document.activeElement.closest('[role=gridcell]')"
# Tab stops inside a grid (a grip behind an unfocused heading is none).
TAB_STOPS = """g => [...g.querySelectorAll('button,a[href],input,select,textarea,[tabindex]')]
  .filter(n => n.tabIndex >= 0 && !n.disabled && n.getClientRects().length > 0).length"""


def tab_into(page, grid):
    page.locator('.planner-toolbar').get_by_role('button').last.focus()
    for _ in range(15):
        page.keyboard.press('Tab')
        if grid.evaluate(IN_GRID):
            return
    raise AssertionError('Tab from the toolbar never reached the grid')


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_tab_enters_the_schedule_grid_at_a_cell_and_a_grip_keeps_its_focus(browser, live_server, h, scheme):
    from .test_browser_schedule import october, show
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme, reduced_motion='reduce')
        for layout in ('month', 'week'):
            show(page, f"schedule/{layout}/2026-10-14?edit={d['id']}")
            grid = page.locator('#schedule-view .wk-grid')
            expect(grid.locator('button.pchip').first).to_be_visible()
            expect(grid.locator('[tabindex="0"]')).to_have_count(1)
            tab_into(page, grid)
            assert grid.evaluate(AT_CELL), f'{layout}: the first Tab stop in the grid is not a cell'
            expect(page.locator(':focus')).not_to_have_attribute('role', 'separator')
        # Arrows move inside the grid; the one stop follows.
        first = page.evaluate('document.activeElement.outerHTML')
        page.keyboard.press('ArrowRight')
        page.keyboard.press('ArrowDown')
        assert grid.evaluate(IN_GRID) and page.evaluate('document.activeElement.outerHTML') != first
        expect(grid.locator('[tabindex="0"]')).to_have_count(1)
        # Enter opens the session; Escape comes back to the same name.
        chip = grid.locator('button.pchip:not(.gap)').first
        chip.focus()
        name = chip.get_attribute('aria-label')
        page.keyboard.press('Enter')
        expect(page.locator('.wk-panel').first).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.locator(':focus')).to_have_attribute('aria-label', name)
        # Tab leaves the grid in one press.
        page.keyboard.press('Tab')
        assert not grid.evaluate(IN_GRID)
        # The grip: Ctrl+Home to the corner heading, Tab to its grip, ← → resize and focus stays on it.
        tab_into(page, grid)
        page.keyboard.press('ControlOrMeta+Home')
        expect(page.locator(':focus')).to_have_attribute('role', 'columnheader')
        page.keyboard.press('Tab')
        sep = page.locator(':focus')
        expect(sep).to_have_attribute('role', 'separator')
        expect(sep).to_have_attribute('aria-label', 'Resize Row headings')
        before = sep.get_attribute('aria-valuenow')
        page.keyboard.press('ArrowRight')
        expect(page.locator(':focus')).to_have_attribute('aria-label', 'Resize Row headings')
        expect(page.locator(':focus')).not_to_have_attribute('aria-valuenow', before)
        page.keyboard.press('ArrowLeft')
        expect(page.locator(':focus')).to_have_attribute('aria-label', 'Resize Row headings')
        assert page.evaluate('document.activeElement !== document.body')
        page.keyboard.press('Shift+Tab')
        expect(page.locator(':focus')).to_have_attribute('role', 'columnheader')
        # People is one Tab stop too.
        show(page, f"schedule/people/2026-10-14?edit={d['id']}")
        people = page.get_by_role('grid', name='Duties by person and day', exact=True)
        expect(people.locator('button.pg-chip').first).to_be_visible()
        expect(people.locator('[tabindex="0"]')).to_have_count(1)
        assert people.evaluate(TAB_STOPS) == 1
    finally:
        context.close()


@pytest.mark.parametrize('width,head', [(390, 180), (900, 320)])
def test_a_focused_people_chip_lands_beside_a_resized_name_column(browser, live_server, h, width, head):
    """WCAG 2.4.11 Focus Not Obscured: a chip focused while it sits under the sticky name column scrolls out beside it,
    at the width the view resized the column to (CSS Scroll Snap 1, scroll-margin applies to scrolling into view)."""
    from .test_browser_schedule import october, show
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        page.emulate_media(reduced_motion='reduce')
        show(page, f"schedule/people/2026-10-14?edit={d['id']}")
        people = page.get_by_role('grid', name='Duties by person and day', exact=True)
        expect(people.locator('button.pg-chip').first).to_be_visible()
        # The width a resize writes (planner_week.ts scheduleWidths), then the leftmost chip is put under the column.
        under, beside, edge = people.evaluate("""(g, head) => {
          g.style.setProperty('--sched-head', head + 'px');
          const s = g.parentElement, left = n => n.getBoundingClientRect().left;
          const chip = [...g.querySelectorAll('button.pg-chip')].sort((a, b) => left(a) - left(b))[0];
          s.scrollLeft += left(chip) - left(s);
          const under = left(chip);
          chip.focus();
          return [under, left(chip), g.querySelector('.pg-head>.pg-name').getBoundingClientRect().right];
        }""", head)
        assert under < edge - 1, (under, edge)  # it started under the column
        assert beside >= edge - 1, (beside, edge)
    finally:
        context.close()


def test_schedule_names_its_places_published_panel_list_table_and_new_range(browser, live_server, h):
    """maintenance: a Week name says the place; the published session panel is a named region; a step of the period
    navigator says the new range politely; the List table has a caption."""
    import re
    from .test_browser_schedule import october, show
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        show(page, f"schedule/week/2026-10-14?edit={d['id']}")
        grid = page.get_by_role('region', name='Week view').get_by_role('grid')
        chip = grid.get_by_role('button', name=re.compile(r'^Jordan Lee, Clinical review session, .+, 12 Oct 2026 08:00.12:00, published, read-only$'))
        place = chip.get_attribute('aria-label').split(', ')[2]
        assert place and place != '12 Oct 2026'
        chip.click()
        expect(page.get_by_role('region', name='Mon 12 Oct · Clinical review session', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        page.get_by_role('button', name='Next week', exact=True).click()
        expect(page.locator('#sr-live-polite')).to_have_text(re.compile(r'^Week of \d{1,2} Oct 2026$'))
        expect(page.locator('#sr-live-polite')).to_have_attribute('aria-live', 'polite')
        show(page, f"schedule/list/2026-10-15?edit={d['id']}")
        expect(page.get_by_role('table', name='Sessions', exact=True)).to_be_visible()
    finally:
        context.close()

"""The Rules database's column menus offer what every shared table's do (DESIGN.md › Shared tables): Sort A → Z / Z → A,
Filter by, Wrap this column, Hide column, Move left / right. A tree's own reading: Rule / who and Reach sort the rules,
the other columns sort each rule's lines; Filter by keeps the lines with those values (the rule opens on them); no
Group by (rules group by Family, lines stay under their rule)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import flag, weekly
from .rules_support import rules_by_family

pytestmark = pytest.mark.browser


def column(page, name):
    page.locator('#main thead .dt-sort').filter(has_text=re.compile(f'^{re.escape(name)}')).click()
    menu = page.get_by_role('menu', name=f'{name} column')
    expect(menu).to_be_visible()
    return menu


def items(menu):
    return [t.strip() for t in menu.locator('[role^=menuitem]').all_inner_texts()]


def test_rules_column_menus_sort_filter_wrap_hide_and_move(browser, live_server, h):
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        rules_by_family(h, page)  # grouped by Family (Question A–F with its people tables is the default, 9f01206c2)
        navigate(page, 'rules')
        main = page.locator('#main')
        heads = main.locator('.rt-table > thead th[scope=col]')  # the list's own columns, not a people table's
        expect(heads).to_have_text(['Rule / who', 'Where', 'Numbers', 'Days', 'Valid', 'Reach'])
        assert items(column(page, 'Where')) == ['Sort A → Z', 'Sort Z → A', 'Filter by Where…', 'Wrap this column', 'Wrap all columns', 'Hide column', 'Move left', 'Move right']
        page.keyboard.press('Escape')
        assert items(column(page, 'Rule / who')) == ['Sort A → Z', 'Sort Z → A', 'Filter by Rule / who…', 'Wrap this column', 'Wrap all columns']  # first: never hidden or moved
        # Sort A → Z on Rule / who: the rules by name, each rule's lines by who.
        page.get_by_role('menuitemradio', name='Sort A → Z').click()
        main.get_by_role('button', name='Open Duties per week').click()
        rule = main.locator('tr.rt-line')
        expect(rule.nth(0)).to_contain_text('Alex Morgan')
        expect(rule.nth(1)).to_contain_text('Everyone')
        # Move Reach left of Valid: the rule's own reach moves with its column.
        column(page, 'Reach').get_by_role('menuitem', name='Move left').click()
        expect(heads).to_have_text(['Rule / who', 'Where', 'Numbers', 'Days', 'Reach', 'Valid'])
        row = main.locator('tr.rt-rule').filter(has_text='Duties per week')
        assert row.evaluate("r => [...r.cells].reduce((n, c) => n + c.colSpan, 0)") == main.locator('thead tr').evaluate(
            "r => [...r.cells].reduce((n, c) => n + c.colSpan, 0)")
        reach_at = main.locator('thead tr').evaluate("r => [...r.cells].reduce((n, c, i) => i < [...r.cells].findIndex(x => x.textContent.startsWith('Reach')) ? n + c.colSpan : n, 0)")
        assert row.evaluate("(r, at) => {let n = 0; for (const c of r.cells) {if (n === at) return c.classList.contains('num'); n += c.colSpan;} return false}", reach_at)
        # Filter by Who (a line's who kind): only the rules with a person line, opened on it.
        column(page, 'Rule / who').get_by_role('menuitem', name='Filter by Rule / who…').click()
        page.get_by_role('button', name='Who: Any').click()
        page.locator('.picker').get_by_role('option', name='People', exact=True).check()
        page.keyboard.press('Escape')
        page.keyboard.press('Escape')
        expect(main.locator('tr.rt-rule')).to_have_count(1)
        expect(main.locator('tr.rt-line')).to_have_count(1)
        expect(main.locator('tr.rt-line')).to_contain_text('Alex Morgan')
        expect(page.get_by_role('button', name=re.compile('^Filter: People'))).to_be_visible()  # the toolbar shows it
        # Hide and wrap: saved like every view setting.
        column(page, 'Days').get_by_role('menuitem', name='Hide column').click()
        expect(heads).to_have_text(['Rule / who', 'Where', 'Numbers', 'Reach', 'Valid'])
        column(page, 'Where').get_by_role('menuitemcheckbox', name='Wrap this column').click()
        expect(main.locator('tr.rt-line td.rt-where')).to_have_class(re.compile('dt-wrap'))
        page.reload()
        expect(heads).to_have_text(['Rule / who', 'Where', 'Numbers', 'Reach', 'Valid'])
    finally:
        context.close()

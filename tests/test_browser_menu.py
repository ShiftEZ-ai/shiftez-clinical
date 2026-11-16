"""The one action menu (dom.ts menu(); UX › controls S2a), WAI-ARIA APG Menu Button: the button says it opens a menu;
↓ / ↑ open it at the first / last item, a letter jumps, Esc closes back to the button; the popup is the menu itself.
Opened from inside an open popover (a filter row's ⋯), it is that popover's child: Esc closes the menu alone."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser

MOUNT = """async () => {
  const {el, menu, opens, popover} = await window.importBundle('dom.js');
  document.querySelectorAll('.menu-fixture').forEach(n => n.remove());
  const said = []; window.__menuSaid = said;
  const items = ['Duplicate', 'Move', 'Rename', 'Remove'].map(label => ({label, icon: 'edit', run: () => said.push(label)}));
  const b = opens(el('button', {type: 'button', class: 'button secondary menu-fixture'}, 'Actions'), 'menu');
  b.addEventListener('click', () => menu(b, 'Fixture actions', items));
  const host = el('button', {type: 'button', class: 'button secondary menu-fixture'}, 'Panel');
  host.addEventListener('click', () => popover(host, panel => {
    const inner = opens(el('button', {type: 'button', class: 'button secondary'}, 'Inner'), 'menu');
    inner.addEventListener('click', () => menu(inner, 'Inner actions', items));
    panel.append(inner);
  }, {label: 'Fixture panel'}));
  document.querySelector('#main').prepend(b, host);
}"""


def test_menu_button_keyboard_and_child_menu(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        expect(page.locator("#main [data-row]").first).to_be_visible()
        page.evaluate(MOUNT)
        button = page.get_by_role("button", name="Actions", exact=True)
        expect(button).to_have_attribute("aria-haspopup", "menu")
        expect(button).to_have_attribute("aria-expanded", "false")
        button.focus()
        page.keyboard.press("ArrowDown")
        menu = page.get_by_role("menu", name="Fixture actions")
        expect(menu).to_have_class(re.compile(r"\bpopover\b"))  # the popup is the menu itself
        expect(menu.get_by_role("menuitem", name="Duplicate")).to_be_focused()
        expect(button).to_have_attribute("aria-expanded", "true")
        page.keyboard.press("r")  # type-ahead: the next item starting with R
        expect(menu.get_by_role("menuitem", name="Rename")).to_be_focused()
        page.keyboard.press("r")
        expect(menu.get_by_role("menuitem", name="Remove")).to_be_focused()
        page.keyboard.press("Escape")
        expect(menu).to_have_count(0)
        expect(button).to_be_focused()
        page.keyboard.press("ArrowUp")  # opens at the last item
        expect(page.get_by_role("menu", name="Fixture actions").get_by_role("menuitem", name="Remove")).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.get_by_role("menu", name="Fixture actions")).to_have_count(0)
        assert page.evaluate("window.__menuSaid") == ["Remove"]
        # A menu inside an open popover is its child: Esc closes the menu, then the popover.
        page.get_by_role("button", name="Panel", exact=True).click()
        panel = page.get_by_role("dialog", name="Fixture panel")
        inner = panel.get_by_role("button", name="Inner", exact=True)
        inner.click()
        child = page.get_by_role("menu", name="Inner actions")
        expect(child.get_by_role("menuitem", name="Duplicate")).to_be_focused()
        page.keyboard.press("Escape")
        expect(child).to_have_count(0)
        expect(panel).to_be_visible()
        expect(inner).to_be_focused()
        inner.click()
        page.get_by_role("menu", name="Inner actions").get_by_role("menuitem", name="Move", exact=True).click()  # a click inside does not close the panel first
        expect(page.get_by_role("menu", name="Inner actions")).to_have_count(0)
        expect(panel).to_be_visible()
        assert page.evaluate("window.__menuSaid") == ["Remove", "Move"]
        page.keyboard.press("Escape")
        expect(panel).to_have_count(0)
        # In a table, ↓ moves between rows (one Tab stop, table_view roveRows): a row's ⋯ does not take it.
        mores = page.locator("#main tbody tr[data-row] .dt-more")
        if mores.count() > 1:
            mores.first.focus()
            page.keyboard.press("ArrowDown")
            expect(page.get_by_role("menu", name="Row actions")).to_have_count(0)
            expect(mores.first).not_to_be_focused()
    finally:
        context.close()

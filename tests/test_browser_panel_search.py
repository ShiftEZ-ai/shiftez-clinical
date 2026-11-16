"""The one search box in a panel (dom.ts panelSearch(); UX › controls S10): named; Esc clears what is typed and the panel
stays; Esc on an empty box closes the panel as usual."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser

MOUNT = """async () => {
  const {el, popover, panelSearch} = await window.importBundle('dom.js');
  document.querySelectorAll('.ps-fixture').forEach(n => n.remove());
  window.__psSeen = [];
  const b = el('button', {type: 'button', class: 'button secondary ps-fixture'}, 'Open search');
  b.addEventListener('click', () => popover(b, panel => panel.append(panelSearch('Find a fixture', 'Find', q => window.__psSeen.push(q))), {label: 'Search fixture'}));
  document.querySelector('#main').prepend(b);
}"""


def test_panel_search_escape_clears_first(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        expect(page.locator("#main [data-row]").first).to_be_visible()
        page.evaluate(MOUNT)
        page.get_by_role("button", name="Open search", exact=True).click()
        panel = page.get_by_role("dialog", name="Search fixture")
        box = panel.get_by_role("searchbox", name="Find a fixture")
        expect(box).to_be_focused()
        page.keyboard.type("Al")
        page.keyboard.press("Escape")
        expect(box).to_have_value("")
        expect(panel).to_be_visible()  # the first Esc only cleared the text
        assert page.evaluate("window.__psSeen") == ["A", "Al", ""]
        page.keyboard.press("Escape")
        expect(panel).to_have_count(0)
    finally:
        context.close()

"""A group's header is its own toggle (dom.ts groupToggle; UX › controls S5; owner rule: no disclosure arrows): a button
with aria-expanded showing the name and count; collapsed, the same solid chip says "N hidden". No chevron anywhere."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser

MOUNT = """async () => {
  const {el, groupToggle} = await window.importBundle('dom.js');
  document.querySelectorAll('.gt-fixture').forEach(n => n.remove());
  window.__gtSaid = [];
  const b = groupToggle(el('strong', {}, 'Consultant'), 12, true, open => window.__gtSaid.push(open), {cls: 'gt-fixture'});
  document.querySelector('#main').prepend(b);
}"""


def test_group_header_is_its_toggle_and_says_hidden(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        expect(page.locator("#main [data-row]").first).to_be_visible()
        page.evaluate(MOUNT)
        toggle = page.locator(".gt-fixture")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(toggle).to_have_accessible_name("Consultant 12")
        expect(toggle.locator(".gt-count")).to_have_text("12")
        toggle.focus()
        page.keyboard.press("Enter")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(toggle.locator(".gt-count")).to_have_text("12 hidden")  # words, not an arrow
        page.keyboard.press("Space")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        assert page.evaluate("window.__gtSaid") == [False, True]
        # The chip is the same solid muted chip either way (DESIGN: dashes mean an open place).
        assert toggle.locator(".gt-count").evaluate("c => getComputedStyle(c).borderTopStyle") == "solid"
        expect(page.locator(".group-toggle [data-icon^=chevron]")).to_have_count(0)
    finally:
        context.close()

"""The one segmented choice (dom.ts segmented(); UX › controls S6), WAI-ARIA APG Radio Group: one Tab stop, arrows move
and (auto) choose, manual chooses with Space only, a disabled option is skipped, nothing chosen lands on the first."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser

MOUNT = """async () => {
  const {el, segmented} = await window.importBundle('dom.js');
  document.querySelectorAll('.seg-fixture').forEach(n => n.remove());
  window.__segSaid = [];
  const opts = [['a', 'Alpha'], ['b', 'Beta', {disabled: true, tip: 'Not allowed here'}], ['c', 'Gamma']];
  const auto = segmented('Auto choice', opts, 'a', v => window.__segSaid.push('auto:' + v), {cls: 'seg-fixture'});
  const manual = segmented('Manual choice', opts, null, v => window.__segSaid.push('manual:' + v), {cls: 'seg-fixture', activation: 'manual'});
  document.querySelector('#main').prepend(auto, manual);
}"""


def test_segmented_is_one_radio_group(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        expect(page.locator("#main [data-row]").first).to_be_visible()
        page.evaluate(MOUNT)
        auto = page.get_by_role("radiogroup", name="Auto choice")
        expect(auto.get_by_role("radio", name="Alpha")).to_have_attribute("aria-checked", "true")
        auto.get_by_role("radio", name="Alpha").focus()
        page.keyboard.press("ArrowRight")  # Beta is disabled: skipped
        expect(auto.get_by_role("radio", name="Gamma")).to_be_focused()
        expect(auto.get_by_role("radio", name="Gamma")).to_have_attribute("aria-checked", "true")
        manual = page.get_by_role("radiogroup", name="Manual choice")
        expect(manual.locator("[aria-checked=true]")).to_have_count(0)
        page.keyboard.press("Tab")  # nothing chosen: the first option takes the group's one Tab stop
        expect(manual.get_by_role("radio", name="Alpha")).to_be_focused()
        page.keyboard.press("ArrowRight")
        expect(manual.get_by_role("radio", name="Gamma")).to_be_focused()
        expect(manual.get_by_role("radio", name="Gamma")).to_have_attribute("aria-checked", "false")  # manual: moved only
        page.keyboard.press("Space")
        expect(manual.get_by_role("radio", name="Gamma")).to_have_attribute("aria-checked", "true")
        manual.get_by_role("radio", name="Gamma").click()  # the same choice again: no change reported
        assert page.evaluate("window.__segSaid") == ["auto:c", "manual:c"]
    finally:
        context.close()

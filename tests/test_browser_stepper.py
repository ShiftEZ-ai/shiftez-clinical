"""The one step rail (dom.ts stepper(); UX › controls S7): an ordered list named Steps, numbered; a done step shows a
check (no ✓ glyph) and says "completed"; the current one is aria-current=step; a step not reachable yet is disabled."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser

MOUNT = """async () => {
  const {stepper} = await window.importBundle('dom.js');
  document.querySelectorAll('.step-fixture').forEach(n => n.remove());
  window.__stepWent = [];
  const go = t => () => window.__stepWent.push(t);
  const rail = stepper([{title: 'Change', state: 'done', go: go('Change')}, {title: 'Preview', state: 'current', go: go('Preview')},
    {title: 'Results', go: go('Results'), disabled: true}], {cls: 'stepper step-fixture'});
  document.querySelector('#main').prepend(rail);
}"""


def test_step_rail_marks_done_and_current(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        expect(page.locator("#main [data-row]").first).to_be_visible()
        page.evaluate(MOUNT)
        rail = page.locator(".step-fixture")
        expect(rail).to_have_attribute("aria-label", "Steps")
        done = rail.get_by_role("button", name="Change (completed)")
        expect(done.locator("[data-icon=check]")).to_have_count(1)
        expect(rail).not_to_contain_text("✓")
        expect(rail.get_by_role("button", name="Preview", exact=True)).to_have_attribute("aria-current", "step")
        expect(rail.get_by_role("button", name="Results", exact=True)).to_be_disabled()
        done.click()
        assert page.evaluate("window.__stepWent") == ["Change"]
    finally:
        context.close()

"""maintenance: a popover taller than its room scrolls inside, above the status footer. Its head and foot stay in view
(View settings' Reset and Save as view), and the wheel scrolls the popover, never the page behind it. The phone bottom
sheet fits between the top of the screen and the footer in the same way. Written for CI (not run on the PC)."""
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, toolbar, view_settings  # noqa: F401

pytestmark = pytest.mark.browser

GEOMETRY = """p => {
  const r = p.getBoundingClientRect(), bar = document.querySelector('.app-footer');
  const foot = bar ? bar.getBoundingClientRect().top : innerHeight;
  const end = p.querySelector(':scope>:is(.vs-foot,.popover-foot,.dt-pop-foot)')?.getBoundingClientRect();
  return {top: r.top, bottom: r.bottom, foot, scrolls: p.scrollHeight > p.clientHeight + 1,
          endBottom: end ? end.bottom : null, overscroll: getComputedStyle(p).overscrollBehaviorY};
}"""
SETTLED = "e => Promise.allSettled(e.getAnimations({subtree: true}).map(a => a.finished))"


def wheel_scrolls_the_panel_only(page, panel):
    box = panel.bounding_box()
    before = page.evaluate('scrollY')
    page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    page.mouse.wheel(0, 1200)
    page.wait_for_function('p => p.scrollTop > 0', arg=panel.element_handle())
    assert page.evaluate('scrollY') == before


def test_view_settings_scrolls_inside_above_the_footer_with_its_foot_in_view(browser, live_server, h):
    w = h.workspace("Popover scroll", "2026-10-01", "2026-11-01")
    # A short window: View settings is taller than the room under its button.
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 480})
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        panel = view_settings(page)
        panel.evaluate(SETTLED)
        g = panel.evaluate(GEOMETRY)
        assert g['top'] >= 0 and g['bottom'] <= g['foot'] + 0.5, g
        assert g['scrolls'] and g['overscroll'] == 'contain', g
        # The foot is pinned to the panel's bottom edge: Reset and Save as view need no scrolling.
        assert g['endBottom'] is not None and abs(g['endBottom'] - g['bottom']) <= 2, g
        expect(panel.get_by_role('button', name='Reset to default')).to_be_in_viewport()
        wheel_scrolls_the_panel_only(page, panel)
        expect(panel.get_by_role('button', name='Reset to default')).to_be_in_viewport()
        expect(panel.get_by_role('radiogroup', name='Layout', exact=True)).to_have_count(1)
    finally:
        context.close()


def test_phone_sheet_fits_between_the_top_and_the_footer(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator", {"width": 390, "height": 560})
    try:
        navigate(page, 'rules')
        panel = toolbar(page.locator('#main'), 'Filter')
        panel.evaluate(SETTLED)
        g = panel.evaluate(GEOMETRY)
        assert g['top'] >= 0 and abs(g['bottom'] - g['foot']) <= 1, g  # a bottom sheet resting on the footer
        assert g['overscroll'] == 'contain', g
        if g['scrolls']:
            wheel_scrolls_the_panel_only(page, panel)
    finally:
        context.close()

"""One placement engine (dom.ts placeNear; UX › controls S1): a popover and the shared picker opened from the same
button land on the same side, never cover it, never run under the status footer and stay on screen; the arrow and
the trigger's ring show which button opened them."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser

OPEN = """async ([where, kind]) => {
  const {el, popover, viewportBottom} = await window.importBundle('dom.js'), {openPicker} = await window.importBundle('picker.js');
  document.querySelectorAll('.place-fixture').forEach(n => n.remove());
  const a = el('button', {type: 'button', class: 'button secondary place-fixture'}, 'Open');
  a.style.setProperty('position', 'fixed');
  a.style.setProperty(where === 'top' ? 'top' : 'bottom', where === 'top' ? '80px' : (innerHeight - viewportBottom() + 40) + 'px');
  a.style.setProperty(where === 'right' ? 'right' : 'left', '12px');
  document.body.append(a);
  // A wide table's horizontal scroller: the anchor sits at its visible right edge, well inside the window.
  let box = null;
  if (where === 'scroller') {
    box = el('div', {class: 'place-fixture'}); box.style.setProperty('width', '320px'); box.style.setProperty('overflow-x', 'auto');
    box.style.setProperty('position', 'fixed'); box.style.setProperty('top', '80px'); box.style.setProperty('left', '40px');
    const wide = el('div', {}); wide.style.setProperty('width', '1200px'); wide.style.setProperty('height', '40px'); wide.style.setProperty('position', 'relative');
    a.remove(); a.style.removeProperty('bottom'); a.style.setProperty('position', 'absolute'); a.style.setProperty('left', '250px'); a.style.setProperty('top', '0');
    wide.append(a); box.append(wide); document.body.append(box);
  }
  const choices = Array.from({length: 40}, (_, i) => ({value: 'v' + i, label: 'Item ' + i}));
  if (kind === 'picker') openPicker(a, document.body, {label: 'Test list', choices, multiple: false, selected: new Set(), recent: false, onPick: () => undefined});
  else popover(a, panel => panel.append(...Array.from({length: 12}, (_, i) => el('button', {type: 'button', class: 'menu-item'}, 'Row ' + i))),
    {label: 'Test', placement: where === 'right' ? 'below-end' : 'below'});
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const p = document.querySelector(kind === 'picker' ? '.picker' : '.popover').getBoundingClientRect(), b = a.getBoundingClientRect();
  const s = box?.getBoundingClientRect();
  return {panel: [p.left, p.top, p.right, p.bottom], button: [b.left, b.top, b.right, b.bottom], bottom: viewportBottom(),
    width: document.documentElement.clientWidth, scroller: s ? s.left + box.clientLeft + box.clientWidth : null,
    ring: a.classList.contains('is-open'), arrow: !!document.querySelector('.pop-arrow')};
}"""


def test_popover_and_picker_share_one_placement(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1280, "height": 800})
    try:
        navigate(page, "people")
        expect(page.locator("#main [data-row]").first).to_be_visible()
        for kind in ("popover", "picker"):
            top = page.evaluate(OPEN, ["top", kind])
            assert top["panel"][1] >= top["button"][3], (kind, top)  # below its button, not over it
            assert top["ring"] and top["arrow"], (kind, top)
            low = page.evaluate(OPEN, ["bottom", kind])
            assert low["panel"][3] <= low["button"][1], (kind, low)  # little room below: it opens above
            assert low["panel"][3] <= low["bottom"] and low["panel"][1] >= 0, (kind, low)  # never under the status footer
            page.keyboard.press("Escape")
        # Kept on screen: never under a classic vertical scrollbar (clientWidth, not innerWidth), for both engines' users.
        for kind in ("popover", "picker"):
            right = page.evaluate(OPEN, ["right", kind])
            assert right["panel"][2] <= right["width"] - 8 and right["panel"][0] >= 8, (kind, right)
            page.keyboard.press("Escape")
        # Inside a horizontal scroller, within that scroller's visible right edge too.
        inside = page.evaluate(OPEN, ["scroller", "popover"])
        assert inside["panel"][2] <= inside["scroller"] - 8, inside
        page.keyboard.press("Escape")
        # Typing changes the list's height: a picker opened upwards keeps its edge on the button at once.
        page.evaluate(OPEN, ["bottom", "picker"])
        page.keyboard.type("Item 3")
        gap = page.evaluate("""() => document.querySelector('.place-fixture').getBoundingClientRect().top
          - document.querySelector('.picker').getBoundingClientRect().bottom""")
        assert 0 <= gap <= 12, gap
    finally:
        context.close()

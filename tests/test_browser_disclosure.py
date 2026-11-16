"""Disclosure (owner decision 6 Nov 2026, DESIGN.md › Disclosure): no ▸/▶ triangle is ever shown. Every <summary> is a
plain text control: no list marker, no ::marker content, a 24px target. Checked on every summary style the app
uses (probes with each component's own classes, in the real stylesheet) and shared with real pages via
`no_disclosure_markers`."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401

pytestmark = pytest.mark.browser
# Every details class the web client gives a <summary> (grep "el('details'" in apps/web/src), and a bare one.
CLASSES = ['', 'pc-more', 'auto-how', 'auto-run-details', 'candidate-details', 'fx-table', 'rule-history', 'issue-fold',
           'issue-why', 'fb-details', 'fb-count-list', 'review-strip panel', 'rc-source', 'rs-map', 'wk-basis', 'hint',
           'auto-advanced']
MARKERS = """() => [...document.querySelectorAll('summary')].filter(s => s.getClientRects().length).flatMap(s => {
  const st = getComputedStyle(s), marker = getComputedStyle(s, '::marker').content, h = s.getBoundingClientRect().height;
  const wrong = [st.listStyleType !== 'none' && 'list-style ' + st.listStyleType, marker !== 'none' && 'marker ' + marker, h < 24 && 'height ' + h];
  return wrong.filter(Boolean).map(w => (s.parentElement.className || 'details') + ': ' + w);
})"""


def no_disclosure_markers(page):
    """Fails if any visible summary on the page shows a disclosure marker or is smaller than a 24px target."""
    assert page.evaluate(MARKERS) == []


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_no_summary_ever_shows_a_disclosure_marker(browser, live_server, scheme):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'home')
        page.evaluate("""classes => {const box = document.createElement('div'); box.id = 'disclosure-probe';
            for (const c of classes) {const d = document.createElement('details'); if (c) d.className = c;
              const s = document.createElement('summary'); s.textContent = 'Probe ' + (c || 'plain'); d.append(s, 'Shown when open');
              box.append(d);}
            document.querySelector('#main').prepend(box);}""", CLASSES)
        probes = page.locator('#disclosure-probe summary')
        expect(probes).to_have_count(len(CLASSES))
        no_disclosure_markers(page)
        # Still a control: the native expanded state opens and closes it from the keyboard.
        first = probes.first
        first.focus()
        page.keyboard.press('Enter')
        expect(page.locator('#disclosure-probe details').first).to_have_attribute('open', '')
        no_disclosure_markers(page)
    finally:
        context.close()

"""The status footer is always one line , on desktop and phone, light and
dark: items that do not fit go behind one clickable "+N" whose popover lists them (named, keyboard, Escape closes)."""
import re

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser
SCREENS = ('home', 'schedule', 'rules', 'people', 'requests')
EXTRA = 30  # wider than a 1440 footer

ONE_LINE = """() => {
  const f=document.querySelector('.app-footer'),cs=getComputedStyle(f);
  const items=[...f.querySelectorAll('.footer-status>*,.footer-status>.health-pills>*,.service-status>*')].filter(n=>{
    const r=n.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(n).display!=='none';});
  const mids=items.map(n=>{const r=n.getBoundingClientRect();return r.top+r.height/2;});
  return {items:items.length,spread:mids.length?Math.max(...mids)-Math.min(...mids):0,
    tallest:Math.max(0,...items.map(n=>n.getBoundingClientRect().height)),
    inner:f.clientHeight-parseFloat(cs.paddingTop)-parseFloat(cs.paddingBottom),
    // A phone's one line is the shared touch target (app.css --target, 86193d779); elsewhere 28px.
    line:parseFloat(cs.getPropertyValue('--target'))||28};
}"""


def one_line(page, where):
    m = page.evaluate(ONE_LINE)
    assert m['items'] and m['spread'] <= 2 and m['inner'] <= max(m['tallest'], m['line']) + 2, (where, m)


@pytest.mark.parametrize('scheme', ['light', 'dark'])
@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'phone'])
def test_the_footer_is_one_line_and_overflow_goes_behind_plus_n(browser, live_server, h, viewport, scheme):
    context, page = sign_in(browser, live_server, 'coordinator', viewport)
    page.emulate_media(color_scheme=scheme)
    try:
        for screen in SCREENS:
            navigate(page, screen)
            page.wait_for_load_state('networkidle')  # late cards (Home) rewrite their status when they arrive
            footer = page.locator('.app-footer')
            status = footer.locator('.page-status > .footer-status')
            expect(status).to_have_count(1)
            one_line(page, screen)
            # More status than any footer holds: still one line, the rest behind "+N".
            status.evaluate("(s,n) => {for(let i=1;i<=n;i++){const x=document.createElement('span');x.className='muted';x.textContent='Extra status '+i;s.append(x);}}", EXTRA)
            key = status.locator('.footer-key')
            expect(key).to_be_visible()
            expect(key).to_have_text(re.compile(r'\+\d+$'))
            expect(key).to_have_attribute('aria-label', re.compile(r'^\d+ more'))
            one_line(page, screen + ' overflowing')
            hidden = int(re.search(r'\+(\d+)', key.inner_text()).group(1))
            assert hidden >= 1
            key.focus()
            page.keyboard.press('Enter')
            listed = page.locator('.popover .footer-over-list')
            expect(listed).to_be_visible()
            expect(listed.locator(':scope > li')).to_have_count(hidden)
            expect(listed).to_contain_text(f'Extra status {EXTRA}')  # the last item is the first to go
            page.keyboard.press('Escape')
            expect(page.locator('.popover')).to_have_count(0)
            expect(key).to_be_focused()
            one_line(page, screen + ' after the popover')
    finally:
        context.close()

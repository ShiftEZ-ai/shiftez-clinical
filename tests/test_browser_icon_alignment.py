"""Icon-only controls (DESIGN.md › Visual contract, icons): every icon sits in the centre of its control (≤ 0.5 px on
both axes), at an even size (no half-pixel offsets), in a square box. Swept over the main pages for several roles, on a
desktop rail and on a phone with its drawer open."""
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser

# Every visible control whose only visible content is one icon (an <svg> or the brand <img>): text a screen reader alone
# reads (.sr-only) does not count. Returns the controls that break the rule, described for the failure message.
MEASURE = r"""() => {
  const bad=[],seen=new Set();
  const textShown=c=>{const walk=document.createTreeWalker(c,NodeFilter.SHOW_TEXT);let n;
    while((n=walk.nextNode())){if(!n.textContent.trim()||n.parentElement.closest('.sr-only,svg'))continue;
      const r=document.createRange();r.selectNodeContents(n);if(r.getBoundingClientRect().width>0)return true;}return false;};
  for(const c of document.querySelectorAll('button,a[href],[role=button],.status-icon')){
    const r=c.getBoundingClientRect();
    if(!r.width||!r.height||r.bottom<=0||r.top>=innerHeight||r.right<=0||r.left>=innerWidth)continue;
    if(getComputedStyle(c).visibility==='hidden'||c.closest('[inert],.fade-out'))continue;
    const glyphs=[...c.querySelectorAll('svg,img')].filter(g=>g.getBoundingClientRect().width>0);
    if(glyphs.length!==1||textShown(c)||c.querySelector('.avatar'))continue;
    const g=glyphs[0].getBoundingClientRect();
    const dx=g.left+g.width/2-(r.left+r.width/2),dy=g.top+g.height/2-(r.top+r.height/2);
    const name=c.getAttribute('aria-label')||c.dataset.tip||c.className;
    const key=name+'@'+Math.round(r.left)+','+Math.round(r.top);if(seen.has(key))continue;seen.add(key);
    const problems=[];
    if(Math.abs(dx)>0.5||Math.abs(dy)>0.5)problems.push(`off centre by ${dx.toFixed(1)},${dy.toFixed(1)}`);
    if(glyphs[0].tagName==='svg'&&(Math.round(g.width)%2||Math.round(g.height)%2))problems.push(`odd icon ${g.width}x${g.height}`);
    if(Math.abs(r.width-r.height)>0.5)problems.push(`box ${r.width}x${r.height} not square`);
    if(problems.length)bad.push(`${name} [${String(c.className).slice(0,40)}]: ${problems.join('; ')}`);
  }
  return bad;
}"""

PAGES = {
    'coordinator': ['home', 'schedule', 'requests', 'people', 'programmes', 'insights', 'rules', 'configuration'],
    'staff': ['home', 'schedule', 'requests', 'inbox'],
    'admin': ['home', 'access', 'system', 'configuration'],
    'approver': ['home', 'requests'],
}


def sweep(page, role, width):
    found = []
    for key in PAGES[role]:
        navigate(page, key)
        found += [f'{role} {width} #{key}: {line}' for line in page.evaluate(MEASURE)]
        if width < 720:
            # The phone drawer: its head (logo, Close menu), search and navigation.
            page.get_by_role('button', name='Menu', exact=True).click()
            expect(page.locator('.sidebar.navigation-open')).to_be_visible()
            found += [f'{role} {width} drawer: {line}' for line in page.evaluate(MEASURE)]
            page.keyboard.press('Escape')
            expect(page.locator('.sidebar.navigation-open')).to_have_count(0)
    return found


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('role', list(PAGES))
def test_icons_sit_centred_in_square_controls(browser, live_server, role, width):
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 900 if width > 720 else 844})
    try:
        found = sweep(page, role, width)
        assert not found, '\n'.join(sorted(set(found)))
    finally:
        context.close()


# Phone touch targets (WCAG 2.2 SC 2.5.5; DESIGN.md › Visual contract): every visible interactive element on the phone
# pages is at least 44 × 44 CSS px. A pill or footer control may keep its look and take its target from a centred
# absolute ::after (app.css › Touch targets), which counts. Not measured, each for a reason:
# - a person's name (.person-link) and display:inline links: targets in running text (SC 2.5.8 Inline exception);
# - checkboxes and radios: their label or row is the target;
# - the search input: its field (.dt-search-wrap) is the target and is measured instead;
# - visually hidden controls (a skip link until focused): no box to tap.
TARGETS = r"""() => {
  const bad=[],seen=new Set();
  const sel='button,a[href],[role=button],[role=tab],[role=menuitem],[role=menuitemradio],[role=menuitemcheckbox],[role=switch],summary,select,input:not([type=hidden],[type=checkbox],[type=radio])';
  for(let c of document.querySelectorAll(sel)){
    if(c.matches('.dt-search'))c=c.closest('.dt-search-wrap')||c;
    if(c.matches('.person-link')||getComputedStyle(c).display==='inline'||c.closest('.sr-only,[inert],.fade-out'))continue;
    const r=c.getBoundingClientRect();
    if(r.width<=1||r.height<=1||getComputedStyle(c).visibility==='hidden')continue;
    // Layout size, not the drawn box: a popover still in its opening scale (0.98) must not read 43 px.
    let w=c.offsetWidth||r.width,h=c.offsetHeight||r.height;const a=getComputedStyle(c,'::after');
    if(a.content!=='none'&&a.position==='absolute'){
      w=Math.max(w,w-parseFloat(a.left)-parseFloat(a.right));h=Math.max(h,h-parseFloat(a.top)-parseFloat(a.bottom));}
    const name=(c.getAttribute('aria-label')||c.textContent||c.className||c.tagName).trim().slice(0,40);
    const key=name+'@'+Math.round(r.left)+','+Math.round(r.top);if(seen.has(key))continue;seen.add(key);
    if(w<43.5||h<43.5)bad.push(`${name} [${String(c.className).slice(0,40)}]: ${w.toFixed(0)}x${h.toFixed(0)}`);
  }
  return bad;
}"""


def targets(page, where):
    return [f'{where}: {line}' for line in page.evaluate(TARGETS)]


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_phone_targets_are_44px(browser, live_server, h, scheme):
    """L6: the top bar, the Schedule and Requests toolbars, the Inbox, the account menu and the phone Fix list."""
    found = []
    for role in ('staff', 'coordinator'):
        context, page = sign_in(browser, live_server, role, viewport={'width': 390, 'height': 844})
        page.emulate_media(color_scheme=scheme)
        try:
            for key in ('home', 'schedule', 'requests', 'inbox'):
                navigate(page, key)
                found += targets(page, f'{role} #{key}')
            page.locator('.topbar .mobile-account').click()
            expect(page.get_by_role('menuitem', name='Send feedback')).to_be_visible()
            found += targets(page, f'{role} account menu')
            page.keyboard.press('Escape')
            if role == 'coordinator':
                from .test_browser_fix_list import serve
                w = h.shift(h.workspace('Phone targets'), people=[])['workspace']
                serve(page, w['id'])
                navigate(page, f'schedule/issues?draft={w["id"]}')
                expect(page.locator('#main .fix-page')).to_be_visible()
                found += targets(page, f'{role} Fix list')
        finally:
            context.close()
    assert not found, '\n'.join(sorted(set(found)))

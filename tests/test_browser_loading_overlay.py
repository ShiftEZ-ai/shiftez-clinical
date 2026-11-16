"""A wait is only its skeleton reviewed: while the API is slow nothing is drawn over the page, its skeleton or
the start-up screen's name and bar (no spinner card, no "Connecting…"); the words are for screen readers. Past 15 s one
quiet line in flow, "Still loading · 0:15" (its elapsed time, no Retry while the read is live, DL-045): at the top of each
skeleton, under the bar at start-up."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_person_profile import open_person
from .test_browser_rule_catalogue import flag

pytestmark = pytest.mark.browser
VIEWS = [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')]
# Every read but the live stream and the pulse (the footer's own business) is held.
READS = re.compile(r'.*/api/(?!events|pulse).*')

# Points across each shown skeleton (above the footer) must hit the skeleton itself: nothing lies over it.
COVERED = """() => {const out=[],limit=Math.min(innerHeight,document.querySelector('.app-footer')?.getBoundingClientRect().top||innerHeight)-1;
  for(const b of document.querySelectorAll('.loading:not(.sk-wait)')){const r=b.getBoundingClientRect();if(!r.width||!r.height||r.top>=limit)continue;
    for(const [fx,fy] of [[.5,.5],[.15,.15],[.85,.15],[.15,.85],[.85,.85]]){const x=r.left+r.width*fx,y=Math.min(r.top+r.height*fy,limit);
      const e=document.elementFromPoint(x,y);if(e&&!b.contains(e)&&!e.contains(b))out.push([b.className,String(e.className||e.tagName)]);}}
  return out;}"""
# Each shown skeleton's lines: in flow, inside it at its top, clear of its rows, and not covered.
LINES = """() => [...document.querySelectorAll('.loading:not(.sk-wait)')].filter(b=>b.getClientRects().length).map(b=>{
  const lines=[...b.querySelectorAll(':scope>.sk-slow')],l=lines[0];if(!l)return {count:0};
  const r=l.getBoundingClientRect(),o=b.getBoundingClientRect(),hit=document.elementFromPoint(r.left+2,r.top+r.height/2);
  const rows=[...b.querySelectorAll('.sk-item')].map(i=>i.getBoundingClientRect()).filter(i=>i.height&&i.top<o.bottom);
  return {count:lines.length,position:getComputedStyle(l).position,first:b.firstElementChild===l,
    inside:r.top>=o.top-1&&r.bottom<=o.bottom+1,clear:rows.every(i=>i.top>=r.bottom-.5),seen:!!hit&&l.contains(hit),text:l.textContent};})"""


def surfaces(h):
    person = next(p for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == h.seed['people'][0])
    return {'home': '#home', 'schedule': '#schedule', 'rules': '#rules', 'people': '#people', 'panel': person}


@pytest.mark.parametrize('surface', ['home', 'schedule', 'rules', 'people', 'panel'])
@pytest.mark.parametrize('width,theme', VIEWS)
def test_a_slow_read_draws_nothing_over_the_page_then_one_line(browser, live_server, h, surface, width, theme):
    size = {'width': width, 'height': 900 if width == 1440 else 844}
    # Rules: the rule catalogue is the page's read (maintenance, 10 Nov); the legacy list awaits no read once the lists are in
    # memory, so it has nothing slow to show.
    if surface == 'rules':
        flag(h)
    context, page = sign_in(browser, live_server, 'coordinator', size, color_scheme=theme)
    held, reads = [], READS
    try:
        page.clock.install()
        where = surfaces(h)[surface]
        # The panel: only its own read is held (the People list it opens from must answer).
        reads = '**/api/people/*/card*' if surface == 'panel' else READS
        navigate(page, 'requests')
        page.route(reads, lambda route: held.append(route))
        if surface == 'panel':
            open_person(page, where)  # the docked person panel opens, its body waiting
        else:
            page.evaluate(f"location.hash = '{where}'")
        expect(page.locator('.loading:not(.sk-wait)').first).to_be_visible()
        focused = page.evaluate('document.activeElement?.outerHTML?.slice(0, 80)')
        page.clock.fast_forward(3200)
        assert page.locator('.loading .sk-slow, .loading .spinner').count() == 0  # the moving skeleton alone
        assert page.evaluate(COVERED) == []
        page.clock.fast_forward(12_000)
        lines = page.evaluate(LINES)
        assert lines, 'a skeleton is still waiting'
        for line in lines:
            assert line['count'] == 1 and line['position'] == 'static' and line['first'], line
            assert line['inside'] and line['clear'] and line['seen'], line
            assert re.fullmatch(r'Still loading · 0:1\d', line['text']), line
        assert page.locator('.loading .sk-slow button').count() == 0  # no Retry while the read is live
        assert page.evaluate(COVERED) == []
        assert page.evaluate('document.activeElement?.outerHTML?.slice(0, 80)') == focused  # never takes focus
    finally:
        page.unroute(reads)
        for route in held:
            try:
                route.continue_()
            except Exception:  # noqa: BLE001  the page moved on; the read is gone
                pass
        context.close()


# The name and the bar are what they are at their centres: nothing lies over them.
BOOT = """() => {const main=document.querySelector('#main.boot'),out={};
  for(const [k,n] of [['name',main.querySelector('h1')],['bar',main.querySelector('.sk-boot .sk-grid')]]){const r=n.getBoundingClientRect();
    out[k]={top:Math.round(r.top),hit:[.1,.5,.9].every(f=>{const e=document.elementFromPoint(r.left+r.width*f,r.top+r.height/2);return !!e&&(n.contains(e)||e.contains(n));})};}
  const lines=[...main.querySelectorAll('.sk-slow')],bar=main.querySelector('.sk-boot .sk-grid').getBoundingClientRect();
  out.lines=lines.map(l=>({below:l.getBoundingClientRect().top>=bar.bottom,position:getComputedStyle(l).position,text:l.textContent}));
  out.visible=[...main.querySelectorAll('.loading *')].filter(n=>!n.closest('.sr-only')&&[...n.childNodes].some(c=>c.nodeType===3&&c.textContent.trim())).length;
  return out;}"""


@pytest.mark.parametrize('width,theme', VIEWS)
def test_a_slow_start_up_shows_only_the_emblem_name_and_bar(browser, live_server, width, theme):
    context = browser.new_context(viewport={'width': width, 'height': 900 if width == 1440 else 844}, color_scheme=theme)
    page = context.new_page()
    page.route('**/api/me*', lambda route: None)  # the account read never answers
    try:
        page.clock.install()
        page.goto(live_server, wait_until='commit')
        page.wait_for_function("() => document.querySelector('.sk-boot[data-born]') && getComputedStyle(document.querySelector('#main.boot')).opacity === '1'")
        expect(page.locator('#main.boot .loading > .sr-only')).to_have_text(re.compile(r'Connecting to .+…'))
        page.clock.run_for(14_000)
        before = page.evaluate(BOOT)
        assert before['name']['hit'] and before['bar']['hit'] and before['lines'] == [] and before['visible'] == 0, before
        page.clock.run_for(1_500)
        after = page.evaluate(BOOT)
        assert after['name']['hit'] and after['bar']['hit'], after
        assert [{**l, 'text': None} for l in after['lines']] == [{'below': True, 'position': 'static', 'text': None}], after
        assert re.fullmatch(r'Still loading · 0:1\d', after['lines'][0]['text']), after
        assert abs(after['name']['top'] - before['name']['top']) <= 1, (before, after)  # nothing moves
    finally:
        context.close()

"""Loading states reviewed: a page's skeleton fills its content area, its rows scaling with the height; a
long wait draws nothing over it reviewed. LOADING_SHOTS=<folder> keeps a
screenshot of the Planner, Home, Fairness and a person panel while loading and during the long wait."""
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_person_profile import open_person

from .test_browser_progress_bar import without_kept_replies
pytestmark = pytest.mark.browser
SHOTS = os.getenv('LOADING_SHOTS')
VIEWS = [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')]


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=f'{SHOTS}/{name}.png')


def open_held(page, pattern, address):
    """Opens an address with one of its reads held; returns the held routes."""
    held = []
    page.route(pattern, lambda route: held.append(route))
    page.evaluate(f"location.hash = '{address}'")
    return held


def release(page, pattern, held):
    for route in held:  # continue first: removing the last interceptor resumes held routes (Playwright 1.63+, test_browser.Held)
        route.continue_()
    page.unroute(pattern)


def visible_rows(page, box):
    """Skeleton rows wholly inside the skeleton's box (the rest are clipped)."""
    return box.evaluate("""b => {const r=b.getBoundingClientRect();
      return [...b.querySelectorAll('.sk-item')].filter(i=>{const x=i.getBoundingClientRect();return x.height&&x.top>=r.top&&x.bottom<=r.bottom+1;}).length;}""")


def test_a_page_skeleton_fills_the_height_and_its_rows_scale_with_it(browser, live_server, h):
    """The Schedule loading at 1440×700 and 1440×1300: the skeleton reaches the footer and a taller screen shows more rows."""
    counts = {}
    for height in (700, 1300):
        context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': height})
        try:
            navigate(page, 'home')
            without_kept_replies(page)  # else the Schedule opens from the rota list Home read and asks nothing
            # The planner's saved views are read on its first opening in every session (table_view.ts fetchViews) and
            # the page waits for them: holding both keeps the skeleton up whichever read the page waits on.
            pattern = re.compile(r'.*/api/(workspaces\?limit=|table-views\?table=planner).*')
            held = open_held(page, pattern, '#schedule')
            box = page.locator('#main .loading.page-sk.sk-fill')
            expect(box).not_to_have_class(re.compile(r'\bsk-wait\b'))
            gap = box.evaluate("b => (document.querySelector('.app-footer')?.getBoundingClientRect().top||innerHeight) - b.getBoundingClientRect().bottom")
            assert 0 <= gap <= 40, gap  # down to the footer
            counts[height] = visible_rows(page, box)
            release(page, pattern, held)
        finally:
            context.close()
    assert counts[1300] > counts[700] >= 7 * 4, counts  # calendar: whole weeks of 7 cells


@pytest.mark.parametrize('width,theme', VIEWS)
def test_loading_shots(browser, live_server, h, width, theme):
    """Screenshots (LOADING_SHOTS) of each surface while loading and during the long wait, checked by eye."""
    if not SHOTS:
        pytest.skip('LOADING_SHOTS not set')
    size = {'width': width, 'height': 900 if width == 1440 else 844}
    target = next(p for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == h.seed['people'][0])
    surfaces = [('planner', '**/api/workspaces?limit=*', '#schedule'), ('home', re.compile(r'.*/api/home(\?.*)?$'), '#home'),
                ('fairness', '**/api/fairness/**', '#insights/fairness')]
    for name, pattern, address in surfaces + [('panel', '**/api/people/*/card*', None)]:
        context, page = sign_in(browser, live_server, 'coordinator', size, color_scheme=theme)
        try:
            page.clock.install()
            navigate(page, 'requests')
            if address:
                held = open_held(page, pattern, address)
            else:
                held = []
                page.route(pattern, lambda route: held.append(route))
                open_person(page, target)
            expect(page.locator('#main .loading:not(.sk-wait), .dock-panel .loading:not(.sk-wait), [role=region] .loading:not(.sk-wait)').first).to_be_visible()
            shot(page, f'{name}-loading-{width}-{theme}')
            page.clock.fast_forward(15_500)
            expect(page.get_by_text(re.compile(r'^Still loading · 0:1\d$')).first).to_be_visible()
            shot(page, f'{name}-long-wait-{width}-{theme}')
            release(page, pattern, held)
        finally:
            context.close()

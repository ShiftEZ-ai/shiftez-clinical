"""The Planner's first open at department scale (163 people, 768 sessions): the week is drawn from the rota's compact
detail and that week's sessions and findings, read-only (data-reading: its writes held, Auto-schedule says it waits;
D59 no longer makes it inert, so it is read meanwhile), before the rota's whole detail (megabytes) lands; then it is drawn
whole and usable. Bytes and time to each are printed (FIRST_OPEN)."""
import json
import time
from collections import Counter
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from .test_backend_scale import scale_department
from .test_browser import PAGE_LOAD_MS, browser, live_server, sign_in, navigate, wait_held  # noqa: F401

pytestmark = pytest.mark.browser


def test_the_planner_draws_a_department_week_before_the_whole_rota_and_then_whole(browser, live_server, h):
    w, *_ = scale_department(h)
    w = h.lifecycle(w, 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        reads = []
        def finished(request):
            url = urlsplit(request.url)
            if request.method == 'GET' and url.path.startswith('/api/') and url.path != '/api/events':
                response = request.response()
                size = len(response.body()) if response and response.status == 200 else 0
                route = (url.path + ('?' + url.query if url.query else '')).replace(w['id'], '{id}')
                reads.append((time.monotonic(), route.split('&start=')[0], size))
        page.on('requestfinished', finished)
        # The whole rota is read ahead side by side with the window (readAhead): when it lands first the page rightly
        # draws it whole with no window (app.ts schedulePage), so it is held until the window is on screen.
        held = []
        whole_read = lambda url: urlsplit(url).path == f"/api/workspaces/{w['id']}" and urlsplit(url).query == 'view=schedule'
        navigate(page, 'home')
        page.route(whole_read, lambda route: held.append(route))
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        reads.clear()
        started = time.monotonic()
        # The address alone: navigate() waits for the page to be loaded, which is the whole rota.
        page.evaluate("key => { location.hash = key; }", f"schedule/week/2026-10-11?edit={w['id']}")
        # The window: drawn and read-only (a Week chip is [data-shift]); Auto-schedule waits for the whole rota (e7d1d6ba5).
        expect(page.locator('#schedule-view[data-reading] [data-shift]').first).to_be_attached(timeout=PAGE_LOAD_MS)
        first = time.monotonic()
        wait_held(page, held)  # the read ahead asked for it: released now, read once (below)
        held.pop().continue_()
        page.unroute(whole_read)  # after continuing: unrouting first would continue the held route itself
        # Then the whole rota: usable, with its own health.
        expect(page.locator('#schedule-view:not([data-reading]) [data-shift]').first).to_be_visible(timeout=PAGE_LOAD_MS)
        whole = time.monotonic()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        expect(page.get_by_text('Loading the whole rota…')).to_have_count(0)
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        before = [r for r in reads if r[0] <= first]
        detail = [r for r in reads if r[1] == '/api/workspaces/{id}?view=schedule']
        by_route = Counter()
        for _, route, size in reads:
            by_route[route] += size
        print('FIRST_OPEN', json.dumps({'seconds_to_window': round(first - started, 2), 'seconds_to_whole': round(whole - started, 2),
            'bytes_to_window': sum(r[2] for r in before), 'bytes_to_whole': sum(r[2] for r in reads if r[0] <= whole),
            'reads': len(reads), 'by_route': dict(by_route.most_common(6))}))
        assert not any(r[1] == '/api/workspaces/{id}?view=schedule' for r in before), [r[1] for r in before]
        assert any(r[1] == '/api/workspaces/{id}?view=compact' for r in before), [r[1] for r in before]
        assert len(detail) == 1, [r[1] for r in reads]  # the whole rota is read once
    finally:
        context.close()
    return locals()


@pytest.mark.browser
@pytest.mark.wall_benchmark
def test_the_planner_first_open_bytes_wall_budget(browser, live_server, h):
    state = test_the_planner_draws_a_department_week_before_the_whole_rota_and_then_whole(browser, live_server, h)
    assert sum(r[2] for r in state['before']) < 1_000_000


# Each day's header (filled/needed, short of staff) and each session's open places, as drawn.
DRAWN = """() => ({
  days: [...document.querySelectorAll('#schedule-view .wk-head .wk-day[role=columnheader]')].map(d =>
    [d.getAttribute('aria-label'), d.classList.contains('short'), d.querySelector('.wk-cov')?.textContent || '']),
  open: Object.fromEntries([...document.querySelectorAll('#schedule-view [data-session]')].map(c =>
    [c.dataset.session, c.querySelectorAll('.pchip.gap').length]))})"""


def test_the_window_draws_the_same_open_places_and_short_days_as_the_whole_rota(browser, live_server, h):
    """The first paint's open places and the days short of staff come from the window's coverage rows: the same as the
    whole rota shows for those days once it lands."""
    from .test_browser_planner import needing_type
    typ = needing_type(h, 'Window cover fixture')
    w = h.shift(h.workspace('Window cover rota'))['workspace']  # staffed
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[], typ=typ)['workspace']
    w = h.shift(w, start='2026-10-07T13:00:00+03:00', end='2026-10-07T17:00:00+03:00', people=[], typ=typ)['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    whole_read = lambda url: urlsplit(url).path == f"/api/workspaces/{w['id']}" and urlsplit(url).query == 'view=schedule'
    try:
        navigate(page, 'home')
        page.route(whole_read, lambda route: held.append(route))  # the whole rota waits until the window is read
        page.evaluate("key => { location.hash = key; }", f"schedule/week/2026-10-04?edit={w['id']}")
        expect(page.locator('#schedule-view[data-reading] [data-session]').first).to_be_attached(timeout=PAGE_LOAD_MS)
        expect(page.locator('#schedule-view[data-reading] .pchip.gap').first).to_be_attached()
        for _ in range(600):  # bounded: two frames a round
            if held:
                break
            page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        assert held, 'the whole rota was not asked for'
        window = page.evaluate(DRAWN)
        assert sum(window['open'].values()) >= 2 and sum(1 for d in window['days'] if d[1]) == 2, window
        held.pop().continue_()
        page.unroute(whole_read)  # after continuing: unrouting first would continue the held route itself
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        expect(page.locator('#schedule-view:not([data-reading]) .pchip.gap').first).to_be_visible()
        assert page.evaluate(DRAWN) == window
    finally:
        context.close()

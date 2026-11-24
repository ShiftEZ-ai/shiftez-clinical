"""At department scale the usable week reads only compact detail and window parts. The first session action reads
the whole rota once, then opens its panel. Bytes and time to each are printed (FIRST_OPEN)."""
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
        held = []
        whole_read = lambda url: urlsplit(url).path == f"/api/workspaces/{w['id']}" and urlsplit(url).query == 'view=schedule'
        navigate(page, 'home')
        page.route(whole_read, lambda route: held.append(route))
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        reads.clear()
        started = time.monotonic()
        navigate(page, f"schedule/week/2026-10-11?edit={w['id']}")
        expect(page.locator('#schedule-view:not([data-reading]) [data-shift]').first).to_be_visible(timeout=PAGE_LOAD_MS)
        first = time.monotonic()
        before = list(reads)
        assert not held, 'opening a draft must not ask for the whole rota'
        expect(page.get_by_role('button', name='Auto-schedule', exact=True)).to_be_enabled()
        page.locator('#schedule-view button.pchip[data-assignment]').first.click()
        wait_held(page, held)
        assert len(held) == 1
        expect(page.locator('.wk-panel')).to_have_count(0)
        held.pop().continue_()
        page.unroute(whole_read)  # after continuing: unrouting first would continue the held route itself
        # The whole rota is now usable, and the original press opens its session panel.
        expect(page.locator('.wk-panel')).to_be_visible(timeout=PAGE_LOAD_MS)
        expect(page.locator('#schedule-view:not([data-reading]) [data-shift]').first).to_be_visible(timeout=PAGE_LOAD_MS)
        whole = time.monotonic()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        expect(page.get_by_text('Loading the whole rota…')).to_have_count(0)
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        detail = [r for r in reads if r[1] == '/api/workspaces/{id}?view=schedule']
        by_route = Counter()
        for _, route, size in reads:
            by_route[route] += size
        print('FIRST_OPEN', json.dumps({'seconds_to_window': round(first - started, 2), 'seconds_to_whole': round(whole - started, 2),
            'bytes_to_window': sum(r[2] for r in before), 'bytes_to_whole': sum(r[2] for r in reads if r[0] <= whole),
            'reads': len(reads), 'by_route': dict(by_route.most_common(6))}))
        assert not any(r[1] == '/api/workspaces/{id}?view=schedule' for r in before), [r[1] for r in before]
        assert any(r[1] == '/api/workspaces/{id}?view=compact' for r in before), [r[1] for r in before]
        assert all(any(f'/parts?part={part}&' in r[1] for r in before) for part in ('shifts', 'validation'))
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
    """The window's coverage equals the whole rota's after an explicit whole-rota action."""
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
        page.route(whole_read, lambda route: held.append(route))
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        expect(page.locator('#schedule-view:not([data-reading]) [data-session]').first).to_be_visible(timeout=PAGE_LOAD_MS)
        expect(page.locator('#schedule-view:not([data-reading]) .pchip.gap').first).to_be_visible()
        assert not held, 'coverage must not ask for the whole rota'
        window = page.evaluate(DRAWN)
        assert sum(window['open'].values()) >= 2 and sum(1 for d in window['days'] if d[1]) == 2, window
        page.get_by_role('button', name='Add session', exact=True).click()
        wait_held(page, held)
        assert len(held) == 1
        held.pop().continue_()
        page.unroute(whole_read)  # after continuing: unrouting first would continue the held route itself
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        expect(page.get_by_role('region', name='Add session', exact=True)).to_be_visible()
        expect(page.locator('#schedule-view:not([data-reading]) .pchip.gap').first).to_be_visible()
        assert page.evaluate(DRAWN) == window
    finally:
        context.close()

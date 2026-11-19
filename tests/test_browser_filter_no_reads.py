"""maintenance: a filter change on the loaded Schedule redraws from what the page already read: no request, however long the page
has been open and even when another tab's change arrived while the Filter popover was open (that pulse is kept and
applied when the popover closes). Before: the planner memo's two-minute age limit and the kept pulse's 10 s replay
(forgetSchedule) each made every filter change read the rota list, the draft, its jobs ×2 and the week's sessions."""
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from .test_browser import PAGE_LOAD_MS, browser, live_server, sign_in, navigate, pick_view, close_popover, condition, planner_filter  # noqa: F401
from .test_browser_live_scopes import RECORD_PULSES, command
from packages.domain.time import now

pytestmark = pytest.mark.browser


def test_a_filter_change_on_the_loaded_schedule_reads_nothing(browser, live_server, h):
    other = h.configure('assignment_types', {'name': 'G4 other clinic', 'starts_on': '2020-01-01',
                        'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    w = h.shift(h.workspace('G4 filter rota'))['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', typ=other)['workspace']
    planner = sign_in(browser, live_server, 'coordinator')[1]  # another tab: its save is pushed to this one
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    reads = []
    def read(request):
        path = urlsplit(request.url).path
        if request.method == 'GET' and path.startswith('/api/') and path not in ('/api/events', '/api/pulse'):
            reads.append(path)
    page.on('request', read)
    try:
        context.add_init_script(RECORD_PULSES)
        page.reload()
        expect(page.locator('#main[data-loaded-view]')).to_have_count(1, timeout=PAGE_LOAD_MS)
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        pick_view(page, 'List')
        rows = page.locator('tr.dt-open')
        expect(rows).to_have_count(2)
        page.wait_for_function('() => window.livePulses.length > 0', timeout=15000)  # the stream's starting point
        pop = planner_filter(page)
        condition(pop, 'Duty type', ['G4 other clinic'], op='none')
        expect(rows).to_have_count(1)
        expect(rows.first).not_to_contain_text('G4 other clinic')
        # Three minutes on the page without firing timers: past the memo's former two-minute limit.
        page.clock.set_system_time(page.evaluate('Date.now()') + 180_000)
        # Another tab changes something the Schedule shows while the Filter popover is open: the pulse is kept, and its
        # 10 s replay (run here) must not drop what the page read.
        since = page.evaluate('window.livePulses.length')
        command(planner, 'catalogue.create', {'catalogue': 'tags', 'value': {'name': 'G4 live tag', 'labels': ['people'],
                'colour': 'violet'}, 'reason': 'Fictional'})
        page.wait_for_function("""since => window.livePulses.slice(since).some(p => (p.changes || []).some(c => c.scope === 'catalogue'))""",
                               arg=since, timeout=15000)
        page.clock.run_for(11_000)
        expect(pop).to_be_visible()
        reads.clear()
        pop.locator('.cond-line').last.get_by_label('Operator', exact=True).select_option('any')
        expect(rows).to_have_count(1)
        expect(rows.first).to_contain_text('G4 other clinic')
        expect(page.locator('#main[data-loaded-view=schedule]')).to_have_count(1)  # the redraw finished
        assert reads == [], reads
        # Closing the popover applies the kept change: the live update is late, never lost.
        close_popover(page)
        # A Schedule read (the draft or the rota list), not /api/pulse: the tick polls pulse anyway, so only a schedule
        # read proves the kept change forgot the cache and was applied.
        with page.expect_request(lambda r: r.method == 'GET' and urlsplit(r.url).path.startswith('/api/workspaces')):
            page.clock.run_for(11_000)
    finally:
        context.close()
        planner.context.close()


SHOWN_SESSIONS = "() => new Set([...document.querySelectorAll('#schedule-view [data-shift]')].map(e => e.dataset.shift)).size"


def test_a_filter_change_on_an_open_draft_past_the_rota_list_reads_nothing(browser, live_server, h):
    """maintenance: a draft that is not on the rota list's first page (a department with years of rotas, an old draft opened by its
    link) was read with a plain request that never entered the draft cache, so every filter change on the Week read the
    whole draft again (GET /api/workspaces/<id>, seconds at department scale). It is now read once, like any draft."""
    other = h.configure('assignment_types', {'name': 'G4 far clinic', 'starts_on': '2020-01-01',
                        'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    far = now().date() - timedelta(days=3 * 365)
    far = far - timedelta(days=far.weekday())  # a Monday
    day = lambda n, hour: f"{far + timedelta(days=n)}T{hour:02d}:00:00+03:00"
    w = h.shift(h.workspace('G4 far draft', start=str(far), end=str(far + timedelta(days=7))), start=day(0, 8), end=day(0, 12))['workspace']
    w = h.shift(w, start=day(1, 8), end=day(1, 12), typ=other)['workspace']
    # The first page (50 rotas) is filled with rotas covering today, which sort ahead of an old one.
    for n in range(50):
        h.workspace(f'G4 current {n}', start=str(now().date()), end=str(now().date() + timedelta(days=7)))
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    reads = []
    def read(request):
        path = urlsplit(request.url).path
        if request.method == 'GET' and path.startswith('/api/') and path not in ('/api/events', '/api/pulse'):
            reads.append(path)
    page.on('request', read)
    try:
        navigate(page, f"schedule/week/{far}?edit={w['id']}")
        expect(page.locator('#schedule-view:not([data-reading])')).to_have_attribute('aria-label', 'Week view', timeout=PAGE_LOAD_MS)
        expect(page.locator('#main[data-loaded-view=schedule]')).to_have_count(1)
        page.wait_for_function(f"() => ({SHOWN_SESSIONS})() === 2")
        pop = planner_filter(page)
        reads.clear()
        condition(pop, 'Duty type', ['G4 far clinic'], op='none')
        page.wait_for_function(f"() => ({SHOWN_SESSIONS})() === 1")
        pop.locator('.cond-line').last.get_by_label('Operator', exact=True).select_option('any')
        page.wait_for_function(f"() => ({SHOWN_SESSIONS})() === 1")
        expect(page.locator('#main[data-loaded-view=schedule]')).to_have_count(1)  # the redraw finished
        assert reads == [], reads  # includes GET /api/workspaces/<draft id>
        close_popover(page)
    finally:
        context.close()

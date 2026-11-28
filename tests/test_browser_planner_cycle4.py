"""Live QA cycle 4 (2026-11-06) planner fixes: a run's progress keeps moving while the page redraws (H-02), a run with
nothing to apply says why (H-01), Month and its Review link choose the data they need (H-04), and Continue on a
phone keeps the running auto-schedule and shows its status (H-12)."""
import contextlib
import re

import pytest
from playwright.sync_api import Error as PlaywrightError, expect

from .browser_acceptance_support import shot
from .test_browser import PAGE_LOAD_MS, SCHEDULE_VIEWS, browser, choose_layout, choose_radio, close_popover, live_server, navigate, route_fetch, sign_in, view_settings  # noqa: F401
from .test_browser_autoschedule_live import STEP, rota, start_run
from .test_browser_cp_sat import cp_server  # noqa: F401
from .test_browser_phone_scope import notice

pytestmark = pytest.mark.browser


def hold(page, pattern):
    """Requests matching `pattern` are held (not answered) until `release` is called."""
    held = []
    page.route(pattern, lambda route: held.append(route))

    def release():
        page.unroute(pattern)
        for route in held:
            with contextlib.suppress(PlaywrightError):  # a read the page has since dropped is already handled
                route.continue_()
    return held, release


def test_run_progress_keeps_moving_while_a_redraw_waits_for_the_rota(browser, cp_server, h):
    """H-02: a newer draw of the page that waits on the rota's read (minutes under load) never froze the run shown:
    its progress follows the job's stage meanwhile."""
    a, b = h.seed['people'][:2]
    context, page, stream, w, _ = start_run(browser, cp_server, h, [(4, 8, [a]), (5, 8, []), (6, 8, [b])])
    try:
        strip = page.locator('.app-footer .footer-status > .asl-strip')
        expect(strip.locator(STEP)).to_have_text('2/5 Preparing')
        held, release = hold(page, re.compile(re.escape(cp_server) + r'/api/workspaces/' + w['id'] + r'\?view=schedule$'))
        # Week: a redraw that reads the rota again (held), so the People view and its run stay on screen meanwhile.
        panel = view_settings(page.locator('.planner-toolbar'))
        choose_layout(panel, SCHEDULE_VIEWS['Week'][0])
        choose_radio(panel, 'Range', SCHEDULE_VIEWS['Week'][1])
        close_popover(page)
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'People view')
        stream.progress = {'stage': 'improving', 'elapsed_seconds': 80, 'search_started_seconds': 75, 'time_limit_seconds': 120}
        expect(strip.locator(STEP)).to_have_text('3/5 Searching', timeout=20000)
        assert held, 'the redraw was not waiting on the rota read'
        shot(page, 'run-progress-during-redraw-1440-light', full_page=False)
        release()
    finally:
        context.close()


def test_a_run_with_nothing_to_apply_says_why(browser, live_server, h):
    """H-01: a finished run with no change says why and what to do next, not only "Nothing to apply"."""
    w, _ = rota(h, [(4, 8, [h.seed['people'][0]])], name='Nothing to apply')
    fingerprint = h.detail(w)['current_fingerprint']
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])
    failed = {'status': 'translation_error', 'change_count': 0, 'additions': [], 'removals': [],
              'validation': {'valid': False, 'error_count': 120, 'warning_count': 0}, 'message': 'Solver and validation disagree'}

    def finished(route):
        response = route_fetch(route)
        body = response.json()
        for job in body['jobs'] if 'jobs' in body else [body['job']] if body.get('job') else []:
            if job.get('kind') == 'generation':
                job.update(status='succeeded', outcome='failed', result={**(job.get('result') or {}), **failed},
                           completed_at=job['created_at'], input_fingerprint=fingerprint, currency='current', result_stale=False)
        route.fulfill(response=response, json=body)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        page.route(re.compile(re.escape(live_server) + r'/api/jobs(\?.*|/[^/?]+(\?.*)?)$'), finished)
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.get_by_role('button', name='Auto-schedule', exact=True).click()
        panel = page.locator('#auto-panel')
        expect(panel).to_contain_text('Nothing to apply')
        expect(panel).to_contain_text('failed the final rule check (120 must fix), so it was not offered. Run auto-schedule again')
        shot(page, 'nothing-to-apply-why-1440-light', full_page=False)
    finally:
        context.close()


@pytest.mark.parametrize('address', ['schedule/month/2026-10-01?edit={id}&review=proposal', 'schedule/month/2026-10-01?edit={id}'])
def test_month_and_homes_review_link_draw_the_window_first(browser, live_server, h, address):
    """An ordinary Month stays on its window; Home's Review link needs the whole rota for its open panel."""
    w, _ = rota(h, [(4, 8, [h.seed['people'][0]]), (6, 8, [h.seed['people'][1]])], name='Windowed month')
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    parts = []
    try:
        page.on('request', lambda r: parts.append(r.url) if '/parts?part=' in r.url else None)
        whole = re.compile(re.escape(live_server) + r'/api/workspaces/' + w['id'] + r'\?view=schedule$')
        held, release = hold(page, whole)
        if 'review=proposal' in address:
            with page.expect_request(lambda r: bool(whole.match(r.url))):
                page.evaluate('a => location.hash = a', address.format(id=w['id']))
            expect(page.locator('#main .loading[aria-busy="true"]').first).to_be_attached()
            assert not parts, parts
        else:
            navigate(page, address.format(id=w['id']))
            expect(page.locator('.planner-bar')).to_contain_text('Windowed month')
            assert any('part=shifts' in u for u in parts), parts
            assert not held, 'Opening Month requested the whole rota'
            with page.expect_request(lambda r: bool(whole.match(r.url))):
                page.get_by_role('button', name='Auto-schedule', exact=True).click()
        release()
        expect(page.locator('#auto-panel')).to_be_visible(timeout=PAGE_LOAD_MS)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
    finally:
        context.close()


def test_continue_on_a_phone_keeps_the_running_auto_schedule(browser, cp_server, h):
    """H-12: a coordinator narrowing to a phone mid-run sees "Best on a desktop"; Continue anyway draws the planner from
    what was read (no reload of the rota), and the run's status stays in the footer."""
    a, b = h.seed['people'][:2]
    context, page, stream, w, _ = start_run(browser, cp_server, h, [(4, 8, [a]), (5, 8, []), (6, 8, [b])], color_scheme='dark', phone_notice=True)
    try:
        expect(page.locator('.app-footer .asl-strip ' + STEP)).to_have_text('2/5 Preparing')
        reads = []
        page.on('request', lambda r: reads.append(r.url) if re.search(r'/api/workspaces(?:\?limit=.*|/[^/?]+(?:\?view=schedule)?)?$', r.url) else None)
        page.set_viewport_size({'width': 390, 'height': 844})
        notice(page, 'Schedule planning').get_by_role('button', name='Continue anyway', exact=True).click()
        expect(page.locator('#main .phone-banner')).to_contain_text('Not optimised for phones.')
        stream.progress = {'stage': 'improving', 'elapsed_seconds': 80, 'search_started_seconds': 75, 'time_limit_seconds': 120}
        expect(page.locator('.app-footer .asl-strip ' + STEP)).to_have_text('3/5 Searching', timeout=20000)
        assert not reads, reads  # the rota list and the rota came from what this visit read
        shot(page, 'phone-continue-run-390-dark', full_page=False)
    finally:
        context.close()

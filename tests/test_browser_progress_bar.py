"""The top progress bar (loading.ts track) is for waits the person started: opening a page whose content is not on
screen, an explicit Refresh or Retry, their own save. Background work (live refreshes, job polling, the automatic check,
the pulse) never shows it, so an idle Planner is still. Owner, 6 Nov 2026: "the line above moving from left to right
makes me anxious, like it is not loaded yet"."""
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from .test_browser import browser, live_server, navigate, sign_in, route_fetch  # noqa: F401

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
DECEMBER = date(2026, 12, 6)  # a Sunday: a four-week December draft
# Time the bar is on, in ms, while the page is watched (a MutationObserver on its class).
# The bar's visible time, sampled every 100 ms (a lit sample counts 100 ms; a new lit run counts once).
BAR_CLOCK = """window.__bar={on:0,count:0,was:false};setInterval(()=>{const b=document.querySelector('.top-progress'),
  lit=!!b&&b.classList.contains('on')&&getComputedStyle(b).visibility!=='hidden';if(lit){window.__bar.on+=100;if(!window.__bar.was)window.__bar.count++;}
  window.__bar.was=lit;},100);"""
BAR_ON = "() => window.__bar.on"


def december_at_scale(h):
    """150 people and a four-week December draft of 500 sessions, checked (the department's shape)."""
    w = h.workspace('December at scale', DECEMBER.isoformat(), (DECEMBER + timedelta(days=28)).isoformat())
    dep, org = h.seed['department_id'], h.seed['organisation_id']
    with transaction(h.app.state.engine, write=True) as db:
        people = [{'id': str(uuid.uuid4()), 'organisation_id': org, 'display_name': f'Scale Person {k:03}', 'short_code': f'S{k:03}'}
                  for k in range(150)]
        db.execute(m.Person.__table__.insert(), people)
        pids = [p['id'] for p in people]
        db.execute(m.Membership.__table__.insert(), [{'id': m.uid(), 'department_id': dep, 'person_id': pid, 'category_id': h.seed['category'],
                                                      'starts_on': date(2024, 1, 1), 'ends_on': None} for pid in pids])
        db.execute(m.Eligibility.__table__.insert(), [{'id': m.uid(), 'department_id': dep, 'person_id': pid, 'service_id': sv,
                                                       'starts_on': date(2024, 1, 1)} for pid in pids for sv in h.seed['services']])
        snapshot = record(db.get(m.AssignmentType, h.seed['type']))
        shifts, assignments = [], []
        for n in range(500):
            at = datetime.combine(DECEMBER + timedelta(days=n % 28), datetime.min.time(), timezone.utc) + timedelta(hours=2 * ((n // 28) // 2))
            sid = m.uid()
            shifts.append({'id': sid, 'workspace_id': w['id'], 'assignment_type_id': h.seed['type'], 'type_revision': snapshot['revision'],
                           'type_snapshot': snapshot, 'location_id': h.seed['locations'][(n // 28) % 2], 'start': at, 'end': at + timedelta(hours=2)})
            assignments.append({'id': m.uid(), 'shift_id': sid, 'person_id': pids[n % 150], 'start': at, 'end': at + timedelta(hours=2)})
        db.execute(m.Shift.__table__.insert(), shifts)
        db.execute(m.ShiftService.__table__.insert(), [{'shift_id': s['id'], 'service_id': h.seed['services'][0]} for s in shifts])
        db.execute(m.Assignment.__table__.insert(), assignments)
    w = h.get('coordinator', '/api/workspaces/' + w['id'])['workspace']
    return h.lifecycle(w, 'validate')['workspace']


def background_traffic(page, w):
    """What runs under an idle Planner besides live updates: the draft reads as not checked, so the automatic check
    starts a validation job, which the page then polls each second (no worker ever finishes it)."""
    def unchecked(route):
        reply = route_fetch(route)  # unconditional (no If-None-Match), as before
        body = reply.json()
        if body.get('workspace', {}).get('id') == w['id']:
            body['workspace']['validation_current'] = False
        route.fulfill(status=200, json=body)
    page.route(re.compile(r'.*/api/workspaces/' + w['id'] + r'(\?.*)?$'), unchecked)
    page.route('**/health/service', lambda route: route.fulfill(status=200, json={'status': 'ok'}))
    job = {'id': 'idle-check', 'revision': 1, 'kind': 'validation', 'status': 'running', 'workspace_id': w['id']}
    page.route('**/api/jobs/idle-check/status', lambda route: route.fulfill(json={'job': job}))

    def commands(route):
        body = route.request.post_data_json or {}
        if body.get('kind') == 'job.create' and (body.get('payload') or {}).get('kind') == 'validation':
            return route.fulfill(json={'job': job})
        route.continue_()
    page.route('**/api/commands', commands)


def idle_planner_bar_ms(page, h, w, seconds):
    """Opens the December draft, waits until it has drawn, then leaves it idle for `seconds` while someone else changes
    another rota every 5 s (a live update the page reads quietly) and the check job is polled. Returns the time the
    bar was on (ms) and how many times it lit. The waits measure the idle period itself (no state to wait for)."""
    other = h.workspace('Someone else\'s rota', '2027-02-07', '2027-02-14')
    navigate(page, f"schedule/week/{DECEMBER.isoformat()}?edit={w['id']}")
    expect(page.locator(f'#schedule-view {DRAFT_CHIP}').first).to_be_visible(timeout=60_000)
    # The opening's own bar ends first (it stays its 400 ms once shown): only the idle time is measured.
    expect(page.locator('.top-progress')).not_to_have_class(re.compile(r'\bon\b'), timeout=30_000)
    page.evaluate("window.__bar.on=0;window.__bar.count=0;window.__bar.was=false")
    started = page.evaluate('performance.now()')
    n = 0
    while page.evaluate('performance.now()') - started < seconds * 1000:
        other = h.shift(other, start=f'2027-02-07T{8 + n % 8:02}:00:00+03:00', end=f'2027-02-07T{9 + n % 8:02}:00:00+03:00')['workspace']
        n += 1
        mark = page.evaluate('performance.now()')
        page.wait_for_function('t => performance.now() - t > 5000', arg=mark, timeout=30_000)
    return round(page.evaluate(BAR_ON)), page.evaluate('window.__bar.count')


@pytest.mark.wall_benchmark
def test_measure_idle_planner_bar_seconds_per_minute(browser, live_server, h):
    """The owner's measure (not a gate): seconds per minute the bar is on while the December Planner is idle.
    PW_API_DELAY_MS reproduces a loaded server (dev: reads of 0.3–2.4 s)."""
    w = december_at_scale(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.add_init_script(BAR_CLOCK)
        page.reload()
        background_traffic(page, w)
        on, lit = idle_planner_bar_ms(page, h, w, 60)
        result = {'bar_on_seconds_per_minute': round(on / 1000, 1), 'times_lit': lit, 'api_delay_ms': os.getenv('PW_API_DELAY_MS', '0')}
        print('IDLE-BAR', json.dumps(result))
        if os.getenv('BAR_MEASUREMENT'):
            open(os.environ['BAR_MEASUREMENT'], 'w').write(json.dumps(result))
    finally:
        context.close()


@pytest.fixture
def slow_api(monkeypatch):
    """Every API answer takes 600 ms (set before live_server starts it): background reads outlast the bar's 300 ms."""
    monkeypatch.setenv('PW_API_DELAY_MS', '600')


def test_an_idle_planner_never_shows_the_bar(browser, slow_api, live_server, h):
    """A December draft open and idle for 20 s while someone else changes another rota (live updates read quietly) and
    the automatic check's job is polled each second, every answer slow: the bar never lights."""
    w = december_at_scale(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.add_init_script(BAR_CLOCK)
        page.reload()
        background_traffic(page, w)
        polled = []
        page.on('request', lambda r: polled.append(r.url) if '/api/jobs/idle-check/status' in r.url else None)
        on, lit = idle_planner_bar_ms(page, h, w, 20)
        assert polled, 'the check job was never polled'
        assert (on, lit) == (0, 0), (on, lit)
        # The data stayed on screen throughout: no page skeleton replaced it.
        expect(page.locator('#main .loading.page-sk')).to_have_count(0)
    finally:
        context.close()


def without_kept_replies(page):
    """The person's own save: replies this tab holds are no longer shown as current (api.ts keptReads, keptEpoch), so the
    next page opened reads again. Without it, opening a page answers from the replies Home read, asks nothing and has
    nothing to wait for (c6d9edec1)."""
    lit = re.compile(r'\bon\b')
    with page.expect_response(lambda r: r.url.endswith('/api/commands')) as saved:
        page.evaluate("window.importBundle('api.js').then(m => m.command('table_view.create', {table_key: 'people', name: 'Read again', definition: {}}))")
    assert saved.value.ok, saved.value.text()
    expect(page.locator('.top-progress')).not_to_have_class(lit)


def held_command(page, held, script):
    """Sends a command from the page with /api/commands held; returns once the request is out."""
    with page.expect_request(lambda r: r.url.endswith('/api/commands'), timeout=15_000):
        # Not returned: page.evaluate would wait on the held command's promise until it gave up, and the bar is off by then.
        page.evaluate(f"() => {{ {script}; }}")


def release(held):
    for route in held:
        route.continue_()
    held.clear()


def past_the_delay(page):
    """Waits until well past the bar's 300 ms delay (the property measured is that nothing lit meanwhile)."""
    page.wait_for_function('t => performance.now() - t > 1000', arg=page.evaluate('performance.now()'))


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_opening_a_page_and_a_save_show_the_bar_and_an_automatic_command_never(browser, live_server, h, scheme):
    """The waits the person started still show it (the 300 ms delay itself: test_browser_loading): opening a page and
    their own save. A command the app sends by itself (the automatic rota check) does not, however long it takes."""
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme=scheme)
    bar = page.locator('.top-progress')
    lit = re.compile(r'\bon\b')
    held = []
    try:
        navigate(page, 'home')
        expect(page.locator('#main .loading')).to_have_count(0)
        without_kept_replies(page)
        # Opening the Schedule with its rota list held (a page read its opening waits for).
        page.route('**/api/workspaces?limit=*', lambda route: held.append(route))
        page.evaluate("location.hash = '#schedule'")
        expect(bar).to_have_class(lit)
        release(held)  # continue first: removing the last interceptor resumes held routes (Playwright 1.63+, test_browser.Held)
        page.unroute('**/api/workspaces?limit=*')
        expect(bar).not_to_have_class(lit)
        page.route('**/api/commands', lambda route: held.append(route))
        # The app's own check (a validation job), held: the bar stays off.
        held_command(page, held, "window.importBundle('api.js').then(m => m.command('job.create', {workspace_id: 'w', kind: 'validation', automatic: true}).catch(() => {}))")
        past_the_delay(page)
        expect(bar).not_to_have_class(lit)
        release(held)
        # The person's own save, held: the bar lights.
        held_command(page, held, "window.importBundle('api.js').then(m => m.command('table_view.create', {table_key: 'people', name: 'Mine', definition: {}}).catch(() => {}))")
        expect(bar).to_have_class(lit)
        release(held)
        expect(bar).not_to_have_class(lit)
    finally:
        context.close()


def test_with_reduced_motion_the_bar_does_not_move(browser, live_server, h):
    """Reduced motion: the bar shows as a still line, with no animation."""
    context, page = sign_in(browser, live_server, 'coordinator', reduced_motion='reduce')
    bar = page.locator('.top-progress')
    held = []
    try:
        navigate(page, 'home')
        page.route('**/api/commands', lambda route: held.append(route))
        held_command(page, held, "window.importBundle('api.js').then(m => m.command('table_view.create', {table_key: 'people', name: 'Still', definition: {}}).catch(() => {}))")
        expect(bar).to_have_class(re.compile(r'\bon\b'))
        assert bar.evaluate("b => getComputedStyle(b, '::after').animationName") == 'none'
        release(held)
    finally:
        context.close()

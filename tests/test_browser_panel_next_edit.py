"""The session panel is live for the next edit as soon as its save answers; the whole-rota check runs behind it, and
Submit, Approve and Publish wait for it in the footer's own words, whatever state the check is in."""
import contextlib
import re

import pytest
from playwright.sync_api import expect

from apps.worker.main import process_one
from .test_browser import browser, live_server, sign_in, open_rota  # noqa: F401
from .test_be18_command_latency import worker_up

pytestmark = pytest.mark.browser
SAVE = 'Saved · checking the rest of the rota…'
QUEUED = 'Waiting to check'
RUNNING = 'Checking the rota'
WHOLE_ROTA_CLAIM = re.compile(r'Rota check passed|must fix')
# Every flash a session panel shows, kept as it appears (a flash can be replaced before a later assertion reads it).
FLASHES = """() => { window.__flashes = []; new MutationObserver(() => {
  for (const n of document.querySelectorAll('.wk-panel .wk-flash')) window.__flashes.push(n.textContent); })
  .observe(document.body, {subtree: true, childList: true, characterData: true}); }"""


def two_open_sessions(h):
    w = h.workspace('Next edit rota', '2026-10-04', '2026-10-11')
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[])['workspace']
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[], location=1)['workspace']
    return h.lifecycle(w, 'validate')['workspace']


def checks_open(h, w):
    return [j for j in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']
            if j['kind'] == 'validation' and j['status'] in ('queued', 'running')]


def assign_first(page, panel):
    """Assigns the first person who passes every rule; returns their name and the shift.update sent."""
    first = panel.locator('.wk-cand.ok').first
    expect(first).to_be_visible(timeout=20000)
    name = first.locator('strong').inner_text()
    with page.expect_request(lambda r: r.url.endswith('/api/commands') and r.method == 'POST'
                             and (r.post_data_json or {}).get('kind') == 'shift.update') as sent:
        first.get_by_role('button', name=re.compile(r'^Assign (?!.* with a warning$)')).click()
    return name, sent.value


def open_gap(page):
    """Opens the first open place's fill panel, its candidates read synchronously (only the rota check is queued)."""
    page.locator('.pchip.gap').first.click()
    panel = page.locator('.wk-panel')
    expect(panel).to_contain_text('Can work this session', timeout=20000)
    return panel


def start(browser, live_server, h):  # noqa: F811
    w = two_open_sessions(h)
    worker_up(h)  # a live worker: each edit queues the complete check, which no worker runs until process_one
    context, page = sign_in(browser, live_server, 'coordinator')
    # Who can work a session is asked synchronously here, so only the rota check waits in the job queue.
    page.route(re.compile(r'.*/api/planner/fill-options\?async_options=true$'),
               lambda route: route.continue_(url=route.request.url.split('?')[0]))
    return w, context, page


def submit_of(page):
    return page.locator('.planner-bar').get_by_role('button', name='Submit for review…', exact=True)


def held_as_footer_says(page, words=None, press=False):
    """Submit is held in place (focusable, aria-disabled) with the footer's own words for the check: one name per state,
    in its tip, its screen-reader description and (`press`) the message a press shows. Returns the words."""
    submit = submit_of(page)
    expect(submit).to_have_attribute('aria-disabled', 'true', timeout=20000)
    if words:
        expect(submit).to_have_attribute('data-tip', words, timeout=20000)
    words = submit.get_attribute('data-tip')
    assert words
    expect(page.locator('.app-footer')).to_contain_text(words)
    described = submit.evaluate("b => (b.getAttribute('aria-describedby') || '').split(' ')"
                                ".map(id => document.getElementById(id)?.textContent || '').join(' ')")
    assert words in described, described
    if press:
        submit.click(force=True)  # a real press on the held control (Playwright waits for aria-disabled ones otherwise)
        expect(page.locator('#toast-tray .toast').first).to_have_text(words)
    return words


def refuse_delta(scopes):
    """Each shift.update reply's delta is refused (as for an amendment draft or after someone else's edit): the page
    cannot patch its copy and reads the rota whole. The reply's own scope is kept in `scopes`."""
    def answer(route):
        if (route.request.post_data_json or {}).get('kind') != 'shift.update':
            return route.continue_()
        response = route.fetch()
        body = response.json()
        scopes.append(body['validation'].get('scope'))
        body['delta'] = {**(body.get('delta') or {}), 'stale': True}
        route.fulfill(response=response, json=body)
    return answer


def older_reads_running(page, older_id, released):
    """A worker has the older check: its status reads say running until `released`, then its real end."""
    def answer(route):
        response = route.fetch()
        body = response.json()
        if not released:
            body['job'] = {**body['job'], 'status': 'running', 'error_code': None, 'completed_at': None}
        route.fulfill(response=response, json=body)
    page.route(re.compile(r'.*/api/jobs/' + re.escape(older_id) + r'/status$'), answer)


def no_whole_rota_claim(page):
    """The save's reply checked only the edit: its flash names the change and claims no whole-rota result."""
    page.wait_for_function("() => (window.__flashes || []).some(t => t.includes('added to the draft'))", timeout=20000)
    said = page.evaluate('() => window.__flashes')
    assert not [t for t in said if WHOLE_ROTA_CLAIM.search(t)], said


def test_panel_is_live_while_the_whole_rota_check_runs_and_submit_waits_for_it(browser, live_server, h):  # noqa: F811
    w, context, page = start(browser, live_server, h)
    try:
        open_rota(page, w['id'])
        page.evaluate(FLASHES)
        panel = open_gap(page)
        first, sent = assign_first(page, panel)
        assert sent.post_data_json['expected_revision'] == w['revision']
        # (a) The save's reply makes the panel editable: its own result, and the check behind it in words.
        expect(panel.get_by_role('status').filter(has_text=first + ' added to the draft')).to_be_visible(timeout=20000)
        expect(panel.locator('.wk-save-slot')).to_have_text(SAVE)
        no_whole_rota_claim(page)
        older = checks_open(h, w)
        assert len(older) == 1, 'the complete check is still waiting for a worker'
        expect(panel.locator('.wk-cands [aria-disabled=true], .wk-cands button:disabled')).to_have_count(0)
        # (c) Submit is held, focusable, saying what the footer says: the check is queued.
        held_as_footer_says(page, QUEUED)
        # A worker picks the check up: the footer says it in place, and Submit's tip, description and press say the
        # same. The older check's end reaches this page only when released, so it lands after the newer edit's check
        # has started.
        older_id, released = older[0]['id'], []
        older_reads_running(page, older_id, released)
        expect(page.locator('.app-footer [data-check-line]')).to_contain_text(RUNNING, timeout=20000)
        held_as_footer_says(page, RUNNING, press=True)
        # (b) A second edit while the check runs: the candidate list for it is loaded and live, and the edit is sent at
        # the current revision and saved.
        current = h.detail(w)['workspace']['revision']
        assert current > w['revision']
        panel = open_gap(page)
        assert checks_open(h, w), 'the check is still open while the next edit is chosen'
        live = panel.locator('.wk-cand.ok').first.get_by_role('button', name=re.compile(r'^Assign (?!.* with a warning$)'))
        expect(live).to_be_enabled()
        expect(live).not_to_have_attribute('aria-disabled', 'true')
        expect(panel.locator('.wk-loading')).to_have_count(0)
        second, sent = assign_first(page, panel)
        assert sent.post_data_json['expected_revision'] == current
        expect(panel.get_by_role('status').filter(has_text=second + ' added to the draft')).to_be_visible(timeout=20000)
        expect(panel.locator('.wk-save-slot')).to_have_text(SAVE)
        newer = checks_open(h, w)
        assert [j['id'] for j in newer] != [older_id] and newer, 'the newer edit has its own check'
        held_as_footer_says(page, QUEUED)
        # (d) The older check ends LAST, after the newer check started: its end (the server failed it as stale) is
        # dropped without drawing. The footer keeps the newer check's words, no "Check stopped", and Submit stays held.
        line = page.locator('.app-footer [data-check-line]')
        expect(line).to_contain_text(QUEUED)
        with page.expect_response(lambda r: r.url.endswith('/api/jobs/' + older_id + '/status'), timeout=20000):
            released.append(True)
        # Two ticks of the footer's clock: the page has had time to draw anything the older check's end would draw.
        for _ in range(2):
            ticked = line.inner_text()
            expect(line).not_to_have_text(ticked, timeout=5000)
        expect(page.locator('.check-stopped')).to_have_count(0)
        expect(page.locator('.app-footer')).not_to_contain_text('Check stopped')
        held_as_footer_says(page, QUEUED)
        # (e) The worker runs the newer check: the holds release and the slot empties in place.
        for _ in range(5):
            if not checks_open(h, w):
                break
            process_one(h.app.state.engine)
        assert not checks_open(h, w)
        latest = h.detail(w)['workspace']
        assert not latest['validation'].get('scope') and latest['revision'] >= current
        expect(submit_of(page)).not_to_have_attribute('aria-disabled', 'true', timeout=20000)
        expect(panel.locator('.wk-save-slot')).to_have_count(1)
        expect(panel.locator('.wk-save-slot')).to_have_text('')
        expect(page.locator('.app-footer').get_by_role('status').filter(has_text=re.compile('Waiting to check|Checking the rota'))).to_have_count(0)
        assert len({p['person_id'] for s in h.detail(w)['shifts'] for p in s['participants']}) == 2
    finally:
        context.close()


def test_a_reply_the_page_cannot_patch_claims_no_result_and_a_cancelled_check_keeps_submit_held(browser, live_server, h):  # noqa: F811
    w, context, page = start(browser, live_server, h)
    try:
        # The reply's delta is refused here: the panel draws from the copy it had, whose last check was complete. The
        # reply's own scope still says it checked only the edit.
        scopes = []
        page.route(re.compile(r'.*/api/commands$'), refuse_delta(scopes))
        open_rota(page, w['id'])
        page.evaluate(FLASHES)
        first, _ = assign_first(page, open_gap(page))
        no_whole_rota_claim(page)
        assert scopes == ['command_changes'], scopes
        held_as_footer_says(page, QUEUED)
        # Cancel the check: nothing is queued now, so the footer says "Not checked" (never "Waiting to check"), and the
        # latest revision is unchecked, so Submit stays held, saying the same.
        cancel = page.locator('.app-footer').get_by_role('button', name=re.compile(r'^Cancel: '))
        expect(cancel).to_be_visible(timeout=20000)
        cancel.click()
        expect(cancel).to_have_count(0, timeout=20000)
        held_as_footer_says(page, 'Not checked', press=True)
        expect(page.locator('.app-footer')).not_to_contain_text(QUEUED)
        assert not checks_open(h, w), 'the cancelled check is not queued'
    finally:
        context.close()


def test_a_check_that_has_not_started_keeps_submit_held_and_the_panel_stops_saying_checking(browser, live_server, h):  # noqa: F811
    w, context, page = start(browser, live_server, h)
    try:
        # Nobody runs the queued check (no live worker in the rota's summary) and the page's take-over is refused.
        takeovers = []

        def no_worker(route):
            response = route.fetch()
            route.fulfill(response=response, json={**response.json(), 'worker_liveness': 'stopped'})

        def refused_takeover(route):
            sent = route.request.post_data_json or {}
            if sent.get('kind') == 'job.cancel' and (sent.get('payload') or {}).get('only_if_queued'):
                takeovers.append(sent['target_id'])
                return route.fulfill(status=409, json={'error': {'code': 'WORKFLOW_STATE', 'message': 'The check could not be taken over.'}})
            route.continue_()
        page.route(re.compile(r'.*/api/workspaces/[^/?]+/summary$'), no_worker)
        page.route(re.compile(r'.*/api/commands$'), refused_takeover)
        open_rota(page, w['id'])
        panel = open_gap(page)
        first, _ = assign_first(page, panel)
        expect(panel.get_by_role('status').filter(has_text=first + ' added to the draft')).to_be_visible(timeout=20000)
        held_as_footer_says(page, "Check hasn't started")
        # The panel's slot no longer says the check is running, and keeps its place.
        expect(panel.locator('.wk-save-slot')).to_have_count(1)
        expect(panel.locator('.wk-save-slot')).to_have_text('')
        queued = checks_open(h, w)
        assert queued, 'the check is still queued: nobody ran it'
        assert takeovers == [job['id'] for job in queued]
        expect(page.locator('.check-stopped')).to_have_count(0)
    finally:
        context.close()


def test_an_older_check_failed_by_the_next_edit_never_draws_stopped_before_the_newer_check_is_on_the_page(browser, live_server, h):  # noqa: F811
    """Amendment drafts and replies the page cannot patch: the page reads the rota whole after each edit, so the older
    check's end (the server failed it as stale when the next edit queued its own) can arrive before the newer check is
    on the page. It is dropped: no "Check stopped" in the footer or above the grid, and Submit keeps the footer's words."""
    w, context, page = start(browser, live_server, h)
    try:
        scopes = []
        page.route(re.compile(r'.*/api/commands$'), refuse_delta(scopes))
        open_rota(page, w['id'])
        assign_first(page, open_gap(page))
        held_as_footer_says(page, QUEUED)
        older = checks_open(h, w)
        assert len(older) == 1
        older_id, released = older[0]['id'], []
        older_reads_running(page, older_id, released)
        expect(page.locator('.app-footer [data-check-line]')).to_contain_text(RUNNING, timeout=20000)
        held_as_footer_says(page, RUNNING)
        # From the second edit on, the page's whole reads of the rota wait (parked), as a slow read does at scale.
        parked, holding = [], [True]
        whole_read = re.compile(r'.*/api/workspaces/' + re.escape(w['id']) + r'\?view=schedule$')
        page.route(whole_read, lambda route: parked.append(route) if holding else route.continue_())
        # Each `parked` check follows a later page call, which lets the held read's route handler run first.
        with page.expect_request(lambda r: bool(whole_read.search(r.url)), timeout=20000):
            panel = open_gap(page)
            second, _ = assign_first(page, panel)
        expect(panel.get_by_role('status').filter(has_text=second + ' added to the draft')).to_be_visible(timeout=20000)
        assert parked, 'the page reads the rota whole after the edit'
        assert scopes == ['command_changes', 'command_changes'], scopes
        jobs = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']
        assert next(j for j in jobs if j['id'] == older_id)['status'] == 'failed', 'the next edit replaced the older check'
        assert checks_open(h, w), 'the newer edit has its own check'
        # The older check's end reaches the page now, while the newer check is not on it yet. Once the page has dealt
        # with it (its next whole read is asked for), nothing says the check stopped.
        reads = len(parked)
        with page.expect_request(lambda r: bool(whole_read.search(r.url)), timeout=20000), \
                page.expect_response(lambda r: r.url.endswith('/api/jobs/' + older_id + '/status'), timeout=20000):
            released.append(True)
        expect(page.locator('.check-stopped')).to_have_count(0)
        assert len(parked) > reads, 'the dropped check asks for the rota as it is now'
        expect(page.locator('.app-footer')).not_to_contain_text('Check stopped')
        held_as_footer_says(page)
        # The reads land: the newer check is on the page, and Submit waits for it in the footer's words.
        holding.clear()
        for route in parked:
            with contextlib.suppress(Exception):  # a read the page has since replaced may be gone
                route.continue_()
        expect(page.locator('.app-footer [data-check-line]')).to_contain_text(QUEUED, timeout=20000)
        held_as_footer_says(page, QUEUED)
        expect(page.locator('.app-footer')).not_to_contain_text('Check stopped')
    finally:
        context.close()

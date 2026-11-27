"""B6 (Home on a phone) and B7 (waiting and errors), mockups/B: a coordinator's phone Needs you keeps only what a phone
completes and Today says who is on duty (never a ratio); staff tell the coordinator they can't make a duty; a slow Home
card offers Retry alone; a rota check shows its elapsed time with Cancel and, when it stops, names why; a sign-in from
the wrong address says where to go. Home rows are shaped as /api/home sends them (an approved absence is
`published_absence_conflicts`); a phone keeps the rows it can finish and counts the rest. FEB_SHOTS=<folder> saves the screens for review against the mockup shots."""
import json
import os
import re
from datetime import datetime, timedelta

import pytest
from packages.domain.time import now as clock_now
from playwright.sync_api import expect
from packages.contracts.schemas import ABSENCE_REPORT_LABEL

from .test_browser import route_fetch, browser, live_server, sign_in, navigate, expect_loaded, new_context, open_app, wait_held, wait_until  # noqa: F401
from .test_browser_home import populated, local_today, PHONE, DESKTOP

pytestmark = pytest.mark.browser
SHOTS = os.getenv('FEB_SHOTS')


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + '.png'))


def sizes(page, theme, name, loaded=True):
    """The screen at phone and desktop width in `theme`, named <screen>--<state>--<phone|desktop>-<theme>. `loaded`: wait
    for the page's reads after each resize (the phone scope redraws Home when the width crosses the breakpoint)."""
    if not SHOTS:
        return
    before = page.viewport_size
    for label, size in (('desktop', {'width': 1440, 'height': 900}), ('phone', PHONE)):
        page.set_viewport_size(size)
        # The layout settles (the navigation rail or drawer moves) before the picture.
        if loaded:
            expect(page.locator('#main .loading')).to_have_count(0)
        page.evaluate("() => Promise.all(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => {})))")
        shot(page, f'{name}--{label}-{theme}')
    page.set_viewport_size(before)


def json_route(page, pattern, edit):
    def handle(route):
        # A full answer to edit, never a 304 for the page's remembered ETag.
        response = route_fetch(route, headers={k: v for k, v in route.request.all_headers().items() if k.lower() != 'if-none-match'})
        body = response.json()
        edit(body)
        route.fulfill(response=response, body=json.dumps(body))
    page.route(pattern, handle)


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_phone_coordinator_needs_you_and_today(browser, live_server, h, theme):
    """B6: rows a phone can finish (Replace, swaps, a draft's Fix list), then "N more items need a desktop" with no button to a
    wall; no Rota card or New rota; Today is clinic · time · "1 open" or "N on duty"."""
    populated(h)
    day = local_today().isoformat()
    person = h.seed['people'][0]
    absent = {'publication_id': 'pub-1', 'workspace_id': 'w-1', 'shift_id': 'shift-1', 'assignment_id': 'a-1', 'person_id': person,
              'start': f'{day}T10:00:00Z', 'end': f'{day}T13:00:00Z'}
    action = {'key': 'amend_publication', 'publication_id': 'pub-1', 'workspace_id': 'w-1', 'shift_id': 'shift-1', 'date': day,
              'amendment_shift_state': 'not_created'}

    def home(body):
        body['attention'] = [{'key': 'published_absence_conflicts', 'count': 1, 'sample': [absent], 'action': action},
                             {'key': 'swaps_to_apply', 'count': 2, 'sample': [{'proposer': 'Staff 01(test)', 'recipient': 'Sam Lee', 'start': f'{day}T05:00:00Z'}]},
                             {'key': 'no_rank', 'count': 11}, {'key': 'unchecked', 'count': 3, 'rota_id': 'x', 'rota_name': 'Draft'}]

    def gap(body):
        # A session still to come today is short (one that has ended is not listed on a phone).
        shift = next(s for s in body['shifts'] if len(s['participants']) == 1 and datetime.fromisoformat(s['end']) > clock_now())
        body['coverage'].append({'shift_id': shift['id'], 'status': 'gap', 'required': 2, 'scheduled': 1})
        shift['staffing'] = {**(shift.get('staffing') or {}), 'places_open': 1}  # maintenance's staffing, read first
    context, page = sign_in(browser, live_server, 'coordinator', PHONE, phone_notice=True)
    try:
        page.emulate_media(color_scheme=theme)
        json_route(page, '**/api/home', home)
        json_route(page, '**/api/schedule/sessions?**', gap)
        page.reload()
        expect_loaded(page, 'home')
        main = page.locator('#main')
        rows = main.locator('.home-needs-list li')
        expect(rows).to_have_count(3)
        expect(rows.nth(0)).to_contain_text('Published duty overlaps unavailability')
        expect(rows.nth(1)).to_contain_text('Swaps to apply')
        # A draft not checked is a draft row like any other (one rule): its name, Needs a check, Fix list.
        expect(rows.nth(2)).to_contain_text('Draft')
        expect(rows.nth(2)).to_contain_text('Needs a check')
        expect(rows.nth(2).get_by_role('button', name='Fix list', exact=True)).to_be_visible()
        elsewhere = main.locator('.home-elsewhere')
        expect(elsewhere).to_have_text('1 more item needs a desktop: People without a rank')
        expect(elsewhere.get_by_role('button')).to_have_count(0)
        expect(main.locator('.home-rota')).to_have_count(0)
        expect(page.get_by_role('button', name='New rota', exact=True)).to_have_count(0)
        today = main.locator('.home-today')
        expect(today.locator('table')).to_have_count(0)
        expect(today.locator('.home-today-list li').filter(has_text='1 open')).to_have_count(1)
        expect(today.locator('.home-today-list li').filter(has_text=re.compile(r'\d+ on duty$'))).not_to_have_count(0)
        expect(today).not_to_contain_text(re.compile(r'\d+/\d+'))
        expect(main.get_by_role('region', name='Away today')).to_be_visible()
        sizes(page, theme, 'b6-home-on-phone--coord')
        rows.nth(0).get_by_role('button', name='Replace', exact=True).click()
        expect(page).to_have_url(re.compile(rf'#schedule/week/{day}\?source=pub-1&shift=shift-1$'))
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_staff_cant_make_it_tells_the_coordinator(browser, live_server, h, theme):
    """B6, D13: Can't make it (Sick · Emergency · Other, an optional note) sends request.create absence_report for that
    duty against the server; the hero then says it was reported and waits for approval (never a delivery time it does
    not have), My requests says Waiting for approval (SX-08: finding cover only once approved) with Withdraw, and Undo withdraws."""
    me = h.get('staff', '/api/me')['person_id']
    day = local_today() + timedelta(days=1)
    w = h.workspace('Can’t make it rota', day.isoformat(), (day + timedelta(days=7)).isoformat())
    w = h.shift(w, start=f'{day}T13:00:00+03:00', end=f'{day}T16:00:00+03:00', people=[me])['workspace']
    h.publish(w)
    sent = []

    def commands(route):  # what the client sends; the real server answers
        body = json.loads(route.request.post_data or '{}')
        if body.get('kind') in ('request.create', 'request.decide'):
            sent.append(body)
        route.fallback()
    context, page = sign_in(browser, live_server, 'staff', PHONE, phone_notice=True)
    try:
        page.emulate_media(color_scheme=theme)
        page.route('**/api/commands', commands)
        page.reload()
        expect_loaded(page, 'home')
        hero = page.locator('#main .home-hero')
        expect(hero).to_contain_text('Next duty · tomorrow')
        sizes(page, theme, 'b6-home-on-phone--staff')
        hero.get_by_role('button', name=ABSENCE_REPORT_LABEL, exact=True).click()
        dialog = page.get_by_role('dialog', name=ABSENCE_REPORT_LABEL + '?')
        reason = dialog.get_by_role('radiogroup', name='Reason')
        expect(reason.get_by_role('radio', name='Sick')).to_have_attribute('aria-checked', 'true')
        sizes(page, theme, 'b6-home-on-phone--staff-report')
        reason.get_by_role('radio', name='Emergency').click()
        dialog.get_by_label('Note for the coordinator').fill('Family emergency')
        dialog.get_by_role('button', name='Tell the coordinator').click()
        expect(page.get_by_role('status').filter(has_text='Reported. An approver decides the absence')).to_be_visible()
        payload = sent[0]['payload']
        assert payload['reason'] == 'emergency' and payload['category'] == 'emergency' and payload['note'] == 'Family emergency'
        assert payload['publication_id'] and payload['assignment_id']
        expect(hero).to_contain_text('Tomorrow · you can’t make it')
        expect(hero).to_contain_text('Reported · waiting for approval')
        expect(hero.get_by_role('button', name=ABSENCE_REPORT_LABEL)).to_have_count(0)
        mine = page.get_by_role('region', name='My requests')
        expect(mine).to_contain_text('Waiting for approval')
        expect(mine).not_to_contain_text('Finding cover')
        expect(mine.get_by_role('button', name='Withdraw: ' + ABSENCE_REPORT_LABEL)).to_be_visible()
        sizes(page, theme, 'b6-home-on-phone--staff-sent')
        page.get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_role('status').filter(has_text='Report withdrawn.')).to_be_visible()
        assert sent[-1]['kind'] == 'request.decide' and sent[-1]['payload']['decision'] == 'withdraw'
        expect(hero.get_by_role('button', name=ABSENCE_REPORT_LABEL, exact=True)).to_be_visible()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_a_slow_home_card_says_still_loading_and_retries_alone(browser, live_server, h, theme):
    """B7 slow: the frame and the other cards are there; Today's skeleton waits quietly, past 15 s says "Still loading"
    with its elapsed time (no Retry while live), and at the read's limit offers Retry, which reads Today again."""
    populated(h)
    held = []
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        page.emulate_media(color_scheme=theme)
        navigate(page, 'requests')
        # A new visit: Home opened again in the same one is drawn from the replies this tab kept (api.ts keptReads,
        # c6d9edec1), so its reads would not reach the server at all. Reloaded here, Home's opening reads every card.
        page.reload()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'requests')
        page.clock.pause_at(page.evaluate("new Date(Date.now()+1000).toISOString()"))
        page.route('**/api/schedule/sessions?**', lambda route: held.append(route))
        # The other cards' reads answer first, so the paused clock then settles them (their 400 ms minimum).
        answered = set()
        page.on('response', lambda r: answered.update(k for k in ('/api/home?', '/api/home/rotas', '/api/calendar') if k in r.url + '?'))
        page.evaluate("location.hash='home'")
        wait_held(page, held)
        wait_until(page, lambda: len(answered) == 3, lambda: f"Home's other cards never answered: only {sorted(answered)}")
        page.clock.run_for(16000)
        today = page.get_by_role('region', name='Today')
        expect(today.locator('.sk-slow')).to_contain_text('Still loading · 0:1')  # past 15 s: the one quiet line
        expect(today.get_by_role('button', name='Retry loading today', exact=True)).to_have_count(0)  # never while the read is live
        expect(page.locator('#main .home-month')).to_have_count(4)  # the Rota card loaded on its own
        sizes(page, theme, 'b7-waiting-and-errors--slow', loaded=False)
        page.clock.run_for(45000)  # the read's limit (api.ts READ_TIMEOUT): the held read is aborted, Retry offered
        expect(today.locator('.read-failed')).to_contain_text('Didn’t load in 1:00.')
        page.unroute('**/api/schedule/sessions?**')
        # Pressed while the clock is still held, before any timer due meanwhile redraws the page.
        today.get_by_role('button', name='Retry loading today', exact=True).click()
        page.clock.resume()
        expect(today.locator('table')).to_be_visible()
    finally:
        page.unroute_all(behavior='ignoreErrors')  # a held request is left unanswered; closing the page drops it
        context.close()


def stale_draft(h):
    day = local_today() + timedelta(days=40)
    w = h.workspace('Check timing draft', day.isoformat(), (day + timedelta(days=7)).isoformat())
    return h.shift(w, start=f'{day}T08:00:00+03:00', end=f'{day}T12:00:00+03:00')['workspace'], day


def unchecked(page, rota):
    """The draft reads as changed since its last check, so the Schedule checks it again (autoCheck). The server says
    whether its stored check is current (validation_current, GEN-FIX b8e1874f5); the fingerprint is kept stale too."""
    def edit(body):
        if body.get('workspace', {}).get('id') == rota:
            body['workspace']['validation'] = {**(body['workspace'].get('validation') or {}), 'fingerprint': 'before-the-change'}
            body['workspace']['validation_current'] = False
    json_route(page, re.compile(r'/api/workspaces/' + rota + r'(\?|$)'), edit)


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_a_long_check_shows_elapsed_time_and_cancel_stops_it_on_the_server(browser, live_server, h, theme):
    """B7 check (maintenance): with a worker serving jobs the check is a validation job; the footer's one working item is
    "Checking the rota · m:ss" with Cancel (no ETA), which sends job.cancel and leaves the last results "Not checked"."""
    w, day = stale_draft(h)
    sent = []
    job = {'id': 'check-job', 'revision': 1, 'kind': 'validation', 'status': 'running', 'workspace_id': w['id']}

    def commands(route):
        body = json.loads(route.request.post_data or '{}')
        if body.get('kind') == 'job.create' and body['payload'].get('kind') == 'validation':
            sent.append(body)
            route.fulfill(json={'job': job})
        elif body.get('kind') == 'job.cancel':
            sent.append(body)
            route.fulfill(json={'job': {**job, 'status': 'cancelled'}})
        else:
            route.continue_()
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        page.emulate_media(color_scheme=theme)
        unchecked(page, w['id'])
        page.route('**/health/service', lambda route: route.fulfill(json={'status': 'ready'}))
        page.route('**/api/jobs/check-job/status', lambda route: route.fulfill(json={'job': job}))
        page.route('**/api/commands', commands)
        navigate(page, f'schedule/week/{day}?edit={w["id"]}')
        footer = page.locator('.app-footer')
        working = footer.locator('.long-work')
        with page.expect_response('**/api/jobs/check-job/status'):
            expect(working).to_contain_text(re.compile(r'Checking the rota · \d+:\d\d'))
        sizes(page, theme, 'b7-waiting-and-errors--check')
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/commands')
                                 and (r.post_data_json or {}).get('kind') == 'job.cancel'):
            working.get_by_role('button', name='Cancel: checking the rota').click()
        expect(footer.locator('.long-work')).to_have_count(0)
        expect(footer.get_by_role('button', name='Not checked')).to_be_visible()
        assert sent[0]['payload'] == {'workspace_id': w['id'], 'kind': 'validation', 'automatic': True}
        assert sent[-1]['kind'] == 'job.cancel' and sent[-1]['target_id'] == 'check-job'
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_check_without_a_worker_offers_no_cancel_it_cannot_honour(browser, live_server, h):
    """Without a worker the check is the synchronous workspace.validate, which the server cannot stop: elapsed time, no
    Cancel button."""
    w, day = stale_draft(h)
    held = []
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        unchecked(page, w['id'])
        page.route('**/api/commands', lambda route: held.append(route) if json.loads(route.request.post_data or '{}').get('kind') == 'workspace.validate' else route.continue_())
        navigate(page, f'schedule/week/{day}?edit={w["id"]}')
        wait_held(page, held)
        working = page.locator('.app-footer .long-work')
        expect(working).to_contain_text(re.compile(r'Checking the rota · \d+:\d\d'))
        expect(working.get_by_role('button')).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')  # a held request is left unanswered; closing the page drops it
        context.close()


@pytest.mark.parametrize('worker', [True, False])
def test_a_recheck_keeps_its_counts_with_cancel_only_for_a_job(browser, live_server, h, worker):
    """DL-019: a re-check (a stored check, then the rota changed) keeps the last counts beside "Checking the rota · m:ss",
    the same line as a first check. Cancel stands there only when the check is a job the server can stop: pressing it
    sends job.cancel and keeps the counts, "Not checked". The synchronous check (no worker) offers no Cancel."""
    w, day = stale_draft(h)
    h.lifecycle(h.detail(w)['workspace'], 'validate')
    sent, held = [], []
    job = {'id': 'recheck-job', 'revision': 1, 'kind': 'validation', 'status': 'running', 'workspace_id': w['id']}

    def commands(route):
        body = json.loads(route.request.post_data or '{}')
        if worker and body.get('kind') == 'job.create' and body['payload'].get('kind') == 'validation':
            sent.append(body)
            route.fulfill(json={'job': job})
        elif worker and body.get('kind') == 'job.cancel':
            sent.append(body)
            route.fulfill(json={'job': {**job, 'status': 'cancelled'}})
        elif not worker and body.get('kind') == 'workspace.validate':
            held.append(route)
        else:
            route.continue_()
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        unchecked(page, w['id'])
        if worker:
            page.route('**/health/service', lambda route: route.fulfill(json={'status': 'ready'}))
            page.route('**/api/jobs/recheck-job/status', lambda route: route.fulfill(json={'job': job}))
        page.route('**/api/commands', commands)
        navigate(page, f'schedule/week/{day}?edit={w["id"]}')
        if not worker:
            wait_held(page, held)
        footer = page.locator('.app-footer')
        pills = footer.locator('.fix-pills')
        working = pills.locator('.long-work')
        expect(working).to_contain_text(re.compile(r'Checking the rota · \d+:\d\d'))
        counts = pills.locator('.health-pill .num').all_inner_texts()
        if not worker:
            expect(working.get_by_role('button')).to_have_count(0)
            return
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/commands')
                                 and (r.post_data_json or {}).get('kind') == 'job.cancel'):
            working.get_by_role('button', name='Cancel: checking the rota').click()
        expect(footer.locator('.long-work')).to_have_count(0)
        expect(footer.get_by_role('button', name='Not checked')).to_be_visible()
        assert pills.locator('.health-pill .num').all_inner_texts() == counts  # the last counts, kept
        assert sent[-1]['kind'] == 'job.cancel' and sent[-1]['target_id'] == 'recheck-job'
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_a_stopped_check_names_its_cause_and_checks_again(browser, live_server, h, theme):
    """B7 failed: one line names the cause (maintenance `cause`) with Check again; the footer pill reads "Check stopped · Check again" (DL-018)."""
    w, day = stale_draft(h)
    refusals = [1]

    def commands(route):
        if json.loads(route.request.post_data or '{}').get('kind') == 'workspace.validate' and refusals:
            refusals.pop()
            route.fulfill(status=409, json={'error': {'code': 'STALE_INPUTS', 'message': 'Inputs changed during the check',
                'details': {'cause': {'kind': 'wish', 'person': 'Staff 03(test)', 'date': '2026-12-17'}}}})
        else:
            route.continue_()
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        page.emulate_media(color_scheme=theme)
        unchecked(page, w['id'])
        page.route('**/api/commands', commands)
        navigate(page, f'schedule/week/{day}?edit={w["id"]}')
        line = page.locator('#main .check-stopped')
        expect(line).to_contain_text(re.compile(r'The check stopped after \d+:\d\d\. Staff 03\(test\) added a wish for Thu 17 Dec while it ran\.'))
        # DL-018: one state, one verb: the pill says what the line says
        expect(page.locator('.app-footer').get_by_role('button', name='Check stopped · Check again')).to_be_visible()
        sizes(page, theme, 'b7-waiting-and-errors--failed')
        with page.expect_request(lambda r: r.method == 'POST' and '/api/commands' in r.url and 'workspace.validate' in (r.post_data or '')):
            line.get_by_role('button', name='Check again', exact=True).click()
        expect(page.locator('#main .check-stopped')).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('canonical', [True, False], ids=['redirect', 'message'])
def test_sign_in_from_another_address_goes_to_the_usual_one(browser, live_server, h, canonical):
    """D15 (B7 signin): ORIGIN_REJECTED with `canonical_origin` goes there; without it, plain words where to sign in and
    no form that cannot work here."""
    usual = live_server.replace('127.0.0.1', 'localhost')
    details = {'canonical_origin': usual} if canonical else {}
    context = new_context(browser, PHONE)
    page = open_app(context, live_server)
    try:
        page.route('**/auth/identify', lambda route: route.fulfill(status=403, json={'error': {'code': 'ORIGIN_REJECTED', 'message': 'A same-origin request is required', 'details': details}}))
        page.get_by_label('Username or email', exact=True).fill('staff')
        page.get_by_role('button', name='Continue', exact=True).click()
        if canonical:
            expect(page).to_have_url(re.compile('^' + re.escape(usual)))
        else:
            card = page.locator('.login-form')
            host = re.escape(live_server.split('//')[1])
            expect(card.get_by_role('status')).to_have_text(re.compile(rf'^Sign in at .+’s usual address\. This address \({host}\) can’t keep you signed in\.$'))
            expect(card.locator('form, input')).to_have_count(0)
            expect(page.locator('body')).not_to_contain_text('same-origin')
            for theme in ('light', 'dark'):
                page.emulate_media(color_scheme=theme)
                sizes(page, theme, 'b7-waiting-and-errors--signin')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_exports_idle_has_no_red_text(browser, live_server, h, theme):
    """B7 exports: before anything is chosen nothing is red; the Person picker says what is needed."""
    from .test_browser_insights_system import two_versions
    me = h.get('staff', '/api/me')['person_id']
    two_versions(h, people=[me, next(p for p in h.seed['people'] if p != me)])
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        page.emulate_media(color_scheme=theme)
        navigate(page, 'insights/exports')
        calendar = page.locator('#main .export-row').filter(has_text='Personal calendar')
        expect(calendar.get_by_label('Person', exact=True).locator('option[value=""]')).to_have_text('Choose a person')
        expect(page.locator('#main .field-error, #main .notice.error')).to_have_count(0)
        sizes(page, theme, 'b7-waiting-and-errors--exports')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

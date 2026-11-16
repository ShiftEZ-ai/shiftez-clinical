"""Auto-schedule live view reviewed: the grid follows the
solver's better schedules from job.progress.events, the footer carries one run strip, the finish card says what the run
gained, and Configuration › Auto-schedule › Animation sets how. The event stream is the real API's job list with a
running job's progress written in by the test (the worker's side is tested in the API's own tests)."""
import os
import re
import threading
from pathlib import Path

import pytest
from playwright.sync_api import expect

from apps.worker.main import process_one
from packages.domain.time import now as clock_now
from .test_browser import browser, close_popover, live_server, open_config, open_rota, pick_view, route_fetch, sign_in, navigate, view_settings, wait_held  # noqa: F401
from .test_browser_cp_sat import cp_server  # noqa: F401
from .test_browser_footer_one_line import one_line

pytestmark = pytest.mark.browser
SHOTS = os.getenv('ASLIVE_SHOTS')
STEP = '.asl-step:not(.asl-roll-old)'  # the step shown (not the old one rolling out)  # a folder: the run, the footer and the settings are saved there for review

# D-123: "5/5 Done" stays DONE_STRIP_MS (3 s), so the strip's Done state is read in one atomic read, in the first frame
# that shows it (polled every animation frame), never by separate assertions racing its exit.
DONE_READ = ("()=>{const s=document.querySelector('.app-footer .asl-strip'),t=s?.querySelector('" + STEP + "')?.textContent;"
             "return t==='5/5 Done'&&{step:t,left:s.querySelector('.asl-left')?.textContent??'',"
             "bar:s.querySelector('.asl-bar')?.className??''};}")


def seen_done(page, timeout=20000):
    """The footer strip at Done: its step, time used and bar class, all from the same frame."""
    return page.wait_for_function(DONE_READ, polling='raf', timeout=timeout).json_value()


# Every animation the page starts, with the properties it animates and whether it is part of the live view.
WATCH = """(() => {window.__anim=[];const animate=Element.prototype.animate;
  Element.prototype.animate=function(frames,options){try{const list=Array.isArray(frames)?frames:[frames];
    window.__anim.push({live:!!this.closest?.('#schedule-view,.asl-strip,.asl-preview'),props:[...new Set(list.flatMap(f=>Object.keys(f)))]});}catch(e){}
    return animate.call(this,frames,options);};
  window.__flew=0;new MutationObserver(r=>{for(const x of r)if(x.target.classList?.contains('asl-flying'))window.__flew++;})
    .observe(document,{subtree:true,attributes:true,attributeFilter:['class']});
  // Long animation frames (over 50 ms) whose work includes the live view's own scripts.
  window.__loaf=[];try{new PerformanceObserver(l=>window.__loaf.push(...l.getEntries().filter(e=>e.scripts.some(x=>/autoschedule_/.test(x.sourceURL)))
    .map(e=>Math.round(e.duration)))).observe({type:'long-animation-frame',buffered:false});}catch(e){}})()"""


def shot(page, name, locator=None, settle=False):
    if SHOTS:
        if settle:
            page.wait_for_function("() => document.getAnimations().every(a => a.playState !== 'running')")
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        (locator or page).screenshot(path=str(Path(SHOTS) / f'{name}.png'))


def rota(h, sessions, name='Live auto-schedule'):
    """A checked draft with these sessions (start hour on a day of October 2026, who works it, and optionally its duty
    type and the seed's service index)."""
    w = h.workspace(name)
    for day, hour, people, *extra in sessions:
        w = h.shift(w, f'2026-10-{day:02d}T{hour:02d}:00:00+03:00', f'2026-10-{day:02d}T{hour + 4:02d}:00:00+03:00', people=people,
                    typ=extra[0] if extra else None, service=extra[1] if len(extra) > 1 else 0)['workspace']
    w = h.command('coordinator', 'workspace.validate', {'reason': 'Routine rota check'}, w['id'], w['revision'])['workspace']
    shifts = sorted(h.detail(w)['shifts'], key=lambda s: s['start'])
    return w, [s['id'] for s in shifts]


class Stream:
    """The rota's job list as the server answers it, with its generation job shown running at the progress set here."""
    def __init__(self, page, origin, workspace_id):
        self.progress = {'stage': 'preparing', 'elapsed_seconds': 1}
        self.extra = {}  # more fields of the running job (its search_control, generation_scope)
        self.events = []
        self.on = True
        self.loaded = []
        self.reads = 0
        # The job list, and the running job read on its own while it is shown live (its compact status read, maintenance).
        page.route(origin + '/api/jobs?workspace_id=' + workspace_id, self.answer)
        page.route(origin + '/api/jobs/*', self.answer)
        page.route(re.compile(re.escape(origin) + r'/api/jobs/[^/?]+/status(\?.*)?$'), self.answer)

    def wait_reads(self, page, count=2):
        """Until the page has read the job `count` more times (the progress set here has reached it)."""
        target = self.reads + count
        while self.reads < target:  # each answered read finishes a request (the route handler counted it first)
            page.wait_for_event('requestfinished', timeout=60_000)

    def answer(self, route):
        self.reads += 1
        response = route_fetch(route)
        body = response.json()
        if self.on:
            for job in body['jobs'] if 'jobs' in body else [body['job']] if body.get('job') else []:
                if job.get('kind') == 'generation':
                    job.update({'status': 'running', 'result': None, 'started_at': clock_now().isoformat(), 'search_control': {'minutes': 2},
                                'progress': {**self.progress, 'events': self.events[-50:], 'improvements_found': len(self.events)},
                                **self.extra})
        route.fulfill(response=response, json=body)

    def event(self, adds=(), removes=(), **extra):
        pair = lambda x: {'shift_id': x[0], 'person_id': x[1]}
        self.events.append({'seq': len(self.events) + 1, 'at': clock_now().isoformat(), 'kind': 'improvement',
                            'objective': 100 - len(self.events), 'bound': 0, 'adds': [pair(x) for x in adds],
                            'removes': [pair(x) for x in removes], 'open_places': 0, 'warnings': 0, 'truncated': False, **extra})


def start_run(browser, origin, h, sessions, view='People', **options):
    w, ids = rota(h, sessions)
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])
    context, page = sign_in(browser, origin, 'coordinator', viewport={'width': 1440, 'height': 900}, **options)
    page.add_init_script(WATCH)
    stream = Stream(page, origin, w['id'])
    page.on('request', lambda request: stream.loaded.append(request.url))  # the resource timing buffer is full before a run
    open_rota(page, w['id'])
    page.reload()  # the watcher is installed from the first script on
    pick_view(page, view)
    # A run going opens the panel; closed, the grid and the footer carry the run.
    auto = page.get_by_role('button', name='Auto-schedule', exact=True)
    expect(auto).to_have_attribute('aria-expanded', 'true')
    auto.click()
    expect(page.locator('#auto-panel')).to_be_hidden()
    return context, page, stream, w, ids


def chip(page, session, person):
    return page.locator(f'#schedule-view .pg-chip[data-session="{session}"][data-person="{person}"]')


def cell(page, person, day):
    return page.locator(f'#schedule-view .pg-row[data-row-person="{person}"] .pg-cell[data-day="2026-10-{day:02d}"]')


def test_a_run_plays_on_the_people_grid_from_the_solvers_events(browser, cp_server, h):
    a, b, c = h.seed['people'][:3]
    context, page, stream, w, (s_morning, s_afternoon, s_open, s_later) = start_run(browser, cp_server, h, [
        (4, 8, [a]), (4, 13, [b]), (5, 8, []), (6, 8, [c])])
    try:
        strip = page.locator('.app-footer .footer-status > .asl-strip')
        expect(strip).to_be_visible()
        expect(strip.locator(STEP)).to_have_text('2/5 Preparing')
        expect(strip.locator('.asl-left')).to_have_text('0 s used')  # time used as people read it, never a guessed end (A4)
        expect(strip.get_by_role('status')).to_have_text('Step 2 of 5: Preparing')
        assert any(u.endswith('/autoschedule_live.js') for u in stream.loaded)  # loaded for the run
        expect(page.locator('#schedule-view .people-grid')).to_have_attribute('aria-busy', 'true')
        open_row = page.locator('#schedule-view .pg-open-row')
        expect(open_row.locator('.pg-cell[data-day="2026-10-05"] .pg-open')).to_have_text('+1')

        # 2 · The first schedule: the open place on the 5th goes to c, flying from the Open places row.
        stream.progress = {'stage': 'finding', 'elapsed_seconds': 6, 'search_started_seconds': 4, 'time_limit_seconds': 120}
        stream.event(adds=[(s_open, c)])
        expect(chip(page, s_open, c)).to_be_visible()
        expect(open_row.locator('.pg-cell[data-day="2026-10-05"] .pg-open')).to_have_count(0)
        expect(strip.locator(STEP)).to_have_text('3/5 Searching')
        expect(strip.locator('.asl-left')).to_have_text(re.compile(r'^\d s used$'))
        page.wait_for_function('() => window.__flew > 0')

        # 3 · Better schedules: a and b swap the two sessions of the 4th (a flight each), then c's later duty goes to a.
        stream.progress = {**stream.progress, 'stage': 'improving', 'elapsed_seconds': 20}
        stream.event(adds=[(s_morning, b), (s_afternoon, a)], removes=[(s_morning, a), (s_afternoon, b)])
        stream.event(adds=[(s_later, a)], removes=[(s_later, c)])
        expect(cell(page, a, 4).locator(f'.pg-chip[data-session="{s_afternoon}"]')).to_have_count(1)
        expect(cell(page, b, 4).locator(f'.pg-chip[data-session="{s_morning}"]')).to_have_count(1)
        expect(chip(page, s_later, a)).to_have_count(1)
        expect(chip(page, s_later, c)).to_have_count(0)
        expect(strip.locator(STEP)).to_have_text('3/5 Searching')
        expect(strip.get_by_role('status')).to_have_text('Step 3 of 5: Searching')
        bar = strip.get_by_role('progressbar', name='Search time used')
        expect(bar).to_have_attribute('aria-valuetext', re.compile(r'^[12]\d s used$'))
        shot(page, 'run-people-1440')
        shot(page, 'footer-run-1440', page.locator('.app-footer'))
        one_line(page, 'during a run')

        # Motion is transform and opacity only, and no frame with the live view's work runs long (headless, no GPU).
        anims = page.evaluate('window.__anim.filter(a=>a.live)')
        assert anims and all(set(a['props']) <= {'transform', 'opacity', 'offset', 'easing', 'composite'} for a in anims), anims
        long_frames = page.evaluate('window.__loaf')
        assert max(long_frames, default=0) < (200 if os.getenv('CI') else 100), long_frames

        # 5 · Done: the result is the truth; the strip says the time used and a card what was gained.
        stream.on = False
        process_one(h.app.state.engine)
        at_done = seen_done(page)
        assert at_done['step'] == '5/5 Done'
        assert re.fullmatch(r'(< 1 s|\d+ s|\d+ min( \d+ s)?) used', at_done['left']), at_done
        assert re.search(r'\bdone\b', at_done['bar']), at_done
        expect(page.locator('#schedule-view .people-grid')).to_have_attribute('aria-busy', 'false')
        card = page.get_by_role('region', name='Auto-schedule result')
        expect(card).to_contain_text('Ready for review')
        expect(card).to_contain_text('Duties changed')
        expect(card.locator('dt:text-is("Better schedules") + dd')).to_have_text('3')
        expect(card).not_to_contain_text('need staff')  # nothing the footer already shows
        shot(page, 'finish-card-1440', settle=True)
        one_line(page, 'at Done')
        card.get_by_role('button', name='Review result', exact=True).click()
        expect(card).to_have_count(0)
        expect(page.locator('#auto-panel')).to_be_visible()
        expect(strip).to_have_count(0)  # D-123: a press in the result card ends Done at once; the footer's status returns
    finally:
        page.unroute_all(behavior='ignoreErrors')  # a job read still in flight must not fail the next test's setup
        context.close()


def test_a_real_run_ends_on_its_result(browser, cp_server, h):
    """The real API and worker (no written-in progress): the grid ends on the result's own additions, ringed."""
    w, ids = rota(h, [(4, 8, []), (5, 8, []), (6, 8, [])])
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_rota(page, w['id'])
        pick_view(page, 'People')
        page.get_by_role('button', name='Auto-schedule', exact=True).click()
        expect(page.locator('#auto-panel')).to_be_hidden()
        expect(page.locator('.asl-strip ' + STEP)).to_have_text(re.compile(r'^1/5 '))
        worker = threading.Thread(target=process_one, args=(h.app.state.engine,))
        worker.start()
        worker.join(60)
        assert seen_done(page)['step'] == '5/5 Done'
        job = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]
        added = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']['additions']
        assert added
        for a in added:
            expect(chip(page, a['shift_id'], a['participant']['person_id'])).to_have_count(1)
        expect(page.locator('#schedule-view .pg-cell.asl-changed')).to_have_count(len(added))
        expect(page.get_by_role('region', name='Auto-schedule result')).to_be_visible()
        # D-123: Done is said once; untouched, the strip leaves after DONE_STRIP_MS (3 s) and the footer's status returns.
        expect(page.locator('.app-footer .asl-strip')).to_have_count(0, timeout=10000)
    finally:
        context.close()


def test_pause_animation_holds_the_grid_until_resume(browser, cp_server, h):
    """The account menu holds the preview still while the solver continues; resuming catches up at once."""
    a, b, _ = h.seed['people'][:3]
    context, page, stream, w, (s1, s2) = start_run(browser, cp_server, h, [(4, 8, [a]), (4, 13, [b])])
    try:
        strip = page.locator('.app-footer .asl-strip')
        expect(page.get_by_role('button', name='Pause animation', exact=True)).to_have_count(0)
        page.get_by_role('button', name=re.compile('^Account menu,')).click()
        motion = page.get_by_role('menuitemcheckbox', name='Animate auto-schedule', exact=True)
        expect(motion).to_have_attribute('aria-checked', 'true')
        motion.focus()
        page.keyboard.press('Enter')
        expect(motion).to_have_attribute('aria-checked', 'false')
        page.keyboard.press('Escape')
        page.evaluate('window.__anim=[];window.__flew=0')
        stream.progress = {'stage': 'improving', 'elapsed_seconds': 9, 'search_started_seconds': 3, 'time_limit_seconds': 120}
        stream.event(adds=[(s1, b), (s2, a)], removes=[(s1, a), (s2, b)])
        stream.wait_reads(page)
        expect(cell(page, a, 4).locator(f'.pg-chip[data-session="{s1}"]')).to_have_count(1)
        expect(strip.locator(STEP)).to_have_text('3/5 Searching')
        assert page.evaluate('window.__anim.filter(a=>a.live)') == []
        assert page.evaluate('window.__flew') == 0
        one_line(page, 'still motion during a run')
        page.reload()
        # After a reload the run going opens its panel again; the menu is opened once that has happened (opening the
        # panel moves focus, which would close a menu opened before it).
        expect(page.locator('#auto-panel')).to_be_visible()
        expect(page.locator('#auto-panel').get_by_role('button', name='Stop and keep best', exact=True)).to_be_visible()
        page.get_by_role('button', name=re.compile('^Account menu,')).click()
        expect(motion).to_have_attribute('aria-checked', 'false')
        motion.click()
        expect(motion).to_have_attribute('aria-checked', 'true')
        page.keyboard.press('Escape')
        expect(cell(page, a, 4).locator(f'.pg-chip[data-session="{s2}"]')).to_have_count(1)
        expect(page.locator('#auto-panel').get_by_role('button', name='Pause animation', exact=True)).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_one_announcement_per_step_and_rota_health_folds_last(browser, cp_server, h):
    """With the panel open, the footer strip is the one live region for the step. A footer too full for one line folds
    the screen's low-value items behind +N before the rota's health; the run strip itself never folds."""
    a = h.seed['people'][0]
    context, page, stream, w, _ = start_run(browser, cp_server, h, [(4, 8, [a]), (5, 8, [])])
    try:
        stream.progress = {'stage': 'improving', 'elapsed_seconds': 9, 'search_started_seconds': 3, 'time_limit_seconds': 120}
        page.get_by_role('button', name='Auto-schedule', exact=True).click()
        expect(page.locator('#auto-panel .auto-stage')).to_be_visible()
        expect(page.locator('#auto-panel .auto-stage')).not_to_have_attribute('role', 'status')
        expect(page.locator('[role=status]').filter(has_text='Step 3 of 5: Searching')).to_have_count(1)
        status = page.locator('.app-footer .page-status > .footer-status')
        status.evaluate("s => {for(let i=1;i<=30;i++){const x=document.createElement('span');x.className='muted';x.textContent='Extra status '+i;s.append(x);}}")
        expect(status.locator('.footer-key')).to_have_text(re.compile(r'\+\d+$'))
        expect(status.locator('.asl-strip')).to_be_visible()
        health = status.locator('.health-pills > *').first
        expect(health).to_be_visible()
        expect(status.locator('span.muted', has_text='Extra status 30')).to_be_hidden()
        one_line(page, 'full footer during a run')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_reduced_motion_fades_every_change_and_nothing_flies(browser, cp_server, h):
    a, b, _ = h.seed['people'][:3]
    context, page, stream, w, (s1, s2, s_open) = start_run(browser, cp_server, h, [(4, 8, [a]), (4, 13, [b]), (5, 8, [])],
                                                           reduced_motion='reduce')
    try:
        stream.progress = {'stage': 'improving', 'elapsed_seconds': 9, 'search_started_seconds': 3, 'time_limit_seconds': 120}
        stream.event(adds=[(s1, b), (s2, a), (s_open, a)], removes=[(s1, a), (s2, b)])
        expect(cell(page, a, 4).locator(f'.pg-chip[data-session="{s2}"]')).to_have_count(1)
        expect(chip(page, s_open, a)).to_be_visible()
        strip = page.locator('.asl-strip')
        expect(strip.locator(STEP)).to_have_text('3/5 Searching')
        assert page.evaluate('window.__flew') == 0
        assert page.locator('.asl-roll-old').count() == 0  # the step swaps at once
        anims = page.evaluate('window.__anim.filter(a=>a.live)')
        assert anims == [], anims  # true reduced motion is static, including opacity
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_the_week_view_moves_a_person_between_sessions(browser, cp_server, h):
    a = h.seed['people'][0]
    context, page, stream, w, (s1, s2) = start_run(browser, cp_server, h, [(4, 8, [a]), (4, 13, [])], view='Week')
    try:
        grid = page.locator('#schedule-view .wk-grid')
        expect(grid.locator(f'.pchip[data-shift="{s1}"][data-person="{a}"]')).to_have_count(1)
        stream.progress = {'stage': 'improving', 'elapsed_seconds': 9, 'search_started_seconds': 3, 'time_limit_seconds': 120}
        stream.event(adds=[(s2, a)], removes=[(s1, a)])
        expect(grid.locator(f'.pchip[data-shift="{s2}"][data-person="{a}"]')).to_have_count(1)
        expect(grid.locator(f'.pchip[data-shift="{s1}"][data-person="{a}"]')).to_have_count(0)
        expect(grid.locator(f'.pchip.gap[data-shift="{s1}"]')).to_be_visible()  # the place it left is open
        page.wait_for_function('() => window.__flew > 0')  # cross-session moves are big moves by default
        shot(page, 'run-week-1440')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_week_preview_chips_do_not_open_the_saved_assignments(browser, cp_server, h):
    """A swap and a replacement relabel saved Week chips; until Apply they open nothing
    (no session panel, no menu, no hover card), and say they are not available."""
    a, b, c = h.seed['people'][:3]
    context, page, stream, w, (s1, s2, s3) = start_run(browser, cp_server, h, [(4, 8, [a]), (4, 13, [b]), (5, 8, [c])], view='Week')
    try:
        grid = page.locator('#schedule-view .wk-grid')
        stream.progress = {'stage': 'improving', 'elapsed_seconds': 9, 'search_started_seconds': 3, 'time_limit_seconds': 120}
        stream.event(adds=[(s1, b), (s2, a)], removes=[(s1, a), (s2, b)])  # a swap
        stream.event(adds=[(s3, a)], removes=[(s3, c)])  # c replaced by a
        changed = [grid.locator(f'.pchip[data-shift="{s}"][data-person="{p}"]') for s, p in ((s1, b), (s2, a), (s3, a))]
        for chip in changed:
            expect(chip).to_have_count(1)
            expect(chip).to_have_attribute('aria-disabled', 'true')
            chip.click(force=True)
            chip.click(button='right', force=True)
            chip.hover(force=True)
            expect(page.locator('.wk-panel')).to_have_count(0)
            expect(page.get_by_role('menu')).to_have_count(0)
            expect(page.locator('.hc-head')).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_an_empty_week_session_fills_live_with_open_places_hidden(browser, cp_server, h):
    """With Open places hidden, an empty session shows "No one yet" carrying its session;
    the live fill replaces it with the person."""
    a = h.seed['people'][0]
    context, page, stream, w, (s1, s2) = start_run(browser, cp_server, h, [(4, 8, []), (5, 8, [a])], view='Week')
    try:
        view_settings(page.locator('.planner-toolbar'), 'Properties').get_by_role('button', name='Hide Open places', exact=True).click()
        close_popover(page)
        grid = page.locator('#schedule-view .wk-grid')
        expect(grid.locator(f'.pchip.empty[data-shift="{s1}"]')).to_have_count(1)
        stream.progress = {'stage': 'placing', 'elapsed_seconds': 5}
        stream.event(adds=[(s1, a)])
        expect(grid.locator(f'.pchip[data-shift="{s1}"][data-person="{a}"]')).to_have_count(1)
        expect(grid.locator(f'.pchip.empty[data-shift="{s1}"]')).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_run_that_ends_after_another_rota_opens_leaves_that_rota_alone(browser, cp_server, h):
    """Rota A's run ends while its finishing read is delayed; B is opened (its own run
    shown live) before the read answers. A's late answer does not end B's run nor show A's result card or Apply."""
    wa, _ = rota(h, [(4, 8, []), (5, 8, [])], name='Live rota A')
    ja = h.command('coordinator', 'job.create', {'workspace_id': wa['id'], 'kind': 'generation'}, revision=wa['revision'])['job']['id']
    worker = threading.Thread(target=process_one, args=(h.app.state.engine,))
    worker.start()
    worker.join(60)  # A has a real result; the page is shown it running until the test says it ended
    wb, _ = rota(h, [(6, 8, [])], name='Live rota B')
    h.command('coordinator', 'job.create', {'workspace_id': wb['id'], 'kind': 'generation'}, revision=wb['revision'])
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    state = {'a_running': True, 'hold': False, 'held': []}

    def answer(route):
        if state['hold'] and route.request.url.endswith('/api/jobs/' + ja):
            state['held'].append(route)  # A's finishing read, answered after B is shown
            return
        response = route_fetch(route)
        body = response.json()
        for job in body['jobs'] if 'jobs' in body else [body['job']] if body.get('job') else []:
            if job.get('kind') == 'generation' and (job['id'] != ja or state['a_running']):
                job.update(status='running', result=None, started_at=clock_now().isoformat(), search_control={'minutes': 2},
                           progress={'stage': 'preparing', 'elapsed_seconds': 1, 'events': [], 'improvements_found': 0})
        if not state['a_running'] and route.request.url.endswith('/api/jobs/' + ja):
            state['hold'] = True  # the read that sees A ended passes; the one after it (the finish) waits
        route.fulfill(response=response, json=body)
    for w in (wa, wb):
        page.route(cp_server + '/api/jobs?workspace_id=' + w['id'], answer)
    page.route(cp_server + '/api/jobs/*', answer)
    page.route(re.compile(re.escape(cp_server) + r'/api/jobs/[^/?]+/status(\?.*)?$'), answer)  # the live status read (maintenance)
    try:
        open_rota(page, wa['id'])
        pick_view(page, 'People')
        auto = page.get_by_role('button', name='Auto-schedule', exact=True)
        expect(auto).to_have_attribute('aria-expanded', 'true')
        auto.click()
        expect(page.locator('.asl-strip ' + STEP)).to_have_text(re.compile(r'^2/5 '))
        state['a_running'] = False
        wait_held(page, state['held'])
        open_rota(page, wb['id'])
        expect(page.locator('#schedule-view')).to_have_attribute('data-view-key', re.compile('^' + re.escape(wb['id']) + r'\|'))
        expect(page.locator('.asl-strip ' + STEP)).to_have_text(re.compile(r'^2/5 '))
        with page.expect_response(lambda r: r.url.endswith('/api/jobs/' + ja)):
            for route in state['held']:
                route.continue_()
        page.wait_for_event('requestfinished', timeout=60_000)  # a later read: A's late answer has been handled
        expect(page.get_by_role('region', name='Auto-schedule result')).to_have_count(0)
        expect(page.locator('.asl-strip ' + STEP)).to_have_text(re.compile(r'^2/5 '))
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_the_live_view_is_not_loaded_without_a_run(browser, live_server, h):
    w, _ = rota(h, [(4, 8, [h.seed['people'][0]])])
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    urls = []
    page.on('request', lambda request: urls.append(request.url))
    try:
        page.reload()
        open_rota(page, w['id'])
        expect(page.locator('#schedule-view .pg-chip, #schedule-view .pchip').first).to_be_visible()
        assert urls and not [u for u in urls if re.search(r'/autoschedule_(live|motion)\.js$', u)]
        expect(page.locator('.asl-strip')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_admin_sets_the_animation_with_preview_validation_guard_and_save(browser, live_server, h, scheme):
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 900})
    page.emulate_media(color_scheme=scheme)
    page.add_init_script(WATCH)
    try:
        page.reload()
        open_config(page, 'Auto-schedule & scoring')
        pane = page.locator('.config-pane')
        pane.get_by_role('tab', name='Animation', exact=True).click()
        style = pane.get_by_label('Style', exact=True)
        expect(style).to_have_value('hybrid')
        expect(pane.get_by_label('Rows for a big move', exact=True)).to_have_value('4')
        expect(pane.get_by_label('Updates per second', exact=True)).to_have_value('3')
        expect(pane.get_by_label('Footer step change', exact=True)).to_have_value('roll')
        preview = pane.get_by_role('region', name='Preview')
        expect(preview.locator('.people-grid .pg-chip').first).to_be_visible()
        assert not pane.get_by_role('tabpanel', name='Animation').locator('.hint, small, p').count()  # labels only, no helper text
        # Fly: the preview plays it before anything is saved.
        style.select_option('fly')
        expect(pane.get_by_label('Rows for a big move', exact=True)).to_be_disabled()  # Big moves apply to Hybrid only
        page.wait_for_function("() => window.__flew > 0")
        shot(page, f'config-animation-{scheme}')
        # Validation from the contract's limits, shown at Save.
        rate = pane.get_by_label('Updates per second', exact=True)
        rate.fill('9')
        bar = page.get_by_role('region', name='Unsaved changes')
        bar.get_by_role('button', name='Save defaults', exact=True).click()
        expect(rate).to_have_attribute('aria-invalid', 'true')
        expect(page.get_by_role('dialog', name='Save auto-schedule defaults')).to_have_count(0)
        rate.fill('5')
        # The dirty guard.
        page.get_by_role('button', name='Home', exact=True).click()
        leave = page.get_by_role('dialog', name='Leave this page?')
        leave.get_by_role('button', name='Keep editing', exact=True).click()
        expect(rate).to_have_value('5')
        bar.get_by_role('button', name='Save defaults', exact=True).click()
        dialog = page.get_by_role('dialog', name='Save auto-schedule defaults', exact=True)
        dialog.get_by_label('Reason', exact=True).fill('Show the auto-schedule as flights')
        dialog.get_by_role('button', name='Save defaults', exact=True).click()
        expect(bar).to_be_hidden()
        saved = h.get('admin', '/api/me')['department']['autoschedule_animation']
        assert saved['mode'] == 'fly' and saved['max_updates_per_second'] == 5
        page.reload()
        open_config(page, 'Auto-schedule & scoring')
        pane.get_by_role('tab', name='Animation', exact=True).click()
        expect(pane.get_by_label('Style', exact=True)).to_have_value('fly')
        expect(pane.get_by_label('Updates per second', exact=True)).to_have_value('5')
        # Undo puts the previous settings back (audited like every department setting).
        style.select_option('roll')
        bar.get_by_role('button', name='Save defaults', exact=True).click()
        dialog.get_by_label('Reason', exact=True).fill('Try rolling changes')
        dialog.get_by_role('button', name='Save defaults', exact=True).click()
        expect(bar).to_be_hidden()
        assert h.get('admin', '/api/me')['department']['autoschedule_animation']['mode'] == 'roll'
        page.locator('.toast').get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Undone', exact=False).first).to_be_visible()
        assert h.get('admin', '/api/me')['department']['autoschedule_animation']['mode'] == 'fly'
    finally:
        context.close()


def test_a_phone_keeps_its_footer_during_a_run(browser, cp_server, h):
    a = h.seed['people'][0]
    w, _ = rota(h, [(4, 8, [a]), (4, 13, [])])
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 390, 'height': 844})
    stream = Stream(page, cp_server, w['id'])
    try:
        stream.progress = {'stage': 'improving', 'elapsed_seconds': 9, 'search_started_seconds': 3, 'time_limit_seconds': 120}
        open_rota(page, w['id'])
        expect(page.locator('#auto-panel').get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
        expect(page.locator('.asl-strip')).to_have_count(0)  # the sheet shows the run; the footer keeps the rota's health
        one_line(page, 'phone run')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        context.close()

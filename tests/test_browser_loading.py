"""Loading states (loading.ts; DESIGN.md › Loading): the timing rules on a paused clock, sign-in success / refusal /
slow start, page skeletons giving way to content, busy buttons that cannot be pressed twice, reduced motion, and the
same on a phone in dark mode. Also: optimistic batches that save, fail, revert and retry, and long-job progress."""
import json
import re
import pytest
from playwright.sync_api import expect
from .conftest import TEST_PASSWORD
from .test_browser import browser, live_server, sign_in, new_context, open_app, navigate, open_rota  # noqa: F401
from .test_browser_operator_footer import wait_for_held_route

pytestmark = pytest.mark.browser
MODES = [(1440, 'light'), (390, 'dark')]


def paused(page):
    """Stop the page clock where it is: timers then run only through run_for."""
    # Python numeric instants are seconds, while Date.now() is milliseconds.
    page.clock.pause_at(page.evaluate('new Date(Date.now() + 1000).toISOString()'))
    page.clock.run_for(1000)


def test_timing_rules_on_a_paused_clock(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        # Home's cards read in the background; start from an idle page so the top bar below is this test's alone.
        expect(page.locator('#main .loading')).to_have_count(0)
        expect(page.locator('.top-progress')).not_to_have_class(re.compile(r'\bon\b'))
        paused(page)
        page.evaluate("""async () => {const L=await window.importBundle('loading.js');window.L=L;window.log=[];window.ends={};
          window.quick=L.delayed(()=>log.push('quick show'),()=>log.push('quick hide'));
          window.slow=L.delayed(()=>log.push('slow show'),()=>log.push('slow hide'));}""")
        # Under 300 ms nothing is shown at all.
        page.clock.run_for(200)
        page.evaluate("void quick.end().then(v=>ends.quick=v)")
        page.clock.run_for(400)
        assert page.evaluate('ends.quick') is False
        assert page.evaluate('log') == ['slow show']
        # Shown at 300 ms and ended at 600 ms: it stays until 700 ms (400 ms minimum), then gives way.
        page.evaluate("void slow.end().then(v=>ends.slow=v)")
        page.clock.run_for(99)
        assert page.evaluate("ends.slow") is None
        page.clock.run_for(1)
        assert page.evaluate("ends.slow") is True and page.evaluate('log') == ['slow show', 'slow hide']

        # A skeleton: invisible for 300 ms, nothing drawn over it, then after 15 s one line with its elapsed time and no
        # Retry while the read is live (DL-045); settle() waits out the minimum.
        page.evaluate("""() => {const sk=L.skeleton('table','Loading test…');sk.id='sk';document.querySelector('#main').prepend(sk);}""")
        sk = page.locator('#sk')
        expect(sk).to_have_class(re.compile(r'\bsk-wait\b'))
        expect(sk).to_have_attribute('aria-busy', 'true')
        page.clock.run_for(300)
        expect(sk).not_to_have_class(re.compile(r'\bsk-wait\b'))
        expect(page.locator('#loading-live')).to_have_text('Loading test…')
        page.clock.run_for(14699)
        expect(sk.locator('.sk-slow, .spinner')).to_have_count(0)  # the moving skeleton alone (owner, 8 Nov 2026)
        page.clock.run_for(1)
        expect(sk.locator('.sk-slow')).to_have_text('Still loading · 0:15')
        expect(page.locator('#loading-live')).to_have_text('Still loading')  # said once, never the ticking clock
        page.clock.run_for(3000)
        expect(sk.locator('.sk-slow')).to_have_text('Still loading · 0:18')
        expect(sk.get_by_role('button')).to_have_count(0)  # no Retry during a live read
        page.evaluate("void L.settle(document.querySelector('#sk')).then(v=>ends.sk=v)")
        page.clock.run_for(1)
        assert page.evaluate('ends.sk') is True
        expect(sk).not_to_have_attribute('aria-busy', 'true')

        # Busy labels and the top progress bar.
        assert page.evaluate("['Apply 3 fixes','Save','Sign in','Publish…'].map(L.busyLabel)") == ['Applying 3 fixes…', 'Saving…', 'Sign in', 'Publishing…']
        page.evaluate("() => {window.work=new Promise(r=>window.finish=r);L.track(work);}")
        bar = page.locator('.top-progress')
        page.clock.run_for(299)
        expect(bar).not_to_have_class(re.compile(r'\bon\b'))
        page.clock.run_for(1)
        expect(bar).to_have_class(re.compile(r'\bon\b'))
        page.evaluate('finish()')
        page.clock.run_for(399)
        expect(bar).to_have_class(re.compile(r'\bon\b'))
        page.clock.run_for(1)
        expect(bar).not_to_have_class(re.compile(r'\bon\b'))
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', MODES, ids=['desktop-light', 'phone-dark'])
def test_button_busy_and_no_double_press(browser, live_server, width, scheme):
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        page.evaluate("""async () => {const {button,withIcon}=await window.importBundle('dom.js');window.runs=0;
          const b=withIcon(button('Apply 3 fixes',()=>{runs++;return new Promise(r=>window.release=r);},'primary'),'check');
          b.id='apply';document.querySelector('#main').prepend(b);}""")
        apply = page.locator('#apply')
        apply.click()
        apply.dispatch_event('click')  # a second press while it works is ignored
        expect(apply).to_have_attribute('aria-busy', 'true')
        expect(apply).to_be_disabled()
        expect(apply).to_have_text('Applying 3 fixes…')
        expect(apply.locator('.spinner')).to_have_count(1)
        expect(apply.locator('svg')).to_have_count(0)
        assert page.evaluate('runs') == 1
        page.evaluate('release()')
        expect(apply).to_have_text('Apply 3 fixes')
        expect(apply.locator('svg')).to_have_count(1)
        expect(apply).to_be_enabled()
        expect(apply).not_to_have_attribute('aria-busy', 'true')
    finally:
        context.close()


def form(page):
    return page.get_by_label('Username or email', exact=True), page.get_by_label('Password', exact=True), page.get_by_role('button', name=re.compile('^Sign'))


@pytest.mark.parametrize('width,scheme', MODES, ids=['desktop-light', 'phone-dark'])
def test_sign_in_busy_then_frame_at_once_and_slow_start(browser, live_server, width, scheme):
    context = new_context(browser, {'width': width, 'height': 900})
    try:
        page = open_app(context, live_server)
        page.emulate_media(color_scheme=scheme)
        held, lists = [], []
        page.route('**/auth/login', lambda route: held.append(route))
        page.route('**/api/catalogues*', lambda route: lists.append(route))
        user, password, submit = form(page)
        user.fill('coordinator')
        page.get_by_role('button', name='Continue', exact=True).click()
        password.fill(TEST_PASSWORD)
        submit.click()
        wait_for_held_route(page, held)
        # Only the button changes: spinner and "Signing in…", fields locked, nothing pressed twice.
        expect(submit).to_have_text('Signing in…')
        expect(submit.locator('.spinner')).to_have_count(1)
        expect(user).to_have_attribute('readonly', '')
        expect(password).to_have_attribute('readonly', '')
        password.press('Enter')  # Enter while signing in sends nothing
        held.pop().continue_()
        # The app frame appears at once (name in the account button), the page as a skeleton under the top bar.
        expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible()
        wait_for_held_route(page, lists)
        skeleton = page.locator('#main .loading.page-sk')
        expect(skeleton).to_be_visible()
        expect(skeleton).to_have_attribute('aria-busy', 'true')
        expect(page.locator('.login-layout')).to_have_count(0)
        assert len(held) == 0  # Enter while signing in sent nothing
        page.clock.run_for(3000)
        expect(skeleton.locator('.sk-slow, .spinner')).to_have_count(0)  # nothing drawn over the skeleton
        page.clock.run_for(12000)
        expect(skeleton.locator('.sk-slow')).to_contain_text('Still loading · 0:1')  # its elapsed time
        expect(skeleton.get_by_role('button', name='Retry')).to_have_count(0)  # never Retry while the read is live
        lists.pop().continue_()
        expect(page.locator('#main[data-loaded-view=home]')).to_have_count(1)
        expect(page.locator('#main .loading.page-sk')).to_have_count(0)
        expect(page.locator('#main .fade-in').first).to_be_attached()
    finally:
        context.close()


def test_sign_in_refused_keeps_input_and_focuses_password(browser, live_server):
    context = new_context(browser)
    try:
        page = open_app(context, live_server)
        user, password, submit = form(page)
        user.fill('coordinator')
        page.get_by_role('button', name='Continue', exact=True).click()
        password.fill('not-the-password')
        submit.click()
        expect(page.locator('#login-error .notice')).to_be_visible()
        expect(submit).to_have_text('Sign in')
        expect(submit).to_be_enabled()
        expect(password).to_be_focused()
        expect(password).to_have_attribute('aria-invalid', 'true')
        expect(user).to_have_value('coordinator')
        expect(password).to_have_value('not-the-password')
        expect(user).not_to_have_attribute('readonly', '')
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', MODES, ids=['desktop-light', 'phone-dark'])
def test_page_skeleton_gives_way_to_content(browser, live_server, width, scheme):
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        held = []
        page.route('**/api/requests/all*', lambda route: held.append(route))
        page.evaluate("location.hash='requests'")
        skeleton = page.locator('#main .loading.page-sk.sk-table')
        expect(skeleton).to_be_visible()
        expect(skeleton).not_to_have_class(re.compile(r'\bsk-wait\b'))
        expect(page.locator('.topbar .top-progress.on')).to_have_count(1)
        expect(page.locator('#loading-live')).to_have_text('Loading Requests…')
        # Header stays usable while the page loads.
        expect(page.get_by_role('button', name='Inbox')).to_be_enabled()
        page.route('**/api/requests/all*', lambda route: route.continue_())
        for route in held:
            route.continue_()
        expect(page.locator('#main[data-loaded-view=requests]')).to_have_count(1)
        expect(page.locator('#main .loading.page-sk')).to_have_count(0)
        expect(page.locator('.topbar .top-progress.on')).to_have_count(0)
    finally:
        context.close()


def test_reduced_motion_keeps_states_without_movement(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.emulate_media(reduced_motion='reduce')
        page.evaluate("""async () => {const L=await window.importBundle('loading.js');const sk=L.skeleton('cards');sk.id='sk';document.querySelector('#main').prepend(sk);
          L.track(new Promise(()=>{}));}""")
        sk = page.locator('#sk')
        expect(sk).not_to_have_class(re.compile(r'\bsk-wait\b'))
        expect(page.locator('.top-progress.on')).to_have_count(1)
        assert sk.locator('.sk').first.evaluate('n => getComputedStyle(n).animationName') == 'none'
        assert page.locator('.top-progress').evaluate("n => getComputedStyle(n,'::after').animationName") == 'none'
        assert page.locator('.top-progress').evaluate("n => getComputedStyle(n,'::after').left") == '0px'
    finally:
        context.close()


def unassign(page):
    """Unassign from the selection bar; a narrow bar folds it into ⋯ More."""
    bar = page.get_by_role('toolbar', name='Bulk actions')
    expect(bar).to_be_visible()
    if bar.get_by_role('button', name='Unassign').is_visible():
        bar.get_by_role('button', name='Unassign').click()
    else:
        bar.get_by_role('button', name='More actions').click()
        page.get_by_role('menu', name='More actions').get_by_role('menuitem', name='Unassign').click()


@pytest.mark.parametrize('width,scheme', MODES, ids=['desktop-light', 'phone-dark'])
def test_optimistic_batch_saving_failure_revert_and_retry(browser, live_server, h, width, scheme):
    from .test_browser_resolve_issues import conflicted, issues_page, group
    w, _ = conflicted(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        held = []
        page.route('**/api/commands', lambda route: held.append(route))
        main.get_by_role('checkbox', name='Select all On leave or away', exact=True).check()
        unassign(page)
        wait_for_held_route(page, held)
        # At once: the rows read as saving (spinner, dotted underline), before the server answers.
        expect(main.locator('.save-mark.saving').first).to_be_visible()
        # A refusal puts each row back with "Couldn't save · Retry".
        held.pop().fulfill(status=503, content_type='application/json', body=json.dumps({'error': {'code': 'CATALOGUE_BUSY', 'message': 'Busy, try again'}}))
        expect(main.locator('.save-mark.saving')).to_have_count(0)
        failed = main.locator('.save-mark.failed').first
        expect(failed).to_contain_text("Couldn't save")
        expect(page.locator('.toast').filter(has_text='Busy, try again')).to_be_visible()
        expect(main.locator('.issues-counts')).to_contain_text(' · 0 resolved')
        # Retry sends it again and the rows confirm with a tick.
        page.unroute('**/api/commands')
        failed.get_by_role('button', name='Retry').click()
        expect(main.locator('.issues-counts')).not_to_contain_text(' · 0 resolved')
        expect(main.locator('.save-mark.failed')).to_have_count(0)
        if width >= 768:  # the table's Fix column confirms with a tick (phone cards say it in their line)
            expect(main.locator('.save-mark.saved').first).to_contain_text('Unassigned')
        expect(page.locator('.toast').filter(has_text='issues resolved').last.get_by_role('button', name='Undo')).to_be_visible()
    finally:
        context.close()


def test_conflict_reverts_without_retry(browser, live_server, h):
    from .test_browser_resolve_issues import conflicted, issues_page
    w, _ = conflicted(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        page.route('**/api/commands', lambda route: route.fulfill(status=409, content_type='application/json',
                   body=json.dumps({'error': {'code': 'STALE_REVISION', 'message': 'This record changed meanwhile.'}})))
        main.get_by_role('checkbox', name='Select all On leave or away', exact=True).check()
        unassign(page)
        failed = main.locator('.save-mark.failed').first
        expect(failed).to_have_text("Couldn't save")
        expect(failed.get_by_role('button', name='Retry')).to_have_count(0)
        expect(page.locator('.toast').filter(has_text='changed while you were working')).to_be_visible()
    finally:
        context.close()


def test_long_job_progress_reads_percent_steps_and_time_used(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.evaluate("""async () => {const {generationProgress}=await window.importBundle('generation_options.js');
          const n=generationProgress({status:'running',progress:{stage:'finding',time_limit_seconds:120,elapsed_seconds:70,search_started_seconds:10}},70);
          n.id='job';document.querySelector('#main').prepend(n);}""")
        job = page.locator('#job')
        # FD-04: one line (step n/5 in the contract's stage words, percent, time used, and the allowance left said as a
        # ceiling, "up to": never a forecast, A4) over one bar. Times in duration words (83ae3d14b, one unit everywhere).
        expect(job.locator('.auto-stage')).to_have_text('3/5 Searching · 53% · 1 min used · up to 1 min left')  # ME-02: the footer's search clock
        expect(job.get_by_role('progressbar')).to_have_attribute('value', '53')
        expect(job.locator('ol, .muted:not(.auto-time)')).to_have_count(0)
        expect(job).not_to_contain_text('you can keep working')
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', MODES, ids=['desktop-light', 'phone-dark'])
def test_schedule_forward_action_applies_in_place_with_two_requests(browser, live_server, h, width, scheme):
    """Remove a person from a session (the footer's blocking list): the command, then the draft read again — no session,
    lists in memory, rota list, published days or jobs; the grid is patched without a reload, scroll and selection kept."""
    from .test_leave_disclosure import rota
    from .test_browser_qa_planner_a2 import settled
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    scroll_states = []

    def record_scroll(stage):
        scroll_states.append(page.evaluate("""stage => ({stage,y:scrollY,
          active:{tag:document.activeElement?.tagName,name:document.activeElement?.getAttribute('aria-label')},
          layout:[...document.querySelectorAll('.topbar,.planner-bar,.app-footer,.planner-main')].map(n=>({
            class:n.className,top:n.getBoundingClientRect().top,height:n.getBoundingClientRect().height,fitted:n.dataset.fitted}))})""", stage))

    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'planner')
        open_rota(page, w['id']); settled(page)
        expect(page.locator('.topbar .planner-bar')).to_have_attribute('data-fitted', '1')
        page.evaluate("document.querySelector('.topbar').dataset.mark='kept';window.scrollTo(0,40)")
        scrolled = page.evaluate('scrollY')
        record_scroll('baseline')
        with page.expect_response(re.compile(r'/rule-map$')):  # read on the first hover, not by the action
            page.locator('.planner-main').dispatch_event('pointerover')
        # From the keyboard: focusing a sticky footer control never scrolls the page (its scroll-margin, app.css).
        must = page.locator('.app-footer .fix-pills').get_by_role('button', name=re.compile(r'^\d+ must fix'))
        must.focus()
        assert page.evaluate('scrollY') == scrolled
        must.press('Enter')
        head = page.locator('.popover.fix-pop')
        expect(head).to_be_visible()  # the Fix list's head reads; it holds no buttons on its rows (D9)
        record_scroll('issues-open')
        page.keyboard.press('Escape')
        expect(head).to_have_count(0)
        if width < 768:
            return  # D10: planning edits stay on a desktop (the phone planner is an agenda without person chips)
        sent = []
        page.on('request', lambda r: sent.append(r.url.split('/api/')[1]) if '/api/' in r.url and not re.search(r'/api/(pulse|events)', r.url) else None)
        # The forward action, in place: a person taken off a session from the grid.
        page.locator('.planner-main .pchip').first.click(button='right')
        page.get_by_role('menuitem', name=re.compile('^Remove from this session')).click()
        record_scroll('issues-closed')
        expect(page.locator('.toast').last.get_by_role('button', name='Undo')).to_be_visible()
        expect(page.locator('.planner-main .pchip.is-saving')).to_have_count(0)
        expect(page.locator('#main[data-loaded-view=schedule] .planner-main')).to_be_visible()
        # The edit, then the draft: nothing else (a hover over the redrawn grid may read the new rule map, as any first hover).
        assert [u for u in sent if not u.endswith('/rule-map')] == ['commands', 'workspaces/' + w['id']], sent
        assert sent.count(f"workspaces/{w['id']}/rule-map") <= 1, sent
        assert page.evaluate("document.querySelector('.topbar')?.dataset.mark") == 'kept', 'redrawn in place, not a page reload'
        record_scroll('grid-ready')
        # The grid mounts before ResizeObserver fits the replacement header; scroll anchoring follows that layout.
        expect(page.locator('.topbar .planner-bar')).to_have_attribute('data-fitted', '1')
        try:
            page.wait_for_function('expected => scrollY === expected', arg=scrolled)
        finally:
            record_scroll('after-fit')
            print('Schedule scroll checkpoints: ' + json.dumps(scroll_states))
        assert page.evaluate('scrollY') == scrolled
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', MODES, ids=['desktop-light', 'phone-dark'])
def test_enter_submits_each_sign_in_step(browser, live_server, width, scheme):
    """QA-076: each step is a native form, so Enter in the field sends it like the button does. The username step sends
    /auth/identify, the password step /auth/login. (A CDP key event without text "\r" never submits a form; Playwright's
    press sends a real Enter.)"""
    context = new_context(browser, {'width': width, 'height': 900})
    try:
        page = open_app(context, live_server)
        page.emulate_media(color_scheme=scheme)
        user, password, _ = form(page)
        user.fill('coordinator')
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/auth/identify')):
            user.press('Enter')
        expect(password).to_be_visible()
        expect(password).to_be_focused()  # the password step takes focus, the username stays in the form (hidden)
        expect(page.locator('input[autocomplete=username]')).to_have_value('coordinator')
        password.fill(TEST_PASSWORD)
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/auth/login')):
            password.press('Enter')
        expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible()
    finally:
        context.close()

"""maintenance / maintenance: an Assign from the Fix list, then the toast's Undo, with real clicks on real findings.

maintenance: the toast's Undo is the same undo as the top bar's "Undo last edit". The rota checks itself after every edit,
which moves its revision but not its content, so the toast's Undo still undoes that edit (it once refused as stale,
sending nothing). maintenance: the footer's must fix, its Rota health popover and the Fix list panel read one source (the
edit's fix_list_delta, then the next full read): equal after the Assign, with the ✓ chip naming who; and after the
Undo (its own delta) all three are back to where they were and the ✓ chip is gone."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in  # noqa: F401
from .test_browser_fix_list_panel import DESKTOP, real_gaps
from .test_browser import FIX_PILL, fix_pill

pytestmark = pytest.mark.browser

OPEN_ALL = re.compile(r'^Open all in Fix list \(\d+\)$')
CHECK_MS = 60000  # the rota's own check after an edit (a worker job) lands within this


def settled(page):
    """The rota's own check after the last change has landed (the footer no longer says it is checking)."""
    # DL-018: the two words of a check in hand (queued, running)
    expect(page.locator('.app-footer')).not_to_contain_text(re.compile('Waiting to check|Checking the rota'), timeout=CHECK_MS)


def must_counts(page):
    """Must fix as the footer, its Rota health popover and the Fix list panel each say it."""
    page.wait_for_function("""selector=>{const n=document.querySelector(selector+' .num');
        const rows=document.querySelectorAll('.dock-panel.fix-side section[data-level=must] li[data-cause]');
        return !!n && Number(n.textContent) === rows.length;}""", arg=FIX_PILL+'.err', timeout=CHECK_MS)
    footer = int(fix_pill(page, 'err').locator('.num').inner_text())
    panel = page.locator('.dock-panel.fix-side section[data-level=must] li[data-cause]').count()
    fix_pill(page, 'err').click()
    pop = page.get_by_role('dialog', name='Rota health')
    expect(pop.locator('.iss-row.error')).to_have_count(footer)
    popover = pop.locator('.iss-row.error').count()
    page.keyboard.press('Escape')
    expect(pop).to_have_count(0)
    return footer, popover, panel


def open_panel_at(page, w, gap):
    from .test_browser_team_box import open_view
    open_view(page, w, 'Week')
    fix_pill(page, 'err').click()
    page.locator('.popover.fix-pop').get_by_role('button', name=OPEN_ALL).click()
    row = page.locator(f'.dock-panel.fix-side li[data-cause="{gap["id"]}"]')
    expect(row).to_be_visible()
    return row


def assign_first(row):
    row.locator('.fix-chip', has_text=re.compile(r'^\w{3} 8')).click()
    row.locator('.fix-fill-box').get_by_role('button', name=re.compile(r'^Assign ')).first.click()
    done = row.locator('.fix-chip.done')
    expect(done).to_have_count(1)
    expect(done).to_have_accessible_name(re.compile(r'fixed, .+ assigned$'))
    return done


def people_on(h, w, shift):
    return len(next(s for s in h.detail(w)['shifts'] if s['id'] == shift)['participants'])


def test_after_an_assign_the_footer_popover_and_panel_say_one_count(browser, live_server, h):
    w, gaps, gap = real_gaps(h, 'one count')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        row = open_panel_at(page, w, gap)
        settled(page)
        before = must_counts(page)
        assert before[0] == before[1] == before[2], before
        done = assign_first(row)
        now = must_counts(page)  # the edit's own fix_list_delta, before its check lands
        assert now[0] == now[1] == now[2], now
        settled(page)
        later = must_counts(page)  # then the full read
        assert later[0] == later[1] == later[2], later
        expect(done).to_have_count(1)
    finally:
        context.close()


def test_the_toasts_undo_undoes_an_assign_and_every_count_comes_back(browser, live_server, h):
    w, gaps, gap = real_gaps(h, 'toast undo')
    before_people = people_on(h, w, gaps[0])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        row = open_panel_at(page, w, gap)
        settled(page)
        before = must_counts(page)
        assign_first(row)
        toast = page.locator('#toast-tray .toast').filter(has=page.get_by_role('button', name='Undo', exact=True)).first
        expect(toast).to_be_visible()
        toast.hover()  # WCAG 2.2.1: the message waits while the pointer is on it, through the rota's own check
        settled(page)  # the check moved the rota's revision: the toast's Undo must still be this edit's (maintenance)
        assert people_on(h, w, gaps[0]) == before_people + 1
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('#toast-tray .toast').filter(has_text=re.compile(r'^Undone'))).to_be_visible(timeout=CHECK_MS)
        assert people_on(h, w, gaps[0]) == before_people  # the server read: the assignment is gone
        # The Undo's own delta: the session is open in its cause again, so no ✓ chip outlives it (maintenance).
        expect(row.locator('.fix-chip.done')).to_have_count(0)
        settled(page)
        assert must_counts(page) == before
        # Redo (the top bar): the same edit again, its own delta: ✓ back, one count on all three surfaces.
        page.locator('.planner-bar').get_by_role('button', name='Redo', exact=True).click()
        expect(row.locator('.fix-chip.done')).to_have_count(1)
        assert people_on(h, w, gaps[0]) == before_people + 1
        settled(page)
        now = must_counts(page)
        assert now[0] == now[1] == now[2], now
    finally:
        context.close()


def test_opening_the_fix_list_asks_for_no_candidates_until_a_session_is_chosen(browser, live_server, h):
    """LEAD-UIUX (live capture, 7 Nov 2026: 58 s unasked): fill-options runs only when someone asks for candidates (a
    session's chip, the row's Assign, a marker's Fix, the move on after their Assign), never when the Fix list opens
    by itself (the panel, an issue list's Open all)."""
    w, gaps, gap = real_gaps(h, 'ask first')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    asked = []  # the session each fill-options asks about
    page.on('request', lambda r: asked.append((r.post_data_json or {}).get('shift_id')) if r.method == 'POST' and r.url.endswith('/api/planner/fill-options') else None)
    try:
        row = open_panel_at(page, w, gap)
        settled(page)
        # Whichever cause the panel put in focus, its box offers candidates instead of asking for them.
        expect(page.locator('.dock-panel.fix-side .fix-fill-box').get_by_role('button', name='Show who can work it', exact=True).first).to_be_visible()
        assert asked == []  # the panel opened and focused a session by itself: nothing asked
        row.locator('.fix-chip', has_text=re.compile(r'^\w{3} 8')).click()  # the person chooses this session
        assign = row.locator('.fix-fill-box').get_by_role('button', name=re.compile(r'^Assign '))
        expect(assign.first).to_be_visible()
        assert asked == [gaps[0]], asked
        # An Assign that moves on to the cause's next session: they keep fixing, so its candidates load at once (one ask).
        assign.first.click()
        expect(row.locator('.fix-chip.done')).to_have_count(1)
        expect(row.locator('.fix-fill-box').get_by_role('button', name=re.compile(r'^Assign ')).first).to_be_visible()
        assert asked.count(gaps[1]) >= 1 and asked[:2] == [gaps[0], gaps[1]], asked
    finally:
        context.close()


def test_choosing_a_session_finds_its_candidates_as_a_job(browser, live_server, h):
    """PERF async fill-options (46f346af7): where workers serve jobs, choosing a session queues who-can-work-it as a job
    (never the synchronous call): the box says "Finding who can work it" with its elapsed time, then the candidates land
    in the same box once the job runs (here the test runs it, as the worker would)."""
    from apps.worker.main import process_one
    w, gaps, gap = real_gaps(h, 'async candidates')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    asked = []
    page.on('request', lambda r: asked.append(r.url) if r.method == 'POST' and '/api/planner/fill-options' in r.url else None)
    page.route('**/health/service', lambda r: r.fulfill(status=200, json={'status': 'ok'}))  # workers serve jobs
    try:
        row = open_panel_at(page, w, gap)
        with page.expect_response(lambda r: '/api/planner/fill-options' in r.url) as queued:
            row.locator('.fix-chip', has_text=re.compile(r'^\w{3} 8')).click()
        assert queued.value.status == 202, queued.value.status  # queued as a job
        box = row.locator('.fix-fill-box')
        expect(box).to_contain_text('Finding who can work it')
        assert asked and all('async_options=true' in u for u in asked), asked  # a job, never the synchronous call
        job = queued.value.json()['job']
        status_path = '**/api/jobs/' + job['id'] + '/status'
        page.route(status_path, lambda route: route.fulfill(json={'job': {
            **job, 'status': 'running', 'progress': {'result': {'provisional_page': True, 'candidates': []}}}}))
        expect(box).to_contain_text('Still checking others')
        expect(box).not_to_contain_text('Nobody passes every rule')
        page.unroute(status_path)
        for _ in range(6):  # the queued jobs (its candidates; a check, if one was queued), bounded
            if not process_one(h.app.state.engine):
                break
        # Woken by the live "jobs" change, else the status read (backing off to 10 s).
        expect(box.get_by_role('button', name=re.compile(r'^Assign ')).first).to_be_visible(timeout=CHECK_MS)
        expect(box).not_to_contain_text('Finding who can work it')
        assert all('async_options=true' in u for u in asked), asked
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_the_session_panel_finds_its_candidates_as_the_same_job(browser, live_server, h):
    """One way to ask who can work a session (candidates.ts fillOptions): the session panel's list, opened from an open
    place on the grid, is the same job (202, never the synchronous call), says "Finding who can work it", and lists the
    candidates in the panel once the job runs."""
    from apps.worker.main import process_one
    from .test_browser_team_box import open_view
    w, gaps, gap = real_gaps(h, 'panel candidates')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    asked = []
    page.on('request', lambda r: asked.append(r.url) if r.method == 'POST' and '/api/planner/fill-options' in r.url else None)
    page.route('**/health/service', lambda r: r.fulfill(status=200, json={'status': 'ok'}))  # workers serve jobs
    try:
        open_view(page, w, 'Week')
        with page.expect_response(lambda r: '/api/planner/fill-options' in r.url) as queued:
            page.locator(f"button.pchip.gap[data-shift='{gaps[0]}']").first.click()
        assert queued.value.status == 202, queued.value.status
        panel = page.locator('.wk-panel')
        expect(panel).to_contain_text('Finding who can work it')
        for _ in range(6):
            if not process_one(h.app.state.engine):
                break
        expect(panel.locator('.wk-cands').get_by_role('button', name=re.compile(r'^Assign ')).first).to_be_visible(timeout=CHECK_MS)
        assert asked and all('async_options=true' in u for u in asked), asked
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_provisional_candidates_show_before_the_final_page(browser, live_server, h):
    """PERF async fill-options (d4c1b4991 streams fully validated pages): a provisional page's people show at once,
    assignable, with "Still checking others…"; the final page then replaces them and the line goes. The job's status
    read is served here (a provisional page, then the final one) so the order is certain."""
    from .test_browser import open_rota
    from .test_browser_fix_list_panel import two_gaps, with_gaps
    from .test_browser_fix_list_qa3 import open_panel
    w, shifts = two_gaps(h, 'provisional first')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    person = lambda i: {'person_id': f'p{i}', 'display_name': f'Fictional Person {i}', 'status': 'ok', 'findings': [],
                        'reason_short': '12 hours this week', 'week_hours': 12, 'rota_hours': 12, 'rest_before_hours': 40,
                        'can_lead': False, 'fills': True}
    first = {'provisional_page': True, 'workspace_id': w['id'], 'workspace_revision': w['revision'], 'shift_id': shifts[0],
             'slot': {'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'participation': 'staff',
                      'overtime': False, 'segments': []},
             'source_fingerprint': 'f', 'evaluated_at': '2026-10-04T08:00:00+03:00', 'basis': '', 'candidates': [person(1)]}
    final = {**first, 'provisional_page': False, 'candidates': [person(1), person(2)]}
    reads = []

    def status(route):
        reads.append(1)
        job = ({'id': 'job-p', 'kind': 'fill_options', 'status': 'running', 'revision': 1, 'progress': {'result': first}}
               if len(reads) < 3 else {'id': 'job-p', 'kind': 'fill_options', 'status': 'succeeded', 'revision': 2, 'result': final})
        route.fulfill(json={'job': job})
    try:
        with_gaps(page, w, shifts)
        page.route('**/health/service', lambda r: r.fulfill(status=200, json={'status': 'ok'}))  # workers serve jobs
        page.route(re.compile(r'.*/api/planner/fill-options\?async_options=true$'), lambda r: r.fulfill(
            status=202, json={'job': {'id': 'job-p', 'kind': 'fill_options', 'status': 'queued', 'revision': 0},
                              'source_fingerprint': 'f', 'joined': False}))
        page.route(re.compile(r'.*/api/jobs/job-p/status.*'), status)
        open_rota(page, w['id'])
        panel = open_panel(page)
        panel.locator('li[data-cause=c1] .fix-chip').first.click()
        box = panel.locator('li[data-cause=c1] .fix-fill-box')
        expect(box).to_contain_text('Still checking others', timeout=CHECK_MS)  # the provisional page first
        expect(box.locator('.wk-cand')).to_have_count(1)
        expect(box.get_by_role('button', name='Assign Fictional Person 1')).to_be_visible()  # final for that person
        expect(box).not_to_contain_text('Still checking others', timeout=CHECK_MS)  # then the final page
        expect(box.locator('.wk-cand')).to_have_count(2)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

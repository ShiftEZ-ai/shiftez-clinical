"""Auto-schedule run flow reviewed: Run shows the run at once (panel and footer) before the server answers,
a second press never starts a second run, what the run's checks found shows while it goes (job.progress.precheck_issues), and
the footer's score follows the best schedule so far (job.progress.best_score.total). Progress is written into the real API's
answers by the live view's Stream (tests/test_browser_autoschedule_live.py)."""
import json

import pytest
from playwright.sync_api import expect

from .test_browser import auto_setup, browser, live_server, open_rota, open_scheduling, sign_in, wait_held  # noqa: F401
from .test_browser_cp_sat import cp_server  # noqa: F401
from .test_browser_autoschedule_live import STEP, rota, start_run

pytestmark = pytest.mark.browser


def creates(route_list):
    return [r for r in route_list if json.loads(r.request.post_data or '{}').get('kind') == 'job.create']


def test_run_shows_its_progress_at_once_and_a_second_press_starts_no_second_run(browser, cp_server, h):
    w, _ = rota(h, [(4, 8, []), (5, 8, [])], name='Instant auto-schedule')
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    held, posted, listed = [], [], []
    try:
        open_rota(page, w['id'])

        def hold_list(route):
            listed.append(route)  # the read the panel makes on opening: answered only after Run is pressed
        page.route(lambda url: '/api/jobs?workspace_id=' in url, hold_list, times=1)
        open_scheduling(page)
        wait_held(page, listed)

        def hold(route):
            posted.append(route)
            if creates([route]):
                held.append(route)  # the server is slow to answer: the run must show before it does
            else:
                route.continue_()
        page.route(cp_server + '/api/commands', hold)
        panel = page.locator('#auto-panel')
        run = panel.get_by_role('button', name='Run auto-schedule', exact=True)
        run.dblclick()
        wait_held(page, held)
        # The opening read answers (no runs yet) while job.create is still held: it must not replace the pending progress.
        with page.expect_response(lambda r: '/api/jobs?workspace_id=' in r.url):
            listed[0].continue_()
        # Within a second of the press, with the server still holding job.create: the footer's step and the panel's progress.
        expect(page.locator('.app-footer .footer-status > .asl-strip ' + STEP)).to_have_text('1/5 Checking rules', timeout=1000)
        expect(panel.get_by_text('Waiting for the scheduler to start', exact=True)).to_be_visible(timeout=1000)
        expect(run).to_have_count(0)
        # A jobs read that lands while job.create is held has no run in it yet: the run stays shown as starting, in the
        # panel and the footer, never "No auto-schedule runs" (issue 1793). Back on the tab, the open panel reads at once.
        with page.expect_response(lambda r: '/api/jobs?workspace_id=' in r.url) as listed:
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        # Not Response.finished(): this read is often a 304 answered from the cache, which never reports finishing.
        assert listed.value.status in (200, 304), listed.value.status
        page.evaluate("() => new Promise(r => requestAnimationFrame(() => setTimeout(r, 100)))")
        expect(panel.get_by_text('No auto-schedule runs for this rota yet.', exact=True)).to_have_count(0)
        expect(panel.get_by_text('Waiting for the scheduler to start', exact=True)).to_be_visible()
        expect(page.locator('.app-footer .footer-status > .asl-strip ' + STEP)).to_have_text('1/5 Checking rules')
        # Back on What to fill, the way on leads to the run going, not to a second one.
        auto_setup(panel)
        panel.get_by_role('button', name='Show run', exact=True).click()
        expect(panel.get_by_text('Waiting for the scheduler to start', exact=True)).to_be_visible()
        expect(panel.locator('.auto-stage')).to_be_focused()  # no Stop yet: focus lands on the progress line, not the page
        held[0].continue_()
        stop = panel.get_by_role('button', name='Cancel auto-schedule', exact=True).or_(panel.get_by_role('button', name='Stop and keep best', exact=True))
        expect(stop.first).to_be_visible()
        auto_setup(panel)
        panel.get_by_role('button', name='Show run', exact=True).click()
        expect(panel.locator('[data-run-stop]:not(:disabled)').first).to_be_focused()  # once the run exists, its Stop
        expect(page.locator('.app-footer .footer-status > .asl-strip')).to_have_count(1)  # one strip: the run's own took over
        assert len(creates(posted)) == 1, 'a second press must not start a second run'
        jobs = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']
        assert [j['kind'] for j in jobs] == ['generation']
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()
        for job in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']:
            if job['status'] in ('queued', 'running'):  # left for no later test's worker
                h.command('coordinator', 'job.cancel', {'reason': 'Browser test clean-up'}, job['id'], job['revision'])


def test_findings_and_the_best_score_show_while_the_run_goes(browser, cp_server, h):
    a, b = h.seed['people'][:2]
    context, page, stream, w, (s_first, s_second) = start_run(browser, cp_server, h, [(4, 8, [a]), (4, 9, [])])
    try:
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        # A4: the draft's footer holds its Fix list counts and the run's strip; the score is the draft's own (owner,
        # 7 Nov 2026: shown while anything must be fixed, capped), never the run's.
        footer = page.locator('.app-footer')
        expect(footer.get_by_role('group', name='Rota health')).to_be_visible()
        expect(footer.locator('.score-pill .score-blocking')).to_have_text('· capped')

        # 1 · Preparing, with what the checks found already (before any search): shown at once, by rule.
        # The contract's shape (AutoschedulePrecheckIssue): classification, code, count, up to three samples; no message.
        stream.progress = {'stage': 'preparing', 'elapsed_seconds': 1, 'precheck_issues': [
            {'classification': 'protected', 'code': 'QUOTA_ABOVE_MAXIMUM', 'count': 6, 'samples': [{'person_id': a}]},
            {'classification': 'blocking', 'code': 'TIME_OVERLAP', 'count': 2, 'samples': [{'shift_id': s_first, 'person_id': a}]}]}
        stream.wait_reads(page)
        found = panel.get_by_role('region', name='Found so far')
        expect(found).to_be_visible()
        expect(found.locator('.needs-head .wk-count')).to_have_text('8')
        expect(found.get_by_role('list').first).to_have_attribute('aria-label', 'Must fix')  # blocking first, whatever order the worker sent
        blocking = found.get_by_role('list', name='Must fix')
        # The code in the contract's plain words (FINDING_MESSAGES); a code it has no words for keeps its readable name.
        from packages.contracts.schemas import FINDING_MESSAGES
        # One item of the one issue list (d413c4189, UIUX-AUDIT C1): the words with the count, the samples as its actions.
        expect(blocking.locator('.issue-card .issue-text strong')).to_have_text(FINDING_MESSAGES['TIME_OVERLAP'] + ' · 2')
        # Each sample is its own action, never a run of " · " separators ending in one.
        expect(blocking.locator('.issue-actions > *')).to_have_count(1)
        assert not blocking.locator('.issue-actions > *').first.inner_text().rstrip().endswith('·')
        expect(found.get_by_role('list', name='Kept as they are')).to_contain_text('Quota above maximum')
        expect(blocking.locator('.link-button')).to_have_count(1)  # the sample session, shown on the schedule when chosen
        expect(page.locator('.asl-strip ' + STEP)).to_have_text('2/5 Preparing')

        # 2 · Searching with a better schedule: the step moves on; the run's best score is not shown as a standalone
        # score (A7: none while anything must be fixed, and the result says the run's change once it ends).
        stream.progress = {**stream.progress, 'stage': 'improving', 'elapsed_seconds': 8, 'search_started_seconds': 4, 'best_score': {'total': 87.25, 'components': {'coverage': 40, 'rules': 20, 'fairness': 15, 'preferences': 10, 'stability': 2.25}}}
        stream.event(adds=[(s_second, b)])
        expect(page.locator('.asl-strip ' + STEP)).to_have_text('3/5 Searching')
        expect(footer.locator('.score-pill')).to_have_count(1)  # the draft's own
        expect(footer).not_to_contain_text('87.3')
        stream.progress = {**stream.progress, 'best_score': {'total': 91, 'components': {}}}
        stream.wait_reads(page)
        expect(found).to_be_visible()  # still there: issues stay while the run goes

        # 3 · The run ends: its strip leaves the footer; the draft's own counts stay.
        stream.on = False
        job = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]
        h.command('coordinator', 'job.cancel', {'reason': 'Browser test: run ends'}, job['id'], job['revision'])
        expect(footer.locator('.asl-strip')).to_have_count(0, timeout=15000)
        expect(footer.get_by_role('group', name='Rota health')).to_be_visible()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()
        for job in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']:
            if job['status'] in ('queued', 'running'):
                h.command('coordinator', 'job.cancel', {'reason': 'Browser test clean-up'}, job['id'], job['revision'])


def test_an_older_server_without_stages_or_findings_still_shows_the_run(browser, cp_server, h):
    """No progress at all (a server before stages and findings): the run still shows at once, step 1."""
    context, page, stream, w, _ = start_run(browser, cp_server, h, [(4, 8, [])])
    try:
        stream.progress = {}
        stream.wait_reads(page)
        expect(page.locator('.asl-strip ' + STEP)).to_have_text('1/5 Checking rules')
        open_scheduling(page)
        expect(page.locator('#auto-panel').get_by_role('region', name='Found so far')).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()
        for job in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']:
            if job['status'] in ('queued', 'running'):
                h.command('coordinator', 'job.cancel', {'reason': 'Browser test clean-up'}, job['id'], job['revision'])


def test_focus_stays_on_its_control_while_the_run_redraws_and_lands_on_the_result_when_it_ends(browser, cp_server, h):
    """Every read redraws the run's panel: a focused control keeps focus (WCAG 2.4.3); when the run ends, the result has it."""
    context, page, stream, w, _ = start_run(browser, cp_server, h, [(4, 8, [])])
    try:
        stream.progress = {'stage': 'improving', 'elapsed_seconds': 8, 'search_started_seconds': 4}
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        expect(panel.locator('.auto-stage')).to_be_visible()
        expect(panel.get_by_role('button', name='Pause animation', exact=True)).to_have_count(0)
        # Animation is a personal account choice; the run retains its time and stop controls.
        for control in (panel.get_by_role('button', name='Add 5 min', exact=True),
                        panel.locator('[data-run-stop]:not(:disabled)').first):
            control.focus()
            panel.locator('.auto-stage').evaluate("n => { n.dataset.drawn = 'before'; }")
            stream.wait_reads(page)  # the stub answers each read with the run still going
            expect(panel.locator('.auto-stage:not([data-drawn])')).to_have_count(1)  # redrawn
            expect(control).to_be_focused()
        # The run ends while focus is in it: focus lands on the result, not the page.
        stream.on = False
        job = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]
        h.command('coordinator', 'job.cancel', {'reason': 'Browser test: run ends'}, job['id'], job['revision'])
        expect(panel.locator('.auto-stage')).to_have_count(0, timeout=15000)
        expect(panel.locator(':focus')).to_have_count(1)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()
        for job in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']:
            if job['status'] in ('queued', 'running'):
                h.command('coordinator', 'job.cancel', {'reason': 'Browser test clean-up'}, job['id'], job['revision'])

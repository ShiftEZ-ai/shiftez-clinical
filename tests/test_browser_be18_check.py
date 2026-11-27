"""Resume the real complete check queued by an assignment edit, through the existing UI."""
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one
from .test_browser import browser, holding, live_server, sign_in, navigate, wait_held
from .test_be18_command_latency import edit, worker_up
from .browser_acceptance_support import shot

pytestmark = pytest.mark.browser
COMMANDS = re.compile(r'.*/api/commands$')
# The take-over's audited reason is the client's own (one place), read here and never copied.
RULE = (Path(__file__).parents[1] / 'apps/web/src/rule_check_api.ts').read_text(encoding='utf-8')
TAKEN_OVER = re.search(r"CHECK_TAKEN_OVER='([^']+)'", RULE).group(1)
CHECK_WORDS = re.compile('Waiting to check|Checking the rota')


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_implicit_complete_check_is_visible_and_worker_completion_refreshes_the_page(h, browser, live_server, theme):
    w = h.shift(h.workspace())['workspace']
    shift = h.detail(w)['shifts'][0]
    worker_up(h)
    w = edit(h, w, shift, [])['workspace']
    job = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.emulate_media(color_scheme=theme)
        navigate(page, 'schedule/week/2026-10-04?edit=' + w['id'])
        # D76: the check is the footer's working item with its Cancel; it never opens the Auto-schedule panel.
        # DL D-063, kept by DL-018 (Design Lead) + Master 10 Nov: one wording rule; a queued check says Waiting to check
        progress = page.locator('.app-footer').locator('[data-check-line]').filter(has_text=re.compile('Waiting to check|Checking the rota'))
        expect(progress).to_contain_text('Waiting to check')
        expect(page.get_by_role('button', name='Cancel: waiting to check', exact=True)).to_be_visible()
        expect(page.locator('#auto-panel')).to_be_hidden()
        jobs = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']
        assert [row['id'] for row in jobs] == [job['id']]
        shot(page, 'be18-' + theme + '-checking', full_page=False)
        assert process_one(h.app.state.engine)
        expect(progress).to_have_count(0)
        with transaction(h.app.state.engine) as db:
            assert not db.get(m.Workspace, w['id']).validation.get('scope')
            assert db.get(m.Job, job['id']).status == 'succeeded'
        shot(page, 'be18-' + theme + '-complete', full_page=False)
    finally:
        context.close()


def unserved_check(h):
    """A complete check an edit queued (a worker heartbeat is recent) that no worker ever starts."""
    w = h.shift(h.workspace())['workspace']
    worker_up(h)
    w = edit(h, w, h.detail(w)['shifts'][0], [])['workspace']
    return w, h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]


def bound_ms(h, w):
    """maintenance: the bound is the server's heartbeat window on the rota's summary (a live worker: wait for it), never a copy."""
    summary = h.get('coordinator', '/api/workspaces/' + w['id'] + '/summary')
    assert summary['worker_liveness'] == 'running'
    return summary['worker_heartbeat_max_age_seconds'] * 1000


def watch_takeover(page, validate):
    """The commands a take-over sends, in order; `validate(route)` answers its synchronous check."""
    sent = []

    def commands(route):
        body = route.request.post_data_json or {}
        if body.get('kind') not in ('job.cancel', 'workspace.validate'):
            return route.continue_()
        sent.append(body)
        return validate(route) if body['kind'] == 'workspace.validate' else route.continue_()
    page.route(COMMANDS, commands)
    return sent


def test_a_queued_check_nobody_starts_is_taken_over_by_the_page_once(h, browser, live_server):
    """DL-018 + Master 10 Nov 2026, the one automatic fallback: at the bound the page stops the queued job as Cancel does
    (audited, with the reason Operations counts), only then checks the rota itself, and never tries a second time."""
    w, job = unserved_check(h)
    BOUND = bound_ms(h, w)
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    try:
        page.clock.install()
        sent = watch_takeover(page, held.append)  # the page's own check is held: its words can be read while it runs
        with holding(page) as reads:
            summary_url = live_server + '/api/workspaces/' + w['id'] + '/summary'
            reads.hold(summary_url)
            navigate(page, 'schedule/week/2026-10-04?edit=' + w['id'])
            footer = page.locator('.app-footer')
            progress = footer.locator('[data-check-line]').filter(has_text=CHECK_WORDS)
            expect(progress).to_contain_text('Waiting to check')  # DL-018: queued, unchanged
            wait_held(page, reads[summary_url])
            remaining = min(10_000, BOUND // 2)
            # Change the job's age without firing timers while the summary is held.
            page.clock.set_system_time(datetime.fromisoformat(job['created_at']) + timedelta(milliseconds=BOUND - remaining))
            with page.expect_response(lambda response: response.url == summary_url) as summary:
                reads.release(summary_url)
            assert summary.value.ok
            summary.value.finished()
            expect(progress).to_contain_text('Waiting to check')
            assert not sent
        page.clock.run_for(remaining + 1_000)
        wait_held(page, held)
        expect(progress).to_contain_text('Checking the rota')  # DL-018: without a click
        expect(footer.get_by_role('button', name=re.compile('^Cancel: '))).to_have_count(0)  # the server cannot stop this one
        # Stopped first, then checked: one job.cancel, one synchronous check, no two jobs.
        assert [c['kind'] for c in sent] == ['job.cancel', 'workspace.validate']
        assert sent[0]['target_id'] == job['id'] and sent[0]['payload'] == {'reason': TAKEN_OVER, 'only_if_queued': True}
        assert sent[1]['target_id'] == w['id'] and sent[1]['payload']['automatic'] is True
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Job, job['id']).status == 'cancelled'
            # Recorded where Operations can count it: the cancel's audit entry carries the fixed reason.
            assert db.query(m.AuditEvent).filter_by(target_id=job['id'], reason=TAKEN_OVER).count() == 1
        held[0].continue_()
        expect(progress).to_have_count(0)
        page.clock.run_for(BOUND * 2)
        assert [c['kind'] for c in sent] == ['job.cancel', 'workspace.validate']  # one automatic attempt, never a loop
        assert [row['id'] for row in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']] == [job['id']]
        with transaction(h.app.state.engine) as db:
            assert not db.get(m.Workspace, w['id']).validation.get('scope')  # the complete check landed
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_check_refused_for_the_persons_own_access_only_says_so(h, browser, live_server):
    """DL-018 + DL-007: a check job refused for the person's own access reads "Check stopped" on a disabled pill that
    points to the line saying why; nothing offers a check the person cannot run, and the page asks for none.
    The refusal is the worker's own outcome written on the real job (apps/worker/main.py: failed, ACCESS_REVOKED) and read
    through the real status endpoint. The role itself is not removed: that also changes the signed-in page's session,
    which is another journey."""
    w, job = unserved_check(h)
    BOUND = bound_ms(h, w)
    context, page = sign_in(browser, live_server, 'coordinator')
    checks = []
    try:
        page.clock.install()
        page.on('request', lambda r: checks.append(r.post_data_json['kind']) if r.method == 'POST' and COMMANDS.match(r.url)
                and (r.post_data_json or {}).get('kind') in ('job.create', 'workspace.validate') else None)
        with holding(page) as held:
            summary_url = live_server + '/api/workspaces/' + w['id'] + '/summary'
            status_url = live_server + '/api/jobs/' + job['id'] + '/status'
            held.hold(summary_url)
            held.hold(status_url)
            navigate(page, 'schedule/week/2026-10-04?edit=' + w['id'])
            footer = page.locator('.app-footer')
            progress = footer.locator('[data-check-line]').filter(has_text=CHECK_WORDS)
            expect(progress).to_contain_text('Waiting to check')
            wait_held(page, held[summary_url])
            with page.expect_response(lambda response: response.url == summary_url) as summary:
                held.release(summary_url)
            assert summary.value.ok
            page.clock.run_for(1_000)  # advance to the first queued-job status read, never real time
            wait_held(page, held[status_url])
            expect(footer.get_by_role('button', name='Cancel: waiting to check', exact=True)).to_be_visible()  # the job is joined
            with transaction(h.app.state.engine, write=True) as db:
                row = db.get(m.Job, job['id'])
                row.status, row.error_code, row.completed_at = 'failed', 'ACCESS_REVOKED', m.now()
                row.revision += 1
            line = page.locator('#main .check-stopped')
            with page.expect_response(lambda response: response.url == status_url) as status:
                held.release(status_url)
            assert status.value.ok
            expect(line).to_have_text('The check stopped · your role cannot run it')  # one verb; the reason from the one map
            expect(line.get_by_role('button')).to_have_count(0)  # the line offers no check
            pill = footer.get_by_role('button', name='Check stopped', exact=True)
            expect(pill).to_be_disabled()
            assert pill.evaluate('b => b.tagName') == 'BUTTON'
            expect(pill).to_have_attribute('aria-describedby', line.get_attribute('id'))  # DL-007: disabled, and says why
            expect(pill).to_have_accessible_description('The check stopped · your role cannot run it')
            expect(page.get_by_role('button', name=re.compile('Check again'))).to_have_count(0)
            expect(progress).to_have_count(0)
            asked = list(checks)
            page.clock.run_for(BOUND * 2)  # past the bound: no take-over, no second attempt
            expect(pill).to_be_disabled()
            assert checks == asked and 'workspace.validate' not in checks
            with transaction(h.app.state.engine) as db:
                assert db.get(m.Job, job['id']).status == 'failed'  # not stopped again, not replaced
            assert [r['id'] for r in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']] == [job['id']]
    finally:
        context.close()


def test_a_take_over_that_fails_too_says_the_check_has_not_started(h, browser, live_server):
    """DL-018: only when the page's own attempt also fails does the pill read "Check hasn't started · Check again". No
    joined job and no live job is left: nothing claims a check, and there is no second automatic attempt."""
    w, job = unserved_check(h)
    BOUND = bound_ms(h, w)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.clock.install()
        sent = watch_takeover(page, lambda route: route.fulfill(status=500, content_type='application/json',
            body='{"error":{"code":"INTERNAL","message":"Check unavailable."}}'))
        with holding(page) as held:
            summary_url = live_server + '/api/workspaces/' + w['id'] + '/summary'
            status_url = live_server + '/api/jobs/' + job['id'] + '/status'
            held.hold(summary_url)
            held.hold(status_url)
            navigate(page, 'schedule/week/2026-10-04?edit=' + w['id'])
            footer = page.locator('.app-footer')
            progress = footer.locator('[data-check-line]').filter(has_text=CHECK_WORDS)
            expect(progress).to_contain_text('Waiting to check')
            wait_held(page, held[summary_url])
            with page.expect_response(lambda response: response.url == summary_url) as summary:
                held.release(summary_url)
            assert summary.value.ok
            page.clock.run_for(1_000)  # advance to the first queued-job status read, never real time
            wait_held(page, held[status_url])
            with page.expect_response(lambda response: response.url == status_url) as status:
                held.release(status_url)
            assert status.value.ok
            page.clock.run_for(BOUND)
            again = footer.get_by_role('button', name="Check hasn't started · Check again")  # DL-018: the person's view
            expect(again).to_be_visible()
            expect(progress).to_have_count(0)  # no joined job, no live job: nothing claims a check
            expect(page.locator('#main .check-stopped')).to_have_count(0)  # it never started: not "The check stopped"
            assert [c['kind'] for c in sent] == ['job.cancel', 'workspace.validate']
            page.clock.run_for(BOUND * 2)
            expect(again).to_be_visible()
            expect(progress).to_have_count(0)
            assert [c['kind'] for c in sent] == ['job.cancel', 'workspace.validate']  # never a second automatic attempt
            assert [row['id'] for row in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']] == [job['id']]
            # The person's own Check again is the next attempt.
            page.unroute(COMMANDS)
            again.click()
            expect(page.locator('.toast').filter(has_text='Checked')).to_be_visible()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

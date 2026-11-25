"""Late scheduling replies and polling keep the correct rota's setup and result."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from apps.worker.main import process_one
from .test_browser import auto_setup, auto_result, wait_run_created, browser, live_server, navigate, open_rota, open_scheduling, pick_view, sign_in, wait_held, wait_until  # noqa: F401
from .test_browser import holding
from .test_browser_cp_sat import cp_server, _apply, _joins  # noqa: F401

pytestmark = pytest.mark.browser


def test_held_apply_keeps_review_and_live_strip_without_success_or_undo(browser, cp_server, h):
    w = h.shift(h.workspace('Held Apply is not a save'), people=[])['workspace']
    target = h.detail(w)['shifts'][0]
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'session_ids': [target['id']]}, revision=w['revision'])['job']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_rota(page, w['id'])
        panel = page.locator('#auto-panel')
        strip = page.get_by_role('contentinfo', name='Status').locator('.asl-strip')
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
        expect(strip).to_be_visible()
        # Keep Done's dismissal timer still; dispatch clicks below avoid its separate pointer/focus dismissal.
        clock = datetime(2026, 10, 3, 6, tzinfo=timezone.utc)
        page.clock.install(time=clock)
        page.clock.pause_at(clock + timedelta(seconds=1))
        h.process_jobs()
        completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        assert completed['status'] == 'succeeded' and completed['result']['additions']
        assert {a['shift_id'] for a in completed['result']['additions']} == {target['id']}
        assert not completed['result']['removals']
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        review = panel.get_by_role('region', name='Review', exact=True)
        expect(review).to_be_visible(timeout=15000)
        expect(strip.get_by_role('status')).to_have_text('Step 5 of 5: Done')
        expect(page.locator('#save-state')).to_be_hidden()

        # A governed rule change holds the proposal without changing the revision loaded by the page.
        h.policy({'kind': 'max_duties', 'maximum': 10, 'window': 'week'}, workspace_id=w['id'], checked=[w])
        apply = panel.get_by_role('button', name=re.compile(r'^(Apply \d+ changes?|Check again and apply)$'))
        expect(apply).to_be_visible()
        expect(apply).to_be_enabled()
        assert h.detail(w)['workspace']['revision'] == w['revision']
        with page.expect_response(lambda r: r.url.endswith('/api/commands')
                                  and (r.request.post_data_json or {}).get('kind') == 'generation.apply') as held:
            apply.dispatch_event('click')
        assert held.value.request.post_data_json['expected_revision'] == w['revision']
        assert held.value.status == 200 and held.value.json()['reconciliation']['status'] == 'revalidation_required'
        page.clock.run_for(100)  # Flush immediate notifications and redraws without expiring Done.
        expect(page.get_by_text('Suggestions applied to the draft', exact=False)).to_have_count(0)
        expect(page.get_by_role('alert').filter(has_text='Suggestions were not applied')).to_be_visible()
        expect(review).to_be_visible()
        expect(page.get_by_role('region', name='Messages').get_by_role('button', name='Undo', exact=True)).to_have_count(0)
        expect(panel.get_by_role('button', name=re.compile('^Undo Auto-schedule '))).to_have_count(0)
        expect(strip).to_be_visible()
        expect(page.locator('#save-state')).to_be_hidden()
        assert not any(s['participants'] for s in h.detail(w)['shifts'])
        assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'

        # The held run's governed inputs stay stale; the control must be generated under the active rule.
        control = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
            'session_ids': [target['id']]}, revision=w['revision'])['job']
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
        expect(strip).to_be_visible()
        h.process_jobs()
        completed = h.get('coordinator', '/api/jobs/' + control['id'])['job']
        assert completed['status'] == 'succeeded' and completed['result']['additions']
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(review).to_be_visible(timeout=15000)
        expect(strip.get_by_role('status')).to_have_text('Step 5 of 5: Done')
        expect(apply).to_be_visible()
        expect(apply).to_be_enabled()
        assert h.detail(w)['workspace']['revision'] == w['revision']
        with page.expect_response(lambda r: r.url.endswith('/api/commands')
                                  and (r.request.post_data_json or {}).get('kind') == 'generation.apply') as applied:
            apply.dispatch_event('click')
        assert applied.value.ok and applied.value.json()['reconciliation']['status'] == 'applied'
        page.clock.run_for(100)
        success = page.get_by_role('region', name='Messages').get_by_role('status').filter(has_text='Suggestions applied to the draft')
        expect(success).to_be_visible()
        expect(success.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        expect(strip).to_have_count(0)
        expect(page.locator('#save-state')).to_have_attribute('aria-label', re.compile('^Saved'))
        assert h.get('coordinator', '/api/jobs/' + control['id'])['job']['status'] == 'applied'
        assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'
        assert sum(len(s['participants']) for s in h.detail(w)['shifts']) == len(completed['result']['additions'])
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_late_job_creation_keeps_the_next_rotas_setup(browser, cp_server, h, width, scheme):
    first = h.shift(h.workspace('First scheduling setup'), people=[])['workspace']
    second = h.shift(h.workspace('Second scheduling setup'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': width, 'height': 900})
    page.emulate_media(color_scheme=scheme)
    held = []

    def hold_create(route):
        if route.request.post_data_json.get('kind') == 'job.create':
            held.append(route)
        else:
            route.continue_()

    try:
        open_rota(page, first['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        page.route('**/api/commands', hold_create)
        page.evaluate("""() => {
            window.schedulingCreateFinished = false;
            window.addEventListener('rota-saved', event => {
                if (event.detail.kind === 'job.create') window.schedulingCreateFinished = true;
            });
        }""")
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        wait_held(page, held)
        open_rota(page, second['id'])
        open_scheduling(page)
        auto_setup(panel, fields=False)
        panel.get_by_role('radio', name='Fairness first', exact=True).click()
        with page.expect_response(lambda response: response.request.method == 'POST'
                                  and response.url.endswith('/api/commands')
                                  and response.request.post_data_json.get('kind') == 'job.create'):
            held.pop().continue_()
        # Wait for the app to finish accepting the save before redrawing this rota's settings.
        page.wait_for_function('() => window.schedulingCreateFinished')
        pick_view(page, 'List')
        expect(panel.locator('.auto-body')).to_have_attribute('data-state', 'setup')
        expect(panel.get_by_role('radio', name='Fairness first', exact=True)).to_be_checked()
        assert h.get('coordinator', '/api/jobs?workspace_id=' + second['id'])['jobs'] == []
        jobs = h.get('coordinator', '/api/jobs?workspace_id=' + first['id'])['jobs']
        assert len(jobs) == 1 and jobs[0]['status'] == 'queued'
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'dark'), (390, 'light')])
def test_polled_completed_result_survives_a_layout_change(browser, cp_server, h, width, scheme):
    workspace = h.shift(h.workspace('Polling scheduling result'), people=[])['workspace']
    h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'}, revision=workspace['revision'])
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': width, 'height': 900})
    page.emulate_media(color_scheme=scheme)
    try:
        open_rota(page, workspace['id'])
        panel = page.locator('#auto-panel')
        expect(panel).to_be_visible()  # a run going opens the panel (pressing Auto-schedule now would close it)
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
        wait_run_created(page)
        process_one(h.app.state.engine)
        expect(panel.get_by_role('region', name='Review')).to_be_visible(timeout=15000)
        # Further polls stay pending: the layout must retain the result it has already received.
        page.route(cp_server + '/api/jobs?workspace_id=' + workspace['id'], lambda route: None)
        # The shared keyboard shortcut also works while a phone's scheduling panel covers the toolbar.
        page.keyboard.press('l')
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'List view')
        # A layout redraw uses the jobs already received, without replaying the old queued snapshot.
        expect(panel.get_by_role('region', name='Review')).to_be_visible()
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_fresh_job_staleness_overrides_an_older_rota_snapshot(browser, cp_server, h, width, scheme):
    workspace = h.shift(h.workspace('Inputs changed before result read'), people=[])['workspace']
    h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
              revision=workspace['revision'])
    process_one(h.app.state.engine)  # Keep an older run so the latest result also appears in Recent runs.
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                    revision=workspace['revision'])['job']
    process_one(h.app.state.engine)
    completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert completed['result']['additions'] and not completed['result_stale']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': width, 'height': 900})
    page.emulate_media(color_scheme=scheme)
    held = []
    page.route(cp_server + '/api/jobs/' + job['id'], lambda route: held.append(route))
    try:
        open_rota(page, workspace['id'])
        open_scheduling(page)
        wait_held(page, held)
        # The rota has already loaded. Keep live updates pending so this read must respect the server's fresh
        # staleness flag instead of relying on a later refresh to replace that older rota snapshot.
        page.route('**/api/pulse', lambda route: None)
        page.route('**/api/events', lambda route: None)
        h.command('coordinator', 'availability.record', {
            'person_id': completed['result']['additions'][0]['participant']['person_id'], 'kind': 'recorded_leave',
            'leave_type': 'Annual leave', 'start': '2026-10-04T00:00:00+03:00',
            'end': '2026-10-05T00:00:00+03:00', 'private_reason': 'Synthetic stale-result acceptance'})
        assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['result_stale']
        held.pop().continue_()
        panel = page.locator('#auto-panel')
        # A4/BE-1: the job read's fresh currency wins over the older rota snapshot. The proposal is not thrown away: it
        # says the rota changed since the run, and Apply checks it against the current rota before anything is written.
        drift = panel.locator('.auto-stale')
        expect(drift).to_have_text('The rota changed since this run · Apply checks it against the current rota first')
        auto_result(panel)
        review = panel.get_by_role('region', name='Review')
        expect(review).to_be_visible()
        expect(review.locator('.auto-stale')).to_have_count(1)  # said once, inside the result it qualifies
        expect(panel.get_by_role('button', name='Check again and apply', exact=True)).to_be_visible()
        expect(panel.get_by_role('button', name=re.compile(r'^Apply \d+ changes?$'))).to_have_count(0)  # never applied unchecked
        expect(panel.locator('.run-list li').first).to_contain_text('Check again to apply')
    finally:
        context.close()


def _proposal(page, h, panel):
    panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
    expect(panel.get_by_role('progressbar')).to_be_visible()
    wait_run_created(page)
    process_one(h.app.state.engine)
    expect(panel.get_by_role('region', name='Review')).to_be_visible(timeout=15000)


def _finish_rota_check(page, h, workspace_id, joined):
    check = next(j for j in h.get('coordinator', '/api/jobs?workspace_id=' + workspace_id)['jobs']
                 if j['kind'] == 'validation' and j['status'] == 'queued')
    status_url = f"/api/jobs/{check['id']}/status"
    # The page has joined this job before the worker takes it, so its next read can see it finish.
    wait_until(page, lambda: any(r.url.endswith(status_url) for r in joined),
               'The page did not join the rota check', seconds=15)
    with page.expect_response(lambda r: r.url.endswith(status_url) and r.ok
                              and r.json()['job']['status'] == 'succeeded'):
        assert h.process_jobs()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_an_undo_already_used_is_refused_once_without_not_saved(browser, cp_server, h, width, scheme):
    """G-17: Apply, then Undo from Recent runs; the Apply message's own Undo is then stale. Pressing it says so once and
    sends nothing: no conflict message, and the footer never turns to "Not saved". At phone width the message covers
    Recent runs (pointer overlap tracked in the backlog, decision in docs/design/fe-audit-decisions.md), so both Undo
    buttons are pressed from the keyboard there, as a keyboard user would."""
    press = (lambda b: b.click()) if width >= 768 else (lambda b: b.press('Enter'))
    w = h.shift(h.workspace('Stale toast undo'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': width, 'height': 900})
    page.emulate_media(color_scheme=scheme)
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        _proposal(page, h, panel)
        joined = _joins(page)
        panel.get_by_role('button', name='Apply 1 change', exact=True).click()
        toast = page.locator('.toast').filter(has_text='Suggestions applied')
        expect(toast).to_be_visible()
        toast.dispatch_event('mouseenter')  # Pause the toast while both rota checks settle.
        _finish_rota_check(page, h, w['id'], joined)
        # The applied run keeps its Undo in Recent runs, under the rota check that follows Apply (BE-18).
        joined = _joins(page)
        press(panel.locator('.run-list').get_by_role('button', name=re.compile('^Undo Auto-schedule ')))
        expect(page.locator('.toast').filter(has_text='Undone.')).to_be_visible()
        # Let the page join and finish the check before recording commands from the stale Undo.
        _finish_rota_check(page, h, w['id'], joined)
        commands = []
        page.on('request', lambda r: commands.append(r.post_data_json.get('kind')) if r.url.endswith('/api/commands') else None)
        press(toast.get_by_role('button', name='Undo', exact=True))
        expect(page.locator('.toast').filter(has_text='Already undone, or the rota changed since')).to_have_count(1)
        assert commands == []
        expect(page.locator('#save-state')).not_to_contain_text('Not saved')
    finally:
        context.close()


def test_a_finished_undo_check_clears_the_footer_and_stops_polling(browser, cp_server, h):
    w = h.shift(h.workspace('Check after undo'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        page.clock.install()
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        _proposal(page, h, panel)
        _apply(page, h, panel, 'Apply 1 change')
        expect(page.locator('.toast').filter(has_text='Suggestions applied')).to_be_visible()

        clock = datetime.fromtimestamp(page.evaluate('Date.now()') / 1000, timezone.utc)
        page.clock.pause_at(clock + timedelta(seconds=1))
        # Let the status poll deliver completion before a live update can refresh the kept job list.
        with holding(page) as held:
            events = re.compile(r'/api/events(?:\?|$)')
            held.hold(events)
            joined = _joins(page)
            recent = panel.locator('.run-list')
            recent.get_by_role('button', name=re.compile('^Undo Auto-schedule ')).click()
            expect(page.locator('.toast').filter(has_text='Undone.')).to_be_visible()
            check = next(j for j in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']
                         if j['kind'] == 'validation' and j['status'] == 'queued')
            check_id = check['id']
            status_url = f'{cp_server}/api/jobs/{check_id}/status'
            state = recent.locator('li').filter(has_text='Full rota check').first.locator('.badge')
            expect(state).to_have_text('Queued')
            wait_until(page, lambda: any(f'job_id={check_id}' in r.request.url for r in held[events]),
                       'the page never joined the Undo check')

            with page.expect_response(status_url) as status:
                assert h.process_jobs()
                page.clock.run_for(1_000)  # The joined job's first status poll.
            assert status.value.ok and status.value.json()['job']['status'] == 'succeeded'
            after_completion = len(joined)
            # The panel must show completion before its own job-list poll has a chance to refresh it.
            expect(state).to_have_text('Checked')
            page.clock.run_for(1_500)  # A rejoined job would poll again within this window.
            footer = page.get_by_role('contentinfo', name='Status')
            expect(footer.locator('[data-check-line]')).to_have_count(0)
            expect(footer).not_to_contain_text('Waiting to check')
            expect(footer.locator('[data-check-cancel]')).to_have_count(0)
            expect(state).to_have_text('Checked')
            assert not any(r.url == status_url for r in joined[after_completion:]), 'the finished check was polled again'
    finally:
        context.close()


@pytest.mark.parametrize('from_dialog', [False, True], ids=['panel', 'dialog'])
def test_apply_updates_the_run_and_grid_before_follow_up_reads(browser, cp_server, h, from_dialog):
    w = h.shift(h.workspace('Apply reviewed result'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_rota(page, w['id'])
        pick_view(page, 'Week')
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        _proposal(page, h, panel)
        expect(page.get_by_role('columnheader', name=re.compile(r'0 of 1 places filled'))).to_be_visible()
        apply = panel.get_by_role('button', name='Apply 1 change', exact=True)
        if from_dialog:
            panel.get_by_role('button', name='Review latest result', exact=True).click()
            dialog = page.get_by_role('dialog', name='Auto-schedule results')
            apply = dialog.get_by_role('button', name='Apply the change', exact=True)

        # Let the review finish loading before holding reads that could redraw the saved result.
        expect(apply).to_be_enabled()
        page.route('**/api/**', lambda route: None if route.request.method == 'GET' else route.continue_())
        with page.expect_response(lambda r: r.url.endswith('/api/commands')
                                  and (r.request.post_data_json or {}).get('kind') == 'generation.apply') as saved:
            apply.click()
        assert saved.value.ok and saved.value.json()['reconciliation']['status'] == 'applied'
        expect(panel.get_by_text('Assignments added to draft', exact=True)).to_be_visible()
        expect(panel.locator('.run-list .badge').filter(has_text=re.compile(r'^Applied$'))).to_be_visible()
        expect(panel.get_by_role('button', name='Apply 1 change', exact=True)).to_have_count(0)
        expect(page.get_by_role('dialog', name='Auto-schedule results')).to_have_count(0)
        expect(page.get_by_role('columnheader', name=re.compile(r'1 of 1 places filled'))).to_be_visible()
        expect(page.locator('.toast').filter(has_text='Suggestions applied')).to_be_visible()
    finally:
        context.close()


def test_redo_of_an_applied_proposal_refreshes_the_footer(browser, cp_server, h):
    w = h.shift(h.workspace('Check after redo'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        footer = page.get_by_role('contentinfo', name='Status')
        must_fix = footer.get_by_role('button', name=re.compile(r'^1 must fix'))
        ready = footer.get_by_role('button', name=re.compile(r'^Ready to submit.*score'))
        expect(must_fix).to_be_visible()
        _proposal(page, h, panel)

        # Let each check settle through its status response before a live update can refresh the rota separately.
        with holding(page) as held:
            held.hold(re.compile(r'/api/events(?:\?|$)'))
            joined = _joins(page)
            panel.get_by_role('button', name='Apply 1 change', exact=True).click()
            expect(page.locator('.toast').filter(has_text='Suggestions applied')).to_be_visible()
            _finish_rota_check(page, h, w['id'], joined)
            expect(ready).to_be_visible()

            joined = _joins(page)
            panel.locator('.run-list').get_by_role('button', name=re.compile('^Undo Auto-schedule ')).click()
            expect(page.locator('.toast').filter(has_text='Undone.')).to_be_visible()
            _finish_rota_check(page, h, w['id'], joined)
            expect(must_fix).to_be_visible()
            expect(footer.locator('[data-check-line]')).to_have_count(0)

            joined = _joins(page)
            page.locator('.planner-bar').get_by_role('button', name='Redo', exact=True).click()
            expect(page.locator('.toast').filter(has_text='Redone')).to_be_visible()
            _finish_rota_check(page, h, w['id'], joined)
            expect(ready).to_be_visible()
            expect(must_fix).to_have_count(0)
            expect(footer.locator('[data-check-line]')).to_have_count(0)
            expect(footer).not_to_contain_text('Needs a check')
    finally:
        context.close()


def test_an_unrelated_message_during_a_slow_refresh_cannot_take_a_draft_edits_undo(browser, cp_server, h):
    """Review #586: Apply suggestions, the refresh after it is held, then Account › Notifications › Turn off (tab alerts). The applied
    suggestions keep their own Undo; "Desktop alerts are off." offers none (its Undo would have removed the rota edit)."""
    w = h.shift(h.workspace('Held refresh undo'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    held = []
    try:
        page.evaluate("localStorage.setItem('rota.alerts', 'on')")
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        _proposal(page, h, panel)

        def without_delta(route):
            if (route.request.post_data_json or {}).get('kind') != 'generation.apply':
                return route.continue_()
            reply = route.fetch()
            route.fulfill(response=reply, json={**reply.json(), 'delta': None})

        # Account › Notifications reads this device's alert state; only that read passes the hold.
        page.route('**/api/**', lambda route: held.append(route) if route.request.method == 'GET' and '/api/me/push' not in route.request.url else route.continue_())
        # An older reply requires the slow draft read this test exercises.
        page.route('**/api/commands', without_delta)
        panel.get_by_role('button', name='Apply 1 change', exact=True).click()
        wait_held(page, held)  # the refresh after the edit is waiting on the server
        applied = page.locator('.toast').filter(has_text='Suggestions applied')
        expect(applied.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        page.get_by_role('button', name=re.compile('^Account menu, ')).click()
        page.get_by_role('menuitem', name='Notifications', exact=True).click()
        page.locator('.np-panel').get_by_role('button', name='Turn off', exact=True).click()
        alerts = page.locator('.toast').filter(has_text='Desktop alerts are off.')
        expect(alerts).to_be_visible()
        expect(alerts.get_by_role('button', name='Undo', exact=True)).to_have_count(0)
        expect(page.locator('.toast').get_by_role('button', name='Undo', exact=True)).to_have_count(1)
        for route in held:  # released once each, before unroute: unroute itself continues whatever it still holds
            route.continue_()
        page.unroute('**/api/**')
        expect(applied.get_by_role('button', name='Undo', exact=True)).to_be_visible()
    finally:
        context.close()


def test_auto_schedule_polls_only_while_a_run_is_going(browser, cp_server, h):
    """PL-03 budget: an idle rota's open panel is read every 30 s at most (another coordinator's run moves no pulse); a
    queued run with the panel closed is read every 30 s, not every 3 s (timers driven by the page clock, no waiting)."""
    idle = h.shift(h.workspace('No run going'), people=[])['workspace']
    busy = h.shift(h.workspace('Run queued, panel closed'), people=[])['workspace']
    h.command('coordinator', 'job.create', {'workspace_id': busy['id'], 'kind': 'generation'}, revision=busy['revision'])
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    reads = []
    page.on('request', lambda r: reads.append(r.url) if '/api/jobs' in r.url else None)
    try:
        page.clock.install()
        open_rota(page, idle['id'])
        open_scheduling(page)
        expect(page.locator('#auto-panel').locator('.auto-body')).to_have_attribute('data-state', 'setup')
        before = len(reads)
        page.clock.run_for(120_000)
        expect(page.locator('#auto-panel')).to_be_visible()
        assert len(reads) - before <= 4, reads[before:]  # bounded idle refresh: one read per 30 s while open and shown
        page.get_by_role('button', name='Auto-schedule', exact=True).click()  # close it
        open_rota(page, busy['id'])
        expect(page.locator('#auto-panel')).to_be_visible()  # a running job opens the panel; close it again
        page.locator('#auto-panel').get_by_role('button', name='Close auto-schedule', exact=True).click()
        expect(page.locator('#auto-panel')).to_be_hidden()
        before = len(reads)
        page.clock.run_for(60_000)
        expect(page.locator('#auto-panel')).to_be_hidden()
        assert len(reads) - before <= 2, reads[before:]  # was 20 at one every 3 s
    finally:
        context.close()


def test_an_idle_open_panel_sees_another_coordinators_new_run_without_a_reload(browser, cp_server, h):
    """Review #571 item 7: another coordinator (a second client) starts a run on this rota. job.create moves no live
    pulse, so the idle open panel finds it on its bounded 30 s read and shows it running, with no reload."""
    w = h.shift(h.workspace('Run started elsewhere'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        page.clock.install()
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        expect(panel.locator('.auto-body')).to_have_attribute('data-state', 'setup')
        expect(panel.get_by_role('progressbar')).to_have_count(0)
        h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])
        page.clock.run_for(31_000)
        expect(panel.get_by_role('progressbar')).to_be_visible()
    finally:
        context.close()


def test_apply_patches_the_draft_without_reading_the_whole_app(browser, cp_server, h):
    """G-45: Apply is one command whose reply patches the draft; the session, lists and leave types are not read again,
    and the rota is not read before the command (the page holds its revision)."""
    w = h.shift(h.shift(h.workspace('Apply in place'), people=[])['workspace'], start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        _proposal(page, h, panel)
        reads = []
        page.on('request', lambda r: reads.append((r.method, r.url.split('/api/')[-1].split('?')[0])) if '/api/' in r.url else None)
        _apply(page, h, panel, 'Apply 2 changes')
        expect(page.locator('.toast').filter(has_text='Suggestions applied')).to_be_visible()
        # Applied: the run reads Applied with its Undo, under the complete check that followed it (BE-18).
        expect(panel.locator('.run-list').get_by_role('button', name=re.compile('^Undo Auto-schedule '))).to_be_visible()
        expect(panel.locator('.run-list li').filter(has_text='Auto-schedule')).to_contain_text('Applied')
        paths = [p for _, p in reads]
        for whole in ('me', 'catalogues', 'leave-types'):
            assert whole not in paths, reads
        first = reads.index(('POST', 'commands'))
        assert not [p for _, p in reads[:first] if p.startswith('workspaces')], reads  # no read of the rota before Apply
    finally:
        context.close()


def test_a_retry_that_reads_the_same_result_enables_apply_again(browser, cp_server, h):
    """Review #571 (SHOULD 2): a failed poll disables Apply under "Couldn't refresh"; a Retry whose read is unchanged
    still redraws the footer, so Apply is enabled again (it used to stay disabled once the warning cleared)."""
    w = h.shift(h.workspace('Retry re-enables Apply'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        page.clock.install()
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar')).to_be_visible()
        wait_run_created(page)
        process_one(h.app.state.engine)
        page.clock.run_for(4_000)  # the running poll reads the finished run
        expect(panel.get_by_role('region', name='Review')).to_be_visible(timeout=15000)
        apply = panel.get_by_role('button', name='Apply 1 change', exact=True)
        expect(apply).to_be_enabled()
        page.route('**/api/jobs?workspace_id=*', lambda route: route.fulfill(status=503, json={'error': {'code': 'UNAVAILABLE', 'message': 'Service unavailable'}}))
        page.clock.run_for(31_000)  # the open panel's idle read fails
        paused = panel.locator('.auto-paused')
        expect(paused).to_contain_text("Couldn't refresh. Showing the last result.")
        expect(paused).not_to_contain_text('Service unavailable')  # a bare status phrase is not shown
        expect(apply).to_be_disabled()
        page.unroute('**/api/jobs?workspace_id=*')
        paused.get_by_role('button', name='Retry', exact=True).click()  # the same jobs come back
        expect(panel.locator('.auto-paused')).to_have_count(0)
        expect(panel.get_by_role('button', name='Apply 1 change', exact=True)).to_be_enabled()
    finally:
        context.close()


def test_a_failed_refresh_keeps_the_result_with_retry(browser, cp_server, h):
    """G-46: a poll that fails keeps the run on screen under one line with Retry inside; it does not replace it."""
    w = h.shift(h.workspace('Failed poll keeps result'), people=[])['workspace']
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        page.clock.install()
        open_rota(page, w['id'])
        panel = page.locator('#auto-panel')
        expect(panel).to_be_visible()  # a run going opens the panel
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
        wait_run_created(page)
        down = lambda route: route.fulfill(status=503, json={'error': {'code': 'UNAVAILABLE', 'message': 'Service unavailable'}})
        page.route('**/api/jobs?workspace_id=*', down)
        page.route('**/api/jobs/**', down)  # a run shown live is read on its own (its compact status read, BE-17)
        page.clock.run_for(4_000)
        paused = panel.locator('.auto-paused')
        expect(paused).to_contain_text("Couldn't refresh. Showing the last result.")
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()  # still shown
        process_one(h.app.state.engine)
        # Keep failures in place while the worker finishes, then hold reads until Retry has been clicked.
        retried = []
        hold = lambda route: retried.append(route)
        for url in ('**/api/jobs?workspace_id=*', '**/api/jobs/**'):
            page.route(url, hold)
            page.unroute(url, down)
        paused.get_by_role('button', name='Retry', exact=True).click()
        wait_held(page, retried)
        expect(panel.locator('.auto-paused')).to_have_count(0)
        expect(panel.get_by_role('region', name='Review')).not_to_be_visible()
        # Unroute resumes routes it still holds: release these once before removing their handlers.
        for route in retried:
            route.continue_()
        for url in ('**/api/jobs?workspace_id=*', '**/api/jobs/**'):
            page.unroute(url, hold)
        expect(panel.get_by_role('region', name='Review')).to_be_visible(timeout=15000)
        expect(panel.locator('.auto-paused')).to_have_count(0)
    finally:
        context.close()


def test_panel_shows_skeleton_while_jobs_load(browser, cp_server, h):
    """FD-03: while the rota's runs are read, the panel's place shows its skeleton, never an empty column."""
    w = h.shift(h.workspace('Skeleton while jobs load'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    held = []
    try:
        # The panel reads the rota's runs when the rota opens (closed, it still discovers a run going): hold that read.
        page.route(re.compile(r'/api/jobs\?workspace_id='), lambda route: held.append(route))
        open_rota(page, w['id'])
        wait_held(page, held)
        page.get_by_role('button', name='Auto-schedule', exact=True).click()
        expect(page.locator('#auto-panel .sk-panel')).to_be_attached()
        expect(page.locator('#auto-panel .automatic-scheduling')).to_have_count(0)
        held.pop().continue_()
        page.unroute(re.compile(r'/api/jobs\?workspace_id='))
        expect(page.locator('#auto-panel .automatic-scheduling')).to_be_visible()
        expect(page.locator('#auto-panel .sk-panel')).to_have_count(0)
    finally:
        context.close()


def test_closed_panel_holds_no_endless_skeleton_after_address_or_reload(browser, cp_server, h):
    """G1 J5 (11 Nov 2026): a rota opened by its address, or reloaded, with a finished run keeps Auto-schedule closed.
    The closed panel waits on nothing, so it holds no skeleton ("Still loading · m:ss" that never ended); opened, it shows
    its result, and a failed read ends in Couldn't load with Retry, never a skeleton left loading."""
    w = h.shift(h.workspace('Closed panel after a run'), people=[])['workspace']
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])
    process_one(h.app.state.engine)
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    panel = page.locator('#auto-panel')
    jobs = re.compile(r'/api/jobs\?workspace_id=')
    try:
        navigate(page, f"schedule/week/{w['starts_on']}?edit={w['id']}")
        for arrival in ('address', 'reload'):
            if arrival == 'reload':
                page.reload()
            # The panel and its first read of the rota's runs land in one step (schedulingPanel): attached = read.
            expect(panel.locator('.auto-body')).to_be_attached()
            expect(panel).to_be_hidden()
            expect(panel.locator('.loading')).to_have_count(0)
            # A failed read on opening says so in the panel, with Retry; Retry reads again and shows the result.
            page.route(jobs, lambda route: route.fulfill(status=500, json={'error': {'code': 'INTERNAL', 'message': 'Jobs unavailable.'}}))
            open_scheduling(page)
            failed = panel.get_by_role('button', name='Retry', exact=True)
            expect(failed).to_be_visible()
            expect(panel).to_contain_text('Couldn’t load auto-schedule.')
            expect(panel.locator('.loading')).to_have_count(0)
            page.unroute(jobs)
            failed.click()
            expect(panel.get_by_role('region', name='Review')).to_be_visible()
            expect(panel.locator('.loading')).to_have_count(0)
    finally:
        context.close()


def test_progress_single_line_and_cancel(browser, cp_server, h):
    """FD-04: a running job is one changing line ("n/6 step · % · time") over one bar, with Cancel; no step list or
    helper sentences."""
    w = h.shift(h.workspace('One progress line'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
        progress = panel.locator('.auto-progress')
        expect(progress.locator('p')).to_have_count(1)
        expect(progress.locator('ol')).to_have_count(0)
        expect(progress).not_to_contain_text('you can keep working')
        expect(panel.get_by_role('button', name='Cancel auto-schedule', exact=True)).to_be_visible()
    finally:
        context.close()


def test_run_is_disabled_with_reason_for_empty_scope(browser, cp_server, h):
    """SR-28: "Selected sessions only" with nothing selected disables Run and says why before it is pressed."""
    w = h.shift(h.workspace('Run says why'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        run = panel.get_by_role('button', name='Run auto-schedule', exact=True)
        expect(run).to_be_enabled()
        auto_setup(panel)  # D76: the setup's fields open under Change
        panel.get_by_label('Assign into', exact=True).select_option('selected')
        expect(run).to_be_disabled()
        expect(panel.locator('.auto-why')).to_have_text('Select sessions on the schedule first.')
        panel.get_by_label('Assign into', exact=True).select_option('period')
        expect(run).to_be_enabled()
        expect(panel.locator('.auto-why')).to_have_text('')
    finally:
        context.close()


def test_failed_auto_check_is_said_once(browser, cp_server, h):
    """SR-19: a refused automatic rota check says so once, instead of leaving a silent "Needs a check". B7 (FE-B): it is
    one line under the toolbar naming why, with Check again, never a toast that goes away; the footer reads Check stopped · Check again (DL-018: the pill follows the line)."""
    w = h.shift(h.workspace('Check refused'), people=[])['workspace']
    # Edits keep a draft checked; a rule change leaves it needing one (activated over this rota with its check receipt).
    h.policy({'kind': 'restriction', 'weekdays': [6]}, checked=[w])
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})

    def refuse(route):
        if (route.request.post_data_json or {}).get('kind') == 'workspace.validate':
            route.fulfill(status=500, content_type='application/json', body='{"error":{"code":"INTERNAL","message":"Check unavailable."}}')
        else:
            route.continue_()
    page.route('**/api/commands', refuse)
    try:
        open_rota(page, w['id'])
        line = page.locator('#main .check-stopped')
        expect(line).to_have_count(1)
        expect(line).to_contain_text(re.compile(r'^The check stopped after \d+:\d\d\. Check unavailable\.'))
        expect(line.get_by_role('button', name='Check again', exact=True)).to_be_visible()
        # DL-018: one state, one verb: the pill says what the line says
        expect(page.locator('.app-footer').get_by_role('button', name='Check stopped · Check again')).to_be_visible()
        page.wait_for_load_state("networkidle")  # a second refused check would have been answered by now
        expect(line).to_have_count(1)
        expect(page.locator('.toast').filter(has_text='check')).to_have_count(0)  # said once, in the line only
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_queued_run_not_compared_yet_never_says_the_rota_changed(browser, cp_server, h):
    """G1FP: a deferred run not yet prepared has no effective input fingerprint (currency 'preparing'). It makes no
    claim about the rota: its progress shows, never "Rota changed" or "Older run"."""
    w = h.shift(h.workspace('Run not prepared yet'), people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])['job']

    def unprepared(route):
        reply = route.fetch()
        body = reply.json()
        for j in body.get('jobs') or [body.get('job') or {}]:
            if j.get('id') == job['id']:
                j.update(input_fingerprint=None, currency='preparing')
        route.fulfill(response=reply, json=body)

    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    page.route(re.compile(r'/api/jobs(\?|/)'), unprepared)
    try:
        open_rota(page, w['id'])
        panel = page.locator('#auto-panel')
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
        expect(panel).not_to_contain_text('Rota changed')
        expect(panel).not_to_contain_text('Older run')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()
        for j in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']:
            if j['status'] in ('queued', 'running'):  # left for no later test's worker
                h.command('coordinator', 'job.cancel', {'reason': 'Browser test clean-up'}, j['id'], j['revision'])

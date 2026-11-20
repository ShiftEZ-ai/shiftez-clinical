import re
import time
from dataclasses import replace
from datetime import datetime, timedelta
import pytest
from playwright.sync_api import expect
from .test_browser import wait_run_created, route_fetch, browser, live_server, sign_in, navigate, open_scheduling, pick_view, open_rota, wait_held, wait_until, auto_setup, auto_result
from apps.api.main import create_app
from apps.worker.main import process_one
from packages.contracts.schemas import GENERATION_WEIGHT_PRESETS
from .test_browser_rule_catalogue import shot as _shot
from .test_browser_rule_checks import adds_label, no_new_label

# The Results dialog's Apply for every change: 'Apply the change' or 'Apply all N changes'.
APPLY_ALL = re.compile(r'^Apply (the change|all \d+ changes)$')
pytestmark = pytest.mark.browser


@pytest.fixture
def cp_server(h, monkeypatch, request):
    monkeypatch.setenv('ROTA_GENERATION_ENGINE', 'cp_sat')
    # Three places decide a run's scheduler and must agree, or the worker refuses the run (STALE_SCHEDULER): the live
    # server (the environment above), the worker these tests run (process_one reads the harness engine's settings) and
    # the harness app that queues jobs sent with h.command (it keeps the Settings it was created with).
    engine = h.app.state.engine
    engine.update_execution_options(rota_write_settings=replace(
        engine.get_execution_options()['rota_write_settings'], generation_engine='cp_sat'))
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings, engine=engine)
    h.clients.clear()  # they were bound to the old app
    return request.getfixturevalue('live_server')


@pytest.mark.parametrize('width', [1440, 390])
def test_late_scheduling_panel_and_scroll_keep_actions_clear_of_toasts(browser, cp_server, h, width):
    workspace = h.shift(h.workspace('Late scheduling controls'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': width, 'height': 900})
    jobs_url = cp_server + '/api/jobs?workspace_id=' + workspace['id']
    held = []
    paused = True
    def hold_jobs(route):
        if paused:
            held.append(route)
        else:
            route.continue_()
    page.route(jobs_url, hold_jobs)
    try:
        open_rota(page, workspace['id'])
        wait_held(page, held)
        open_scheduling(page)
        # The page footer was registered before this delayed panel existed.
        page.evaluate("async () => { const {notify}=await window.importBundle('dom.js');notify('Scheduling test notice','error'); }")
        notice = page.get_by_role('alert').filter(has_text='Scheduling test notice')
        expect(notice).to_be_visible()
        paused = False
        for route in held:
            route.continue_()
        run = page.get_by_role('button', name='Run auto-schedule', exact=True)
        expect(run).to_be_visible()
        page.wait_for_function("""() => {const t=document.querySelector('#toast-tray'),f=document.querySelector('.auto-foot');
            const r=f?.getBoundingClientRect();return r && (r.top>=innerHeight || t.getBoundingClientRect().bottom<=r.top);}""")
        if width == 390:
            # A normal-flow phone footer changes position without resizing.
            page.locator('.auto-foot').evaluate("f => window.scrollBy(0,f.getBoundingClientRect().bottom-innerHeight+100)")
            page.wait_for_function("""() => document.querySelector('#toast-tray').getBoundingClientRect().bottom <=
                document.querySelector('.auto-foot').getBoundingClientRect().top""")
        expect(notice).to_be_visible()
        run.click()
        expect(page.locator('#auto-panel').get_by_role('progressbar')).to_be_visible()
        wait_run_created(page)
        assert h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs'][0]['status'] == 'queued'
    finally:
        context.close()


def test_planner_configures_generates_and_applies_cp_sat(browser, cp_server, h):
    workspace = h.shift(h.workspace('FM clinic planning'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator')
    navigate(page, 'planner')
    open_scheduling(page)
    open_rota(page, workspace['id'])
    panel = page.locator('#auto-panel')
    auto_setup(panel, fields=False)
    # A run picks a preset and its own search time; scoring, advisory strengths and tuning are department settings.
    expect(panel.get_by_label('Search time', exact=True)).to_be_visible()
    for label in ('Coverage weight', 'Default for each run', 'Stop after no improvement', 'Low strength (×)'):
        expect(page.get_by_label(label, exact=True)).to_have_count(0)
    expect(panel.get_by_role('button', name='Change scoring settings')).to_have_count(0)
    expect(panel).not_to_contain_text('set by an admin')  # no tagline (G-05)
    panel.get_by_role('radio', name='Fairness first', exact=True).click()
    # Keep existing assignments is the default; the setup line says what was chosen (D76: one line with Change).
    auto_setup(page.locator('#auto-panel'))
    expect(page.get_by_label('Keep existing assignments', exact=True)).to_be_checked()
    expect(page.locator('#auto-panel .rl-done')).to_have_count(1)
    expect(page.locator('#auto-panel .rl-done').first).to_contain_text('keep existing assignments')
    queued_requests = []
    page.on('request', lambda request: queued_requests.append(request.url))
    page.get_by_role('button', name='Run auto-schedule', exact=True).click()
    expect(page.locator('#auto-panel').get_by_role('progressbar')).to_be_visible()
    wait_run_created(page)
    expect(page.locator('#auto-panel .auto-stage strong')).to_have_text(re.compile(r'^(Waiting for the scheduler to start|\d/5 .+)$'))
    assert not any(url.endswith('/api/catalogues') or url.endswith('/api/workspaces/' + workspace['id']) for url in queued_requests)
    job = h.get('coordinator', '/api/jobs')['jobs'][0]
    assert job['generation_engine'] == 'cp_sat'
    assert job['effective_options']['mode'] == 'fill_gaps'
    factory = h.get('coordinator', '/api/policies/capabilities')['generation_defaults']
    assert 'objective_order' not in factory
    # The run names its preset; the server resolves the weights (raw per-run weights stay refused).
    assert job['effective_options'].get('weight_preset') == 'fairness_first'
    assert 'objective_order' not in job['effective_options']
    process_one(h.app.state.engine)
    expect(page.get_by_role('button', name='Review latest result', exact=True)).to_be_visible(timeout=15000)
    # The proposal's actions fit the side panel: whole labels, nothing past its edge.
    expect(page.locator('.auto-foot')).to_be_visible()
    assert page.locator('.auto-foot').evaluate("""row => { const edge = row.getBoundingClientRect().right;
        return [...row.querySelectorAll('button')].every(b => b.scrollWidth <= b.clientWidth + 1 && b.getBoundingClientRect().right <= edge + 1); }""")
    page.get_by_role('button', name='Review latest result', exact=True).click()
    # Timing and evidence are support detail: under Run details, after the result.
    page.get_by_role('dialog').locator('details.auto-run-details > summary').click()
    expect(page.get_by_role('dialog').locator('.run-phases')).to_contain_text('Searching')
    page.get_by_role('button', name='View candidates considered', exact=True).click()
    evidence = page.get_by_role('dialog', name='Candidates considered', exact=True)
    expect(evidence.get_by_role('columnheader', name='Person', exact=True)).to_be_visible()
    expect(evidence.get_by_role('cell', name='Eligible and available; other rules still apply', exact=True).first).to_be_visible()
    evidence.get_by_role('button', name='Close dialog', exact=True).click()
    # Suggestions: the day has its own column, so one Time column (hours only) instead of full From and Until stamps.
    results = page.get_by_role('dialog', name='Auto-schedule results')
    expect(results.get_by_role('columnheader', name='Time', exact=True)).to_be_visible()
    expect(results.get_by_role('columnheader', name='From', exact=True)).to_have_count(0)
    expect(results.get_by_role('cell', name=re.compile(r'^\d\d:\d\d–\d\d:\d\d$')).first).to_be_visible()
    results.get_by_role('button', name=APPLY_ALL).click()
    expect(page.get_by_role('dialog')).not_to_be_visible()
    assert h.detail(workspace)['shifts'][0]['participants']
    assert h.get('coordinator', '/api/publications')['publications'] == []
    context.close()


@pytest.fixture
def queued_generation_view_change(browser, cp_server, h):
    workspace = h.shift(h.workspace('FM queued job view change'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator')
    jobs_url = cp_server + '/api/jobs?workspace_id=' + workspace['id']
    held = []

    def hold_generation(route):
        request = route.request
        if request.method == 'POST' and request.post_data_json['kind'] == 'job.create':
            held.append(route)  # Forward this actual request after the replacement panel has loaded.
        else:
            route.continue_()

    try:
        # The draft's jobs are read once, with its sessions, wherever the draft first shows (landing or its link).
        with page.expect_response(lambda response: response.url == jobs_url) as initial:
            navigate(page, 'planner')
            open_scheduling(page)
            open_rota(page, workspace['id'])
        assert initial.value.json()['jobs'] == []
        panel = page.locator('#auto-panel')
        expect(panel.locator('.auto-body')).to_have_attribute('data-state', 'setup')
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'Week view')
        old_panel = panel.element_handle()
        page.route('**/api/commands', hold_generation)
        with page.expect_request(lambda request: request.method == 'POST' and request.url.endswith('/api/commands')
                                 and request.post_data_json['kind'] == 'job.create'):
            panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        assert len(held) == 1
        assert h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs'] == []

        # Another view (List) redraws the panel while no job is committed yet (it reuses the jobs read on open: PL-03).
        # The press is still on its way, so the redrawn panel offers no second Run (ME-03).
        pick_view(page, 'List')
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'List view')
        expect(panel.get_by_role('button', name='Run auto-schedule', exact=True)).to_have_count(0)
        assert not old_panel.evaluate('element => element.isConnected')
        empty_loaded = time.monotonic()
        with page.expect_response(lambda response: response.request.method == 'POST'
                                  and response.url.endswith('/api/commands')
                                  and response.request.post_data_json['kind'] == 'job.create') as queued:
            held.pop().continue_()
        assert queued.value.status == 200
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible(timeout=6000)
        elapsed = time.monotonic() - empty_loaded
        yield h, workspace, page, panel, elapsed
    finally:
        context.close()


def test_queued_generation_survives_a_planner_view_change_and_applies(queued_generation_view_change):
    h, workspace, page, panel, _ = queued_generation_view_change
    expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'List view')
    jobs = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
    assert len(jobs) == 1
    job = jobs[0]
    assert job['status'] == 'queued' and job['generation_engine'] == 'cp_sat'
    assert not h.detail(workspace)['shifts'][0]['participants']

    process_one(h.app.state.engine)
    expect(panel.get_by_role('button', name='Review latest result', exact=True)).to_be_visible(timeout=15000)
    completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    _change_tile(panel.get_by_role('region', name='Review'), completed['result'])  # the queued run's own result
    expect(panel.get_by_role('button', name='Apply 1 change', exact=True)).to_be_visible()
    assert completed['status'] == 'succeeded'
    additions = completed['result']['additions']
    assert len(additions) == 1
    assignment = additions[0]['participant']
    panel.get_by_role('button', name='Review latest result', exact=True).click()
    review = page.get_by_role('dialog', name='Auto-schedule results', exact=True)
    review.get_by_role('button', name=APPLY_ALL).click()
    expect(review).not_to_be_visible()
    applied = h.detail(workspace)
    assert applied['shifts'][0]['id'] == additions[0]['shift_id']
    assert len(applied['shifts'][0]['participants']) == 1
    participant = applied['shifts'][0]['participants'][0]
    for key in ('person_id', 'start', 'end'):
        assert participant[key] == assignment[key]
    assert applied['workspace']['status'] == 'draft'
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'applied'
    assert h.get('coordinator', '/api/publications')['publications'] == []

    page.reload()
    navigate(page, 'planner')
    pick_view(page, 'List')
    name = next(person['display_name'] for person in h.get('coordinator', '/api/catalogues')['people']
                if person['id'] == assignment['person_id'])
    expect(page.locator('tr.dt-open').get_by_text(name, exact=True)).to_be_visible()
    assert h.detail(workspace)['shifts'][0]['participants'] == applied['shifts'][0]['participants']


@pytest.mark.wall_benchmark
def test_queued_generation_view_change_cpu_budget(queued_generation_view_change):
    *_, elapsed = queued_generation_view_change
    assert elapsed < 8  # The empty panel's idle poll takes 10 seconds.


def test_planner_groups_location_conflicts_with_capacity(browser, cp_server, h):
    workspace = h.shift(h.workspace('FM room capacity check'), people=[])['workspace']
    workspace = h.shift(workspace, people=[])['workspace']
    h.shift(workspace, people=[])
    context, page = sign_in(browser, cp_server, 'coordinator')
    navigate(page, 'planner')
    open_scheduling(page)
    open_rota(page, workspace['id'])
    page.get_by_role('button', name='Run auto-schedule', exact=True).click()
    expect(page.locator('#auto-panel').get_by_role('progressbar')).to_be_visible()  # drawn as soon as Run is pressed
    wait_run_created(page)  # the worker can only take the run once job.create has answered
    process_one(h.app.state.engine)
    page.get_by_role('button', name='Review latest result', exact=True).click()
    review = page.get_by_role('dialog', name='Auto-schedule results', exact=True)
    # Shown as a labelled table, not behind a closed disclosure.
    expect(review.get_by_role('heading', name=re.compile('3 concurrent sessions, capacity 2 · 1 affected interval$'))).to_have_count(1)
    expect(review.get_by_role('columnheader', name='Published', exact=True)).to_be_visible()
    context.close()


@pytest.mark.parametrize('change', ['hours', 'scope', 'rule_days'])
def test_rule_what_if_keeps_saved_limit_and_assignments_unchanged(browser, cp_server, h, change):
    workspace = h.shift(h.workspace('FM clinic limit trial'), people=[])['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'FM daily hours', 'owner': 'FM coordinator', 'source': 'What-if scenario',
        'starts_on': '2026-01-01', 'definition': {'rule': {'kind': 'restriction', 'weekdays': [6]} if change == 'rule_days' else {'kind': 'max_hours', 'hours': 1, 'window': 'day'}}})['record']
    before = h.snapshot(workspace)
    context, page = sign_in(browser, cp_server, 'coordinator')
    navigate(page, 'rules')
    page.locator('.rule-item').filter(has_text=policy['name']).locator('.rl-actions [aria-label^="More for"]').click()  # the row's More menu
    page.get_by_role('menuitem', name='Check rule against rotas', exact=True).click()
    page.get_by_role('button', name='Check a change', exact=True).click()
    if change == 'hours':
        page.get_by_label('Hours', exact=True).fill('4')
    elif change == 'scope':
        page.get_by_text('Who and when', exact=True).click()
        page.get_by_role('group', name='Weekdays', exact=True).get_by_label('Sunday', exact=True).uncheck()
    else:
        days = page.get_by_role('group', name='Rule weekdays', exact=True)
        days.get_by_label('Sunday', exact=True).uncheck()
        days.get_by_label('Monday', exact=True).check()
    page.get_by_role('button', name='Check this change', exact=True).click()
    expect(page.get_by_role('button', name='View change check', exact=True)).to_be_visible()
    expect(page.get_by_text('Change check waiting', exact=True)).to_be_visible()
    job = h.get('coordinator', '/api/jobs')['jobs'][0]
    assert job['checked_rules'] == [{'policy_id': policy['id'], 'temporary_change': True}]
    process_one(h.app.state.engine)
    expect(page.get_by_text('Change check complete', exact=True)).to_be_visible(timeout=15000)
    page.get_by_role('button', name='View change check', exact=True).click()
    expect(page.get_by_text('A valid schedule was found with the proposed limits.', exact=True)).to_be_visible()
    assert h.snapshot(workspace) == before
    context.close()


def test_planner_explains_workload_and_clinic_conflict(browser, cp_server, h):
    h.policy({'kind': 'max_hours', 'hours': 1, 'window': 'day'})
    workspace = h.shift(h.workspace('FM conflicting daily limit'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator')
    navigate(page, 'planner')
    open_scheduling(page)
    open_rota(page, workspace['id'])
    page.get_by_role('button', name='Run auto-schedule', exact=True).click()
    expect(page.locator('#auto-panel').get_by_role('progressbar')).to_be_visible()
    wait_run_created(page)
    process_one(h.app.state.engine)
    _finish(page, h)  # nobody can be filled: the person chooses Fill everything else; it says what needs you
    panel = page.locator('#auto-panel')
    review = panel.get_by_role('region', name='Review')
    expect(review).to_be_visible(timeout=15000)
    # Nobody can work a four-hour session under a one-hour daily limit: the run proposes nothing and says what is
    # still to fix in the server's words (the limit is the cause), with nothing to apply.
    must = [c for c in _job(h, workspace)['result']['fix_list_after']['causes'] if c['level'] == 'must']
    _expect_still(review, must)
    expect(panel.get_by_role('button', name=re.compile(r'^Apply'))).to_have_count(0)
    page.get_by_role('button', name='Review latest result', exact=True).click()
    dialog = page.get_by_role('dialog')
    expect(dialog.get_by_role('button', name=APPLY_ALL)).to_have_count(0)
    assert not h.detail(workspace)['shifts'][0]['participants']
    context.close()


@pytest.mark.parametrize('blocks', [False, True])
def test_rule_review_checks_and_independent_activation(browser, cp_server, h, blocks):
    workspace = h.shift(h.workspace('FM Sunday clinic review'), people=[])['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Monday protected teaching', 'owner': 'FM coordinator', 'source': 'Review scenario',
        'starts_on': '2026-01-01', 'definition': {'rule': {'kind': 'restriction', 'weekdays': [6] if blocks else [0]}}})['record']
    context, page = sign_in(browser, cp_server, 'coordinator')
    navigate(page, 'rules')
    page.locator('.rule-item').filter(has_text=policy['name']).locator('.rl-actions [aria-label^="More for"]').click()  # the row's More menu
    page.get_by_role('menuitem', name='Check rule against rotas', exact=True).click()
    page.get_by_role('button', name='Check all affected rotas', exact=True).click()
    expect(page.get_by_role('cell', name='Waiting to check', exact=True)).to_be_visible()
    job = h.get('coordinator', '/api/jobs')['jobs'][0]
    assert job['checked_rules'] == [{'policy_id': policy['id'], 'temporary_change': False}]
    process_one(h.app.state.engine)
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    outcome = ('This rule introduces a conflict' if blocks else
               no_new_label(result))
    expect(page.get_by_role('cell', name=outcome, exact=True)).to_be_visible(timeout=15000)
    if not blocks:
        assert result['no_new_problems']
        workspace = h.shift(workspace, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[])['workspace']
        page.get_by_role('button', name='Refresh checks', exact=True).click()
        expect(page.get_by_role('cell', name='Inputs changed — check again', exact=True)).to_be_visible()
        page.get_by_role('button', name='Check rota', exact=True).click()
        expect(page.get_by_role('cell', name='Waiting to check', exact=True)).to_be_visible()
        job = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs'][0]
        process_one(h.app.state.engine)
        result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
        assert result['no_new_problems']
        outcome = no_new_label(result)
        expect(page.get_by_role('cell', name=outcome, exact=True)).to_be_visible(timeout=15000)
    context.close()
    context, page = sign_in(browser, cp_server, 'approver')
    navigate(page, 'rules')
    page.get_by_role('button', name='Review', exact=True).click()
    expect(page.get_by_role('cell', name=outcome, exact=True)).to_be_visible()
    if not blocks:
        expect(page.get_by_label(
            'Activate even though some affected rotas are not checked or still have problems.',
            exact=True)).not_to_be_visible()
    page.get_by_label('Reason', exact=True).fill('Independent review of FM teaching and clinic cover')
    page.get_by_role('button', name='Activate', exact=True).click()
    if blocks:
        expect(page.get_by_role('alert').filter(has_text='previously feasible rota impossible')).to_be_visible()
        expect(page.get_by_role('dialog')).to_be_visible()
        assert _rule_status(h, workspace, policy) == 'proposed'
    else:
        expect(page.get_by_role('dialog')).not_to_be_visible()
        assert _rule_status(h, workspace, policy) == 'active'
    context.close()


def _rule_status(h, workspace, policy):
    """A rule made after the draft, as the draft reads it: a draft keeps the inputs it was created with until its
    baseline is refreshed."""
    draft = h.refresh_draft_baseline(h.detail(workspace)['workspace'])
    return next(p for p in h.snapshot(draft)['policies'] if p['id'] == policy['id'])['status']


def test_activation_says_a_fresh_check_is_needed_before_the_click(browser, cp_server, h):
    """No fresh rota check: the dialog says so beside Activate, with the check to run, before anyone presses it."""
    workspace = h.shift(h.workspace('FM unchecked rota'), people=[])['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Monday protected teaching', 'owner': 'FM coordinator', 'source': 'Review scenario',
        'starts_on': '2026-01-01', 'definition': {'rule': {'kind': 'restriction', 'weekdays': [0]}}})['record']
    context, page = sign_in(browser, cp_server, 'approver')
    navigate(page, 'rules')
    page.get_by_role('button', name='Review', exact=True).click()
    dialog = page.get_by_role('dialog')
    expect(dialog.get_by_role('cell', name='Not checked', exact=True)).to_be_visible()
    dialog.get_by_label('Reason', exact=True).fill('Independent review of FM teaching')
    # Said before the click, beside the button, with the check to run; Activate waits for it.
    gate = dialog.locator('.rule-gate')
    expect(gate).to_have_text('Activation needs a fresh check of FM unchecked rota first. Check FM unchecked rota')
    expect(dialog.get_by_role('button', name='Activate', exact=True)).to_be_disabled()
    gate.get_by_role('button', name='Check FM unchecked rota', exact=True).click()
    expect(dialog.get_by_role('cell', name='Waiting to check', exact=True)).to_be_visible()
    expect(gate).to_have_text('Checking FM unchecked rota. Activate when it finishes.')
    job = h.get('approver', '/api/jobs?workspace_id=' + workspace['id'])['jobs'][0]
    process_one(h.app.state.engine)
    result = h.get('approver', '/api/jobs/' + job['id'])['job']['result']
    assert result['no_new_problems']
    expect(dialog.get_by_role('cell',
        name=no_new_label(result),
        exact=True)).to_be_visible(timeout=15000)
    expect(dialog.get_by_label(
        'Activate even though some affected rotas are not checked or still have problems.',
        exact=True)).not_to_be_visible()
    dialog.get_by_role('button', name='Activate', exact=True).click()
    expect(page.get_by_role('dialog')).not_to_be_visible()
    assert _rule_status(h, workspace, policy) == 'active'
    context.close()


def test_planner_explicit_incomplete_proposal_cannot_pass_review(browser, cp_server, h):
    h.policy({'kind': 'minimum_cover', 'minimum': 10})
    # The duty type itself needs ten: an open place the summary explains (who could not take it and why).
    typ = h.configure('assignment_types', {'name': 'FM shortage clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 10}]}})['record']['id']
    workspace = h.shift(h.workspace('FM staffing shortage'), typ=typ, people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator')
    navigate(page, 'planner')
    open_scheduling(page)
    open_rota(page, workspace['id'])
    page.get_by_role('button', name='Run auto-schedule', exact=True).click()
    expect(page.locator('#auto-panel').get_by_role('progressbar')).to_be_visible()
    wait_run_created(page)
    process_one(h.app.state.engine)
    _finish(page, h)  # the person chooses Fill everything else when the run hit a conflict
    job = h.get('coordinator', '/api/jobs')['jobs'][0]
    assert job['diagnostic_proposal']
    after = _job(h, workspace)['result']['fix_list_after']['summary']
    review = page.locator('#auto-panel .auto-review')
    # The places it cannot fill stay open and are said as such (never a claim that the rota is ready).
    expect(review.get_by_role('status').first).to_contain_text(f"{after['places_open']} places stay open", timeout=15000)
    expect(review.get_by_role('group', name='Result').locator('.auto-stat').filter(has_text='Must fix')).to_contain_text(str(after['must']))
    assert after['must'] and after['submit_blocked']
    expect(_still(review)).to_have_count(1 if after['must'] else 0)
    page.get_by_role('button', name='Review latest result', exact=True).click()
    page.get_by_role('dialog', name='Auto-schedule results').get_by_role('button', name=APPLY_ALL).click()
    expect(page.get_by_role('dialog')).not_to_be_visible()
    current = h.detail(workspace)['workspace']
    assert not current['validation']['valid']
    h.lifecycle(current, 'submit', status=422)
    assert not h.get('coordinator', '/api/publications')['publications']
    context.close()


def test_shortage_review_keeps_interval_and_does_not_claim_zero_blockers(browser, cp_server, h):
    restriction = h.policy({'kind': 'restriction', 'weekdays': [6]})
    workspace = h.shift(h.workspace('FM Sunday shortage evidence'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator')
    navigate(page, 'planner')
    open_scheduling(page)
    open_rota(page, workspace['id'])
    page.get_by_role('button', name='Run auto-schedule', exact=True).click()
    expect(page.locator('#auto-panel').get_by_role('progressbar')).to_be_visible()
    wait_run_created(page)
    process_one(h.app.state.engine)
    _finish(page, h)
    expect(page.locator('#auto-panel .auto-review').get_by_role('status').first).to_contain_text('1 place stays open', timeout=15000)
    expect(page.get_by_text('0 assignments to add; 0 assignments to remove · No candidate validation available', exact=True)).to_have_count(0)
    page.get_by_role('button', name='Review latest result', exact=True).click()
    review = page.get_by_role('dialog', name='Auto-schedule results', exact=True)
    review.locator('details.auto-run-details > summary').click()
    expect(review.get_by_role('cell', name='Nobody', exact=True)).to_be_visible()
    review.get_by_role('button', name='Who and why', exact=True).click()
    evidence = page.get_by_role('dialog', name='Candidates considered', exact=True)
    expect(evidence.get_by_role('cell', name='See the selected duty', exact=True)).to_have_count(0)
    expect(evidence.get_by_text('4 Oct 2026 · 08:00–12:00', exact=True)).to_be_visible()  # the session's time, said once
    # The reason names its rule in one visible line: no disclosure, ids or revisions.
    rule_name = restriction['name']
    expect(evidence.get_by_text('Active scheduling restriction · ' + rule_name, exact=True).first).to_be_visible()
    expect(evidence.locator('details')).to_have_count(0)
    expect(evidence.get_by_text(re.compile('Rule reference|Revision|Supporting records'))).to_have_count(0)
    context.close()


def test_whole_run_candidates_say_each_session_and_readable_dates(browser, cp_server, h):
    """View candidates considered (the whole run): every row is a person on one session, so each row says the session's
    day and time, the count says checks (not people), and leave and rotations read as dates, never ids or ISO stamps."""
    alex, jordan, _ = h.seed['people']
    h.command('coordinator', 'availability.record', {'person_id': alex, 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-06T00:00:00+03:00', 'private_reason': 'Family event'})
    programme = h.configure('programmes', {'name': 'FM residency', 'starts_on': '2026-01-01'})['record']
    cohort = h.configure('cohorts', {'name': 'Residents', 'programme_id': programme['id'], 'starts_on': '2026-01-01', 'ends_on': '2027-01-01'})['record']
    away = h.configure('rotation_types', {'name': 'External ICU', 'starts_on': '2026-01-01'}, role='coordinator')['record']
    h.configure('programme_memberships', {'person_id': jordan, 'programme_id': programme['id'], 'starts_on': '2026-01-01',
                                          'ends_on': '2027-01-01'}, role='coordinator')  # a rotation needs membership (ROTATION_MEMBERSHIP)
    h.configure('rotations', {'person_id': jordan, 'cohort_id': cohort['id'], 'rotation_type_id': away['id'], 'starts_on': '2026-10-04',
                              'ends_on': '2026-10-11', 'department_available': False}, role='coordinator')
    w = h.shift(h.workspace('FM candidates evidence'), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator')
    open_rota(page, w['id'])
    open_scheduling(page)
    page.get_by_role('button', name='Run auto-schedule', exact=True).click()
    expect(page.locator('#auto-panel').get_by_role('progressbar')).to_be_visible()
    wait_run_created(page)
    process_one(h.app.state.engine)
    page.get_by_role('button', name='Review latest result', exact=True).click(timeout=15000)
    page.get_by_role('dialog', name='Auto-schedule results').locator('details.auto-run-details > summary').click()
    page.get_by_role('dialog', name='Auto-schedule results').get_by_role('button', name='View candidates considered', exact=True).click()
    evidence = page.get_by_role('dialog', name='Candidates considered', exact=True)
    expect(evidence.get_by_text(re.compile(r'^1–6 of 6 person and session checks$'))).to_be_visible()
    expect(evidence.get_by_role('columnheader', name='Session', exact=True)).to_be_visible()
    expect(evidence.get_by_role('cell', name='5 Oct 2026 08:00–12:00').first).to_be_visible()
    expect(evidence.get_by_text('Recorded unavailability · 4 Oct 2026 00:00 – 6 Oct 2026 00:00').first).to_be_visible()
    expect(evidence.get_by_text('External rotation · 4 Oct 2026 – 10 Oct 2026').first).to_be_visible()
    expect(evidence.get_by_text(re.compile(r'\d{4}-\d\d-\d\d|Supporting records|Rule reference'))).to_have_count(0)
    context.close()


def test_auto_schedule_shows_staged_progress_then_what_the_proposal_achieves(browser, cp_server, h):
    from apps.api.db import retry_write
    from apps.worker.main import claim_job, heartbeat_job, finish_job, prepare_deferred_generation
    from packages.domain.cp_sat import generate
    workspace = h.shift(h.workspace('FM progress journey'), people=[])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator')
    navigate(page, 'planner')
    open_scheduling(page)
    open_rota(page, workspace['id'])
    page.get_by_role('button', name='Run auto-schedule', exact=True).click()
    panel = page.locator('#auto-panel')
    expect(panel.get_by_text('Waiting for the scheduler to start', exact=True)).to_be_visible()
    wait_run_created(page)
    # The worker has found a valid schedule and is improving the fairness objective.
    engine = h.app.state.engine
    claim = retry_write(engine, claim_job)
    # A queued run carries its request only; the worker freezes the rota's inputs once it has the run.
    prepare_deferred_generation(engine, claim, engine.get_execution_options()['rota_write_settings'])
    progress = {'stage': 'improving', 'tier': 3, 'elapsed_seconds': 40, 'first_solution_seconds': 6,
                'time_limit_seconds': 300, 'search_started_seconds': 10, 'objective_value': 12, 'best_bound': 4,
                'objective_names': ['targets', 'preferences', 'changes', 'balance']}
    assert retry_write(engine, lambda db: heartbeat_job(db, claim, progress))
    expect(panel.locator('.auto-stage').filter(has_text='3/5 Improving a fair spread of work')).to_be_visible(timeout=15000)
    expect(page.locator('.app-footer').get_by_role('status').filter(has_text='Step 3 of 5: Searching')).to_be_visible()  # the one announcement
    bar = panel.get_by_role('progressbar', name='Auto-schedule progress')
    expect(bar).to_have_attribute('value', '23')  # 15% setup + 75% x 30 s of 300 s searched
    # FD-04: one changing line, no step list or helper sentences.
    expect(panel.locator('.auto-stage')).to_have_text(re.compile(r'^3/5 Improving a fair spread of work · 23% · .+ used · up to .+ left$'))  # left of the allowance, a ceiling
    expect(panel.locator('.auto-progress p')).to_have_count(1)
    result = generate(claim['input'], 30)
    retry_write(engine, lambda db: finish_job(db, claim, result, None))
    card = page.get_by_role('region', name='Review')
    expect(card).to_be_visible(timeout=15000)
    expect(card.get_by_role('status').first).to_contain_text('All 2 places filled')
    expect(card.get_by_role('group', name='Result')).to_contain_text('Changes2')
    context.close()


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}], ids=['desktop', 'phone'])
def test_auto_schedule_steps_review_issues_show_apply_and_undo(browser, cp_server, h, viewport):
    """What to fill → Priorities → Review & apply: each issue says what, which session, who and why, with its fixes."""
    typ = h.configure('assignment_types', {'name': 'FM review clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 10, 'target': 12}]}})['record']['id']
    w = h.shift(h.workspace('FM review journey'), typ=typ, people=[])['workspace']
    # Two sessions at the same time: whoever works one is "already working then" for the other.
    w = h.shift(w, typ=typ, people=[])['workspace']
    # Advisory cover is separate only when mandatory cover is met; headcount gaps group their derived target.
    advisory_type = h.configure('assignment_types', {'name': 'Desired cover review', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'target': 4}]}})['record']['id']
    w = h.shift(w, typ=advisory_type, start='2026-10-05T08:00:00+03:00',
                end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport=viewport)
    if viewport['width'] == 390:
        state = context.storage_state()
        context.close()
        context = browser.new_context(storage_state=state, has_touch=True, viewport=viewport,
                                      locale='en-GB', timezone_id='Asia/Riyadh')
        page = context.new_page()
        page.goto(cp_server)
        expect(page.locator('#main[data-loaded-view]')).to_have_count(1, timeout=30000)
    try:
        navigate(page, 'planner')
        open_scheduling(page)
        open_rota(page, w['id'])
        panel = page.locator('#auto-panel')
        expect(panel.locator('.auto-body')).to_have_attribute('data-state', 'setup')
        auto_setup(panel)  # D76: the setup's fields open under Change
        expect(panel.get_by_label('From', exact=True)).to_have_value('Sun 4 Oct 2026')
        expect(panel.get_by_label('To', exact=True)).to_have_value('Sat 10 Oct 2026')
        # The day count never sits alone on a line behind a dot ("· 7 days"): beside the last day, or alone without the dot.
        count = panel.locator('.date-count')
        expect(count).to_have_text('7 days')
        assert count.evaluate('''c => { const last = c.previousElementSibling.getBoundingClientRect(), r = c.getBoundingClientRect();
            return r.top < last.bottom || getComputedStyle(c, '::before').content === 'none'; }''')
        # A run picks a preset and its own search time; scoring, advisory strengths and wishes are department settings.
        auto_setup(panel, fields=False)
        expect(panel.get_by_role('radio', name='Department defaults')).to_be_checked()
        for label in ('Fairness weight', 'Coverage weight', 'Wish points per person (0 = wishes off)', 'Wish points renew each',
                      'Catch-up for declined wishes (up to ×)', 'Low strength (×)', 'Default for each run'):
            expect(panel.get_by_label(label, exact=True)).to_have_count(0)
        factory = h.get('coordinator', '/api/policies/capabilities')['generation_defaults']
        # The presets come from the contracts (one list); the last one clicked is Fairness first.
        labels = [p['label'] for k, p in GENERATION_WEIGHT_PRESETS.items() if k != 'fairness_first'] + ['Fairness first']
        for label in labels:
            panel.get_by_role('radio', name=label, exact=True).click()
            expect(panel.get_by_role('radio', name=label, exact=True)).to_be_checked()
        expect(panel).not_to_contain_text('set by an admin')  # no tagline (G-05)
        _shot(page, f'run-options-{viewport["width"]}')
        submitted = []
        page.on('request', lambda r: submitted.append(r.post_data_json) if r.url.endswith('/api/commands') and r.method == 'POST' else None)
        expect(panel.locator('.rl-done')).to_contain_text('all clinics')
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.locator('.auto-body')).to_have_attribute('data-state', 'run')
        request = next(r['payload']['generation_options'] for r in submitted if r['kind'] == 'job.create')
        assert request.get('weight_preset') == 'fairness_first' and 'objective_order' not in request
        assert request['mode'] == 'fill_gaps'
        assert not {'weights', 'wish_budget_points', 'wish_period'} & request['scoring'].keys()
        expect(panel.get_by_role('progressbar')).to_be_visible()
        wait_run_created(page)
        process_one(h.app.state.engine)
        _finish(page, h)  # the person chooses Fill everything else when the run hit a conflict
        review = panel.get_by_role('region', name='Review')
        expect(review).to_be_visible(timeout=15000)
        # The job list is deliberately lean; the detail carries the stored comparison scores.
        latest = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]
        job = h.get('coordinator', '/api/jobs/' + latest['id'])['job']
        assert job['status'] == 'succeeded'
        summary = job['result']['summary']
        current_score = h.detail(w)['workspace']['validation']['score']
        assert summary['score_before'] == current_score
        # A7: while anything must be fixed the footer shows the must-fix count, never a standalone score.
        fix = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['summary']
        assert fix['must'] and fix['score'] is None
        expect(page.locator('.app-footer').get_by_role('button', name=re.compile(rf"^{fix['must']} must fix"))).to_be_visible()
        # A3: the run's score change from the rota now, on the footer's (uncapped) measure; always valid as a change.
        result = job['result']
        delta = result['score_delta']
        assert delta == round(result['fix_list_after']['summary']['uncapped_score'] - result['fix_list_before']['summary']['uncapped_score'], 1) > 0
        assert not summary['score']['publishable'] and not summary['score_before']['publishable']
        _change_tile(review, result)
        _shot(page, f"auto-review-score-{viewport['width']}-light", full_page=False)
        expect(panel.locator('.rl-done')).to_have_count(1)  # the run's setup is one line
        run_context = job['run_context']
        assert run_context['basis'] == 'frozen_workspace_and_selected_duties'
        assert run_context['scope_window'] == {'start': w['starts_on'], 'end': w['ends_on']}
        duties = h.detail(w)['shifts']
        assert datetime.fromisoformat(run_context['selection_window']['start']) == min(datetime.fromisoformat(s['start']) for s in duties)
        assert datetime.fromisoformat(run_context['selection_window']['end']) == max(datetime.fromisoformat(s['end']) for s in duties)
        assert set(run_context['service_ids']) == {service for s in duties for service in s['service_ids']}
        queued_payload = next(r['payload'] for r in submitted if r['kind'] == 'job.create')
        assert job.get('generation_scope') == queued_payload.get('generation_scope') is None
        expect(panel.locator('.rl-done')).to_contain_text('duties 4 Oct 2026 – 5 Oct 2026')
        # ME-19: a run over every clinic of the rota says "all clinics" (never a list of every clinic).
        rota_services = {sid for s in duties for sid in s['service_ids']}
        assert set(run_context['service_ids']) == rota_services
        expect(panel.locator('.rl-done')).to_contain_text(' · '.join([
            'all clinics', 'Everyone', 'kept existing assignments', 'Fairness first']))
        frozen_header = panel.locator('.rl-done').inner_text()
        auto_setup(panel)
        panel.get_by_label('Keep existing assignments', exact=True).uncheck()
        auto_setup(panel, fields=False)
        panel.get_by_role('radio', name='Department defaults', exact=True).click()
        auto_result(panel)
        expect(panel.locator('.rl-done')).to_have_text(frozen_header, use_inner_text=True)
        stored_job = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        for key in ('run_context', 'generation_scope', 'effective_options', 'input_fingerprint'):
            assert stored_job.get(key) == job.get(key)
        assert 'objective_order' not in factory
        assert job['effective_options'].get('weight_preset') == 'fairness_first'
        assert 'objective_order' not in job['effective_options']
        assert [step['name'] for step in job['result']['objectives']] == ['coverage', 'weighted']
        # What is still to fix: each must-fix cause the run leaves is ONE Fix list row, in the server's words, with its
        # one action (the shortage of ten in both clinics is one cause, not a card per clinic).
        must = [c for c in result['fix_list_after']['causes'] if c['level'] == 'must']
        assert must
        _expect_still(review, must)
        expect(review.get_by_role('group', name='Result').locator('.auto-stat').filter(has_text='Open places')).to_contain_text(
            str(result['fix_list_after']['summary']['places_open']))
        # Advisory findings never stop Apply and are not decided here (the Fix list's All checks saves an Accept).
        expect(review.get_by_role('button', name=re.compile('^Accept'))).to_have_count(0)
        # Show proposal on grid: the proposal is drawn on the schedule; nothing is saved yet.
        panel.get_by_role('button', name='Show proposal on grid', exact=True).click()
        assert not any(s['participants'] for s in h.detail(w)['shifts'])
        # Reversibility decides whether to apply, so it stays visible next to the button (not in a tooltip).
        expect(panel.locator('.auto-foot')).to_contain_text('You can undo')
        _apply(page, h, panel, re.compile(r'^Apply \d+ changes?$'))
        toast = page.locator('.toast').filter(has_text='Suggestions applied')
        expect(toast).to_be_visible()
        assert any(s['participants'] for s in h.detail(w)['shifts'])
        stored = h.detail(w)['workspace']['validation']['score']
        assert stored['components']['stability']['value'] is None
        assert stored == summary['score']
        after_fix = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['summary']
        expect(page.locator('.app-footer').get_by_role('button', name=re.compile(rf"^{after_fix['must']} must fix"))).to_be_visible()
        assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']['summary'] == summary
        with page.expect_response(lambda response: response.url.endswith('/api/commands')
                and response.request.method == 'POST'
                and response.request.post_data_json.get('kind') == 'workspace.undo'
                and response.request.post_data_json.get('target_id') == w['id']) as undone:
            toast.get_by_role('button', name='Undo', exact=True).click()
        assert undone.value.status == 200
        expect(page.get_by_text('Undone.', exact=False)).to_be_visible()
        assert not any(s['participants'] for s in h.detail(w)['shifts'])
        # The panel no longer says the suggestions are in the draft: undone, the run is a proposal the rota changed under
        # (A4, maintenance), applied again only after a check against the current rota.
        expect(panel.get_by_role('status').filter(has_text='The rota changed since this run · Apply checks it against the current rota first')).to_be_visible()
        expect(panel).not_to_contain_text('Assignments added to draft')
        expect(panel.get_by_role('button', name='Check again and apply', exact=True)).to_be_visible()
        assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']['summary'] == summary
    finally:
        context.close()


@pytest.mark.parametrize('width, elapsed, duration', [(1440, 0.2, '< 1 s'), (390, 2.3, '2 s')])
def test_auto_schedule_empty_review_and_a_refused_range(browser, cp_server, h, width, elapsed, duration):
    w = h.shift(h.workspace('FM nothing needs you'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'admin', {'width': width, 'height': 1000 if width == 1440 else 844})
    def timed_result(route):
        response = route_fetch(route)
        body = response.json()
        job = body['job']
        if job.get('completed_at'):
            start = datetime.fromisoformat(job['started_at'] or job['created_at'])
            job['completed_at'] = (start + timedelta(seconds=elapsed)).isoformat()
        route.fulfill(response=response, json=body)
    page.route(re.compile(r'/api/jobs/[^/?]+$'), timed_result)
    try:
        navigate(page, 'planner')
        open_scheduling(page)
        open_rota(page, w['id'])
        panel = page.locator('#auto-panel')
        auto_setup(panel)  # D76: the setup's fields open under Change
        # Invalid dates cannot start a run; the phone calendar prevents a backwards range.
        if width == 390:
            panel.locator('.date-summary').click()
            calendar = page.get_by_role('dialog', name='Choose dates', exact=True)
            friday = calendar.get_by_role('button', name=re.compile('^Friday 9 October 2026'))
            monday = calendar.get_by_role('button', name=re.compile('^Monday 5 October 2026'))
            friday.click()
            monday.click()
            expect(monday).to_have_attribute('aria-pressed', 'true')
            expect(monday).to_have_attribute('aria-label', 'Monday 5 October 2026, first day')
            expect(friday).to_have_attribute('aria-pressed', 'false')
            expect(calendar.get_by_role('button', name='Use', exact=True)).to_be_disabled()
        else:
            panel.get_by_label('From', exact=True).fill('2026-10-09')
            panel.get_by_label('To', exact=True).fill('2026-10-05')
            # SR-28: Run is disabled with its reason said beside it, before it is pressed.
            expect(panel.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_disabled()
            expect(panel.locator('.auto-why')).to_have_text('The first day must be before the last.')
        assert h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'] == []
        if width == 390:
            calendar.get_by_role('button', name=re.compile('^Sunday 4 October 2026')).click()
            calendar.get_by_role('button', name=re.compile('^Saturday 10 October 2026')).click()
            calendar.get_by_role('button', name='Use 4 – 10 Oct', exact=True).click()
            expect(panel.locator('.date-summary')).to_have_text('Sun 4 Oct → Sat 10 Oct')
        else:
            panel.get_by_label('From', exact=True).fill('2026-10-04')
            panel.get_by_label('To', exact=True).fill('2026-10-10')
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar')).to_be_visible()
        wait_run_created(page)
        process_one(h.app.state.engine)
        review = panel.get_by_role('region', name='Review')
        expect(review.get_by_role('status').first).to_have_text(re.compile(r'^Better: all 1 must-fix cause cleared'), timeout=15000)
        expect(review.get_by_role('region', name='Still to fix')).to_have_count(0)
        # The header names the rota only; the outcome box owns the result (G-19).
        expect(panel.locator('.auto-head small')).to_have_text(w['name'])
        expect(panel.get_by_role('button', name='Apply 1 change', exact=True)).to_be_enabled()
        if width == 390:
            panel.scroll_into_view_if_needed()
            page.wait_for_function("""() => parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--topbar-height')) ===
                document.querySelector('.topbar').getBoundingClientRect().height""")
            expect(panel.get_by_role('button', name='Close auto-schedule', exact=True)).to_be_in_viewport(ratio=1)
            assert panel.locator('.auto-foot .hint').evaluate("""hint => {
                const text=document.createRange();text.selectNodeContents(hint);
                return [...text.getClientRects()].length === 1;
            }""")  # Undo availability remains readable on one line.
            panel.locator('.auto-foot').evaluate("""foot => window.scrollBy(0,Math.max(0,
                foot.getBoundingClientRect().bottom-document.querySelector('.app-footer').getBoundingClientRect().top+12))""")
            assert panel.locator('.auto-foot').evaluate("""foot => foot.getBoundingClientRect().bottom <=
                document.querySelector('.app-footer').getBoundingClientRect().top""")
        panel.get_by_role('button', name='Review latest result', exact=True).click()
        details = page.get_by_role('dialog', name='Auto-schedule results', exact=True)
        details.locator('details.auto-run-details > summary').click()
        expect(details.locator('.run-phases')).not_to_contain_text('0.0s')
        expect(details.locator('.run-phases')).to_contain_text('< 1 s')
        details.get_by_role('button', name='Close dialog', exact=True).click()
        panel.get_by_role('button', name='Discard', exact=True).click()
        panel.get_by_role('button', name='Discard?', exact=True).click()
        expect(panel.get_by_text('Proposal discarded · nothing was applied', exact=True)).to_be_visible()
        assert not h.detail(w)['shifts'][0]['participants']
    finally:
        context.close()


def _review(page, h, w):
    """Runs auto-schedule up to Review & apply. When the run hit rules that cannot all be met, the person chooses Fill
    everything else (_finish), so the worker runs once more."""
    panel = page.locator('#auto-panel')
    panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
    expect(panel.get_by_role('progressbar')).to_be_visible()
    wait_run_created(page)
    process_one(h.app.state.engine)
    _finish(page, h)
    review = panel.get_by_role('region', name='Review')
    expect(review).to_be_visible(timeout=15000)
    return review


def _change_tile(review, result):
    """The result's third number (A3): the score change from the rota now, or, for a proven clash of mandatory rules
    (where no score is fair to show), the number of changes, which Apply names too."""
    tiles = review.get_by_role('group', name='Result').locator('.auto-stat')
    conflict = result.get('conflict') or next((d for d in result.get('diagnostics') or [] if d.get('strict_status') == 'INFEASIBLE'), None)
    if (conflict or {}).get('strict_status') == 'INFEASIBLE' and result['generation_outcome']['candidate'] != 'valid':
        expect(tiles.filter(has_text='Score')).to_have_count(0)
        changes = review.page.locator('#auto-panel').get_by_role('button', name=re.compile(r'^Apply (\d+) changes?$')).inner_text().split(' ')[1]
        expect(tiles.filter(has_text='Changes')).to_have_text(re.compile(rf'^Changes\s*{changes}$'))
        return tiles.filter(has_text='Changes')
    delta = result['score_delta']
    expect(tiles.filter(has_text='Score')).to_have_text(re.compile(r'^Score\s*' + re.escape(f"{'+' if delta > 0 else '−' if delta < 0 else '±'}{abs(delta):g}") + r'\s*vs now$'))
    return tiles.filter(has_text='Score')


def _joins(page):
    """The page's job requests from now on: a job.create (coalesced onto the queued check) or a read of a job. A page that
    already knows the check maintenance queued after Apply joins it by reading its status (D76), with no job.create."""
    joined = []
    page.on('request', lambda r: joined.append(r) if '/api/jobs/' in r.url
            or r.url.endswith('/api/commands') and (r.post_data_json or {}).get('kind') == 'job.create' else None)
    return joined


def _check_after_apply(page, h, workspace_id, joined):
    """The worker's part after Apply: when a complete check is queued, wait until the page has joined it (so it sees it
    finish, instead of later queueing a second one nobody runs here), then let the worker take every queued job."""
    check = next((j for j in h.get('coordinator', '/api/jobs?workspace_id=' + workspace_id)['jobs']
                  if j['kind'] == 'validation' and j['status'] == 'queued'), None)
    if check:
        wait_until(page, lambda: any('/api/jobs/' + check['id'] in r.url or '/api/jobs/' not in r.url for r in joined),
                   'the page never joined the queued rota check')
        while process_one(h.app.state.engine):
            pass


def _apply(page, h, panel, name):
    """Apply from the panel, then the worker's part (_check_after_apply)."""
    joined = _joins(page)
    with page.expect_response(lambda r: r.url.endswith('/api/commands')
                              and (r.request.post_data_json or {}).get('kind') == 'generation.apply') as reply:
        panel.get_by_role('button', name=name, exact=True).click()
    _check_after_apply(page, h, reply.value.json()['workspace']['id'], joined)


def _job(h, w, kind='generation'):
    """The rota's latest job of `kind`, read in full (its Fix lists before and after the run)."""
    latest = next(j for j in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'] if j['kind'] == kind)
    return h.get('coordinator', '/api/jobs/' + latest['id'])['job']


def _still(review):
    """Still to fix: the one next step (D76), the top must-fix cause the run leaves as its Fix list row (A3)."""
    return review.get_by_role('region', name='Still to fix').locator('li[data-cause]')


def _expect_still(review, must):
    """D76: one row, the top cause; any others counted with the way to the Fix list."""
    expect(_still(review)).to_have_count(1)
    expect(_still(review)).to_contain_text(must[0]['sentence'])
    more = review.get_by_role('button', name=re.compile(r'^\d+ more causes? in the Fix list$'))
    expect(more).to_have_count(1 if len(must) > 1 else 0)
    if len(must) > 1:
        expect(more).to_have_text(f"{len(must) - 1} more cause{'s' if len(must) > 2 else ''} in the Fix list")


def _finish(page, h):
    """After a run is processed: when it hit rules that cannot all be met (requirements_conflict), the person chooses
    Fill everything else and that run is processed too; nothing goes on by itself (on_conflict 'report').

    The choice is read from the processed run, not from what the panel shows at one moment. With the run's saved
    preparation (resume_available) the press is job.continue on that run (no dialog: the press is the choice); without
    one, a new run with every break flagged."""
    panel = page.locator('#auto-panel')
    latest = h.get('coordinator', '/api/jobs')['jobs'][0]
    job = h.get('coordinator', '/api/jobs/' + latest['id'])['job']
    result = job.get('result') or {}
    if job['kind'] == 'generation' and not job.get('diagnostic_proposal') and result.get('status') == 'requirements_conflict':
        fill = panel.get_by_role('button', name='Fill everything else', exact=True)
        expect(fill).to_have_count(1, timeout=15000)  # one way on: the clash card's primary or the footer's
        assert h.get('coordinator', '/api/jobs')['jobs'][0]['id'] == job['id']  # nothing went on by itself
        if result.get('resume_available'):
            with page.expect_request(lambda r: r.url.endswith('/api/commands') and (r.post_data_json or {}).get('kind') == 'job.continue') as sent:
                fill.click()
            assert sent.value.post_data_json['target_id'] == job['id']
            assert sent.value.post_data_json['payload']['reason'] == 'Chosen: Fill everything else (flag every break)'
        else:
            with page.expect_request(lambda r: r.url.endswith('/api/commands') and (r.post_data_json or {}).get('kind') == 'job.create') as sent:
                fill.click()
            payload = sent.value.post_data_json['payload']
            assert payload['diagnostic_proposal'] is True and payload['generation_options']['on_conflict'] == 'continue_flagged'
        wait_run_created(page)
        process_one(h.app.state.engine)
    expect(panel.locator('.auto-outcome').first).to_be_visible(timeout=15000)


def test_auto_schedule_use_suggested_person_and_allow_an_exception(browser, cp_server, h):
    """The fixes a run leaves are the Fix list's (A3, D9): each must-fix cause is ONE row with the server's one action
    (a capped clinic and a one-duty-a-day limit each say what to do), nothing is approved or excepted from the result,
    and Apply keeps every cap."""
    typ = h.configure('assignment_types', {'name': 'FM capped clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 3}]}})['record']['id']
    # At most two on this duty: the third person is free (no rule of their own stops them), the place stays open.
    h.policy({'kind': 'maximum_participants', 'maximum': 2}, scope={'assignment_type_id': typ})
    # One duty a day, exceptions allowed: on the second session the others are stopped by a rule that can be excepted.
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'day'}, exception=True)
    pair = h.configure('assignment_types', {'name': 'FM pair clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2}]}})['record']['id']
    w = h.shift(h.workspace('FM one-click fixes'), typ=typ, people=[])['workspace']
    w = h.shift(w, typ=pair, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    w = h.shift(w, typ=pair, start='2026-10-05T13:00:00+03:00', end='2026-10-05T17:00:00+03:00', people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_scheduling(page)
        open_rota(page, w['id'])
        review = _review(page, h, w)
        labels = {'autoschedule_scoped': 'Auto-schedule these', 'clash': 'See the clash', 'open_line': 'Change the rule…',
                  'review_targets': 'Review targets', 'rebalance': 'Rebalance', 'choose_cover': 'Choose cover',
                  'adjust_rota': 'Allow for this rota…', 'add_sessions': 'Add sessions', 'open_eligibility': 'Who can work it'}
        must = [c for c in _job(h, w)['result']['fix_list_after']['causes'] if c['level'] == 'must']
        assert must
        _expect_still(review, must)  # D76: the top cause is the one next step, with its one action
        expect(_still(review).get_by_role('button', name=labels[must[0]['action']['kind']], exact=True)).to_be_visible()
        assert not h.detail(w)['workspace'].get('rule_adjustments')  # reading the result changed no rule
        panel = page.locator('#auto-panel')
        _apply(page, h, panel, re.compile(r'^Apply \d+ changes?$'))
        expect(page.locator('.toast').filter(has_text='Suggestions applied')).to_be_visible()
        shift = next(s for s in h.detail(w)['shifts'] if s['assignment_type_id'] == typ)
        assert len(shift['participants']) == 2  # the cap holds: the third place stays open
    finally:
        context.close()


def test_auto_schedule_review_in_dark_mode(browser, cp_server, h):
    w = h.shift(h.workspace('FM dark review'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator')
    legacy_results = []
    def older_result(route):
        """A result stored before results carried their Fix lists (maintenance) and score_before: the earlier layout."""
        response = route_fetch(route)
        body = response.json()
        job = body['job']
        result = job.get('result') or {}
        summary = result.get('summary', {})
        if summary.pop('score_before', None) is not None:
            for key in ('fix_list_before', 'fix_list_after', 'score_delta', 'open_places_before', 'open_places_after', 'changes_by_cause'):
                result.pop(key, None)
            legacy_results.append(job['id'])
        route.fulfill(response=response, json=body)
    page.route(re.compile(r'/api/jobs/[^/?]+$'), older_result)
    try:
        page.emulate_media(color_scheme='dark')
        navigate(page, 'planner')
        open_scheduling(page)
        open_rota(page, w['id'])
        review = _review(page, h, w)
        expect(review.get_by_role('status').first).to_contain_text(re.compile(r'All \d+ places? filled'))
        # An older result still shows its stored run score but has no invented delta against today's footer.
        assert legacy_results
        job = h.get('coordinator', '/api/jobs/' + legacy_results[-1])['job']
        score = review.locator('.auto-stat').filter(has=page.get_by_role('button', name='About score', exact=True))
        expect(score.locator('b')).to_have_text(f"{job['result']['summary']['score']['score']:g}")
        expect(score.locator('.score-delta')).to_have_count(0)
        expect(score).not_to_contain_text('from')
        # Dark surfaces: the panel and its tiles are not left white; text stays light on them.
        colours = page.locator('#auto-panel .auto-stat').first.evaluate("""e => {const s=getComputedStyle(e);
            const lum=c=>{const [r,g,b]=c.match(/\\d+/g).map(Number);return (r*299+g*587+b*114)/1000;};
            return [lum(s.backgroundColor), lum(getComputedStyle(e.querySelector('b')).color)];}""")
        assert colours[0] < 80 < colours[1], colours
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_unchanged_advisory_candidate_shows_equal_before_after_scores(browser, cp_server, h, width, scheme):
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, severity='advisory')
    w = h.shift(h.workspace('Unchanged advisory comparison'))['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        auto_setup(panel)
        panel.get_by_label('Keep existing assignments', exact=True).check()
        review = _review(page, h, w)
        latest = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]
        job = h.get('coordinator', '/api/jobs/' + latest['id'])['job']
        summary = job['result']['summary']
        assert job['result']['additions'] == job['result']['removals'] == []
        expect(review.get_by_role('status').first).to_contain_text('Nothing to change')
        expect(panel.get_by_role('button', name=re.compile(r'^Apply'))).to_have_count(0)
        assert summary['score_before'] == summary['score']
        assert 0 < summary['score']['score'] < 100
        # A3: the score as a change between the two versions; nothing moved, so ±0 (never a second, invented number).
        assert job['result']['score_delta'] == 0
        score = review.get_by_role('group', name='Result').locator('.auto-stat').filter(has_text='Score')
        expect(score).to_have_text(re.compile(r'^Score\s*±0\s*vs now$'))
        expect(score.locator('.score-delta')).to_have_count(0)
        score.scroll_into_view_if_needed()
        _shot(page, f'unchanged-auto-review-score-{width}-{scheme}', full_page=False)
        panel.get_by_role('button', name='Review latest result', exact=True).click()
        history = page.get_by_role('dialog', name='Auto-schedule results', exact=True)
        expect(history.locator('.auto-stat').filter(has_text='Score')).to_have_text(re.compile(r'^Score\s*±0\s*vs now$'))
        expect(history.locator('.score-delta')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('role', ['approver', 'credentialer', 'auditor', 'staff'])
def test_auto_schedule_is_only_for_people_who_edit_drafts(browser, cp_server, h, role):
    w = h.shift(h.workspace('FM roles auto-schedule'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, role)
    try:
        navigate(page, 'home')
        page.goto(cp_server + '/#schedule')  # a role without the Schedule lands where it may be
        expect(page.locator('.topbar')).to_be_visible()
        expect(page.get_by_role('button', name='Auto-schedule', exact=True)).to_have_count(0)
        expect(page.locator('#auto-panel')).not_to_be_visible()
        assert w['id']
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_capped_proposal_shows_footer_score_and_legacy_delta_through_apply_and_undo(browser, cp_server, h, width, scheme):
    typ = h.configure('assignment_types', {'name': 'Four required places', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 4, 'maximum': 4}]}})['record']['id']
    w = h.workspace('Capped proposal score')
    for day in range(4, 8):
        w = h.shift(w, typ=typ, start=f'2026-10-{day:02}T08:00:00+03:00',
            end=f'2026-10-{day:02}T12:00:00+03:00', people=h.seed['people'] if day < 7 else [])['workspace']
    before_score = h.get('coordinator', '/api/workspaces/' + w['id'] + '/summary')['score']
    before = before_score['uncapped_score']
    assert before_score['score'] == 59 < before
    context, page = sign_in(browser, cp_server, 'coordinator', {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        open_rota(page, w['id'])
        open_scheduling(page)
        review = _review(page, h, w)
        job = _job(h, w)
        summary = job['result']['summary']
        saved = summary['score_saved']
        assert summary['score_before'] == before_score
        assert saved['score'] == 59 < saved['uncapped_score'] and not saved['publishable']
        # A3/A7: the result says the run's change on the uncapped measure (capped at 59 both times, the change is still
        # real); no standalone score while anything must be fixed.
        assert job['result']['score_delta'] == round(saved['uncapped_score'] - before, 1) > 0
        _change_tile(review, job['result']).scroll_into_view_if_needed()
        _shot(page, f'capped-proposal-score-{width}-{scheme}', full_page=False)
        must = lambda: h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['summary']['must']
        footer = page.locator('.app-footer')
        expect(footer.get_by_role('button', name=re.compile(rf'^{must()} must fix'))).to_be_visible()
        _apply(page, h, page.locator('#auto-panel'), re.compile(r'^Apply \d+ changes?$'))
        toast = page.locator('.toast').filter(has_text='Suggestions applied')
        expect(toast).to_be_visible()
        after = h.get('coordinator', '/api/workspaces/' + w['id'] + '/summary')['score']
        assert after == saved
        expect(footer.get_by_role('button', name=re.compile(rf'^{must()} must fix'))).to_be_visible()
        # Owner, 7 Nov 2026: the score shows while anything must be fixed too, capped (the must-fix pill gives the count).
        expect(footer.locator('.score-pill .score-blocking')).to_have_text('· capped')
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Undone.', exact=False)).to_be_visible()
        assert h.get('coordinator', '/api/workspaces/' + w['id'] + '/summary')['score']['uncapped_score'] == before
        expect(footer.get_by_role('button', name=re.compile(rf'^{must()} must fix'))).to_be_visible()
    finally:
        # The panel keeps polling the job: stop intercepting (ignoring a fetch still in flight) before closing.
        page.unroute_all(behavior='ignoreErrors')
        context.close()

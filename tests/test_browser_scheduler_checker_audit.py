"""Expanded scheduler/checker audit: desired behavior, intentionally red on the audited source.

These cases exercise existing public controls and real checks. Request holds isolate pending/stale presentation;
the scoped result comes from the real CP-SAT worker, never a fabricated success or a weakened validator.
"""
import json
import re
from dataclasses import replace

import pytest
from playwright.sync_api import expect

from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services.jobs import create_job
from apps.worker.main import process_one
from packages.contracts.schemas import Command
from packages.domain.cp_sat import generate
from .test_browser import auto_result, wait_run_created, row_action, route_fetch, browser, live_server, navigate, open_rota, open_rule, open_scheduling, planner_tool, sign_in  # noqa: F401
from .test_browser_cp_sat import cp_server, _joins, _check_after_apply  # noqa: F401
from .test_browser_rule_catalogue import catalogue_rule, flag, line, set_numbers, shot
from .rules_support import rule_action, rule_row


pytestmark = pytest.mark.browser
CHECK = '**/api/rules/catalogue/duties_per_week/check'


def proposed_line(page, h):
    page.locator('.topbar').get_by_role('button', name=re.compile(r'^Propose \d+ changes?$')).click()
    dialog = page.get_by_role('dialog', name=re.compile('^Propose'))
    expect(dialog).to_be_visible()
    dialog.get_by_label('Reason', exact=True).fill('Independent checker audit')
    dialog.get_by_role('button', name='Propose', exact=True).click()
    expect(page.locator('#main .rc-waiting')).to_be_visible()
    entry = next(e for e in h.get('approver', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_week')
    return entry['proposed']['definition']['rule']['lines'][0]


@pytest.mark.parametrize('role', ['coordinator', 'auditor'])
@pytest.mark.parametrize('width,scheme', [(390, 'light'), (1440, 'dark')])
def test_catalogue_number_strengths_remain_readable_in_line_panel(browser, live_server, h, role, width, scheme):
    """K1f numbers: one row Minimum | Maximum | Target, each strength a "!" chip beside its field (a target is always
    advisory: no chip); only the bound that has a number offers None."""
    flag(h)
    record = catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}})])
    line_id = record['definition']['rule']['lines'][0]['id']
    context, page = sign_in(browser, live_server, role, {'width': width, 'height': 900})
    editable = role == 'coordinator'
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules/duties_per_week/' + line_id)
        panel = page.locator('.rc-line-panel')
        numbers = panel.locator('.rc-lp-numbers')
        expect(numbers).to_be_visible()
        tops = []
        for label, value in [('Minimum', ''), ('Maximum', '5'), ('Target', '')]:
            number = numbers.get_by_label(label, exact=True)
            expect(number).to_have_attribute('type', 'number')
            expect(number).to_have_value(value)
            assert number.evaluate('e => e.readOnly') is not editable
            tops.append(number.evaluate('e => e.getBoundingClientRect().top'))
        # The strength reads in words beside its number; an empty number's strength waits (editable) or is absent.
        chip = numbers.get_by_role('button', name='Maximum: mandatory', exact=True)
        expect(chip).to_have_text('Mandatory')
        expect(chip).to_have_attribute('data-level', 'mandatory')
        expect(chip).to_be_enabled() if editable else expect(chip).to_be_disabled()
        expect(numbers.locator('.rc-strength.is-idle')).to_have_count(2 if editable else 0)
        expect(numbers.locator('.rc-strength:not(.is-idle)')).to_have_count(1)
        if width >= 720:
            assert max(tops) - min(tops) < 2, tops  # one row
        none = numbers.get_by_role('button', name='None', exact=True)
        if editable:
            expect(none).to_have_count(1)
            expect(numbers.get_by_label('Maximum', exact=True).locator('xpath=../..').get_by_role('button', name='None', exact=True)).to_have_count(1)
            expect(none).to_have_attribute('aria-pressed', 'false')
            # The chip opens the shared menu of every strength, the current one ticked; a target is never mandatory.
            numbers.get_by_role('button', name='Maximum: mandatory', exact=True).click()
            menu = page.get_by_role('menu', name='Maximum strength')
            expect(menu.get_by_role('menuitemradio')).to_have_text(['Mandatory', 'Advisory · critical', 'Advisory · high', 'Advisory · medium', 'Advisory · low'])
            expect(menu.get_by_role('menuitemradio', name='Mandatory')).to_have_attribute('aria-checked', 'true')
            menu.get_by_role('menuitemradio', name='Advisory · high').click()
            expect(menu).to_have_count(0)
            expect(numbers.get_by_role('button', name='Maximum: advisory · high', exact=True)).to_have_attribute('data-tip', 'Advisory · high')
            numbers.get_by_label('Target', exact=True).fill('3')  # a strength shows once its number is there
            numbers.get_by_role('button', name='Target: advisory · medium', exact=True).click()
            expect(page.get_by_role('menu', name='Target strength').get_by_role('menuitemradio', name='Mandatory')).to_have_count(0)
            page.keyboard.press('Escape')
            panel.get_by_role('button', name='Keep change', exact=True).click()
            expect(numbers.get_by_role('button', name='Maximum: advisory · high', exact=True)).to_be_visible()
        else:
            expect(none).to_have_count(0)
            expect(panel.get_by_role('button', name='Keep change', exact=True)).to_have_count(0)
        assert numbers.evaluate('e => e.scrollWidth <= e.clientWidth + 1')
        numbers.scroll_into_view_if_needed()
        shot(page, f'rule-number-strengths-{role}-{width}-{scheme}')
        if editable and width >= 720:  # the chosen advisory level is what is proposed (a phone's sheet covers the notice)
            panel.get_by_role('button', name='Close panel', exact=True).click()
            assert proposed_line(page, h)['numbers']['max'] == {'value': 5, 'level': 'high'}
    finally:
        context.close()


def test_read_only_explicit_none_remains_visible_in_number_details(browser, live_server, h):
    flag(h)
    record = catalogue_rule(h, 'duties_per_week', [line({'max': {'value': None, 'level': 'mandatory'}})])
    context, page = sign_in(browser, live_server, 'auditor', {'width': 390, 'height': 900})
    try:
        navigate(page, 'rules/duties_per_week/' + record['definition']['rule']['lines'][0]['id'])
        panel = page.locator('.rc-line-panel')
        expect(panel.get_by_label('Maximum', exact=True)).to_be_disabled()
        expect(panel.get_by_label('Maximum', exact=True)).to_have_value('')
        # An explicit None has no number, so no strength to read (rc.13 QA): the pressed None is the fact shown.
        expect(panel.locator('.rc-strength')).to_have_count(0)
        none = panel.get_by_role('button', name='None', exact=True)
        expect(none).to_be_visible()
        expect(none).to_be_disabled()
        expect(none).to_have_attribute('aria-pressed', 'true')
        expect(panel.get_by_role('button', name='Keep change', exact=True)).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('target_first', [False, True])
def test_note_only_edit_preserves_each_number_classification(browser, live_server, h, target_first):
    """A note must neither make the target mandatory nor weaken a mandatory bound."""
    flag(h)
    bounds = {'min': {'value': 1, 'level': 'mandatory'}, 'max': {'value': 5, 'level': 'mandatory'}}
    target = {'target': {'value': 3, 'level': 'medium'}}
    numbers = {**target, **bounds} if target_first else {**bounds, **target}
    record = catalogue_rule(h, 'duties_per_week', [line(numbers, note='Original note')])
    original = record['definition']['rule']['lines'][0]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week/' + original['id'])
        panel = page.locator('.rc-line-panel')
        panel.get_by_label('Note', exact=True).fill('Only the note changed')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        saved = proposed_line(page, h)
        assert saved['note'] == 'Only the note changed'
        assert saved['numbers'] == original['numbers']
    finally:
        context.close()



def test_note_only_edit_preserves_inherited_and_nondefault_advisory_strengths(browser, live_server, h):
    flag(h)
    numbers = {'min': {'value': 1, 'level': None}, 'max': {'value': 5, 'level': 'low'}, 'target': {'value': 3, 'level': 'high'}}
    record = catalogue_rule(h, 'duties_per_week', [line(numbers, note='Existing strengths')])
    original = record['definition']['rule']['lines'][0]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week/' + original['id'])
        panel = page.locator('.rc-line-panel')
        panel.get_by_label('Note', exact=True).fill('Strengths kept')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        assert proposed_line(page, h)['numbers'] == original['numbers']
    finally:
        context.close()

def test_bulk_new_target_defaults_to_advisory_without_weakening_bounds(browser, live_server, h):
    flag(h)
    numbers = {'min': {'value': 1, 'level': 'mandatory'}, 'max': {'value': 5, 'level': 'mandatory'}}
    catalogue_rule(h, 'duties_per_week', [line(numbers, note='Bounds remain mandatory')])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        page.locator('#main tbody tr[data-row] input.bulk-check').check()
        set_numbers(page, target=3)  # Keep existing classifications; a new target is necessarily advisory.
        saved = proposed_line(page, h)
        assert saved['numbers']['min'] == numbers['min']
        assert saved['numbers']['max'] == numbers['max']
        assert saved['numbers']['target'] == {'value': 3, 'level': 'medium'}
    finally:
        context.close()


@pytest.mark.parametrize('outcome', ['pending', 'failure'])
def test_unfinished_catalogue_check_never_reports_a_pass(browser, live_server, h, outcome):
    flag(h)
    record = catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}})])
    line_id = record['definition']['rule']['lines'][0]['id']
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    try:
        page.route(CHECK, lambda route: held.append(route))
        with page.expect_request(CHECK):
            navigate(page, 'rules/duties_per_week/' + line_id)
        panel = page.locator('.rc-line-panel')
        expect(panel).to_be_visible()
        if outcome == 'failure':
            held[0].fulfill(status=503, json={'error': {'code': 'UNAVAILABLE', 'message': 'Check service unavailable'}})
            expect(page.locator('.rc-checkbar')).to_contain_text('could not be checked')
        expect(panel).not_to_contain_text('✓ No duplicate or overlap')
    finally:
        context.close()


def test_old_catalogue_response_cannot_attach_to_values_changed_before_debounce(browser, live_server, h):
    """An obsolete check is delivered late (checks are shared between their callers and never aborted, f12
    rule_catalogue.ts checkLines): the version guard alone keeps its answer off the newer values."""
    outcome = 'delivered_late'
    flag(h)
    catalogue_rule(h, 'duties_per_week', [line({'min': {'value': 1, 'level': 'mandatory'},
                                               'max': {'value': 5, 'level': 'mandatory'}})])
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    try:
        navigate(page, 'rules/duties_per_week')
        # Exercise cancellation and the independent version guard with real server results. Only the obsolete
        # check's signal resists abort in the late-delivery case; unrelated requests retain normal cancellation.
        page.evaluate('''late => {
            const fetch = window.fetch, abort = AbortController.prototype.abort, held = new WeakSet();
            AbortController.prototype.abort = function(...args) {
                if (held.has(this.signal)) { document.documentElement.dataset.checkerAbortAttempted = 'yes'; return; }
                return abort.apply(this, args);
            };
            window.fetch = async (...args) => {
                const check = String(args[0]).endsWith('/api/rules/catalogue/duties_per_week/check');
                const maximum = check ? JSON.parse(args[1].body).lines[0].numbers.max.value : null;
                if (late && check && maximum === -1 && args[1].signal)  // checks carry no signal since f12 (shared)
                    held.add(args[1].signal);
                const response = await fetch(...args);
                if (check) {
                    // api.ts request() reads the body as text and parses it itself.
                    const text = response.text.bind(response);
                    response.text = async () => {
                        const payload = await text(), channel = new MessageChannel();
                        // A new task runs after the API and checker promise continuations, without moving the clock.
                        channel.port1.onmessage = () => {
                            document.documentElement.dataset.checkerBodyMax = String(maximum);
                            channel.port1.close(); channel.port2.close();
                        };
                        channel.port2.postMessage(null);
                        return payload;
                    };
                }
                return response;
            };
        }''', outcome == 'delivered_late')
        page.clock.pause_at(page.evaluate('new Date(Date.now()+1000).toISOString()'))
        def hold(route):
            # The page's opening check of the saved rule (max 5) can still be in flight on a loaded runner: not an edit's.
            if route.request.post_data_json['lines'][0]['numbers']['max']['value'] == 5:
                return route.fallback()
            held.append((route, route_fetch(route)))
            # A request event precedes both interception and route.fetch completing.
            page.evaluate('(count) => document.documentElement.dataset.checkerResponses = String(count)', len(held))

        page.route(CHECK, hold)

        def maximum(value):
            page.locator('#main tbody tr[data-row]').get_by_label(re.compile('^Max: ')).click()
            control = page.get_by_role('dialog', name='Edit Max').get_by_label('Max', exact=True)
            control.fill(str(value))
            control.press('Enter')

        maximum(-1)  # A negative maximum is refused; its response must not annotate the repaired max 6.
        page.clock.run_for(300)  # the edit's own check may ask first; the page's debounced check shares it
        expect(page.locator('html')).to_have_attribute('data-checker-responses', '1')
        route, response = held[0]
        assert route.request.post_data_json['lines'][0]['numbers']['max']['value'] == -1
        assert response.json()['issues']
        maximum(6)
        with page.expect_request_finished(lambda request: request == route.request):
            route.fulfill(response=response)
        expect(page.locator('html')).to_have_attribute('data-checker-body-max', '-1')
        expect(page.locator('#main .rc-checkbar')).not_to_contain_text('refused')
        expect(page.locator('#main .rc-checkbar')).not_to_contain_text('rule issue')
        page.clock.run_for(300)
        expect(page.locator('html')).to_have_attribute('data-checker-responses', '2')
        current, response = held[-1]
        assert current.request.post_data_json['lines'][0]['numbers']['max']['value'] == 6
        assert response.json()['issues'] == []
        with page.expect_request_finished(lambda request: request == current.request):
            current.fulfill(response=response)
        expect(page.locator('html')).to_have_attribute('data-checker-body-max', '6')
        expect(page.locator('#main .rc-checkbar')).to_be_empty()
    finally:
        context.close()


def test_rule_what_if_edits_the_visible_condition_tree_without_hidden_original_scope(browser, cp_server, h):
    """The existing shared builder must expose the actual scope used in this read-only trial."""
    flag(h, False)
    conditions = {'match': 'all', 'conditions': [{'field': 'service_ids', 'op': 'any', 'values': [h.seed['services'][0]]}]}
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Condition-based trial rule', 'owner': 'Audit', 'source': 'Synthetic browser fixture',
        'starts_on': '2026-01-01', 'definition': {'scope': {'conditions': conditions},
                                              'rule': {'kind': 'max_hours', 'hours': 1, 'window': 'day'}}})['record']
    workspace = h.shift(h.workspace('Condition trial rota'), people=[])['workspace']
    before = h.snapshot(workspace)
    context, page = sign_in(browser, cp_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.locator('.rule-item').filter(has_text=policy['name']).locator('.rl-actions [aria-label^="More for"]').click()
        page.get_by_role('menuitem', name='Check rule against rotas', exact=True).click()
        page.get_by_role('button', name='Check a change', exact=True).click()
        trial = page.get_by_role('dialog', name='Check a change to ' + policy['name'], exact=True)
        builder = trial.locator('.cond-root')
        expect(builder).to_be_visible()
        expect(builder.get_by_label('Filter on', exact=True)).to_have_value('service_ids')
        row_action(builder.locator('.cond-row').first, 'Remove')
        trial.get_by_label('Hours', exact=True).fill('4')
        with page.expect_request(lambda r: r.url.endswith('/api/commands') and (r.post_data_json or {}).get('kind') == 'job.create') as queued:
            trial.get_by_role('button', name='Check this change', exact=True).click()
        scope = queued.value.post_data_json['payload']['rule_changes'][0]['definition']['scope']
        assert not scope.get('conditions') and not scope.get('service_ids') and not scope.get('service_id')
        expect(page.get_by_role('button', name='View change check', exact=True)).to_be_visible()
        assert h.snapshot(workspace) == before
    finally:
        context.close()


def test_scoped_no_change_candidate_never_claims_the_whole_rota_is_ready(browser, cp_server, h):
    h.policy({'kind': 'min_rest', 'hours': 12})
    workspace = h.shift(h.workspace('Scoped readiness audit'))['workspace']
    workspace = h.shift(workspace, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00')['workspace']
    workspace = h.shift(workspace, start='2026-10-10T08:00:00+03:00', end='2026-10-10T12:00:00+03:00')['workspace']
    selected = max(h.detail(workspace)['shifts'], key=lambda shift: shift['start'])
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=workspace['revision'], payload={
            'workspace_id': workspace['id'], 'kind': 'generation', 'session_ids': [selected['id']],
            'generation_options': {'mode': 'fill_gaps'}}), replace(h.settings, generation_engine='cp_sat'))['job']
    h.process_jobs(scheduler=lambda snapshot, **options: generate(snapshot, time_budget_seconds=5, workers=1, **options))
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert result['status'] == 'candidate_with_gaps', result
    assert not result['additions'] and not result['removals']
    assert not result['validation']['valid']
    assert {finding['code'] for finding in result['outside_scope_issues']} == {'MIN_REST'}
    context, page = sign_in(browser, cp_server, 'coordinator')
    try:
        open_rota(page, workspace['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        expect(panel.get_by_role('button', name='Review latest result', exact=True)).to_be_visible()
        expect(panel).to_contain_text('Outside these sessions')
        expect(panel.get_by_text('Current rota is ready for review', exact=True)).to_have_count(0)
        expect(panel).not_to_contain_text('No issues: every place filled')
        assert not h.detail(workspace)['workspace']['validation']['valid']
    finally:
        context.close()


@pytest.mark.parametrize('change', ['inputs', 'archive', 'input_compiler', 'result_compiler'])
def test_historical_candidate_stays_read_only_when_its_rota_inputs_change(browser, cp_server, h, change):
    workspace = h.shift(h.workspace('Historical candidate audit'), people=[])['workspace']
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=workspace['revision'], payload={
            'workspace_id': workspace['id'], 'kind': 'generation',
            'generation_options': {'mode': 'fill_gaps'}}), replace(h.settings, generation_engine='cp_sat'))['job']
    h.process_jobs(scheduler=lambda snapshot, **options: generate(snapshot, time_budget_seconds=5, workers=1, **options))
    completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert completed['status'] == 'succeeded' and completed['result']['additions']
    if change == 'inputs':
        workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    elif change == 'archive':
        workspace = h.command('coordinator', 'workspace.archive', {'reason': 'Keep this run as history'},
                              workspace['id'], workspace['revision'])['workspace']
        assert workspace['archived_at']
    else:
        from apps.api import models as m
        with transaction(h.app.state.engine, write=True) as db:
            stored = db.get(m.Job, job['id'])
            if change == 'input_compiler':
                stored.input_snapshot = {**stored.input_snapshot, 'compiler_version': 'older-version'}
            else:
                stored.result = {**stored.result, 'algorithm_version': 'older-version'}
    before = h.snapshot(workspace)
    assert (h.detail(workspace)['current_fingerprint'] != completed['input_fingerprint']) == (change == 'inputs')
    context, page = sign_in(browser, cp_server, 'coordinator')
    try:
        open_rota(page, workspace['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        auto_result(panel)
        if change == 'inputs':
            # A4/BE-1 (268b5e8ef): a proposal the rota changed under stays reviewable, and Apply checks it against the
            # current rota first; it is never applied unchecked.
            expect(panel.locator('.auto-stale')).to_have_text('The rota changed since this run · Apply checks it against the current rota first')
            expect(panel.get_by_role('button', name='Check again and apply', exact=True)).to_be_visible()
            expect(panel.get_by_role('button', name=re.compile(r'^Apply \d+ changes?$'))).to_have_count(0)
        elif change.endswith('_compiler'):
            # Apply refuses a changed scheduler (STALE_SCHEDULER), so nothing here may offer a re-check.
            expect(panel).to_contain_text('Scheduler changed · run auto-schedule again')
            expect(panel.get_by_role('button', name=re.compile(r'^(Apply|Check again and apply)'))).to_have_count(0)
            expect(panel).not_to_contain_text('The rota changed since this run')
            expect(panel.get_by_role('button', name='Show proposal on grid', exact=True)).to_have_count(0)
        panel.get_by_role('button', name='Review latest result', exact=True).click()
        result = page.get_by_role('dialog', name='Auto-schedule results', exact=True)
        expect(result).to_be_visible()
        expect(result.get_by_role('columnheader', name='Person', exact=True).last).to_be_visible()
        expect(result.get_by_role('button', name=re.compile(r'^Apply (the change|all \d+ changes)$'))).to_have_count(0)
        if change == 'inputs':
            # The dialog names the same checked Apply as the panel.
            expect(result.get_by_role('button', name='Check again and apply', exact=True)).to_be_visible()
        else:
            expect(result.get_by_role('button', name=re.compile(r'^(Apply chosen|Check again and apply)'))).to_have_count(0)
        expect(result).to_contain_text({'inputs': 'The rota changed since this run', 'archive': 'This rota is archived'}.get(change, 'The scheduler changed since this run'))
        expect(panel).not_to_contain_text('Proposal ready · a better one may exist')
        expect(panel).not_to_contain_text('Best proposal for these priorities')
        expect(result).not_to_contain_text('Proposal ready · a better one may exist')
        expect(result).not_to_contain_text('Best proposal for these priorities')
        if change.endswith('_compiler'):
            rejected = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
                'job_id': job['id'], 'reason': 'Old scheduler result cannot change current assignments'},
                revision=workspace['revision'], status=409)
            assert rejected['error']['code'] == 'STALE_SCHEDULER'
        expect(result.get_by_role('button', name='Open Fix list', exact=True)).to_have_count(0)
        assert h.snapshot(workspace) == before
        assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'
    finally:
        context.close()


@pytest.mark.parametrize('status,validated,valid,diagnostic,proven,wording', [
    ('invalid_inputs', False, False, False, False, ''),
    ('translation_error', True, True, False, False, ''),
    ('requirements_conflict', False, False, False, False, ''),
    ('unknown_time_limit', False, False, False, False, ''),
    ('unknown_time_limit', True, True, False, False, ''),
    ('candidate_with_gaps', True, False, True, False, ''),
    ('candidate_feasible', True, False, False, False, ''),
    ('candidate_feasible', True, True, False, False, 'Proposal ready · a better one may exist'),
    ('candidate_feasible', True, True, False, True, 'Best proposal for these priorities'),
])
def test_run_details_only_claim_a_schedule_when_the_result_has_a_candidate(browser, cp_server, h, status, validated, valid, diagnostic, proven, wording):
    """Synthetic stored outcome shapes isolate wording; this is not solver qualification."""
    workspace = h.shift(h.workspace('Stored outcome wording'), people=[])['workspace']
    before = h.snapshot(workspace)
    context, page = sign_in(browser, cp_server, 'coordinator')
    try:
        open_rota(page, workspace['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        with page.expect_response(lambda response: response.url.endswith('/api/commands')
                and response.request.method == 'POST'
                and response.request.post_data_json.get('kind') == 'job.create'
                and response.request.post_data_json.get('payload', {}).get('workspace_id') == workspace['id']) as queued:
            panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        assert queued.value.ok, queued.value.text()
        expect(panel.get_by_role('progressbar')).to_be_visible()
        wait_run_created(page)
        process_one(h.app.state.engine, scheduler=lambda snapshot, **options: generate(snapshot, time_budget_seconds=5, workers=1, **options))
        latest = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs'][0]

        def outcome(route):
            response = route_fetch(route)
            body = response.json()
            job = body['job']
            result = job['result']
            result.update(status=status, optimality_proven=proven, diagnostic_proposal=diagnostic)
            job['diagnostic_proposal'] = diagnostic
            if not validated:
                result.pop('validation', None)
            else:
                result['validation']['valid'] = valid
            if not wording:
                result['additions'], result['removals'] = [], []
            route.fulfill(response=response, json=body)

        page.route('**/api/jobs/' + latest['id'], outcome)
        page.reload()
        open_rota(page, workspace['id'])
        open_scheduling(page)
        auto_result(panel)
        if wording:
            expect(panel.get_by_role('status').filter(has_text=wording)).to_be_visible()
        else:
            expect(panel).not_to_contain_text('Proposal ready · a better one may exist')
            expect(panel).not_to_contain_text('Best proposal for these priorities')
        expect(panel).not_to_contain_text('Best schedule')
        panel.get_by_role('button', name='Review latest result', exact=True).click(timeout=15000)
        dialog = page.get_by_role('dialog', name='Auto-schedule results', exact=True)
        expect(dialog).to_be_visible()
        # The words may end with why the search stopped ("… · search time used up": autoschedule_result.ts searchEnded).
        expect(dialog.get_by_text(re.compile(r'^Proposal ready · a better one may exist( · .+)?$'))).to_have_count(int(wording == 'Proposal ready · a better one may exist'))
        expect(dialog.get_by_text('Best proposal for these priorities', exact=True)).to_have_count(int(wording == 'Best proposal for these priorities'))
        expect(dialog).not_to_contain_text('Best schedule')
        if not wording:
            expect(dialog.get_by_role('button', name=re.compile(r'^Apply'))).to_have_count(0)
        if status == 'unknown_time_limit' and not validated:
            expect(dialog).to_contain_text('No valid proposal was found within the search time')
        assert h.snapshot(workspace) == before
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
@pytest.mark.parametrize('change', ['replacement', 'leave_retracted_while_open'])
def test_stored_conflicts_open_current_issues_without_editing_from_old_findings(browser, cp_server, h, width, scheme, change):
    from copy import deepcopy
    from apps.api import models as m

    person = h.seed['people'][0]
    private_reason = 'Synthetic annual leave for historical conflict review'
    leave = h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave',
        'leave_type': 'Annual leave', 'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00',
        'private_reason': private_reason})['record']
    workspace = h.shift(h.workspace('Historical conflict audit'), people=[person])['workspace']
    original = h.detail(workspace)['shifts'][0]
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=workspace['revision'], payload={
            'workspace_id': workspace['id'], 'kind': 'generation',
            'generation_options': {'mode': 'fill_gaps'}}), replace(h.settings, generation_engine='cp_sat'))['job']
    h.process_jobs(scheduler=lambda snapshot, **options: generate(snapshot, time_budget_seconds=5, workers=1, **options))
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Job, job['id'])
        recorded_input = deepcopy(stored.input_snapshot)
        recorded_result = deepcopy(stored.result)
    completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert completed['status'] == 'succeeded' and completed['result']['status'] == 'candidate_with_gaps'
    assert not completed['result']['validation']['valid']
    assert not completed['result']['optimality_proven']
    assert not completed['result']['removals']  # fill_gaps keeps the conflicting manual assignment.
    finding = next(f for f in completed['result']['issues'] if f['code'] == 'UNAVAILABLE')
    assert finding['protected']
    assert finding['shift_id'] == original['id'] and finding['person_id'] == person
    context, page = sign_in(browser, cp_server, 'coordinator', {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        open_rota(page, workspace['id'])
        open_scheduling(page)
        page.locator('#auto-panel').get_by_role('button', name='Review latest result', exact=True).click()
        dialog = page.get_by_role('dialog', name='Auto-schedule results', exact=True)
        expect(dialog).to_contain_text(finding['leave']['type'])
        expect(dialog).to_contain_text('4 Oct 2026 08:00–12:00')
        expect(dialog).not_to_contain_text(private_reason)
        expect(dialog.get_by_role('button', name=re.compile(r'^(Pick someone else|Remove .* from this session|Allow an exception)'))).to_have_count(0)
        dialog.get_by_role('button', name=re.compile(r'^Open all in Fix list \(\d+\)$')).click()
        if width > 400:  # a desk keeps the rota and opens the Fix list beside it; a phone opens its page (openIssues)
            expect(page.get_by_role('region', name='Fix list', exact=True)).to_be_visible()
        else:
            expect(page).to_have_url(re.compile(r'#schedule/issues\?draft='))
        # The per-check table (the Fix list's All checks view) lists the current finding.
        navigate(page, f"schedule/issues?draft={workspace['id']}&view=checks")
        expect(page.locator('.issues-head')).to_be_visible()
        expect(page.locator('.issues-list')).to_contain_text('Annual')
        expect(page.locator('.issues-list').get_by_role('checkbox').first).to_be_visible()
        page.get_by_role('button', name='Back to schedule', exact=True).click()
        expect(page.locator('.issues-head')).to_have_count(0)
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        auto_result(panel)
        panel.get_by_role('button', name='Review latest result', exact=True).click()
        expect(dialog).to_be_visible()
        if change == 'replacement':
            page.keyboard.press('Escape')
            expect(dialog).to_have_count(0)
            # Same session/person IDs, but a different assignment for valid work on a new day.
            start, end = '2026-10-08T08:00:00+03:00', '2026-10-08T12:00:00+03:00'
            workspace = h.command('coordinator', 'shift.update', {'workspace_id': workspace['id'],
                'shift_id': original['id'], 'assignment_type_id': original['assignment_type_id'],
                'location_id': original['location_id'], 'service_ids': original['service_ids'],
                'attributes': original['attributes'], 'start': start, 'end': end,
                'participants': [{'person_id': person, 'start': start, 'end': end}]}, revision=workspace['revision'])['workspace']
            changed = h.detail(workspace)
            assert changed['workspace']['validation']['valid']
            assert changed['shifts'][0]['participants'][0]['id'] != original['participants'][0]['id']
            page.reload()
            open_rota(page, workspace['id'])
            open_scheduling(page)
            panel = page.locator('#auto-panel')
            auto_result(panel)
            panel.get_by_role('button', name='Review latest result', exact=True).click()
            expect(dialog).to_contain_text('The rota changed since this run')
            expect(dialog).not_to_contain_text('8 Oct 2026')
        else:
            # Availability can change while the dialog stays open without changing the rota revision.
            h.command('coordinator', 'availability.retract', {'reason': 'Leave cancelled'}, leave['id'], leave['revision'])
            changed = h.detail(workspace)
            assert changed['workspace']['revision'] == workspace['revision']
            assert any(flag['availability_id'] == leave['id'] for flag in original['double_booking_conflicts'])
            assert not changed['shifts'][0].get('double_booking_conflicts')
            # Current leave flags are derived; retracting leave must not edit stored session fields.
            assert [{k: v for k, v in shift.items() if k != 'double_booking_conflicts'} for shift in changed['shifts']] == [
                {k: v for k, v in original.items() if k != 'double_booking_conflicts'}]
        assert changed['current_fingerprint'] != completed['input_fingerprint']
        before = h.snapshot(workspace)
        # BE-1 (268b5e8ef, docs/design/scoped-job-inputs.md; TEST-REPAIR "never refused as stale 409"): Apply
        # re-checks a proposal against the current rota and writes only what the current check allows. The historical
        # findings never authorize an edit: the receipt is computed from the current inputs.
        expect(dialog).to_contain_text(finding['leave']['type'])
        expect(dialog).to_contain_text('4 Oct 2026 08:00–12:00')
        expect(dialog).not_to_contain_text(private_reason)
        expect(dialog.get_by_role('button', name=re.compile(r'^(Pick someone else|Remove .* from this session|Allow an exception)'))).to_have_count(0)
        expect(dialog.get_by_role('button', name='Details', exact=True)).to_have_count(0)
        expect(dialog.get_by_role('button', name='Resolve all in one place', exact=True)).to_have_count(0)
        shot(page, f'historical-conflicts-{change}-{width}-{scheme}', full_page=False)
        if change == 'replacement':
            # The session the proposal fills changed under it: nothing is written, the placement is a content conflict.
            # The dialog stays open and says so where Apply was, with the next step; nothing is left to apply.
            with page.expect_response(lambda response: response.url.endswith('/api/commands')
                    and (response.request.post_data_json or {}).get('kind') == 'generation.apply') as applied:
                dialog.get_by_role('button', name='Check again and apply', exact=True).click()
            assert applied.value.status == 200, applied.value.text()
            reply = applied.value.json()
            expect(dialog).to_be_visible()
            expect(dialog.get_by_role('status').filter(has_text='Nothing was applied')).to_have_text(
                'Nothing was appliedNone of its changes fit the rota as it is now. Run auto-schedule again for the current rota.')
            expect(dialog.get_by_role('button', name=re.compile(r'^(Apply|Check again and apply)'))).to_have_count(0)
        else:
            # An already-open dialog retains its suggestion; applying it is checked against the current server inputs
            # (the leave is gone, so the proposal's own addition now fits beside the kept manual assignment).
            joined = _joins(page)
            with page.expect_response(lambda response: response.url.endswith('/api/commands')
                    and (response.request.post_data_json or {}).get('kind') == 'generation.apply') as applied:
                dialog.get_by_role('button', name=re.compile(r'^(Apply (the change|all \d+ changes)|Check again and apply)$')).click()
            assert applied.value.status == 200, applied.value.text()
            reply = applied.value.json()
            expect(dialog).to_have_count(0)
        receipt = reply['reconciliation']
        assert receipt['source_fingerprint'] == completed['input_fingerprint']
        assert receipt['current_fingerprint'] == changed['current_fingerprint'] != completed['input_fingerprint']
        assert private_reason not in str(reply)
        if change == 'replacement':
            assert receipt['status'] == 'content_conflict', receipt
            assert not receipt['addition_indexes'] and not receipt['removal_indexes']
            assert [(c['code'], c['shift_id']) for c in receipt['conflicting_placements']] == [('CONTENT_CONFLICT', original['id'])]
            assert reply['workspace']['revision'] == workspace['revision']
            assert h.snapshot(workspace) == before
        else:
            assert receipt['status'] == 'applied' and receipt['addition_indexes'] == [0] and receipt['validation_current'], receipt
            assert any(f['kind'] == 'availability' and f['id'] == leave['id'] for f in receipt['changed_facts']), receipt['changed_facts']
            assert reply['validation']['valid']
            # Exactly the proposal's addition was written; the manual assignment and the session are kept as they are.
            session = h.detail(workspace)['shifts'][0]
            added = [p for p in session['participants'] if p['source'] == 'auto_schedule']
            assert [(p['person_id'], p['source_run_id']) for p in added] == [
                (completed['result']['additions'][0]['participant']['person_id'], job['id'])]
            assert [p for p in session['participants'] if p['source'] != 'auto_schedule'] == changed['shifts'][0]['participants']
        fresh_result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
        # Leave disclosure follows current availability; the recorded solver evidence stays immutable (the receipt and
        # the applied marker are separate from it).
        separate = ('reconciliation', 'applied', 'applied_content_revision')
        receipts = ('issues', 'validation', *separate)
        assert (fresh_result['reconciliation']['id'], fresh_result['reconciliation']['status']) == (receipt['id'], receipt['status'])
        # The read discloses leave as availability stands now: compare the evidence without it; a retracted leave is
        # named nowhere in the fresh read.
        undisclosed = lambda x: ({k: undisclosed(v) for k, v in x.items() if k != 'leave'} if isinstance(x, dict)
                                 else [undisclosed(v) for v in x] if isinstance(x, list) else x)
        if change != 'replacement':
            assert '"leave"' not in json.dumps({k: v for k, v in fresh_result.items() if k not in receipts})
        assert undisclosed({k: v for k, v in fresh_result.items() if k not in receipts}) == undisclosed({
            k: v for k, v in completed['result'].items() if k not in receipts})
        assert {k: v for k, v in fresh_result['validation'].items() if k != 'findings'} == {
            k: v for k, v in completed['result']['validation'].items() if k != 'findings'}
        assert [{k: v for k, v in finding.items() if k != 'leave'} for finding in fresh_result['validation']['findings']] == [
            {k: v for k, v in finding.items() if k != 'leave'} for finding in completed['result']['validation']['findings']]
        assert [{k: v for k, v in issue.items() if k != 'leave'} for issue in fresh_result['issues']] == [
            {k: v for k, v in issue.items() if k != 'leave'} for issue in completed['result']['issues']]
        fresh_finding = next(f for f in fresh_result['issues'] if f['code'] == 'UNAVAILABLE')
        if change == 'leave_retracted_while_open':
            assert 'leave' not in fresh_finding
            assert all('leave' not in finding for finding in fresh_result['validation']['findings'])
        else:
            assert fresh_finding['leave'] == finding['leave']
        assert private_reason not in str(fresh_result)
        with transaction(h.app.state.engine) as db:
            stored = db.get(m.Job, job['id'])
            assert stored.input_snapshot == recorded_input
            assert {k: v for k, v in stored.result.items() if k not in separate} == recorded_result
        if change == 'replacement':
            dialog.get_by_role('button', name=re.compile(r'^Open all in Fix list \(\d+\)$')).click()
            if width > 600:  # a desktop opens the Fix list beside its planner (d413c4189, app.ts openIssues)
                expect(page.get_by_role('region', name='Fix list', exact=True)).to_be_visible()
                expect(page).not_to_have_url(re.compile(r'#schedule/issues'))
            else:
                expect(page).to_have_url(re.compile(r'#schedule/issues\?draft='))
        else:
            # BE-18: Apply queues the complete check of the current rota; once the worker has run it, the stored check is
            # the current rota's, so the Fix list needs no new check.
            _check_after_apply(page, h, workspace['id'], joined)
            current = h.detail(workspace)
            assert current['workspace']['validation']['fingerprint'] == current['current_fingerprint']
        navigate(page, f"schedule/issues?draft={workspace['id']}&view=checks")
        expect(page.locator('.issues-head')).to_be_visible()
        expect(page.locator('.issues-counts')).not_to_contain_text('needs checking')
        expect(page.locator('.issues-list')).not_to_contain_text('Annual')
        assert h.detail(workspace)['workspace']['validation']['valid']
        if change == 'replacement':
            assert h.detail(workspace)['shifts'] == changed['shifts']
    finally:
        context.close()



def test_replacement_selection_requires_both_halves_of_the_same_assignment(browser, cp_server, h):
    from .test_template_scheduler_audit import lead_proposal
    workspace, job, candidate = lead_proposal(h)
    before = h.snapshot(workspace)
    context, page = sign_in(browser, cp_server, 'coordinator')
    try:
        open_rota(page, workspace['id'])
        open_scheduling(page)
        page.locator('#auto-panel').get_by_role('button', name='Review latest result', exact=True).click()
        result = page.get_by_role('dialog', name='Auto-schedule results', exact=True)
        removal = result.locator('[data-table="schedule-removals"] tbody tr[data-row] input.bulk-check')
        removal.uncheck()
        expect(result.get_by_role('button', name='Apply chosen (1 of 2)', exact=True)).to_be_disabled()
        expect(result.get_by_text('Select the matching replacement removal too, or deselect its new assignment.', exact=True)).to_be_visible()
        expect(result.get_by_role('button', name=re.compile(r'^Apply (the change|all \d+ changes)$'))).to_be_enabled()
        assert h.snapshot(workspace) == before
        removal.check()
        expect(result.get_by_text('Select the matching replacement removal too, or deselect its new assignment.', exact=True)).to_have_count(0)
        result.get_by_role('button', name=re.compile(r'^Apply (the change|all \d+ changes)$')).click()
        expect(result).to_have_count(0)
        after = h.detail(workspace)
        lead = candidate['additions'][0]['participant']['person_id']
        people = after['shifts'][0]['participants']
        assert len(people) == 2 and next(p for p in people if p['person_id'] == lead)['team_lead']
    finally:
        context.close()


@pytest.mark.parametrize('historical_mode', ['off', 'set'])
def test_historical_safety_flag_cannot_offer_exceptions_or_weaker_rota_adjustments(browser, live_server, h, historical_mode):
    from apps.api import models as m
    rest = h.policy({'kind': 'min_rest', 'hours': 12})
    hours = h.policy({'kind': 'max_hours', 'hours': 40, 'window': 'week'}, exception=True)
    # Historical storage may predate the safety contract. Read it without legitimising its unsafe flag.
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Policy, rest['id'])
        stored.definition = {**stored.definition, 'exception_allowed': True}
    workspace = h.shift(h.workspace('Safety controls audit'))['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, workspace['id']).rule_adjustments = [{'policy_id': rest['id'], 'policy_revision': rest['revision'],
            'mode': historical_mode, 'amount': 8 if historical_mode == 'set' else None, 'by': None, 'at': None}]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        sheet = open_rule(page, rest['name'])
        expect(sheet).to_contain_text('Not allowed')
        expect(sheet).not_to_contain_text('Allowed with approval')
        sheet.get_by_role('button', name='Close panel', exact=True).click()
        # Request exception lives in the Rules page's Exceptions panel (8d703991a).
        page.get_by_role('button', name=re.compile(r'^Exceptions( \(\d+ pending\))?$')).click()
        page.get_by_role('region', name='Exceptions', exact=True).get_by_role('button', name='Request exception', exact=True).click()
        dialog = page.get_by_role('dialog', name='Request a rule exception', exact=True)
        choices = dialog.get_by_label('Exception-permitting rule', exact=True).locator('option')
        assert rest['name'] not in choices.all_text_contents()
        assert hours['name'] in choices.all_text_contents()
        dialog.get_by_role('button', name='Close dialog', exact=True).click()
        open_rota(page, workspace['id'])
        planner_tool(page, 'Rules in this rota')
        context_rules = page.get_by_role('dialog', name='Rules in this rota', exact=True)
        context_row = rule_row(context_rules, rest['name'])
        context_row.locator('.rk-row-head').click()
        expect(context_row).to_contain_text('The department rule applies. The older adjustment is not applied.')
        expect(context_row).not_to_contain_text('Switched off for this rota')
        rule_action(context_row, 'Make stricter for this rota')
        context_row.get_by_label('Justification', exact=True).fill('Agreed with the department for this rota')
        context_row.get_by_role('button', name='Back to the department rule', exact=True).click()
        expect(context_row).not_to_contain_text('older adjustment')
        assert not h.detail(workspace)['workspace']['rule_adjustments']
        # The same contextual entry point permits stricter numbers but never Off or weaker safety limits.
        if not context_row.get_by_role('form', name='Make stricter for this rota').count():
            rule_action(context_row, 'Make stricter for this rota')
        expect(context_row.get_by_role('button', name='Switch off for this rota', exact=True)).to_have_count(0)
        context_number = context_row.get_by_role('spinbutton')
        context_number.fill('8')
        context_row.get_by_role('button', name='Use for this rota', exact=True).click()
        assert not context_number.evaluate('(input) => input.checkValidity()')
        assert not h.detail(workspace)['workspace']['rule_adjustments']
        # The same panel then makes it stricter for this rota, with its justification, and takes it back.
        context_number.fill('14')
        context_row.get_by_label('Justification', exact=True).fill('Agreed with the department for this rota')
        context_row.get_by_role('button', name='Use for this rota', exact=True).click()
        row = rule_row(context_rules, rest['name'])
        expect(row.locator('.rk-row-head')).to_contain_text('Changed for this rota.')
        expect(row).to_contain_text('This rota uses 14 h instead of 12')
        assert h.detail(workspace)['workspace']['rule_adjustments'][0]['amount'] == 14
        row.get_by_label('Justification', exact=True).fill('Back to the department rule')
        row.get_by_role('button', name='Back to the department rule', exact=True).click()
        expect(context_rules.locator('.rk-row-head').filter(has_text='Changed for this rota')).to_have_count(0)
        assert not h.detail(workspace)['workspace']['rule_adjustments']
    finally:
        context.close()

"""Prepared R1–R7 journeys for f417db1e; collection is not runtime qualification.

Ordinary commands and the basic worker produce real proposals/receipts in the fictional database.
Cause/date presentation fixtures transform reads only; no simulated result is applied.
Future N1–N7 contracts belong in the private matrix until their generated types land.
"""
import copy
import csv
import io
import re

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one
from packages.contracts.schemas import FindingCauseGroup, ROLE_NAMES
from .test_browser import (browser, live_server, sign_in, navigate, open_rota, open_scheduling, auto_setup, auto_result,
                           open_session, form_submit, planner_tool, pick_view, route_fetch,
                           holding, wait_held, expect_loaded)  # noqa: F401
from .test_browser_cp_sat import cp_server  # noqa: F401
from .test_browser_autoschedule_live import seen_done
from .test_browser_finding_causes import breach_rota, open_cause
from .test_browser_export_display import format_as
from .test_browser_rule_catalogue import flag, line, weekly
from .test_browser_rule_catalogue_details import propose_off
from .test_rule_catalogue_api import propose
from .test_generation_population import create as create_population_job, scope as population_scope
from .test_template_scheduler_audit import use_cp_sat
from .test_browser_source_parity import person_name

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 1000}
READY = 'Auto-schedule proposal ready · review it'


@pytest.fixture
def waiting_proposal(h):
    """A retained roster and an empty later duty, completed by the ordinary basic worker."""
    w = h.shift(h.workspace('Fictional repair proposal'), people=h.seed['people'][:1])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_options': {'mode': 'fill_gaps'}}, revision=w['revision'])['job']
    assert job['generation_engine'] == 'legacy', 'This UI fixture must not start CP-SAT'
    assert process_one(h.app.state.engine)
    job = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert job['status'] == 'succeeded' and job['outcome'] in ('complete', 'partial')
    assert not job['result_stale'] and job['result']['additions']
    return w, job


def test_same_session_requirements_are_one_fix_list_cause_without_losing_rules(browser, live_server, h):
    """D9: two rules short on the same session are one decision on the Fix list; its ▾ names both rules."""
    rules = [h.policy({'kind': 'minimum_cover', 'minimum': 1}) for _ in range(2)]
    # A duty type with no headcount of its own: the two rules are the session's only requirement (a rule-less gap is
    # its own cause, by design).
    rule_only = h.configure('assignment_types', {'name': 'Fictional rule-only clinic', 'starts_on': '2020-01-01',
                                                 'definition': {'requirements': []}})['record']['id']
    w = h.shift(h.workspace('Fictional same-task requirements'), typ=rule_only, people=[])['workspace']
    fix = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")
    assert fix['summary']['must'] == 1
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, f"schedule/issues?draft={w['id']}")
        rows = page.locator('#main section[data-level=must] .fix-list > li[data-cause]')
        expect(rows).to_have_count(1)
        rows.first.get_by_role('button', name='Show evidence', exact=True).click()
        for rule in rules:
            expect(rows.first.locator('.fix-detail')).to_contain_text(rule['name'])
    finally:
        context.close()


def test_same_cause_and_exact_repair_tasks_merge_without_losing_rules(browser, live_server, h):
    rules = [h.policy({'kind': 'minimum_cover', 'minimum': 1}) for _ in range(2)]
    w = h.shift(h.workspace('Fictional same-task requirements'), people=[])['workspace']
    summary = h.get('coordinator', f"/api/workspaces/{w['id']}/summary")
    assert len(summary['repair_tasks']) == 1
    assert {r['id'] for r in rules} <= {f['rule_id'] for f in summary['findings']}
    assert set(summary['repair_tasks'][0]['finding_keys']) == {f['key'] for f in summary['findings']}
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_rota(page, w['id'])
        pop = open_cause(page, w['id'], len(h.detail(w)['workspace']['validation']['findings']), 'unfilled')
        card = pop.locator('.issue-card[data-cause=unfilled]')
        expect(card).to_have_count(1)
        expect(card).to_contain_text('1 session:')
        for rule in rules:
            expect(card).to_contain_text(rule['name'])
        expect(card).to_contain_text(f"Who ({len(summary['findings'])} findings)")
        expect(card.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_visible()
    finally:
        context.close()


def test_independent_causes_under_one_parent_keep_both_details_and_actions(browser, live_server, h):
    """Read-only presentation fixture: two server cause groups refer to the same grouped issue keys."""
    _, rule, w = breach_rota(h)
    summary = h.get('coordinator', f"/api/workspaces/{w['id']}/summary")
    first = summary['cause_groups'][0]
    second = copy.deepcopy(first)
    second.update(key='fictional-independent-eligibility-cause', code='no_eligible_people')
    second['fix_hints'] = [{**first['fix_hints'][0], 'action': 'open_eligibility'}]
    second = FindingCauseGroup.model_validate(second).model_dump(mode='json')
    summary['cause_groups'] = [first, second]
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        # The page reads the cause groups as /summary?view=causes (app.ts rotaSummary): match it with its query.
        page.route(f"**/api/workspaces/{w['id']}/summary*", lambda route: route.fulfill(json=summary))
        open_rota(page, w['id'])
        pop = open_cause(page, w['id'], len(h.detail(w)['workspace']['validation']['findings']), 'no_candidate_sessions')
        missing = pop.locator('.issue-card[data-cause=no_candidate_sessions]')
        eligibility = pop.locator('.issue-card[data-cause=no_eligible_people]')
        for card in (missing, eligibility):
            expect(card).to_have_count(1)
            expect(card).to_contain_text('2 people:')
            expect(card).to_contain_text('Who (2 findings)')
            expect(card).to_contain_text(rule['name'])
        expect(missing.get_by_role('button', name=re.compile(r'^Add .+ sessions to this rota$'))).to_be_visible()
        eligibility.get_by_role('button', name='Open People', exact=True).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'people')
    finally:
        context.close()


@pytest.mark.parametrize('role,scheme', [('coordinator', 'light'), ('admin', 'dark')])
def test_waiting_proposal_is_single_on_schedule_resolve_and_submit(browser, live_server, h, waiting_proposal, role, scheme):
    w, job = waiting_proposal
    context, page = sign_in(browser, live_server, role, viewport=DESKTOP)
    try:
        page.emulate_media(color_scheme=scheme)
        open_rota(page, w['id'])
        navigate(page, 'home')
        home_row = page.locator('.home-month').filter(has_text=w['name'])
        expect(home_row).to_contain_text('proposal ready')
        home_row.get_by_role('button', name='Review the auto-schedule proposal for ' + w['name'], exact=True).click()
        expect(page.locator('#auto-panel').get_by_role('region', name='Review')).to_be_visible()
        open_rota(page, w['id'])
        expect(page.get_by_role('button', name='Auto-schedule · proposal ready', exact=True)).to_be_visible()
        expect(page.locator('.app-footer').get_by_role('button', name=READY, exact=True)).to_have_count(1)
        # The Fix list says the waiting proposal once: its footer pill, beside the must-fix counts.
        navigate(page, f"schedule/issues?draft={w['id']}")
        expect(page.locator('#main .fix-page')).to_be_visible()
        expect(page.locator('.app-footer').get_by_role('button', name=READY, exact=True)).to_have_count(1)
        # Mockup A1: the Fix list's planner bar names the rota; its switcher opens the rota's schedule.
        page.get_by_role('button', name=f"Switch draft · {w['name']}", exact=True).click()
        page.get_by_role('button', name='Open its schedule', exact=True).click()
        page.get_by_role('button', name='Submit for review…', exact=True).click()
        dialog = page.get_by_role('dialog', name=re.compile('^Submit'))
        expect(dialog.locator('.proposal-note')).to_have_count(1)
        dialog.get_by_role('button', name='Review the proposal', exact=True).click()
        panel = page.locator('#auto-panel')
        expect(panel.get_by_role('region', name='Review')).to_be_visible()
        expect(page).to_have_url(re.compile(rf'edit={w["id"]}'))
        assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'
    finally:
        context.close()


def test_cancel_edit_guard_preserves_form_and_hidden_roster_then_edit_stales_history(browser, live_server, h, waiting_proposal):
    w, job = waiting_proposal
    before = h.detail(w)
    staffed = next(s for s in before['shifts'] if s['participants'])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    commands = []
    try:
        open_rota(page, w['id'])
        expect(page.get_by_role('button', name=READY, exact=True)).to_be_visible()
        open_session(page, staffed['type_snapshot']['name'])
        page.locator('.wk-panel').get_by_role('button', name='Edit session', exact=True).click()
        editor = page.get_by_role('region', name='Edit session and participants', exact=True)
        editor.get_by_label('Session end', exact=True).fill('2026-10-04T13:00')
        roster = editor.get_by_label('Participant', exact=True)
        expect(roster).to_have_value(h.seed['people'][0])
        expect(editor.get_by_text('Not saved yet: review what this changes', exact=False)).to_be_visible()
        # The roster step may be off-screen; save/cancel must keep its native picker values as well as visible fields.
        values = editor.locator('form').evaluate("f=>[...f.elements].filter(e=>e.matches('input,select,textarea')).map(e=>[e.id,e.value,e.checked])")
        page.on('request', lambda r: commands.append(r.post_data_json) if r.url.endswith('/api/commands') else None)
        form_submit(editor).click()
        guard = page.get_by_role('dialog', name='Edit the draft now?', exact=True)
        expect(guard).to_be_visible()
        expect(guard).to_contain_text('checked again before you apply it')
        guard.get_by_role('button', name='Close dialog', exact=True).click()  # cancel without accepting the edit
        expect(guard).to_have_count(0)
        expect(form_submit(editor)).to_be_enabled()
        assert commands == [], 'Cancelling the guard must send no command'
        assert h.detail(w)['shifts'] == before['shifts']
        assert editor.locator('form').evaluate("f=>[...f.elements].filter(e=>e.matches('input,select,textarea')).map(e=>[e.id,e.value,e.checked])") == values
        form_submit(editor).click()
        guard.get_by_role('button', name='Edit anyway', exact=True).click()
        expect(editor).to_have_count(0)
        saved = h.detail(w)
        edited = next(s for s in saved['shifts'] if s['id'] == staffed['id'])
        assert edited['participants'][0]['person_id'] == h.seed['people'][0]
        assert edited['end'] != staffed['end']
        # The page's read-only look at the draft's baseline after a save is not a change to the draft.
        assert [c['kind'] for c in commands if c['kind'] != 'workspace.baseline.refresh.preview'] == ['shift.update']
        retained = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        assert retained['status'] == 'succeeded' and retained['result_stale']
        assert retained['result'] == job['result']
        expect(page.get_by_role('button', name=READY, exact=True)).to_have_count(0)
        # Undo is already an explicit reversal, not another warn-before-edit choice.
        page.locator('.toast').get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_role('dialog', name='Edit the draft now?', exact=True)).to_have_count(0)
        expect(page.locator('.toast').filter(has_text='Undone. The previous values are back.')).to_be_visible()
        assert h.detail(w)['shifts'] == before['shifts']
    finally:
        context.close()


def test_a_superseded_run_offers_no_apply_and_its_refusal_leads_to_the_latest_run(browser, live_server, h, waiting_proposal):
    """R3-N: a newer run of the same sessions replaces the older one. An Apply already open when it lands is refused
    (RUN_SUPERSEDED), writes nothing and says so once, in the dialog where Apply was, with the way to the latest run (the
    press is never just a closed dialog); Recent runs marks the older run
    with the shared badge and its Review offers no Apply, only that next step."""
    w, older = waiting_proposal
    words = 'A newer run replaced this one. Review the latest run.'
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        panel.get_by_role('button', name='Review latest result', exact=True).click()
        dialog = page.get_by_role('dialog', name='Auto-schedule results', exact=True)
        apply = dialog.get_by_role('button', name=re.compile(r'^Apply (the change|all \d+ changes)$'))
        expect(apply).to_be_visible()  # still current while the dialog opens
        newer = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
            'generation_options': {'mode': 'fill_gaps'}}, revision=h.detail(w)['workspace']['revision'])['job']
        assert process_one(h.app.state.engine)
        assert h.get('coordinator', '/api/jobs/' + newer['id'])['job']['status'] == 'succeeded'
        assert h.get('coordinator', '/api/jobs/' + older['id'])['job']['currency'] == 'superseded'
        before = h.snapshot(w)
        with page.expect_response(lambda r: r.url.endswith('/api/commands')
                and (r.request.post_data_json or {}).get('kind') == 'generation.apply') as refused:
            apply.click()
        assert refused.value.status == 409 and refused.value.json()['error']['code'] == 'RUN_SUPERSEDED'
        assert h.snapshot(w) == before
        said = dialog.get_by_role('status').filter(has_text='Replaced by a newer run')
        expect(said).to_contain_text(words)
        expect(dialog.get_by_role('button', name=re.compile(r'^Apply'))).to_have_count(0)
        expect(page.locator('.toast').filter(has_text=words)).to_have_count(0)  # said once, in the dialog
        expect(page.locator('.toast.error')).to_have_count(0)  # and not again as a failure
        said.get_by_role('button', name='Review latest run', exact=True).click()
        expect(dialog).to_have_count(0)
        expect(panel.get_by_role('region', name='Review')).to_be_visible()
        # Recent runs: the older run is marked replaced (shared badge); its Review says so, offers no Apply, leads on.
        runs = panel.locator('.run-list > li')
        expect(runs).to_have_count(2)
        replaced = runs.filter(has=page.locator('.badge', has_text='Replaced by a newer run'))
        expect(replaced).to_have_count(1)
        replaced.get_by_role('button', name=re.compile(r'^Review ')).click()
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text(words)
        expect(dialog.get_by_role('button', name=re.compile(r'^Apply'))).to_have_count(0)
        dialog.get_by_role('button', name='Review latest run', exact=True).click()
        expect(dialog).to_have_count(0)
        expect(panel.get_by_role('region', name='Review')).to_be_visible()
        assert h.snapshot(w) == before
    finally:
        context.close()


@pytest.mark.parametrize('change', ['person', 'rule'])
def test_other_input_changes_stale_proposal_without_a_draft_revision_change(browser, live_server, h, waiting_proposal, change):
    w, job = waiting_proposal
    previous = job
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_options': {'mode': 'fill_gaps'}}, revision=w['revision'])['job']
    assert job['generation_engine'] == 'legacy'
    assert process_one(h.app.state.engine)
    job = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert job['status'] == 'succeeded' and not job['result_stale'] and job['result']['additions']
    assert job['id'] != previous['id']
    assert {j['id'] for j in h.get('coordinator', f"/api/jobs?workspace_id={w['id']}")['jobs']} == {previous['id'], job['id']}
    def stored_inputs(jid):
        with transaction(h.app.state.engine) as db:
            stored = db.get(m.Job, jid)
            return {key: copy.deepcopy(getattr(stored, key)) for key in ('result', 'input_snapshot', 'input_fingerprint')}
    recorded = {j['id']: stored_inputs(j['id']) for j in (previous, job)}
    revision = h.detail(w)['workspace']['revision']
    if change == 'person':
        h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][1], 'kind': 'recorded_leave',
            'start': '2026-10-05T00:00:00+03:00', 'end': '2026-10-06T00:00:00+03:00', 'private_reason': 'Fictional stale-input test'})
    else:
        h.policy({'kind': 'minimum_cover', 'minimum': 2})
    after = h.detail(w)
    # Leave and a safety rule (minimum cover) bind the existing draft live: no baseline refresh, no draft revision.
    assert after['workspace']['revision'] == revision
    assert after['current_fingerprint'] != job['input_fingerprint']
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['result_stale']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        # The input edit legitimately refreshes workspace.validation before the refusals being tested.
        with page.expect_response(lambda r: r.url.endswith('/api/commands')
                and (r.request.post_data_json or {}).get('kind') == 'workspace.validate'
                and (r.request.post_data_json or {}).get('target_id') == w['id']) as checked:
            open_rota(page, w['id'])
        assert checked.value.ok, checked.value.text()
        open_scheduling(page)
        # maintenance/A4: the proposal stays reviewable; the drift is said and Apply checks it again (never "N still fit" yet).
        review = page.locator('#auto-panel').get_by_role('region', name='Review')
        expect(review.locator('.auto-stale')).to_contain_text('The rota changed since this run')
        expect(page.locator('#auto-panel').get_by_role('button', name='Check again and apply', exact=True)).to_be_visible()
        checked_detail = h.detail(w)
        assert {k: v for k, v in checked_detail.items() if k != 'workspace'} == {
            k: v for k, v in after.items() if k != 'workspace'}
        # The check refreshes the stored validation and so makes it current; nothing else on the rota moves.
        checked_fields = {'validation', 'validation_current'}
        assert checked_detail['workspace']['validation_current'] is True
        assert {k: v for k, v in checked_detail['workspace'].items() if k not in checked_fields} == {
            k: v for k, v in after['workspace'].items() if k not in checked_fields}
        after = checked_detail
        assert after['workspace']['revision'] == revision
        assert after['workspace']['validation']['fingerprint'] == after['current_fingerprint']
        # maintenance: Apply reconciles a proposal with the current inputs instead of refusing it; the original proposal and its
        # frozen inputs are kept, the receipt rides beside it.
        applied = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
            'reason': 'Fictional stale proposal reconciliation'}, revision=revision)
        assert applied['reconciliation']['status'] in ('applied', 'unresolved', 'revalidation_required')
        retained = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        assert retained['input_fingerprint'] == job['input_fingerprint']
        assert {k: v for k, v in retained['result'].items() if k not in ('reconciliation', 'applied', 'applied_content_revision')} == {
            k: v for k, v in job['result'].items() if k not in ('reconciliation', 'applied', 'applied_content_revision')}
        for original in (previous, job):  # raw inputs are private to storage, never rewritten
            assert stored_inputs(original['id'])['input_snapshot'] == recorded[original['id']]['input_snapshot']
    finally:
        context.close()


def test_edit_guard_review_returns_to_its_own_draft_without_writing(browser, live_server, h, waiting_proposal):
    w, job = waiting_proposal
    other = h.shift(h.workspace('Fictional other draft'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    commands = []
    try:
        open_rota(page, other['id'])
        open_rota(page, w['id'])
        expect(page.get_by_role('button', name=READY, exact=True)).to_be_visible()
        open_session(page)
        page.locator('.wk-panel').get_by_role('button', name='Edit session', exact=True).click()
        editor = page.get_by_role('region', name='Edit session and participants', exact=True)
        editor.get_by_label('Session end', exact=True).fill('2026-10-04T13:00')
        expect(editor.get_by_text('Not saved yet: review what this changes', exact=False)).to_be_visible()
        page.on('request', lambda r: commands.append(r.post_data_json) if r.url.endswith('/api/commands') else None)
        form_submit(editor).click()
        guard = page.get_by_role('dialog', name='Edit the draft now?', exact=True)
        guard.get_by_role('button', name='Review the proposal', exact=True).click()
        dirty = editor.locator('.unsaved-bar')
        expect(dirty).to_contain_text('Not saved. Close anyway?')
        dirty.get_by_role('button', name='Discard changes', exact=True).click()
        expect(editor).to_have_count(0)
        expect(page.locator('#auto-panel').get_by_role('region', name='Review')).to_be_visible()
        expect(page).to_have_url(re.compile(rf'edit={w["id"]}'))
        assert not commands
        assert not h.get('coordinator', '/api/jobs/' + job['id'])['job']['result_stale']
        assert h.get('coordinator', f"/api/jobs?workspace_id={other['id']}")['jobs'] == []
    finally:
        context.close()


def test_bulk_issue_edit_cancel_keeps_selection_and_sends_no_command(browser, live_server, h, waiting_proposal):
    w, _ = waiting_proposal
    before = h.detail(w)['shifts']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    commands = []
    try:
        navigate(page, 'schedule/issues?draft=' + w['id'] + '&view=checks')  # the Fix list's All checks table
        expect(page.locator('.proposal-note')).to_be_visible()
        row = page.locator('.issues-list tbody tr[data-row]')
        expect(row).to_have_count(1)
        row.get_by_role('checkbox').check()
        bar = page.get_by_role('toolbar', name='Bulk actions', exact=True)
        page.on('request', lambda r: commands.append(r.post_data_json) if r.url.endswith('/api/commands') else None)
        bar.get_by_role('button', name='Remove sessions', exact=True).click()
        guard = page.get_by_role('dialog', name='Edit the draft now?', exact=True)
        expect(guard).to_be_visible()
        guard.get_by_role('button', name='Close dialog', exact=True).click()
        expect(row.get_by_role('checkbox')).to_be_checked()
        assert not commands
        assert h.detail(w)['shifts'] == before
    finally:
        context.close()


@pytest.mark.parametrize('finish', ['apply', 'discard'])
def test_apply_or_discard_clears_ready_signals_and_finished_footer(browser, live_server, h, finish):
    w = h.shift(h.workspace('Fictional finished run'), people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])['job']
    assert job['generation_engine'] == 'legacy'
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_rota(page, w['id'])
        panel = page.locator('#auto-panel')
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
        expect(page.locator('.app-footer .asl-strip')).to_be_visible()
        assert process_one(h.app.state.engine)
        assert seen_done(page)['step'] == '5/5 Done'  # read at once: Done stays 3 s (D-123)
        expect(panel.get_by_role('region', name='Review')).to_be_visible()
        expect(page.get_by_role('button', name=READY, exact=True)).to_be_visible()
        completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        if finish == 'apply':
            panel.get_by_role('button', name=re.compile(r'^Apply \d+ changes?$')).click()
            expect(page.locator('.toast').filter(has_text='Suggestions applied')).to_be_visible()
        else:
            panel.get_by_role('button', name='Discard', exact=True).click()
            panel.get_by_role('button', name='Discard?', exact=True).click()
            expect(panel).to_contain_text('Proposal discarded')
        expect(page.get_by_role('button', name=READY, exact=True)).to_have_count(0)
        expect(page.get_by_role('button', name='Auto-schedule · proposal ready', exact=True)).to_have_count(0)
        expect(page.locator('.app-footer .asl-strip')).to_have_count(0)
        stored = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        assert stored['status'] == ('applied' if finish == 'apply' else 'dismissed')
        if finish == 'apply':
            assert stored['result']['applied']['added_count'] == len(completed['result']['additions'])
        page.reload()
        expect(page.get_by_role('button', name=READY, exact=True)).to_have_count(0)
    finally:
        context.close()


def test_finishing_an_older_apply_does_not_remove_another_runs_footer(browser, live_server, h, waiting_proposal):
    w, _ = waiting_proposal
    other = h.shift(h.workspace('Fictional next run'), people=[])['workspace']
    queued = h.command('coordinator', 'job.create', {'workspace_id': other['id'], 'kind': 'generation'}, revision=other['revision'])['job']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        with holding(page) as held:
            open_rota(page, w['id'])
            open_scheduling(page)
            apply = page.locator('#auto-panel').get_by_role('button', name=re.compile(r'^Apply \d+ changes?$'))
            expect(apply).to_be_visible()
            pattern = '**/api/commands'
            held.hold(pattern)
            apply.click()
            wait_held(page, held[pattern])
            assert held[pattern][0].request.post_data_json['kind'] == 'generation.apply'
            open_rota(page, other['id'])
            expect(page.locator('#auto-panel').get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
            expect(page.locator('.app-footer .asl-strip')).to_be_visible()
            with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.post_data_json['kind'] == 'generation.apply') as applied:
                held.release(pattern)
            assert applied.value.ok
            expect(page).to_have_url(re.compile(rf'edit={other["id"]}'))
            expect(page.locator('.app-footer .asl-strip')).to_be_visible()
            expect(page.locator('#auto-panel').get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
            assert h.get('coordinator', '/api/jobs/' + queued['id'])['job']['status'] == 'queued'
    finally:
        context.close()


def test_review_describes_the_job_after_keep_and_preset_settings_change(browser, cp_server, h):
    """Later tiny CP integration: retain the real worker receipt rather than synthesising an applicable job."""
    use_cp_sat(h)
    w = h.shift(h.workspace('Fictional reviewed run settings'), people=[])['workspace']
    person = h.seed['people'][1]
    job = create_population_job(h, w, population_scope('person_ids', person),
        generation_options={'mode': 'fill_gaps', 'weight_preset': 'fairness_first'})
    assert process_one(h.app.state.engine, settings=h.settings)  # the worker runs the engine the run was queued for
    completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert not completed['result_stale'] and completed['status'] == 'succeeded'
    context, page = sign_in(browser, cp_server, 'coordinator', viewport=DESKTOP)
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        expect(panel.get_by_role('region', name='Review')).to_be_visible()
        expect(panel.locator('.rl-done')).to_contain_text(person_name(h, person))
        expect(panel.locator('.rl-done')).to_contain_text('kept existing assignments')
        expect(panel.locator('.rl-done')).to_contain_text('Fairness first')
        auto_setup(panel)
        panel.get_by_role('button', name=re.compile('^People to schedule:')).click()
        page.get_by_role('dialog', name='People to schedule', exact=True).get_by_role('button', name='Everyone', exact=True).click()
        panel.get_by_label('Keep existing assignments', exact=True).uncheck()
        expect(panel.locator('.keep-source')).to_contain_text('Chosen on this device')
        auto_setup(panel, fields=False)
        panel.get_by_role('radio', name='Department defaults', exact=True).click()
        auto_result(panel)
        expect(panel.locator('.rl-done')).to_contain_text('Run of')
        expect(panel.locator('.rl-done')).to_contain_text(person_name(h, person))
        expect(panel.locator('.rl-done')).not_to_contain_text('Everyone')
        expect(panel.locator('.rl-done')).to_contain_text('kept existing assignments')
        expect(panel.locator('.rl-done')).to_contain_text('Fairness first')
        expect(panel.locator('.rl-done')).not_to_contain_text('could change existing assignments')
    finally:
        context.close()


@pytest.mark.parametrize('role,width,scheme', [('approver', 1440, 'dark'), ('staff', 1440, 'light'),
                                             ('coordinator', 390, 'dark'), ('admin', 390, 'light')])
def test_proposal_permissions_and_phone_home_health(browser, live_server, h, waiting_proposal, role, width, scheme):
    assert role in ROLE_NAMES
    w, _ = waiting_proposal
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 844}, phone_notice=True)
    try:
        page.emulate_media(color_scheme=scheme)
        if width == 390:
            navigate(page, 'home')
            health = page.locator('.home-needs-list li').filter(has_text=w['name'])
            expect(health).to_have_count(1)
            # D9/D10: the draft's row is its Fix list's must-fix causes, and the Fix list opens on the phone.
            expect(health).to_contain_text('Must fix before submitting')
            health.get_by_role('button', name='Fix list', exact=True).click()
            expect(page).to_have_url(re.compile('draft=' + w['id']))
            expect(page.locator('#main .fix-page .fix-gate')).to_contain_text('must fix before you submit')
            expect(page.get_by_role('heading', name='Best on a desktop', exact=True)).to_have_count(0)
            navigate(page, 'home')
            expect(health).to_contain_text('Must fix before submitting')
        else:
            navigate(page, 'home')
            expect(page.get_by_role('button', name=READY, exact=True)).to_have_count(0)
            if role == 'approver':
                open_rota(page, w['id'])
                expect(page.get_by_role('button', name=re.compile(r'^Auto-schedule(?: · proposal ready)?$'))).to_have_count(0)
            expect(page.get_by_role('button', name='Edit anyway', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_new_person_save_opens_rank_and_clinic_next_steps(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        page.get_by_role('button', name='New person', exact=True).click()
        form = page.get_by_role('dialog', name='New person', exact=True)
        form.get_by_label('Display name', exact=True).fill('Fictional Noor Preparation')
        form_submit(form).click()
        expect(form).to_have_count(0)
        panel = page.get_by_role('region', name='Fictional Noor Preparation', exact=True)
        expect(panel).to_contain_text('Fictional Noor Preparation')
        expect(panel.get_by_role('button', name='Add rank', exact=True)).to_be_visible()
        expect(panel.get_by_role('heading', name='Clinics', exact=True)).to_be_visible()
        expect(panel).not_to_contain_text('Cannot be scheduled yet')
        person = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['display_name'] == 'Fictional Noor Preparation')
        assert person['current_category_ids'] == []
    finally:
        context.close()


def test_setup_hours_need_every_service_and_recurring_stays_optional(browser, live_server, h):
    services = h.get('admin', '/api/catalogues')['services']
    missing = [s for s in services if s.get('active') is not False and not s.get('calendar_id')]
    assert missing, 'The ordinary seed has clinics without hours despite an existing calendar'
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'configuration/setup')
        hours = page.locator('.setup-step').filter(has=page.get_by_text('Opening hours', exact=True))
        expect(hours).not_to_have_class(re.compile(r'\bdone\b'))
        expect(hours).to_contain_text(f'{len(missing)} services without opening hours')
        recurring = page.locator('.setup-step').filter(has=page.get_by_text('Recurring sessions', exact=True))
        expect(recurring).to_contain_text('Optional')
        expect(page.locator('.setup-health')).not_to_contain_text('Cannot be scheduled yet')
    finally:
        context.close()


@pytest.mark.parametrize('on', [True, False], ids=['turn-on', 'turn-off'])
def test_approver_sees_rule_on_off_and_department_day_at_utc_boundary(browser, live_server, h, on):
    flag(h)
    if on:
        propose(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}})], on=True)
    else:
        weekly(h)
        propose_off(h)

    def boundary(route):
        reply = route_fetch(route)
        body = reply.json()
        entry = next(e for e in body['entries'] if e['id'] == 'duties_per_week')
        assert entry['proposed']
        entry['proposed']['created_at'] = '2026-10-03T22:30:00+00:00'  # 4 Oct in the seeded department's department-local zone
        route.fulfill(response=reply, json=body)

    context, page = sign_in(browser, live_server, 'approver', viewport=DESKTOP)
    try:
        page.route(re.compile(r'/api/rules/catalogue(?:\?|$)'), boundary)
        navigate(page, 'rules/duties_per_week')
        waiting = page.locator('.rc-waiting')
        expect(waiting.locator('.rc-prop-summary')).to_contain_text('Turns the rule ' + ('on' if on else 'off'))
        expect(waiting.locator('.rc-prop-who')).to_contain_text('4 Oct 2026')
        expect(waiting.locator('.rc-prop-who')).not_to_contain_text('3 Oct 2026')
        expect(waiting.get_by_role('button', name='Review and approve', exact=True)).to_be_visible()
    finally:
        context.close()


def test_population_picker_has_one_blank_line_without_silently_restricting_everyone(browser, cp_server, h):
    """Presentation only: a CP-capable server is needed for this control, but no job is started."""
    w = h.shift(h.workspace('Fictional population picker'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport=DESKTOP)
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        auto_setup(panel)  # D76: the setup's fields open under Change
        everyone = panel.get_by_role('button', name='People to schedule: Everyone', exact=True)
        everyone.click()
        picker = page.get_by_role('dialog', name='People to schedule', exact=True)
        expect(picker.locator('.cond-line')).to_have_count(1)
        picker.get_by_role('button', name='Apply', exact=True).click()
        expect(panel.get_by_role('button', name='People to schedule: Choose people…', exact=True)).to_be_visible()
        expect(panel.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_disabled()
        panel.get_by_role('button', name='People to schedule: Choose people…', exact=True).click()
        picker.get_by_role('button', name='Everyone', exact=True).click()
        expect(everyone).to_be_visible()
        expect(panel.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_enabled()
        assert h.get('coordinator', f"/api/jobs?workspace_id={w['id']}")['jobs'] == []
    finally:
        context.close()


def test_draft_csv_marks_filename_without_adding_columns(browser, live_server, h, tmp_path):
    w = h.shift(h.workspace('Fictional draft export'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_rota(page, w['id'])
        pick_view(page, 'List')
        planner_tool(page, 'Export…')
        dialog = page.get_by_role('dialog', name='Print or export', exact=True)
        format_as(dialog, 'Data (CSV)')
        with page.expect_download() as saved:
            dialog.get_by_role('button', name='Save CSV', exact=True).click()
        assert '(draft, not published)' in saved.value.suggested_filename
        path = tmp_path / 'fictional-draft.csv'
        saved.value.save_as(str(path))
        rows = list(csv.reader(io.StringIO(path.read_text(encoding='utf-8-sig'), newline='')))
        assert rows[0] == ['When', 'Duty', 'Place', 'Service', 'People', 'Status']
        assert len(rows) == 2
    finally:
        context.close()

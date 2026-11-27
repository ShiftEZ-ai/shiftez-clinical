"""Backend wave 3 (maintenance) through the web client: reasoned "Break rule and assign" (RUL-E04), the retire/restore
approval count in Configuration (RUL-03) and its pending display, "partly outside this rota" (RUL-E01) and
continuing a time-limited run with twice the time (SR-25)."""
import json
import re

import pytest
from playwright.sync_api import expect

from apps.worker.main import process_one
from .test_browser import wait_run_created, browser, live_server, sign_in, navigate, open_config, open_rota, open_rule, open_scheduling, save_dialog  # noqa: F401
from .test_browser_cp_sat import cp_server  # noqa: F401
from .test_browser_autoschedule_budget import confirm
from .test_bulk_reverse_transitions import proposal, transition

pytestmark = pytest.mark.browser
REASON = 'Only cover available; arrange relief before submission'


def on_leave(h):
    """An open session on 4 Oct and the first person on recorded leave that day (blocked in the fill panel)."""
    person = h.seed['people'][0]
    h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave',
        'leave_type': 'Annual leave', 'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00',
        'private_reason': 'Synthetic leave'})
    w = h.shift(h.workspace('Break rule rota'), people=[])['workspace']
    return w, person, h.snapshot(w)['people'][person]['display_name']


def blocked_row(page, w, who):
    open_rota(page, w['id'])
    page.locator('.pchip.gap').first.click()
    panel = page.get_by_role('dialog').filter(has_text='Can work this session')
    expect(panel).to_be_visible(timeout=20000)
    row = panel.get_by_role('region', name=re.compile(r'^Not available · \d+$')).locator('li').filter(has_text=who)
    row.get_by_role('button', name=f'Break rule and assign {who}', exact=True).click()
    return row


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (1440, 'dark')])
def test_break_rule_needs_a_reason_and_an_armed_confirm(browser, live_server, h, width, scheme):
    w, person, who = on_leave(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900}, color_scheme=scheme)
    try:
        row = blocked_row(page, w, who)
        reason = row.get_by_role('textbox', name=f'Reason to assign {who} against the rule', exact=True)
        expect(reason).to_be_focused()
        go = row.locator('form').get_by_role('button', name='Break rule and assign', exact=True)
        go.click()  # empty reason: the browser asks for it; nothing is sent or armed
        assert not reason.evaluate('(input) => input.checkValidity()')
        expect(go).not_to_have_class(re.compile(r'\barmed\b'))
        reason.fill(REASON)
        go.click()
        armed = row.locator('form button.armed')
        expect(armed).to_contain_text('Confirm?')
        assert not h.detail(w)['shifts'][0]['participants']
        with page.expect_request(lambda r: r.url.endswith('/api/commands') and r.method == 'POST') as sent:
            armed.click()
        assert sent.value.post_data_json['payload']['override_reason'] == REASON
        expect(page.get_by_role('status').filter(has_text='added to the draft')).to_be_visible(timeout=20000)
        detail = h.detail(w)
        assert [(p['person_id'], p['override_reason']) for p in detail['shifts'][0]['participants']] == [(person, REASON)]
        # The conflict still blocks review after saving.
        assert any(f['code'] == 'UNAVAILABLE' and f['severity'] == 'error' for f in detail['workspace']['validation']['findings'])
    finally:
        context.close()


def test_person_unavailable_refusal_is_said_beside_the_reason(browser, live_server, h):
    w, _, who = on_leave(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        def strip(route):  # the server refuses a new blocker even when no reason arrives
            body = route.request.post_data_json
            if body.get('kind') == 'shift.update':
                body['payload'].pop('override_reason', None)
            route.continue_(post_data=json.dumps(body))
        page.route('**/api/commands', strip)
        row = blocked_row(page, w, who)
        row.get_by_role('textbox').fill(REASON)
        go = row.locator('form').get_by_role('button', name='Break rule and assign', exact=True)
        go.click()
        row.locator('form button.armed').click()
        expect(row.get_by_role('alert')).to_contain_text('Known unavailability overlaps')
        assert not h.detail(w)['shifts'][0]['participants']
    finally:
        context.close()


def test_several_people_on_leave_are_saved_without_then_assigned_in_the_fill_panel(browser, live_server, h):
    """RUL-E04: the session editor has no reason field. Two people on leave there are refused (PERSON_UNAVAILABLE,
    details.people sorted): under the server's message as sent, one button saves the session without both; its
    message then offers the fill panel for the first named person, whose reasoned assign is unchanged."""
    w, first, _ = on_leave(h)
    second = h.seed['people'][1]
    h.command('coordinator', 'availability.record', {'person_id': second, 'kind': 'recorded_leave',
        'leave_type': 'Annual leave', 'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00',
        'private_reason': 'Synthetic leave'})
    names = h.snapshot(w)['people']
    order = [names[pid]['display_name'] for pid in sorted([first, second])]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, w['id'])
        page.locator('.pchip.gap').first.click()
        # The open place opens the fill mode; Edit session lives in the panel's Whole session mode (S-09).
        page.locator('.wk-panel').get_by_role('button', name='Whole session', exact=True).click()
        page.locator('.wk-panel').get_by_role('button', name='Edit session', exact=True).click()
        editor = page.get_by_role('region', name='Edit session and participants', exact=True)
        for pid in (first, second):
            editor.get_by_role('button', name='Add participant', exact=True).click()
            editor.get_by_label('Participant', exact=True).last.select_option(pid)
        expect(editor.get_by_text('Save the session without this person, then use the planner fill panel', exact=False)).to_be_visible(timeout=20000)
        expect(editor.get_by_role('textbox', name=re.compile('^Reason to assign'))).to_have_count(0)
        assert not h.detail(w)['shifts'][0]['participants']
        editor.get_by_role('button', name=f'Save without {order[0]} and {order[1]}', exact=True).click()
        toast = page.locator('.toast').filter(has_text='Session saved.')
        expect(toast.get_by_role('button', name='Undo', exact=True)).to_be_visible(timeout=20000)
        assert not h.detail(w)['shifts'][0]['participants']
        toast.get_by_role('button', name=f'Assign {order[0]} in the fill panel', exact=True).click()
        panel = page.get_by_role('dialog').filter(has_text='Can work this session')
        expect(panel.get_by_role('searchbox', name='Find a person', exact=True)).to_have_value(order[0])
        expect(panel.get_by_role('button', name=f'Break rule and assign {order[0]}', exact=True)).to_be_visible(timeout=20000)
    finally:
        context.close()


def test_retire_approval_count_is_configured_validated_and_undone(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Department settings')
        page.get_by_role('tab', name='Governance', exact=True).click()
        count = page.get_by_role('spinbutton', name='Approvals to retire or restore a rule', exact=True)
        expect(count).to_have_value('1')
        expect(count).to_have_attribute('min', '1')
        expect(count).to_have_attribute('max', '3')
        count.fill('4')
        save = page.get_by_role('region', name='Unsaved changes').get_by_role('button', name='Save settings', exact=True)
        save.click()
        assert not count.evaluate('(input) => input.checkValidity()')
        expect(page.get_by_role('dialog', name='Save department settings', exact=True)).to_have_count(0)
        count.fill('2')
        save.click()
        dialog = page.get_by_role('dialog', name='Save department settings', exact=True)
        dialog.get_by_label('Reason', exact=True).fill('Two approvers retire rules')
        save_dialog(dialog, 'Save settings')
        assert h.get('admin', '/api/me')['department']['policy_retire_approvals_required'] == 2
        page.locator('.toast').filter(has_text='Department settings saved').get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Undone. The previous values are back.', exact=True)).to_be_visible()
        assert h.get('admin', '/api/me')['department']['policy_retire_approvals_required'] == 1
    finally:
        context.close()


def test_retire_waiting_for_more_approvals_says_who_and_how_many(browser, live_server, h):
    row = transition(h, 'approver', 'policy.activate', proposal(h))['record']
    assert h.get('admin', '/api/me')['department']['policy_retire_approvals_required'] == 1
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        sheet = open_rule(page, row['name'])
        sheet.get_by_role('button', name='Retire rule', exact=True).click()
        expect(page.get_by_role('dialog', name='Retire rule')).to_contain_text(
            'Retiring a safety rule needs approval from at least two different people')
        confirm(page, 'Retire rule', 'Retire rule', 'First of two approvals')
        expect(page.locator('.toast').filter(
            has_text='Retiring approved by you (1 of 2) · 1 more approval needed from a different approver')).to_be_visible()
        assert next(p for p in h.get('approver', '/api/policies?status=active')['policies'] if p['id'] == row['id'])['status'] == 'active'
    finally:
        context.close()


def test_a_week_window_starting_inside_the_rota_is_partly_outside(browser, live_server, h):
    h.policy({'kind': 'max_duties', 'window': 'week', 'maximum': 1}, scope={'person_id': h.seed['people'][0]})
    w = h.workspace('Partly outside week', start='2026-10-04', end='2026-10-08')
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    w = h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        expect(page.locator('#main[data-loaded-view=schedule] .issues-head')).to_be_visible(timeout=20000)
        main = page.locator('#main')
        expect(main.locator('td[data-col=reason]').filter(has_text='Counted over 4–10 Oct 2026, partly outside this rota')).not_to_have_count(0)
        expect(main).not_to_contain_text('look-ahead after this rota')
    finally:
        context.close()


def test_time_limited_run_continues_with_twice_the_time(browser, cp_server, h):
    workspace = h.shift(h.workspace('Twice the time'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator')
    try:
        open_rota(page, workspace['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
        wait_run_created(page)
        first = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs'][0]
        minutes = first['effective_options']['search_minutes'] * 2

        def time_limit(snapshot, **options):
            return {'status': 'unknown_time_limit', 'algorithm_version': snapshot['compiler_version'],
                    'input_fingerprint': snapshot['fingerprint'],
                    'phase_timings': {'preparation_seconds': 1, 'search_seconds': 300, 'validation_seconds': 0, 'summary_seconds': 0}}
        assert process_one(h.app.state.engine, scheduler=time_limit)
        twice = panel.get_by_role('button', name=f'Continue with {minutes} minutes', exact=True)
        expect(twice).to_be_visible(timeout=20000)
        twice.click()
        continued = confirm(page, 'Continue auto-schedule', 'Continue', 'Twice the search time')
        assert continued['job']['effective_options']['search_minutes'] == minutes
    finally:
        context.close()

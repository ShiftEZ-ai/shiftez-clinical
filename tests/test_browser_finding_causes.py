"""Explained rule breaches on the Fix list (D9): findings that share a cause are one row, saying what is wrong (the
server's sentence) with one action, and its other fixes behind ▾ (the findings' fix hints), each opening the existing
form or screen with its context."""
import json
import re

import pytest
from playwright.sync_api import expect
from .test_browser import form_submit, route_fetch, browser, live_server, sign_in, navigate, open_rota, open_config, planner_tool  # noqa: F401
from .test_finding_cause_api import minimum
from .test_browser_source_parity import person_name

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 1000}


def chosen_series(dialog):
    """Add recurring sessions' one multiple-choice field (C-07): its values (the shared picker keeps them in a hidden
    <select multiple>), so a test checks which recurring sessions are preselected."""
    return dialog.locator('.pick-field').filter(has_text='Recurring sessions').locator('select[data-native]')


def breach_rota(h):
    """Two people who each need one Sunday-morning session of a clinic the rota has none of: two breaches, one cause."""
    people = h.seed['people'][:2]
    policy = minimum(h, people, scoped=True)
    w = h.lifecycle(h.workspace('Breach journey'), 'validate')['workspace']
    found = [f for f in h.detail(w)['workspace']['validation']['findings'] if f['rule_id'] == policy['id']]
    assert len(found) == 2 and {f['cause']['code'] for f in found} == {'no_candidate_sessions'}
    return people, policy, w


def open_cause(page, workspace_id, total, code=None):
    """The draft's complete list (All checks, owner decision) with every finding counted, then a cause group's existing
    cause card(s) and their fixes: the Schedule's own cards (app.ts causeGroupIssue)."""
    navigate(page, f'schedule/issues?draft={workspace_id}&view=checks')
    expect(page.locator('.issues-counts')).to_have_text(re.compile(rf'^{total} open · 0 resolved'))
    page.locator('#main button[data-causes~="%s"]' % code if code else '#main button[data-causes]').first.click()
    return page.get_by_role('dialog', name='Cause and fixes')


def fix_row(page, workspace_id):
    """The rota's Fix list and its first must-fix cause (each test's rota has one)."""
    navigate(page, f'schedule/issues?draft={workspace_id}')
    row = page.locator('#main section[data-level=must] .fix-list > li[data-cause]').first
    expect(row).to_be_visible()
    return row


def other_fixes(row):
    """The cause's evidence and other fixes, behind ▾."""
    row.get_by_role('button', name='Show evidence', exact=True).click()
    return row.locator('.fix-detail')


def test_rota_without_the_sessions_a_rule_needs_explains_the_breach_and_adds_them(browser, live_server, h):
    people, policy, w = breach_rota(h)
    # The person fixing it may change recurring sessions (admin) and the draft (coordinator).
    h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['coordinator'], 'role': 'admin',
                                      'starts_on': '2020-01-01', 'reason': 'Breach journey fixture'})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        # One cause for both breaches (the server's sentence), its people in the evidence behind ▾.
        row = fix_row(page, w['id'])
        expect(page.locator('#main section[data-level=must] .fix-list > li[data-cause]')).to_have_count(1)
        expect(row).to_contain_text('2 people need more duties')
        detail = other_fixes(row)
        for pid in people:
            expect(detail).to_contain_text(person_name(h, pid))
        # The fix opens the recurring-session form filled in from the rule (maintenance add_sessions with its exact prefill):
        # duty type, place, clinic, day, times, dates.
        add = row.get_by_role('button', name='Add sessions', exact=True)
        expect(add).to_have_class(re.compile(r'(^| )primary( |$)'))
        add.click()
        form = page.get_by_role('dialog', name='New recurring session')
        expect(form.get_by_label('Duty type', exact=True)).to_have_value(h.seed['type'])
        expect(form.get_by_label('Location', exact=True)).to_have_value(h.seed['locations'][0])
        expect(form.get_by_role('group', name='Clinics and services').get_by_role('checkbox', checked=True)).to_have_count(1)
        expect(form.get_by_role('checkbox', name='Sunday', exact=True)).to_be_checked()
        expect(form.get_by_role('checkbox', name='Monday', exact=True)).not_to_be_checked()
        expect(form.get_by_label('Sunday start', exact=True)).to_have_value('08:00')
        expect(form.get_by_label('Sunday end', exact=True)).to_have_value('12:00')
        expect(form).to_contain_text('After you create it, its sessions can be added to Breach journey')
        assigned = form.get_by_role('group', name='Usual assignees')
        for pid in people:
            assigned.get_by_role('checkbox', name=person_name(h, pid), exact=True).check()
        form_submit(form).click()
        expect(form).to_have_count(0, timeout=15000)
        series = [s for s in h.get('admin', '/api/catalogues')['recurring_series'] if set(s['definition']['default_person_ids']) == set(people)]
        assert len(series) == 1 and (series[0]['starts_on'], series[0]['ends_on']) == ('2026-10-04', '2026-10-11')
        # Then its sessions go into this draft through the usual reviewed Add recurring sessions, the new one chosen.
        add_sessions = page.get_by_role('dialog', name='Add recurring sessions')
        expect(chosen_series(add_sessions)).to_have_values([series[0]['id']])  # only the new one
        # D-077 (7db6e17f1): one press saves; the impact preview is optional and never asked for first.
        add_sessions.get_by_role('button', name='Add sessions', exact=True).click()
        expect(add_sessions).to_have_count(0, timeout=15000)
        # The draft checks itself again: the breach is gone, on the server and in the footer.
        page.wait_for_function("""id => fetch('/api/workspaces/'+id).then(r=>r.json()).then(d=>
            d.workspace.validation && d.workspace.validation.fingerprint===d.current_fingerprint)""", arg=w['id'], timeout=20000)
        check = h.detail(w)['workspace']['validation']
        assert not [f for f in check['findings'] if f['rule_id'] == policy['id']]
        assert not [c for c in h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['causes']
                    if any(l['rule_id'] == policy['id'] for l in c['rule_lines'])]
    finally:
        context.close()


def test_coordinator_adds_the_whole_pattern_to_the_draft_in_one_change_with_one_undo(browser, live_server, h):
    """Recurring sessions are admin-only: a coordinator's fix adds the cause's days and times across its dates to this
    rota's draft as one bulk change. People are optional: the empty sessions are saved, the problems the save brings
    show at once as explained cards (never an all-clear), and one Undo takes the whole change back."""
    people, policy, w = breach_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        add = fix_row(page, w['id']).get_by_role('button', name='Add sessions', exact=True)
        expect(add).to_have_class(re.compile(r'(^| )primary( |$)'))
        add.click()
        form = page.get_by_role('dialog', name=re.compile(r'^Add .+ sessions to this rota$'))
        days = form.get_by_role('group', name='Days')
        expect(form.get_by_label('Duty type', exact=True)).to_have_value(h.seed['type'])
        expect(form.get_by_label('Location', exact=True)).to_have_value(h.seed['locations'][0])
        expect(days.get_by_role('checkbox', name='Sunday', exact=True)).to_be_checked()
        expect(days.get_by_role('checkbox', name='Monday', exact=True)).not_to_be_checked()
        expect(form.get_by_label('Start', exact=True)).to_have_value('08:00')
        expect(form.get_by_label('End', exact=True)).to_have_value('12:00')
        # Nobody is put on them unasked: auto-schedule fills them.
        assigned = form.get_by_role('group', name='People on each session (optional)')
        for pid in people:
            expect(assigned.get_by_role('checkbox', name=person_name(h, pid), exact=True)).not_to_be_checked()
        expect(form).to_contain_text('1 session: Sun 4 Oct')
        days.get_by_role('checkbox', name='Monday', exact=True).check()  # the whole pattern, not one day
        expect(form).to_contain_text('2 sessions: Sun 4 Oct')
        form.get_by_role('button', name='Add sessions', exact=True).click()
        expect(form).to_have_count(0, timeout=15000)
        shifts = h.detail(w)['shifts']
        assert len(shifts) == 2 and all(not s['participants'] for s in shifts)
        # Saved is not ready: the new problems show at once, explained, with their fixes; the message is a warning.
        found = page.get_by_role('region', name=re.compile(r'^\d+ new issues? to resolve$'))  # a list: a docked panel (C26)
        expect(found).to_contain_text('Saved is not the same as ready')
        expect(found.locator('.issue-card').first).to_be_visible()
        toast = page.locator('.toast.warning').filter(has_text=re.compile(r'2 sessions added to Breach journey · \d+ new issues? to resolve'))
        expect(toast).to_be_visible()
        # The breach is no longer "no sessions": they exist and wait for people.
        page.wait_for_function("""id => fetch('/api/workspaces/'+id).then(r=>r.json()).then(d=>
            d.workspace.validation && d.workspace.validation.fingerprint===d.current_fingerprint)""", arg=w['id'], timeout=20000)
        causes = {f['cause']['code'] for f in h.detail(w)['workspace']['validation']['findings']
                  if f['rule_id'] == policy['id'] and f.get('cause')}
        assert causes and 'no_candidate_sessions' not in causes, causes
        page.keyboard.press('Escape')
        expect(found).to_have_count(0)
        # One Undo takes the whole change back.
        toast.get_by_role('button', name='Undo').click()
        page.wait_for_function("id => fetch('/api/workspaces/'+id).then(r=>r.json()).then(d=>d.shifts.length===0)", arg=w['id'], timeout=15000)
    finally:
        context.close()


def test_people_view_marks_each_person_with_the_breach_in_words_and_its_cause(browser, live_server, h):
    people, _, w = breach_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        other_fixes(fix_row(page, w['id'])).get_by_role('button', name='Show on schedule', exact=True).click()
        for pid in people:
            mark = page.locator('.pg-name', has_text=person_name(h, pid)).locator('.pg-breach')
            expect(mark).to_have_text('Breach')  # icon and word, never colour alone
            expect(mark).to_have_attribute('aria-label', re.compile(r'^Rule breach: There are no .+ sessions in this rota: Needs 1'))
    finally:
        context.close()


def test_leave_breach_opens_that_persons_leave(browser, live_server, h):
    absent = h.seed['people'][1]
    policy = minimum(h, [absent])
    h.command('coordinator', 'availability.record', {'person_id': absent, 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-11T00:00:00+03:00', 'private_reason': 'Synthetic leave'})
    w = h.lifecycle(h.shift(h.workspace('Leave breach'), people=[h.seed['people'][0]])['workspace'], 'validate')['workspace']
    finding = next(f for f in h.detail(w)['workspace']['validation']['findings'] if f['rule_id'] == policy['id'])
    assert finding['cause']['code'] == 'person_unavailable'
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        other_fixes(fix_row(page, w['id'])).get_by_role('button', name='Open leave', exact=True).click()
        panel = page.locator('.person-panel')
        expect(panel).to_contain_text(person_name(h, absent))
        expect(panel.get_by_role('tab', name='Requests')).to_have_attribute('aria-selected', 'true')
    finally:
        context.close()


def test_approved_leave_duty_fix_names_cover_and_remove_actions(browser, live_server, h):
    person = h.seed['people'][0]
    minimum(h, [person])
    w = h.shift(h.workspace('Approved leave duty'), people=[person])['workspace']
    request = h.command('staff', 'request.create', {'person_id': person,
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-11T00:00:00+03:00',
        'private_reason': 'Synthetic approved leave'})['record']
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Independent test decision'},
              request['id'], request['revision'])
    w = h.detail(w)['workspace']  # approved leave binds the existing draft live: no baseline refresh
    h.lifecycle(w, 'validate')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        row = fix_row(page, w['id'])
        expect(row).to_contain_text('Duties during approved leave')
        # The row's fix is done in place (Assign, 0c4eddd44); the cause's own Find cover / replace joins its other fixes
        # behind Show evidence (fix_list.ts fixRow `aside`), so it is offered once, there.
        detail = other_fixes(row)
        expect(detail.get_by_role('button', name='Find cover / replace', exact=True)).to_be_visible()
        expect(row.get_by_role('button', name='Find cover / replace', exact=True)).to_have_count(1)
        expect(detail.get_by_role('button', name='Replace…', exact=True)).to_be_visible()
        expect(detail.get_by_role('button', name='Remove from the duty', exact=True)).to_be_visible()
    finally:
        context.close()


# Every other cause's fix hint: where its fix (behind ▾ on the Fix list) goes. Its words are the All checks table's.
CAUSES = {
    'no_eligible_people': ({'sessions_found': 3, 'eligible_people': 0}, 'open_eligibility',
                           r'nobody who needs them can work the .+ sessions', r'3 .+ sessions exist .+, but nobody it applies to is eligible',
                           'Open eligibility', lambda page, h: expect(page.locator('.person-panel')).to_be_visible()),
    'capacity_short': ({'sessions_found': 1, 'possible': 1, 'places_available': 1}, 'raise_capacity',
                       r'too few .+ places for everyone', r'Needs 1; at most 1 possible from 1 .+ session',
                       'Raise the limit', lambda page, h: expect(page.locator('.rule-sheet')).to_be_visible()),
    # A bound the server prorated by working days reads as such, here and on each person's line (never recalculated).
    'partial_window': ({'sessions_found': 1, 'days_in_window': 7, 'days_available': 2, 'actual': 0, 'proration': {
                            'configured_amount': 2, 'amount': 1, 'working_days': 1, 'full_working_days': 5, 'starts_on': '2026-10-04',
                            'ends_on': '2026-10-11', 'calendar_id': 'synthetic', 'rounding': 'up'}}, 'open_rota',
                       r'only part of the rule period is in this rota',
                       r'In this rota 2 of 7 days; needs 1 \(prorated: 1 of 5 days\), has 0[\s\S]*0 of 1 \(prorated: 1 of 5 days\)',
                       'Open rota rules', lambda page, h: expect(page.get_by_role('dialog', name='Rules in this rota')).to_be_visible()),
    'unfilled': ({'sessions_found': 2, 'possible': 2, 'actual': 0}, 'run_auto_schedule',
                 r'.+ places are free but not assigned', r'Needs 1; 2 possible, 0 assigned',
                 'Run auto-schedule', lambda page, h: expect(page.locator('#auto-panel')).to_be_visible()),
    'above_limit': ({'sessions_found': 2, 'actual': 2}, 'review_assignments',
                    r'more than the rule allows', r'2 assigned; at most 1',
                    'Review assignments', lambda page, h: expect(page.locator('#main .fix-page')).to_be_visible()),
}


@pytest.mark.parametrize('code', CAUSES)
def test_each_cause_is_worded_from_its_counts_and_its_fix_opens_the_right_screen(browser, live_server, h, code):
    people, policy, w = breach_rota(h)
    counts, action, head, why, label, opened = CAUSES[code]
    retained_findings = h.detail(w)['workspace']['validation']['findings']

    def recast(finding):
        if finding.get('rule_id') != policy['id'] or not finding.get('cause'):
            return finding
        prefill = {**finding['fix_hints'][0]['prefill'], 'person_id': finding['person_id'], 'rule_id': policy['id']}
        return {**finding, 'cause': {**finding['cause'], **counts, 'code': code}, 'fix_hints': [{'action': action, 'prefill': prefill, 'matching_series': []}]}

    def edit(route):
        reply = route_fetch(route)
        if route.request.method != 'GET' or not reply.ok or not reply.text():
            route.fulfill(response=reply)
            return
        body = reply.json()
        if route.request.url.split('?')[0].endswith('/summary'):
            # The cause panel requests the compact projection; its source findings remain on the workspace.
            findings = [recast(f) for f in body.get('findings', retained_findings)]
            if 'findings' in body:
                body['findings'] = findings
            for group in body.get('cause_groups') or []:
                group['code'] = code
                group['fix_hints'] = [recast({**f, 'rule_id': policy['id']})['fix_hints'][0] for f in findings if f['key'] in group['finding_keys']][:1]
        elif 'findings' in (body['workspace'].get('validation') or {}):  # the compact read carries the check's summary only
            body['workspace']['validation']['findings'] = [recast(f) for f in body['workspace']['validation']['findings']]
        route.fulfill(response=reply, body=json.dumps(body))

    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        page.route(re.compile(rf'/api/workspaces/{w["id"]}(/summary)?(\?.*)?$'), edit)
        other_fixes(fix_row(page, w['id'])).get_by_role('button', name=re.compile('^' + re.escape(label))).first.click()
        opened(page, h)
    finally:
        context.close()


def test_department_chooses_the_opening_hours_that_prorate_targets_with_audit_and_undo(browser, live_server, h):
    """The working days a partial rule period is prorated by: the department's opening hours, chosen in Department
    settings (active ones only; none = the department weekend days), saved with a reason and undone in one click."""
    week = [{'weekday': d, 'windows': [{'start_time': '08:00', 'end_time': '16:00'}]} for d in range(5)]
    calendar = h.configure('operating_calendars', {'name': 'Department working week', 'starts_on': '2020-01-01', 'active': True,
        'definition': {'timezone': 'Asia/Riyadh', 'week': week}})['record']
    h.configure('operating_calendars', {'name': 'Retired hours', 'starts_on': '2020-01-01', 'active': False,
        'definition': {'timezone': 'Asia/Riyadh', 'week': week}})['record']
    before = h.get('admin', '/api/me')['department']['operating_calendar_id'] or ''
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        open_config(page, 'Department settings')
        pane = page.locator('.config-pane')
        choice = pane.get_by_label('Working days for prorated targets', exact=True)
        expect(choice).to_have_value(before)
        expect(choice.locator('option', has_text='Retired hours')).to_have_count(0)
        choice.select_option(calendar['id'])
        pane.get_by_role('button', name='Save settings', exact=True).click()
        page.get_by_role('dialog').get_by_label('Reason', exact=True).fill('Prorate by our opening hours')
        page.get_by_role('dialog').get_by_role('button', name='Save settings', exact=True).click()
        undo = page.get_by_role('button', name='Undo', exact=True)
        expect(undo).to_be_visible()
        assert h.get('admin', '/api/me')['department']['operating_calendar_id'] == calendar['id']
        undo.click()
        expect(pane.get_by_label('Working days for prorated targets', exact=True)).to_have_value(before)
        assert (h.get('admin', '/api/me')['department']['operating_calendar_id'] or '') == before
    finally:
        context.close()


PRORATION = {'configured_amount': 2, 'amount': 1, 'working_days': 1, 'full_working_days': 5, 'starts_on': '2026-10-04',
             'ends_on': '2026-10-11', 'calendar_id': 'synthetic', 'rounding': 'up'}


def test_prorated_targets_read_the_same_in_the_quota_report_and_the_issue_details(browser, live_server, h):
    """One shared wording ("(prorated: 1 of 5 days)") wherever a prorated amount shows: the rota's Targets and limits
    report and each finding's numbers (Resolve issues, previews). The server's amount is shown, never recalculated."""
    _, policy, w = breach_rota(h)

    def edit(route):
        reply = route_fetch(route)
        if route.request.method != 'GET' or not reply.ok or not reply.text():
            route.fulfill(response=reply)
            return
        body = reply.json()
        check = body['workspace'].get('validation') or {}
        if 'quota_report' not in check:  # the compact read carries the check's summary only
            route.fulfill(response=reply)
            return
        for row in check['quota_report']['rows']:
            if row['policy_id'] == policy['id']:
                row.update(amount=1, proration=PRORATION)
        for f in check['findings']:
            if f['rule_id'] == policy['id']:
                f['detail'] = {**(f.get('detail') or {}), 'actual': 0, 'amount': 1, 'proration': PRORATION}
        route.fulfill(response=reply, body=json.dumps(body))

    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        page.route(re.compile(rf'/api/workspaces/{w["id"]}(\?.*)?$'), edit)
        navigate(page, 'planner')
        open_rota(page, w['id'])
        planner_tool(page, 'Workload targets for this rota')
        targets = page.locator('[data-rota-targets]')
        expect(targets).to_contain_text('1 (prorated: 1 of 5 days)')
        expect(targets).to_contain_text('2 prorated by working days')
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')  # the Fix list's All checks table
        expect(page.locator('#main')).to_contain_text('0 of 1 (prorated: 1 of 5 days)')
    finally:
        context.close()


def test_matching_series_is_extended_to_the_breach_dates_without_cutting_it_short(browser, live_server, h):
    """A recurring session that matches but starts after the breach's dates: Extend moves its start earlier and keeps
    it open-ended (never adds an end). Editing a series is admin-only: a coordinator is not offered it."""
    from .test_series_reconciliation import template
    later = template(h, days=(6,), starts_on='2026-10-06', ends_on=None)
    people, policy, w = breach_rota(h)
    hint = next(f for f in h.detail(w)['workspace']['validation']['findings'] if f['rule_id'] == policy['id'])['fix_hints'][0]
    assert {m['id']: m['action'] for m in hint['matching_series']}.get(later['id']) == 'extend'
    # A coordinator: only the draft-only fix, never the admin-only series edit.
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        row = fix_row(page, w['id'])
        expect(row.get_by_role('button', name='Add sessions', exact=True)).to_have_class(re.compile(r'(^| )primary( |$)'))
        expect(other_fixes(row).get_by_role('button', name=re.compile(r'^(Extend|Switch on) '))).to_have_count(0)
    finally:
        context.close()
    h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['coordinator'], 'role': 'admin',
                                      'starts_on': '2020-01-01', 'reason': 'Series extension fixture'})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        extend = other_fixes(fix_row(page, w['id'])).get_by_role('button', name=f"Extend {later['name']}", exact=True)
        expect(extend).to_be_visible()  # beside the row's one primary action, never a second primary
        extend.click()
        form = page.get_by_role('dialog', name=re.compile(r'^Edit recurring session'))
        expect(form.get_by_label('Effective from', exact=True)).to_have_value(re.compile('4 Oct 2026'))
        expect(form.get_by_label('Effective until (included)', exact=True)).to_have_value('')
        form.get_by_role('button', name=re.compile('^Save')).click()  # no rota changes: saved on the first click
        expect(form).to_have_count(0, timeout=15000)
        saved = next(s for s in h.get('admin', '/api/catalogues')['recurring_series'] if s['id'] == later['id'])
        assert (saved['starts_on'], saved.get('ends_on')) == ('2026-10-04', None), saved
        expect(chosen_series(page.get_by_role('dialog', name='Add recurring sessions'))).to_have_values([later['id']])  # only the extended one
    finally:
        context.close()

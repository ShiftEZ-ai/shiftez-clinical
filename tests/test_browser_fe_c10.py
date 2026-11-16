"""maintenance (SWEEP-3 findings SW3-B): regressions for the governance screens (review, return, reopen, rule proposals,
self-approval)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, route_fetch, save_dialog  # noqa: F401

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 900}


def test_a_return_shows_the_draft_at_once_even_when_the_rota_list_is_slow(browser, live_server, h):
    """SW3-B-03b: after Return to coordinator the page shows the returned draft (no Approve, no Return) at once; it no
    longer waits minutes on a read of the whole rota list, nor keeps "Waiting for approval" when that read fails."""
    w = h.lifecycle(h.shift(h.workspace('Return reply'))['workspace'], 'submit')['workspace']
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        navigate(page, 'planner/' + w['id'])
        page.get_by_role('button', name='Return to coordinator', exact=True).click()
        form = page.get_by_role('dialog', name='Return to coordinator', exact=True)
        form.get_by_label('Reason', exact=True).fill('Cover the Thursday clinic first')
        page.route('**/api/workspaces?*', lambda route: route.fulfill(status=502, body='Bad gateway'))
        save_dialog(form, 'Return to coordinator')
        expect(page.locator('.toast').filter(has_text='Returned to the coordinator.')).to_be_visible()
        expect(page.get_by_role('button', name='Return to coordinator', exact=True)).to_have_count(0)
        expect(page.get_by_role('button', name='Approve rota', exact=True)).to_have_count(0)
        assert h.detail(w)['workspace']['status'] == 'draft'
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_an_approval_refused_because_the_rota_was_reopened_says_so_and_shows_it(browser, live_server, h):
    """SW3-B-21: the coordinator reopens a rota while the approver has Approve open. Approve is refused (safety), and
    the approver gets one message saying what happened, the dialog closes and the page shows the draft (no Approve)."""
    w = h.lifecycle(h.shift(h.workspace('Reopened meanwhile'))['workspace'], 'submit')['workspace']
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        navigate(page, 'planner/' + w['id'])
        page.get_by_role('button', name='Approve rota', exact=True).click()
        form = page.get_by_role('dialog', name='Approve rota', exact=True)
        form.get_by_label('Note', exact=True).fill('Independent review')
        h.lifecycle(w, 'reopen')
        form.get_by_role('button', name='Approve rota', exact=True).click()
        expect(form).to_have_count(0)
        toasts = page.locator('.toast')
        expect(toasts.filter(has_text='It was reopened as a draft meanwhile. Nothing was saved')).to_be_visible()
        expect(toasts.filter(has_text='Another session')).to_have_count(0)
        expect(toasts.filter(has_text='This record changed while you were working')).to_have_count(0)
        expect(page.get_by_role('button', name='Approve rota', exact=True)).to_have_count(0)
        expect(page.locator('.app-footer')).not_to_contain_text('Not saved')
        assert h.detail(w)['workspace']['status'] == 'draft'
    finally:
        context.close()


def test_a_limit_over_a_run_of_days_says_the_run_wherever_the_line_is_read(browser, live_server, h):
    """SW3-B-13: "60 hours in any 14 days" read "≤ 60" in the approver's proposal table and "60 hours per period" in the
    sentences; every place now words a line through one function (unitText/windowText) that says the run."""
    from .test_browser_rule_catalogue import flag, line
    flag(h)
    fourteen = line({'max': {'value': 60, 'level': 'medium'}}, settings={'max_hours': {'window': 'rolling', 'window_days': 14}})
    h.command('coordinator', 'rule.propose', {'catalogue_id': 'hours_per_period', 'lines': [fourteen], 'reason': 'Fellows cap'})
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        navigate(page, 'rules/hours_per_period')
        added = page.locator('.rc-waiting tr.rc-prop-added')
        expect(added).to_contain_text('≤ 60')
        expect(added).to_contain_text('hours in any 14 days')
        page.locator('.rc-waiting').get_by_role('button', name='Review and approve', exact=True).click()
        says = page.locator('.rc-line-panel .rc-says-text').first
        expect(says).to_contain_text('at most 60 hours in any 14 days')
        expect(says).not_to_contain_text('per period')
    finally:
        context.close()


def test_home_review_of_a_rule_change_opens_its_proposal(browser, live_server, h):
    """SW3-B-14: Home › Rule change › Review opens the rule page with the proposal (Activate…) in view, not the whole
    Rules list with nothing open."""
    from .test_browser_rule_catalogue import flag, line
    flag(h)
    h.command('coordinator', 'rule.propose', {'catalogue_id': 'hours_per_period', 'reason': 'Fellows cap',
                                              'lines': [line({'max': {'value': 60, 'level': 'medium'}})]})
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        navigate(page, 'home')
        page.locator('li[data-need="rules"]').get_by_role('button', name='Review', exact=True).click()
        expect(page).to_have_url(re.compile(r'#rules/hours_per_period'))
        waiting = page.locator('.rc-waiting')
        expect(waiting.get_by_role('button', name='Review and approve', exact=True)).to_be_in_viewport()
    finally:
        context.close()


def test_a_new_line_picked_from_the_rules_list_opens_in_its_editor(browser, live_server, h):
    """SW3-B-15: "+ New line in <rule>" on the Rules list, then a choice, opens the rule page with that new (unsaved) line
    in its editor; it used to land on "This line is not available in the current rule" with the line gone."""
    from .test_browser_rule_catalogue import flag, weekly
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        main.locator('tr.rt-rule').filter(has_text='Duties per week').get_by_role('button', name='Open Duties per week').click()
        main.get_by_role('button', name='+ New line in Duties per week', exact=True).click()
        page.get_by_role('combobox', name='Search New line').fill('Continuity clinic')
        page.get_by_role('option', name=re.compile('^Continuity clinic')).first.click()
        expect(page).to_have_url(re.compile('#rules/duties_per_week/'))
        expect(page.locator('.rc-line-panel .side-head h2')).to_contain_text('Continuity clinic')
        expect(main.get_by_text('This line is not available in the current rule')).to_have_count(0)
    finally:
        context.close()


def test_a_self_activation_offers_check_and_stop_and_decides_once_stopped(browser, live_server, h):
    """SW3-B-19: the self-activation panel says Self-approved before deciding, offers Check again and Stop for a check that
    never finishes (no worker runs here), and once stopped allows "Approve without a complete check" with a reason, as
    the server requires (no decision while a check runs)."""
    from .test_browser_dev_findings import solo
    from .test_browser_rule_catalogue import flag, line
    flag(h)
    who = solo(h)
    workspace = h.shift(h.workspace('Self check draft'))['workspace']
    h.command(who, 'rule.propose', {'catalogue_id': 'duties_per_week', 'reason': 'Own proposal',
                                    'lines': [line({'max': {'value': 5, 'level': 'mandatory'}})]})
    context, page = sign_in(browser, live_server, who, DESKTOP)
    try:
        navigate(page, 'rules/duties_per_week')
        page.locator('.rc-waiting').get_by_role('button', name='Review and approve', exact=True).click()
        review = page.get_by_role('region', name='Add a line for everyone · turns the rule on', exact=True)
        effect = review.locator('.rc-effect')
        expect(effect.get_by_text(workspace['name'], exact=True)).to_be_visible()
        expect(review.get_by_text('Self-approved', exact=True)).to_be_visible()
        approve = review.get_by_role('button', name='Approve and activate', exact=True)
        expect(effect).to_contain_text('Waiting to check · ')
        expect(approve).to_be_disabled()
        # maintenance: a review read that fails is said and polling goes on (it used to stop while the timer kept counting).
        page.route('**/api/policies/*/review', lambda route: route.fulfill(status=503, json={'error': {'code': 'UNAVAILABLE', 'message': 'The service is busy.'}}))
        expect(review.get_by_text("Couldn't check · The service is busy.")).to_be_visible()
        page.unroute('**/api/policies/*/review')
        expect(review.get_by_text("Couldn't check")).to_have_count(0)
        review.get_by_role('button', name='Stop', exact=True).click()
        expect(effect).to_contain_text('Check stopped')
        expect(review.get_by_role('button', name='Check again', exact=True)).to_be_visible()
        review.get_by_label('Approve without a complete check of these drafts', exact=True).check()
        review.get_by_label('Reason', exact=True).fill('Worker is down; the line only lowers a cap')
        expect(approve).to_be_enabled()
        # Check again starts a fresh check: the decision waits for it again.
        review.get_by_role('button', name='Check again', exact=True).click()
        expect(effect).to_contain_text('Waiting to check · ')
        expect(approve).to_be_disabled()
    finally:
        context.close()


def test_a_self_approval_permission_offers_only_the_work_its_grantee_can_do(browser, live_server, h):
    """SW3-B-18: Add permission offers only the actions the chosen role can do as their own work (maintenance's grantable);
    a Rota Coordinator, who proposes rules but never activates them, is not offered "Activate a rule they proposed".
    A saved grant whose action nobody it covers can do says so (maintenance's unusable_actions)."""
    import json
    h.command('admin', 'self_approval.grant', {'role': 'admin', 'actions': ['rule.activate'], 'starts_on': '2026-01-01',
              'ends_on': '2026-12-01', 'reason': 'Only admin proposes rules'})

    def legacy(route):  # a grant saved before maintenance that its holders can never use
        body = route_fetch(route).json()
        for p in body['permissions']:
            p['unusable_actions'] = p['actions']
        route.fulfill(status=200, content_type='application/json', body=json.dumps(body))
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        page.route('**/api/self-approval', legacy)
        navigate(page, 'access/routes')
        row = page.locator('.dt-table tbody tr').filter(has_text='Only admin proposes rules')
        expect(row).to_contain_text('Never used: Activate a rule they proposed')
        page.get_by_role('button', name='Add permission', exact=True).click()
        form = page.get_by_role('dialog', name='Add a self-approval permission', exact=True)
        form.get_by_label('Applies to', exact=True).select_option('role')
        form.get_by_label('Role', exact=True).select_option('admin')
        activate = form.get_by_label('Activate a rule they proposed', exact=True)
        expect(activate).to_be_visible()
        activate.check()
        form.get_by_label('Role', exact=True).select_option('coordinator')
        expect(activate).to_be_hidden()
        expect(activate).not_to_be_checked()
        expect(form.get_by_label('Decide their own leave or availability request', exact=True)).to_be_visible()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

def test_the_published_footer_counts_causes_as_the_fix_list_does(browser, live_server, h):
    """SX-13 (SW3-B-09): the published footer read "50 checks could improve" while the rota's Fix list said "5 could
    improve"; it now says the Fix list's causes the server sends, and the count of stored checks no longer shows."""
    h.publish(h.shift(h.workspace())['workspace'])
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        def issued_counts(route):
            response = route_fetch(route, headers={k: v for k, v in route.request.headers.items() if k != 'if-none-match'})
            body = response.json()
            body['summary'] = {'sessions_with_gaps': 0, 'blocking': 4, 'advisory': 50, 'extra_cover_sessions': 0, 'must': 1, 'could': 5}
            route.fulfill(response=response, json=body)
        page.route('**/api/schedule/sessions?**', issued_counts)
        navigate(page, 'schedule/week/2026-10-04?edit=none')
        footer = page.locator('.app-footer .health-pills')
        expect(footer.get_by_role('button', name='5 could improve', exact=True)).to_be_visible()
        expect(footer.get_by_role('button', name='1 must fix', exact=True)).to_be_visible()
        expect(footer).not_to_contain_text('50')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

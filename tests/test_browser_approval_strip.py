"""The approval review (L11, D25/maintenance): the rota leads with ONE status from the status palette; the review is facts in
words (its step, "0 of 1 approvals", its due time, "Overdue since …" with an icon once past); the approver accepts or
rejects each item and Approve stays disabled, with its reason, until every item is decided. A review the server holds
out of date (stale_inputs) is "Needs resubmitting" only, never also "Waiting for approval". Approver, 1440 light and
390 dark."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, route_fetch, pick_view  # noqa: F401
from .test_browser_schedule import show

pytestmark = pytest.mark.browser

VIEWS = [({'width': 1440, 'height': 900}, 'light'), ({'width': 390, 'height': 844}, 'dark')]
PAST = '2026-01-01T08:00:00+00:00'


def past_due(page, workspace_id):
    """The rota's reply with its pending step's due time in the past (the server computes it from submitted_at)."""
    def handle(route):
        reply = route_fetch(route)
        data = reply.json()
        progress = data.get('review_progress') or {}
        for step in [*progress.get('steps', []), progress.get('pending_step') or {}]:
            if step.get('due_at'):
                step['due_at'] = PAST
        route.fulfill(response=reply, json=data)
    page.route(re.compile(rf'/api/workspaces/{workspace_id}(\?.*)?$'), handle)


def one_status(page, words):
    bar = page.locator('.topbar .planner-bar')
    status = bar.locator('.rota-status')
    expect(status).to_have_count(1)
    expect(status).to_have_text(words)
    expect(status).to_have_class(re.compile(r'\bbadge\b.*\bwarn\b|\bwarn\b.*\bbadge\b'))  # statusBadge, the palette's tone
    assert status.get_attribute('aria-label') is None  # its accessible name is its words
    expect(bar.locator('.health-pill').filter(has_text=re.compile('Needs resubmitting|Waiting'))).to_have_count(0)
    expect(bar.locator('.pc-chip')).to_have_count(0)  # no second "change waiting" pill for the rota on screen
    return bar


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_review_has_one_status_overdue_in_words_and_per_item_decisions(browser, live_server, h, view, scheme):
    rest = h.policy({'kind': 'max_hours', 'hours': 12, 'window_days': 7})
    w = h.shift(h.workspace('Review with an item'))['workspace']
    w = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': rest['id'], 'mode': 'set', 'amount': 20,
                  'reason': 'Workload agreed for this rota'}, w['id'], w['revision'])['workspace']
    w = h.lifecycle(w, 'submit')['workspace']
    context, page = sign_in(browser, live_server, 'approver', viewport=view, color_scheme=scheme)
    try:
        past_due(page, w['id'])
        navigate(page, 'schedule?approve=' + w['id'])  # Home's "Rota to review" (D25): the review opens at once
        form = page.get_by_role('dialog', name='Approve rota', exact=True)
        expect(form).to_be_visible()
        expect(form).not_to_contain_text('Review step:')  # the strip already names the step
        item = form.get_by_role('region', name='Changes for this rota').locator('li[data-review-item]')
        expect(item).to_have_count(1)
        expect(item).to_contain_text('“Workload agreed for this rota”')
        expect(item.get_by_role('button', name='Accept', exact=True)).to_be_visible()
        expect(item.get_by_role('button', name='Reject', exact=True)).to_be_visible()
        approve = form.locator('button[type=submit]')
        expect(approve).to_be_disabled()
        reason = form.locator('.decide-count')
        expect(reason).to_contain_text('Decide each item to approve')
        expect(approve).to_have_attribute('aria-describedby', reason.get_attribute('id'))
        item.get_by_role('button', name='Accept', exact=True).click()
        expect(approve).to_be_enabled()
        expect(approve).to_have_text('Approve rota')
        assert approve.get_attribute('aria-describedby') is None

        # Behind the modal dialog (inert): CSS locators, not roles.
        one_status(page, 'Waiting for approval')
        strip = page.locator('.review-strip')
        expect(strip.locator('.review-head > button[aria-label="About this review"]')).to_have_count(1)  # inline after "Review"
        expect(strip.locator('.review-step')).to_have_count(0)  # no coloured step pill
        facts = strip.locator('.review-facts .fact')
        expect(facts.filter(has_text=re.compile(r'^0 of 1 approvals$'))).to_have_count(1)
        overdue = strip.locator('.overdue')
        expect(overdue).to_have_text(re.compile(r'^Overdue since '))
        expect(overdue.locator('svg')).to_have_count(1)  # never colour alone
        assert '·' not in strip.locator('.review-facts').text_content()  # separators are CSS, never a dangling "·"
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_out_of_date_review_is_needs_resubmitting_only(browser, live_server, h, view, scheme):
    w = h.lifecycle(h.shift(h.workspace('Out of date review'))['workspace'], 'submit')['workspace']
    h.policy({'kind': 'max_hours', 'hours': 100, 'window': 'week'}, severity='advisory')  # inputs change after submit
    context, page = sign_in(browser, live_server, 'approver', viewport=view, color_scheme=scheme)
    try:
        navigate(page, 'schedule?approve=' + w['id'])
        bar = one_status(page, 'Needs resubmitting')
        expect(bar).not_to_contain_text('Waiting for approval')
        expect(page.get_by_role('dialog', name='Approve rota')).to_have_count(0)  # the server would refuse (STALE_INPUTS)
        expect(bar.get_by_role('button', name='Approve rota', exact=True)).to_have_count(0)
        expect(bar.get_by_role('button', name='Return to coordinator', exact=True)).to_have_class(re.compile(r'\bprimary\b'))
        help = page.locator('.review-strip .review-head').get_by_role('button', name='About this review')
        expect(help).to_have_attribute('data-tip', re.compile('can’t be approved'))
    finally:
        context.close()


def approved_rota(h, name):
    w = h.shift(h.workspace(name, '2026-10-04', '2026-10-11'))['workspace']
    w = h.lifecycle(w, 'submit')['workspace']
    return h.lifecycle(w, 'approve', 'approver')['workspace']


def planner_verdict(page, text, publish):
    bar = page.locator('.topbar .planner-bar')
    expect(bar.locator('.rota-status')).to_have_text(text)
    expect(bar.get_by_role('button', name='Publish…', exact=True)).to_have_count(1 if publish else 0)


def test_approved_stale_rota_keeps_one_verdict_while_switching_week_month_day_and_list(browser, live_server, h):
    w = approved_rota(h, 'Golden-like approved rota')
    h.policy({'kind': 'max_hours', 'hours': 100, 'window': 'week'})  # inputs change after approval
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        show(page, f'schedule/week/2026-10-04?edit={w["id"]}')
        for i, view in enumerate(('Week', 'Month', 'Day', 'List')):
            if i:
                pick_view(page, view)
            planner_verdict(page, 'Needs resubmitting', False)
        pick_view(page, 'Week')
        planner_verdict(page, 'Needs resubmitting', False)
    finally:
        context.close()


def test_approved_current_rota_offers_publish_in_every_planner_view(browser, live_server, h):
    w = approved_rota(h, 'Golden-like current rota')
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        show(page, f'schedule/week/2026-10-04?edit={w["id"]}')
        for i, view in enumerate(('Week', 'Month', 'Day', 'List')):
            if i:
                pick_view(page, view)
            planner_verdict(page, 'Approved', True)
    finally:
        context.close()


def test_approved_stale_rota_note_and_reopen_follow_the_actor(browser, live_server, h):
    w = approved_rota(h, 'Golden-like approved actor states')
    h.policy({'kind': 'max_hours', 'hours': 100, 'window': 'week'})  # inputs change after approval
    approver_context, approver = sign_in(browser, live_server, 'approver', viewport={'width': 1440, 'height': 900})
    coordinator_context, coordinator = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        show(approver, f'schedule/week/2026-10-04?edit={w["id"]}')
        approver_bar = approver.locator('.topbar .planner-bar')
        expect(approver_bar.locator('.rota-status')).to_have_text('Needs resubmitting')
        expect(approver_bar.get_by_role('button', name='Reopen as draft', exact=True)).to_have_count(0)
        note = approver_bar.locator('.health-pill.info').filter(has_text='Waiting for the coordinator')
        expect(note).to_be_visible()
        expect(note).to_have_attribute('data-tip', re.compile(r'The coordinator reopens it'))

        show(coordinator, f'schedule/week/2026-10-04?edit={w["id"]}')
        coordinator_bar = coordinator.locator('.topbar .planner-bar')
        expect(coordinator_bar.locator('.rota-status')).to_have_text('Needs resubmitting')
        expect(coordinator_bar.get_by_role('button', name='Reopen as draft', exact=True)).to_be_visible()
        expect(coordinator_bar.locator('.health-pill.info').filter(has_text='Waiting for the coordinator')).to_have_count(0)
    finally:
        approver_context.close()
        coordinator_context.close()

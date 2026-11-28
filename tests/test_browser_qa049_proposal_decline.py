"""maintenance (High, governance): an approver's decision on a waiting rule proposal never looks dead. On live, the proposal's
effect review (GET /api/policies/{id}/review) took over 120 s and "Review and approve" waited on it with nothing on
screen. The decision panel now opens at once; Decline needs no review; the effect loads into the panel. Here: a
coordinator proposes a change, the review read is held, a different person (the approver) declines it from the panel,
and the proposal is declined with its audit record. Runs in CI only."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, wait_held  # noqa: F401
from .test_browser_rule_catalogue import flag, catalogue_rule, line

pytestmark = pytest.mark.browser
REASON = 'Five in a row is too many for this team'


def proposal(h):
    """The coordinator proposes Days in a row ≤ 4 → ≤ 5 (the live maintenance change)."""
    flag(h)
    broad = line({'max': {'value': 4, 'level': 'mandatory'}})
    rule = catalogue_rule(h, 'days_in_a_row', [broad])
    lines = [{**broad, 'numbers': {'max': {'value': 5, 'level': 'mandatory'}}}]
    check = h.client('coordinator').post('/api/rules/catalogue/days_in_a_row/check', json={'lines': lines, 'settings': None}).json()
    record = h.command('coordinator', 'rule.propose', {'catalogue_id': 'days_in_a_row', 'lines': lines, 'reason': 'Team asked for five',
        'expected_policy_id': rule['id'], 'expected_revision': rule['revision'],
        'acknowledged_overlaps': [o['line_id'] for o in check['overlaps']]})['record']
    assert record['status'] == 'proposed'
    return rule, record


def test_the_decision_panel_opens_at_once_and_a_different_approver_declines(browser, live_server, h):
    rule, proposed = proposal(h)
    held = []
    context, page = sign_in(browser, live_server, 'approver'); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        # The effect review hangs, as on live: nothing may wait on it but Approve.
        page.route(re.compile(rf".*/api/policies/{proposed['id']}/review$"), lambda route: held.append(route))
        navigate(page, 'rules/days_in_a_row')
        page.locator('#main').get_by_role('button', name='Review and approve', exact=True).click()
        panel = page.locator('.rc-line-panel')
        expect(panel).to_be_visible()  # at once, before the review answers
        expect(panel.locator('.rc-effect-box [aria-busy=true]')).to_have_count(1)  # the shared skeleton: Checking the effect…
        approve = panel.locator('.rc-lp-foot .button.primary')  # relabelled "Checking the effect…" while it waits
        expect(approve).to_be_disabled()
        expect(approve).to_have_attribute('aria-busy', 'true')
        decline = panel.get_by_role('button', name='Decline…', exact=True)
        expect(decline).to_be_enabled()
        decline.click()
        dialog = page.get_by_role('dialog', name='Decline the change to Days in a row')
        dialog.get_by_label('Reason', exact=True).fill(REASON)
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and (r.request.post_data_json or {}).get('kind') == 'policy.decline') as sent:
            dialog.get_by_role('button', name='Decline', exact=True).click()
        assert sent.value.status == 200, sent.value.text()
        expect(page.locator('#toast-tray .toast', has_text='Proposal declined.')).to_be_visible()
        expect(panel).to_have_count(0)
        # Declined, by a different person than the one who proposed it, and audited with its reason.
        body = h.get('approver', '/api/policies?limit=200')
        row = next(p for p in (body['policies'] if isinstance(body, dict) else body) if p['id'] == proposed['id'])
        # A declined proposal is kept as a retired version that nobody approved (policies.py decline_policy).
        assert (row['status'], row['approved_by']) == ('retired', None), (row['status'], row.get('approved_by'))
        assert row['created_by'] != h.seed['identities']['approver']
        audit = h.get('auditor', '/api/audit?action=policy')['events']
        assert any(e['action'] == 'policy.decline' and e['target_id'] == proposed['id'] and REASON in str(e.get('reason', '')) for e in audit), \
            [(e['action'], e.get('target_id')) for e in audit[:10]]
        # The rule in force is untouched: still ≤ 4.
        entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'days_in_a_row')
        assert entry['proposed'] is None
        assert errors == []
    finally:
        for route in held:
            if not route.request.failure:
                route.abort()
        context.close()


def test_a_failed_review_read_says_so_with_retry_and_decline_still_works(browser, live_server, h):
    rule, proposed = proposal(h)
    context, page = sign_in(browser, live_server, 'approver')
    try:
        page.route(re.compile(rf".*/api/policies/{proposed['id']}/review$"),
                   lambda route: route.fulfill(status=503, json={'error': {'code': 'UNAVAILABLE', 'message': 'internal detail never shown'}}))
        navigate(page, 'rules/days_in_a_row')
        page.locator('#main').get_by_role('button', name='Review and approve', exact=True).click()
        panel = page.locator('.rc-line-panel')
        expect(panel.get_by_text('Couldn’t load the effect of this change.')).to_be_visible()
        expect(panel.get_by_text('internal detail never shown')).to_have_count(0)  # never the raw error
        expect(panel.get_by_role('button', name='Retry', exact=True)).to_be_visible()
        expect(panel.get_by_role('button', name='Decline…', exact=True)).to_be_enabled()
    finally:
        context.close()


def test_a_stale_effect_answer_never_reenables_approve(browser, live_server, h):
    """A Retry retires the first review: its late answer cannot release or paint over the second check."""
    _, proposed = proposal(h)
    base = h.get('approver', f"/api/policies/{proposed['id']}/review")

    def answer(summary):
        return {**base, 'summary': summary, 'ready': True, 'can_decide': True, 'requires_acknowledgement': False,
                'effect': [{**row, 'status': 'ready', 'effect': 'no_change', 'clears': 0, 'adds': 0,
                            'summary': summary} for row in base.get('effect', [])]}

    held = []

    def hold(route):
        held.append(route)

    context, page = sign_in(browser, live_server, 'approver')
    try:
        page.route(re.compile(rf".*/api/policies/{proposed['id']}/review$"), hold)
        navigate(page, 'rules/days_in_a_row')
        page.locator('#main').get_by_role('button', name='Review and approve', exact=True).click()
        panel = page.locator('.rc-line-panel')
        approve = panel.locator('.rc-lp-foot .button.primary')
        wait_held(page, held)
        expect(approve).to_be_disabled()
        expect(approve).to_have_attribute('aria-busy', 'true')
        retry = panel.get_by_role('button', name='Retry', exact=True)
        expect(retry).to_be_visible(timeout=20000)
        retry.click()
        expect(approve).to_be_disabled()
        expect(approve).to_have_attribute('aria-busy', 'true')
        assert page.evaluate('document.activeElement !== document.body')
        wait_held(page, held, count=2)

        with page.expect_response(lambda r: r.url == held[0].request.url):
            held[0].fulfill(json=answer('first answer'))
        expect(approve).to_be_disabled()
        expect(approve).to_have_attribute('aria-busy', 'true')
        expect(panel.get_by_text('first answer', exact=True)).to_have_count(0)

        with page.expect_response(lambda r: r.url == held[1].request.url):
            held[1].fulfill(json=answer('second answer'))
        expect(panel.get_by_text('second answer', exact=True)).to_be_visible()
        expect(panel.get_by_text('first answer', exact=True)).to_have_count(0)
    finally:
        for route in held:
            if not route.request.failure:
                try:
                    route.abort()
                except Exception:
                    pass
        context.close()


def test_a_retry_during_current_effect_release_cannot_start_a_new_check(browser, live_server, h):
    """A Retry attempted while the current answer releases its busy state cannot outdate that answer."""
    _, proposed = proposal(h)
    base = h.get('approver', f"/api/policies/{proposed['id']}/review")

    def answer(summary):
        return {**base, 'summary': summary, 'ready': True, 'can_decide': True, 'requires_acknowledgement': False,
                'effect': [{**row, 'status': 'ready', 'effect': 'no_change', 'clears': 0, 'adds': 0,
                            'summary': summary} for row in base.get('effect', [])]}

    held = []
    context, page = sign_in(browser, live_server, 'approver')
    try:
        page.route(re.compile(rf".*/api/policies/{proposed['id']}/review$"), lambda route: held.append(route))
        navigate(page, 'rules/days_in_a_row')
        page.clock.pause_at(page.evaluate('Date.now()') + 1000)
        # Make the busy indicator observe its full minimum hold even after the skeleton has offered Retry.
        page.evaluate("Object.defineProperty(performance, 'now', {value: () => 300, configurable: true})")
        page.locator('#main').get_by_role('button', name='Review and approve', exact=True).click()
        panel = page.locator('.rc-line-panel')
        approve = panel.locator('.rc-lp-foot .button.primary')
        wait_held(page, held)
        page.clock.run_for(15000)
        retry = panel.get_by_role('button', name='Retry', exact=True)
        expect(retry).to_be_visible(timeout=20000)
        retry_handle = retry.element_handle()
        assert retry_handle is not None
        with page.expect_response(lambda r: r.url == held[0].request.url):
            held[0].fulfill(json=answer('first answer'))
        # A response event can arrive before the app parses the body and retires the first Retry.
        expect(retry).to_have_count(0)
        retry_handle.evaluate('button => button.click()')
        expect(approve).to_be_disabled()
        expect(approve).to_have_attribute('aria-busy', 'true')
        page.clock.run_for(400)
        expect(panel.get_by_text('first answer', exact=True)).to_be_visible()
        assert len(held) == 1
    finally:
        for route in held:
            if not route.request.failure:
                try:
                    route.abort()
                except Exception:
                    pass
        context.close()


def test_the_panel_checks_two_drafts_at_a_time_and_none_after_it_closes(browser, live_server, h):
    """The drafts' effect checks: at most two queued at once, in the order listed (the server runs them one by one), and
    none started once the panel is closed. (maintenance probe: it queued all eleven, one after another, and went on after
    close.)"""
    rule, proposed = proposal(h)
    for n in range(3):
        h.shift(h.workspace(f'Effect draft {n}'))
    held = []

    def commands(route):
        if (route.request.post_data_json or {}).get('kind') == 'job.create':
            held.append(route)
        else:
            route.continue_()
    context, page = sign_in(browser, live_server, 'approver')
    try:
        page.route('**/api/commands', commands)
        navigate(page, 'rules/days_in_a_row')
        page.locator('#main').get_by_role('button', name='Review and approve', exact=True).click()
        panel = page.locator('.rc-line-panel')
        # Two at once; the route handler takes each as it is sent (both may leave before a request listener is on).
        wait_held(page, held, count=2)
        page.evaluate('() => new Promise(r => setTimeout(r, 0))')
        assert len(held) == 2, len(held)  # the third waits for one of them
        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(panel).to_have_count(0)
        for route in list(held):
            with page.expect_response(lambda r, u=route.request.url: r.url == u):
                route.continue_()
        page.evaluate('() => new Promise(r => setTimeout(r, 0))')  # the workers' next turn has run
        assert len(held) == 2, len(held)  # closed: the third was never started
    finally:
        for route in held:
            if not route.request.failure:
                try:
                    route.continue_()
                except Exception:
                    pass
        context.close()


def test_a_press_on_approve_while_the_effect_loads_says_why_then_activates(browser, live_server, h):
    """Issue 1157: a press on Approve and activate while the effect is still read was lost with nothing said. The button is
    held (aria-disabled, its reason in the tip), so a press, by pointer or by key, says why; once the effect is known
    the next press activates the change."""
    rule, proposed = proposal(h)
    held = []

    def review(route):
        if held:
            route.continue_()
        else:
            held.append(route)
    context, page = sign_in(browser, live_server, 'approver')
    try:
        page.route(re.compile(rf".*/api/policies/{proposed['id']}/review$"), review)
        navigate(page, 'rules/days_in_a_row')
        page.locator('#main').get_by_role('button', name='Review and approve', exact=True).click()
        panel = page.locator('.rc-line-panel')
        approve = panel.locator('.rc-lp-foot .button.primary')
        wait_held(page, held)
        expect(approve).to_be_disabled()
        expect(approve).to_have_attribute('aria-busy', 'true')
        expect(approve).to_have_attribute('data-tip', 'Waits until the effect of this change is known')
        why = page.locator('#toast-tray .toast', has_text='Waits until the effect of this change is known')
        approve.click(force=True)  # a real press on the held button, not waiting for it
        expect(why).to_be_visible()
        approve.focus()
        page.keyboard.press('Enter')
        expect(why.first).to_be_visible()
        entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'days_in_a_row')
        assert entry['proposed'] is not None  # nothing was activated while the effect was unknown
        with page.expect_response(lambda r: r.url == held[0].request.url):
            held[0].continue_()
        expect(approve).to_be_enabled(timeout=15000)  # the real review, once released
        expect(approve).not_to_have_attribute('aria-disabled', 'true')
        approve.click()
        expect(page.locator('#toast-tray .toast', has_text='Days in a row: the change is active.')).to_be_visible()
        expect(panel).to_have_count(0)
        entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'days_in_a_row')
        assert entry['proposed'] is None
    finally:
        for route in held:
            if not route.request.failure:
                try:
                    route.abort()
                except Exception:
                    pass
        context.close()

"""RUL-03 in the web client: an Undo of a retire or restore that needs another approver says it is waiting for
approval (who approved, how many more), never that it is done, and its own Undo withdraws that approval. With one
approval the same Undo still says it applied."""
import re

import pytest
from playwright.sync_api import expect

from .governance_support import identity
from .test_browser import browser, live_server, sign_in, navigate, open_rule  # noqa: F401
from .test_browser_autoschedule_budget import confirm
from .test_browser_rules_phase3 import ended_rule
from .rules_support import rule_status
from .test_bulk_reverse_transitions import proposal, transition
from .test_policy_transition_approvals import settings

pytestmark = pytest.mark.browser
WAITING = '(1 of 2) · 1 more approval needed from a different approver'


def status(h, row):
    return next(p for s in ('active', 'retired') for p in h.get('approver', '/api/policies?status=' + s)['policies']
                if p['id'] == row['id'])['status']


def retired_by_two(h):
    row = transition(h, 'approver', 'policy.activate', proposal(h))['record']
    settings(h, 2)
    identity(h, 'second_approver', ['approver'])
    transition(h, 'second_approver', 'policy.retire', row)
    return transition(h, 'approver', 'policy.retire', row)['record']


def test_restore_undo_waiting_for_a_second_approver_is_pending_and_withdrawn(browser, live_server, h):
    row = retired_by_two(h)
    transition(h, 'second_approver', 'policy.restore', row)
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        rule_status(page, 'History')
        sheet = open_rule(page, row['name'])
        sheet.get_by_role('button', name='Restore…', exact=True).click()
        confirm(page, 'Restore rule', 'Restore rule', 'Second of two approvals')
        restored = page.locator('.toast').filter(has_text='Rule restored.')
        restored.get_by_role('button', name='Undo', exact=True).click()
        waiting = page.locator('.toast').filter(has_text='Retiring approved by you ' + WAITING)
        expect(waiting).to_be_visible()
        expect(page.get_by_text('Rule retired again.', exact=True)).to_have_count(0)
        assert status(h, row) == 'active'
        waiting.get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Approval withdrawn. The rule is unchanged.', exact=True)).to_be_visible()
        active = next(p for p in h.get('approver', '/api/policies?status=active')['policies'] if p['id'] == row['id'])
        # Withdrawn: the second approver's vote alone is one of two.
        assert not transition(h, 'second_approver', 'policy.retire', active)['approval']['complete']
    finally:
        context.close()


def test_group_retirement_undo_waiting_for_a_second_approver_is_pending_and_withdrawn(browser, live_server, h):
    old = ended_rule(h)
    settings(h, 2)
    identity(h, 'second_approver', ['approver'])
    transition(h, 'second_approver', 'policy.retire', old, payload={'reason': 'Ended: first of two approvals'})
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Group similar rules').click()
        ended = page.get_by_role('dialog', name='Group similar rules').locator('.group-ended')
        ended.get_by_role('button', name='Retire Old rest rule').click()  # D42: reversible, acts at once with Undo
        expect(ended.locator('.badge', has_text='Retired')).to_be_visible()
        assert status(h, old) == 'retired'
        ended.get_by_role('button', name='Undo retiring Old rest rule').click()
        expect(ended.locator('.badge', has_text='Waiting for approval')).to_be_visible()
        expect(ended).to_contain_text('Restoring approved by you ' + WAITING)
        expect(ended).not_to_contain_text(re.compile('restored', re.I))
        assert status(h, old) == 'retired'
        ended.get_by_role('button', name='Withdraw approval for Old rest rule').click()
        expect(ended.locator('.badge', has_text='Retired')).to_be_visible()
        retired = next(p for p in h.get('approver', '/api/policies?status=retired')['policies'] if p['id'] == old['id'])
        assert not transition(h, 'second_approver', 'policy.restore', retired)['approval']['complete']
    finally:
        context.close()


def test_restore_undo_with_one_approval_still_says_retired_again(browser, live_server, h):
    row = transition(h, 'approver', 'policy.retire', transition(h, 'approver', 'policy.activate', proposal(h))['record'])['record']
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        rule_status(page, 'History')
        open_rule(page, row['name']).get_by_role('button', name='Restore…', exact=True).click()
        confirm(page, 'Restore rule', 'Restore rule', 'Restored by one approver')
        page.locator('.toast').filter(has_text='Rule restored.').get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Rule retired again.', exact=True)).to_be_visible()
        assert status(h, row) == 'retired'
    finally:
        context.close()

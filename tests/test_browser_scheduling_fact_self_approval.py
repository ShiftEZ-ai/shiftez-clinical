"""Requests offers own-fact approval only with permission and records a real browser decision."""
import pytest
from playwright.sync_api import expect

from .governance_support import identity
from .test_browser import browser, live_server, navigate, whole_list_if_waiting  # noqa: F401
from .test_browser_requests import rows
from .test_browser_rule_catalogue_v2 import reason, themed
from .test_scheduling_facts_api import workload

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('permitted', [False, True], ids=['independent-approval-required', 'self-approval-permitted'])
def test_own_fact_review_and_approval_follow_server_eligibility(browser, live_server, h, permitted):
    reviewer = identity(h, 'both', ['coordinator', 'approver'], person=h.seed['people'][0])
    fact = h.command('both', 'person.workload.propose', workload(h, 3))['fact']
    permission = None
    if permitted:
        permission = h.command('admin', 'self_approval.grant', {
            'identity_id': reviewer, 'actions': ['rule.activate'], 'starts_on': '2026-01-01', 'ends_on': '2027-01-01',
            'reason': 'Explicit synthetic scheduling-fact permission'})['permission']
    context, page = themed(browser, live_server, 'both')
    try:
        navigate(page, 'requests')
        whole_list_if_waiting(page)  # a decided fact stays listed; Waiting for you drops it (998bbca16)
        row = rows(page).filter(has_text='Personal workload')
        expect(row).to_contain_text('At most 3 duties per calendar week · Mandatory')
        expect(row.get_by_role('button', name='Approve', exact=True)).to_have_count(int(permitted))
        row.locator('.request-title strong').click()
        panel = page.get_by_role('region', name='Personal workload')
        expect(panel).to_contain_text('At most 3 duties per calendar week · Mandatory')
        expect(panel.locator('.request-history')).to_contain_text('Proposed')
        approve = panel.get_by_role('button', name='Approve', exact=True)
        expect(approve).to_have_count(int(permitted))
        if not permitted:
            expect(panel.get_by_role('button', name='Decline', exact=True)).to_have_count(0)
            expect(panel.locator('.request-status')).to_contain_text('You proposed it; another approver decides')
            assert [f['id'] for f in h.get('auditor', f"/api/people/{fact['person_id']}/scheduling-facts")['proposed']] == [fact['id']]
            return
        approve.click()
        review = page.get_by_role('dialog', name='Review Personal workload', exact=True)
        expect(review.get_by_role('button', name='Approve', exact=True)).to_be_visible()
        decision_reason = 'Reviewed the synthetic workload with independent cover by phone'
        reason(page, decision_reason, 'Approve')
        expect(review).to_have_count(0)
        expect(page.locator('#toast-tray .toast', has_text='Change approved.')).to_be_visible()
        # The decided fact stays listed on the whole list, now Approved and no longer decidable. The row is first seen
        # Approved: the refresh redraws the list, and a missing row would satisfy the button check alone.
        expect(row).to_contain_text('Approved')
        expect(row.get_by_role('button', name='Approve', exact=True)).to_have_count(0)
        facts = h.get('auditor', f"/api/people/{fact['person_id']}/scheduling-facts")
        assert not facts['proposed']
        assert [(f['id'], f['approved_by']) for f in facts['active']] == [(fact['id'], reviewer)]
        event = next(e for e in h.get('auditor', '/api/audit?self_approved=true')['events']
                     if e['action'] == 'person.workload.activate' and e['target_id'] == fact['id'])
        assert event['reason'] == decision_reason
        assert event['detail']['self_approved'] == [{'action': 'rule.activate', 'permission_id': permission['id']}]
    finally:
        context.close()

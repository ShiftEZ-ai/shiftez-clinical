"""Governance changes have Undo like every other change: self-approval permissions and review delegations."""
from datetime import timedelta

import pytest
from playwright.sync_api import expect

from packages.domain.time import now as clock_now
from .governance_support import identity, route
from .test_browser import browser, live_server, sign_in, navigate, save_dialog, holding  # noqa: F401

pytestmark = pytest.mark.browser


def undo(page, said):
    toast = page.locator('#toast-tray .toast').filter(has_text=said)
    toast.get_by_role('button', name='Undo', exact=True).click()


def test_self_approval_permission_added_and_ended_are_undone(browser, live_server, h):
    identity(h, 'both', ['coordinator', 'approver'])
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'access/routes')
        page.get_by_role('button', name='Add permission', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Person', exact=True).select_option(h.seed['identities']['both'])
        form.get_by_label('Activate a rule they proposed', exact=True).check()
        form.get_by_label('From', exact=True).fill('2020-01-01')
        form.get_by_label('Reason', exact=True).fill('Sole reviewer this month')
        save_dialog(form, 'Add permission')
        undo(page, 'Permission added.')
        expect(page.get_by_text('Permission ended again.')).to_be_visible()
        [first] = h.get('admin', '/api/self-approval')['permissions']
        assert first['revoked'] and first['state'] == 'ended'
        assert h.get('both', '/api/me')['self_approval'] == []

        # Ending a permission is undone by the server's restore: the same subject, actions, dates, reason and creator.
        kept = h.command('admin', 'self_approval.grant', {'identity_id': h.seed['identities']['both'], 'actions': ['rule.activate'],
                         'starts_on': '2026-01-01', 'ends_on': '2027-01-01', 'reason': 'Sole reviewer again'})['permission']
        page.reload()
        page.get_by_role('button', name='More actions for Fictional both', exact=True).first.click()
        page.get_by_role('menuitem', name='End permission', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Reason', exact=True).fill('Second approver joined')
        save_dialog(form, 'End permission')
        undo(page, 'Permission ended.')
        expect(page.get_by_text('Permission restored.')).to_be_visible()
        rows = h.get('admin', '/api/self-approval')['permissions']
        restored = [p for p in rows if p['state'] == 'active']
        assert len(restored) == 1 and restored[0]['id'] != kept['id']
        assert (restored[0]['identity_id'], restored[0]['actions'], restored[0]['starts_on'], restored[0]['ends_on']) == (kept['identity_id'], kept['actions'], kept['starts_on'], kept['ends_on'])
        assert h.get('both', '/api/me')['self_approval'] == ['rule.activate']
        assert (restored[0]['reason'], restored[0]['created_by']) == (kept['reason'], kept['created_by'])  # restored, not regranted
        reviewed = page.get_by_role('row').filter(has_text='Sole reviewer again')
        expect(reviewed.filter(has_text='Ended')).to_have_count(1)
        expect(reviewed.filter(has_text='Active')).to_have_count(1)
    finally:
        context.close()


def test_review_delegation_revoked_and_recorded_are_undone(browser, live_server, h):
    identity(h, 'programme', ['programme_lead'])
    identity(h, 'delegate', ['approver'])
    review_route = route(h, [{'key': 'programme', 'label': 'Programme review', 'role': 'programme_lead'}])
    start = clock_now() - timedelta(minutes=1)
    h.command('programme', 'review_delegation.create', {'route_id': review_route['id'], 'step_key': 'programme',
              'delegate_id': h.seed['identities']['delegate'], 'starts_at': start.isoformat(),
              'expires_at': (start + timedelta(days=1)).isoformat(), 'reason': 'Leave cover'})
    context, page = sign_in(browser, live_server, 'programme')
    try:
        navigate(page, 'access/routes')
        row = page.get_by_role('row').filter(has_text='Fictional delegate')
        expect(row).to_contain_text('Active')
        expect(row.get_by_role('cell').first).to_have_text('Programme review')  # the step's label, not its key
        row.get_by_role('button', name='Revoke delegation', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Reason', exact=True).fill('Back from leave early')
        save_dialog(form, 'Confirm')
        undo(page, 'Delegation revoked.')
        expect(page.get_by_text('Delegation restored.')).to_be_visible()
        delegations = h.get('admin', '/api/review-delegations')['delegations']
        assert sorted(d['revoked'] for d in delegations) == [False, True]
        again = next(d for d in delegations if not d['revoked'])
        assert (again['route_id'], again['step_key'], again['delegate_id'], again['delegator_id']) == (
            review_route['id'], 'programme', h.seed['identities']['delegate'], h.seed['identities']['programme'])
        expect(page.get_by_role('row').filter(has_text='Active')).to_have_count(1)

        # Recording one is undone by revoking it.
        live = len([d for d in delegations if not d['revoked']])
        page.get_by_role('button', name='Delegate review step', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Delegate reviewer', exact=True).select_option(h.seed['identities']['delegate'])
        form.get_by_label('Delegation reason', exact=True).fill('Second cover period')
        # The page refresh after the save is held: the save's own message still owns its Undo meanwhile (a slow refresh
        # used to let the generic "Delegation recorded." fallback claim it).
        with holding(page) as held:
            held.hold('**/api/me')
            form.get_by_role('button', name='Record delegation', exact=True).click()
            recorded = page.locator('#toast-tray .toast').filter(has_text='Review delegation recorded.')
            expect(recorded.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        expect(form).not_to_be_visible()
        expect(page.locator('#toast-tray .toast').filter(has_text='Delegation recorded.')).to_have_count(1)
        undo(page, 'Review delegation recorded.')
        expect(page.get_by_text('Delegation revoked.', exact=True)).to_be_visible()
        assert len([d for d in h.get('admin', '/api/review-delegations')['delegations'] if not d['revoked']]) == live
    finally:
        context.close()


def test_admin_undo_of_a_lead_delegation_revocation_restores_the_original_delegator(browser, live_server, h):
    """An admin revokes a programme lead's delegation and presses Undo: the server restores it from that record (the
    lead stays the delegator, same route revision, step, delegate and dates); the admin never becomes the delegator."""
    identity(h, 'programme', ['programme_lead'])
    identity(h, 'delegate', ['approver'])
    review_route = route(h, [{'key': 'programme', 'label': 'Programme review', 'role': 'programme_lead'}])
    start = clock_now() - timedelta(minutes=1)
    original = h.command('programme', 'review_delegation.create', {'route_id': review_route['id'], 'step_key': 'programme',
                         'delegate_id': h.seed['identities']['delegate'], 'starts_at': start.isoformat(),
                         'expires_at': (start + timedelta(days=1)).isoformat(), 'reason': 'Leave cover'})['delegation']
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'access/routes')
        row = page.get_by_role('row').filter(has_text='Fictional delegate')
        expect(row).to_contain_text('Active')
        row.get_by_role('button', name='Revoke delegation', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Reason', exact=True).fill('Lead asked to end it')
        save_dialog(form, 'Confirm')
        undo(page, 'Delegation revoked.')
        expect(page.get_by_text('Delegation restored.')).to_be_visible()
        delegations = h.get('admin', '/api/review-delegations')['delegations']
        again = next(d for d in delegations if not d['revoked'])
        assert again['id'] != original['id'] and again['delegator_id'] == h.seed['identities']['programme']
        assert {k: again[k] for k in ('route_id', 'route_revision', 'step_key', 'delegate_id', 'starts_at', 'expires_at')} ==             {k: original[k] for k in ('route_id', 'route_revision', 'step_key', 'delegate_id', 'starts_at', 'expires_at')}
        expect(page.get_by_role('row').filter(has_text='Fictional delegate').filter(has_text='Active')).to_have_count(1)
    finally:
        context.close()


def test_add_permission_says_when_people_cannot_load_and_retries(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    failed = []

    def fail_once(route):
        if not failed:
            failed.append(route.request.url)
            route.fulfill(status=503, content_type='application/json',
                          body='{"error":{"code":"UNAVAILABLE","message":"Service unavailable."}}')
        else:
            route.continue_()
    try:
        navigate(page, 'access/routes')
        page.route('**/api/access', fail_once)
        page.get_by_role('button', name='Add permission', exact=True).click()
        toast = page.locator('#toast-tray .toast.error').filter(has_text='Couldn’t load the people to choose from.')
        expect(toast).to_be_visible()
        expect(page.get_by_role('dialog', name='Add a self-approval permission')).to_have_count(0)
        toast.get_by_role('button', name='Retry', exact=True).click()
        form = page.get_by_role('dialog', name='Add a self-approval permission')
        expect(form.get_by_label('Person', exact=True)).to_be_visible()
        assert len(failed) == 1
    finally:
        context.close()
# Include this file in hosted supervision and team-lead qualification.

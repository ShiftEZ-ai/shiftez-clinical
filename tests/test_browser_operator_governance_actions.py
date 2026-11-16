"""Actual authority-changing UI actions with independent saved-state checks.

Fictional source duties, routes and identities are explicit API/database fixtures.
The delegation, revocations and refused decisions use real HTTP Browser actions.
These cases do not qualify institutional identity providers or notification delivery.
"""
from packages.domain.time import now as clock_now
from datetime import datetime, timedelta, timezone

import re

import pytest
from playwright.sync_api import expect

from .governance_support import developer, identity, route
from .test_browser import expect_loaded, browser, live_server, sign_in, navigate, save_dialog, decide, open_rota  # noqa: F401

pytestmark = pytest.mark.browser


def reload_view(page, key):
    page.reload()
    expect_loaded(page, key)
    expect(page.locator('#main [role=alert]')).to_have_count(0)


def command_response(kind):
    return lambda response: (
        response.url.endswith('/api/commands')
        and response.request.method == 'POST'
        and response.request.post_data_json.get('kind') == kind
    )


def issued_evidence(h, publication_id):
    """The publication as read, without the live absence overlay's read time (its fingerprint still covers its content)."""
    read = h.get('coordinator', '/api/publications/' + publication_id)
    return {**read, 'operational': {k: v for k, v in (read.get('operational') or {}).items() if k != 'as_of'}}


def test_exact_step_delegated_review_then_ui_revoke_blocks_publication_without_changing_snapshot(browser, live_server, h):
    # The existing immutable publication is an API fixture, not a claimed UI setup.
    issued = h.publish(h.shift(h.workspace('Fictional existing issued schedule'))['workspace'])
    original_id = issued['publication_id']
    original = issued_evidence(h, original_id)
    identity(h, 'programme', ['programme_lead'])
    identity(h, 'delegate', ['approver'])
    review_route = route(h, [
        {'key': 'programme', 'label': 'Independent programme review', 'role': 'programme_lead'},
        {'key': 'clinical', 'label': 'Independent clinical review', 'role': 'approver'},
    ])
    candidate = h.workspace('Fictional delegated amendment', base=original_id)
    candidate = h.lifecycle(candidate, 'submit')['workspace']
    contexts = []
    try:
        owner_context, owner = sign_in(browser, live_server, 'programme'); contexts.append(owner_context)
        navigate(owner,'access/routes')
        owner.get_by_role('button', name='Delegate review step', exact=True).click()
        form = owner.get_by_role('dialog')
        form.get_by_label('Route and review step', exact=True).select_option(review_route['id'] + '|programme')
        form.get_by_label('Delegate reviewer', exact=True).select_option(h.seed['identities']['delegate'])
        local_now = clock_now().astimezone(timezone(timedelta(hours=3)))
        starts = (local_now - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M')
        expires = (local_now + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        form.get_by_label('Delegation starts', exact=True).fill(starts)
        form.get_by_label('Delegation expires', exact=True).fill(expires)
        form.get_by_label('Delegation reason', exact=True).fill('Fictional one-step leave cover')
        save_dialog(form, 'Record delegation')
        delegation = h.get('admin', '/api/review-delegations')['delegations'][0]
        assert (delegation['route_id'], delegation['step_key'], delegation['delegator_id'], delegation['delegate_id']) == (
            review_route['id'], 'programme', h.seed['identities']['programme'], h.seed['identities']['delegate'])
        for key, value in [('starts_at', starts), ('expires_at', expires)]:
            assert datetime.fromisoformat(delegation[key]) == datetime.fromisoformat(value + '+03:00')
        assert not delegation['revoked']
        assert h.detail(candidate)['workspace']['status'] == 'submitted'
        reload_view(owner,'access/routes')
        expect(owner.get_by_role('row').filter(has_text='Fictional delegate')).to_contain_text('Active')

        delegate_context, delegate = sign_in(browser, live_server, 'delegate'); contexts.append(delegate_context)
        navigate(delegate, 'planner')
        open_rota(delegate, candidate['id'])
        # SX-17: an amendment (based on a publication) is approved as changes.
        delegate.get_by_role('button', name='Approve changes', exact=True).click()
        approval = delegate.get_by_role('dialog')
        approval.get_by_label('Review authority', exact=True).select_option(delegation['id'])
        approval.get_by_label('Note', exact=True).fill('Review only the delegated programme step')
        save_dialog(approval, 'Approve changes')
        after_first = h.detail(candidate)
        assert after_first['workspace']['status'] == 'submitted'
        assert after_first['review_progress']['pending_step']['key'] == 'clinical'

        reviewer_context, reviewer = sign_in(browser, live_server, 'approver'); contexts.append(reviewer_context)
        navigate(reviewer, 'planner')
        open_rota(reviewer, candidate['id'])
        decide(reviewer, 'Approve changes')
        approved = h.detail(candidate)['workspace']
        assert approved['status'] == 'approved'

        owner.get_by_role('button', name='Revoke delegation', exact=True).click()
        revoke = owner.get_by_role('dialog')
        revoke.get_by_label('Reason', exact=True).fill('Fictional delegated cover ended before publication')
        save_dialog(revoke, 'Confirm')
        ended = next(d for d in h.get('admin', '/api/review-delegations')['delegations'] if d['id'] == delegation['id'])
        assert ended['revoked'] and ended['revision'] == delegation['revision'] + 1
        reload_view(owner,'access/routes')
        expect(owner.get_by_role('row').filter(has_text='Fictional delegate')).to_contain_text('Revoked')

        publisher_context, publisher = sign_in(browser, live_server, 'coordinator'); contexts.append(publisher_context)
        navigate(publisher, 'planner')
        open_rota(publisher, candidate['id'])
        publisher.get_by_role('button', name='Publish…', exact=True).click()
        publication = publisher.get_by_role('dialog', name=re.compile('^Publish '))
        publication.get_by_label('Note (optional)', exact=True).fill('Attempt to publish after delegated authority ended')
        publication.get_by_role('button', name='Publish', exact=True).click()
        with publisher.expect_response(command_response('workspace.publish')) as pending:
            publication.locator('button.armed').click()
        response = pending.value
        assert response.status == 409
        error = response.json()['error']
        assert error['code'] == 'DELEGATION_INACTIVE'
        expect(publication.get_by_role('alert')).to_contain_text(error['message'])
        expect(publication).to_be_visible()
        unchanged = h.detail(candidate)['workspace']
        assert unchanged['status'] == 'approved' and unchanged['revision'] == approved['revision']
        assert [p['id'] for p in h.get('coordinator', '/api/publications')['publications']] == [original_id]
        assert issued_evidence(h, original_id) == original
    finally:
        for context in contexts:
            context.close()


def test_ui_role_revoke_persists_and_open_operator_session_loses_authority(browser, live_server, h):
    operator_id = h.seed['identities']['operator']
    binding = next(b for b in h.get('admin', '/api/access')['bindings']
                   if b['identity_id'] == operator_id and b['role'] == 'operator' and not b['revoked'])
    operator_context, operator = sign_in(browser, live_server, 'operator')
    # Only a Developer revokes Developer access, and another live Developer must remain.
    admin_context, admin = sign_in(browser, live_server, developer(h))
    try:
        navigate(operator,'system/operations')
        # Retry an expired keep-alive socket once; HTTP status and authority assertions remain strict.
        assert operator.request.get(live_server + '/api/operations', max_retries=1).status == 200
        navigate(admin,'access/people')
        who = next(i['display_name'] for i in h.get('admin', '/api/access')['identities'] if i['id'] == operator_id)
        admin.get_by_role('button', name='More actions for ' + who, exact=True).first.click()
        with admin.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.post_data_json.get('kind') == 'role.revoke'):
            admin.get_by_role('menuitem', name='Remove Developer role', exact=True).click()
        saved = next(b for b in h.get('admin', '/api/access')['bindings'] if b['id'] == binding['id'])
        assert saved['revoked'] and saved['revision'] == binding['revision'] + 1
        reload_view(admin,'access/people')
        admin.get_by_role('button', name='More actions for ' + who, exact=True).first.click()
        expect(admin.get_by_role('menuitem', name='Remove Developer role', exact=True)).to_have_count(0)
        admin.keyboard.press('Escape')
        admin.get_by_role('tab', name='Role history').click()
        history = admin.get_by_role('tabpanel')
        # Grouped by person, the person is the group heading (AC-02): search narrows the table to them.
        history.get_by_role('searchbox', name='Search access history', exact=True).fill(who)
        expect(history.locator('tbody tr[data-row]').filter(has_text='Developer').filter(has_text='Revoked')).to_have_count(1)
        # Same already-authenticated Browser context/cookies, with no new login.
        forbidden = operator.request.get(live_server + '/api/operations', max_retries=1)
        assert forbidden.status == 403
        assert forbidden.json()['error']['code'] == 'FORBIDDEN'
        assert h.get('developer', '/api/operations')['metrics'] is not None
    finally:
        operator_context.close()
        admin_context.close()


def test_ui_end_self_approval_permission_reloads_ended_and_refuses_own_rule_activation(browser, live_server, h):
    identity(h, 'both', ['coordinator', 'approver'])
    admin_context, admin = sign_in(browser, live_server, 'admin')
    owner_context = None
    try:
        navigate(admin,'access/routes')
        admin.get_by_role('button', name='Add permission', exact=True).click()
        form = admin.get_by_role('dialog')
        form.get_by_label('Person', exact=True).select_option(h.seed['identities']['both'])
        form.get_by_label('Activate a rule they proposed', exact=True).check()
        # D4: a new permission ends a year after its start by default; start it now so it is in force.
        form.get_by_label('From', exact=True).fill(clock_now().date().isoformat())
        form.get_by_label('Reason', exact=True).fill('Fictional bounded sole-reviewer permission')
        save_dialog(form, 'Add permission')
        permission = h.get('admin', '/api/self-approval')['permissions'][0]
        assert permission['identity_id'] == h.seed['identities']['both']
        assert permission['actions'] == ['rule.activate'] and permission['state'] == 'active'
        proposal = h.command('both', 'policy.create', {
            'name': 'Fictional rule awaiting independent activation', 'owner': 'Fictional review team',
            'source': 'Browser authority lifecycle fixture', 'starts_on': '2020-01-01',
            'definition': {'rule': {'kind': 'min_rest', 'hours': 11}}})['record']
        owner_context, owner = sign_in(browser, live_server, 'both')
        assert h.get('both', '/api/me')['self_approval'] == ['rule.activate']

        admin.get_by_role('button', name='More actions for Fictional both', exact=True).click()
        admin.get_by_role('menuitem', name='End permission', exact=True).click()
        form = admin.get_by_role('dialog')
        form.get_by_label('Reason', exact=True).fill('Independent reviewer returned; exception ended')
        save_dialog(form, 'End permission')
        ended = next(p for p in h.get('admin', '/api/self-approval')['permissions'] if p['id'] == permission['id'])
        assert ended['revoked'] and ended['state'] == 'ended' and ended['revision'] == permission['revision'] + 1
        reload_view(admin,'access/routes')
        expect(admin.get_by_role('row').filter(has_text='Fictional both')).to_contain_text('Ended')
        expect(admin.get_by_role('button', name='More actions for Fictional both', exact=True)).to_have_count(0)
        assert h.get('both', '/api/me')['self_approval'] == []

        # The owner's pre-existing session still has clinical review role, but
        # its ended exception cannot authorise a decision on its own proposal.
        navigate(owner, 'rules')
        owner.get_by_role('button', name='Review', exact=True).click()
        review = owner.get_by_role('dialog')
        review.get_by_label('Reason', exact=True).fill('Attempt own activation after exception ended')
        with owner.expect_response(command_response('policy.activate')) as pending:
            review.get_by_role('button', name='Activate', exact=True).click()
        response = pending.value
        assert response.status == 403
        error = response.json()['error']
        assert error['code'] == 'SELF_APPROVAL'
        expect(owner.get_by_role('alert').filter(has_text=error['message'])).to_be_visible()
        expect(review).to_be_visible()
        saved = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['id'] == proposal['id'])
        assert saved['status'] == 'proposed' and saved['revision'] == proposal['revision']
        assert h.get('coordinator', '/api/publications')['publications'] == []
    finally:
        if owner_context:
            owner_context.close()
        admin_context.close()

"""Self-approval permissions in the browser: an admin adds and ends one; approvers see it; the history marks each use."""
import copy
import json

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, route_fetch  # noqa: F401
from .governance_support import developer, identity

pytestmark = pytest.mark.browser


def test_admin_adds_and_ends_a_permission_and_approvers_can_see_it(browser, live_server, h):
    # Read-only roles see the policy even when nothing is set up (no add button, no admin advice).
    context, page = sign_in(browser, live_server, 'auditor')
    try:
        navigate(page,'access/routes')
        expect(page.get_by_role('heading', name='Self-approval permissions')).to_be_visible()
        expect(page.get_by_text('Nobody can approve their own work')).to_be_visible()
        expect(page.get_by_role('button', name='Add permission', exact=True)).to_have_count(0)
        # The Built-in independent approval card says what is in use; the footer does not repeat it.
        expect(page.locator('.route-default')).to_contain_text('Built-in independent approval')
        expect(page.locator('.app-footer')).not_to_contain_text('Built-in independent approval')
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'admin')
    grants = []

    def grant(route):  # what the client sends; the real server stores it
        body = route.request.post_data_json or {}
        if body.get('kind') == 'self_approval.grant':
            grants.append(copy.deepcopy(body['payload']))
        route.fallback()
    try:
        page.route('**/api/commands', grant)
        navigate(page,'access/routes')
        expect(page.get_by_text('Nobody can approve their own work')).to_be_visible()
        # D5: Urgent is offered only while the rota route in force has urgent steps; setting it up is a route proposal.
        urgent = page.locator('.urgent-route')
        expect(urgent).to_contain_text('Off')
        expect(urgent).to_contain_text('Urgent is not offered on published rotas')
        urgent.get_by_role('button', name='Set up…', exact=True).click()
        route_form = page.get_by_role('dialog', name='Propose review route', exact=True)
        expect(route_form.get_by_label('Enable urgent amendment route', exact=True)).to_be_checked()
        expect(route_form.get_by_role('group', name='Urgent amendment steps')).to_be_visible()
        route_form.get_by_role('button', name='Cancel', exact=True).click()
        expect(route_form).to_have_count(0)
        page.get_by_role('button', name='Add permission', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Applies to', exact=True).select_option('role')
        # maintenance / SW3-B-18: only work the role can do is offered; admins propose and activate rules.
        form.get_by_label('Role', exact=True).select_option('admin')
        form.get_by_label('Activate a rule they proposed', exact=True).check()
        # D4: an end date by default (the server's default length); no end is an explicit choice.
        until = form.get_by_role('group', name='Dates', exact=True).get_by_label('Until (included)', exact=True)
        expect(until).not_to_have_value('')
        form.get_by_label('No end date', exact=True).check()
        expect(until).to_be_disabled()
        form.get_by_label('Reason', exact=True).fill('Only one approver this quarter')
        form.get_by_role('button', name='Add permission', exact=True).click()
        expect(page.get_by_text('Permission added.')).to_be_visible()
        assert grants[-1]['no_end_date'] is True and grants[-1]['ends_on'] is None
        row = page.locator('.dt-table tbody tr').filter(has_text='Everyone with the admin role')
        expect(row).to_contain_text('Activate a rule they proposed')
        expect(row).to_contain_text('No end date')
        # The shared date range: Until is the last included day (stored as the day after).
        page.get_by_role('button', name='Add permission', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Applies to', exact=True).select_option('role')
        form.get_by_label('Role', exact=True).select_option('auditor')
        expect(form.get_by_label('Activate a rule they proposed', exact=True)).to_be_hidden()
        form.get_by_label('Approve a rota they built or changed', exact=True).check()
        dates = form.get_by_role('group', name='Dates', exact=True)
        dates.get_by_label('Until (included)', exact=True).fill('2026-12-31')
        form.get_by_label('Reason', exact=True).fill('Year-end cover')
        form.get_by_role('button', name='Add permission', exact=True).click()
        expect(form).to_have_count(0)
        added = next(p for p in h.get('admin', '/api/self-approval')['permissions'] if p.get('role') == 'auditor')
        assert added['ends_on'] == '2027-01-01'
        expect(page.locator('.dt-table tbody tr').filter(has_text='Everyone with the auditor role')).to_contain_text('31 Dec 2026')
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page,'access/routes')
        expect(page.locator('.dt-table tbody')).to_contain_text('Everyone with the admin role')
        expect(page.get_by_role('button', name='Add permission', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_history_marks_decisions_on_your_own_work(browser, live_server, h):
    identity(h, 'both', ['coordinator', 'approver'])
    h.command('admin', 'self_approval.grant', {'identity_id': h.seed['identities']['both'], 'actions': ['rule.activate'],
              'starts_on': '2026-01-01', 'ends_on': '2027-01-01', 'reason': 'Solo cover'})
    rule = h.command('both', 'policy.create', {'name': 'Own rule', 'owner': 'QA', 'source': 'Browser test', 'starts_on': '2020-01-01',
                     'definition': {'rule': {'kind': 'min_rest', 'hours': 11}}})['record']
    h.command('both', 'policy.activate', {'reason': 'Checked with the lead'}, rule['id'], rule['revision'])
    context, page = sign_in(browser, live_server, developer(h))
    try:
        navigate(page,'system/audit')
        page.get_by_role('button', name='Filter', exact=True).click()
        page.get_by_role('dialog', name='Filter').get_by_label('Kind of change', exact=True).select_option('self')
        page.keyboard.press('Escape')
        rows = page.locator('.audit-row')
        expect(rows).to_have_count(1)
        expect(rows.first).to_contain_text('Self-approved')
        expect(rows.first).to_contain_text('Checked with the lead')
    finally:
        context.close()

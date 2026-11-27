"""A person who decides their own request under a self-approval permission is told so before deciding, and the decided
request says so afterwards: in its row and in its history, with the same Self-approved badge a rota decision wears."""
import pytest
from playwright.sync_api import expect

from .governance_support import identity
from .test_availability_patterns import pattern
from .test_browser import browser, live_server, sign_in, navigate, whole_list  # noqa: F401
from .test_request_decide_many import request_row
from .test_request_self_approved_mark import GRANT
from .test_rotation_leave_policy import resident

pytestmark = pytest.mark.browser


def test_a_self_approved_request_decision_says_so_in_the_dialog_row_and_history(browser, live_server, h):
    person = h.seed['people'][0]
    owner = identity(h, 'pattern_owner', ['staff', 'coordinator'], person=person)
    h.command('admin', 'self_approval.grant', {'identity_id': owner, **GRANT})
    made = h.command('pattern_owner', 'availability_pattern.propose', pattern(person))['pattern']
    context, page = sign_in(browser, live_server, 'pattern_owner')
    try:
        navigate(page, 'requests')
        whole_list(page)
        listed = page.locator(f'#main [data-row="{made["id"]}"]')
        expect(listed).to_have_count(1)
        listed.get_by_role('button', name='Approve', exact=True).click()
        dialog = page.get_by_role('dialog')
        # Said before it is done: deciding your own request is a self-approval.
        expect(dialog.get_by_text('Self-approved', exact=True)).to_be_visible()
        dialog.get_by_label('Reason', exact=True).fill('Reviewed with a colleague by phone')
        dialog.get_by_role('button', name='Approve', exact=True).click()
        expect(dialog).to_be_hidden()
        # The decided request keeps the mark in its row and in its history.
        decided = page.locator(f'#main [data-row="{made["id"]}"]')
        expect(decided.get_by_text('Self-approved', exact=True)).to_be_visible()
        decided.locator('td[data-col=type], .dt-card-title').first.click()
        history = page.locator('.request-panel .request-history')
        expect(history.get_by_text('Self-approved', exact=True)).to_have_count(1)
    finally:
        context.close()


def test_an_independent_request_decision_wears_no_self_approved_badge(browser, live_server, h):
    person = h.seed['people'][0]
    made = h.command('staff', 'availability_pattern.propose', pattern(person))['pattern']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        whole_list(page)
        listed = page.locator(f'#main [data-row="{made["id"]}"]')
        listed.get_by_role('button', name='Approve', exact=True).click()
        dialog = page.get_by_role('dialog')
        expect(dialog.get_by_role('button', name='Approve', exact=True)).to_be_visible()
        expect(dialog.get_by_text('Self-approved', exact=True)).to_have_count(0)
        dialog.get_by_role('button', name='Approve', exact=True).click()
        expect(dialog).to_be_hidden()
        decided = page.locator(f'#main [data-row="{made["id"]}"]')
        expect(decided).to_have_count(1)
        expect(decided.get_by_text('Self-approved', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_record_leave_announces_self_approval_for_the_selected_person(browser, live_server, h):
    record_and_check_leave(browser, live_server, h, pending=False)


def test_record_leave_requiring_rotation_approval_announces_the_pending_request(browser, live_server, h):
    record_and_check_leave(browser, live_server, h, pending=True)


def record_and_check_leave(browser, live_server, h, *, pending):
    person, other = h.seed['people'][:2]
    coordinator = identity(h, 'leave_coordinator', ['coordinator'], person=person)
    h.command('admin', 'self_approval.grant', {'identity_id': coordinator, **GRANT})
    if pending:
        resident(h, 'needs_approval')
    context, page = sign_in(browser, live_server, 'leave_coordinator')
    try:
        navigate(page, 'requests')
        whole_list(page)
        page.get_by_role('button', name='New request', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_role('radio', name='Leave or absence', exact=True).check()
        people = dialog.locator('fieldset[name="People"]')
        own = people.locator(f'input[value="{person}"]')
        independent = people.locator(f'input[value="{other}"]')
        message = dialog.locator('form').get_by_text('Your leave is self-approved only if it can be recorded without separate review.',
                                     exact=True)
        independent.check()
        expect(message).to_be_hidden()
        expect(dialog.get_by_label('Reason', exact=True)).to_be_visible()
        own.check()
        expect(message).to_be_visible()
        expect(dialog.get_by_text('Self-approved', exact=True)).to_have_count(0)
        own.uncheck()
        expect(message).to_be_hidden()
        independent.uncheck()
        own.check()
        reason = dialog.get_by_label('Reason for self-approval', exact=True)
        expect(reason).to_have_js_property('required', True)
        dialog.get_by_label('Type', exact=True).select_option(label='Annual')
        if pending:
            dialog.get_by_label('All day', exact=True).uncheck()
            dialog.get_by_label('Unavailable from', exact=True).fill('2026-10-04T08:00')
            dialog.get_by_label('Unavailable until', exact=True).fill('2026-10-04T12:00')
        reason.fill('Reviewed with a colleague by phone')
        dialog.get_by_role('button', name='Record leave', exact=True).click()
        expect(dialog).to_be_hidden()
        row = page.locator('#main [data-row]').filter(has=page.get_by_text('Self-approved', exact=True))
        if pending:
            toast = page.locator('.toast', has_text='Submitted for approval')
            expect(toast.get_by_role('button', name='Undo', exact=True)).to_be_visible()
            expect(row).to_have_count(0)
            return
        expect(row).to_have_count(1)
        row.locator('td[data-col=type], .dt-card-title').first.click()
        expect(page.locator('.request-panel .request-status').get_by_text('Self-approved', exact=True)).to_be_visible()
        expect(page.locator('.request-panel .request-history').get_by_text('Self-approved', exact=True)).to_have_count(1)
    finally:
        context.close()


@pytest.mark.parametrize('on_behalf', [False, True], ids=['own-request', 'on-behalf'])
def test_admin_request_decision_announces_only_actual_self_approval(browser, live_server, h, on_behalf):
    person, other = h.seed['people'][:2]
    admin = identity(h, 'request_admin', ['admin'], person=person)
    h.command('admin', 'self_approval.grant', {'identity_id': admin, **GRANT})
    made = request_row(h, 'request_admin', other if on_behalf else person, kind='administrative')
    context, page = sign_in(browser, live_server, 'request_admin')
    try:
        navigate(page, 'requests')
        whole_list(page)
        row = page.locator(f'#main [data-row="{made["id"]}"]')
        row.get_by_role('button', name='Approve', exact=True).click()
        dialog = page.get_by_role('dialog')
        mark_count = 0 if on_behalf else 1
        expect(dialog.get_by_text('Self-approved', exact=True)).to_have_count(mark_count)
        reason = dialog.get_by_label('Reason', exact=True)
        expect(reason).to_have_js_property('required', not on_behalf)
        if not on_behalf:
            reason.fill('Reviewed with a colleague by phone')
        dialog.get_by_role('button', name='Approve', exact=True).click()
        expect(dialog).to_be_hidden()
        expect(row.get_by_text('Approved', exact=True)).to_be_visible()
        expect(row.get_by_text('Self-approved', exact=True)).to_have_count(mark_count)

        row.locator('td[data-col=type], .dt-card-title').first.click()
        status = page.locator('.request-panel .request-status')
        expect(status.get_by_text('Approved', exact=True)).to_be_visible()
        expect(status.get_by_text('Self-approved', exact=True)).to_have_count(mark_count)
        history = page.locator('.request-panel .request-history')
        expect(history.get_by_text('Approved', exact=True)).to_be_visible()
        expect(history.get_by_text('Self-approved', exact=True)).to_have_count(mark_count)

        navigate(page, 'home')
        entry = page.locator('#main .home-needs-list li[data-need=self_approved]')
        if on_behalf:
            expect(entry).to_have_count(0)
        else:
            entry.get_by_role('button', name='Open', exact=True).click()
            panel = page.get_by_role('region', name='Self-approved decisions')
            expect(panel.locator('.audit-row .audit-self')).to_have_text('Self-approved')
    finally:
        context.close()

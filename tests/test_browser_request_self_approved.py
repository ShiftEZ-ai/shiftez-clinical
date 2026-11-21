"""A person who decides their own request under a self-approval permission is told so before deciding, and the decided
request says so afterwards: in its row and in its history, with the same Self-approved badge a rota decision wears."""
import pytest
from playwright.sync_api import expect

from .governance_support import identity
from .test_availability_patterns import pattern
from .test_browser import browser, live_server, sign_in, navigate, whole_list  # noqa: F401
from .test_request_self_approved_mark import GRANT

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

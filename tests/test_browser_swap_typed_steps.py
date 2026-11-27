"""A duty swap on a phone with nothing typed: both messages and the confirmation note are optional, the expiry field is
named for the date and time it holds, and "Nobody eligible" carries the reason the server's eligibility check gave."""
import pytest
from playwright.sync_api import expect

from .governance_support import identity, swap_automation
from .test_browser import browser, live_server, sign_in, navigate, route_fetch  # noqa: F401
from .test_browser_requests import PHONE, published, new_request, no_horizontal_scroll

pytestmark = pytest.mark.browser


def test_a_swap_is_offered_answered_and_confirmed_on_a_phone_with_nothing_typed(browser, live_server, h):
    swap_automation(h, False, False)  # the manual path: confirming ends at "accepted"
    colleague = h.seed['people'][1]
    identity(h, 'staff_two', ['staff'], colleague)
    published(h, days=2)
    context, page = sign_in(browser, live_server, 'staff', PHONE)
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Swap a duty')
        dialog.get_by_label('Swap with', exact=True).select_option(colleague)
        # The field holds a date and a time, and is named for both.
        until = dialog.get_by_role('group', name='Offer open until: date and time', exact=True)
        expect(until.get_by_label('Offer open until', exact=True)).to_be_visible()
        expect(until.get_by_label('Offer open until time', exact=True)).to_be_visible()
        message = dialog.get_by_label('Message to them', exact=True)
        expect(message).to_have_value('')
        assert message.evaluate('e => e.required') is False
        no_horizontal_scroll(page)
        dialog.get_by_role('button', name='Send offer', exact=True).click()
        expect(page.get_by_text('Offer sent.', exact=False)).to_be_visible()
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'staff_two', PHONE)
    try:
        navigate(page, 'requests')
        page.locator('#main').get_by_role('button', name='Offer a duty in return', exact=True).click()
        dialog = page.get_by_role('dialog', name='Offer a duty in return')
        message = dialog.get_by_label('Message to them', exact=True)
        expect(message).to_have_value('')
        assert message.evaluate('e => e.required') is False
        no_horizontal_scroll(page)
        dialog.get_by_role('button', name='Offer a duty in return', exact=True).click()
        expect(page.get_by_text('Sent. They confirm the pair next', exact=False)).to_be_visible()
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'staff', PHONE)
    try:
        navigate(page, 'requests')
        page.locator('#main').get_by_role('button', name='Confirm swap', exact=True).click()
        dialog = page.get_by_role('dialog', name='Confirm swap')
        expect(dialog.get_by_label('Reason', exact=True)).to_have_value('')
        no_horizontal_scroll(page)
        dialog.get_by_role('button', name='Confirm swap', exact=True).click()
        expect(page.get_by_text('Swap confirmed. A coordinator prepares the rota change next.', exact=False)).to_be_visible()
    finally:
        context.close()
    assert h.get('staff', '/api/swaps')['swaps'][0]['status'] == 'accepted'


def test_nobody_eligible_says_the_reason_the_eligibility_check_gave(browser, live_server, h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    published(h, days=2)
    reason = 'This swap leaves too little rest between duties.'

    def nobody(route):  # the answer of the real check, with every colleague refused for one reason
        if 'assignment_id=' not in route.request.url:
            return route.fallback()
        response = route_fetch(route)
        body = response.json()
        body['recipients'] = [{**r, 'eligible': False, 'message': reason} for r in body['recipients']]
        body['no_eligible_recipients_reason'] = reason
        route.fulfill(response=response, json=body)
    context, page = sign_in(browser, live_server, 'staff', PHONE)
    try:
        page.route('**/api/swaps/options?*', nobody)
        navigate(page, 'requests')
        dialog = new_request(page, 'Swap a duty')
        expect(dialog.get_by_text(f'Nobody eligible can take this duty. {reason} Ask your coordinator instead, or choose another duty.',
                                  exact=False)).to_be_visible()
        expect(dialog.get_by_role('button', name='Send offer', exact=True)).to_be_disabled()
        no_horizontal_scroll(page)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

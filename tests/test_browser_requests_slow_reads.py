"""Requests never waits on its slow side reads: the page draws while overtime eligibility is still being read, and New
request closes as soon as its save is answered, before the list is read again."""
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401
from .test_browser_requests import day, new_request

pytestmark = pytest.mark.browser


def test_requests_draws_and_new_request_closes_while_slow_reads_are_held(browser, live_server):
    context, page = sign_in(browser, live_server, 'staff')
    held = []
    eligibility = lambda url: urlsplit(url).path == '/api/requests/overtime-eligibility'
    # The page's list (not the form's own read of this person's requests before it sends).
    listing = lambda url: urlsplit(url).path == '/api/requests/all' and 'person_id=' not in urlsplit(url).query
    try:
        page.route(eligibility, lambda route: held.append(route))  # never answered during the test
        navigate(page, 'requests')  # the page is drawn (data-loaded-view) with eligibility unanswered
        assert any(eligibility(r.request.url) for r in held), 'the overtime eligibility was not asked for'
        dialog = new_request(page, 'Leave')
        dialog.get_by_label('All day').check()
        dialog.get_by_label('First day', exact=True).fill(day())
        dialog.get_by_label('Last day', exact=True).fill(day())
        dialog.get_by_label('Type', exact=True).select_option(label='Annual')
        dialog.get_by_label('Reason', exact=True).fill('Held reads fixture')
        page.route(listing, lambda route: held.append(route))  # the reload after the save waits too
        with page.expect_response(lambda r: urlsplit(r.url).path == '/api/commands' and r.request.method == 'POST') as saved:
            dialog.get_by_role('button', name='Send request', exact=True).click()
        assert saved.value.ok
        expect(dialog).to_be_hidden()  # closed on the save's answer: nothing it could wait for has been answered
        expect(page.get_by_text('Request submitted, not yet approved.')).to_be_visible()
    finally:
        page.unroute(listing)
        page.unroute(eligibility)  # continues every held read
        context.close()

"""UX-RETRY-ROOT: a Retry is never dead behind a hung refresh. A live change starts a refresh whose account read never
answers; a page then fails to load. Its Retry (app.ts retryRefresh) does not join the hung refresh: exactly one new
account read goes out and the page recovers. Before the fix the Retry joined it and sent nothing. Coordinator, 1440."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, route_fetch  # noqa: F401

pytestmark = pytest.mark.browser


def test_page_failure_retry_recovers_behind_a_hung_refresh(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    held, me = [], []
    try:
        # Home reads no publications: Workload below is then a page this tab has no kept reply for (api.ts keptReads;
        # one it has opens from that reply and never fails to load).
        navigate(page, 'home')

        def account(route):
            me.append(route.request.url)
            if len(me) == 1:
                held.append(route)  # the refresh's account read: never answered while the test runs
            else:
                route.continue_()

        def moved(route):
            # A live change nobody named (no `changes`): the pulse refreshes everything, the account first.
            body = route_fetch(route).json()
            body['activity'] = int(body.get('activity') or 0) + 1
            body.pop('changes', None)
            route.fulfill(json=body)

        page.route(re.compile(r'/api/me$'), account)
        page.route(re.compile(r'/api/pulse$'), moved)
        with page.expect_request(re.compile(r'/api/me$')):
            page.evaluate("window.dispatchEvent(new Event('online'))")
        page.unroute(re.compile(r'/api/pulse$'))

        page.route(re.compile(r'/api/publications$'),
                   lambda route: route.fulfill(status=409, json={'error': {'code': 'SYNTHETIC', 'message': 'Synthetic failure'}}))
        page.evaluate("location.hash='insights/workload'")
        failed = page.locator('#main').get_by_text('Couldn’t load Workload.', exact=False)  # DL-045 (c5a282a5d): named by the open tab
        expect(failed).to_be_visible()
        page.unroute(re.compile(r'/api/publications$'))

        retry = page.locator('#main').get_by_role('button', name='Retry', exact=True)
        with page.expect_request(re.compile(r'/api/me$')):
            retry.click()
        expect(page.get_by_role('heading', name='Nothing published yet', exact=True)).to_be_visible()
        expect(failed).to_have_count(0)
        assert len(me) == 2, me  # exactly one new account read: the hung one was not joined, nor stacked on
    finally:
        for route in held:
            route.abort()
        context.close()

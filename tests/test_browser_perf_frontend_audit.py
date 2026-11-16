"""A refresh reads the account and the lists in memory together, not one after the other (perf audit 2026-10-29)."""
import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from .test_browser import wait_held, reconnect, browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser


def test_a_refresh_waits_for_the_account_before_reading_the_lists(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    catalogue_requests = []
    try:
        navigate(page, 'people')
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'people', timeout=20000)
        page.on('request', lambda request: catalogue_requests.append(request)
                if urlsplit(request.url).path == '/api/catalogues' else None)
        page.route(re.compile(r'/api/me$'), lambda r: held.append(r))
        reconnect(page)  # offline, then online: the app refreshes in the background (`online` alone reads nothing, 60315a5de)
        wait_held(page, held)
        assert len(held) == 1 and not catalogue_requests
        with page.expect_request(lambda r: urlsplit(r.url).path == '/api/catalogues', timeout=15000):
            held.pop().continue_()
        page.unroute(re.compile(r'/api/me$'))
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'people')
        expect(page.locator('#main [role=alert]')).to_have_count(0)
    finally:
        while held:
            held.pop().continue_()
        context.close()

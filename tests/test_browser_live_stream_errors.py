"""The live stream's failures: a server `error` (anything but a 401) never signs out, and the stream reconnects; only
`expired` (a 401) shows the session-ended sign-in."""
import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from .test_browser import PAGE_LOAD_MS, browser, live_server, sign_in  # noqa: F401

pytestmark = pytest.mark.browser

ENDED = 'Your session ended. Sign in to continue where you left off.'
SETTLE = '() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))'


def stream_says(page, event, data):
    """The next stream the page opens gets `event` and ends; the ones after it reach the server."""
    opened = []
    def answer(route):
        opened.append(route.request)
        if len(opened) == 1:
            route.fulfill(status=200, headers={'Content-Type': 'text/event-stream'}, body=f'event: {event}\ndata: {data}\n\n')
        else:
            route.continue_()
    page.route(lambda url: urlsplit(url).path == '/api/events', answer)
    page.reload()
    expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible(timeout=PAGE_LOAD_MS)
    return opened


def test_a_stream_error_reconnects_without_signing_out(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        opened = stream_says(page, 'error', '{"retry": true}')
        for _ in range(1000):  # bounded: two frames a round; the first retry waits 0.5 to 1 s
            if len(opened) >= 2:
                break
            page.evaluate(SETTLE)
        assert len(opened) >= 2, 'the stream did not reconnect after an error'
        expect(page.get_by_text(ENDED)).to_have_count(0)
        expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible()
    finally:
        context.close()


def test_an_expired_stream_shows_the_session_ended_sign_in(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        stream_says(page, 'expired', '{}')
        expect(page.get_by_text(ENDED)).to_be_visible()
    finally:
        context.close()

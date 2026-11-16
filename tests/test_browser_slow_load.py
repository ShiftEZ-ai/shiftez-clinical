"""DL-045: one slow-load state everywhere, shown on Configuration › Auto-schedule & scoring › Score review (42 s cold on
DEV a5dd59837, where it showed a skeleton with no ending). A live read shows "Still loading · m:ss" and never Retry; at
the read's limit it is aborted and the area says "Didn’t load in 1:00." with Retry; a failed read says "Couldn’t load
the score review." with Retry; Retry reads again, recovers, and gives focus back to the area."""
import json
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in, wait_held  # noqa: F401

pytestmark = pytest.mark.browser
REVIEW = re.compile(r'.*/api/workspaces/[^/]+/score-review.*')


def test_a_slow_then_failing_read_ends_with_retry_and_retry_recovers(browser, live_server, h):
    w = h.workspace('Slow load draft')
    held, mode = [], ['hold']

    def answer(route):
        if mode[0] == 'hold':
            held.append(route)
        elif mode[0] == 'fail':
            route.fulfill(status=500, content_type='application/json',
                          body=json.dumps({'error': {'code': 'INTERNAL_ERROR', 'message': 'The service had a problem.'}}))
        else:
            route.continue_()

    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        page.clock.pause_at(page.evaluate("new Date(Date.now()+1000).toISOString()"))
        page.route(REVIEW, answer)
        page.evaluate(f"location.hash='configuration/auto_schedule?tab=score&draft={w['id']}'")
        wait_held(page, held)
        pane = page.locator('.config-pane[data-kind=auto_schedule]')
        page.clock.run_for(16_000)
        expect(pane.locator('.sk-slow')).to_have_text(re.compile(r'^Still loading · 0:1\d$'))  # its elapsed time
        expect(pane.get_by_role('button', name='Retry', exact=True)).to_have_count(0)  # never while the read is live
        page.clock.run_for(45_000)  # the read's limit: aborted, the area ends in words and Retry
        timed_out = pane.locator('.read-failed')
        expect(timed_out).to_contain_text('Didn’t load in 1:00.')
        expect(pane.locator('.loading[aria-busy=true]')).to_have_count(0)  # no skeleton left without an ending
        mode[0] = 'fail'
        pane.get_by_role('button', name='Retry', exact=True).click()
        page.clock.resume()
        expect(pane.locator('.read-failed')).to_contain_text('Couldn’t load the score review.')
        retry = pane.get_by_role('button', name='Retry', exact=True)
        expect(retry).to_be_focused()  # failed again: focus on the new Retry, never lost
        mode[0] = 'ok'
        retry.press('Enter')  # a real, keyboard-operable button
        expect(pane.locator('.score-review')).to_be_visible()
        expect(pane.locator('.read-failed')).to_have_count(0)
        assert page.evaluate("!!document.activeElement?.closest('.config-pane')"), 'focus returns to the loaded area'
    finally:
        page.unroute_all(behavior='ignoreErrors')  # the held read was aborted; closing the page drops it
        context.close()

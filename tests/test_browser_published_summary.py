"""Published health pills consume the server's issued counts, without recounting raw findings."""
import re

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, route_fetch  # noqa: F401

pytestmark = pytest.mark.browser


def test_published_layer_uses_server_summary_counts(browser, live_server, h):
    h.publish(h.shift(h.workspace())['workspace'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        def issued_counts(route):
            response = route_fetch(route, headers={k: v for k, v in route.request.headers.items() if k != 'if-none-match'})
            body = response.json()
            # Distinct from the raw check: the published layer must display the API's canonical units.
            body['summary'] = {'sessions_with_gaps': 7, 'blocking': 11, 'advisory': 13, 'extra_cover_sessions': 17}
            route.fulfill(response=response, json=body)
        page.route('**/api/schedule/sessions?**', issued_counts)
        navigate(page, 'schedule/week/2026-10-04?edit=none')
        footer = page.locator('.app-footer .health-pills')
        # One rota health (UIUX-AUDIT C3): the draft's pills and words; sessions short of people are Must fix, never a
        # separate "need staff" pill; Could improve is the gold circle.
        for label in ('11 must fix', '13 could improve'):
            expect(footer.get_by_role('button', name=label, exact=True)).to_be_visible()
        expect(footer).not_to_contain_text('need staff')
        expect(footer.locator('.health-pill.could')).to_be_visible()
        expect(footer.locator('.health-pill.info').filter(has_text='could improve')).to_have_count(0)
        expect(footer.get_by_role('button', name=re.compile('17.*extra.cover', re.I))).to_be_visible()
    finally:
        context.close()

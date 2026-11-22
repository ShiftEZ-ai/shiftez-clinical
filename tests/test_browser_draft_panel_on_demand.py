"""The rule page offers a draft effect read and fetches it only after the person asks."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, open_rule, route_fetch, sign_in  # noqa: F401
from .test_rule_templates import activate, propose

pytestmark = pytest.mark.browser


def test_rule_page_loads_draft_effect_on_demand_and_retries_a_failed_read(browser, live_server, h):
    rule = activate(h, propose(h, {'kind': 'max_hours', 'hours': 6, 'window': 'day'},
                              name='On-demand effect rule'))
    draft = h.workspace('On-demand draft', '2026-11-01', '2026-11-08')
    context, page = sign_in(browser, live_server, 'coordinator')
    map_calls = []
    drift_calls = []
    map_attempts = 0
    held = []

    def rule_map(route):
        nonlocal map_attempts
        map_attempts += 1
        map_calls.append(route.request.url)
        if map_attempts == 1:
            held.append(route)   # held until the loading state is seen, then aborted
            return
        response = route_fetch(route)
        route.fulfill(response=response)

    def baseline_drift(route):
        drift_calls.append(route.request.url)
        h.route_json('coordinator', f"/api/workspaces/{draft['id']}/baseline/refresh-preview",
                     lambda body: body)(route)

    try:
        page.route(re.compile(r".*/api/workspaces/" + re.escape(draft['id']) + r"/rule-map$"), rule_map)
        page.route(re.compile(r".*/api/workspaces/" + re.escape(draft['id']) + r"/baseline/refresh-preview$"), baseline_drift)
        navigate(page, 'rules')
        open_rule(page, 'On-demand effect rule')

        section = page.locator('.rl-sec').filter(has_text=f"In {draft['name']}")
        expect(section).to_be_visible()
        show = section.get_by_role('button', name=f"Show effect in {draft['name']}", exact=True)
        expect(show).to_be_visible()
        expect(show).to_have_text('Show effect')
        show.focus()
        expect(show).to_be_focused()
        assert not map_calls
        assert not drift_calls

        with page.expect_request(re.compile(r".*/api/workspaces/" + re.escape(draft['id']) + r"/rule-map$")):
            show.press('Enter')
        expect(page.get_by_text(f"Loading {draft['name']} effect…", exact=True)).to_be_visible()
        expect(page.get_by_role('heading', name=f"In {draft['name']}", exact=True)).to_be_focused()
        held.pop().abort()
        failure = page.locator('.read-failed')
        expect(failure).to_contain_text(re.compile(rf"^Couldn’t load {re.escape(draft['name'])} effect\."))
        expect(failure).to_have_attribute('role', 'alert')
        retry = failure.get_by_role('button', name='Retry', exact=True)
        expect(retry).to_be_visible()
        assert len(map_calls) == 1

        retry.press('Enter')
        expect(page.locator('.rl-sec').filter(has_text=f"In {draft['name']}").get_by_text('No duties yet.', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Retry', exact=True)).to_have_count(0)
        assert len(map_calls) == 2
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

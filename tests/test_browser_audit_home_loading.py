"""Home must retain a child calendar result that arrives before the page is mounted."""
from datetime import timedelta

import pytest
from playwright.sync_api import expect

from .test_browser import route_fetch, browser, live_server, sign_in, navigate, wait_held  # noqa: F401
from .test_browser_home import local_today

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("calendar_fails", [False, True], ids=["duties", "retry"])
def test_home_retains_calendar_answer_before_mount(browser, live_server, h, calendar_fails):
    day = local_today() + timedelta(days=1)
    workspace = h.workspace("Home loading audit", day.isoformat(), (day + timedelta(days=7)).isoformat())
    workspace = h.shift(workspace, start=f"{day.isoformat()}T08:00:00+03:00",
                        end=f"{day.isoformat()}T12:00:00+03:00")["workspace"]
    h.publish(workspace)
    context, page = sign_in(browser, live_server, "staff")
    home, calendar = [], []
    try:
        navigate(page, "requests")
        # Python's pause_at numeric argument uses seconds; an ISO instant preserves the pinned test calendar.
        page.clock.pause_at(page.evaluate("new Date(Date.now()+1000).toISOString()"))
        page.route("**/api/home", lambda route: home.append((route, route_fetch(route))))
        page.route("**/api/calendar?**", lambda route: calendar.append((route, route_fetch(route))))
        page.evaluate("location.hash='home'")
        wait_held(page, home)
        # Show the page skeleton; it must now remain visible for its minimum duration.
        page.clock.run_for(300)
        route, response = home.pop()
        route.fulfill(response=response)
        wait_held(page, calendar)
        assert page.locator(".home-hero:not([hidden])").count() == 0, "The child is still off-screen during page settle"
        route, response = calendar.pop()
        with page.expect_response(lambda r: "/api/calendar?" in r.url) as answered:
            if calendar_fails:
                route.fulfill(status=503, content_type="application/json", body='{"error":{"code":"SERVER_ERROR","message":"Calendar temporarily unavailable"}}')
            else:
                route.fulfill(response=response)
        answered.value.finished()
        page.evaluate("() => Promise.resolve().then(() => Promise.resolve())")
        page.clock.run_for(700)
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "home")
        panel = page.get_by_role("region", name="Coming up")
        expect(panel.locator(".loading")).to_have_count(0)
        hero = page.locator(".home-hero")
        if calendar_fails:
            expect(panel).to_contain_text("Couldn’t load coming up.")
            expect(hero).to_be_hidden()
            page.clock.resume()
            panel.get_by_role("button", name="Retry loading coming up").click()
            wait_held(page, calendar)
            route, response = calendar.pop()
            route.fulfill(response=response)
        # The one duty is the hero; nothing else is coming up.
        expect(hero).to_contain_text("Next duty · tomorrow")
        expect(panel).to_contain_text("Nothing else in the next two weeks.")
        # A duty still to come can be offered to a colleague (when the department allows staff swaps).
        offer = hero.get_by_role("button", name="Offer swap", exact=True)
        expect(offer).to_have_count(1 if h.get("staff", "/api/me").get("staff_swap_access") and h.get("staff", "/api/swaps/options")["recipients"] else 0)
    finally:
        for route, _ in home + calendar:
            route.abort()
        context.close()

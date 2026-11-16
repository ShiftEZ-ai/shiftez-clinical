"""Lost connection (N-10): a page opened while the server can't be reached does not claim to show saved data; the page
that showed data before keeps it under one line, with Retry and no problem report (nothing for the team to fix)."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, expect_loaded

pytestmark = pytest.mark.browser


def test_offline_pages_say_what_is_true(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        expect_loaded(page, "home")
        page.locator("#department-nav [data-route=people]").click()
        expect_loaded(page, "people")
        page.route("**/api/**", lambda route: route.abort())
        page.evaluate("window.dispatchEvent(new Event('online'))")  # a refresh: it cannot reach the server
        banner = page.locator("#main > .stale-banner")
        expect(banner).to_have_text(re.compile(r"^Couldn’t refresh · showing data from \d\d:\d\dRetry$"))
        expect(banner.get_by_role("button", name="Report this problem")).to_have_count(0)
        expect(page.locator("#main tbody tr[data-row]").first).to_be_visible()  # the People data stays
        # Another page opened while offline has no data of its own to be stale.
        page.locator("#department-nav [data-route=home]").click()
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "home")
        expect(page.locator("#main > .stale-banner")).to_have_count(0)
        expect(page.get_by_text("Showing saved data")).to_have_count(0)
    finally:
        context.close()

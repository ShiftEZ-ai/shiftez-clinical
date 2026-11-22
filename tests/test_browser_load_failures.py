"""A collection page whose first read fails says only that it did not load, with Retry inside the message, and is not
the start-up screen ("can't be reached", "No schedule could be retrieved") nor a repeating toast. Rows already shown
whose refresh fails carry one line saying how old they are, with Retry; the footer is not the only sign."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, expect_loaded  # noqa: F401

pytestmark = pytest.mark.browser

OUTAGE = {"status": 503, "content_type": "application/json",
          "body": '{"error":{"code":"UNAVAILABLE","message":"Synthetic outage"}}'}


def test_people_first_load_failure_and_refresh_failure_say_what_is_true(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        expect_loaded(page, "home")
        # The first read of People fails: the account answers, the page's lists do not.
        page.route("**/api/catalogues*", lambda route: route.fulfill(**OUTAGE))
        page.evaluate("location.hash = '#people'")
        page.reload()
        main = page.locator("#main")
        failed = main.locator(".read-failed")
        expect(failed).to_contain_text("Couldn’t load People.")
        retry = failed.get_by_role("button", name="Retry", exact=True)
        expect(retry).to_be_visible()
        expect(page.locator(".boot-failed")).to_have_count(0)
        expect(main).not_to_contain_text("can’t be reached")
        expect(main).not_to_contain_text("No schedule could be retrieved")
        expect(main.locator(".stale-banner")).to_have_count(0)  # nothing was shown, so nothing is stale
        # A pulse meanwhile adds no message of its own: the page already says it did not load.
        with page.expect_response("**/api/pulse"):
            page.evaluate("window.dispatchEvent(new Event('online'))")
        expect(page.locator("#toast-tray .toast")).to_have_count(0)
        expect(failed).to_have_count(1)
        # Retry inside the message, by keyboard, loads the page once the lists answer.
        page.unroute("**/api/catalogues*")
        retry.focus()
        page.keyboard.press("Enter")
        expect_loaded(page, "people")
        rows = main.locator("tbody tr[data-row]")
        expect(rows.first).to_be_visible()
        expect(main.locator(".read-failed")).to_have_count(0)

        # A refresh fails over the rows shown: they stay, under one line saying how old they are, with Retry.
        page.route("**/api/**", lambda route: route.fulfill(**OUTAGE))
        page.evaluate("window.dispatchEvent(new Event('online'))")
        banner = page.locator("#main > .stale-banner")
        expect(banner).to_contain_text(re.compile(r"^Couldn’t refresh · showing data from \d\d:\d\d"))
        expect(banner).to_have_attribute("role", "status")
        expect(rows.first).to_be_visible()
        expect(page.locator("#main .read-failed")).to_have_count(0)
        page.unroute("**/api/**")
        banner.get_by_role("button", name="Retry", exact=True).click()
        expect(page.locator("#main > .stale-banner")).to_have_count(0)
        expect(rows.first).to_be_visible()
    finally:
        page.unroute_all(behavior="ignoreErrors")
        context.close()

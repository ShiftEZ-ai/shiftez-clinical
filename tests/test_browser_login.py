"""Sign-in and start-up (F1): the sign-in page is the brand and the form, Username has focus; start-up waits like any
page and fails on one screen (Retry; Sign out when signed in)."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, new_context, open_app, expect_loaded

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("viewport,scheme", [({"width": 1440, "height": 900}, "light"), ({"width": 390, "height": 844}, "dark")])
def test_sign_in_page_is_the_brand_and_the_form(browser, live_server, viewport, scheme):
    """N-06 / N-07: no marketing story; the brand, "Sign in" and the form, with Username focused."""
    context = new_context(browser, viewport, color_scheme=scheme)
    try:
        page = open_app(context, live_server)
        expect(page.get_by_role("heading", name="Sign in", level=1)).to_be_visible()
        expect(page.locator(".login-brand")).to_have_text("Rota")
        # The institution's own wordmark, loaded from the app's assets, at its natural aspect ratio and named.
        mark = page.get_by_role("img", name="Example Organisation")
        expect(mark).to_be_visible()
        expect(mark).to_have_js_property("complete", True)
        size = mark.evaluate("n => ({w: n.naturalWidth, h: n.naturalHeight, rw: n.getBoundingClientRect().width, rh: n.getBoundingClientRect().height})")
        assert size and size["w"] > 0 and abs(size["rw"] / size["rh"] - size["w"] / size["h"]) < 0.08, size
        expect(page.locator(".login-story")).to_have_count(0)
        expect(page.get_by_text("Welcome back")).to_have_count(0)
        expect(page.get_by_label("Username or email", exact=True)).to_be_focused()
        # The form starts within the first screen on a phone (the story used to take 195px above it).
        assert page.get_by_label("Username or email", exact=True).evaluate("n => n.getBoundingClientRect().bottom < innerHeight")
    finally:
        context.close()


def test_start_up_failure_is_one_screen_with_retry_and_sign_out(browser, live_server, h):
    """N-12: signed in, a failed start-up shows the brand, "Rota can't be reached", what to do meanwhile, Retry (focused)
    and Sign out; signed out, the same screen without Sign out."""
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        expect_loaded(page, "home")
        page.route("**/api/me", lambda route: route.fulfill(status=503, content_type="application/json",
                                                             body='{"error":{"code":"UNAVAILABLE","message":"Synthetic outage"}}'))
        page.reload()
        main = page.locator("#main.boot-failed")
        expect(main.get_by_role("heading", name="Rota can’t be reached")).to_be_visible()
        expect(main).to_contain_text("This is not an empty rota.")
        expect(main.get_by_role("button", name="Retry", exact=True)).to_be_focused()
        expect(main.get_by_role("button", name="Sign out", exact=True)).to_be_visible()
        page.unroute("**/api/me")
        main.get_by_role("button", name="Retry", exact=True).click()
        expect_loaded(page, "home")
    finally:
        context.close()
    context = new_context(browser)
    try:
        context.route("**/auth/config", lambda route: route.abort())
        page = open_app(context, live_server)
        main = page.locator("#main.boot-failed")
        expect(main.get_by_role("heading", name="Rota can’t be reached")).to_be_visible()
        expect(main.get_by_role("button", name="Retry", exact=True)).to_be_visible()
        expect(main.get_by_role("button", name="Sign out")).to_have_count(0)
    finally:
        context.close()


def test_a_slow_start_up_waits_quietly_then_offers_retry(browser, live_server, h):
    """N-26: while the account is being read, start-up shows only the moving line (the words for screen readers), then
    "Still loading · 0:15" after 15 s (never a frozen "Connecting…", never Retry while the read is live), and at the read's
    limit "Didn’t load in 1:00." with Retry (DL-045). Retry asks again instead of joining the stuck read."""
    context, page = sign_in(browser, live_server, "coordinator")
    held = []
    try:
        expect_loaded(page, "home")
        # Only the first read of the account is held (stuck); later ones go through.
        page.route("**/api/me", lambda route: held.append(route) if not held else route.continue_())
        page.reload()
        boot = page.locator("#main.boot")
        expect(boot.locator(".loading")).to_have_count(1)
        page.clock.run_for(3500)
        expect(boot.locator(".loading > .sr-only")).to_have_text(re.compile(r"Connecting to .+…"))  # for screen readers
        expect(boot.locator(".sk-slow, .spinner")).to_have_count(0)  # nothing drawn over the line reviewed
        page.clock.run_for(12000)
        expect(boot.locator(".sk-slow")).to_have_text(re.compile(r"^Still loading · 0:1\d$"))  # its elapsed time
        expect(boot.get_by_role("button", name="Retry", exact=True)).to_have_count(0)  # never while the read is live
        # The read's limit (READ_TIMEOUT): the stuck read is aborted and start-up ends with its words and Retry.
        page.clock.run_for(45000)
        expect(page.locator("#main.boot-failed")).to_contain_text("Didn’t load in 1:00.")
        # Retry asks again rather than joining the stuck read, and the app starts from the new answer.
        boot.get_by_role("button", name="Retry", exact=True).click()
        expect_loaded(page, "home")
        # The stuck read answering late, even "session expired", is dropped: the recovered app stays (one frame, one
        # page, no Sign in).
        try:
            held.pop().fulfill(status=401, content_type="application/json",
                               body='{"error":{"code":"SESSION_EXPIRED","message":"Session expired"}}')
        except Exception:  # the app aborted the superseded read: nothing can arrive  # noqa: BLE001
            pass
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect_loaded(page, "home")
        expect(page.get_by_role("heading", name="Sign in", level=1)).to_have_count(0)
        expect(page.locator("#main")).to_have_count(1)
        expect(page.locator(".workspace-shell")).to_have_count(1)
    finally:
        for route in held:
            route.abort()
        context.close()


@pytest.mark.parametrize("age_days,shown", [(13, True), (15, False)])
def test_remembered_account_expires(browser, live_server, age_days, shown):
    """SEC-10: "Continue as <name>" is offered for 14 days after the last sign-in on this device, then forgotten."""
    import json
    from packages.domain.time import now as clock_now
    remembered = {"identity": "00000000-0000-0000-0000-000000000001", "name": "Remembered Person (test)", "username": "remembered",
                  "at": int((clock_now().timestamp() - age_days * 86400) * 1000)}
    context = new_context(browser, storage_state={"cookies": [], "origins": [{"origin": live_server,
        "localStorage": [{"name": "rota.account", "value": json.dumps(remembered)}]}]})
    try:
        page = open_app(context, live_server)
        expect(page.get_by_role("heading", name="Sign in", level=1)).to_be_visible()
        expect(page.get_by_text("Remembered Person (test)")).to_have_count(1 if shown else 0)
        assert (page.evaluate("localStorage.getItem('rota.account')") is not None) == shown
    finally:
        context.close()

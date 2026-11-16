"""A failed request-kind read must never submit the previously displayed form."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, wait_held  # noqa: F401
from .test_browser_requests import day, new_request, published

pytestmark = pytest.mark.browser


def test_failed_swap_kind_load_cannot_submit_previous_leave(browser, live_server, h):
    published(h)
    context, page = sign_in(browser, live_server, "staff")
    try:
        navigate(page, "requests")
        # The dialog asks for swap choices once as it opens (to say whether Swap is possible), and choosing Swap joins
        # that same read while it is in flight: hold it from before the dialog opens, or a slow runner never sees a request.
        choices = []
        page.route("**/api/swaps/options", lambda route: choices.append(route))
        dialog = new_request(page, "Leave")
        dialog.get_by_label("All day").check()
        dialog.get_by_label("First day", exact=True).fill(day())
        dialog.get_by_label("Last day", exact=True).fill(day())
        dialog.get_by_label("Reason", exact=True).fill("Unsent leave before choosing swap")
        before = h.get("staff", "/api/requests")["requests"]

        writes = []
        page.on("request", lambda r: writes.append(r) if r.method == "POST" and r.url.endswith("/api/commands") else None)
        dialog.get_by_role("radio", name=re.compile("^Swap a duty")).check()
        wait_held(page, choices)
        submit = dialog.get_by_role("button", name="Send offer", exact=True)
        expect(submit).to_be_disabled()
        dialog.locator("form").evaluate("form => form.requestSubmit()")
        expect(dialog.locator(".loading")).to_be_visible()
        expect(submit).to_be_disabled()  # The shared form guard leaves the loading state untouched.
        assert writes == []
        assert h.get("staff", "/api/requests")["requests"] == before

        choices[0].fulfill(status=503, content_type="application/json",
                           body='{"error":{"code":"READ_UNAVAILABLE","message":"Synthetic swap choices unavailable"}}')
        expect(dialog.get_by_text("Couldn’t load the swap options.", exact=False)).to_be_visible()  # C3-F5: names what
        expect(submit).to_be_disabled()
        dialog.locator("form").evaluate("form => form.requestSubmit()")
        expect(submit).to_be_disabled()
        assert writes == []
        assert h.get("staff", "/api/requests")["requests"] == before, "The failed Swap form saved the previous Leave request."

        # A successful kind switch recovers. While its one save is running, the
        # kind cannot change and a second submit cannot send a second command.
        dialog.get_by_role("radio", name=re.compile("^Leave")).check()
        dialog.get_by_label("All day").check()
        dialog.get_by_label("First day", exact=True).fill(day())
        dialog.get_by_label("Last day", exact=True).fill(day())
        dialog.get_by_label("Type", exact=True).select_option(label="Annual")
        dialog.get_by_label("Reason", exact=True).fill("Intended recovered leave")
        commands = []
        page.route("**/api/commands", lambda route: commands.append(route))
        dialog.get_by_role("button", name="Send request", exact=True).click()
        wait_held(page, commands)
        expect(dialog.get_by_role("radio", name=re.compile("^Swap a duty"))).to_be_disabled()
        dialog.locator("form").evaluate("form => form.requestSubmit()")
        expect(dialog.get_by_role("button", name="Saving…", exact=True)).to_be_disabled()
        assert len(commands) == len(writes) == 1
        commands[0].continue_()
        expect(page.get_by_text("Request submitted, not yet approved.", exact=True)).to_be_visible()
        after = h.get("staff", "/api/requests")["requests"]
        assert len(after) == 1 and after[0]["private_reason"] == "Intended recovered leave"
    finally:
        context.close()


def test_late_swap_choices_do_not_replace_the_newer_leave_form(browser, live_server, h):
    published(h)
    context, page = sign_in(browser, live_server, "staff")
    try:
        navigate(page, "requests")
        # The dialog asks for swap choices once as it opens (to say whether Swap is possible), and choosing Swap joins
        # that same read while it is in flight: hold it from before the dialog opens, or a slow runner never sees a request.
        choices = []
        page.route("**/api/swaps/options", lambda route: choices.append(route))
        dialog = new_request(page, "Leave")
        dialog.get_by_role("radio", name=re.compile("^Swap a duty")).check()
        wait_held(page, choices)
        dialog.get_by_role("radio", name=re.compile("^Leave")).check()
        reason = dialog.get_by_label("Reason", exact=True)
        reason.fill("The current Leave form")
        with page.expect_response("**/api/swaps/options"):
            choices[0].continue_()
        expect(reason).to_have_value("The current Leave form")
        expect(dialog.get_by_role("button", name="Send request", exact=True)).to_be_enabled()
        expect(dialog.get_by_text("There is no colleague you can offer a duty to.")).to_have_count(0)
        assert h.get("staff", "/api/requests")["requests"] == []
    finally:
        context.close()

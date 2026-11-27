"""Real shared UI actions explain failed replies without leaking an old account's action."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import route_fetch, browser, live_server, sign_in, navigate, wait_held  # noqa: F401
from .test_browser_fairness_dashboard import published_month, october, section
from .test_browser_uiux_sessions import switch_account

pytestmark = pytest.mark.browser


def test_async_catalogue_row_history_explains_failure_and_can_retry(browser, live_server):
    context, page = sign_in(browser, live_server, "admin")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        navigate(page, "configuration")
        page.get_by_role("button", name="Ranks", exact=True).click()
        pattern = "**/api/catalogues/categories/*/history"
        page.route(pattern, lambda route: route.abort())
        page.locator(".dt-more").first.click()
        page.get_by_role("menuitem", name="History", exact=True).click()
        # The shared history (SW3-D-07) opens at once and says a failed read inside it.
        history = page.get_by_role("dialog", name="Definition history")
        expect(history).to_contain_text("The server could not be reached")
        history.get_by_role("button", name="Close dialog").click()
        page.unroute(pattern)
        page.locator(".dt-more").first.click()
        page.get_by_role("menuitem", name="History", exact=True).click()
        expect(history.locator("tbody tr").first).to_be_visible()
        assert not errors
    finally:
        context.close()


def test_custom_eligibility_undo_explains_refused_save_and_preserves_confirmed_state(browser, live_server, h):
    person = h.seed["people"][0]
    catalogue = h.get("credentialer", "/api/catalogues")
    who = next(p["display_name"] for p in catalogue["people"] if p["id"] == person)
    row = next(r for r in catalogue["eligibilities"] if r["person_id"] == person)
    h.command("credentialer", "eligibility.end", {"ends_on": "2026-10-01", "reason": "Fictional trial"}, row["id"], row["revision"])
    clinic = next(s["name"] for s in catalogue["services"] if s["id"] == row["service_id"])
    context, page = sign_in(browser, live_server, "credentialer")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        navigate(page, "people")
        page.locator(".dt-table tbody tr", has_text=who).first.get_by_text(who, exact=True).first.click()
        panel = page.get_by_role("region", name=who, exact=True)
        # The ended permission waits behind "Show ended (N)" (4e111b9fd, SW3-D-14).
        ended = panel.get_by_role("button", name=re.compile(r"^Show ended \(\d+\)$"))
        expect(ended.first).to_be_visible()
        for toggle in ended.all():
            toggle.click()
        panel.get_by_role("button", name=f"More for {clinic}", exact=True).click()
        page.get_by_role("menu", name=f"More for {clinic}", exact=True).get_by_role("menuitem", name="Change dates", exact=True).click()
        form = page.get_by_role("dialog", name=f"Change dates · {clinic}", exact=True)
        form.get_by_label("Last day (included, optional)", exact=True).fill("")
        form.get_by_label("Reason", exact=True).fill("Fictional continuation")
        # A slow server (CI saw 2 s): the refresh after the save outlasts the 1.2 s "nobody reported it" timer, and the
        # save's own message must still carry the Undo (it is shown before the refresh is awaited, app.ts).
        slow, held = "**/api/catalogues?*", []
        page.route(slow, lambda route: held.append(route))
        form.get_by_role("button", name="Save", exact=True).click()
        wait_held(page, held)
        page.clock.run_for(2000)
        for route in held:  # the refresh's read (and any other read held meanwhile) answers now
            route.continue_()
        page.unroute(slow)  # after: unroute continues the reads it still holds, so a later continue_ would be a second
        undo = page.locator(".toast").filter(has_text="dates changed").get_by_role("button", name="Undo", exact=True)
        expect(undo).to_be_visible()
        expect(page.locator(".toast").get_by_role("button", name="Undo", exact=True)).to_have_count(1)

        def refuse_undo(route):
            if route.request.post_data_json["kind"] == "eligibility.update_dates":  # the server's own Undo of an extend (shared Undo, api.ts)
                route.fulfill(status=409, json={"error": {"code": "STALE_REVISION", "message": "Fictional Undo refused: reload the current permission."}})
            else:
                route.continue_()

        page.route("**/api/commands", refuse_undo)
        undo.click()
        # G-04 (cycle 3, adf40abbe; api.ts undoRefused, tests/test_browser_cycle3_fixes.py): an Undo the server refuses
        # as STALE_* is one plain outcome, never the server's words beside shared conflict guidance.
        refusal = page.locator(".toast.error")
        expect(refusal).to_have_count(1)
        expect(refusal).to_contain_text("Already undone, or the rota changed since. Nothing was undone.")
        expect(page.locator(".toast").filter(has_text="Fictional Undo refused")).to_have_count(0)
        current = next(r for r in h.get("credentialer", "/api/catalogues")["eligibilities"] if r["id"] == row["id"])
        assert current["ends_on"] is None
        expect(page.get_by_text("Undone. The earlier dates are back.", exact=True)).to_have_count(0)
        assert not errors
    finally:
        context.close()


def test_fairness_drill_explains_failed_read_keeps_focus_and_can_retry(browser, live_server, h):
    published_month(h)
    context, page = sign_in(browser, live_server, "coordinator")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        navigate(page,"insights/fairness")
        october(page)
        alex = section(page, "distribution").get_by_role("button", name=re.compile(r"^Staff 01\(test\): 3 duties"))
        pattern = "**/api/fairness/sessions?*"
        page.route(pattern, lambda route: route.abort())
        alex.click()
        expect(page.locator(".toast.error")).to_contain_text("Sessions unavailable")
        expect(alex).to_be_focused()
        expect(page.get_by_role("region", name="Duties · Staff 01(test)", exact=True)).to_have_count(0)
        page.unroute(pattern)
        alex.click()
        expect(page.get_by_role("region", name="Duties · Staff 01(test)", exact=True).locator("tbody tr")).to_have_count(3)
        assert not errors
    finally:
        context.close()


def test_async_row_action_discards_reply_after_signing_in_as_another_role(browser, live_server):
    context, page = sign_in(browser, live_server, "admin")
    errors, held = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        navigate(page, "configuration")
        page.get_by_role("button", name="Ranks", exact=True).click()
        pattern = "**/api/catalogues/categories/*/history"
        page.route(pattern, lambda route: held.append((route, route_fetch(route))))
        page.locator(".dt-more").first.click()
        with page.expect_request(pattern):
            page.get_by_role("menuitem", name="History", exact=True).click()
        # The shared history (SW3-D-07) opens at once, its read still on the way: closed, the account changes behind it.
        page.get_by_role("dialog", name="Definition history").get_by_role("button", name="Close dialog").click()
        switch_account(page, "staff")
        assert len(held) == 1
        route, response = held[0]
        # Leaving the page aborted the read (api.ts leavePage); a late answer must still change nothing.
        try:
            route.fulfill(response=response)
        except Exception:
            pass  # the browser already dropped the aborted request
        navigate(page, "inbox")
        expect(page.get_by_role("dialog", name="Definition history")).to_have_count(0)
        expect(page.locator(".toast.error")).to_have_count(0)
        assert not errors
    finally:
        context.close()


def test_which_rule_lookup_explains_failure_and_ignores_a_previous_person_reply(browser, live_server, h):
    a, b = h.seed["people"][:2]
    first = h.policy({"kind": "max_hours", "hours": 12, "window": "week"}, scope={"person_id": a})
    second = h.policy({"kind": "max_hours", "hours": 24, "window": "week"}, scope={"person_id": b})
    context, page = sign_in(browser, live_server, "coordinator")
    errors, held = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        navigate(page, "rules")
        page.get_by_role("button", name="Which rule applies?", exact=True).click()
        dialog = page.get_by_role("dialog", name="Which rule applies?", exact=True)
        day, who = dialog.get_by_label("Day", exact=True), dialog.get_by_label("Person", exact=True)
        day.fill("2026-10-04")
        pattern = "**/api/policies/applies?*"
        page.route(pattern, lambda route: route.abort())
        who.select_option(a)
        expect(dialog.locator(".notice.error")).to_contain_text("The server could not be reached")
        page.unroute(pattern)
        who.select_option(b)
        expect(dialog.locator(".which-rule-rows")).to_contain_text(second["name"])

        def delay_previous_person(route):
            if f"person_id={a}&" in route.request.url:
                held.append((route, route_fetch(route)))
            else:
                route.continue_()

        page.route(pattern, delay_previous_person)
        with page.expect_request(lambda request: f"person_id={a}&" in request.url):
            who.select_option(a)
        with page.expect_response(lambda reply: f"person_id={b}&" in reply.url) as current:
            who.select_option(b)
        current.value.finished()
        expect(dialog.locator(".which-rule-rows")).to_contain_text(second["name"])
        assert len(held) == 1
        route, response = held[0]
        with page.expect_response(lambda reply: f"person_id={a}&" in reply.url) as completed:
            route.fulfill(response=response)
        completed.value.finished()
        # Consume browser microtasks/render frames after the held, real response is delivered.
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect(who).to_have_value(b)
        expect(dialog.locator(".which-rule-rows")).to_contain_text(second["name"])
        expect(dialog.locator(".which-rule-rows")).not_to_contain_text(first["name"])
        assert not errors
    finally:
        context.close()

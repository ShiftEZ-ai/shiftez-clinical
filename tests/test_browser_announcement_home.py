"""Home records reading only after its actual notice cards have been attached."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, route_fetch, sign_in, wait_held  # noqa: F401
from .test_announcement_receipts import counts, notice, notes, receipts, required_links

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("late_content", [False, True], ids=["normal", "late-card"])
def test_home_records_only_rendered_announcements_after_mount(browser, live_server, h, late_content):
    context, page = sign_in(browser, live_server, "staff")
    try:
        expect(page.locator(".home-notice-list")).to_be_attached()
        navigate(page, "inbox")
        rows = [notice(h, {"roles": ["staff"]}, name=name,
            importance="must_acknowledge" if index == 0 else "normal")
            for index, name in enumerate(("A required notice", "C team notice", "D team notice", "E later notice", "F later notice"))]
        resource = h.command("coordinator", "catalogue.create", {"catalogue": "department_content", "reason": "Share a resource",
            "value": {"name": "B staff resource", "kind": "link", "url": "https://example.org/resource",
                "starts_on": "2020-01-01", "audience": {"roles": ["staff"]}}})["record"]
        # Made elsewhere by the harness, whose live change stays in the test process (SQLite): the tab learns of it as a
        # fresh tab does, so Home's content read below is the server's, not the reply kept since sign-in .
        page.reload()
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "inbox")
        seen_calls = []

        def check_command(route):
            value = route.request.post_data_json
            if value["kind"] == "department_content.seen":
                listing = page.locator(".home-notice-list")
                assert listing.evaluate("node => node.isConnected")
                rendered_ids = listing.locator("[data-notice-id]").evaluate_all("nodes => nodes.map(node => node.dataset.noticeId)")
                assert value["payload"]["ids"] == rendered_ids
                seen_calls.append(rendered_ids)
            route.continue_()

        page.route("**/api/commands", check_command)
        held = []
        if late_content:
            page.route("**/api/content?*", lambda route: held.append((route, route_fetch(route))))
            navigate(page, "home")
            wait_held(page, held)
            skeleton = page.locator(".home-notices .loading")
            expect(skeleton).not_to_have_class(re.compile(r"\bsk-wait\b"))
            expect(page.locator(".home-notice-list")).to_have_count(0)
            assert seen_calls == []
            assert all(counts(h, row["id"])["read"] == 0 for row in rows)

        with page.expect_response(lambda response: response.url.endswith("/api/commands")
                and response.request.post_data_json.get("kind") == "department_content.seen") as recorded:
            if late_content:
                route, response = held.pop()
                assert response.ok
                route.fulfill(response=response)
            else:
                navigate(page, "home")
        assert recorded.value.ok
        # SW3-D-19: inject the synthetic failure after Home has drawn; page changes clear an older page's save error.
        page.evaluate("window.dispatchEvent(new CustomEvent('rota-save-failed', {detail: {kind: 'request.create', message: 'Synthetic edit failed'}}))")
        expect(page.locator("#save-state")).to_have_attribute("aria-label", "Not saved: Synthetic edit failed")
        expect(page.locator(".home-notice-list li")).to_have_count(4)
        expect(page.locator(".home-notice-list")).to_contain_text(resource["name"])
        expected_ids = [row["id"] for row in rows[:3]]
        assert seen_calls == [expected_ids]
        for index, row in enumerate(rows):
            own = receipts(h, row["id"])["receipts"][0]
            assert bool(own["read_at"]) == (index < 3)
            assert own["acknowledged_at"] is None
        assert counts(h, resource["id"])["read"] == 0
        navigate(page, "inbox")
        assert seen_calls == [expected_ids]
    finally:
        context.close()


def test_home_never_hides_pending_acknowledgements_behind_the_limit(browser, live_server, h):
    required = [notice(h, {"roles": ["staff"]}, importance="must_acknowledge", name=f"Required {index}") for index in range(5)]
    for name in ("X normal notice", "Y normal notice"):
        notice(h, {"roles": ["staff"]}, name=name)
    context, page = sign_in(browser, live_server, "staff")
    try:
        items = page.locator(".home-notice-list li")
        expect(items).to_have_count(5)
        fifth = items.filter(has_text="Required 4")
        fifth.get_by_role("button", name="I acknowledge: Required 4", exact=True).click()
        expect(fifth.get_by_text("Acknowledged", exact=True)).to_be_visible()
        assert receipts(h, required[4]["id"])["receipts"][0]["acknowledged_at"]

        page.get_by_role("button", name="Show all 7 notices").click()
        expect(items).to_have_count(7)
        expect(page.locator(".home-notice-list")).to_be_focused()
        expect(page.get_by_role("button", name="Show all 7 notices")).to_have_count(0)
        expect(items.filter(has_text="Required 4").get_by_text("Acknowledged", exact=True)).to_be_visible()
        expect(items.filter(has_text="Y normal notice")).to_be_visible()
    finally:
        context.close()


def test_home_only_announcement_can_be_acknowledged_among_required_links(browser, live_server, h):
    links = required_links(h)
    row = notice(h, {"roles": ["staff"]}, importance="must_acknowledge", name="Z required announcement")
    context, page = sign_in(browser, live_server, "staff")
    try:
        card = page.locator(".home-notice-list li").filter(has_text=row["name"])
        expect(card).to_be_visible()
        card.get_by_role("button", name="I acknowledge: " + row["name"], exact=True).click()
        expect(card.get_by_text("Acknowledged", exact=True)).to_be_visible()
        own = receipts(h, row["id"])["receipts"][0]
        assert own["read_at"] and own["acknowledged_at"] and own["delivered_at"] is None
        assert notes(h, row["id"]) == []

        page.reload()
        expect(card).to_have_count(0)
        expect(page.locator(".home-notice-list li")).to_have_count(4)
        for link in links:
            expect(page.locator(".home-notice-list")).to_contain_text(link["name"])
            assert counts(h, link["id"])["read"] == 0
    finally:
        context.close()

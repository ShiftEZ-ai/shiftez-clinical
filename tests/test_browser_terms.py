"""One name per concept, and the small gaps closed with it: review returns a rota, sign-in can be enabled again,
an Admin role chip can be revoked, and a coordinator can follow the Setup guide."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, decide, no_opening_hours  # noqa: F401

pytestmark = pytest.mark.browser


def test_sidebar_labels_start_their_page_titles(browser, live_server, h):
    context, page = sign_in(browser, live_server, "admin")
    try:
        nav = page.locator("#department-nav")
        for key, label, title in [("system", "System", "System"), ("inbox", "Inbox", "Inbox"), ("access", "Access & approvals", "Access & approvals"), ("insights", "Insights", "Insights")]:
            if key == "inbox":  # the Inbox opens from the top-bar bell, not the sidebar
                expect(page.locator(".topbar .bell")).to_have_attribute("aria-label", re.compile(r"^Inbox( |$)"))
            else:
                expect(nav.locator(f"[data-route={key}]")).to_contain_text(label)
            navigate(page, key)
            expect(page.locator(".topbar h1")).to_have_text(title)
    finally:
        context.close()


def test_approver_returns_a_rota_in_review_to_the_coordinator(browser, live_server, h):
    w = h.lifecycle(h.shift(h.workspace("Rota to return"))["workspace"], "submit")["workspace"]
    context, page = sign_in(browser, live_server, "approver")
    try:
        navigate(page, "planner")
        decide(page, "Return to coordinator")
        expect(page.get_by_text("Returned to the coordinator.", exact=False).first).to_be_visible()
        assert h.detail(w)["workspace"]["status"] == "draft"
    finally:
        context.close()


def test_admin_enables_sign_in_again_and_cannot_remove_own_last_admin_role(browser, live_server, h):
    access = h.get("admin", "/api/access")
    staff = next(i for i in access["identities"] if i["id"] == h.seed["identities"]["staff"])
    admin = next(i for i in access["identities"] if i["id"] == h.seed["identities"]["admin"])
    h.command("admin", "identity.disable", {"reason": "Left for a term"}, staff["id"], staff["revision"])
    context, page = sign_in(browser, live_server, "admin")
    try:
        navigate(page,"access/people")
        # Roles are removed from the row's actions; your own last Admin role is not offered (no lock-out).
        expect(page.locator(".chip-x")).to_have_count(0)
        more = page.get_by_role("button", name=f"More actions for {admin['display_name']}", exact=True)
        if more.count():
            more.first.click()
            expect(page.get_by_role("menu", name="Row actions")).to_be_visible()
            expect(page.get_by_role("menuitem", name="Remove Admin role", exact=True)).to_have_count(0)
            page.keyboard.press("Escape")
        page.get_by_role("button", name=f"More actions for {staff['display_name']}", exact=True).first.click()
        page.get_by_role("menuitem", name="Enable sign-in", exact=True).click()
        decide_dialog = page.get_by_role("dialog")
        decide_dialog.get_by_label("Reason", exact=True).fill("Back from leave")
        decide_dialog.get_by_role("button", name="Enable sign-in", exact=True).click()
        expect(decide_dialog).not_to_be_visible()
        assert not next(i for i in h.get("admin", "/api/access")["identities"] if i["id"] == staff["id"])["disabled"]
    finally:
        context.close()


def test_coordinator_follows_the_setup_guide_without_admin_actions(browser, live_server, h):
    no_opening_hours(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page,"configuration/setup")
        main = page.locator("#main")
        # Done steps hide their notes (C-04 puts done admin steps such as Locations first): the open step says it.
        expect(main.get_by_text("An admin sets this up.", exact=True).filter(visible=True).first).to_be_visible()
        expect(main.get_by_role("button", name="Add an announcement", exact=True).or_(main.get_by_role("button", name="Review Announcements", exact=True))).to_be_visible()
        expect(main.get_by_role("button", name="Review People & eligibility", exact=True)).to_be_visible()
        # The setup guide lives in Configuration: in the sidebar while setup is incomplete (no rota yet in this seed).
        expect(page.locator("#department-nav [data-route=configuration]")).to_have_count(1)
        expect(page.locator("#department-nav [data-route=setup]")).to_have_count(0)
        # The guide should only link to sections the coordinator can use.
        nav = page.get_by_role("navigation", name="Configuration sections")
        sections = nav.get_by_role("button")
        allowed_sections = {"setup", "scheduling_groups", "tags", "auto_schedule", "department_content"}
        expect(sections).to_have_count(len(allowed_sections))
        assert set(sections.evaluate_all("buttons => buttons.map(button => button.dataset.section)")) == allowed_sections
        expect(nav.locator("[data-section=auto_schedule]")).to_have_count(1)
        expect(sections.filter(has_text="Scheduling groups")).to_have_count(1)
        expect(sections.filter(has_text="Tags")).to_have_count(1)
    finally:
        context.close()

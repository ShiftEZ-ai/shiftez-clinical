"""PostgreSQL journeys for review, personal views and previewed changes."""

import re

import pytest
from playwright.sync_api import expect

from .test_browser import (
    browser,
    live_server,
    navigate,
    open_rota,
    pick_view,
    restart_app,
    save_dialog,
    sign_in,
    toolbar,
)  # noqa: F401

pytestmark = [
    pytest.mark.browser,
    pytest.mark.parametrize("h", [pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True),
]


def grant(h, account, role):
    h.command("admin", "role.grant", {
        "identity_id": h.seed["identities"][account],
        "role": role,
        "starts_on": "2020-01-01",
        "reason": "Set up a synthetic workflow account",
    })


def review_step(page, label):
    return page.locator(".review-strip").get_by_role("row").filter(
        has=page.get_by_role("cell", name=label, exact=True),
    )


def approve(page):
    page.get_by_role("button", name="Approve rota", exact=True).click()
    save_dialog(page.get_by_role("dialog", name="Approve rota", exact=True), "Approve rota")


def test_review_route_requires_two_independent_steps_before_publication(browser, live_server, h):
    grant(h, "admin", "coordinator")
    grant(h, "credentialer", "approver")
    route_name = "Two-step rota review"
    first, second = "Coverage review", "Release review"

    owner_context, owner = sign_in(browser, live_server, "admin")
    reviewer_context, reviewer = sign_in(browser, live_server, "approver")
    try:
        navigate(owner, "access/routes")
        owner.get_by_role("button", name="Propose review route", exact=True).click()
        proposal = owner.get_by_role("dialog", name="Propose review route", exact=True)
        proposal.get_by_label("Route name", exact=True).fill(route_name)
        proposal.get_by_label("Effective from", exact=True).fill("2026-10-01")
        proposal.get_by_role("button", name="Add routine review step", exact=True).click()
        steps = proposal.locator("section.form-section").get_by_label("Step label", exact=True)
        expect(steps).to_have_count(2)
        steps.nth(0).fill(first)
        steps.nth(1).fill(second)
        proposal.get_by_label("Configuration reason", exact=True).fill("Require separate review decisions")
        save_dialog(proposal, "Save route proposal")
        restart_app(owner)

        card = owner.locator(".route-card").filter(has=owner.get_by_role("heading", name=route_name, exact=True))
        expect(card.get_by_text("Waiting for approval", exact=True)).to_be_visible()
        expect(card.get_by_role("cell", name=first, exact=True)).to_be_visible()
        expect(card.get_by_role("cell", name=second, exact=True)).to_be_visible()

        navigate(reviewer, "access/routes")
        reviewer.get_by_role("button", name="Activate route", exact=True).click()
        activation = reviewer.get_by_role("dialog", name="Activate review route", exact=True)
        activation.get_by_label("Reason", exact=True).fill("Activate the independently reviewed route")
        save_dialog(activation, "Confirm")
        restart_app(owner)
        expect(card.get_by_text("Active", exact=True)).to_be_visible()

        draft = h.shift(h.workspace("Rota for independent review"))["workspace"]
        open_rota(owner, draft["id"])
        owner.get_by_role("button", name="Submit for review…", exact=True).click()
        submit = owner.get_by_role("dialog", name=f"Submit {draft['name']} for review", exact=True)
        save_dialog(submit, "Submit for review")
        restart_app(owner)
        expect(review_step(owner, first).get_by_role("cell", name="Waiting for approval", exact=True)).to_be_visible()
        expect(review_step(owner, second).get_by_role("cell", name="Not yet open", exact=True)).to_be_visible()

        open_rota(reviewer, draft["id"])
        approve(reviewer)
        restart_app(owner)
        expect(review_step(owner, first).get_by_role("cell", name="Complete", exact=True)).to_be_visible()
        expect(review_step(owner, second).get_by_role("cell", name="Waiting for approval", exact=True)).to_be_visible()
        expect(owner.get_by_role("button", name="Publish…", exact=True)).to_have_count(0)
        restart_app(reviewer)
        expect(review_step(reviewer, second).get_by_role("cell", name="Waiting for approval", exact=True)).to_be_visible()
        expect(reviewer.get_by_role("button", name="Approve rota", exact=True)).to_have_count(0)

        current = h.detail(draft)["workspace"]
        assert h.lifecycle(current, "approve", "approver", status=409)["error"]["code"] == "DUPLICATE_REVIEWER"
        assert h.lifecycle(current, "publish", "admin", status=409)["error"]["code"] == "WORKFLOW_STATE"
        assert h.get("admin", "/api/publications")["publications"] == []

        second_context, second_reviewer = sign_in(browser, live_server, "credentialer")
        try:
            open_rota(second_reviewer, draft["id"])
            approve(second_reviewer)
        finally:
            second_context.close()

        restart_app(owner)
        expect(owner.locator(".rota-status")).to_have_text("Approved")
        owner.locator(".review-strip summary").click()
        for label in (first, second):
            expect(review_step(owner, label).get_by_role("cell", name="Complete", exact=True)).to_be_visible()
        assert h.get("admin", "/api/publications")["publications"] == []

        owner.get_by_role("button", name="Publish…", exact=True).click()
        publish = owner.get_by_role("dialog", name=f"Publish {draft['name']}", exact=True)
        publish.get_by_role("button", name="Publish", exact=True).click()
        publish.get_by_role("button", name="Confirm publish?", exact=True).click()
        expect(publish).not_to_be_visible()
        publication, = h.get("admin", "/api/publications")["publications"]
        assert publication["name"] == draft["name"] and publication["current"]
        navigate(owner, f"schedule/week/{draft['starts_on']}?source={publication['id']}")
        restart_app(owner)
        expect(owner.get_by_text(f"{draft['name']} · version 1 is published and read-only.", exact=True)).to_be_visible()
    finally:
        reviewer_context.close()
        owner_context.close()


def test_personal_list_survives_reload_and_bulk_closure_requires_explicit_save(browser, live_server, h):
    draft = h.workspace("Rota for bulk closure")
    for day in ("2026-10-04", "2026-10-05", "2026-10-06"):
        draft = h.shift(draft, start=f"{day}T08:00:00+03:00", end=f"{day}T12:00:00+03:00")["workspace"]
    before = h.detail(draft)
    first, second, untouched = sorted(before["shifts"], key=lambda shift: shift["start"])
    reason = "Equipment unavailable for both sessions"
    name = "My session list"

    context, page = sign_in(browser, live_server, "coordinator")
    try:
        open_rota(page, draft["id"])
        pick_view(page, "List")
        bar = page.locator(".planner-toolbar")
        toolbar(bar, "Saved views").get_by_role("menuitem", name="New view…", exact=True).click()
        view = page.get_by_role("dialog", name="New view", exact=True)
        view.get_by_label("View name", exact=True).fill(name)
        expect(view.get_by_label("Share with the department", exact=True)).not_to_be_checked()
        save_dialog(view, "Save view")
        saved = bar.get_by_role("button", name=f"Saved views: {name}", exact=True)
        expect(saved).to_be_visible()
        restart_app(page)
        expect(saved).to_be_visible()
        session_list = page.get_by_role("region", name="List view", exact=True)
        expect(session_list.get_by_role("columnheader", name="Duty", exact=True)).to_be_visible()
        rows = [session_list.locator(f'tr[data-session="{shift["id"]}"]') for shift in (first, second, untouched)]
        for row in rows[:2]:
            row.get_by_role("checkbox").check()
        actions = page.get_by_role("toolbar", name="Actions for selected sessions", exact=True)
        expect(actions.get_by_text("2 sessions selected", exact=True)).to_have_count(1)
        actions.get_by_role("button", name="Close sessions", exact=True).click()

        dialog = page.get_by_role("dialog", name="Close sessions", exact=True)
        dialog.get_by_label("Closure reason", exact=True).fill(reason)
        dialog.get_by_role("button", name="Calculate impact", exact=True).click()
        expect(dialog.get_by_text("Not saved yet. 0 added, 0 removed, 2 changed.", exact=False)).to_be_visible()
        impact = dialog.get_by_role("table").filter(has=page.get_by_role("columnheader", name="Changed fields", exact=True))
        expect(impact.get_by_role("cell", name=re.compile(r" · Open · Unlocked$"))).to_have_count(2)
        expect(impact.get_by_role("cell", name=re.compile(rf" · Closed · {re.escape(reason)} · Unlocked$"))).to_have_count(2)
        after_preview = h.detail(draft)
        assert after_preview["shifts"] == before["shifts"]
        assert after_preview["workspace"]["content_revision"] == before["workspace"]["content_revision"]

        save_dialog(dialog, "Apply previewed draft change")
        restart_app(page)
        expect(saved).to_be_visible()
        for row in rows[:2]:
            expect(row.get_by_text("Closed", exact=True)).to_be_visible()
        expect(rows[2]).to_be_visible()
        expect(rows[2].get_by_text("Closed", exact=True)).to_have_count(0)
        persisted = {shift["id"]: shift for shift in h.detail(draft)["shifts"]}
        for shift in (first, second):
            assert persisted[shift["id"]]["cancelled"]
            assert persisted[shift["id"]]["cancellation_reason"] == reason
        assert persisted[untouched["id"]] == untouched
    finally:
        context.close()


def configuration_record(h, kind, record_id):
    return next(row for row in h.get("admin", "/api/catalogues")[kind] if row["id"] == record_id)


def edit_configuration(page, address, record, title):
    navigate(page, f"configuration/{address}")
    row = page.locator(f'tr[data-row="{record["id"]}"]')
    expect(row.get_by_role("heading", name=record["name"], exact=True)).to_be_visible()
    row.get_by_role("button", name="Edit", exact=True).click()
    return page.get_by_role("dialog", name=title, exact=True)


def test_resource_and_opening_hours_preview_the_linked_rota_before_saving(browser, live_server, h):
    calendar = h.configure("operating_calendars", {
        "name": "Equipment opening hours",
        "starts_on": "2020-01-01",
        "definition": {
            "timezone": h.get("admin", "/api/me")["organisation"]["timezone"],
            "week": [{"weekday": 6, "windows": [{"start_time": "08:00", "end_time": "10:00"}]}],
        },
    })["record"]
    pool = h.configure("resource_pools", {
        "name": "Shared equipment", "starts_on": "2020-01-01", "capacity": 1,
    })["record"]
    duty = h.configure("assignment_types", {
        "name": "Equipment booking", "starts_on": "2020-01-01",
        "definition": {"resources": [{"resource_pool_id": pool["id"], "units": 1}]},
    })["record"]
    draft = h.shift(h.workspace("Rota using shared equipment"), typ=duty["id"])["workspace"]
    finding = "The required interval is outside configured opening windows"

    context, page = sign_in(browser, live_server, "admin")
    try:
        resource = edit_configuration(page, "resource_pools", pool, "Edit resource pool")
        resource.get_by_label("Opening hours", exact=True).select_option(label=calendar["name"])
        expect(resource.get_by_text("1 active rota checked.", exact=True)).to_be_visible(timeout=20000)
        expect(resource.get_by_role("cell", name=finding, exact=True)).to_be_visible()
        expect(resource.get_by_role("cell", name="Introduced", exact=True)).to_be_visible()
        expect(resource.get_by_role("row").filter(has_text=draft["name"])).to_contain_text("0 → 1")
        assert configuration_record(h, "resource_pools", pool["id"])["calendar_id"] is None
        save_dialog(resource, "Save version")
        restart_app(page)
        resource_row = page.locator(f'tr[data-row="{pool["id"]}"]')
        expect(resource_row.get_by_text(f"Department-wide · {calendar['name']}", exact=True)).to_be_visible()

        opening = edit_configuration(page, "opening-hours", calendar, "Edit opening hours")
        end = opening.get_by_label("Sunday opening end", exact=True)
        expect(end).to_have_value("10:00")
        end.fill("12:00")
        expect(opening.get_by_text("1 finding resolved.", exact=True)).to_be_visible(timeout=20000)
        expect(opening.get_by_role("cell", name=finding, exact=True)).to_be_visible()
        expect(opening.get_by_role("cell", name="Resolved", exact=True)).to_be_visible()
        expect(opening.get_by_role("row").filter(has_text=draft["name"])).to_contain_text("1 → 0")
        assert configuration_record(h, "operating_calendars", calendar["id"])["definition"] == calendar["definition"]
        save_dialog(opening, "Save version")
        restart_app(page)
        reopened = edit_configuration(page, "opening-hours", calendar, "Edit opening hours")
        expect(reopened.get_by_label("Sunday opening end", exact=True)).to_have_value("12:00")
        assert configuration_record(h, "resource_pools", pool["id"])["calendar_id"] == calendar["id"]
    finally:
        context.close()

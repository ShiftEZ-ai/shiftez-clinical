"""PostgreSQL journeys for drafting, reviewed changes and the owner's reference views."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from packages.domain.time import now

from .test_browser import (  # noqa: F401 -- browser and live_server are shared fixtures
    browser,
    close_popover,
    decide,
    expect_loaded,
    live_server,
    navigate,
    new_draft,
    open_rota,
    open_session,
    pick_view,
    save_dialog,
    session_action,
    sign_in,
    toolbar,
)

pytestmark = [
    pytest.mark.browser,
    pytest.mark.parametrize("h", [pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True),
]

EDITOR = "Edit session and participants"


def reload_view(page, view):
    page.reload(wait_until="domcontentloaded")
    expect_loaded(page, view)


def assert_draft(h, page, workspace):
    expect(page.get_by_role("button", name=f"Switch draft · {workspace['name']}", exact=True)).to_be_visible()
    expect(page.locator(".planner-bar .rota-status")).to_have_text("Draft")
    stored = h.detail(workspace)["workspace"]
    assert stored["status"] == "draft"
    assert not stored.get("publication_id")
    return stored


def person_name(h, person_id):
    people = h.get("coordinator", "/api/catalogues")["people"]
    return next(person["display_name"] for person in people if person["id"] == person_id)


def department_now(h):
    zone = ZoneInfo(h.get("coordinator", "/api/me")["organisation"]["timezone"])
    return now().astimezone(zone)


def session_time(day, hour, zone):
    return datetime.combine(day, time(hour), zone).isoformat()


def duty_row(page, shift_id):
    return page.locator(f'tr[data-session="{shift_id}"]')


def test_reconcile_previews_only_later_occurrences_and_keeps_duty_ids(browser, live_server, h):
    rota = h.workspace("Recurring duty draft")
    name = "Two-day recurring duty"
    definition = {
        "anchor": rota["starts_on"],
        "days": [{"weekday": day, "start_time": "08:00", "end_time": "12:00"} for day in (0, 1)],
        "service_ids": [h.seed["services"][0]],
    }
    series_value = {
        "name": name,
        "starts_on": rota["starts_on"],
        "ends_on": rota["ends_on"],
        "assignment_type_id": h.seed["type"],
        "location_id": h.seed["locations"][0],
        "definition": definition,
    }
    series = h.configure("recurring_series", series_value)["record"]
    rota = h.command(
        "coordinator", "series.expand", {"workspace_id": rota["id"], "series_id": series["id"]},
        revision=rota["revision"],
    )["workspace"]
    series_value["definition"] = {
        **definition,
        "days": [dict(day, start_time="09:00", end_time="13:00") for day in definition["days"]],
    }
    h.configure("recurring_series", series_value, series["id"], series["revision"])
    stored = h.detail(rota)
    earlier, later = sorted(stored["shifts"], key=lambda duty: duty["occurrence_on"])

    context, page = sign_in(browser, live_server, "coordinator")
    with context:
        open_rota(page, rota["id"])
        page.get_by_role("button", name="Rota tools", exact=True).click()
        page.get_by_role("menuitem", name="Update recurring sessions", exact=True).click()
        dialog = page.get_by_role("dialog", name="Update recurring sessions", exact=True)
        dialog.get_by_label("Recurring session", exact=True).select_option(series["id"])
        dialog.get_by_label("From", exact=True).fill(later["occurrence_on"])
        dialog.get_by_label("Change reason", exact=True).fill("Move only the later occurrence")
        dialog.get_by_role("button", name="Calculate draft impact", exact=True).click()
        expect(dialog.get_by_text("0 added, 0 removed, 1 changed;", exact=False)).to_be_visible()
        changes = dialog.get_by_role("row").filter(has_text="08:00").filter(has_text="09:00")
        expect(changes).to_have_count(1)
        expect(changes).to_contain_text("13:00")
        unchanged = h.detail(rota)
        assert unchanged["shifts"] == stored["shifts"]
        assert unchanged["workspace"]["revision"] == stored["workspace"]["revision"]

        dialog.get_by_role("button", name="Update sessions", exact=True).click()
        expect(dialog).not_to_be_visible()
        reload_view(page, "schedule")
        assert_draft(h, page, rota)
        pick_view(page, "List")
        expect(duty_row(page, earlier["id"])).to_contain_text("08:00")
        expect(duty_row(page, later["id"])).to_contain_text("09:00")
        updated = {duty["id"]: duty for duty in h.detail(rota)["shifts"]}
        assert updated.keys() == {earlier["id"], later["id"]}
        assert updated[earlier["id"]] == earlier
        assert updated[later["id"]]["occurrence_on"] == later["occurrence_on"]
        assert updated[later["id"]]["start"] != later["start"]
        assert updated[later["id"]]["end"] != later["end"]


def test_segmented_presence_survives_preview_and_explicit_draft_save(browser, live_server, h):
    rota = h.workspace("Segmented duty draft")
    staff = h.seed["people"][0]
    start = "2026-10-04T08:00:00+03:00"
    end = "2026-10-04T12:00:00+03:00"
    participant = {
        "person_id": staff, "start": start, "end": end,
        "segments": [
            {"start": start, "end": "2026-10-04T09:30:00+03:00"},
            {"start": "2026-10-04T10:15:00+03:00", "end": end},
        ],
    }
    rota = h.shift(rota, people=[participant], start=start, end=end)["workspace"]
    original, = h.detail(rota)["shifts"]
    segment_text = "08:00–09:30, 10:15–12:00"

    context, page = sign_in(browser, live_server, "coordinator")
    with context:
        open_rota(page, rota["id"])
        open_session(page)
        expect(page.locator(".wk-session").get_by_text(segment_text, exact=True)).to_be_visible()
        page.get_by_role("button", name="Edit session", exact=True).click()
        editor = page.get_by_role("region", name=EDITOR, exact=True)
        editor.get_by_label("Session end time", exact=True).fill("12:30")
        editor.get_by_role("button", name="Calculate draft impact", exact=True).click()
        expect(editor.get_by_text("Not saved yet:", exact=False)).to_be_visible()
        shown_change = editor.get_by_role("row").filter(has_text="12:30")
        assert h.detail(rota)["shifts"] == [original]
        assert assert_draft(h, page, rota)["revision"] == rota["revision"]
        expect(shown_change.get_by_role("cell").nth(2)).to_contain_text("presence 08:00–09:30; 10:15–12:00")

        editor.get_by_role("button", name="Save draft session", exact=True).click()
        expect(editor).not_to_be_visible()
        reload_view(page, "schedule")
        assert_draft(h, page, rota)
        open_session(page)
        expect(page.locator(".wk-session").get_by_text(segment_text, exact=True)).to_be_visible()
        saved, = h.detail(rota)["shifts"]
        assert saved["id"] == original["id"] and saved["end"] != original["end"]
        assert saved["participants"][0]["segments"] == original["participants"][0]["segments"]


def test_staffing_choice_previews_without_saving_until_explicit_save(browser, live_server, h):
    rota = h.shift(h.workspace("Staffing choice draft"), people=[])["workspace"]
    original, = h.detail(rota)["shifts"]
    candidate_id = h.seed["people"][0]
    candidate = person_name(h, candidate_id)

    context, page = sign_in(browser, live_server, "coordinator")
    with context:
        open_rota(page, rota["id"])
        open_session(page)
        expect(page.locator(".wk-session").get_by_text("No people assigned", exact=True)).to_be_visible()
        session_action(page, "Find staffing or replacement options")
        options = page.get_by_role("region", name="Staffing and replacement options", exact=True)
        options.get_by_role("button", name="Calculate staffing options", exact=True).click()
        expect(options.get_by_text("No changes saved.", exact=False)).to_be_visible()
        assert h.detail(rota)["shifts"] == [original]
        options.get_by_role("button", name=f"Review {candidate} in draft", exact=True).click()
        editor = page.get_by_role("region", name=EDITOR, exact=True)
        expect(editor.get_by_text("Not saved yet:", exact=False)).to_be_visible()
        expect(editor.get_by_role("row").filter(has_text="Unassigned")).to_contain_text(candidate)
        assert h.detail(rota)["shifts"] == [original]
        assert assert_draft(h, page, rota)["revision"] == rota["revision"]

        editor.get_by_role("button", name="Save draft session", exact=True).click()
        expect(editor).not_to_be_visible()
        reload_view(page, "schedule")
        assert_draft(h, page, rota)
        open_session(page)
        expect(page.locator(".wk-session").get_by_role("button", name=candidate)).to_be_visible()
        saved, = h.detail(rota)["shifts"]
        assert saved["id"] == original["id"]
        assert [person["person_id"] for person in saved["participants"]] == [candidate_id]


def test_publish_independently_activated_quota_commitment(browser, live_server, h):
    local = department_now(h)
    first = local.date() + timedelta(days=1)
    last = first + timedelta(days=6)
    title = "Weekly duty ceiling"
    rota_name = "Quota commitment rota"
    staff = person_name(h, h.seed["people"][0])

    owner, page = sign_in(browser, live_server, "coordinator")
    with owner:
        navigate(page, "rules")
        page.get_by_role("button", name="Add rule", exact=True).click()
        form = page.get_by_role("dialog", name="Add rule", exact=True)
        form.get_by_role("radio", name="Quota or commitment", exact=True).check()
        form.get_by_label("Maximum", exact=True).fill("5")
        form.get_by_label("From", exact=True).fill(first.isoformat())
        form.get_by_label("Until (included)", exact=True).fill(last.isoformat())
        form.get_by_label("Effective from", exact=True).fill(first.isoformat())
        form.get_by_label("Rule title", exact=True).fill(title)
        form.get_by_label("Policy owner", exact=True).fill("Scheduling owner")
        form.get_by_label("Source or departmental decision", exact=True).fill("Reviewed synthetic duty limit")
        save_dialog(form, "Propose rule")
        proposals = h.get("coordinator", "/api/policies?status=proposed")["policies"]
        proposal = next(rule for rule in proposals if rule["name"] == title)
        rule_row = f'[data-rule-ids~="{proposal["id"]}"]'
        reload_view(page, "rules")
        expect(page.locator(rule_row)).to_contain_text(title)
        expect(page.locator(rule_row).get_by_role("button", name="Review", exact=True)).to_have_count(0)

        reviewer, review_page = sign_in(browser, live_server, "approver")
        with reviewer:
            navigate(review_page, "rules")
            review_page.locator(rule_row).get_by_role("button", name="Review", exact=True).click()
            review = review_page.get_by_role("dialog", name=f"Review {title}", exact=True)
            expect(review.get_by_text("No rota being planned overlaps these dates.", exact=False)).to_be_visible()
            review.get_by_label("Reason", exact=True).fill("Independent review of the duty ceiling")
            review.get_by_role("button", name="Activate", exact=True).click()
            expect(review).not_to_be_visible()
            reload_view(review_page, "rules")
            expect(review_page.locator(rule_row)).to_contain_text(title)
            expect(review_page.locator(rule_row).get_by_role("button", name="Review", exact=True)).to_have_count(0)
            active = h.get("approver", "/api/policies?status=active")["policies"]
            assert any(rule["id"] == proposal["id"] for rule in active)

            rota = h.workspace(rota_name, first.isoformat(), (last + timedelta(days=1)).isoformat())
            rota = h.shift(rota, session_time(first, 8, local.tzinfo), session_time(first, 12, local.tzinfo))["workspace"]
            open_rota(page, rota["id"])
            decide(page, "Submit for review")
            reload_view(page, "schedule")
            expect(page.get_by_role("button", name="Reopen as draft", exact=True)).to_be_visible()
            assert h.detail(rota)["workspace"]["status"] == "submitted"

            open_rota(review_page, rota["id"])
            decide(review_page, "Approve rota")
            reload_view(review_page, "schedule")
            expect(review_page.get_by_role("button", name="Publish…", exact=True)).to_be_visible()
            assert h.detail(rota)["workspace"]["status"] == "approved"

        reload_view(page, "schedule")
        decide(page, "Publish rota")
        reload_view(page, "schedule")
        publications = h.get("coordinator", "/api/publications")["publications"]
        published = next(item for item in publications if item["workspace_id"] == rota["id"])
        assert published["current"]

        navigate(page, "insights/workload")
        expect(page.get_by_label("Version", exact=True)).to_have_value(published["id"])
        reload_view(page, "insights/workload")
        expect(page.get_by_label("Version", exact=True)).to_have_value(published["id"])
        expect(page.get_by_role("heading", name="Targets and limits", exact=True)).to_be_visible()
        row = page.get_by_role("row").filter(has_text=f"{title} · v").filter(has_text=staff)
        expect(row.get_by_role("cell", name="maximum", exact=True)).to_be_visible()
        expect(row.get_by_role("cell", name="5", exact=True)).to_be_visible()
        expect(row.get_by_role("cell", name="1", exact=True)).to_be_visible()
        expect(row.get_by_role("cell", name="met", exact=True)).to_be_visible()


def test_create_unpublished_draft_and_reopen_it(browser, live_server, h):
    title = "Unpublished planning draft"
    context, page = sign_in(browser, live_server, "coordinator")
    with context:
        navigate(page, "schedule")
        new_draft(page)
        dialog = page.get_by_role("dialog", name="New rota", exact=True)
        dialog.get_by_label("Rota name", exact=True).fill(title)
        save_dialog(dialog, "Create draft")
        expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()

        reload_view(page, "schedule")
        rotas = h.get("coordinator", "/api/workspaces")["workspaces"]
        rota = next(item for item in rotas if item["name"] == title)
        assert_draft(h, page, rota)
        assert all(item["workspace_id"] != rota["id"] for item in h.get("coordinator", "/api/publications")["publications"])


def test_decide_staff_request_and_read_persisted_decision(browser, live_server, h):
    local = department_now(h)
    day = local.date() + timedelta(days=3)
    staff = h.seed["people"][0]
    reason = "Synthetic staff request for review"
    decision = "Reviewed and declined the requested date"
    record = h.command("staff", "request.create", {
        "person_id": staff,
        "start": session_time(day, 8, local.tzinfo),
        "end": session_time(day, 12, local.tzinfo),
        "private_reason": reason,
        "kind": "leave",
    })["record"]

    context, page = sign_in(browser, live_server, "approver")
    with context:
        navigate(page, f'requests?open={record["id"]}')
        panel = page.locator(".request-panel")
        expect(panel.get_by_role("button", name=person_name(h, staff), exact=True)).to_be_visible()
        expect(panel.get_by_text(reason, exact=True)).to_be_visible()
        panel.get_by_role("button", name="Decline", exact=True).click()
        dialog = page.get_by_role("dialog", name="Decline leave", exact=True)
        dialog.get_by_label("Reason", exact=True).fill(decision)
        save_dialog(dialog, "Decline")

        reload_view(page, "requests")
        expect(panel.locator(".request-status")).to_contain_text("Declined")
        expect(panel.locator(".request-facts").get_by_text(decision, exact=True)).to_be_visible()
        stored = h.get("approver", f'/api/requests/all?id={record["id"]}')["item"]
        assert stored["stage"] == "declined"
        assert stored["decision_reason"] == decision


def test_find_unassigned_duty_while_staffed_duty_is_filtered_out(browser, live_server, h):
    rota = h.shift(h.workspace("Open duty draft"), people=[])["workspace"]
    vacant, = h.detail(rota)["shifts"]
    rota = h.shift(rota, start="2026-10-04T13:00:00+03:00", end="2026-10-04T17:00:00+03:00")["workspace"]
    staffed = next(duty for duty in h.detail(rota)["shifts"] if duty["participants"])

    context, page = sign_in(browser, live_server, "coordinator")
    with context:
        open_rota(page, rota["id"])
        pick_view(page, "List")
        expect(duty_row(page, vacant["id"])).to_be_visible()
        expect(duty_row(page, staffed["id"])).to_be_visible()
        filters = toolbar(page.locator(".planner-toolbar"), "Filter")
        filters.get_by_label("Only sessions with open places", exact=True).check()
        close_popover(page)
        expect(duty_row(page, vacant["id"])).to_be_visible()
        expect(duty_row(page, staffed["id"])).to_have_count(0)
        duty_row(page, vacant["id"]).locator("td strong").first.click()
        expect(page.locator(".wk-session").get_by_text("No people assigned", exact=True)).to_be_visible()


def test_read_person_clinic_and_rank_records_after_reload(browser, live_server, h):
    person_id = h.seed["people"][0]
    catalogues = h.get("coordinator", "/api/catalogues")
    rank = next(row["name"] for row in catalogues["categories"] if row["id"] == h.seed["category"])
    clinic = next(row["name"] for row in catalogues["services"] if row["id"] == h.seed["services"][0])

    context, page = sign_in(browser, live_server, "coordinator")
    with context:
        navigate(page, f"people?open={person_id}")
        reload_view(page, "people")
        profile = page.get_by_role("region", name=person_name(h, person_id), exact=True)
        expect(profile.get_by_role("tab", name="Overview", exact=True)).to_have_attribute("aria-selected", "true")
        rank_section = profile.locator("section").filter(has=page.get_by_role("heading", name="Rank", exact=True))
        clinics = profile.locator("section").filter(has=page.get_by_role("heading", name="Clinics", exact=True))
        expect(rank_section.get_by_text(rank, exact=True)).to_be_visible()
        expect(clinics.get_by_text(clinic, exact=True)).to_be_visible()

"""People (F9): one bulk engine, the person forms in the page's words with no dead ends, Undo on End, the panel's place
in the list on a reload, status and gaps in the shared palette, and the Workload tab without its rota scores."""
import re
import pytest
from playwright.sync_api import expect
from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .governance_support import identity
from .test_browser import browser, live_server, sign_in, navigate, close_popover, expect_loaded  # noqa: F401
from .test_browser_people_merged import row
from .test_browser_person_profile import open_person, person, section

pytestmark = pytest.mark.browser


def test_the_bulk_bar_is_change_and_two_shortcuts_into_it(browser, live_server, h):
    """P-07: the bar offers Change… and two shortcuts that open it with their change chosen (Give rank: the Give rank
    action); the page's own bulk dialogs are gone."""
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        boxes = page.locator("td .bulk-check")
        boxes.nth(0).check()
        boxes.nth(1).check()
        bar = page.get_by_role("toolbar", name="Bulk actions")
        for name in ("Change…", "Add tag", "Give rank"):
            expect(bar.get_by_role("button", name=name, exact=True)).to_be_visible()
        for gone in ("Set colour", "Remove tag", "Give a rank"):
            expect(bar.get_by_role("button", name=gone, exact=True)).to_have_count(0)
        bar.get_by_role("button", name="Give rank", exact=True).click()
        change = page.get_by_role("region", name="Change 2 people")
        expect(change.get_by_role("combobox", name="What to change")).to_contain_text("Give rank")
        expect(page.get_by_role("dialog")).to_have_count(0)  # no modal of its own
        # Tags once (P-07): the Tags field, not also a Change tags action.
        what = change.get_by_role("combobox", name="What to change")
        expect(what.locator("option", has_text=re.compile("^Tags$"))).to_have_count(1)
        expect(what.locator("option", has_text="Change tags")).to_have_count(0)
    finally:
        context.close()


def test_record_leave_for_several_people_is_the_one_shared_form(browser, live_server, h):
    """P-04 (owner decision): the bar keeps Record leave under More actions; it opens the one Record leave form
    (availabilityFields) with the selected people already chosen, not a second form of the bulk panel's own."""
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        boxes = page.locator("td .bulk-check")
        boxes.nth(0).check()
        boxes.nth(1).check()
        page.get_by_role("toolbar", name="Bulk actions").get_by_role("button", name="More actions").click()
        page.get_by_role("menuitem", name="Record leave", exact=True).click()
        form = page.get_by_role("dialog", name="Record leave for 2 people")
        expect(form.get_by_label("All day", exact=True)).to_be_visible()  # the shared form's own fields
        expect(form.locator(".pick-field, fieldset.multi-field").filter(has_text="People").locator(":checked, .pick-chip, .pick-button")).not_to_have_count(0)
        expect(page.get_by_role("region", name=re.compile("^Change "))).to_have_count(0)
    finally:
        context.close()


def test_relationship_forms_speak_the_page_and_say_what_is_missing(browser, live_server, h):
    """P-03 / P-24: Add rank in the page's words; with no ranks in the department, the form says so and where they are
    set up instead of an empty required picker."""
    with transaction(h.app.state.engine, write=True) as db:
        dept = m.Department(organisation_id=h.seed["organisation_id"], name="Forms department")
        db.add(dept)
        db.flush()
        dept_id = dept.id
    identity(h, "forms-coordinator", ["coordinator"], department=dept_id)
    context, page = sign_in(browser, live_server, "forms-coordinator")
    try:
        navigate(page, "people")
        row(page, "Staff 01(test)").get_by_role("button", name=re.compile("^Open ")).click()
        panel = page.locator(".dock-panel.person-panel")
        panel.get_by_role("button", name="Add rank", exact=True).click()
        form = page.get_by_role("dialog", name="Add rank")
        expect(form).to_contain_text("Membership labels do not establish clinical authority.")  # governance stays visible
        expect(form.locator(".missing-setup")).to_contain_text("No ranks yet")
        expect(form.get_by_label("From", exact=True)).to_be_visible()
    finally:
        context.close()


def test_a_retired_rank_still_records_its_history(browser, live_server, h):
    """P-24 regression: a department whose only rank is retired still offers it, and a historical rank saves; "nothing
    set up" means no choices at all, not no active ones (the API keeps its own relationship checks)."""
    from datetime import date
    with transaction(h.app.state.engine, write=True) as db:
        dept = m.Department(organisation_id=h.seed["organisation_id"], name="Retired ranks department")
        db.add(dept)
        db.flush()
        rank = m.Category(department_id=dept.id, name="Retired grade", starts_on=date(2020, 1, 1), ends_on=date(2025, 1, 1), active=False)
        db.add(rank)
        db.flush()
        dept_id, retired_id = dept.id, rank.id
    identity(h, "retired-coordinator", ["coordinator"], department=dept_id)
    context, page = sign_in(browser, live_server, "retired-coordinator")
    try:
        navigate(page, "people")
        row(page, "Staff 01(test)").get_by_role("button", name=re.compile("^Open ")).click()
        page.locator(".dock-panel.person-panel").get_by_role("button", name="Add rank", exact=True).click()
        form = page.get_by_role("dialog", name="Add rank")
        expect(form.locator(".missing-setup")).to_have_count(0)
        form.get_by_label("Rank", exact=True).select_option(label="Retired grade · inactive")
        form.get_by_label("From", exact=True).fill("2021-03-01")
        form.get_by_label("Last day (optional)", exact=True).fill("2021-12-31")
        form.get_by_role("button", name="Add rank", exact=True).click()
        toast = page.locator("#toast-tray .toast").filter(has_text="Rank added.")
        expect(form).to_have_count(0)
        # Undo (the one registry, api.ts) takes the record back as if never added.
        toast.get_by_role("button", name="Undo").click()
        expect(page.locator("#toast-tray")).to_contain_text("Undone")
        assert not [r for r in h.get("retired-coordinator", "/api/catalogues")["memberships"] if r["category_id"] == retired_id], "the record stays"
    finally:
        context.close()


def test_the_tags_picker_says_the_servers_limits_under_the_field(browser, live_server, h):
    """Review maintenance (6): the creatable Tags picker keeps the server's limits (TAG_LIMITS): a tag that is too long is
    said under the field on Save, and nothing is sent."""
    context, page = sign_in(browser, live_server, "coordinator")
    sent = []
    # A save (/api/commands), not the form's impact preview (a dry run, f11).
    page.on("request", lambda r: r.method == "POST" and "/api/commands" in r.url and "catalogue.update" in (r.post_data or "") and sent.append(r))
    try:
        navigate(page, "people")
        row(page, "Staff 01(test)").get_by_role("button", name=re.compile("^Open ")).click()
        page.get_by_role("button", name=re.compile("^Edit Staff 01\\(test\\)")).click()
        form = page.get_by_role("dialog")
        form.get_by_role("button", name=re.compile("^Tags: ")).click()
        long = "A" * 25
        page.get_by_label("Search Tags").fill(long)
        page.get_by_role("button", name=f"Add “{long}”").click()
        close_popover(page)
        form.get_by_role("button", name="Save version", exact=True).click()
        expect(form.locator(".pick-field").filter(has_text="Tags").locator(".field-error")).to_have_text("Each tag up to 24 characters.")
        assert not sent
    finally:
        context.close()


def test_person_edit_is_live_after_reload_and_offers_undo(browser, live_server, h):
    """Ordinary person facts apply immediately, survive reload and offer Undo."""
    p = person(h)

    def directory_extension():
        rows = h.get("coordinator", "/api/directory")["people"]
        entry = next(row for row in rows if row["id"] == p["id"])
        return next((contact["value"] for contact in entry.get("contacts", ())
                     if contact["legacy_field"] == "work_extension"), None)

    context, page = sign_in(browser, live_server, "coordinator")
    try:
        panel = open_person(page, p)
        panel.get_by_role("button", name=f"Edit {p['display_name']}", exact=True).click()
        form = page.get_by_role("dialog", name="Edit person", exact=True)
        form.get_by_label("Office extension", exact=True).fill("4321")
        form.get_by_label("Change reason", exact=True).fill("Add the office extension")
        form.get_by_role("button", name="Save version", exact=True).click()
        toast = page.locator("#toast-tray .toast").filter(has_text="saved and is live")
        expect(toast).to_be_visible()
        expect(toast.get_by_role("button", name="Undo", exact=True)).to_be_visible()
        expect(form).to_have_count(0)

        page.reload()
        expect_loaded(page, "people")
        panel = open_person(page, p)
        expect(panel).to_contain_text("4321")
        assert directory_extension() == "4321"
        panel.get_by_role("button", name=f"Edit {p['display_name']}", exact=True).click()
        extension = form.get_by_label("Office extension", exact=True)
        expect(extension).to_have_value("4321")
        extension.fill("")
        form.get_by_role("button", name="Save version", exact=True).click()
        expect(form).to_have_count(0)
        assert not person(h).get("work_extension")
        assert not directory_extension()

        toast.get_by_role("button", name="Undo", exact=True).click()
        expect(page.locator("#toast-tray")).to_contain_text("Undone")
        assert directory_extension() == "4321"
        page.reload()
        expect_loaded(page, "people")
        expect(open_person(page, p)).to_contain_text("4321")
    finally:
        context.close()


def test_editing_job_title_preserves_contacts_omitted_from_people_list(browser, live_server, h):
    p = person(h, 1)
    numbers = {"work_phone": "555018811", "work_extension": "4321", "pager": "551122"}
    h.configure("people", {"display_name": p["display_name"], **numbers}, p["id"], p["revision"])
    for typ in h.get("admin", "/api/contact-types")["contact_types"]:
        if typ["legacy_field"] in numbers:
            value = {**{key: typ[key] for key in s.ContactTypeInput.model_fields}, "visibility": {"mode": "self_and_admins"}}
            h.configure("contact_types", value, typ["id"], typ["revision"])
    assert not numbers.keys() & person(h, 1).keys()

    context, page = sign_in(browser, live_server, "coordinator")
    try:
        panel = open_person(page, p)
        panel.get_by_role("button", name=f"Edit {p['display_name']}", exact=True).click()
        form = page.get_by_role("dialog", name="Edit person", exact=True)
        for label in ("Work phone", "Office extension", "Pager number"):
            expect(form.get_by_label(label, exact=True)).to_have_value("")
        form.get_by_label("Job title", exact=True).fill("Updated title")
        form.get_by_role("button", name="Save version", exact=True).click()
        expect(form).to_have_count(0)

        saved = next(row for row in h.get("admin", "/api/catalogues?include=people")["people"] if row["id"] == p["id"])
        assert saved["title"] == "Updated title"
        assert {key: saved.get(key) for key in numbers} == numbers
    finally:
        context.close()


def test_ending_a_rank_says_so_with_undo(browser, live_server, h):
    """P-10: End rank saves with a message and Undo, which puts the earlier dates back."""
    p = person(h)
    rank = next(c for c in h.get("coordinator", "/api/catalogues")["categories"] if c.get("active") is not False)
    h.command("coordinator", "membership.create", {"person_id": p["id"], "category_id": rank["id"], "starts_on": "2026-01-01", "ends_on": None})
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        panel = open_person(page, p)
        ranks = section(panel, "Rank")
        ranks.get_by_role("button", name=f"End {rank['name']}").first.click()
        form = page.get_by_role("dialog", name=f"End {rank['name']}")
        form.get_by_label("Reason").fill("Moves to the next year")
        form.get_by_role("button", name="End", exact=True).click()
        toast = page.locator("#toast-tray .toast").filter(has_text=f"{rank['name']} ends")
        expect(toast).to_be_visible()
        toast.get_by_role("button", name="Undo").click()
        expect(page.locator("#toast-tray")).to_contain_text("The earlier dates are back.")
        rows = [r for r in h.get("coordinator", "/api/catalogues")["memberships"] if r["person_id"] == p["id"] and r["category_id"] == rank["id"]]
        assert any(r["ends_on"] is None for r in rows), rows
    finally:
        context.close()


def test_a_panel_opened_by_link_knows_its_place_and_the_list_uses_the_shared_palette(browser, live_server, h):
    """P-09 / P-25: a person opened from the address shows "N of M" with ↑ ↓; a missing rank is a warning badge like
    missing eligibility."""
    p = person(h, 1)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        page.evaluate(f"location.hash = 'people?open={p['id']}'")
        expect_loaded(page, "people")
        panel = page.locator(".dock-panel.person-panel")
        expect(panel).to_contain_text(re.compile(r"Person \d+ of \d+"))
        close_popover(page)
        lone = h.configure("people", {"display_name": "Fictional rankless colleague"})["record"]
        page.reload()
        expect_loaded(page, "people")
        cell = row(page, lone["display_name"]).locator("td[data-col=rank]")
        expect(cell.locator(".badge")).to_have_text("No current rank")
    finally:
        context.close()


def test_workload_offers_calendar_periods_when_rota_scores_fail(browser, live_server, h):
    """P-15: the score read failing hides nothing else: the calendar periods still load, and one line says the rota
    scores are missing, with Retry inside it."""
    p = person(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        panel = open_person(page, p)
        page.route("**/score*", lambda route: route.fulfill(status=500, content_type="application/json",
                                                            body='{"error":{"code":"INTERNAL_ERROR","message":"Synthetic score outage"}}'))
        panel.get_by_role("tab", name=re.compile("^Workload")).click()
        note = panel.locator(".notice").filter(has_text="Couldn’t load the rota scores. ")
        expect(note.get_by_role("button", name="Retry", exact=True)).to_be_visible()
        expect(panel.get_by_label("Period", exact=True)).to_contain_text("This month")
    finally:
        context.close()

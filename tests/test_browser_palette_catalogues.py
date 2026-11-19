"""Ctrl K opens exact, authorized loaded catalogue sources through their existing owners."""
import re
import uuid

import pytest
from playwright.sync_api import expect

from packages.contracts.schemas import PERMISSIONS, ROLE_NAMES
from .governance_support import identity
from .test_browser import browser, live_server, navigate, save_dialog, sign_in  # noqa: F401
from .test_browser_palette_sources import search
from .test_browser_rule_catalogue import catalogue_rule, flag, line
from .test_rule_catalogue_api import approve, propose

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("role", ROLE_NAMES)
def test_record_results_obey_page_and_edit_roles(browser, live_server, h, role):
    flag(h)
    clinic = h.configure("services", {"name": "Palette clinic authority", "starts_on": "2026-01-01"})["record"]
    tag = h.configure("tags", {"name": "Palette tag authority", "labels": ["services"]})["record"]
    rule_line = line({"max": {"value": 5, "level": "mandatory"}}, note="Palette rule authority")
    catalogue_rule(h, "duties_per_week", [rule_line])
    user = "palette_" + role
    identity(h, user, [role], person=h.seed["people"][0] if role == "staff" else None)
    assert role in h.get(user, "/api/me")["roles"]  # admin also carries the roles it inherits
    # A programme lead manages the programmes they lead or created (training.lead_programmes).
    programme = h.configure("programmes", {"name": "Palette programme authority", "starts_on": "2026-01-01"},
                            role=user if role == "programme_lead" else "admin")["record"]
    context, page = sign_in(browser, live_server, user)
    writes = []
    page.on("request", lambda request: writes.append(request.post_data_json)
            if request.method == "POST" and request.url.endswith("/api/commands") else None)
    try:
        cases = [(clinic["name"], role == "admin", "configuration/services?open=" + clinic["id"], "Edit service"),
                 (tag["name"], role in {"admin", "coordinator"}, "configuration/tags?open=" + tag["id"], "#" + tag["name"]),
                 (programme["name"], role in {"admin", "coordinator", "programme_lead"},
                  "programmes/programmes?open=" + programme["id"], "Edit programme"),
                 (rule_line["note"], role in PERMISSIONS['rules.read'],
                  "rules/duties_per_week/" + rule_line["id"], None)]
        for query, permitted, route, title in cases:
            menu = search(page, query)
            expect(menu.get_by_role("listbox")).not_to_have_attribute("aria-busy", "true")
            choice = menu.get_by_role("option")
            expect(choice).to_have_count(1 if permitted else 0)
            if not permitted:
                menu.get_by_role("combobox").press("Escape")
                continue
            choice.click()
            expect(page).to_have_url(re.compile("#" + re.escape(route) + "$"))
            is_tag = route.startswith("configuration/tags?")
            panel = (page.get_by_role("region" if is_tag else "dialog", name=title, exact=True)
                     if title else page.locator(".rc-line-panel"))
            expect(panel).to_be_visible()
            if is_tag:
                expect(panel.get_by_label("Name", exact=True)).to_have_value(tag["name"])
                expect(panel.get_by_role("button", name="Save tag", exact=True)).to_be_visible()
            if not title and role not in {"admin", "coordinator"}:
                expect(panel.get_by_role("button", name="Keep change", exact=True)).to_have_count(0)
                expect(panel.get_by_label("Note", exact=True)).to_have_attribute("readonly", "")
            page.reload()
            expect(panel).to_be_visible()
            panel.get_by_role("button", name="Cancel" if title and not is_tag else "Close panel", exact=True).click()
            expect(panel).to_have_count(0)
        # A manually pasted source ID does not grant the configuration page or its editor.
        if role != "admin":
            page.goto(live_server + "/#configuration/services?open=" + clinic["id"])
            expect(page.locator("#main")).to_contain_text("This record is not available" if role in {"coordinator", "operator"} else "Restricted page")
            expect(page.get_by_role("dialog", name="Edit service", exact=True)).to_have_count(0)
        if role not in {"admin", "coordinator"}:
            page.goto(live_server + "/#configuration/tags?open=" + tag["id"])
            expect(page.locator("#main")).to_contain_text("This record is not available" if role == "operator" else "Restricted page")
            expect(page.get_by_role("region", name="#" + tag["name"], exact=True)).to_have_count(0)
        assert writes == []
    finally:
        context.close()


def test_all_named_configuration_sources_use_their_canonical_form_and_exact_id(browser, live_server, h):
    location_type = h.configure("location_types", {"name": "Palette location type source", "starts_on": "2026-01-01"})["record"]
    location = h.configure("locations", {"name": "Palette location source", "starts_on": "2026-01-01",
                                        "location_type_id": location_type["id"]})["record"]
    duty_type = h.configure("assignment_types", {"name": "Palette duty source", "starts_on": "2026-01-01",
                                                "definition": {}})["record"]
    records = [("location_types", location_type, "location type"), ("locations", location, "location"),
               ("assignment_types", duty_type, "duty type")]
    for kind, title, extra in (
        ("recurring_series", "recurring session", {"assignment_type_id": duty_type["id"], "location_id": location["id"],
            "ends_on": "2027-01-01", "definition": {"anchor": "2026-01-04", "service_ids": [h.seed["services"][0]],
            "days": [{"weekday": 0, "start_time": "08:00", "end_time": "12:00"}]}}),
        ("operating_calendars", "opening hours", {"definition": {"timezone": "Asia/Riyadh"}}),
        ("resource_pools", "resource pool", {"capacity": 2}),
        ("categories", "rank", {}),
        ("capabilities", "skill", {}),
        ("department_content", "announcement", {"description": "Exact announcement source", "notify": False}),
    ):
        record = h.configure(kind, {"name": "Palette " + title + " source", "starts_on": "2026-01-01", **extra})["record"]
        records.append((kind, record, title))
    context, page = sign_in(browser, live_server, "admin")
    writes = []
    page.on("request", lambda request: writes.append(request.post_data_json)
            if request.method == "POST" and request.url.endswith("/api/commands") else None)
    try:
        # Observe Home's real read receipt before leaving for configuration.
        page.reload()
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "home")
        announcement = next(record for kind, record, _ in records if kind == "department_content")
        expect(page.locator(".home-notice-list").get_by_text(announcement["name"], exact=True)).to_be_visible()
        # Sections are addressed by their name (#configuration/opening-hours); old list names still open the record.
        address = {"operating_calendars": "opening-hours", "department_content": "announcements"}
        for kind, record, title in records:
            search(page, record["name"].replace(" ", "")).get_by_role("option").click()
            expect(page).to_have_url(re.compile(r"#configuration/" + address.get(kind, kind) + r"\?open=" + record["id"] + "$"))
            dialog = page.get_by_role("dialog", name="Edit " + title, exact=True)
            name = dialog.get_by_label("Title" if kind == "department_content" else "Name", exact=True)
            expect(name).to_have_value(record["name"])
            page.reload()
            expect(name).to_have_value(record["name"])
            dialog.get_by_role("button", name="Cancel", exact=True).click()
            expect(dialog).to_have_count(0)
            expect(page).to_have_url(re.compile(r"#configuration/" + address.get(kind, kind) + "$"))
        kind, record, title = next(r for r in records if r[0] == "operating_calendars")
        page.goto(live_server + "/#configuration/operating_calendars?open=" + record["id"])
        expect(page).to_have_url(re.compile(r"#configuration/opening-hours\?open=" + record["id"] + "$"))
        expect(page.get_by_role("dialog", name="Edit " + title, exact=True).get_by_label("Name", exact=True)).to_have_value(record["name"])
        seen_receipt = {"kind": "department_content.seen", "payload": {"ids": [announcement["id"]]}}
        assert writes and all(write == seen_receipt for write in writes), writes
    finally:
        context.close()


@pytest.mark.parametrize("role,width,scheme", [("admin", 1440, "light"), ("coordinator", 390, "dark")])
def test_tag_exact_source_owns_reload_dirty_close_save_and_stale_links(browser, live_server, h, role, width, scheme):
    # Tag names are unique within a department; a clinic can still have the same label.
    name = "Palette shared source"
    tag = h.configure("tags", {"name": name, "description": "Original tag source", "labels": ["services"]})["record"]
    clinic = h.configure("services", {"name": name, "description": "Separate clinic source", "starts_on": "2026-01-01"})["record"]
    context, page = sign_in(browser, live_server, role, viewport={"width": width, "height": 900})
    writes = []
    page.on("request", lambda request: writes.append(request.post_data_json)
            if request.method == "POST" and request.url.endswith("/api/commands") else None)
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, "configuration/tags")
        page.get_by_role("searchbox", name="Search tags", exact=True).fill("No local tag result")
        menu = search(page, name.replace(" ", ""))
        expect(menu.get_by_role("listbox")).not_to_have_attribute("aria-busy", "true")
        expect(menu.get_by_role("option")).to_have_count(2 if role == "admin" else 1)
        choice = menu.get_by_role("option").filter(has=page.get_by_text("#" + name, exact=True))
        expect(choice).to_contain_text("Tags")
        choice.click()
        route = "#configuration/tags?open=" + tag["id"]
        panel = page.get_by_role("region", name="#" + name, exact=True)
        expect(page).to_have_url(re.compile(re.escape(route) + "$"))
        expect(panel.get_by_label("Description", exact=True)).to_have_value("Original tag source")
        page.reload()
        expect(panel).to_have_count(1)
        expect(panel.get_by_label("Name", exact=True)).to_have_value(name)
        panel.get_by_label("Description", exact=True).fill("Unsaved exact tag")
        page.evaluate("location.hash='home'")
        expect(panel.get_by_role("button", name="Keep editing", exact=True)).to_be_visible()
        expect(page).to_have_url(re.compile(re.escape(route) + "$"))
        expect(panel.get_by_label("Description", exact=True)).to_have_value("Unsaved exact tag")
        panel.get_by_role("button", name="Keep editing", exact=True).click()
        search(page, "Home").get_by_role("option", name=re.compile("^Home")).click()
        expect(panel.get_by_role("button", name="Keep editing", exact=True)).to_be_visible()
        expect(page).to_have_url(re.compile(re.escape(route) + "$"))
        expect(panel.get_by_label("Description", exact=True)).to_have_value("Unsaved exact tag")
        panel.get_by_role("button", name="Discard changes", exact=True).click()
        expect(panel).to_have_count(0)
        # Discard releases the guard and resumes the requested Home navigation.
        expect(page).to_have_url(re.compile(r"#home$"))
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home')
        assert writes == []
        search(page, name).get_by_role("option").filter(has=page.get_by_text("#" + name, exact=True)).click()
        expect(panel.get_by_label("Description", exact=True)).to_have_value("Original tag source")
        panel.get_by_label("Description", exact=True).fill("Saved exact tag source")
        panel.get_by_role("button", name="Close panel", exact=True).click()
        panel.get_by_role("button", name="Keep editing", exact=True).click()
        save_dialog(panel, "Save tag")
        expect(panel).to_have_count(0)
        expect(page).to_have_url(re.compile(r"#configuration/tags$"))
        assert len(writes) == 1
        assert writes[0]["target_id"] == tag["id"] and writes[0]["expected_revision"] == tag["revision"]
        assert writes[0]["payload"]["catalogue"] == "tags"
        current = h.get("admin", "/api/catalogues")
        assert next(row for row in current["tags"] if row["id"] == tag["id"])["description"] == "Saved exact tag source"
        assert next(row for row in current["services"] if row["id"] == clinic["id"])["description"] == "Separate clinic source"
        page.reload()
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "configuration")
        expect(panel).to_have_count(0)
        search(page, name).get_by_role("option").filter(has=page.get_by_text("#" + name, exact=True)).click()
        expect(panel.get_by_label("Description", exact=True)).to_have_value("Saved exact tag source")
        panel.get_by_role("button", name="Close panel", exact=True).click()
        expect(page).to_have_url(re.compile(r"#configuration/tags$"))
        page.goto(live_server + "/#configuration/tags?open=" + str(uuid.uuid4()))
        expect(page.get_by_text("This record is not available. Choose one from the list.", exact=True)).to_be_visible()
        expect(page.locator(".dock-panel")).to_have_count(0)
        page.reload()
        expect(page.get_by_text("This record is not available. Choose one from the list.", exact=True)).to_be_visible()
        expect(page.locator(".dock-panel")).to_have_count(0)
        assert len(writes) == 1
    finally:
        context.close()


@pytest.mark.parametrize("width,scheme", [(1440, "light"), (390, "dark")])
def test_clinic_exact_source_survives_filters_reload_save_and_hash_navigation(browser, live_server, h, width, scheme):
    first = h.configure("services", {"name": "Physical Urgent Care", "description": "Palette north clinic",
                                     "starts_on": "2026-01-01", "mode": "physical"})["record"]
    chosen = h.configure("services", {"name": "Physical Urgent Care", "description": "Palette south clinic",
                                      "starts_on": "2026-01-01", "mode": "physical"})["record"]
    context, page = sign_in(browser, live_server, "admin", viewport={"width": width, "height": 900})
    writes = []
    page.on("request", lambda request: writes.append(request.post_data_json)
            if request.method == "POST" and request.url.endswith("/api/commands") else None)
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, "configuration/services")
        page.get_by_role("searchbox", name="Search services", exact=True).fill("No such local table result")
        menu = search(page, "physicalurgentcare")
        candidates = menu.get_by_role("option").filter(has_text="Palette")
        expect(candidates).to_have_count(2)
        candidates.filter(has_text="Palette south clinic").click()
        route = "#configuration/services?open=" + chosen["id"]
        expect(page).to_have_url(re.compile(re.escape(route) + "$"))
        dialog = page.get_by_role("dialog", name="Edit service", exact=True)
        expect(dialog.get_by_label("Description", exact=True)).to_have_value("Palette south clinic")
        page.reload()
        expect(dialog).to_have_count(1)
        expect(dialog.get_by_label("Description", exact=True)).to_have_value("Palette south clinic")
        assert writes == []
        dialog.get_by_label("Description", exact=True).fill("Saved exact south source")
        # An ordinary hash change must use this form's existing guard and retain its exact source/edits.
        page.evaluate("location.hash='home'")
        expect(dialog.get_by_role("button", name="Keep editing", exact=True)).to_be_visible()
        expect(page).to_have_url(re.compile(re.escape(route) + "$"))
        expect(dialog.get_by_label("Description", exact=True)).to_have_value("Saved exact south source")
        dialog.get_by_role("button", name="Keep editing", exact=True).click()
        save_dialog(dialog, "Save version")
        expect(dialog).to_have_count(0)
        expect(page).to_have_url(re.compile(r"#configuration/services$"))
        # Save refreshes before the shared form closes: it must neither reopen nor save another duplicate.
        assert len(writes) == 1
        assert writes[0]["target_id"] == chosen["id"] and writes[0]["expected_revision"] == chosen["revision"]
        current = {row["id"]: row for row in h.get("admin", "/api/catalogues")["services"]}
        assert current[chosen["id"]]["description"] == "Saved exact south source"
        assert current[first["id"]]["description"] == "Palette north clinic"
        page.reload()
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "configuration")
        expect(dialog).to_have_count(0)
        search(page, "physicalurgentcare").get_by_role("option").filter(has_text="Saved exact south source").click()
        expect(dialog).to_be_visible()
        # Clean direct navigation must preserve the newly requested hash, including Back/hashchange ordering.
        page.evaluate("location.hash='home'")
        expect(page).to_have_url(re.compile(r"#home$"))
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "home")
        expect(dialog).to_have_count(0)
        for suffix in ("services?open=" + str(uuid.uuid4()), "not_a_kind?open=" + chosen["id"],
                       "services/extra?open=" + chosen["id"], "services?open="):
            page.goto(live_server + "/#configuration/" + suffix)
            expect(page.get_by_text("This record is not available. Choose one from the list.", exact=True)).to_be_visible()
            expect(page.get_by_role("dialog")).to_have_count(0)
        assert len(writes) == 1
    finally:
        context.close()


@pytest.mark.parametrize("width,scheme", [(1440, "dark"), (390, "light")])
def test_palette_respects_existing_dirty_form_and_unknown_dialog_owners(browser, live_server, h, width, scheme):
    source = h.configure("services", {"name": "Palette guarded source", "starts_on": "2026-01-01"})["record"]
    context, page = sign_in(browser, live_server, "admin", viewport={"width": width, "height": 900})
    writes = []
    page.on("request", lambda request: writes.append(request.post_data_json)
            if request.method == "POST" and request.url.endswith("/api/commands") else None)
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, "configuration/services")
        # This is the ordinary table action, not a form created by the new ?open route.
        page.get_by_role("button", name="New service", exact=True).click()
        draft = page.get_by_role("dialog", name="New service", exact=True)
        draft.get_by_label("Name", exact=True).fill("Keep this unsaved clinic")
        old_url = page.url
        search(page, source["name"]).get_by_role("option").click()
        expect(page).to_have_url(old_url)
        expect(draft).to_be_visible()
        expect(draft.get_by_label("Name", exact=True)).to_have_value("Keep this unsaved clinic")
        expect(draft.get_by_role("button", name="Keep editing", exact=True)).to_be_visible()
        expect(page.get_by_role("dialog", name="Edit service", exact=True)).to_have_count(0)
        assert writes == []
        draft.get_by_role("button", name="Discard changes", exact=True).click()
        expect(draft).to_have_count(0)
        # Discard does not automatically resume the cancelled palette action.
        expect(page).to_have_url(old_url)
        expect(page.get_by_role("dialog", name="Edit service", exact=True)).to_have_count(0)
        search(page, source["name"]).get_by_role("option").click()
        editor = page.get_by_role("dialog", name="Edit service", exact=True)
        expect(editor.get_by_label("Name", exact=True)).to_have_value(source["name"])
        # Re-selecting the same source cannot let the old dialog's delayed close erase the new owner's address.
        search(page, source["name"]).get_by_role("option").click()
        expect(editor).to_have_count(1)
        expect(page).to_have_url(re.compile(r"#configuration/services\?open=" + source["id"] + "$"))
        page.reload()
        expect(editor).to_be_visible()
        editor.get_by_role("button", name="Cancel", exact=True).click()
        expect(page).to_have_url(re.compile(r"#configuration/services$"))
        page.evaluate("""() => {const modal=document.createElement('dialog');modal.id='unknown-owner';
            modal.textContent='Another operation is pending';document.body.append(modal);modal.showModal();}""")
        search(page, "Home").get_by_role("option", name=re.compile("^Home")).click()
        expect(page.locator("#unknown-owner")).to_be_visible()
        expect(page).to_have_url(re.compile(r"#configuration/services$"))
        assert writes == []
    finally:
        context.close()


def test_named_programme_sources_use_existing_form_and_exact_id(browser, live_server, h):
    identity(h, "programme_lead", ["programme_lead"])
    # The lead creates the programme, so it leads it (training.lead_programmes).
    programme = h.configure("programmes", {"name": "Palette programme source", "starts_on": "2026-01-01"},
                            role="programme_lead")["record"]
    cohort = h.configure("cohorts", {"name": "Palette cohort source", "programme_id": programme["id"],
                                    "starts_on": "2026-01-01", "ends_on": "2027-01-01"})["record"]
    rotation = h.configure("rotation_types", {"name": "Palette rotation source", "starts_on": "2026-01-01"})["record"]
    calendar = h.configure("programme_calendar_entries", {"name": "Palette academic source", "programme_id": programme["id"],
                          "kind": "academic_block", "starts_on": "2026-10-04", "ends_on": "2026-10-06"})["record"]
    context, page = sign_in(browser, live_server, "programme_lead")
    try:
        for kind, record, title in (("programmes", programme, "programme"), ("cohorts", cohort, "cohort"),
                                    ("rotation_types", rotation, "rotation type"),
                                    ("programme_calendar_entries", calendar, "academic calendar entry")):
            search(page, record["name"].replace(" ", "")).get_by_role("option").click()
            expect(page).to_have_url(re.compile(r"#programmes/" + kind + r"\?open=" + record["id"] + "$"))
            dialog = page.get_by_role("dialog", name="Edit " + title, exact=True)
            expect(dialog.get_by_label("Name", exact=True)).to_have_value(record["name"])
            page.reload()
            expect(dialog.get_by_label("Name", exact=True)).to_have_value(record["name"])
            dialog.get_by_role("button", name="Cancel", exact=True).click()
            expect(page).to_have_url(re.compile(r"#programmes/" + kind + "$"))
    finally:
        context.close()


@pytest.mark.parametrize("width,scheme", [(1440, "light"), (390, "dark")])
def test_rule_results_are_current_exact_sources_and_unavailable_lines_do_not_fall_back(browser, live_server, h, width, scheme):
    flag(h)
    retired_line = line({"max": {"value": 5}}, note="Retired palette source marker")
    old = catalogue_rule(h, "duties_per_week", [retired_line])
    current_lines = [line({"max": {"value": 3}}, note="Current palette line " + str(i),
                          who={"conditions": [{"field": "person_ids", "values": [person]}]})
                     for i, person in enumerate(h.seed["people"][:2])]
    current = approve(h, propose(h, "duties_per_week", current_lines, policy_id=old["id"], revision=old["revision"])["record"])
    pending = line({"max": {"value": 4}}, note="Pending palette source marker")
    propose(h, "duties_per_week", [pending], policy_id=current["id"], revision=current["revision"])
    context, page = sign_in(browser, live_server, "auditor", viewport={"width": width, "height": 900})
    try:
        page.emulate_media(color_scheme=scheme)
        for query in (retired_line["note"], pending["note"]):
            menu = search(page, query)
            expect(menu.get_by_role("listbox")).not_to_have_attribute("aria-busy", "true")
            expect(menu.get_by_role("option")).to_have_count(0)
            menu.get_by_role("combobox").press("Escape")
        menu = search(page, current["name"])
        menu.get_by_role("option").filter(has=page.get_by_text("Current rule", exact=True)).click()
        expect(page).to_have_url(re.compile(r"#rules/duties_per_week$"))
        source = current_lines[1]
        search(page, source["note"]).get_by_role("option").click()
        expect(page).to_have_url(re.compile(r"#rules/duties_per_week/" + source["id"] + "$"))
        panel = page.locator(".rc-line-panel")
        expect(panel.get_by_label("Note", exact=True)).to_have_value(source["note"])
        expect(panel.get_by_role("button", name="Keep change", exact=True)).to_have_count(0)
        page.reload()
        expect(panel.get_by_label("Note", exact=True)).to_have_value(source["note"])
        panel.get_by_role("button", name="Close panel", exact=True).click()
        page.evaluate("id=>location.hash='rules/duties_per_week/'+id", retired_line["id"])
        expect(page.get_by_text("This line is not available in the current rule. Choose one from the list.", exact=True)).to_be_visible()
        expect(panel).to_have_count(0)
        page.reload()
        expect(page.get_by_text("This line is not available in the current rule. Choose one from the list.", exact=True)).to_be_visible()
        expect(panel).to_have_count(0)
        page.evaluate("location.hash='rules/not_a_catalogue_entry'")
        expect(page.get_by_text("This rule is not available. Choose one from Rules.", exact=True)).to_be_visible()
        expect(page.locator(".rt-root")).to_have_count(0)
        flag(h, False)
        page.goto(live_server + "/#home")
        page.reload()
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "home")
        menu = search(page, source["note"])
        expect(menu.get_by_role("listbox")).not_to_have_attribute("aria-busy", "true")
        expect(menu.get_by_role("option")).to_have_count(0)
    finally:
        context.close()
# Include this file in hosted supervision and team-lead qualification.

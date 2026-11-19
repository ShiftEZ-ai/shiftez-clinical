"""Programmes › Rotation board (a Timeline view): a programme lead sees trainees on a timeline, filters to external rotators, groups by
programme, decides a trainee's rotation change, plans rotations, keeps a note and reads (not edits) rotation types; a trainee sees only their own board and
asks for a change, also to a rotation type nobody is on yet. Fixtures are fictional and made through the API; the actions under test use the real UI."""
import os
import re

import pytest
from playwright.sync_api import expect

from .test_browser import add_filter, browser, condition, expect_loaded, live_server, navigate, sign_in, whole_list  # noqa: F401
from .test_browser_operator_role_coverage import all_roles  # noqa: F401
from .test_residency import REASON, lead_of

pytestmark = pytest.mark.browser
SHOTS = os.getenv("ROTATION_BOARD_SHOTS")


def shot(page, name):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=True)


def residency(h):
    p = h.configure("programmes", {"name": "Fictional residency", "starts_on": "2026-01-01"})["record"]
    lead_of(h, p)
    cohort = h.configure("cohorts", {"name": "Fictional year 1", "programme_id": p["id"], "starts_on": "2026-07-01", "ends_on": "2027-07-01"})["record"]
    types = [h.configure("rotation_types", {"name": name, "starts_on": "2026-01-01"})["record"] for name in ("Emergency", "Paediatrics", "Clinic")]
    trainee, local, visitor = h.seed["people"][:3]
    for pid, extra in ((trainee, {}), (local, {}), (visitor, {"external": True, "home_programme": "Fictional visiting programme"})):
        h.configure("programme_memberships", {"person_id": pid, "programme_id": p["id"], "starts_on": "2026-07-01", "ends_on": "2027-07-01", **extra},
                    role="programme_lead")
    blocks = [{"rotation_type_id": t["id"], "length": 1, "unit": "months"} for t in types]
    planned = h.command("programme_lead", "rotation.plan", {"cohort_id": cohort["id"], "person_ids": [trainee, local, visitor], "starts_on": "2026-10-01",
                                                            "blocks": blocks, "stagger": True, "reason": REASON})["created"]
    return p, cohort, types, (trainee, local, visitor), planned


@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 900}, {"width": 390, "height": 844}], ids=["desktop", "phone"])
def test_programme_lead_reads_filters_groups_and_decides(browser, live_server, all_roles, viewport):
    h = all_roles
    p, cohort, types, (trainee, local, visitor), planned = residency(h)
    first = next(r for r in planned if r["person_id"] == trainee and r["starts_on"] == "2026-10-01")
    h.command("staff", "rotation.request_change", {"rotation_id": first["id"], "rotation_type_id": types[2]["id"], "starts_on": "2026-10-01",
                                                   "ends_on": "2026-11-01", "private_reason": "Fictional elective swap"})
    context, page = sign_in(browser, live_server, "programme_lead", viewport)
    try:
        navigate(page, "programmes/board")
        if viewport["width"] < 720:  # a phone shows the agenda; the timeline is one tap away
            page.get_by_role("button", name="Show the timeline", exact=True).click()
        board = page.get_by_role("region", name="Rotation timeline")
        expect(board.locator('button.tl-bar[data-bar]:not([data-bar*=":"])')).to_have_count(9)
        expect(board.get_by_role("button", name="Emergency, 1 Oct – 31 Oct 2026, change requested")).to_be_visible()
        shot(page, f"lead-board-{viewport['width']}")
        page.get_by_role("button", name="Filter", exact=True).click()
        condition(page.locator(".dt-pop"), "Trainees", ["External rotators"])  # the shared Filter
        page.keyboard.press("Escape")
        expect(board.locator(".tl-row")).to_have_count(1)
        expect(board.locator(".wk-tag.ext")).to_have_count(1)
        page.get_by_role("button", name="Filter: External rotators (1 active)").click()
        page.get_by_role("button", name="Clear all filters").click()
        page.keyboard.press("Escape")
        expect(board.locator(".tl-row")).to_have_count(3)
        page.get_by_role("button", name=re.compile("^Group")).click()
        page.get_by_role("menuitemradio", name="Rotation now").click()
        expect(board.get_by_role("group")).to_have_count(3)
        board.get_by_role("button", name="Emergency, 1 Oct – 31 Oct 2026, change requested").click()
        panel = page.locator(".dock-panel")
        expect(panel.get_by_role("heading", name="Change waiting")).to_be_visible()
        panel.get_by_role("button", name="Approve", exact=True).click()
        dialog = page.get_by_role("dialog", name="Approve rotation change")
        dialog.get_by_role("button", name="Approve").click()
        # The decided rotation (exact name): the waiting change's own bar ("Change waiting: Clinic, 1 Oct – 31 Oct 2026")
        # also contains these words and shows before the approval is saved.
        expect(board.locator(f'[data-row="{trainee}"]').get_by_role("button", name="Clinic, 1 Oct – 31 Oct 2026", exact=True)).to_be_visible()
        expect(board.get_by_role("button", name=re.compile("^Change waiting: "))).to_have_count(0)
        changed = next(r for r in h.get("programme_lead", "/api/catalogues")["rotations"] if r["id"] == first["id"])
        assert changed["rotation_type_id"] == types[2]["id"]
        page.reload()
        if viewport["width"] < 720:
            page.get_by_role("button", name="Show the timeline", exact=True).click()
        expect(page.locator(f'.tl [data-row="{trainee}"]').get_by_role("button", name="Clinic, 1 Oct – 31 Oct 2026", exact=True)).to_be_visible()
    finally:
        context.close()


def test_programme_lead_plans_and_notes(browser, live_server, all_roles):
    h = all_roles
    p, cohort, types, (trainee, local, visitor), planned = residency(h)
    context, page = sign_in(browser, live_server, "programme_lead")
    try:
        navigate(page, "programmes/board")
        page.get_by_role("button", name="Next", exact=True).click()
        page.get_by_role("button", name="Plan rotations", exact=True).click()
        dialog = page.get_by_role("dialog", name="Plan rotations")
        dialog.get_by_label("First block starts", exact=True).fill("2027-01-01")
        dialog.get_by_label("Rotation type", exact=True).select_option(types[0]["id"])
        dialog.get_by_role("button", name="Plan rotations").click()
        expect(page.get_by_role("status").filter(has_text="3 rotations planned")).to_be_visible()
        assert len(h.get("programme_lead", "/api/catalogues")["rotations"]) == 12
        # Choosing a cohort of another programme picks that programme's trainees instead.
        other = h.configure("programmes", {"name": "Fictional second programme", "starts_on": "2026-01-01"})["record"]
        second = h.configure("cohorts", {"name": "Fictional second year", "programme_id": other["id"], "starts_on": "2026-11-01", "ends_on": "2027-11-01"})["record"]  # not current: the dialog opens on the first
        newcomer = h.configure("people", {"display_name": "Fictional second trainee"})["record"]["id"]
        for pid, starts in ((h.get("programme_lead", "/api/me")["person_id"], "2026-01-01"), (newcomer, "2026-11-01")):  # the lead leads it today
            h.configure("programme_memberships", {"person_id": pid, "programme_id": other["id"], "starts_on": starts, "ends_on": "2027-11-01"},
                        role="coordinator")
        page.reload()
        expect_loaded(page, "programmes/board")
        page.get_by_role("button", name="Plan rotations", exact=True).click()
        dialog = page.get_by_role("dialog", name="Plan rotations")
        dialog.get_by_label("Cohort", exact=True).select_option(second["id"])
        dialog.get_by_label("First block starts", exact=True).fill("2027-02-01")
        dialog.get_by_label("Rotation type", exact=True).select_option(types[1]["id"])
        dialog.get_by_role("button", name="Plan rotations").click()
        expect(page.get_by_role("status").filter(has_text="1 rotation planned")).to_be_visible()
        assert [r["person_id"] for r in h.get("programme_lead", "/api/catalogues")["rotations"] if r["cohort_id"] == second["id"]] == [newcomer]
        board = page.get_by_role("region", name="Rotation timeline")
        board.locator(f'.tl-row[data-row="{trainee}"] .tl-who').click()
        panel = page.locator(".dock-panel")
        # While the note saves, its box cannot be typed into (nothing typed then is lost with the saved text).
        held = []
        page.route("**/api/commands", lambda route: held.append(route) if "trainee_note.create" in (route.request.post_data or "") else route.continue_())
        panel.get_by_label("Add a note", exact=True).fill("Fictional progress note")
        with page.expect_request(lambda r: "trainee_note.create" in (r.post_data or "")):
            panel.get_by_role("button", name="Save note").click()
        expect(panel.get_by_label("Add a note", exact=True)).not_to_be_editable()
        held[0].continue_()
        page.unroute("**/api/commands")
        expect(panel.get_by_label("Add a note", exact=True)).to_be_editable()
        expect(panel.get_by_label("Add a note", exact=True)).to_have_value("")
        expect(panel.locator(".rb-note p")).to_have_text("Fictional progress note")
        shot(page, "lead-trainee-panel")
        expect(board.locator(f'.tl-row[data-row="{trainee}"] .rb-notes')).to_have_accessible_name("1 note")  # the row's notes count
        assert [n["body"] for n in h.get("programme_lead", f"/api/people/{trainee}/trainee-notes")["notes"]] == ["Fictional progress note"]
        # A note being written survives deleting another; then nothing is left unsaved.
        panel.get_by_label("Add a note", exact=True).fill("Fictional draft note")
        page.wait_for_load_state("networkidle")  # the board's refresh after the save has settled (it moves focus)
        panel.locator(".rb-note").get_by_role("button", name="Delete").press("Enter")  # toasts may cover the panel's foot
        expect(panel.locator(".rb-note")).to_have_count(0)
        expect(panel.get_by_label("Add a note", exact=True)).to_have_value("Fictional draft note")
        # Undo brings the note back into the open panel; the draft stays.
        page.locator("#toast-tray .toast", has_text="Note deleted").get_by_role("button", name="Undo", exact=True).click()
        expect(panel.locator(".rb-note p")).to_have_text("Fictional progress note")
        expect(panel.get_by_label("Add a note", exact=True)).to_have_value("Fictional draft note")
        # Saved notes and their authors never reach a feedback screenshot (auditors read those; notes are the leads').
        author = panel.locator(".rb-note small").inner_text().split(" · ")[0]
        page.keyboard.press("Control+Shift+F")
        expect(page.get_by_role("dialog", name="Send feedback")).to_have_attribute("data-captured", "yes")  # copied
        copied = page.evaluate("() => window.__fbSnapshot.root.textContent")
        assert "Fictional progress note" not in copied and author not in copied and "Fictional draft note" not in copied
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog", name="Send feedback")).to_have_count(0)
        panel.get_by_label("Add a note", exact=True).fill("")
        # A saved note leaves nothing unsaved. Rotation types are department-wide: a lead reads them, only a coordinator changes them.
        page.get_by_role("navigation", name="Sections").get_by_role("button", name="Rotation types").click()
        expect_loaded(page, "programmes/rotation_types")
        expect(page.locator("#main").get_by_text("Emergency", exact=True).first).to_be_visible()
        expect(page.get_by_role("button", name="New rotation type")).to_have_count(0)
        expect(page.locator("#main").get_by_role("button", name="Edit", exact=True)).to_have_count(0)
    finally:
        context.close()


def test_programme_lead_copies_a_month_forward(browser, live_server, all_roles):
    """Copy forward takes rotations starting in the chosen days, the last one included, and no more."""
    h = all_roles
    residency(h)
    context, page = sign_in(browser, live_server, "programme_lead")
    try:
        navigate(page, "programmes/board")
        page.get_by_role("button", name="More rotation actions").click()
        page.get_by_role("menuitem", name=re.compile("^Copy rotations forward")).click()
        dialog = page.get_by_role("dialog", name="Copy rotations forward")
        dialog.get_by_label("Rotations starting from", exact=True).fill("2026-10-01")
        dialog.get_by_label("Until (included)", exact=True).fill("2026-10-31")
        dialog.get_by_label("Move forward by · months", exact=True).fill("6")
        dialog.get_by_role("button", name="Copy rotations").click()
        expect(page.get_by_role("status").filter(has_text="copied")).to_be_visible()
        copied = [r for r in h.get("programme_lead", "/api/catalogues")["rotations"] if r["starts_on"] >= "2027-04-01"]
        assert copied and {r["starts_on"] for r in copied} == {"2027-04-01"}  # not the rotations starting 1 Nov
    finally:
        context.close()


def test_trainee_sees_own_board_and_asks_for_a_change(browser, live_server, all_roles):
    h = all_roles
    p, cohort, types, (trainee, local, visitor), planned = residency(h)
    assert h.get("staff", "/api/me")["trainee"] is True
    elective = h.configure("rotation_types", {"name": "Fictional elective", "starts_on": "2026-01-01"})["record"]  # on nobody's rotations
    context, page = sign_in(browser, live_server, "staff")
    try:
        page.get_by_role("navigation", name="Department pages").get_by_role("button", name="Programmes", exact=True).click()
        board = page.get_by_role("region", name="Rotation timeline")
        expect(board.locator(".tl-row")).to_have_count(1)
        expect(page.get_by_role("button", name="Plan rotations")).to_have_count(0)
        shot(page, "trainee-board")
        board.get_by_role("button", name="Emergency, 1 Oct – 31 Oct 2026").click()
        page.locator(".dock-panel").get_by_role("button", name="Request a change").click()
        dialog = page.get_by_role("dialog", name="Request a rotation change")
        dialog.get_by_label("Rotation type", exact=True).select_option(elective["id"])
        # The last day included shows as such and is stored as the day after (one conversion, not two).
        until = dialog.get_by_label("Until (included)", exact=True)
        expect(until).to_have_value("Sat 31 Oct 2026")
        until.fill("2026-10-30")
        dialog.get_by_label("Why", exact=True).fill("Fictional reason for the change")
        dialog.get_by_role("button", name="Send request").click()
        expect(board.get_by_role("button", name="Emergency, 1 Oct – 31 Oct 2026, change requested")).to_be_visible()
        waiting = h.get("programme_lead", "/api/requests/all")["items"]
        assert [(i["kind"], i["person_id"]) for i in waiting] == [("rotation_change", trainee)]
        proposed = h.get("staff", "/api/rotation-board?start=2026-10-01&end=2027-01-01")["changes"][0]["proposed"]
        assert (proposed["rotation_type_id"], proposed["ends_on"]) == (elective["id"], "2026-10-31")
    finally:
        context.close()


def test_rotation_change_reason_never_enters_a_feedback_snapshot(browser, live_server, all_roles):
    h = all_roles
    residency(h)
    context, page = sign_in(browser, live_server, "staff")
    try:
        page.get_by_role("navigation", name="Department pages").get_by_role("button", name="Programmes", exact=True).click()
        page.get_by_role("region", name="Rotation timeline").get_by_role("button", name="Emergency, 1 Oct – 31 Oct 2026").click()
        page.locator(".dock-panel").get_by_role("button", name="Request a change").click()
        page.get_by_role("dialog", name="Request a rotation change").get_by_label("Why", exact=True).fill("Private family matter XYZZY")
        page.keyboard.press("Control+Shift+F")
        expect(page.get_by_role("dialog", name="Send feedback")).to_have_attribute("data-captured", "yes")  # copied
        leaked = page.evaluate("""() => { const r = window.__fbSnapshot.root;
            return r.textContent.includes('XYZZY') || [...r.querySelectorAll('textarea,input')].some(i => (i.getAttribute('value')||'').includes('XYZZY')); }""")
        assert leaked is False
    finally:
        context.close()


def test_coordinator_lead_approves_only_their_trainees_requests(browser, live_server, all_roles):
    """A coordinator who also leads a programme (no approver role) reads every request but is offered Approve only on
    their trainees' (the server's can_decide), so a mixed selection never fails as a whole."""
    from apps.api import models as m
    from apps.api.db import transaction
    from packages.contracts.schemas import RotationTypeInput
    from .governance_support import identity
    h = all_roles
    _, _, types, (trainee, _, _), _ = residency(h)
    h.configure('rotation_types', {**{key: types[0][key] for key in RotationTypeInput.model_fields},
        'leave_policy': 'needs_approval'}, types[0]['id'], types[0]['revision'])
    outsider = h.configure("people", {"display_name": "Fictional outside colleague"})["record"]["id"]
    with transaction(h.app.state.engine) as db:
        director = db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id
    identity(h, "coordinator_lead", ["coordinator", "programme_lead"], person=director)
    names = {p["id"]: p["display_name"] for p in h.get("admin", "/api/catalogues")["people"]}
    for pid, on in ((trainee, "2026-10-04"), (outsider, "2026-10-05")):
        h.command("coordinator", "request.create", {"person_id": pid, "kind": "leave", "start": on + "T09:00:00+03:00",
                                                    "end": on + "T10:00:00+03:00", "private_reason": "Fictional confidential request"})
    context, page = sign_in(browser, live_server, "coordinator_lead", {"width": 1440, "height": 900})
    try:
        navigate(page, "requests")
        whole_list(page)  # Waiting for you (maintenance) holds only their trainee's request
        rows = page.locator("#main tr.dt-open")
        expect(rows.filter(has_text=names[outsider])).to_have_count(1)
        expect(rows.filter(has_text=names[outsider]).get_by_role("button", name="Approve", exact=True)).to_have_count(0)
        expect(rows.filter(has_text=names[trainee]).get_by_role("button", name="Approve", exact=True)).to_be_visible()
        for name in (names[trainee], names[outsider]):
            box = rows.filter(has_text=name).locator("input.bulk-check")
            if box.count():
                box.check()
        expect(page.get_by_role("toolbar", name="Bulk actions").get_by_role("button", name="Approve 1")).to_be_visible()
    finally:
        context.close()


def test_rotation_templates_keep_retired_types_and_shared_ones_stay_read_only_for_leads(browser, live_server, all_roles):
    """A lead renames their programme's template without losing a block's rotation type retired since; a shared template
    (no programme) is read-only to them; a new one belongs to a programme they lead."""
    h = all_roles
    p, _, types, _, _ = residency(h)
    block = {"rotation_type_id": types[0]["id"], "service_id": None, "length": 1, "unit": "months", "department_available": True}
    own = h.configure("rotation_templates", {"name": "Fictional own plan", "programme_id": p["id"], "blocks": [block]})["record"]
    h.configure("rotation_templates", {"name": "Fictional shared plan", "blocks": [block]})
    h.command("admin", "catalogue.bulk_update", {"catalogue": "rotation_types", "items": [{"id": types[0]["id"], "revision": types[0]["revision"]}],
                                                 "active": False, "reason": "Fictional retirement"})
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        navigate(page, "programmes/rotation_templates")
        rows = page.locator("#main tbody tr")
        expect(rows.filter(has_text="Fictional shared plan")).to_have_count(1)
        expect(rows.filter(has_text="Fictional shared plan").get_by_role("button", name="Edit", exact=True)).to_have_count(0)
        rows.filter(has_text="Fictional own plan").get_by_role("button", name="Edit", exact=True).click()
        dialog = page.get_by_role("dialog", name=re.compile("^Edit"))
        dialog.get_by_label("Name", exact=True).fill("Fictional renamed plan")
        dialog.get_by_role("button", name="Save version", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="Fictional renamed plan saved").first).to_be_visible()
        saved = next(t for t in h.get("programme_lead", "/api/catalogues")["rotation_templates"] if t["id"] == own["id"])
        assert (saved["name"], saved["blocks"][0]["rotation_type_id"]) == ("Fictional renamed plan", types[0]["id"])
        page.get_by_role("button", name="New rotation template", exact=True).click()
        programme = page.get_by_role("dialog").get_by_label("Programme", exact=True)
        expect(programme).to_have_value(p["id"])
        assert programme.evaluate("e => e.required") is True
    finally:
        context.close()


def test_reordering_template_blocks_is_an_unsaved_change(browser, live_server, all_roles):
    """Moving a block is an edit: closing the template without saving asks first."""
    h = all_roles
    _, _, types, _, _ = residency(h)
    blocks = [{"rotation_type_id": t["id"], "service_id": None, "length": 1, "unit": "months", "department_available": True} for t in types[:2]]
    h.configure("rotation_templates", {"name": "Fictional ordered plan", "blocks": blocks})
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        navigate(page, "programmes/rotation_templates")
        page.locator("#main tbody tr").filter(has_text="Fictional ordered plan").get_by_role("button", name="Edit", exact=True).click()
        dialog = page.get_by_role("dialog", name=re.compile("^Edit"))
        dialog.get_by_role("button", name="Move block earlier").nth(1).click()
        page.keyboard.press("Escape")
        expect(dialog.get_by_role("alert")).to_contain_text("Not saved.")
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_changes_waiting_is_its_own_layer_with_rotations_hidden(browser, live_server, all_roles, scheme):
    """Each layer stands alone: with Rotations hidden and "Changes waiting" on, the proposed bar still shows; turning
    "Changes waiting" off hides it."""
    h = all_roles
    p, cohort, types, (trainee, local, visitor), planned = residency(h)
    first = next(r for r in planned if r["person_id"] == trainee and r["starts_on"] == "2026-10-01")
    h.command("staff", "rotation.request_change", {"rotation_id": first["id"], "rotation_type_id": types[2]["id"], "starts_on": "2026-10-01",
                                                   "ends_on": "2026-11-01", "private_reason": "Fictional elective swap"})
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, "programmes/board")
        board = page.get_by_role("region", name="Rotation timeline")
        proposed = board.get_by_role("button", name=re.compile("^Change waiting: Clinic"))
        expect(proposed).to_be_visible()
        page.get_by_role("button", name=re.compile("^View settings")).click()
        layers = page.locator(".dt-pop").get_by_role("group", name="Layers")
        layers.get_by_role("checkbox", name=re.compile("^Rotations")).uncheck()
        expect(board.locator('button.tl-bar[data-bar]:not([data-bar*=":"])')).to_have_count(0)
        expect(proposed).to_be_visible()
        layers.get_by_role("checkbox", name="Changes waiting", exact=True).uncheck()
        expect(proposed).to_have_count(0)
    finally:
        context.close()


def test_every_plan_entry_says_a_cohort_is_missing_and_creates_one(browser, live_server, all_roles):
    """P-17 regression: with trainees but no cohort, Plan rotations (toolbar, a trainee's Plan bar) opens on what is
    missing, with the step that creates a cohort, instead of an empty required Cohort picker."""
    h = all_roles
    p = h.configure("programmes", {"name": "Fictional cohortless residency", "starts_on": "2026-01-01"})["record"]
    lead_of(h, p)
    trainee = h.seed["people"][0]
    h.configure("programme_memberships", {"person_id": trainee, "programme_id": p["id"], "starts_on": "2026-07-01", "ends_on": "2027-07-01"},
                role="programme_lead")
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        navigate(page, "programmes/board")
        toolbar = page.locator("#main").get_by_role("button", name="Plan rotations", exact=True)
        expect(toolbar).not_to_have_attribute("aria-disabled", "true")
        # At 1440 the whole toolbar (views, period, search, Filter … Plan rotations, More) sits on one line.
        rows = page.locator("#main .dt-toolbar").first.evaluate(
            "t => new Set([...t.children].filter(c => c.offsetParent).map(c => { const r = c.getBoundingClientRect(); return Math.round(r.top + r.height / 2); })).size")
        assert rows == 1, rows
        page.locator(f'.tl-row[data-row="{trainee}"]').get_by_role("button", name=re.compile("^Plan rotations for ")).first.click()
        dialog = page.get_by_role("dialog", name="Plan rotations")
        expect(dialog.locator(".missing-setup")).to_contain_text("No cohorts yet")
        dialog.get_by_role("button", name="New cohort", exact=True).click()
        expect(page.get_by_role("dialog", name="New cohort")).to_be_visible()
        expect(dialog).to_have_count(0)
    finally:
        context.close()


def test_programme_name_is_said_once_by_its_group(browser, live_server, all_roles):
    """LIVE-08: grouped by programme (the default), the group heading names the programme; its trainees' rows do not
    repeat it under every name."""
    h = all_roles
    residency(h)
    context, page = sign_in(browser, live_server, "programme_lead")
    try:
        navigate(page, "programmes/board")
        board = page.get_by_role("region", name="Rotation timeline")
        expect(board.get_by_text("Fictional residency").first).to_be_visible()  # the group heading
        expect(board.locator(".tl-nm small", has_text="Fictional residency")).to_have_count(0)
    finally:
        context.close()
# Include this file in hosted supervision and team-lead qualification.

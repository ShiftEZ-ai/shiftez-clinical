"""Schedule views (Notion-style): the saved view first in the toolbar, View settings (layout, range, Person label…) and
the saved-view menu (new, duplicate, rename, reset, delete), kept on the server across reloads; desktop and phone."""
import os
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, toolbar, close_popover, view_settings, choose_radio, choose_layout, save_dialog, pick_view
from .test_browser_combined_acceptance import save_view

pytestmark = pytest.mark.browser
SHOTS = os.getenv("VIEW_SHOTS")  # a folder: the test also saves screenshots there (design review)


def shot(page, name):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, name + ".png"))


# The chip a part belongs to. Not locator(chips, has=part): a `has` locator is matched inside each chip, so one that
# starts at #schedule-view (outside the chip) never matches.
CHIP_OF = "xpath=ancestor::*[@data-session][contains(concat(' ', normalize-space(@class), ' '), ' pg-chip ')][1]"


def person_with_pager(h):
    people = h.get("admin", "/api/catalogues")["people"]
    p = next(x for x in people if x["id"] == h.seed["people"][0])
    value = {k: p[k] for k in ("display_name", "active", "title", "work_phone", "work_extension", "staff_number", "directory_section", "directory_order", "short_code", "tags", "colour") if k in p}  # legacy phone fields are absent without contacts
    h.configure("people", {**value, "pager": "4471"}, p["id"], p["revision"])
    return p


def test_toolbar_order_and_saved_view_journey(browser, live_server, h):
    w = h.workspace("View settings rota")
    h.shift(w, people=h.seed["people"][:1])
    person = person_with_pager(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "schedule/week/2026-10-04")
        bar = page.locator(".planner-toolbar")
        # One order everywhere : saved view (named after what is shown), period, Search, Filter, Group, Sort, View settings.
        order = bar.evaluate("""bar => [...bar.querySelectorAll(':scope > .dt-tool, :scope > .wk-nav, :scope > .dt-search-wrap')].map(n =>
            n.classList.contains('dt-saved') ? 'saved' : n.classList.contains('wk-nav') ? 'period' : n.matches('.dt-search-wrap') ? 'search'
            : n.getAttribute('aria-label').split(/[:(]/)[0].trim())""")
        assert order == ["saved", "period", "search", "Filter", "Group", "Sort", "View settings"], order
        expect(bar.locator(".dt-saved .dt-tool-label")).to_have_text("Default view")
        expect(bar.get_by_role("button", name="Group: Service", exact=True)).to_have_text("Group: Service")
        view = bar.get_by_role("button", name=re.compile("^View settings"))
        expect(view).to_have_attribute("data-tip", re.compile("^View settings"))
        expect(view.locator(".dt-tool-label")).to_have_count(0)  # an icon, named for screen readers and in its tooltip

        # Range: 2 weeks is the same calendar over fourteen days.
        panel = view_settings(bar)
        expect(panel.get_by_role("radiogroup", name="Layout", exact=True).get_by_role("radio", name="Calendar", exact=True)).to_have_attribute("aria-checked", "true")
        choose_radio(panel, "Range", "2 weeks")
        expect(page.locator(".wk-grid .wk-head .wk-day")).to_have_count(14)
        shot(page, "settings-main")

        # Person label: pager first, then the short name, joined with a comma.
        panel = view_settings(bar, "Properties")
        panel.locator(".vs-row", has_text="Person label").click()
        # The Person label editor (view_settings.ts labelBody, D75/D80): one name format, what is added to it, and
        # where; the parts' words are the contract's (LABEL_PART_DEFINITIONS). Each choice keeps the keyboard's place.
        choose_radio(panel, "Name", "F. Lastname")
        panel.get_by_label("Add to the name", exact=True).select_option("pager")
        expect(panel.get_by_role("button", name="Hide Pager", exact=True)).to_be_focused()
        place = panel.get_by_label("Place Pager", exact=True)
        expect(place).to_have_value("after")
        place.select_option("before")
        expect(place).to_have_value("before")
        expect(place).to_be_focused()
        panel.get_by_label("Separator", exact=True).select_option("comma")
        expect(panel.get_by_label("Preview")).to_contain_text("4471, ")
        shot(page, "settings-label")
        close_popover(page)
        expect(page.locator(".pchip-name", has_text="4471, ").first).to_be_visible()

        # Save it as a view: the dropdown now names it, and a reload brings it back from the server.
        toolbar(bar, "Saved views").get_by_role("menuitem", name="New view…", exact=True).click()
        form = page.get_by_role("dialog", name="New view")
        form.get_by_label("View name", exact=True).fill("Two weeks by pager")
        save_dialog(form, "Save view")
        expect(bar.locator(".dt-saved .dt-tool-label")).to_have_text("Two weeks by pager")
        saved = h.get("coordinator", "/api/table-views?table=planner")["views"][0]
        assert saved["definition"]["planner"]["range"] == "2weeks"
        assert saved["definition"]["planner"]["label"] == {"parts": ["pager", "short"], "separator": "comma", "honorific": "dr"}
        page.reload()
        expect(bar.locator(".dt-saved .dt-tool-label")).to_have_text("Two weeks by pager")
        expect(page.locator(".wk-grid .wk-head .wk-day")).to_have_count(14)
        expect(page.locator(".pchip-name", has_text="4471, ").first).to_be_visible()

        # Rename in View settings; the name changes, the settings stay.
        panel = view_settings(bar)
        name = panel.get_by_label("View name", exact=True)
        name.fill("Fortnight by pager")
        name.press("Enter")
        expect(bar.locator(".dt-saved .dt-tool-label")).to_have_text("Fortnight by pager")
        close_popover(page)
        assert h.get("coordinator", "/api/table-views?table=planner")["views"][0]["name"] == "Fortnight by pager"

        # Duplicate opens the copy; Reset to default leaves it changed, ready to update or discard.
        toolbar(bar, "Saved views").get_by_role("menuitem", name="Duplicate “Fortnight by pager”…", exact=True).click()
        form = page.get_by_role("dialog", name="Duplicate view")
        expect(form.get_by_label("View name", exact=True)).to_have_value("Fortnight by pager copy")
        save_dialog(form, "Duplicate")
        expect(bar.locator(".dt-saved .dt-tool-label")).to_have_text("Fortnight by pager copy")
        assert len(h.get("coordinator", "/api/table-views?table=planner")["views"]) == 2
        panel = view_settings(bar)
        panel.get_by_role("button", name="Reset to default", exact=True).click()
        expect(page.locator(".wk-grid .wk-head .wk-day")).to_have_count(7)
        expect(panel.get_by_role("button", name="Update view", exact=True)).to_be_visible()
        expect(bar.get_by_role("button", name="Saved views: Fortnight by pager copy (changed)", exact=True)).to_be_visible()
        close_popover(page)

        # Delete acts at once (Undo in its message) and goes back to the default view.
        menu = toolbar(bar, "Saved views")
        menu.get_by_role("menuitem", name="Delete “Fortnight by pager copy”", exact=True).click()
        expect(bar.locator(".dt-saved .dt-tool-label")).to_have_text("Default view")
        assert [v["name"] for v in h.get("coordinator", "/api/table-views?table=planner")["views"]] == ["Fortnight by pager"]
    finally:
        context.close()


def test_view_settings_fit_a_phone(browser, live_server, h):
    w = h.workspace("Phone view rota")
    h.shift(w, people=h.seed["people"][:1])
    context, page = sign_in(browser, live_server, "coordinator", {"width": 390, "height": 844})
    try:
        navigate(page, "schedule/week/2026-10-04")
        bar = page.locator(".planner-toolbar")
        expect(bar.locator(".dt-saved .dt-tool-label")).to_be_visible()  # the view's name stays readable on a phone
        panel = view_settings(bar)
        expect(panel).to_be_in_viewport(ratio=1)
        choose_layout(panel, "People")
        expect(page.locator("#schedule-view")).to_have_attribute("aria-label", "People view")
        # Layout and range are independent: People keeps the week chosen before.
        expect(panel.get_by_role("radiogroup", name="Range", exact=True).get_by_role("radio", name="Week", exact=True)).to_have_attribute("aria-checked", "true")
        panel = view_settings(bar, "Layers")
        expect(panel.get_by_label("Leave and wishes", exact=True)).to_be_visible()
        shot(page, "settings-phone")
    finally:
        context.close()


def test_calendar_rows_are_duties_or_people_and_people_group_by_rank(browser, live_server, h):
    """Calendar is one layout: its rows are duties or people; people's cells show duty codes or duties and hours (kept
    when rows switch back); people can be grouped by their rank."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner"); settled(page)
        bar = page.locator(".planner-toolbar")
        panel = view_settings(bar)
        expect(panel.get_by_role("radiogroup", name="Cells show", exact=True).get_by_role("radio", name="Sessions", exact=True)).to_be_visible()
        choose_radio(panel, "Rows", "People")
        expect(page.locator("#schedule-view")).to_have_attribute("aria-label", "Timeline view")
        choose_radio(panel, "Cells show", "Duties and hours")
        expect(page.locator("#schedule-view")).to_have_attribute("aria-label", "People view")
        choose_radio(panel, "Rows", "Duties")
        expect(page.locator("#schedule-view")).to_have_attribute("aria-label", "Week view")
        expect(panel.get_by_role("radiogroup", name="Cells show", exact=True).get_by_role("radio", name="Counts", exact=True)).to_be_visible()
        choose_radio(panel, "Rows", "People")
        expect(page.locator("#schedule-view")).to_have_attribute("aria-label", "People view")
        choose_radio(panel, "Cells show", "Duty codes")
        close_popover(page)
        group = toolbar(bar, "Group")
        group.get_by_label("Group by", exact=True).select_option("rank")
        close_popover(page)
        heads = page.locator("#schedule-view .tl-grid .pg-group-head")
        expect(heads.first).to_be_visible()
        expect(page.locator("#schedule-view .tl-cover")).to_have_count(0)  # a session's places belong to no rank
        # Back to duty rows: groups of people do not apply there.
        choose_layout(view_settings(bar), "Calendar")
        expect(bar.get_by_role("button", name="Group: Service", exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize("mode,scheme", [("person", "light"), ("timeline", "dark")])
def test_people_rows_show_the_views_properties_and_colour(browser, live_server, h, mode, scheme):
    """a cell's properties change whatever the rows and columns are. People × Days (Duties and hours,
    mode person) and the codes grid (mode timeline) draw each duty with its code, then the view's properties in its
    order (Place here), say them in the chip's name, and follow Colour by (one colour per person)."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    off = [{"key": "location", "on": False, "form": "text"}]
    on = [{"key": "location", "on": True, "form": "text"}]
    for name, props, colour in ((f"Fictional places off {mode}", off, "type"), (f"Fictional places on {mode}", on, "person")):
        h.command("coordinator", "table_view.create", {"table_key": "planner", "name": name, "definition": {"planner": {
            "mode": mode, "range": "week", "properties": props, "colour_by": colour}}})
    context, page = sign_in(browser, live_server, "coordinator", color_scheme=scheme)
    try:
        navigate(page, "planner"); settled(page)
        chips = page.locator("#schedule-view .pg-chip[data-session]")
        pick_view(page, f"Fictional places off {mode}"); settled(page)
        expect(chips.first).to_be_visible()
        expect(page.locator("#schedule-view .pg-chip[data-session] .pg-chip-meta")).to_have_count(0)
        pick_view(page, f"Fictional places on {mode}"); settled(page)
        meta = page.locator("#schedule-view .pg-chip[data-session] .pg-chip-meta").first
        expect(meta).not_to_be_empty()
        chip = meta.locator(CHIP_OF)
        expect(chip).to_have_attribute("aria-label", re.compile(re.escape(meta.inner_text())))
        expect(page.get_by_text("Colour: one per person").first).to_be_attached()
        shot(page, f"people-properties-{mode}-{scheme}")
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_staffing_numbers_can_be_hidden_per_view(browser, live_server, h, scheme):
    """the staffing numbers on the columns can be hidden. PlannerViewSettings.staffing false hides the
    places filled / needed under each day (and the group totals) for that view; the default view still shows them."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    h.command("coordinator", "table_view.create", {"table_key": "planner", "name": "Fictional no staffing numbers",
        "definition": {"planner": {"mode": "week", "range": "week", "staffing": False}}})
    context, page = sign_in(browser, live_server, "coordinator", color_scheme=scheme)
    try:
        navigate(page, "planner"); settled(page)
        numbers = page.locator("#schedule-view .wk-day .wk-cov")
        expect(numbers.first).to_be_visible()
        pick_view(page, "Fictional no staffing numbers"); settled(page)
        expect(numbers).to_have_count(0)
        expect(page.locator("#schedule-view .wk-sum")).to_have_count(0)
        box = view_settings(page.locator(".planner-toolbar")).get_by_label("Staffing numbers", exact=True)
        expect(box).not_to_be_checked()
        box.check()
        expect(numbers.first).to_be_visible()
        close_popover(page)
    finally:
        context.close()


PLANNER_READS = re.compile(r"/api/(workspaces|table-views|jobs|schedule/sessions|publications)\b")


@pytest.mark.parametrize("width,scheme", [(1440, "light"), (390, "dark")])
def test_view_settings_choices_redraw_without_reading_again_after_two_minutes(browser, live_server, h, width, scheme):
    """QA-024: past the planner memo's two minutes, turning Staffing numbers off read the rota list, saved views, the
    draft and its jobs again (1.5–4 s on DEV) while Time had toggled at once. A View settings choice that only changes how
    the same rota is drawn reads nothing, however long the page has been open; the saved view still turns dirty."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    # A phone's Week is an agenda (no staffing numbers): there the People × Days grid's Staffed / needed row.
    phone = width < 600
    layout = {"mode": "timeline", "rows": "person"} if phone else {"mode": "week"}
    h.command("coordinator", "table_view.create", {"table_key": "planner", "name": "Fictional staffing shown",
        "definition": {"planner": {**layout, "range": "week"}}})
    context, page = sign_in(browser, live_server, "coordinator", {"width": width, "height": 900}, color_scheme=scheme, has_touch=phone)
    reads = []
    page.on("request", lambda r: reads.append(r.url) if PLANNER_READS.search(r.url) else None)
    try:
        navigate(page, "planner"); settled(page)
        pick_view(page, "Fictional staffing shown"); settled(page)
        numbers = page.locator("#schedule-view " + (".tl-cover" if phone else ".wk-day .wk-cov"))
        expect(numbers.first).to_be_visible()
        # Three minutes later without firing timers (no live poll or refresh resets the memo's age).
        page.clock.set_system_time(page.evaluate("Date.now()") + 180_000)
        reads.clear()
        bar = page.locator(".planner-toolbar")
        box = view_settings(bar).get_by_label("Staffing numbers", exact=True)
        if phone:
            box.tap()
        else:
            box.focus(); page.keyboard.press("Space")
        expect(numbers).to_have_count(0)
        expect(box).not_to_be_checked()
        expect(page.locator(".vs-foot").get_by_role("button", name="Update view", exact=True)).to_be_visible()
        panel = view_settings(bar, "Properties")
        # A card shows its time by default, a grid's chip does not (VIEW_ITEM_SHOWN_BY_DEFAULT): each toggles the other way.
        before, after = ("Show Time", "Hide Time") if phone else ("Hide Time", "Show Time")
        time = panel.get_by_role("button", name=before, exact=True)
        time.tap() if phone else time.click()
        expect(panel.get_by_role("button", name=after, exact=True)).to_be_visible()
        assert reads == [], reads
        close_popover(page)
        shot(page, f"staffing-toggle-no-reads-{width}-{scheme}")
    finally:
        context.close()


@pytest.mark.parametrize("width,scheme", [(1440, "light"), (390, "dark")])
def test_session_cells_name_their_people_when_the_view_shows_them(browser, live_server, h, width, scheme):
    """D80: People is a registry property, hidden by default. Where a cell is a session (a codes grid whose rows are not
    people), it names the session's people by the view's Person label; it is offered there and not on Week's cards."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    names = [p["display_name"] for p in h.get("coordinator", "/api/catalogues")["people"]]
    for name, on in (("Fictional services plain", False), ("Fictional services with people", True)):
        h.command("coordinator", "table_view.create", {"table_key": "planner", "name": name, "definition": {"planner": {
            "mode": "timeline", "range": "week", "rows": "service", "group_by": "none",  # rows and group: different dimensions
            "properties": [{"key": "people", "on": on, "form": "text"}]}}})
    context, page = sign_in(browser, live_server, "coordinator", {"width": width, "height": 900}, color_scheme=scheme)
    try:
        navigate(page, "planner"); settled(page)
        bar = page.locator(".planner-toolbar")
        panel = view_settings(bar, "Properties")
        expect(panel.locator('.vs-prop[data-part="people"]')).to_have_count(0)  # Week's cards always name their people
        close_popover(page)
        meta = page.locator("#schedule-view .pg-chip[data-session] .pg-chip-meta")
        pick_view(page, "Fictional services plain"); settled(page)
        expect(page.locator("#schedule-view .pg-chip[data-session]").first).to_be_visible()
        expect(meta).to_have_count(0)
        pick_view(page, "Fictional services with people"); settled(page)
        named = meta.filter(has_text=re.compile("|".join(re.escape(n) for n in names))).first
        expect(named).to_be_visible()
        chip = named.locator(CHIP_OF)
        expect(chip).to_have_attribute("aria-label", re.compile(re.escape(named.inner_text())))
        panel = view_settings(bar, "Properties")
        expect(panel.locator('.vs-prop[data-part="people"]')).to_have_count(1)
        close_popover(page)
    finally:
        context.close()


@pytest.mark.parametrize("width,scheme", [(1440, "dark"), (390, "light")])
def test_grid_person_rows_show_leave_and_wishes_when_the_view_shows_them(browser, live_server, h, width, scheme):
    """D80: Leave and wishes is a registry property, hidden by default, drawn where a grid has a person's days (people
    rows by day, the codes grid) by the same leave layer People × Days uses; People keeps its Layers switch."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    person = h.seed["people"][0]
    h.command("coordinator", "availability.record", {"person_id": person, "kind": "recorded_leave", "leave_type": "Annual leave",
        "start": "2026-10-06T00:00:00+03:00", "end": "2026-10-07T00:00:00+03:00", "private_reason": "Fictional leave"})
    for name, on in (("Fictional codes without leave", False), ("Fictional codes with leave", True)):
        h.command("coordinator", "table_view.create", {"table_key": "planner", "name": name, "definition": {"planner": {
            "mode": "timeline", "range": "week", "properties": [{"key": "leave", "on": on, "form": "text"}]}}})
    context, page = sign_in(browser, live_server, "coordinator", {"width": width, "height": 900}, color_scheme=scheme)
    try:
        navigate(page, "planner"); settled(page)
        row = page.locator(f'#schedule-view .pg-row[data-row-person="{person}"]').first
        pick_view(page, "Fictional codes without leave"); settled(page)
        expect(row).to_be_visible()
        expect(row.locator(".pg-cell.leave")).to_have_count(0)
        pick_view(page, "Fictional codes with leave"); settled(page)
        leave = row.locator(".pg-cell.leave")
        expect(leave).to_have_count(1)
        expect(leave).to_have_attribute("data-tip", re.compile("^On leave or away"))
        panel = view_settings(page.locator(".planner-toolbar"), "Properties")
        expect(panel.locator('.vs-prop[data-part="leave"]')).to_have_count(1)
        close_popover(page)
        expect(view_settings(page.locator(".planner-toolbar"), "Layers").get_by_label("Leave and wishes", exact=True)).to_have_count(0)
        close_popover(page)
    finally:
        context.close()


@pytest.mark.parametrize("width,scheme", [(1440, "light"), (390, "dark")])
def test_coverage_rows_and_columns_are_chosen_and_saved(browser, live_server, h, width, scheme):
    """D80: Coverage takes Rows and Columns. The default (duty types over days) keeps the Coverage table; services over
    weeks (or any session dimension and date axis) count places filled / needed in the one counts grid, saved with the view."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    h.command("coordinator", "table_view.create", {"table_key": "planner", "name": "Fictional coverage by service",
        "definition": {"planner": {"mode": "coverage", "range": "week", "rows": "service", "columns": "week", "group_by": "none"}}})
    context, page = sign_in(browser, live_server, "coordinator", {"width": width, "height": 900}, color_scheme=scheme)
    try:
        navigate(page, "planner"); settled(page)
        bar = page.locator(".planner-toolbar")
        choose_layout(view_settings(bar), "Coverage")
        expect(page.locator("#schedule-view .coverage-table")).to_be_visible()
        panel = view_settings(bar)
        expect(panel.get_by_label("Rows", exact=True)).to_have_value("type")
        expect(panel.get_by_label("Columns", exact=True)).to_have_value("day")
        close_popover(page)
        pick_view(page, "Fictional coverage by service"); settled(page)
        grid = page.locator("#schedule-view .ct-grid")
        expect(grid).to_be_visible()
        expect(page.locator("#schedule-view .coverage-table")).to_have_count(0)
        expect(grid.locator(".pg-cell.sg-count").filter(has_text=re.compile(r"^\d+/\d+$")).first).to_be_visible()
        panel = view_settings(bar)
        expect(panel.get_by_label("Rows", exact=True)).to_have_value("service")
        expect(panel.get_by_label("Columns", exact=True)).to_have_value("week")
        close_popover(page)
    finally:
        context.close()


@pytest.mark.parametrize("width,scheme", [(1440, "dark"), (390, "light")])
def test_each_layout_keeps_its_own_columns(browser, live_server, h, width, scheme):
    """D80 (Notion: a view's layout settings are its own): the grid's Columns set to weeks never carry into Coverage,
    which opens as its table (duty types over days); Coverage's own axes are saved with the view (coverage) and the
    calendar's Columns are still weeks when it comes back."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    context, page = sign_in(browser, live_server, "coordinator", {"width": width, "height": 900}, color_scheme=scheme)
    try:
        navigate(page, "planner"); settled(page)
        bar = page.locator(".planner-toolbar")
        panel = view_settings(bar)
        choose_radio(panel, "Rows", "People")
        panel.get_by_label("Columns", exact=True).select_option("week")
        expect(page.locator("#schedule-view .pg-head .pg-day").first).to_contain_text("Week of")
        choose_layout(panel, "Coverage")
        expect(page.locator("#schedule-view .coverage-table")).to_be_visible()  # not the counts grid by weeks
        panel = view_settings(bar)
        expect(panel.get_by_label("Columns", exact=True)).to_have_value("day")
        panel.get_by_label("Rows", exact=True).select_option("service")
        expect(page.locator("#schedule-view .ct-grid")).to_be_visible()
        close_popover(page)
        save_view(page, bar, "Fictional coverage own axes")
        saved = next(v for v in h.get("coordinator", "/api/table-views?table=planner")["views"] if v["name"] == "Fictional coverage own axes")
        planner = saved["definition"]["planner"]
        assert planner["coverage"] == {"rows": "service"} and planner["columns"] == "week", planner
        panel = view_settings(bar)
        choose_radio(panel, "Layout", "Calendar")
        expect(view_settings(bar).get_by_label("Columns", exact=True)).to_have_value("week")
        close_popover(page)
    finally:
        context.close()


def test_people_view_keeps_supervision_team_grouping_in_a_saved_view(browser, live_server, h):
    """The People view's Supervision team grouping is saved as itself (once saved as Rank), and its default sort is
    left out of the view."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner"); settled(page)
        bar = page.locator(".planner-toolbar")
        panel = view_settings(bar)
        choose_radio(panel, "Rows", "People")
        choose_radio(panel, "Cells show", "Duties and hours")
        expect(page.locator("#schedule-view")).to_have_attribute("aria-label", "People view")
        close_popover(page)
        bar.get_by_role("button", name=re.compile("^Group")).click()
        page.get_by_role("menuitemradio", name="Supervision team", exact=True).click()
        close_popover(page)
        toolbar(bar, "Saved views").get_by_role("menuitem", name="New view…", exact=True).click()
        form = page.get_by_role("dialog", name="New view"); form.get_by_label("View name", exact=True).fill("By team")
        save_dialog(form, "Save view")
        saved = next(v for v in h.get("coordinator", "/api/table-views?table=planner")["views"] if v["name"] == "By team")
        assert saved["definition"]["planner"]["group_by"] == "team"
        assert "people_sort" not in saved["definition"]["planner"]
    finally:
        context.close()


def test_filters_take_several_values_and_are_saved_in_the_view(browser, live_server, h):
    """Filter: each field takes any of several values (the shared picker), shows as one chip, and a saved view keeps it."""
    from .test_browser import planner_filter, filter_pick
    from .test_browser_qa_planner import checked_rota, settled
    _, typ = checked_rota(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner"); settled(page)
        bar = page.locator(".planner-toolbar")
        filter_pick(planner_filter(page), "Duty type", "QA clinic")
        close_popover(page)
        expect(bar.get_by_role("button", name=re.compile(r"^Filter: .+ \(1 active\)$"))).to_be_visible()
        toolbar(bar, "Saved views").get_by_role("menuitem", name="New view…", exact=True).click()
        form = page.get_by_role("dialog", name="New view"); form.get_by_label("View name", exact=True).fill("QA clinic only")
        save_dialog(form, "Save view")
        saved = h.get("coordinator", "/api/table-views?table=planner")["views"][0]["definition"]["planner"]
        assert saved["filters"] == {"assignment_type_ids": [typ]}
    finally:
        context.close()


def test_columns_by_week_or_weekday_show_counts_and_are_saved(browser, live_server, h):
    """Columns: days, weeks or weekdays. Weeks and weekdays (and duty rows' Counts cells) show numbers: places filled /
    needed per duty, duties per person; the view keeps the choice."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner"); settled(page)
        bar = page.locator(".planner-toolbar")
        panel = view_settings(bar)
        choose_radio(panel, "Range", "2 weeks")
        choose_radio(panel, "Cells show", "Counts")
        grid = page.locator("#schedule-view .ct-grid")
        expect(grid.locator(".pg-day")).to_have_count(14)
        expect(grid.get_by_role("gridcell", name=re.compile(r"of \d+ places filled$")).first).to_be_visible()
        choose_radio(panel, "Columns", "Weeks")
        expect(grid.locator(".pg-day")).to_have_count(2)
        expect(panel.get_by_role("radiogroup", name="Cells show", exact=True)).to_be_hidden()
        choose_radio(panel, "Rows", "People")
        choose_radio(panel, "Columns", "Weekdays")
        expect(grid.get_by_role("gridcell", name=re.compile(r"duty|duties$")).first).to_be_visible()
        close_popover(page)
        toolbar(bar, "Saved views").get_by_role("menuitem", name="New view…", exact=True).click()
        form = page.get_by_role("dialog", name="New view"); form.get_by_label("View name", exact=True).fill("Weekday load")
        save_dialog(form, "Save view")
        saved = h.get("coordinator", "/api/table-views?table=planner")["views"][0]["definition"]["planner"]
        assert saved["columns"] == "weekday" and saved["mode"] == "timeline"
    finally:
        context.close()


def test_any_rows_columns_group_and_then_by_are_saved_with_collapsed_groups(browser, live_server, h):
    """The Schedule grid: Rows = any dimension, Columns = weeks or a category, Group › Then by nested and folded; the
    saved view keeps all of it and a reload draws the same grid, the folded subgroup still folded."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner"); settled(page)
        bar = page.locator(".planner-toolbar")
        panel = view_settings(bar)
        choose_radio(panel, "Rows", "Duty types")
        # A dimension cannot be both the rows and the columns.
        expect(panel.get_by_label("Columns", exact=True).locator("option", has_text=re.compile("^Duty types$"))).to_be_disabled()
        choose_radio(panel, "Columns", "Weeks")
        choose_radio(panel, "Cells show", "Counts")
        close_popover(page)
        # The new layout is drawn (after its rota read) before its Group menu is opened: a menu opened over the previous
        # Calendar keeps that view's choices, which have no Then by.
        expect(page.locator("#schedule-view")).to_have_attribute("aria-label", "Timeline view")
        group = toolbar(bar, "Group")
        expect(group.get_by_label("Group by", exact=True)).to_have_value("service")
        group.get_by_role("button", name="+ Add subgroup", exact=True).click()
        group.get_by_label("Then by", exact=True).select_option(label="Rank")
        close_popover(page)
        grid = page.locator("#schedule-view .sg-grid")
        expect(grid.locator(".pg-head .pg-name")).to_have_text("Service · Rank · Duty type")
        sub = grid.locator(".sg-sub .wk-toggle").first
        sub.click()
        expect(grid.locator(".sg-sub .wk-toggle").first).to_have_attribute("aria-expanded", "false")
        toolbar(bar, "Saved views").get_by_role("menuitem", name="New view…", exact=True).click()
        form = page.get_by_role("dialog", name="New view"); form.get_by_label("View name", exact=True).fill("Duty types by week")
        save_dialog(form, "Save view")
        saved = h.get("coordinator", "/api/table-views?table=planner")["views"][0]["definition"]["planner"]
        assert (saved["rows"], saved["columns"], saved["group_by"], saved["subgroup"], saved["cells"]) == ("type", "week", "service", "rank", "counts")
        assert len(saved["collapsed"]) == 1 and " › " in saved["collapsed"][0]
        page.reload(); settled(page)
        grid = page.locator("#schedule-view .sg-grid")
        expect(grid.locator(".pg-head .pg-name")).to_have_text("Service · Rank · Duty type")
        expect(grid.locator(".sg-sub .wk-toggle[aria-expanded=false]")).to_have_count(1)
        expect(bar.get_by_role("button", name="Saved views: Duty types by week", exact=True)).to_be_visible()
    finally:
        context.close()


def test_views_saved_before_the_axes_still_open(browser, live_server, h):
    """Older saved views (no rows or subgroup; a Timeline by weekday, a "month" calendar) open as they were saved."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    h.command("coordinator", "table_view.create", {"table_key": "planner", "name": "Old weekday counts",
              "definition": {"planner": {"mode": "timeline", "columns": "weekday", "group_by": "rank"}}})
    h.command("coordinator", "table_view.create", {"table_key": "planner", "name": "Old month", "definition": {"planner": {"mode": "month"}}})
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner"); settled(page)
        bar = page.locator(".planner-toolbar")
        toolbar(bar, "Saved views").get_by_role("menuitemradio", name=re.compile(r"^Old weekday counts")).click()
        close_popover(page)
        grid = page.locator("#schedule-view .sg-grid")
        expect(grid.get_by_role("columnheader", name=re.compile(r"^Every "))).not_to_have_count(0)
        expect(bar.get_by_role("button", name="Group: Rank", exact=True)).to_be_visible()
        toolbar(bar, "Saved views").get_by_role("menuitemradio", name=re.compile(r"^Old month")).click()
        close_popover(page)
        expect(page.locator("#schedule-view")).to_have_attribute("aria-label", "Month view")
    finally:
        context.close()


def test_a_coordinator_shares_a_view_with_the_department(browser, live_server, h):
    """New view › Share with the department: listed for everyone under Department, changed only by coordinators and admins."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner"); settled(page)
        bar = page.locator(".planner-toolbar")
        toolbar(bar, "Saved views").get_by_role("menuitem", name="New view…", exact=True).click()
        form = page.get_by_role("dialog", name="New view"); form.get_by_label("View name", exact=True).fill("Clinic wall")
        form.get_by_label("Share with the department", exact=True).check()
        save_dialog(form, "Save view")
        expect(toolbar(bar, "Saved views").get_by_role("group", name="Department").get_by_role("menuitemradio", name="Clinic wall")).to_be_visible()
        seen = [v for v in h.get("staff", "/api/table-views?table=planner")["views"] if v["name"] == "Clinic wall"]
        assert seen and seen[0]["scope"] == "department" and seen[0]["can_edit"] is False
    finally:
        context.close()


def test_interactive_schedule_grids_take_clicks(browser, live_server, h):
    """The Timeline's decorative grid lines take no clicks, but the Schedule grids sharing its class must: a duty opens
    and a group folds in the people timeline, and the Counts grid stays in flow and hit-testable."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner"); settled(page)
        bar = page.locator(".planner-toolbar")
        choose_radio(view_settings(bar), "Rows", "People")
        close_popover(page)
        expect(page.locator("#schedule-view")).to_have_attribute("aria-label", "Timeline view")
        grid = page.locator("#schedule-view .tl-grid")
        assert grid.first.evaluate("g => getComputedStyle(g).pointerEvents") != "none"
        toggle = grid.locator(".wk-toggle").first
        expect(toggle).to_have_attribute("aria-expanded", "true")
        chip = grid.locator(".pg-chip").first
        chip.click()  # a real click: fails if something covers the chip or it takes no pointer events
        expect(page.locator(".wk-panel, .dock-panel").first).to_be_visible(timeout=10000)
        page.keyboard.press("Escape")
        page.locator("#schedule-view .tl-grid .wk-toggle").first.click()
        expect(page.locator("#schedule-view .tl-grid .wk-toggle").first).to_have_attribute("aria-expanded", "false")
        # Counts: duty rows by count, the same grid class, laid out in the page and clickable.
        panel = view_settings(bar)
        choose_radio(panel, "Rows", "Duties")
        choose_radio(panel, "Cells show", "Counts")
        close_popover(page)
        counts = page.locator("#schedule-view .ct-grid")
        expect(counts).to_be_visible()
        style = counts.evaluate("g => [getComputedStyle(g).position, getComputedStyle(g).pointerEvents]")
        assert style[0] != "absolute" and style[1] != "none", style
        counts.locator(".pg-group-head").first.click()  # hit-testable: nothing decorative intercepts it
    finally:
        context.close()


def test_a_saved_view_changed_only_in_its_or_filter_offers_update(browser, live_server, h):
    """a saved view whose only change is its Or (nonplain) filter reads as changed: the menu
    names it "(changed)" and View settings offers Update view, not "Saved to this view"."""
    from .test_browser_qa_planner import checked_rota, settled
    checked_rota(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner"); settled(page)
        bar = page.locator(".planner-toolbar")
        toolbar(bar, "Saved views").get_by_role("menuitem", name="New view…", exact=True).click()
        form = page.get_by_role("dialog", name="New view"); form.get_by_label("View name", exact=True).fill("Plain")
        save_dialog(form, "Save view")
        plain = h.get("coordinator", "/api/table-views?table=planner")["views"][0]["definition"]
        anyone = {"match": "any", "conditions": [{"field": "person_ids", "values": [p]} for p in h.seed["people"][:3]]}
        h.command("coordinator", "table_view.create", {"table_key": "planner", "name": "Any of three", "definition": {**plain, "conditions": anyone}})
        page.reload(); settled(page)
        toolbar(bar, "Saved views").get_by_role("menuitemradio", name=re.compile(r"^Any of three"), exact=False).click()
        close_popover(page)
        expect(bar.get_by_role("button", name="Saved views: Any of three", exact=True)).to_be_visible()
        pop = toolbar(bar, "Filter")
        pop.get_by_role("button", name="Filter actions").first.click()
        page.get_by_role("menuitem", name="Remove", exact=True).click()
        close_popover(page)
        expect(bar.get_by_role("button", name="Saved views: Any of three (changed)", exact=True)).to_be_visible()
        expect(view_settings(bar).get_by_role("button", name="Update view", exact=True)).to_be_visible()
    finally:
        context.close()


def test_list_default_views_personal_then_department(browser, live_server, h):
    """Saved views menu on any list (People here): sections Built-in · Department · Mine, a badge on the default, Set as
    my default / department default, Use department default with Undo; a reload opens the default."""
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        bar = page.locator(".dt-toolbar").first
        toolbar(bar, "Saved views").get_by_role("menuitem", name="New view…", exact=True).click()
        form = page.get_by_role("dialog", name="New view"); form.get_by_label("View name", exact=True).fill("Team list")
        form.get_by_label("Share with the department", exact=True).check()
        save_dialog(form, "Save view")
        menu = toolbar(bar, "Saved views")
        expect(menu.get_by_role("group", name="Department").get_by_role("menuitemradio", name="Team list")).to_be_visible()
        with page.expect_response(lambda r: r.url.endswith("/api/commands") and r.request.method == "POST"
                                  and (r.request.post_data_json or {}).get("kind") == "table_view.set_default"
                                  and (r.request.post_data_json or {}).get("payload", {}).get("table_key") == "people"
                                  and (r.request.post_data_json or {}).get("payload", {}).get("scope") == "department") as defaulted:
            menu.get_by_role("menuitem", name="Set as department default", exact=True).click()
        assert defaulted.value.status == 200, defaulted.value.text()
        expect(toolbar(bar, "Saved views").get_by_role("menuitemradio", name="Team list Department default")).to_be_visible()
        # Back to the built-in view and make that the coordinator's own default: it wins over the department's.
        toolbar(bar, "Saved views").get_by_role("menuitemradio", name="Default view", exact=True).click()
        with page.expect_response(lambda r: r.url.endswith("/api/commands") and r.request.method == "POST"
                                  and (r.request.post_data_json or {}).get("kind") == "table_view.set_default"
                                  and (r.request.post_data_json or {}).get("payload") == {"table_key": "people", "scope": "personal", "view_id": None}) as defaulted:
            toolbar(bar, "Saved views").get_by_role("menuitem", name="Set as my default", exact=True).click()
        assert defaulted.value.status == 200, defaulted.value.text()
        assert h.get("coordinator", "/api/table-views?table=people")["defaults"]["personal_set"] is True
        page.reload()
        expect(bar.locator(".dt-saved .dt-tool-label")).to_have_text("Default view")
        with page.expect_response(lambda r: r.url.endswith("/api/commands") and r.request.method == "POST"
                                  and (r.request.post_data_json or {}).get("kind") == "table_view.set_default"
                                  and (r.request.post_data_json or {}).get("payload") == {"table_key": "people", "scope": "personal", "clear": True}) as defaulted:
            toolbar(bar, "Saved views").get_by_role("menuitem", name="Use department default", exact=True).click()
        assert defaulted.value.status == 200, defaulted.value.text()
        with page.expect_response(lambda r: r.url.endswith("/api/commands") and r.request.method == "POST"
                                  and (r.request.post_data_json or {}).get("kind") == "table_view.set_default"
                                  and (r.request.post_data_json or {}).get("payload") == {"table_key": "people", "scope": "personal", "view_id": None, "clear": False}) as undone:
            page.get_by_role("button", name="Undo", exact=True).click()
        assert undone.value.status == 200, undone.value.text()
        expect(page.get_by_role("status").get_by_text("Undone. The previous values are back.", exact=True)).to_be_visible()
        # Undone: the coordinator's own choice is back (and can be cleared again).
        with page.expect_response(lambda r: r.url.endswith("/api/commands") and r.request.method == "POST"
                                  and (r.request.post_data_json or {}).get("kind") == "table_view.set_default"
                                  and (r.request.post_data_json or {}).get("payload") == {"table_key": "people", "scope": "personal", "clear": True}) as defaulted:
            toolbar(bar, "Saved views").get_by_role("menuitem", name="Use department default", exact=True).click()
        assert defaulted.value.status == 200, defaulted.value.text()
        page.reload()
        expect(bar.locator(".dt-saved .dt-tool-label")).to_have_text("Team list")
    finally:
        context.close()
    # Staff follow the department default and cannot change it.
    context, page = sign_in(browser, live_server, "staff")
    try:
        navigate(page, "people")
        bar = page.locator(".dt-toolbar").first
        expect(bar.locator(".dt-saved .dt-tool-label")).to_have_text("Team list")
        expect(toolbar(bar, "Saved views").get_by_role("menuitem", name="Set as department default")).to_have_count(0)
    finally:
        context.close()
# Include this file in hosted supervision and team-lead qualification.

"""Redesigned shell and planner journeys. Real Chromium, real API and migrated database; no mocks."""
from packages.domain.time import now as clock_now
import re
import pytest
from playwright.sync_api import expect
from .governance_support import developer
from .test_browser import browser, live_server, sign_in, navigate, open_scheduling, pick_view, show_names, open_session, planner_filter, toolbar, close_popover, open_config, open_rota, view_settings, no_opening_hours, wait_run_created  # noqa: F401
from .rules_support import rule_row

pytestmark = pytest.mark.browser
ACCOUNT = re.compile(r"^Account menu, ")


def rota(h):
    people = h.seed["people"]
    w = h.workspace("Redesign rota", "2026-10-04", "2026-10-11")
    for day in range(4, 8):
        staff = [] if day == 5 else [people[day % len(people)]]
        w = h.shift(w, f"2026-10-{day:02d}T08:00:00+03:00", f"2026-10-{day:02d}T12:00:00+03:00", people=staff)["workspace"]
    w = h.shift(w, "2026-10-06T13:00:00+03:00", "2026-10-06T17:00:00+03:00", people=[people[1]], location=1)["workspace"]
    return h.lifecycle(w, "validate")["workspace"]


def test_theme_account_menu_palette_and_sign_out(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        account = page.get_by_role("button", name=ACCOUNT)
        account.click()
        menu = page.get_by_role("dialog", name="Account")
        menu.get_by_role("menuitemradio", name="Dark").click()
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        page.keyboard.press("Escape")
        expect(menu).to_have_count(0)
        expect(account).to_be_focused()
        page.reload()
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        # The theme is applied before the app loads; shortcuts work once the page has loaded (CI pressed Ctrl+K too early).
        expect(page.locator("#main[data-loaded-view]")).to_have_count(1)
        page.keyboard.press("Control+k")
        palette = page.get_by_role("dialog", name="Command menu")
        expect(palette).to_be_visible()
        palette.get_by_role("combobox").fill("schedule")
        page.keyboard.press("Enter")
        expect(palette).to_have_count(0)
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "schedule")
        page.keyboard.press("?")
        expect(page.get_by_role("dialog", name="Keyboard shortcuts")).to_be_visible()
        page.keyboard.press("Escape")
        page.get_by_role("button", name=ACCOUNT).click()
        page.get_by_role("menuitem", name="Sign out", exact=True).click()
        expect(page.get_by_label("Username or email", exact=True)).to_be_visible()
        # Only who signed in last (for the sign-in page) and when this device signed out (for its other tabs, maintenance).
        assert sorted(page.evaluate("Object.keys(localStorage)")) == ["fmuc.signed-out-at", "rota.account"]
        expect(page.locator("html")).not_to_have_attribute("data-theme", "dark")
    finally:
        context.close()


def test_sidebar_rail_pin_and_no_horizontal_scroll(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator")

    def title_uncovered():
        # Neither the rail nor the pinned sidebar covers the page: the title's first letter is its own.
        title = page.locator(".topbar h1").first
        expect(title).to_be_visible()
        # After a peek the rail narrows with a CSS transition: measured once it has settled at its own width.
        side = page.locator(".sidebar")
        expect(side).to_have_css("width", side.evaluate(
            "n => getComputedStyle(n).getPropertyValue(n.closest('.shell[data-nav=pinned]') ? '--nav-w' : '--rail-w').trim()"))
        assert title.evaluate("h => {const r=h.getBoundingClientRect();return h.contains(document.elementFromPoint(r.left+2,(r.top+r.bottom)/2));}")

    try:
        shell = page.locator(".shell")
        sidebar = page.locator(".sidebar")
        expect(shell).to_have_attribute("data-nav", "rail")
        title_uncovered()
        page.mouse.move(1000, 600)
        page.keyboard.press("Tab")
        page.get_by_role("button", name="Search or jump to", exact=True).focus()
        expect(sidebar).to_have_class(re.compile(r"\bpeek\b"))
        page.locator("#main").focus()
        expect(sidebar).not_to_have_class(re.compile(r"\bpeek\b"))
        sidebar.hover(); page.clock.run_for(500)
        expect(sidebar).to_have_class(re.compile(r"\bpeek\b"))
        # The peek widens the sidebar (a CSS transition): click Pin once it has settled at its full width, so the
        # click lands on the button where it ends up, not where it is mid-animation (a flake under load).
        expect(sidebar).to_have_css("width", sidebar.evaluate("n => getComputedStyle(n).getPropertyValue('--nav-w').trim()"))
        page.get_by_role("button", name="Pin sidebar open").click()
        expect(shell).to_have_attribute("data-nav", "pinned")
        page.mouse.move(1000, 600)
        title_uncovered()
        for view in ("overview", "planner", "timeline", "people", "rules", "reports"):
            navigate(page, view)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), view
        page.reload()
        expect(page.locator("#main[data-loaded-view]")).to_have_count(1)
        expect(shell).to_have_attribute("data-nav", "pinned")
        page.get_by_role("button", name="Unpin sidebar", exact=True).click()
        expect(shell).to_have_attribute("data-nav", "rail")
        page.mouse.move(1000, 600)
        sidebar.hover(); page.clock.run_for(500)
        expect(sidebar).to_have_class(re.compile(r"\bpeek\b"))
        page.mouse.move(1000, 600)
        expect(sidebar).not_to_have_class(re.compile(r"\bpeek\b"))
        title_uncovered()
    finally:
        context.close()


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("route,tab", [("insights", "Workload"), ("system", "Operations")])
def test_rail_navigation_leaves_the_destination_tabs_accessible(browser, live_server, h, width, theme, route, tab):
    # Operations is a Developer tab: the owner's Admin + Developer account reaches both destinations.
    context, page = sign_in(browser, live_server, developer(h), {"width": width, "height": 1000})
    try:
        page.emulate_media(color_scheme=theme)
        sidebar = page.get_by_role("complementary", name="Navigation")
        expect(page.locator(".shell")).to_have_attribute("data-nav", "rail")
        if width == 390:
            page.get_by_role("button", name="Menu", exact=True).click()
        else:
            # Start outside the rail. After the navigation click, the pointer stays where it clicked.
            page.mouse.move(1000, 600)
            expect(sidebar).not_to_have_class(re.compile(r"\bpeek\b"))
        nav = page.get_by_role("navigation", name="Department pages", exact=True)
        nav.locator(f'[data-route="{route}"]').click(position={"x": 20, "y": 16})
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", route)
        # Advance the real hover delay and width transition without moving the pointer away to dismiss the rail.
        page.clock.run_for(700)
        target = page.get_by_role("tablist", name=route.capitalize(), exact=True).get_by_role("tab", name=tab, exact=True)
        expect(target).to_be_visible()
        assert target.evaluate("b => {const r=b.getBoundingClientRect();return b.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}"), "The destination tab is covered after sidebar navigation"
        target.click()
        expect(target).to_have_attribute("aria-selected", "true")
        expect(page.locator("#main")).to_have_attribute("data-loaded-tab", tab.lower())
        if width == 390:
            expect(sidebar).not_to_be_visible()
            expect(page.get_by_role("button", name="Menu", exact=True)).to_have_attribute("aria-expanded", "false")
    finally:
        context.close()


def test_planner_week_grid_groups_chips_hover_views_names_and_drag(browser, live_server, h):
    w = rota(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner")
        grid = page.get_by_role("grid", name=re.compile(r"^Sessions grouped by service"))
        expect(grid).to_be_visible()
        expect(grid.locator(".wk-day").first).to_contain_text("/")  # places filled / needed
        toggle = grid.locator(".wk-toggle").first
        toggle.click()
        toggle = page.locator(".wk-toggle").first
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(page.locator(".wk-sum").first).to_be_visible()
        toggle.click()
        expect(page.locator(".wk-toggle").first).to_have_attribute("aria-expanded", "true")
        expect(grid.locator(".pchip.gap")).to_have_count(1)  # the legend's open-place key is drawn as a gap chip too
        filters = planner_filter(page)
        filters.get_by_label("Only sessions with open places", exact=True).check()
        expect(page.locator(".wk-rowhead")).to_have_count(1)
        filters.get_by_label("Only sessions with open places", exact=True).uncheck()
        expect(page.locator(".wk-rowhead")).to_have_count(2)
        close_popover(page)
        # A7: the footer's health pill opens the Fix list's must-fix cause: the open place on 5 Oct, and nothing else.
        must = [c for c in h.get("coordinator", f"/api/workspaces/{w['id']}/fix-list")["causes"] if c["level"] == "must"]
        gap = next(s["id"] for s in h.detail(w)["shifts"] if not s["participants"])
        assert len(must) == 1 and gap in must[0]["shift_ids"], must
        page.locator(".app-footer").get_by_role("button", name="1 must fix · open the fix list", exact=True).click()
        issues = page.get_by_role("dialog", name="Rota health")
        # The one issue list (4c8b40be3): the must-fix cause, one item per session it names, and nothing else.
        items = issues.locator("li.finding, li.iss-row")  # a card, or a row for an issue with no card fields (issue_marker.ts)
        expect(items).to_have_count(max(1, len(must[0]["shift_ids"])))
        expect(items.first).to_contain_text(must[0]["sentence"])
        page.keyboard.press("Escape")
        chip = page.locator(".pchip:not(.gap)").first
        chip.hover()
        tip = page.get_by_role("tooltip")
        expect(tip).to_contain_text("Click to open")
        # Why here: the server's score effect for this person on this session (the Why tab's answer).
        expect(tip.get_by_role("list", name="Why here")).to_contain_text("with them here")
        page.mouse.move(700, 20)
        chip.click()
        panel = page.get_by_role("dialog").filter(has_text="Working this session")
        expect(panel).to_be_visible()
        expect(panel.get_by_role("list", name="Rules applied to this assignment")).to_be_visible()
        panel.get_by_role("button", name="Close panel").click()
        expect(panel).to_have_count(0)
        pick_view(page,'Coverage')
        expect(page.get_by_role("table", name="Coverage by duty type and day")).to_be_visible()
        expect(page.locator(".cov-cell.short")).to_have_count(1)
        pick_view(page,'People')
        expect(page.get_by_role("grid", name="Duties by person and day")).to_be_visible()
        expect(page.locator(".person-group").first).to_be_visible()
        pick_view(page,'Week')
        show_names(page, "code")
        expect(page.locator(".pchip-name").first).to_have_text(re.compile(r"^[A-Z0-9]{2,6}$"))
        show_names(page, "full")
        people = page.locator(".pchip.draggable")
        people.nth(0).drag_to(people.nth(1))
        swap = page.get_by_role("dialog", name="Exchange two assignments")
        expect(swap).to_be_visible()
        assert swap.get_by_label("First assignment").input_value() != swap.get_by_label("Second assignment").input_value()
    finally:
        context.close()


def test_fill_an_open_place_from_the_grid_in_one_click(browser, live_server, h):
    w = rota(h)
    open_shift = next(s for s in h.detail(w)["shifts"] if not s["participants"])
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner")
        page.locator(".pchip.gap").first.click()
        panel = page.get_by_role("dialog").filter(has_text="Can work this session")
        expect(panel).to_be_visible(timeout=20000)
        first = panel.locator(".wk-cand.ok").first
        name = first.locator("strong").inner_text()
        first.get_by_role("button", name=re.compile(r"^Assign (?!.* with a warning$)")).click()
        expect(page.get_by_role("status").filter(has_text="added to the draft")).to_be_visible(timeout=20000)
        after = next(s for s in h.detail(w)["shifts"] if s["id"] == open_shift["id"])
        assert [p["person_id"] for p in after["participants"]] and name
        page.get_by_role("button", name="How to read this schedule").click()
        expect(page.get_by_role("heading", name="How to read this schedule")).to_be_visible()
    finally:
        context.close()


def test_fill_panel_lists_people_not_available_in_view(browser, live_server, h):
    """F3: who cannot work an open place is a labelled section with its count, shown (not a closed disclosure)."""
    busy = h.seed["people"][0]
    w = h.workspace("Not available rota", "2026-10-04", "2026-10-11")
    w = h.shift(w, "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00", people=[busy])["workspace"]
    w = h.shift(w, "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00", people=[], location=1)["workspace"]
    w = h.lifecycle(w, "validate")["workspace"]
    who = h.snapshot(w)["people"][busy]["display_name"]
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        open_rota(page, w["id"])
        page.locator(".pchip.gap").first.click()
        panel = page.get_by_role("dialog").filter(has_text="Can work this session")
        expect(panel).to_be_visible(timeout=20000)
        blocked = panel.get_by_role("region", name=re.compile(r"^Not available · \d+$"))
        expect(blocked.get_by_role("heading", name=re.compile(r"^Not available · \d+$"))).to_be_visible()
        expect(blocked.locator("li").filter(has_text=who)).to_be_visible()
        expect(panel.locator("details.wk-blocked")).to_have_count(0)
    finally:
        context.close()


def test_rota_calendar_setup_guide_and_publish_summary(browser, live_server, h):
    no_opening_hours(h)
    w = rota(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        # The Year view (the old rota calendar): each day says what needs attention; its panel opens the week.
        navigate(page, "schedule/year/2026-10-05")
        cell = page.locator('.sd-yday[aria-label^="5 Oct 2026:"]')
        expect(cell).to_have_attribute("aria-label", re.compile(r"Redesign rota, draft, 1 session, 1 open place"))
        cell.click()
        panel = page.locator(".sd-panel")
        expect(panel).to_contain_text("1 open place")
        panel.get_by_role("button", name="Open in Week").click()
        expect(page.locator("#schedule-view")).to_have_attribute("aria-label", "Week view")
        expect(page).to_have_url(re.compile(r"#schedule/week/2026-10-05"))
        # Must-fix causes (A5): Submit for review… says so, cannot submit, and points straight at them.
        page.get_by_role("button", name="Submit for review…", exact=True).click()
        dialog = page.get_by_role("dialog", name="Submit Redesign rota for review")
        expect(dialog.locator(".fix-gate")).to_contain_text(re.compile(r"\d+ causes? must be fixed first"))
        expect(dialog.get_by_role("button", name="Submit for review", exact=True)).to_be_disabled()
        causes = dialog.get_by_role("list", name="Must fix").locator("li[data-cause]")
        expect(causes.first).to_be_visible()
        # Show on schedule (a cause's details, R3) closes the dialog and brings its sessions into focus, ringed.
        causes.first.get_by_role("button", name="Show evidence", exact=True).click()
        causes.first.get_by_role("button", name="Show on schedule").click()
        expect(dialog).to_have_count(0)
        expect(page.get_by_role("region", name="Cause in focus")).to_be_visible()
        expect(page.locator("#main .cause-ring").first).to_be_visible()
    finally:
        context.close()
    context, page = sign_in(browser, live_server, "admin")
    try:
        navigate(page,"configuration/setup")
        expect(page.get_by_role("img", name=re.compile(r"of \d+ required steps complete"))).to_be_visible()
        page.get_by_role("button", name="Set opening hours").click()
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "configuration")
        expect(page.get_by_role("navigation", name="Configuration sections").get_by_role("button", name="Opening hours", exact=True)).to_have_attribute("aria-current", "page")
    finally:
        context.close()


def test_staff_phone_next_duties_and_menu(browser, live_server, h):
    me = h.get("staff", "/api/me")
    assert me.get("person_id") == h.seed['people'][0], 'Seeded staff account must be linked to its person'
    from datetime import timedelta
    start = clock_now().date() + timedelta(days=1); duty = start + timedelta(days=2)
    w = h.workspace("Staff published rota", start.isoformat(), (start + timedelta(days=7)).isoformat())
    w = h.shift(w, f"{duty}T08:00:00+03:00", f"{duty}T12:00:00+03:00", people=[me["person_id"]])["workspace"]
    h.publish(w)
    context, page = sign_in(browser, live_server, "staff", viewport={"width": 390, "height": 844})
    try:
        navigate(page,"home")
        expect(page.get_by_role("heading", name="Coming up")).to_be_visible()
        expect(page.locator(".home-hero")).to_contain_text("Next duty · in")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        page.get_by_role("button", name="Menu", exact=True).click()
        expect(page.get_by_role("button", name="Schedule", exact=True)).to_be_visible()
    finally:
        context.close()


def test_a_saved_steps_preference_still_shows_all_on_one_page(browser, live_server, h):
    """Forms have one layout reviewed: an old saved step-by-step preference changes nothing."""
    context, page = sign_in(browser, live_server, 'admin')
    try:
        page.evaluate("localStorage.setItem('rota.prefs', JSON.stringify({forms:'steps'}))")
        page.reload()
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add rule')
        dialog.get_by_role('radio', name=re.compile('^Minimum rest')).check()
        expect(dialog.get_by_label('Rule title', exact=True)).to_be_visible()
        expect(dialog.get_by_role('button', name='Propose rule')).to_be_visible()
        expect(dialog.get_by_role('button', name='Next', exact=True)).to_be_hidden()
        expect(dialog.get_by_role('radiogroup', name='Form layout')).to_have_count(0)
        assert 'forms' not in page.evaluate("JSON.parse(localStorage.getItem('rota.prefs') || '{}')")
    finally:
        context.close()


def test_codes_tags_rules_applied_and_proposal_overlay(browser, live_server, h):
    from apps.worker.main import process_one
    people = h.get("admin", "/api/catalogues")["people"]
    first = people[0]
    base = {k: first[k] for k in ("display_name", "active", "title", "staff_number", "directory_section", "directory_order")}
    h.configure("people", {**base, "short_code": "ZZQ", "tags": ["Locum"]}, first["id"], first["revision"])
    rule = h.policy({"kind": "min_rest", "hours": 12})
    w = h.shift(h.workspace("Overlay rota", "2026-10-04", "2026-10-11"), people=[first["id"]])["workspace"]
    w = h.shift(w, "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00", people=[])["workspace"]
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        row = page.get_by_role("row").filter(has_text=first["display_name"])
        expect(row.get_by_label("Short code ZZQ")).to_be_visible()
        expect(row).to_contain_text("Locum")
        codes = page.evaluate("""async () => { const {autoCode, uniqueCodes} = await window.importBundle('dom.js');
            const m = uniqueCodes([{id:'a',display_name:'Pavel Cooper'},{id:'b',display_name:'Rana Alfarsi'},{id:'c',display_name:'Z',short_code:'PCO'}]);
            return [autoCode('Olivia Jensen'), autoCode('Ivo Allen'), m.get('a'), m.get('b'), m.get('c')]; }""")
        assert codes[:2] == ['OJE', 'IAL'] and len(set(codes[2:])) == 3 and codes[4] == 'PCO'
        page.get_by_label("Search people", exact=True).fill("zzq")
        expect(page.get_by_role("row")).to_have_count(2)
        navigate(page, "planner")
        open_scheduling(page)
        open_rota(page, w["id"])
        show_names(page, "code")
        expect(page.locator(".pchip-name").filter(has_text="ZZQ")).to_have_count(1)
        show_names(page, "full")
        page.locator(".pchip").filter(has_text=first["display_name"]).click()
        sheet = page.get_by_role("dialog").filter(has_text="Rules applied here")
        expect(rule_row(sheet, rule["name"])).to_be_visible()
        # One session panel (S-09): the session's own view is a mode of the same panel, not a second one.
        sheet.get_by_role("button", name="Whole session").click()
        sheet = page.locator(".wk-panel")
        expect(sheet.get_by_role("heading", name="People")).to_be_visible()
        expect(page.locator(".dock-panel")).to_have_count(0)
        sheet.get_by_role("button", name="Close panel").click()
        open_scheduling(page)  # opening a session panel closed it
        page.get_by_role("button", name="Run auto-schedule", exact=True).click()
        expect(page.locator("#auto-panel").get_by_role("progressbar")).to_be_visible()  # drawn as soon as Run is pressed
        wait_run_created(page)  # the worker can only take the run once job.create has answered
        process_one(h.app.state.engine)
        show = page.get_by_role("button", name="Show proposal on grid", exact=True)
        expect(show).to_be_visible(timeout=15000)
        show.click()
        expect(page.locator(".proposal-bar")).to_contain_text("to add")
        expect(page.locator(".pchip.proposed-add").first).to_be_visible()
        page.get_by_role("button", name="Dismiss", exact=True).click()
        expect(page.locator(".proposal-bar")).to_have_count(0)
    finally:
        context.close()


def test_session_editor_tabs_suggest_staff_history_and_live_impact(browser, live_server, h):
    w = rota(h)
    shift = next(s for s in h.detail(w)["shifts"] if s["participants"])
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner")
        open_session(page, shift["type_snapshot"]["name"])
        page.get_by_role("button", name="Edit session", exact=True).click()
        sheet = page.get_by_role("region", name="Edit session and participants")
        expect(sheet.get_by_text("Not saved yet: review what this changes", exact=False)).to_be_visible(timeout=20000)  # live, no button press
        sheet.get_by_role("tab", name="History").click()
        expect(sheet.get_by_text("Session created", exact=True).first).to_be_visible()
        sheet.get_by_role("tab", name="Checks").click()
        expect(sheet.get_by_role("heading", name="Rules applied")).to_be_visible()
        sheet.get_by_role("tab", name="Suggested staff").click()
        expect(sheet.get_by_role("heading", name="Best matches")).to_be_visible(timeout=20000)
        before = sheet.locator(".nested-card").count()
        saved = h.detail(w)["shifts"]
        sheet.locator(".sheet-panel .wk-cand").first.get_by_role("button", name="Add", exact=True).click()
        expect(sheet.get_by_role("tab", name="Session")).to_have_attribute("aria-selected", "true")
        expect(sheet.locator(".nested-card")).to_have_count(before + 1)
        assert h.detail(w)["shifts"] == saved  # adding a suggestion saves nothing
    finally:
        context.close()


def test_department_display_defaults_apply_until_someone_picks_their_own(browser, live_server, h):
    rota(h)
    context, page = sign_in(browser, live_server, "admin")
    try:
        navigate(page, "configuration")
        page.get_by_role("button", name="Names & codes").click()
        bar = page.get_by_role("region", name="Unsaved changes")
        save = bar.get_by_role("button", name="Save defaults", exact=True)
        expect(bar).to_be_hidden()  # the one save control appears once something changes
        page.get_by_role("radiogroup", name="Show names as").get_by_text("Short code").click()
        expect(save).to_be_enabled()
        save.click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Reason", exact=True).fill("Agreed department display")
        dialog.get_by_role("button", name="Save defaults", exact=True).click()
        expect(page.get_by_text("Department display defaults saved.")).to_be_visible()
    finally:
        context.close()
    settings = h.get("staff", "/api/me")["department"]["display_settings"]
    assert settings["names_default"] == "code"
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner")
        expect(page.locator(".pchip-name").first).to_have_text(re.compile(r"^[A-Z0-9]{2,6}$"))
        # The department default is the view's Person label until the view chooses its own.
        panel = view_settings(page.locator(".planner-toolbar"), "Properties")
        panel.locator(".vs-row", has_text="Person label").click()
        names = panel.get_by_role("radiogroup", name="Name", exact=True)
        expect(names.get_by_role("radio", name="Short code", exact=True)).to_have_attribute("aria-checked", "true")
        expect(panel.locator('.vs-prop[data-part]:not([data-part="title"])')).to_have_count(0)  # nothing added to the code
    finally:
        context.close()


def test_planner_shortcuts_open_the_canonical_forms_prefilled(browser, live_server, h):
    w = rota(h)
    person = next(p["person_id"] for s in h.detail(w)["shifts"] for p in s["participants"])
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner")
        monday = page.locator(".wk-day").nth(1)
        expect(monday).to_contain_text("5")
        monday.hover()
        monday.locator(".wk-more").click()
        page.get_by_role("menuitem", name="Add session on this day").click()
        dialog = page.get_by_role("region", name="Add session")
        expect(dialog.get_by_label("Session start", exact=True)).to_have_value("Mon 5 Oct 2026")
        page.keyboard.press("Escape")
        monday.click(button="right")
        page.get_by_role("menuitem", name="Add a rule for Mondays").click()
        rule = page.get_by_role("dialog", name="Add rule")
        days = rule.get_by_role("group", name="Weekdays", exact=True)
        expect(days.get_by_label("Monday")).to_be_checked()
        expect(days.get_by_label("Sunday")).not_to_be_checked()
        page.keyboard.press("Escape")
        # Duty rows: coordinators can add and propose, never edit admin catalogues from here.
        page.locator(".wk-rowhead").first.click(button="right")
        expect(page.get_by_role("menuitem", name="Add this session on another day")).to_be_visible()
        expect(page.get_by_role("menuitem", name="Edit duty type")).to_have_count(0)
        page.keyboard.press("Escape")
        page.locator(".pchip:not(.gap):not(.empty)").first.click(button="right")
        page.get_by_role("menuitem", name="Record leave").click()
        leave = page.get_by_role("dialog", name="Record leave")
        name = next(p["display_name"] for p in h.get("coordinator", "/api/catalogues")["people"] if p["id"] == person)
        expect(leave.locator(".pick-field, fieldset.multi-field").filter(has_text="People").first).to_contain_text(name)
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_rule_line_keeps_the_last_included_day_and_saves_exclusive_end(browser, live_server, h, width):
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add rule', exact=True)
        dialog.get_by_role('radio', name='Maximum number of duties', exact=True).check()
        dialog.get_by_role('button', name='Add a person', exact=True).click()
        row = dialog.locator('.pp-row')
        row.get_by_label('Person', exact=True).select_option(h.seed['people'][0])
        row.get_by_label('Maximum duties', exact=True).fill('3')
        row.get_by_label('From', exact=True).fill('2026-10-04')
        row.get_by_label('Until (included)', exact=True).fill('2026-10-17')
        dialog.get_by_label('Rule title', exact=True).fill('Included line dates')
        dialog.get_by_label('Policy owner', exact=True).fill('Synthetic owner')
        dialog.get_by_label('Source or departmental decision', exact=True).fill('Synthetic date review witness')
        dialog.get_by_label('Effective from', exact=True).fill('2026-10-01')
        expect(row.get_by_label('Until (included)', exact=True)).to_have_value('Sat 17 Oct 2026')
        dialog.get_by_role('button', name='Propose rule', exact=True).click()
        expect(dialog).to_have_count(0)
        saved = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['name'] == 'Included line dates')
        assert saved['definition']['per_person'][0]['starts_on'] == '2026-10-04'
        assert saved['definition']['per_person'][0]['ends_on'] == '2026-10-18'
    finally:
        context.close()

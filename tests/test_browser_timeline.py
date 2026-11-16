"""The shared Timeline layout (apps/web/src/timeline.ts) on Programmes › Rotation board (approved mockup, 21 Oct 2026):
conflicts, time away, hover card, side panel and keyboard, layers and zoom in View settings, saved views, the phone
agenda, loading/empty/filtered states, sticky header, and 150 trainees × a year drawn only where they are seen.
Fixtures are fictional: made through the API, or (the department-scale board) an invented board answer."""
import json
import os
import re
import time

import pytest
from playwright.sync_api import expect

from .legend_guard import assert_legend_matches

from .test_browser import browser, close_popover, expect_loaded, live_server, navigate, sign_in  # noqa: F401
from .test_browser_operator_role_coverage import all_roles  # noqa: F401
from .test_browser_rotation_board import residency
from .test_browser_sticky import Sticky

pytestmark = pytest.mark.browser
SHOTS = os.getenv("TIMELINE_SHOTS")


def shot(page, name):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, name + ".png"))


def board_with_conflict_and_time_away(h):
    """The residency, plus an overlapping rotation, leave (after the planned rotations: leave during one follows its type's
    setting) and protected time for the first trainee."""
    from datetime import date
    from apps.api import models as m
    from apps.api.db import transaction
    p, cohort, types, (trainee, local, visitor), planned = residency(h)
    # The API refuses overlapping rotations (ROTATION_OVERLAP); older or imported records can still overlap.
    with transaction(h.app.state.engine) as db:
        source = db.get(m.Rotation, next(r["id"] for r in planned if r["person_id"] == trainee))
        copy = {c.key: getattr(source, c.key) for c in m.Rotation.__table__.columns if c.key != "id"}
        db.add(m.Rotation(**{**copy, "rotation_type_id": types[2]["id"], "starts_on": date(2026, 10, 20), "ends_on": date(2026, 11, 10)}))
    h.command("coordinator", "availability.record", {"person_id": trainee, "kind": "recorded_leave", "start": "2027-01-15T00:00:00+03:00",
                                                     "end": "2027-01-22T00:00:00+03:00", "private_reason": "Fictional leave"})
    h.command("coordinator", "availability.record", {"person_id": trainee, "kind": "protected_time", "start": "2026-12-06T08:00:00+03:00",
                                                     "end": "2026-12-06T12:00:00+03:00", "private_reason": "Fictional study morning"})
    return types, trainee


def view_settings(page):
    page.get_by_role("button", name=re.compile("^View settings")).click()
    return page.locator(".dt-pop")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_board_shows_conflicts_time_away_hover_card_and_panel(browser, live_server, all_roles, scheme):
    h = all_roles
    types, trainee = board_with_conflict_and_time_away(h)
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, "programmes/board")
        board = page.get_by_role("region", name="Rotation timeline")
        row = board.locator(f'.tl-row[data-row="{trainee}"]')
        # Two rotations that overlap: a red zone with its badge (icon and words), and the row's flag.
        expect(row.locator(".tl-zbadge")).to_have_text("Overlap · 21 d")
        # The row's flag is the shared issue marker (a83c1ad4c): its button names the person and the must-fix.
        expect(row.locator(".tl-who").get_by_role("button", name=re.compile(r"\. Must fix: Two rotations overlap$"))).to_be_visible()
        expect(page.locator(".app-footer").get_by_text("1 conflict")).to_be_visible()
        # Time away is a block in the rotation lane, never red: leave a neutral block with a plane, protected time with a book.
        leave = row.get_by_role("img", name=re.compile(r", 15 Jan – 21 Jan 2027$"))
        expect(leave).to_have_class(re.compile(r"\btl-leave\b"))
        expect(leave.locator('[data-icon="plane"]')).to_have_count(1)
        expect(row.locator(".tl-protected [data-icon=book]")).to_have_count(1)
        assert "rgb(180, 35, 24)" not in leave.evaluate("n => getComputedStyle(n).backgroundColor")
        # The shared key above the board (dom.ts legend): what it draws, leave and protected time among them; no arrows.
        key = page.locator(".rb-page > .view-legend.tl-legend")
        expect(key).to_be_visible()
        for words in ("Leave", "Academic & protected time"):
            expect(key.locator("li").filter(has_text=words)).to_have_count(1)
        expect(key.locator("[data-icon=chevronDown], [data-icon=chevronRight]")).to_have_count(0)
        assert_legend_matches(page, root=".rb-page", scrollers=[".tl"], expect_marks=["mark:leave", "mark:protected", "zone:error"])
        # Hover card: what the bar is, its dates and type.
        bar = row.get_by_role("button", name=re.compile(r"^Emergency, 1 Oct – 31 Oct 2026"))
        bar.hover()
        card = page.get_by_role("tooltip")
        expect(card).to_contain_text("Click to open")
        expect(card).to_contain_text("Internal: duties allowed when eligible")
        shot(page, f"board-hover-{scheme}")
        page.mouse.move(5, 5)
        # The panel: its conflict card with the recommended fix, ↑ ↓ through this trainee's rotations.
        bar.click()
        panel = page.locator(".dock-panel")
        expect(panel.get_by_role("heading", name=re.compile(r"^Overlaps Clinic · \d+ days$"))).to_be_visible()
        expect(panel.locator(".rb-card.err").get_by_role("button", name="Change dates")).to_have_class(re.compile("recommended"))
        expect(bar).to_have_class(re.compile("is-explained"))
        shot(page, f"board-panel-{scheme}")
        panel.get_by_role("button", name=re.compile("^Next rotation")).click()
        expect(panel.locator("h2")).to_have_text("Clinic")
        # The pointer resting on ↓ opens its tooltip, and Escape closes that first (WCAG 1.4.13): move off and let it go.
        page.mouse.move(5, 5)
        expect(page.get_by_role("tooltip")).to_have_count(0)
        page.keyboard.press("Escape")
        expect(panel).to_have_count(0)
    finally:
        context.close()


def test_view_settings_layers_zoom_density_and_collapse(browser, live_server, all_roles):
    h = all_roles
    types, trainee = board_with_conflict_and_time_away(h)
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        navigate(page, "programmes/board")
        board = page.get_by_role("region", name="Rotation timeline")
        expect(board.locator(".tl-leave")).to_have_count(1)
        pop = view_settings(page)
        # Layers are the legend: a swatch and a count each; conflicts are always shown.
        expect(pop.get_by_role("group", name="Layers")).to_contain_text("Always shown")
        shot(page, "board-view-settings")
        pop.get_by_role("checkbox", name="Leave", exact=True).uncheck()
        expect(board.locator(".tl-leave")).to_have_count(0)
        expect(board.locator(".tl-zone")).to_have_count(1)
        # Week zoom: 13 weeks, by week, weekends shaded; the period moves a week at a time.
        page.locator(".dt-pop").get_by_role("radiogroup", name="Range").get_by_role("radio", name="3 months").click()
        board = page.get_by_role("region", name="Rotation timeline")
        expect(board.locator(".tl-weekend")).to_have_count(13)
        # Gridlines, weekends and Today are one click-through layer behind the rows (its own class: the Schedule's
        # Timeline grid is a different component, S-14).
        layer = board.locator(".rb-tl-grid")
        expect(layer.locator(".tl-line").first).to_be_attached()
        assert layer.evaluate("g => [getComputedStyle(g).position, getComputedStyle(g).pointerEvents, g.getBoundingClientRect().height > 24]") == ["absolute", "none", True]
        expect(page.locator(".rb-nav strong")).to_have_text("27 Sep 2026 – 26 Dec 2026")
        close_popover(page)
        page.get_by_role("button", name="Next", exact=True).click()
        expect(page.locator(".rb-nav strong")).to_have_text("4 Oct 2026 – 2 Jan 2027")
        pop = view_settings(page)
        pop.get_by_role("radiogroup", name="Row density").get_by_role("radio", name="Compact").click()
        expect(page.locator(".tl.tl-compact")).to_have_count(1)
        close_popover(page)
        # A group collapses to its header (with its count) and opens again.
        toggle = page.get_by_role("button", name=re.compile("^Fictional residency"))
        toggle.click()
        expect(page.locator(".tl-row")).to_have_count(0)
        expect(page.get_by_role("button", name=re.compile("^Fictional residency"))).to_have_attribute("aria-expanded", "false")
        page.reload()
        expect_loaded(page, "programmes/board")
        expect(page.locator(".tl-row")).to_have_count(0)  # kept
        page.get_by_role("button", name=re.compile("^Fictional residency")).click()
        expect(page.locator(".tl-row")).to_have_count(3)
        expect(page.locator(".tl-leave")).to_have_count(0)  # the layer stays off
    finally:
        context.close()


def test_saved_view_keeps_the_timeline_settings(browser, live_server, all_roles):
    h = all_roles
    board_with_conflict_and_time_away(h)
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        navigate(page, "programmes/board")
        pop = view_settings(page)
        pop.get_by_role("radiogroup", name="Range").get_by_role("radio", name="6 months").click()
        page.locator(".dt-pop").get_by_role("checkbox", name="Leave", exact=True).uncheck()
        page.locator(".dt-pop").get_by_label("Bar label", exact=True).select_option("dates")
        page.locator(".dt-pop").get_by_label("Colour rotations by", exact=True).select_option("cohort")
        close_popover(page)
        page.get_by_role("button", name=re.compile("^Saved views")).click()
        page.get_by_role("menuitem", name="New view…").click()
        dialog = page.get_by_role("dialog", name="New view")
        dialog.get_by_label("View name", exact=True).fill("Fictional planning view")
        dialog.get_by_role("button", name="Save view").click()
        expect(page.get_by_role("button", name=re.compile("^Saved views: Fictional planning view"))).to_be_visible()
        [saved] = h.get("programme_lead", "/api/table-views?table=rotation-board")["views"]
        assert saved["definition"]["layout"] == "timeline"
        assert (saved["definition"]["timeline"]["zoom"], saved["definition"]["timeline"]["hidden"]) == ("month", ["leave"])
        assert (saved["definition"]["timeline"]["bar_label"], saved["definition"]["timeline"]["colour_by"]) == ("dates", "cohort")
        # Back to the default, then the saved view brings its zoom and layers back.
        page.get_by_role("button", name=re.compile("^Saved views")).click()
        page.get_by_role("menuitemradio", name="Default view").click()
        expect(page.locator(".tl-bar.tl-leave")).to_have_count(1)  # the bar (the key's swatch is .tl-swatch.tl-leave)
        page.get_by_role("button", name=re.compile("^Saved views")).click()
        page.get_by_role("menuitemradio", name=re.compile("^Fictional planning view")).click()
        expect(page.locator(".tl-bar.tl-leave")).to_have_count(0)
        expect(page.locator(".rb-nav strong")).to_have_text("Oct 2026 – Mar 2027")
        pop = view_settings(page)
        expect(pop.get_by_label("Bar label", exact=True)).to_have_value("dates")
        expect(pop.get_by_label("Colour rotations by", exact=True)).to_have_value("cohort")
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_a_trainee_reads_their_rotations_as_an_agenda_on_a_phone(browser, live_server, all_roles, scheme):
    h = all_roles
    board_with_conflict_and_time_away(h)
    context, page = sign_in(browser, live_server, "staff", {"width": 390, "height": 844})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, "programmes")
        agenda = page.get_by_role("region", name="Rotations")
        expect(agenda.get_by_role("heading", name=re.compile("^Now"))).to_be_visible()
        expect(agenda.get_by_role("progressbar", name="Done so far")).to_have_attribute("aria-valuenow", "2")
        expect(agenda.get_by_role("heading", name=re.compile("^November 2026"))).to_be_visible()
        expect(page.get_by_role("region", name="Rotation timeline")).to_have_count(0)
        shot(page, f"trainee-agenda-{scheme}")
        wide = page.evaluate("""() => [...document.querySelectorAll('body *')].filter(n => n.getBoundingClientRect().right > innerWidth + 1 && n.getClientRects().length)
            .slice(0, 8).map(n => n.tagName + '.' + n.className + ' ' + Math.round(n.getBoundingClientRect().right))""")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), wide
        agenda.get_by_role("button", name=re.compile(r"^Emergency, 1 Oct")).click()
        expect(page.locator(".dock-panel").get_by_role("button", name="Request a change")).to_be_visible()
        # A trainee reads their rotation's dates and conflicts; its history and the leads' notes are not theirs.
        expect(page.locator(".dock-panel").get_by_role("tab")).to_have_text(["Overview", re.compile("^Conflicts")])
    finally:
        context.close()


def test_empty_filtered_and_loading_states(browser, live_server, all_roles):
    h = all_roles
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        navigate(page, "programmes/board")
        # No programme yet: one primary, the first setup step; Plan rotations says why it cannot run yet (P-17).
        expect(page.locator("#main").get_by_text("No programmes yet")).to_be_visible()
        expect(page.locator("#main").get_by_role("button", name="New programme", exact=True)).to_be_visible()
        plan = page.get_by_role("button", name="Plan rotations")
        expect(plan).to_have_attribute("aria-disabled", "true")
        plan.focus()
        page.keyboard.press("Enter")  # aria-disabled keeps it focusable: pressing it says why
        expect(page.locator("#toast-tray")).to_contain_text("Add programme members first.")
        expect(page.get_by_role("dialog", name="Plan rotations")).to_have_count(0)
        residency(h)
        # Loading: the timeline's own skeleton, busy, while the board is read.
        held = []
        page.route("**/api/rotation-board*", lambda route: held.append(route))
        page.reload()
        skeleton = page.locator(".loading.sk-timeline")
        expect(skeleton).to_have_attribute("aria-busy", "true")
        expect(skeleton).to_be_visible()
        shot(page, "board-loading")
        for route in held:
            route.continue_()
        page.unroute("**/api/rotation-board*")
        board = page.get_by_role("region", name="Rotation timeline")
        expect(board.locator(".tl-row")).to_have_count(3)
        # Filtered to nothing: what is hidden and the way back.
        page.get_by_role("searchbox", name="Search trainees").fill("Nobody fictional")
        expect(page.get_by_text("No one matches")).to_be_visible()
        expect(page.get_by_text("None of 3 trainees match “Nobody fictional” and these filters.")).to_be_visible()
        page.get_by_role("button", name="Show all").click()
        expect(board.locator(".tl-row")).to_have_count(3)
        # The board draws its toolbar again: focus still lands in its search box, not on Saved views (live check C5).
        expect(page.get_by_role("searchbox", name="Search trainees")).to_be_focused()
    finally:
        context.close()


def test_keyboard_reaches_bars_and_the_panel_returns_focus(browser, live_server, all_roles):
    h = all_roles
    types, trainee = board_with_conflict_and_time_away(h)
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        navigate(page, "programmes/board")
        bar = page.locator(f'.tl-row[data-row="{trainee}"]').get_by_role("button", name=re.compile(r"^Emergency, 1 Oct"))
        bar.focus()
        expect(bar).to_be_focused()
        page.keyboard.press("Enter")
        panel = page.locator(".dock-panel")
        expect(panel.locator("h2")).to_have_text("Emergency")
        page.keyboard.press("Escape")
        expect(panel).to_have_count(0)
        expect(bar).to_be_focused()
    finally:
        context.close()


def fake_board(start, end, people=150):
    """An invented department-scale board: 150 trainees, a year of monthly rotations each, some leave."""
    types = {f"type-{k}": name for k, name in enumerate(["Urgent care", "Paediatrics", "Internal medicine", "Women's health", "Dermatology", "Elective"])}
    rows, rotations, memberships, leave = [], [], [], []
    for i in range(people):
        pid = f"person-{i:03}"
        rows.append({"id": pid, "display_name": f"Fictional trainee {i:03}", "short_code": f"T{i:03}", "notes": 0})
        memberships.append({"id": f"m-{i}", "person_id": pid, "programme_id": f"programme-{i % 3}", "starts_on": "2026-07-01", "ends_on": "2027-12-01", "external": i % 10 == 0})
        for m in range(12):
            y, mo = divmod(9 + m, 12)
            a, b = f"{2026 + y}-{mo + 1:02}-01", f"{2026 + (9 + m + 1) // 12}-{(9 + m + 1) % 12 + 1:02}-01"
            rotations.append({"id": f"r-{i}-{m}", "person_id": pid, "cohort_id": "cohort-0", "rotation_type_id": f"type-{(i + m) % 6}", "service_id": None,
                              "starts_on": a, "ends_on": b, "department_available": (i + m) % 7 != 0, "revision": 1})
        if i % 4 == 0:
            leave.append({"person_id": pid, "starts_on": "2026-11-08", "ends_on": "2026-11-15", "stage": "approved", "label": "Leave", "kind": "leave"})
    return {"start": start, "end": end, "people": rows, "memberships": memberships, "rotations": rotations, "leave": leave, "changes": [],
            "names": {"programmes": {f"programme-{k}": f"Fictional programme {k}" for k in range(3)}, "cohorts": {"cohort-0": "Fictional cohort"},
                      "rotation_types": types, "services": {}},
            "choices": {"rotation_types": types, "services": {}}, "can_plan": True, "can_note": False}


@pytest.fixture
def department_scale_board(browser, live_server, all_roles):
    """150 trainees × 12 rotations on the board, and the seconds until its first row showed."""
    def answer(route):
        q = dict(x.split("=") for x in route.request.url.split("?")[1].split("&"))
        route.fulfill(status=200, content_type="application/json", body=json.dumps(fake_board(q["start"], q["end"])))
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        page.route("**/api/rotation-board*", answer)
        started = time.perf_counter()
        navigate(page, "programmes/board")
        board = page.get_by_role("region", name="Rotation timeline")
        expect(board.locator(".tl-row").first).to_be_visible()
        yield page, board, time.perf_counter() - started
    finally:
        context.close()


@pytest.mark.wall_benchmark
def test_department_scale_board_cpu_budget(department_scale_board):
    _, _, drawn = department_scale_board
    assert drawn < (20 if os.getenv("CI") else 10), f"the board took {drawn:.1f} s"


def test_department_scale_draws_only_the_rows_in_view_and_keeps_the_header_stuck(department_scale_board):
    """150 trainees × 12 rotations: the timeline draws the rows near the viewport (not all 150), scrolls to the last
    one, and its sticky header lets nothing through (tests/sticky_guard.js). Its time budget: the cpu_budget test."""
    page, board, _ = department_scale_board
    assert board.locator(".tl-row").count() < 60, "only rows near the viewport are drawn"
    board.evaluate("t => t.scrollTop = t.scrollHeight")
    expect(board.locator(".tl-row", has_text="Fictional trainee 149")).to_be_visible()
    assert board.locator(".tl-row").count() < 60
    board.evaluate("t => t.scrollTop = 0")
    expect(board.locator(".tl-row", has_text="Fictional trainee 000")).to_be_visible()
    # Rows drawn while scrolling back up sit in the page in visual order, so Tab follows what is seen.
    tops = board.locator(".tl-group,.tl-row").evaluate_all("ns => ns.map(n => parseFloat(n.style.getPropertyValue('--top')))")
    assert tops == sorted(tops), tops
    board.locator(".tl-row").first.locator(".tl-who").focus()
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.closest('.tl-row').querySelector('.tl-who b').textContent") == "Fictional trainee 000"
    guard = Sticky(page, "programme_lead")
    guard.check("rotation board, 150 trainees")
    guard.done()
    shot(page, "board-150")


def test_every_bar_that_opens_something_is_a_24px_target_in_both_densities(department_scale_board):
    """WCAG 2.2 SC 2.5.8 Target Size (Minimum): a clickable bar is at least 24 x 24 CSS px, also short rotations at the
    year zoom and in Compact rows, and its lane keeps that room free (no two clickable bars overlap)."""
    page, board, _ = department_scale_board
    small = """bars => {
        const boxes = bars.filter(b => b.offsetParent).map(b => b.getBoundingClientRect());
        const under = boxes.filter(r => r.width < 23.5 || r.height < 23.5).length;
        let overlaps = 0;
        for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
            const a = boxes[i], b = boxes[j];
            if (a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5) overlaps++;
        }
        return [boxes.length > 0, under, overlaps];
    }"""
    assert board.locator("button.tl-bar").evaluate_all(small) == [True, 0, 0]
    pop = view_settings(page)
    pop.get_by_role("radiogroup", name="Row density").get_by_role("radio", name="Compact").click()
    close_popover(page)
    board = page.get_by_role("region", name="Rotation timeline")
    expect(page.locator(".tl.tl-compact")).to_have_count(1)
    assert board.locator("button.tl-bar").evaluate_all(small) == [True, 0, 0]


def test_a_conflicting_duty_day_has_an_alert_and_words_not_colour_alone(browser, live_server, all_roles):
    """The duty-density strip (drawn by the shared layout) marks each run of conflicting days with an alert icon and
    its words; a trainee's notes count shows on their row."""
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        page.evaluate("""async () => {
          const {timelineView} = await window.importBundle('timeline.js');
          document.querySelector('#main').replaceChildren(timelineView({label: 'Fixture timeline', start: '2026-10-01', end: '2027-10-01', zoom: 'year',
            groups: [{key: '', name: '', rows: [{id: 'p1', name: 'Fictional person', bars: [], tags: [{text: '2', tip: '2 notes', icon: 'edit', class: 'rb-notes'}],
              heat: {levels: {'2026-10-05': 2, '2026-10-22': 1}, errors: ['2026-10-22', '2026-10-23', '2026-11-04']}}]}]}));
        }""")
        region = page.get_by_role("region", name="Fixture timeline")
        expect(region.get_by_role("img", name="Duty conflict: 22 Oct – 23 Oct 2026")).to_be_visible()
        expect(region.get_by_role("img", name="Duty conflict: 4 Nov")).to_be_visible()
        expect(region.locator(".tl-heat-x [data-icon=alert]")).to_have_count(2)
        expect(region.locator(".tl-heatmap .hx")).to_have_count(3)
        expect(region.locator(".rb-notes[aria-label='2 notes']")).to_have_text("2")
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_rotation_panel_shows_its_dates_and_tabs(browser, live_server, all_roles, scheme):
    """The bar side panel (approved mockup v3): a mini timeline of the months around the rotation with its overlap, and
    tabs for Overview, Conflicts (with their count), Notes and History."""
    h = all_roles
    types, trainee = board_with_conflict_and_time_away(h)
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, "programmes/board")
        page.locator(f'.tl-row[data-row="{trainee}"]').get_by_role("button", name=re.compile(r"^Emergency, 1 Oct")).click()
        panel = page.locator(".dock-panel")
        tablist = panel.get_by_role("tablist", name="Rotation")
        expect(tablist.get_by_role("tab")).to_have_text(["Overview", "Conflicts 1", "Notes 0", "History"])
        expect(tablist.get_by_role("tab", name="Overview")).to_be_focused()
        mini = panel.get_by_role("img", name="Emergency, 1 Oct – 31 Oct 2026, 1 overlap")
        expect(mini).to_be_visible()
        expect(mini.locator(".tl-zone")).to_have_count(1)
        expect(mini.locator(".tl-cell")).to_have_text(["Sep", "Oct", "Nov"])
        shot(page, f"rotation-panel-{scheme}")
        tablist.get_by_role("tab", name="Conflicts 1").click()
        expect(panel.get_by_role("tabpanel").get_by_role("heading", name=re.compile(r"^Overlaps Clinic"))).to_be_visible()
        tablist.get_by_role("tab", name="History").click()
        expect(panel.get_by_role("tabpanel").locator("li").first).to_contain_text("Version 1 · 1 Oct – 31 Oct 2026")
        tablist.get_by_role("tab", name="Notes 0").click()
        expect(panel.get_by_label("Add a note", exact=True)).to_be_visible()
        # A rotation with no overlap: its Conflicts tab says so.
        page.locator(f'.tl-row[data-row="{trainee}"]').get_by_role("button", name=re.compile(r"^Clinic, 1 Dec")).click()
        expect(panel.locator("h2")).to_have_text("Clinic")
        panel.get_by_role("tab", name="Conflicts 0").click()
        expect(panel.get_by_role("tabpanel")).to_have_text("No conflicts: this rotation shares no days with another.")
    finally:
        context.close()


def test_bar_label_and_colour_by_change_the_bars_and_are_kept(browser, live_server, all_roles):
    h = all_roles
    residency(h)
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    hues = """bs => new Set(bs.map(b => [...b.classList].find(c => c.startsWith('duty-')))).size"""
    try:
        navigate(page, "programmes/board")
        board = page.get_by_role("region", name="Rotation timeline")
        bars = board.locator("button.tl-bar.tl-block")
        expect(bars).to_have_count(9)
        assert bars.evaluate_all(hues) == 3  # by rotation type (the default): three types, three hues
        expect(board.locator("button.tl-bar.tl-block .tl-lbl", has_text=re.compile(r"^1 Oct"))).to_have_count(0)
        # Year zoom: a month is narrow, so a bar's dates are its first day (the full dates are in its name and card).
        pop = view_settings(page)
        pop.get_by_label("Bar label", exact=True).select_option("dates")
        board = page.get_by_role("region", name="Rotation timeline")
        expect(board.locator("button.tl-bar.tl-block .tl-lbl", has_text=re.compile(r"^1 Oct")).first).to_be_visible()
        page.locator(".dt-pop").get_by_label("Colour rotations by", exact=True).select_option("cohort")
        board = page.get_by_role("region", name="Rotation timeline")
        expect(board.locator("button.tl-bar.tl-block")).to_have_count(9)
        assert board.locator("button.tl-bar.tl-block").evaluate_all(hues) == 1  # one cohort, one hue
        close_popover(page)
        page.reload()
        expect_loaded(page, "programmes/board")
        board = page.get_by_role("region", name="Rotation timeline")
        expect(board.locator("button.tl-bar.tl-block .tl-lbl", has_text=re.compile(r"^1 Oct")).first).to_be_visible()
        assert board.locator("button.tl-bar.tl-block").evaluate_all(hues) == 1
        expect(page.get_by_role("button", name=re.compile("^View settings"))).to_have_class(re.compile(r"\bon\b"))
    finally:
        context.close()


def test_board_notes_and_history_failures_say_so_with_try_again(browser, live_server, all_roles):
    h = all_roles
    p, cohort, types, (trainee, local, visitor), planned = residency(h)
    outage = {"status": 500, "content_type": "application/json", "body": json.dumps({"error": {"code": "SERVER_ERROR", "message": "Fictional outage."}})}
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        navigate(page, "programmes/board")
        page.route("**/api/rotation-board*", lambda route: route.fulfill(**outage))
        page.reload()
        main = page.locator("#main")
        expect(main.get_by_role("alert")).to_contain_text("Couldn’t load the rotation board. Fictional outage.")
        page.unroute("**/api/rotation-board*")
        main.get_by_role("button", name="Retry", exact=True).click()
        board = page.get_by_role("region", name="Rotation timeline")
        expect(board.locator(".tl-row")).to_have_count(3)
        # The trainee's notes: a failed read says so in the panel, and Retry reads them again.
        page.route("**/api/people/*/trainee-notes", lambda route: route.fulfill(**outage))
        board.locator(f'.tl-row[data-row="{trainee}"] .tl-who').click()
        panel = page.locator(".dock-panel")
        expect(panel.get_by_role("alert")).to_contain_text("Couldn’t load the notes. Fictional outage.")
        expect(panel.get_by_label("Add a note", exact=True)).to_be_editable()  # writing a note still works
        page.unroute("**/api/people/*/trainee-notes")
        panel.get_by_role("button", name="Retry", exact=True).click()
        expect(panel.get_by_role("alert")).to_have_count(0)
        page.keyboard.press("Escape")
        # A rotation's history the same way.
        page.route("**/api/catalogues/rotations/*/history", lambda route: route.fulfill(**outage))
        board.get_by_role("button", name=re.compile(r"^Emergency, 1 Oct")).click()
        panel.get_by_role("tab", name="History").click()
        expect(panel.get_by_role("alert")).to_contain_text("Couldn’t load the history. Fictional outage.")
        page.unroute("**/api/catalogues/rotations/*/history")
        panel.get_by_role("button", name="Retry", exact=True).click()
        expect(panel.get_by_role("tabpanel").locator("li")).to_have_count(1)
    finally:
        context.close()


@pytest.mark.wall_benchmark
def test_department_scale_board_view_changes_cpu_budget(department_scale_board):
    """PI-1: at 150 trainees × 12 rotations a Group change and a layer switched off and on redraw from the board's
    index (no whole-board scans) and keep each trainee's built row. The board's own script time (CPU profile self time
    in rotation_board.js) stays under 25 ms for the three changes; the timeline's layout is its own budget."""
    page, board, _ = department_scale_board
    cdp = page.context.new_cdp_session(page)
    cdp.send("Profiler.enable")
    cdp.send("Profiler.setSamplingInterval", {"interval": 100})
    cdp.send("Profiler.start")
    page.get_by_role("button", name=re.compile(r"^Group")).click()
    page.get_by_role("menuitemradio", name="Cohort", exact=True).click()
    expect(page.locator(".tl-group").first).to_be_visible()
    close_popover(page)
    pop = view_settings(page)
    pop.get_by_role("checkbox", name="Leave", exact=True).uncheck()
    pop.get_by_role("checkbox", name="Leave", exact=True).check()
    close_popover(page)
    expect(board.locator(".tl-row").first).to_be_visible()
    profile = cdp.send("Profiler.stop")["profile"]
    files = {n["id"]: n["callFrame"]["url"] for n in profile["nodes"]}
    board_ms = sum(d for s, d in zip(profile["samples"], profile["timeDeltas"]) if files[s].endswith("/rotation_board.js")) / 1000
    print("rotation board script time for 3 view changes:", round(board_ms, 1), "ms")
    assert board_ms < 25 * (3 if os.getenv("CI") else 1), board_ms

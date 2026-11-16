"""Planning on the Rotation board by hand (timeline_drag.ts, approved mockup 21 Oct 2026), against the real governed
gestures (maintenance: POST /api/timeline/preview and the timeline.rotation command): drag a palette chip onto a row, drag a
bar to move it or its edge to resize it, drop a template, the keyboard alternatives, the live conflict preview that
stops a change, Undo, and a trainee's own change going for approval. Fixtures are fictional, made through the API."""
import os
import re
from datetime import date

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, set_layout, sign_in, wait_held  # noqa: F401
from .test_browser_operator_role_coverage import all_roles  # noqa: F401
from .test_browser_rotation_board import residency

pytestmark = pytest.mark.browser
START, END = date(2026, 10, 1), date(2027, 10, 1)  # the board's Year window on the test day (3 Oct 2026)


def x_of(lane, day):
    box = lane.bounding_box()
    return box["x"] + ((day - START).days + 0.5) / (END - START).days * box["width"]


def drag(page, source, lane, day, steps=12):
    box = source.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    lb = lane.bounding_box()
    page.mouse.move(x_of(lane, day), lb["y"] + lb["height"] / 2, steps=steps)


# The contrast of a drag tip's line against the tip (WCAG 1.4.3: 4.5:1 for its 11.5px text).
TIP_CONTRAST = """el => {
  const rgb = c => c.match(/[\\d.]+/g).slice(0, 3).map(Number);
  const lum = c => { const [r, g, b] = rgb(c).map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }); return .2126 * r + .7152 * g + .0722 * b; };
  const [a, b] = [lum(getComputedStyle(el).color), lum(getComputedStyle(el.closest('.tl-drag-tip')).backgroundColor)].sort((x, y) => y - x);
  return (a + .05) / (b + .05);
}"""


def tip_contrast(page, line):
    """The line's contrast in light and dark with Theme = System (no data-theme: the OS decides)."""
    assert page.evaluate("() => !document.documentElement.dataset.theme")
    out = {}
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        out[scheme] = line.evaluate(TIP_CONTRAST)
    return out


def rotations_of(h, person):
    return sorted((r["starts_on"], r["ends_on"]) for r in h.get("coordinator", "/api/catalogues")["rotations"] if r["person_id"] == person)


def board(page, trainee):
    navigate(page, "programmes/board")
    region = page.get_by_role("region", name="Rotation timeline")
    return region, region.locator(f'.tl-row[data-row="{trainee}"] .tl-lane')


def test_a_chip_dropped_on_a_row_plans_a_rotation_and_undo_takes_it_back(browser, live_server, all_roles):
    h = all_roles
    _, _, types, (trainee, _, _), _ = residency(h)
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        region, lane = board(page, trainee)
        palette = page.get_by_role("complementary", name="Rotations to place on the board")
        chip = palette.get_by_role("button", name=re.compile(f"^{types[1]['name']}, 4 wk"))
        drag(page, chip, lane, date(2027, 1, 12))
        tip = lane.locator(".tl-drag-tip")
        expect(tip).to_contain_text("1 Jan – 28 Jan 2027")  # snaps to the month at Year zoom
        expect(tip).not_to_contain_text("Checking")
        if os.getenv("TIMELINE_SHOTS"):
            page.screenshot(path=os.path.join(os.environ["TIMELINE_SHOTS"], "board-drag-chip.png"))
        page.mouse.up()
        expect(page.get_by_role("status").filter(has_text="1 rotation saved")).to_be_visible()
        expect(region.locator(f'.tl-row[data-row="{trainee}"]').get_by_role("button", name=re.compile(rf"^{types[1]['name']}, 1 Jan – 28 Jan 2027"))).to_be_visible()
        assert ("2027-01-01", "2027-01-29") in rotations_of(h, trainee)
        page.locator("#toast-tray .toast", has_text="1 rotation saved").get_by_role("button", name="Undo", exact=True).click()
        expect(region.locator(f'.tl-row[data-row="{trainee}"]').get_by_role("button", name=re.compile(r", 1 Jan – 28 Jan 2027"))).to_have_count(0)
        assert ("2027-01-01", "2027-01-29") not in rotations_of(h, trainee)
    finally:
        context.close()


def test_the_live_preview_stops_a_drop_that_overlaps(browser, live_server, all_roles):
    h = all_roles
    _, _, types, (trainee, _, _), _ = residency(h)
    before = rotations_of(h, trainee)
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        region, lane = board(page, trainee)
        chip = page.get_by_role("complementary", name="Rotations to place on the board").get_by_role("button", name=re.compile(f"^{types[0]['name']},"))
        drag(page, chip, lane, date(2026, 11, 10))
        tip = lane.locator(".tl-drag-tip")
        expect(tip.locator(".tl-gov.error")).to_be_visible()  # red, with its icon and words
        expect(tip.locator(".tl-gov.error [data-icon=alert]")).to_have_count(1)
        ratios = tip_contrast(page, tip.locator(".tl-gov.error"))
        assert min(ratios.values()) >= 4.5, ratios
        if os.getenv("TIMELINE_SHOTS"):
            page.screenshot(path=os.path.join(os.environ["TIMELINE_SHOTS"], "board-drag-blocked.png"))
        page.mouse.up()
        expect(lane.locator(".tl-ghost")).to_have_count(0, timeout=6000)
        assert rotations_of(h, trainee) == before
    finally:
        context.close()


def test_dragging_a_bar_moves_it_and_its_edge_resizes_it(browser, live_server, all_roles):
    h = all_roles
    _, _, types, (trainee, _, _), _ = residency(h)
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        region, lane = board(page, trainee)
        bar = lane.get_by_role("button", name=re.compile(r", 1 Dec – 31 Dec 2026"))
        drag(page, bar, lane, date(2027, 1, 16))
        expect(lane.locator(".tl-drag-tip")).to_contain_text("1 Jan – 31 Jan 2027")
        expect(lane.locator(".tl-drag-tip")).not_to_contain_text("Checking")
        page.mouse.up()
        expect(page.get_by_role("status").filter(has_text="1 rotation saved")).to_be_visible()
        assert ("2027-01-01", "2027-02-01") in rotations_of(h, trainee)
        # The right edge: to the end of March.
        moved = lane.get_by_role("button", name=re.compile(r", 1 Jan – 31 Jan 2027"))
        expect(moved).to_be_visible()  # the lane redraws after the save
        box = moved.bounding_box()
        page.mouse.move(box["x"] + box["width"] - 3, box["y"] + box["height"] / 2)
        page.mouse.down()
        lb = lane.bounding_box()
        page.mouse.move(x_of(lane, date(2027, 3, 25)), lb["y"] + lb["height"] / 2, steps=12)
        expect(lane.locator(".tl-drag-tip")).to_contain_text("1 Jan – 31 Mar 2027")
        expect(lane.locator(".tl-drag-tip")).not_to_contain_text("Checking")
        page.mouse.up()
        expect(region.locator(f'.tl-row[data-row="{trainee}"]').get_by_role("button", name=re.compile(r", 1 Jan – 31 Mar 2027"))).to_be_visible()
        assert ("2027-01-01", "2027-04-01") in rotations_of(h, trainee)
    finally:
        context.close()


def test_a_template_drop_places_its_sequence(browser, live_server, all_roles):
    h = all_roles
    p, _, types, (trainee, _, _), _ = residency(h)
    blocks = [{"rotation_type_id": t["id"], "service_id": None, "length": 1, "unit": "months", "department_available": True} for t in types[:2]]
    h.configure("rotation_templates", {"name": "Fictional two-month plan", "programme_id": p["id"], "blocks": blocks})
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        region, lane = board(page, trainee)
        chip = page.get_by_role("complementary", name="Rotations to place on the board").get_by_role("button", name=re.compile("^Fictional two-month plan, 2 mo"))
        expect(chip.locator(".rb-stripe i")).to_have_count(2)
        drag(page, chip, lane, date(2027, 2, 3))
        expect(lane.locator(".tl-drag-tip")).to_contain_text("1 Feb – 31 Mar 2027")
        expect(lane.locator(".tl-drag-tip")).not_to_contain_text("Checking")
        page.mouse.up()
        expect(page.get_by_role("status").filter(has_text="2 rotations saved")).to_be_visible()
        assert {("2027-02-01", "2027-03-01"), ("2027-03-01", "2027-04-01")} <= set(rotations_of(h, trainee))
    finally:
        context.close()


def test_the_keyboard_moves_a_bar_and_places_a_chip(browser, live_server, all_roles):
    h = all_roles
    _, _, types, (trainee, _, _), _ = residency(h)
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        region, lane = board(page, trainee)
        bar = lane.get_by_role("button", name=re.compile(r", 1 Dec – 31 Dec 2026"))
        expect(bar).to_have_attribute("aria-keyshortcuts", re.compile(r"ArrowRight.*Alt\+ArrowRight"))
        bar.focus()
        page.keyboard.press("Shift+ArrowRight")  # a month
        expect(lane.locator(".tl-drag-tip")).to_contain_text("1 Jan – 31 Jan 2027")
        expect(lane.locator(".tl-drag-tip")).not_to_contain_text("Checking")
        page.keyboard.press("Enter")
        expect(page.get_by_role("status").filter(has_text="1 rotation saved")).to_be_visible()
        assert ("2027-01-01", "2027-02-01") in rotations_of(h, trainee)
        # The redraw replaced the bar the keys were on and draws its rows a frame later: the focus follows it there.
        expect(lane.get_by_role("button", name=re.compile(r", 1 Jan – 31 Jan 2027"))).to_be_focused()
        # A chip goes on the row last focused; Escape leaves everything as it was.
        region.locator(f'.tl-row[data-row="{trainee}"] .tl-who').focus()
        chip = page.get_by_role("complementary", name="Rotations to place on the board").get_by_role("button", name=re.compile(f"^{types[2]['name']},"))
        chip.focus()
        page.keyboard.press("Enter")
        expect(region.locator(f'.tl-row[data-row="{trainee}"] .tl-ghost')).to_have_count(1)
        page.keyboard.press("Escape")
        expect(region.locator(".tl-ghost")).to_have_count(0)
        chip.focus()
        page.keyboard.press("Enter")
        for _ in range(4):  # from 4 Oct (today's week) to 1 Nov … past the planned months, into February
            page.keyboard.press("Shift+ArrowRight")
        expect(region.locator(".tl-drag-tip")).to_contain_text("Feb 2027")
        expect(region.locator(".tl-drag-tip")).not_to_contain_text("Checking")
        # Hold the save: the Enter that places the chip must not also start a second placement (a second ghost).
        held = []
        page.route("**/api/commands", lambda route: held.append(route))
        with page.expect_request(lambda r: r.url.endswith("/api/commands") and r.method == "POST"):
            page.keyboard.press("Enter")
        assert page.evaluate("() => document.querySelectorAll('.tl-ghost').length") == 1
        held[0].continue_()
        page.unroute("**/api/commands")
        expect(region.locator(f'.tl-row[data-row="{trainee}"]').get_by_role("button", name=re.compile(rf"^{types[2]['name']}, 1 Feb – 28 Feb 2027"))).to_be_visible()
        assert ("2027-02-01", "2027-03-01") in rotations_of(h, trainee)
        # The Enter that placed it started nothing new: no ghost is left, and the next keyboard move works.
        expect(region.locator(".tl-ghost")).to_have_count(0)
        moved = region.locator(f'.tl-row[data-row="{trainee}"] .tl-lane').get_by_role("button", name=re.compile(r", 1 Jan – 31 Jan 2027"))
        moved.focus()
        page.keyboard.press("Shift+ArrowRight")
        expect(region.locator(".tl-drag-tip")).to_contain_text("1 Feb – 3 Mar 2027")  # a month on, the same 31 days
        page.keyboard.press("Escape")
        expect(region.locator(".tl-ghost")).to_have_count(0)
    finally:
        context.close()


def test_a_trainees_own_move_goes_for_approval(browser, live_server, all_roles):
    h = all_roles
    _, _, _, (trainee, _, _), _ = residency(h)
    context, page = sign_in(browser, live_server, "staff", {"width": 1440, "height": 900})
    try:
        region, lane = board(page, trainee)
        expect(page.get_by_role("complementary", name="Rotations to place on the board")).to_have_count(0)
        bar = lane.get_by_role("button", name=re.compile(r", 1 Dec – 31 Dec 2026"))
        drag(page, bar, lane, date(2027, 1, 16))
        tip = lane.locator(".tl-drag-tip")
        expect(tip.locator(".tl-gov.warn")).to_contain_text("Goes to your programme lead to approve")
        expect(tip.locator(".tl-gov.warn [data-icon=clock]")).to_have_count(1)
        ratios = tip_contrast(page, tip.locator(".tl-gov.warn"))
        assert min(ratios.values()) >= 4.5, ratios
        page.mouse.up()
        expect(region.get_by_role("button", name=re.compile(r", 1 Dec – 31 Dec 2026, change requested"))).to_be_visible()
        assert [i["kind"] for i in h.get("programme_lead", "/api/requests/all")["items"]] == ["rotation_change"]
        assert ("2026-12-01", "2027-01-01") in rotations_of(h, trainee)  # unchanged until the lead approves
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_a_moved_bar_comes_back_with_undo(browser, live_server, all_roles, scheme):
    h = all_roles
    _, _, _, (trainee, _, _), _ = residency(h)
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        page.emulate_media(color_scheme=scheme)
        region, lane = board(page, trainee)
        drag(page, lane.get_by_role("button", name=re.compile(r", 1 Dec – 31 Dec 2026")), lane, date(2027, 1, 16))
        expect(lane.locator(".tl-drag-tip")).not_to_contain_text("Checking")
        if os.getenv("TIMELINE_SHOTS"):
            page.screenshot(path=os.path.join(os.environ["TIMELINE_SHOTS"], f"board-drag-move-{scheme}.png"))
        page.mouse.up()
        toast = page.locator("#toast-tray .toast", has_text="1 rotation saved")
        expect(toast).to_be_visible()
        assert ("2027-01-01", "2027-02-01") in rotations_of(h, trainee)
        # The bar is back before the Undo command lands (the trace shows it pending): read the store once it has.
        with page.expect_response(lambda r: r.url.endswith("/api/commands") and r.request.method == "POST") as undone:
            toast.get_by_role("button", name="Undo", exact=True).click()
        assert undone.value.ok
        expect(lane.get_by_role("button", name=re.compile(r", 1 Dec – 31 Dec 2026"))).to_be_visible()
        # The Undo command has finished (its own message) before the server is read: the bar alone may show first.
        expect(page.locator("#toast-tray .toast", has_text="Undone. The previous values are back.")).to_be_visible()
        assert ("2026-12-01", "2027-01-01") in rotations_of(h, trainee)
        assert ("2027-01-01", "2027-02-01") not in rotations_of(h, trainee)
    finally:
        context.close()


def test_an_unrelated_message_during_a_slow_refresh_cannot_take_a_drops_undo(browser, live_server, all_roles):
    """Review maintenance: a chip dropped on the board, the refresh after it is held, then Account › Notifications › Turn off (tab alerts). The
    saved rotation keeps its own Undo; "Desktop alerts are off." offers none (its Undo would have removed the rotation)."""
    h = all_roles
    _, _, types, (trainee, _, _), _ = residency(h)
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    held = []
    try:
        page.evaluate("localStorage.setItem('rota.alerts', 'on')")
        region, lane = board(page, trainee)
        chip = page.get_by_role("complementary", name="Rotations to place on the board").get_by_role("button", name=re.compile(f"^{types[1]['name']}, 4 wk"))
        drag(page, chip, lane, date(2027, 1, 12))
        expect(lane.locator(".tl-drag-tip")).not_to_contain_text("Checking")
        # Account › Notifications reads this device's alert state; only that read passes the hold.
        page.route("**/api/**", lambda route: held.append(route) if route.request.method == "GET" and "/api/me/push" not in route.request.url else route.continue_())
        page.mouse.up()
        wait_held(page, held)  # the refresh after the save is waiting on the server
        saved = page.locator("#toast-tray .toast", has_text="1 rotation saved")
        expect(saved.get_by_role("button", name="Undo", exact=True)).to_be_visible()
        page.get_by_role("button", name=re.compile("^Account menu, ")).click()
        page.get_by_role("menuitem", name="Notifications", exact=True).click()
        page.locator(".np-panel").get_by_role("button", name="Turn off", exact=True).click()
        alerts = page.locator("#toast-tray .toast", has_text="Desktop alerts are off.")
        expect(alerts).to_be_visible()
        expect(alerts.get_by_role("button", name="Undo", exact=True)).to_have_count(0)
        expect(page.locator("#toast-tray .toast").get_by_role("button", name="Undo", exact=True)).to_have_count(1)
        for route in held:  # released once each, before unroute: unroute itself continues whatever it still holds
            route.continue_()
        page.unroute("**/api/**")
        expect(saved.get_by_role("button", name="Undo", exact=True)).to_be_visible()
        assert ("2027-01-01", "2027-01-29") in rotations_of(h, trainee)
    finally:
        context.close()


def test_academic_and_protected_time_are_never_a_conflict(browser, live_server, all_roles):
    h = all_roles
    _, _, _, (trainee, _, _), _ = residency(h)
    h.command("coordinator", "availability.record", {"person_id": trainee, "kind": "protected_time", "start": "2027-01-04T00:00:00+03:00",
                                                     "end": "2027-01-15T00:00:00+03:00", "private_reason": "Fictional teaching week"})
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        region, lane = board(page, trainee)
        drag(page, lane.get_by_role("button", name=re.compile(r", 1 Dec – 31 Dec 2026")), lane, date(2027, 1, 16))
        tip = lane.locator(".tl-drag-tip")
        expect(tip).to_contain_text("1 Jan – 31 Jan 2027")
        expect(tip).not_to_contain_text("Checking")
        expect(tip.locator(".tl-gov")).to_have_count(0)  # nothing red, nothing waiting
        page.mouse.up()
        expect(page.get_by_role("status").filter(has_text="1 rotation saved")).to_be_visible()
        assert ("2027-01-01", "2027-02-01") in rotations_of(h, trainee)
    finally:
        context.close()


def test_a_read_only_role_cannot_drag(browser, live_server, all_roles):
    """An auditor reads every rotation on People › Timeline (the board is for planners); nothing there moves."""
    h = all_roles
    _, _, _, (trainee, _, _), _ = residency(h)
    before = rotations_of(h, trainee)
    context, page = sign_in(browser, live_server, "auditor", {"width": 1440, "height": 900})
    try:
        navigate(page, "people")
        set_layout(page, "Timeline")
        region = page.get_by_role("region", name="People timeline")
        lane = region.locator(f'.tl-row[data-row="{trainee}"] .tl-lane')
        bar = lane.get_by_role("button", name=re.compile(r"^Rotations: Emergency, 1 Oct – 31 Oct 2026"))
        expect(bar).to_be_visible()
        expect(bar).not_to_have_attribute("aria-keyshortcuts", re.compile("."))
        drag(page, bar, lane, date(2027, 1, 16))
        expect(region.locator(".tl-ghost")).to_have_count(0)
        page.mouse.up()
        bar.focus()
        page.keyboard.press("Shift+ArrowRight")
        expect(region.locator(".tl-ghost, .tl-drag-tip")).to_have_count(0)
        assert rotations_of(h, trainee) == before
    finally:
        context.close()


@pytest.mark.parametrize("role", ["staff", "programme_lead"])
def test_a_phone_reads_the_board_and_never_drags(browser, live_server, all_roles, role):
    h = all_roles
    _, _, _, (trainee, _, _), _ = residency(h)
    before = rotations_of(h, trainee)
    context, page = sign_in(browser, live_server, role, {"width": 390, "height": 844})
    try:
        navigate(page, "programmes/board")
        expect(page.locator(".tl-agenda")).to_be_visible()
        expect(page.get_by_role("complementary", name="Rotations to place on the board")).to_have_count(0)
        if role == "programme_lead":  # the whole timeline is one tap away, still read-only
            page.get_by_role("button", name="Show the timeline", exact=True).click()
            region = page.get_by_role("region", name="Rotation timeline")
            bar = region.locator(f'.tl-row[data-row="{trainee}"]').get_by_role("button", name=re.compile(r", 1 Dec – 31 Dec 2026"))
            bar.focus()
            page.keyboard.press("Shift+ArrowRight")
            expect(region.locator(".tl-ghost")).to_have_count(0)
        assert rotations_of(h, trainee) == before
    finally:
        context.close()


@pytest.mark.parametrize("kind", ["type", "template"])
def test_escape_cancels_a_chip_drag(browser, live_server, all_roles, kind):
    """Escape during a palette drag cancels it: letting go afterwards places nothing."""
    h = all_roles
    p, _, types, (trainee, _, _), _ = residency(h)
    if kind == "template":
        blocks = [{"rotation_type_id": t["id"], "service_id": None, "length": 1, "unit": "months", "department_available": True} for t in types[:2]]
        h.configure("rotation_templates", {"name": "Fictional two-month plan", "programme_id": p["id"], "blocks": blocks})
    before = rotations_of(h, trainee)
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        region, lane = board(page, trainee)
        palette = page.get_by_role("complementary", name="Rotations to place on the board")
        chip = palette.get_by_role("button", name=re.compile("^Fictional two-month plan," if kind == "template" else f"^{types[1]['name']},"))
        drag(page, chip, lane, date(2027, 2, 3))
        expect(lane.locator(".tl-ghost")).to_have_count(1)
        page.keyboard.press("Escape")
        expect(region.locator(".tl-ghost")).to_have_count(0)
        page.mouse.up()
        expect(region.locator(".tl-ghost")).to_have_count(0)
        expect(page.get_by_role("status").filter(has_text="saved")).to_have_count(0)
        assert rotations_of(h, trainee) == before
    finally:
        context.close()


def test_a_chip_dropped_after_a_cohort_ends_goes_into_the_next_cohort(browser, live_server, all_roles):
    """Year 1 ends on 1 July; a rotation dropped in August belongs to the successor cohort that covers it (the server's
    cohort window), not to the nearest rotation's old cohort."""
    h = all_roles
    p, _, types, (trainee, _, _), _ = residency(h)
    nxt = h.configure("cohorts", {"name": "Fictional year 2", "programme_id": p["id"], "starts_on": "2027-07-01", "ends_on": "2028-07-01"})["record"]
    h.configure("programme_memberships", {"person_id": trainee, "programme_id": p["id"], "starts_on": "2027-07-01", "ends_on": "2028-07-01"}, role="programme_lead")
    context, page = sign_in(browser, live_server, "programme_lead", {"width": 1440, "height": 900})
    try:
        region, lane = board(page, trainee)
        chip = page.get_by_role("complementary", name="Rotations to place on the board").get_by_role("button", name=re.compile(f"^{types[1]['name']}, 4 wk"))
        drag(page, chip, lane, date(2027, 8, 12))
        tip = lane.locator(".tl-drag-tip")
        expect(tip).to_contain_text("1 Aug – 28 Aug 2027")
        expect(tip).not_to_contain_text("Checking")
        expect(tip.locator(".tl-gov.error")).to_have_count(0)
        page.mouse.up()
        expect(page.get_by_role("status").filter(has_text="1 rotation saved")).to_be_visible()
        placed = next(r for r in h.get("coordinator", "/api/catalogues")["rotations"] if r["person_id"] == trainee and r["starts_on"] == "2027-08-01")
        assert placed["cohort_id"] == nxt["id"]
    finally:
        context.close()

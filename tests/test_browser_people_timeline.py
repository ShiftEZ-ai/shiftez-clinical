"""People › Timeline (person_timeline.ts on the shared Timeline layout): offered only where the server has the person
timeline (GET /api/timeline), drawn from the contract's layers (never a list of its own), grouped and filtered as the
People list is, with conflicts (red, or gold with a clock), duty density, the item panel, settings kept with the list,
and an agenda on a phone. The first journey reads the real endpoint (maintenance); the conflict and density drawings use
fictional answers in the contract's shape (TimelineResponse) for the list's own fictional people, since a duty on
leave needs a published rota."""
import json
import os
import re
from datetime import date
from urllib.parse import parse_qsl

import pytest
from playwright.sync_api import expect

from .legend_guard import assert_legend_matches

from .test_browser_operator_role_coverage import all_roles  # noqa: F401
from .test_browser_rotation_board import residency
from .test_browser import browser, close_popover, expect_loaded, live_server, navigate, set_layout, sign_in, toolbar  # noqa: F401

pytestmark = pytest.mark.browser

LAYERS = [{"kind": "rotation", "label": "Rotations", "style_key": "rotation", "count": 2, "source": "rotations"},
          {"kind": "leave", "label": "Leave", "style_key": "leave", "count": 1, "source": "availability"},
          {"kind": "overtime_offer", "label": "Overtime offers", "style_key": "overtime", "count": 1, "source": "scheduling_facts"},
          {"kind": "eligibility", "label": "Eligibility", "style_key": "eligibility", "count": 1, "source": "eligibilities"},
          {"kind": "rank", "label": "Rank changes", "style_key": "rank", "count": 1, "source": "memberships"},
          {"kind": "duty_density", "label": "Published duties", "style_key": "density", "count": 3, "source": "publications"},
          {"kind": "conflict", "label": "Conflicts", "style_key": "conflict", "count": 2, "source": "validation"}]


def item(i, kind, label, a, b, **details):
    return {"id": f"i{i}", "kind": kind, "source": kind, "label": label, "starts_on": a, "ends_on": b, "status": "active", "details": details}


def answer(people, start, end):
    first, second = people[0], people[1]
    rows = []
    for p in people:
        items, conflicts, density = [], [], []
        if p == first:
            items = [item(1, "rotation", "Fictional urgent care", "2026-10-01", "2026-12-01", rotation_type_id="t1"),
                     item(2, "leave", "Annual leave", "2026-10-18", "2026-10-30"),
                     item(3, "overtime_offer", "Overtime offer · 4 sessions", "2026-11-01", "2026-11-22"),
                     item(4, "eligibility", "Eligible: Fictional paediatric clinic", "2026-11-15", None),
                     item(5, "rank", "Rank → Consultant", "2026-12-01", None)]
            conflicts = [{"code": "DUTY_ON_LEAVE", "message": "2 duties on leave", "severity": "error", "person_id": first, "shift_id": "s1",
                          "start": "2026-10-22T00:00:00+03:00", "end": "2026-10-24T00:00:00+03:00", "detail": {}, "outside_rota": False}]
            density = [{"day": "2026-10-05", "count": 2}, {"day": "2026-10-06", "count": 1}, {"day": "2026-10-22", "count": 1}]
        if p == second:
            items = [item(6, "rotation", "Fictional elective", "2026-10-01", "2027-01-01", rotation_type_id="t2", department_available=False)]
            conflicts = [{"code": "LEAVE_NEEDS_APPROVAL", "message": "Needs approval · leave in Fictional elective", "severity": "warning", "person_id": second,
                          "start": "2026-11-02T00:00:00+03:00", "end": "2026-11-06T00:00:00+03:00", "detail": {}, "outside_rota": False}]
        rows.append({"id": p, "display_name": "", "short_code": "", "group": None, "items": items, "duty_density": density, "conflicts": conflicts})
    return {"start": start, "end": end, "timezone": "Asia/Riyadh", "total": len(rows), "offset": 0, "limit": 200, "has_more": False, "group_by": "none",
            "sort_by": "name", "sort_direction": "asc", "layers": LAYERS, "people": rows, "names": {}, "can_plan": True}


def serve(page, h):
    """The person timeline in the contract's shape for the department's own (fictional) people."""
    people = [p["id"] for p in h.get("coordinator", "/api/catalogues")["people"]]
    asked = []

    def reply(route):
        q = dict(parse_qsl(route.request.url.split("?", 1)[1], keep_blank_values=True))
        asked.append(q)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(answer(people, q["start"], q["end"])))
    page.route(re.compile(r".*/api/timeline\?.*"), reply)
    return people, asked


def test_people_timeline_reads_the_server_and_switches_on_by_itself(browser, live_server, all_roles):
    h = all_roles
    _, _, _, (trainee, _, _), _ = residency(h)
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        navigate(page, "people")
        set_layout(page, "Timeline")
        row = page.get_by_role("region", name="People timeline").locator(f'.tl-row[data-row="{trainee}"]')
        expect(row.get_by_role("button", name=re.compile(r"^Rotations: Emergency, 1 Oct – 31 Oct 2026"))).to_be_visible()
        panel = toolbar(page, "View settings")
        expect(panel.get_by_role("group", name="Layers").get_by_role("checkbox", name="Programme memberships", exact=True)).to_be_checked()
        close_popover(page)
    finally:
        context.close()


def test_people_timeline_is_offered_only_when_the_server_has_it(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        page.route(re.compile(r".*/api/timeline\?.*"), lambda r: r.fulfill(status=404, content_type="application/json",
                                                                      body=json.dumps({"error": {"code": "NOT_FOUND", "message": "Not found"}})))
        navigate(page, "people")
        layouts = toolbar(page, "View settings").get_by_role("radiogroup", name="Layout", exact=True)
        expect(layouts.get_by_role("radio", name="Table", exact=True)).to_be_visible()
        expect(layouts.get_by_role("radio", name="Timeline", exact=True)).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_people_timeline_draws_the_servers_layers_conflicts_and_density(browser, live_server, h, scheme):
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        page.emulate_media(color_scheme=scheme)
        people, asked = serve(page, h)
        navigate(page, "people")
        set_layout(page, "Timeline")
        board = page.get_by_role("region", name="People timeline")
        first = board.locator(f'.tl-row[data-row="{people[0]}"]')
        expect(first.get_by_role("button", name=re.compile(r"^Rotations: Fictional urgent care, 1 Oct – 30 Nov 2026"))).to_be_visible()
        expect(first.locator(".tl-leave [data-icon=plane]")).to_have_count(1)
        expect(first.locator(".tl-offer")).to_have_count(1)
        expect(first.locator(".tl-rail")).to_have_count(0)
        expect(first.locator(".tl-milestone")).to_have_count(1)
        # A conflict is red with an alert and its words; a warning is gold with a clock.
        expect(first.locator(".tl-zone:not(.approval) .tl-zbadge")).to_have_text("2 duties on leave")
        expect(first.locator(".tl-zone .tl-zbadge [data-icon=alert]")).to_have_count(1)
        second = board.locator(f'.tl-row[data-row="{people[1]}"]')
        expect(second.locator(".tl-zone.approval .tl-zbadge")).to_have_text("Needs approval · leave in Fictional elective")
        expect(second.locator(".tl-zone.approval [data-icon=clock]")).to_have_count(1)
        expect(second.locator("[data-icon=external]")).to_have_count(1)
        # Duty density: one cell a day, a duty on a leave day in red.
        expect(first.locator(".tl-heatmap .hx")).to_have_count(2)
        expect(first.locator(".tl-heatmap .h2")).to_have_count(1)
        # The shared key above the timeline: only the layers drawn (Eligibility is off; conflicts carry their words).
        key = page.locator(".tl-host > .view-legend.tl-legend")
        expect(key).to_be_visible()
        assert sorted(key.locator("li").all_inner_texts()) == ["Conflict", "Leave", "Needs approval", "Overtime offers", "Published duties", "Rank changes", "Rotations"]
        assert_legend_matches(page, root=".tl-host", scrollers=[".tl"], expect_marks=[
            "layer:rotation", "layer:leave", "layer:overtime_offer", "layer:rank", "layer:duty_density", "zone:error", "zone:approval"])
        if os.getenv("TIMELINE_SHOTS"):
            page.screenshot(path=os.path.join(os.environ["TIMELINE_SHOTS"], f"people-timeline-{scheme}.png"))
        # The layers are the server's: View settings lists them with their counts, and hiding one hides its bars.
        panel = toolbar(page, "View settings")
        layers = panel.get_by_role("group", name="Layers")
        for layer in LAYERS[:-1]:
            checkbox = layers.get_by_role("checkbox", name=layer["label"], exact=True)
            if layer["kind"] == "eligibility":
                expect(checkbox).not_to_be_checked()
            else:
                expect(checkbox).to_be_checked()
        assert 'eligibility' not in asked[-1]['layers'].split(',')
        layers.get_by_role("checkbox", name="Eligibility", exact=True).check()
        expect(board.locator(".tl-rail")).to_have_count(1)
        assert 'eligibility' in asked[-1]['layers'].split(',')
        expect(layers).to_contain_text("Always shown")
        layers.get_by_role("checkbox", name="Overtime offers", exact=True).uncheck()
        expect(board.locator(".tl-offer")).to_have_count(0)
        assert "overtime_offer" not in asked[-1]["layers"].split(",")
        panel = page.get_by_role("dialog", name="View settings", exact=True)
        panel.get_by_role("radiogroup", name="Range").get_by_role("radio", name="3 months").click()
        expect(page.locator(".tl-weekend")).to_have_count(13)
        close_popover(page)
        # Kept with the list: the layout, zoom and hidden layer come back after a reload.
        page.reload()
        expect_loaded(page, "people")
        expect(page.get_by_role("region", name="People timeline").locator(".tl-weekend")).to_have_count(13)
        expect(board.locator(".tl-offer")).to_have_count(0)
        # A bar opens its panel with the conflict that touches it.
        page.get_by_role("region", name="People timeline").get_by_role("button", name=re.compile(r"^Leave: Annual leave")).click()
        dock = page.locator(".dock-panel")
        expect(dock.get_by_role("heading", name="2 duties on leave")).to_be_visible()
        assert "200" in {q["limit"] for q in asked}  # one read of up to 200 people a page
    finally:
        context.close()


def test_people_timeline_groups_like_the_list_and_reads_as_an_agenda_on_a_phone(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        people, _ = serve(page, h)
        navigate(page, "people")
        set_layout(page, "Timeline")
        toolbar(page, "Group").get_by_role("menuitemradio", name="Status").click()
        board = page.get_by_role("region", name="People timeline")
        expect(board.get_by_role("button", name=re.compile("^Active"))).to_have_attribute("aria-expanded", "true")
        expect(board.locator(".tl-gsum")).to_contain_text("1 conflict")
        page.set_viewport_size({"width": 390, "height": 844})
        page.reload()
        expect_loaded(page, "people")
        agenda = page.locator(".tl-agenda")
        expect(agenda.get_by_role("heading", name=re.compile("^Elsewhere now"))).to_be_visible()
        page.get_by_role("button", name="Show the timeline", exact=True).click()
        expect(page.get_by_role("region", name="People timeline")).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_a_persons_schedule_shows_their_own_timeline(browser, live_server, all_roles, scheme):
    h = all_roles
    _, _, _, (trainee, _, _), _ = residency(h)
    name = next(p["display_name"] for p in h.get("coordinator", "/api/catalogues")["people"] if p["id"] == trainee)
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    asked = []
    page.on("request", lambda r: asked.append(r.url) if "/timeline?" in r.url else None)
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, "people")
        page.get_by_label("Search people", exact=True).fill(name)
        page.locator("#main .dt-body tbody tr, #main .dt-body .dt-card").filter(has_text=name).first.locator("strong").first.click()
        panel = page.get_by_role("region", name=name, exact=True)
        panel.get_by_role("tab", name="Schedule", exact=True).click()
        tab = panel.get_by_role("tabpanel", name="Schedule", exact=True).locator(".ph-section").filter(
            has=page.get_by_role("heading", name="Timeline", exact=True))
        timeline = tab.get_by_role("region", name="People timeline")
        expect(timeline.locator(".tl-row[data-row]")).to_have_count(1)
        expect(timeline.get_by_role("button", name=re.compile(r"^Rotations: Emergency, 1 Oct – 31 Oct 2026"))).to_be_visible()
        assert any(f"/api/people/{trainee}/timeline?" in u for u in asked)
        if os.getenv("TIMELINE_SHOTS"):
            page.screenshot(path=os.path.join(os.environ["TIMELINE_SHOTS"], f"person-timeline-{scheme}.png"))
        # A year at a time: Next moves the window and reads it; Today comes back.
        tab.get_by_role("button", name="Next", exact=True).click()
        expect(tab.get_by_role("button", name="Today", exact=True)).to_be_visible()
        tab.get_by_role("button", name="Today", exact=True).click()
        expect(timeline.get_by_role("button", name=re.compile(r"^Rotations: Emergency, 1 Oct – 31 Oct 2026"))).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize("zone,start,end,days", [
    ("Asia/Riyadh", "2026-10-21T21:30:00+00:00", "2026-10-21T23:00:00+00:00", [22]),        # 00:30–02:00 on 22 Oct in department-local
    ("America/New_York", "2026-10-22T04:00:00+00:00", "2026-10-23T04:00:00+00:00", [22]),  # local midnight to midnight (EDT)
    ("Asia/Riyadh", "2026-10-21T17:00:30+00:00", "2026-10-21T21:00:30+00:00", [21, 22])])  # 20:00:30 to 00:00:30: the next day too
def test_a_conflict_is_drawn_on_the_departments_day(browser, live_server, h, zone, start, end, days):
    """The server sends conflict instants in UTC; their days are the department's (the response's timezone)."""
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        people = [p["id"] for p in h.get("coordinator", "/api/catalogues")["people"]]
        asked = []

        def reply(route):
            q = dict(parse_qsl(route.request.url.split("?", 1)[1], keep_blank_values=True))
            asked.append(q)
            body = answer(people, q["start"], q["end"])
            body["timezone"] = zone
            body["people"][0]["conflicts"] = [{"code": "DUTY_ON_LEAVE", "message": "A duty on leave", "severity": "error", "person_id": people[0],
                                               "shift_id": "s1", "start": start, "end": end, "detail": {}, "outside_rota": False}]
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        page.route(re.compile(r".*/api/timeline\?.*"), reply)
        navigate(page, "people")
        set_layout(page, "Timeline")
        row = page.get_by_role("region", name="People timeline").locator(f'.tl-row[data-row="{people[0]}"]')
        expect(row.locator(".tl-heatmap .hx")).to_have_count(len(days))
        first = date.fromisoformat(asked[-1]["start"])
        index = row.locator(".tl-heatmap > span").evaluate_all("cs => cs.flatMap((c, i) => c.classList.contains('hx') ? [i] : [])")
        assert index == [(date(2026, 10, d) - first).days for d in days], index
    finally:
        context.close()


def test_a_persons_phone_timeline_is_their_own_agenda(browser, live_server, all_roles):
    """On a phone, a person's Schedule › Timeline reads as their own agenda: the rotation they are on now and the ones
    months ahead, not the department's two-week summary."""
    h = all_roles
    _, _, types, (trainee, _, _), _ = residency(h)
    name = next(p["display_name"] for p in h.get("coordinator", "/api/catalogues")["people"] if p["id"] == trainee)
    context, page = sign_in(browser, live_server, "coordinator", {"width": 390, "height": 844})
    try:
        navigate(page, "people")
        page.get_by_label("Search people", exact=True).fill(name)
        page.locator("#main .dt-body tbody tr, #main .dt-body .dt-card").filter(has_text=name).first.locator("strong").first.click()
        panel = page.get_by_role("region", name=name, exact=True)
        panel.get_by_role("tab", name="Schedule", exact=True).click()
        agenda = panel.locator(".tl-agenda")
        expect(agenda.get_by_role("heading", name=re.compile("^Now"))).to_be_visible()
        expect(agenda).to_contain_text(types[0]["name"])
        expect(agenda).to_contain_text(types[2]["name"])  # December: well past the next two weeks
        expect(agenda).not_to_contain_text("No one is away")
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_saved_timeline_survives_a_failed_read_and_retries(browser, live_server, h, scheme):
    """A saved Timeline layout whose reads fail with anything but 404 stays the Timeline,
    says what failed, and Retry draws it once the server answers."""
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, "people")
        set_layout(page, "Timeline")
        board = page.get_by_role("region", name="People timeline")
        expect(board.locator(".tl-row").first).to_be_visible()
        busy = re.compile(r".*/api/timeline\?.*")
        page.route(busy, lambda r: r.fulfill(status=503, content_type="application/json",
                                             body=json.dumps({"error": {"code": "BUSY", "message": "Server busy"}})))
        page.reload()
        expect_loaded(page, "people")
        failed = page.locator(".notice.error").filter(has_text="Couldn’t load the timeline.")
        expect(failed).to_be_visible()
        page.unroute(busy)
        failed.get_by_role("button", name="Retry", exact=True).click()
        expect(page.get_by_role("region", name="People timeline").locator(".tl-row").first).to_be_visible()
        expect(failed).to_have_count(0)
    finally:
        context.close()

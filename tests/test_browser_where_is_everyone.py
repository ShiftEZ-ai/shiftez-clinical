"""People › Where is everyone (approved mockup v2 + D48): the People list's Timeline layout as one line per person, a
built-in view. The server owns the line (TimelinePerson.segments, docs/design/where-is-everyone-api.md): which stretch
wins a day and the clinics' counts. These answers are fictional segments in that contract's shape for the list's own
people; the fake drops what `hide`, `hide_leave_types` and `source=published` ask it to (the real fall-through is the
server's, tested in tests/test_whereabouts.py). What is tested here is the drawing, the requests and the interactions."""
import json
import os
import re
from datetime import date, timedelta
from urllib.parse import parse_qsl

import pytest
from playwright.sync_api import expect

from .legend_guard import assert_legend_matches
from .test_browser import browser, choose, close_popover, condition, expect_loaded, filter_by, live_server, navigate, sign_in, sort_direction, toolbar  # noqa: F401

pytestmark = pytest.mark.browser

LAYERS = [{"kind": "rotation", "label": "Rotations", "style_key": "rotation", "count": 2, "source": "rotations"},
          {"kind": "leave", "label": "Leave", "style_key": "leave", "count": 2, "source": "availability"},
          {"kind": "academic", "label": "Academic time", "style_key": "academic", "count": 1, "source": "programmes"}]
ZZ = {"id": "a-ucc", "label": "Urgent cover", "short_name": "ZZ", "colour": None}
AA = {"id": "a-fm", "label": "Field medicine", "short_name": "AA", "colour": None}
PROC = {"id": "a-proc", "label": "Procedures", "short_name": "Proc", "colour": None}


# Window totals per person (ranked, the main one first): what Works in reads.
AREAS = {0: [{"id": "a-ucc", "label": "Urgent cover", "short_name": "ZZ", "count": 30}, {"id": "a-fm", "label": "Field medicine", "short_name": "AA", "count": 4}],
         2: [{"id": "a-proc", "label": "Procedures", "short_name": "Proc", "count": 20}]}


def month(start, n):
    d = date.fromisoformat(start)
    t = d.year * 12 + d.month - 1 + n
    return date(t // 12, t % 12 + 1, 1)


def seg(sid, kind, a, b, label, short, source="published", rota=None, **extra):
    return {"id": sid, "starts_on": str(a), "ends_on": str(b), "kind": kind, "label": label, "short_label": short, "source": source, "rota": rota,
            "areas": [], "clinics": [], **extra}


def work(sid, a, b, areas, clinics, source="published", rota="Fictional rota"):
    return seg(sid, "work", a, b, " · ".join(x["label"] for x in areas), " · ".join(x["short_name"] for x in areas), source, rota if source == "published" else None,
               areas=areas, clinics=[{"id": f"c{i}", "label": n, "count": c} for i, (n, c) in enumerate(clinics)])


def leave(sid, a, b, kind):
    return seg(sid, "leave", a, b, kind["name"], kind["short"], leave_type_id=kind["id"], leave_type=kind["name"], leave_short_name=kind["short"],
               leave_colour=None, status="recorded")


def answer(people, q, types):
    """p0 a consultant: ZZ with AA, Annual leave inside month 1, one Sick day in month 2 (a thin segment at 12 months),
    then a draft month; p1 a resident: an away Ophthalmology rotation with academic time on top, then a home Family
    medicine rotation; p2 Procedures (its own hue, never Ophtha's), then out of the department (it left)."""
    m = [month(q["start"], i) for i in range(5)]
    d = timedelta
    annual, sick = types
    line = {0: [work("w0", m[0], m[1], [ZZ, AA], [("Poly Urgent Clinic", 7), ("Virtual Urgent Cover", 3), ("AA Clinic", 4)]),
                work("w1", m[1], m[1] + d(9), [ZZ], [("Poly Urgent Clinic", 4)]), leave("l1", m[1] + d(9), m[1] + d(17), annual),
                work("w2", m[1] + d(17), m[2], [ZZ], [("Poly Urgent Clinic", 8)]),
                work("w3", m[2], m[2] + d(4), [ZZ], [("Poly Urgent Clinic", 2)], "draft"), leave("l2", m[2] + d(4), m[2] + d(5), sick),
                work("w4", m[2] + d(5), m[3], [ZZ], [("Poly Urgent Clinic", 7)], "draft")],
            1: [seg("r1", "away", m[0], m[1], "Ophthalmology", "Ophtha", rotation_type_id="t-oph"),
                seg("a1", "academic", m[1], m[1] + d(14), "Academic block", "Acad"),
                seg("r2", "away", m[1] + d(14), m[2], "Ophthalmology", "Ophtha", rotation_type_id="t-oph"),
                seg("r3", "home", m[2], m[4], "Field medicine", "AA", rotation_type_id="t-fm")],
            2: [work("w5", m[0], m[3], [PROC], [("Minor procedures", 20)]),
                # The server's outside-department stretch (rank memberships): this person left after month 3.
                seg("o1", "outside", m[3], m[4], "Left the department", "Out", reason="left")]}
    hide = set(filter(None, q.get("hide", "").split(","))) | ({"draft"} if q.get("source") == "published" else set())
    types_off = set(filter(None, q.get("hide_leave_types", "").split(",")))
    keep = lambda g: (g["kind"] not in hide and not (g["kind"] in ("away", "home") and "rotation" in hide) and not (g["kind"] == "protected" and "academic" in hide)
                      and g["source"] in (("published",) if "draft" in hide else ("published", "draft", "usual")) and g.get("leave_type_id") not in types_off)
    rows = [{"id": p, "display_name": "", "short_code": "", "group": None, "items": [], "duty_density": [], "conflicts": [],
             "areas": AREAS.get(i, []) if q.get("bucket") else None, "segments": [g for g in line.get(i, []) if keep(g)] if q.get("bucket") else None}
            for i, p in enumerate(people)]
    return {"start": q["start"], "end": q["end"], "timezone": "Asia/Riyadh", "total": len(rows), "offset": 0, "limit": 200, "has_more": False, "group_by": "none",
            "sort_by": "name", "sort_direction": "asc", "layers": LAYERS, "people": rows, "names": {}, "can_plan": False}


def kinds(h):
    """Two leave types in the department's catalogue (the Show › Leave chips read it): made once, before signing in."""
    have = {t["name"]: t for t in h.get("coordinator", "/api/catalogues")["leave_types"]}
    types = [have.get(name) or h.configure("leave_types", {"name": name, "short_code": code, "short_name": short})["record"]
             for name, code, short in (("Fictional annual leave", "FAL", "AL"), ("Fictional sick leave", "FSL", "Sick"))]
    return [{"id": t["id"], "name": t["name"], "short": t.get("short_name") or ""} for t in types]


def serve(page, h):
    """The fake timeline for the department's own people."""
    types = kinds(h)
    people = [p["id"] for p in h.get("coordinator", "/api/catalogues")["people"] if p.get("active") is not False]
    asked = []

    def reply(route):
        q = dict(parse_qsl(route.request.url.split("?", 1)[1], keep_blank_values=True))
        asked.append(q)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(answer(people, q, types)))
    page.route(re.compile(r".*/api/timeline\?.*"), reply)
    return people, asked


ALPHA = "Alpha fictional rank"


def ranks(h, people):
    """The fixture's people are all "Demo practitioners"; a rank created last (first A–Z) is given to the third, so the
    groups' order shows whether they read in the department's configured rank order (the catalogue's) or by name."""
    rank = h.configure("categories", {"name": ALPHA, "starts_on": "2020-01-01"})["record"]
    h.command("coordinator", "membership.create", {"person_id": people[2], "category_id": rank["id"], "starts_on": "2020-01-01"})
    return [c["name"] for c in h.get("coordinator", "/api/catalogues")["categories"]]


def row(board, pid):
    """A person's row (the third person is in two rank groups: its first)."""
    return board.locator(f'.tl-row[data-row="{pid}"]').first


CUT = "ns=>ns.filter(n=>n.scrollWidth>n.clientWidth+1||n.closest('.tl-bar').scrollWidth>n.closest('.tl-bar').clientWidth+1).map(n=>n.textContent)"


def where_is_everyone(page):
    """Choose the built-in view from the toolbar's Saved view menu."""
    toolbar(page, "Saved views").get_by_role("menuitemradio", name=re.compile(r"^Where is everyone")).click()
    board = page.get_by_role("region", name="People timeline")
    expect(board).to_be_visible()
    return board


def shot(page, name):
    if os.getenv("TIMELINE_SHOTS"):
        page.screenshot(path=os.path.join(os.environ["TIMELINE_SHOTS"], f"where-{name}.png"))


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_one_line_per_person_by_precedence_with_distinct_hues_and_no_orphan_dots(browser, live_server, h, scheme):
    people = [p["id"] for p in h.get("coordinator", "/api/catalogues")["people"] if p.get("active") is not False]
    order = ranks(h, people)
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        page.emulate_media(color_scheme=scheme)
        people, asked = serve(page, h)
        navigate(page, "people")
        board = where_is_everyone(page)
        # Saved view names the view; 12 months by month columns, starting two months back; only the drawn layers asked.
        expect(page.locator("button.dt-tool").filter(has_text="Where is everyone")).to_be_visible()
        expect(row(board, people[0]).locator(".tl-lane>.tl-bar").first).to_be_visible()
        q = [q for q in asked if q.get("bucket")][-1]
        assert q["bucket"] == "month" and q["layers"] == "" and "hide" not in q and "source" not in q  # no item layers: the segments
        today = date.fromisoformat(page.evaluate("new Date().toISOString().slice(0,10)"))
        s = month(str(today.replace(day=1)), -2)
        assert q["start"] == str(s) and q["end"] == str(month(str(s), 12))
        expect(page.locator(".dt-period strong")).to_have_text(re.compile(rf"^{s:%b} {s.year} – "))
        # A person with two current ranks is grouped once, under the first in the configured order.
        expect(board.locator(f'.tl-row[data-row="{people[2]}"]')).to_have_count(1)
        # Months only at 12 months (no quarter row).
        expect(board.locator(".tl-head .tl-r1")).to_have_count(0)
        expect(board.locator(".tl-head .tl-r2 .tl-cell")).to_have_count(12)
        # Grouped by rank in the department's configured rank order (the catalogue's).
        heads = board.locator(".tl-group .tl-gtoggle strong")
        expect(heads).to_have_text([n for n in order if n in ("Demo practitioners", ALPHA)])
        expect(board.locator(".tl-gsum span")).to_have_count(0)  # no group summaries on the line
        m = [month(str(s), i) for i in range(4)]
        a = row(board, people[0])
        bars = a.locator(".tl-lane>.tl-bar")
        labels = bars.evaluate_all("ns=>ns.map(n=>n.getAttribute('aria-label').split(', ').slice(1).join(', '))")
        # In the server's order: the two areas first, then Annual leave at its exact days, then the draft month last.
        # Stretches too narrow for any word (at 12 months: the 9 days of ZZ before the leave, the 4 draft days and the one
        # Sick day) fold into the stretch they touch, which says them ("also …") and lists them in its card.
        assert labels[0].startswith("Urgent cover · Field medicine, ") and "published · Fictional rota" in labels[0], labels
        leave_at = next(i for i, x in enumerate(labels) if x.startswith("Fictional annual leave, "))
        assert labels[leave_at + 1].startswith("Urgent cover, ") and labels[-1].endswith("draft"), labels
        assert not any(x.startswith("Fictional sick leave, ") for x in labels), labels
        assert any("also" in x and "Fictional sick leave" in x for x in labels), labels
        # Short names, never cut words: "ZZ · AA", "ZZ"; the 12-month leave shows its code.
        expect(bars.first.locator(".tl-lbl")).to_have_text("ZZ · AA")
        expect(a.locator(".tl-leave .tl-lbl").first).to_have_text("AL")
        # Not published yet: the same mark, hatched. The draft month's first 4 days and its Sick day are too short and
        # fold into the published ZZ before them; the rest of the draft month joins that stretch, whose words show
        # once (only the source changes).
        drafts = a.locator(".tl-bar.draft")
        expect(drafts).to_have_count(1)
        assert "repeating-linear-gradient" in drafts.first.evaluate("n=>getComputedStyle(n).backgroundImage")
        expect(a.locator(".tl-bar.joined")).to_have_count(1)
        expect(a.locator(".tl-bar.joined .tl-lbl")).to_have_count(0)
        # No dots or icons anywhere on the line: the words and the mark's form say it (View settings › Show is the key).
        expect(board.locator(".tl-lane .tl-dot, .tl-lane .icon, .tl-code")).to_have_count(0)
        # Words never end in "…" on the line (D48): what does not fit is shorter, or folded.
        assert board.locator(".tl-bar .tl-lbl").evaluate_all(CUT) == []
        # Short stretches on: the Sick day is a thin bar of its own, with a 24 × 24 px hit area (WCAG 2.2 SC 2.5.8).
        reads = len(asked)
        toolbar(page, "View settings").get_by_role("checkbox", name="Short stretches", exact=True).check()
        close_popover(page)
        sick = a.get_by_role("button", name=re.compile(r", Fictional sick leave, .*, recorded$"))
        expect(sick).to_have_class(re.compile(r"\bthin\b"))
        expect(sick.locator(".tl-lbl")).to_have_count(0)
        assert len(asked) == reads  # drawing only: no new read
        hit = sick.evaluate("""n=>{const r=n.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
          return [[x,y-10],[x+10,y],[x-10,y+10]].map(([a,b])=>document.elementFromPoint(a,b)===n)}""")
        assert hit == [True, True, True], hit
        # The resident: academic time over the away rotation, the away rotation ↗ over nothing, then home AA.
        b = row(board, people[1]).locator(".tl-lane>.tl-bar")
        names = b.evaluate_all("ns=>ns.map(n=>n.getAttribute('aria-label').split(', ')[1])")
        assert names == ["Ophthalmology", "Academic block", "Ophthalmology", "Field medicine"], names
        # Two weeks at 12 months fit "Acad" (its own short name, never "Acade…"), not the book too: dashed indigo, words.
        expect(row(board, people[1]).locator(".tl-protected .tl-lbl")).to_have_text("Acad")
        # An away rotation is a neutral outline (no area hue); a home rotation its area's tint, framed.
        oph = row(board, people[1]).locator(".tl-lane>.tl-bar").first
        expect(oph).to_have_class(re.compile(r"\bext\b"))
        assert "duty-" not in oph.get_attribute("class")
        expect(row(board, people[1]).locator(".tl-lane>.tl-bar.tl-outline")).to_have_count(1)
        # A distinct hue per area: Procedures never shares one with Urgent cover (D48).
        hue = lambda loc: re.search(r"duty-(\w+)", loc.get_attribute("class")).group(1)
        proc = row(board, people[2]).locator(".tl-lane>.tl-bar").first
        assert hue(proc) != hue(bars.first)
        # Outside the department is the server's stretch, drawn quietly and named in Who's available's words.
        out = row(board, people[2]).locator(".tl-lane>.tl-bar.tl-out")
        expect(out).to_have_count(1)
        assert "not in the department" in out.get_attribute("aria-label")
        # No legend in the footer: the key is View settings › Show.
        expect(page.locator(".app-footer .footer-legend")).to_have_count(0)
        # Rank headings span the row (never "Assistant Consultant -…"); no initials discs on the line.
        assert heads.evaluate_all("ns=>ns.filter(n=>n.scrollWidth>n.clientWidth+1).length") == 0
        expect(board.locator(".tl-av")).to_have_count(0)
        # People, not rows: the third person is in two rank groups and counts once.
        expect(board.locator(".tl-corner")).to_have_text(f"{len(people)} people")
        expect(row(board, people[0]).locator(".tl-nm small")).to_have_count(0)  # the name alone (its grade is the group)
        shot(page, f"1440-{scheme}")
    finally:
        context.close()


def test_view_settings_show_range_start_and_labels(browser, live_server, h):
    kinds(h)
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        people, asked = serve(page, h)
        navigate(page, "people")
        board = where_is_everyone(page)
        a = row(board, people[0])
        expect(a.locator(".tl-leave")).to_have_count(1)  # the one-day Sick leave is folded
        panel = toolbar(page, "View settings")
        show = panel.get_by_role("group", name="Show", exact=True)
        for name in ("Worked in", "Rotations", "Leave", "Academic or protected", "Not published yet"):
            expect(show.get_by_role("checkbox", name=name, exact=True)).to_be_checked()
        # Show is the key: each toggle carries the marks it draws.
        expect(show.locator(".tl-layer").filter(has_text="Not published yet").locator(".tl-swatch.draft")).to_have_count(1)
        expect(show.locator(".tl-layer").filter(has_text="Rotations").locator(".tl-swatch")).to_have_count(2)
        # Not in the department is always shown (the server always sends it): a key row, no checkbox.
        expect(show.locator(".tl-layer").filter(has_text="Not in the department")).to_contain_text("Always shown")
        expect(show.get_by_role("checkbox", name="Not in the department")).to_have_count(0)
        short = show.get_by_role("checkbox", name="Short stretches", exact=True)
        expect(short).not_to_be_checked()
        short.check()
        expect(a.locator(".tl-leave")).to_have_count(2)
        # A leave type off: only that type goes.
        sick = show.get_by_role("group", name="Leave types").get_by_role("button", name="Fictional sick leave")
        expect(sick).to_have_attribute("aria-pressed", "true")
        sick.click()
        expect(sick).to_have_attribute("aria-pressed", "false")
        expect(a.locator(".tl-leave")).to_have_count(1)
        assert asked[-1]["hide_leave_types"] == [t["id"] for t in h.get("coordinator", "/api/catalogues")["leave_types"] if t["name"] == "Fictional sick leave"][0]
        # Leave off: the leave goes, its days read as worked again, and its types hide with it.
        show.get_by_role("checkbox", name="Leave", exact=True).uncheck()
        expect(a.locator(".tl-leave")).to_have_count(0)
        assert asked[-1]["hide"] == "leave"  # the server lets its days fall through to what they were
        expect(show.get_by_role("group", name="Leave types")).to_be_hidden()
        # Two choices in one open popover both stay (Leave and Sick leave are both still hidden).
        show.get_by_role("checkbox", name="Not published yet", exact=True).uncheck()
        expect(a.locator(".tl-bar.draft")).to_have_count(0)
        assert asked[-1]["source"] == "published" and asked[-1]["hide"] == "leave" and "hide_leave_types" in asked[-1]
        expect(a.locator(".tl-leave")).to_have_count(0)
        show.get_by_role("checkbox", name="Academic or protected", exact=True).uncheck()
        expect(row(board, people[1]).locator(".tl-protected")).to_have_count(0)
        # Labels: full names; at 12 months a month is too narrow for them, so it keeps the short name (never "Urge…").
        panel.get_by_role("radiogroup", name="Labels").get_by_role("radio", name="Full").click()
        expect(a.locator(".tl-lbl").first).to_have_text("ZZ · AA")
        # Range: 3 months by week columns; Starts moves the window.
        period = page.locator(".dt-period strong")
        panel.get_by_role("radiogroup", name="Range").get_by_role("radio", name="3 months").click()
        expect(period).to_have_text(re.compile(r"^\d+ \w+ \d{4} – "))  # weeks: the days are named
        expect(row(board, people[0]).locator(".tl-lane>.tl-bar").first).to_be_visible()
        assert asked[-1]["bucket"] == "week"
        expect(a.locator(".tl-lbl").filter(has_text=re.compile(r"^Urgent cover"))).not_to_have_count(0)  # wide enough now
        shown = period.inner_text()
        later = month(asked[-1]["start"][:8] + "01", 2)
        panel.get_by_label("Starts", exact=True).select_option(str(later))
        expect(period).not_to_have_text(shown)
        assert asked[-1]["start"] > str(later - timedelta(days=7))
        close_popover(page)
        # Kept with the list: a reload shows the same settings.
        page.reload()
        expect_loaded(page, "people")
        board = page.get_by_role("region", name="People timeline")
        expect(row(board, people[0]).locator(".tl-leave")).to_have_count(0)
        expect(row(board, people[0]).locator(".tl-lbl").filter(has_text=re.compile(r"^Urgent cover"))).not_to_have_count(0)
        expect(page.locator("button.dt-tool").filter(has_text="Where is everyone")).to_be_visible()
    finally:
        context.close()


def test_hover_card_and_keyboard_through_segments(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        people, _ = serve(page, h)
        navigate(page, "people")
        board = where_is_everyone(page)
        first = row(board, people[0]).locator(".tl-lane>.tl-bar").first
        first.hover()
        card = page.get_by_role("tooltip")
        expect(card).to_contain_text("Urgent cover · Field medicine")
        for words in ("Dates", "Duration", "Poly Urgent Clinic · 7", "Virtual Urgent Cover · 3", "AA Clinic · 4", "Published · Fictional rota"):
            expect(card).to_contain_text(words)
        page.mouse.move(-1, -1)
        expect(page.get_by_role("tooltip")).to_have_count(0)
        # Keyboard: a row's first segment is its one tab stop; ← → move along it, ↓ to the row below; the card follows focus.
        bars = row(board, people[0]).locator(".tl-lane>.tl-bar")
        expect(bars.first).to_have_attribute("tabindex", "0")
        expect(bars.nth(1)).to_have_attribute("tabindex", "-1")
        row(board, people[0]).locator(".tl-who").focus()
        page.keyboard.press("Tab")
        expect(bars.first).to_be_focused()
        expect(bars.first).to_have_css("outline-style", "solid")
        page.keyboard.press("ArrowRight")
        expect(bars.nth(1)).to_be_focused()
        expect(bars.nth(1)).to_have_attribute("tabindex", "0")
        expect(page.get_by_role("tooltip")).to_contain_text("Dates")  # the focused stretch's card
        page.keyboard.press("End")
        expect(bars.last).to_be_focused()
        page.keyboard.press("Home")
        expect(bars.first).to_be_focused()
        page.keyboard.press("ArrowDown")
        focused = page.evaluate("document.activeElement.closest('.tl-row')?.dataset.row")
        assert focused and focused != people[0]
    finally:
        context.close()


def test_group_filter_and_sort_keep_the_line(browser, live_server, h):
    people = [p["id"] for p in h.get("coordinator", "/api/catalogues")["people"] if p.get("active") is not False]
    ranks(h, people)
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        people, _ = serve(page, h)
        navigate(page, "people")
        board = where_is_everyone(page)
        expect(page.locator("button.dt-tool").filter(has_text="Current rank")).to_be_visible()
        # Collapsing a group hides its rows; its heading stays, and a person also in another group stays there.
        toggle = board.locator(".tl-gtoggle").filter(has_text="Demo practitioners")
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        # The head is dom.ts groupToggle: closed is said in words on its count chip, never with an arrow (L-TIMELINE-1).
        expect(toggle.locator(".gt-count")).to_have_text(re.compile(r"^\d+ hidden$"))
        expect(board.locator(".tl-gtoggle [data-icon^=chevron]")).to_have_count(0)
        expect(board.locator(f'.tl-row[data-row="{people[0]}"]')).to_have_count(0)
        expect(board.locator(f'.tl-row[data-row="{people[2]}"]')).to_have_count(1)
        toggle.click()
        # Search narrows the rows; the line stays one line.
        name = next(p["display_name"] for p in h.get("coordinator", "/api/catalogues")["people"] if p["id"] == people[0])
        page.get_by_role("searchbox", name="Search people").fill(name)
        expect(board.locator(".tl-row")).to_have_count(1)
        expect(row(board, people[0]).locator(".tl-lane>.tl-bar").first).to_be_visible()
        page.get_by_role("searchbox", name="Search people").fill("")
        # Filter by rank: only that rank's people, still one line each.
        filter_by(page, "Current rank", [ALPHA])
        expect(board.locator(".tl-row")).to_have_count(1)  # the third person, once
        expect(board.locator(f'.tl-row[data-row="{people[0]}"]')).to_have_count(0)
        toolbar(page, "Filter").get_by_role("button", name="Clear all filters").click()
        close_popover(page)
        expect(board.locator(f'.tl-row[data-row="{people[0]}"]')).to_have_count(1)
        # Sort by name Z → A turns the first two round inside their group.
        pair = lambda: [r for r in board.locator(".tl-row").evaluate_all("ns=>ns.map(n=>n.dataset.row)") if r in people[:2]]
        choose(page, "Sort", "Sort by", "Person")
        expect(board.locator(f'.tl-row[data-row="{people[1]}"]')).to_have_count(1)
        before = pair()
        sort_direction(page, "Descending")
        close_popover(page)
        expect(board.locator(f'.tl-row[data-row="{before[1]}"] ~ .tl-row[data-row="{before[0]}"]')).to_have_count(1)
        # Works in: Group by area puts each person under their main area once; Filter "works in" matches any of theirs.
        choose(page, "Group", "Group by", "Works in")
        heads = board.locator(".tl-group .tl-gtoggle strong")
        expect(heads.filter(has_text="Urgent cover")).to_have_count(1)
        expect(heads.filter(has_text="Field medicine")).to_have_count(0)
        expect(heads.filter(has_text="Procedures")).to_have_count(1)
        filter_by(page, "Works in", ["Field medicine"])
        expect(board.locator(".tl-row")).to_have_count(1)
        expect(board.locator(f'.tl-row[data-row="{people[0]}"]')).to_have_count(1)
    finally:
        context.close()


def test_works_in_fills_the_table_from_the_line_read(browser, live_server, h):
    """Works in is a People column (hidden by default): in the Table, filtering by it reads the line's default window."""
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        people, asked = serve(page, h)
        navigate(page, "people")
        rows = page.locator("#main .dt tbody tr[data-row]")
        expect(rows.first).to_be_visible()
        # Read lazily: opening People reads nothing for Works in; opening Filter reads it and offers its values.
        assert not [q for q in asked if q.get("bucket")], asked
        held, line = [], re.compile(r".*/api/timeline\?.*bucket=month.*")
        page.route(line, lambda route: held.append(route))
        with page.expect_request(line):
            panel = toolbar(page, "Filter")
        # 695e43ed9: the open builder stays and redraws its rows in place (setFields), so the rows are what is new.
        panel.locator(".dt-filter-builder .cond-level").first.evaluate("n => n.dataset.before = 1")
        page.unroute(line)
        held.pop().fallback()
        # The values land while the popover is open: it offers them without being reopened.
        expect(panel.locator(".dt-filter-builder .cond-level:not([data-before])")).to_be_visible()
        condition(panel, "Works in", ["Procedures"])
        close_popover(page)
        expect(rows).to_have_count(1)
        expect(page.locator(f'#main tr[data-row="{people[2]}"]')).to_be_visible()
        q = [q for q in asked if q.get("bucket")][-1]
        assert q["bucket"] == "month" and q["layers"] == ""
    finally:
        context.close()


def test_a_phone_opens_people_as_where_is_everyone(browser, live_server, h):
    """With the phone scope on (no Continue anyway), People opens as the line, not "Best on a desktop": one person per
    row, the months swipe inside the timeline under sticky names, and the page itself never scrolls sideways."""
    context, page = sign_in(browser, live_server, "coordinator", {"width": 390, "height": 844}, phone_notice=True)
    try:
        people, asked = serve(page, h)
        navigate(page, "people")
        board = page.get_by_role("region", name="People timeline")
        expect(board).to_be_visible()
        expect(page.locator("#main .desktop-only")).to_have_count(0)
        expect(page.locator("#main button.dt-tool").filter(has_text="Where is everyone")).to_have_count(0)  # pinned: no Saved view menu
        expect(page.locator("#main .dt-toolbar")).to_be_visible()
        who = row(board, people[0]).locator(".tl-who")
        expect(row(board, people[0]).locator(".tl-lane>.tl-bar").first).to_be_visible()
        # The view's 12 months, wider than the screen: it opens near today and swipes; the names stay put.
        assert asked[-1]["bucket"] == "month"
        assert 365 <= (date.fromisoformat(asked[-1]["end"]) - date.fromisoformat(asked[-1]["start"])).days <= 366
        assert board.evaluate("n=>n.scrollWidth>n.clientWidth&&n.scrollLeft>0")
        x = who.evaluate("n=>n.getBoundingClientRect().left")
        board.evaluate("n=>{n.scrollLeft+=250}")
        assert who.evaluate("n=>n.getBoundingClientRect().left") == x
        # The group labels stay too (QA-062: a stretched sticky label scrolled away, "…stration" at x=-17).
        lefts = board.locator(".tl-gtoggle").evaluate_all("ns=>ns.map(n=>n.getBoundingClientRect().left)")
        edge = board.evaluate("n=>n.getBoundingClientRect().left+n.clientLeft")
        assert lefts and min(lefts) >= edge - 0.5, (lefts, edge)
        # A person's record is desktop work (M29): the name is not a button on a phone.
        assert who.evaluate("n=>n.tagName") == "DIV"
        # One person per row: every row is one line, the same height whatever it holds.
        heights = board.locator(".tl-row").evaluate_all("ns=>[...new Set(ns.map(n=>n.getBoundingClientRect().height))]")
        assert len(heights) == 1, heights
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        shot(page, "390-phone")
    finally:
        context.close()


def test_a_resized_window_keeps_the_line_and_its_range(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        people, asked = serve(page, h)
        navigate(page, "people")
        board = where_is_everyone(page)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(row(board, people[0]).locator(".tl-lane>.tl-bar").first).to_be_visible()
        expect(page.locator(".dt-period strong")).to_have_text(re.compile(r"^\w{3} \d{4} – \w{3} \d{4}$"))  # still 12 months
        expect(board.locator(".tl-lane .tl-dot, .tl-lane .icon")).to_have_count(0)
        assert board.locator(".tl-bar .tl-lbl").evaluate_all(CUT) == []
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        shot(page, "390-light")
        page.emulate_media(color_scheme="dark")
        shot(page, "390-dark")
    finally:
        context.close()


def test_an_admin_sets_which_areas_a_month_names_with_undo(browser, live_server, h):
    """Configuration › Department › Where is everyone: the area share and most areas are a versioned display setting
    (WhereaboutsSettings; the server's projection reads it, tests/test_whereabouts.py), saved with a reason, with Undo."""
    from .test_browser import open_config
    context, page = sign_in(browser, live_server, "admin")
    try:
        open_config(page, "Department settings")
        page.get_by_label("Share of a column’s duties that names an area (%)").fill("50")
        page.get_by_label("Areas named at most").fill("1")
        page.locator("section.panel").filter(has=page.get_by_role("heading", name="Where is everyone")).get_by_role("button", name="Save", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Reason", exact=True).fill("One main area a month")
        dialog.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_text("Where is everyone settings saved.")).to_be_visible()
        assert h.get("staff", "/api/me")["department"]["display_settings"]["whereabouts"] == {"area_share": 50, "max_areas": 1}
        page.get_by_role("button", name="Undo").first.click()
        expect(page.get_by_label("Share of a column’s duties that names an area (%)")).to_have_value("25")
    finally:
        context.close()


def test_the_legend_lists_the_marks_drawn_and_hides_from_view_settings(browser, live_server, h):
    """Owner, 7 Nov 2026: "no legend!". The shared key (dom.ts `legend`) sits above the timeline and lists only the marks
    this view and range draw, with a swatch and a word each; areas in the department's area order (name order); a bare
    short label with its full name. View settings › Show › Legend hides it, saved with the view, with no new read."""
    kinds(h)
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        people, asked = serve(page, h)
        navigate(page, "people")
        board = where_is_everyone(page)
        key = page.locator(".tl-host > .view-legend.tl-legend")
        expect(key).to_be_visible()
        assert key.evaluate("n=>n.getBoundingClientRect().bottom") <= board.evaluate("n=>n.getBoundingClientRect().top") + 1  # above it
        items = key.locator("li")
        words = items.all_inner_texts()
        for mark in ("Worked in", "Rotation", "Rotation elsewhere", "Leave", "Academic or protected", "Not in the department", "Not published yet"):
            assert mark in words, words
        # Every entry has its swatch; at most two lines.
        expect(items.locator(".tl-swatch, .code-chip")).to_have_count(len(words))
        assert key.evaluate("n=>n.getBoundingClientRect().height") <= 50
        # The guard (tests/legend_guard.py): every mark drawn is stamped with its entry, every entry has a mark, no mark
        # without one; every row is laid out, so the timeline is scrolled through.
        assert_legend_matches(page, root=".tl-host", scrollers=[".tl"], expect_marks=[
            "mark:work", "mark:home", "mark:away", "mark:leave", "mark:protected", "mark:outside", "mark:draft",
            "area:AA", "area:ZZ", "area:Proc", "code:Ophtha", "code:AL"])
        # Areas: their colour with short and full name, in name order whichever led a month.
        shorts = [w.split(" ")[0] for w in words if w in ("AA Field medicine", "Proc Procedures", "ZZ Urgent cover")]
        assert shorts == ["AA", "Proc", "ZZ"], words
        # A bare code drawn on the line (Ophtha) is in the key with its full name; the card says both too.
        expect(items.filter(has=page.locator(".code-chip", has_text="Ophtha")).locator(".lg-words")).to_have_text("Ophthalmology")
        row(board, people[1]).locator(".tl-lane>.tl-bar").first.hover()
        card = page.get_by_role("tooltip")
        expect(card.locator(".code-chip")).to_have_text("Ophtha")
        expect(card).to_contain_text("Ophthalmology")
        page.mouse.move(-1, -1)
        # Only what is drawn: Leave off removes its mark and its code.
        panel = toolbar(page, "View settings")
        show = panel.get_by_role("group", name="Show", exact=True)
        show.get_by_role("checkbox", name="Leave", exact=True).uncheck()
        expect(items.filter(has_text="Leave")).to_have_count(0)
        expect(items.filter(has=page.locator(".code-chip", has_text="AL"))).to_have_count(0)
        # Legend off: hidden, drawing only (no new read), and kept with the view.
        reads = len(asked)
        legend_switch = show.get_by_role("checkbox", name="Legend", exact=True)
        expect(legend_switch).to_be_checked()
        legend_switch.uncheck()
        expect(page.locator(".tl-host > .view-legend")).to_have_count(0)
        expect(row(board, people[0]).locator(".tl-lane>.tl-bar").first).to_be_visible()
        assert len(asked) == reads
        close_popover(page)
        page.reload()
        expect_loaded(page, "people")
        expect(page.get_by_role("region", name="People timeline")).to_be_visible()
        expect(page.locator(".tl-host > .view-legend")).to_have_count(0)
    finally:
        context.close()


def test_a_phone_shows_the_same_compact_legend(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator", {"width": 390, "height": 844}, phone_notice=True)
    try:
        people, _ = serve(page, h)
        navigate(page, "people")
        board = page.get_by_role("region", name="People timeline")
        key = page.locator(".tl-host > .view-legend.tl-legend")
        expect(key).to_be_visible()
        expect(key.locator("li").filter(has_text="Worked in")).to_have_count(1)
        # A key wraps onto the lines it needs and never scrolls, clips or hides an entry (eb8635b6d, UIUX-AUDIT L1): on a
        # phone it may take more lines than on a desktop, but never scrolls inside itself.
        assert key.evaluate("n=>n.scrollHeight<=n.clientHeight+1&&n.scrollWidth<=n.clientWidth+1")
        assert_legend_matches(page, root=".tl-host", scrollers=[".tl"])
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        expect(board.locator(".tl-lane>.tl-bar").first).to_be_visible()
    finally:
        context.close()

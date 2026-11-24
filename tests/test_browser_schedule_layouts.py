"""Schedule ranges and layouts from View settings: Month as week bands with "+N more", 3 days and Custom ranges, the
Days & time page (weekdays, week start, time window) kept in a saved view, and the key pinned on one line with overflow."""
import os
import re
import pytest
from playwright.sync_api import expect
from .legend_guard import legend_state
from .test_browser import browser, live_server, sign_in, navigate, toolbar, close_popover, view_settings, choose_radio, save_dialog  # noqa: F401

pytestmark = pytest.mark.browser
SHOTS = os.getenv("VIEW_SHOTS")


def shot(page, name):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, name + ".png"))


def busy_day(h, sessions=5):
    """One draft with `sessions` sessions on Sunday 4 October (one person each), so a month cell overflows."""
    w = h.workspace("Busy October", "2026-10-01", "2026-11-01")
    people = h.seed["people"]
    for i in range(sessions):
        hour = 7 + i * 2
        w = h.shift(w, f"2026-10-04T{hour:02d}:00:00+03:00", f"2026-10-04T{hour + 1:02d}:00:00+03:00", people=[people[i % len(people)]])["workspace"]
    return w


def test_month_is_the_week_grid_in_bands_with_more(browser, live_server, h):
    w = busy_day(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, f"schedule/month/2026-10-04?edit={w['id']}")
        grid = page.locator(".wk-grid.mo")
        expect(grid).to_be_visible()
        assert grid.locator(".mo-wk").count() >= 5  # one band per week of October
        expect(grid.locator(".wk-day.mo-out").first).to_be_visible()  # September or November fills the first or last band
        cell = grid.get_by_role("gridcell", name=re.compile(r"^4 Oct 2026 · "))
        # Comfortable shows two people, then "+3", which lists the whole lane for that day.
        expect(cell.locator(".pchip:not(.more)")).to_have_count(2)
        tops = cell.evaluate("c=>[...c.children].filter(n=>n.matches('.pchip,.team-box,.wk-open-row')).map(n=>n.getBoundingClientRect().top)")
        assert len(tops) >= 2 and all(round(b - a, 1) >= 24 for a, b in zip(tops, tops[1:])), tops  # WCAG 2.2 SC 2.5.8 spacing: 20 px pills at a 24 px pitch
        more = cell.get_by_role("button", name=re.compile(r"^3 more entries on 4 Oct 2026, .+: show all$"))
        expect(more).to_have_text("+3")  # +N everywhere, never "+N more" (DESIGN D8, 1aeb88039); its name says the rest
        more.click()
        expect(page.locator(".popover .mo-more .pchip")).to_have_count(5)
        shot(page, "month-more")
        close_popover(page)
        # Compact fits three.
        choose_radio(view_settings(page.locator(".planner-toolbar")), "Density", "Compact")
        close_popover(page)
        expect(grid.get_by_role("gridcell", name=re.compile(r"^4 Oct 2026 · ")).locator(".pchip:not(.more)")).to_have_count(3)
        # Pinned under the schedule; one line, with full entries in its overflow popover.
        key = page.locator("#main .view-legend:visible").first
        expect(key).to_be_visible()
        assert key.evaluate("k => [getComputedStyle(k).position, getComputedStyle(k).flexWrap]") == ["sticky", "nowrap"]
        assert grid.evaluate("g => g.compareDocumentPosition(document.querySelector('#main .view-legend')) & Node.DOCUMENT_POSITION_FOLLOWING")
        state = legend_state(page)
        assert not state["clipped"], state["clipped"]
        assert not state["cut"], state["cut"]
        shot(page, "month")
    finally:
        context.close()


def test_month_lanes_name_each_clinic_in_full_and_people_by_short_name(browser, live_server, h):
    """Two clinics whose names start alike stay tellable apart: each lane name gets two lines, with the full name as its
    tooltip and accessible name. Month's narrow columns name people by their short name unless the viewer chose a style."""
    from apps.api import models as m
    from apps.api.db import transaction
    clinics = ('Family Medicine Clinic', 'Family Medicine Virtual Clinic')
    with transaction(h.app.state.engine, write=True) as db:
        for sid, name in zip(h.seed['services'], clinics):
            db.get(m.Service, sid).name = name
    w = busy_day(h, 1)
    w = h.shift(w, "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00", service=1)["workspace"]
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, f"schedule/month/2026-10-04?edit={w['id']}")
        grid = page.locator(".wk-grid.mo")
        for name in clinics:
            head = grid.get_by_role("rowheader", name=name, exact=True).first
            expect(head).to_be_visible()
            label = head.locator(".clamp-2")
            expect(label).to_have_attribute("title", name)
            assert label.evaluate("n => n.scrollHeight <= n.clientHeight + 1"), name  # the whole name fits its two lines
        expect(grid.get_by_role("gridcell", name=re.compile(r"^4 Oct 2026 · ")).first.locator(".pchip").first).to_contain_text("A. Morgan")
    finally:
        context.close()


SPLIT_WORDS = """() => [...document.querySelectorAll('#schedule-view .clamp-2')].flatMap(n => {
    const text = n.firstChild; if (!text || text.nodeType !== 3) return [];
    return [...text.data.matchAll(/[^\\s-]+/g)].filter(m => {
        const r = document.createRange(); r.setStart(text, m.index); r.setEnd(text, m.index + m[0].length);
        return new Set([...r.getClientRects()].map(x => Math.round(x.top))).size > 1;  // one word drawn on two lines
    }).map(m => m[0]);
})"""


def test_lane_and_group_names_wrap_between_words_only(browser, live_server, h):
    """Lane and group heads in Week, Month and the Day timeline take two lines and break only between words: a word
    never splits ("Teleconsultati / on"; a hyphen may end a line); a word longer than the column ends in "…", with the full name as tooltip."""
    from .test_browser import choose
    names = ('Distinguished Clinic', 'Teleconsultation', 'Urgent Care Walk-in Overnight Coverage', 'Otorhinolaryngologicalservices')
    w = h.workspace("Long names", "2026-10-01", "2026-11-01")
    for i, name in enumerate(names):
        typ = h.configure('assignment_types', {'name': name, 'starts_on': '2020-01-01', 'definition': {}})['record']['id']
        w = h.shift(w, f"2026-10-04T{8 + i:02d}:00:00+03:00", f"2026-10-04T{9 + i:02d}:00:00+03:00", typ=typ)["workspace"]
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1280, "height": 900})
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        choose(page.locator(".planner-toolbar"), "Group", "Group by", "Duty type")
        for route in ("week", "month", "day"):
            navigate(page, f"schedule/{route}/2026-10-04?edit={w['id']}")
            expect(page.locator("#schedule-view .clamp-2").filter(has_text="Teleconsultation").first).to_be_visible()
            assert page.evaluate(SPLIT_WORDS) == [], route
        long = page.locator("#schedule-view .clamp-2").filter(has_text="Otorhinolaryngologicalservices").first
        expect(long).to_have_attribute("title", "Otorhinolaryngologicalservices")
    finally:
        context.close()


def test_people_chips_in_narrow_day_columns_are_not_cut(browser, live_server, h):
    """Two weeks on a phone leave ~30px per day: a chip shows its duty code whole and drops the time (still in its name),
    instead of cutting "08:00–10:00" at both edges."""
    w = busy_day(h, 3)
    context, page = sign_in(browser, live_server, "coordinator", {"width": 390, "height": 844})
    try:
        navigate(page, f"schedule/people/2026-10-04?edit={w['id']}")
        chips = page.locator(".people-grid .pg-cell .pg-chip")
        expect(chips.first).to_be_visible()
        expect(chips.first).to_have_accessible_name(re.compile(r"\d\d:\d\d–\d\d:\d\d"))
        cut = chips.evaluate_all("cs => cs.filter(c => c.scrollWidth > c.clientWidth + 1 || [...c.children].some(x => x.scrollWidth > x.clientWidth + 1)).length")
        assert cut == 0
        expect(page.locator(".people-grid .pg-chip-time").first).to_be_hidden()
    finally:
        context.close()


def test_ranges_and_days_and_time_are_kept_in_a_saved_view(browser, live_server, h):
    w = busy_day(h, 2)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        bar = page.locator(".planner-toolbar")
        heads = page.locator(".wk-grid .wk-head .wk-day")
        panel = view_settings(bar)
        choose_radio(panel, "Range", "3 days")
        expect(heads).to_have_count(3)
        choose_radio(panel, "Range", "Custom")
        days = panel.get_by_label("Days in the range", exact=True)
        days.fill("10")
        days.press("Enter")
        expect(heads).to_have_count(10)
        choose_radio(panel, "Range", "Week")
        expect(heads).to_have_count(7)
        # Days & time: hide Friday and Saturday, start the week on Monday.
        panel = view_settings(bar, "Days")
        panel.get_by_role("button", name="Friday", exact=True).click()
        panel.get_by_role("button", name="Saturday", exact=True).click()
        expect(panel.get_by_role("button", name="Saturday", exact=True)).to_have_attribute("aria-pressed", "false")
        expect(heads).to_have_count(5)
        panel.get_by_label("Week starts on", exact=True).select_option("0")
        expect(heads.first).to_contain_text("Mon")
        shot(page, "days")
        close_popover(page)
        toolbar(bar, "Saved views").get_by_role("menuitem", name="New view…", exact=True).click()
        form = page.get_by_role("dialog", name="New view")
        form.get_by_label("View name", exact=True).fill("Weekdays from Monday")
        save_dialog(form, "Save view")
        planner = h.get("coordinator", "/api/table-views?table=planner")["views"][0]["definition"]["planner"]
        assert planner["weekdays"] == [0, 1, 2, 3, 6] and planner["week_start"] == 0
        expect(bar.locator(".dt-saved .dt-tool-label")).to_have_text("Weekdays from Monday")
        page.reload()
        expect(heads).to_have_count(5)
        expect(heads.first).to_contain_text("Mon")
        # The time window narrows the Day layout to those hours.
        panel = view_settings(bar)
        choose_radio(panel, "Range", "Day")
        panel = view_settings(bar, "Days")
        panel.get_by_label("From", exact=True).fill("08:00")
        panel.get_by_label("To", exact=True).fill("12:00")
        panel.get_by_label("To", exact=True).press("Tab")
        close_popover(page)
        expect(page.locator(".sd-axis span")).to_have_count(4)
        expect(page.locator(".sd-axis span").first).to_have_text("08:00")
    finally:
        context.close()


def test_month_open_in_week_keeps_the_editable_draft(browser, live_server, h):
    """SR-08 / LIVE-43: two drafts on one day; Month's "Open in Week" keeps the draft being edited, and a dated Year
    address does not attach a draft that only covers the day's week."""
    first = h.shift(h.workspace('Edited draft', '2026-10-04', '2026-10-11'))['workspace']
    h.shift(h.workspace('Competing draft', '2026-10-04', '2026-10-11'))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        # The day calendar with "Open in Week" is Year's (Month is the session grid).
        navigate(page, f"schedule/year/2026-10-05?edit={first['id']}")
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule', timeout=45000)
        page.locator('.sd-yday[aria-label^="5 Oct 2026"]').click()
        page.locator('.sd-panel').get_by_role('button', name='Open in Week').click()
        expect(page.locator('.rota-switcher h1')).to_have_text('Edited draft')
        assert f"edit={first['id']}" in page.evaluate('location.hash')
        navigate(page, 'schedule/year/2026-10-11')
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule', timeout=45000)
        assert 'edit=' + first['id'] not in page.evaluate('location.hash')
    finally:
        context.close()

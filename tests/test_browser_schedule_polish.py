"""Schedule polish: every view scrolls in its own box under the toolbar (the page never scrolls, the column header stays
in sight: Week, Day, People, Timeline, Coverage, List); a failed load retries in its message; a Timeline cell with
several duties stacks one chip per duty code with its count instead of "first chip +N"."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, expect_loaded  # noqa: F401

pytestmark = pytest.mark.browser
SHORT = {"width": 1440, "height": 560}  # a short window, so a small rota already overflows

# The view's scroll box (nearest scrolling ancestor of its first column header), scrolled to the end.
SCROLL = """region => {
  const head = region.querySelector('thead th, [role=columnheader]');
  let box = head.parentElement;
  while (box && !(['auto', 'scroll'].includes(getComputedStyle(box).overflowY) && box.scrollHeight > box.clientHeight + 4)) box = box.parentElement;
  const main = document.querySelector('.planner-main');
  if (!box || !region.contains(box)) return {overflows: false};
  box.scrollTop = box.scrollHeight;
  return {overflows: true, gap: Math.abs(head.getBoundingClientRect().top - box.getBoundingClientRect().top),
          page: main.scrollHeight - main.clientHeight};
}"""


def busy_week(h):
    """Twelve overlapping sessions on Sunday 4 October (stacked lanes in Day, chips in Week) and eight more on other
    days at distinct hours (more Coverage rows), with the seed's people, services and places in turn."""
    w = h.workspace("Sticky headers", "2026-10-04", "2026-10-11")
    people, services, locations = h.seed["people"], h.seed["services"], h.seed["locations"]
    times = [(4, 8)] * 12 + [(5 + i % 5, 7 + i) for i in range(8)]
    for i, (day, hour) in enumerate(times):
        w = h.shift(w, f"2026-10-{day:02d}T{hour:02d}:00:00+03:00", f"2026-10-{day:02d}T{hour + 3:02d}:00:00+03:00",
                    people=[people[i % len(people)]], service=i % len(services), location=i % len(locations))["workspace"]
    h.lifecycle(w, "validate")
    return w


@pytest.mark.parametrize("view,label", [("week", "Week"), ("day", "Day"), ("people", "People"), ("timeline", "Timeline"),
                                        ("coverage", "Coverage"), ("list", "List")])
def test_schedule_view_scrolls_in_its_own_box_with_header_in_sight(browser, live_server, h, view, label):
    w = busy_week(h)
    context, page = sign_in(browser, live_server, "coordinator", viewport=SHORT)
    try:
        navigate(page, f"schedule/{view}/2026-10-04?edit={w['id']}")
        region = page.get_by_role("region", name=f"{label} view", exact=True)
        expect(region.locator("thead th, [role=columnheader]").first).to_be_visible()
        result = region.evaluate(SCROLL)
        assert result["overflows"], "the view should overflow this short window"
        assert result["gap"] <= 2, result  # the header stays at the top of the scroll box
        assert result["page"] <= 1, result  # nothing outside the view stretches the page
        if view == "week":  # a week of a longer loaded window is not "filtered" when no filter is on
            expect(region.get_by_role("columnheader", name=re.compile(r"^4 Oct 2026 · \d+ of \d+ places filled · the draft starts$"))).to_be_visible()
    finally:
        context.close()


def test_published_schedule_failure_retries_in_the_message_without_a_count(browser, live_server, h):
    context, page = sign_in(browser, live_server, "approver")
    try:
        page.route("**/api/schedule/sessions*", lambda route: route.abort())
        page.evaluate("location.hash = 'schedule/week/2026-12-06'")
        expect_loaded(page, 'schedule/week/2026-12-06')  # This journey deliberately loads an alert.
        alert = page.locator("#schedule-view").get_by_role("alert")
        expect(alert).to_contain_text("Couldn’t load the published sessions.")
        retry = alert.get_by_role("button", name="Retry", exact=True)  # in the message, as on every page
        expect(retry).to_be_visible()
        expect(page.locator(".app-footer .footer-total")).to_have_text("")  # nothing loaded: no "0 sessions"
        page.unroute("**/api/schedule/sessions*")
        retry.click()
        expect(alert).to_have_count(0)
        expect(page.locator(".app-footer .footer-total").first).to_have_text("0 sessions in view")
    finally:
        context.close()


def test_timeline_cell_counts_a_repeated_duty_code_and_lists_it(browser, live_server, h):
    w = h.workspace("Twice a day", "2026-10-04", "2026-10-11")
    person = h.seed["people"][0]
    for start, end in (("08:00", "10:00"), ("13:00", "15:00")):
        w = h.shift(w, f"2026-10-04T{start}:00+03:00", f"2026-10-04T{end}:00+03:00", people=[person])["workspace"]
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, f"schedule/timeline/2026-10-04?edit={w['id']}")
        region = page.get_by_role("region", name="Timeline view", exact=True)
        both = region.get_by_role("button", name=re.compile(r"^2 .+ sessions, "))
        expect(both).to_have_count(1)
        expect(both).to_have_text(re.compile(r"^2×"))
        expect(region.locator(".pg-cell .tl-more")).to_have_count(0)  # no "first chip +1"
        both.click()
        expect(page.locator(".popover .tl-day li")).to_have_count(2)
    finally:
        context.close()

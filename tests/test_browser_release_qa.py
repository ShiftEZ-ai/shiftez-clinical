"""Release-QA regressions: required dropdowns, toast stacking, the phone published list and the phone planner bar."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser
PHONE = {"width": 390, "height": 844}


def test_required_dropdown_starts_on_choose_and_blocks_an_untouched_save(browser, live_server, h):
    # The header form without a person picked in advance (ranks for one person are a row action).
    context, page = sign_in(browser, live_server, "credentialer")
    try:
        navigate(page, "people")
        page.get_by_role("button", name="Authorise eligibility", exact=True).click()
        dialog = page.get_by_role("dialog")
        person = dialog.get_by_label("Person", exact=True)
        expect(person).to_have_value("")
        assert person.evaluate("s => s.selectedOptions[0].textContent") == "Choose…"
        # The shared picker lists real choices only, with nothing pre-ticked.
        person.click()
        picker = page.locator(".picker")
        expect(picker).to_be_visible()
        expect(picker.get_by_text("Choose…", exact=True)).to_have_count(0)
        expect(picker.locator("[aria-selected=true]")).to_have_count(0)
        page.keyboard.press("Escape")
        # An untouched submit is stopped by the browser: nothing is saved for "the first person".
        before = len(h.get("credentialer", "/api/catalogues")["eligibilities"])
        dialog.get_by_role("button", name="Authorise clinics", exact=True).click()  # the form's own words (P-03)
        expect(dialog).to_be_visible()
        assert person.evaluate("s => s.validity.valueMissing")
        assert len(h.get("credentialer", "/api/catalogues")["eligibilities"]) == before
        # A choice clears it.
        person.select_option(label="Staff 01(test)")
        assert not person.evaluate("s => s.validity.valueMissing")
    finally:
        context.close()


@pytest.mark.parametrize("viewport,cap", [(PHONE, 2), (None, 3)])
def test_toasts_stack_newest_first_and_stay_capped(browser, live_server, h, viewport, cap):
    context, page = sign_in(browser, live_server, "coordinator", viewport=viewport)
    try:
        navigate(page, "people")
        for i in range(5):
            page.evaluate("i => window.dispatchEvent(new CustomEvent('undo-unclaimed',{detail:'Saved change '+i}))", i)
        toasts = page.locator("#toast-tray .toast:visible")
        expect(toasts).to_have_count(cap)
        expect(toasts.first).to_have_text("Saved change 4.")
    finally:
        context.close()


def _published_week(h, me):
    people = h.seed["people"]
    w = h.workspace("Agenda rota", "2026-10-04", "2026-10-11")
    for day in range(4, 11):
        who = [me] if day in (5, 7) else [people[day % len(people)]]
        w = h.shift(w, f"2026-10-{day:02d}T08:00:00+03:00", f"2026-10-{day:02d}T12:00:00+03:00", people=who)["workspace"]
    h.publish(w)


def test_phone_reads_the_published_week_as_a_day_list_with_own_duties_marked(browser, live_server, h):
    me = h.get("staff", "/api/me").get("person_id")
    assert me == h.seed['people'][0], 'Seeded staff account must be linked to its person'
    _published_week(h, me)
    context, page = sign_in(browser, live_server, "staff", viewport=PHONE)
    try:
        navigate(page, "schedule/week/2026-10-04")  # the published days (the timeline opens on today)
        agenda = page.locator(".ag")
        expect(agenda).to_be_visible()
        expect(page.locator(".wk-grid")).to_have_count(0)
        # My duties (S-21): your two duties; the days between collapse to one line each; no "You" on your own items.
        mine = agenda.locator(".ag-item.mine")
        expect(mine.first).to_be_visible()
        assert mine.count() >= 2
        expect(agenda.locator(".ag-gap").first).to_be_visible()
        assert not any("You" in t for t in mine.all_inner_texts())
        # Day headers stick under the top bar while their day scrolls.
        # Retrying: a live update can re-render the list, and a replaced (detached) header reports no style at all.
        expect(page.locator(".ag-head").first).to_have_css("position", "sticky")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        # The grid is still one tap away.
        page.get_by_role("radio", name="Grid", exact=True).click()
        expect(page.locator("#schedule-view .wk-grid")).to_be_visible()
    finally:
        context.close()
    context, page = sign_in(browser, live_server, "staff")
    try:
        navigate(page, "schedule/week/2026-10-04")
        expect(page.locator("#schedule-view .wk-grid")).to_be_visible()  # a desktop always draws the grid
    finally:
        context.close()


def test_phone_planner_bar_is_two_compact_rows(browser, live_server, h):
    people = h.seed["people"]
    w = h.workspace("Compact bar rota", "2026-10-11", "2026-10-18")
    for day in range(11, 15):
        w = h.shift(w, f"2026-10-{day:02d}T08:00:00+03:00", f"2026-10-{day:02d}T12:00:00+03:00", people=[] if day % 2 else [people[0]])["workspace"]
    w = h.lifecycle(w, "validate")["workspace"]
    context, page = sign_in(browser, live_server, "coordinator", viewport=PHONE)
    try:
        navigate(page, "planner")
        bar = page.locator(".topbar .planner-bar")
        expect(bar).to_have_class(re.compile(r"\btightest\b"))
        validate = page.get_by_role("button", name="Submit for review…", exact=True)  # icon-only on a phone
        expect(validate).to_be_visible()
        assert validate.locator(".label").evaluate("e => e.getBoundingClientRect().width") <= 1  # clipped, still read out
        rows = page.evaluate("""() => {
          const items = [...document.querySelectorAll('.topbar.has-planner > *, .topbar .planner-bar > *')]
            .filter(e => getComputedStyle(e).display !== 'contents' && getComputedStyle(e).position !== 'absolute' && e.getBoundingClientRect().height > 4);
          const mids = items.map(e => { const r = e.getBoundingClientRect(); return (r.top + r.bottom) / 2; }).sort((a, b) => a - b);
          return [mids.filter((m, i) => i === 0 || m - mids[i - 1] > 14).length,
            items.map(e => { const r=e.getBoundingClientRect(),s=getComputedStyle(e);
              return {class:e.className,text:e.textContent,rect:r.toJSON(),order:s.order,display:s.display,flex:s.flex}; })];
        }""")
        assert rows[0] <= 2, repr(rows)  # Preserve every child's rectangle rather than pytest's truncated collection display.
        # Blocking issues are counted once, in the status footer's rota health (not in the top bar).
        expect(page.locator('.planner-bar .workflow-blocked')).to_have_count(0)
        # A7/D9: the pill counts the Fix list's must-fix causes, the server's one count.
        must = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['summary']['must']
        assert must > 0
        blocked = page.locator('footer.app-footer').get_by_role('button', name=f'{must} must fix · open the fix list', exact=True)
        expect(blocked).to_have_attribute('aria-label', f'{must} must fix · open the fix list')
        expect(blocked.locator('.num')).to_have_text(str(must))
        expect(blocked).to_be_in_viewport()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    finally:
        context.close()

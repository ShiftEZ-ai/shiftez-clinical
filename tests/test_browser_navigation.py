"""The app shell's navigation (F1): Back steps through pages and page tabs, the skip link focuses the page without
routing, an unknown address says so, Escape folds a focused rail, and the bell's name follows its unread count."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, expect_loaded

pytestmark = pytest.mark.browser
DESKTOP = {"width": 1440, "height": 900}


def go(page, route):
    page.locator(f"#department-nav [data-route={route}]").click()
    expect_loaded(page, route)


def test_back_steps_through_pages_and_page_tabs(browser, live_server, h):
    """N-01: moving between pages and page tabs adds history entries; Back and Forward walk them."""
    context, page = sign_in(browser, live_server, "coordinator", DESKTOP)
    try:
        expect_loaded(page, "home")
        go(page, "requests")
        go(page, "people")
        go(page, "insights")
        page.get_by_role("tab", name="Workload").click()
        expect_loaded(page, "insights/workload")
        page.go_back()
        expect_loaded(page, "insights/fairness")
        page.go_back()
        expect_loaded(page, "people")
        expect(page.locator("#department-nav [aria-current=page]")).to_have_attribute("data-route", "people")
        page.go_back()
        expect_loaded(page, "requests")
        page.go_forward()
        expect_loaded(page, "people")
    finally:
        context.close()


def test_a_refused_back_keeps_the_page_asked_for(browser, live_server, h):
    """N-01 with unsaved settings: Back asks first (the one leave dialog) and the page stays; Discard changes goes to the
    page Back asked for, not one further, and Forward still knows the settings page."""
    context, page = sign_in(browser, live_server, "admin", DESKTOP)
    try:
        expect_loaded(page, "home")
        go(page, "people")
        go(page, "configuration")
        page.get_by_role("navigation", name="Configuration sections").get_by_role("button", name="Department settings", exact=True).click()
        page.get_by_label("Department name", exact=True).fill("Unsaved Back probe")
        page.go_back()
        leave = page.get_by_role("dialog", name="Leave this page?")
        expect(leave).to_contain_text("Not saved")
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "configuration")
        expect(page.get_by_label("Department name", exact=True)).to_have_value("Unsaved Back probe")
        expect(page).to_have_url(re.compile(r"#configuration"))
        leave.get_by_role("button", name="Discard changes", exact=True).click()
        expect_loaded(page, "people")
        page.go_back()
        expect_loaded(page, "home")
        page.go_forward()
        expect_loaded(page, "people")
        page.go_forward()
        expect_loaded(page, "configuration")
    finally:
        context.close()


def test_a_refused_back_onto_an_entry_without_a_place_keeps_the_changes(browser, live_server, h):
    """History from before this release (or a link) has entries with no step: Back onto one with unsaved settings is
    refused in place. It never walks on to a further entry, which would discard the changes without asking."""
    context, page = sign_in(browser, live_server, "admin", DESKTOP)
    try:
        expect_loaded(page, "home")
        page.evaluate("""() => { for (const h of ['#requests', '#people', '#configuration'])
          History.prototype.pushState.call(history, null, '', h); }""")
        page.reload()
        expect_loaded(page, "configuration")
        page.get_by_role("navigation", name="Configuration sections").get_by_role("button", name="Department settings", exact=True).click()
        page.get_by_label("Department name", exact=True).fill("Unsaved Back probe")
        page.go_back()
        expect(page.get_by_role("dialog", name="Leave this page?")).to_contain_text("Not saved")
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect(page).to_have_url(re.compile(r"#configuration"))
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "configuration")
        expect(page.get_by_label("Department name", exact=True)).to_have_value("Unsaved Back probe")
    finally:
        context.close()


def test_skip_link_focuses_the_page_and_keeps_the_address(browser, live_server, h):
    """N-02: the skip link moves focus to the page's main region; it is not an address (it never opens Home)."""
    context, page = sign_in(browser, live_server, "coordinator", DESKTOP)
    try:
        expect_loaded(page, "home")
        go(page, "people")
        page.locator("a.skip-link").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#main")).to_be_focused()
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "people")
        assert page.evaluate("location.hash") == "#people"
    finally:
        context.close()


def test_an_unknown_address_opens_home_and_says_so(browser, live_server, h):
    """N-21: a mistyped link opens Home with one line saying the link opens no page; the bad address is not kept."""
    context, page = sign_in(browser, live_server, "coordinator", DESKTOP)
    try:
        expect_loaded(page, "home")
        go(page, "people")
        page.evaluate("location.hash = 'nonsense-page'")
        expect_loaded(page, "home")
        expect(page.locator("#toast-tray")).to_contain_text("That link doesn’t open a page here")
        assert page.evaluate("location.hash") == "#home"
    finally:
        context.close()


def test_escape_folds_a_rail_opened_by_focus(browser, live_server, h):
    """N-22: Tab into the unpinned rail opens it; Escape folds it and hands focus to the page."""
    context, page = sign_in(browser, live_server, "coordinator", DESKTOP)
    try:
        expect_loaded(page, "home")
        page.evaluate("() => { if (document.querySelector('.shell').dataset.nav !== 'rail') document.querySelector('.pin-button').click(); }")
        expect(page.locator(".shell")).to_have_attribute("data-nav", "rail")
        page.locator("#department-nav [data-route=people]").focus()
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Tab")  # keyboard focus (:focus-visible) peeks the rail open
        expect(page.locator(".sidebar")).to_have_class(re.compile(r"\bpeek\b"))
        page.keyboard.press("Escape")
        expect(page.locator(".sidebar")).not_to_have_class(re.compile(r"\bpeek\b"))
        expect(page.locator("#main")).to_be_focused()
    finally:
        context.close()


def test_the_bell_name_goes_back_to_inbox_once_everything_is_read(browser, live_server, h):
    """N-14: the bell says "Inbox · N unread" while there is unread mail, and "Inbox" again once it is all read."""
    context, page = sign_in(browser, live_server, "coordinator", DESKTOP)
    try:
        expect_loaded(page, "home")
        bell = page.locator(".topbar .bell")
        page.evaluate("""() => { const b = document.querySelector('.topbar .bell');
            b.setAttribute('aria-label', 'Inbox · 3 unread'); b.dataset.tip = 'Inbox · 3 unread'; }""")  # as after a pulse with mail
        go(page, "people")  # a page change repaints the counts from the server (nothing unread in the seed)
        expect(bell).to_have_accessible_name("Inbox")
        expect(bell).to_have_attribute("data-tip", "Inbox")
    finally:
        context.close()


def test_command_menu_has_no_key_legend_or_group_hints(browser, live_server, h):
    """N-19: the command menu is a search box and results: no key legend, page rows without their nav group."""
    context, page = sign_in(browser, live_server, "coordinator", DESKTOP)
    try:
        expect_loaded(page, "home")
        page.keyboard.press("Control+k")
        palette = page.get_by_role("dialog", name="Command menu")
        expect(palette.get_by_role("combobox")).to_have_attribute("placeholder", "Search")
        expect(palette.locator(".palette-foot:not([hidden])")).to_have_count(0)
        people = palette.get_by_role("option", name="People", exact=True)
        expect(people).to_be_visible()
        expect(people.locator(".hint")).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize("route", ["home", "requests", "people", "access", "rules", "programmes"])
def test_page_help_never_repeats_itself_or_counts(browser, live_server, h, route):
    """N-15: one description per page: the ⓘ tip (where a page has help) repeats no sentence and holds no count."""
    context, page = sign_in(browser, live_server, "admin", DESKTOP)
    try:
        expect_loaded(page, "home")
        page.evaluate(f"location.hash = '{route}'")
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", route)
        for tip in page.locator(".topbar .page-info").evaluate_all("ns => ns.map(n => n.dataset.tip)"):
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", tip) if s.strip()]
            assert len(sentences) == len(set(sentences)), tip
            assert not re.search(r"(^|\.\s)[\d,]+ \w+$", tip), tip
    finally:
        context.close()

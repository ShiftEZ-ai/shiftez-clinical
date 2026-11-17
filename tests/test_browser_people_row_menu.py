"""QA-043b (UX › controls S3): a People row's ⋯ menu was a lone "Open" for department readers. It now lists, under
"Open", the person panel's own sections this reader may open (the same list as the panel's tabs, personSections),
then the actions the contract's permissions allow, never an "Open" item (the row itself opens the panel)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, wait_held, wait_until  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("role,actions", [
    ("approver", []),
    ("coordinator", ["Record leave", "Add rank", "Add a rule for this person"]),
])
def test_people_row_menu_lists_the_panel_sections_and_permitted_actions(browser, live_server, h, role, actions):
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page, "people")
        row = page.locator("#main [data-row]").first
        expect(row).to_be_visible()
        row.get_by_role("button", name=re.compile(r"^More actions for ")).click()
        menu = page.get_by_role("menu", name="Row actions")
        sections = [t.strip() for t in menu.get_by_role("group", name="Open").get_by_role("menuitem").all_text_contents()]
        assert sections and "Open" not in sections and "Overview" not in sections, sections
        expect(menu.get_by_role("menuitem", name="Open", exact=True)).to_have_count(0)
        for name in actions:
            expect(menu.get_by_role("menuitem", name=name, exact=True)).to_be_visible()
        if not actions:
            expect(menu.get_by_role("separator")).to_have_count(0)
        # Each section opens the panel on that tab: the menu and the tabs are one list.
        target = sections[-1]
        menu.get_by_role("menuitem", name=target, exact=True).click()
        panel = page.locator(".dock-panel.person-panel")
        expect(panel.get_by_role("tab", name=target, exact=True)).to_have_attribute("aria-selected", "true")
        tabs = [t.strip() for t in panel.get_by_role("tab").evaluate_all("ts => ts.map(t => t.getAttribute('aria-label') || t.textContent)")]
        assert [t for t in tabs if t != "Overview"] == sections, (tabs, sections)
    finally:
        context.close()


def test_an_open_row_menu_stays_on_its_row_when_a_refresh_redraws_the_list(browser, live_server, h):
    """A foreground refresh already reading the list when its row menu opens replaces the button.
    The menu stays on the new button, focus stays on the same item, and that item acts on the same row."""
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        first = page.locator("#main [data-row]").nth(1)
        expect(first).to_be_visible()
        more = page.locator(f'#main [data-row="{first.get_attribute("data-row")}"]').get_by_role(
            "button", name=re.compile(r"^More actions for "))
        person = more.get_attribute("aria-label").removeprefix("More actions for ")
        held = []
        # The refresh's list read: a list-holding role reads the catalogue again (its contacts were joined from the
        # directory once, at start); a role without lists reads the directory.
        directory = re.compile(r"/api/(catalogues|directory)(\?.*)?$")
        page.route(directory, lambda route: held.append(route))
        # A background refresh waits for the menu to close. Use the foreground recheck to exercise menu adoption.
        page.evaluate("dispatchEvent(new Event('rota-recheck'))")
        wait_held(page, held)
        more.click()
        menu = page.get_by_role("menu", name="Row actions")
        sections = [t.strip() for t in menu.get_by_role("group", name="Open").get_by_role("menuitem").all_text_contents()]
        assert sections, sections
        target = sections[min(1, len(sections) - 1)]
        for _ in range(sections.index(target)):
            page.keyboard.press("ArrowDown")
        item = menu.get_by_role("menuitem", name=target, exact=True)
        expect(item).to_be_focused()
        more.evaluate("b => { window.fixtureOldMore = b; }")
        # A real change elsewhere while the read is held: its reply carries a new people version, so the list is redrawn
        # (an unchanged reply rightly keeps the list as drawn, app.ts `known=`).
        h.configure("people", {"display_name": "Zz Refresh person (test)", "title": "Registrar"})
        for route in held:  # continue first: removing the last interceptor resumes held routes (Playwright 1.63+, test_browser.Held)
            route.continue_()
        page.unroute(directory)
        wait_until(page, lambda: page.evaluate("!window.fixtureOldMore.isConnected"), "the list was never redrawn")
        expect(menu).to_be_visible()
        expect(more).to_have_attribute("aria-expanded", "true")  # the row's new button
        expect(item).to_be_focused()
        page.keyboard.press("Enter")
        panel = page.locator(".dock-panel.person-panel")
        expect(panel.get_by_role("tab", name=target, exact=True)).to_have_attribute("aria-selected", "true")
        expect(panel).to_contain_text(person)
        expect(menu).to_have_count(0)
    finally:
        context.close()

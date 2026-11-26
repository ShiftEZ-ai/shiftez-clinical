"""The one picker behind every dropdown: search, keyboard, select all shown, chosen first, and dialogs."""
import re
import pytest
from playwright.sync_api import expect
from packages.contracts.schemas import ROLE_LABELS, ROLE_NAMES
from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser


def test_popover_keyboard_selection_returns_focus_without_taking_it_from_the_next_control(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        group = page.get_by_role("button", name="Group", exact=True)
        group.focus()
        group.press("Enter")
        rank = page.get_by_role("menu", name="Group by", exact=True).get_by_role("menuitemradio", name="Current rank", exact=True)
        rank.focus()
        rank.press("Enter")
        expect(page.get_by_role("dialog", name="Group", exact=True)).to_be_visible()
        page.keyboard.press("Escape")  # the editor stays open for another level or direction
        group = page.get_by_role("button", name="Group: Current rank", exact=True)
        expect(group).to_be_focused()
        expect(page.locator("#main .dt-chips")).to_have_count(0)  # the Group button says it; no second row

        group.press("Enter")
        search = page.get_by_role("searchbox", name="Search people", exact=True)
        search.click()
        expect(page.get_by_role("dialog", name="Group", exact=True)).to_have_count(0)
        expect(search).to_be_focused()

        views = page.get_by_role("button", name="Saved views", exact=True)
        views.focus()
        views.press("Enter")
        save = page.get_by_role("menuitem", name="New view…", exact=True)
        save.focus()
        save.press("Enter")
        dialog = page.get_by_role("dialog", name="New view", exact=True)
        expect(dialog).to_be_visible()
        assert dialog.evaluate("dialog => dialog.contains(document.activeElement)")
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(views).to_be_focused()
    finally:
        context.close()


def test_typing_on_a_dropdown_searches_ignoring_accents_and_enter_picks(browser, live_server, h):
    kind = h.get("admin", "/api/catalogues")["location_types"][0]["id"]
    for name in ("Zürich clinic", "Zurich annex", "North clinic", *[f"Room {n}" for n in range(1, 11)]):
        h.configure("locations", {"name": name, "starts_on": "2020-01-01", "location_type_id": kind})
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        h.workspace('Picker draft')  # a draft: Add session opens the session form (a long single choice: Location)
        navigate(page, 'planner')
        page.locator('#main').get_by_role('button', name='Add session', exact=True).click()
        location = page.locator('.dock-panel').get_by_label('Location', exact=True)
        location.focus()
        page.keyboard.type("zur")
        picker = page.locator(".picker")
        expect(picker.get_by_role("combobox")).to_have_value("zur")
        expect(picker.get_by_role("option")).to_have_count(2)
        expect(picker.locator("mark").first).to_have_text(re.compile(r"^Z[uü]r$"))
        page.keyboard.press("ArrowDown")  # arrows only move the highlight
        expect(picker).to_have_count(1)
        page.keyboard.press("Enter")
        expect(picker).to_have_count(0)
        assert location.evaluate("s => s.selectedOptions[0].textContent") in ("Zürich clinic", "Zurich annex")
        expect(location).to_be_focused()
    finally:
        context.close()


def test_select_all_shown_escape_in_a_dialog_and_chosen_first(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "inbox")
        page.get_by_role("button", name="New announcement", exact=True).click()
        form = page.get_by_role("dialog")
        form.get_by_label("For", exact=True).select_option("some")
        form.locator(".cond-line select").first.select_option("roles")  # "Where Role is any of …"
        expect(form.locator(".picker")).to_be_visible()  # choosing the field opens its values; Esc closes just that list
        page.keyboard.press("Escape")
        expect(form.locator(".picker")).to_have_count(0)
        roles = form.locator(".cond-line .pick-button").first
        roles.click()
        picker = form.locator(".picker")
        expect(picker.get_by_role("option")).to_have_count(len(ROLE_NAMES))
        for role in ROLE_NAMES:
            expect(picker.get_by_role("option", name=ROLE_LABELS[role], exact=True)).to_be_visible()
        picker.get_by_role("button", name=re.compile(r"^Select all")).click()
        expect(roles.locator(".pick-chip")).to_have_count(len(ROLE_NAMES))  # every chosen role remains a chip
        picker.get_by_role("button", name=re.compile(r"^Clear")).click()
        # Tick two, close with Esc (the dialog stays), reopen: the two are listed first.
        picker.get_by_role("option", name="Staff").check()
        picker.get_by_role("option", name="Approver").check()
        page.keyboard.press("Escape")
        expect(picker).to_have_count(0)
        expect(form).to_be_visible()
        expect(roles).to_contain_text("Staff")
        roles.click()
        expect(form.locator(".picker .picker-group").first).to_contain_text("Chosen")
    finally:
        context.close()


def test_keyboard_reaches_select_all_and_keeps_its_place(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "inbox")
        page.get_by_role("button", name="New announcement", exact=True).click()
        form = page.get_by_role("dialog")
        form.get_by_label("For", exact=True).select_option("some")
        form.locator(".cond-line select").first.select_option("roles")  # "Where Role is any of …"
        expect(form.locator(".picker")).to_be_visible()  # choosing the field opens its values; Esc closes just that list
        page.keyboard.press("Escape")
        expect(form.locator(".picker")).to_have_count(0)
        roles = form.locator(".cond-line .pick-button").first
        roles.focus()
        page.keyboard.press("ArrowDown")  # opens the list
        picker = form.locator(".picker")
        expect(picker).to_have_count(1)
        page.keyboard.press("Enter")  # ticks the highlighted role, list stays open
        expect(picker.get_by_role("option", checked=True)).to_have_count(1)
        page.keyboard.press("Tab")  # to the action row: "Select all"
        expect(picker.get_by_role("button", name=re.compile(r"^Select all"))).to_be_focused()
        page.keyboard.press("Enter")
        expect(roles.locator(".pick-chip")).to_have_count(len(ROLE_NAMES))  # every chosen role remains a chip
        expect(picker.get_by_role("button", name=re.compile(r"^Clear"))).to_be_focused()  # "Select all" is now done, focus moves on sensibly
        page.keyboard.press("Escape")
        expect(picker).to_have_count(0)
        expect(form).to_be_visible()
    finally:
        context.close()


def test_a_list_can_drop_recent_and_show_trailing_meta_per_row(browser, live_server, h):
    """recent: false keeps a long list in the caller's order (no Recent section, nothing remembered); meta adds
    trailing content to each row, read as part of the option. Opened, picked and reopened in one go, so the page's own
    focus handling cannot close the list in between."""
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        expect(page.locator("#main [data-row]").first).to_be_visible()
        seen = page.evaluate("""async () => {
          const {openPicker} = await window.importBundle('picker.js'), {el} = await window.importBundle('dom.js');
          const anchor = el('button', {type: 'button'}, 'Open'); document.body.append(anchor);
          const choices = Array.from({length: 10}, (_, i) => ({value: 'v' + i, label: 'Item ' + i}));
          const open = recent => openPicker(anchor, document.body, {label: 'Test list', choices, multiple: false, selected: new Set(),
            recentKey: 'picker-test-' + recent, recent, onPick: () => undefined,
            meta: c => c.value === 'v9' ? el('span', {}, '9 of 9, at the limit') : null});
          const out = {};
          for (const recent of [true, false]) {
            open(recent);
            [...document.querySelectorAll('.picker [role=option]')].find(o => o.textContent.startsWith('Item 9')).click();
            const close = open(recent), options = [...document.querySelectorAll('.picker [role=option]')];
            out[recent] = {recentGroup: [...document.querySelectorAll('.picker .picker-group')].some(g => g.textContent.startsWith('Recent')),
              order: options.map(o => o.querySelector('.picker-label').textContent),
              meta: [...document.querySelectorAll('.picker .picker-meta')].map(m => [m.closest('[role=option]').textContent, m.textContent])};
            close();
          }
          anchor.remove();
          return out;
        }""")
        assert seen['true']['recentGroup'] and seen['true']['order'][0] == 'Item 9', seen
        assert not seen['false']['recentGroup'] and seen['false']['order'] == [f'Item {n}' for n in range(10)], seen
        for recent in ('true', 'false'):
            assert seen[recent]['meta'] == [['Item 99 of 9, at the limit', '9 of 9, at the limit']], seen  # the option's text includes it
    finally:
        context.close()


MOUNT_FIELDS = """async () => {
  const {select, multiPick} = await window.importBundle('dom.js');
  const zones = select('Scheduling timezone', Array.from({length: 10}, (_, i) => ({value: 'z' + i, label: 'Fictional zone ' + i})), 'z0');
  const status = multiPick('Status', [{value: 'Active', label: 'Active'}, {value: 'Inactive', label: 'Inactive'}, {value: 'Ended', label: 'Ended'}], ['Ended']);
  const host = document.createElement('section'); host.id = 'picker-fixture'; host.className = 'panel';
  host.append(zones.wrap, status.wrap); document.querySelector('#main').prepend(host);
}"""


@pytest.mark.parametrize("width", [700, 390])
def test_phone_sheet_follows_the_one_breakpoint_names_its_field_and_closes(browser, live_server, h, width):
    """X-09: the picker is a bottom sheet at the one phone breakpoint (720 px), not its own 640. X-10: the sheet names the
    field it is for and has a 44 px Close; a tap on the dimmed page behind closes it too."""
    context, page = sign_in(browser, live_server, "coordinator", {"width": width, "height": 900})
    try:
        navigate(page, "people")
        page.evaluate(MOUNT_FIELDS)
        zones = page.locator("#picker-fixture").get_by_label("Scheduling timezone", exact=True)
        zones.click()
        sheet = page.locator(".picker.sheet")
        expect(sheet).to_be_visible()
        expect(sheet.get_by_role("heading")).to_have_text("Scheduling timezone")
        expect(page.get_by_role("dialog", name="Scheduling timezone", exact=True)).to_be_visible()
        # Still found by its field's name (the Drafts switcher keeps an open list across a redraw this way).
        expect(sheet).to_have_attribute("aria-label", "Scheduling timezone")
        close = sheet.get_by_role("button", name="Close", exact=True)
        # Its own size (the sheet's box is scaled for a moment while it opens).
        assert close.evaluate("b => [getComputedStyle(b).width, getComputedStyle(b).height]") == ["44px", "44px"]
        close.click()
        expect(sheet).to_have_count(0)
        expect(zones).to_be_focused()
        zones.click()
        expect(sheet).to_be_visible()
        page.mouse.click(width / 2, 12)  # the dimmed page above the sheet
        expect(sheet).to_have_count(0)
    finally:
        context.close()


def test_multiple_choice_is_a_multiselectable_listbox(browser, live_server, h):
    """X-36: the combobox's list is a listbox (aria-multiselectable) of options, each selected and checked or not; the
    highlight moving with ↓ points at an option, so screen readers announce it."""
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "inbox")
        page.get_by_role("button", name="New announcement", exact=True).click()
        form = page.get_by_role("dialog")
        form.get_by_label("For", exact=True).select_option("some")
        form.locator(".cond-line select").first.select_option("roles")
        picker = form.locator(".picker")
        listbox = picker.get_by_role("listbox")
        expect(listbox).to_have_attribute("aria-multiselectable", "true")
        expect(picker.get_by_role("checkbox")).to_have_count(0)  # the tick box is drawing, not a second control
        page.keyboard.press("ArrowDown")
        active = picker.get_by_role("combobox").get_attribute("aria-activedescendant")
        assert page.locator(f"#{active}").get_attribute("role") == "option"
        page.keyboard.press("Enter")
        expect(page.locator(f"#{active}")).to_have_attribute("aria-selected", "true")
        expect(page.locator(f"#{active}")).to_have_attribute("aria-checked", "true")
    finally:
        context.close()


def test_a_short_list_has_no_sections_or_bulk_buttons(browser, live_server, h):
    """X-37: three options open without Chosen / Others sections, Select all or Clear: the tick shows the choice and the
    list keeps its order. X-11: every select draws the shared picker's chevron (an outline V)."""
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        page.evaluate(MOUNT_FIELDS)
        page.locator("#picker-fixture .pick-button").click()
        picker = page.locator(".picker")
        expect(picker.get_by_role("option")).to_have_count(3)
        expect(picker.locator(".picker-group")).to_have_count(0)
        expect(picker.get_by_role("button", name=re.compile("^(Select all|Clear)"))).to_have_count(0)
        expect(picker.get_by_role("option").first).to_have_accessible_name("Active")  # Ended (chosen) is not moved first
        picker.get_by_role("option", name="Inactive", exact=True).check()
        expect(picker.get_by_role("option", checked=True)).to_have_count(2)
        page.keyboard.press("Escape")
        caret = page.locator("#picker-fixture").get_by_label("Scheduling timezone", exact=True).evaluate("s => getComputedStyle(s).backgroundImage")
        assert caret.count("linear-gradient") == 2 and "to right top" in caret and "to right bottom" in caret, caret
    finally:
        context.close()


def test_a_group_option_is_reached_and_chosen_by_keyboard(browser, live_server, h):
    """X-36 follow-up (review of maintenance): a grouped multiple choice's "All in <group>" option is part of the listbox's
    keyboard path: ↓ reaches it (active-descendant points at it) and Enter chooses everyone in that group."""
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        page.evaluate("""async () => {
          const {multiPick} = await window.importBundle('dom.js');
          const people = ['Consultant', 'Resident'].flatMap(g => [1, 2, 3, 4].map(i => ({value: g + i, label: `Fictional ${g} ${i}`, group: g})));
          const field = multiPick('People', people, []);
          const host = document.createElement('section'); host.id = 'picker-fixture'; host.className = 'panel';
          host.append(field.wrap); document.querySelector('#main').prepend(host); }""")
        page.locator("#picker-fixture .pick-button").click()
        picker = page.locator(".picker")
        combo = picker.get_by_role("combobox")
        expect(combo).to_be_focused()
        group = picker.get_by_role("option", name="All in Resident", exact=True)
        target = group.get_attribute("id")
        assert target, "the group option has an id the combobox can point at"
        for _ in range(12):
            if combo.get_attribute("aria-activedescendant") == target:
                break
            page.keyboard.press("ArrowDown")
        assert combo.get_attribute("aria-activedescendant") == target
        expect(group).to_have_class(re.compile(r"\bactive\b"))  # highlighted like any row it passes
        page.keyboard.press("Enter")
        expect(picker.get_by_role("option", name="All in Resident", exact=True)).to_have_attribute("aria-selected", "true")
        for i in range(1, 5):
            expect(picker.get_by_role("option", name=f"Fictional Resident {i}", exact=True)).to_have_attribute("aria-selected", "true")
        expect(picker.get_by_role("option", name="Fictional Consultant 1", exact=True)).to_have_attribute("aria-selected", "false")
    finally:
        context.close()

"""Shared UI regressions: real Chromium/API; faults are isolated to disposable test contexts."""
import re

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, pick_view, save_dialog, toolbar, close_popover, set_layout, choose, planner_filter, filter_pick, view_settings, press_escape  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('phone', [False, True], ids=['desktop', 'phone'])
def test_planner_view_settings_belong_to_the_renderer_and_keep_hidden_choices(browser, live_server, h, phone):
    h.shift(h.workspace('Renderer control ownership'))
    context, page = sign_in(browser, live_server, 'coordinator',
                            {'width': 390, 'height': 844} if phone else None)
    try:
        navigate(page, 'planner')
        bar = page.locator('.planner-toolbar')

        def fits(panel):
            expect(panel).to_be_visible()
            expect(panel).to_be_in_viewport()

        def menus(present, colour=None):
            # Colour by belongs to every renderer that colours its items through the shared colourRule: the card grids
            # and People's chips, not Coverage's counts or the List (app.ts surfaceNow); Density to the card grids only.
            view = view_settings(bar); fits(view)
            expect(view.get_by_role('radiogroup', name='Layout', exact=True)).to_have_count(1)
            expect(view.locator('.vs-row', has_text='Properties')).to_have_count(1)  # the Person label, in every view
            expect(view.locator('.vs-row', has_text='Colour by')).to_have_count(1 if (present if colour is None else colour) else 0)
            expect(view.get_by_role('radiogroup', name='Density', exact=True)).to_have_count(1 if present else 0)
            close_popover(page)

        def options(tool):
            panel = toolbar(bar, tool); fits(panel)
            names = [x.strip() for x in panel.get_by_role('menuitemradio').all_text_contents()]
            if not names and tool == 'Sort':  # a sort list (several keys): its first key's choices
                names = panel.locator('.dt-sort-row').first.get_by_label('Sort by', exact=True).evaluate("s => [...s.options].map(o => o.textContent.trim())")
            if not names and tool == 'Group':
                # Group is the levels editor (3eb3ec9f3): grouped, its first level's field choices (the default marked),
                # and "No grouping" is the Remove grouping action.
                expect(panel.get_by_role('button', name='Remove grouping', exact=True)).to_be_visible()
                names = ['No grouping'] + panel.locator('.dt-group-list .dt-sort-row').first.get_by_label('Group by', exact=True).evaluate(
                    "s => [...s.options].map(o => o.textContent.replace(/ \\(default\\)$/, '').trim())")
            close_popover(page)
            return names

        def grid_choices(present, colour=None):
            menus(present, colour)
            for tool in ('Group', 'Sort'):
                expect(bar.get_by_role('button', name=re.compile(rf'^{tool}($|:)'))).to_have_count(1 if present else 0)

        def kept():
            expect(bar.get_by_role('button', name='Group: Duty type', exact=True)).to_be_visible()
            expect(bar.get_by_role('button', name=re.compile(r'^Sort: Place'))).to_be_visible()
            view = view_settings(bar, 'Colour by')
            expect(view.get_by_role('radiogroup', name='Colour by').get_by_role('radio', name='Service', exact=True)).to_have_attribute('aria-checked', 'true')
            view = view_settings(bar)
            expect(view.get_by_role('radiogroup', name='Density').get_by_role('radio', name='Compact', exact=True)).to_have_attribute('aria-checked', 'true')
            close_popover(page)
            expect(page.locator('.wk-grid')).to_have_class(re.compile(r'\bcompact\b'))

        if phone:
            # Agenda has fixed chronological order and day grouping, and colours its items through colourRule
            # (planner_week.ts, surfaceNow 'agenda'), so it offers Colour by.
            grid_choices(False, colour=True)
            bar.get_by_role('radiogroup', name='Calendar layout').get_by_role('radio', name='Grid', exact=True).click()
        grid_choices(True)
        assert options('Group') == ['No grouping', 'Service', 'Duty type', 'Location', 'Duty time']  # Duty time: 8d703991a
        assert options('Sort') == ['Default order', 'Duty type', 'Place']
        choose(bar, 'Group', 'Group by', 'Duty type')
        choose(bar, 'Sort', 'Sort by', 'Place')
        view_settings(bar, 'Colour by').get_by_role('radiogroup', name='Colour by').get_by_role('radio', name='Service', exact=True).click()
        view_settings(bar).get_by_role('radiogroup', name='Density').get_by_role('radio', name='Compact', exact=True).click()
        close_popover(page)
        kept()
        for layout in ('People', 'Coverage', 'List'):
            pick_view(page, layout); menus(False, colour=layout == 'People')
            if layout == 'People':
                assert options('Group') == ['No grouping', 'Rank', 'Tag', 'Supervision team']
                assert options('Sort')[1:] == ['Hours', 'Name']
            if layout == 'Coverage':
                grid_choices(False)
            if layout == 'List':
                assert {'Duty', 'Status'} <= set(options('Group'))
                assert 'When' in options('Sort')
        # Month is the week grid in bands: the same session display choices.
        pick_view(page, 'Month'); menus(True)
        pick_view(page, 'Week'); grid_choices(True); kept()
        if phone:
            bar.get_by_role('radiogroup', name='Calendar layout').get_by_role('radio', name='Agenda', exact=True).click()
            grid_choices(False)
            # A remembered phone Agenda does not hide controls on a desktop that renders the Grid.
            page.set_viewport_size({'width': 1440, 'height': 1000})
            pick_view(page, 'List'); pick_view(page, 'Week'); grid_choices(True)  # the next draw is at desktop width
            # The same popovers fit narrow and short viewports and stay open across the planner's redraw.
            for viewport, consultation, label in [({'width': 320, 'height': 568}, 'virtual', 'Virtual'),
                                                   ({'width': 390, 'height': 360}, 'physical', 'In person')]:
                page.set_viewport_size(viewport)
                pick_view(page, 'List')
                page.get_by_role('button', name='Rota tools', exact=True).click()
                tools = page.get_by_role('menu', name='Rota tools', exact=True)
                fits(tools)
                tools.get_by_role('menuitem', name='Update recurring sessions', exact=True).click()
                dialog = page.get_by_role('dialog', name='Update recurring sessions', exact=True)
                expect(dialog).to_be_visible()
                dialog.get_by_role('button', name='Close dialog', exact=True).click()
                expect(dialog).not_to_be_visible()
                filters = planner_filter(page); fits(filters)
                previous = page.locator('#schedule-view').element_handle()
                filters.get_by_role('checkbox', name='Only sessions with open places', exact=True).check()
                page.wait_for_function('previous=>!previous.isConnected', arg=previous)
                fits(filters)  # Still open, and positioned against the redrawn toolbar.
                filters.get_by_role('button', name='Clear all filters', exact=True).click()
                expect(filters.get_by_role('checkbox', name='Only sessions with open places', exact=True)).not_to_be_checked()
                fits(filters)
                close_popover(page)
                navigate(page, 'calendar')
                page.get_by_role('button', name=re.compile('^Filter')).click()  # the shared collection toolbar
                calendar_filters = page.get_by_role('dialog', name='Filter')
                filter_pick(calendar_filters, 'Consultation mode', label)  # The last field must be reachable.
                expect(calendar_filters.get_by_role('button', name=f'Consultation mode: {label}', exact=True)).to_be_visible()
                expect(page.get_by_role('button', name=re.compile(r'^Filter: .+ \(1 active\)$'))).to_be_visible()
                press_escape(page)  # Dismiss pointer help before sending one Escape to the Filter popover.
                expect(page.get_by_role('tooltip')).to_have_count(0)
                expect(calendar_filters).to_have_count(0)
                navigate(page, 'planner'); pick_view(page, 'Week')
    finally:
        context.close()


def test_planner_tabs_support_arrows_and_keep_the_selected_tab_focused(browser, live_server, h):
    """Schedule views are one choice: the View menu ticks the current one, and the letter keys switch views."""
    h.shift(h.workspace("Keyboard UI regression"))
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner")
        view = page.locator("#schedule-view")
        expect(view).to_have_attribute("aria-label", "Week view")
        page.locator("body").press("m")
        expect(view).to_have_attribute("aria-label", "Month view")
        panel = view_settings(page.locator(".planner-toolbar"))
        expect(panel.get_by_role("radiogroup", name="Layout", exact=True).get_by_role("radio", name="Calendar")).to_have_attribute("aria-checked", "true")
        expect(panel.get_by_role("radiogroup", name="Range", exact=True).get_by_role("radio", name="Month")).to_have_attribute("aria-checked", "true")
        expect(panel.locator('[role=radiogroup][aria-label=Range] [role=radio][aria-checked="true"]')).to_have_count(1)
        close_popover(page)
        page.locator("body").press("w")
        expect(view).to_have_attribute("aria-label", "Week view")
    finally:
        context.close()


def test_account_menu_arrows_never_change_the_theme(browser, live_server):
    """The account menu is one menu (N-16): it opens on an item, arrows move through Appearance and the actions below
    without changing the theme, and Enter chooses."""
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        page.get_by_role("button", name=re.compile(r"^Account menu, ")).click()
        menu = page.get_by_role("menu", name="Account")
        group = menu.get_by_role("group", name="Appearance")
        group.get_by_role("menuitemradio", name="Light", exact=True).click()
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        group.get_by_role("menuitemradio", name="Light", exact=True).focus()
        feedback = menu.get_by_role("menuitem", name="Send feedback")
        for _ in range(menu.locator('[role^=menuitem]').count()):
            if feedback.evaluate('n => n === document.activeElement'):
                break
            page.keyboard.press("ArrowDown")
            expect(page.locator("html")).to_have_attribute("data-theme", "light")
        expect(feedback).to_be_focused()  # motion is another menu item, never a theme selection
        page.keyboard.press("Home")
        page.keyboard.press("ArrowDown")
        dark = group.get_by_role("menuitemradio", name="Dark", exact=True)
        expect(dark).to_be_focused()
        page.keyboard.press("Enter")
        expect(dark).to_have_attribute("aria-checked", "true")
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    finally:
        context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_shared_semantic_text_contrast_in_each_theme(browser, live_server, theme):
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        ratios = page.evaluate("""async theme => {
          document.documentElement.dataset.theme = theme;
          const {el, notify, button, armed} = await window.importBundle('dom.js');
          notify('Contrast check error', 'error');
          const confirm = armed(button('Delete sample', () => {}));
          document.body.append(confirm, el('span', {class:'rule-banner-count'}, '23'),
            el('div', {class:'bell'}, el('span', {class:'count'}, '2')));
          confirm.click();
          const luminance = colour => {
            const values = colour.match(/[\\d.]+/g).slice(0,3).map(Number).map(v => {
              v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
            });
            return values.reduce((sum,v,i) => sum + v * [.2126,.7152,.0722][i], 0);
          };
          return ['.toast.error','.armed','.rule-banner-count','.bell .count'].map(selector => {
            const style = getComputedStyle(document.querySelector(selector));
            const a=luminance(style.color), b=luminance(style.backgroundColor);
            return {selector,ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
          });
        }""", theme)
        assert all(item["ratio"] >= 4.5 for item in ratios), ratios
    finally:
        context.close()


@pytest.mark.parametrize("failed_lists", ["both", "publications"])
def test_rota_calendar_read_failure_is_explicit_and_preserves_good_sources(browser, live_server, h, failed_lists):
    """The Year view (the old rota calendar): a failed day summary says so with Retry and keeps the navigator; a failed
    rota list is an explicit page error, never an empty "no rotas" state."""
    h.shift(h.workspace("Available partial rota"))
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        page.route("**/api/schedule/days*", lambda route: route.abort())
        if failed_lists == "both":
            page.route("**/api/workspaces?limit=50&offset=0", lambda route: route.abort())
        page.evaluate("location.hash='timeline'")
        expect(page.locator("#main").get_by_role("alert").first).to_contain_text(
            re.compile(r"unavailable|unable to load|could not|failed|didn.t load", re.I))
        expect(page.get_by_role("heading", name="No rotas yet", exact=True)).to_have_count(0)
        expect(page.locator("#main").get_by_role("button", name=re.compile("retry|try again", re.I)).first).to_be_visible()
        if failed_lists == "publications":
            expect(page.locator("#main")).to_have_attribute("data-loaded-view", "schedule")
            expect(page.locator(".planner-toolbar .wk-nav")).to_be_visible()
            # An undated Year link opens no draft from another month (S-30); every rota stays one step away.
            expect(page.get_by_role("button", name=re.compile(r"^(Switch draft · Available partial rota|Choose a draft)$"))).to_be_visible()
    finally:
        context.close()


def test_failed_publication_rule_checks_show_a_neutral_unchecked_day(browser, live_server, h):
    """Live checks of the published days failing must not break the schedule: it keeps the stored check and its sessions."""
    h.publish(h.shift(h.workspace("Coverage unavailable regression"))["workspace"])
    context, page = sign_in(browser, live_server, "coordinator")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.route("**/api/publications/*/coverage", lambda route: route.abort())
        navigate(page, "schedule/week/2026-10-04?edit=none")
        expect(page.locator("#schedule-view .pchip").first).to_be_visible()
        navigate(page, "schedule/year/2026-10-04?edit=none")
        day = page.locator('.sd-yday[aria-label^="4 Oct 2026:"]')
        expect(day).to_have_attribute("aria-label", re.compile("Coverage unavailable regression, published, 1 session"))
        expect(page.locator("#main [role=alert]")).to_have_count(0)
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize("phone", [False, True], ids=["desktop", "phone"])
def test_directory_filtered_empty_keeps_controls_and_recovers(browser, live_server, h, phone):
    # Real complete contacts and current relationships: every row reads Complete.
    for index, person in enumerate(h.get("coordinator", "/api/catalogues")["people"]):
        h.configure("people", {"display_name": person["display_name"],
            "title": "Fictional clinician", "directory_section": "Demo team",
            "staff_number": f"DEMO-{index + 1}", "work_phone": f"555100{index + 1}"},
            person["id"], person["revision"])
    catalogue = h.get("coordinator", "/api/catalogues")
    people = catalogue["people"]
    assert len(people) == 3 and all(person["active"] for person in people)
    person_ids = {person["id"] for person in people}
    for kind in ("memberships", "eligibilities"):
        assert person_ids <= {row["person_id"] for row in catalogue[kind]
            if row["starts_on"] <= "2026-10-04" and not row["ends_on"] and row.get("active", True)}
    viewport = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 1000}
    context, page = sign_in(browser, live_server, "coordinator", viewport=viewport)
    try:
        navigate(page,"people")
        table = page.locator("#main .dt")

        def layout(name):
            set_layout(table, name)

        def visible_people(inactive=False):
            expect(page.locator(".app-footer .dt-count")).to_have_text("3 people")
            for person in people:
                expect(table.get_by_text(person["display_name"], exact=True)).to_be_visible()
            expect(table.get_by_text("Inactive", exact=True)).to_have_count(3 if inactive else 0)

        def facet(label, value):
            """Toggle one value of a filter in the toolbar's Filter popover."""
            from .test_browser import condition
            table.get_by_role("button", name=re.compile("^Filter")).click()
            pop = page.locator(".dt-pop")
            line = pop.locator(".cond-line:not(.dt-as-of)").filter(has=page.locator(".pick-button", has_text=value))
            if line.count():  # the value is on: untick it in its row
                line.first.locator(".pick-button").click()
                page.locator(".picker").get_by_role("option", name=value, exact=True).click()
                page.keyboard.press("Escape")
            else:
                condition(pop, label, [value])
            page.keyboard.press("Escape")

        def filtered_empty():
            expect(table.get_by_role("heading", name="No matching people", exact=True)).to_be_visible()
            expect(table.get_by_role("heading", name="No people yet", exact=True)).to_have_count(0)
            expect(page.locator(".app-footer .dt-count")).to_have_text("0 of 3 people")
            expect(table.get_by_role("button", name=re.compile("^Filter"))).to_be_visible()
            expect(table.get_by_label("Search people", exact=True)).to_be_visible()
            expect(table.get_by_role("button", name=re.compile("^View"))).to_be_visible()

        for name in ("Table", "Cards"):
            layout(name)
            visible_people()
            facet("Details to review", "Needs attention")
            filtered_empty()
            facet("Details to review", "Needs attention")
            visible_people()
        # Deactivation changes the real source, not an intercepted response or a DOM fixture.
        for person in h.get("coordinator", "/api/catalogues")["people"]:
            value = {key: person[key] for key in
                ("display_name", "title", "directory_section", "staff_number", "work_phone")}
            h.configure("people", {**value, "active": False}, person["id"], person["revision"])
        page.reload()
        navigate(page,"people")
        for name in ("Table", "Cards"):
            layout(name)
            filtered_empty()  # active people only, by default
            facet("Status", "Active")
            visible_people(inactive=True)
            facet("Status", "Active")
        stored = h.get("coordinator", "/api/catalogues")["people"]
        assert {person["id"] for person in stored} == person_ids
        assert all(not person["active"] for person in stored)
    finally:
        context.close()


@pytest.mark.parametrize('action,role', [('Reset to default', 'menuitem'), ('Default view', 'menuitemradio')])
def test_collection_default_restores_adaptive_layout_after_manual_phone_choice(browser, live_server, h, action, role):
    phone = {'width': 390, 'height': 844}
    context, page = sign_in(browser, live_server, 'coordinator', phone)
    try:
        navigate(page, 'people')
        table = page.locator('#main .dt[data-table="people"]')
        expect(table.locator('.dt-card')).to_have_count(3)
        set_layout(table, 'Table')
        expect(table.locator('tr[data-row]')).to_have_count(3)
        page.reload()
        expect(table.locator('tr[data-row]')).to_have_count(3)  # The explicit choice persists until reset.

        toolbar(table, 'Saved views').get_by_role(role, name=action, exact=True).click()
        expect(table.locator('.dt-card')).to_have_count(3)
        expect(table.locator('tr[data-row]')).to_have_count(0)
        page.reload()
        expect(table.locator('.dt-card')).to_have_count(3)
        page.set_viewport_size({'width': 1440, 'height': 1000})
        expect(table.locator('tr[data-row]')).to_have_count(3)
        page.set_viewport_size(phone)
        expect(table.locator('.dt-card')).to_have_count(3)
    finally:
        context.close()


@pytest.mark.parametrize('layout', ['Table', 'Cards'])
def test_collection_saved_layout_remains_explicit_when_returning_from_default(browser, live_server, h, layout):
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 390, 'height': 844})
    try:
        navigate(page, 'people')
        table = page.locator('#main .dt[data-table="people"]')
        set_layout(table, 'Table')
        if layout == 'Cards':
            set_layout(table, 'Cards')
        toolbar(table, 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        name = 'Explicit people ' + layout
        dialog.get_by_label('View name', exact=True).fill(name)
        save_dialog(dialog, 'Save view')
        expect(table.get_by_role('button', name='Saved views: ' + name, exact=True)).to_be_visible()
        saved = h.get('coordinator', '/api/table-views?table=people')['views'][0]
        assert saved['definition']['layout'] == layout.lower()

        toolbar(table, 'Saved views').get_by_role('menuitemradio', name='Default view', exact=True).click()
        expect(table.locator('.dt-card')).to_have_count(3)
        toolbar(table, 'Saved views').get_by_role('menuitemradio', name=name, exact=True).click()
        rows = table.locator('tr[data-row]' if layout == 'Table' else '.dt-card')
        expect(rows).to_have_count(3)
        page.reload()
        expect(rows).to_have_count(3)
        page.set_viewport_size({'width': 1440, 'height': 1000})
        expect(rows).to_have_count(3)  # Both named layouts remain explicit across the breakpoint.
        assert h.get('coordinator', '/api/table-views?table=people')['views'][0] == saved
    finally:
        context.close()


def test_collection_sort_works_in_cards_board_headers_and_saved_clear(browser, live_server, h):
    """The shared toolbar on a real list (Requests; it replaced the Today table this used): Cards, Board, column headers
    and saved views keep one sort, and a saved "no sorting" stays unsorted after reload."""
    for on, kind in (('2026-11-04', 'leave'), ('2026-11-06', 'meeting'), ('2026-11-08', 'administrative')):
        h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': kind, 'private_reason': 'Fictional',
                                              'start': f'{on}T00:00:00+03:00', 'end': f'{on}T23:59:00+03:00'})
    ascending = ['Other', 'Meeting', 'Administrative']  # untyped leave is titled Other (maintenance)
    descending = ascending[::-1]
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        page.wait_for_function("() => document.querySelectorAll('#main .dt tbody tr').length === 3")
        lst = page.locator('#main .dt').first
        # One toolbar order everywhere: Saved view, Search, Filter, Group, Sort, View settings.
        assert lst.locator('.dt-toolbar > .dt-search-wrap, .dt-toolbar > .dt-tool').evaluate_all(
            "nodes => nodes.map(n => n.matches('.dt-tool') ? n.getAttribute('aria-label').split(/[:(]/)[0].trim() : 'Search')") == [
            'Saved views', 'Search', 'Filter', 'Group', 'Sort', 'View settings']

        def titles(where=''):
            return lst.locator(f'{where} .dt-card-title .request-title strong')

        choose(lst, 'Sort', 'Sort by', 'Dates')
        set_layout(lst, 'Cards')
        toolbar(lst, 'Sort').get_by_role('radiogroup', name='Sort direction', exact=True).get_by_role('radio', name='Descending', exact=True).click()
        close_popover(page)
        expect(titles()).to_have_text(descending)
        choose(lst, 'Group', 'Group by', 'Status')  # one column: every request is waiting
        set_layout(lst, 'Board')
        expect(titles('.dt-board')).to_have_text(descending)
        set_layout(lst, 'Table')
        when = lst.get_by_role('columnheader', name='Dates', exact=True)
        expect(when).to_have_attribute('aria-sort', 'descending')
        # The header opens the column menu (table_menu.ts): Sort A → Z.
        when.get_by_role('button', name='Dates', exact=True).click()
        page.get_by_role('menu', name='Dates column').get_by_role('menuitemradio', name='Sort A → Z', exact=True).click()
        expect(when).to_have_attribute('aria-sort', 'ascending')
        direction = toolbar(lst, 'Sort').get_by_role('radiogroup', name='Sort direction', exact=True)
        expect(direction.get_by_role('radio', name='Ascending', exact=True)).to_have_attribute('aria-checked', 'true')
        direction.get_by_role('radio', name='Descending', exact=True).click()
        close_popover(page)
        set_layout(lst, 'Board')
        toolbar(lst, 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('Ordered request board')
        save_dialog(dialog, 'Save view')
        saved = h.get('staff', '/api/table-views?table=requests')['views'][0]
        assert saved['definition']['sort'] == ['when', 'desc'] and saved['definition']['layout'] == 'board'
        page.reload(); navigate(page, 'requests')
        expect(lst.get_by_role('button', name='Saved views: Ordered request board', exact=True)).to_be_visible()
        expect(titles('.dt-board')).to_have_text(descending)
        choose(lst, 'Sort', 'Sort by', 'No sorting')
        expect(toolbar(lst, 'Sort').get_by_role('radiogroup', name='Sort direction')).to_have_count(0)
        close_popover(page)
        expect(lst.get_by_role('button', name='Saved views: Ordered request board (changed)', exact=True)).to_be_visible()
        toolbar(lst, 'Saved views').get_by_role('menuitem', name='Update “Ordered request board”', exact=True).click()
        expect(page.get_by_text('View updated.', exact=True)).to_be_visible()
        assert h.get('staff', '/api/table-views?table=requests')['views'][0]['definition']['sort'] is None
        page.reload(); navigate(page, 'requests')
        sort = toolbar(lst, 'Sort')
        expect(sort.get_by_role('menuitemradio', name='No sorting', exact=True)).to_have_attribute('aria-checked', 'true')
        expect(sort.get_by_role('radiogroup', name='Sort direction')).to_have_count(0)
    finally:
        context.close()
# Include this file in hosted supervision and team-lead qualification.

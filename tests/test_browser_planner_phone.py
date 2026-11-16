"""The planner on a phone reads day by day; switching views reuses the loaded rota; the rota list loads once; empty states are h2."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import auto_setup, auto_result, footer_working, browser, live_server, sign_in, navigate, pick_view, planner_filter, filter_pick, toolbar, choose, close_popover, planner_tool, reconnect  # noqa: F401

from .test_browser_phone_panels import assert_sheet
from .rules_support import rule_row
from .test_browser_rule_catalogue import shot

pytestmark = pytest.mark.browser
PHONE = {"width": 390, "height": 844}


def _draft_week(h):
    people = h.seed["people"]
    w = h.workspace("Phone planner rota", "2026-10-04", "2026-10-11")
    for day in range(4, 11):
        w = h.shift(w, f"2026-10-{day:02d}T08:00:00+03:00", f"2026-10-{day:02d}T12:00:00+03:00", people=[] if day == 6 else [people[day % len(people)]])["workspace"]
    return h.lifecycle(w, "validate")["workspace"]


def test_phone_planner_is_a_day_list_with_sessions_open_places_and_add(browser, live_server, h):
    _draft_week(h)
    context, page = sign_in(browser, live_server, "coordinator", viewport=PHONE)
    try:
        navigate(page, "planner")
        agenda = page.locator("#main .ag")
        expect(agenda).to_be_visible()
        expect(page.locator(".wk-grid")).to_have_count(0)
        expect(agenda.locator(".ag-day")).to_have_count(7)
        # The unstaffed session shows its open place.
        expect(agenda.locator(".ag-open").first).to_have_text("1 open")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        # A session opens the one session panel (S-09), with editing for a draft.
        agenda.locator(".ag-item").first.click()
        dialog = page.locator(".wk-panel")
        expect(dialog.get_by_role("button", name="Edit session", exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)
        # Each day adds a session on that day.
        page.get_by_role("button", name=re.compile(r"^Add session on .*5 Oct")).click()
        dialog = page.locator(".dock-panel")
        expect(dialog.get_by_label("Session start", exact=True)).to_have_value("Mon 5 Oct 2026")
        page.keyboard.press("Escape")
        # Month on a phone is the same day list over the month, never the wide grid.
        pick_view(page, "Month")
        expect(page.locator("#main .ag-day").first).to_be_visible()
        expect(page.locator(".wk-grid")).to_have_count(0)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    finally:
        context.close()

def test_phone_month_collapses_runs_of_empty_days(browser, live_server, h):
    """A month on a phone lists days with sessions and folds each run of empty days into one line ("3 days without
    sessions"); choosing it shows those days, each with its Add session. A week where sessions can be added still lists every day."""
    w = _draft_week(h)
    context, page = sign_in(browser, live_server, "coordinator", PHONE)
    try:
        navigate(page, f"schedule/month/2026-10-04?edit={w['id']}")
        gaps = page.locator("#schedule-view .ag-gap")
        expect(gaps.first).to_have_text(re.compile(r"^3 days without sessionsThu 1 Oct – Sat 3 Oct$"))  # 1–3 Oct
        expect(page.locator("#schedule-view .ag-day")).to_have_count(7)  # 4–10 Oct, each with a session
        expect(page.locator("#schedule-view .ag-none")).to_have_count(0)
        gaps.first.click()
        thursday = page.get_by_role("region", name=re.compile(r"1 Oct 2026"))
        expect(thursday).to_be_focused()
        expect(thursday.get_by_role("button", name=re.compile(r"^Add session on 1 Oct 2026"))).to_be_visible()
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        expect(page.locator("#schedule-view .ag-gap")).to_have_count(0)
    finally:
        context.close()


def test_phone_planner_list_grid_switch_matches_the_published_rota(browser, live_server, h):
    _draft_week(h)
    context, page = sign_in(browser, live_server, "coordinator", viewport=PHONE)
    try:
        navigate(page, "planner")
        layout = page.get_by_role("radiogroup", name="Calendar layout")
        expect(layout.get_by_role("radio", name="Agenda")).to_have_attribute("aria-checked", "true")
        expect(page.locator('.planner-toolbar').get_by_role('radiogroup', name='Calendar layout')).to_be_visible()
        expect(page.locator("#main .ag")).to_be_visible()
        # Grid shows the week grid; only the grid scrolls sideways, never the page.
        layout.get_by_role("radio", name="Grid").click()
        expect(page.locator(".wk-grid")).to_be_visible()
        expect(page.locator("#main .ag")).to_have_count(0)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        # The pick holds across views for this visit.
        pick_view(page, "Day")
        expect(page.locator(".wk-grid")).to_be_visible()
        pick_view(page, "Week")
        expect(page.locator(".wk-grid")).to_be_visible()
        # Back to List shows the day list again.
        page.get_by_role("radiogroup", name="Calendar layout").get_by_role("radio", name="Agenda").click()
        expect(page.locator("#main .ag")).to_be_visible()
        expect(page.locator(".wk-grid")).to_have_count(0)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    finally:
        context.close()


def test_switching_views_revalidates_instead_of_resending_the_rota(browser, live_server, h):
    w = _draft_week(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner")
        expect(page.locator(".wk-grid")).to_be_visible()
        detail = "/api/workspaces/" + w["id"]
        seen = []
        page.on("response", lambda r: r.url.endswith(detail) and seen.append(r.status))
        for name in ("Month", "People", "Coverage", "List", "Week"):
            pick_view(page, name)
        page.wait_for_timeout(300)
        assert seen and set(seen) == {304}, seen  # revalidated, never sent again
        # An edit elsewhere sends the new rota once.
        seen.clear()
        h.shift(h.detail(w)["workspace"], "2026-10-08T13:00:00+03:00", "2026-10-08T17:00:00+03:00")
        pick_view(page, "List")
        expect(page.locator("tr.dt-open")).to_have_count(8)
        assert 200 in seen and seen.count(200) <= 1, seen
    finally:
        context.close()


def test_rota_list_loads_once_and_empty_states_are_h2(browser, live_server, h):
    h.workspace("Empty rota")
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner")
        lists = []
        page.on("request", lambda r: "/api/workspaces?limit=50&offset=0" in r.url and lists.append(r.url))
        page.reload()
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "schedule")
        expect(page.locator(".nav-count, .sidebar").first).to_be_visible()
        page.wait_for_timeout(800)
        assert len(lists) <= 1, lists
        # The empty week sits under the page's h1: its title is an h2.
        expect(page.get_by_role("heading", name="No sessions this week", level=2)).to_be_visible()
    finally:
        context.close()


def test_list_toolbar_preserves_rota_scope_collection_view_and_empty_recovery(browser, live_server, h):
    from .test_browser import save_dialog
    w = _draft_week(h)
    names = {p["id"]: p["display_name"] for p in h.get("coordinator", "/api/catalogues")["people"]}
    lone = names[h.seed["people"][0]]  # works exactly one session of the week (9 Oct)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner"); pick_view(page, "List")
        bar = page.locator(".planner-toolbar")
        # One toolbar for every layout: one search box, owned by the planner.
        expect(bar.locator("input[type=search]:visible")).to_have_count(1)
        search = bar.get_by_label("Search sessions", exact=True)
        expect(search).to_be_visible()
        # The whole-rota scope remains recoverable even when it excludes every session.
        filters = planner_filter(page)
        names = {r["id"]: r["name"] for r in h.get("coordinator", "/api/catalogues")["locations"]}
        filter_pick(filters, "Location", names[h.seed["locations"][1]])
        expect(page.locator("tr.dt-open")).to_have_count(0)
        expect(page.get_by_text("Hidden by filters", exact=True)).to_be_visible()
        filters.get_by_role("button", name="Clear all filters", exact=True).click()
        expect(page.locator("tr.dt-open")).to_have_count(7)
        filter_pick(filters, "Location", names[h.seed["locations"][0]])
        close_popover(page)
        expect(bar.get_by_role("button", name=re.compile(r'^Filter: .+ \(1 active\)$'))).to_be_visible()
        search.fill("No such duty or person")
        expect(page.locator("tr.dt-open")).to_have_count(0)
        expect(search).to_be_visible()
        search.fill(lone)
        expect(page.locator("tr.dt-open")).to_have_count(1)
        page.get_by_role("checkbox", name=re.compile("^Select ")).last.check()
        expect(page.locator("#selection-bar")).to_contain_text("1 session selected")
        # The List layout's Group and Sort come from its table, in the same toolbar.
        choose(bar, "Group", "Group by", "Status")
        expect(bar.get_by_role("button", name="Group: Status", exact=True)).to_be_visible()
        expect(page.locator("tr.dt-group")).to_have_count(1)
        toolbar(bar, "Saved views").get_by_role("menuitem", name="New view…", exact=True).click()
        form = page.get_by_role("dialog", name="New view"); form.get_by_label("View name", exact=True).fill("Clinic rota scope")
        save_dialog(form, "Save view")
        rota = h.get("coordinator", "/api/table-views?table=planner")["views"][0]["definition"]["planner"]
        assert rota["filters"] == {"location_ids": [h.seed["locations"][0]]} and rota["search"] == lone
        assert rota["mode"] == "list"
        # Another view keeps the day in view (the address holds it) and the selection.
        day = re.search(r"#schedule/[^/]+/(\d{4}-\d\d-\d\d)", page.url).group(1)
        pick_view(page, "Week")
        # Changing the layout changes the saved view (marked changed), so the address still names it.
        expect(page).to_have_url(re.compile(rf"#schedule/v\.[^/]+/{day}"))
        expect(bar.get_by_role("button", name="Saved views: Clinic rota scope (changed)", exact=True)).to_be_visible()
        expect(page.locator("#selection-bar")).to_contain_text("1 session selected")
        page.reload(); navigate(page, "planner"); pick_view(page, "Clinic rota scope")
        expect(bar.get_by_role("button", name="Saved views: Clinic rota scope", exact=True)).to_be_visible()
        # Selecting the named view restores its saved query and scope.
        expect(bar.get_by_label("Search sessions", exact=True)).to_have_value(lone)
        expect(page.locator("tr.dt-open")).to_have_count(1)
        expect(planner_filter(page).locator(".cond-line .pick-button").first).to_contain_text(names[h.seed["locations"][0]])
        close_popover(page)
        assert h.detail(w)["workspace"]["status"] == "draft"
    finally:
        context.close()


@pytest.mark.parametrize("only", ["open", "issues"])
def test_attention_filters_apply_to_every_planner_layout(browser, live_server, h, only):
    h.policy({"kind": "maximum_participants", "maximum": 1, "aggregation": "assignment"})
    w = h.workspace("Mixed attention filter")
    w = h.shift(w, people=[h.seed["people"][0]])["workspace"]
    w = h.shift(w, "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00", people=[])["workspace"]
    w = h.shift(w, "2026-10-06T08:00:00+03:00", "2026-10-06T12:00:00+03:00", people=h.seed["people"][:2])["workspace"]
    h.lifecycle(w, "validate")
    validation = h.detail(w)["workspace"]["validation"]
    gaps = {r["shift_id"] for r in validation["coverage"] if r["status"] == "gap"}
    errors = {f["shift_id"] for f in validation["findings"] if f["severity"] == "error" and f.get("shift_id")}
    all_ids = {s["id"] for s in h.detail(w)["shifts"]}
    expected = gaps if only == "open" else errors
    assert expected and expected < all_ids, validation
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "planner")
        planner_filter(page).get_by_role("checkbox", name="Only sessions with open places" if only == "open" else "Only sessions that break a rule", exact=True).check()
        close_popover(page)
        for view in ("Week", "List", "People", "Coverage"):
            pick_view(page, view)
            # Rule-context nodes identify actual displayed sessions across all four renderers.
            shown = page.locator("#schedule-view [data-rk]").evaluate_all("""nodes => [...new Set(nodes.flatMap(n => {
              const t=JSON.parse(n.dataset.rk); return t.shift?[t.shift]:t.shifts||[];
            }))]""")
            assert set(shown) == expected, (view, shown, expected)
        planner_filter(page).get_by_role("button", name="Clear all filters", exact=True).click()
        close_popover(page)
        pick_view(page, "List")
        expect(page.locator("tr.dt-open")).to_have_count(3)
        assert h.detail(w)["workspace"]["status"] == "draft"
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [None, PHONE], ids=['desktop', 'phone'])
def test_fill_search_recovers_without_claiming_rules_reject_everyone(browser, live_server, h, viewport):
    from .test_fill_options import options
    w = h.shift(h.workspace('Fill search recovery'), people=[])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    shift = h.detail(w)['shifts'][0]
    eligible = [c for c in options(h, w, shift['id'])['candidates'] if c['status'] == 'ok']
    assert eligible
    blocked = h.shift(h.workspace('Actual rule conflict'), people=[])['workspace']
    h.policy({'kind': 'max_hours', 'hours': 1, 'window': 'week', 'include_overtime': True}, workspace_id=blocked['id'])
    blocked = h.lifecycle(blocked, 'validate')['workspace']
    blocked_shift = h.detail(blocked)['shifts'][0]
    assert not any(c['status'] == 'ok' for c in options(h, blocked, blocked_shift['id'])['candidates'])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport)
    try:
        navigate(page, 'planner')

        def fill(rota):
            page.get_by_role('button', name=re.compile(r'^Switch draft')).click()
            page.get_by_role('listbox', name='Drafts').get_by_role('option').filter(has=page.get_by_text(rota['name'], exact=True)).click()
            if viewport:
                pick_view(page, 'Coverage')
                page.locator('.cov-cell.short').first.click()
                expect(page.get_by_role('radiogroup', name='Calendar layout').get_by_role('radio', name='Agenda')).to_have_attribute('aria-checked', 'true')
                expect(page.locator('.wk-grid')).to_have_count(0)
            else:
                pick_view(page, 'Week')
                page.locator('button.pchip.gap').first.click()
            expect(page.locator('.wk-cands .wk-loading')).to_have_count(0)
            expect(page.locator('.wk-cands')).to_be_visible()

        fill(w)
        expect(page.locator('.wk-cand.ok')).to_have_count(len(eligible))
        search = page.get_by_label('Find a person', exact=True)
        search.fill('No such fictional person 94872')
        expect(page.locator('.wk-cands')).to_contain_text('No people match this search.')
        expect(page.get_by_text('Nobody passes every rule for this session.', exact=True)).to_have_count(0)
        expect(page.locator('.wk-cand.ok')).to_have_count(0)
        page.get_by_role('button', name='Clear search', exact=True).click()
        expect(search).to_have_value('')
        expect(search).to_be_focused()
        expect(page.locator('.wk-cand.ok')).to_have_count(len(eligible))
        page.keyboard.press('Escape')
        expect(page.locator('.wk-panel')).to_have_count(0)
        fill(blocked)
        expect(page.locator('.wk-cands')).to_contain_text('Nobody passes every rule for this session.')
        expect(page.get_by_role('button', name='Clear search', exact=True)).to_have_count(0)
        search.fill('No such fictional person 94872')
        expect(page.locator('.wk-cands')).to_contain_text('No people match this search.')
        page.get_by_role('button', name='Clear search', exact=True).click()
        expect(page.locator('.wk-cands')).to_contain_text('Nobody passes every rule for this session.')
        assert not h.detail(w)['shifts'][0]['participants']
        assert not h.detail(blocked)['shifts'][0]['participants']
    finally:
        context.close()


def test_phone_fill_routes_resolve_and_help_share_the_agenda_panel(browser, live_server, h):
    from .test_fill_options import options
    h.policy({'kind': 'min_rest', 'hours': 12})
    w = h.shift(h.workspace('Agenda panels over two weeks', '2026-10-04', '2026-10-18'), people=[])['workspace']
    w = h.shift(w, '2026-10-12T08:00:00+03:00', '2026-10-12T12:00:00+03:00', people=[])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    shifts = sorted(h.detail(w)['shifts'], key=lambda s: s['start'])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=PHONE)
    errors = []
    # The harness answers the worker probe 503 on purpose (test_browser.py: no worker beside the test server), and the
    # app asks it on every planner (D-062): that one resource error is the harness's; every other error still fails.
    harness = lambda message: (message.location or {}).get('url', '').endswith('/health/service')
    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' and not harness(message) else None)
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        navigate(page, 'planner')
        page.get_by_role('button', name=re.compile(r'^Switch draft')).click()
        page.get_by_role('listbox', name='Drafts').get_by_role('option').filter(has=page.get_by_text(w['name'], exact=True)).click()
        # Help explains the views that draw sessions (Year and Month show day health and have no help panel).
        for index, view in enumerate(('Week', 'Day', 'People', 'Coverage', 'List')):
            pick_view(page, view)
            if view == 'List':
                # A phone shows the list as cards.
                expect(page.locator('.dt-card')).to_have_count(2)
            # Help preserves the renderer and the existing collection controls, rather than switching layouts.
            bar = page.locator('.planner-toolbar').element_handle() if view == 'List' else None
            help_button = page.get_by_role('button', name='How to read this schedule', exact=True)
            help_button.click()
            expect(help_button).to_have_attribute('aria-pressed', 'true')
            help_panel = page.locator('.wk-panel')
            expect(help_panel).to_contain_text('How to read this schedule')
            expect(help_panel).to_contain_text('The status footer shows open places')
            expect(page.locator('#schedule-view')).to_have_attribute('aria-label', f'{view} view')
            if index % 2:
                page.keyboard.press('Escape')
            else:
                help_panel.get_by_role('button', name='Close help', exact=True).click()
            expect(page.locator('.wk-panel')).to_have_count(0)
            expect(help_button).to_be_focused()
            expect(help_button).to_have_attribute('aria-pressed', 'false')
            if bar:
                assert bar.evaluate('node=>node.isConnected')
                search = page.get_by_label('Search sessions', exact=True)
                expect(search).to_have_count(1)
                search.fill('No such session')
                expect(page.locator('.dt-card')).to_have_count(0)
                page.get_by_label('Search sessions', exact=True).fill('')
                expect(page.locator('.dt-card')).to_have_count(2)
        pick_view(page, 'Coverage')
        page.locator('.cov-cell.short').first.click()
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'Week view')
        expect(page.locator('.ag')).to_be_visible()
        expect(page.locator('.wk-cand.ok').first).to_be_visible()
        expect(page.locator('.wk-grid')).to_have_count(0)
        page.keyboard.press('Escape')
        expect(page.locator('.wk-panel')).to_have_count(0)
        pick_view(page, 'List')
        page.locator('.dt-card').first.locator('.dt-more').click()
        page.get_by_role('menuitem', name='Fill open place', exact=True).click()
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'Week view')
        expect(page.locator('.wk-cand.ok').first).to_be_visible()
        expect(page.get_by_role('radiogroup', name='Calendar layout').get_by_role('radio', name='Agenda')).to_have_attribute('aria-checked', 'true')
        page.keyboard.press('Escape')
        planner_tool(page, 'Resolve problems one by one')
        panel = page.locator('.wk-panel')
        expect(panel.locator('.resolve-bar')).to_contain_text('2 to resolve')
        expected = {}
        for index, shift in enumerate(shifts):
            expect(panel.locator('.wk-panel-head small:not(.side-pos)')).to_contain_text('4 Oct' if index == 0 else '12 Oct')
            expect(panel.locator('.wk-cand.ok').first).to_be_visible()
            current = h.detail(w)['workspace']
            candidate = next(c for c in options(h, current, shift['id'])['candidates'] if c['status'] == 'ok')
            expected[shift['id']] = candidate['person_id']
            expect(panel.locator('.wk-cand.ok strong').first).to_have_text(candidate['display_name'])
            panel.get_by_role('button', name=re.compile(r'^Assign (?!.* with a warning$)')).first.click()
            if index == 0:
                expect(panel.locator('.resolve-bar')).to_contain_text('1 to resolve')
                expect(page.locator('.planner-toolbar .wk-nav strong')).to_contain_text('11 Oct')
        expect(page.get_by_text('Everything is resolved', exact=False)).to_be_visible()
        detail = h.detail(w)
        assert {s['id']: [p['person_id'] for p in s['participants']] for s in detail['shifts']} == {sid: [pid] for sid, pid in expected.items()}
        assert detail['workspace']['status'] == 'draft'
        assert not any(c['status'] == 'gap' for c in detail['workspace']['validation']['coverage'])
        page.reload(); navigate(page, 'planner'); pick_view(page, 'List')
        expect(page.locator('.dt-card')).to_have_count(2)
        assert {s['id']: [p['person_id'] for p in s['participants']] for s in h.detail(w)['shifts']} == {sid: [pid] for sid, pid in expected.items()}
        assert h.get('coordinator', '/api/publications')['publications'] == []
        # A person panel retains real rule results after the same resolved cache is reused in another renderer.
        pick_view(page, 'Week')
        page.get_by_role('radiogroup', name='Calendar layout').get_by_role('radio', name='Grid').click()
        page.locator('button.pchip:not(.gap)').first.click()
        rules = page.get_by_label('Rules applied to this assignment', exact=True)
        expect(rules).not_to_contain_text('Loading the rules')
        result = h.get('coordinator', '/api/workspaces/' + w['id'] + '/shifts/' + shifts[0]['id'] + '/rules')
        own = next(r for r in result['people'] if r['person_id'] == expected[shifts[0]['id']])
        assert own['rules']
        expect(rule_row(rules, own['rules'][0]['name'])).to_be_visible()  # the row says a sentence; its rule is data-rk-name
        page.get_by_role('radiogroup', name='Calendar layout').get_by_role('radio', name='Agenda').click()
        for view in ('Day', 'People', 'Coverage', 'List'):
            pick_view(page, view)
            expect(page.locator('.wk-panel')).to_contain_text('Working this session')
            expect(rules).not_to_contain_text('Loading the rules')
            expect(rule_row(rules, own['rules'][0]['name'])).to_be_visible()  # the row says a sentence; its rule is data-rk-name
        page.get_by_role('button', name='Close panel', exact=True).click()
        expect(page.locator('.wk-panel')).to_have_count(0)
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [PHONE, {'width': 1440, 'height': 900}], ids=['phone', 'desktop'])
def test_a_schedule_redraw_keeps_focus_on_the_same_control(browser, live_server, h, viewport):
    """A redraw that lands after a toolbar action (a slow draft switch, a live update) replaces the top bar and the
    toolbar: keyboard focus stays on the same control instead of dropping to the page (the help-button flake)."""
    _draft_week(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport)
    try:
        navigate(page, 'planner')
        for control in (page.get_by_role('button', name='How to read this schedule', exact=True),
                        page.locator('.planner-toolbar').get_by_role('button', name=re.compile(r'^View settings'))):
            control.focus()
            old = control.element_handle()
            reconnect(page)  # a live refresh: the whole Schedule is drawn again
            page.wait_for_function('node => !node.isConnected', arg=old)
            expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
            expect(control).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [PHONE, {'width': 1440, 'height': 900}], ids=['phone', 'desktop-to-phone'])
def test_hidden_completed_jobs_load_on_open_and_live_jobs_still_auto_open(browser, live_server, h, viewport):
    from apps.worker.main import process_one
    w = h.shift(h.workspace('Deferred completed job detail'))['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    completed = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'}, revision=w['revision'])['job']
    assert process_one(h.app.state.engine)
    done = h.get('coordinator', '/api/jobs/' + completed['id'])['job']
    assert done['status'] == 'succeeded' and done['result']['valid']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport)
    page.emulate_media(color_scheme='dark' if viewport == PHONE else 'light')
    detail_url = live_server + '/api/jobs/' + completed['id']
    jobs_url = live_server + '/api/jobs?workspace_id=' + w['id']
    details = []
    page.on('request', lambda request: details.append(request.url) if request.method == 'GET' and request.url == detail_url else None)
    try:
        with page.expect_response(lambda response: response.url == jobs_url) as initial:
            navigate(page, 'planner')
        assert initial.value.status == 200
        assert initial.value.json()['jobs'][0]['id'] == completed['id']
        expect(page.locator('#auto-panel')).not_to_be_visible()
        assert details == []
        for view in ('Month', 'People', 'Coverage', 'List', 'Week'):
            previous = page.locator('#auto-panel').element_handle()
            pick_view(page, view)  # reuses the jobs read on open: nothing is polled while no run is going (PL-03)
            page.wait_for_function('previous=>!previous.isConnected', arg=previous)
            expect(page.locator('#schedule-view')).to_have_attribute('aria-label', f'{view} view')
            expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
            expect(page.locator('#auto-panel')).not_to_be_visible()
            assert details == [], (view, details)
        with page.expect_response(lambda response: response.url == detail_url) as opened:
            page.get_by_role('button', name='Auto-schedule', exact=True).click()
        assert opened.value.status == 200 and opened.value.json()['job']['id'] == completed['id']
        expect(page.locator('#auto-panel').get_by_text('Full rota check passed', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Open Fix list', exact=True)).to_be_visible()  # SW3-A-07: a check opens the Fix list
        panel = page.locator('#auto-panel')
        if viewport != PHONE:
            page.set_viewport_size(PHONE)
            expect(page.locator('#schedule-view .ag')).to_be_visible()
        assert_sheet(page, panel)
        # D76 (run card): no steps; the setup and the result are both reachable without sideways scrolling.
        expect(panel.locator('.step-rail')).to_have_count(0)
        auto_setup(panel)
        assert panel.evaluate('e=>e.scrollWidth<=e.clientWidth+1')
        auto_result(panel)
        assert panel.evaluate('e=>e.scrollWidth<=e.clientWidth+1')
        shot(page, 'auto-panel-phone-open-' + str(viewport['width']), full_page=False)
        # Esc and Close share the same state and return to the header's trigger.
        opener = page.get_by_role('button', name='Auto-schedule', exact=True)
        panel.get_by_role('button', name='Close auto-schedule', exact=True).press('Escape')
        expect(panel).not_to_be_visible()
        expect(opener).to_have_attribute('aria-expanded', 'false')
        expect(opener).to_be_focused()
        opener.click()
        assert_sheet(page, panel)
        panel.get_by_role('button', name='Close auto-schedule', exact=True).click()
        expect(panel).not_to_be_visible()
        expect(opener).to_be_focused()
        # A real live check remains queued until the worker runs; the page's next read finds it. D76: a check is the
        # footer's working item (with Cancel), never the Auto-schedule panel's, so the panel stays closed.
        # (Nothing is polled while no run is going, PL-03: the page's next read is what finds a job started elsewhere.)
        current = h.detail(w)['workspace']
        queued = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'}, revision=current['revision'])['job']
        assert queued['status'] == 'queued'
        with page.expect_response(lambda response: response.url == jobs_url and response.status == 200
                                  and any(j['id'] == queued['id'] and j['status'] == 'queued'
                                          for j in response.json()['jobs'])) as discovered:
            page.reload()
        assert any(j['id'] == queued['id'] and j['status'] == 'queued' for j in discovered.value.json()['jobs'])
        # DL D-063, kept by DL-018 (Design Lead) + Master 10 Nov: one wording rule; a queued check says Waiting to check
        pop = footer_working(page, 'Waiting to check', phone=True)  # the footer is one line on a phone: behind its +N
        expect(pop.get_by_role('button', name='Cancel: waiting to check', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.locator('#auto-panel')).to_be_hidden()
        expect(page.get_by_role('button', name='Auto-schedule', exact=True)).to_have_attribute('aria-expanded', 'false')
        assert process_one(h.app.state.engine)
        expect(page.locator('.app-footer .long-work')).to_have_count(0, timeout=15000)
        opener.click()
        expect(page.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_enabled()
        expect(page.get_by_role('button', name='Check the whole rota', exact=True)).to_be_enabled()
        latest = h.get('coordinator', '/api/jobs/' + queued['id'])['job']
        assert latest['status'] == 'succeeded' and latest['result']['valid']
        assert h.detail(w)['workspace']['status'] == 'draft'
        # The final action is reachable above the status footer with native keyboard focus.
        last_action = panel.get_by_role('button', name='Run auto-schedule', exact=True)
        last_action.focus()
        expect(last_action).to_be_focused()
        assert last_action.evaluate("""e=>{const r=e.getBoundingClientRect();return r.top>=document.querySelector('.topbar').getBoundingClientRect().bottom&&
            r.bottom<=document.querySelector('.app-footer').getBoundingClientRect().top;}""")
        shot(page, 'auto-panel-phone-last-action-' + str(viewport['width']), full_page=False)
        # A sibling panel replaces Auto in the same slot and is also before the calendar.
        planner_tool(page, 'Rules in this rota')
        rules = page.get_by_role('dialog', name='Rules in this rota', exact=True)
        expect(panel).not_to_be_visible()
        expect(page.locator('.side-panel:visible')).to_have_count(1)
        assert_sheet(page, rules)
        rules.get_by_role('button', name='Close rules panel (Esc)', exact=True).click()
        expect(rules).to_have_count(0)
        opener.click()
        assert_sheet(page, panel)
    finally:
        context.close()

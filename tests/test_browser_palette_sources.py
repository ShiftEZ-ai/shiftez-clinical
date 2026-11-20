"""Ctrl+K resolves normalized matches to the exact permitted record, including unloaded rota history."""
import re
import uuid

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import DEFAULT_CONTACT_TYPES
from .governance_support import developer, identity
from .test_browser import browser, live_server, sign_in, navigate, planner_filter, view_settings, close_popover  # noqa: F401
from .test_workspace_search import workspace_row

pytestmark = pytest.mark.browser
PRESENTATIONS = [(1440, "light"), (1440, "dark"), (390, "light"), (390, "dark")]
# D68: who sees the Extension comes from the seeded contact type, not a copied role list.
EXTENSION_ROLES = set(next(t for t in DEFAULT_CONTACT_TYPES if t["legacy_field"] == "work_extension")["visibility"]["roles"])


def search(page, query):
    page.keyboard.press("Control+k")
    menu = page.get_by_role("dialog", name="Command menu")
    menu.get_by_role("combobox").fill(query)
    return menu


@pytest.mark.parametrize("width,scheme", PRESENTATIONS)
def test_palette_normalizes_ranks_exact_matches_and_owns_async_results(browser, live_server, width, scheme):
    context, page = sign_in(browser, live_server, "staff", viewport={"width": width, "height": 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, "home")
        page.locator("#main").focus()
        page.evaluate("""async () => {
          const {openPalette}=await window.importBundle('palette.js');window.palettePicked=null;window.paletteLoads=[];
          const item=(label,keywords='')=>({label,keywords,group:'People',icon:'people',run:()=>window.palettePicked=label});
          window.openTestPalette=()=>openPalette([
            item('A broad alias','JoseQuinn'),item('Dr. José Quinn'),item('JoseQuinn'),item('نيلوفر الجبلي'),
          ],q=>new Promise((resolve,reject)=>window.paletteLoads.push({q,resolve:labels=>resolve(labels.map(x=>item(x))),reject})));
          window.openTestPalette();
        }""")
        menu = page.get_by_role("dialog", name="Command menu")
        field = menu.get_by_role("combobox")
        field.fill("josequinn")
        expect(menu.get_by_role("option").nth(0)).to_contain_text("JoseQuinn")
        expect(menu.get_by_role("option").nth(1)).to_contain_text("Dr. José Quinn")
        expect(menu.get_by_role("option").nth(2)).to_contain_text("A broad alias")
        field.fill("نيلوفرالجبلي")
        expect(menu.get_by_role("option")).to_have_count(1)
        field.fill("Alpha")
        page.wait_for_function("() => window.paletteLoads.some(x=>x.q==='Alpha')")
        field.fill("Beta")
        page.wait_for_function("() => window.paletteLoads.some(x=>x.q==='Beta')")
        page.evaluate("paletteLoads.find(x=>x.q==='Beta').resolve(['Beta current result'])")
        expect(menu.get_by_role("option")).to_have_text("Beta current result")
        page.evaluate("paletteLoads.find(x=>x.q==='Alpha').resolve(['Alpha obsolete result'])")
        expect(menu.get_by_role("option")).to_have_text("Beta current result")
        field.fill("Failed")
        page.wait_for_function("() => window.paletteLoads.some(x=>x.q==='Failed')")
        page.evaluate("paletteLoads.find(x=>x.q==='Failed').reject(new Error('synthetic read failure'))")
        retry = menu.get_by_role("button", name="Retry search", exact=True)
        expect(retry).to_be_visible()
        expect(field).not_to_have_attribute("aria-activedescendant", re.compile(".+"))
        field.press("ArrowDown")
        expect(field).not_to_have_attribute("aria-activedescendant", re.compile(".+"))
        retry.click()
        expect(field).to_be_focused()
        page.wait_for_function("() => window.paletteLoads.filter(x=>x.q==='Failed').length===2")
        page.evaluate("paletteLoads.filter(x=>x.q==='Failed')[1].resolve(['Failed read recovered'])")
        expect(menu.get_by_role("option")).to_have_text("Failed read recovered")
        field.focus()
        field.dispatch_event("keydown", {"key": "Enter", "isComposing": True})
        expect(menu).to_be_visible()
        field.press("Enter")
        expect(menu).to_have_count(0)
        assert page.evaluate("palettePicked") == "Failed read recovered"
        expect(page.locator("#main")).to_be_focused()
        # A closed menu cannot be repopulated by its last response.
        page.evaluate("() => { window.openTestPalette(); }")
        field.fill("Closed")
        page.wait_for_function("() => window.paletteLoads.some(x=>x.q==='Closed')")
        field.dispatch_event("keydown", {"key": "Escape", "isComposing": True})
        expect(menu).to_be_visible()
        field.press("Escape")
        expect(menu).to_have_count(0)  # One Escape closes even while a nonempty search is loading.
        expect(page.locator("#main")).to_be_focused()
        page.evaluate("paletteLoads.find(x=>x.q==='Closed').resolve(['Closed private result'])")
        expect(menu).to_have_count(0)
        expect(page.get_by_text("Closed private result", exact=True)).to_have_count(0)
        expect(page.locator("#main")).to_be_focused()
        # Empty-query Escape follows the same close/focus path.
        page.evaluate("() => { window.openTestPalette(); }")
        expect(field).to_have_value("")
        field.press("Escape")
        expect(menu).to_have_count(0)
        expect(page.locator("#main")).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize("role", ["admin", "coordinator", "approver", "credentialer", "programme_lead", "auditor", "staff"])
@pytest.mark.parametrize("width,scheme", [(1440, "light"), (390, "dark")])
def test_person_results_open_exact_duplicates_from_home_and_survive_reload(browser, live_server, h, role, width, scheme):
    if role == "programme_lead":
        identity(h, role, [role])
    for section in ("Alpha section", "Beta section"):
        person = h.configure("people", {"display_name": "Dr. Olivia Quinn", "title": "Registrar",
                                        "directory_section": section, "work_extension": "6543" if section.startswith("Beta") else "1234",
                                        "staff_number": "PRIVATE-BETA" if section.startswith("Beta") else "PRIVATE-ALPHA"})["record"]
    context, page = sign_in(browser, live_server, role, viewport={"width": width, "height": 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, "home")
        menu = search(page, "oliviaquinn")
        choices = menu.get_by_role("option").filter(has_text="Dr. Olivia Quinn")
        expect(choices).to_have_count(2)
        choices.filter(has_text="Beta section").click()
        expect(page).to_have_url(re.compile(r"#people\?open=" + person["id"] + "$"))
        panel = page.get_by_role("region", name=person["display_name"], exact=True)
        expect(panel).to_be_visible()
        expect(panel).to_contain_text("Beta section")
        expect(panel.locator(".pc-more")).to_have_count(1)  # The card has loaded, not "Loading…".
        if role in EXTENSION_ROLES:
            expect(panel).to_contain_text("6543")
        else:
            expect(panel).not_to_contain_text("6543")
        expect(panel).not_to_contain_text("1234")
        expect(panel).not_to_contain_text("Alpha section")
        if role not in {"admin", "coordinator"}:
            expect(panel).not_to_contain_text("PRIVATE-BETA")
        if role == "staff":
            expect(panel.get_by_role("button", name=re.compile("^Edit"))).to_have_count(0)
        page.reload()
        expect(panel).to_be_visible()
        expect(panel).to_contain_text("Beta section")
        expect(panel.locator(".pc-more")).to_have_count(1)  # The card has loaded, not "Loading…".
        if role in EXTENSION_ROLES:
            expect(panel).to_contain_text("6543")
        else:
            expect(panel).not_to_contain_text("6543")
        page.keyboard.press("Escape")
        expect(panel).to_have_count(0)
        expect(page).to_have_url(re.compile(r"#people$"))
        unknown = str(uuid.uuid4())
        asked = []
        page.on("request", lambda request: asked.append(request.url) if unknown in request.url else None)
        page.evaluate("id=>location.hash='people?open='+id", unknown)
        # DL-027: a link to someone this reader is not shown lands on the list with a note true whether the person is
        # hidden or removed; no request names the id, so no 403-vs-404 tells the two apart (maintenance).
        expect(page.locator("#main .notice.info")).to_have_text("That person can't be shown.")
        assert asked == []
        expect(page.locator("#main .dt")).to_be_visible()
        expect(page.locator(".person-panel")).to_have_count(0)
    finally:
        context.close()


def test_person_search_includes_late_catalogue_entries_and_import_aliases(browser, live_server, h):
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([m.Person(organisation_id=h.seed["organisation_id"], display_name=f"A large directory {n:03}") for n in range(410)])
        person = m.Person(organisation_id=h.seed["organisation_id"], display_name="Z Last Person", directory_section="Late fixture")
        db.add(person); db.flush(); person_id = person.id
        db.add(m.SourceMapping(department_id=h.seed["department_id"], source_name="Synthetic alias", source_key="alias:latepersonlogin",
                               target_kind="people", target_id=person.id))
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "home")
        menu = search(page, "ZLastPerson")
        expect(menu.get_by_role("option").filter(has_text="Z Last Person")).to_be_visible()
        menu.get_by_role("combobox").press("Escape")
        menu = search(page, "latepersonlogin")
        menu.get_by_role("option").filter(has_text="Z Last Person").click()
        expect(page).to_have_url(re.compile(r"#people\?open=" + person_id + "$"))
        expect(page.get_by_role("region", name="Z Last Person", exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize("width,scheme", [(1440, "light"), (390, "dark")])
def test_rota_search_opens_unloaded_draft_and_exact_publication_sources(browser, live_server, h, width, scheme):
    old = h.shift(h.workspace("Original searched publication"))["workspace"]
    old_pub = h.publish(old)["publication_id"]
    amendment = h.workspace("Current searched publication", base=old_pub)
    current_pub = h.publish(amendment)["publication_id"]
    with transaction(h.app.state.engine, write=True) as db:
        draft = workspace_row(h, "Historical unique draft", 1000)
        db.add_all([draft, *[workspace_row(h, f"Recent unrelated draft {n}", n) for n in range(60)]]); db.flush()
        draft_id = draft.id
    context, page = sign_in(browser, live_server, "coordinator", viewport={"width": width, "height": 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, "home")
        menu = search(page, "Historicalunique")
        menu.get_by_role("option", name=re.compile("^Historical unique draft")).click()
        expect(page).to_have_url(re.compile(r"#schedule/week/2026-10-04\?edit=" + draft_id + "$"))
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "schedule")
        page.reload()
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "schedule")
        expect(page).to_have_url(re.compile("edit=" + draft_id))
        # The finite publications list may omit an older version. Its exact authorized source remains readable.
        page.route(re.compile(r"/api/publications$"), lambda route: route.fulfill(json={"publications": []}))
        for name, publication_id, wording in (("Original searched publication", old_pub, "superseded and read-only"),
                                               ("Current searched publication", current_pub, "published and read-only")):
            menu = search(page, name)
            menu.get_by_role("option").filter(has_text=name).click()
            expect(page).to_have_url(re.compile(r"#schedule/week/2026-10-04\?edit=none&source=" + publication_id + "$"))
            expect(page.locator("#main")).to_contain_text(name)
            expect(page.locator("#main")).to_contain_text(wording)
            expect(page.get_by_role("button", name="New session", exact=True)).to_have_count(0)
            page.reload()
            expect(page.locator("#main")).to_contain_text(name)
            expect(page.locator("#main")).to_contain_text(wording)
            bar = page.locator('.planner-toolbar')
            bar.get_by_role('button', name='Next week', exact=True).click()
            later = re.compile(r'#schedule/week/2026-10-11\?edit=none&source=' + publication_id + '$')
            expect(page).to_have_url(later)
            page.reload()
            expect(page).to_have_url(later)
            expect(page.locator('#main')).to_contain_text(name)
            bar.get_by_role('button', name='Previous week', exact=True).click()
            expect(page).to_have_url(re.compile(r'/2026-10-04\?edit=none&source=' + publication_id + '$'))
            dates = page.evaluate("""() => {
                const day=new Date(),format=d=>d.toLocaleDateString('en-CA',{timeZone:'Asia/Riyadh'});
                const today=format(day);day.setDate(day.getDate()+28);return {today,away:format(day)};
            }""")
            navigate(page, 'schedule/week/' + dates['away'] + '?edit=none&source=' + publication_id)
            bar.get_by_role('button', name='Go to today', exact=True).click()
            expect(page).to_have_url(re.compile('/' + dates['today'] + r'\?edit=none&source=' + publication_id + '$'))
        # Stale publication links report a read failure rather than showing an unrelated draft.
        page.evaluate("id=>location.hash='schedule/week/2026-10-04?edit=none&source='+id", str(uuid.uuid4()))
        expect(page.locator("#main [role=alert]")).to_contain_text("Couldn’t load the schedule.")
        expect(page.get_by_role("button", name="New session", exact=True)).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_publication_sources_release_ownership_for_drafts_and_year(browser, live_server, h, width, scheme):
    from .test_browser import new_draft, save_dialog

    original = h.shift(h.workspace('Historical source ownership'))['workspace']
    old_pub = h.publish(original)['publication_id']
    current = h.workspace('Currently published ownership', base=old_pub)
    current_pub = h.publish(current)['publication_id']
    draft = h.workspace('Separate editable November', '2026-11-01', '2026-11-08')
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        old_route = 'schedule/week/2026-10-11?edit=none&source=' + old_pub
        current_route = 'schedule/week/2026-10-04?edit=none&source=' + current_pub
        navigate(page, old_route)
        expect(page.locator('#main')).to_contain_text('superseded and read-only')
        navigate(page, current_route)
        expect(page.locator('#main')).to_contain_text('Currently published ownership · version')
        page.go_back()
        expect(page).to_have_url(re.compile('#' + re.escape(old_route) + '$'))
        expect(page.locator('#main')).to_contain_text('Historical source ownership · version')
        page.reload()
        expect(page).to_have_url(re.compile('#' + re.escape(old_route) + '$'))
        page.go_forward()
        expect(page).to_have_url(re.compile('#' + re.escape(current_route) + '$'))
        expect(page.locator('#main')).to_contain_text('Currently published ownership · version')

        page.get_by_role('button', name='Choose a draft', exact=True).click()
        page.get_by_role('listbox', name='Drafts', exact=True).get_by_role('option').filter(has_text=draft['name']).click()
        expect(page).to_have_url(re.compile(r'#schedule/week/2026-11-01\?edit=' + draft['id'] + '$'))
        expect(page.locator('#main')).not_to_contain_text('published and read-only')
        page.reload()
        expect(page).to_have_url(re.compile(r'edit=' + draft['id'] + '$'))

        navigate(page, old_route)
        new_draft(page)
        dialog = page.get_by_role('dialog', name='New rota', exact=True)
        dialog.get_by_label('Rota name', exact=True).fill('Created after publication source')
        dialog.get_by_label('First day', exact=True).fill('2026-12-01')
        dialog.get_by_label('Last day', exact=True).fill('2026-12-07')
        save_dialog(dialog, 'Create draft')
        # The app opens the new draft once the server has created it; only then read it back.
        expect(page).to_have_url(re.compile(r'#schedule/week/2026-12-01\?edit=[^&]+$'))
        created = next(w for w in h.get('coordinator', '/api/workspaces')['workspaces'] if w['name'] == 'Created after publication source')
        expect(page).to_have_url(re.compile(r'#schedule/week/2026-12-01\?edit=' + created['id'] + '$'))
        expect(page.locator('#main')).not_to_contain_text('superseded and read-only')

        navigate(page, old_route)
        page.locator('#main').focus()
        page.keyboard.press('y')
        expect(page).to_have_url(re.compile(r'#schedule/year/2026-10-11\?edit=none$'))
        expect(page.get_by_text('Year shows currently published rotas.', exact=True)).to_be_visible()
        expect(page.locator('#main')).not_to_contain_text('superseded and read-only')
        year = page.locator('.sd-year')
        expect(year.get_by_role('button', name=re.compile('Currently published ownership, published'))).not_to_have_count(0)
        expect(year.get_by_role('button', name=re.compile('Historical source ownership'))).to_have_count(0)
        page.reload()
        expect(page).to_have_url(re.compile(r'#schedule/year/2026-10-11\?edit=none$'))
        expect(year).to_be_visible()
        expect(year.get_by_role('button', name=re.compile('Currently published ownership, published'))).not_to_have_count(0)
    finally:
        context.close()


def test_programme_lead_results_do_not_link_forbidden_publications_and_operator_has_no_people(browser, live_server, h):
    identity(h, "programme_lead", ["programme_lead"])
    h.publish(h.shift(h.workspace("Publication source permission"))["workspace"])
    for role in ("programme_lead", "operator"):
        context, page = sign_in(browser, live_server, role)
        try:
            navigate(page, "home")
            menu = search(page, "Publication source permission" if role == "programme_lead" else "Alex")
            expect(menu.get_by_text("No matches. Try a page name, a person or an action.", exact=True)).to_be_visible()
            expect(menu.get_by_role("option")).to_have_count(0)
            expect(menu.get_by_role("button", name="Retry search", exact=True)).to_have_count(0)
        finally:
            context.close()


@pytest.mark.parametrize("width,scheme", [(1440, "dark"), (390, "light")])
def test_page_section_and_action_results_keep_their_exact_destinations(browser, live_server, h, width, scheme):
    draft = h.shift(h.workspace("Action source draft"))["workspace"]
    context, page = sign_in(browser, live_server, developer(h), viewport={"width": width, "height": 900})
    try:
        page.emulate_media(color_scheme=scheme)
        for query, label, route in (("Home", "Home", "home"), ("Audit log", "Audit log", "system/audit"),
                                     ("scoring weights", "Auto-schedule", "configuration/auto_schedule")):
            menu = search(page, query)
            menu.get_by_role("option", name=re.compile("^" + re.escape(label))).first.click()
            expect(page).to_have_url(re.compile("#" + route + "$"))
            expect(page.locator("#main")).to_have_attribute("data-loaded-view", route.split("/")[0])
        menu = search(page, "New rota")
        menu.get_by_role("option", name="New rota", exact=True).click()
        dialog = page.get_by_role("dialog", name="New rota", exact=True)
        expect(dialog).to_be_visible()
        expect(dialog.get_by_label("Rota name", exact=True)).to_be_visible()
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(dialog).to_have_count(0)
        menu = search(page, "generate")
        menu.get_by_role("option", name=re.compile("^Auto-schedule Action source draft")).click()
        expect(page).to_have_url(re.compile(r"#schedule/week/2026-10-04\?edit=" + draft["id"] + "$"))
        expect(page.locator("#auto-panel")).to_be_visible()
        expect(page.locator("#auto-panel").get_by_role("button", name="Run auto-schedule", exact=True)).to_be_visible()
    finally:
        context.close()


def test_missing_or_changed_workspace_never_substitutes_another_draft(browser, live_server, h):
    h.workspace("Unrelated draft must not replace a search result")
    changing = h.shift(h.workspace("Draft changing after search", "2026-10-11", "2026-10-18"),
                       start="2026-10-11T08:00:00+03:00", end="2026-10-11T12:00:00+03:00")["workspace"]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, changing["id"]).created_at = workspace_row(h, "older timestamp", 1000).created_at
        db.add_all([workspace_row(h, f"Newer unrelated rota {n}", n) for n in range(60)])
    assert changing["id"] not in {w["id"] for w in h.get("coordinator", "/api/workspaces?limit=50")["workspaces"]}
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "home")
        page.evaluate("id=>location.hash='schedule/week/2026-10-04?edit='+id", str(uuid.uuid4()))
        expect(page.get_by_text("That rota is not available. Showing the schedule without it.", exact=True)).to_be_visible()
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "schedule")
        expect(page).to_have_url(re.compile(r"edit=none(?:&|$)"))
        expect(page.get_by_role("button", name="New session", exact=True)).to_have_count(0)
        menu = search(page, changing["name"])
        result = menu.get_by_role("option", name=re.compile("^" + re.escape(changing["name"])))
        expect(result).to_be_visible()
        # The user selected a real draft, then an independent reviewer/publication completed before activation.
        publication = h.publish(changing)["publication_id"]
        assert changing["id"] not in {w["id"] for w in h.get("coordinator", "/api/workspaces?limit=50")["workspaces"]}
        result.click()
        published_route = re.compile(r"#schedule/week/2026-10-11\?edit=none&source=" + publication + "$")
        expect(page).to_have_url(published_route)
        expect(page.locator("#main")).to_contain_text("Draft changing after search · version")
        expect(page.locator("#main")).to_contain_text("published and read-only")
        expect(page.get_by_role("button", name="New session", exact=True)).to_have_count(0)
        page.reload()
        expect(page).to_have_url(published_route)
        expect(page.locator("#main")).to_contain_text("Draft changing after search · version")
        expect(page.locator("#main")).to_contain_text("published and read-only")
        expect(page.get_by_role("button", name="New session", exact=True)).to_have_count(0)
        # An old edit link has only the workspace ID; detail must resolve its own exact publication too.
        page.evaluate("id=>location.hash='schedule/week/2026-10-11?edit='+id", changing["id"])
        expect(page).to_have_url(published_route)
        expect(page.locator("#main")).to_contain_text("Draft changing after search · version")
        page.reload()
        expect(page).to_have_url(published_route)
        expect(page.locator("#main")).to_contain_text("Draft changing after search · version")
        expect(page.locator("#main")).to_contain_text("published and read-only")
        expect(page.get_by_role("button", name="New session", exact=True)).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize("width,scheme", [(1440, "light"), (390, "dark")])
def test_rota_result_resets_restrictive_planner_filters_and_days(browser, live_server, h, width, scheme):
    w = h.shift(h.workspace("Exact filled clean target"))["workspace"]
    h.lifecycle(w, "validate")
    detail = h.detail(w)
    shift_id = detail["shifts"][0]["id"]
    validation = detail["workspace"]["validation"]
    assert not any(c["status"] == "gap" for c in validation["coverage"])
    assert not any(f["severity"] == "error" for f in validation["findings"])
    context, page = sign_in(browser, live_server, "coordinator", viewport={"width": width, "height": 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, "schedule/week/2026-10-04?edit=" + w["id"])
        target = page.locator(f'#schedule-view [data-rk*="{shift_id}"]').first
        expect(target).to_be_visible()
        labels = ("Only sessions with open places", "Only sessions that break a rule")
        for label in labels:
            planner_filter(page).get_by_role("checkbox", name=label, exact=True).check()
        close_popover(page)
        days = view_settings(page.locator(".planner-toolbar"), "Days")
        days.get_by_role("button", name="Sunday", exact=True).click()
        days.get_by_label("Week starts on", exact=True).select_option("0")
        days.get_by_label("From", exact=True).fill("15:00")
        days.get_by_label("To", exact=True).fill("16:00")
        days.get_by_label("To", exact=True).press("Tab")
        close_popover(page)
        expect(target).not_to_be_visible()
        search(page, w["name"]).get_by_role("option", name=re.compile("^" + re.escape(w["name"]))).click()
        expect(page).to_have_url(re.compile(r"#schedule/week/2026-10-04\?edit=" + w["id"] + "$"))
        expect(target).to_be_visible()
        for label in labels:
            expect(planner_filter(page).get_by_role("checkbox", name=label, exact=True)).not_to_be_checked()
        close_popover(page)
        days = view_settings(page.locator(".planner-toolbar"), "Days")
        expect(days.get_by_role("group", name="Weekdays shown").locator('[aria-pressed="true"]')).to_have_count(7)
        expect(days.get_by_label("Week starts on", exact=True)).to_have_value("")
        expect(days.get_by_role("button", name="Whole day", exact=True)).to_be_disabled()
        close_popover(page)
        page.reload()
        expect(page).to_have_url(re.compile(r"#schedule/week/2026-10-04\?edit=" + w["id"] + "$"))
        expect(target).to_be_visible()
    finally:
        context.close()

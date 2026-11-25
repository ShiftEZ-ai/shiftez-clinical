"""Authored real-browser governance journeys; no mocked transport or storage.

Fixtures create fictional initial identities/source duties using the real API.
Every consent/review transition under test is performed through the actual UI.
"""
from packages.domain.time import now as clock_now
from datetime import timedelta
import re
from pathlib import Path
from urllib.parse import urlsplit
import pytest
from playwright.sync_api import expect
from .governance_support import identity, swap_automation
from .test_browser import department_tab, browser, live_server, sign_in, navigate, save_dialog, decide, TEST_PASSWORD
from .test_browser import whole_list, route_fetch, wait_held, reconnect

pytestmark = pytest.mark.browser

AUTO_PUBLICATION_NOTICE = 'After the final independent review, this swap is published automatically.'


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}], ids=['desktop', 'phone'])
def test_inactive_staff_keeps_personal_pages_without_forbidden_swap_background_reads(browser, live_server, h, viewport):
    person = next(row for row in h.get('admin', '/api/catalogues')['people'] if row['id'] == h.seed['people'][0])
    h.command('admin', 'catalogue.bulk_update', {'catalogue': 'people',
        'items': [{'id': person['id'], 'revision': person['revision']}], 'active': False,
        'reason': 'Synthetic inactive staff profile'})
    assert h.get('staff', '/api/me')['staff_swap_access'] is False
    errors, swap_reads = {'cold': [], 'authenticated': []}, []
    phase = 'cold'
    context = browser.new_context(viewport=viewport, locale='en-GB', timezone_id='Asia/Riyadh')
    page = context.new_page()
    page.on('pageerror', lambda error: errors[phase].append(('pageerror', str(error))))
    page.on('console', lambda message: errors[phase].append(('console', message.text)) if message.type == 'error' else None)
    page.on('response', lambda response: errors[phase].append(('http', response.request.method, response.url, response.status)) if response.status >= 400 else None)
    page.on('request', lambda request: swap_reads.append(urlsplit(request.url).path) if urlsplit(request.url).path.startswith('/api/swaps') else None)
    try:
        # Cold boot probes the session once; observe all errors before and after sign-in.
        me_url = live_server + '/api/me'
        cold_console = 'Failed to load resource: the server responded with a status of 401 (Unauthorized)'
        with page.expect_response(lambda response: response.request.method == 'GET' and response.url == me_url) as probe, \
                page.expect_console_message(lambda message: message.type == 'error' and message.text == cold_console):
            page.goto(live_server)
            expect(page.get_by_label('Username or email', exact=True)).to_be_visible(timeout=20000)
        assert probe.value.status == 401
        assert probe.value.json()['error']['code'] == 'AUTH_REQUIRED'
        assert len(errors['cold']) == 2, errors['cold']
        assert errors['cold'].count(('http', 'GET', me_url, 401)) == 1
        assert errors['cold'].count(('console', cold_console)) == 1
        # Keep the listeners active for the earliest login/Overview/badge requests.
        phase = 'authenticated'
        page.get_by_label('Username or email', exact=True).fill('staff')
        page.get_by_role('button', name='Continue', exact=True).click()
        page.get_by_label('Password', exact=True).fill(TEST_PASSWORD)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home', timeout=20000)
        navigate(page,'home')
        expect(page.get_by_role('heading', name='Coming up', exact=True)).to_be_visible()
        expect(page.get_by_text('You have no duties coming up.', exact=True)).to_be_visible()
        navigate(page,'insights/workload')
        expect(page.get_by_role('heading', name='Nothing published yet', exact=True)).to_be_visible()
        # Without staff swap access there is no swap to offer: New request leaves Swap out, and nothing reads /api/swaps.
        navigate(page,'requests?type=swap')  # narrowed to swaps: the filtered-empty state, with its Swap chip to clear
        expect(page.get_by_role('heading', name='No matching requests', exact=True)).to_be_visible()
        page.reload()
        navigate(page,'requests?type=swap')
        page.get_by_role('button', name='New request', exact=True).click()
        expect(page.get_by_role('dialog', name='New request').get_by_role('radio', name=re.compile('^Leave'))).to_be_visible()
        expect(page.get_by_role('dialog', name='New request').get_by_role('radio', name=re.compile('^Swap'))).to_have_count(0)
        expect(page.get_by_role('dialog', name='New request').get_by_role('radio', name=re.compile('^Available for overtime'))).to_have_count(0)
        assert not swap_reads, swap_reads
        assert not errors['authenticated'], errors['authenticated']
    finally:
        context.close()


def test_configure_activate_and_complete_two_step_route_in_real_ui(browser, live_server, h):
    identity(h, "programme", ["programme_lead"])
    contexts = []
    errors = []
    try:
        ac, admin = sign_in(browser, live_server, "admin"); contexts.append(ac)
        admin.on("pageerror", lambda error: errors.append(str(error)))
        navigate(admin,"access/routes")
        admin.get_by_role("button", name="Propose review route", exact=True).click()
        form = admin.get_by_role("dialog")
        expect(form.get_by_text("Rota review routes cannot overlap each other. Swap review routes may overlap.", exact=False)).to_be_visible()
        form.get_by_label("Route name", exact=True).fill("Browser two-step route")
        form.get_by_label("Effective from", exact=True).fill("2026-10-01")
        form.get_by_label("Step label", exact=True).first.fill("Independent programme review")
        form.get_by_label("Required review role", exact=True).first.select_option("programme_lead")
        form.get_by_role("button", name="Add routine review step", exact=True).click()
        form.get_by_label("Step label", exact=True).nth(1).fill("Independent clinical review")
        form.get_by_label("Configuration reason", exact=True).fill("Synthetic independent browser acceptance")
        expect(form.get_by_label("Step key", exact=True)).to_have_count(0)  # an internal name, made from the label
        save_dialog(form, "Save route proposal")
        steps = next(r for r in h.get("admin", "/api/review-routes")["routes"] if r["name"] == "Browser two-step route")["definition"]["steps"]
        assert [s["key"] for s in steps] == ["routine_independent_programme_review", "routine_independent_clinical_review"]
        rc, reviewer = sign_in(browser, live_server, "approver"); contexts.append(rc)
        reviewer.on("pageerror", lambda error: errors.append(str(error)))
        navigate(reviewer,"access/routes")
        reviewer.get_by_role("button", name="Activate route", exact=True).click()
        form = reviewer.get_by_role("dialog")
        form.get_by_label("Reason", exact=True).fill("Independently activate synthetic route")
        save_dialog(form, "Confirm")
        w = h.shift(h.workspace("Browser route candidate"))["workspace"]
        w = h.lifecycle(w, "submit")["workspace"]
        pc, programme = sign_in(browser, live_server, "programme"); contexts.append(pc)
        programme.on("pageerror", lambda error: errors.append(str(error)))
        navigate(programme, "planner")
        programme.get_by_role("button", name="Approve rota", exact=True).click()
        form = programme.get_by_role("dialog", name="Approve rota", exact=True)
        expect(form).not_to_contain_text(AUTO_PUBLICATION_NOTICE)
        form.get_by_label("Note", exact=True).fill("Independent UI acceptance review")
        save_dialog(form, "Approve rota")
        assert h.detail(w)["workspace"]["status"] == "submitted"
        navigate(reviewer, "planner")
        reviewer.get_by_role("button", name="Approve rota", exact=True).click()
        form = reviewer.get_by_role("dialog", name="Approve rota", exact=True)
        expect(form).not_to_contain_text(AUTO_PUBLICATION_NOTICE)
        form.get_by_label("Note", exact=True).fill("Independent UI acceptance review")
        save_dialog(form, "Approve rota")
        assert h.detail(w)["workspace"]["status"] == "approved"
        cc, coordinator = sign_in(browser, live_server, "coordinator"); contexts.append(cc)
        navigate(coordinator, "planner")
        decide(coordinator, "Publish rota")
        assert h.detail(w)["workspace"]["status"] == "published"
        assert not errors
    finally:
        for context in contexts:
            context.close()


def test_staff_consent_to_independent_publication_in_real_ui(browser, live_server, h, tmp_path):
    # The manual path: a coordinator prepares and publishes (both automatic steps switched off).
    swap_automation(h, False, False)
    # Dynamic future dates avoid expiry tests depending on when qualification runs.
    day = clock_now().date() + timedelta(days=14)
    identity(h, "staff_two", ["staff"], h.seed["people"][1])
    w = h.workspace("Browser staff consent source", start=day.isoformat(), end=(day+timedelta(days=7)).isoformat())
    w = h.shift(w, day.isoformat()+"T08:00:00+03:00", day.isoformat()+"T12:00:00+03:00")["workspace"]
    next_day = (day+timedelta(days=1)).isoformat()
    w = h.shift(w, next_day+"T08:00:00+03:00", next_day+"T12:00:00+03:00", people=[h.seed["people"][1]])["workspace"]
    original = h.publish(w)["publication_id"]
    contexts = []
    errors = []
    try:
        sc, staff = sign_in(browser, live_server, "staff"); contexts.append(sc)
        staff.on("pageerror", lambda error: errors.append(str(error)))
        navigate(staff,"requests?type=swap")
        staff.get_by_role("button", name="New request", exact=True).click()
        form = staff.get_by_role("dialog")
        form.get_by_role("radio", name=re.compile("^Swap a duty")).check()
        form.get_by_label("Swap with", exact=True).select_option(h.seed["people"][1])
        form.get_by_label("Message to them", exact=True).fill("Fictional two-sided exchange")
        save_dialog(form, "Send offer")
        tc, recipient = sign_in(browser, live_server, "staff_two"); contexts.append(tc)
        recipient.on("pageerror", lambda error: errors.append(str(error)))
        navigate(recipient,"requests?type=swap")
        recipient.get_by_role("button", name="Offer a duty in return", exact=True).click()
        form = recipient.get_by_role("dialog")
        form.get_by_label("Message to them", exact=True).fill("I offer exactly my displayed duty")
        save_dialog(form, "Offer a duty in return")
        # An already-open proposer tab must discover a counteroffer, not require a reload.
        expect(staff.get_by_role("button", name="Confirm swap", exact=True)).to_be_visible(timeout=16000)
        decide(staff, "Confirm swap")
        row = h.get("staff", "/api/swaps")["swaps"][0]
        assert row["status"] == "accepted"
        assert h.get("staff", "/api/publications/"+original)["publication"]["current"]
        cc, coordinator = sign_in(browser, live_server, "coordinator"); contexts.append(cc)
        coordinator.on("pageerror", lambda error: errors.append(str(error)))
        navigate(coordinator,"requests?type=swap")
        coordinator.get_by_role("button", name="Prepare amendment", exact=True).click()
        form = coordinator.get_by_role("dialog")
        expect(form).to_contain_text("After the final approval a coordinator publishes it.")
        form.get_by_label("Preparation reason", exact=True).fill("Prepare only the exact consented pair")
        save_dialog(form, "Prepare draft amendment")
        expect(coordinator.locator("#main")).to_have_attribute("data-loaded-view", "schedule")
        decide(coordinator, "Submit for review")
        rc, approver = sign_in(browser, live_server, "approver"); contexts.append(rc)
        navigate(approver,"requests?type=swap")
        approver.get_by_role("button", name="Open rota", exact=True).click()
        expect(approver.locator("#main")).to_have_attribute("data-loaded-view", "schedule")
        approver.get_by_role("button", name="Approve changes", exact=True).click()
        form = approver.get_by_role("dialog", name="Approve changes", exact=True)
        expect(form).not_to_contain_text(AUTO_PUBLICATION_NOTICE)
        form.get_by_label("Note", exact=True).fill("Independent UI acceptance review")
        save_dialog(form, "Approve changes")
        assert h.detail(h.get("staff", "/api/swaps/"+row["id"])["swap"]["workspace_id"])["workspace"]["status"] == "approved"
        assert h.get("staff", "/api/publications/"+original)["publication"]["current"]
        coordinator.reload(); navigate(coordinator, "planner")
        decide(coordinator, "Publish rota")
        final = h.get("staff", "/api/swaps/"+row["id"])
        assert final["swap"]["status"] == "published"
        pid = final["swap"]["result_publication_id"]
        result = h.get("coordinator", "/api/publications/"+pid)["snapshot"]
        assert [s["participants"][0]["person_id"] for s in result["shifts"]] == [h.seed["people"][1], h.seed["people"][0]]
        staff.reload(); navigate(staff,"requests?type=swap")
        expect(staff.locator("#main tr.dt-open, #main .dt-card").filter(has_text="Swap").first).to_contain_text("Approved")
        staff.set_viewport_size({"width": 390, "height": 844})
        assert staff.evaluate("document.documentElement.scrollWidth<=window.innerWidth+2")
        staff.screenshot(path=str(tmp_path/"staff-consent-mobile.png"), full_page=True)
        assert not errors
    finally:
        for context in contexts:
            context.close()


def agreed_swap_pages(browser, live_server, h, contexts, errors, *, unrelated_gap=False):
    """Prepare the offered pair on a fresh rota; return the proposer page and original publication."""
    day = clock_now().date() + timedelta(days=14)
    identity(h, "staff_two", ["staff"], h.seed["people"][1])
    w = h.workspace("Browser automatic swap source", start=day.isoformat(), end=(day+timedelta(days=7)).isoformat())
    w = h.shift(w, day.isoformat()+"T08:00:00+03:00", day.isoformat()+"T12:00:00+03:00")["workspace"]
    next_day = (day+timedelta(days=1)).isoformat()
    w = h.shift(w, next_day+"T08:00:00+03:00", next_day+"T12:00:00+03:00", people=[h.seed["people"][1]])["workspace"]
    if unrelated_gap:
        typ = h.configure("assignment_types", {"name": "Unrelated advisory duty", "starts_on": "2020-01-01",
                                              "definition": {}})["record"]
        h.cover_target(2, {"assignment_type_id": typ["id"]})
        third_day = (day+timedelta(days=2)).isoformat()
        w = h.shift(w, third_day+"T08:00:00+03:00", third_day+"T12:00:00+03:00",
                    people=[h.seed["people"][2]], typ=typ["id"])["workspace"]
    original = h.publish(w)["publication_id"]
    options = h.get("staff", "/api/swaps/options")["assignments"]
    swap = h.command("staff", "swap.offer", {"publication_id": original, "assignment_id": options[0]["assignment_id"],
                     "recipient_person_id": h.seed["people"][1], "expires_at": (clock_now()+timedelta(days=1)).isoformat(),
                     "reason": "Automatic swap journey"})["swap"]
    second = h.get("staff_two", "/api/swaps/options?swap_id="+swap["id"])["assignments"][0]["assignment_id"]
    h.command("staff_two", "swap.counteroffer", {"assignment_id": second, "reason": "This one in return"}, swap["id"], swap["revision"])
    sc, staff = sign_in(browser, live_server, "staff"); contexts.append(sc)
    staff.on("pageerror", lambda error: errors.append(str(error)))
    navigate(staff, "requests?type=swap")
    return staff, swap["id"], original


def test_an_agreed_swap_prepares_and_publishes_itself_after_approval(browser, live_server, h):
    contexts, errors = [], []
    try:
        staff, swap_id, original = agreed_swap_pages(browser, live_server, h, contexts, errors)
        decide(staff, "Confirm swap")
        expect(staff.get_by_text("prepared and sent for independent approval", exact=False)).to_be_visible()
        row = h.get("staff", "/api/swaps/"+swap_id)["swap"]
        assert row["status"] == "reviewing" and row["workspace_id"]
        # A prepared swap is withdrawn or declined, not undone: the toast offers no Undo.
        expect(staff.locator("#toast-tray").get_by_role("button", name="Undo")).to_have_count(0)
        rc, approver = sign_in(browser, live_server, "approver"); contexts.append(rc)
        approver.on("pageerror", lambda error: errors.append(str(error)))
        navigate(approver, "requests?type=swap")
        approver.get_by_role("button", name="Open rota", exact=True).click()
        expect(approver.locator("#main")).to_have_attribute("data-loaded-view", "schedule")
        decide(approver, "Approve changes")
        expect(approver.locator(".toast").filter(has_text="Approved · the swap is published")).to_be_visible()
        assert h.get("staff", "/api/swaps/"+swap_id)["swap"]["status"] == "published"
        assert not h.get("staff", "/api/publications/"+original)["publication"]["current"]
        assert not errors
    finally:
        for context in contexts:
            context.close()


@pytest.mark.parametrize('scheme,viewport', [('light', {'width': 1440, 'height': 1000}),
                                           ('dark', {'width': 390, 'height': 844})])
def test_swap_approval_labels_scoped_checks_and_preserves_unrelated_publication_gap(browser, live_server, h, scheme, viewport):
    contexts, errors = [], []
    try:
        staff, swap_id, original = agreed_swap_pages(browser, live_server, h, contexts, errors, unrelated_gap=True)
        source = h.get("coordinator", "/api/publications/"+original)
        source_shifts = source["snapshot"]["shifts"]
        source_unrelated = source_shifts[2]["id"]
        original_gaps = [f for f in source["original_validation"]["findings"]
                         if f["code"] == "SCOPED_COVERAGE_GAP" and f["shift_id"] == source_unrelated]
        assert original_gaps and all(f["severity"] == "warning" for f in original_gaps)
        decide(staff, "Confirm swap")
        row = h.get("staff", "/api/swaps/"+swap_id)["swap"]
        assert row["status"] == "reviewing" and row["workspace_id"]
        amendment = h.detail(row["workspace_id"])
        submitted = amendment["workspace"]["validation"]
        unrelated = next(s["id"] for s in amendment["shifts"]
                         if any(p["person_id"] == h.seed["people"][2] for p in s["participants"]))
        # D11: a swap is a published change: only the changed sessions are rechecked, and the untouched session keeps its
        # issued check, so its advisory gap is carried forward as it was (a warning), never blocking the swap.
        assert submitted["scope"] == "published_changes"
        assert submitted["error_count"] == 0 and submitted["warning_count"] == 1
        carried = [f for f in submitted["findings"] if f.get("shift_id") == unrelated]
        assert carried and all(f["code"] == "SCOPED_COVERAGE_GAP" and f["severity"] == "warning" for f in carried)
        assert not any(f["severity"] == "error" for f in submitted["findings"])
        rc, approver = sign_in(browser, live_server, "approver", viewport=viewport); contexts.append(rc)
        approver.emulate_media(color_scheme=scheme)
        approver.on("pageerror", lambda error: errors.append(str(error)))
        navigate(approver, "requests?type=swap")
        approver.get_by_role("button", name="Open rota", exact=True).click()
        expect(approver.locator("#main")).to_have_attribute("data-loaded-view", "schedule")
        approver.get_by_role("button", name="Approve changes", exact=True).click()
        form = approver.get_by_role("dialog", name="Approve changes", exact=True)
        expect(form.get_by_text(AUTO_PUBLICATION_NOTICE, exact=True)).to_be_visible()
        summary = form.locator(".approve-summary .muted")
        expect(summary).to_have_text("Only the changes rechecked")
        # ME-27: counts come from the rota's Fix list (causes), as Publish and the footer say them.
        fix = h.get("approver", f"/api/workspaces/{row['workspace_id']}/fix-list")["summary"]
        expect(form.locator(".fix-gate")).to_contain_text(f"{fix['could']} could improve" if fix["could"] else "Nothing new since it was submitted")
        form.get_by_label("Note", exact=True).fill("Review the swap; retain the unrelated advisory gap")
        save_dialog(form, "Approve changes")
        final = h.get("staff", "/api/swaps/"+swap_id)["swap"]
        assert final["status"] == "published"
        issued = h.get("coordinator", "/api/publications/"+final["result_publication_id"])
        shifts, check = issued["snapshot"]["shifts"], issued["original_validation"]
        assert len(shifts) == 3
        assert [s["participants"][0]["person_id"] for s in shifts] == [
            h.seed["people"][1], h.seed["people"][0], h.seed["people"][2]]
        assert check["scope"] == "published_changes" and "score" not in check
        assert {r["shift_id"] for r in check["coverage"]} == {s["id"] for s in shifts}
        gaps = [f for f in check["findings"] if f["code"] == "SCOPED_COVERAGE_GAP" and f["shift_id"] == unrelated]
        assert gaps and all(f["severity"] == "warning" for f in gaps)
        assert h.get("coordinator", "/api/publications/"+original)["snapshot"] == source["snapshot"]
        assert not errors
    finally:
        for context in contexts:
            context.close()


def test_swap_publication_notice_waits_for_all_steps_and_quorum_in_real_ui(browser, live_server, h):
    from .test_swap_approval_settings import settings, swap_route
    dedicated = swap_route(h, [
        {"key": "clinical", "label": "Clinical", "role": "approver"},
        {"key": "programme", "label": "Programme", "role": "programme_lead", "approvals_required": 2}])
    settings(h, swap_review_route_id=dedicated["id"])
    identity(h, "lead_one", ["programme_lead"])
    identity(h, "lead_two", ["programme_lead"])
    contexts, errors = [], []
    try:
        staff, swap_id, original = agreed_swap_pages(browser, live_server, h, contexts, errors)
        source = h.get("coordinator", "/api/publications/"+original)["snapshot"]
        decide(staff, "Confirm swap")
        row = h.get("staff", "/api/swaps/"+swap_id)["swap"]
        assert row["status"] == "reviewing" and row["workspace_id"]
        workspace_id = row["workspace_id"]
        for who, step, seats, final in [("approver", "clinical", 0, False),
                                        ("lead_one", "programme", 0, False),
                                        ("lead_two", "programme", 1, True)]:
            progress = h.detail(workspace_id)["review_progress"]
            assert progress["pending_step"]["key"] == step and progress["pending_step"]["approved_count"] == seats
            rc, reviewer = sign_in(browser, live_server, who); contexts.append(rc)
            reviewer.on("pageerror", lambda error: errors.append(str(error)))
            # Programme reviewers open the exact rota; they have no access to swap Requests.
            navigate(reviewer, "planner/"+workspace_id)
            reviewer.get_by_role("button", name="Approve changes", exact=True).click()
            form = reviewer.get_by_role("dialog", name="Approve changes", exact=True)
            expect(form.get_by_text(AUTO_PUBLICATION_NOTICE, exact=True)).to_be_visible()
            if step == "clinical":
                expect(form).to_contain_text("1 more review step follows before it can be published.")
            form.get_by_label("Note", exact=True).fill("Independent required review seat")
            save_dialog(form, "Approve changes")
            current = h.get("staff", "/api/swaps/"+swap_id)["swap"]
            assert h.detail(workspace_id)["workspace"]["status"] == ("published" if final else "submitted")
            assert current["status"] == ("published" if final else "reviewing")
            assert bool(current["result_publication_id"]) is final
            assert h.get("staff", "/api/publications/"+original)["publication"]["current"] is (not final)
        assert h.get("coordinator", "/api/publications/"+original)["snapshot"] == source
        assert not errors
    finally:
        for context in contexts:
            context.close()


def test_department_settings_save_the_swap_automation_and_reload_it(browser, live_server, h):
    context, page = sign_in(browser, live_server, "admin")
    try:
        navigate(page, "configuration/department")
        department_tab(page, "Swaps")
        pane = page.locator(".config-pane")
        prepare = pane.get_by_role("checkbox", name="Prepare automatically when both agree", exact=True)
        publish = pane.get_by_role("checkbox", name="Publish automatically after final approval", exact=True)
        expect(prepare).to_be_checked()
        expect(publish).to_be_checked()
        expect(pane.get_by_label("Who approves swaps", exact=True)).to_have_value("")
        publish.uncheck()
        bar = pane.get_by_role("region", name="Unsaved changes")
        bar.get_by_role("button", name="Save settings", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Reason", exact=True).fill("Coordinators publish swaps")
        dialog.get_by_role("button", name="Save settings", exact=True).click()
        expect(page.get_by_role("button", name="Undo", exact=True)).to_be_visible()
        assert h.get("admin", "/api/me")["department"]["swap_auto_publish"] is False
        page.reload()
        department_tab(page, "Swaps")
        expect(page.locator(".config-pane").get_by_role("checkbox", name="Publish automatically after final approval", exact=True)).not_to_be_checked()
        expect(page.locator(".config-pane").get_by_role("checkbox", name="Prepare automatically when both agree", exact=True)).to_be_checked()
    finally:
        context.close()


def test_a_failed_route_list_keeps_the_configured_swap_route_on_an_unrelated_save(browser, live_server, h):
    from .test_swap_approval_settings import settings, swap_route
    dedicated = swap_route(h)
    settings(h, swap_review_route_id=dedicated["id"])
    context, page = sign_in(browser, live_server, "admin")
    try:
        page.route("**/api/review-routes**", lambda route: route.fulfill(status=503, json={"detail": "Unavailable"}))
        navigate(page, "configuration/department")
        department_tab(page, "Swaps")
        pane = page.locator(".config-pane")
        picker = pane.get_by_label("Who approves swaps", exact=True)
        expect(picker).to_be_disabled()
        expect(picker).to_have_value(dedicated["id"])
        expect(pane.get_by_text("Couldn’t load the review routes.", exact=False)).to_be_visible()
        department_tab(page, "General")
        pane.get_by_label("Department name", exact=True).fill("Renamed department")
        pane.get_by_role("region", name="Unsaved changes").get_by_role("button", name="Save settings", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Reason", exact=True).fill("New department name")
        dialog.get_by_role("button", name="Save settings", exact=True).click()
        expect(page.get_by_role("button", name="Undo", exact=True)).to_be_visible()
        department = h.get("admin", "/api/me")["department"]
        assert department["name"] == "Renamed department"
        assert department["swap_review_route_id"] == dedicated["id"]
    finally:
        context.close()


def test_a_retired_swap_route_stays_while_unrelated_settings_save(browser, live_server, h):
    from apps.api import models as m
    from apps.api.db import transaction
    from .test_swap_approval_settings import swap_route
    dedicated = swap_route(h)
    h.command("approver", "review_route.retire", {"reason": "Retire the swap route"}, dedicated["id"], dedicated["revision"])
    # Preserve a historical retired selection; current commands refuse retiring the selected route.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).swap_review_route_id = dedicated["id"]
    context, page = sign_in(browser, live_server, "admin")
    try:
        navigate(page, "configuration/department")
        department_tab(page, "Swaps")
        pane = page.locator(".config-pane")
        picker = pane.get_by_label("Who approves swaps", exact=True)
        expect(picker).to_have_value(dedicated["id"])
        expect(picker.locator("option:checked")).to_contain_text("no longer active")
        department_tab(page, "General")
        pane.get_by_label("Department name", exact=True).fill("Renamed department")
        pane.get_by_role("region", name="Unsaved changes").get_by_role("button", name="Save settings", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Reason", exact=True).fill("New department name")
        dialog.get_by_role("button", name="Save settings", exact=True).click()
        expect(page.get_by_role("button", name="Undo", exact=True)).to_be_visible()
        department = h.get("admin", "/api/me")["department"]
        assert department["name"] == "Renamed department"
        assert department["swap_review_route_id"] == dedicated["id"]
    finally:
        context.close()


def test_a_manually_prepared_swap_publishes_itself_when_only_publication_is_automatic(browser, live_server, h):
    swap_automation(h, False, True)
    contexts, errors = [], []
    try:
        staff, swap_id, original = agreed_swap_pages(browser, live_server, h, contexts, errors)
        decide(staff, "Confirm swap")
        expect(staff.get_by_text("A coordinator prepares the rota change next.", exact=False)).to_be_visible()
        assert h.get("staff", "/api/swaps/"+swap_id)["swap"]["status"] == "accepted"
        cc, coordinator = sign_in(browser, live_server, "coordinator"); contexts.append(cc)
        coordinator.on("pageerror", lambda error: errors.append(str(error)))
        navigate(coordinator, "requests?type=swap")
        coordinator.get_by_role("button", name="Prepare amendment", exact=True).click()
        form = coordinator.get_by_role("dialog")
        expect(form).to_contain_text("published duties stay as they are")
        expect(form).to_contain_text("After the final approval the swap is published automatically.")
        form.get_by_label("Preparation reason", exact=True).fill("Prepare the consented pair")
        save_dialog(form, "Prepare draft amendment")
        expect(coordinator.locator("#main")).to_have_attribute("data-loaded-view", "schedule")
        decide(coordinator, "Submit for review")
        rc, approver = sign_in(browser, live_server, "approver"); contexts.append(rc)
        navigate(approver, "requests?type=swap")
        approver.get_by_role("button", name="Open rota", exact=True).click()
        main = approver.locator("#main")
        expect(main).to_have_attribute("data-loaded-view", "schedule")
        state = approver.locator(".planner-bar .rota-status")
        expect(state).to_have_text("Amendment · Waiting for approval")
        approver.get_by_role("button", name="Approve changes", exact=True).click()
        dialog = approver.get_by_role("dialog", name="Approve changes", exact=True)
        dialog.get_by_label("Note", exact=True).fill("Independent UI acceptance review")
        held = []
        approver.route("**/api/workspaces?*", lambda route: held.append(route))
        try:
            with approver.expect_request(lambda request: "/api/workspaces?" in request.url):
                dialog.get_by_role("button", name="Approve changes", exact=True).click()
                armed = dialog.locator("button.armed")
                if armed.count():
                    armed.click()
            expect(state).to_have_text("Amendment · Published")
            expect(approver.locator(".planner-bar .rota-decisions")).to_be_empty()
        finally:
            for route in held:
                route.continue_()
            approver.unroute("**/api/workspaces?*")

        expect(dialog).to_have_count(0)
        expect(main).to_have_attribute("data-loaded-view", "schedule")
        expect(main.locator("#schedule-view")).to_be_visible()
        expect(approver.locator(".planner-bar").get_by_role("heading", name="Schedule", exact=True)).to_be_visible()
        expect(state).to_have_count(0)
        expect(approver.get_by_role("button", name="Approve changes", exact=True)).to_have_count(0)
        expect(main.get_by_text(re.compile(r"Couldn.t load (the schedule|the published sessions)"))).to_have_count(0)
        expect(main.get_by_text("This rota has changed. Search again", exact=False)).to_have_count(0)
        expect(approver).to_have_url(re.compile(r"[?&]edit=none(?:&|$)"))
        expect(approver.locator(".toast").filter(has_text="Approved · the swap is published")).to_be_visible()
        assert h.get("staff", "/api/swaps/"+swap_id)["swap"]["status"] == "published"
        assert not h.get("staff", "/api/publications/"+original)["publication"]["current"]
        assert not errors
    finally:
        for context in contexts:
            context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_review_routes_read_as_dates_and_roles_and_say_what_activation_did(browser, live_server, h, width, scheme):
    """F13 A-12/A-15/A-17/A-18: the built-in approval is "In use" (one status palette); a route shows dates and role
    names, not ISO dates, revisions or codes; activating it says what happens next; no permanent retire note."""
    context, page = sign_in(browser, live_server, 'approver', viewport={'width': width, 'height': 900}, color_scheme=scheme)
    try:
        navigate(page, 'access/routes')
        expect(page.locator('.route-default .badge')).to_have_text('In use')
        expect(page.locator('#main')).not_to_contain_text('Before retiring a review route')
        h.command('admin', 'review_route.create', {'name': 'Readable route', 'purpose': 'rota', 'starts_on': '2026-10-01',
            'ends_on': '2026-12-31', 'definition': {'schema_version': 1, 'steps': [{'key': 'clinical_review',
            'label': 'Independent clinical review', 'role': 'approver', 'escalation_role': 'programme_lead'}],
            'urgent_steps': None}, 'reason': 'Readable route browser check'})
        page.reload()
        panel = page.locator('section.panel').filter(has_text='Readable route')
        expect(panel).to_contain_text('1 Oct 2026')
        expect(panel).to_contain_text('30 Dec 2026')  # the last included day (the end is exclusive)
        expect(panel).not_to_contain_text('2026-10-01')
        expect(panel).not_to_contain_text('version')
        expect(panel).not_to_contain_text('programme_lead')
        expect(panel.locator('td').filter(has_text=re.compile('^Programme lead$'))).to_have_count(1)
        panel.get_by_role('button', name='Activate route', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Reason', exact=True).fill('Independently activate the readable route')
        save_dialog(form, 'Confirm')
        expect(page.locator('.toast', has_text='Readable route is active. Rotas submitted from now use it.')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        context.close()


def test_a_route_keeps_at_least_one_review_step(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'access/routes')
        page.get_by_role('button', name='Propose review route', exact=True).click()
        form = page.get_by_role('dialog')
        routine = form.locator('.form-section').filter(has=page.get_by_role('heading', name='Routine review steps'))
        remove = routine.get_by_role('button', name='Remove review step', exact=True)
        expect(remove).to_have_count(1)
        expect(remove).to_be_disabled()
        expect(remove).to_have_attribute('data-tip', 'A route needs at least one step')
        routine.get_by_role('button', name='Add routine review step', exact=True).click()
        expect(remove).to_have_count(2)
        expect(remove.first).to_be_enabled()
        remove.first.click()
        expect(remove).to_have_count(1)
        expect(remove).to_be_disabled()
    finally:
        context.close()


@pytest.mark.parametrize('scheme,viewport', [('light', {'width': 1440, 'height': 1000}),
                                           ('dark', {'width': 390, 'height': 844})])
def test_approve_dialog_names_rule_adjustments(browser, live_server, h, scheme, viewport):
    """SR-18 / SR-32: a rule changed for this rota only is named where the approver decides (dialog and review strip)."""
    rest = h.policy({'kind': 'max_hours', 'hours': 12, 'window_days': 7})
    w = h.shift(h.workspace('Adjusted for approval'))['workspace']
    w = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': rest['id'], 'mode': 'set', 'amount': 20,
                  'reason': 'Workload agreed for this rota'}, w['id'], w['revision'])['workspace']
    w = h.lifecycle(w, 'submit')['workspace']
    context, page = sign_in(browser, live_server, 'approver', viewport=viewport, color_scheme=scheme)
    try:
        navigate(page, 'planner/' + w['id'])
        strip = page.locator('.review-strip')
        expect(strip).to_contain_text('1 rule change for this rota')
        expect(strip).to_contain_text(rest['name'] + ' · set to 20')
        page.get_by_role('button', name='Approve rota', exact=True).click()
        form = page.get_by_role('dialog', name='Approve rota', exact=True)
        # D16/R5: each rota adjustment is an item the approver decides, with its justification quoted.
        changes = form.get_by_role('region', name='Changes for this rota')
        expect(changes.locator('li[data-review-item]')).to_have_count(1)
        expect(changes).to_contain_text(rest['name'])
        expect(changes).to_contain_text('Set to 20 for this rota')
        expect(changes).to_contain_text('“Workload agreed for this rota”')
        expect(form.get_by_text('Self-approved', exact=True)).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('scheme,viewport', [('light', {'width': 1440, 'height': 1000}),
                                           ('dark', {'width': 390, 'height': 844})])
def test_self_approval_is_marked_in_review_strip(browser, live_server, h, scheme, viewport):
    """LIVE-26: approving your own rota under a self-approval permission is said in the dialog and on the review step."""
    identity(h, 'solo', ['coordinator', 'approver'])
    h.command('admin', 'self_approval.grant', {'identity_id': h.seed['identities']['solo'], 'actions': ['rota.approve'],
              'starts_on': '2026-10-01', 'reason': 'Only one approver this quarter'})  # D4: in force, ends a year later
    own = h.command('solo', 'workspace.create', {'name': 'Solo built rota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'})['workspace']
    own = h.shift(own)['workspace']
    own = h.command('solo', 'workspace.submit', {'reason': 'Ready'}, own['id'], own['revision'])['workspace']
    context, page = sign_in(browser, live_server, 'solo', viewport=viewport, color_scheme=scheme)
    try:
        navigate(page, 'planner/' + own['id'])
        strip = page.locator('.review-strip')
        expect(strip).to_be_visible()
        expect(strip.get_by_text('Self-approved', exact=True)).to_have_count(0)
        page.get_by_role('button', name='Approve rota', exact=True).click()
        form = page.get_by_role('dialog', name='Approve rota', exact=True)
        expect(form.locator('.approve-summary').get_by_text('Self-approved', exact=True)).to_be_visible()
        form.get_by_label('Reason', exact=True).fill('Checked the rest gaps myself')
        save_dialog(form, 'Approve rota')
        assert h.detail(own)['workspace']['status'] == 'approved'
        expect(page.locator('.review-strip').get_by_text('Self-approved', exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('broken', [False, True], ids=['opens', 'fails-visibly'])
def test_approve_rota_is_never_silent_for_a_sole_approver(browser, live_server, h, broken):
    """A sole approver under a self-approval permission pressed Approve rota on their own submitted rota and nothing
    happened: a failure while the dialog was built was an unhandled rejection. Every press now ends in the dialog or a
    visible message, never a page error and never an orphaned closed dialog."""
    identity(h, 'sole', ['coordinator', 'approver'])
    h.command('admin', 'self_approval.grant', {'identity_id': h.seed['identities']['sole'], 'actions': ['rota.approve'],
              'starts_on': '2026-10-01', 'reason': 'Only one approver this quarter'})
    own = h.command('sole', 'workspace.create', {'name': 'Sole built rota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'})['workspace']
    own = h.shift(own)['workspace']
    own = h.command('sole', 'workspace.submit', {'reason': 'Ready'}, own['id'], own['revision'])['workspace']
    context, page = sign_in(browser, live_server, 'sole')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        navigate(page, 'planner/' + own['id'])
        approve = page.get_by_role('button', name='Approve rota', exact=True)
        expect(approve).to_be_enabled()
        expect(approve).not_to_have_attribute('aria-disabled', 'true')
        if broken:  # the press's Fix list read answers without its summary: the dialog's build throws
            def no_summary(route):
                body = route_fetch(route).json()
                body.pop('summary', None)
                route.fulfill(json=body)
            page.route(f"**/api/workspaces/{own['id']}/fix-list?*", no_summary, times=1)
        approve.click()
        if broken:
            expect(page.locator('#toast-tray .toast.error')).to_be_visible()
            expect(page.locator('dialog.dialog')).to_have_count(0)  # no closed, orphaned dialog left behind
        else:
            expect(page.locator('dialog[open]').or_(page.locator('#toast-tray .toast')).first).to_be_visible()
            expect(page.get_by_role('dialog', name='Approve rota', exact=True)).to_be_visible()
        assert errors == []
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def held_review(page):
    """The review's reads held (its overlaps read is the review's alone), each overlaps request counted, page errors kept."""
    held, overlaps, errors = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda r: overlaps.append(r.url) if urlsplit(r.url).path == '/api/schedule/overlaps' else None)
    page.route('**/api/schedule/overlaps?*', lambda route: held.append(route))
    return held, overlaps, errors


def release(page, held):
    for route in held:  # released once each, before unroute (unroute continues whatever it still holds)
        route.continue_()
    page.unroute('**/api/schedule/overlaps?*')


def test_final_approval_updates_the_schedule_header_before_the_detail_read(browser, live_server, h):
    name = "Rota ready to publish"
    w = h.lifecycle(h.shift(h.workspace(name))["workspace"], "submit")["workspace"]
    context, page = sign_in(browser, live_server, "approver", viewport={"width": 1440, "height": 900})
    try:
        navigate(page, "planner/" + w["id"])
        bar = page.locator(".planner-bar")
        status = bar.locator(".rota-status")
        approve = bar.get_by_role("button", name="Approve rota", exact=True)
        publish = bar.get_by_role("button", name="Publish…", exact=True)
        expect(status).to_have_text("Waiting for approval")
        expect(publish).to_have_count(0)
        approve.click()
        dialog = page.get_by_role("dialog", name="Approve rota", exact=True)
        expect(dialog).to_be_visible()
        detail = f"**/api/workspaces/{w['id']}?*"
        held = []
        page.route(detail, lambda route: held.append(route))
        try:
            dialog.get_by_role("button", name="Approve rota", exact=True).click()
            wait_held(page, held)
            expect(status).to_have_text("Approved")
            expect(approve).to_have_count(0)
            expect(dialog).to_have_count(0)
            expect(page.locator(".toast").filter(has_text="Approved · ready to publish")).to_be_visible()
            expect(publish).to_be_enabled()
            publish.click()
            publication = page.get_by_role("dialog", name=f"Publish {name}", exact=True)
            expect(publication).to_be_visible()
        finally:
            for route in held:
                route.continue_()
            page.unroute(detail)

        # The button opened before the detail read resumed. Its dialog must publish the approved revision.
        save_dialog(publication, "Publish")
        assert h.detail(w)["workspace"]["status"] == "published"
    finally:
        context.close()


def test_approve_rota_opened_from_home_is_held_and_a_press_joins_it(browser, live_server, h):
    """#1041: while Home's "Rota to review" (?approve=) reads the review, Approve rota is held and says it is busy; a
    press joins that opening (one read, one dialog), never a second, stacked Approve dialog."""
    w = h.lifecycle(h.shift(h.workspace('Held review rota'))['workspace'], 'submit')['workspace']
    context, page = sign_in(browser, live_server, 'approver')
    try:
        held, overlaps, errors = held_review(page)
        navigate(page, 'schedule?approve=' + w['id'])
        wait_held(page, held)
        approve = page.get_by_role('button', name='Approving rota…', exact=True)
        expect(approve).to_have_attribute('aria-disabled', 'true')
        expect(approve).to_have_attribute('aria-busy', 'true')
        approve.focus()  # aria-disabled keeps it focusable; pressing it says why (Playwright will not click it)
        page.keyboard.press('Enter')
        expect(page.locator('#toast-tray .toast').filter(has_text='Approving rota…')).to_be_visible()
        assert len(overlaps) == 1  # the press started no second review
        release(page, held)
        expect(page.get_by_role('dialog', name='Approve rota', exact=True)).to_be_visible()
        expect(page.locator('dialog[open]')).to_have_count(1)
        assert len(overlaps) == 1
        page.keyboard.press('Escape')
        expect(page.get_by_role('button', name='Approve rota', exact=True)).not_to_have_attribute('aria-disabled', 'true')
        assert errors == []
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_approve_rota_pressed_stays_held_when_the_page_redraws_during_its_review(browser, live_server, h):
    """#1041: a redraw while a press reads the review (the pulse refreshing the rota) draws Approve rota held and busy,
    never a fresh enabled button that looks dead; the press still ends in one dialog."""
    w = h.lifecycle(h.shift(h.workspace('Redrawn review rota'))['workspace'], 'submit')['workspace']
    other = h.workspace('Another rota')
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'planner/' + w['id'])
        approve = page.get_by_role('button', name='Approve rota', exact=True)
        expect(approve).to_be_enabled()
        held, overlaps, errors = held_review(page)
        approve.click()
        wait_held(page, held)
        page.locator('button.keep-label').filter(has_text=re.compile('^Approv')).evaluate("b => { b.dataset.before = '1'; }")
        h.shift(other)  # the pulse moves
        reconnect(page)  # and the page refreshes in the background
        expect(page.locator('button[data-before]')).to_have_count(0)  # redrawn
        redrawn = page.get_by_role('button', name='Approving rota…', exact=True)
        expect(redrawn).to_have_attribute('aria-disabled', 'true')
        expect(redrawn).to_have_attribute('aria-busy', 'true')
        release(page, held)
        expect(page.get_by_role('dialog', name='Approve rota', exact=True)).to_be_visible()
        expect(page.locator('dialog[open]')).to_have_count(1)
        assert len(overlaps) == 1
        assert errors == []
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('scheme,viewport', [('light', {'width': 1440, 'height': 1000}),
                                           ('dark', {'width': 390, 'height': 844})])
def test_approve_dialog_shows_the_same_summary_as_submit(browser, live_server, h, scheme, viewport):
    """SR-32 / A5–A6: the approver decides on the summary the coordinator submitted with (one component): the same tiles
    and the Fix list's gate, without the coordinator's Open fix list."""
    h.cover_target(2, {'assignment_type_id': h.seed['type']})
    w = h.shift(h.workspace('Same summary rota'))['workspace']  # one of two wished-for people: a warning, not a block
    h.lifecycle(w, 'validate')
    contexts = []
    try:
        cc, coordinator = sign_in(browser, live_server, 'coordinator'); contexts.append(cc)
        navigate(coordinator, f"schedule/week/2026-10-04?edit={w['id']}")
        coordinator.get_by_role('button', name='Submit for review…', exact=True).click()
        submit = coordinator.get_by_role('dialog', name='Submit Same summary rota for review', exact=True)
        tiles = submit.locator('.publish-summary')
        expect(tiles).to_contain_text('Places filled')  # A5: the Fix list's tiles
        submitted_tiles = tiles.text_content()
        coordinator.keyboard.press('Escape')
        expect(submit).to_have_count(0)
        h.lifecycle(h.detail(w)['workspace'], 'submit')
        ac, approver = sign_in(browser, live_server, 'approver', viewport=viewport, color_scheme=scheme); contexts.append(ac)
        navigate(approver, 'planner/' + w['id'])
        approver.get_by_role('button', name='Approve rota', exact=True).click()
        form = approver.get_by_role('dialog', name='Approve rota', exact=True)
        expect(form.locator('.publish-summary')).to_have_text(submitted_tiles)
        expect(form.locator('.fix-gate')).to_contain_text('Nothing new since it was submitted')
        expect(form.get_by_role('button', name='Open Fix list', exact=True)).to_have_count(0)
    finally:
        for context in contexts:
            context.close()


def test_no_internal_vocabulary(browser, live_server, h):
    """maintenance: Link rota names each rota by its dates and state in words (no ISO date, no raw status word), and the
    approval dialog speaks of the rota, never of a "candidate" or "input versions"."""
    request = h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'meeting',
        'start': '2026-10-04T09:00:00+03:00', 'end': '2026-10-04T10:00:00+03:00', 'private_reason': 'Fictional meeting'})['record']
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Meeting reviewed'}, request['id'], request['revision'])
    h.shift(h.workspace())
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        page.get_by_role('button', name=re.compile('^More actions for Meeting')).first.click()
        page.get_by_role('menuitem', name='Link rota', exact=True).click()
        related = page.get_by_label('Related rota', exact=True)
        expect(related).to_be_visible()  # the dialog opens after its rota list is read; all_text_contents() does not wait
        labels = related.locator('option').all_text_contents()
        assert labels and not [x for x in labels if re.search(r'\d{4}-\d{2}-\d{2}| · draft$', x)], labels
        assert any(' · Draft' in x and '4 Oct 2026' in x for x in labels), labels
    finally:
        context.close()
    source = (Path(__file__).resolve().parents[1] / 'apps/web/src/governance.ts').read_text(encoding='utf-8')
    assert 'candidate and input versions' not in source and 'My own active review authority' not in source

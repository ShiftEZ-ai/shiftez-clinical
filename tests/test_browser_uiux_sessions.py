"""Account changes keep private UI data isolated; export failures explain recovery."""
import re
import pytest
from playwright.sync_api import expect
from .conftest import TEST_PASSWORD
from .test_browser import route_fetch, browser, live_server, sign_in, navigate, pick_view  # noqa: F401

pytestmark = pytest.mark.browser


def switch_account(page, role):
    """Use sign-out/sign-in without reloading the document or replacing its context."""
    page.get_by_role("button", name=re.compile(r"^Account menu, ")).click()
    page.get_by_role("menuitem", name="Sign out", exact=True).click()
    expect(page.get_by_label("Username or email", exact=True)).to_be_visible()
    page.get_by_label("Username or email", exact=True).fill(role)
    page.get_by_role("button", name="Continue", exact=True).click()
    page.get_by_label("Password", exact=True).fill(TEST_PASSWORD)
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("button", name=re.compile(r"^Account menu, "))).to_be_visible()
    page.evaluate("location.hash = 'overview'")
    expect(page.locator("#main")).to_have_attribute("data-loaded-view", "home", timeout=20000)


def test_people_leave_does_not_follow_a_coordinator_into_an_auditor_session(browser, live_server, h):
    h.shift(h.workspace("Private leave cache journey"))
    h.command("coordinator", "availability.record", {
        "person_id": h.seed["people"][0], "start": "2026-10-04T08:00:00+03:00",
        "end": "2026-10-04T12:00:00+03:00", "kind": "actual_absence",
        "private_reason": "Fictional private reason",
    })
    typ = h.configure('leave_types', {'name': 'Assignable leave', 'short_code': 'ASGN',
                                     'counts_as_unavailable': False})['record']
    for person_id, type_id in ((h.seed['people'][1], typ['id']), (h.seed['people'][2], None)):
        row = h.command('coordinator', 'availability.record', {
            'person_id': person_id, 'start': '2026-10-04T08:00:00+03:00',
            'end': '2026-10-04T12:00:00+03:00', 'kind': 'actual_absence', 'leave_type_id': type_id, 'private_reason': 'Synthetic absence reason',
        })['record']
        if type_id is None:
            h.command('coordinator', 'availability.retract', {'reason': 'Synthetic retraction'}, row['id'], row['revision'])
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", re.compile(".+"), timeout=20000)
        navigate(page, "planner")
        pick_view(page, "People")
        expect(page.locator(".pg-cell.leave")).to_have_count(1)
        pick_view(page, "Week")
        pick_view(page, "People")
        expect(page.locator(".pg-cell.leave")).to_have_count(1)
        page.evaluate("window.sameDocument = true")
        switch_account(page, "auditor")
        navigate(page, "planner")
        pick_view(page, "People")
        expect(page.get_by_role("region", name="People view", exact=True)).to_be_visible()
        assert page.evaluate("window.sameDocument")
        expect(page.locator(".pg-cell.leave")).to_have_count(0)
        expect(page.get_by_text("Fictional private reason", exact=True)).to_have_count(0)
    finally:
        context.close()


def test_staff_command_menu_never_inherits_cached_draft_rotas(browser, live_server, h):
    h.shift(h.workspace("Private draft name for coordinator"))
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", re.compile(".+"), timeout=20000)
        navigate(page, "planner")
        # The open menu shows the rota list as soon as it arrives, which also fills the app's cached copy.
        page.keyboard.press("Control+k")
        palette = page.get_by_role("dialog", name="Command menu")
        palette.get_by_role("combobox").fill("Private draft name")
        expect(palette.get_by_role("option").first).to_be_visible()
        page.mouse.click(5, 5)
        expect(palette).to_have_count(0)
        switch_account(page, "staff")
        page.keyboard.press("Control+k")
        palette.get_by_role("combobox").fill("Private draft name")
        expect(palette.get_by_role("option")).to_have_count(0)
        expect(palette.get_by_text("No matches. Try a page name, a person or an action.", exact=True)).to_be_visible()
    finally:
        context.close()


def test_a_prior_accounts_delayed_api_reply_is_discarded(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator")
    held = []
    try:
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", re.compile(".+"), timeout=20000)
        # Keep the real authenticated reply in transit; its content is never mocked.
        page.route("**/api/catalogues", lambda route: held.append((route, route_fetch(route))))
        page.evaluate("""() => {
          window.delayedReply = null;
          window.importBundle('api.js').then(api => api.get('/api/catalogues'))
            .then(() => window.delayedReply = 'accepted', e => window.delayedReply = e.code);
        }""")
        # Polled, not a fixed wait: on a loaded runner the request can take longer than any fixed pause to be held.
        for _ in range(200):
            if held:
                break
            page.wait_for_timeout(50)
        assert len(held) == 1
        switch_account(page, "staff")
        route, response = held[0]
        route.fulfill(response=response)
        page.wait_for_function("() => window.delayedReply !== null")
        assert page.evaluate("window.delayedReply") == "SESSION_CHANGED"
        expect(page.get_by_role("button", name=re.compile(r"^Account menu, Staff"))).to_be_visible()
    finally:
        context.close()


@pytest.fixture
def limited_export_server(h, request):
    h.server_env = {"ROTA_RATE_EXPORTS_PER_MINUTE": "2"}
    return request.getfixturevalue("live_server")


def test_report_export_downloads_and_a_real_rate_limit_is_explained(browser, limited_export_server, h):
    publication = h.publish(h.shift(h.workspace("Export recovery journey"))["workspace"])["publication_id"]
    context, page = sign_in(browser, limited_export_server, "coordinator")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        navigate(page,"insights/exports")  # downloads live in Insights › Exports
        workload_csv = page.get_by_role("button", name="Download Workload CSV", exact=True)
        with page.expect_download() as download:
            workload_csv.click()
        assert download.value.suggested_filename == "scheduled-workload.csv"
        assert download.value.failure() is None
        # A fixed minute can roll over between exports, at most once in these few seconds, so the 5 exports (with the one
        # above) cannot all fit under 2 per window: one of them is refused, wherever the minute ends. (With 4, a roll-over
        # between the 2nd and 3rd left 2 + 2 and nothing was refused.) The API tests pin the clock for the exact ceiling.
        statuses = []
        for _ in range(4):
            with page.expect_response(lambda response: response.request.method == "GET"
                                      and response.url == f"{limited_export_server}/api/reports/{publication}?format=csv") as exported:
                workload_csv.click()
            response = exported.value
            statuses.append(response.status)
            assert response.status in {200, 429}, statuses
            if response.status == 429:
                break
        assert statuses[-1] == 429, statuses
        assert response.json()["error"]["code"] == "RATE_LIMITED"
        assert 1 <= int(response.headers["retry-after"]) <= 60
        expect(page.locator(".toast.error")).to_contain_text("Too many reports or exports")
        assert not errors
    finally:
        context.close()

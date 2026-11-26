"""Sign-in (docs/design/sign-in.md): always the username or email first, then the password step or the institutional
provider; unknown names behave like password accounts; a delay is a calm countdown; Remember me goes with
both methods. Configuration › Sign-in: write-only secret, redirect URI to copy, reason + same-button confirm,
revision conflicts, Undo, and the break-glass rule said only where it matters."""
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from .conftest import TEST_PASSWORD
from .governance_support import identity
from .test_browser import browser, live_server, new_context, open_app, open_config, route_fetch, sign_in, sign_in_with_form, fill_sign_in, expect_loaded  # noqa: F401

pytestmark = pytest.mark.browser

VIEWPORTS = [({"width": 1440, "height": 900}, "light"), ({"width": 390, "height": 844}, "dark")]
IDS = ["desktop-light", "phone-dark"]


def routed(context):
    """The server as configured for both methods (the routing itself is still the real /auth/identify unless a test
    answers it)."""
    def config(route):
        reply = route_fetch(route)
        route.fulfill(response=reply, json={**reply.json(), "mode": "both", "institutional_login": True})
    context.route("**/auth/config", config)


def delayed(route, *, seconds=3):
    route.fulfill(status=429, content_type="application/json", headers={"Retry-After": str(seconds)},
                  body=json.dumps({"error": {"code": "LOGIN_RATE_LIMIT", "message": "Too many attempts; try again later",
                                             "details": {"retry_after": seconds}}}))


@pytest.mark.parametrize("viewport,scheme", VIEWPORTS, ids=IDS)
def test_username_first_then_password_step_with_change_and_remember_me(browser, live_server, viewport, scheme):
    """Password only too: the username or email first, then the password step (the name, "Not you? Change",
    Remember me); the username stays in the form for password managers."""
    context = new_context(browser, viewport, color_scheme=scheme)
    try:
        page = open_app(context, live_server)
        form = page.locator(".login-form")
        username, password = form.get_by_label("Username or email", exact=True), form.get_by_label("Password", exact=True)
        remember, change = form.get_by_label("Remember me", exact=True), form.get_by_role("button", name="Not you? Change")
        expect(username).to_be_focused()
        expect(username).to_have_attribute("autocomplete", "username")
        expect(password).to_be_hidden()
        expect(change).to_be_hidden()
        expect(remember).not_to_be_checked()
        expect(form.get_by_role("button", name="Sign in", exact=True)).to_have_count(0)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        username.fill("staff")
        with page.expect_request("**/auth/identify"):
            form.get_by_role("button", name="Continue", exact=True).click()
        expect(password).to_be_focused()
        expect(password).to_have_attribute("autocomplete", "current-password")
        expect(username).to_be_hidden()
        expect(form.locator(".login-who")).to_contain_text("staff")
        # "Not you? Change": back to the username, the password typed is dropped.
        password.fill("typed-for-the-wrong-name")
        change.click()
        expect(username).to_be_focused()
        expect(password).to_be_hidden()
        expect(password).to_have_value("")
        username.fill("coordinator")
        form.get_by_role("button", name="Continue", exact=True).click()
        expect(username).to_have_value("coordinator")  # carried into the password step for password managers
        password.fill(TEST_PASSWORD)
        remember.check()
        with page.expect_request("**/auth/login") as sent:
            form.get_by_role("button", name="Sign in", exact=True).click()
        assert sent.value.post_data_json["remember_me"] is True
        assert sent.value.post_data_json["username"] == "coordinator"
        expect(page.get_by_role("button", name=re.compile(r"^Account menu, "))).to_be_visible(timeout=20000)
    finally:
        context.close()


@pytest.mark.parametrize("viewport,scheme", VIEWPORTS, ids=IDS)
def test_unknown_name_behaves_like_a_password_account(browser, live_server, viewport, scheme):
    """No enumeration: an unknown name gets the password step and the generic refusal, like a wrong password, with
    institutional sign-in on as well."""
    context = new_context(browser, viewport, color_scheme=scheme)
    routed(context)
    try:
        page = open_app(context, live_server)
        form = page.locator(".login-form")
        password = form.get_by_label("Password", exact=True)
        expect(form.get_by_role("link", name="Continue with institutional sign-in")).to_have_count(0)
        expect(form.get_by_role("link", name="Continue with institution", exact=True)).to_be_visible()
        form.get_by_label("Username or email", exact=True).fill("nobody.synthetic@example.test")
        form.get_by_role("button", name="Continue", exact=True).click()
        expect(password).to_be_focused()
        password.fill("not-the-password")
        form.get_by_role("button", name="Sign in", exact=True).click()
        expect(form.locator("#login-error .notice.error")).to_be_visible()
        expect(password).to_have_attribute("aria-invalid", "true")
    finally:
        context.close()


@pytest.mark.parametrize("code,message", [
    ("UNPROVISIONED_IDENTITY", "Your identity needs an active departmental role binding"),
    ("FORBIDDEN", "No access to this department"),
])
def test_password_access_refusal_leaves_the_password_field_valid(browser, live_server, code, message):
    context = new_context(browser)

    def login_response(route):
        attempt = route.request.post_data_json
        if attempt["password"] != TEST_PASSWORD:
            route.fulfill(status=401, json={"error": {"code": "INVALID_LOGIN", "message": "Unable to sign in"}})
        elif code == "FORBIDDEN" and not attempt.get("department_id"):
            route.fulfill(status=409, json={"error": {
                "code": "DEPARTMENT_REQUIRED", "message": "Select a department for this session",
                "details": {"departments": [{"id": "first", "name": "First"}, {"id": "second", "name": "Second"}]},
            }})
        else:
            route.fulfill(status=403, json={"error": {"code": code, "message": message}})

    context.route("**/auth/login", login_response)
    try:
        page = open_app(context, live_server)
        form = page.locator(".login-form")
        form.get_by_label("Username or email", exact=True).fill("staff")
        form.get_by_role("button", name="Continue", exact=True).click()
        password = form.get_by_label("Password", exact=True)
        submit = form.get_by_role("button", name="Sign in", exact=True)
        password.fill("not-the-password")
        submit.click()
        expect(password).to_have_attribute("aria-invalid", "true")

        password.fill(TEST_PASSWORD)
        submit.click()
        if code == "FORBIDDEN":
            department = form.get_by_label("Department", exact=True)
            expect(department).to_be_focused()
            department.select_option("first")
            submit.click()
        expect(form.locator("#login-error .notice.error")).to_have_text(message)
        expect(password).not_to_have_attribute("aria-invalid", "true")
        expect(password).not_to_have_attribute("aria-describedby", "login-error")
        if code == "FORBIDDEN":
            expect(department).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize("remember", [False, True], ids=["session", "remembered"])
def test_institutional_account_goes_to_auth_start_with_remember_me_and_the_page(browser, live_server, remember):
    context = new_context(browser)
    routed(context)
    context.route("**/auth/identify", lambda route: route.fulfill(json={"method": "institutional"}))
    started = []
    context.route("**/auth/start**", lambda route: (started.append(route.request.url),
                                                    route.fulfill(status=200, content_type="text/html", body="<title>IdP</title>")))
    try:
        page = context.new_page()
        page.goto(live_server + "/#schedule/issues?draft=abc")
        page.get_by_label("Username or email", exact=True).fill("hospital.person")
        if remember:
            page.get_by_label("Remember me", exact=True).check()
        button = page.get_by_role("button", name="Continue", exact=True)
        box = button.bounding_box()  # a real pointer click: nothing covers the button
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        expect(page).to_have_title("IdP")
        query = parse_qs(urlparse(started[0]).query)
        assert query["next"] == ["#schedule/issues?draft=abc"]
        assert query.get("remember_me") == (["true"] if remember else None)
        assert "identifier" not in query  # the name typed is never put in an address
    finally:
        context.close()


@pytest.mark.parametrize("step", ["identify", "login"])
def test_a_delay_counts_down_calmly_and_keeps_what_was_typed(browser, live_server, step):
    context = new_context(browser)
    context.route(f"**/auth/{step}", delayed)
    try:
        page = context.new_page()
        page.clock.install()
        page.goto(live_server)
        form = page.locator(".login-form")
        form.get_by_label("Username or email", exact=True).fill("coordinator")
        if step == "login":
            form.get_by_role("button", name="Continue", exact=True).click()
            form.get_by_label("Password", exact=True).fill(TEST_PASSWORD)
        form.get_by_label("Remember me", exact=True).check()
        submit = form.locator("button[type=submit]")
        submit.click()
        wait = form.locator("#login-error .notice")
        expect(wait).to_have_text("Too many attempts. Try again in 3 s.")
        expect(wait).not_to_have_class(re.compile(r"\berror\b"))
        expect(submit).to_be_disabled()
        page.clock.run_for(1000)
        expect(wait).to_have_text("Too many attempts. Try again in 2 s.")
        page.clock.run_for(2500)
        expect(wait).to_have_count(0)
        expect(submit).to_be_enabled()
        expect(form.get_by_label("Username or email", exact=True)).to_have_value("coordinator")
        expect(form.get_by_label("Remember me", exact=True)).to_be_checked()
    finally:
        context.close()


@pytest.mark.parametrize("viewport,scheme", VIEWPORTS, ids=IDS)
def test_both_methods_keep_institutional_sign_in_reachable(browser, live_server, viewport, scheme):
    """Passwords and the provider both on: one compact "Continue with institution" under the identifier form, for an
    identity linked to the provider without a password account; Remember me goes with it."""
    context = new_context(browser, viewport, color_scheme=scheme)
    routed(context)
    started = []
    context.route("**/auth/start**", lambda route: (started.append(route.request.url),
                                                    route.fulfill(status=200, content_type="text/html", body="<title>IdP</title>")))
    try:
        page = open_app(context, live_server)
        form = page.locator(".login-form")
        expect(form.get_by_label("Username or email", exact=True)).to_be_focused()
        link = form.get_by_role("link", name="Continue with institution", exact=True)
        expect(link).to_have_count(1)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        form.get_by_label("Remember me", exact=True).check()
        link.click()
        expect(page).to_have_title("IdP")
        assert parse_qs(urlparse(started[0]).query)["remember_me"] == ["true"]
    finally:
        context.close()


def test_use_another_account_ends_the_local_countdown(browser, live_server):
    """A 429 countdown belongs to the remembered account: "Use another account" makes Continue available at once
    (the server still throttles if it must)."""
    context = new_context(browser)
    context.add_init_script("localStorage.setItem('rota.account', JSON.stringify({identity: 'x', name: 'Quartz Example', username: 'coordinator'}))")
    context.route("**/auth/identify", lambda route: delayed(route, seconds=600))
    try:
        page = context.new_page()
        page.clock.install()
        page.goto(live_server)
        form = page.locator(".login-form")
        submit = form.locator("button[type=submit]")
        submit.click()
        expect(form.locator("#login-error .notice")).to_contain_text("Too many attempts.")
        expect(submit).to_be_disabled()
        form.get_by_role("button", name="Use another account", exact=True).click()
        expect(form.locator("#login-error .notice")).to_have_count(0)
        expect(submit).to_be_enabled()
        page.clock.run_for(2000)  # the old countdown no longer touches the form
        expect(submit).to_be_enabled()
        expect(form.locator("#login-error .notice")).to_have_count(0)
    finally:
        context.close()


def test_not_you_change_ends_the_local_countdown(browser, live_server):
    """A 429 countdown on the password step belongs to the name it was for: "Not you? Change" makes Continue available
    at once, with no countdown and no problem left on the password (the server still throttles if it must)."""
    context = new_context(browser)
    context.route("**/auth/login", lambda route: delayed(route, seconds=600))
    try:
        page = context.new_page()
        page.clock.install()
        page.goto(live_server)
        form = page.locator(".login-form")
        password = form.get_by_label("Password", exact=True)
        form.get_by_label("Username or email", exact=True).fill("staff")
        form.get_by_role("button", name="Continue", exact=True).click()
        password.fill(TEST_PASSWORD)
        submit = form.locator("button[type=submit]")
        submit.click()
        expect(form.locator("#login-error .notice")).to_contain_text("Too many attempts.")
        expect(submit).to_be_disabled()
        form.get_by_role("button", name="Not you? Change").click()
        expect(form.locator("#login-error .notice")).to_have_count(0)
        expect(submit).to_be_enabled()
        expect(submit).to_have_text("Continue")
        expect(password).not_to_have_attribute("aria-invalid", "true")
        page.clock.run_for(2000)  # the old countdown no longer touches the form
        expect(submit).to_be_enabled()
        expect(form.locator("#login-error .notice")).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize("viewport,scheme", VIEWPORTS, ids=IDS)
def test_a_refused_password_for_the_remembered_account_names_it(browser, live_server, viewport, scheme):
    """After Sign out the device offers the last account: its password field is described by that account, and a
    refused password names the account it was tried for, with "Use another account" in the message itself."""
    context = new_context(browser, viewport, color_scheme=scheme)
    context.add_init_script("localStorage.setItem('rota.account', JSON.stringify({identity: 'x', name: 'Quartz Example', username: 'coordinator'}))")
    context.route("**/auth/login", lambda route: route.fulfill(status=401, content_type="application/json", body=json.dumps(
        {"error": {"code": "INVALID_LOGIN", "message": "Unable to sign in with these credentials", "details": None}})))
    try:
        page = open_app(context, live_server)
        form = page.locator(".login-form")
        password = form.get_by_label("Password", exact=True)
        expect(page.get_by_text("Continue where you left off.", exact=True)).to_have_count(0)  # the card says who
        form.get_by_role("button", name="Continue", exact=True).click()
        expect(password).to_be_focused()
        expect(password).to_have_accessible_description(re.compile("Quartz Example"))
        password.fill("not-this-account")
        form.get_by_role("button", name="Sign in", exact=True).click()
        error = form.locator("#login-error .notice")
        expect(error).to_contain_text("Unable to sign in as Quartz Example with this password.")
        expect(password).to_be_focused()
        expect(password).to_have_attribute("aria-describedby", "login-account-who login-error")
        # Directly under the Password field, like other field errors.
        assert page.evaluate("document.querySelector('#login-error').previousElementSibling.contains(document.querySelector('#current-password'))")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        error.get_by_role("button", name="Use another account", exact=True).click()
        expect(page.locator(".login-account")).to_have_count(0)
        expect(form.locator("#login-error .notice")).to_have_count(0)
        expect(form.get_by_role("button", name="Use another account", exact=True)).to_have_count(0)
        username = form.get_by_label("Username or email", exact=True)
        expect(username).to_be_focused()
        expect(username).to_have_value("")
        expect(password).to_be_hidden()
        expect(password).not_to_have_attribute("aria-describedby", "login-account-who")
    finally:
        context.close()


def test_a_refused_password_for_a_typed_account_keeps_the_server_words(browser, live_server):
    """Only the remembered account is named: a name typed into the form keeps the server's own refusal."""
    context = new_context(browser)
    context.route("**/auth/login", lambda route: route.fulfill(status=401, content_type="application/json", body=json.dumps(
        {"error": {"code": "INVALID_LOGIN", "message": "Unable to sign in with these credentials", "details": None}})))
    try:
        page = open_app(context, live_server)
        form = page.locator(".login-form")
        form.get_by_label("Username or email", exact=True).fill("staff")
        form.get_by_role("button", name="Continue", exact=True).click()
        form.get_by_label("Password", exact=True).fill("not-this-account")
        form.get_by_role("button", name="Sign in", exact=True).click()
        expect(form.locator("#login-error .notice")).to_have_text("Unable to sign in with these credentials")
        expect(form.locator("#login-error").get_by_role("button", name="Use another account")).to_have_count(0)
    finally:
        context.close()


def test_after_sign_out_another_accounts_password_names_the_remembered_one(browser, live_server):
    """The reported path, end to end with the real server: one account signs in and out, another person types their
    own password on the card the device offers, the refusal names the card's account, and "Use another account" in it
    signs the other person in."""
    context, page = sign_in_with_form(browser, live_server, "staff")
    try:
        menu = page.get_by_role("button", name=re.compile(r"^Account menu, "))
        name = menu.get_attribute("aria-label").removeprefix("Account menu, ")
        assert name
        menu.click()
        page.get_by_role("menuitem", name="Sign out", exact=True).click()
        form = page.locator(".login-form")
        expect(page.locator(".login-account")).to_contain_text(name)
        expect(form.get_by_label("Username or email", exact=True)).to_be_hidden()
        form.get_by_role("button", name="Continue", exact=True).click()
        form.get_by_label("Password", exact=True).fill("not-this-account")
        with page.expect_response("**/auth/login") as refused:
            form.get_by_role("button", name="Sign in", exact=True).click()
        assert refused.value.status == 401
        error = form.locator("#login-error .notice")
        expect(error).to_contain_text(f"Unable to sign in as {name} with this password.")
        error.get_by_role("button", name="Use another account", exact=True).click()
        fill_sign_in(page, "coordinator")
        expect(menu).to_be_visible(timeout=20000)
        expect(menu).not_to_have_accessible_name(f"Account menu, {name}")
    finally:
        context.close()


@pytest.mark.parametrize("failure", ["http", "network"])
def test_token_rotation_is_stamped_only_when_it_succeeds(browser, live_server, failure):
    """A due rotation that fails (refused or no connection) is tried again at the next open, not 12 hours later."""
    context, page = sign_in(browser, live_server, "staff")
    account = page.get_by_role("button", name=re.compile(r"^Account menu, "))
    try:
        page.evaluate("localStorage.setItem('rota.rotated', '1')")
        page.route("**/auth/refresh", (lambda route: route.fulfill(status=503, json={})) if failure == "http" else (lambda route: route.abort()))
        with page.expect_request("**/auth/refresh"):
            page.reload()
        expect(account).to_be_visible(timeout=20000)
        assert page.evaluate("localStorage.getItem('rota.rotated')") == "1"
        page.unroute("**/auth/refresh")
        with page.expect_response("**/auth/refresh") as refreshed:
            page.reload()
        assert refreshed.value.ok
        expect(account).to_be_visible(timeout=20000)
        assert int(page.evaluate("localStorage.getItem('rota.rotated')")) > 1
    finally:
        context.close()


def test_institutional_only_server_keeps_remember_me_on_its_button(browser, live_server):
    def institutional_only(route):
        reply = route_fetch(route)
        route.fulfill(response=reply, json={**reply.json(), "password_login": False, "development_login": False,
                                            "institutional_login": True, "mode": "institutional_only"})
    context = new_context(browser)
    context.route("**/auth/config", institutional_only)
    started = []
    context.route("**/auth/start**", lambda route: (started.append(route.request.url),
                                                    route.fulfill(status=200, content_type="text/html", body="<title>IdP</title>")))
    try:
        page = open_app(context, live_server)
        form = page.locator(".login-form")
        start = form.get_by_role("link", name="Continue with institutional sign-in", exact=True)
        expect(start).to_be_visible()  # the form is drawn before its absences mean anything
        expect(form.get_by_label("Password", exact=True)).to_have_count(0)
        form.get_by_label("Remember me", exact=True).check()
        start.click()
        expect(page).to_have_title("IdP")
        assert parse_qs(urlparse(started[0]).query)["remember_me"] == ["true"]
    finally:
        context.close()


# Configuration › Sign-in

PATH = "/api/admin/auth-config"


def provider_form(page):
    page.get_by_label("Institutional sign-in", exact=True).check()
    page.get_by_label("Issuer", exact=True).fill("https://idp.test")
    page.get_by_label("Client ID", exact=True).fill("rota-synthetic")
    page.get_by_label("Allowed provider hosts", exact=True).fill("idp.test")


def save_with_reason(page, reason="Synthetic IT configuration"):
    page.get_by_role("region", name="Unsaved changes").get_by_role("button", name="Save", exact=True).click()
    dialog = page.get_by_role("dialog", name="Save sign-in settings")
    dialog.get_by_label("Reason", exact=True).fill(reason)
    submit = dialog.locator(".form-footer button[type=submit]")
    submit.click()
    expect(submit).to_have_class(re.compile(r"\barmed\b"))  # same-button confirm
    submit.click()
    return dialog


@pytest.mark.parametrize("viewport,scheme", VIEWPORTS, ids=IDS)
def test_sign_in_settings_default_off_save_test_and_undo(browser, live_server, h, viewport, scheme):
    context, page = sign_in(browser, live_server, "admin", viewport, color_scheme=scheme)
    try:
        open_config(page, "Sign-in")
        pane = page.locator(".config-pane")
        # Default: password only, the provider and the method hidden until institutional sign-in is turned on.
        expect(pane.get_by_label("Institutional sign-in", exact=True)).not_to_be_checked()
        expect(pane.get_by_role("heading", name="Institutional provider")).to_be_hidden()
        expect(pane.get_by_label("Sign-in method", exact=True)).to_be_hidden()
        expect(pane.get_by_role("button", name="Undo last change")).to_have_count(0)
        expect(page.get_by_role("region", name="Unsaved changes")).to_be_hidden()
        provider_form(page)
        expect(pane.get_by_role("heading", name="Institutional provider")).to_be_visible()
        expect(pane.locator(".sign-in-uri code")).to_have_text(re.compile(r"/auth/callback$"))
        expect(pane.get_by_role("button", name="Copy redirect URI")).to_be_visible()
        expect(pane.get_by_label("Client secret", exact=True)).to_have_value("")
        recovery = pane.get_by_text("A local administrator can always sign in with a password.")
        expect(recovery).to_have_count(0)
        pane.get_by_label("Sign-in method", exact=True).select_option("institutional_only")
        expect(recovery).to_have_count(0)
        pane.get_by_label("Sign-in method", exact=True).select_option("password_only")
        expect(pane.get_by_role("button", name="Test connection")).to_be_disabled()  # tests the saved settings only
        expect(save_with_reason(page)).to_be_hidden()
        expect(page.locator("#toast-tray")).to_contain_text("Sign-in settings saved.")
        saved = h.client("admin").get(PATH).json()
        assert saved["institutional_enabled"] is True and saved["issuer"] == "https://idp.test" and saved["revision"] == 1
        # Saved: the test runs (the synthetic provider cannot be reached, so it says so) and Undo is offered.
        pane.get_by_role("button", name="Test connection").click()
        expect(pane.locator(".notice.error")).to_contain_text("Unable to verify provider discovery or signing keys")
        pane.get_by_role("button", name="Undo last change").click()
        dialog = page.get_by_role("dialog", name="Undo the last sign-in change")
        dialog.get_by_label("Reason", exact=True).fill("Synthetic rollback")
        dialog.locator(".form-footer button[type=submit]").click()
        dialog.locator(".form-footer button[type=submit].armed").click()
        expect(dialog).to_be_hidden()
        expect(pane.get_by_label("Institutional sign-in", exact=True)).not_to_be_checked()
        assert h.client("admin").get(PATH).json()["institutional_enabled"] is False
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    finally:
        context.close()


def test_sign_in_settings_conflict_secret_and_links(browser, live_server, h):
    context, page = sign_in(browser, live_server, "admin")
    try:
        open_config(page, "Sign-in")
        pane = page.locator(".config-pane")
        provider_form(page)
        # Linked accounts: rows are added and removed in place.
        pane.get_by_role("button", name="Add link").click()
        expect(pane.locator(".sign-in-link")).to_have_count(1)
        pane.get_by_role("button", name="Remove link").click()
        expect(pane.locator(".sign-in-link")).to_have_count(0)
        # Someone else saves meanwhile: this save is refused and the page says so, with Reload.
        other = h.client("admin").put(PATH, json={"expected_revision": 0, "confirmed": True, "reason": "Elsewhere"})
        assert other.status_code == 200, other.text
        save_with_reason(page)
        conflict = pane.locator(".notice.warning", has_text="Someone else changed the sign-in settings")
        expect(conflict).to_be_visible()
        conflict.get_by_role("button", name="Reload").click()
        expect(conflict).to_have_count(0)
        expect(pane.get_by_role("button", name="Undo last change")).to_be_visible()
        provider_form(page)
        # The secret is write-only: once set it shows "Set", and its field stays empty.
        if h.client("admin").get(PATH).json()["key_configured"]:
            pane.get_by_label("Client secret", exact=True).fill("synthetic-secret")
            expect(save_with_reason(page)).to_be_hidden()
            expect(pane.locator(".rule-facts")).to_contain_text("Set")
            expect(pane.get_by_label("Client secret", exact=True)).to_have_value("")
            assert "synthetic-secret" not in page.content()
        else:
            expect(pane.get_by_label("Client secret", exact=True)).to_be_disabled()
            expect(pane.get_by_text("FMUC_DATA_KEY")).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize("role", ["coordinator", "approver", "credentialer", "programme_lead", "auditor", "staff"])
def test_only_admins_see_sign_in_settings(browser, live_server, h, role):
    if role == "programme_lead":
        identity(h, "programme_lead", ["programme_lead"])
    context, page = sign_in(browser, live_server, role)
    try:
        page.goto(live_server + "/#configuration/sign-in")
        expect(page.get_by_role("heading", name="Institutional provider")).to_have_count(0)
        expect(page.get_by_label("Institutional sign-in", exact=True)).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize("code,message", [
    ("provider_denied", "Your organisation’s sign-in was cancelled."),
    ("expired", "Sign-in took too long. Try again."),
    ("config", "Institutional sign-in isn’t set up correctly. Contact an administrator."),
    ("failed", "Couldn’t sign you in. Try again."),
    ("unknown-code", "Couldn’t sign you in. Try again."),
])
def test_provider_callback_failure_is_said_once_then_cleared(browser, live_server, code, message):
    """/auth/callback sends failures to /?signin_error=…: one calm line on the sign-in page, then the address is clean
    (a reload says nothing more)."""
    context = new_context(browser)
    try:
        page = context.new_page()
        page.goto(live_server + "/?signin_error=" + code + "#schedule")
        status = page.locator(".login-form").get_by_role("status")
        expect(status).to_have_text(message)
        assert "signin_error" not in page.url and page.url.endswith("#schedule")
        expect(page.get_by_label("Username or email", exact=True)).to_be_focused()
        page.reload()
        expect(page.get_by_label("Username or email", exact=True)).to_be_visible()
        expect(page.locator(".login-form").get_by_role("status")).to_have_count(0)
    finally:
        context.close()



CHOICE = "synthetic-opaque-continuation-7f3a"
DEPARTMENTS = [{"id": "11111111-1111-4111-8111-111111111111", "name": "General Services (test)"},
               {"id": "22222222-2222-4222-8222-222222222222", "name": "Clinical Services (test)"}]


def choice_routes(context, live_server, *, listed=None, redeemed=None):
    """AP-S01: the server-held department step, answered here (the provider exchange is the server's). `listed` and
    `redeemed` are (status, body) refusals; a successful redemption signs in through the real /auth/login, so the browser
    holds the normal session cookies only after the choice, as the server sets them."""
    calls = {"list": [], "choose": [], "cancel": [], "start": []}
    problem = lambda status, code: dict(status=status, json={"error": {"code": code, "message": f"Synthetic {code} refusal"}})

    def departments(route):
        calls["list"].append(parse_qs(urlparse(route.request.url).query))
        route.fulfill(**problem(*listed)) if listed else route.fulfill(json={"expires_at": "2026-10-30T12:05:00Z", "departments": DEPARTMENTS})

    def choose(route):
        calls["choose"].append((route.request.post_data_json, route.request.headers.get("origin")))
        if redeemed:
            return route.fulfill(**problem(*redeemed))
        reply = context.request.post(live_server + "/auth/login", data={"username": "coordinator", "password": TEST_PASSWORD},
                                     headers={"origin": live_server})
        assert reply.ok, reply.text()
        route.fulfill(json={"expires_at": None, "display_name": "Coordinator (test)", "return_to": "/#schedule"})

    context.route("**/auth/oidc/departments**", departments)
    context.route("**/auth/oidc/department", choose)
    context.route("**/auth/oidc/cancel", lambda route: (calls["cancel"].append(route.request.post_data_json), route.fulfill(json={"cancelled": True})))
    context.route("**/auth/start**", lambda route: (calls["start"].append(route.request.url),
                                                    route.fulfill(status=200, content_type="text/html", body="<title>IdP</title>")))
    return calls


@pytest.mark.parametrize("viewport,scheme", VIEWPORTS, ids=IDS)
def test_institutional_department_choice_offers_only_allowed_departments_and_signs_in(browser, live_server, viewport, scheme):
    """The continuation leaves the address at once and is never stored; only the server's allowed departments are offered;
    Continue sends exactly the continuation and the chosen department (with the page's Origin), then the app opens."""
    context = new_context(browser, viewport, color_scheme=scheme)
    routed(context)
    calls = choice_routes(context, live_server)
    try:
        page = context.new_page()
        page.goto(live_server + "/?signin_choice=" + CHOICE)
        department = page.get_by_label("Department", exact=True)
        expect(page.get_by_role("heading", name="Choose a department", exact=True)).to_be_visible()
        expect(department).to_be_focused()
        assert "signin_choice" not in page.url and CHOICE not in page.url, page.url
        assert calls["list"] == [{"continuation": [CHOICE]}], calls
        expect(department.locator("option:not([hidden])")).to_have_text([d["name"] for d in DEPARTMENTS])
        expect(department).to_have_value("")
        stored = page.evaluate("JSON.stringify({...localStorage}) + JSON.stringify({...sessionStorage})")
        assert CHOICE not in stored, stored
        department.select_option(DEPARTMENTS[1]["id"])
        page.get_by_role("button", name="Continue", exact=True).click()
        expect(page.get_by_role("button", name=re.compile(r"^Account menu, "))).to_be_visible(timeout=20000)
        assert calls["choose"] == [({"continuation": CHOICE, "department_id": DEPARTMENTS[1]["id"]}, live_server)], calls
        expect_loaded(page, "schedule")
        expect(page).to_have_url(re.compile(rf"^{re.escape(live_server)}/#schedule/week/[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$"))
        assert "signin_choice" not in page.url, page.url
    finally:
        context.close()


def test_institutional_department_choice_cancel_ends_it_and_keeps_the_password_sign_in(browser, live_server):
    context = new_context(browser)
    routed(context)
    calls = choice_routes(context, live_server)
    try:
        page = context.new_page()
        page.goto(live_server + "/?signin_choice=" + CHOICE + "#schedule")
        expect(page.get_by_label("Department", exact=True)).to_be_visible()
        page.get_by_role("button", name="Cancel", exact=True).click()
        expect(page.get_by_label("Username or email", exact=True)).to_be_visible()
        assert calls["cancel"] == [{"continuation": CHOICE}] and calls["choose"] == [], calls
        page.reload()  # the continuation was never kept: no department step again
        expect(page.get_by_label("Username or email", exact=True)).to_be_visible()
        expect(page.get_by_label("Department", exact=True)).to_have_count(0)
        # The password path is unchanged after a cancelled institutional sign-in.
        page.get_by_label("Username or email", exact=True).fill("coordinator")
        page.get_by_role("button", name="Continue", exact=True).click()
        page.get_by_label("Password", exact=True).fill(TEST_PASSWORD)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("button", name=re.compile(r"^Account menu, "))).to_be_visible(timeout=20000)
    finally:
        context.close()


@pytest.mark.parametrize("listed,redeemed", [
    ((401, "OIDC_STATE"), None),                    # expired, cancelled or another browser's continuation
    ((401, "OIDC_CONFIGURATION_CHANGED"), None),
    (None, (401, "OIDC_REAUTHENTICATION")),         # forced re-authentication at redemption
    (None, (403, "UNPROVISIONED_IDENTITY")),        # the account lost its access since the provider sign-in
    (None, (403, "FORBIDDEN")),                     # a department not (any more) allowed
], ids=["state", "configuration", "reauthentication", "unprovisioned", "foreign"])
def test_institutional_department_choice_refusals_say_why_and_restart_only_when_asked(browser, live_server, listed, redeemed):
    """Every refusal is said with the server's words; nothing is retried or redeemed by itself, no session is kept, and
    Sign in again cancels the continuation, then starts a fresh institutional sign-in."""
    context = new_context(browser)
    routed(context)
    calls = choice_routes(context, live_server, listed=listed, redeemed=redeemed)
    code = (listed or redeemed)[1]
    try:
        page = context.new_page()
        page.goto(live_server + "/?signin_choice=" + CHOICE + "#schedule")
        if redeemed:
            page.get_by_label("Department", exact=True).select_option(DEPARTMENTS[0]["id"])
            page.get_by_role("button", name="Continue", exact=True).click()
        expect(page.locator("#login-error")).to_contain_text(f"Synthetic {code} refusal")
        again = page.get_by_role("button", name="Sign in again", exact=True)
        expect(again).to_be_focused()
        assert len(calls["choose"]) == (1 if redeemed else 0), calls
        assert page.request.get(live_server + "/api/me").status == 401  # no session from a refused choice
        again.click()
        expect(page).to_have_title("IdP")
        assert calls["cancel"] == [{"continuation": CHOICE}], calls
        assert len(calls["start"]) == 1 and len(calls["choose"]) == (1 if redeemed else 0), calls
    finally:
        context.close()

def test_remember_me_days_is_validated_saved_and_undone(browser, live_server, h):
    context, page = sign_in(browser, live_server, "admin")
    try:
        open_config(page, "Sign-in")
        days = page.locator(".config-pane").get_by_label("Remember me for (days)", exact=True)
        expect(days).to_have_value("30")
        days.fill("0")  # outside 1–90: the form says so and nothing opens
        page.get_by_role("region", name="Unsaved changes").get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_role("dialog", name="Save sign-in settings")).to_have_count(0)
        assert not days.evaluate("n => n.checkValidity()")
        days.fill("14")
        expect(save_with_reason(page)).to_be_hidden()
        assert h.client("admin").get(PATH).json()["remember_me_days"] == 14
        page.locator(".config-pane").get_by_role("button", name="Undo last change").click()
        dialog = page.get_by_role("dialog", name="Undo the last sign-in change")
        dialog.get_by_label("Reason", exact=True).fill("Synthetic rollback")
        dialog.locator(".form-footer button[type=submit]").click()
        dialog.locator(".form-footer button[type=submit].armed").click()
        expect(days).to_have_value("30")
    finally:
        context.close()


def test_links_show_their_issuer_and_are_reconfirmed_when_it_changes(browser, live_server, h):
    staff = h.seed["identities"]["staff"]
    saved = h.client("admin").put(PATH, json={
        "expected_revision": 0, "confirmed": True, "reason": "Synthetic provider", "mode": "password_only",
        "institutional_enabled": True, "issuer": "https://idp.test", "client_id": "rota-synthetic",
        "allowed_hosts": ["idp.test", "idp2.test"],
        "institutional_bindings": [{"identity_id": staff, "issuer": "https://idp.test", "subject": "synthetic-subject"}]})
    assert saved.status_code == 200, saved.text
    context, page = sign_in(browser, live_server, "admin")
    try:
        open_config(page, "Sign-in")
        pane = page.locator(".config-pane")
        link = pane.locator(".sign-in-link")
        expect(link.locator(".sign-in-link-issuer")).to_contain_text("https://idp.test")
        confirm = link.get_by_label("Confirm for the new issuer", exact=True)
        expect(confirm).to_be_hidden()
        pane.get_by_label("Issuer", exact=True).fill("https://idp2.test")
        expect(confirm).to_be_visible()
        # Not confirmed: Save stops at the link.
        page.get_by_role("region", name="Unsaved changes").get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_role("dialog", name="Save sign-in settings")).to_have_count(0)
        # The problem describes the box; its name stays its label (dom.ts namingLabel, WCAG 4.1.2).
        expect(confirm).to_have_attribute("aria-invalid", "true")
        expect(confirm).to_have_accessible_name("Confirm for the new issuer")
        expect(confirm).to_have_accessible_description("Tick this box to continue")
        message = link.locator(".field-error:visible")
        expect(message).to_have_count(1)
        box, note = confirm.bounding_box(), message.bounding_box()
        assert note["y"] >= box["y"] + box["height"] - 1, (box, note)  # on its own line under the box, not beside it
        confirm.check()
        expect(confirm).not_to_have_attribute("aria-invalid", "true")
        expect(link.locator(".field-error:visible")).to_have_count(0)
        expect(save_with_reason(page)).to_be_hidden()
        binding = h.client("admin").get(PATH).json()["institutional_bindings"][0]
        assert binding == {"identity_id": staff, "issuer": "https://idp2.test", "subject": "synthetic-subject"}
        expect(link.locator(".sign-in-link-issuer")).to_contain_text("https://idp2.test")
        expect(link.get_by_label("Confirm for the new issuer", exact=True)).to_be_hidden()
        # Back to the old issuer without confirming: the browser-side check holds it, the server is never asked.
        pane.get_by_label("Issuer", exact=True).fill("https://idp.test")
        expect(link.get_by_label("Confirm for the new issuer", exact=True)).to_be_visible()
    finally:
        context.close()

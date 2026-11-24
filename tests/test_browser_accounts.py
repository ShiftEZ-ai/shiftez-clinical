"""An admin invites a person; the person opens the one-time link, sets a password and signs in with it."""
import json
import re
import secrets
import pytest
from playwright.sync_api import expect
from sqlalchemy import select
from apps.api import models as m
from apps.api.auth import digest
from apps.api.db import transaction
from apps.api.identity_store import AutheliaUsersFile
from apps.api.main import create_app
from apps.api.settings import Settings
from packages.contracts.schemas import SYSTEM_ROLES
from .test_browser import browser, live_server, sign_in, navigate, new_context, open_app, route_fetch, wait_held, wait_until  # noqa: F401
from .test_browser_rule_catalogue import shot
from .governance_support import developer

pytestmark = pytest.mark.browser


@pytest.fixture
def users_file(h, tmp_path):
    path = tmp_path / "users.yml"
    path.write_text(json.dumps({"users": {}}), encoding="utf-8")
    h.server_env = {"ROTA_IDENTITY_USERS_FILE": str(path)}
    return path


def test_invite_accept_and_sign_in(users_file, browser, live_server, h):
    # The owner's set-up (Admin + Developer): it invites, and it reads the audit log the invitation lands in.
    context, page = sign_in(browser, live_server, developer(h))
    try:
        navigate(page,"access/people")
        page.get_by_role("button", name="Invite person", exact=True).click()
        form = page.get_by_role("dialog")
        form.get_by_label("Name shown in the app", exact=True).fill("Invited Person (test)")
        form.get_by_label("Email", exact=True).fill("invited.person@example.test")
        expect(form.get_by_label("Sign-in name", exact=True)).to_have_value("invited.person")
        form.get_by_label("Reason", exact=True).fill("Fictional invitation for a test account")
        form.get_by_role("button", name="Create invitation", exact=True).click()
        shown = page.get_by_role("dialog").filter(has_text="Invitation link")
        link = shown.get_by_label("One-time link", exact=True).input_value()
        assert re.fullmatch(re.escape(live_server) + r"/invite/[A-Za-z0-9_-]{43}", link)
        shown.get_by_role("button", name="Done", exact=True).click()
        row = page.locator(".dt-table tbody tr:not(.dt-group)").filter(has_text="Invited Person (test)").first
        expect(row).to_contain_text("Invited")
        expect(row).to_contain_text("invited.person")
        # maintenance: the history reads the contract's words, never the raw action ("invite account", chip "account").
        navigate(page, "system/audit")
        entry = page.locator(".audit-row").filter(has_text="Invited Person (test)").first
        expect(entry).to_contain_text("invited an account")
        expect(entry.locator(".role-chip")).to_have_text("Accounts")
    finally:
        context.close()
    assert json.loads(users_file.read_text(encoding="utf-8"))["users"]["invited.person"]["disabled"] is True

    password = secrets.token_urlsafe(18)  # a throwaway test value, never printed
    guest = browser.new_context(viewport={"width": 390, "height": 844}, locale="en-GB", timezone_id="Asia/Riyadh")
    try:
        page = guest.new_page()
        page.goto(link)
        expect(page.get_by_role("heading", name="Welcome, Invited Person (test)")).to_be_visible()
        page.get_by_label("New password", exact=True).fill("short")
        expect(page.get_by_text("Too short", exact=False)).to_be_visible()
        page.get_by_label("New password", exact=True).fill(password)
        page.get_by_label("Repeat new password", exact=True).fill(password)
        page.get_by_role("button", name="Set password", exact=True).click()
        expect(page.get_by_text("Your password is set", exact=False)).to_be_visible()
        # maintenance (3a94628fe): taken to sign-in offering the account just set up, its password only.
        expect(page.locator(".login-account")).to_contain_text("invited.person")
        # Identifier-first sign-in (f87383cea): the password is asked once Continue is pressed for that account.
        expect(page.get_by_label("Password", exact=True)).to_be_hidden()
        page.get_by_role("button", name="Continue", exact=True).click()
        page.get_by_label("Password", exact=True).fill(password)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("button", name=re.compile(r"^Account menu, Invited Person"))).to_be_visible()
        page.goto(link)  # single use
        expect(page.get_by_role("heading", name="This link cannot be used")).to_be_visible()
    finally:
        guest.close()
    assert json.loads(users_file.read_text(encoding="utf-8"))["users"]["invited.person"]["disabled"] is False


def test_invite_offers_only_roles_the_inviter_manages(users_file, browser, live_server, h):
    # Even with no live Developer, an admin can never choose Developer or Auditor in an invitation: those need a
    # Developer. They are shown disabled with why (maintenance), never offered.
    with transaction(h.app.state.engine, write=True) as db:
        for binding in db.scalars(select(m.RoleBinding).where(m.RoleBinding.role.in_(SYSTEM_ROLES))):
            binding.revoked = True
    context, page = sign_in(browser, live_server, "admin")
    try:
        navigate(page, "access/people")
        page.get_by_role("button", name="Invite person", exact=True).click()
        form = page.get_by_role("dialog")
        expect(form.get_by_text("Staff", exact=True).first).to_be_visible()
        # Every role is more than a short list holds, so Access roles is the shared picker (dom.ts multi, LONG_LIST).
        form.locator(".pick-field").filter(has_text="Access roles").locator(".pick-button").click()
        for role in ("Developer", "Auditor"):
            expect(page.locator(".picker").get_by_role("option", name=role, exact=True)).to_have_attribute("aria-disabled", "true")
    finally:
        context.close()


@pytest.fixture
def department_accounts(users_file, h):
    users = AutheliaUsersFile(users_file)
    accounts = [{"username": "multi.department", "password": secrets.token_urlsafe(30),
                 "name": "Multi Department (test)", "identity_id": h.seed["identities"]["staff"]},
                {"username": "single.department", "password": secrets.token_urlsafe(30),
                 "name": "Single Department (test)", "identity_id": h.seed["identities"]["operator"]}]
    for account in accounts:
        users.create(account["username"], account["name"], account["username"] + "@example.test")
        users.set_password(account["username"], account["password"])
    with transaction(h.app.state.engine, write=True) as db:
        original = db.get(m.Department, h.seed["department_id"])
        other = m.Department(organisation_id=original.organisation_id, name="Second Department (test)")
        db.add(other)
        db.flush()
        for account in accounts:
            identity = db.get(m.Identity, account["identity_id"])
            identity.display_name, identity.password_hash = account["name"], None
            db.add(m.Account(identity_id=identity.id, username=account["username"],
                             email=account["username"] + "@example.test", accepted_at=m.now()))
        db.add(m.RoleBinding(identity_id=accounts[0]["identity_id"], department_id=other.id,
                             role="staff", starts_on=m.now().date()))
        accounts[0]["department_id"], accounts[0]["department_name"] = other.id, other.name
        accounts[1]["department_id"] = original.id
    return accounts


@pytest.mark.parametrize(("width", "theme"), [(1440, "light"), (390, "dark")])
def test_password_department_selection_and_credential_edits_keep_the_account_binding(
        department_accounts, browser, live_server, h, width, theme):
    multiple, single = department_accounts
    context = new_context(browser, viewport={"width": width, "height": 900}, color_scheme=theme)
    try:
        page = open_app(context, live_server)
        username = page.get_by_label("Username or email", exact=True)
        password = page.get_by_label("Password", exact=True)
        department = page.get_by_label("Department", exact=True)
        expect(password).to_have_attribute("autocomplete", "current-password")
        expect(password).to_have_attribute("id", "current-password")
        username.fill(multiple["username"])
        page.get_by_role("button", name="Continue", exact=True).click()
        password.fill(multiple["password"])
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(department).to_be_visible()
        expect(department).to_be_focused()
        expect(department).to_have_value("")
        expect(username).to_have_value(multiple["username"])
        expect(password).to_have_value(multiple["password"])
        expect(password).not_to_have_attribute("aria-invalid", "true")
        assert not any(cookie["name"] in {"rota_session", "__Host-rota"} for cookie in context.cookies(live_server))
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(department).to_be_visible()
        expect(department).to_have_value("")
        assert not department.evaluate("select => select.checkValidity()")
        expect(page.locator(".login-form")).to_be_visible()
        assert not any(cookie["name"] in {"rota_session", "__Host-rota"} for cookie in context.cookies(live_server))
        department.click()
        picker = page.get_by_role("dialog", name="Department", exact=True)
        expect(picker.get_by_role("option")).to_have_count(2)
        shot(page, f"pr387-department-visual-{width}-{theme}")
        picker.get_by_role(
            "option", name=multiple["department_name"], exact=True).click()
        password.fill(secrets.token_urlsafe(30))
        expect(department).to_have_count(0)
        password.fill(multiple["password"])
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(department).to_be_visible()
        department.click()
        page.get_by_role("dialog", name="Department", exact=True).get_by_role(
            "option", name=multiple["department_name"], exact=True).click()
        page.get_by_role("button", name="Not you? Change", exact=True).click()
        expect(password).to_have_value("")
        username.fill(single["username"])
        expect(department).to_have_count(0)
        page.get_by_role("button", name="Continue", exact=True).click()
        password.fill(single["password"])
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("button", name="Account menu, " + single["name"], exact=True)).to_be_visible()
        me = context.request.get(live_server + "/api/me").json()
        assert (me["identity_id"], me["account"], me["department"]["id"]) == (
            single["identity_id"], {"username": single["username"]}, single["department_id"])
    finally:
        context.close()

    context = new_context(browser, viewport={"width": width, "height": 900}, color_scheme=theme)
    try:
        page = open_app(context, live_server)
        page.get_by_label("Username or email", exact=True).fill(multiple["username"])
        page.get_by_role("button", name="Continue", exact=True).click()
        page.get_by_label("Password", exact=True).fill(multiple["password"])
        page.get_by_role("button", name="Sign in", exact=True).click()
        department = page.get_by_label("Department", exact=True)
        expect(department).to_be_visible()
        department.click()
        page.get_by_role("dialog", name="Department", exact=True).get_by_role(
            "option", name=multiple["department_name"], exact=True).click()
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("button", name="Account menu, " + multiple["name"], exact=True)).to_be_visible()
        me = context.request.get(live_server + "/api/me").json()
        assert (me["identity_id"], me["account"], me["department"]["id"], me["roles"]) == (
            multiple["identity_id"], {"username": multiple["username"]}, multiple["department_id"], ["staff"])
        token = next(cookie["value"] for cookie in context.cookies(live_server) if cookie["name"] == "rota_session")
        with transaction(h.app.state.engine) as db:
            session = db.scalar(select(m.Session).where(m.Session.token_hash == digest(token)))
            assert session.identity_id == multiple["identity_id"] and session.department_id == multiple["department_id"]
            assert session.auth_method == "password" and not session.revoked
    finally:
        context.close()


@pytest.mark.parametrize(("width", "theme"), [(1440, "light"), (390, "dark")])
def test_remembered_account_cannot_change_while_password_sign_in_is_pending(
        department_accounts, browser, live_server, width, theme):
    account, _ = department_accounts
    remembered = {"identity": account["identity_id"], "name": account["name"], "username": account["username"]}
    context = new_context(browser, viewport={"width": width, "height": 900}, color_scheme=theme,
        storage_state={"cookies": [], "origins": [{"origin": live_server,
            "localStorage": [{"name": "rota.account", "value": json.dumps(remembered)}]}]})
    held = []
    try:
        page = open_app(context, live_server)
        another = page.get_by_role("button", name="Use another account", exact=True)
        remove = page.get_by_role("button", name="Remove " + account["name"] + " from this device", exact=True)
        expect(page.get_by_label("Username or email", exact=True)).to_be_hidden()
        expect(another).to_be_enabled()
        expect(remove).to_be_enabled()
        page.route("**/auth/login", lambda route: held.append(route))
        page.get_by_role("button", name="Continue", exact=True).click()
        page.get_by_label("Password", exact=True).fill(account["password"])
        page.get_by_role("button", name="Sign in", exact=True).click()
        wait_held(page, held)
        expect(another).to_be_disabled()
        expect(remove).to_be_disabled()
        route = held.pop()
        response = route_fetch(route)
        assert response.status == 409
        route.fulfill(response=response)
        department = page.get_by_label("Department", exact=True)
        expect(department).to_be_visible()
        expect(another).to_be_enabled()
        expect(remove).to_be_enabled()
        department.click()
        page.get_by_role("dialog", name="Department", exact=True).get_by_role(
            "option", name=account["department_name"], exact=True).click()
        page.get_by_role("button", name="Sign in", exact=True).click()
        wait_held(page, held)
        expect(another).to_be_disabled()
        expect(remove).to_be_disabled()
        route = held.pop()
        response = route_fetch(route)
        assert response.status == 200
        route.fulfill(response=response)
        expect(page.get_by_role("button", name="Account menu, " + account["name"], exact=True)).to_be_visible()
        me = context.request.get(live_server + "/api/me").json()
        assert (me["identity_id"], me["account"], me["department"]["id"]) == (
            account["identity_id"], {"username": account["username"]}, account["department_id"])
    finally:
        for route in held:
            route.abort()
        context.close()


def test_after_setting_a_password_sign_in_offers_the_new_account(users_file, browser, live_server, h):
    """maintenance: on a device that remembers another account, finishing an invitation makes sign-in offer the account that
    was just set up (identifier first, prefilled), never the previous one; that account's device memory goes too, and no
    password is kept."""
    # The harness's own app sends the invitation, so it reads the users file the live server was given (server_env).
    h.app.state.engine.dispose()
    h.settings = Settings(environment='test', database_url=h.settings.database_url, identity_users_file=str(users_file))
    h.app = create_app(h.settings)
    result = h.command('admin', 'account.invite', {'display_name': 'Set-up Person (test)', 'email': 'setup.person@example.test',
                                                   'username': 'setup.person', 'roles': ['staff'],
                                                   'reason': 'Fictional invitation for a sign-in check'})
    token = result['one_time_link'].rsplit('/', 1)[1]
    previous = h.get('admin', '/api/me')['display_name']
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'home')  # the app opens: this device now remembers the admin
        remembered = lambda: page.evaluate("JSON.parse(localStorage.getItem('rota.account')||'{}').name")
        wait_until(page, lambda: remembered() == previous, lambda: f'this device remembers {remembered()!r}, not {previous!r}')
        page.evaluate("localStorage.setItem('rota.board.view', '{}')")
        context.clear_cookies()  # the admin's session ends; the device still remembers them
        page.goto(live_server + '/invite/' + token)
        expect(page.get_by_role('heading', name='Welcome, Set-up Person (test)')).to_be_visible()
        password = secrets.token_urlsafe(18)  # a throwaway test value, never printed
        page.get_by_label('New password', exact=True).fill(password)
        page.get_by_label('Repeat new password', exact=True).fill(password)
        page.get_by_role('button', name='Set password', exact=True).click()
        expect(page.get_by_text('Your password is set', exact=False)).to_be_visible()
        chip = page.locator('.login-account')
        expect(chip).to_contain_text('Set-up Person (test)')  # taken to sign-in, offering the new account
        expect(chip).to_contain_text('setup.person')
        expect(chip).not_to_contain_text(previous)
        expect(page.get_by_text('Continue where you left off.', exact=True)).to_be_visible()
        expect(page.get_by_label('Username or email', exact=True)).to_have_value('setup.person')
        kept = page.evaluate("Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)]))")
        assert 'rota.board.view' not in kept  # the previous account's device memory is gone
        assert password not in json.dumps(kept)
    finally:
        context.close()


def test_invite_shows_roles_it_cannot_give_and_needs_a_reason(users_file, browser, live_server, h):
    """maintenance: every role is listed and a department admin sees why explicit roles are disabled.
    maintenance: an invitation grants roles, so it takes a reason; without one nothing is sent."""
    from packages.contracts.schemas import INSTALLATION_ADMIN_ROLE, ROLE_LABELS, ROLE_NAMES
    context, page = sign_in(browser, live_server, "admin")
    sent = []
    page.on("request", lambda r: sent.append(r) if r.method == "POST" and "account.invite" in (r.post_data or "") else None)
    try:
        navigate(page, "access/people")
        page.get_by_role("button", name="Invite person", exact=True).click()
        form = page.get_by_role("dialog")
        # Every role is more than a short list holds, so Access roles is the shared picker (dom.ts multi, LONG_LIST).
        form.locator(".pick-field").filter(has_text="Access roles").locator(".pick-button").click()
        roles = page.locator(".picker")
        for role in ROLE_NAMES:
            box = roles.get_by_role("option", name=ROLE_LABELS[role], exact=True)
            expect(box).to_have_count(1)
            if role in ("auditor", "operator"):
                expect(box).to_have_attribute("aria-disabled", "true")
                expect(box).to_have_accessible_description("Only a Developer can grant this")
            elif role == INSTALLATION_ADMIN_ROLE:
                expect(box).to_have_attribute("aria-disabled", "true")
                expect(box).to_have_accessible_description(
                    f"Only an {ROLE_LABELS[role]} or {ROLE_LABELS['operator']} can grant this")
            else:
                expect(box).not_to_have_attribute("aria-disabled", "true")
        expect(roles.get_by_text("Only a Developer can grant this", exact=True)).to_have_count(2)  # said in words, not only greyed
        page.keyboard.press("Escape")
        expect(roles).to_have_count(0)
        form.get_by_label("Name shown in the app", exact=True).fill("Reasonless invite (test)")
        form.get_by_label("Email", exact=True).fill("reasonless.invite@example.test")
        form.get_by_role("button", name="Create invitation", exact=True).click()
        reason = form.get_by_label("Reason", exact=True)
        expect(reason).to_be_focused()  # the browser stops the submit at the empty required reason
        assert reason.evaluate("r => r.validity.valueMissing")
        assert not sent
        reason.fill("Fictional invitation for a test account")
        form.get_by_role("button", name="Create invitation", exact=True).click()
        expect(page.get_by_role("dialog").filter(has_text="Invitation link")).to_be_visible()
        assert len(sent) == 1 and '"reason":"Fictional invitation for a test account"' in sent[0].post_data
    finally:
        context.close()

"""An admin onboards staff from Access: picks people without a sign-in, previews, creates their accounts with a
same-button confirm and gets the one-time links once, as a file only; the per-person Resend still works after."""
import csv
import io
import re
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import ACCOUNT_ONBOARDING_LIMITS
from .test_browser import browser, expect_loaded, holding, live_server, navigate, reconnect, sign_in, wait_held  # noqa: F401
from .test_browser_accounts import users_file  # noqa: F401
from .governance_support import developer

pytestmark = pytest.mark.browser
WAVE = min(ACCOUNT_ONBOARDING_LIMITS["rows"], ACCOUNT_ONBOARDING_LIMITS["wave"])
STAFF = "Onboard Staff"


def staff(h, count, *, shared_name=False):
    """Synthetic people in the active department, each with a work email and no sign-in."""
    ids = []
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        category_id = db.scalar(select(m.Category.id).where(m.Category.department_id == department.id).limit(1))
        for index in range(count):
            person = m.Person(organisation_id=department.organisation_id,
                              display_name=STAFF if shared_name else f"{STAFF} {index + 1}",
                              work_email=f"onboard.staff{index + 1}@example.test")
            db.add(person)
            db.flush()
            ids.append(person.id)
            db.add(m.Membership(department_id=department.id, category_id=category_id, person_id=person.id,
                                starts_on=date(2020, 1, 1)))
    return ids


def bulk(panel):
    return panel.get_by_role("toolbar", name="Bulk actions")


def open_panel(page):
    navigate(page, "access/people")
    page.get_by_role("button", name="Onboard staff", exact=True).click()
    panel = page.get_by_role("region", name=re.compile("^Onboard staff"))
    expect(panel).to_be_visible()
    return panel


@pytest.mark.parametrize("viewport,scheme,dismiss", [
    pytest.param(None, "light", False, id="None-light"),
    pytest.param({"width": 390, "height": 844}, "dark", False, id="viewport1-dark"),
    pytest.param(None, "light", True, id="escape-during-refresh"),
])
def test_preview_create_download_once_then_resend(users_file, browser, live_server, h, viewport, scheme, dismiss):  # noqa: F811
    person_ids = staff(h, 3, shared_name=True)
    first, second = "onboard.staff1", "onboard.staff2"
    recipients = {person_ids[0]: first, person_ids[1]: second}
    labels = {f"{STAFF} ({username})": username for username in recipients.values()}
    context, page = sign_in(browser, live_server, developer(h), viewport=viewport)
    with closing(context), holding(page) as held:
        pulse = "**/api/pulse"
        if dismiss:
            # Isolate Done's refresh from the live update triggered by onboarding.
            page.route("**/api/events*", lambda route: route.abort())
            page.reload()
            expect_loaded(page, "home")
            held.hold(pulse)
        page.emulate_media(color_scheme=scheme)
        panel = open_panel(page)
        for person_id in recipients:
            panel.locator(f'[data-row="{person_id}"]').get_by_role("checkbox", name=f"Select {STAFF}", exact=True).check()
        bulk(panel).get_by_role("button", name="Preview", exact=True).click()
        # The batch summary, not the result table's own row count (also a status).
        expect(panel.get_by_role("status").filter(has_text=re.compile(r"^2 to create$"))).to_be_visible()
        expect(panel.get_by_text("Select up to", exact=False)).to_have_count(0)
        for person_id, username in recipients.items():
            row = panel.locator(f'[data-row="{person_id}"]')
            expect(row.get_by_text(f"{STAFF} ({username})", exact=True)).to_be_visible()
        create = panel.get_by_role("button", name="Create 2 accounts", exact=True)
        create.focus()
        page.keyboard.press("Enter")  # armed: the first press asks, the same button confirms
        expect(panel.get_by_role("button", name="Confirm? Create 2 accounts", exact=True)).to_be_focused()
        with page.expect_download() as pending:
            page.keyboard.press("Enter")
        saved = pending.value
        assert saved.suggested_filename == "staff-invitations.csv"
        rows = list(csv.reader(io.StringIO(Path(saved.path()).read_text(encoding="utf-8-sig"))))
        assert rows[0] == ["Person", "One-time link"]
        assert sorted(r[0] for r in rows[1:]) == sorted(labels)
        invitation_url = re.compile(re.escape(live_server) + r"/invite/([A-Za-z0-9_-]{43})")
        def link_info(token):
            return context.request.get(live_server + "/api/account-links/" + token)

        tokens = {}
        for label, link in rows[1:]:
            match = invitation_url.fullmatch(link)
            assert match is not None, "The downloaded invitation must use the configured public origin"
            username = labels[label]
            tokens[username] = match.group(1)
            info = link_info(tokens[username])
            assert info.status == 200
            assert info.json()["username"] == username
        expect(panel.get_by_text("Links downloaded for 2 people.", exact=True)).to_be_visible()
        expect(panel.get_by_role("status").filter(has_text=re.compile(r"^2 created$"))).to_be_visible()
        # The links are only in the file: never on the page, in storage, or offered for a second download.
        for _, link in rows[1:]:
            assert link not in page.content()
            assert link not in page.evaluate("JSON.stringify({...localStorage})+JSON.stringify({...sessionStorage})")
        expect(panel.get_by_role("button", name=re.compile("^(Create|Confirm|Download)"))).to_have_count(0)
        access = "**/api/access"
        held.hold(access)
        done = panel.get_by_role("button", name="Done", exact=True)
        done.click()
        wait_held(page, held[access])
        expect(done).to_be_disabled()
        if dismiss:
            page.keyboard.press("Escape")
            expect(panel).to_be_hidden()
            more = page.get_by_role("button", name=re.compile(r"^More actions for ")).first
            more.click()
            menu = page.get_by_role("menu", name="Row actions", exact=True)
            expect(menu).to_be_visible()
        held.release(access)
        if dismiss:
            expect_loaded(page, "access/people")
            expect(menu).to_be_visible()
            page.keyboard.press("Escape")
            expect(menu).to_be_hidden()
            held.release(pulse)
            reconnect(page)
        expect(panel).to_be_hidden()

        # The existing per-person Resend gives that one person a new link and retires the downloaded one.
        page.get_by_role("button", name=f"More actions for {STAFF} {first}", exact=True).click()
        page.get_by_role("menuitem", name="Resend invitation", exact=True).click()
        shown = page.get_by_role("dialog", name="Invitation link", exact=True)
        resent = shown.get_by_label("One-time link", exact=True)
        expect(resent).to_have_value(re.compile(r"/invite/[A-Za-z0-9_-]{43}$"))
        token = resent.input_value().rsplit("/", 1)[1]
        assert token not in tokens.values()
        replacement = link_info(token)
        assert [replacement.status, link_info(tokens[first]).status, link_info(tokens[second]).status] == [200, 404, 200]
        assert replacement.json()["username"] == first


def test_selection_over_the_batch_cap_says_why(users_file, browser, live_server, h):  # noqa: F811
    staff(h, WAVE + 1)
    context, page = sign_in(browser, live_server, developer(h))
    sent = []
    page.on("request", lambda r: sent.append(r.url) if "/api/account-onboarding/" in r.url else None)
    try:
        panel = open_panel(page)
        panel.get_by_role("searchbox", name="Search people").fill(STAFF)
        panel.get_by_role("checkbox", name="Select all people shown", exact=True).check()
        expect(panel.get_by_text("Select up to", exact=False)).to_have_count(0)  # the reason shows only once the cap is hit
        bulk(panel).get_by_role("button", name="Preview", exact=True).click()
        expect(panel.get_by_text(f"Select up to {WAVE} people per batch.", exact=True)).to_be_visible()
        assert sent == []
    finally:
        context.close()

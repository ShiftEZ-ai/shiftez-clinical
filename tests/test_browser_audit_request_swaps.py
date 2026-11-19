"""Request paging and Person filters must retain swaps hidden by neither authorization nor expiry."""
import re
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from packages.domain.time import now, pin_clock
from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import swaps
from tests.outbox_support import process_one_after_undo as process_one
from .governance_support import identity
from .test_browser import browser, live_server, sign_in, navigate, filter_by, expect_loaded, whole_list, wait_held  # noqa: F401
from .test_browser_requests import PHONE, published, rows
from .test_browser_rule_catalogue import shot

pytestmark = pytest.mark.browser


def offer_payload(h):
    identity(h, "staff_two", ["staff"], h.seed["people"][1])
    publication = published(h, days=2)
    assignment = h.get("staff", "/api/swaps/options")["assignments"][0]
    return {"publication_id": publication, "assignment_id": assignment["assignment_id"],
            "recipient_person_id": h.seed["people"][1], "expires_at": (now() + timedelta(days=1)).isoformat(),
            "reason": "Audit swap invitation"}


def test_expired_swap_after_an_empty_server_page_is_reachable(browser, live_server, h):
    payload = offer_payload(h)
    # Create valid consent in the past, then restore the clock; consent fields stay immutable.
    present = now()
    pin_clock(present - timedelta(minutes=2))
    try:
        expired = h.command("staff", "swap.offer", {
            **payload, "expires_at": (present - timedelta(minutes=1)).isoformat()})["swap"]
    finally:
        pin_clock(present)
    # Historical duplicate offers remain readable; new commands refuse duplicates.
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([m.SwapRequest(department_id=h.seed['department_id'], publication_id=payload['publication_id'],
            proposer_id=h.seed['identities']['staff'], proposer_person_id=h.seed['people'][0],
            recipient_person_id=h.seed['people'][1], first_assignment_id=payload['assignment_id'],
            expires_at=now() + timedelta(days=1), reason='Synthetic historical offer') for _ in range(100)])
    first = h.get("coordinator", "/api/requests/all?type=swap&status=expired&limit=100&offset=0")
    following = h.get("coordinator", "/api/requests/all?type=swap&status=expired&limit=100&offset=100")
    assert not first["items"] and first["has_more"]
    assert [item["id"] for item in following["items"]] == [expired["id"]]
    assert not following["has_more"]

    context, page = sign_in(browser, live_server, "coordinator")
    try:
        page.evaluate("location.hash='requests?type=swap&status=expired'")
        expect_loaded(page, "requests")
        expect(rows(page)).to_have_count(0)
        page.get_by_role("button", name="Show more", exact=True).click()
        expect(rows(page)).to_have_count(1)
        expect(rows(page)).to_have_attribute("data-row", expired["id"])
        expect(rows(page)).to_contain_text("Expired")
        expect(page.get_by_role("button", name="Show more", exact=True)).to_have_count(0)
    finally:
        context.close()


def test_person_filter_keeps_a_swap_received_by_that_person(browser, live_server, h):
    swap = h.command("staff", "swap.offer", offer_payload(h))["swap"]
    item = h.get("coordinator", "/api/requests/all?person_ids=" + h.seed["people"][1])["items"][0]
    assert item["id"] == swap["id"] and item["proposer_person_id"] != item["recipient_person_id"]
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "requests")
        whole_list(page)  # the swap waits for a colleague, not for this coordinator (Waiting for you, maintenance)
        filter_by(page, "Person", [item["recipient_name"]])
        expect(rows(page)).to_have_count(1)
        expect(rows(page)).to_have_attribute("data-row", swap["id"])
        expect(rows(page).locator('td[data-col=status] .badge')).to_have_text('Waiting for ' + item['recipient_name'])
        filter_by(page, 'Status', ['Waiting for colleague'])
        expect(rows(page)).to_have_count(1)
        # Waiting for colleague is a built-in status view: the views button names it, Filter counts the person only (X-01).
        # F-C2: the list's own view rules are counted after the person's ("1 active, N from the view").
        expect(page.get_by_role('button', name=re.compile(rf'^Filter: {re.escape(item["recipient_name"])} \(1 active(, \d+ from the view)?\)$'))).to_be_visible()
        # Wait for the status read before clicking the row this filter returned.
        expect(page.locator("#main .requests .dt-body")).to_have_attribute("aria-busy", "false")
        rows(page).locator("td[data-col=type]").click()
        panel = page.get_by_role("region", name=f'{item["proposer_name"]} and {item["recipient_name"]}', exact=True)
        expect(panel).to_contain_text("Audit swap invitation")
        page.reload()
        expect_loaded(page, "requests")
        expect(rows(page)).to_have_count(1)
        expect(rows(page)).to_have_attribute("data-row", swap["id"])
    finally:
        context.close()


def test_clicking_a_request_while_its_list_is_reading_opens_it_afterwards(browser, live_server, h):
    swap = h.command("staff", "swap.offer", offer_payload(h))["swap"]
    context, page = sign_in(browser, live_server, "coordinator")
    held = []

    def status_read(url):
        path = urlsplit(url)
        return path.path == "/api/requests/all" and parse_qs(path.query).get("status") == ["reply"]

    try:
        navigate(page, "requests")
        whole_list(page)
        body = page.locator("#main .requests .dt-body")
        row = rows(page).and_(page.locator(f'[data-row="{swap["id"]}"]'))
        expect(row).to_be_visible()
        page.route(status_read, lambda route: held.append(route))
        filter_by(page, "Status", ["Waiting for colleague"])
        wait_held(page, held)
        expect(body).to_have_attribute("aria-busy", "true")
        row.locator("td[data-col=type]").click()
        panel = page.locator(".request-panel")
        expect(panel).to_have_count(0)
        held.pop().continue_()
        expect(body).to_have_attribute("aria-busy", "false")
        expect(panel).to_have_count(1)
        expect(panel).to_contain_text("Audit swap invitation")
    finally:
        for route in held:
            route.continue_()
        context.close()


@pytest.mark.parametrize("kind,viewport,theme", [("exchange", None, "light"), ("give", PHONE, "dark")])
def test_open_offer_shows_audience_pending_until_worker_finishes(browser, live_server, h, kind, viewport, theme):
    payload = offer_payload(h)
    payload.pop("recipient_person_id")
    swap = h.command("staff", "swap.offer", {**payload, "kind": kind})["swap"]
    assert swap["audience_pending"] is True
    context, page = sign_in(browser, live_server, "staff", viewport)
    try:
        page.evaluate("t => localStorage.setItem('rota.prefs', JSON.stringify({theme:t}))", theme)
        page.reload()
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        navigate(page, "requests?type=swap")
        listed = page.locator("#main .dt-card" if viewport else "#main tr.dt-open").first
        expect(listed.locator(".badge")).to_have_text("Finding colleagues…")
        expect(listed).to_contain_text("Checking eligibility before notifying colleagues")
        listed.locator(".dt-card-title" if viewport else "td[data-col=type]").click()
        panel = page.locator(".request-status")
        expect(panel.locator(".badge")).to_have_text("Finding colleagues…")
        if kind == "give":
            expect(page.locator(".request-panel")).not_to_contain_text("a duty in return")
        shot(page, f"swap-audience-{kind}-pending")
        for _ in range(30):
            if not process_one(h.app.state.engine):
                break
        assert h.get("staff", "/api/swaps/" + swap["id"])["swap"]["audience_pending"] is False
        expect(listed.locator(".badge")).to_have_text("Waiting for a colleague", timeout=30000)
        expect(panel.locator(".badge")).to_have_text("Waiting for a colleague")
        expect(panel).to_contain_text("accept this duty" if kind == "give" else "offer a duty in return")
        shot(page, f"swap-audience-{kind}-ready")
        h.command("staff", "swap.withdraw", {"reason": "Close synthetic open offer"}, swap["id"], swap["revision"])
        page.reload()
        expect_loaded(page, "requests")
        expect(listed.locator(".badge")).to_have_text("Withdrawn")
        expect(page.locator("#main")).not_to_contain_text("Finding colleagues…")
    finally:
        context.close()


def test_open_offer_audience_failure_shows_recovery_without_reloading(browser, live_server, h, monkeypatch):
    payload = offer_payload(h)
    payload.pop("recipient_person_id")
    swap = h.command("staff", "swap.offer", payload)["swap"]
    context, page = sign_in(browser, live_server, "staff")
    try:
        navigate(page, "requests?type=swap")
        listed = rows(page).first
        expect(listed.locator(".badge")).to_have_text("Finding colleagues…")
        listed.locator("td[data-col=type]").click()
        panel = page.locator(".request-status")
        expect(panel.locator(".badge")).to_have_text("Finding colleagues…")
        def fail_audience(db, inputs):
            raise RuntimeError("Synthetic eligibility worker failure")
        monkeypatch.setattr(swaps, "compute_audience", fail_audience)
        for _ in range(30):
            if not process_one(h.app.state.engine):
                break
        item = h.get("staff", "/api/swaps/" + swap["id"])["swap"]
        assert item["audience_pending"] is False and item["audience_failed"] is True
        expect(listed.locator(".badge")).to_have_text("Could not find colleagues", timeout=30000)
        expect(panel).to_contain_text("Withdraw this offer and offer it again to retry")
        expect(page.locator("#main")).not_to_contain_text("Finding colleagues…")
        shot(page, "swap-audience-failed")
    finally:
        context.close()

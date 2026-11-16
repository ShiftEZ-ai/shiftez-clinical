"""Moving between pages keeps no earlier page alive (SH-01): node and listener counts after garbage collection stay flat
over repeated round trips, and the app frame (sidebar, top bar, footer) is built once (SH-07)."""
from datetime import datetime, timedelta, timezone
import pytest
from apps.api import models as m
from apps.api.db import transaction
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, expect_loaded

pytestmark = pytest.mark.browser


def metrics(page, cdp):
    # Skeleton timers (Retry at 15 s) end by themselves: run the page clock past them first.
    page.clock.run_for(16000)
    cdp.send("HeapProfiler.collectGarbage")
    cdp.send("HeapProfiler.collectGarbage")
    values = {m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]}
    return values["Nodes"], values["JSEventListeners"]


def go(page, route):
    # A sidebar click, as a person moves (the clicked nav button holds focus while the next page builds).
    page.locator(f"#department-nav [data-route={route}]").click()
    expect_loaded(page, route)


@pytest.mark.cpu_budget
def test_navigation_does_not_retain_previous_pages(browser, live_server, h):
    # Department scale (as the Requests list test): 150 people, 20 requests each; the list draws its first 100.
    with transaction(h.app.state.engine, write=True) as db:
        made = [m.Person(organisation_id=h.seed["organisation_id"], display_name=f"Scale person {n:03d}") for n in range(150 - len(h.seed["people"]))]
        db.add_all(made)
        db.flush()
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for n, pid in enumerate([*h.seed["people"], *(p.id for p in made)]):
            for k in range(20):
                at = start + timedelta(days=41 * k + n % 30)
                db.add(m.RequestRecord(department_id=h.seed["department_id"], person_id=pid, kind="leave", start=at, end=at + timedelta(days=1),
                                       private_reason="Scale fixture", status="approved", created_by=h.seed["identities"]["coordinator"]))
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        cdp = context.new_cdp_session(page)
        cdp.send("Performance.enable")
        expect_loaded(page, "home")
        go(page, "requests"); go(page, "home")  # warm up: lazy modules, caches
        nodes, listeners = metrics(page, cdp)
        for _ in range(10):
            go(page, "requests"); go(page, "home")
        after_nodes, after_listeners = metrics(page, cdp)
        # Before the fix each round trip kept ~5,000 nodes and ~590 listeners at department scale (hundreds at seed).
        print("navigation retained", {"nodes": after_nodes - nodes, "listeners": after_listeners - listeners})
        assert after_nodes - nodes < 1500, (nodes, after_nodes)
        assert after_listeners - listeners < 200, (listeners, after_listeners)
    finally:
        context.close()


def test_the_app_frame_is_built_once(browser, live_server, h):
    """Sidebar, top bar and footer stay the same nodes across pages; the nav mark, title and page actions follow the page."""
    context, page = sign_in(browser, live_server, "coordinator", {"width": 1440, "height": 900})
    try:
        expect_loaded(page, "home")
        page.evaluate("() => {window.__frame = ['.sidebar', '.topbar', '.app-footer'].map(s => document.querySelector(s));}")
        go(page, "people")
        expect(page.locator("#department-nav [aria-current=page]")).to_have_attribute("data-route", "people")
        expect(page.locator(".topbar h1")).to_have_text("People")
        expect(page.locator(".topbar .page-actions").get_by_role("button", name="New person")).to_be_visible()
        go(page, "requests")
        expect(page.locator("#department-nav [aria-current=page]")).to_have_count(1)
        expect(page.locator("#department-nav [aria-current=page]")).to_have_attribute("data-route", "requests")
        expect(page.locator(".topbar .page-actions").get_by_role("button", name="New person")).to_have_count(0)
        go(page, "schedule")
        go(page, "home")
        expect(page.locator(".topbar .planner-bar")).to_have_count(0)  # the Schedule's bar went with it
        assert page.evaluate("() => ['.sidebar', '.topbar', '.app-footer'].every((s, i) => document.querySelector(s) === window.__frame[i])")
    finally:
        context.close()

"""Actual permitted-browser operator pagination/filter/retry, not a DOM simulation."""
from datetime import timedelta
import re
import pytest
from playwright.sync_api import expect
from apps.api.db import transaction
from apps.api import models as m
from .test_operations_observability import outbox, NOW
from .test_browser import browser, condition, holding, live_server, sign_in, navigate, save_dialog, wait_held
from .test_browser_operations_history_query import caption

pytestmark = pytest.mark.browser


def test_operator_can_find_old_failed_event_and_retry_from_ui(browser, live_server, h):
    with transaction(h.app.state.engine, write=True) as db:
        for _ in range(105): outbox(db, h.seed["department_id"], "available_in_app")
        outbox(db, h.seed["department_id"], "failed", created=NOW-timedelta(days=1))
    context, page = sign_in(browser, live_server, "operator")
    errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        navigate(page,"system/operations")
        expect(page.get_by_role("heading", name="Queue observations", exact=True)).to_be_visible()
        page.get_by_role("button", name="Next events", exact=True).click()
        caption(page, "Showing 101–106 of 106")
        # One way to filter: the table's own State filter, sent to the server so older pages are searched too.
        expect(page.get_by_label("Outbox state", exact=True)).to_have_count(0)
        page.get_by_role("button", name="Filter", exact=True).click()
        from .test_browser import condition
        condition(page.locator(".dt-pop"), "State", ["failed"])
        page.keyboard.press("Escape")
        caption(page, "Showing 1–1 of 1")
        page.get_by_role("button", name="Retry", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Reason", exact=True).fill("Explicit fictional operator retry")
        save_dialog(dialog, "Confirm")
        caption(page, "Showing 0–0 of 0")
        # The retry gives the worker work, so Operations re-reads every 10 s (app.ts opsBusy) and a redraw can move the
        # Filter popover under the click. A hidden tab skips only that periodic check; Clear all still reads the list.
        page.evaluate("() => Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'})")
        # The filter that emptied the list stays visible, so it can be cleared.
        page.get_by_role("button", name=re.compile("^Filter: failed")).click()  # active filters live in the Filter popover
        page.get_by_role("button", name="Clear all filters", exact=True).click()  # its rule is a builder row; Clear all drops it
        caption(page, "Showing 1–100 of 106")
        assert not errors
    finally:
        context.close()



def test_operations_follows_the_worker_only_while_it_has_work(browser, live_server, h):
    with transaction(h.app.state.engine, write=True) as db:
        waiting = outbox(db, h.seed["department_id"], "queued")
    context, page = sign_in(browser, live_server, "operator")
    reads = []
    page.on("requestfinished", lambda r: reads.append(r.url) if "/api/operations?" in r.url else None)
    try:
        # A controllable clock, installed before the app starts its 10-second check, instead of real waiting.
        page.clock.install()
        page.reload()
        navigate(page,"system/operations")
        ops = lambda r: "/api/operations?" in r.url
        # A notification is waiting to go out: the page follows it.
        with page.expect_request_finished(ops):
            page.clock.run_for(25_000)
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Outbox, waiting).state = "available_in_app"
        # The next read finds the worker idle; from then on the page waits for the pulse instead of re-reading.
        with page.expect_request_finished(ops):
            page.clock.run_for(25_000)
        expect(page.locator("#main")).to_have_attribute("data-loaded-view", "system")
        page.wait_for_timeout(500)  # let that redraw settle before counting
        idle = len(reads)
        page.clock.run_for(65_000)
        page.wait_for_timeout(500)
        assert len(reads) == idle
    finally:
        context.close()


def test_open_outbox_filter_keeps_clear_button_during_worker_poll(browser, live_server, h):
    with transaction(h.app.state.engine, write=True) as db:
        outbox(db, h.seed["department_id"], "queued")
        outbox(db, h.seed["department_id"], "failed")
    context, page = sign_in(browser, live_server, "operator")
    try:
        page.clock.install()
        page.reload()
        navigate(page, "system/operations")
        caption(page, "Showing 1–2 of 2")
        operations = "**/api/operations?*"
        clear = page.get_by_role("button", name="Clear all filters", exact=True)
        filtered = page.get_by_role("button", name=re.compile("^Filter: failed"))
        with holding(page) as held:
            held.hold(operations)
            page.get_by_role("button", name="Filter", exact=True).click()
            condition(page.locator(".dt-pop"), "State", ["failed"])
            wait_held(page, held[operations])
            before = clear.element_handle()
            held.release(operations)
            caption(page, "Showing 1–1 of 1")
            assert clear.evaluate("(node, before) => node === before && before.isConnected", before)

            held.hold(operations)
            page.keyboard.press("Escape")
            page.clock.fast_forward(10_001)
            wait_held(page, held[operations])
            # Open after the poll starts reading the page, so it has passed its first check for an open popover.
            filtered.click()
            anchor, before = filtered.element_handle(), clear.element_handle()
            poll_url = held[operations][0].request.url
            with page.expect_request_finished(lambda request: request.url == poll_url):
                held.release(operations)
            expect(page.locator("#main")).to_have_attribute("data-loaded-view", "system")
            page.clock.fast_forward(10_001)
            assert filtered.evaluate("(node, anchor) => node === anchor && anchor.isConnected", anchor)
            assert clear.evaluate("(node, before) => node === before && before.isConnected", before)
            before.click()
            caption(page, "Showing 1–2 of 2")
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_operations_shows_only_what_is_queued_and_an_empty_outbox_plainly(browser, live_server, h, width, scheme):
    """F13 A-20/A-21/A-35: no rows of zero pills, no "Showing 0–0 of 0", a plain empty outbox; at 1440 the five health
    tiles share one row."""
    context, page = sign_in(browser, live_server, 'operator', viewport={'width': width, 'height': 900}, color_scheme=scheme)
    try:
        navigate(page, 'system/operations')
        queues = page.locator('.ops-queue')
        expect(queues.first).to_be_visible()
        for i in range(queues.count()):
            queue = queues.nth(i)
            states = queue.locator('.ops-state')
            if states.count() == 0:
                expect(queue).to_contain_text('Nothing queued')
            for count in states.locator('b').all_inner_texts():
                assert count != '0'
        main = page.locator('#main')
        expect(main).not_to_contain_text('matching events')
        expect(main.get_by_role('heading', name='No notifications waiting', exact=True)).to_be_visible()
        if width == 1440:
            tops = page.locator('.ops-tile').evaluate_all('ts => ts.map(t => Math.round(t.getBoundingClientRect().top))')
            assert len(tops) == 5 and len(set(tops)) == 1, tops
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        context.close()

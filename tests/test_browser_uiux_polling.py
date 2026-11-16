"""Inbox background checks keep unchanged UI stable and notice equal-count changes."""
import re
import time
import uuid
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .test_browser import route_fetch, browser, live_server, sign_in, navigate, fill_sign_in, reconnect  # noqa: F401
from .test_browser_uiux_sessions import switch_account

pytestmark = pytest.mark.browser


def notification(db, h, recipient, title):
    event = m.Outbox(department_id=h.seed['department_id'], event_key=str(uuid.uuid4()),
                     kind='announcement', payload={}, state='available_in_app', next_attempt_at=m.now())
    db.add(event)
    db.flush()
    item = m.Notification(outbox_id=event.id, identity_id=recipient, title=title, message='Fictional polling check')
    db.add(item)
    db.flush()
    return item.id


def test_idle_inbox_preserves_ui_and_receives_changed_payload_with_same_unread_count(browser, live_server, h):
    recipient = h.get('coordinator', '/api/me')['identity_id']
    with transaction(h.app.state.engine, write=True) as db:
        original = notification(db, h, recipient, 'Original polling notice')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        # The first timer starts both real SSE and fallback polling. Observe that
        # connection handshake before measuring a steady background cycle.
        cdp=context.new_cdp_session(page)
        cdp.send('Network.enable')
        initial_pulse=[];startup_reads=[];pending=set()
        cdp.on('Network.eventSourceMessageReceived',lambda event: initial_pulse.append(event) if event['eventName']=='pulse' else None)
        def started(request):
            if request.method=='GET' and urlsplit(request.url).path=='/api/notifications':
                startup_reads.append(request);pending.add(request)
        page.on('request',started)
        page.on('requestfinished',lambda request: pending.discard(request))
        navigate(page, 'inbox')
        expect(page.get_by_text('Original polling notice', exact=True)).to_be_visible()
        original_node = page.locator('.note-item').first.element_handle()
        with page.expect_response(lambda response: urlsplit(response.url).path == '/api/pulse', timeout=15000) as startup:
            page.clock.run_for(11000)  # the pulse is the app's 10 s timer, on the page clock
        startup.value.finished()
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            page.wait_for_timeout(50)
            if initial_pulse and startup_reads and not pending:
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                if not pending:break
        else:pytest.fail('Initial SSE pulse and notification reads did not complete')
        # Idle with the live stream up (SH-04): the Inbox follows the stream like every page, so two timer ticks right
        # after a ping (the stream is alive) read nothing, where it used to read the pulse and the inbox every 10 s.
        pinged = []
        cdp.on('Network.eventSourceMessageReceived', lambda event: pinged.append(event) if event['eventName'] == 'ping' else None)
        deadline = time.monotonic() + 30
        while not pinged and time.monotonic() < deadline:
            page.evaluate('() => new Promise(resolve => requestAnimationFrame(resolve))')
        assert pinged, 'no ping from the live stream'
        cdp.detach()
        reads = []
        page.on('request', lambda request: reads.append(urlsplit(request.url).path) if request.method == 'GET' else None)
        page.clock.run_for(20_000)
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        periodic = [path for path in reads if path != '/api/events']
        assert periodic.count('/api/notifications') <= 1 and periodic.count('/api/pulse') <= 1, periodic
        assert '/api/me' not in reads and '/api/catalogues' not in reads, reads
        assert original_node.evaluate('(node) => node.isConnected'), 'An idle check replaced the inbox'

        # Equal unread counts do not mean equal inboxes: another client acknowledges one and receives another. The
        # minute's check (not a pushed pulse) still reads the inbox and shows it.
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Notification, original).acknowledged_at = m.now()
            notification(db, h, recipient, 'Replacement polling notice')
        page.clock.run_for(60_000)
        expect(page.get_by_text('Replacement polling notice', exact=True)).to_be_visible(timeout=15000)
        expect(page.get_by_text('Original polling notice', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_pending_badges_release_their_loading_slot_after_same_account_refresh(browser, live_server, h):
    recipient = h.get('coordinator', '/api/me')['identity_id']
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    try:
        navigate(page,'home')
        # Hold an actual server reply, then let another ordinary reconnect refresh replace /api/me.
        page.route('**/api/notifications', lambda route: held.append((route, route_fetch(route))))
        previous = page.locator('#main').element_handle()
        # The reconnect reads the inbox badge when the pulse moved (as the third reconnect below): a notice arrives first.
        with transaction(h.app.state.engine, write=True) as db:
            notification(db, h, recipient, 'Notice before the first badge refresh')
        with page.expect_request(lambda request: urlsplit(request.url).path == '/api/notifications'):
            reconnect(page)
        page.wait_for_function('old => !old.isConnected', arg=previous)
        # Overview awaits the same non-notification reads as the badge loader.
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home')
        # Receiving the request does not mean route.fetch has finished under concurrent load.
        deadline = time.monotonic() + 10
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert len(held) == 1
        previous = page.locator('#main').element_handle()
        with page.expect_response(lambda response: urlsplit(response.url).path == '/api/me'):
            reconnect(page)
        page.wait_for_function('old => !old.isConnected', arg=previous)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home')
        route, response = held.pop()
        with page.expect_response(lambda reply: urlsplit(reply.url).path == '/api/notifications') as delivered:
            route.fulfill(response=response)
        delivered.value.finished()
        page.unroute('**/api/notifications')
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        with transaction(h.app.state.engine, write=True) as db:
            notification(db, h, recipient, 'Notice after overlapping badge refresh')
        # A third reconnect must start a fresh badge read, rather than leave badgesLoading locked forever.
        with page.expect_request(lambda request: urlsplit(request.url).path == '/api/notifications', timeout=5000):
            reconnect(page)
        expect(page.locator('.topbar .bell')).to_have_attribute('aria-label', 'Inbox · 2 unread')  # the unread count sits on the top-bar bell
    finally:
        context.close()


def test_a_deferred_inbox_pulse_does_not_reboot_the_next_accounts_page(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'inbox')
        page.get_by_role('button', name='New announcement', exact=True).click()
        expect(page.get_by_role('dialog')).to_be_visible()
        # A real pulse is deferred while this person's form is open.
        with page.expect_response(lambda response: urlsplit(response.url).path == '/api/pulse', timeout=15000):
            page.clock.run_for(11000)  # the pulse is the app's 10 s timer, on the page clock
        page.wait_for_timeout(100)
        page.keyboard.press('Escape')
        expect(page.get_by_role('dialog')).to_have_count(0)
        switch_account(page, 'staff')
        reads = []
        page.on('request', lambda request: reads.append(urlsplit(request.url).path) if request.method == 'GET' else None)
        navigate(page, 'inbox')  # the inbox reads the pulse on every timer tick
        current = page.locator('#main').element_handle()
        # Wait for the next real timer tick (its pulse read) instead of a fixed sleep: a stale deferred pulse would
        # have been applied on that tick instead of reading a new one.
        with page.expect_response(lambda response: urlsplit(response.url).path == '/api/pulse', timeout=25000):
            page.clock.run_for(11000)  # the tick is the app's 10 s timer, on the page clock
        assert '/api/me' not in reads, 'The former account pulse rebooted the staff session: ' + repr(reads)
        assert current.evaluate('(node) => node.isConnected'), 'The old account replaced the current page'
    finally:
        context.close()


def test_a_page_asked_for_while_signing_in_is_drawn_once_by_the_start(browser, live_server):
    """A link followed before the start has read the live baseline is drawn by the start, not drawn and then replaced."""
    context, page = sign_in(browser, live_server, 'coordinator')
    held, order, holding = [], [], [True]

    def release():
        # Each held read is continued once, here; later reads pass straight through. Unrouting first instead would let
        # Playwright continue the held routes itself, and continuing them again raises "Route is already handled!".
        holding.clear()
        while held:
            held.pop(0).continue_()
    try:
        page.get_by_role('button', name=re.compile(r'^Account menu, ')).click()
        page.get_by_role('menuitem', name='Sign out', exact=True).click()
        expect(page.get_by_label('Username or email', exact=True)).to_be_visible()
        # The start's baseline read waits until released.
        page.route('**/api/pulse', lambda route: held.append(route) if holding else route.continue_())
        page.on('request', lambda request: order.append(urlsplit(request.url).path))
        with page.expect_request(lambda request: urlsplit(request.url).path == '/api/pulse', timeout=10000):
            fill_sign_in(page, 'staff')
            expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible()
        page.evaluate("location.hash = 'inbox'")
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        assert held, 'the start did not read the live baseline'
        released = len(order)
        release()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'inbox')
        drawn = page.locator('#main').element_handle()
        assert '/api/notifications' not in order[:released], 'the inbox was drawn before the start finished: ' + repr(order)
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        assert drawn.evaluate('(node) => node.isConnected'), 'the start replaced the page it had just drawn'
    finally:
        release()
        context.close()

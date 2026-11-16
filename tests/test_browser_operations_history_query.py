"""Whole-history Operations queries through real controls and the API, including late answers and retry authority."""
import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from .governance_support import developer
from .test_browser import route_fetch, browser, live_server, sign_in, navigate, filter_by, toolbar, close_popover, save_dialog, wait_held  # noqa: F401
from .test_operations_history_query import history

pytestmark = pytest.mark.browser


def search_for(page, text):
    box = page.get_by_role('searchbox', name='Search events', exact=True)
    box.fill(text)
    box.press('Enter')


def caption(page, text):
    """'Showing a–b of n': the footer counts a single page; only several server pages say which part is shown."""
    first, last, total = map(int, re.match(r'Showing (\d+)–(\d+) of (\d+)', text).groups())
    if total > 100:
        expect(page.locator('#main .section-head').filter(has_text='Notification outbox')).to_contain_text(f'{first}–{last} of {total}')
    else:
        expect(page.locator('.app-footer .page-status .dt-count').last).to_have_text(f'{total} event' + ('' if total == 1 else 's'))
        expect(page.locator('#main .section-head').filter(has_text='Notification outbox')).not_to_contain_text(' of ')


def clear_filters(page):
    toolbar(page, 'Filter').get_by_role('button', name='Clear all filters', exact=True).click()
    close_popover(page)


@pytest.mark.parametrize('role,width,dark', [('developer', 1440, False), ('operator', 390, True), ('auditor', 1440, True)])
def test_operations_queries_find_old_events_and_preserve_role_actions(browser, live_server, h, role, width, dark):
    role = developer(h) if role == 'developer' else role  # the owner: Admin plus Developer
    ids = history(h)
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 1000})
    try:
        page.emulate_media(color_scheme='dark' if dark else 'light')
        navigate(page, 'system/operations')
        caption(page, 'Showing 1–100 of 107')
        expect(page.get_by_text('Older Beta delivery error', exact=True)).to_have_count(0)
        # This kind is absent from page one but must still be selectable in the common Filter picker.
        filter_by(page, 'Event', ['publication_notice'])
        caption(page, 'Showing 1–1 of 1')
        expect(page.get_by_text(r'Older Alpha 100%_match\path', exact=True)).to_be_visible()
        clear_filters(page)
        caption(page, 'Showing 1–100 of 107')
        filter_by(page, 'State', ['failed', 'retry'])
        caption(page, 'Showing 1–2 of 2')
        expect(page.get_by_text('Older Beta delivery error', exact=True)).to_be_visible()
        search_for(page, 'no matching error')
        caption(page, 'Showing 0–0 of 0')
        expect(page.get_by_role('heading', name='No matching events', exact=True)).to_be_visible()
        search_for(page, 'Older Alpha')
        caption(page, 'Showing 1–1 of 1')
        if role == 'auditor':
            expect(page.get_by_role('button', name='Retry', exact=True)).to_have_count(0)
            h.command(role, 'outbox.retry', {'reason': 'Reader cannot retry'}, ids['failed'], 1, status=403)
        else:
            page.get_by_role('button', name='Retry', exact=True).click()
            dialog = page.get_by_role('dialog', name='Retry notification event', exact=True)
            dialog.get_by_label('Reason', exact=True).fill('Retry the older filtered event')
            save_dialog(dialog, 'Confirm')
            caption(page, 'Showing 0–0 of 0')
            saved = h.get(role, '/api/operations?q=Older%20Alpha')['outbox']
            assert len(saved) == 1 and saved[0]['id'] == ids['failed']
            assert saved[0]['state'] == 'queued' and saved[0]['revision'] == 2
        search_for(page, '')
        clear_filters(page)
        caption(page, 'Showing 1–100 of 107')
        page.get_by_role('button', name='Next events', exact=True).click()
        caption(page, 'Showing 101–107 of 107')
        # Search resets the server offset rather than searching only the visible older page.
        search_for(page, 'Completed event 50')
        caption(page, 'Showing 1–1 of 1')
        expect(page.get_by_text('Completed event 50', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Next events', exact=True)).to_be_hidden()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


def test_operations_newer_query_owns_rows_and_failed_query_can_retry(browser, live_server, h):
    history(h)
    context, page = sign_in(browser, live_server, 'operator')
    held = []
    try:
        navigate(page, 'system/operations')

        def delay_alpha(route):
            query = parse_qs(urlparse(route.request.url).query)
            if query.get('q') == ['Older Alpha']:
                held.append((route, route_fetch(route)))
            else:
                route.continue_()

        page.route('**/api/operations?*', delay_alpha)
        search_for(page, 'Older Alpha')
        wait_held(page, held)
        search_for(page, 'Older Beta')
        caption(page, 'Showing 1–1 of 1')
        expect(page.get_by_text('Older Beta delivery error', exact=True)).to_be_visible()
        route, response = held.pop()
        with page.expect_request_finished(lambda request: parse_qs(urlparse(request.url).query).get('q') == ['Older Alpha']):
            route.fulfill(response=response)
        page.evaluate('() => new Promise(requestAnimationFrame)')
        expect(page.get_by_text('Older Beta delivery error', exact=True)).to_be_visible()
        expect(page.get_by_text(r'Older Alpha 100%_match\path', exact=True)).to_have_count(0)
        page.unroute('**/api/operations?*', delay_alpha)

        page.route('**/api/operations?*', lambda route: route.abort())
        search_for(page, 'no matching error')
        expect(page.get_by_role('alert')).to_contain_text('Couldn’t load events.')
        expect(page.get_by_text('Older Beta delivery error', exact=True)).to_be_visible()
        page.unroute('**/api/operations?*')
        page.get_by_role('button', name='Retry loading events', exact=True).click()
        caption(page, 'Showing 0–0 of 0')
        expect(page.get_by_role('alert')).to_have_count(0)
        expect(page.get_by_role('heading', name='No matching events', exact=True)).to_be_visible()
        search_for(page, '')
        caption(page, 'Showing 1–100 of 107')
    finally:
        for route, response in held:
            route.fulfill(response=response)
        context.close()


def test_a_live_update_read_while_a_filter_opens_does_not_redraw_it_away(browser, live_server, h):
    """Operations follows the worker every pulse while it has work. A filter opened while that pulse's refresh was
    still reading stays open: the redraw waits until it is closed (maintenance: the popover was pulled from under the test)."""
    history(h)
    context, page = sign_in(browser, live_server, developer(h))  # Admin + Developer: reads Operations and the requests badge
    try:
        navigate(page, 'system/operations')
        caption(page, 'Showing 1–100 of 107')
        held = []
        page.route('**/api/me', lambda route: held.append(route) if not held else route.continue_())
        wait_held(page, held)  # the pulse's refresh has started reading
        panel = toolbar(page, 'Filter')
        node = panel.element_handle()
        reads = []
        page.on('request', lambda request: reads.append(request.url) if '/api/operations?' in request.url else None)
        # Its read finishes with the filter open; a redraw would read the page while the nav badges it reloads come in.
        with page.expect_response(lambda response: '/api/requests/all?' in response.url):
            held[0].continue_()
        page.evaluate('() => new Promise(done => requestAnimationFrame(done))')
        assert reads == [] and node.evaluate('n => n.isConnected'), 'the page was redrawn under the open filter'
        expect(panel).to_be_visible()
        # Closed, the waiting update is drawn.
        with page.expect_request(lambda request: '/api/operations?' in request.url):
            close_popover(page)
    finally:
        context.close()

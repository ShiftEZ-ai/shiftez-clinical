"""Global Requests sorting through shared controls, paging, saved views and failed/superseded reads."""
from urllib.parse import parse_qs, urlparse
import re

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .test_requests_search import request_row
from .test_browser import whole_list
from .test_browser import route_fetch, browser, live_server, sign_in, navigate, expect_loaded, toolbar, close_popover  # noqa: F401

pytestmark = pytest.mark.browser


def history(h):
    with transaction(h.app.state.engine, write=True) as db:
        records = [request_row(h, h.seed['people'][0], 'Sort history', day=n,
                               kind='meeting' if n % 2 else 'leave',
                               status='approved' if n % 3 == 0 else 'submitted') for n in range(220)]
        db.add_all(records)
        db.flush()
        return [row.id for row in records]


def choose(page, control, label):
    panel = toolbar(page, control)
    if label == 'No grouping':
        # Group is the levels editor (3eb3ec9f3): a grouping is removed with Remove grouping; ungrouped, the start
        # menu lists only fields and ticks none.
        if panel.locator('.dt-group-list .dt-sort-row').count():
            panel.get_by_role('button', name='Remove grouping', exact=True).click()
        expect(panel.locator('.dt-group-list .dt-sort-row')).to_have_count(0)
        expect(panel.get_by_role('menu', name='Group by', exact=True).get_by_role('menuitemradio', checked=True)).to_have_count(0)
        return close_popover(page)
    # Group options include the visible "default" badge in their accessible name, depending on the role.
    menu = panel.get_by_role('menu', name=control + ' by', exact=True)
    menu.get_by_role('menuitemradio', name=re.compile(r'^' + re.escape(label) + r'(?: default)?$')).click()


def ids(page):
    return page.locator('#main .dt-body [data-row]').evaluate_all('nodes=>nodes.map(n=>n.dataset.row)')


def expect_ids(page, wanted):
    expect(page.locator('#main .dt-body [data-row]')).to_have_count(len(wanted))
    page.wait_for_function('(wanted)=>JSON.stringify([...document.querySelectorAll("#main .dt-body [data-row]")].map(n=>n.dataset.row))===JSON.stringify(wanted)', arg=wanted)


def sorted_response(response, key, direction):
    q = parse_qs(urlparse(response.url).query)
    return urlparse(response.url).path == '/api/requests/all' and q.get('sort_by') == [key] and q.get('sort_direction') == [direction]


@pytest.mark.parametrize('role,viewport,theme', [
    ('approver', {'width': 1440, 'height': 900}, 'light'),
    ('staff', {'width': 390, 'height': 844}, 'dark'),
])
def test_global_dates_toolbar_header_paging_saved_update_undo_reload(browser, live_server, h, role, viewport, theme):
    made = history(h)
    context, page = sign_in(browser, live_server, role, viewport=viewport)
    page.emulate_media(color_scheme=theme)
    try:
        navigate(page, 'requests')
        if role != 'staff':
            whole_list(page)  # the whole list, not the decider's Waiting for you (998bbca16 maintenance)
        expect(page.get_by_text('Showing 100 of 220', exact=True)).to_be_visible()
        choose(page, 'Group', 'No grouping')
        choose(page, 'Sort', 'Dates')
        expect_ids(page, made[:100])
        toolbar(page, 'Sort').get_by_role('radio', name='Descending', exact=True).click()
        expect_ids(page, made[::-1][:100])
        page.get_by_role('button', name='Show more', exact=True).click()
        expect_ids(page, made[::-1][:200])
        assert len(set(ids(page))) == 200
        # Saving persists the server order, not the loaded-window size.
        toolbar(page, 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
        form = page.get_by_role('dialog', name='New view', exact=True)
        form.get_by_label('View name', exact=True).fill('Newest request dates')
        form.get_by_role('button', name='Save view', exact=True).click()
        expect(form).not_to_be_visible()
        page.reload()
        expect_loaded(page, 'requests')
        expect_ids(page, made[::-1][:100])
        toolbar(page, 'View settings').get_by_role('radio', name='Table', exact=True).click()
        page.keyboard.press('Escape')
        header = page.locator('#main th[data-col="when"]')
        expect(header).to_have_attribute('aria-sort', 'descending')
        header.get_by_role('button', name='Dates', exact=True).click()
        page.get_by_role('menu', name='Dates column').get_by_role('menuitemradio', name='Sort A → Z', exact=True).click()
        expect_ids(page, made[:100])
        toolbar(page, 'Saved views').get_by_role('menuitem', name='Update “Newest request dates”', exact=True).click()
        page.locator('.toast').filter(has_text='View updated.').get_by_role('button', name='Undo', exact=True).click()
        expect_ids(page, made[::-1][:100])
        assert h.get(role, '/api/table-views?table=requests')['views'][0]['definition']['sort'] == ['when', 'desc']
        page.reload()
        expect_loaded(page, 'requests')
        expect_ids(page, made[::-1][:100])
        # Explicit ordering changes rows within groups, not the canonical status group order.
        choose(page, 'Group', 'Status')
        expect(page.locator('#main .dt-group-toggle strong')).to_have_text(['Waiting for approval', 'Approved'])
        menu = toolbar(page, 'Sort')
        expect(menu.get_by_role('menuitemradio', name='Details', exact=True)).to_have_count(0)
        expect(menu.get_by_role('menuitemradio', name='Schedule', exact=True)).to_have_count(0)
        choose(page, 'Sort', 'No sorting')
        page.reload()
        expect_loaded(page, 'requests')
        expect(toolbar(page, 'Sort').get_by_role('menuitemradio', name='No sorting', exact=True)).to_have_attribute('aria-checked', 'true')
    finally:
        context.close()


def test_superseded_sort_response_and_failed_sort_preserve_current_rows_and_retry(browser, live_server, h):
    made = history(h)
    context, page = sign_in(browser, live_server, 'staff')
    held = []
    try:
        navigate(page, 'requests')

        def delay_ascending(route):
            q = parse_qs(urlparse(route.request.url).query)
            if q.get('sort_by') == ['when'] and q.get('sort_direction') == ['asc']:
                held.append((route, route_fetch(route)))
            else:
                route.continue_()

        page.route('**/api/requests/all?*', delay_ascending)
        choose(page, 'Sort', 'Dates')
        toolbar(page, 'Sort').get_by_role('radio', name='Descending', exact=True).click()
        expect_ids(page, made[::-1][:100])
        assert len(held) == 1
        route, response = held.pop()
        with page.expect_response(lambda r: sorted_response(r, 'when', 'asc')) as late:
            route.fulfill(response=response)
        late.value.finished()
        expect_ids(page, made[::-1][:100])
        page.unroute('**/api/requests/all?*', delay_ascending)

        def fail_person(route):
            if parse_qs(urlparse(route.request.url).query).get('sort_by') == ['person']:
                route.abort()
            else:
                route.continue_()

        page.route('**/api/requests/all?*', fail_person)
        choose(page, 'Sort', 'Person')
        expect(page.get_by_text('Couldn’t load requests.', exact=False)).to_be_visible()
        expect_ids(page, made[::-1][:100])
        page.unroute('**/api/requests/all?*', fail_person)
        expected = h.get('staff', '/api/requests/all?sort_by=person&sort_direction=asc')['items']
        page.get_by_role('button', name='Retry loading requests', exact=True).click()
        expect_ids(page, [row['id'] for row in expected])
        expect(page.get_by_role('button', name='Retry loading requests', exact=True)).to_have_count(0)
    finally:
        for route, response in held:
            route.fulfill(response=response)
        context.close()


def test_type_sort_and_legacy_derived_saved_sort_recover_without_a_rejected_read(browser, live_server, h):
    made = history(h)
    context, page = sign_in(browser, live_server, 'staff')
    responses = []
    try:
        navigate(page, 'requests')
        page.on('response', lambda r: responses.append(r) if urlparse(r.url).path == '/api/requests/all' else None)
        choose(page, 'Sort', 'Type')
        wanted = sorted(made[::2])[:100]  # same type uses the stable source/ID tie, not its incidental title
        expect_ids(page, wanted)
        # 4d6b04454: Details is drawn only while a loaded row has details; these leave requests have none, so there is
        # no Details column (and so no Details sort) to offer.
        expect(page.locator('#main th[data-col="detail"]')).to_have_count(0)
        page.evaluate('''() => {
            const key=Object.keys(localStorage).find(k=>k.startsWith('rota.table.')&&k.endsWith('.requests'));
            if(!key)throw new Error('Requests view is not persisted');
            const view=JSON.parse(localStorage.getItem(key));view.sort=['impact','desc'];
            localStorage.setItem(key,JSON.stringify(view));
        }''')
        page.reload()
        expect_loaded(page, 'requests')
        expected = h.get('staff', '/api/requests/all')['items']
        # Requests' existing default groups stages, even when grouping is off; the API waiting/rest order agrees here.
        expect_ids(page, [row['id'] for row in expected])
        expect(toolbar(page, 'Sort').get_by_role('menuitemradio', name='No sorting', exact=True)).to_have_attribute('aria-checked', 'true')
        assert responses and all(r.status == 200 for r in responses)
        assert all(parse_qs(urlparse(r.url).query).get('sort_by') != ['impact'] for r in responses)
    finally:
        context.close()

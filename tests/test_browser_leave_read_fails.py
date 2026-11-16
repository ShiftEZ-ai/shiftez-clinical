"""Leave reads must not silently make people look available on either Schedule grid."""
from datetime import date, timedelta
import re

import pytest
from playwright.sync_api import expect

from .test_browser import (  # noqa: F401
    browser, live_server, sign_in, navigate, view_settings, close_popover, pick_view,
)
from .test_browser_schedule import checked, settled, today
from .test_leave_types import selected

pytestmark = pytest.mark.browser
AVAILABILITY = '**/api/availability'
LEAVE_CLASS = re.compile(r'(^|\s)leave(\s|$)')


@pytest.fixture
def leave_rota(h):
    day = date.fromisoformat(today())
    week_start = h.get('coordinator', '/api/me')['organisation']['week_start']
    first = day - timedelta(days=(day.weekday() - week_start) % 7)
    person = h.seed['people'][0]
    w = h.workspace('Fictional leave read rota', first.isoformat(), (first + timedelta(days=7)).isoformat())
    w = h.shift(w, f'{day}T08:00:00+03:00', f'{day}T12:00:00+03:00', people=[person])['workspace']
    leave = h.command('coordinator', 'availability.record', {
        'person_id': person, 'kind': 'recorded_leave', 'leave_type_id': selected(h)['id'],
        'start': f'{day}T00:00:00+03:00', 'end': f'{day + timedelta(days=1)}T00:00:00+03:00',
        'private_reason': 'Fictional leave for the read failure check',
    })['record']
    assert leave['counts_as_unavailable'] is True
    return checked(h, w), person, day.isoformat()


def open_people(page, w, day):
    navigate(page, f"schedule/people/{day}?edit={w['id']}", alert=True)
    expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'People view')
    view_settings(page.locator('.planner-toolbar'), 'Layers').get_by_label('Leave and wishes', exact=True).check()
    close_popover(page)
    settled(page)


def day_cell(page, person, day, column='data-day'):
    cell = page.locator(f'#schedule-view [data-row-person="{person}"] .pg-cell[{column}="{day}"]')
    expect(cell).to_be_visible()
    return cell


def assert_failed_leave(page, cell):
    status = page.locator('#schedule-view .pg-leave-status')
    failed = status.locator('.read-failed')
    expect(failed).to_be_visible()
    expect(failed).to_have_attribute('role', 'alert')
    expect(failed).to_contain_text('Couldn’t load leave.')
    expect(failed).to_contain_text('People may show as available.')
    expect(failed.get_by_role('button', name='Retry loading leave', exact=True)).to_be_visible()
    expect(cell).not_to_have_class(LEAVE_CLASS)
    assert status.evaluate("""slot => {
        const scroller = slot.parentElement.querySelector(':scope > .week-scroller');
        return !!scroller && !slot.closest('.week-scroller')
            && !!(slot.compareDocumentPosition(scroller) & Node.DOCUMENT_POSITION_FOLLOWING);
    }"""), 'Leave status must precede the grid scroller, outside it'


def retry_leave(page, cell):
    page.unroute(AVAILABILITY)
    status = page.locator('#schedule-view .pg-leave-status')
    status.get_by_role('button', name='Retry loading leave', exact=True).click()
    expect(cell).to_have_class(LEAVE_CLASS)
    expect(cell).to_have_attribute('data-tip', re.compile(r'^On leave or away'))
    expect(status).to_be_empty()


def test_people_failed_leave_is_not_cached_then_retry_marks_leave(browser, live_server, leave_rota):
    w, person, day = leave_rota
    context, page = sign_in(browser, live_server, 'coordinator')
    failed_reads = []

    def fail(route):
        failed_reads.append(route.request.url)
        route.fulfill(status=500, json={'error': {'code': 'INTERNAL_ERROR', 'message': 'Fictional leave read failure'}})

    try:
        page.route(AVAILABILITY, fail)
        open_people(page, w, day)
        assert_failed_leave(page, day_cell(page, person, day))

        # Re-enter before the successful Retry: a successful leave read is intentionally cached.
        reads_before = len(failed_reads)
        navigate(page, f"schedule/week/{day}?edit={w['id']}")
        expect(page.locator('#schedule-view .pg-leave-status')).to_have_count(0)
        open_people(page, w, day)
        cell = day_cell(page, person, day)
        assert_failed_leave(page, cell)
        assert len(failed_reads) > reads_before, 'A failed leave read must be requested again on re-entry'

        retry_leave(page, cell)
    finally:
        context.close()


def test_refused_leave_read_is_information(browser, live_server, leave_rota):
    w, person, day = leave_rota
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(AVAILABILITY, lambda route: route.fulfill(status=403, json={
            'error': {'code': 'ROLE_REFUSED', 'message': 'Not available for your role.'},
        }))
        open_people(page, w, day)
        status = page.locator('#schedule-view .pg-leave-status')
        info = status.locator('.notice.info')
        expect(info).to_be_visible()
        expect(info).to_have_attribute('role', 'note')
        expect(info).to_have_text('Not available for your role.')
        expect(status.locator('.read-failed, [role="alert"]')).to_have_count(0)
        expect(status.get_by_role('button', name='Retry loading leave', exact=True)).to_have_count(0)
        expect(day_cell(page, person, day)).not_to_have_class(LEAVE_CLASS)
    finally:
        context.close()


@pytest.mark.parametrize('code', ['SESSION_CHANGED', 'PAGE_LEFT'])
def test_discarded_leave_read_is_silent(browser, live_server, leave_rota, code):
    w, person, day = leave_rota
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    try:
        page.route(AVAILABILITY, lambda route: held.append(route))
        with page.expect_request(AVAILABILITY):
            open_people(page, w, day)
        cell = day_cell(page, person, day)
        # Join the held read so silence is checked after leaveLayer's rejection handler has run.
        page.evaluate("""async () => {
            const api = await window.importBundle('api.js');
            window.leaveRead = api.get('/api/availability').then(() => 'accepted', error => error.code);
        }""")
        assert held
        for route in held:
            route.fulfill(status=409, json={'error': {'code': code, 'message': 'The previous read was discarded.'}})
        assert page.evaluate('window.leaveRead') == code
        status = page.locator('#schedule-view .pg-leave-status')
        expect(status).to_be_empty()
        expect(page.locator('#schedule-view .read-failed')).to_have_count(0)
        expect(cell).not_to_have_class(LEAVE_CLASS)
    finally:
        context.close()


@pytest.mark.parametrize('cells', ['codes', 'counts'])
def test_second_grid_failed_leave_then_retry_marks_leave(browser, live_server, h, leave_rota, cells):
    w, person, day = leave_rota
    name = f'Fictional leave read {cells}'
    h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': name, 'definition': {'planner': {
        'mode': 'timeline', 'range': 'week', 'rows': 'person', 'columns': 'day', 'cells': cells,
        'properties': [{'key': 'leave', 'on': True, 'form': 'text'}],
    }}})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(AVAILABILITY, lambda route: route.fulfill(status=500, json={
            'error': {'code': 'INTERNAL_ERROR', 'message': 'Fictional leave read failure'},
        }))
        navigate(page, f"schedule/week/{day}?edit={w['id']}")
        pick_view(page, name)
        settled(page)
        expect(page.locator('#schedule-view .tl-grid')).to_be_visible()
        cell = day_cell(page, person, day, 'data-col')
        assert_failed_leave(page, cell)
        retry_leave(page, cell)
    finally:
        context.close()

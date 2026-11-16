"""rc.16 UI/UX review, batch 4: inline form errors (R-01), checks before sending leave (R-05), the searchable timezone
list (C-01), one empty-state pattern (C-03), grouped tables without the grouped column (AC-02) and one Tab stop for a
table's rows (G-08)."""
from datetime import timedelta
import re

import pytest
from sqlalchemy import select
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .test_browser import form_submit, browser, live_server, sign_in, navigate, fill_catalogue, no_opening_hours  # noqa: F401
from .test_browser_requests import day, new_request, published

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 1000}
PHONE = {'width': 390, 'height': 844}


@pytest.mark.parametrize('viewport', [DESKTOP, PHONE], ids=['desktop', 'phone'])
def test_form_problems_show_under_the_field_and_the_first_gets_focus(browser, live_server, h, viewport):
    context, page = sign_in(browser, live_server, 'staff', viewport=viewport)
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Leave')
        dialog.get_by_role('button', name='Send request', exact=True).click()
        reason = dialog.get_by_label('Reason', exact=True)
        # A message under each field (not a passing browser bubble), named as its description, in the field's own words;
        # the focus goes to the first problem on the page, the Type (frontend audit R-02).
        expect(reason).to_have_attribute('aria-invalid', 'true')
        kind = dialog.get_by_label('Type', exact=True)
        expect(kind).to_be_focused()
        expect(kind).to_have_accessible_description('Choose a type')
        message = page.locator('#' + reason.get_attribute('aria-describedby').split()[-1])
        expect(message).to_be_visible()
        expect(message).to_have_text('Add a reason')  # the privacy note is the field's hint (f8)
        reason.fill('Family matter')
        expect(reason).not_to_have_attribute('aria-invalid', 'true')
        expect(message).to_be_hidden()
        # Fixed: the old problem is no longer part of the field's accessible description; only its hint is (R-24).
        expect(reason).to_have_accessible_description('Private · no patient information')
    finally:
        context.close()


def test_new_leave_shows_the_duties_it_touches_and_refuses_an_exact_copy(browser, live_server, h):
    published(h)
    context, page = sign_in(browser, live_server, 'staff', viewport=DESKTOP)

    def ask():
        dialog = new_request(page, 'Leave')
        dialog.get_by_label('All day').check()
        dialog.get_by_label('First day', exact=True).fill(day())
        dialog.get_by_label('Last day', exact=True).fill(day())
        dialog.get_by_label('Type', exact=True).select_option(label='Annual')
        dialog.get_by_label('Reason', exact=True).fill('Family matter')
        return dialog

    try:
        navigate(page, 'requests')
        dialog = ask()
        # Before Send: the duty these dates touch, in the form itself.
        expect(dialog.locator('.request-clash')).to_contain_text('On these dates you have 1 duty')
        dialog.get_by_role('button', name='Send request', exact=True).click()
        expect(dialog).to_be_hidden()
        count = len(h.get('staff', '/api/requests')['requests'])
        dialog = ask()
        expect(dialog.locator('.request-clash')).to_contain_text('You already asked for exactly this')
        dialog.get_by_role('button', name='Send request', exact=True).click()
        expect(dialog).to_contain_text('You already asked for exactly this.')
        assert len(h.get('staff', '/api/requests')['requests']) == count
    finally:
        context.close()


def test_an_exact_copy_past_the_first_page_of_requests_is_still_refused(browser, live_server, h):
    """More than 200 waiting requests (the API's page): the copy sits past the first page either way it is sorted,
    and the form still finds it."""
    published(h)
    context, page = sign_in(browser, live_server, 'staff', viewport=DESKTOP)

    def ask():
        dialog = new_request(page, 'Leave')
        dialog.get_by_label('All day').check()
        dialog.get_by_label('First day', exact=True).fill(day())
        dialog.get_by_label('Last day', exact=True).fill(day())
        dialog.get_by_label('Type', exact=True).select_option(label='Annual')
        dialog.get_by_label('Reason', exact=True).fill('Family matter')
        return dialog

    try:
        navigate(page, 'requests')
        dialog = ask()
        dialog.get_by_role('button', name='Send request', exact=True).click()
        expect(dialog).to_be_hidden()
        with transaction(h.app.state.engine, write=True) as db:
            original = db.scalars(select(m.RequestRecord).where(m.RequestRecord.person_id == h.seed['people'][0])).one()
            columns = {c.key: getattr(original, c.key) for c in m.RequestRecord.__table__.columns if c.key not in {'id', 'created_at'}}
            # 210 earlier and 210 later: the copy is row 211 sorted either way.
            db.add_all(m.RequestRecord(**{**columns, 'start': original.start + timedelta(days=n), 'end': original.end + timedelta(days=n)})
                       for n in [*range(-420, -210), *range(1, 211)])
        first = h.get('staff', f"/api/requests/all?person_id={h.seed['people'][0]}&type=leave,other&status=waiting,approved&limit=200")
        assert first['total'] == 421 and original.id not in {r['id'] for r in first['items']}
        count = first['total']
        dialog = ask()
        expect(dialog.locator('.request-clash')).to_contain_text('You already asked for exactly this', timeout=15000)
        dialog.get_by_role('button', name='Send request', exact=True).click()
        expect(dialog).to_contain_text('You already asked for exactly this.')
        assert h.get('staff', f"/api/requests/all?person_id={h.seed['people'][0]}&type=leave,other&status=waiting,approved&limit=1")['total'] == count
    finally:
        context.close()


@pytest.mark.parametrize('when', ['same-day', 'last-day'])
def test_timed_request_reads_the_duties_of_its_last_day(browser, live_server, h, when):
    """A timed request reads the calendar through the day its end falls in (the calendar's end day is exclusive):
    a same-day 08:00-16:00 request and a two-day request ending mid-morning both find the duty on that day."""
    published(h)
    start, end = (f'{day()}T08:00', f'{day()}T16:00') if when == 'same-day' else (f'{day(-1)}T09:00', f'{day()}T10:00')
    context, page = sign_in(browser, live_server, 'staff', viewport=DESKTOP)
    asked = []
    page.on('request', lambda r: '/api/calendar?' in r.url and asked.append(r.url))
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Leave')
        dialog.get_by_label('All day').uncheck()
        dialog.get_by_label('Request from', exact=True).fill(start)
        dialog.get_by_label('Request until', exact=True).fill(end)
        dialog.get_by_label('Request until', exact=True).blur()
        expect(dialog.locator('.request-clash')).to_contain_text('On these dates you have 1 duty')
        assert f'start={start[:10]}&end={day(1)}' in asked[-1], asked[-1]  # the end day is read whole
    finally:
        context.close()


def test_scheduling_timezone_is_a_searchable_list(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'configuration/department')
        zone = page.get_by_label('Scheduling timezone', exact=True)
        expect(zone).to_have_value('Asia/Riyadh')
        zone.click()
        picker = page.locator('.picker')
        picker.get_by_role('combobox').fill('tokyo')
        picker.get_by_role('option', name='Asia/Tokyo').click()
        expect(zone).to_have_value('Asia/Tokyo')
        expect(page.get_by_role('button', name='Save settings', exact=True)).to_be_enabled()
    finally:
        context.close()


def test_empty_opening_hours_say_why_and_offer_the_one_action(browser, live_server, h):
    no_opening_hours(h)
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'configuration/opening-hours')
        state = page.locator('#main .empty-state')
        expect(state.get_by_role('heading')).to_have_text('No opening hours yet')
        expect(state).to_contain_text('When clinics and services open, so sessions fit real hours.')  # the kind's purpose (C-02)
        expect(page.locator('#main').get_by_role('button', name='Set opening hours', exact=True)).to_have_count(1)
        state.get_by_role('button', name='Set opening hours', exact=True).click()
        expect(page.get_by_role('dialog')).to_be_visible()
    finally:
        context.close()


def test_role_history_grouped_by_person_does_not_repeat_the_person_column(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'access/history')
        table = page.locator('#main table.dt-table')
        expect(table.locator('tr.dt-group').first).to_be_visible()
        expect(table.get_by_role('columnheader', name=re.compile('^Person'))).to_have_count(0)
        expect(table.get_by_role('columnheader', name=re.compile('^Role'))).to_have_count(1)
    finally:
        context.close()


def test_a_tables_rows_are_one_tab_stop_and_arrows_move_between_rows(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        body = page.locator('#main table.dt-table tbody')
        expect(body.locator('tr').nth(1)).to_be_visible()
        stops = 'b => [...b.querySelectorAll("[data-rove]")].filter(n => n.tabIndex === 0).map(n => n.closest("tr").rowIndex)'
        rows = set(body.evaluate(stops))
        assert len(rows) == 1, rows  # every stop is in one row
        first = body.locator('[data-rove][tabindex="0"]').first
        first.focus()
        page.keyboard.press('ArrowDown')
        moved = page.evaluate('document.activeElement.closest("tr").rowIndex')
        assert moved == next(iter(rows)) + 1
        assert set(body.evaluate(stops)) == {moved}
        expect(page.locator('#main table.dt-table')).to_have_accessible_description('Use arrow keys to move between rows.')
        # A text field in a row: Escape leaves it for the table, so the keyboard is never stuck there.
        page.evaluate('() => { const i = document.createElement("input"); i.id = "row-field"; '
                      'document.querySelector("#main table.dt-table tbody tr[data-row] td:last-child").append(i); }')
        page.locator('#row-field').focus()
        page.keyboard.press('Escape')
        assert page.evaluate('document.activeElement.classList.contains("table-scroll")')
    finally:
        context.close()


def test_group_headings_keep_their_own_tab_stop(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'access/history')
        toggles = page.locator('#main .dt-group-toggle')
        expect(toggles.first).to_be_visible()
        assert all(t == 0 for t in toggles.evaluate_all('ts => ts.map(t => t.tabIndex)'))
    finally:
        context.close()


def test_a_new_recurring_session_keeps_its_usual_assignees_by_default_and_says_so(browser, live_server, h):
    """The form shows what is saved: Keep starts on for a new series once it has usual assignees, and is always sent."""
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'configuration')
        page.get_by_role('button', name='Recurring sessions', exact=True).click()
        page.get_by_role('button', name='New recurring session', exact=True).click()
        form = fill_catalogue(page, 'Usual assignees kept')
        form.get_by_label('Effective until (included)', exact=True).fill('2026-10-31')
        form.get_by_label('First week of the pattern', exact=True).fill('2026-10-01')
        form.get_by_label('Duty type', exact=True).select_option(h.seed['type'])
        form.get_by_label('Location', exact=True).select_option(h.seed['locations'][0])
        form.get_by_role('group', name='Clinics and services', exact=True).get_by_role('checkbox').first.check()
        form.get_by_role('checkbox', name='Monday', exact=True).check()
        fixed = form.get_by_role('checkbox', name='Keep the usual assignees on every session')
        expect(fixed).to_be_disabled()  # nobody to keep yet
        expect(fixed).not_to_be_checked()
        form.get_by_role('group', name='Usual assignees').get_by_role('checkbox').first.check()
        expect(fixed).to_be_enabled()
        expect(fixed).to_be_checked()
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'catalogue.create') as created:
            form_submit(form).click()
        sent = created.value.request.post_data_json['payload']['value']['definition']
        assert sent['usual_assignees_fixed'] is True
        assert created.value.ok, created.value.text()
        record = next(r for r in h.get('admin', '/api/catalogues')['recurring_series'] if r['name'] == 'Usual assignees kept')
        assert record['definition']['usual_assignees_fixed'] is True
    finally:
        context.close()

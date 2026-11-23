"""Configuration › Recurring sessions: one row per series type (older data keeps one series per weekday or person),
the clinic week grid as the shared collection's Calendar layout, inline edits with Undo, and the form (required
fields, the one-year limit beside the field, times from the duty type, staff needed, the shared excluded-dates list)."""
import os
import re
import time

import pytest
from datetime import date
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import SeriesDefinition

from .test_browser import form_submit, browser, live_server, sign_in, open_config, close_popover, fonts_ready  # noqa: F401

pytestmark = pytest.mark.browser
SHOTS = os.getenv('RECURRING_SHOTS')  # a folder: each state is saved there for a visual review
WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
NOTE = 'Reference fixed clinic; named assignees confirmed by the user. Synthetic test data.'


def shot(page, name, *, animations='allow'):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, name + '.png'), full_page=False, animations=animations)


def duty_types(h):
    def typ(name, start, end, minimum):
        return h.configure('assignment_types', {'name': name, 'starts_on': '2020-01-01', 'definition': {
            'default_timing': {'start_time': start, 'end_time': end}, 'maximum_duration_minutes': 720,
            'requirements': [{'minimum': minimum, 'maximum': minimum + 1}]}})['record']
    return {'am': typ('Morning', '08:30', '12:00', 1), 'pm': typ('Afternoon', '13:00', '16:00', 1),
            'eve': typ('Evening shift', '16:00', '24:00', 2)}


def series(h, name, typ, location, service, days, people=(), starts='2026-09-01', ends=None, active=True):
    return h.configure('recurring_series', {'name': name, 'description': NOTE, 'starts_on': starts, 'ends_on': ends, 'active': active,
        'assignment_type_id': typ['id'], 'location_id': location, 'definition': {'anchor': starts,
            'days': [{'weekday': d, 'start_time': typ['definition']['default_timing']['start_time'],
                      'end_time': typ['definition']['default_timing']['end_time']} for d in days],
            'service_ids': [service], 'default_person_ids': list(people)}})['record']


def department(h, clinics=25):
    """The DEV shape at department scale, written straight to the database (the API's rate limit is per person):
    one series per weekday and per person, two people on some days, about 110 series over 25 clinics."""
    types = duty_types(h)
    effective = {'department_id': h.seed['department_id'], 'starts_on': date(2020, 1, 1), 'active': True}
    made = 0
    with transaction(h.app.state.engine, write=True) as db:
        people = [m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Dr Fictional Person {n:02d}') for n in range(30)]
        db.add_all(people); db.flush()
        for c in range(clinics):
            name = 'Poly Urgent Clinic' if c == 0 else f'Fictional Clinic {c:02d}'
            service = m.Service(name=name, description='', **effective)
            db.add(service); db.flush()
            room = m.Location(name=name + ' Room 1', location_type_id=h.seed['location_type'], capacity=4, **effective)
            db.add(room); db.flush()

            def add(label, typ, day, person):
                t = typ['definition']['default_timing']
                definition = SeriesDefinition(anchor=date(2026, 9, 1), service_ids=[service.id], default_person_ids=[person],
                                              days=[{'weekday': day, 'start_time': t['start_time'], 'end_time': t['end_time']}])
                db.add(m.RecurringSeries(id=m.uid(), department_id=h.seed['department_id'], name=label, description=NOTE,
                                         assignment_type_id=typ['id'], location_id=room.id, starts_on=date(2026, 9, 1),
                                         definition=definition.model_dump(mode='json')))
            for d in (6, 0, 1, 2, 3) if c == 0 else (6, 1, 3, 0, 2)[: 2 + c % 3]:
                for k in range(2 if c == 0 and d != 0 else 1):
                    add(f'{name} · {WEEKDAYS[d]} · 08:30', types['am'], d, people[(c + d + k) % 30].id); made += 1
                if c % 2 == 0:
                    add(f'{name} · {WEEKDAYS[d]} · 13:00', types['pm'], d, people[(c * 3 + d) % 30].id); made += 1
    return types, [p.id for p in people], made


def until(check, seconds=10):
    """Poll the API until a saved state is reached (an Undo of several saves finishes after its first message)."""
    end = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < end, 'timed out'
        time.sleep(0.2)


def open_recurring(page):
    open_config(page, 'Recurring sessions')
    expect(page.get_by_role('heading', name='Recurring sessions', exact=True)).to_be_visible()


def layout(page, name):
    page.get_by_role('button', name=re.compile(r'^View settings')).click()
    page.get_by_role('radiogroup', name='Layout').get_by_role('radio', name=name, exact=True).click()
    page.keyboard.press('Escape')


def test_empty_list_offers_create(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Recurring sessions')
        expect(page.get_by_text('No recurring sessions yet', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='New recurring session', exact=True)).to_be_visible()
        shot(page, 'recurring-empty-light')
    finally:
        context.close()


@pytest.fixture(params=['light', 'dark'])
def department_scale_recurring_grid(browser, live_server, h, request):
    scheme = request.param
    types, people, made = department(h)
    poly = [s for s in h.get('admin', '/api/catalogues')['recurring_series'] if s['name'].startswith('Poly Urgent')]
    # Ended, switched off and upcoming read as such in the grid.
    ended = series(h, 'Fictional Clinic 02 · Thursday · 16:00', types['eve'], poly[0]['location_id'], poly[0]['definition']['service_ids'][0], [3], starts='2026-08-01', ends='2026-09-01')
    series(h, 'Poly Urgent Clinic · Thursday · 16:00 off', types['eve'], poly[0]['location_id'], poly[0]['definition']['service_ids'][0], [3], active=False)
    series(h, 'Poly Urgent Clinic · Monday · 16:00 later', types['eve'], poly[0]['location_id'], poly[0]['definition']['service_ids'][0], [0], starts='2026-11-01')
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        began = time.monotonic()
        open_recurring(page)
        table = page.locator('[data-table="catalogue-recurring_series"]')
        # The default view: grouped by clinic, each clinic a week grid.
        expect(page.get_by_role('button', name='Group: Clinic', exact=True)).to_be_visible()
        expect(table.locator('.dt-group-toggle')).to_have_count(25)
        expect(table.locator('table.sw-week')).to_have_count(25)
        elapsed = time.monotonic() - began
        yield page, table, scheme, made, elapsed
    finally:
        context.close()


def test_clinic_week_grid_at_department_scale(department_scale_recurring_grid):
    page, table, scheme, made, _ = department_scale_recurring_grid
    assert made + 3 >= 100
    # One unit everywhere: the list counts its rows (sessions) and the series in them; the section list counts rows too.
    count = page.locator('.app-footer .dt-count').inner_text()
    assert re.fullmatch(r'\d+ sessions · \d+ series', count), count
    expect(page.get_by_role('navigation', name='Configuration sections').get_by_role('button', name='Recurring sessions')
           .locator('.nav-count-muted')).to_have_text(count.split()[0])
    poly_group = table.locator('.dt-card-group', has=page.locator('.dt-group-toggle', has_text='Poly Urgent Clinic'))
    expect(poly_group).to_contain_text('sessions a week')
    shot(page, f'recurring-grid-top-{scheme}')
    grid = poly_group.locator('xpath=following-sibling::div[1]').locator('table.sw-week')
    grid.scroll_into_view_if_needed()
    rows = grid.locator('tbody tr')
    expect(rows).to_have_count(3)  # Morning, Afternoon, Evening shift
    morning = rows.filter(has_text='08:30–12:00')
    expect(morning).to_contain_text('1 person each')
    # Sunday morning has two sessions, each with its own person; Monday one.
    evening = rows.filter(has_text='16:00–24:00')
    expect(evening).to_contain_text('2 people each')
    expect(evening.get_by_role('button', name=re.compile(r'Evening shift 16:00–24:00: .*From 1 Nov 2026'))).to_have_count(1)
    expect(evening.get_by_role('button', name=re.compile(r'Evening shift 16:00–24:00: .*, Off[,.]'))).to_have_count(1)
    expect(table.get_by_role('button', name=re.compile(r'Ended 31 Aug 2026'))).to_have_count(1)
    # A gap in the pattern is one click from a new session for that day and time.
    expect(morning.get_by_role('button', name='Add Poly Urgent Clinic, Morning 08:30–12:00 on Friday', exact=True)).to_have_count(1)
    shot(page, f'recurring-grid-{scheme}')
    # A cell opens its series.
    morning.get_by_role('button', name=re.compile(r'^Poly Urgent Clinic, Morning 08:30–12:00: .*one of 2 sessions')).first.click()
    dialog = page.get_by_role('dialog', name='Edit recurring session')
    expect(dialog).to_be_visible()
    expect(dialog.get_by_label('Name', exact=True)).to_have_value(re.compile(r'^Poly Urgent Clinic · \w+ · 08:30$'))
    shot(page, f'recurring-edit-{scheme}')
    dialog.get_by_role('button', name='Cancel', exact=True).click()
    # Group options: clinic, clinic type, duty type, person, none.
    page.get_by_role('button', name='Group: Clinic', exact=True).click()
    menu = page.get_by_role('dialog', name='Group', exact=True)
    field = menu.get_by_label('Group by', exact=True)
    for option in ['Clinic', 'Clinic type', 'Duty type', 'Usual assignees']:
        expect(field.locator('option', has_text=re.compile('^' + re.escape(option) + r'( \(default\))?$'))).to_have_count(1)
    expect(menu.get_by_role('button', name='Remove grouping', exact=True)).to_be_visible()
    field.select_option(label='Duty type')
    close_popover(page)
    expect(page.get_by_role('button', name='Group: Duty type', exact=True)).to_be_visible()
    expect(table.locator('.dt-group-toggle')).to_have_count(3)
    shot(page, f'recurring-grid-by-type-{scheme}')
    # The flat table: one row per series type, the shared description said once (not on every row).
    page.get_by_role('button', name='Group: Duty type', exact=True).click()
    page.get_by_role('dialog', name='Group', exact=True).get_by_role('button', name='Remove grouping', exact=True).click()
    close_popover(page)
    layout(page, 'Table')
    rows = table.locator('tbody tr[data-row]')
    # Morning, afternoon, and three evening versions (ended, switched off, from November).
    expect(rows.filter(has_text='Poly Urgent Clinic')).to_have_count(5)
    expect(table).not_to_contain_text('Reference fixed clinic')
    # Every column fits at 1440 (no sideways scrolling to reach Dates and Status).
    assert table.locator('.table-scroll').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
    expect(rows.filter(has_text='Poly Urgent Clinic').first).to_contain_text('08:30–12:00')
    shot(page, f'recurring-table-{scheme}')
    # A row made of several series (one per day and person) opens a list of them to choose from.
    rows.filter(has_text='Poly Urgent Clinic').filter(has_text='08:30–12:00').locator('td[data-col=name]').click()
    chooser = page.get_by_role('menu', name='Recurring sessions')
    expect(chooser.get_by_role('menuitem')).to_have_count(9)
    page.keyboard.press('Escape')
    # Filtered to nothing: the toolbar stays, and says how to get back.
    page.get_by_role('searchbox', name='Search recurring sessions').fill('nothing matches this')
    expect(page.get_by_text('No matching sessions', exact=True)).to_be_visible()
    shot(page, f'recurring-filtered-empty-{scheme}')
    page.get_by_role('button', name='Show all', exact=True).click()
    layout(page, 'Calendar')


@pytest.mark.wall_benchmark
def test_department_scale_recurring_grid_cpu_budget(department_scale_recurring_grid):
    _, _, _, _, elapsed = department_scale_recurring_grid
    assert elapsed < (20 if os.getenv('CI') else 10)


def undo_of(ids):
    """The one Undo of an inline edit: the server's own reverse of its bulk change (one bulk.apply of every series it
    changed, bound to the change by undo_of), sent as the server returned it."""
    def matches(response):
        body = response.request.post_data_json if response.request.method == 'POST' and response.url.endswith('/api/commands') else None
        payload = (body or {}).get('payload') or {}
        return (body or {}).get('kind') == 'bulk.apply' and bool(payload.get('undo_of'))             and {i.get('target_id') for i in payload.get('items', [])} == set(ids)
    return matches


def stored(h, ids):
    """The saved days, times and status of these series (read from the API)."""
    return {s['id']: (s['definition']['days'], s['active']) for s in h.get('admin', '/api/catalogues')['recurring_series'] if s['id'] in ids}


def test_inline_days_times_and_status_save_together_with_one_undo(browser, live_server, h):
    types = duty_types(h)
    one = series(h, 'Fictional Clinic · Morning', types['am'], h.seed['locations'][0], h.seed['services'][0], [0, 1])
    split = [series(h, f'Fictional Split Clinic · {WEEKDAYS[d]} · 13:00', types['pm'], h.seed['locations'][1], h.seed['services'][1], [d], [h.seed['people'][d % 3]]) for d in (2, 3)]
    before = stored(h, {one['id'], *(x['id'] for x in split)})
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_recurring(page)
        layout(page, 'Table')
        table = page.locator('[data-table="catalogue-recurring_series"]')
        row = table.locator('tbody tr[data-row]').filter(has_text='08:30–12:00')
        # Days: one series gains Thursday.
        row.locator('td[data-col=days]').click()
        page.get_by_role('checkbox', name='Thursday', exact=True).check()
        page.get_by_role('button', name='Save', exact=True).click()
        expect(page.get_by_text('Days saved.', exact=False)).to_be_visible()
        days = row.locator('.sr-days')
        expect(days).to_have_attribute('aria-label', re.compile('Thursday'))  # the table drawn from the saved records
        saved = next(s for s in h.get('admin', '/api/catalogues')['recurring_series'] if s['id'] == one['id'])
        assert sorted(d['weekday'] for d in saved['definition']['days']) == [0, 1, 3]
        # The inline edit is one bulk change (one transaction, one Undo): its Undo is the server's own reverse.
        drawn = table.element_handle()
        with page.expect_response(undo_of([one['id']])) as undone:
            page.get_by_role('button', name='Undo', exact=True).click()
        assert undone.value.ok, undone.value.text()
        page.wait_for_function('table => !table.isConnected', arg=drawn)
        assert stored(h, {one['id']}) == {one['id']: before[one['id']]}  # days, times and status as they were
        # The table drawn from the undone records (the Undo's message can be an earlier one, still on screen): an edit
        # from the table drawn before it would send the revisions the Undo replaced.
        expect(days).to_have_attribute('aria-label', re.compile('^(?!.*Thursday)'))
        until(lambda: sorted(d['weekday'] for s in h.get('admin', '/api/catalogues')['recurring_series'] if s['id'] == one['id']
                             for d in s['definition']['days']) == [0, 1])
        # Times: two series (one per day) change together, and one Undo puts both back.
        split_row = table.locator('tbody tr[data-row]').filter(has_text='Review clinic')
        expect(split_row).to_have_count(1)
        split_row.locator('td[data-col=times]').click()
        page.get_by_label('Start', exact=True).fill('13:30')
        page.get_by_label('End', exact=True).fill('16:30')
        page.get_by_role('button', name='Save', exact=True).click()
        expect(page.get_by_text('Times saved.', exact=False)).to_be_visible()
        expect(split_row.locator('td[data-col=times]')).to_contain_text('13:30')
        after = [s for s in h.get('admin', '/api/catalogues')['recurring_series'] if s['id'] in {x['id'] for x in split}]
        assert {d['start_time'] for s in after for d in s['definition']['days']} == {'13:30'}
        expect(split_row.locator('td[data-col=times]')).to_have_text('13:30–16:30')
        # One Undo restores both series (one reverse for both).
        drawn = table.element_handle()
        with page.expect_response(undo_of([x['id'] for x in split])) as undone:
            page.get_by_role('button', name='Undo', exact=True).click()
        assert undone.value.ok, undone.value.text()
        page.wait_for_function('table => !table.isConnected', arg=drawn)
        assert stored(h, {x['id'] for x in split}) == {x['id']: before[x['id']] for x in split}
        expect(table.locator('tbody tr[data-row]').filter(has_text='13:30')).to_have_count(0)
        expect(split_row).to_have_count(1)
        expect(split_row.locator('td[data-col=times]')).to_contain_text('13:00')
        until(lambda: {d['start_time'] for s in h.get('admin', '/api/catalogues')['recurring_series'] if s['id'] in {x['id'] for x in split}
                       for d in s['definition']['days']} == {'13:00'})
        expect(split_row.locator('td[data-col=times]')).to_have_text('13:00–16:00')
        # Status: switching the row off switches both of its series off.
        split_row = table.locator('tbody tr[data-row]').filter(has_text='Review clinic')
        split_row.locator('td[data-col=status]').click()
        expect(page.get_by_text('Status saved.', exact=False)).to_be_visible()
        assert all(not s['active'] for s in h.get('admin', '/api/catalogues')['recurring_series'] if s['id'] in {x['id'] for x in split})
        expect(table.locator('tbody tr[data-row]').filter(has_text='Review clinic')).to_contain_text('Inactive')
        shot(page, 'recurring-inline-light')
        # And one Undo switches both back on: days, times and status all as they were.
        page.get_by_role('button', name='Undo', exact=True).click()
        until(lambda: stored(h, {one['id'], *(x['id'] for x in split)}) == before)
    finally:
        context.close()


def test_form_requires_fields_limits_dates_beside_the_field_and_saves_on_first_click(browser, live_server, h):
    types = duty_types(h)
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_recurring(page)
        page.get_by_role('button', name='New recurring session', exact=True).click()
        form = page.get_by_role('dialog', name='New recurring session')
        # Required fields are marked in words, or the optional ones are (maintenance: the fewer kind); never a required one as optional.
        expect(form.locator('[data-cue]').first).to_be_visible()
        for label in ['Duty type', 'Location', 'Name']:
            expect(form.locator('.field label[data-cue=optional]', has_text=re.compile('^' + label))).to_have_count(0)
        # Times follow the chosen duty type until someone types their own; staff needed comes from it.
        form.get_by_label('Duty type', exact=True).select_option(types['eve']['id'])
        expect(form.get_by_label('Monday start', exact=True)).to_have_value('16:00')
        expect(form.get_by_label('Monday end', exact=True)).to_have_value('24:00')
        expect(form.locator('.series-staff')).to_contain_text('2 people')
        expect(form.locator('.series-staff').get_by_role('button', name='Edit duty type')).to_be_visible()
        form.get_by_label('Location', exact=True).select_option(h.seed['locations'][0])
        form.get_by_role('group', name='Clinics and services').get_by_role('checkbox').first.check()
        # The name reads "<Clinic> · <Duty type>" until one is typed.
        expect(form.get_by_label('Name', exact=True)).to_have_value(re.compile(r' · Evening shift$'))
        # The end defaults to a year (less a day); a longer version is refused beside the field, before Save.
        form.get_by_label('Effective from', exact=True).fill('2026-10-01')
        expect(form.get_by_label('Effective until (included)', exact=True)).to_have_value('Thu 30 Sep 2027')
        form.get_by_label('Effective until (included)', exact=True).fill('2027-12-31')
        expect(form.locator('.field-error')).to_contain_text('More than one year: choose Fri 1 Oct 2027 or earlier')
        shot(page, 'recurring-form-limit-light')
        form.get_by_label('Effective until (included)', exact=True).fill('2027-03-31')
        expect(form.locator('.field-error')).to_be_hidden()
        for day in ('Sunday', 'Tuesday'):
            form.get_by_role('checkbox', name=day, exact=True).check()
        # Excluded dates: the shared list, from the calendar (several days) and from pasted text.
        excluded = form.get_by_role('group', name='Excluded dates')
        excluded.get_by_role('button', name='Paste dates').click()
        page.get_by_label('Dates to use', exact=True).fill('2026-12-01\n2 Dec 2026, 03/12/2026\nnot a date')
        expect(page.get_by_text('3 dates replace the list · not read: not a date', exact=True)).to_be_visible()
        page.get_by_role('button', name='Use these dates', exact=True).click()
        expect(excluded.locator('.date-chip')).to_have_count(3)
        excluded.get_by_role('button', name='Remove Wednesday 2 December 2026').click()
        expect(excluded.locator('.date-chip')).to_have_count(2)
        excluded.get_by_role('button', name='Add dates').click()
        cal = page.locator('.date-pop')
        cal.locator('[data-day="2026-10-20"]').click()
        cal.locator('[data-day="2026-10-22"]').click(modifiers=['Shift'])
        cal.get_by_role('button', name='Use 5 dates').click()
        expect(excluded.locator('.date-chip')).to_have_count(5)
        shot(page, 'recurring-form-light')
        page.emulate_media(color_scheme='dark')
        shot(page, 'recurring-form-dark', animations='disabled')
        # Saved on the first click once the check is current and changes nothing.
        expect(form.get_by_text(re.compile(r'Configuration checked\.'))).to_be_visible(timeout=20000)
        form_submit(form).click()
        expect(form).to_have_count(0)
        made = h.get('admin', '/api/catalogues')['recurring_series'][0]
        assert made['ends_on'] == '2027-04-01'
        assert made['definition']['excluded_dates'] == ['2026-10-20', '2026-10-21', '2026-10-22', '2026-12-01', '2026-12-03']
        assert {(d['weekday'], d['start_time'], d['end_time']) for d in made['definition']['days']} == {(6, '16:00', '24:00'), (1, '16:00', '24:00')}
    finally:
        context.close()


def test_save_straight_after_typing_waits_for_the_check_and_saves_on_the_same_click(browser, live_server, h):
    """A slow check still running when Save is pressed (the last keystroke a moment ago) does not refuse the save."""
    types = duty_types(h)
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_recurring(page)
        page.get_by_role('button', name='New recurring session', exact=True).click()
        form = page.get_by_role('dialog', name='New recurring session')
        form.get_by_label('Duty type', exact=True).select_option(types['am']['id'])
        form.get_by_label('Location', exact=True).select_option(h.seed['locations'][0])
        form.get_by_role('checkbox', name='Monday', exact=True).check()
        page.route('**/api/catalogue/preview', lambda route: (time.sleep(1.2), route.continue_()))
        form.get_by_label('Change reason', exact=True).fill('Typed just before saving')
        form_submit(form).click()
        expect(form).to_have_count(0, timeout=15000)
        assert h.get('admin', '/api/catalogues')['recurring_series'][0]['definition']['days'][0]['weekday'] == 0
    finally:
        context.close()


def test_collection_tables_reach_down_to_the_footer(browser, live_server, h):
    duty_types(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Fictional Long List Person {n:02d}') for n in range(80)])
    for size in ({'width': 1440, 'height': 900}, {'width': 1920, 'height': 1080}):
        context, page = sign_in(browser, live_server, 'admin', viewport=size)
        try:
            from .test_browser import navigate
            navigate(page, 'people')
            scroll = page.locator('[data-table] .dt-body > .table-scroll').first
            expect(scroll).to_be_visible()
            fonts_ready(page)
            # The table settles at its full height (it ends just above the footer) after layout.
            page.wait_for_function('''()=>{const s=document.querySelector('[data-table] .dt-body > .table-scroll').getBoundingClientRect();
                const d=document.querySelector('.app-footer').getBoundingClientRect().top-s.bottom;return d>=0&&d<=64;}''')
            gap = page.evaluate('''()=>{const s=document.querySelector('[data-table] .dt-body > .table-scroll').getBoundingClientRect();
                const f=document.querySelector('.app-footer').getBoundingClientRect();return {bottom:s.bottom,footer:f.top,scrolls:document.querySelector('[data-table] .dt-body > .table-scroll').scrollHeight>s.height};}''')
            assert gap['scrolls'], gap
            assert 0 <= gap['footer'] - gap['bottom'] <= 64, gap  # the table ends just above the footer, not 300px short
            if SHOTS:
                page.screenshot(path=os.path.join(SHOTS, f"people-height-{size['width']}.png"))
        finally:
            context.close()


def test_clash_dialog_primary_is_back(browser, live_server):
    """SR-16: in the quick-edit clash dialog the safe way (Back) is the primary; saving the clash is secondary."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.evaluate("""async () => {const {clashDialog}=await window.importBundle('double_booking.js');
          void clashDialog([{code:'TIME_OVERLAP',person_name:'Alex',session_name:'A',other_session_name:'B',date:'2026-10-05',
            start:'2026-10-05T08:00:00+03:00',end:'2026-10-05T12:00:00+03:00',shift_id:'s1',other_shift_id:'s2',cancel_actions:[]}],()=>undefined);}""")
        dialog = page.get_by_role('dialog', name='Double bookings')
        expect(dialog.locator('.actions button.primary')).to_have_text('Back')
        expect(dialog.get_by_role('button', name='Save with these double bookings', exact=True)).not_to_have_class(re.compile(r'\bprimary\b'))
    finally:
        context.close()

"""Relative and empty filter values, Notion-style filter rows (docs/design/relative-filters.md, canvas v54): the one
condition builder in the collection Filter, Select by condition and rule conditions; the one value picker (search,
Relative first, the exact value); saved views keep what was chosen and move with the day. Desktop 1440 light and dark,
the phone's bottom sheet at 390. The server's clock (and the browser's) is frozen: relative values are resolved by the
server in the department calendar, never by the browser."""
from .clock_support import freeze_clock
import os
import re
from datetime import datetime, timedelta

import pytest
from playwright.sync_api import expect

from apps.api import auth, models as m
from apps.api.db import transaction
from .test_browser import add_filter, browser, live_server, navigate, row_action, sign_in, toolbar, close_popover, expect_loaded  # noqa: F401
from .test_browser import whole_list
from .test_condition_cases_client import client  # noqa: F401  (the compiled client, for logic checks in Node)
from .test_browser_rule_catalogue import bar_action

pytestmark = pytest.mark.browser
DESKTOP, PHONE = {'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}
# A month well after the app's today (requests are recorded ahead), so the frozen days are fixed whatever today is.
_soon = m.now().date() + timedelta(days=70)
MONTH = _soon.replace(day=1)
NEXT = (MONTH + timedelta(days=32)).replace(day=1)
DAY1, DAY2 = MONTH + timedelta(days=9), NEXT + timedelta(days=9)  # the 10th of each month
MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def shot(page, name):
    """RF_SHOTS=<folder>: keep a screenshot for review; otherwise nothing."""
    if os.getenv('RF_SHOTS'):
        page.screenshot(path=os.path.join(os.environ['RF_SHOTS'], name + '.png'), animations='disabled')


_CLOCK = {}


@pytest.fixture(autouse=True)
def server_clock(monkeypatch, tmp_path):
    """The API's clock (apps.api.models.now) reads a day from a file, so a forked server follows a change too. Autouse:
    set before live_server starts."""
    path = tmp_path / 'clock.txt'
    path.write_text('')
    real = m.clock_now
    monkeypatch.setattr(auth, "now", real)  # Calendar navigation must not age the ordinary test sessions.
    freeze_clock(monkeypatch, lambda: datetime.fromisoformat(path.read_text().strip()) if path.read_text().strip() else real())
    _CLOCK['path'] = path
    yield
    _CLOCK.clear()


def freeze(page, day):
    """The server's day and the browser's: 09:00 in Riyadh."""
    _CLOCK['path'].write_text(f'{day.isoformat()}T06:00:00+00:00')
    page.clock.set_fixed_time(f'{day.isoformat()}T09:00:00+03:00')


def only_relative(picker):
    """Every value offered is relative (the clock), so the list is one section and has no section header (F-R8)."""
    expect(picker.get_by_role('option').first).to_be_visible()
    expect(picker.locator('.picker-list .picker-group')).to_have_count(0)
    expect(picker.locator('.picker-list [role=option]:not(:has(.picker-glyph))')).to_have_count(0)


def record(h, person, first, last=None):
    """Leave recorded by the coordinator from `first` to `last` (both included, all day)."""
    last = last or first
    return h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
        'start': f'{first}T00:00:00+03:00', 'end': f'{last + timedelta(days=1)}T00:00:00+03:00', 'private_reason': 'Relative filter fixture'})


def rows(page):
    return page.locator('#main tr.dt-open')


def filter_row(pop, n=-1):
    rows_ = pop.locator('.cond-root > .cond-level > .cond-list > .cond-row')
    return rows_.nth(n) if n >= 0 else rows_.last


def choose_field(row, field, part=None):
    row.get_by_label('Filter on', exact=True).select_option(label=field)
    if part:
        row.get_by_label('Part', exact=True).select_option(label=part)


def value_picker(page, row):
    row.locator('.cond-value').first.click()
    picker = page.locator('.picker')
    expect(picker).to_be_visible()
    return picker


def range_words(a, b, today=DAY1):
    """What the value chip shows beside a relative date: "1–31 Oct", "29 Nov – 26 Dec" (the year only when not this one)."""
    year = str(today.year)
    y = lambda d: '' if str(d.year) == year else f' {d.year}'
    if a == b:
        return f'{a.day} {MON[a.month - 1]}{y(a)}'
    if a.year != b.year:
        return f'{a.day} {MON[a.month - 1]} {a.year} – {b.day} {MON[b.month - 1]} {b.year}'
    if a.month == b.month:
        return f'{a.day}–{b.day} {MON[b.month - 1]}{y(b)}'
    return f'{a.day} {MON[a.month - 1]} – {b.day} {MON[b.month - 1]}{y(b)}'


def errors_of(page):
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    return errors


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_filter_rows_read_as_a_sentence_with_chips_menus_and_groups(browser, live_server, h, scheme):
    """Where [field] [part] [operator] [value] [⋯]: the type's operators from the contract, Is empty removes the value,
    ⋯ duplicates, turns into a group and removes; + Add filter rule ▾ adds a rule or a group; Delete filter clears."""
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        page.emulate_media(color_scheme=scheme)
        freeze(page, DAY1)
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        pop = toolbar(page, 'Filter')
        assert pop.bounding_box()['width'] > 640  # about 780px: a row reads as one line
        row = filter_row(pop)  # an empty Filter starts with one blank rule
        expect(row.locator('.cond-where')).to_have_text('Where')
        choose_field(row, 'Dates')
        row = filter_row(pop)
        # The date's parts, and its operators: the contract's list for dates, Is empty after a divider.
        expect(row.get_by_label('Part', exact=True)).to_have_value('start')
        ops = row.get_by_label('Operator', exact=True).locator('option').all_inner_texts()
        assert ops == ['Is', 'Is before', 'Is after', 'Is on or before', 'Is on or after', 'Is between', 'Is within', 'Is empty', 'Is not empty'], ops
        expect(row.get_by_label('Operator', exact=True).locator('option[value=empty]')).to_have_attribute('data-divider', '')
        expect(page.locator('.picker')).to_be_visible()
        page.keyboard.press('Escape')  # the value picker opened on its own with the new field
        expect(page.locator('.picker')).to_have_count(0)
        # Is empty: no value control, the slot says so in plain muted text (not a button).
        row.get_by_label('Operator', exact=True).select_option('empty')
        row = filter_row(pop)
        expect(row.locator('.cond-novalue')).to_have_text('no value needed')
        expect(row.locator('.cond-value')).to_have_count(0)
        row.get_by_label('Operator', exact=True).select_option('within')
        row = filter_row(pop)
        expect(row.locator('.cond-value')).to_have_count(1)
        expect(row.locator('.cond-novalue')).to_have_count(0)
        # ⋯: Duplicate, Turn into group, Remove.
        row_action(row, 'Duplicate')
        expect(pop.locator('.cond-root .cond-line')).to_have_count(2)
        expect(filter_row(pop).get_by_label('Joiner', exact=True)).to_have_value('all')
        row_action(filter_row(pop), 'Turn into group')
        expect(pop.locator('.cond-group')).to_have_count(1)
        expect(pop.locator('.cond-group').get_by_role('button', name='+ Add filter rule')).to_be_visible()
        shot(page, f'rows-{scheme}')
        row_action(filter_row(pop), 'Remove group')
        expect(pop.locator('.cond-group')).to_have_count(0)
        add_filter(pop, group=True)
        expect(pop.locator('.cond-group')).to_have_count(1)
        # Delete filter: the same-button confirm, then one blank rule.
        delete = pop.get_by_role('button', name='Delete filter')
        delete.click()
        expect(pop.get_by_role('button', name=re.compile('^Delete every rule'))).to_be_visible()
        pop.get_by_role('button', name=re.compile('^Delete every rule')).click()
        expect(pop.locator('.cond-root .cond-line')).to_have_count(1)
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_typing_next_2_weeks_picks_a_relative_range_and_an_exact_date_stays_fixed(browser, live_server, h, scheme):
    """Typing a short form shows only its matches, the parsed one first and highlighted; Enter picks it and the chip
    shows the clock, the name and today's dates. An exact date from the calendar field is fixed (no clock)."""
    alex, jordan, _ = h.seed['people']
    record(h, alex, DAY1 + timedelta(days=3))   # inside the next 2 weeks of DAY1
    record(h, jordan, DAY1 + timedelta(days=30))  # outside
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        page.emulate_media(color_scheme=scheme)
        freeze(page, DAY1)
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        expect(rows(page).nth(1)).to_be_visible()  # both records are in before they are counted
        total = rows(page).count()
        pop = toolbar(page, 'Filter')
        choose_field(filter_row(pop), 'Dates')
        picker = page.locator('.picker')
        expect(picker).to_be_visible()  # opened on its own
        only_relative(picker)
        expect(picker.get_by_role('option', name=re.compile('^This month'))).to_be_visible()
        expect(picker.locator('.picker-foot')).to_have_text('Last / Next N always include today.')
        search = picker.get_by_role('combobox')
        search.fill('next 2 weeks')
        best = picker.locator('.picker-row.best')
        expect(best).to_contain_text('Next 2 weeks')
        shot(page, f'typing-{scheme}')
        search.press('Enter')
        expect(picker).to_have_count(0)
        chip = filter_row(pop).locator('.cond-value')
        expect(chip.locator('.cond-value-name')).to_have_text('Next 2 weeks')
        expect(chip.locator('.cond-value-dates')).to_have_text(range_words(DAY1, DAY1 + timedelta(days=13)))
        expect(chip).to_have_attribute('data-tip', re.compile('moves with today'))
        expect(rows(page)).to_have_count(1)
        # The open popover counts what the resolved value leaves (not the count before it resolved), and the button's
        # tooltip names the condition once ("Filter: Dates …", never "Filter: : …").
        expect(pop.locator('.dt-active-head .dt-pop-label')).to_have_text('Active · 1 request')
        tip = page.locator('#main button.dt-tool[data-tip^="Filter"]').first.get_attribute('data-tip')
        assert tip.startswith('Filter: Dates') and ': :' not in tip, tip
        shot(page, f'relative-{scheme}')
        # An exact date: the shared date field in the picker; fixed, no clock.
        chip.click()
        picker = page.locator('.picker')
        exact = picker.locator('.picker-exact input')
        exact.fill((DAY1 + timedelta(days=30)).isoformat())
        expect(picker).to_have_count(0)
        chip = filter_row(pop).locator('.cond-value')
        expect(chip.locator('.icon')).to_have_count(1)  # only the caret: no clock
        expect(chip.locator('.cond-value-dates')).to_have_count(0)
        expect(rows(page)).to_have_count(1)
        close_popover(page)
        assert total >= 2
        assert not errors, errors
    finally:
        context.close()


def test_choosing_or_clearing_a_value_keeps_keyboard_focus_on_its_chip(browser, live_server, h):
    """Review (#470) #5: the value chip is drawn anew after a choice; focus returns to it (not to the page), after
    Enter on a typed value and after Clear."""
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        freeze(page, DAY1)
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        pop = toolbar(page, 'Filter')
        choose_field(filter_row(pop), 'Dates')
        picker = page.locator('.picker')
        picker.get_by_role('combobox').fill('next 2 weeks')
        expect(picker.locator('.picker-row.best')).to_contain_text('Next 2 weeks')
        picker.get_by_role('combobox').press('Enter')
        chip = filter_row(pop).locator('.cond-value')
        expect(chip.locator('.cond-value-name')).to_have_text('Next 2 weeks')
        expect(chip).to_be_focused()
        page.keyboard.press('Enter')  # the focused chip opens its picker again
        picker.get_by_role('button', name='Clear', exact=True).click()
        expect(chip.locator('.cond-value-name')).to_have_text('Choose…')
        expect(chip).to_be_focused()
    finally:
        context.close()


def test_saved_view_keeps_this_month_and_moves_with_the_day(browser, live_server, h):
    """A saved view stores the relative value as chosen (the token, not dates); its menu marks it with a clock and
    today's range; on another day it shows that day's month and rows."""
    alex, jordan, _ = h.seed['people']
    record(h, alex, DAY1 + timedelta(days=2))
    record(h, jordan, DAY2 + timedelta(days=2))
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        freeze(page, DAY1)
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        pop = toolbar(page, 'Filter')
        choose_field(filter_row(pop), 'Dates')
        page.locator('.picker').get_by_role('option', name=re.compile('^This month')).click()
        expect(filter_row(pop).locator('.cond-value-name')).to_have_text('This month')
        close_popover(page)
        expect(rows(page)).to_have_count(1)
        expect(rows(page).first).to_contain_text('Alex')
        toolbar(page, 'Saved views').get_by_role('menuitem', name='New view…').click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('Starting this month')
        # The save forgets resolved values; hold the new answers so the menu opens before them (CI's slow case).
        held, gate = [], {'open': False}
        page.route('**/api/conditions/resolve-operands', lambda r: r.continue_() if gate['open'] else held.append(r))
        dialog.get_by_role('button', name='Save view', exact=True).click()
        expect(dialog).not_to_be_visible()
        [view] = [v for v in h.get('coordinator', '/api/table-views?table=requests')['views'] if v['name'] == 'Starting this month']
        [line] = view['definition']['conditions']['conditions']
        assert line['field'] == 'start' and line['value'] == {'kind': 'relative', 'token': 'this_month'}, line
        last = (NEXT - timedelta(days=1))
        menu = toolbar(page, 'Saved views')
        item = menu.get_by_role('menuitemradio', name=re.compile('^Starting this month'))
        expect(item).to_contain_text('moves with the day')
        item.evaluate("n => n.dataset.beforeSavedRefresh = '1'")
        delete = menu.get_by_role('menuitem', name='Delete “Starting this month”', exact=True)
        delete.focus()
        gate['open'] = True
        for r in held:
            r.continue_()
        expect(item).to_contain_text(range_words(MONTH, last))  # the open menu fills in today's dates
        expect(menu.locator('[data-before-saved-refresh]')).to_have_count(0)
        expect(delete).to_be_focused()  # the refreshed menu keeps focus on the same action
        assert any(v['id'] == view['id'] for v in h.get('coordinator', '/api/table-views?table=requests')['views'])
        delete.press('Escape')
        expect(menu).to_have_count(0)
        expect(page.get_by_role('button', name='Saved views: Starting this month', exact=True)).to_be_focused()
        # Another day: the same view now means next month.
        freeze(page, DAY2)
        catalogue = []
        page.route(re.compile(r'.*/api/table-views\?table=requests$'), lambda route: catalogue.append(route))
        with page.expect_request(re.compile(r'.*/api/table-views\?table=requests$')):
            page.reload()
        expect_loaded(page, 'requests')
        assert catalogue, 'the saved-view catalogue was held after reload'
        assert any(v['id'] == view['id'] for v in h.get('coordinator', '/api/table-views?table=requests')['views'])
        menu = toolbar(page, 'Saved views')
        item = menu.get_by_role('menuitemradio', name=re.compile('^Starting this month'))
        expect(item).to_have_count(0)
        menu.get_by_role('menuitemradio', name=re.compile('^All requests')).press('End')
        # 176a12f82: while the catalogue loads, the view shown is named by its id, so both defaults are offered (Set as
        # department default is last). The personal view removes the department action when loaded; keep focus on the
        # surviving personal action.
        department_default = menu.get_by_role('menuitem', name='Set as department default', exact=True)
        expect(department_default).to_be_focused()
        department_default.press('ArrowUp')
        my_default = menu.get_by_role('menuitem', name='Set as my default', exact=True)
        expect(my_default).to_be_focused()
        for route in catalogue:
            route.continue_()
        expect(item).to_contain_text(range_words(NEXT, (NEXT + timedelta(days=32)).replace(day=1) - timedelta(days=1), DAY2))
        expect(department_default).to_have_count(0)  # a personal view is never the department default
        expect(my_default).to_be_focused()  # New choices/actions must not move keyboard ownership to another menu item.
        item.click()
        expect(rows(page)).to_have_count(1)
        expect(rows(page).first).to_contain_text('Jordan')
        assert not errors, errors
    finally:
        context.close()


def test_viewer_values_appear_only_where_they_mean_something(browser, live_server, h):
    """Select by condition offers the viewer's own values from the server's catalogue: a credentialer with no person of
    their own sees no Me and no Clinics I can work at; linked to a person with clinic permissions, they appear, and
    Me counts that one person."""
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        page.get_by_role('button', name='Selection options', exact=True).first.click()
        page.get_by_role('menu', name='Selection options').get_by_role('menuitem', name='Select by condition…').click()
        pop = page.get_by_role('dialog', name='Select by condition')
        row = pop.locator('.cond-line').last
        row.get_by_label('Filter on', exact=True).select_option(label='Person')
        picker = page.locator('.picker')
        expect(picker).to_be_visible()
        expect(picker.get_by_role('option', name=re.compile('^Me( ·|$)'))).to_have_count(0)
        page.keyboard.press('Escape')
        row = pop.locator('.cond-line').last
        row.get_by_label('Filter on', exact=True).select_option(label='Can work at')
        expect(picker.get_by_role('option', name=re.compile('^My department'))).to_be_visible()
        expect(picker.get_by_role('option', name=re.compile('^Clinics I can work at'))).to_have_count(0)
        page.keyboard.press('Escape')
    finally:
        context.close()
    with transaction(h.app.state.engine, write=True) as db:
        identity = db.get(m.Identity, h.seed['identities']['credentialer'])
        identity.person_id = h.seed['people'][1]
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        page.get_by_role('button', name='Selection options', exact=True).first.click()
        page.get_by_role('menu', name='Selection options').get_by_role('menuitem', name='Select by condition…').click()
        pop = page.get_by_role('dialog', name='Select by condition')
        row = pop.locator('.cond-line').last
        row.get_by_label('Filter on', exact=True).select_option(label='Can work at')
        picker = page.locator('.picker')
        # The server's hint says what it means now ("Clinics I can work at · 2 clinics").
        expect(picker.get_by_role('option', name=re.compile('^Clinics I can work at · .+'))).to_be_visible()
        page.keyboard.press('Escape')
        row = pop.locator('.cond-line').last
        row.get_by_label('Filter on', exact=True).select_option(label='Person')
        me = picker.get_by_role('option', name=re.compile('^Me( ·|$)'))
        expect(me).to_be_visible()
        # Relative first, and it stands alone: choosing Me clears an exact person, and the other way round.
        expect(picker.locator('.picker-group').first).to_contain_text('Relative')
        picker.get_by_role('option', name=h.get('admin', '/api/catalogues')['people'][0]['display_name'], exact=True).check()
        me.check()
        expect(picker.locator('input[type=checkbox]:checked')).to_have_count(1)
        page.keyboard.press('Escape')
        expect(pop.locator('.cond-line').last.locator('.rp.relative')).to_have_text(re.compile('^Me · 1 person'))
        expect(pop.locator('.bulk-condition-count')).to_have_text(re.compile(r'^1 person matches'))
    finally:
        context.close()


def test_rule_conditions_offer_only_dates_relative_to_the_duty(browser, live_server, h):
    """A rule line's Where: Duty date lists only "relative to the duty" values (and an exact date), with the footer
    saying why; today's values (Today, This month) and the viewer's are not offered."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line
    flag(h)
    rule = catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Default')])
    line_id = rule['definition']['rule']['lines'][0]['id']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, f'rules/duties_per_week/{line_id}')
        panel = page.locator('.rc-line-panel')
        expect(panel.locator('.rc-says')).to_contain_text('This line says')
        where = panel.locator('.rc-lp-section').nth(1)
        add_filter(where)
        row = where.locator('.cond-line').last
        row.get_by_label('Filter on', exact=True).select_option(label='Duty date')
        picker = page.locator('.picker')
        expect(picker).to_be_visible()
        only_relative(picker)
        expect(picker.get_by_role('option', name=re.compile("^The duty's week"))).to_be_visible()
        expect(picker.get_by_role('option', name=re.compile('^(Today|This month)'))).to_have_count(0)
        expect(picker.locator('.picker-foot')).to_contain_text('only dates relative to the duty')
        expect(picker.locator('.picker-exact input')).to_be_visible()
        # "7 days before the duty" is understood here without the server's calendar.
        picker.get_by_role('combobox').fill('7 days before the duty')
        expect(picker.locator('.picker-row.best')).to_contain_text('Within 7 days before the duty')
        picker.get_by_role('combobox').press('Enter')
        chip = where.locator('.cond-value')
        expect(chip.locator('.cond-value-name')).to_have_text('Within 7 days before the duty')
        expect(chip).to_have_attribute('data-tip', re.compile("each duty's own day"))
        shot(page, 'rule-duty')
    finally:
        context.close()


def test_phone_staff_requests_value_picker_is_a_bottom_sheet(browser, live_server, h):
    """Staff on a phone: the same picker as a bottom sheet with 48px rows, Relative first, Clear and Done."""
    context, page = sign_in(browser, live_server, 'staff', viewport=PHONE)
    try:
        page.emulate_media(color_scheme='dark')
        freeze(page, DAY1)
        navigate(page, 'requests')
        pop = toolbar(page, 'Filter')
        choose_field(filter_row(pop), 'Dates')
        sheet = page.locator('.picker.sheet')
        expect(sheet).to_be_visible()
        only_relative(sheet)
        assert sheet.get_by_role('option').first.bounding_box()['height'] >= 47
        expect(sheet.get_by_role('button', name='Clear', exact=True)).to_be_visible()
        expect(sheet.get_by_role('button', name='Done', exact=True)).to_be_visible()
        shot(page, 'phone-sheet')
        sheet.get_by_role('option', name=re.compile('^This week')).click()
        expect(filter_row(pop).locator('.cond-value-name')).to_have_text('This week')
        # Clear empties the value again.
        filter_row(pop).locator('.cond-value').click()
        page.locator('.picker.sheet').get_by_role('button', name='Clear', exact=True).click()
        expect(filter_row(pop).locator('.cond-value-name')).to_have_text('Choose…')
    finally:
        context.close()


def test_keyboard_path_through_rows_menus_and_pickers(browser, live_server, h):
    """Keyboard only: + Add filter rule ▾ opens its menu (↓, Enter), Esc closes only the open menu or picker, Enter on the
    value chip opens the picker, typing and Enter pick, Backspace in an empty search takes back the last value chosen."""
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        freeze(page, DAY1)
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        pop = toolbar(page, 'Filter')
        adder = pop.get_by_role('button', name='+ Add filter rule')
        adder.focus()
        page.keyboard.press('Enter')
        menu = page.get_by_role('menu', name='Add', exact=True)  # a child menu of the Filter popover (dom.ts menu())
        expect(menu.get_by_role('menuitem', name='Add filter rule')).to_be_focused()
        page.keyboard.press('ArrowDown')
        expect(menu.get_by_role('menuitem', name='Add filter group')).to_be_focused()
        page.keyboard.press('Escape')
        expect(menu).to_have_count(0)
        expect(pop).to_be_visible()  # Esc closed the menu, not the Filter
        expect(adder).to_be_focused()
        row = filter_row(pop)  # the blank rule an empty Filter starts with
        # Type: choose two values, then Backspace in the empty search takes the last one back.
        row.get_by_label('Filter on', exact=True).select_option(label='Type')
        picker = page.locator('.picker')
        expect(picker).to_be_visible()
        page.keyboard.press('Enter')
        page.keyboard.press('ArrowDown')
        page.keyboard.press('Enter')
        expect(picker.locator('input[type=checkbox]:checked')).to_have_count(2)
        page.keyboard.press('Backspace')
        expect(picker.locator('input[type=checkbox]:checked')).to_have_count(1)
        page.keyboard.press('Escape')
        expect(picker).to_have_count(0)
        expect(pop).to_be_visible()
        # A date by keyboard: Dates, Enter on the value chip, type "today", Enter.
        add_filter(pop)
        row = filter_row(pop)
        choose_field(row, 'Dates')
        expect(page.locator('.picker')).to_be_visible()  # opened on its own
        page.keyboard.press('Escape')
        expect(page.locator('.picker')).to_have_count(0)
        chip = filter_row(pop).locator('.cond-value')
        chip.focus()
        page.keyboard.press('Enter')
        page.keyboard.type('today')
        expect(page.locator('.picker .picker-row.best')).to_contain_text('Today')
        page.keyboard.press('Enter')
        expect(filter_row(pop).locator('.cond-value-name')).to_have_text('Today')
        expect(filter_row(pop).locator('.cond-value-dates')).to_have_text(range_words(DAY1, DAY1))
    finally:
        context.close()


def test_people_i_approve_on_the_requests_person_column_is_resolved_by_the_server(browser, live_server, h):
    """An approver's Requests: the Person column offers People I approve (with the server's count) and filters by the
    ids the server resolves; a coordinator, who approves nobody, is not offered it."""
    alex, jordan, _ = h.seed['people']
    record(h, alex, DAY1 + timedelta(days=2))
    record(h, jordan, DAY1 + timedelta(days=4))
    approve = h.client('approver').post('/api/conditions/resolve-operands', json={'table_key': 'requests', 'context': 'filter',
        'operands': [{'field': 'person', 'operand': {'kind': 'relative', 'token': 'people_i_approve'}}]}).json()['results'][0]
    for role, offered in (('coordinator', False), ('approver', True)):
        context, page = sign_in(browser, live_server, role, viewport=DESKTOP)
        try:
            freeze(page, DAY1)
            navigate(page, 'requests')
            whole_list(page)  # the recorded leave is decided: not in the decider's Waiting for you (QA-039)
            pop = toolbar(page, 'Filter')
            filter_row(pop).get_by_label('Filter on', exact=True).select_option(label='Person')
            picker = page.locator('.picker')
            expect(picker).to_be_visible()
            option = picker.get_by_role('option', name=re.compile('^People I approve'))
            if not offered:
                expect(picker.get_by_role('option', name=re.compile('Taylor|Alex|Jordan')).first).to_be_visible()
                expect(option).to_have_count(0)
                continue
            expect(option).to_have_accessible_name(f"People I approve · {approve['display']}")
            option.check()
            page.keyboard.press('Escape')
            expect(filter_row(pop).locator('.rp.relative')).to_contain_text('People I approve')
            close_popover(page)
            expected = sum(1 for p in (alex, jordan) if p in approve['ids'])
            expect(rows(page)).to_have_count(expected)
        finally:
            context.close()


def test_saving_a_view_with_the_lists_own_columns_status_is_not_empty_and_type_is_not(browser, live_server, h):
    """A view filtering the list's own columns (Status is not empty, Type is not Swap) saves, keeps them as chosen and
    shows the same rows when chosen again."""
    record(h, h.seed['people'][0], DAY1 + timedelta(days=2))
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        freeze(page, DAY1)
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        pop = toolbar(page, 'Filter')
        row = filter_row(pop)
        row.get_by_label('Filter on', exact=True).select_option(label='Status')
        expect(page.locator('.picker')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.locator('.picker')).to_have_count(0)
        filter_row(pop).get_by_label('Operator', exact=True).select_option('not_empty')
        add_filter(pop)
        filter_row(pop).get_by_label('Filter on', exact=True).select_option(label='Type')
        picker = page.locator('.picker')
        picker.get_by_role('option', name=re.compile('^Swap')).check()
        page.keyboard.press('Escape')
        filter_row(pop).get_by_label('Operator', exact=True).select_option('none')
        close_popover(page)
        expect(rows(page).first).to_be_visible()  # the recorded leave: it has a status and is no swap
        shown = rows(page).count()
        toolbar(page, 'Saved views').get_by_role('menuitem', name='New view…').click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('With a status, no swaps')
        dialog.get_by_role('button', name='Save view', exact=True).click()
        expect(dialog).not_to_be_visible()
        [view] = [v for v in h.get('coordinator', '/api/table-views?table=requests')['views'] if v['name'] == 'With a status, no swaps']
        lines = view['definition']['conditions']['conditions']
        assert {(x['field'], x['op']) for x in lines} == {('status', 'not_empty'), ('type', 'none')}, lines
        page.reload()
        expect_loaded(page, 'requests')
        toolbar(page, 'Saved views').get_by_role('menuitemradio', name=re.compile('^With a status, no swaps')).click()
        expect(rows(page)).to_have_count(shown)
    finally:
        context.close()


def test_a_last_day_filter_compares_the_day_shown_not_the_day_stored(browser, live_server, h):
    """A Last day (ends_on, stored as the day after) is filtered as shown: a permission shown "Last day 31 Dec" matches
    Is 31 Dec and Is on or after 31 Dec, not Is after 31 Dec; one whose last day is today matches Is Today."""
    from datetime import date
    from .test_browser_bulk_changes import open_their_permissions
    alex, jordan = h.seed['people'][:2]
    dec31 = date(DAY1.year, 12, 31)
    clinic = h.configure('services', {'name': 'Last day clinic', 'starts_on': '2020-01-01'})['record']
    permit = lambda who, last: h.command('credentialer', 'eligibility.create', {'person_id': who, 'service_id': clinic['id'],
        'starts_on': (DAY1 - timedelta(days=60)).isoformat(), 'ends_on': (last + timedelta(days=1)).isoformat()})
    permit(alex, dec31)  # shown 31 Dec, stored 1 Jan
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        freeze(page, DAY1)
        open_their_permissions(page)
        ours = page.locator('#main tbody tr', has_text='Last day clinic')
        expect(ours).to_have_count(1)
        expect(ours).to_contain_text(f'31 Dec {DAY1.year}')
        pop = toolbar(page, 'Filter')
        choose_field(filter_row(pop), 'Last day')
        picker = page.locator('.picker')
        expect(picker).to_be_visible()
        picker.locator('.picker-exact input').fill(dec31.isoformat())
        expect(picker).to_have_count(0)
        operator = lambda op: filter_row(pop).get_by_label('Operator', exact=True).select_option(op)

        def day(d):
            value_picker(page, filter_row(pop)).locator('.picker-exact input').fill(d.isoformat())
            expect(page.locator('.picker')).to_have_count(0)
        # Each step changes the result, so each one is seen applied.
        operator('after')
        expect(ours).to_have_count(0)  # Is after 31 Dec: no
        operator('on_or_after')
        expect(ours).to_have_count(1)  # Is on or after 31 Dec: yes
        operator('is')
        day(dec31 + timedelta(days=1))
        expect(ours).to_have_count(0)  # Is 1 Jan (the day stored): no
        day(dec31)
        expect(ours).to_have_count(1)  # Is 31 Dec (the day shown): yes
        # Relative: a permission whose last day is today is "Last day is Today".
        permit(jordan, DAY1)
        value_picker(page, filter_row(pop)).get_by_role('option', name=re.compile('^Today')).click()
        close_popover(page)
        page.reload()
        today_row = page.locator('#main tbody tr', has_text='Last day clinic')
        expect(today_row).to_have_count(1)
        expect(today_row).to_contain_text('Jordan Lee')
        expect(today_row).to_contain_text(f'{DAY1.day} {MON[DAY1.month - 1]} {DAY1.year}')
        assert not errors, errors
    finally:
        context.close()


def test_select_by_condition_and_the_list_filter_agree_on_a_last_day(browser, live_server, h):
    """B1: "Last day is 31 Dec" means the day shown, the same for the list's Filter and for Select by condition (one
    meaning, on the server): both find the permission shown with last day 31 Dec, and only it."""
    from datetime import date
    from .test_browser_bulk_changes import open_their_permissions, caret, bar
    alex, jordan = h.seed['people'][:2]
    dec31 = date(DAY1.year, 12, 31)
    clinic = h.configure('services', {'name': 'Last day clinic', 'starts_on': '2020-01-01'})['record']
    for who, last in ((alex, dec31), (jordan, dec31 + timedelta(days=1))):  # shown 31 Dec (stored 1 Jan); shown 1 Jan
        h.command('credentialer', 'eligibility.create', {'person_id': who, 'service_id': clinic['id'],
            'starts_on': (DAY1 - timedelta(days=60)).isoformat(), 'ends_on': (last + timedelta(days=1)).isoformat()})
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    try:
        freeze(page, DAY1)
        open_their_permissions(page)
        main = page.locator('#main')
        ours = main.locator('tbody tr', has_text='Last day clinic')
        expect(ours).to_have_count(2)
        # The list's Filter.
        pop = toolbar(page, 'Filter')
        choose_field(filter_row(pop), 'Last day')
        page.locator('.picker .picker-exact input').fill(dec31.isoformat())
        expect(page.locator('.picker')).to_have_count(0)
        expect(ours).to_have_count(1)
        expect(ours).to_contain_text('Alex Morgan')
        pop.get_by_role('button', name='Delete filter').click()
        pop.get_by_role('button', name=re.compile('^Delete every rule')).click()
        close_popover(page)
        expect(ours).to_have_count(2)
        # Select by condition, on the same list.
        caret(main).get_by_role('menuitem', name='Select by condition…').click()
        sel = page.get_by_role('dialog', name='Select by condition')
        choose_field(filter_row(sel), 'Last day')
        page.locator('.picker .picker-exact input').fill(dec31.isoformat())
        expect(page.locator('.picker')).to_have_count(0)
        expect(sel.locator('.bulk-condition-count')).to_contain_text(re.compile(r'^1 '))
        sel.get_by_role('button', name='Select', exact=True).click()
        expect(bar(page)).to_contain_text('1')
        expect(ours.filter(has_text='Alex Morgan').locator('.bulk-check')).to_be_checked()
        expect(ours.filter(has_text='Jordan Lee').locator('.bulk-check')).not_to_be_checked()
    finally:
        context.close()


def test_select_all_and_a_group_box_give_way_to_no_relative_value(browser, live_server, h):
    """Review (#470) #6: with Me chosen, Select all (and a group's box) ticks the exact people and clears Me, as choosing
    one person does: what is ticked is what the condition keeps (never Me plus everyone shown, Me saved)."""
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['credentialer']).person_id = h.seed['people'][1]
    people = h.get('admin', '/api/catalogues')['people']
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        page.get_by_role('button', name='Selection options', exact=True).first.click()
        page.get_by_role('menu', name='Selection options').get_by_role('menuitem', name='Select by condition…').click()
        pop = page.get_by_role('dialog', name='Select by condition')
        pop.locator('.cond-line').last.get_by_label('Filter on', exact=True).select_option(label='Person')
        picker = page.locator('.picker')
        me = picker.get_by_role('option', name=re.compile('^Me( ·|$)'))
        me.check()
        expect(me).to_be_checked()
        picker.get_by_role('button', name=re.compile(r'^Select all \d+')).click()
        expect(me).not_to_be_checked()
        expect(picker.locator('[role=option][aria-checked=true]:not([aria-label^="All in"])')).to_have_count(len(people))
        # A group's box does the same, and the condition keeps exactly the people ticked.
        picker.get_by_role('button', name=re.compile(r'^Clear')).click()
        me.check()
        picker.locator('.picker-group-toggle').first.click()
        expect(me).not_to_be_checked()
        ticked = picker.locator('[role=option][aria-checked=true]:not([aria-label^="All in"])').count()
        assert ticked > 0
        page.keyboard.press('Escape')
        line = pop.locator('.cond-line').last
        expect(line.locator('.rp.relative')).to_have_count(0)
        expect(pop.locator('.bulk-condition-count')).to_have_text(re.compile(rf'^{ticked} (people|person) match'))
    finally:
        context.close()


CLIENT_CHECKS = r"""
const [dir] = process.argv.slice(1);
globalThis.document = {cookie: ''};
let refuse = false;
globalThis.fetch = async (path, init) => refuse
  ? new Response(JSON.stringify({error: {code: 'UNAVAILABLE', message: 'Resolving is unavailable', details: null}}), {status: 503, headers: {'Content-Type': 'application/json'}})
  : new Response(JSON.stringify({results: JSON.parse(init.body).operands.map(() => ({label: 'Me', display: 'Me', ids: ['p1']}))}), {status: 200, headers: {'Content-Type': 'application/json'}});
// A browser set to Turkish: its default toLocaleLowerCase() lowers "I" to dotless "ı" and "İ" to plain "i".
const lowerHere = String.prototype.toLocaleLowerCase;
String.prototype.toLocaleLowerCase = function (locale) { return lowerHere.call(this, locale ?? 'tr'); };
const c = await import(new URL('conditions.js', dir));
const out = {};
const typed = (field, op, value) => ({field, op, values: [], value});
out.text_dotted_capital_is_i = c.lineMatches(typed('name', 'is', {kind: 'absolute', value: 'i'}), ['İ'], 'text');
out.text_dotted_capital_is_i_dot = c.lineMatches(typed('name', 'is', {kind: 'absolute', value: 'i̇'}), ['İ'], 'text');
out.text_capital_contains_i = c.lineMatches(typed('name', 'contains', {kind: 'absolute', value: 'i'}), ['NIGHT'], 'text');
out.legacy_tag_is_not_upper = c.lineMatches({field: 'tags', op: 'none', values: ['NIGHT']}, ['NIGHT']);
out.typed_tag_is_not_lower = c.lineMatches(typed('tags', 'none', {kind: 'absolute', value: ['night']}), ['NIGHT']);
out.typed_tag_is_upper = c.lineMatches(typed('tags', 'any', {kind: 'absolute', value: ['Night']}), ['NIGHT']);
out.typed_all_of_nothing = c.lineMatches(typed('tags', 'all', {kind: 'absolute', value: []}), ['a']);
out.empty_reads_raw_values = c.lineMatches({field: 'category_ids', op: 'empty', values: []}, ['No current rank'], undefined, undefined, []);
out.not_empty_reads_raw_values = c.lineMatches({field: 'category_ids', op: 'not_empty', values: []}, ['No current rank'], undefined, undefined, []);
out.literal_typed = c.literalValues(typed('person_ids', 'any', {kind: 'absolute', value: ['p1', 'p2']}));
out.literal_raw = c.literalValues(typed('person_ids', 'any', 'p3'));
out.literal_relative = c.literalValues(typed('person_ids', 'any', {kind: 'relative', token: 'me'}));
const owned = c.ownLines([typed('cohort_ids', 'any', {kind: 'absolute', value: ['c1']}), typed('rotation_type_ids', 'any', {kind: 'relative', token: 'my_rotation_types'}),
  {field: 'person_ids', op: 'any', values: ['p9']}], ['cohort_ids', 'rotation_type_ids']);
out.own_values = owned.values('cohort_ids');
out.own_kept = owned.kept.map(x => x.field);
const scope = {context: 'filter'}, me = {kind: 'relative', token: 'me'};
refuse = true;
await c.resolveOperands([{field: 'person_ids', operand: me}], scope);
out.failure = c.resolveFailure('person_ids', me, scope);
refuse = false;
await c.retryResolve([{field: 'person_ids', operand: me}], scope);
out.after_retry = [c.resolveFailure('person_ids', me, scope), !!c.resolvedNow('person_ids', me, scope)];
c.forgetResolved();
out.after_forget = !!c.resolvedNow('person_ids', me, scope);
console.log(JSON.stringify(out));
"""


def test_the_client_reads_literal_operands_tags_empties_and_failures_as_the_server_does(client):
    """Review (#470) #4-#7 and the reader handoff, in the compiled client: one literal reader (plain, absolute or raw;
    relative names none), tags compared lower-cased both sides, Has all of nothing is false, Is empty reads raw values
    (never a placeholder), protected time keeps a relative own line, a refused resolution is recorded (Retry clears it)
    and resolved values can be forgotten."""
    import json as _json
    import subprocess
    node, folder = client
    out = _json.loads(subprocess.run([node, '--input-type=module', '-e', CLIENT_CHECKS, folder.as_uri() + '/'],
                                     capture_output=True, text=True, check=True, timeout=20).stdout)
    assert out['legacy_tag_is_not_upper'] is False and out['typed_tag_is_not_lower'] is False and out['typed_tag_is_upper'] is True, out
    assert out['typed_all_of_nothing'] is False, out
    # Lowered as the server's lower() does, whatever the browser's language (#470 #3).
    assert out['text_dotted_capital_is_i'] is False and out['text_dotted_capital_is_i_dot'] is True, out
    assert out['text_capital_contains_i'] is True, out
    assert out['empty_reads_raw_values'] is True and out['not_empty_reads_raw_values'] is False, out
    assert out['literal_typed'] == ['p1', 'p2'] and out['literal_raw'] == ['p3'] and out['literal_relative'] == [], out
    assert out['own_values'] == ['c1'] and out['own_kept'] == ['rotation_type_ids', 'person_ids'], out
    assert 'unavailable' in out['failure'].lower(), out
    assert out['after_retry'] == ['', True] and out['after_forget'] is False, out



def people_filter(page, field, op=None):
    navigate(page, 'people')
    pop = toolbar(page, 'Filter')
    row = filter_row(pop)
    choose_field(row, field)
    row = filter_row(pop)
    if op:
        row.get_by_label('Operator', exact=True).select_option(op)
    return pop, filter_row(pop)


def open_values(page, row):
    """The value picker, which opens on its own once a field is chosen."""
    picker = page.locator('.picker')
    expect(picker).to_be_visible()
    return picker


@pytest.mark.parametrize('field,text', [('Current rank', 'No current rank'), ('Clinical eligibility', 'No current eligibility')])
def test_is_empty_finds_people_with_none_not_the_placeholder_shown(browser, live_server, h, field, text):
    """Review (#470) #5: a person with no rank (or no clinic eligibility) shows a placeholder in the list; Current rank
    (Clinical eligibility) Is empty keeps that person (matching reads the raw empty value), Is not empty hides them."""
    h.configure('people', {'display_name': 'Nobody Assigned', 'short_code': 'NBA'})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        people_filter(page, field, 'empty')
        close_popover(page)
        main = page.locator('#main')
        expect(main.locator('tbody tr', has_text='Nobody Assigned')).to_have_count(1)
        expect(main.locator('tbody tr', has_text='Nobody Assigned')).to_contain_text(text)
        pop = toolbar(page, 'Filter')
        filter_row(pop).get_by_label('Operator', exact=True).select_option('not_empty')
        close_popover(page)
        expect(main.locator('tbody tr', has_text='Nobody Assigned')).to_have_count(0)
    finally:
        context.close()


def test_tags_is_not_hides_an_uppercase_tag(browser, live_server, h):
    """Review (#470) #6: a person tagged NIGHT is hidden by Tags Is not NIGHT (tags compared lower-cased on both sides, as
    the server's lower())."""
    people = {p['id']: p for p in h.get('admin', '/api/catalogues')['people']}
    alex = people[h.seed['people'][0]]
    h.configure('tags', {'name': 'NIGHT', 'labels': ['people']})
    h.command('admin', 'catalogue.update', {'catalogue': 'people', 'value': {**{k: alex[k] for k in ('display_name', 'active')}, 'tags': ['NIGHT']},
              'reason': 'Test setup'}, alex['id'], alex['revision'])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        # The value picker opens on its own once the field is chosen; choosing the operator redraws the line under it.
        # So: close it, choose Is not, then open the value from the line as a person does.
        pop, row = people_filter(page, 'Tags')
        picker = open_values(page, row)
        page.keyboard.press('Escape')
        expect(picker).to_have_count(0)
        row.get_by_label('Operator', exact=True).select_option('none')
        filter_row(pop).locator('.pick-button, .cond-value').first.click()
        expect(picker).to_be_visible()
        picker.get_by_role('option', name=re.compile('NIGHT')).first.check()
        close_popover(page)
        main = page.locator('#main')
        expect(main.locator('tbody tr', has_text='Jordan Lee')).to_have_count(1)
        expect(main.locator('tbody tr', has_text='Alex Morgan')).to_have_count(0)
    finally:
        context.close()


def link_credentialer(h, person):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['credentialer']).person_id = person


def test_a_relative_value_the_server_cannot_resolve_says_so_with_retry(browser, live_server, h):
    """Review (#470) #4: Clinical eligibility, Clinics I can work at cannot be resolved (the server answers 503): the list says so
    with Retry, and Select all matching waits; Retry once the server answers resolves it."""
    link_credentialer(h, h.seed['people'][1])
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    failing = {'on': True}
    body = '{"error":{"code":"UNAVAILABLE","message":"Resolving is unavailable","details":null}}'
    page.route('**/api/conditions/resolve-operands', lambda route: route.fulfill(status=503, content_type='application/json', body=body)
               if failing['on'] else route.continue_())
    try:
        pop, row = people_filter(page, 'Clinical eligibility')
        open_values(page, row).get_by_role('option', name=re.compile('^Clinics I can work at')).click()
        close_popover(page)
        main = page.locator('#main')
        note = main.locator('.dt-unresolved')
        expect(note).to_contain_text('could not be worked out')
        expect(note).to_contain_text('Resolving is unavailable')
        main.get_by_role('button', name='Selection options', exact=True).first.click()
        expect(page.get_by_role('menu', name='Selection options').get_by_role('menuitem', name=re.compile('^Select all'))).to_be_disabled()
        page.keyboard.press('Escape')
        failing['on'] = False
        note.get_by_role('button', name='Retry').click()
        expect(note).to_be_hidden()
    finally:
        context.close()


def test_selection_by_matches_waits_while_a_relative_value_is_resolving(browser, live_server, h):
    """Review (#470, BLOCKING): while Clinics I can work at is still being resolved the list shows everyone, so Select
    all shown or matching, a group's box, Invert and Ctrl A wait, and the bar offers no action; a hand pick stays.
    Once the answer arrives, Select all matching takes only the matches: a person outside the viewer's clinics is never
    selected, nor sent to Show their…."""
    viewer = h.seed['people'][1]
    link_credentialer(h, viewer)
    outside = h.configure('people', {'display_name': 'Outside Clinics', 'short_code': 'OUT'})['record']
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    held = []
    sent = []
    page.on('request', lambda r: sent.append(r.post_data or '') if '/api/bulk/' in r.url and r.method == 'POST' else None)
    try:
        pop, row = people_filter(page, 'Clinical eligibility')
        open_values(page, row).get_by_role('option', name=re.compile('^Clinics I can work at')).check()
        close_popover(page)
        main = page.locator('#main')
        expect(main.locator('tbody tr', has_text='Outside Clinics')).to_have_count(0)
        # Opened again (the list keeps its filter), its answer held: the list's matches are unknown until it comes.
        page.route('**/api/conditions/resolve-operands', lambda route: held.append(route))
        with page.expect_request('**/api/conditions/resolve-operands'):
            page.reload()
        expect_loaded(page, 'people')
        expect(main.locator('tbody tr', has_text='Outside Clinics')).to_have_count(1)  # unknown yet: shown
        everyone = main.get_by_role('checkbox', name=re.compile('^Select all .* shown'))
        expect(everyone).to_be_disabled()
        main.get_by_role('button', name='Selection options', exact=True).first.click()
        menu = page.get_by_role('menu', name='Selection options')
        expect(menu.get_by_role('menuitem', name=re.compile('^Select all'))).to_be_disabled()
        expect(menu.get_by_role('menuitem', name='Invert selection')).to_be_disabled()
        page.keyboard.press('Escape')
        jordan = main.locator('tbody tr', has_text='Jordan Lee').locator('.bulk-check')
        jordan.check()  # a hand pick
        main.locator('tbody tr', has_text='Jordan Lee').locator('td').nth(1).click(position={'x': 2, 'y': 2})
        page.keyboard.press('Control+a')
        bar = page.locator('.selection-bar')
        expect(bar).to_contain_text('Waiting for the filter to be worked out')
        expect(bar.locator('.sel-act')).to_have_count(0)
        expect(main.locator('tbody tr', has_text='Outside Clinics').locator('.bulk-check')).not_to_be_checked()
        for route in held:
            route.continue_()
        page.unroute('**/api/conditions/resolve-operands')
        expect(main.locator('tbody tr', has_text='Outside Clinics')).to_have_count(0)
        expect(jordan).to_be_checked()  # the hand pick stayed
        expect(everyone).to_be_enabled()
        main.get_by_role('button', name='Selection options', exact=True).first.click()
        page.get_by_role('menu', name='Selection options').get_by_role('menuitem', name=re.compile('^Select all')).click()
        shown = main.locator('tbody tr .bulk-check')
        expect(shown.first).to_be_checked()
        assert all(b.is_checked() for b in shown.all())
        expect(bar).to_contain_text(str(shown.count()))
        # The bar folds its last secondary actions into ⋯ More when they do not fit beside the count (selection_bar.ts).
        bar_action(page, 'Show their…')
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Clinic permissions')).click()
        expect(page.locator('.bulk-crumb')).to_contain_text('People')
        expect(page.locator('#main tbody tr').first).to_be_visible()
        assert sent and not any(outside['id'] in body for body in sent), sent
        expect(page.locator('#main tbody tr', has_text='Outside Clinics')).to_have_count(0)
    finally:
        context.close()


def test_my_clinics_follow_a_permission_change_without_a_reload(browser, live_server, h):
    """Review (#470) #7: Clinical eligibility, Clinics I can work at is resolved once; the viewer then gains a clinic (changed by
    someone else): the list asks again when the change arrives, and a person at that clinic now matches."""
    viewer = h.seed['people'][1]
    link_credentialer(h, viewer)
    clinic = h.configure('services', {'name': 'Newly permitted clinic', 'starts_on': '2020-01-01'})['record']
    other = h.configure('people', {'display_name': 'Only New Clinic', 'short_code': 'ONC'})['record']
    h.command('admin', 'eligibility.create', {'person_id': other['id'], 'service_id': clinic['id'], 'starts_on': '2026-01-01'})
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    try:
        pop, row = people_filter(page, 'Clinical eligibility')
        open_values(page, row).get_by_role('option', name=re.compile('^Clinics I can work at')).check()
        close_popover(page)
        main = page.locator('#main')
        expect(main.locator('tbody tr', has_text='Jordan Lee')).to_have_count(1)
        expect(main.locator('tbody tr', has_text='Only New Clinic')).to_have_count(0)
        h.command('admin', 'eligibility.create', {'person_id': viewer, 'service_id': clinic['id'], 'starts_on': '2026-01-01'})
        expect(main.locator('tbody tr', has_text='Only New Clinic')).to_have_count(1, timeout=30_000)
    finally:
        context.close()



def test_a_rule_line_for_one_person_named_by_a_typed_value_reads_with_their_name(browser, live_server, h):
    """Reader handoff: a catalogue line whose who names one person by a typed operand ({kind: absolute}) reads with that
    person's name and lists them under Who (the shared literal reader), as a plain value does."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line
    flag(h)
    alex = h.seed['people'][0]
    typed_who = {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [], 'value': {'kind': 'absolute', 'value': [alex]}}]}
    rule = catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 4, 'level': 'mandatory'}}, who=typed_who, note='Typed person line')])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'rules/duties_per_month')
        row = page.locator('#main tr', has_text='Typed person line')
        expect(row).to_contain_text('Alex Morgan')
        navigate(page, f"rules/duties_per_month/{rule['definition']['rule']['lines'][0]['id']}")
        expect(page.locator('.rc-line-panel .rc-says')).to_contain_text('Alex Morgan: ')  # the line's sentence names them
    finally:
        context.close()


def test_a_reply_made_stale_by_a_save_is_asked_again(browser, live_server, h):
    """rc.16: a relative filter's answer arrives after a save forgot the resolved values (it is dropped as stale): it is
    asked once more, so Select all does not stay "Waiting for the filter to be worked out"."""
    alex, _, _ = h.seed['people']
    record(h, alex, DAY1 + timedelta(days=2))
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    held, gate = [], {'open': False}
    try:
        freeze(page, DAY1)
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        pop = toolbar(page, 'Filter')
        choose_field(filter_row(pop), 'Dates')
        page.locator('.picker').get_by_role('option', name=re.compile('^This month')).click()
        close_popover(page)
        expect(rows(page)).to_have_count(1)
        page.route('**/api/conditions/resolve-operands', lambda r: r.continue_() if gate['open'] else held.append(r))
        with page.expect_request('**/api/conditions/resolve-operands'):
            page.reload()
        expect_loaded(page, 'requests')
        everyone = page.locator('#main').get_by_role('checkbox', name=re.compile('^Select all .* shown'))
        expect(everyone).to_be_disabled()
        # A command saved meanwhile (api.ts announces every one as rota-saved) forgets the resolved values while the
        # answer is out; nothing on this list asks again by itself.
        page.evaluate("window.dispatchEvent(new CustomEvent('rota-saved', {detail: {kind: 'catalogue.update'}}))")
        gate['open'] = True
        for r in held:
            r.continue_()
        expect(everyone).to_be_enabled()
        expect(rows(page)).to_have_count(1)
    finally:
        context.close()

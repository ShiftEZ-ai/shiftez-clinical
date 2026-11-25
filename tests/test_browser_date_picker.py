"""The one date picker (apps/web/src/date_picker.ts): New draft's Dates range by mouse, keyboard and presets; the
department's week start; typing; min/max; the phone sheet. Playwright reads a date field's shown text ("Sun 1 Nov 2026");
the page's .value (what forms send) is ISO."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import auto_setup, browser, live_server, sign_in, navigate, new_draft, open_scheduling, open_rota, open_config  # noqa: F401
from .governance_support import developer

pytestmark = pytest.mark.browser
TODAY = '2026-10-03T09:00:00+03:00'  # a Saturday


def open_new_draft(browser, live_server, viewport=None):
    context, page = sign_in(browser, live_server, 'coordinator', viewport)
    page.clock.set_fixed_time(TODAY)
    navigate(page, 'planner')
    new_draft(page)
    return context, page, page.get_by_role('dialog', name='New rota', exact=True)


def calendar(page, dialog, name='Choose dates'):
    dialog.get_by_role('button', name=name, exact=True).click()
    pop = page.get_by_role('dialog', name=name, exact=True)
    expect(pop).to_be_visible()
    return pop


def test_weekday_dates_keep_three_letter_months_and_the_department_day(browser, live_server, h):
    """September stays Sep in labels, fields and the phone summary, including across a timezone's midnight."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        labels = page.evaluate("""async () => {
          const {formatDay, formatDate, field} = await window.importBundle('dom.js'), {dateRange} = await window.importBundle('date_picker.js');
          const first = field('September first day', 'date', '2027-09-30'), last = field('October last day', 'date', '2027-10-01');
          document.querySelector('#main').append(dateRange('September dates', first, last));
          const instant = '2027-09-30T21:30:00Z';
          return [formatDay('2027-09-30', 'UTC'), formatDay('2027-09-30', 'UTC', true),
                  formatDay(instant, 'UTC', true), formatDay(instant, 'Asia/Riyadh', true),
                  formatDate('2027-09-30'), formatDate(instant, 'Asia/Riyadh')];
        }""")
        assert labels == ['Thu 30 Sep', 'Thu 30 Sep 2027', 'Thu 30 Sep 2027', 'Fri 1 Oct 2027', '30 Sep 2027', '1 Oct 2027']
        expect(page.get_by_label('September first day', exact=True)).to_have_value('Thu 30 Sep 2027')
        expect(page.get_by_label('October last day', exact=True)).to_have_value('Fri 1 Oct 2027')
        expect(page.get_by_role('group', name='September dates').locator('.date-summary')).to_have_text('Thu 30 Sep 2027 → Fri 1 Oct 2027')
    finally:
        context.close()


def test_new_draft_dates_by_preset_mouse_and_keyboard(browser, live_server, h):
    context, page, dialog = open_new_draft(browser, live_server)
    try:
        first, last = dialog.get_by_label('First day', exact=True), dialog.get_by_label('Last day', exact=True)
        dates = dialog.get_by_role('group', name='Dates', exact=True)
        expect(dates).to_be_visible()
        pop = calendar(page, dialog)
        # Department week (Sunday first in the seed), Fri–Sat weekend shaded, today marked: said in each day's name, so
        # no legend sits under the grid (X-12).
        expect(pop.locator('th').first).to_have_text('Su')
        expect(pop.locator('.dp-foot .hint')).to_have_count(0)
        expect(pop.get_by_role('button', name=re.compile('^Saturday 3 October 2026, weekend, today'))).to_have_attribute('aria-current', 'date')
        expect(pop.get_by_role('button', name=re.compile('^Friday 2 October 2026, weekend'))).to_have_class(re.compile('weekend'))
        # Presets: Next month is November.
        pop.get_by_role('button', name='Next month', exact=True).click()
        expect(pop.get_by_role('button', name='Next month', exact=True)).to_have_attribute('aria-pressed', 'true')
        pop.get_by_role('button', name='Use 1 – 30 Nov', exact=True).click()
        expect(pop).to_be_hidden()
        expect(first).to_have_value('Sun 1 Nov 2026')
        expect(last).to_have_value('Mon 30 Nov 2026')
        expect(dates).to_contain_text('30 days')
        expect(first).to_be_focused()
        # Mouse: two clicks pick a range; Custom is the state then.
        pop = calendar(page, dialog)
        pop.get_by_role('button', name='Show previous month', exact=True).click()
        pop.get_by_role('button', name=re.compile('^Thursday 15 October 2026')).click()
        pop.get_by_role('button', name=re.compile('^Tuesday 20 October 2026')).click()
        expect(pop.get_by_role('button', name='Custom', exact=True)).to_have_attribute('aria-pressed', 'true')
        pop.get_by_role('button', name='Use 15 – 20 Oct', exact=True).click()
        expect(first).to_have_value('Thu 15 Oct 2026')
        expect(last).to_have_value('Tue 20 Oct 2026')
        # Keyboard: focus starts on the first day; arrows, Enter twice, then Use.
        pop = calendar(page, dialog)
        expect(pop.get_by_role('button', name=re.compile('^Thursday 15 October 2026'))).to_be_focused()
        page.keyboard.press('ArrowRight')
        page.keyboard.press('Enter')
        page.keyboard.press('ArrowDown')
        page.keyboard.press('Enter')
        expect(pop.get_by_role('button', name='Use 16 – 23 Oct', exact=True)).to_be_focused()
        page.keyboard.press('Enter')
        expect(first).to_have_value('Fri 16 Oct 2026')
        expect(last).to_have_value('Fri 23 Oct 2026')
        # PageDown moves a month; Escape closes without changing anything and focus stays in the field.
        pop = calendar(page, dialog)
        page.keyboard.press('PageDown')
        expect(pop.get_by_role('button', name=re.compile('^Monday 16 November 2026'))).to_be_focused()
        page.keyboard.press('Escape')
        expect(pop).to_be_hidden()
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role('button', name='Choose dates', exact=True)).to_be_focused()
        expect(first).to_have_value('Fri 16 Oct 2026')
        # Typing still works, in the department's format too; leaving the field tidies it.
        first.fill('1/10/2026')
        last.fill('31 Oct 2026')
        expect(dates).to_contain_text('31 days')
        assert first.evaluate('n => n.value') == '2026-10-01'  # what the form sends: ISO
        dialog.get_by_label('Rota name', exact=True).click()
        expect(first).to_have_value('Thu 1 Oct 2026')  # leaving the field shows the date in full
        first.fill('31/02/2026')
        assert first.evaluate('n => n.validationMessage') == 'Enter a date such as 1 Oct 2026 or 01/10/2026.'
    finally:
        context.close()


def test_week_start_follows_the_department_setting(browser, live_server, h):
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    h.command('admin', 'department.settings', {'department_name': dept['name'], 'department_revision': dept['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': 0,
        'reason': 'Monday weeks for the date picker'}, dept['id'], org['revision'])
    context, page, dialog = open_new_draft(browser, live_server)
    try:
        pop = calendar(page, dialog)
        expect(pop.locator('th').first).to_have_text('Mo')
        # Next week is the department's next Monday-to-Sunday week.
        pop.get_by_role('button', name='Next week', exact=True).click()
        expect(pop.get_by_role('button', name='Use 5 – 11 Oct', exact=True)).to_be_enabled()
    finally:
        context.close()


def test_min_and_max_cannot_be_picked_or_typed(browser, live_server, h):
    w = h.workspace('Two month rota', '2026-10-04', '2026-12-04')
    h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_scheduling(page)
        open_rota(page, w['id'])
        panel = page.locator('#auto-panel')
        auto_setup(panel)  # D76: the setup's fields open under Change
        start = panel.get_by_label('From', exact=True)
        expect(start).to_have_value('Sun 4 Oct 2026')
        panel.get_by_role('button', name='Choose dates', exact=True).click()  # From/To are one range field
        pop = page.get_by_role('dialog', name='Choose dates', exact=True)
        before = pop.get_by_role('button', name=re.compile('^Saturday 3 October 2026'))
        expect(before).to_have_attribute('aria-disabled', 'true')
        before.click(force=True)  # a day before the rota is not taken
        expect(before).to_have_attribute('aria-pressed', 'false')
        pop.get_by_role('button', name=re.compile('^Monday 5 October 2026')).click()
        pop.get_by_role('button', name=re.compile('^Friday 9 October 2026')).click()
        pop.get_by_role('button', name='Use 5 – 9 Oct', exact=True).click()
        expect(pop).to_be_hidden()
        expect(start).to_have_value('Mon 5 Oct 2026')
        expect(panel.get_by_label('To', exact=True)).to_have_value('Fri 9 Oct 2026')
        start.fill('2026-09-01')
        assert start.evaluate('n => n.validationMessage') == 'Choose Sun 4 Oct 2026 or later.'
    finally:
        context.close()


def test_phone_shows_one_month_as_a_sheet(browser, live_server, h):
    context, page, dialog = open_new_draft(browser, live_server, {'width': 390, 'height': 844})
    try:
        pop = calendar(page, dialog)
        expect(pop.locator('table')).to_have_count(1)
        # A full-width sheet (computed style: its box is scaled for a moment while it opens).
        assert pop.evaluate('n => {const s = getComputedStyle(n); return [s.left, s.right]}') == ['0px', '0px']
        pop.get_by_role('button', name='Rest of this month', exact=True).click()
        pop.get_by_role('button', name='Use 3 – 31 Oct', exact=True).click()
        expect(dialog.get_by_label('Last day', exact=True)).to_have_value('Sat 31 Oct 2026')
    finally:
        context.close()


def test_dates_inside_a_filter_panel_open_their_calendar_inline(browser, live_server, h):
    """The audit filter's From/To dates are one range field; its calendar opens inside the filter panel."""
    context, page = sign_in(browser, live_server, developer(h))
    page.clock.set_fixed_time(TODAY)
    try:
        navigate(page, 'system/audit')
        page.get_by_role('button', name='Filter', exact=True).click()
        panel = page.get_by_role('dialog', name='Filter')
        panel.get_by_role('button', name='Choose dates', exact=True).click()
        inline = panel.get_by_role('group', name='Choose dates', exact=True)
        expect(inline).to_be_visible()  # inside the filter panel, which stays open
        expect(inline.locator('table')).to_have_count(1)
        inline.get_by_role('button', name=re.compile('^Thursday 1 October 2026')).click()
        inline.get_by_role('button', name=re.compile('^Saturday 3 October 2026')).click()
        inline.get_by_role('button', name='Use 1 – 3 Oct', exact=True).click()
        expect(inline).to_be_hidden()
        expect(panel.get_by_label('From date', exact=True)).to_have_value('Thu 1 Oct 2026')
        expect(panel.get_by_label('To date', exact=True)).to_have_value('Sat 3 Oct 2026')
    finally:
        context.close()


def test_an_optional_last_day_can_stay_empty(browser, live_server, h):
    """Add rank (From, Last day optional) is one range field: a first day alone is enough."""
    context, page = sign_in(browser, live_server, 'coordinator')
    page.clock.set_fixed_time(TODAY)
    try:
        navigate(page, 'people')
        page.locator('.dt-table tbody tr').first.get_by_role('button', name=re.compile('^Open ')).click()
        page.locator('.dock-panel.person-panel').get_by_role('button', name='Add rank', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add rank')
        dates = dialog.get_by_role('group', name='Dates', exact=True)
        dates.get_by_role('button', name='Choose dates', exact=True).click()
        pop = page.get_by_role('dialog', name='Choose dates', exact=True)
        pop.get_by_role('button', name='Clear', exact=True).click()  # From starts on today; pick again
        pop.get_by_role('button', name=re.compile('^Monday 5 October 2026')).click()
        pop.get_by_role('button', name='Use from Mon 5 Oct 2026', exact=True).click()
        expect(dates.get_by_label('From', exact=True)).to_have_value('Mon 5 Oct 2026')
        expect(dates.get_by_label('Last day (optional)', exact=True)).to_have_value('')
    finally:
        context.close()


def test_all_day_leave_picks_its_days_with_the_date_picker(browser, live_server, h):
    """All day swaps the two times for one Dates range; the request still runs from the first day's start to the end
    of the last day."""
    context, page = sign_in(browser, live_server, 'staff')
    page.clock.set_fixed_time(TODAY)
    sent = []
    page.on('request', lambda r: r.method == 'POST' and 'private_reason' in (r.post_data or '') and sent.append(r.post_data))
    try:
        navigate(page, 'requests')
        page.get_by_role('button', name='New request', exact=True).click()
        dialog = page.get_by_role('dialog', name='New request')
        dialog.get_by_role('radio', name=re.compile('^Leave')).check()
        expect(dialog.get_by_label('All day')).to_be_checked()  # leave starts as whole days
        expect(dialog.get_by_label('Request from', exact=True)).to_be_hidden()
        dates = dialog.get_by_role('group', name='Dates', exact=True)
        dates.get_by_role('button', name='Choose dates', exact=True).click()
        pop = page.get_by_role('dialog', name='Choose dates', exact=True)
        pop.get_by_role('button', name='Clear', exact=True).click()
        pop.get_by_role('button', name=re.compile('^Monday 5 October 2026')).click()
        pop.get_by_role('button', name=re.compile('^Wednesday 7 October 2026')).click()
        pop.get_by_role('button', name='Use 5 – 7 Oct', exact=True).click()
        expect(dates).to_contain_text('3 days')
        dialog.get_by_label('Type', exact=True).select_option(label='Annual')
        dialog.get_by_label('Reason', exact=True).fill('Family event')
        dialog.get_by_role('button', name='Send request', exact=True).click()
        expect(page.get_by_text('Request submitted, not yet approved.')).to_be_visible()
        # 5 Oct 00:00 to 8 Oct 00:00 department-local time, exactly as before the picker.
        assert len(sent) == 1 and '"start":"2026-10-04T21:00:00+00:00","end":"2026-10-07T21:00:00+00:00"' in sent[0], sent
    finally:
        context.close()


def test_a_date_and_time_is_the_shared_date_field_with_a_24_hour_time(browser, live_server, h):
    """Every date and time (here a leave request's From and Until) is the shared date field plus a 24-hour time: typed
    ("1530", "8:05"), stepped by 5 minutes with ↑/↓ (an hour with PageUp), snapped to the grid; no browser picker left.
    The request still sends the same instants."""
    context, page = sign_in(browser, live_server, 'staff')
    page.clock.set_fixed_time(TODAY)
    sent = []
    page.on('request', lambda r: r.method == 'POST' and 'private_reason' in (r.post_data or '') and sent.append(r.post_data))
    try:
        navigate(page, 'requests')
        page.get_by_role('button', name='New request', exact=True).click()
        dialog = page.get_by_role('dialog', name='New request')
        # A time on its own (Unavailable every week) is the same 24-hour spinbutton.
        dialog.get_by_role('radio', name=re.compile('^Unavailable every week')).check()
        weekly = dialog.get_by_role('spinbutton', name='Not available from')
        expect(weekly).to_have_value('13:00')
        weekly.press('ArrowDown')
        expect(weekly).to_have_value('12:55')
        expect(dialog.locator('input[type=datetime-local], input[type=date], input[type=time]')).to_have_count(0)
        dialog.get_by_role('radio', name=re.compile('^Leave')).check()
        expect(dialog.locator('input[type=datetime-local], input[type=date], input[type=time]')).to_have_count(0)
        dialog.get_by_label('All day').uncheck()
        first = dialog.get_by_label('Request from', exact=True)
        first.fill('5 Oct 2026')
        start = dialog.get_by_role('spinbutton', name='Request from time')
        start.fill('')
        first.focus()
        first.press('Tab')
        expect(first).to_have_value('Mon 5 Oct 2026')
        assert start.evaluate('n => n.validationMessage') == 'Enter a time such as 08:00 (24-hour).'
        start.fill('8:03')
        start.press('ArrowUp')
        expect(start).to_have_value('08:05')
        start.press('ArrowDown')
        start.press('ArrowDown')
        expect(start).to_have_value('07:55')
        start.press('PageUp')
        expect(start).to_have_value('08:55')
        expect(start).to_have_attribute('aria-valuetext', '08:55')
        until = dialog.get_by_label('Request until time', exact=True)
        until.fill('25:00')
        assert until.evaluate('n => n.validationMessage') == 'Enter a time such as 08:00 (24-hour).'
        until.fill('1530')
        until.blur()
        expect(until).to_have_value('15:30')
        # A whole date and time typed (or pasted) into the date lands in both parts.
        dialog.get_by_label('Request until', exact=True).fill('2026-10-05T16:45')
        expect(dialog.get_by_label('Request until', exact=True)).to_have_value('Mon 5 Oct 2026')
        expect(until).to_have_value('16:45')
        dialog.get_by_label('Type', exact=True).select_option(label='Annual')
        dialog.get_by_label('Reason', exact=True).fill('Clinic run')
        dialog.get_by_role('button', name='Send request', exact=True).click()
        expect(page.get_by_text('Request submitted, not yet approved.')).to_be_visible()
        assert len(sent) == 1 and '"start":"2026-10-05T05:55:00+00:00","end":"2026-10-05T13:45:00+00:00"' in sent[0], sent
    finally:
        context.close()


def test_a_date_and_time_max_is_checked_to_the_minute(browser, live_server, h):
    """Regression: a date-and-time field with a "yyyy-mm-ddTHH:MM" max refused only later days, so a later time on the
    last day passed. Now the whole value is compared: the last day stays pickable, a time after the max is refused with
    the bound in words, the max itself is accepted, and a changed max re-checks the value already there."""
    context, page = sign_in(browser, live_server, 'staff')
    page.clock.set_fixed_time(TODAY)
    try:
        navigate(page, 'requests')
        page.get_by_role('button', name='New request', exact=True).click()
        dialog = page.get_by_role('dialog', name='New request')
        dialog.get_by_role('radio', name=re.compile('^Leave')).check()
        dialog.get_by_label('All day').uncheck()
        until = dialog.get_by_label('Request until', exact=True)
        until.evaluate("n => n.max = '2026-10-05T16:00'")
        until.fill('2026-10-05T16:45')
        expect(dialog.get_by_role('spinbutton', name='Request until time')).to_have_value('16:45')
        assert until.evaluate('n => n.validationMessage') == 'Choose Mon 5 Oct 2026 16:00 or earlier.'
        # Changing only the time re-checks the date and time.
        time = dialog.get_by_role('spinbutton', name='Request until time')
        time.fill('1600')
        assert until.evaluate('n => n.validationMessage') == ''
        until.fill('2026-10-06T08:00')
        assert until.evaluate('n => n.validationMessage') == 'Choose Mon 5 Oct 2026 16:00 or earlier.'
        # A new max re-checks the value already in the field.
        until.evaluate("n => n.max = '2026-10-06T09:00'")
        expect(until).to_have_js_property('validationMessage', '')
        # The calendar still offers the max's own day.
        until.locator('xpath=..').get_by_role('button', name='Choose date', exact=True).click()
        pop = page.get_by_role('dialog', name='Choose a date', exact=True)
        expect(pop.get_by_role('button', name=re.compile('^Tuesday 6 October 2026'))).not_to_have_attribute('aria-disabled', 'true')
        expect(pop.get_by_role('button', name=re.compile('^Wednesday 7 October 2026'))).to_have_attribute('aria-disabled', 'true')
    finally:
        context.close()


def test_last_day_included_keeps_the_stored_end_unchanged(browser, live_server, h):
    """lastDayIncluded (date_picker.ts) is the one conversion for ends stored as the day after the last day: the field
    shows and takes the last day, .value reads and writes the stored end, typed or picked."""
    context, page = sign_in(browser, live_server, 'coordinator')
    page.clock.set_fixed_time(TODAY)
    try:
        result = page.evaluate("""async () => {
          const dom = await window.importBundle('dom.js'), dp = await window.importBundle('date_picker.js');
          const first = dom.field('From', 'date', '2026-10-01'), last = dom.field('Until (included)', 'date', '2026-11-01');
          dp.lastDayIncluded(last.input);
          const raw = n => HTMLInputElement.prototype.__lookupGetter__('value').call(n);
          const dialog = document.createElement('dialog'), form = document.createElement('form');
          form.append(dp.dateRange('Dates', first, last)); dialog.append(form); document.body.append(dialog); dialog.showModal();
          const out = {shown: raw(last.input), stored: last.input.value, count: form.textContent.match(/\\d+ days/)?.[0]};
          last.input.value = '2026-12-01'; out.set = [raw(last.input), last.input.value];
          last.input.value = ''; out.empty = [raw(last.input), last.input.value];
          last.input.value = '2026-11-01';
          return out;
        }""")
        assert result == {'shown': 'Sat 31 Oct 2026', 'stored': '2026-11-01', 'count': '31 days',
                          'set': ['Mon 30 Nov 2026', '2026-12-01'], 'empty': ['', '']}, result
        dates = page.get_by_role('group', name='Dates', exact=True)
        until = dates.get_by_label('Until (included)', exact=True)
        until.fill('2026-10-30')  # typed: the last day
        assert until.evaluate('n => n.value') == '2026-10-31'
        dates.get_by_role('button', name='Choose dates', exact=True).click()
        pop = page.get_by_role('dialog', name='Choose dates', exact=True)
        expect(pop.get_by_role('button', name=re.compile('^Friday 30 October 2026, weekend, last day'))).to_be_visible()
        pop.get_by_role('button', name=re.compile('^Thursday 1 October 2026')).click()
        pop.get_by_role('button', name=re.compile('^Saturday 31 October 2026')).click()
        pop.get_by_role('button', name='Use 1 – 31 Oct', exact=True).click()
        expect(until).to_have_value('Sat 31 Oct 2026')
        assert until.evaluate('n => n.value') == '2026-11-01'  # picked: stored as the day after
    finally:
        context.close()


def test_weekend_days_come_from_the_department_setting(browser, live_server, h):
    """Configuration › Department settings › Weekend days is the one weekend every calendar shades: the date picker and
    the Schedule/planner (isWeekend)."""
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Department settings')
        weekend = page.get_by_role('group', name='Weekend days')
        expect(weekend.get_by_label('Friday', exact=True)).to_be_checked()
        weekend.get_by_label('Friday', exact=True).uncheck()
        weekend.get_by_label('Sunday', exact=True).check()
        page.locator('.config-pane').get_by_role('button', name='Save settings', exact=True).click()
        page.get_by_role('dialog').get_by_label('Reason', exact=True).fill('Weekend is Saturday and Sunday')
        page.get_by_role('dialog').get_by_role('button', name='Save settings', exact=True).click()
        expect(page.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        assert h.get('admin', '/api/me')['department']['weekend_days'] == [5, 6]
    finally:
        context.close()
    context, page, dialog = open_new_draft(browser, live_server)
    try:
        pop = calendar(page, dialog)
        expect(pop.get_by_role('button', name=re.compile('^Sunday 4 October 2026, weekend'))).to_have_class(re.compile('weekend'))
        expect(pop.get_by_role('button', name=re.compile(r'^Friday 2 October 2026(?:,|$)'))).not_to_have_class(re.compile('weekend'))
        shaded = page.evaluate("async () => { const m = await window.importBundle('planner_logic.js'); return ['2026-10-02', '2026-10-03', '2026-10-04'].map(m.isWeekend); }")
        assert shaded == [False, True, True]
    finally:
        context.close()


def test_one_day_range_one_arrow_and_a_phone_sheet_named_for_its_field(browser, live_server, h):
    """X-13: a one-day range reads "Use 3 Oct" (not "3 – 3 Oct"). X-14: one arrow, the shared tether's (no second
    arrow of the calendar's own). X-15: on a phone the calendar sheet is headed by the field it fills, with a Close."""
    context, page, dialog = open_new_draft(browser, live_server)
    try:
        pop = calendar(page, dialog)
        pop.get_by_role('button', name=re.compile('^Saturday 3 October 2026')).click()
        pop.get_by_role('button', name=re.compile('^Saturday 3 October 2026')).click()
        expect(pop.get_by_role('button', name='Use 3 Oct', exact=True)).to_be_enabled()
        expect(page.locator('.pop-arrow')).to_have_count(1)
        assert pop.evaluate("p => getComputedStyle(p, '::before').content") in ('none', 'normal')
        expect(pop.locator('.sheet-head')).to_have_count(0)  # a sheet's head is for phones only
    finally:
        context.close()
    context, page, dialog = open_new_draft(browser, live_server, {'width': 390, 'height': 844})
    try:
        pop = calendar(page, dialog)
        head = pop.locator('.sheet-head')
        expect(head.get_by_role('heading')).to_have_text('Dates')
        close = head.get_by_role('button', name='Close', exact=True)
        # Its own size (the sheet's box is scaled for a moment while it opens).
        assert close.evaluate('b => [getComputedStyle(b).width, getComputedStyle(b).height]') == ['44px', '44px']
        close.click()
        expect(pop).to_have_count(0)
        expect(dialog).to_be_visible()
    finally:
        context.close()


def test_paste_dates_says_what_it_will_do_in_one_line(browser, live_server, h):
    """X-12: Paste dates opens on its text box (the placeholder shows the formats) and one result line."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        page.evaluate("""async () => {const {dateList} = await window.importBundle('date_picker.js');
          document.querySelector('#main').prepend(dateList('Closed days', ['2026-10-12']).wrap);}""")
        page.get_by_role('button', name='Paste dates', exact=True).click()
        pop = page.get_by_role('dialog', name='Paste dates', exact=True)
        expect(pop.locator('p.hint')).to_have_count(1)
        expect(pop.locator('p.hint')).to_have_text('1 date replaces the list')
        pop.get_by_label('Dates to use', exact=True).fill('2026-10-12\n13 Oct 2026')
        expect(pop.locator('p.hint')).to_have_text('2 dates replace the list')
    finally:
        context.close()

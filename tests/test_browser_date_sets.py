"""Holidays & dates off in the browser (docs/design/date-sets.md): a department date set with Gregorian and Hijri
entries and the server's resolved dates, a one-year adjustment and its Undo, opening hours closed on a set, the shared
condition builder offering a set and its entries (read-only roles read it as facts), the Schedule's day markers and the
phone layout."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import add_filter, browser, live_server, navigate, open_config, open_rota, save_dialog, sign_in  # noqa: F401
from .test_browser_rule_catalogue import catalogue_rule, flag, line, shot
from .test_browser_rule_catalogue_v2 import themed

pytestmark = pytest.mark.browser

SECTION = 'Holidays & dates off'


def date_set(h, name='Public holidays', entries=None):
    entries = entries if entries is not None else [
        {'name': 'Founding Day', 'repeat': 'yearly_gregorian', 'first_day': '2026-02-22', 'last_day': '2026-02-22'},
        {'name': 'Eid al-Fitr', 'repeat': 'yearly_hijri', 'first_day': '1447-10-01', 'last_day': '1447-10-04'}]
    return h.configure('date_sets', {'name': name, 'entries': entries})['record']


def resolved(h, record, start, end, role='admin'):
    return h.get(role, f'/api/date-sets/resolved?start={start}&end={end}&set_id={record["id"]}')


def day_words(iso):
    """How a whole date reads on the page ("Sun 22 Feb 2026"), as date_picker.showDay writes it."""
    from datetime import date
    d = date.fromisoformat(iso)
    return f'{d.strftime("%a")} {d.day} {d.strftime("%b")} {d.year}'


def reason(page, text, label):
    dialog = page.get_by_role('dialog').last
    dialog.get_by_label('Reason', exact=True).fill(text)
    dialog.get_by_role('button', name=label, exact=True).click()
    expect(dialog).not_to_be_visible()


def test_admin_creates_a_set_with_gregorian_and_hijri_entries_and_sees_their_dates(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin'); errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        open_config(page, SECTION)
        expect(page.get_by_text('No holidays & dates off yet', exact=True)).to_be_visible()
        page.get_by_role('button', name='New date set', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Name', exact=True).fill('Public holidays')
        first = dialog.locator('.ds-entry').nth(0)
        first.get_by_label('Entry name', exact=True).fill('National Day')
        expect(first.get_by_label('Repeats', exact=True)).to_have_value('yearly_gregorian')
        first.get_by_label('First day', exact=True).fill('2026-09-23')
        first.get_by_label('Last day', exact=True).fill('2026-09-23')
        dialog.get_by_role('button', name='Add entry', exact=True).click()
        second = dialog.locator('.ds-entry').nth(1)
        second.get_by_label('Entry name', exact=True).fill('Eid al-Fitr')
        second.get_by_label('Repeats', exact=True).select_option('yearly_hijri')
        # A Hijri entry is typed as Hijri year-month-day and read back in words; the server works out its dates.
        expect(second.get_by_label('First day', exact=True)).to_be_hidden()
        # Not every Hijri month has a 30th: a last day of 30 needs Until the end of the month; a first day of 30 is refused.
        h_first, h_last = second.get_by_label('First day (Hijri)', exact=True), second.get_by_label('Last day (Hijri)', exact=True)
        h_last.fill('1447-09-30')
        expect(second.get_by_text('Not every Hijri month has a 30th: tick Until the end of the month.', exact=True)).to_be_visible()
        second.get_by_label('Until the end of the month', exact=True).check()
        expect(second.get_by_text('Not every Hijri month has a 30th: tick Until the end of the month.', exact=True)).to_be_hidden()
        second.get_by_label('Until the end of the month', exact=True).uncheck()
        h_first.fill('1447-09-30')
        expect(second.get_by_text('Not every Hijri month has a 30th; start on the 29th or earlier.', exact=True)).to_be_visible()
        assert h_first.evaluate('i => !i.validity.valid')
        h_first.fill('1447-10-01')
        h_last.fill('1447-10-04')
        expect(second.locator('.field-error:visible')).to_have_count(0)
        expect(first.get_by_role('button', name='Remove entry National Day', exact=True)).to_be_visible()
        expect(second.locator('.ds-hijri-words')).to_have_text('1 Shawwal – 4 Shawwal')
        expect(second.locator('.ds-when')).to_have_text('The Gregorian dates are worked out when you save.')
        dialog.get_by_label('Change reason', exact=True).fill('Department holidays for planning')
        save_dialog(dialog)
        record = next(r for r in h.get('admin', '/api/date-sets')['date_sets'] if r['name'] == 'Public holidays')
        assert [(e['name'], e['repeat'], e['first_day'], e['last_day']) for e in record['entries']] == [
            ('National Day', 'yearly_gregorian', '2026-09-23', '2026-09-23'), ('Eid al-Fitr', 'yearly_hijri', '1447-10-01', '1447-10-04')]
        # The list: entries and the next date (the server's resolver), Hijri included.
        row = page.locator('[data-row]').filter(has_text='Public holidays')
        expect(row).to_contain_text('National Day, Eid al-Fitr')
        upcoming = sorted(resolved(h, record, '2026-10-03', '2028-10-02'), key=lambda o: o['first_day'])[0]
        expect(row).to_contain_text(upcoming['name'])
        expect(row).to_contain_text(day_words(upcoming['first_day']))
        # Dates: this year's occurrences, each as the server resolved it; the Hijri year is named.
        row.get_by_role('button', name='Dates of Public holidays', exact=True).click()
        panel = page.locator('.dock-panel')
        eid = next(o for o in resolved(h, record, '2026-01-01', '2026-12-31') if o['name'] == 'Eid al-Fitr')
        eid_row = panel.locator('tbody tr').filter(has_text='Eid al-Fitr')
        expect(eid_row).to_contain_text(f'{eid["year"]} AH')
        expect(eid_row).to_contain_text(day_words(eid['first_day']))
        expect(eid_row).to_contain_text(day_words(eid['last_day']))
        expect(panel.locator('tbody tr').filter(has_text='National Day')).to_contain_text('Wed 23 Sep 2026')
        # Editing shows a saved entry's next dates beside it.
        row.get_by_role('button', name='Edit', exact=True).click()
        edit = page.get_by_role('dialog')
        expect(edit.locator('.ds-entry').nth(1).locator('.ds-when')).to_contain_text('Next: ')
        shot(page, 'date-set-form-1440-light', full_page=False)
        assert not errors
    finally:
        context.close()


def test_adjust_one_year_then_undo_and_back_to_the_computed_dates(browser, live_server, h):
    record = date_set(h)
    context, page = sign_in(browser, live_server, 'admin'); errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        open_config(page, SECTION)
        page.get_by_role('button', name='Dates of Public holidays', exact=True).click()
        panel = page.locator('.dock-panel')
        founding = panel.locator('tbody tr').filter(has_text='Founding Day')
        expect(founding).to_contain_text('Sun 22 Feb 2026')
        expect(founding.get_by_text('Adjusted', exact=True)).to_have_count(0)
        founding.get_by_role('button', name='Adjust Founding Day 2026', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('First day', exact=True).fill('2026-02-23')
        dialog.get_by_label('Last day', exact=True).fill('2026-02-23')
        reason(page, 'Announced one day later this year', 'Save adjustment')
        expect(founding).to_contain_text('Mon 23 Feb 2026')
        expect(founding.get_by_text('Adjusted', exact=True)).to_be_visible()
        assert [o['adjusted'] for o in resolved(h, record, '2026-01-01', '2026-12-31') if o['name'] == 'Founding Day'] == [True]
        # Only that year changes.
        assert [o['first_day'] for o in resolved(h, record, '2027-01-01', '2027-12-31') if o['name'] == 'Founding Day'] == ['2027-02-22']
        shot(page, 'date-set-dates-1440-light', full_page=False)
        # Undo puts the set back as it was.
        page.locator('.toast').filter(has_text='Founding Day adjusted for 2026.').get_by_role('button', name='Undo', exact=True).click()
        expect(founding).to_contain_text('Sun 22 Feb 2026')
        expect(founding.get_by_text('Adjusted', exact=True)).to_have_count(0)
        assert [o['adjusted'] for o in resolved(h, record, '2026-01-01', '2026-12-31') if o['name'] == 'Founding Day'] == [False]
        # Adjust again, then "Back to the computed dates" removes it.
        founding.get_by_role('button', name='Adjust Founding Day 2026', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('First day', exact=True).fill('2026-02-24')
        dialog.get_by_label('Last day', exact=True).fill('2026-02-24')
        reason(page, 'Announced two days later this year', 'Save adjustment')
        expect(founding.get_by_text('Adjusted', exact=True)).to_be_visible()
        founding.get_by_role('button', name='Back to the computed dates for Founding Day 2026', exact=True).click()
        reason(page, 'The announcement was withdrawn', 'Use computed dates')
        expect(founding).to_contain_text('Sun 22 Feb 2026')
        expect(founding.get_by_text('Adjusted', exact=True)).to_have_count(0)
        entry = next(e for e in h.get('admin', '/api/date-sets')['date_sets'][0]['entries'] if e['name'] == 'Founding Day')
        assert entry['overrides'] == []
        assert not errors
    finally:
        context.close()


def test_import_entries_and_copy_to_next_year(browser, live_server, h, tmp_path):
    record = date_set(h, entries=[{'name': 'Founding Day', 'repeat': 'yearly_gregorian', 'first_day': '2026-02-22', 'last_day': '2026-02-22'}])
    csv = tmp_path / 'holidays.csv'
    csv.write_text('name,first_day,last_day\nTraining day,2026-11-12,2026-11-12\n')
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, SECTION)
        row = page.locator('[data-row]').filter(has_text='Public holidays')
        row.hover()
        row.get_by_role('button', name=re.compile('^More actions')).click()
        page.get_by_role('menuitem', name='Import entries…').click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('File', exact=True).set_input_files(str(csv))
        reason(page, 'Department training calendar', 'Import')
        assert [e['name'] for e in h.get('admin', '/api/date-sets')['date_sets'][0]['entries']] == ['Founding Day', 'Training day']
        expect(row).to_contain_text('Founding Day, Training day')
        row.hover()
        row.get_by_role('button', name=re.compile('^More actions')).click()
        page.get_by_role('menuitem', name='Copy to next year…').click()
        reason(page, 'Next year’s planning', 'Copy')
        copy = next(r for r in h.get('admin', '/api/date-sets')['date_sets'] if r['id'] != record['id'])
        assert copy['name'] == 'Public holidays 2027'
        assert sorted((e['repeat'], e['first_day']) for e in copy['entries']) == [('once', '2027-11-12')]  # yearly entries already recur, so only once-only dates move
        expect(page.locator('[data-row]').filter(has_text='Public holidays 2027')).to_be_visible()
    finally:
        context.close()


def test_an_imported_gregorian_month_end_survives_an_edit(browser, live_server, h, tmp_path):
    """An imported Gregorian entry ending with its month keeps that end through an edit, save and reload."""
    record = date_set(h, 'Breaks', [{'name': 'Founding Day', 'repeat': 'yearly_gregorian', 'first_day': '2026-02-22', 'last_day': '2026-02-22'}])
    csv = tmp_path / 'breaks.csv'
    csv.write_text('name,first_day,last_day,repeat,last_day_of_month\nFebruary break,2026-02-01,2026-02-01,yearly_gregorian,true\n')
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, SECTION)
        row = page.locator('[data-row]').filter(has_text='Breaks')
        row.hover()
        row.get_by_role('button', name=re.compile('^More actions')).click()
        page.get_by_role('menuitem', name='Import entries…').click()
        page.get_by_role('dialog').get_by_label('File', exact=True).set_input_files(str(csv))
        reason(page, 'Winter breaks', 'Import')
        expect(row).to_contain_text('February break')
        row.get_by_role('button', name='Edit', exact=True).click()
        dialog = page.get_by_role('dialog')
        entry = dialog.locator('.ds-entry').nth(1)
        expect(entry.get_by_label('Until the end of the month', exact=True)).to_be_checked()
        dialog.get_by_label('Name', exact=True).fill('School breaks')
        dialog.get_by_label('Change reason', exact=True).fill('Renamed')
        save_dialog(dialog, 'Save version')
        page.reload()
        saved = next(r for r in h.get('admin', '/api/date-sets')['date_sets'] if r['id'] == record['id'])
        assert saved['name'] == 'School breaks'
        assert [e.get('last_day_of_month') for e in saved['entries'] if e['name'] == 'February break'] == [True]
        assert [(o['first_day'], o['last_day']) for o in resolved(h, record, '2027-01-01', '2027-12-31') if o['name'] == 'February break'] == [('2027-02-01', '2027-02-28')]
        assert [o['last_day'] for o in resolved(h, record, '2028-01-01', '2028-12-31') if o['name'] == 'February break'] == ['2028-02-29']
    finally:
        context.close()


def test_an_imported_yearly_event_shows_its_first_year_and_length_and_both_can_be_cleared(browser, live_server, h):
    record = date_set(h, 'School break', [
        {'name': 'Winter break', 'repeat': 'yearly_gregorian', 'first_day': '2025-02-28', 'last_day': '2025-03-01',
         'start_year': 2025, 'fixed_duration': True},
        {'name': 'Founding Day', 'repeat': 'yearly_gregorian', 'first_day': '2026-02-22', 'last_day': '2026-02-22'}])
    context, page = sign_in(browser, live_server, 'admin'); errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        open_config(page, SECTION)
        page.locator('[data-row]').filter(has_text='School break').get_by_role('button', name='Edit', exact=True).click()
        dialog = page.get_by_role('dialog')
        imported, made = dialog.locator('.ds-entry').nth(0), dialog.locator('.ds-entry').nth(1)
        expect(imported.get_by_label('Not before its first year', exact=True)).to_be_checked()
        expect(imported.get_by_label('Same number of days every year', exact=True)).to_be_checked()
        # A hand-made yearly entry repeats its month and day: nothing more to show.
        expect(made.get_by_label('Not before its first year', exact=True)).to_have_count(0)
        # Only a yearly Gregorian repeat has them.
        imported.get_by_label('Repeats', exact=True).select_option('once')
        expect(imported.get_by_label('Same number of days every year', exact=True)).to_be_hidden()
        imported.get_by_label('Repeats', exact=True).select_option('yearly_gregorian')
        imported.get_by_label('Same number of days every year', exact=True).uncheck()
        dialog.get_by_label('Change reason', exact=True).fill('Ends on 1 March every year')
        save_dialog(dialog, 'Save version')
        saved = next(r for r in h.get('admin', '/api/date-sets')['date_sets'] if r['id'] == record['id'])['entries']
        assert (saved[0].get('start_year'), saved[0].get('fixed_duration', False)) == (2025, False)
        assert [o['last_day'] for o in resolved(h, record, '2028-01-01', '2028-12-31') if o['name'] == 'Winter break'] == ['2028-03-01']
        assert not [o for o in resolved(h, record, '2024-01-01', '2024-12-31') if o['name'] == 'Winter break']
        assert not errors
    finally:
        context.close()


def test_an_unrelated_edit_preserves_a_saved_recurrence_start_year(browser, live_server, h):
    record = date_set(h, 'Deferred break', [
        {'name': 'Winter break', 'repeat': 'yearly_gregorian', 'first_day': '2025-02-28', 'last_day': '2025-03-01',
         'start_year': 2028, 'fixed_duration': True}])
    original = record['entries'][0]
    assert not resolved(h, record, '2025-01-01', '2027-12-31')
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, SECTION)
        page.locator('[data-row]').filter(has_text='Deferred break').get_by_role('button', name='Edit', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Name', exact=True).fill('Renamed deferred break')
        dialog.get_by_label('Change reason', exact=True).fill('Rename the set without changing its dates')
        save_dialog(dialog, 'Save version')
        saved = next(r for r in h.get('admin', '/api/date-sets')['date_sets'] if r['id'] == record['id'])
        assert saved['entries'] == [original]
        assert not resolved(h, record, '2025-01-01', '2027-12-31')
        assert [(o['first_day'], o['last_day']) for o in resolved(h, record, '2028-01-01', '2028-12-31')] == [
            ('2028-02-28', '2028-02-29')]
        page.reload()
        page.locator('[data-row]').filter(has_text='Renamed deferred break').get_by_role('button', name='Edit', exact=True).click()
        dialog = page.get_by_role('dialog')
        entry = dialog.locator('.ds-entry')
        expect(entry.get_by_text('No dates in the years before 2028.', exact=True)).to_be_visible()
        entry.get_by_label('Not before its first year', exact=True).uncheck()
        dialog.get_by_label('Change reason', exact=True).fill('Explicitly remove the saved first-year bound')
        save_dialog(dialog, 'Save version')
        saved = next(r for r in h.get('admin', '/api/date-sets')['date_sets'] if r['id'] == record['id'])
        assert saved['entries'] == [{**original, 'start_year': None}]
        assert [(o['first_day'], o['last_day']) for o in resolved(h, record, '2027-01-01', '2027-12-31')] == [
            ('2027-02-28', '2027-03-01')]
    finally:
        context.close()


@pytest.mark.parametrize('change', ['rename', 'remove_all', 'switch_set'])
def test_reactivating_a_calendar_preserves_unavailable_entry_names_until_explicitly_repaired(browser, live_server, h, change):
    record = date_set(h, 'Calendar days', [
        {'name': 'A', 'repeat': 'once', 'first_day': '2026-10-06', 'last_day': '2026-10-06'},
        *([{'name': 'B', 'repeat': 'once', 'first_day': '2026-10-07', 'last_day': '2026-10-07'}] if change == 'rename' else [])])
    calendar = h.configure('operating_calendars', {'name': 'Narrow calendar', 'starts_on': '2020-01-01', 'active': False,
        'definition': {'timezone': 'Asia/Riyadh', 'week': [{'weekday': d, 'windows': [{'start_time': '08:00', 'end_time': '16:00'}]} for d in range(7)],
                       'date_set_rules': [{'date_set_id': record['id'], 'entry_names': ['A'], 'windows': []}]}})['record']
    if change == 'switch_set':
        replacement = date_set(h, 'Replacement days', [
            {'name': 'B', 'repeat': 'once', 'first_day': '2026-10-07', 'last_day': '2026-10-07'}])
    else:
        replacement = record
        h.configure('date_sets', {'name': record['name'], 'entries': [
            {**entry, 'name': 'Renamed A'} if entry['name'] == 'A' else entry for entry in record['entries']
        ] if change == 'rename' else []}, record['id'], record['revision'])
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Opening hours')
        page.locator('[data-row]').filter(has_text='Narrow calendar').get_by_role('button', name='Edit', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Name', exact=True).fill('Reactivated narrow calendar')
        dialog.get_by_label('Active for future use', exact=True).check()
        dialog.get_by_label('Change reason', exact=True).fill('Reactivate without broadening the closure')
        if change == 'switch_set':
            dialog.get_by_label('Date set', exact=True).select_option(replacement['id'])
        with page.expect_response(lambda r: r.url.endswith('/api/catalogue/preview') and r.request.post_data_json.get('target_id') == calendar['id']) as refused:
            dialog.get_by_role('button', name='Save version', exact=True).click()
        reply = refused.value
        assert reply.request.post_data_json['payload']['value']['definition']['date_set_rules'][0]['entry_names'] == ['A']
        assert reply.status == 422 and reply.json()['error']['code'] == 'DATE_SET_ENTRY_NAMES'
        retained = next(r for r in h.get('admin', '/api/catalogues')['operating_calendars'] if r['id'] == calendar['id'])
        assert retained == calendar
        rule = dialog.locator('fieldset.nested-card').filter(has=page.get_by_label('On these days', exact=True))
        expect(rule.locator('.pick-button')).to_have_attribute('aria-label', 'Entries: A · unavailable')
        rule.locator('.pick-button').click()
        page.locator('.picker').get_by_role('option', name='A · unavailable', exact=True).uncheck()
        if change != 'remove_all':
            page.locator('.picker').get_by_role('option', name='B', exact=True).check()
        page.keyboard.press('Escape')
        save_dialog(dialog, 'Save version')
        saved = next(r for r in h.get('admin', '/api/catalogues')['operating_calendars'] if r['id'] == calendar['id'])
        assert saved['active'] is True
        assert saved['definition']['date_set_rules'] == [
            {'date_set_id': replacement['id'], 'entry_names': ['B'] if change != 'remove_all' else [], 'windows': []}]
        assert sorted(saved['definition']['week'], key=lambda d: d['weekday']) == sorted(
            calendar['definition']['week'], key=lambda d: d['weekday'])
    finally:
        context.close()


@pytest.mark.parametrize('replacement_entries', [True, False], ids=['other_entries', 'no_entries'])
def test_changing_a_condition_date_set_preserves_its_entry_selection_until_explicitly_repaired(browser, live_server, h, replacement_entries):
    original = date_set(h, 'Original days', [{'name': 'A', 'repeat': 'once', 'first_day': '2026-10-06', 'last_day': '2026-10-06'}])
    other = date_set(h, 'Other days', [{'name': 'B', 'repeat': 'once', 'first_day': '2026-10-07', 'last_day': '2026-10-07'}] if replacement_entries else [])
    flag(h)
    where = {'match': 'all', 'conditions': [{'field': 'date_set_ids', 'op': 'any', 'values': [original['id']], 'entry_names': ['A']}]}
    saved_rule = catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Default'),
        line({'max': {'value': 2, 'level': 'mandatory'}}, where=where, note='Named days')])
    named_line = saved_rule['definition']['rule']['lines'][1]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f'rules/duties_per_week/{named_line["id"]}')
        panel = page.locator('.rc-line-panel')
        panel.locator('.cond-line').first.locator('.cond-values .pick-button').click()
        picker = page.locator('.picker')
        picker.get_by_role('option', name='Original days', exact=True).uncheck()
        picker.get_by_role('option', name='Other days', exact=True).check()
        page.keyboard.press('Escape')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()

        page.locator('.topbar').get_by_role('button', name='Propose 1 change', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Change the parent set without broadening the named selection')
        acknowledgement = dialog.get_by_role('checkbox', name='I have read which lines give way and that is intended', exact=True)
        if acknowledgement.count():
            acknowledgement.check()
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.post_data_json.get('kind') == 'rule.propose') as refused:
            dialog.get_by_role('button', name='Propose', exact=True).click()
        proposed_line = next(item for item in refused.value.request.post_data_json['payload']['lines'] if item['id'] == named_line['id'])
        assert proposed_line['where']['conditions'][0].get('entry_names') == ['A']
        assert refused.value.status == 422 and refused.value.json()['error']['code'] == 'DATE_SET_ENTRY_NAMES'
        assert next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_week')['proposed'] is None
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        if dialog.locator('.unsaved-bar').count():
            dialog.get_by_role('button', name='Discard changes', exact=True).click()
        expect(dialog).not_to_be_visible()
        page.locator('[data-row="' + named_line['id'] + '"] [data-col="who"]').click()
        entries = page.locator('.rc-line-panel .cond-entries')
        expect(entries.locator('.pick-button')).to_have_attribute('aria-label', 'Entries: A · unavailable')
        entries.locator('.pick-button').click()
        page.locator('.picker').get_by_role('option', name='A · unavailable', exact=True).uncheck()
        if replacement_entries:
            page.locator('.picker').get_by_role('option', name='B', exact=True).check()
        page.keyboard.press('Escape')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        page.locator('.topbar').get_by_role('button', name='Propose 1 change', exact=True).click()
        acknowledgement = page.get_by_role('dialog').get_by_role('checkbox', name='I have read which lines give way and that is intended', exact=True)
        if acknowledgement.count():
            acknowledgement.check()
        reason(page, 'Explicitly replace the unavailable entry selection', 'Propose')
        proposed = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_week')['proposed']
        replacement = next(item for item in proposed['definition']['rule']['lines'] if item['id'] == named_line['id'])
        assert replacement['where']['conditions'][0] == {
            'field': 'date_set_ids', 'op': 'any', 'values': [other['id']], **({'entry_names': ['B']} if replacement_entries else {})}
    finally:
        context.close()


def test_opening_hours_closed_on_a_date_set(browser, live_server, h):
    record = date_set(h)
    context, page = sign_in(browser, live_server, 'admin'); errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        open_config(page, 'Opening hours')
        page.get_by_role('button', name='Set opening hours', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Name', exact=True).fill('Clinic hours with holidays')
        dialog.get_by_label('Effective from', exact=True).fill('2026-10-01')
        dialog.get_by_role('button', name='Add Sunday window', exact=True).click()
        dialog.get_by_role('button', name='Add date set rule', exact=True).click()
        expect(dialog.get_by_text('The first rule that matches a day applies', exact=False)).to_be_visible()
        rules = dialog.locator('fieldset.nested-card').filter(has=page.get_by_label('On these days', exact=True))
        rule = rules.first
        expect(rule.get_by_role('button', name='Move the rule for Public holidays up', exact=True)).to_be_disabled()
        rule.get_by_label('Date set', exact=True).select_option(label='Public holidays')
        expect(rule.locator('legend')).to_have_text('On Public holidays')

        def choose_entry(fieldset, name):
            fieldset.locator('.pick-button').click()
            page.locator('.picker').get_by_role('option', name=name, exact=True).check()
            page.keyboard.press('Escape')
            expect(fieldset.locator('.pick-button')).to_have_attribute('aria-label', f'Entries: {name}')
        # The shared picker, with the condition builder's words: none chosen means all entries.
        expect(rule.locator('.pick-button')).to_contain_text('All entries')
        choose_entry(rule, 'Eid al-Fitr')
        expect(rule.get_by_label('On these days', exact=True)).to_have_value('closed')
        # A second rule opens other hours on the remaining entry.
        dialog.get_by_role('button', name='Add date set rule', exact=True).click()
        other = rules.nth(1)
        other.get_by_label('Date set', exact=True).select_option(label='Public holidays')
        choose_entry(other, 'Founding Day')
        other.get_by_label('On these days', exact=True).select_option('open')
        other.get_by_label('Open from', exact=True).fill('09:00')
        other.get_by_label('Open until', exact=True).fill('13:00')
        expect(other.get_by_role('button', name='Add hours', exact=True)).to_be_visible()
        # The first rule that matches applies: move the open rule first; focus stays on the rule's buttons.
        up = other.get_by_role('button', name='Move the rule for Public holidays up', exact=True)
        expect(up).to_be_enabled()
        up.click()
        expect(rules.first.get_by_label('On these days', exact=True)).to_have_value('open')
        expect(rules.first.get_by_role('button', name='Remove the rule for Public holidays', exact=True)).to_be_focused()
        expect(rules.first.get_by_role('button', name='Move the rule for Public holidays up', exact=True)).to_be_disabled()
        expect(rules.nth(1).get_by_role('button', name='Move the rule for Public holidays up', exact=True)).to_be_enabled()
        dialog.get_by_label('Change reason', exact=True).fill('Closed on public holidays')
        rules.first.scroll_into_view_if_needed()
        shot(page, 'date-set-opening-hours-1440-light', full_page=False)
        save_dialog(dialog)
        saved = next(c for c in h.get('admin', '/api/catalogues')['operating_calendars'] if c['name'] == 'Clinic hours with holidays')
        assert saved['definition']['date_set_rules'] == [
            {'date_set_id': record['id'], 'entry_names': ['Founding Day'], 'windows': [{'start_time': '09:00', 'end_time': '13:00'}]},
            {'date_set_id': record['id'], 'entry_names': ['Eid al-Fitr'], 'windows': []}]
        # Edited again, the rules come back as saved (nothing is dropped on the next save).
        page.locator('[data-row]').filter(has_text='Clinic hours with holidays').get_by_role('button', name='Edit', exact=True).click()
        dialog = page.get_by_role('dialog')
        rules = dialog.locator('fieldset.nested-card').filter(has=page.get_by_label('On these days', exact=True))
        expect(rules).to_have_count(2)
        expect(rules.nth(1).locator('.pick-button')).to_have_attribute('aria-label', 'Entries: Eid al-Fitr')
        # Only reordering is still a change: closing asks first.
        rules.nth(1).get_by_role('button', name='Move the rule for Public holidays up', exact=True).click()
        expect(rules.first.locator('.pick-button')).to_have_attribute('aria-label', 'Entries: Eid al-Fitr')
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        guard = dialog.locator('.unsaved-bar')
        expect(guard).to_contain_text('Not saved. Close anyway?')
        guard.get_by_role('button', name='Discard changes', exact=True).click()
        expect(dialog).to_have_count(0)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize('repair,width,theme', [('hours', 1440, 'light'), ('closed', 390, 'dark')])
def test_removing_final_date_set_hours_requires_explicit_repair_before_save(browser, live_server, h, repair, width, theme):
    record = date_set(h, 'Clinic dates', [
        {'name': 'Holiday', 'repeat': 'once', 'first_day': '2026-10-06', 'last_day': '2026-10-06'}])
    calendar = h.configure('operating_calendars', {'name': 'Open holiday hours', 'starts_on': '2020-01-01',
        'definition': {'timezone': 'Asia/Riyadh', 'week': [
            {'weekday': day, 'windows': [{'start_time': '08:00', 'end_time': '16:00'}]} for day in range(7)],
            'date_set_rules': [{'date_set_id': record['id'], 'entry_names': ['Holiday'],
                                'windows': [{'start_time': '09:00', 'end_time': '13:00'}]}]}})['record']
    context, page = themed(browser, live_server, 'admin', width, theme)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        open_config(page, 'Opening hours')
        page.locator('[data-row]').filter(has_text=calendar['name']).get_by_role('button', name='Edit', exact=True).click()
        dialog = page.get_by_role('dialog')
        rule = dialog.locator('fieldset.nested-card').filter(has=page.get_by_label('On these days', exact=True))
        mode = rule.get_by_label('On these days', exact=True)
        expect(mode).to_have_value('open')
        dialog.get_by_label('Change reason', exact=True).fill('Explicitly repair removed holiday hours')
        rule.get_by_role('button', name='Remove these hours', exact=True).click()
        expect(rule.get_by_label('Open from', exact=True)).to_have_count(0)
        expect(mode).to_have_value('open')
        dialog.get_by_role('button', name='Save version', exact=True).click()
        message = rule.get_by_text('Add opening hours or choose Closed.', exact=True)
        expect(message).to_be_visible()
        assert not mode.evaluate('input => input.checkValidity()')
        assert next(row for row in h.get('admin', '/api/catalogues')['operating_calendars']
                    if row['id'] == calendar['id']) == calendar
        if repair == 'hours':
            rule.get_by_role('button', name='Add hours', exact=True).click()
            rule.get_by_label('Open from', exact=True).fill('10:00')
            rule.get_by_label('Open until', exact=True).fill('14:00')
        else:
            mode.select_option('closed')
        expect(message).to_be_hidden()
        assert mode.evaluate('input => input.checkValidity()')
        save_dialog(dialog, 'Save version')
        saved = next(row for row in h.get('admin', '/api/catalogues')['operating_calendars'] if row['id'] == calendar['id'])
        assert saved['definition']['date_set_rules'] == [{'date_set_id': record['id'], 'entry_names': ['Holiday'],
            'windows': [{'start_time': '10:00', 'end_time': '14:00'}] if repair == 'hours' else []}]
        assert sorted(saved['definition']['week'], key=lambda day: day['weekday']) == sorted(
            calendar['definition']['week'], key=lambda day: day['weekday'])
        assert saved['definition']['overrides'] == calendar['definition']['overrides']
        page.locator('[data-row]').filter(has_text=calendar['name']).get_by_role('button', name='Edit', exact=True).click()
        dialog = page.get_by_role('dialog')
        rule = dialog.locator('fieldset.nested-card').filter(has=page.get_by_label('On these days', exact=True))
        expect(rule.get_by_label('On these days', exact=True)).to_have_value('open' if repair == 'hours' else 'closed')
        expect(rule.locator('.pick-button')).to_have_attribute('aria-label', 'Entries: Holiday')
        if repair == 'hours':
            expect(rule.get_by_label('Open from', exact=True)).to_have_value('10:00')
            expect(rule.get_by_label('Open until', exact=True)).to_have_value('14:00')
        assert not errors
    finally:
        context.close()


def test_condition_builder_offers_the_date_set_and_its_entries(browser, live_server, h):
    record = date_set(h)
    flag(h)
    where = {'match': 'all', 'conditions': [{'field': 'date_set_ids', 'op': 'any', 'values': [record['id']], 'entry_names': ['Eid al-Fitr']}]}
    rule = catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Department default'),
                                                 line({'max': {'value': 2, 'level': 'mandatory'}}, where=where, note='Holidays')])
    default, holidays = rule['definition']['rule']['lines']
    assert holidays['where']['conditions'][0]['entry_names'] == ['Eid al-Fitr']
    context, page = sign_in(browser, live_server, 'coordinator'); errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        navigate(page, f'rules/duties_per_week/{default["id"]}')
        panel = page.locator('.rc-line-panel')
        where_side = panel.locator('.rc-lp-section').nth(1)
        add_filter(where_side)
        crow = where_side.locator('.cond-line').first
        crow.get_by_label('Filter on', exact=True).select_option(label='On a date set')
        picker = page.locator('.picker')
        picker.get_by_role('option', name='Public holidays', exact=True).check()
        page.keyboard.press('Escape')
        expect(picker).to_have_count(0)
        # The set's named entries narrow the line; none chosen means every entry.
        entries = crow.locator('.cond-entries')
        expect(entries).to_contain_text('All entries')
        entries.locator('.pick-button').click()
        picker.get_by_role('option', name='Eid al-Fitr', exact=True).check()
        page.keyboard.press('Escape')
        expect(entries.locator('.pick-button')).to_have_attribute('aria-label', 'Entries: Eid al-Fitr')
        shot(page, 'date-set-condition-1440-light', full_page=False)
        assert not errors
    finally:
        context.close()
    # Read-only: the line on a date set and one of its entries reads as facts, never as pickers.
    context, page = sign_in(browser, live_server, 'auditor')
    try:
        navigate(page, f'rules/duties_per_week/{holidays["id"]}')
        facts = page.locator('.rc-line-panel .cond-facts')
        expect(facts.first).to_contain_text('On a date set')
        expect(facts.first).to_contain_text('Public holidays')
        expect(facts.first).to_contain_text('only')
        expect(facts.first).to_contain_text('Eid al-Fitr')
        expect(page.locator('.rc-line-panel .cond-line')).to_have_count(0)
        # The line's sentence and its Where pill name the entry, "on" a date set (a day, not a place).
        expect(page.locator('.rc-line-panel')).to_contain_text('on Public holidays (Eid al-Fitr)')
        expect(page.locator('.rp[title="Public holidays (Eid al-Fitr)"]').first).to_be_visible()
    finally:
        context.close()


def test_a_line_on_a_day_in_every_chosen_date_set_is_proposed(browser, live_server, h):
    holidays, ramadan = date_set(h), date_set(h, 'Ramadan', [
        {'name': 'Ramadan', 'repeat': 'yearly_hijri', 'first_day': '1447-09-01', 'last_day': '1447-09-29'}])
    flag(h)
    rule = catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Department default')])
    default = rule['definition']['rule']['lines'][0]
    context, page = sign_in(browser, live_server, 'coordinator'); errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        navigate(page, f'rules/duties_per_week/{default["id"]}')
        panel = page.locator('.rc-line-panel')
        where_side = panel.locator('.rc-lp-section').nth(1)
        add_filter(where_side)
        crow = where_side.locator('.cond-line').first
        crow.get_by_label('Filter on', exact=True).select_option(label='On a date set')
        picker = page.locator('.picker')
        picker.get_by_role('option', name='Public holidays', exact=True).check()
        picker.get_by_role('option', name='Ramadan', exact=True).check()
        page.keyboard.press('Escape')
        expect(picker).to_have_count(0)
        # A day can sit in several sets: "has all of" is offered, in a day's words.
        match = crow.get_by_role('radiogroup', name='Match', exact=True)  # the shared segmented() (dom.ts)
        match.get_by_role('radio', name='All of', exact=True).click()
        expect(match.get_by_role('radio', name='All of', exact=True)).to_have_attribute('aria-checked', 'true')
        expect(match.get_by_role('radio', name='All of', exact=True)).to_have_attribute('data-tip', 'Matches only a day in every one of them')
        expect(crow.locator('.cond-hint')).to_contain_text('a day in every one')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        page.locator('.topbar').get_by_role('button', name='Propose 1 change', exact=True).click()
        reason(page, 'Lighter weeks when a holiday falls in Ramadan', 'Propose')
        expect(page.locator('.rc-waiting')).to_contain_text('waiting for approval')
        entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_week')
        proposed = next(l for l in entry['proposed']['definition']['rule']['lines'] if l['id'] == default['id'])
        assert proposed['where']['conditions'] == [{'field': 'date_set_ids', 'op': 'all', 'values': [holidays['id'], ramadan['id']]}]
        assert not errors
    finally:
        context.close()


def test_dates_show_the_last_year_chosen_when_replies_arrive_out_of_order(browser, live_server, h):
    date_set(h)
    held = []

    def hold_earlier_year(route):
        if f'start={earlier}-01-01' in route.request.url:
            held.append(route)
        else:
            route.continue_()
    context, page = sign_in(browser, live_server, 'admin'); errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        open_config(page, SECTION)
        page.get_by_role('button', name='Dates of Public holidays', exact=True).click()
        panel = page.locator('.dock-panel')
        founding = panel.locator('tbody tr').filter(has_text='Founding Day')
        year = panel.get_by_label('Year', exact=True)
        this_year = int(year.input_value())
        expect(founding).to_contain_text(day_words(f'{this_year}-02-22'))
        earlier, later = this_year - 1, this_year + 1
        page.route('**/api/date-sets/resolved?*', hold_earlier_year)
        with page.expect_request(lambda r: f'start={earlier}-01-01' in r.url):
            year.select_option(str(earlier))
        year.select_option(str(later))
        expect(founding).to_contain_text(day_words(f'{later}-02-22'))
        # The slower reply for the year no longer chosen arrives last and is dropped.
        with page.expect_response(lambda r: f'start={earlier}-01-01' in r.url):
            held[0].continue_()
        page.evaluate('() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))')
        expect(founding).to_contain_text(day_words(f'{later}-02-22'))
        expect(panel.locator('tbody')).not_to_contain_text(str(earlier))
        expect(year).to_have_value(str(later))
        assert not errors
    finally:
        context.close()


def test_schedule_week_marks_the_named_day(browser, live_server, h):
    date_set(h, 'Department days', [{'name': 'Staff training day', 'repeat': 'once', 'first_day': '2026-10-06', 'last_day': '2026-10-06'}])
    w = h.shift(h.workspace('Marked week'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, w['id'])
        head = page.locator('.wk-head .wk-day').filter(has=page.locator('.date-mark'))
        expect(head).to_have_count(1)
        expect(head.locator('.date-mark')).to_have_text('Staff training day')
        expect(head).to_have_attribute('aria-label', re.compile('Staff training day'))
        shot(page, 'date-set-week-marker-1440-light', full_page=False)
        # Month: the day's cell names it too.
        navigate(page, 'schedule/month/2026-10-06')
        expect(page.locator('#main .date-mark')).to_have_count(1)
        expect(page.locator('#main .date-mark')).to_have_text('Staff training day')
        shot(page, 'date-set-month-marker-1440-light', full_page=False)
        # Year: the day is underlined and named in its accessible name and tooltip.
        navigate(page, 'schedule/year/2026-10-06')
        marked = page.locator('.sd-yday.has-date-mark')
        expect(marked).to_have_count(1)
        expect(marked).to_have_attribute('aria-label', re.compile('Staff training day'))
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_holidays_screens_fit_the_width(browser, live_server, h, width, theme):
    from .test_browser_shared_controls import panel_has_handle

    date_set(h)
    context, page = themed(browser, live_server, 'admin', width, theme)
    try:
        open_config(page, SECTION)
        expect(page.locator('[data-row]').filter(has_text='Public holidays')).to_be_visible()
        shot(page, f'date-sets-list-{width}-{theme}', full_page=False)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.get_by_role('button', name='Dates of Public holidays', exact=True).click()
        expect(page.locator('.dock-panel tbody tr').filter(has_text='Founding Day')).to_be_visible()
        panel_has_handle(page, 'date-set-dates', width > 400)
        shot(page, f'date-sets-dates-{width}-{theme}', full_page=False)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.locator('.dock-panel').get_by_role('button', name=re.compile('^Close')).first.click()
        page.locator('[data-row]').filter(has_text='Public holidays').get_by_role('button', name='Edit', exact=True).click()
        expect(page.get_by_role('dialog').locator('.ds-entry')).to_have_count(2)
        shot(page, f'date-sets-form-{width}-{theme}', full_page=False)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()

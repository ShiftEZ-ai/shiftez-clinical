"""System › Bulk editor (sheet.ts, docs/design/bulk-changes.md §2.9): a record type from the registry as a Sheet — cells
edited, filled down and pasted from Excel are staged (never saved until Preview), Preview is the shared one (problems
with fixes), Apply is one bulk.apply with one Undo; keyboard only; dark; read-only for the auditor."""
from .browser_seed import seeded
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in, toolbar  # noqa: F401

pytestmark = pytest.mark.browser
ROUTE = 'system/bulk'
CRED = 'credentialer'


def clinic(h, name):
    return h.configure('services', {'name': name, 'starts_on': '2020-01-01'})['record']


def permit(h, person, service, starts_on='2026-10-01', ends_on='2026-11-01'):
    return h.command(CRED, 'eligibility.create', {'person_id': person, 'service_id': service['id'],
                                                   'starts_on': starts_on, 'ends_on': ends_on})['record']


def staff_numbers(h):
    """Staff numbers for the three demo people (Staff 01(test) 10423, Staff 02(test) 10431, Staff 03(test) 10977)."""
    people = {p['id']: p for p in h.get('admin', '/api/catalogues')['people']}
    for pid, number in zip(h.seed['people'], ('10423', '10431', '10977')):
        p = people[pid]
        h.configure('people', {**{k: p[k] for k in ('display_name',) if k in p}, 'staff_number': number}, pid, p['revision'])


@seeded
def walk_in(h):
    """Walk-in North permissions for the three demo people, each ending 1 Nov 2026 (last day 31 Oct)."""
    service = clinic(h, 'Walk-in North')
    return service, [permit(h, pid, service) for pid in h.seed['people']]


def open_sheet(page, record_type='Clinic permissions'):
    navigate(page, ROUTE)
    main = page.locator('#main')
    pick = main.get_by_role('button', name=re.compile(r'^Record type: '))
    if not re.search(record_type, pick.get_attribute('aria-label') or ''):
        pick.click()
        page.get_by_role('listbox', name='Record types').get_by_role('option', name=re.compile('^' + re.escape(record_type))).click()
    grid = main.get_by_role('grid', name=record_type + ' sheet')
    expect(grid).to_be_visible()
    return main, grid


def cell(grid, column, *texts):
    """The cell under the column headed `column` of the one row holding every text."""
    # The Sheet redraws (a skeleton, then the grid) after a save or Undo: wait for the header before reading them all.
    expect(grid.locator('thead th').first).to_be_visible()
    headers = [t.strip() for t in grid.locator('thead th').all_inner_texts()]
    rows = grid.locator('tbody tr')
    for t in texts:
        rows = rows.filter(has_text=t)
    expect(rows).to_have_count(1)
    return rows.get_by_role('gridcell').nth(headers.index(column))


def stage_bar(main):
    return main.locator('.stage-bar')


def test_sheet_date_filters_use_relative_last_days_and_empty_ends(browser, live_server, h):
    resolved = h.client(CRED).post('/api/conditions/resolve-operand', json={
        'field': 'ends_on', 'collection': 'eligibilities', 'table_key': 'bulk-editor-eligibilities',
        'operand': {'kind': 'relative', 'token': 'this_month'}}).json()
    service = clinic(h, 'Relative sheet boundary')
    alex, jordan, _ = h.seed['people']
    permit(h, alex, service, resolved['from'], (date.fromisoformat(resolved['to']) + timedelta(days=1)).isoformat())
    permit(h, jordan, service, resolved['from'], None)
    context, page = sign_in(browser, live_server, CRED)
    try:
        main, grid = open_sheet(page)
        main.get_by_role('searchbox', name='Search rows').fill('Relative sheet boundary')
        expect(grid.locator('tbody tr')).to_have_count(2)
        pop = toolbar(page, 'Filter')
        row = pop.locator('.cond-root .cond-line').last
        row.get_by_label('Filter on', exact=True).select_option(label='Until (included)')
        picker = page.locator('.picker')
        expect(picker).to_be_visible()
        picker.get_by_role('option', name=re.compile('^This month')).click()
        expect(row.locator('.cond-value-name')).to_have_text('This month')
        expect(row.locator('.cond-value-dates')).not_to_be_empty()
        expect(grid.locator('tbody tr')).to_have_count(1)
        expect(grid.locator('tbody tr')).to_contain_text('Staff 01(test)')
        row.get_by_label('Operator', exact=True).select_option('empty')
        expect(row.locator('.cond-value')).to_have_count(0)
        expect(grid.locator('tbody tr')).to_have_count(1)
        expect(grid.locator('tbody tr')).to_contain_text('Staff 02(test)')
        row.get_by_label('Operator', exact=True).select_option('not_empty')
        expect(grid.locator('tbody tr')).to_have_count(1)
        expect(grid.locator('tbody tr')).to_contain_text('Staff 01(test)')
    finally:
        context.close()


def test_edit_fill_down_preview_apply_and_undo(browser, live_server, h):
    service, permits = walk_in(h)
    context, page = sign_in(browser, live_server, CRED)
    try:
        main, grid = open_sheet(page)
        # The shared toolbar narrows the Sheet: only the Walk-in North permissions, one under another.
        main.get_by_role('searchbox', name='Search rows').fill('Walk-in')
        expect(grid.locator('tbody tr')).to_have_count(3)
        until = cell(grid, 'Until (included)', 'Staff 01(test)', 'Walk-in North')
        # One cell is in the tab order; Enter edits with the shared date field in the cell bar.
        until.click()
        expect(until).to_have_attribute('tabindex', '0')
        page.keyboard.press('Enter')
        box = main.get_by_label('Until (included) for Staff 01(test)')
        expect(box).to_be_focused()
        box.fill('31/12/2026')
        box.press('Enter')
        expect(until).to_have_class(re.compile(r'\bis-staged\b'))
        expect(until.locator('.was')).to_have_text('31 Oct 2026')
        expect(until).to_contain_text('31 Dec 2026')
        expect(until.locator('.sr-only')).to_have_text(', changed, not saved')
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        # Shift+Down twice selects the column for the other two; Ctrl D fills the first value down.
        until.focus()
        page.keyboard.press('Shift+ArrowDown')
        page.keyboard.press('Shift+ArrowDown')
        page.keyboard.press('Control+d')
        expect(grid.locator('td.is-staged')).to_have_count(3)
        expect(stage_bar(main)).to_contain_text('3 changes in 3 rows')
        # Nothing is saved yet.
        assert all(e['ends_on'] == '2026-11-01' for e in h.get(CRED, '/api/catalogues')['eligibilities'] if e['service_id'] == service['id'])
        stage_bar(main).get_by_role('button', name='Save 3 changes').click()
        # A clean small change applies at once (the light path) with the one Undo in the toast.
        toast = page.locator('#toast-tray .toast').filter(has_text='changed')
        expect(toast).to_contain_text('3 clinic permissions changed')
        expect(stage_bar(main)).to_be_hidden()
        changed = grid.locator('td.is-changed')
        expect(changed).to_have_count(3)
        assert {e['ends_on'] for e in h.get(CRED, '/api/catalogues')['eligibilities'] if e['service_id'] == service['id']} == {'2027-01-01'}
        toast.get_by_role('button', name='Undo').click()
        expect(page.locator('#toast-tray')).to_contain_text(re.compile('Undone|undone'))
        assert {e['ends_on'] for e in h.get(CRED, '/api/catalogues')['eligibilities'] if e['service_id'] == service['id']} == {'2026-11-01'}
        # F13 A-29: the values are back, so no cell still says it was changed.
        grid = main.get_by_role('grid', name='Clinic permissions sheet')
        expect(cell(grid, 'Until (included)', 'Staff 01(test)', 'Walk-in North')).to_contain_text('31 Oct 2026')
        expect(grid.locator('td.is-changed')).to_have_count(0)
        expect(grid.locator('.sr-only', has_text=', changed')).to_have_count(0)
    finally:
        context.close()


def test_the_cell_bar_names_the_cell_and_keeps_its_keys_behind_help(browser, live_server, h):
    """F13 A-31: the bar reads "<record> · <field>" and the value; the keys sit behind one help mark."""
    walk_in(h)
    context, page = sign_in(browser, live_server, CRED)
    try:
        main, grid = open_sheet(page)
        bar = main.locator('.cellbar')
        cell(grid, 'Until (included)', 'Staff 01(test)', 'Walk-in North').click()
        expect(bar.locator('.ref')).to_have_text(re.compile(r'^Staff 01\(test\).* · Until \(included\)$'))
        expect(bar).not_to_contain_text('Arrows move')
        expect(bar.get_by_role('button', name='Sheet keys', exact=True)).to_have_attribute('data-tip', re.compile('Enter or F2 edits'))
        grid.get_by_role('gridcell').first.click()
        expect(bar.locator('.ref')).not_to_contain_text('Record')
    finally:
        context.close()


def test_a_read_only_role_opens_on_people_and_empty_types_offer_no_paste(browser, live_server, h):
    """F13 A-30: an auditor opens on People (with rows), not the first type; an empty type says so without a tautology
    and offers no Paste from Excel (nothing to match)."""
    context, page = sign_in(browser, live_server, 'auditor')
    try:
        navigate(page, ROUTE)
        main = page.locator('#main')
        expect(main.get_by_role('button', name=re.compile(r'^Record type: '))).to_have_attribute('aria-label', 'Record type: People')
        expect(main.get_by_role('grid', name='People sheet').locator('tbody tr').first).to_be_visible()
    finally:
        context.close()
    # Date sets: the admin may change them and the seed has none.
    assert h.client('admin').get('/api/bulk/collections').json()['collections']['date_sets']['count'] == 0
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, ROUTE)
        main = page.locator('#main')
        main.get_by_role('button', name=re.compile(r'^Record type: ')).click()
        page.get_by_role('listbox', name='Record types').get_by_role('option', name=re.compile('^Date sets')).click()
        expect(main.get_by_text('No date sets yet', exact=True)).to_be_visible()
        expect(main).not_to_contain_text('appear here once there are some')
        expect(main.get_by_role('button', name='Paste from Excel')).to_have_count(0)
    finally:
        context.close()


def test_discard_is_a_same_button_confirm(browser, live_server, h):
    walk_in(h)
    context, page = sign_in(browser, live_server, CRED)
    try:
        main, grid = open_sheet(page)
        target = cell(grid, 'Until (included)', 'Staff 02(test)', 'Walk-in North')
        target.click()
        page.keyboard.press('F2')
        box = main.get_by_label('Until (included) for Staff 02(test)')
        box.fill('15 Nov 2026')
        box.press('Escape')  # Esc cancels: nothing staged
        expect(stage_bar(main)).to_be_hidden()
        expect(target).to_be_focused()
        page.keyboard.press('Enter')
        main.get_by_label('Until (included) for Staff 02(test)').fill('15 Nov 2026')
        page.keyboard.press('Tab')  # Tab keeps and moves right
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        discard = stage_bar(main).get_by_role('button', name=re.compile('Discard'))
        discard.click()
        expect(stage_bar(main)).to_be_visible()
        stage_bar(main).get_by_role('button', name=re.compile('Discard all')).click()
        expect(stage_bar(main)).to_be_hidden()
        expect(grid.locator('td.is-staged')).to_have_count(0)
    finally:
        context.close()


def test_paste_from_excel_matches_reports_and_stages(browser, live_server, h):
    staff_numbers(h)
    service, _ = walk_in(h)
    # Staff 03 has a second Walk-in North permission (later), so "10977 · Walk-in North" matches two records.
    permit(h, h.seed['people'][2], service, '2026-12-01', '2027-03-01')
    context, page = sign_in(browser, live_server, CRED)
    try:
        main, grid = open_sheet(page)
        main.get_by_role('button', name='Paste from Excel').click()
        dialog = page.get_by_role('dialog', name='Paste from Excel')
        dialog.get_by_label('Pasted').fill('Staff number\tClinic\tLast day\n10423\tWalk-in North\t31/12/2026\n'
                                           '10431\tWalk-in North\t31/12/2026\n10999\tWalk-in North\t31/12/2026\n'
                                           '10977\tWalk-in North\t28/02/2027\n')
        summary = dialog.locator('.matrix-summary')
        expect(summary).to_contain_text('2 rows matched')
        expect(summary).to_contain_text('1 not found')
        expect(summary).to_contain_text('1 matches more than one record')
        expect(summary).to_contain_text('day/month/year')
        lines = dialog.locator('.sheet-paste-lines')
        expect(lines.locator('tr').filter(has_text='10999')).to_contain_text('Not found')
        ambiguous = lines.locator('tr').filter(has_text='10977')
        expect(ambiguous).to_contain_text('2 records')
        stage = dialog.get_by_role('button', name='Stage 2 changes')
        expect(stage).to_be_enabled()
        # Choosing which record the line means stages it too.
        ambiguous.get_by_label(re.compile('Which record for')).click()  # the shared picker
        page.locator('.picker-row[role=option]').filter(has_text='31 Oct 2026').click()
        expect(dialog.locator('button[type=submit]')).to_have_text('Stage 3 changes')
        dialog.get_by_role('button', name='Stage 3 changes').click()
        expect(dialog).to_be_hidden()
        expect(grid.locator('td.is-staged')).to_have_count(3)
        expect(stage_bar(main)).to_contain_text('3 changes in 3 rows')
    finally:
        context.close()


def resolution_label(h, clinic_name, ends_on, resolution_id):
    """The words the server's dry run gives a fix (by its id), for Staff 01's open-ended permission at a clinic."""
    services = {x['id']: x['name'] for x in h.get(CRED, '/api/catalogues')['services']}
    rows = h.client(CRED).post('/api/bulk/rows', json={'selection': {'collection': 'eligibilities'}, 'limit': 100}).json()['rows']
    x = next(r for r in rows if r['person_id'] == h.seed['people'][0] and services.get(r['service_id']) == clinic_name)
    item = {'target_id': x['id'], 'expected_revision': x['revision'], 'changes': [{'field': 'ends_on', 'how': 'set', 'value': ends_on}]}
    check = h.client(CRED).post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': {
        'item_kind': 'eligibility.update_dates', 'items': [item], 'reason': 'Read the fix labels'}}).json()
    return next(z['label'] for i in check['items'][0]['issues'] for z in i['resolutions'] if z['id'] == resolution_id)


def test_conflict_is_resolved_in_preview_then_applied(browser, live_server, h):
    """Giving an open-ended permission a last day shortens it: Preview lists it as a problem with its fixes."""
    context, page = sign_in(browser, live_server, CRED)
    try:
        main, grid = open_sheet(page)
        target = cell(grid, 'Until (included)', 'Staff 01(test)', 'Continuity clinic')
        target.click()
        page.keyboard.press('Enter')
        main.get_by_label('Until (included) for Staff 01(test)').fill('31 Dec 2026')
        page.keyboard.press('Enter')
        stage_bar(main).get_by_role('button', name='Save 1 change').click()
        panel = page.locator('.dock-panel.bulk-panel')
        expect(panel).to_contain_text('nothing saved yet')
        expect(panel.locator('.bulk-tile.err strong')).to_have_text('1')
        panel.get_by_role('button', name=re.compile('^Fix ')).first.click()
        page.get_by_role('menuitem', name=resolution_label(h, 'Continuity clinic', '2027-01-01', 'shorten_anyway')).click()
        expect(panel.locator('.bulk-clear')).to_contain_text('All clear')
        panel.get_by_role('button', name=re.compile(r'^Apply ready rows \(1\)')).click()
        expect(page.locator('#toast-tray')).to_contain_text('1 clinic permission changed')
        expect(grid.locator('td.is-changed')).to_have_count(1)
        alex = [e for e in h.get(CRED, '/api/catalogues')['eligibilities'] if e['person_id'] == h.seed['people'][0] and e['ends_on']]
        assert [e['ends_on'] for e in alex] == ['2027-01-01']
    finally:
        context.close()


def test_keyboard_only_grid_and_copy(browser, live_server, h):
    walk_in(h)
    context, page = sign_in(browser, live_server, CRED)
    try:
        main, grid = open_sheet(page)
        # Exactly one cell is tabbable (roving tabindex); arrows move it.
        expect(grid.locator('td[tabindex="0"]')).to_have_count(1)
        grid.locator('td[tabindex="0"]').focus()
        page.keyboard.press('ArrowRight')
        focused = grid.locator('td[tabindex="0"]')
        expect(focused).to_be_focused()
        expect(focused).to_have_attribute('aria-colindex', '2')
        page.keyboard.press('ArrowDown')
        expect(grid.locator('td[tabindex="0"]')).to_have_attribute('aria-colindex', '2')
        page.keyboard.press('End')
        expect(grid.locator('td[tabindex="0"]')).to_have_attribute('aria-colindex', re.compile(r'[3-9]'))
        page.keyboard.press('Home')
        expect(grid.locator('td[tabindex="0"]')).to_have_attribute('aria-colindex', '1')
        page.keyboard.press('Shift+ArrowRight')
        expect(grid.locator('td[aria-selected="true"]')).to_have_count(2)
        expect(main.locator('.cellbar')).to_contain_text('2 cells selected')
        page.keyboard.press('Escape')
        expect(grid.locator('td[aria-selected="true"]')).to_have_count(1)
        page.keyboard.press('Home')
        # The record column is never edited: Enter says so instead.
        page.keyboard.press('Enter')
        expect(main.locator('.cellbar input, .cellbar select')).to_have_count(0)
        expect(page.locator('#sr-live')).to_have_text(re.compile('changed with Change|read-only|Read only'))
        # Ctrl C copies the range as tab-separated text (the copy event).
        copied = page.evaluate("""() => new Promise(done => {
            const cell = document.querySelector('[role=grid] td[tabindex="0"]');
            const data = new DataTransfer();
            const e = new ClipboardEvent('copy', {clipboardData: data, bubbles: true, cancelable: true});
            cell.dispatchEvent(e); done(data.getData('text/plain'));})""")
        assert copied.strip()
    finally:
        context.close()


@pytest.mark.parametrize('dark', [False, True])
def test_a_role_that_may_not_change_a_type_reads_it_only(browser, live_server, h, dark):
    """Clinic permissions are changed by credentialers: a coordinator sees the same Sheet read-only (light and dark)."""
    walk_in(h)
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme='dark' if dark else 'light')
    try:
        main, grid = open_sheet(page)
        expect(grid).to_have_attribute('aria-readonly', 'true')
        expect(main.get_by_role('button', name='Paste from Excel')).to_have_count(0)
        target = cell(grid, 'Until (included)', 'Staff 01(test)', 'Walk-in North')
        target.click()
        page.keyboard.press('Enter')
        expect(main.get_by_label('Until (included) for Staff 01(test)')).to_have_count(0)
        expect(main.locator('.cellbar')).to_contain_text('Read only')
        page.keyboard.press('Control+d')
        expect(stage_bar(main)).to_be_hidden()
        expect(main.get_by_role('button', name='Fill down')).to_have_count(0)
        if dark:
            bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
            assert bg not in ('rgb(255, 255, 255)', 'rgb(248, 250, 252)'), bg
    finally:
        context.close()


def test_record_types_from_the_registry_and_the_fill_handle(browser, live_server, h):
    walk_in(h)
    context, page = sign_in(browser, live_server, 'admin')
    try:
        main, grid = open_sheet(page)
        pick = main.get_by_role('button', name=re.compile(r'^Record type: '))
        pick.click()
        # The shared picker (picker.ts): the types grouped, each with its count, searchable.
        menu = page.get_by_role('listbox', name='Record types')
        for group in ('People & permissions', 'Configuration'):
            expect(menu.locator('.picker-group').filter(has_text=group)).to_have_count(1)
        # Counts arrive as the list opens (Clinic permissions: 6 demo + 3 Walk-in North).
        expect(menu.get_by_role('option', name=re.compile('^Clinic permissions'))).to_contain_text('9')
        page.get_by_role('combobox', name='Search Record types').fill('servi')
        menu.get_by_role('option', name=re.compile('^Services')).click()
        services = main.get_by_role('grid', name='Services sheet')
        expect(services).to_be_visible()
        expect(services.locator('tbody tr').filter(has_text='Walk-in North')).to_have_count(1)
        # Back to permissions: drag the corner of a cell down over two rows to fill them.
        main, grid = open_sheet(page)
        main.get_by_role('searchbox', name='Search rows').fill('Walk-in')
        expect(grid.locator('tbody tr')).to_have_count(3)
        first = cell(grid, 'Until (included)', 'Staff 01(test)')
        first.click()
        page.keyboard.press('Enter')
        main.get_by_label('Until (included) for Staff 01(test)').fill('15 Dec 2026')
        page.keyboard.press('Enter')
        first.click()
        handle = first.locator('.fill-handle')
        expect(handle).to_have_count(1)
        handle.dispatch_event('pointerdown', {'button': 0, 'bubbles': True})
        cell(grid, 'Until (included)', 'Staff 03(test)').dispatch_event('pointerover', {'bubbles': True})
        page.evaluate("window.dispatchEvent(new PointerEvent('pointerup'))")
        expect(grid.locator('td.is-staged')).to_have_count(3)
        expect(stage_bar(main)).to_contain_text('3 changes in 3 rows')
    finally:
        context.close()


def test_fill_down_by_single_clicks_stages_what_the_fill_handle_does(browser, live_server, h):
    """WCAG 2.2 SC 2.5.7 (issue 1727): Fill down, a click on the pattern's last cell and one on the last row fill a
    two-row pattern down with no pointer held down: the same staged cells as dragging the corner, read-only columns
    left as they were, each step announced, and the save undone by its one Undo."""
    service, _ = walk_in(h)
    # Two of them also at a second clinic: five rows, and no filled date makes one record overlap another.
    south = clinic(h, 'Walk-in South')
    for pid in h.seed['people'][:2]:
        permit(h, pid, south)
    context, page = sign_in(browser, live_server, CRED)
    try:
        main, grid = open_sheet(page)
        main.get_by_role('searchbox', name='Search rows').fill('Walk-in')
        rows = grid.locator('tbody tr')
        expect(rows).to_have_count(5)
        headers = [t.strip() for t in grid.locator('thead th').all_inner_texts()]
        until, clinic_col = headers.index('Until (included)'), headers.index('Clinic')
        live = page.locator('#sr-live')
        fill = main.locator('.cellbar').get_by_role('button', name='Fill down', exact=True)

        def at(r, c):
            return rows.nth(r).get_by_role('gridcell').nth(c)

        def pattern():
            for r, day in ((0, '15/12/2026'), (1, '20/12/2026')):
                at(r, until).click()
                page.keyboard.press('Enter')
                box = main.get_by_label(re.compile(r'^Until \(included\) for '))
                box.fill(day)
                box.press('Enter')
            expect(grid.locator('td.is-staged')).to_have_count(2)

        def until_texts():
            return [at(r, until).inner_text() for r in range(5)]

        # The corner handle, dragged from the two-row pattern to the last row.
        pattern()
        at(0, until).click()
        at(1, until).click(modifiers=['Shift'])
        at(1, until).locator('.fill-handle').dispatch_event('pointerdown', {'button': 0, 'bubbles': True})
        at(4, until).dispatch_event('pointerover', {'bubbles': True})
        page.evaluate("window.dispatchEvent(new PointerEvent('pointerup'))")
        expect(grid.locator('td.is-staged')).to_have_count(5)
        dragged = until_texts()
        assert [t.count('15 Dec 2026') + t.count('20 Dec 2026') for t in dragged] == [1] * 5
        stage_bar(main).get_by_role('button', name=re.compile('Discard')).click()
        stage_bar(main).get_by_role('button', name=re.compile('Discard all')).click()
        expect(stage_bar(main)).to_be_hidden()

        # The same fill with single clicks: Fill down, the pattern's last cell, the last row.
        pattern()
        at(0, until).click()
        expect(fill).to_have_attribute('aria-pressed', 'false')
        fill.click()
        expect(fill).to_have_attribute('aria-pressed', 'true')
        expect(main.locator('.cellbar')).to_contain_text('Choose the last cell of the pattern.')
        expect(live).to_have_text('Choose the last cell of the pattern.')
        at(1, until).click()
        expect(live).to_have_text('Choose the last row to fill.')
        at(1, until).click()
        expect(live).to_have_text('Choose a row below the pattern.')
        expect(grid.locator('td.is-staged')).to_have_count(2)
        at(4, until).click()
        expect(live).to_have_text('3 cells filled down · not saved yet')
        expect(fill).to_have_attribute('aria-pressed', 'false')
        expect(grid.locator('td.is-staged')).to_have_count(5)
        assert until_texts() == dragged

        # A pattern over a read-only column: only the editable column fills, and the count says so.
        at(0, until).click()
        fill.click()
        at(0, clinic_col).click()
        at(4, clinic_col).click()
        expect(live).to_have_text('4 cells filled down · not saved yet')
        for r in range(5):
            expect(at(r, until)).to_contain_text('15 Dec 2026')
            expect(at(r, clinic_col)).not_to_have_class(re.compile(r'\bis-staged\b'))
        # Esc leaves Fill down without changing anything.
        fill.click()
        page.keyboard.press('Escape')
        expect(live).to_have_text('Fill down cancelled.')
        expect(fill).to_have_attribute('aria-pressed', 'false')

        def ends():
            return sorted(e['ends_on'] for e in h.get(CRED, '/api/catalogues')['eligibilities'] if e['service_id'] in (service['id'], south['id']))

        assert ends() == ['2026-11-01'] * 5
        stage_bar(main).get_by_role('button', name='Save 5 changes').click()
        toast = page.locator('#toast-tray .toast').filter(has_text='changed')
        expect(toast).to_contain_text('5 clinic permissions changed')
        assert ends() == ['2026-12-16'] * 5
        toast.get_by_role('button', name='Undo').click()
        expect(page.locator('#toast-tray')).to_contain_text(re.compile('Undone|undone'))
        assert ends() == ['2026-11-01'] * 5
    finally:
        context.close()


def test_a_paste_choice_holds_only_while_its_line_still_names_that_record(browser, live_server, h):
    """Review #1: the record chosen for an ambiguous pasted line is dropped when that line changes (here to a staff
    number that matches nobody), so Stage never changes a record the paste no longer names."""
    staff_numbers(h)
    service, _ = walk_in(h)
    permit(h, h.seed['people'][2], service, '2026-12-01', '2027-03-01')
    head = 'Staff number\tClinic\tLast day\n10423\tWalk-in North\t31/12/2026\n'
    context, page = sign_in(browser, live_server, CRED)
    try:
        main, grid = open_sheet(page)
        main.get_by_role('button', name='Paste from Excel').click()
        dialog = page.get_by_role('dialog', name='Paste from Excel')
        pasted = dialog.get_by_role('textbox', name='Pasted')
        pasted.fill(head + '10977\tWalk-in North\t28/02/2027\n')
        lines = dialog.locator('.sheet-paste-lines')
        ambiguous = lines.locator('tr').filter(has_text='10977')
        expect(ambiguous).to_contain_text('2 records')
        ambiguous.get_by_label(re.compile('Which record for')).click()
        page.locator('.picker-row[role=option]').filter(has_text='31 Oct 2026').click()
        submit = dialog.locator('button[type=submit]')
        expect(submit).to_have_text('Stage 2 changes')
        # The same header, the chosen line now names nobody: the choice goes with it.
        pasted.fill(head + '10999\tWalk-in North\t28/02/2027\n')
        expect(lines.locator('tr').filter(has_text='10999')).to_contain_text('Not found')
        expect(submit).to_have_text('Stage 1 change')
        # Back to the ambiguous staff number: it asks again rather than reusing the old choice.
        pasted.fill(head + '10977\tWalk-in North\t28/02/2027\n')
        expect(lines.locator('tr').filter(has_text='10977')).to_contain_text('2 records')
        expect(submit).to_have_text('Stage 1 change')
        submit.click()  # the line left to choose is skipped, not a blocked form
        expect(dialog).to_be_hidden()
        expect(grid.locator('td.is-staged')).to_have_count(1)
    finally:
        context.close()


def test_an_ordinary_list_sheet_reads_its_rows_again_after_apply(browser, live_server, h):
    """Review #11: after Apply in a list's Sheet (People › Show their… › Clinic permissions › Sheet) the list reads its
    records again (its afterChange), so a second edit of the same record uses the new revision and saves too."""
    service, _ = walk_in(h)
    context, page = sign_in(browser, live_server, CRED)
    try:
        navigate(page, 'people')
        page.locator('#main tr', has_text='Staff 01(test)').locator('.bulk-check').check()
        page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Show their…').click()
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Clinic permissions')).click()
        main = page.locator('#main')
        toolbar(main, 'View settings').get_by_role('radio', name='Sheet', exact=True).click()
        page.keyboard.press('Escape')
        for typed, saved in (('31/12/2026', '2027-01-01'), ('15/01/2027', '2027-01-16')):
            grid = main.get_by_role('grid', name='Clinic permissions sheet')
            target = cell(grid, 'Until (included)', 'Walk-in North')
            target.click()
            page.keyboard.press('Enter')
            main.get_by_label('Until (included) for Staff 01(test)').fill(typed)
            page.keyboard.press('Enter')
            stage_bar(main).get_by_role('button', name='Save 1 change').click()
            expect(page.locator('#toast-tray .toast').filter(has_text='1 clinic permission changed').last).to_be_visible()
            expect(stage_bar(main)).to_be_hidden()
            alex = [e for e in h.get(CRED, '/api/catalogues')['eligibilities'] if e['service_id'] == service['id'] and e['person_id'] == h.seed['people'][0]]
            assert [e['ends_on'] for e in alex] == [saved], alex
            expect(cell(main.get_by_role('grid', name='Clinic permissions sheet'), 'Until (included)', 'Walk-in North')).to_contain_text(typed[:2].lstrip('0'))
            page.locator('#toast-tray .toast').evaluate_all('ts => ts.forEach(t => t.remove())')
    finally:
        context.close()


def test_stage_waits_while_pasted_names_are_being_matched(browser, live_server, h):
    """Review #1: while the server is matching pasted staff numbers, Stage is off (the lines it would stage are stale);
    it comes back once the matches arrive."""
    staff_numbers(h)
    walk_in(h)
    context, page = sign_in(browser, live_server, CRED)
    held = []
    try:
        main, grid = open_sheet(page)
        main.get_by_role('button', name='Paste from Excel').click()
        dialog = page.get_by_role('dialog', name='Paste from Excel')
        pasted = dialog.get_by_role('textbox', name='Pasted')
        submit = dialog.locator('button[type=submit]')
        pasted.fill('Staff number\tClinic\tLast day\n10423\tWalk-in North\t31/12/2026\n')
        expect(submit).to_have_text('Stage 1 change')
        page.route('**/api/bulk/resolve', lambda route: held.append(route))
        # The matching request is on its way (held here): Stage stays off until its answer arrives.
        with page.expect_request('**/api/bulk/resolve'):
            pasted.fill('Staff number\tClinic\tLast day\n10431\tWalk-in North\t31/12/2026\n')
            expect(submit).to_be_disabled()
        expect(dialog.locator('.matrix-summary')).to_contain_text('Matching names')
        expect(submit).to_be_disabled()
        for route in held:
            route.continue_()
        page.unroute('**/api/bulk/resolve')
        expect(submit).to_have_text('Stage 1 change')
        expect(submit).to_be_enabled()
        submit.click()
        expect(dialog).to_be_hidden()
        expect(cell(grid, 'Until (included)', 'Staff 02(test)', 'Walk-in North')).to_have_class(re.compile(r'\bis-staged\b'))
    finally:
        context.close()


def contact_labels():
    """Every name a contact column could carry: the contact types, their old record-field labels and the People list's."""
    from packages.contracts.schemas import DEFAULT_CONTACT_TYPES
    return {t['name'] for t in DEFAULT_CONTACT_TYPES} | {'Work phone', 'Extension', 'Pager', 'Work email'}


def people_view(page, layout):
    main = page.locator('#main')
    toolbar(main, 'View settings').get_by_role('radio', name=layout, exact=True).click()
    page.keyboard.press('Escape')
    return main


def test_a_list_shown_as_sheet_keeps_its_grouping_and_shown_columns(browser, live_server, h):
    """Review #12: People › Group by Status, Title and Short code shown → Sheet keeps the grouping (rows in their
    groups, the grouped column shown) and edits the shown columns as their record fields; back to Table keeps the view
    settings. Contacts are never bulk fields (G3-32e): a shown Work phone stays out of the Sheet."""
    p = next(x for x in h.get('admin', '/api/catalogues')['people'] if x['id'] == h.seed['people'][1])
    h.command('admin', 'catalogue.update', {'catalogue': 'people', 'value': {**{k: p[k] for k in ('display_name',)}, 'active': False},
              'reason': 'Test setup'}, p['id'], p['revision'])
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'people')
        main = page.locator('#main')
        pop = toolbar(main, 'Filter')
        if pop.get_by_role('button', name='Clear all filters', exact=True).count():
            pop.get_by_role('button', name='Clear all filters', exact=True).click()
        page.keyboard.press('Escape')
        settings = toolbar(main, 'View settings')
        for name in ('Title', 'Short code', 'Work phone'):
            box = settings.get_by_role('checkbox', name=name, exact=True)
            if not box.is_checked():
                box.check()
        page.keyboard.press('Escape')
        toolbar(main, 'Group').get_by_role('menuitemradio', name='Status').click()
        page.keyboard.press('Escape')
        people_view(page, 'Sheet')
        grid = main.get_by_role('grid', name='People sheet')
        headers = [t.strip() for t in grid.locator('thead th').all_inner_texts()]
        assert {'In use', 'Title', 'Short code'} <= set(headers), headers
        assert not set(headers) & contact_labels(), headers
        # Grouped by In use: the active people first, the one not in use last.
        names = grid.locator('tbody tr td:first-child').all_inner_texts()
        assert names[-1].strip() == p['display_name'], names
        target = cell(grid, 'Title', 'Staff 01(test)')
        target.click()
        page.keyboard.press('Enter')
        main.get_by_label('Title for Staff 01(test)').fill('Sheet title')
        page.keyboard.press('Enter')
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        stage_bar(main).get_by_role('button', name=re.compile('Discard')).click()
        stage_bar(main).get_by_role('button', name=re.compile('Discard all')).click()
        people_view(page, 'Table')
        expect(main.get_by_role('button', name=re.compile('^Group'))).to_contain_text('Status')
        for name in ('Title', 'Short code', 'Work phone'):
            expect(main.locator('th', has_text=name)).to_have_count(1)
    finally:
        context.close()


def test_a_list_sheet_is_desktop_only_on_a_phone_width_and_after_a_resize(browser, live_server, h):
    """Review #19: a list shown as Sheet gives the approved desktop-only notice at phone width, also when the window
    narrows after it was drawn, and the grid again when it widens."""
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'people')
        main = people_view(page, 'Sheet')
        expect(main.get_by_role('grid', name='People sheet')).to_be_visible()
        page.set_viewport_size({'width': 390, 'height': 844})
        expect(main.get_by_role('heading', name='Best on a desktop')).to_be_visible()
        expect(main.get_by_role('grid', name='People sheet')).to_have_count(0)
        page.set_viewport_size({'width': 1440, 'height': 900})
        expect(main.get_by_role('grid', name='People sheet')).to_be_visible()
        page.set_viewport_size({'width': 390, 'height': 844})
        page.reload()
        expect(page.locator('#main').get_by_role('heading', name='Best on a desktop')).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('second', ['admin', 'auditor'])
def test_staged_cells_end_with_the_session_that_staged_them(browser, live_server, h, second):
    """Review #2: account A stages a Sheet edit and signs out; account B signs in in the same tab and finds nothing
    staged, no Preview bar and no marked cells (an auditor too)."""
    from .conftest import TEST_PASSWORD
    walk_in(h)
    context, page = sign_in(browser, live_server, CRED)
    try:
        main, grid = open_sheet(page)
        target = cell(grid, 'Until (included)', 'Staff 01(test)', 'Walk-in North')
        target.click()
        page.keyboard.press('Enter')
        main.get_by_label('Until (included) for Staff 01(test)').fill('31/12/2026')
        page.keyboard.press('Enter')
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        page.get_by_role('button', name=re.compile(r'^Account menu, ')).click()
        page.get_by_role('menuitem', name='Sign out', exact=True).click()
        another = page.get_by_role('button', name='Use another account')
        if another.count():
            another.click()
        page.get_by_label('Username or email', exact=True).fill(second)
        page.get_by_role('button', name='Continue', exact=True).click()
        page.get_by_label('Password', exact=True).fill(TEST_PASSWORD)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible()
        main, grid = open_sheet(page)
        expect(grid.locator('tbody tr').filter(has_text='Walk-in North').first).to_be_visible()
        expect(grid.locator('td.is-staged, td.is-changed')).to_have_count(0)
        expect(stage_bar(main)).to_be_hidden()
        expect(main.get_by_role('button', name=re.compile(r'^(Preview|Save) \d'))).to_have_count(0)
    finally:
        context.close()


def test_a_late_reply_for_an_earlier_paste_never_enables_stage(browser, live_server, h):
    """Review (4th) #4: the matching reply for paste A arrives after paste B was typed but before B is read: Stage is
    never enabled for A's rows; only B's own reply decides (here nothing to stage)."""
    staff_numbers(h)
    walk_in(h)
    context, page = sign_in(browser, live_server, CRED)
    held = []
    try:
        main, grid = open_sheet(page)
        main.get_by_role('button', name='Paste from Excel').click()
        dialog = page.get_by_role('dialog', name='Paste from Excel')
        pasted = dialog.get_by_role('textbox', name='Pasted')
        submit = dialog.locator('button[type=submit]')
        # Every moment Stage is enabled is recorded (a brief one would not be seen by polling).
        submit.evaluate("b => { window.__enabled = []; new MutationObserver(() => { if (!b.disabled) window.__enabled.push(b.textContent); })"
                        ".observe(b, {attributes: true, childList: true, characterData: true, subtree: true}); }")
        page.route('**/api/bulk/resolve', lambda route: held.append(route))
        with page.expect_request('**/api/bulk/resolve'):
            pasted.fill('Staff number\tClinic\tLast day\n10423\tWalk-in North\t31/12/2026\n')
        expect(dialog.locator('.matrix-summary')).to_contain_text('Matching names')
        # B is typed; A's answer arrives while B waits to be read.
        with page.expect_request('**/api/bulk/resolve'):
            pasted.fill('Staff number\tClinic\tLast day\n10999\tWalk-in North\t31/12/2026\n')
            held[0].continue_()
        expect(dialog.locator('.matrix-summary')).to_contain_text('Matching names')
        assert len(held) == 2, held
        held[1].continue_()
        page.unroute('**/api/bulk/resolve')
        expect(dialog.locator('.sheet-paste-lines tr').filter(has_text='10999')).to_contain_text('Not found')
        expect(submit).to_have_text('Stage 0 changes')
        expect(submit).to_be_disabled()
        assert page.evaluate('window.__enabled') == [], page.evaluate('window.__enabled')
    finally:
        context.close()


def no_light_path(h):
    """Department bulk limit "Most applied directly" 0: every change opens Preview (nothing applies at once)."""
    me = h.get('admin', '/api/me')
    department, organisation = me['department'], me['organisation']
    limits = h.get('admin', '/api/bulk/settings')['settings']
    h.command('admin', 'department.settings', {'department_name': department['name'], 'department_revision': department['revision'],
              'organisation_name': organisation['name'], 'timezone': organisation['timezone'], 'week_start': organisation['week_start'],
              'bulk_settings': {**limits, 'bulk_direct_max': 0}, 'reason': 'Always preview'}, department['id'], organisation['revision'])


def edit_until(page, main, grid, who, typed):
    target = cell(grid, 'Until (included)', who, 'Walk-in North')
    target.click()
    page.keyboard.press('Enter')
    main.get_by_label(f'Until (included) for {who}').fill(typed)
    page.keyboard.press('Enter')
    return target


def test_a_value_staged_after_preview_survives_the_apply_of_the_earlier_one(browser, live_server, h):
    """Review (5th) #5: A is previewed; B is staged in the same cell before Apply. Apply saves A and keeps B staged (on
    the record's new revision), so B then previews and applies too."""
    service, _ = walk_in(h)
    no_light_path(h)
    context, page = sign_in(browser, live_server, CRED)
    try:
        main, grid = open_sheet(page)
        edit_until(page, main, grid, 'Staff 01(test)', '31/12/2026')
        stage_bar(main).get_by_role('button', name='Save 1 change').click()
        dock = page.locator('.dock-panel.bulk-panel')
        apply = dock.get_by_role('button', name=re.compile(r'^Apply ready rows \(1\)'))
        expect(apply).to_be_visible()
        edit_until(page, main, main.get_by_role('grid', name='Clinic permissions sheet'), 'Staff 01(test)', '15/01/2027')
        apply.click()
        expect(page.locator('#toast-tray')).to_contain_text('1 clinic permission changed')
        alex = lambda: [e for e in h.get(CRED, '/api/catalogues')['eligibilities'] if e['service_id'] == service['id'] and e['person_id'] == h.seed['people'][0]]
        assert [e['ends_on'] for e in alex()] == ['2027-01-01']
        grid = main.get_by_role('grid', name='Clinic permissions sheet')
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        expect(cell(grid, 'Until (included)', 'Staff 01(test)', 'Walk-in North')).to_contain_text('15 Jan 2027')
        page.locator('.dock-panel.bulk-panel').get_by_role('button', name='Close', exact=True).click() if page.locator('.dock-panel.bulk-panel').get_by_role('button', name='Close', exact=True).count() else None
        stage_bar(main).get_by_role('button', name='Save 1 change').click()
        page.locator('.dock-panel.bulk-panel').get_by_role('button', name=re.compile(r'^Apply ready rows \(1\)')).click()
        expect(page.locator('#toast-tray .toast').filter(has_text='1 clinic permission changed')).to_have_count(2)
        assert [e['ends_on'] for e in alex()] == ['2027-01-16']
    finally:
        context.close()


def test_a_datetime_reads_in_the_department_timezone_in_the_sheet_and_its_copy(browser, live_server, h):
    """Review (5th) #6: leave from 08:00 Riyadh time shows (and copies) 08:00, not the UTC 05:00."""
    alex = h.seed['people'][0]
    kind = h.get('admin', '/api/catalogues')['leave_types'][0]['id']
    h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
              'common': {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kind, 'private_reason': 'Morning'},
              'items': [{'payload': {'start': '2027-03-03T08:00:00+03:00', 'end': '2027-03-03T12:30:00+03:00'}}]})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        main, grid = open_sheet(page, 'Leave & availability')
        start = cell(grid, 'From', '3 Mar 2027')
        expect(start).to_contain_text('08:00')
        expect(start).not_to_contain_text('05:00')
        start.click()
        copied = page.evaluate("""() => { const cell = document.querySelector('[role=grid] td[tabindex="0"]'), data = new DataTransfer();
            cell.dispatchEvent(new ClipboardEvent('copy', {clipboardData: data, bubbles: true, cancelable: true})); return data.getData('text/plain'); }""")
        assert '08:00' in copied and '05:00' not in copied, copied
    finally:
        context.close()


def test_staged_cells_ask_before_a_reload_loses_them(browser, live_server, h):
    """Review (5th) #14: staged cells (the Bulk editor's, and a list shown as Sheet) are registered with the page's
    unload guard: a reload asks first."""
    walk_in(h)
    context, page = sign_in(browser, live_server, CRED)
    asked = []

    def answer(dialog):
        asked.append(dialog.type)
        dialog.accept()
    page.on('dialog', answer)
    try:
        main, grid = open_sheet(page)
        page.reload()
        assert asked == [], asked  # nothing staged: no question
        main, grid = open_sheet(page)
        edit_until(page, main, grid, 'Staff 01(test)', '31/12/2026')
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        page.reload()
        assert asked == ['beforeunload'], asked
        # A list's Sheet: People › Show their… › Clinic permissions › Sheet.
        navigate(page, 'people')
        page.locator('#main tr', has_text='Staff 02(test)').locator('.bulk-check').check()
        page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Show their…').click()
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Clinic permissions')).click()
        main = page.locator('#main')
        toolbar(main, 'View settings').get_by_role('radio', name='Sheet', exact=True).click()
        page.keyboard.press('Escape')
        edit_until(page, main, main.get_by_role('grid', name='Clinic permissions sheet'), 'Staff 02(test)', '31/12/2026')
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        page.reload()
        assert asked == ['beforeunload', 'beforeunload'], asked
    finally:
        context.close()


@pytest.mark.parametrize('direct', ['default', 'zero'])
def test_a_staged_switch_off_previews_with_the_armed_confirm(browser, live_server, h, direct):
    """Review (b4a8) #6: People › In use → No staged in the Sheet is destructive, as in Change…: Preview never applies
    at once (under the direct limit) and its Apply is the same-button armed confirm (also when the limit is 0)."""
    if direct == 'zero':
        no_light_path(h)
    taylor = h.seed['people'][2]
    active = lambda: next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == taylor)['active']
    context, page = sign_in(browser, live_server, 'admin')
    try:
        main, grid = open_sheet(page, 'People')
        target = cell(grid, 'In use', 'Staff 03(test)')
        target.click()
        page.keyboard.press('Enter')
        box = main.get_by_label('In use for Staff 03(test)')
        expect(box).to_be_focused()
        page.keyboard.press('Space')
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        stage_bar(main).get_by_role('button', name='Save 1 change').click()
        dock = page.locator('.dock-panel.bulk-panel')
        apply = dock.locator('.bulk-foot .button.primary')
        expect(apply).to_have_text(re.compile(r'^Apply ready rows \(1\)'))
        assert active() is True  # nothing applied at once
        apply.click()
        expect(apply).to_have_accessible_name(re.compile(r'^Confirm switch off 1\?'))
        assert active() is True
        apply.click()
        expect(page.locator('#toast-tray')).to_contain_text('1 person changed')
        assert active() is False
    finally:
        context.close()


def test_a_proposed_rule_line_edit_leaves_the_stage_without_marking_the_rule_changed(browser, live_server, h):
    """Review (b4a8) #7: a Note edit of a rule line staged in the Sheet and proposed ("waiting for approval") leaves the
    stage: no "not saved" mark, no unload question, nothing to propose twice; the rule in force is not marked changed."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line
    flag(h)
    rule = catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Sheet line')])
    context, page = sign_in(browser, live_server, 'coordinator')
    asked = []
    page.on('dialog', lambda d: (asked.append(d.type), d.accept()))
    try:
        main, grid = open_sheet(page, 'Rule lines')
        note = cell(grid, 'Note', 'Sheet line')
        note.click()
        page.keyboard.press('Enter')
        box = main.get_by_label(re.compile('^Note for '))
        box.fill('Changed in the sheet')
        box.press('Enter')
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        stage_bar(main).get_by_role('button', name='Save 1 change').click()
        dock = page.locator('.dock-panel.bulk-panel')
        dock.get_by_role('button', name=re.compile(r'^Propose 1')).click()
        expect(dock.get_by_role('heading', name='Proposed · waiting for approval')).to_be_visible()
        grid = main.get_by_role('grid', name='Rule lines sheet')
        expect(grid.locator('tbody tr', has_text='Sheet line')).to_have_count(1)  # read again: the rule in force
        expect(stage_bar(main)).to_be_hidden()
        expect(grid.locator('td.is-staged')).to_have_count(0)
        expect(grid.locator('td.is-changed')).to_have_count(0)  # changed only once approved
        expect(cell(grid, 'Note', 'Sheet line')).to_be_visible()
        policies = h.get('admin', '/api/catalogues')['policies']
        assert [p['definition']['rule']['lines'][0]['note'] for p in policies if p.get('status') == 'proposed'] == ['Changed in the sheet']
        assert next(p for p in policies if p['id'] == rule['id'])['status'] == 'active'
        dock.locator('.bulk-foot').get_by_role('button', name='Close').click()
        page.reload()
        assert asked == [], asked
    finally:
        context.close()


def test_the_bulk_editor_reads_its_kept_as_of_day_after_navigating_away_and_back(browser, live_server, h):
    """Review (b4a8) #1 (Sheet re-entry): the Bulk editor's As of day stays with its shared View; leaving and coming back
    reads the rows (and matches pasted names) on that day, not today."""
    context, page = sign_in(browser, live_server, 'admin')
    sent = []
    page.on('request', lambda r: sent.append(r.post_data_json) if r.method == 'POST' and '/api/bulk/rows' in r.url else None)
    try:
        main, grid = open_sheet(page, 'People')
        pop = toolbar(main, 'Filter')
        with page.expect_request(lambda r: '/api/bulk/rows' in r.url and '2000-01-01' in (r.post_data or '')):
            pop.locator('.dt-as-of input').fill('01/01/2000')
            page.keyboard.press('Tab')
        page.keyboard.press('Escape')
        expect(main.get_by_role('grid', name='People sheet')).to_be_visible()
        navigate(page, 'people')
        sent.clear()
        page.go_back()
        expect(main.get_by_role('grid', name='People sheet')).to_be_visible()
        assert sent and all(b['selection']['where']['as_of'] == '2000-01-01' for b in sent), sent
        expect(toolbar(main, 'Filter').locator('.dt-as-of input')).to_have_value(re.compile('1 Jan 2000'))
    finally:
        context.close()


def test_pasting_the_saved_value_over_a_staged_clear_unstages_it(browser, live_server, h):
    """Review (5fd5) #7: Staff 01's last day (31 Oct 2026) cleared in the Sheet, then Paste from Excel gives 31/10/2026 back:
    the paste is a change (from the staged clear) and, being the saved value, leaves nothing staged."""
    staff_numbers(h)
    walk_in(h)
    context, page = sign_in(browser, live_server, CRED)
    try:
        main, grid = open_sheet(page)
        until = cell(grid, 'Until (included)', 'Staff 01(test)', 'Walk-in North')
        until.click()
        page.keyboard.press('Delete')
        expect(until).to_have_class(re.compile(r'\bis-staged\b'))
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        main.get_by_role('button', name='Paste from Excel').click()
        dialog = page.get_by_role('dialog', name='Paste from Excel')
        dialog.get_by_label('Pasted').fill('Staff number\tClinic\tLast day\n10423\tWalk-in North\t31/10/2026\n')
        expect(dialog.locator('.matrix-summary')).to_contain_text(re.compile(r'^1 rows? matched'))
        dialog.get_by_role('button', name='Stage 1 change').click()
        expect(dialog).to_be_hidden()
        grid = main.get_by_role('grid', name='Clinic permissions sheet')
        expect(grid.locator('td.is-staged')).to_have_count(0)
        expect(stage_bar(main)).to_be_hidden()
        expect(cell(grid, 'Until (included)', 'Staff 01(test)', 'Walk-in North')).to_have_text('31 Oct 2026')
    finally:
        context.close()


def test_change_cells_waits_for_the_staged_changes_of_its_rows(browser, live_server, h):
    """Self-review (c): Change N cells… saves from the records as read; over rows with staged (unsaved) cells it says
    so instead of opening, and the staged cells stay."""
    walk_in(h)
    context, page = sign_in(browser, live_server, CRED)
    try:
        main, grid = open_sheet(page)
        main.get_by_role('searchbox', name='Search rows').fill('Walk-in')
        expect(grid.locator('tbody tr')).to_have_count(3)
        edit_until(page, main, grid, 'Staff 01(test)', '31/12/2026')
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        cell(grid, 'Until (included)', 'Staff 01(test)', 'Walk-in North').focus()
        page.keyboard.press('Shift+ArrowDown')
        main.get_by_role('button', name=re.compile(r'^Change 2 cells')).click()
        expect(page.locator('#toast-tray')).to_contain_text('preview or discard them first')
        expect(page.locator('.dock-panel.bulk-panel')).to_have_count(0)
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
    finally:
        context.close()


def test_a_sheet_rule_line_proposal_waits_for_unsaved_edits_of_its_rule(browser, live_server, h):
    """Pre-review #1: Maximum 5 → 6 left unsaved on the Rules page; a Note edit of the same rule's line staged in the
    Bulk editor does not propose (it would drop the Maximum 6): it says so, and the draft is still there."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line, set_numbers
    flag(h)
    catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Sheet line')])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_month')
        page.locator('#main tr[data-row]').first.locator('input.bulk-check').check()
        set_numbers(page, max=6)
        page.evaluate("location.hash='system/bulk'")
        # Leaving asks first (OD-10); the Rules page keeps its draft, so the choice is "Go to …", never Discard.
        leave = page.get_by_role('dialog', name='Leave this page?')
        expect(leave.get_by_role('button', name='Discard changes')).to_have_count(0)
        leave.get_by_role('button', name=re.compile(r'^Go to ')).click()
        expect(leave).to_have_count(0)
        main, grid = open_sheet(page, 'Rule lines')
        note = cell(grid, 'Note', 'Sheet line')
        note.click()
        page.keyboard.press('Enter')
        box = main.get_by_label(re.compile('^Note for '))
        box.fill('Changed in the sheet')
        box.press('Enter')
        stage_bar(main).get_by_role('button', name='Save 1 change').click()
        expect(page.locator('#toast-tray')).to_contain_text('unsaved edits on the Rules page')
        expect(page.locator('.dock-panel.bulk-panel')).to_have_count(0)
        assert not [p for p in h.get('admin', '/api/catalogues')['policies'] if p.get('status') == 'proposed']
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
    finally:
        context.close()


def test_rule_edits_of_one_account_never_hold_up_the_next(browser, live_server, h):
    """Pre-review 2 #3: A leaves an unsaved Maximum 6 on the Rules page and signs out; B, signing in in the same tab and
    going straight to the Bulk editor, previews a rule line Note edit (A's abandoned edits are gone with A's session)."""
    from .test_browser_bulk_changes import switch_account
    from .test_browser_rule_catalogue import catalogue_rule, flag, line, set_numbers
    flag(h)
    catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Sheet line')])
    context, page = sign_in(browser, live_server, 'coordinator')
    page.on('dialog', lambda d: d.accept())
    try:
        navigate(page, 'rules/duties_per_month')
        page.locator('#main tr[data-row]').first.locator('input.bulk-check').check()
        set_numbers(page, max=6)
        switch_account(page, 'admin')
        main, grid = open_sheet(page, 'Rule lines')
        note = cell(grid, 'Note', 'Sheet line')
        note.click()
        page.keyboard.press('Enter')
        box = main.get_by_label(re.compile('^Note for '))
        box.fill('Changed by the next account')
        box.press('Enter')
        stage_bar(main).get_by_role('button', name='Save 1 change').click()
        expect(page.locator('.dock-panel.bulk-panel').get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
        expect(page.locator('#toast-tray')).not_to_contain_text('unsaved edits on the Rules page')
    finally:
        context.close()



def paste_into(page, target, text):
    """Ctrl V of `text` (as Excel puts it on the clipboard) into the grid at `target`."""
    target.click()
    page.evaluate("""text => { const cell = document.querySelector('[role=grid] td[tabindex="0"]'), data = new DataTransfer();
        data.setData('text/plain', text); cell.dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true})); }""", text)


def test_a_positional_paste_keeps_blank_rows_and_commas_in_their_cells(browser, live_server, h):
    """Review (b0bf) #2: Excel's "111 / (blank) / 333" pasted down Title puts 333 on the third person (the blank row
    keeps its place). Bulk › People has no contact column (G3-32e: contacts only through the role-aware reader)."""
    context, page = sign_in(browser, live_server, 'admin')
    try:
        main, grid = open_sheet(page, 'People')
        expect(grid.locator('thead th').first).to_be_visible()
        headers = [t.strip() for t in grid.locator('thead th').all_inner_texts()]
        assert not set(headers) & contact_labels(), headers
        paste_into(page, cell(grid, 'Title', 'Staff 01(test)'), '111\n\n333\n')
        expect(cell(grid, 'Title', 'Staff 01(test)')).to_contain_text('111')
        expect(cell(grid, 'Title', 'Staff 02(test)')).not_to_contain_text('333')
        expect(cell(grid, 'Title', 'Staff 03(test)')).to_contain_text('333')
    finally:
        context.close()


def test_a_comma_pasted_into_one_cell_stays_in_that_cell(browser, live_server, h):
    """Review (b0bf) #2: "Main, Floor 2" pasted into one Directory section cell is one value there (only tabs split
    columns); the next column is left as it was."""
    context, page = sign_in(browser, live_server, 'admin')
    try:
        main, grid = open_sheet(page, 'People')
        headers = [t.strip() for t in grid.locator('thead th').all_inner_texts()]
        after = headers[headers.index('Directory section') + 1]
        before = cell(grid, after, 'Staff 01(test)').inner_text()
        paste_into(page, cell(grid, 'Directory section', 'Staff 01(test)'), 'Main, Floor 2')
        expect(cell(grid, 'Directory section', 'Staff 01(test)')).to_contain_text('Main, Floor 2')
        expect(cell(grid, after, 'Staff 01(test)')).to_have_text(before)
    finally:
        context.close()


def test_the_rules_sheet_proposes_a_rule_by_its_own_id(browser, live_server, h):
    """A rule's own Sheet field edit creates a governed proposal and leaves the active version unchanged."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line
    flag(h)
    rule = catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Everyone')])
    sent = []
    context, page = sign_in(browser, live_server, 'coordinator')
    page.on('request', lambda r: sent.append(r.post_data_json) if r.method == 'POST' and '/api/commands/preview' in r.url else None)
    try:
        main, grid = open_sheet(page, 'Rules')
        target = cell(grid, 'Enabled', 'Duties per month')
        target.click()
        page.keyboard.press('Enter')
        page.keyboard.press('Space')
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        stage_bar(main).get_by_role('button', name='Save 1 change').click()
        dock = page.locator('.dock-panel.bulk-panel')
        expect(dock.get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
        payload = sent[-1]['payload']
        assert 'selection' not in payload and [i['target_id'] for i in payload['items']] == [rule['id']], payload
        assert payload['items'][0]['changes'] == [{'field': 'on', 'how': 'set', 'value': False}], payload
        propose = dock.get_by_role('button', name='Propose 1', exact=True)
        expect(propose).to_be_enabled()
        propose.click()
        expect(dock.get_by_role('heading', name='Proposed · waiting for approval')).to_be_visible()
        policies = h.get('admin', '/api/catalogues')['policies']
        waiting = [p for p in policies if p['status'] == 'proposed'
                   and p['definition']['rule']['id'] == rule['definition']['rule']['id']]
        assert len(waiting) == 1
        assert waiting[0]['definition']['rule'] == {**rule['definition']['rule'], 'on': False}
        original = next(p for p in policies if p['id'] == rule['id'])
        assert original['status'] == 'active' and original['definition']['rule']['on'] is True
        expect(stage_bar(main)).to_be_hidden()
    finally:
        context.close()



def test_a_copied_cell_with_line_breaks_tabs_and_quotes_pastes_back_whole(browser, live_server, h):
    """Review (6cd6) #2: a Description holding "North / South", a tab and a "quote", copied from its cell and pasted into
    another service's Description, is staged there whole (one cell, nothing on the next row)."""
    services = h.get('admin', '/api/catalogues')['services']
    text = 'North\nSouth\tand "more"'
    first = services[0]
    h.configure('services', {**{k: first[k] for k in ('name', 'starts_on') if k in first}, 'description': text}, first['id'], first['revision'])
    context, page = sign_in(browser, live_server, 'admin')
    try:
        main, grid = open_sheet(page, 'Services')
        source = cell(grid, 'Description', first['name'])
        source.click()
        copied = page.evaluate("""() => { const cell = document.querySelector('[role=grid] td[tabindex="0"]'), data = new DataTransfer();
            cell.dispatchEvent(new ClipboardEvent('copy', {clipboardData: data, bubbles: true, cancelable: true})); return data.getData('text/plain'); }""")
        other = services[1]['name']
        paste_into(page, cell(grid, 'Description', other), copied)
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        target = cell(grid, 'Description', other)
        expect(target).to_have_class(re.compile(r'\bis-staged\b'))
        staged = target.evaluate("td => [...td.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('')")
        assert staged == text, (staged, copied)
    finally:
        context.close()



def test_change_cells_in_the_rules_sheet_sends_the_rules_as_rules(browser, live_server, h):
    """Review (e51a) #4: two rules' Enabled cells selected in the Rules sheet, Change N cells…: Preview sends the two
    rules as items of their own ids (never a selection of rule lines built from rule ids)."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line
    flag(h)
    rules = [catalogue_rule(h, entry, [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Everyone')])
             for entry in ('duties_per_month', 'duties_per_week')]
    sent = []
    context, page = sign_in(browser, live_server, 'coordinator')
    page.on('request', lambda r: sent.append(r.post_data_json) if r.method == 'POST' and '/api/commands/preview' in r.url else None)
    try:
        main, grid = open_sheet(page, 'Rules')
        first = cell(grid, 'Enabled', 'Duties per month')
        second = cell(grid, 'Enabled', 'Duties per week')
        first.click()
        second.click(modifiers=['Shift'])
        main.get_by_role('button', name=re.compile(r'^Change \d+ cells')).click()  # the range may hold another rule between them
        dock = page.locator('.dock-panel.bulk-panel')
        row = dock.locator('.bulk-changes .cond-row').last
        row.locator('.bulk-value select').select_option(label='No')  # Enabled: the boolean's own choice
        dock.get_by_role('button', name='Preview →').click()
        page.wait_for_function("() => !document.documentElement.hasAttribute('data-bulk-busy')")
        payload = sent[-1]['payload']
        assert 'selection' not in payload, payload
        assert {r['id'] for r in rules} <= {i['target_id'] for i in payload['items']}, payload
        assert payload['changes'] == [{'field': 'on', 'how': 'set', 'value': False}], payload
        propose = dock.locator('.bulk-foot .button.primary')
        expect(propose).to_have_text(re.compile(r'^Propose'))
        propose.click()
        expect(dock.get_by_role('heading', name='Proposed · waiting for approval')).to_be_visible()
        proposed = [p for p in h.get('admin', '/api/catalogues')['policies'] if p.get('status') == 'proposed']
        assert len(proposed) >= 2 and all(p['definition']['rule'].get('on') is False for p in proposed), proposed
    finally:
        context.close()


def test_tags_copied_and_pasted_in_the_sheet_keep_their_names_and_memberships(browser, live_server, h):
    """Review (67a5) #1: Staff 01's tags #student and "North, East", copied from the People sheet and pasted onto Staff 02,
    become exactly those two tags (one of them holding a comma); "#student, brandnew" pasted onto Staff 03 keeps student
    (not a new tag "#student") and adds brandnew."""
    for name in ('student', 'North, East'):
        h.configure('tags', {'name': name, 'labels': ['people']})
    people = {p['id']: p for p in h.get('admin', '/api/catalogues')['people']}
    alex, jordan, taylor = h.seed['people']
    a = people[alex]
    h.command('admin', 'catalogue.update', {'catalogue': 'people', 'value': {**{k: a[k] for k in ('display_name', 'active')}, 'tags': ['student', 'North, East']},
              'reason': 'Test setup'}, alex, a['revision'])
    no_light_path(h)
    context, page = sign_in(browser, live_server, 'admin')
    try:
        main, grid = open_sheet(page, 'People')
        source = cell(grid, 'Tags', 'Staff 01(test)')
        source.click()
        copied = page.evaluate("""() => { const cell = document.querySelector('[role=grid] td[tabindex="0"]'), data = new DataTransfer();
            cell.dispatchEvent(new ClipboardEvent('copy', {clipboardData: data, bubbles: true, cancelable: true})); return data.getData('text/plain'); }""")
        paste_into(page, cell(grid, 'Tags', 'Staff 02(test)'), copied)
        paste_into(page, cell(grid, 'Tags', 'Staff 03(test)'), '#student, brandnew')
        expect(stage_bar(main)).to_contain_text('2 changes in 2 rows')
        stage_bar(main).get_by_role('button', name='Save 2 changes').click()
        page.locator('.dock-panel.bulk-panel').get_by_role('button', name=re.compile(r'^Apply ready rows \(2\)')).click()
        expect(page.locator('#toast-tray')).to_contain_text('2 people changed')
        now = {p['id']: p for p in h.get('admin', '/api/catalogues')['people']}
        assert sorted(now[jordan]['tags']) == ['North, East', 'student'], now[jordan]['tags']
        assert sorted(now[taylor]['tags']) == ['brandnew', 'student'], now[taylor]['tags']
    finally:
        context.close()


def test_multiline_text_is_kept_exactly_through_edit_escape_and_accept(browser, live_server, h):
    """Pre-review (full 2) #1: a Description "North / South" opened with Enter and accepted unchanged stages nothing (the
    editor is the shared text area, its line kept); F2 then Escape stages nothing; Alt+Enter adds a line, which is staged
    exactly. A rule line's Note (a long text too) behaves the same."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line
    services = h.get('admin', '/api/catalogues')['services']
    first = services[0]
    text = 'North\nSouth'
    h.configure('services', {**{k: first[k] for k in ('name', 'starts_on') if k in first}, 'description': text}, first['id'], first['revision'])
    flag(h)
    catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Line one\nLine two')])
    context, page = sign_in(browser, live_server, 'admin')
    try:
        main, grid = open_sheet(page, 'Services')
        target = cell(grid, 'Description', first['name'])
        target.click()
        page.keyboard.press('Enter')
        box = main.get_by_label(f"Description for {first['name']}")
        expect(box).to_be_focused()
        assert box.evaluate('e => e.tagName') == 'TEXTAREA' and box.input_value() == text
        page.keyboard.press('Enter')                      # accepted unchanged
        expect(stage_bar(main)).to_be_hidden()
        target.click()
        page.keyboard.press('F2')
        page.keyboard.press('End')
        page.keyboard.type(' and more')
        page.keyboard.press('Escape')                     # cancelled
        expect(stage_bar(main)).to_be_hidden()
        target.click()
        page.keyboard.press('Enter')
        page.keyboard.press('Control+End')
        page.keyboard.press('Alt+Enter')
        page.keyboard.type('East')
        page.keyboard.press('Enter')
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        staged = cell(grid, 'Description', first['name']).evaluate("td => [...td.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('')")
        assert staged == text + '\nEast', staged
        stage_bar(main).get_by_role('button', name=re.compile('^Discard')).click()
        stage_bar(main).get_by_role('button', name=re.compile('^Discard|^Confirm')).click()
        expect(stage_bar(main)).to_be_hidden()
        main, grid = open_sheet(page, 'Rule lines')
        note = cell(grid, 'Note', 'Line one')
        note.click()
        page.keyboard.press('Enter')
        expect(main.get_by_label(re.compile('^Note for '))).to_have_value('Line one\nLine two')
        page.keyboard.press('Enter')
        expect(stage_bar(main)).to_be_hidden()
    finally:
        context.close()



@pytest.mark.parametrize('first', ['#student', 'student'])
def test_a_tag_and_a_tag_named_with_a_hash_stay_apart_through_copy_and_paste(browser, live_server, h, first):
    """Review (4d5b) #1: tags "#student" and "student" both exist (either created first); Staff 01's one tag, copied from the
    People sheet and pasted onto Staff 02, is that same tag on Staff 02 (its label #name is matched first)."""
    names = [first, 'student' if first == '#student' else '#student']
    for name in names:
        h.configure('tags', {'name': name, 'labels': ['people']})
    people = {p['id']: p for p in h.get('admin', '/api/catalogues')['people']}
    alex, jordan, _ = h.seed['people']
    a = people[alex]
    for tag in names:
        h.command('admin', 'catalogue.update', {'catalogue': 'people', 'value': {**{k: a[k] for k in ('display_name', 'active')}, 'tags': [tag]},
                  'reason': 'Test setup'}, alex, a['revision'])
        a = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == alex)
        no_light_path(h)
        context, page = sign_in(browser, live_server, 'admin')
        try:
            main, grid = open_sheet(page, 'People')
            cell(grid, 'Tags', 'Staff 01(test)').click()
            copied = page.evaluate("""() => { const cell = document.querySelector('[role=grid] td[tabindex="0"]'), data = new DataTransfer();
                cell.dispatchEvent(new ClipboardEvent('copy', {clipboardData: data, bubbles: true, cancelable: true})); return data.getData('text/plain'); }""")
            paste_into(page, cell(grid, 'Tags', 'Staff 02(test)'), copied)
            stage_bar(main).get_by_role('button', name='Save 1 change').click()
            page.locator('.dock-panel.bulk-panel').get_by_role('button', name=re.compile(r'^Apply ready rows \(1\)')).click()
            expect(page.locator('#toast-tray')).to_contain_text('1 person changed')
            now = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == jordan)
            assert now['tags'] == [tag], (tag, copied, now['tags'])
        finally:
            context.close()



@pytest.mark.parametrize('first', ['Cafe', 'Café'])
def test_a_pasted_clinic_name_picks_that_clinic_not_one_differing_only_by_accents(browser, live_server, h, first):
    """Review (1c7f) #1: clinics "Cafe" and "Café" both exist (either made first); pasting "Café" into a rotation's
    Clinic stages Café and pasting "Cafe" stages Cafe (an exact name wins; a folded match only when it is the only one)."""
    from .test_rotation_attachments import rotation_value
    names = [first, 'Café' if first == 'Cafe' else 'Cafe']
    for name in names:
        h.configure('services', {'name': name, 'starts_on': '2020-01-01'})
    h.configure('rotations', rotation_value(h), role='coordinator')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        main, grid = open_sheet(page, 'Rotations')
        target = grid.locator('tbody tr').first.get_by_role('gridcell').nth(
            [t.strip() for t in grid.locator('thead th').all_inner_texts()].index('Clinic'))
        for name in ('Café', 'Cafe'):
            paste_into(page, target, name)
            expect(target).to_have_class(re.compile(r'\bis-staged\b'))
            shown = target.evaluate("td => [...td.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('')")
            assert shown == name, (first, name, shown)
    finally:
        context.close()


def test_a_failed_name_lookup_never_stages_an_earlier_paste(browser, live_server, h):
    """Review (1c7f) #2: a paste is read (a matched and an ambiguous line, a choice offered); the text is replaced and
    its name lookup fails: the earlier lines and their choice are gone and nothing can be staged."""
    staff_numbers(h)
    service, _ = walk_in(h)
    permit(h, h.seed['people'][2], service, '2026-12-01', '2027-03-01')
    context, page = sign_in(browser, live_server, CRED)
    try:
        main, grid = open_sheet(page)
        main.get_by_role('button', name='Paste from Excel').click()
        dialog = page.get_by_role('dialog', name='Paste from Excel')
        dialog.get_by_label('Pasted', exact=True).fill('Staff number\tClinic\tLast day\n10423\tWalk-in North\t31/12/2026\n'
                                           '10977\tWalk-in North\t28/02/2027\n')
        lines = dialog.locator('.sheet-paste-lines')
        expect(lines.locator('tr').filter(has_text='10977')).to_contain_text('2 records')
        expect(dialog.get_by_role('button', name='Stage 1 change')).to_be_enabled()
        page.route('**/api/bulk/resolve', lambda route: route.fulfill(status=503, content_type='application/json',
                   body='{"error":{"code":"UNAVAILABLE","message":"Matching is unavailable","details":null}}'))
        dialog.get_by_label('Pasted', exact=True).fill('Staff number\tClinic\tLast day\n10431\tWalk-in North\t15/01/2027\n')
        expect(dialog.locator('.matrix-summary')).to_contain_text('Matching is unavailable')
        expect(dialog.locator('.sheet-paste-lines')).to_have_count(0)  # no earlier line, no earlier choice
        expect(dialog.locator('button[type=submit]')).to_be_disabled()
    finally:
        context.close()


@pytest.mark.parametrize('column', ['Clinic', 'Mentor'])
def test_a_pasted_name_two_records_share_picks_neither(browser, live_server, h, column):
    """Review (3ac1) #1: two clinics named "Twin clinic" (or two people named "Sam Twin", as mentors): pasting that name
    into a rotation's Clinic (or Mentor) stages nothing and says it was not read (never the first of them); a name only
    one record has still stages."""
    from .test_rotation_attachments import rotation_value
    if column == 'Clinic':
        for _ in range(2):
            h.configure('services', {'name': 'Twin clinic', 'starts_on': '2020-01-01'})
        h.configure('services', {'name': 'Single clinic', 'starts_on': '2020-01-01'})
        shared, single = 'Twin clinic', 'Single clinic'
    else:
        for code in ('ST1', 'ST2'):
            h.configure('people', {'display_name': 'Sam Twin', 'short_code': code})
        shared, single = 'Sam Twin', 'Staff 02(test)'
    h.configure('rotations', rotation_value(h), role='coordinator')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        main, grid = open_sheet(page, 'Rotations')
        target = grid.locator('tbody tr').first.get_by_role('gridcell').nth(
            [t.strip() for t in grid.locator('thead th').all_inner_texts()].index(column))
        paste_into(page, target, shared)
        expect(page.locator('#toast-tray')).to_contain_text('1 not recognised')
        expect(target).not_to_have_class(re.compile(r'\bis-staged\b'))
        expect(stage_bar(main)).to_be_hidden()
        paste_into(page, target, single)
        expect(target).to_have_class(re.compile(r'\bis-staged\b'))
        shown = target.evaluate("td => [...td.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('')")
        assert shown == single, shown
    finally:
        context.close()


# JSON punctuation or a record id in a cell: the Sheet showing raw data instead of words.
RAW = re.compile(r'[{}\[\]]|"\w+":|[0-9a-f]{8}-[0-9a-f]{4}-')


def test_every_cell_reads_as_words_never_json_or_ids(browser, live_server, h):
    """Release QA 2026-10-20: structured and coded values read as their own screens say them (rule type by name, rule
    lines as line words, weekdays by name, details and audiences in words, sessions by their people), and a structure
    is neither typed nor pasted."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line
    from .test_events import event_type
    flag(h)
    catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 5, 'level': 'mandatory'}}, weekdays=[0, 1, 2, 3])])
    event_type(h)
    workspace = h.shift(h.workspace(), people=[])['workspace']
    h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=h.seed['people'][:2])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        main, grid = open_sheet(page, 'Rules')
        expect(cell(grid, 'Rule type', 'Duties per month')).to_have_text('Duties per month')
        expect(cell(grid, 'Rule lines', 'Duties per month')).to_contain_text('1 line: Everyone · All clinics · Mon–Thu')
        expect(cell(grid, 'Settings', 'Duties per month')).to_have_text('—')
        for record_type in ('Rules', 'Rule lines', 'Duty types', 'Event types', 'Planner sessions'):
            main, grid = open_sheet(page, record_type)
            expect(grid.locator('tbody tr').first).to_be_visible()
            raw = [t for t in grid.locator('tbody td').all_inner_texts() if RAW.search(t)]
            assert not raw, (record_type, raw)
            if record_type == 'Rule lines':
                weekdays = cell(grid, 'Weekdays', 'Mon–Thu')
                expect(weekdays).to_have_text('Mon–Thu')
                expect(cell(grid, 'Who', 'Mon–Thu')).to_have_text('Everyone')
                expect(cell(grid, 'Where', 'Mon–Thu')).to_have_text('All clinics')
                # Who, Where, Weekdays and Numbers are changed in the rule's own editor: read-only, never pasted.
                expect(weekdays).to_have_attribute('aria-readonly', 'true')
                main.get_by_role('button', name='Paste from Excel').click()
                dialog = page.get_by_role('dialog', name='Paste from Excel')
                dialog.get_by_label('Pasted').fill('Note\tWeekdays\nEveryone\tMon\n')
                expect(dialog.locator('.sheet-paste-map select').first).to_be_attached()
                options = dialog.locator('.sheet-paste-map select').first.evaluate('s => [...s.options].map(o => o.label)')
                assert 'Note' in options and not {'Who', 'Where', 'Weekdays', 'Numbers'} & set(options), options
                dialog.get_by_label('Pasted').fill('')
                dialog.get_by_role('button', name='Cancel').click()
                expect(dialog).to_be_hidden()
            if record_type == 'Event types':
                expect(grid.locator('tbody td').filter(has_text=re.compile('^Everyone$')).first).to_be_visible()
            if record_type == 'Planner sessions':
                titles = grid.locator('tbody td:first-child').all_inner_texts()
                assert 'Unstaffed' in titles and {'Staff 01(test), Staff 02(test)', 'Staff 02(test), Staff 01(test)'} & set(titles), titles
    finally:
        context.close()


def test_text_typed_in_an_open_cell_is_staged_when_a_search_redraws_the_sheet(browser, live_server, h):
    """#467 SHOULD 8: a Description being typed is staged (not lost) when Search redraws the grid, and focus stays in Search."""
    first = h.get('admin', '/api/catalogues')['services'][0]
    context, page = sign_in(browser, live_server, 'admin')
    try:
        main, grid = open_sheet(page, 'Services')
        cell(grid, 'Description', first['name']).click()
        page.keyboard.press('Enter')
        main.get_by_label(f"Description for {first['name']}").fill('Typed before the search')
        search = main.get_by_role('searchbox', name='Search rows')
        search.fill(first['name'])
        expect(search).to_be_focused()
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
        expect(cell(main.get_by_role('grid', name='Services sheet'), 'Description', first['name'])).to_contain_text('Typed before the search')
    finally:
        context.close()


def test_an_input_method_enter_confirms_its_text_not_the_cell(browser, live_server, h):
    """#467 SHOULD 10: the Enter that confirms composed (IME) text neither keeps the cell nor moves; a plain Enter then does."""
    first = h.get('admin', '/api/catalogues')['services'][0]
    context, page = sign_in(browser, live_server, 'admin')
    try:
        main, grid = open_sheet(page, 'Services')
        cell(grid, 'Description', first['name']).click()
        page.keyboard.press('Enter')
        box = main.get_by_label(f"Description for {first['name']}")
        box.fill('日本語')
        box.evaluate("e => e.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', isComposing: true, bubbles: true, cancelable: true}))")
        expect(box).to_be_focused()
        expect(stage_bar(main)).to_be_hidden()
        box.press('Enter')
        expect(stage_bar(main)).to_contain_text('1 change in 1 row')
    finally:
        context.close()

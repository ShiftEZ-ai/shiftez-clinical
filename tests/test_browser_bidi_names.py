"""INN-043 browser proof: mixed-script names stay readable and raw outside render sinks."""

import csv
import io
import re

import pytest
from playwright.sync_api import expect

from .test_browser import (
    browser,
    choose,
    close_popover,
    condition,
    filter_line,
    live_server,
    navigate,
    open_config,
    pick_view,
    planner_tool,
    save_dialog,
    sign_in,
    toolbar,
)
from .test_browser_bulk_changes import caret
from .test_browser_qa_planner import settled

pytestmark = pytest.mark.browser

PERSON_A = '(test) د. سنبلة'
PERSON_B = 'خزامى النخلي (test)'
CLINIC = 'عيادة ب'
PLACE = 'Room 1'
TAG = 'فريق-1'
DATE_SET = 'عطلة 2'
MARKS = re.compile('[\u2066-\u2069]')


def setup_bidi_fixture(h):
    p1 = h.configure('people', {'display_name': PERSON_A, 'active': True})['record']
    p2 = h.configure('people', {'display_name': PERSON_B, 'active': True})['record']
    rank = h.configure('categories', {'name': 'طبيب 3', 'starts_on': '2020-01-01'})['record']
    for person in (p1, p2):
        h.command('coordinator', 'membership.create', {
            'person_id': person['id'], 'category_id': rank['id'], 'starts_on': '2020-01-01'})
    clinic = h.configure('services', {'name': CLINIC, 'starts_on': '2020-01-01'})['record']
    location_type = h.get('admin', '/api/catalogues')['location_types'][0]['id']
    place = h.configure('locations', {'name': PLACE, 'starts_on': '2020-01-01',
                                      'location_type_id': location_type, 'capacity': 3})['record']
    for person in (p1, p2):
        h.command('credentialer', 'eligibility.create', {
            'person_id': person['id'], 'service_id': clinic['id'], 'starts_on': '2020-01-01'})
    tag = h.configure('tags', {'name': TAG, 'labels': ['people'], 'colour': 'teal'})['record']
    h.command('admin', 'tag.members', {'add': [{'kind': 'people', 'id': p1['id']}, {'kind': 'people', 'id': p2['id']}],
                                        'reason': 'Synthetic bidi test'}, tag['id'], tag['revision'])
    workspace = h.workspace('Synthetic bidi rota')
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    workspace = h.command('coordinator', 'shift.create', {
        'workspace_id': workspace['id'], 'assignment_type_id': h.seed['type'], 'location_id': place['id'],
        'service_ids': [clinic['id']], 'start': start, 'end': end,
        'participants': [{'person_id': p1['id'], 'start': start, 'end': end},
                         {'person_id': p2['id'], 'start': start, 'end': end}],
        'attributes': {}, 'override_reason': 'Synthetic bidi browser fixture'}, revision=workspace['revision'])['workspace']
    workspace = h.lifecycle(workspace, 'validate')['workspace']
    h.command('admin', 'table_view.create', {
        'table_key': 'planner', 'name': 'Synthetic bidi label',
        'definition': {'planner': {'mode': 'week', 'range': 'week',
                                   'label': {'parts': ['full', 'level'], 'separator': 'dot', 'honorific': 'none'}}}})
    h.configure('date_sets', {'name': 'Synthetic source', 'entries': [
        {'name': 'Synthetic entry', 'repeat': 'yearly_gregorian', 'first_day': '2026-12-01', 'last_day': '2026-12-01'}]})
    return p1, p2, workspace


def assert_no_marks(page):
    assert page.evaluate("() => !/[\\u2066-\\u2069]/.test(document.body.textContent || '')")
    assert page.evaluate("() => !/[\\u2066-\\u2069]/.test(document.body.innerText || '')")


def test_mixed_script_names_keep_render_order_and_raw_values(browser, live_server, h, tmp_path):
    p1, p2, workspace = setup_bidi_fixture(h)
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 900})
    context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=live_server)
    requests = []
    page.on('request', lambda request: requests.append((request.url, request.post_data or '')) if '/api/' in request.url else None)
    try:
        navigate(page, 'people')
        main = page.locator('#main')
        first = main.locator('tbody tr[data-row]').filter(has_text=PERSON_A).first
        expect(first).to_be_visible()
        expect(first.locator('bdi').filter(has_text=PERSON_A)).to_have_count(1)
        expect(first.locator('.role-chip bdi').first).to_be_visible()
        expect(first.locator('.tag-chip bdi,.chip-text bdi').first).to_be_visible()
        assert_no_marks(page)

        search = page.get_by_role('searchbox', name='Search people', exact=True)
        search.fill('سنبلة')
        expect(first).to_be_visible()
        assert not MARKS.search(search.input_value())
        search.fill('')

        # The shared picker is exercised with a typed Arabic fragment; its input and chosen value stay raw.
        pop = toolbar(main, 'Filter')
        condition(pop, 'Tags', [TAG])
        picker = toolbar(main, 'Filter')
        line = filter_line(picker, 'Tags')
        assert line is not None
        line.locator('.pick-button').click()
        value = page.locator('.picker').get_by_role('combobox')
        value.fill('فريق')
        expect(page.locator('.picker').get_by_role('option', name=TAG, exact=True)).to_be_visible()
        assert not MARKS.search(value.input_value())
        page.locator('.picker').get_by_role('option', name=TAG, exact=True).check()
        page.keyboard.press('Escape')
        close_popover(page)

        # Filter by the synthetic tag and sort by the raw Person column, then persist the view.
        choose(main, 'Sort', 'Sort by', 'Person')
        visible_names = main.locator('tbody tr[data-row] td[data-col=name] strong').all_inner_texts()
        assert set(visible_names) == {PERSON_A, PERSON_B}
        assert visible_names == sorted(visible_names, key=str.casefold)
        toolbar(main, 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('Synthetic bidi view')
        dialog.get_by_role('button', name='Save view', exact=True).click()
        expect(dialog).to_have_count(0)
        saved = next(v for v in h.get('admin', '/api/table-views?table=people')['views'] if v['name'] == 'Synthetic bidi view')
        assert not MARKS.search(repr(saved))
        assert_no_marks(page)

        # A schedule label has a name and level separated by " · "; it renders as one bdi per part.
        navigate(page, f'schedule/list/2026-10-04?edit={workspace["id"]}')
        settled(page)
        pick_view(page, 'Synthetic bidi label')
        planner_tool(page, 'Export…')
        export = page.get_by_role('dialog', name='Print or export', exact=True)
        export.get_by_role('button', name='Cancel', exact=True).click()
        pill = page.locator('#schedule-view .pchip-name').filter(has_text=PERSON_A).first
        expect(pill).to_be_visible()
        assert pill.evaluate("n => n.tagName === 'BDI' && n.textContent === n.closest('.pchip').querySelector('.pchip-name').textContent")
        assert pill.locator(':scope > bdi').count() >= 2
        assert not MARKS.search(pill.get_attribute('aria-label') or '')
        assert not MARKS.search(pill.locator('..').get_attribute('data-tip') or '')
        pill_bounds = pill.evaluate("""n => {
          const [name, after] = [...n.children].filter(x => x.tagName === 'BDI');
          const a=name.getBoundingClientRect(), b=after.getBoundingClientRect();
          return {nameRight:a.right, afterLeft:b.left};
        }""")
        assert pill_bounds['afterLeft'] >= pill_bounds['nameRight'] - 1, pill_bounds

        # A plain shared chip (without the planner's rich card) exercises the dotted hover renderer.
        navigate(page, 'people')
        clinic_chip = page.locator('#main tbody tr[data-row]').filter(has_text=PERSON_A).first.locator('.role-chip').first
        clinic_chip.evaluate("n => n.setAttribute('data-tip', 'عيادة ب · 3')")
        clinic_chip.hover()
        expect(page.locator('[role=tooltip].help-tooltip')).to_be_visible()
        tip = page.locator('[role=tooltip].help-tooltip').last
        assert tip.locator('bdi').count() >= 2
        assert tip.inner_text() == 'عيادة ب · 3'
        assert_no_marks(page)

        # The Schedule export is the shared CSV surface for both List and People layouts.
        navigate(page, f'schedule/list/2026-10-04?edit={workspace["id"]}')
        settled(page)
        pick_view(page, 'Synthetic bidi label')
        planner_tool(page, 'Export…')
        export = page.get_by_role('dialog', name='Print or export', exact=True)
        export.get_by_role('radiogroup', name='Format', exact=True).get_by_role('radio', name='Data (CSV)', exact=True).click()
        with page.expect_download() as saved_download:
            export.get_by_role('button', name='Save CSV', exact=True).click()
        csv_path = tmp_path / 'schedule.csv'
        saved_download.value.save_as(str(csv_path))
        csv_bytes = csv_path.read_bytes()
        assert not MARKS.search(csv_bytes.decode('utf-8-sig'))
        assert PERSON_A in csv_bytes.decode('utf-8-sig') and PERSON_B in csv_bytes.decode('utf-8-sig')
        rows = list(csv.reader(io.StringIO(csv_bytes.decode('utf-8-sig'), newline='')))
        assert rows and any(PERSON_A in cell for row in rows for cell in row)

        # Copy not-found names is a real clipboard path carrying user-entered name text.
        navigate(page, 'people')
        caret(main)
        page.get_by_role('menu', name='Selection options').get_by_role('menuitem', name='Paste names or codes…').click()
        paste = page.get_by_role('dialog', name='Paste names or codes')
        missing = 'د. سنبلة جاهظ ذاخج'
        paste.get_by_label('Names or codes', exact=True).fill(missing)
        paste.get_by_role('button', name='Copy not found', exact=True).click()
        assert page.evaluate('() => navigator.clipboard.readText()') == missing
        assert not MARKS.search(page.evaluate('() => navigator.clipboard.readText()'))

        # Creating a date set through the UI exercises the isolated name toast and its adjacent number.
        open_config(page, 'Holidays & dates off')
        page.get_by_role('button', name='New date set', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Name', exact=True).fill(DATE_SET)
        entry = form.locator('.ds-entry').first
        entry.get_by_label('Entry name', exact=True).fill('Synthetic holiday')
        entry.get_by_label('First day', exact=True).fill('2026-12-02')
        entry.get_by_label('Last day', exact=True).fill('2026-12-02')
        form.get_by_label('Change reason', exact=True).fill('Synthetic bidi browser test')
        save_dialog(form)
        toast = page.locator('.toast').filter(has_text=f'{DATE_SET} created.').last
        expect(toast).to_be_visible()
        expect(toast.locator('bdi')).to_have_text(DATE_SET)
        assert not MARKS.search(toast.text_content() or '')
        assert_no_marks(page)
    finally:
        context.close()

    for url, body in requests:
        assert not MARKS.search(url), url
        assert not MARKS.search(body), body

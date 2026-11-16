"""Prepared maintenance: selected published print sheet, complete labels and retained print artifacts."""
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .test_browser import route_fetch, browser, live_server, sign_in, navigate, planner_tool, wait_held  # noqa: F401
from .test_browser_insights_system import publication_selector_versions, publication_selector_unstaffed, omit_old_publication_catalogue_rows

pytestmark = pytest.mark.browser


def print_selector_dialog(page):
    navigate(page, 'schedule/week/2026-10-04?edit=none')
    planner_tool(page, 'Print sheet…')
    return page.get_by_role('dialog', name='Print sheet', exact=True)


def assert_print_snapshot_choices(dialog, snapshot):
    person_id, person = next(iter(snapshot['people'].items()))
    service_id, service = next(iter(snapshot['services'].items()))
    picker = dialog.get_by_label('Print person', exact=True)
    expect(picker.locator('option:not([value=""])')).to_have_text([person['display_name']])
    assert picker.locator('option:not([value=""])').evaluate_all('nodes => nodes.map(n => n.value)') == [person_id]
    clinics = dialog.get_by_role('group', name='Clinics', exact=True)
    expect(clinics.get_by_role('checkbox')).to_have_count(1)
    expect(clinics.get_by_role('checkbox', name=service['name'], exact=True)).to_be_visible()
    expect(picker).to_have_value('')
    expect(clinics.get_by_role('checkbox')).not_to_be_checked()
    return picker, clinics, person_id, service_id


def open_selected_print(context, dialog, publication_id, person_name):
    with context.expect_page() as opened:
        dialog.get_by_role('button', name='Open print sheet', exact=True).click()
    printed = opened.value
    printed.wait_for_load_state('load')
    assert urlparse(printed.url).path == '/api/reports/' + publication_id
    expect(printed.get_by_role('heading', name=person_name, exact=True)).to_be_visible()
    expect(printed.locator('tbody tr')).to_have_count(1)
    return printed


@pytest.mark.parametrize('width,missing_current', [(1440, False), (390, True)])
def test_print_choices_follow_historical_publication_and_reset_people_and_clinics_on_switch(browser, live_server, h, width, missing_current, tmp_path):
    issued = publication_selector_versions(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    context.add_init_script('window.print = () => {};')
    if missing_current:
        omit_old_publication_catalogue_rows(page, issued[0])
    try:
        dialog = print_selector_dialog(page)
        for index, value in enumerate(issued):
            pub, snapshot = value['publication'], value['snapshot']
            dialog.get_by_label('Published rota', exact=True).select_option(pub['id'])
            picker, clinics, person_id, service_id = assert_print_snapshot_choices(dialog, snapshot)
            picker.select_option(person_id)
            clinics.get_by_role('checkbox').check()
            dialog.get_by_label('Print date', exact=True).fill(pub['starts_on'])
            if index == 0:
                artifact = Path(os.getenv('PW_COVERAGE_ARTIFACT_DIR', str(tmp_path))) / f'print-dialog-{width}'
                artifact.mkdir(parents=True, exist_ok=True)
                for scheme in ('light', 'dark'):
                    page.emulate_media(color_scheme=scheme)
                    page.screenshot(path=str(artifact / f'{scheme}.png'), full_page=True, animations='disabled')
                page.emulate_media(color_scheme='light')
            printed = open_selected_print(context, dialog, pub['id'], snapshot['people'][person_id]['display_name'])
            try:
                query = parse_qs(urlparse(printed.url).query)
                assert query['person_id'] == [person_id] and query['service_ids'] == [service_id]
                expect(printed.locator('tbody tr')).to_contain_text(snapshot['services'][service_id]['name'])
            finally:
                printed.close()
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_print_switch_waits_for_selected_snapshot_and_ignores_a_late_old_response(browser, live_server, h, width):
    issued = publication_selector_versions(h)
    first, second = issued
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    context.add_init_script('window.print = () => {};')
    held = []
    path = '**/api/publications/' + second['publication']['id']
    page.route(path, lambda route: held.append(route))
    try:
        dialog = print_selector_dialog(page)
        opener = dialog.get_by_role('button', name='Open print sheet', exact=True)
        expect(opener).to_be_enabled()
        picker, clinics, person_id, _ = assert_print_snapshot_choices(dialog, first['snapshot'])
        picker.select_option(person_id)
        clinics.get_by_role('checkbox').check()
        # The read is sent asynchronously after the choice: wait for it before checking the interception held it.
        with page.expect_request(lambda r: r.url.endswith('/api/publications/' + second['publication']['id'])):
            dialog.get_by_label('Published rota', exact=True).select_option(second['publication']['id'])
        expect(opener).to_be_disabled()
        assert held, 'Selected publication read must reach the deterministic interception checkpoint'
        dialog.get_by_label('Published rota', exact=True).select_option(first['publication']['id'])
        expect(opener).to_be_enabled()
        picker, clinics, person_id, _ = assert_print_snapshot_choices(dialog, first['snapshot'])
        picker.select_option(person_id)
        clinics.get_by_role('checkbox').check()
        with page.expect_response(lambda response: urlparse(response.url).path == '/api/publications/' + second['publication']['id']):
            held.pop().fulfill(json=second)
        page.evaluate('() => new Promise(requestAnimationFrame)')
        expect(dialog.get_by_label('Published rota', exact=True)).to_have_value(first['publication']['id'])
        expect(picker).to_have_value(person_id)
        expect(clinics.get_by_role('checkbox')).to_be_checked()
        printed = open_selected_print(context, dialog, first['publication']['id'], first['snapshot']['people'][person_id]['display_name'])
        printed.close()
    finally:
        for route in held:
            route.abort()
        context.close()


@pytest.mark.parametrize('role', ['coordinator', 'staff'])
@pytest.mark.parametrize('width', [1440, 390])
def test_schedule_tools_stays_open_when_published_coverage_arrives(browser, live_server, h, role, width):
    publication = h.publish(h.shift(h.workspace('Coverage refresh print menu'), people=[h.seed['people'][0]])['workspace'])['publication_id']
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 900})
    held = []
    path = '/api/publications/' + publication + '/coverage'
    page.route('**' + path, lambda route: held.append((route, route_fetch(route))))
    try:
        navigate(page, 'schedule/week/2026-10-04?edit=none')
        wait_held(page, held)
        tools = page.get_by_role('button', name='Schedule tools', exact=True)
        tools.click()
        menu = page.get_by_role('menu', name='Schedule tools', exact=True)
        expect(menu.get_by_role('menuitem', name='Print or export…', exact=True)).to_be_visible()
        expect(tools).to_have_attribute('aria-expanded', 'true')
        anchor = tools.element_handle()
        route, response = held.pop()
        assert response.ok
        with page.expect_request_finished(lambda request: request == route.request, timeout=10000):
            route.fulfill(response=response)
        # Wait for the coverage redraw itself, so the assertions cannot pass on the old open menu before it lands.
        page.wait_for_function('anchor => !anchor.isConnected', arg=anchor)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        expect(tools).to_have_attribute('aria-expanded', 'true')
        expect(menu).to_be_visible()
        menu.get_by_role('menuitem', name='Print or export…', exact=True).click()
        page.get_by_role('dialog', name='Print or export').get_by_role('button', name='Published sheet…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Print sheet', exact=True)
        expect(dialog).to_be_visible()
        expect(dialog.get_by_label('Published rota', exact=True)).to_have_value(publication)
        expect(dialog.get_by_role('button', name='Open print sheet', exact=True)).to_be_enabled()
        expect(menu).to_have_count(0)
        expect(page.locator('#main [role=alert]')).to_have_count(0)
    finally:
        for route, _ in held:
            route.abort()
        context.close()


def test_print_selected_snapshot_failure_disables_open_and_can_retry(browser, live_server, h):
    first, second = publication_selector_versions(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    context.add_init_script('window.print = () => {};')
    failed = []
    def fail_once(route):
        if not failed:
            failed.append(True)
            route.fulfill(status=503, content_type='application/json', body=json.dumps({'error': {
                'code': 'TEST_PUBLICATION_UNAVAILABLE', 'message': 'Selected publication temporarily unavailable'}}))
        else:
            route.continue_()
    page.route('**/api/publications/' + second['publication']['id'], fail_once)
    try:
        dialog = print_selector_dialog(page)
        expect(dialog.get_by_role('button', name='Open print sheet', exact=True)).to_be_enabled()
        dialog.get_by_label('Published rota', exact=True).select_option(second['publication']['id'])
        expect(dialog).to_contain_text('Couldn’t load the print options.')
        expect(dialog.get_by_role('button', name='Open print sheet', exact=True)).to_be_disabled()
        dialog.get_by_role('button', name='Retry', exact=True).click()
        expect(dialog.get_by_role('button', name='Open print sheet', exact=True)).to_be_enabled()
        picker, clinics, person_id, _ = assert_print_snapshot_choices(dialog, second['snapshot'])
        picker.select_option(person_id)
        clinics.get_by_role('checkbox').check()
        dialog.get_by_label('Print date', exact=True).fill(second['publication']['starts_on'])
        printed = open_selected_print(context, dialog, second['publication']['id'], second['snapshot']['people'][person_id]['display_name'])
        printed.close()
    finally:
        context.close()


def test_unstaffed_publication_prints_all_without_a_person_filter(browser, live_server, h):
    publication = publication_selector_unstaffed(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    context.add_init_script('window.print = () => {};')
    try:
        dialog = print_selector_dialog(page)
        expect(dialog.get_by_label('Print person', exact=True)).to_have_count(0)
        expect(dialog.get_by_role('button', name='Open print sheet', exact=True)).to_be_enabled()
        with context.expect_page() as opened:
            dialog.get_by_role('button', name='Open print sheet', exact=True).click()
        printed = opened.value
        try:
            printed.wait_for_load_state('load')
            assert urlparse(printed.url).path == '/api/reports/' + publication
            assert 'person_id' not in parse_qs(urlparse(printed.url).query)
            expect(printed.locator('tbody tr')).to_have_count(1)
            expect(printed.locator('tbody tr')).to_contain_text('Unstaffed export group')
        finally:
            printed.close()
    finally:
        context.close()


def test_staff_paired_publication_does_not_offer_another_person_export_or_print_filter(browser, live_server, h):
    mine, supervisor = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Paired export scope', 'starts_on': '2020-01-01', 'definition': {'supervision': {
        'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
        'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    publication = h.publish(h.shift(h.workspace(), typ=typ['id'], people=[
        {'person_id': mine, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': supervisor},
        {'person_id': supervisor, 'start': start, 'end': end, 'participation': 'supervisor', 'team_lead': True}])['workspace'])['publication_id']
    snapshot = h.get('staff', '/api/publications/' + publication)['snapshot']
    assert set(snapshot['people']) == {mine, supervisor}
    context, page = sign_in(browser, live_server, 'staff', viewport={'width': 390, 'height': 900})
    context.add_init_script('window.print = () => {};')
    try:
        navigate(page, 'insights/exports')
        expect(page.locator('.export-row').filter(has_text='Personal calendar').get_by_label('Person', exact=True)).to_have_count(0)
        with page.expect_download() as pending:
            page.get_by_role('button', name='Download Personal calendar', exact=True).click()
        calendar = Path(pending.value.path()).read_text(encoding='utf-8-sig').replace('\n ', '')
        uid = next(line for line in calendar.splitlines() if line.startswith('UID:'))
        assert mine in uid and supervisor not in uid
        assert calendar.count('BEGIN:VEVENT') == 1
        dialog = print_selector_dialog(page)
        expect(dialog.get_by_label('Print person', exact=True)).to_have_count(0)
        expect(dialog.get_by_role('group', name='Clinics', exact=True)).to_have_count(0)
        printed = open_selected_print(context, dialog, publication, snapshot['people'][mine]['display_name'])
        printed.close()
    finally:
        context.close()


def expect_atomic_times(page):
    times = page.locator('.print-time')
    expect(times).to_have_text(['04 Oct 08:00–04 Oct 12:00'] * 2)
    expect(page.locator('.print-period')).to_have_text('4 Oct 2026 – 10 Oct 2026')
    assert page.locator('.print-time,.print-period').evaluate_all('''nodes => nodes.every(node => {
        const range = document.createRange(); range.selectNodeContents(node);
        return range.getClientRects().length === 1 && node.scrollWidth <= node.clientWidth + 1;
    })'''), 'Period, duty and assigned-time ranges must each occupy one unbroken line'


@pytest.mark.parametrize('width', [1440, 390])
def test_selected_published_print_sheet_retains_long_labels_and_hides_controls(browser, live_server, h, tmp_path, width):
    labels = {
        'person': 'Alex Morgan — Senior Family Medicine Consultant for Integrated Community Follow-up and Continuity Care',
        'clinic': 'Continuity clinic — Comprehensive Follow-up and Coordinated Long-Term Condition Review Service',
        'location': 'Main outpatient building — Family Medicine Centre, extended consultation room at the east corridor',
        'duty': 'Clinical review session — Comprehensive Follow-up, Medication Review and Coordinated Care Planning',
    }
    person, other_person = h.seed['people'][:2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, person).display_name = labels['person']
        db.get(m.Service, h.seed['services'][0]).name = labels['clinic']
        db.get(m.Location, h.seed['locations'][0]).name = labels['location']
        db.get(m.AssignmentType, h.seed['type']).name = labels['duty']
    # One included duty; independently excluded by person, clinic and the selected week.
    w = h.workspace('Selected print acceptance', start='2026-10-04', end='2026-10-14')
    for day, who, service in [(4, person, 0), (5, other_person, 0), (6, person, 1), (12, person, 0)]:
        w = h.shift(w, f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00',
                    people=[who], service=service)['workspace']
    publication = h.publish(w)['publication_id']
    issued = h.get('coordinator', '/api/publications/' + publication)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    # Print commands can never open a system printer during this acceptance test.
    context.add_init_script('window.print = () => { window.__printCalled = true; };')
    printed = None
    try:
        navigate(page, 'schedule/week/2026-10-04?edit=none')
        planner_tool(page, 'Print sheet…')
        dialog = page.get_by_role('dialog', name='Print sheet', exact=True)
        dialog.get_by_label('Published rota', exact=True).select_option(publication)
        dialog.get_by_label('Print period', exact=True).select_option('month')
        dialog.get_by_label('Print period', exact=True).select_option('week')
        dialog.get_by_label('Print person', exact=True).select_option(person)
        dialog.get_by_role('group', name='Clinics', exact=True).get_by_label(labels['clinic'], exact=True).check()
        with context.expect_page() as opened:
            dialog.get_by_role('button', name='Open print sheet', exact=True).click()
        printed = opened.value
        printed.wait_for_load_state('load')
        query = parse_qs(urlparse(printed.url).query)
        assert urlparse(printed.url).path == '/api/reports/' + publication
        assert query == {'format': ['print'], 'layout': ['week'], 'on': ['2026-10-04'],
                         'person_id': [person], 'service_ids': [h.seed['services'][0]]}
        expect(printed.get_by_role('heading', name=labels['person'], exact=True)).to_be_visible()
        expect(printed.locator('body')).to_contain_text('4 Oct 2026 – 10 Oct 2026')
        expect(printed.locator('body')).to_contain_text('times in Asia/Riyadh')
        expect(printed.locator('tbody tr')).to_have_count(1)
        row = printed.locator('tbody tr')
        expect(row.get_by_role('cell', name=labels['duty'], exact=True)).to_be_visible()
        expect(row.get_by_role('cell', name=labels['clinic'], exact=True)).to_be_visible()
        expect(row.get_by_role('cell', name=labels['location'], exact=True)).to_be_visible()
        expect(row).to_contain_text(labels['person'])
        expect(row).to_contain_text('04 Oct 08:00–04 Oct 12:00')
        expect(printed.get_by_role('navigation')).to_be_visible()
        artifact = Path(os.getenv('PW_COVERAGE_ARTIFACT_DIR', str(tmp_path))) / f'published-print-{width}'
        artifact.mkdir(parents=True, exist_ok=True)
        scroll = printed.get_by_role('region', name='Published duties', exact=True)
        expect_atomic_times(printed)
        assert printed.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'The document must fit the phone; only the table scrolls'
        printed.screenshot(path=str(artifact / 'screen-preview.png'), full_page=True, animations='disabled')
        if width == 390:
            assert scroll.evaluate('node => node.scrollWidth > node.clientWidth'), 'Readable phone columns need a contained scroll area'
            scroll.focus()
            scroll.press('ArrowRight')
            printed.wait_for_function('node => node.scrollLeft > 0', arg=scroll.element_handle())
            scroll.evaluate('node => node.scrollLeft = node.scrollWidth')
            assert row.locator('td').last.evaluate('''node => {
                const table = node.closest(".print-table").getBoundingClientRect();
                const cell = node.getBoundingClientRect();
                return cell.right <= table.right + 1 && cell.left < table.right;
            }''')
            printed.screenshot(path=str(artifact / 'screen-preview-scrolled.png'), full_page=True, animations='disabled')
        # Print-media screenshots use the A4 content width; a 390px screenshot is not the exported paper layout.
        printed.set_viewport_size({'width': 1032, 'height': 726})
        printed.emulate_media(media='print')
        expect(printed.get_by_role('navigation')).to_be_hidden()
        expect(scroll).to_have_css('outline-style', 'none')
        expect(printed.locator('button,input,select,textarea,[contenteditable=true]')).to_have_count(0)
        expect_atomic_times(printed)
        assert printed.locator('th.duty-time').evaluate('''node => {
            const range = document.createRange(); range.selectNodeContents(node);
            return range.getClientRects().length === 1;
        }'''), 'The Duty time heading must fit its reserved column'
        assert printed.locator('h1,td').evaluate_all('''nodes => nodes.every(node => {
            const css = getComputedStyle(node);
            return css.textOverflow !== 'ellipsis' && !['hidden','clip'].includes(css.overflowX)
                && node.scrollWidth <= node.clientWidth + 1;
        })'''), 'Printed labels must wrap without clipping'
        printed.screenshot(path=str(artifact / 'print-media.png'), full_page=True, animations='disabled')
        (artifact / 'print-media.html').write_text(printed.content(), encoding='utf8')
        (artifact / 'receipt.json').write_text(json.dumps({'publication_id': publication, 'query': query,
            'screen_viewport_width': width, 'print_media_viewport_width': 1032, 'labels': labels, 'rows': row.all_inner_texts(),
            'visual_review': 'pending artifact inspection'}, indent=2), encoding='utf8')
        pdf = printed.pdf(path=str(artifact / 'published-print.pdf'), prefer_css_page_size=True, print_background=True)
        assert pdf.startswith(b'%PDF-') and len(pdf) > 1000
        # Printing changes nothing: the read is the same but for its live operational summary's read time (as_of);
        # that summary's fingerprint still binds its content.
        unread = lambda r: {**r, 'operational': {k: v for k, v in r['operational'].items() if k != 'as_of'}}
        assert unread(h.get('coordinator', '/api/publications/' + publication)) == unread(issued)
    finally:
        if printed:
            printed.close()
        context.close()

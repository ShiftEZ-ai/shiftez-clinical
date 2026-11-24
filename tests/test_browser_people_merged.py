"""People = People & eligibility + Directory: one list whose columns and actions follow the role, a side panel with tabs
per person, role-filtered contacts (staff read /api/directory), and the journeys that run through it."""
import re
import time
import uuid
from datetime import date

import pytest
from playwright.sync_api import expect
from sqlalchemy import insert

from apps.api import models as m
from apps.api.auth import effective_roles
from apps.api.db import transaction
from .governance_support import identity
from .test_browser import TEST_PASSWORD, browser, live_server, sign_in, navigate, filter_by, save_dialog, pick, wait_held, wait_until  # noqa: F401
from .test_browser_access_page import sign_out
from .test_browser_catalogue_hydration import held_app
from .test_browser_w3_setup_today import no_page_overflow

pytestmark = pytest.mark.browser
PHONE = {'width': 390, 'height': 844}
# Columns each role sees by default (desktop), in order; the first is always Person.
COLUMNS = {
    'staff': ['Person', 'Title', 'Section', 'Hospital phone', 'Extension'],
    'admin': ['Person', 'Current rank', 'Clinical eligibility', 'Skills', 'Tags', 'Hospital phone', 'Extension'],
    'coordinator': ['Person', 'Current rank', 'Clinical eligibility', 'Skills', 'Tags', 'Hospital phone', 'Extension'],
    'credentialer': ['Person', 'Clinical eligibility', 'Skills', 'Hospital phone', 'Extension'],
    'approver': ['Person', 'Current rank', 'Clinical eligibility', 'Tags', 'Hospital phone', 'Extension'],
    'auditor': ['Person', 'Current rank', 'Clinical eligibility', 'Tags', 'Hospital phone', 'Extension'],
    'programme_lead': ['Person', 'Current rank', 'Clinical eligibility', 'Tags', 'Hospital phone', 'Extension'],
}
# Side panel tabs per role (accessible names; Workload, Requests and Quotas are their short labels).
SHORT = {'Workload & fairness': 'Workload', 'Leave & requests': 'Requests', 'Quotas & offers': 'Quotas'}


def expect_tabs(tabs, names):
    """Tabs by accessible name; three show a short label (Workload, Requests, Quotas) with the full name as their name."""
    expect(tabs).to_have_text([SHORT.get(n, n) for n in names])
    for i, name in enumerate(names):
        expect(tabs.nth(i)).to_have_accessible_name(name)


# Side panel tabs per role.
TABS = {
    'staff': [],
    'admin': ['Overview', 'Schedule', 'Workload & fairness', 'Leave & requests', 'Rules', 'Quotas & offers', 'Access'],
    'coordinator': ['Overview', 'Schedule', 'Workload & fairness', 'Leave & requests', 'Rules', 'Quotas & offers'],
    'credentialer': ['Overview', 'Schedule'],
    'approver': ['Overview', 'Schedule', 'Workload & fairness', 'Leave & requests', 'Rules', 'Quotas & offers'],
    'auditor': ['Overview', 'Schedule', 'Workload & fairness', 'Leave & requests', 'Rules', 'Quotas & offers', 'History'],
    'programme_lead': ['Overview', 'Schedule', 'Workload & fairness', 'Leave & requests', 'Rules', 'Quotas & offers'],
}


def contact(h):
    return h.configure('people', {'display_name': 'Fictional Colleague Contact', 'title': 'Registrar', 'directory_section': 'Urgent cover',
                                  'work_phone': '+966 11 000 1234', 'work_extension': '4321 / 8765', 'staff_number': 'PRIVATE-77'})['record']


def row(page, name):
    return page.locator('#main .dt-body tbody tr, #main .dt-body .dt-card').filter(has_text=name).first


def visible_contact_fields(h, role, person):
    me = h.get(role, '/api/me')
    roles = effective_roles(set(me['roles']))
    types = h.get(role, '/api/contact-types')['contact_types']
    return {typ['legacy_field'] for typ in types if typ['visibility']['mode'] == 'everyone'
            or typ['visibility']['mode'] == 'roles' and roles.intersection(typ['visibility']['roles'])
            or typ['visibility']['mode'] == 'self_and_admins' and ('admin' in roles or me['person_id'] == person['id'])}


def expect_contact_values(container, person, shown, *, links=False):
    for field in ('work_phone', 'work_extension'):
        value = person.get(field, '')
        if not value:
            continue
        numbers = value.split(' / ') if field == 'work_extension' else [value]
        for number in numbers:
            if field not in shown:
                expect(container).not_to_contain_text(number)
            elif links:
                expect(container.get_by_role('link', name=number, exact=True)).to_have_attribute(
                    'href', 'tel:' + re.sub(r'[^+\d]', '', number))
            else:
                expect(container).to_contain_text(number)


def open_contact_details(panel):
    """The card's contacts are always shown reviewed: wait for the card itself."""
    expect(panel.locator('.pc')).to_be_visible()
    expect(panel.locator('.pc .loading')).to_have_count(0)


@pytest.mark.parametrize('role', list(COLUMNS))
def test_every_role_sees_its_columns_and_panel_in_both_themes_and_widths(browser, live_server, h, role):
    if role == 'programme_lead':
        identity(h, role, ['programme_lead'])
    person = contact(h)
    shown = visible_contact_fields(h, role, person)
    for viewport in (None, PHONE):
        context, page = sign_in(browser, live_server, role, viewport=viewport)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        try:
            for scheme in ('light', 'dark'):
                page.emulate_media(color_scheme=scheme)
                navigate(page, 'people')
                expect(row(page, person['display_name'])).to_be_visible()
                if viewport is None:
                    expect(page.locator('#main .dt thead th[scope=col]')).to_have_text(COLUMNS[role])
                # Directory and catalogue use the same contact visibility.
                expect_contact_values(row(page, person['display_name']), person, shown, links=True)
                expect(page.locator('#main .dt')).not_to_contain_text('PRIVATE-77')
                no_page_overflow(page)
                # The row opens the side panel (a sheet on phones); Escape closes it.
                row(page, person['display_name']).locator('strong').first.click()
                panel = page.get_by_role('region', name=person['display_name'])
                expect(panel).to_be_visible()
                open_contact_details(panel)
                expect_contact_values(panel, person, shown)
                tabs = panel.get_by_role('tablist')
                if TABS[role]:
                    expect_tabs(tabs.get_by_role('tab'), TABS[role])
                else:
                    expect(tabs).to_have_count(0)  # one tab (contacts): no strip
                    # The section is said once, in the header under the name (title · section, app.ts personDetails
                    # sub, P-02); the contact body does not repeat it.
                    expect(panel.get_by_text('Urgent cover')).to_have_count(1)
                # Only admin/coordinator see employee numbers; permitted contact facts remain in Overview.
                expect(panel.get_by_text('PRIVATE-77')).to_have_count(1 if role in {'admin', 'coordinator'} else 0)
                no_page_overflow(page)
                page.keyboard.press('Escape')
                expect(panel).to_have_count(0)
            assert not errors, errors
        finally:
            context.close()


def test_staff_directory_search_preserves_contact_privacy(browser, live_server, h):
    person = contact(h)
    shown = visible_contact_fields(h, 'staff', person)
    context, page = sign_in(browser, live_server, 'staff', viewport=PHONE)
    calls = []
    page.on('request', lambda r: calls.append(r.url) if '/api/' in r.url else None)
    try:
        navigate(page, 'people')
        assert any(u.endswith('/api/directory') for u in calls) and not any('/api/catalogues' in u for u in calls)
        page.get_by_label('Search people', exact=True).fill('registrar')
        found = row(page, person['display_name'])
        expect(found).to_be_visible()
        expect_contact_values(found, person, shown, links=True)
        expect(found.locator('a[href^="tel:"]')).to_have_count(sum(
            len(person[field].split(' / ')) for field in ('work_phone', 'work_extension') if field in shown))
        expect(found).not_to_contain_text('PRIVATE-77')
        expect(page.locator('.app-footer .dt-count')).to_contain_text('1 of')
        # Filtered to nothing: the toolbar stays, and clearing the search recovers.
        page.get_by_label('Search people', exact=True).fill('nobody has this name')
        expect(page.get_by_role('heading', name='No matching people', exact=True)).to_be_visible()
        page.get_by_label('Search people', exact=True).fill('')
        expect(found).to_be_visible()
        # No management action anywhere for staff.
        expect(page.locator('.topbar .page-actions .button')).to_have_count(0)
        expect(page.get_by_role('button', name=re.compile('^(Edit|More actions for)'))).to_have_count(0)
    finally:
        context.close()


def test_directory_failure_shows_retry_and_recovers(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff')
    try:
        page.route(re.compile(r'/api/directory$'), lambda route: route.abort())
        page.evaluate("location.hash='people'")
        expect(page.locator('#main [role=alert]')).to_contain_text('Couldn’t load People.')
        page.unroute(re.compile(r'/api/directory$'))
        page.get_by_role('button', name='Retry', exact=True).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'people')
        expect(page.locator('#main .dt tbody tr').first).to_be_visible()
    finally:
        context.close()


def test_staff_people_is_the_directory_and_a_refusal_is_never_no_people_yet(browser, live_server, h):
    """maintenance: staff hold no catalogues, so People reads the directory (never the placeholder as "No people yet"), asks
    nothing the role is refused, and a refused read says "Not available for your role." (info, no Retry)."""
    context, page = sign_in(browser, live_server, 'staff')
    refused = []
    page.on('response', lambda r: refused.append(r.url) if '/api/' in r.url and r.status == 403 else None)
    try:
        navigate(page, 'people')
        expect(page.locator('#main .dt tbody tr').first).to_be_visible()
        expect(page.get_by_role('heading', name='No people yet', exact=True)).to_have_count(0)
        assert not refused, refused
        page.route(re.compile(r'/api/directory$'), lambda route: route.fulfill(
            status=403, json={'error': {'code': 'FORBIDDEN', 'message': 'Not allowed'}}))
        page.evaluate("location.hash='people'")
        page.reload()  # a reload: re-opening within the tab answers from its kept reply (api.ts keptReads), never this failure
        info = page.locator('#main .notice.info')
        expect(info).to_have_text('Not available for your role.')
        expect(page.get_by_role('heading', name='No people yet', exact=True)).to_have_count(0)
        expect(page.locator('#main [role=alert]')).to_have_count(0)
        expect(page.get_by_role('button', name='Retry', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_a_credentialer_on_a_phone_gets_the_directory_never_the_collection(browser, live_server, h):
    """DL-029: on a phone every role gets Where is everyone or the contact directory, never the operator collection. A
    credentialer holds the lists but reads no timeline, so its phone People reads /api/directory (the narrower data)."""
    # phone_notice: the phone scope on (without it the harness continues every page as on a desktop).
    context, page = sign_in(browser, live_server, 'credentialer', viewport={'width': 390, 'height': 844}, phone_notice=True)
    try:
        asked = []
        page.on('request', lambda r: asked.append(r.url) if '/api/' in r.url else None)
        navigate(page, 'people')
        expect(page.locator('#main .dt tbody tr').first).to_be_visible()
        # Its rows are the directory's: this tab read /api/directory (at start, kept since: api.ts keptReads, ,
        # so People opened now may answer from memory), and People asked for no timeline.
        assert page.evaluate(r"() => performance.getEntriesByType('resource').some(e => /\/api\/directory$/.test(e.name))")
        assert not [u for u in asked if re.search(r'/api/timeline\b', u)], asked
        expect(page.get_by_role('searchbox', name='Search people')).to_have_attribute('placeholder', 'Name, title, phone or extension')
        expect(page.get_by_role('columnheader', name='Clinical eligibility')).to_have_count(0)
    finally:
        context.close()


def test_old_directory_address_and_ctrl_k_person_open_people_searched(browser, live_server, h):
    person = contact(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.evaluate("location.hash='directory'")
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'people')
        expect(page).to_have_url(re.compile(r'#people$'))
        navigate(page, 'home')
        page.keyboard.press('Control+k')
        page.keyboard.type('Fictional Colleague')
        page.get_by_role('option', name=re.compile('^Fictional Colleague Contact')).first.click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'people')
        expect(page).to_have_url(re.compile(r'#people\?open=' + person['id'] + '$'))
        expect(page.get_by_role('region', name=person['display_name'], exact=True)).to_be_visible()
    finally:
        context.close()


def test_credentialer_authorises_from_the_panel_and_it_stays_after_reload(browser, live_server, h):
    person = h.configure('people', {'display_name': 'Fictional Panel Person'})['record']
    service = h.get('credentialer', '/api/catalogues')['services'][0]
    context, page = sign_in(browser, live_server, 'credentialer')
    try:
        navigate(page, 'people')
        row(page, person['display_name']).locator('strong').first.click()
        panel = page.get_by_role('region', name=person['display_name'])
        # A credentialer starts with clinics and skills at the top of Overview.
        expect(panel.get_by_role('tab', name='Overview', exact=True)).to_have_attribute('aria-selected', 'true')
        expect(panel.locator('.ph-section h3')).to_have_text(['Details', 'Clinics', 'Skills', 'Rank'])
        clinics = panel.locator('.ph-section').filter(has=page.get_by_role('heading', name='Clinics', exact=True))
        expect(clinics.get_by_text('None', exact=True)).to_be_visible()
        panel.get_by_role('button', name='Authorise eligibility', exact=True).click()
        # The page's words (P-03): Authorise clinics · Clinics · From.
        form = page.get_by_role('dialog', name='Authorise clinics')
        expect(form.locator('.relationship-person')).to_contain_text(person['display_name'])  # SW3-D-14: fixed, not a picker
        pick(form, 'Clinics', [service['name']])
        form.get_by_label('From', exact=True).fill('2026-10-01')
        save_dialog(form, 'Authorise clinics')
        # The panel stays open on the same tab and shows the new record.
        expect(panel.get_by_role('tab', name='Overview', exact=True)).to_have_attribute('aria-selected', 'true')
        expect(clinics.get_by_text(service['name'], exact=True)).to_be_visible()
        assert any(r['person_id'] == person['id'] for r in h.get('credentialer', '/api/catalogues')['eligibilities'])
        page.reload()
        navigate(page, 'people')
        expect(row(page, person['display_name'])).to_contain_text(service['name'])
        row(page, person['display_name']).locator('strong').first.click()
        expect(clinics.get_by_text(service['name'], exact=True)).to_be_visible()
    finally:
        context.close()


def test_panel_tabs_follow_the_keyboard_and_close_returns_focus(browser, live_server, h):
    person = contact(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        r = row(page, person['display_name'])
        r.get_by_role('button', name='Open ' + person['display_name'], exact=True).click()  # one action: Open (P-02)
        panel = page.get_by_role('region', name=person['display_name'])
        first = panel.get_by_role('tab', name='Overview', exact=True)
        first.focus()
        page.keyboard.press('ArrowRight')
        expect(panel.get_by_role('tab', name='Schedule', exact=True)).to_be_focused()
        page.keyboard.press('Enter')
        expect(panel.get_by_role('tab', name='Schedule', exact=True)).to_have_attribute('aria-selected', 'true')
        page.keyboard.press('End')
        expect(panel.get_by_role('tab', name='Quotas & offers', exact=True)).to_be_focused()
        page.keyboard.press('Escape')
        expect(panel).to_have_count(0)
        # Focus returns to the person's row menu (the menu item that opened the panel is gone).
        expect(r.get_by_role('button', name='Open ' + person['display_name'], exact=True)).to_be_focused()
    finally:
        context.close()


@pytest.fixture
def department_scale_people(browser, live_server, h):
    """150+ people: People opens without reading people again (saved views and, on its first open, the deferred lists)."""
    org = h.seed['organisation_id']
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Person), [{'id': str(uuid.uuid4()), 'organisation_id': org, 'display_name': f'Scale Person {k:03}',
                                       'title': 'Resident', 'work_phone': f'5550{k:03}', 'tags': ['night'] if k % 3 else []} for k in range(160)])
    context, page = sign_in(browser, live_server, 'coordinator')
    calls = []
    page.on('request', lambda r: calls.append(r.url) if '/api/' in r.url else None)
    try:
        started = time.perf_counter()
        navigate(page, 'inbox')
        baseline = time.perf_counter() - started
        # The seed has setup left to do. Wait for the boot-time Configuration read before measuring People.
        expect(page.locator('#nav-badge-configuration')).to_have_text(re.compile(r'^\d+ of \d+ setup steps done$'))
        calls.clear()
        started = time.perf_counter()
        navigate(page, 'people')
        elapsed = time.perf_counter() - started
        yield page, calls, baseline, elapsed
    finally:
        context.close()


def test_department_scale_people_open_fast_with_one_request(department_scale_people):
    page, calls, _, _ = department_scale_people
    expect(page.locator('.app-footer .dt-count')).to_have_text(re.compile(r'^16\d people$'))
    # Nothing but saved views: no people data again, and no read left over from a page already left (Home's today panel
    # used to start after the person had moved on, which made this flaky under load).
    # (and the one-off check that the server has the person timeline, made once a session). Home and Inbox hold only the
    # lean lists (S7, ): People's first open reads the deferred ones once, sending the versions held (`known`), never
    # a full or lean catalogue read.
    other = [u for u in calls if '/api/table-views' not in u and '/api/timeline?' not in u]
    deferred = [u for u in other if '/api/catalogues?known=' in u and 'boot=true' not in u]
    assert len(deferred) == 1 and [u for u in other if u not in deferred] == [], calls


@pytest.mark.wall_benchmark
def test_department_scale_people_cpu_budget(department_scale_people):
    _, _, baseline, elapsed = department_scale_people
    # Wall time includes Chromium/HTTP work; nearby navigation calibrates shared-runner scheduling delays.
    budget = max(5, baseline * 5)
    assert elapsed < budget, {'seconds': elapsed, 'baseline_seconds': baseline, 'budget_seconds': budget}


def test_long_text_stays_on_one_line_and_badges_and_chips_stay_inside_their_cells(browser, live_server, h):
    """A long job title is one line (the full title is the tooltip), names are never broken, Details to review badges
    stay inside their cell, the Person column stays pinned while the table scrolls sideways, and on phones a long clinic
    chip wraps inside its card."""
    title = 'Consultant Department A Physician, Department of Field Medicine'
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Service, h.seed['services'][0]).name = 'Field Medicine Virtual Clinic for Urgent Cover'
        person = db.get(m.Person, h.seed['people'][0])
        person.title, name = title, person.display_name
    for viewport in (None, PHONE):
        context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport)
        try:
            # Every column shown (as the View settings' property list would).
            page.evaluate("""async () => {const me=await fetch('/api/me').then(r=>r.json());
                localStorage.setItem('rota.table.'+me.identity_id+'.'+me.department.id+'.people',JSON.stringify({hidden: []}));}""")
            navigate(page, 'people')
            r = row(page, name)
            expect(r).to_be_visible()
            if viewport is None:
                clip = r.locator('.dt-clip', has_text=title)
                expect(clip).to_have_attribute('title', title)
                # Measured on the table on screen: the list may draw its rows once more right after the page is shown
                # (run : dt-rove-1 -> dt-rove-2, no read), and a replaced cell measures 0 wide. The locators
                # find the new cells on each try; a title that wraps or fits never passes.
                one_line = ('n => n.isConnected && n.scrollWidth > n.clientWidth && n.getClientRects().length === 1 && n.offsetHeight < 30'
                            " && n.closest('tr').querySelector('.person-cell strong').getClientRects().length === 1")
                wait_until(page, lambda: clip.evaluate(one_line), lambda: clip.evaluate(
                    'n => ({connected: n.isConnected, scroll: n.scrollWidth, client: n.clientWidth, rects: n.getClientRects().length, height: n.offsetHeight})'), 10)
                outside = page.evaluate("""() => [...document.querySelectorAll('#main .dt-table td .badge')].filter(b => {
                    const a = b.getBoundingClientRect(), c = b.closest('td').getBoundingClientRect();
                    return a.left < c.left - 1 || a.right > c.right + 1 || b.scrollWidth > b.clientWidth + 1;}).map(b => b.textContent)""")
                assert outside == [], outside
                page.locator('#main .table-scroll').evaluate('s => s.scrollTo(s.scrollWidth, 0)')
                expect(r.locator('.person-cell strong')).to_be_in_viewport()
            else:
                chip = r.locator('.role-chip', has_text='Field Medicine Virtual Clinic for Urgent Cover')
                expect(chip).to_be_visible()
                assert chip.evaluate("c => c.getBoundingClientRect().right <= c.closest('.dt-card').getBoundingClientRect().right")
        finally:
            context.close()


def test_six_clinics_stay_one_line_until_the_column_wraps(browser, live_server, h):
    """Clinical eligibility is one line by default (first two chips, then "+4" opening the numbered list), in both
    themes; the column menu's Wrap this column shows every chip."""
    person = contact(h)
    clinics = [h.configure('services', {'name': f'Pill Clinic {n}', 'starts_on': '2026-01-01'})['record']['id'] for n in range(6)]
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Eligibility), [{'person_id': person['id'], 'department_id': h.seed['department_id'], 'service_id': s,
                                            'starts_on': date(2020, 1, 1)} for s in clinics])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        cell = row(page, person['display_name']).locator('td[data-col=services]')
        for scheme in ('light', 'dark'):
            page.emulate_media(color_scheme=scheme)
            expect(cell.locator('.role-chip:visible')).to_have_count(2)
            expect(cell.locator('.pill-more')).to_have_text('+4')
            assert cell.locator('.pill-list').evaluate("l => l.offsetHeight <= l.querySelector('.role-chip').offsetHeight + 4"), 'one line'
        cell.locator('.pill-more').click()
        expect(page.get_by_role('dialog', name='All clinics').locator('li')).to_have_count(6)
        page.keyboard.press('Escape')
        page.locator('#main').get_by_role('button', name=re.compile('^Clinical eligibility')).click()
        page.get_by_role('menuitemcheckbox', name='Wrap this column').click()
        expect(cell).to_have_class(re.compile('dt-wrap'))
        expect(cell.locator('.role-chip:visible')).to_have_count(6)
        expect(cell.locator('.pill-more')).to_be_hidden()
    finally:
        context.close()


@pytest.mark.parametrize('role', ['coordinator', 'staff'])
@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_people_compact_search_opens_exact_name_and_survives_reload(browser, live_server, h, role, width, theme):
    duplicates = [h.configure('people', {'display_name': 'Dr. Fictional Al-Example', 'directory_section': section,
                  'work_phone': mobile, 'work_extension': extension})['record']
                  for section, mobile, extension in [('Alpha', '+15550101', '1234'), ('Beta', '+15550102', '6543')]]
    accent = h.configure('people', {'display_name': 'Dr. Synthétique Al-Example', 'work_extension': '7410'})['record']
    arabic = h.configure('people', {'display_name': 'كيذش قيثه', 'work_extension': '8520'})['record']
    h.configure('people', {'display_name': 'Unrelated Colleague', 'work_extension': '9999'})
    shown = visible_contact_fields(h, role, duplicates[1])
    context, page = sign_in(browser, live_server, role, {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=theme)
        navigate(page, 'people')
        search = page.get_by_label('Search people', exact=True)
        records = page.locator('#main .dt-body tbody tr[data-row], #main .dt-body .dt-card[data-row]')
        search.fill('fictionalalexample')
        expect(records).to_have_count(2)
        # Duplicate labels never decide identity; a withheld contact is not an identifier either.
        chosen = page.locator(f'#main .dt-body [data-row="{duplicates[1]["id"]}"]')
        expect_contact_values(chosen, duplicates[1], shown, links=True)
        chosen.locator('strong').first.click()
        panel = page.get_by_role('region', name=duplicates[1]['display_name'], exact=True)
        expect(page).to_have_url(re.compile(r'#people\?open=' + duplicates[1]['id'] + '$'))
        open_contact_details(panel)
        expect_contact_values(panel, duplicates[1], shown)
        expect(panel).not_to_contain_text('1234')
        expect(panel).not_to_contain_text(duplicates[0]['work_phone'])
        page.reload()
        expect(panel).to_be_visible()
        open_contact_details(panel)
        expect_contact_values(panel, duplicates[1], shown)
        expect(panel).not_to_contain_text('1234')
        page.keyboard.press('Escape')
        expect(panel).to_have_count(0)
        for query, person in [('synthetiquealexample', accent), ('كيذشقيثه', arabic)]:
            search.fill(query)
            expect(records).to_have_count(1)
            expect(records).to_have_attribute('data-row', person['id'])
        # Punctuation remains a literal query; an empty compact key must not match every record.
        search.fill('-')
        expect(records).to_have_count(3)
        expect(records.filter(has_text='Unrelated Colleague')).to_have_count(0)
        search.fill('no-such-person')
        expect(records).to_have_count(0)
        search.fill('')
        expect(records.filter(has_text='Unrelated Colleague')).to_have_count(1)
    finally:
        context.close()


def test_collection_search_draws_one_focus_ring(browser, live_server, h):
    """The search box's frame shows focus; the input inside it draws no second ring of its own."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        search = page.get_by_label('Search people', exact=True)
        search.fill('Staff 01')
        expect(search).to_be_focused()
        assert search.evaluate('i => getComputedStyle(i).boxShadow') == 'none'
        assert search.evaluate("i => getComputedStyle(i.closest('.dt-search-wrap')).boxShadow") != 'none'
    finally:
        context.close()


@pytest.mark.parametrize('loading', [False, True], ids=['fast', 'loading'])
def test_wide_table_shows_columns_continue_under_the_pinned_actions(browser, live_server, h, loading):
    """A table wider than its box: the pinned actions column casts an edge while columns continue beneath it (a heading
    under it reads as scrolled, not cut); scrolled to the end, the edge goes."""
    if loading:
        context, page, _, held, release = held_app(browser, live_server, 'coordinator', route='people')
        page.set_viewport_size({'width': 1100, 'height': 900})
    else:
        context, page = sign_in(browser, live_server, 'coordinator', {'width': 1100, 'height': 900})
    try:
        if loading:
            wait_held(page, held)
            expect(page.locator('#main .loading.page-sk:not(.sk-wait)')).to_be_visible()
            release()
            expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'people')
        else:
            navigate(page, 'people')
        scroll = page.locator('#main .dt-body > .table-scroll')
        assert scroll.evaluate('s => s.scrollWidth > s.clientWidth + 1')
        expect(scroll).to_have_class(re.compile(r'\bmore-right\b'))
        assert page.locator('#main th.dt-actions-col').evaluate('t => getComputedStyle(t).boxShadow') != 'none'
        scroll.evaluate('s => s.scrollLeft = s.scrollWidth')
        expect(scroll).not_to_have_class(re.compile(r'\bmore-right\b'))
    finally:
        if loading:
            release()
        context.unroute_all(behavior='wait')
        context.close()


def test_phone_person_tabs_show_they_scroll_and_keep_the_open_tab_in_view(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', PHONE)
    try:
        pid = h.seed['people'][0]
        navigate(page, f'people?open={pid}&tab=overview')
        tabs = page.locator('.person-panel [role=tablist]')
        expect(tabs).to_have_class(re.compile(r'\bfade-end\b'))  # more tabs to the right
        navigate(page, f'people?open={pid}&tab=access')
        expect(tabs.get_by_role('tab', name='Access', exact=True)).to_have_attribute('aria-selected', 'true')
        expect(tabs).to_have_class(re.compile(r'\bfade-start\b'))
        page.wait_for_function("""() => {const r=document.querySelector('.person-panel [role=tablist]'),t=r.querySelector('[aria-selected=true]');
            const a=r.getBoundingClientRect(),b=t.getBoundingClientRect();return b.left>=a.left-1&&b.right<=a.right+1;}""")
    finally:
        context.close()


@pytest.mark.parametrize('role', ['credentialer', 'auditor'])
def test_withheld_numbers_are_not_missing_and_contacts_come_from_the_directory(browser, live_server, h, role):
    """Contacts follow configured visibility in the list, card and reload; a withheld employee number is not missing."""
    person = contact(h)
    shown = visible_contact_fields(h, role, person)
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page, 'people')
        page.get_by_role('button', name=re.compile('^View')).click()
        review = page.get_by_role('dialog', name='View settings').get_by_role('checkbox', name='Details to review', exact=True)
        if role == 'credentialer':
            review.check()
        else:
            expect(review).to_have_count(0)
        page.keyboard.press('Escape')
        expect_contact_values(row(page, person['display_name']), person, shown, links=True)
        expect(row(page, person['display_name']).get_by_text('No employee number', exact=True)).to_have_count(0)
        row(page, person['display_name']).locator('strong').first.click()
        panel = page.get_by_role('region', name=person['display_name'])
        open_contact_details(panel)
        expect_contact_values(panel, person, shown)
        expect(panel.get_by_text('PRIVATE-77')).to_have_count(0)
        page.reload()
        open_contact_details(panel)
        expect_contact_values(panel, person, shown)
        expect(panel.get_by_text('PRIVATE-77')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('release', ['abort', 'fulfil'])
def test_a_directory_read_held_across_a_sign_in_never_shows_the_last_accounts_people(browser, live_server, h, release):
    """Account A's (credentialer: contacts withheld) directory read is held; A signs out and staff
    of another organisation (B) signs in; A's read then fails or answers. B's People reads B's own directory and never
    shows A's people, whatever A's read did."""
    person = contact(h)
    with transaction(h.app.state.engine, write=True) as db:
        org = m.Organisation(name='Other hospital', timezone='Asia/Riyadh', week_start=6)
        db.add(org); db.flush()
        dept = m.Department(organisation_id=org.id, name='Elsewhere')
        db.add(dept); db.flush()
        dept_id = dept.id
    identity(h, 'other_staff', ['staff'], department=dept_id)
    context, page = sign_in(browser, live_server, 'credentialer')
    calls = []
    try:
        page.add_init_script("""(() => {
          const fetch = window.fetch.bind(window);
          window.fetch = (input, init) => {
            if (new URL(input instanceof Request ? input.url : input, location.href).pathname !== '/api/directory')
              return fetch(input, init);
            window.fetch = fetch;
            return new Promise((resolve, reject) => { window.heldDirectory = {resolve, reject}; });
          };
        })();""")
        page.reload()  # boot reads the people catalogue, then A's directory: held
        page.wait_for_function('() => !!window.heldDirectory')
        sign_out(page)
        page.on('request', lambda r: calls.append(r.url) if r.url.endswith('/api/directory') else None)
        page.get_by_label('Username or email', exact=True).fill('other_staff')
        page.get_by_role('button', name='Continue', exact=True).click()
        page.get_by_label('Password', exact=True).fill(TEST_PASSWORD)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.locator('#main[data-loaded-view]')).to_have_count(1, timeout=20000)
        # A's read lands only now, after B is signed in: as a failure, or as an answer naming A's colleague.
        page.evaluate("""({release, person}) => {
          const held = window.heldDirectory;
          delete window.heldDirectory;
          if (release === 'abort') held.reject(new TypeError('Failed to fetch'));
          else held.resolve(new Response(JSON.stringify({people: [person]}), {headers: {'Content-Type': 'application/json'}}));
        }""", {'release': release, 'person': {'id': person['id'], 'display_name': person['display_name'],
                                            'work_phone': '+966 11 000 1234'}})
        page.evaluate('() => new Promise(requestAnimationFrame)')
        navigate(page, 'people')
        assert calls, 'B read its own directory'
        main = page.locator('#main')
        expect(main).to_have_attribute('data-loaded-view', 'people')
        expect(main).not_to_contain_text(person['display_name'])
        expect(main).not_to_contain_text('+966 11 000 1234')
    finally:
        context.close()

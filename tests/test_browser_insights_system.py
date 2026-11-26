"""Merged pages: Insights (Fairness · Workload · Exports), Configuration with the Setup guide, Access & approvals and
System — tabs per role, one shared publication source, each tab's own header and footer status, old addresses,
empty and failure states, phone and dark."""
import csv
import io
import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from sqlalchemy import select
from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import SYSTEM_ROLES, PersonInput, ServiceInput
from .governance_support import developer, identity
from .test_browser import route_fetch, browser, live_server, sign_in, navigate, save_dialog  # noqa: F401
from .test_browser_configuration_shell import expect_coordinator_sections
from .test_browser_w3_setup_today import no_page_overflow, finish_required_steps
from .test_block_quotas import quota

pytestmark = pytest.mark.browser
PHONE = {'width': 390, 'height': 844}


def two_versions(h, people=None):
    """Two published rotas; staff (Alex Morgan) works in both."""
    me = h.get('staff', '/api/me')['person_id']
    for name, day in (('Fictional October rota', '2026-10-04'), ('Fictional November rota', '2026-11-01')):
        end = '2026-10-11' if day == '2026-10-04' else '2026-11-08'
        h.publish(h.shift(h.workspace(name, start=day, end=end), start=day + 'T08:00:00+03:00', end=day + 'T12:00:00+03:00', people=people or [me])['workspace'])
    return h.get('coordinator', '/api/publications')['publications']


def source(page):
    return page.locator('.topbar').get_by_label('Version', exact=True)


def publication_selector_versions(h):
    """Different published people/clinics, then renamed inactive current-directory records."""
    first_person, second_person = h.seed['people'][:2]
    first = h.publish(h.shift(h.workspace('October selector source'), people=[first_person])['workspace'])['publication_id']
    second = h.publish(h.shift(h.workspace('November selector source', start='2026-11-01', end='2026-11-08'),
        start='2026-11-01T08:00:00+03:00', end='2026-11-01T12:00:00+03:00',
        people=[second_person], service=1)['workspace'])['publication_id']
    issued = [h.get('coordinator', '/api/publications/' + identifier) for identifier in (first, second)]
    catalogue = h.get('admin', '/api/catalogues')
    person = next(p for p in catalogue['people'] if p['id'] == first_person)
    clinic = next(s for s in catalogue['services'] if s['id'] == h.seed['services'][0])
    person_value = {key: value for key, value in person.items() if key in PersonInput.model_fields}
    clinic_value = {key: value for key, value in clinic.items() if key in ServiceInput.model_fields}
    h.configure('people', {**person_value, 'display_name': 'Renamed current directory person', 'active': False}, person['id'], person['revision'])
    h.configure('services', {**clinic_value, 'name': 'Renamed current clinic', 'active': False}, clinic['id'], clinic['revision'])
    return issued


def omit_old_publication_catalogue_rows(page, issued):
    """Historical publication choices must still work when absent from the current catalogue response."""
    old_people, old_services = set(issued['snapshot']['people']), set(issued['snapshot']['services'])
    def respond(route):
        response = route_fetch(route)
        data = response.json()
        for kind, old in (('people', old_people), ('services', old_services)):
            if kind in data:  # a later read sends only the lists that changed
                data[kind] = [r for r in data[kind] if r['id'] not in old]
        route.fulfill(response=response, json=data)
    page.route(re.compile(r'/api/catalogues(\?|$)'), respond)
    # sign_in already loaded the original catalogue: reload so this fixture is actually consumed.
    loaded_view = page.locator('#main').get_attribute('data-loaded-view')
    with page.expect_response(lambda response: response.url.split('?')[0].endswith('/api/catalogues')):
        page.reload(wait_until='domcontentloaded')
    expect(page.locator('#main')).to_have_attribute('data-loaded-view', loaded_view)


def publication_selector_unstaffed(h):
    typ = h.configure('assignment_types', {'name': 'Unstaffed export group', 'starts_on': '2020-01-01',
        'definition': {'purpose': 'group', 'requirements': []}})['record']
    return h.publish(h.shift(h.workspace('Unstaffed selector source'), people=[], typ=typ['id'])['workspace'])['publication_id']


@pytest.mark.parametrize('width', [1440, 390])
def test_unstaffed_publication_disables_personal_calendar_without_offering_unassigned_people(browser, live_server, h, width):
    publication = publication_selector_unstaffed(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    try:
        navigate(page, 'insights/exports')
        expect(source(page)).to_have_value(publication)
        row = page.locator('.export-row').filter(has_text='Personal calendar')
        expect(row.get_by_role('button', name='Download Personal calendar', exact=True)).to_be_disabled()
        expect(row).to_contain_text('No people in this version')
        expect(row.get_by_label('Person', exact=True)).to_have_count(0)
        # Other report formats remain usable even though no person has an assignment.
        with page.expect_download() as pending:
            page.get_by_role('button', name='Download Report JSON', exact=True).click()
        document = json.loads(Path(pending.value.path()).read_text(encoding='utf-8-sig'))
        assert document['publication_id'] == publication and document['rows'] == []
    finally:
        context.close()


@pytest.mark.parametrize('role,width,missing_current', [('coordinator', 1440, False), ('auditor', 390, True)])
def test_personal_calendar_choices_follow_publication_names_and_switch_to_the_downloaded_version(browser, live_server, h, role, width, missing_current):
    issued = publication_selector_versions(h)
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 900})
    if missing_current:
        omit_old_publication_catalogue_rows(page, issued[0])
    try:
        navigate(page, 'insights/exports')
        for value in issued:
            publication, snapshot = value['publication'], value['snapshot']
            person_id, person = next(iter(snapshot['people'].items()))
            source(page).select_option(publication['id'])
            picker = page.locator('.export-row').filter(has_text='Personal calendar').get_by_label('Person', exact=True)
            expect(picker.locator('option:not([value=""])')).to_have_text([person['display_name']])
            assert picker.locator('option:not([value=""])').evaluate_all('nodes => nodes.map(n => n.value)') == [person_id]
            # The shared required picker chooses its sole option, never the previous version's person.
            expect(picker).to_have_value(person_id)
            picker.select_option(person_id)
            with page.expect_download() as pending:
                page.get_by_role('button', name='Download Personal calendar', exact=True).click()
            download = pending.value
            assert download.failure() is None and download.suggested_filename == 'personal-rota.ics'
            calendar = Path(download.path()).read_text(encoding='utf-8-sig').replace('\n ', '')
            assert 'X-PUBLICATION-ID:' + publication['id'] in calendar
            assert calendar.count('BEGIN:VEVENT') == calendar.count('END:VEVENT') == 1
            uid = next(line for line in calendar.splitlines() if line.startswith('UID:'))
            assert person_id in uid and all(other not in uid for other in h.seed['people'] if other != person_id)
            assert 'DTSTART:' + snapshot['shifts'][0]['start'][:10].replace('-', '') + 'T050000Z' in calendar
    finally:
        context.close()


@pytest.mark.parametrize('version', ['scheduled-v2', 'scheduled-v3'])
def test_workload_and_exports_share_one_publication_source(browser, live_server, h, monkeypatch, version):
    from apps.api.services import planning
    original = planning.publication_snapshot

    def snapshot_version(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        snapshot['metric_version'] = version
        if version == 'scheduled-v2':
            del snapshot['overtime_offered_intervals']
        return snapshot

    monkeypatch.setattr(planning, 'publication_snapshot', snapshot_version)
    pubs = two_versions(h)
    older = next(p for p in pubs if p['sequence'] == 1)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'insights/workload')
        expect(page.get_by_role('tablist', name='Insights').get_by_role('tab')).to_have_text(['Fairness', 'Workload', 'Exports'])
        source(page).select_option(older['id'])
        # The version is named once, in the picker: the rota's name and dates, Superseded only when it is (I-15).
        expect(source(page).locator('option:checked')).to_have_text('Fictional October rota · 4–10 Oct 2026')
        expect(page.locator('#main .panel-head')).not_to_contain_text('Publication')
        expect(page.locator('[data-table="workload"] th[data-col=reserved]')).to_have_count(0)
        expect(page.locator('[data-table="workload"] th[data-col=off_cover]')).to_have_count(0)
        page.locator('[data-table="workload"]').get_by_role('button', name='View settings', exact=True).click()
        page.get_by_label('Reserved hours', exact=True).check()
        page.get_by_label('Off-cover hours', exact=True).check()
        page.keyboard.press('Escape')
        expect(page.get_by_role('columnheader', name='Reserved hours', exact=True)).to_be_visible()
        expect(page.get_by_role('columnheader', name='Off-cover hours', exact=True)).to_be_visible()
        # One count: the table's own (I-31); the footer adds people only when a breakdown gives several rows each.
        expect(page.locator('.app-footer .page-status')).not_to_contain_text('with duties')
        page.get_by_label('Report breakdown', exact=True).select_option('week')
        expect(page.locator('.app-footer .page-status')).to_contain_text('with duties')
        page.get_by_label('Report breakdown', exact=True).select_option('')
        page.get_by_role('tab', name='Exports', exact=True).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-tab', 'exports')
        expect(source(page)).to_have_value(older['id'])  # the same source on both tabs
        expect(page).to_have_url(re.compile(r'#insights/exports$'))
        # No commitments report in this version: no row rather than a dead button (I-20).
        expect(page.get_by_role('button', name='Download Commitments CSV', exact=True)).to_have_count(0)
        with page.expect_download() as got:
            page.get_by_role('button', name='Download Duties CSV', exact=True).click()
        assert got.value.suggested_filename == 'published-assignments.csv'
        # The version's only person is chosen for you, so the calendar file is ready to download.
        calendar = page.locator('#main .export-row').filter(has_text='Personal calendar')
        expect(calendar.get_by_label('Person', exact=True)).to_have_value(h.get('staff', '/api/me')['person_id'])
        expect(calendar.get_by_role('button', name='Download Personal calendar', exact=True)).to_be_enabled()
    finally:
        context.close()


def test_staff_see_their_own_line_and_subscription_first(browser, live_server, h):
    two_versions(h)
    context, page = sign_in(browser, live_server, 'staff', viewport=PHONE)
    try:
        for scheme in ('light', 'dark'):
            page.emulate_media(color_scheme=scheme)
            navigate(page, 'insights/workload')
            expect(page.locator('.workload-mine')).to_contain_text('1 duty')
            navigate(page, 'insights/exports')
            first = page.locator('#main [role=tabpanel] > *').first
            expect(first).to_contain_text('Calendar subscription')
            mine = page.locator('#main .export-row').filter(has_text='Personal calendar')
            expect(mine).to_contain_text('Your duties')
            expect(mine.get_by_label('Person', exact=True)).to_have_count(0)
            expect(page.get_by_role('button', name='Download Commitments CSV', exact=True)).to_have_count(0)
            no_page_overflow(page)
    finally:
        context.close()


def test_insights_empty_and_failure_states(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'insights/workload')
        expect(page.get_by_role('heading', name='Nothing published yet', exact=True)).to_be_visible()
        navigate(page, 'insights/exports')
        expect(page.get_by_role('heading', name='Nothing published yet', exact=True)).to_be_visible()
        page.route(re.compile(r'/api/publications$'), lambda route: route.abort())
        page.evaluate("location.hash='insights/workload'")
        page.reload()  # a reload: re-opening within the tab answers from its kept reply (api.ts keptReads), never this failure
        expect(page.locator('#main [role=alert]')).to_contain_text('Couldn’t load Workload.')  # DL-045 (c5a282a5d): named by the open tab
        page.unroute(re.compile(r'/api/publications$'))
        page.get_by_role('button', name='Retry', exact=True).click()
        expect(page.get_by_role('heading', name='Nothing published yet', exact=True)).to_be_visible()
    finally:
        context.close()


OLD = {'reports': 'insights/workload', 'fairness': 'insights/fairness', 'governance': 'access/routes', 'access': 'access/people',
       'data': 'system/imports', 'operations': 'system/operations', 'audit': 'system/audit', 'setup': 'configuration/setup'}


def test_old_addresses_land_on_the_right_tab(browser, live_server, h):
    context, page = sign_in(browser, live_server, developer(h))
    try:
        for old, new in OLD.items():
            page.evaluate('k => { location.hash = k }', old)
            page_name, _, tab = new.partition('/')
            expect(page.locator('#main')).to_have_attribute('data-loaded-view', page_name)
            expect(page).to_have_url(re.compile(re.escape('#' + new) + '$'))
            if page_name != 'configuration':
                expect(page.get_by_role('tab', selected=True).first).to_be_visible()
            expect(page.locator('#main [role=alert]')).to_have_count(0)
    finally:
        context.close()


def test_configuration_opens_on_the_setup_guide_until_setup_is_done(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        page.evaluate("location.hash='configuration'")
        expect(page).to_have_url(re.compile(r'#configuration/setup$'))
        nav = page.get_by_role('navigation', name='Configuration sections')
        guide = nav.get_by_role('button', name='Setup guide', exact=True)
        expect(guide).to_have_attribute('aria-current', 'page')
        expect(nav.get_by_role('button').first).to_have_text(re.compile(r'^Setup guide\d+/\d+$'))  # first, with progress
        expect(page.locator('.setup-steps')).to_be_visible()
        nav.get_by_role('button', name='Duty types', exact=True).click()
        expect(page).to_have_url(re.compile(r'#configuration/assignment_types$'))
        expect(page.locator('.config-pane .section-head h2')).to_have_text('Duty types')
        # Ctrl K finds the guide.
        page.keyboard.press('Control+k')
        page.keyboard.type('Setup guide')
        page.get_by_role('option', name=re.compile('^Setup guide')).first.click()
        expect(page).to_have_url(re.compile(r'#configuration/setup$'))
        expect(page.locator('.setup-steps')).to_be_visible()
        # The sidebar badge on Configuration counts the same steps as the guide.
        badge = page.locator('#department-nav [data-route=configuration] .nav-count')
        expect(badge).to_have_text(re.compile(r'^\d+/\d+$'))
        # The ring reads "6/8" over "required" (059355f6e); the badge is the count alone.
        assert badge.inner_text() == page.locator('.setup-ring span').inner_text().split()[0]
    finally:
        context.close()
    finish_required_steps(h, clinic_hours=True)
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration')
        expect(page).to_have_url(re.compile(r'#configuration/assignment_types$'))  # the section shown has its address
        nav = page.get_by_role('navigation', name='Configuration sections')
        expect(nav.get_by_role('button', name='Setup guide', exact=True)).to_have_count(0)
        expect(nav.locator('[data-section=setup]')).to_have_text('Data health')  # stays first, now Data health (C-33)
        expect(page.locator('#department-nav [data-route=configuration] .nav-count')).to_have_count(0)
        # Its words go with it: no hidden element and no description left on the button.
        expect(page.locator('#nav-badge-configuration')).to_have_count(0)
        expect(page.locator('#department-nav [data-route=configuration]')).not_to_have_attribute('aria-describedby', re.compile('.*'))
        navigate(page, 'configuration/setup')  # still reachable by its address
        expect(page.locator('.setup-hero h2')).to_have_text(re.compile('^Ready to plan'))
    finally:
        context.close()
    # After setup, coordinators open on the first permitted record section; the guide remains as Data health.
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        expect(page.locator('#department-nav [data-route=configuration]')).to_have_count(1)
        navigate(page, 'configuration')
        nav = page.get_by_role('navigation', name='Configuration sections')
        sections = expect_coordinator_sections(page, h)
        expect(nav.get_by_role('button', name='Auto-schedule & scoring', exact=True)).to_be_visible()
        expect(nav.get_by_role('button', name='Data health', exact=True)).to_be_visible()
        expect(nav.get_by_role('button', name='Scheduling groups', exact=True)).to_be_visible()
        expect(nav.get_by_role('button', name='Tags', exact=True)).to_be_visible()
        first_record = next(spec for kind, spec in sections.items() if kind != 'setup' and not spec.get('settings'))
        expect(page.locator('.config-pane .section-head h2').first).to_have_text(first_record['label'])
        nav.get_by_role('button', name='Tags', exact=True).click()
        expect(page.locator('.config-pane .section-head h2').first).to_have_text('Tags')
        navigate(page, 'configuration/setup')
        expect(page.locator('.setup-hero h2')).to_have_text(re.compile('^Ready to plan'))
    finally:
        context.close()


@pytest.mark.parametrize('giver, role, label', [('admin', 'approver', 'Approver'), ('developer', 'auditor', 'Auditor')])
def test_admin_gives_a_role_and_role_history_shows_it(browser, live_server, h, giver, role, label):
    # Developer and Auditor are given only by a Developer: an admin is not offered them while a Developer exists.
    giver = developer(h) if giver == 'developer' else giver
    target = next(i for i in h.get('admin', '/api/access')['identities'] if i['id'] == h.seed['identities']['staff'])
    context, page = sign_in(browser, live_server, giver)
    try:
        navigate(page, 'access/people')
        tabs = page.get_by_role('tablist', name='Access & approvals')
        before = int(tabs.get_by_role('tab', name='Role history', exact=True).locator('.tab-count').inner_text())
        # Header actions belong to People & roles only.
        expect(page.locator('.topbar .page-actions').get_by_role('button', name='Add hospital sign-in', exact=True)).to_be_visible()
        page.get_by_role('button', name=f"More actions for {target['display_name']}", exact=True).first.click()
        page.get_by_role('menuitem', name='Give a role', exact=True).click()
        form = page.get_by_role('dialog')
        options = form.get_by_label('Role', exact=True).locator('option')
        offered = {'Developer', 'Auditor'} if giver == 'developer' else set()
        for system in ('Developer', 'Auditor'):
            option = options.filter(has_text=system)
            expect(option).to_have_count(1)  # maintenance: every role is shown; one this giver may not give is disabled with why
            if system in offered:
                expect(option).to_be_enabled()
            else:
                expect(option).to_be_disabled()
                expect(option).to_have_attribute('data-hint', 'Only a Developer can grant this')
        form.get_by_label('Role', exact=True).select_option(role)
        form.get_by_label('Reason', exact=True).fill('Fictional cover')
        save_dialog(form, 'Give role')
        tabs.get_by_role('tab', name='Role history', exact=True).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-tab', 'history')
        expect(page.locator('.topbar .page-actions .button')).to_have_count(0)
        expect(tabs.get_by_role('tab', name='Role history', exact=True).locator('.tab-count')).to_have_text(str(before + 1))
        # Grouped by person: the name is the group heading, not repeated in each row (AC-02).
        def grant():
            page.get_by_label('Search access history', exact=True).fill(target['display_name'])
            expect(page.locator('#main tr.dt-group').filter(has_text=target['display_name'])).to_have_count(1)
            return page.locator('#main tbody tr[data-row]').filter(has_text=label)
        expect(grant()).to_have_count(1)
        page.reload()
        expect(page.locator('#main')).to_have_attribute('data-loaded-tab', 'history')
        expect(grant()).to_have_count(1)
    finally:
        context.close()


def test_a_developer_without_admin_manages_only_developer_and_auditor(browser, live_server, h):
    # A Developer opens People & roles to give Developer, Auditor or Installation admin; department roles,
    # sign-ins and accounts stay with admins.
    identity(h, 'devonly', ['operator'])
    target = next(i for i in h.get('admin', '/api/access')['identities'] if i['id'] == h.seed['identities']['staff'])
    context, page = sign_in(browser, live_server, 'devonly')
    try:
        navigate(page, 'access/people')
        expect(page.locator('#main')).to_have_attribute('data-loaded-tab', 'people')
        for header in ('Invite person', 'Add hospital sign-in'):
            expect(page.locator('.topbar .page-actions').get_by_role('button', name=header, exact=True)).to_have_count(0)
        # Department roles cannot be removed here; Developer can (from the row's actions, never a chip ×).
        expect(page.locator('.chip-x')).to_have_count(0)
        page.get_by_role('button', name=f"More actions for {target['display_name']}", exact=True).first.click()
        expect(page.get_by_role('menuitem', name=re.compile('^Remove (Admin|Staff) role'))).to_have_count(0)
        expect(page.get_by_role('menuitem', name='Password reset link', exact=True)).to_have_count(0)
        page.get_by_role('menuitem', name='Give a role', exact=True).click()
        form = page.get_by_role('dialog')
        expect(form.get_by_label('Role', exact=True).locator('option:not([disabled])')).to_have_text(['Auditor', 'Installation admin', 'Developer'])
        expect(form.get_by_label('Role', exact=True).locator('option', has_text='Coordinator')).to_have_attribute('data-hint', 'Only an Admin can grant this')
        form.get_by_label('Role', exact=True).select_option('auditor')
        form.get_by_label('Reason', exact=True).fill('Fictional audit cover')
        save_dialog(form, 'Give role')
        assert 'auditor' in h.get('staff', '/api/me')['roles']
    finally:
        context.close()


@pytest.mark.parametrize('gone', ['revoked', 'disabled', 'pending'])
def test_an_admin_never_gives_developer(browser, live_server, h, gone):
    # No live Developer (revoked, or held only by a disabled account or an invitation not yet accepted): an admin is still
    # not offered Developer, and a direct grant is refused. Only the system owner restores Developer, outside the app.
    developer(h)
    admin_id = h.seed['identities']['admin']
    with transaction(h.app.state.engine, write=True) as db:
        for binding in db.scalars(select(m.RoleBinding).where(m.RoleBinding.role.in_(SYSTEM_ROLES))):
            if gone == 'revoked' or binding.identity_id == admin_id:
                binding.revoked = True
            elif gone == 'disabled':
                db.get(m.Identity, binding.identity_id).disabled = True
            else:
                db.add(m.Account(identity_id=binding.identity_id, username=f'pending{binding.identity_id[:8]}',
                                 email='pending@example.test', accepted_at=None))
    staff = next(i for i in h.get('admin', '/api/access')['identities'] if i['id'] == h.seed['identities']['staff'])['display_name']
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'access/people')
        page.get_by_role('button', name=f'More actions for {staff}', exact=True).first.click()
        page.get_by_role('menuitem', name='Give a role', exact=True).click()
        options = page.get_by_role('dialog').get_by_label('Role', exact=True).locator('option')
        expect(options.filter(has_text='Coordinator')).to_be_enabled()
        for role in ('Developer', 'Auditor'):
            expect(options.filter(has_text=role)).to_be_disabled()  # shown with why (maintenance), never given
    finally:
        context.close()
    denied = h.command('admin', 'role.grant', {'identity_id': admin_id, 'role': 'operator', 'starts_on': '2020-01-01',
                                               'reason': 'Fictional first Developer'}, status=403)
    assert denied['error']['code'] == 'SYSTEM_ROLE_GRANT'
    assert 'operator' not in h.get('admin', '/api/me')['roles']


@pytest.mark.parametrize('role', ['coordinator', 'approver', 'programme_lead', 'auditor'])
def test_review_routes_reachable_without_the_admin_tabs(browser, live_server, h, role):
    if role == 'programme_lead':
        identity(h, role, ['programme_lead'])
    context, page = sign_in(browser, live_server, role, viewport=PHONE)
    try:
        page.emulate_media(color_scheme='dark')
        navigate(page, 'access')
        expect(page).to_have_url(re.compile(r'#access/routes$'))
        expect(page.get_by_role('tablist', name='Access & approvals')).to_have_count(0)  # one tab: no strip
        expect(page.get_by_role('tabpanel')).to_have_count(0)
        expect(page.locator('.route-default')).to_contain_text('Built-in independent approval')
        expect(page.locator('.app-footer')).not_to_contain_text('Built-in independent approval')
        # Its header moved into the top bar (later sections keep theirs).
        expect(page.locator('#main .section-head h2').filter(has_text='Effective review routes')).to_have_count(0)
        no_page_overflow(page)
    finally:
        context.close()


@pytest.mark.parametrize('role, tabs', [('admin', ['Imports', 'Bulk editor']), ('approver', []), ('auditor', ['Bulk editor', 'Operations', 'Audit log', 'Feedback']),
                                        ('coordinator', ['Imports', 'Bulk editor']), ('credentialer', ['Imports', 'Bulk editor']),
                                        ('operator', ['Operations', 'Audit log', 'Feedback']), ('programme_lead', [])])
def test_system_tabs_lift_their_own_status_into_the_footer(browser, live_server, h, role, tabs):
    if role == 'programme_lead':
        identity(h, role, [role])
    for viewport in (None, PHONE):
        context, page = sign_in(browser, live_server, role, viewport=viewport)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        try:
            for scheme in ('light', 'dark'):
                page.emulate_media(color_scheme=scheme)
                navigate(page, 'system')
                strip = page.get_by_role('tablist', name='System')
                expect(strip.get_by_role('tab')).to_have_count(len(tabs))
                if role in {'admin', 'approver', 'coordinator', 'credentialer', 'programme_lead', 'auditor'}:
                    navigate(page, 'system/bulk')
                    expect(page.locator('#main')).to_have_attribute('data-loaded-tab', 'bulk')
                    if viewport:
                        expect(page.get_by_role('heading', name='Best on a desktop', exact=True)).to_be_visible()
                    else:
                        expect(page.get_by_role('button', name=re.compile('^Record type: '))).to_be_visible()
                keys = [t for t in ('imports', 'operations', 'audit') if role in {'imports': {'admin', 'coordinator', 'credentialer'}, 'operations': {'auditor', 'operator'}, 'audit': {'auditor', 'operator'}}[t]]
                for key in keys:
                    navigate(page, 'system/' + key)
                    status = page.locator('.app-footer .page-status')
                    expect(status).to_contain_text({'imports': re.compile('review'), 'operations': re.compile('Worker'), 'audit': re.compile('change')}[key])
                    no_page_overflow(page)
            assert not errors, errors
        finally:
            context.close()


def test_a_configuration_change_says_what_it_made_in_the_toast_and_the_audit_log(browser, live_server, h):
    context, page = sign_in(browser, live_server, developer(h))
    try:
        navigate(page, 'configuration')
        page.get_by_role('navigation', name='Configuration sections').get_by_role('button', name='Location types', exact=True).click()
        page.locator('.config-pane .section-head').get_by_role('button', name='New location type').click()
        form = page.get_by_role('dialog')
        form.get_by_label('Name', exact=True).fill('Procedure room')
        save_dialog(form)
        expect(page.locator('.toast').filter(has_text='Procedure room created. Published rotas stay as they were.')).to_be_visible()
        navigate(page, 'system/audit')
        expect(page.locator('.audit-row').first).to_contain_text('added a location type “Procedure room”')
    finally:
        context.close()


def test_credentialer_imports_header_moves_into_the_top_bar(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'credentialer')
    try:
        navigate(page, 'system/imports')
        actions = page.locator('.topbar .page-actions')
        expect(actions.get_by_role('button', name='Upload staff CSV', exact=True)).to_be_visible()
        expect(page.locator('#main .section-head')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('role,quota_state', [('coordinator', 'populated'), ('staff', 'populated')],
                         ids=['coordinator-populated', 'staff-populated'])
def test_all_export_buttons_download_selected_publication_contents(browser, live_server, h, role, quota_state):
    mine = h.get('staff', '/api/me')['person_id']
    other = next(p for p in h.seed['people'] if p != mine)
    quota(h, scope={} if quota_state == 'populated' else {'person_id': other}, comparison='target', amount=2, severity='advisory')
    publications = two_versions(h, people=[mine, other])
    selected = next(p for p in publications if p['sequence'] == 1)
    expected = h.get(role, '/api/reports/' + selected['id'])
    assert bool(expected['quotas']['rows']) == (quota_state == 'populated')
    people = {p['id']: p['display_name'] for p in h.get('admin', '/api/catalogues')['people']}
    expected_ids = {mine, other} if role == 'coordinator' else {mine}
    assert {r['person_id'] for r in expected['rows']} == expected_ids
    context, page = sign_in(browser, live_server, role, viewport=PHONE if role == 'staff' else None)
    try:
        navigate(page, 'insights/workload')
        source(page).select_option(selected['id'])
        expect(source(page).locator('option:checked')).to_contain_text('Fictional October rota')
        page.get_by_role('tab', name='Exports', exact=True).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-tab', 'exports')
        expect(source(page)).to_have_value(selected['id'])
        person = page.locator('#main .export-row').filter(has_text='Personal calendar').get_by_label('Person', exact=True)
        if role == 'coordinator':
            person.select_option(mine)
        else:
            expect(person).to_have_count(0)
        files = {}
        downloads = [('Workload CSV', 'scheduled-workload.csv'), ('Duties CSV', 'published-assignments.csv'),
                     ('Commitments CSV', 'published-commitments.csv'), ('Report JSON', 'publication-report.json'),
                     ('Personal calendar', 'personal-rota.ics')]
        if role == 'staff':  # I-20: staff get their own calendar only, not the department's files
            expect(page.locator('#main .export-row strong')).to_have_text(['Personal calendar'])
            downloads = downloads[-1:]
        for label, filename in downloads:
            with page.expect_download() as pending:
                page.get_by_role('button', name='Download ' + label, exact=True).click()
            downloaded = pending.value
            assert downloaded.failure() is None
            assert downloaded.suggested_filename == filename
            files[label] = Path(downloaded.path()).read_text(encoding='utf-8-sig')

        if role == 'coordinator':
            workload = list(csv.reader(io.StringIO(files['Workload CSV'])))
            metadata = dict(zip(workload[0][::2], workload[0][1::2]))
            assert metadata['publication_id'] == selected['id'] and metadata['sequence'] == '1'
            rows = [dict(zip(workload[1], values)) for values in workload[2:]]
            assert {r['person_id'] for r in rows} == expected_ids
            assert {r['person_id']: (int(r['assignments']), float(r['scheduled_hours'])) for r in rows} == {
                r['person_id']: (r['assignments'], r['scheduled_hours']) for r in expected['rows']}
            assignments = list(csv.DictReader(io.StringIO(files['Duties CSV'])))
            assert len(assignments) == len(expected_ids)
            assert {r['person'] for r in assignments} == {people[p] for p in expected_ids}
            assert {r['publication_id'] for r in assignments} == {selected['id']}
            assert {r['start'][:10] for r in assignments} == {'2026-10-04'}
            commitments = csv.DictReader(io.StringIO(files['Commitments CSV']))
            assert {'publication_id', 'person_id', 'amount', 'actual'} <= set(commitments.fieldnames)
            commitments = list(commitments)
            assert len(commitments) == len(expected['quotas']['rows'])
            assert {r['publication_id'] for r in commitments} == ({selected['id']} if commitments else set())
            assert {(r['policy_id'], r['person_id']): (float(r['amount']), float(r['actual'])) for r in commitments} == {
                (r['policy_id'], r['person_id']): (r['amount'], r['actual']) for r in expected['quotas']['rows']}
            document = json.loads(files['Report JSON'])
            assert document['publication_id'] == selected['id'] and document['publication_sequence'] == 1
            assert document['rows'] == expected['rows']
        calendar = files['Personal calendar'].replace('\n ', '')  # unfold RFC 5545 continuation lines
        assert calendar.startswith('BEGIN:VCALENDAR') and calendar.rstrip().endswith('END:VCALENDAR')
        assert 'X-PUBLICATION-ID:' + selected['id'] in calendar
        assert calendar.count('BEGIN:VEVENT') == calendar.count('END:VEVENT') == 1
        assert 'DTSTART:20261004T050000Z' in calendar and 'DTEND:20261004T090000Z' in calendar
        uid = next(line for line in calendar.splitlines() if line.startswith('UID:'))
        assert selected['id'] in uid and mine in uid and other not in uid
        if role == 'staff':
            for content in files.values():
                assert other not in content and people[other] not in content
    finally:
        context.close()

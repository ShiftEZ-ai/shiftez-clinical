"""Configuration IA: every configuration-like setting lives under Configuration (department settings, sign-in sessions,
auto-schedule & scoring), Access is only about people and roles, and the auto-schedule options are one component used
both for the department defaults and for a single run in the planner."""
import re
import time

import pytest
from playwright.sync_api import expect

from .governance_support import identity
from .test_browser import auto_setup, browser, live_server, sign_in, navigate, open_config, open_scheduling  # noqa: F401
from .test_browser_audit_date_units import stored_theme
from .test_browser_cp_sat import cp_server  # noqa: F401 (one fixture: server and in-test worker both on CP-SAT)

pytestmark = pytest.mark.browser

GROUPS = {'Scheduling': ['Duty types', 'Recurring sessions', 'Opening hours', 'Resource pools'],
          'Places & services': ['Locations', 'Location types', 'Services'],
          'People': ['Ranks', 'Skills & capabilities', 'Names & codes'],
          'Department': ['Tags', 'Department settings', 'Auto-schedule & scoring', 'Announcements']}


def no_overflow(page):
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')


@pytest.fixture
def legacy_session_server(h, request, idle):
    h.server_env = {'ROTA_SESSION_IDLE_MINUTES': str(idle), 'ROTA_SESSION_HOURS': '4'}
    department = h.get('admin', '/api/me')['department']
    h.command('admin', 'department.sessions', {'idle_minutes': None, 'max_hours': None},
              department['id'], department['revision'])
    return request.getfixturevalue('live_server')


@pytest.mark.parametrize('idle,width,theme', [(1, 1440, 'light'), (2, 390, 'dark'),
                                             (3, 1440, 'dark'), (4, 390, 'light')])
def test_inherited_short_timeout_survives_unchanged_save_edits_and_undo(browser, legacy_session_server, idle, width, theme):
    context, page = sign_in(browser, legacy_session_server, 'admin', {'width': width, 'height': 900})
    idle_text = f'{idle} minute' + ('' if idle == 1 else 's')
    try:
        stored_theme(page, theme)
        open_config(page, 'Department settings')
        pane = page.locator('.config-pane')
        expect(pane).to_contain_text(f'Signed out after {idle_text} without activity, and 4 hours after signing in')
        pane.get_by_role('button', name='Edit session settings', exact=True).click()
        dialog = page.get_by_role('dialog', name='Session settings', exact=True)
        field = dialog.get_by_label('Sign out after inactivity (minutes)', exact=True)
        expect(field).to_have_value(str(idle))
        expect(dialog).to_contain_text(f'Inherited deployment setting: {idle_text}. Keep it unchanged or choose at least 5 minutes.')
        assert field.evaluate('(input) => input.validity.valid')
        field.fill(str(4 if idle != 4 else 3))
        assert field.evaluate('(input) => input.validationMessage') == 'New inactivity limits must be at least 5 minutes.'
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(dialog).to_be_visible()
        field.fill(str(idle))
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(dialog).to_have_count(0)
        saved = context.request.get(legacy_session_server + '/api/me').json()['department']
        assert saved['session_idle_minutes'] is None and saved['session_max_hours'] is None
        pane.get_by_role('button', name='Edit session settings', exact=True).click()
        dialog.get_by_label('Longest session (hours)', exact=True).fill('2')
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(pane).to_contain_text(f'Signed out after {idle_text} without activity, and 2 hours after signing in')
        page.locator('.toast').filter(has_text='Session settings saved').first.get_by_role('button', name='Undo', exact=True).click()
        expect(pane).to_contain_text(f'Signed out after {idle_text} without activity, and 4 hours after signing in')
        pane.get_by_role('button', name='Edit session settings', exact=True).click()
        field.fill('5')
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(pane).to_contain_text('Signed out after 5 minutes without activity')
        page.locator('.toast').filter(has_text='Session settings saved').first.get_by_role('button', name='Undo', exact=True).click()
        expect(pane).to_contain_text(f'Signed out after {idle_text} without activity')
        page.reload()
        open_config(page, 'Department settings')
        expect(pane).to_contain_text(f'Signed out after {idle_text} without activity')
        saved = context.request.get(legacy_session_server + '/api/me').json()['department']
        assert saved['session_idle_minutes'] is None and saved['session_max_hours'] is None
        no_overflow(page)
    finally:
        context.close()


def test_configuration_groups_every_setting_and_access_keeps_only_people_and_roles(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        navigate(page, 'configuration')
        nav = page.get_by_role('navigation', name='Configuration sections')
        for group, sections in GROUPS.items():
            box = nav.locator('.config-group').filter(has=page.locator('.config-group-label', has_text=group))
            for section in sections:
                expect(box.get_by_role('button', name=section, exact=True)).to_be_visible()
        # Record lists carry a count; settings pages do not.
        expect(nav.get_by_role('button', name='Duty types', exact=True).locator('.nav-count-muted')).to_have_count(1)
        for section in ('Department settings', 'Auto-schedule & scoring'):
            expect(nav.get_by_role('button', name=section, exact=True).locator('.nav-count-muted')).to_have_count(0)
        open_config(page, 'Department settings')
        pane = page.locator('.config-pane')
        expect(pane.get_by_label('Scheduling timezone', exact=True)).to_have_value('Asia/Riyadh')
        expect(pane.get_by_role('heading', name='Sign-in sessions', exact=True)).to_be_visible()  # sign-in session limits
        expect(pane.get_by_role('region', name='Unsaved changes')).to_be_hidden()  # nothing changed: no save control
        pane.get_by_role('button', name='Edit session settings', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Sign out after inactivity (minutes)', exact=True).fill('45')
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(page.get_by_text('Session settings saved.', exact=True)).to_be_visible()
        expect(pane.get_by_text('Signed out after 45 minutes without activity', exact=False)).to_be_visible()
        # No upper limit beyond the server's technical ceiling: the form takes its bounds from the server, not a copy.
        pane.get_by_role('button', name='Edit session settings', exact=True).click()
        dialog = page.get_by_role('dialog')
        expect(dialog.get_by_text('At least 5 minutes.', exact=False)).to_be_visible()
        dialog.get_by_label('Sign out after inactivity (minutes)', exact=True).fill('1440')
        dialog.get_by_label('Longest session (hours)', exact=True).fill('720')
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(pane.get_by_text('Signed out after 24 hours without activity, and 720 hours after signing in', exact=False)).to_be_visible()
        # Access: people and role history only; the settings moved out.
        navigate(page,'access/people')
        main = page.locator('#main')
        expect(main.get_by_text('Department settings', exact=True)).to_have_count(0)
        expect(main.get_by_role('button', name='Edit department settings')).to_have_count(0)
        expect(main.get_by_role('heading', name='Sign-in sessions', exact=True)).to_have_count(0)
        expect(main.locator('details')).to_have_count(0)
        people = main.get_by_role('tab', name='People & roles', exact=True)
        expect(people).to_have_attribute('aria-selected', 'true')
        expect(people.locator('.tab-count')).to_have_text(re.compile(r'^\d+$'))  # the count sits beside the name
        expect(main.get_by_role('button', name='Give a role', exact=True)).to_have_count(0)  # header button replaced by the row action
        main.get_by_role('tab', name=re.compile(r'^Role history')).click()
        expect(main.get_by_role('tabpanel').get_by_role('row').filter(has_text='Admin').first).to_be_visible()
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')])
def test_session_unlimited_is_explicit_warned_persisted_and_undoable(browser, live_server, h, width, theme):
    context, page = sign_in(browser, live_server, 'admin', {'width': width, 'height': 900})
    try:
        stored_theme(page, theme)
        open_config(page, 'Department settings')
        pane = page.locator('.config-pane')
        expect(pane).to_contain_text('Signed out after 8 hours without activity, and 24 hours after signing in')
        pane.get_by_role('button', name='Edit session settings', exact=True).click()
        dialog = page.get_by_role('dialog', name='Session settings', exact=True)
        idle = dialog.get_by_label('Sign out after inactivity (minutes)', exact=True)
        maximum = dialog.get_by_label('Longest session (hours)', exact=True)
        expect(idle).to_have_value('480')
        expect(maximum).to_have_value('24')
        expect(dialog.get_by_label('No inactivity limit', exact=True)).not_to_be_checked()
        maximum_toggle = dialog.get_by_label('No maximum session length', exact=True)
        expect(maximum_toggle).not_to_be_checked()
        expect(dialog.get_by_text('Unlimited sessions increase the risk', exact=False)).to_be_hidden()
        maximum_toggle.check()
        expect(maximum).to_be_disabled()
        expect(idle).to_be_enabled()
        expect(dialog.get_by_text('Unlimited sessions increase the risk', exact=False)).to_be_visible()
        expect(dialog).to_contain_text('unattended device or a stolen session token')
        no_overflow(page)
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(pane).to_contain_text('Signed out after 8 hours without activity; no maximum session length.')
        page.locator('.toast').filter(has_text='Session settings saved').get_by_role('button', name='Undo', exact=True).click()
        expect(pane).to_contain_text('Signed out after 8 hours without activity, and 24 hours after signing in')
        pane.get_by_role('button', name='Edit session settings', exact=True).click()
        expect(dialog.get_by_label('No maximum session length', exact=True)).not_to_be_checked()
        dialog.get_by_label('No inactivity limit', exact=True).check()
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(pane).to_contain_text('No inactivity limit; signed out 24 hours after signing in at the latest.')
        pane.get_by_role('button', name='Edit session settings', exact=True).click()
        expect(dialog.get_by_label('No inactivity limit', exact=True)).to_be_checked()
        dialog.get_by_label('No maximum session length', exact=True).check()
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(pane).to_contain_text('No automatic sign-out for inactivity or session length.')
        expect(pane.get_by_text('Unlimited sessions increase the risk', exact=False)).to_be_visible()
        no_overflow(page)
        page.reload()
        open_config(page, 'Department settings')
        pane.get_by_role('button', name='Edit session settings', exact=True).click()
        expect(dialog.get_by_label('No inactivity limit', exact=True)).to_be_checked()
        expect(dialog.get_by_label('No maximum session length', exact=True)).to_be_checked()
        expect(idle).to_be_disabled()
        expect(maximum).to_be_disabled()
        dialog.get_by_label('No inactivity limit', exact=True).uncheck()
        dialog.get_by_label('No maximum session length', exact=True).uncheck()
        idle.fill('5')
        maximum.fill('1')
        no_overflow(page)
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(pane).to_contain_text('Signed out after 5 minutes without activity, and 1 hour after signing in')
        expect(pane.get_by_text('Unlimited sessions increase the risk', exact=False)).to_have_count(0)
        no_overflow(page)
    finally:
        context.close()


@pytest.mark.parametrize('inherited,width,theme', [
    pytest.param(False, 1440, 'light', id='finite-desktop'),
    pytest.param(True, 390, 'dark', id='inherited-phone'),
])
def test_disabled_session_limits_ignore_invalid_drafts_and_preserve_saved_values(browser, request, h, inherited, width, theme):
    if inherited:
        h.server_env = {'ROTA_SESSION_IDLE_MINUTES': '2', 'ROTA_SESSION_HOURS': '4'}
        department = h.get('admin', '/api/me')['department']
        h.command('admin', 'department.sessions', {'idle_minutes': None, 'max_hours': None},
                  department['id'], department['revision'])
    server = request.getfixturevalue('live_server')
    context, page = sign_in(browser, server, 'admin', {'width': width, 'height': 900})
    try:
        stored_theme(page, theme)
        before = context.request.get(server + '/api/me').json()['department']
        open_config(page, 'Department settings')
        pane = page.locator('.config-pane')
        pane.get_by_role('button', name='Edit session settings', exact=True).click()
        dialog = page.get_by_role('dialog', name='Session settings', exact=True)
        idle = dialog.get_by_label('Sign out after inactivity (minutes)', exact=True)
        maximum = dialog.get_by_label('Longest session (hours)', exact=True)
        idle.fill('3' if inherited else '4')
        maximum.fill('0')
        dialog.get_by_label('No inactivity limit', exact=True).check()
        dialog.get_by_label('No maximum session length', exact=True).check()
        expect(idle).to_be_disabled()
        expect(maximum).to_be_disabled()
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(dialog).to_have_count(0)
        expect(pane).to_contain_text('No automatic sign-out for inactivity or session length.')
        saved = context.request.get(server + '/api/me').json()['department']
        assert saved['session_idle_unlimited'] is True and saved['session_max_unlimited'] is True
        assert saved['session_idle_minutes'] == before['session_idle_minutes']
        assert saved['session_max_hours'] == before['session_max_hours']
        page.reload()
        open_config(page, 'Department settings')
        pane.get_by_role('button', name='Edit session settings', exact=True).click()
        expect(idle).to_have_value('2' if inherited else '480')
        expect(maximum).to_have_value('4' if inherited else '24')
        dialog.get_by_label('No inactivity limit', exact=True).uncheck()
        dialog.get_by_label('No maximum session length', exact=True).uncheck()
        idle.fill('3' if inherited else '4')
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(dialog).to_be_visible()
        assert idle.evaluate('(input) => input.validity.valid') is False
        idle.fill('2' if inherited else '480')
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(dialog).to_have_count(0)
        restored = context.request.get(server + '/api/me').json()['department']
        assert restored['session_idle_unlimited'] is False and restored['session_max_unlimited'] is False
        assert restored['session_idle_minutes'] == before['session_idle_minutes']
        assert restored['session_max_hours'] == before['session_max_hours']
        no_overflow(page)
    finally:
        context.close()


def test_give_a_role_from_the_person_row(browser, live_server, h):
    access = h.get('admin', '/api/access')
    staff_id = h.get('staff', '/api/me')['identity_id']
    target = next(i for i in access['identities'] if i['id'] == staff_id)
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page,'access/people')
        row = page.get_by_role('row').filter(has_text=target['display_name']).first
        row.get_by_role('button', name=re.compile('^(More actions|Actions)')).click()
        page.get_by_role('menuitem', name='Give a role', exact=True).click()
        dialog = page.get_by_role('dialog')
        expect(dialog).to_contain_text('Give a role to ' + target['display_name'])
        dialog.get_by_label('Role', exact=True).select_option('credentialer')
        dialog.get_by_label('Reason', exact=True).fill('Fictional credential cover')
        dialog.get_by_role('button', name='Give role', exact=True).click()
        expect(page.get_by_role('dialog')).to_have_count(0)
        roles = [b['role'] for b in h.get('admin', '/api/access')['bindings'] if b['identity_id'] == target['id'] and not b['revoked']]
        assert 'credentialer' in roles
    finally:
        context.close()


def test_command_menu_jumps_to_configuration_sections(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page,'home')
        page.keyboard.press('Control+k')
        page.keyboard.type('scoring weights')
        page.get_by_role('option', name=re.compile('Auto-schedule & scoring')).first.click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'configuration')
        expect(page.locator('.config-pane .section-head h2').first).to_have_text('Auto-schedule & scoring')
        page.keyboard.press('Control+k')
        page.keyboard.type('timezone')
        page.get_by_role('option', name=re.compile('Department settings')).first.click()
        expect(page.locator('.config-pane .section-head h2').first).to_have_text('Department settings')
    finally:
        context.close()


@pytest.mark.parametrize('role', ['coordinator', 'approver', 'credentialer', 'programme_lead', 'auditor', 'staff'])
def test_configuration_settings_are_admin_only(browser, live_server, h, role):
    """Only admins change settings. A coordinator's Configuration is the setup guide, Scheduling groups and Tags (tags of
    any kind for #tag lines); Access & approvals is open to those who use review routes."""
    if role == 'programme_lead':
        identity(h, 'programme_lead', ['programme_lead'])
    context, page = sign_in(browser, live_server, role)
    try:
        expect(page.locator('#department-nav [data-route=configuration]')).to_have_count(1 if role == 'coordinator' else 0)
        expect(page.locator('#department-nav [data-route=access]')).to_have_count(0 if role in ('credentialer', 'staff') else 1)
        if role == 'coordinator':
            navigate(page, 'configuration/setup')
            expect(page.locator('.setup-steps')).to_be_visible()
            nav = page.get_by_role('navigation', name='Configuration sections')
            # Setup guide, Scheduling groups (D19, 6b6091020: who schedules whom, read by the Rota Coordinators), Tags, and
            # Auto-schedule & scoring for its Score review (their rule proposals; its settings stay an admin's)
            expect(nav.get_by_role('button')).to_have_count(4)
            expect(nav.get_by_role('button', name='Scheduling groups', exact=True)).to_be_visible()
            nav.get_by_role('button', name='Tags', exact=True).click()
            expect(page.locator('.config-pane').get_by_role('button', name='New tag', exact=True)).to_be_visible()  # header, or the empty state (C-03)
        page.keyboard.press('Control+k')
        page.keyboard.type('auto-schedule & scoring')
        expect(page.get_by_role('option', name=re.compile('Auto-schedule & scoring'))).to_have_count(1 if role == 'coordinator' else 0)
    finally:
        context.close()


def test_run_options_start_from_department_defaults_and_do_not_change_them(browser, cp_server, h):
    h.shift(h.workspace('Options rota'), people=[])
    me = h.get('admin', '/api/me')
    dept, org = me['department'], me['organisation']
    factory = h.get('admin', '/api/policies/capabilities')['generation_defaults']
    defaults = {**factory, 'scoring': {**factory['scoring'], 'weights': {**factory['scoring']['weights'], 'coverage': 60}}}
    h.command('admin', 'department.settings', {'department_name': dept['name'], 'department_revision': dept['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
        'generation_defaults': defaults, 'reason': 'Fictional department defaults'}, dept['id'], org['revision'])
    context, page = sign_in(browser, cp_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        expect(panel.locator('.auto-body')).to_have_attribute('data-state', 'setup')
        expect(panel.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_visible()
        expect(panel.get_by_role('button', name='Check the whole rota', exact=True)).to_be_visible()
        expect(panel.locator('details')).to_have_count(0)
        auto_setup(panel, fields=False)
        # A run picks one of four presets and its own search time (83ae3d14b names that field "Search time", as
        # Configuration does); department scoring, strengths and wishes stay in Configuration.
        for label in ('Coverage weight', 'Catch-up for declined wishes (up to ×)', 'Low strength (×)'):
            expect(panel.get_by_label(label, exact=True)).to_have_count(0)
        expect(panel.get_by_label('Search time', exact=True)).to_be_visible()
        assert panel.get_by_role('radiogroup', name='Priorities', exact=True).evaluate("""group =>
            [...group.querySelectorAll('button')].every(button => button.scrollWidth <= button.clientWidth + 1)""")
        panel.get_by_role('radio', name='Fairness first', exact=True).click()
        # The chosen step and preset survive leaving and reopening the planner.
        navigate(page,'home')
        navigate(page, 'planner')
        open_scheduling(page)
        expect(panel.locator('.auto-body')).to_have_attribute('data-state', 'setup')
        expect(panel.get_by_role('radio', name='Fairness first')).to_be_checked()
        assert h.get('admin', '/api/me')['department']['generation_defaults']['scoring']['weights']['coverage'] == 60
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_settings_pages_fit_a_phone(browser, live_server, h, scheme):
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 390, 'height': 844})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        page.emulate_media(color_scheme=scheme)
        for section in ('Auto-schedule & scoring', 'Department settings'):
            open_config(page, section)
            no_overflow(page)
        navigate(page,'access/people')
        no_overflow(page)
        assert not errors, errors
    finally:
        context.close()


@pytest.fixture
def department_scale_configuration(browser, live_server, h):
    for i in range(120):
        h.configure('people', {'display_name': f'Scale person {i:03}'})
    context, page = sign_in(browser, live_server, 'admin')
    try:
        timings = {}
        for section in ('Names & codes', 'Auto-schedule & scoring', 'Department settings', 'Duty types'):
            navigate(page, 'configuration')
            started = time.perf_counter()
            page.get_by_role('navigation', name='Configuration sections').get_by_role('button', name=section, exact=True).click()
            expect(page.locator('.config-pane .section-head h2').first).to_have_text(section)
            timings[section] = round(time.perf_counter() - started, 2)
        print('Configuration section open (s):', timings)
        yield page, timings
    finally:
        context.close()


def test_configuration_opens_quickly_at_department_scale(department_scale_configuration):
    page, _ = department_scale_configuration
    expect(page.locator('.names-table')).to_have_count(0)


@pytest.mark.wall_benchmark
def test_department_scale_configuration_cpu_budget(department_scale_configuration):
    _, timings = department_scale_configuration
    assert max(timings.values()) < 10, timings  # generous: shared CI hosts; typical well under 1 s

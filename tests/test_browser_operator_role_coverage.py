"""Role/surface contracts and representative persisted actions, not an all-buttons claim.

Initial scheduling data and unread items are explicit API/database fixtures. Actions
under test use the real UI; each persisted outcome is checked independently and
after an actual page reload. No transport mocks or deployment/demo access.
"""
import re
import uuid

import pytest
from playwright.sync_api import expect

from packages.contracts.schemas import ROLE_NAMES, SYSTEM_ROLES
from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_browser import SMOKE_PATHS, expect_loaded, browser, live_server, sign_in, navigate, save_dialog, pick, holding  # noqa: F401
from .test_browser import whole_list

pytestmark = pytest.mark.browser

# Authorization expectation, independently frozen from the eight-role product
# contract. Admin receives all effective roles; it is not a restricted ninth role.
PAGES = {
    'home': 'Home', 'schedule': 'Schedule',
    'requests': 'Requests', 'people': 'People', 'programmes': 'Programmes', 'insights': 'Insights',
    'rules': 'Rules', 'configuration': 'Configuration', 'access': 'Access & approvals', 'system': 'System',
    'inbox': 'Inbox',
}
# Pages each role may open (the route boundary). The sidebar shows the same pages except NAV_HIDDEN.
ROLE_PAGES = {
    'admin': set(PAGES),
    'coordinator': set('home schedule requests people programmes insights rules configuration access system inbox'.split()),
    'approver': set('home schedule requests people insights rules access system inbox'.split()),
    'credentialer': set('home people system inbox'.split()),
    'staff': set('home schedule requests people insights inbox'.split()),
    'operator': set('home inbox configuration access system'.split()),
    # Auditors and programme leads read pending person facts in Requests (read-only; maintenance).
    'auditor': set('home schedule requests people insights access system inbox'.split()),
    'programme_lead': set('home schedule requests people programmes insights access system inbox'.split()),  # System › Bulk editor
}
# Staff keep four pages in the sidebar (Insights is a link on Home); a coordinator's Configuration holds only the setup
# guide and leaves the sidebar once setup is complete, so its sidebar entry depends on the department's data.
NAV_HIDDEN = {('staff', 'insights'), ('coordinator', 'configuration')}
# Tabs of merged pages, in order; a page with one tab for a role shows no tab strip (empty list).
ROLE_TABS = {
    'admin': {'insights': ['Fairness', 'Workload', 'Exports'], 'access': ['People & roles', 'Review routes', 'Role history'], 'system': ['Imports', 'Bulk editor']},
    'coordinator': {'insights': ['Fairness', 'Workload', 'Exports'], 'access': [], 'system': ['Imports', 'Bulk editor']},
    'approver': {'insights': ['Fairness', 'Workload', 'Exports'], 'access': []},
    'auditor': {'insights': ['Fairness', 'Workload', 'Exports'], 'access': [], 'system': ['Bulk editor', 'Operations', 'Audit log', 'Feedback']},
    'credentialer': {'system': ['Imports', 'Bulk editor']},
    'operator': {'access': [], 'system': ['Operations', 'Audit log', 'Feedback']},
    'programme_lead': {'insights': [], 'access': [], 'system': []},
}


@pytest.fixture
def all_roles(h):
    assert set(ROLE_PAGES) == set(ROLE_NAMES)
    identity(h, 'programme_lead', ['programme_lead'])
    return h


def test_role_surface_keysets_match_authoritative_roles():
    assert set(ROLE_PAGES) == set(SMOKE_PATHS) == set(ROLE_NAMES)


def reload_view(page, key):
    page.reload()
    expect_loaded(page, key)
    expect(page.locator('#main [role=alert]')).to_have_count(0)


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_all_role_routes_forbidden_controls_and_common_help(browser, live_server, all_roles, role):
    h = all_roles
    h.publish(h.shift(h.workspace('Fictional issued role-coverage source'))['workspace'])
    h.shift(h.workspace('Fictional editable role-coverage source'))
    # An admin holds every department role; Developer (operator) and Auditor are granted separately.
    assert set(h.get(role, '/api/me')['roles']) == (set(ROLE_PAGES) - {*SYSTEM_ROLES, 'auditor'} if role == 'admin' else {role})
    context, page = sign_in(browser, live_server, role)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        # Navigate through the real departmental navigation, then force each
        # forbidden hash to verify that hiding a menu is not the only boundary.
        for key, label in PAGES.items():
            nav = page.get_by_role('navigation', name='Department pages')
            control = nav.get_by_role('button', name=label, exact=True)
            if key == 'inbox':
                # Every role reaches its inbox from the bell in the top bar, not the sidebar.
                expect(control).to_have_count(0)
                page.locator('.topbar .bell').click()
                page.get_by_role('dialog', name='Inbox').get_by_role('button', name='Open inbox', exact=True).click()
                expect(page.locator('#main')).to_have_attribute('data-loaded-view', key, timeout=20000)
                continue
            if (role, key) in NAV_HIDDEN:
                navigate(page, key)
                expect(page.get_by_role('heading', name='Restricted page', exact=True)).to_have_count(0)
                continue
            if key in ROLE_PAGES[role]:
                expect(control).to_have_count(1)
                control.click()
                expect(page.locator('#main')).to_have_attribute('data-loaded-view', key, timeout=20000)
                expect(page.locator('#main [role=alert]')).to_have_count(0)
                expect(page.get_by_role('heading', name='Restricted page', exact=True)).to_have_count(0)
                if key == 'schedule' and role not in {'admin', 'coordinator'}:
                    for action in ('New rota', 'Add session', 'Auto-schedule', 'Publish…', 'Submit for review…'):
                        expect(page.get_by_role('button', name=action, exact=True)).to_have_count(0)
                if key == 'requests' and role in {'auditor', 'programme_lead'}:
                    for action in ('New request', 'Record leave', 'Approve', 'Decline'):
                        expect(page.get_by_role('button', name=action, exact=True)).to_have_count(0)
                if key == 'people' and role in {'credentialer', 'programme_lead'}:
                    expect(page.get_by_role('button', name='New person', exact=True)).to_have_count(0)
                if key == 'people' and role == 'programme_lead':
                    expect(page.get_by_role('button', name='Authorise eligibility', exact=True)).to_have_count(0)
                if key == 'configuration' and role == 'operator':
                    sections = page.locator('.config-nav [data-section]')
                    permitted = {'integrations', 'branding'}
                    if h.get(role, '/api/me')['department']['score_check_enabled']:
                        permitted.add('ai_provider')
                    expect(sections).to_have_count(len(permitted))
                    assert set(sections.evaluate_all('ns => ns.map(n => n.dataset.section)')) == permitted
                    page.evaluate("location.hash='configuration/department'")
                    expect(page.locator('#main')).to_have_attribute('data-loaded-tab', 'integrations')
                    expect(page).to_have_url(re.compile(r'#configuration/integrations$'))
                    expect(page.locator('#main .config-pane[data-kind=department]')).to_have_count(0)
                if key in ROLE_TABS.get(role, {}):
                    # A tab may carry a count beside its name (Access & approvals: People & roles · N).
                    expect(page.get_by_role('tablist', name=label, exact=True).get_by_role('tab')).to_have_text(
                        [re.compile('^' + re.escape(t) + r'\d*$') for t in ROLE_TABS[role][key]])
                if key == 'system' and role == 'auditor':
                    navigate(page, 'system/operations')
                    expect(page.get_by_role('button', name='Retry', exact=True)).to_have_count(0)
            else:
                expect(control).to_have_count(0)
                page.evaluate('key => { location.hash = key; }', key)
                expect(page.get_by_role('heading', name='Restricted page', exact=True)).to_be_visible()
                expect(page.locator('#main')).not_to_have_attribute('data-loaded-view', key)

        navigate(page,'home')
        page.get_by_role('button', name=re.compile(r'^Account menu, ')).click()
        # The account menu holds only account things, each once: no second shortcuts entry, no Inbox or search copies.
        menu = page.get_by_role('dialog', name='Account')
        expect(menu.get_by_role('menuitem', name=re.compile(r'Keyboard shortcuts'))).to_have_count(1)
        expect(menu.get_by_role('menuitem', name=re.compile(r'^(Help|Inbox|Search)'))).to_have_count(0)
        menu.get_by_role('menuitem', name=re.compile(r'Keyboard shortcuts')).click()
        expect(page.get_by_role('dialog', name='Keyboard shortcuts')).to_be_visible()
        expect(page.get_by_role('dialog').get_by_role('table')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.locator('dialog[open]')).to_have_count(0)
        assert not errors
    finally:
        context.close()


def unread_item(db, h, recipient, title):
    event = m.Outbox(department_id=h.seed['department_id'], event_key=str(uuid.uuid4()),
                     kind='announcement', payload={}, state='available_in_app', next_attempt_at=m.now())
    db.add(event)
    db.flush()
    item = m.Notification(outbox_id=event.id, identity_id=recipient, title=title,
                          message='Fictional role coverage notification')
    db.add(item)
    db.flush()
    return item.id


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_every_role_acknowledges_own_inbox_item_and_reload_preserves_it(browser, live_server, all_roles, role):
    h = all_roles
    other = 'staff' if role != 'staff' else 'coordinator'
    with transaction(h.app.state.engine, write=True) as db:
        own = unread_item(db, h, h.seed['identities'][role], 'Own role coverage item')
        foreign = unread_item(db, h, h.seed['identities'][other], 'Other account private item')
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page, 'inbox')
        expect(page.get_by_text('Other account private item', exact=True)).to_have_count(0)
        with page.expect_response(lambda response: response.url.endswith('/api/commands')
                and response.request.method == 'POST'
                and response.request.post_data_json.get('kind') == 'notification.ack'
                and response.request.post_data_json.get('target_id') == own) as acknowledged:
            page.get_by_role('button', name='Mark as read: Own role coverage item', exact=True).click()
        assert acknowledged.value.ok, acknowledged.value.text()
        expect(page.get_by_role('button', name='Mark as read: Own role coverage item', exact=True)).to_have_count(0)
        own_saved = next(n for n in h.get(role, '/api/notifications')['notifications'] if n['id'] == own)
        assert own_saved['read_at'] is not None and own_saved['acknowledged_at'] is not None and own_saved['revision'] == 2
        h.command(role, 'notification.ack', {'reason': 'Forbidden cross-account acknowledgement'}, foreign, 1, status=404)
        other_saved = next(n for n in h.get(other, '/api/notifications')['notifications'] if n['id'] == foreign)
        assert other_saved['read_at'] is None and other_saved['acknowledged_at'] is None and other_saved['revision'] == 1
        reload_view(page, 'inbox')
        expect(page.get_by_text('Own role coverage item', exact=True)).to_be_visible()
        expect(page.locator('.note-item').filter(has_text='Own role coverage item').get_by_label('Read', exact=True)).to_be_visible()
        expect(page.get_by_text('Other account private item', exact=True)).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('role', ['admin', 'coordinator'])
def test_permitted_person_create_persists_and_does_not_grant_authority(browser, live_server, h, role):
    before = h.get('credentialer', '/api/catalogues')
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page, 'people')
        page.get_by_role('button', name='New person', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Display name', exact=True).fill('Fictional created coverage person')
        form.get_by_label('Job title', exact=True).fill('Coverage verification')
        save_dialog(form)
        saved = h.get('credentialer', '/api/catalogues')
        person = next(p for p in saved['people'] if p['display_name'] == 'Fictional created coverage person')
        assert person['title'] == 'Coverage verification'
        assert saved['eligibilities'] == before['eligibilities'] and saved['grants'] == before['grants']
        assert not any(r['person_id'] == person['id'] for r in saved['memberships'])
        reload_view(page, 'people')
        # A created person opens in the detail dock too; the list row is the persisted record.
        expect(page.get_by_label('people table').get_by_text(person['display_name'], exact=True)).to_be_visible()
    finally:
        context.close()


def test_credentialer_authorises_one_service_and_reload_keeps_exact_dates(browser, live_server, h):
    person = h.configure('people', {'display_name': 'Fictional eligibility coverage person'})['record']
    service = h.get('credentialer', '/api/catalogues')['services'][0]
    context, page = sign_in(browser, live_server, 'credentialer')
    try:
        navigate(page, 'people')
        page.get_by_role('button', name='Authorise eligibility', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Person', exact=True).select_option(person['id'])
        pick(form, 'Clinics', [service['name']])  # the form's own word (P-03)
        form.get_by_label('From', exact=True).fill('2026-10-01')
        form.get_by_label('Last day (optional)', exact=True).fill('2026-10-31')
        expect(form.locator('.form-footer button[type=submit]')).to_have_text('Authorise clinics')
        save_dialog(form)  # its own submit, "Authorise clinics" (P-03)
        saved = [r for r in h.get('credentialer', '/api/catalogues')['eligibilities'] if r['person_id'] == person['id']]
        assert len(saved) == 1
        assert (saved[0]['service_id'], saved[0]['starts_on'], saved[0]['ends_on']) == (service['id'], '2026-10-01', '2026-11-01')
        reload_view(page, 'people')
        page.get_by_label('Search people', exact=True).fill(person['display_name'])
        page.get_by_text(person['display_name'], exact=True).click()
        expect(page.get_by_role('region', name=person['display_name'])).to_contain_text(service['name'])
        expect(page.get_by_role('region', name=person['display_name'])).to_contain_text('31 Oct 2026')
    finally:
        context.close()


def test_an_undo_that_keeps_a_record_changed_since_says_so_not_undone(browser, live_server, h):
    """Review maintenance (credential Undo): a skill's Undo comes back "0 removed; 1 changed since and kept" when the grant
    changed after it was made. The message says so as a problem, never "the previous values are back"."""
    person = h.configure('people', {'display_name': 'Fictional undo kept person'})['record']  # holds no skill yet
    skill = h.get('credentialer', '/api/catalogues')['capabilities'][0]
    before = {g['id'] for g in h.get('credentialer', '/api/catalogues')['grants']}
    context, page = sign_in(browser, live_server, 'credentialer')
    try:
        navigate(page, 'people')
        # SW3-D-14 (4e111b9fd): opened from a person's row, the form's person is that row's, fixed.
        page.get_by_label('Search people', exact=True).fill(person['display_name'])
        page.get_by_role('button', name=re.compile('^More actions for ' + re.escape(person['display_name']))).click()
        page.get_by_role('menuitem', name='Grant skill', exact=True).click()
        form = page.get_by_role('dialog', name='Grant skill')
        expect(form.locator('.relationship-person')).to_contain_text(person['display_name'])
        form.get_by_label('Skill', exact=True).select_option(skill['id'])
        form.get_by_label('Evidence you checked', exact=True).fill('Fictional certificate seen')
        # The page refresh after the save is held: the grant's own message owns its Undo meanwhile (a slow refresh
        # used to let the generic "Saved." fallback claim it, run 40287895378).
        toast = page.locator('.toast').filter(has_text='Skill granted.')
        with holding(page) as held:
            held.hold('**/api/me')
            form.locator('.form-footer button[type=submit]').click()  # the dialog closes only once the refresh ends
            expect(toast.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        expect(form).not_to_be_visible()
        expect(page.locator('#toast-tray .toast').get_by_role('button', name='Undo', exact=True)).to_have_count(1)
        [row] = [g for g in h.get('credentialer', '/api/catalogues')['grants'] if g['id'] not in before]
        h.command('credentialer', 'grant.update_dates', {'ends_on': '2031-01-01', 'reason': 'Changed elsewhere'},
                  row['id'], row['revision'])  # changed since: the Undo keeps it
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('.toast.error')).to_contain_text('Not everything was undone: 0 undone; 1 changed since and kept.')
        expect(page.locator('.toast').filter(has_text='previous values are back')).to_have_count(0)
        assert row['id'] in {g['id'] for g in h.get('credentialer', '/api/catalogues')['grants']}
    finally:
        context.close()


def test_programme_lead_creates_programme_without_person_or_clinical_authority(browser, live_server, all_roles):
    h = all_roles
    context, page = sign_in(browser, live_server, 'programme_lead')
    try:
        navigate(page, 'programmes')
        page.get_by_role('navigation', name='Sections').get_by_role('button', name=re.compile(r'^Programmes')).click()
        page.get_by_role('button', name='New programme', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Name', exact=True).fill('Fictional lead-owned programme')
        form.get_by_label('Effective from', exact=True).fill('2026-10-01')
        save_dialog(form)
        saved = next(p for p in h.get('programme_lead', '/api/catalogues')['programmes'] if p['name'] == 'Fictional lead-owned programme')
        assert saved['starts_on'] == '2026-10-01'
        reload_view(page, 'programmes')
        expect(page.get_by_role('heading', name=saved['name'], exact=True)).to_be_visible()
        h.configure('people', {'display_name': 'Forbidden lead person'}, role='programme_lead', status=403)
    finally:
        context.close()


def test_approver_decides_request_without_automatic_cover_or_publication(browser, live_server, h):
    request = h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'meeting',
        'start': '2026-10-04T09:00:00+03:00', 'end': '2026-10-04T10:00:00+03:00',
        'private_reason': 'Fictional role-coverage meeting'})['record']
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page,'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        expect(page.get_by_role('button', name='Record leave', exact=True)).to_have_count(0)
        page.get_by_role('button', name='Approve', exact=True).click()
        save_dialog(page.get_by_role('dialog'), 'Approve')
        saved = next(r for r in h.get('approver', '/api/requests')['requests'] if r['id'] == request['id'])
        assert saved['status'] == 'approved'
        protected, = h.get('staff', '/api/availability')['availability']
        assert protected['kind'] == 'protected_time' and protected['request_id'] == request['id']
        assert all(protected[key] == request[key] for key in ('person_id', 'start', 'end'))
        assert protected['counts_as_unavailable'] and not protected['retracted']
        assert h.get('staff', '/api/publications')['publications'] == []
        reload_view(page,'requests')
        expect(page.get_by_role('button', name='Approve', exact=True)).to_have_count(0)
        expect(page.locator('tbody tr').filter(has_text='Meeting')).to_contain_text('Approved')
    finally:
        context.close()


def test_staff_weekly_proposal_survives_reload_without_becoming_active(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page,'requests')
        expect(page.get_by_role('button', name='Record leave', exact=True)).to_have_count(0)
        page.get_by_role('button', name='New request', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_role('radio', name=re.compile('^Unavailable every week')).check()
        form.get_by_label('Tuesday', exact=True).check()
        form.get_by_label('Note', exact=True).fill('Fictional weekly coverage note')
        save_dialog(form, 'Send for approval')
        rows = h.get('staff', '/api/availability-patterns')['patterns']
        assert len(rows) == 1 and rows[0]['status'] == 'proposed'
        assert rows[0]['person_id'] == h.seed['people'][0]
        reload_view(page,'requests')
        row = page.locator('tbody tr').filter(has_text='Fictional weekly coverage note')
        expect(row).to_contain_text('Tue')
        expect(row).to_contain_text('Waiting for approval')
        expect(page.get_by_role('button', name='Approve', exact=True)).to_have_count(0)
        assert h.get('staff', '/api/availability-patterns')['patterns'][0]['status'] == 'proposed'
    finally:
        context.close()


def test_operator_retries_failed_event_and_auditor_cannot(browser, live_server, h):
    with transaction(h.app.state.engine, write=True) as db:
        event = m.Outbox(department_id=h.seed['department_id'], event_key=str(uuid.uuid4()),
            kind='announcement', payload={}, state='failed', attempts=3, next_attempt_at=m.now(),
            last_error='Fictional retry coverage error')
        db.add(event)
        db.flush()
        event_id = event.id
    auditor_context, auditor = sign_in(browser, live_server, 'auditor')
    context, page = sign_in(browser, live_server, 'operator')
    try:
        navigate(auditor,'system/operations')
        expect(auditor.get_by_text('Fictional retry coverage error', exact=True)).to_be_visible()
        expect(auditor.get_by_role('button', name='Retry', exact=True)).to_have_count(0)
        h.command('auditor', 'outbox.retry', {'reason': 'Read authority cannot retry'}, event_id, 1, status=403)
        navigate(page,'system/operations')
        page.get_by_role('button', name='Retry', exact=True).click()
        save_dialog(page.get_by_role('dialog'), 'Confirm')
        saved = next(e for e in h.get('operator', '/api/operations')['outbox'] if e['id'] == event_id)
        assert saved['state'] == 'queued' and saved['attempts'] == 0 and saved['revision'] == 2
        reload_view(page,'system/operations')
        expect(page.get_by_role('button', name='Retry', exact=True)).to_have_count(0)
        expect(page.locator('tbody')).to_contain_text('Queued')
    finally:
        auditor_context.close()
        context.close()

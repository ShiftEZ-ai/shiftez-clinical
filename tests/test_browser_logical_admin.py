"""Admin saves preserve the whole edit and report effective access dates truthfully."""
import re

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .test_browser import browser, live_server, sign_in, navigate, open_config, save_dialog, wait_held, pick  # noqa: F401
from .test_browser_tags import tag_row

pytestmark = pytest.mark.browser


def _command(kind):
    return lambda r: r.request.method == 'POST' and r.url.endswith('/api/commands') and r.request.post_data_json['kind'] == kind


def _tag(h, tag_id):
    data = h.get('admin', '/api/catalogues')
    return (next(t for t in data['tags'] if t['id'] == tag_id),
            sorted((m['kind'], m['member_id']) for m in data['tag_members'] if m['tag_id'] == tag_id))


def test_unchanged_tag_save_sends_no_command_or_invalidating_input_revision(browser, live_server, h):
    tag = h.configure('tags', {'name': 'Unchanged tag', 'labels': ['services', 'people'], 'description': 'Keep this tag'})['record']
    before = _tag(h, tag['id'])
    with transaction(h.app.state.engine) as db:
        inputs = db.get(m.Organisation, h.seed['organisation_id']).input_revision
    context, page = sign_in(browser, live_server, 'admin')
    writes = []
    try:
        open_config(page, 'Tags')
        tag_row(page, tag['name']).get_by_role('button', name='Edit', exact=True).click()
        form = page.get_by_role('region', name='#Unchanged tag', exact=True)
        page.on('request', lambda r: writes.append(r.url) if r.method == 'POST' and r.url.endswith('/api/commands') else None)
        save_dialog(form, 'Save tag')
        expect(page.get_by_text('No changes to save.', exact=True)).to_be_visible()
        assert writes == []
        assert _tag(h, tag['id']) == before
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Organisation, h.seed['organisation_id']).input_revision == inputs
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [None, {'width': 390, 'height': 844}], ids=['desktop', 'phone'])
def test_rejected_tag_save_keeps_members_and_retry_undo_restores_the_whole_edit(browser, live_server, h, viewport):
    tag = h.configure('tags', {'name': 'Original tag', 'labels': ['services']})['record']
    h.configure('tags', {'name': 'Taken name', 'labels': ['services']})
    clinic = h.get('admin', '/api/catalogues')['services'][0]
    h.command('admin', 'tag.members', {'add': [{'kind': 'services', 'id': clinic['id']}], 'reason': 'Test tag member'}, tag['id'], tag['revision'])
    before = _tag(h, tag['id'])
    context, page = sign_in(browser, live_server, 'admin', viewport)
    try:
        open_config(page, 'Tags')
        tag_row(page, tag['name']).get_by_role('button', name='Edit', exact=True).click()
        form = page.get_by_role('region', name='#Original tag', exact=True)
        form.get_by_role('button', name='Remove ' + clinic['name'], exact=True).click()
        form.get_by_label('Name', exact=True).fill('Taken name')
        with page.expect_response(_command('catalogue.update')) as rejected:
            form.get_by_role('button', name='Save tag', exact=True).click()
        assert rejected.value.status == 409
        assert rejected.value.json()['error']['code'] == 'TAG_NAME_TAKEN'
        expect(form).to_be_visible()
        assert _tag(h, tag['id']) == before

        # Correct the same open form: the rejected save did not consume its revision or remove anyone.
        form.get_by_label('Name', exact=True).fill('Corrected tag')
        save_dialog(form, 'Save tag')
        changed, members = _tag(h, tag['id'])
        assert changed['name'] == 'Corrected tag' and members == []
        page.locator('.toast').filter(has_text='Tag saved.').get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Undone. The previous values are back.', exact=True)).to_be_visible()
        restored, members = _tag(h, tag['id'])
        assert restored['name'] == before[0]['name'] and members == before[1]
        page.reload()
        open_config(page, 'Tags')
        expect(tag_row(page, 'Original tag').locator('.tag-ref').first).to_contain_text('1')
    finally:
        context.close()


def test_dropping_a_populated_tag_kind_previews_its_removals_and_undo_restores_them(browser, live_server, h):
    tag = h.configure('tags', {'name': 'Clinic group', 'labels': ['people', 'services']})['record']
    clinic = h.get('admin', '/api/catalogues')['services'][0]
    h.command('admin', 'tag.members', {'add': [{'kind': 'services', 'id': clinic['id']}], 'reason': 'Test tag member'}, tag['id'], tag['revision'])
    h.policy({'kind': 'min_rest', 'hours': 11}, {'conditions': {'match': 'all', 'conditions': [
        {'field': 'service_ids', 'op': 'any', 'values': [tag['id']]}]}})
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Tags')
        tag_row(page, tag['name']).get_by_role('button', name='Edit', exact=True).click()
        form = page.get_by_role('region', name='#Clinic group', exact=True)
        pick(form, 'Can label', ['Clinics'], False)
        expect(form.locator('.tag-effect')).to_contain_text('1 line in 1 rule follows the change')
        save_dialog(form, 'Save tag')
        saved, members = _tag(h, tag['id'])
        assert saved['labels'] == ['people'] and not members
        page.locator('.toast').filter(has_text='Tag saved.').get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Undone. The previous values are back.', exact=True)).to_be_visible()
        restored, members = _tag(h, tag['id'])
        assert restored['labels'] == ['people', 'services']
        assert members == [('services', clinic['id'])]
    finally:
        context.close()


def test_new_tag_undo_archives_the_tag_and_removes_its_members(browser, live_server, h):
    clinic = h.get('admin', '/api/catalogues')['services'][0]
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Tags')
        page.get_by_role('button', name='New tag', exact=True).click()
        form = page.get_by_role('region', name='New tag', exact=True)
        form.get_by_label('Name', exact=True).fill('Creation undo')
        pick(form, 'Can label', ['People'], False)
        pick(form, 'Can label', ['Clinics'])
        form.get_by_role('button', name=re.compile('Add clinics')).click()
        page.locator('.picker').get_by_role('option', name=clinic['name'], exact=True).check()
        page.keyboard.press('Escape')
        save_dialog(form, 'Save tag')
        tag = next(t for t in h.get('admin', '/api/catalogues')['tags'] if t['name'] == 'Creation undo')
        assert _tag(h, tag['id'])[1] == [('services', clinic['id'])]
        page.locator('.toast').filter(has_text='Tag #Creation undo created.').get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Undone. The previous values are back.', exact=True)).to_be_visible()
        restored, members = _tag(h, tag['id'])
        assert restored['active'] is False and not members
    finally:
        context.close()


@pytest.mark.parametrize('operation', ['create', 'update'])
@pytest.mark.parametrize('refresh_result', ['late', 'failed'])
def test_tag_confirmation_owns_undo_while_refresh_is_pending(browser, live_server, h, operation, refresh_result):
    """Committed changes keep their own Undo through slow/failed refresh and other saves."""
    clinic = h.get('admin', '/api/catalogues')['services'][0]
    existing = h.configure('tags', {'name': 'Before delayed update', 'labels': ['services']})['record'] if operation == 'update' else None
    context, page = sign_in(browser, live_server, 'admin')
    writes, held = [], []
    try:
        open_config(page, 'Tags')
        if existing:
            tag_row(page, existing['name']).get_by_role('button', name='Edit', exact=True).click()
            form = page.get_by_role('region', name='#' + existing['name'], exact=True)
        else:
            page.get_by_role('button', name='New tag', exact=True).click()
            form = page.get_by_role('region', name='New tag', exact=True)
            pick(form, 'Can label', ['People'], False)
            pick(form, 'Can label', ['Clinics'])
        form.get_by_label('Name', exact=True).fill('Confirmed tag')
        form.get_by_role('button', name=re.compile('Add clinics')).click()
        page.locator('.picker').get_by_role('option', name=clinic['name'], exact=True).check()
        page.keyboard.press('Escape')
        page.on('request', lambda request: writes.append(request) if request.method == 'POST' and request.url.endswith('/api/commands') else None)
        page.route('**/api/me', lambda route: held.append(route) if not held else route.continue_())
        form.get_by_role('button', name='Save tag', exact=True).click()
        wait_held(page, held)
        message = 'Tag saved.' if existing else 'Tag #Confirmed tag created.'
        confirmation = page.locator('.toast').filter(has_text=message)
        expect(confirmation.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        # Keep the transient action available while synchronous server checks run.
        confirmation.get_by_role('button', name='Undo', exact=True).focus()
        expect(confirmation.get_by_role('button', name='Undo', exact=True)).to_be_focused()
        expect(page.locator('.toast').filter(has_text=re.compile(r'^Saved\.'))).to_have_count(0)
        assert len(writes) == 1
        tag = next(t for t in h.get('admin', '/api/catalogues')['tags'] if t['name'] == 'Confirmed tag')
        assert _tag(h, tag['id'])[1] == [('services', clinic['id'])]

        # Another owner uses the same canonical command/notification helpers while the first refresh is held.
        # Its Undo must not acquire the earlier tag edit, and the late refresh must not take it back.
        independent = operation == 'update' and refresh_result == 'late'
        if independent:
            page.evaluate("""async () => {
              const {command}=await window.importBundle('api.js'),{notify}=await window.importBundle('dom.js');
              await command('catalogue.create',{catalogue:'tags',value:{name:'Independent tag',labels:['people'],active:true},reason:'Independent test save'});
              notify('Independent tag created.');
            }""")
            expect(confirmation.get_by_role('button', name='Undo', exact=True)).to_be_visible()
            expect(page.locator('.toast').filter(has_text='Independent tag created.').get_by_role('button', name='Undo', exact=True)).to_be_visible()

        # Creation exercises Undo while the original refresh is still pending; edit exercises Undo after it.
        if not existing:
            with page.expect_response(_command('catalogue.update')):
                confirmation.get_by_role('button', name='Undo', exact=True).click()
        if refresh_result == 'failed':
            held[0].fulfill(status=503, content_type='application/json', body='{"error":{"code":"READ_UNAVAILABLE","message":"Synthetic refresh unavailable"}}')
            # The page still shows its data: one banner says how old it is, with Retry (no second message).
            expect(page.locator('#main > .stale-banner')).to_contain_text(re.compile(r'Couldn’t refresh · showing data from \d\d:\d\d'))
        else:
            held[0].continue_()
        expect(form).not_to_be_visible()
        if existing:
            expect(confirmation.get_by_role('button', name='Undo', exact=True)).to_be_visible()
            confirmation.get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Undone. The previous values are back.', exact=True)).to_be_visible()
        restored, members = _tag(h, tag['id'])
        assert members == []
        if existing:
            assert restored['name'] == 'Before delayed update'
        else:
            assert restored['active'] is False
        assert len(writes) == (3 if independent else 2), 'A refresh must never resubmit the committed save.'
        if independent:
            other = next(t for t in h.get('admin', '/api/catalogues')['tags'] if t['name'] == 'Independent tag')
            assert other['active'] is True
            # The same message again replaces the one showing (dom.ts notify): mark the first, so the second is its own.
            page.evaluate("document.querySelectorAll('.toast').forEach(t => { t.dataset.before = '1'; })")
            page.locator('.toast').filter(has_text='Independent tag created.').get_by_role('button', name='Undo', exact=True).click()
            undone = page.locator('.toast').filter(has_text='Undone. The previous values are back.')
            expect(undone.and_(page.locator('.toast:not([data-before])'))).to_be_visible()
            expect(undone).to_have_count(1)
            assert _tag(h, other['id'])[0]['active'] is False
            assert _tag(h, tag['id']) == (restored, members)
            assert len(writes) == 4
    finally:
        context.close()


@pytest.mark.parametrize('independent', ['sessions', 'rules'])
def test_independent_department_save_and_undo_keep_unsaved_fields(browser, live_server, h, independent):
    initial = h.get('admin', '/api/me')['department']
    initial_idle = h.get('admin', '/api/access')['sessions']['idle_minutes'] if independent == 'sessions' else None
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Department settings')
        name = page.get_by_label('Department name', exact=True)
        name.fill('Department unsaved name')
        if independent == 'sessions':
            page.get_by_role('button', name='Edit session settings', exact=True).click()
            dialog = page.get_by_role('dialog', name='Session settings', exact=True)
            dialog.get_by_label('Sign out after inactivity (minutes)', exact=True).fill('35')
            save_dialog(dialog, 'Save')
            message = 'Session settings saved.'
            assert h.get('admin', '/api/access')['sessions']['idle_minutes'] == 35
        else:
            toggle = page.get_by_label('Show the Rules page as the rule catalogue', exact=True)
            expect(toggle).not_to_be_checked()
            # This control requests a confirmed change; its checkmark remains the saved state until Save.
            toggle.click()
            dialog = page.get_by_role('dialog', name='Show the rule catalogue', exact=True)
            expect(dialog).to_be_visible()
            expect(toggle).not_to_be_checked()
            dialog.get_by_label('Reason', exact=True).fill('Test independent setting')
            save_dialog(dialog, 'Save')
            message = 'The Rules page now shows the rule catalogue.'
            assert h.get('admin', '/api/me')['department']['display_settings']['rule_catalogue'] is True
            expect(toggle).to_be_checked()
        expect(name).to_have_value('Department unsaved name')
        page.locator('.toast').filter(has_text=message).get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Undone. The previous values are back.', exact=True)).to_be_visible()
        expect(name).to_have_value('Department unsaved name')
        assert h.get('admin', '/api/me')['department']['name'] == initial['name']
        if independent == 'sessions':
            assert h.get('admin', '/api/access')['sessions']['idle_minutes'] == initial_idle
        else:
            expect(page.get_by_label('Show the Rules page as the rule catalogue', exact=True)).not_to_be_checked()
        page.get_by_role('button', name='Home', exact=True).click()
        leave = page.get_by_role('dialog', name='Leave this page?')
        expect(leave).to_contain_text('Your changes on this page are lost if you go to Home.')
        leave.get_by_role('button', name='Keep editing', exact=True).click()
        expect(leave).to_have_count(0)
        expect(name).to_have_value('Department unsaved name')
        page.get_by_role('region', name='Unsaved changes').get_by_role('button', name='Save settings', exact=True).click()
        dialog = page.get_by_role('dialog', name='Save department settings', exact=True)
        dialog.get_by_label('Reason', exact=True).fill('Save retained department edits')
        save_dialog(dialog, 'Save settings')
        assert h.get('admin', '/api/me')['department']['name'] == 'Department unsaved name'
        page.reload()
        open_config(page, 'Department settings')
        expect(name).to_have_value('Department unsaved name')
    finally:
        context.close()


def _access_person(h, role):
    return next(i for i in h.get('admin', '/api/access')['identities'] if i['id'] == h.seed['identities'][role])


def _bulk_role(page, people, start, last):
    for person in people:
        page.locator('tbody tr[data-row]').filter(has_text=person['display_name']).get_by_role('checkbox').check()
    page.get_by_role('toolbar', name='Bulk actions', exact=True).get_by_role('button', name='Give a role', exact=True).click()
    form = page.get_by_role('dialog', name=re.compile('^Give a role to'))
    # Admin is never granted in bulk: one at a time, through its own approval path (fe-audit decision).
    expect(form.get_by_label('Role', exact=True).locator('option[value=admin]')).to_have_count(0)
    form.get_by_label('Role', exact=True).select_option('credentialer')
    form.get_by_label('From', exact=True).fill(start)
    form.get_by_label('Until (included, optional)', exact=True).fill(last)
    form.get_by_label('Reason', exact=True).fill('Fictional access window')
    return form


def test_future_roles_are_scheduled_with_the_included_last_day_and_can_be_revoked(browser, live_server, h):
    person = _access_person(h, 'staff')
    binding = h.command('admin', 'role.grant', {'identity_id': person['id'], 'role': 'credentialer',
        'starts_on': '2026-10-10', 'ends_on': '2026-10-13', 'reason': 'Fictional future authority'})['record']
    context, page = sign_in(browser, live_server, 'admin')
    try:
        linked = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == person['person_id'])
        navigate(page, 'people')
        page.get_by_role('searchbox', name='Search people', exact=True).fill(linked['display_name'])
        page.get_by_text(linked['display_name'], exact=True).click()
        details = page.get_by_role('region', name=linked['display_name'], exact=True)
        details.get_by_role('tab', name='Access', exact=True).click()
        account = details.get_by_role('tabpanel', name='Access', exact=True).locator('.ov-stat').first
        expect(account).to_contain_text('staff')  # current roles only: the future one is not yet held
        expect(account).not_to_contain_text('credentialer')
        details.get_by_role('button', name='Close panel', exact=True).click()
        navigate(page, 'access/people')
        chip = page.locator('tbody tr[data-row]').filter(has_text=person['display_name']).locator('.role-chip').filter(has_text='Credentialer')
        expect(chip).to_contain_text('Scheduled from 10 Oct 2026')
        expect(chip).to_have_attribute('data-tip', 'From 10 Oct 2026 until 12 Oct 2026 (included)')
        navigate(page, 'access/history')
        # Grouped by person, the person is the group heading (AC-02): search narrows the table to them.
        page.get_by_role('searchbox', name='Search access history', exact=True).fill(person['display_name'])
        row = page.locator('tbody tr[data-row]').filter(has_text='Credentialer')
        expect(row).to_contain_text('Scheduled')
        expect(row.get_by_role('cell', name='12 Oct 2026', exact=True)).to_be_visible()
        navigate(page, 'access/people')
        # Removing someone else's role is a row action (no small × on the chip) that acts at once with Undo (D42).
        page.get_by_role('button', name='More actions for ' + person['display_name'], exact=True).first.click()
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.post_data_json.get('kind') == 'role.revoke'):
            page.get_by_role('menuitem', name='Remove Credentialer role', exact=True).click()
        expect(page.locator('.toast').filter(has_text='Credentialer role removed').get_by_role('button', name='Undo', exact=True)).to_be_visible()
        assert next(b for b in h.get('admin', '/api/access')['bindings'] if b['id'] == binding['id'])['revoked']
    finally:
        context.close()


def test_bulk_role_grants_a_disjoint_future_window_and_reports_covered_window_as_noop(browser, live_server, h):
    person = _access_person(h, 'staff')
    h.command('admin', 'role.grant', {'identity_id': person['id'], 'role': 'credentialer',
        'starts_on': '2026-10-01', 'ends_on': '2026-10-08', 'reason': 'Fictional current authority'})
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'access/people')
        form = _bulk_role(page, [person], '2026-10-10', '2026-10-12')
        save_dialog(form, 'Give role')
        grants = [b for b in h.get('admin', '/api/access')['bindings'] if b['identity_id'] == person['id'] and b['role'] == 'credentialer']
        assert sorted((b['starts_on'], b['ends_on']) for b in grants) == [('2026-10-01', '2026-10-08'), ('2026-10-10', '2026-10-13')]
        form = _bulk_role(page, [person], '2026-10-10', '2026-10-12')
        save_dialog(form, 'Give role')
        expect(page.get_by_text('No changes. 1 person already has access for the whole period.', exact=True)).to_be_visible()
        assert [b for b in h.get('admin', '/api/access')['bindings'] if b['identity_id'] == person['id'] and b['role'] == 'credentialer'] == grants
    finally:
        context.close()


def test_bulk_role_overlap_is_refused_before_any_selected_person_is_changed(browser, live_server, h):
    clear, overlap = _access_person(h, 'coordinator'), _access_person(h, 'staff')
    h.command('admin', 'role.grant', {'identity_id': overlap['id'], 'role': 'credentialer',
        'starts_on': '2026-10-10', 'ends_on': '2026-10-13', 'reason': 'Fictional existing authority'})
    before = h.get('admin', '/api/access')['bindings']
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'access/people')
        form = _bulk_role(page, [clear, overlap], '2026-10-12', '2026-10-15')
        form.get_by_role('button', name='Give role', exact=True).click()
        expect(form.locator('.notice.error')).to_contain_text('No roles were granted.')
        expect(form.locator('.notice.error')).to_contain_text(overlap['display_name'])
        assert h.get('admin', '/api/access')['bindings'] == before
    finally:
        context.close()


def test_future_programme_membership_is_upcoming(browser, live_server, h):
    programme = h.configure('programmes', {'name': 'Future membership audit', 'starts_on': '2026-01-01'})['record']
    h.configure('programme_memberships', {'person_id': h.seed['people'][0], 'programme_id': programme['id'],
        'starts_on': '2026-10-10', 'ends_on': '2026-11-01'}, role='coordinator')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'programmes')
        page.get_by_role('button', name='Programme memberships', exact=True).click()
        expect(page.locator('.config-pane tr[data-row]').filter(has_text=programme['name']).get_by_role('cell', name='Upcoming', exact=True)).to_be_visible()
    finally:
        context.close()

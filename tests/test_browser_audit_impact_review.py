"""Configuration review preserves completed impact and the real API's stale-input guard."""
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest
from playwright.sync_api import expect

from .test_browser import form_submit, route_fetch, browser, live_server, navigate, pick, sign_in, wait_held  # noqa: F401
from .test_browser_operator_role_coverage import all_roles  # noqa: F401
from .test_browser_people_merged import row as people_row
from .test_browser_rule_catalogue import shot

pytestmark = pytest.mark.browser


def assert_form_contained(dialog):
    result = dialog.evaluate('''d => {
        const bounds=d.getBoundingClientRect(),outside=[],detachedHelp=[];
        for(const e of d.querySelectorAll('form,form .form-footer,form .stepper,form .table-scroll,form input:not([type=hidden]),form select,form textarea,form button')){
            // Hidden content is not rendered. The rail and data-table scrollers own their buttons'
            // horizontal overflow; their own bounds, every rendered field and the footer remain checked.
            if(!e.checkVisibility()||(e.matches('button')&&e.closest('.stepper')))continue;
            const owner=e.closest('.table-scroll');
            if(e.matches('button')&&owner&&['auto','scroll'].includes(getComputedStyle(owner).overflowX))continue;
            const r=e.getBoundingClientRect();if(!r.width||!r.height)continue;
            if(r.left<bounds.left-1||r.right>bounds.right+1||r.left<0||r.right>innerWidth)
                outside.push({label:e.labels?.[0]?.textContent||e.textContent||e.name,left:r.left,right:r.right});
        }
        for(const help of d.querySelectorAll('.configuration-impact button[aria-label="What this preview checks"]')){
            if(!help.checkVisibility())continue;
            const status=help.parentElement.querySelector('.configuration-impact-status');
            const a=status.getBoundingClientRect(),b=help.getBoundingClientRect();
            if(!a.width||!a.height||!b.width||!b.height)continue;
            if(b.top>=a.bottom||b.bottom<=a.top)detachedHelp.push(status.textContent);
        }
        return {outside,detachedHelp,dialogOverflow:d.scrollWidth-d.clientWidth,pageOverflow:document.documentElement.scrollWidth-innerWidth};
    }''')
    assert result['outside'] == [] and result['dialogOverflow'] <= 1 and result['pageOverflow'] <= 1, result
    assert result['detachedHelp'] == [], result


@pytest.mark.parametrize('width,scheme', [(390, 'dark'), (1440, 'light')])
def test_populated_configuration_preview_scrolls_within_form_and_keeps_save_visible(browser, live_server, all_roles, width, scheme):
    h = all_roles
    workspace = h.shift(h.workspace('Fictional populated configuration preview rota'))['workspace']
    context, page = sign_in(browser, live_server, 'programme_lead', {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        page.evaluate("localStorage.setItem('rota.prefs', JSON.stringify({forms:'all'}))")
        navigate(page, 'programmes/programmes')
        page.get_by_role('button', name='New programme', exact=True).click()
        dialog = page.get_by_role('dialog', name='New programme', exact=True)
        dialog.get_by_label('Name', exact=True).fill('Fictional contained programme')
        dialog.get_by_label('Effective from', exact=True).fill('2026-10-04')
        description = 'A populated impact preview must not widen the form.'
        dialog.get_by_label('Description', exact=True).fill(description)
        with page.expect_response(lambda r: r.url.endswith('/api/catalogue/preview') and r.request.method == 'POST'
                                  and r.request.post_data_json['payload']['value'].get('description') == description) as preview:
            dialog.get_by_role('button', name='Calculate again', exact=True).click()
        assert preview.value.status == 200
        assert [r['workspace_id'] for r in preview.value.json()['impacts']] == [workspace['id']]
        impact = dialog.locator('section').filter(has=page.get_by_role('heading', name='Review configuration consequences', exact=True))
        expect(impact.locator('table tbody tr')).to_have_count(1)
        # The comparison is shown under its heading, never folded away.
        expect(impact.get_by_role('heading', name='Rota comparison', exact=True)).to_be_visible()
        expect(impact.locator('table')).to_be_visible()
        assert_form_contained(dialog)
        scroll = impact.locator('.table-scroll')
        if width == 390:
            assert scroll.evaluate('e => e.scrollWidth > e.clientWidth'), 'the preview owns the horizontal overflow'
        footer = dialog.locator('.form-footer')
        footer.scroll_into_view_if_needed()
        save = form_submit(dialog)
        expect(save).to_be_in_viewport(ratio=1)
        expect(footer.get_by_role('button', name='Cancel', exact=True)).to_be_in_viewport(ratio=1)
        assert_form_contained(dialog)
        shot(page, f'configuration-preview-contained-{width}-{scheme}')
        save.click()
        expect(dialog).to_have_count(0)
        saved = next(p for p in h.get('programme_lead', '/api/catalogues')['programmes'] if p['name'] == 'Fictional contained programme')
        assert saved['starts_on'] == '2026-10-04'
    finally:
        context.close()


def present_preview(route, scenario):
    """Exercise presentation-only states over a real read-only preview; these cases never submit this response."""
    response = route_fetch(route)
    assert response.status == 200
    body = response.json()
    introduced = []
    resolved = []
    errors, warnings = (2, 1) if scenario == 'existing' else (0, 0)
    if scenario == 'replacement':
        errors = 1
        introduced = [{'severity': 'error', 'code': 'NEW_SAFETY_CONFLICT', 'message': 'Fictional newly introduced mandatory conflict.'}]
        resolved = [{'severity': 'error', 'code': 'OLD_SAFETY_CONFLICT', 'message': 'Fictional previous conflict now resolved.'}]
    if scenario == 'warning':
        warnings = 1
        introduced = [{'severity': 'warning', 'code': 'COVERAGE_TARGET', 'message': 'Fictional engine advisory wording.'}]
    body.update(workspace_count=1, partial=scenario == 'partial', inventory_only=scenario == 'inventory', impacts=[{
        'workspace_id': 'presentation-fixture', 'name': 'Fictional reviewed rota', 'status': 'draft',
        'before_error_count': errors, 'after_error_count': errors,
        'before_warning_count': 0 if scenario == 'warning' else warnings, 'after_warning_count': warnings,
        'introduced': introduced, 'resolved': resolved,
    }])
    if scenario == 'inventory':
        body.update(workspace_count=0, impacts=[])
    if scenario == 'contact':
        body.update(impacts=[], message='Contact details checked. Scheduling inputs are unchanged; no rota validation is needed. Nothing saved or approved.')
    route.fulfill(response=response, json=body)


def close_preview(context, page):
    # Stop test-owned callbacks before disposal. Errors during the active test still
    # fail it; cancelled fetches after teardown must not fail the next context.
    page.unroute_all(behavior='ignoreErrors')
    context.close()


def test_preview_teardown_preserves_failure_and_keeps_next_context_usable(browser):
    entered, release = [], Event()

    class PreviewServer(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/preview':
                entered.append(True)
                release.wait(30)
                # Teardown has disposed this request's client. Close without
                # writing a response to its cancelled connection.
                return
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), PreviewServer)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    context = browser.new_context()
    page = context.new_page()
    following = None
    try:
        def preview(route):
            response = route_fetch(route)
            route.fulfill(response=response)

        page.goto(f'http://127.0.0.1:{server.server_port}/')
        page.route('**/preview', preview)
        page.evaluate("() => { void fetch('/preview'); }")
        wait_held(page, entered)
        with pytest.raises(AssertionError, match='Synthetic preview assertion'):
            try:
                raise AssertionError('Synthetic preview assertion')
            finally:
                close_preview(context, page)
        release.set()
        # A callback from the closed page must not surface on the next test's
        # Clock.install (or any other operation on its fresh browser context).
        following = browser.new_context()
        next_page = following.new_page()
        next_page.set_content('<p>Next context is usable</p>')
        expect(next_page.get_by_text('Next context is usable', exact=True)).to_be_visible()
    finally:
        release.set()
        if following:
            following.close()
        if not page.is_closed():
            close_preview(context, page)
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize('scenario', ['unchanged', 'existing', 'replacement', 'warning', 'partial', 'inventory', 'contact'])
def test_compact_configuration_preview_keeps_review_decisions_visible(browser, live_server, h, scenario):
    context, page = sign_in(browser, live_server, 'admin', {'width': 390, 'height': 900})
    try:
        page.emulate_media(color_scheme='dark')
        page.evaluate("localStorage.setItem('rota.prefs', JSON.stringify({forms:'all'}))")
        page.route('**/api/catalogue/preview', lambda route: present_preview(route, scenario))
        navigate(page, 'people')
        page.get_by_role('button', name='New person', exact=True).click()
        dialog = page.get_by_role('dialog', name='New person')
        dialog.get_by_label('Display name', exact=True).fill('Fictional compact review')
        expect(dialog.get_by_role('heading', name='Review before saving', exact=True)).to_be_hidden()
        expect(dialog.get_by_text('Check what you entered. Use Edit or Back to change anything.', exact=True)).to_have_count(0)
        expect(dialog.get_by_role('button', name='Back', exact=True)).to_be_hidden()
        expect(dialog.get_by_role('button', name='Add person', exact=True)).to_be_visible()
        impact = dialog.locator('.configuration-impact')
        checked = 'Configuration checked. Existing duties stay unchanged.' if scenario == 'inventory' else 'Configuration checked.' if scenario == 'contact' else 'Impact not calculated.' if scenario == 'partial' else '1 active rota checked.'
        expect(impact.get_by_text(checked, exact=False)).to_be_visible()
        expect(impact.get_by_role('button', name='Calculate again', exact=True)).to_be_enabled()
        assert_form_contained(dialog)
        if scenario in ('inventory', 'contact'):
            expect(impact.locator('.notice')).to_have_count(0)
            expect(impact.locator('details,table')).to_have_count(0)
            expect(impact.get_by_text(re.compile(r'active rotas? checked\.'))).to_have_count(0)
        else:
            expect(impact.locator('table')).to_have_count(2 if scenario in ('replacement', 'warning') else 1)
            expect(impact.get_by_role('heading', name='Rota comparison', exact=True)).to_be_visible()
            expect(impact.locator('table').first).to_be_visible()
        if scenario == 'replacement':
            # Equal totals cannot hide a newly introduced mandatory finding behind a comparison/help control.
            expect(impact.get_by_role('alert')).to_contain_text('1 new mandatory conflict. Review before saving.')
            expect(impact.get_by_role('alert').get_by_text('Fictional reviewed rota · Fictional newly introduced mandatory conflict.', exact=True)).to_be_visible()
            expect(impact.get_by_role('alert')).not_to_contain_text('NEW_SAFETY_CONFLICT')
            expect(impact.get_by_text('1 mandatory conflict remains.', exact=True)).to_be_visible()
            expect(impact.get_by_text('1 finding resolved.', exact=True)).to_be_visible()
            expect(impact.get_by_text('No finding changes.', exact=False)).to_have_count(0)
            expect(impact.get_by_role('row').filter(has_text='Fictional reviewed rota').first).to_contain_text('1 → 1')
            expect(impact.get_by_role('row').filter(has_text='Fictional newly introduced mandatory conflict.').get_by_role('cell')).to_have_text(
                ['Introduced', 'Fictional newly introduced mandatory conflict.'])
            expect(impact.get_by_role('row').filter(has_text='Fictional previous conflict now resolved.').get_by_role('cell')).to_have_text(
                ['Resolved', 'Fictional previous conflict now resolved.'])
            expect(impact).not_to_contain_text(re.compile(r'NEW_SAFETY_CONFLICT|OLD_SAFETY_CONFLICT'))
            # The opened comparison owns its horizontal overflow, and its header menu remains reachable.
            explanation = impact.get_by_role('button', name='Explanation', exact=True)
            explanation.scroll_into_view_if_needed()
            expect(explanation).to_be_in_viewport(ratio=1)
            explanation.focus()
            page.keyboard.press('Enter')
            menu = page.get_by_role('menu', name='Explanation column', exact=True)
            expect(menu.get_by_role('menuitemcheckbox', name='Wrap this column', exact=True)).to_be_visible()
            page.keyboard.press('Escape')
            expect(menu).to_have_count(0)
            expect(explanation).to_be_focused()
            assert_form_contained(dialog)
        else:
            expect(impact.get_by_role('alert')).to_have_count(0)
        if scenario == 'existing':
            expect(impact.get_by_text('2 mandatory conflicts remain · 1 warning remains.', exact=True)).to_be_visible()
            expect(impact.get_by_text('No finding changes.', exact=False)).to_be_visible()
        if scenario == 'warning':
            expect(impact.get_by_text('1 new warning.', exact=True)).to_be_visible()
            expect(impact.get_by_text('1 warning remains.', exact=True)).to_be_visible()
            # Known codes use the shared plain wording; unknown replacement codes retain their message above.
            expect(impact.get_by_role('row').filter(has_text='Fewer people than the preferred number (advisory)').get_by_role('cell')).to_have_text(
                ['Introduced', 'Fewer people than the preferred number (advisory)'])
            expect(impact).not_to_contain_text('COVERAGE_TARGET')
            expect(impact).not_to_contain_text('Fictional engine advisory wording.')
        if scenario == 'partial':
            expect(impact).to_contain_text('are more than this check covers. Save again to apply; each rota is still validated.')
        if scenario == 'unchanged':
            expect(impact.get_by_text('No finding changes.', exact=False)).to_be_visible()
            expect(impact.get_by_text('mandatory conflict', exact=False)).to_have_count(0)
        impact.scroll_into_view_if_needed()
        shot(page, f'configuration-impact-{scenario}-390-dark')
        impact.get_by_role('button', name='What this preview checks', exact=True).click()
        scope = page.get_by_role('dialog', name='What this preview checks', exact=True)
        expect(scope).to_contain_text('Historical issued snapshots remain unchanged')
        expect(scope).to_contain_text('Nothing saved or approved')
        if scenario == 'inventory':
            expect(scope).to_contain_text('Saving this configuration does not create or update duties.')
        if scenario == 'contact':
            expect(scope).to_contain_text('Contact details checked.')
            expect(scope).to_contain_text('no rota validation is needed')
        page.keyboard.press('Escape')
        expect(scope).to_have_count(0)
        expect(impact.get_by_role('button', name='What this preview checks', exact=True)).to_be_focused()
        assert not any(p['display_name'] == 'Fictional compact review' for p in h.get('admin', '/api/catalogues')['people'])
    finally:
        close_preview(context, page)


def test_batch_configuration_preview_opens_people_with_conflicts_or_partial_scope(browser, live_server, h):
    programme = h.configure('programmes', {'name': 'Fictional batch review programme', 'starts_on': '2026-01-01'})['record']
    people = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    chosen = h.seed['people'][:2]
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 390, 'height': 900})
    try:
        page.evaluate("localStorage.setItem('rota.prefs', JSON.stringify({forms:'all'}))")
        page.route('**/api/catalogue/preview', lambda route: present_preview(route,
            'replacement' if route.request.post_data_json['payload']['value']['person_id'] == chosen[0] else 'partial'))
        navigate(page, 'programmes/programme_memberships')
        page.get_by_role('button', name='New programme membership', exact=True).click()
        dialog = page.get_by_role('dialog', name='New programme membership')
        pick(dialog, 'People', [people[i] for i in chosen])
        dialog.get_by_label('Programme', exact=True).select_option(programme['id'])
        dialog.get_by_label('Effective from', exact=True).fill('2026-10-01')
        dialog.get_by_label('Effective until (included)', exact=True).fill('2026-10-31')
        impact = dialog.locator('.configuration-impact')
        expect(impact.get_by_text('2 records will be saved.', exact=True)).to_be_visible()
        expect(impact.locator('.notice').filter(has_text='2 records will be saved.')).to_have_count(0)
        for person in chosen:
            part = impact.locator('section.configuration-impact-record').filter(has=page.get_by_role('heading', name=people[person], exact=True))
            expect(part).to_be_visible()
            if person == chosen[0]:
                expect(part.get_by_role('alert')).to_contain_text('Fictional newly introduced mandatory conflict.')
            else:
                expect(part).to_contain_text('are more than this check covers. Save again to apply; each rota is still validated.')
        expect(impact.get_by_role('alert')).to_contain_text('Fictional reviewed rota · Fictional newly introduced mandatory conflict.')
        expect(impact.get_by_role('alert')).not_to_contain_text('NEW_SAFETY_CONFLICT')
        expect(impact.get_by_role('alert')).to_be_visible()
        expect(impact).to_contain_text('are more than this check covers. Save again to apply; each rota is still validated.')
        # Each person's rota comparison is shown under its own heading (nothing folded away).
        expect(impact.get_by_role('heading', name='Rota comparison', exact=True)).to_have_count(2)
        expect(impact).not_to_contain_text(re.compile(r'NEW_SAFETY_CONFLICT|OLD_SAFETY_CONFLICT'))
        assert_form_contained(dialog)
        impact.scroll_into_view_if_needed()
        shot(page, 'configuration-impact-batch-390')
        assert not any(r['programme_id'] == programme['id'] for r in h.get('coordinator', '/api/catalogues')['programme_memberships'])
    finally:
        close_preview(context, page)


GOVERNANCE = 'Membership labels do not establish clinical authority. Clinical records require an authorised credentialer.'


@pytest.mark.parametrize('width', [1440, 390])
def test_credentialer_relationship_form_keeps_controls_and_save_visible(browser, live_server, h, width):
    """This sibling uses the shared grid, but relationship writes do not have a configuration-impact preview."""
    person = h.configure('people', {'display_name': 'Fictional contained eligibility person'})['record']
    service = h.get('credentialer', '/api/catalogues')['services'][0]
    context, page = sign_in(browser, live_server, 'credentialer', {'width': width, 'height': 900})
    try:
        page.evaluate("localStorage.setItem('rota.prefs', JSON.stringify({forms:'all'}))")
        navigate(page, 'people')
        page.get_by_role('button', name='Authorise eligibility', exact=True).click()
        dialog = page.get_by_role('dialog', name='Authorise clinics', exact=True)
        # Governance stays visible: it limits what the record means, so it is never moved into a help icon.
        expect(dialog.get_by_text(GOVERNANCE, exact=True)).to_be_visible()
        dialog.get_by_label('Person', exact=True).select_option(person['id'])
        pick(dialog, 'Clinics', [service['name']])
        dialog.get_by_label('From', exact=True).fill('2026-10-04')
        dialog.get_by_label('Last day (optional)', exact=True).fill('2026-10-31')
        assert_form_contained(dialog)
        save = dialog.get_by_role('button', name='Authorise clinics', exact=True)
        save.scroll_into_view_if_needed()
        expect(save).to_be_in_viewport(ratio=1)
        expect(dialog.get_by_role('button', name='Cancel', exact=True)).to_be_in_viewport(ratio=1)
        shot(page, f'credentialer-form-contained-{width}')
        save.click()
        expect(dialog).to_have_count(0)
        saved = [r for r in h.get('credentialer', '/api/catalogues')['eligibilities'] if r['person_id'] == person['id']]
        assert [(r['service_id'], r['starts_on'], r['ends_on']) for r in saved] == [(service['id'], '2026-10-04', '2026-11-01')]
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('role,action,title,label', [
    ('coordinator', 'Add rank', 'Add rank', 'Rank'),
    ('credentialer', 'Grant skill', 'Grant skill', 'Skill'),
])
def test_relationship_forms_show_governance_and_required_marks(browser, live_server, h, width, role, action, title, label):
    person = h.configure('people', {'display_name': 'Fictional authority help person'})['record']
    context, page = sign_in(browser, live_server, role, {'width': width, 'height': 900})
    try:
        navigate(page, 'people')
        people_row(page, person['display_name']).get_by_role('button', name=re.compile('^More actions for ')).click()
        page.get_by_role('menuitem', name=action, exact=True).click()
        dialog = page.get_by_role('dialog', name=title, exact=True)
        field = dialog.get_by_label(label, exact=True)
        expect(field).to_be_visible()
        expect(field).to_have_accessible_name(label)
        expect(field).to_have_attribute('required', '')
        # maintenance: the form marks the fewer kind in words; a required field never reads as optional.
        expect(dialog.locator('[data-cue]').first).to_be_visible()
        expect(dialog.locator(f'label[for="{field.get_attribute("id")}"][data-cue=optional]')).to_have_count(0)
        # Opened from a person's row, the person is fixed and shown as a record label rather than a second picker.
        person_field = dialog.get_by_text(person['display_name'], exact=True)
        expect(dialog.locator('.relationship-person')).to_contain_text(person['display_name'])
        expect(dialog.get_by_text(GOVERNANCE, exact=True)).to_be_visible()
        if action == 'Grant skill':
            evidence = dialog.get_by_label('Evidence you checked', exact=True)
            expect(evidence).to_have_attribute('required', '')
            expect(dialog.locator(f'label[for="{evidence.get_attribute("id")}"][data-cue=optional]')).to_have_count(0)
            optional_scope = dialog.get_by_label('Only at clinic', exact=True)
            expect(dialog.locator(f'label[for="{optional_scope.get_attribute("id")}"][data-cue=required]')).to_have_count(0)
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        expect(dialog).to_have_count(0)
    finally:
        context.close()


def open_person(page, name):
    navigate(page, 'people')
    page.get_by_role('button', name='New person', exact=True).click()
    dialog = page.get_by_role('dialog', name='New person')
    dialog.get_by_label('Display name', exact=True).fill(name)
    expect(dialog.get_by_text('Configuration checked.', exact=True)).to_be_visible()
    return dialog


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}])
def test_review_keeps_completed_impact_and_creates_person_on_first_save(browser, live_server, h, viewport):
    context, page = sign_in(browser, live_server, 'admin', viewport)
    try:
        dialog = open_person(page, 'Fictional reviewed person')

        # A needless Review request would clear the completed fingerprint while it is pending.
        # Hold only that request, then release it after Save, without mocking its real API result.
        held = []
        page.route('**/api/catalogue/preview', lambda route: held.append(route))
        form_submit(dialog).click()
        for route in held:
            route.continue_()
        expect(dialog).to_have_count(0)
        people = h.get('admin', '/api/catalogues')['people']
        assert any(person['display_name'] == 'Fictional reviewed person' for person in people)
    finally:
        context.close()


def test_changed_values_are_checked_before_saving_and_a_harmless_check_saves_on_the_same_click(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        dialog = open_person(page, 'Fictional original name')
        held = []
        page.route('**/api/catalogue/preview', lambda route: held.append(route))
        dialog.get_by_label('Display name', exact=True).fill('Fictional changed name')
        expect(dialog.get_by_text('Checking the impact. Nothing has been saved.', exact=True)).to_be_attached()
        form_submit(dialog).click()
        # Nothing is saved while the check for the changed values is still being worked out.
        assert not any(person['display_name'] == 'Fictional changed name' for person in h.get('admin', '/api/catalogues')['people'])
        for route in held:
            route.continue_()
        # The check for exactly these values changes nothing: the same click saves (no second click asked for).
        expect(dialog).to_have_count(0)
        people = h.get('admin', '/api/catalogues')['people']
        assert any(person['display_name'] == 'Fictional changed name' for person in people)
        assert not any(person['display_name'] == 'Fictional original name' for person in people)
    finally:
        context.close()


def test_unchanged_form_rejects_stale_shared_inputs_and_explicit_recalculation_recovers(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        dialog = open_person(page, 'Fictional refreshed person')
        h.configure('categories', {'name': 'Fictional new shared rank', 'starts_on': '2020-01-01'})
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST') as saved:
            form_submit(dialog).click()
        assert saved.value.status == 409
        assert saved.value.json()['error']['code'] == 'STALE_PREVIEW'
        expect(dialog.get_by_role('alert').filter(has_text='Recalculate configuration impact.')).to_be_visible()
        assert not any(person['display_name'] == 'Fictional refreshed person' for person in h.get('admin', '/api/catalogues')['people'])

        calculate = dialog.get_by_role('button', name='Calculate again', exact=True)
        calculate.focus()
        expect(calculate).to_be_focused()
        with page.expect_response(lambda r: r.url.endswith('/api/catalogue/preview') and r.request.method == 'POST') as preview:
            calculate.press('Enter')
        assert preview.value.status == 200
        expect(dialog.get_by_text('Configuration checked.', exact=True)).to_be_visible()
        expect(calculate).to_be_focused()
        form_submit(dialog).click()
        expect(dialog).to_have_count(0)
        assert any(person['display_name'] == 'Fictional refreshed person' for person in h.get('admin', '/api/catalogues')['people'])
    finally:
        context.close()


def test_contact_only_preview_explains_unchanged_scheduling_without_an_empty_findings_table(browser, live_server, h):
    person = h.configure('people', {'display_name': 'Fictional contact preview'})['record']
    h.workspace()
    context, page = sign_in(browser, live_server, 'admin')
    try:
        page.evaluate("localStorage.setItem('rota.prefs', JSON.stringify({forms:'all'}))")
        navigate(page, 'people')
        people_row(page, person['display_name']).locator('strong').first.click()
        page.get_by_role('region', name=person['display_name'], exact=True).get_by_role('button', name='Edit ' + person['display_name'], exact=True).click()
        dialog = page.get_by_role('dialog', name='Edit person')
        expect(dialog.get_by_text('Configuration checked.', exact=True)).to_be_visible()
        with page.expect_response(lambda r: r.url.endswith('/api/catalogue/preview') and r.request.method == 'POST') as preview:
            dialog.get_by_label('Work phone', exact=True).fill('+966 11 000 1234')
        assert preview.value.status == 200
        assert preview.value.json()['proposed']['work_phone'] == '+966 11 000 1234'
        impact = dialog.locator('section').filter(has=page.get_by_role('heading', name='Review configuration consequences', exact=True))
        expect(impact.get_by_text('Configuration checked.', exact=True)).to_be_visible()
        expect(impact.get_by_text(re.compile(r'active rotas? checked\.'))).to_have_count(0)
        impact.get_by_role('button', name='What this preview checks', exact=True).click()
        scope = page.get_by_role('dialog', name='What this preview checks', exact=True)
        expect(scope.get_by_text('Contact details checked.', exact=False)).to_be_visible()
        expect(scope).to_contain_text('no rota validation is needed')
        expect(scope).to_contain_text('Historical issued snapshots remain unchanged')
        page.keyboard.press('Escape')
        expect(scope).to_have_count(0)
        expect(impact.get_by_role('table')).to_have_count(0)
        dialog.get_by_role('button', name='Save version', exact=True).click()
        expect(dialog).to_have_count(0)
        saved = next(row for row in h.get('admin', '/api/catalogues')['people'] if row['id'] == person['id'])
        assert saved['work_phone'] == '+966 11 000 1234'
    finally:
        context.close()

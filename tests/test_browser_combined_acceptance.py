"""Small real-browser acceptance for the cumulative candidate, on disposable fictional data."""
import re
from copy import deepcopy

import pytest
from playwright.sync_api import expect

from packages.contracts import schemas as s
from apps.api import models as m
from apps.api.db import transaction
from .test_browser import (auto_setup, browser, live_server, new_context, sign_in, navigate, open_rota,
                           open_scheduling, add_filter, close_popover,
                           toolbar, view_settings, save_dialog)
from .test_browser_cp_sat import cp_server, _check_after_apply, _joins, _review
from .test_browser_rule_catalogue import flag
from .test_browser_rule_catalogue_v2 import v2_rule, line
from .test_browser_rule_filter_builder import pick
from .test_branding_configuration import raster
from .test_browser_autoschedule_budget import confirm
from .browser_acceptance_support import shot

pytestmark = pytest.mark.browser
VIEWS = [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')]


def options(bar):
    bar.get_by_role('button', name=re.compile('^Search options')).click()
    return bar.page.get_by_role('dialog', name='Search options', exact=True)


def option(bar, owner, value):
    panel = options(bar)
    panel.get_by_role('menu', name=owner, exact=True).get_by_role('menuitemradio', name=value, exact=True).click()
    close_popover(bar.page)


def reference(bar, kind, label):
    panel = options(bar)
    panel.get_by_role('menu', name='Find a record').get_by_role(
        'menuitem', name=re.compile('^' + re.escape(kind) + r'(?:\s+picked)?$')).click()
    picker = bar.page.get_by_role('dialog', name='Find ' + kind.lower(), exact=True)
    picker.get_by_role('combobox').fill(label)
    picker.get_by_role('option', name=label, exact=True).click()
    expect(picker).to_have_count(0)


def save_view(page, bar, name):
    toolbar(bar, 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
    dialog = page.get_by_role('dialog', name='New view')
    dialog.get_by_label('View name', exact=True).fill(name)
    save_dialog(dialog, 'Save view')


@pytest.mark.parametrize('width,scheme', VIEWS)
def test_people_search_exact_tokens_saved_view_and_keyboard(browser, live_server, h, width, scheme):
    tag = h.configure('tags', {'name': 'Cedar intake', 'labels': ['people']})['record']
    first = h.configure('people', {'display_name': 'Cedar Quinn', 'tags': ['Cedar intake']})['record']
    second = h.configure('people', {'display_name': 'Cedar Quinn extra'})['record']
    for person in (first, second):
        h.command('coordinator', 'membership.create', {'person_id': person['id'],
            'category_id': h.seed['category'], 'starts_on': '2020-01-01'})
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900}, color_scheme=scheme)
    try:
        navigate(page, 'people')
        bar = page.locator('.dt-toolbar').first
        search = bar.get_by_role('searchbox')
        search.fill('Cedar Quinn')
        expect(page.locator('tr[data-row], .dt-card').filter(has_text='Cedar Quinn')).to_have_count(2)
        option(bar, 'Match', next(x['label'] for x in s.SEARCH_MODES if x['value'] == 'exact'))
        expect(page.locator('tr[data-row], .dt-card').filter(has_text='Cedar Quinn')).to_have_count(1)
        reference(bar, 'People', 'Cedar Quinn')
        expect(bar.get_by_role('button', name='Search the text instead of the picked people record')).to_be_visible()
        expect(search).to_have_value('Cedar Quinn')
        save_view(page, bar, 'Cedar exact person')
        page.reload()
        expect(bar.get_by_role('button', name='Saved views: Cedar exact person', exact=True)).to_be_visible()
        expect(bar.get_by_role('button', name='Search the text instead of the picked people record')).to_be_visible()
        search.fill('Cedar Quinn extra')
        expect(bar.get_by_role('button', name='Search the text instead of the picked people record')).to_be_hidden()
        expect(page.locator('tr[data-row], .dt-card').filter(has_text='Cedar Quinn extra')).to_have_count(1)
        panel = options(bar)
        shot(page, f'combined-people-options-{width}-{scheme}', full_page=False)
        page.keyboard.press('Escape')
        expect(panel).to_have_count(0)
        expect(bar.get_by_role('button', name=re.compile('^Search options'))).to_be_focused()
        options(bar).get_by_role('button', name='Reset search options', exact=True).click()
        search.fill('Cedar')
        expect(page.locator('tr[data-row], .dt-card').filter(has_text='Cedar Quinn')).to_have_count(2)
        reference(bar, 'Tags', tag['name'])
        expect(page.locator('tr[data-row], .dt-card').filter(has_text='Cedar Quinn')).to_have_count(1)
        shot(page, f'combined-people-reset-{width}-{scheme}', full_page=False)
    finally:
        context.close()


def test_rule_level_tag_reference_matches_the_real_rules_tree(browser, live_server, h):
    flag(h, legacy=False)
    record = v2_rule(h, 'workload_limit', [line({'max': {'value': 5, 'level': 'mandatory'}},
                                              measure='duties', period={'kind': 'calendar_week'})])
    tag = h.configure('tags', {'name': 'Cedar policy marker', 'labels': ['policies']})['record']
    h.command('coordinator', 'tag.members', {'add': [{'kind': 'policies', 'id': record['id']}],
        'reason': 'Mark the fictional rule'}, tag['id'], tag['revision'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        bar = page.locator('.dt-toolbar').first
        workload = page.locator('.rt-item').filter(has_text='Workload')
        unrelated = page.locator('.rt-item').filter(has_text='Fair share')
        expect(workload).to_be_visible()
        expect(unrelated).to_be_visible()
        reference(bar, 'Tags', tag['name'])
        shot(page, 'combined-rules-tag-reference', full_page=False)
        expect(page.locator('.rt-item').filter(has_text='Workload')).to_be_visible()
        expect(unrelated).to_have_count(0)
        scopes = {x['value']: x['label'] for x in s.SEARCH_SCOPES}
        option(bar, 'Search in', scopes['self'])
        expect(workload).to_have_count(0)
        option(bar, 'Search in', scopes['related'])
        expect(workload).to_be_visible()
        expect(unrelated).to_have_count(0)
        bar.get_by_role('button', name='Search the text instead of the picked tags record').click()
        option(bar, 'Match', next(x['label'] for x in s.SEARCH_MODES if x['value'] == 'exact'))
        expect(bar.get_by_role('searchbox')).to_have_value(tag['name'])
        expect(workload).to_be_visible()
        expect(unrelated).to_have_count(0)
        shot(page, 'combined-rules-tag-related-text', full_page=False)
        option(bar, 'Search in', scopes['self'])
        expect(page.locator('.rt-item')).to_have_count(0)
        # Reset keeps legacy plain-text search: policy tags are related records only with explicit options.
        options(bar).get_by_role('button', name='Reset search options', exact=True).click()
        expect(page.locator('.rt-item')).to_have_count(0)
        bar.get_by_role('searchbox').fill('')
        expect(workload).to_be_visible()
        expect(unrelated).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_branding_raster_save_history_undo_and_invalid_guard(browser, live_server, h, width, scheme):
    original = h.get('staff', '/api/branding')
    context, page = sign_in(browser, live_server, 'admin', {'width': width, 'height': 900}, color_scheme=scheme)
    try:
        navigate(page, 'configuration/department')
        panel = page.locator('.brand-settings')
        panel.get_by_role('button', name='Edit', exact=True).click()
        dialog = page.get_by_role('dialog', name='Name and logo', exact=True)
        dialog.get_by_label('App name', exact=True).fill('Cedar clinical rota')
        dialog.get_by_label('Short name', exact=True).fill('Cedar')
        dialog.get_by_label('Reason for the change', exact=True).fill('Fictional acceptance branding')
        upload = dialog.get_by_label('Emblem image', exact=True)
        upload.set_input_files({'name': 'invalid.svg', 'mimeType': 'image/svg+xml', 'buffer': b'<svg/>'})
        dialog.get_by_role('button', name='Save', exact=True).click()
        assert not upload.evaluate('(input)=>input.checkValidity()')
        expect(upload).to_have_attribute('aria-invalid', 'true')
        error = dialog.locator('.brand-pick').first.locator('.field-error[data-auto]')
        expect(error).to_be_visible()
        error_id = error.get_attribute('id')
        assert error_id in upload.get_attribute('aria-describedby').split()
        assert h.get('staff', '/api/branding') == original
        page.evaluate('''() => {const decode=Image.prototype.decode;Image.prototype.decode=function(){
            if(!this.src.startsWith('data:'))return decode.call(this);
            return new Promise((resolve,reject)=>{window.releaseCedarImage=()=>decode.call(this).then(resolve,reject);});};}''')
        upload.set_input_files({'name': 'cedar.png', 'mimeType': 'image/png', 'buffer': raster()})
        dialog.get_by_role('button', name='Save', exact=True).click()
        assert not upload.evaluate('(input)=>input.checkValidity()')
        expect(upload).to_have_attribute('aria-invalid', 'true')
        expect(error).to_contain_text('Checking the image')
        assert h.get('staff', '/api/branding') == original
        page.wait_for_function('()=>typeof window.releaseCedarImage === "function"')
        page.evaluate('()=>window.releaseCedarImage()')

        def valid_logo():
            expect(upload).to_have_js_property('validationMessage', '')
            assert upload.get_attribute('aria-invalid') is None
            expect(error).to_be_hidden()
            assert error_id not in (upload.get_attribute('aria-describedby') or '').split()

        valid_logo()
        expect(dialog.locator('.brand-pick').first.locator('img').first).to_have_attribute('src', re.compile('^data:image/png'))
        shot(page, f'combined-branding-preview-{width}-{scheme}', full_page=False)
        # Reset a pending replacement; its late failed decode must not revive the field's old error or preview.
        page.evaluate('()=>window.releaseCedarImage=null')
        upload.set_input_files({'name': 'late-invalid.png', 'mimeType': 'image/png', 'buffer': b'not a raster'})
        page.wait_for_function('()=>typeof window.releaseCedarImage === "function"')
        dialog.get_by_role('button', name='Save', exact=True).click()
        assert not upload.evaluate('(input)=>input.checkValidity()')
        expect(upload).to_have_attribute('aria-invalid', 'true')
        assert h.get('staff', '/api/branding') == original
        dialog.locator('.brand-pick').first.get_by_role('button', name='Use default', exact=True).click()
        valid_logo()
        assert upload.evaluate('(input)=>input.files.length') == 0
        page.evaluate('()=>window.releaseCedarImage()')
        valid_logo()
        expect(dialog.locator('.brand-pick').first.locator('img').first).to_have_attribute('src', original['emblem_url'])
        shot(page, f'combined-branding-reset-{width}-{scheme}', full_page=False)
        page.evaluate('()=>window.releaseCedarImage=null')
        upload.set_input_files({'name': 'cedar.png', 'mimeType': 'image/png', 'buffer': raster()})
        page.wait_for_function('()=>typeof window.releaseCedarImage === "function"')
        page.evaluate('()=>window.releaseCedarImage()')
        valid_logo()
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(dialog).to_have_count(0)
        expect(panel).to_contain_text('Cedar clinical rota')
        page.reload()
        expect(panel).to_contain_text('Cedar clinical rota')
        assert h.get('staff', '/api/branding')['short_name'] == 'Cedar'
        panel.get_by_role('button', name='History', exact=True).click()
        history = page.get_by_role('dialog', name='Name and logo history', exact=True)
        expect(history).to_contain_text('Fictional acceptance branding')
        shot(page, f'combined-branding-history-{width}-{scheme}', full_page=False)
        history.get_by_role('button', name='Close dialog', exact=True).click()
        panel.get_by_role('button', name='Undo last change', exact=True).click()
        confirm(page, 'Undo the last name and logo change', 'Undo change', 'Restore fictional presentation')
        expect(panel).to_contain_text(original['app_name'])
        page.reload()
        assert {k: v for k, v in h.get('staff', '/api/branding').items() if k != 'revision'} == {
            k: v for k, v in original.items() if k != 'revision'}
    finally:
        context.close()


def test_stalled_public_branding_does_not_block_signin(browser, live_server):
    context = new_context(browser)
    held = []
    page = context.new_page()
    page.route('**/api/branding', lambda route: held.append(route))
    try:
        page.goto(live_server)
        expect(page.get_by_role('button', name='Continue', exact=True)).to_be_visible()
        expect(page.get_by_label('Username or email', exact=True)).to_be_visible()
        assert held
        shot(page, 'combined-branding-stalled-signin', full_page=False)
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'dark'), (390, 'light')])
def test_saved_layers_reload_and_explicit_route_precedence(browser, live_server, h, width, scheme):
    w = h.shift(h.workspace('Cedar layer draft'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900}, color_scheme=scheme)
    try:
        # Leave and wishes is People's layer (D80: offered where drawn; the grid has a property instead).
        navigate(page, f"schedule/people/2026-10-04?edit={w['id']}")
        bar = page.locator('.planner-toolbar')
        layers = view_settings(bar, 'Layers')
        layers.get_by_label('Leave and wishes', exact=True).uncheck()
        layers.get_by_role('radio', name='Overlay', exact=True).click()
        close_popover(page)
        save_view(page, bar, 'Cedar layers')
        saved = h.get('coordinator', '/api/table-views?table=planner')['views'][0]['definition']['planner']['layers']
        assert saved == {'draft': True, 'leave': False, 'mine': False, 'mode': 'overlay'}
        page.reload()
        layers = view_settings(bar, 'Layers')
        expect(layers.get_by_label('Leave and wishes', exact=True)).not_to_be_checked()
        expect(layers.get_by_role('radio', name='Overlay', exact=True)).to_have_attribute('aria-checked', 'true')
        shot(page, f'combined-layers-{width}-{scheme}', full_page=False)
        close_popover(page)
        navigate(page, f"schedule/people/2026-10-04?edit={w['id']}&leave=1&mode=side")
        layers = view_settings(bar, 'Layers')
        expect(layers.get_by_label('Leave and wishes', exact=True)).to_be_checked()
        expect(layers.get_by_role('radio', name='Side by side', exact=True)).to_have_attribute('aria-checked', 'true')
    finally:
        context.close()


def choose_group(page, panel, field, label):
    auto_setup(panel)  # D76: who to schedule opens under the setup line's Change
    panel.get_by_role('button', name=re.compile('^People to schedule:')).click()
    group = page.get_by_role('dialog', name='People to schedule', exact=True)
    add_filter(group)
    pick(page, group.locator('.cond-line').first, field, [label])
    group.get_by_role('button', name='Apply', exact=True).click()


def run_pass(page, h, w):
    queued = []
    def remember(request):
        if request.url.endswith('/api/commands') and request.method == 'POST':
            body = request.post_data_json
            if body.get('kind') == 'job.create':
                queued.append(body['payload'])
    page.on('request', remember)
    try:
        _review(page, h, w)
    finally:
        page.remove_listener('request', remember)
    jobs = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']
    latest = h.get('coordinator', '/api/jobs/' + jobs[0]['id'])['job']
    assert queued and queued[0]['diagnostic_proposal'] is False
    assert latest['generation_scope'] == queued[0]['generation_scope']
    assert latest['generation_scope']['protection'] == 'keep_existing'
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Job, latest['id'])
        for key in ('generation_scope', 'generation_scope_source'):
            assert stored.result[key] == stored.input_snapshot[key]
    if latest.get('diagnostic_proposal'):
        assert latest['result']['status'] == 'candidate_with_gaps'
    return latest


def apply_pass(page, h):
    """Apply, then the worker's part: the complete check Apply queues (maintenance) finishes before the panel goes on."""
    joined = _joins(page)
    with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                              and r.request.post_data_json.get('kind') == 'generation.apply') as reply:
        page.locator('#auto-panel').get_by_role('button', name=re.compile(r'^Apply \d+ changes?$')).click()
    assert reply.value.ok, reply.value.text()
    _check_after_apply(page, h, reply.value.json()['workspace']['id'], joined)
    return reply.value.json()


def test_two_pass_group_preserves_supervisor_fields_and_normal_review_guards(browser, cp_server, h):
    clinician = h.seed['people'][1]
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    typ = h.configure('assignment_types', {'name': 'Cedar supervised clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 3}], 'supervision': {
            'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
            'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']
    w = h.shift(h.workspace('Cedar two passes'), typ=typ['id'], people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator')
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        choose_group(page, panel, 'Person', names[clinician])
        auto_setup(panel)  # D76: the setup's fields open under Change
        expect(panel.get_by_label('Keep existing assignments', exact=True)).to_be_checked()
        run_pass(page, h, w)
        shot(page, 'combined-population-first-pass', full_page=False)
        apply_pass(page, h)
        current = h.detail(w)
        original = deepcopy(current['shifts'][0]['participants'])
        assert len(original) == 1 and original[0]['person_id'] == clinician
        h.lifecycle(current['workspace'], 'submit', status=422)
        trainee = h.configure('people', {'display_name': 'Cedar later trainee', 'tags': ['Cedar later intake']})['record']['id']
        h.command('coordinator', 'membership.create', {'person_id': trainee, 'category_id': h.seed['trainee_category'], 'starts_on': '2026-10-04'})
        h.command('credentialer', 'eligibility.create', {'person_id': trainee, 'service_id': h.seed['services'][0], 'starts_on': '2026-10-04'})
        panel.get_by_role('button', name='Schedule another group…', exact=True).click()
        group = page.get_by_role('dialog', name='People to schedule', exact=True)
        group.get_by_role('button', name='Apply', exact=True).click()
        expect(panel.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_disabled()
        page.reload()
        open_rota(page, w['id'])
        open_scheduling(page)
        choose_group(page, panel, 'Person', 'Cedar later trainee')
        run_pass(page, h, w)
        shot(page, 'combined-population-second-pass', full_page=False)
        apply_pass(page, h)
        current = h.detail(w)
        after = current['shifts'][0]['participants']
        assert [p for p in after if p['person_id'] == clinician] == original
        assert next(p for p in after if p['person_id'] == trainee)['supervisor_person_id'] == clinician
        assert current['workspace']['validation']['valid']
        submitted = h.lifecycle(current['workspace'], 'submit')['workspace']
        approved = h.lifecycle(submitted, 'approve', 'approver')['workspace']
        h.lifecycle(approved, 'publish')
        assert h.get('coordinator', '/api/publications')['publications']
    finally:
        context.close()


def test_staff_schedule_search_offers_no_directory_reference(browser, live_server):
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'schedule/week/2026-10-04')
        panel = options(page.locator('.planner-toolbar'))
        expect(panel.get_by_role('menu', name='Find a record')).to_have_count(0)
        shot(page, 'combined-staff-search-own-options', full_page=False)
    finally:
        context.close()


def test_missing_group_capability_hides_group_control(browser, cp_server, h):
    w = h.shift(h.workspace('Cedar legacy capability'), people=[])['workspace']
    body = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])
    body['generation_capability'].pop('generation_scope', None)
    context, page = sign_in(browser, cp_server, 'coordinator')
    page.route('**/api/jobs?workspace_id=' + w['id'], lambda route: route.fulfill(json=body))
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        expect(panel.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_visible()
        auto_setup(panel)
        expect(panel.get_by_role('button', name=re.compile('^People to schedule:'))).to_have_count(0)
    finally:
        context.close()

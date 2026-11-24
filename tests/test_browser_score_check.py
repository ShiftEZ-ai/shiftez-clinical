"""Schedule › Compare your rota with Rota (score_check.ts) and Configuration › AI provider (ai_provider.ts), on real data:
a coordinator uploads a real CSV (multipart), decides every Check item, saves the imported draft and starts Rota's
same-session auto-schedule; Compare reads two real drafts after a real CP-SAT run. Approvers read Compare only; staff
are refused. States: empty, failure + Retry, populated, department scale, read-only; 1440 in light and dark.

Only the external AI provider is stubbed (score_check_provider.test_connection and .extract, in this process where
the worker runs); Rota's own API is never mocked. A forced 503 tests Retry."""
import csv
import io
import json
import re
from dataclasses import replace

import pytest
from playwright.sync_api import expect
from apps.api import models as m
from apps.api.db import transaction
from apps.api.main import create_app
from .governance_support import developer, identity
from .test_browser_operator_governance_actions import command_response
from .test_browser import browser, live_server, sign_in, navigate, open_config, department_tab, route_fetch, expect_loaded  # noqa: F401
from .test_score_check_benchmark import imported
from .test_score_check_frontend_api import upload as seed_upload
from .test_score_check_provider import _pdf as synthetic_pdf

pytestmark = pytest.mark.browser


def score_checking(h, on):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).score_check_enabled = on


@pytest.fixture(autouse=True)
def score_check_on(h):
    """Score checking uses the same CP-SAT engine in API, worker and browser server."""
    h.settings = replace(h.settings, generation_engine='cp_sat')
    engine = h.app.state.engine
    # Code that reads its settings from the engine (a bare process_one, preparation settings) must agree too.
    engine.update_execution_options(rota_write_settings=replace(
        engine.get_execution_options()['rota_write_settings'], generation_engine='cp_sat'))
    h.app = create_app(h.settings, engine=engine)
    h.clients.clear()  # none yet when this runs first; never keep one bound to the old app
    h.server_env = {'ROTA_GENERATION_ENGINE': 'cp_sat'}
    score_checking(h, True)
DESKTOP = {'width': 1440, 'height': 1000}
ROWS = [['Staff 01(test)', '2026-10-04', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
        ['Staff 02(ttest)', '2026-10-05', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
        ['Unknown Visitor', '2026-10-06', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
        ['Staff 01(test)', 'soon', 'Clinical review session', 'Continuity clinic', '08:00', '12:00']]


def rota_csv(rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['Group', 'Residents'])
    writer.writerow(['Person', 'Date', 'Code', 'Clinic', 'Start', 'End'])
    writer.writerows(rows)
    return out.getvalue().encode()


def open_page(browser, live_server, role, scheme='light'):
    context, page = sign_in(browser, live_server, role, viewport=DESKTOP)
    page.emulate_media(color_scheme=scheme)
    return context, page


def no_overflow(page):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')


def compared(h):
    """An imported draft and Rota's same-session draft, solved by the real worker and applied."""
    from apps.worker.main import process_one
    source = imported(h)
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        department.generation_defaults = {**(department.generation_defaults or {}), 'search_minutes': 1, 'stop_without_improvement_seconds': 1}
    queued = h.command('coordinator', 'score_check.auto', {'workspace_id': source['id']}, revision=source['revision'])
    auto = queued['workspace']
    assert process_one(h.app.state.engine, solver_workers=1, settings=h.settings)
    h.command('coordinator', 'generation.apply', {'workspace_id': auto['id'], 'job_id': queued['job']['id'],
              'reason': 'Benchmark browser test'}, revision=auto['revision'])
    return source


def pick_first(page):
    page.get_by_role('option').first.click()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_coordinator_uploads_checks_saves_and_starts_the_auto_schedule(browser, live_server, h, scheme):
    context, page = open_page(browser, live_server, 'coordinator', scheme)
    try:
        navigate(page, 'schedule/benchmark/upload')
        main = page.locator('#main')
        # Empty: one title, the steps, one big drop zone; Continue waits for files; no provider means spreadsheets only.
        expect(main.get_by_role('heading', name='Compare your rota with Rota')).to_be_visible()
        expect(page.get_by_role('list', name='Steps').locator('[aria-current=step]')).to_have_text(re.compile('Upload'))
        expect(page.get_by_role('button', name='Continue')).to_be_disabled()
        expect(main.get_by_role('button', name=re.compile('Drop your rota files here'))).to_contain_text('Excel or CSV files.')
        no_overflow(page)
        main.locator('input[type=file]').set_input_files({'name': 'residents.csv', 'mimeType': 'text/csv', 'buffer': rota_csv(ROWS)})
        # Populated: what was understood, in words, with inline edits.
        files = main.get_by_role('region', name='Your files')
        expect(files.get_by_text('residents.csv')).to_be_visible()
        expect(files.get_by_role('button', name='Group: Residents. Change')).to_be_visible()
        expect(files).to_contain_text('Read without AI: CSV and Excel in the standard layout')  # {profiles: []}
        expect(main.get_by_role('button', name=re.compile('^Read by'))).to_have_count(0)
        files.get_by_role('button', name=re.compile(r'^Month: .*\. Change$')).click()
        page.get_by_role('option', name='October 2026').click()
        expect(files.get_by_role('button', name='Month: October 2026. Change')).to_be_visible()
        expect(page.locator('.app-footer')).to_contain_text('Not saved')
        page.get_by_role('button', name='Continue').click()
        # Check: each section decided with its own action; Save waits for every alarm.
        expect(main.get_by_role('heading', name=re.compile('need your decision'))).to_be_visible()
        save = page.get_by_role('button', name='Save as draft')
        expect(save).to_be_disabled()
        unknown = main.get_by_role('region', name='Not in Rota')
        unknown.locator('.bm-row', has_text='Unknown Visitor').get_by_role('button', name='Add Unknown Visitor as person').click()
        add = page.get_by_role('dialog', name='Add Unknown Visitor')
        expect(add.get_by_label('Display name')).to_have_value('Unknown Visitor')
        add.get_by_role('button', name='Add', exact=True).click()
        expect(unknown.locator('.bm-row', has_text='Unknown Visitor')).to_contain_text('Added')
        unsure = main.get_by_role('region', name='Unsure matches')
        unsure.locator('.bm-row', has_text='Staff 02(ttest)').get_by_role('button', name='Confirm').click()
        expect(unsure.locator('.bm-row', has_text='Staff 02(ttest)').get_by_role('button', name='Undo')).to_be_visible()
        cells = main.get_by_role('region', name='Cells not read clearly')
        cells.get_by_role('button', name='Skip').first.click()
        expect(cells.get_by_role('button', name='Undo').first).to_be_visible()
        # A clinic is a service at a location: Confirm (or Match…) asks for the location too.
        clinic = main.locator('.bm-row', has=page.locator('.bm-written', has_text=re.compile('^Continuity clinic$')))
        if clinic.count():
            if clinic.get_by_role('button', name='Confirm').count():
                clinic.get_by_role('button', name='Confirm').click()
            else:
                clinic.get_by_role('button', name='Match…').click()
                pick_first(page)  # the service
            pick_first(page)  # its location
            expect(clinic.get_by_role('button', name='Undo')).to_be_visible()
        missing = main.get_by_role('region', name='Missing from their files')
        while missing.get_by_role('button', name='Acknowledge').count():
            before = missing.get_by_role('button', name='Acknowledge').count()
            missing.get_by_role('button', name='Acknowledge').first.click()
            expect(missing.get_by_role('button', name='Acknowledge')).to_have_count(before - 1)
        expect(page.locator('#bm-alarms')).to_have_text('All acknowledged')
        expect(save).to_be_enabled()
        save.click()
        # Compare: no Rota draft yet → Auto-schedule now creates it and opens it in the Planner.
        expect(main.get_by_text('No auto-schedule draft yet')).to_be_visible()
        page.get_by_role('button', name='Auto-schedule now').click()
        page.wait_for_url(re.compile(r'#schedule/.*edit='))
        with transaction(h.app.state.engine) as db:
            assert db.query(m.Workspace).filter_by(origin='auto').count() == 1
        no_overflow(page)
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_compare_shows_the_verdict_head_to_head_and_details_on_real_drafts(browser, live_server, h, scheme):
    source = compared(h)
    context, page = open_page(browser, live_server, 'coordinator', scheme)
    try:
        navigate(page, 'schedule/benchmark/compare?draft=' + source['id'])
        main = page.locator('#main')
        expect(main.get_by_role('heading', name=re.compile('scores higher|nothing to fix|Both rotas score the same'))).to_be_visible()
        duel = main.get_by_role('region', name=re.compile('compared with'))
        expect(duel.get_by_role('radio')).to_have_count(2)
        expect(page.get_by_role('button', name=re.compile(r'^(Submit for review…|Resolve \d+ blocking)$'))).to_have_count(1)
        duel.get_by_role('radio').first.check()
        expect(duel.get_by_role('radio').first).to_be_checked()
        for tab in ('Fairness', 'Gaps', 'All details', 'Rules'):
            main.get_by_role('tab', name=re.compile('^' + tab)).click()
            expect(main.get_by_role('tabpanel')).to_be_visible()
        expect(main.get_by_role('tab', name='All details')).to_have_text('All details')  # no empty count pill
        no_overflow(page)
        with transaction(h.app.state.engine) as db:
            auto = db.query(m.Workspace).filter_by(origin='auto').one().id
        page.get_by_role('button', name='Adjust & re-run').click()
        page.get_by_role('menuitem', name='Re-run auto-schedule').click()
        page.wait_for_url(re.compile('edit=' + auto))
        with transaction(h.app.state.engine) as db:
            assert db.query(m.Workspace).filter_by(origin='auto').count() == 1  # no second copy
    finally:
        context.close()


def test_department_scale_check_lists_stay_usable(browser, live_server, h):
    seeded = seed_upload(h, [[f'Visitor {i}', '2026-10-04', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'] for i in range(300)])
    context, page = open_page(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/benchmark/check?import=' + seeded['id'])
        unknown = page.get_by_role('region', name='Not in Rota')
        expect(unknown.locator('.bm-row')).to_have_count(4)
        unknown.get_by_role('button', name=re.compile(r'^Show all \d+')).click()
        expect(unknown.locator('.bm-row')).to_have_count(300)
        no_overflow(page)
    finally:
        context.close()


def test_failure_offers_retry(browser, live_server, h):
    """The one fault injection in this file: a single Check read answers 503 at the browser's network layer (the server
    cannot be made to fail one read on demand), then the route is removed and Retry reads the real Check."""
    seeded = seed_upload(h, ROWS)
    context, page = open_page(browser, live_server, 'coordinator')
    try:
        def fail(route):
            route.fulfill(status=503, content_type='application/json',
                          body=json.dumps({'error': {'code': 'SERVER_ERROR', 'message': 'The service had a problem.'}}))
        page.route(re.compile(r'.*/api/score-imports/[^/]+/check$'), fail)
        page.evaluate("id => { location.hash = 'schedule/benchmark/check?import=' + id; }", seeded['id'])
        expect(page.locator('#main').get_by_text(re.compile(r'^Couldn’t load the score check\.'))).to_be_visible()
        page.unroute(re.compile(r'.*/api/score-imports/[^/]+/check$'))
        page.get_by_role('button', name='Retry').click()
        expect(page.locator('#main').get_by_role('heading', name=re.compile('need your decision'))).to_be_visible()
    finally:
        context.close()


def test_approver_reads_compare_only_and_staff_is_refused(browser, live_server, h):
    source = imported(h)
    context, page = open_page(browser, live_server, 'approver', 'dark')
    try:
        navigate(page, 'schedule/benchmark/compare?draft=' + source['id'])
        expect(page.locator('#main').get_by_text('No auto-schedule draft yet')).to_be_visible()
        expect(page.get_by_role('button', name='Auto-schedule now')).to_have_count(0)
        expect(page.get_by_role('button', name='Submit for review…')).to_have_count(0)
        navigate(page, 'schedule/benchmark/upload')
        expect(page.locator('#main').get_by_role('heading', name='Restricted page')).to_be_visible()
    finally:
        context.close()
    context, page = open_page(browser, live_server, 'staff')
    try:
        page.evaluate("id => { location.hash = 'schedule/benchmark/compare?draft=' + id; }", source['id'])
        expect(page.locator('#main').get_by_role('heading', name='Restricted page')).to_be_visible()
    finally:
        context.close()


KEY = '17' * 32  # FMUC_DATA_KEY for the test server and this process (the worker), so saved provider keys decrypt.
TEST_API_KEY = 'fictional-provider-key'  # a made-up value; the provider is never called (see the stubs below)


@pytest.fixture
def ai_ready(h):
    """Before the live server starts: the data key on both sides, and an admin with Developer access."""
    h.server_env = {**getattr(h, 'server_env', {}), 'FMUC_DATA_KEY': KEY}
    object.__setattr__(h.settings, 'fmuc_data_key', KEY)
    developer(h)


def run_worker(h):
    from apps.worker import score_check as worker
    assert worker.process_one(h.app.state.engine, h.settings)


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_developer_sets_up_tests_and_removes_a_provider_and_admin_does_not_see_it(ai_ready, browser, live_server, h, scheme, monkeypatch):
    from apps.api.services import score_check_provider as provider

    def answered(profile, *, settings, audit):  # the external provider's HTTP, stubbed at the backend boundary
        event = {'attempt': 1, 'profile_id': profile.id, 'kind': profile.kind, 'model': profile.model, 'file_sha256': 'a' * 64}
        audit({**event, 'result': 'started'})
        audit({**event, 'result': 'valid', 'input_tokens': 2, 'output_tokens': 1, 'duration_ms': 30})
        return {'ok': True, 'usage': {'input_tokens': 2, 'output_tokens': 1}}
    monkeypatch.setattr(provider, 'test_connection', answered)
    context, page = open_page(browser, live_server, 'developer', scheme)
    try:
        navigate(page, 'configuration/ai-provider')
        main = page.locator('#main')
        expect(main.get_by_text('No AI provider yet')).to_be_visible()
        switch = main.get_by_role('switch', name='Read rota files with AI')
        expect(switch).not_to_be_checked()
        switch.check()
        expect(page.get_by_role('status').filter(has_text='Reading with AI is on.')).to_be_visible()
        page.get_by_role('button', name='Add provider').click()
        dialog = page.get_by_role('dialog', name='Add provider')
        dialog.get_by_label('Name', exact=True).fill('Synthetic provider')
        dialog.get_by_label('Model').fill('synthetic-model')
        dialog.get_by_label('API key').fill(TEST_API_KEY)
        expect(dialog).to_contain_text('Sends files and staff names to Anthropic (api.anthropic.com)')
        dialog.get_by_role('button', name='Add provider').click()
        row = main.get_by_role('row', name=re.compile('^Synthetic provider'))
        expect(row).to_contain_text('Saved')
        assert TEST_API_KEY not in page.content()  # the key is never sent back
        with page.expect_response(command_response('ai_provider.test')) as queued:
            row.get_by_role('button', name='Test').click()
        assert queued.value.ok, queued.value.text()
        with transaction(h.app.state.engine) as db:  # the queued, audited test job
            assert db.query(m.ScoreCheckExtractionJob).filter_by(kind='test').count() == 1
        run_worker(h)
        expect(page.get_by_role('status').filter(has_text='Synthetic provider answered.')).to_be_visible(timeout=10000)
        # Read the page again (navigate() to the address already shown draws nothing): Recent AI calls lists the test.
        page.reload()
        expect_loaded(page, 'configuration/ai-provider')
        expect(main.get_by_role('table').last).to_contain_text('Valid')  # Recent AI calls
        row = main.get_by_role('row', name=re.compile('^Synthetic provider'))
        row.get_by_role('button', name='Delete').click()
        expect(main.get_by_text('No AI provider yet')).to_be_visible()
        page.get_by_role('status').get_by_role('button', name='Undo').click()  # ai_provider.restore, from the shared Undo
        restored = main.get_by_role('row', name=re.compile('^Synthetic provider'))
        expect(restored).to_contain_text('Off')  # back, but disabled and not default
        expect(restored).to_contain_text('Saved')  # its key was kept
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'configuration/department')
        expect(page.locator('.config-nav').get_by_role('button', name='AI provider', exact=True)).to_have_count(0)
    finally:
        context.close()


def page_extraction(prepared):
    """A provider's reply for one PDF page or photo, built from the generated RotaExtraction contract (synthetic names only)."""
    from packages.contracts import schemas as contracts
    ref, file_ref = sorted(prepared.cell_refs)[0], prepared.file_ref
    said = {'file_ref': file_ref, 'cell_refs': [ref], 'confidence': 0.95, 'reason': 'Synthetic page reading'}
    value = {'files': [{'file_ref': file_ref, 'group': 'Residents', 'layout': 'sessions_list', 'rows_read': 1, 'confidence': 0.95,
                        'starts_on': '2026-10-04', 'ends_on': '2026-10-05'}],
             'people': [{'as_written': 'Staff 01(test)', **said}],
             'duty_codes': [{'code': 'Clinical review session', 'kind': 'duty', **said}],
             'clinics': [{'as_written': 'Continuity clinic', **said}],
             'assignments': [{'person': 'Staff 01(test)', 'date': '2026-10-04', 'file_ref': file_ref, 'cell_ref': ref,
                              'duty_code': 'Clinical review session', 'clinic': 'Continuity clinic', 'start': '08:00', 'end': '12:00'}],
             'unparsed': [], 'warnings': []}
    return contracts.RotaExtraction.model_validate(value).model_dump(mode='json', exclude_none=True)


def synthetic_png():
    """A tiny real photo, made as the backend's image-preparation test makes it (PIL)."""
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGB', (3, 2), 'white').save(out, format='PNG')
    return out.getvalue()


@pytest.mark.parametrize('name,mime,make,ref', [
    ('residents.pdf', 'application/pdf', lambda: synthetic_pdf('Staff 01(test) 4 Oct Clinical review session'), 'PDF!P1'),
    ('residents.png', 'image/png', synthetic_png, 'Image!1')], ids=['pdf', 'photo'])
def test_coordinator_sees_where_files_go_before_uploading_and_a_pdf_or_photo_is_read(ai_ready, browser, live_server, h, monkeypatch,
                                                                                       name, mime, make, ref):
    from apps.api.services import score_check_provider as provider
    # A provider on a non-default port: the privacy line must keep host:port as the server gives it.
    saved = h.command('operator', 'ai_provider.save', {'name': 'Synthetic provider', 'kind': 'openai_chat', 'model': 'synthetic-model',
                      'base_url': 'https://example.com:8443/v1', 'api_key': TEST_API_KEY, 'enabled': True, 'is_default': True})['profile']
    with transaction(h.app.state.engine) as db:
        revision = db.get(m.Department, h.seed['department_id']).revision
    h.command('operator', 'ai_provider.enabled', {'enabled': True}, revision=revision)
    menu = h.get('coordinator', '/api/score-imports/providers')
    assert menu == {'profiles': [{'id': saved['id'], 'revision': saved['revision'], 'name': 'Synthetic provider', 'kind': 'openai_compatible',
                                  'base_url': 'example.com:8443', 'model': 'synthetic-model', 'is_default': True}]}

    def extract(profile, prompt, files, schema, *, settings, audit, validate):  # the provider's HTTP, stubbed at the boundary
        assert files[0].cell_refs == frozenset({ref})  # the real PDF page or photo reached the worker
        event = {'attempt': 1, 'profile_id': profile.id, 'kind': profile.kind, 'model': profile.model, 'file_sha256': files[0].sha256}
        audit({**event, 'result': 'started'})
        result = validate(page_extraction(files[0]))
        audit({**event, 'result': 'valid', 'duration_ms': 1})
        return result, {}
    monkeypatch.setattr(provider, 'extract', extract)
    context, page = open_page(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/benchmark/upload')
        main = page.locator('#main')
        # Before anything is sent: who reads the files and where they go (host:port kept); PDFs and photos are then accepted.
        expect(main.get_by_role('button', name=re.compile('Read by Synthetic provider'))).to_be_visible()
        expect(main).to_contain_text('Sends files and staff names to example.com:8443')
        expect(main.get_by_role('button', name=re.compile('Drop your rota files here'))).to_contain_text('Excel, CSV, PDF or photos.')
        assert '.pdf' in main.locator('input[type=file]').get_attribute('accept')
        # Without AI stays one choice away.
        main.get_by_role('button', name=re.compile('Read by Synthetic provider')).click()
        expect(page.get_by_role('option', name=re.compile('Without AI'))).to_be_visible()
        page.keyboard.press('Escape')
        main.locator('input[type=file]').set_input_files({'name': name, 'mimeType': mime, 'buffer': make()})
        expect(page.get_by_role('button', name='Reading 1 file…')).to_be_disabled()
        expect(main.get_by_role('status').filter(has_text='Reading')).to_be_visible()
        with transaction(h.app.state.engine) as db:  # consent was recorded with the provider's current revision
            batch = db.query(m.ScoreCheckImportBatch).order_by(m.ScoreCheckImportBatch.created_at.desc()).first()
            assert batch.use_ai and batch.privacy_acknowledged and batch.profile_revision == saved['revision']
        # A poll that arrives while a popover is open keeps it anchored and focused (the row and the Time button are patched,
        # never replaced): the Time popover across one poll, then a Group picker across the file finishing its read.
        main.get_by_role('button', name='Add time').click()
        timing = page.get_by_role('dialog', name='Time it took')
        expect(timing.get_by_label('Work hours in total')).to_be_focused()
        with page.expect_response(lambda r: '/api/score-imports/' in r.url and r.request.method == 'GET'):
            pass
        expect(timing).to_be_visible()
        expect(timing.get_by_label('Work hours in total')).to_be_focused()
        page.keyboard.press('Escape')
        group = main.get_by_role('button', name=re.compile(r'^Group: .*\. Change$'))
        group.click()
        picking = page.get_by_role('dialog', name='Group')
        expect(picking).to_be_visible()
        run_worker(h)
        expect(main.get_by_text('Ready')).to_be_visible(timeout=10000)
        expect(picking).to_be_visible()  # still open, still anchored to the same button
        assert group.evaluate('b => b.isConnected')
        page.keyboard.press('Escape')
        expect(main.get_by_role('region', name='Your files')).to_contain_text(name)
        expect(page.get_by_role('button', name='Continue')).to_be_enabled()
    finally:
        context.close()


@pytest.mark.parametrize('role', ['approver', 'programme_lead', 'auditor'])
def test_read_roles_see_a_populated_compare_read_only(browser, live_server, h, role):
    if role == 'programme_lead':
        identity(h, role, [role])
    source = compared(h)
    context, page = open_page(browser, live_server, role)
    try:
        navigate(page, 'schedule/benchmark/compare?draft=' + source['id'])
        main = page.locator('#main')
        duel = main.get_by_role('region', name='Your rota compared with Rota')
        expect(duel.get_by_role('radio')).to_have_count(2)
        expect(main.get_by_role('note').filter(has_text='Read only')).to_be_visible()
        for name in ('Submit for review…', 'Auto-schedule now', 'Adjust & re-run'):
            expect(page.get_by_role('button', name=name)).to_have_count(0)
        expect(page.get_by_role('button', name=re.compile(r'^Resolve \d+ blocking$'))).to_have_count(0)
        main.get_by_role('tab', name=re.compile('^Rules')).focus()
        page.keyboard.press('ArrowRight')
        expect(main.get_by_role('tab', name=re.compile('^Fairness'))).to_be_focused()  # the shared tabs' arrow keys
    finally:
        context.close()


def test_verdict_and_period_words_are_honest(browser, live_server, h):
    """The verdict never names a winner on a tie, unknown scores or a higher score with more blocking issues, and a
    period never shows its exclusive end. The page's own module is called (no API involved)."""
    source = compared(h)
    context, page = open_page(browser, live_server, 'coordinator')
    try:
        words = page.evaluate("""async () => {
            const m = await window.importBundle('score_check.js');
            const side = (origin, score, blocking) => ({origin, name: origin, score, blocking});
            const v = (a, b) => m.verdict(a, b);
            return {
              tie: v(side('import', 80, 0), side('auto', 80, 0)),
              unknown: v(side('import', null, 0), side('auto', 90, 0)),
              mixed: v(side('import', 50, 0), side('auto', 90, 3)),
              ours: v(side('import', 59, 14), side('auto', 84.6, 0)),
              theirs: v(side('import', 90, 0), side('auto', 70, 0)),
              month: m.periodLabel('2026-11-01', '2026-12-01'),
              day: m.periodLabel('2026-10-04', '2026-10-05'),
              span: m.periodLabel('2026-10-04', '2026-11-04'),
              names: [m.sideName({origin: 'import', name: 'Department (imported)'}), m.sideName({origin: 'auto', name: 'Auto-schedule 1'})],
            };
        }""")
        assert words['tie'] == {'winner': None, 'text': 'Both rotas score the same'}
        assert words['unknown'] == {'winner': None, 'text': 'The scores are not available yet'}
        assert words['mixed']['winner'] is None
        assert words['ours'] == {'winner': 1, 'text': 'Rota’s rota scores higher, with nothing to fix'}
        assert words['theirs'] == {'winner': 0, 'text': 'Your rota scores higher, with nothing to fix'}
        assert (words['month'], words['day'], words['span']) == ('November 2026', '4 Oct 2026', '4 Oct – 3 Nov 2026')
        assert words['names'] == ['Your rota', 'Rota']
        said = page.evaluate("""() => window.importBundle('scoreCheckApi.js').then(a => [
            ['anthropic', 'https://api.anthropic.com'], ['openai_compatible', 'api.openai.com.external.example'],
            ['openai_compatible', 'localhost.external.example:443'], ['openai_compatible', 'localhost:11434'],
            ['anthropic', 'https://evil.example/v1'], ['openai_compatible', '10.example.net']].map(([kind, base_url]) => a.sendsTo({kind, base_url})))""")
        assert said == ['Sends files and staff names to Anthropic (api.anthropic.com)',
                        'Sends files and staff names to api.openai.com.external.example',
                        'Sends files and staff names to localhost.external.example:443',
                        'Files stay on localhost:11434',
                        'Sends files and staff names to evil.example',
                        'Sends files and staff names to 10.example.net']
        bench = h.get('coordinator', '/api/benchmark?workspace=' + source['id'])
        label = page.evaluate("([a, b]) => window.importBundle('score_check.js').then(m => m.periodLabel(a, b))", [bench['period_start'], bench['period_end']])
        navigate(page, 'schedule/benchmark/compare?draft=' + source['id'])
        expect(page.locator('#main .bm-sub')).to_contain_text(label + '.')
    finally:
        context.close()


def test_leaving_upload_says_the_files_stay_and_remove_file_has_undo(browser, live_server, h):
    seeded = seed_upload(h, ROWS)
    context, page = open_page(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/benchmark/upload?import=' + seeded['id'])
        main = page.locator('#main')
        files = main.get_by_role('region', name='Your files')
        remove = files.get_by_role('button', name='Remove residents.csv')
        remove.click()
        expect(main.get_by_role('region', name='Your files')).to_have_count(0)
        page.get_by_role('status').get_by_role('button', name='Undo').click()
        expect(main.get_by_role('region', name='Your files').get_by_text('residents.csv')).to_be_visible()
        # The dirty-close guard: leaving keeps the uploaded files, and says so.
        page.locator('.topbar').get_by_role('link', name='Planner').click()
        guard = page.get_by_role('dialog', name='Leave this page?')
        expect(guard).to_contain_text('Your files stay in this import')
        guard.get_by_role('button', name=re.compile('^Go to')).click()
        expect(page).not_to_have_url(re.compile('benchmark'))
    finally:
        context.close()


def test_one_upload_even_when_its_reply_is_lost(browser, live_server, h):
    """A lost reply (the server committed, the browser never heard) and a retry of the same files is one import:
    the upload keeps its Idempotency-Key until it is answered. The reply is dropped at the network layer once."""
    context, page = open_page(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/benchmark/upload')
        lost = {'done': False}

        def drop_reply(route):
            if lost['done']:
                return route.continue_()
            lost['done'] = True
            route_fetch(route)  # the server receives and commits the upload (its Idempotency-Key makes a retry safe)
            route.abort('connectionreset')
        page.route(re.compile(r'.*/api/score-imports$'), drop_reply)
        file = {'name': 'residents.csv', 'mimeType': 'text/csv', 'buffer': rota_csv(ROWS)}
        page.locator('#main input[type=file]').set_input_files(file)
        expect(page.get_by_role('status').or_(page.get_by_role('alert')).filter(has_text=re.compile('Connection lost'))).to_be_visible()
        page.locator('#main input[type=file]').set_input_files(file)
        expect(page.locator('#main').get_by_role('region', name='Your files')).to_be_visible()
        with transaction(h.app.state.engine) as db:
            assert db.query(m.ScoreCheckImportBatch).count() == 1
    finally:
        context.close()


def test_editing_a_provider_keeps_what_was_not_edited(ai_ready, browser, live_server, h):
    saved = h.command('operator', 'ai_provider.save', {'name': 'Intranet provider', 'kind': 'openai_chat', 'model': 'synthetic-model',
                      'base_url': 'https://example.com/v1', 'structured_output': 'json_object', 'enabled': True})['profile']
    context, page = open_page(browser, live_server, 'developer')
    try:
        navigate(page, 'configuration/ai-provider')
        page.locator('#main').get_by_role('row', name=re.compile('^Intranet provider')).get_by_role('button', name='Edit').click()
        dialog = page.get_by_role('dialog', name='Edit Intranet provider')
        thinking = dialog.locator('select').filter(has=page.locator('option', has_text='Medium'))
        assert 'Token budget' not in ' '.join(thinking.locator('option').all_text_contents())  # never a setting the server refuses
        expect(dialog.get_by_role('checkbox', name='On the hospital network')).not_to_be_checked()  # an explicit choice
        dialog.get_by_label('Name', exact=True).fill('Intranet provider (renamed)')
        dialog.get_by_role('button', name='Save').click()
        expect(page.locator('#main').get_by_role('row', name=re.compile('^Intranet provider \\(renamed\\)'))).to_be_visible()
        profile = next(p for p in h.get('operator', '/api/ai-providers')['profiles'] if p['id'] == saved['id'])
        assert profile['structured_output'] == 'json_object' and profile['allow_private_endpoint'] is False
    finally:
        context.close()


def test_a_clinic_added_from_a_past_month_exists_for_that_month(browser, live_server, h):
    """A clinic added in Check starts on the import's earliest day (not today), so the past month's sessions there
    validate; a duty code offers only Match and Leave out."""
    seeded = seed_upload(h, [['Staff 01(test)', '2026-09-10', 'Clinical review session', 'Clinic 9', '08:00', '12:00'],
                             ['Staff 01(test)', '2026-09-17', 'Clinical review session', 'Clinic 9', '08:00', '12:00']])
    context, page = open_page(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/benchmark/check?import=' + seeded['id'])
        main = page.locator('#main')
        unknown = main.get_by_role('region', name='Not in Rota')
        row = unknown.locator('.bm-row', has=page.locator('.bm-written', has_text=re.compile('^Clinic 9$')))
        row.get_by_role('button', name='Add Clinic 9 as clinic').click()
        add = page.get_by_role('dialog', name='Add Clinic 9')
        expect(add.locator('form.bm-add-form')).to_be_visible()
        expect(add.get_by_label('Name')).to_have_value('Clinic 9')
        add.get_by_role('button', name=re.compile(r'^Location: ')).click()
        pick_first(page)
        add.get_by_role('button', name='Add', exact=True).click()
        expect(row).to_contain_text('Added')
        for code in main.locator('.bm-row.unknown').all():  # a duty code: Match and Leave out, no Add
            if code.get_by_text(re.compile('^Duty code in')).count():
                expect(code.get_by_role('button', name=re.compile('^Add '))).to_have_count(0)
        missing = main.get_by_role('region', name='Missing from their files')
        while missing.get_by_role('button', name='Acknowledge').count():
            before = missing.get_by_role('button', name='Acknowledge').count()
            missing.get_by_role('button', name='Acknowledge').first.click()
            expect(missing.get_by_role('button', name='Acknowledge')).to_have_count(before - 1)
        for leave in main.locator('.bm-row:not(.done)').get_by_role('button', name='Leave out').all():
            leave.click()
        page.get_by_role('button', name='Save as draft').click()
        expect(main.get_by_text('No auto-schedule draft yet')).to_be_visible()
        with transaction(h.app.state.engine) as db:
            clinic = db.query(m.Service).filter_by(name='Clinic 9').one()
            workspace = db.query(m.Workspace).filter_by(origin='import').one()
            assert str(clinic.starts_on) <= '2026-09-10'
            ws_id, revision = workspace.id, workspace.revision
        checked = h.command('coordinator', 'workspace.validate', {'reason': 'Check the imported rota'}, ws_id, revision)
        findings = (h.detail(ws_id)['workspace'].get('validation') or {}).get('findings') or checked.get('validation', {}).get('findings', [])
        assert not [f for f in findings if f.get('code') == 'SERVICE_INACTIVE']
    finally:
        context.close()


def test_an_oversized_file_is_refused_before_it_is_read_or_sent(browser, live_server, h):
    """The contract's limits (generated SCORE_CHECK_LIMITS) are checked before a file is read, hashed or uploaded."""
    from packages.contracts.schemas import SCORE_CHECK_LIMITS
    context, page = open_page(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/benchmark/upload')
        sent = []
        page.on('request', lambda r: sent.append(r.url) if '/api/score-imports' in r.url and r.method == 'POST' else None)
        big = {'name': 'huge.csv', 'mimeType': 'text/csv', 'buffer': b'x' * (SCORE_CHECK_LIMITS['file_bytes'] + 1)}
        page.locator('#main input[type=file]').set_input_files(big)
        expect(page.get_by_role('alert').or_(page.get_by_role('status')).filter(has_text='huge.csv is larger than 10 MB.')).to_be_visible()
        assert sent == []
        with transaction(h.app.state.engine) as db:
            assert db.query(m.ScoreCheckImportBatch).count() == 0
    finally:
        context.close()


def test_automatic_matches_can_be_reviewed_overridden_and_undone(browser, live_server, h):
    seeded = seed_upload(h, ROWS)
    context, page = open_page(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/benchmark/check?import=' + seeded['id'])
        main = page.locator('#main')
        matched = main.locator('.bm-matched')
        matched.get_by_role('button', name='Review').click()
        review = page.get_by_role('region', name='Matched automatically')
        alex = review.get_by_role('row').filter(has=page.locator('.bm-written', has_text=re.compile('^Staff 01\\(test\\)$')))
        expect(alex).to_be_visible()
        expect(alex).to_contain_text('%')
        alex.get_by_role('button', name='Leave out').click()
        expect(alex).to_have_count(0)  # it is now a decision of its own
        page.keyboard.press('Escape')
        decided = main.locator('.bm-row.done', has=page.locator('.bm-written', has_text=re.compile('^Staff 01\\(test\\)$')))
        expect(decided).to_contain_text('Left out')
        decided.get_by_role('button', name='Undo').click()
        expect(decided).to_have_count(0)
        matched.get_by_role('button', name='Review').click()
        expect(page.get_by_role('region', name='Matched automatically').get_by_role('row')
               .filter(has=page.locator('.bm-written', has_text=re.compile('^Staff 01\\(test\\)$')))).to_be_visible()
    finally:
        context.close()


def test_a_clinic_added_after_the_import_changed_uses_its_current_dates(browser, live_server, h):
    """The import's period changes after Check opened (elsewhere): Add is refused as stale, both are read again,
    and the second Add starts the clinic on the import's current earliest day."""
    from .test_score_check_frontend_api import change
    seeded = seed_upload(h, [['Staff 01(test)', '2026-10-04', 'Clinical review session', 'Clinic 9', '08:00', '12:00']])
    context, page = open_page(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/benchmark/check?import=' + seeded['id'])
        main = page.locator('#main')
        expect(main.get_by_role('heading', name=re.compile('need your decision'))).to_be_visible()
        change(h, seeded, 'update_source', source_id=seeded['sources'][0]['id'], period_start='2026-09-01', period_end='2026-11-01')
        row = main.get_by_role('region', name='Not in Rota').locator('.bm-row', has=page.locator('.bm-written', has_text=re.compile('^Clinic 9$')))
        row.get_by_role('button', name='Add Clinic 9 as clinic').click()
        add = page.get_by_role('dialog', name='Add Clinic 9')
        add.get_by_role('button', name=re.compile(r'^Location: ')).click()
        pick_first(page)
        add.get_by_role('button', name='Add', exact=True).click()
        expect(add).to_contain_text('changed meanwhile and was reloaded')
        add.get_by_role('button', name='Add', exact=True).click()
        expect(add).to_have_count(0)
        expect(row).to_contain_text('Added')
        with transaction(h.app.state.engine) as db:
            assert str(db.query(m.Service).filter_by(name='Clinic 9').one().starts_on) == '2026-09-01'
    finally:
        context.close()


def test_a_stale_matches_answer_never_replaces_a_newer_one(browser, live_server, h):
    """Two independent reads of the match list are in flight: the first (after leaving out one match) is held at the
    network layer; a second decision's read (a new GET: every save clears shared in-flight reads) answers first. The
    older answer, released afterwards with the list from before both decisions, must not replace the newer one."""
    seeded = seed_upload(h, ROWS)
    context, page = open_page(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/benchmark/check?import=' + seeded['id'])
        main = page.locator('#main')
        before = json.dumps(h.get('coordinator', '/api/score-imports/%s/matches?offset=0&limit=50' % seeded['id']))
        main.locator('.bm-matched').get_by_role('button', name='Review').click()
        review = page.get_by_role('region', name='Matched automatically')
        row = lambda written: review.get_by_role('row').filter(has=page.locator('.bm-written', has_text=re.compile('^%s$' % re.escape(written))))
        alex, duty = row('Staff 01(test)'), row('Clinical review session')
        expect(alex).to_be_visible()
        expect(duty).to_be_visible()
        held, seen = [], []
        page.on('request', lambda r: seen.append(r) if '/matches?' in r.url else None)
        page.route(re.compile(r'.*/matches\?'), lambda route: held.append(route) if not held else route.continue_())
        is_matches = lambda r: '/matches?' in r.url
        with page.expect_request(is_matches):  # the older read: held
            alex.get_by_role('button', name='Leave out').click()
        with page.expect_request(is_matches):  # the newer read: answers
            duty.get_by_role('button', name='Leave out').click()
        expect(alex).to_have_count(0)
        expect(duty).to_have_count(0)
        assert len(held) == 1 and len(seen) == 2 and seen[0] is not seen[1]  # two distinct requests
        with page.expect_event('requestfinished', lambda r: r is seen[0]):
            held[0].fulfill(status=200, content_type='application/json', body=before)  # the older read, the old list
        page.unroute(re.compile(r'.*/matches\?'))
        page.evaluate('() => new Promise(requestAnimationFrame)')  # the page has handled that answer (one frame)
        expect(alex).to_have_count(0)
        expect(duty).to_have_count(0)
    finally:
        context.close()


def test_score_checking_is_off_until_a_developer_turns_it_on(browser, live_server, h):
    """Off (the shipped default): no Compare page for a coordinator, no AI provider section, and only a Developer may tick
    Department settings › Advanced › Score checking (the server refuses anyone else)."""
    score_checking(h, False)
    developer(h)
    context, page = open_page(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/benchmark/upload')
        main = page.locator('#main')
        expect(main).to_contain_text('Score checking is off for this department.')
        expect(main.get_by_role('heading', name='Compare your rota with Rota')).to_have_count(0)
    finally:
        context.close()
    for role, can_turn_on in (('admin', False), ('developer', True)):
        context, page = open_page(browser, live_server, role)
        try:
            open_config(page, 'Department settings')
            sections = page.get_by_role('navigation', name='Configuration sections')
            expect(sections.get_by_role('button', name='AI provider', exact=True)).to_have_count(0)
            department_tab(page, 'Advanced')
            box = page.locator('.config-pane').get_by_label('Score checking', exact=True)
            expect(box).not_to_be_checked()
            (expect(box).to_be_enabled if can_turn_on else expect(box).to_be_disabled)()
        finally:
            context.close()

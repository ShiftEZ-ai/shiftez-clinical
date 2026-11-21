"""Minutes and resumable controls through Chromium and the real audited API."""
from datetime import timedelta
import re

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction, retry_write
from apps.worker.main import process_one, claim_job, heartbeat_job
from packages.contracts.schemas import AUTO_SCHEDULE_LIMITS
from .test_browser import minutes_words, auto_setup, footer_working, wait_run_created, browser, live_server, sign_in, navigate, open_rota, open_scheduling  # noqa: F401
from .test_browser_cp_sat import cp_server  # noqa: F401
from .test_browser_rule_catalogue import shot
from .test_staff_wishes import department_settings

pytestmark = pytest.mark.browser


def confirm(page, title, action, reason):
    dialog = page.get_by_role('dialog', name=title, exact=True)
    dialog.get_by_label('Reason', exact=True).fill(reason)
    with page.expect_response(lambda response: response.url.endswith('/api/commands')
                              and response.request.method == 'POST') as reply:
        dialog.get_by_role('button', name=action, exact=True).click()
    assert reply.value.ok, reply.value.text()
    expect(dialog).to_have_count(0)
    return reply.value.json()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_search_minutes_continue_extend_and_stop_are_audited(browser, cp_server, h, width, scheme):
    maximum = 8
    h.command('admin', 'department.settings', {**department_settings(h),
              'generation_defaults': {'max_search_minutes': maximum}}, h.seed['department_id'],
              h.get('admin', '/api/me')['organisation']['revision'])
    workspace = h.shift(h.workspace('Minutes and continuation'), people=[])['workspace']
    before = h.detail(workspace)['shifts']
    context, page = sign_in(browser, cp_server, 'coordinator',
                            viewport={'width': width, 'height': 900}, color_scheme=scheme)
    try:
        expect(page.locator('html')).to_have_css('color-scheme', scheme)
        open_rota(page, workspace['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        auto_setup(panel, fields=False)
        minutes = panel.get_by_role('textbox', name='Search time', exact=True)
        # One unit, read and written as people read it (L10); its bounds are said in the same words.
        expect(minutes).to_have_value(minutes_words(AUTO_SCHEDULE_LIMITS['default_minutes']))
        minutes.fill(str(maximum + 1))
        expect(minutes).to_have_js_property('validationMessage', f"Between {minutes_words(AUTO_SCHEDULE_LIMITS['minimum_minutes'])} and {minutes_words(maximum)}.")
        for invalid in ('0', str(maximum + 1), '1.5'):
            minutes.fill(invalid)
            panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
            assert not minutes.evaluate('(input) => input.checkValidity()')
            assert not h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
        minutes.fill('7')
        shot(page, f'autoschedule-minutes-{width}-{scheme}')
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
        wait_run_created(page)
        first = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs'][0]
        assert first['effective_options']['search_minutes'] == 7
        # D76: while queued the run offers only Cancel; more time once its search runs (below).
        expect(panel.get_by_role('button', name=re.compile(r'^Add \d+ min$'))).to_have_count(0)
        expect(panel.get_by_role('button', name='Cancel auto-schedule', exact=True)).to_be_visible()

        # A stored time-limit outcome isolates the journey; native cache/hint recovery has domain/worker coverage.
        def needs_time(snapshot, **options):
            return {'status': 'needs_more_time', 'algorithm_version': snapshot['compiler_version'],
                    'input_fingerprint': snapshot['fingerprint'],
                    'phase_timings': {'preparation_seconds': 180, 'search_seconds': 420,
                                      'validation_seconds': 0, 'summary_seconds': 0}}
        assert process_one(h.app.state.engine, scheduler=needs_time)
        # Expired preparation still continues the frozen inputs and explains the rebuild.
        with transaction(h.app.state.engine, write=True) as db:
            old = db.get(m.Job, first['id'])
            age = timedelta(days=h.settings.generation_resume_days + 1)
            old.created_at -= age
            old.started_at -= age
            old.completed_at -= age
        expect(panel.get_by_text('More search time needed', exact=True)).to_be_visible()
        expect(panel).to_contain_text('Saved inputs are kept.')
        expect(panel).to_contain_text('preparation stopped early runs again.')
        # A later whole-rota check must leave the unfinished generation reachable, including after reload.
        # D76: the check is the footer's working item; the panel keeps showing the run that needs more time.
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.post_data_json.get('kind') == 'job.create'):
            panel.get_by_role('button', name='Check the whole rota', exact=True).click()
        if width != 390:  # on a phone the sheet covers the footer; its "+N" holds the check (test_browser_planner_phone)
            footer_working(page, 'Waiting to check')  # queued: the one wording rule (DL D-063, kept by DL-018 + Master 10 Nov)
        expect(panel.locator('.auto-outcome').filter(has_text='More search time needed')).to_be_visible()
        expect(panel.get_by_role('button', name='Cancel check', exact=True)).to_have_count(0)
        expect(panel.get_by_role('button', name='Stop and keep best', exact=True)).to_have_count(0)
        assert process_one(h.app.state.engine)
        checked = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs'][0]
        assert checked['kind'] == 'validation' and checked['status'] == 'succeeded'
        expect(panel.locator('.auto-outcome').filter(has_text='More search time needed')).to_be_visible()
        page.reload()
        open_rota(page, workspace['id'])
        open_scheduling(page)
        expect(panel.locator('.auto-outcome').filter(has_text='More search time needed')).to_be_visible()
        more = panel.get_by_role('textbox', name='More search time', exact=True)
        more.fill('3')
        # Checking again redraws the panel without changing the generation being continued.
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.post_data_json.get('kind') == 'job.create'):
            panel.get_by_role('button', name='Check the whole rota', exact=True).click()
        if width != 390:
            footer_working(page, 'Waiting to check')  # queued: the one wording rule (DL D-063, kept by DL-018 + Master 10 Nov)
        assert process_one(h.app.state.engine)
        expect(panel.locator('.auto-outcome').filter(has_text='More search time needed')).to_be_visible()
        expect(more).to_have_value(minutes_words(3))  # kept across the redraw, written as the app writes times (83ae3d14b)
        shot(page, f'autoschedule-continue-{width}-{scheme}')
        panel.get_by_role('button', name='Continue for 3 min', exact=True).click()
        continued = confirm(page, 'Continue auto-schedule', 'Continue', 'Continue the synthetic prepared attempt')
        resumed = continued['job']
        assert continued['preparation_expired']
        expect(page.locator('.toast').filter(has_text='Cached preparation expired. This attempt rebuilds from your saved inputs.')).to_be_visible()
        assert resumed['id'] != first['id'] and resumed['input_fingerprint'] == first['input_fingerprint']
        assert resumed['effective_options']['search_minutes'] == 3
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
        claim = retry_write(h.app.state.engine, claim_job)
        assert claim['id'] == resumed['id']
        assert retry_write(h.app.state.engine, lambda db: heartbeat_job(db, claim, {'stage': 'summarizing'}))
        expect(panel.get_by_role('button', name='Add 5 min', exact=True)).to_be_disabled()
        assert retry_write(h.app.state.engine, lambda db: heartbeat_job(db, claim, {'stage': 'finding'}))
        expect(panel.get_by_role('button', name='Add 5 min', exact=True)).to_be_enabled()
        panel.get_by_role('button', name='Add 5 min', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add search time', exact=True)
        more = dialog.get_by_role('textbox', name='Time to add', exact=True)
        expect(more).to_have_value('5 min')
        more.fill('6 min')
        expect(more).to_have_js_property('validationMessage', 'Between 1 min and 5 min.')  # up to the room left
        more.fill('4 min')
        extended = confirm(page, 'Add search time', 'Add time', 'Allow four more search minutes')['job']
        assert extended['search_control']['minutes'] == 7
        # The remaining minute is usable; a five-minute default must not disable it.
        panel.get_by_role('button', name='Add 1 min', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add search time', exact=True)
        expect(dialog.get_by_role('textbox', name='Time to add', exact=True)).to_have_value('1 min')
        extended = confirm(page, 'Add search time', 'Add time', 'Use the remaining search minute')['job']
        assert extended['search_control']['minutes'] == 8
        expect(panel.get_by_role('button', name='Add 0 min', exact=True)).to_be_disabled()
        panel.get_by_role('button', name='Stop and keep best', exact=True).click()
        stopped = confirm(page, 'Stop and keep best', 'Stop and keep best', 'Keep all prepared work')['job']
        assert stopped['search_control']['stop']
        expect(panel.get_by_role('button', name='Keeping the best result…', exact=True)).to_be_disabled()
        shot(page, f'autoschedule-stop-{width}-{scheme}')
        assert h.detail(workspace)['shifts'] == before
        with transaction(h.app.state.engine) as db:
            actions = set(db.scalars(select(m.AuditEvent.action).where(
                m.AuditEvent.target_id.in_([first['id'], resumed['id']]))))
        assert {'job.continue', 'job.add_time', 'job.stop_keep_best'} <= actions
    finally:
        context.close()


def test_department_search_maximum_saves_validates_and_undoes(browser, cp_server, h):
    context, page = sign_in(browser, cp_server, 'admin')
    try:
        navigate(page, 'configuration/auto_schedule')
        page.get_by_role('tab', name='Timing', exact=True).click()
        maximum = page.get_by_role('textbox', name='Maximum search time', exact=True)
        minutes = page.get_by_role('textbox', name='Search time', exact=True)
        maximum.fill('9 min')
        minutes.fill('10 min')
        expect(minutes).to_have_js_property('validationMessage', 'Between 1 min and 9 min.')  # the ceiling follows the maximum
        page.get_by_role('button', name='Save defaults', exact=True).click()
        assert not minutes.evaluate('(input) => input.checkValidity()')
        expect(page.get_by_role('dialog')).to_have_count(0)
        minutes.fill('7 min')
        page.get_by_role('button', name='Save defaults', exact=True).click()
        confirm(page, 'Save auto-schedule defaults', 'Save defaults', 'Set the department search limit')
        defaults = h.get('admin', '/api/me')['department']['generation_defaults']
        assert defaults['max_search_minutes'] == 9 and defaults['search_minutes'] == 7
        page.get_by_role('button', name='Undo', exact=True).click()
        expect(maximum).to_have_value(minutes_words(AUTO_SCHEDULE_LIMITS['default_max_minutes']))
        page.reload()
        page.get_by_role('tab', name='Timing', exact=True).click()
        expect(page.get_by_role('textbox', name='Search time', exact=True)).to_have_value(
            minutes_words(AUTO_SCHEDULE_LIMITS['default_minutes']))
    finally:
        context.close()


def test_draft_panel_shows_the_search_time_captured_with_the_draft(browser, cp_server, h):
    """A draft runs with the auto-schedule settings captured when it started: after the department's default moves on,
    its set-up panel still shows the captured search time, and the department keeps its new value."""
    captured, current = 4, 6

    def department_minutes(minutes):
        h.command('admin', 'department.settings', {**department_settings(h),
                  'generation_defaults': {'search_minutes': minutes}}, h.seed['department_id'],
                  h.get('admin', '/api/me')['organisation']['revision'])

    department_minutes(captured)
    workspace = h.shift(h.workspace('Captured search time'), people=[])['workspace']
    department_minutes(current)
    assert h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['generation_defaults']['search_minutes'] == captured
    assert h.get('admin', '/api/me')['department']['generation_defaults']['search_minutes'] == current
    context, page = sign_in(browser, cp_server, 'coordinator')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        open_rota(page, workspace['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        auto_setup(panel, fields=False)
        expect(panel.get_by_role('textbox', name='Search time', exact=True)).to_have_value(minutes_words(captured))
        assert errors == []
    finally:
        context.close()

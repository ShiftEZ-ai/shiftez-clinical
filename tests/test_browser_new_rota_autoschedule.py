"""New rota (audit G-01, G-02, G-30, G-42): a start that cannot succeed is disabled with its reason, the dialog has no
taglines, and a rota created with "Run auto-schedule after setup" opens on its Auto-schedule panel, which shows the run
and then its result without any click. Coordinator, 1440 light and 390 dark."""
import re

import pytest
from playwright.sync_api import expect

from apps.worker.main import process_one
from .test_browser import browser, holding, live_server, sign_in, navigate, new_draft, save_dialog  # noqa: F401
from .test_browser_cp_sat import cp_server  # noqa: F401
from .test_series_reconciliation import template

pytestmark = pytest.mark.browser
VIEWS = [pytest.param({'width': 1440, 'height': 900}, 'light', id='1440-light'),
         pytest.param({'width': 390, 'height': 844}, 'dark', id='390-dark')]


def start_choice(dialog, value):
    return dialog.get_by_label('Start from', exact=True).locator(f'option[value="{value}"]')


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_a_start_that_cannot_succeed_is_disabled_with_its_reason(browser, live_server, h, view, scheme):
    series = [r for r in h.get('admin', '/api/catalogues')['recurring_series'] if r.get('active')]
    rotas = h.get('coordinator', '/api/workspaces?limit=1')['workspaces']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'planner')
        new_draft(page)
        dialog = page.get_by_role('dialog', name='New rota', exact=True)
        expect(dialog).not_to_contain_text('Nothing is shared with staff until it is approved and published')
        expect(dialog).not_to_contain_text('Current rules and eligibility apply')
        if series:
            expect(start_choice(dialog, 'patterns')).to_be_enabled()
        else:
            expect(start_choice(dialog, 'patterns')).to_be_disabled()
            expect(start_choice(dialog, 'patterns')).to_have_attribute('data-hint', 'None set up yet · Configuration')
        if rotas:
            expect(start_choice(dialog, 'previous')).to_be_enabled()
        else:
            expect(start_choice(dialog, 'previous')).to_be_disabled()
            expect(start_choice(dialog, 'previous')).to_have_attribute('data-hint', 'no rota to copy yet')
        # An empty rota has nothing to auto-schedule: no toggle for it.
        expect(dialog.get_by_label('Run auto-schedule after setup', exact=True)).to_be_hidden()
    finally:
        context.close()


@pytest.mark.parametrize('start_from', ['patterns', 'previous'], ids=['recurring', 'copy'])
def test_new_rota_creates_a_draft_without_starting_auto_schedule(browser, live_server, h, start_from):
    if start_from == 'patterns':
        template(h, days=(0,), ends_on=None)
    else:
        h.shift(h.workspace('Copy source'), people=[])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/week/2026-10-04' if start_from == 'patterns' else 'planner')
        new_draft(page)
        dialog = page.get_by_role('dialog', name='New rota', exact=True)
        dialog.get_by_label('Start from', exact=True).select_option(start_from)
        automate = dialog.get_by_label('Run auto-schedule after setup', exact=True)
        expect(automate).to_be_visible()
        expect(automate).not_to_be_checked()
        # The name follows the dates from the server's suggestion; a person presses Create once it is there.
        expect(dialog.get_by_label('Rota name', exact=True)).to_have_value(re.compile(r'^Rota '))

        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'workspace.create') as created:
            save_dialog(dialog, 'Create draft')
        assert created.value.ok, created.value.text()
        assert created.value.request.post_data_json['payload']['generate_assignments'] is False
        result = created.value.json()
        assert result['setup']['created'] > 0
        jobs = h.get('coordinator', '/api/jobs?workspace_id=' + result['workspace']['id'])['jobs']
        assert not any(job['kind'] == 'generation' for job in jobs)
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_new_rota_with_auto_schedule_opens_on_its_run_and_result(browser, cp_server, h, view, scheme):
    source = h.shift(h.workspace('Copy source for auto-schedule'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'planner')
        new_draft(page)
        dialog = page.get_by_role('dialog', name='New rota', exact=True)
        dialog.get_by_label('Start from', exact=True).select_option('previous')
        expect(dialog.get_by_label('Previous rota', exact=True)).to_have_value(source['id'])
        automate = dialog.get_by_label('Run auto-schedule after setup', exact=True)
        expect(automate).not_to_be_checked()
        expect(dialog.get_by_role('button', name=re.compile('^Create draft'))).to_be_visible()
        # F-N1: the button says the run starts too; unticked, it is a plain Create draft.
        automate.check()
        create = dialog.get_by_role('button', name=re.compile('^Create and auto-schedule'))
        expect(create).to_be_visible()
        automate.uncheck()
        expect(dialog.get_by_role('button', name=re.compile('^Create draft'))).to_be_visible()
        automate.check()
        save_dialog(dialog, re.compile('^Create and auto-schedule'))
        panel = page.locator('#auto-panel')
        expect(panel).to_be_visible()  # opened for the run, not left closed on an unstaffed draft
        expect(panel.locator('.auto-body')).to_have_attribute('data-state', 'run')
        process_one(h.app.state.engine)
        expect(panel.get_by_role('region', name='Review')).to_be_visible(timeout=20000)
    finally:
        context.close()


def test_create_before_the_suggested_name_lands_clears_its_error_when_it_does(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    overlaps = '**/api/schedule/overlaps?**'
    try:
        with holding(page) as held:
            navigate(page, 'planner')
            held.hold(overlaps)
            with page.expect_request(lambda r: '/api/schedule/overlaps?' in r.url):
                new_draft(page)
            dialog = page.get_by_role('dialog', name='New rota', exact=True)
            name = dialog.get_by_label('Rota name', exact=True)
            expect(name).to_have_value('')
            dialog.get_by_role('button', name='Create draft', exact=True).click()
            expect(name).to_have_attribute('aria-invalid', 'true')
            expect(dialog.locator('.field-error').filter(has_text='Add a rota name')).to_be_visible()
            # Release answers what is held, then unroutes: unrouting first already lets them through (Playwright 1.63+).
            held.release(overlaps)
            expect(name).to_have_value(re.compile(r'^Rota '))
            expect(name).not_to_have_attribute('aria-invalid', 'true')
            expect(dialog.locator('.field-error').filter(has_text='Add a rota name')).to_be_hidden()
    finally:
        context.close()

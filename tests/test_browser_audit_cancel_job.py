"""Cancel a queued auto-schedule through the UI (D76: a queued run offers only Cancel), leaving the draft unchanged."""
import re
import pytest
from playwright.sync_api import expect

from apps.worker.main import process_one
from .test_browser import wait_run_created, browser, live_server, sign_in, open_rota, open_scheduling  # noqa: F401
from .test_browser_cp_sat import cp_server  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('viewport', [
    {'width': 1440, 'height': 900},
    {'width': 390, 'height': 844},
], ids=['desktop', 'phone'])
def test_cancel_queued_auto_schedule_leaves_the_draft_unchanged(browser, cp_server, h, viewport):
    workspace = h.shift(h.workspace('Stop queued auto-schedule audit'), people=[])['workspace']
    before = h.detail(workspace)
    context, page = sign_in(browser, cp_server, 'coordinator', viewport=viewport)
    try:
        open_rota(page, workspace['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
        wait_run_created(page)
        job = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs'][0]
        assert job['status'] == 'queued' and job['generation_engine'] == 'cp_sat'

        # D76: a queued run offers only Cancel; Stop and keep best and more time wait for its search.
        expect(panel.get_by_role('button', name='Stop and keep best', exact=True)).to_have_count(0)
        expect(panel.get_by_role('button', name=re.compile(r'^Add \d+ min$'))).to_have_count(0)
        # Owner (7 Nov 2026): Cancel discards the run, so it is the same-button confirm (armed, solid red), no dialog.
        expect(panel.get_by_role('button', name='Cancel auto-schedule', exact=True)).to_be_visible()
        cancel = panel.locator('.run-cancel')
        cancel.click()
        expect(cancel).to_have_class(re.compile(r'\barmed\b'))
        expect(cancel).to_have_accessible_name('Discard this run? Cancel auto-schedule')
        expect(page.get_by_role('dialog')).to_have_count(0)
        cancel.click()
        expect(panel.get_by_role('progressbar')).to_have_count(0)
        cancelled = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        assert cancelled['status'] == 'cancelled' and cancelled['result'] is None
        # Nothing left for the worker, and the draft is exactly as it was.
        assert not process_one(h.app.state.engine)
        expect(panel.get_by_text('Auto-schedule cancelled', exact=True)).to_be_visible()
        after = h.detail(workspace)
        assert after['workspace']['revision'] == before['workspace']['revision']
        assert after['workspace']['status'] == 'draft'
        assert after['shifts'] == before['shifts']

        # After a reload the cancelled run is in Recent runs and Run is offered again.
        page.reload()
        open_rota(page, workspace['id'])
        open_scheduling(page)
        expect(panel.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_enabled()
    finally:
        context.close()

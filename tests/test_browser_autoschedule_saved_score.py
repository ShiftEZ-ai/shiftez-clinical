"""The saved-score contract is verified against the rendered Planner footer after Apply."""
import re

import pytest
from playwright.sync_api import expect

from apps.worker.main import process_one
from .test_browser import browser, live_server, sign_in, open_rota
from .test_browser_cp_sat import cp_server
from .test_autoschedule_backend_audit import run_job

pytestmark = pytest.mark.browser


def test_applied_job_score_saved_is_the_planner_footer_score(browser, cp_server, h):
    workspace = h.shift(h.workspace('Saved score acceptance'), people=[h.seed['people'][0]])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    job = run_job(h, workspace, generation_options={'mode': 'rebalance'})
    saved = job['result']['summary']['score_saved']
    h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'], 'job_id': job['id'],
        'reason': 'Apply complete proposal for footer acceptance'}, revision=workspace['revision'])
    while process_one(h.app.state.engine):  # the worker finishes the complete check Apply queued (maintenance)
        pass
    assert h.detail(workspace)['workspace']['validation']['score'] == saved
    assert h.get('coordinator', f"/api/workspaces/{workspace['id']}/fix-list")['summary']['must'] == 0
    context, page = sign_in(browser, cp_server, 'coordinator')
    try:
        open_rota(page, workspace['id'])
        # A7: with nothing to fix, the footer's one score is the saved run's.
        expect(page.locator('.app-footer').get_by_role('button', name=f"Ready to submit · score {saved['score']:g}", exact=True)).to_be_visible()
    finally:
        context.close()

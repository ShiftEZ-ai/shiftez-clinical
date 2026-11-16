"""Drawing a reviewed proposal keeps its loaded assignments; changed job revisions are read again."""
import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one
from .test_browser import browser, live_server, sign_in, navigate, open_rota, open_scheduling
from .test_browser_progress_bar import without_kept_replies

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('changed', ('job', 'workspace'))
def test_show_proposal_reuses_current_job_but_refreshes_changed_revision(browser, live_server, h, changed):
    workspace = h.shift(h.workspace('Proposal read cache'), people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                    revision=workspace['revision'])['job']
    process_one(h.app.state.engine)
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']['additions']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, workspace['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        expect(panel.get_by_role('button', name='Show proposal on grid', exact=True)).to_be_visible()
        reads = []
        page.on('request', lambda request: reads.append(request.url) if request.url.endswith('/api/jobs/' + job['id']) else None)
        panel.get_by_role('button', name='Show proposal on grid', exact=True).click()
        expect(page.locator('.proposal-bar')).to_be_visible()
        expect(panel.get_by_role('button', name='Hide proposal on grid', exact=True)).to_be_visible()
        expect(page.locator('.pchip.proposed-add').first).to_be_visible()
        assert not reads, 'Drawing the already reviewed proposal must not download its assignments again'
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Job, job['id']) if changed == 'job' else db.get(m.Workspace, workspace['id'])
            row.revision += 1
        # A raw database bump sends no live change, so this tab is told the way a save of its own tells it: the replies
        # it kept stop being current (api.ts keptReads, c6d9edec1) and the reopened rota is read again.
        without_kept_replies(page)
        navigate(page, 'home')
        open_rota(page, workspace['id'])
        open_scheduling(page)
        expect(panel.get_by_role('button', name='Hide proposal on grid', exact=True)).to_be_visible()
        assert reads, f'A changed {changed} revision must fetch the current result'
    finally:
        context.close()

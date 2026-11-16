"""Empty rota health and Submit share the server prerequisite and an Add session action.
The counts of a draft with sessions are the Fix list's
(tests/test_browser_fix_list.py). Coordinator, 1440 light and 390 dark."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, open_rota, footer_item  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.postgres,
              pytest.mark.parametrize('h', ['postgresql'], indirect=True)]
VIEWS = [pytest.param({'width': 1440, 'height': 900}, 'light', id='1440-light'),
         pytest.param({'width': 390, 'height': 844}, 'dark', id='390-dark')]


def submit_dialog(page):
    page.get_by_role('button', name=re.compile(r'^Submit for review')).first.click()
    dialog = page.get_by_role('dialog', name=re.compile('^Submit .* for review$'))
    expect(dialog).to_be_visible()
    return dialog


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_empty_draft_says_no_sessions_yet_and_why_submit_waits(browser, live_server, h, view, scheme):
    from .test_gen_fix_readiness import advisory
    advisory(h)
    w = h.workspace('Health empty draft')
    summary = h.get('coordinator', f'/api/workspaces/{w["id"]}/fix-list')['summary']
    assert summary['could'] > 0
    reason = summary['submission_issue']['message']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        open_rota(page, w['id'])
        pills = page.locator('.app-footer .health-pills')
        expect(pills).to_contain_text(reason)
        expect(pills).not_to_contain_text('score')  # no "n/a score"
        expect(pills).not_to_contain_text('Ready')
        if view['width'] > 400:
            dialog = submit_dialog(page)
            expect(dialog.locator('.summary-item').first).to_contain_text('0')
            expect(dialog.get_by_text(reason, exact=True)).to_be_visible()
            expect(dialog.get_by_role('button', name='Submit for review', exact=True)).to_be_disabled()
            dialog.get_by_role('button', name='Add session', exact=True).click()
            expect(dialog).to_have_count(0)
            expect(page.locator('.dock-panel').get_by_label('Location', exact=True)).to_be_visible()  # the session form
        else:
            footer_item(page, 'button', re.compile(r'^Add session$')).click()  # one-line phone footer: maybe behind +N
            expect(page.locator('.dock-panel').get_by_label('Location', exact=True)).to_be_visible()
    finally:
        context.close()


def test_empty_completed_check_has_same_reason_and_add_action(browser, live_server, h):
    from apps.worker.main import process_one
    from apps.api import models as m
    from apps.api.db import transaction
    w = h.workspace('Empty checked draft')
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'}, revision=w['revision'])['job']
    assert process_one(h.app.state.engine)
    # Historical workers stored summaries without the prerequisite; current server health still owns readiness.
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.result_summary = {**row.result_summary, 'result': {
            k: v for k, v in row.result_summary['result'].items() if k != 'submission_issue'}}
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, w['id'])
        page.get_by_role('button', name='Auto-schedule', exact=True).click()
        panel = page.locator('#auto-panel')
        expect(panel).to_contain_text('Add a session before submitting')
        expect(panel).not_to_contain_text('Full rota check passed')
        panel.get_by_role('button', name='Add session', exact=True).click()
        expect(page.locator('.dock-panel').get_by_label('Location', exact=True)).to_be_visible()
    finally:
        context.close()

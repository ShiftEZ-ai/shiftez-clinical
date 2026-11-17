"""Cycle 4: checking a rota is not saving it; optional approval notes stay optional."""
import pytest
from playwright.sync_api import Page, expect
from apps.api import models as m
from apps.api.db import transaction
from .test_browser import browser, live_server, sign_in, open_rota, wait_held  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.postgres,
              pytest.mark.parametrize('h', ['postgresql'], indirect=True)]


def no_save_state(page: Page):
    """D-4 (coordinator decision): until the person's own save the footer says nothing about saving: the element is
    hidden and has no accessible name."""
    state = page.locator('#save-state')
    expect(state).to_be_hidden()
    assert state.get_attribute('aria-label') is None


def test_open_unchecked_rota_does_not_check_and_background_check_never_claims_save(browser, live_server, h):
    w = h.shift(h.workspace())['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, w['id']).validation = None
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        commands = []
        page.on('request', lambda r: commands.append(r.post_data_json) if r.url.endswith('/api/commands') else None)
        open_rota(page, w['id'])
        expect(page.locator('#schedule-view')).to_be_visible()
        no_save_state(page)
        assert not [r for r in commands if r['kind'] == 'workspace.validate' or
                    r['kind'] == 'job.create' and r['payload']['kind'] == 'validation']
        # Observe the complete command lifecycle while the background answer is held.
        held = []
        page.route('**/api/commands', lambda route: held.append(route))
        page.evaluate("""id => { window.importBundle('api.js').then(m => m.command('job.create',
            {workspace_id:id,kind:'validation',automatic:true})); }""", w['id'])
        wait_held(page, held)
        no_save_state(page)
        with page.expect_response(lambda r: r.url.endswith('/api/commands')):
            held.pop().continue_()
        no_save_state(page)
        page.unroute('**/api/commands')
        page.evaluate("""() => window.importBundle('api.js').then(m => m.command('table_view.create',
            {table_key:'people',name:'Explicit save',definition:{}}))""")
        expect(page.locator('#save-state')).to_have_attribute('aria-label', 'Saved just now')
    finally:
        context.close()

"""Configuration › Integrations (docs/guides/feedback.md): an Admin with Developer access connects GitHub with a pasted
token, sees it only as its last four characters, tests, replaces and disconnects it; without the server's data key the page says why it cannot
connect. GitHub itself is replaced by a stub in the server process (the only call that leaves Rota)."""
from datetime import timedelta

import pytest
from playwright.sync_api import expect
from packages.domain.time import now as clock_now
from .governance_support import developer
from .test_browser import browser, live_server, sign_in, navigate, wait_held  # noqa: F401

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 1000}


@pytest.fixture(autouse=True)
def developer_account(h):
    developer(h)


@pytest.fixture
def github(h, monkeypatch):
    """Before live_server forks: a data key for the server and a GitHub check that accepts tokens ending in 'good'."""
    from apps.api.services import github_integrations
    soon = clock_now() + timedelta(days=5)  # the app's clock (tests run it at a fixed date)
    monkeypatch.setattr(github_integrations, 'check_repository', lambda repo, token: (token.endswith('good'), soon))
    h.server_env = {**getattr(h, 'server_env', {}), 'FMUC_DATA_KEY': '6d3e458f35f5c893e6617b39c13de5f300060d6f7b87d84efe5597e5a27fdf24'}


def test_developer_connects_tests_replaces_and_disconnects_github(github, browser, live_server, h):
    context, page = sign_in(browser, live_server, 'developer', viewport=DESKTOP)
    try:
        navigate(page, 'configuration/integrations')
        panel = page.locator('.config-pane .integration')
        expect(panel.locator('.integration-head')).to_contain_text('Not connected')
        # F13 A-41: the token steps are shown in the form (not folded away); no tagline above.
        expect(panel.get_by_role('list', name='How to get a token')).to_contain_text('Read and write')
        expect(page.locator('#main')).not_to_contain_text('Services Rota connects to')
        panel.get_by_label('Repository').fill('example-org/rota')
        panel.get_by_label('Token', exact=True).fill('github_pat_bad')
        panel.get_by_role('button', name='Connect').click()
        expect(page.get_by_role('alert').filter(has_text='could not verify').first).to_be_visible()
        expect(panel.locator('.integration-head')).to_contain_text('Not connected')
        panel.get_by_label('Token', exact=True).fill('github_pat_123good')
        panel.get_by_role('button', name='Connect').click()
        expect(panel.locator('.integration-head')).to_contain_text('Connected')
        # The token is never shown again: only its last four characters.
        expect(panel).to_contain_text('••••good')
        expect(panel).not_to_contain_text('github_pat_123good')
        expect(panel.get_by_label('Token', exact=True)).to_have_count(0)
        expect(panel).to_contain_text('Replace it before then')  # expires in five days
        panel.get_by_role('button', name='Test').click()
        expect(page.get_by_role('status').filter(has_text='GitHub accepted the token').first).to_be_visible()
        # Replace opens the form again; Cancel keeps the connection as it was.
        panel.get_by_role('button', name='Replace token…').click()
        expect(panel.get_by_label('Token', exact=True)).to_be_focused()
        panel.get_by_role('button', name='Cancel').click()
        expect(panel.get_by_label('Token', exact=True)).to_have_count(0)
        panel.get_by_role('button', name='Replace token…').click()
        panel.get_by_label('Token', exact=True).fill('github_pat_456good')
        panel.get_by_role('button', name='Replace token').click()
        expect(panel).to_contain_text('••••good')
        expect(panel.get_by_role('button', name='Test')).to_be_visible()
        # Disconnect asks on the same button first.
        disconnect = panel.get_by_role('button', name='Disconnect')
        disconnect.click()
        expect(panel.locator('.integration-head')).to_contain_text('Connected')
        panel.locator('button.armed').click()
        expect(panel.locator('.integration-head')).to_contain_text('Not connected')
        expect(panel.get_by_label('Token', exact=True)).to_be_visible()
        stored = h.get('developer', '/api/integrations/github')
        assert stored['connected'] is False and stored['token_hint'] is None
    finally:
        context.close()


def test_without_a_data_key_the_page_says_why_it_cannot_connect(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'developer', viewport=DESKTOP)
    try:
        navigate(page, 'configuration/integrations')
        panel = page.locator('.config-pane .integration')
        # Plain words for admins (C-08); the setting's name only in the detail for whoever runs the server.
        expect(panel).to_contain_text('A GitHub token cannot be saved on this server yet.')
        expect(panel.get_by_text('FMUC_DATA_KEY', exact=False)).to_be_hidden()
        expect(panel.get_by_label('Token', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_a_typed_token_asks_before_leaving_the_page(github, browser, live_server, h):
    context, page = sign_in(browser, live_server, 'developer', viewport=DESKTOP)
    try:
        navigate(page, 'configuration/integrations')
        panel = page.locator('.config-pane .integration')
        panel.get_by_label('Token', exact=True).fill('github_pat_123good')
        tags = page.locator('.config-nav [data-section=tags]')
        tags.click()
        # One unsaved-changes guard (dom.ts leaveGuard): Keep editing keeps the token; Discard changes leaves.
        guard = page.get_by_role('dialog', name='Leave this page?', exact=True)
        guard.get_by_role('button', name='Keep editing', exact=True).click()
        expect(guard).to_have_count(0)
        expect(panel.get_by_label('Token', exact=True)).to_have_value('github_pat_123good')
        tags.click()
        guard.get_by_role('button', name='Discard changes', exact=True).click()
        expect(page.locator('.config-pane .integration')).to_have_count(0)
    finally:
        context.close()


def test_a_late_test_answer_keeps_the_next_pages_unsaved_guard(github, browser, live_server, h):
    context, page = sign_in(browser, live_server, 'developer', viewport=DESKTOP)
    try:
        navigate(page, 'configuration/integrations')
        panel = page.locator('.config-pane .integration')
        panel.get_by_label('Repository').fill('example-org/rota')
        panel.get_by_label('Token', exact=True).fill('github_pat_123good')
        panel.get_by_role('button', name='Connect').click()
        held = []
        page.route('**/api/commands', lambda route: held.append(route), times=1)
        panel.get_by_role('button', name='Test').click()
        wait_held(page, held)
        page.get_by_role('navigation', name='Configuration sections').get_by_role('button', name='Department settings', exact=True).click()
        leave = page.get_by_label('Leave on planning issues', exact=True)
        leave.select_option('details')
        held[0].continue_()
        expect(page.get_by_role('status').filter(has_text='GitHub accepted the token').first).to_be_visible()
        # The Test answer arrived after leaving: Department settings still asks before its edit is lost.
        page.locator('.config-nav [data-section=tags]').click()
        page.get_by_role('dialog', name='Leave this page?', exact=True).get_by_role('button', name='Keep editing', exact=True).click()
        expect(leave).to_have_value('details')
    finally:
        context.close()


def test_a_late_first_load_keeps_the_next_pages_unsaved_guard(github, browser, live_server, h):
    context, page = sign_in(browser, live_server, 'developer', viewport=DESKTOP)
    try:
        navigate(page, 'configuration/department')
        held = []
        page.route('**/api/integrations/github', lambda route: held.append(route), times=1)
        page.evaluate("location.hash = 'configuration/integrations'")
        wait_held(page, held)
        navigate(page, 'configuration/department')
        leave = page.get_by_label('Leave on planning issues', exact=True)
        leave.select_option('details')
        with page.expect_response('**/api/integrations/github'):
            held[0].continue_()
        # Integrations loaded after the admin moved on: Department settings still asks before its edit is lost.
        page.locator('.config-nav [data-section=tags]').click()
        page.get_by_role('dialog', name='Leave this page?', exact=True).get_by_role('button', name='Keep editing', exact=True).click()
        expect(leave).to_have_value('details')
    finally:
        context.close()


def test_one_action_at_a_time_while_github_answers(github, browser, live_server, h):
    context, page = sign_in(browser, live_server, 'developer', viewport=DESKTOP)
    try:
        navigate(page, 'configuration/integrations')
        panel = page.locator('.config-pane .integration')
        panel.get_by_label('Repository').fill('example-org/rota')
        panel.get_by_label('Token', exact=True).fill('github_pat_123good')
        panel.get_by_role('button', name='Connect').click()
        held = []
        page.route('**/api/commands', lambda route: held.append(route), times=1)
        panel.get_by_role('button', name='Test').click()
        wait_held(page, held)
        # A replacement typed now would be wiped by Test's answer: Replace and Disconnect wait for it.
        expect(panel.get_by_role('button', name='Replace token…')).to_be_disabled()
        expect(panel.get_by_role('button', name='Disconnect')).to_be_disabled()
        held[0].continue_()
        expect(page.get_by_role('status').filter(has_text='GitHub accepted the token').first).to_be_visible()
        expect(panel.get_by_role('button', name='Replace token…')).to_be_enabled()
    finally:
        context.close()


def test_cancel_waits_for_a_pending_replacement(github, browser, live_server, h):
    context, page = sign_in(browser, live_server, 'developer', viewport=DESKTOP)
    try:
        navigate(page, 'configuration/integrations')
        panel = page.locator('.config-pane .integration')
        panel.get_by_label('Repository').fill('example-org/rota')
        panel.get_by_label('Token', exact=True).fill('github_pat_123good')
        panel.get_by_role('button', name='Connect').click()
        panel.get_by_role('button', name='Replace token…').click()
        panel.get_by_label('Token', exact=True).fill('github_pat_456good')
        held = []
        page.route('**/api/commands', lambda route: held.append(route), times=1)
        panel.get_by_role('button', name='Replace token', exact=True).click()
        wait_held(page, held)
        # Its answer redraws the panel, so a Cancel, a newer Replace form or newer typing would be lost: they wait.
        expect(panel.get_by_role('button', name='Cancel')).to_be_disabled()
        expect(panel.get_by_label('Token', exact=True)).not_to_be_editable()
        expect(panel.get_by_label('Repository')).not_to_be_editable()
        held[0].continue_()
        expect(page.get_by_role('status').filter(has_text='GitHub connected').first).to_be_visible()
        expect(panel.get_by_role('button', name='Replace token…')).to_be_enabled()
    finally:
        context.close()


def test_a_failed_replacement_lets_cancel_work_again(github, browser, live_server, h):
    context, page = sign_in(browser, live_server, 'developer', viewport=DESKTOP)
    try:
        navigate(page, 'configuration/integrations')
        panel = page.locator('.config-pane .integration')
        panel.get_by_label('Repository').fill('example-org/rota')
        panel.get_by_label('Token', exact=True).fill('github_pat_123good')
        panel.get_by_role('button', name='Connect').click()
        panel.get_by_role('button', name='Replace token…').click()
        panel.get_by_label('Token', exact=True).fill('github_pat_bad')
        panel.get_by_role('button', name='Replace token', exact=True).click()
        expect(page.get_by_role('alert').filter(has_text='could not verify').first).to_be_visible()
        expect(panel.get_by_label('Token', exact=True)).to_be_editable()  # a failure gives the fields back as sent
        expect(panel.get_by_label('Token', exact=True)).to_have_value('github_pat_bad')
        panel.get_by_role('button', name='Cancel').click()
        expect(panel.get_by_label('Token', exact=True)).to_have_count(0)
        expect(panel).to_contain_text('••••good')
    finally:
        context.close()


def test_operator_finds_integrations_without_developer_access(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'operator', viewport=DESKTOP)
    try:
        navigate(page, 'configuration/integrations')
        expect(page.locator('#main[data-loaded-view=configuration]')).to_be_visible()
        expect(page.locator('.config-pane .integration')).to_contain_text('A GitHub token cannot be saved on this server yet.')
        assert h.get('operator', '/api/integrations/github')['connected'] is False
    finally:
        context.close()

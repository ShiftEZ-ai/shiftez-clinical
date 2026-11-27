"""Cycle 4: checking a rota is not saving it; optional approval notes stay optional."""
import re
import pytest
from playwright.sync_api import Page, expect
from apps.api.errors import Problem
from apps.api.services import planning
from apps.worker.main import process_one
from .test_be18_command_latency import worker_up
from .test_browser import browser, live_server, sign_in, open_rota, navigate, wait_held  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.postgres,
              pytest.mark.parametrize('h', ['postgresql'], indirect=True)]


def no_save_state(page: Page):
    """D-4 (coordinator decision): until the person's own save the footer says nothing about saving: the element is
    hidden and has no accessible name."""
    state = page.locator('#save-state')
    expect(state).to_be_hidden()
    assert state.get_attribute('aria-label') is None


def test_open_current_rota_does_not_check_and_background_check_never_claims_save(browser, live_server, h):
    w = h.shift(h.workspace())['workspace']
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


WAITS = re.compile(r'^Waits for the rota check · \d+:\d\d$')
INCOMPLETE = 'Needs a complete check'
STOPPED_SAID = 'The rota check stopped. Check again.'


def submit_holds(page: Page, width, tip):
    """#1436: a draft's Submit is held in place (focusable, aria-disabled) with its reason beside it in words and as its
    description. A phone planner has no Submit."""
    submit = page.locator('.planner-bar').get_by_role('button', name='Submit for review…', exact=True)
    if width <= 400:
        expect(submit).to_have_count(0)
        return
    expect(submit).to_have_attribute('aria-disabled', 'true', timeout=20000)
    expect(submit).to_have_attribute('data-tip', tip, timeout=20000)
    why = submit.evaluate("b => (b.getAttribute('aria-describedby') || '').split(' ')"
                          ".map(id => document.getElementById(id)).find(n => n?.matches('.why-unavailable'))?.id || ''")
    assert why, "the reason is the held step's description"
    expect(page.locator('#' + why)).to_be_visible()
    expect(page.locator('#' + why)).to_have_text(tip)


def said(page: Page):
    """Every text the one polite region said since `listen`, in order."""
    return page.evaluate('() => window.__said.slice()')


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_assignment_save_holds_submit_on_one_clock_and_says_the_check_once(browser, live_server, h, monkeypatch, width, theme):
    typ = h.configure('assignment_types', {'name': 'Required attendance', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    first = h.shift(h.workspace(), typ=typ, people=[])
    w = h.shift(first['workspace'], '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00',
                typ=typ, people=[])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    shift_id = first['shift_id']
    causes = h.get('coordinator', f'/api/workspaces/{w["id"]}/fix-list?view=lean')['causes']
    cover = next(c for c in causes if shift_id in c['shift_ids'] and (c.get('session_findings') or {}).get(shift_id))
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 920}, color_scheme=theme)
    held = []

    def is_command(value, kind):
        request = value.request if hasattr(value, 'request') else value
        return request.url.endswith('/api/commands') and (request.post_data_json or {}).get('kind') == kind

    def hold_edit(route):
        if is_command(route, 'shift.update'):
            held.append(route)
        else:
            route.continue_()

    try:
        page.route(re.compile(r'.*/api/planner/fill-options\?async_options=true$'),
                   lambda route: route.continue_(url=route.request.url.split('?')[0]))
        if width <= 400:
            navigate(page, f'schedule/issues?draft={w["id"]}')
            expect(page.locator('.fix-page')).to_be_visible()
            row = page.locator(f'.fix-page li[data-cause="{cover["id"]}"]')
            row.locator('.fix-chip', has_text=re.compile(r'^\w{3} 4$')).click()
            panel = row.locator('.fix-fill-box')
        else:
            open_rota(page, w['id'])
            page.locator(f'#schedule-view [data-session~="{shift_id}"] .pchip.gap').first.click()
            panel = page.locator('.wk-panel')
        worker_up(h)
        page.evaluate("""() => { window.__said = []; let last = '';
            new MutationObserver(() => { const t = document.getElementById('loading-live')?.textContent || '';
                if (t !== last) { last = t; if (t) window.__said.push(t); } })
              .observe(document.body, {childList: true, subtree: true, characterData: true}); }""")
        page.route('**/api/commands', hold_edit)
        panel.locator('.wk-cand.ok').first.get_by_role('button', name=re.compile(r'^Assign ')).click()
        wait_held(page, held)
        # Saving: Submit waits from the save's start, on the clock.
        expect(page.locator('#save-state')).to_have_attribute('aria-label', 'Saving…')
        submit_holds(page, width, WAITS)
        with page.expect_response(lambda r: is_command(r, 'shift.update')) as saved:
            held.pop().continue_()
        assert saved.value.ok
        data = saved.value.json()
        assert data['validation']['scope'] == 'command_changes' and data['validation']['stale_since']
        assert saved.value.request.post_data_json['payload']['shift_id'] == shift_id
        assert shift_id in data['validation']['stale_shift_ids']
        assert data['fix_list_delta']['summary']['checking'] is True
        # Saved and checking: still the one clock, and nothing is said while the check runs.
        expect(page.locator('#save-state')).to_have_attribute('aria-label', 'Saved just now')
        check_line = page.locator('.fix-gate' if width <= 400 else '.app-footer')
        expect(check_line).to_contain_text('Waiting to check')
        submit_holds(page, width, WAITS)
        if width <= 400:
            expect(page.locator('.fix-body')).to_have_attribute('aria-busy', 'true')
        assert not [t for t in said(page) if t.startswith(('Rota checked', 'The rota check'))], said(page)

        def fail_once(*args, **kwargs):
            raise Problem(409, 'CHECK_FAILED', 'Synthetic check interruption')

        # Could not finish: Submit needs a complete check, and the stop is said once.
        with monkeypatch.context() as patch:
            patch.setattr(planning, 'revalidate', fail_once)
            assert process_one(h.app.state.engine)
        stopped = page.locator('.check-stopped')
        expect(stopped).to_be_visible(timeout=20000)
        submit_holds(page, width, INCOMPLETE)
        page.wait_for_function('words => window.__said.includes(words)', arg=STOPPED_SAID)
        with page.expect_response(lambda r: is_command(r, 'job.create')):
            stopped.get_by_role('button', name='Check again').click()
        assert process_one(h.app.state.engine)
        # Settled: the counts are said once, in one sentence, and Submit no longer waits for the check.
        expect(stopped).to_have_count(0, timeout=20000)
        page.wait_for_function("() => window.__said.some(t => t.startsWith('Rota checked'))")
        words = [t for t in said(page) if t.startswith(('Rota checked', 'The rota check'))]
        assert len(words) == 2 and words[0] == STOPPED_SAID, words
        assert re.fullmatch(r'Rota checked: [1-9]\d* must fix\.', words[1]), words
        if width > 400:
            submit = page.locator('.planner-bar').get_by_role('button', name='Submit for review…', exact=True)
            expect(submit).not_to_have_attribute('data-tip', WAITS)
            expect(submit).not_to_have_attribute('data-tip', INCOMPLETE)
            expect(check_line.get_by_role('button', name='Needs a check', exact=True)).to_have_count(0)
        # The late must fix toast (#1437) needs the settle receipt; a normal check offers none.
        expect(page.locator('#toast-tray .toast', has_text='with your edit')).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

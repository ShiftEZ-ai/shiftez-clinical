"""The session panel is live for the next edit as soon as its save answers; the whole-rota check runs behind it."""
import re

import pytest
from playwright.sync_api import expect

from apps.worker.main import process_one
from .test_browser import browser, live_server, sign_in, open_rota  # noqa: F401
from .test_be18_command_latency import worker_up

pytestmark = pytest.mark.browser
SAVE = 'Saved · checking the rest of the rota…'
HELD = 'Checking the latest change…'


def two_open_sessions(h):
    w = h.workspace('Next edit rota', '2026-10-04', '2026-10-11')
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[])['workspace']
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[], location=1)['workspace']
    return h.lifecycle(w, 'validate')['workspace']


def checks_open(h, w):
    return [j for j in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']
            if j['kind'] == 'validation' and j['status'] in ('queued', 'running')]


def assign_first(page, panel):
    """Assigns the first person who passes every rule; returns their name and the shift.update sent."""
    first = panel.locator('.wk-cand.ok').first
    expect(first).to_be_visible(timeout=20000)
    name = first.locator('strong').inner_text()
    with page.expect_request(lambda r: r.url.endswith('/api/commands') and r.method == 'POST'
                             and (r.post_data_json or {}).get('kind') == 'shift.update') as sent:
        first.get_by_role('button', name=re.compile(r'^Assign (?!.* with a warning$)')).click()
    return name, sent.value


def test_panel_is_live_while_the_whole_rota_check_runs_and_submit_waits_for_it(browser, live_server, h):
    w = two_open_sessions(h)
    worker_up(h)  # a live worker: each edit queues the complete check, which no worker runs until process_one
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        # Who can work a session is asked synchronously here, so only the rota check waits in the job queue.
        page.route(re.compile(r'.*/api/planner/fill-options\?async_options=true$'),
                   lambda route: route.continue_(url=route.request.url.split('?')[0]))
        open_rota(page, w['id'])
        page.locator('.pchip.gap').first.click()
        panel = page.locator('.wk-panel')
        expect(panel).to_contain_text('Can work this session', timeout=20000)
        first, sent = assign_first(page, panel)
        assert sent.post_data_json['expected_revision'] == w['revision']
        # (a) The save's reply makes the panel editable: its own result, and the check behind it in words.
        expect(panel.get_by_role('status').filter(has_text=first + ' added to the draft')).to_be_visible(timeout=20000)
        slot = panel.locator('.wk-save-slot')
        expect(slot).to_have_text(SAVE)
        assert checks_open(h, w), 'the complete check is still waiting for a worker'
        expect(panel.locator('.wk-panel-body button:disabled')).to_have_count(0)
        # (c) Submit is held, focusable, with the reason.
        submit = page.locator('.planner-bar').get_by_role('button', name='Submit for review…', exact=True)
        expect(submit).to_have_attribute('aria-disabled', 'true')
        expect(submit).to_have_attribute('data-tip', HELD)
        # (b) A second edit while the check runs is sent at the current revision and saved.
        current = h.detail(w)['workspace']['revision']
        assert current > w['revision']
        page.locator('.pchip.gap').first.click()
        expect(panel).to_contain_text('Can work this session', timeout=20000)
        second, sent = assign_first(page, panel)
        assert sent.post_data_json['expected_revision'] == current
        expect(panel.get_by_role('status').filter(has_text=second + ' added to the draft')).to_be_visible(timeout=20000)
        expect(panel.locator('.wk-save-slot')).to_have_text(SAVE)
        expect(submit).to_have_attribute('aria-disabled', 'true')
        # (d) Once the worker runs the checks, only the latest revision's result is drawn: the holds release and the slot
        # empties in place.
        for _ in range(5):
            if not checks_open(h, w):
                break
            process_one(h.app.state.engine)
        assert not checks_open(h, w)
        latest = h.detail(w)['workspace']
        assert not latest['validation'].get('scope') and latest['revision'] >= current
        expect(submit).not_to_have_attribute('aria-disabled', 'true', timeout=20000)
        expect(panel.locator('.wk-save-slot')).to_have_count(1)
        expect(panel.locator('.wk-save-slot')).to_have_text('')
        expect(page.locator('.app-footer').get_by_role('status').filter(has_text=re.compile('Waiting to check|Checking the rota'))).to_have_count(0)
        assert len({p['person_id'] for s in h.detail(w)['shifts'] for p in s['participants']}) == 2
    finally:
        context.close()

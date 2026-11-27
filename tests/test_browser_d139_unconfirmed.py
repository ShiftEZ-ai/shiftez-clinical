"""D-139: a save whose reply was lost is settled by its receipt (GET /api/commands/{key}), never said "Not saved" when the
server applied it; a refused replay is said once and never blocks another rota's save, nor comes back after a reload.
Replies are held or replaced with page.route; nothing waits on a clock."""
import json
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, open_rota  # noqa: F401 (fixtures)
from .test_planner_redo import move_to, people

pytestmark = pytest.mark.browser
STALE = json.dumps({'error': {'code': 'STALE_REVISION', 'message': 'Another session changed this record; reload and reconcile'}})


def edited(h, name):
    first, second = h.seed['people'][:2]
    return move_to(h, h.shift(h.workspace(name), people=[first])['workspace'], second), first, second


def undo_route(page, first):
    """The first Undo: `first` ('commit': the server applies it, its reply is lost; 'lose': lost before the server);
    its replay is refused STALE_REVISION, as when the rota moved on before the replay was admitted."""
    seen = []

    def handle(route):
        body = route.request.post_data_json or {}
        if route.request.method != 'POST' or body.get('kind') != 'workspace.undo' or len(seen) >= 2:
            return route.fallback()
        seen.append(route.request.headers.get('idempotency-key'))
        if len(seen) == 1:
            if first == 'commit':
                route.fetch()
            return route.abort('failed')
        return route.fulfill(status=409, content_type='application/json', body=STALE)
    page.route('**/api/commands', handle)
    return seen


def undo(page):
    page.locator('.planner-bar').get_by_role('button', name='Undo last edit').click()


def test_an_undo_applied_but_unanswered_says_undone(browser, live_server, h):
    w, first, _ = edited(h, 'D139 applied undo')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, w['id'])
        keys = undo_route(page, 'commit')
        with page.expect_response(lambda r: '/api/commands/' in r.url and r.request.method == 'GET') as receipt:
            undo(page)
        assert receipt.value.ok and receipt.value.json()['status'] == 'committed'
        expect(page.locator('.toast').filter(has_text='Undone')).to_be_visible()
        assert len(keys) == 2 and keys[0] == keys[1]  # one replay with the same key, nothing more
        assert people(h, w) == [[first]]
        expect(page.get_by_text('Not saved', exact=False)).to_have_count(0)
        expect(page.get_by_text('Another session changed this record', exact=False)).to_have_count(0)
    finally:
        context.close()


def refused_then_other_rota(browser, live_server, h, reload):
    a, a_first, a_second = edited(h, 'D139 rota A')
    b, b_first, _ = edited(h, 'D139 rota B')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, a['id'])
        keys = undo_route(page, 'lose')
        with page.expect_response(lambda r: '/api/commands/' in r.url and r.request.method == 'GET') as receipt:
            undo(page)
        assert receipt.value.status == 404  # nothing committed: the refusal stands
        expect(page.locator('.toast').filter(has_text='Nothing was undone')).to_be_visible()
        assert len(keys) == 2 and people(h, a) == [[a_second]]
        sent = []
        page.on('request', lambda r: sent.append(r.post_data_json) if r.url.endswith('/api/commands') and r.method == 'POST' else None)
        if reload:
            page.reload()
        open_rota(page, b['id'])
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'workspace.undo') as done:
            undo(page)
        assert done.value.ok, done.value.text()
        assert done.value.request.post_data_json['target_id'] == b['id']
        assert done.value.request.headers['idempotency-key'] not in keys
        # has_text is a case-insensitive substring: A's 'Nothing was undone' alert stays until dismissed, so name B's.
        expect(page.locator('.toast.success').filter(has_text='Undone')).to_be_visible()
        assert people(h, b) == [[b_first]] and people(h, a) == [[a_second]]
        # Rota A's refused Undo is never sent again: not alongside B's, not after a reload.
        assert not [c for c in sent if c and c.get('target_id') == a['id'] and c.get('kind') == 'workspace.undo']
    finally:
        context.close()


def test_after_a_refused_replay_another_rota_saves(browser, live_server, h):
    refused_then_other_rota(browser, live_server, h, reload=False)


def test_after_a_reload_nothing_refused_is_sent_again(browser, live_server, h):
    refused_then_other_rota(browser, live_server, h, reload=True)


def test_an_open_session_form_never_outlives_its_rota(browser, live_server, h):
    """D-139: Add session open on rota A with a change; the address moves to rota B: the panel's own Keep editing /
    Discard changes asks; Discard closes it, rota B shows, and a session saved there lands on B (A untouched)."""
    a, b = h.workspace('D139 form rota A'), h.workspace('D139 form rota B')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, a['id'])
        page.locator('#main').get_by_role('button', name='Add session', exact=True).click()
        form = page.locator('.dock-panel')
        form.get_by_label('Location', exact=True).select_option(h.seed['locations'][0])
        page.evaluate("id => { location.hash = 'planner/' + id; }", b['id'])
        bar = form.locator('.unsaved-bar')
        expect(bar).to_contain_text('Not saved.')
        expect(bar.get_by_role('button', name='Keep editing', exact=True)).to_be_focused()
        bar.get_by_role('button', name='Discard changes', exact=True).click()
        expect(page.locator('.dock-panel')).to_have_count(0)
        expect(page).to_have_url(re.compile(re.escape(b['id'])))
        page.locator('#main').get_by_role('button', name='Add session', exact=True).click()
        form = page.locator('.dock-panel')
        form.get_by_label('Duty type', exact=True).select_option(h.seed['type'])
        form.get_by_label('Location', exact=True).select_option(h.seed['locations'][0])
        form.get_by_label('Continuity clinic', exact=True).check()  # the duty type needs a clinic
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'shift.create') as saved:
            form.get_by_role('button', name='Save draft session', exact=True).click()
        assert saved.value.ok, saved.value.text()
        assert saved.value.request.post_data_json['payload']['workspace_id'] == b['id']
        expect(page.locator('.dock-panel')).to_have_count(0)
        assert len(h.detail(b)['shifts']) == 1 and h.detail(a)['shifts'] == []
    finally:
        context.close()

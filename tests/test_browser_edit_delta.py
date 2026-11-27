"""RESP-REVIEW: a draft edit's reply that carries its delta (edit, Undo, Redo; build/handoff/EDIT-DELTA.md) patches the
draft the page holds, so the Schedule redraws without reading the whole rota again (up to 5 MB on dev); a stale delta, or
one that does not follow on from the copy held, falls back to the whole read. The delta is added to the real server's
reply here, so the client path is tested before and independently of the server producing it."""
import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in, route_fetch  # noqa: F401
from .test_browser_planner import two_people_week

pytestmark = pytest.mark.browser

APPLY = """async ([held, reply]) => (await window.importBundle('edit_delta.js')).applyEditDelta(held, reply)"""
APPLY_CHECK = """async ([held, reply]) => (await window.importBundle('edit_delta.js')).applyCheckDelta(held, reply)"""


def held(**workspace):
    return {'workspace': {'id': 'w', 'revision': 4, 'validation': {'fingerprint': 'old', 'error_count': 2,
            'findings': [{'shift_id': 'a', 'person_id': 'p1', 'code': 'REST'}, {'shift_id': 'z', 'person_id': 'p2', 'code': 'HOURS'},
                         {'shift_id': 'y', 'person_id': 'p3', 'code': 'KEEP'}],
            'coverage': [{'shift_id': 'a', 'required': 1}, {'shift_id': 'b', 'required': 1}]}, **workspace},
            'shifts': [{'id': 'b', 'start': '2026-10-05T08:00:00+00:00', 'participants': [{'id': 'pb', 'person_id': 'p2'}]},
                       {'id': 'a', 'start': '2026-10-04T08:00:00+00:00', 'participants': [{'id': 'pa', 'person_id': 'p1'}]}],
            'current_fingerprint': 'old', 'current_input_revision': 1, 'current_availability_revision': 1}


def reply(**delta):
    return {'workspace': {'id': 'w', 'revision': 5, 'validation': {'fingerprint': 'new', 'error_count': 0}},
            'health': {'open_places': 0},
            'delta': {'base_revision': 4, 'revision': 5, 'current_fingerprint': 'new', **delta}}


def check_reply(*, stale=False, partial=False):
    return {'delta': {'kind': 'complete_check', 'workspace_id': 'w', 'revision': 4,
                      'current_fingerprint': 'checked', 'stale': stale, 'partial': partial,
                      'validation': {'fingerprint': 'checked', 'findings': [{'code': 'NEW'}], 'coverage': []}}}


def test_complete_check_delta_patches_the_held_validation_and_falls_back_on_uncertain_input(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        checked = page.evaluate(APPLY_CHECK, [held(), check_reply()])
        assert checked['workspace']['validation']['findings'] == [{'code': 'NEW'}]
        assert checked['workspace']['validation_current'] is True
        assert checked['current_fingerprint'] == 'checked'
        for case in ([held(), {'delta': None}], [held(), check_reply(stale=True)],
                     [held(), check_reply(partial=True)], [{**held(), 'partial': True}, check_reply()],
                     [held(revision=3), check_reply()], [None, check_reply()]):
            assert page.evaluate(APPLY_CHECK, case) is None
    finally:
        context.close()


def test_a_delta_patches_the_held_draft_and_anything_else_reads_it_whole(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        # Sessions upserted and removed, assignments moved, findings and coverage replaced inside their scope only.
        new = page.evaluate(APPLY, [held(), reply(
            shifts=[{'id': 'c', 'start': '2026-10-04T06:00:00+00:00', 'participants': []}], removed_shift_ids=['b'],
            assignments=[{'id': 'pn', 'shift_id': 'a', 'person_id': 'p9'}], removed_assignment_ids=['pa'],
            findings={'shift_ids': ['a'], 'person_ids': ['p2'], 'items': [{'shift_id': 'a', 'person_id': 'p9', 'code': 'NEW'}]},
            coverage={'shift_ids': ['a', 'b'], 'person_ids': ['p2'],
                      'items': [{'shift_id': 'a', 'required': 1, 'scheduled': 1}]})])
        assert [s['id'] for s in new['shifts']] == ['c', 'a']  # the server's order: start, then id
        assert new['shifts'][1]['participants'] == [{'id': 'pn', 'person_id': 'p9'}]
        assert sorted(f['code'] for f in new['workspace']['validation']['findings']) == ['KEEP', 'NEW']
        assert new['workspace']['validation']['coverage'] == [{'shift_id': 'a', 'required': 1, 'scheduled': 1}]
        assert new['workspace']['revision'] == 5 and new['workspace']['validation']['error_count'] == 0
        assert new['current_fingerprint'] == 'new' == new['workspace']['validation']['fingerprint']
        # A session sent whole wins over assignment patches for it: its people stay in the row's own order.
        whole = page.evaluate(APPLY, [held(), reply(
            shifts=[{'id': 'a', 'start': '2026-10-04T08:00:00+00:00', 'participants': [{'id': 'pz', 'person_id': 'p8'}, {'id': 'pa', 'person_id': 'p1'}]}],
            assignments=[{'id': 'pz', 'shift_id': 'a', 'person_id': 'p8'}])])
        assert [p['id'] for p in next(s for s in whole['shifts'] if s['id'] == 'a')['participants']] == ['pz', 'pa'], whole
        # Without findings or coverage in the delta, the held ones stay.
        same = page.evaluate(APPLY, [held(), reply()])
        assert len(same['workspace']['validation']['findings']) == 3 and len(same['shifts']) == 2
        # Each of these reads the rota whole instead.
        for case in ([held(), {**reply(), 'delta': None}],
                     [held(), reply(stale=True)],
                     [held(revision=3), reply()],                                   # the copy held is behind
                     [held(), {**reply(), 'workspace': {'id': 'w', 'revision': 6}}],  # the reply is not that revision
                     [held(), reply(current_fingerprint=None)],
                     [{**held(), 'partial': True}, reply()],                       # only a window is held
                     [held(base_publication_id='pub'), reply()],                   # an amendment's change list
                     [held(), reply(assignments=[{'id': 'x', 'shift_id': 'missing', 'person_id': 'p1'}])],
                     [None, reply()]):
            assert page.evaluate(APPLY, case) is None, case
    finally:
        context.close()


def _with_delta(page, h, w, stale=False):
    """Adds a delta to the real replies of a Remove (shift.update) and its Undo, built from the rota as it was."""
    before = {s['id']: s for s in h.detail(w)['shifts']}
    state = {'revision': w['revision']}

    def handle(route):
        body = route.request.post_data_json or {}
        if route.request.method != 'POST' or body.get('kind') not in ('shift.update', 'workspace.undo'):
            return route.continue_()
        response = route_fetch(route)
        data = response.json()
        if response.status != 200:
            return route.fulfill(response=response)
        check = data.get('validation') or data['workspace'].get('validation') or {}
        if body['kind'] == 'shift.update':
            sid = body['payload']['shift_id']
            kept = {p['person_id'] for p in body['payload']['participants']}
            shift = {**before[sid], 'participants': [p for p in before[sid]['participants'] if p['person_id'] in kept]}
        else:
            shift = before[state['shift']]
        state['shift'] = shift['id']
        data['delta'] = {'base_revision': state['revision'], 'revision': data['workspace']['revision'], 'stale': stale,
                         'shifts': [shift], 'findings': {'shift_ids': [shift['id']], 'items': []},
                         'current_fingerprint': check.get('fingerprint')}
        state['revision'] = data['workspace']['revision']
        route.fulfill(response=response, json=data)
    page.route('**/api/commands', handle)


def _open(page, w):
    navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
    chips = page.locator('.wk-grid button.pchip.draggable')
    expect(chips).to_have_count(2)  # draggable: the whole rota is held (not only the window)
    return chips


def _whole_reads(page, w):
    seen = []
    page.on('request', lambda r: seen.append(r.url) if r.method == 'GET' and urlsplit(r.url).path == f"/api/workspaces/{w['id']}" else None)
    return seen


def test_an_edit_and_its_undo_redraw_from_the_delta_without_reading_the_rota(browser, live_server, h):
    w = two_people_week(h, 'Delta edit rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        _with_delta(page, h, w)
        chips = _open(page, w)
        seen = _whole_reads(page, w)
        chips.nth(1).click(button='right')
        page.get_by_role('menuitem', name=re.compile('^Remove')).click()
        undo = page.locator('.toast').get_by_role('button', name='Undo', exact=True)
        expect(undo).to_be_visible()
        expect(chips).to_have_count(1)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        undo.click()
        expect(chips).to_have_count(2)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        assert seen == [], seen
        # The redraw after a save is measured (app.ts renderPage: the PC's step 3 decision reads it).
        assert page.evaluate("() => performance.getEntriesByName('render:schedule:redraw').length") >= 2
        assert sorted(len(s['participants']) for s in h.detail(w)['shifts']) == [1, 1]  # the server agrees
    finally:
        context.close()


def test_a_stale_delta_reads_the_rota_whole(browser, live_server, h):
    w = two_people_week(h, 'Stale delta rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        _with_delta(page, h, w, stale=True)
        chips = _open(page, w)
        chips.nth(1).click(button='right')
        with page.expect_request(lambda r: r.method == 'GET' and urlsplit(r.url).path == f"/api/workspaces/{w['id']}"):
            page.get_by_role('menuitem', name=re.compile('^Remove')).click()
        expect(chips).to_have_count(1)
    finally:
        context.close()

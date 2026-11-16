"""RESP-REVIEW: every control in a planner grid's cells is handled once per grid and reads its session, findings and
roster by id when used, in the draft as held now (WeekHooks.live), never what the grid was drawn from, so a cell kept on
screen after an edit's delta can never act on, or say, something stale."""

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401

pytestmark = pytest.mark.browser

GRID = """async () => {
  const m = await window.importBundle('planner_week.js');
  window.__calls = [];
  const days = Array.from({length: 7}, (_, i) => `2026-10-${String(4 + i).padStart(2, '0')}`);
  // Each session at its own time: its own row, so each cell holds one selection box.
  const hour = {s1: '08', s2: '13', s3: '18'};
  const session = (id, participants, extra = {}) => ({id, workspace_id: 'w', assignment_type_id: 't', location_id: 'l', service_ids: ['v'],
    start: `2026-10-04T${hour[id]}:00:00+03:00`, end: `2026-10-04T${hour[id]}:30:00+03:00`, attributes: {}, participants,
    type_snapshot: {name: 'Clinic duty', definition: {fields: [], requirements: []}}, ...extra});
  const ctx = {me: {organisation: {timezone: 'Asia/Riyadh', week_start: 6}, person_id: null},
    catalogues: {people: [{id: 'p1', display_name: 'Pat One'}, {id: 'p2', display_name: 'Sam Two'}, {id: 'p3', display_name: 'Lee Three'}],
      services: [{id: 'v', name: 'Clinic'}], locations: [{id: 'l', name: 'Room'}], memberships: [], categories: [], assignment_types: []}};
  const drawn = {workspace: {id: 'w', revision: 1, status: 'draft', starts_on: '2026-10-04', ends_on: '2026-10-11',
      validation: {findings: [], coverage: []}},
    shifts: [session('s1', [{id: 'a1', person_id: 'p1'}]), session('s2', [{id: 'b1', person_id: 'p2'}], {locked: true}), session('s3', [])], wishes: []};
  // Held now: a rule broken on Pat's session and someone added to the locked one, both since the draw.
  const live = {...drawn, workspace: {...drawn.workspace, revision: 7, validation: {findings: [
      {shift_id: 's1', person_id: 'p1', severity: 'error', code: 'REST', message: 'Live rest rule'}], coverage: []}},
    shifts: [drawn.shifts[0], session('s2', [{id: 'b1', person_id: 'p2'}, {id: 'b2', person_id: 'p3'}], {locked: true}), drawn.shifts[2]]};
  const selected = new Set();
  const hooks = {name: p => (p && p.display_name) || '', code: () => 'X', details: s => window.__calls.push(['details', s.id]), dnd: n => n,
    proposal: null, editable: true, fresh: true, selected, onSelect: () => window.__calls.push(['select', [...selected]]),
    refresh: async () => {}, tallies: true, live: () => live, lock: async (s, on) => { window.__calls.push(['lock', s.id, s.participants.length, on]); }};
  const root = m.weekGrid(ctx, drawn, drawn.shifts, {days, page: 0, has_previous: false, has_next: false, unit: 'week'}, () => {}, hooks);
  // In a modal dialog (the top layer): the page's fixed bars never cover it, and its cards open inside it.
  const layer = document.createElement('dialog'); layer.id = 'cells'; layer.setAttribute('aria-label', 'Cells'); layer.append(root);
  document.body.append(layer); layer.showModal();
}"""

CONTROLS = {'name': 'button.pchip[data-assignment="a1"]', 'lock': 'button.wk-lock[data-shift="s2"]',
            'select': 'input.wk-check[data-shift="s1"]', 'no one yet': 'button.pchip.empty[data-shift="s3"]'}


def test_cell_controls_have_no_listeners_of_their_own_and_read_the_held_draft(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        page.evaluate(GRID)
        cdp = context.new_cdp_session(page)
        for name, selector in CONTROLS.items():
            expect(page.locator('#cells ' + selector)).to_have_count(1)
            node = cdp.send('Runtime.evaluate', {'expression': f"document.querySelector('#cells {selector}')"})['result']
            own = {x['type'] for x in cdp.send('DOMDebugger.getEventListeners', {'objectId': node['objectId']})['listeners']}
            assert not own & {'click', 'change', 'contextmenu', 'mouseenter', 'mouseover'}, (name, own)
        # A name's card is built when shown, from the held draft: the rule broken since the draw is on it.
        page.locator('#cells ' + CONTROLS['name']).hover()
        expect(page.locator('#cells .hovercard')).to_contain_text('Live rest rule')
        # The lock acts on the session as held now (two people on it), the box ticks the session.
        page.locator('#cells ' + CONTROLS['lock']).click()
        page.locator('#cells .wk-check-hit:has(input[data-shift="s1"])').click()
        page.wait_for_function("() => window.__calls.length >= 2")
        calls = page.evaluate('window.__calls')
        assert ['lock', 's2', 2, False] in calls, calls
        assert ['select', ['s1']] in calls, calls
    finally:
        context.close()


def test_the_session_panel_shows_the_session_as_held_now(browser, live_server, h):
    """The panel a name opens is drawn from the held draft: someone added to the session since the grid was drawn is on it."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        page.evaluate(GRID)
        page.locator('#cells button.pchip[data-assignment="b1"]').click()
        panel = page.locator('#cells .wk-panel')
        expect(panel).to_be_visible()
        panel.get_by_role('button', name='Whole session', exact=True).click()
        expect(panel).to_contain_text('Lee Three')
    finally:
        context.close()

"""Scoped live updates: a pushed change refreshes only the open pages that show what it touched (onPulse, paths[].live)."""
import uuid
from collections import Counter
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from .test_browser import PAGE_LOAD_MS, browser, live_server, sign_in, navigate  # noqa: F401
from .test_browser import whole_list

pytestmark = pytest.mark.browser

SETTLE = '() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))'
# Every pulse the app's event stream delivers, as the app receives it.
RECORD_PULSES = """(() => {const add = EventSource.prototype.addEventListener; window.livePulses = [];
  EventSource.prototype.addEventListener = function (type, listener, options) {
    const wrapped = type === 'pulse' ? e => {window.livePulses.push(JSON.parse(e.data)); listener(e);} : listener;
    return add.call(this, type, wrapped, options);};})()"""


def command(page, kind, payload, target=None, revision=None):
    """A save from another tab, through the live server: its stream announces the change at once."""
    reply = page.evaluate("""async ([kind, payload, key, target, revision]) => {
        const csrf = document.cookie.split('; ').find(c => c.startsWith('rota_csrf=')).split('=')[1];
        const r = await fetch('/api/commands', {method: 'POST', headers: {'Content-Type': 'application/json',
            'X-CSRF-Token': csrf, 'Idempotency-Key': key}, body: JSON.stringify({kind, payload, target_id: target, expected_revision: revision})});
        return {status: r.status, body: await r.text()};}""", [kind, payload, str(uuid.uuid4()), target, revision])
    assert reply['status'] == 200, reply


class Reads:
    """The reads a page starts (GET /api/…, the event stream aside), by route with its query."""
    def __init__(self, page):
        self.page, self.seen, self.pending = page, [], set()
        page.on('request', self.started)
        page.on('requestfinished', lambda r: self.pending.discard(r))
        page.on('requestfailed', lambda r: self.pending.discard(r))

    def started(self, request):
        url = urlsplit(request.url)
        if request.method == 'GET' and url.path.startswith('/api/') and url.path != '/api/events':
            self.seen.append(url.path + ('?' + url.query if url.query else ''))
            self.pending.add(request)

    def caused_by(self, scope, change):
        """Make `change`, wait for the pulse naming `scope`, step past the 300 ms window in which pulses are merged, then
        wait for the reads it started to finish; return them by route."""
        since = self.page.evaluate('window.livePulses.length')
        self.seen.clear()
        change()
        self.page.wait_for_function("""([since, scope]) => window.livePulses.slice(since)
            .some(p => (p.changes || []).some(c => c.scope === scope))""", arg=[since, scope], timeout=15000)
        self.page.clock.run_for(1000)
        for _ in range(1000):  # bounded: each round is two frames
            self.page.evaluate(SETTLE)
            if not self.pending:
                break
        assert not self.pending, [r.url for r in self.pending]
        return Counter(self.seen)


def test_a_live_change_reads_only_on_the_pages_that_show_it(browser, live_server, h):
    """Another tab adds a tag (a list) and records leave (availability). The Requests page open in this tab reads
    nothing for the first, and reads its list once for the second, without the account or the lists."""
    planner = sign_in(browser, live_server, 'coordinator')[1]
    context, page = sign_in(browser, live_server, 'approver')
    try:
        context.add_init_script(RECORD_PULSES)
        page.reload()
        expect(page.locator('#main[data-loaded-view]')).to_have_count(1, timeout=PAGE_LOAD_MS)
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        page.wait_for_function('() => window.livePulses.length > 0', timeout=15000)  # the stream's starting point
        reads = Reads(page)
        unrelated = reads.caused_by('catalogue', lambda: command(planner, 'catalogue.create', {'catalogue': 'tags',
            'value': {'name': 'Live scope tag', 'labels': ['people'], 'colour': 'violet'}, 'reason': 'Fictional'}))
        assert not unrelated, f'a change Requests does not show read {dict(unrelated)}'
        related = reads.caused_by('availability', lambda: command(planner, 'availability.record', {
            'person_id': h.seed['people'][0], 'kind': 'recorded_leave', 'start': '2026-10-04T00:00:00+03:00',
            'end': '2026-10-05T00:00:00+03:00', 'private_reason': 'Fictional leave'}))
        lists = {route: n for route, n in related.items() if route.startswith('/api/requests/all?') and 'status=waiting' not in route}
        assert sum(lists.values()) == 1, f'the Requests list was not read exactly once: {dict(related)}'
        assert all(n == 1 for n in related.values()), f'a route was read more than once: {dict(related)}'
        assert '/api/me' not in related and not any(r.startswith('/api/catalogues') for r in related), dict(related)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'requests')
    finally:
        context.close()
        planner.context.close()


def test_a_rota_change_only_refreshes_the_open_rota(browser, live_server, h):
    """A workspace id is a place: a change to rota A does not redraw rota B."""
    rota_a = h.shift(h.workspace('Live rota A'))['workspace']
    rota_b = h.workspace('Live rota B')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        context.add_init_script(RECORD_PULSES)
        page.reload()
        expect(page.locator('#main[data-loaded-view]')).to_have_count(1, timeout=PAGE_LOAD_MS)
        navigate(page, f'schedule/week/2026-10-04?edit={rota_b["id"]}')
        page.wait_for_function('() => window.livePulses.length > 0', timeout=15000)
        # The app's own minute check (a poll of /api/pulse, in full when the state moved) runs on real time and lands
        # in this window on a loaded runner. A hidden tab skips it; pushed pulses are still applied (app.ts setInterval).
        page.evaluate("() => Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'})")
        reads = Reads(page)
        before = page.evaluate('window.livePulses.length')
        reads.seen.clear()
        command(page, 'shift.create', {
            'workspace_id': rota_a['id'], 'assignment_type_id': h.seed['type'],
            'location_id': h.seed['locations'][0], 'service_ids': [h.seed['services'][0]],
            'start': '2026-10-05T08:00:00+03:00', 'end': '2026-10-05T12:00:00+03:00',
            'participants': [{'person_id': h.seed['people'][0], 'start': '2026-10-05T08:00:00+03:00', 'end': '2026-10-05T12:00:00+03:00'}],
            'attributes': {}, 'override_reason': 'Scoped live update test'}, target=None, revision=rota_a['revision'])
        page.wait_for_function("([before]) => window.livePulses.length > before && window.livePulses.slice(before).some(p => (p.changes || []).some(c => c.scope === 'workspace' && c.id === '%s'))" % rota_a['id'], arg=[before], timeout=15000)
        # The one read it causes is the Schedule menu's rota list (its count of the rota changed elsewhere); the open
        # rota B and everything else are not read again.
        menu_list = '/api/workspaces?limit=50&offset=0'
        with page.expect_request(lambda r: menu_list in r.url):
            page.clock.run_for(1000)
        for _ in range(1000):  # bounded: each round is two frames
            page.evaluate(SETTLE)
            if not reads.pending:
                break
        assert reads.seen == [menu_list], reads.seen
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
    finally:
        context.close()


def test_an_open_request_form_is_not_reset_by_a_related_live_change(browser, live_server, h):
    """Scoped updates wait behind a dirty/open form and preserve its entered value."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        context.add_init_script(RECORD_PULSES)
        page.reload()
        expect(page.locator('#main[data-loaded-view]')).to_have_count(1, timeout=PAGE_LOAD_MS)
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        page.wait_for_function('() => window.livePulses.length > 0', timeout=15000)
        page.get_by_role('button', name='New request', exact=True).click()
        dialog = page.get_by_role('dialog', name='New request')
        expect(dialog).to_be_visible()
        # The kind first, then its fields (requests.ts newRequest): a coordinator records leave.
        dialog.get_by_role('radio', name='Leave or absence', exact=True).check()
        reason = dialog.get_by_label('Reason', exact=True)
        reason.fill('Keep this unsaved text')
        before = page.evaluate('window.livePulses.length')
        command(page, 'availability.record', {
            'person_id': h.seed['people'][0], 'kind': 'recorded_leave',
            'start': '2026-10-06T00:00:00+03:00', 'end': '2026-10-07T00:00:00+03:00',
            'private_reason': 'Scoped live update test'}, target=None, revision=None)
        page.wait_for_function("([before]) => window.livePulses.length > before && window.livePulses.slice(before).some(p => (p.changes || []).some(c => c.scope === 'availability'))", arg=[before], timeout=15000)
        page.clock.run_for(1000)
        expect(dialog).to_be_visible()
        expect(reason).to_have_value('Keep this unsaved text')
    finally:
        context.close()

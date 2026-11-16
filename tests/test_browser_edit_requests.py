"""P0 edit latency (LEAD-UIUX, maintenance/005/008): after an Assign, its Undo and its Redo, the edit's own reply is the only
source of the new must-fix count and the chips. Between the press and the edit drawn, the client sends the edit and
nothing that re-reads what the reply carries: no rota detail, no rota list, no Fix list, no summary. The footer, its
Rota health popover and the Fix list panel then say the same count. Real clicks on server findings.

(The rota's own check after an edit is the server's background work; how its result arrives is LEAD-PERFORMANCE's
frozen contract, asserted here once it lands.)"""
import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from .test_browser import DRAFT_CHIP, browser, live_server, sign_in  # noqa: F401
from .test_browser_fix_list_panel import DESKTOP, real_gaps
from .test_browser_fixlist_undo import open_panel_at, must_counts, settled, people_on

pytestmark = pytest.mark.browser

# What an edit's reply already carries: reading any of these again between the press and the edit drawn is a refetch.
REFETCH = re.compile(r'^/api/workspaces(\?|/[^/?]+(\?|$)|/[^/?]+/(fix-list|summary|rule-map|parts))')


class Sent:
    """Every /api request the page sends (the live stream and pulse aside), in order: (method, path with query)."""
    def __init__(self, page):
        self.log = []
        def seen(r):
            u = urlsplit(r.url)
            if u.path.startswith('/api/') and u.path not in ('/api/events', '/api/pulse'):
                self.log.append((r.method, u.path + ('?' + u.query if u.query else '')))
        page.on('request', seen)

    def mark(self):
        return len(self.log)

    def since(self, at):
        return self.log[at:]


def no_refetch(sent, at, edit):
    seen = sent.since(at)
    assert ('POST', '/api/commands') in seen, seen
    refetched = [(m, p) for m, p in seen if m == 'GET' and REFETCH.match(p)]
    assert not refetched, f'{edit}: re-read what the reply carries: {refetched}'
    # The fix in place for the next session asks once at most.
    assert sum(p == '/api/planner/fill-options' for _, p in seen) <= 1, seen


# What the rota's own check after an edit may read at most once: settled from the job's complete-check delta it reads
# none of these; without a worker (this harness) or on a null/stale/partial delta, the governed full read reads each once.
# The live capture (c31f9cd) saw the rota read three times, the rota list three times and the Fix list twice per edit.
ONCE = {'rota': re.compile(r'^/api/workspaces/[^/?]+(\?|$)'), 'rota list': re.compile(r'^/api/workspaces\?'),
        'Fix list': re.compile(r'^/api/workspaces/[^/?]+/fix-list'), 'whole job': re.compile(r'^/api/jobs/[^/?]+$')}


def once_after_check(sent, at, edit):
    seen = [p for m, p in sent.since(at) if m == 'GET']
    for what, pattern in ONCE.items():
        n = sum(bool(pattern.match(p)) for p in seen)
        assert n <= 1, f'{edit} and its check read the {what} {n} times: {seen}'


def test_assign_undo_and_redo_draw_from_their_own_reply(browser, live_server, h):
    w, gaps, gap = real_gaps(h, 'edit requests')
    before_people = people_on(h, w, gaps[0])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    sent = Sent(page)
    try:
        row = open_panel_at(page, w, gap)
        settled(page)
        before = must_counts(page)

        # Assign: the ✓ chip with who and the counts come from the reply.
        row.locator('.fix-chip', has_text=re.compile(r'^\w{3} 8')).click()
        assign = row.locator('.fix-fill-box').get_by_role('button', name=re.compile(r'^Assign '))
        expect(assign.first).to_be_visible()
        chips = page.locator(f'#schedule-view {DRAFT_CHIP}[data-rk*=\'"shift":"{gaps[0]}"\']')
        had = chips.count()
        assert had == before_people == 1
        at = sent.mark()
        assign.first.click()
        expect(row.locator('.fix-chip.done')).to_have_count(1)
        toast = page.locator('#toast-tray .toast').filter(has=page.get_by_role('button', name='Undo', exact=True)).first
        expect(toast).to_be_visible()
        # maintenance: the editor's own cell is drawn from the reply at once: the person in it, its open place gone.
        expect(chips).to_have_count(had + 1)
        expect(page.locator(f'#schedule-view [data-session~="{gaps[0]}"] .wk-open-row')).to_have_count(0)
        no_refetch(sent, at, 'Assign')  # with no read behind it
        assert people_on(h, w, gaps[0]) == before_people + 1
        toast.hover()  # the message waits while the pointer is on it
        settled(page)
        once_after_check(sent, at, 'Assign')

        # Undo from the toast: its own reply puts the session back; the ✓ chip goes.
        at = sent.mark()
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('#toast-tray .toast').filter(has_text=re.compile(r'^Undone'))).to_be_visible()
        expect(row.locator('.fix-chip.done')).to_have_count(0)
        no_refetch(sent, at, 'Undo')
        assert people_on(h, w, gaps[0]) == before_people
        settled(page)
        once_after_check(sent, at, 'Undo')
        assert must_counts(page) == before

        # Redo from the top bar: the same edit again, from its own reply.
        at = sent.mark()
        page.locator('.planner-bar').get_by_role('button', name='Redo', exact=True).click()
        expect(row.locator('.fix-chip.done')).to_have_count(1)
        no_refetch(sent, at, 'Redo')
        assert people_on(h, w, gaps[0]) == before_people + 1
        settled(page)
        once_after_check(sent, at, 'Redo')
        now = must_counts(page)
        assert now[0] == now[1] == now[2], now
    finally:
        context.close()


def test_another_persons_assign_reaches_the_watcher_from_its_pulse_with_no_rota_read(browser, live_server, h):
    """PERF remote-watcher (1f08c8059): someone else's Assign carries its edit delta on the workspace pulse; the watcher,
    holding the draft at that delta's base revision, draws it from memory (the same path as an edit's own reply), with
    no GET /api/workspaces/{id}. A save from another tab: sent through the live server, not this page's own command()."""
    from .test_browser_live_scopes import RECORD_PULSES, command
    from .test_browser import PAGE_LOAD_MS
    from .test_browser_team_box import open_view
    from .test_browser_fixlist_undo import settled
    w = h.shift(h.workspace('Remote watcher'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    sent = Sent(page)
    try:
        context.add_init_script(RECORD_PULSES)
        page.reload()  # the recorder is installed on load
        expect(page.locator('#main[data-loaded-view]')).to_have_count(1, timeout=PAGE_LOAD_MS)
        open_view(page, w, 'Week')
        settled(page)  # the draft held whole, its own check landed
        page.wait_for_function('() => window.livePulses.length > 0', timeout=15000)  # the stream's starting point
        s = h.detail(w)['shifts'][0]
        chips = page.locator(f'#schedule-view {DRAFT_CHIP}[data-rk*=\'"shift":"{s["id"]}"\']')
        expect(chips).to_have_count(0)
        at, pulses = sent.mark(), page.evaluate('(window.livePulses||[]).length')
        command(page, 'shift.update', {
            'workspace_id': w['id'], 'shift_id': s['id'], 'assignment_type_id': s['assignment_type_id'],
            'location_id': s['location_id'], 'service_ids': s['service_ids'], 'start': s['start'], 'end': s['end'],
            'attributes': s.get('attributes') or {}, 'override_reason': 'Remote watcher test',
            'participants': [{'person_id': h.seed['people'][0], 'start': s['start'], 'end': s['end']}]},
            target=None, revision=h.detail(w)['workspace']['revision'])
        page.wait_for_function("""([since, id]) => (window.livePulses||[]).slice(since)
            .some(p => (p.changes||[]).some(c => c.scope === 'workspace' && c.id === id && c.delta))""", arg=[pulses, w['id']], timeout=15000)
        expect(chips).to_have_count(1)  # drawn from the pulse's delta
        # From the remote save (the first command) to the watcher's own next command (its rota check, which without a
        # worker here ends in the governed full read): the remote edit itself read no rota.
        seen = sent.since(at)
        posts = [i for i, (m, p) in enumerate(seen) if (m, p) == ('POST', '/api/commands')]
        window = seen[:posts[1]] if len(posts) > 1 else seen
        rota = re.compile(r'^/api/workspaces/[^/?]+(\?|$)')
        assert not [p for m, p in window if m == 'GET' and rota.match(p)], seen
    finally:
        context.close()

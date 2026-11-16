"""RESP-REVIEW correctness review (7 Nov 2026): regression tests for the bugs it confirmed in code."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401

pytestmark = pytest.mark.browser


def test_a_declined_save_shows_no_empty_error_message(browser, live_server, h):
    """A draft edit the person declines (the waiting-proposal guard's Keep) or leaves fails with PAGE_LEFT and an empty
    message. Save paths that notify their own error (the Fix list's Assign, the session panel's Assign and Make team
    lead) showed an empty red message with Report a problem; reportError never did. A real error still shows."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        page.evaluate("""async () => {
          const dom = await window.importBundle('dom.js'), api = await window.importBundle('api.js');
          dom.notify('', 'error', undefined, new api.ApiError('PAGE_LEFT', '', 0));
          dom.notify('A real refusal.', 'error', undefined, new api.ApiError('EDIT_INVALID', 'A real refusal.', 422));
        }""")
        toasts = page.locator('#toast-tray .toast.error')
        expect(toasts).to_have_count(1)
        expect(toasts).to_contain_text('A real refusal.')
    finally:
        context.close()


def test_the_fix_list_page_rereads_when_the_complete_check_lands(browser, live_server, h):
    """A complete check that lands after an edit (the worker's) changes the rota's stored check (evaluated_at, scope) but
    not its revision. The Fix list is keyed on them: the page drew from the draft it held, so it kept the edit's
    "Checking…" list until the schedule was forgotten. The worker's completion announces itself (a `workspace` live
    change naming the job), and a page reopened after it reads the Fix list again instead of the reply the tab kept."""
    from urllib.parse import urlsplit
    from apps.worker.main import process_one
    w = h.shift(h.workspace('Check lands rota'), people=[])['workspace']
    w = h.command('coordinator', 'workspace.validate', {'reason': 'Routine rota check'}, w['id'], w['revision'])['workspace']
    # The complete check is queued before the page reads anything: its completion is the only change between the reads.
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'}, revision=w['revision'])['job']
    context, page = sign_in(browser, live_server, 'coordinator')
    route = f"schedule/issues?draft={w['id']}"
    fix_list = lambda r: r.method == 'GET' and urlsplit(r.url).path == f"/api/workspaces/{w['id']}/fix-list"
    try:
        navigate(page, route)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        navigate(page, 'home')
        # The complete check lands (the worker): the stored check is newer, the revision is not. Its live change reaches
        # this tab: Home, which shows rotas, reads again.
        # The worker runs in this test process: on SQLite its change is not pushed across processes (live.py signal), so
        # the stream's own recompute brings it (main.py /api/events: at most every 20 s). Wait past that, not 15 s.
        with page.expect_request(lambda r: r.method == 'GET' and urlsplit(r.url).path.startswith('/api/home'), timeout=30000):
            while process_one(h.app.state.engine):
                pass
        assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'
        with page.expect_request(fix_list):
            navigate(page, route)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
    finally:
        context.close()


def test_a_saved_view_from_before_list_columns_is_not_changed(browser, live_server, h):
    """S2: a Schedule saved view stored before List columns were saved carries none (or nulls). Its absent fields equal
    the List's defaults, so after it is applied it does not read as changed; a real column change still does."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        same = page.evaluate("""async () => {
          const t = await window.importBundle('table_view.js'), id = 'planner-list';
          const old = {planner: {mode: 'list'}}, nulls = {planner: {mode: 'list'}, hidden: null, order: null, wrap: null, widths: null};
          t.setTableColumns(id, {hidden: ['place'], order: ['duty', 'when'], wrap: ['people']});  // customised on this device
          t.setTableColumns(id, old);  // the old view applied
          const applied = [t.sameColumns(id, old), t.sameColumns(id, nulls), t.sameColumns(id, {...old, hidden: [], order: [], wrap: [], widths: {}})];
          t.setTableColumns(id, {hidden: ['status']});  // then a real change
          return [...applied, t.sameColumns(id, old)];
        }""")
        assert same == [True, True, True, False], same
    finally:
        context.close()


PHONE = {'width': 390, 'height': 844}


def test_a_phone_shows_the_loading_state_while_the_rota_is_read(browser, live_server, h):
    """PC-QA cycle 3 maintenance: on a phone the Schedule read its window first and drew it while the whole rota loaded (~5 s
    of an empty page on the VPS). A phone now reads the rota whole, as the window-first rule's own note says, with the
    shared loading state on screen meanwhile."""
    import re
    from .test_browser_autoschedule_live import rota
    from .test_browser_planner_cycle4 import hold
    w, _ = rota(h, [(4, 8, [h.seed['people'][0]])], name='Phone loading rota')
    context, page = sign_in(browser, live_server, 'coordinator', PHONE)
    parts = []
    try:
        page.on('request', lambda r: parts.append(r.url) if '/parts?part=' in r.url else None)
        held, release = hold(page, re.compile(re.escape(live_server) + r'/api/workspaces/' + w['id'] + r'$'))
        whole = re.compile(re.escape(live_server) + r'/api/workspaces/' + w['id'] + r'$')
        with page.expect_request(lambda r: bool(whole.match(r.url))):  # the whole rota, at once (no window first)
            page.evaluate('a => location.hash = a', f"schedule/week/2026-10-04?edit={w['id']}")
        expect(page.locator('#main .loading[aria-busy="true"]').first).to_be_attached()
        release()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule', timeout=30000)
        assert not parts, parts
    finally:
        context.close()


def test_continue_anyway_shows_the_loading_state_not_the_notice(browser, live_server, h):
    """PC-QA cycle 3 maintenance: Continue anyway is a wait the person started: the shared loading state replaces "Best on a
    desktop" at once (a background redraw kept the notice, or nothing, on screen until the rota was read)."""
    import re
    from .test_browser_autoschedule_live import rota
    from .test_browser_planner_cycle4 import hold
    from .test_browser_phone_scope import notice
    w, _ = rota(h, [(4, 8, [h.seed['people'][0]])], name='Continue loading rota')
    context, page = sign_in(browser, live_server, 'coordinator', PHONE, phone_notice=True)
    try:
        # Held from the start: a read begun ahead (the address names the rota) cannot finish before Continue.
        held, release = hold(page, re.compile(re.escape(live_server) + r'/api/workspaces/' + w['id'] + r'$'))
        page.evaluate('a => location.hash = a', f"schedule/week/2026-10-04?edit={w['id']}")
        notice(page, 'Schedule planning').get_by_role('button', name='Continue anyway', exact=True).click()
        expect(page.locator('#main .desktop-only')).to_have_count(0)
        expect(page.locator('#main .loading[aria-busy="true"]').first).to_be_attached()
        release()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule', timeout=30000)
    finally:
        context.close()


STICKY_GRID = """async () => {
  const m = await window.importBundle('planner_week.js');
  const days = Array.from({length: 7}, (_, i) => `2026-10-${String(4 + i).padStart(2, '0')}`);
  const services = [{id: 'v1', name: 'Physical and Virtual Urgent Care Clinic for Families'}, {id: 'v2', name: 'Sport Medicine Clinic'}];
  const shifts = Array.from({length: 14}, (_, i) => ({id: 's' + i, workspace_id: 'w', assignment_type_id: 't', location_id: 'l', service_ids: [services[i % 2].id],
    start: `${days[i % 7]}T08:00:00+03:00`, end: `${days[i % 7]}T12:00:00+03:00`, attributes: {}, participants: [{id: 'a' + i, person_id: 'p' + (i % 3)}],
    type_snapshot: {name: 'Clinic duty', definition: {fields: [], requirements: [{minimum: 1}]}}}));
  const ctx = {me: {organisation: {timezone: 'Asia/Riyadh', week_start: 6}, person_id: null},
    catalogues: {people: [0, 1, 2].map(i => ({id: 'p' + i, display_name: 'Person ' + i})), services, locations: [{id: 'l', name: 'Room'}], memberships: [], categories: [], assignment_types: []}};
  const data = {workspace: {id: 'w', revision: 1, status: 'draft', starts_on: days[0], ends_on: '2026-10-11', validation: {findings: [], coverage: []}}, shifts, wishes: []};
  const hooks = {name: p => (p && p.display_name) || '', code: () => 'X', details: () => {}, dnd: n => n, proposal: null, editable: true, fresh: true,
    selected: new Set(), onSelect: () => {}, refresh: async () => {}, tallies: true};
  const was = m.weekView.groupBy; m.weekView.groupBy = 'service';
  const root = m.weekGrid(ctx, data, shifts, {days, page: 0, has_previous: false, has_next: false, unit: 'week'}, () => {}, hooks);
  m.weekView.groupBy = was;
  const layer = document.createElement('dialog'); layer.id = 'sticky'; layer.setAttribute('aria-label', 'Sticky'); layer.append(root);
  document.body.append(layer); layer.showModal(); layer.style.setProperty('width', '1400px'); layer.style.setProperty('max-width', 'none');
}"""


def test_sticky_heads_stay_opaque_and_follow_a_narrower_grid(browser, live_server, h):
    """PC-QA cycle 3 maintenance: with the rota's rules panel open (rules in context dims the grid), sticky day, group and row
    heads went to 28% opacity, so the heads stacked at the same sticky top, and chips scrolled under the day header,
    showed through. They now stay opaque and only their contents fade. And the row heads' sticky offset follows the
    tallest group head when a narrower grid (a side panel opening) wraps a clinic name."""
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'people')
        page.evaluate(STICKY_GRID)
        day = page.locator('#sticky .wk-head>.wk-day[data-rk]').first
        expect(day).to_be_attached()
        page.evaluate("() => document.querySelector('#sticky').classList.add('rk-dim')")
        assert day.evaluate('n => getComputedStyle(n).opacity') == '1'
        # Row 96 dims with neutral colour tokens so sticky heads remain opaque and do not show scrolled content through them.
        muted = page.evaluate("""() => { const probe = document.createElement('span'); probe.style.color = 'var(--muted)'; document.body.append(probe); const value = getComputedStyle(probe).color; probe.remove(); return value; }""")
        assert day.evaluate('n => getComputedStyle(n.firstElementChild).opacity') == '1'
        assert day.evaluate('n => getComputedStyle(n.firstElementChild).color') == muted
        tallest = "previous => {const g = document.querySelector('#sticky .wk-grid'); const heads = [...g.querySelectorAll(':scope>.wk-group:not(.wk-subgroup)>.wk-group-head')];" \
                  " const height = g.style.getPropertyValue('--wk-group-h'); return heads.length && height !== previous && height === Math.max(...heads.map(n => n.offsetHeight)) + 'px';}"
        page.wait_for_function(tallest)
        before = page.evaluate("() => document.querySelector('#sticky .wk-grid').style.getPropertyValue('--wk-group-h')")
        assert before
        # Wide row heads first: the long clinic name on one line. (At the default 208 px it already fills its two-line
        # clamp, planner_week.ts headRow `clamp-2`, so only a head wide enough for one line can show the offset growing.)
        page.evaluate("() => document.querySelector('#sticky .wk-grid').style.setProperty('--sched-head', '600px')")
        page.wait_for_function(tallest, arg=before)
        wide = page.evaluate("() => document.querySelector('#sticky .wk-grid').style.getPropertyValue('--wk-group-h')")
        assert int(wide[:-2]) < int(before[:-2]), (before, wide)
        # The grid narrows (a side panel opens): the long clinic name wraps, and the offset follows.
        page.evaluate("() => {const d = document.querySelector('#sticky'); d.style.setProperty('width', '520px'); document.querySelector('#sticky .wk-grid').style.setProperty('--sched-head', '120px');}")
        page.wait_for_function(tallest, arg=wide)
        narrow = page.evaluate("() => document.querySelector('#sticky .wk-grid').style.getPropertyValue('--wk-group-h')")
        assert int(narrow[:-2]) > int(wide[:-2]), (wide, narrow)
    finally:
        context.close()

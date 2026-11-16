"""Client-side budgets: Schedule grid redraws at department scale (maintenance) and start-up reads in one request (maintenance)."""
import os
import pytest
from playwright.sync_api import expect  # noqa: F401

from .test_browser import browser, live_server, sign_in, navigate, expect_loaded  # noqa: F401

pytestmark = pytest.mark.browser

WEEK = """async (findingCount) => {
  const m = await window.importBundle('planner_week.js');
  const days = Array.from({length: 7}, (_, i) => `2026-10-${String(4 + i).padStart(2, '0')}`);
  const people = Array.from({length: 150}, (_, i) => ({id: 'p' + i, display_name: 'Person ' + i}));
  const shifts = Array.from({length: 500}, (_, i) => {const d = days[i % 7];
    return {id: 's' + i, workspace_id: 'w', assignment_type_id: 't' + (i % 10), location_id: 'l', service_ids: ['v'],
      start: `${d}T08:00:00+03:00`, end: `${d}T12:00:00+03:00`, attributes: {},
      participants: [{id: 'a' + i, person_id: 'p' + (i % 150)}, {id: 'b' + i, person_id: 'p' + ((i + 7) % 150)}],
      type_snapshot: {name: 'Duty ' + (i % 10), definition: {fields: [], requirements: [{minimum: 1}]}}};});
  const findings = Array.from({length: findingCount}, (_, i) => ({shift_id: 's' + (i % 500), person_id: 'p' + (i % 150),
    severity: i % 3 ? 'warning' : 'error', code: 'RULE', message: 'Rule ' + (i % 20)}));
  const ctx = {me: {organisation: {timezone: 'Asia/Riyadh', week_start: 6}, person_id: null},
    catalogues: {people, services: [{id: 'v', name: 'Clinic'}], locations: [{id: 'l', name: 'Room'}], memberships: [], categories: [], assignment_types: []}};
  const data = {workspace: {id: 'perf', revision: 1, status: 'draft', starts_on: '2026-10-04', ends_on: '2026-10-11',
    validation: {findings, coverage: []}}, shifts, wishes: []};
  const hooks = {name: p => (p && p.display_name) || '', code: () => 'X', details: () => {}, dnd: n => n, proposal: null,
    editable: true, fresh: true, selected: new Set(), onSelect: () => {}, refresh: async () => {}, tallies: true};
  const period = {days, page: 0, has_previous: false, has_next: false, unit: 'week'};
  m.weekGrid(ctx, data, shifts, period, () => {}, hooks);  // warm up
  const t = performance.now();
  const root = m.weekGrid(ctx, data, shifts, period, () => {}, hooks);
  return {ms: performance.now() - t, chips: root.querySelectorAll('.pchip').length};
}"""


def test_week_redraw_budget(browser, live_server, h):
    """maintenance: 500 sessions with 3,000 findings draw about as fast as with none: findings are indexed once per draw, not
    scanned again for every name (it was O(sessions × findings))."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        none = page.evaluate(WEEK, 0)
        many = page.evaluate(WEEK, 3000)
        assert many['chips'] >= 1000, many
        slack = 3 if os.environ.get('CI') else 2
        assert many['ms'] <= none['ms'] * slack + 150, (none, many)
    finally:
        context.close()


@pytest.mark.parametrize('address', ['people', 'home'])
def test_boot_requests_parallel(browser, live_server, h, address):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        expect_loaded(page, 'home')
        seen = []
        page.on('request', lambda r: seen.append(r.url) if '/api/catalogues' in r.url else None)
        # Only the reloaded document's reads: the page left may still send a late background read (Home's setup
        # counts) before the reload commits.
        page.on('framenavigated', lambda f: seen.clear() if f == page.main_frame else None)
        # The address changes without a hashchange, so only the reload's boot reads: Home holds no deferred lists since
        # 063cbb959 (S7), so a hashchange to People would start their read before the reload.
        page.evaluate("a => {history.replaceState(null, '', '#' + a); location.reload();}", address)
        expect_loaded(page, address)
        # People: one full catalogue read, no lean one before it. Home: the lean read first (the rest follows the page).
        assert seen and ('boot=true' in seen[0]) == (address == 'home'), seen
        if address == 'people':
            assert len(seen) == 1, seen
    finally:
        context.close()


MONTH = """async ([weeks, findingCount, agenda]) => {
  const m = await window.importBundle('planner_week.js');
  const start = Date.UTC(2026, 9, 4), days = Array.from({length: weeks * 7}, (_, i) => new Date(start + i * 864e5).toISOString().slice(0, 10));
  // A December-like department: 150 people, 10 duty types, about 17 sessions a day.
  const people = Array.from({length: 150}, (_, i) => ({id: 'p' + i, display_name: 'Person ' + i}));
  const shifts = Array.from({length: days.length * 17}, (_, i) => {const d = days[i % days.length];
    return {id: 's' + i, workspace_id: 'w', assignment_type_id: 't' + (i % 10), location_id: 'l', service_ids: ['v'],
      start: `${d}T08:00:00+03:00`, end: `${d}T12:00:00+03:00`, attributes: {},
      participants: [{id: 'a' + i, person_id: 'p' + (i % 150)}, {id: 'b' + i, person_id: 'p' + ((i + 7) % 150)}],
      type_snapshot: {name: 'Duty ' + (i % 10), definition: {fields: [], requirements: [{minimum: 1}]}}};});
  const findings = Array.from({length: findingCount}, (_, i) => ({shift_id: 's' + (i % shifts.length), person_id: 'p' + (i % 150),
    severity: i % 3 ? 'warning' : 'error', code: 'RULE', message: 'Rule ' + (i % 20)}));
  const ctx = {me: {organisation: {timezone: 'Asia/Riyadh', week_start: 6}, person_id: null},
    catalogues: {people, services: [{id: 'v', name: 'Clinic'}], locations: [{id: 'l', name: 'Room'}], memberships: [], categories: [], assignment_types: []}};
  const data = {workspace: {id: 'perf', revision: 1, status: 'draft', starts_on: days[0], ends_on: days.at(-1),
    validation: {findings, coverage: []}}, shifts, wishes: []};
  const hooks = {name: p => (p && p.display_name) || '', code: () => 'X', details: () => {}, dnd: n => n, proposal: null,
    editable: true, fresh: true, selected: new Set(), onSelect: () => {}, refresh: async () => {}, tallies: true};
  const period = {days, page: 0, has_previous: false, has_next: false, unit: 'month'};
  const draw = () => agenda ? m.dayAgenda(ctx, shifts, period, () => {}, hooks, {data}) : m.weekGrid(ctx, data, shifts, period, () => {}, hooks);
  draw();  // warm up
  const t = performance.now();
  const root = draw();
  return {ms: performance.now() - t, nodes: root.getElementsByTagName('*').length};
}"""


def test_month_draw_grows_linearly_with_its_days(browser, live_server, h):
    """RESP-REVIEW: a Month draws each day's header and cells from sessions bucketed by day once per draw. Before, every
    day header and every lane cell scanned all the sessions again (days × sessions, ~110,000 day lookups for a
    December month), so twice the days with the same sessions a day cost about four times as much; now about twice.
    The printed times are the measurement the absolute budget is to be set from (measure before limits)."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        five = page.evaluate(MONTH, [5, 1500, False])
        ten = page.evaluate(MONTH, [10, 3000, False])
        print('month draw', {'5 weeks': five, '10 weeks': ten})
        assert ten['nodes'] > five['nodes'] > 1000, (five, ten)
        slack = 3.5 if os.environ.get('CI') else 3
        assert ten['ms'] <= five['ms'] * slack + 60, (five, ten)
    finally:
        context.close()


def test_phone_agenda_draws_findings_once(browser, live_server, h):
    """RESP-REVIEW: the phone agenda indexes findings by session and by day once per draw. Before, every day scanned
    every finding (with a search of its sessions for each) and every item scanned every finding again."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        none = page.evaluate(MONTH, [5, 0, True])
        # Five times a department's findings, so the old per-day and per-item scans (millions of steps) stand out.
        many = page.evaluate(MONTH, [5, 15000, True])
        print('agenda draw', {'no findings': none, '15,000 findings': many})
        slack = 3 if os.environ.get('CI') else 2
        assert many['ms'] <= none['ms'] * slack + 20, (none, many)
    finally:
        context.close()


RESOLVE = """async (findingCount) => {
  const m = await window.importBundle('planner_week.js');
  const shifts = Array.from({length: 500}, (_, i) => ({id: 's' + i, start: `2026-10-${String(4 + i % 28).padStart(2, '0')}T08:00:00+03:00`,
    participants: [{id: 'a' + i, person_id: 'p' + (i % 150)}], type_snapshot: {name: 'Duty', definition: {}}}));
  const findings = Array.from({length: findingCount}, (_, i) => ({shift_id: 's' + (i % 500), person_id: 'p' + (i % 150),
    severity: i % 3 ? 'warning' : 'error', code: 'RULE', message: 'Rule ' + (i % 20)}));
  const data = {workspace: {id: 'w', validation: {findings, coverage: []}}, shifts};
  m.resolveQueue(data, null);  // warm up
  const t = performance.now();
  for (let k = 0; k < 5; k++) m.resolveQueue(data, null);
  return {ms: (performance.now() - t) / 5};
}"""


def test_resolve_queue_reads_each_sessions_findings_once(browser, live_server, h):
    """RESP-REVIEW: the planner counts what is left to resolve on every draw; findings are indexed by session once, not
    scanned again for every session (500 sessions × ~1,000 blocking findings at department scale)."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        none = page.evaluate(RESOLVE, 0)
        # Ten times a department's findings, so the old scan (500 × ~10,000) stands out from one pass over them.
        many = page.evaluate(RESOLVE, 30000)
        print('resolve queue', {'no findings': none, '30,000 findings': many})
        slack = 3 if os.environ.get('CI') else 2
        assert many['ms'] <= none['ms'] * slack + 10, (none, many)
    finally:
        context.close()


DAY = """async (findingCount) => {
  const m = await window.importBundle('schedule.js');
  const day = '2026-10-05', hours = Array.from({length: 20}, (_, i) => String(7 + (i % 12)).padStart(2, '0'));
  const people = Array.from({length: 150}, (_, i) => ({id: 'p' + i, display_name: 'Person ' + i}));
  // A busy day: 20 sessions, two people each; the rota's findings spread over 500 sessions, a few on these.
  const shifts = hours.map((h, i) => ({id: 's' + i, workspace_id: 'w', assignment_type_id: 't' + (i % 10), location_id: 'l', service_ids: ['v'],
    start: `${day}T${h}:00:00+03:00`, end: `${day}T${h}:45:00+03:00`, attributes: {},
    participants: [{id: 'a' + i, person_id: 'p' + i}, {id: 'b' + i, person_id: 'p' + (i + 20)}],
    type_snapshot: {name: 'Duty ' + (i % 10), definition: {fields: [], requirements: [{minimum: 1}]}}}));
  const findings = Array.from({length: findingCount}, (_, i) => ({shift_id: 's' + (i % 500), person_id: 'p' + (i % 150),
    severity: i % 3 ? 'warning' : 'error', code: 'RULE', message: 'Rule ' + (i % 20)}));
  const ctx = {me: {organisation: {timezone: 'Asia/Riyadh', week_start: 6}, person_id: null},
    catalogues: {people, services: [{id: 'v', name: 'Clinic'}], locations: [{id: 'l', name: 'Room'}], memberships: [], categories: [], assignment_types: []}};
  const data = {workspace: {id: 'w', revision: 1, status: 'draft', starts_on: day, ends_on: '2026-10-06', validation: {findings, coverage: []}}, shifts, wishes: []};
  const hooks = {name: p => (p && p.display_name) || '', code: () => 'X', details: () => {}, dnd: n => n, proposal: null,
    editable: true, fresh: true, selected: new Set(), onSelect: () => {}, refresh: async () => {}, tallies: true};
  const period = {days: [day], page: 0, has_previous: false, has_next: false, unit: 'day'};
  const draw = () => m.dayTimeline(ctx, data, shifts, period, () => {}, hooks, () => {}, document.createElement('div'));
  draw();  // warm up
  const t = performance.now();
  for (let k = 0; k < 5; k++) draw();
  return {ms: (performance.now() - t) / 5};
}"""


def test_day_view_reads_each_sessions_findings_once(browser, live_server, h):
    """RESP-REVIEW: the Day view indexes the check's findings by session once per draw; before, each of the day's
    sessions scanned every finding several times (its errors, each person's team check, its issue count)."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        none = page.evaluate(DAY, 0)
        many = page.evaluate(DAY, 30000)
        print('day draw', {'no findings': none, '30,000 findings': many})
        slack = 3 if os.environ.get('CI') else 2
        assert many['ms'] <= none['ms'] * slack + 10, (none, many)
    finally:
        context.close()


RULE_MARKS = """async ([extra, check]) => {
  const m = await window.importBundle('planner_rules.js');
  // A department rota: 28 days, 1,000 sessions (500 on screen), 150 people; 600 ordinary marks, plus `extra` marks on
  // sessions and a duty that are not on screen (20 sessions each), which a scan of every mark per node read for every node.
  const days = Array.from({length: 28}, (_, i) => new Date(Date.UTC(2026, 9, 4 + i)).toISOString().slice(0, 10));
  const shifts = Array.from({length: 1000}, (_, i) => ({id: 's' + i, start: `${days[i % 28]}T08:00:00+03:00`, end: `${days[i % 28]}T12:00:00+03:00`, participants: []}));
  const mark = (n, o) => ({kind: 'template_line', rule_id: 'r' + (n % 5), rule_name: 'Rule ' + (n % 5), text: 'Mark ' + n, by: null, at: null,
    people: [], services: [], locations: [], types: [], days: null, sessions: [], global: false, ...o});
  const markers = [];
  for (let n = 0; n < 600; n++) markers.push(mark(n, n % 3 === 0 ? {people: ['p' + (n % 150)], days: [n % 28, (n + 3) % 28]}
    : n % 3 === 1 ? {sessions: [n % 500, (n * 7) % 1000]} : {days: [n % 28]}));
  for (let n = 0; n < extra; n++) markers.push(mark(600 + n, {sessions: Array.from({length: 20}, (_, k) => 500 + ((n + k * 25) % 500)), types: ['off-screen'], days: [n % 28]}));
  const id = 'perf-marks-' + extra;
  const map = {days, shifts: shifts.map(s => s.id), checked: true, fresh: true, rules: [], markers};
  const real = window.fetch;
  window.fetch = (url, o) => String(url).includes('/rule-map') ? Promise.resolve(new Response(JSON.stringify(map), {status: 200, headers: {'Content-Type': 'application/json'}})) : real(url, o);
  const ctx = {me: {identity_id: 'me', organisation: {timezone: 'Asia/Riyadh'}}, catalogues: {people: []}};
  const data = {workspace: {id, revision: 1, status: 'draft', name: 'Perf', starts_on: days[0], ends_on: '2026-11-01', validation: {findings: []}},
    shifts, current_fingerprint: 'f', current_availability_revision: 1};
  const hooks = {editable: true, canPropose: false, canRequestException: false, policy: () => undefined, sentence: () => '',
    changeForEveryone: () => {}, rotaChanges: () => {}, refresh: async () => {}};
  const body = document.createElement('div'), main = document.createElement('div');
  body.append(main); document.body.append(body);
  const targets = [
    ...Array.from({length: 500}, (_, i) => ({kind: 'session', label: 'S' + i, shift: 's' + i})),
    ...Array.from({length: 150}, (_, i) => ({kind: 'person', label: 'P' + i, person: 'p' + i, days: i % 2 ? [days[i % 28]] : []})),
    ...Array.from({length: 300}, (_, i) => ({kind: 'assignment', label: 'A' + i, person: 'p' + (i % 150), shift: 's' + i})),
    ...days.map(d => ({kind: 'day', label: d, day: d}))];
  const nodes = targets.map(t => {const n = m.tag(document.createElement('div'), t); main.append(n); return n;});
  const frame = () => new Promise(r => requestAnimationFrame(() => r()));
  try {
    await m.loadRuleMap(data);
    m.mountRules(ctx, data, body, main, hooks);
    for (let k = 0; k < 600 && main.dataset.rkRules === undefined; k++) await frame();
    const runs = [];
    for (let k = 0; k < 3; k++) {
      performance.clearMeasures('rk-decorate');
      main.append(document.createElement('i'));  // a redraw of the view: the observer sweeps every tagged node again
      for (let f = 0; f < 600 && !performance.getEntriesByName('rk-decorate').length; f++) await frame();
      runs.push(performance.getEntriesByName('rk-decorate')[0].duration);
    }
    let differences = 0;
    if (check) {
      // The scan this replaced, as it was: every node's marks must be the same marks in the same order.
      const di = d => days.indexOf(d), shiftDay = id => days[Number(id.slice(1)) % 28], si = id => map.shifts.indexOf(id);
      const old = t => {const d = t.day ? di(t.day) : t.shift ? di(shiftDay(t.shift)) : -1, on = x => d < 0 || !x.days || x.days.includes(d);
        return markers.filter(x => t.kind === 'day' ? !!x.days && x.days.includes(d) && !x.people.length && !x.services.length && !x.locations.length && !x.types.length
          : t.kind === 'person' ? x.people.includes(t.person) && (!t.days.length || !x.days || t.days.some(y => x.days.includes(di(y))))
          : t.kind === 'assignment' ? (x.people.includes(t.person) || x.sessions.includes(si(t.shift))) && on(x)
          : x.sessions.includes(si(t.shift)));};
      const label = xs => {const words = xs.map(x => `Own number: ${x.text}`);return xs.length === 1 ? words[0] : `${xs.length} exceptions: ${words.join('; ')}`;};
      targets.forEach((t, i) => {const want = old(t), got = nodes[i].querySelector(':scope>.rk-mark');
        if ((want.length ? label(want) : null) !== (got ? got.getAttribute('aria-label') : null)) differences++;});
    }
    return {ms: Math.min(...runs), marked: main.querySelectorAll('.rk-mark').length, differences};
  } finally {window.fetch = real; body.remove();}
}"""


def test_rule_marks_sweep_reads_indexes_not_every_mark_per_node(browser, live_server, h):
    """RESP-REVIEW: the planner's rule-mark sweep (planner_rules.ts decorate → markersAt / hits) looks each tagged node up
    in indexes built once per rule map. Before, every node scanned every mark's people, sessions and days (and every
    rule's named people and lines): ~200,000 steps per sweep at department scale, growing with marks × nodes. Ten times
    the marks on sessions that are not on screen now costs about the same; the marks drawn are exactly the old scan's.
    The printed times are the measurement an absolute budget is to be set from (measure before limits)."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        few = page.evaluate(RULE_MARKS, [0, True])
        many = page.evaluate(RULE_MARKS, [6000, False])
        print('rule marks sweep', {'600 marks': few, '6,600 marks': many})
        assert few['differences'] == 0 and few['marked'] > 300, few
        assert many['marked'] == few['marked'], (few, many)  # the extra marks name no session on screen
        slack = 3 if os.environ.get('CI') else 2
        assert many['ms'] <= few['ms'] * slack + 15, (few, many)
    finally:
        context.close()


COLOUR_BY_RANK = """async (memberships) => {
  const m = await window.importBundle('planner_week.js'), dom = await window.importBundle('dom.js');
  const days = Array.from({length: 7}, (_, i) => `2026-10-${String(4 + i).padStart(2, '0')}`);
  // 150 people; each person's membership history (older ranks ended, the current one last).
  const per = Math.max(1, Math.round(memberships / 150));
  const rows = Array.from({length: 150 * per}, (_, i) => {const p = i % 150, k = Math.floor(i / 150), now = k === per - 1;
    return {id: 'm' + i, person_id: 'p' + p, category_id: 'c' + (now ? p % 6 : (p + k + 1) % 6), starts_on: now ? '2020-01-01' : '2010-01-01', ends_on: now ? null : '2019-12-31'};});
  const ctx = {me: {organisation: {timezone: 'Asia/Riyadh', week_start: 6}, person_id: null},
    catalogues: {people: Array.from({length: 150}, (_, i) => ({id: 'p' + i, display_name: 'Person ' + i})), services: [],
      memberships: rows, categories: Array.from({length: 6}, (_, i) => ({id: 'c' + i, name: 'Rank ' + i})), locations: [], assignment_types: []}};
  const s = {id: 's', assignment_type_id: 't', service_ids: ['v'], type_snapshot: {name: 'Duty'}};
  const was = m.weekView.colourBy; m.weekView.colourBy = 'role';
  try {
    const draw = () => {const rule = m.colourRule(ctx, days, dom.legend('Key', true));const out = [];for (let i = 0; i < 1000; i++) out.push(rule.seenChip(s, 'p' + (i % 150), document.createElement('span')));return out;};
    const first = draw();  // warm up
    const t = performance.now();
    for (let k = 0; k < 5; k++) draw();
    return {ms: (performance.now() - t) / 5, hues: first.slice(0, 6)};
  } finally { m.weekView.colourBy = was; }
}"""


def test_colour_by_rank_reads_each_persons_rank_once_per_draw(browser, live_server, h):
    """RESP-REVIEW S4: Colour by Rank finds each person's current rank from memberships indexed once per draw; before,
    every chip scanned every membership (1,000 chips × 200 memberships at department scale, more with history)."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        few = page.evaluate(COLOUR_BY_RANK, 150)
        many = page.evaluate(COLOUR_BY_RANK, 6000)  # 40 membership rows a person: the old scan grows 40 times
        print('colour by rank', {'150 memberships': few, '6,000 memberships': many})
        assert few['hues'] == many['hues'], (few, many)  # the same current rank either way
        slack = 3 if os.environ.get('CI') else 2
        assert many['ms'] <= few['ms'] * slack + 5, (few, many)
    finally:
        context.close()

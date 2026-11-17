"""FE-PERF budgets at department scale (163 people, 768 sessions in four weeks: test_backend_scale.scale_department).

- A page opened again shows what this tab last read for it at once (api.ts keptReads): it is drawn while every one of
  its reads to the server is still held unanswered, within RE_OPEN_MS.
- The Planner's first open draws a usable December-sized week within PLANNER_FIRST_MS (a fresh tab, nothing kept).

Budgets are the owner's targets, doubled on CI runners (shared CPU) and scaled by PW_CPU_THROTTLE. On the VPS's
department-scale data re-opens measured 4-66 ms; these
tests were not yet run locally reviewed, so the first CI run calibrates
them: tighten to its measured times plus margin, never loosen past the targets. No fixed waits.
"""
import os

import pytest
from playwright.sync_api import expect

from packages.contracts.schemas import WEB_KEPT_READS

from .test_backend_scale import scale_department
from .test_browser import PAGE_LOAD_MS, browser, holding, live_server, navigate, sign_in  # noqa: F401
from .test_browser import whole_list

pytestmark = pytest.mark.browser

SLOWER = (2 if os.getenv('CI') else 1) * float(os.getenv('PW_CPU_THROTTLE') or 1)
# Owner targets (FE-PERF): re-open under 300 ms to useful content; the December Planner under 2 s to a usable grid.
RE_OPEN_MS = 300 * SLOWER
PLANNER_FIRST_MS = 2000 * SLOWER

# Opens an address in-app (as the sidebar does) and answers when its page is loaded and painted (two frames).
# page.evaluate has no timeout of its own: a page that is never drawn must fail here, within `limit` ms, not hang the shard.
OPEN = r"""([hash, view, selector, limit]) => {
  const main = () => document.querySelector('#main'); main().removeAttribute('data-loaded-view');
  const t0 = performance.now(), ready = () => main()?.getAttribute('data-loaded-view') === view && (!selector || main()?.querySelector(selector));
  location.hash = hash;
  let observer, timer;
  const drawn = (async () => {
    await new Promise(done => {
      if (ready()) return done();
      observer = new MutationObserver(() => { if (ready()) { observer.disconnect(); done(); } });
      // Navigation replaces main, so watch its stable ancestor.
      observer.observe(document.body, {subtree: true, childList: true, attributes: true, attributeFilter: ['data-loaded-view']});
    });
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    return performance.now() - t0;
  })();
  const timeout = new Promise((_, fail) => {
    timer = setTimeout(() => {
      const loaded = main()?.getAttribute('data-loaded-view');
      const found = selector && main()?.querySelector(selector) ? 'found' : 'missing';
      fail(new Error(`${hash} not drawn within ${limit} ms: data-loaded-view=${loaded}, ${selector || 'no selector'} ${found}`));
    }, limit);
  });
  return Promise.race([drawn, timeout]).finally(() => {
    clearTimeout(timer);
    observer?.disconnect();
  });
}"""


def timed_open(page, hash, view, selector=None):
    """Milliseconds until the page is drawn; raises (never hangs) when it is not drawn within PAGE_LOAD_MS."""
    return page.evaluate(OPEN, [hash, view, selector, PAGE_LOAD_MS])


@pytest.fixture
def department(h):
    w, *_ = scale_department(h)
    return h.lifecycle(w, 'validate')['workspace']


def reopened_while_held(browser, live_server, department):
    """Each page opened again while every read is held: [(hash, ms)], after the behaviour itself is checked."""
    context, page = sign_in(browser, live_server, 'coordinator')
    planner = f"schedule/week/2026-10-11?edit={department['id']}"
    try:
        for address in ('people', planner, 'requests', 'home'):
            navigate(page, address)
        # Fresh replies can be used without rechecking. Age them without firing app timers.
        page.clock.set_system_time(page.evaluate('Date.now()') + WEB_KEPT_READS['fresh_ms'])
        with holding(page) as held:
            held.hold('**/api/**')  # nothing the server answers from here on reaches the page until released
            opens = []
            for hash, view, selector in (('#people', 'people', 'tbody tr'), ('#' + planner, 'schedule', '[data-session]'),
                                         ('#requests', 'requests', None), ('#home', 'home', None)):
                try:
                    opens.append((hash, timed_open(page, hash, view, selector)))
                except Exception as error:
                    # Diagnostic (REOPEN-HELD, failure only): the reads held when the page was not drawn, and the view
                    # the page says it has drawn; the failure itself is unchanged.
                    urls = [route.request.url for route in held['**/api/**']]
                    loaded = page.locator('#main').get_attribute('data-loaded-view')
                    raise AssertionError(f'{hash} not drawn while held; data-loaded-view={loaded}; held reads={urls}') from error
            assert held['**/api/**'], 'no read was asked again in the background'
        # Released: the background reads answer (unchanged) and the page stays as drawn, without an error.
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home')
        expect(page.locator('#main [role=alert]')).to_have_count(0)
        return opens
    finally:
        context.close()


def test_a_page_opened_again_is_drawn_from_what_was_read_while_the_server_is_asked_again(browser, live_server, h, department):
    reopened_while_held(browser, live_server, department)  # drawn at all while held: timed_open raises otherwise


@pytest.mark.wall_benchmark
def test_a_page_opened_again_is_drawn_within_its_budget(browser, live_server, h, department):
    for hash, ms in reopened_while_held(browser, live_server, department):
        assert ms <= RE_OPEN_MS, (hash, round(ms), RE_OPEN_MS)


def test_a_change_made_elsewhere_reaches_a_page_opened_again(browser, live_server, h, department):
    """Shown at once from what was read, then redrawn quietly once the background read brings the change. The live
    stream is cut, so only that background read can bring it."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route('**/api/events', lambda route: route.abort())
        page.reload()
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        # The list as drawn once its read is in (counted at once, it can still be empty): the server's whole list.
        rows = h.get('coordinator', '/api/requests/all?limit=1')['total']
        # Requests groups by Status (de4f35c4f): count request rows, not the group heads between them.
        expect(page.locator('#main tbody tr[data-row]')).to_have_count(rows)
        navigate(page, 'home')
        h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'leave',
            'start': '2026-10-06T00:00:00+03:00', 'end': '2026-10-06T23:59:00+03:00', 'private_reason': 'Made elsewhere (test)'})
        page.evaluate("() => { location.hash = '#requests'; }")
        expect(page.locator('#main tbody tr[data-row]')).to_have_count(rows + 1, timeout=PAGE_LOAD_MS)
        expect(page.locator('#main [role=alert]')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.wall_benchmark
def test_the_december_sized_planner_is_usable_within_its_budget_on_first_open(browser, live_server, h, department):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'home')
        ms = timed_open(page, f"#schedule/week/2026-10-11?edit={department['id']}", 'schedule', '#schedule-view:not([inert]) [data-session]')
        print('PLANNER_FIRST_MS', round(ms))
        assert ms <= PLANNER_FIRST_MS, (round(ms), PLANNER_FIRST_MS)
    finally:
        context.close()

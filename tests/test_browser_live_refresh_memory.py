"""A tab left open on the Schedule stays the same size (MEMLEAK, 6 Nov 2026): every change elsewhere moves the
department's revision, the live pulse refreshes the page and the published days' live coverage is read again. Each
answer is the whole publication's check (about 5 MB of JSON for a published year); the page used to keep every stale
one (up to 20 per publication), so an idle tab on a busy department grew by megabytes a minute."""
import re

import pytest
from playwright.sync_api import expect

from .heap_paths import detached_paths, snapshot
from .test_browser import browser, live_server, navigate, reconnect  # noqa: F401
from .test_browser_people_scale_timeline import people_at_department_scale  # noqa: F401

pytestmark = pytest.mark.browser

COVERAGE = re.compile(r"/api/publications/[^/]+/coverage$")


def measured(page, cdp):
    page.clock.run_for(16000)  # the skeleton timers end by themselves (loading.ts)
    for _ in range(2):
        cdp.send("HeapProfiler.collectGarbage")
    values = {m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]}
    return values["JSHeapUsedSize"], values["Nodes"]


# Diagnostic (HEAP): the nodes in the document (elements, text and the rest) and its elements by tag.first-class, so a
# failure says what grew and whether the growth is in the page or detached (CDP's Nodes counts detached nodes too).
DOM_COUNTS = r'''() => { const by = {}; let all = 0; const w = document.createTreeWalker(document, NodeFilter.SHOW_ALL);
  for (let n = w.currentNode; n; n = w.nextNode()) { all++; if (n.nodeType === 1) {
    const key = n.tagName.toLowerCase() + '.' + ((n.getAttribute('class') || '').trim().split(/\s+/)[0] || '');
    by[key] = (by[key] || 0) + 1; } }
  return {all, by}; }'''


def grown_by(before, after):
    keys = set(before['by']) | set(after['by'])
    diff = sorted(((after['by'].get(k, 0) - before['by'].get(k, 0), k) for k in keys), reverse=True)
    return {'in_document': after['all'] - before['all'], 'top': [(k, d) for d, k in diff[:15] if d]}


def test_live_refreshes_after_changes_elsewhere_keep_the_heap_bounded(people_at_department_scale, h):  # noqa: F811
    page, _, _ = people_at_department_scale
    service = h.configure("services", {"name": "Changed elsewhere clinic", "starts_on": "2020-01-01"})["record"]
    cdp = page.context.new_cdp_session(page)
    cdp.send("Performance.enable")
    main = page.locator("#main")
    asked = []
    page.on("request", lambda r: asked.append(r) if COVERAGE.search(r.url) else None)

    def coverage_read(step):
        """Runs `step`, then waits for the coverage read it starts to land (a published year's check takes seconds) and
        for the redraw that draws it: a measure taken while that page is still being built counts it as detached."""
        n = len(asked)
        with page.expect_request_finished(lambda r: r in asked[n:], timeout=180_000):
            step()
        expect(page.locator("#schedule-view[data-live-checked]")).to_be_attached(timeout=120_000)

    def pulse_after_change(n):
        def seen():
            nonlocal service  # someone else's change (the live stream may push it before the periodic check)
            main.evaluate("m => m.removeAttribute('data-loaded-view')")
            service = h.command("admin", "catalogue.update", {"catalogue": "services", "value": {
                **{k: service[k] for k in ("name", "starts_on", "ends_on", "active")}, "description": f"Changed elsewhere {n}"},
                "reason": "Live refresh memory"}, service["id"], service["revision"])["record"]
            page.clock.run_for(60_000)  # the periodic check (once a minute while the stream is alive) refreshes the page
            expect(main).to_have_attribute("data-loaded-view", "schedule", timeout=120_000)
        coverage_read(seen)

    try:
        coverage_read(lambda: navigate(page, "schedule/month/2026-11-10?edit=none"))
        pulse_after_change(0)
        heap, nodes = measured(page, cdp)
        dom = page.evaluate(DOM_COUNTS)
        for n in range(1, 7):
            pulse_after_change(n)
            # A refresh with nothing changed (coming back online) in between, as the live stream's reconnects do.
            main.evaluate("m => m.removeAttribute('data-loaded-view')")
            reconnect(page)
            expect(main).to_have_attribute("data-loaded-view", "schedule", timeout=120_000)
        after_heap, after_nodes = measured(page, cdp)
        grown = (after_heap - heap) / 2**20
        print("live refresh memory", {"heap_mb": round(grown, 2), "nodes": after_nodes - nodes})
        # Measured locally: before the fix +1.8 MB per change (about 11 MB over these six); after it, +0.5 MB over ten.
        assert grown < 6, (heap, after_heap)
        if after_nodes - nodes >= 500:
            grew = grown_by(dom, page.evaluate(DOM_COUNTS))
            detached = (after_nodes - nodes) - grew['in_document']
            pytest.fail(f'nodes {nodes} -> {after_nodes}; in the document {grew["in_document"]:+}, detached {detached:+}; '
                        f'grew by selector {grew["top"]}; held by {detached_paths(snapshot(cdp))}')
    finally:
        cdp.detach()

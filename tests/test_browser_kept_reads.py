"""maintenance/128: a Rules page rebuilt by a live change while its slow reads are on their way (the page follows the `workspace`
scope, so every draft change rebuilds it) joins them through api.ts keptRead: one rules-explained and one conversions
request per distinct URL. maintenance: Rules, Configuration and Insights load and resize without a console error (the one
ResizeObserver, loading.ts observeSize, runs its callbacks in the next frame), at 1440 and 390."""
import json
import time

import pytest

from .test_browser import browser, expect_loaded, live_server, navigate, route_fetch, sign_in, wait_held  # noqa: F401

FRAMES = "new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"
# Window error events too: Chrome reports the ResizeObserver loop as one, with no exception object.
ERRORS = "window.__errors = []; addEventListener('error', e => window.__errors.push(String(e.message)));"


def rebuild(page, marker, moved, n, seconds=30):
    """A live pulse naming a scope the page follows, until the page has read `marker` again (its rebuild started)."""
    before, deadline = len(marker), time.monotonic() + seconds
    moved[0] = n
    page.evaluate("window.dispatchEvent(new Event('online'))")
    while len(marker) <= before and time.monotonic() < deadline:
        page.evaluate(FRAMES)  # each call lets Playwright deliver its request events
    assert len(marker) > before, 'the live change never rebuilt the page'


def live_pulse(page, scope, moved):
    """Pulses that say someone else changed `scope` (activity moved by moved[0]) once moved[0] is set."""
    def pulse(route):
        reply = route_fetch(route)
        body = reply.json()
        if moved[0]:
            body.update(activity=body['activity'] + moved[0], changes=[{'scope': scope, 'id': None}])
        route.fulfill(response=reply, body=json.dumps(body))
    page.route('**/api/pulse', pulse)


def test_a_rules_page_rebuilt_during_its_load_asks_once_per_explainer_url(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    explained, conversions, catalogue, held, holding, moved = [], [], [], [], [True], [0]
    page.on('request', lambda r: (explained if '/rules-explained' in r.url else conversions if '/conversions' in r.url
                                  else catalogue if '/api/rules/catalogue?' in r.url else []).append(r.url))
    page.route('**/api/rules-explained?**', lambda route: held.append(route) if holding[0] else route.continue_())
    live_pulse(page, 'workspace', moved)  # someone else changed a draft: the Rules page follows that scope
    try:
        navigate(page, 'rules')
        wait_held(page, held)
        # A rebuild while rules-explained is on its way (refreshes run one at a time: one is all that can start now).
        rebuild(page, catalogue, moved, 1)
        holding[0] = False
        for route in held:
            route.continue_()
        expect_loaded(page, 'rules')
        rebuild(page, catalogue, moved, 2)  # answered: a rebuild by a draft change reads it from the kept answer
        expect_loaded(page, 'rules')
        assert explained and len(explained) == len(set(explained)), explained
        assert len(conversions) == len(set(conversions)), conversions
    finally:
        context.close()


def test_department_settings_reads_the_app_manifest_once_across_rebuilds(browser, live_server):
    """maintenance: Name and logo's installed-app preview reads the manifest through keptRead: one request for the load, none
    when a live change Configuration follows rebuilds the page (the manifest follows only settings)."""
    context, page = sign_in(browser, live_server, 'admin')
    manifest, branding, moved = [], [], [0]
    # The page's own reads only: a browser may fetch the linked manifest itself (resource type "manifest").
    page.on('request', lambda r: (manifest if '/manifest.webmanifest' in r.url and r.resource_type == 'fetch'
                                  else branding if r.url.endswith('/api/admin/branding') else []).append(r.url))
    live_pulse(page, 'catalogue', moved)
    try:
        navigate(page, 'configuration/department')
        expect_loaded(page, 'configuration/department')
        page.locator('#main .brand-installed .icon-previews').wait_for()
        assert len(manifest) == 1, manifest
        drawn = page.locator('#main .brand-installed .icon-previews').element_handle()
        rebuild(page, branding, moved, 1)
        page.wait_for_function('e => !e.isConnected', arg=drawn)  # the rebuilt page replaced the panel...
        page.locator('#main .brand-installed .icon-previews').wait_for()  # ...and drew its preview from the kept manifest
        assert len(manifest) == 1, manifest
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('role,key', [('coordinator', 'rules'), ('admin', 'configuration'), ('coordinator', 'insights')])
def test_pages_load_and_resize_without_console_errors(browser, live_server, role, key, width):
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 900 if width > 600 else 844})
    errors = []
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        page.evaluate(ERRORS)
        navigate(page, key)
        expect_loaded(page, key)
        page.evaluate(FRAMES)
        # A narrower and a wider window: every watched box reports again.
        page.set_viewport_size({'width': width - 80 if width > 600 else 360, 'height': 800})
        page.evaluate(FRAMES)
        page.set_viewport_size({'width': width, 'height': 900 if width > 600 else 844})
        page.evaluate(FRAMES)
        errors += page.evaluate('window.__errors')
        assert not errors, errors
    finally:
        context.close()

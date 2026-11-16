"""A failed module must offer recovery without changing normal load initialization."""
import json
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, bundle_file  # noqa: F401
from .test_browser_rule_catalogue import shot

pytestmark = pytest.mark.browser

# The same words as the app's own start-up failure screen (app.ts bootFailure, N-12).
BOOT_FAILURE = ("This is not an empty rota. No schedule could be retrieved. "
                "Use the approved departmental fallback and contact support.")


@pytest.mark.parametrize("width,scheme,saved_theme", [
    (1440, "light", "system"), (390, "light", "system"),
    (1440, "dark", "system"), (390, "dark", "system"),
    (1440, "dark", "light"), (390, "light", "dark"),
])
def test_failed_import_offers_keyboard_reload_and_recovers(browser, live_server, width, scheme, saved_theme):
    context = browser.new_context(service_workers="allow", viewport={"width": width, "height": 844}, color_scheme=scheme,
        storage_state={"cookies": [], "origins": [{"origin": live_server,
            "localStorage": [{"name": "rota.prefs", "value": json.dumps({"theme": saved_theme})}]}]})
    page = context.new_page()
    module = bundle_file("person_label.js")

    def unavailable(route):
        route.fulfill(status=502, content_type="text/plain", body="Bad Gateway")

    try:
        page.route(module, unavailable)
        with page.expect_response(module) as failed:
            page.goto(live_server)
        assert failed.value.status == 502
        expect(page.get_by_role("heading", name="Rota can’t be reached", exact=True)).to_be_visible()
        expect(page.get_by_role("alert")).to_have_text(BOOT_FAILURE)
        reload = page.get_by_role("button", name="Reload", exact=True)
        expect(reload).to_be_focused()
        expect(page.get_by_text("Connecting to the shared rota service…", exact=True)).to_have_count(0)
        theme = scheme if saved_theme == "system" else saved_theme
        if saved_theme != "system":
            expect(page.locator("html")).to_have_attribute("data-theme", saved_theme)
        expect(page.locator("html")).to_have_css("color-scheme", theme)
        shot(page, f"startup-recovery-{width}-{scheme}-{saved_theme}")
        page.unroute(module, unavailable)
        with page.expect_response(module) as recovered:
            reload.press("Enter")
        assert recovered.value.status == 200
        expect(page.get_by_label("Username or email", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Continue", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Reload", exact=True)).to_have_count(0)
        expect(page.get_by_role("alert")).to_have_count(0)
        expect(page.locator("html")).to_have_css("color-scheme", theme)
        # The unchanged static module still registers the worker from window.load.
        page.wait_for_function("() => navigator.serviceWorker.getRegistration().then(registration => !!registration?.active)")
    finally:
        context.close()


@pytest.mark.parametrize("fault", ["invalid-preferences", "blocked-theme", "malformed-theme"])
def test_theme_failure_keeps_system_styling_and_keyboard_recovery(browser, live_server, fault):
    preferences = "{invalid" if fault == "invalid-preferences" else json.dumps({"theme": "light"})
    context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark",
        storage_state={"cookies": [], "origins": [{"origin": live_server,
            "localStorage": [{"name": "rota.prefs", "value": preferences}]}]})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def unavailable(route):
        route.fulfill(status=502, content_type="text/plain", body="Bad Gateway")

    def broken_theme(route):
        if fault == "blocked-theme":
            unavailable(route)
        else:
            route.fulfill(status=200, content_type="text/javascript", body="export const = broken;")

    try:
        # Even the app entry point can fail: recovery must remain a classic script, independent of theme imports.
        page.route(bundle_file("app.js"), unavailable)
        if fault != "invalid-preferences":
            page.route(bundle_file("theme.js"), broken_theme)
        # The stored theme is applied by the start-up script itself, before the first paint (LIVE-15), so a broken
        # theme module no longer matters; unreadable preferences leave the system theme.
        page.goto(live_server)
        expect(page.get_by_role("heading", name="Rota can’t be reached", exact=True)).to_be_visible()
        expect(page.get_by_role("alert")).to_have_text(BOOT_FAILURE)
        reload = page.get_by_role("button", name="Reload", exact=True)
        expect(reload).to_be_focused()
        if fault == "invalid-preferences":
            expect(page.locator("html")).to_have_css("color-scheme", "dark")
            expect(page.locator("html")).not_to_have_attribute("data-theme", "light")
        else:
            expect(page.locator("html")).to_have_attribute("data-theme", "light")
        shot(page, f"startup-theme-fallback-{fault}")
        page.unroute(bundle_file("app.js"), unavailable)
        if fault != "invalid-preferences":
            page.unroute(bundle_file("theme.js"), broken_theme)
        reload.press("Enter")
        expect(page.get_by_label("Username or email", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Continue", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Reload", exact=True)).to_have_count(0)
        expect(page.get_by_role("alert")).to_have_count(0)
        expect(page.locator("html")).to_have_css("color-scheme", "dark" if fault == "invalid-preferences" else "light")
        assert not errors, [str(error) for error in errors]
    finally:
        context.close()


def test_running_bundle_owns_branding_and_unversioned_assets_fail_loudly(browser, live_server):
    context = browser.new_context()
    page = context.new_page()
    paths = []
    page.on('request', lambda r: paths.append(r.url.removeprefix(live_server)))
    try:
        page.goto(live_server)
        expect(page.get_by_role('heading', name='Sign in', exact=True)).to_be_visible()
        result = page.evaluate('''async () => {
            const {brand,BRANDING_DEFAULTS}=await window.importBundle('brand.js');
            const publicBrand=await (await fetch('/api/branding')).json();
            const base=new URL('.',document.querySelector('script[type="module"]').src);
            return {same:(await window.importBundle('api.js'))===(await import(new URL('api.js',base).href)),
                emblem:brand().emblem_url,expected:BRANDING_DEFAULTS.emblem_url,publicEmblem:publicBrand.emblem_url,
                wordmark:BRANDING_DEFAULTS.wordmark_url,
                icons:await (async()=>{const link=document.querySelector('link[rel=manifest]').href;
                  return (await (await fetch(link)).json()).icons.map(i=>new URL(i.src,link).pathname);})()};
        }''')
        assert result['same'] and result['emblem'] == result['expected'] == result['publicEmblem']
        expect(page.locator('img.brand-wordmark').first).to_have_attribute('src', result['wordmark'])
        assert not [p for p in paths if p.startswith('/assets/') and not re.match(r'/assets/[0-9a-f]{64}/', p)]
        assert {p for p in paths if p.endswith('/brand/emblem.png')} == {result['emblem']}
        for name in ('app.js', 'api.js', 'brand/emblem.png', 'print.css', 'sw.js'):
            assert page.request.get(live_server + '/assets/' + name).status == 404
        for path in result['icons']:
            assert page.request.get(live_server + path).ok
        assert page.request.get(live_server + '/sw.js').headers['cache-control'] == 'no-cache'
    finally:
        context.close()


def test_service_worker_starts_the_whole_versioned_shell_offline(browser, live_server):
    context = browser.new_context(service_workers='allow')
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server)
        expect(page.get_by_role('heading', name='Sign in', exact=True)).to_be_visible()
        page.evaluate('''async () => {
            await navigator.serviceWorker.ready;
            if(!navigator.serviceWorker.controller)await new Promise(ok=>navigator.serviceWorker.addEventListener('controllerchange',ok,{once:true}));
        }''')
        keys = page.evaluate('() => caches.keys()')
        assert len(keys) == 1 and keys[0].startswith('rota-shell-')
        context.set_offline(True)
        page.reload()
        expect(page.get_by_role('heading', name='Rota can’t be reached')).to_be_visible()
        assert not errors, errors
        kept = page.evaluate('''async () => (await (await caches.open((await caches.keys())[0])).keys()).map(r=>new URL(r.url).pathname)''')
        assert kept and all(p == '/' or re.match(r'/assets/[0-9a-f]{64}/', p) for p in kept)
    finally:
        context.close()

"""Boot reads the lean catalogue (`boot=true`); the deferred lists arrive through one shared loader before any page or
action that reads them (docs/design/backend-scale-performance.md › Item 10 handoff)."""
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from .test_browser import route_fetch, wait_held, TEST_PASSWORD, browser, live_server, new_context, expect_loaded  # noqa: F401

pytestmark = pytest.mark.browser


def held_app(browser, origin, role, route='', hold=True):
    """A signed-in page opened at `route` whose deferred-list read is held until `release()`; `reads` lists every
    catalogue read in order."""
    context = new_context(browser)
    assert context.request.post(origin + '/auth/login', data={'username': role, 'password': TEST_PASSWORD},
                                headers={'origin': origin}).ok
    reads, held = [], []

    def catalogues(r):
        query = parse_qs(urlsplit(r.request.url).query)
        reads.append(query)
        if hold and 'boot' not in query and 'counts_only' not in query:
            held.append(r)
        else:
            r.continue_()
    context.route(re.compile(r'/api/catalogues(\?|$)'), catalogues)
    page = context.new_page()
    page.goto(origin + ('/#' + route if route else ''))

    def release():
        nonlocal hold
        hold = False
        while held:
            held.pop().continue_()
    return context, page, reads, held, release


@pytest.mark.parametrize('route', ['home', 'inbox', 'feedback'])
def test_lean_pages_load_setup_counts_without_hydrating_lists(browser, live_server, h, route):
    context, page, reads, held, release = held_app(browser, live_server, 'admin', route)
    try:
        expect_loaded(page, route)
        # The setup count is computed from counts alone. The sidebar starts as the rail, where a badge is a dot and its
        # number sits in the tooltip (the number itself shows once the sidebar is pinned open). A screen reader hears
        # the same words as the button's description, the hidden number adding nothing to its name.
        config = page.locator('#department-nav [data-route="configuration"]')
        expect(config).to_have_class(re.compile(r'\bhas-badge\b'))
        expect(config.locator('.nav-count')).to_have_text(re.compile(r'^\d+/\d+$'))
        expect(config).to_have_attribute('data-tip', re.compile(r'setup steps done$'))
        expect(config).to_have_accessible_name('Configuration')
        expect(config).to_have_accessible_description(re.compile(r'^\d+ of \d+ setup steps done$'))
        # By reference to a visually hidden element holding the words (widely supported), not the draft aria-description.
        expect(config).to_have_attribute('aria-describedby', 'nav-badge-configuration')
        expect(config).not_to_have_attribute('aria-description', re.compile('.*'))
        expect(page.locator('#nav-badge-configuration.sr-only')).to_have_text(re.compile(r'^\d+ of \d+ setup steps done$'))
        assert reads[0] == {'boot': ['true']}, reads
        assert any('counts_only' in query for query in reads), reads
        assert all('boot' in query or 'counts_only' in query for query in reads), reads
        assert not held
        # Navigating to Rules hydrates first; counts never masquerade as loaded collections.
        page.locator('#department-nav [data-route="rules"]').click()
        wait_held(page, held)
        assert 'known' in reads[-1], reads
        expect(page.locator('#main')).not_to_have_attribute('data-loaded-view', 'rules')
        release()
        expect_loaded(page, 'rules')
        expect(page.locator('#main [role=alert]')).to_have_count(0)
    finally:
        release()
        context.unroute_all(behavior='wait')
        context.close()


def test_staff_read_no_catalogue_lists(browser, live_server, h):
    context, page, reads, _, release = held_app(browser, live_server, 'staff')
    try:
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home', timeout=20000)
        page.get_by_role('button', name=re.compile(r'^Account menu, ')).wait_for()
        assert reads == []
    finally:
        release()
        context.unroute_all(behavior='wait')
        context.close()


@pytest.mark.parametrize('route', ['people', 'rules', 'programmes', 'schedule', 'configuration/recurring_series',
                                   'configuration/opening-hours', 'configuration/resource_pools'])
def test_a_page_reading_deferred_lists_waits_for_them_behind_its_skeleton(browser, live_server, h, route):
    context, page, reads, held, release = held_app(browser, live_server, 'admin', route)
    try:
        main = page.locator('#main')
        expect(main.locator('.loading.page-sk')).to_be_visible(timeout=20000)
        wait_held(page, held)
        assert held, reads
        # Held: the page shows its skeleton, never a page drawn from lists that are not loaded.
        page.wait_for_timeout(300)
        expect(main).not_to_have_attribute('data-loaded-view', re.compile('.'))
        # The account menu opened while the first page loads stays open when that page draws (restore d50c82189).
        account = page.get_by_role('button', name=re.compile(r'^Account menu, '))
        account.click()
        expect(account).to_have_attribute('aria-expanded', 'true')
        release()
        expect_loaded(page, route)
        expect(account).to_have_attribute('aria-expanded', 'true')  # a closed popover sets it back to false
        expect(main.locator('[role=alert]')).to_have_count(0)
    finally:
        release()
        context.unroute_all(behavior='wait')
        context.close()


def test_new_rota_from_home_waits_for_the_deferred_lists(browser, live_server, h):
    context, page, reads, held, release = held_app(browser, live_server, 'coordinator')
    try:
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home', timeout=20000)
        page.get_by_role('button', name='New rota', exact=True).click()
        wait_held(page, held)
        assert held, reads
        page.wait_for_timeout(300)
        # The form reads recurring sessions: it opens only once they are loaded.
        expect(page.get_by_role('dialog')).to_have_count(0)
        release()
        expect(page.get_by_role('dialog', name='New rota')).to_be_visible()
    finally:
        release()
        context.unroute_all(behavior='wait')
        context.close()


def test_department_scale_boot_transfer_and_home_time(browser, live_server, h):
    """163 people: the boot catalogue is a fraction of the full one; Home draws without waiting for the rest.
    'before' replays the old boot (the full catalogue first) in the same run."""
    from .test_backend_scale import scale_department
    scale_department(h)
    h.get('admin', '/api/catalogues')  # both runs meet a warm server list cache
    out = {}
    for label in ('before', 'after'):
        context = new_context(browser)
        try:
            assert context.request.post(live_server + '/auth/login', data={'username': 'admin', 'password': TEST_PASSWORD},
                                        headers={'origin': live_server}).ok
            sizes, old = [], label == 'before'

            def catalogues(r):
                url = r.request.url.replace('?boot=true', '') if old else r.request.url
                reply = route_fetch(r, url=url)
                sizes.append((url, len(reply.body())))
                r.fulfill(response=reply)
            context.route(re.compile(r'/api/catalogues(\?|$)'), catalogues)
            page = context.new_page()
            began = time.perf_counter()
            page.goto(live_server)
            expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home', timeout=60000)
            out[label] = {'boot_catalogue_bytes': sizes[0][1], 'home_ms': round((time.perf_counter() - began) * 1000)}
        finally:
            context.unroute_all(behavior='wait')
            context.close()
    print(json.dumps(out))
    assert out['after']['boot_catalogue_bytes'] < 0.3 * out['before']['boot_catalogue_bytes'], out

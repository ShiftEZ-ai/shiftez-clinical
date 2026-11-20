"""Page unification (21 pages → 10): every old address still opens, and shows its new address."""
import re

import pytest
from playwright.sync_api import expect

from pathlib import Path

from .governance_support import developer
from .test_browser import browser, live_server, sign_in, expect_loaded, REDIRECTS, SCHEDULE_ALIASES  # noqa: F401

pytestmark = pytest.mark.browser


def test_test_redirects_mirror_the_route_alias_map():
    """One alias map (routes.ts); the tests' copy must not drift from it."""
    source = (Path(__file__).resolve().parents[1] / 'apps/web/src/routes.ts').read_text(encoding='utf-8')
    block = source.split('ROUTE_ALIASES:Record<string,Alias>=withoutPrototype({', 1)[1].split('\n});', 1)[0]
    # String entries are mirrored here; the Schedule's old pages are function entries in the same map.
    assert dict(re.findall(r"^\s*(\w+):'([^']+)'", block.replace(',', ',\n'), re.M)) == REDIRECTS
    assert set(re.findall(r"^\s*(\w+):\(", block, re.M)) == SCHEDULE_ALIASES


def test_old_schedule_addresses_open_the_schedule(browser, live_server, h):
    """#planner/<rota>/<layout.range>, #timeline, #calendar and #published open the matching Schedule view."""
    w = h.shift(h.workspace('Old planner link'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        # The old draft link opens that draft in the People view, on the draft's own first day.
        for old, want in ((f'planner/{w["id"]}/person.rota', rf'#schedule/people/{w["starts_on"]}\?edit={w["id"]}$'),
                          ('planner', r'#schedule/week/[\d-]+'), ('timeline', r'#schedule/year/[\d-]+(\?|$)'),
                          ('calendar', r'#schedule/people/[\d-]+'), ('published', r'#schedule/month/[\d-]+')):
            page.evaluate('key => { location.hash = key; }', old)
            expect_loaded(page, 'schedule')
            expect(page).to_have_url(re.compile(want))
    finally:
        context.close()


def test_every_old_address_redirects_for_admin_and_developer(browser, live_server, h):
    context, page = sign_in(browser, live_server, developer(h))
    try:
        # Access & roles kept its address; with no tab it opens its first tab.
        for old, new in {**REDIRECTS, 'access': 'access/people'}.items():
            page.evaluate('key => { location.hash = key; }', old)
            expect_loaded(page, new)
            # The address bar shows where the old link now lives (bookmarks can be updated).
            expect(page).to_have_url(re.compile(re.escape('#' + new) + '$'))
            expect(page.get_by_role('heading', name='Restricted page', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_old_address_on_first_load_and_unknown_page(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.goto(live_server + '/#availability')
        expect_loaded(page, 'requests')
        expect(page).to_have_url(re.compile(r'#requests$'))
        page.goto(live_server + '/#no-such-page')
        expect_loaded(page, 'home')
    finally:
        context.close()


def test_tab_is_kept_in_the_address(browser, live_server, h):
    context, page = sign_in(browser, live_server, developer(h))
    try:
        page.evaluate("location.hash='system'")
        expect_loaded(page, 'system/imports')
        tabs = page.get_by_role('tablist', name='System', exact=True)
        tabs.get_by_role('tab', name='Audit log', exact=True).click()
        expect_loaded(page, 'system/audit')
        expect(page).to_have_url(re.compile(r'#system/audit$'))
        page.reload()
        expect_loaded(page, 'system/audit')
        expect(tabs.get_by_role('tab', name='Audit log', exact=True)).to_have_attribute('aria-selected', 'true')
        # A tab the role cannot see opens the first tab it can (auditor has no Imports).
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'auditor')
    try:
        page.evaluate("location.hash='system/imports'")
        expect_loaded(page, 'system/bulk')  # its first tab: the Bulk editor (read-only for auditors)
    finally:
        context.close()

"""The stored theme applies before the first paint (LIVE-15, maintenance), and the browser's own chrome follows it (maintenance)."""
import json

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, new_context  # noqa: F401

pytestmark = pytest.mark.browser

# Records the theme at the moment <body> is first inserted: anything later would have painted the other theme first.
AT_BODY = """new MutationObserver((_, o) => {if (document.body) {window.themeAtBody = document.documentElement.getAttribute('data-theme'); o.disconnect();}})
  .observe(document, {childList: true, subtree: true});"""


@pytest.mark.parametrize('stored,system', [('dark', 'light'), ('light', 'dark')])
def test_stored_theme_applies_before_first_paint(browser, live_server, stored, system):
    context = new_context(browser, color_scheme=system, storage_state={'cookies': [], 'origins': [{'origin': live_server,
        'localStorage': [{'name': 'rota.prefs', 'value': json.dumps({'theme': stored})}]}]})
    try:
        context.add_init_script(AT_BODY)
        page = context.new_page()
        page.goto(live_server)
        expect(page.get_by_role('heading', name='Sign in', level=1)).to_be_visible()
        assert page.evaluate('window.themeAtBody') == stored
        expect(page.locator('html')).to_have_css('color-scheme', stored)
        # The browser and installed-app chrome take the chosen theme's page background, not a fixed teal.
        bg = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()")
        assert set(page.locator('meta[name="theme-color"]').evaluate_all('ms => ms.map(m => m.content)')) == {bg}
    finally:
        context.close()


def test_system_theme_keeps_a_colour_per_scheme(browser, live_server):
    context = new_context(browser)
    try:
        page = context.new_page()
        page.goto(live_server)
        expect(page.get_by_role('heading', name='Sign in', level=1)).to_be_visible()
        metas = page.locator('meta[name="theme-color"]').evaluate_all('ms => ms.map(m => [m.media, m.content])')
        assert {media for media, _ in metas} == {'(prefers-color-scheme: light)', '(prefers-color-scheme: dark)'}
        assert '#0F7B6C' not in {content for _, content in metas}
    finally:
        context.close()

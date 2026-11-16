"""rc.16 UI/UX review, batch 4b: one primary colour and "New rota" (S-08), View settings without the toolbar's own
Group/Sort/Filter (S-10), no hover cards on touch (S-11), red only for problems (P-04), the side panel as the one
person editor (P-02), lighter inner tabs (SY-03), the Bulk editor's first type (SY-02), the weights total (C-02), the
setup guide's order (C-04), System's own icon (G-04) and the selection bar clearing the table (P-06)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_config, toolbar, view_settings  # noqa: F401

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 1000}


def test_one_primary_colour_and_new_rota(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'schedule')
        # SX-14: an empty schedule offers New rota once, in its empty state; the toolbar's copy is hidden then.
        new = page.locator('#main').get_by_role('button', name='New rota', exact=True)
        expect(new).to_have_count(1)
        expect(new).to_have_class(re.compile(r'\bprimary\b'))
        expect(page.locator('.button.dark')).to_have_count(0)
        new.click()
        expect(page.get_by_role('dialog', name='New rota', exact=True)).to_be_visible()
    finally:
        context.close()


def test_view_settings_leave_group_sort_and_filter_to_the_toolbar(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'schedule')
        panel = view_settings(page.locator('.planner-toolbar'))
        names = panel.locator('.vs-row-name').all_inner_texts()
        assert 'Properties' in names and 'Days' in names, names
        assert not {'Group by', 'Sort', 'Filter'} & set(names), names
    finally:
        context.close()


def test_touch_shows_no_hover_card(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        target = page.locator('#main [data-tip]').first
        expect(target).to_be_visible()
        # The page's clock is driven by the test: well past the hover delay, without a fixed wait.
        page.clock.install()
        target.dispatch_event('pointerover', {'pointerType': 'touch', 'bubbles': True})
        page.clock.run_for(2000)
        expect(page.get_by_role('tooltip')).to_have_count(0)
        target.dispatch_event('pointerover', {'pointerType': 'mouse', 'bubbles': True})
        page.clock.run_for(2000)
        expect(page.get_by_role('tooltip')).to_have_count(1)
    finally:
        context.close()


def test_people_open_the_side_panel_ranks_are_neutral_and_automatic_hues_skip_coral(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        row = page.locator('#main tbody tr[data-row]').first
        expect(row.get_by_role('button', name=re.compile('^Edit '))).to_have_count(0)
        # Ranks wear their own tints (dcee3cf05, RANK_PALETTE .duty-rankN), never a clinic, group or person hue.
        expect(page.locator('#main td[data-col=rank] .role-chip').first).to_be_visible()
        expect(page.locator('#main td[data-col=rank] .role-chip:not([class*="duty-rank"]):not(.duty-slate)')).to_have_count(0)  # slate: no rank
        assert page.evaluate("""async () => {const {colourOf} = await window.importBundle('dom.js');
            return Array.from({length: 400}, (_, i) => colourOf(undefined, 'seed-' + i)).every(c => c !== 'coral' && c !== 'slate');}""")
        row.get_by_role('button', name=re.compile('^Open ')).click()
        expect(page.locator('.dock-panel')).to_be_visible()
        expect(page.get_by_role('dialog', name=re.compile('^Edit person'))).to_have_count(0)
    finally:
        context.close()


def test_forms_have_no_layout_toggle(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        page.get_by_role('button', name='New person', exact=True).click()
        dialog = page.get_by_role('dialog', name='New person')
        expect(dialog.get_by_role('radiogroup', name='Form layout')).to_have_count(0)
    finally:
        context.close()


def test_imports_inner_tabs_and_bulk_editor_first_type(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'system/imports')
        expect(page.locator('#main .tabs-inner [role=tab]', has_text='Staff')).to_be_visible()
        navigate(page, 'system/bulk')
        pick = page.locator('#main').get_by_role('button', name=re.compile('^Record type: '))
        expect(pick).to_have_attribute('aria-label', 'Record type: People')
        expect(page.locator('#main').get_by_role('button', name='Paste from Excel', exact=True)).to_have_class(re.compile(r'\bsecondary\b'))
    finally:
        context.close()


def test_weights_show_their_total_and_setup_runs_in_order(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        open_config(page, 'Auto-schedule & scoring')
        expect(page.get_by_label('Coverage weight', exact=True)).to_have_accessible_description(re.compile(r'^\d+%$'))  # its share
        navigate(page, 'configuration/setup')
        expect(page.locator('.setup-step').first).to_be_visible()
        titles = page.locator('.setup-step .setup-title strong').all_inner_texts()
        assert titles.index('Locations') < titles.index('Opening hours') < titles.index('Duty types'), titles
        assert titles[-2:] == ['Recurring sessions', 'Announcements'], titles  # optional steps last
        # System has its own icon, not Configuration's.
        icons = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('nav button, nav a')]
            .filter(b => /^(Configuration|System)/.test(b.getAttribute('aria-label') || b.textContent.trim()))
            .map(b => [(b.getAttribute('aria-label') || b.textContent.trim()).match(/^(Configuration|System)/)[1], b.querySelector('svg')?.innerHTML || '']))""")
        assert icons.get('Configuration') and icons.get('System') and icons['Configuration'] != icons['System'], icons
    finally:
        context.close()

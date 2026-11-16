"""F4 (rc17): the legacy Rules page, the rota's rules sheet and the rules-in-context panel use the one shared collection
toolbar (table_view.ts collectionToolbar): status views live in Saved views, search sits in the toolbar, active filters
live inside Filter, and topics are labelled sections with counts (no closed disclosures)."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, open_rota, planner_tool, toolbar, close_popover  # noqa: F401
from .rules_support import rule_status

pytestmark = pytest.mark.browser


def test_rules_status_views_and_a_named_view_survive_a_reload(browser, live_server, h):
    h.policy({'kind': 'min_rest', 'hours': 11})
    h.policy({'kind': 'max_hours', 'hours': 48, 'window': 'week'}, severity='advisory')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        bar = page.locator('.rule-toolbar')
        # One toolbar, in the shared order; no status tab strip and no chip row beside it.
        expect(page.get_by_role('tablist', name='Rule status')).to_have_count(0)
        order = bar.evaluate("b => [...b.querySelectorAll('.dt-saved,.dt-search,.dt-tool:not(.dt-saved)')].map(n => n.matches('.dt-saved') ? 'saved' : n.matches('.dt-search') ? 'search' : n.getAttribute('aria-label').split(/[:( ]/)[0])")
        assert order == ['saved', 'search', 'Filter', 'Group', 'Sort'], order  #
        expect(bar.locator('button.dt-saved')).to_have_attribute('aria-label', 'Saved views: Current')  # the count is the footer's (ecb451af)
        rule_status(page, 'History')
        expect(page.get_by_role('region', name='History', exact=True)).to_be_visible()
        rule_status(page, 'Current')
        # A filter, saved as a named view with the status it was made on, comes back after a reload.
        toolbar(bar, 'Filter')
        page.get_by_role('button', name=re.compile('^Severity')).click()
        page.locator('.picker').get_by_label('Advisory', exact=True).check()
        close_popover(page)
        expect(page.locator('#rule-status-panel .rule-item')).to_have_count(1)
        expect(page.locator('.dt-chips')).to_have_count(0)
        toolbar(bar, 'Saved views').get_by_role('menuitem', name='New view…').click()
        dialog = page.get_by_role('dialog', name='New view')
        dialog.get_by_label('View name').fill('Advisory rules')
        dialog.get_by_role('button', name='Save view').click()
        expect(bar.locator('button.dt-saved')).to_have_attribute('aria-label', re.compile('^Saved views: Advisory rules'))
        saved = h.get('coordinator', '/api/table-views?table=rules')['views']
        assert saved[0]['definition']['filters'] == {'severity': ['advisory']}
        page.reload()
        navigate(page, 'rules')
        toolbar(bar, 'Saved views').get_by_role('menuitemradio', name=re.compile('^Advisory rules')).click()
        close_popover(page)
        expect(page.locator('#rule-status-panel .rule-item')).to_have_count(1)
        expect(bar.get_by_role('button', name=re.compile(r'^Filter: Advisory'))).to_be_visible()
    finally:
        context.close()


def test_rules_in_context_search_is_part_of_its_toolbar(browser, live_server, h):
    """The rota's one rule search (the dock's own searchable rule list is retired for this panel's)."""
    for hours in range(4, 13):
        h.policy({'kind': 'min_rest', 'hours': hours})
    w = h.shift(h.workspace('Context rota'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota', exact=True)
        tools = panel.get_by_role('group', name='Rules')
        expect(tools.get_by_role('searchbox', name='Find a rule')).to_be_visible()
        expect(panel.locator('input.wk-search')).to_have_count(0)  # no second, separate search box
        tools.get_by_role('searchbox', name='Find a rule').fill('no rule is called this')
        expect(panel.get_by_text('No rule matches that search.')).to_be_visible()
    finally:
        context.close()


def test_draft_switcher_is_the_shared_picker_with_search_and_keyboard(browser, live_server, h):
    """F5: the draft switcher opens the shared picker (picker.ts): searchable, keyboard-driven, its actions under the list."""
    h.shift(h.workspace('Alpha draft'))
    h.shift(h.workspace('Beta draft', '2026-10-11', '2026-10-18'), '2026-10-11T08:00:00+03:00', '2026-10-11T12:00:00+03:00')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        page.locator('.rota-switcher').click()
        picker = page.get_by_role('dialog', name='Drafts', exact=True)
        expect(picker.get_by_role('button', name='New rota', exact=True)).to_be_visible()
        search = picker.get_by_role('combobox', name='Search Drafts')
        expect(search).to_be_focused()
        search.fill('beta')
        expect(picker.get_by_role('option')).to_have_count(1)
        search.press('Enter')
        expect(page.locator('.rota-switcher h1')).to_have_text('Beta draft')
        expect(picker).to_have_count(0)
    finally:
        context.close()

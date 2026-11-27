"""The rota's Workload targets dock shows the targets of the latest rota check, whichever lands first: the check or the
dock's opening; a rule change being typed in the rules panel survives the automatic check landing; and a check that
lands under an open menu is drawn once. Real server answers, only held back to force the order that failed under load.
(The dock reads nothing itself since its rule list was retired for the rules panel's, so the late-read races it once
had cannot occur.)"""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import (browser, live_server, sign_in, navigate, open_rota, planner_tool, wait_held,  # noqa: F401
                           view_settings, close_popover)
from .rules_support import rule_action, rule_row

pytestmark = pytest.mark.browser
TARGETS = 'Workload targets for this rota'
# The page names itself first thing, synchronously, whenever it draws (renderPage, documentTitle): a title set back from
# this mark means a redraw began.
MARK = 'No redraw since'


def test_targets_dock_opened_while_the_check_runs_shows_the_checked_targets(browser, live_server, h):
    w = h.shift(h.workspace('Sheet race'))['workspace']
    # A prorated target activated after the rota was made: its stored check is out of date, so the page checks it.
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties',
              'comparison': 'target', 'amount': 2, 'prorate_department_days': True, 'reference_days': 28}, severity='advisory')
    # A draft keeps the inputs it was made with (425d832da): taking the department's newer rules is the reviewed
    # baseline refresh, which drops the stored check. Non-safety rule (safety_rule() False), as 8d6258e80 does.
    h.refresh_draft_baseline(h.detail(w)['workspace'])
    context, page = sign_in(browser, live_server, 'coordinator')
    checks = []

    def hold_check(route):
        if 'workspace.validate' in (route.request.post_data or ''):
            checks.append(route)
        else:
            route.continue_()
    try:
        page.route(re.compile(r'.*/api/commands$'), hold_check)
        with page.expect_request(lambda r: r.url.endswith('/api/commands') and 'workspace.validate' in (r.post_data or ''), timeout=20000):
            navigate(page, f"planner/{w['id']}")  # the automatic check is on its way, held
        page.evaluate('1')  # let the route handler take it
        assert len(checks) == 1
        planner_tool(page, TARGETS)
        sheet = page.locator('.dock-panel[data-rota-targets]')
        expect(sheet.get_by_role('heading', name=TARGETS)).to_be_visible()
        expect(sheet.get_by_text('Workload targets update when the rota check finishes.', exact=True)).to_be_visible()
        checks.pop().continue_()
        # The check lands: the open dock redraws in place with the checked targets.
        footer = page.locator('.app-footer')
        expect(footer.get_by_text(re.compile(r'^Checking the rota'))).to_have_count(0, timeout=20000)
        expect(sheet.get_by_role('button', name='Adjust workload target', exact=True)).to_be_visible(timeout=20000)
        expect(sheet.get_by_text('Workload targets update when the rota check finishes.', exact=True)).to_have_count(0)
    finally:
        for route in checks:
            route.continue_()
        context.close()


@pytest.mark.parametrize('typed', [False, True])
def test_automatic_check_keeps_a_rule_change_being_typed_in_the_rules_panel(browser, live_server, h, typed):
    rule = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    w = h.shift(h.workspace('Open rule controls during check'))['workspace']
    # An advisory target activated after the draft, taken by the reviewed baseline refresh (425d832da, as 8d6258e80):
    # its stored check is then out of date, so opening the rota checks it. Non-safety rule (safety_rule() False).
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties',
              'comparison': 'target', 'amount': 2, 'prorate_department_days': True, 'reference_days': 28}, severity='advisory')
    h.refresh_draft_baseline(h.detail(w)['workspace'])
    context, page = sign_in(browser, live_server, 'coordinator')
    checks = []
    try:
        page.route(re.compile(r'.*/api/commands$'), lambda route: checks.append(route)
                   if 'workspace.validate' in (route.request.post_data or '') else route.continue_())
        open_rota(page, w['id'])
        wait_held(page, checks)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        row = rule_row(panel, rule['name'])
        rule_action(row, 'Adjust for this rota')
        number = row.get_by_label(f"New number for “{rule['name']}” in this rota")
        if typed:
            number.fill('4')
            row.get_by_label('Justification', exact=True).fill('Agreed with the department for this rota')
        # A focused text field deliberately postpones the redraw. Move to a button without closing the controls.
        row.get_by_role('button', name='Use for this rota').focus()
        checks.pop().continue_()
        expect(page.locator('.app-footer').get_by_text(re.compile(r'^Checking the rota'))).to_have_count(0, timeout=20000)
        number = rule_row(panel, rule['name']).get_by_label(f"New number for “{rule['name']}” in this rota")
        expect(number).to_be_visible()  # the open rule and its change controls stay open
        expect(number).to_have_value('4' if typed else '5')
        panel.get_by_role('button', name='Close rules panel (Esc)', exact=True).click()
        if typed:
            expect(panel.get_by_role('alert')).to_contain_text('Not saved.')
        else:
            expect(panel).to_have_count(0)
        assert not h.detail(w)['workspace']['rule_adjustments']
    finally:
        for route in checks:
            if not route.request.failure:
                route.continue_()
        context.close()


def test_a_check_that_lands_under_an_open_menu_is_drawn_once(browser, live_server, h):
    """The automatic check lands while View settings is open, so its result waits; the person's own choice there redraws
    the page, which reads the checked rota. Closing the menu then draws nothing more: the wait used to redraw the whole
    page again with nothing new, replacing the grid under the pointer (the hover card and its chip were lost)."""
    w = h.shift(h.workspace('Check under an open menu'))['workspace']
    # Out of date as in the tests above, so opening the rota checks it (synchronously: no worker answers here).
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties',
              'comparison': 'target', 'amount': 2, 'prorate_department_days': True, 'reference_days': 28}, severity='advisory')
    h.refresh_draft_baseline(h.detail(w)['workspace'])
    context, page = sign_in(browser, live_server, 'coordinator')
    checks = []
    try:
        page.clock.install()  # time runs as usual; run_for below fires the page's pending waits instead of waiting for them
        page.route(re.compile(r'.*/api/commands$'), lambda route: checks.append(route)
                   if 'workspace.validate' in (route.request.post_data or '') else route.continue_())
        navigate(page, 'schedule/week/2026-10-04?edit=' + w['id'])
        wait_held(page, checks)
        checking = page.locator('.app-footer').get_by_text(re.compile(r'^Checking the rota'))
        expect(checking).to_be_visible()
        panel = view_settings(page.locator('.planner-toolbar'))
        with page.expect_response(lambda r: r.url.endswith('/api/commands')
                                  and 'workspace.validate' in (r.request.post_data or '')) as checked:
            checks.pop().continue_()
        assert checked.value.ok
        checked.value.finished()
        page.evaluate('mark => { document.title = mark; }', MARK)
        density = panel.get_by_role('radiogroup', name='Density')
        other = density.get_by_role('radio', checked=False).first.inner_text().strip()
        choice = density.get_by_role('radio', name=other, exact=True)
        choice.click()
        expect(choice).to_have_attribute('aria-checked', 'true')
        page.wait_for_function('mark => document.title !== mark', arg=MARK)  # the person's own redraw began
        expect(checking).to_have_count(0)  # it drew the checked rota, the menu still open
        expect(panel).to_be_visible()
        page.evaluate('mark => { document.title = mark; }', MARK)
        close_popover(page)
        page.clock.run_for(5_000)  # well past the page's wait for someone mid-task (1.5 s)
        assert page.evaluate('() => document.title') == MARK, 'the landed check redrew the page a second time'
    finally:
        for route in checks:
            route.continue_()
        page.unroute_all(behavior='ignoreErrors')
        context.close()

"""The rota's Workload targets dock shows the targets of the latest rota check, whichever lands first: the check or the
dock's opening; and a rule change being typed in the rules panel survives the automatic check landing. Real server
answers, only held back to force the order that failed under load. (The dock reads nothing itself since its rule list
was retired for the rules panel's, so the late-read races it once had cannot occur.)"""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_rota, planner_tool, wait_held  # noqa: F401
from .rules_support import rule_action, rule_row

pytestmark = pytest.mark.browser
TARGETS = 'Workload targets for this rota'


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

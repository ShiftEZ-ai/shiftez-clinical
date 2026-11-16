"""The Schedule's one selection panel (docs/design/selection-panel.md): a day, row, group, person or any cell selects;
Ctrl adds; Esc clears; the panel's tabs (Details or Summary, Rules, Issues, a person's Duties) open on what was clicked;
"This selection | All rules"; and Make it the department default proposes this rota's number. Runs in CI only."""
import json
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, pick_view, press_escape, planner_tool  # noqa: F401
from .test_browser_rules_in_planner import rota, open_planner, synthetic_rules
from .test_rule_templates import propose
from .rules_support import rule_action, rule_row

pytestmark = pytest.mark.browser


def test_a_day_and_a_row_select_together_and_esc_clears(browser, live_server, h):
    w, _ = rota(h)  # 4–10 Oct 2026
    context, page = sign_in(browser, live_server, 'coordinator'); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        open_planner(page, w)
        monday = page.locator('.wk-day[data-rk*="2026-10-05"]').first
        monday.click()
        panel = page.locator('.rk-panel')
        expect(panel.get_by_role('heading', level=2)).to_contain_text('Mon')
        expect(panel.get_by_role('tab', name=re.compile('^Rules'))).to_have_attribute('aria-selected', 'true')  # a heading opens on Rules
        expect(monday).to_have_class(re.compile(r'\brk-selected\b'))
        row = page.locator('.wk-rowhead.rk-pinnable').first
        row.click(modifiers=['Control'])
        expect(panel.get_by_role('heading', level=2)).to_have_text('2 selected')
        expect(page.locator('.rk-selected')).to_have_count(2)
        row.click(modifiers=['Control'])  # again: taken out of the selection
        expect(panel.get_by_role('heading', level=2)).to_contain_text('Mon')
        expect(page.locator('.rk-selected')).to_have_count(1)
        monday.focus()
        page.keyboard.press('Escape')  # nothing selected: the panel shows the rota's rules (Why panel v2)
        expect(page.get_by_role('dialog', name='Rules in this rota')).to_be_visible()
        expect(page.locator('.rk-selected')).to_have_count(0)
        assert errors == []
    finally:
        context.close()


def test_an_empty_cell_opens_its_details_and_all_rules_shows_the_rotas(browser, live_server, h):
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        cell = page.locator('.wk-cell.rk-pinnable:not([data-session])').first
        cell.click()
        panel = page.locator('.rk-panel')
        expect(panel.get_by_role('heading', level=2)).to_contain_text(' · ')  # "Duty · Sunday 4 Oct": one line
        expect(panel.get_by_role('tab', name='Details')).to_have_attribute('aria-selected', 'true')
        expect(panel.get_by_text('No session here.')).to_be_visible()
        expect(panel.locator('.rk-duties')).to_have_count(0)  # never a list of dates
        expect(cell).to_have_class(re.compile(r'\brk-selected\b'))
        panel.get_by_role('tab', name=re.compile('^Rules')).click()
        expect(panel.get_by_role('radiogroup')).to_have_count(0)  # no switches: the selection drives the panel
        page.evaluate('document.activeElement.blur()')
        page.keyboard.press('Escape')  # nothing selected: the rota's rules, not only here
        expect(rule_row(page.get_by_role('dialog', name='Rules in this rota'), 'Review clinic hours')).to_be_visible()
        expect(cell).not_to_have_class(re.compile(r'\brk-selected\b'))
    finally:
        context.close()


def test_a_person_opens_on_their_details_card_and_lists_their_duties(browser, live_server, h):
    w, _ = rota(h)
    alex = h.seed['people'][0]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        pick_view(page, 'People')
        page.locator(f'.pg-row[data-row-person="{alex}"] .pg-name').click()
        panel = page.locator('.rk-panel')
        expect(panel.get_by_role('tab', name='Details')).to_have_attribute('aria-selected', 'true')
        expect(panel.locator('.pc')).to_be_visible()  # the one person card, not a second one
        panel.get_by_role('tab', name='Duties').click()
        expect(panel.locator('.rk-duties li')).to_have_count(2)  # Monday and Tuesday
        expect(panel.locator('.rk-fact').filter(has_text='duties')).to_contain_text('2')
        press_escape(page)
    finally:
        context.close()


def test_make_it_the_department_default_proposes_this_rotas_number(browser, live_server, h):
    w, rules = rota(h)
    w = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': rules['duties']['id'], 'mode': 'set', 'amount': 4,
                                                           'reason': 'Short staffed in October'}, w['id'], w['revision'])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        page.locator('.wk-day[data-rk*="2026-10-05"]').first.click()
        panel = page.locator('.rk-panel')
        row = rule_row(panel, 'Weekly duty limit')
        row.locator('.rk-row-head').click()
        row.locator('.rk-more').click()
        item = page.get_by_role('menu', name='More actions').get_by_role('menuitem', name=re.compile('^Make department default…'))
        expect(item).to_have_text('Make department default…')  # an enabled item is its label alone (AGENTS §2)
        item.click()
        dialog = page.get_by_role('dialog', name=re.compile('^Change Weekly duty limit'))
        expect(dialog.get_by_role('spinbutton').first).to_have_value('4')
    finally:
        context.close()


def test_a_selections_issues_are_the_one_issue_list(browser, live_server, h):
    """The Issues tab is issue_marker.ts issueList (FIXLIST-UX's one list), never a second list."""
    w, rules = rota(h)
    w = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': rules['duties']['id'], 'mode': 'set', 'amount': 1,
                                                           'reason': 'Issue list fixture'}, w['id'], w['revision'])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    alex = h.seed['people'][0]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        pick_view(page, 'People')
        page.locator(f'.pg-row[data-row-person="{alex}"] .pg-name').click()
        panel = page.locator('.rk-panel')
        issues = panel.get_by_role('tab', name=re.compile('^Issues'))
        expect(issues).to_contain_text(re.compile(r'\d'))
        issues.click()
        expect(panel.locator('.iss-list .iss-row').first).to_be_visible()  # the one list's items, not a second list
        expect(panel.locator('.iss-line')).to_have_count(0)
    finally:
        context.close()


def test_a_modifier_click_on_a_session_selects_it_and_a_plain_click_still_opens_it(browser, live_server, h):
    """One model: the panel follows what is selected. Ctrl + click on a session's name selects the session (its cell is
    ringed); a plain click still opens the session panel, where it is staffed; bulk checkboxes stay bulk actions."""
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        chip = page.locator('button.pchip[data-assignment]').first
        shift = chip.get_attribute('data-shift')
        chip.click(modifiers=['Control'])
        panel = page.locator('.rk-panel')
        expect(panel.get_by_role('heading', level=2)).to_contain_text(' · ')
        expect(panel.get_by_role('tab', name='Details')).to_have_attribute('aria-selected', 'true')
        expect(page.locator(f'.wk-cell[data-session~="{shift}"]')).to_have_class(re.compile(r'\brk-selected\b'))
        expect(page.locator('.wk-panel')).to_have_count(0)  # selected, not opened
        page.locator('.wk-day[data-rk*="2026-10-05"]').first.click(modifiers=['Control'])
        expect(panel.get_by_role('heading', level=2)).to_have_text('2 selected')
        expect(page.locator('input.wk-check:checked')).to_have_count(0)  # the bulk selection is untouched
        press_escape(page)
        chip.click()
        expect(page.locator('.wk-panel')).to_be_visible()
    finally:
        context.close()


def test_select_mode_turns_taps_into_add_and_remove(browser, live_server, h):
    """Touch has no modifier keys: the panel's Select toggle makes every tap add or remove; it is off again on close."""
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        page.locator('.wk-day[data-rk*="2026-10-05"]').first.click()
        panel = page.locator('.rk-panel')
        select = panel.get_by_role('button', name='Select', exact=True)
        expect(select).to_have_attribute('aria-pressed', 'false')
        select.click()
        expect(select).to_have_attribute('aria-pressed', 'true')
        page.locator('.wk-day[data-rk*="2026-10-06"]').first.click()
        expect(panel.get_by_role('heading', level=2)).to_have_text('2 selected')
        page.locator('.wk-day[data-rk*="2026-10-06"]').first.click()  # again: taken out
        expect(page.locator('.rk-selected')).to_have_count(1)
        panel.get_by_role('button', name='Close rules panel (Esc)', exact=True).click()
        page.locator('.wk-day[data-rk*="2026-10-05"]').first.click()
        expect(page.locator('.rk-panel').get_by_role('button', name='Select', exact=True)).to_have_attribute('aria-pressed', 'false')
    finally:
        context.close()


def test_a_rule_with_a_waiting_proposal_shows_proposed_and_offers_no_second_one(browser, live_server, h):
    w, rules = rota(h)
    # A coordinator proposes a new version of the duty limit; no approver has activated it.
    proposal = propose(h, {'kind': 'max_duties', 'maximum': 6, 'window': 'week'}, name='Weekly duty limit', replaces_id=rules['duties']['id'])
    # The panel reads the server's answer (rule map rules[].proposal), never a guess from the policy rows it holds.
    mapped = next(r for r in h.get('coordinator', f"/api/workspaces/{w['id']}/rule-map")['rules'] if r['id'] == rules['duties']['id'])
    assert mapped['proposal']['policy_id'] == proposal['id'], mapped.get('proposal')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        page.locator('.wk-day[data-rk*="2026-10-05"]').first.click()
        row = rule_row(page.locator('.rk-panel'), 'Weekly duty limit')
        # Folded, the row says it in words (Why panel v2: no pill); opened, it names who proposed it and when.
        expect(row.locator('.rk-row-head')).to_contain_text('A change is waiting for approval.')
        row.locator('.rk-row-head').click()
        expect(row.locator('.rk-detail')).to_contain_text('a change waiting for approval')
        row.locator('.rk-more').click()
        expect(page.get_by_role('menu', name='More actions').get_by_role('menuitem', name=re.compile('^Make department default…'))).to_be_disabled()
    finally:
        context.close()


def test_make_a_catalogue_rules_rota_change_the_department_default_opens_the_servers_seed(browser, live_server, h):
    """A catalogue rule's one rota adjustment is made the department default from the server's seed
    (POST /api/policies/{id}/promote-adjustment), opened in the rule line panel, whose one way out is the department
    proposal (no Apply to this rota); never the rule's own page."""
    from .test_browser_rule_catalogue import flag, catalogue_rule, line
    from .test_browser_rules_in_planner import MON
    flag(h)
    broad = line({'max': {'value': 5, 'level': 'mandatory'}})
    rule = catalogue_rule(h, 'days_in_a_row', [broad])
    w = h.shift(h.workspace('Promote a rota change'), *MON, people=[h.seed['people'][0]])['workspace']
    w = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': rule['id'], 'mode': 'set', 'amount': 4, 'line_id': broad['id'],
                                                           'number': 'max', 'reason': 'Four in a row this rota'}, w['id'], w['revision'])['workspace']
    adjustment = w['rule_adjustments'][0]['id']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        page.locator('.wk-day[data-rk*="2026-10-05"]').first.click()
        row = rule_row(page.locator('.rk-panel'), rule['name'])
        with page.expect_request(lambda r: r.url.endswith(f"/api/policies/{rule['id']}/promote-adjustment")) as asked:
            rule_action(row, 'Make department default…')
        assert asked.value.post_data_json == {'workspace_id': w['id'], 'adjustment_id': adjustment}
        panel = page.locator('.rc-line-panel')
        expect(panel.locator('.rc-says')).to_contain_text('4')  # this rota's number, ready to propose
        foot = panel.locator('.rc-lp-foot')
        expect(foot.get_by_role('button', name='Apply to this rota', exact=True)).to_have_count(0)
        expect(page).not_to_have_url(re.compile(r'#rules'))
    finally:
        context.close()



def test_ticked_sessions_are_the_panels_selection(browser, live_server, h):
    """QA-078 (live ce85b38e9): with two session checkboxes ticked the panel stayed on the session clicked last. The ticked
    set is the selection, whatever changes it (the set's one change point in app.ts): "2 sessions" with the set's rules;
    unticking both, or Clear in the selection bar, shows the rota's rules again."""
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator'); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        open_planner(page, w)
        page.locator('.wk-day[data-rk*="2026-10-05"]').first.click()  # a panel open on Monday
        panel = page.locator('.rk-panel')
        expect(panel.get_by_role('heading', level=2)).to_contain_text('Mon')
        hits = page.locator('.wk-grid .wk-check-hit')
        hits.nth(0).click()
        hits.nth(1).click()
        expect(page.locator('input.wk-check:checked')).to_have_count(2)
        expect(panel.get_by_role('heading', level=2)).to_have_text('2 sessions')
        panel.get_by_role('tab', name=re.compile('^Rules')).click()
        expect(rule_row(panel, 'Weekly duty limit')).to_be_visible()  # the set's rules (Alex works both)
        hits.nth(0).click()
        hits.nth(1).click()
        expect(page.locator('input.wk-check:checked')).to_have_count(0)
        expect(panel.get_by_role('heading', level=2)).to_have_text('Rules in this rota')
        # Cleared from the selection bar (not a box): the panel follows the set too.
        hits.nth(0).click()
        hits.nth(1).click()
        expect(panel.get_by_role('heading', level=2)).to_have_text('2 sessions')
        page.get_by_role('button', name='Clear selection', exact=True).click()
        expect(page.locator('input.wk-check:checked')).to_have_count(0)
        expect(panel.get_by_role('heading', level=2)).to_have_text('Rules in this rota')
        assert errors == []
    finally:
        context.close()


def test_the_rules_panels_search_clears_on_the_first_esc_and_the_dock_closes_on_the_second(browser, live_server, h):
    """Live 235391b (3d): a docked panel's search lost its text and closed the panel on the first Esc. The toolbar's search
    is panelSearch(): the first Esc clears the text and the dock stays; the second Esc closes the dock."""
    w, _ = rota(h)
    shift_ids = [s['id'] for s in h.detail(w)['shifts']]
    payload = json.dumps({'days': [f'2026-10-{d:02}' for d in range(4, 11)], 'shifts': shift_ids, 'checked': True, 'fresh': True,
                          'rules': synthetic_rules(12, h.seed['people'], shift_ids), 'markers': []})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/rule-map$'), lambda route: route.fulfill(status=200, content_type='application/json', body=payload))
        page.reload()
        expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible()
        open_planner(page, w)
        page.locator('.wk-day[data-rk]').first.hover()  # the rule map loads on first use
        page.wait_for_function("() => document.querySelector('.planner-main')?.dataset.rkRules === '12'")
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        find = panel.get_by_role('searchbox', name='Find a rule')
        find.fill('Synthetic rule 003')
        find.press('Escape')
        expect(find).to_have_value('')
        expect(panel).to_be_visible()  # the first Esc cleared the text only
        find.press('Escape')
        expect(panel).to_be_hidden()  # the second closes the dock
    finally:
        context.close()


def test_the_rule_highlight_is_never_under_the_selection_bar(browser, live_server, h):
    """Live 235391b (X2): the selection bar covered the locked rule highlight. Both are bottom bars on one layer scale, and
    the highlight rises above the selection bar while it shows."""
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        rule_row(panel, 'Weekly duty limit').locator('.rk-row-head').click()  # opening a rule locks its highlight
        spot = page.locator('.rk-spot.locked')
        expect(spot).to_be_visible()
        page.locator('.wk-grid .wk-check-hit').first.click()
        bar = page.locator('.selection-bar:not([hidden])')
        expect(bar).to_be_visible()
        page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
        a, b = bar.bounding_box(), spot.bounding_box()
        apart = a['y'] >= b['y'] + b['height'] or b['y'] >= a['y'] + a['height'] or a['x'] >= b['x'] + b['width'] or b['x'] >= a['x'] + a['width']
        assert apart, (a, b)
    finally:
        context.close()

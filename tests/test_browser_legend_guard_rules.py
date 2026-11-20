"""The legend guard on the rule views (DESIGN.md › every mark has a legend entry; UIUX finding L9): the Rules list, a
rule's page (and its waiting proposal), the checks page and the Planner's rules panel each draw their marks through
the shared key (dom.ts `legend`), placed after the view it explains, never in the status footer. Runs in CI only
(owner order: no tests on the Mac)."""
import json
import re

import pytest
from playwright.sync_api import expect

from .legend_guard import assert_legend_matches
from .test_browser import browser, live_server, navigate, planner_tool, sign_in  # noqa: F401
from .test_browser_resolve_issues import conflicted, issues_page
from .test_browser_rule_catalogue import catalogue_rule, flag, line
from .test_browser_rules_in_planner import open_planner, rota, synthetic_rules
from .test_rule_catalogue_conflicts import tag_people

pytestmark = pytest.mark.browser
# Every mark a rule's lines draw: the numbers notation, both strengths, an inherited number (↰) and each pill hue.
# A rank pill is tinted by its rank and names itself (owner decision C21, 38c982190), so these lines draw no 'group' mark.
LINE_MARKS = ['mandatory', 'advisory', 'inherited', 'person', 'place']


def mandatory(n):
    return {'value': n, 'level': 'mandatory'}


def lines_of_every_kind(h):
    """Duties per month: everyone 1 – 20, Alex ≤ 10 (inheriting the minimum and the rank's target, ↰), the demo rank → 8 advisory, one
    clinic ≤ 15 and #exceptional_trainee ≤ 6 (a tag chip names itself)."""
    a = h.seed['people'][0]
    tag = tag_people(h, 'exceptional_trainee', [a])
    who = lambda field, v: {'conditions': [{'field': field, 'values': [v]}]}  # noqa: E731
    lines = [line({'min': mandatory(1), 'max': mandatory(20)}, note='Everyone line'),
             line({'max': mandatory(10)}, who=who('person_ids', a), note='Alex line'),
             line({'target': {'value': 8, 'level': 'medium'}}, who=who('category_ids', h.seed['category']), note='Rank line'),
             line({'max': mandatory(15)}, where=who('service_ids', h.seed['services'][0]), note='Clinic line'),
             line({'max': mandatory(6)}, who=who('person_ids', tag), note='Tag line')]
    return catalogue_rule(h, 'duties_per_month', lines), lines


def no_footer_key(page):
    """The status footer holds status only: no legend entries, and none of the key's words."""
    footer = page.locator('.app-footer')
    expect(footer.locator('.footer-legend')).to_have_count(0)
    expect(footer).not_to_contain_text('must hold')
    expect(footer).not_to_contain_text('on leave or away')


def test_the_rules_list_keys_every_mark_its_lines_draw(browser, live_server, h):
    flag(h)
    lines_of_every_kind(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        toggle = main.get_by_role('button', name='Duties per month lines')
        toggle.click()
        expect(main.locator('li.rt-ln')).to_have_count(5)
        # Alex is of the demo rank: the minimum (Everyone) and the target (the rank line) come from broader lines.
        expect(main.locator('li.rt-ln').filter(has_text='Alex Morgan').locator('.rc-inh')).to_have_count(2)
        expect(main.locator('.rt > .view-legend')).to_be_visible()  # after the table it explains
        assert_legend_matches(page, root='#main', expect_marks=['numbers', *LINE_MARKS])
        no_footer_key(page)
        # Closed again, its lines' marks go and so do their entries.
        toggle.click()
        expect(toggle).to_have_attribute('aria-expanded', 'false')
        expect(main.locator('li.rt-ln')).to_have_count(0)
        assert_legend_matches(page, root='#main')
    finally:
        context.close()


def test_a_rule_page_and_its_waiting_proposal_key_their_marks(browser, live_server, h):
    flag(h)
    rule, lines = lines_of_every_kind(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_month')
        main = page.locator('#main')
        expect(main.locator('tr[data-row]')).to_have_count(5)
        expect(main.locator('.rc-crumb .rc-info')).to_have_attribute('data-tip', re.compile('the most specific who wins'))
        # One number per column (Min, Max, Target): the min – max → target notation is not drawn, so not keyed.
        names = assert_legend_matches(page, root='#main', expect_marks=LINE_MARKS)
        assert 'numbers' not in names, sorted(names)
        no_footer_key(page)
    finally:
        context.close()
    # A change waiting for approval: its line changes read min – max → target.
    b = h.seed['people'][1]
    more = lines + [line({'min': mandatory(2), 'max': mandatory(4)}, who={'conditions': [{'field': 'person_ids', 'values': [b]}]}, note='Jordan line')]
    check = h.client('coordinator').post('/api/rules/catalogue/duties_per_month/check', json={'lines': more}).json()
    h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_month', 'expected_policy_id': rule['id'], 'expected_revision': rule['revision'],
                                              'lines': more, 'acknowledged_overlaps': [o['line_id'] for o in check['overlaps']], 'reason': 'Jordan range'})
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules/duties_per_month')
        main = page.locator('#main')
        expect(main.locator('.rc-waiting .rc-prop-table')).to_contain_text('Jordan')
        assert_legend_matches(page, root='#main', expect_marks=['numbers', 'mandatory', 'person'])
    finally:
        context.close()


def test_the_checks_page_keys_its_leave_pill(browser, live_server, h):
    w, _ = conflicted(h)  # Alex on leave (type "Annual") over two of his duties
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        expect(main.locator('tbody tr[data-row]').filter(has_text='Annual').first).to_be_visible()
        expect(main.locator('.issues-list > .view-legend')).to_contain_text('on leave or away')
        assert_legend_matches(page, root='#main', expect_marks=['leave'])
        no_footer_key(page)
    finally:
        context.close()


def test_the_planner_rules_panel_keys_each_rule_state(browser, live_server, h):
    w, _ = rota(h)
    shift_ids = [s['id'] for s in h.detail(w)['shifts']]
    states = ['breach', 'warning', 'met']
    rules = synthetic_rules(6, h.seed['people'], shift_ids, lambda i: states[i % 3])
    for r in rules:
        r['status']['warnings'] = int(r['status']['state'] == 'warning')
    payload = json.dumps({'days': [f'2026-10-{d:02}' for d in range(4, 11)], 'shifts': shift_ids, 'checked': True, 'fresh': True, 'rules': rules, 'markers': []})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/rule-map$'), lambda route: route.fulfill(status=200, content_type='application/json', body=payload))
        page.reload()
        expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible()
        open_planner(page, w)
        page.locator('.wk-day[data-rk]').first.hover()  # the rule map loads on first use
        page.wait_for_function("() => document.querySelector('.planner-main')?.dataset.rkRules === '6'")
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        expect(panel.locator('.rk-row')).to_have_count(4)  # the met rest folds under "N more are met"
        panel.locator('.rk-fold').click()
        expect(panel.locator('.rk-row')).to_have_count(6)
        expect(panel.locator('.view-legend')).to_contain_text('Must fix')
        assert_legend_matches(page, root='.rk-panel', expect_marks=['issue:error', 'issue:warning', 'state:met'])
        # Folded again, only the rules with issues are drawn: the key follows.
        panel.locator('.rk-fold').click()
        expect(panel.locator('.rk-row')).to_have_count(4)
        names = assert_legend_matches(page, root='.rk-panel', expect_marks=['issue:error', 'issue:warning'])
        assert 'state:met' not in names, sorted(names)
    finally:
        context.close()

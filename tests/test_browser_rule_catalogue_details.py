"""Rules catalogue details from the frontend audit (U-14, U-20, U-21, U-24, U-26, U-28): what the page head and a
proposal note say, what select-all picks, unit words, Undo's message after an activation and target sizes."""
import re  # noqa: F401

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import flag, weekly, line

pytestmark = pytest.mark.browser


def entry(h, key):
    return next(e for e in h.get('approver', '/api/rules/catalogue')['entries'] if e['id'] == key)


def propose_off(h):
    """A waiting change of Duties per week that only turns it off (its lines unchanged)."""
    e = entry(h, 'duties_per_week')
    rule, lines = e['rule'], e['rule']['definition']['rule']['lines']
    check = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check', json={'on': False, 'lines': lines}).json()
    return h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_week', 'on': False, 'lines': lines,
        'expected_policy_id': rule['id'], 'expected_revision': rule['revision'], 'acknowledged_overlaps': [o['line_id'] for o in check['overlaps']],
        'reason': 'Pause the weekly cap'})['record']


def test_historical_advisory_safety_line_displays_as_mandatory(browser, live_server, h):
    from .test_conversion_safety_kinds import store_level
    from .test_rule_catalogue_api import approve, propose

    flag(h)
    applied = approve(h, propose(h, 'rest_between_duties', [
        line(numbers={'min': {'value': 20, 'level': 'mandatory'}}),
    ])['record'])
    store_level(h, applied, 0, 'min')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/rest_between_duties')
        badge = page.locator('#main .rc-level[data-tip="Safety rules are always mandatory"]')
        expect(badge).to_contain_text('Mandatory')
    finally:
        context.close()


def test_inherited_safety_number_displays_as_mandatory(browser, live_server, h):
    from .test_conversion_safety_kinds import store_level
    from .test_rule_catalogue_api import approve, propose

    flag(h)
    applied = approve(h, propose(h, 'people_per_session', [
        line(numbers={'min': {'value': 2, 'level': 'mandatory'}}),
        line(numbers={'target': {'value': 3, 'level': 'high'}},
             who={'conditions': [{'field': 'person_ids', 'values': [h.seed['people'][0]]}]}),
    ])['record'])
    store_level(h, applied, 0, 'min')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/people_per_session')
        inherited = page.locator('#main .rule-num').filter(has=page.locator('.rc-inh', has_text='2↰'))
        expect(inherited.locator('.rc-level[data-level="mandatory"]')).to_contain_text('Mandatory')
    finally:
        context.close()


def test_the_rule_page_head_says_its_facts_once_and_a_proposal_only_what_it_changes(browser, live_server, h):
    """U-21: under the name, the family and reach (the description is the name's ⓘ; the line count is the footer's).
    U-20: a proposal that only turns the rule off says just that, with no empty table of line changes."""
    flag(h)
    weekly(h)
    propose_off(h)
    description = entry(h, 'duties_per_week')['description']
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules/duties_per_week')
        main = page.locator('#main')
        summary = main.locator('.rc-summary')
        expect(summary).to_be_visible()
        expect(summary).not_to_contain_text(description.rstrip('.'))
        expect(summary).not_to_contain_text('2 lines')
        expect(main.locator('.rc-crumb .rc-info')).to_have_attribute('data-tip', re.compile('^' + re.escape(description)))
        expect(page.locator('.app-footer')).to_contain_text('2 lines')
        waiting = main.locator('.rc-waiting')
        expect(waiting.locator('.rc-prop-summary')).to_have_text('Turns the rule off')
        expect(waiting.locator('.rc-prop-table')).to_have_count(0)
    finally:
        context.close()


def test_select_all_picks_lines_so_rule_on_never_switches_on_an_empty_rule(browser, live_server, h):
    """U-24: the catalogue lists empty rule types too; select-all picks the lines shown, never those empty rules."""
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        expect(main.locator('li.rt-item').filter(has_text='no lines').first).to_be_visible()
        with_lines = [e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['rule'] and e['rule']['definition']['rule']['lines']]
        main.get_by_role('checkbox', name='Select all lines shown').check()
        bar = page.locator('.selection-bar:not([hidden])')
        lines = sum(len(e['rule']['definition']['rule']['lines']) for e in with_lines)
        expect(bar).to_contain_text(f"{len(with_lines)} rule{'s' if len(with_lines) > 1 else ''} · {lines} lines")  # never the empty ones
        expect(main.locator('.rt-qhead .rt-picked')).to_have_count(0)  # the family's tri-state box says it
    finally:
        context.close()


def test_undoing_a_new_version_says_the_previous_version_applies_again(browser, live_server, h):
    """U-14: undoing the activation of a rule's new version brings back the version before (not "the rule is retired")."""
    flag(h)
    weekly(h)
    propose_off(h)
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules/duties_per_week')
        page.locator('.rc-waiting').get_by_role('button', name='Review and approve').click()
        # B5 (f1d80dcc4): an independent approver decides in the docked panel, named after the change; no reason (maintenance).
        panel = page.get_by_role('region', name='Change to Duties per week · turns the rule off', exact=True)
        panel.get_by_role('button', name='Approve and activate', exact=True).click()
        expect(panel).to_have_count(0)
        # The approval says the rule is off and offers Undo.
        page.locator('#toast-tray .toast', has_text='Duties per week: the rule is off.').get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('#toast-tray .toast', has_text='Activation undone: the previous version applies again.')).to_be_visible()
    finally:
        context.close()
    assert entry(h, 'duties_per_week')['rule']['definition']['rule'].get('on') is not False


def test_rule_numbers_say_one_weekend_and_two_weekends(browser, live_server, h):
    """U-26: a unit word agrees with its number (rule_view.ts limitPill, the legacy list's numbers)."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        said = page.evaluate("""async () => {const {limitPill}=await window.importBundle('rule_view.js');
            return [limitPill([{min:null,max:1,target:null,unit:'weekends'}]),limitPill([{min:null,max:2,target:null,unit:'weekends'}]),
                    limitPill([{min:null,max:1,target:null,unit:'weekends'},{min:null,max:2,target:null,unit:'weekends'}]),
                    limitPill([{min:null,max:1,target:null,unit:'duties'}]),limitPill([{min:null,max:1,target:null,unit:'session units'}])];}""")
        # The singular of "duties" is "duty", not "dutie".
        assert said == ['≤ 1 weekend', '≤ 2 weekends', '≤ 1–2 weekends', '≤ 1 duty', '≤ 1 session unit'], said
    finally:
        context.close()


def test_rules_controls_are_at_least_24_px(browser, live_server, h):
    """U-28 (WCAG 2.2 SC 2.5.8): the open toggles and the rule's ⓘ are 24 × 24 targets, not only by spacing."""
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        toggles = page.locator('#main .rt-toggle')
        expect(toggles.first).to_be_visible()
        sizes = toggles.evaluate_all('ts => ts.map(t => t.getBoundingClientRect()).map(r => Math.min(r.width, r.height))')
        assert min(sizes) >= 24, sizes
        navigate(page, 'rules/duties_per_week')
        info = page.locator('#main .rc-crumb .rc-info')
        expect(info).to_be_visible()
        box = info.bounding_box()
        assert min(box['width'], box['height']) >= 24, box
    finally:
        context.close()

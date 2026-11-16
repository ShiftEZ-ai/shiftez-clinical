"""Fair share line dials in the browser: a line's share of a fair amount, its leeway, and the Off strength."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import add_filter, browser, live_server, navigate  # noqa: F401
from .test_browser_rule_filter_builder import pick
from .test_browser_rule_catalogue import flag
from .test_browser_rule_catalogue_v2 import line, reason, shot, themed

pytestmark = pytest.mark.browser

GOAL = {'balance_goal': {'metric': 'duties', 'period': 'rota'}}


def fair_share_rule(h, lines):
    """The department's one Fair share rule (seeded with its workload line) gets these lines as well."""
    existing = next(p for p in h.get('admin', '/api/policies?status=active')['policies']
                    if p['definition']['rule'].get('id') == 'fair_share')
    lines = existing['definition']['rule']['lines'] + lines
    check = h.client('coordinator').post('/api/rules/catalogue/fair_share/check', json={'lines': lines, 'settings': GOAL}).json()
    record = h.command('coordinator', 'rule.propose', {'catalogue_id': 'fair_share', 'catalogue_version': 1,
        'expected_policy_id': existing['id'], 'expected_revision': existing['revision'], 'lines': lines, 'settings': GOAL,
        'acknowledged_overlaps': [o['line_id'] for o in check['overlaps']], 'reason': 'Fair share fixture'})['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent review'}, record['id'], record['revision'])


def fair_share_lines(h):
    rule = next(e['rule'] for e in h.get('approver', '/api/rules/catalogue?include_hidden=true')['entries'] if e['id'] == 'fair_share')
    return rule['definition']['rule']['lines']


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_fair_share_line_sets_its_share_leeway_and_off(browser, live_server, h, width, theme):
    flag(h)
    person = h.seed['people'][0]
    general = line({'apply': {'value': None, 'level': 'medium'}})
    own = line({'apply': {'value': None, 'level': 'medium'}}, who={'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [person]}]})
    fair_share_rule(h, [general, own])
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    approver, review = themed(browser, live_server, 'approver', width, theme)
    try:
        navigate(page, f'rules/fair_share/{own["id"]}')
        panel = page.locator('.rc-line-panel')
        panel.get_by_role('button', name='Edit…', exact=True).click()
        pop = page.locator('.rc-settings')
        expect(pop).to_contain_text('A percentage of a fair share (50% is half)')
        for label in ('Share of a fair amount', 'Leeway around the aim (%)', 'Leeway of at least'):
            choices = pop.get_by_role('radiogroup', name=label, exact=True)
            expect(choices.get_by_role('radio', name='Same as rule', exact=True)).to_have_attribute('aria-checked', 'true')
            choices.get_by_role('radio', name='Own value', exact=True).click()
        # "As" is always one of its two modes: a blank choice would save a mode no check accepts.
        expect(pop.get_by_label('As', exact=True).locator('option')).to_have_text(
            ['Percent of a fair share', 'More or less than a fair share'])
        share = pop.get_by_role('spinbutton', name='Share of a fair amount', exact=True)
        pop.get_by_label('As', exact=True).select_option('amount')
        share.fill('-2')
        assert share.evaluate('x => x.checkValidity()')
        pop.get_by_label('As', exact=True).select_option('percent')
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(share).to_be_focused()  # negative amounts are valid, negative percentages are not
        expect(pop).to_be_visible()
        share.fill('')
        choices = pop.get_by_role('radiogroup', name='Share of a fair amount', exact=True)
        expect(choices.get_by_role('radio', name='Same as rule', exact=True)).to_have_attribute('aria-checked', 'true')
        choices.get_by_role('radio', name='Own value', exact=True).click()
        share.fill('50')
        pop.get_by_role('spinbutton', name='Leeway around the aim (%)', exact=True).fill('20')
        pop.get_by_role('spinbutton', name='Leeway of at least', exact=True).fill('2')
        shot(page, 'fair-share-configured-dials', width, theme)
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(panel.locator('.rc-lp-settings')).to_contain_text('50% of a fair share')
        # Off is a strength of its own: the line stays, and nothing is counted for it.
        panel.get_by_role('button', name='Applies: advisory · medium').click()
        menu = page.get_by_role('menu', name='Applies strength')
        expect(menu.get_by_role('menuitemradio', name='Off · not applied')).to_be_visible()
        menu.get_by_role('menuitemradio', name='Off · not applied').click()
        expect(panel.get_by_role('button', name='Applies: off, not applied')).to_have_text('Off')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        page.locator('.topbar').get_by_role('button', name='Propose 1 change', exact=True).click()
        page.get_by_role('dialog').get_by_label('I have read which lines give way and that is intended', exact=True).check()
        reason(page, 'Half a share for a part-timer', 'Propose')
        expect(page.locator('.rc-waiting')).to_contain_text('waiting for approval')
        navigate(review, 'rules/fair_share')
        review.locator('.rc-waiting').get_by_role('button', name='Review and approve', exact=True).click()
        # B5 (f1d80dcc4): the decision panel is named after the change; an independent approver activates in one step.
        decision = review.get_by_role('region', name=re.compile('^Change a line for '))
        decision.get_by_role('button', name='Approve and activate', exact=True).click()
        expect(decision).to_have_count(0)
        expect(review.locator('.rc-waiting')).to_have_count(0)
        changed = next(l for l in fair_share_lines(h) if l['id'] == own['id'])
        # maintenance (3e40bd3be): a person line stores its resolved settings, the rule's own (metric, period) included.
        assert changed['settings']['balance_goal'] == {**GOAL['balance_goal'], 'share_adjustment': {'mode': 'percent', 'value': 50},
                                                       'tolerance_percent': 20, 'tolerance_units': 2}
        assert changed['numbers']['apply']['level'] == 'off'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()
        approver.close()


def test_fair_share_amount_dial_reads_as_more_or_less(browser, live_server, h):
    flag(h)
    general = line({'apply': {'value': None, 'level': 'medium'}},
                   settings={'balance_goal': {'share_adjustment': {'mode': 'amount', 'value': -2}}})
    fair_share_rule(h, [general])
    context, page = themed(browser, live_server, 'auditor', 1440, 'dark')
    try:
        navigate(page, f'rules/fair_share/{general["id"]}')
        expect(page.locator('.rc-line-panel .rc-lp-settings')).to_contain_text('2 less than a fair share')
        shot(page, 'fair-share-negative-amount', 1440, 'dark')
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_pair_exposure_partners_are_chosen_in_settings_with_the_shared_builder(browser, live_server, h, width, theme):
    """DEV acceptance maintenance: Fair share's pair exposure needs its partners; Settings offers them with the shared condition
    builder (person fields only, as the contract allows) and the proposal saves them."""
    flag(h)
    fair_share_rule(h, [line({'target': {'value': 1, 'level': 'medium'}, 'apply': {'value': None, 'level': 'medium'}})])
    senior = next(c['name'] for c in h.get('admin', '/api/catalogues')['categories'] if c['id'] == h.seed['category'])
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    try:
        navigate(page, 'rules/fair_share')
        page.get_by_role('button', name='Settings', exact=True).click()
        pop = page.locator('.rc-settings')
        partners = pop.get_by_role('group', name='Partners', exact=True)
        expect(partners).to_be_hidden()  # partners count only for duties shared with a partner
        pop.get_by_label('What to count', exact=True).select_option(label='Duties shared with a partner')
        expect(partners).to_contain_text('who the partners are')
        add_filter(partners)
        options = partners.locator('.cond-line').first.get_by_label('Filter on', exact=True).evaluate("s => [...s.options].map(o => o.textContent)")
        assert 'Rank' in options and not {'Clinic', 'Duty type', 'Place'} & set(options), options  # who, not where
        pick(page, partners.locator('.cond-line').first, 'Rank', [senior])
        pop.get_by_label('Aim', exact=True).select_option(label='At least once per pair')
        shot(page, 'fair-share-pair-partners', width, theme)
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(pop).to_have_count(0)
        page.locator('.topbar').get_by_role('button', name=re.compile(r'^Propose \d+ change')).click()
        reason(page, 'Trainees meet each consultant', 'Propose')
        expect(page.locator('.rc-waiting')).to_contain_text('waiting for approval')
        rule = next(e for e in h.get('coordinator', '/api/rules/catalogue?include_hidden=true')['entries'] if e['id'] == 'fair_share')
        goal = rule['proposed']['definition']['rule']['settings']['balance_goal']
        assert goal['metric'] == 'pair_exposure' and goal['aim'] == 'min_per_pair'
        assert goal['partners']['conditions'][0]['values'] == [h.seed['category']]
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()

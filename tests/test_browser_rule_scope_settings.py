"""Two line settings in the v2 rule editor: Team leads counted across concurrent duties together (count_scope: one
lead for the Physical and Virtual UCC duties) and Fair share groups pooled together (comparison). Each is set on a
line, proposed, approved, reloaded and edited; their labels and help come from the catalogue and contract."""
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate  # noqa: F401
from .test_browser_rule_catalogue import flag
from .test_browser_rule_catalogue_v2 import reason, themed

pytestmark = pytest.mark.browser
VIEWS = [(1440, 'light'), (390, 'dark')]


def shot(page, name, width, theme):
    folder = os.getenv('RULES_FE3_SHOTS')
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=f"{folder}/{name}-{width}-{theme}.png", animations='disabled')


def new_line(page, name):
    page.locator('#main').get_by_role('button', name='New line: a person, rank, clinic or #tag').click()
    page.get_by_role('combobox', name='Search New line').fill(name)
    page.get_by_role('option', name=re.compile('^' + re.escape(name))).first.click()
    return page.locator('.rc-line-panel')


def own_choice(pop, label, choice):
    pop.get_by_role('radiogroup', name=label, exact=True).get_by_role('radio', name='Own value', exact=True).click()
    pop.get_by_role('combobox', name=label, exact=True).select_option(label=choice)


def propose_and_approve(page, h, entry, text):
    page.locator('.topbar').get_by_role('button', name=re.compile(r'^Propose \d+ change')).click()
    tick = page.get_by_role('dialog').get_by_label('I have read which lines give way and that is intended', exact=True)
    if tick.count():
        tick.check()
    reason(page, text, 'Propose')
    expect(page.locator('#main .rc-waiting')).to_contain_text('waiting for approval')
    proposed = next(e for e in h.get('approver', '/api/rules/catalogue?include_hidden=true')['entries'] if e['id'] == entry)['proposed']
    h.command('approver', 'policy.activate', {'reason': 'Independent review'}, proposed['id'], proposed['revision'])
    return proposed['definition']['rule']


@pytest.mark.parametrize('width,theme', VIEWS)
def test_one_team_lead_across_concurrent_duties_saves_reloads_and_edits(browser, live_server, h, width, theme):
    flag(h)
    clinic = next(s['name'] for s in h.get('admin', '/api/catalogues')['services'] if s['id'] == h.seed['services'][0])
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    try:
        navigate(page, 'rules/team_leads')
        panel = new_line(page, clinic)
        panel.get_by_role('button', name='Edit…', exact=True).click()
        pop = page.locator('.rc-settings')
        # Label and help are the catalogue's; the choice sits beside Continuity.
        expect(pop).to_contain_text('Count leaders per duty, or across these concurrent duties together.')
        labels = pop.locator('.rc-tree-label').all_text_contents()
        assert labels.index('Count leaders') == labels.index('Continuity') + 1, labels
        own_choice(pop, 'Count leaders', 'Across these concurrent duties together')
        shot(page, 'team-lead-scope-settings', width, theme)
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(pop).to_have_count(0)
        expect(panel.locator('.rc-lp-settings')).to_contain_text('Across these concurrent duties together')
        for label in ('Minimum', 'Maximum'):
            panel.get_by_label(label, exact=True).fill('1')
        expect(panel.get_by_role('heading', level=2)).to_contain_text('1 – 1 team lead across these duties together')
        expect(panel.get_by_role('status')).to_contain_text('1 team lead across these duties together from everyone')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        rule = propose_and_approve(page, h, 'team_leads', 'One lead covers both UCC duties')
        line = next(l for l in rule['lines'] if l.get('settings', {}).get('team_lead_requirement'))
        assert line['settings']['team_lead_requirement']['count_scope'] == 'concurrent_duties'

        # Reload: the line reads in words and edits back to per duty.
        page.reload()
        navigate(page, f"rules/team_leads/{line['id']}")
        panel = page.locator('.rc-line-panel')
        expect(panel).to_contain_text('across these duties together')
        panel.get_by_role('button', name='Edit…', exact=True).click()
        pop = page.locator('.rc-settings')
        expect(pop.get_by_role('combobox', name='Count leaders', exact=True)).to_have_value('concurrent_duties')
        pop.get_by_role('combobox', name='Count leaders', exact=True).select_option(label='Per duty')
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(panel.locator('.rc-lp-settings')).to_contain_text('Per duty')
        expect(panel).not_to_contain_text('across these duties together')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        shot(page, 'team-lead-scope-edited', width, theme)
        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(page.locator('.topbar').get_by_role('button', name='Propose 1 change', exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', VIEWS)
def test_fair_share_line_pools_its_groups(browser, live_server, h, width, theme):
    flag(h)
    rank = next(c['name'] for c in h.get('admin', '/api/catalogues')['categories'] if c['id'] == h.seed['category'])
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    try:
        navigate(page, 'rules/fair_share')
        panel = new_line(page, rank)
        panel.get_by_role('button', name='Edit…', exact=True).click()
        pop = page.locator('.rc-settings')
        own_choice(pop, 'What to count', 'Duties')  # the seeded rule counts nothing of its own: a line names its measure
        own_choice(pop, 'Groups', 'Pool these groups together')
        expect(pop.get_by_role('combobox', name='Groups', exact=True).locator('option[value="within_group"]')).to_have_text('Each group on its own')
        shot(page, 'fair-share-pooled-settings', width, theme)
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(pop).to_have_count(0)
        expect(panel.locator('.rc-lp-settings')).to_contain_text('Pool these groups together')
        panel.get_by_role('checkbox', name='Applies', exact=True).check()
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        rule = propose_and_approve(page, h, 'fair_share', 'Compare these ranks together')
        line = next(l for l in rule['lines'] if l.get('settings', {}).get('balance_goal', {}).get('comparison'))
        assert line['settings']['balance_goal']['comparison'] == 'pooled'

        # The rule-wide switch is gone: groups are a setting like any other.
        page.reload()
        page.locator('.topbar').get_by_role('button', name='Settings', exact=True).click()
        rule_pop = page.locator('.rc-settings')
        expect(rule_pop.get_by_role('combobox', name='Groups', exact=True)).to_be_visible()
        expect(rule_pop.get_by_role('switch', name='Balance between people')).to_have_count(0)
        rule_pop.get_by_role('button', name='Cancel', exact=True).click()
        expect(rule_pop).to_have_count(0)
        navigate(page, f"rules/fair_share/{line['id']}")
        panel = page.locator('.rc-line-panel')
        expect(panel.locator('.rc-lp-settings')).to_contain_text('Pool these groups together')
        panel.get_by_role('button', name='Edit…', exact=True).click()
        pop = page.locator('.rc-settings')
        expect(pop.get_by_role('combobox', name='Groups', exact=True)).to_have_value('pooled')
        pop.get_by_role('combobox', name='Groups', exact=True).select_option(label='Each group on its own')
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(panel.locator('.rc-lp-settings')).to_contain_text('Each group on its own')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        shot(page, 'fair-share-pooled-edited', width, theme)
        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(page.locator('.topbar').get_by_role('button', name='Propose 1 change', exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()
# Include this file in hosted supervision and team-lead qualification.

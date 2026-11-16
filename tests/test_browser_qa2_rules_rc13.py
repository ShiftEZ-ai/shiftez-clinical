"""rc.13 Rules QA: words in Simplify rules and the line panel, read-only lines as facts, and panels that fit at 1024."""
import re

import pytest
from playwright.sync_api import expect

from .governance_support import identity
from .test_browser import browser, live_server, navigate  # noqa: F401
from .test_browser_qa_rules_rc12 import blocked_department
from .test_browser_rule_catalogue import catalogue_rule, flag, rules_action
from .test_browser_rule_catalogue_v2 import line, reason, simple_legacy, themed, v2_rule

pytestmark = pytest.mark.browser


def test_one_inheriting_person_reads_in_the_singular(browser, live_server, h):
    blocked_department(h)
    context, page = themed(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        rules_action(page, re.compile('^Simplify rules'))
        blocker = page.locator('.rs-blockers li.rs-blocker', has_text='Weekly clinical target')
        expect(blocker).to_contain_text('A person has no number of their own and takes the rule’s usual one. Give them a number')
    finally:
        context.close()


def test_simplify_states_read_as_words_and_undone_matches_the_history(browser, live_server, h):
    simple_legacy(h)
    context, page = themed(browser, live_server, 'coordinator')
    review, rpage = themed(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        rules_action(page, 'Simplify rules')
        page.locator('.rs-panel').get_by_role('button', name='Propose', exact=True).click()
        reason(page, 'One rule per idea', 'Propose')
        expect(page.locator('#toast-tray')).to_contain_text('Proposed')
        navigate(rpage, 'rules')
        rules_action(rpage, 'Simplify rules · waiting for approval')
        rpage.locator('.rs-panel').get_by_role('button', name='Activate', exact=True).click()
        reason(rpage, 'Independently reviewed', 'Activate')
        expect(rpage.locator('#toast-tray')).to_contain_text('Rules simplified')
        rpage.reload()
        rules_action(rpage, 'Simplify rules')
        # The one status palette says its words with a capital, never the raw state.
        expect(rpage.locator('.rs-panel .rs-state .badge')).to_have_text('Active')
        rpage.locator('.rs-panel').get_by_role('button', name='Undo', exact=True).click()
        reason(rpage, 'Restore the previous representation', 'Undo')
        expect(rpage.locator('#toast-tray')).to_contain_text('The previous rules apply again')
        rpage.reload()
        rules_action(rpage, re.compile('^Simplify rules'))
        panel = rpage.locator('.rs-panel')
        panel.locator('summary', has_text='Proposal history').click()
        panel.locator('details.rs-map tbody tr').first.click()
        expect(rpage.locator('.rs-panel .rs-state .badge')).to_have_text('Undone')
    finally:
        context.close()
        review.close()


def quota_world(h):
    flag(h)
    catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 6, 'level': 'mandatory'}},
                                                settings={'block_quota': {'assignment_type_ids': [], 'overtime': 'include'}})])


def open_first_line(page):
    navigate(page, 'rules/duties_per_month')
    page.locator('#main [data-line] td:nth-child(2)').first.click()
    return page.locator('.rc-line-panel')


def test_line_settings_say_what_an_empty_list_means(browser, live_server, h):
    quota_world(h)
    context, page = themed(browser, live_server, 'coordinator')
    try:
        panel = open_first_line(page)
        settings = panel.locator('.rc-lp-settings')
        expect(settings).to_contain_text('All duty types')
        expect(settings).not_to_contain_text('0 chosen')
        # The rule sets none of these itself, so there is no "rule: —" beside each.
        expect(settings).not_to_contain_text('rule:')
        expect(panel.get_by_role('button', name='Use the rule’s settings', exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('role,theme', [('auditor', 'light'), ('approver', 'dark'), ('programme_lead', 'light')])
def test_read_only_line_shows_strength_only_for_its_numbers(browser, live_server, h, role, theme):
    quota_world(h)
    v1 = h.get('admin', '/api/catalogues')['services'][:2]
    catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 4, 'level': 'mandatory'}},
                                               where={'conditions': [{'field': 'service_ids', 'values': [s['id'] for s in v1]}]})])
    if role == 'programme_lead':
        identity(h, role, [role])
    context, page = themed(browser, live_server, role, 1024, theme)
    try:
        navigate(page, 'rules/duties_per_week')
        page.locator('#main [data-line] td:nth-child(2)').first.click()
        panel = page.locator('.rc-line-panel')
        # An empty number has no strength to read; the maximum keeps its "!".
        expect(panel.locator('.rc-strength')).to_have_count(1)
        # A line with its own settings: they open as facts behind Details…, never Edit….
        panel = open_first_line(page)
        expect(panel.get_by_role('button', name='Details…', exact=True)).to_be_visible()
        expect(panel.get_by_role('button', name='Edit…', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_one_person_line_says_their_name(browser, live_server, h):
    flag(h, legacy=False)
    person = h.get('admin', '/api/catalogues')['people'][0]
    rule = v2_rule(h, 'workload_limit', [line({'max': {'value': 4, 'level': 'mandatory'}}, measure='duties', period={'kind': 'calendar_week'},
                                              who={'conditions': [{'field': 'person_ids', 'values': [person['id']]}]})])
    context, page = themed(browser, live_server, 'coordinator')
    try:
        navigate(page, f"rules/workload_limit/{rule['definition']['rule']['lines'][0]['id']}")
        says = page.locator('.rc-line-panel .rc-says')
        expect(says).to_contain_text((person.get('name') or person['display_name']) + ': at most 4')
        expect(says).not_to_contain_text('That person')
    finally:
        context.close()


def test_panels_fit_at_1024(browser, live_server, h):
    blocked_department(h)
    h.command('coordinator', 'policy.create', {'name': 'A long older rule name that wraps in the side panel beside its effect', 'owner': 'Department',
        'source': 'Synthetic', 'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'max_duties', 'maximum': 3, 'window': 'week'}, 'scope': {}, 'severity': 'mandatory', 'exception_allowed': False}})
    context, page = themed(browser, live_server, 'auditor', 1024, 'dark')
    try:
        navigate(page, 'rules')
        rules_action(page, re.compile('^Other department rules'))
        table = page.locator('.dock-panel table')
        expect(table).to_be_visible()
        scroll = table.locator('xpath=ancestor::*[contains(@class,"table-scroll")][1]')
        assert scroll.evaluate('e => e.scrollWidth <= e.clientWidth + 1')
        page.get_by_role('button', name='Close panel', exact=True).click()
        rules_action(page, re.compile('^Simplify rules'))
        expect(page.locator('.rs-panel .rs-state')).to_be_visible()
        # The page title stays whole beside the folded header buttons.
        assert page.locator('.topbar h1').evaluate('e => e.scrollWidth <= e.clientWidth')
    finally:
        context.close()


def test_simplify_for_a_reader_who_cannot_preview_says_no_proposal_waits(browser, live_server, h):
    simple_legacy(h)
    identity(h, 'programme_lead', ['programme_lead'])
    context, page = themed(browser, live_server, 'coordinator')
    lead, lpage = themed(browser, live_server, 'programme_lead')
    try:
        navigate(page, 'rules')
        rules_action(page, 'Simplify rules')
        page.locator('.rs-panel').get_by_role('button', name='Propose', exact=True).click()
        reason(page, 'One rule per idea', 'Propose')
        expect(page.locator('#toast-tray')).to_contain_text('Proposed')
        page.reload()
        rules_action(page, 'Simplify rules · waiting for approval')
        page.locator('.rs-panel').get_by_role('button', name='Withdraw', exact=True).click()
        page.locator('.rs-panel').get_by_role('button', name='Withdraw?', exact=True).click()
        expect(page.locator('#toast-tray')).to_contain_text('Proposal withdrawn')
        # The department is still on its old rules: a reader without the preview is not told there is nothing to simplify.
        navigate(lpage, 'rules')
        rules_action(lpage, 'Simplify rules')
        panel = lpage.locator('.rs-panel')
        expect(panel).to_contain_text('No proposal waiting')
        expect(panel).not_to_contain_text('Nothing to simplify')
        expect(panel.locator('summary', has_text='Proposal history')).to_be_visible()
    finally:
        context.close()
        lead.close()


def test_auto_schedule_defaults_save_says_what_was_saved(browser, live_server, h):
    context, page = themed(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration/auto_schedule')
        page.get_by_role('tab', name='How it decides', exact=True).click()
        overtime = page.get_by_label('Avoid overtime weight', exact=True)
        overtime.fill('2')
        overtime.press('Tab')
        page.get_by_role('button', name='Save defaults', exact=True).click()
        reason(page, 'Overtime matters a little more', 'Save defaults')
        expect(page.locator('#toast-tray')).to_contain_text('Auto-schedule defaults saved')
        assert h.get('admin', '/api/me')['department']['generation_defaults']['overtime_penalty'] == 2
    finally:
        context.close()


def test_configuration_counts_what_is_in_use_like_the_setup_guide(browser, live_server, h):
    from .test_announcements_operations import announce
    announce(h, {'everyone': True}, active=False, name='Draft notice')
    context, page = themed(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration/setup')
        nav = page.get_by_role('navigation', name='Configuration sections')
        # An inactive announcement is not counted: the sidebar and the guide's "Add an announcement" agree.
        expect(nav.get_by_role('button', name='Announcements', exact=True)).not_to_contain_text('1')
        expect(page.locator('#main')).to_contain_text('Add an announcement')
    finally:
        context.close()

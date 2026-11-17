"""The simplified Rules page: one row per rule (one mark, a plain sentence, where and its name, a value pill, who and how
many people), and the rule sheet (who and when, numbers on a range bar, exceptions, the current draft's days, facts)."""
import os
import re

import pytest
from playwright.sync_api import expect

from .test_alpha_calendar import programme
from .test_browser import browser, live_server, sign_in, navigate, open_rule  # noqa: F401
from .test_rule_conditions import line, tree
from .test_rule_templates import activate, propose

pytestmark = pytest.mark.browser
SHOTS = os.environ.get('RULES_SHOTS')


def rules(h):
    a, b, c = h.seed['people']
    prog, _, _ = programme(h)
    cover = activate(h, propose(h, {'kind': 'minimum_cover', 'minimum': 2, 'aggregation': 'service_group'},
                                {'conditions': tree(line('service_ids', *h.seed['services'][:2])), 'weekdays': [6, 0, 1, 2, 3]},
                                name='UCC pooled cover', severity='advisory', strength='medium'))
    hours = activate(h, propose(h, {'kind': 'max_hours', 'hours': 6, 'window': 'day'}, {'conditions': tree(line('person_ids', a, b, c))},
                                name='Daily hours', per_person=[{'person_id': b, 'amount': 8, 'ends_on': '2026-12-01'},
                                                                {'person_id': c, 'amount': 7}]))
    activate(h, propose(h, {'kind': 'max_duties', 'maximum': 5}, name='Residents duty cap', listed_only=True,
                        per_person=[{'programme_id': prog['id'], 'amount': 4, 'starts_on': '2026-10-01'}]))
    waiting = propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest between duties')
    quota = activate(h, propose(h, {'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties',
                                    'comparison': 'range', 'amount': 2, 'maximum_amount': 6, 'target_amount': 4,
                                    'window_alignment': 'calendar_week'}, name='Weekly duties'))
    return cover, hours, waiting, quota


@pytest.mark.parametrize('width', [1440, 390])
def test_rule_rows_marks_pills_and_reach(browser, live_server, h, width):
    cover, hours, waiting, quota = rules(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        navigate(page, 'rules')
        row = page.locator('.rule-item').filter(has_text='UCC pooled cover')
        expect(row.locator('.rl-text strong')).to_have_text(re.compile(r'^At least 2 people at .+ & .+ together$'))
        expect(row.locator('.rl-mark')).to_have_text('Advisory')
        expect(row.locator('.rule-num')).to_have_text('≥ 2')
        expect(row.locator('.rl-text span')).to_have_text('Sun–Thu · UCC pooled cover')   # the clinics are in the sentence, not again
        expect(row.locator('.rl-who')).to_contain_text('Everyone')
        expect(row.locator('.rl-count')).to_have_text(' · 3 people')   # the reach arrives after the first draw
        limits = page.locator('.rule-item').filter(has_text='Daily hours')
        expect(limits.locator('.rl-mark')).to_have_text('Mandatory')
        expect(limits.locator('.rule-num')).to_have_text('≤ 6–8 h')   # its lines allow up to 8
        expect(limits.locator('.rl-count')).to_have_text(' · 3 people')
        expect(page.locator('.rule-item').filter(has_text='Weekly duties').locator('.rule-num')).to_have_text('2 – 6 → 4 duties')
        rest = page.locator('.rule-item').filter(has_text='Rest between duties')
        expect(rest.locator('.rl-mark')).to_have_text('Waiting for approval')
        expect(page.locator('.rl-legend li')).to_have_text(['Waiting for approval', 'Mandatory', 'Advisory', '≥ 2Minimum · ≤ maximum · → target'])
        # Group heads: a coloured square per topic, the count and what waits there.
        head = page.locator('.rule-group-toggle').filter(has_text='Limits & rest')
        expect(head.locator('.rule-group-icon')).to_have_class('rule-group-icon duty-indigo')
        expect(head.locator('.rl-group-meta')).to_have_text('1 waiting for approval')
        assert page.evaluate('() => document.scrollingElement.scrollWidth <= innerWidth')
        if SHOTS:
            page.screenshot(path=f'{SHOTS}/rules-list-{width}.png', full_page=True)
        # Group by mandatory or advisory, then no grouping; sort by people reached.
        page.locator('.rule-toolbar').get_by_role('button', name='Group: Topic').click()
        page.get_by_role('menuitemradio', name='Mandatory or advisory').click()
        expect(page.locator('.rule-group-name')).to_have_text(['Mandatory', 'Advisory'])
        page.locator('.rule-toolbar').get_by_role('button', name='Group: Mandatory or advisory').click()
        page.get_by_role('menuitemradio', name='No grouping').click()
        expect(page.locator('.rule-group-name')).to_have_count(0)
        expect(page.locator('.rule-item')).to_have_count(6)  # Five authored rules plus seeded Fair share.
    finally:
        context.close()


def test_rule_sheet_sections_range_bar_exceptions_and_draft_days(browser, live_server, h):
    cover, hours, waiting, quota = rules(h)
    a, b, _ = h.seed['people']
    w = h.workspace('November draft')
    for day in ('04', '05', '06'):
        w = h.shift(w, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T16:00:00+03:00', people=[a])['workspace']   # 8 h > 6 h on each day
    w = h.shift(w, '2026-10-07T08:00:00+03:00', '2026-10-07T12:00:00+03:00', people=[b])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        sheet = open_rule(page, 'Daily hours')
        expect(sheet.locator('.rl-meta')).to_contain_text('Mandatory')
        expect(sheet.locator('.rl-meta')).to_contain_text('Effective from 1 Jan 2020')
        expect(sheet.locator('.rl-meta')).to_contain_text('Daily hours')
        expect(sheet.get_by_role('tab')).to_have_text(['Details', 'History', 'Source'])
        expect(sheet.locator('.rl-sec-head h3')).to_have_text(['Who & when', 'Numbers', 'Lines for specific people or programmes', 'In November draft'])
        expect(sheet.locator('.rl-sentence')).to_contain_text('All sessions, on every day, all day.')
        # Numbers: the maximum is set, the minimum and target are not.
        expect(sheet.locator('.rl-num')).to_have_text(['Minimum—no lower limit', 'Maximum6–8hours per day · varies by line', 'Target—no target'])
        expect(sheet.locator('.rl-range')).to_have_attribute('aria-label', 'No minimum, maximum 6 to 8, no target')
        expect(sheet.locator('.rl-tick-label')).to_have_text(['6', '8'])
        # Exceptions: each person's own number and dates, as a list.
        expect(sheet.locator('.rl-exc strong')).to_have_count(2)
        expect(sheet.locator('.rl-exc small').first).to_have_text('Person · maximum 8 · until 30 Nov 2026')
        expect(sheet.locator('.rl-exc small').last).to_have_text('Person · maximum 7')
        expect(sheet.get_by_role('button', name='New line')).to_be_visible()
        # The draft: a bar per day; a's three long days are short.
        section = sheet.locator('.rl-sec').filter(has_text='In November draft')
        section.get_by_role('button', name='Show effect in November draft', exact=True).click()
        expect(section.locator('.rl-day')).to_have_count(7)
        expect(section.locator('.rl-day.short')).to_have_count(3)
        expect(section.locator('.rl-health-count')).to_have_text('4 of 7 days met')
        expect(section.locator('.hint')).to_contain_text('Short on Sun 4 Oct')
        expect(section.locator('.hint')).to_contain_text('Counts 3 people today.')
        expect(sheet.locator('.rule-facts dt')).to_have_text(['Effective', 'Exception requests', 'Checked against'])
        expect(sheet.locator('.rl-sheet-foot').get_by_role('button')).to_have_text(['Preview impact', 'Change…'])
        if SHOTS:
            page.screenshot(path=f'{SHOTS}/rules-sheet.png')
        page.keyboard.press('Escape')
        expect(sheet).to_have_count(0)
        # A rule that does not apply in the draft's days, or is not active there, has no draft section.
        sheet = open_rule(page, 'Rest between duties')
        expect(sheet.locator('.rl-meta')).to_contain_text('Waiting for approval')
        expect(sheet.locator('.rl-num')).to_have_text(['Minimum11hours between duties', 'Maximum—no upper limit', 'Target—aims for the minimum'])
        expect(sheet.locator('.rl-health-slot')).to_have_count(1)   # waiting rules are not in the draft's map
        page.keyboard.press('Escape')
        sheet = open_rule(page, 'Residents duty cap')
        expect(sheet.locator('.rl-sentence')).to_contain_text('Applies only to the people and programmes listed below.')
        expect(sheet.locator('.rl-exc strong')).to_have_text('Family medicine residency')
        expect(sheet.locator('.rl-exc small')).to_have_text('Programme · maximum 4 · from 1 Oct 2026')
        page.keyboard.press('Escape')
        sheet = open_rule(page, 'Weekly duties')
        expect(sheet.locator('.rl-tick')).to_have_count(3)
        expect(sheet.locator('.rl-num')).to_have_text(['Minimum2duties each week', 'Maximum6duties each week', 'Target4duties each week · medium strength'])
        expect(sheet.locator('.rule-facts dt')).to_have_text(['Effective', 'Exception requests', 'Checked against', 'Counts', 'Period'])
    finally:
        context.close()


@pytest.mark.parametrize('role', ['admin', 'approver'])
def test_rule_sheet_actions_follow_the_role(browser, live_server, h, role):
    rules(h)
    context, page = sign_in(browser, live_server, role, {'width': 390, 'height': 844})
    try:
        navigate(page, 'rules')
        sheet = open_rule(page, 'Daily hours')
        foot = sheet.locator('.rl-sheet-foot')
        if role == 'approver':
            expect(foot.get_by_role('button')).to_have_text(['Preview impact', 'Retire rule'])
            expect(sheet.get_by_role('button', name='New line')).to_have_count(0)
            expect(sheet.locator('.rl-exc button')).to_have_count(0)
            expect(sheet.get_by_role('button', name='Edit')).to_have_count(0)
        else:
            expect(foot.get_by_role('button', name='Change…')).to_be_visible()
            expect(sheet.locator('.rl-health-slot')).to_have_count(1)   # an admin does not read rotas: no draft section
        assert page.evaluate('() => document.scrollingElement.scrollWidth <= innerWidth')
        page.keyboard.press('Escape')
        if role == 'approver':
            row = page.locator('.rule-item').filter(has_text='Rest between duties')
            row.get_by_role('button', name='Review', exact=True).click()
            expect(page.get_by_role('dialog').last).to_be_visible()
    finally:
        context.close()


def test_change_a_rule_in_four_steps_with_exceptions_then_approve(browser, live_server, h):
    _, hours, _, _ = rules(h)
    a, b, c = h.seed['people']
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/directory')['people']}
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 390, 'height': 844})
    try:
        navigate(page, 'rules')
        sheet = open_rule(page, 'Daily hours')
        sheet.get_by_role('button', name=f'Edit line for {names[b]}').click()
        dialog = page.get_by_role('dialog', name='Change Daily hours')
        expect(dialog.locator('.stepper .step-link')).to_have_text(['1Who & when', '2Numbers', '3Lines', '4Review & propose'])
        row = dialog.locator('.rl-exc.open')
        expect(row.get_by_label('Maximum')).to_have_value('8')
        expect(row.get_by_label('Until (included)')).to_have_value('Mon 30 Nov 2026')
        if SHOTS:
            page.screenshot(path=f'{SHOTS}/rules-change.png')
        row.get_by_label('Maximum').fill('9')
        row.get_by_label('Until (included)').fill('')
        row.get_by_role('button', name='Save line').click()
        expect(dialog.locator('.rl-exc small').first).to_have_text('maximum 9')
        # Cancel leaves a row as it was; the trash button takes it out.
        dialog.get_by_role('button', name=f'Edit line for {names[c]}').click()
        dialog.locator('.rl-exc.open').get_by_label('Maximum').fill('1')
        dialog.locator('.rl-exc.open').get_by_role('button', name='Cancel').click()
        expect(dialog.locator('.rl-exc small').last).to_have_text('maximum 7')
        dialog.get_by_role('button', name=f'Remove line for {names[c]}').click()
        expect(dialog.locator('.rl-exc')).to_have_count(1)
        # A new person, with dates; "Same" (empty) keeps the rule's number.
        dialog.get_by_role('button', name='Add a person').click()
        expect(dialog.get_by_role('button', name='Add a programme')).to_have_count(0)   # people only: the rule is for everyone it matches
        new = dialog.locator('.rl-exc.open')
        new.get_by_label('Person').select_option(a)
        expect(new.get_by_label('Maximum')).to_have_attribute('placeholder', 'Same')
        new.get_by_label('From').fill('2026-10-10')
        new.get_by_label('Until (included)').fill('2026-10-20')
        new.get_by_role('button', name='Save line').click()
        expect(dialog.locator('.rl-exc small').last).to_have_text('the rule’s numbers · 10 Oct 2026 – 20 Oct 2026')
        dialog.get_by_role('button', name='Propose change').click()
        expect(dialog).to_have_count(0)
    finally:
        context.close()
    proposed = next(p for p in h.get('approver', '/api/policies?status=proposed')['policies'] if p['name'] == 'Daily hours')
    assert proposed['definition']['replaces_id'] == hours['id']
    assert [(r.get('person_id'), r.get('amount'), r.get('starts_on'), r.get('ends_on')) for r in proposed['definition']['per_person']] == [
        (b, 9, None, None), (a, None, '2026-10-10', '2026-10-21')]
    h.command('coordinator', 'policy.activate', {'reason': 'Same person'}, proposed['id'], proposed['revision'], status=403)  # a different person approves
    activate(h, proposed)
    assert next(p for p in h.get('approver', '/api/policies?status=active')['policies'] if p['name'] == 'Daily hours')['id'] == proposed['id']


@pytest.mark.parametrize('width', [1440, 390])
def test_who_truncates_its_names_never_the_people_count(browser, live_server, h, width):
    tags = [f'Fictional long cohort tag number {n} for truncation' for n in range(6)]
    activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, {'conditions': tree(line('tags', *tags))}, name='Long who'))
    activate(h, propose(h, {'kind': 'min_rest', 'hours': 10}, name='Lined rest', per_person=[{'person_id': h.seed['people'][0], 'amount': 12}]))
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        navigate(page, 'rules')
        row = page.locator('.rule-item').filter(has_text='Long who')
        expect(row.locator('.rl-count')).to_have_text(re.compile(r' · \d+ (person|people)$'))
        names = row.locator('.rl-who-text')
        expect(names).to_have_attribute('title', ', '.join(tags))     # every name in the tooltip
        expect(names.locator('.sr-only')).to_have_text(', '.join(tags))  # and for screen readers
        fit = row.evaluate("""r => {const who=r.querySelector('.rl-who').getBoundingClientRect(),count=r.querySelector('.rl-count'),c=count.getBoundingClientRect();
          return {inside:c.width>0&&c.left>=who.left-0.5&&c.right<=who.right+0.5,whole:count.scrollWidth<=count.clientWidth+0.5};}""")
        assert fit == {'inside': True, 'whole': True}
        # A line with its own number widens the numbers to the range: "≥ 10–12 h", not the rule's own "≥ 10 h".
        expect(page.locator('.rule-item').filter(has_text='Lined rest').locator('.rule-num')).to_have_text('≥ 10–12 h')
        assert page.evaluate('document.scrollingElement.scrollWidth <= innerWidth'), page.evaluate(
            "[...document.querySelectorAll('body *')].filter(n=>n.getBoundingClientRect().right>innerWidth+1).slice(0,6).map(n=>n.tagName+'.'+n.className+' '+Math.round(n.getBoundingClientRect().right))")
    finally:
        context.close()

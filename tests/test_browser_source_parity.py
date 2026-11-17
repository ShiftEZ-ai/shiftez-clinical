"""Source rota parity in the existing screens (docs/design/source-rota-parity.md, frontend contract handoff): rule
Settings (conditional credit, staffing selection, overtime runs, fairness distribution), a Workload line's counting
period apart from its validity, auto-schedule fill priorities, recurring-session usual people and closures, the
"Usual assignee" condition, and people's rota names. Desktop 1440, light and dark."""
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_browser import add_filter, browser, live_server, navigate  # noqa: F401
from .test_browser_rule_catalogue import flag
from .test_browser_rule_catalogue_v2 import line, reason, themed, v2_rule
from .test_browser_rule_filter_builder import pick

pytestmark = pytest.mark.browser
THEMES = ['light', 'dark']
SHOTS = os.getenv('PARITY_SHOTS')  # a folder: each surface is saved there for a visual review


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f'{name}.png'), full_page=False, animations='disabled')


def entry(h, catalogue_id):
    return next(e for e in h.get('coordinator', '/api/rules/catalogue?version=2&include_hidden=true')['entries'] if e['id'] == catalogue_id)


def category(h, category_id):
    return next(c['name'] for c in h.get('admin', '/api/catalogues')['categories'] if c['id'] == category_id)


def activate(h, catalogue_id):
    """Another person approves the waiting version (independent review)."""
    proposed = entry(h, catalogue_id)['proposed']
    return h.command('approver', 'policy.activate', {'reason': 'Independent review'}, proposed['id'], proposed['revision'])['record']


def propose(page, why):
    page.locator('.topbar').get_by_role('button', name=re.compile(r'^Propose \d+ change')).click()
    reason(page, why, 'Propose')
    expect(page.locator('.rc-waiting')).to_contain_text('waiting for approval')


@pytest.mark.parametrize('theme', THEMES)
def test_staffing_settings_conditional_credit_and_selection_order_save_and_reload(browser, live_server, h, theme):
    flag(h, legacy=False)
    v2_rule(h, 'people_per_session', [line({'min': {'value': 2, 'level': 'mandatory'}})])
    trainee, senior = category(h, h.seed['trainee_category']), category(h, h.seed['category'])
    context, page = themed(browser, live_server, 'coordinator', 1440, theme)
    try:
        navigate(page, 'rules/people_per_session')
        page.get_by_role('button', name='Settings', exact=True).click()
        pop = page.locator('.rc-settings')
        credit = pop.get_by_role('group', name='Counts only alongside others')
        expect(credit).to_contain_text('These people count only when enough others who always count are on the same duty.')
        add_filter(credit)
        pick(page, credit.locator('.cond-line').first, 'Rank', [trainee])
        order = pop.get_by_role('group', name='Most preferred first')
        order.get_by_role('button', name='+ Add next choice').click()
        pick(page, order.locator('.cond-line').first, 'Rank', [senior])
        pop.get_by_label('Choose the group for', exact=True).select_option(label='Each week')
        # Half a pair: the server's check refuses it beside the setting to fix, and the popover stays open.
        pop.get_by_role('button', name='Keep', exact=True).click()
        others_field = pop.locator('.field').filter(has=page.get_by_label('Others needed on the duty', exact=True))
        expect(others_field.locator('.rc-setting-error')).to_contain_text('Conditional credit needs both')
        expect(pop.get_by_label('Others needed on the duty', exact=True)).to_have_attribute('aria-invalid', 'true')
        # Out of range: Keep says so on the field and keeps the popover open.
        others = pop.get_by_label('Others needed on the duty', exact=True)
        others.fill('101')
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(pop).to_be_visible()
        assert not others.evaluate('x => x.checkValidity()')
        others.fill('1')
        shot(page, f'staffing-settings-{theme}')
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(pop).to_have_count(0)
        propose(page, 'Trainees count beside a senior; seniors first')
        rule = entry(h, 'people_per_session')['proposed']['definition']['rule']
        cover = rule['settings']['minimum_cover']
        assert cover['credit_conditions']['conditions'][0]['values'] == [h.seed['trainee_category']]
        assert cover['credit_minimum_others'] == 1 and cover['selection_period'] == 'week'
        assert cover['selection_order'][0]['conditions'][0]['values'] == [h.seed['category']]
        activate(h, 'people_per_session')
        page.reload()
        page.get_by_role('button', name='Settings', exact=True).click()
        pop = page.locator('.rc-settings')
        expect(pop.get_by_role('group', name='Counts only alongside others')).to_contain_text(trainee)
        expect(pop.get_by_role('group', name='Most preferred first')).to_contain_text(senior)
        expect(pop.get_by_label('Choose the group for', exact=True)).to_have_value('week')
        expect(pop.get_by_label('Others needed on the duty', exact=True)).to_have_value('1')
    finally:
        context.close()


@pytest.mark.parametrize('theme', THEMES)
def test_line_settings_same_as_rule_none_or_own_round_trip(browser, live_server, h, theme):
    """Each line setting says "Same as rule" (absent), "None for this line" (null / []) or "Own value", and that is what
    is saved: none -> own -> same -> none, and same -> own, each through Keep -> propose -> approve -> reload."""
    flag(h, legacy=False)
    trainee, senior = category(h, h.seed['trainee_category']), category(h, h.seed['category'])
    tree = {'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [h.seed['trainee_category']]}]}
    empty = {'credit_conditions': None, 'credit_minimum_others': 0, 'selection_order': []}
    rule = v2_rule(h, 'people_per_session', [line({'min': {'value': 2, 'level': 'mandatory'}}, settings={'minimum_cover': empty})],
                   settings={'minimum_cover': {'credit_conditions': tree, 'credit_minimum_others': 1, 'selection_order': [tree]}})
    line_id = rule['definition']['rule']['lines'][0]['id']
    assert rule['definition']['rule']['lines'][0]['settings']['minimum_cover'] == empty
    context, page = themed(browser, live_server, 'coordinator', 1440, theme)

    def open_settings(while_open=False):
        if while_open:
            # A proposal closes the editor. Open the line from the rule before following the same-rule link.
            navigate(page, 'rules/people_per_session')
            page.reload()
            page.locator(f'#main tr[data-row="{line_id}"] [data-col="who"]').click()
            expect(page.locator('.rc-line-panel')).to_be_visible()
        navigate(page, f'rules/people_per_session/{line_id}')
        # The link keeps its line and opens it, also when followed from an open panel of the same rule.
        expect(page).to_have_url(re.compile(rf'#rules/people_per_session/{line_id}$'))
        expect(page.locator('.rc-line-panel').get_by_role('button', name='Edit…', exact=True)).to_be_visible()
        page.reload()
        page.locator('.rc-line-panel').get_by_role('button', name='Edit…', exact=True).click()
        return page.locator('.rc-settings')

    def choice(pop, label):
        return pop.get_by_role('radiogroup', name=label, exact=True)

    def shows(pop, label, text):
        expect(choice(pop, label).get_by_role('radio', name=text, exact=True)).to_have_attribute('aria-checked', 'true')

    def save(pop, why):
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(pop).to_have_count(0)
        panel = page.locator('.rc-line-panel')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        propose(page, why)
        kept = entry(h, 'people_per_session')['proposed']['definition']['rule']['lines'][0]['settings'].get('minimum_cover', {})
        activate(h, 'people_per_session')
        return kept

    try:
        # The stored explicit empties read as "None for this line"; the 0 as the line's own value.
        pop = open_settings()
        shows(pop, 'Counts only alongside others', 'None for this line')
        shows(pop, 'Most preferred first', 'None for this line')
        shows(pop, 'Others needed on the duty', 'Own value')
        expect(pop.get_by_role('group', name='Counts only alongside others')).to_be_hidden()
        shot(page, f'line-settings-tristate-{theme}')
        # none -> own; none -> same; clearing an own value goes back to "Same as rule", never to "none".
        choice(pop, 'Counts only alongside others').get_by_role('radio', name='Own value', exact=True).click()
        credit = pop.get_by_role('group', name='Counts only alongside others')
        add_filter(credit)
        pick(page, credit.locator('.cond-line').first, 'Rank', [trainee])
        shows(pop, 'Counts only alongside others', 'Own value')
        choice(pop, 'Most preferred first').get_by_role('radio', name='Same as rule', exact=True).click()
        pop.get_by_role('spinbutton', name='Others needed on the duty', exact=True).fill('')
        shows(pop, 'Others needed on the duty', 'Same as rule')
        kept = save(pop, 'This line: trainees count beside others; the rule’s order')
        assert kept == {'credit_conditions': tree}

        # Reloaded: own and same as rule. own -> same; same -> none.
        pop = open_settings(while_open=True)
        shows(pop, 'Counts only alongside others', 'Own value')
        expect(pop.get_by_role('group', name='Counts only alongside others')).to_contain_text(trainee)
        shows(pop, 'Most preferred first', 'Same as rule')
        shows(pop, 'Others needed on the duty', 'Same as rule')
        choice(pop, 'Counts only alongside others').get_by_role('radio', name='Same as rule', exact=True).click()
        choice(pop, 'Most preferred first').get_by_role('radio', name='None for this line', exact=True).click()
        kept = save(pop, 'This line: the rule’s credit; no preferred order')
        assert kept == {'selection_order': []}

        # Reloaded: same -> none; none -> own; same -> own 0.
        pop = open_settings()
        shows(pop, 'Counts only alongside others', 'Same as rule')
        shows(pop, 'Most preferred first', 'None for this line')
        choice(pop, 'Counts only alongside others').get_by_role('radio', name='None for this line', exact=True).click()
        choice(pop, 'Others needed on the duty').get_by_role('radio', name='Own value', exact=True).click()
        pop.get_by_role('spinbutton', name='Others needed on the duty', exact=True).fill('0')
        choice(pop, 'Most preferred first').get_by_role('radio', name='Own value', exact=True).click()
        order = pop.get_by_role('group', name='Most preferred first')
        order.get_by_role('button', name='+ Add next choice').click()
        pick(page, order.locator('.cond-line').first, 'Rank', [senior])
        kept = save(pop, 'This line: no conditional credit; seniors first')
        assert kept['credit_conditions'] is None and kept['credit_minimum_others'] == 0
        assert kept['selection_order'][0]['conditions'][0]['values'] == [h.seed['category']]

        # Reload and Keep untouched: the same three states, saved unchanged.
        pop = open_settings()
        shows(pop, 'Counts only alongside others', 'None for this line')
        shows(pop, 'Others needed on the duty', 'Own value')
        shows(pop, 'Most preferred first', 'Own value')
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(pop).to_have_count(0)
        panel = page.locator('.rc-line-panel')
        panel.get_by_label('Minimum', exact=True).fill('3')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        propose(page, 'Three people; this line’s settings unchanged')
        again = entry(h, 'people_per_session')['proposed']['definition']['rule']['lines'][0]
        assert again['numbers']['min']['value'] == 3 and again['settings']['minimum_cover'] == kept
    finally:
        context.close()


@pytest.mark.parametrize('theme', THEMES)
def test_days_in_a_row_overtime_extension_saves_and_validates(browser, live_server, h, theme):
    flag(h, legacy=False)
    v2_rule(h, 'days_in_a_row', [line({'max': {'value': 6, 'level': 'mandatory'}})])
    context, page = themed(browser, live_server, 'coordinator', 1440, theme)
    try:
        navigate(page, 'rules/days_in_a_row')
        page.get_by_role('button', name='Settings', exact=True).click()
        pop = page.locator('.rc-settings')
        expect(pop.get_by_role('checkbox', name='Overtime days count toward the run')).to_be_checked()
        extra = pop.get_by_label('Extra days with overtime', exact=True)
        extra.fill('40')
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(pop).to_be_visible()  # above the schema's 31: the field says so
        extra.fill('2')
        shot(page, f'days-in-a-row-settings-{theme}')
        pop.get_by_role('button', name='Keep', exact=True).click()
        propose(page, 'Overtime may extend a run by two days')
        settings = entry(h, 'days_in_a_row')['proposed']['definition']['rule']['settings']
        assert settings['max_consecutive_days']['overtime_extension_days'] == 2
        activate(h, 'days_in_a_row')
        page.reload()
        page.get_by_role('button', name='Settings', exact=True).click()
        expect(page.locator('.rc-settings').get_by_label('Extra days with overtime', exact=True)).to_have_value('2')
    finally:
        context.close()


@pytest.mark.parametrize('theme', THEMES)
def test_counting_period_is_apart_from_validity_and_its_early_end_needs_attention(browser, live_server, h, theme):
    flag(h, legacy=False)
    rule = v2_rule(h, 'workload_limit', [line({'max': {'value': 10, 'level': 'mandatory'}}, measure='duties', starts_on='2026-09-01',
                                              period={'kind': 'fixed', 'days': 14, 'starts_on': '2026-09-01', 'ends_on': '2026-12-01'})])
    first = rule['definition']['rule']['lines'][0]['id']
    context, page = themed(browser, live_server, 'coordinator', 1440, theme)
    try:
        attention = h.get('coordinator', '/api/rules/catalogue?version=2')['attention']
        assert [(x['kind'], x['facts']) for x in attention if x['kind'] == 'counting_ends'] == [
            ('counting_ends', {'counting_end': '2026-12-01', 'validity_end': None})], attention
        # The rules list: a counting end before an open validity is a Needs attention issue, in words.
        navigate(page, 'rules')
        page.locator('.rt-att').click()  # the rule's "1 needs attention" opens its issues
        expect(page.locator('#main')).to_contain_text('Counting ends early')
        expect(page.locator('#main')).to_contain_text('Counting ends 30 Nov 2026, before the line stops applying (no end)')
        navigate(page, f'rules/workload_limit/{first}')
        panel = page.locator('.rc-line-panel')
        warn = panel.locator('.rc-count-end')
        expect(warn).to_contain_text('Counting ends 30 Nov 2026')
        counting = panel.get_by_role('group', name='Counting dates')
        expect(counting.get_by_label('Last day (included)', exact=True)).to_have_value(re.compile('30 Nov 2026'))
        panel.get_by_label('Blocks start on', exact=True).fill('2026-09-07')
        # Part blocks are prorated by working days automatically: no switch for it any more.
        expect(panel.get_by_role('checkbox', name='Part blocks get a matching share')).to_have_count(0)
        panel.get_by_role('checkbox', name='Repeats while the line is valid').check()
        counting.get_by_label('Last day (included)', exact=True).fill('')
        counting.get_by_label('Last day (included)', exact=True).press('Tab')
        expect(warn).to_be_hidden()
        # The line's own Valid dates are untouched.
        expect(panel.get_by_role('group', name='Valid').get_by_label('From', exact=True)).to_have_value(re.compile('1 Sep 2026'))
        shot(page, f'counting-period-{theme}')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        propose(page, 'Fortnightly blocks keep counting while the line applies')
        saved = entry(h, 'workload_limit')['proposed']['definition']['rule']['lines'][0]
        quota = saved['settings']['block_quota']
        assert quota['window_anchor_on'] == '2026-09-07' and not quota.get('prorate_partial_windows') and quota['repeat'] is True, quota
        assert quota.get('ends_on') is None and saved.get('ends_on') is None, saved
        activate(h, 'workload_limit')
        navigate(page, f'rules/workload_limit/{first}')  # closed by hand, the address became the rule's: reopen the line
        page.reload()
        panel = page.locator('.rc-line-panel')
        expect(panel.get_by_label('Blocks start on', exact=True)).to_have_value(re.compile('7 Sep 2026'))
        expect(panel.get_by_role('checkbox', name='Repeats while the line is valid')).to_be_checked()
        expect(panel.locator('.rc-count-end')).to_be_hidden()
    finally:
        context.close()


@pytest.mark.parametrize('theme', THEMES)
def test_fair_share_distribution_share_with_its_duties_and_weight(browser, live_server, h, theme):
    flag(h, legacy=False)
    v2_rule(h, 'fair_share', [line({'apply': {'value': None, 'level': 'medium'}})], settings={'balance_goal': {'metric': 'duties'}})
    service = next(s['name'] for s in h.get('admin', '/api/catalogues')['services'] if s['id'] == h.seed['services'][0])
    context, page = themed(browser, live_server, 'coordinator', 1440, theme)
    try:
        navigate(page, 'rules/fair_share')
        page.get_by_role('button', name='Settings', exact=True).click()
        pop = page.locator('.rc-settings')
        pop.get_by_label('Share out', exact=True).select_option(label='A share of each person’s duties')
        within = pop.get_by_role('group', name='Duties in the share')
        add_filter(within)
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(within.locator('.rc-setting-error')).to_be_visible()  # a share needs its duties: said beside them
        pick(page, within.locator('.cond-line').first, 'Clinic', [service])
        pop.get_by_label('Share to aim for (0–1)', exact=True).fill('0.6')
        pop.get_by_label('Weight', exact=True).fill('0')
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(pop).to_be_visible()  # a weight must be more than 0
        pop.get_by_label('Weight', exact=True).fill('2')
        pop.get_by_role('checkbox', name='Also used by the basic scheduler').check()
        shot(page, f'fair-share-settings-{theme}')
        pop.get_by_role('button', name='Keep', exact=True).click()
        propose(page, 'Share clinic duties')
        goal = entry(h, 'fair_share')['proposed']['definition']['rule']['settings']['balance_goal']
        assert goal['distribution'] == 'share' and goal['target_share'] == 0.6 and goal['weight'] == 2 and goal['optimize_greedy'] is True
        assert goal['within']['conditions'][0] == {'field': 'service_ids', 'op': 'any', 'values': [h.seed['services'][0]]}
    finally:
        context.close()


@pytest.mark.parametrize('theme', THEMES)
def test_usual_assignee_condition_scopes_a_line_to_sessions_a_person_runs(browser, live_server, h, theme):
    flag(h, legacy=False)
    rule = v2_rule(h, 'people_per_session', [line({'min': {'value': 1, 'level': 'mandatory'}})])
    first = rule['definition']['rule']['lines'][0]['id']
    usual = person_name(h, h.seed['people'][0])
    context, page = themed(browser, live_server, 'coordinator', 1440, theme)
    try:
        navigate(page, f'rules/people_per_session/{first}')
        panel = page.locator('.rc-line-panel')
        where = panel.locator('.rc-lp-section').filter(has=page.get_by_role('heading', name=re.compile('^Where')))
        add_filter(where)
        pick(page, where.locator('.cond-line').first, 'Usual assignee', [usual])
        shot(page, f'usual-assignee-condition-{theme}')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        propose(page, 'Sessions this person runs')
        saved = entry(h, 'people_per_session')['proposed']['definition']['rule']['lines'][0]
        assert saved['where']['conditions'][0] == {'field': 'usual_assignee_ids', 'op': 'any', 'values': [h.seed['people'][0]]}
    finally:
        context.close()


@pytest.mark.parametrize('theme', THEMES)
def test_fill_priorities_and_pair_budget_save_reload_and_validate(browser, live_server, h, theme):
    context, page = themed(browser, live_server, 'admin', 1440, theme)
    try:
        navigate(page, 'configuration/auto_schedule')
        fill = page.get_by_label('Fill priorities', exact=True)
        expect(fill).to_have_value('weighted')  # today's behaviour by default
        fill.select_option(label='Higher priorities filled first')
        page.get_by_role('tab', name='Advanced', exact=True).click()
        budget = page.get_by_label('Most pairs a pair limit may weigh', exact=True)
        expect(budget).to_have_value('50000')
        budget.fill('0')
        page.get_by_role('button', name='Save defaults', exact=True).click()
        assert not budget.evaluate('x => x.checkValidity()')
        expect(page.get_by_role('dialog', name='Save auto-schedule defaults')).to_have_count(0)
        budget.fill('80000')
        shot(page, f'auto-schedule-advanced-{theme}')
        page.get_by_role('button', name='Save defaults', exact=True).click()
        reason(page, 'Fill higher priorities first', 'Save defaults')
        expect(page.locator('#toast-tray')).to_contain_text('Auto-schedule defaults saved')
        saved = h.get('admin', '/api/me')['department']['generation_defaults']
        assert saved['allocation_order'] == 'tiered' and saved['pair_limit_max_opportunities'] == 80000, saved
        page.reload()
        expect(page.get_by_label('Fill priorities', exact=True)).to_have_value('tiered')
        page.get_by_role('tab', name='Advanced', exact=True).click()
        expect(page.get_by_label('Most pairs a pair limit may weigh', exact=True)).to_have_value('80000')
    finally:
        context.close()


def person_name(h, person_id):
    return next(p['display_name'] for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == person_id)


RETIRED_CLOSE = 'No longer closes sessions: they stay open when every usual assignee is away.'


def closing_series(h):
    """A recurring session whose one usual person is on leave for the whole of its Monday, materialised in a draft."""
    from .test_series_reconciliation import template, materialise
    source = template(h, days=(0,), definition={'anchor': '2026-10-04', 'days': [{'weekday': 0, 'start_time': '08:00', 'end_time': '12:00'}],
        'service_ids': [h.seed['services'][0]], 'default_person_ids': [h.seed['people'][0]],
        'usual_assignees_fixed': True, 'close_when_all_usual_unavailable': True})
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'recorded_leave',
        'start': '2026-10-05T00:00:00+03:00', 'end': '2026-10-06T00:00:00+03:00', 'private_reason': 'Synthetic leave'})
    return source, materialise(h, source)


@pytest.mark.parametrize('theme', THEMES)
def test_recurring_session_usual_people_options_save_and_validate(browser, live_server, h, theme):
    from .test_series_reconciliation import template
    source = template(h, days=(0,))
    context, page = themed(browser, live_server, 'admin', 1440, theme)
    try:
        navigate(page, 'configuration/recurring_series')
        page.locator('.sw-entry').first.click()
        form = page.get_by_role('dialog', name=re.compile('^Edit recurring session'))
        fixed = form.get_by_role('checkbox', name='Keep the usual assignees on every session')
        expect(fixed).not_to_be_checked()
        # DL-009 (D38: sessions stay open on absence): the retired close switch is never offered, nor its read-only line
        # on a series that never had it.
        expect(form.get_by_role('checkbox', name='Close a session when every usual assignee is away')).to_have_count(0)
        expect(form.get_by_text(RETIRED_CLOSE)).to_have_count(0)
        # Without usual assignees the option means nothing: it cannot be chosen.
        expect(fixed).to_be_disabled()
        form.get_by_role('group', name='Usual assignees').get_by_role('checkbox', name=person_name(h, h.seed['people'][0]), exact=True).check()
        fixed.check()
        shot(page, f'recurring-usual-options-{theme}')
        form.get_by_label('Change reason', exact=True).fill('Usual clinician runs it or it closes')
        form.get_by_role('button', name=re.compile('^Save')).click()
        expect(form).to_have_count(0, timeout=15000)
        saved = next(r for r in h.get('admin', '/api/catalogues')['recurring_series'] if r['id'] == source['id'])
        assert saved['definition']['usual_assignees_fixed'] is True and not saved['definition'].get('close_when_all_usual_unavailable')
        # rc.16: the switch shows in the grid and the table, in words with an icon (never colour alone).
        entry = page.locator('.sw-entry').first
        expect(entry.locator('.sr-switch')).to_have_text(['Kept'])
        expect(entry).to_have_attribute('aria-label', re.compile(r'keeps usual people\. Edit$'))
        from .test_browser import toolbar, close_popover
        main = page.locator('#main')
        toolbar(main, 'View settings').get_by_role('radio', name='Table', exact=True).click()
        close_popover(page)
        expect(main.locator('td.sr-people .sr-switch')).to_have_text(['Keeps usual people'])
        shot(page, f'recurring-usual-switches-{theme}')
    finally:
        context.close()


def test_a_stored_series_with_the_retired_close_switch_says_so_read_only_and_keeps_it(browser, live_server, h):
    """DL-009: a series stored with close_when_all_usual_unavailable on shows one read-only line (no control), and a save
    keeps the stored value (the server reads and ignores it: sessions stay open, D38)."""
    source, _ = closing_series(h)
    context, page = themed(browser, live_server, 'admin', 1440, 'light')
    try:
        navigate(page, 'configuration/recurring_series')
        page.locator('.sw-entry').first.click()
        form = page.get_by_role('dialog', name=re.compile('^Edit recurring session'))
        expect(form.get_by_text(RETIRED_CLOSE)).to_be_visible()
        expect(form.get_by_role('checkbox', name='Close a session when every usual assignee is away')).to_have_count(0)
        form.get_by_label('Description', exact=True).fill('Kept as stored')
        form.get_by_label('Change reason', exact=True).fill('A routine edit keeps the stored value')
        form.get_by_role('button', name=re.compile('^Save')).click()
        expect(form).to_have_count(0, timeout=15000)
        saved = next(r for r in h.get('admin', '/api/catalogues')['recurring_series'] if r['id'] == source['id'])
        assert saved['definition']['close_when_all_usual_unavailable'] is True
        # Still findable, said as having no effect (DL-009), never as a closure.
        expect(page.locator('.sw-entry').first.locator('.sr-switch')).to_contain_text(['No effect'])
    finally:
        context.close()


@pytest.mark.parametrize('theme', THEMES)
def test_planner_keeps_a_session_open_when_its_usual_people_are_away(browser, live_server, h, theme):
    # D38 (b0fb0ad37): an absence never closes a session, whatever the series' retained closure setting says; the
    # usual person is left out and the place stays open (Replace first). Closing is the coordinator's explicit action.
    _, workspace = closing_series(h)
    [session] = h.detail(workspace)['shifts']
    assert not session['closed'] and not session['cancelled'] and not session['participants']
    assert [(x['person_id'], x['code']) for x in session['template_skipped']] == [(h.seed['people'][0], 'UNAVAILABLE')]
    context, page = themed(browser, live_server, 'coordinator', 1440, theme)
    try:
        navigate(page, f'schedule/week/2026-10-05?edit={workspace["id"]}')
        expect(page.locator(f'button.pchip.gap[data-shift="{session["id"]}"]')).to_have_count(1)  # the grid's open place
        expect(page.locator('.pchip.closed')).to_have_count(0)
        shot(page, f'planner-closed-session-{theme}')
        # An open session is work: the footer's count keeps it (as the server's workload does).
        expect(page.locator('.footer-total').first).to_have_text('1 session in view')
    finally:
        context.close()


@pytest.mark.parametrize('theme', THEMES)
def test_person_fairness_shows_each_week_of_a_spread_goal(browser, live_server, h, theme):
    from .test_browser_person_profile import open_person, person, related, section
    from .test_browser_person_record import tab, W
    from .test_browser_rule_catalogue import catalogue_rule
    goal = catalogue_rule(h, 'fair_share', [line({'apply': {'value': None, 'level': 'medium'}})],
                          settings={'balance_goal': {'metric': 'duties', 'period': 'rota', 'distribution': 'period_spread'}})
    related(h)
    p = person(h)
    served = next(g for g in h.get('coordinator', f"/api/people/{p['id']}/fairness")['goals'] if g['name'] == goal['name'])
    weeks = [b for b in served['people'][0]['buckets'] if b['bucket']]
    assert weeks, served
    context, page = themed(browser, live_server, 'coordinator', 1440, theme)
    try:
        shown = tab(open_person(page, p), W)
        shown.get_by_label('Period', exact=True).select_option(label='Requests rota')
        expect(shown.locator('.loading')).to_have_count(0)
        parts = section(shown, 'Balance goals and targets').locator('.pr-line.pr-part')
        expect(parts).to_have_count(len(weeks))
        expect(parts.first).to_contain_text('Week of ')
        goal_row = section(shown, 'Balance goals and targets').locator('.pr-line:not(.pr-part)').filter(has_text=goal['name'])
        expect(goal_row).to_contain_text(re.compile(r'\d+ dut(y|ies) · fair share'))  # the server's unit
        expect(goal_row).not_to_contain_text('distribution')
        shot(page, f'person-fairness-weeks-{theme}')
    finally:
        context.close()


@pytest.mark.parametrize('theme', THEMES)
def test_person_rota_name_and_aliases_save_and_are_searchable(browser, live_server, h, theme):
    person = h.configure('people', {'display_name': 'Fictional alias person'})['record']
    context, page = themed(browser, live_server, 'admin', 1440, theme)
    try:
        navigate(page, f'people?open={person["id"]}')
        page.get_by_role('tabpanel', name='Overview').get_by_role('button', name='Edit ' + person['display_name'], exact=True).click()
        dialog = page.get_by_role('dialog', name='Edit person')
        dialog.get_by_label('Name on the rota', exact=True).fill('Dr F. Alias (Paeds)')
        aliases = dialog.get_by_label('Other names on rotas', exact=True)
        aliases.fill('\n'.join(f'Alias {n}' for n in range(31)))
        assert aliases.evaluate('x => x.validationMessage') == 'Up to 30 other names.'
        aliases.fill('FALIAS, consultant\nFictional A.')
        shot(page, f'person-rota-names-{theme}')
        dialog.get_by_role('button', name=re.compile('^Save')).click()
        expect(dialog).to_have_count(0, timeout=15000)
        saved = next(r for r in h.get('admin', '/api/catalogues')['people'] if r['id'] == person['id'])
        assert saved['rota_name'] == 'Dr F. Alias (Paeds)' and saved['rota_aliases'] == ['FALIAS, consultant', 'Fictional A.']
        navigate(page, 'people')
        page.get_by_role('searchbox').first.fill('FALIAS')
        expect(page.locator('#main .dt tbody tr[data-row]')).to_have_count(1)
        expect(page.locator('#main .dt tbody tr[data-row]')).to_contain_text(person['display_name'])
    finally:
        context.close()

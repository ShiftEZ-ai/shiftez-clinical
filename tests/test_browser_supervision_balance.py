"""Supervision as a Fair share measure (SUP-BAL): an admin adds a line for a rank counting trainee sessions supervised
per month (settings that do not apply stay hidden; Chosen dates asks for its dates); it saves, reloads and edits to a
custom period, and the Fairness view shows the supervisors' trainee sessions with the real share basis."""
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect
from sqlalchemy import delete

from apps.api import models as m
from apps.api.db import transaction

from .test_browser import browser, live_server, navigate  # noqa: F401
from .test_browser_rule_catalogue import flag
from .test_browser_rule_catalogue_v2 import reason, themed
from .test_browser_fairness_dashboard import october, section
from .test_browser_fair_share_dials import fair_share_rule

pytestmark = pytest.mark.browser
DAY = '2026-10-{:02d}T{}:00+03:00'


def supervised_october(h):
    """Published October clinics: Jordan supervises Alex four times; Taylor, on two of those sessions, never does (one consultant carries it all). Alex,
    the trainee, holds no supervising skill."""
    trainee, jordan, taylor = h.seed['people']
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(delete(m.CapabilityGrant).where(m.CapabilityGrant.person_id == trainee,
                                                   m.CapabilityGrant.capability_id == h.seed['supervisor_capability']))
    typ = h.configure('assignment_types', {'name': 'Supervised clinic', 'starts_on': '2020-01-01', 'definition': {
        'supervision': {'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
                        'trainees_per_supervisor': 1}}})['record']['id']
    w = h.workspace('October supervision', '2026-10-04', '2026-10-11')
    for day, supervisor in ((4, jordan), (5, jordan), (6, jordan), (7, jordan)):
        start, end = DAY.format(day, '08:00'), DAY.format(day, '12:00')
        w = h.shift(w, start=start, end=end, typ=typ, people=[
            {'person_id': trainee, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': supervisor},
            {'person_id': supervisor, 'start': start, 'end': end, 'participation': 'supervisor'},
            # Taylor works two of these sessions and could supervise (a share by supervisable sessions), but never does.
            *([{'person_id': taylor, 'start': start, 'end': end, 'participation': 'supervisor'}] if day in (4, 5) else [])])['workspace']
    h.publish(w)


def shot(page, name, width, theme):
    folder = os.getenv('SUP_BAL_SHOTS')
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=f"{folder}/{name}-{width}-{theme}.png", animations='disabled')


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_supervision_fair_share_line_saves_edits_and_shows_in_fairness(browser, live_server, h, width, theme):
    flag(h)
    supervised_october(h)
    rank = next(c['name'] for c in h.get('admin', '/api/catalogues')['categories'] if c['id'] == h.seed['category'])
    context, page = themed(browser, live_server, 'admin', width, theme)
    try:
        navigate(page, 'rules/fair_share')
        main = page.locator('#main')
        main.get_by_role('button', name='New line: a person, rank, clinic or #tag').click()
        page.get_by_role('combobox', name='Search New line').fill(rank)
        page.get_by_role('option', name=re.compile('^' + rank)).first.click()
        panel = page.locator('.rc-line-panel')
        panel.get_by_role('button', name='Edit…', exact=True).click()
        pop = page.locator('.rc-settings')
        for label in ('What to count', 'Period'):
            pop.get_by_role('radiogroup', name=label, exact=True).get_by_role('radio', name='Own value', exact=True).click()
        # Partners and pairing belong to duties shared with a partner: never offered for supervision.
        expect(pop.get_by_role('radiogroup', name='Partners', exact=True)).to_be_hidden()
        pop.get_by_role('combobox', name='What to count', exact=True).select_option(label='Trainee sessions supervised')
        expect(pop.get_by_role('radiogroup', name='Partners', exact=True)).to_be_hidden()
        expect(pop.get_by_role('radiogroup', name='A pair counts when', exact=True)).to_be_hidden()
        # Choices the contract refuses for this measure are not offered: overtime offered, a pair minimum, a duties share.
        for label in ('Share by', 'Aim', 'Share out'):
            pop.get_by_role('radiogroup', name=label, exact=True).get_by_role('radio', name='Own value', exact=True).click()
        for label, value in (('Share by', 'offered'), ('Aim', 'min_per_pair'), ('Aim', 'workload_spread'), ('Share out', 'share'), ('Share out', 'period_spread')):
            expect(pop.get_by_role('combobox', name=label, exact=True).locator(f'option[value="{value}"]')).to_be_disabled()
        for label in ('Share by', 'Aim', 'Share out'):
            pop.get_by_role('radiogroup', name=label, exact=True).get_by_role('radio', name='Same as rule', exact=True).click()
        expect(pop.get_by_role('radiogroup', name='Share to aim for (0–1)', exact=True)).to_be_hidden()
        expect(pop.get_by_role('radiogroup', name='Duties in the share', exact=True)).to_be_hidden()
        # Chosen dates asks for its dates (the line's own: the rule has none); Keep waits for them.
        period = pop.get_by_role('combobox', name='Period', exact=True)
        period.select_option(label='Chosen dates')
        start = pop.get_by_role('textbox', name='From', exact=True)
        expect(start).to_be_visible()
        expect(pop.get_by_role('radiogroup', name='Days back', exact=True)).to_be_hidden()
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(start).to_be_focused()
        expect(pop).to_be_visible()
        period.select_option(label='Calendar month')
        expect(start).to_be_hidden()
        expect(pop.get_by_role('radiogroup', name='Days back', exact=True)).to_be_hidden()
        shot(page, 'supervision-line-settings', width, theme)
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(pop).to_have_count(0)
        expect(panel.locator('.rc-lp-settings')).to_contain_text('Trainee sessions supervised')
        expect(panel.locator('.rc-lp-settings')).not_to_contain_text('From')
        panel.get_by_role('checkbox', name='Applies', exact=True).check()
        expect(panel).to_contain_text('fair share applies to trainee sessions supervised over the calendar month')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(main.locator('[data-line]').filter(has_text=rank)).to_contain_text('Trainee sessions supervised')
        expect(main.locator('[data-line]').filter(has_text='Everyone')).to_contain_text('Workload')
        page.locator('.topbar').get_by_role('button', name=re.compile(r'^Propose \d+ change')).click()
        dialog = page.get_by_role('dialog')
        tick = dialog.get_by_label('I have read which lines give way and that is intended', exact=True)
        if tick.count():
            tick.check()
        reason(page, 'Share supervision fairly between consultants', 'Propose')
        expect(main.locator('.rc-waiting')).to_contain_text('waiting for approval')
        proposed = next(e for e in h.get('approver', '/api/rules/catalogue?include_hidden=true')['entries'] if e['id'] == 'fair_share')['proposed']
        h.command('approver', 'policy.activate', {'reason': 'Independent review'}, proposed['id'], proposed['revision'])
        saved = next(l for l in proposed['definition']['rule']['lines'] if l.get('settings', {}).get('balance_goal', {}).get('metric') == 'supervision')
        assert saved['settings']['balance_goal']['period'] == 'month'
        assert not {'starts_on', 'ends_on', 'period_days'} & set(saved['settings']['balance_goal'])
        assert saved['who']['conditions'][0]['values'] == [h.seed['category']]

        # Reload: the saved line reads in words, and edits like any other line.
        page.reload()
        expect(page.locator('#main [data-line]').filter(has_text=rank)).to_contain_text('Trainee sessions supervised')
        navigate(page, f"rules/fair_share/{saved['id']}")
        panel = page.locator('.rc-line-panel')
        expect(panel.locator('.rc-lp-settings')).to_contain_text('Trainee sessions supervised')
        panel.get_by_role('button', name='Edit…', exact=True).click()
        pop = page.locator('.rc-settings')
        expect(pop.get_by_role('combobox', name='What to count', exact=True)).to_have_value('supervision')
        expect(pop.get_by_role('radiogroup', name='Partners', exact=True)).to_be_hidden()
        share = pop.get_by_role('radiogroup', name='Share by', exact=True)
        share.get_by_role('radio', name='Own value', exact=True).click()
        pop.get_by_role('combobox', name='Share by', exact=True).select_option(label='Equally')
        # An academic-year style window: Chosen dates with its first and last day.
        pop.get_by_role('combobox', name='Period', exact=True).select_option(label='Chosen dates')
        pop.get_by_role('textbox', name='From', exact=True).fill('2026-10-01')
        pop.get_by_role('textbox', name='Until (included)', exact=True).fill('2026-10-31')
        pop.get_by_role('textbox', name='Until (included)', exact=True).blur()
        shot(page, 'supervision-line-custom-dates', width, theme)
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(pop).to_have_count(0)
        expect(panel.locator('.rc-lp-settings')).to_contain_text('Equally')
        expect(panel.locator('.rc-lp-settings')).to_contain_text('31 Oct 2026')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        shot(page, 'supervision-line-edited', width, theme)
        panel.get_by_role('button', name='Close panel', exact=True).click()
        page.locator('.topbar').get_by_role('button', name='Propose 1 change', exact=True).click()
        reason(page, 'Supervision over October, shared equally', 'Propose')
        expect(page.locator('#main .rc-waiting')).to_contain_text('waiting for approval')
        proposed = next(e for e in h.get('approver', '/api/rules/catalogue?include_hidden=true')['entries'] if e['id'] == 'fair_share')['proposed']
        goal = next(l for l in proposed['definition']['rule']['lines'] if l['id'] == saved['id'])['settings']['balance_goal']
        assert (goal['period'], goal['starts_on'], goal['ends_on'], goal['share_by']) == ('custom', '2026-10-01', '2026-11-01', 'equal')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')

        # Fairness (a second tab: the edited rule stays a draft): each consultant's supervised trainee sessions.
        page = context.new_page()
        page.goto(live_server)
        expect(page.locator('#main[data-loaded-view]')).to_have_count(1, timeout=30000)
        navigate(page, 'insights/fairness')
        october(page)
        page.get_by_label('Measure', exact=True).select_option(label='Trainee sessions supervised')
        dist = section(page, 'distribution')
        expect(dist).to_contain_text('Trainee sessions supervised per person')
        expect(dist.locator('.fx-basis')).to_have_text('Reference (not a goal) · pro rata to days available')
        expect(dist).to_contain_text('Default band (not a goal’s)')
        # With no one chosen, Supervision compares the people who can supervise, never the trainee.
        expect(page.locator('.fx-scope')).to_contain_text('Demo supervision')
        expect(dist.locator('.fx-drow')).to_have_count(2)
        expect(dist.get_by_role('button', name=re.compile('^Alex Morgan'))).to_have_count(0)
        jordan = dist.get_by_role('button', name=re.compile('^Jordan Lee: 4 trainee sessions supervised'))
        expect(jordan).to_be_visible()
        expect(dist.get_by_role('button', name=re.compile('^Taylor Noor: 0 trainee sessions supervised'))).to_be_visible()
        # A goal card names each person beside a long unit (the name never shrinks to nothing).
        goal = section(page, 'goals').locator('.fx-goal').filter(has_text='Trainee sessions supervised')
        expect(goal.get_by_role('button', name=re.compile('^Jordan Lee'))).to_be_visible()
        assert goal.locator('.fx-link .truncate').first.evaluate('x => x.getBoundingClientRect().width > 40')
        goal.scroll_into_view_if_needed()
        shot(page, 'supervision-goal', width, theme)
        dist.scroll_into_view_if_needed()
        shot(page, 'supervision-fairness', width, theme)
        jordan.click()
        drill = page.get_by_role('region', name='Trainee sessions supervised · Jordan Lee')
        expect(drill.locator('tbody tr')).to_have_count(4)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_a_goal_that_applies_to_nobody_says_so(browser, live_server, h, width, theme):
    """A supervision goal for a rank nobody holds scores 100 on the server; its card warns instead of looking perfect."""
    flag(h)
    supervised_october(h)
    fair_share_rule(h, [{'id': '6d0c3f0e-0000-4000-8000-000000000001', 'numbers': {'apply': {'value': None, 'level': 'medium'}},
                         'who': {'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [h.seed['trainee_category']]}]},
                         'settings': {'balance_goal': {'metric': 'supervision', 'period': 'month'}}}])
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    try:
        navigate(page, 'insights/fairness')
        october(page)
        card = section(page, 'goals').locator('.fx-goal').filter(has_text='Trainee sessions supervised')
        expect(card).to_contain_text('Applies to nobody')
        expect(card.locator('.fx-goal-score')).to_have_count(0)
        card.scroll_into_view_if_needed()
        shot(page, 'supervision-goal-nobody', width, theme)
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_a_supervision_goals_distribution_shows_its_goal_card_numbers(browser, live_server, h, width, theme):
    """Distribution › Compare with a goal reads that goal's own figures (basis goal): the subtitle says how it is
    shared, and every person the goal card lists has the same total and expected share in the distribution."""
    flag(h)
    supervised_october(h)
    fair_share_rule(h, [{'id': '6d0c3f0e-0000-4000-8000-000000000002', 'numbers': {'apply': {'value': None, 'level': 'medium'}},
                         'who': {'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [h.seed['category']]}]},
                         'settings': {'balance_goal': {'metric': 'supervision', 'period': 'month'}}}])
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    try:
        navigate(page, 'insights/fairness')
        october(page)
        card = section(page, 'goals').locator('.fx-goal').filter(has_text='Trainee sessions supervised')
        listed = card.locator('.fx-list .fx-link')
        expect(listed).to_have_count(2)  # Jordan above and Taylor below their supervisable share
        said = {row.locator('.truncate').inner_text(): row.locator('small').inner_text().split(' ')[:3] for row in listed.all()}
        dist = section(page, 'distribution')
        expect(dist.locator('.fx-basis')).to_contain_text('Reference (not a goal)')
        pick = dist.get_by_label('Compare with', exact=True)
        pick.select_option(pick.locator('option', has_text='Trainee sessions supervised').get_attribute('value'))
        dist = section(page, 'distribution')
        expect(dist.locator('.fx-basis')).to_contain_text('Shared by sessions each person can supervise')
        expect(dist.locator('.fx-basis')).to_contain_text('Each group on its own')
        expect(dist).to_contain_text('Expected band (the goal’s leeway)')
        for name, (actual, _, expected) in said.items():
            row = dist.locator('.fx-drow').filter(has=page.locator('.fx-name', has_text=name))
            expect(row.locator('.fx-val')).to_have_text(actual)
            expect(row.locator('.fx-expv')).to_have_text(expected)
        dist.scroll_into_view_if_needed()
        shot(page, 'supervision-goal-distribution', width, theme)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()

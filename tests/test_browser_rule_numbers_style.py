"""Rule numbers read in one style everywhere (approved mockups, DESIGN.md › Rule numbers): plain text in the UI font,
tabular numerals, weight 500 — no pill, no monospace — "≤ 10", "1↰ – 10" and a labelled strength badge ("Mandatory"), an inherited number muted. The Rules
database, a rule's lines table, the line panel's title, Needs attention and Which rule applies? all draw them through
`numbersEl` (`.rule-num`)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import catalogue_rule, flag, line, rules_action

pytestmark = pytest.mark.browser


def plain(locator):
    expect(locator).to_be_visible()
    # A rule check may redraw the row; native expectations read its current connected number each time.
    expect(locator).not_to_have_css('font-family', re.compile(r'.*(mono|consol|courier).*', re.I))
    expect(locator).to_have_css('font-variant-numeric', re.compile(r'.*tabular-nums.*'))
    expect(locator).to_have_css('font-weight', '500')
    expect(locator).to_have_css('background-color', 'rgba(0, 0, 0, 0)')
    expect(locator).to_have_css('border-top-width', '0px')


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_rule_numbers_read_in_one_style_everywhere(browser, live_server, h, scheme):
    flag(h)
    a = h.seed['people'][0]
    catalogue_rule(h, 'duties_per_month', [line({'min': {'value': 1, 'level': 'mandatory'}, 'max': {'value': 20, 'level': 'mandatory'}}, note='Everyone line'),
                                           line({'max': {'value': 10, 'level': 'mandatory'}}, who={'conditions': [{'field': 'person_ids', 'values': [a]}]}, note='Alex line')])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules')
        main = page.locator('#main')
        main.get_by_role('button', name='Open Duties per month').click()
        alex = main.locator('tr.rt-line').filter(has=page.locator('.rp.person')).locator('.rule-num')
        expect(alex).to_have_text('1↰ – 10 Mandatory')
        plain(alex)
        expect(alex.locator('.rc-inh')).to_have_text('1↰')
        expect(alex.locator('.rc-inh')).to_have_css('font-style', 'normal')  # muted, not italic
        expect(page.locator('.rl-val, .rt-num')).to_have_count(0)  # the pill and the mono variant are gone
        # Issues (here the overlap acknowledged when proposed, under Acknowledged): each line it names carries its numbers.
        main.locator('button.dt-saved').click()
        page.get_by_role('menuitemradio', name=re.compile('^Acknowledged')).click()
        issue = main.locator('tr.rt-issue').first
        expect(issue.locator('.rule-num').first).to_have_text('1 – 20 Mandatory')
        plain(issue.locator('.rule-num').first)
        # A rule's lines table and the line panel's title.
        navigate(page, 'rules/duties_per_month')
        row = main.locator('tr[data-row]').filter(has_text='Alex line')
        max_ = row.get_by_label(re.compile('^Max: ')).locator('.rule-num')
        expect(max_).to_have_text('10 Mandatory')
        plain(max_)
        row.locator('td[data-col="who"]').click()
        expect(page.locator('.rc-line-panel').get_by_role('heading', level=2)).to_have_text(re.compile(r' · 1↰ – 10 · Mandatory$'))
        # Which rule applies?: the number each person gets.
        navigate(page, 'rules')
        rules_action(page, 'Which rule applies?')
        dialog = page.get_by_role('dialog', name='Which rule applies?')
        dialog.get_by_label('Person', exact=True).select_option(a)
        number = dialog.locator('.which-rule-rows li').filter(has_text='Line 2').locator('.rule-num')
        expect(number).to_have_text('≤ 10 Mandatory')
        plain(number)
    finally:
        context.close()


def test_advisory_level_tooltip_names_its_weight(browser, live_server, h):
    """RU-18: "Advisory · medium" says what medium weighs (the department's strengths, Configuration › Auto-schedule &
    scoring) where the word appears."""
    flag(h)
    catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'medium'}})])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.locator('#main').get_by_role('button', name='Open Duties per week').click()
        badge = page.locator('#main tr.rt-line .rc-level[data-level="advisory"]').first
        expect(badge).to_have_text('Advisory · medium')
        expect(badge).to_have_attribute('data-tip', re.compile(r'^Counts \d+× a low advisory rule in the schedule score$'))
    finally:
        context.close()

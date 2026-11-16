"""QA sweep before rc.9 (Rules catalogue): the regressions found there — number-by-number words, the selection bar's
count and actions agreeing, same-button confirm on Remove lines, the read-only line panel and Needs attention on a
phone."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import bar_action, catalogue_rule, flag, line, selection

pytestmark = pytest.mark.browser


def n(value):
    return {'value': value, 'level': 'mandatory'}


def monthly(h):
    """Everyone min 2 max 5, the rank min 1 and Alex max 3: line 1 gives its min to the rank and its max to Alex."""
    a = h.seed['people'][0]
    rank = {'conditions': [{'field': 'category_ids', 'values': [h.seed['category']]}]}
    dates = {'starts_on': '2026-10-01', 'ends_on': '2027-01-01'}
    catalogue_rule(h, 'duties_per_month', [line({'min': n(2), 'max': n(5)}, **dates), line({'min': n(1)}, who=rank, **dates),
                                           line({'max': n(3)}, who={'conditions': [{'field': 'person_ids', 'values': [a]}]}, **dates)])


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_needs_attention_words_and_the_selection_count(browser, live_server, h, width, scheme):
    flag(h)
    monthly(h)
    # Its overlap was acknowledged when proposed (shared acknowledgements): taken back, it needs attention again.
    overlap = [x['fingerprint'] for x in h.get('coordinator', '/api/rules/catalogue')['attention'] if x['kind'] == 'overlap']
    h.command('coordinator', 'rule.unacknowledge', {'fingerprints': overlap})
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules')
        main = page.locator('#main')
        main.locator('button.dt-saved').click()
        page.get_by_role('menuitemradio', name=re.compile('^Needs attention')).click()
        issue = main.locator('tr.rt-issue').filter(has_text='gives way')
        # Each winner with the numbers it takes; "its other numbers" only when the line has others.
        expect(issue).to_contain_text('Line 1 gives way to line 2 on its min, and to line 3 on its max, for 3 people')
        expect(issue).not_to_contain_text('other numbers')
        if width < 720:  # a phone scrolls the table sideways rather than crushing the sentence to a word a line
            assert issue.locator('td').nth(3).bounding_box()['width'] >= 200
        # A picked issue that a search hides is still picked: the bar's count and its action agree.
        issue.locator('input.bulk-check').check()
        main.get_by_role('searchbox', name='Search issues').fill('zzz')
        expect(selection(page)).to_contain_text('Overlap')
        bar_action(page, 'Acknowledge')  # in ⋯ More on a phone
        page.get_by_role('dialog', name='Acknowledge 1 issue').get_by_role('button', name='Acknowledge', exact=True).click()
        expect(page.locator('.toast').filter(has_text='1 issue acknowledged for everyone.')).to_be_visible()
    finally:
        context.close()


def test_the_check_says_which_numbers_give_way_and_remove_lines_asks_twice(browser, live_server, h):
    flag(h)
    monthly(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_month')
        main = page.locator('#main')
        everyone = main.locator('tr[data-row]').first
        everyone.get_by_role('button', name='⚠ Gives way').click()
        pop = page.get_by_role('dialog', name='Two lines cover the same people')
        expect(pop).to_contain_text('on its max and min for 3 people today')
        expect(pop).not_to_contain_text('no longer applies')  # number by number: never the whole line
        page.keyboard.press('Escape')
        everyone.locator('input.bulk-check').check()
        remove = selection(page).get_by_role('button', name='Remove lines')
        remove.click()  # armed: the first press asks
        expect(main.locator('tr[data-row]')).to_have_count(3)
        remove.click()
        expect(main.locator('tr[data-row]')).to_have_count(2)
    finally:
        context.close()


def test_a_read_only_line_panel_keeps_its_titles_clear(browser, live_server, h):
    flag(h)
    monthly(h)
    context, page = sign_in(browser, live_server, 'auditor')
    try:
        navigate(page, 'rules/duties_per_month')
        page.locator('#main tr[data-row]').filter(has_text='Alex Morgan').locator('td[data-col="who"]').click()
        panel = page.locator('.rc-line-panel')
        expect(panel.locator('.rc-lp-title').first).to_be_visible()
        expect(panel.locator('.cond-fact').first).to_be_visible()
        title, item = panel.locator('.rc-lp-title').first.bounding_box(), panel.locator('.cond-fact').first.bounding_box()
        assert title['y'] + title['height'] <= item['y'], (title, item)  # the filters sit under their title, not over it
        expect(panel.get_by_label('Minimum', exact=True)).to_have_attribute('placeholder', '1↰')
        expect(panel.get_by_role('button', name='+ Add filter rule')).to_have_count(0)
        expect(panel.locator('.cond-line select:not([disabled])')).to_have_count(0)
    finally:
        context.close()

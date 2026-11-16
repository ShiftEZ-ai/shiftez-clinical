"""Rules UI/UX review maintenance, maintenance and maintenance (11 Nov 2026), in the browser: a disabled Propose says why with Open it, the All
rules key lists the marks the list draws on one line, and a rank wears one tint on its rule page and in Needs attention
while that page's key covers every mark its lines draw."""
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_audit_attention_layout import view
from .test_browser_rule_catalogue import catalogue_rule, flag, line

pytestmark = pytest.mark.browser

WAITING = 'Duties per week already has a change waiting for approval'
# Every [data-rule-mark] drawn and shown is entered in a key on screen under each of its marks (the Rules key's SSOT).
UNKEYED = """root => {const r=document.querySelector(root),keys=[...r.querySelectorAll('.view-legend li[data-lg-entry]')].map(li=>li.dataset.lgEntry);
  return [...r.querySelectorAll('[data-rule-mark]')].filter(n=>n.checkVisibility()&&!n.closest('.view-legend'))
    .flatMap(n=>n.dataset.ruleMark.split(' ').filter(k=>!(n.getAttribute('data-lg')||'').split(' ').some(s=>s.endsWith(':'+k)&&keys.includes(s)))
    .map(k=>k+' '+n.outerHTML.slice(0,80)));}"""


def _waiting(h):
    """Duties per week, not in force, with a change waiting for another person's approval."""
    flag(h)
    h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_week', 'lines': [line({'max': {'value': 5, 'level': 'mandatory'}})],
                                              'reason': 'Waiting fixture', 'acknowledged_overlaps': []})


def _description(locator):
    return locator.evaluate("b => (b.getAttribute('aria-describedby')||'').split(' ').map(id => document.getElementById(id)?.textContent||'').join(' ')")


def test_a_disabled_propose_says_why_and_opens_the_waiting_change(browser, live_server, h):
    _waiting(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules/duties_per_week')
        # The page's header actions sit in the top bar; a waiting proposal keeps the DEV page's "+ New line" control absent.
        main, top = page.locator('#main'), page.locator('.topbar')
        propose = top.get_by_role('button', name='Propose', exact=True)
        expect(propose).to_be_disabled()
        why = top.locator('.why-unavailable').filter(has_text=WAITING)
        expect(why).to_be_visible()  # visible words, not a tooltip only
        assert WAITING in _description(propose)
        expect(main.get_by_role('button', name='+ New line', exact=True)).to_have_count(0)
        why.get_by_role('button', name='Open it').click()
        expect(page.locator('.rc-line-panel')).to_be_visible()  # the waiting change, to approve or decline
        # The Rules list: nothing changed yet, so Propose says so.
        navigate(page, 'rules')
        listed = page.locator('.topbar').get_by_role('button', name='Propose', exact=True)
        expect(listed).to_be_disabled()
        assert 'Nothing to propose yet' in _description(listed)
        expect(page.locator('.topbar .why-unavailable').filter(has_text='Nothing to propose yet')).to_be_visible()
    finally:
        context.close()


def test_the_all_rules_key_lists_the_marks_the_list_draws_on_one_line(browser, live_server, h):
    _waiting(h)
    catalogue_rule(h, 'rest_between_duties', [line({'min': {'value': 11, 'level': 'mandatory'}})])
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        view(page, 'All rules')
        main = page.locator('#main')
        expect(main.locator('tr.rt-rule .rt-waiting-chip').first).to_be_visible()
        key = main.locator('.rt > .view-legend')
        expect(key).to_be_visible()
        # The marks drawn are listed (words in the key, including any behind "+N").
        expect(key).to_contain_text('read only until approved or declined')
        expect(key).to_contain_text('Off: not in force')
        assert page.evaluate(UNKEYED, '#main') == []
        # One line below the table, the rest behind "+N".
        box = key.evaluate('k => ({h:k.getBoundingClientRect().height,line:parseFloat(getComputedStyle(k).fontSize)*2.6,'
                           'below:k.getBoundingClientRect().top>=document.querySelector(".rt-body").getBoundingClientRect().bottom-1})')
        assert box['h'] <= box['line'] + 14 and box['below'], box
    finally:
        context.close()


def test_a_rank_wears_one_tint_and_the_rule_page_key_covers_its_marks(browser, live_server, h):
    flag(h)
    today = date.fromisoformat(h.get('coordinator', '/api/rules/catalogue')['day'])
    rank = h.seed['category']
    name = next(c['name'] for c in h.get('coordinator', '/api/catalogues')['categories'] if c['id'] == rank)
    who = {'conditions': [{'field': 'category_ids', 'values': [rank]}]}
    catalogue_rule(h, 'duties_per_week', [
        line({'max': {'value': 4, 'level': 'mandatory'}}, who=who, ends_on=str(today + timedelta(days=11))),  # ends soon: Needs attention
        line({'max': {'value': 3, 'level': 'mandatory'}}, starts_on=str(today + timedelta(days=40)))])        # Starts later
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    tint = "n => getComputedStyle(n).backgroundColor"
    try:
        navigate(page, 'rules/duties_per_week')
        main = page.locator('#main')
        page_pill = main.locator('.rp', has_text=name).first
        expect(page_pill).to_be_visible()
        on_page = page_pill.evaluate(tint)
        # The key names what the lines draw (an entry behind "+N" still counts: it is listed there).
        key = main.locator('.view-legend').filter(has_text='Rank, each in its own tint')
        expect(key).to_have_count(1)
        expect(key).to_contain_text('not in force yet')
        assert page.evaluate(UNKEYED, '#main') == []
        navigate(page, 'rules')
        view(page, 'Needs attention')
        listed = main.locator('.rt-ilines .rp', has_text=name).first
        expect(listed).to_be_visible()
        assert listed.evaluate(tint) == on_page  # one rank, one tint (C21), never Group purple
    finally:
        context.close()

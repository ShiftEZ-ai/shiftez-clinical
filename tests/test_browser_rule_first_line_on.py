"""The first line of a rule never in force turns it on (#1200), on the rule's own page as on the Rules list: the switch
reads On, the proposal says it turns the rule on, and once approved the rule is in force and read by the rule map. A
rule in force that is off stays off when a line is added. Release blocker: a new safety rule approved with its first
line stayed off while the confirmation said the change was active."""
import re
import uuid

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import flag
from .test_rule_catalogue_api import approve, propose as propose_rule

pytestmark = pytest.mark.browser
SWITCH = 'Duties per week on or off'


def entry(h, key):
    return next(e for e in h.get('approver', '/api/rules/catalogue')['entries'] if e['id'] == key)


def someone(h):
    """The first seeded person's name, as the New line picker lists it."""
    return next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == h.seed['people'][0])['display_name']


def add_person_line(page, name, maximum):
    main = page.locator('#main')
    main.get_by_role('button', name='New line: a person, rank, clinic or #tag').click()
    page.get_by_role('combobox', name='Search New line').fill(name)
    page.get_by_role('option', name=re.compile('^' + re.escape(name))).first.click()
    panel = page.locator('.rc-line-panel')
    panel.get_by_label('Maximum', exact=True).fill(str(maximum))
    panel.get_by_role('button', name='Keep change').click()
    panel.get_by_role('button', name='Close panel', exact=True).click()
    expect(main.locator('tr').filter(has_text=name)).to_contain_text(f'{maximum} Mandatory')


def propose_in_page(page, name, changes, says):
    page.locator('.topbar').get_by_role('button', name=f'Propose {changes}').click()
    dialog = page.get_by_role('dialog')
    for words in says:
        expect(dialog).to_contain_text(words)
    dialog.get_by_label('Reason', exact=True).fill('Weekly cap')
    dialog.get_by_role('button', name='Propose', exact=True).click()
    expect(page.locator('#main .rc-waiting')).to_contain_text(name)


def test_the_first_line_of_a_new_rule_turns_it_on_and_it_is_in_force_once_approved(browser, live_server, h):
    flag(h)
    assert entry(h, 'duties_per_week')['rule'] is None  # never in force
    name = someone(h)
    w = h.shift(h.workspace('First line'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    approver, review = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules/duties_per_week')
        switch = page.get_by_role('switch', name=SWITCH)
        expect(switch).not_to_be_checked()
        add_person_line(page, name, 3)
        expect(switch).to_be_checked()  # the first line turns it on, as the Rules list's + New line does
        expect(page.locator('#main .rc-summary')).to_contain_text('On')
        propose_in_page(page, name, '2 changes', ['Turns the rule on'])
        proposed = entry(h, 'duties_per_week')['proposed']
        assert proposed['definition']['rule'].get('on', True) is True
        navigate(review, 'rules/duties_per_week')
        expect(review.locator('.rc-waiting .rc-prop-summary')).to_have_text('1 line added · Turns the rule on')
    finally:
        context.close()
        approver.close()
    active = approve(h, proposed)
    assert active['definition']['rule'].get('on', True) is True
    rule = entry(h, 'duties_per_week')['rule']
    assert rule['id'] == active['id'] and rule['definition']['rule'].get('on', True) is True
    h.refresh_draft_baseline(h.detail(w)['workspace'])  # the new version is newer than the draft's baseline
    assert any(r['id'] == rule['id'] for r in h.get('coordinator', f"/api/workspaces/{w['id']}/rule-map")['rules'])


def test_a_line_added_to_a_rule_switched_off_keeps_it_off(browser, live_server, h):
    flag(h)
    record = propose_rule(h, 'duties_per_week', [{'id': str(uuid.uuid4()), 'numbers': {'max': {'value': 5}}}],
                          on=False, reason='Off for now')['record']
    off = approve(h, record)
    assert off['definition']['rule'].get('on', True) is False
    name = someone(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        switch = page.get_by_role('switch', name=SWITCH)
        expect(switch).not_to_be_checked()
        add_person_line(page, name, 3)
        expect(switch).not_to_be_checked()  # a rule in force keeps its own on or off
        propose_in_page(page, name, '1 change', [])
        expect(page.get_by_role('dialog')).to_have_count(0)
    finally:
        context.close()
    proposed = entry(h, 'duties_per_week')['proposed']
    assert proposed['definition']['rule'].get('on', True) is False
    assert approve(h, proposed)['definition']['rule'].get('on', True) is False

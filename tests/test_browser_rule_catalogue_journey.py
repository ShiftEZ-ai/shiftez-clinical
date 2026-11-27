"""The rule catalogue end to end: a rule turned on, a line added, checked as you edit (the replacement is shown),
proposed, approved by another person on the rule's page with its line changes, read by the rule map, and
undone (retiring brings back the version before). The admin's move of the rules in force into the catalogue."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import route_fetch, browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_catalogue_approval_checks import check_and_approve_in_panel
from .test_browser_rule_catalogue import flag, shot
from .test_rule_catalogue_guard import world

pytestmark = pytest.mark.browser


def reason(page, text, button):
    dialog = page.get_by_role('dialog')
    dialog.get_by_label('Reason', exact=True).fill(text)
    dialog.get_by_role('button', name=button, exact=True).click()


def entry(h, key):
    return next(e for e in h.get('approver', '/api/rules/catalogue')['entries'] if e['id'] == key)


def test_turn_on_add_a_line_acknowledge_its_overlap_approve_and_undo(browser, live_server, h):
    flag(h)
    w = h.shift(h.workspace('Catalogue journey'))['workspace']
    coordinator, page = sign_in(browser, live_server, 'coordinator')
    approver, review = sign_in(browser, live_server, 'approver')
    try:
        # Turn the rule on (a proposal) and approve it on the rule's page, where its line changes are listed.
        navigate(page, 'rules')
        page.get_by_role('switch', name='Duties per week on or off').click()  # a change in the draft, proposed with the rest
        # SW3-B-16: turning it on and its first line are two changes, as the dialog lists them.
        page.locator('.topbar').get_by_role('button', name='Propose 2 changes').click()
        expect(page.get_by_role('dialog')).to_contain_text('Duties per week · 2 changes')
        reason(page, 'Weekly cap', 'Propose')
        expect(page.locator('#main li.rt-item').filter(has_text='Duties per week')).to_contain_text('Waiting for approval')
        navigate(review, 'rules/duties_per_week')
        waiting = review.locator('.rc-waiting')
        # Only what is not zero (U-20); a rule not in force yet says its activation turns it on (8df451143).
        expect(waiting.locator('.rc-prop-summary')).to_have_text('1 line added · Turns the rule on')
        expect(waiting.locator('tr.rc-prop-added')).to_contain_text('≤ 5 Mandatory')
        waiting.get_by_role('button', name='Review and approve').click()
        # B5: the approver decides in the docked panel once the overlapping draft's effect is checked.
        check_and_approve_in_panel(review, h, w, entry(h, 'duties_per_week')['proposed'], 'Add a line for everyone · turns the rule on')
        expect(review.locator('.rc-summary')).to_contain_text('On')  # an approver reads On, not a switch (U-19)

        # Add a line for one person and read the resolved replacement as you edit.
        navigate(page, 'rules/duties_per_week')
        main = page.locator('#main')
        main.get_by_role('button', name='New line: a person, rank, clinic or #tag').click()
        page.get_by_role('combobox', name='Search New line').fill('Staff 01(test)')
        page.get_by_role('option', name=re.compile('^Staff 01\\(test\\)')).first.click()
        # The new line opens in the panel; it joins the table once it has a number.
        panel = page.locator('.rc-line-panel')
        panel.get_by_label('Maximum', exact=True).fill('3')
        panel.get_by_role('button', name='Keep change').click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        alex = main.locator('tr').filter(has_text='Staff 01(test)')
        expect(alex.locator('.rt-instead')).to_contain_text('(instead of ≤ 5')
        expect(alex).to_contain_text('3 Mandatory')  # The person line takes the strength it overrides.
        expect(alex.locator('.rc-overlap')).to_have_count(0)
        shot(page, 'ordered-override-checked')
        page.locator('.topbar').get_by_role('button', name='Propose 1 change').click()
        dialog = page.get_by_role('dialog')
        expect(dialog.get_by_label('I have read which lines give way and that is intended')).to_have_count(0)
        reason(page, 'Staff 01 returns from leave', 'Propose')
        expect(main.locator('.rc-waiting')).to_contain_text('Staff 01(test)')
        proposal = entry(h, 'duties_per_week')['proposed']
        broad, personal = proposal['definition']['rule']['lines']
        [replacement] = proposal['check']['overlaps']
        assert replacement['line_id'] == broad['id'] and replacement['winner_ids'] == [personal['id']]
        assert replacement['numbers'] == ['max'] and replacement['people'] == 1
        for part, maximum in ((broad, 5), (personal, 3)):
            assert part['numbers']['max'] == {'value': maximum, 'level': 'mandatory'}
            assert not (part.get('where') or {}).get('conditions')
            assert set(part.get('weekdays', range(7))) == set(range(7))
            assert not part.get('starts_on') and not part.get('ends_on')
        expect(main.locator('.rc-waiting').get_by_role('button', name='Review and approve')).to_have_count(0)  # the proposer withdraws, never approves
        expect(main.locator('.rc-waiting').get_by_role('button', name='Withdraw')).to_be_visible()

        # The approver approves: the rule map reads the new line; Undo retires it and the version before applies again.
        review.reload()
        expect(review.locator('.rc-waiting tr.rc-prop-added')).to_contain_text('Staff 01(test)')
        review.locator('.rc-waiting').get_by_role('button', name='Review and approve').click()
        check_and_approve_in_panel(review, h, w, entry(h, 'duties_per_week')['proposed'], 'Add a line for Staff 01(test)')
        expect(review.locator('.rc-waiting')).to_have_count(0)
        # The Undo message lasts 10 s unless held: hold it (as a person reading it would) while the server is checked.
        undo = review.locator('.toast').get_by_role('button', name='Undo')
        undo.hover()
        rule = entry(h, 'duties_per_week')['rule']
        assert len(rule['definition']['rule']['lines']) == 2
        h.refresh_draft_baseline(h.detail(w)['workspace'])  # the new version is newer than the draft's baseline
        assert any(r['id'] == rule['id'] for r in h.get('coordinator', f"/api/workspaces/{w['id']}/rule-map")['rules'])
        undo.click()
        expect(review.get_by_text(re.compile('Activation undone'))).to_be_visible()
        after = entry(h, 'duties_per_week')['rule']
        assert after['id'] != rule['id'] and len(after['definition']['rule']['lines']) == 1
    finally:
        coordinator.close()
        approver.close()

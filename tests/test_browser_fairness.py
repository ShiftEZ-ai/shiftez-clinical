"""Balance goals in the rule form: the sentence builder, the preview on the current draft, and converting an
allocation preference's balancing into a goal."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, save_dialog, open_rule  # noqa: F401

pytestmark = pytest.mark.browser


def test_goal_builder_previews_on_the_draft_and_saves(browser, live_server, h):
    a = h.seed['people'][0]
    w = h.workspace('Published week', '2026-10-04', '2026-10-11')
    for day in (4, 5):
        w = h.shift(w, f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00', people=[a])['workspace']
    h.publish(w)
    draft = h.workspace('Draft week', '2026-10-11', '2026-10-18')
    h.shift(draft, '2026-10-11T08:00:00+03:00', '2026-10-11T12:00:00+03:00', people=[a])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_role('radio', name='Balance goal', exact=True).check()
        builder = form.locator('[data-goal-builder]')
        expect(builder).to_contain_text('for the people in Scope, over')
        builder.get_by_label('Count', exact=True).select_option('duties')
        builder.get_by_label('Over', exact=True).select_option('month')
        builder.get_by_label('Aiming for', exact=True).select_option('equal_share')
        builder.get_by_label('Strength', exact=True).select_option('high')
        expect(form.locator('.scope-when')).to_be_hidden()  # a goal counts whole days
        form.get_by_role('button', name='Preview on current draft').click()
        preview = form.locator('.goal-preview')
        expect(preview).to_contain_text('Goal score')
        row = preview.locator('tr').filter(has_text='Alex Morgan')
        expect(row).to_contain_text('2')  # two duties in the published week (history) + one on the draft
        expect(row.locator('td').nth(3)).to_have_text('3')
        form.get_by_label('Rule title', exact=True).fill('Fair duties each month')
        form.get_by_label('Source or departmental decision', exact=True).fill('Constructed browser test')
        save_dialog(form, 'Propose rule')
        rule = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['name'] == 'Fair duties each month')
        assert rule['definition']['rule']['kind'] == 'balance_goal' and rule['definition']['rule']['period'] == 'month'
        assert rule['definition']['strength'] == 'high' and rule['definition']['severity'] == 'advisory'
        expect(page.locator('.rule-item').filter(has_text='Fair duties each month')).to_contain_text('Count duties over the calendar month')
    finally:
        context.close()


def test_preference_balancing_converts_to_a_goal(browser, live_server, h):
    preference = h.policy({'kind': 'allocation_preference', 'priority': 0, 'balance_by': 'duties', 'balance_window': 'rolling',
                           'balance_window_days': 14}, severity='advisory')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        sheet = open_rule(page, preference['name'])
        sheet.get_by_role('button', name='Convert to balance goal…').click()
        # The server's conversion says what changes first; editing opens the prefilled form.
        page.get_by_role('dialog').get_by_role('button', name='Edit before proposing…').click()
        form = page.get_by_role('dialog')
        expect(form.get_by_role('radio', name='Balance goal', exact=True)).to_be_checked()
        builder = form.locator('[data-goal-builder]')
        expect(builder.get_by_label('Over', exact=True)).to_have_value('rolling')
        expect(builder.get_by_label('Days', exact=True)).to_have_value('14')
        expect(form.get_by_label('Rule title', exact=True)).to_have_value(preference['name'])
    finally:
        context.close()


def test_a_target_each_takes_own_targets_from_the_per_person_table(browser, live_server, h):
    a = h.seed['people'][0]
    draft = h.workspace('Draft week', '2026-10-11', '2026-10-18')
    h.shift(draft, '2026-10-11T08:00:00+03:00', '2026-10-11T12:00:00+03:00', people=[a])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_role('radio', name='Balance goal', exact=True).check()
        builder = form.locator('[data-goal-builder]')
        own = builder.locator('.pp-editor')
        expect(own).to_be_hidden()  # an equal share has no amounts
        builder.get_by_label('Over', exact=True).select_option('rota')
        builder.get_by_label('Aiming for', exact=True).select_option('target')
        builder.get_by_label('Amount', exact=True).fill('1')
        expect(own).to_be_visible()
        own.get_by_role('button', name='Add a person').click()
        own.get_by_label('Person', exact=True).select_option(label='Alex Morgan')
        own.get_by_label('Target', exact=True).fill('4')
        expect(own.get_by_label('From', exact=True)).to_have_count(0)  # a goal's own target holds for its period
        form.get_by_role('button', name='Preview on current draft').click()
        preview = form.locator('.goal-preview')
        expect(preview.locator('tr').filter(has_text='Alex Morgan').locator('td').nth(4)).to_have_text(re.compile(r'^4 '))
        expect(preview.locator('tr').filter(has_text='Jordan Lee').locator('td').nth(4)).to_have_text(re.compile(r'^1 '))
        form.get_by_label('Rule title', exact=True).fill('Own duty targets')
        form.get_by_label('Source or departmental decision', exact=True).fill('Constructed browser test')
        save_dialog(form, 'Propose rule')
        rule = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['name'] == 'Own duty targets')
        assert rule['definition']['rule']['person_amounts'] == {a: 4} and rule['definition']['rule']['amount'] == 1
        expect(page.locator('.rule-item').filter(has_text='Own duty targets')).to_contain_text('1 person has their own')
    finally:
        context.close()


def test_an_overtime_goal_shares_by_what_each_person_offered(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_role('radio', name='Balance goal', exact=True).check()
        builder = form.locator('[data-goal-builder]')
        share = builder.get_by_label('Share by', exact=True)
        expect(share.locator('option[value=offered]')).to_be_hidden()  # only overtime shares by offered hours
        builder.get_by_label('Count', exact=True).select_option('overtime_hours')
        expect(share).to_have_value('offered')  # the catalogue's suggestion for a new overtime goal
        builder.get_by_label('Count', exact=True).select_option('duties')
        expect(share).to_have_value('available_days')
        builder.get_by_label('Count', exact=True).select_option('overtime_duties')
        builder.get_by_label('Aiming for', exact=True).select_option('equal_share')
        form.get_by_label('Rule title', exact=True).fill('Fair overtime by offers')
        form.get_by_label('Source or departmental decision', exact=True).fill('Constructed browser test')
        save_dialog(form, 'Propose rule')
        rule = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['name'] == 'Fair overtime by offers')
        assert rule['definition']['rule']['share_by'] == 'offered'
        expect(page.locator('.rule-item').filter(has_text='Fair overtime by offers')).to_contain_text('pro rata to overtime offered')
    finally:
        context.close()

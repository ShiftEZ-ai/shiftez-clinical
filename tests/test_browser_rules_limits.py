"""Rules and limits (W2): one quota rule with a minimum, maximum and advisory target, proposed from Add rule and read
back as one sentence and its Numbers; an existing range gaining a target through Change… (Undo withdraws it); a quota
line's own target edited in place; topic sections (collapse survives a re-render, counts, attention badge); empty,
filtered-empty and read-only states; department scale (~1,000 rules incl. history); and phone width."""
import re
from datetime import date

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, save_dialog, open_rule  # noqa: F401
from .test_rule_templates import propose, activate
from .rules_support import rule_status

pytestmark = pytest.mark.browser

RANGE = {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'range', 'amount': 6, 'maximum_amount': 8,
         'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'window_alignment': 'calendar_week'}


def policies(h, status=None):
    rows = h.get('coordinator', '/api/catalogues')['policies'] + h.get('coordinator', '/api/policies?status=retired')['policies']
    return [p for p in rows if status is None or p['status'] == status]


def fact(item, label):
    """The value of one labelled fact on a rule's Details tab."""
    return item.locator('.rule-facts > div').filter(has=item.page.locator('dt', has_text=re.compile('^' + label + '$'))).locator('dd')


def section(page, topic):
    return page.locator('section.rule-group').filter(has=page.locator('.rule-group-name', has_text=topic))


def test_add_rule_quota_with_minimum_maximum_and_target_reads_as_one_rule(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog', name='Add rule')
        form.get_by_role('radio', name='Quota or commitment', exact=True).check()
        limits = form.locator('.quota-limits')
        strength = limits.get_by_label('Target strength', exact=True)
        limits.get_by_label('Minimum', exact=True).fill('2')
        limits.get_by_label('Maximum', exact=True).fill('6')
        expect(strength).to_be_hidden()  # no target yet
        target = limits.get_by_label('Target', exact=True)
        target.fill('7')
        assert 'within the minimum and maximum' in target.evaluate('i => i.validationMessage')
        target.fill('4')
        assert target.evaluate('i => i.validity.valid')
        expect(strength).to_be_visible()
        strength.select_option('high')
        period = form.locator('.quota-period')
        period.get_by_label('From', exact=True).fill('2026-10-01')
        period.get_by_label('Until (included)', exact=True).fill('2026-10-31')
        period.get_by_label('Repeat', exact=True).select_option('calendar_week')
        expect(period.get_by_label('Scale the target to each person’s days in the department', exact=True)).to_be_hidden()  # bounds are set
        # Limits keep the default Mandatory; only a lone target is forced advisory, and a limit restores it.
        classification = form.get_by_label('Classification', exact=True)
        expect(classification).to_be_enabled()
        expect(classification).to_have_value('mandatory')
        limits.get_by_label('Minimum', exact=True).fill('')
        limits.get_by_label('Maximum', exact=True).fill('')
        expect(classification).to_have_value('advisory')
        expect(classification).to_be_disabled()
        limits.get_by_label('Minimum', exact=True).fill('2')
        limits.get_by_label('Maximum', exact=True).fill('6')
        expect(classification).to_have_value('mandatory')
        form.get_by_label('Rule title', exact=True).fill('Weekly clinical commitment')
        form.get_by_label('Source or departmental decision', exact=True).fill('Constructed browser test, not institutional policy')
        form.get_by_label('Effective from', exact=True).fill('2026-10-01')
        save_dialog(form, 'Propose rule')
        expect(page.get_by_text('Rule proposed. It applies once an approver activates it.')).to_be_visible()
        rule = next(p for p in policies(h, 'proposed') if p['name'] == 'Weekly clinical commitment')
        r = rule['definition']['rule']
        assert (r['comparison'], r['amount'], r['maximum_amount'], r['target_amount'], r['target_strength']) == ('range', 2, 6, 4, 'high')
        # Waiting: the row's mark, and the approver's single row action is Review (it opens the activation dialog).
        item = page.locator('.rule-item').filter(has_text='Weekly clinical commitment')
        expect(item.locator('.rl-mark')).to_have_text('Waiting for approval')
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        expect(page.locator('.rule-item').filter(has_text='Weekly clinical commitment').get_by_role('button', name='Review', exact=True)).to_be_visible()
    finally:
        context.close()
    activate(h, rule)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        item = page.locator('.rule-item').filter(has_text='Weekly clinical commitment')
        expect(item.locator('.rl-text strong')).to_have_text('Between 2 and 6 duties each week · aim for 4')
        expect(item.locator('.rl-mark')).to_have_text('Mandatory')  # simply active: no waiting mark
        expect(item.locator('.rule-num')).to_have_text('2 – 6 → 4 duties')
        sheet = open_rule(page, 'Weekly clinical commitment')
        expect(sheet.locator('.rl-num')).to_have_text(['Minimum2duties each week', 'Maximum6duties each week', 'Target4duties each week · high strength'])
        expect(sheet.locator('.rl-meta .badge')).to_have_text('Mandatory')
        expect(fact(sheet, 'Exception requests')).to_have_text('Not allowed')
        expect(fact(sheet, 'Counts')).to_contain_text('Every duty type')
    finally:
        context.close()


def test_existing_range_gains_a_target_through_change_and_undo_withdraws_it(browser, live_server, h):
    rule = activate(h, propose(h, RANGE, name='Internship weekly clinical range', starts_on='2026-10-01', ends_on='2026-11-01'))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        item = page.locator('.rule-item').filter(has_text='Internship weekly clinical range')
        expect(item.locator('.rl-text strong')).to_have_text('Between 6 and 8 duties each week')
        sheet = open_rule(page, 'Internship weekly clinical range')
        expect(sheet.locator('.rl-num')).to_have_text(['Minimum6duties each week', 'Maximum8duties each week', 'Target—no target'])
        sheet.locator('.rl-sheet-foot').get_by_role('button', name='Change…').click()
        form = page.get_by_role('dialog', name='Change Internship weekly clinical range')
        limits = form.locator('.quota-limits')
        expect(limits.get_by_label('Target', exact=True)).to_have_value('')
        limits.get_by_label('Target', exact=True).fill('7')
        limits.get_by_label('Target strength', exact=True).select_option('high')
        form.get_by_role('button', name='Propose change').click()
        expect(page.get_by_text('Change proposed.', exact=False)).to_be_visible()
        change = next(p for p in policies(h, 'proposed') if p['definition'].get('replaces_id') == rule['id'])
        r = change['definition']['rule']
        assert (r['comparison'], r['amount'], r['maximum_amount'], r['target_amount'], r['target_strength']) == ('range', 6, 8, 7, 'high')
        assert next(p for p in policies(h) if p['id'] == rule['id'])['status'] == 'active'  # the current rule applies until approval
        page.locator('.toast').filter(has_text='Change proposed.').get_by_role('button', name='Undo').click()
        expect(page.get_by_text('Change withdrawn.')).to_be_visible()
        assert next(p for p in policies(h) if p['id'] == change['id'])['status'] == 'retired'
        assert next(p for p in policies(h) if p['id'] == rule['id'])['status'] == 'active'
        expect(page.locator('.rule-item').filter(has_text='Internship weekly clinical range')).to_have_count(1)
    finally:
        context.close()


def test_quota_line_target_is_edited_in_place_and_shown_in_the_lines_table(browser, live_server, h):
    a = h.seed['people'][0]
    who = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    rule = activate(h, propose(h, {**RANGE, 'target_amount': 7}, name='Clinic commitment', starts_on='2026-10-01', ends_on='2026-11-01',
                               per_person=[{'person_id': a, 'amount': 5}]))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        sheet = open_rule(page, 'Clinic commitment')
        expect(sheet.locator('.rule-lines')).to_have_count(0)  # plain person lines read as a list of exceptions
        expect(sheet.locator('.rl-exc strong')).to_have_text(who[a])
        expect(sheet.locator('.rl-exc small')).to_have_text('Person · minimum 5 · maximum 8 · target 7')  # the rule's maximum and target

        page.keyboard.press('Escape')
        expect(sheet).to_have_count(0)

        def edit_target(value):
            sheet = open_rule(page, 'Clinic commitment')
            sheet.get_by_role('button', name=f'Edit line for {who[a]}').click()
            dialog = page.get_by_role('dialog', name='Change Clinic commitment')
            row = dialog.locator('.rl-exc.open')
            expect(row.get_by_label('Minimum', exact=True)).to_have_value('5')
            expect(row.get_by_label('Target', exact=True)).to_have_value('')
            expect(row.get_by_label('Target', exact=True)).to_have_attribute('placeholder', 'Same')  # empty: the rule's
            row.get_by_label('Target', exact=True).fill(value)
            row.get_by_role('button', name='Save line').click()
            expect(dialog.locator('.rl-exc small')).to_have_text(f'minimum 5 · maximum 8 · target {value}')
            dialog.get_by_role('button', name='Propose change').click()
            expect(dialog).to_have_count(0)
            expect(page.get_by_text('Change proposed.', exact=False).first).to_be_visible()
            page.keyboard.press('Escape')  # the sheet of the rule in force
            expect(sheet).to_have_count(0)
            return next(p for p in policies(h, 'proposed') if p['definition'].get('replaces_id') == rule['id'])

        first = edit_target('6')
        assert first['definition']['per_person'][0]['settings']['target_amount'] == 6
        assert first['definition']['per_person'][0]['settings'].get('maximum_amount') is None  # only what was set
        page.locator('.toast').filter(has_text='Change proposed.').get_by_role('button', name='Undo').click()
        expect(page.get_by_text('Change withdrawn.')).to_be_visible()
        assert next(p for p in policies(h) if p['id'] == first['id'])['status'] == 'retired'

        second = edit_target('6')
        activate(h, second)
        page.reload()
        navigate(page, 'rules')
        sheet = open_rule(page, 'Clinic commitment')
        expect(sheet.locator('.rl-exc small')).to_have_text('Person · minimum 5 · maximum 8 · target 6')
    finally:
        context.close()


def test_topic_sections_collapse_across_rerenders_with_counts_and_attention(browser, live_server, h):
    activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest between duties'))
    activate(h, propose(h, {'kind': 'max_hours', 'hours': 50, 'window': 'week'}, name='Weekly hours'))
    activate(h, propose(h, {'kind': 'minimum_cover', 'minimum': 2}, name='Clinic cover'))
    propose(h, {'kind': 'max_consecutive_days', 'maximum': 6}, name='Days in a row')  # waiting: needs attention
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        limits, cover = section(page, 'Limits & rest'), section(page, 'Staffing & cover')
        expect(limits.locator('.gt-count')).to_have_text('3')
        expect(cover.locator('.gt-count')).to_have_text('1')
        expect(limits.locator('.rl-group-meta')).to_have_text('1 waiting for approval')
        expect(cover.locator('.rl-group-meta')).to_have_count(0)
        toggle = limits.get_by_role('button', name=re.compile('^Limits & rest'))
        expect(toggle).to_have_attribute('aria-expanded', 'true')
        toggle.click()
        expect(toggle).to_have_attribute('aria-expanded', 'false')
        expect(limits.locator('.rule-group-body')).to_be_hidden()
        expect(cover.locator('.rule-item')).to_have_count(1)
        # A re-render (search) keeps the section collapsed and its count follows the search.
        page.get_by_label('Find rules', exact=True).fill('e')
        limits = section(page, 'Limits & rest')
        toggle = limits.get_by_role('button', name=re.compile('^Limits & rest'))
        expect(toggle).to_have_attribute('aria-expanded', 'false')
        expect(limits.locator('.rule-group-body')).to_be_hidden()
        page.get_by_label('Find rules', exact=True).fill('weekly')
        expect(section(page, 'Limits & rest').locator('.gt-count')).to_have_text('1 hidden')  # collapsed: the toggle says so (897da148f)
        expect(section(page, 'Staffing & cover')).to_have_count(0)
        toggle = section(page, 'Limits & rest').get_by_role('button', name=re.compile('^Limits & rest'))
        toggle.click()
        expect(toggle).to_have_attribute('aria-expanded', 'true')
        expect(section(page, 'Limits & rest').locator('.rule-item')).to_have_count(1)
        # Needs attention lists only the waiting rule.
        page.get_by_label('Find rules', exact=True).fill('')
        rule_status(page, 'Needs attention')
        expect(page.locator('.rule-item')).to_have_count(1)
        expect(page.locator('.rule-item')).to_contain_text('Days in a row')
    finally:
        context.close()


def test_empty_filtered_history_and_read_only_states(browser, live_server, h):
    from apps.api import models as m
    from apps.api.db import transaction
    from .governance_support import identity

    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Never authored rule-state department')
        db.add(department)
        db.flush()
        department_id = department.id
        rank = m.Category(department_id=department_id, name='Synthetic practitioners', starts_on=date(2020, 1, 1))
        db.add(rank)
        db.flush()
        db.add_all(m.Membership(department_id=department_id, person_id=person, category_id=rank.id,
                               starts_on=date(2020, 1, 1)) for person in h.seed['people'])
    for role in ('coordinator', 'approver', 'auditor'):
        identity(h, 'empty-' + role, [role], department=department_id)
    context, page = sign_in(browser, live_server, 'empty-coordinator')
    try:
        navigate(page, 'rules')
        expect(page.get_by_role('heading', name='Start with a common rule', exact=True)).to_be_visible()
        cards = page.locator('.rule-starters .rule-card')
        expect(cards.first).to_be_visible()
        expect(page.get_by_role('button', name='Add rule', exact=True)).to_be_visible()
        cards.filter(has_text='Quota or commitment').click()
        form = page.get_by_role('dialog', name='Add rule')
        expect(form.get_by_role('radio', name='Quota or commitment', exact=True)).to_be_checked()
        expect(form.locator('.quota-limits')).to_be_visible()
        page.keyboard.press('Escape')
        expect(form).to_have_count(0)
    finally:
        context.close()
    for role in ('approver', 'auditor'):
        context, page = sign_in(browser, live_server, 'empty-' + role)
        try:
            if role == 'approver':
                navigate(page, 'rules')
                expect(page.get_by_role('heading', name='No rules yet', exact=True)).to_be_visible()
            else:  # an auditor has no Rules page
                page.evaluate("location.hash='rules'")
                expect(page.get_by_role('heading', name='Restricted page', exact=True)).to_be_visible()
            expect(page.get_by_role('button', name='Add rule', exact=True)).to_have_count(0)
        finally:
            context.close()

    a = h.seed['people'][0]
    proposal = h.command('empty-coordinator', 'policy.create', {'name': 'Clinic commitment',
        'owner': 'Test policy owner', 'source': 'Constructed fixture', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
        'definition': {'rule': {**RANGE, 'target_amount': 7}, 'scope': {}, 'severity': 'mandatory',
                       'per_person': [{'person_id': a, 'amount': 5}]}})['record']
    h.command('empty-approver', 'policy.activate', {'reason': 'Independent synthetic review'},
              proposal['id'], proposal['revision'])
    for role in ('coordinator', 'approver'):
        context, page = sign_in(browser, live_server, 'empty-' + role)
        try:
            navigate(page, 'rules')
            sheet = open_rule(page, 'Clinic commitment')
            expect(sheet.locator('.rl-num').first).to_be_visible()
            editable = role == 'coordinator'
            expect(sheet.locator('.rl-exc')).to_have_count(1)  # everyone reads the exceptions
            for name in ('New line', 'Change…'):
                expect(sheet.get_by_role('button', name=name, exact=True)).to_have_count(1 if editable else 0)
            expect(sheet.get_by_role('button', name=re.compile('^Edit line for '))).to_have_count(1 if editable else 0)
            page.keyboard.press('Escape')
            expect(sheet).to_have_count(0)
            expect(page.get_by_role('button', name='Add rule', exact=True)).to_have_count(1 if editable else 0)
            if editable:
                page.get_by_label('Find rules', exact=True).fill('no rule is called this')
                expect(page.get_by_role('heading', name='No matching rules', exact=True)).to_be_visible()
                page.get_by_label('Find rules', exact=True).fill('')
                rule_status(page, 'History')
                expect(page.get_by_role('heading', name='No history yet', exact=True)).to_be_visible()
        finally:
            context.close()


def test_department_scale_rules_page_caps_sections_searches_and_loads_history(browser, live_server, h):
    from apps.api.db import transaction
    from apps.api import models as m
    ids = h.seed['identities']

    def row(i, kind, rule, status):
        return m.Policy(department_id=h.seed['department_id'], name=f'Scale {kind} {i:04}', kind=kind, status=status,
                        definition={'schema_version': 1, 'rule': rule, 'scope': {}, 'severity': 'mandatory', 'exception_allowed': False},
                        owner='Synthetic owner', source='Synthetic department-scale fixture', starts_on=date(2026, 1, 1),
                        created_by=ids['coordinator'], approved_by=ids['approver'])
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([row(i, 'min_rest', {'kind': 'min_rest', 'hours': 11}, 'active') for i in range(420)]
                   + [row(i, 'minimum_cover', {'kind': 'minimum_cover', 'minimum': 1}, 'active') for i in range(330)]
                   + [row(i, 'max_hours', {'kind': 'max_hours', 'hours': 48, 'window': 'week'}, 'retired') for i in range(250)])
    propose(h, {'kind': 'max_consecutive_days', 'maximum': 6}, name='Scale waiting rule')  # through the API as well
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        # 751 authored current rules plus seeded Fair share; retired versions never count.
        expect(page.locator('.rule-summary')).to_have_text('752 rules · 1 needs attention', timeout=30000)
        limits, cover = section(page, 'Limits & rest'), section(page, 'Staffing & cover')
        expect(limits.locator('.gt-count')).to_have_text('421')
        expect(limits.locator('.rule-item')).to_have_count(50)
        expect(cover.locator('.rule-item')).to_have_count(50)
        expect(page.locator('.rule-item')).to_have_count(101)  # Two capped sections plus seeded Fair share.
        cover.get_by_role('button', name='Show 280 more', exact=True).click()
        expect(cover.locator('.rule-item')).to_have_count(330)
        expect(limits.get_by_role('button', name='Show 371 more', exact=True)).to_be_visible()
        page.get_by_label('Find rules', exact=True).fill('Scale minimum_cover 0042')
        expect(page.locator('.rule-item')).to_have_count(1)
        expect(page.locator('.rule-summary')).to_have_text('1 matching rule')
        page.get_by_label('Find rules', exact=True).fill('')
        rule_status(page, 'History')
        expect(page.locator('.rule-summary')).to_have_text('250 rules in history', timeout=30000)
        history = section(page, 'Limits & rest')
        expect(history.locator('.rule-item')).to_have_count(50)
        expect(history.locator('.rl-state').first).to_have_text('Retired')
        history.get_by_role('button', name='Show 200 more', exact=True).click()
        expect(history.locator('.rule-item')).to_have_count(250)
    finally:
        context.close()


def test_rules_row_and_details_tabs_fit_a_phone(browser, live_server, h):
    a = h.seed['people'][0]
    activate(h, propose(h, {**RANGE, 'target_amount': 7}, name='Clinic commitment', starts_on='2026-10-01', ends_on='2026-11-01',
                        per_person=[{'person_id': a, 'amount': 5}]))
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 390, 'height': 844})
    fits = 'document.scrollingElement.scrollWidth <= innerWidth'
    try:
        navigate(page, 'rules')
        item = page.locator('.rule-item').filter(has_text='Clinic commitment')
        expect(item.locator('.rl-text strong')).to_have_text('Between 5–6 and 8 duties each week · aim for 7')  # the line's own minimum reads as a range
        assert page.evaluate(fits)
        item = open_rule(page, 'Clinic commitment')
        tabs = item.get_by_role('tablist', name='Rule information')
        expect(item.locator('.rl-num')).to_have_text(['Minimum5–6duties each week · varies by line', 'Maximum8duties each week', 'Target7duties each week · medium strength'])
        expect(item.locator('.rl-excs')).to_be_visible()
        assert page.evaluate(fits)
        tabs.get_by_role('tab', name='History', exact=True).click()
        expect(item.get_by_text('No earlier versions. This is the first version of this rule.', exact=True)).to_be_visible()
        tabs.get_by_role('tab', name='Source', exact=True).click()
        expect(item.get_by_text('Policy owner', exact=True)).to_be_visible()
        assert page.evaluate(fits)
        tabs.get_by_role('tab', name='Details', exact=True).click()
        item.get_by_role('button', name=re.compile('^Edit line for ')).click()
        dialog = page.get_by_role('dialog', name='Change Clinic commitment')
        expect(dialog.locator('.rl-exc.open').get_by_label('Target', exact=True)).to_be_visible()
        assert page.evaluate(fits)
    finally:
        context.close()

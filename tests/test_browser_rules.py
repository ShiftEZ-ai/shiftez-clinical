"""Rules as defaults with per-rota changes, rule families with bulk decline, rule templates, overrides, protected time and weekly availability."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, save_dialog, condition, planner_tool, open_rule  # noqa: F401
from .test_browser import whole_list
from .rules_support import rule_action, rule_row

pytestmark = pytest.mark.browser


def test_rota_changes_a_department_rule_from_the_planner(browser, live_server, h):
    rest = h.policy({'kind': 'min_rest', 'hours': 12})
    w = h.workspace('Rules rota', '2026-10-04', '2026-10-11')
    w = h.shift(w, '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00', people=[h.seed['people'][0]])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        row = rule_row(panel, rest['name'])
        expect(row.locator('.rk-row-head')).to_be_visible()
        row.locator('.rk-row-head').click()
        rule_action(row, 'Make stricter for this rota')  # a safety rule is only made stricter (452627a96, AGENTS §3)
        row.get_by_label(f"New number for “{rest['name']}” in this rota").fill('14')
        row.get_by_label('Justification', exact=True).fill('Extra rest this rota')
        row.get_by_role('button', name='Use for this rota').click()
        changed = rule_row(panel, rest['name'])
        expect(changed.locator('.rk-row-head')).to_contain_text('Changed for this rota.')
        expect(changed).to_contain_text('Raised from 12 to 14 for this rota')  # the opened row's words (452627a96)
        # The rota's change count is the hint on Rota tools › Rules in this rota.
        panel.get_by_role('button', name='Close rules panel (Esc)', exact=True).click()
        page.get_by_role('button', name='Rota tools', exact=True).click()
        expect(page.get_by_role('menu', name='Rota tools').get_by_role('menuitem', name=re.compile(r'^Rules in this rota'))).to_contain_text('1 change')
        saved = h.detail(w)['workspace']['rule_adjustments']
        assert saved[0]['policy_id'] == rest['id'] and saved[0]['amount'] == 14
    finally:
        context.close()


def test_rule_families_collapse_and_decline_in_bulk(browser, live_server, h):
    for day in ('2026-10-04', '2026-10-11', '2026-10-18'):
        h.command('coordinator', 'policy.create', {'name': f'Imported weekly minimum · {day}', 'owner': 'Import', 'source': 'Reference import', 'starts_on': day,
            'definition': {'severity': 'mandatory', 'exception_allowed': False, 'scope': {}, 'rule': {'kind': 'min_rest', 'hours': 10}}})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        entry = page.locator('.rule-item').filter(has_text='Imported weekly minimum')
        expect(entry).to_have_count(1)
        expect(entry).to_contain_text('3 copies')
        entry.get_by_role('checkbox').check()
        page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Decline 3 proposed').click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Duplicate weekly copies from the import')
        dialog.get_by_role('button', name='Decline rules').click()
        # One bulk change: Preview, then the armed same-button confirm.
        apply = page.locator('.dock-panel.bulk-panel .bulk-foot .button.primary')
        apply.click(); apply.click()
        expect(page.get_by_text('3 rules declined.')).to_be_visible()
        assert all(p['status'] == 'retired' for p in h.get('coordinator', '/api/catalogues')['policies'] if p['name'].startswith('Imported weekly minimum'))
    finally:
        context.close()


def test_staff_weekly_availability_is_approved_by_a_coordinator(browser, live_server, h):
    me = h.get('staff', '/api/me')['person_id']
    assert me == h.seed['people'][0], 'Seeded staff account must be linked to its person'
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page,'requests')
        page.get_by_role('button', name='New request', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_role('radio', name=re.compile('^Unavailable every week')).check()
        dialog.get_by_label('Tuesday').check()
        dialog.get_by_label('Note').fill('Teaching')
        dialog.get_by_role('button', name='Send for approval').click()
        expect(page.get_by_text('Weekly availability sent for approval.')).to_be_visible()
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        page.get_by_role('button', name='Approve', exact=True).click()
        page.get_by_role('dialog').get_by_role('button', name='Approve').click()
        expect(page.get_by_text('Weekly availability approved.')).to_be_visible()
        assert h.get('coordinator', '/api/availability-patterns')['patterns'][0]['status'] == 'active'
    finally:
        context.close()


def test_one_rule_names_several_people_with_the_searchable_picker(browser, live_server, h):
    for i in range(8):  # enough people that the list becomes a picker
        h.configure('people', {'display_name': f'Picker Person {i}'})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Rule title', exact=True).fill('Rest for the picker group')
        form.get_by_label('Source or departmental decision', exact=True).fill('Constructed browser test')
        form.get_by_role('radio', name='Minimum rest', exact=True).check()
        line = form.locator('.cond-line').first
        line.scroll_into_view_if_needed()
        line.get_by_label('Filter on', exact=True).select_option(label='Person')
        form.get_by_label('Search Person').fill('Picker Person')
        form.locator('.picker').get_by_role('button', name='Select all').click()
        form.locator('.picker').get_by_label('Picker Person 3', exact=True).uncheck()
        # The chosen people as chips while they fit, then "+N" for the rest.
        field = line.locator('.pick-button')
        expect(field.locator('.pick-chip')).to_have_count(7)
        expect(field.locator('.pick-more')).to_have_text(f"+{7 - field.locator('.pick-chip:visible').count()}")
        page.keyboard.press('Escape')
        expect(form.locator('.scope-summary')).to_contain_text('Person is any of Picker Person 0')
        save_dialog(form, 'Propose rule')
        rule = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['name'] == 'Rest for the picker group')
        lines = rule['definition']['scope']['conditions']['conditions']
        assert lines[0]['field'] == 'person_ids' and len(lines[0]['values']) == 7 and not rule['definition']['scope'].get('person_ids')
        expect(page.locator('.rule-item').filter(has_text='Rest for the picker group').locator('.rl-who')).to_contain_text('Picker Person 0, Picker Person 1 +5')
    finally:
        context.close()


def test_template_numbers_are_edited_as_rows_and_old_copies_group_into_one_rule(browser, live_server, h):
    a, b, c = h.seed['people'][:3]
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    template = h.policy({'kind': 'max_hours', 'hours': 40}, scope={}, severity='mandatory')
    from apps.api.db import transaction
    from apps.api import models as m
    with transaction(h.app.state.engine, write=True) as db:  # a rule template with one person's own number
        row = db.get(m.Policy, template['id'])
        row.name, row.definition = 'Weekly hours', {**row.definition, 'per_person': [{'person_id': a, 'amount': 30, 'starts_on': None, 'ends_on': None}]}
    for person, hours in ((b, 44), (c, 48)):  # older per-person copies of another rule
        h.policy({'kind': 'max_duties', 'maximum': hours // 10}, scope={'person_ids': [person]})
    with transaction(h.app.state.engine, write=True) as db:
        for p in db.query(m.Policy).filter(m.Policy.kind == 'max_duties'):
            p.name = f"{names[p.definition['scope']['person_ids'][0]]} – Duty cap"
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        entry = page.locator('.rule-item').filter(has_text='Weekly hours')
        expect(entry).to_contain_text('At most 30–40 hours per week')
        sheet = open_rule(page, 'Weekly hours')
        expect(sheet.locator('.rl-exc strong')).to_have_text(names[a])
        expect(sheet.locator('.rl-exc small')).to_have_text('Person · maximum 30')
        sheet.get_by_role('button', name='Change…').click()
        form = page.get_by_role('dialog', name='Change Weekly hours')
        form.get_by_role('button', name=f'Edit line for {names[a]}').click()
        row = form.locator('.rl-exc.open')
        row.get_by_label('Maximum', exact=True).fill('35')
        row.get_by_role('button', name='Save line').click()
        form.get_by_role('button', name='Add a person', exact=True).click()
        added = form.locator('.rl-exc.open')
        added.get_by_label('Person', exact=True).select_option(label=names[b])
        added.get_by_label('Maximum', exact=True).fill('45')
        added.get_by_label('From', exact=True).fill('2026-10-01')
        added.get_by_role('button', name='Save line').click()
        expect(form.locator('.rl-exc small')).to_have_text(['maximum 35', 'maximum 45 · from 1 Oct 2026'])
        save_dialog(form, 'Propose change')
        proposed = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['status'] == 'proposed' and p['kind'] == 'max_hours')
        assert proposed['definition']['replaces_id'] == template['id']
        assert [(r['person_id'], r['amount'], r.get('starts_on')) for r in proposed['definition']['per_person']] == [(a, 35, None), (b, 45, '2026-10-01')]
        h.command('approver', 'policy.activate', {'reason': 'Independent review'}, proposed['id'], proposed['revision'])
        assert next(p for p in h.get('approver', '/api/policies?status=retired')['policies'] if p['id'] == template['id'])

        # The older copies read as one rule with lines; "Make one rule with lines" proposes a template for them.
        page.reload()
        family = page.locator('.rule-item').filter(has_text='Duty cap')
        expect(family).to_contain_text('2 copies')
        open_rule(page, 'Duty cap').get_by_role('button', name='Make one rule with lines').click()
        form = page.get_by_role('dialog', name='Group similar rules')
        expect(form.get_by_role('checkbox', name='Duty cap: 2 rules into one')).to_be_checked()
        form.get_by_label('Reason', exact=True).fill('One rule with a per-person table')
        save_dialog(form, 'Propose grouped rules')
        grouped = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['status'] == 'proposed' and p['kind'] == 'max_duties')
        assert grouped['name'] == 'Duty cap' and len(grouped['definition']['merged_ids']) == 2
    finally:
        context.close()


def test_review_rechecks_safety_when_older_copies_are_selected(browser, live_server, h):
    from .test_browser import wait_held
    from .test_rule_catalogue_api import approve

    payload = {'name': 'Rest standard', 'owner': 'Rule coordinator', 'source': 'Review rest requirements',
               'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'min_rest', 'hours': 11}}}
    old = approve(h, h.command('coordinator', 'policy.create', payload)['record'])
    new = h.command('coordinator', 'policy.create', {**payload,
        'definition': {'rule': {'kind': 'min_rest', 'hours': 9}}})['record']
    assert not new['definition'].get('replaces_id') and not new['definition'].get('merged_ids')
    context, page = sign_in(browser, live_server, 'approver')
    held = []

    def hold_first_preview(route):
        if route.request.method == 'POST' and not held:
            held.append(route)
        else:
            route.continue_()

    try:
        page.route(f"**/api/policies/{new['id']}/review", hold_first_preview)
        navigate(page, 'rules')
        row = page.locator(f'.rule-item[data-rule-ids~="{new["id"]}"]')
        row.get_by_role('button', name=re.compile(r'^Review(?: 1)?$')).click()
        review = page.get_by_role('dialog', name=re.compile(r'^Review '))
        replacement = review.get_by_role('checkbox', name=re.compile(r'^Rest standard'))
        activate = review.get_by_role('button', name='Activate', exact=True)
        warning = 'This proposal reduces mandatory safety protection and needs independent approval.'
        expect(replacement).to_be_checked()
        wait_held(page, held)
        expect(activate).to_be_disabled()
        retry = review.get_by_role('button', name='Retry', exact=True)
        expect(retry).to_have_count(0)
        page.clock.fast_forward(60_000)
        expect(review.get_by_text('Didn’t load in 1:00.', exact=True)).to_be_visible()
        expect(retry).to_be_visible()
        expect(activate).to_be_enabled()
        retry.click()
        expect(review.get_by_text(warning, exact=True)).to_be_visible()
        expect(activate).to_have_accessible_description(warning)
        replacement.uncheck()
        expect(review.get_by_text(warning, exact=True)).not_to_be_visible()
        expect(activate).to_be_enabled()
        replacement.check()
        expect(review.get_by_text(warning, exact=True)).to_be_visible()
        expect(activate).to_have_accessible_description(warning)
        review.get_by_label('Reason', exact=True).fill('Independent rest review')
        activate.click()
        expect(review).to_have_count(0)
    finally:
        context.close()
    assert any(p['id'] == old['id'] for p in h.get('approver', '/api/policies?status=retired')['policies'])
    assert any(p['id'] == new['id'] for p in h.get('approver', '/api/policies?status=active')['policies'])


@pytest.mark.parametrize('own', [True, False], ids=['proposer', 'independent'])
def test_failed_safety_protection_question_only_blocks_the_proposer(browser, live_server, h, own):
    from .governance_support import identity

    reviewer = identity(h, 'proposing_approver', ['coordinator', 'approver'])
    h.command('admin', 'self_approval.grant', {'identity_id': reviewer, 'actions': ['rule.activate'],
        'starts_on': '2020-01-01', 'no_end_date': True, 'reason': 'Synthetic review cover'})
    assert 'rule.activate' in h.get('proposing_approver', '/api/me')['self_approval']
    old = h.policy({'kind': 'min_rest', 'hours': 11})
    new = h.command('proposing_approver', 'policy.create', {
        'name': 'Rest standard', 'owner': 'Rule coordinator', 'source': 'Review rest requirements',
        'starts_on': '2020-01-01',
        'definition': {'replaces_id': old['id'], 'rule': {'kind': 'min_rest', 'hours': 9}}})['record']
    role = 'proposing_approver' if own else 'approver'
    context, page = sign_in(browser, live_server, role)

    def unavailable(route):
        if route.request.method == 'POST':
            route.fulfill(status=503, json={'error': {'code': 'UNAVAILABLE', 'message': 'Protection question unavailable'}})
        else:
            route.continue_()

    try:
        page.route(f"**/api/policies/{new['id']}/review", unavailable)
        navigate(page, 'rules')
        row = page.locator(f'.rule-item[data-rule-ids~="{new["id"]}"]')
        row.get_by_role('button', name=re.compile(r'^Review(?: 1)?$')).click()
        review = page.get_by_role('dialog', name=re.compile(r'^Review '))
        activate = review.get_by_role('button', name='Activate', exact=True)
        message = 'Could not check safety protection.'
        expect(review.get_by_text(message, exact=True)).to_be_visible()
        expect(review.get_by_role('button', name='Retry', exact=True)).to_be_visible()
        expect(activate).to_have_accessible_description(message)
        review.get_by_label('Reason', exact=True).fill('Review the new rest limit')
        if own:
            expect(activate).to_be_disabled()
        else:
            expect(activate).to_be_enabled()
            activate.click()
            expect(review).to_have_count(0)
    finally:
        context.close()
    active = {p['id'] for p in h.get('approver', '/api/policies?status=active')['policies']}
    assert (old['id'] in active) is own
    assert (new['id'] in active) is not own


def test_override_is_proposed_from_the_rule_form_and_refused_when_not_narrower(browser, live_server, h):
    a = h.seed['people'][0]
    base = h.policy({'kind': 'max_hours', 'hours': 40})
    with __import__('apps.api.db', fromlist=['transaction']).transaction(h.app.state.engine, write=True) as db:
        from apps.api import models as m
        db.get(m.Policy, base['id']).name = 'Department hours'
    learners = next(c for c in h.get('coordinator', '/api/catalogues')['categories'] if c['id'] == h.seed['trainee_category'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_role('radio', name='Maximum hours', exact=True).check()
        expect(form.locator('.rule-help')).to_contain_text('Adds up each person’s duty hours')
        form.get_by_role('spinbutton', name='Maximum hours', exact=True).fill('48')
        form.get_by_label('Source or departmental decision', exact=True).fill('Constructed browser test')
        form.get_by_label('Rule title', exact=True).fill('Learner hours')
        form.get_by_role('group', name='Replaces these rules for the people it matches (optional)').get_by_label('Department hours', exact=True).check()
        # Everyone: not narrower than the rule it replaces, so the check refuses it in plain words.
        expect(form.get_by_text('must be more specific than “Department hours”', exact=False)).to_be_visible()
        condition(form, 'Rank', [learners['name']])
        expect(form.get_by_text('Rule settings supported.', exact=False)).to_be_visible()
        save_dialog(form, 'Propose rule')
        rule = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['name'] == 'Learner hours')
        assert rule['definition']['overrides'] == [base['id']]
        h.command('approver', 'policy.activate', {'reason': 'Independent review'}, rule['id'], rule['revision'])
        page.reload()
        sheet = open_rule(page, 'Department hours')
        expect(sheet).to_contain_text('replaced by Learner hours')
        page.keyboard.press('Escape')
        expect(sheet).to_have_count(0)
        page.get_by_role('button', name='Which rule applies?').click()
        dialog = page.get_by_role('dialog', name='Which rule applies?')
        dialog.get_by_label('Person', exact=True).select_option(a)
        expect(dialog.locator('.which-rule-rows')).to_contain_text('Department hours')  # one row per rule (P-06)
    finally:
        context.close()


def test_rule_scope_uses_the_condition_builder_with_a_live_preview(browser, live_server, h):
    a = h.seed['people'][0]
    learners = next(c for c in h.get('coordinator', '/api/catalogues')['categories'] if c['id'] == h.seed['trainee_category'])
    h.command('coordinator', 'membership.create', {'person_id': a, 'category_id': learners['id'], 'starts_on': '2020-01-01'})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_role('radio', name='Maximum hours', exact=True).check()
        form.get_by_label('Source or departmental decision', exact=True).fill('Constructed browser test')
        expect(form.locator('.scope-summary')).to_contain_text('Matches 3 people now.')
        # Everyone except the learners: "Rank is none of Demo learners".
        condition(form, 'Rank', [learners['name']], op='none')
        expect(form.locator('.scope-summary')).to_contain_text('except rank ' + learners['name'])
        expect(form.locator('.scope-summary')).to_contain_text('Matches 2 people now.')
        form.get_by_label('Rule title', exact=True).fill('Hours except learners')
        save_dialog(form, 'Propose rule')
        rule = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['name'] == 'Hours except learners')
        assert rule['definition']['scope']['conditions'] == {'match': 'all', 'conditions': [
            {'field': 'category_ids', 'op': 'none', 'values': [learners['id']]}]}
        expect(page.locator('.rule-item').filter(has_text='Hours except learners')).to_contain_text('except rank ' + learners['name'])
    finally:
        context.close()


def test_propose_rule_fits_a_phone_in_both_form_modes(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 390, 'height': 844})
    wider = """() => { const d = document.querySelector('dialog[open]'); const right = d.getBoundingClientRect().right;
        return [...d.querySelectorAll('*')].filter(e => !e.parentElement.closest('.stepper')  // the step names scroll sideways by design
            && e.getBoundingClientRect().right > right + 1).map(e => e.tagName + '.' + e.className); }"""
    try:
        for mode in ('all', 'steps'):
            page.evaluate("mode => localStorage.setItem('rota.prefs', JSON.stringify({forms: mode}))", mode)
            navigate(page, 'rules')
            page.get_by_role('button', name='Add rule', exact=True).click()
            form = page.get_by_role('dialog')
            expect(form.get_by_role('radio', name='Minimum rest', exact=True)).to_be_attached()
            # The rule cards, the step names and the buttons stay inside the dialog: nothing is cut off at the right.
            assert page.evaluate(wider) == [], mode
            page.keyboard.press('Escape')
            expect(form).to_have_count(0)
    finally:
        context.close()


def test_rule_title_names_who_and_a_goal_scope_is_people_only(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_role('radio', name='Minimum rest', exact=True).check()
        expect(form.get_by_label('Rule title', exact=True)).to_have_value('Minimum rest · 11 hours')
        condition(form, 'Rank', ['Demo learners'])
        expect(form.get_by_label('Rule title', exact=True)).to_have_value('Minimum rest · 11 hours · Rank is Demo learners')
        condition(form, 'Clinic', ['Continuity clinic'])
        # A balance goal chooses people only: the clinic line goes (the goal counts clinics itself) and the days are not said.
        form.get_by_role('radio', name='Balance goal', exact=True).check()
        scope = form.locator('.audience-conditions').first
        assert 'Clinic' not in scope.locator('.cond-line').first.get_by_label('Filter on', exact=True).inner_text()
        expect(form.locator('.scope-summary')).to_have_text(re.compile(r'^Applies to Rank is Demo learners\. '))
        # Duty types or clinics, not both.
        goal = form.locator('[data-goal-builder]')
        goal.get_by_role('button', name=re.compile('^Duty types')).click()
        goal.locator('.picker').get_by_role('option', name='Clinical review session', exact=True).check()
        page.keyboard.press('Escape')
        expect(goal.get_by_role('button', name=re.compile('^Clinics'))).to_be_disabled()
    finally:
        context.close()


def test_convert_preference_to_goal_moves_its_clinic_onto_the_goal(browser, live_server, h):
    clinic = h.seed['services'][0]
    h.policy({'kind': 'allocation_preference', 'priority': 5, 'balance_by': 'duties'},
             {'conditions': {'match': 'all', 'conditions': [{'field': 'service_ids', 'op': 'any', 'values': [clinic]},
                                                           {'field': 'category_ids', 'op': 'any', 'values': [h.seed['trainee_category']]}]}}, severity='advisory')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        # The seeded Fair share sits under Targets & commitments, above Preferences: open this preference by its words.
        page.locator('.rule-item').filter(has_text='Allocation priority 5').locator('.rl-text').click()
        page.locator('.dock-panel.rule-sheet').get_by_role('button', name='Convert to Balance goal…').click()
        # The preference also sets a priority, so it stays; the goal takes its balancing (edit before proposing).
        page.get_by_role('dialog').get_by_role('button', name='Edit before proposing…').click()
        form = page.get_by_role('dialog', name='Add rule')
        goal = form.locator('[data-goal-builder]')
        expect(goal.get_by_role('button', name=re.compile('^Clinics'))).to_contain_text('Continuity clinic')
        expect(form.locator('.scope-summary')).to_contain_text('Rank is Demo learners')
        expect(form.locator('.scope-summary')).not_to_contain_text('Clinic')
        form.get_by_label('Source or departmental decision', exact=True).fill('Converted in a browser test')
        save_dialog(form, 'Propose rule')
        saved = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['kind'] == 'balance_goal')
        assert saved['definition']['rule']['service_ids'] == [clinic]
        # Who: the preference's people, among those who can work at its clinic (the goal shares among them only).
        assert saved['definition']['scope']['conditions']['conditions'] == [{'field': 'category_ids', 'op': 'any', 'values': [h.seed['trainee_category']]},
                                                                           {'field': 'eligible_service_ids', 'op': 'any', 'values': [clinic]}]
    finally:
        context.close()


def test_retry_of_safety_protection_question_renews_a_stale_token_once(browser, live_server, h):
    old = h.policy({'kind': 'min_rest', 'hours': 11})
    new = h.command('coordinator', 'policy.create', {
        'name': 'Rest standard', 'owner': 'Rule coordinator', 'source': 'Review rest requirements',
        'starts_on': '2020-01-01',
        'definition': {'replaces_id': old['id'], 'rule': {'kind': 'min_rest', 'hours': 9}}})['record']
    context, page = sign_in(browser, live_server, 'approver')
    questions, renewals = [], []

    def review(route):
        if route.request.method != 'POST':
            route.continue_()
            return
        questions.append(route.request.post_data_json)
        if len(questions) == 1:
            route.fulfill(status=503, json={'error': {'code': 'UNAVAILABLE', 'message': 'Protection question unavailable'}})
        elif len(questions) == 2:
            route.fulfill(status=403, json={'error': {'code': 'CSRF_FAILED', 'message': 'Refresh your session'}})
        else:
            route.continue_()

    try:
        page.route(f"**/api/policies/{new['id']}/review", review)
        page.on('response', lambda r: renewals.append(r.status) if r.url.endswith('/auth/refresh') else None)
        navigate(page, 'rules')
        row = page.locator(f'.rule-item[data-rule-ids~="{new["id"]}"]')
        row.get_by_role('button', name=re.compile(r'^Review(?: 1)?$')).click()
        dialog = page.get_by_role('dialog', name=re.compile(r'^Review '))
        retry = dialog.get_by_role('button', name='Retry', exact=True)
        expect(retry).to_be_visible()
        retry.click()
        expect(dialog.get_by_text('This proposal reduces mandatory safety protection and needs independent approval.',
                                  exact=True)).to_be_visible()
        expect(retry).to_have_count(0)
        expect(dialog.get_by_role('button', name='Activate', exact=True)).to_be_enabled()
        expect(page.get_by_role('dialog', name='Sign in')).to_have_count(0)
        assert renewals == [200] and len(questions) == 3
        assert questions[1] == questions[2]
    finally:
        context.close()

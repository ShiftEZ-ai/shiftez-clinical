"""Rules made simple: one row per rule grouped by topic, a count that never includes retired versions, History, filters,
a grouped rule's lines edited in place (proposed, with Undo), Group similar rules end to end, overtime switched off
by retired limits, phone width, and the planner showing a grouped rule as one row with each person's number."""
import re

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, planner_tool, open_rule, draft_chip  # noqa: F401
from .test_browser_rules_in_planner import open_planner, MON
from .test_rule_templates import propose, activate
from .rules_support import rule_row, rule_status

pytestmark = pytest.mark.browser


def names(h):
    return {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}


def copies(h):
    """Three older per-person copies of one duty cap (what imports used to make), named after each person."""
    who = names(h)
    return [activate(h, propose(h, {'kind': 'max_duties', 'maximum': n, 'window': 'week'}, {'person_ids': [p]}, name=f'{who[p]} – Duty cap'))
            for p, n in zip(h.seed['people'][:3], (4, 5, 6))]


def policies(h, status=None):
    rows = h.get('coordinator', '/api/catalogues')['policies'] + h.get('coordinator', '/api/policies?status=retired')['policies']
    return [p for p in rows if status is None or p['status'] == status]


def test_in_force_topics_count_history_and_filters(browser, live_server, h):
    activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest between duties'))
    activate(h, propose(h, {'kind': 'minimum_cover', 'minimum': 2}, {'service_ids': [h.seed['services'][0]]}, name='Clinic cover'))
    old = activate(h, propose(h, {'kind': 'max_hours', 'hours': 50, 'window': 'week'}, name='Old weekly hours'))
    h.command('approver', 'policy.retire', {'reason': 'No longer used'}, old['id'], old['revision'])
    propose(h, {'kind': 'run_length', 'minimum': 2, 'maximum': 5}, severity='advisory', name='Runs of days')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        # Four rules (three in force including seeded Fair share, one waiting); retired versions never count.
        expect(page.locator('.rule-summary')).to_have_text('4 rules · 1 needs attention')
        heads = page.locator('.rule-group-name')
        expect(heads).to_have_text(['Staffing & cover', 'Limits & rest', 'Targets & commitments', 'Preferences & fairness'])
        waiting = page.locator('.rule-item').filter(has_text='Runs of days')
        expect(waiting.locator('.rl-mark')).to_have_text('Waiting for approval')
        sheet = open_rule(page, 'Runs of days')
        expect(sheet.locator('.rl-meta')).to_contain_text('Advisory')
        page.keyboard.press('Escape')
        expect(sheet).to_have_count(0)
        expect(page.locator('.rule-item').filter(has_text='Old weekly hours')).to_have_count(0)
        # History holds retired versions only.
        rule_status(page, 'History')
        expect(page.locator('.rule-item')).to_have_count(1)
        expect(page.locator('.rule-item').filter(has_text='Old weekly hours').locator('.rl-state')).to_have_text('Retired')
        rule_status(page, 'Current')
        # Filters: topic (multi-select with search) and who/where (the condition builder).
        page.locator('.rule-toolbar').get_by_role('button', name='Filter', exact=True).click()
        page.get_by_role('button', name=re.compile('^Topic')).click()
        page.locator('.picker').get_by_label('Staffing & cover', exact=True).check()
        page.keyboard.press('Escape')
        expect(page.locator('.rule-item')).to_have_count(1)
        # The shared toolbar shows the active filter on its button, with a count.
        expect(page.locator('.rule-toolbar').get_by_role('button', name='Filter: Staffing & cover (1 active)', exact=True)).to_be_visible()
        # Active filters live inside Filter (no chip row), each with its ×.
        expect(page.locator('.dt-chips')).to_have_count(0)
        page.get_by_role('dialog', name='Filter').get_by_role('button', name='Remove filter Topic: Staffing & cover').click()
        page.keyboard.press('Escape')
        expect(page.locator('.rule-item')).to_have_count(4)
        page.get_by_label('Find rules', exact=True).fill('rest')
        expect(page.locator('.rule-item')).to_have_count(1)
    finally:
        context.close()


def test_group_similar_rules_propose_approve_history_and_retire_restores(browser, live_server, h):
    old = copies(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        expect(page.locator('.rule-item').filter(has_text='Duty cap')).to_contain_text('3 copies')
        page.get_by_role('button', name='Group similar rules').click()
        form = page.get_by_role('dialog', name='Group similar rules')
        expect(form).to_contain_text('Nothing changes until an approver activates each new rule')
        expect(form.get_by_role('checkbox', name='Duty cap: 3 rules into one')).to_be_checked()
        expect(form.locator('.group-count')).to_contain_text('3 rules → 1 rule')
        form.get_by_label('Reason', exact=True).fill('One rule with lines')
        form.get_by_role('button', name='Propose grouped rules').click()
        expect(page.get_by_text('1 rule proposed in place of 3', exact=False)).to_be_visible()
        grouped = next(p for p in policies(h, 'proposed') if p['definition'].get('merged_ids'))
        assert grouped['definition']['listed_only'] and len(grouped['definition']['per_person']) == 3
        expect(page.locator('.rule-item').filter(has_text='Replaces 3 rules')).to_be_visible()
    finally:
        context.close()
    activate(h, grouped)
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        # One rule, read as one: a range and who it reaches; the old copies are only in History.
        expect(page.locator('.rule-item')).to_have_count(2)  # grouped Duty cap plus seeded Fair share
        item = page.locator('.rule-item').filter(has_text='Duty cap')
        expect(item).to_have_count(1)
        expect(item).to_contain_text('At most 4–6 duties per week')
        expect(item).to_contain_text('3 people')
        expect(page.locator('.rule-summary')).to_have_text('2 rules')
        rule_status(page, 'History')
        expect(page.locator('.rule-item').filter(has_text='Duty cap')).to_contain_text('3 retired')
        rule_status(page, 'Current')
        item.locator('.rl-text').click()
        sheet = page.locator('.dock-panel.rule-sheet')
        sheet.get_by_role('tab', name='History', exact=True).click()
        expect(sheet.locator('.rule-history')).to_contain_text('Duty cap')
        sheet.get_by_role('tab', name='Details', exact=True).click()
        sheet.locator('.rl-sheet-foot').get_by_role('button', name='Retire rule').click()
        dialog = page.get_by_role('dialog', name='Retire rule')
        expect(dialog).to_contain_text('The 3 rules it replaced apply again')
        dialog.get_by_label('Reason', exact=True).fill('Back to separate rules')
        dialog.get_by_role('button', name='Retire rule').click()
        expect(page.get_by_text('Rule retired. The 3 rules it replaced apply again.')).to_be_visible()
        expect(sheet).to_have_count(0)
        assert {p['id'] for p in policies(h, 'active')} >= {p['id'] for p in old}
        expect(page.locator('.rule-item').filter(has_text='Duty cap')).to_contain_text('3 copies')
    finally:
        context.close()


def test_lines_are_edited_in_place_as_a_proposal_with_undo(browser, live_server, h):
    a, b, _ = h.seed['people']
    who = names(h)
    rule = activate(h, propose(h, {'kind': 'max_duties', 'maximum': 5, 'window': 'week'}, name='Weekly duties', listed_only=True,
                               per_person=[{'person_id': a, 'amount': 3}, {'person_id': b}]))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        item = page.locator('.rule-item').filter(has_text='Weekly duties')
        expect(item).to_contain_text('At most 3–5 duties per week')
        sheet = open_rule(page, 'Weekly duties')
        expect(sheet.locator('.rule-lines')).to_have_count(0)
        expect(sheet.locator('.rl-sec').filter(has_text='Lines for specific people or programmes')).to_contain_text('The rule applies only to these.')
        expect(sheet.locator('.rl-exc strong')).to_have_text([who[a], who[b]])
        expect(sheet.locator('.rl-exc small')).to_have_text(['Person · maximum 3', 'Person · the rule’s numbers'])  # a line without a number uses the rule's
        sheet.get_by_role('button', name=f'Edit line for {who[b]}').click()
        dialog = page.get_by_role('dialog', name='Change Weekly duties')
        row = dialog.locator('.rl-exc.open')
        expect(row.get_by_label('Maximum', exact=True)).to_have_attribute('placeholder', 'Same')
        row.get_by_label('Maximum', exact=True).fill('4')
        row.get_by_role('button', name='Save line').click()
        expect(dialog.locator('.rl-exc small')).to_have_text(['maximum 3', 'maximum 4'])
        dialog.get_by_role('button', name='Propose change').click()
        expect(dialog).to_have_count(0)
        expect(page.get_by_text('Change proposed.', exact=False)).to_be_visible()
        proposal = next(p for p in policies(h, 'proposed') if p['definition'].get('replaces_id') == rule['id'])
        assert [(r['person_id'], r['amount']) for r in proposal['definition']['per_person']] == [(a, 3), (b, 4)]
        assert proposal['definition']['listed_only']
        page.locator('.toast').get_by_role('button', name='Undo').click()
        expect(page.get_by_text('Change withdrawn.')).to_be_visible()
        assert next(p for p in policies(h) if p['id'] == proposal['id'])['status'] == 'retired'
        assert next(p for p in policies(h) if p['id'] == rule['id'])['status'] == 'active'
    finally:
        context.close()


def test_overtime_switched_off_needs_attention_and_points_at_the_limits_in_force(browser, live_server, h):
    cap = activate(h, propose(h, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 4, 'overtime': 'only',
                                  'starts_on': '2026-01-04', 'ends_on': '2026-07-05'}, name='Overtime ceiling'))
    offer = activate(h, propose(h, {'kind': 'allocation_overtime', 'required_policy_ids': [cap['id']]}, severity='advisory', name='Overtime for nights'))
    # A new version of the ceiling replaces the old one: the offer still points at the retired one.
    new = propose(h, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 4, 'overtime': 'only',
                      'starts_on': '2026-01-04', 'ends_on': '2026-07-05'}, name='Overtime ceiling', replaces_id=cap['id'])
    new = activate(h, new)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        item = page.locator('.rule-item').filter(has_text='Overtime for nights')
        expect(item.locator('.rl-mark')).to_have_text('Needs attention')
        expect(item.locator('.rl-mark')).to_have_attribute('title', 'Overtime switched off: the limits it needs are no longer in force.')
        rule_status(page, 'Needs attention')
        expect(page.locator('.rule-item')).to_have_count(1)
        item.get_by_role('button', name='Switch back on…').click()
        # The server's mapping, in words: what it was, what it will be, and the limits in force it keeps to.
        form = page.get_by_role('dialog', name='Switch overtime back on')
        expect(form.locator('.change-summary')).to_contain_text('Was')
        expect(form.locator('.change-summary')).to_contain_text('Limits: Overtime ceiling')
        form.get_by_label('Reason', exact=True).fill('Point it at the ceiling in force')
        form.get_by_role('button', name='Propose switch-on').click()
        expect(form).to_have_count(0)
        expect(page.get_by_text('Switch-on proposed.', exact=False)).to_be_visible()
        proposal = next(p for p in policies(h, 'proposed') if offer['id'] in (p['definition'].get('merged_ids') or []))
        assert proposal['definition']['rule']['required_policy_ids'] == [new['id']]
    finally:
        context.close()


def test_rules_page_fits_a_phone(browser, live_server, h):
    copies(h)
    activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest between duties'))
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 390, 'height': 844})
    try:
        navigate(page, 'rules')
        sheet = open_rule(page, 'Duty cap')
        expect(sheet.get_by_role('tablist', name='Rule information')).to_be_visible()
        assert page.evaluate('document.scrollingElement.scrollWidth <= innerWidth')
    finally:
        context.close()


def test_planner_shows_a_grouped_rule_once_with_each_persons_number(browser, live_server, h):
    a, b, _ = h.seed['people']
    activate(h, propose(h, {'kind': 'max_duties', 'maximum': 5, 'window': 'week'}, name='Grouped duty cap', listed_only=True,
                        per_person=[{'person_id': a, 'amount': 2}, {'person_id': b}]))
    w = h.shift(h.workspace('Grouped rule rota'), *MON, people=[a, b])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        expect(rule_row(panel, 'Grouped duty cap')).to_have_count(1)
        page.keyboard.press('Escape')
        for person, number, words in ((a, '2 dut', 'their own number'), (b, '5 dut', 'their line')):
            page.locator(draft_chip(person)).first.click()
            side = page.get_by_role('dialog').filter(has_text='Rules applied here')
            row = rule_row(side, 'Grouped duty cap')
            expect(row).to_have_count(1)
            expect(row).to_contain_text(number)  # the folded row's one sentence carries the person's own number
            row.locator('.rk-row-head').click()  # where it comes from is said in the opened row (Why v2, cf5b4115f)
            expect(row).to_contain_text(words)
            expect(row).not_to_contain_text('undefined')
            expect(row).not_to_contain_text('NaN')
            side.get_by_role('button', name='Close panel').click()
    finally:
        context.close()


def catalogue_names(h, kind):
    return {r['id']: r.get('name') or r.get('display_name') for r in h.get('coordinator', '/api/catalogues')[kind]}


def order_rule(h):
    """An allocation order: clinics filled first to last (each line's number is only its place)."""
    s0, s1 = h.seed['services'][:2]
    lines = [{'service_ids': [s1], 'amount': 20}, {'service_ids': [s0], 'amount': 30}, {'assignment_type_ids': [h.seed['type']], 'amount': 10}]
    return activate(h, propose(h, {'kind': 'allocation_preference', 'priority': 0, 'session_priority': 0}, severity='advisory', name='Session order',
                               listed_only=True, line_number='session_priority', per_person=lines))


def test_allocation_order_reads_first_to_last_and_moving_a_line_proposes_a_new_version(browser, live_server, h):
    rule = order_rule(h)
    s0, s1 = h.seed['services'][:2]
    svc, typ = catalogue_names(h, 'services'), catalogue_names(h, 'assignment_types')[h.seed['type']]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        item = page.locator('.rule-item').filter(has_text='Session order')
        expect(item).to_contain_text(f'Fills sessions in this order: {svc[s0]} → {svc[s1]} → {typ}')
        box = open_rule(page, 'Session order').locator('.rule-lines')
        expect(box.locator('h4')).to_have_text('Fills 3 lines in order')
        expect(box.locator('th')).to_have_text(['#', 'Clinics', 'Duty types'])  # no line names anyone: no Who column
        expect(box.locator('tbody tr').first).to_contain_text(svc[s0])
        box.get_by_role('button', name='Edit order').click()
        rows = box.locator('.pp-row')
        expect(rows.first.locator('.pp-rank')).to_have_text('1')
        expect(rows.first.get_by_role('button', name='Move up')).to_be_disabled()
        expect(rows.last.get_by_role('button', name='Move down')).to_be_disabled()
        # Saving the order as it is proposes nothing.
        box.get_by_role('button', name='Propose change').click()
        expect(page.get_by_text('No changes to propose.')).to_be_visible()
        assert not [p for p in policies(h, 'proposed') if p['definition'].get('replaces_id') == rule['id']]
        box.get_by_role('button', name='Edit order').click()
        rows.last.get_by_role('button', name='Move up').click()
        expect(rows.nth(1)).to_contain_text(typ)
        expect(rows.nth(1).get_by_role('button', name='Move up')).to_be_focused()  # focus follows the line
        box.get_by_role('button', name='Propose change').click()
        expect(page.get_by_text('Change proposed.', exact=False)).to_be_visible()
        proposal = next(p for p in policies(h, 'proposed') if p['definition'].get('replaces_id') == rule['id'])
        assert proposal['definition']['line_number'] == 'session_priority'
        assert [(r.get('service_ids') or r.get('assignment_type_ids'), r['amount']) for r in proposal['definition']['per_person']] == [
            ([s0], 30), ([h.seed['type']], 20), ([s1], 10)]
    finally:
        context.close()


def test_ranks_are_a_line_column_and_staffing_lines_name_who_counts(browser, live_server, h):
    s0 = h.seed['services'][0]
    rank = h.seed['trainee_category']
    rule = activate(h, propose(h, {'kind': 'minimum_cover', 'minimum': 2}, name='Clinic cover lines', listed_only=True, per_person=[{'service_ids': [s0]}]))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        box = open_rule(page, 'Clinic cover lines').locator('.rule-lines')
        expect(box.locator('th')).to_have_text(['Who counts', 'Minimum people', 'Clinics'])
        box.get_by_role('button', name='Edit lines').click()
        expect(box.locator('.pp-head')).to_contain_text('Who counts')
        expect(box.locator('.pp-editor > .hint')).to_contain_text('who counts (a person or ranks')
        box.get_by_role('button', name='Add column').click()
        page.get_by_role('menuitem', name='Ranks').click()
        box.locator('.pp-row').first.get_by_role('button', name=re.compile('^Ranks')).click()
        page.locator('.picker').get_by_role('option', name=catalogue_names(h, 'categories')[rank], exact=True).check()
        page.keyboard.press('Escape')
        box.get_by_role('button', name='Propose change').click()
        expect(page.get_by_text('Change proposed.', exact=False)).to_be_visible()
        proposal = next(p for p in policies(h, 'proposed') if p['definition'].get('replaces_id') == rule['id'])
        assert proposal['definition']['per_person'][0]['category_ids'] == [rank]
    finally:
        context.close()
    activate(h, proposal)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        sheet = open_rule(page, 'Clinic cover lines')
        expect(sheet.locator('.rule-lines th')).to_have_text(['Who counts', 'Minimum people', 'Ranks', 'Clinics'])  # the editor's column order
        expect(sheet.locator('.rule-lines tbody')).to_contain_text(catalogue_names(h, 'categories')[rank])
    finally:
        context.close()


def test_overtime_offer_reads_one_row_per_person_with_their_cap(browser, live_server, h):
    a, b, _ = h.seed['people']
    s0 = h.seed['services'][0]
    who, svc, typ = names(h), catalogue_names(h, 'services'), catalogue_names(h, 'assignment_types')[h.seed['type']]
    cap = activate(h, propose(h, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 4, 'overtime': 'only',
                                  'starts_on': '2026-01-04', 'ends_on': '2026-07-05'}, name='Overtime cap lines', listed_only=True, per_person=[{'person_id': a, 'amount': 3}]))
    activate(h, propose(h, {'kind': 'allocation_overtime', 'required_policy_ids': [cap['id']]}, severity='advisory', name='Overtime offers', listed_only=True,
                        per_person=[{'person_id': a, 'weekdays': [1], 'assignment_type_ids': [h.seed['type']]},
                                    {'person_id': a, 'weekdays': [4, 5], 'service_ids': [s0]},
                                    {'person_id': b, 'weekdays': [0]}]))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        box = open_rule(page, 'Overtime offers').locator('.rule-lines')
        expect(box.locator('h4')).to_have_text('Offered to 2 people · 3 lines')
        expect(box.locator('th')).to_have_text(['Who', 'Overtime offered', 'Overtime cap'])
        expect(box.locator('tbody tr')).to_have_count(2)
        row_a = box.locator('tbody tr').filter(has_text=who[a])
        expect(row_a).to_contain_text(f'Tue: {typ} · Fri, Sat: {svc[s0]}')
        expect(row_a).to_contain_text('3 duties')
        row_b = box.locator('tbody tr').filter(has_text=who[b])
        expect(row_b).to_contain_text('Mon: any duty')
        expect(row_b.get_by_role('cell', name='No cap', exact=True)).to_be_visible()  # the cap lists only Dr A
    finally:
        context.close()


def test_a_declined_proposal_reads_declined_not_retired(browser, live_server, h):
    activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest between duties'))
    waiting = propose(h, {'kind': 'max_hours', 'hours': 40, 'window': 'week'}, name='Weekly hours idea')
    h.command('approver', 'policy.decline', {'reason': 'Not agreed'}, waiting['id'], waiting['revision'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        rule_status(page, 'History')
        item = page.locator('.rule-item').filter(has_text='Weekly hours idea')
        expect(item.locator('.rl-state')).to_have_text('Declined')
        expect(page.locator('.rule-summary')).to_have_text('1 rule in history')
    finally:
        context.close()


def balancing(h, name='Fair spread'):
    return activate(h, propose(h, {'kind': 'allocation_preference', 'priority': 0, 'balance_by': 'duties', 'balance_window': 'rolling',
                                   'balance_window_days': 14}, severity='advisory', name=name))


def test_group_dialog_renames_a_group_and_proposes_a_goal_only_when_ticked(browser, live_server, h):
    copies(h)
    preference = balancing(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Group similar rules').click()
        form = page.get_by_role('dialog', name='Group similar rules')
        form.get_by_label('Name of the new rule for Duty cap').fill('Weekly duty cap')
        changes = form.locator('.group-changes')
        expect(changes).to_contain_text('Changes that are not identical')
        expect(changes).to_contain_text('proposed only when you tick it')
        goal = changes.get_by_role('checkbox', name='Balance goal: Fair spread')
        expect(goal).not_to_be_checked()
        expect(changes.locator('.change-summary')).to_contain_text('Was')
        expect(changes.locator('.change-summary')).to_contain_text('Now')
        goal.check()
        expect(form.locator('.group-count')).to_contain_text('3 rules → 1 rule')  # a change is not a group
        form.get_by_label('Reason', exact=True).fill('One rule with lines, and fair shares as a goal')
        form.get_by_role('button', name='Propose grouped rules').click()
        expect(page.get_by_text('2 rules proposed in place of 4', exact=False)).to_be_visible()
        proposed = policies(h, 'proposed')
        assert any(p['name'] == 'Weekly duty cap' and len(p['definition']['merged_ids']) == 3 for p in proposed)
        assert any(p['kind'] == 'balance_goal' and p['definition']['merged_ids'] == [preference['id']] for p in proposed)
    finally:
        context.close()


def test_convert_to_balance_goal_shows_was_and_now_and_undo_withdraws_it(browser, live_server, h):
    preference = balancing(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        open_rule(page, 'Fair spread').get_by_role('button', name='Convert to Balance goal…').click()
        form = page.get_by_role('dialog', name='Convert to Balance goal')
        expect(form.locator('.change-summary')).to_contain_text('fair share')
        expect(form.get_by_role('button', name='Edit before proposing…')).to_be_visible()
        form.get_by_label('Reason', exact=True).fill('Fair shares count history')
        form.get_by_role('button', name='Propose Balance goal').click()
        expect(page.get_by_text('Balance goal proposed.', exact=False)).to_be_visible()
        goal = next(p for p in policies(h, 'proposed') if p['kind'] == 'balance_goal')
        assert goal['definition']['merged_ids'] == [preference['id']]
        page.locator('.toast').get_by_role('button', name='Undo').click()
        expect(page.get_by_text('Proposal withdrawn.')).to_be_visible()
        assert next(p for p in policies(h) if p['id'] == goal['id'])['status'] == 'retired'
    finally:
        context.close()


def test_order_editor_and_group_changes_fit_a_phone(browser, live_server, h):
    order_rule(h)
    copies(h)
    balancing(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 375, 'height': 812})
    try:
        navigate(page, 'rules')
        item = open_rule(page, 'Session order')
        item.get_by_role('button', name='Edit order').click()
        expect(item.locator('.pp-row').first.get_by_role('button', name='Move down')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        item.get_by_role('button', name='Discard changes').click()
        item.get_by_role('button', name='Discard?').click()
        page.keyboard.press('Escape')
        expect(item).to_have_count(0)
        page.get_by_role('button', name='Group similar rules').click()
        form = page.get_by_role('dialog', name='Group similar rules')
        expect(form.locator('.group-changes')).to_be_visible()
        assert form.evaluate('d => d.scrollWidth <= d.clientWidth')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        context.close()


def test_planner_shows_a_rank_lines_number_as_the_persons_number(browser, live_server, h):
    a = h.seed['people'][0]  # every demo person holds the practitioners rank
    activate(h, propose(h, {'kind': 'max_duties', 'maximum': 5, 'window': 'week'}, name='Rank duty cap', listed_only=True,
                        per_person=[{'category_ids': [h.seed['category']], 'amount': 2}]))
    w = h.shift(h.workspace('Rank line rota'), *MON, people=[a])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        page.locator(draft_chip(a)).first.click()
        side = page.get_by_role('dialog').filter(has_text='Rules applied here')
        row = rule_row(side, 'Rank duty cap')
        expect(row).to_contain_text('2 dut')  # the folded row's one sentence carries the person's own number
        row.locator('.rk-row-head').click()  # where it comes from is said in the opened row (Why v2, cf5b4115f)
        expect(row).to_contain_text('their rank’s line')
    finally:
        context.close()

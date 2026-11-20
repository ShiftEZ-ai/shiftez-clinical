"""Rule simplification phase 3 in the browser: a quota as a range (form, sentence, change), lines with their own
settings (read-only columns, editing, phone width), ended rules in Group similar rules (an approver retires them, Undo
restores), changes that propose several rules, Restore in History (with the server's refusal inline, Undo retires
again), and extending a clinic permission (Undo puts the earlier end back)."""
import os
import re

import pytest
from playwright.sync_api import expect
from .test_browser import route_fetch, browser, live_server, sign_in, navigate, save_dialog, open_rule  # noqa: F401
from .test_rule_templates import propose, activate
from .rules_support import rule_status
from packages.contracts.schemas import ENDED_REASON

pytestmark = pytest.mark.browser
SHOTS = os.environ.get('ROTA_SHOTS')  # a folder: keep screenshots while reviewing the UI


def shot(page, name):
    if SHOTS:
        page.screenshot(path=f'{SHOTS}/{name}.png', full_page=False)


def catalogue(h, role='coordinator'):
    return h.get(role, '/api/catalogues')


def names(h):
    return {p['id']: p['display_name'] for p in catalogue(h)['people']}


def ranks(h):
    return {c['name']: c['id'] for c in catalogue(h)['categories']}


def policies(h, status=None):
    rows = h.get('coordinator', '/api/catalogues')['policies'] + h.get('coordinator', '/api/policies?status=retired')['policies']
    return [p for p in rows if status is None or p['status'] == status]


def policy(h, pid):
    return next(p for p in policies(h) if p['id'] == pid)


def test_range_quota_form_round_trip_sentence_and_change(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Rule title', exact=True).fill('Weekly clinical range')
        form.get_by_label('Policy owner', exact=True).fill('Fictional owner')
        form.get_by_label('Source or departmental decision', exact=True).fill('Constructed browser test, not institutional policy')
        form.get_by_label('Effective from', exact=True).fill('2026-10-01')
        form.get_by_role('radio', name='Quota or commitment', exact=True).check()
        period = form.locator('.quota-period')
        period.get_by_label('From', exact=True).fill('2026-10-01')
        period.get_by_label('Until (included)', exact=True).fill('2026-10-31')
        period.get_by_label('Repeat', exact=True).select_option('calendar_week')
        limits = form.locator('.quota-limits')
        expect(limits.get_by_label('Target strength', exact=True)).to_be_hidden()  # no target yet
        limits.get_by_label('Minimum', exact=True).fill('6')
        most = limits.get_by_label('Maximum', exact=True)
        most.fill('5')
        assert 'cannot be below the minimum' in most.evaluate('i => i.validationMessage')
        most.fill('7')
        assert most.evaluate('i => i.validity.valid')
        # A range's lines carry their own minimum and maximum beside the rule's (empty: the rule's).
        form.get_by_role('button', name='Add a person').click()
        row = form.locator('.pp-row').first
        expect(row.get_by_label('Minimum', exact=True)).to_be_visible()
        expect(row.get_by_label('Maximum', exact=True)).to_be_visible()
        form.get_by_role('button', name='Remove this line').click()
        most.scroll_into_view_if_needed()
        shot(page, 'range-form')
        save_dialog(form, 'Propose rule')
        rule = next(p for p in policies(h, 'proposed') if p['name'] == 'Weekly clinical range')
        r = rule['definition']['rule']
        assert (r['comparison'], r['amount'], r['maximum_amount']) == ('range', 6, 7)
    finally:
        context.close()
    activate(h, rule)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        item = page.locator('.rule-item').filter(has_text='Weekly clinical range')
        expect(item).to_contain_text('Between 6 and 7 duties each week')
        expect(page.locator('.rule-group-name')).to_have_text(['Targets & commitments'])  # the seeded Fair share is a target too
        sheet = open_rule(page, 'Weekly clinical range')
        cards = sheet.locator('.rl-num')
        expect(cards.nth(0)).to_have_text('Minimum6duties each week')
        expect(cards.nth(1)).to_have_text('Maximum7duties each week')
        sheet.get_by_role('button', name='Change…').click()
        form = page.get_by_role('dialog', name='Change Weekly clinical range')
        box = form.locator('.quota-limits')
        expect(box.get_by_label('Minimum', exact=True)).to_have_value('6')
        expect(box.get_by_label('Maximum', exact=True)).to_have_value('7')
        box.get_by_label('Maximum', exact=True).fill('8')
        form.get_by_label('Until (included)', exact=True).fill('2026-10-31')
        form.get_by_role('button', name='Propose change').click()
        expect(page.get_by_text('Change proposed.', exact=False)).to_be_visible()
        change = next(p for p in policies(h, 'proposed') if p['definition'].get('replaces_id') == rule['id'])
        assert (change['definition']['rule']['amount'], change['definition']['rule']['maximum_amount']) == (6, 8)
    finally:
        context.close()


def supervision_rule(h):
    a, b, _ = h.seed['people']
    rank = ranks(h)
    return activate(h, propose(h, {'kind': 'supervision_limit', 'mode': 'capacity', 'trainee_category_ids': [rank['Demo learners']], 'maximum': 2},
                               name='Trainee places', listed_only=True,
                               per_person=[{'person_id': a, 'settings': {'mode': 'allowed_ranks', 'trainee_category_ids': [rank['Demo practitioners']]}},
                                           {'person_id': b, 'amount': 3}]))


def test_line_settings_read_as_columns_and_are_edited_in_place(browser, live_server, h):
    a, b, _ = h.seed['people']
    who = names(h)
    rule = supervision_rule(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        lines = open_rule(page, 'Trainee places').locator('.rule-lines')
        # Only the settings some line sets; a line without one reads the rule's.
        expect(lines.locator('th')).to_have_text(['Supervisor', 'Trainees', 'Supervises', 'Trainee ranks'])
        rows = lines.locator('tbody tr')
        # In the sheet each line is a card of labelled values (it never scrolls sideways).
        labelled = lambda *values: [f'{label}\n{value}' for label, value in zip(['Supervisor', 'Trainees', 'Supervises', 'Trainee ranks'], values)]  # noqa: E731
        expect(rows.nth(0).locator('td')).to_have_text(labelled(who[a], '—', 'Only these ranks', 'Demo practitioners'), use_inner_text=True)
        expect(rows.nth(1).locator('td')).to_have_text(labelled(who[b], '3', 'Up to 3 (rule)', 'Demo learners (rule)'), use_inner_text=True)
        shot(page, 'line-settings-table')
        lines.get_by_role('button', name='Edit lines').click()
        editor = lines.locator('.pp-editor')
        expect(editor.locator('.pp-head span')).to_contain_text(['Supervisor', 'Trainees', 'Supervises', 'Trainee ranks'])
        expect(editor.locator('.pp-row').nth(0).get_by_label('Trainees', exact=True)).to_be_disabled()
        editor.locator('.pp-row').nth(1).get_by_label('Supervises', exact=True).select_option('allowed_ranks')
        expect(editor.locator('.pp-row').nth(1).get_by_label('Trainees', exact=True)).to_be_disabled()
        shot(page, 'line-settings-editor')
        lines.get_by_role('button', name='Propose change').click()
        expect(page.get_by_text('Change proposed.', exact=False)).to_be_visible()
        proposal = next(p for p in policies(h, 'proposed') if p['definition'].get('replaces_id') == rule['id'])
        second = proposal['definition']['per_person'][1]
        assert second['person_id'] == b and second['settings']['mode'] == 'allowed_ranks' and second.get('amount') is None
    finally:
        context.close()


def cover_rule(h):
    a, _, _ = h.seed['people']
    s0, s1 = h.seed['services'][:2]
    return activate(h, propose(h, {'kind': 'minimum_cover', 'minimum': 1}, name='Companion cover', listed_only=True,
                               per_person=[{'service_ids': [s0], 'amount': 2, 'settings': {'when_person_ids': [a]}}, {'service_ids': [s1]}]))


def test_when_on_duty_column_is_added_and_saved_and_fits_a_phone(browser, live_server, h):
    a, b, _ = h.seed['people']
    who = names(h)
    rule = cover_rule(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 390, 'height': 844})
    try:
        navigate(page, 'rules')
        lines = open_rule(page, 'Companion cover').locator('.rule-lines')
        expect(lines.locator('th')).to_have_text(['Who counts', 'Minimum people', 'Clinics', 'When on duty'])
        expect(lines.locator('tbody tr').nth(0)).to_contain_text(who[a])
        expect(lines.locator('tbody tr').nth(1)).to_contain_text('Always (rule)')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        shot(page, 'line-settings-phone')
        lines.get_by_role('button', name='Edit lines').click()
        row = lines.locator('.pp-row').nth(1)
        row.get_by_role('button', name=re.compile('^When on duty')).click()
        page.locator('.picker').get_by_label(who[b], exact=True).check()
        page.keyboard.press('Escape')
        expect(row.get_by_role('button', name=re.compile('^When on duty'))).to_contain_text(who[b])
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        shot(page, 'line-settings-phone-editor')
        lines.get_by_role('button', name='Propose change').click()
        expect(page.get_by_text('Change proposed.', exact=False)).to_be_visible()
        proposal = next(p for p in policies(h, 'proposed') if p['definition'].get('replaces_id') == rule['id'])
        assert [(l.get('settings') or {}).get('when_person_ids') for l in proposal['definition']['per_person']] == [[a], [b]]
    finally:
        context.close()


def ended_rule(h):
    return activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Old rest rule', starts_on='2020-01-01', ends_on='2020-02-01'))


def test_group_dialog_ended_rules_are_retired_by_an_approver_with_undo(browser, live_server, h):
    old = ended_rule(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Group similar rules').click()
        dialog = page.get_by_role('dialog', name='Group similar rules')
        ended = dialog.locator('.group-ended')
        expect(ended).to_contain_text('Old rest rule')
        expect(ended).to_contain_text('An approver retires them')
        expect(ended.get_by_role('button', name='Retire Old rest rule')).to_have_count(0)
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Group similar rules').click()
        dialog = page.get_by_role('dialog', name='Group similar rules')
        ended = dialog.locator('.group-ended')
        expect(ended.locator('.change-summary')).to_contain_text('Was')
        # D42: retiring is reversible (Undo in the row), so it acts on the first press.
        with page.expect_request(lambda r: r.url.endswith('/api/commands') and r.method == 'POST') as sent:
            ended.get_by_role('button', name='Retire Old rest rule').click()
        body = sent.value.post_data_json
        assert body['kind'] == 'policy.retire' and body['payload']['reason'].startswith(ENDED_REASON + ': ')
        expect(ended.locator('.badge', has_text='Retired')).to_be_visible()
        shot(page, 'ended-retired')
        assert policy(h, old['id'])['status'] == 'retired'
        # Undo in the row (a toast would sit behind the dialog): the rule applies again, and can be retired again.
        ended.get_by_role('button', name='Undo retiring Old rest rule').click()
        expect(ended.get_by_role('button', name='Retire Old rest rule')).to_be_visible()
        assert policy(h, old['id'])['status'] == 'active'
    finally:
        context.close()


def test_group_dialog_names_new_changes_and_counts_the_rules_a_change_proposes(browser, live_server, h):
    old = ended_rule(h)
    a = activate(h, propose(h, {'kind': 'allocation_preference', 'priority': 0, 'session_priority': 5}, severity='advisory', name='Clinic first'))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        # A change that also proposes a Balance goal (shape from the server contract; the finder is tested server-side).
        def fake(route):
            data = route_fetch(route).json()
            data['changes'] = data.get('changes', []) + [{'shape': 'order', 'rule_ids': [a['id'], old['id']], 'kind': 'allocation_preference',
                'name': 'Allocation order', 'was': 'Two preferences', 'now': 'One order and a Balance goal', 'lines': 2, 'people': 0,
                'starts_on': '2020-01-01', 'ends_on': None, 'payload': {}, 'extra': [{}]}]
            route.fulfill(json=data)
        page.route('**/api/policies/groups', fake)
        navigate(page, 'rules')
        page.get_by_role('button', name='Group similar rules').click()
        dialog = page.get_by_role('dialog', name='Group similar rules')
        change = dialog.locator('.change-row').filter(has=page.get_by_role('checkbox', name='Join the allocation order: Allocation order'))
        expect(change.locator('.change-count')).to_have_text('2 rules → 2')
        expect(dialog.locator('.group-ended')).to_contain_text('An approver retires them')
        assert dialog.evaluate('d => d.scrollWidth <= d.clientWidth')
        shot(page, 'group-dialog-changes')
    finally:
        context.close()


def test_restore_in_history_with_the_servers_refusal_and_undo(browser, live_server, h):
    plain = activate(h, propose(h, {'kind': 'max_hours', 'hours': 50, 'window': 'week'}, name='Weekly hours cap'))
    h.command('approver', 'policy.retire', {'reason': 'By mistake'}, plain['id'], plain['revision'])
    replaced = activate(h, propose(h, {'kind': 'min_rest', 'hours': 10}, name='Rest ten'))
    newer = propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest eleven', replaces_id=replaced['id'])
    activate(h, newer)
    assert policy(h, replaced['id'])['status'] == 'retired'
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        rule_status(page, 'History')
        open_rule(page, 'Rest ten').get_by_role('button', name='Restore…').click()
        dialog = page.get_by_role('dialog', name='Restore rule')
        dialog.get_by_label('Reason', exact=True).fill('Needed again')
        dialog.get_by_role('button', name='Restore rule').click()
        expect(dialog.get_by_text('Rest eleven replaces this rule', exact=False)).to_be_visible()
        shot(page, 'restore-refused')
        dialog.get_by_role('button', name='Cancel').click()
        page.get_by_role('button', name='Discard changes').click()
        page.keyboard.press('Escape')
        expect(page.locator('.dock-panel.rule-sheet')).to_have_count(0)
        open_rule(page, 'Weekly hours cap').get_by_role('button', name='Restore…').click()
        dialog = page.get_by_role('dialog', name='Restore rule')
        dialog.get_by_label('Reason', exact=True).fill('Retired by mistake')
        dialog.get_by_role('button', name='Restore rule').click()
        expect(page.get_by_text('Rule restored. It applies again', exact=False)).to_be_visible()
        assert policy(h, plain['id'])['status'] == 'active'
        page.locator('.toast').filter(has_text='Rule restored.').get_by_role('button', name='Undo').click()
        expect(page.get_by_text('Rule retired again.')).to_be_visible()
        assert policy(h, plain['id'])['status'] == 'retired'
    finally:
        context.close()


def test_extend_a_clinic_permission_until_changed_with_undo(browser, live_server, h):
    person = h.seed['people'][0]
    who = names(h)[person]
    row = next(r for r in catalogue(h, 'credentialer')['eligibilities'] if r['person_id'] == person)
    ended = h.command('credentialer', 'eligibility.end', {'ends_on': '2026-10-01', 'reason': 'Trial month'}, row['id'], row['revision'])['record']
    clinic = next(s['name'] for s in catalogue(h)['services'] if s['id'] == row['service_id'])
    context, page = sign_in(browser, live_server, 'credentialer')
    try:
        navigate(page, 'people')
        page.locator('.dt-table tbody tr', has_text=who).first.get_by_text(who, exact=True).first.click()
        details = page.get_by_role('region', name=who)
        # An ended permission waits behind "Show ended (N)" (SW3-D-14): what holds now comes first.
        details.get_by_role('button', name='Show ended (1)', exact=True).click()
        details.get_by_role('button', name=f'More for {clinic}', exact=True).click()
        page.get_by_role('menu', name=f'More for {clinic}', exact=True).get_by_role('menuitem', name='Change dates', exact=True).click()
        form = page.get_by_role('dialog', name=f'Change dates · {clinic}', exact=True)
        last_day = form.get_by_label('Last day (included, optional)', exact=True)
        expect(last_day).to_be_visible()
        last_day.fill('')
        expect(last_day).to_have_value('')
        form.get_by_label('Reason', exact=True).fill('Permissions continue')
        shot(page, 'change-dates-dialog')
        form.get_by_role('button', name='Save', exact=True).click()
        expect(page.get_by_text(f'{clinic} dates changed.')).to_be_visible()
        now = next(r for r in catalogue(h, 'credentialer')['eligibilities'] if r['id'] == row['id'])
        assert now['ends_on'] is None
        page.locator('.toast').filter(has_text='dates changed').get_by_role('button', name='Undo').click()
        expect(page.get_by_text('Undone. The earlier dates are back.')).to_be_visible()
        back = next(r for r in catalogue(h, 'credentialer')['eligibilities'] if r['id'] == row['id'])
        assert back['ends_on'] == ended['ends_on'] == '2026-10-01'
    finally:
        context.close()

"""Rules decisions preserve their actor, base version, visible scope and unsent work."""
import copy
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from .conftest import TEST_PASSWORD
from .test_browser import browser, condition, live_server, navigate, sign_in  # noqa: F401
from .rules_support import rules_by_family
from .test_browser_rule_catalogue import catalogue_rule, flag, line, selection, set_numbers, weekly
from .test_browser_rules_database import entry, propose_all
from .test_browser_uiux_sessions import switch_account
from .test_rule_catalogue_conflicts import lines_for

pytestmark = pytest.mark.browser


def view(page, name):
    page.locator('#main button.dt-saved').click()
    page.get_by_role('menuitemradio', name=re.compile('^' + re.escape(name))).click()


def stage_max(page, value, name='Department default'):
    page.locator('#main tr[data-row]').filter(has_text=name).locator('input.bulk-check').check()
    set_numbers(page, max=value)
    expect(page.locator('.app-footer')).to_contain_text('1 change not yet proposed')


def ending_rules(h):
    end = str(date.fromisoformat(h.get('coordinator', '/api/rules/catalogue')['day']) + timedelta(days=11))
    catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}}, ends_on=end)])
    catalogue_rule(h, 'people_per_session', [line({'max': {'value': 3, 'level': 'mandatory'}}, ends_on=end)])


def inherited_conflict(h):
    catalogue_rule(h, 'duties_per_month', [
        line({'min': {'value': 1, 'level': 'mandatory'}, 'max': {'value': 20, 'level': 'mandatory'}}, note='Everyone'),
        line({'max': {'value': 0, 'level': 'mandatory'}},
             who={'conditions': [{'field': 'person_ids', 'values': [h.seed['people'][0]]}]}, note='Alex on leave'),
    ])


@pytest.mark.parametrize('role', ['admin', 'approver'])
@pytest.mark.parametrize('expired', [False, True], ids=['signout', 'expiry'])
def test_unsent_rule_edits_and_search_do_not_cross_accounts(browser, live_server, h, role, expired):
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        stage_max(page, 4)
        page.get_by_role('button', name='← Rules').click()
        page.get_by_role('searchbox', name='Search rules').fill('Department default')
        page.evaluate('window.ruleSameDocument = true')
        if expired:
            ident = h.get('coordinator', '/api/me')['identity_id']
            h.command('admin', 'account.end_sessions', {'reason': 'Test expired rule editor'}, ident)
            page.evaluate("window.importBundle('api.js').then(api => api.get('/api/me')).catch(() => null)")
            expect(page.get_by_label('Username or email', exact=True)).to_be_visible()
            page.get_by_label('Username or email', exact=True).fill(role)
            page.get_by_role('button', name='Continue', exact=True).click()
            page.get_by_label('Password', exact=True).fill(TEST_PASSWORD)
            with page.expect_navigation():  # another account after expiry reloads the app (UX-01)
                page.get_by_role('button', name='Sign in', exact=True).click()
            expect(page.get_by_role('button', name=re.compile('^Account menu, '))).to_be_visible()
        else:
            switch_account(page, role)
        navigate(page, 'rules')
        assert page.evaluate('window.ruleSameDocument') is (None if expired else True)
        expect(page.get_by_role('searchbox', name='Search rules')).to_have_value('')
        expect(page.locator('.app-footer')).not_to_contain_text('not yet proposed')
        navigate(page, 'rules/duties_per_week')
        row = page.locator('#main tr[data-row]').filter(has_text='Department default')
        expect(row.locator('td[data-col="n_max"]')).to_contain_text('5 Mandatory')
        expect(row.locator('td[data-col="n_max"]')).not_to_contain_text('4!')
    finally:
        context.close()


def test_an_approved_replacement_with_equal_revision_invalidates_the_old_local_draft(browser, live_server, h):
    flag(h)
    first = weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        stage_max(page, 4)
        lines = copy.deepcopy(first['definition']['rule']['lines'])
        lines[0]['numbers']['max']['value'] = 7
        pending = h.command('admin', 'rule.propose', {
            'catalogue_id': 'duties_per_week', 'expected_policy_id': first['id'],
            'expected_revision': first['revision'], 'lines': lines,
            'acknowledged_overlaps': [lines[0]['id']], 'reason': 'Reviewed replacement',
        })['record']
        current = h.command('approver', 'policy.activate', {'reason': 'Approved replacement'}, pending['id'], pending['revision'])['record']
        assert current['id'] != first['id'] and current['revision'] == first['revision']
        page.get_by_role('button', name='← Rules').click()
        expect(page.locator('.app-footer')).not_to_contain_text('not yet proposed')
        navigate(page, 'rules/duties_per_week')
        expect(page.locator('#main tr[data-row]').filter(has_text='Department default')).to_contain_text('7 Mandatory')
        stage_max(page, 6)
        page.get_by_role('button', name='← Rules').click()
        propose_all(page, 'Edit the latest approved version')
        proposal = entry(h, 'duties_per_week')['proposed']
        assert proposal['definition']['merged_ids'] == [current['id']]
        assert proposal['definition']['rule']['lines'][0]['numbers']['max']['value'] == 6
    finally:
        context.close()


@pytest.mark.parametrize('pick', ['Select all lines shown', 'Select Workload over time'])
@pytest.mark.parametrize('scope', ['search', 'filter'])
def test_filtered_bulk_selection_changes_only_the_visible_line(browser, live_server, h, pick, scope):
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        rules_by_family(h, page)  # Group: Family (Question, A–F, is the default)
        navigate(page, 'rules')
        if scope == 'search':
            page.get_by_role('searchbox', name='Search rules').fill('Return from leave')
        else:
            page.get_by_role('button', name='Filter', exact=True).click()
            condition(page.locator('.dt-pop'), 'Lines: Who', ['People'])
            page.keyboard.press('Escape')
        expect(page.locator('li.rt-ln')).to_have_count(1)
        page.get_by_label(pick, exact=True).check()
        expect(selection(page)).to_contain_text('1 rule · 1 line')
        set_numbers(page, max=2)
        page.get_by_role('searchbox', name='Search rules').fill('')
        propose_all(page, 'Update only the selected person')
        lines = entry(h, 'duties_per_week')['proposed']['definition']['rule']['lines']
        assert [x['numbers']['max']['value'] for x in lines] == [5, 2]
    finally:
        context.close()


def test_a_rule_replaced_under_an_unsent_change_says_so_and_keeps_it_to_compare(browser, live_server, h):
    """The unsent change is never dropped silently: the page says the rule changed, shows the change against the rule
    as it is now, counts it no longer, and forgets it only when the person discards it."""
    flag(h)
    first = weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        stage_max(page, 4)
        lines = copy.deepcopy(first['definition']['rule']['lines'])
        lines[0]['numbers']['max']['value'] = 7
        pending = h.command('admin', 'rule.propose', {
            'catalogue_id': 'duties_per_week', 'expected_policy_id': first['id'],
            'expected_revision': first['revision'], 'lines': lines,
            'acknowledged_overlaps': [lines[0]['id']], 'reason': 'Reviewed replacement',
        })['record']
        h.command('approver', 'policy.activate', {'reason': 'Approved replacement'}, pending['id'], pending['revision'])
        page.get_by_role('button', name='← Rules').click()
        said = page.locator('#main .notice.warning').filter(has_text='Duties per week changed while you were editing')
        for where in ('list', 'rule page'):
            expect(said).to_have_count(1)
            expect(said).to_contain_text('Your unsent change is kept to compare; it is not in the rule.')
            expect(said.locator('.rc-diff')).to_contain_text('Rule now ≤ 7 · your change ≤ 4')  # both sides named, no arrow
            expect(page.locator('.app-footer')).not_to_contain_text('not yet proposed')
            if where == 'list':
                navigate(page, 'rules/duties_per_week')
        expect(page.locator('#main tr[data-row]').filter(has_text='Department default')).to_contain_text('7 Mandatory')
        said.get_by_role('button', name='Discard my unsent change', exact=True).click()  # cannot be undone: armed first
        expect(said).to_have_count(1)
        said.get_by_role('button', name='Confirm? Discard my unsent change', exact=True).click()
        expect(said).to_have_count(0)
        page.get_by_role('button', name='← Rules').click()
        expect(page.get_by_role('searchbox', name='Search rules')).to_be_visible()
        expect(said).to_have_count(0)
        # The person's own proposal is not "changed while you were editing": proposing clears the draft (rules_tree.ts
        # proposeAll, rule_catalogue.ts propose button) before any read sees the rule's proposal.
        navigate(page, 'rules/duties_per_week')
        stage_max(page, 6)
        page.get_by_role('button', name='← Rules').click()
        propose_all(page, 'My own change')
        assert entry(h, 'duties_per_week')['proposed'] is not None
        navigate(page, 'rules/duties_per_week')
        expect(page.locator('#main .rc-summary')).to_contain_text('In force now:')  # read after the proposal
        expect(page.locator('#main .notice.warning').filter(has_text='changed while you were editing')).to_have_count(0)
    finally:
        context.close()


def test_attention_fix_preserves_other_unsent_numbers(browser, live_server, h):
    flag(h)
    inherited_conflict(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_month')
        stage_max(page, 2, 'Alex on leave')
        page.get_by_role('button', name='← Rules').click()
        view(page, 'Needs attention')
        page.get_by_role('button', name='Give line 2 no min (None)', exact=True).click()
        propose_all(page, 'Keep the unsent maximum and remove inherited minimum')
        numbers = entry(h, 'duties_per_month')['proposed']['definition']['rule']['lines'][1]['numbers']
        assert numbers == {'max': {'value': 2, 'level': 'mandatory'}, 'min': {'value': None, 'level': 'mandatory'}}
    finally:
        context.close()


def test_attention_remove_refuses_to_discard_an_edited_line(browser, live_server, h):
    flag(h)
    _, groups = lines_for(h, 'tag and rank')
    groups[1]['note'] = 'Edited group line'
    first = catalogue_rule(h, 'duties_per_week', groups)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        stage_max(page, 2, 'Edited group line')
        page.get_by_role('button', name='← Rules').click()
        view(page, 'Needs attention')
        page.get_by_role('button', name='Remove line 2', exact=True).click()
        expect(page.locator('.toast.warning')).to_contain_text('conflicts with unsent edits')
        expect(page.locator('tr.rt-issue')).to_have_count(1)
        propose_all(page, 'Preserve the edited person line')
        lines = entry(h, 'duties_per_week')['proposed']['definition']['rule']['lines']
        assert len(lines) == 2 and lines[1]['id'] == first['definition']['rule']['lines'][1]['id']
        assert lines[1]['numbers']['max']['value'] == 2
    finally:
        context.close()


def test_bulk_acknowledgement_is_explicit_cancelable_and_keeps_the_note(browser, live_server, h):
    flag(h)
    ending_rules(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        view(page, 'Needs attention')
        page.get_by_label('Select every issue shown').check()
        selection(page).get_by_role('button', name='Apply recommended').click()
        dialog = page.get_by_role('dialog', name='Apply recommended fixes')
        expect(dialog).to_contain_text('2 issues will be acknowledged for everyone now')
        expect(dialog).to_contain_text('No rule change will be proposed')
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        assert all(x['acknowledged'] is None for x in h.get('coordinator', '/api/rules/catalogue')['attention'])
        expect(page.locator('tr.rt-issue')).to_have_count(2)
        selection(page).get_by_role('button', name='Apply recommended').click()
        dialog.get_by_label('Reason', exact=True).fill('Both end with this rota')
        dialog.get_by_role('button', name='Apply', exact=True).click()
        expect(page.get_by_role('heading', name='Nothing needs attention')).to_be_visible()
        assert {x['acknowledged']['note'] for x in h.get('coordinator', '/api/rules/catalogue')['attention']} == {'Both end with this rota'}
        expect(page.locator('.toast').filter(has_text='added to the proposal')).to_have_count(0)
        expect(page.locator('.app-footer')).not_to_contain_text('not yet proposed')
    finally:
        context.close()


def test_resolve_only_bulk_selection_does_not_claim_a_staged_fix(browser, live_server, h):
    flag(h)
    catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 1, 'level': 'mandatory'}})])
    workspace = h.workspace(name='Rota requiring attention', start='2026-10-04', end='2026-10-11')
    for day in ('2026-10-05', '2026-10-06'):
        workspace = h.shift(workspace, f'{day}T08:00:00+03:00', f'{day}T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        view(page, 'Needs attention')
        expect(page.locator('tr.rt-issue')).to_have_count(1)
        page.get_by_label('Select every issue shown').check()
        selection(page).get_by_role('button', name='Apply recommended').click()
        expect(page.locator('.toast')).to_contain_text('must be opened individually')
        expect(page.locator('.toast').filter(has_text='added to the proposal')).to_have_count(0)
        expect(page.locator('.app-footer')).not_to_contain_text('not yet proposed')
        assert entry(h, 'duties_per_week')['proposed'] is None
        assert all(x['acknowledged'] is None for x in h.get('coordinator', '/api/rules/catalogue')['attention'])
    finally:
        context.close()


def test_attention_counts_and_empty_copy_track_local_fix_undo_and_discard(browser, live_server, h):
    flag(h)
    inherited_conflict(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        view(page, 'Needs attention')
        saved = page.locator('#main button.dt-saved')

        def attention(n):  # the views menu lists each view's count 
            saved.click()
            item = page.get_by_role('menu', name='Saved views').get_by_role('menuitemradio', name=re.compile('^Needs attention'))
            expect(item).to_contain_text(str(n))
            page.keyboard.press('Escape')
            expect(page.get_by_role('menu', name='Saved views')).to_have_count(0)

        expect(saved).to_have_attribute('aria-label', 'Saved views: Needs attention')
        attention(2)  # RU-02 counts lines: the one inherited conflict names Alex's line and the Everyone line
        apply = page.get_by_role('button', name='Give line 2 no min (None)', exact=True)
        apply.click()
        attention(0)
        expect(page.locator('#main')).to_contain_text('the rules in force have not changed')
        expect(page.locator('.app-footer')).to_contain_text('1 change not yet proposed')
        page.locator('.toast').filter(has_text='Give line 2 no min').get_by_role('button', name='Undo', exact=True).click()
        attention(2)
        apply.click()
        page.get_by_role('button', name='Discard changes', exact=True).click()
        page.get_by_role('button', name='Discard all? Discard changes', exact=True).click()
        attention(2)
        expect(page.locator('.app-footer')).not_to_contain_text('not yet proposed')
    finally:
        context.close()


def test_attention_search_finds_the_visible_person_and_clinic(browser, live_server, h):
    flag(h)
    catalogues = h.get('coordinator', '/api/catalogues')
    person = next(x for x in catalogues['people'] if x['id'] == h.seed['people'][0])
    clinic = next(x for x in catalogues['services'] if x['id'] == h.seed['services'][0])
    end = str(date.fromisoformat(h.get('coordinator', '/api/rules/catalogue')['day']) + timedelta(days=11))
    catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}},
        who={'conditions': [{'field': 'person_ids', 'values': [person['id']]}]},
        where={'conditions': [{'field': 'service_ids', 'values': [clinic['id']]}]}, ends_on=end)])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        view(page, 'Needs attention')
        search = page.get_by_role('searchbox', name='Search issues')
        for query in (person['display_name'], clinic['name']):
            search.fill(query)
            expect(page.locator('tr.rt-issue')).to_have_count(1)
        page.get_by_role('button', name='Let it end · acknowledge…', exact=True).click()
        page.get_by_role('dialog', name='Acknowledge 1 issue').get_by_role('button', name='Acknowledge', exact=True).click()
        view(page, 'Acknowledged')
        for query in (person['display_name'], clinic['name']):
            search.fill(query)
            expect(page.locator('tr.rt-issue')).to_have_count(1)
    finally:
        context.close()


def test_bulk_take_back_undo_restores_each_issues_own_note(browser, live_server, h):
    flag(h)
    ending_rules(h)
    issues = h.get('coordinator', '/api/rules/catalogue')['attention']
    notes = {x['fingerprint']: 'Reason for ' + x['catalogue_id'] for x in issues}
    for fingerprint, note in notes.items():
        h.command('coordinator', 'rule.acknowledge', {'fingerprints': [fingerprint], 'note': note})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        view(page, 'Acknowledged')
        page.get_by_label('Select every issue shown').check()
        selection(page).get_by_role('button', name='Take back', exact=True).click()
        expect(page.get_by_role('heading', name='Nothing acknowledged')).to_be_visible()
        page.locator('.toast').filter(has_text='2 acknowledgements taken back').get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('tr.rt-issue')).to_have_count(2)
        after = h.get('coordinator', '/api/rules/catalogue')['attention']
        assert {x['fingerprint']: x['acknowledged']['note'] for x in after} == notes
    finally:
        context.close()


def test_saved_attention_search_survives_acknowledge_take_back_and_resets_explicitly(browser, live_server, h):
    flag(h)
    ending_rules(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        view(page, 'Needs attention')
        search = page.get_by_role('searchbox', name='Search issues')
        search.fill('Duties')
        search.press('Enter')
        page.locator('#main button.dt-saved').click()
        page.get_by_role('menuitem', name='New view…', exact=True).click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('Ending duties')
        dialog.get_by_role('button', name='Save view', exact=True).click()
        saved = page.locator('#main button.dt-saved')
        expect(saved).to_have_attribute('aria-label', 'Saved views: Ending duties')
        search.fill('Duties per')
        search.press('Enter')
        expect(saved).to_have_attribute('aria-label', 'Saved views: Ending duties (changed)')
        search.fill('Duties')
        search.press('Enter')
        page.get_by_role('button', name='Let it end · acknowledge…', exact=True).click()
        page.get_by_role('dialog', name='Acknowledge 1 issue').get_by_role('button', name='Acknowledge', exact=True).click()
        expect(page.get_by_role('heading', name='No matching issues')).to_be_visible()
        expect(search).to_have_value('Duties')
        expect(saved).to_have_attribute('aria-label', 'Saved views: Ending duties')
        view(page, 'Acknowledged')
        page.locator('tr.rt-issue').get_by_role('button', name=re.compile('^Take back')).click()
        expect(page.get_by_role('heading', name='Nothing acknowledged')).to_be_visible()
        expect(search).to_have_value('Duties')
        view(page, 'Needs attention')
        expect(page.locator('tr.rt-issue')).to_have_count(1)
        saved.click()
        page.get_by_role('menuitem', name='Reset to default', exact=True).click()
        expect(search).to_have_value('')
        expect(page.locator('tr.rt-issue')).to_have_count(2)
    finally:
        context.close()

"""The Rules page as one database (mockups R8–R10, docs/design/rule-catalogue.md §6, §8): lines selected across rules
change together from one selection bar and are proposed together for another person to approve; a line opens in the
editor docked beside its rule's table; Needs attention applies recommended fixes into the same proposal; read-only
roles read it all and change nothing."""
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from apps.api.services.rule_catalogue import attention_counts
from .test_rule_catalogue_conflicts import lines_for
from .test_browser import add_filter, browser, live_server, navigate, sign_in, route_fetch  # noqa: F401
from .rules_support import rules_by_family
from .test_browser_rule_catalogue import catalogue_rule, flag, line, selection, set_numbers, weekly
from .browser_acceptance_support import shot

pytestmark = pytest.mark.browser


def entry(h, key, role='approver'):
    return next(e for e in h.get(role, '/api/rules/catalogue')['entries'] if e['id'] == key)


def propose_all(page, text):
    page.locator('.topbar').get_by_role('button', name=re.compile(r'^Propose \d+ changes?$')).click()
    dialog = page.get_by_role('dialog', name=re.compile('^Propose'))
    expect(dialog.get_by_label('I have read which lines give way and that is intended')).to_have_count(0)
    dialog.get_by_label('Reason', exact=True).fill(text)
    dialog.get_by_role('button', name='Propose', exact=True).click()
    expect(dialog).to_have_count(0)
    return dialog


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_lines_selected_across_rules_change_together_and_an_approver_approves(browser, live_server, h, width, scheme):
    flag(h)
    weekly(h)
    catalogue_rule(h, 'people_per_session', [line({'max': {'value': 3, 'level': 'mandatory'}}, note='Session cap')])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        page.emulate_media(color_scheme=scheme)
        rules_by_family(h, page)  # Group: Family (Question, A–F, is the default)
        navigate(page, 'rules')
        main = page.locator('#main')
        main.get_by_role('button', name='All rules').click()
        expect(page.locator('.topbar .page-actions')).to_have_attribute('data-fitted', '1')
        main.get_by_label('Select line 1 of Duties per week').check()
        main.get_by_label('Select line 1 of People per session').check()
        # Tri-state: the rule with one of its two lines picked is mixed, and so is its family and select-all.
        expect(main.get_by_label('Select Duties per week', exact=True)).to_have_attribute('aria-checked', 'mixed')
        expect(main.get_by_label('Select Workload over time')).to_have_attribute('aria-checked', 'mixed')
        expect(main.get_by_label('Select People per session', exact=True)).to_be_checked()
        expect(main.get_by_label('Select all lines shown')).to_have_attribute('aria-checked', 'mixed')
        bar = selection(page)
        expect(bar).to_contain_text('2 rules · 2 lines')
        shot(page, f'rules-selected-{width}-{scheme}')
        set_numbers(page, max=4)  # checked by the server before it is applied

        expect(main.locator('li.rt-ln.rc-changed')).to_have_count(2)
        expect(main.locator('li.rt-ln.rc-changed').first.locator('.rt-ln-num')).to_contain_text('≤ 4')
        expect(page.locator('.app-footer')).to_contain_text('2 changes not yet proposed')
        expect(main.locator('li.rt-item').filter(has_text='Duties per week')).to_contain_text('1 change')
        expect(page.locator('.topbar').get_by_role('button', name='Propose 2 changes', exact=True)).to_be_in_viewport(ratio=1)
        shot(page, f'rules-changed-actions-{width}-{scheme}')
        # One proposal per rule, sent together; the line for everyone still gives way to Alex Morgan's.
        propose_all(page, 'Busier winter')
        expect(main.locator('li.rt-item').filter(has_text='Waiting for approval')).to_have_count(2)
        expect(page.locator('.topbar').get_by_role('button', name='Propose', exact=True)).to_be_disabled()
    finally:
        context.close()
    assert not errors
    context, page = sign_in(browser, live_server, 'approver', {'width': width, 'height': 900})
    try:
        navigate(page, 'rules/duties_per_week')
        page.locator('.rc-waiting').get_by_role('button', name='Review and approve').click()
        # B5 (f1d80dcc4): the approver decides in the docked panel, named after the change, beside the proposer's reason;
        # an independent approval is one step without a reason of its own (BE-9).
        decision = page.get_by_role('region', name='Change a line for everyone', exact=True)
        expect(decision.locator('.rc-quote')).to_have_text('Busier winter')
        decision.get_by_role('button', name='Approve and activate', exact=True).click()
        expect(decision).to_have_count(0)
        expect(page.locator('.rc-waiting')).to_have_count(0)
    finally:
        context.close()
    week = entry(h, 'duties_per_week')['rule']['definition']['rule']['lines']
    assert [x['numbers']['max']['value'] for x in week] == [4, 3]
    assert entry(h, 'people_per_session')['proposed']['definition']['rule']['lines'][0]['numbers']['max']['value'] == 4


def test_a_line_opens_beside_the_table_moves_with_the_arrows_and_keeps_an_edit(browser, live_server, h):
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        main, panel = page.locator('#main'), page.locator('.rc-line-panel')
        alex = main.locator('tr[data-row]').filter(has_text='Alex Morgan')
        alex.locator('td[data-col="who"]').click()
        expect(panel.get_by_role('heading', level=2)).to_have_text('Alex Morgan at all clinics · ≤ 3 · Mandatory')
        expect(panel).to_contain_text('Duties per week › Line 2 of 2')  # the table's order: everyone first
        expect(alex).to_have_class(re.compile('is-explained'))
        panel.get_by_role('button', name='Previous line').click()
        expect(panel.get_by_role('heading', level=2)).to_have_text('Everyone at all clinics · ≤ 5 · Mandatory')
        expect(main.locator('tr[data-row]').filter(has_text='Department default')).to_have_class(re.compile('is-explained'))
        panel.get_by_role('button', name='Next line').click()
        # Where: a Clinic filter; Numbers: max 2; Keep change puts it in the draft (the table follows).
        where = panel.locator('.rc-lp-section').nth(1)
        add_filter(where)
        where.locator('.cond-line .pick-button').click()
        page.locator('.picker').get_by_role('option', name='Continuity clinic', exact=True).check()
        page.keyboard.press('Escape')
        panel.get_by_label('Maximum', exact=True).fill('2')
        panel.get_by_role('button', name='Keep change').click()
        expect(panel.get_by_role('heading', level=2)).to_have_text('Alex Morgan at Continuity clinic · ≤ 2 · Mandatory')
        expect(alex.get_by_label(re.compile('^Max: '))).to_have_text('2 Mandatory')
        expect(page.locator('.app-footer')).to_contain_text('1 change not yet proposed')
        shot(page, 'line-panel')
        # Inline editing still works beside it.
        everyone = main.locator('tr[data-row]').filter(has_text='Department default')
        everyone.get_by_label(re.compile('^Max: ')).click()
        page.get_by_role('dialog', name='Edit Max').get_by_label('Max', exact=True).fill('6')
        page.get_by_role('dialog', name='Edit Max').get_by_label('Max', exact=True).press('Enter')
        expect(page.locator('.app-footer')).to_contain_text('2 changes not yet proposed')
        # The change is kept with the Rules page: the list counts it, and coming back finds it.
        main.get_by_role('button', name='← Rules').click()
        expect(page.locator('.app-footer')).to_contain_text('2 changes not yet proposed')
        main.locator('li.rt-item').get_by_role('button', name='Duties per week', exact=True).click()  # within the Rules page: no question
        expect(main.locator('tr[data-row]').filter(has_text='Alex Morgan').get_by_label(re.compile('^Max: '))).to_have_text('2 Mandatory')
        # Remove line (press twice), then Esc closes the panel.
        main.locator('tr[data-row]').filter(has_text='Alex Morgan').locator('td[data-col="where"]').click()
        remove = panel.get_by_role('button', name='Remove line')
        remove.click()
        panel.get_by_role('button', name=re.compile('Remove this line')).click()
        expect(main.locator('tr[data-row]')).to_have_count(1)
        expect(panel).to_be_hidden()
    finally:
        context.close()


def test_exact_mandatory_line_reads_exactly_in_its_rule_row(browser, live_server, h):
    """the one-row Rules list keeps the row to one count line and leaves both stored bounds visible when opened."""
    flag(h)
    catalogue_rule(h, 'duties_per_month', [line({
        'min': {'value': 16, 'level': 'mandatory'},
        'max': {'value': 16, 'level': 'mandatory'},
    })])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        item = page.locator('li.rt-item').filter(has_text='Duties per month')
        expect(item.locator('.rt-sentence')).to_have_count(0)
        expect(item.locator('.rt-meta')).to_contain_text('1 line')
        page.get_by_role('button', name='Duties per month lines').click()
        expect(page.locator('li.rt-ln .rt-ln-num')).to_contain_text(re.compile(r'16\D+16'))
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_needs_attention_applies_recommended_fixes_into_the_proposal(browser, live_server, h, width, scheme):
    flag(h)
    today = date.fromisoformat(h.get('coordinator', '/api/rules/catalogue')['day'])
    a = h.seed['people'][0]
    # Alex's leave line sets only "max 0": blank inherits the monthly minimum for everyone (a conflict); another line
    # ends within 30 days.
    catalogue_rule(h, 'duties_per_month', [line({'min': {'value': 1, 'level': 'mandatory'}, 'max': {'value': 20, 'level': 'mandatory'}}, note='Everyone line'),
                                           line({'max': {'value': 0, 'level': 'mandatory'}}, who={'conditions': [{'field': 'person_ids', 'values': [a]}]}, note='Alex on leave')])
    catalogue_rule(h, 'people_per_session', [line({'max': {'value': 3, 'level': 'mandatory'}}, ends_on=str(today + timedelta(days=11)))])
    found = h.get('coordinator', '/api/rules/catalogue')['attention']
    assert [(x['kind'], x['fixes'][0]['kind']) for x in found] == [('inherits', 'none'), ('ending', 'acknowledge')]
    assert all(x['acknowledged'] is None for x in found)
    ending_issue = next(x for x in found if x['kind'] == 'ending')
    h.command('coordinator', 'rule.acknowledge', {'fingerprints': [ending_issue['fingerprint']], 'note': 'End date reviewed'})
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules')
        main = page.locator('#main')
        # RU-02 counts lines: the inherits issue names Alex's line and the Everyone line, the ending issue its own line.
        # F-R1: said once, on the saved-view button (one warning icon), not again in the footer.
        expect(main.locator('button.dt-saved .dt-view-warn')).to_have_text('3 lines need attention')
        expect(main.locator('button.dt-saved .dt-view-warn svg')).to_have_count(1)
        expect(page.locator('.app-footer')).not_to_contain_text('need attention')
        main.locator('button.dt-saved').click()
        page.get_by_role('menuitemradio', name=re.compile('^Acknowledged')).click()
        acked = main.locator('tr.rt-issue')
        expect(acked).to_have_count(1)
        expect(acked).to_contain_text('End date reviewed')
        acked.get_by_role('button', name=re.compile('^Take back')).click()  # back to Needs attention, for everyone
        expect(main.get_by_role('heading', name='Nothing acknowledged')).to_be_visible()
        main.locator('button.dt-saved').click()
        page.get_by_role('menuitemradio', name=re.compile('^Needs attention')).click()
        rows = main.locator('tr.rt-issue')
        expect(rows).to_have_count(2)
        expect(rows.filter(has_text='gives way')).to_have_count(0)
        conflict = rows.nth(0)
        expect(conflict).to_contain_text('Line 2 inherits min 1 from line 1, which contradicts its own numbers')
        expect(conflict).to_contain_text('★ Recommended')
        expect(conflict).to_contain_text('Give line 2 no min (None)')
        expect(rows.nth(1)).to_contain_text('(10 days)')
        shot(page, f'needs-attention-{width}-{scheme}')
        # Select all (the header box), Apply recommended: None goes into the draft, the others are acknowledged.
        main.get_by_label('Select every issue shown').check()
        selection(page).get_by_role('button', name='Apply recommended').click()
        dialog = page.get_by_role('dialog', name='Apply recommended fixes')
        expect(dialog).to_contain_text('1 issue will be acknowledged for everyone now')
        expect(dialog).to_contain_text('1 fix will be staged locally')
        dialog.get_by_role('button', name='Apply', exact=True).click()
        expect(main.get_by_text('Nothing needs attention')).to_be_visible()
        expect(page.locator('.app-footer')).to_contain_text('1 change not yet proposed')
        propose_all(page, 'No monthly minimum while Alex is on leave')
        main.locator('button.dt-saved').click()
        page.get_by_role('menuitemradio', name=re.compile(r'^All rules\b')).click()
        expect(main.locator('li.rt-item').filter(has_text='Duties per month')).to_contain_text('Waiting for approval')
    finally:
        context.close()
    assert not errors
    lines = entry(h, 'duties_per_month')['proposed']['definition']['rule']['lines']
    assert lines[1]['numbers'] == {'max': {'value': 0, 'level': 'mandatory'}, 'min': {'value': None, 'level': 'mandatory'}}


def test_an_inherited_number_shows_its_value_and_source_and_none_removes_it(browser, live_server, h):
    """Blank inherits, shown (owner, 2026-10-04): in the database, on the rule page and in the line editor an inherited
    number reads as the number, muted with ↰ and its source line in the tooltip; None clears it explicitly."""
    flag(h)
    a = h.seed['people'][0]
    catalogue_rule(h, 'duties_per_month', [line({'min': {'value': 1, 'level': 'mandatory'}, 'max': {'value': 20, 'level': 'mandatory'}}, note='Everyone line'),
                                           line({'max': {'value': 10, 'level': 'mandatory'}}, who={'conditions': [{'field': 'person_ids', 'values': [a]}]}, note='Alex line')])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        main.get_by_role('button', name='Duties per month lines').click()
        alex = main.locator('li.rt-ln').filter(has=page.locator('.rp.person'))
        expect(alex.locator('.rt-ln-num')).to_have_text('1↰ – 10')
        expect(alex.locator('.rc-inh')).to_have_attribute('data-tip', 'Inherited from Everyone · All clinics')
        main.locator('li.rt-item').get_by_role('button', name='Duties per month', exact=True).click()
        row = main.locator('tr[data-row]').filter(has_text='Alex line')
        expect(row.get_by_label(re.compile('^Min: '))).to_have_text('1↰ Mandatory')
        row.locator('td[data-col="who"]').click()
        panel = page.locator('.rc-line-panel')
        expect(panel.get_by_role('heading', level=2)).to_have_text('Alex Morgan at all clinics · 1↰ – 10 · Mandatory')
        expect(panel.get_by_label('Minimum', exact=True)).to_have_attribute('placeholder', '1↰')
        expect(panel.get_by_label('Minimum', exact=True)).to_have_attribute('title', '↰ 1 from Everyone · All clinics')
        panel.get_by_role('button', name='None').first.click()
        panel.get_by_role('button', name='Keep change').click()
        expect(row.get_by_label(re.compile('^Min: '))).to_have_text('None')
        page.locator('.topbar').get_by_role('button', name='Propose 1 change').click()
        dialog = page.get_by_role('dialog', name=re.compile('^Propose'))
        expect(dialog.get_by_label('I have read which lines give way and that is intended')).to_have_count(0)
        dialog.get_by_label('Reason', exact=True).fill('No minimum for Alex')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        expect(main.locator('.rc-waiting')).to_be_visible()
    finally:
        context.close()
    lines = entry(h, 'duties_per_month')['proposed']['definition']['rule']['lines']
    assert lines[1]['numbers']['min']['value'] is None and lines[1]['numbers']['max']['value'] == 10


@pytest.mark.parametrize('role', ['auditor', 'approver'])
def test_read_only_roles_read_the_database_and_needs_attention_without_changing_anything(browser, live_server, h, role):
    flag(h)
    weekly(h)
    _, groups = lines_for(h, 'tag and rank')
    catalogue_rule(h, 'duties_per_month', groups)
    both = next(x for x in h.get('coordinator', '/api/rules/catalogue')['attention'] if x['kind'] == 'both')
    h.command('coordinator', 'rule.acknowledge', {'fingerprints': [both['fingerprint']], 'note': 'Group conflict reviewed'})
    today = date.fromisoformat(h.get('coordinator', '/api/rules/catalogue')['day'])
    catalogue_rule(h, 'people_per_session', [line({'max': {'value': 3, 'level': 'mandatory'}},
                   ends_on=str(today + timedelta(days=11)))])
    before = h.get('auditor', '/api/rules/catalogue')
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        main.get_by_role('button', name='Duties per week lines').click()
        expect(main.locator('li.rt-ln')).to_have_count(2)
        expect(main.locator('input.bulk-check')).to_have_count(0)
        expect(main.get_by_role('button', name=re.compile('^\\+ New line'))).to_have_count(0)
        # A read-only role reads On / Off, not a switch it cannot use (U-19).
        expect(page.get_by_role('switch', name='Duties per week on or off')).to_have_count(0)
        expect(main.locator('li.rt-item').filter(has_text='Duties per week').locator('.rt-state')).to_have_text('On')
        expect(page.locator('.topbar').get_by_role('button', name=re.compile('^Propose'))).to_have_count(0)
        main.locator('button.dt-saved').click()
        page.get_by_role('menuitemradio', name=re.compile('^Needs attention')).click()
        expect(main.locator('tr.rt-issue')).to_have_count(1)
        expect(main.locator('.rt-choice')).to_have_text('Let it end')
        expect(main.locator('.rt-recommended-action, .rt-fixes button')).to_have_count(0)
        # A reviewed equal-tier conflict: read with who and when, never taken back from here.
        main.locator('button.dt-saved').click()
        page.get_by_role('menuitemradio', name=re.compile('^Acknowledged')).click()
        expect(main.locator('tr.rt-issue')).to_have_count(1)
        expect(main.locator('tr.rt-issue')).to_contain_text('★ Recommended')
        expect(main.locator('tr.rt-issue td.rt-acked')).to_contain_text('Group conflict reviewed')
        expect(main.locator('.rt-choice')).to_have_text('Keep both')
        expect(main.get_by_role('button', name=re.compile('^(Apply|Take back)'))).to_have_count(0)
        expect(main.locator('.rt-recommended-action, .rt-fixes button')).to_have_count(0)
        assert h.get('auditor', '/api/rules/catalogue') == before
    finally:
        context.close()


def test_a_mandatory_line_blocking_a_draft_opens_resolve_issues(browser, live_server, h):
    flag(h)
    catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 1, 'level': 'mandatory'}}, note='Everyone line')])
    b = h.seed['people'][1]
    w = h.workspace(start='2026-10-04', end='2026-10-11')
    for day in ('05', '06'):
        w = h.shift(w, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00', people=[b])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        main.locator('button.dt-saved').click()
        page.get_by_role('menuitemradio', name=re.compile('^Needs attention')).click()
        blocked = main.locator('tr.rt-issue').filter(has_text='Duties per week')
        expect(blocked).to_contain_text('Open Fix list')
        expect(blocked).to_contain_text('Make line 1 advisory')
        blocked.get_by_role('button', name=f'Open Fix list ({w["name"]})', exact=True).click()
        expect(page).to_have_url(re.compile(f"#schedule/issues\\?draft={w['id']}"))
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_an_empty_group_is_kept_one_issue_lists_every_blocked_draft_and_clinics_in_a_who_read_as_people(browser, live_server, h, width, scheme):
    """Hand-testing rc.9: a Keep off line for a rotation nobody is on today recommends Keep (it holds again when someone
    rotates in), the drafts one line blocks are one issue, and "can work at" clinics in a who never read as a where."""
    flag(h)
    b, clinic = h.seed['people'][1], h.seed['services'][0]
    visiting = h.configure('rotation_types', {'name': 'Visiting rotation', 'starts_on': '2026-01-01'})['record']
    catalogue_rule(h, 'keep_off', [line({'apply': {'value': None, 'level': 'mandatory'}}, who={'conditions': [{'field': 'rotation_type_ids', 'values': [visiting['id']]}]})])
    catalogue_rule(h, 'hours_per_week', [line({'max': {'value': 40, 'level': 'mandatory'}}, who={'conditions': [{'field': 'eligible_service_ids', 'values': [clinic]}]})])
    catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 1, 'level': 'mandatory'}}, note='Everyone line')])
    for name, start, end in (('Early October', '2026-10-04', '2026-10-11'), ('Mid October', '2026-10-11', '2026-10-18')):
        w = h.workspace(name=name, start=start, end=end)
        for day in (1, 2):
            first = (date.fromisoformat(start) + timedelta(days=day)).isoformat()
            w = h.shift(w, f'{first}T08:00:00+03:00', f'{first}T12:00:00+03:00', people=[b])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules')
        main = page.locator('#main')
        main.get_by_role('button', name='Hours per week lines').click()
        expect(main.locator('li.rt-ln .rp').filter(has_text=re.compile('^Can work at '))).to_have_count(1)
        main.locator('button.dt-saved').click()
        page.get_by_role('menuitemradio', name=re.compile('^Needs attention')).click()
        nobody = main.locator('tr.rt-issue').filter(has_text='Keep off')
        expect(nobody.locator('[data-col=issue-what]')).to_have_text('No active person matches this scope today')
        expect(nobody.locator('[data-col=issue-lines]')).to_contain_text('Visiting rotation')
        expect(nobody.locator('.rt-rec')).to_contain_text('Keep')
        expect(nobody.get_by_role('button', name='Remove line 1')).to_be_visible()  # the alternative
        blocked = main.locator('tr.rt-issue').filter(has_text='Duties per week')
        expect(blocked).to_have_count(1)
        expect(blocked).to_contain_text(re.compile('in Early October, Mid October:'))
        expect(blocked.get_by_role('button', name='Open Fix list (Mid October)')).to_be_visible()
        shot(page, f'needs-attention-groups-{width}-{scheme}')
    finally:
        context.close()
    assert not errors


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_a_pill_opens_its_line_every_day_reads_pressed_and_none_shows_only_when_it_does_something(browser, live_server, h, width, scheme):
    """Hand-testing rc.9: a who pill opens the line like its cell; a line valid every day shows all seven days pressed
    (the one day-toggle style); None shows only for a number the line sets or would inherit, naming what it drops."""
    flag(h)
    a = h.seed['people'][0]
    catalogue_rule(h, 'duties_per_month', [line({'min': {'value': 1, 'level': 'mandatory'}, 'max': {'value': 20, 'level': 'mandatory'}}, note='Everyone line'),
                                           line({'max': {'value': 10, 'level': 'mandatory'}}, who={'conditions': [{'field': 'person_ids', 'values': [a]}]}, note='Alex line')])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules/duties_per_month')
        main, panel = page.locator('#main'), page.locator('.rc-line-panel')
        main.locator('[data-row]').filter(has_text='Alex line').locator('.rp.person').first.click()  # a table row or a phone card
        expect(panel.get_by_role('heading', level=2)).to_contain_text('Alex Morgan')
        days = panel.get_by_role('group', name='Days').get_by_role('button')
        expect(days).to_have_count(7)
        for n in range(7):
            expect(days.nth(n)).to_have_attribute('aria-pressed', 'true')
        # Alex's line sets a max and inherits the min: both have None; the everyone line has no target to drop.
        expect(panel.get_by_label('Minimum', exact=True).locator('xpath=../..').get_by_role('button', name='None')).to_have_attribute('data-tip', "No minimum for this line: don't take 1")
        expect(panel.locator('.rc-none')).to_have_count(2)
        panel.get_by_role('button', name='Previous line').click()
        expect(panel.get_by_role('heading', level=2)).to_contain_text('Everyone')
        expect(panel.locator('.rc-none')).to_have_count(2)  # it sets min and max
        shot(page, f'line-panel-days-none-{width}-{scheme}')
    finally:
        context.close()


def test_acknowledgements_are_shared_two_coordinators_see_the_same_state(browser, live_server, h):
    """Acknowledge (with a note) is the department's, not the browser's: another person sees it under Acknowledged with
    who and when, takes it back, and the first person sees it need attention again."""
    flag(h)
    today = date.fromisoformat(h.get('coordinator', '/api/rules/catalogue')['day'])
    catalogue_rule(h, 'people_per_session', [line({'max': {'value': 3, 'level': 'mandatory'}}, ends_on=str(today + timedelta(days=11)))])
    original_rule = entry(h, 'people_per_session')['rule']
    first, page = sign_in(browser, live_server, 'coordinator')
    second, other = sign_in(browser, live_server, 'admin', {'width': 390, 'height': 900})
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        main.locator('button.dt-saved').click()
        page.get_by_role('menuitemradio', name=re.compile('^Needs attention')).click()
        action = main.get_by_role('button', name='Let it end · acknowledge…', exact=True)
        expect(action).to_have_text('Let it end · acknowledge…')
        action.click()
        dialog = page.get_by_role('dialog', name='Acknowledge 1 issue')
        expect(dialog).to_contain_text('The rules and rota checks do not change.')
        dialog.get_by_label('Reason', exact=True).fill('Ends with the winter rota')
        dialog.get_by_role('button', name='Acknowledge', exact=True).click()
        expect(main.get_by_role('heading', name='Nothing needs attention')).to_be_visible()
        [issue] = h.get('auditor', '/api/rules/catalogue')['attention']
        assert issue['acknowledged']['note'] == 'Ends with the winter rota'
        assert entry(h, 'people_per_session')['rule'] == original_rule
        assert entry(h, 'people_per_session')['proposed'] is None
        # The admin, in another browser: the same issue under Acknowledged, with who, when and the note.
        navigate(other, 'rules')
        theirs = other.locator('#main')
        theirs.locator('button.dt-saved').click()
        other.get_by_role('menuitemradio', name=re.compile(r'^Acknowledged\s*1')).click()
        row = theirs.locator('tr.rt-issue')
        expect(row).to_contain_text(issue['acknowledged']['by'])
        expect(row).to_contain_text('Ends with the winter rota')
        row.get_by_role('button', name=re.compile('^Take back')).click()
        expect(theirs.get_by_role('heading', name='Nothing acknowledged')).to_be_visible()
        assert entry(h, 'people_per_session')['rule'] == original_rule
        page.reload()
        main.locator('button.dt-saved').click()
        page.get_by_role('menuitemradio', name=re.compile('^Needs attention')).click()
        expect(main.locator('tr.rt-issue')).to_have_count(1)
    finally:
        first.close()
        second.close()


def test_keyboard_focus_stays_on_the_control_through_redraws(browser, live_server, h):
    """Every tick and toggle redraws the database: the control pressed keeps keyboard focus (WCAG 2.4.3)."""
    flag(h)
    weekly(h)
    end = str(date.fromisoformat(h.get('coordinator', '/api/rules/catalogue')['day']) + timedelta(days=11))
    catalogue_rule(h, 'people_per_session', [line({'max': {'value': 3, 'level': 'mandatory'}}, ends_on=end)])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        toggle = main.get_by_role('button', name='Duties per week lines')
        toggle.focus()
        page.keyboard.press('Enter')
        expect(toggle).to_be_focused()
        expect(toggle).to_have_attribute('aria-expanded', 'true')
        page.keyboard.press('Enter')
        expect(toggle).to_be_focused()
        expect(toggle).to_have_attribute('aria-expanded', 'false')
        page.keyboard.press('Enter')
        box = main.get_by_label('Select line 1 of Duties per week')
        box.focus()
        page.keyboard.press('Space')
        expect(box).to_be_checked()
        expect(box).to_be_focused()
        main.locator('button.dt-saved').click()
        page.get_by_role('menuitemradio', name=re.compile('^Needs attention')).click()
        pick = main.locator('tr.rt-issue input.bulk-check').first
        pick.focus()
        page.keyboard.press('Space')
        expect(pick).to_be_checked()
        expect(pick).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')],
                         ids=['light', 'dark', '390-light', '390-dark'])
def test_attention_badge_counts_lines_not_pairs(browser, live_server, h, width, scheme):
    """RU-02: equal-tier conflicts are one issue per pair; the rule's badge counts the lines involved."""
    flag(h)
    # Everyone plus one line per seeded person (the seed has three).
    lines = [line({'max': {'value': 10, 'level': 'mandatory'}})] + [line({'max': {'value': n + 1, 'level': 'mandatory'}}, who={'conditions': [{'field': 'person_ids', 'values': [p]}]}) for n, p in enumerate(h.seed['people'][:3])]
    catalogue_rule(h, 'duties_per_month', lines)
    rule = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_month')['rule']
    ids = [l['id'] for l in rule['definition']['rule']['lines']]
    pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]  # 4 lines, 6 overlapping pairs

    def six_pairs(route):
        response = route_fetch(route)
        body = response.json()
        body['attention'] = [{'id': f'both-{n}', 'kind': 'both', 'rule_id': rule['id'], 'catalogue_id': 'duties_per_month',
                              'lines': [], 'line_ids': list(pair), 'people': None, 'facts': {}, 'fixes': [], 'fingerprint': f'f{n}',
                              'acknowledged': None} for n, pair in enumerate(pairs)]
        body['attention_counts'] = attention_counts([issue for issue in body['attention'] if not issue.get('acknowledged')])
        assert body['attention_counts'] == {'rules': 1, 'lines': 4}
        route.fulfill(response=response, json=body)

    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 1000})
    try:
        page.emulate_media(color_scheme=scheme)
        page.route('**/api/rules/catalogue', six_pairs)
        navigate(page, 'rules')
        badge = page.locator('#main .rt-att')
        expect(badge).to_have_text('⚠ 4 lines need attention')
        expect(badge).to_be_in_viewport(ratio=1)
        expect(page.locator('#main button.dt-saved .dt-view-warn')).to_have_text('4 lines need attention')
        expect(page.locator('.app-footer')).not_to_contain_text('6 need')
        shot(page, f'ux-first-rules-attention-{width}-{scheme}', full_page=False)
    finally:
        context.close()


def test_list_orders_lines_anchor_then_exception_and_keeps_stored_numbers(browser, live_server, h):
    """Unsorted Rules lines show the anchor first, then its exception, with their stored numbers."""
    flag(h)
    a = h.seed['people'][0]
    catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 10, 'level': 'mandatory'}}, who={'conditions': [{'field': 'person_ids', 'values': [a]}]}, note='Alex line'),
                                           line({'max': {'value': 20, 'level': 'mandatory'}}, note='Everyone line')])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        expect(page.get_by_text('the most specific line wins', exact=False)).to_have_count(0)
        main.get_by_role('button', name='Duties per month lines').click()
        rows = main.locator('li.rt-ln')
        expect(rows.nth(0)).to_contain_text('Everyone')
        expect(rows.nth(0).locator('.rt-n')).to_have_text('2')
        expect(rows.nth(1).locator('.rp.person')).to_be_visible()
        expect(rows.nth(1).locator('.rt-n')).to_have_text('1')  # The nested exception keeps its stored line number.
    finally:
        context.close()


def test_disabled_switch_says_why(browser, live_server, h):
    """RU-24: a rule waiting for approval keeps a disabled switch that says so on hover."""
    flag(h)
    h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_week', 'on': True,
                                              'lines': [line({'max': {'value': 5, 'level': 'mandatory'}})], 'reason': 'Waiting'})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        switch = page.get_by_role('switch', name='Duties per week on or off')
        expect(switch).to_be_disabled()
        expect(switch).to_have_attribute('data-tip', 'Waiting for approval')
    finally:
        context.close()

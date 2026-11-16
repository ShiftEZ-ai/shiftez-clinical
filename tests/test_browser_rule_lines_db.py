"""A catalogue rule's page (#rules/<id>) as a lines database: cells edit in place in an anchored editor, + New line
picks who or where with the shared picker (a line that already exists says so), selected lines change together, and
every edit stays in a local draft until "Propose N changes" (overlaps acknowledged) and another person approves.
The page uses rule_catalogue.ts, shared table_view.ts/table_menu.ts column controls, and apps/web/public/assets/app.css."""
import re

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import rule_action, catalogue_rule, duplicate, flag, line, set_numbers, shot, weekly

pytestmark = pytest.mark.browser


def cell(row, label):
    return row.get_by_label(re.compile(f'^{label}: '))


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_lines_are_edited_in_a_draft_and_proposed_together(browser, live_server, h, width, scheme):
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        page.emulate_media(color_scheme=scheme)
        if width < 720:  # the lines database is a table: on a phone too, by choice
            page.evaluate("""async () => {const me=await fetch('/api/me').then(r=>r.json());
                localStorage.setItem('rota.table.'+me.identity_id+'.'+me.department.id+'.rule-line-db',JSON.stringify({layout:'table',chosen:true}));}""")
        navigate(page, 'rules/duties_per_week')
        main, footer = page.locator('#main'), page.locator('.app-footer')
        everyone = main.locator('tr').filter(has_text='Department default')
        # A number: the anchored editor, Enter saves into the draft (nothing is sent).
        cell(everyone, 'Max').click()
        editor = page.get_by_role('dialog', name='Edit Max')
        editor.get_by_label('Max', exact=True).fill('4')
        editor.get_by_label('Max', exact=True).press('Enter')
        expect(cell(everyone, 'Max')).to_have_text('4 Mandatory')
        expect(cell(everyone, 'Max').locator('.rule-num')).to_have_text('4 Mandatory')  # the one style of rule numbers: plain text
        if width > 720:
            expect(footer).to_contain_text('1 change not yet proposed')
        # Mandatory ↔ Advisory in the same editor.
        cell(everyone, 'Max').click()
        page.get_by_role('dialog', name='Edit Max').get_by_role('radio', name='Advisory').click()  # segmented(): a radio group
        page.get_by_role('dialog', name='Edit Max').get_by_role('button', name='Save').click()
        expect(cell(everyone, 'Max')).to_have_text('4 Advisory · medium')
        # Tab keeps the edit and opens the next cell (Shift+Tab the one before).
        cell(everyone, 'Max').click()
        page.get_by_role('dialog', name='Edit Max').get_by_label('Max', exact=True).press('Tab')
        expect(page.get_by_role('dialog', name='Edit Target')).to_be_visible()
        page.get_by_role('dialog', name='Edit Target').get_by_label('Target', exact=True).press('Shift+Tab')
        expect(page.get_by_role('dialog', name='Edit Max')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.get_by_role('dialog', name='Edit Max')).to_have_count(0)
        # The rule's description, a plain sentence, is the name's ⓘ (U-21: the head says each fact once).
        expect(main.locator('.rc-crumb .rc-info')).to_have_attribute('data-tip', re.compile('how many duties a person works each week', re.I))
        # + New line naming a clinic adds a line with no numbers yet, opened in the panel to give them.
        main.get_by_role('button', name='New line: a person, rank, clinic or #tag').click()
        page.get_by_role('combobox', name='Search New line').fill('Continuity clinic')
        page.get_by_role('option', name=re.compile('^Continuity clinic')).first.click()
        expect(page.locator('.rc-line-panel').get_by_role('heading', level=2)).to_contain_text('Continuity clinic')
        clinic = main.locator('tr').filter(has_text='Continuity clinic')
        expect(clinic).to_have_count(0)  # a draft in the panel until it has a number
        panel = page.locator('.rc-line-panel')
        panel.get_by_label('Maximum', exact=True).fill('5')
        panel.get_by_role('button', name='Keep change').click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(cell(clinic, 'Max')).to_have_text('5 Advisory · medium')  # a new number is advisory
        # The same who and where again opens a new line; the server's check says whether it is a duplicate (a Workload
        # line may count something else there). Closed before it has a number, nothing is added.
        main.get_by_role('button', name='New line: a person, rank, clinic or #tag').click()
        page.get_by_role('combobox', name='Search New line').fill('Continuity clinic')
        page.get_by_role('option', name=re.compile('^Continuity clinic')).first.click()
        expect(panel.get_by_role('heading', level=2)).to_contain_text('Continuity clinic')
        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(main.locator('tr[data-row]')).to_have_count(3)  # three lines: nothing was added
        # Select the lines (Shift ticks the range) and switch their strength together. Alex Morgan's line goes Advisory
        # too: a more specific line outranks a broader one only at the same strength, else both apply (maintenance, 21e096a0d),
        # so the line for everyone gives way to it again.
        boxes = main.locator('tbody tr[data-row] input.bulk-check')
        boxes.nth(0).click()
        boxes.nth(2).click(modifiers=['Shift'])
        expect(main.locator('tbody tr[data-row] input.bulk-check:checked')).to_have_count(3)
        set_numbers(page, level='Advisory')
        expect(main.locator('tbody tr[data-row] input.bulk-check:checked')).to_have_count(0)
        shot(page, f'rule-page-{width}-{scheme}')
        # ← Rules keeps the changes (the list counts them); leaving the Rules page asks first.
        main.get_by_role('button', name='← Rules').click()
        expect(page.locator('.app-footer')).to_contain_text('changes not yet proposed')
        page.evaluate("location.hash='home'")
        leave = page.get_by_role('dialog', name='Leave this page?')
        expect(leave).to_contain_text('changes not yet proposed. Propose them, or leave again')
        # The drafts are kept either way: the choice is Keep editing or go on (never "Discard changes").
        expect(leave.get_by_role('button', name='Discard changes')).to_have_count(0)
        expect(leave.get_by_role('button', name='Go to Home', exact=True)).to_be_visible()
        leave.get_by_role('button', name='Keep editing', exact=True).click()
        expect(page).to_have_url(re.compile('#rules$'))
        # Opening the rule asks the same; going on keeps the drafts.
        page.evaluate("location.hash='rules/duties_per_week'")
        leave.get_by_role('button', name=re.compile(r'^Go to Rules')).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-tab', 'duties_per_week')
        # Propose: the changes and the lines that give way, acknowledged, with a reason.
        page.locator('.topbar').get_by_role('button', name=re.compile(r'^Propose \d+ changes?$')).click()
        dialog = page.get_by_role('dialog')
        expect(dialog.locator('.rc-diff-added')).to_contain_text('Continuity clinic')
        dialog.get_by_label('I have read which lines give way and that is intended').check()
        dialog.get_by_label('Reason', exact=True).fill('Weekly caps agreed')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        expect(main.locator('.rc-waiting')).to_contain_text('waiting for approval')
    finally:
        context.close()
    assert not errors
    entry = next(e for e in h.get('approver', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_week')
    lines = entry['proposed']['definition']['rule']['lines']
    assert len(lines) == 3 and any(line['where'] for line in lines)
    assert entry['proposed']['diff']['added'] and entry['proposed']['diff']['changed']


def test_history_lists_each_version_with_its_line_changes(browser, live_server, h):
    flag(h)
    first = weekly(h)
    lines = first['definition']['rule']['lines']
    lines[0]['numbers']['max']['value'] = 6
    record = h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_week', 'expected_policy_id': first['id'], 'expected_revision': first['revision'], 'lines': lines,
                                                       'acknowledged_overlaps': [lines[0]['id']], 'reason': 'Busier winter'})['record']
    h.command('approver', 'policy.activate', {'reason': 'Agreed'}, record['id'], record['revision'])
    context, page = sign_in(browser, live_server, 'auditor')
    try:
        history = h.get('auditor', '/api/rules/catalogue/duties_per_week/history?version=1&page=1&limit=50')['policies']
        assert len(history) == 2
        asked = []
        def history_page(route):
            from urllib.parse import parse_qs, urlsplit
            number = int(parse_qs(urlsplit(route.request.url).query)['page'][0])
            asked.append(number)
            route.fulfill(json={'policies': history[number-1:number], 'has_more': number == 1})
        page.route('**/duties_per_week/history?**', history_page)
        navigate(page, 'rules/duties_per_week')
        expect(page.get_by_role('button', name='New line: a person, rank, clinic or #tag')).to_have_count(0)  # read-only
        expect(page.get_by_label(re.compile('^Max: '))).to_have_count(0)
        rule_action(page, 'History')
        dialog = page.get_by_role('dialog', name=re.compile('^History'))
        expect(dialog.locator('.rc-history > li')).to_have_count(1)
        expect(dialog.locator('.rc-diff-changed')).to_have_count(0)  # older version has not arrived yet
        dialog.get_by_role('button', name='Show more', exact=True).click()
        expect(dialog.locator('.rc-history > li')).to_have_count(2)
        expect(dialog.locator('.rc-history > li').nth(1)).to_be_focused()
        assert asked == [1, 2]
        expect(dialog.locator('.rc-history > li').first).to_contain_text('Busier winter')
        # Only the changed part reads was → now; the rest of the line is said once.
        expect(dialog.locator('.rc-diff-changed').first).to_contain_text('≤ 5 → ≤ 6 · Mandatory · Always')
        expect(dialog.locator('.rc-diff-changed').first).not_to_contain_text('Always → Everyone')
    finally:
        context.close()


def test_settings_are_chosen_in_their_popover_and_proposed_with_the_lines(browser, live_server, h):
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        page.locator('.topbar').get_by_role('button', name='Settings').click()
        pop = page.get_by_role('dialog', name='Duties per week settings')
        pop.get_by_label('Counts overtime').first.uncheck()
        shot(page, 'settings-popover')
        pop.get_by_role('button', name='Keep').click()
        expect(page.locator('.app-footer')).to_contain_text('1 change not yet proposed')
        page.locator('.topbar').get_by_role('button', name='Propose 1 change').click()
        dialog = page.get_by_role('dialog')
        expect(dialog.locator('.rc-settings-diff tbody tr')).to_contain_text(['Counts overtime'])  # RU-22: Setting · Was · Now
        dialog.get_by_label('I have read which lines give way and that is intended').check()
        dialog.get_by_label('Reason', exact=True).fill('Overtime does not count')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        expect(page.locator('#main .rc-waiting')).to_be_visible()
    finally:
        context.close()
    entry = next(e for e in h.get('approver', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_week')
    assert any(v.get('include_overtime') is False for v in entry['proposed']['definition']['rule']['settings'].values())


def test_a_rule_with_settings_to_choose_opens_its_page_first(browser, live_server, h):
    flag(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        # RU-04: its switch cannot flip back and forth; it is disabled and says why (one reaction, not three).
        switch = page.get_by_role('switch', name='Not after on or off')
        expect(switch).to_be_disabled()
        expect(switch).to_have_attribute('data-tip', re.compile('in Settings first$'))
        page.locator('#main tr.rt-rule').filter(has_text='Not after').get_by_role('button', name='Not after', exact=True).click()
        expect(page).to_have_url(re.compile('#rules/not_after$'))
        # Its first line is ready; the duty types (needed) are chosen in Settings, then it is proposed.
        types = [t['name'] for t in h.get('admin', '/api/catalogues')['assignment_types']]
        page.locator('.topbar').get_by_role('button', name='Settings').click()
        pop = page.get_by_role('dialog', name='Not after settings')
        for label, name in (('After these duty types (needed)', types[0]), ('Not these duty types (needed)', types[-1])):
            pop.get_by_role('button', name=re.compile('^' + re.escape(label))).first.click()
            page.locator('.picker').get_by_role('option', name=name, exact=True).check()
            page.keyboard.press('Escape')
        pop.get_by_role('button', name='Keep').click()
        page.locator('.topbar').get_by_role('button', name=re.compile(r'^Propose \d+ changes?$')).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('No early after a night')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        expect(page.locator('#main .rc-waiting')).to_be_visible()
    finally:
        context.close()
    entry = next(e for e in h.get('approver', '/api/rules/catalogue')['entries'] if e['id'] == 'not_after')
    assert entry['proposed']['definition']['rule']['settings']['forbidden_transition']['from_type_ids']


def test_many_issues_show_as_one_summary_and_a_badge_per_line(browser, live_server, h):
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        main = page.locator('#main')
        main.get_by_role('button', name='New line: a person, rank, clinic or #tag').click()
        page.get_by_role('combobox', name='Search New line').fill('Continuity clinic')
        page.get_by_role('option', name=re.compile('^Continuity clinic')).first.click()
        panel = page.locator('.rc-line-panel')
        panel.get_by_label('Maximum', exact=True).fill('5')
        panel.get_by_role('button', name='Keep change').click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(main.locator('tbody tr[data-row]')).to_have_count(3)
        # The person's line twice: the same line, and both overlap the line for everyone.
        main.locator('tr').filter(has_text='Alex Morgan').locator('input.bulk-check').check()
        duplicate(page)
        bar = main.locator('.rc-checkbar')
        expect(bar).to_contain_text('2 duplicate lines')
        expect(main.locator('.notice')).to_have_count(0)  # never a banner per issue
        bar.get_by_role('button', name='Show these lines').click()
        expect(page.locator('.app-footer .dt-count')).to_have_text('2 lines')  # only the two copies; the others have no issue
        expect(main.locator('tr').filter(has_text='Continuity clinic')).to_have_count(0)
        main.locator('tr').filter(has_text='Alex Morgan').first.get_by_role('button', name='⚠ Same line').click()
        expect(page.get_by_role('dialog', name='The same line twice')).to_contain_text('“Alex Morgan at all clinics · Sun–Sat · ≤ 3 · Mandatory · 1 Oct 2026 → 14 Nov 2026” is there twice')
        page.keyboard.press('Escape')
        bar.get_by_role('button', name='Show all lines').click()
        expect(page.locator('.app-footer .dt-count')).to_have_text('4 lines')
    finally:
        context.close()


def test_the_check_bar_reads_on_one_line_counts_in_words_and_the_rule_reaches_each_person_once(browser, live_server, h):
    """The check bar is one row (its words, then "Show these lines"), counts overlaps in words, the header of the
    unsortable Check column keeps its label readable and aligned, and reach counts each person once (not its widest line)."""
    flag(h)
    weekly(h)  # a line for everyone and one for Alex Morgan: the rule reaches all 3 people
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        main = page.locator('#main')
        expect(main).to_contain_text('reaches 3 people')
        bar = main.locator('.rc-checkbar')
        expect(bar).to_contain_text('⚠ 1 overlap')
        words, show = bar.locator('span').first.bounding_box(), bar.get_by_role('button', name='Show these lines').bounding_box()
        assert show['x'] > words['x'] + words['width'] and abs(show['y'] + show['height'] / 2 - words['y'] - words['height'] / 2) < 4
        check = main.get_by_role('columnheader', name='Check', exact=True)
        button = check.get_by_role('button', name='Check', exact=True)
        expect(button).to_be_visible()
        layout = check.evaluate('''h => {
            const b=h.querySelector('button'),label=b.querySelector('.dt-column-label'),
                body=h.closest('table').querySelector('tbody tr[data-row] [data-col=check]'),
                head=h.getBoundingClientRect(),button=b.getBoundingClientRect(),text=label.getBoundingClientRect(),cell=body.getBoundingClientRect();
            const inside=(a,b)=>a.left>=b.left-1&&a.right<=b.right+1&&a.top>=b.top-1&&a.bottom<=b.bottom+1;
            return {text:label.textContent,clipped:label.scrollWidth>label.clientWidth+1,
                labelContained:inside(text,button),buttonContained:inside(button,head),
                columnAligned:Math.abs(cell.left-head.left)<=1&&Math.abs(cell.right-head.right)<=1};
        }''')
        assert layout == {'text': 'Check', 'clipped': False, 'labelContained': True, 'buttonContained': True, 'columnAligned': True}, layout
        button.click()
        menu = page.get_by_role('menu', name='Check column', exact=True)
        expect(menu.get_by_role('menuitemradio')).to_have_count(0)  # Check remains unsortable.
        expect(menu.get_by_role('menuitemcheckbox', name='Wrap this column', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        main.get_by_role('button', name='New line: a person, rank, clinic or #tag').click()
        page.get_by_role('combobox', name='Search New line').fill('Demo practitioners')  # the rank: gives way to Alex's line
        page.get_by_role('option', name=re.compile('^Demo practitioners')).first.click()
        panel = page.locator('.rc-line-panel')
        panel.get_by_label('Maximum', exact=True).fill('5')  # a new line starts without numbers
        panel.get_by_role('button', name='Keep change').click()
        expect(bar).to_contain_text('⚠ 2 overlaps')
    finally:
        context.close()


def test_line_numbers_agree_between_list_page_and_panel(browser, live_server, h):
    """RU-07: a line has one number everywhere, its stored order: the list's rail, the open line's panel ("Line 2 of 2")
    and the check's messages, whatever order the table shows it in."""
    flag(h)
    a = h.seed['people'][0]
    catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 10, 'level': 'mandatory'}}, who={'conditions': [{'field': 'person_ids', 'values': [a]}]}),
                                           line({'max': {'value': 20, 'level': 'mandatory'}}, note='Everyone line')])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        main.get_by_role('button', name='Open Duties per month').click()
        everyone = main.locator('tr.rt-line').filter(has_text='Everyone')
        expect(everyone.locator('.rt-n')).to_have_text('2')
        everyone.get_by_role('button', name='Open line 2 of Duties per month').click()
        expect(page.locator('.rc-line-panel')).to_contain_text('Line 2 of 2')
    finally:
        context.close()


def test_history_shows_a_scope_change_past_the_shortened_list(browser, live_server, h):
    """Only the fifth person of a five-person line changes (E → F): History names the change in full,
    never "(note or settings)" with two identical shortened lists."""
    flag(h)
    with transaction(h.app.state.engine, write=True) as db:
        extra = [m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Scope person {n}') for n in range(3)]
        db.add_all(extra)
        db.flush()
        people = [*h.seed['people'], *(p.id for p in extra)]
    who = lambda ids: {'conditions': [{'field': 'person_ids', 'values': ids}]}  # noqa: E731
    first = catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5}}, who=who(people[:5]))])
    lines = first['definition']['rule']['lines']
    lines[0]['who'] = who(people[:4] + [people[5]])
    record = h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_week', 'expected_policy_id': first['id'], 'expected_revision': first['revision'], 'lines': lines,
                                                       'acknowledged_overlaps': [], 'reason': 'Swap the fifth person'})['record']
    h.command('approver', 'policy.activate', {'reason': 'Agreed'}, record['id'], record['revision'])
    context, page = sign_in(browser, live_server, 'auditor')
    try:
        navigate(page, 'rules/duties_per_week')
        rule_action(page, 'History')
        changed = page.get_by_role('dialog', name=re.compile('^History')).locator('.rc-diff-changed').first
        expect(changed).to_contain_text('→')
        expect(changed).not_to_contain_text('(note or settings)')
        expect(changed).not_to_contain_text('more')
    finally:
        context.close()

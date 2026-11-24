"""Rule lines as Notion-style filters (reviewed mockups K1f + K1g): Who and Where rows [Where | And/Or] [field]
[operator] [values] [count] [×]; one joiner per level, a different joiner wraps rows into a group; "is not" as an
operator; "has all of" on person fields only; Count (Together | Each) only where it changes the result; the reach links
("2 people", "2 clinics") opening their lists from the server's reach details; the table's Each mark; every role."""
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from .governance_support import identity
from .test_browser import add_filter, browser, choose, live_server, navigate, sign_in, press_escape  # noqa: F401
from .test_browser_rule_catalogue import catalogue_rule, flag, line, shot, weekly
from packages.contracts.schemas import PERMISSIONS

pytestmark = pytest.mark.browser
RULES_READERS = tuple(role for role in PERMISSIONS['rules.read']
                      if role == 'admin' or role not in PERMISSIONS['rule.propose'])

VIEWS = [(1440, 'light'), (390, 'dark')]
ALL_VIEWS = [(w, c) for w in (1440, 390) for c in ('light', 'dark')]  # the screenshots the owner reviews


def entry(h, key, role='approver'):
    return next(e for e in h.get(role, '/api/rules/catalogue')['entries'] if e['id'] == key)


def rota_this_week(h):
    """A rota holding today, with Alex (twice) and Jordan on duty: the reach lists carry this week's counts."""
    day = date.fromisoformat(h.get('coordinator', '/api/rules/catalogue')['day'])
    w = h.workspace('Reach fixture', (day - timedelta(days=7)).isoformat(), (day + timedelta(days=14)).isoformat())
    a, b, _ = h.seed['people']
    for (start, end), service in ((('08', '12'), 0), (('13', '17'), 1)):  # today: this week whatever the weekday
        w = h.shift(w, f'{day}T{start}:00:00+03:00', f'{day}T{end}:00:00+03:00', people=[a, b] if service == 0 else [a], service=service)['workspace']
    return w


def compound(h):
    """K1f's line: Who = (Rank is any of both ranks · or Skill has all of both skills · or Alex) and not Taylor;
    Where = Clinic is any of both clinics, each."""
    s = h.seed
    a, _, c = s['people']
    who = {'match': 'all', 'conditions': [
        {'match': 'any', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [s['category'], s['trainee_category']]},
                                        {'field': 'capability_ids', 'op': 'all', 'values': [s['capability'], s['supervisor_capability']]},
                                        {'field': 'person_ids', 'op': 'any', 'values': [a]}]},
        {'field': 'person_ids', 'op': 'none', 'values': [c]}]}
    where = {'match': 'all', 'conditions': [{'field': 'service_ids', 'op': 'any', 'values': s['services'], 'count': 'each'}]}
    return catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Department default'),
                                                 line({'max': {'value': 1, 'level': 'mandatory'}}, who=who, where=where, note='Compound')])


def open_line(page, key, line_id):
    navigate(page, f'rules/{key}/{line_id}')
    panel = page.locator('.rc-line-panel')
    expect(panel.locator('.rc-says')).to_contain_text('This line says')
    return panel


def sides(panel):
    return panel.locator('.rc-lp-section').nth(0), panel.locator('.rc-lp-section').nth(1)


def pick(page, row, field, values, op=None):
    """One filter row: its field (opening its values), the values from the shared picker, then its operator."""
    if field:
        row.get_by_label('Filter on', exact=True).select_option(label=field)
    else:
        row.locator('.pick-button').click()
    picker = page.locator('.picker')
    expect(picker.get_by_role('combobox')).to_be_focused()  # search is always on
    for v in values:
        picker.get_by_role('option', name=v, exact=True).check()
    page.keyboard.press('Escape')
    expect(picker).to_have_count(0)
    if op:
        row.get_by_label('Operator', exact=True).select_option(label=op)


def operators(row):
    return [t.strip() for t in row.get_by_label('Operator', exact=True).locator('option').all_inner_texts()]


def propose(page, reason):
    close = page.locator('.rc-line-panel').get_by_role('button', name='Close panel', exact=True)
    if close.count():
        close.click()  # a phone's sheet covers the page
    page.locator('.topbar').get_by_role('button', name=re.compile(r'^Propose \d+ changes?$')).click()
    dialog = page.get_by_role('dialog', name=re.compile('^Propose'))
    expect(dialog.get_by_label('I have read which lines give way and that is intended')).to_have_count(0)
    dialog.get_by_label('Reason', exact=True).fill(reason)
    dialog.get_by_role('button', name='Propose', exact=True).click()
    expect(dialog).to_have_count(0)


def approve(h):
    waiting = entry(h, 'duties_per_week')['proposed']
    h.command('approver', 'policy.activate', {'reason': 'Agreed'}, waiting['id'], waiting['revision'])


@pytest.mark.parametrize('width,scheme', ALL_VIEWS)
def test_filters_join_group_and_save_as_the_new_tree(browser, live_server, h, width, scheme):
    """Build "(Rank any of · or Skill has all of · or Alex) and Taylor is not" from an empty Who, count the clinics
    each, keep, propose, approve; then reload, edit back to Together and propose again."""
    flag(h)
    rule = weekly(h)
    everyone = rule['definition']['rule']['lines'][0]['id']
    s = h.seed
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        page.emulate_media(color_scheme=scheme)
        panel = open_line(page, 'duties_per_week', everyone)
        who, where = sides(panel)
        expect(who.locator('.cond-line')).to_have_count(0)  # no filter: everyone
        add_filter(who)
        rows = who.locator('.cond-line')
        expect(rows).to_have_count(1)
        expect(who.locator('.cond-where')).to_have_text('')  # F-R4 (maintenance, d1beaa224): the Who heading leads, no "Where" word
        pick(page, rows.nth(0), 'Rank', ['Demo practitioners', 'Demo learners'])
        assert operators(rows.nth(0)) == ['Is any of', 'Has all of', 'Is not', 'Is empty', 'Is not empty']  # a person field holds several values
        expect(rows.nth(0).locator('.cond-count')).to_have_count(0)  # workload: Who always counts each person
        # A second row: And by default; Or for the whole level (two rows need no group).
        add_filter(who)
        joiners = who.get_by_label('Joiner', exact=True)
        expect(joiners).to_have_count(1)
        expect(joiners.first).to_have_value('all')
        joiners.first.select_option(label='Or')
        pick(page, rows.nth(1), 'Skill', ['Demo independent practice', 'Demo supervision'], op='Has all of')
        add_filter(who)
        expect(joiners.nth(1)).to_have_value('any')  # one joiner per level
        pick(page, rows.nth(2), 'Person', ['Staff 01(test)'])
        assert operators(rows.nth(2)) == ['Is', 'Is not', 'Is empty', 'Is not empty']  # a person has one identity: no "has all of"
        # "and Taylor is not": the other joiner wraps the three "or" rows into a group, so the meaning stays clear.
        add_filter(who)
        pick(page, rows.nth(3), 'Person', ['Staff 03(test)'], op='Is not')
        who.get_by_label('Joiner', exact=True).nth(2).select_option(label='And')
        group = who.locator('.cond-group')
        expect(group).to_have_count(1)
        expect(group.locator('.cond-line')).to_have_count(3)
        expect(group.locator('.cond-group-head')).to_have_text('Group')
        expect(who.locator(':scope .cond-root > .cond-level > .cond-list > .cond-row')).to_have_count(2)
        expect(who.locator('.cond-row').last.get_by_label('Joiner', exact=True)).to_have_value('all')
        expect(who.locator('.cond-count')).to_have_count(0)
        shot(page, f'builder-group-{width}-{scheme}', full_page=False)
        # Where: two clinics, counted at each; Count is labelled, its buttons in words, the choice said beside it.
        add_filter(where)
        expect(where.locator('.cond-where')).to_have_text('Where')  # only Who drops the lead word
        crow = where.locator('.cond-line').first
        expect(crow.get_by_label('Filter on', exact=True)).to_have_value('service_ids')
        assert operators(crow) == ['Is', 'Is not', 'Is empty', 'Is not empty']  # a session is at one clinic
        pick(page, crow, None, ['Continuity clinic', 'Review clinic'])
        assert operators(crow) == ['Is any of', 'Is not', 'Is empty', 'Is not empty']
        switch = crow.get_by_role('radiogroup', name='Count')
        expect(switch.get_by_role('radio', name=re.compile('^Together'))).to_have_attribute('aria-checked', 'true')
        expect(switch).to_contain_text('Each')  # words on the buttons reviewed
        expect(crow.locator('.cond-hint')).to_have_text('all clinics counted together')
        switch.get_by_role('radio', name=re.compile('^Each')).click()
        expect(panel.locator('.rc-says')).to_contain_text('5 at each of Continuity clinic and Review clinic')
        shot(page, f'builder-count-{width}-{scheme}', full_page=False)
        panel.get_by_role('button', name='Keep change').click()
        expect(page.locator('.app-footer')).to_contain_text('1 change not yet proposed')
        alex_line = rule['definition']['rule']['lines'][1]['id']
        expect(page.locator(f'#main [data-row="{everyone}"] .cond-each-mark')).to_have_count(1)
        expect(page.locator(f'#main [data-row="{alex_line}"] .cond-each-mark')).to_have_count(0)
        propose(page, 'Grouped who, clinics each')
    finally:
        context.close()
    assert not errors
    proposed = next(l for l in entry(h, 'duties_per_week')['proposed']['definition']['rule']['lines'] if l['id'] == everyone)
    assert proposed['who'] == {'match': 'all', 'conditions': [
        {'match': 'any', 'conditions': [
            {'field': 'category_ids', 'op': 'any', 'values': [s['category'], s['trainee_category']]},
            {'field': 'capability_ids', 'op': 'all', 'values': [s['capability'], s['supervisor_capability']]},
            {'field': 'person_ids', 'op': 'any', 'values': [s['people'][0]]}]},
        {'field': 'person_ids', 'op': 'none', 'values': [s['people'][2]]}]}
    assert proposed['where'] == {'match': 'all', 'conditions': [{'field': 'service_ids', 'op': 'any', 'values': s['services'], 'count': 'each'}]}
    approve(h)
    # Reload: the line reads back as built; Together again is one more proposal.
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 1000})
    try:
        page.emulate_media(color_scheme=scheme)
        panel = open_line(page, 'duties_per_week', everyone)
        who, where = sides(panel)
        expect(who.locator('.cond-group .cond-line')).to_have_count(3)
        expect(who.locator('.cond-line').nth(1).get_by_label('Operator', exact=True)).to_have_value('all')
        expect(who.locator('.cond-line').nth(3).get_by_label('Operator', exact=True)).to_have_value('none')
        switch = where.get_by_role('radiogroup', name='Count')
        expect(switch.get_by_role('radio', name=re.compile('^Each'))).to_have_attribute('aria-checked', 'true')
        switch.get_by_role('radio', name=re.compile('^Together')).click()
        panel.get_by_role('button', name='Keep change').click()
        expect(page.locator('#main tr[data-row] .cond-each-mark')).to_have_count(0)
        propose(page, 'Clinics together again')
    finally:
        context.close()
    proposed = next(l for l in entry(h, 'duties_per_week')['proposed']['definition']['rule']['lines'] if l['id'] == everyone)
    assert proposed['where'] == {'match': 'all', 'conditions': [{'field': 'service_ids', 'op': 'any', 'values': s['services']}]}


@pytest.mark.parametrize('width,scheme', VIEWS)
def test_count_shows_only_where_it_changes_the_result(browser, live_server, h, width, scheme):
    """Staffing counts people of each kind: Who has Count. Protected time counts nothing: no Count anywhere."""
    flag(h)
    s = h.seed
    staffing = catalogue_rule(h, 'kind_per_session', [line({'min': {'value': 1, 'level': 'mandatory'}},
        who={'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [s['category'], s['trainee_category']]}]})])
    protected = catalogue_rule(h, 'protected_time', [line({'apply': {'value': None, 'level': 'mandatory'}},
        where={'match': 'all', 'conditions': [{'field': 'service_ids', 'op': 'any', 'values': s['services']}]})])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 1000})
    try:
        page.emulate_media(color_scheme=scheme)
        panel = open_line(page, 'kind_per_session', staffing['definition']['rule']['lines'][0]['id'])
        who, _ = sides(panel)
        row = who.locator('.cond-line').first
        row.get_by_role('radiogroup', name='Count').get_by_role('radio', name=re.compile('^Each')).click()
        expect(panel.locator('.rc-says')).to_contain_text('1 from each of Demo practitioners and Demo learners')
        shot(page, f'count-staffing-{width}-{scheme}', full_page=False)
        # "is not" never counts: the switch goes; back to "is any of", it is there again.
        row.get_by_label('Operator', exact=True).select_option(label='Is not')
        expect(row.locator('.cond-count')).to_have_count(0)
        row.get_by_label('Operator', exact=True).select_option(label='Is any of')
        expect(row.locator('.cond-count')).to_have_count(1)
        panel.get_by_role('button', name='Cancel', exact=True).click()
        expect(panel).to_have_count(0)
        panel = open_line(page, 'protected_time', protected['definition']['rule']['lines'][0]['id'])
        expect(panel.locator('.cond-line')).to_have_count(1)
        expect(panel.locator('.cond-count')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', VIEWS)
def test_fair_share_count_uses_catalogue_sides_and_saves_each_clinic(browser, live_server, h, width, scheme):
    flag(h)
    s = h.seed
    rule = catalogue_rule(h, 'fair_share', [line({'target': {'value': 2, 'level': 'medium'}},
        who={'conditions': [{'field': 'person_ids', 'values': s['people'][:2]}]})],
        settings={'balance_goal': {'metric': 'duties', 'aim': 'target', 'period': 'rota'}})
    line_id = rule['definition']['rule']['lines'][0]['id']
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 1000})
    try:
        page.emulate_media(color_scheme=scheme)
        panel = open_line(page, 'fair_share', line_id)
        who, where = sides(panel)
        expect(who.locator('.cond-count')).to_have_count(0)
        add_filter(where)
        pick(page, where.locator('.cond-line').first, None, ['Continuity clinic', 'Review clinic'])
        switch = where.get_by_role('radiogroup', name='Count')
        expect(switch.get_by_role('radio', name=re.compile('^Together'))).to_have_attribute('aria-checked', 'true')
        switch.get_by_role('radio', name=re.compile('^Each')).click()
        panel.get_by_role('button', name='Keep change').click()
        expect(page.locator(f'#main [data-row="{line_id}"] .cond-each-mark')).to_have_count(1)
        propose(page, 'Compare Fair share separately for each clinic')
    finally:
        context.close()
    [saved] = entry(h, 'fair_share')['proposed']['definition']['rule']['lines']
    assert saved['where']['conditions'][0]['count'] == 'each'
    assert not saved['who'].get('count') and not saved['who']['conditions'][0].get('count')


def test_existing_lines_load_and_save_unchanged(browser, live_server, h):
    """A line saved before Count (no op on its first filter, "is none of" as op none) opens, keeps and proposes with
    its tree exactly as stored."""
    flag(h)
    s = h.seed
    who = {'conditions': [{'field': 'category_ids', 'values': [s['category']]}, {'field': 'person_ids', 'op': 'none', 'values': [s['people'][2]]}]}
    rule = catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}}, who=who, note='Legacy')])
    stored = rule['definition']['rule']['lines'][0]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        panel = open_line(page, 'duties_per_week', stored['id'])
        w, _ = sides(panel)
        expect(w.locator('.cond-line').nth(1).get_by_label('Operator', exact=True)).to_have_value('none')
        expect(w.locator('.cond-line').nth(1).get_by_label('Operator', exact=True).locator('option:checked')).to_have_text('Is not')
        panel.get_by_role('button', name='Keep change').click()
        expect(page.locator('.app-footer')).not_to_contain_text('not yet proposed')
        panel = page.locator('.rc-line-panel')
        panel.get_by_label('Maximum', exact=True).fill('6')
        panel.get_by_role('button', name='Keep change').click()
        propose(page, 'Only the number')
    finally:
        context.close()
    proposed = entry(h, 'duties_per_week')['proposed']['definition']['rule']['lines'][0]
    assert proposed['who'] == stored['who'] and proposed.get('where') == stored.get('where')
    assert proposed['numbers']['max']['value'] == 6


@pytest.mark.parametrize('width,scheme', ALL_VIEWS)
def test_reach_lists_group_people_by_their_filter_with_load_and_links(browser, live_server, h, width, scheme):
    flag(h)
    rule = compound(h)
    rota_this_week(h)
    compound_id = rule['definition']['rule']['lines'][1]['id']
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        page.emulate_media(color_scheme=scheme)
        panel = open_line(page, 'duties_per_week', compound_id)
        heading = panel.locator('.rc-lp-title').first
        people = heading.get_by_role('button', name='2 people')
        expect(people).to_be_visible()
        expect(panel.locator('.rc-says').get_by_role('button', name='2 people')).to_be_visible()
        expect(panel.get_by_role('heading', level=2)).to_have_text('2 people at Continuity clinic, Review clinic (each) · ≤ 1 · Mandatory')  # DESIGN K1f: who at where · numbers
        expect(panel.get_by_role('img', name='No duplicate or overlap')).to_be_visible()
        # No chip cut to a stub; at desktop every row, group rows included, is one line (its Match / Count row below it
        # when it has two or more values).
        stubs = panel.evaluate('''p => [...p.querySelectorAll('.pick-values>.pick-chip')].filter(c => !c.hidden)
            .filter(c => c.scrollWidth > c.clientWidth + 1 && c.clientWidth < 90).map(c => c.textContent)''')
        assert not stubs, stubs
        if width >= 1024:
            rows = panel.evaluate('''p => [...p.querySelectorAll('.cond-line')].map(r => Math.max(...[...r.children].filter(c => !c.classList.contains('cond-more'))
                .map(c => c.getBoundingClientRect().bottom)) - r.getBoundingClientRect().top)''')
            assert max(rows) < 40, rows
        shot(page, f'builder-{width}-{scheme}', full_page=False)
        people.click()
        pop = page.locator('.picker')  # the shared picker (picker.ts)
        expect(pop.locator('.picker-foot')).to_have_text(re.compile(r'^Reach fixture · '))  # the rota and week counted
        # Grouped by the filter that matched them (Taylor, "is not", never appears).
        expect(pop.locator('.picker-group')).to_have_count(2)
        expect(pop.locator('.picker-group').first).to_contain_text('Rank is any of')
        expect(pop.locator('.picker-group').last).to_contain_text('Person is Staff 01(test)')
        expect(pop).not_to_contain_text('Staff 03(test)')
        alex = pop.get_by_role('option', name=re.compile('^Staff 01\\(test\\)'))
        expect(alex).to_contain_text('1 of 1, at the limit')  # once at each clinic: each clinic is counted on its own
        expect(alex.locator('.picker-meta .rc-load.at .rc-load-bar')).to_have_count(1)  # the bar beside the words
        expect(pop.locator('.picker-group', has_text='Recent')).to_have_count(0)  # fullest load first, never reordered
        expect(pop.locator('.picker-foot')).to_contain_text('2 at the limit')
        pop.get_by_role('combobox').fill('jordan')
        expect(pop.get_by_role('option')).to_have_count(1)
        pop.get_by_role('combobox').fill('')
        shot(page, f'reach-people-{width}-{scheme}', full_page=False)
        press_escape(page)
        expect(pop).to_have_count(0)
        clinics = panel.locator('.rc-lp-title').nth(1).get_by_role('button', name='2 clinics')
        clinics.click()
        pop = page.locator('.picker')
        expect(pop.get_by_role('option')).to_have_count(2)
        expect(pop.get_by_role('option').filter(has_text='Counted for each')).to_have_count(2)
        shot(page, f'reach-clinics-{width}-{scheme}', full_page=False)
        press_escape(page)
        people.click()
        page.locator('.picker').get_by_role('option', name=re.compile('^Staff 01\\(test\\)')).click()
        expect(page.get_by_role('dialog', name='Which rule applies?')).to_be_visible()
        page.keyboard.press('Escape')
        panel.locator('.rc-lp-title').first.get_by_role('button', name='2 people').click()
        page.locator('.picker').get_by_role('button', name='Show on Schedule →').click()
        expect(page).to_have_url(re.compile('#schedule'))
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()
    assert not errors


@pytest.mark.parametrize('width,scheme', ALL_VIEWS)
def test_the_table_marks_lines_counted_for_each(browser, live_server, h, width, scheme):
    flag(h)
    lines = compound(h)['definition']['rule']['lines']
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules/duties_per_week')
        expect(page.locator(f'#main [data-row="{lines[1]["id"]}"] .cond-each-mark')).to_have_count(1)
        expect(page.locator(f'#main [data-row="{lines[0]["id"]}"] .cond-each-mark')).to_have_count(0)
        # "not Staff 03(test)" is muted, never struck through (strikethrough is an old value in the approval diff).
        except_pill = page.locator(f'#main [data-row="{lines[1]["id"]}"] .rp.except').first
        expect(except_pill).to_have_text('not Staff 03(test)')
        expect(except_pill).to_have_css('text-decoration-line', 'none')  # retries: the row may re-render once
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        shot(page, f'table-{width}-{scheme}', full_page=False)
    finally:
        context.close()


@pytest.mark.parametrize('role,width,scheme', [(r, w, c) for r in RULES_READERS
                                                for w, c in VIEWS] + [('staff', 1440, 'light')])
def test_every_role_reads_the_filters_as_it_may(browser, live_server, h, role, width, scheme):
    """Admins edit; every Rules reader reads the same filters as facts; staff have no Rules page."""
    if role == 'programme_lead':
        identity(h, role, [role])
    flag(h)
    rule = compound(h)
    rota_this_week(h)
    context, page = sign_in(browser, live_server, role, {'width': width, 'height': 1000})
    try:
        page.emulate_media(color_scheme=scheme)
        if role == 'staff':
            page.evaluate("location.hash='rules'")
            expect(page.get_by_text('Restricted page')).to_be_visible()
            return
        panel = open_line(page, 'duties_per_week', rule['definition']['rule']['lines'][1]['id'])
        who, where = sides(panel)
        if role == 'admin':
            expect(who.locator('.cond-group .cond-line')).to_have_count(3)
            expect(who.get_by_role('button', name='+ Add filter rule').last).to_be_visible()
            expect(who.get_by_role('button', name='+ Add filter rule')).to_have_count(2)  # the group's and the level's
            expect(panel.get_by_role('button', name='Keep change')).to_be_visible()
        else:
            expect(panel.get_by_role('button', name='+ Add filter rule')).to_have_count(0)
            # Read-only: facts (field · operator · value chips · Count in words), never disabled pickers reviewed.
            expect(who.locator('.cond-fact-group .cond-fact')).to_have_count(3)
            expect(panel.locator('.cond-line, .cond-count')).to_have_count(0)
            expect(panel.locator('select:not([disabled]), input:not([readonly]):not([disabled]):not([type=hidden])')).to_have_count(0)
            expect(who.locator('.cond-fact').last).to_contain_text('is not')
            expect(where.locator('.cond-fact')).to_contain_text('Clinic is any of Continuity clinic Review clinic · each clinic counted separately')
        links = panel.locator('.rc-reach-link')
        if role == 'credentialer':
            expect(links).to_have_count(0)
            expect(panel.locator('.rc-lp-title').first).to_contain_text(re.compile(r'Who · \d+ (person|people)'))
        else:
            expect(panel.locator('.rc-lp-title').first.get_by_role('button', name='2 people')).to_be_visible()
            panel.locator('.rc-lp-title').first.get_by_role('button', name='2 people').click()
            expect(page.locator('.picker').get_by_role('option')).to_have_count(2)
            page.keyboard.press('Escape')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        shot(page, f'role-{role}-{width}-{scheme}', full_page=False)
    finally:
        context.close()



def test_credentialer_is_refused_the_rules_page(browser, live_server):
    context, page = sign_in(browser, live_server, 'credentialer')
    try:
        page.evaluate("location.hash='rules'")
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'restricted')
        expect(page.get_by_role('heading', name='Restricted page', exact=True)).to_be_visible()
        expect(page.locator('#main')).not_to_have_attribute('data-loaded-view', 'rules')
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', VIEWS)
def test_a_new_line_opens_itself_advisory_and_empty_and_says_what_is_on_screen(browser, live_server, h, width, scheme):
    """Audit H01/H5/M09: + New line opens that line (never the previously open one), with no numbers; a number typed
    takes the broader same-where line's strength (D8) and "This line says" follows every edit before Keep change; the
    other lines are untouched."""
    flag(h)
    rule = weekly(h)
    everyone = rule['definition']['rule']['lines'][0]['id']
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 1000})
    try:
        page.emulate_media(color_scheme=scheme)
        panel = open_line(page, 'duties_per_week', everyone)
        panel.get_by_role('button', name='Close panel', exact=True).click()
        page.get_by_role('button', name='New line: a person, rank, clinic or #tag').click()
        page.get_by_role('combobox', name='Search New line').fill('Demo learners')
        page.get_by_role('option', name=re.compile('^Demo learners')).first.click()
        panel = page.locator('.rc-line-panel')
        expect(panel.get_by_role('heading', level=2)).to_contain_text('Demo learners')
        for label in ('Minimum', 'Maximum', 'Target'):
            expect(panel.get_by_label(label, exact=True)).to_have_value('')
        says = panel.locator('.rc-says')
        expect(says).to_contain_text('no number yet')
        # Only a draft in the panel: Keep change says what it needs, and the rule's own check carries on undisturbed.
        keep = panel.get_by_role('button', name='Keep change')
        expect(keep).to_have_attribute('aria-disabled', 'true')
        expect(keep).to_have_attribute('data-tip', 'Add a minimum, maximum or target')
        expect(page.locator('#main .rc-checkbar')).to_be_empty()
        expect(page.locator('#main .rc-checkbar')).not_to_contain_text('refused')
        expect(page.locator('#main tr[data-row], #main [data-row]').filter(has_text='Demo learners')).to_have_count(0)
        panel.get_by_label('Maximum', exact=True).fill('4')
        expect(keep).to_have_attribute('aria-disabled', 'false')
        expect(says).to_contain_text('at most 4 duties per week')
        # D8: a new line with the same where as a broader line starts with that line's strength (Everyone ≤ 5 Mandatory).
        expect(says).to_contain_text('(mandatory)')
        expect(panel.get_by_role('button', name='Maximum: mandatory', exact=True)).to_be_visible()
        panel.get_by_role('group', name='Days').get_by_role('button', name='Saturday').click()
        expect(says).to_contain_text('Sun–Fri')
        panel.get_by_role('button', name='Keep change').click()
        expect(page.locator('.app-footer')).to_contain_text('1 change not yet proposed')
        # The line that was open before keeps its own number.
        expect(page.locator(f'#main [data-row="{everyone}"]')).to_contain_text('5 Mandatory')
    finally:
        context.close()


def test_an_empty_new_line_closes_without_a_trace(browser, live_server, h):
    """A new line without a number is only a draft in the panel: closing drops it, no question, no change counted."""
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        page.get_by_role('button', name='New line: a person, rank, clinic or #tag').click()
        page.get_by_role('combobox', name='Search New line').fill('Demo learners')
        page.get_by_role('option', name=re.compile('^Demo learners')).first.click()
        panel = page.locator('.rc-line-panel')
        expect(panel.get_by_role('heading', level=2)).to_contain_text('Demo learners')
        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(panel).to_have_count(0)
        expect(page.get_by_role('dialog', name='Discard changes?')).to_have_count(0)
        expect(page.locator('#main tr[data-row]')).to_have_count(2)
        expect(page.locator('.app-footer')).not_to_contain_text('not yet proposed')
    finally:
        context.close()


def test_an_excluded_tag_reads_not_and_groups_as_combined_filters(browser, live_server, h):
    """Audit M07/M08: "not #Night" (never a plain #Night chip); a who of a group is "Combined filters", not Everyone."""
    flag(h)
    h.configure('tags', {'name': 'Night', 'labels': ['people']})
    night = next(t['id'] for t in h.get('admin', '/api/catalogues')['tags'] if t['name'] == 'Night')
    s = h.seed
    rule = catalogue_rule(h, 'duties_per_week', [
        line({'max': {'value': 5, 'level': 'mandatory'}}, who={'match': 'all', 'conditions': [
            {'field': 'category_ids', 'op': 'any', 'values': [s['category']]}, {'field': 'person_ids', 'op': 'none', 'values': [night]}]}),
        line({'max': {'value': 4, 'level': 'mandatory'}}, who={'match': 'all', 'conditions': [{'match': 'any', 'conditions': [
            {'field': 'category_ids', 'op': 'any', 'values': [s['trainee_category']]},
            {'field': 'person_ids', 'op': 'any', 'values': [s['people'][0]]}]}]})])
    lines = rule['definition']['rule']['lines']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        expect(page.locator(f'#main [data-row="{lines[0]["id"]}"] .tag-chip')).to_contain_text('not #Night')
        # A rule's page groups its lines by place; grouped by who kind, a who of a group reads Combined filters.
        choose(page.locator('#main'), 'Group', 'Group by', 'Who kind')
        expect(page.locator('#main').get_by_text('Combined filters', exact=True)).to_have_count(1)
        expect(page.locator('#main').get_by_text('Everyone', exact=True)).to_have_count(0)
    finally:
        context.close()

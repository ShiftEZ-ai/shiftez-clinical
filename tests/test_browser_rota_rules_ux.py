"""The rota's one panel (docs/design/rota-rules-and-availability.md): its rules with safety kinds locked and every
rota-only change justified, reviewed with the rota (D16), and who is available over the rota's dates, beside the
schedule. Runs in CI only (owner order: no tests on the Mac)."""
import re

import pytest
from playwright.sync_api import expect

from .legend_guard import assert_legend_matches

from .test_browser import browser, live_server, sign_in, planner_tool, navigate, open_rota, route_fetch, filter_by  # noqa: F401
from .test_browser_rules_in_planner import rota, open_planner, MON
from .rules_support import rule_action, rule_row

pytestmark = pytest.mark.browser
WHY = 'Short staffed in October'


def test_rota_rules_change_needs_a_justification_and_safety_stays_locked(browser, live_server, h):
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator'); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        expect(panel.get_by_role('tab', name='Rules')).to_have_attribute('aria-selected', 'true')
        # A safety kind: a lock on its row (said in words to a screen reader), why it can't be loosened, never switched off.
        rest = rule_row(panel, 'Rest between duties')
        expect(rest.locator('.rk-row-head').get_by_role('img', name='Safety rule: can only be made stricter')).to_be_visible()
        rule_action(rest, 'Make stricter for this rota')  # never "Adjust": a safety rule only gets stricter
        expect(rest.get_by_role('form', name='Make stricter for this rota')).to_be_visible()
        expect(rest.get_by_role('button', name='Switch off for this rota', exact=True)).to_have_count(0)
        # A non-safety number: refused without a justification, applied with one, with Undo.
        duties = rule_row(panel, 'Weekly duty limit')
        rule_action(duties, 'Adjust for this rota')
        duties.get_by_label('New number for “Weekly duty limit” in this rota').fill('4')
        duties.get_by_role('button', name='Use for this rota', exact=True).click()
        assert not duties.get_by_label('Justification', exact=True).evaluate('(input) => input.checkValidity()')
        assert h.detail(w)['workspace']['rule_adjustments'] == []
        duties.get_by_label('Justification', exact=True).fill(WHY)
        duties.get_by_role('button', name='Use for this rota', exact=True).click()
        toast = page.locator('.toast').filter(has_text='changed for this rota')
        expect(toast.get_by_role('button', name='Undo')).to_be_visible()
        [adjustment] = h.detail(w)['workspace']['rule_adjustments']
        assert adjustment['amount'] == 4 and adjustment['reason'] == WHY
        # The changed rule says so; opened, one sentence says what changed, who, when and why, then its review state.
        changed = rule_row(panel, 'Weekly duty limit')
        expect(changed.locator('.rk-row-head')).to_contain_text('Changed for this rota.')
        detail = changed.locator('.rk-detail')
        expect(detail).to_contain_text(re.compile(r'Lowered from 5 to 4 for this rota by you on .+: “' + re.escape(WHY) + '”'))
        expect(detail).to_contain_text('Reviewed when the rota is submitted.')
        assert errors == []
    finally:
        context.close()


def test_who_is_available_over_the_rota_dates(browser, live_server, h):
    w, _ = rota(h)  # 4–10 Oct 2026
    # A person of its own: no rotation, academic time or rank history from the seed can change what this row says.
    away = h.command('admin', 'catalogue.create', {'catalogue': 'people', 'value': {'display_name': 'Dr Ione Varga'},
                                                   'reason': 'Availability fixture'})['record']['id']
    h.command('coordinator', 'availability.record', {'person_id': away, 'kind': 'recorded_leave',
              'start': '2026-10-07T00:00:00+03:00', 'end': '2026-10-09T00:00:00+03:00', 'private_reason': 'Leave'})
    # The tab's own read (the server's whereabouts projection, work hidden, rank memberships for start/leave dates).
    read = h.get('coordinator', f"/api/timeline?start=2026-10-04&end=2026-10-11&bucket=day&hide=work&layers=rank&group_by=rank&limit=200&person_ids={away}")
    [line] = read['people']
    assert [(g['kind'], g['starts_on'], g['ends_on']) for g in line['segments']] == [('leave', '2026-10-07', '2026-10-09')]
    context, page = sign_in(browser, live_server, 'coordinator'); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        open_planner(page, w)
        planner_tool(page, 'Who’s available')
        panel = page.get_by_role('dialog', name='Who’s available')
        expect(panel.get_by_role('tab', name='Who’s available')).to_have_attribute('aria-selected', 'true')
        views = panel.locator('.dt-toolbar .dt-saved')
        # This person can work none of the rota's sessions: they are under Everyone in the department, not the default.
        partly = panel.locator('.ra-row').filter(has=page.get_by_role('button', name=line['display_name']))
        expect(views).to_have_accessible_name('Saved views: Can work in this rota')
        expect(partly).to_have_count(0)
        views.click()
        page.get_by_role('menu', name='Saved views').get_by_role('menuitemradio', name=re.compile('^Everyone in the department')).click()
        expect(partly).to_have_attribute('data-ra', 'part')
        expect(partly.locator('.ra-words')).to_have_text('Away 2 d')
        # The strip says every stretch and why to a screen reader; its leave is drawn as leave.
        expect(partly.get_by_role('img')).to_have_accessible_name(re.compile(r'7 Oct'))
        expect(partly.locator('.ra-leave')).to_have_count(1)
        # The shared key (dom.ts legend, as every timeline's): the marks this view draws, leave among them.
        key = panel.locator('.view-legend.ra-key')
        expect(key.locator('li').filter(has_text='Leave')).to_have_count(1)
        assert_legend_matches(page, root='.ra', expect_marks=['leave'])
        # Partly away is among those who can work here: only partly-away rows, and not this person.
        views.click()
        page.get_by_role('menu', name='Saved views').get_by_role('menuitemradio', name=re.compile('^Partly away')).click()
        expect(partly).to_have_count(0)
        expect(panel.locator('.ra-row[data-ra]:not([data-ra="part"])')).to_have_count(0)
        # The key names only the marks the shown strips draw: one entry per kind drawn, none for a kind not drawn.
        drawn = panel.locator('.ra-row .ra-strip i').evaluate_all("ns=>[...new Set(ns.map(n=>n.className))]")
        expect(key.locator('li')).to_have_count(len(drawn))
        for kind, words in (('ra-leave', 'Leave'), ('ra-protected', 'Academic or protected'), ('ra-away', 'Rotation elsewhere'), ('ra-out', 'Not in the department')):
            expect(key.locator('li').filter(has_text=words)).to_have_count(1 if kind in drawn else 0)
        # Back to the rules without leaving the panel.
        panel.get_by_role('tab', name='Rules').click()
        expect(page.get_by_role('dialog', name='Rules in this rota')).to_be_visible()
        assert errors == []
    finally:
        context.close()


def test_a_safety_rule_with_several_lines_offers_no_exception_and_says_its_numbers_can_only_be_stricter(browser, live_server, h):
    """PC-QA cycle 3 #10, #11: a safety kind never offers an exception anywhere (AGENTS §3); its lines' numbers are said
    to be stricter-only (never "no adjustable number"); a line edited during the rota (two dated versions that read the
    same) is one row."""
    from .test_browser_rule_catalogue import flag, catalogue_rule, line
    flag(h)
    clinic = h.seed['services'][0]
    rule = catalogue_rule(h, 'people_per_session', [
        line({'max': {'value': 3, 'level': 'mandatory'}}, starts_on='2026-10-04', ends_on='2026-10-07'),
        line({'max': {'value': 3, 'level': 'mandatory'}}, starts_on='2026-10-07', ends_on='2026-10-11'),
        line({'max': {'value': 2, 'level': 'mandatory'}}, where={'conditions': [{'field': 'service_ids', 'values': [clinic]}]})])
    w = h.shift(h.workspace('Safety lines'), *MON)['workspace']  # 4–10 Oct 2026
    context, page = sign_in(browser, live_server, 'coordinator'); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        row = rule_row(panel, rule['name'])
        row.locator('.rk-row-head').click()
        expect(row).not_to_contain_text('no adjustable number')
        # ⋯ says why it can't be changed for this rota, and never offers an exception (a safety kind).
        row.locator('.rk-more').click()
        menu = page.get_by_role('menu', name='More actions')
        stricter = menu.get_by_role('menuitem', name=re.compile('^Make stricter for this rota'))
        expect(stricter).to_be_disabled()
        expect(stricter).to_contain_text('Its lines’ numbers can only be made stricter')
        expect(menu.get_by_role('menuitem', name=re.compile('^Make an exception'))).to_have_count(0)
        row.locator('.rk-row-head').click()
        assert errors == []
    finally:
        context.close()


def test_a_rule_switched_off_for_this_rota_is_listed_with_its_name_reason_and_review(browser, live_server, h):
    """PC-QA cycle 3 #9: a rule switched off for this rota stays listed (above the fold, as changed) with its status in
    words, its reason, who and review state, never as a nameless line under Exceptions here."""
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        duties = rule_row(panel, 'Weekly duty limit')
        rule_action(duties, 'Adjust for this rota')
        duties.get_by_label('Justification', exact=True).fill(WHY)
        duties.get_by_role('button', name='Switch off for this rota', exact=True).click()
        expect(page.locator('.toast').filter(has_text='switched off for this rota')).to_be_visible()
        assert h.detail(w)['workspace']['rule_adjustments'][0]['mode'] == 'off'
        off = rule_row(panel, 'Weekly duty limit')
        expect(off.locator('.rk-row-head')).to_contain_text('Switched off for this rota')
        expect(off.locator('.rk-detail')).to_contain_text(f'Switched off for this rota by you on ')
        expect(off.locator('.rk-detail')).to_contain_text(f'“{WHY}”')
        expect(off.locator('.rk-detail')).to_contain_text('Reviewed when the rota is submitted.')
        expect(panel.locator('.rk-marker').filter(has_text='Switched off for this rota')).to_have_count(0)
    finally:
        context.close()



def test_a_rule_row_is_one_sentence_with_its_status_and_the_met_rest_folds(browser, live_server, h):
    """Why panel v2, slice 1 (CONTRACT §10): a collapsed row is one sentence and its status in words: no rule name, line
    number or chip; safety is a lock. What changed comes first; the met rest folds under one worded toggle."""
    w, rules = rota(h)
    clinic = rules['clinic']
    w = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': clinic['id'], 'mode': 'off', 'reason': WHY},
                  target=w['id'], revision=w['revision'])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        heads = panel.locator('.rk-row-head')
        # The changed rule stays above the fold alongside any other unmet rules.
        expect(heads.filter(has_text='Switched off for this rota.')).to_have_count(1)
        shown = heads.count()
        fold = panel.locator('.rk-fold')
        expect(fold).to_have_text(re.compile(r'^\d+ more (is|are) met$'))
        folded = int(fold.inner_text().split()[0])
        expect(fold).to_have_attribute('aria-expanded', 'false')
        fold.click()
        expect(fold).to_have_attribute('aria-expanded', 'true')
        expect(heads).to_have_count(shown + folded)
        expect(fold).to_be_focused()
        names = [r['name'] for r in rules.values()]
        for head in heads.all():
            text = head.inner_text()
            assert not any(name in text for name in names), text
            assert not re.search(r'#\d', text), text
            assert re.search(r'(Met|Not met|Could be better|Not checked yet|Switched off for this rota)\.', text), text
            assert head.locator('.rk-num, .rk-safety, .rk-win, .chip, [class*="tag"]').count() == 0, text
        rest = rule_row(panel, rules['rest']['name'])
        expect(rest.locator('.rk-row-head').get_by_role('img', name='Safety rule: can only be made stricter')).to_be_visible()
        expect(panel.get_by_role('list', name='Key')).to_contain_text('Can only be made stricter')
    finally:
        context.close()


def test_an_opened_rule_row_says_its_source_and_change_and_keeps_its_actions_under_more(browser, live_server, h):
    """Why panel v2, slice 2 (CONTRACT §10): an opened row is a few sentences (where the number comes from; what changed
    here, by whom, when and why; its review), never the rule's name, then ⋯ through the shared menu: Open rule, Show on
    the schedule, Adjust for this rota, an exception (said why not here) and Make department default. Fix only when it
    fails."""
    w, rules = rota(h)
    duties = rules['duties']
    w = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': duties['id'], 'mode': 'set', 'amount': 6, 'reason': WHY},
                  target=w['id'], revision=w['revision'])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        row = rule_row(panel, duties['name'])
        row.locator('.rk-row-head').click()
        detail = row.locator('.rk-detail')
        expect(detail.locator('p').first).to_contain_text('From the department rule')
        expect(detail).to_contain_text(re.compile(r'Raised from 5 to 6 for this rota by you on .+: “' + re.escape(WHY) + '”'))
        expect(detail).to_contain_text('Reviewed when the rota is submitted.')
        expect(detail.locator('p').first).not_to_contain_text(duties['name'])
        expect(detail).to_contain_text(f'Own number in “{duties["name"]}”: 3 duties instead of 6 duties.')
        expect(row.get_by_role('button', name='Fix', exact=True)).to_have_count(0)  # nothing fails here
        row.locator('.rk-more').click()
        items = page.get_by_role('menu', name='More actions').get_by_role('menuitem')
        expect(items).to_have_text([re.compile('^Open rule'), re.compile('^(Show on|Stop showing on) the schedule'),
                                    re.compile('^Adjust for this rota'), re.compile('^Make an exception'),
                                    re.compile('^Make department default…')])
        expect(items.nth(3)).to_be_disabled()  # the whole rota: an exception needs a person, clinic, place, duty or day
        items.nth(2).click()
        expect(row.get_by_role('form', name='Adjust for this rota')).to_be_visible()
        expect(row.get_by_label(f"New number for “{duties['name']}” in this rota")).to_be_focused()
    finally:
        context.close()


def test_two_rules_that_cant_both_hold_come_first_with_their_line_numbers_and_open_both(browser, live_server, h):
    """Why panel v2, slice 3 (CONTRACT §6, §10): a conflict the explainer reports is the first row, in the server's own
    sentence; only there, opened, are the two lines numbered, with what it reaches; Open both opens both rules' rows."""
    w, rules = rota(h)
    a, b = rules['duties'], rules['clinic']
    sentence = 'These two can’t both be true: at most 5 duties a week, and every Review clinic session needs 6 people'
    conflict = {'conflict_id': 'c1', 'sentence': sentence, 'line_ids': ['l57', 'l118'], 'rule_ids': [a['id'], b['id']],
                'line_refs': [57, 118], 'reach': {'sessions_count': 3}}

    def with_conflict(route):
        reply = route_fetch(route)
        body = reply.json()
        route.fulfill(response=reply, json={**body, 'conflicts': [conflict]})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/rules-explained$'), with_conflict)
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        row = panel.locator('.rk-row').first
        expect(row).to_have_attribute('data-rk-conflict', 'c1')
        head = row.locator('.rk-row-head')
        expect(head).to_have_text(sentence + '.')
        expect(head).not_to_contain_text('#')  # lines are numbered only once it is opened
        expect(panel.get_by_role('list', name='Key')).to_contain_text('Two rules can’t both be true')
        head.click()
        expect(row.locator('.rk-detail')).to_contain_text('Lines #57 and #118.')
        expect(row.locator('.rk-detail')).to_contain_text('3 sessions are affected.')
        row.get_by_role('button', name='Open both', exact=True).click()
        for rule in (a, b):
            expect(panel.locator(f'[data-rk-row="{rule["id"]}"] .rk-row-head')).to_have_attribute('aria-expanded', 'true')
        # Every other row stays unnumbered (the slice 1 guard, here with a conflict on screen).
        for other in panel.locator('.rk-row:not(.rk-conflict) .rk-row-head').all():
            assert not re.search(r'#\d', other.inner_text()), other.inner_text()
    finally:
        context.close()


def test_the_rules_list_groups_by_question_with_one_sentence_per_rule(browser, live_server, h):
    """Why panel v2, slices 4 and 5 (CONTRACT §5, §10): the Rules list groups by the server's questions in its order, with its
    letters and labels as sent; each rule leads with the server's one sentence (a lock for safety, the name kept as its
    link); a conflict says its sentence and two line numbers once, and Open both opens the rule's lines. While the route
    read fails the list keeps its families, and says so."""
    from .test_browser_rule_catalogue import flag, catalogue_rule, line
    flag(h)
    rule = catalogue_rule(h, 'people_per_session', [line({'max': {'value': 3, 'level': 'mandatory'}})])
    entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['rule'] and e['rule']['id'] == rule['id'])
    summary = 'Each session has at most 3 people.'
    projection = {'groups': [{'key': 'who_where', 'letter': 'A', 'label': 'Who may work where', 'order': 1},
                             {'key': 'staffing', 'letter': 'B', 'label': 'Staffing in each session', 'order': 2}],
                  'rules': [{'rule_id': rule['id'], 'catalogue_id': entry['id'], 'question_group': 'staffing', 'safety': True,
                             'level': 'mandatory', 'summary_sentence': summary, 'line_ids': []}],
                  'conflicts': [{'conflict_id': 'c1', 'sentence': 'These two can’t both be true: at most 3 people, and at least 4.',
                                 'line_ids': ['x', 'y'], 'rule_ids': [rule['id'], rule['id']], 'line_refs': [12, 40],
                                 'reach': {'sessions_count': 2}}],
                  # Slice 5: ranks and only the people who differ, only populated columns, the server's words per cell.
                  'people_table': {'columns': [{'key': 'cap', 'label': 'Monthly cap', 'rule_ids': [rule['id']]}],
                                   'rows': [{'kind': 'rank', 'id': 'r-fe', 'display': 'Fellow',
                                             'cells': {'cap': {'display': '14', 'cell': '14', 'rule_id': rule['id']}}},
                                            {'kind': 'person', 'id': 'p-a', 'display': 'Dr Sample Alpha', 'parent': 'r-fe',
                                             'cells': {'cap': {'display': '18', 'cell': '18, an exception', 'rule_id': rule['id'],
                                                               'kind': 'exception'}}}]}}
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/api/rules-explained\?group_by=question.*'), lambda route: route.fulfill(json=projection))
        navigate(page, 'rules')
        main = page.locator('#main')
        heads = main.locator('tr.rt-family')
        expect(heads.first).to_contain_text('Staffing in each session')  # A has no rule here: not drawn
        expect(heads.first.locator('.rt-letter')).to_have_text('B')
        row = main.locator(f'tr.rt-rule[data-rule="{entry["id"]}"]')
        expect(row.locator('.rt-sentence')).to_have_text(summary)
        expect(row.get_by_role('img', name='Safety rule: can only be made stricter')).to_be_visible()
        expect(row.get_by_role('button', name=entry['name'], exact=True)).to_be_visible()  # the link to its page
        conflict = main.locator('tr.rt-conflict')
        expect(conflict).to_have_count(1)
        expect(conflict).to_contain_text('These two can’t both be true: at most 3 people, and at least 4. Lines #12 and #40.')
        conflict.get_by_role('button', name='Open both').click()
        expect(row.locator('.rt-toggle')).to_have_attribute('aria-expanded', 'true')
        # The people table, once, under the question whose rules it reads (here B), in the server's words.
        table = main.get_by_role('table', name='Staffing in each session')
        expect(table.get_by_role('columnheader')).to_have_text(['Who', 'Monthly cap'])
        expect(table.get_by_role('row', name=re.compile('^Fellow'))).to_contain_text('14')
        expect(table.get_by_role('row', name=re.compile('^Dr Sample Alpha'))).to_contain_text('18, an exception')
        # How to read it is help on the table's caption, not a visible line (AGENTS §2, DESIGN).
        expect(table.get_by_role('button', name='People are listed only where they differ from their rank.')).to_be_visible()
        expect(main.get_by_text('People are listed only where they differ from their rank.', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_the_rules_list_moves_a_kept_family_default_to_question_once(browser, live_server, h):
    """Why panel v2 slice 4: a device that kept the old default grouping (Family) moves to Question once, keeping its
    other settings; Family chosen after that stays (LEAD-UIUX: no store reset)."""
    from .test_browser_rule_catalogue import flag, catalogue_rule, line
    flag(h)
    rule = catalogue_rule(h, 'people_per_session', [line({'max': {'value': 3, 'level': 'mandatory'}})])
    entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['rule'] and e['rule']['id'] == rule['id'])
    projection = {'groups': [{'key': 'staffing', 'letter': 'B', 'label': 'Staffing in each session', 'order': 1}],
                  'rules': [{'rule_id': rule['id'], 'catalogue_id': entry['id'], 'question_group': 'staffing', 'safety': False,
                             'level': 'mandatory', 'summary_sentence': 'Each session has at most 3 people.', 'line_ids': []}], 'conflicts': []}
    me = h.get('coordinator', '/api/me')
    key = f"rota.rules.tree.{me['identity_id']}.{me['department']['id']}"
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/api/rules-explained\?group_by=question.*'), lambda route: route.fulfill(json=projection))
        page.evaluate("([k, v]) => localStorage.setItem(k, JSON.stringify(v))", [key, {'group': 'family', 'wrap': ['who']}])
        navigate(page, 'rules')
        main = page.locator('#main')
        expect(main.locator('tr.rt-family').first.locator('.rt-letter')).to_have_text('B')  # moved to Question
        kept = page.evaluate("k => JSON.parse(localStorage.getItem(k))", key)
        assert kept['group'] == 'question' and kept['question_default'] is True and kept['wrap'] == ['who'], kept
        page.evaluate("([k, v]) => localStorage.setItem(k, JSON.stringify(v))", [key, {**kept, 'group': 'family'}])
        page.reload()
        expect(main.locator('tr.rt-rule').first).to_be_visible()
        expect(main.locator('.rt-letter')).to_have_count(0)  # Family, chosen after the move, stays
    finally:
        context.close()

def test_the_rules_questions_read_that_fails_once_says_so_and_retry_reads_them_again(browser, live_server, h):
    """QA-133: the Rules page asks for its questions on open; a failed read (500) shows the shared failure note (its
    reference, Report this problem) over the family list, and its Retry sends exactly one new request (never the app refresh, never a held read) and draws the groups in place."""
    from .test_browser_rule_catalogue import flag, catalogue_rule, line
    flag(h)
    rule = catalogue_rule(h, 'people_per_session', [line({'max': {'value': 3, 'level': 'mandatory'}})])
    entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['rule'] and e['rule']['id'] == rule['id'])
    projection = {'groups': [{'key': 'staffing', 'letter': 'B', 'label': 'Staffing in each session', 'order': 1}],
                  'rules': [{'rule_id': rule['id'], 'catalogue_id': entry['id'], 'question_group': 'staffing', 'safety': False,
                             'level': 'mandatory', 'summary_sentence': 'Each session has at most 3 people.', 'line_ids': []}], 'conflicts': []}
    asked = []

    def questions(route):
        asked.append(route.request.url)
        if len(asked) == 1:
            route.fulfill(status=500, json={'error': {'code': 'INTERNAL_ERROR', 'message': 'The service had a problem.'},
                                            'correlation_id': 'ref-qa133-test'})
        else:
            route.fulfill(json=projection)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/api/rules-explained\?group_by=question.*'), questions)
        navigate(page, 'rules', alert=True)  # the first questions read answers 500: its failure note is expected
        main = page.locator('#main')
        failure = main.locator('.notice.error', has_text='Couldn’t load the rules’ questions.')
        expect(failure).to_be_visible()
        # The shared failure note never hides the error: its reference, and Report this problem (QA-133).
        expect(failure).to_contain_text('Reference: ref-qa133-test')
        expect(failure.get_by_role('button', name='Report this problem')).to_be_visible()
        expect(main.locator('tr.rt-rule').first).to_be_visible()  # the family list still works under it
        assert len(asked) == 1  # the page asked on open
        expect(main.locator('.rt-letter')).to_have_count(0)
        failure.get_by_role('button', name='Retry', exact=True).click()
        expect(main.locator('tr.rt-family').first.locator('.rt-letter')).to_have_text('B')
        expect(failure).to_have_count(0)
        assert len(asked) == 2  # exactly one new request
    finally:
        context.close()


def test_the_rules_questions_refusal_has_no_error_or_retry(browser, live_server, h):
    """B128: a refusal is the role's, never an error with a pointless Retry."""
    from .test_browser_rule_catalogue import catalogue_rule, line
    catalogue_rule(h, 'people_per_session', [line({'max': {'value': 3, 'level': 'mandatory'}})])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/api/rules-explained\?group_by=question.*'),
                   lambda route: route.fulfill(status=403, json={
                       'error': {'code': 'FORBIDDEN', 'message': 'Your current role does not permit this action'}}))
        navigate(page, 'rules')
        main = page.locator('#main')
        expect(main.locator('.rl-row').first).to_be_visible()
        expect(main.locator('.notice', has_text='Couldn’t read the rules’ questions')).to_have_count(0)
        expect(main.get_by_role('button', name='Retry', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_who_is_available_defaults_to_who_can_work_in_this_rota(browser, live_server, h):
    """Who's available answers "who could I schedule in this rota?": by default the people the server says can work one of
    its sessions (rota_eligible, the shared qualified() check); someone who cannot is listed only under Everyone in the
    department."""
    from .test_timeline_data import rota_eligibility_fixture
    w, eligible, not_eligible = rota_eligibility_fixture(h)  # a rank-scoped duty: only the first holds the rank
    names = {p['id']: p['display_name'] for p in h.get('coordinator', f"/api/timeline?start=2026-10-04&end=2026-10-11"
             f"&person_ids={eligible}&person_ids={not_eligible}")['people']}
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, h.detail(w)['workspace'])
        planner_tool(page, 'Who’s available')
        panel = page.get_by_role('dialog', name='Who’s available')
        row = lambda pid: panel.locator('.ra-row').filter(has=page.get_by_role('button', name=names[pid]))
        views = panel.locator('.dt-toolbar .dt-saved')
        expect(views).to_have_accessible_name('Saved views: Can work in this rota')
        expect(row(eligible)).to_have_count(1)
        expect(row(not_eligible)).to_have_count(0)
        views.click()
        page.get_by_role('menu', name='Saved views').get_by_role('menuitemradio', name=re.compile('^Everyone in the department')).click()
        expect(row(eligible)).to_have_count(1)
        expect(row(not_eligible)).to_have_count(1)
    finally:
        context.close()


def test_a_rota_with_no_sessions_shows_everyone(browser, live_server, h):
    """No sessions yet: the server has no eligibility to give (rota_eligible null), so everyone shows and the view menu
    has no Can work in this rota."""
    w = h.workspace('No sessions yet')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        planner_tool(page, 'Who’s available')
        panel = page.get_by_role('dialog', name='Who’s available')
        views = panel.locator('.dt-toolbar .dt-saved')
        expect(views).to_have_accessible_name('Saved views: Everyone in the department')
        expect(panel.locator('.ra-row:not(.ra-scale)').first).to_be_visible()
        views.click()
        expect(page.get_by_role('menu', name='Saved views').get_by_role('menuitemradio', name=re.compile('^Can work in this rota'))).to_have_count(0)
    finally:
        context.close()


def test_who_is_available_reads_its_weeks_on_the_timelines_axis(browser, live_server, h):
    """The strip's date axis is the timeline's (timeline.ts units): department weeks (Sunday here), the rota's first and
    last cut at its ends, each labelled with its first day in full; every strip spans the row and carries the same week
    lines at 3:1 (--control-line). Live check 6cea25859: weeks from the rota's Tuesday, labels cut, lines at 1.4:1."""
    a = h.seed['people'][0]
    w = h.workspace('Axis check', start='2027-06-01', end='2027-07-01')  # Tuesday 1 – Wednesday 30 June
    w = h.shift(w, '2027-06-01T08:00:00+03:00', '2027-06-01T12:00:00+03:00', people=[a])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        planner_tool(page, 'Who’s available')
        panel = page.get_by_role('dialog', name='Who’s available')
        ticks = panel.locator('.ra-ticks span')
        expect(ticks).to_have_text(['1 Jun', '6 Jun', '13 Jun', '20 Jun', '27 Jun'])
        strip = panel.locator('.ra-row:not(.ra-scale) .ra-strip').first
        expect(strip).to_have_css('background-image', re.compile('repeating-linear-gradient'))
        line = page.evaluate("""() => {const p = document.createElement('i'); p.style.setProperty('color', 'var(--control-line)');
          document.body.append(p); const c = getComputedStyle(p).color; p.remove(); return c;}""")
        assert line in strip.evaluate('s => getComputedStyle(s).backgroundImage')
        assert strip.evaluate("s => getComputedStyle(s).getPropertyValue('--o').trim()") == '5'  # 1–5 June, then Sunday
        for width in (1440, 390):
            page.set_viewport_size({'width': width, 'height': 900 if width > 400 else 844})
            expect(ticks.first).to_be_visible()
            assert ticks.evaluate_all('ts => ts.filter(t => t.scrollWidth > t.clientWidth).map(t => t.textContent)') == []
            row = strip.locator('xpath=..')
            assert strip.bounding_box()['width'] >= row.bounding_box()['width'] - 1  # the strip has the row's width
    finally:
        context.close()



def test_a_people_table_cell_never_hides_its_limits_behind_a_bare_number(browser, live_server, h):
    """Owner P1 (8 Nov): the UCC overtime cap read "at most 2 duties" in Workload per person, though it is overtime only,
    at two UCC services, until 1 Jan 2027. A cell renders the server's `cell` words (every qualifier) as sent, never its
    bare `display`."""
    from .test_browser_rule_catalogue import flag, catalogue_rule, line
    flag(h)
    rule = catalogue_rule(h, 'people_per_session', [line({'max': {'value': 2, 'level': 'mandatory'}})])
    entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['rule'] and e['rule']['id'] == rule['id'])
    worded = 'at most 2 UCC overtime duties a month, until 31 Dec 2026'
    projection = {'at': '2026-11-08', 'groups': [{'key': 'workload_person', 'letter': 'D', 'label': 'Workload per person', 'order': 1}],
                  'rules': [{'rule_id': rule['id'], 'catalogue_id': entry['id'], 'question_group': 'workload_person', 'safety': False,
                             'level': 'mandatory', 'summary_sentence': 'Each session has at most 2 people.', 'line_ids': []}],
                  'conflicts': [],
                  'people_table': {'columns': [{'key': 'ucc', 'label': 'UCC overtime', 'rule_ids': [rule['id']]}],
                                   'rows': [{'kind': 'rank', 'id': 'r-co', 'display': 'Consultant',
                                             'cells': {'ucc': {'display': 'at most 2 duties', 'cell': worded, 'qualified': True,
                                                               'rule_id': rule['id'], 'kind': 'line'}}}]}}
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/api/rules-explained\?group_by=question.*'), lambda route: route.fulfill(json=projection))
        navigate(page, 'rules')
        table = page.locator('#main').get_by_role('table', name='Workload per person')
        expect(table.get_by_role('row', name=re.compile('^Consultant')).get_by_role('cell')).to_have_text([worded])  # as sent
        expect(table).not_to_contain_text('at most 2 duties')
    finally:
        context.close()


def test_a_rule_with_several_sentences_reads_the_explainers_summary_not_the_catalogue(browser, live_server, h):
    """Live 235391b (1g): 14 of 17 panel rows showed catalogue text, because a rule with several sentences and no line of
    the selection's own fell back to it. Such a row reads the explainer's summary_sentence."""
    from .test_browser_rule_catalogue import catalogue_rule, line
    w, _ = rota(h)
    clinic = h.seed['services'][0]
    rule = catalogue_rule(h, 'people_per_session', [
        line({'max': {'value': 3, 'level': 'mandatory'}}),
        line({'max': {'value': 2, 'level': 'mandatory'}}, where={'conditions': [{'field': 'service_ids', 'values': [clinic]}]})])
    told = next(r for r in h.get('coordinator', f"/api/workspaces/{w['id']}/rules-explained")['rules'] if r['rule_id'] == rule['id'])
    assert len(told['sentences']) > 1 and told['summary_sentence'], told
    entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['rule'] and e['rule']['id'] == rule['id'])
    summary = told['summary_sentence'].strip()
    summary = summary if re.search(r'[.!?…”]$', summary) else summary + '.'
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')  # nothing selected: no line of the selection's own
        panel = page.get_by_role('dialog', name='Rules in this rota')
        sentence = rule_row(panel, rule['name']).locator('.rk-row-sentence')
        expect(sentence).to_have_text(summary)
        assert entry['description'] not in sentence.inner_text()
    finally:
        context.close()



UNEXPLAINED = "Can\u2019t explain this rule right now. Its check still applies."
UNEXPLAINED_SHORT = "Can\u2019t explain right now"


def test_schedule_rules_panel_marks_an_unexplained_safety_rule(browser, live_server, h):
    """DL-037: the schedule panel keeps an unexplained safety rule visible, worded and counted as a warning."""
    workspace, rules = rota(h)

    def mark_rest_unexplained(route):
        response = route_fetch(route)
        body = response.json()
        row = next(item for item in body['rules'] if item['rule_id'] == rules['rest']['id'])
        row.update(unexplained=True, sentences=[])
        route.fulfill(response=response, json=body)

    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/workspaces/[^/]+/rules-explained$'), mark_rest_unexplained)
        open_planner(page, workspace)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        row = rule_row(panel, rules['rest']['name'])
        warning = row.locator('.rule-unexplained')
        expect(warning).to_have_text(UNEXPLAINED)
        expect(warning.locator('.iss-mark.warning .icon')).to_have_count(1)
        expect(row.locator('.rk-status')).to_have_text(re.compile(r'\S+'))
        expect(row.get_by_role('img', name='Safety rule: can only be made stricter')).to_be_visible()
        summary_warning = panel.locator('.rk-head small .rule-unexplained')
        expect(summary_warning).to_have_text('1 rule can\u2019t be explained right now')
        expect(summary_warning.locator('.iss-mark.warning .icon')).to_have_count(1)
    finally:
        context.close()


def test_rules_list_marks_an_unexplained_rule_in_each_grouping(browser, live_server, h):
    """DL-037: question and family groupings both surface the same warning and saved-view count."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line

    flag(h)
    rule = catalogue_rule(h, 'people_per_session', [line({'max': {'value': 3, 'level': 'mandatory'}})])
    entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['rule'] and e['rule']['id'] == rule['id'])
    projection = {'groups': [{'key': 'staffing', 'letter': 'B', 'label': 'Staffing in each session', 'order': 2}],
                  'rules': [{'rule_id': rule['id'], 'catalogue_id': entry['id'], 'question_group': 'staffing', 'safety': True,
                             'level': 'mandatory', 'summary_sentence': 'Each session has at most 3 people.', 'unexplained': True,
                             'line_ids': []}], 'conflicts': [], 'people_table': None}
    me = h.get('coordinator', '/api/me')
    storage_key = f"rota.rules.tree.{me['identity_id']}.{me['department']['id']}"
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/api/rules-explained\?group_by=question.*'), lambda route: route.fulfill(json=projection))
        for index, grouping in enumerate(('question', 'family')):
            page.evaluate("([key, value]) => localStorage.setItem(key, JSON.stringify(value))",
                          [storage_key, {'group': grouping, 'question_default': True, 'wrap': ['who']}])
            if index:
                page.reload()
            else:
                navigate(page, 'rules')
            main = page.locator('#main')
            row = main.locator(f'tr.rt-rule[data-rule="{entry["id"]}"]')
            marker = row.locator('.rule-unexplained')
            expect(marker).to_have_text(UNEXPLAINED if grouping == 'question' else UNEXPLAINED_SHORT)
            expect(marker.locator('.iss-mark.warning .icon')).to_have_count(1)
            lock = row.get_by_role('img', name='Safety rule: can only be made stricter')
            if grouping == 'question':
                expect(lock).to_be_visible()
            else:
                expect(lock).to_be_visible()
            expect(row.get_by_role('button', name=entry['name'], exact=True)).to_be_visible()
            expect(main.locator('button.dt-saved .dt-view-warn')).to_contain_text('1 rule can\u2019t be explained right now')
    finally:
        context.close()


def test_rules_list_shows_one_safety_lock_and_none_for_a_preference_in_each_grouping(browser, live_server, h):
    """G7: a safety and a non-safety rule keep their own lock state in Family, Question and ungrouped views."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line

    flag(h)
    catalogue_rule(h, 'rest_between_duties', [line({'min': {'value': 11, 'level': 'mandatory'}})])
    catalogue_rule(h, 'preferences', [line({'priority': {'value': 1, 'level': 'medium'}},
                                            who={'conditions': [{'field': 'person_ids', 'values': [h.seed['people'][0]]}]})])
    entries = h.get('coordinator', '/api/rules/catalogue')['entries']
    rest = next(e for e in entries if e['id'] == 'rest_between_duties')
    preference = next(e for e in entries if e['id'] == 'preferences')
    assert rest['rule'] and preference['rule']
    projection = {'groups': [{'key': 'workload_person', 'letter': 'D', 'label': 'Workload per person', 'order': 1},
                             {'key': 'preferences', 'letter': 'F', 'label': 'Fairness and preferences', 'order': 2}],
                  'rules': [{'rule_id': rest['rule']['id'], 'catalogue_id': rest['id'], 'question_group': 'workload_person',
                             'safety': True, 'level': 'mandatory', 'summary_sentence': 'At least 11 hours between duties.',
                             'line_ids': []},
                            {'rule_id': preference['rule']['id'], 'catalogue_id': preference['id'], 'question_group': 'preferences',
                             'safety': False, 'level': 'medium', 'summary_sentence': 'Some duties could better match preferences.',
                             'line_ids': []}],
                  'conflicts': [], 'people_table': None}
    me = h.get('coordinator', '/api/me')
    storage_key = f"rota.rules.tree.{me['identity_id']}.{me['department']['id']}"
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/api/rules-explained\?group_by=question.*'), lambda route: route.fulfill(json=projection))
        for index, grouping in enumerate(('family', 'question', '')):
            page.evaluate("([key, value]) => localStorage.setItem(key, JSON.stringify(value))",
                          [storage_key, {'group': grouping, 'question_default': True, 'wrap': ['who']}])
            if index:
                page.reload()
            else:
                navigate(page, 'rules')
            main = page.locator('#main')
            safety = main.locator(f'tr.rt-rule[data-rule="{rest["id"]}"]')
            non_safety = main.locator(f'tr.rt-rule[data-rule="{preference["id"]}"]')
            expect(safety).to_have_count(1)
            expect(non_safety).to_have_count(1)
            lock = safety.locator('.rk-lockmark[role="img"]')
            expect(lock).to_have_count(1)
            expect(lock).to_have_accessible_name('Safety rule: can only be made stricter')
            expect(non_safety.locator('.rk-lockmark[role="img"]')).to_have_count(0)
    finally:
        context.close()


def test_rules_key_hides_safety_lock_when_a_family_filter_shows_only_preferences(browser, live_server, h):
    """G7: the shared Rules key follows the filtered rows and drops its safety entry when none are shown."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line

    flag(h)
    catalogue_rule(h, 'rest_between_duties', [line({'min': {'value': 11, 'level': 'mandatory'}})])
    catalogue_rule(h, 'preferences', [line({'priority': {'value': 1, 'level': 'medium'}},
                                            who={'conditions': [{'field': 'person_ids', 'values': [h.seed['people'][0]]}]})])
    entries = h.get('coordinator', '/api/rules/catalogue')['entries']
    rest = next(e for e in entries if e['id'] == 'rest_between_duties')
    preference = next(e for e in entries if e['id'] == 'preferences')
    projection = {'groups': [{'key': 'workload_person', 'letter': 'D', 'label': 'Workload per person', 'order': 1},
                             {'key': 'preferences', 'letter': 'F', 'label': 'Fairness and preferences', 'order': 2}],
                  'rules': [{'rule_id': rest['rule']['id'], 'catalogue_id': rest['id'], 'question_group': 'workload_person',
                             'safety': True, 'level': 'mandatory', 'summary_sentence': 'At least 11 hours between duties.',
                             'line_ids': []},
                            {'rule_id': preference['rule']['id'], 'catalogue_id': preference['id'], 'question_group': 'preferences',
                             'safety': False, 'level': 'medium', 'summary_sentence': 'Some duties could better match preferences.',
                             'line_ids': []}],
                  'conflicts': [], 'people_table': None}
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/api/rules-explained\?group_by=question.*'), lambda route: route.fulfill(json=projection))
        navigate(page, 'rules')
        main = page.locator('#main')
        expect(main.get_by_role('list', name='Key').get_by_text('safety rule: can only be made stricter', exact=True)).to_have_count(1)
        filter_by(main, 'Family', ['Preferences & fairness'])
        expect(main.locator(f'tr.rt-rule[data-rule="{rest["id"]}"]')).to_have_count(0)
        expect(main.locator(f'tr.rt-rule[data-rule="{preference["id"]}"]')).to_have_count(1)
        expect(main.get_by_role('list', name='Key').get_by_text('safety rule: can only be made stricter', exact=True)).to_have_count(0)
    finally:
        context.close()

"""Rule builder fixes from the live DEV review reviewed: Count and Match as labelled controls with their
meaning in words; the Tag field's empty state; a waiting proposal in brief (only the changed lines, unchanged on
demand); the strength menu ticks only the chosen strength; strength as a labelled badge, never "!"; the line panel's
title follows what is typed and names every Where filter; its actions never move; the Reach column shows the panel's
reach. Desktop light and dark, and a rule at department scale (1,000 lines)."""
import json
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from .test_browser import add_filter, browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import catalogue_rule, flag, line, shot
from .test_browser_catalogue_approval_checks import pending, row_menu
from .test_browser_rule_filter_builder import open_line, pick, sides

pytestmark = pytest.mark.browser

VIEWS = [(1440, 'light'), (1440, 'dark'), (1024, 'light')]


def staffing(h):
    s = h.seed
    return catalogue_rule(h, 'kind_per_session', [line({'min': {'value': 1, 'level': 'mandatory'}},
        who={'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [s['category']]}]})])


@pytest.mark.parametrize('width,scheme', VIEWS)
def test_count_and_match_are_labelled_with_their_meaning(browser, live_server, h, width, scheme):
    """Two ranks on a staffing line: "Match: Any of · All of" and "Count: Together · Each", each said in words beside
    it; one value shows neither; the Tag field with no tags says so and links to Configuration › Tags."""
    flag(h)
    rule = staffing(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 1000})
    try:
        page.emulate_media(color_scheme=scheme)
        panel = open_line(page, 'kind_per_session', rule['definition']['rule']['lines'][0]['id'])
        who, _ = sides(panel)
        row = who.locator('.cond-line').first
        expect(row.locator('.cond-more')).to_be_hidden()  # one rank: nothing to count or match
        pick(page, row, None, ['Demo learners'])
        match = row.get_by_role('radiogroup', name='Match')  # the shared segmented() (dom.ts)
        count = row.get_by_role('radiogroup', name='Count')
        expect(match.get_by_role('radio', name='Any of', exact=True)).to_have_attribute('aria-checked', 'true')
        expect(count.get_by_role('radio', name='Together', exact=True)).to_have_attribute('aria-checked', 'true')
        expect(row.locator('.cond-choice-label')).to_have_text(['Match', 'Count'])
        expect(row.locator('.cond-hint')).to_have_text('any one of them · all ranks counted together')
        count.get_by_role('radio', name='Each', exact=True).click()
        expect(row.locator('.cond-hint')).to_have_text('any one of them · each rank counted separately')
        expect(panel.locator('.rc-says')).to_contain_text('1 from each of Demo practitioners and Demo learners')
        match.get_by_role('radio', name='All of', exact=True).click()
        expect(row.get_by_label('Operator', exact=True)).to_have_value('all')
        expect(row.locator('.cond-hint')).to_contain_text('must have every one')
        shot(page, f'live-count-match-{width}-{scheme}', full_page=False)
        # Tag: no tags yet reads as an empty state with the way to make some, not an empty picker.
        add_filter(who)
        tag = who.locator('.cond-line').nth(1)
        tag.get_by_label('Filter on', exact=True).select_option(label='Tag')  # no tags: no picker opens
        expect(tag.locator('.cond-empty')).to_contain_text('No tags yet')
        expect(tag.get_by_role('button', name='Create tags', exact=True)).to_be_visible()
        shot(page, f'live-tag-empty-{width}-{scheme}', full_page=False)
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', VIEWS)
def test_line_panel_title_badges_strength_menu_and_steady_footer(browser, live_server, h, width, scheme):
    """A new line at a clinic and a duty type: the title names both and follows the number typed; the strength menu
    ticks one option; numbers show "Mandatory" as a badge, never "!"; the footer does not move; Reach matches."""
    flag(h)
    a = h.seed['people'][0]
    rule = catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Department default')])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 1000})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules/duties_per_week')
        main = page.locator('#main')
        expect(main.locator('tr[data-row]')).to_have_count(1)
        expect(main.locator('tr[data-row]').first).not_to_contain_text('!')
        expect(main.locator('tr[data-row] .rc-level').first).to_have_text('Mandatory')
        main.get_by_role('button', name='New line: a person, rank, clinic or #tag').click()
        page.get_by_role('combobox', name='Search New line').fill('Continuity clinic')
        page.get_by_role('option', name=re.compile('^Continuity clinic')).first.click()
        panel = page.locator('.rc-line-panel')
        _, where = sides(panel)
        add_filter(where)
        pick(page, where.locator('.cond-line').nth(1), 'Duty type', ['Clinical review session'])
        head = panel.locator('.side-head h2')
        expect(head).to_contain_text('Everyone at Continuity clinic · Clinical review session · no number')
        foot = panel.locator('.rc-lp-foot')
        before = foot.bounding_box()
        maximum = panel.get_by_label('Maximum', exact=True)
        maximum.fill('11')
        expect(panel.locator('.rc-says-text')).to_contain_text('11')
        expect(head).to_contain_text('≤ 11 · ')
        expect(head).not_to_contain_text('no number')
        expect(head).not_to_contain_text('!')
        expect(panel.locator('.rc-says')).to_contain_text('at Continuity clinic · Clinical review session')
        after = foot.bounding_box()
        assert abs(after['y'] - before['y']) < 1, (before, after)  # the actions never move under the pointer
        # The strength menu: only the chosen strength is ticked.
        chip = panel.locator('.rc-strength').nth(1)
        chip.click()
        menu = page.get_by_role('menu', name=re.compile('strength'))
        expect(menu.locator('[aria-checked=true]')).to_have_count(1)
        expect(menu.locator('.tick')).to_have_count(1)
        shot(page, f'live-strength-menu-{width}-{scheme}', full_page=False)
        menu.get_by_role('menuitemradio', name=re.compile('Advisory · high')).click()
        expect(chip).to_have_text('Advisory · high')
        expect(head).to_contain_text('≤ 11 · Advisory · high')
        panel.get_by_role('button', name='Keep change').click()
        rows = main.locator('tr[data-row]')
        expect(rows).to_have_count(2)
        # Reach: the new line's column says what its panel says, never "—".
        reach_link = page.locator('.rc-line-panel .rc-reach-link').first
        expect(reach_link).to_be_visible(timeout=15000)
        people = re.match(r'(\d+)', reach_link.inner_text()).group(1)
        new_row = main.locator('tr.rc-changed')
        expect(new_row.locator('td[data-col=reach]')).to_have_text(people)
        shot(page, f'live-line-panel-{width}-{scheme}', full_page=False)
        assert a  # the seed's people exist (reach counts them)
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', VIEWS)
def test_a_waiting_proposal_shows_only_its_changes(browser, live_server, h, width, scheme):
    """One line added to a rule of three: "1 line added · 0 changed · 0 removed", a table of that line, the three
    unchanged ones only on demand, the proposer and Withdraw."""
    flag(h)
    s = h.seed
    rule = catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}}),
        line({'max': {'value': 3, 'level': 'mandatory'}}, who={'conditions': [{'field': 'person_ids', 'values': [s['people'][0]]}]}),
        line({'max': {'value': 4, 'level': 'medium'}}, where={'conditions': [{'field': 'service_ids', 'values': s['services']}]})])
    lines = rule['definition']['rule']['lines'] + [line({'max': {'value': 2, 'level': 'mandatory'}},
        who={'conditions': [{'field': 'person_ids', 'values': [s['people'][1]]}]}, where={'conditions': [{'field': 'service_ids', 'values': s['services']}]})]
    check = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check', json={'lines': lines}).json()
    h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_week', 'lines': lines, 'reason': 'Second person limit', 'expected_policy_id': rule['id'], 'expected_revision': rule['revision'],
                                             'acknowledged_overlaps': [o['line_id'] for o in check['overlaps']]})
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 1000})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules/duties_per_week')
        note = page.get_by_role('region', name='Proposed change, waiting for approval')
        expect(note.locator('.rc-prop-summary')).to_have_text('1 line added')
        rows = note.locator('tbody tr')
        expect(rows).to_have_count(1)
        expect(rows.first.locator('td').first).to_have_text('Added')
        expect(note).not_to_contain_text('~')
        expect(note).to_contain_text('You proposed it')
        expect(note).to_contain_text('Another person approves it')
        expect(note.get_by_role('button', name='Withdraw', exact=True)).to_be_visible()
        shot(page, f'live-proposal-{width}-{scheme}', full_page=False)
        more = note.get_by_role('button', name='Show 3 unchanged lines', exact=True)
        more.click()
        expect(rows).to_have_count(4)
        expect(note.locator('tr.rc-prop-same')).to_have_count(3)
        note.get_by_role('button', name='Hide unchanged lines', exact=True).click()
        expect(rows).to_have_count(1)
    finally:
        context.close()


def test_department_scale_proposal_and_panel(browser, live_server, h):
    """1,000 lines in force and one added: the note still lists one line, the panel opens and its title follows."""
    flag(h)
    day = lambda n: str(date(2027, 1, 1) + timedelta(days=n))  # noqa: E731 - one line per day: none is the same line
    rule = catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}}, starts_on=day(n), ends_on=day(n + 1)) for n in range(1000)])
    lines = rule['definition']['rule']['lines'] + [line({'max': {'value': 4, 'level': 'mandatory'}}, starts_on=day(1001), ends_on=day(1002))]
    h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_week', 'lines': lines, 'reason': 'One more day',
                                             'expected_policy_id': rule['id'], 'expected_revision': rule['revision']})
    context, page = sign_in(browser, live_server, 'approver', {'width': 1440, 'height': 1000})
    try:
        navigate(page, 'rules/duties_per_week')
        note = page.get_by_role('region', name='Proposed change, waiting for approval')
        expect(note.locator('.rc-prop-summary')).to_have_text('1 line added')
        expect(note.locator('tbody tr')).to_have_count(1)
        expect(note.get_by_role('button', name='Show 1000 unchanged lines', exact=True)).to_be_visible()
        # SW3-B-16: the proposer by name, as Home and the decision panel name them.
        expect(note.locator('.rc-prop-who')).to_contain_text(re.compile(r'^Proposed by (?!another person)\S'))
        shot(page, 'live-proposal-scale-1440-light', full_page=False)
        # A line of the 1,000 opens in its panel, its numbers in words with the strength badge.
        first = rule['definition']['rule']['lines'][0]['id']
        panel = open_line(page, 'duties_per_week', first)
        expect(panel.locator('.side-head h2')).to_contain_text('≤ 5 · Mandatory')
        expect(panel.locator('.side-head h2')).not_to_contain_text('!')
    finally:
        context.close()


FINDINGS = """async () => {const {findingsDiff}=await window.importBundle('rule_review.js');
  const short=(bound,name='Evening cover')=>({requirement_name:name,service_ids:[],start:'2026-10-05T16:00:00+03:00',end:'2026-10-05T20:00:00+03:00',required:1,available_upper_bound:bound});
  const result=(proposed)=>({checks:{baseline:{diagnostics:[short(1)],validation:{findings:[{severity:'error',message:'Clinic A is short'}]}},
                              proposed:{diagnostics:proposed,validation:{findings:[{severity:'error',message:'Clinic A is short'}]}}}});
  const n=(r)=>{const d=findingsDiff(r,()=>'','Asia/Riyadh');return [d.added.length,d.existing.length,d.added.map(f=>f.worse)];};
  return {same:n(result([short(1)])),worse:n(result([short(0)])),added:n(result([short(1),short(1,'Night cover')]))};}"""


def test_rule_review_tells_new_problems_from_existing_ones(browser, live_server, h):
    """Unchanged shortages and errors are "existing"; a new one, or the same with fewer people able to work it, is new."""
    context, page = sign_in(browser, live_server, 'approver', {'width': 1440, 'height': 900})
    try:
        got = page.evaluate(FINDINGS)
        assert got['same'] == [0, 2, []]
        assert got['worse'] == [1, 1, [True]]
        assert got['added'] == [1, 2, [False]]
    finally:
        context.close()


def test_rule_review_answers_per_rota_with_existing_problems_on_demand(browser, live_server, h):
    """A rota whose problems were all there before the rule: "No new problems from this rule · N existing problems
    unchanged" (green, with an icon), its findings behind "View existing", and the override worded for that case.
    A catalogue proposal is decided in its docked panel (B5, f1d80dcc4: effect counts from /api/policies/{id}/review);
    this per-rota review is the one a proposal outside the catalogue opens, from the Rules page's Other pending rules."""
    flag(h)
    s = h.seed
    w = h.workspace('Review fixture', '2026-10-04', '2026-10-11')
    title = 'Review rest proposal'
    proposal = h.command('coordinator', 'policy.create', {'name': title, 'owner': 'Synthetic department',
        'source': 'Existing problems review', 'starts_on': '2026-10-01',
        'definition': {'rule': {'kind': 'min_rest', 'hours': 12}, 'workspace_id': w['id']}})['record']
    short = {'requirement_name': 'Evening cover', 'service_ids': [s['services'][0]], 'start': '2026-10-05T16:00:00+03:00', 'end': '2026-10-05T20:00:00+03:00',
             'required': 1, 'available_upper_bound': 0}
    other = {**short, 'start': '2026-10-06T16:00:00+03:00', 'end': '2026-10-06T20:00:00+03:00'}
    job = {'id': 'job-1', 'kind': 'feasibility', 'status': 'succeeded', 'checked_rules': [{'policy_id': proposal['id']}], 'created_at': '2026-10-03T08:00:00Z',
           'result': {'status': 'requirements_conflict', 'pre_existing_conflict': True, 'full_feasibility_established': False, 'newly_introduced_contradiction': False,
                      'checks': {'baseline': {'status': 'requirements_conflict', 'diagnostics': [short, other]},
                                 'proposed': {'status': 'requirements_conflict', 'diagnostics': [short, other]}}}}
    context, page = sign_in(browser, live_server, 'approver', {'width': 1440, 'height': 900})
    try:
        page.route(re.compile(r'/api/jobs\?workspace_id='), lambda r: r.fulfill(status=200, content_type='application/json', body=json.dumps({'jobs': [job]})))
        page.route(re.compile(r'/api/jobs/job-1$'), lambda r: r.fulfill(status=200, content_type='application/json', body=json.dumps({'job': job})))
        navigate(page, 'rules')
        row_menu(page, pending(page, 1), title).get_by_role('menuitem', name='Review', exact=True).click()
        review = page.get_by_role('dialog', name='Review ' + title, exact=True)
        result = review.locator('.review-result')
        expect(result).to_have_text('No new problems from this rule · 2 existing problems unchanged')
        expect(result).to_have_class(re.compile(r'\bok\b'))
        expect(review.get_by_text('Activate with the existing problems unchanged (they are not from this rule).', exact=True)).to_be_visible()
        expect(review).not_to_contain_text('Existing conflicts remain')
        shot(page, 'live-review-existing-1440-light', full_page=False)
        review.get_by_role('button', name='View existing (2)', exact=True).click()
        details = page.get_by_role('dialog', name=w['name'])
        expect(details.locator('.review-finding')).to_have_count(2)
        expect(details).not_to_contain_text('Current rules')
        shot(page, 'live-review-findings-1440-light', full_page=False)
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_read_only_roles_read_filters_as_facts(browser, live_server, h, scheme):
    """An auditor reads Who as "Rank is any of [chips] · each rank counted separately" (the Who heading leads: no "Where",
    F-R4), never disabled pickers."""
    flag(h)
    s = h.seed
    rule = catalogue_rule(h, 'kind_per_session', [line({'min': {'value': 1, 'level': 'mandatory'}},
        who={'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [s['category'], s['trainee_category']], 'count': 'each'}]})])
    context, page = sign_in(browser, live_server, 'auditor', {'width': 1440, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        panel = open_line(page, 'kind_per_session', rule['definition']['rule']['lines'][0]['id'])
        who, _ = sides(panel)
        fact = who.locator('.cond-fact')
        expect(fact).to_have_count(1)
        expect(fact.locator('.cond-fact-join')).to_have_text('')
        expect(fact).to_have_text(re.compile(r'^\s*Rank is any of'))
        expect(fact).to_contain_text('each rank counted separately')
        expect(fact.locator('.rp')).to_have_count(2)
        expect(panel.locator('select, .cond-line, .cond-count')).to_have_count(0)
        shot(page, f'live-readonly-facts-1440-{scheme}', full_page=False)
    finally:
        context.close()

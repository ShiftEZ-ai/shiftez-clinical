"""Rules in context on the planner: hover summaries, the pinned rules panel, the three ways to change a rule,
exception markers, the rule highlight (desktop and phone) and a department-sized rule map."""
import json
import re

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, pick_view, open_rota, planner_tool, press_escape, reconnect, wait_held  # noqa: F401
from .test_rule_templates import propose, activate
from .rules_support import rule_action, rule_row

pytestmark = pytest.mark.browser
MON = ('2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')
TUE = ('2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00')
_UNEXPLAINED = "Can\u2019t explain this rule right now. Its check still applies."


def rota(h):
    """Staff 01 in the Continuity clinic on Monday and Tuesday, Staff 02 in the Review clinic on Monday. Rules: a department
    duty limit with Staff 02's own number (a template line), minimum rest (a safety rule) and a Review-clinic hours limit."""
    a, b, _ = h.seed['people']
    duties = activate(h, propose(h, {'kind': 'max_duties', 'maximum': 5, 'window': 'week'}, name='Weekly duty limit', per_person=[{'person_id': b, 'amount': 3}]))
    rest = activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest between duties'))
    clinic = activate(h, propose(h, {'kind': 'max_hours', 'hours': 40, 'window': 'week'}, {'service_ids': [h.seed['services'][1]]}, name='Review clinic hours'))
    w = h.workspace('Rules in context')
    w = h.shift(w, *MON, people=[a])['workspace']
    w = h.shift(w, *TUE, people=[a])['workspace']
    w = h.shift(w, *MON, people=[b], service=1, location=1)['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    return w, {'duties': duties, 'rest': rest, 'clinic': clinic}


def open_planner(page, w):
    navigate(page, 'planner')
    open_rota(page, w['id'])
    expect(page.locator('.wk-day[data-rk]').first).to_be_visible()
    # The planner has tagged its places (the rule map itself is read on the first hover or focus).
    page.wait_for_function("() => document.querySelectorAll('[data-rk]').length > 0")
    # A draft checks itself on opening; let that settle so the planner is not redrawn mid-step.
    expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)


def test_hover_pin_highlight_and_exception_markers(browser, live_server, h):
    w, rules = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator'); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        open_planner(page, w)
        monday = page.locator('.wk-day').nth(1)
        # Hover (after a short delay): a summary of the rules that apply on that day.
        monday.hover()
        card = page.locator('.hovercard')
        expect(card).to_contain_text('rules apply')
        expect(card).to_contain_text('Rest between duties')
        # Keyboard: focus shows the card, Enter pins the panel; the grid stays usable beside it.
        monday.focus(); page.keyboard.press('Enter')
        panel = page.get_by_role('dialog', name=re.compile('Mon'))
        expect(panel).to_be_visible()
        assert panel.get_attribute('aria-modal') == 'false'
        expect(rule_row(panel, 'Weekly duty limit')).to_be_visible()
        expect(page.locator('.wk-grid')).to_be_visible()
        page.keyboard.press('Escape')
        expect(panel).to_be_hidden()
        # Staff 02 has their own number in the duty limit: a marker with words, not colour alone.
        mark = page.locator('.pchip').filter(has_text='Staff 02').locator('.rk-mark')
        expect(mark).to_have_attribute('aria-label', re.compile('Own number'))
        # Staff 02's session panel: which rules count here, and their own number wins over the rule's 5.
        page.locator('.pchip').filter(has_text='Staff 02').click()
        person = page.get_by_role('dialog').filter(has_text='Rules applied here')
        row = rule_row(person, 'Weekly duty limit')
        # The row is the rule's sentence (Why panel v2); the person's own number is said in the opened row.
        row.locator('.rk-row-head').click()
        expect(row.locator('.rk-detail')).to_contain_text(re.compile('Own number.*3 duties instead of 5 duties'))
        expect(rule_row(person, 'Review clinic hours')).to_be_visible()
        person.get_by_role('button', name='Close panel').click()
        # The rules pill lists every rule in the rota; hovering one previews its highlight, opening it locks it.
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        clinic = rule_row(panel, 'Review clinic hours')
        clinic.hover()
        expect(page.locator('.rk-spot')).to_contain_text('Review clinic hours')
        expect(page.locator('.planner-main')).to_have_class(re.compile('rk-dim'))
        clinic.locator('.rk-row-head').click()
        spot = page.locator('.rk-spot.locked')
        expect(spot).to_contain_text(re.compile(r'applies to \d+ (person|people) · 1 session · 1 day'))
        page.wait_for_timeout(200)  # the bar's pop-in animation
        assert spot.bounding_box()['x'] + spot.bounding_box()['width'] <= page.locator('.rk-panel').bounding_box()['x']  # not under the panel
        expect(page.locator('.pchip.rk-hit')).to_have_count(1)
        expect(page.locator('.pchip.rk-hit')).to_contain_text('Staff 02')
        # DL-069 keeps dimmed content opaque and moves it to the neutral muted token so the hue is not the only cue.
        muted = page.evaluate("""() => { const probe = document.createElement('span'); probe.style.color = 'var(--muted)'; document.body.append(probe); const value = getComputedStyle(probe).color; probe.remove(); return value; }""")
        dimmed_chip = page.locator('.pchip:not(.rk-hit)[data-rk]').first
        assert dimmed_chip.evaluate('n => getComputedStyle(n).opacity') == '1'
        assert dimmed_chip.evaluate('n => getComputedStyle(n).color') == muted
        page.locator('.wk-grid').click(position={'x': 5, 'y': 5})
        page.keyboard.press('Escape')
        expect(page.locator('.rk-spot')).to_have_count(0)
        # The same highlight works in the people and coverage views.
        clinic.locator('.rk-row-head').click()  # closes it
        clinic.locator('.rk-row-head').click()  # opens and locks again
        pick_view(page, 'People')
        expect(page.locator('.pg-name.rk-hit')).to_contain_text('Staff 02')
        pick_view(page, 'Coverage')
        expect(page.locator('.cov-cell.rk-hit')).to_have_count(1)
        # List: the rows are sessions; only Staff 02's is lit, the others dim.
        pick_view(page, 'List')
        page.locator('.planner-toolbar').hover()  # Leave any rule preview revealed beneath View settings.
        expect(page.locator('.rk-spot.locked')).to_contain_text('Review clinic hours')
        expect(page.locator('tr.rk-hit')).to_have_count(1)
        expect(page.locator('tr.rk-hit')).to_contain_text('Staff 02')
        dimmed_row = page.locator('tr[data-rk]:not(.rk-hit)').first
        assert dimmed_row.evaluate('n => getComputedStyle(n).opacity') == '1'
        assert dimmed_row.evaluate('n => getComputedStyle(n).color') == muted
        assert not errors
    finally:
        context.close()


def test_rules_panel_has_no_views_or_switches(browser, live_server, h):
    """Why panel v2 (slice 4): the list orders itself (what needs fixing, what changed, the rest folded), so the panel
    has no views picker, no List | Explained and no This selection | All rules; only Search, when the list is long."""
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        expect(panel.locator('.rk-fold')).to_be_visible()
        expect(panel.locator('.dt-saved')).to_have_count(0)
        expect(panel.get_by_role('radiogroup')).to_have_count(0)
        expect(panel.get_by_role('searchbox')).to_have_count(0)  # three rules: nothing to search
    finally:
        context.close()


def test_an_open_hover_card_fills_in_when_the_rule_map_arrives(browser, live_server, h):
    """The rule map is read on the first hover; a card opened before it arrived shows "Loading…" and then the rules,
    without hovering again (the merge-queue flake of test_hover_pin_highlight_and_exception_markers)."""
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    try:
        page.route(re.compile(r'.*/rule-map$'), lambda route: held.append(route))
        open_planner(page, w)
        page.locator('.wk-day').nth(1).hover()
        card = page.locator('.hovercard')
        expect(card).to_contain_text('Loading the rules that apply')
        assert held, 'the rule map was not held'
        held.pop().continue_()
        expect(card).to_contain_text('rules apply')
        expect(card).to_contain_text('Rest between duties')
        expect(card).not_to_contain_text('Loading the rules')
    finally:
        context.close()


def test_three_ways_to_change_a_rule(browser, live_server, h):
    w, rules = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator'); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        # 1. This rota only: immediate, with Undo.
        rule_action(rule_row(panel, 'Weekly duty limit'), 'Adjust for this rota')
        panel.get_by_label('New number for “Weekly duty limit” in this rota').fill('4')
        panel.get_by_label('Justification', exact=True).fill('Agreed with the department for this rota')
        panel.get_by_role('button', name='Use for this rota').click()
        toast = page.locator('.toast').filter(has_text='changed for this rota')
        expect(toast).to_be_visible()
        page.wait_for_timeout(1500)  # a second, unclaimed "Saved" message would appear by now
        assert page.locator('.toast').all_inner_texts() == [toast.inner_text()], page.locator('.toast').all_inner_texts()
        assert h.detail(w)['workspace']['rule_adjustments'][0]['amount'] == 4
        toast.get_by_role('button', name='Undo').click()
        expect(page.locator('.toast').filter(has_text='Undone')).to_be_visible()
        assert h.detail(w)['workspace']['rule_adjustments'] == []
        # 2. Exceptions: never offered for a safety rule (its lock note says why); proposed for a clinic otherwise.
        page.locator('.wk-group-head').filter(has_text='Continuity clinic').hover()
        page.get_by_role('button', name='Rules for Continuity clinic').click()
        panel = page.get_by_role('dialog', name='Continuity clinic')
        rest = rule_row(panel, 'Rest between duties')
        rest.locator('.rk-row-head').click()
        rest.locator('.rk-more').click()
        menu = page.get_by_role('menu', name='More actions')
        expect(menu.get_by_role('menuitem', name=re.compile('^Make stricter for this rota'))).to_be_visible()
        expect(menu.get_by_role('menuitem', name=re.compile('^Make an exception'))).to_have_count(0)
        rule_row(panel, 'Rest between duties').locator('.rk-row-head').click()
        duty = rule_row(panel, 'Weekly duty limit')
        rule_action(duty, 'Make an exception for this clinic')
        form = panel.get_by_role('form', name='Make an exception for this clinic')
        form.get_by_label('Number for this clinic').fill('6')
        form.get_by_label('Reason').fill('Clinic runs extra lists this term')
        form.get_by_role('button', name='Send for approval').click()
        expect(page.locator('.toast').filter(has_text='Exception proposed')).to_be_visible()
        proposed = [p for p in h.get('coordinator', '/api/policies?status=proposed')['policies'] if rules['duties']['id'] in p['definition']['overrides']]
        assert len(proposed) == 1 and proposed[0]['definition']['rule']['maximum'] == 6
        assert proposed[0]['definition']['scope']['conditions']['conditions'][-1] == {'field': 'service_ids', 'op': 'any', 'values': [h.seed['services'][0]]}
        # 3. For everyone: the usual rule form, proposed for approval.
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        # The proposal above refreshes the page; its toast leaving means the panel has been redrawn (a redraw closes an
        # open menu).
        expect(page.locator('.toast').filter(has_text='Exception proposed')).to_have_count(0, timeout=20000)
        rule_action(rule_row(panel, 'Rest between duties'), 'Make department default')  # under ⋯ (Why panel v2)
        expect(page.get_by_role('dialog', name='Change Rest between duties')).to_be_visible()
        assert not errors
    finally:
        context.close()


def test_a_date_problem_stays_in_its_own_cell_of_the_exception_form(browser, live_server, h):
    """A wrapped date (From, Until in one row of .rk-dates) shows its problem inside its own label: the two dates stay
    side by side, the message is the field's description and its name stays its label (dom.ts namingLabel)."""
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        page.locator('.wk-group-head').filter(has_text='Continuity clinic').hover()
        page.get_by_role('button', name='Rules for Continuity clinic').click()
        panel = page.get_by_role('dialog', name='Continuity clinic')
        duty = rule_row(panel, 'Weekly duty limit')
        rule_action(duty, 'Make an exception for this clinic')
        form = panel.get_by_role('form', name='Make an exception for this clinic')
        dates = form.locator('.rk-dates input.date-text')
        start, until = dates.nth(0), dates.nth(1)
        # One read in one script turn: the panel may still shift by a pixel as it settles (focus, restored scroll).
        same_row = lambda: dates.evaluate_all('d => Math.abs(d[0].getBoundingClientRect().top - d[1].getBoundingClientRect().top) < 1')
        expect(until).to_be_visible()
        assert same_row()
        # Named by its label's words only (not also by the calendar button), before and after the problem.
        expect(start).to_have_accessible_name('From')
        expect(until).to_have_accessible_name('Until (included)')
        width = start.bounding_box()['width']
        start.fill('not a date')
        start.press('Tab')
        form.get_by_label('Reason').fill('Clinic runs extra lists this term')
        form.get_by_role('button', name='Send for approval').click()
        expect(start).to_have_attribute('aria-invalid', 'true')
        expect(start).to_have_accessible_description(re.compile(r'^Enter a date'))
        expect(start).to_have_accessible_name('From')
        expect(until).to_have_accessible_name('Until (included)')
        message = form.locator('.rk-dates .field-error:visible')
        expect(message).to_have_count(1)
        expect(start).to_be_visible()
        expect(until).to_be_visible()
        assert same_row()  # Until is not pushed to the next row
        box, note = start.bounding_box(), message.bounding_box()
        assert note['y'] >= box['y'] + box['height'] - 1, (box, note)  # the message is under the date, not beside it
        assert abs(box['width'] - width) <= 1, (width, box)  # and the date keeps its width
        assert form.locator('.rk-dates > .field-error').count() == 0  # never a grid cell of its own
    finally:
        context.close()


def test_approver_sees_rules_but_cannot_change_them(browser, live_server, h):
    w, _ = rota(h)
    h.lifecycle(w, 'submit')
    context, page = sign_in(browser, live_server, 'approver')
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        row = rule_row(panel, 'Weekly duty limit')
        row.locator('.rk-row-head').click()
        # A reader of a submitted rota opens the rule and nothing else (Why panel v2): no ⋯ menu, no way to adjust it,
        # make an exception or change the department rule.
        expect(row.get_by_role('button', name='Open rule', exact=True)).to_be_visible()
        expect(row.locator('.rk-more')).to_have_count(0)
        change = re.compile('^(Adjust for this rota|Make stricter for this rota|Make an exception|Make department default|Change the rule for everyone)')
        for role in ('button', 'menuitem'):  # the change actions are menu items under ⋯; none as a button either
            expect(page.get_by_role(role, name=change)).to_have_count(0)
    finally:
        context.close()
    # The coordinator of a submitted rota: rota-only changes and rota-only rules both wait for a draft.
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        expect(panel.get_by_role('button', name='Workload targets for this rota', exact=True)).to_be_visible()
        expect(panel.get_by_role('button', name='Add a rule for this rota')).to_have_count(0)
    finally:
        context.close()


def test_rules_page_show_on_planner(browser, live_server, h):
    w, rules = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        navigate(page, 'rules')
        page.locator('.rule-item').filter(has_text='Review clinic hours').get_by_role('button', name='Show on the schedule').click()
        expect(page.locator('.rk-spot.locked')).to_contain_text('Review clinic hours')
        expect(page.get_by_role('dialog', name='Rules in this rota')).to_be_visible()
    finally:
        context.close()


def test_phone_tap_pins_and_highlights(browser, live_server, h):
    w, _ = rota(h)
    # Touch and mouse both open the selected day in the shared rules panel.
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 390, 'height': 844}, has_touch=True)
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        day = page.locator('.ag-head[data-rk]').filter(has_text=re.compile(r'^Mon '))
        day.tap()
        panel = page.get_by_role('dialog', name=re.compile('Mon'))
        expect(panel).to_be_visible()
        box = panel.bounding_box()
        assert box['width'] <= 390 and box['y'] > 200  # a bottom sheet, the day list still visible above it
        rule_row(panel, 'Rest between duties').locator('.rk-row-head').click()
        expect(page.locator('.rk-spot.locked')).to_contain_text('Rest between duties')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.keyboard.press('Escape')
        # The whole rota's rules are in Rota tools on a phone too.
        planner_tool(page, 'Rules in this rota')
        expect(page.get_by_role('dialog', name='Rules in this rota')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        # A7: the footer's first health pill carries the rota's state (and its score once nothing must be fixed).
        pills = page.locator('.app-footer .health-pills')
        score, box = pills.locator('.health-pill').first.bounding_box(), pills.bounding_box()
        assert score['x'] + score['width'] <= box['x'] + box['width'] + 1  # the pill is not cut off beside the others
        assert box['x'] + box['width'] <= 391
    finally:
        context.close()


def synthetic_rules(n, people, shift_ids, state=lambda i: 'met'):
    """A rule map's rules, as /rule-map serves them, for sizes the seeded department cannot reach cheaply."""
    return [{'id': f'r{i}', 'name': f'Synthetic rule {i:03}', 'kind': 'max_hours', 'severity': 'mandatory', 'safety': False, 'exception_allowed': False,
             'adjustable': True, 'overridable': True, 'number': {'field': 'hours', 'value': 40 + i % 9, 'unit': 'hours', 'stricter': 'lower'}, 'department_value': 40,
             'adjusted': None, 'off': False, 'workspace_only': False, 'overrides': [], 'overridden_by': [], 'person_side': True, 'session_side': i % 7 == 0,
             'named': {'people': [people[i % len(people)]], 'services': [], 'locations': [], 'types': [], 'weekdays': None, 'dated': False},
             'sessions': list(range(len(shift_ids))) if i % 3 == 0 else [], 'people': {p: [[0, 7]] for p in people[i % 5::5]}, 'replaced': {},
             'days': list(range(7)), 'lines': [], 'status': {'state': state(i), 'errors': int(state(i) == 'breach'), 'warnings': 0}} for i in range(n)]


@pytest.mark.parametrize('variant', ['met', 'breach', 'replaced'], ids=['all-met', 'breach-first', 'replaced-fold'])
def test_schedule_rules_panel_orders_unexplained_rules(browser, live_server, h, variant):
    """DL-037: unexplained rows stay above the met fold, with safety first; replaced rows return to that fold."""
    w, existing = rota(h)
    person = h.seed['people'][0]
    if variant == 'breach':
        # The seeded safety rule supplies a real finding for B, while the browser receives the four-rule map below.
        w = h.shift(w, '2026-10-05T20:00:00+03:00', '2026-10-05T23:30:00+03:00', people=[person])['workspace']
        w = h.lifecycle(w, 'validate')['workspace']

    shift_ids = [s['id'] for s in h.detail(w)['shifts']]
    names = ['B', 'S', 'N', 'M1', 'M2'] if variant == 'breach' else ['S', 'N', 'M1', 'M2']
    mapped = synthetic_rules(len(names), h.seed['people'], shift_ids)
    for rule, name in zip(mapped, names):
        rule['id'] = existing['rest']['id'] if name == 'B' else name
        rule['name'] = name
        rule['safety'] = name == 'S'
        rule['named']['people'] = [person]
        rule['sessions'] = list(range(len(shift_ids)))
        rule['people'] = {person: [[0, 7]]}
        rule['days'] = list(range(7))
        rule['status'] = {'state': 'met', 'errors': 0, 'warnings': 0}
    if variant == 'breach':
        mapped[0]['status'] = {'state': 'breach', 'errors': 1, 'warnings': 0}
    if variant == 'replaced':
        replaced = mapped[0]
        replaced['named']['people'] = []
        replaced['sessions'] = []
        replaced['people'] = {}
        replaced['replaced'] = {person: [[0, 7]]}
        replaced['overridden_by'] = ['N']

    map_payload = {'days': [f'2026-10-{day:02}' for day in range(4, 11)], 'shifts': shift_ids,
                   'checked': True, 'fresh': True, 'rules': mapped, 'markers': []}
    explained_payload = {
        'workspace_id': w['id'],
        'rules': [{'rule_id': rule['id'], 'title': rule['name'], 'question_group': 'D Workload per person',
                   'level': 'mandatory', 'safety': rule['safety'], 'sentences': [],
                   'unexplained': rule['name'] in {'S', 'N'}, 'summary_sentence': f'{rule["name"]} explained.',
                   'summary_short': f'{rule["name"]}.', 'line_ids': []} for rule in mapped],
        'conflicts': [],
    }
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/api/workspaces/[^/]+/rule-map$'), lambda route: route.fulfill(json=map_payload))
        page.route(re.compile(r'.*/api/workspaces/[^/]+/rules-explained$'),
                   lambda route: route.fulfill(json=explained_payload))
        open_planner(page, w)
        pick_view(page, 'People')
        person_row = page.locator(f'.pg-row[data-row-person="{person}"]')
        expect(person_row).to_be_visible()
        person_row.locator('.pg-name').click()
        panel = page.locator('.rk-panel')
        rules_tab = panel.get_by_role('tab', name=re.compile(r'^Rules'))
        expect(rules_tab).to_be_visible()
        rules_tab.click()

        fold_words = '3 more: 2 met, 1 decided by a more specific rule' if variant == 'replaced' else '2 more are met'
        expect(panel.get_by_role('button', name=fold_words, exact=True)).to_be_visible()
        top = panel.locator('.rk-list > ul.rk-rules').first
        expected_top = {'met': ['S', 'N'], 'breach': ['B', 'S', 'N'], 'replaced': ['N']}[variant]
        assert top.locator('.rk-row').evaluate_all("rows => rows.map(row => row.dataset.rkName)") == expected_top
        for name in ({'S', 'N'} if variant != 'replaced' else {'N'}):
            row = top.locator(f'.rk-row[data-rk-name="{name}"]')
            expect(row.locator('.rule-unexplained')).to_have_text(_UNEXPLAINED)

        panel.get_by_role('button', name=fold_words, exact=True).click()
        fold = panel.locator('#rk-fold-sel')
        expect(fold).to_be_visible()
        folded = set(fold.locator('.rk-row').evaluate_all("rows => rows.map(row => row.dataset.rkName)"))
        assert folded == ({'M1', 'M2'} if variant != 'replaced' else {'S', 'M1', 'M2'})
        expected_unexplained = '1 rule can\u2019t be explained right now' if variant == 'replaced' else '2 rules can\u2019t be explained right now'
        expect(panel.locator('.rk-head small')).to_contain_text(expected_unexplained)
    finally:
        context.close()


def test_show_all_opens_only_its_own_section(browser, live_server, h):
    w, _ = rota(h)
    shift_ids = [s['id'] for s in h.detail(w)['shifts']]
    rules = synthetic_rules(120, h.seed['people'], shift_ids, lambda i: 'breach' if i % 2 else 'met')
    payload = json.dumps({'days': [f'2026-10-{d:02}' for d in range(4, 11)], 'shifts': shift_ids, 'checked': True, 'fresh': True, 'rules': rules, 'markers': []})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/rule-map$'), lambda route: route.fulfill(status=200, content_type='application/json', body=payload))
        page.reload()
        expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible()
        open_planner(page, w)
        # The rule map loads on first use (hover, focus, pin or highlight): hover a day to ask for it.
        page.locator('.wk-day[data-rk]').first.hover()
        page.wait_for_function("() => document.querySelector('.planner-main')?.dataset.rkRules === '120'", timeout=60000)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        # The 60 broken rules first (40 drawn, then Show all); the 60 met fold under one toggle, cut the same way.
        expect(panel.get_by_role('button', name='Show all 60')).to_have_count(1)
        panel.get_by_role('button', name='60 more are met').click()
        expect(panel.get_by_role('button', name='Show all 60')).to_have_count(2)
        panel.get_by_role('button', name='Show all 60').first.click()
        expect(panel.get_by_role('button', name='Show all 60')).to_have_count(1)
        expect(panel.locator('.rk-row')).to_have_count(60 + 40)
    finally:
        context.close()


@pytest.fixture
def department_sized_rule_map(browser, live_server, h):
    """974 rules and 142 people (the real department's size) served as the rule map: the planner decorates, hovers,
    pins and highlights without stalling."""
    w, _ = rota(h)
    shift_ids = [s['id'] for s in h.detail(w)['shifts']]
    people = [f'p{i}' for i in range(142)] + h.seed['people']
    rules = synthetic_rules(974, people, shift_ids)
    markers = [{'kind': 'template_line', 'rule_id': f'r{i}', 'rule_name': f'Synthetic rule {i:03}', 'text': 'Own number', 'by': 'Approver', 'at': None,
                'people': [people[i % len(people)]], 'services': [], 'locations': [], 'types': [], 'days': None, 'sessions': [], 'global': False} for i in range(300)]
    days = [f'2026-10-{d:02}' for d in range(4, 11)]
    payload = json.dumps({'days': days, 'shifts': shift_ids, 'checked': True, 'fresh': True, 'rules': rules, 'markers': markers})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        # Signing in can land on the planner and load the real map before a route exists; route, then start afresh.
        page.route(re.compile(r'.*/rule-map$'), lambda route: route.fulfill(status=200, content_type='application/json', body=payload))
        page.reload()
        expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible()
        open_planner(page, w)
        # Ready: the big map (not the rota's own three rules) is drawn onto the grid.
        # The rule map loads on first use (hover, focus, pin or highlight): hover a day to ask for it.
        page.locator('.wk-day[data-rk]').first.hover()
        page.wait_for_function("() => document.querySelector('.planner-main')?.dataset.rkRules === '974'", timeout=60000)
        # A redraw keeps the old grid on screen until the new one is ready: wait until no redraw is in flight.
        expect(page.locator('#main[data-loaded-view=schedule]')).to_have_count(1)
        # Timed in the page (main-thread work only), so a busy CI machine does not turn test overhead into a failure.
        page.get_by_role('button', name='Rota tools', exact=True).click()  # the menu opens outside the timing
        timings = page.evaluate("""async () => {
            const frame = () => new Promise(r => requestAnimationFrame(() => r()));
            let t = performance.now();
            [...document.querySelectorAll('[role=menuitem]')].find(b => b.textContent.includes('Rules in this rota')).click();
            const rows = document.querySelectorAll('.rk-panel .rk-row').length, sub = document.querySelector('.rk-head small').textContent;
            const pin = performance.now() - t;
            await frame();
            const find = document.querySelector('.rk-panel input[type=search]');
            find.value = 'Synthetic rule 333'; find.dispatchEvent(new Event('input'));
            await frame();
            t = performance.now();
            document.querySelector('.rk-panel [data-rk-row="r333"] .rk-row-head').click();
            const spot = document.querySelector('.rk-spot.locked')?.textContent || '';
            const highlight = performance.now() - t;
            return {rows, sub, pin, spot, highlight, dim: document.querySelector('.planner-main').classList.contains('rk-dim')};
        }""")
        print(f"974-rule map in the page: pin {timings['pin']:.0f} ms, highlight {timings['highlight']:.0f} ms")
        yield page, timings
    finally:
        context.close()


def test_department_sized_rule_map_stays_fast_in_the_browser(department_sized_rule_map):
    page, timings = department_sized_rule_map
    # The first rules of each section are drawn at once; the rest on "Show all" or by searching.
    assert '974 rules' in timings['sub'] and 0 < timings['rows'] <= 45 and 'Synthetic rule 333' in timings['spot'] and timings['dim']
    page.keyboard.press('Escape')
    page.locator('.wk-day').nth(1).hover()
    expect(page.locator('.hovercard')).to_contain_text('rules apply')


@pytest.mark.wall_benchmark
def test_department_sized_rule_map_cpu_budget(department_sized_rule_map):
    _, timings = department_sized_rule_map
    # Budgets far above a developer machine (tens of milliseconds) so slow shared runners still pass.
    assert timings['pin'] < 2500 and timings['highlight'] < 1500


def test_a_goal_warns_on_the_assignment_when_someone_is_outside_the_band(browser, live_server, h):
    from .test_fairness import goal
    a, b, _ = h.seed['people']
    g = goal(h, period='rota', scope={'person_ids': [a, b]})
    w = h.workspace('Uneven week')
    for day in (5, 6, 7, 8):
        w = h.shift(w, f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00', people=[a])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        page.locator('.pchip[data-rk]').first.click()
        panel = page.locator('.wk-panel')
        mine = rule_row(panel.get_by_role('list', name='Rules applied to this assignment'), g['name'])
        expect(mine).to_contain_text('Could be better')  # the row says its status in words (Why panel v2), not a count
        panel.get_by_role('button', name='Close panel').click()
        planner_tool(page, 'Rules in this rota')
        rows = rule_row(page.get_by_role('dialog', name='Rules in this rota'), g['name'])
        expect(rows).to_contain_text('Could be better')
    finally:
        context.close()


def test_clinic_skills_and_leave_buffers_are_exact_on_the_planner(browser, live_server, h):
    """A skill held at the Review clinic only: Staff 01's hover card there lists its rule, their Continuity session's does not
    (as its "Rules applied here" panel says). A leave buffer lights only the session worked next to the leave."""
    from datetime import date
    from apps.api import models as m
    from apps.api.db import transaction
    from .test_browser import reconnect, wait_held
    a, b, _ = h.seed['people']
    review = h.seed['services'][1]
    with transaction(h.app.state.engine, write=True) as db:
        skill = m.Capability(department_id=h.seed['department_id'], name='Review clinic sign-off', description='Synthetic',
                             starts_on=date(2020, 1, 1))
        db.add(skill)
        db.flush()
        db.add(m.CapabilityGrant(person_id=a, capability_id=skill.id, department_id=h.seed['department_id'], service_id=review,
                                 granted_by=h.seed['identities']['admin'], evidence='Synthetic', starts_on=date(2020, 1, 1)))
        skill_id = skill.id
    activate(h, propose(h, {'kind': 'max_hours', 'hours': 30, 'window': 'week'},
                        {'conditions': {'match': 'all', 'conditions': [{'field': 'capability_ids', 'op': 'any', 'values': [skill_id]}]}},
                        name='Sign-off hours'))
    activate(h, propose(h, {'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00', 'leave_buffer_days': 1},
                        name='Rest next to leave'))
    h.command('coordinator', 'availability.record', {'person_id': a, 'kind': 'recorded_leave', 'start': '2026-10-07T00:00:00+03:00',
                                                     'end': '2026-10-08T00:00:00+03:00', 'private_reason': 'Synthetic'})
    w = h.workspace('Exact rules')
    w = h.shift(w, *MON, people=[a])['workspace']                                                                  # Continuity clinic
    w = h.shift(w, '2026-10-05T13:00:00+03:00', '2026-10-05T17:00:00+03:00', people=[a], service=1)['workspace']   # Review clinic
    w = h.shift(w, *TUE, people=[a, b], seed_restricted=True)['workspace']                                                               # the day before the leave
    shifts = {s['id']: s for s in h.detail(w)['shifts']}
    context, page = sign_in(browser, live_server, 'coordinator'); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        open_planner(page, w)
        # The people view's chips read the rule map: the Review session counts one rule more than the Continuity one.
        pick_view(page, 'People')
        chips = page.locator('.pg-chip[data-rk]')
        expect(chips).to_have_count(4)
        targets = [json.loads(chips.nth(i).get_attribute('data-rk')) for i in range(4)]
        chip = lambda test: chips.nth(next(i for i, t in enumerate(targets) if t['person'] == a and test(shifts[t['shift']])))

        def rules_on(node):
            node.hover()
            head = page.locator('.hovercard .rk-hc-head strong')
            expect(head).to_have_text(re.compile(r'^(\d+|No) rules? appl'))
            count = 0 if head.inner_text().startswith('No') else int(head.inner_text().split()[0])
            page.mouse.move(0, 0)
            expect(page.locator('.hovercard')).to_have_count(0)
            return count
        review_session = chip(lambda s: review in s['service_ids'])
        # The rota is checked again under a resting pointer: the planner redraws its chips once it has read the checked
        # rota, and the card comes back on the new chip for the same person and session, reading the rule map again,
        # without the mouse moving.
        review_session.hover()
        expect(page.locator('.hovercard .rk-hc-head strong')).to_have_text(re.compile(r'^(\d+|No) rules? appl'))
        drafts, maps = [], []
        draft = re.compile(rf"/api/workspaces/{w['id']}(\?.*)?$")
        rule_map = re.compile(rf".*/api/workspaces/{w['id']}/rule-map$")
        page.route(draft, lambda route: drafts.append(route))
        page.route(rule_map, lambda route: maps.append(route))
        now = h.detail(w)['workspace']
        checked = now['validation']['evaluated_at']
        h.lifecycle(now, 'validate')
        assert h.detail(w)['workspace']['validation']['evaluated_at'] != checked
        reconnect(page)
        wait_held(page, drafts)
        review_session.evaluate("b => { b.dataset.drawnBefore = '' }")
        for route in drafts:
            route.continue_()
        page.unroute(draft)
        wait_held(page, maps)
        expect(page.locator('[data-drawn-before]')).to_have_count(0)
        expect(review_session).to_have_attribute('aria-describedby', re.compile(r'hover-help-'))
        for route in maps:
            route.continue_()
        page.unroute(rule_map)
        expect(page.locator('.hovercard .rk-hc-head strong')).to_have_text(re.compile(r'^(\d+|No) rules? appl'))
        page.mouse.move(0, 0)
        expect(page.locator('.hovercard')).to_have_count(0)
        assert rules_on(review_session) == rules_on(chip(lambda s: review not in s['service_ids'] and s['start'][:10] == '2026-10-05')) + 1
        pick_view(page, 'Week')
        # The leave buffer: only Staff 01's Tuesday place (the day before the leave), not Monday's, not Staff 02's.
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        rule_row(panel, 'Rest next to leave').locator('.rk-row-head').click()
        expect(page.locator('.rk-spot.locked')).to_contain_text(re.compile(r'applies to 1 person · 1 session'))
        expect(page.locator('.pchip.rk-hit')).to_have_count(1)
        lit = json.loads(page.locator('.pchip.rk-hit').get_attribute('data-rk'))
        assert lit['person'] == a and shifts[lit['shift']]['start'][:10] == '2026-10-06', lit
        assert not errors, errors
    finally:
        context.close()


def test_rota_rules_panel_offers_a_number_for_catalogue_rules(browser, live_server, h):
    """SR-22 / RU-13 / RU-14: a catalogue rule with a number gets "use N instead" for this rota in the rules panel
    (read from the rule map; the dock's own rule list is retired), with its lines, and the panel opens on facts."""
    from .test_browser_rule_catalogue import flag, catalogue_rule, line
    flag(h)
    rule = catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 6, 'level': 'mandatory'}})])
    w = h.shift(h.workspace('Catalogue numbers'), *MON)['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        expect(panel).not_to_contain_text('They are the defaults for every rota')
        expect(panel).not_to_contain_text('Add a rule that applies here and nowhere else')
        row = rule_row(panel, rule['name'])
        rule_action(row, 'Adjust for this rota')
        expect(row.get_by_label(f"New number for “{rule['name']}” in this rota")).to_have_value('6')
    finally:
        context.close()


@pytest.mark.parametrize('status', ['draft', 'submitted'])
def test_a_rules_panel_shows_the_value_a_revised_mandatory_rule_now_binds(browser, live_server, h, status):
    """A rota captured a weekly cap of 5; the department rule was then revised to 2 (approved), after submission for a
    submitted rota. The rules panel says both numbers for that rule (in the draft's number field's name too); an
    unchanged rule says one."""
    a = h.seed['people'][0]
    cap = activate(h, propose(h, {'kind': 'max_duties', 'maximum': 5, 'window': 'week'}, name='Weekly duty limit'))
    activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest between duties'))
    w = h.workspace('Revised cap')
    w = h.shift(w, *MON, people=[a])['workspace']
    w = h.shift(w, *TUE, people=[a])['workspace']
    w = h.lifecycle(w, 'validate' if status == 'draft' else 'submit')['workspace']
    activate(h, propose(h, {'kind': 'max_duties', 'maximum': 2, 'window': 'week'}, name='Weekly duty limit', replaces_id=cap['id']))
    context, page = sign_in(browser, live_server, 'coordinator' if status == 'draft' else 'approver'); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        revised = rule_row(panel, 'Weekly duty limit')
        expect(revised.locator('.rk-status')).to_contain_text(re.compile(r'Captured 5, now binds 2 dut'))
        unchanged = rule_row(panel, 'Rest between duties')
        expect(unchanged).to_be_visible()
        expect(unchanged).not_to_contain_text('now binds')
        expect(unchanged).not_to_contain_text('Captured')
        if status == 'draft':
            # The rota is checked again while a refresh is reading it, and More opens before that read answers: the
            # redrawn panel reads the rule map again before it has rows, and the open menu waits for the row's new
            # button instead of closing; its item then opens the same numeric form.
            expect(page.locator('.app-footer').get_by_text(re.compile(r'^Checking the rota'))).to_have_count(0, timeout=20000)
            head = revised.locator('.rk-row-head')
            if head.get_attribute('aria-expanded') != 'true':
                head.click()
            drafts, maps = [], []
            draft = re.compile(rf"/api/workspaces/{w['id']}(\?.*)?$")
            rule_map = re.compile(rf".*/api/workspaces/{w['id']}/rule-map$")
            page.route(draft, lambda route: drafts.append(route))
            page.route(rule_map, lambda route: maps.append(route))
            now = h.detail(w)['workspace']
            checked = now['validation']['evaluated_at']
            h.lifecycle(now, 'validate')
            assert h.detail(w)['workspace']['validation']['evaluated_at'] != checked
            reconnect(page)
            wait_held(page, drafts)
            more = revised.locator('.rk-more')
            more.evaluate("b => { b.dataset.drawnBefore = '' }")
            more.click()
            item = page.get_by_role('menu', name='More actions').get_by_role('menuitem', name=re.compile('^Adjust for this rota'))
            expect(item).to_be_visible()
            for route in drafts:
                route.continue_()
            page.unroute(draft)
            wait_held(page, maps)
            expect(panel.get_by_text('Working out which rules apply', exact=False)).to_be_visible()
            expect(item).to_be_visible()
            for route in maps:
                route.continue_()
            page.unroute(rule_map)
            expect(panel.locator('[data-drawn-before]')).to_have_count(0)
            expect(more).to_have_attribute('aria-expanded', 'true')
            item.click()
            field = revised.get_by_label(re.compile(r'^New number for .Weekly duty limit. in this rota, captured 5, now binds 2'))
            expect(field).to_have_value('5')
            expect(field.locator('..')).to_contain_text('now binds 2')  # the field's own row: the number, its unit and the binding value
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('unmet', [False, True], ids=['all-met', 'one-unmet'])
def test_click_on_a_day_head_does_not_pin_the_rules_panel(browser, live_server, h, unmet):
    """REQ-PANEL: mouse and keyboard open the selected day in the same panel, and focus moves to its first stop
    (planner_rules.ts drawPanel): the first rule row shown, else the selected tab (rules that are met are folded)."""
    w, _ = rota(h)
    if unmet:  # Staff 01 works Monday evening too: under 11 h before Tuesday morning, so Rest between duties is not met
        w = h.shift(w, '2026-10-05T20:00:00+03:00', '2026-10-05T23:30:00+03:00', people=[h.seed['people'][0]])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        monday = page.locator('.wk-day').nth(1)
        monday.locator('strong').click()  # the day's name: with an issue the head also carries its issues button
        panel = page.get_by_role('dialog', name='Monday 5 Oct', exact=True)
        expect(panel).to_be_visible()
        if unmet:
            expect(rule_row(panel, 'Rest between duties').locator('.rk-row-head')).to_be_focused()
        else:
            expect(panel.locator('.rk-row-head')).to_have_count(0)
            expect(panel.get_by_role('tab', selected=True)).to_be_focused()
        press_escape(page)
        expect(page.get_by_role('dialog', name=re.compile('Mon'))).to_have_count(0)
        monday.focus(); page.keyboard.press('Enter')
        expect(page.get_by_role('dialog', name=re.compile('Mon'))).to_be_visible()
    finally:
        context.close()


def test_rule_tag_sweep_stays_within_budget_at_department_scale(browser, live_server, h):
    """SI-50: the [data-rk] sweep over 2,000 tagged places (a 500-session week's chips and heads) is measured; it stays
    within a frame budget (CI-aware). Above it, wire the nodes lazily on first pointerover/focusin."""
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page, w)
        # The test clock fakes `performance` (its entry lists stay empty): the sweep's own measure call is recorded instead.
        page.evaluate("""() => {window.rkSweeps=[];const measure=performance.measure.bind(performance);
          performance.measure=(name,o)=>{if(name==='rk-decorate')window.rkSweeps.push(performance.now()-o.start);return measure(name,o);};
          const chip=document.querySelector('.pchip[data-rk]'),host=chip.parentElement,box=document.createElement('div');
          for(let i=0;i<2000;i++)box.append(chip.cloneNode(true));host.append(box);}""")
        page.wait_for_function("() => window.rkSweeps.length > 0")
        worst = page.evaluate("() => Math.max(...window.rkSweeps)")
        assert worst < 100, f'rk-decorate took {worst:.0f} ms for 2,000 nodes'  # 16 ms locally; CI hosts are slower
    finally:
        context.close()


def test_a_list_row_keeps_one_exception_mark_across_sweeps(browser, live_server, h):
    """A planner list row draws its exception mark in its first cell; a sweep that walks the row again, without the row
    being redrawn, replaces that mark rather than adding another beside it."""
    w, _ = rota(h)
    shift_ids = [s['id'] for s in h.detail(w)['shifts']]
    marker = {'kind': 'exception', 'rule_id': 'r0', 'rule_name': 'Synthetic rule 000', 'text': 'Differs for this session', 'by': 'Approver', 'at': None,
              'people': [], 'services': [], 'locations': [], 'types': [], 'days': None, 'sessions': [0], 'global': False}
    payload = json.dumps({'days': [f'2026-10-{d:02}' for d in range(4, 11)], 'shifts': shift_ids, 'checked': True, 'fresh': True,
                          'rules': synthetic_rules(1, h.seed['people'], shift_ids), 'markers': [marker]})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'.*/rule-map$'), lambda route: route.fulfill(status=200, content_type='application/json', body=payload))
        page.reload()
        expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible()
        open_planner(page, w)
        page.locator('.wk-day[data-rk]').first.hover()  # the rule map loads on first use
        page.wait_for_function("() => document.querySelector('.planner-main')?.dataset.rkRules === '1'", timeout=60000)
        pick_view(page, 'List')
        row = page.locator(f'tr[data-rk][data-session="{shift_ids[0]}"]')
        expect(row.locator('.rk-mark-slot > .rk-mark')).to_have_count(1)
        # Each node added to the view wakes one more sweep over every tagged place, the unchanged row included.
        page.evaluate("""() => {window.rkSweeps=0;const measure=performance.measure.bind(performance);
          performance.measure=(name,o)=>{if(name==='rk-decorate')window.rkSweeps++;return measure(name,o);};}""")
        for sweeps in (1, 2):
            page.evaluate("() => document.querySelector('.planner-main').append(document.createElement('span'))")
            page.wait_for_function(f"() => window.rkSweeps >= {sweeps}")
        expect(row.locator('.rk-mark')).to_have_count(1)
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_mouse_selections_open_the_matching_day_person_and_session(browser, live_server, h, width, scheme):
    w, _ = rota(h)
    shifts = h.detail(w)['shifts']
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        if width == 390:
            navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
            expect(page.locator('.ag-head[data-rk]').first).to_be_visible()
        else:
            open_planner(page, w)
        for day, date in (('Mon', '2026-10-05'), ('Tue', '2026-10-06')):
            page.locator(f'.wk-day[data-rk*="{date}"], .ag-head[data-rk*="{date}"]').first.click()
            panel = page.locator('.rk-panel')
            expect(panel.get_by_role('heading', level=2)).to_contain_text(day)
            # Every rule here is met and folded: focus moves to the selected tab, and a redraw (the sentences arriving) keeps it.
            expect(panel.get_by_role('tab', selected=True)).to_be_focused()
            press_escape(page)
        pick_view(page, 'People')
        for pid in h.seed['people'][:2]:
            row = page.locator(f'.pg-row[data-row-person="{pid}"]')
            row.locator('.pg-name').click()
            expect(page.locator('.rk-panel').get_by_role('heading', level=2)).to_have_text(names[pid])
            # A person opens on Details (their card); this test reads their rules, so it chooses that tab.
            rules_tab = page.locator('.rk-panel').get_by_role('tab', name=re.compile('^Rules'))
            rules_tab.click()
            expect(rules_tab).to_have_attribute('aria-selected', 'true')
            press_escape(page)
            shift = next(s for s in shifts if any(p['person_id'] == pid for p in s['participants']))
            row.locator(f'.pg-chip[data-session="{shift["id"]}"]').click()
            panel = page.locator('.wk-panel')
            expect(panel.get_by_role('heading', level=2)).to_have_text(names[pid])
            expect(panel.locator('.wk-panel-head')).to_contain_text('Tue' if shift['start'].startswith('2026-10-06') else 'Mon')
            panel.get_by_role('button', name='Whole session', exact=True).click()
            expect(panel.get_by_role('heading', level=2)).to_contain_text(shift['type_snapshot']['name'])
            for person in shift['participants']:
                expect(panel.locator('.wk-session')).to_contain_text(names[person['person_id']])
            press_escape(page)
    finally:
        context.close()

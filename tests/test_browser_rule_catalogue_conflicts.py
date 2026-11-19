"""Conflicting lines on a rule's page (docs/design/rule-catalogue.md §6): the badges, popovers and check bar say what
the server's check says, an answer never shows for lines it was not asked about, Propose stops or asks for an
acknowledgement exactly when rule.propose would refuse, and an approver sees the same on the waiting version."""
import json
import re

import pytest
from playwright.sync_api import expect

from .test_browser import add_filter, browser, live_server, navigate, sign_in, wait_held  # noqa: F401
from .test_browser_rule_catalogue import catalogue_rule, duplicate, flag, line, selection
from .test_browser_rule_filter_builder import pick
from .test_rule_catalogue_conflicts import tag_people

pytestmark = pytest.mark.browser


def crafted(h):
    """A line for everyone, the demo rank, Alex Morgan and #exceptional_trainee (naming Alex): everyone gives way to
    the three, the rank and the tag give way to Alex, and the rank and the tag both apply to Alex."""
    a = h.seed['people'][0]
    tag = tag_people(h, 'exceptional_trainee', [a])
    who = lambda field, v: {'conditions': [{'field': field, 'values': [v]}]}  # noqa: E731
    lines = [line({'max': {'value': 5}}, note='Everyone line'), line({'max': {'value': 4}}, who=who('category_ids', h.seed['category']), note='Rank line'),
             line({'max': {'value': 3}}, who=who('person_ids', a), note='Alex line'), line({'max': {'value': 6}}, who=who('person_ids', tag), note='Tag line')]
    return catalogue_rule(h, 'duties_per_week', lines), lines


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_the_page_says_what_the_check_says_and_propose_asks_what_the_server_would(browser, live_server, h, width, scheme):
    flag(h)
    rule, lines = crafted(h)
    server = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check', json={'lines': lines}).json()
    assert len(server['overlaps']) == 3 and [w['kind'] for w in server['warnings']] == ['both']
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        page.emulate_media(color_scheme=scheme)
        page.evaluate("""async () => {const me=await fetch('/api/me').then(r=>r.json());
            localStorage.setItem('rota.table.'+me.identity_id+'.'+me.department.id+'.rule-line-db',JSON.stringify({layout:'table',chosen:true}));}""")
        navigate(page, 'rules/duties_per_week')
        main = page.locator('#main')
        bar, row = main.locator('.rc-checkbar'), lambda note: main.locator('tbody tr').filter(has_text=note)  # noqa: E731
        expect(bar).to_contain_text('⚠ 1 both apply')
        expect(bar).not_to_contain_text('overlaps')
        expect(row('Everyone line').locator('.rc-overlap')).to_have_count(0)
        for label in ('Rank line', 'Tag line'):
            expect(row(label).locator('.rc-overlap[data-rule-mark=both]')).to_have_text('⚠ Both apply')
        row('Rank line').locator('.rc-overlap[data-rule-mark=both]').click()
        pop = page.get_by_role('dialog', name='Both apply')
        expect(pop).to_contain_text('both apply to 1 person today')
        ranks = h.get('coordinator', '/api/catalogues?include=categories')['categories']
        rank_name = next(rank['name'] for rank in ranks if rank['id'] == h.seed['category'])
        expect(pop).to_contain_text(rank_name)
        expect(pop).to_contain_text('exceptional_trainee')
        expect(pop.get_by_role('button', name='Show the other line')).to_be_visible()
        page.keyboard.press('Escape')
        expect(row('Alex line').locator('.rc-overlap')).to_have_count(0)
        expect(page.get_by_role('button', name='That is right')).to_have_count(0)

        main.get_by_role('button', name=re.compile(r'^Filter\b')).click()
        filter_panel = page.locator('.dt-pop')
        add_filter(filter_panel)
        pick(page, filter_panel.locator('.cond-line').last, 'Overlaps', ['Overlaps'])
        expect(main.locator('tbody tr[data-row]')).to_have_count(2)
        expect(row('Rank line')).to_be_visible()
        expect(row('Tag line')).to_be_visible()
        filter_panel.get_by_role('button', name='Clear all filters', exact=True).click()
        page.keyboard.press('Escape')
        expect(main.locator('tbody tr[data-row]')).to_have_count(4)
        navigate(page, 'rules')
        main.get_by_role('button', name='Duties per week lines', exact=True).click()
        for part in (lines[1], lines[3]):
            expect(main.locator(f'li.rt-ln[data-line="{part["id"]}"] .rt-ln-flags')).to_contain_text('⚠ Overlaps')
        navigate(page, 'rules/duties_per_week')
        # The same line twice: Propose stops here, as rule.propose would (422), and sends nothing.
        row('Alex line').locator('input.bulk-check').check()
        duplicate(page)
        expect(bar).to_contain_text('2 duplicate lines')
        sent = []
        page.on('request', lambda r: sent.append(r.post_data or '') if r.url.endswith('/api/commands') else None)
        page.locator('.topbar').get_by_role('button', name=re.compile(r'^Propose \d+ changes?$')).click()
        expect(page.locator('.toast.error')).to_contain_text('is there twice')
        assert not [x for x in sent if 'rule.propose' in x]
        # Remove the copy while its check is held: the answer about the duplicate never shows for the lines without it.
        held = []
        page.route('**/api/rules/catalogue/duties_per_week/check', lambda route: held.append(route))
        with page.expect_request('**/api/rules/catalogue/duties_per_week/check'):
            row('Alex line').last.locator('input.bulk-check').check()
            selection(page).get_by_role('button', name='Remove lines').click()  # armed: press twice
            selection(page).get_by_role('button', name='Remove lines').click()
        expect(main.locator('tbody tr[data-row]')).to_have_count(4)
        expect(bar).to_be_empty()
        expect(main.locator('.rc-overlap')).to_have_count(0)
        held[-1].continue_()
        page.unroute('**/api/rules/catalogue/duties_per_week/check')
        # The ordered exceptions stay quiet after a fresh check.
        expect(bar).to_have_text(re.compile('^⚠ 1 both apply'))
        row('Everyone line').get_by_label(re.compile('^Max: ')).click()
        page.get_by_role('dialog', name='Edit Max').get_by_label('Max', exact=True).fill('6')
        page.get_by_role('dialog', name='Edit Max').get_by_label('Max', exact=True).press('Enter')
        page.locator('.topbar').get_by_role('button', name='Propose 1 change').click()
        dialog = page.get_by_role('dialog', name=re.compile('^Propose'))
        expect(dialog.get_by_label('I have read which lines give way and that is intended')).to_have_count(0)
        dialog.get_by_label('Reason', exact=True).fill('A busier winter')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        expect(main.locator('.rc-waiting')).to_contain_text('waiting for approval')
    finally:
        context.close()
    assert not errors
    proposed = next(e for e in h.get('approver', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_week')['proposed']
    assert len(proposed['check']['overlaps']) == 3


def test_ordered_overrides_need_no_acknowledgement_and_the_approver_sees_conflicts(browser, live_server, h):
    flag(h)
    rule, lines = crafted(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        main = page.locator('#main')
        row = main.locator('tbody tr').filter(has_text='Everyone line')
        row.get_by_label(re.compile('^Max: ')).click()
        page.get_by_role('dialog', name='Edit Max').get_by_label('Max', exact=True).fill('6')
        page.get_by_role('dialog', name='Edit Max').get_by_label('Max', exact=True).press('Enter')
        expect(main.locator('.rc-checkbar')).to_contain_text('⚠ 1 both apply')
        page.locator('.topbar').get_by_role('button', name='Propose 1 change').click()
        dialog = page.get_by_role('dialog', name=re.compile('^Propose'))
        expect(dialog).not_to_contain_text('Lines that give way to more specific ones')
        expect(dialog.get_by_role('checkbox')).to_have_count(0)
        dialog.get_by_label('Reason', exact=True).fill('A busier winter')
        submit = dialog.get_by_role('button', name='Propose', exact=True)
        expect(submit).to_be_enabled()
        submit.click()
        expect(main.locator('.rc-waiting')).to_be_visible()
    finally:
        context.close()
    proposed = next(e for e in h.get('approver', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_week')['proposed']
    said = len(proposed['check']['overlaps']) + len(proposed['check']['warnings'])
    assert said == 4
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules/duties_per_week')
        waiting = page.locator('#main .rc-waiting')
        expect(waiting.locator('.rc-prop-check li')).to_have_count(said)
        # The approver still sees which line each proposed line gives way to, as information, not as a warning.
        expect(waiting.locator('.rc-prop-check')).to_contain_text('gives way to')
        expect(waiting.locator('.rc-prop-warnings li')).to_have_count(len(proposed['check']['warnings']))
        expect(waiting.locator('.rc-prop-gives li')).to_have_count(len(proposed['check']['overlaps']))
        expect(waiting.locator('.rc-prop-warnings')).not_to_contain_text('gives way to')
        expect(waiting.locator('.rc-prop-check')).to_contain_text('both apply to 1 person today')
        expect(waiting.get_by_role('button', name='Review and approve')).to_be_visible()
    finally:
        context.close()
    assert json.dumps(proposed['check']['duplicates']) == '[]'


def test_a_line_that_inherits_a_conflict_is_marked_as_you_edit(browser, live_server, h):
    """Blank inherits, checked on effective numbers as you edit (not only once in force): Alex's "max 0" takes
    everyone's min 2, min above max; None clears it and the mark goes."""
    flag(h)
    a = h.seed['people'][0]
    catalogue_rule(h, 'duties_per_month', [line({'min': {'value': 2, 'level': 'mandatory'}, 'max': {'value': 5, 'level': 'mandatory'}}, note='Everyone line'),
                                           line({'max': {'value': 0, 'level': 'mandatory'}}, who={'conditions': [{'field': 'person_ids', 'values': [a]}]}, note='Leave')])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_month')
        main = page.locator('#main')
        leave = main.locator('tbody tr[data-row]').filter(has_text='Leave')
        expect(main.locator('.rc-checkbar')).to_contain_text('1 inherits a conflict')
        leave.get_by_role('button', name='⚠ Inherits a conflict').click()
        expect(page.get_by_role('dialog', name='Inherits a conflict')).to_contain_text('inherits min 2 from “Everyone at all clinics')
        page.keyboard.press('Escape')
        leave.locator('td[data-col="who"]').click()
        panel = page.locator('.rc-line-panel')
        expect(panel).to_contain_text('⚠ Inherits a conflict')
        panel.get_by_role('button', name='None').first.click()
        panel.get_by_role('button', name='Keep change').click()
        expect(leave.get_by_role('button', name='⚠ Inherits a conflict')).to_have_count(0)
        expect(main.locator('.rc-checkbar')).not_to_contain_text('inherits a conflict')
    finally:
        context.close()


def test_a_failed_proposal_can_be_retried_without_acknowledging_ordered_overrides(browser, live_server, h):
    """A refusal leaves the draft intact for a deliberate retry and sends no acknowledgement."""
    flag(h)
    crafted(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        main = page.locator('#main')
        row = main.locator('tbody tr').filter(has_text='Everyone line')
        row.get_by_label(re.compile('^Max: ')).click()
        page.get_by_role('dialog', name='Edit Max').get_by_label('Max', exact=True).fill('6')
        page.get_by_role('dialog', name='Edit Max').get_by_label('Max', exact=True).press('Enter')
        expect(main.locator('.rc-checkbar')).to_contain_text('⚠ 1 both apply')
        page.locator('.topbar').get_by_role('button', name='Propose 1 change').click()
        dialog = page.get_by_role('dialog', name=re.compile('^Propose'))
        dialog.get_by_label('Reason', exact=True).fill('A busier winter')
        expect(dialog.get_by_role('checkbox')).to_have_count(0)
        propose = dialog.get_by_role('button', name='Propose', exact=True)
        held, sent = [], []

        def hold_first_proposal(route):
            command = route.request.post_data_json
            if command['kind'] != 'rule.propose':
                route.continue_()
                return
            sent.append(command)
            if len(sent) == 1:
                held.append(route)
            else:
                route.continue_()

        page.route('**/api/commands', hold_first_proposal)
        propose.click()
        wait_held(page, held)
        expect(dialog.get_by_role('button', name='Saving…')).to_be_disabled()
        dialog.locator('form').evaluate('form => form.requestSubmit()')
        held.pop().fulfill(status=500, json={'error': {'code': 'SERVER_ERROR', 'message': 'Synthetic refusal'}})
        expect(dialog.get_by_text('Synthetic refusal', exact=True)).to_be_visible()
        expect(propose).to_be_enabled()
        assert len(sent) == 1 and sent[0]['payload']['acknowledged_overlaps'] == []
        propose.click()
        expect(main.locator('.rc-waiting')).to_be_visible()
        assert len(sent) == 2 and sent[1]['payload'] == sent[0]['payload']
    finally:
        context.close()

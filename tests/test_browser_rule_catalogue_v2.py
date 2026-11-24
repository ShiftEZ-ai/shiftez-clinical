"""Catalogue v2 uses the existing Rules/People surfaces and independent decision workflow."""
from .browser_seed import seeded
import os
import re
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import ROLE_NAMES
from .governance_support import identity
from .test_browser import add_filter, browser, condition, holding, live_server, fonts_ready, navigate, route_fetch, sign_in, wait_held  # noqa: F401
from .test_browser_rule_catalogue import flag, rule_action, rules_action
from .test_browser_rule_filter_builder import pick, rota_this_week
from .test_browser_shared_controls import panel_has_handle
from .test_browser_cp_sat import cp_server  # noqa: F401
from .test_rule_catalogue_api import propose as propose_rule
from .test_scheduling_facts_api import decide, workload

pytestmark = pytest.mark.browser
# The diagonal pair: both widths and both themes run every journey; no body here reads a colour, and the only
# branches are width-based. Colour and contrast are guarded by tests/test_web_css_tokens.py.
VIEWS = [(1440, 'light'), (390, 'dark')]
VIEW_IDS = ['desktop-light', 'phone-dark']
# The requested readers come from the contracts; eligibility and operations roles have separate surfaces.
READERS = [role for role in ROLE_NAMES if role not in {'credentialer', 'operator'}]


def shot(page, name, width, theme):
    """CATV2_SHOTS enables the four requested surfaces' 1440/390, light/dark review captures."""
    fonts_ready(page)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'The surface overflows the viewport'
    if os.getenv('CATV2_SHOTS'):
        folder = Path(os.environ['CATV2_SHOTS'])
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f"{name}-{'desktop' if width == 1440 else 'phone'}-{theme}.png"),
                        full_page=False, animations='disabled')


def themed(browser, origin, role, width=1440, theme='light'):
    context, page = sign_in(browser, origin, role, {'width': width, 'height': 1000 if width == 1440 else 844})
    page.evaluate("t => localStorage.setItem('rota.prefs', JSON.stringify({forms:'all', theme:t}))", theme)
    page.reload()
    expect(page.locator('#main[data-loaded-view]')).to_have_count(1, timeout=30000)
    return context, page


def line(numbers, **more):
    return {'id': str(uuid.uuid4()), 'numbers': numbers, **more}


def v2_rule(h, entry, lines, settings=None):
    response = h.client('coordinator').post(f'/api/rules/catalogue/{entry}/check',
                                            json={'catalogue_version': 2, 'lines': lines, 'settings': settings})
    assert response.status_code == 200, response.text
    record = propose_rule(h, entry, lines, catalogue_version=2,
                          acknowledged=[o['line_id'] for o in response.json()['overlaps']], reason='Catalogue v2 fixture',
                          **({'settings': settings} if settings is not None else {}))['record']
    return h.command('approver', 'policy.activate', {'reason': 'Independent review'}, record['id'], record['revision'])['record']


@seeded
def converted_world(h):
    """Two authored ideas plus seeded Fair share; Workload retains one ended line for history."""
    flag(h, legacy=False)
    rank = {'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [h.seed['category']]}]}
    rule = v2_rule(h, 'workload_limit', [
        line({'max': {'value': 5, 'level': 'mandatory'}}, measure='duties', period={'kind': 'calendar_week'}),
        line({'max': {'value': 48, 'level': 'mandatory'}}, who=rank, measure='scheduled_hours', period={'kind': 'rolling', 'days': 7}),
        line({'max': {'value': 2, 'level': 'mandatory'}},
             who={'conditions': [{'field': 'category_ids', 'values': [h.seed['trainee_category']]}]},
             measure='duties', period={'kind': 'day'}, starts_on='2026-08-01', ends_on='2026-09-01', note='Term block')])
    v2_rule(h, 'duty_spacing', [line({'min': {'value': 11, 'level': 'mandatory'}})])
    return rule


def simple_legacy(h):
    flag(h, legacy=False)
    h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    h.policy({'kind': 'min_rest', 'hours': 11})


def reason(page, text, label):
    dialog = page.get_by_role('dialog').filter(has=page.get_by_label('Reason', exact=True))
    dialog.get_by_label('Reason', exact=True).fill(text)
    dialog.get_by_role('button', name=label, exact=True).click()
    confirm = dialog.get_by_role('button', name=re.compile('^Confirm ' + re.escape(label.lower()) + r'\?'))
    if confirm.count():
        confirm.click()


def choose(page, scope, label, option):
    """Use the app's shared picker's keyboard entry point rather than select_option."""
    control = scope.get_by_label(label, exact=True)
    control.focus()
    control.press('Enter')
    picker = page.get_by_role('dialog', name=label, exact=True)
    expect(picker.get_by_role('combobox')).to_be_focused()
    picker.get_by_role('option', name=option, exact=True).click()
    expect(picker).to_have_count(0)


def collection_filter(page, main, field, value):
    """The Rules collection's Filter is the shared condition builder: "Where <field> is <value>"."""
    main.get_by_role('button', name=re.compile('^Filter')).click()
    condition(page.locator('.dt-pop'), field, [value])
    page.keyboard.press('Escape')


def facts_panel(page, person):
    navigate(page, f'people?open={person}')
    panel = page.locator('.person-panel')
    panel.get_by_role('tab', name='Quotas & offers', exact=True).click()
    expect(panel.locator('.ph-section').filter(has_text='Quotas & targets')).to_be_visible()
    return panel


def active_facts(h, person):
    value = workload(h, 4, person_id=person)
    quota = decide(h, h.command('coordinator', 'person.workload.propose', value)['fact'])['fact']
    offer = h.command('coordinator', 'person.overtime.propose', {'kind': 'overtime_offer', 'person_id': person,
        'catalogue_id': 'overtime_offers', 'line': line({'apply': {'value': None, 'level': 'medium'}}, weekdays=[0, 2],
            start_time='17:00', end_time='21:00', where={'conditions': [{'field': 'assignment_type_ids', 'values': [h.seed['type']]}]}),
        'reason': 'Synthetic overtime pattern'})['fact']
    return quota, decide(h, offer)['fact']


@pytest.mark.parametrize('width,theme', VIEWS, ids=VIEW_IDS)
def test_v2_list_hides_empty_and_ended_but_collection_filters_reach_both(browser, live_server, h, width, theme):
    converted_world(h)
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        rows = main.locator('li.rt-item')
        expect(rows).to_have_count(3)
        expect(rows.filter(has_text='Workload limit')).to_contain_text('2 lines')
        main.get_by_role('button', name='All rules', exact=True).click()
        expect(main.locator('li.rt-ln')).to_have_count(4)
        expect(main.locator('li.rt-ln').filter(has_text='duties per calendar week')).to_have_count(1)
        expect(main.locator('li.rt-ln').filter(has_text='scheduled hours in any 7 days')).to_have_count(1)
        expect(main.locator('li.rt-ln').filter(has_text='duties per day')).to_have_count(0)
        shot(page, 'catalogue', width, theme)
        collection_filter(page, main, 'Lines: Valid', 'Ended')
        expect(main.locator('li.rt-ln').filter(has_text='duties per day')).to_have_count(1)
        main.get_by_role('button', name=re.compile('^Filter')).click()
        page.locator('.dt-pop').get_by_role('button', name='Clear all filters', exact=True).click()
        page.keyboard.press('Escape')
        collection_filter(page, main, 'Availability', 'Empty')
        expect(rows.filter(has_text='Days in a row')).to_have_count(1)
        expect(rows.filter(has_text='Workload limit')).to_have_count(0)
        page.reload()
        expect(page.locator('#main li.rt-item').filter(has_text='Days in a row')).to_have_count(1)
        page.get_by_role('button', name='Add rule', exact=True).first.click()
        picker = page.get_by_role('dialog', name='Add rule', exact=True)
        expect(picker.get_by_role('option', name=re.compile('^Days in a row'))).to_be_visible()
        expect(picker.get_by_role('option', name=re.compile('^Workload limit'))).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', VIEWS, ids=VIEW_IDS)
def test_empty_catalogue_and_filtered_empty_keep_the_shared_controls(browser, live_server, h, width, theme):
    flag(h, legacy=False)
    [seeded] = h.get('coordinator', '/api/rules/catalogue?version=2')['entries']
    assert seeded['id'] == 'fair_share' and seeded['rule']['definition']['rule']['lines']
    rule = seeded['rule']
    h.command('approver', 'policy.retire', {'reason': 'Exercise an empty catalogue'}, rule['id'], rule['revision'])
    context, page = themed(browser, live_server, 'admin', width, theme)
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        expect(main.locator('li.rt-item')).to_have_count(0)
        expect(page.get_by_role('button', name='Add rule', exact=True).first).to_be_visible()
        collection_filter(page, main, 'Availability', 'Empty')
        expect(main.locator('li.rt-item')).not_to_have_count(0)
        main.get_by_role('searchbox', name='Search rules', exact=True).fill('No such synthetic rule')
        expect(main.get_by_text('No matching rules', exact=True)).to_be_visible()
        main.get_by_role('searchbox', name='Search rules', exact=True).fill('')
        expect(main.locator('li.rt-item')).not_to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', VIEWS, ids=VIEW_IDS)
def test_workload_measure_period_propose_reload_review_and_undo(browser, live_server, h, width, theme):
    original = converted_world(h)
    first = original['definition']['rule']['lines'][0]['id']
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    approver, review = themed(browser, live_server, 'approver', width, theme)
    try:
        navigate(page, f'rules/workload_limit/{first}')
        panel = page.locator('.rc-line-panel')
        expect(panel.locator('.rc-says')).to_contain_text('at most 5 duties per calendar week')
        choose(page, panel, 'Measure', 'Scheduled hours')
        choose(page, panel, 'Period', 'Rolling')
        panel.get_by_label('Period days', exact=True).fill('14')
        panel.get_by_label('Maximum', exact=True).fill('60')
        expect(panel.locator('.rc-says')).to_contain_text('at most 60 scheduled hours in any 14 days')
        shot(page, 'workload-line', width, theme)
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        page.locator('.topbar').get_by_role('button', name='Propose 1 change', exact=True).click()
        reason(page, 'Two-week hour cap', 'Propose')
        expect(page.locator('.rc-waiting')).to_contain_text('waiting for approval')
        page.reload()
        expect(page.locator('.rc-waiting')).to_contain_text('60')
        expect(page.locator('.rc-waiting').get_by_role('button', name='Review and approve', exact=True)).to_have_count(0)
        navigate(review, 'rules/workload_limit')
        review.locator('.rc-waiting').get_by_role('button', name='Review and approve', exact=True).click()
        # B5 (f1d80dcc4): the approver decides in the docked panel, named after the change, beside the proposer's reason;
        # no rota being planned overlaps, so an independent approval is one step without a reason of its own (BE-9).
        decision = review.get_by_role('region', name='Change a line for everyone', exact=True)
        expect(decision.locator('.rc-quote')).to_have_text('Two-week hour cap')
        # The proposal reads as the editor did: its measure and period, never "at most 60 workload limit".
        expect(decision.locator('.rc-says')).to_contain_text('at most 60 scheduled hours in any 14 days')
        decision.get_by_role('button', name='Approve and activate', exact=True).click()
        expect(decision).to_have_count(0)
        expect(review.locator('.rc-waiting')).to_have_count(0)
        rule = next(e['rule'] for e in h.get('approver', '/api/rules/catalogue?version=2&include_hidden=true')['entries'] if e['id'] == 'workload_limit')
        assert len(rule['definition']['rule']['lines']) == 3, 'Editing a current line must retain the hidden ended line'
        changed = next(l for l in rule['definition']['rule']['lines'] if l['id'] == first)
        assert (changed['measure'], changed['period'], changed['numbers']['max']['value']) == ('scheduled_hours', {'kind': 'rolling', 'days': 14}, 60)
        review.locator('#toast-tray').get_by_role('button', name='Undo', exact=True).click()
        expect(review.locator('#toast-tray')).to_contain_text('Activation undone')
        restored = next(e['rule'] for e in h.get('approver', '/api/rules/catalogue?version=2')['entries'] if e['id'] == 'workload_limit')
        assert restored['id'] == original['id']
    finally:
        context.close()
        approver.close()


@pytest.mark.parametrize('label,option,field,changed', [
    ('Measure', 'Scheduled hours', 'measure', 'scheduled_hours'),
    ('Period', 'Calendar month', 'period', {'kind': 'calendar_month'}),
])
def test_measure_or_period_alone_refreshes_draft_reach(browser, live_server, h, label, option, field, changed):
    flag(h, legacy=False)
    original = line({'max': {'value': 5, 'level': 'mandatory'}},
                    measure='duties', period={'kind': 'calendar_week'})
    v2_rule(h, 'workload_limit', [original])
    entries = h.get('coordinator', '/api/rules/catalogue?version=2&include_hidden=true')['entries']
    original = next(e for e in entries if e['id'] == 'workload_limit')['rule']['definition']['rule']['lines'][0]
    rota_this_week(h)
    context, page = themed(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/workload_limit/' + original['id'])
        panel = page.locator('.rc-line-panel')
        # An Everyone line says "Everyone"; its reach link is in the Who heading.
        people = panel.locator('.rc-lp-count').get_by_role('button', name=re.compile(r'^\d+ people$'))
        people.click()
        pop = page.locator('.picker')  # the shared picker (picker.ts): the period below, each load in words
        expect(pop.locator('.picker-foot')).to_contain_text('Reach fixture')
        loads = pop.get_by_role('option').filter(has_text=re.compile(r'\d+ of \d+'))
        expect(loads).not_to_have_count(0)
        page.keyboard.press('Escape')
        def draft_check(request):
            return (request.method == 'POST' and request.url.endswith('/api/rules/catalogue/workload_limit/check')
                    and any(l.get(field) == changed for l in request.post_data_json['lines']))
        with page.expect_response(lambda response: draft_check(response.request)) as checked:
            choose(page, panel, label, option)
        assert checked.value.ok
        checked.value.finished()
        [draft] = checked.value.request.post_data_json['lines']
        assert draft['numbers'] == original['numbers']
        other = 'period' if field == 'measure' else 'measure'
        assert draft[other] == original[other]
        people.click()
        expect(pop.locator('.picker-foot')).to_have_text('today')
        expect(pop.get_by_role('option').first).to_be_visible()
        expect(loads).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('role', READERS)
@pytest.mark.parametrize('width,theme', VIEWS, ids=VIEW_IDS)
def test_role_permissions_on_catalogue_and_person_facts(browser, live_server, h, role, width, theme):
    original = converted_world(h)
    if role not in h.seed['identities']:
        identity(h, role, [role])
    person = h.get('staff', '/api/me')['person_id'] if role == 'staff' else h.seed['people'][0]
    active_facts(h, person)
    context, page = themed(browser, live_server, role, width, theme)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.evaluate("location.hash='rules'")
        if role in {'staff', 'installation_admin'}:
            expect(page.get_by_text('Restricted page', exact=True)).to_be_visible()
        else:
            expect(page.locator('#main li.rt-item')).to_have_count(3)
            editable = role in {'admin', 'coordinator'}
            if editable:
                expect(page.get_by_role('button', name='Add rule', exact=True).first).to_be_visible()
            else:
                expect(page.get_by_role('button', name='Add rule', exact=True)).to_have_count(0)
            navigate(page, 'rules/workload_limit/' + original['definition']['rule']['lines'][0]['id'])
            panel = page.locator('.rc-line-panel')
            if editable:
                expect(panel.get_by_label('Period', exact=True)).to_be_enabled()
            else:
                expect(panel.get_by_label('Period', exact=True)).to_be_disabled()
                expect(panel.get_by_role('button', name='Keep change', exact=True)).to_have_count(0)
                expect(panel.locator('input:not([readonly]):not([disabled]):not([type=hidden]), textarea:not([readonly]):not([disabled]), select:not([disabled])')).to_have_count(0)
            panel.get_by_role('button', name='Close panel', exact=True).click()
        if role == 'installation_admin':
            navigate(page, 'people')
            expect(page.get_by_text('Restricted page', exact=True)).to_be_visible()
            assert not errors
            return
        panel = facts_panel(page, person)
        expect(panel.locator('.ph-section').filter(has_text='Quotas & targets')).to_contain_text('≤ 4 duties per calendar week · Mandatory')
        expect(panel.locator('.ph-section').filter(has_text='Overtime offers')).not_to_contain_text('No overtime offered.')
        if role in {'admin', 'coordinator'}:
            expect(panel.get_by_role('button', name='Add a personal quota', exact=True)).to_be_visible()
            expect(panel.get_by_role('button', name='Add an overtime offer', exact=True)).to_be_visible()
        else:
            expect(panel.get_by_role('button', name=re.compile('^Add (a personal quota|an overtime offer)$'))).to_have_count(0)
            expect(panel.get_by_role('button', name=re.compile('^Change'))).to_have_count(0)
        if role in {'staff', 'auditor', 'programme_lead'}:
            expect(panel.get_by_role('button', name=re.compile('^(Approve|Retire|Undo|Restore)'))).to_have_count(0)
        fonts_ready(page)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', VIEWS, ids=VIEW_IDS)
def test_simplify_preview_shows_api_before_after_and_equivalence(browser, live_server, h, width, theme):
    simple_legacy(h)
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    try:
        navigate(page, 'rules')
        rules_action(page, 'Simplify rules')
        panel = page.locator('.rs-panel')
        expect(panel.locator('.rs-state')).to_contain_text('Preview')
        panel_has_handle(page, 'simplify-rules', width > 400)
        expect(panel.locator('.rule-facts')).to_contain_text('3 → 3')
        expect(panel.locator('.notice.success')).to_contain_text('Behaviour unchanged')
        expect(panel.locator('.rs-map [data-row]')).to_have_count(3)
        expect(panel.locator('.rs-map')).to_contain_text('Workload limit · 1 line')
        expect(panel.locator('.rs-map')).to_contain_text('Rest between duties · 1 line')
        expect(panel.get_by_role('button', name='Propose', exact=True)).to_be_enabled()
        shot(page, 'simplify-proposal', width, theme)
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')], ids=['desktop-light', 'phone-dark'])
def test_simplify_propose_reload_independent_activate_reload_undo(browser, live_server, h, width, theme):
    simple_legacy(h)
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    approver, review = themed(browser, live_server, 'approver', width, theme)
    try:
        navigate(page, 'rules')
        rules_action(page, 'Simplify rules')
        page.locator('.rs-panel').get_by_role('button', name='Propose', exact=True).click()
        reason(page, 'One rule per idea', 'Propose')
        expect(page.locator('#toast-tray')).to_contain_text('Proposed')
        # The ⋯ menu lists Simplify rules once its conversions read lands (a slow runner opened it first).
        with page.expect_response(lambda r: '/api/rules/catalogue/conversions' in r.url):
            page.reload()
        rules_action(page, 'Simplify rules · waiting for approval')
        panel = page.locator('.rs-panel')
        expect(panel.locator('.rs-state')).to_contain_text('by you')
        expect(panel.get_by_role('button', name='Activate', exact=True)).to_have_count(0)
        expect(panel.get_by_role('button', name='Withdraw', exact=True)).to_be_visible()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        conversions = '**/api/rules/catalogue/conversions?include_history=true'
        with holding(review) as held:
            held.hold(conversions)
            navigate(review, 'rules')
            wait_held(review, held[conversions])
            review.get_by_role('button', name=re.compile('^More rule actions')).click()
            review.get_by_role('menuitem', name='Simplify rules', exact=True).focus()
            held.release(conversions)
            pending = review.get_by_role('menuitem', name='Simplify rules · waiting for approval', exact=True)
            expect(pending).to_be_focused()
            pending.click()
        review.locator('.rs-panel').get_by_role('button', name='Activate', exact=True).click()
        reason(review, 'Independently reviewed', 'Activate')
        expect(review.locator('#toast-tray')).to_contain_text('Rules simplified')
        expect(review.locator('#main li.rt-item').filter(has_text='Workload limit')).to_have_count(1)
        assert h.get('auditor', '/api/rules/catalogue/conversions')['records'][0]['status'] == 'active'
        # The ⋯ menu lists Simplify rules once its conversions read lands (a slow runner opened it first).
        with review.expect_response(lambda r: '/api/rules/catalogue/conversions' in r.url):
            review.reload()
        rules_action(review, 'Simplify rules')
        review.locator('.rs-panel').get_by_role('button', name='Undo', exact=True).click()
        reason(review, 'Restore the previous representation', 'Undo')
        expect(review.locator('#toast-tray')).to_contain_text('The previous rules apply again')
        assert h.get('auditor', '/api/rules/catalogue/conversions')['records'] == []
        history = h.get('auditor', '/api/rules/catalogue/conversions?include_history=true')['records']
        assert len(history) == 1 and history[0]['status'] == 'retired'
        kinds = sorted(p['definition']['rule']['kind'] for p in h.get('admin', '/api/policies?status=active')['policies'])
        assert kinds == ['catalogue', 'max_duties', 'min_rest']
    finally:
        context.close()
        approver.close()


@pytest.mark.parametrize('role', [role for role in ROLE_NAMES if role in {'approver', 'auditor'}])
def test_simplify_preview_is_read_only_without_proposal_permission(browser, live_server, h, role):
    simple_legacy(h)
    context, page = themed(browser, live_server, role)
    try:
        navigate(page, 'rules')
        rules_action(page, 'Simplify rules')
        panel = page.locator('.rs-panel')
        expect(panel.get_by_role('button', name='Propose', exact=True)).to_have_count(0)
        expect(panel.locator('.rs-actions')).to_contain_text('Coordinators and admins propose')
    finally:
        context.close()


def test_simplify_blocker_stays_visible_and_cannot_propose(browser, live_server, h):
    simple_legacy(h)
    h.command('coordinator', 'policy.create', {'name': 'Waiting rule', 'owner': 'Department', 'source': 'Synthetic', 'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'max_duties', 'maximum': 3, 'window': 'week'}, 'scope': {}, 'severity': 'mandatory', 'exception_allowed': False}})
    context, page = themed(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        rules_action(page, 'Simplify rules')
        panel = page.locator('.rs-panel')
        expect(panel.locator('.rs-blockers')).to_contain_text('1 thing stops the move')
        expect(panel.locator('.rs-blockers')).to_contain_text('Rule changes are waiting for approval')
        expect(panel.locator('.rs-blockers li', has_text='Waiting rule').get_by_role('button', name='Open proposal')).to_be_visible()
        expect(panel.get_by_role('button', name='Propose', exact=True)).to_be_disabled()
    finally:
        context.close()


@pytest.mark.parametrize('proof', [None, False], ids=['unknown-proof', 'changed-proof'])
def test_simplify_unfinished_or_changed_proof_never_allows_proposal(browser, live_server, h, proof):
    simple_legacy(h)
    context, page = themed(browser, live_server, 'coordinator')
    def changed(route):
        response = route_fetch(route)
        payload = response.json()
        payload['proof'] = None if proof is None else {**payload['proof'], 'identical': False, 'expanded_equal': False}
        route.fulfill(response=response, json=payload)
    try:
        navigate(page, 'rules')
        page.route('**/api/rules/catalogue/migration?version=2&background=true', changed)
        rules_action(page, 'Simplify rules')
        panel = page.locator('.rs-panel')
        expect(panel.locator('.notice.error')).to_contain_text('did not finish' if proof is None else 'Behaviour would change')
        expect(panel.get_by_role('button', name='Propose', exact=True)).to_be_disabled()
        assert h.get('auditor', '/api/rules/catalogue/conversions')['records'] == []
    finally:
        context.close()


def test_simplify_stale_activation_reports_conflict_without_changing_rules(browser, live_server, h):
    simple_legacy(h)
    active = {p['id']: p for p in h.get('admin', '/api/policies?status=active')['policies']}
    plan = h.get('coordinator', '/api/rules/catalogue/migration?version=2&proof=true')
    record = h.command('coordinator', 'rule.migrate_to_catalogue', {'version': 2, 'plan_hash': plan['plan_hash'], 'reason': 'Synthetic reviewed proposal'})['record']
    context, page = themed(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        rules_action(page, 'Simplify rules · waiting for approval')
        expect(page.locator('.rs-panel').get_by_role('button', name='Activate', exact=True)).to_be_enabled()
        h.workspace('Input changed after the review opened')
        page.locator('.rs-panel').get_by_role('button', name='Activate', exact=True).click()
        reason(page, 'This review is now stale', 'Activate')
        expect(page.get_by_role('dialog').locator('.notice.error')).to_be_visible()
        assert h.get('auditor', f"/api/rules/catalogue/conversions/{record['id']}")['record']['status'] == 'proposed'
        assert {p['id']: p for p in h.get('admin', '/api/policies?status=active')['policies']} == active
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', VIEWS, ids=VIEW_IDS)
def test_person_quota_propose_reload_review_change_and_durable_undo(browser, live_server, h, width, theme):
    flag(h, legacy=False)
    person = h.seed['people'][1]
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    approver, review = themed(browser, live_server, 'approver', width, theme)
    try:
        panel = facts_panel(page, person)
        expect(panel).to_contain_text('None. The department’s workload rules apply.')
        expect(panel).to_contain_text('No overtime offered.')
        panel.get_by_role('button', name='Add a personal quota', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add a personal quota', exact=True)
        dialog.get_by_label('Maximum', exact=True).fill('3')
        choose(page, dialog, 'Period', 'Calendar week')
        reason(page, 'Part-time this term', 'Propose')
        expect(panel.locator('.ph-section').filter(has_text='Waiting for approval')).to_contain_text('≤ 3 duties per calendar week · Mandatory')
        page.reload()
        panel = facts_panel(page, person)
        expect(panel.get_by_role('button', name=re.compile('^Approve'))).to_have_count(0)
        rpanel = facts_panel(review, person)
        rpanel.get_by_role('button', name=re.compile('^Approve')).click()
        reason(review, 'Agreed', 'Approve')
        quotas = rpanel.locator('.ph-section').filter(has_text='Quotas & targets')
        expect(quotas).to_contain_text('≤ 3 duties per calendar week · Mandatory')
        shot(review, 'person-facts', width, theme)
        page.reload()
        panel = facts_panel(page, person)
        panel.get_by_role('button', name=re.compile('^Change')).click()
        page.get_by_role('dialog').get_by_label('Maximum', exact=True).fill('4')
        reason(page, 'Updated personal cap', 'Propose')
        expect(panel.locator('.ph-section').filter(has_text='Waiting for approval')).to_contain_text('≤ 4 duties per calendar week · Mandatory')
        review.reload()
        rpanel = facts_panel(review, person)
        rpanel.get_by_role('button', name=re.compile('^Approve')).click()
        reason(review, 'Independent replacement review', 'Approve')
        expect(rpanel.locator('.ph-section').filter(has_text='Quotas & targets')).to_contain_text('≤ 4 duties per calendar week · Mandatory')
        review.reload()
        rpanel = facts_panel(review, person)
        rpanel.locator('.ph-section').filter(has_text='Quotas & targets').get_by_role('button', name=re.compile('^Undo')).click()
        reason(review, 'Restore the previous cap', 'Undo')
        expect(rpanel.locator('.ph-section').filter(has_text='Quotas & targets')).to_contain_text('≤ 3 duties per calendar week · Mandatory')
        facts = h.get('auditor', f'/api/people/{person}/scheduling-facts')
        assert len(facts['active']) == 1 and len(facts['history']) == 1
        with transaction(h.app.state.engine) as db:
            actions = list(db.scalars(select(m.AuditEvent.action).where(m.AuditEvent.target_id.in_([f['id'] for f in facts['active'] + facts['history']]))))
        assert 'person.workload.propose' in actions and 'person.workload.activate' in actions and 'person.workload.undo' in actions
    finally:
        context.close()
        approver.close()


def test_overtime_offer_uses_shared_scope_builder_and_independent_review(browser, live_server, h):
    flag(h, legacy=False)
    person = h.seed['people'][1]
    context, page = themed(browser, live_server, 'coordinator', 390, 'dark')
    approver, review = themed(browser, live_server, 'approver', 390, 'dark')
    try:
        panel = facts_panel(page, person)
        panel.get_by_role('button', name='Add an overtime offer', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add an overtime offer', exact=True)
        where = dialog.locator('.rc-lp-section').filter(has=page.get_by_role('heading', name='Where', exact=True))
        add_filter(where)
        pick(page, where.locator('.cond-line').first, 'Clinic', ['Continuity clinic'])
        reason(page, 'Synthetic overtime offer', 'Propose')
        expect(panel.locator('.ph-section').filter(has_text='Waiting for approval')).to_contain_text('Continuity clinic')
        rpanel = facts_panel(review, person)
        rpanel.get_by_role('button', name=re.compile('^Approve')).click()
        reason(review, 'Independent offer review', 'Approve')
        expect(rpanel.locator('.ph-section').filter(has_text='Overtime offers')).to_contain_text('Continuity clinic')
        facts = h.get('auditor', f'/api/people/{person}/scheduling-facts')
        [offer] = facts['active']
        assert offer['kind'] == 'overtime_offer'
        assert offer['data']['line']['where']['conditions'][0]['values'] == [h.seed['services'][0]]
        rpanel.get_by_role('button', name=re.compile('^Undo')).click()
        reason(review, 'Undo the offer', 'Undo')
        expect(rpanel.locator('.ph-section').filter(has_text='Overtime offers')).to_contain_text('No overtime offered.')
    finally:
        context.close()
        approver.close()


@pytest.mark.parametrize('surface', ['catalogue', 'simplify', 'facts'])
def test_failed_read_shows_retry_and_recovers_without_losing_context(browser, live_server, h, surface):
    simple_legacy(h) if surface == 'simplify' else flag(h, legacy=False)
    person = h.seed['people'][0]
    context, page = themed(browser, live_server, 'coordinator')
    pattern = {'catalogue': '**/api/rules/catalogue?*', 'simplify': '**/api/rules/catalogue/migration?version=2&background=true',
               'facts': f'**/api/people/{person}/scheduling-facts'}[surface]
    try:
        page.route(pattern, lambda route: route.fulfill(status=503, json={'error': {'code': 'UNAVAILABLE', 'message': 'Synthetic read outage'}}), times=1)
        if surface == 'catalogue':
            page.evaluate("location.hash='rules'")
            expect(page.locator('#main')).to_contain_text('Couldn’t load Rules.')
            page.get_by_role('button', name='Retry', exact=True).click()
            expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'rules')
            expect(page.get_by_role('button', name='Add rule', exact=True).first).to_be_visible()
        elif surface == 'simplify':
            navigate(page, 'rules')
            rules_action(page, 'Simplify rules')
            panel = page.locator('.rs-panel')
            expect(panel.locator('.notice.error')).to_contain_text('Synthetic read outage')
            panel.get_by_role('button', name='Try again', exact=True).click()
            expect(panel.locator('.notice.success')).to_contain_text('Behaviour unchanged')
        else:
            navigate(page, f'people?open={person}')
            panel = page.locator('.person-panel')
            panel.get_by_role('tab', name='Quotas & offers', exact=True).click()
            expect(panel.locator('.notice.error')).to_contain_text('Synthetic read outage')
            # DL-045 (18c3bd77a): a panel part's Retry names it, as every person-panel part (test_browser_person_record)
            panel.get_by_role('button', name='Retry loading quotas & offers', exact=True).click()
            expect(panel.locator('.ph-section').filter(has_text='Quotas & targets')).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('surface', ['catalogue', 'simplify', 'facts'])
def test_loading_states_wait_for_their_own_read_and_show_content_afterward(browser, live_server, h, surface):
    simple_legacy(h) if surface == 'simplify' else flag(h, legacy=False)
    person = h.seed['people'][0]
    context, page = themed(browser, live_server, 'coordinator', 390, 'dark')
    pattern = {'catalogue': '**/api/rules/catalogue?*', 'simplify': '**/api/rules/catalogue/migration?version=2&background=true',
               'facts': f'**/api/people/{person}/scheduling-facts'}[surface]
    held = []
    try:
        page.route(pattern, lambda route: held.append(route), times=1)
        if surface == 'catalogue':
            with page.expect_request(pattern):
                page.evaluate("location.hash='rules'")
            scope = page.locator('#main')
        elif surface == 'simplify':
            navigate(page, 'rules')
            with page.expect_request(pattern):
                rules_action(page, 'Simplify rules')
            scope = page.locator('.rs-panel')
        else:
            navigate(page, f'people?open={person}')
            panel = page.locator('.person-panel')
            with page.expect_request(pattern):
                panel.get_by_role('tab', name='Quotas & offers', exact=True).click()
            # The tab's own wait: the Overview behind it loads on its own (its card may still be reading).
            scope = panel.get_by_role('tabpanel', name='Quotas & offers', exact=True)
        expect(scope.locator('.loading[aria-busy=true]')).to_be_visible()
        page.wait_for_function("() => document.querySelector('.loading:not(.sk-wait)') !== null")
        held.pop().continue_()
        expect(scope.locator('.loading[aria-busy=true]')).to_have_count(0)
        if surface == 'simplify':
            expect(scope.locator('.notice.success')).to_contain_text('Behaviour unchanged')
        elif surface == 'facts':
            expect(scope.locator('.ph-section').filter(has_text='Quotas & targets')).to_be_visible()
        else:
            expect(scope.get_by_role('searchbox', name='Search rules', exact=True)).to_be_visible()
    finally:
        for route in held:
            route.abort()
        context.close()


def test_person_target_keeps_approved_goal_scope_period_and_strength(browser, live_server, h):
    flag(h, legacy=False)
    goal = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'target', 'amount': 10}, severity='advisory')
    person = h.seed['people'][1]
    context, page = themed(browser, live_server, 'coordinator')
    approver, review = themed(browser, live_server, 'approver')
    try:
        panel = facts_panel(page, person)
        panel.get_by_role('button', name='Add a personal target', exact=True).click()
        page.get_by_role('dialog', name='Choose a target source', exact=True).get_by_role('button', name='Continue', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add a personal target', exact=True)
        expect(dialog).to_contain_text('Scope, period and strength come from the approved Fair-share goal.')
        expect(dialog.locator('.cond-root')).to_have_count(0)
        expect(dialog.get_by_label('Measure', exact=True)).to_have_count(0)
        dialog.get_by_label('Target', exact=True).fill('7')
        reason(page, 'Personal share this term', 'Propose')
        expect(panel.locator('.ph-section').filter(has_text='Waiting for approval')).to_contain_text('(personal target)')
        rpanel = facts_panel(review, person)
        rpanel.get_by_role('button', name=re.compile('^Approve')).click()
        reason(review, 'Independent target review', 'Approve')
        expect(rpanel.locator('.ph-section').filter(has_text='Quotas & targets')).to_contain_text('(personal target)')
        [target] = h.get('auditor', f'/api/people/{person}/scheduling-facts')['active']
        assert target['kind'] == 'target' and target['data']['goal_ref']['policy_id'] == goal['id']
        assert target['data']['line']['numbers']['target']['value'] == 7
        assert target['data']['line']['numbers']['target']['level'] in (None, 'medium')
        assert target['data']['line'].get('where') is None
        assert target['data']['line'].get('period') is None
    finally:
        context.close()
        approver.close()


@pytest.mark.parametrize('surface', ['simplify', 'facts'])
def test_alternate_login_of_proposer_is_refused_by_independence_guard(browser, live_server, h, surface):
    person = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['coordinator']).person_id = person
    identity(h, 'same_person_approver', ['approver'], person=person)
    if surface == 'simplify':
        simple_legacy(h)
        plan = h.get('coordinator', '/api/rules/catalogue/migration?version=2&proof=true')
        record = h.command('coordinator', 'rule.migrate_to_catalogue', {'version': 2, 'plan_hash': plan['plan_hash'], 'reason': 'Synthetic full proposal'})['record']
    else:
        flag(h, legacy=False)
        record = h.command('coordinator', 'person.workload.propose', workload(h))['fact']
    context, page = themed(browser, live_server, 'same_person_approver')
    try:
        if surface == 'simplify':
            navigate(page, 'rules')
            rules_action(page, 'Simplify rules · waiting for approval')
            page.locator('.rs-panel').get_by_role('button', name='Activate', exact=True).click()
            reason(page, 'Another login of the same person', 'Activate')
            expect(page.get_by_role('dialog').locator('.notice.error')).to_be_visible()
            assert h.get('auditor', f"/api/rules/catalogue/conversions/{record['id']}")['record']['status'] == 'proposed'
        else:
            panel = facts_panel(page, person)
            panel.get_by_role('button', name=re.compile('^Approve')).click()
            reason(page, 'Another login of the same person', 'Approve')
            expect(page.locator('.toast.error')).to_be_visible()
            facts = h.get('auditor', f'/api/people/{person}/scheduling-facts')
            assert facts['active'] == [] and [f['id'] for f in facts['proposed']] == [record['id']]
    finally:
        context.close()


@pytest.mark.parametrize('action', ['Withdraw', 'Decline'])
def test_conversion_decline_and_withdraw_leave_current_rules_and_a_history_record(browser, live_server, h, action):
    simple_legacy(h)
    plan = h.get('coordinator', '/api/rules/catalogue/migration?version=2&proof=true')
    h.command('coordinator', 'rule.migrate_to_catalogue', {'version': 2, 'plan_hash': plan['plan_hash'], 'reason': 'Synthetic complete proposal'})
    role = 'coordinator' if action == 'Withdraw' else 'approver'
    context, page = themed(browser, live_server, role)
    try:
        navigate(page, 'rules')
        rules_action(page, 'Simplify rules · waiting for approval')
        page.locator('.rs-panel').get_by_role('button', name=action, exact=True).click()
        if action == 'Withdraw':  # cannot be undone: the same-button confirm
            page.locator('.rs-panel').get_by_role('button', name='Withdraw?', exact=True).click()
        if action == 'Decline':
            reason(page, 'Keep the current representation', 'Decline')
        expect(page.locator('#toast-tray')).to_contain_text('Proposal withdrawn' if action == 'Withdraw' else 'Declined')
        assert h.get('auditor', '/api/rules/catalogue/conversions')['records'] == []
        [history] = h.get('auditor', '/api/rules/catalogue/conversions?include_history=true')['records']
        assert history['status'] == 'declined'
        assert sorted(p['definition']['rule']['kind'] for p in h.get('admin', '/api/policies?status=active')['policies']) == ['catalogue', 'max_duties', 'min_rest']
        page.reload()
        rules_action(page, 'Simplify rules')
        panel = page.locator('.rs-panel')
        panel.get_by_text('Proposal history', exact=True).click()
        expect(panel.locator('details')).to_contain_text('Synthetic complete proposal')
    finally:
        context.close()


def test_no_duties_and_zero_quota_remain_distinct_workload_modes(browser, live_server, h):
    original = converted_world(h)
    context, page = themed(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/workload_limit/' + original['definition']['rule']['lines'][0]['id'])
        panel = page.locator('.rc-line-panel')
        panel.get_by_label('Maximum', exact=True).fill('0')
        expect(panel.get_by_label('Mode', exact=True)).to_have_value('count')
        expect(panel.locator('.rc-says')).to_contain_text('at most 0 duties per calendar week')
        choose(page, panel, 'Mode', 'No duties')
        expect(panel.get_by_label('Maximum', exact=True)).to_have_count(0)
        expect(panel.get_by_label('Measure', exact=True)).to_be_hidden()
        expect(panel.locator('.rc-says')).to_contain_text('no duties')
        expect(panel.get_by_role('checkbox', name='No duties', exact=True)).to_be_checked()
    finally:
        context.close()


def test_person_fact_review_checks_the_exact_fact_against_affected_rotas(browser, cp_server, h):
    from apps.worker.main import process_one
    flag(h, legacy=False)
    w = h.shift(h.workspace('Personal workload contextual review'))['workspace']
    fact = h.command('coordinator', 'person.workload.propose', workload(h, 2))['fact']
    before = h.snapshot(w)
    context, page = themed(browser, cp_server, 'approver')
    try:
        panel = facts_panel(page, fact['person_id'])
        panel.get_by_role('button', name=re.compile('^Approve')).click()
        dialog = page.get_by_role('dialog', name=re.compile('^Review'))
        expect(dialog.get_by_text('Not checked', exact=True)).to_be_visible()
        expect(dialog.locator('.rule-gate')).to_contain_text('needs a fresh check')
        expect(dialog.get_by_role('button', name='Approve', exact=True)).to_be_disabled()
        assert h.get('auditor', f"/api/people/{fact['person_id']}/scheduling-facts")['active'] == []
        dialog.get_by_role('button', name='Check all affected rotas', exact=True).click()
        expect(dialog.get_by_text('Waiting to check', exact=True)).to_be_visible()
        assert process_one(h.app.state.engine)
        expect(dialog.get_by_text('Works with this rule', exact=True)).to_be_visible(timeout=30000)
        assert h.snapshot(w) == before
        [job] = h.get('approver', f"/api/jobs?workspace_id={w['id']}")['jobs']
        checked = h.get('approver', '/api/jobs/' + job['id'])['job']
        assert checked['result']['bindings'][0]['fact_id'] == fact['id']
        reason(page, 'Independent checked approval', 'Approve')
        expect(dialog).to_have_count(0)
        assert [f['id'] for f in h.get('auditor', f"/api/people/{fact['person_id']}/scheduling-facts")['active']] == [fact['id']]
    finally:
        context.close()


def calendar_panel(page):
    navigate(page, 'programmes')
    page.get_by_role('button', name='Academic calendar', exact=True).click()
    page.get_by_role('button', name='Protected time', exact=True).first.click()
    panel = page.locator('.dock-panel').filter(has=page.get_by_role('heading', name='Protected time', exact=True))
    expect(panel).to_be_visible()
    return panel


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')], ids=['desktop-light', 'phone-dark'])
def test_calendar_protection_is_reviewed_data_and_metadata_cannot_change_its_dates(browser, live_server, h, width, theme):
    programme = h.configure('programmes', {'name': 'Synthetic teaching programme', 'starts_on': '2026-01-01'})['record']
    entry = h.configure('programme_calendar_entries', {'name': 'Synthetic teaching block', 'programme_id': programme['id'],
        'kind': 'academic_block', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}, role='coordinator')['record']
    # Programmes is a programme page: the reviewer is a programme lead who also approves (a different person).
    identity(h, 'programme_approver', ['programme_lead', 'approver'])
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    approver, review = themed(browser, live_server, 'programme_approver', width, theme)
    try:
        panel = calendar_panel(page)
        panel_has_handle(page, 'calendar-protection', width > 400)
        expect(panel).to_contain_text('No protection recorded.')
        panel.get_by_role('button', name='Add protection', exact=True).click()
        dialog = page.get_by_role('dialog', name='Protect this time', exact=True)
        expect(dialog.locator('.cond-root')).to_have_count(2)
        dialog.get_by_label('From · local time', exact=True).fill('08:00')
        dialog.get_by_label('Until · local time', exact=True).fill('12:00')
        dialog.get_by_label('First protected day', exact=True).fill('2026-10-05')
        dialog.get_by_label('Last protected day (included)', exact=True).fill('2026-10-06')
        reason(page, 'Approved academic protection proposal', 'Propose protected time')
        expect(panel).to_contain_text('08:00–12:00')
        expect(panel.get_by_role('button', name='Review', exact=True)).to_have_count(0)
        rpanel = calendar_panel(review)
        rpanel.get_by_role('button', name='Review', exact=True).click()
        reason(review, 'Independent academic protection approval', 'Approve')
        expect(rpanel.get_by_role('button', name='Review', exact=True)).to_have_count(0)
        expect(rpanel.locator('.badge', has_text='active')).to_have_count(1)
        [fact] = h.get('auditor', f"/api/programme-calendar-entries/{entry['id']}/protections")['protections']
        assert fact['status'] == 'active'
        assert (fact['starts_on'], fact['ends_on']) == ('2026-10-05', '2026-10-07')
        page.reload()
        panel = calendar_panel(page)
        panel.get_by_role('button', name='Close panel', exact=True).click()
        page.get_by_role('button', name='Edit', exact=True).first.click()
        edit = page.get_by_role('dialog', name='Edit academic calendar entry', exact=True)
        expect(edit.locator('.notice.warning')).to_contain_text('approved or waiting protection')
        for label in ('Programme', 'Entry type', 'Effective from', 'Effective until (included)'):
            expect(edit.get_by_label(label, exact=True)).to_be_disabled()
        edit.get_by_role('button', name='Cancel', exact=True).click()
        review.reload()
        rpanel = calendar_panel(review)
        rpanel.get_by_role('button', name='Undo', exact=True).click()
        reason(review, 'Undo the protection approval', 'Undo')
        expect(rpanel.locator('.badge', has_text='retired')).to_have_count(1)
        [retained] = h.get('auditor', f"/api/programme-calendar-entries/{entry['id']}/protections")['protections']
        assert retained['id'] == fact['id'] and retained['status'] == 'retired'
    finally:
        context.close()
        approver.close()


def test_rule_counts_agree_between_view_button_and_footer(browser, live_server, h):
    """RU-03: the Rules view's count and the footer count the same rules; the footer adds how many are in force when that
    differs (a rule waiting for approval is listed, not in force)."""
    flag(h, legacy=False)
    v2_rule(h, 'duty_spacing', [line({'min': {'value': 11, 'level': 'mandatory'}})])
    propose_rule(h, 'workload_limit', [line({'max': {'value': 5, 'level': 'mandatory'}}, measure='duties', period={'kind': 'calendar_week'})],
                 catalogue_version=2, reason='Waiting')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        # The department's seeded Fair share is in force too (scoring_defaults): 3 listed, the waiting one not in force.
        expect(page.locator('.app-footer .dt-count')).to_have_text('3 rules')
        expect(page.locator('.app-footer .footer-total')).to_contain_text(re.compile(r'^2 in force\b'))
        page.locator('#main button.dt-saved').click()
        expect(page.get_by_role('menuitemradio', name=re.compile(r'^All rules\b'))).to_contain_text('3')
    finally:
        context.close()


def test_empty_department_sees_common_first_rules(browser, live_server, h):
    """RU-20: with no rules yet, Add rule opens on "Common first rules" (rest, workload, people per session, days in a row)
    before the families."""
    flag(h, legacy=False)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.locator('.topbar').get_by_role('button', name='Add rule', exact=True).click()
        picker = page.locator('.picker')
        expect(picker.locator('.picker-group').first).to_contain_text('Common first rules')
        for name in ('Rest between duties', 'Workload limit', 'People per session', 'Days in a row'):
            expect(picker.get_by_role('option', name=name, exact=True)).to_be_visible()
    finally:
        context.close()


def test_rule_page_states_exception_eligibility(browser, live_server, h):
    """RU-06: a rule in force says on its page how person lines may differ (never weaker for a safety rule) and what it
    is checked against. FE-B (f1d80dcc4): "Person lines" replaced "Exception requests" on catalogue rules."""
    flag(h, legacy=False)
    v2_rule(h, 'people_per_session', [line({'min': {'value': 1, 'level': 'mandatory'}})])
    v2_rule(h, 'days_in_a_row', [line({'max': {'value': 6, 'level': 'mandatory'}})])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        for rule, allowed in (('people_per_session', 'Allowed, never weaker, with approval'), ('days_in_a_row', 'Allowed, with approval')):
            navigate(page, 'rules/' + rule)
            facts = page.locator('#main .rule-facts')
            expect(facts.locator('div').filter(has=page.get_by_text('Person lines', exact=True)).locator('dd')).to_have_text(allowed)
            expect(facts.locator('div').filter(has=page.get_by_text('Checked against', exact=True)).locator('dd')).to_have_text('Rotas being planned')
    finally:
        context.close()


def test_approver_can_review_a_live_catalogue_rule_from_detail_actions(browser, live_server, h):
    """QA-049: an active catalogue rule keeps the live-rule check in its detail menu and starts the policy review read."""
    flag(h, legacy=False)
    active = v2_rule(h, 'days_in_a_row', [line({'max': {'value': 6, 'level': 'mandatory'}})])
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules/days_in_a_row')
        page.locator('.topbar').get_by_role('button', name='More actions for this rule', exact=True).click()
        with page.expect_request(lambda request: request.method == 'GET' and
                                 request.url.endswith(f"/api/policies/{active['id']}/review")) as review:
            # The detail action starts the shared effect read and opens the existing rule-review dialog.
            page.get_by_role('menuitem', name='Check rule against rotas', exact=True).click()
        assert review.value.method == 'GET'
        expect(page.get_by_role('dialog', name=re.compile(r'^Review '))).to_be_visible()
    finally:
        context.close()


def test_catalogue_rule_page_has_retire_and_in_draft(browser, live_server, h):
    """UX-16: the catalogue rule page shows how the rule fares in the draft being planned and lets an approver retire it."""
    flag(h, legacy=False)
    v2_rule(h, 'duty_spacing', [line({'min': {'value': 11, 'level': 'mandatory'}})])
    h.shift(h.workspace('Retire draft'))
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules/duty_spacing')
        expect(page.locator('#main').get_by_text('In Retire draft', exact=False)).to_be_visible()
        rule_action(page, 'Retire rule')
        dialog = page.get_by_role('dialog', name=re.compile('^Retire '))
        dialog.get_by_label('Reason', exact=True).fill('No longer needed')
        dialog.get_by_role('button', name='Retire rule', exact=True).click()
        expect(page.get_by_text('retired. It no longer applies', exact=False)).to_be_visible()
    finally:
        context.close()
    entry = next(e for e in h.get('approver', '/api/rules/catalogue?version=2&include_hidden=true')['entries'] if e['id'] == 'duty_spacing')
    assert not entry['rule'] or entry['rule']['status'] != 'active'



def test_catalogue_retire_waiting_for_a_second_approval_keeps_the_rule(browser, live_server, h):
    """With two approvals required, the first approver's retire says it waits (never "retired") and its Undo withdraws."""
    from .test_policy_transition_approvals import settings
    flag(h, legacy=False)
    v2_rule(h, 'duty_spacing', [line({'min': {'value': 11, 'level': 'mandatory'}})])
    settings(h, 2)
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules/duty_spacing')
        rule_action(page, 'Retire rule')
        dialog = page.get_by_role('dialog', name=re.compile('^Retire '))
        dialog.get_by_label('Reason', exact=True).fill('No longer needed')
        dialog.get_by_role('button', name='Retire rule', exact=True).click()
        waiting = page.locator('.toast').filter(has_text='Retiring approved by you (1 of 2) · 1 more approval needed from a different approver')
        expect(waiting).to_be_visible()
        expect(page.get_by_text('It no longer applies', exact=False)).to_have_count(0)
        entry = next(e for e in h.get('approver', '/api/rules/catalogue?version=2')['entries'] if e['id'] == 'duty_spacing')
        assert entry['rule']['status'] == 'active'
        waiting.get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Approval withdrawn. The rule is unchanged.', exact=True)).to_be_visible()
    finally:
        context.close()
    # Withdrawn on the server: the same approver's vote counts again as the first of two (a vote still held is refused).
    rule = next(e for e in h.get('approver', '/api/rules/catalogue?version=2')['entries'] if e['id'] == 'duty_spacing')['rule']
    again = h.command('approver', 'policy.retire', {'reason': 'No longer needed'}, rule['id'], rule['revision'])
    assert (again['approval']['approvals_received'], again['approval']['complete'], again['record']['status']) == (1, False, 'active')


def test_reach_popover_shows_shortfall_for_minimum_lines(browser, live_server, h):
    """RU-16: a minimum line's reach lists who is short ("2 of 3, short") with the bar beside the words."""
    flag(h, legacy=False)
    # Per day: a rule activated on the pinned day (a Saturday) is in force on no working day of this week, so a weekly
    # minimum is prorated to 0 there; today's own window is whole and keeps its 3.
    v2_rule(h, 'workload_limit', [line({'min': {'value': 3, 'level': 'mandatory'}}, measure='duties', period={'kind': 'day'})])
    rota_this_week(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/workload_limit')
        page.locator('#main tr[data-row] td[data-col="who"]').first.click()
        panel = page.locator('.rc-line-panel')
        # An "Everyone" line's sentence says "Everyone"; its reach link sits in the Who heading.
        panel.locator('.rc-lp-count').get_by_role('button', name=re.compile(r'^\d+ (people|person)$')).first.click()
        pop = page.locator('.picker')
        alex = pop.get_by_role('option', name=re.compile('^Alex Morgan'))
        expect(alex).to_contain_text('2 of 3, short')
        expect(alex.locator('.picker-meta .rc-load.short .rc-load-bar')).to_have_count(1)
        expect(pop.locator('.picker-foot')).to_contain_text('short')
    finally:
        context.close()


def test_retiring_a_revised_rule_says_the_version_it_replaced_applies_again(browser, live_server, h):
    """revise 11 h to 8 h, then Retire: the server restores 11 h, and the dialog and message say so."""
    flag(h, legacy=False)
    first = v2_rule(h, 'duty_spacing', [line({'min': {'value': 11, 'level': 'mandatory'}})])
    revised = propose_rule(h, 'duty_spacing', [line({'min': {'value': 8, 'level': 'mandatory'}})], catalogue_version=2,
                           policy_id=first['id'], revision=first['revision'])['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent review'}, revised['id'], revised['revision'])
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules/duty_spacing')
        rule_action(page, 'Retire rule')
        dialog = page.get_by_role('dialog', name=re.compile('^Retire '))
        expect(dialog).to_contain_text('the version it replaced applies again')
        dialog.get_by_label('Reason', exact=True).fill('Back to the earlier rest')
        # The dialog already contains this sentence; only the status message proves retirement completed.
        completed = page.get_by_role('status').filter(has_text='The version it replaced applies again')
        with holding(page) as held:
            held.hold('**/api/commands')
            dialog.get_by_role('button', name='Retire rule', exact=True).click()
            wait_held(page, held['**/api/commands'])
            expect(completed).to_have_count(0)
            current = next(e for e in h.get('approver', '/api/rules/catalogue?version=2')['entries'] if e['id'] == 'duty_spacing')
            assert current['rule']['id'] == revised['id'] and current['rule']['status'] == 'active'
            held.release('**/api/commands')
            expect(completed).to_be_visible()
        expect(page.get_by_text('It no longer applies', exact=False)).to_have_count(0)
    finally:
        context.close()
    entry = next(e for e in h.get('approver', '/api/rules/catalogue?version=2')['entries'] if e['id'] == 'duty_spacing')
    assert entry['rule']['id'] == first['id'] and entry['rule']['status'] == 'active'


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_a_click_on_a_live_rules_note_opens_its_line_not_an_edit(browser, live_server, h, width, theme):
    """H-14: one click on a live rule's Note shows the line (its whole note in the line's panel), never a free-text
    edit in the table cell."""
    flag(h, legacy=False)
    note = 'Keep the evening lead for the consultant on call'
    v2_rule(h, 'duty_spacing', [line({'min': {'value': 11, 'level': 'mandatory'}}, note=note)])
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    try:
        navigate(page, 'rules/duty_spacing')
        cell = page.locator('#main tr[data-row] td[data-col="note"], #main [data-row] [data-col="note"]').first
        if width == 1440:
            expect(cell).to_have_text(note)
            cell.click()
            expect(page.locator('#main td[data-col="note"] input, #main td[data-col="note"] textarea, .dt-edit-bar')).to_have_count(0)
        else:
            page.locator('#main [data-row]').first.click()
        panel = page.locator('.rc-line-panel')
        expect(panel.get_by_label('Note', exact=True)).to_have_value(note)
        shot(page, 'rule-note-opens-line', width, theme)
    finally:
        context.close()


def test_old_rule_link_opens_its_v2_rule_and_unknown_id_says_so(browser, live_server, h):
    """#rules/keep_off (folded into Workload limit by catalogue v2) replaces the address and opens that rule."""
    flag(h, legacy=False)
    context, page = themed(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/keep_off')
        expect(page).to_have_url(re.compile(r'#rules/workload_limit$'))
        expect(page.locator('#main').get_by_role('heading', name='Workload limit')).to_be_visible()
        expect(page.get_by_text('This rule is not available. Choose one from Rules.', exact=True)).to_have_count(0)
        navigate(page, 'rules/no_such_rule')
        expect(page.get_by_text('This rule is not available. Choose one from Rules.', exact=True)).to_be_visible()
    finally:
        context.close()

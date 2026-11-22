"""B3–B5 (mockups/B): a person line's relation to the broader line and its effect (B3), Relax for this person from a
blocker row (B4, D16: applied to this rota) and the approver's one-step decision (B5).

Routes give department-shaped answers of the real contracts (the check's `relations`, maintenance's unsaved-line `/effect`,
`/api/catalogues?active_only=true`, `POST /api/policies/{id}/relax`, `GET /api/policies/{id}/review`) for drafts this
small world does not have, and capture the D16 `workspace.rule_adjust` scope. FEB_R_SHOTS=<folder> writes the review
screenshots at 1440/390, light/dark."""
import copy
import re
import uuid
import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, route_fetch, sign_in, fonts_ready, fix_list_reply  # noqa: F401
from .test_browser_rule_catalogue import flag
from .test_browser_rule_catalogue_v2 import line, v2_rule
from .test_rule_catalogue_api import propose as propose_rule
from packages.contracts.schemas import FIX_ACTION_LABELS
from packages.domain.time import now

pytestmark = pytest.mark.browser
VIEWS = [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')] if os.getenv('FEB_R_SHOTS') else [(1440, 'light')]
IDS = [f"{'desktop' if w == 1440 else 'phone'}-{t}" for w, t in VIEWS]


def shot(page, screen, state, width, theme):
    if not os.getenv('FEB_R_SHOTS'):
        return
    fonts_ready(page)
    folder = Path(os.environ['FEB_R_SHOTS'])
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / f"{screen}--{state}--{'desktop' if width == 1440 else 'phone'}-{theme}.png"), animations='disabled')


def themed(browser, origin, role, width, theme):
    context, page = sign_in(browser, origin, role, {'width': width, 'height': 900 if width == 1440 else 844})
    page.evaluate("t => localStorage.setItem('rota.prefs', JSON.stringify({forms:'all', theme:t}))", theme)
    page.reload()
    expect(page.locator('#main[data-loaded-view]')).to_have_count(1, timeout=30000)
    return context, page


def world(h):
    """Workload limit: a rank's mandatory weekly maximum at one clinic, and one person's advisory line for a week."""
    flag(h, legacy=False)
    person = h.seed['people'][0]
    name = next(p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == person)
    where = {'match': 'all', 'conditions': [{'field': 'service_ids', 'op': 'any', 'values': [h.seed['services'][0]]}]}
    rank = {'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [h.seed['category']]}]}
    who = {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [person]}]}
    broad = line({'max': {'value': 5, 'level': 'mandatory'}}, who=rank, where=where, measure='duties', period={'kind': 'calendar_week'})
    mine = line({'max': {'value': 6, 'level': 'medium'}}, who=who, where=where, measure='duties', period={'kind': 'calendar_week'},
                starts_on='2026-10-04', ends_on='2026-10-11')
    rule = v2_rule(h, 'workload_limit', [broad, mine])
    return rule, broad, mine, person, name, who, where


def relation(broad, overrides):
    return {'line_id': broad['id'], 'line_no': 1, 'overrides': overrides, 'both_apply': not overrides,
            'reason': None if overrides else 'strength_differs', 'fix': None if overrides else 'match_strength',
            'default_severity': {'max': 'mandatory'}}


NOW = now().isoformat()


@pytest.mark.parametrize('width,theme', VIEWS, ids=IDS)
def test_line_relation_effect_reach_and_inactive_clinics(browser, live_server, h, width, theme):
    rule, broad, mine, person, name, _, _ = world(h)
    state = {'overrides': False}

    def check(route):
        response = route_fetch(route)
        body = response.json()
        body['relations'] = {mine['id']: relation(broad, state['overrides'])}
        route.fulfill(response=response, json=body)

    def line_effect(route):  # maintenance unsaved-line effect: the shape the server sends, for a draft this world does not have
        sent = json.loads(route.request.post_data)
        effects.append(sent)
        route.fulfill(json={'line_id': sent['line']['id'], 'preview': True, 'committed': False, 'relations': {}, 'effects': {sent['line']['id']: [
            {'workspace_id': 'w1', 'name': 'Rota 4–10 Oct', 'revision': 1, 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
             'effect': {'status': 'ready', 'clears': 1, 'adds': 0, 'effect': 'clears', 'summary': ''}, 'cleared_checks': 1, 'new_must_checks': 0}]}})
    effects = []
    services = h.get('coordinator', '/api/catalogues')['services']
    chosen = next(x for x in services if x['id'] == h.seed['services'][0])
    hidden = next(x for x in services if x['id'] != chosen['id'])
    active = {'services': [chosen], 'locations': [], 'assignment_types': [], 'inactive_count': {'services': 1}}
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    try:
        page.route('**/api/rules/catalogue/workload_limit/check', check)
        page.route('**/api/rules/catalogue/workload_limit/effect', line_effect)
        page.route('**/api/catalogues?active_only=true**', lambda route: route.fulfill(json=active))
        navigate(page, f"rules/workload_limit/{mine['id']}")
        main, panel = page.locator('#main'), page.locator('.rc-line-panel')
        # The rule fact says what the page allows: person lines, through approval.
        expect(main.locator('.rc-facts')).to_contain_text('Person lines')
        expect(main.locator('.rc-facts')).to_contain_text('Allowed, with approval')
        says = panel.locator('.rc-says')
        expect(says).to_have_class('rc-says is-warn')
        expect(says.locator('.rc-relation')).to_contain_text(
            f'Does not override line 1: its maximum is Mandatory, this one Advisory. Both apply, so {name} still has at most 5 duties.')
        # The line it names is a link, and its row is tinted in the table.
        if width == 1440:
            expect(main.locator(f'tr[data-line="{broad["id"]}"].is-pointed')).to_have_count(1)
        effect = panel.locator('.rc-effect')
        expect(effect.locator('li').nth(0)).to_contain_text('Clears 1 check')
        assert effects[-1]['line']['id'] == mine['id'] and effects[-1]['expected_policy_id'] == rule['id']
        expect(panel.get_by_role('button', name='Only drafts whose dates overlap Sun 4 Oct – Sat 10 Oct 2026 are checked. Other drafts are not affected.')).to_have_count(1)
        shot(page, 'b3-rule-line-editor', 'warn', width, theme)
        # The one fix: the line takes the broader line's strength, and then it overrides it.
        state['overrides'] = True
        says.get_by_role('button', name='Make it Mandatory', exact=True).click()
        expect(panel.get_by_role('button', name='Maximum: mandatory', exact=True)).to_be_visible()
        expect(says).to_have_class('rc-says')
        expect(says.locator('.rc-relation')).to_contain_text(f'Overrides line 1 for {name}')
        shot(page, 'b3-rule-line-editor', 'ok', width, theme)
        # Reach counts over the line's own dates.
        panel.locator('.rc-lp-title .rc-reach-link').first.click()
        reach = page.get_by_role('dialog', name=re.compile('this line applies to$'))
        expect(reach).to_contain_text('Sun 4 Oct – Sat 10 Oct 2026')
        shot(page, 'b3-rule-line-editor', 'reach', width, theme)
        page.keyboard.press('Escape')
        expect(reach).to_have_count(0)
        # Inactive clinics stay out of the Where picker, with one quiet way to show them.
        panel.get_by_role('button', name=re.compile('^Clinic: ')).first.click()
        picker = page.locator('.picker')
        expect(picker).to_contain_text('1 inactive clinic not shown')
        expect(picker.get_by_role('option', name=hidden['name'], exact=True)).to_have_count(0)
        shot(page, 'b3-rule-line-editor', 'picker', width, theme)
        picker.get_by_role('button', name='Show', exact=True).click()
        expect(page.locator('.picker').get_by_role('option', name=hidden['name'], exact=True)).to_have_count(1)
    finally:
        context.close()


# Desktop: the line panel docks beside the Fix list.
@pytest.mark.parametrize('width,theme', [v for v in VIEWS if v[0] == 1440], ids=[i for i in IDS if i.startswith('desktop')])
def test_relax_for_a_person_applies_to_this_rota_with_a_justification(browser, live_server, h, width, theme):
    rule, broad, mine, person, name, who, where = world(h)
    w = h.shift(h.workspace('Relax rota'))['workspace']
    w = h.command('coordinator', 'workspace.validate', {'reason': 'Check'}, w['id'], w['revision'])['workspace']
    finding = {'code': 'QUOTA_MAX', 'severity': 'error', 'rule_id': rule['id'], 'person_id': person, 'message': 'Over the weekly maximum',
               'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-11T00:00:00+03:00',
               'window': {'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-11T00:00:00+03:00'}, 'detail': {'line': 0}}

    def with_finding(body):
        body = copy.deepcopy(body)
        body['workspace']['validation']['findings'] = [*body['workspace']['validation'].get('findings', []), finding]
        return body
    seed_line = {**copy.deepcopy(broad), 'id': 'seed-line', 'who': who, 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}
    seed = {'catalogue_id': 'workload_limit', 'catalogue_version': 2, 'expected_policy_id': rule['id'], 'expected_revision': rule['revision'],
            'binding_line_id': broad['id'], 'binding_line_no': 1, 'line': seed_line,
            'lines': [*rule['definition']['rule']['lines'], seed_line], 'needs_approval': False, 'scope': 'rota',
            'adjustment': {'policy_id': rule['id'], 'mode': 'set', 'line_id': broad['id'], 'number': 'max', 'person_id': person,
                           'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}}
    sent, reply = [], {'workspace': h.detail(w)['workspace']}

    def commands(route):
        payload = route.request.post_data_json or {}
        if payload.get('kind') != 'workspace.rule_adjust':
            return route.fallback()
        sent.append(payload)
        route.fulfill(json=reply)
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    try:
        detail = f"/api/workspaces/{w['id']}?view=schedule"
        page.route(f'**{detail}', h.route_json('coordinator', detail, with_finding))
        page.route(f"**/api/policies/{rule['id']}/relax", lambda route: route.fulfill(json=seed))
        effects = []

        def effect(route):  # maintenance adjustment-effect: a preview, nothing saved (drop once the backend lands)
            effects.append(route.request.post_data_json)
            route.fulfill(json={'workspace_id': w['id'], 'revision': w['revision'], 'preview': True, 'committed': False,
                                'effect': {'status': 'ready', 'clears': 1, 'adds': 0, 'effect': 'clears', 'summary': 'Clears 1 check'}})
        page.route(f"**/api/workspaces/{w['id']}/adjustment-effect", effect)
        page.route('**/api/commands', commands)
        # The Fix list's cause (maintenance adjust_rota, one person on the binding line): its one action opens the line panel.
        cause = {'id': 'c-relax', 'level': 'must', 'checks': 1, 'sentence': f'{name} is over the weekly maximum', 'meta': 'Workload limit',
                 'who': [], 'person_ids': [person], 'shift_ids': [], 'safety': False, 'new': False, 'evidence': [],
                 'action': {'kind': 'adjust_rota', 'scope': {'person_ids': [person], 'shift_ids': [], 'starts_on': '2026-10-04',
                            'ends_on': '2026-10-11', 'keep_existing': True}},
                 'rule_lines': [{'rule_id': rule['id'], 'line_id': broad['id'], 'line': 0, 'slot': 'max'}],
                 'score_gain': None, 'finding_keys': [], 'evidence_hash': 'x', 'acknowledgeable': True, 'acknowledgement': None}
        fix = {'workspace_id': w['id'], 'revision': w['revision'], 'fingerprint': 'x', 'causes': [cause], 'review_items': [],
               'summary': {'must': 1, 'could': 0, 'places_filled': 1, 'places_total': 1, 'places_open': 0, 'checks_must': 1,
                           'checks_could': 0, 'fairness_checks': 0, 'acknowledged': 0, 'submit_blocked': True}}
        page.route(f"**/api/workspaces/{w['id']}/fix-list*", lambda route: fix_list_reply(route, fix))
        navigate(page, f"schedule/issues?draft={w['id']}")
        row = page.locator('#main .fix-list > li[data-cause=c-relax]')
        shot(page, 'b4-relax-for-person', 'row', width, theme)
        row.get_by_role('button', name=FIX_ACTION_LABELS['adjust_rota'], exact=True).click()  # the contract's word
        panel = page.locator('.rc-line-panel')
        expect(panel.locator('.rc-says')).to_contain_text(name)
        foot = panel.locator('.rc-lp-foot')
        expect(foot.get_by_role('button', name='Change the department rule…', exact=True)).to_be_visible()
        apply = foot.get_by_role('button', name='Apply to this rota', exact=True)
        with page.expect_response(lambda response: response.url.endswith('/adjustment-effect')
                                  and response.request.post_data_json.get('adjustment', {}).get('amount') == 7):
            panel.get_by_label('Maximum', exact=True).fill('7')
        expect(panel.locator('.rc-effect')).to_contain_text('Relax rota')
        expect(panel.locator('.rc-effect')).to_contain_text('Clears 1 check')
        assert any(e['adjustment']['amount'] == 7 and e['expected_revision'] == w['revision'] for e in effects), effects
        apply.click()
        assert not sent, 'A justification is required first'
        panel.get_by_label('Justification', exact=True).fill('On induction this week')
        shot(page, 'b4-relax-for-person', 'panel', width, theme)
        apply.click()
        expect(panel).to_have_count(0)
        assert sent[0]['target_id'] == w['id'] and sent[0]['payload'] == {**seed['adjustment'], 'amount': 7, 'reason': 'On induction this week'}
    finally:
        context.close()


@pytest.mark.parametrize('on', [False, True], ids=['rule-off', 'rule-on'])
def test_changing_the_department_rule_from_relax_keeps_the_rule_on_or_off(browser, live_server, h, on):
    """A line proposed from the line panel (Change the department rule…) adds that line and keeps the rule as it is: an off
    rule stays off once approved, an on rule stays on. Release blocker: the panel used to send on=true."""
    rule, broad, mine, person, name, who, where = world(h)
    if not on:  # the department switched this rule off: an approved version that is off
        record = propose_rule(h, 'workload_limit', rule['definition']['rule']['lines'], catalogue_version=2, on=False,
                              policy_id=rule['id'], revision=rule['revision'], reason='Off for now')['record']
        rule = h.command('approver', 'policy.activate', {'reason': 'Independent review'}, record['id'], record['revision'])['record']
    assert rule['definition']['rule'].get('on', True) is on
    w = h.shift(h.workspace('Relax rota'))['workspace']
    w = h.command('coordinator', 'workspace.validate', {'reason': 'Check'}, w['id'], w['revision'])['workspace']
    # The next week: the fixture's own person line already covers 4–10 Oct, and the same line twice is refused.
    seed_line = {**copy.deepcopy(broad), 'id': str(uuid.uuid4()), 'who': who, 'starts_on': '2026-10-11', 'ends_on': '2026-10-18'}
    seed = {'catalogue_id': 'workload_limit', 'catalogue_version': 2, 'expected_policy_id': rule['id'], 'expected_revision': rule['revision'],
            'binding_line_id': broad['id'], 'binding_line_no': 1, 'line': seed_line,
            'lines': [*rule['definition']['rule']['lines'], seed_line], 'needs_approval': False, 'scope': 'rota',
            'adjustment': {'policy_id': rule['id'], 'mode': 'set', 'line_id': broad['id'], 'number': 'max', 'person_id': person,
                           'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}}
    proposals = []

    def commands(route):
        payload = route.request.post_data_json or {}
        if payload.get('kind') == 'rule.propose':
            proposals.append(payload['payload'])
        return route.fallback()
    context, page = themed(browser, live_server, 'coordinator', 1440, 'light')
    try:
        page.route(f"**/api/policies/{rule['id']}/relax", lambda route: route.fulfill(json=seed))
        page.route(f"**/api/workspaces/{w['id']}/adjustment-effect", lambda route: route.fulfill(json={
            'workspace_id': w['id'], 'revision': w['revision'], 'preview': True, 'committed': False,
            'effect': {'status': 'ready', 'clears': 1, 'adds': 0, 'effect': 'clears', 'summary': 'Clears 1 check'}}))
        page.route('**/api/commands', commands)
        cause = {'id': 'c-relax', 'level': 'must', 'checks': 1, 'sentence': f'{name} is over the weekly maximum', 'meta': 'Workload limit',
                 'who': [], 'person_ids': [person], 'shift_ids': [], 'safety': False, 'new': False, 'evidence': [],
                 'action': {'kind': 'adjust_rota', 'scope': {'person_ids': [person], 'shift_ids': [], 'starts_on': '2026-10-04',
                            'ends_on': '2026-10-11', 'keep_existing': True}},
                 'rule_lines': [{'rule_id': rule['id'], 'line_id': broad['id'], 'line': 0, 'slot': 'max'}],
                 'score_gain': None, 'finding_keys': [], 'evidence_hash': 'x', 'acknowledgeable': True, 'acknowledgement': None}
        fix = {'workspace_id': w['id'], 'revision': w['revision'], 'fingerprint': 'x', 'causes': [cause], 'review_items': [],
               'summary': {'must': 1, 'could': 0, 'places_filled': 1, 'places_total': 1, 'places_open': 0, 'checks_must': 1,
                           'checks_could': 0, 'fairness_checks': 0, 'acknowledged': 0, 'submit_blocked': True}}
        page.route(f"**/api/workspaces/{w['id']}/fix-list*", lambda route: fix_list_reply(route, fix))
        navigate(page, f"schedule/issues?draft={w['id']}")
        row = page.locator('#main .fix-list > li[data-cause=c-relax]')
        row.get_by_role('button', name=FIX_ACTION_LABELS['adjust_rota'], exact=True).click()
        panel = page.locator('.rc-line-panel')
        panel.get_by_label('Justification', exact=True).fill('A standing exception for this person')  # required in the panel
        panel.locator('.rc-lp-foot').get_by_role('button', name='Change the department rule…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Change Workload limit')
        dialog.get_by_label('Reason', exact=True).fill('A standing exception for this person')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        expect(panel).to_have_count(0)
    finally:
        context.close()
    assert len(proposals) == 1 and proposals[0]['on'] is on, proposals
    before = h.get('approver', '/api/rules/catalogue/workload_limit/detail?version=2')['entry']  # the client reads the detail
    settings = before['rule']['definition']['rule'].get('settings')  # the client's settingsOf: the rule's, else the defaults
    assert proposals[0]['settings'] == (settings if settings is not None else before.get('default_settings') or {})
    assert {l['id'] for l in proposals[0]['lines']} >= {broad['id'], mine['id']}
    proposed = before['proposed']
    assert proposed['definition']['rule'].get('on', True) is on
    active = h.command('approver', 'policy.activate', {'reason': 'Independent review'}, proposed['id'], proposed['revision'])['record']
    assert active['definition']['rule'].get('on', True) is on


def test_relax_is_never_offered_for_a_safety_rule():
    """D7: the menu builder leaves Relax out for a safety line (a mixed rule is judged by the line's own number slots,
    the contracts' NEVER_OVERRIDDEN, as schemas.safety_rule does)."""
    source = (Path(__file__).resolve().parents[1] / 'apps/web/src/rule_catalogue.ts').read_text(encoding='utf-8')
    assert "t.relax_allowed!==false&&!safetyLine(t.policy?.definition?.rule,t.binding_line_id)" in source
    assert "NEVER_OVERRIDDEN.includes(slot.kind)" in source


@pytest.mark.parametrize('width,theme', VIEWS, ids=IDS)
@pytest.mark.parametrize('job_status', ['queued', 'running'])
def test_approver_sees_the_effect_and_approves_and_activates_in_one_step(browser, live_server, h, width, theme, job_status):
    rule, broad, mine, person, name, who, where = world(h)
    added = line({'max': {'value': 4, 'level': 'mandatory'}}, who=who, where=where, measure='duties', period={'kind': 'calendar_week'},
                 starts_on='2027-01-03', ends_on='2027-01-10')
    lines = [*rule['definition']['rule']['lines'], added]
    check = h.client('coordinator').post('/api/rules/catalogue/workload_limit/check', json={'catalogue_version': 2, 'lines': lines}).json()
    proposal = propose_rule(h, 'workload_limit', lines, catalogue_version=2, policy_id=rule['id'], revision=rule['revision'],
                            acknowledged=[o['line_id'] for o in check['overlaps']], reason=f'{name} is on induction')['record']
    from datetime import timedelta
    # The page's clock is the app's pinned one (test_browser: context.clock.install(time=clock_now())): the queue time is
    # two minutes before *that* "now", not the machine's, or it would lie in the page's future and read 0:00.
    queued_at = (now() - timedelta(minutes=2)).isoformat()
    effect = {'workspace_id': 'w1', 'name': 'Rota 11–17 Oct', 'revision': 1, 'job_id': None,
              'status': job_status, 'created_at': queued_at, 'started_at': NOW if job_status == 'running' else None}
    review = {'policy_id': proposal['id'], 'relations': {added['id']: relation(broad, True)}, 'effect': [effect], 'ready': False}
    decisions = []
    context, page = themed(browser, live_server, 'approver', width, theme)
    try:
        page.route(f"**/api/policies/{proposal['id']}/review", lambda route: route.fulfill(json=review))
        page.on('request', lambda r: decisions.append(r.post_data_json) if r.method == 'POST' and r.url.endswith('/api/commands')
                and r.post_data_json['kind'] == 'policy.activate' else None)
        navigate(page, 'rules/workload_limit')
        main = page.locator('#main')
        expect(main.locator('.rc-proposed').get_by_text('Proposed', exact=True)).to_be_visible()
        main.locator('.rc-waiting').get_by_role('button', name='Review and approve', exact=True).click()
        panel = page.get_by_role('region', name=f'Add a line for {name}')
        expect(panel.locator('.rc-relation')).to_contain_text('Overrides line 1')
        expect(panel.locator('.rc-quote')).to_have_text(f'{name} is on induction')
        approve = panel.get_by_role('button', name='Approve and activate', exact=True)
        expect(panel.locator('.rc-effect')).to_contain_text('Waiting to check · ' if job_status == 'queued' else 'Checking · ')
        if job_status == 'queued':
            expect(panel.locator('.elapsed-time')).to_have_text(re.compile(r'(?:[2-9]|[1-9]\d+):[0-5]\d'))
            with page.expect_response(lambda response: response.url.endswith(f"/policies/{proposal['id']}/review")):
                page.clock.run_for(3500)  # the next poll (the panel's 3 s timer, on the page clock); its redraw must retain the original queue time
            expect(panel.locator('.elapsed-time')).to_have_text(re.compile(r'(?:[2-9]|[1-9]\d+):[0-5]\d'))
        expect(approve).to_be_disabled()
        shot(page, 'b5-approve-rule-line', 'checking', width, theme)
        # The effect is read again until it is known; then Approve and activate is one step, with no checkbox.
        review['effect'] = [{**effect, 'status': 'ready', 'clears': 1, 'adds': 0, 'effect': 'clears'}]
        review['ready'] = True
        expect(panel.locator('.rc-effect')).to_contain_text('Clears 1 check', timeout=15000)
        expect(approve).to_be_enabled()
        expect(panel.get_by_role('checkbox')).to_have_count(0)
        shot(page, 'b5-approve-rule-line', 'checked', width, theme)
        approve.click()
        expect(main.locator('.rc-waiting')).to_have_count(0)
        assert decisions[-1]['target_id'] == proposal['id'] and decisions[-1]['payload'] == {'check_job_ids': []}  # no invented reason (maintenance)
        active = next(e['rule'] for e in h.get('approver', '/api/rules/catalogue?version=2&include_hidden=true')['entries'] if e['id'] == 'workload_limit')
        assert active['id'] == proposal['id'] and active['status'] == 'active'
    finally:
        context.close()


@pytest.mark.skipif(not os.getenv('FEB_R_SHOTS'), reason='Review screenshots only (the behaviour is in test_browser_self_approval.py)')
@pytest.mark.parametrize('width,theme', VIEWS, ids=IDS)
def test_access_and_approvals_review_shots(browser, live_server, h, width, theme):
    context, page = themed(browser, live_server, 'admin', width, theme)
    try:
        navigate(page, 'access/routes')
        urgent = page.locator('.urgent-route')
        expect(urgent).to_contain_text('Urgent is not offered')
        urgent.scroll_into_view_if_needed()
        shot(page, 'access-approvals', 'urgent', width, theme)
        urgent.get_by_role('button', name='Set up…', exact=True).click()
        form = page.get_by_role('dialog', name='Propose review route', exact=True)
        form.get_by_role('group', name='Urgent amendment steps').scroll_into_view_if_needed()
        shot(page, 'access-approvals', 'urgent-route-form', width, theme)
        form.get_by_role('button', name='Cancel', exact=True).click()
        page.get_by_role('button', name='Add permission', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add a self-approval permission')
        expect(dialog.get_by_label('Until (included)', exact=True)).not_to_have_value('')
        shot(page, 'access-approvals', 'self-approval-grant', width, theme)
    finally:
        context.close()

"""The auto-schedule result in Fix-list terms (mockups A3/A4; owner decisions D1, D3, D9): better or worse in must-fix
causes, a proven clash of two rules with only the governed choices the server offers (never a cap change for a safety
kind), and a proposal the rota changed under, checked again, applying only what still fits.

The backend contracts (maintenance fix_list_before/after, maintenance conflict, maintenance currency/reconciliation) are injected into the
job read as their contracts describe them, so the rendering is checked before those branches are merged."""
import json
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, route_fetch
from .test_browser_autoschedule_review import _open, _run, FOUR, DESKTOP
from .browser_acceptance_support import shot

pytestmark = pytest.mark.browser
SUMMARY = {'checked_at': '2026-11-02T09:00:00Z', 'places_filled': 509, 'places_total': 516, 'could': 5}
CLASH_CAUSE = {'id': 'cause-clash', 'level': 'must', 'checks': 20, 'sentence': 'Weekend Urgent Care needs more seniors than the caps allow',
               'meta': '7 places open · rules clash', 'action': {'kind': 'clash', 'scope': {}}, 'evidence': None, 'score_gain': None,
               'rule_lines': [], 'finding_keys': [], 'acknowledgeable': False}
RULES = [{'kind': 'policy', 'rule_id': 'rule-kind', 'revision': 1, 'name': 'People of a kind per session', 'line': 6, 'slot': 'minimum',
          'rule_kind': 'requirement', 'safety': True, 'person_ids': [], 'session_ids': [], 'starts_on': '2026-12-04', 'ends_on': '2026-12-19'},
         {'kind': 'policy', 'rule_id': 'rule-cap', 'revision': 1, 'name': 'Duties per period', 'line': 30, 'slot': 'maximum',
          'rule_kind': 'block_quota', 'safety': False, 'person_ids': ['p1'], 'session_ids': [], 'starts_on': '2026-12-01', 'ends_on': '2026-12-31'}]
CONFLICT = {'strict_status': 'INFEASIBLE', 'explanation_status': 'INFEASIBLE', 'proven': True, 'code': 'CONFLICTING_REQUIREMENTS',
            'message': 'These requirements cannot all be met.', 'requirements': RULES,
            'causes': [{'key': 'k', 'message': 'These requirements cannot all be met.', 'requirements': RULES,
                        'evidence': {'required_places': 24, 'maximum_possible': 23, 'complete': True,
                                     'message': '12 weekend shifts need 24 senior places; at 2 each, 23 are possible.'},
                        'decisions': [{'kind': 'open_rule', 'rule_id': 'rule-kind', 'line': 6},
                                      {'kind': 'open_rule', 'rule_id': 'rule-cap', 'line': 30},
                                      {'kind': 'propose_cap_change', 'rule_id': 'rule-cap', 'line_id': 'line-31', 'line': 30, 'slot': 'maximum', 'person_ids': ['p1'],
                                       'starts_on': '2026-12-01', 'ends_on': '2026-12-31', 'requires_approval': True},
                                      # never offered: the server must not send it for a safety kind, and the client refuses it too (D7)
                                      {'kind': 'propose_cap_change', 'rule_id': 'rule-kind', 'line': 6, 'person_ids': ['p1'], 'requires_approval': True},
                                      {'kind': 'review_eligibility', 'person_ids': ['p1', 'p2'], 'session_ids': ['s1']}]}]}


def _inject(page, extra):
    """Every single-job read carries `extra(job)` merged in (the contract fields the backend branches add)."""
    def edit(route):
        reply = route_fetch(route)
        body = reply.json()
        job = body.get('job') or {}
        if job.get('kind') == 'generation' and job.get('result'):
            extra(job)
        route.fulfill(response=reply, body=json.dumps(body))
    page.route(re.compile(r'/api/jobs/[^/?]+$'), edit)


def _workspace(h, name):
    w = h.shift(h.workspace(name), start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    return h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[])['workspace']


@pytest.mark.parametrize('view,scheme', FOUR)
def test_a_better_run_says_causes_cleared_and_what_is_still_to_fix(browser, live_server, h, view, scheme):
    w = _workspace(h, 'FM better run')
    context, page, panel = _open(browser, live_server, h, w, view, scheme)
    try:
        _inject(page, lambda job: job['result'].update(
            fix_list_before={'summary': {**SUMMARY, 'must': 3, 'places_open': 7}},
            fix_list_after={'summary': {**SUMMARY, 'must': 1, 'places_open': 7}, 'causes': [CLASH_CAUSE]},
            open_places_before=7, open_places_after=7))
        _run(page, h, panel)
        review = panel.get_by_role('region', name='Review')
        expect(review.get_by_role('status').first).to_have_text(re.compile(r'^Better: 2 of 3 must-fix causes cleared.*Still to fix: Weekend Urgent Care needs more seniors'), timeout=15000)
        stats = review.get_by_role('group', name='Result')
        expect(stats.locator('.auto-stat').filter(has_text='Must fix')).to_have_text(re.compile(r'Must fix\s*1\s*−2 from 3'))
        expect(stats.locator('.auto-stat').filter(has_text='Open places')).to_contain_text('±0')
        expect(stats).not_to_contain_text('Blocking')
        still = review.get_by_role('region', name='Still to fix')
        expect(still.get_by_role('button', name='See the clash', exact=True)).to_be_visible()
        expect(panel.locator('.auto-foot .button.primary')).to_have_text('Apply 2 changes')  # one primary
        shot(page, f"fe-a-a3-better-{view['width']}-{scheme}")
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_worse_run_makes_discard_the_primary(browser, live_server, h):
    w = _workspace(h, 'FM worse run')
    context, page, panel = _open(browser, live_server, h, w, DESKTOP, 'light')
    try:
        _inject(page, lambda job: job['result'].update(
            fix_list_before={'summary': {**SUMMARY, 'must': 3, 'places_open': 7}},
            fix_list_after={'summary': {**SUMMARY, 'must': 4, 'places_open': 8}, 'causes': []},
            open_places_before=7, open_places_after=8))
        _run(page, h, panel)
        review = panel.get_by_role('region', name='Review')
        expect(review.get_by_role('status').first).to_contain_text('Worse: 1 new must-fix cause', timeout=15000)
        expect(panel.locator('.auto-foot .button.primary')).to_have_text('Discard')
        expect(panel.get_by_role('button', name='Apply 2 changes', exact=True)).not_to_have_class(re.compile(r'\bprimary\b'))
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def _clash(people):
    """CONFLICT with real people: two cap decisions (one per person) and the people whose eligibility can be reviewed."""
    a, b = people[:2]
    conflict = json.loads(json.dumps(CONFLICT))
    decisions = conflict['causes'][0]['decisions']
    for d in decisions:
        d['person_ids'] = [a if x == 'p1' else b for x in d.get('person_ids', [])]
    cap = next(d for d in decisions if d['kind'] == 'propose_cap_change' and d['rule_id'] == 'rule-cap')
    decisions.append({**cap, 'person_ids': [b]})
    next(d for d in decisions if d['kind'] == 'review_eligibility')['person_ids'] = [a, b]
    return conflict


@pytest.mark.parametrize('view,scheme', FOUR)
def test_a_clash_relaxes_the_cap_for_this_rota_inline_with_its_effect(browser, live_server, h, view, scheme):
    """R4 (D1, D3, D7, D16): the non-safety cap is relaxed for this rota inline, people ranked by fit, a required reason
    and the effect shown before anything applies; no "Keep the gaps and submit" for a safety rule, said once why."""
    w = _workspace(h, 'FM clash run')
    a, b = h.seed['people'][:2]
    conflict = _clash([a, b])
    context, page, panel = _open(browser, live_server, h, w, view, scheme)
    seeds = []

    def seed(route):
        sent = json.loads(route.request.post_data)
        seeds.append(sent)
        route.fulfill(json={'scope': 'rota', 'needs_approval': False, 'adjustment': {'policy_id': 'rule-cap', 'line_id': 'line-31', 'number': 'max',
            'person_id': sent['person_id'], 'starts_on': '2026-12-01', 'ends_on': '2027-01-01', 'mode': 'set', 'amount': 2}})

    def effect(route):  # maintenance adjustment-effect: nothing saved; with fit_person_ids, the server's ranking
        sent = json.loads(route.request.post_data)
        fit = [{'person_id': b, 'display_name': 'B', 'eligible_open_places': 3, 'shift_ids': [], 'cleared_checks': 20, 'new_must_checks': 0},
               {'person_id': a, 'display_name': 'A', 'eligible_open_places': 2, 'shift_ids': [], 'cleared_checks': 20, 'new_must_checks': 0}]
        route.fulfill(json={'revision': w['revision'], 'preview': True, 'committed': False,
            'effect': {'status': 'ready', 'clears': 20, 'adds': 0, 'effect': 'clears', 'summary': ''},
            **({'ranked_fit': {'candidates': fit, 'excluded': [], 'complete': True, 'optimal': False, 'basis': ''}} if sent.get('fit_person_ids') else {})})
    try:
        page.route(re.compile(r'/api/policies/[^/]+/relax$'), seed)
        page.route(re.compile(r'/api/workspaces/[^/]+/adjustment-effect$'), effect)
        # A proven clash leaves places open: its candidate has gaps (isClash reads a valid candidate as no clash).
        _inject(page, lambda job: job['result'].update(conflict=conflict,
            generation_outcome={**(job['result'].get('generation_outcome') or {}), 'candidate': 'with_gaps'},
            fix_list_before={'summary': {**SUMMARY, 'must': 3, 'places_open': 7}},
            fix_list_after={'summary': {**SUMMARY, 'must': 1, 'places_open': 7}, 'causes': [CLASH_CAUSE]},
            open_places_before=7, open_places_after=7))
        _run(page, h, panel)
        review = panel.get_by_role('region', name='Review')
        expect(review.get_by_role('status').first).to_have_text(re.compile(r'^Two rules can’t both be met.*7 places stay open'), timeout=15000)
        clash = review.get_by_role('region', name='Clash')
        expect(clash.locator('.clash-rule')).to_have_count(2)
        expect(clash.locator('.clash-rule').first).to_contain_text('line 7')  # zero-based line 6, said as people count
        expect(clash).to_contain_text('24 places needed, 23 possible.')
        relax = clash.locator('li.relax')
        expect(relax).to_have_count(1)  # the non-safety maximum only (D7): never the safety rule
        expect(relax).to_contain_text('≤ 3 instead of ≤ 2 for one person')
        radios = relax.get_by_role('radio')
        expect(radios).to_have_count(2)
        expect(radios.first).to_be_checked()  # best fit first
        expect(relax.locator('.relax-person').first).to_contain_text('3 open places they can take · clears 20')
        effect = relax.get_by_role('status')
        expect(effect).to_have_text('Clears 20 checks · no new must fix · approved with the rota')
        go = relax.get_by_role('button', name='Relax for this rota', exact=True)
        expect(go).to_be_disabled()  # a reason is required
        relax.get_by_label('Reason').fill('One extra weekend covers December’s 24 senior places.')
        expect(go).to_be_enabled()
        expect(clash.get_by_role('button', name='Who can work it', exact=True)).to_be_visible()  # the server's review_eligibility people
        expect(clash).to_contain_text('A permanent change goes through approval')
        expect(clash).not_to_contain_text('Keep the')  # minimum cover is a safety kind (D3)
        expect(clash.locator('.clash-safety')).to_have_text('People of a kind per session is a safety rule, so these places can’t be submitted open.')
        expect(panel).not_to_contain_text('Pending owner decision')
        expect(review.get_by_role('group', name='Result').locator('.auto-stat').filter(has_text='Changes')).to_be_visible()
        shot(page, f"fe-a-r4-clash-relax-{view['width']}-{scheme}")
        if view['width'] > 400 and scheme == 'light':
            assert seeds[-1]['person_id'] == b and seeds[-1]['binding_line_id'] == 'line-31'
            go.click()
            # A relax is a draft edit: the waiting proposal asks first, as every draft edit does.
            with page.expect_request(lambda r: r.method == 'POST' and '/api/commands' in r.url) as sent:
                page.get_by_role('dialog', name='Edit the draft now?').get_by_role('button', name='Edit anyway', exact=True).click()
            body = json.loads(sent.value.post_data)
            payload = body.get('payload', body)
            assert body.get('kind') == 'workspace.rule_adjust'
            assert payload == {'policy_id': 'rule-cap', 'line_id': 'line-31', 'number': 'max', 'person_id': b, 'starts_on': '2026-12-01',
                               'ends_on': '2027-01-01', 'mode': 'set', 'amount': 3, 'reason': 'One extra weekend covers December’s 24 senior places.'}
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('view,scheme', FOUR)
def test_inputs_changed_during_the_run_applies_what_still_fits(browser, live_server, h, view, scheme):
    w = _workspace(h, 'FM changed run')
    person = h.seed['people'][0]

    def changed(job):
        job['currency'] = 'revalidation_required'
        additions = job['result'].get('additions') or []
        job['reconciliation'] = {'status': 'partial', 'addition_indexes': [0], 'removal_indexes': [],
            'conflicting_placements': [{'shift_id': additions[1]['shift_id'], 'addition_indexes': [1], 'removal_indexes': [], 'code': 'MANDATORY_CONFLICT', 'findings': []}],
            'changed_facts': [{'kind': 'wish', 'change': 'added', 'person_id': person, 'person_name': 'Felix Parker',
                               'starts_on': '2026-10-06', 'ends_on': '2026-10-06', 'actor': {'id': 'x', 'display_name': 'Felix Parker'}}],
            'changed_fact_count': 1}
    context, page, panel = _open(browser, live_server, h, w, view, scheme)
    try:
        _inject(page, changed)
        _run(page, h, panel)
        review = panel.get_by_role('region', name='Review')
        expect(review.get_by_role('status').first).to_have_text(re.compile(r'^The rota changed during the run'), timeout=15000)
        expect(review.get_by_role('list', name='What changed')).to_have_text('Felix Parker added a wish: Tue 6 Oct 2026')
        expect(review).to_contain_text('1 of 2 changes still fit')
        expect(review).to_contain_text('1 is left out')
        expect(review).not_to_contain_text('Ready to apply')  # one outcome: what still fits, never the stale proposal's
        expect(panel.get_by_role('button', name='Run again with the changes', exact=True)).to_be_visible()
        apply = panel.locator('.auto-foot .button.primary')
        expect(apply).to_have_text('Apply 1 change')
        shot(page, f"fe-a-a4-changed-{view['width']}-{scheme}")
        if view['width'] > 400 and scheme == 'light':
            with page.expect_request(lambda r: r.method == 'POST' and '/api/commands' in r.url) as sent:
                apply.click()
            payload = json.loads(sent.value.post_data)
            body = payload.get('payload', payload)
            assert body['addition_indexes'] == [0] and body['removal_indexes'] == []
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

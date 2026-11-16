"""The Fix list reviewed: one server object drawn on its page, the
footer, Home, Submit and the approver's review, with the same counts everywhere (causes, never findings).

The server object is the maintenance contract (GET /api/workspaces/{id}/fix-list, `fix_list` on rota summaries and Home items).
These tests give the page department-scale shapes of that contract through page.route, so they check what the client
draws from it (nothing here is counted by the client); test_browser_fix_list_counts.py and the real-adjustment review
below read the real server. Coordinator and approver, 1440 light and 390 dark."""
import json
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_rota, route_fetch, toolbar, condition, fix_list_reply  # noqa: F401
from .test_browser import fix_pill

pytestmark = pytest.mark.browser
SHOTS = os.getenv('FIX_LIST_SHOTS')


def shot(page, name, view=None, scheme=''):
    """FIX_LIST_SHOTS=<dir> keeps a picture of each screen for comparing with the approved mockups."""
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        size = 'phone' if view and view['width'] < 500 else 'desktop'
        page.screenshot(path=str(Path(SHOTS) / f'{name}-{size}-{scheme or "light"}.png'), full_page=size == 'phone')


VIEWS = [pytest.param({'width': 1440, 'height': 900}, 'light', id='1440-light'),
         pytest.param({'width': 390, 'height': 844}, 'dark', id='390-dark')]


def cause(i, level, checks, sentence, meta, kind, acknowledgeable=True, **extra):
    return {'id': f'c{i}', 'level': level, 'checks': checks, 'sentence': sentence, 'meta': meta,
            'action': {'kind': kind, 'scope': {'person_ids': [], 'shift_ids': [], 'starts_on': '2026-10-04',
                                               'ends_on': '2026-10-10', 'keep_existing': True}},
            'who': [], 'person_ids': [], 'shift_ids': [], 'safety': False, 'new': False,
            # The server's presentation (fix_list_facts.py): the plain line, and no session to assign unless a test says so.
            'detail_line': meta, 'session_findings': {},
            'evidence': [], 'score_gain': None, 'score_gain_estimated': True, 'rule_lines': [], 'finding_keys': [f'k{i}'], 'evidence_hash': f'h{i}',
            'acknowledgeable': acknowledgeable, 'acknowledgement': None, **extra}


def fix_list(workspace_id, must=True, acknowledged=False, safety=False, rank=None, shift=None):
    causes = []
    if must:
        causes += [
            cause(1, 'must', 53, 'Six interns are short of clinic time', '6 interns · 4–10 Oct · Duties per week',
                  'autoschedule_scoped', evidence=[{'code': 'RULE', 'person_id': 'p-jules', 'person_name': 'Jules Foster', 'shift_id': None,
                                                     'start': '2026-10-04T00:00:00Z', 'shift_ids': [], 'detail': {'required': 6, 'scheduled': 4},
                                                     'explanation': 'Below 6 session units a week.'}]),
            cause(2, 'must', 20, 'Weekend Urgent Care needs more seniors than the caps allow', '7 places open · rules clash',
                  'clash', acknowledgeable=not safety, safety=safety, new=True,
                  who=[{'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [rank]}]}] if rank else []),
            cause(3, 'must', 4, 'Two R1 residents are under their October minimum', 'Duties per month ≥ 4',
                  'autoschedule_scoped')]
        if shift:
            causes[1]['action']['scope']['shift_ids'] = [shift]
        if acknowledged:
            for c, why in zip(causes, ['Look-back week; programme lead agreed.', 'Cap change proposed today.', 'Both start late.']):
                c['acknowledgement'] = {'cause_id': c['id'], 'reason': why, 'at': '2026-10-02T07:14:00Z'}
    causes += [cause(4, 'could', 198, 'Training targets are under or over in 61 sessions', 'Companion cover', 'autoschedule_scoped',
                     score_gain=2.6),
               cause(5, 'could', 91, 'Workload is uneven across 32 people', 'Fair share', 'rebalance', score_gain=4.1)]
    causes += [cause(10 + i, 'could', 6 - i, f'Target {i + 1} is one duty off', 'Duties per period', 'rebalance') for i in range(5)]
    m = sum(c['level'] == 'must' for c in causes)
    return {'workspace_id': workspace_id, 'revision': 1, 'fingerprint': 'x', 'causes': causes,
            'summary': {'must': m, 'could': len(causes) - m, 'checked_at': '2026-10-02T07:00:00Z', 'places_filled': 509,
                        'places_total': 516, 'places_open': 7, 'checks_must': 77, 'checks_could': 289, 'fairness_checks': 91,
                        'acknowledged': 3 if acknowledged else 0, 'submit_blocked': bool(m),
                        'score': None if m else 71.4, 'uncapped_score': 63.8 if m else 71.4, 'since': {'cleared': 6, 'new': 2}},
            'review_items': [{'id': f'gap-{c["id"]}', 'kind': 'acknowledged_gap', 'reason': c['acknowledgement']['reason'],
                              'source': {'cause_id': c['id']}} for c in causes if c.get('acknowledgement')]}


def serve(page, workspace_id, population_note=None, **options):
    """The maintenance shapes: the Fix list itself, and the same object on the rota list's summaries and on Home's draft row."""
    body = fix_list(workspace_id, **options)
    if population_note:
        body['summary']['population_note'] = population_note
    page.route(f'**/api/workspaces/{workspace_id}/fix-list*', lambda r: fix_list_reply(r, body))

    def rotas(route):
        reply = route_fetch(route)
        if reply.status == 304 or not reply.body():
            return route.fulfill(response=reply)
        data = reply.json()
        for w in data.get('workspaces', []):
            if w.get('id') == workspace_id:
                w.setdefault('health', {}).setdefault('summary', {})['fix_list'] = body
        route.fulfill(response=reply, body=json.dumps(data))
    page.route(re.compile(r'.*/api/workspaces\?.*'), rotas)

    def home(route):
        reply = route_fetch(route)
        if reply.status == 304 or not reply.body():
            return route.fulfill(response=reply)
        data = reply.json()
        data['attention'] = [{'key': 'blocking', 'count': 77, 'rota_id': workspace_id, 'rota_name': 'Fix list rota',
                              'rotas': 1, 'fix_list': {'summary': body['summary']}}] + [
            i for i in data.get('attention', []) if i.get('key') not in ('blocking', 'open_places')]
        route.fulfill(response=reply, body=json.dumps(data))
    page.route(re.compile(r'.*/api/home$'), home)

    def replies(route):  # a draft edit's (or check's) reply carries the rota's health with its Fix list too
        reply = route_fetch(route)
        try:
            data = reply.json()
        except ValueError:
            return route.fulfill(response=reply)
        if isinstance(data.get('health'), dict) and data.get('workspace', {}).get('id') == workspace_id:
            data['health'].setdefault('summary', {})['fix_list'] = body
            if isinstance(data.get('summary'), dict):
                data['summary']['fix_list'] = body
        route.fulfill(response=reply, body=json.dumps(data))
    page.route(re.compile(r'.*/api/commands$'), replies)
    page.route(re.compile(r'.*/api/workspaces/' + re.escape(workspace_id) + r'(\?.*)?$'), replies)
    # The page signed in before these routes: Home's reads (the rota list among them) may still be in flight and a later
    # read joins them. Read everything again through the routes.
    page.reload()
    return body


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_fix_list_page_two_levels_one_action_each_and_evidence_behind_details(browser, live_server, h, view, scheme):
    w = h.shift(h.workspace('Fix list page'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        serve(page, w['id'])
        navigate(page, f'schedule/issues?draft={w["id"]}')
        main = page.locator('#main .fix-page')
        gate = main.locator('.fix-gate')
        expect(gate).to_contain_text('3 must fix before you submit · 509 of 516 places filled')
        expect(gate.locator('.fix-since')).to_have_text('Today: 6 cleared · 2 new')  # SW3-A-22: in words, on screen
        expect(page.locator('#main')).not_to_contain_text('Best on a desktop')  # D10: phones read and act on it
        # A1: the rota's bar on top (its rota switcher, its stage, its next steps on a desktop) and its health below.
        bar = page.locator('.topbar .planner-bar')
        expect(bar.get_by_role('button', name='Switch draft · Fix list page', exact=True)).to_be_visible()
        expect(bar.locator('.rota-status')).to_contain_text('Draft')
        expect(bar.get_by_role('button', name='Submit for review…')).to_have_count(1 if view['width'] > 400 else 0)
        expect(fix_pill(page, 'err')).to_contain_text('3')
        expect(fix_pill(page, 'could')).to_contain_text('7')  # the D78 gold circle (UIUX C3)
        must, could = main.locator('section[data-level=must]'), main.locator('section[data-level=could]')
        expect(must.get_by_role('heading', name='Must fix')).to_be_visible()
        expect(must.locator('.fix-level .badge')).to_have_text('3')
        expect(could.locator('.fix-level .badge')).to_have_text('7')
        # Ranked as the server ranks them; the level is a mark; the line under the sentence is the server's plain one, never
        # the scorer's units (FIXLIST-UX: no checks counts or score numbers on rows).
        expect(must.locator('.fix-row strong')).to_contain_text(['Six interns are short of clinic time',
                                                              'Weekend Urgent Care needs more seniors than the caps allow',
                                                              'Two R1 residents are under their October minimum'])
        expect(must.locator('.fix-mark').first).to_have_accessible_name('Must fix')
        expect(must.locator('.fix-row').first.locator('.fix-meta')).to_have_text('6 interns · 4–10 Oct · Duties per week')
        expect(main.locator('.fix-row').filter(has_text=re.compile(r'\d+ checks?\b'))).to_have_count(0)
        expect(must.locator('.fix-row').nth(1).locator('.fix-tag')).to_have_text('New')
        # One primary per surface: two causes one scoped run can take are one "Auto-schedule 2 of these" (R1).
        expect(main.locator('.button.primary')).to_have_count(1)
        expect(must.locator('.fix-level .button.primary')).to_have_text('Auto-schedule 2 of these')
        expect(must.get_by_role('button', name='See the clash')).to_be_visible()
        expect(could.locator('.fix-row').nth(1)).not_to_contain_text('score')
        # Could improve: the top five, then the rest folded (Must fix always in full).
        expect(could.locator('.fix-row')).to_have_count(5)
        expect(could.locator('.fix-tail')).to_contain_text('2 more')
        expect(could.locator('.fix-tail')).not_to_contain_text('checks')
        could.get_by_role('button', name='Show all 7').click()
        expect(could.locator('.fix-row')).to_have_count(7)
        # "Show evidence" (a worded toggle, its cause by aria-describedby) shows the evidence, keyboard reachable.
        details = must.locator('li[data-cause]', has_text='Six interns are short of clinic time').locator('.fix-more')
        expect(details).to_have_accessible_name('Show evidence')
        expect(details).to_have_attribute('aria-expanded', 'false')
        expect(details).to_have_accessible_description(re.compile('Six interns are short of clinic time'))
        details.focus()
        page.keyboard.press('Enter')
        expect(must.locator('.fix-detail').first).to_be_visible()
        expect(details).to_have_attribute('aria-expanded', 'true')
        expect(details).to_have_text('Hide evidence')
        expect(must.locator('.fix-detail').first).to_contain_text('Below 6 session units a week.')
        expect(must.locator('.fix-detail tbody tr').first).to_contain_text(re.compile(r'Jules Foster.*required 6 · scheduled 4'))
        shot(page, 'a1-fix-list', view, scheme)
        # Search and Filter narrow the rows; nothing is recounted.
        main.get_by_role('searchbox', name='Search the fix list').fill('workload')
        expect(main.locator('.fix-list>li:visible')).to_have_count(1)
        main.get_by_role('searchbox', name='Search the fix list').fill('')
        page.keyboard.press('Enter')
        expect(main.locator('.fix-list>li:visible')).to_have_count(10)
        if view['width'] > 400:
            main.get_by_role('button', name=re.compile('^Saved views')).click()
            page.get_by_role('menuitemradio', name=re.compile('^All checks')).click()
            expect(page.locator('#main .issues-head h2')).to_have_text('All checks · Fix list page')
        # A1: the switcher lists the live rotas (another one opens its Fix list) and leads back to this rota's schedule.
        navigate(page, f'schedule/issues?draft={w["id"]}')
        bar.get_by_role('button', name='Switch draft · Fix list page', exact=True).click()
        drafts = page.get_by_role('dialog', name='Drafts', exact=True)
        expect(drafts.get_by_role('option', name=re.compile('^Fix list page'))).to_have_attribute('aria-selected', 'true')
        drafts.get_by_role('button', name='Open its schedule', exact=True).click()
        expect(page).to_have_url(re.compile(r'#schedule/(?!issues).*edit=' + w['id']))
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_cause_action_opens_auto_schedule_scoped_to_its_people_and_dates(browser, live_server, h):
    w = h.shift(h.workspace('Fix list action'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        serve(page, w['id'])
        navigate(page, f'schedule/issues?draft={w["id"]}')
        page.locator('#main .fix-level .button.primary').click()  # Auto-schedule 2 of these
        expect(page.locator('#auto-panel')).to_be_visible()
        expect(page).to_have_url(re.compile(f'edit={w["id"]}'))
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_footer_says_must_fix_and_could_improve_and_its_popover_is_the_fix_list_head(browser, live_server, h, view, scheme):
    w = h.shift(h.workspace('Fix list footer'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        serve(page, w['id'], population_note="Checked for: 144 people in the applied planning population; 6 people outside this rota's population")
        open_rota(page, w['id'])
        pills = page.locator('.app-footer .fix-pills')
        must = pills.get_by_role('button', name=re.compile('^3 must fix'))
        expect(must).to_be_visible()
        expect(pills.get_by_role('button', name='7 could improve')).to_be_visible()
        # Owner, 7 Nov 2026: the score shows while anything must be fixed too, capped (the must-fix pill gives the count).
        expect(pills.locator('.score-pill .score-blocking')).to_have_text('· capped')
        must.click()
        pop = page.locator('.popover.fix-pop')
        expect(pop.locator('.popover-head')).to_contain_text('Rota health')
        note = pop.get_by_text("Checked for: 144 people in the applied planning population; 6 people outside this rota's population", exact=True)
        expect(note).to_have_count(1)
        expect(note).to_be_visible()
        # The one issue list (live check F6): one item per cause with its level mark, no buttons that do the work.
        expect(pop.locator('.iss-row')).to_have_count(3)
        expect(pop.locator('.iss-row .button')).to_have_count(0)  # the head reads; the work is on the Fix list
        expect(pop).not_to_contain_text('Score shows once')
        shot(page, 'a2-footer-health', view, scheme)
        pop.get_by_role('button', name=re.compile(r'^Open all in Fix list \(\d+\)$')).click()
        if view['width'] > 720:  # FIXLIST-UX: beside the schedule on a desktop; the page on a phone (D10)
            expect(page.locator('.dock-panel.fix-side')).to_be_visible()
            expect(page.locator('#main .wk-grid, #main .planner-main').first).to_be_visible()
        else:
            expect(page.locator('#main .fix-page')).to_be_visible()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_ready_rota_shows_its_score_and_what_could_improve(browser, live_server, h):
    w = h.shift(h.workspace('Fix list ready'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        serve(page, w['id'], must=False)
        open_rota(page, w['id'])
        ready = fix_pill(page, 'ok')
        expect(ready).to_contain_text('Ready · score 71.4')
        ready.click()
        pop = page.locator('.popover.fix-pop')
        expect(pop.locator('.fix-gate')).to_contain_text('Nothing must be fixed · ready to submit')
        expect(pop.locator('.iss-row.warning')).to_have_count(5)
        shot(page, 'a7-health-ready', {'width': 1440})
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_home_lists_the_draft_by_its_must_fix_causes_with_fix_list(browser, live_server, h, view, scheme):
    w = h.shift(h.workspace('Fix list home'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        serve(page, w['id'])
        page.reload()
        row = page.locator('[data-need=blocking]')
        expect(row.locator('.home-count')).to_have_text('3')
        expect(row).to_contain_text('Must fix before submitting')
        shot(page, 'a2-home', view, scheme)
        row.get_by_role('button', name='Fix list', exact=True).click()
        if view['width'] > 720:
            expect(page.locator('.dock-panel.fix-side')).to_be_visible()
            expect(page.locator('#main .planner-main')).to_be_visible()
        else:
            expect(page.locator('#main .fix-page')).to_be_visible()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def submit_dialog(page):
    page.get_by_role('button', name=re.compile(r'^Submit for review')).first.click()
    dialog = page.get_by_role('dialog', name=re.compile('^Submit .* for review$'))
    expect(dialog).to_be_visible()
    return dialog


def test_submit_reads_the_fix_list_and_may_carry_acknowledged_gaps_with_a_reason_each(browser, live_server, h):
    w = h.shift(h.workspace('Fix list submit'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        serve(page, w['id'])
        open_rota(page, w['id'])
        dialog = submit_dialog(page)
        # Sessions · Places filled · Could improve, all from the Fix list summary (SW-21; no recounted tiles), and no Score
        # while anything must be fixed (A7).
        expect(dialog.locator('.summary-item')).to_have_count(3)
        expect(dialog.locator('.summary-item').nth(1)).to_contain_text('509 of 516')
        expect(dialog.locator('.summary-item').nth(2)).to_have_text(re.compile(r'^Could improve\s*7$'))
        expect(dialog.locator('.summary-item').filter(has_text='Score')).to_have_count(0)
        expect(dialog.locator('.fix-gate')).to_contain_text('3 causes must be fixed first')
        expect(dialog.locator('.fix-row')).to_have_count(3)
        expect(dialog.locator('.fix-row .button')).to_have_count(0)
        submit = dialog.locator('button[type=submit]')
        expect(submit).to_be_disabled()
        shot(page, 'a5-submit', {'width': 1440})
        dialog.get_by_label('Submit with these gaps acknowledged').check()
        expect(submit).to_have_text('Submit with 3 gaps')
        expect(submit).to_be_enabled()
        reasons = dialog.get_by_label('Why submit with this gap')
        expect(reasons).to_have_count(3)
        for i in range(3):
            reasons.nth(i).fill(f'Reason {i + 1} agreed')
        shot(page, 'a5-submit-with-gaps', {'width': 1440})
        submit.click()  # press twice: it arms first
        expect(submit).to_have_class(re.compile('armed'))
        with page.expect_request(lambda r: r.url.endswith('/api/commands') and r.method == 'POST') as sent:
            submit.click()
        body = sent.value.post_data_json
        assert body['kind'] == 'workspace.submit'
        assert [a['cause_id'] for a in body['payload']['acknowledgements']] == ['c1', 'c2', 'c3']
        assert body['payload']['acknowledgements'][0]['reason'] == 'Reason 1 agreed'
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_safety_cause_cannot_be_acknowledged_so_submit_offers_no_gaps(browser, live_server, h):
    w = h.shift(h.workspace('Fix list safety'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        serve(page, w['id'], safety=True)
        open_rota(page, w['id'])
        dialog = submit_dialog(page)
        expect(dialog.get_by_label('Submit with these gaps acknowledged')).to_have_count(0)
        expect(dialog.locator('li[data-cause=c2] .fix-tag')).to_contain_text(['Safety'])  # never acknowledgeable (D3)
        expect(dialog.locator('button[type=submit]')).to_be_disabled()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


ADJUSTMENT = {'id': 'adj-1', 'kind': 'rule_adjustment', 'reason': 'Marta Green covers the extra weekend; agreed with the programme lead.',
              'source': {'id': 'adj-1', 'policy_id': 'p-unknown', 'line_id': 'l1', 'number': 'max', 'person_id': None,
                         'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'mode': 'set', 'amount': 3,
                         'reason': 'Marta Green covers the extra weekend; agreed with the programme lead.'}}


def test_approver_decides_each_adjustment_and_gap_and_the_primary_follows(browser, live_server, h):
    """R5: every item accepted or rejected (a reject with its reason), "k of n decided"; Approve rota when all are
    accepted, Return to coordinator when one is rejected, nothing until all are decided."""
    w = h.shift(h.workspace('Fix list decide'))['workspace']
    w = h.command('coordinator', 'workspace.submit', {'reason': 'Ready'}, w['id'], w['revision'])['workspace']
    context, page = sign_in(browser, live_server, 'approver', viewport={'width': 1440, 'height': 900})
    try:
        listed = fix_list(w['id'], acknowledged=True)
        listed['review_items'].append(ADJUSTMENT)
        serve(page, w['id'], acknowledged=True)
        page.route(f'**/api/workspaces/{w["id"]}/fix-list*', lambda r: fix_list_reply(r, listed))
        open_rota(page, w['id'])
        page.get_by_role('button', name='Approve rota', exact=True).click()
        dialog = page.get_by_role('dialog', name='Approve rota')
        expect(dialog.locator('.fix-gate')).to_contain_text('Nothing new since it was submitted')
        changes = dialog.get_by_role('region', name='Changes for this rota')
        gaps = dialog.get_by_role('region', name='Acknowledged gaps')
        expect(changes.locator('[data-review-item=adj-1]')).to_contain_text(re.compile(r'At most 3 for this rota · .*4 Oct.*10 Oct'))
        expect(changes).to_contain_text('“Marta Green covers the extra weekend; agreed with the programme lead.”')
        expect(gaps.locator('.fix-quote')).to_have_text(['“Look-back week; programme lead agreed.”',
                                                        '“Cap change proposed today.”', '“Both start late.”'])
        approve = dialog.locator('button[type=submit]')
        count = dialog.locator('.decide-count')
        expect(count).to_have_text('Decide each item to approve 0 of 4 decided')  # the separators are CSS (factLine)
        expect(approve).to_be_disabled()
        gaps.get_by_role('button', name='Accept all').click()
        changes.get_by_role('button', name='Reject', exact=True).click()
        expect(count).to_have_text('A reject needs its reason 4 of 4 decided 1 rejected')
        expect(approve).to_have_text('Return to coordinator')
        expect(approve).to_be_disabled()  # a reject needs its reason
        changes.get_by_label('Why reject').fill('The extra weekend is not justified')
        expect(approve).to_be_enabled()
        shot(page, 'r5-approver-decisions', {'width': 1440})
        changes.get_by_role('button', name='Accept', exact=True).click()
        expect(approve).to_have_text('Approve rota')
        changes.get_by_role('button', name='Reject', exact=True).click()
        dialog.get_by_label('Note', exact=True).fill('Returned for the weekend cap')
        approve.click()
        with page.expect_request(lambda r: r.url.endswith('/api/commands') and r.method == 'POST') as sent:
            approve.click()
        body = sent.value.post_data_json
        assert body['kind'] == 'workspace.approve'
        decisions = {d['item_id']: d for d in body['payload']['decisions']}
        assert decisions['adj-1'] == {'item_id': 'adj-1', 'kind': 'adjustment', 'accept': False,
                                      'reason': 'The extra weekend is not justified'}
        assert all(decisions[f'gap-c{i}']['accept'] for i in (1, 2, 3))
        assert set(body['payload']) == {'note', 'decisions', 'delegation_id'}  # maintenance decisions + maintenance optional note
        assert body['payload']['note'] == 'Returned for the weekend cap'
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_saved_view_of_a_scheduling_group_keeps_the_gate_rota_wide(browser, live_server, h):
    """R2: filtering by rank keeps the rota-wide gate ("3 must fix · 1 in this view") and counts the rest outside it."""
    w = h.shift(h.workspace('Fix list group'), people=[])['workspace']
    rank = h.seed['category']
    name = next(r['name'] for r in h.get('admin', '/api/catalogues')['categories'] if r['id'] == rank)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        serve(page, w['id'], rank=rank)
        navigate(page, f'schedule/issues?draft={w["id"]}')
        main = page.locator('#main .fix-page')
        # The shared Filter (conditionFilter): Where Rank is any of <rank>.
        condition(toolbar(main, 'Filter'), 'Rank', [name])
        page.keyboard.press('Escape')
        expect(main.locator('.fix-gate')).to_contain_text('3 must fix before you submit · 1 in this view')
        expect(main.locator('section[data-level=must] .fix-row')).to_have_count(1)
        outside = main.get_by_role('region', name='Outside this view')
        expect(outside).to_contain_text('Everyone else')
        expect(outside.locator('.badge').last).to_have_text('2')
        shot(page, 'r2-fix-list-my-group', {'width': 1440})
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_focus_mode_rings_a_causes_sessions_and_steps_with_j_and_k(browser, live_server, h):
    """R3: Show on schedule opens the cause bar over the Schedule; its sessions are ringed, J / K step, Esc leaves."""
    w = h.shift(h.workspace('Fix list focus'), people=[])['workspace']
    shift = h.detail(w)['shifts'][0]['id']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        serve(page, w['id'], shift=shift)
        open_rota(page, w['id'])
        navigate(page, f'schedule/issues?draft={w["id"]}')
        clash = page.locator('#main section[data-level=must] li[data-cause=c2]')
        clash.get_by_role('button', name='Show evidence', exact=True).click()
        clash.get_by_role('button', name='Show on schedule').click()
        bar = page.get_by_role('region', name='Cause in focus')
        expect(bar).to_contain_text('1 of 1')
        expect(bar).to_contain_text('Weekend Urgent Care needs more seniors than the caps allow')
        expect(page.locator(f'#main [data-session~="{shift}"].cause-ring').first).to_be_visible()
        shot(page, 'r3-focus', {'width': 1440})
        page.keyboard.press('Escape')
        expect(bar).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_the_approver_reviews_a_real_rota_adjustment_and_a_reject_returns_the_rota(browser, live_server, h):
    """D16 + R5 against the server: the coordinator's line/person/date adjustment (maintenance) is listed once in the
    approver's review with its justification; rejecting it with a reason returns the rota to draft without it."""
    from .test_be5_governance import binding_fixture
    binding, policy, rota = binding_fixture(h)
    why = 'Covers the extra clinic this week; agreed with the programme lead.'
    rota = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': policy['id'], 'line_id': binding['id'], 'number': 'max',
                     'person_id': h.seed['people'][0], 'mode': 'set', 'amount': 2, 'reason': why}, rota['id'], rota['revision'])['workspace']
    rota = h.command('coordinator', 'workspace.submit', {'reason': 'Ready'}, rota['id'], rota['revision'])['workspace']
    context, page = sign_in(browser, live_server, 'approver', viewport={'width': 1440, 'height': 900})
    try:
        open_rota(page, rota['id'])
        page.get_by_role('button', name='Approve rota', exact=True).click()
        dialog = page.get_by_role('dialog', name='Approve rota')
        changes = dialog.get_by_role('region', name='Changes for this rota')
        item = changes.locator('li[data-review-item]')
        expect(item).to_have_count(1)
        expect(item).to_contain_text('At most 2 for this rota')
        expect(item.locator('.fix-quote')).to_have_text(f'“{why}”')
        changes.get_by_role('button', name='Reject', exact=True).click()
        changes.get_by_label('Why reject').fill('Find another person for the clinic first')
        dialog.get_by_label('Note', exact=True).fill('Returned for the weekly cap')
        approve = dialog.locator('button[type=submit]')
        expect(approve).to_have_text('Return to coordinator')
        approve.click()
        approve.click()  # the decision is pressed twice
        expect(page.get_by_text('Returned to the coordinator with your decisions')).to_be_visible()
    finally:
        context.close()
    after = h.detail(rota)['workspace']
    assert after['status'] == 'draft' and after['rule_adjustments'] == []



def test_open_people_opens_the_persons_eligibility_from_the_servers_prefill(browser, live_server, h):
    """maintenance open_eligibility: one person in the cause's exact fix hint opens that person; nobody named opens People."""
    w = h.shift(h.workspace('Fix list eligibility'), people=[])['workspace']
    person = h.seed['people'][0]
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        listed = fix_list(w['id'])
        listed['causes'][0]['action'] = {'kind': 'open_eligibility', 'scope': {'person_ids': [person], 'shift_ids': [],
            'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'keep_existing': True,
            'prefills': [{'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'person_id': person}]}}
        serve(page, w['id'])
        page.route(f'**/api/workspaces/{w["id"]}/fix-list*', lambda r: fix_list_reply(r, listed))
        navigate(page, f'schedule/issues?draft={w["id"]}')
        page.locator('#main li[data-cause=c1]').get_by_role('button', name='Who can work it', exact=True).click()
        expect(page.locator('.person-panel')).to_be_visible()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_fix_list_fetches_evidence_on_expand_retries_and_searches_all_evidence(browser, live_server, h, view, scheme):
    from copy import deepcopy
    from urllib.parse import parse_qs, urlsplit
    w = h.shift(h.workspace('Lazy evidence'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    full, reads, fail = fix_list(w['id']), [], [True]
    full['causes'][1]['evidence'] = [{'person_name': 'Synthetic evidence-only person', 'explanation': 'Retained evidence'}]
    def respond(route):
        query = parse_qs(urlsplit(route.request.url).query)
        reads.append(query)
        if 'cause_id' in query and fail[0]:
            route.fulfill(status=503, json={'error': {'code': 'UNAVAILABLE', 'message': 'Evidence could not be loaded'}})
            return
        fix_list_reply(route, deepcopy(full))
    try:
        serve(page, w['id'])
        page.route(f'**/api/workspaces/{w["id"]}/fix-list*', respond)
        navigate(page, f'schedule/issues?draft={w["id"]}')
        main = page.locator('#main .fix-page')
        row = main.locator('li[data-cause=c1]')
        expect(row).to_be_visible()
        assert reads and all(q.get('view') == ['lean'] for q in reads)  # opening reads the lean view only
        toggle = row.locator('.fix-more')
        expect(toggle).to_have_accessible_name('Show evidence')
        toggle.focus(); page.keyboard.press('Enter')
        expect(row.get_by_role('alert')).to_contain_text('Evidence could not be loaded')
        fail[0] = False
        row.get_by_role('button', name='Retry', exact=True).click()
        expect(row.locator('.fix-detail')).to_contain_text('Jules Foster')
        count = len(reads)
        toggle.click(); toggle.click()
        expect(toggle).to_have_attribute('aria-expanded', 'true')
        expect(row.locator('.fix-detail')).to_contain_text('Below 6 session units a week.')
        assert len(reads) == count
        main.get_by_role('searchbox', name='Search the fix list').fill('Synthetic evidence-only person')
        expect(main.locator('.fix-list>li:visible')).to_have_count(1)
        expect(main.locator('.fix-list>li:visible')).to_have_attribute('data-cause', 'c2')
        assert len([q for q in reads if q.get('view') == ['full']]) == 1, 'Search hydrates once, not once per cause'
        page.screenshot(path=f'/tmp/perf-sweep-fix-list-{view["width"]}.png', full_page=True)
    finally:
        context.close()


def test_a_collapsed_row_draws_its_facts_from_the_lean_view_alone(browser, live_server, h):
    """Lean Fix list (LEAD-UIUX, 8 Nov 2026): opening reads view=lean only, and a collapsed row shows its Safety and New
    tags, the line under its sentence and its action's label from the lean row, with no detail read until Show evidence."""
    from urllib.parse import parse_qs, urlsplit
    w = h.shift(h.workspace('Lean rows'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    body, views = fix_list(w['id'], safety=True), []

    def respond(route):
        views.append((parse_qs(urlsplit(route.request.url).query).get('view') or ['full'])[0])
        fix_list_reply(route, body)
    try:
        serve(page, w['id'])
        page.route(f'**/api/workspaces/{w["id"]}/fix-list*', respond)
        navigate(page, f'schedule/issues?draft={w["id"]}')
        row = page.locator('#main .fix-page li[data-cause=c2]')
        expect(row.locator('.fix-tag')).to_have_text(['New', 'Safety'])
        expect(row.locator('.fix-meta')).to_contain_text('7 places open · rules clash')
        expect(row.get_by_role('button', name='See the clash', exact=True)).to_be_visible()
        assert views and set(views) == {'lean'}, views
        first = page.locator('#main .fix-page li[data-cause=c1]')
        first.get_by_role('button', name='Show evidence', exact=True).click()
        expect(first.locator('.fix-detail')).to_contain_text('Jules Foster')  # its evidence: one detail read
        assert views.count('detail') == 1, views
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

"""Cycle-2 frontend fixes (journey root-cause audit, maintenance): the Fix list's rows and groups say one unit per number, its
evidence names clinic, rule and why once per distinct row, the footer popover opens on the list the page holds, the
Auto-schedule panel opens beside the Fix list and never offers a second run while one goes, the search clock is the
footer's, a failed published change is said in its panel, a stale CSRF token renews once, and the shared toolbar says a
view's own filter. Fix list shapes come through page.route (the maintenance/maintenance contract); everything else is the real API."""
import json
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect
from packages.contracts.schemas import ABSENCE_REPORT_LABEL

from .test_browser import browser, live_server, sign_in, navigate, open_config, open_rota, route_fetch, save_dialog, fix_list_reply  # noqa: F401
from .test_browser_fix_list import cause, fix_list, serve
from .test_browser import fix_pill
from .test_browser_autoschedule_live import start_run
from .test_browser_cp_sat import cp_server  # noqa: F401
from .test_browser_published_changes import FakeChanges, published, open_session

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 900}
SHOTS = os.getenv('FE_C2_SHOTS')  # a folder: the fixed screens, for the handoff's after pictures


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f'after-{name}-1440l.png'))


def evidence_list(workspace_id):
    """A rota-wide must cause with one row three times (one per rule line), and an advisory safety-capable cause."""
    body = fix_list(workspace_id)
    row = {'code': 'RULE', 'person_id': 'p-jules', 'person_name': 'Jules Foster', 'shift_id': None, 'start': '2026-10-04T00:00:00Z',
           'detail': {'required': 2, 'available': 0}, 'rule_name': 'Available eligible cover',
           'sessions': [{'shift_id': 's1', 'service_names': ['Physical Urgent Care'], 'location_name': 'Main', 'assignment_type_name': 'Day shift'}],
           'reason': '0 eligible: nobody meets the eligibility requirements for Physical Urgent Care during 4–10 Oct.'}
    body['causes'][0]['evidence'] = [dict(row), dict(row), dict(row)]
    body['causes'][0]['safety'] = True
    body['causes'][3]['safety'] = True  # an advisory cover target (maintenance sends safety only on must; the client agrees)
    return body


def test_fix_list_shows_the_servers_safety_flag_merges_evidence_and_groups_by_level(browser, live_server, h):
    w = h.shift(h.workspace('FE-C2 fix list'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        body = evidence_list(w['id'])
        reads = []
        page.route(f'**/api/workspaces/{w["id"]}/fix-list*', lambda r: (reads.append(1), fix_list_reply(r, body)))
        navigate(page, f'schedule/issues?draft={w["id"]}')
        main = page.locator('#main .fix-page')
        must, could = main.locator('section[data-level=must]'), main.locator('section[data-level=could]')
        expect(must.locator('li[data-cause=c1] .fix-tag')).to_have_text('Safety')
        # DL-001: the server decides safety (maintenance: must-fix only); the client draws what it is sent.
        expect(could.locator('li[data-cause=c4] .fix-safety')).to_have_text('Safety')
        # ME-13: the three identical rows are one, with the clinic, the rule, the numbers and how many times; why, once.
        row = must.locator('li[data-cause=c1]')
        row.get_by_role('button', name='Show evidence', exact=True).click()
        table = row.locator('.fix-detail table')
        expect(table.locator('th')).to_have_text(['Person', 'Session', 'Rule', 'Found', 'Times'])
        expect(table.locator('tbody tr')).to_have_count(1)
        expect(table.locator('tbody tr')).to_contain_text('Physical Urgent Care')
        expect(table.locator('tbody tr')).to_contain_text('Available eligible cover')
        expect(table.locator('tbody tr td').last).to_have_text('×3')
        shot(page, 'fix-list-evidence')
        expect(row.locator('.fix-detail p').filter(has_text='0 eligible')).to_have_count(1)
        # F-17: grouped by rule, a group holding both levels counts each in its own words (one unit per number).
        main.locator('.dt-tool').filter(has_text='Group').click()
        page.get_by_role('menuitemradio', name='Rule').click()
        expect(main.locator('section.fix-panel .fix-level .badge.err').first).to_have_text(re.compile(r'^\d+ must fix$'))
        expect(main.locator('section.fix-panel .fix-level .badge').filter(has_text=re.compile(r'^\d+$'))).to_have_count(0)
        # F-10: the footer popover opens on the list this page holds, without reading it again.
        before = len(reads)
        fix_pill(page, 'err').click()
        pop = page.get_by_role('dialog', name='Rota health')
        expect(pop.get_by_role('button', name=re.compile(r'^Open all in Fix list \(\d+\)$'))).to_be_visible()
        expect(pop).not_to_contain_text('Loading…')
        shot(page, 'footer-popover')
        assert len(reads) == before
    finally:
        context.close()


def test_fix_list_auto_schedule_opens_beside_it_in_place(browser, live_server, h):
    """ME-15: the top bar's Auto-schedule on the Fix list opens the panel there; the page and its address stay."""
    w = h.shift(h.workspace('FE-C2 fix list auto'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        serve(page, w['id'])
        navigate(page, f'schedule/issues?draft={w["id"]}')
        expect(page.locator('#main .fix-page .fix-gate')).to_be_visible()
        auto = page.locator('.topbar .planner-bar').get_by_role('button', name='Auto-schedule')
        auto.click()
        panel = page.locator('#auto-panel')
        expect(panel).to_be_visible()
        expect(auto).to_have_attribute('aria-expanded', 'true')
        expect(page.locator('#main .fix-page')).to_be_visible()
        shot(page, 'fix-list-autoschedule-in-place')
        assert 'schedule/issues' in page.url
        auto.click()  # the same button closes it, as on the schedule
        expect(panel).to_be_hidden()
        expect(auto).to_have_attribute('aria-expanded', 'false')
    finally:
        context.close()


def test_a_running_auto_schedule_offers_no_second_run_and_the_panel_clock_is_the_footers(browser, cp_server, h):
    """ME-03 and ME-02: while a run goes the panel holds Stop / Add time / Cancel and no Run or rota check; the time
    used it says is the search clock the footer's strip shows."""
    a, b = h.seed['people'][:2]
    context, page, stream, w, _ = start_run(browser, cp_server, h, [(4, 8, [a]), (5, 8, [b])], view='Week')
    try:
        stream.progress = {'stage': 'scoring', 'elapsed_seconds': 90, 'search_elapsed_seconds': 42}
        page.get_by_role('button', name='Auto-schedule', exact=True).click()
        panel = page.locator('#auto-panel')
        expect(panel.get_by_role('button', name='Stop and keep best', exact=True)).to_be_visible()
        expect(panel.locator('.auto-foot').get_by_role('button', name=re.compile('Run auto-schedule|Check the whole rota'))).to_have_count(0)
        expect(panel.locator('.auto-time')).to_contain_text('42 s used')  # the search has ended: no time left is said
        expect(panel.locator('.auto-time')).not_to_contain_text('left')
        expect(page.locator('.app-footer .asl-left')).to_have_text('42 s used')
        shot(page, 'running-panel-clock')
    finally:
        context.close()


class FailingChanges(FakeChanges):
    """The change list whose first `publication.change` (or send) is refused the way a busy server refuses it."""
    def __init__(self, h, fail):
        super().__init__(h)
        self.fail = fail

    def command(self, route):
        kind = (route.request.post_data_json or {}).get('kind', '')
        if kind == self.fail:
            self.fail = ''
            return route.fulfill(status=503, json={'error': {'code': 'CATALOGUE_BUSY',
                                                             'message': 'The system is busy, so this change was not saved. Try again.'}})
        return super().command(route)


@pytest.mark.parametrize('fail', ['publication.change', 'publication.changes.submit'], ids=['change', 'send'])
def test_a_refused_published_change_is_said_in_its_panel_with_try_again(browser, live_server, h, fail):
    """ME-01 / F8: the refusal stays in the panel (not only the footer's save state), and Try again or Send goes on."""
    tomorrow, _ = published(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        fake = FailingChanges(h, fail)
        fake.install(page)
        panel = open_session(page, h, tomorrow)
        panel.get_by_role('button', name='Change Alex Morgan on this session').click()
        page.get_by_role('menuitem', name='Replace…').click()
        panel.get_by_role('button', name='Assign Taylor Noor').click()
        if fail == 'publication.change':
            expect(panel.locator('.notice.error')).to_contain_text('This change was not made. The system is busy')
            panel.get_by_role('button', name='Try again').click()
        expect(panel.get_by_text('Passes every check')).to_be_visible()
        panel.get_by_role('button', name='Send for approval').click()
        if fail == 'publication.changes.submit':
            expect(panel.locator('.pc-failed .notice.error')).to_contain_text('Not sent. The change is still in the list.')
            shot(page, 'send-failed-inline')
            panel.get_by_role('button', name='Send for approval').click()
        expect(panel.get_by_text(re.compile('^Waiting for approval'))).to_be_visible()
    finally:
        context.close()


def test_a_stale_csrf_token_renews_once_then_asks_to_sign_in_again(browser, live_server, h):
    """ME-16: a write refused for its CSRF token renews the session's tokens and is sent once more (same body); when
    renewing is refused too, the person is asked to sign in again, the page kept."""
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        navigate(page, 'home')
        sent, renewed = [], []
        refused = {'n': 1}

        def commands(route):
            sent.append(route.request.headers.get('idempotency-key'))
            if refused['n']:
                refused['n'] -= 1
                return route.fulfill(status=403, json={'error': {'code': 'CSRF_FAILED', 'message': 'Refresh your session before saving'}})
            route.fallback()
        page.route('**/api/commands', commands)
        page.on('request', lambda r: renewed.append(1) if r.url.endswith('/auth/refresh') else None)
        payload = {'kind': 'workspace.create', 'payload': {'name': 'FE-C2 csrf', 'starts_on': '2027-03-01', 'ends_on': '2027-03-08'}}
        done = page.evaluate("async p=>{const m=await window.importBundle('api.js');try{await m.command(p.kind,p.payload);return 'ok';}catch(e){return e.code;}}", payload)
        assert done == 'ok' and len(sent) == 2 and sent[0] == sent[1] and len(renewed) == 1
        # Renewing refused too: the sign-in prompt, in words a person can act on.
        refused['n'] = 1
        page.route('**/auth/refresh', lambda r: r.fulfill(status=403, json={'error': {'code': 'CSRF_FAILED', 'message': 'no'}}))
        code = page.evaluate("async p=>{const m=await window.importBundle('api.js');try{await m.command(p.kind,p.payload);return 'ok';}catch(e){return e.code;}}",
                             {**payload, 'payload': {**payload['payload'], 'name': 'FE-C2 csrf 2'}})
        assert code == 'CSRF_FAILED'
        # The sign-in prompt, the page kept behind it. Its words are the CSRF ones unless another expiry notice (the
        # session's own token renewal, also refused here) reaches it first; either way the person signs in again.
        expect(page.get_by_role('dialog', name='Sign in')).to_be_visible()
    finally:
        context.close()


def test_a_views_own_filter_is_counted_on_the_filter_button(browser, live_server, h):
    """F-C2: Tags' default view filters by Status is Active; the Filter button counts it (no accent: not a change)."""
    h.configure('tags', {'name': 'FEC2Tag', 'labels': ['people'], 'description': 'FE-C2'})
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        open_config(page, 'Tags')
        button = page.locator('.config-pane .dt-tool').filter(has_text='Filter').first
        expect(button.locator('.dt-tool-count')).to_have_text('1')
        expect(button).not_to_have_class(re.compile(r'\bon\b'))
        expect(button).to_have_attribute('aria-label', 'Filter (1 from the view)')
    finally:
        context.close()


def test_no_end_date_empties_the_end_of_a_self_approval_permission(browser, live_server, h):
    """F-A3: with No end date ticked the dates read open-ended; unticked, the default end comes back."""
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, 'access/routes')
        page.get_by_role('button', name='Add permission', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add a self-approval permission')
        until = dialog.get_by_label('Until (included)', exact=True)
        expect(until).not_to_have_value('')
        dialog.get_by_label('No end date', exact=True).check()
        expect(until).to_have_value('')
        expect(until).to_be_disabled()
        dialog.get_by_label('No end date', exact=True).uncheck()
        expect(until).not_to_have_value('')
    finally:
        context.close()


def test_new_rota_lists_rotas_to_copy_newest_first_with_their_state(browser, live_server, h):
    """ME-05: two rotas of one name and dates are told apart by their state; the list is newest first."""
    h.workspace('FE-C2 copy', '2026-10-04', '2026-10-11')
    later = h.workspace('FE-C2 later', '2026-11-01', '2026-11-08')
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        navigate(page, 'home')
        page.get_by_role('button', name='New rota').first.click()
        dialog = page.get_by_role('dialog', name='New rota', exact=True)
        dialog.get_by_label('Start from', exact=True).select_option('previous')
        options = dialog.get_by_label('Previous rota', exact=True).locator('option')
        texts = options.all_text_contents()
        mine = [t for t in texts if t.startswith('FE-C2')]
        assert mine[0].startswith('FE-C2 later') and all(t.endswith('· draft') for t in mine), texts
        assert later['id']
    finally:
        context.close()


def test_the_approver_sees_what_an_amendment_changes(browser, live_server, h):
    """F4 (maintenance change_diff): the approve dialog lists each changed session, who was on it and who is now."""
    a, b, _ = h.seed['people']
    w = h.shift(h.workspace('FE-C2 amendment'), people=[a])['workspace']
    shift = h.detail(w)['shifts'][0]
    w = h.command('coordinator', 'workspace.validate', {'reason': 'Routine rota check'}, w['id'], w['revision'])['workspace']
    h.command('coordinator', 'workspace.submit', {'reason': 'Ready'}, w['id'], w['revision'])
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        def detail(route):
            reply = route_fetch(route)
            data = reply.json()
            after = {**shift, 'participants': [{'person_id': b}]}
            data['change_diff'] = {'base_publication_id': 'p1', 'sessions': [{'before': shift, 'after': after}],
                                   'first_changed_date': '2026-10-04', 'review_week_start': '2026-10-04'}
            route.fulfill(response=reply, body=json.dumps(data))
        page.route(f'**/api/workspaces/{w["id"]}', detail)
        open_rota(page, w['id'])
        page.get_by_role('button', name='Approve rota').click()
        changes = page.get_by_role('dialog').get_by_role('region', name='What changes')
        expect(changes).to_contain_text('Alex Morgan → Jordan')
        shot(page, 'approver-what-changes')
    finally:
        context.close()


def test_an_uncertain_rule_effect_is_approved_only_with_an_explicit_reasoned_acknowledgement(browser, live_server, h):
    """maintenance / D6: a check that could not finish keeps Approve off until the approver ticks that they approve without it
    and says why; the decision carries acknowledge_unresolved and that reason (no invented receipt)."""
    from .test_browser_rule_line_relation import world, relation, themed
    from .test_browser_rule_catalogue_v2 import line
    from .test_rule_catalogue_api import propose as propose_rule
    rule, broad, _, _, name, who, where = world(h)
    added = line({'max': {'value': 4, 'level': 'mandatory'}}, who=who, where=where, measure='duties', period={'kind': 'calendar_week'},
                 starts_on='2027-01-03', ends_on='2027-01-10')
    lines = [*rule['definition']['rule']['lines'], added]
    check = h.client('coordinator').post('/api/rules/catalogue/workload_limit/check', json={'catalogue_version': 2, 'lines': lines}).json()
    proposal = propose_rule(h, 'workload_limit', lines, catalogue_version=2, policy_id=rule['id'], revision=rule['revision'],
                            acknowledged=[o['line_id'] for o in check['overlaps']], reason=f'{name} is on induction')['record']
    effect = {'workspace_id': 'w1', 'name': 'Rota 3–9 Jan', 'revision': 1, 'job_id': None, 'status': 'unresolved',
              'summary': 'The check could not finish for this rota.', 'requires_acknowledgement': True}
    review = {'policy_id': proposal['id'], 'relations': {added['id']: relation(broad, True)}, 'effect': [effect], 'ready': False,
              'can_decide': True, 'requires_acknowledgement': True}
    context, page = themed(browser, live_server, 'approver', 1440, 'light')
    try:
        page.route(f"**/api/policies/{proposal['id']}/review", lambda route: route.fulfill(json=review))
        navigate(page, 'rules/workload_limit')
        page.locator('#main .rc-waiting').get_by_role('button', name='Review and approve', exact=True).click()
        panel = page.get_by_role('region', name=f'Add a line for {name}')
        expect(panel.locator('.rc-effect')).to_contain_text('Check incomplete')
        expect(panel.locator('.rc-effect')).to_contain_text('The check could not finish for this rota.')
        approve = panel.get_by_role('button', name='Approve and activate', exact=True)
        expect(approve).to_be_disabled()
        panel.get_by_label('Approve without a complete check of these drafts', exact=True).check()
        expect(approve).to_be_disabled()  # the reason is still missing
        panel.get_by_label('Reason', exact=True).fill('January draft is being rebuilt; recheck after')
        expect(approve).to_be_enabled()
        shot(page, 'rule-review-acknowledge')
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/commands')
                                 and (r.post_data_json or {}).get('kind') == 'policy.activate') as sent:
            approve.click()
        payload = sent.value.post_data_json['payload']
        assert payload['acknowledge_unresolved'] is True and payload['reason'] == 'January draft is being rebuilt; recheck after'
    finally:
        context.close()


def test_wish_priority_is_set_in_configuration_and_kept_by_other_saves(browser, live_server, h):
    """F-C1 (maintenance): the three wish-priority settings are on Staff preferences in the contract's words and defaults, and
    saving another setting sends them as they are (never back to their defaults)."""
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        open_config(page, 'Auto-schedule & scoring')
        pane = page.locator('.config-pane')
        pane.get_by_role('tab', name='Staff preferences', exact=True).click()
        decay = pane.get_by_label('Wish priority retained per period', exact=True)
        expect(decay).to_have_value('0.5')
        expect(pane.get_by_label('Wish history periods', exact=True)).to_have_value('6')
        decay.fill('0.8')
        pane.get_by_role('button', name='Save defaults', exact=True).click()
        dialog = page.get_by_role('dialog', name='Save auto-schedule defaults', exact=True)
        dialog.get_by_label('Reason', exact=True).fill('Longer wish memory')
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/commands')) as sent:
            save_dialog(dialog)
        assert '"wish_priority_decay": 0.8' in json.dumps(sent.value.post_data_json)
    finally:
        context.close()


def test_the_approver_reads_the_coordinators_submit_note(browser, live_server, h):
    """ME-25: the note typed when submitting is quoted in the approver's decision; an empty one ("Routine update") is not."""
    a = h.seed['people'][0]
    w = h.shift(h.workspace('FE-C2 submit note'), people=[a])['workspace']
    w = h.command('coordinator', 'workspace.validate', {'reason': 'Routine rota check'}, w['id'], w['revision'])['workspace']
    h.command('coordinator', 'workspace.submit', {'reason': 'Weekend cover agreed with the lead'}, w['id'], w['revision'])
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        open_rota(page, w['id'])
        page.get_by_role('button', name='Approve rota').click()
        note = page.get_by_role('dialog').get_by_role('region', name='Submitted with this note')
        expect(note).to_contain_text('“Weekend cover agreed with the lead”')
        shot(page, 'approver-submit-note')
    finally:
        context.close()


def test_a_staff_cant_make_it_reaches_the_coordinators_home_and_opens_replace(browser, live_server, h):
    """F7 (maintenance absence_reported): the coordinator's Home says who can't make which duty; once approved its Replace
    opens that published session straight on Replace for that person."""
    tomorrow, _ = published(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        fake = FakeChanges(h)
        fake.install(page)
        session = next(s for s in fake.shifts if s['start'].startswith(tomorrow.isoformat()))
        who = next(p for p in session['participants'] if p['person_id'] == h.seed['people'][0])  # Alex Morgan
        item = {'key': 'absence_reported', 'count': 1, 'sample': [{'request_id': 'r1', 'person_id': who['person_id'], 'person': 'Alex Morgan',
                'start': session['start'], 'end': session['end'], 'status': 'approved',
                'replace_action': {'key': 'amend_publication', 'publication_id': 'p', 'shift_id': session['id'], 'assignment_id': who['id']}}]}

        def home(route):
            reply = route_fetch(route)
            data = reply.json()
            data['attention'] = [item, *data.get('attention', [])]
            route.fulfill(response=reply, body=json.dumps(data))
        page.route(re.compile(r'.*/api/home(\?.*)?$'), home)
        page.reload()
        navigate(page, 'home')
        row = page.locator('#main .home-needs-list li[data-need=absence_reported]')
        expect(row).to_contain_text(ABSENCE_REPORT_LABEL)
        expect(row).to_contain_text('Alex Morgan')
        expect(row).to_contain_text('needs cover')
        row.get_by_role('button', name='Replace').click()
        expect(page.locator('.dock-panel').get_by_role('heading', name='Replace Alex Morgan')).to_be_visible()
        shot(page, 'home-cant-make-it-replace')
    finally:
        context.close()

"""maintenance (SWEEP-3 findings SW3-A, -B, -C and -D left open by maintenance to maintenance): regressions, one or more per finding."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import auto_result, browser, live_server, sign_in, navigate, open_rota, save_dialog, fix_list_reply  # noqa: F401
from .test_browser_fix_list import serve
from .test_browser import fix_pill

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 900}


@pytest.mark.parametrize('role,ready', [('approver', 'Ready to approve'), ('coordinator', 'Ready')])
def test_a_submitted_rota_reads_ready_for_the_readers_own_step(browser, live_server, h, role, ready):
    """SW3-B-11: an approver read "Ready to submit" on a rota waiting for their approval. The footer and the Fix list
    now say the reader's own next step: the approver approves; the coordinator who submitted it has none."""
    w = h.shift(h.workspace('Ready for whom'))['workspace']
    w = h.command('coordinator', 'workspace.submit', {'reason': 'Ready'}, w['id'], w['revision'])['workspace']
    context, page = sign_in(browser, live_server, role, DESKTOP)
    try:
        serve(page, w['id'], must=False)
        open_rota(page, w['id'])
        pill = fix_pill(page, 'ok')
        expect(pill).to_have_attribute('aria-label', f'{ready} · score 71.4')
        pill.click()
        gate = page.locator('.popover.fix-pop .fix-gate')
        expect(gate).to_contain_text('Nothing must be fixed')
        expect(gate).not_to_contain_text('submit')
        if role == 'approver':
            expect(gate).to_contain_text('ready to approve')
        navigate(page, f'schedule/issues?draft={w["id"]}')
        page_gate = page.locator('#main .fix-page .fix-gate')
        expect(page_gate).to_contain_text('Nothing must be fixed')
        expect(page_gate).not_to_contain_text('submit')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_turning_a_rule_on_counts_as_a_change_and_activation_and_retirement_can_be_undone(browser, live_server, h):
    """SW3-B-16: switching a rule on with its first line read "Propose 1 change" while the dialog listed two; the
    approver's panel title left out that it turns the rule on; the page said "Activate…" and the panel "Approve and
    activate"; Retire rule had no Undo (D42)."""
    from .test_browser_rule_catalogue import flag, rule_action
    flag(h)
    rule = lambda: next(e for e in h.get('approver', '/api/rules/catalogue')['entries'] if e['id'] == 'rest_between_duties')
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        navigate(page, 'rules/rest_between_duties')
        page.get_by_role('switch', name='Rest between duties on or off').click()
        page.locator('.topbar').get_by_role('button', name='Propose 2 changes', exact=True).click()
        dialog = page.get_by_role('dialog', name='Propose 2 changes to Rest between duties')
        expect(dialog).to_contain_text('Turns the rule on')
        dialog.get_by_label('Reason', exact=True).fill('Rest after every duty')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        expect(dialog).to_have_count(0)
    finally:
        context.close()
    assert rule()['proposed'] is not None
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        navigate(page, 'rules/rest_between_duties')
        page.locator('.rc-waiting').get_by_role('button', name='Review and approve', exact=True).click()
        panel = page.locator('.rc-line-panel')
        expect(panel).to_contain_text(re.compile(r'Add a line for .* · turns the rule on'))
        panel.get_by_role('button', name='Approve and activate', exact=True).click()
        expect(page.locator('#toast-tray .toast', has_text='the change is active.').get_by_role('button', name='Undo')).to_be_visible()
        expect(page.locator('.rc-waiting')).to_have_count(0)
        rule_action(page, 'Retire rule')
        retire = page.get_by_role('dialog', name='Retire Rest between duties')
        retire.get_by_label('Reason', exact=True).fill('Trial over')
        save_dialog(retire, 'Retire rule')
        page.locator('#toast-tray .toast', has_text='Rest between duties retired.').get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('#toast-tray .toast', has_text='Retirement undone: the rule applies again.')).to_be_visible()
    finally:
        context.close()
    assert rule()['rule']['status'] == 'active'


def test_a_resubmitted_amendment_no_longer_says_its_changes_were_returned(browser, live_server, h):
    """SW3-B-05: after the coordinator resubmitted a returned amendment, the chip still said "1 change returned": the
    change list read for the publication was kept. A decision on the amendment now has it read again."""
    from .test_browser import IMPORT_BUNDLE
    from .test_browser_schedule import show
    from .test_published_changes import issued, change
    pub, shifts = issued(h)
    result = change(h, pub, shifts[0])
    w = h.command('coordinator', 'publication.changes.submit', {'reason': 'Cover the clinic'}, target=pub,
                  revision=result['workspace']['revision'])['workspace']
    w = h.lifecycle(w, 'reopen', role='approver')['workspace']
    day = shifts[0]['start'][:10]
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        show(page, f'schedule/week/{day}')
        chip = page.locator('.pc-chip')
        expect(chip).to_have_text('1 change returned')
        page.evaluate(f"""async ([id, rev]) => (await ({IMPORT_BUNDLE})('api.js')).command('workspace.submit', {{reason: 'Asked Taylor first'}}, id, rev)""",
                      [w['id'], w['revision']])
        show(page, f'schedule/month/{day}')
        show(page, f'schedule/week/{day}')
        expect(chip).to_have_text('1 change waiting')
    finally:
        context.close()


def test_the_submit_dialog_says_the_score_as_the_footer_does(browser, live_server, h):
    """SW3-A-25: the dialog said "Score 58" while the footer said 58.4: both now show the server's one-decimal score."""
    from .test_browser_fix_list import submit_dialog
    w = h.shift(h.workspace('Score in both places'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        serve(page, w['id'], must=False)
        open_rota(page, w['id'])
        expect(page.locator('.app-footer .fix-pills .health-pill.ok')).to_contain_text('score 71.4')
        dialog = submit_dialog(page)
        expect(dialog.locator('.summary-item').filter(has_text='Score')).to_have_text(re.compile(r'^Score\s*71\.4$'))
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_coordinator_phone_counts_sessions_in_view_as_the_desktop_does(browser, live_server, h):
    """SW3-A-30: the same published sessions read "N sessions in view" on a desktop and "N duties in view" on a phone."""
    from .test_browser_published_changes import published
    from .test_browser_schedule import show
    tomorrow, _ = published(h)
    for viewport in (DESKTOP, {'width': 390, 'height': 844}):
        context, page = sign_in(browser, live_server, 'coordinator', viewport)
        try:
            show(page, f'schedule/week/{tomorrow.isoformat()}')
            expect(page.locator('.app-footer .footer-total')).to_have_text(re.compile(r'^\d+ sessions? in view$'))
        finally:
            context.close()


def test_a_searched_person_is_drawn_first_and_never_hidden_in_more(browser, live_server, h):
    """SW3-A-31: searching a person (Show › whole duty, the default) left them inside a cell's "+N" when they were
    not among its first chips. The people searched now come first."""
    from .test_browser_schedule import show
    people = h.seed['people']
    w = h.shift(h.workspace('Search keeps the match in view'), people=people)['workspace']
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    last = people[-1]
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        show(page, f'schedule/month/2026-10-04?edit={w["id"]}')
        search = page.get_by_role('searchbox', name='Search sessions', exact=True)
        search.fill(names[last]); search.press('Enter')
        cell = page.locator('#schedule-view .wk-cell[data-session], #schedule-view .wk-cell').filter(has=page.locator('.pchip.more')).first
        expect(page.locator(f'#schedule-view {draft_chip(last)}').first).to_be_visible()
        expect(cell.locator('.pchip:not(.more)').first).to_have_attribute('data-person', last)
    finally:
        context.close()


@pytest.mark.parametrize('viewport,scheme', [(DESKTOP, 'light'), ({'width': 390, 'height': 844}, 'dark')])
def test_a_published_session_names_each_person_once_with_a_clear_menu(browser, live_server, h, viewport, scheme):
    """SW3-A-27: the published session panel repeated each short name ("M. Harris") under the full name in a tinted bar,
    and the phone's ⋯ was faint. A label that only shortens the name is not repeated (`spells`); a real code still
    shows; the ⋯ is at full ink."""
    from .test_browser import IMPORT_BUNDLE
    from .test_browser_published_changes import published, open_session
    tomorrow, _ = published(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport, color_scheme=scheme)
    try:
        panel = open_session(page, h, tomorrow)
        said = page.evaluate(f"""async () => {{const m = await ({IMPORT_BUNDLE})('planner_week.js');
            return [m.spells('D. Harris', 'Dr. Diego Harris'), m.spells('F. Parker', 'Dr. Felix Parker'),
                    m.spells('ZZQ', 'Dr. Felix Parker'), m.spells('A. Smith', 'Dr. Alex Jordan Morgan Smith')]}}""")
        assert said == [True, True, False, True]
        menu = panel.locator('.wk-person').first.get_by_role('button', name=re.compile('^Change .* on this session$'))
        expect(menu).to_be_visible()
        ink = page.evaluate("getComputedStyle(document.body).color")
        assert menu.evaluate("b => getComputedStyle(b).color") == ink
    finally:
        context.close()


def test_mark_absent_asks_why_once_and_replace_does_not_ask_again(browser, live_server, h):
    """SW3-A-28: Mark absent saved "Absent" with no reason, and the Replace that followed asked for one (defaulting to
    Sick). The reason is now chosen with Mark absent (one press, one of the contract's ABSENCE_CATEGORIES) and Replace never asks again."""
    from .test_browser_published_changes import published, open_session
    _, later = published(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        pubs = []
        page.on('request', lambda r: pubs.extend(re.findall(r'/api/publications/([^/?]+)/pending-changes', r.url)))
        panel = open_session(page, h, later, person=2)
        panel.get_by_role('button', name='Change Taylor Noor on this session').click()
        page.get_by_role('menu', name='Change Taylor Noor').get_by_role('menuitem', name='Mark absent · Leave').click()
        expect(panel).to_contain_text('Nobody is left on this session.')
        changes = h.get('coordinator', f'/api/publications/{pubs[-1]}/pending-changes')['changes']
        assert [(c['kind'], c['absence_reason']) for c in changes] == [('absence', 'leave')]
        panel.get_by_role('button', name='Replace Taylor Noor…').click()
        expect(panel.get_by_role('heading', name='Replace Taylor Noor')).to_be_visible()
        expect(panel.get_by_role('radiogroup')).to_have_count(0)  # the reason is not asked again
    finally:
        context.close()


def test_a_background_refresh_keeps_the_fix_list_and_its_open_cause(browser, live_server, h):
    """SW3-A-23: a background refresh dropped the Fix list back to the "Loading Schedule…" skeleton for as long as the
    read took (minutes under load) and closed the cause being read. The list stays (inert) meanwhile and the cause's
    details stay open."""
    from .test_browser import wait_held
    w = h.shift(h.workspace('Fix list refresh'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        serve(page, w['id'])
        navigate(page, f'schedule/issues?draft={w["id"]}')
        row = page.locator('#main .fix-page li[data-cause]').first
        row.locator('.fix-more').click()
        expect(row.locator('.fix-detail')).to_contain_text('Below 6 session units a week.')
        held, reads = [], []
        page.on('request', lambda r: reads.append(r.url) if '/fix-list' in r.url else None)
        page.route('**/api/pulse*', lambda r: held.append(r))
        page.evaluate("window.dispatchEvent(new Event('online'))")
        wait_held(page, held)
        expect(page.locator('#main .fix-page li[data-cause]').first).to_be_visible()  # not the page skeleton
        expect(page.locator('#main')).not_to_contain_text('Loading Schedule')
        with page.expect_response(lambda r: '/api/pulse' in r.url) as pulse:
            held[0].continue_()
        pulse.value.finished()
        page.evaluate('()=>new Promise(requestAnimationFrame)')
        assert reads == [], 'An unchanged pulse must not read the Fix list again'
        expect(page.locator('#main .fix-page li[data-cause] .fix-detail').first).to_be_visible()
        expect(page.locator('#main .fix-page li[data-cause] .fix-more').first).to_have_attribute('aria-expanded', 'true')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_the_auto_schedule_panel_stays_closed_on_a_new_visit_and_off_a_rota_in_review(browser, live_server, h):
    """SW3-A-24: the auto-schedule panel re-opened by itself on every visit, and on an amendment waiting for approval it
    offered an enabled Run auto-schedule. Leaving the Schedule closes it; it opens only on a draft you can change."""
    from .test_browser import open_scheduling
    w = h.shift(h.workspace('Panel on every visit'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        navigate(page, 'home')
        open_rota(page, w['id'])
        expect(page.locator('#auto-panel')).to_be_hidden()
        open_scheduling(page)
        h.command('coordinator', 'workspace.submit', {'reason': 'Ready'}, w['id'], h.detail(w)['workspace']['revision'])
        page.evaluate("window.dispatchEvent(new Event('online'))")  # a background refresh reads it submitted
        expect(page.locator('.topbar')).to_contain_text('Waiting for approval')
        expect(page.locator('#auto-panel')).to_be_hidden()
        expect(page.get_by_role('button', name='Run auto-schedule')).to_have_count(0)
    finally:
        context.close()


def test_a_rota_check_in_the_auto_schedule_panel_opens_the_fix_list(browser, live_server, h):
    """SW3-A-07: with a rota check as the latest run, "Review latest result" opened the full per-check table ("216 must
    fix") beside a footer counting causes ("2 must fix"). It now opens the Fix list, in the footer's unit."""
    from apps.worker.main import process_one
    from .test_browser import open_scheduling
    w = h.shift(h.workspace('Check opens the fix list'), people=[])['workspace']
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'}, revision=w['revision'])
    process_one(h.app.state.engine)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        expect(panel.get_by_role('button', name='Open fix list')).to_be_visible()
        expect(panel.get_by_role('button', name='Review latest result')).to_have_count(0)
        panel.get_by_role('button', name='Open fix list').click()
        # FIXLIST-UX: on a desktop the Fix list is the Planner's side panel, in the same slot.
        expect(page.locator('.dock-panel.fix-side')).to_be_visible()
    finally:
        context.close()


def test_an_auto_schedule_result_lists_its_shortfalls_one_per_line(browser, live_server, h):
    """SW3-A-08: the result dialog showed its shortfalls as one string ("UCC weekday trainee target: 0 of 1. Search
    ended….; People of a kind per session: 1 of 2. …"). Each is now its own line."""
    from .test_browser import open_scheduling
    w = h.shift(h.workspace('Shortfalls one per line'))['workspace']
    said = ['UCC weekday trainee target: 0 of 1. Search ended before proving this shortfall unavoidable.',
            'Companion cover: 0 of 1. Search ended before proving this shortfall unavoidable.']
    at = '2026-10-03T06:00:00+00:00'
    job = {'id': 'job-shortfalls', 'revision': 1, 'kind': 'generation', 'status': 'succeeded', 'outcome': 'needs_more_time',
           'workspace_id': w['id'], 'created_at': at, 'started_at': at, 'completed_at': at, 'input_fingerprint': 'older',
           'result': {'status': 'needs_more_time', 'warnings': said}}
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        page.route(f'**/api/jobs?workspace_id={w["id"]}*', lambda r: r.fulfill(json={'jobs': [job]}))
        page.route('**/api/jobs/job-shortfalls', lambda r: r.fulfill(json={'job': job}))
        open_rota(page, w['id'])
        open_scheduling(page)
        auto_result(page.locator('#auto-panel'))
        page.locator('#auto-panel').get_by_role('button', name='Review latest result').click()
        dialog = page.get_by_role('dialog')
        expect(dialog.locator('.iss-row.warning .iss-text')).to_have_text(said)
        expect(dialog).not_to_contain_text('.; ')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_an_approver_on_a_phone_reads_approve_in_words(browser, live_server, h):
    """SW3-B-10: on a phone the reviewer's Approve was a bare ✓ beside a fully labelled "Return to coordinator". The
    decision keeps its words."""
    w = h.shift(h.workspace('Approve in words'))['workspace']
    h.command('coordinator', 'workspace.submit', {'reason': 'Ready'}, w['id'], w['revision'])
    context, page = sign_in(browser, live_server, 'approver', {'width': 390, 'height': 844}, color_scheme='dark')
    try:
        open_rota(page, w['id'])
        approve = page.locator('.topbar').get_by_role('button', name='Approve rota', exact=True)
        expect(approve).to_be_visible()
        assert approve.evaluate("b => [...b.querySelectorAll('.label')].every(l => getComputedStyle(l).position !== 'absolute')")
        assert approve.inner_text().strip() == 'Approve rota'
    finally:
        context.close()


def test_an_ended_self_approval_permission_says_when_by_whom_and_why(browser, live_server, h):
    """SW3-B-20: an ended permission showed only its granted dates and "Ended"; its row now says when it ended, by
    whom and why, at once after End permission too."""
    from .governance_support import identity
    identity(h, 'both', ['coordinator', 'approver'])
    payload = {'identity_id': h.seed['identities']['both'], 'actions': ['rule.activate'], 'starts_on': '2026-01-01',
               'ends_on': '2027-01-01', 'reason': 'Only one approver this quarter'}
    first = h.command('admin', 'self_approval.grant', payload)['permission']
    h.command('admin', 'self_approval.revoke', {'reason': 'Second approver joined'}, first['id'], first['revision'])
    h.command('admin', 'self_approval.grant', {**payload, 'reason': 'Cover for leave'})
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, 'access/routes')
        ended = page.locator('tr').filter(has_text='Only one approver this quarter')
        expect(ended).to_contain_text('Ended')
        expect(ended).to_contain_text('“Second approver joined”')
        live = page.locator('tr').filter(has_text='Cover for leave')
        live.get_by_role('button', name=re.compile('More actions|End permission')).first.click()
        item = page.get_by_role('menuitem', name='End permission')
        if item.count():
            item.click()
        dialog = page.get_by_role('dialog', name='End permission')
        dialog.get_by_label('Reason', exact=True).fill('Leave is over')
        dialog.get_by_role('button', name='End permission', exact=True).click()
        expect(live).to_contain_text('“Leave is over”')
    finally:
        context.close()


def test_a_refused_line_says_why_where_it_is_marked_and_holds_propose(browser, live_server, h):
    """SW3-B-22: a line the check refused read "⚠ This line is refused" / "1 line refused" with no reason (only a
    later toast on Propose said one), and Propose stayed enabled. The reason is said beside the count and in the
    line's status, and Propose waits until the line is changed."""
    import json
    from .test_browser_rule_catalogue import flag
    from .test_browser import route_fetch
    flag(h)
    why = 'Choose Only when these people are on duty in Settings first'

    def refuse(route):
        response = route_fetch(route)
        body = response.json()
        body['issues'] = [*body.get('issues', []), {'field': 'lines.0.numbers', 'message': why}]
        route.fulfill(response=response, body=json.dumps(body))
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        page.route('**/api/rules/catalogue/rest_between_duties/check', refuse)
        navigate(page, 'rules/rest_between_duties')
        page.get_by_role('switch', name='Rest between duties on or off').click()
        expect(page.locator('#main')).to_contain_text(f'1 line refused: {why}')
        expect(page.locator('.topbar').get_by_role('button', name='Propose 2 changes', exact=True)).to_be_disabled()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_saving_notifications_acts_once_and_offers_undo(browser, live_server, h):
    """SW3-D-08: Notifications › Save turned into "Confirm save?" (a second press for a reversible change) and its
    "Notifications saved." had no Undo. One Save in the dialog saves; its message's Undo restores the version before."""
    from .test_browser_pwa import policy, open_config
    policy(h, lock_screen_detail='generic', reason='Alerts on for the pilot')
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        open_config(page, 'Notifications')
        pane = page.locator('.config-pane')
        pane.locator('.lvl-card', has_text='Event and date').click()
        pane.get_by_role('button', name='Save…').click()
        dialog = page.get_by_role('dialog', name='Save notifications')
        dialog.get_by_label('Reason').fill('More detail on the lock screen')
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(dialog).to_have_count(0)  # one press: no "Confirm save?"
        assert h.get('admin', '/api/me/push')['policy']['lock_screen_detail'] == 'event_date'
        page.locator('#toast-tray .toast', has_text='Notifications saved.').get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('#toast-tray .toast', has_text='Undone. The earlier notifications settings are back.')).to_be_visible()
        assert h.get('admin', '/api/me/push')['policy']['lock_screen_detail'] == 'generic'
    finally:
        context.close()


def test_data_health_counts_as_configuration_does_and_says_what_its_ring_counts(browser, live_server, h):
    """SW3-D-10: the ring said 8/8 beside ten steps (the tenth a bare "10"), "Recurring sessions: 123" beside the
    nav's 52, and screen readers kept "Loading Setup guide…" after the load. The ring says it counts required steps,
    an optional step is not numbered, the step counts recurring sessions as the nav does, and the wait's words clear."""
    from .test_browser_recurring_sessions import duty_types, series
    types = duty_types(h)
    for day in (0, 1):  # one clinic, duty and time on two weekdays: one row in Recurring sessions
        series(h, f'Clinic day {day}', types['am'], h.seed['locations'][0], h.seed['services'][0], [day])
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, 'configuration/setup')
        expect(page.locator('.setup-ring')).to_contain_text('required')
        nav = page.locator('.config-nav [data-section="recurring_series"] .nav-count-muted')
        step = page.locator('.setup-step').filter(has_text='Recurring sessions')
        expect(nav).to_have_text('1')
        expect(step).to_contain_text('1 recurring session')
        expect(step).not_to_contain_text('2 recurring sessions')
        for optional in page.locator('.setup-step:not(.done)').filter(has_text='Optional').all():
            expect(optional.locator('.setup-mark')).to_have_text('')
        expect(page.locator('#loading-live:not(:empty)')).to_have_count(0)
        # A said message (e.g. "Press again to confirm: Discard") does not stay in the live region once spoken.
        from .test_browser import IMPORT_BUNDLE
        page.evaluate(f"async () => (await ({IMPORT_BUNDLE})('dom.js')).announce('Press again to confirm: Discard')")
        expect(page.locator('#sr-live')).to_have_text('Press again to confirm: Discard')
        expect(page.locator('#sr-live')).to_have_text('')
    finally:
        context.close()


def test_one_group_field_list_says_its_group_once_and_data_health_flags_shared_rank_codes(browser, live_server, h):
    """SW3-D-11: the scheduling group's field list repeated "Who · people" under every field (the list has one group),
    and two ranks sharing the code CON went unnoticed. The rows no longer repeat a single group, and Data health
    lists ranks sharing a code (the server's check) with the way to fix it."""
    from apps.api import models as m
    from apps.api.db import transaction
    from .test_browser import add_filter
    one = h.configure('categories', {'name': 'Associate Consultant · CON', 'starts_on': '2026-01-01'})['record']
    two = h.configure('categories', {'name': 'Consultant · CNS', 'starts_on': '2026-01-01'})['record']
    with transaction(h.app.state.engine, write=True) as db:  # an older collision, as imported before the check existed
        db.get(m.Category, two['id']).name = 'Consultant · CON'
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, 'configuration/setup')
        health = page.locator('.setup-health li').filter(has_text='Ranks sharing a short code')
        expect(health).to_contain_text('CON: Associate Consultant · CON, Consultant · CON')
        expect(health.get_by_role('button', name='Open ranks')).to_be_visible()
        navigate(page, 'configuration/scheduling-groups')
        page.locator('#main').get_by_role('button', name='New scheduling group').first.click()
        who = page.get_by_role('dialog', name='New scheduling group').locator('.audience-conditions')
        add_filter(who)
        who.locator('.cond-line').last.get_by_label('Filter on', exact=True).click()
        picker = page.locator('.picker')
        expect(picker.get_by_role('option').first).to_be_visible()
        expect(picker.locator('.picker-hint').filter(has_text='Who · people')).to_have_count(0)
    finally:
        context.close()
    assert one['id'] != two['id']


def test_configuration_explanations_sit_behind_help_and_no_empty_review_step(browser, live_server, h):
    """SW3-D-12: the duty type form put a paragraph under nearly every heading and listed an empty "Review" step;
    Department settings had a sentence under every field; a new tag's panel had a tagline. The explanations sit
    behind ⓘ beside their heading or label (still each control's accessible description), the empty step is gone,
    and the tag panel has no tagline."""
    from .test_browser import open_config
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        open_config(page, 'Duty types')
        page.get_by_role('button', name='New duty type').first.click()
        dialog = page.get_by_role('dialog')
        expect(dialog.locator('.stepper')).not_to_contain_text('Review')
        expect(dialog.get_by_text('All supported purposes reserve', exact=False)).to_have_count(0)
        expect(dialog.get_by_role('button', name='About Participation and time')).to_have_attribute('data-tip', re.compile('All supported purposes reserve'))
        dialog.get_by_role('button', name='Cancel').click()
        open_config(page, 'Department settings')
        pane = page.locator('.config-pane')
        expect(pane.get_by_role('button', name=re.compile('^About Leave on planning issues'))).to_be_attached()
        expect(pane.locator('small.hint').filter(has_text='Staff never see others')).to_have_count(0)
        expect(pane.get_by_label('Leave on planning issues', exact=True)).to_have_accessible_description(re.compile('Staff never see others'))
        open_config(page, 'Tags')
        page.get_by_role('button', name='New tag').first.click()
        expect(page.locator('.dock-panel')).not_to_contain_text('Name it, choose what it can label')
    finally:
        context.close()


def test_scoring_says_its_order_on_one_line_and_names_the_overtime_unit(browser, live_server, h):
    """SW3-D-13: "Matters most → least: Coverage › Rules & targets › …" wrapped over four lines in the first column, and
    "Avoid overtime: 1" had no unit. The order spans the row; the field says it is a weight like the others."""
    from .test_browser import open_config
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        open_config(page, 'Auto-schedule & scoring')
        order = page.locator('.score-order')
        expect(order).to_contain_text('Matters most → least')
        grid = page.locator('.score-grid.score-weights')
        assert order.bounding_box()['width'] > grid.bounding_box()['width'] * 0.8
        expect(page.get_by_label('Avoid overtime weight', exact=True)).to_be_visible()
    finally:
        context.close()


def test_a_person_panel_puts_ended_records_behind_show_ended_and_add_rank_knows_the_person(browser, live_server, h):
    """SW3-D-14: a person's panel listed every ended clinic eligibility (20, each with Extend) before anything else, and
    Add rank from that panel still asked which person. Ended records wait behind "Show ended (N)"; the person is said."""
    person = h.seed['people'][1]
    for name in ('Ended clinic A', 'Ended clinic B'):  # two clinics this person no longer works
        service = h.configure('services', {'name': name, 'starts_on': '2025-01-01'})['record']['id']
        h.command('credentialer', 'eligibility.create', {'person_id': person, 'service_id': service,
                                                        'starts_on': '2025-01-01', 'ends_on': '2025-06-01'})
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, f'people?open={person}')
        panel = page.locator('.dock-panel.person-panel')
        clinics = panel.locator('.ph-section').filter(has=page.get_by_role('heading', name='Clinics', exact=True))
        expect(clinics.locator('.ph-past')).to_be_hidden()
        expect(clinics.get_by_role('button', name='Extend', exact=False)).to_have_count(0)
        show = clinics.get_by_role('button', name='Show ended (2)')
        show.click()
        expect(clinics.locator('.ph-past li')).to_have_count(2)
        expect(clinics.get_by_role('button', name='Hide ended')).to_have_attribute('aria-expanded', 'true')
        panel.get_by_role('button', name='Add rank', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add rank', exact=True)
        expect(dialog.get_by_label('Person', exact=True)).to_have_count(0)
        expect(dialog.locator('.relationship-person')).to_be_visible()
    finally:
        context.close()


def test_rules_count_what_needs_attention_in_one_unit(browser, live_server, h):
    """SW3-D-20: the badge said "186 lines need attention" while the Needs attention view said 183 (issues, not lines).
    The view now counts the badge's lines."""
    from datetime import date, timedelta
    from .test_browser_rule_catalogue import flag, line
    from .test_browser_rules_database import catalogue_rule
    flag(h)
    today = date.fromisoformat(h.get('coordinator', '/api/rules/catalogue')['day'])
    a = h.seed['people'][0]
    catalogue_rule(h, 'duties_per_month', [line({'min': {'value': 1, 'level': 'mandatory'}, 'max': {'value': 20, 'level': 'mandatory'}}, note='Everyone line'),
                                           line({'max': {'value': 0, 'level': 'mandatory'}}, who={'conditions': [{'field': 'person_ids', 'values': [a]}]}, note='Alex on leave')])
    catalogue_rule(h, 'people_per_session', [line({'max': {'value': 3, 'level': 'mandatory'}}, ends_on=str(today + timedelta(days=11)))])
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        expect(main.locator('button.dt-saved .dt-view-warn')).to_have_text('3 lines need attention')
        main.locator('button.dt-saved').click()
        item = page.get_by_role('menuitemradio', name=re.compile('^Needs attention'))
        expect(item).to_contain_text('3')
        expect(item).not_to_contain_text('2')
    finally:
        context.close()


def test_operations_opens_failed_jobs_names_events_and_counts_the_whole_outbox(browser, live_server, h):
    """SW3-D-24: Operations showed "2 failed" jobs that could not be opened, raw event keys ("Absence_approved") and
    "100 events" beside "1–100 of 148". The failed count opens those jobs with their run details, events read in
    words, and the count is the server's total."""
    import json
    from datetime import datetime, timezone
    from sqlalchemy import insert
    from apps.api import models as m
    from apps.api.db import transaction
    from .test_browser import route_fetch
    w = h.workspace('Failed run rota')
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Job), [{'id': 'ops-failed-run', 'department_id': h.seed['department_id'], 'workspace_id': w['id'],
            'requested_by': h.seed['identities']['coordinator'], 'kind': 'generation', 'status': 'failed', 'error_code': 'WORKER_LOST',
            'input_fingerprint': 'ops', 'created_at': datetime(2026, 10, 2, 8, tzinfo=timezone.utc), 'input_snapshot': {'generation_engine': 'cp_sat'}}])

    def more(route):
        response = route_fetch(route)
        body = response.json()
        body['total'], body['has_more'] = 148, True
        body['outbox'] = [{**body['outbox'][0], 'kind': 'absence_approved'}, *body['outbox'][1:]] if body['outbox'] else [
            {'id': 'ev-1', 'revision': 1, 'kind': 'absence_approved', 'state': 'queued', 'attempts': 0, 'last_error': None}]
        route.fulfill(response=response, body=json.dumps(body))
    context, page = sign_in(browser, live_server, 'operator', DESKTOP)
    try:
        page.route('**/api/operations?*', more)
        navigate(page, 'system/operations')
        page.locator('.ops-queues').get_by_role('button', name=re.compile('failed')).click()
        pop = page.locator('.popover')
        expect(pop).to_contain_text('WORKER_LOST')
        expect(pop.get_by_role('button', name='Run details')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.locator('#main').get_by_text('Absence approved', exact=True).first).to_be_visible()
        expect(page.locator('.app-footer .page-status .dt-count').last).to_have_text('148 events')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def _incompatible_run(h, name):
    """A generation run that failed with SOLVER_RULE_INCOMPATIBLE, stored as the worker leaves it."""
    from datetime import datetime, timezone
    from sqlalchemy import insert
    from apps.api import models as m
    from apps.api.db import transaction
    w = h.workspace(name)
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Job), [{'id': 'ops-incompatible-run', 'department_id': h.seed['department_id'], 'workspace_id': w['id'],
            'requested_by': h.seed['identities']['coordinator'], 'kind': 'generation', 'status': 'failed',
            'error_code': 'SOLVER_RULE_INCOMPATIBLE', 'input_fingerprint': 'ops',
            'created_at': datetime(2026, 10, 2, 8, tzinfo=timezone.utc), 'input_snapshot': {'generation_engine': 'cp_sat'}}])
    return w


def test_a_run_stopped_by_an_incompatible_rule_says_which_and_opens_rules(browser, live_server, h):
    """DL-049: a run that failed with SOLVER_RULE_INCOMPATIBLE said "Check your access and try again. Reference: CODE".
    Its run details now say a rule can't be scheduled automatically and open Rules, without the code. Run details are
    the coordinator's (GET /api/jobs/{id}): opened from the rota's Auto-schedule panel."""
    from .test_browser import open_scheduling
    w = _incompatible_run(h, 'Incompatible rule rota')
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        navigate(page, 'planner')
        open_scheduling(page)
        open_rota(page, w['id'])
        page.locator('#auto-panel').get_by_role('button', name='View run details', exact=True).click()
        dialog = page.get_by_role('dialog')
        stopped = dialog.locator('.auto-outcome.err')
        expect(stopped).to_contain_text('A rule can’t be scheduled automatically')
        expect(stopped).to_contain_text('Change the lines marked on Rules, then run auto-schedule again.')
        expect(stopped.get_by_role('button', name='Open Rules')).to_be_visible()
        expect(dialog).not_to_contain_text('Check your access')
        expect(dialog).not_to_contain_text('Reference:')
    finally:
        context.close()


@pytest.mark.parametrize('role', ['operator', 'auditor'])
def test_operations_never_offers_run_details_the_server_refuses(browser, live_server, h, role):
    """The client never offers what the server refuses: GET /api/jobs/{id} is a coordinator's read, so the Operations
    queue lists a failed run for an operator or auditor without Run details."""
    _incompatible_run(h, 'Incompatible rule rota')
    context, page = sign_in(browser, live_server, role, DESKTOP)
    try:
        navigate(page, 'system/operations')
        page.locator('.ops-queues').get_by_role('button', name=re.compile('failed')).click()
        listed = page.locator('.popover').get_by_role('listitem').filter(has_text='SOLVER_RULE_INCOMPATIBLE')
        expect(listed).to_have_count(1)
        expect(page.locator('.popover').get_by_role('button', name='Run details')).to_have_count(0)
    finally:
        context.close()

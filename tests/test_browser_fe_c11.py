"""Cycle-3 SWEEP-3 frontend fixes on the coordinator's planning screens (maintenance): leaving the Replace step after Mark
absent keeps the absence (SW3-A-18); an apply or a publish shows its new state at once, with nothing offered to press
again (SW3-A-04, SW3-A-05, SW3-A-14)."""
import json
import re

import pytest
from playwright.sync_api import expect

from apps.worker.main import process_one
from .test_browser import browser, live_server, sign_in, navigate, save_dialog, route_fetch, wait_held  # noqa: F401
from .test_browser_published_changes import published, open_session
from .test_browser_autoschedule_review import _open, _run
from .test_browser_cp_sat import _joins

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 900}


def staged(h, pub):
    """The publication's hidden change list as the server keeps it: (kind, status) per change."""
    return [(c['kind'], c['status']) for c in h.get('coordinator', f'/api/publications/{pub}/pending-changes')['changes']]


def test_leaving_replace_after_mark_absent_keeps_the_absence(browser, live_server, h):
    """SW3-A-18 (S1): Mark absent on a one-place published session, Replace…, then Back: the absence is still staged
    (the server's list, the panel and the grid's open place); Assign then adds the cover on top of it."""
    _, later = published(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        pubs = []
        page.on('request', lambda r: pubs.extend(re.findall(r'/api/publications/([^/?]+)/pending-changes', r.url)))
        panel = open_session(page, h, later, person=2)
        panel.get_by_role('button', name='Change Staff 03(test) on this session').click()
        page.get_by_role('menu', name='Change Staff 03(test)').get_by_role('menuitem', name='Mark absent · Sick').click()
        expect(panel).to_contain_text('Nobody is left on this session.')
        panel.get_by_role('button', name='Replace Staff 03(test)…').click()
        expect(panel.get_by_role('heading', name='Replace Staff 03(test)')).to_be_visible()
        expect(panel.locator('.wk-cands').get_by_role('button', name=re.compile('^Assign '))).not_to_have_count(0)
        assert staged(h, pubs[-1]) == [('absence', 'draft')]  # opening Replace records nothing and removes nothing
        panel.get_by_role('button', name=re.compile('Back to session')).click()
        state = panel.locator('.pc-state')
        expect(state).to_have_count(1)
        expect(state).to_contain_text('Staff 03(test)')
        expect(page.locator('#schedule-view .pchip.gap').first).to_contain_text('open place')
        assert staged(h, pubs[-1]) == [('absence', 'draft')]
        # Replace is still offered for the staged absence; Assign adds the cover and the absence stays.
        panel.locator('.pc-foot').get_by_role('button', name='Replace Staff 03(test)…').click()
        panel.locator('.wk-cands').get_by_role('button', name=re.compile('^Assign ')).first.click()
        expect(panel.get_by_role('button', name=re.compile('^Send '))).to_be_visible()
        expect(panel.locator('.change-summary')).to_contain_text('Staff 03(test) · absent')
        assert staged(h, pubs[-1]) == [('absence', 'draft'), ('add', 'draft')]
        # SW3-A-29: with no urgent route there is nothing to choose: no lone "Routine" radio.
        assert not h.get('coordinator', f'/api/publications/{pubs[-1]}/pending-changes')['urgent_route']['active']
        expect(panel.get_by_role('radio', name=re.compile('^Routine'))).to_have_count(0)
    finally:
        context.close()


def test_an_apply_whose_answer_was_lost_still_reads_applied_with_undo(browser, live_server, h):
    """SW3-A-04/05: the apply went through but its answer was lost (a 502 from the gateway under load). The panel reads
    the run as applied: no Apply to press again, and the run row offers its Undo (never "Undo not available")."""
    w = h.shift(h.workspace('FE-C11 lost apply'), start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    w = h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[])['workspace']
    context, page, panel = _open(browser, live_server, h, w, DESKTOP, 'light')
    try:
        _run(page, h, panel)
        apply = panel.get_by_role('button', name=re.compile(r'^Apply \d+ changes?$'))
        expect(apply).to_be_visible(timeout=15000)

        answers = []

        def lost(route):
            # Only the first answer is lost: the page confirms an unknown outcome by sending the same command again
            # with the same key (8fd9aa12f, maintenance), and that replay's answer arrives.
            if (route.request.post_data_json or {}).get('kind') != 'generation.apply' or answers:
                return route.fallback()
            answers.append(route_fetch(route))  # the server applies it
            route.fulfill(status=502, content_type='application/json',
                          body=json.dumps({'error': {'code': 'BAD_GATEWAY', 'message': 'Bad gateway'}}))
        joined = _joins(page)
        page.route('**/api/commands', lost)
        apply.click()
        expect(panel.locator('.run-list').get_by_role('button', name=re.compile('^Undo Auto-schedule '))).to_be_visible()
        expect(panel.get_by_role('button', name=re.compile(r'^(Apply \d+ changes?|Check again and apply)$'))).to_have_count(0)
        expect(panel.locator('.run-no-undo')).to_have_count(0)
        assert next(j for j in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'] if j['kind'] == 'generation')['status'] == 'applied'
        page.unroute('**/api/commands', lost)
        if any(j['kind'] == 'validation' and j['status'] == 'queued' for j in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']):
            wait_held(page, joined)
            while process_one(h.app.state.engine):
                pass
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_publishing_shows_published_in_the_header_before_the_page_reads_again(browser, live_server, h):
    """SW3-A-14: once the server confirms the publish, the header says Published and offers no Publish… or Reopen to
    press again, while the reads that redraw the page are still on their way (≈70 s under load)."""
    w = h.shift(h.workspace('FE-C11 publish header'))['workspace']
    w = h.lifecycle(w, 'submit')['workspace']
    w = h.lifecycle(w, 'approve', 'approver')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        navigate(page, f"schedule/week/{w['starts_on']}?edit={w['id']}")
        bar = page.locator('.planner-bar')
        bar.get_by_role('button', name='Publish…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Publish FE-C11 publish header', exact=True)
        page.route('**/api/workspaces?*', lambda route: None)  # the reads after publishing stay unanswered
        save_dialog(dialog, 'Publish')
        expect(page.locator('.toast').filter(has_text='Published · staff are told.')).to_be_visible()
        expect(bar.locator('.rota-status')).to_contain_text('Published')
        expect(bar.get_by_role('button', name='Publish…', exact=True)).to_have_count(0)
        expect(bar.get_by_role('button', name='Reopen as draft')).to_have_count(0)
        assert h.detail(w)['workspace']['status'] == 'published'
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_fixed_assignment_on_the_integrated_rota_is_not_offered_to_change(browser, live_server, h):
    """SW3-A-10: on an integrated rota a department coordinator who is not a Rota Coordinator sees a fixed assignment as
    fixed, with no Replace (the server refuses it: SESSION_DEFINITION_AUTHORITY), and the fill panel says why."""
    from .test_browser import open_rota
    from .test_browser_p2_groups import real_rota
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    rota, shift, _ = real_rota(h, people=[{'person_id': h.seed['people'][0], 'start': start, 'end': end, 'fixed': True}])
    assert h.detail(rota)['shifts'][0]['participants'][0]['fixed']
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_rota(page, rota['id'])
        page.locator(f'#schedule-view {draft_chip(h.seed["people"][0], word=True)}').first.click()
        panel = page.locator('.wk-panel')
        expect(panel).to_contain_text('Fixed assignment. Only the Rota Coordinator changes it on this rota.')
        expect(panel.get_by_role('button', name='Replace', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_a_coordinator_who_is_not_the_rota_coordinator_sees_why_sessions_are_held(browser, live_server, h):
    """SW3-A-10 / DL-007: on the period's rota a department coordinator who is not its Rota Coordinator cannot add, remove
    or redefine sessions (the server refuses: SESSION_DEFINITION_AUTHORITY). Add session stays in place, disabled, and
    the session panel says why before anything is pressed. Decided from the rota read's can_change_fixed."""
    from .test_browser import open_rota
    from .test_browser_p2_groups import real_rota
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    rota, _, _ = real_rota(h, people=[{'person_id': h.seed['people'][0], 'start': start, 'end': end}])
    # A second session the Rota Coordinator locked: its lock mark unlocks for those who may.
    added = h.shift(rota, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[], role='admin')
    locked = added['shifts'][0]['id']
    h.command('admin', 'shift.action', {'workspace_id': rota['id'], 'shift_ids': [locked], 'action': 'lock',
        'reason': 'Kept as planned'}, revision=added['workspace']['revision'])
    assert h.get('coordinator', '/api/planning-settings')['definition']['coordinator_identity_ids'] == [h.seed['identities']['admin']]
    held = "Only the Rota Coordinator can add sessions to the period's rota."
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_rota(page, rota['id'])
        add = page.get_by_role('button', name='Add session', exact=True).first
        expect(add).to_have_attribute('aria-disabled', 'true')
        expect(add).to_have_attribute('data-tip', held)
        mark = page.locator(f'#schedule-view button.wk-lock[data-shift="{locked}"]').first
        expect(mark).to_have_attribute('aria-disabled', 'true')
        expect(mark).to_have_accessible_description("Only the Rota Coordinator can lock or unlock sessions on the period's rota.")
        page.locator(f'#schedule-view {draft_chip(h.seed["people"][0], word=True)}').first.click()
        panel = page.locator('.wk-panel')
        panel.get_by_role('button', name='Whole session', exact=True).click()
        expect(panel.locator('.sessions-held')).to_have_text(held)
        lock = panel.get_by_role('button', name='Lock', exact=True)
        expect(lock).to_have_attribute('aria-disabled', 'true')
        expect(lock).to_have_attribute('data-tip', "Only the Rota Coordinator can lock or unlock sessions on the period's rota.")
        # The edit form: what defines the session is held in place with why; who works it stays editable.
        panel.get_by_role('button', name='Edit session', exact=True).click()
        form = page.locator('.dock-panel').filter(has_text='Edit session and participants')
        define = "Only the Rota Coordinator can change a session's type, time, place or clinics on the period's rota."
        expect(form.locator('#session-define-held')).to_have_text(define)
        for label in ('Duty type', 'Location', 'Session start', 'Session end'):
            field = form.get_by_label(label, exact=True)
            expect(field).to_be_disabled()
            expect(field).to_have_attribute('aria-describedby', 'session-define-held')
        expect(form.get_by_role('button', name='Add participant', exact=True)).to_be_enabled()
        page.keyboard.press('Escape')
        # The selection bar: Close and Remove stay in place, disabled, each saying why.
        from .test_browser import pick_view
        pick_view(page, 'List')
        page.locator('tbody .bulk-check').first.check()
        bar = page.locator('#selection-bar')
        remove = bar.get_by_role('button', name='Remove', exact=True)
        expect(remove).to_have_attribute('aria-disabled', 'true')
        expect(remove).to_have_attribute('data-tip', "Only the Rota Coordinator can remove sessions from the period's rota.")
        close = bar.get_by_role('button', name='Close sessions', exact=True)
        expect(close).to_have_attribute('aria-disabled', 'true')
        expect(close).to_have_attribute('data-tip', "Only the Rota Coordinator can close or reopen sessions on the period's rota.")
    finally:
        context.close()


def test_a_private_draft_without_a_rota_says_why_merge_is_off(browser, live_server, h):
    """SW3-A-11 (D41): with no Rota Coordinators a private draft has nothing to merge into. Merge into rota… is there,
    off, and says why and who can fix it, in the same words as the group form (SW3-D-03)."""
    from .test_browser import open_rota
    w = h.shift(h.workspace('FE-C11 no rota'))['workspace']
    draft = h.command('coordinator', 'workspace.create', {'name': 'FE-C11 no rota · draft 1', 'starts_on': w['starts_on'], 'ends_on': w['ends_on'],
        'visibility': 'private', 'purpose': 'sandbox', 'fork_from': {'workspace_id': w['id'], 'revision': w['revision']}})['workspace']
    assert not h.get('admin', '/api/planning-settings')['definition']['coordinator_identity_ids']
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_rota(page, draft['id'])
        merge = page.locator('.planner-bar').get_by_role('button', name='Merge into rota…')
        expect(merge).to_have_attribute('aria-disabled', 'true')
        expect(merge).to_have_attribute('data-tip', re.compile('^No Rota Coordinators yet, so this period has no rota to merge into. An admin'))
        merge.focus()  # aria-disabled keeps it focusable; pressing it says why (Playwright will not click it)
        page.keyboard.press('Enter')
        expect(page.locator('.toast').filter(has_text='No Rota Coordinators yet')).to_be_visible()
        expect(page.locator('.dock-panel.merge-panel')).to_have_count(0)
    finally:
        context.close()


def test_break_rule_is_offered_only_when_the_server_allows_it(browser, live_server, h):
    """SW3-A-15: "Break rule and assign" follows the server's override_allowed; a candidate without that answer (or with
    false: a double booking, a safety rule) is not offered it. SW3-A-21: the row has no kind badge."""
    from .test_browser_be_wave3_frontend import on_leave
    from .test_browser import open_rota
    w, person, who = on_leave(h)

    def unanswered(route):
        reply = route_fetch(route)
        body = reply.json()
        for c in body.get('candidates', []):
            c.pop('override_allowed', None)
        route.fulfill(response=reply, json=body)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        page.route('**/api/planner/fill-options', unanswered)
        open_rota(page, w['id'])
        page.locator('.pchip.gap').first.click()
        panel = page.get_by_role('dialog').filter(has_text='Can work this session')
        row = panel.get_by_role('region', name=re.compile(r'^Not available · \d+$')).locator('li').filter(has_text=who)
        expect(row).to_be_visible(timeout=20000)
        expect(row.get_by_role('button', name=f'Break rule and assign {who}', exact=True)).to_have_count(0)
        # SW3-A-21: the group heading says why; no uppercase kind badge (LEAVE, CLASH) that reads like a job title.
        expect(row).not_to_contain_text('LEAVE')
        expect(row.locator('.wk-kind')).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_an_empty_month_offers_new_rota_once_with_that_month(browser, live_server, h):
    """SX-14: an empty month shows one New rota (the empty state's; the toolbar's goes while it shows), and it offers that
    month's dates (SW3-A-02, as the toolbar's does)."""
    from datetime import datetime
    from .test_browser_schedule_views import show
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        show(page, 'schedule/month/2028-02-01?edit=none')
        new = page.locator('#main').get_by_role('button', name='New rota', exact=True)
        expect(new).to_have_count(1)
        expect(page.locator('#main .empty-state').get_by_role('button', name='New rota', exact=True)).to_be_visible()
        new.click()
        dialog = page.get_by_role('dialog', name='New rota', exact=True)
        day = lambda label: datetime.strptime(dialog.get_by_label(label, exact=True).input_value(), '%a %d %b %Y').date().isoformat()
        assert (day('First day'), day('Last day')) == ('2028-02-01', '2028-02-29')
    finally:
        context.close()


def test_a_merge_counts_a_replace_as_one_change(browser, live_server, h):
    """SX-26: one Replace in a private draft is one change in the merge preview (its count and its row), not the
    assignment rows behind it."""
    from .test_planning_groups_drafts import integrated
    from .test_browser_p2_groups import fork, put, open_merge, merge
    mine, other = h.seed['people'][:2]
    rota, shift, _ = integrated(h, people=[mine], membership={'match': 'all', 'conditions': [{'field': 'person_ids', 'values': [mine, other]}]})
    draft = fork(h, rota)
    draft = put(h, draft, h.detail(draft)['shifts'][0], [other])
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        panel = open_merge(page, draft, h)
        expect(panel.get_by_role('status').first).to_contain_text('All 1 change fit')
        expect(panel.get_by_role('button', name='Merge 1 change')).to_be_enabled()
        expect(panel.locator('.merge-fits li')).not_to_contain_text('assignment change')
        merge(panel, h, 'Merge 1 change')
        expect(page.get_by_text(f'1 change merged into {rota["name"]}.')).to_be_visible()
    finally:
        context.close()


def test_after_a_replace_the_panel_names_who_works_the_session(browser, live_server, h):
    """SW3-A-09: right after Replace → Assign, the panel says every place is filled and lists who works the session (the
    person just put in), not only "Add another person anyway"."""
    from .test_browser import open_rota
    w = h.shift(h.workspace('FE-C11 replace'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_rota(page, w['id'])
        page.locator(f'#schedule-view {draft_chip(h.seed["people"][0], word=True)}').first.click()
        panel = page.locator('.wk-panel')
        panel.get_by_role('button', name='Replace', exact=True).click()
        assign = panel.locator('.wk-cands').get_by_role('button', name=re.compile('^Assign ')).first
        who = assign.get_attribute('aria-label').removeprefix('Assign ').removesuffix(' with a warning')
        assign.click()
        expect(panel).to_contain_text('Every place on this session is filled.')
        expect(panel.locator('.wk-session')).to_contain_text(who)
    finally:
        context.close()

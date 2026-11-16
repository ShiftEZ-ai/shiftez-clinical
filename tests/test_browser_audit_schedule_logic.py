"""Schedule scope, workflow promises and recovery use the same facts as their commands."""
import re
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .test_browser import auto_setup, wait_run_created, filter_pick, browser, live_server, sign_in, navigate, open_scheduling, toolbar  # noqa: F401
from .test_resolve_issues import leave_rota

pytestmark = pytest.mark.browser


def local_start(shift):
    return datetime.fromisoformat(shift['start']).astimezone(ZoneInfo('Asia/Riyadh'))


@pytest.mark.parametrize('width', [1440, 390])
def test_stale_issues_require_recheck_and_renewed_selection(browser, live_server, h, width):
    w = leave_rota(h)
    leave = h.get('coordinator', '/api/availability')['availability'][0]
    h.command('coordinator', 'availability.retract', {'reason': 'Leave cancelled'}, leave['id'], leave['revision'])
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 950})
    try:
        navigate(page, f"schedule/issues?draft={w['id']}&view=checks")
        check = page.get_by_role('button', name='Check rota', exact=True)
        expect(check).to_be_visible()
        expect(check.locator('..')).to_contain_text('The rota inputs changed. Check the rota again before resolving its issues.')
        spacing = check.evaluate('''button => {
            const notice=button.parentElement,range=document.createRange();range.selectNodeContents(notice.firstChild);
            const text=range.getBoundingClientRect(),action=button.getBoundingClientRect(),box=notice.getBoundingClientRect();
            return {gap:Math.max(action.left-text.right,action.top-text.bottom),
                contained:action.left>=box.left&&action.right<=box.right&&action.left>=0&&action.right<=innerWidth};
        }''')
        assert spacing['gap'] >= 4 and spacing['contained'], spacing
        expect(page.locator('.issues-list').get_by_role('checkbox')).to_have_count(0)
        expect(page.get_by_role('button', name='Unassign', exact=True)).to_have_count(0)
        check.focus()
        expect(check).to_be_focused()
        check.press('Enter')
        expect(page.locator('.issues-counts')).not_to_contain_text('needs checking')
        expect(page.locator('.issues-list')).not_to_contain_text('Annual')
        assert all(p['person_id'] == h.seed['people'][0] for s in h.detail(w)['shifts'] for p in s['participants'])
        assert not any(f['code'] == 'UNAVAILABLE' for f in h.detail(w)['workspace']['validation']['findings'])
    finally:
        context.close()


def test_undo_after_a_stale_issue_refusal_restores_current_remedies(browser, live_server, h):
    w = leave_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/issues?draft={w['id']}&view=checks")
        rows = page.locator('.issues-list tbody tr[data-row]').filter(has_text='Annual')
        rows.first.get_by_role('checkbox').check()
        page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Unassign', exact=True).click()
        expect(page.get_by_role('button', name='Undo last batch', exact=True)).to_be_visible()
        # Model a late server refusal without a live-update refresh overtaking this exact recovery path.
        refused = False
        def stale_once(route):
            nonlocal refused
            if not refused and route.request.post_data_json['kind'] == 'issues.resolve':
                refused = True
                route.fulfill(status=409, content_type='application/json', body=json.dumps({'error': {
                    'code': 'STALE_CHECK', 'message': 'The rota inputs changed. Check the rota again.'}}))
            else:
                route.continue_()
        page.route('**/api/commands', stale_once)
        rows.filter(has=page.get_by_role('checkbox')).first.get_by_role('checkbox').check()
        page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Unassign', exact=True).click()
        expect(page.locator('.issues-counts')).to_contain_text('needs checking')
        page.get_by_role('button', name='Undo last batch', exact=True).click()
        expect(page.locator('.issues-counts')).not_to_contain_text('needs checking')
        expect(page.get_by_role('button', name='Check rota', exact=True)).to_be_hidden()
        rows.filter(has=page.get_by_role('checkbox')).first.get_by_role('checkbox').check()
        page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Unassign', exact=True).click()
        expect(page.locator('.issues-counts')).to_contain_text('1 resolved')
    finally:
        context.close()


@pytest.mark.parametrize('dimension', ['Rank', 'Programme'])
def test_schedule_membership_filters_follow_duty_local_dates(browser, live_server, h, dimension):
    selected = h.configure('categories' if dimension == 'Rank' else 'programmes',
                           {'name': 'Dated audit membership', 'starts_on': '2020-01-01'})['record']
    alex, jordan = h.seed['people'][:2]
    for person, start, end in [(alex, '2026-10-04', '2026-10-05'), (jordan, '2026-10-06', '2026-10-07')]:
        values = {'person_id': person, 'starts_on': start, 'ends_on': end}
        if dimension == 'Rank':
            h.command('coordinator', 'membership.create', {**values, 'category_id': selected['id']})
        else:
            h.configure('programme_memberships', {**values, 'programme_id': selected['id']}, role='coordinator')
    w = h.workspace('Dated membership filters')
    for day in (4, 5, 6):
        # 21:30 UTC belongs to the NEXT day in the department timezone.
        w = h.shift(w, f'2026-10-{day:02}T00:30:00+03:00', f'2026-10-{day:02}T02:00:00+03:00', people=[alex, jordan])['workspace']
    detail = h.detail(w)
    expected = {s['id'] for s in detail['shifts'] if local_start(s).day in (4, 6)}
    assert len(expected) == 2  # start date inclusive, end exclusive; current facts differ by person
    h.lifecycle(w, 'validate')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/list/2026-10-04?edit={w['id']}")
        expect(page.locator('#schedule-view tr[data-session]')).to_have_count(3)
        filter_pick(toolbar(page.locator('.planner-toolbar'), 'Filter'), dimension, selected['name'])
        page.keyboard.press('Escape')
        expect(page.locator('#schedule-view tr[data-session]')).to_have_count(2)
        assert set(page.locator('#schedule-view tr[data-session]').evaluate_all('rows => rows.map(r => r.dataset.session)')) == expected
    finally:
        context.close()


@pytest.mark.parametrize('person', [0, 1], ids=['other-participant', 'same-participant'])
def test_schedule_person_facets_must_hold_for_one_participant(browser, live_server, h, person):
    """Person + Rank are read from ONE participant (as the server's evaluator does): a session of A and B, where only B
    holds the rank, does not match "Person A, Rank X"; it matches "Person B, Rank X"."""
    rank = h.configure('categories', {'name': 'Facet audit consultant', 'starts_on': '2020-01-01'})['record']
    alex, jordan = h.seed['people'][:2]
    h.command('coordinator', 'membership.create', {'person_id': jordan, 'category_id': rank['id'], 'starts_on': '2020-01-01'})
    w = h.workspace('One participant holds every person facet')
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[alex, jordan])['workspace']
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[alex])['workspace']
    two = next(s['id'] for s in h.detail(w)['shifts'] if len(s['participants']) == 2)
    name = next(p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == [alex, jordan][person])
    h.lifecycle(w, 'validate')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/list/2026-10-04?edit={w['id']}")
        sessions = page.locator('#schedule-view tr[data-session]')
        expect(sessions).to_have_count(2)
        pop = toolbar(page.locator('.planner-toolbar'), 'Filter')
        filter_pick(pop, 'Person', name)
        filter_pick(pop, 'Rank', rank['name'])
        page.keyboard.press('Escape')
        expect(sessions).to_have_count(person)
        if person:
            assert sessions.evaluate_all('rows => rows.map(r => r.dataset.session)') == [two]
    finally:
        context.close()


@pytest.mark.parametrize('matching', [True, False], ids=['intersection', 'no-match'])
def test_selected_auto_schedule_intersects_dates_clinics_and_unlocked_sessions(browser, live_server, h, matching):
    w = h.workspace('Selected scope')
    for day, service, hour in [(4, 0, 8), (5, 0, 8), (4, 1, 13), (4, 0, 13)]:
        w = h.shift(w, f'2026-10-{day:02}T{hour:02}:00:00+03:00', f'2026-10-{day:02}T{hour+3:02}:00:00+03:00',
                    people=[], service=service)['workspace']
    duties = h.detail(w)['shifts']
    locked = next(s for s in duties if s['service_ids'] == [h.seed['services'][0]] and local_start(s).day == 4 and local_start(s).hour == 13)
    w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [locked['id']], 'action': 'lock', 'reason': 'Keep locked'}, revision=w['revision'])['workspace']
    before = {s['id']: s['participants'] for s in h.detail(w)['shifts']}
    wanted = next(s['id'] for s in duties if s['service_ids'] == [h.seed['services'][0]] and local_start(s).day == 4 and local_start(s).hour == 8)
    h.lifecycle(w, 'validate')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/list/2026-10-04?edit={w['id']}")
        page.locator('#schedule-view').get_by_role('checkbox', name='Select all sessions shown', exact=True).check()
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        auto_setup(panel)  # D76: the setup's fields open under Change
        panel.get_by_label('Assign into', exact=True).select_option('selected')
        panel.get_by_label('From', exact=True).fill('2026-10-04' if matching else '2026-10-07')
        panel.get_by_label('To', exact=True).fill('2026-10-04' if matching else '2026-10-07')
        panel.get_by_role('button', name=re.compile('^Clinics:')).click()
        service = next(s['name'] for s in h.get('coordinator', '/api/catalogues')['services'] if s['id'] == h.seed['services'][0])
        page.locator('.picker').get_by_role('option', name=service, exact=True).check()
        page.keyboard.press('Escape')
        if not matching:
            # SR-28: Run is disabled with its reason said beside it, before it is pressed.
            expect(panel.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_disabled()
            expect(panel.locator('.auto-why')).to_have_text('No selected sessions in these dates and clinics.')
            assert h.get('coordinator', '/api/jobs')['jobs'] == []
            return
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar')).to_be_visible()
        wait_run_created(page)
        job = h.get('coordinator', '/api/jobs')['jobs'][0]
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Job, job['id']).input_snapshot['generation_session_ids'] == [wanted]
        from apps.worker.main import process_one
        process_one(h.app.state.engine)
        panel.get_by_role('button', name='Review latest result', exact=True).click(timeout=15000)
        page.get_by_role('dialog', name='Auto-schedule results').get_by_role('button', name=re.compile(r'^Apply (the change|all \d+ changes)$')).click()
        expect(page.get_by_role('dialog', name='Auto-schedule results')).to_have_count(0)
        after = h.detail(w)
        assert next(s for s in after['shifts'] if s['id'] == wanted)['participants']
        assert all(s['participants'] == before[s['id']] for s in after['shifts'] if s['id'] != wanted)
    finally:
        context.close()


@pytest.mark.parametrize('role', ['coordinator', 'approver'])
def test_publish_only_offers_permitted_cleanup_and_labels_audit_reason(browser, live_server, h, role):
    competitors = {status: h.shift(h.workspace('Competing ' + status))['workspace'] for status in ('draft', 'submitted', 'approved')}
    for status in ('submitted', 'approved'):
        competitors[status] = h.lifecycle(competitors[status], 'submit')['workspace']
        if status == 'approved':
            competitors[status] = h.lifecycle(competitors[status], 'approve', 'approver')['workspace']
    w = h.shift(h.workspace('Publication scope'))['workspace']
    w = h.lifecycle(w, 'submit')['workspace']
    w = h.lifecycle(w, 'approve', 'approver')['workspace']
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.get_by_role('button', name='Publish…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Publish Publication scope', exact=True)
        expect(dialog.get_by_label('Note (optional)', exact=True)).to_be_visible()  # ME-25: a note, not a decision
        expect(dialog.locator('textarea')).to_have_count(1)
        expect(dialog).not_to_contain_text('Kept in audit history')  # audit is implied (G-24)
        expect(dialog.get_by_label('Note to staff', exact=True)).to_have_count(0)
        for status in ('submitted', 'approved'):
            expect(dialog.get_by_label(f'Discard Competing {status} after publishing', exact=True)).to_have_count(0)
        discard = dialog.get_by_label('Discard Competing draft after publishing', exact=True)
        if role == 'coordinator':
            discard.check()
        else:
            expect(discard).to_have_count(0)
        reason = f'Audit reason from {role}'
        dialog.get_by_label('Note (optional)', exact=True).fill(reason)
        dialog.get_by_role('button', name='Publish', exact=True).click()
        dialog.locator('button.armed').click()
        expect(dialog).to_have_count(0, timeout=15000)
        expect(page.locator('.toast').filter(has_text='Published · staff are told.')).to_be_visible()
        assert h.detail(w)['workspace']['status'] == 'published'
        assert bool(h.detail(competitors['draft'])['workspace']['archived_at']) == (role == 'coordinator')
        for status in ('submitted', 'approved'):
            assert h.detail(competitors[status])['workspace']['status'] == status
            assert not h.detail(competitors[status])['workspace']['archived_at']
        with transaction(h.app.state.engine) as db:
            assert db.scalar(select(m.AuditEvent.reason).where(m.AuditEvent.action == 'workspace.publish', m.AuditEvent.target_id == w['id'])) == reason
    finally:
        context.close()


def test_submit_reason_is_optional_audit_context(browser, live_server, h):
    w = h.shift(h.workspace('Submission audit context'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.get_by_role('button', name='Submit for review…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Submit Submission audit context for review', exact=True)
        # ME-25: one optional note, said to be for the approver (who sees it quoted in the review).
        expect(dialog.get_by_label('Note for the approver (optional)', exact=True)).to_be_visible()
        expect(dialog.locator('textarea')).to_have_count(1)
        expect(dialog).not_to_contain_text('Kept in audit history')  # audit is implied (G-24)
        dialog.get_by_role('button', name='Submit for review', exact=True).click()
        expect(dialog).to_have_count(0, timeout=15000)
        assert h.detail(w)['workspace']['status'] == 'submitted'
        with transaction(h.app.state.engine) as db:
            assert db.scalar(select(m.AuditEvent.reason).where(m.AuditEvent.action == 'workspace.submit', m.AuditEvent.target_id == w['id'])) == 'Routine update'
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_archived_drafts_can_restore_beyond_the_first_page(browser, live_server, h, width):
    old = h.shift(h.workspace('Oldest archived audit draft'))['workspace']
    original_shifts = h.detail(old)['shifts']
    h.command('coordinator', 'workspace.archive', {'reason': 'Test recovery'}, old['id'], old['revision'])
    with transaction(h.app.state.engine) as db:
        source = db.get(m.Workspace, old['id'])
        for i in range(100):
            db.add(m.Workspace(department_id=source.department_id, name=f'Newer archived {i:03}', starts_on=source.starts_on,
                ends_on=source.ends_on, created_by=source.created_by, last_edited_by=source.last_edited_by,
                archived_at=m.now(), created_at=source.created_at + timedelta(seconds=i + 1)))
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 950})
    try:
        navigate(page, 'schedule/week/2026-11-01?edit=none')
        page.get_by_role('button', name=re.compile(r'^(Switch draft · |Choose a draft)')).click()
        page.get_by_role('dialog', name='Drafts', exact=True).get_by_role('button', name='Archived rotas', exact=True).click()
        dialog = page.get_by_role('region', name='Archived rotas', exact=True)  # a list: a docked panel (C26)
        expect(dialog).to_contain_text('101 archived rotas · page 1')
        expect(dialog.get_by_role('button', name='Previous archived rotas', exact=True)).to_be_disabled()
        expect(dialog.get_by_text(old['name'], exact=True)).to_have_count(0)
        dialog.get_by_role('button', name='Next archived rotas', exact=True).click()
        expect(dialog).to_contain_text('101 archived rotas · page 2')
        expect(dialog.get_by_role('button', name='Next archived rotas', exact=True)).to_be_disabled()
        dialog.locator('.discarded-row').filter(has_text=old['name']).get_by_role('button', name='Restore ' + old['name'], exact=True).click()
        expect(dialog).to_have_count(0)
        # Both draft and its visible dates are committed before close; do not wait for the subsequent redraw.
        assert page.url.endswith(f"#schedule/week/2026-10-04?edit={old['id']}")
        page.reload()
        restored = page.get_by_role('button', name='Switch draft · ' + old['name'], exact=True)
        expect(restored).to_be_visible()
        session = page.get_by_role('region', name='Week view', exact=True).locator(
            f'button[data-rk*="{original_shifts[0]["id"]}"][data-person~="{h.seed["people"][0]}"]')
        expect(session).to_be_visible()
        expect(session).to_have_accessible_name(re.compile(re.escape(original_shifts[0]['type_snapshot']['name'])))
        restored_data = h.detail(old)
        assert restored_data['workspace']['archived_at'] is None
        assert restored_data['shifts'] == original_shifts
        archived = h.get('coordinator', '/api/workspaces?archived=true&limit=100')
        assert archived['total'] == 100 and all(w['id'] != old['id'] for w in archived['workspaces'])
    finally:
        context.close()

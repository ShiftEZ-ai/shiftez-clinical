"""Archive, Restore and Delete draft reviewed: one action per rota state named by
the server's allowed_actions, the same-button confirm with a reason, Undo after Archive, the server's refusal words, and
the Archived rotas list with who, when, why and Restore. Archived rotas leave the staff schedule until restored."""
import re
from datetime import date

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .test_browser import browser, live_server, sign_in, open_rota, route_fetch  # noqa: F401
from .test_browser_schedule import at, october, show

pytestmark = pytest.mark.browser
ARMED = re.compile(r'\barmed\b')


def staff_sources(h):
    """The publications in force that staff read for 4–13 Oct (the schedule window's sources)."""
    return {s['id'] for s in h.get('staff', '/api/schedule/sessions?start=2026-10-04&end=2026-10-14')['sources']}


def confirm(dialog, reason=None, *, armed=True):
    """Fill the optional reason, then submit once or confirm with an armed second press."""
    if reason is not None:
        dialog.get_by_label('Reason').fill(reason)
    submit = dialog.locator('button[type=submit]')
    expect(submit).not_to_have_class(ARMED)
    submit.click()
    if armed:
        expect(submit).to_have_class(ARMED)
        submit.click()


def rota_tools(page):
    page.get_by_role('button', name='Rota tools', exact=True).click()
    return page.get_by_role('menu', name='Rota tools')


def test_direct_link_to_discarded_draft_offers_restore_instead_of_submit(browser, live_server, h):
    w = h.workspace('Discarded draft')
    h.command('coordinator', 'workspace.delete_draft', {'reason': 'Unused draft'}, w['id'], w['revision'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, w['id'])
        bar = page.locator('.planner-bar')
        expect(bar.locator('.rota-status')).to_have_text('Archived')
        submit = bar.get_by_role('button', name='Submit for review…', exact=True)
        expect(submit).to_have_count(0)
        bar.get_by_role('button', name='Restore', exact=True).click()
        expect(bar.locator('.rota-status')).to_have_text('Draft')
        expect(submit).to_be_visible()
        assert h.detail(w)['workspace']['archived_at'] is None
    finally:
        context.close()


def test_archive_a_published_rota_hides_it_from_staff_until_restored(browser, live_server, h):
    pid, _ = october(h)
    assert pid in staff_sources(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 950})
    try:
        show(page, 'schedule/week/2026-10-04?edit=none')
        page.get_by_role('button', name='Schedule tools', exact=True).click()
        page.get_by_role('menu', name='Schedule tools').get_by_role('menuitem', name='Archive…', exact=True).click()
        dialog = page.get_by_role('dialog', name=re.compile('^Archive · October rota'))
        expect(dialog).to_contain_text('Staff will stop seeing this rota.')
        # A published rota needs a reason: the empty form does not arm.
        submit = dialog.locator('button[type=submit]')
        submit.click()
        expect(submit).not_to_have_class(ARMED)
        confirm(dialog, 'Replaced by the corrected rota')
        expect(page.locator('.toast', has_text='October rota archived.')).to_be_visible()
        assert pid not in staff_sources(h)

        # The Archived rotas list (the draft switcher's foot): who, when, why, and Restore.
        page.get_by_role('button', name=re.compile(r'^(Switch draft · |Choose a draft)')).click()
        page.get_by_role('dialog', name='Drafts', exact=True).get_by_role('button', name='Archived rotas', exact=True).click()
        panel = page.get_by_role('region', name='Archived rotas', exact=True)
        row = panel.locator('.discarded-row').filter(has_text='October rota')
        expect(row).to_contain_text('Archived by you')
        expect(row).to_contain_text('Replaced by the corrected rota')
        expect(row).to_contain_text('Published')
        row.get_by_role('button', name='Restore October rota', exact=True).click()
        expect(page.locator('.toast', has_text='October rota restored.')).to_be_visible()
        expect(panel).to_have_count(0)
        assert pid in staff_sources(h)
    finally:
        context.close()


def test_archive_offers_undo_that_restores(browser, live_server, h):
    w = h.lifecycle(h.shift(h.workspace('Submitted rota', '2026-11-01', '2026-11-08'), *at(1, month=11))['workspace'], 'submit')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, w['id'])
        menu = rota_tools(page)
        expect(menu.get_by_role('menuitem', name='Delete draft…', exact=True)).to_have_count(0)
        menu.get_by_role('menuitem', name='Archive…', exact=True).click()
        dialog = page.get_by_role('dialog', name=re.compile('^Archive · Submitted rota'))
        expect(dialog).to_contain_text('Its review is withdrawn.')
        confirm(dialog, 'Planned again next month')
        toast = page.locator('.toast', has_text='Submitted rota archived.')
        expect(toast).to_be_visible()
        assert h.detail(w)['workspace']['archived_at'] is not None
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Undone.', exact=False)).to_be_visible()
        restored = h.detail(w)['workspace']
        assert restored['archived_at'] is None and restored['status'] == 'submitted'
    finally:
        context.close()


@pytest.mark.parametrize('baseline', [True, False], ids=['recoverable', 'legacy'])
def test_delete_draft_has_exactly_one_notification_with_undo_only_when_recoverable(browser, live_server, h, baseline):
    if baseline:
        w = h.shift(h.workspace('Unused draft', '2026-11-01', '2026-11-08'), *at(1, month=11))['workspace']
    else:
        # No command creates a legacy draft: seed the historical row without deleting immutable baseline evidence.
        with transaction(h.app.state.engine, write=True) as db:
            actor = h.seed['identities']['coordinator']
            row = m.Workspace(id=m.uid(), department_id=h.seed['department_id'], created_by=actor, last_edited_by=actor,
                name='Unused draft', starts_on=date(2026, 11, 1), ends_on=date(2026, 11, 8),
                status='draft', revision=1, content_revision=1)
            db.add(row)
            db.flush()
            w = {'id': row.id}
    assert bool(h.detail(w)['workspace'].get('baseline')) is baseline
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, w['id'])
        menu = rota_tools(page)
        expect(menu.get_by_role('menuitem', name='Archive…', exact=True)).to_have_count(0)
        menu.get_by_role('menuitem', name='Delete draft…', exact=True).click()
        dialog = page.get_by_role('dialog', name=re.compile('^Delete draft · Unused draft'))
        if baseline:
            expect(dialog).not_to_contain_text('This can’t be undone.')
        else:
            expect(dialog).to_contain_text('This can’t be undone.')
        confirm(dialog)  # Both draft deletions arm; a draft's reason is optional.
        expect(dialog).to_be_hidden()
        messages = page.get_by_role('region', name='Messages', exact=True)
        expect(messages.get_by_role('status')).to_have_count(1)
        expect(messages.get_by_role('alert')).to_have_count(0)
        toast = messages.get_by_role('status')
        expect(toast).to_be_visible()
        expect(toast.get_by_text('Unused draft removed.' if baseline else 'Unused draft deleted.', exact=True)).to_be_visible()
        expect(toast.get_by_role('button', name='Undo', exact=True)).to_have_count(1 if baseline else 0)
        ids = {r['id'] for r in h.get('coordinator', '/api/workspaces?include_archived=true&limit=100')['workspaces']}
        assert (w['id'] in ids) is baseline
        if baseline:
            assert h.detail(w)['workspace']['archived_at'] is not None
            toast.get_by_role('button', name='Undo', exact=True).click()
            expect(page.get_by_text('Undone.', exact=False)).to_be_visible()
            assert h.detail(w)['workspace']['archived_at'] is None
            assert h.detail(w)['workspace']['baseline']
        else:
            h.get('coordinator', '/api/workspaces/' + w['id'], status=404)
    finally:
        context.close()


@pytest.mark.parametrize('status,code,words', [
    (409, 'ROTA_JOB_RUNNING', 'A check or auto-schedule is running on this rota. Try again when it finishes.'),
    (403, 'GROUP_EDIT_AUTHORITY', 'Only the Rota Coordinator can change the integrated rota lifecycle'),
])
def test_a_refusal_shows_the_server_words(browser, live_server, h, status, code, words):
    w = h.shift(h.workspace('Busy draft', '2026-11-01', '2026-11-08'), *at(1, month=11))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, w['id'])

        def refuse(route):
            if 'workspace.delete_draft' in (route.request.post_data or ''):
                return route.fulfill(status=status, json={'error': {'code': code, 'message': words}})
            return route.continue_()
        page.route('**/api/commands', refuse)
        rota_tools(page).get_by_role('menuitem', name='Delete draft…', exact=True).click()
        dialog = page.get_by_role('dialog', name=re.compile('^Delete draft'))
        confirm(dialog)
        expect(dialog).to_contain_text(words)
        assert h.detail(w)['workspace']['id'] == w['id']
    finally:
        context.close()


def test_only_the_actions_the_server_allows_are_offered(browser, live_server, h):
    w = h.lifecycle(h.shift(h.workspace('Review rota', '2026-11-01', '2026-11-08'), *at(1, month=11))['workspace'], 'submit')['workspace']
    assert 'workspace.archive' not in (h.get('approver', '/api/workspaces/' + w['id'])['workspace'].get('allowed_actions') or [])
    context, page = sign_in(browser, live_server, 'approver')
    try:
        open_rota(page, w['id'])
        menu = rota_tools(page)
        expect(menu).to_be_visible()
        expect(menu.get_by_role('menuitem', name=re.compile(r'^(Archive|Delete draft|Restore)…'))).to_have_count(0)
    finally:
        context.close()


def test_a_coordinator_without_edit_rights_is_not_offered_the_action(browser, live_server, h):
    """The period's integrated rota: only its Rota Coordinator (here the admin) may archive or delete it."""
    from .test_planning_groups_drafts import integrated
    w, _, _ = integrated(h)
    assert h.get('coordinator', '/api/workspaces/' + w['id'])['workspace']['allowed_actions'] == []
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, w['id'])
        menu = rota_tools(page)
        expect(menu).to_be_visible()
        expect(menu.get_by_role('menuitem', name=re.compile(r'^(Archive|Delete draft|Restore)…'))).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('listed', ['without its actions', 'absent'])
def test_the_rota_s_own_read_names_its_action_past_the_list_s_first_page(browser, live_server, h, listed):
    """A rota past the list's first 50 has no row (or one without allowed_actions): the rota's own read, whole or
    compact, carries them, so its menu still offers the action."""
    w = h.shift(h.workspace('Far rota', '2026-11-01', '2026-11-08'), *at(1, month=11))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        def page_one(route):
            response = route_fetch(route)
            if response.status != 200:
                return route.fulfill(response=response)
            body = response.json()
            rows = [r for r in body['workspaces'] if r['id'] != w['id']] if listed == 'absent' else [
                {k: v for k, v in r.items() if k != 'allowed_actions'} if r['id'] == w['id'] else r for r in body['workspaces']]
            route.fulfill(response=response, json={**body, 'workspaces': rows})
        page.route(re.compile(r'/api/workspaces\?'), page_one)
        open_rota(page, w['id'])
        expect(rota_tools(page).get_by_role('menuitem', name='Delete draft…', exact=True)).to_be_visible()
    finally:
        context.close()


def test_archived_rotas_name_who_archived_them(browser, live_server, h):
    w = h.lifecycle(h.shift(h.workspace('Admin archived rota', '2026-11-01', '2026-11-08'), *at(1, month=11))['workspace'], 'submit')['workspace']
    h.command('admin', 'workspace.archive', {'reason': 'Superseded by the new plan'}, w['id'], w['revision'])
    name = h.get('admin', '/api/me')['display_name']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, 'schedule/week/2026-11-01?edit=none')
        page.get_by_role('button', name=re.compile(r'^(Switch draft · |Choose a draft)')).click()
        page.get_by_role('dialog', name='Drafts', exact=True).get_by_role('button', name='Archived rotas', exact=True).click()
        row = page.get_by_role('region', name='Archived rotas', exact=True).locator('.discarded-row').filter(has_text='Admin archived rota')
        expect(row).to_contain_text(f'Archived by {name}')
        expect(row).to_contain_text('Superseded by the new plan')
    finally:
        context.close()

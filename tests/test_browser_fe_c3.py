"""Cycle-3 frontend fixes (journey root-cause audit, maintenance), on the real API: the approver reads what an amendment changes
(was → now, the reason, the submit note) on the review strip and in Approve, as Publish shows it, counted in the Fix
list's causes, with the changed session marked on the grid (ME-26, ME-27); Home names an amendment to review (ME-28);
sending changes says so on the chip from the reply (ME-21); Publish confirms on the page, not inside a closing dialog
(ME-24); the shared reason box is "Reason" (F-maintenance); deciding your own route proposal says so (F20); a phone sheet is not
covered by a message about the page behind it (F18); the rule lines table keeps Valid and Reach on screen (F-R2)."""
import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, open_rota, navigate  # noqa: F401
from .test_browser_schedule import show
from .test_browser_published_changes import published
from .test_published_changes import change, issued

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 900}
SHOTS = os.getenv('FE_C3_SHOTS')  # a folder: the fixed screens, for the handoff's after pictures


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f'after-{name}-1440l.png'))


def amendment(h):
    """A published rota whose Monday duty is handed over (Sick), sent for approval with a note."""
    pub, shifts = issued(h)
    made = change(h, pub, shifts[1], absence_reason='Sick')
    sent = h.command('coordinator', 'publication.changes.submit', {'reason': 'Sick cover agreed with the lead'},
                     target=pub, revision=made['workspace']['revision'])
    return sent['workspace'], shifts[1]


def test_the_approver_reads_an_amendments_changes_reason_and_note_where_they_decide(browser, live_server, h):
    w, issued_shift = amendment(h)
    fix = h.get('approver', f"/api/workspaces/{w['id']}/fix-list")['summary']
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        open_rota(page, w['id'])
        # The review strip: what changes, was → now with the coordinator's reason.
        strip = page.locator('.review-strip')
        changes = strip.get_by_role('region', name='What changes')
        expect(changes).to_contain_text('Staff 02(test) → Staff 03(test)')
        expect(changes).to_contain_text('· sick')
        # The changed session is marked on the grid (the person who joins it), on the change's week.
        expect(page.locator('#schedule-view .pchip.pc-changed').first).to_be_visible()
        shot(page, 'ME-26-review-strip')
        # An amendment is approved as its changes: "Approve changes" (SWEEP-2, 281cc5c36).
        page.get_by_role('button', name='Approve changes', exact=True).click()
        dialog = page.get_by_role('dialog', name='Approve changes')
        expect(dialog.get_by_role('region', name='What changes')).to_contain_text('Staff 02(test) → Staff 03(test)')
        expect(dialog).to_contain_text('Only the changes rechecked')
        # ME-27: one unit: the Fix list's causes, never the checks' count.
        expect(dialog).not_to_contain_text('sessions ·')
        if fix['could']:
            expect(dialog).to_contain_text(f"{fix['could']} could improve")
        shot(page, 'ME-26-approve-dialog')
    finally:
        context.close()


def test_the_review_strip_quotes_a_whole_rotas_submit_note(browser, live_server, h):
    w = h.shift(h.workspace('FE-C3 strip note'), people=[h.seed['people'][0]])['workspace']
    w = h.command('coordinator', 'workspace.validate', {'reason': 'Routine rota check'}, w['id'], w['revision'])['workspace']
    h.command('coordinator', 'workspace.submit', {'reason': 'Weekend cover agreed with the lead'}, w['id'], w['revision'])
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        open_rota(page, w['id'])
        expect(page.locator('.review-strip').get_by_role('region', name='Submitted with this note')).to_contain_text('Weekend cover agreed with the lead')
    finally:
        context.close()


def test_the_approvers_home_names_an_amendment_with_its_changes(browser, live_server, h):
    amendment(h)
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        row = page.get_by_role('region', name='Needs you').get_by_role('listitem').filter(has_text='Amendment to review')
        expect(row).to_contain_text('1 change · sick')
        shot(page, 'ME-28-approver-home')
    finally:
        context.close()


def test_sending_changes_says_so_on_the_chip_before_the_list_is_read_again(browser, live_server, h):
    day, _ = published(h)
    pub = h.get('coordinator', '/api/publications')['publications'][0]
    shifts = h.get('coordinator', '/api/publications/' + pub['id'])['snapshot']['shifts']
    change(h, pub['id'], shifts[0])
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        show(page, f'schedule/week/{day.isoformat()}')
        chip = page.locator('.pc-chip')
        expect(chip).to_have_text('1 change · not sent')
        held = []
        page.route('**/pending-changes', lambda route: held.append(route))  # the list read after the send waits
        chip.click()
        with page.expect_request(lambda r: r.url.endswith('/pending-changes')):
            page.get_by_role('button', name='Send 1 change for approval').click()
        expect(page.locator('.pc-chip')).to_have_text('1 change waiting')  # from the send's reply, the read still held
        for route in held:
            route.continue_()
        page.unroute('**/pending-changes')
    finally:
        context.close()


def test_publish_confirms_on_the_published_rota_not_inside_a_closing_dialog(browser, live_server, h):
    a = h.seed['people'][0]
    w = h.shift(h.workspace('FE-C3 publish'), people=[a])['workspace']
    w = h.command('coordinator', 'workspace.validate', {'reason': 'Routine rota check'}, w['id'], w['revision'])['workspace']
    w = h.command('coordinator', 'workspace.submit', {'reason': 'Ready'}, w['id'], w['revision'])['workspace']
    h.lifecycle(w, 'approve', 'approver')
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_rota(page, w['id'])
        page.get_by_role('button', name='Publish…', exact=True).click()
        dialog = page.get_by_role('dialog', name=f"Publish {w['name']}")
        dialog.get_by_role('button', name='Publish', exact=True).click()
        held = []
        page.route('**/api/workspaces?*', lambda route: held.append(route))  # the refresh after publishing waits
        with page.expect_request(lambda r: '/api/workspaces?' in r.url):
            dialog.locator('button.armed').click()
        expect(dialog).to_have_count(0)  # closed once published, while the page is read again
        for route in held:
            route.continue_()
        page.unroute('**/api/workspaces?*')
        toast = page.locator('#toast-tray .toast').filter(has_text='Published · staff are told.')
        expect(toast).to_be_visible()
        expect(page.locator('dialog .toast')).to_have_count(0)
        assert h.detail(w)['workspace']['status'] == 'published'
    finally:
        context.close()


def test_activating_your_own_route_proposal_says_it_is_self_approved(browser, live_server, h):
    from .governance_support import identity
    identity(h, 'route_owner', ['admin', 'approver'])
    h.command('admin', 'self_approval.grant', {'role': 'approver', 'actions': ['route.decide'], 'starts_on': '2026-06-01',
              'ends_on': '2027-05-31', 'reason': 'Only one administrator this quarter'})
    h.command('route_owner', 'review_route.create', {'name': 'Own route', 'purpose': 'rota', 'starts_on': '2026-10-01',
        'definition': {'schema_version': 1, 'steps': [{'key': 'review', 'label': 'Independent review', 'role': 'approver'}],
                       'urgent_steps': None}, 'reason': 'Proposed by the person who decides it'})
    context, page = sign_in(browser, live_server, 'route_owner', DESKTOP)
    try:
        navigate(page, 'access/routes')
        page.locator('section.panel').filter(has_text='Own route').get_by_role('button', name='Activate route', exact=True).click()
        expect(page.get_by_role('dialog')).to_contain_text('You proposed this route: deciding it is self-approved')
    finally:
        context.close()


def test_a_phone_sheet_is_not_covered_by_a_message_about_the_page_behind_it(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 390, 'height': 844})
    try:
        page.evaluate("""async()=>{const d=await import(document.querySelector('script[type=module]').src.replace(/app\.js.*$/,'dom.js'));
            d.notify('Swap offered');const sheet=document.createElement('dialog');sheet.id='fe-c3-sheet';document.body.append(sheet);sheet.showModal();
            d.notify('Saved · Undo','success',{label:'Undo',run:()=>{}});}""")
        expect(page.locator('#fe-c3-sheet #toast-tray .toast')).to_have_count(1)  # only the one with an action stays
        expect(page.locator('.toast').filter(has_text='Swap offered')).to_have_count(0)
    finally:
        context.close()


def test_the_rule_lines_table_keeps_valid_and_reach_on_screen_at_1440(browser, live_server, h):
    from .test_browser_rule_catalogue import flag
    from .test_browser_rule_catalogue_conflicts import crafted
    flag(h)
    crafted(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        page.evaluate("""async () => {const me=await fetch('/api/me').then(r=>r.json());
            localStorage.setItem('rota.table.'+me.identity_id+'.'+me.department.id+'.rule-line-db',JSON.stringify({layout:'table',chosen:true}));}""")
        navigate(page, 'rules/duties_per_week')
        expect(page.locator('#main tbody tr').filter(has_text='Everyone line')).to_be_visible()
        # Valid and Reach end inside the table's own scroll box (Where shows one place, then +N).
        fits = page.evaluate("""()=>{const t=document.querySelector('#main table'),s=t.closest('.table-scroll')||t.parentElement;
            const edge=k=>t.querySelector(`thead th[data-col="${k}"]`).getBoundingClientRect().right;
            return ['valid','reach'].every(k=>edge(k)<=s.getBoundingClientRect().right+1)}""")
        assert fits
        shot(page, 'F-R2-rule-lines')
    finally:
        context.close()

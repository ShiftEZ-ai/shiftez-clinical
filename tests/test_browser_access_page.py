"""Access & approvals › People & roles: neutral role chips, one Give a role form with Undo, one access read a visit."""
import re

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_browser import TEST_PASSWORD, browser, live_server, navigate, sign_in  # noqa: F401

pytestmark = pytest.mark.browser


def staff_identity(h):
    staff_id = h.get('staff', '/api/me')['identity_id']
    return next(i for i in h.get('admin', '/api/access')['identities'] if i['id'] == staff_id)


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_give_a_role_offers_only_new_roles_and_its_message_undoes_it(browser, live_server, h, width, scheme):
    target = staff_identity(h)
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': width, 'height': 900}, color_scheme=scheme)
    try:
        navigate(page, 'access/people')
        main = page.locator('#main')
        # Roles are neutral chips (hues mean duty types); no tagline or permanent server banner.
        chips = main.locator('.role-chip')
        expect(chips.first).to_be_visible()
        assert all('duty-slate' in c for c in chips.evaluate_all('cs => cs.map(c => c.className)'))
        expect(main.get_by_text('Who can sign in, and what each person can do.')).to_have_count(0)
        row = page.get_by_role('row').filter(has_text=target['display_name']).first
        page.get_by_role('button', name=f"More actions for {target['display_name']}", exact=True).first.click()
        page.get_by_role('menuitem', name='Give a role', exact=True).click()
        dialog = page.get_by_role('dialog', name='Give a role to ' + target['display_name'])
        # The role they hold already (Staff) is not offered again; the form has no preamble.
        options = dialog.get_by_label('Role', exact=True).locator('option')
        expect(options.filter(has_text=re.compile('^Staff$'))).to_have_count(0)
        expect(options.filter(has_text='Credentialer')).to_have_count(1)
        expect(dialog.locator('.dialog-head p')).to_have_text('')
        dialog.get_by_label('Role', exact=True).select_option('credentialer')
        dialog.get_by_label('Reason', exact=True).fill('Fictional credential cover')
        dialog.get_by_role('button', name='Give role', exact=True).click()
        expect(dialog).to_have_count(0)
        toast = page.locator('.toast', has_text=f"Credentialer role given to {target['display_name']}.")
        expect(toast).to_be_visible()

        def held():
            return [b['role'] for b in h.get('admin', '/api/access')['bindings']
                    if b['identity_id'] == target['id'] and not b['revoked']]
        assert 'credentialer' in held()
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('.toast', has_text='Undone')).to_be_visible()
        expect(row.locator('.role-chip', has_text='Credentialer')).to_have_count(0)
        assert 'credentialer' not in held()
    finally:
        context.close()


def test_access_tabs_share_one_read_until_a_save(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    reads = []
    page.on('request', lambda r: reads.append(r.url) if r.url.endswith('/api/access') and r.method == 'GET' else None)
    try:
        navigate(page, 'access/people')
        tabs = page.get_by_role('tablist', name='Access & approvals')
        main = page.locator('#main')
        expect(main).to_have_attribute('data-loaded-tab', 'people')
        for tab, key in [('Role history', 'history'), ('Review routes', 'routes'), ('People & roles', 'people'),
                         ('Role history', 'history')]:
            tabs.get_by_role('tab', name=tab, exact=True).click()
            expect(main).to_have_attribute('data-loaded-tab', key)
        expect(main.locator('tbody tr').first).to_be_visible()
        assert len(reads) == 1, reads
        # A save reads it again (the new grant shows).
        tabs.get_by_role('tab', name='People & roles', exact=True).click()
        expect(main).to_have_attribute('data-loaded-tab', 'people')
        target = staff_identity(h)
        page.get_by_role('button', name=f"More actions for {target['display_name']}", exact=True).first.click()
        page.get_by_role('menuitem', name='Give a role', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Role', exact=True).select_option('credentialer')
        dialog.get_by_label('Reason', exact=True).fill('Fictional cover')
        dialog.get_by_role('button', name='Give role', exact=True).click()
        expect(page.get_by_role('row').filter(has_text=target['display_name']).first.locator(
            '.role-chip', has_text='Credentialer')).to_be_visible()
        assert len(reads) == 2, reads
    finally:
        context.close()


def sign_out(page):
    """Sign out to a sign-in form for any account: a remembered account (app.ts showLogin, "Continue as") hides the
    Username field, so "Use another account" is pressed when it is offered."""
    page.get_by_role('button', name=re.compile(r'^Account menu, ')).click()
    page.get_by_role('menuitem', name='Sign out', exact=True).click()
    expect(page.get_by_role('button', name='Continue', exact=True)).to_be_visible()  # shown either way
    another = page.get_by_role('button', name='Use another account', exact=True)
    if another.count():
        another.click()
    expect(page.get_by_label('Username or email', exact=True)).to_be_visible()


def access_read(r):
    return r.url.endswith('/api/access') and r.request.method == 'GET'


def sign_in_onto_access(page, username):
    """Sign in, then open Access explicitly. Observed from before the sign-in: boot may restore the Access address
    left at sign-out and read it then, so the read is caught wherever it happens. It must be a fresh 200 read."""
    with page.expect_response(access_read) as read:
        page.get_by_label('Username or email', exact=True).fill(username)
        page.get_by_role('button', name='Continue', exact=True).click()
        page.get_by_label('Password', exact=True).fill(TEST_PASSWORD)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible(timeout=20000)
        navigate(page, 'access/people')
    assert read.value.status == 200, read.value.status
    return read.value.json()


def open_access_reading(page):
    """Open Access explicitly and return the GET /api/access it makes: a fresh, authorised read (200)."""
    with page.expect_response(access_read) as read:
        navigate(page, 'access/people')
    assert read.value.status == 200, read.value.status
    return read.value.json()


def test_signing_in_again_reads_access_afresh_not_the_last_sessions(browser, live_server, h):
    """Review maintenance item 3: the Access read is the signed-in account's. After sign-out and sign-in, opening Access reads
    /api/access again and shows what changed meanwhile, never the previous session's copy."""
    target = staff_identity(h)
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_access_reading(page)
        row = page.get_by_role('row').filter(has_text=target['display_name']).first
        expect(row).to_be_visible()
        expect(row.locator('.role-chip', has_text='Credentialer')).to_have_count(0)
        # Changed while this session's copy is held (another admin elsewhere).
        h.command('admin', 'role.grant', {'identity_id': target['id'], 'role': 'credentialer', 'starts_on': '2020-01-01',
                                          'reason': 'Fictional cover from another session'})
        sign_out(page)
        sign_in_onto_access(page, 'admin')
        expect(page.get_by_role('row').filter(has_text=target['display_name']).first.locator(
            '.role-chip', has_text='Credentialer')).to_be_visible()
    finally:
        context.close()


def test_another_organisations_admin_never_sees_the_last_accounts_access(browser, live_server, h):
    """Review maintenance item 3 (security): sign out of account A, sign in as another organisation's admin (B) in the same
    tab, open Access: B's own read is made and drawn, and none of A's identities or role bindings appear."""
    with transaction(h.app.state.engine, write=True) as db:
        org = m.Organisation(name='Other hospital', timezone='Asia/Riyadh', week_start=6)
        db.add(org); db.flush()
        dept = m.Department(organisation_id=org.id, name='Elsewhere')
        db.add(dept); db.flush()
        dept_id = dept.id
    identity(h, 'other_admin', ['admin'], department=dept_id)
    a_access = h.get('admin', '/api/access')
    a_names = sorted({i['display_name'] for i in a_access['identities']})
    assert a_names and 'Fictional other_admin' not in a_names
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_access_reading(page)
        expect(page.get_by_role('row').filter(has_text=a_names[0]).first).to_be_visible()  # A's data was drawn
        sign_out(page)
        b_access = sign_in_onto_access(page, 'other_admin')
        assert {i['display_name'] for i in b_access['identities']}.isdisjoint(a_names)
        main = page.locator('#main')
        expect(main).to_have_attribute('data-loaded-tab', 'people')
        expect(page.get_by_role('row').filter(has_text='Fictional other_admin').first).to_be_visible()
        for name in a_names:
            expect(main).not_to_contain_text(name)
        for tab, key in [('Role history', 'history'), ('Review routes', 'routes')]:  # bindings and routes too
            page.get_by_role('tablist', name='Access & approvals').get_by_role('tab', name=tab, exact=True).click()
            expect(main).to_have_attribute('data-loaded-tab', key)
            for name in a_names:
                expect(main).not_to_contain_text(name)
    finally:
        context.close()

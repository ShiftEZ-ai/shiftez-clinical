"""System › Audit log: rows read as sentences; the reference is one Copy button, not read out on every row."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import TEST_PASSWORD, browser, live_server, navigate, sign_in  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_audit_rows_fold_sign_ins_and_copy_their_reference(browser, live_server, h, width, scheme):
    # Three sign-ins in a row by one person fold into one row (counted as sign-ins, not devices).
    from fastapi.testclient import TestClient
    for _ in range(3):  # three browsers (signing in again in one browser ends its earlier session)
        client = TestClient(h.app, base_url=h.settings.public_origin, headers={'origin': h.settings.public_origin})
        reply = client.post('/auth/login', json={'username': 'approver', 'password': TEST_PASSWORD})
        assert reply.status_code == 200, reply.text
    context, page = sign_in(browser, live_server, 'auditor', viewport={'width': width, 'height': 900},
                            color_scheme=scheme)
    try:
        context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=live_server)
        navigate(page, 'system/audit')
        row = page.locator('.audit-row').filter(has_text=re.compile(r'signed in 3 times'))
        expect(row).to_have_count(1)
        # "Signed in" only repeats the change: no reason line.
        expect(page.locator('.audit-row small').filter(has_text='Signed in')).to_have_count(0)
        # The time is plain text: not a tab stop, and no account or correlation id read out with it.
        time = row.locator('.audit-time')
        assert time.get_attribute('tabindex') is None
        expect(time).not_to_contain_text('Reference')
        copy = row.get_by_role('button', name=re.compile(r'^Copy reference for \d\d:\d\d(–\d\d:\d\d)? change$'))
        copy.click()
        expect(page.locator('.toast', has_text='Reference copied.')).to_be_visible()
        copied = page.evaluate('navigator.clipboard.readText()')
        assert copied.startswith('Account ') and ' · Reference ' in copied and copied.endswith('auth.sign_in')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        context.close()


def test_audit_reloads_do_not_reread_the_people_list(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'auditor')
    reads = []
    page.on('request', lambda r: reads.append(r.url) if '/api/audit?' in r.url else None)
    try:
        navigate(page, 'system/audit')
        expect(page.locator('.audit-row').first).to_be_visible()
        assert len(reads) == 1 and 'people=false' not in reads[0]
        with page.expect_response(lambda r: '/api/audit?' in r.url and 'q=approver' in r.url):
            page.get_by_label('Search history', exact=True).fill('approver')
        assert 'people=false' in reads[-1]
        # The Person filter still offers the people read on opening.
        page.get_by_role('button', name=re.compile('^Filter')).click()
        person = page.get_by_label('Person', exact=True)
        expect(person.locator('option')).not_to_have_count(1)
    finally:
        context.close()

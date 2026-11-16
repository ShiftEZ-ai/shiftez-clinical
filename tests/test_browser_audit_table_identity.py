"""Saved collection searches belong to one account and department, including after session expiry."""
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect
from sqlalchemy import update

from apps.api import models as m
from packages.domain.time import now
from .conftest import TEST_PASSWORD
from .test_browser import browser, live_server, sign_in, navigate, expect_loaded, filter_by  # noqa: F401

pytestmark = pytest.mark.browser


def expire(page, h, identity):
    with h.app.state.engine.begin() as db:
        db.execute(update(m.Session).where(m.Session.identity_id == identity).values(
            expires_at=now() - timedelta(minutes=1)))
    # A real authenticated request gets the server's expiry response and drives the app's normal sign-in state.
    code = page.evaluate("""async () => {
        const api = await window.importBundle('api.js');
        try { await api.request('/api/me'); return 'unexpected success'; } catch (e) { return e.code; }
    }""")
    assert code == 'SESSION_EXPIRED'
    expect(page.get_by_label('Username or email', exact=True)).to_be_visible()


def login_api(context, origin, role, department):
    reply = context.request.post(origin + '/auth/login', data={
        'username': role, 'password': TEST_PASSWORD, 'department_id': department}, headers={'origin': origin})
    assert reply.ok, reply.text()
    current = context.request.get(origin + '/api/me')
    assert current.ok, current.text()
    return current.json()


@pytest.mark.parametrize('destination', ['account', 'department'])
def test_expired_session_does_not_share_table_search_or_filters(browser, live_server, h, destination):
    original_department = h.seed['department_id']
    original_identity = h.seed['identities']['coordinator']
    private_query = 'Confidential audit note from the first account'
    h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'leave',
        'start': '2026-10-06T00:00:00+03:00', 'end': '2026-10-06T23:59:00+03:00', 'private_reason': private_query})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        target_role, target_department = 'approver', original_department
        if destination == 'department':
            with h.app.state.engine.begin() as db:
                # The same real identity may work in another department; login accepts its canonical department ID.
                target_department = 'e8dbe3fb-1fd6-436e-b2e5-708628d5fc98'
                db.execute(m.Department.__table__.insert().values(id=target_department,
                    organisation_id=h.seed['organisation_id'], name='Table privacy audit department'))
                db.execute(m.RoleBinding.__table__.insert().values(identity_id=original_identity,
                    department_id=target_department, role='coordinator', starts_on=date(2020, 1, 1)))
                db.execute(m.RequestRecord.__table__.insert().values(department_id=target_department,
                    person_id=h.seed['people'][0], kind='leave', private_reason='Other department fixture',
                    start=datetime(2026, 10, 6, tzinfo=timezone.utc), end=datetime(2026, 10, 7, tzinfo=timezone.utc),
                    status='submitted', created_by=original_identity))
            target_role = 'coordinator'

        navigate(page, 'requests')
        filter_by(page.locator('#main'), 'Type', ['Leave'])
        search = page.get_by_role('searchbox', name='Search requests', exact=True)
        with page.expect_response(lambda response: urlparse(response.url).path == '/api/requests/all'
                                  and parse_qs(urlparse(response.url).query).get('q') == [private_query]):
            search.fill(private_query)
        # Free-text search is never stored in the browser (it may hold private notes); filters are kept.
        assert private_query not in page.evaluate("JSON.stringify(Object.entries(localStorage))")
        page.reload()
        expect_loaded(page, 'requests')
        expect(search).to_have_value('')
        expect(page.get_by_role('button', name='Filter: Leave (1 active)', exact=True)).to_be_visible()

        expire(page, h, original_identity)
        signed_out = page.evaluate("""async () => {
            const t = await window.importBundle('table_view.js');
            return {q:t.tableSearch('requests'),filters:t.tableFilters('requests')};
        }""")
        current = login_api(context, live_server, target_role, target_department)
        assert current['identity_id'] == h.seed['identities'][target_role]
        assert current['department']['id'] == target_department
        reads = []
        page.on('request', lambda request: reads.append(parse_qs(urlparse(request.url).query))
                if urlparse(request.url).path == '/api/requests/all' else None)
        page.reload()
        expect_loaded(page, 'requests')
        other_query = search.input_value()
        other_filters = page.evaluate("""async () => (await window.importBundle('table_view.js')).tableFilters('requests')""")
        leaked_reads = [query for query in reads if query.get('q') == [private_query]]

        expire(page, h, current['identity_id'])
        login_api(context, live_server, 'coordinator', original_department)
        page.reload()
        expect_loaded(page, 'requests')
        # The same identity keeps its filters; another account signing in on this device forgot them (LIVE-50).
        kept = 'Filter: Leave (1 active)' if destination == 'department' else 'Filter'
        expect(page.get_by_role('button', name=kept, exact=True)).to_be_visible()
        assert signed_out == {'q': '', 'filters': {}}, {
            'signed_out': signed_out, 'other_query': other_query, 'other_filters': other_filters,
            'leaked_requests': leaked_reads}
        # The other account starts on its own defaults: both targets decide requests, so Waiting for you (998bbca16
        # maintenance, a built-in view's server scope), never the first account's Leave filter or search.
        assert other_query == '' and other_filters == {'for_me': ['1']}, (other_query, other_filters)
        assert not leaked_reads, leaked_reads
    finally:
        context.close()

"""D-001: an undated Schedule opens only a draft this reader acts on; phone stays published."""
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from packages.contracts.schemas import CAPABILITIES, ROLE_NAMES
from packages.domain.time import now
from .governance_support import identity
from .test_browser import browser, live_server, sign_in, navigate, expect_loaded  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_undated_schedule_and_home_only_offer_actionable_drafts(browser, live_server, h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    organisation = h.get('coordinator', '/api/me')['organisation']
    today = now().astimezone(ZoneInfo(organisation['timezone'])).date()
    first = today - timedelta(days=(today.weekday() - organisation['week_start']) % 7)
    end = first + timedelta(days=7)
    published = h.shift(h.workspace('D001 published', str(first), str(end)),
                        start=f'{today}T08:00:00+03:00', end=f'{today}T12:00:00+03:00')['workspace']
    h.publish(published)
    draft = h.shift(h.workspace('D001 draft', str(today), str(today + timedelta(days=1))),
                    start=f'{today}T13:00:00+03:00', end=f'{today}T14:00:00+03:00',
                    people=[])['workspace']
    check = h.command('coordinator', 'job.create', {'workspace_id': draft['id'], 'kind': 'validation'},
                      revision=draft['revision'])['job']
    while h.process_jobs():
        pass
    assert h.get('coordinator', '/api/jobs/' + check['id'])['job']['status'] == 'succeeded'
    # A participant edit retains the earlier check as stale, so Home must say "needs a check".
    shift = h.detail(draft)['shifts'][0]
    payload = {key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=draft['id'], shift_id=shift['id'],
                   participants=[{'person_id': h.seed['people'][1], 'start': shift['start'], 'end': shift['end']}])
    draft = h.command('coordinator', 'shift.update', payload, revision=draft['revision'])['workspace']
    roles = h.get(role, '/api/me')['roles']
    can_draft = any(r in CAPABILITIES['planning.draft'] for r in roles)
    planner = any(r in CAPABILITIES[key] for key in (
        'ui.coordinator', 'ui.approver', 'ui.auditor', 'ui.programme_lead') for r in roles)

    for width in (1440, 390):
        context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 1000}, phone_notice=(width == 390))
        try:
            expect_loaded(page, 'home')
            if width == 1440:
                rota = page.locator('.home-rota')
                if planner:
                    expect(rota.locator('.home-month')).to_have_count(4)
                else:
                    expect(rota).to_have_count(0)
                expect(rota.get_by_role('button', name='Open D001 draft', exact=True)).to_have_count(1 if can_draft else 0)
                expect(rota.get_by_text('needs a check', exact=True)).to_have_count(1 if can_draft else 0)
            else:
                expect(page.locator('.home-rota')).to_have_count(0)

            if role in ('credentialer', 'operator'):
                expect(page.locator('#department-nav [data-route=schedule]')).to_have_count(0)
                page.evaluate("location.hash='schedule'")
                expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'restricted')
                expect(page.get_by_role('heading', name='Restricted page', exact=True)).to_be_visible()
                assert 'edit' not in parse_qs(urlsplit(page.url).fragment.partition('?')[2])
                page.reload()
                expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'restricted')
                continue

            navigate(page, 'schedule')
            for reload in (False, True):
                if reload:
                    page.reload()
                    expect_loaded(page, 'schedule')
                edit = parse_qs(urlsplit(page.url).fragment.partition('?')[2]).get('edit')
                opens_draft = width == 1440 and can_draft
                if width == 390:
                    assert draft['id'] not in (edit or [])
                else:
                    assert edit == ([draft['id']] if opens_draft else ['none'] if planner else None)
                if opens_draft:
                    expect(page.locator('.topbar h1')).to_have_text('D001 draft')
                else:
                    expect(page.locator('.topbar h1')).to_have_text('Schedule')
                person = h.seed['people'][1 if opens_draft else 0]
                session = page.locator(f'#schedule-view :is(.pchip,.pg-chip,.ag-item)[data-person~="{person}"]')
                expect(session.first).to_be_visible()
                if not opens_draft:
                    expect(page.locator(f'#schedule-view :is(.pchip,.pg-chip,.ag-item)[data-person~="{h.seed["people"][1]}"]')).to_have_count(0)
                expect(page.locator('#main [role=alert]')).to_have_count(0)
        finally:
            context.close()

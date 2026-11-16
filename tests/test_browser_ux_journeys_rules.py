"""Say what happens before the click (DESIGN.md › No dead ends): a new rule covers the rota being planned from its first
day, Activate says which rota needs a fresh check first (test_browser_cp_sat.py), and Home names a duty that approved
leave now conflicts with, with Replace opening that session."""
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from packages.domain.time import now as clock_now

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser


def test_a_new_rule_starts_with_the_rota_being_planned(browser, live_server, h):
    today = clock_now().astimezone(ZoneInfo('Asia/Riyadh')).date()  # the browser's pinned clock, organisation day
    first = today - timedelta(days=40)  # before every demo rota
    h.workspace('Rota being planned', first.isoformat(), (today + timedelta(days=20)).isoformat())
    for n in range(100):  # newer rotas fill the first page of /api/workspaces; the one being planned must still be found
        h.workspace(f'Later rota {n}', (today + timedelta(days=30 + n)).isoformat(), (today + timedelta(days=31 + n)).isoformat())
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog', name='Add rule', exact=True)
        expect(form.get_by_label('Effective from', exact=True)).to_have_value(f'{first:%a} {first.day} {first:%b %Y}')  # the field shows the day
        said = form.get_by_text('From the first day of Rota being planned.', exact=True)
        expect(said).to_be_visible()
        form.get_by_label('Effective from', exact=True).fill(today.isoformat())
        expect(said).to_be_hidden()
    finally:
        context.close()


def test_home_names_a_duty_during_approved_leave_and_replace_opens_it(browser, live_server, h):
    w = h.workspace('Leave conflict rota')
    h.shift(w)
    shift = h.detail(w)['shifts'][0]
    # The item as home.py sends it (leave_conflicts; tests/test_home_leave_conflicts.py covers the server side).
    item = {'key': 'leave_conflicts', 'count': 2, 'sample': [{'person': 'Alex Morgan', 'start': shift['start'], 'end': shift['end'],
        'rota_id': w['id'], 'rota_name': w['name'], 'shift_id': shift['id'], 'request_id': 'r1'}]}
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route('**/api/home', h.route_json('coordinator', '/api/home', lambda body: {
            **body, 'attention': [item, *body['attention']]}))
        page.reload()
        row = page.locator('.home-needs li').filter(has_text='Duties during approved leave')
        expect(row).to_contain_text(re.compile(r'Alex Morgan · Sun,? 4 Oct'))
        # G-02: each duty is its own line with its Replace; the one past the sample opens the Fix list.
        expect(row.locator('.home-need-items > li').nth(0)).to_contain_text('Leave conflict rota')
        expect(row.locator('.home-need-items > li').nth(1)).to_contain_text('1 more')
        expect(row.locator('.home-need-items > li').nth(1).get_by_role('button', name='Fix list')).to_be_visible()
        row.get_by_role('button', name='Replace').click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        assert f"edit={w['id']}" in page.url
        expect(page.locator('#main .issue-spot').first).to_be_visible()  # the session is marked
    finally:
        context.close()

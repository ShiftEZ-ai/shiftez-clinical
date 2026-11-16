"""Findings say each thing once (FD-06): a rule matched by two of its lines is one row in the session panel."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, open_rota, route_fetch, sign_in  # noqa: F401

pytestmark = pytest.mark.browser


def test_fix_buttons_deduped(browser, live_server, h):
    """FD-06: the session panel's "Rules applied here" lists Supervision once when two of its lines apply (shot 150)."""
    w = h.shift(h.workspace('FM rules once'))['workspace']
    rows = [{'id': 'sup', 'name': 'Supervision', 'status': 'passes', 'messages': [], 'number': None, 'own_line': None},
            {'id': 'sup', 'name': 'Supervision', 'status': 'warning', 'messages': ['Second line'], 'number': None, 'own_line': None}]

    def lines(route):
        response = route_fetch(route)
        body = response.json()
        for person in body['people']:
            person['rules'] = rows
        route.fulfill(response=response, json=body)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    page.route(re.compile(r'/shifts/[^/]+/rules$'), lines)
    try:
        open_rota(page, w['id'])
        page.locator('.wk-grid button.pchip:not(.empty):not(.gap)').first.click()
        applied = page.get_by_role('list', name='Rules applied to this assignment')
        expect(applied.locator(':scope > li').filter(has_text='Supervision')).to_have_count(1)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

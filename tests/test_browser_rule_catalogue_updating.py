"""maintenance: after an approval the rule catalogue's projection is rebuilt and its reads answer 202 {state:'updating'}
until it is ready. The Rules page open meanwhile never shows the rules from before: its live redraw gives way to the
page's normal skeleton (no overlay, nothing inert left on screen), then shows the new rules. The rebuild is held here by
answering the catalogue reads with the server's envelope until the test releases them."""
import json
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import flag

pytestmark = pytest.mark.browser

UPDATING = json.dumps({'state': 'updating', 'projection': 'rule_catalogue', 'stamp': 'held', 'job_id': None,
                       'retry_after_seconds': 2})


def test_approval_leaves_the_rules_page_on_its_skeleton_then_the_new_rules(browser, live_server, h):
    flag(h)
    proposer, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('switch', name='Rest between duties on or off').click()
        page.locator('.topbar').get_by_role('button', name='Propose 2 changes').click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Rest after every duty')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        expect(page.locator('#main li.rt-item').filter(has_text='Rest between duties')).to_contain_text('Waiting for approval')
    finally:
        proposer.close()
    context, page = sign_in(browser, live_server, 'approver')
    try:
        held = {'on': False, 'answered': 0}

        def catalogue(route):
            if not held['on']:
                return route.continue_()
            held['answered'] += 1
            route.fulfill(status=202, headers={'Retry-After': '2', 'Cache-Control': 'no-store'},
                          content_type='application/json', body=UPDATING)

        page.route(re.compile(r'/api/rules/catalogue(\?|$)'), catalogue)
        navigate(page, 'rules')
        row = page.locator('#main li.rt-item').filter(has_text='Rest between duties')
        expect(row).to_contain_text('Waiting for approval')
        held['on'] = True
        waiting = next(e for e in h.get('approver', '/api/rules/catalogue')['entries']
                       if e['id'] == 'rest_between_duties')['proposed']
        h.command('approver', 'policy.activate', {'reason': 'Agreed'}, waiting['id'], waiting['revision'])
        # The live redraw meets the rebuild: the page's skeleton, never the rules from before, and no overlay.
        main = page.locator('#main')
        expect(main.locator('.loading.page-sk')).to_be_visible(timeout=30_000)
        expect(main.locator('li.rt-item')).to_have_count(0)
        expect(main.locator('[inert]')).to_have_count(0)
        expect(page.get_by_role('dialog')).to_have_count(0)
        assert held['answered'] >= 1
        # Ready: the next read after Retry-After shows the new rules.
        held['on'] = False
        expect(row).to_be_visible(timeout=15_000)
        expect(row).not_to_contain_text('Waiting for approval')
        expect(main.locator('.loading.page-sk')).to_have_count(0)
    finally:
        context.close()

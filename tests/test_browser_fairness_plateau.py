"""Fair share search time: the one auto-schedule options panel sets how long a run keeps improving fairness while it
still improves (fairness_plateau_seconds), and a run's result says why its fair share search stopped and warns when it
stopped while fairness was still improving."""
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_browser import auto_result, browser, live_server, open_config, open_rota  # noqa: F401
from .test_browser_rule_catalogue_v2 import themed

pytestmark = pytest.mark.browser
VIEWS = [(1440, 'light'), (390, 'dark')]
LABEL = 'Keep improving fairness'


def shot(page, name, width, theme):
    folder = os.getenv('RULES_FE4_SHOTS')
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=f"{folder}/{name}-{width}-{theme}.png", animations='disabled')


@pytest.mark.parametrize('width,theme', VIEWS)
def test_admin_sets_how_long_fairness_keeps_improving(browser, live_server, h, width, theme):
    context, page = themed(browser, live_server, 'admin', width, theme)
    try:
        open_config(page, 'Auto-schedule & scoring')
        pane = page.locator('.config-pane')
        pane.get_by_role('tab', name='Timing', exact=True).click()
        fair = pane.get_by_label(LABEL, exact=True)
        expect(fair).to_have_value('15 s')
        expect(pane.get_by_role('button', name=f'About {LABEL}', exact=True)).to_have_attribute(
            'data-tip', 'While fair shares still improve, the search goes on until this long passes without progress. 0 uses the whole search time.')
        fair.fill('40 s')
        shot(page, 'fairness-plateau-option', width, theme)
        pane.get_by_role('button', name='Save defaults', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Give fairness longer')
        dialog.get_by_role('button', name='Save defaults', exact=True).click()
        expect(page.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        assert h.get('admin', '/api/me')['department']['generation_defaults']['fairness_plateau_seconds'] == 40
        open_config(page, 'Auto-schedule & scoring')
        pane.get_by_role('tab', name='Timing', exact=True).click()
        expect(pane.get_by_label(LABEL, exact=True)).to_have_value('40 s')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', VIEWS)
def test_a_result_says_why_fair_share_search_stopped(browser, live_server, h, width, theme):
    from .test_browser import open_scheduling
    w = h.shift(h.workspace('Fairness still improving'))['workspace']
    warning = 'Search stopped while fairness was still improving; the search time allowance ended.'
    at = '2026-10-03T06:00:00+00:00'
    job = {'id': 'job-fair-stop', 'revision': 1, 'kind': 'generation', 'status': 'succeeded', 'outcome': 'needs_more_time',
           'workspace_id': w['id'], 'created_at': at, 'started_at': at, 'completed_at': at, 'input_fingerprint': 'older',
           'result': {'status': 'needs_more_time', 'warnings': [warning],
                      'fair_share_search': {'stop_reason': 'time_limit', 'fairness_improving': True, 'plateau_seconds': 15}}}
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    try:
        page.route(f'**/api/jobs?workspace_id={w["id"]}*', lambda r: r.fulfill(json={'jobs': [job]}))
        page.route('**/api/jobs/job-fair-stop', lambda r: r.fulfill(json={'job': job}))
        open_rota(page, w['id'])
        open_scheduling(page)
        auto_result(page.locator('#auto-panel'))
        page.locator('#auto-panel').get_by_role('button', name='Review latest result').click()
        dialog = page.get_by_role('dialog')
        # Visible, not inside Run details: an item of the one issue list "Run warnings" (4c8b40be3).
        expect(dialog.get_by_role('list', name='Run warnings')).to_contain_text(warning)
        details = dialog.locator('details.auto-run-details')
        details.locator('summary').click()
        expect(details).to_contain_text('Fair share search: the search time ran out')
        shot(page, 'fairness-stop-reason', width, theme)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

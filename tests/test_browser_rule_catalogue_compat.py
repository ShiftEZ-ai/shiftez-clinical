"""The Rules page and the planner keep working with catalogue rules in force (before the catalogue's own page ships)."""
import uuid

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_rule_catalogue_guard import world

pytestmark = pytest.mark.browser


def test_rules_and_planner_load_with_catalogue_rules_in_force(browser, live_server, h):
    w, _, _ = world(h)
    a = h.seed['people'][0]
    lines = [{'id': str(uuid.uuid4()), 'numbers': {'max': {'value': 2}}},
             {'id': str(uuid.uuid4()), 'who': {'conditions': [{'field': 'person_ids', 'values': [a]}]}, 'numbers': {'max': {'value': 1}}}]
    record = h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_week', 'lines': lines, 'acknowledged_overlaps': [lines[0]['id']],
                                                       'reason': 'Weekly duty caps'})['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent review'}, record['id'], record['revision'])
    errors = []
    context, page = sign_in(browser, live_server, 'coordinator')
    page.on('pageerror', lambda error: errors.append(str(error)))
    navigate(page, 'rules')
    expect(page.locator('.rule-item').filter(has_text='Duties per week').first).to_be_visible()
    navigate(page, f"planner?workspace={w['id']}")
    expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
    context.close()
    assert not errors

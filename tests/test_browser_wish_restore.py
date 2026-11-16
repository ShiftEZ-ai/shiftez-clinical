"""Both existing withdrawal surfaces use the audited inverse and retain the standing preference's identity."""
import re

import pytest
from playwright.sync_api import expect

from tests.test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from tests.test_wish_restore import (PATTERN_RESTORE_CASES, assert_original_values, occurrence_signature,
                                     pattern_preference, standing_with_overrides, stored_state)

pytestmark = pytest.mark.browser


def is_command(response, kind):
    return (response.url.endswith('/api/commands') and response.request.method == 'POST'
            and response.request.post_data_json.get('kind') == kind)


@pytest.mark.parametrize('surface', ['requests', 'calendar'])
def test_withdraw_undo_reload_preserves_standing_preference_and_override_links(browser, live_server, h, surface):
    workspace, saved = standing_with_overrides(h)
    before = stored_state(h, saved['id'])
    occurrences = occurrence_signature(h, workspace)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        page.clock.set_fixed_time('2026-10-03T06:00:00Z')
        navigate(page, 'requests')
        if surface == 'calendar':
            page.get_by_role('button', name='Wish calendar', exact=True).click()
            root = page.get_by_role('region', name='Wishes')
            expect(root.get_by_role('meter')).to_have_attribute('value', '4')
        else:
            root = page.locator('#main')
        row = root.locator(f'tbody tr[data-row="{saved["id"]}"]')
        expect(row).to_contain_text(saved['note'])
        row.get_by_role('button', name=re.compile('More actions')).click()
        action = page.get_by_role('menuitem', name='Withdraw wish', exact=True)
        with page.expect_response(lambda response: is_command(response, 'wish.withdraw')) as withdrawn:
            action.click()
        assert withdrawn.value.status == 200
        assert withdrawn.value.json()['wish']['status'] == 'withdrawn'
        assert h.get('staff', '/api/wishes')['wishes'] == []
        with page.expect_response(lambda response: is_command(response, 'wish.restore')) as restored:
            page.get_by_role('button', name='Undo', exact=True).click()
        assert restored.value.status == 200
        restored_wish = restored.value.json()['wish']
        assert restored_wish['id'] == saved['id'] and restored_wish['repeat'] == saved['repeat']
        expect(root.locator(f'tbody tr[data-row="{saved["id"]}"]')).to_contain_text(saved['note'])
        page.reload()
        navigate(page, 'requests')
        if surface == 'calendar':
            page.get_by_role('button', name='Wish calendar', exact=True).click()
            root = page.get_by_role('region', name='Wishes')
            expect(root.get_by_role('meter')).to_have_attribute('value', '4')
        else:
            root = page.locator('#main')
        row = root.locator(f'tbody tr[data-row="{saved["id"]}"]')
        expect(row).to_contain_text(saved['note'])
        expect(row.get_by_role('button', name=re.compile('More actions'))).to_be_visible()
    finally:
        context.close()
    after = stored_state(h, saved['id'])
    assert after['wish']['status'] == 'open'
    for key in ('person_id', 'kind', 'first_day', 'last_day', 'points', 'repeat', 'note', 'created_by', 'created_at'):
        assert after['wish'][key] == before['wish'][key]
    assert after['overrides'] == before['overrides']
    assert occurrence_signature(h, workspace) == occurrences
    assert [row['id'] for row in h.get('staff', '/api/wishes')['wishes']] == [saved['id']]


@pytest.mark.parametrize('surface', ['requests', 'calendar'])
@pytest.mark.parametrize('kind,settings', PATTERN_RESTORE_CASES)
def test_withdraw_undo_reload_preserves_every_pattern_and_custom_weekend(browser, live_server, h, surface, kind, settings):
    saved = pattern_preference(h, kind, settings)
    endpoint = '/api/wishes?start=2026-10-01&end=2027-01-01'
    before_usage = h.get('staff', endpoint)['usage']
    context, page = sign_in(browser, live_server, 'staff')
    try:
        page.clock.set_fixed_time('2026-10-03T06:00:00Z')
        navigate(page, 'requests')
        if surface == 'calendar':
            page.get_by_role('button', name='Wish calendar', exact=True).click()
            root = page.get_by_role('region', name='Wishes')
            expect(root.get_by_role('meter')).to_have_attribute('value', '3')
        else:
            root = page.locator('#main')
        row = root.locator(f'tbody tr[data-row="{saved["id"]}"]')
        expect(row).to_contain_text(saved['note'])
        row.get_by_role('button', name=re.compile('More actions')).click()
        action = page.get_by_role('menuitem', name='Withdraw wish', exact=True)
        with page.expect_response(lambda response: is_command(response, 'wish.withdraw')) as withdrawn:
            action.click()
        assert withdrawn.value.status == 200 and withdrawn.value.json()['wish']['status'] == 'withdrawn'
        assert h.get('staff', endpoint)['wishes'] == []
        with page.expect_response(lambda response: is_command(response, 'wish.restore')) as restored:
            page.get_by_role('button', name='Undo', exact=True).click()
        assert restored.value.status == 200
        assert_original_values(restored.value.json()['wish'], saved)
        page.reload()
        navigate(page, 'requests')
        if surface == 'calendar':
            page.get_by_role('button', name='Wish calendar', exact=True).click()
            root = page.get_by_role('region', name='Wishes')
            expect(root.get_by_role('meter')).to_have_attribute('value', '3')
        else:
            root = page.locator('#main')
        row = root.locator(f'tbody tr[data-row="{saved["id"]}"]')
        expect(row).to_contain_text(saved['note'])
        expect(row.get_by_role('button', name=re.compile('More actions'))).to_be_visible()
    finally:
        context.close()
    after = h.get('staff', endpoint)
    [restored] = after['wishes']
    assert restored['status'] == 'open'
    assert_original_values(restored, saved)
    assert after['usage'] == before_usage

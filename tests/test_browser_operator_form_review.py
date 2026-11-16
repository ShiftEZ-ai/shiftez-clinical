"""Custom selections are saved exactly as chosen, after changing them, by the real API (forms are all on one page)."""
import re

import pytest
from playwright.sync_api import expect
from .test_browser import form_submit, add_filter, browser, live_server, sign_in, navigate, condition, open_config  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}])
def test_rule_review_keeps_named_people_exclusions_and_nested_groups_after_edit(browser, live_server, h, viewport):
    context, page = sign_in(browser, live_server, 'coordinator', viewport)
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add rule')
        dialog.get_by_role('radio', name='Maximum hours', exact=True).check()
        scope = dialog.locator('.scope-who-what')
        condition(scope, 'Person', ['Alex Morgan'])
        condition(scope, 'Person', ['Taylor Noor'], op='none')
        add_filter(scope, group=True)
        group = scope.locator('.cond-group')
        condition(group, 'Clinic', ['Continuity clinic'])
        condition(group, 'Clinic', ['Review clinic'])
        group.get_by_label('Joiner', exact=True).select_option(label='Or')  # a group's rows join one way
        dialog.get_by_label('Rule title', exact=True).fill('Exact reviewed scope')
        dialog.get_by_label('Source or departmental decision', exact=True).fill('Fictional operator form review')
        # Change the picker afterwards; what is saved follows its actual chosen values.
        line = scope.locator('.cond-line').first
        line.locator('.pick-button').click()
        picker = scope.locator('.picker')
        # A short list has no Clear (X-37): untick what is chosen, then tick Jordan Lee.
        for name in picker.get_by_role('option', checked=True).evaluate_all('o => o.map(x => x.getAttribute("aria-label"))'):
            picker.get_by_role('option', name=name, exact=True).uncheck()
        picker.get_by_role('option', name='Jordan Lee', exact=True).check()
        page.keyboard.press('Escape')
        dialog.get_by_role('button', name='Propose rule', exact=True).click()
        expect(dialog).to_have_count(0)
        record = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['name'] == 'Exact reviewed scope')
        assert record['status'] == 'proposed'
        assert record['definition']['scope']['conditions'] == {'match': 'all', 'conditions': [
            {'field': 'person_ids', 'op': 'any', 'values': [h.seed['people'][1]]},
            {'field': 'person_ids', 'op': 'none', 'values': [h.seed['people'][2]]},
            {'match': 'any', 'conditions': [
                {'field': 'service_ids', 'op': 'any', 'values': [h.seed['services'][0]]},
                {'field': 'service_ids', 'op': 'any', 'values': [h.seed['services'][1]]}]}]}
    finally:
        context.close()


def test_long_multi_picker_review_names_every_selection_once_and_saves_all(browser, live_server, h):
    for index in range(8):
        h.configure('categories', {'name': f'Review rank {index + 1}', 'starts_on': '2020-01-01'})
    ranks = h.get('admin', '/api/catalogues')['categories']
    names = [r['name'] for r in ranks]
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Duty types')  # Configuration opens on the Setup guide until setup is done
        page.get_by_role('button', name='New duty type', exact=True).click()
        dialog = page.get_by_role('dialog', name='New duty type')
        dialog.get_by_label('Name', exact=True).fill('Complete reviewed ranks')
        trigger = dialog.get_by_role('button', name=re.compile(r'^Permitted ranks · empty means any eligible rank:'))
        trigger.click()
        picker = dialog.locator('.picker')
        for name in names[:3]:
            picker.get_by_role('option', name=name, exact=True).check()
        page.keyboard.press('Escape')
        expect(trigger.locator('.pick-chip')).to_have_count(3)  # the chosen ranks as chips (then "+N" when narrow)
        trigger.click()
        picker.get_by_role('button', name=f'Select all {len(names)}', exact=True).click()
        page.keyboard.press('Escape')
        expect(trigger.locator('.pick-chip')).to_have_count(len(names))  # the chosen ones, never "All (N)"
        expect(trigger.locator('.pick-more')).to_be_visible()
        form_submit(dialog).click()
        expect(dialog).to_have_count(0)
        record = next(r for r in h.get('admin', '/api/catalogues')['assignment_types'] if r['name'] == 'Complete reviewed ranks')
        assert set(record['definition']['allowed_category_ids']) == {r['id'] for r in ranks}
    finally:
        context.close()

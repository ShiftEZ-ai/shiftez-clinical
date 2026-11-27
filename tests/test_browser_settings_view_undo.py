"""Department display and private saved-view changes recover through the real Undo action."""
import pytest
from playwright.sync_api import expect

from .test_browser import (browser, condition, live_server, sign_in, navigate, toolbar, save_dialog,
                           view_settings, choose_radio, close_popover)  # noqa: F401
from .test_browser_rule_catalogue import flag
from .test_browser_rules_logic import ending_rules, view as rules_view

pytestmark = pytest.mark.browser


def test_display_defaults_undo_restores_saved_settings_and_reloaded_controls(browser, live_server, h):
    before = h.get('admin', '/api/me')['department']['display_settings'] or {}
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration')
        page.get_by_role('button', name='Names & codes', exact=True).click()
        page.get_by_role('radiogroup', name='Show names as').get_by_text('Short code', exact=True).click()
        page.get_by_role('region', name='Unsaved changes').get_by_role('button', name='Save defaults', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Test reversible display settings')
        save_dialog(dialog, 'Save defaults')
        toast = page.locator('.toast').filter(has_text='Department display defaults saved.')
        expect(toast).to_be_visible()
        assert h.get('admin', '/api/me')['department']['display_settings']['names_default'] == 'code'
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('.toast').filter(has_text='Undone.')).to_be_visible()
        restored = h.get('admin', '/api/me')['department']['display_settings']
        assert restored['names_default'] == before.get('names_default', 'full')
        assert restored['rule_catalogue'] == before.get('rule_catalogue', False)
        page.reload()
        navigate(page, 'configuration')
        page.get_by_role('button', name='Names & codes', exact=True).click()
        expect(page.get_by_role('radiogroup', name='Show names as').locator('input[value="full"]')).to_be_checked()
    finally:
        context.close()


def test_saved_table_view_update_and_delete_undo_restore_query_selection_and_reload(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        search = page.get_by_role('searchbox', name='Search people', exact=True)
        search.fill('Staff 01')
        toolbar(page, 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('My recovered people')
        save_dialog(dialog, 'Save view')
        original = h.get('coordinator', '/api/table-views?table=people')['views'][0]
        search.fill('Staff 02')
        toolbar(page, 'Saved views').get_by_role('menuitem', name='Update “My recovered people”', exact=True).click()
        page.locator('.toast').filter(has_text='View updated.').get_by_role('button', name='Undo', exact=True).click()
        expect(search).to_have_value('Staff 01')
        expect(page.get_by_role('button', name='Saved views: My recovered people', exact=True)).to_be_visible()
        restored = h.get('coordinator', '/api/table-views?table=people')['views'][0]
        assert restored['definition'] == original['definition']
        menu = toolbar(page, 'Saved views')
        menu.get_by_role('menuitem', name='Delete “My recovered people”', exact=True).click()
        expect(page.get_by_role('button', name='Saved views', exact=True)).to_be_visible()
        assert h.get('coordinator', '/api/table-views?table=people')['views'] == []
        page.locator('.toast').filter(has_text='View deleted.').get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_role('button', name='Saved views: My recovered people', exact=True)).to_be_visible()
        expect(search).to_have_value('Staff 01')
        recovered = h.get('coordinator', '/api/table-views?table=people')['views'][0]
        assert recovered['id'] != original['id'] and recovered['definition'] == original['definition']
        page.reload()
        expect(page.get_by_role('button', name='Saved views: My recovered people', exact=True)).to_be_visible()
        expect(search).to_have_value('Staff 01')
    finally:
        context.close()


def test_saved_planner_view_delete_undo_reselects_its_layout_and_new_identity(browser, live_server, h):
    h.shift(h.workspace('Planner recovery'))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/week/2026-10-04')
        bar = page.locator('.planner-toolbar')
        panel = view_settings(bar)
        choose_radio(panel, 'Range', '2 weeks')
        close_popover(page)
        toolbar(bar, 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('Recovered fortnight')
        save_dialog(dialog, 'Save view')
        expect(bar.get_by_role('button', name='Saved views: Recovered fortnight', exact=True)).to_be_visible()  # its redraw
        original = h.get('coordinator', '/api/table-views?table=planner')['views'][0]
        menu = toolbar(bar, 'Saved views')
        menu.get_by_role('menuitem', name='Delete “Recovered fortnight”', exact=True).click()
        page.locator('.toast').filter(has_text='View deleted.').get_by_role('button', name='Undo', exact=True).click()
        # Undo reports done once the page shows the view it brought back (the toolbar may still be the one from before).
        expect(page.locator('.toast').filter(has_text='Undone. The previous values are back.')).to_be_visible()
        expect(bar.get_by_role('button', name='Saved views: Recovered fortnight', exact=True)).to_be_visible()
        expect(page.locator('.wk-grid .wk-head .wk-day')).to_have_count(14)
        recovered = h.get('coordinator', '/api/table-views?table=planner')['views'][0]
        assert recovered['id'] != original['id'] and recovered['definition'] == original['definition']
        page.reload()
        expect(bar.get_by_role('button', name='Saved views: Recovered fortnight', exact=True)).to_be_visible()
        expect(page.locator('.wk-grid .wk-head .wk-day')).to_have_count(14)
    finally:
        context.close()


def test_saved_rules_view_update_and_delete_undo_restore_mode_search_and_reload(browser, live_server, h):
    flag(h)
    ending_rules(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        rules_view(page, 'Needs attention')
        search = page.get_by_role('searchbox', name='Search issues', exact=True)
        search.fill('Duties')
        search.press('Enter')  # the search draws once typing pauses (f12); Enter searches at once
        toolbar(page, 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('Recovered rule issues')
        save_dialog(dialog, 'Save view')
        saved = page.locator('#main button.dt-saved')
        expect(saved).to_have_attribute('aria-label', 'Saved views: Recovered rule issues')
        original = h.get('coordinator', '/api/table-views?table=rule-tree')['views'][0]
        search.fill('People')
        search.press('Enter')
        toolbar(page, 'Saved views').get_by_role('menuitem', name='Update “Recovered rule issues”', exact=True).click()
        page.locator('.toast').filter(has_text='View updated.').get_by_role('button', name='Undo', exact=True).click()
        expect(search).to_have_value('Duties')
        expect(saved).to_have_attribute('aria-label', 'Saved views: Recovered rule issues')
        expect(page.locator('tr.rt-issue')).to_have_count(1)
        restored = h.get('coordinator', '/api/table-views?table=rule-tree')['views'][0]
        assert restored['definition'] == original['definition']
        menu = toolbar(page, 'Saved views')
        menu.get_by_role('menuitem', name='Delete “Recovered rule issues”', exact=True).click()
        expect(page.locator('.toast').filter(has_text='View deleted.')).to_be_visible()
        assert h.get('coordinator', '/api/table-views?table=rule-tree')['views'] == []
        rules_view(page, 'All rules')
        expect(page.get_by_role('searchbox', name='Search rules', exact=True)).to_be_visible()
        page.locator('.toast').filter(has_text='View deleted.').get_by_role('button', name='Undo', exact=True).click()
        expect(search).to_have_value('Duties')
        expect(saved).to_have_attribute('aria-label', 'Saved views: Recovered rule issues')
        expect(page.locator('tr.rt-issue')).to_have_count(1)
        recovered = h.get('coordinator', '/api/table-views?table=rule-tree')['views'][0]
        assert recovered['id'] != original['id'] and recovered['definition'] == original['definition']
        page.reload()
        expect(saved).to_have_attribute('aria-label', 'Saved views: Recovered rule issues')
        expect(search).to_have_value('Duties')
        expect(page.locator('tr.rt-issue')).to_have_count(1)
    finally:
        context.close()


def test_saved_fairness_view_update_and_delete_undo_restore_measure_and_recover_after_reload(browser, live_server, h):
    h.publish(h.shift(h.workspace('Fairness view source'))['workspace'])  # with nothing published Fairness is one empty card (f10)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'insights/fairness')
        measure = page.get_by_label('Measure', exact=True)
        measure.select_option('hours')
        toolbar(page, 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('Recovered hours')
        save_dialog(dialog, 'Save view')
        expect(page.get_by_role('button', name='Saved views: Recovered hours', exact=True)).to_be_visible()
        original = h.get('coordinator', '/api/table-views?table=fairness')['views'][0]
        measure.select_option('duties')
        toolbar(page, 'Saved views').get_by_role('menuitem', name='Update “Recovered hours”', exact=True).click()
        page.locator('.toast').filter(has_text='View updated.').get_by_role('button', name='Undo', exact=True).click()
        expect(measure).to_have_value('hours')
        expect(page.get_by_role('button', name='Saved views: Recovered hours', exact=True)).to_be_visible()
        assert h.get('coordinator', '/api/table-views?table=fairness')['views'][0]['definition'] == original['definition']
        menu = toolbar(page, 'Saved views')
        menu.get_by_role('menuitem', name='Delete “Recovered hours”', exact=True).click()
        expect(page.locator('.toast').filter(has_text='View deleted.')).to_be_visible()
        assert h.get('coordinator', '/api/table-views?table=fairness')['views'] == []
        toolbar(page, 'Saved views').get_by_role('menuitemradio', name='Default view', exact=True).click()
        expect(measure).to_have_value('duties')
        page.locator('.toast').filter(has_text='View deleted.').get_by_role('button', name='Undo', exact=True).click()
        expect(measure).to_have_value('hours')
        expect(page.get_by_role('button', name='Saved views: Recovered hours', exact=True)).to_be_visible()
        recovered = h.get('coordinator', '/api/table-views?table=fairness')['views'][0]
        assert recovered['id'] != original['id'] and recovered['definition'] == original['definition']
        page.reload()
        toolbar(page, 'Saved views').get_by_role('menuitemradio', name='Recovered hours', exact=True).click()
        expect(measure).to_have_value('hours')
    finally:
        context.close()


def test_saved_rules_filter_edits_keep_saved_definition_until_update_and_undo(browser, live_server, h):
    flag(h)
    ending_rules(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        rules_view(page, 'Needs attention')
        condition(toolbar(page, 'Filter'), 'Rule', ['Duties per week'])  # the shared Filter
        close_popover(page)
        expect(page.locator('tr.rt-issue')).to_have_count(1)
        toolbar(page, 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('Saved duty filter')
        save_dialog(dialog, 'Save view')
        saved = page.locator('#main button.dt-saved')
        expect(saved).to_have_attribute('aria-label', 'Saved views: Saved duty filter')
        original = h.get('coordinator', '/api/table-views?table=rule-tree')['views'][0]
        assert original['definition']['filters'] == {'rule': ['Duties per week']}
        # Applying an existing view must not share its stored filter dictionary with the editable state.
        rules_view(page, 'Saved duty filter')
        toolbar(page, 'Filter').get_by_role('button', name='Clear all filters', exact=True).click()
        close_popover(page)
        expect(page.locator('tr.rt-issue')).to_have_count(2)
        expect(saved).to_have_attribute('aria-label', 'Saved views: Saved duty filter (changed)')
        menu = toolbar(page, 'Saved views')
        expect(menu.get_by_role('menuitem', name='Update “Saved duty filter”', exact=True)).to_be_visible()
        assert h.get('coordinator', '/api/table-views?table=rule-tree')['views'][0]['definition'] == original['definition']
        menu.get_by_role('menuitemradio', name='Saved duty filter', exact=True).click()
        expect(page.locator('tr.rt-issue')).to_have_count(1)
        expect(saved).to_have_attribute('aria-label', 'Saved views: Saved duty filter')
        toolbar(page, 'Filter').get_by_role('button', name='Clear all filters', exact=True).click()
        close_popover(page)
        toolbar(page, 'Saved views').get_by_role('menuitem', name='Update “Saved duty filter”', exact=True).click()
        toast = page.locator('.toast').filter(has_text='View updated.')
        expect(toast).to_be_visible()
        assert h.get('coordinator', '/api/table-views?table=rule-tree')['views'][0]['definition']['filters'] == {}
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('tr.rt-issue')).to_have_count(1)
        expect(saved).to_have_attribute('aria-label', 'Saved views: Saved duty filter')
        assert h.get('coordinator', '/api/table-views?table=rule-tree')['views'][0]['definition'] == original['definition']
        page.reload()
        expect(saved).to_have_attribute('aria-label', 'Saved views: Saved duty filter')
        expect(page.locator('tr.rt-issue')).to_have_count(1)
    finally:
        context.close()

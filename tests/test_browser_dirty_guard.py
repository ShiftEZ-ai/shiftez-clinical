"""One unsaved-changes guard reviewed: leaving a page with
unsaved changes asks with the same Keep editing / Discard changes choice as a dialog or side panel, names where you were
going, focuses Keep editing and never discards silently later."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, open_config, expect_loaded, navigate, planner_tool  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('how', ['menu', 'address'])
def test_leaving_unsaved_settings_asks_keep_editing_or_discard(browser, live_server, h, how):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Department settings')
        name = page.get_by_label('Department name', exact=True)
        saved = name.input_value()
        name.fill('Fictional unsaved department')

        def go():
            if how == 'menu':
                page.locator('#department-nav').get_by_role('button', name='People', exact=True).click()
            else:
                page.evaluate("location.hash = 'people'")
        go()
        leave = page.get_by_role('dialog', name='Leave this page?')
        expect(leave).to_contain_text('Not saved. Your changes on this page are lost if you go to People.')
        keep = leave.get_by_role('button', name='Keep editing', exact=True)
        expect(keep).to_be_focused()
        # A warning, not an error: the same bar as a dialog's guard.
        assert leave.locator('.unsaved-bar').evaluate("b => getComputedStyle(b).backgroundColor") == \
            page.evaluate("() => {const d = document.createElement('div'); d.style.background = 'var(--warn-soft)'; document.body.append(d); const c = getComputedStyle(d).backgroundColor; d.remove(); return c}")
        # Keep editing: still here, the edit intact; nothing is discarded later behind the person's back.
        keep.click()
        expect(leave).to_have_count(0)
        expect(name).to_have_value('Fictional unsaved department')
        assert page.evaluate('location.hash').startswith('#configuration')
        # Escape is Keep editing too.
        go()
        expect(leave).to_be_visible()
        page.keyboard.press('Escape')
        expect(leave).to_have_count(0)
        expect(name).to_have_value('Fictional unsaved department')
        # Discard changes goes where the person was going; the saved name is untouched.
        go()
        leave.get_by_role('button', name='Discard changes', exact=True).click()
        expect_loaded(page, 'people')
        assert h.get('admin', '/api/me')['department']['name'] == saved
    finally:
        context.close()


@pytest.mark.parametrize('section,create,save', [('Location types', 'New location type', 'Add location type'),
                                                 ('Recurring sessions', 'New recurring session', 'Add recurring session')])
def test_a_refused_empty_save_is_no_change_but_a_refused_edit_still_is(browser, live_server, section, create, save):
    """CA-F01: Add <thing> is the form's Save. Pressed on an empty form the browser refuses it and nothing changed, so
    Cancel closes at once; a real edit refused by the same Save still asks Keep editing / Discard changes."""
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, section)
        page.get_by_role('button', name=create, exact=True).click()
        dialog = page.get_by_role('dialog', name=create, exact=True)
        dialog.get_by_role('button', name=save, exact=True).click()
        assert dialog.locator('form').evaluate('f => f.checkValidity()') is False  # the required fields still refuse
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        expect(dialog).to_have_count(0)
        page.get_by_role('button', name=create, exact=True).click()
        dialog = page.get_by_role('dialog', name=create, exact=True)
        description = dialog.get_by_label('Description', exact=True)
        description.fill('Fictional unsaved description')  # a real edit; the required Name stays empty
        dialog.get_by_role('button', name=save, exact=True).click()
        assert dialog.locator('form').evaluate('f => f.checkValidity()') is False
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        keep = dialog.get_by_role('button', name='Keep editing', exact=True)
        expect(keep).to_be_focused()
        keep.click()
        expect(description).to_have_value('Fictional unsaved description')
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        dialog.get_by_role('button', name='Discard changes', exact=True).click()
        expect(dialog).to_have_count(0)
    finally:
        context.close()


def test_add_recurring_sessions_refused_untouched_cancels_without_asking(browser, live_server, h):
    """CA-F01: "Add sessions" pressed with nothing chosen is refused and adds nothing; Cancel then closes at once."""
    workspace = h.workspace('Fictional untouched recurring draft')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        planner_tool(page, 'Add recurring sessions')
        dialog = page.get_by_role('dialog', name='Add recurring sessions', exact=True)
        dialog.get_by_role('button', name='Add sessions', exact=True).click()
        expect(dialog.get_by_role('alert').first).to_be_visible()
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        expect(dialog).to_have_count(0)
        assert h.detail(workspace)['shifts'] == []
    finally:
        context.close()

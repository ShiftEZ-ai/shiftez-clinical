"""Messages (toasts): placed above the bars of whatever they sit in, out of a docked panel's way, each dismissible,
Ctrl/⌘ Z runs the newest Undo (frontend audit X-16, X-17, G-22; owner decision OD-8) and Alt M reaches it (issue 1408)."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, open_config, save_dialog  # noqa: F401
from .test_browser_person_profile import open_person, person

pytestmark = pytest.mark.browser
MESSAGE = 'Session settings saved.'


def save_sessions(page, minutes):
    page.get_by_role('button', name='Edit session settings', exact=True).click()
    dialog = page.get_by_role('dialog', name='Session settings', exact=True)
    dialog.get_by_label('Sign out after inactivity (minutes)', exact=True).fill(str(minutes))
    save_dialog(dialog, 'Save')
    expect(dialog).to_have_count(0)
    return page.locator('#toast-tray .toast').filter(has_text=MESSAGE)


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}])
def test_a_message_after_a_dialog_sits_just_above_the_page_footer_and_ctrl_z_undoes_it(browser, live_server, h, viewport):
    before = h.get('admin', '/api/access')['sessions']['idle_minutes']
    context, page = sign_in(browser, live_server, 'admin', viewport)
    try:
        open_config(page, 'Department settings')
        toast = save_sessions(page, 35)
        expect(toast).to_be_visible()
        # X-16: handed back from the closed dialog, the tray is restacked for the page: just above the status footer,
        # not floating where the dialog's footer was.
        footer = page.locator('.app-footer').bounding_box()
        box = toast.bounding_box()
        assert 0 <= footer['y'] - (box['y'] + box['height']) <= 80, (box, footer)
        expect(page.get_by_role('region', name='Messages')).to_contain_text(MESSAGE)
        assert h.get('admin', '/api/access')['sessions']['idle_minutes'] == 35
        # X-17 / OD-8: Ctrl Z outside a field runs the newest Undo.
        page.locator('#main').focus()
        page.keyboard.press('Control+z')
        expect(page.get_by_text('Undone. The previous values are back.', exact=True)).to_be_visible()
        assert h.get('admin', '/api/access')['sessions']['idle_minutes'] == before
        # Every message can be dismissed.
        done = page.locator('#toast-tray .toast').filter(has_text='Undone.')
        done.get_by_role('button', name='Dismiss message', exact=True).click()
        expect(done).to_have_count(0)
    finally:
        context.close()


def test_ctrl_z_in_a_text_field_stays_the_fields_own_undo(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Department settings')
        toast = save_sessions(page, 40)
        expect(toast.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        name = page.get_by_label('Department name', exact=True)
        name.focus()
        page.keyboard.press('Control+z')
        expect(toast.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        assert h.get('admin', '/api/access')['sessions']['idle_minutes'] == 40
    finally:
        context.close()


def test_messages_stay_clear_of_a_docked_panel(browser, live_server, h):
    """G-22: an Undo message still showing when a side panel docks moves over the page column, off the panel."""
    context, page = sign_in(browser, live_server, 'admin', {'width': 1440, 'height': 1000})
    try:
        open_config(page, 'Department settings')
        toast = save_sessions(page, 45)
        expect(toast).to_be_visible()
        panel = open_person(page, person(h))
        expect(toast).to_be_visible()
        page.wait_for_function("""() => {const t = document.querySelector('#toast-tray .toast'), p = document.querySelector('.dock-panel');
            return t && p && t.getBoundingClientRect().right <= p.getBoundingClientRect().left;}""")
        assert toast.bounding_box()['x'] + toast.bounding_box()['width'] <= panel.bounding_box()['x']
    finally:
        context.close()


def test_alt_m_reaches_the_newest_undo_which_stays_while_focused_and_hands_focus_back(browser, live_server, h):
    """Issue 1408 (WCAG 2.2 SC 2.1.1, 2.2.1, 2.4.3): one key from anywhere reaches the newest message's Undo, Escape goes
    back, the message outlives its timeout while focus is in it, and Undo (a save: the message turns into a saving note)
    or Dismiss pressed by keyboard hands focus back to the control it came from, not to the top of the document."""
    before = h.get('admin', '/api/access')['sessions']['idle_minutes']
    context, page = sign_in(browser, live_server, 'admin', {'width': 1440, 'height': 1000})
    try:
        open_config(page, 'Department settings')
        toast = save_sessions(page, 50)
        undo = toast.get_by_role('button', name='Undo', exact=True)
        origin = page.get_by_role('button', name='Edit session settings', exact=True)
        origin.focus()
        page.keyboard.press('Alt+m')
        expect(undo).to_be_focused()
        page.keyboard.press('Escape')
        expect(origin).to_be_focused()
        page.keyboard.press('Alt+m')
        expect(undo).to_be_focused()
        # An Undo message closes after 10 s; with focus in it, it stays (the page's clock runs past that at once).
        page.clock.fast_forward(11_000)
        expect(undo).to_be_visible()
        expect(undo).to_be_focused()
        page.keyboard.press('Enter')
        expect(origin).to_be_focused()
        undone = page.locator('#toast-tray .toast').filter(has_text='Undone. The previous values are back.')
        expect(undone).to_be_visible()
        assert h.get('admin', '/api/access')['sessions']['idle_minutes'] == before
        # A message with no action is reached at its Dismiss; dismissed by keyboard, focus goes back too.
        page.keyboard.press('Alt+m')
        expect(undone.get_by_role('button', name='Dismiss message', exact=True)).to_be_focused()
        page.keyboard.press('Enter')
        expect(undone).to_have_count(0)
        expect(origin).to_be_focused()
    finally:
        context.close()

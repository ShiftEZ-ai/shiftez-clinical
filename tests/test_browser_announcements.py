"""Announcements in the browser: a coordinator writes one for a rank; the right people see it and are told."""
import pytest
from playwright.sync_api import expect
from tests.outbox_support import process_one_after_undo as process_one
from .test_browser import department_tab, browser, live_server, sign_in, navigate, open_config  # noqa: F401
from .governance_support import identity

pytestmark = pytest.mark.browser


def test_coordinator_announces_to_a_rank_and_only_they_are_told(browser, live_server, h):
    me = h.get('staff', '/api/me')['person_id']
    other = next(p for p in h.seed['people'] if p != me)
    identity(h, 'colleague', ['staff'], other)
    rank = h.configure('categories', {'name': 'Night registrar', 'starts_on': '2020-01-01'})['record']
    h.command('coordinator', 'membership.create', {'person_id': me, 'category_id': rank['id'], 'starts_on': '2020-01-01'})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'inbox')
        page.get_by_role('button', name='New announcement', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Title', exact=True).fill('North clinic closed Thursday')
        form.get_by_label('Message', exact=True).fill('Use the South clinic rooms instead.')
        form.get_by_label('For', exact=True).select_option('some')
        form.locator('.cond-line').first.locator('.pick-button').click()  # "Where Rank is any of …"
        # The shared picker's rows are options of a multi-select listbox, ticked like the box they draw (X-36).
        page.locator('.picker').get_by_role('option', name='Night registrar', exact=True).check()
        page.keyboard.press('Escape')
        form.get_by_label('Importance', exact=True).select_option('important')
        form.get_by_label('Change reason', exact=True).fill('Tell the night registrars')
        form.locator('button[type=submit]').click()
        expect(page.get_by_text('Announcement saved.', exact=False)).to_be_visible()
    finally:
        context.close()
    process_one(h.app.state.engine)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page,'home')
        notice = page.locator('.home-notice-list li').filter(has_text='North clinic closed Thursday')
        expect(notice).to_contain_text('Important')
        navigate(page, 'inbox')
        expect(page.get_by_text('Important: North clinic closed Thursday')).to_be_visible()
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'colleague')
    try:
        navigate(page,'home')
        expect(page.locator('.home-notice-list li').filter(has_text='North clinic closed Thursday')).to_have_count(0)
    finally:
        context.close()


def test_must_acknowledge_survives_edits_and_staff_acknowledge_on_home_and_in_the_inbox(browser, live_server, h):
    other = next(p for p in h.seed['people'] if p != h.get('staff', '/api/me')['person_id'])
    identity(h, 'colleague', ['staff'], other)
    title = 'Fire drill procedure updated'
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'inbox')
        page.get_by_role('button', name='New announcement', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Title', exact=True).fill(title)
        form.get_by_label('Message', exact=True).fill('Read the new evacuation routes.')
        form.get_by_label('Importance', exact=True).select_option('must_acknowledge')
        form.get_by_label('Change reason', exact=True).fill('Everyone must confirm the new routes')
        form.locator('button[type=submit]').click()
        expect(page.get_by_text('Announcement saved.', exact=False)).to_be_visible()
        notice = next(i for i in h.get('coordinator', '/api/content?page_size=50')['items'] if i['name'] == title)
        assert notice['importance'] == 'must_acknowledge'
    finally:
        context.close()
    process_one(h.app.state.engine)  # delivered to inboxes before the edit
    # Reopen and change only the message: the importance is kept, not reset to Normal.
    context, page = sign_in(browser, live_server, 'admin')
    try:
        page.goto(live_server + '/#configuration/announcements?open=' + notice['id'])
        form = page.get_by_role('dialog', name='Edit announcement', exact=True)
        expect(form.get_by_label('Importance', exact=True)).to_have_value('must_acknowledge')
        form.get_by_label('Message', exact=True).fill('Read the new evacuation routes on the intranet.')
        form.get_by_label('Change reason', exact=True).fill('Clarify where the routes are')
        form.locator('button[type=submit]').click()
        expect(page.get_by_text('Announcement saved.', exact=False)).to_be_visible()
        saved = next(i for i in h.get('coordinator', '/api/content?page_size=50')['items'] if i['name'] == title)
        assert saved['importance'] == 'must_acknowledge' and saved['description'].endswith('on the intranet.')
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'home')
        notice = page.locator('.home-notice-list li').filter(has_text=title)
        notice.get_by_role('button', name='I acknowledge: ' + title, exact=True).click()
        expect(notice.get_by_text('Acknowledged', exact=True)).to_be_visible()
        item = next(i for i in h.get('staff', '/api/content?page_size=50')['items'] if i['name'] == title)
        assert item['receipt']['acknowledged_at']
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'colleague')
    try:
        navigate(page, 'inbox')
        ack = page.get_by_role('button', name='I acknowledge: Important: ' + title, exact=True)
        ack.click()
        expect(ack).to_have_count(0)
        note = next(n for n in h.get('colleague', '/api/notifications')['notifications'] if n['title'].endswith(title))
        assert note['acknowledged_at']
    finally:
        context.close()


def test_announcement_send_time_and_reminders_are_department_settings_with_undo(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Department settings')
        department_tab(page, 'Announcements')
        pane = page.locator('.config-pane')
        expect(pane.get_by_label('Announcements reach inboxes at', exact=True)).to_have_value('07:00')
        expect(pane.get_by_label('Remind people who have not read after (days)', exact=True)).to_have_value('')
        pane.get_by_label('Announcements reach inboxes at', exact=True).fill('06:30')
        pane.get_by_label('Remind people who have not read after (days)', exact=True).fill('3')
        pane.get_by_label('Hours between reminders', exact=True).fill('48')
        pane.get_by_role('button', name='Save settings', exact=True).click()
        page.get_by_role('dialog').get_by_label('Reason', exact=True).fill('Remind unread notices')
        page.get_by_role('dialog').get_by_role('button', name='Save settings', exact=True).click()
        undo = page.get_by_role('button', name='Undo', exact=True)
        expect(undo).to_be_visible()
        department = h.get('admin', '/api/me')['department']
        assert (department['announcement_send_time'], department['announcement_auto_reminder_days'],
                department['announcement_reminder_interval_hours']) == ('06:30', 3, 48)
        undo.click()
        expect(pane.get_by_label('Remind people who have not read after (days)', exact=True)).to_have_value('')
        department = h.get('admin', '/api/me')['department']
        assert (department['announcement_send_time'], department['announcement_auto_reminder_days'],
                department['announcement_reminder_interval_hours']) == ('07:00', None, 24)
    finally:
        context.close()

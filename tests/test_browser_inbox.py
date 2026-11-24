"""Inbox: a notification's kind is the server's `kind`, said the same on its card and in the Kind filter (audit R-09), and
the saved-view button names the view shown (R-23)."""
import uuid

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .test_browser import browser, live_server, sign_in, navigate, filter_by, wait_held  # noqa: F401

pytestmark = pytest.mark.browser
# The worker's titles for each kind; the weekly-unavailability title once fell under "Leave or absence" by its words.
KINDS = {'request': ('Weekly unavailability to approve for Staff 01(test)', 'Request'),
         'availability': ('Leave or absence recorded for Staff 01(test)', 'Leave or absence'),
         'swap': ('Swap offer from Staff 01(test)', 'Swap'),
         'review': ('Rota sent for review', 'Review'),
         'publication': ('October rota published', 'Rota published'),
         'announcement': ('Clinic closed on Friday', 'Announcement'),
         'rule': ('A proposal or decision on a rule', 'Rule')}  # ruleNotice's words: no policy id, so "a rule"


def notify_all(h, recipient):
    with transaction(h.app.state.engine, write=True) as db:
        for kind, (title, _) in KINDS.items():
            event = m.Outbox(department_id=h.seed['department_id'], event_key=str(uuid.uuid4()), kind=kind, payload={},
                             state='available_in_app', next_attempt_at=m.now())
            db.add(event)
            db.flush()
            db.add(m.Notification(outbox_id=event.id, identity_id=recipient, title=title, message='Fictional inbox check'))


def test_each_kind_reads_the_same_on_its_card_and_in_the_kind_filter(browser, live_server, h):
    notify_all(h, h.get('coordinator', '/api/me')['identity_id'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'inbox')
        for title, word in KINDS.values():
            card = page.locator('.note-item').filter(has_text=title)
            expect(card.locator('.note-top small')).to_contain_text(word + ' · ')
        # The Kind filter offers the same words, and filtering by one keeps exactly its card.
        filter_by(page, 'Kind', ['Request'])
        expect(page.locator('.note-item')).to_have_count(1)
        expect(page.locator('.note-item')).to_contain_text(KINDS['request'][0])
    finally:
        context.close()


def test_the_saved_view_button_names_the_view_shown(browser, live_server, h):
    notify_all(h, h.get('coordinator', '/api/me')['identity_id'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'inbox')
        views = page.get_by_role('button', name='Saved views', exact=False)
        expect(views).to_contain_text('Unread')  # opens on what is unread
        expect(views).not_to_contain_text('Default view')
        views.click()
        page.get_by_role('menuitemradio', name='All notifications', exact=False).click()
        expect(views).to_contain_text('All notifications')
    finally:
        context.close()


def test_inbox_counts_are_the_servers_and_show_more_reads_the_next_page(browser, live_server, h):
    """maintenance/maintenance: the bell, the bell's menu and the inbox say the server's unread total (not the rows of one page);
    the inbox says how many of the total it shows, and Show more reads the next page."""
    import json
    import re
    from datetime import timedelta
    at = m.now()
    notes = [{'id': str(uuid.uuid4()), 'revision': 1, 'kind': 'announcement', 'title': f'Synthetic note {i}', 'message': 'Read me',
              'created_at': (at - timedelta(minutes=i)).isoformat(), 'read_at': None} for i in range(150)]
    def page_of(route):
        query = dict(re.findall(r'(\w+)=(\d+)', route.request.url.split('?', 1)[1] if '?' in route.request.url else ''))
        offset, limit = int(query.get('offset', 0)), int(query.get('limit', 100))
        rows = notes[offset:offset + limit]
        route.fulfill(status=200, content_type='application/json', body=json.dumps(
            {'notifications': rows, 'total': 150, 'unread_total': 525, 'limit': limit, 'offset': offset, 'has_more': offset + limit < 150}))
    # The bell also follows the live pulse's unread count, read from the server: the server holds the same 525, so a
    # pulse landing after the routed read (a loaded runner) agrees with it instead of resetting the bell.
    with transaction(h.app.state.engine, write=True) as db:
        boxes = [m.Outbox(id=str(uuid.uuid4()), department_id=h.seed['department_id'], event_key=f'inbox-count:{n}',
                          kind='announcement', payload={}) for n in range(525)]
        db.add_all(boxes)
        db.flush()
        db.add_all([m.Notification(outbox_id=box.id, identity_id=h.seed['identities']['coordinator'],
                                   title='Synthetic unread note', message='Read me') for box in boxes])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route(re.compile(r'/api/notifications(\?.*)?$'), page_of)
        navigate(page, 'inbox')
        expect(page.locator('.note-item').get_by_text('Synthetic note 0', exact=True)).to_be_visible()
        line = page.locator('.inbox-more')
        expect(line).to_contain_text('Showing 100 of 150 notifications.')
        line.get_by_role('button', name='Show more', exact=True).click()
        expect(page.locator('.note-item').get_by_text('Synthetic note 149', exact=True)).to_be_attached()
        expect(line).to_be_hidden()
        # The bell's menu: the same unread total as the bell's name, and how many of all it lists.
        bell = page.locator('.topbar .bell')
        box = bell.bounding_box()
        page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)  # one real pointer click opens it
        menu = page.locator('.note-pop')
        expect(menu.locator('.note-pop-head')).to_contain_text('525 unread')
        expect(menu.locator('.note-pop-foot')).to_contain_text('Latest 8 of 150')
        expect(bell).to_have_attribute('aria-label', 'Inbox · 525 unread')
        expect(bell.locator('.count')).to_have_text('99+')
    finally:
        context.close()


def test_the_bell_opens_on_the_first_click_while_its_read_is_slow(browser, live_server):
    """maintenance: the menu opens at once (busy) and fills in when its read lands; it used to wait for the read, so the
    first click looked dead and a second one closed what the first opened."""
    import re
    held, menu_read = [], re.compile(r'/api/notifications\?limit=8$')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'home')
        page.route(menu_read, lambda route: held.append(route))
        bell = page.locator('.topbar .bell')
        box = bell.bounding_box()
        page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        menu = page.locator('.note-pop')
        expect(menu).to_be_visible()
        expect(menu).to_have_attribute('aria-busy', 'true')
        wait_held(page, held)
        held.pop().continue_()
        page.unroute(menu_read)
        expect(menu).not_to_have_attribute('aria-busy', 'true')
        expect(menu.get_by_role('button', name='Open inbox', exact=True)).to_be_visible()
        page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)  # a second click closes it
        expect(menu).to_have_count(0)
    finally:
        context.close()

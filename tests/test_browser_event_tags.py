"""Event tags use the existing shared picker with current heading choices."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, open_config, pick, sign_in  # noqa: F401
from .test_browser_tags import tag_row
from .test_events import definition, event, event_type

pytestmark = pytest.mark.browser


def test_event_tag_create_reload_remove_and_undo_keep_canonical_membership(browser, live_server, h):
    row = event(h, event_type(h, notify_inbox=False), heading='Synthetic teaching meeting')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_config(page, 'Tags')
        page.locator('.config-pane').get_by_role('button', name='New tag', exact=True).click()
        panel = page.get_by_role('region', name='New tag')
        panel.get_by_label('Name', exact=True).fill('Teaching_meetings')
        pick(panel, 'Can label', ['People'], False)
        pick(panel, 'Can label', ['Events'])
        panel.get_by_role('button', name=re.compile('Add events')).click()
        page.locator('.picker').get_by_role('option', name=row['heading'], exact=True).check()
        page.keyboard.press('Escape')
        expect(panel.locator('.tag-member-list')).to_contain_text(row['heading'])
        panel.get_by_role('button', name='Save tag', exact=True).click()
        expect(page.get_by_text('Tag #Teaching_meetings created.', exact=True)).to_be_visible()
        tag = next(item for item in h.get('coordinator', '/api/catalogues')['tags'] if item['name'] == 'Teaching_meetings')
        assert tag['labels'] == ['events'] and definition(h, row)['tag_ids'] == [tag['id']]
        page.reload()
        open_config(page, 'Tags')
        tag_row(page, tag['name']).locator('.tag-ref').first.click()
        page.locator('.popover').get_by_role('button', name='Open tag', exact=True).click()
        panel = page.get_by_role('region', name='#Teaching_meetings')
        expect(panel.locator('.tag-member-list')).to_contain_text(row['heading'])
        panel.get_by_role('button', name='Remove ' + row['heading'], exact=True).click()
        panel.get_by_role('button', name='Save tag', exact=True).click()
        expect(page.get_by_text('Tag saved.', exact=True)).to_be_visible()
        assert definition(h, row)['tag_ids'] == []
        page.get_by_role('button', name='Undo', exact=True).click()
        # The count can still be from before removal; Undo reports done after its command and refresh finish.
        expect(page.get_by_text('Undone. The previous values are back.', exact=True)).to_be_visible()
        expect(tag_row(page, tag['name']).locator('.tag-ref').first).to_contain_text('1')
        assert definition(h, row)['tag_ids'] == [tag['id']]
        members = [item for item in h.get('coordinator', '/api/catalogues')['tag_members'] if item['tag_id'] == tag['id']]
        assert [(item['kind'], item['member_id']) for item in members] == [('events', row['id'])]
    finally:
        context.close()

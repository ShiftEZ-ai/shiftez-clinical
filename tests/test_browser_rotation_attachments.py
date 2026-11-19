"""The rotation board's existing remove/Undo journey keeps the attachment lifecycle together."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, expect_loaded, live_server, navigate, sign_in  # noqa: F401
from .test_rotation_attachments import assert_attachment, attachments, rotation_value

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 900}, {'width': 390, 'height': 844}],
                         ids=['desktop', 'phone'])
def test_rotation_board_removal_and_undo_restore_the_attachment(browser, live_server, h, viewport):
    value = rotation_value(h)
    rotation = h.configure('rotations', value, role='coordinator')['record']
    assert_attachment(attachments(h)[0], value)
    context, page = sign_in(browser, live_server, 'coordinator', viewport)
    try:
        navigate(page, 'programmes/board')
        if viewport['width'] < 720:  # a phone shows the agenda; the timeline is one tap away
            page.get_by_role('button', name='Show the timeline', exact=True).click()
        board = page.get_by_role('region', name='Rotation timeline')
        bar = board.get_by_role('button', name=re.compile('^Synthetic attachment rotation,'))
        expect(bar).to_be_visible()
        bar.click()
        panel = page.locator('.dock-panel')
        panel.get_by_role('button', name='Remove', exact=True).click()
        undo = page.locator('#toast-tray .toast').filter(has=page.get_by_role('button', name='Undo', exact=True)).last.get_by_role('button', name='Undo', exact=True)
        expect(undo).to_be_visible()
        expect(bar).to_have_count(0)
        assert attachments(h) == []
        assert rotation['id'] not in {row['id'] for row in h.get('coordinator', '/api/catalogues')['rotations']}
        undo.click()
        expect(bar).to_be_visible()
        assert_attachment(attachments(h)[0], value)
        page.reload()
        expect_loaded(page, 'programmes/board')
        if viewport['width'] < 720:
            page.get_by_role('button', name='Show the timeline', exact=True).click()
        expect(bar).to_be_visible()
        [restored] = h.get('coordinator', '/api/catalogues')['rotations']
        assert restored['mentor_id'] == value['mentor_id']
    finally:
        context.close()

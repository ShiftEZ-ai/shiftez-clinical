"""Required Home-only announcements remain actionable beside legacy department links."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_department_content_review import legacy_required_links

pytestmark = pytest.mark.browser


def test_required_home_notice_is_visible_and_links_offer_only_supported_importance(browser, live_server, h):
    links, required = legacy_required_links(h)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'home')
        notice = page.locator('.home-notice-list li').filter(has_text=required['name'])
        expect(notice).to_be_visible()
        acknowledge = notice.get_by_role('button', name='I acknowledge: ' + required['name'], exact=True)
        acknowledge.click()
        expect(acknowledge).to_have_count(0)
        saved = next(item for item in h.get('staff', '/api/content?page_size=5')['items'] if item['id'] == required['id'])
        assert saved['receipt']['acknowledged_at']
        assert page.locator('.home-notice-list li').filter(has_text='A legacy link').get_by_role('button').count() == 0
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'admin')
    try:
        for importance in ('normal', 'important'):
            page.goto(live_server + '/#configuration/announcements?open=' + links[0])
            form = page.get_by_role('dialog', name='Edit announcement', exact=True)
            expect(form).to_be_visible()
            levels = form.get_by_label('Importance', exact=True)
            expect(levels.locator('option[value="must_acknowledge"]')).to_have_count(0)
            expect(levels.locator('option[value="normal"]')).to_have_count(1)
            expect(levels.locator('option[value="important"]')).to_have_count(1)
            levels.select_option(importance)
            form.get_by_label('Change reason', exact=True).fill('Save a supported link importance')
            form.locator('button[type=submit]').click()
            expect(form).not_to_be_visible()
            saved = next(item for item in h.get('coordinator', '/api/catalogues')['department_content'] if item['id'] == links[0])
            assert saved['importance'] == importance and saved['kind'] == 'link'
    finally:
        context.close()
